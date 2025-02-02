import datetime
import os
import uuid
from typing import Any

import hydra
import omegaconf
from cyy_torch_toolbox import Config
from cyy_torch_toolbox.dataset import ClassificationDatasetCollection
from cyy_torch_toolbox.device import get_devices

from .practitioner import Practitioner
from .sampler import get_dataset_collection_sampler


class DistributedTrainingConfig(Config):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.exp_name: str = ""
        self.distributed_algorithm: str = ""
        self.worker_number: int = 0
        self.parallel_number: int = len(get_devices())
        self.round: int = 0
        self.dataset_sampling: str = "iid"
        self.dataset_sampling_kwargs: dict[str, Any] = {}
        self.distribute_init_parameters: bool = True
        self.merge_validation_to_training_set = False
        self.log_file: str = ""
        self.limited_resource: bool = False
        self.endpoint_kwargs: dict = {}
        self.algorithm_kwargs: dict = {}

    def load_config_and_process(self, conf: Any) -> None:
        self.load_config(conf)
        task_time = datetime.datetime.now()
        date_time = f"{task_time:%Y-%m-%d_%H_%M_%S}"
        dataset_name = self.dc_config.dataset_kwargs.get(
            "name", self.dc_config.dataset_name
        )
        dir_suffix = os.path.join(
            self.distributed_algorithm,
            f"{dataset_name}_{self.dataset_sampling}"
            if isinstance(self.dataset_sampling, str)
            else f"{dataset_name}_{'_'.join(self.dataset_sampling)}",
            self.model_config.model_name,
            date_time,
            str(uuid.uuid4()),
        )
        if self.exp_name:
            dir_suffix = os.path.join(self.exp_name, dir_suffix)
        self.save_dir = os.path.join("session", dir_suffix)
        self.log_file = str(os.path.join("log", dir_suffix)) + ".log"
        assert self.reproducible_env_config.make_reproducible_env

    def create_practitioners(self) -> set:
        practitioners = set()
        dataset_collection = self.create_dataset_collection()
        assert isinstance(dataset_collection, ClassificationDatasetCollection)
        sampler = get_dataset_collection_sampler(
            name=self.dataset_sampling,
            dataset_collection=dataset_collection,
            part_number=self.worker_number,
            **self.dataset_sampling_kwargs,
        )
        for practitioner_id in range(self.worker_number):
            practitioner = Practitioner(
                practitioner_id=practitioner_id,
            )
            practitioner.set_sampler(name=self.dc_config.dataset_name, sampler=sampler)
            practitioners.add(practitioner)
        assert practitioners
        return practitioners


global_config: DistributedTrainingConfig = DistributedTrainingConfig()


def __load_config(conf) -> None:
    global_conf_path = os.path.join(
        os.path.dirname(__file__), "..", "conf", "global.yaml"
    )
    if not os.path.isfile(global_conf_path):
        global_conf_path = os.path.join(
            os.path.dirname(__file__), "conf", "global.yaml"
        )
    result_conf = omegaconf.OmegaConf.load(global_conf_path)
    result_conf.merge_with(conf)
    global_config.load_config_and_process(result_conf)


@hydra.main(config_path="../conf", version_base=None)
def load_config(conf) -> None:
    while "dataset_name" not in conf and len(conf) == 1:
        conf = next(iter(conf.values()))
    __load_config(conf)


def load_config_from_file(
    config_file: None | str = None,
) -> DistributedTrainingConfig:
    assert config_file is not None
    conf = omegaconf.OmegaConf.load(config_file)
    __load_config(conf)
    return global_config
