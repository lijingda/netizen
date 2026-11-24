"""Map verified Claude ownership and native lifecycle evidence to reply events."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace

from lark_channel import OutboundCard, OutboundPost, SendResult

from ..ordinary_sessions import (
    CliStateChanged,
    ConversationTarget,
    LogicalTerminal,
    OrdinarySessionPort,
    PhysicalAnswer,
    PhysicalStarted,
    ClaudeCliState,
    RunCardState,
)
from .app_values import _task_has_error
from .reply_events import (
    ActivityChanged,
    ItemDelivery,
    PresentationKey,
    ReplyTarget,
    RunDisplay,
    RunEnded,
    RunState,
    TerminalStatus,
    TurnOpened,
    TurnResult,
)
from .topics import CardReplyLocation


def _card_state(state: ClaudeCliState, verified_status: str | None) -> RunCardState:
    return RunCardState(state.line, verified_status, (state.idle, state.tasks, state.crons))


class ClaudeReplyEvents:
    @staticmethod
    def opened(
        event: PhysicalStarted,
        ordinary: OrdinarySessionPort,
        location: CardReplyLocation,
        reply: Callable[[OutboundCard], Awaitable[SendResult]],
        partial: Callable[[OutboundPost], Awaitable[SendResult]],
    ) -> TurnOpened:
        key = PresentationKey(event.binding_id, event.thread_id, event.turn_id)
        target = ReplyTarget(key, event.origin, key)

        def observe() -> ActivityChanged | None:
            snapshot = ordinary.turn_activity(key.owner_id, thread_id=key.thread_id, turn_id=key.turn_id)
            if snapshot is None or not key.matches(snapshot):
                return None
            return ActivityChanged(
                target, snapshot, tuple(target.partial(p) for p in snapshot.partial_answers)
            )

        return TurnOpened(
            target,
            event.feedback,
            observe,
            lambda: not ordinary.reply_blocked_by_lifecycle(key.owner_id),
            reply,
            partial,
            retained=event.retained,
            location=location,
        )

    @staticmethod
    def result(
        event: PhysicalAnswer,
        location: CardReplyLocation,
        deliver: Callable[
            [object, str | OutboundPost | OutboundCard, CardReplyLocation], Awaitable[SendResult]
        ],
    ) -> TurnResult:
        key = PresentationKey(event.binding_id, event.thread_id, event.turn_id)
        origin = event.origin
        if isinstance(origin, ConversationTarget):
            origin = replace(origin, single_attempt=True)
        run = replace(key, turn_id=event.logical_id) if event.logical_id else None
        target = ReplyTarget(key, origin, run)
        status: TerminalStatus
        if _task_has_error(event) or event.status not in {"completed", "interrupted"}:
            status, body = "failed", event.error_detail or "Claude 本次执行失败。"
        elif event.status == "interrupted":
            status, body = "interrupted", "Claude 本次执行已停止。"
        else:
            status, body = "completed", event.final_response or ""

        async def send(
            content: str | OutboundPost | OutboundCard, operation: str | None, destination: CardReplyLocation
        ) -> SendResult:
            source = origin
            if isinstance(source, ConversationTarget) and operation:
                source = replace(source, delivery_id=source.delivery_id + "/" + operation)
                if isinstance(content, OutboundPost):
                    content = content.markdown or ""
            return await deliver(source, content, destination)

        def suffix() -> str:
            count = event.owned_tasks()
            return f"\n\n后台任务 {count} 个运行中，可继续发送消息" if count else ""

        def run_status() -> str | None:
            if event.reply is None:
                return status
            # A queued older answer can reach Feishu after a later result has
            # already proved the run terminal. Only its latest answer may inherit that proof.
            if event.turn_id == event.reply.latest_answer_turn_id:
                return event.reply.verified_status
            return None

        display = RunDisplay(event.reply, location, event.publishable, lambda: _card_state(event.state(), run_status()))
        return TurnResult(
            target=target,
            status=status,
            content=body,
            final_response=event.final_response,
            task_feedback=event.task_feedback,
            cwd=event.cwd,
            activity=event.activity,
            partials=tuple(target.partial(p) for p in event.partial_answers),
            file_evidence=event.file_evidence,
            file_binding_id=key.owner_id,
            physical_turn_id=key.turn_id,
            allow_suppression=False,
            end=None,
            items=ItemDelivery(display, send, suffix, event.delivery_deadline),
        )

    @staticmethod
    def lifecycle(
        event: CliStateChanged | LogicalTerminal, location: CardReplyLocation
    ) -> RunEnded | RunState:
        run = PresentationKey(event.binding_id, event.thread_id, event.turn_id)
        target = ReplyTarget(
            replace(run, turn_id=event.reply.confirmed_turn_id or run.turn_id), event.origin, run
        )
        state = event.current if isinstance(event, CliStateChanged) else event.state
        display = RunDisplay(
            event.reply,
            location,
            event.publishable,
            lambda: _card_state(state(), event.reply.verified_status),
        )
        if isinstance(event, CliStateChanged):
            return RunState(target, display=display, background=event.background)
        status: TerminalStatus = (
            "completed"
            if event.status == "completed"
            else "interrupted"
            if event.status == "interrupted"
            else "failed"
        )
        return RunEnded(target, status, event.mention_user_id, display)
