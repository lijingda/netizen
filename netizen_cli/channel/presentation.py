"""Completion handling: Result, Files, Goal receipts and reminders."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path

from lark_channel import (
    OutboundCard, OutboundPost, SendResult,
)

from ..bindings import (
    BindingTaskFeedback,
    SideTopicNotFound,
    SideTopicState,
    ThreadBinding,
)
from ..cards import (
    TurnFileCardLimitError,
    turn_files_card,
    turn_progress_card,
)
from ..cards.reply import feishu_answer_text
from .completion_mentions import send_completion_mention
from .messages import _confirmed_reply_id
from .topics import CardReplyLocation, TopicPublishError
from .physical_delivery import BODY, INCOMPLETE, Item, PhysicalDelivery, receipt
from .reply_presenter import (
    _CardUpdateAttempt,
    _PartialAnswerDelivery,
)
from ..ordinary_sessions import ClaudeInputAnchor, ClaudeCliState, CliStateChanged, ConversationTarget, DegradedTurn, LogicalTerminal, OrdinaryConclusion, OrdinaryEvent, PhysicalAnswer, PhysicalStarted
from ..external_calls import external_call
from ..file_evidence import FileEvidence
from ..runtime.codex_replies import CodexFeatureEvent
from ..runtime.codex_contracts import GoalOutcome
from ..runtime.contracts import (
    CompactionOutcome,
    GoalSubmission,
    SideLifecycleOutcome,
    SideTurnActivitySnapshot,
    SideTurnOutcome,
    ThreadActivityDiscardedOutcome,
    TurnActivitySnapshot,
    TurnObservationUnavailableOutcome,
    TurnOutcome,
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
from ..result_images import prepare_result_images
from ..turn_files import (
    TurnDiffSummary,
    TurnFile,
    TurnFileError,
    has_file_evidence,
    resolve_evidenced_files,
    summarize_file_evidence,
)
from ..schedules.models import ScheduleNotFound

from ..session_views import ResultView
from .codex_reply_events import CodexReplyEvents
from .reply_events import (ActivityChanged, DeliveryReceipt, PresentationKey, ReplyTarget, RunState, TurnOpened, TurnResult)
from . import constants
from .app_values import (
    _ClaudeReplyTarget,
    _task_has_error,
    _ScheduledCompletion,
    _message_id,
    _outcome_completion_mention_user_id,
)
from .base import (
    CHANNEL_LOGGER_NAME,
    _normalized_thread_text,
)
from .replies import ChannelReplies


logger = logging.getLogger(CHANNEL_LOGGER_NAME)


@dataclass(frozen=True, slots=True)
class _TaskFiles:
    evidence: FileEvidence = FileEvidence()
    summary: TurnDiffSummary = TurnDiffSummary()


async def _best_effort_update(operation: Awaitable[object]) -> None:
    try:
        async with asyncio.timeout(5):
            await external_call(operation)
    except Exception:
        logger.warning("optional Claude presentation update failed")


class CompletionPresentationChannel(ChannelReplies):
    """Completion handling: Result, Files, Goal receipts and reminders."""

    async def handle_ordinary_completion(self, outcome: OrdinaryEvent) -> None:
        if isinstance(outcome, PhysicalStarted):
            if outcome.active():
                await self._start_logical_presentation(outcome.binding_id, outcome.thread_id, outcome.turn_id,
                    outcome.origin, outcome.feedback, active=outcome.active, retained=outcome.retained, on_confirmed=outcome.reply.confirmed)
            return
        if isinstance(outcome, CliStateChanged):
            if outcome.publishable():
                if outcome.reply.refresh:
                    await _best_effort_update(outcome.reply.refresh(outcome.current()))
                if outcome.background:
                    await _best_effort_update(self._reactions.background(outcome.turn_id))
            return
        if isinstance(outcome, LogicalTerminal):
            if not outcome.publishable():
                return
            if outcome.reply.refresh:
                await _best_effort_update(outcome.reply.refresh(outcome.state()))
            elif not outcome.reply.attempted:
                async def finish_initial_card() -> None:
                    attempt = await self._progress_cards.finish(key=PresentationKey(outcome.binding_id, outcome.thread_id, outcome.turn_id), activity=None, publishable=outcome.publishable, render=lambda snap: turn_progress_card(
                            snapshot=replace(snap, state_line=outcome.state().line), terminal_status=outcome.status,
                            collapsed=True, include_result=outcome.status != "completed"))
                    if attempt and attempt.confirmed:
                        outcome.reply.confirmed(attempt.message_id)
                await _best_effort_update(finish_initial_card())
            outcome.reply.refresh = None
            reaction = {"interrupted": constants.INTERRUPTED_REACTION, "failed": constants.ERROR_REACTION}.get(
                outcome.status, constants.DONE_REACTION)
            await _best_effort_update(self._reactions.freeze(outcome.turn_id))
            await _best_effort_update(self._safe_add_reaction(outcome.origin, reaction))
            if outcome.mention_user_id and outcome.publishable():
                confirmed_id = outcome.reply.message_id
                location = self._physical_card_location(outcome.binding_id, outcome.origin)
                source_id = _message_id(outcome.origin)
                if confirmed_id and not location.usable(confirmed_id):
                    confirmed_id = source_id if location.usable(source_id) else None
                await send_completion_mention(
                    self._channel, scope=self._scope(outcome.origin), card_message_id=confirmed_id,
                    user_id=outcome.mention_user_id, operation_id=outcome.turn_id, isolate_child_cancellation=True,
                    source_message_id=source_id if not outcome.reply.attempted and location.usable(source_id) else None,
                    location=location,
                )
            return
        if isinstance(outcome, DegradedTurn):
            location = self._physical_card_location(outcome.binding_id, outcome.origin)
            await self._physical_reply(outcome.origin, str(outcome.error), location)
            return
        if isinstance(outcome, TurnObservationUnavailableOutcome):
            await self._park_observation(outcome)
            return
        if isinstance(outcome, PhysicalAnswer):
            await self._handle_physical_answer(outcome)
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

    async def _handle_physical_answer(self, outcome: PhysicalAnswer) -> None:
        if not outcome.publishable():
            return
        if isinstance(outcome.origin, ConversationTarget):
            outcome = replace(outcome, origin=replace(outcome.origin, single_attempt=True))
        ledger = self._progress_cards.physical_delivery(outcome.binding_id, outcome.thread_id, outcome.turn_id)
        result = self._result_view(outcome, failure=lambda: outcome.error_detail or "Claude 本次执行失败。",
                                   interruption=lambda: "Claude 本次执行已停止。", empty_answer="")
        for part in outcome.partial_answers:
            if part.thread_id == outcome.thread_id and part.turn_id == outcome.turn_id and part.text.strip():
                ledger.partial(part)
        if result.content:
            ledger.expect(BODY)
        try:
            async with asyncio.timeout_at(outcome.delivery_deadline):
                content = result.content
                if result.status == "completed":
                    content = await self._prepare_task_result_images(result, ledger=ledger) or content
                if outcome.publishable():
                    await self._present_physical_answer(outcome, content, ledger)
        except Exception as error:
            logger.warning("Claude physical delivery ended without full confirmation (%s)", type(error).__name__)
        if ledger.settle() and outcome.publishable():
            target = outcome.origin
            if isinstance(target, ConversationTarget):
                target = replace(target, delivery_id=target.delivery_id + "/notice")
            try:
                async with asyncio.timeout(5):
                    location = self._physical_card_location(outcome.binding_id, target)
                    sent = await external_call(self._physical_reply(target, INCOMPLETE, location))
                if receipt(sent) != "confirmed":
                    logger.warning("Claude incomplete delivery notice was rejected")
            except Exception:
                logger.warning("Claude incomplete delivery notice was not confirmed")

    def _physical_card_location(self, binding_id: str, origin: object) -> CardReplyLocation:
        if isinstance(origin, _ClaudeReplyTarget):
            return origin.location
        if isinstance(origin, ConversationTarget):
            anchor = origin.source
            root = anchor.topic_root_id if isinstance(anchor, ClaudeInputAnchor) else None
            binding = (anchor.source.location.binding if isinstance(anchor, ClaudeInputAnchor) and isinstance(anchor.source, _ClaudeReplyTarget)
                       else self._progress_cards.binding_location(binding_id))
            return CardReplyLocation(origin.scope, None, root, binding)
        scope = self._scope(origin)
        parent = _message_id(origin)
        root = getattr(origin, "root_id", None)
        return CardReplyLocation(scope, parent, root or (None if scope.topic_id else parent),
                                 binding=self._progress_cards.binding_location(binding_id))

    async def _physical_reply(self, target: object, content: str | OutboundPost | OutboundCard,
                              location: CardReplyLocation) -> SendResult:
        if isinstance(target, ConversationTarget):
            # Validate each chunk before the SDK receipts are reduced to one logical send.
            return await self._reply_to_origin(target, content, location=location)
        sent = await self._reply_to_origin(target, content)
        return location.validate(sent, card=isinstance(content, OutboundCard))

    async def _present_physical_answer(self, outcome: PhysicalAnswer, content: str, ledger: PhysicalDelivery) -> None:
        reply = outcome.reply
        snapshot = outcome.activity
        location = self._physical_card_location(outcome.binding_id, outcome.origin)
        async def send_partial(post):
            if not outcome.publishable():
                raise RuntimeError("本轮投递已取消。")
            return await self._send_physical_partial(outcome.origin, post, reply.confirmed if reply else None, location)

        key = PresentationKey(outcome.binding_id, outcome.thread_id, outcome.turn_id)
        partials = await self._progress_cards.prepare_partial_answers(
            target=ReplyTarget(key, outcome.origin, None), reply=send_partial)
        await self._progress_cards.abandon(key=key)
        if not outcome.publishable():
            return
        previous_refresh = reply.refresh if reply else None
        previous_id = reply.message_id if reply else None
        if reply and ledger.items:
            reply.attempted = True
        status = outcome.status
        publishable = outcome.publishable
        binding_id, turn_id = outcome.binding_id, outcome.turn_id
        first_id, last_snapshot = partials.initial or (None, None)
        body_items = ((BODY, *ledger.prepared_images) if content else ())
        previous = ledger.partial_receipt(content)
        if previous in ("unknown", "rejected"):
            ledger.record(body_items, previous)
            content = ""
        location = partials.location or location
        in_card_mode = outcome.task_feedback.progress_card_enabled and not partials.text_only and (last_snapshot is None or first_id is not None)
        if in_card_mode and snapshot and (content or snapshot.partial_answers or first_id):
            state: ClaudeCliState | None = outcome.state()
            def render(current: ClaudeCliState | None) -> OutboundCard:
                return turn_progress_card(snapshot=replace(snapshot, state_line=current.line if current else None),
                                          final_response=content or None, partial_answers=snapshot.partial_answers,
                                          terminal_status=status, include_result=bool(content), collapsed=True)

            items = tuple(key for key, state in ledger.items.items() if state == "unattempted"
                          and (key[0] != "image" or key in ledger.prepared_images))
            attempt = None
            try:
                card = render(state)
            except Exception:
                card = None
            # Already attempted fragments stay on their original transport.
            # Confirmed fragments may remain visible across replacements of the same card.
            if card and all(ledger.items[item] not in ("unknown", "rejected") for item in ledger.partial_text):
                if first_id:
                    attempt = await self._progress_cards.write_physical_card(first_id, card, binding_id=binding_id,
                        turn_id=turn_id, location=location, publishable=publishable, begin=lambda: ledger.begin(items))
                elif publishable():
                    ledger.begin(items)
                    sent = await self._physical_reply(outcome.origin, card, location)
                    attempt = _CardUpdateAttempt(_confirmed_reply_id(sent) or "", sent)
                if attempt is not None:
                    ledger.record(items, receipt(attempt.result))
            if attempt and attempt.confirmed:
                if reply and not attempt.message_id and attempt.result:
                    target = _confirmed_reply_id(attempt.result, on_conflict=_message_id(outcome.origin))
                    if target:
                        reply.confirmed(target)
                if reply and attempt.message_id:
                    message_id = attempt.message_id
                    reply.confirmed(message_id)
                    async def refresh(current: ClaudeCliState | None) -> None:
                        nonlocal state
                        if current != state and publishable():
                            try:
                                card = render(current)
                            except Exception:
                                return  # The terminal body was already delivered.
                            if await self._progress_cards.update_message(message_id, card, binding_id=binding_id,
                                                                         operation_id=turn_id, publishable=publishable,
                                                                         location=location):
                                state = current
                            elif not location.can_write(message_id) and reply.message_id == message_id:
                                reply.message_id = None
                                reply.refresh = None
                    reply.refresh = refresh
                    if previous_refresh and previous_id != message_id:
                        await _best_effort_update(previous_refresh(None))
                    await _best_effort_update(refresh(outcome.state()))
                return
        # A fallback freezes the card at the last confirmed poll. Plain posts have
        # their own receipts, including attempts whose publication is uncertain.
        if first_id and not location.can_write(first_id):
            first_id = None
        displayed = {
            p for p in (last_snapshot.partial_answers if last_snapshot else ())
            if ledger.items.get(("partial", p.item_id)) == "confirmed"
        }
        if reply and first_id and displayed:
            reply.confirmed(first_id)
        try:
            for index, fragment in enumerate(outcome.partial_answers):
                item = ("partial", fragment.item_id)
                if (fragment.thread_id == outcome.thread_id and fragment.turn_id == outcome.turn_id
                        and ledger.items.get(item) == "unattempted"):
                    post = OutboundPost(markdown=f"**阶段性答案**\n\n{feishu_answer_text(fragment.text)}")
                    await self._send_physical_items(outcome, ledger, (item,), post, f"partial/{index}", location)
            if content and (matching := ledger.partial_receipt(content)) != "unattempted":
                ledger.record(body_items, matching)
            if content and ledger.items[BODY] == "unattempted":
                if count := outcome.owned_tasks():
                    content += f"\n\n后台任务 {count} 个运行中，可继续发送消息"
                await self._send_physical_items(outcome, ledger, body_items, content, "final", location)
        finally:
            if first_id and last_snapshot and not any(value == "unknown" for value in ledger.items.values()):
                async def freeze() -> None:
                    card = turn_progress_card(snapshot=replace(last_snapshot, state_line=None), terminal_status=status,
                                              include_result=False, collapsed=True)
                    await self._progress_cards.update_message(first_id, card, binding_id=binding_id,
                                                             operation_id=turn_id, publishable=publishable, location=location)
                await _best_effort_update(freeze())
            if reply and previous_refresh and reply.message_id != previous_id:
                reply.refresh = None
                await _best_effort_update(previous_refresh(None))

    async def _send_physical_items(self, outcome: PhysicalAnswer, ledger: PhysicalDelivery, items: tuple[Item, ...],
                                   content: str | OutboundPost, operation: str, location: CardReplyLocation) -> None:
        if not outcome.publishable() or (outcome.delivery_deadline is not None and
                                         asyncio.get_running_loop().time() >= outcome.delivery_deadline):
            return
        target = outcome.origin
        if isinstance(target, ConversationTarget):
            target = replace(target, delivery_id=target.delivery_id + "/" + operation)
            if isinstance(content, OutboundPost):
                content = content.markdown or ""
        ledger.begin(items)
        try:
            async with asyncio.timeout(self._progress_cards.operation_timeout):
                result = await external_call(self._physical_reply(target, content, location))
            ledger.record(items, receipt(result))
            if outcome.reply and (mid := _confirmed_reply_id(result, on_conflict=_message_id(target))):
                outcome.reply.confirmed(mid)
        except TopicPublishError as error:
            ledger.record(items, "unknown" if error.unknown else "rejected")
        except Exception:
            logger.warning("Claude content delivery was not confirmed")

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

    async def _terminal_card_delivery(self, outcome: TurnResult,
                                      deliver: Callable[[], Awaitable[_CardUpdateAttempt | None]]) -> _CardUpdateAttempt | None:
        try:
            return await deliver()
        except Exception:
            logger.exception("terminal progress-card delivery failed", extra={"turn_id": outcome.turn_id})
            await self._abandon_task_progress_card(outcome)
            return _CardUpdateAttempt("", None)

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
        try:
            try:
                await self._finish_task_reactions(outcome.origin, turn_id, constants.ERROR_REACTION)
            finally:
                await self._progress_cards.abandon(
                    key=PresentationKey(outcome.binding_id, outcome.thread_id, turn_id),
                )
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
        await self._present_run_state(RunState(ReplyTarget(key, outcome.origin, key), message))

    async def _present_run_state(self, event: RunState) -> None:
        key = event.target.key
        await self._reactions.stop(key.turn_id)
        try:
            await self._progress_cards.park_unavailable(binding_id=key.owner_id, thread_id=key.thread_id, turn_id=key.turn_id)
        except Exception:
            logger.exception("failed to park unavailable Turn progress card")
        await self._reply(event.target.origin, event.notice)


    @staticmethod
    def _result_view(
        outcome: OrdinaryConclusion | SideTurnOutcome, *, failure: Callable[[], str],
        interruption: Callable[[], str], empty_answer: str,
    ) -> ResultView:
        if _task_has_error(outcome) or outcome.status not in {"completed", "interrupted"}:
            status = "failed"
            content = failure()
        elif outcome.status == "interrupted":
            status = "interrupted"
            content = interruption()
        else:
            status = "completed"
            content = outcome.final_response or empty_answer
        return ResultView(
            session_id=outcome.binding_id if isinstance(outcome, OrdinaryConclusion) else outcome.side_id,
            thread_id=outcome.thread_id, turn_id=outcome.turn_id, origin=outcome.origin,
            status=status, content=content, cwd=outcome.cwd,
            mention_user_id=_outcome_completion_mention_user_id(outcome),
        )

    async def _finish_result_reactions(self, result: TurnResult) -> None:
        reaction = {"completed": constants.DONE_REACTION, "interrupted": constants.INTERRUPTED_REACTION,
                    "failed": constants.ERROR_REACTION}[result.status]
        await self._finish_task_reactions(result.origin, result.turn_id, reaction)

    async def _complete_task(self, outcome: TurnResult) -> None:
        file_evidence = outcome.file_evidence
        silent = (outcome.allow_suppression and outcome.status == "completed"
                  and not outcome.final_response
                  and self._reply_suppression.is_suppressed(
                      outcome.thread_id, outcome.turn_id, outcome.physical_turn_id,
                  ))
        partial_delivery = await self._progress_cards.prepare_partial_answers(
            target=outcome.target, reply=lambda post: self._reply_to_origin(outcome.origin, post),
            validate_reply=outcome.receipt.validate if outcome.receipt else None,
        )
        await self._finish_result_reactions(outcome)
        task_files = _TaskFiles()
        prepared_response = None
        if not silent and outcome.status == "completed":
            task_files, prepared_response = await self._prepare_task_result(outcome, file_evidence)
        if outcome.task_feedback.progress_card_enabled:
            # A missing card stays silent; failure of an existing card uses
            # the original result fallback below. Successful updates return.
            fallback_to_result = silent and self._progress_cards.has_card(
                owner_id=outcome.target.key.owner_id,
                thread_id=outcome.thread_id, turn_id=outcome.turn_id,
                side=outcome.target.key.side,
            )
            attempt = await self._terminal_card_delivery(outcome, lambda: self._complete_task_progress_card(
                outcome, task_files, prepared_response=prepared_response,
                suppress_result=silent))
            if attempt is not None and attempt.updated:
                if attempt.confirmed:
                    await self._remind_task_completion(outcome, attempt.message_id)
                return
            if outcome.receipt and attempt is not None:
                return
            if silent and (fallback_to_result or (attempt is not None and not attempt.message_id)):
                silent = False
                task_files, prepared_response = await self._prepare_task_result(outcome, file_evidence)
        try:
            if silent:
                async def finish_silent_reply(content: str) -> None:
                    # Only the existing partial-delivery failure notice is nonempty.
                    if content:
                        await self._complete_task_with_files(
                            outcome, self._prepare_task_files(outcome.cwd, file_evidence),
                            prepared_response=content,
                        )
                        return
                    if outcome.receipt:
                        outcome.receipt.suppressed()
                    await self._remind_task_completion(
                        outcome, None, source_message_id=_message_id(outcome.origin),
                    )

                await self._reply_task_answers(
                    outcome, "", partial_delivery=partial_delivery,
                    unconfirmed_content=(
                        "任务已完成，但阶段性答案投递未确认。" if not outcome.final_response else None
                    ),
                    final_reply=finish_silent_reply,
                )
            elif outcome.status != "completed":
                await self._reply_task_answers(
                    outcome, outcome.content, partial_delivery=partial_delivery,
                    allow_completion_mention=outcome.status != "interrupted",
                )
            elif not outcome.partial_answers:
                await self._complete_task_with_files(
                    outcome, task_files, prepared_response=prepared_response,
                )
            else:
                await self._reply_task_answers(
                    outcome,
                    prepared_response or outcome.content,
                    partial_delivery=partial_delivery,
                    unconfirmed_content=(
                        "任务已完成，但阶段性答案投递未确认。"
                        if outcome.partial_answers and not outcome.final_response else None
                    ),
                    final_reply=lambda content: self._complete_task_with_files(
                        outcome, task_files, prepared_response=content,
                    ),
                )
        finally:
            await self._abandon_task_progress_card(outcome)

    async def _finish_task_reactions(self, origin: object, turn_id: str, reaction: str) -> None:
        await self._reactions.freeze(turn_id)
        await self._safe_add_reaction(origin, reaction)
        await self._reactions.stop(turn_id)

    async def _prepare_task_result(
        self, outcome: TurnResult, file_evidence: Callable[[], FileEvidence],
    ) -> tuple[_TaskFiles, str | None]:
        task_files = self._prepare_task_files(outcome.cwd, file_evidence)
        response = await self._prepare_task_result_images(outcome)
        if not response and outcome.partial_answers:
            response = "任务已完成，答案见阶段性答案。"
        return task_files, response

    async def _prepare_task_result_images(
        self, outcome: TurnResult | ResultView, *, ledger: PhysicalDelivery | None = None,
    ) -> str | None:
        content = outcome.content if isinstance(outcome, ResultView) else outcome.final_response
        if not content:
            return content
        content = feishu_answer_text(content)
        if "![" not in content:
            return content
        if ledger:
            return await prepare_result_images(self._channel, content, cwd=outcome.cwd, on_image=ledger.image)
        return await self._prepare_result_images(content, cwd=outcome.cwd)

    async def _prepare_result_images(
        self, content: str, *, cwd: Path | None,
    ) -> str:
        try:
            return await prepare_result_images(self._channel, content, cwd=cwd)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("could not prepare inline result images; preserving answer")
            return content

    async def _complete_task_progress_card(
        self,
        outcome: TurnResult,
        task_files: _TaskFiles,
        *,
        prepared_response: str | None = None,
        suppress_result: bool = False,
    ) -> _CardUpdateAttempt | None:
        terminal_status = outcome.status
        final_response: str | None
        files: tuple[TurnFile, ...] = ()
        scope: FeishuScope | None = None
        file_provenance_id: str | None = None
        file_additions: int | None = None
        file_deletions: int | None = None
        final_response = outcome.content
        if outcome.status == "completed":
            final_response = None if suppress_result else prepared_response or outcome.content
            if not suppress_result and has_file_evidence(task_files.evidence, task_files.summary):
                try:
                    scope, files, file_provenance_id = (
                        self._task_completion_files(outcome, task_files)
                    )
                    file_additions = task_files.summary.additions
                    file_deletions = task_files.summary.deletions
                except Exception:
                    logger.exception(
                        "failed to prepare terminal progress-card files",
                        extra={
                            "owner_id": outcome.target.key.owner_id,
                            "turn_id": outcome.turn_id,
                        },
                    )
                    await self._abandon_task_progress_card(outcome)
                    return None

        def render(
            snapshot: TurnActivitySnapshot | SideTurnActivitySnapshot,
        ) -> OutboundCard:
            return turn_progress_card(
                snapshot=snapshot,
                partial_answers=outcome.partial_answers,
                final_response=final_response,
                files=files,
                terminal_status=terminal_status,
                include_result=not suppress_result,
                collapsed=True,
                scope=scope,
                binding_id=(file_provenance_id if files else None),
                turn_id=(outcome.physical_turn_id if files else None),
                additions=file_additions,
                deletions=file_deletions,
            )

        attempt = await self._progress_cards.finish(
            key=outcome.target.key, activity=outcome.activity, render=render,
        )
        if attempt is not None and outcome.receipt:
            if suppress_result and attempt.confirmed:
                outcome.receipt.suppressed()
            else:
                await outcome.receipt.card(attempt.message_id, attempt.result)
        return attempt

    async def _abandon_task_progress_card(self, outcome: TurnResult) -> None:
        await self._progress_cards.abandon(key=outcome.target.key)

    def _prepare_task_files(self, cwd: Path | None, file_evidence: Callable[[], FileEvidence]) -> _TaskFiles:
        evidence = FileEvidence()
        try:
            evidence = file_evidence()
            if cwd is None:
                raise TurnFileError("本轮工作目录未确认，不能使用项目默认目录解释文件。")
            summary = summarize_file_evidence(evidence, cwd)
        except Exception:
            logger.exception("failed to prepare completed patch statistics")
            summary = TurnDiffSummary()
        return _TaskFiles(evidence, summary)

    def _task_completion_files(
        self, outcome: TurnResult, task_files: _TaskFiles,
    ) -> tuple[FeishuScope, tuple[TurnFile, ...], str]:
        scope = self._scope(outcome.origin)
        key = outcome.target.key
        if key.side:
            record = self._bindings.get_side_topic(key.owner_id)
            if (record.parent_binding_id != outcome.file_binding_id or record.app_id != scope.app_id
                    or record.chat_id != scope.chat_id or record.topic_id != scope.topic_id):
                raise TurnFileError("本轮文件与 Side 完成消息的身份不一致。")
        else:
            binding = self._bindings.get(key.owner_id)
            if binding.scope_key != scope.key or binding.native_thread_id != key.thread_id:
                raise TurnFileError("本轮文件与完成消息的会话身份不一致。")
        if outcome.cwd is None:
            raise TurnFileError("本轮工作目录未确认，无法解析相对文件路径。")
        return scope, resolve_evidenced_files(task_files.evidence, outcome.cwd, task_files.summary), outcome.file_binding_id

    async def _complete_task_with_files(
        self,
        outcome: TurnResult,
        task_files: _TaskFiles,
        *,
        prepared_response: str | None = None,
    ) -> None:
        final_response = prepared_response or outcome.content
        if not has_file_evidence(task_files.evidence, task_files.summary):
            await self._reply_task_result(
                outcome, final_response,
            )
            return
        try:
            scope, files, file_provenance_id = self._task_completion_files(
                outcome,
                task_files,
            )
            card = turn_files_card(
                scope=scope,
                binding_id=file_provenance_id,
                turn_id=outcome.physical_turn_id,
                final_response=(
                    prepared_response or outcome.final_response or constants.TURN_FILES_WITHOUT_FINAL_RESPONSE
                ),
                files=files,
                additions=task_files.summary.additions,
                deletions=task_files.summary.deletions,
                completion_mention_user_id=outcome.mention_user_id,
            ) if files else None
        except asyncio.CancelledError:
            raise
        except TurnFileCardLimitError as error:
            logger.warning(
                "completed Turn file manifest exceeds card limit",
                extra={
                    "owner_id": outcome.target.key.owner_id,
                    "turn_id": outcome.turn_id,
                },
            )
            await self._reply_task_result(
                outcome,
                f"{final_response}\n\n⚠️ 本轮文件卡片未生成：{error}",
            )
            return
        except Exception:
            logger.exception(
                "failed to prepare completed Turn file card",
                extra={
                    "owner_id": outcome.target.key.owner_id,
                    "turn_id": outcome.turn_id,
                },
            )
            await self._reply_task_result(
                outcome, final_response,
            )
            return

        # Only preparation failures are known to precede any publication.
        # Do not catch a text fallback's own failure and send it a second time.
        if card is None:
            await self._reply_task_result(
                outcome, final_response,
            )
            return
        try:
            result = await self._send_completion_reply(outcome.origin, card)
            if result.success:
                return
            logger.warning("completed Turn file card was not confirmed", extra={"turn_id": outcome.turn_id})
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("failed to deliver completed Turn file card", extra={"turn_id": outcome.turn_id})
        # SendResult.failure can hide a timeout, retry, or partial publication.
        # Keep the scheduled receipt and never replace it with a fallback's ack.
        if not outcome.receipt:
            await self._reply_task_result(outcome, final_response, allow_completion_mention=False)

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

    async def _start_logical_presentation(
        self, binding_id: str, thread_id: str, turn_id: str, origin: object, feedback: BindingTaskFeedback, *,
        active: Callable[[], bool], retained: Callable[[], bool], on_confirmed: Callable[[str], None],
    ) -> bool:
        await self._reactions.start(turn_id, _message_id(origin), pulse_enabled=feedback.reaction_pulse_enabled,
                                    retained=retained)
        if not active():
            return False
        key = PresentationKey(binding_id, thread_id, turn_id)
        target = ReplyTarget(key, origin, key)
        location = self._physical_card_location(binding_id, origin)
        def observe() -> ActivityChanged | None:
            snapshot = self._ordinary.turn_activity(binding_id, thread_id=thread_id, turn_id=turn_id)
            if snapshot is None or not key.matches(snapshot):
                return None
            return ActivityChanged(target, snapshot, tuple(target.partial(p) for p in snapshot.partial_answers))
        return await self._progress_cards.open(TurnOpened(target, feedback, observe,
            lambda: not self._ordinary.reply_blocked_by_lifecycle(binding_id),
            lambda card: self._reply_to_origin(origin, card),
            lambda post: self._send_physical_partial(origin, post, on_confirmed, location),
            retained=retained, location=location))

    async def _send_physical_partial(self, origin: object, post: OutboundPost,
                                     on_confirmed: Callable[[str], None] | None, location: CardReplyLocation | None) -> SendResult:
        result = await self._physical_reply(origin, post, location) if location else await self._reply_to_origin(origin, post)
        if on_confirmed:
            target = _confirmed_reply_id(result, on_conflict=_message_id(origin))
            if target:
                on_confirmed(target)
        return result

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

    async def _remind_task_completion(
        self,
        outcome: TurnResult,
        card_message_id: str | None,
        *,
        scope: FeishuScope | None = None,
        source_message_id: str | None = None,
    ) -> None:
        if source_message_id is None and (
            card_message_id is None or not outcome.task_feedback.progress_card_enabled
        ):
            return
        user_id = outcome.mention_user_id
        if user_id is None:
            return
        if outcome.target.key.side:
            session_label = f"临时会话 {outcome.target.key.owner_id[:8]}"
        else:
            name = await self._management.completion_thread_name(outcome.thread_id)
            session_label = (
                _normalized_thread_text(name, max_chars=80)
                or f"会话 {outcome.target.key.owner_id[:8]}"
            )
        await send_completion_mention(
            self._channel, scope=scope or self._scope(outcome.origin),
            card_message_id=card_message_id, user_id=user_id,
            operation_id=outcome.turn_id,
            source_message_id=source_message_id,
            session_label=session_label,
        )

    async def _reply_task_answers(
        self,
        outcome: TurnResult,
        content: str,
        *,
        origin: object | None = None,
        allow_completion_mention: bool = True,
        partial_delivery: _PartialAnswerDelivery | None = None,
        timeout_seconds: float = constants.TERMINAL_ANSWER_DELIVERY_TIMEOUT_SECONDS,
        unconfirmed_content: str | None = None,
        final_reply: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        """Deliver ordered answers once, reserving time and completion effects for final."""

        target = outcome.origin if origin is None else origin
        if final_reply is None:
            async def final_reply(text: str) -> None:
                await self._reply_task_result(
                    outcome, text, origin=target,
                    allow_completion_mention=allow_completion_mention,
                )
        if not outcome.partial_answers:
            # Preserve the existing final-only path and its transport budget.
            await final_reply(content)
            return
        if timeout_seconds <= 0:
            logger.warning("terminal answer delivery budget exhausted before sending")
            return
        if partial_delivery is None:
            partial_delivery = _PartialAnswerDelivery(
                target=outcome.target,
                stopped=asyncio.Event(),
                reply_partial=lambda post: self._reply_to_origin(target, post),
                validate_reply=(
                    outcome.receipt.validate if outcome.receipt else None
                ),
            )
        try:
            async with asyncio.timeout(timeout_seconds):
                confirmed = await self._progress_cards.deliver_partial_answers(
                    partial_delivery, outcome.partials,
                    timeout_seconds=min(5.0, timeout_seconds / 2),
                )
                if not confirmed:
                    logger.warning(
                        "terminal partial-answer delivery was not fully confirmed",
                        extra={"thread_id": outcome.thread_id},
                    )
                    if unconfirmed_content is not None:
                        content = unconfirmed_content
                if outcome.receipt:
                    outcome.receipt.partials(confirmed)
                await final_reply(content)
        except TimeoutError:
            # A timed-out post may already have published SDK chunks. Do not
            # retry the whole item or create a background delivery lifecycle.
            logger.warning("terminal answer delivery budget exhausted", extra={"thread_id": outcome.thread_id})

    async def _reply_task_result(
        self,
        outcome: TurnResult,
        content: str,
        *,
        origin: object | None = None,
        allow_completion_mention: bool = True,
    ) -> None:
        mention = outcome.mention_user_id
        await self._reply(
            outcome.origin if origin is None else origin,
            content,
            completion_mention_user_id=mention if allow_completion_mention else None,
        )
