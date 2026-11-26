"""Map accepted Codex starts and existing native observations to presentation events."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from functools import partial

from lark_channel import OutboundCard, OutboundPost, SendResult

from ..bindings import BindingTaskFeedback
from ..error_messages import describe_error
from ..ordinary_sessions import OrdinaryConclusion, OrdinarySessionPort
from ..runtime.codex_contracts import GoalOutcome
from ..runtime.codex_replies import outcome_file_evidence
from ..runtime.contracts import GoalFinalizationStatus, GoalStateUnknown, SideTurnOutcome, TerminalStateUnknown
from ..codex_runtime import CodexRuntime
from .app_values import (
    _goal_reply_activity,
    _outcome_completion_mention_user_id,
    _task_failure_text,
    _task_has_error,
    _task_interruption_text,
)
from .reply_events import (
    Activity,
    ActivityChanged,
    PresentationKey,
    ReplyTarget,
    RunEnded,
    TurnOpened,
    TurnResult,
    terminal_view,
)


class CodexReplyEvents:
    def __init__(self, runtime: CodexRuntime, ordinary: OrdinarySessionPort) -> None:
        self.runtime = runtime
        self.ordinary = ordinary

    def opened(
        self,
        key: PresentationKey,
        origin: object,
        feedback: BindingTaskFeedback,
        *,
        reply: Callable[[OutboundCard], Awaitable[SendResult]],
        reply_partial: Callable[[OutboundPost], Awaitable[SendResult]],
        validate_reply: Callable[[SendResult], Awaitable[bool]] | None = None,
        goal: bool = False,
        parent_binding_id: str | None = None,
        root_message_id: str | None = None,
    ) -> TurnOpened:
        target = ReplyTarget(key, origin)
        owner, thread, turn = key.index

        def read() -> Activity | None:
            if key.side:
                return self.runtime.side_turn_activity(
                    owner, thread_id=thread, turn_id=turn, refresh_plan=False
                )
            if goal:
                snapshot = self.runtime.goal_activity(owner, thread_id=thread, logical_turn_id=turn)
                return _goal_reply_activity(snapshot) if snapshot is not None else None
            return self.ordinary.turn_activity(owner, thread_id=thread, turn_id=turn)

        def observe() -> ActivityChanged | None:
            snapshot = read()
            return ActivityChanged.from_snapshot(target, snapshot, presentation_id=turn if goal else None)

        def retain() -> bool:
            if key.side:
                return True
            if read() is None:
                return False
            return (
                self.runtime.lifecycle_state(owner) is None
                if goal
                else not self.ordinary.reply_blocked_by_lifecycle(owner)
            )

        return TurnOpened(
            target, feedback, observe, retain,
            reply, reply_partial, validate_reply, reuse_existing=goal,
            process_binding_id=parent_binding_id, process_root_id=root_message_id,
        )

    @classmethod
    def result(cls, native: OrdinaryConclusion | SideTurnOutcome | GoalOutcome) -> TurnResult:
        if isinstance(native, GoalOutcome):
            outcome: OrdinaryConclusion | SideTurnOutcome = cls.goal_conclusion(native)
        else:
            outcome = native
        side = isinstance(outcome, SideTurnOutcome)
        owner = outcome.side_id if isinstance(outcome, SideTurnOutcome) else outcome.binding_id
        key = PresentationKey(owner, outcome.thread_id, outcome.turn_id, side)
        target = ReplyTarget(key, outcome.origin)
        status, content = terminal_view(
            outcome.status, _task_has_error(outcome),
            failure=lambda: _task_failure_text(outcome),
            interruption=lambda: _task_interruption_text(outcome),
            answer=outcome.final_response, empty="任务已结束，未产生文本回复。",
        )
        if isinstance(native, OrdinaryConclusion):
            evidence = native.file_evidence
        else:
            evidence = partial(outcome_file_evidence, native)
        # The existing Goal tap attributes these items to this exact stream. Only
        # their display key is shared; item identity retains the native physical ID.
        parts = tuple(
            target.partial(p, presentation_id=key.turn_id if isinstance(native, GoalOutcome) else None)
            for p in outcome.partial_answers
        )
        return TurnResult(
            target=target,
            execution_unknown=outcome.execution_unknown,
            status=status,
            content=content,
            final_response=outcome.final_response,
            task_feedback=outcome.task_feedback,
            cwd=outcome.cwd,
            activity=outcome.activity,
            partials=parts,
            file_evidence=evidence,
            file_binding_id=outcome.parent_binding_id if isinstance(outcome, SideTurnOutcome) else owner,
            physical_turn_id=(native.final_physical_turn_id or key.turn_id)
            if isinstance(native, GoalOutcome)
            else key.turn_id,
            allow_suppression=not isinstance(native, GoalOutcome)
            or (
                native.error is None
                and native.finalization_error is None
                and native.finalization is not GoalFinalizationStatus.UNKNOWN
            ),
            end=RunEnded(target, status, _outcome_completion_mention_user_id(outcome)),
        )

    @staticmethod
    def goal_conclusion(outcome: GoalOutcome) -> OrdinaryConclusion:
        # The SDK logical stream is one reply lifecycle. Its identifier is only
        # the presenter key; Files always retain the verified final physical ID.
        origin = outcome.origin
        terminal = outcome.final_physical_turn_id is not None and outcome.final_turn_status in {
            "completed",
            "interrupted",
            "failed",
        }
        response = outcome.final_response if terminal else None
        error = outcome.error or (None if terminal else GoalStateUnknown("Goal 终态尚未确认。"))
        if terminal:
            # An uncertain control receipt must not hide an independently
            # confirmed final answer, nor reinterpret its files as logical-ID files.
            notices = []
            if error is not None:
                notices.append(f"Goal 操作结果未确认：{describe_error(error)}")
                error = None
            if outcome.finalization is GoalFinalizationStatus.UNKNOWN:
                detail = (
                    describe_error(outcome.finalization_error)
                    if outcome.finalization_error
                    else "自动结束结果未确认"
                )
                notices.append(f"Goal 已完成，但自动结束结果未确认：{detail}")
            if notices:
                response = "\n\n".join(filter(None, [response, *notices]))
        return OrdinaryConclusion(
            binding_id=outcome.binding_id,
            thread_id=outcome.thread_id,
            turn_id=outcome.logical_turn_id or outcome.final_physical_turn_id or "goal",
            owner_id=outcome.owner_id,
            origin=origin,
            cwd=outcome.cwd,
            status=outcome.final_turn_status if terminal else None,
            final_response=response,
            error_detail=describe_error(error) if error is not None else None,
            execution_unknown=not terminal and isinstance(outcome.error, (GoalStateUnknown, TerminalStateUnknown)),
            background_cleanup_requested=outcome.background_cleanup_requested,
            task_feedback=(
                outcome.task_feedback
                if terminal and outcome.error is None
                else replace(outcome.task_feedback, completion_mention_enabled=False)
            ),
            feedback_revision=outcome.feedback_revision,
            activity=None if outcome.activity is None else _goal_reply_activity(outcome.activity),
            partial_answers=outcome.partial_answers,
        )
