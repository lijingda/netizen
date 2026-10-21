"""Shared, explicit workspace creation intent for ordinary and scheduled sessions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class WorkspacePolicyError(ValueError):
    pass


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
            raise WorkspacePolicyError("worktree 使用方式无效，请重新选择项目目录、已有 worktree 或每次新建 worktree。")

    @classmethod
    def from_dict(cls, value: Any) -> WorkspacePolicy:
        if not isinstance(value, dict):
            raise WorkspacePolicyError("workspace 必须是 worktree 使用方式对象。")
        mode = value.get("mode")
        if mode == "project" and value.keys() == {"mode"}:
            return cls()
        if mode == "existing" and value.keys() == {"mode", "worktree_id"}:
            return cls(mode, worktree_id=value["worktree_id"])
        if mode == "new" and value.keys() in ({"mode"}, {"mode", "ref"}):
            if "ref" not in value or _workspace_text(value["ref"]):
                return cls(mode, ref=value.get("ref"))
        raise WorkspacePolicyError("worktree 使用方式字段无效，请重新选择 worktree。")

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
