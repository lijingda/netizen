"""Completion handling: Result, Files, Goal receipts and reminders."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path

from lark_channel import (
    OutboundCard,
)

from ..bindings import (
    SideTopicNotFound,
    SideTopicState,
    ThreadBinding,
)
from ..cards import (
    TurnFileCardLimitError,
    turn_files_card,
    turn_progress_card,
)
from .completion_mentions import send_completion_mention
from .reply_presenter import (
    _CardUpdateAttempt,
    _PartialAnswerDelivery,
)
from ..runtime.contracts import (
    CompactionOutcome,
    GoalFinalizationStatus,
    GoalOutcome,
    GoalSubmission,
    GoalStateUnknown,
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
    extract_turn_files,
    has_turn_file_references,
    turn_patch_summary,
)
from ..schedules.models import ScheduleNotFound

from . import constants
from .app_values import (
    _GoalReplyResult,
    _ScheduledCompletion,
    _message_id,
    _task_interruption_text,
    _goal_reply_activity,
    _task_failure_text,
    _outcome_completion_mention_user_id,
)
from .base import (
    CHANNEL_LOGGER_NAME,
    _normalized_thread_text,
)
from .replies import ChannelReplies


logger = logging.getLogger(CHANNEL_LOGGER_NAME)


class CompletionPresentationChannel(ChannelReplies):
    """Completion handling: Result, Files, Goal receipts and reminders."""

    async def handle_completion(
        self,
        outcome: (
            TurnOutcome
            | TurnObservationUnavailableOutcome
            | ThreadActivityDiscardedOutcome
            | CompactionOutcome
            | GoalOutcome
            | SideTurnOutcome
            | SideLifecycleOutcome
        ),
    ) -> None:
        origin = getattr(outcome, "origin", None)
        scheduled = isinstance(outcome, TurnOutcome) and isinstance(origin, ScheduledOrigin)
        completion = _ScheduledCompletion()
        if scheduled:
            self._scheduled_completions[origin.run_id] = completion
        try:
            await self._handle_completion(outcome)
            if (scheduled and origin.auto_archive and outcome.error is None
                    and outcome.status == "completed" and completion.delivery_state == "sent"):
                # The initial submission carries the immutable claim policy.
                # Later input deliberately does not cancel this archive intent.
                # Recovery never replays this completion or its side effects.
                await self._archive_scheduled_completion(outcome)
        finally:
            if isinstance(outcome, GoalOutcome):
                self._reply_suppression.discard(
                    outcome.thread_id, outcome.logical_turn_id or outcome.final_physical_turn_id or "goal",
                )
            elif isinstance(outcome, (TurnOutcome, SideTurnOutcome)):
                self._reply_suppression.discard(outcome.thread_id, outcome.turn_id)
            if scheduled:
                self._scheduled_completions.pop(origin.run_id, None)
                # Keep an undelivered terminal Run until the final I/O attempt
                # finishes; setting a receipt earlier would allow retention
                # pruning to delete it while the result card is still updating.
                try:
                    if self._bindings.schedules.get_run(origin.run_id).delivery_state is None:
                        self._bindings.schedules.set_run(origin.run_id, delivery_state="unknown")
                except ScheduleNotFound:
                    pass  # A completed receipt may have allowed normal pruning.

    async def _archive_scheduled_completion(self, outcome: TurnOutcome) -> None:
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

    async def _handle_completion(
        self,
        outcome: (
            TurnOutcome | TurnObservationUnavailableOutcome
            | ThreadActivityDiscardedOutcome | CompactionOutcome | GoalOutcome
            | SideTurnOutcome | SideLifecycleOutcome
        ),
    ) -> None:
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
        if isinstance(outcome, TurnObservationUnavailableOutcome):
            await self._reactions.stop(outcome.turn_id)
            try:
                await self._progress_cards.park_unavailable(
                    binding_id=outcome.binding_id,
                    thread_id=outcome.thread_id,
                    turn_id=outcome.turn_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "failed to park unavailable Turn progress card",
                    extra={
                        "binding_id": outcome.binding_id,
                        "turn_id": outcome.turn_id,
                    },
                )
            message = (
                f"本次 Codex Turn 状态无法确认：{describe_error(outcome.error)}。"
                "已停止后台读取，当前会话及上下文仍保留。"
                "尚未确认本轮是否结束，暂不启动新一轮；可发送消息触发状态检查，"
                "或发送 `/sessions` 重新检查、停止、归档或删除这个会话。"
                "状态仍无法确认时，新消息不会执行。"
            )
            await self._reply(outcome.origin, message)
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
        await self._complete_task(outcome)

    async def _complete_task(
        self,
        outcome: TurnOutcome | SideTurnOutcome,
        *,
        file_turn_id: str | None = None,
        logical_turn: bool = False,
        allow_suppression: bool = True,
    ) -> None:
        """Present one ordinary or SDK logical execution through the same reply path."""
        silent = (allow_suppression and outcome.error is None and outcome.status == "completed"
                  and not outcome.final_response
                  and self._reply_suppression.is_suppressed(
                      outcome.thread_id, outcome.turn_id, file_turn_id or outcome.turn_id,
                  ))
        partial_delivery = await self._progress_cards.prepare_partial_answers(
            owner_id=(outcome.binding_id if isinstance(outcome, TurnOutcome) else outcome.side_id),
            thread_id=outcome.thread_id,
            turn_id=outcome.turn_id,
            side=isinstance(outcome, SideTurnOutcome),
            logical_turn=logical_turn,
            reply=lambda post: self._reply_to_origin(outcome.origin, post),
            validate_reply=(
                (lambda result: self._scheduled_reply_confirmed(outcome.origin, result))
                if isinstance(outcome.origin, ScheduledOrigin) else None
            ),
        )
        if outcome.error is not None:
            terminal_reaction = constants.ERROR_REACTION
        elif outcome.status == "completed":
            terminal_reaction = constants.DONE_REACTION
        elif outcome.status == "interrupted":
            terminal_reaction = constants.INTERRUPTED_REACTION
        else:
            terminal_reaction = constants.ERROR_REACTION
        await self._reactions.freeze(outcome.turn_id)
        await self._safe_add_reaction(outcome.origin, terminal_reaction)
        await self._reactions.stop(outcome.turn_id)
        diff_summary = TurnDiffSummary()
        prepared_response = None
        if not silent and outcome.error is None and outcome.status == "completed":
            diff_summary, prepared_response = await self._prepare_task_result(outcome)
        if outcome.task_feedback.progress_card_enabled:
            # A missing card stays silent; failure of an existing card uses
            # the original result fallback below. Successful updates return.
            fallback_to_result = silent and self._progress_cards.has_card(
                owner_id=(outcome.binding_id if isinstance(outcome, TurnOutcome) else outcome.side_id),
                thread_id=outcome.thread_id, turn_id=outcome.turn_id,
                side=isinstance(outcome, SideTurnOutcome),
            )
            try:
                attempt = await self._complete_task_progress_card(
                    outcome,
                    diff_summary,
                    prepared_response=prepared_response, file_turn_id=file_turn_id,
                    suppress_result=silent,
                )
                if attempt is not None:
                    if attempt.updated:
                        if attempt.confirmed:
                            await self._remind_task_completion(outcome, attempt.message_id)
                        return
                    if isinstance(outcome.origin, ScheduledOrigin):
                        return
            except asyncio.CancelledError:
                raise
            except Exception:
                fallback_to_result = True
                logger.exception(
                    "terminal progress-card delivery failed",
                    extra={
                        "binding_id": getattr(outcome, "binding_id", None),
                        "side_id": getattr(outcome, "side_id", None),
                        "turn_id": outcome.turn_id,
                    },
                )
                await self._abandon_task_progress_card(outcome)
                # A failure after the terminal update/receipt may be uncertain.
                # Never publish a second scheduled result on that uncertainty.
                if isinstance(outcome.origin, ScheduledOrigin):
                    return
            if silent and fallback_to_result:
                silent = False
                diff_summary, prepared_response = await self._prepare_task_result(outcome)
        try:
            if silent:
                async def finish_silent_reply(content: str) -> None:
                    # Only the existing partial-delivery failure notice is nonempty.
                    if content:
                        await self._complete_task_with_files(
                            outcome, self._task_patch_summary(outcome),
                            prepared_response=content, file_turn_id=file_turn_id,
                        )
                        return
                    if isinstance(outcome.origin, ScheduledOrigin):
                        self._record_scheduled_delivery(outcome.origin, "suppressed")
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
            elif outcome.error is not None or outcome.status not in {"completed", "interrupted"}:
                await self._reply_task_answers(
                    outcome, _task_failure_text(outcome), partial_delivery=partial_delivery,
                )
            elif outcome.status == "interrupted":
                await self._reply_task_answers(
                    outcome, _task_interruption_text(outcome), partial_delivery=partial_delivery,
                    allow_completion_mention=False,
                )
            elif not outcome.partial_answers:
                await self._complete_task_with_files(
                    outcome, diff_summary, prepared_response=prepared_response, file_turn_id=file_turn_id,
                )
            else:
                await self._reply_task_answers(
                    outcome,
                    prepared_response or outcome.final_response or "任务已结束，未产生文本回复。",
                    partial_delivery=partial_delivery,
                    unconfirmed_content=(
                        "任务已完成，但阶段性答案投递未确认。"
                        if outcome.partial_answers and not outcome.final_response else None
                    ),
                    final_reply=lambda content: self._complete_task_with_files(
                        outcome, diff_summary, prepared_response=content, file_turn_id=file_turn_id,
                    ),
                )
        finally:
            await self._abandon_task_progress_card(outcome)

    async def _prepare_task_result(
        self, outcome: TurnOutcome | SideTurnOutcome,
    ) -> tuple[TurnDiffSummary, str | None]:
        diff = self._task_patch_summary(outcome)
        response = await self._prepare_task_result_images(outcome)
        if not response and outcome.partial_answers:
            response = "任务已完成，答案见阶段性答案。"
        return diff, response

    async def _prepare_task_result_images(
        self,
        outcome: TurnOutcome | SideTurnOutcome,
    ) -> str | None:
        content = outcome.final_response
        if not content or "![" not in content:
            return content
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
        outcome: TurnOutcome | SideTurnOutcome,
        diff_summary: TurnDiffSummary,
        *,
        prepared_response: str | None = None,
        file_turn_id: str | None = None,
        suppress_result: bool = False,
    ) -> _CardUpdateAttempt | None:
        terminal_status = "failed"
        final_response: str | None
        files: tuple[TurnFile, ...] = ()
        scope: FeishuScope | None = None
        file_provenance_id: str | None = None
        file_additions: int | None = None
        file_deletions: int | None = None
        if outcome.error is not None:
            final_response = _task_failure_text(outcome)
        elif outcome.status == "interrupted":
            terminal_status = "interrupted"
            final_response = _task_interruption_text(outcome)
        elif outcome.status != "completed":
            final_response = _task_failure_text(outcome)
        else:
            terminal_status = "completed"
            final_response = None if suppress_result else (
                prepared_response or outcome.final_response or "任务已结束，未产生文本回复。"
            )
            if not suppress_result and has_turn_file_references(
                tuple(getattr(outcome.result, "items", ())),
                diff_summary=diff_summary,
            ):
                try:
                    scope, files, file_provenance_id = (
                        self._task_completion_files(outcome, diff_summary)
                    )
                    file_additions = diff_summary.additions
                    file_deletions = diff_summary.deletions
                except Exception:
                    logger.exception(
                        "failed to prepare terminal progress-card files",
                        extra={
                            "binding_id": getattr(outcome, "binding_id", None),
                            "side_id": getattr(outcome, "side_id", None),
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
                turn_id=((file_turn_id or outcome.turn_id) if files else None),
                additions=file_additions,
                deletions=file_deletions,
            )

        if isinstance(outcome, TurnOutcome):
            attempt = await self._progress_cards.finish(
                binding_id=outcome.binding_id,
                thread_id=outcome.thread_id,
                turn_id=outcome.turn_id,
                activity=outcome.activity,
                render=render,
            )
            if attempt is not None and isinstance(outcome.origin, ScheduledOrigin):
                if suppress_result and attempt.confirmed:
                    self._record_scheduled_delivery(outcome.origin, "suppressed")
                else:
                    await self._record_scheduled_card_delivery(outcome.origin, attempt.message_id, attempt.result)
            return attempt
        return await self._progress_cards.finish_side(
            side_id=outcome.side_id,
            thread_id=outcome.thread_id,
            turn_id=outcome.turn_id,
            activity=outcome.activity,
            render=render,
        )

    async def _abandon_task_progress_card(
        self,
        outcome: TurnOutcome | SideTurnOutcome,
    ) -> None:
        if isinstance(outcome, TurnOutcome):
            await self._progress_cards.abandon(
                binding_id=outcome.binding_id,
                thread_id=outcome.thread_id,
                turn_id=outcome.turn_id,
            )
            return
        await self._progress_cards.abandon_side(
            side_id=outcome.side_id,
            thread_id=outcome.thread_id,
            turn_id=outcome.turn_id,
        )

    def _task_patch_summary(
        self, outcome: TurnOutcome | SideTurnOutcome,
    ) -> TurnDiffSummary:
        try:
            cwd = outcome.cwd
            if cwd is None:
                raise TurnFileError("本轮工作目录未确认，不能使用项目默认目录解释文件。")
            return turn_patch_summary(
                tuple(getattr(outcome.result, "items", ())),
                cwd,
                children=outcome.patch_children,
                turn_diff=outcome.turn_diff if isinstance(outcome, TurnOutcome) else None,
            )
        except Exception:
            logger.exception("failed to prepare completed patch statistics")
            return TurnDiffSummary()

    def _completion_files(
        self,
        outcome: TurnOutcome,
        diff_summary: TurnDiffSummary,
    ) -> tuple[FeishuScope, tuple[TurnFile, ...]]:
        binding = self._bindings.get(outcome.binding_id)
        scope = self._scope(outcome.origin)
        if (
            binding.scope_key != scope.key
            or binding.native_thread_id != outcome.thread_id
        ):
            raise TurnFileError("本轮文件与完成消息的会话身份不一致。")
        if outcome.cwd is None:
            raise TurnFileError("本轮工作目录未确认，无法解析相对文件路径。")
        return scope, extract_turn_files(
            tuple(getattr(outcome.result, "items", ())),
            outcome.cwd,
            diff_summary=diff_summary,
        )

    def _side_completion_files(
        self,
        outcome: SideTurnOutcome,
        diff_summary: TurnDiffSummary,
    ) -> tuple[FeishuScope, tuple[TurnFile, ...]]:
        record = self._bindings.get_side_topic(outcome.side_id)
        scope = self._scope(outcome.origin)
        if (
            record.parent_binding_id != outcome.parent_binding_id
            or record.app_id != scope.app_id
            or record.chat_id != scope.chat_id
            or record.topic_id != scope.topic_id
        ):
            raise TurnFileError("本轮文件与 Side 完成消息的身份不一致。")
        return scope, extract_turn_files(
            tuple(getattr(outcome.result, "items", ())),
            outcome.cwd,
            diff_summary=diff_summary,
        )

    def _task_completion_files(
        self,
        outcome: TurnOutcome | SideTurnOutcome,
        diff_summary: TurnDiffSummary,
    ) -> tuple[FeishuScope, tuple[TurnFile, ...], str]:
        if isinstance(outcome, TurnOutcome):
            scope, files = self._completion_files(outcome, diff_summary)
            return scope, files, outcome.binding_id
        scope, files = self._side_completion_files(outcome, diff_summary)
        # v4 callbacks are self-contained. The captured Parent Binding is
        # provenance and deterministic-action identity only; paging and send
        # never read its current state or reinterpret the Side as a Binding.
        return scope, files, outcome.parent_binding_id

    async def _complete_task_with_files(
        self,
        outcome: TurnOutcome | SideTurnOutcome,
        diff_summary: TurnDiffSummary,
        *,
        prepared_response: str | None = None,
        file_turn_id: str | None = None,
    ) -> None:
        final_response = prepared_response or outcome.final_response or "任务已结束，未产生文本回复。"
        items = tuple(getattr(outcome.result, "items", ()))
        if not has_turn_file_references(
            items,
            diff_summary=diff_summary,
        ):
            await self._reply_task_result(
                outcome, final_response,
            )
            return
        try:
            scope, files, file_provenance_id = self._task_completion_files(
                outcome,
                diff_summary,
            )
            card = turn_files_card(
                scope=scope,
                binding_id=file_provenance_id,
                turn_id=file_turn_id or outcome.turn_id,
                final_response=(
                    prepared_response or outcome.final_response or constants.TURN_FILES_WITHOUT_FINAL_RESPONSE
                ),
                files=files,
                additions=diff_summary.additions,
                deletions=diff_summary.deletions,
                completion_mention_user_id=_outcome_completion_mention_user_id(outcome),
            ) if files else None
        except asyncio.CancelledError:
            raise
        except TurnFileCardLimitError as error:
            logger.warning(
                "completed Turn file manifest exceeds card limit",
                extra={
                    "binding_id": getattr(outcome, "binding_id", None),
                    "side_id": getattr(outcome, "side_id", None),
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
                    "binding_id": getattr(outcome, "binding_id", None),
                    "side_id": getattr(outcome, "side_id", None),
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
        if not isinstance(outcome.origin, ScheduledOrigin):
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
                SideTopicState.FAILED: "Side 创建失败，已释放可确认的原生资源。",
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
        # The SDK logical stream is one reply lifecycle. Its identifier is only
        # the presenter key; Files always retain the verified final physical ID.
        origin = outcome.origin
        terminal = (
            outcome.final_physical_turn_id is not None
            and outcome.final_turn_status in {"completed", "interrupted", "failed"}
        )
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
                detail = describe_error(outcome.finalization_error) if outcome.finalization_error else "自动结束结果未确认"
                notices.append(f"Goal 已完成，但自动结束结果未确认：{detail}")
            if notices:
                response = "\n\n".join(filter(None, [response, *notices]))
        task = TurnOutcome(
            binding_id=outcome.binding_id, thread_id=outcome.thread_id,
            turn_id=outcome.logical_turn_id or outcome.final_physical_turn_id or "goal",
            owner_id=outcome.owner_id, origin=origin,
            cwd=outcome.cwd,
            result=_GoalReplyResult(
                status=outcome.final_turn_status if terminal else None,
                final_response=response,
                items=outcome.final_items if terminal else (),
            ),
            error=error,
            background_cleanup_requested=outcome.background_cleanup_requested,
            task_feedback=(
                outcome.task_feedback if terminal and outcome.error is None
                else replace(outcome.task_feedback, completion_mention_enabled=False)
            ),
            feedback_revision=outcome.feedback_revision,
            activity=None if outcome.activity is None else _goal_reply_activity(outcome.activity),
            turn_diff=outcome.turn_diff, patch_children=outcome.patch_children,
            partial_answers=outcome.partial_answers,
        )
        await self._complete_task(
            task, file_turn_id=outcome.final_physical_turn_id, logical_turn=True,
            allow_suppression=(outcome.error is None
                               and outcome.finalization is not GoalFinalizationStatus.UNKNOWN
                               and outcome.finalization_error is None),
        )

    async def _present_running_goal(
        self,
        *,
        binding: ThreadBinding,
        submission: GoalSubmission,
        origin: object,
    ) -> bool:
        if submission.continued:
            return True

        def snapshot_reader() -> TurnActivitySnapshot | None:
            snapshot = self._runtime.goal_activity(
                binding.id, thread_id=submission.thread_id,
                logical_turn_id=submission.logical_turn_id,
            )
            return None if snapshot is None else _goal_reply_activity(snapshot)

        await self._reactions.start(
            submission.logical_turn_id, _message_id(origin),
            pulse_enabled=submission.task_feedback.reaction_pulse_enabled,
        )
        return await self._progress_cards.start(
            binding_id=binding.id, thread_id=submission.thread_id,
            turn_id=submission.logical_turn_id, origin=origin,
            progress_enabled=submission.task_feedback.progress_card_enabled,
            reply_partial=lambda post: self._reply_to_origin(origin, post),
            snapshot_reader=snapshot_reader, logical_turn=True,
        )

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
        outcome: TurnOutcome | SideTurnOutcome,
        card_message_id: str | None,
        *,
        scope: FeishuScope | None = None,
        source_message_id: str | None = None,
    ) -> None:
        if source_message_id is None and (
            card_message_id is None or not outcome.task_feedback.progress_card_enabled
        ):
            return
        user_id = _outcome_completion_mention_user_id(outcome)
        if user_id is None:
            return
        if isinstance(outcome, SideTurnOutcome):
            session_label = f"临时会话 {outcome.side_id[:8]}"
        else:
            name = await self._management.completion_thread_name(outcome.thread_id)
            session_label = (
                _normalized_thread_text(name, max_chars=80)
                or f"会话 {outcome.binding_id[:8]}"
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
        outcome: TurnOutcome | SideTurnOutcome,
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
                thread_id=outcome.thread_id,
                turn_id=outcome.turn_id,
                stopped=asyncio.Event(),
                reply_partial=lambda post: self._reply_to_origin(target, post),
                validate_reply=(
                    (lambda result: self._scheduled_reply_confirmed(target, result))
                    if isinstance(target, ScheduledOrigin) else None
                ),
            )
        try:
            async with asyncio.timeout(timeout_seconds):
                confirmed = await self._progress_cards.deliver_partial_answers(
                    partial_delivery, outcome.partial_answers,
                    timeout_seconds=min(5.0, timeout_seconds / 2),
                )
                if not confirmed:
                    logger.warning(
                        "terminal partial-answer delivery was not fully confirmed",
                        extra={"thread_id": outcome.thread_id},
                    )
                    if unconfirmed_content is not None:
                        content = unconfirmed_content
                if isinstance(target, ScheduledOrigin) and target.run_id in self._scheduled_completions:
                    self._scheduled_completions[target.run_id].partial_answers_confirmed = confirmed
                await final_reply(content)
        except TimeoutError:
            # A timed-out post may already have published SDK chunks. Do not
            # retry the whole item or create a background delivery lifecycle.
            logger.warning("terminal answer delivery budget exhausted", extra={"thread_id": outcome.thread_id})

    async def _reply_task_result(
        self,
        outcome: TurnOutcome | SideTurnOutcome,
        content: str,
        *,
        origin: object | None = None,
        allow_completion_mention: bool = True,
    ) -> None:
        await self._reply(
            outcome.origin if origin is None else origin,
            content,
            completion_mention_user_id=(
                _outcome_completion_mention_user_id(outcome) if allow_completion_mention else None
            ),
        )
