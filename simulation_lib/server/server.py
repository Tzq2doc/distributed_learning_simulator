import copy
import functools
import os
import pickle
import random
from typing import Any

import gevent
import gevent.lock
from cyy_naive_lib.log import get_logger
from cyy_naive_lib.topology.cs_endpoint import ServerEndpoint
from cyy_torch_toolbox.inferencer import Inferencer
from cyy_torch_toolbox.ml_type import MachineLearningPhase
from cyy_torch_toolbox.typing import TensorDict

from ..executor import Executor
from ..message import Message, ParameterMessage


class Server(Executor):
    def __init__(self, task_id: int, endpoint: ServerEndpoint, **kwargs: Any) -> None:
        name: str = "server"
        if task_id is not None:
            name = f"server of {task_id}"
        super().__init__(**kwargs, name=name)
        self._endpoint: ServerEndpoint = endpoint

    @property
    def worker_number(self) -> int:
        return self.config.worker_number

    @functools.cached_property
    def tester(self) -> Inferencer:
        tester = self.config.create_inferencer(phase=MachineLearningPhase.Test)
        tester.dataset_collection.remove_dataset(phase=MachineLearningPhase.Training)
        tester.dataset_collection.remove_dataset(phase=MachineLearningPhase.Validation)
        tester.hook_config.summarize_executor = False
        return tester

    def get_metric(
        self,
        parameter_dict: TensorDict | ParameterMessage,
        keep_performance_logger: bool = True,
    ) -> dict:
        if isinstance(parameter_dict, ParameterMessage):
            parameter_dict = parameter_dict.parameter
        self.tester.model_util.load_parameter_dict(parameter_dict)
        self.tester.model_util.disable_running_stats()
        self.tester.set_device(self._get_device())
        self.tester.hook_config.log_performance_metric = keep_performance_logger
        self.tester.inference()
        metric: dict = self.tester.performance_metric.get_epoch_metrics(1)
        self._release_device_lock()
        self.tester.offload_from_device()
        return metric

    def start(self) -> None:
        with self._get_execution_context():
            with open(os.path.join(self.save_dir, "config.pkl"), "wb") as f:
                pickle.dump(self.config, f)
            self._before_start()

        worker_set: set = set()
        while not self._stopped():
            if not worker_set:
                worker_set = set(range(self._endpoint.worker_num))
            with self._get_execution_context():
                assert self._endpoint.worker_num == self.config.worker_number
                for worker_id in copy.copy(worker_set):
                    has_data: bool = self._endpoint.has_data(worker_id)
                    if has_data:
                        get_logger().debug(
                            "get result from %s worker_num %s",
                            worker_id,
                            self._endpoint.worker_num,
                        )
                        self._process_worker_data(
                            worker_id, self._endpoint.get(worker_id=worker_id)
                        )
                        worker_set.remove(worker_id)
                if worker_set:
                    get_logger().debug("wait workers %s", worker_set)

            if worker_set and not self._stopped():
                gevent.sleep(1)

        with self._get_execution_context():
            self._endpoint.close()
            self._server_exit()
            get_logger().debug("end server")

    def _before_start(self) -> None:
        pass

    def _server_exit(self) -> None:
        pass

    def _process_worker_data(self, worker_id: int, data: Message) -> None:
        raise NotImplementedError()

    def _before_send_result(self, result: Message) -> None:
        pass

    def _after_send_result(self, result: Message) -> None:
        pass

    def _send_result(self, result: Message) -> None:
        self._before_send_result(result=result)
        if "worker_result" in result.other_data:
            for worker_id, data in result.other_data["worker_result"].items():
                self._endpoint.send(worker_id=worker_id, data=data)
            return

        selected_workers = self._select_workers()
        get_logger().debug("choose workers %s", selected_workers)
        if selected_workers:
            self._endpoint.broadcast(data=result, worker_ids=selected_workers)
        unselected_workers = set(range(self.worker_number)) - selected_workers
        if unselected_workers:
            self._endpoint.broadcast(data=None, worker_ids=unselected_workers)
        self._after_send_result(result=result)

    def _select_workers(self) -> set:
        if "random_client_number" in self.config.algorithm_kwargs:
            return set(
                random.sample(
                    list(range(self.worker_number)),
                    k=self.config.algorithm_kwargs["random_client_number"],
                )
            )
        return set(range(self.worker_number))

    def _stopped(self) -> bool:
        raise NotImplementedError()
