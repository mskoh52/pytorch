import argparse
import copy
import json
import os
from pathlib import Path

import torch
import torch.distributed as c10d
import torch.distributed.rpc as rpc
import torch.multiprocessing as mp
from torch.distributed.rpc import TensorPipeRpcBackendOptions
from torch.futures import wait_all
from torch.utils.data import DataLoader

from benchmark_class_helper import (get_benchmark_data_map,
                                    get_benchmark_model_map,
                                    get_benchmark_server_map,
                                    get_benchmark_trainer_map)
from metrics.ProcessedMetricsPrinter import ProcessedMetricsPrinter


def get_name(rank, args):
    t_count = args.ntrainer + args.ncudatrainer
    s_count = args.nserver + args.ncudaserver
    if rank < t_count:
        return f"trainer{rank}"
    elif rank < (t_count + s_count):
        return f"server{rank}"
    else:
        return "master"


def get_server_rank(args, rank):
    s_offset = args.ntrainer + args.ncudatrainer
    tps = args.ntrainer // args.nserver
    return rank // tps + s_offset


def get_cuda_server_rank(args, rank):
    s_offset = args.ntrainer + args.ncudatrainer + args.nserver
    t_index = rank - args.ntrainer
    ctps = args.ncudatrainer // args.ncudaserver
    return t_index // ctps + s_offset


def get_server_rref(server_rank, args, extra_args):
    server = get_benchmark_server_map()[str(args.server)]
    name = get_name(
        server_rank,
        args
    )
    if extra_args is not None:
        server_args = extra_args.values()
    else:
        server_args = []
    if server_rank >= args.ntrainer + args.ncudatrainer + args.nserver:
        trainer_count = args.ncudatrainer / args.ncudaserver
        use_cuda_rpc = True
    else:
        trainer_count = args.ntrainer / args.nserver
        use_cuda_rpc = False
    return rpc.remote(
        name,
        server,
        args=(
            server_rank,
            trainer_count,
            use_cuda_rpc,
            *server_args,
        ),
    )


def run_trainer(
    args, extra_args, model, data, rank, server_rref
):
    trainer_class = get_benchmark_trainer_map()[str(args.trainer)]
    if extra_args is not None:
        trainer_args = extra_args.values()
    else:
        trainer_args = []
    trainer_count = args.ntrainer + args.ncudatrainer
    store = c10d.FileStore(args.filestore, trainer_count)
    if args.backend == "gloo":
        process_group = c10d.ProcessGroupGloo(
            store, rank, trainer_count
        )
    elif args.backend == "nccl":
        process_group = c10d.ProcessGroupNCCL(
            store, rank, trainer_count
        )
    use_cuda_rpc = rank >= args.ntrainer
    trainer = trainer_class(
        rank,
        args.ntrainer + args.ncudatrainer,
        process_group,
        use_cuda_rpc,
        server_rref,
        args.backend,
        args.epochs,
        *trainer_args
    )
    trainer.train(model, data)
    metrics = trainer.get_metrics()
    return [rank, metrics]


def call_trainers(args, extra_args, model, train_data, server_rrefs):
    futs = []
    for trainer_rank in range(0, args.ntrainer + args.ncudatrainer):
        trainer_name = get_name(
            trainer_rank,
            args
        )
        server_rref = None
        if server_rrefs:
            if trainer_rank >= args.ntrainer:
                server_rank = get_cuda_server_rank(args, trainer_rank)
            else:
                server_rank = get_server_rank(args, trainer_rank)
            server_rref = server_rrefs[server_rank]
        fut = rpc.rpc_async(
            trainer_name,
            run_trainer,
            args=(
                args,
                extra_args,
                copy.deepcopy(model),
                train_data[trainer_rank],
                trainer_rank,
                server_rref,
            ),
            timeout=args.rpc_timeout
        )
        futs.append(fut)
    return futs


def benchmark_warmup(
    args, extra_args, model, data, server_rrefs
):
    futs = call_trainers(args, extra_args, model, data, server_rrefs)
    wait_all(futs)
    for server_rref in server_rrefs.values():
        server_rref.rpc_sync().reset_state(server_rref)
    print("benchmark warmup done\n")


