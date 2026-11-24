"""Delivery of typed presentation events."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path

from lark_channel import (
    OutboundCard,
    OutboundPost,
    SendResult,
)

from ..cards import (
    TurnFileCardLimitError,
    turn_files_card,
    turn_progress_card,
)
from ..cards.reply import feishu_answer_text, turn_progress_projection
from ..domain import (
    FeishuScope,
)
from ..external_calls import external_call
from ..file_evidence import FileEvidence
from ..result_images import prepare_result_images
from ..runtime.contracts import (
    SideTurnActivitySnapshot,
    TurnActivitySnapshot,
)
from ..turn_files import (
    TurnDiffSummary,
    TurnFile,
    TurnFileError,
    has_file_evidence,
    resolve_evidenced_files,
    summarize_file_evidence,
)
from . import constants
from .app_values import (
    _message_id,
)
from .base import (
    CHANNEL_LOGGER_NAME,
    _normalized_thread_text,
)
from .completion_mentions import send_completion_mention
from .messages import _confirmed_reply_id
from .physical_delivery import BODY, INCOMPLETE, Item, PhysicalDelivery, record_sent_answer
from .replies import ChannelReplies
from .reply_events import ItemDelivery, RunEnded, RunState, TurnResult
from .reply_presenter import (
    _CardUpdateAttempt,
    _PartialAnswerDelivery,
    render_run_card,
)
from .topics import CardReplyLocation, receipt

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
        logger.warning("optional reply update failed")


class CompletionPresentationChannel(ChannelReplies):
    """Delivery of typed presentation events."""

    async def _deliver_with_receipts(self, outcome: TurnResult, policy: ItemDelivery) -> None:
        display = policy.display
        key = outcome.target.key
        if not display.publishable():
            return
        ledger = self._progress_cards.physical_delivery(key)
        for part in outcome.partial_answers:
            ledger.partial(part)
        if outcome.content:
            ledger.expect(BODY)
        try:
            async with asyncio.timeout_at(policy.deadline):
                content = outcome.content
                if outcome.status == "completed":
                    content = await self._prepare_task_result_images(outcome, ledger=ledger) or content
                if display.publishable():
                    await self._deliver_receipted_items(outcome, policy, content, ledger)
        except Exception as error:
            logger.warning("item delivery ended without full confirmation (%s)", type(error).__name__)
        if ledger.settle() and display.publishable():
            try:
                async with asyncio.timeout(5):
                    sent = await external_call(policy.send(INCOMPLETE, "notice", policy.location))
                if receipt(sent) != "confirmed":
                    logger.warning("incomplete delivery notice was rejected")
            except Exception:
                logger.warning("incomplete delivery notice was not confirmed")

    async def _deliver_receipted_items(self, outcome: TurnResult, policy: ItemDelivery, content: str, ledger: PhysicalDelivery) -> None:
        display = policy.display
        reply = display.reply
        snapshot = outcome.activity
        key = outcome.target.key
        partials = await self._terminal_partials(outcome)
        await self._progress_cards.abandon(key=key)
        if not display.publishable():
            return
        status = outcome.status
        publishable = display.publishable
        first_id = partials.message_id
        body_items = ((BODY, *ledger.prepared_images) if content else ())
        matching_items = tuple(item for item, text in ledger.partial_text.items() if content and text == content)
        location = partials.location or policy.location
        in_card_mode = outcome.task_feedback.progress_card_enabled and not partials.text_only
        has_answer = bool(content or outcome.partial_answers)
        if in_card_mode and snapshot and (has_answer or first_id):
            card_partials = {("partial", part.item_id) for part in outcome.partial_answers}
            # A Result can represent a running partial that the terminal snapshot coalesced.
            shown_partials = tuple(item for item in ledger.partial_text if item in card_partials or item in matching_items)
            items = (*body_items, *shown_partials)
            try:
                projection = turn_progress_projection(snapshot=snapshot, partial_answers=outcome.partial_answers,
                    final_response=content or None, include_result=bool(content), terminal_status=status,
                    run_continues=outcome.run_continues, collapsed=True)
                card = render_run_card(projection, display.current())
            except Exception:
                card = None
            if card and first_id and location.can_write(first_id):
                if await self._progress_cards.publish_card(display, first_id, key, location, projection, delivery=ledger, items=items):
                    ledger.record(items, "confirmed")
                    return
                if not any(ledger.items.get(item) == "rejected" and ledger.can_post(item) for item in items):
                    return
            if (card and not first_id and publishable()
                    and all(ledger.items[item] not in ("unknown", "rejected") for item in ledger.partial_text)):
                state, sent = await ledger.send(items, reply, policy.send(card, None, location))
                if state == "confirmed" and sent is not None:
                    record_sent_answer(reply, location, key.turn_id, items, sent)
                    if reply and (mid := _confirmed_reply_id(sent)):
                        await self._progress_cards.publish_card(display, mid, key, location, projection, delivery=ledger, items=items, confirmed=card)
                    return
                if state == "unknown":
                    return
        # Freeze any partials the running card may already show. The ledger
        # independently decides which items can safely take a new text post.
        if first_id and not location.can_write(first_id):
            first_id = None
        last_snapshot = partials.freeze_snapshot
        try:
            for index, fragment in enumerate(outcome.partial_answers):
                item = ("partial", fragment.item_id)
                if (fragment.thread_id == outcome.thread_id and fragment.turn_id == outcome.turn_id
                        and ledger.can_post(item)):
                    post = OutboundPost(markdown=f"**阶段性答案**\n\n{feishu_answer_text(fragment.text)}")
                    await self._send_receipted_items(outcome, policy, ledger, (item,), post, f"partial/{index}", location)
            matching = ledger.partial_receipt(matching_items)
            if matching in ("confirmed", "unknown") or (matching == "rejected" and ledger.posted.intersection(matching_items)):
                ledger.record(body_items, matching)
            elif content and ledger.can_post(BODY):
                content += policy.plain_suffix()
                await self._send_receipted_items(outcome, policy, ledger, (*body_items, *matching_items), content, "final", location)
        finally:
            if first_id and last_snapshot:
                frozen = turn_progress_projection(snapshot=last_snapshot, terminal_status=status,
                    run_continues=outcome.run_continues, include_result=False, collapsed=True)
                shown = tuple(("partial", part.item_id) for part in last_snapshot.partial_answers)
                items = (*shown, *body_items) if any(item in matching_items for item in shown) else shown
                await self._progress_cards.publish_card(display, first_id, key, location, frozen, tracks_run=False,
                    delivery=ledger, items=items)
            else:
                await self._progress_cards.reconcile(display)

    async def _send_receipted_items(self, outcome: TurnResult, policy: ItemDelivery, ledger: PhysicalDelivery, items: tuple[Item, ...],
                                   content: str | OutboundPost, operation: str, location: CardReplyLocation) -> None:
        if not policy.display.publishable() or (policy.deadline is not None and
                                         asyncio.get_running_loop().time() >= policy.deadline):
            return
        _, result = await ledger.send(items, policy.display.reply, external_call(policy.send(content, operation, location)),
                                      post=True, timeout=self._progress_cards.operation_timeout)
        if result is not None:
            record_sent_answer(policy.display.reply, location, outcome.turn_id, items, result)

    async def _terminal_card_delivery(self, outcome: TurnResult,
                                      deliver: Callable[[], Awaitable[_CardUpdateAttempt | None]]) -> _CardUpdateAttempt | None:
        try:
            return await deliver()
        except Exception:
            logger.exception("terminal progress-card delivery failed", extra={"turn_id": outcome.turn_id})
            await self._abandon_task_progress_card(outcome)
            return _CardUpdateAttempt("", None)

    async def _present_run_state(self, event: RunState) -> None:
        if display := event.display:
            if display.publishable() and display.reply:
                await self._progress_cards.reconcile(display)
                if event.background and event.target.run:
                    await _best_effort_update(self._reactions.background(event.target.run.turn_id))
            return
        if event.notice is None:
            return
        if event.send_notice:
            await event.send_notice(event.notice)
            return
        key = event.target.key
        await self._reactions.stop(key.turn_id)
        try:
            await self._progress_cards.park_unavailable(binding_id=key.owner_id, thread_id=key.thread_id, turn_id=key.turn_id)
        except Exception:
            logger.exception("failed to park unavailable Turn progress card")
        await self._reply(event.target.origin, event.notice)

    async def _present_run_end(self, event: RunEnded) -> None:
        run = event.target.run
        if run is None:
            return
        reaction = {"completed": constants.DONE_REACTION, "interrupted": constants.INTERRUPTED_REACTION,
                    "failed": constants.ERROR_REACTION}[event.status]
        display = event.display
        if display is None:
            await self._finish_task_reactions(event.target.origin, run.turn_id, reaction)
            return
        reply = display.reply
        if reply is None:
            return
        try:
            await _best_effort_update(self._progress_cards.finish_run(display, event.status))
            if not display.publishable():
                return
            await _best_effort_update(self._reactions.freeze(run.turn_id))
            await _best_effort_update(self._safe_add_reaction(event.target.origin, reaction))
            if event.mention_user_id and display.publishable():
                location = event.location
                assert location is not None
                source_id = _message_id(event.target.origin)
                anchor = reply.mention_target(source_id if location.usable(source_id) else None)
                await send_completion_mention(
                    self._channel, scope=self._scope(event.target.origin), card_message_id=anchor,
                    user_id=event.mention_user_id, operation_id=run.turn_id, isolate_child_cancellation=True, location=location,
                )
        finally:
            await self._progress_cards.end_process_run(run, "error" if event.status == "failed" else "done")
            self._progress_cards.retire_run(reply)
            reply.retire()

    async def _terminal_partials(self, outcome: TurnResult) -> _PartialAnswerDelivery:
        def reply(post: OutboundPost) -> Awaitable[SendResult]:
            if outcome.items:
                return outcome.items.send(post, None, outcome.items.location)
            return self._reply_to_origin(outcome.origin, post)

        return await self._progress_cards.prepare_partial_answers(
            target=outcome.target, reply=reply,
            validate_reply=outcome.receipt.validate if outcome.receipt else None,
        )

    async def _complete_task(self, outcome: TurnResult) -> None:
        await self._progress_cards.process_result(outcome)
        try:
            await self._deliver_task_result(outcome)
        finally:
            await self._progress_cards.release_process(outcome.target.key)

    async def _deliver_task_result(self, outcome: TurnResult) -> None:
        if outcome.items:
            await self._deliver_with_receipts(outcome, outcome.items)
            return
        file_evidence = outcome.file_evidence
        silent = (outcome.allow_suppression and outcome.status == "completed"
                  and not outcome.final_response
                  and self._reply_suppression.is_suppressed(
                      outcome.thread_id, outcome.turn_id, outcome.physical_turn_id,
                  ))
        partial_delivery = await self._terminal_partials(outcome)
        if outcome.end:
            await self._present_run_end(outcome.end)
        task_files = _TaskFiles()
        prepared_response = None
        if not silent and outcome.status == "completed":
            task_files, prepared_response = await self._prepare_task_result(outcome, file_evidence)
        if outcome.task_feedback.progress_card_enabled:
            # A missing card stays silent; failure of an existing card uses
            # the original result fallback below. Successful updates return.
            fallback_to_result = silent and self._progress_cards.has_card(outcome.target.key)
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
        self, outcome: TurnResult, *, ledger: PhysicalDelivery | None = None,
    ) -> str | None:
        content = outcome.final_response
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
            if isinstance(snapshot, TurnActivitySnapshot) and snapshot.state_line is not None:
                snapshot = replace(snapshot, state_line=None)
            return turn_progress_card(
                snapshot=snapshot, partial_answers=outcome.partial_answers, final_response=final_response,
                terminal_status=outcome.status, run_continues=outcome.run_continues, collapsed=True,
                files=files,
                include_result=not suppress_result,
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
                run_continues=outcome.run_continues,
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
                    partial_delivery, outcome,
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
