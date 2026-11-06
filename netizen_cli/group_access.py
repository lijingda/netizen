"""Optional instance-wide admission for execution in Feishu group chats."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .admin.port_config import ConfigFileEditor


GROUP_ACCESS_NOTICE = "本群尚未启用 Netizen，消息不会执行。请实例管理员在本群发送 /enable，或在 Admin 中启用本群。"


class GroupAccessError(ValueError):
    pass


class GroupAccessConflict(GroupAccessError):
    pass


class GroupAccessLimitExceeded(GroupAccessError):
    pass


class GroupNotEnabled(GroupAccessError):
    def __init__(self) -> None:
        super().__init__(GROUP_ACCESS_NOTICE)


@dataclass(frozen=True, slots=True)
class GroupAccessRule:
    mode: str = "all"
    chat_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.mode, str) or self.mode not in {"all", "only"}:
            raise GroupAccessError("groupAccess.mode must be all or only")
        if not isinstance(self.chat_ids, tuple):
            raise GroupAccessError("groupAccess.chatIds must be a list")
        if any(not isinstance(value, str) or not value or len(value) > 256
               or value != value.strip() or any(ord(c) < 32 for c in value)
               for value in self.chat_ids):
            raise GroupAccessError("groupAccess.chatIds must contain non-empty exact chat IDs")
        chat_ids = tuple(sorted(set(self.chat_ids)))
        if len(chat_ids) > 1000:
            raise GroupAccessLimitExceeded("groupAccess.chatIds must contain at most 1000 chat IDs")
        object.__setattr__(self, "chat_ids", chat_ids)

    def allows(self, chat_id: str, chat_kind: str | None) -> bool:
        # TOPIC is deliberately not an input: direct chats also have topics.
        return (self.mode == "all" or chat_kind == "p2p"
                or chat_id in self.chat_ids)

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "chatIds": list(self.chat_ids)}

    @classmethod
    def from_mapping(cls, value: object) -> GroupAccessRule:
        if not isinstance(value, Mapping) or value.keys() - {"mode", "chatIds"}:
            raise GroupAccessError("groupAccess must contain only mode and chatIds")
        raw_ids = value.get("chatIds", [])
        if not isinstance(raw_ids, list):
            raise GroupAccessError("groupAccess.chatIds must be a list")
        return cls(value.get("mode", "all"), tuple(raw_ids))


class GroupAccessService:
    """Publish immutable rules only after the shared instance YAML write succeeds."""

    def __init__(self, rule: GroupAccessRule = GroupAccessRule(), *,
                 config_editor: ConfigFileEditor | None = None) -> None:
        self._snapshot = (rule, uuid.uuid4().hex)
        self._editor = config_editor
        self._write_lock = threading.Lock()

    @property
    def editable(self) -> bool:
        return self._editor is not None

    def snapshot(self) -> tuple[GroupAccessRule, str]:
        return self._snapshot

    def view(self) -> dict[str, Any]:
        rule, revision = self._snapshot
        return {**rule.to_dict(), "revision": revision}

    def update(self, rule: GroupAccessRule, *, expected_revision: str) -> None:
        with self._write_lock:
            self._update(rule, expected_revision=expected_revision)

    def enable_chat(self, chat_id: str, *, expected_revision: str) -> tuple[GroupAccessRule, bool]:
        """Add one chat without overwriting a concurrent Admin edit or mode change."""
        with self._write_lock:
            if expected_revision != self._snapshot[1]:
                raise GroupAccessConflict("group access changed; refresh before saving")
            rule = self._snapshot[0]
            if chat_id in rule.chat_ids:
                return rule, False
            rule = GroupAccessRule(rule.mode, (*rule.chat_ids, chat_id))
            self._update(rule, expected_revision=expected_revision)
            return rule, True

    def _update(self, rule: GroupAccessRule, *, expected_revision: str) -> None:
        if expected_revision != self._snapshot[1]:
            raise GroupAccessConflict("group access changed; refresh before saving")
        if self._editor is None:
            raise GroupAccessError("instance configuration is unavailable")
        self._editor.update_section("groupAccess", rule.to_dict())
        self._snapshot = (rule, uuid.uuid4().hex)
