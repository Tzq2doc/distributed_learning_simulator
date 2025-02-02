import json
import os
import pickle
from typing import Any

from cyy_naive_lib.log import get_logger
from cyy_torch_toolbox.typing import TensorDict

from ..algorithm.aggregation_algorithm import AggregationAlgorithm
from ..message import Message, ParameterMessage, ParameterMessageBase
from ..util.model_cache import ModelCache
from .server import Server


class AggregationServer(Server):
    def __init__(self, algorithm: AggregationAlgorithm, **kwargs: Any) -> None:
        Server.__init__(self, **kwargs)
        self._model_cache: ModelCache = ModelCache()
        self._round_number: int = 1
        self.__worker_flag: set = set()
        self.__algorithm: AggregationAlgorithm = algorithm
        self.__stat: dict = {}
        self._compute_stat: bool = True
        self.__plateau = 0
        self.__max_acc = 0
        self.need_init_performance = False
        self.__early_stop = self.config.algorithm_kwargs.get("early_stop", False)
        if self.__early_stop:
            get_logger().warning("stop early")

    @property
    def early_stop(self) -> bool:
        return self.__early_stop

    @property
    def algorithm(self):
        return self.__algorithm

    @property
    def round_number(self):
        return self._round_number

    def __get_init_model(self) -> TensorDict:
        parameter_dict: TensorDict = {}
        init_global_model_path = self.config.algorithm_kwargs.get(
            "global_model_path", None
        )
        if init_global_model_path is not None:
            with open(os.path.join(init_global_model_path), "rb") as f:
                parameter_dict = pickle.load(f)
        else:
            parameter_dict = self.tester.model_util.get_parameter_dict()
            # save GPU memory
            self.tester.offload_from_device()
        return parameter_dict

    def _before_start(self) -> None:
        if self.config.distribute_init_parameters:
            self._send_result(
                ParameterMessage(
                    in_round=True,
                    parameter=self.__get_init_model(),
                    other_data={"init": True},
                )
            )

    def _server_exit(self) -> None:
        self.__algorithm.exit()

    def _process_worker_data(self, worker_id: int, data: Message) -> None:
        assert 0 <= worker_id < self.worker_number
        get_logger().debug("get data %s from worker %s", data, worker_id)
        self.__algorithm.process_worker_data(
            worker_id=worker_id,
            worker_data=data,
            save_dir=self.config.get_save_dir(),
            old_parameter_dict=self._model_cache.parameter_dict,
        )
        self.__worker_flag.add(worker_id)
        if len(self.__worker_flag) == self.worker_number:
            result = self._aggregate_worker_data()
            self._send_result(result)
            self.__worker_flag.clear()
        else:
            get_logger().debug(
                "we have %s committed, and we need %s workers,skip",
                len(self.__worker_flag),
                self.worker_number,
            )

    def _aggregate_worker_data(self) -> Any:
        return self.__algorithm.aggregate_worker_data()

    def _before_send_result(self, result: Message) -> None:
        if not isinstance(result, ParameterMessageBase):
            return
        assert isinstance(result, ParameterMessage)
        if self.need_init_performance:
            assert self.config.distribute_init_parameters
        if self.need_init_performance and "init" in result.other_data:
            self.__record_compute_stat(result.parameter, keep_performance_logger=False)
            self.__stat[0] = self.__stat.pop(1)
        elif self._compute_stat and "init" not in result.other_data:
            self.__record_compute_stat(result.parameter)
            if not result.end_training and self.early_stop and self._convergent():
                result.end_training = True
        elif result.end_training:
            self.__record_compute_stat(result.parameter)
        model_path = os.path.join(
            self.config.save_dir,
            "aggregated_model",
            f"round_{self.round_number}.pk",
        )
        self._model_cache.cache_parameter_dict(result.parameter, model_path)
        # if "partial_parameter" in result:
        #     return
        # if self.config.limited_resource:
        #     result["parameter"] = ParameterFileMessage(
        #         path=self._model_cache.get_parameter_path()
        #     )

    def _after_send_result(self, result: Any) -> None:
        if isinstance(result, ParameterMessageBase) and not result.in_round:
            self._round_number += 1
        self.__algorithm.clear_worker_data()

    def _stopped(self) -> bool:
        return self._round_number > self.config.round

    @property
    def performance_stat(self) -> dict:
        return self.__stat

    def _get_stat_key(self):
        return self._round_number

    def __record_compute_stat(
        self, parameter_dict: TensorDict, keep_performance_logger: bool = True
    ) -> None:
        self.tester.set_visualizer_prefix(f"round: {self._round_number},")
        metric = self.get_metric(
            parameter_dict, keep_performance_logger=keep_performance_logger
        )
        round_stat = {f"test_{k}": v for k, v in metric.items()}

        key = self._get_stat_key()
        assert key not in self.__stat

        self.__stat[key] = round_stat
        with open(
            os.path.join(self.save_dir, "round_record.json"),
            "wt",
            encoding="utf8",
        ) as f:
            json.dump(self.__stat, f)

        max_acc = max(t["test_accuracy"] for t in self.__stat.values())
        if max_acc > self.__max_acc:
            self.__max_acc = max_acc
            with open(os.path.join(self.save_dir, "best_global_model.pk"), "wb") as f:
                pickle.dump(
                    parameter_dict,
                    f,
                )

    def _convergent(self) -> bool:
        max_acc = max(t["test_accuracy"] for t in self.performance_stat.values())
        diff = 0.001
        if max_acc > self.__max_acc + diff:
            self.__max_acc = max_acc
            self.__plateau = 0
            return False
        del max_acc
        get_logger().error(
            "max acc is %s diff is %s",
            self.__max_acc,
            self.__max_acc
            - self.performance_stat[self._get_stat_key()]["test_accuracy"],
        )
        self.__plateau += 1
        get_logger().error("plateau is %s", self.__plateau)
        if self.__plateau >= 5:
            return True
        return False
