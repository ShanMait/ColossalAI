#!/usr/bin/env python
# -*- encoding: utf-8 -*-

import argparse
import pprint
import os
from colossalai.nn.optimizer.colossalai_optimizer import ColossalaiOptimizer
import numpy as np
import torch
import torch.nn as nn

from pathlib import Path
from typing import Iterable, Union, Optional, Tuple, List, Dict

from colossalai.amp import convert_to_amp, AMP_TYPE
from colossalai.context import Config, ParallelMode, ConfigException
from colossalai.core import global_context as gpc
from colossalai.engine import Engine
from colossalai.logging import get_dist_logger
from colossalai.utils import (accumulate_gradient, get_current_device,
                              sync_model_param_in_dp, is_using_ddp, is_using_pp)
from colossalai.zero import convert_to_zero, ZeroRedundancyOptimizer_Level_2, ZeroRedundancyOptimizer_Level_3
from colossalai.builder.builder import build_gradient_handler
from torch.optim.optimizer import Optimizer
from torch.optim.lr_scheduler import _LRScheduler
from torch.utils.data import DataLoader
from torch.nn.modules.loss import _Loss
from torch.nn.parallel import DistributedDataParallel as DDP


def get_default_parser():
    '''Reads user command line and uses an argument parser to parse the input arguments.
    Input arguments include configuration, host, port, world size, local rank, backend for torch.distributed.

    :return: returns the parser with the default arguments, the user may add customized arguments into this parser
    :rtype: Namespace
    '''
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=str, help='path to the config file')
    parser.add_argument('--host',
                        type=str,
                        help='the master address for distributed training')
    parser.add_argument('--port',
                        type=int,
                        help='the master port for distributed training')
    parser.add_argument('--world_size', type=int, help='world size for distributed training')
    parser.add_argument('--rank', type=int, help='rank for the default process group')
    parser.add_argument('--local_rank',
                        type=int,
                        help='local rank on the node')
    parser.add_argument('--backend',
                        type=str,
                        default='nccl',
                        help='backend for distributed communication')
    return parser


def launch(config: Union[str, Path, Config, Dict],
           rank: int,
           world_size: int,
           host: str,
           port: int,
           backend: str = 'nccl',
           local_rank: int = None,
           seed: int = 1024,
           verbose: bool = True):
    '''This function first parses the configuration arguments, using :func:parse_args() in case one of the input arguments are not given.
    Then initialize and set distributed environment by calling global_context's functions.

    :param config: config file or config file path are both acceptable
    :type config: Union[str, dict, Config]
    :param rank: rank for the default process group
    :type rank: int
    :param world_size: world size of the default process group
    :type world_size: int
    :param host: the master address for distributed training
    :type host: str
    :param port: the master port for distributed training
    :type port: str
    :param backend: backend for torch.distributed
    :type backend: str
    :param local_rank: rank for the process on the node and is used to set the default CUDA device,
    defaults to None. If local_rank = None, the default device ordinal will be calculated automatically
    :type local_rank: int, optional
    :param verbose: whether to print logs
    :type verbose: bool
    :raises Exception: raise exception when config type is wrong
    '''
    gpc.verbose = verbose

    # set config
    assert isinstance(config, (Config, str, Path, dict)), \
        f'expected argument config to be Config, str or Path, but got {type(config)}'
    if not isinstance(config, Config) and isinstance(config, dict):
        config = Config(config)
    if isinstance(config, (str, Path)):
        config = Config.from_file(config)
    gpc.load_config(config)

    # init default process group
    gpc.init_global_dist(rank, world_size, backend, host, port)

    # init process groups for different parallel modes from config
    gpc.init_parallel_groups()

    # set cuda device
    if torch.cuda.is_available():
        # if local rank is not given, calculate automatically
        gpc.set_device(local_rank)

    gpc.set_seed(seed)

    if verbose:
        logger = get_dist_logger()
        logger.info(f'Distributed environment is initialized, '
                    f'data parallel size: {gpc.data_parallel_size}, pipeline parallel size: {gpc.pipeline_parallel_size}, '
                    f'tensor parallel size: {gpc.tensor_parallel_size}', ranks=[0])


def launch_from_slurm(config: Union[str, Path, Config, Dict],
                      host: str,
                      port: int,
                      backend: str = 'nccl',
                      seed: int = 1024,
                      verbose: bool = True):
    '''A wrapper for colossalai.launch for SLURM launcher by reading rank and world size from the environment variables
    set by SLURM

    :param config: config file or config file path are both acceptable
    :type config: Union[str, dict, Config]
    :param host: the master address for distributed training
    :type host: str
    :param port: the master port for distributed training
    :type port: str
    :param backend: backend for torch.distributed
    :type backend: str
    :param verbose: whether to print logs
    :type verbose: bool
    '''
    rank = int(os.environ['SLURM_PROCID'])
    world_size = int(os.environ['SLURM_NPROCS'])
    launch(config=config,
           rank=rank,
           world_size=world_size,
           host=host,
           port=port,
           backend=backend,
           seed=seed,
           verbose=verbose)


