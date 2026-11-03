"""Independent instance-wide question mention setting in the instance YAML."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from typing import Any

from .admin.port_config import ConfigFileEditor


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


class QuestionNotificationConfigError(ValueError):
    pass


class QuestionNotificationConfigConflict(QuestionNotificationConfigError):
    pass


class QuestionNotificationConfigService:
    def __init__(
        self, settings: QuestionNotificationSettings = QuestionNotificationSettings(), *,
        config_editor: ConfigFileEditor | None = None,
    ) -> None:
        self._snapshot = (settings, uuid.uuid4().hex)
        self._editor = config_editor
        self._write_lock = threading.Lock()

    @property
    def current(self) -> QuestionNotificationSettings:
        return self._snapshot[0]

    def view(self) -> dict[str, Any]:
        settings, revision = self._snapshot
        return {"settings": settings.to_dict(), "revision": revision,
                "editable": self._editor is not None}

    def update(self, settings: QuestionNotificationSettings, *, expected_revision: str) -> None:
        with self._write_lock:
            if expected_revision != self._snapshot[1]:
                raise QuestionNotificationConfigConflict("question notification settings changed")
            if self._editor is None:
                raise QuestionNotificationConfigError("instance configuration is unavailable")
            self._editor.update_section("questionNotifications", settings.to_dict())
            self._snapshot = (settings, uuid.uuid4().hex)
