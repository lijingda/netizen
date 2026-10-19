"""Instance-wide target-chat access for the ordinary session collaboration tools."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from ..admin.port_config import ConfigFileEditor, ConfigFileSnapshot


class SessionPolicyError(ValueError):
    pass


class SessionPolicyConflict(SessionPolicyError):
    pass


@dataclass(frozen=True, slots=True)
class SessionAccessRule:
    mode: str = "all"
    chat_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.mode, str) or self.mode not in {"all", "only", "exclude", "disabled"}:
            raise SessionPolicyError("mode must be all, only, exclude, or disabled")
        if not isinstance(self.chat_ids, tuple) or len(self.chat_ids) > 1000:
            raise SessionPolicyError("chatIds must contain at most 1000 chat IDs")
        if any(not isinstance(value, str) or not value or len(value) > 256
               or value != value.strip() or any(ord(c) < 32 for c in value)
               for value in self.chat_ids):
            raise SessionPolicyError("chatIds must contain non-empty exact chat IDs")
        if self.mode in {"all", "disabled"} and self.chat_ids:
            raise SessionPolicyError("all and disabled modes do not accept chatIds")
        object.__setattr__(self, "chat_ids", tuple(sorted(set(self.chat_ids))))

    def allows(self, chat_id: str) -> bool:
        return (self.mode == "all" or self.mode == "only" and chat_id in self.chat_ids
                or self.mode == "exclude" and chat_id not in self.chat_ids)

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "chatIds": list(self.chat_ids)}

    @classmethod
    def from_mapping(cls, value: object) -> SessionAccessRule:
        if not isinstance(value, Mapping) or value.keys() - {"mode", "chatIds"}:
            raise SessionPolicyError("access rule must contain only mode and chatIds")
        raw_ids = value.get("chatIds", [])
        if not isinstance(raw_ids, list):
            raise SessionPolicyError("chatIds must be a list")
        mode = value.get("mode", "all")
        if not isinstance(mode, str):
            raise SessionPolicyError("mode must be a string")
        return cls(mode, tuple(raw_ids))


@dataclass(frozen=True, slots=True)
class SessionAccessPolicy:
    discovery: SessionAccessRule = SessionAccessRule()
    read_send: SessionAccessRule = SessionAccessRule()

    def to_dict(self) -> dict[str, Any]:
        return {"discovery": self.discovery.to_dict(), "readSend": self.read_send.to_dict()}

    @classmethod
    def from_mapping(cls, value: object) -> SessionAccessPolicy:
        if not isinstance(value, Mapping) or value.keys() - {"discovery", "readSend"}:
            raise SessionPolicyError("sessionTools must contain only discovery and readSend")
        return cls(SessionAccessRule.from_mapping(value.get("discovery", {})),
                   SessionAccessRule.from_mapping(value.get("readSend", {})))


class SessionPolicyService:
    """One shared current policy; saves reuse the existing instance YAML writer.

    The revision is process-local, like Admin action grants and tool cursors.
    No interaction history, access index, or additional configuration file exists.
    """

    def __init__(self, policy: SessionAccessPolicy = SessionAccessPolicy(), *,
                 config_snapshot: ConfigFileSnapshot | None = None,
                 config_editor: ConfigFileEditor | None = None) -> None:
        self._policy = policy
        self._editor = config_editor or (ConfigFileEditor(config_snapshot) if config_snapshot else None)
        self._revision = uuid.uuid4().hex
        self._lock = threading.RLock()

    @property
    def revision(self) -> str:
        return self._revision

    @property
    def editable(self) -> bool:
        return self._editor is not None

    def snapshot(self) -> tuple[SessionAccessPolicy, str]:
        """Capture immutable rules for one tool call and its discovery cursor."""
        with self._lock:
            return self._policy, self._revision

    def allows_discovery(self, chat_id: str) -> bool:
        return self._policy.discovery.allows(chat_id)

    def allows_read_send(self, chat_id: str) -> bool:
        return self._policy.read_send.allows(chat_id)

    def view(self) -> dict[str, Any]:
        with self._lock:
            return {"revision": self._revision, **self._policy.to_dict()}

    def update(self, policy: SessionAccessPolicy, *, expected_revision: str) -> None:
        with self._lock:
            if expected_revision != self._revision:
                raise SessionPolicyConflict("session tool access changed; refresh before saving")
            if self._editor is None:
                raise SessionPolicyError("instance configuration is unavailable")
            self._editor.update_section("sessionTools", policy.to_dict())
            self._policy = policy
            # Invalidate old action grants/cursors even when a rule changes back.
            self._revision = uuid.uuid4().hex
