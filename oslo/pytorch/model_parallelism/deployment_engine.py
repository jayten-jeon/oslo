from contextlib import suppress
from functools import partial
from time import time

import torch
import torch.multiprocessing as mp
import os
import torch.distributed as dist
import numpy as np
import random

from oslo.pytorch.model_parallelism.network.broadcaster import Broadcaster
from oslo.pytorch.model_parallelism.network.mpu import MPU
from oslo.pytorch.model_parallelism.tensor_parallel_enigne import TensorParallelEngine


class DeploymentEngine(object):

    __SUPPORTED_METHOD__ = ["forward", "generate", "to", "cpu", "cuda"]

    def __init__(
        self, model, mapping, tp_size, pp_size, master_addr, master_port, seed
    ):
        self.model = model
        self.mapping = mapping
        self.tp_size = tp_size
        self.pp_size = pp_size
        self.master_addr = master_addr
        self.master_port = master_port
        self.seed = seed
        self.processes = []

    def parallelize(self):
        for rank in range(self.tp_size * self.pp_size):
            parallel_mutex = mp.Event()
            inference_mutex = mp.Event()

            process = DeploymentProcess(
                rank=rank,
                model=self.model,
                mapping=self.mapping,
                tp_size=self.tp_size,
                pp_size=self.pp_size,
                master_addr=self.master_addr,
                master_port=self.master_port,
                seed=self.seed,
                parallel_mutex=parallel_mutex,
                inference_mutex=inference_mutex,
            )

            process.daemon = True
            process.start()
            self.processes.append(process)

        for process in self.processes:
            process.parallel_mutex.wait()

        for method_name in self.__SUPPORTED_METHOD__:
            new_method = partial(self.send, oslo_deployment_method_name=method_name)
            setattr(self.model, method_name, new_method)

    def deparallelize(self):
        for process in self.processes:
            process.join()

    @staticmethod
    def assert_about_device(*args, **kwargs):
        for arg in args:
            if torch.is_tensor(arg):
                assert arg.is_cuda, "input data must be cuda."
        for v in kwargs.values():
            if torch.is_tensor(v):
                assert v.is_cuda, "input data must be cuda."

    def send(self, *args, **kwargs):
        self.assert_about_device(*args, **kwargs)
        method_name = kwargs.pop("oslo_deployment_method_name")

        for process in self.processes:
            process.inference_mutex.set()


class DeploymentProcess(mp.Process):
    def __init__(
        self,
        rank,
        model,
        mapping,
        tp_size,
        pp_size,
        master_addr,
        master_port,
        seed,
        parallel_mutex,
        inference_mutex,
    ):
        super().__init__()
        self.model = model
        self.rank = rank
        self.mapping = mapping
        self.pp_size = pp_size
        self.tp_size = tp_size
        self.master_addr = master_addr
        self.master_port = master_port
        self.seed = seed
        self.parallel_mutex = parallel_mutex
        self.inference_mutex = inference_mutex

    def init_environment_variables(self):
        os.environ["RANK"] = str(self.rank)
        os.environ["LOCAL_RANK"] = str(self.rank)
        os.environ["WORLD_SIZE"] = str(self.tp_size * self.pp_size)
        os.environ["MASTER_PORT"] = str(self.master_port)
        os.environ["MASTER_ADDR"] = str(self.master_addr)
        self.mpu = MPU(self.tp_size, self.pp_size)

    def wait_request(self):
        if self.seed is None:
            seed = torch.tensor(int(time())).cuda()
            dist.broadcast(seed, src=0)
            seed = seed.item()
        else:
            seed = self.seed

        torch.manual_seed(seed)
        np.random.seed(seed)
        random.seed(seed)

        with suppress():
            torch.backends.cudnn.deterministic = True
            torch.backends.cudnn.benchmark = False

        while True:
            self.inference_mutex.wait()
            self.inference_mutex.clear()

    def run(self) -> None:
        self.init_environment_variables()
        tp_engine = TensorParallelEngine(self.model, self.mpu, self.mapping)
        tp_engine.parallelize()

        self.parallel_mutex.set()
        self.wait_request()