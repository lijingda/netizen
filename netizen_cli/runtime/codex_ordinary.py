"""Ordinary session operations on the instance's existing Codex runtime."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..bindings import ThreadBinding
from ..ordinary_sessions import PreparedPrompt, StopPreview
from .turn_start import StartedTurn, TurnStartAttempt
from .contracts import (
    ActiveTurnSnapshot, ContextCursorCommit, StopAcknowledger, StopDisposition, Submission, SubmissionAdmission,
    TurnActivitySnapshot,
)

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

    def capture_turn_start(self, binding_id: str) -> TurnStartAttempt | None:
        return self._runtime.capture_turn_start(binding_id)

    async def preview_stop(self, binding: ThreadBinding) -> StopPreview | None:
        start = self.capture_turn_start(binding.id)
        if start is not None:
            return StopPreview("本轮正在启动，Codex 确认启动后将立即中断。", start)
        active = self._runtime.active_turn(binding.id)
        goal = self._runtime.active_goal(binding.id)
        compacting = self._runtime.is_compacting(binding.id)
        if active is None and goal is None and not compacting:
            persisted_goal = await self._runtime.goal_snapshot(binding)
            goal = self._runtime.active_goal(binding.id)
            if persisted_goal is None or goal is None:
                return None
        return StopPreview("正在暂停 Codex Goal 并中断当前物理 Turn。" if goal is not None else "正在中断当前 Codex Turn。")

    async def stop_exact(
        self, binding_id: str, *, acknowledge: StopAcknowledger | None = None,
        expected_activity_revision: int | None = None, expected_turn_id: str | None = None,
        start: StartedTurn | None = None,
    ) -> StopDisposition:
        if expected_activity_revision is None:
            if expected_turn_id is not None:
                raise ValueError("expected_turn_id requires expected_activity_revision")
            if start is not None:
                return await self._runtime.stop_exact(binding_id, acknowledge=acknowledge, start=start)
            return await self._runtime.stop_exact(binding_id, acknowledge=acknowledge)
        return await self._runtime.stop_exact(
            binding_id, acknowledge=acknowledge, expected_activity_revision=expected_activity_revision,
            expected_turn_id=expected_turn_id,
        )

    async def recheck_turn_exact(
        self, binding_id: str, *, expected_activity_revision: int, expected_turn_id: str,
    ) -> ActiveTurnSnapshot:
        return await self._runtime.recheck_turn_exact(
            binding_id, expected_activity_revision=expected_activity_revision, expected_turn_id=expected_turn_id,
        )