def split_list(arr, n):
    return [arr[i::n] for i in range(n)]


def get_server_metrics(server_rrefs):
    rank_metrics = []
    for rank, server_rref in server_rrefs.items():
        metrics = server_rref.rpc_sync().get_metrics(server_rref)
        rank_metrics.append([rank, metrics])
    return rank_metrics


def run_master(rank, model, data, args, extra_configs, rpc_backend_options):
    world_size = args.ntrainer + args.ncudatrainer + args.nserver + args.ncudaserver + 1
    rpc.init_rpc(
        get_name(
            rank,
            args
        ),
        rank=rank,
        world_size=world_size,
        rpc_backend_options=rpc_backend_options
    )
    server_rrefs = {}
    for i in range(
        args.ntrainer + args.ncudatrainer, world_size - 1
    ):
        server_rrefs[i] = get_server_rref(i, args, extra_configs["server_config"])
    train_data = split_list(
        list(DataLoader(data, batch_size=args.batch_size)),
        args.ntrainer + args.ncudatrainer
    )

    # warmup run the benchmark
    benchmark_warmup(
        args, extra_configs["trainer_config"], model, train_data, server_rrefs
    )
    # run the benchmark
    trainer_futs = call_trainers(
        args, extra_configs["trainer_config"], model, train_data, server_rrefs
    )
    # collect metrics and print
    metrics_printer = ProcessedMetricsPrinter()
    rank_metrics_list = wait_all(trainer_futs)
    metrics_printer.print_metrics("trainer", rank_metrics_list)
    rank_metrics_list = get_server_metrics(server_rrefs)
    metrics_printer.print_metrics("parameter server", rank_metrics_list)


def run_benchmark(rank, model, data, args, config):

    torch.manual_seed(args.torch_seed)
    torch.cuda.manual_seed_all(args.cuda_seed)
    torch.backends.cudnn.benchmark = True
    torch.backends.cudnn.deterministic = True

    world_size = args.ntrainer + args.ncudatrainer + args.nserver + args.ncudaserver + 1
    os.environ['MASTER_ADDR'] = args.master_addr
    os.environ['MASTER_PORT'] = args.master_port
    rpc_backend_options = TensorPipeRpcBackendOptions(rpc_timeout=args.rpc_timeout)
    if rank == world_size - 1:
        # master = [ntrainer + ncudatrainer + nserver + ncudaserver, ntrainer + ncudatrainer + nserver + ncudaserver]
        run_master(rank, model, data, args, config, rpc_backend_options)
    elif rank >= args.ntrainer + args.ncudatrainer:
        # parameter_servers = [ntrainer + ncudatrainer, ntrainer + ncudatrainer + nserver + ncudaserver)
        rpc.init_rpc(
            get_name(
                rank,
                args
            ),
            rank=rank,
            world_size=world_size,
            rpc_backend_options=rpc_backend_options
        )
    else:
        # trainers = [0, ntrainer + ncudatrainer)
        if rank >= args.ntrainer:
            server_rank = get_cuda_server_rank(args, rank)
            server_name = get_name(server_rank, args)
            rpc_backend_options.set_device_map(
                server_name,
                {rank: server_rank}
            )
        trainer_name = get_name(
            rank,
            args
        )
        rpc.init_rpc(
            trainer_name,
            rank=rank,
            world_size=world_size,
            rpc_backend_options=rpc_backend_options
        )
    rpc.shutdown()


def get_json_config(file_name, id):
    f = open(
        os.path.join(
            Path(__file__).parent, file_name
        ),
        "r"
    )
    json_config = json.load(f)[id]
    f.close()
    return json_config


def load_extra_configs(args):
    trainer_config_file = args.trainer_config_path
    server_config_file = args.server_config_path
    configurations = {
        "trainer_config": None,
        "server_config": None
    }
    if args.trainer is not None and trainer_config_file is not None:
        configurations["trainer_config"] = get_json_config(trainer_config_file, args.trainer)
    if args.server is not None and server_config_file is not None:
        configurations["server_config"] = get_json_config(server_config_file, args.server)
    return configurations


