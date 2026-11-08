"""Instance-wide target-chat access for the ordinary session collaboration tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..chat_access import exact_chat_ids, parse_chat_rule
from ..admin.port_config import ConfigSection, ConfigFileEditor, ConfigFileSnapshot


class SessionPolicyError(ValueError):
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
        chat_ids = exact_chat_ids(self.chat_ids, field="chatIds", error=SessionPolicyError)
        if self.mode in {"all", "disabled"} and self.chat_ids:
            raise SessionPolicyError("all and disabled modes do not accept chatIds")
        object.__setattr__(self, "chat_ids", chat_ids)

    def allows(self, chat_id: str) -> bool:
        return (self.mode == "all" or self.mode == "only" and chat_id in self.chat_ids
                or self.mode == "exclude" and chat_id not in self.chat_ids)

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "chatIds": list(self.chat_ids)}

    @classmethod
    def from_mapping(cls, value: object) -> SessionAccessRule:
        mode, chat_ids = parse_chat_rule(value, label="access rule",
                                         field="chatIds", error=SessionPolicyError)
        if not isinstance(mode, str):
            raise SessionPolicyError("mode must be a string")
        return cls(mode, chat_ids)


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


class SessionPolicyService(ConfigSection[SessionAccessPolicy]):
    """One shared current policy; saves reuse the existing instance YAML writer.

    The revision is process-local, like Admin action grants and tool cursors.
    No interaction history, access index, or additional configuration file exists.
    """

    def __init__(self, policy: SessionAccessPolicy = SessionAccessPolicy(), *,
                 config_snapshot: ConfigFileSnapshot | None = None,
                 config_editor: ConfigFileEditor | None = None) -> None:
        super().__init__(policy, section="sessionTools",
                         editor=config_editor or (ConfigFileEditor(config_snapshot) if config_snapshot else None),
                         conflict_message="session tool access changed; refresh before saving")

    def allows_discovery(self, chat_id: str) -> bool:
        return self.current.discovery.allows(chat_id)

    def allows_read_send(self, chat_id: str) -> bool:
        return self.current.read_send.allows(chat_id)

    def view(self) -> dict[str, Any]:
        policy, revision = self.snapshot()
        return {"revision": revision, **policy.to_dict()}
