"""Safe, structured failures at the managed Worktree application boundary."""
from __future__ import annotations

from typing import Any


class WorktreeError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}
