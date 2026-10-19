"""Managed checkout identity; Git and Codex remain the live state authorities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from ..bindings import ThreadBinding


class WorktreeConflict(RuntimeError):
    pass


class WorktreeNotFound(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class WorktreeRecord:
    id: str
    project_alias: str
    project_generation: int
    root: str
    cwd: str
    common_dir: str
    git_dir: str | None
    identity_token: str | None
    origin_binding_id: str | None
    creation_state: Literal["creating", "ready", "unknown"]
    revision: int
    created_at: str


@dataclass(frozen=True, slots=True)
class WorktreeCursor:
    created_at: str
    worktree_id: str

    def __post_init__(self) -> None:
        if not self.created_at or not self.worktree_id:
            raise ValueError("Worktree cursor fields must not be empty")


@dataclass(frozen=True, slots=True)
class WorktreePage:
    items: tuple[WorktreeRecord, ...]
    next_cursor: WorktreeCursor | None


@dataclass(frozen=True, slots=True)
class WorktreeDeleteSnapshot:
    record: WorktreeRecord
    bindings: tuple[ThreadBinding, ...]
    fingerprint: str