def get_data(data_class, data_config):
    data_class = get_benchmark_data_map()[data_class]
    return data_class(**data_config)


def load_data(args):
    data_config_file = args.data_config_path
    data_config = get_json_config(data_config_file, args.data)
    return get_data(data_config["data_class"], data_config["configurations"])


def get_model(model_class, model_config):
    model_class = get_benchmark_model_map()[model_class]
    return model_class(**model_config)


def load_model(args):
    model_config_file = args.model_config_path
    model_config = get_json_config(model_config_file, args.model)
    return get_model(model_config["model_class"], model_config["configurations"])


def main(args):

    # CPU and RPC trainer checks
    if args.ntrainer > 0 and args.ncudatrainer > 0:
        assert args.nserver > 0 and args.ncudaserver > 0
    if args.nserver > 0:
        assert args.ntrainer > 0
        assert args.ntrainer % args.nserver == 0
    if args.ncudaserver > 0:
        assert args.ncudatrainer > 0
        assert args.ncudatrainer % args.ncudaserver == 0

    extra_configs = load_extra_configs(args)
    data = load_data(args)
    model = load_model(args)

    world_size = (
        args.ntrainer + args.ncudatrainer + args.nserver + args.ncudaserver + 1
    )

    mp.spawn(
        run_benchmark,
        args=(
            model,
            data,
            args,
            extra_configs,
        ),
        nprocs=world_size,
        join=True
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RPC server Benchmark")
    parser.add_argument(
        "--master_addr",
        type=str,
        help="IP address of the machine that will host the process with rank 0"
    )
    parser.add_argument(
        "--master_port",
        type=str,
        help="A free port on the machine that will host the process with rank 0"
    )
    parser.add_argument(
        "--trainer",
        type=str,
        help="trainer map key to get trainer class for benchmark run"
    )
    parser.add_argument(
        "--ntrainer",
        type=int,
        help="trainer count for benchmark run"
    )
    parser.add_argument(
        "--ncudatrainer",
        type=int,
        help="cudatrainer count for benchmark run"
    )
    parser.add_argument(
        "--filestore",
        type=str,
        help="filestore location for process group"
    )
    parser.add_argument(
        "--server",
        type=str,
        help="server map key to get trainer class for benchmark run"
    )
    parser.add_argument(
        "--nserver",
        type=int,
        help="server count for benchmark run"
    )
    parser.add_argument(
        "--ncudaserver",
        type=int,
        help="cudaserver count for benchmark run"
    )
    parser.add_argument(
        "--rpc_timeout",
        type=int,
        help="timeout in seconds to use for RPC"
    )
    parser.add_argument(
        "--backend",
        type=str,
        help="distributed communication backend to use for benchmark run"
    )
    parser.add_argument(
        "--epochs",
        type=int,
        help="epoch count for training"
    )
    parser.add_argument(
        "--batch_size",
        type=int,
        default=1,
        help="number of training examples used in one iteration"
    )
    parser.add_argument(
        "--data",
        type=str,
        help="id for data configuration"
    )
    parser.add_argument(
        "--model",
        type=str,
        help="id for model configuration"
    )
    parser.add_argument(
        "--data_config_path",
        type=str,
        help="path to data configuration file"
    )
    parser.add_argument(
        "--model_config_path",
        type=str,
        help="path to model configuration file"
    )
    parser.add_argument(
        "--server_config_path",
        type=str,
        help="path to server configuration file"
    )
    parser.add_argument(
        "--trainer_config_path",
        type=str,
        help="path to trainer configuration file"
    )
    parser.add_argument(
        "--torch_seed",
        type=int,
        default=0,
        help="seed for generating random numbers to a non-deterministic random number"
    )
    parser.add_argument(
        "--cuda_seed",
        type=int,
        default=0,
        help="seed for generating random numbers to a random number for the current GPU"
    )
    args = parser.parse_args()
    print(f"{args}\n")
    main(args)
