"""Map verified Claude ownership and native lifecycle evidence to reply events."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace

from lark_channel import OutboundCard, OutboundPost, SendResult

from ..ordinary_sessions import (
    CLAUDE_RELEASE_HINT,
    CliStateChanged,
    ConversationTarget,
    LogicalTerminal,
    OrdinarySessionPort,
    PhysicalAnswer,
    PhysicalStarted,
)
from .app_values import _task_has_error
from .reply_events import (
    ActivityChanged,
    DeliveryPolicy,
    ItemDelivery,
    PresentationKey,
    ReplyTarget,
    RunDisplay,
    RunEnded,
    RunState,
    TerminalStatus,
    TurnOpened,
    TurnResult,
    terminal_view,
)
from .topics import CardReplyLocation


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
            return ActivityChanged.from_snapshot(target, snapshot)

        return TurnOpened(
            target,
            DeliveryPolicy.ITEM_RECEIPTS,
            event.feedback,
            observe,
            lambda: not ordinary.reply_blocked_by_lifecycle(key.owner_id),
            reply,
            partial,
            retained=event.retained,
            location=location,
            run_reply=event.reply,
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
        status, body = terminal_view(
            event.status, _task_has_error(event),
            failure=lambda: event.error_detail or "Claude 本次执行失败。",
            interruption=lambda: "Claude 本次执行已停止。",
            answer=event.final_response, empty="",
        )

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
            count = event.state().tasks
            text = f"\n\n后台任务 {count} 个运行中，可继续发送消息" if count else ""
            if count or event.session_has_work():
                text += f"\n\n{CLAUDE_RELEASE_HINT}"
            return text

        display = RunDisplay(event.reply, event.publishable, lambda: event.state().line)
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
            physical_turn_id=(event.activity and event.activity.physical_turn_id) or key.turn_id,
            allow_suppression=False,
            run_continues=event.run_continues,
            end=None,
            items=ItemDelivery(display, location, send, suffix, event.delivery_deadline),
        )

    @staticmethod
    def lifecycle(
        event: CliStateChanged | LogicalTerminal, location: Callable[[], CardReplyLocation]
    ) -> RunEnded | RunState:
        run = PresentationKey(event.binding_id, event.thread_id, event.turn_id)
        target = ReplyTarget(run, event.origin, run)
        state = event.current if isinstance(event, CliStateChanged) else event.state
        display = RunDisplay(event.reply, event.publishable, lambda: state().line)
        if isinstance(event, CliStateChanged):
            return RunState(target, display=display, background=event.background)
        status: TerminalStatus = (
            "completed"
            if event.status == "completed"
            else "interrupted"
            if event.status == "interrupted"
            else "failed"
        )
        return RunEnded(target, status, event.mention_user_id, display, location())
