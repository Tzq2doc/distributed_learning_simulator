from typing import Any

from cyy_naive_lib.log import get_logger

from ..common_import import (AggregationServer, FedAVGAlgorithm,
                             ParameterMessageBase, QuantServerEndpoint)
from .phase import Phase


class FedOBDServer(AggregationServer):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(algorithm=FedAVGAlgorithm(), **kwargs)
        self.__phase: Phase = Phase.STAGE_ONE
        assert isinstance(self._endpoint, QuantServerEndpoint)
        self._endpoint.quant_broadcast = True

    def _select_workers(self) -> set:
        if self.__phase != Phase.STAGE_ONE:
            return set(range(self.worker_number))
        return super()._select_workers()

    def _get_stat_key(self):
        if not self.performance_stat:
            return super()._get_stat_key()
        return max(self.performance_stat.keys()) + 1

    def _aggregate_worker_data(self) -> ParameterMessageBase:
        result: ParameterMessageBase = super()._aggregate_worker_data()
        assert result
        self._compute_stat = False
        if self.__phase == Phase.STAGE_ONE:
            self._compute_stat = True
        if "check_acc" in result.other_data:
            self._compute_stat = True
        if result.end_training:
            self.__phase = Phase.END
        match self.__phase:
            case Phase.STAGE_ONE:
                if self.round_number >= self.config.round or (
                    self.early_stop and not self.__has_improvement()
                ):
                    get_logger().warning("switch to phase 2")
                    self.__phase = Phase.STAGE_TWO
                    result.other_data["phase_two"] = True
            case Phase.STAGE_TWO:
                if self.early_stop and not self.__has_improvement():
                    get_logger().warning("stop aggregation")
                    result.end_training = True
            case Phase.END:
                pass
            case _:
                raise NotImplementedError(f"unknown phase {self.__phase}")
        return result

    def _stopped(self) -> bool:
        return self.__phase == Phase.END

    def __has_improvement(self) -> bool:
        if self.__phase == Phase.STAGE_TWO:
            return True
        return not self._convergent()
