"""Ordinary prompt admission, automatic session creation and context receipts."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from lark_channel import (
    OutboundCard,
)

from .base import CHANNEL_LOGGER_NAME
from ..bindings import (
    ThreadBinding,
)
from .question_inputs import CardAnswerOrigin
from ..cards.defaults import defaults_card
from ..defaults import DefaultConfigurationError
from ..runtime.contracts import (
    ContextCursorCommit,
    RuntimeClosed,
    SideSessionClosing,
    SideSessionConflict,
    SideSessionNotFound,
    SkillReferenceError,
    SteerRace,
    Submission,
    SubmissionAdmission,
    SubmitDisposition,
    ThreadCompacting,
    ThreadArchived,
    ThreadStopping,
    ThreadOccupied,
    ThreadResumeNotFound,
)
from ..domain import (
    FeishuScope,
    MessageContextAnchor,
    MentionContextMode,
    PromptInput,
    ScheduledBindingOrigin,
    ScheduledOrigin,
    SessionAgentOrigin,
)
from ..worktrees.errors import WorktreeError
from ..model_settings import ModelCatalogError
from ..image_inputs import (
    ImageReference,
    normalized_message_type,
)
from ..prompt_projection import (
    MessageInputProjection,
    project_current_message,
)
from ..projects import ProjectError
from ..quoted_context import (
    quoted_message_id,
)
from ..message_history import (
    MessageHistoryError,
    MessageHistoryUnavailable,
)
from ..message_projection import (
    SupplementalContextStats,
)
from ..sdk_gap_adapter import SkillCatalogError

from . import constants
from .app_values import (
    _message_chat_type,
    _message_id,
)
from .presentation import CompletionPresentationChannel


logger = logging.getLogger(CHANNEL_LOGGER_NAME)


class PromptChannel(CompletionPresentationChannel):
    """Ordinary prompt admission, automatic session creation and context receipts."""

    async def _prompt(
        self,
        message: Any,
        prompt: PromptInput,
        *,
        current_images: tuple[ImageReference, ...] = (),
    ) -> None:
        binding = self._bindings.active_binding(prompt.scope.key)
        initial_context_anchor = None
        if binding is None:
            binding, initial_context_anchor = await self._auto_create_binding(message, prompt)
        if binding is None:
            await self._reply_no_current_binding(
                message, prompt.scope, task_not_executed=True,
            )
            return
        target_id = quoted_message_id(message)
        current = project_current_message(
            message,
            expected_message_id=prompt.source_id,
            expected_sender_id=prompt.sender_id,
            message_type=normalized_message_type(message),
            content_fidelity=(
                "full_multimodal" if current_images else "full_text"
            ),
            request_text=self._model_task_text(message, prompt.text),
        )
        await self._consume_prompt(
            binding=binding, scope=prompt.scope, message=message, current=current,
            owner_id=prompt.sender_id, skill_names=prompt.skill_names,
            target_id=target_id, current_images=current_images,
            initial_context_anchor=initial_context_anchor,
        )

    async def _auto_create_binding(
        self, message: Any, prompt: PromptInput,
    ) -> tuple[ThreadBinding | None, MessageContextAnchor | None]:
        service = self._management.defaults
        assert service is not None
        try:
            rule = await service.resolve(prompt.scope.chat_id, _message_chat_type(message))
            if rule is None:
                return self._bindings.active_binding(prompt.scope.key), None
            await service.validate(rule)
            settings = rule.session_settings
            anchor = None
            if settings.message_context_mode is MentionContextMode.CATCH_UP:
                self._require_catch_up_message_scope(message, prompt.scope)
                anchor = await self._resolve_context_anchor(prompt.scope, prompt.source_id)
            workspace = rule.workspace
            created = await self._management.create_current_binding(
                scope=prompt.scope, creator_id=prompt.sender_id, project_alias=rule.project,
                turn_settings=settings.turn_settings, task_feedback=settings.task_feedback,
                message_context_mode=settings.message_context_mode, context_anchor=anchor,
                only_if_empty=True,
                worktree_id=workspace.worktree_id,
                worktree_create=(
                    {"mode": "detached", "ref": workspace.ref}
                    if workspace.mode == "new" else None
                ),
            )
        except (DefaultConfigurationError, ProjectError, ModelCatalogError, MessageHistoryError, WorktreeError) as error:
            if isinstance(error, WorktreeError) and (
                error.details or error.code in {"outcome_unknown", "creation_interrupted"}
            ):
                # A retained resource or uncertain handoff must remain visible,
                # even when a manual /new has established another current.
                raise DefaultConfigurationError(
                    f"{error} 本条任务尚未执行，请核查后重新发送。",
                    code=error.code,
                ) from error
            # A concurrent manual creation wins even if the default could not be
            # prepared. Never present an existing Binding as missing, or replay
            # an input after a native operation with uncertain side effects.
            current = self._bindings.active_binding(prompt.scope.key)
            if current is not None:
                return current, None
            await self._reply(message, "默认会话配置不可用：" + str(error))
            return None, None
        if created.retained_worktree_cwd is not None:
            try:
                await self._reply(
                    message,
                    f"已有会话先完成创建，将沿用该会话；本次新建的 worktree "
                    f"{created.retained_worktree_cwd} 已保留，可在 Admin 管理或通过 /new 复用。",
                )
            except Exception:
                logger.warning("default creation retained-worktree notice failed", exc_info=True)
        return created.binding, anchor if created.created else None

    async def _defaults_card(
        self, scope: FeishuScope, *, notice: str | None = None,
        notice_is_error: bool = False,
        draft: Mapping[str, Any] | None = None,
    ) -> OutboundCard:
        service = self._management.defaults
        assert service is not None
        view = await service.manage({"mode": "view", "chat_id": scope.chat_id})
        catalog = None
        try:
            async with asyncio.timeout(5):
                catalog = await self._runtime.model_catalog()
        except Exception:
            # Existing defaults must remain inspectable/deletable even when
            # the native model catalog is unavailable.
            pass
        effective = view["exact"] if view["exact"] is not None else view["effective"]
        project_alias = draft["project"] if draft is not None else effective["project"] if effective else None
        workspace_options = None
        workspace_error = None
        worktrees = self._management.worktrees
        if worktrees is not None and project_alias is not None:
            try:
                workspace_options = await worktrees.options(
                    project_alias=project_alias,
                    expected_project_revision=draft["expected_project_revision"] if draft is not None else None,
                )
            except WorktreeError as error:
                workspace_error = str(error) + " 原 worktree 使用方式已保留，请重新加载或选择有效策略。"
        return defaults_card(
            scope, view, self._projects.list(), catalog,
            notice=notice, notice_is_error=notice_is_error,
            workspace_options=workspace_options, workspace_error=workspace_error,
            allow_workspace_selection=worktrees is not None, draft=draft,
        )

    async def _consume_prompt(
        self, *, binding: ThreadBinding, scope: FeishuScope, message: Any,
        current: MessageInputProjection, owner_id: str,
        skill_names: tuple[str, ...], target_id: str | None = None,
        current_images: tuple[ImageReference, ...] = (),
        scheduled_run_id: str | None = None,
        admission: SubmissionAdmission | None = None,
        initial_context_anchor: MessageContextAnchor | None = None,
        on_accepted: Callable[[], None] | None = None,
    ) -> None:
        """Share preparation, exact admission, acceptance and feedback for inputs."""
        if admission is None:
            admission = await self._runtime.capture_submission_admission(binding.id)
        cwd = self._binding_creation_cwd(binding)
        if (binding.settings_revision != admission.settings_revision
                or binding.context_revision != admission.context_revision
                or binding.feedback_revision != admission.feedback_revision):
            raise SteerRace("准备本条消息期间会话配置或上下文已变化，本条消息未执行。")
        context_commit = None
        if binding.message_context_mode is MentionContextMode.CATCH_UP:
            self._require_catch_up_message_scope(message, scope)
            if binding.context_anchor is None:
                raise MessageHistoryUnavailable(
                    "当前会话缺少群聊上下文边界，本条消息未执行；"
                    "请重新发送 /config 并重新选择 @ 时读取的消息范围。"
                )
            if initial_context_anchor is not None:
                if (
                    initial_context_anchor != binding.context_anchor
                    or initial_context_anchor.message_id != current.message_id
                    or binding.native_thread_id is not None
                ):
                    raise SteerRace("自动创建会话的首条消息边界已变化，请重新发送。")
                prepared = await self._input_preparer.prepare(
                    source_message=message, quoted_target_id=target_id, current=current,
                    current_images=current_images,
                )
                context_commit = ContextCursorCommit(
                    expected_context_revision=admission.context_revision,
                    anchor=initial_context_anchor,
                )
            else:
                prepared = await self._input_preparer.prepare_catch_up(
                    source_message=message,
                    scope=scope,
                    lower=binding.context_anchor,
                    upper_id=current.message_id,
                    quoted_target_id=target_id,
                    current=current,
                    current_images=current_images,
                    message_history=self._message_history,
                )
                assert prepared.context_anchor is not None
                assert prepared.context_stats is not None
                context_commit = ContextCursorCommit(
                    expected_context_revision=admission.context_revision,
                    anchor=prepared.context_anchor,
                )
        else:
            prepared = await self._input_preparer.prepare(
                source_message=message,
                quoted_target_id=target_id,
                current=current,
                current_images=current_images,
            )

        submit_kwargs: dict[str, Any] = dict(
            binding=binding,
            cwd=cwd,
            input=prepared.native_input,
            owner_id=owner_id,
            origin=message,
            skill_names=skill_names,
            context_commit=context_commit,
        )
        submit_kwargs["admission"] = admission
        if scheduled_run_id is not None:
            self._bindings.schedules.begin_binding_submission(scheduled_run_id, binding.id)
        attempted = False
        accepted = False
        try:
            if prepared.context_stats is not None:
                await self._send_context_receipt(message, prepared.context_stats)
            attempted = True
            submission = await self._runtime.submit(**submit_kwargs)
            accepted = True
            if on_accepted is not None:
                on_accepted()
        except BaseException as error:
            if not accepted:
                await self._discard_unsubmitted_attachments(prepared, error, attempted=attempted)
            raise
        finally:
            # The native RPC has consumed the input (or owns its in-flight
            # serialization). Do not retain a second large data-URL reference
            # while the reaction receipt below waits on Feishu I/O.
            submit_kwargs.pop("input", None)
            prepared = None
        if scheduled_run_id is not None:
            try:
                self._bindings.schedules.finish_binding_submission(
                    scheduled_run_id, submission.turn_id, submission.disposition.value,
                )
            except BaseException:
                # Recording the input receipt must never strand the accepted
                # native Turn's ordinary completion consumer.
                if submission.release_receipt_attempt is not None:
                    submission.release_receipt_attempt()
                raise
        if submission.disposition is SubmitDisposition.STEERED:
            if not await self._safe_add_reaction(message, constants.STEER_REACTION):
                await self._reply(message, "已接收调整。")
            return

        await self._present_ordinary_receipt(submission, message)

    async def _discard_unsubmitted_attachments(self, prepared, error: BaseException,
                                               *, attempted: bool = True) -> None:
        # Only exact pre-admission/verified-rejection errors prove no delivery.
        # Generic RPC failures, cancellation during submit and post-acceptance
        # cursor errors retain originals; manual deletion remains available.
        if attempted and not isinstance(error, (
            SteerRace, RuntimeClosed, ThreadStopping, ThreadCompacting,
            ThreadArchived, ThreadResumeNotFound, ThreadOccupied,
            SkillReferenceError, ModelCatalogError, SkillCatalogError,
            SideSessionNotFound, SideSessionClosing, SideSessionConflict,
        )):
            return
        try:
            await self._input_preparer.discard_attachments(prepared)
        except Exception:
            logger.warning("unsubmitted attachment cleanup failed", exc_info=True)

    async def _present_ordinary_receipt(self, submission: Submission, origin: Any) -> None:
        """Ordinary input and scheduled initial Turns share feedback and receipt ordering."""
        release = submission.release_receipt_attempt
        assert release is not None
        try:
            presenters: list[Awaitable[bool]] = [
                self._reactions.start(
                    submission.turn_id,
                    _message_id(origin),
                    pulse_enabled=(
                        submission.task_feedback.reaction_pulse_enabled
                    ),
                )
            ]
            delivery_options = {}
            if isinstance(origin, (ScheduledOrigin, ScheduledBindingOrigin, SessionAgentOrigin, CardAnswerOrigin)):
                delivery_options = {
                    "reply": lambda card: self._reply_to_origin(origin, card),
                }
                if isinstance(origin, ScheduledOrigin):
                    delivery_options["validate_reply"] = lambda result: self._scheduled_reply_confirmed(origin, result)
            presenters.append(
                self._progress_cards.start(
                    binding_id=submission.binding_id,
                    thread_id=submission.thread_id,
                    turn_id=submission.turn_id,
                    origin=origin,
                    progress_enabled=submission.task_feedback.progress_card_enabled,
                    reply_partial=lambda post: self._reply_to_origin(origin, post),
                    **delivery_options,
                )
            )
            if presenters:
                await asyncio.gather(*presenters)
        finally:
            release()

    async def _send_context_receipt(
        self,
        message: Any,
        stats: SupplementalContextStats,
    ) -> None:
        if (
            stats.selected_count == 0
            and stats.omitted_count == 0
            and not stats.is_truncated
        ):
            return
        parts = [f"本次将带入 {stats.selected_count} 条同一范围内的新增消息。"]
        if stats.omitted_count:
            parts.append(f"另有 {stats.omitted_count} 条不符合条件或不受支持，已省略。")
        if stats.is_truncated:
            parts.append("历史扫描、消息数量或文本上限已命中，仅保留较新的上下文。")
        try:
            async with asyncio.timeout(constants.CONTEXT_RECEIPT_TIMEOUT_SECONDS):
                await self._reply(message, " ".join(parts))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "failed to deliver visible catch-up context receipt",
                extra={
                    "message_id": _message_id(message),
                    "selected_count": stats.selected_count,
                    "omitted_count": stats.omitted_count,
                    "truncated": stats.is_truncated,
                },
            )
