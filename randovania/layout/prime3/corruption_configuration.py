import dataclasses

from randovania.games.game import RandovaniaGame
from randovania.layout.base.base_configuration import BaseConfiguration
from randovania.layout.lib.teleporters import TeleporterConfiguration
from randovania.layout.base.logical_resource_action import LayoutLogicalResourceAction


@dataclasses.dataclass(frozen=True)
class CorruptionConfiguration(BaseConfiguration):
    elevators: TeleporterConfiguration
    energy_per_tank: int = dataclasses.field(metadata={"min": 1, "max": 1000, "precision": 1})
    logical_resource_action: LayoutLogicalResourceAction
    start_with_corrupted_hypermode: bool = False

    @classmethod
    def game_enum(cls) -> RandovaniaGame:
        return RandovaniaGame.METROID_PRIME_CORRUPTION
