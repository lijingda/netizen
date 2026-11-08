"""Independent instance-wide question mention setting in the instance YAML."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .admin.port_config import ConfigSection, ConfigFileEditor


@dataclass(frozen=True, slots=True)
class QuestionNotificationSettings:
    mention_initiator: bool = False

    def __post_init__(self) -> None:
        if type(self.mention_initiator) is not bool:
            raise ValueError("mentionInitiator must be a boolean")

    def to_dict(self) -> dict[str, bool]:
        return {"mentionInitiator": self.mention_initiator}

    @classmethod
    def from_dict(cls, value: object) -> QuestionNotificationSettings:
        if not isinstance(value, dict) or set(value) != {"mentionInitiator"}:
            raise ValueError("questionNotifications must contain only mentionInitiator")
        return cls(mention_initiator=value["mentionInitiator"])


class QuestionNotificationConfigService(ConfigSection[QuestionNotificationSettings]):
    def __init__(
        self, settings: QuestionNotificationSettings = QuestionNotificationSettings(), *,
        config_editor: ConfigFileEditor | None = None,
    ) -> None:
        super().__init__(settings, section="questionNotifications", editor=config_editor,
                         conflict_message="question notification settings changed")

    def view(self) -> dict[str, Any]:
        settings, revision = self.snapshot()
        return {"settings": settings.to_dict(), "revision": revision,
                "editable": self.editable}
