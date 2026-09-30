"""Persisted creation choices; never effective native configuration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..session_settings import SessionSettings


class DefaultConfigurationError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_defaults") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class DefaultRule:
    id: str
    app_id: str
    kind: str
    chat_id: str | None
    keyword: str | None
    project: str
    session_settings: SessionSettings
    revision: int
    position: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "chat_id": self.chat_id,
            "keyword": self.keyword, "project": self.project,
            "session_settings": self.session_settings.to_dict(),
            "revision": self.revision, "position": self.position,
        }
