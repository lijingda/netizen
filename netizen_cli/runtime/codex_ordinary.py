"""Ordinary session operations on the instance's existing Codex runtime."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..bindings import ThreadBinding
from ..ordinary_sessions import PreparedPrompt
from .contracts import ContextCursorCommit, Submission, SubmissionAdmission, TurnActivitySnapshot

if TYPE_CHECKING:
    from ..codex_runtime import CodexRuntime


class CodexOrdinarySessions:
    def __init__(self, runtime: CodexRuntime) -> None:
        self._runtime = runtime

    async def capture_submission_admission(self, binding_id: str) -> SubmissionAdmission:
        return await self._runtime.capture_submission_admission(binding_id)

    async def submit(
        self, *, binding: ThreadBinding, cwd: Path | None, input: PreparedPrompt, owner_id: str,
        origin: object, admission: SubmissionAdmission, context_commit: ContextCursorCommit | None,
        skill_names: tuple[str, ...],
    ) -> Submission:
        return await self._runtime.submit(
            binding=binding, cwd=cwd, input=input, owner_id=owner_id, origin=origin,
            admission=admission, context_commit=context_commit, skill_names=skill_names,
        )

    def turn_activity(
        self, binding_id: str, *, thread_id: str, turn_id: str,
    ) -> TurnActivitySnapshot | None:
        return self._runtime.turn_activity(
            binding_id, thread_id=thread_id, turn_id=turn_id, refresh_plan=False,
        )

    def reply_blocked_by_lifecycle(self, binding_id: str) -> bool:
        return self._runtime.lifecycle_state(binding_id) is not None