def launch_from_openmpi(config: Union[str, Path, Config, Dict],
                        host: str,
                        port: int,
                        backend: str = 'nccl',
                        seed: int = 1024,
                        verbose: bool = True):
    '''A wrapper for colossalai.launch for OpenMPI launcher by reading rank and world size from the environment variables
    set by OpenMPI

    :param config: config file or config file path are both acceptable
    :type config: Union[str, dict, Config]
    :param host: the master address for distributed training
    :type host: str
    :param port: the master port for distributed training
    :type port: str
    :param backend: backend for torch.distributed
    :type backend: str
    :param verbose: whether to print logs
    :type verbose: bool
    '''
    rank = int(os.environ['OMPI_COMM_WORLD_RANK'])
    local_rank = int(os.environ['OMPI_COMM_WORLD_LOCAL_RANK'])
    world_size = int(os.environ['OMPI_COMM_WORLD_SIZE'])
    launch(config=config,
           local_rank=local_rank,
           rank=rank,
           world_size=world_size,
           host=host,
           port=port,
           backend=backend,
           seed=seed,
           verbose=verbose)


def launch_from_torch(config: Union[str, Path, Config, Dict],
                      host: str,
                      port: int,
                      backend: str = 'nccl',
                      seed: int = 1024,
                      verbose: bool = True):
    '''A wrapper for colossalai.launch for torchrun or torch.distributed.launch by reading rank and world size 
    from the environment variables set by PyTorch

    :param config: config file or config file path are both acceptable
    :type config: Union[str, dict, Config]
    :param host: the master address for distributed training
    :type host: str
    :param port: the master port for distributed training
    :type port: str
    :param backend: backend for torch.distributed
    :type backend: str
    :param verbose: whether to print logs
    :type verbose: bool
    '''
    rank = int(os.environ['RANK'])
    local_rank = int(os.environ['LOCAL_RANK'])
    world_size = int(os.environ['WORLD_SIZE'])
    launch(config=config,
           local_rank=local_rank,
           rank=rank,
           world_size=world_size,
           host=host,
           port=port,
           backend=backend,
           seed=seed,
           verbose=verbose)


