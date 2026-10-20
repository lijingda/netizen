"""Persisted creation choices; never effective native configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..session_settings import SessionSettings


class DefaultConfigurationError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_defaults") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class WorkspacePolicy:
    """Creation intent only: existing resources retain their exact managed ID."""

    mode: str = "project"
    worktree_id: str | None = None
    ref: str | None = None

    def __post_init__(self) -> None:
        if self.mode == "project":
            valid = self.worktree_id is None and self.ref is None
        elif self.mode == "existing":
            valid = _workspace_text(self.worktree_id) and self.ref is None
        elif self.mode == "new":
            valid = self.worktree_id is None and (self.ref is None or _workspace_text(self.ref))
        else:
            valid = False
        if not valid:
            raise DefaultConfigurationError("工作区策略无效，请重新选择项目目录、已有工作区或每次新建工作区。")

    @classmethod
    def from_dict(cls, value: Any) -> WorkspacePolicy:
        if not isinstance(value, dict):
            raise DefaultConfigurationError("workspace 必须是工作区策略对象。")
        mode = value.get("mode")
        if mode == "project" and value.keys() == {"mode"}:
            return cls()
        if mode == "existing" and value.keys() == {"mode", "worktree_id"}:
            return cls(mode, worktree_id=value["worktree_id"])
        if mode == "new" and value.keys() in ({"mode"}, {"mode", "ref"}):
            if "ref" not in value or _workspace_text(value["ref"]):
                return cls(mode, ref=value.get("ref"))
        raise DefaultConfigurationError("工作区策略字段无效，请重新选择工作区。")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"mode": self.mode}
        if self.worktree_id is not None:
            result["worktree_id"] = self.worktree_id
        if self.ref is not None:
            result["ref"] = self.ref
        return result


def _workspace_text(value: Any) -> bool:
    return (isinstance(value, str) and bool(value) and value == value.strip()
            and len(value) <= 256 and not any(ord(character) < 32 for character in value))


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
    workspace: WorkspacePolicy = field(default_factory=WorkspacePolicy)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "chat_id": self.chat_id,
            "keyword": self.keyword, "project": self.project,
            "session_settings": self.session_settings.to_dict(),
            "workspace": self.workspace.to_dict(),
            "revision": self.revision, "position": self.position,
        }
