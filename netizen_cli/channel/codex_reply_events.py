"""Map accepted Codex starts and existing native observations to presentation events."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from functools import partial

from lark_channel import OutboundCard, OutboundPost, SendResult

from ..bindings import BindingTaskFeedback
from ..error_messages import describe_error
from ..ordinary_sessions import LogicalReply, OrdinaryConclusion, OrdinarySessionPort
from ..runtime.codex_contracts import GoalOutcome
from ..runtime.codex_replies import outcome_file_evidence
from ..runtime.contracts import GoalActivitySnapshot, GoalFinalizationStatus, GoalStateUnknown, SideTurnOutcome
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
    DeliveryPolicy,
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
        logical_turn_id: str | None = None,
        opening: GoalActivitySnapshot | None = None,
        run_reply: LogicalReply | None = None,
    ) -> TurnOpened:
        run = replace(key, turn_id=logical_turn_id) if logical_turn_id else key
        target = ReplyTarget(key, origin, run)
        owner, thread, turn = key.index

        def read() -> Activity | None:
            if key.side:
                return self.runtime.side_turn_activity(
                    owner, thread_id=thread, turn_id=turn, refresh_plan=False
                )
            if goal:
                snapshot = self.runtime.goal_activity(owner, thread_id=thread, logical_turn_id=run.turn_id)
                if snapshot is None or (snapshot.physical_turn_id or run.turn_id) != turn:
                    snapshot = opening
                return _goal_reply_activity(snapshot) if snapshot is not None else None
            return self.ordinary.turn_activity(owner, thread_id=thread, turn_id=turn)

        def observe() -> ActivityChanged | None:
            snapshot = read()
            return ActivityChanged.from_snapshot(target, snapshot)

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
            target, DeliveryPolicy.BOUNDED_FALLBACK, feedback, observe, retain,
            reply, reply_partial, validate_reply, reuse_existing=goal, run_reply=run_reply,
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
        run = replace(key, turn_id=native.logical_turn_id) if isinstance(native, GoalOutcome) and native.logical_turn_id else key
        target = ReplyTarget(key, outcome.origin, run)
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
        parts = tuple(target.partial(p) for p in outcome.partial_answers)
        continues = isinstance(native, GoalOutcome) and native.run_continues
        return TurnResult(
            target=target,
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
            run_continues=continues,
            run_reply=native.reply if isinstance(native, GoalOutcome) else None,
            earlier_delivery_incomplete=isinstance(native, GoalOutcome) and native.earlier_delivery_incomplete,
            allow_suppression=not isinstance(native, GoalOutcome)
            or (
                native.error is None
                and native.finalization_error is None
                and native.finalization is not GoalFinalizationStatus.UNKNOWN
            ),
            end=None if continues else RunEnded(target, status, _outcome_completion_mention_user_id(outcome)),
        )

    @staticmethod
    def goal_conclusion(outcome: GoalOutcome) -> OrdinaryConclusion:
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
        turn_id = outcome.final_physical_turn_id or (outcome.activity.physical_turn_id if outcome.activity else None) or outcome.logical_turn_id or "goal"
        activity = _goal_reply_activity(outcome.activity) if outcome.activity is not None else None
        return OrdinaryConclusion(
            binding_id=outcome.binding_id,
            thread_id=outcome.thread_id,
            turn_id=turn_id,
            owner_id=outcome.owner_id,
            origin=origin,
            cwd=outcome.cwd,
            status=outcome.final_turn_status if terminal else None,
            final_response=response,
            error_detail=describe_error(error) if error is not None else None,
            background_cleanup_requested=outcome.background_cleanup_requested,
            task_feedback=(
                outcome.task_feedback
                if terminal and outcome.error is None
                else replace(outcome.task_feedback, completion_mention_enabled=False)
            ),
            feedback_revision=outcome.feedback_revision,
            activity=activity if activity and activity.turn_id == turn_id else None,
            partial_answers=tuple(p for p in outcome.partial_answers if p.turn_id == turn_id and p.thread_id == outcome.thread_id),
        )
