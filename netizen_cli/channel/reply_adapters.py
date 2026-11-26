"""Channel entry points for native execution and management receipts."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace


from ..bindings import (
    SideTopicNotFound,
    SideTopicState,
    ThreadBinding,
)
from ..domain import (
    FeishuScope,
    ScheduledOrigin,
    ScopeKind,
)
from ..error_messages import describe_error
from ..management import (
    ExactBindingTarget,
)
from ..ordinary_sessions import (
    OrdinaryConclusion,
    OrdinaryEvent,
)
from ..runtime.codex_contracts import GoalOutcome
from ..runtime.codex_replies import CodexFeatureEvent
from ..runtime.contracts import (
    CompactionOutcome,
    GoalSubmission,
    SideLifecycleOutcome,
    SideTurnOutcome,
    ThreadActivityDiscardedOutcome,
    TurnObservationUnavailableOutcome,
    TurnOutcome,
)
from ..schedules.models import ScheduleNotFound
from . import constants
from .app_values import (
    _CardReplyTarget,
    _inbound_root_message_id,
    _message_id,
    _ScheduledCompletion,
)
from .base import (
    CHANNEL_LOGGER_NAME,
)
from .codex_reply_events import CodexReplyEvents
from .process_presentation import ProcessLocation
from .presentation import CompletionPresentationChannel
from .reply_events import DeliveryReceipt, PresentationKey, ReplyTarget, RunState, TurnResult

logger = logging.getLogger(CHANNEL_LOGGER_NAME)


class NativeReplyChannel(CompletionPresentationChannel):
    """Translate already-observed native receipts outside the native consumer."""

    async def handle_ordinary_completion(self, outcome: OrdinaryEvent) -> None:
        if isinstance(outcome, TurnObservationUnavailableOutcome):
            await self._park_observation(outcome)
            return
        scheduled = outcome.origin if isinstance(outcome.origin, ScheduledOrigin) else None
        completion = _ScheduledCompletion()
        if scheduled is not None:
            self._scheduled_completions[scheduled.run_id] = completion
        try:
            await self._complete_task(self._codex_result(outcome))
            if (scheduled is not None and scheduled.auto_archive and outcome.error_detail is None
                    and outcome.status == "completed" and completion.delivery_state == "sent"):
                # The initial submission carries the immutable claim policy.
                # Later input deliberately does not cancel this archive intent.
                # Recovery never replays this completion or its side effects.
                await self._archive_scheduled_completion(outcome)
        finally:
            self._finish_ordinary_completion(outcome)

    def _process_reply_location(self, origin: object, root_id: str | None) -> ProcessLocation | None:
        if getattr(origin, "app_id", self._app_id) != self._app_id:
            return None
        scope, parent = self._scope(origin), _message_id(origin)
        root = getattr(origin, "root_id", None) or _inbound_root_message_id(origin)
        if not parent:
            return None
        if scope.app_id != self._app_id or (root_id is not None and (not scope.topic_id or root not in (None, root_id))):
            return None
        location = ProcessLocation(scope, parent, root or root_id)
        if not scope.topic_id or root_id or getattr(origin, "root_id", None) or isinstance(origin, _CardReplyTarget):
            # Only a rootless topic callback delegates one source read to the process writer.
            return location
        explicit_reply_root = root is not None and root != parent
        if not explicit_reply_root and getattr(origin, "reply", None) is not None:
            return None
        return location.with_source_root(getattr(origin, "raw", None), explicit_inbound_root=explicit_reply_root)


    def _codex_result(self, outcome: OrdinaryConclusion | SideTurnOutcome | GoalOutcome) -> TurnResult:
        event = CodexReplyEvents.result(outcome)
        origin = outcome.origin
        if isinstance(origin, ScheduledOrigin):
            def record_partials(confirmed: bool) -> None:
                if completion := self._scheduled_completions.get(origin.run_id):
                    completion.partial_answers_confirmed = confirmed
            return replace(event, receipt=DeliveryReceipt(
                lambda result: self._scheduled_reply_confirmed(origin, result), record_partials,
                lambda: self._record_scheduled_delivery(origin, "suppressed"),
                lambda message_id, result: self._record_scheduled_card_delivery(origin, message_id, result),
            ))
        return event

    def _finish_ordinary_completion(self, outcome: OrdinaryConclusion | TurnOutcome) -> None:
        scheduled = outcome.origin if isinstance(outcome.origin, ScheduledOrigin) else None
        self._reply_suppression.discard(outcome.thread_id, outcome.turn_id)
        if scheduled is not None:
            self._scheduled_completions.pop(scheduled.run_id, None)
            # Keep an undelivered terminal Run until the final I/O attempt
            # finishes; setting a receipt earlier would allow retention
            # pruning to delete it while the result card is still updating.
            try:
                if self._bindings.schedules.get_run(scheduled.run_id).delivery_state is None:
                    self._bindings.schedules.set_run(scheduled.run_id, delivery_state="unknown")
            except ScheduleNotFound:
                pass  # A completed receipt may have allowed normal pruning.

    async def _projection_failed(self, outcome: TurnOutcome | GoalOutcome) -> None:
        turn_id = (outcome.turn_id if isinstance(outcome, TurnOutcome)
                   else outcome.logical_turn_id or outcome.final_physical_turn_id or "goal")
        key = PresentationKey(outcome.binding_id, outcome.thread_id, turn_id)
        self._progress_cards.end_process_run(key, "error")
        try:
            try:
                await self._finish_task_reactions(outcome.origin, turn_id, constants.ERROR_REACTION)
            finally:
                await self._progress_cards.abandon(key=key)
        finally:
            if isinstance(outcome, TurnOutcome):
                self._finish_ordinary_completion(outcome)

    async def _archive_scheduled_completion(self, outcome: OrdinaryConclusion) -> None:
        origin = outcome.origin
        assert isinstance(origin, ScheduledOrigin)
        scope = FeishuScope(origin.app_id, origin.chat_id, ScopeKind.TOPIC,
                            origin.conversation.thread_id)
        try:
            await self._management.archive_exact_binding(target=ExactBindingTarget(
                scope.key, outcome.binding_id, expected_active_binding_id=None,
            ))
        except Exception as error:
            # Native lifecycle handling owns reconciliation/uncertainty. The
            # successful execution and delivery stay intact, without a retry.
            logger.warning("scheduled completion archive did not finish", extra={
                "run_id": origin.run_id, "binding_id": outcome.binding_id,
                "error_type": type(error).__name__,
            })

    async def handle_codex_completion(self, outcome: CodexFeatureEvent) -> None:
        try:
            await self._present_feature(outcome)
        finally:
            if isinstance(outcome, GoalOutcome):
                self._reply_suppression.discard(
                    outcome.thread_id, outcome.logical_turn_id or outcome.final_physical_turn_id or "goal",
                )
            elif isinstance(outcome, SideTurnOutcome):
                self._reply_suppression.discard(outcome.thread_id, outcome.turn_id)

    async def _present_feature(self, outcome: CodexFeatureEvent) -> None:
        if isinstance(outcome, ThreadActivityDiscardedOutcome):
            try:
                await self._progress_cards.abandon_thread(
                    binding_id=outcome.binding_id,
                    thread_id=outcome.thread_id,
                )
            finally:
                self._reply_suppression.discard_thread(outcome.thread_id)
            if outcome.turn_id is not None:
                await self._reactions.stop(outcome.turn_id)
            return
        if isinstance(outcome, SideLifecycleOutcome):
            await self._complete_side_lifecycle(outcome)
            return
        if isinstance(outcome, GoalOutcome):
            await self._complete_goal(outcome)
            return
        if isinstance(outcome, CompactionOutcome):
            if outcome.error is not None:
                detail = describe_error(outcome.error)
                await self._reply(
                    outcome.origin,
                    f"会话上下文压缩未完成：{detail}",
                )
            elif outcome.status == "completed":
                await self._reply(outcome.origin, "会话上下文压缩已完成。")
            else:
                await self._reply(
                    outcome.origin,
                    f"会话上下文压缩以未知状态结束：{outcome.status!r}。",
                )
            return
        await self._complete_task(self._codex_result(outcome))

    async def _park_observation(self, outcome: TurnObservationUnavailableOutcome) -> None:
        message = (
            f"本次 Codex Turn 状态无法确认：{describe_error(outcome.error)}。"
            "已停止后台读取，当前会话及上下文仍保留。"
            "尚未确认本轮是否结束，暂不启动新一轮；可发送消息触发状态检查，"
            "或发送 `/sessions` 重新检查、停止、归档或删除这个会话。"
            "状态仍无法确认时，新消息不会执行。"
        )
        key = PresentationKey(outcome.binding_id, outcome.thread_id, outcome.turn_id)
        self._progress_cards.tombstone_unknown_process(key)
        await self._present_run_state(RunState(ReplyTarget(key, outcome.origin), message))

    async def _complete_side_lifecycle(
        self,
        outcome: SideLifecycleOutcome,
    ) -> None:
        try:
            record = self._bindings.get_side_topic(outcome.side_id)
        except SideTopicNotFound:
            return
        if record.root_message_id is None:
            return
        if outcome.error is None:
            notice = {
                SideTopicState.CLOSED: "本临时对话已结束，历史消息仍可查看。",
                SideTopicState.EXPIRED: "本临时对话已因空闲超时或服务重启而过期；历史消息仍可查看。",
                SideTopicState.FAILED: "Side 创建失败，已释放可确认的 Codex 资源。",
            }.get(record.state)
            await self._update_side_card(record, notice=notice)
            return
        detail = describe_error(outcome.error, limit=300)
        await self._update_side_card(
            record,
            notice=f"Side 清理尚未确认，可再次结束重试：{detail}",
            notice_is_error=True,
        )

    async def _complete_goal(self, outcome: GoalOutcome) -> None:
        try:
            event = self._codex_result(outcome)
        except BaseException:
            await self._projection_failed(outcome)
            raise
        await self._complete_task(event)

    async def _present_running_goal(
        self,
        *,
        binding: ThreadBinding,
        submission: GoalSubmission,
        origin: object,
    ) -> bool:
        if submission.continued:
            return True

        key = PresentationKey(binding.id, submission.thread_id, submission.logical_turn_id)
        await self._reactions.start(key.turn_id, _message_id(origin), pulse_enabled=submission.task_feedback.reaction_pulse_enabled)
        event = CodexReplyEvents(self._runtime, self._ordinary).opened(
            key, origin, submission.task_feedback, goal=True,
            reply=lambda card: self._channel.reply(origin, card),
            reply_partial=lambda post: self._reply_to_origin(origin, post),
        )
        return await self._progress_cards.open(event)

    async def _goal_management_receipt(
        self, binding: ThreadBinding, scope: FeishuScope, origin: object, *,
        notice: str, source_id: str | None = None,
    ) -> None:
        """Display an accepted control best effort without changing its receipt."""
        try:
            card = await self._goal_management_card(binding, scope, notice=notice)
            if source_id is not None:
                if await self._safe_update_card(source_id, card):
                    return
            else:
                result = await self._reply_to_origin(origin, card)
                if result.success:
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Goal control accepted but management card unavailable")
        try:
            await self._reply(origin, f"{notice} 状态卡暂时不可用，可重新发送 /goal 查询。")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Goal control receipt display failed")
