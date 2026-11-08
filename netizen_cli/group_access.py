"""Optional instance-wide admission for execution in Feishu group chats."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .chat_access import exact_chat_ids, parse_chat_rule
from .admin.port_config import ConfigSection, ConfigFileEditor


GROUP_ACCESS_NOTICE = "本群尚未启用 Netizen，消息不会执行。请实例管理员在本群发送 /enable，或在 Admin 中启用本群。"


class GroupAccessError(ValueError):
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
        chat_ids = exact_chat_ids(self.chat_ids, field="groupAccess.chatIds", error=GroupAccessError)
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
        mode, chat_ids = parse_chat_rule(value, label="groupAccess",
                                         field="groupAccess.chatIds", error=GroupAccessError)
        return cls(mode, chat_ids)


class GroupAccessService(ConfigSection[GroupAccessRule]):
    """Publish immutable rules only after the shared instance YAML write succeeds."""

    def __init__(self, rule: GroupAccessRule = GroupAccessRule(), *,
                 config_editor: ConfigFileEditor | None = None) -> None:
        super().__init__(rule, section="groupAccess", editor=config_editor,
                         conflict_message="group access changed; refresh before saving")

    def view(self) -> dict[str, Any]:
        rule, revision = self.snapshot()
        return {**rule.to_dict(), "revision": revision}

    def enable_chat(self, chat_id: str) -> tuple[GroupAccessRule, bool]:
        """Append to the latest rule under the write lock, without a revision precondition."""
        return self.modify(lambda rule: None if chat_id in rule.chat_ids else
                           GroupAccessRule(rule.mode, (*rule.chat_ids, chat_id)))