def initialize(model: Union[nn.Module, List[nn.Module]],
               optimizer: Union[Optimizer, List[Optimizer]],
               criterion: Union[_Loss, List[_Loss]],
               train_dataloader: Optional[Union[Iterable, List[Iterable]]] = None,
               test_dataloader: Optional[Union[Iterable, List[Iterable]]] = None,
               lr_scheduler: _LRScheduler = None,
               verbose: bool = True
               ) -> Tuple[Engine, DataLoader, DataLoader]:
    ''' Core function to wrap the essential training components with our functionality based on the config which is loaded into gpc.config.

    :param model: your model instance
    :type model: :class:`torch.nn.Module`
    :param optimizer: your optimizer instance
    :type optimizer: :class:`torch.optim.optimizer.Optimizer`
    :param criterion: your criterion instance
    :type criterion: :class:`torch.nn.modules.loss._Loss`
    :param train_dataloader: dataloader for training data
    :type train_dataloader: :class:`torch.utils.data.DataLoader`
    :param train_dataloader: dataloader for testing data
    :type train_dataloader: :class:`torch.utils.data.DataLoader`
    :param lr_scheduler: your lr scheduler instance
    :type lr_scheduler: :class:`torch.nn.lr_scheduler._LRScheduler`
    :param verbose: whether to print logs
    :type verbose: bool
    :return: (engine, train_dataloader, test_dataloader, lr_scheduler)
    :rtype: tuple
    '''
    # get logger
    logger = get_dist_logger()
    gpc.verbose = verbose

    # get config from gpc
    config = gpc.config

    # print config
    if verbose:
        logger.info(f"\n========== Your Config ========\n"
                    f"{pprint.pformat(gpc.config)}\n"
                    f"================================\n", ranks=[0])

    # cudnn
    cudnn_benchmark = config.get('cudnn_benchmark', True)
    cudnn_deterministic = config.get('cudnn_deterministic', False)
    torch.backends.cudnn.benchmark = cudnn_benchmark
    torch.backends.cudnn.deterministic = cudnn_deterministic
    if verbose:
        logger.info(
            f"cuDNN benchmark = {cudnn_benchmark}, deterministic = {cudnn_deterministic}", ranks=[0])

    # first sync model across dp ranks
    model.to(get_current_device())
    use_zero3 = hasattr(gpc.config, 'zero') and gpc.config.zero.level == 3
    if not use_zero3:
        sync_model_param_in_dp(model)

    # check amp and zero
    fp16_cfg = gpc.config.get('fp16', None)
    zero_cfg = gpc.config.get('zero', None)

    if fp16_cfg is not None and fp16_cfg.mode is not None and zero_cfg is not None:
        raise ConfigException(
            "It is not allowed to set fp16 and zero configuration in your config file at the same time")

    # initialize amp
    amp_mode = None
    if fp16_cfg is not None and fp16_cfg.mode is not None:
        cfg_ = fp16_cfg.copy()
        amp_mode = cfg_.pop('mode')
        model, optimizer, criterion = convert_to_amp(model=model,
                                                     optimizer=optimizer,
                                                     criterion=criterion,
                                                     mode=amp_mode,
                                                     amp_config=cfg_)

    if zero_cfg is not None:
        cfg_ = zero_cfg.copy()
        level = cfg_.pop('level')
        model, optimizer = convert_to_zero(model=model,
                                           optimizer=optimizer,
                                           level=level,
                                           zero_config=cfg_
                                           )

    # gradient handler
    gradient_handler_cfg = gpc.config.get('gradient_handler', None)
    if gradient_handler_cfg is None:
        # if gradient handler is not specified in the configuration file,
        # check in the following order
        # 1. if optimizer is ZERO, then use zero grad handler
        # 2. if dp size is larger than 1 and pipeline is not used, use pytorch ddp
        # 3. if using pipeline and dp size larger than 1, use data parallel grad handler
        if isinstance(optimizer, (ZeroRedundancyOptimizer_Level_2,
                                  ZeroRedundancyOptimizer_Level_3)):
            gradient_handler_cfg = [dict(type='ZeROGradientHandler')]
            if verbose:
                logger.info(
                    "Training with zero is detected, ZeROGradientHandler is automatically "
                    "added even though not specified in the configuration",
                    ranks=[0])
        elif is_using_ddp() and not is_using_pp() and amp_mode != AMP_TYPE.NAIVE:
            model = DDP(model, process_group=gpc.get_group(ParallelMode.DATA))
            if verbose:
                logger.info(
                    'Model is using torch.nn.parallel.DistributedDataParallel', ranks=[0])
        elif is_using_ddp():
            gradient_handler_cfg = [dict(type='DataParallelGradientHandler')]
            if verbose:
                logger.info(
                    "Data parallel training is detected when using pipeline parallel, DataParallelGradientHandler is automatically "
                    "added even though not specified in the configuration",
                    ranks=[0])
    else:
        if not isinstance(gradient_handler_cfg, list):
            raise ConfigException(
                f"expected gradient_handler in the configuration file to be a list but got {type(gradient_handler_cfg)}")

    if gradient_handler_cfg is None:
        gradient_handlers = None
        if verbose and not isinstance(model, DDP):
            logger.warning(
                "No PyTorch DDP or gradient handler is set up, please make sure you do not need "
                "to all-reduce the gradients after a training step.",
                ranks=[0])
    else:
        gradient_handlers = [build_gradient_handler(cfg, model, optimizer) for cfg in gradient_handler_cfg]

    # check if optimizer is ColossalaiOptimizer
    if not isinstance(optimizer, (ColossalaiOptimizer, ZeroRedundancyOptimizer_Level_2, ZeroRedundancyOptimizer_Level_3)):
        optimizer = ColossalaiOptimizer(optim=optimizer)

    # gradient accumulation
    grad_accum_size = gpc.config.get('gradient_accumulation', None)
    if grad_accum_size is not None:
        optimizer, train_dataloader, gradient_handlers, lr_scheduler = accumulate_gradient(model=model,
                                                                                           optimizer=optimizer,
                                                                                           dataloader=train_dataloader,
                                                                                           accumulate_size=grad_accum_size,
                                                                                           gradient_handlers=gradient_handlers,
                                                                                           lr_scheduler=lr_scheduler)

    # clip grad norm
    clip_grad_norm = gpc.config.get('clip_grad_norm', 0.0)
    if clip_grad_norm > 0:
        if zero_cfg is not None:
            raise ConfigException(
                "clip_grad_norm should be specified with zero, you should specify clip_grad in zero configuration")
        elif fp16_cfg is not None and fp16_cfg.mode == AMP_TYPE.NAIVE:
            raise ConfigException(
                "clip_grad_norm should be specified with AMP_TYPE.NAIVE, you should specify clip_grad in fp16 configuration")

    engine = Engine(
        model=model,
        optimizer=optimizer,
        criterion=criterion,
        gradient_handlers=gradient_handlers,
        clip_grad_norm=clip_grad_norm
    )

    return engine, train_dataloader, test_dataloader, lr_scheduler
