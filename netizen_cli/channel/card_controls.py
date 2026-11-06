"""Card control actions dispatched by name."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import asyncio
import logging
from dataclasses import replace
from typing import Any


from ..bindings import (
    BindingContextRevisionConflict,
    BindingFeedbackRevisionConflict,
    BindingNotFound,
    BindingSettingsRevisionConflict,
    BindingTaskFeedback,
    SideTopicState,
    ThreadBinding,
)
from ..cards import (
    CardActionError,
    archived_sessions_delete_binding_card,
    binding_configured_card,
    binding_created_card,
    binding_lifecycle_result_card,
    context_mode_display,
    error_card,
    goal_generation,
    scope_from_fetched_card,
    sessions_delete_binding_card,
    rename_binding_card,
)
from .messages import public_chat_kind as _public_chat_kind
from ..cards.defaults import decode_defaults_action
from ..defaults import DefaultConfigurationError
from ..runtime.contracts import (
    StopDisposition,
    ThreadDeleteUnavailable,
    ThreadDeleteTargetChanged,
)
from ..domain import (
    CardControlIntent,
    CardControlName,
    MentionContextMode,
    NativeCapability,
    SettingsSection,
    ScopeKind,
)
from ..error_messages import describe_error
from ..model_settings import ModelCatalogError
from ..management import (
    ActivePointerChanged,
    CurrentBindingChanged,
    CurrentBindingTarget,
    CurrentSideTarget,
    ExactBindingTarget,
    NoCurrentBinding,
    SideIdentityMismatch,
)
from ..projects import ProjectError

from . import constants
from .app_values import (
    _CardReplyConversation,
    _CardReplyTarget,
    _RenameNameGeneration,
)
from .base import (
    CHANNEL_LOGGER_NAME,
    _binding_turn_settings,
    _markdown_code,
    _session_title,
)
from .turn_files import TurnFileChannel


logger = logging.getLogger(CHANNEL_LOGGER_NAME)


class CardControlChannel(TurnFileChannel):
    """Card control actions dispatched by name."""

    async def _handle_defaults_card_action(self, event: Any) -> None:
        action = getattr(event, "action", None)
        message_id = str(getattr(event, "message_id", "") or "")
        chat_id = str(getattr(event, "chat_id", "") or "")
        scope = None
        notice = None
        failed = False
        draft = None
        try:
            operator = getattr(getattr(event, "operator", None), "open_id", None)
            if getattr(action, "tag", None) != "button" or not message_id or not chat_id or not operator:
                raise CardActionError("默认会话配置回调缺少真实卡片或操作者。")
            fetched = await self._channel.fetch_message(message_id)
            chat_kind = _public_chat_kind(await self._channel.get_chat_info(chat_id))
            scope = scope_from_fetched_card(
                app_id=self._app_id, callback_chat_id=chat_id,
                fetched_message=fetched, chat_type=chat_kind,
            )
            if chat_kind is None:
                raise CardActionError("无法确认默认配置所属聊天的类型，请重新打开 /defaults。")
            request = decode_defaults_action(
                scope, getattr(action, "value", None), getattr(action, "form_value", None),
            )
            service = self._management.defaults
            assert service is not None
            if request["mode"] == "load_workspace":
                draft = request
            else:
                await service.manage(request)
                notice = (
                    "已删除当前聊天的精确默认配置；群名规则仍可生效。"
                    if request["mode"] == "delete" else "已保存当前聊天的默认会话配置。"
                )
        except (CardActionError, DefaultConfigurationError, ProjectError, ModelCatalogError) as error:
            notice, failed = str(error), True
        except Exception as error:
            logger.warning("default configuration callback failed", extra={"error_type": type(error).__name__})
            notice, failed = "默认会话配置操作未能确认，请重新打开 /defaults 查看。", True
        if scope is None:
            if message_id:
                await self._safe_update_card(message_id, error_card(notice or "默认会话配置操作失败。"))
            return
        try:
            card = await self._defaults_card(scope, notice=notice, notice_is_error=failed, draft=draft)
            if await self._safe_update_card(message_id, card):
                return
        except Exception:
            logger.exception("default configuration card refresh failed")
        await self._reply(
            _CardReplyTarget(message_id, message_id, chat_id, _CardReplyConversation(scope.topic_id)),
            notice or "请重新打开 /defaults 查看默认会话配置。",
        )

    async def _generate_rename_name(
        self,
        intent: CardControlIntent,
        binding: ThreadBinding,
        generation: _RenameNameGeneration,
    ) -> None:
        async def redraw(*, generating: bool = False, name: str = "", notice: str | None = None) -> bool:
            async with generation.update_lock:
                if generation.invalidated:
                    return True
                async with asyncio.timeout(5):
                    return await self._safe_update_card(
                        intent.source_id,
                        rename_binding_card(
                            scope=intent.scope, binding_id=binding.id,
                            short_id=binding.short_id, project_alias=binding.project_alias,
                            generating=generating, generated_name=name, notice=notice,
                        ),
                    )

        try:
            if not await redraw(generating=True):
                await self._safe_reply_to_card(
                    intent, "生成中卡片未能更新，本次未生成名称；请重新发送 /rename。",
                )
                return
            if generation.invalidated:
                return
            name = ""
            notice = None
            try:
                generated = await self._management.generate_current_binding_name(
                    target=CurrentBindingTarget(intent.scope.key, binding.id),
                )
                current = self._bindings.active_binding(intent.scope.key)
                if (current is None or current.id != binding.id
                        or current.native_thread_id != binding.native_thread_id
                        or self._runtime.lifecycle_state(binding.id) is not None):
                    raise CardActionError("会话已切换或正在归档/删除，生成结果已失效；请重新发送 /rename。")
                name = generated.name
            except (NoCurrentBinding, CurrentBindingChanged, BindingNotFound):
                notice = "名称生成失败：会话已切换、归档或删除，生成结果已失效；请重新发送 /rename。"
            except CardActionError as error:
                notice = f"名称生成失败：{error}"
            except Exception as error:
                notice = f"名称生成失败：{describe_error(error)}。可以重新生成或手动填写。"
            if not await redraw(name=name, notice=notice):
                await self._safe_reply_to_card(
                    intent, "名称生成已结束，但卡片更新失败；原会话名称未改变。请重新发送 /rename。",
                )
        except Exception:
            logger.exception("rename name generation presentation failed")
            if not generation.invalidated:
                await self._safe_reply_to_card(
                    intent, "名称生成卡片未能更新；原会话名称未改变。请重新发送 /rename。",
                )
        finally:
            if self._rename_name_generations.get(intent.source_id) is generation:
                self._rename_name_generations.pop(intent.source_id)

    async def _card_control(self, intent: CardControlIntent) -> None:
        handler = self._card_control_handlers().get(intent.name)
        if handler is None:
            raise CardActionError(f"尚未处理的卡片动作：{intent.name.value}")
        await handler(intent)

    def _card_control_handlers(self) -> dict[CardControlName, Callable[[CardControlIntent], Awaitable[None]]]:
        """Bound handler for each CardControlName; the dispatcher rejects a name without one."""
        return {
            CardControlName.SIDE_CLOSE: self._card_side_close,
            CardControlName.OPEN_SETTINGS_SECTION: self._card_open_settings_section,
            CardControlName.REFRESH_SETTINGS: self._card_open_settings_section,
            CardControlName.REGISTER_PROJECT: self._card_register_project,
            CardControlName.SET_PROJECT_ENABLED: self._card_set_project_enabled,
            CardControlName.PREPARE_NEW_WORKSPACE: self._card_prepare_new_workspace,
            CardControlName.CREATE_BINDING: self._card_create_binding,
            CardControlName.CONFIGURE_BINDING: self._card_configure_binding,
            CardControlName.GENERATE_BINDING_NAME: self._card_generate_binding_name,
            CardControlName.RENAME_BINDING: self._card_rename_binding,
            CardControlName.ARCHIVE_BINDING: self._card_archive_binding,
            CardControlName.DELETE_BINDING: self._card_delete_binding,
            CardControlName.UNARCHIVE_BINDING: self._card_unarchive_binding,
            CardControlName.ACTIVATE_BINDING: self._card_activate_binding,
            CardControlName.ARCHIVE_EXACT_BINDING: self._card_archive_exact_binding,
            CardControlName.PREPARE_EXACT_DELETE_BINDING: self._card_prepare_exact_delete_binding,
            CardControlName.DELETE_EXACT_BINDING: self._card_delete_exact_binding,
            CardControlName.STOP_EXACT_BINDING: self._card_stop_exact_binding,
            CardControlName.RECHECK_EXACT_TURN: self._card_recheck_exact_turn,
            CardControlName.PREPARE_ARCHIVED_DELETE_BINDING: self._card_prepare_archived_delete_binding,
            CardControlName.DELETE_ARCHIVED_BINDING: self._card_delete_archived_binding,
            CardControlName.REFRESH_ARCHIVED_SESSIONS: self._card_refresh_archived_sessions,
            CardControlName.SESSIONS_PAGE: self._card_sessions_page,
            CardControlName.GOAL_REFRESH: self._card_goal_action,
            CardControlName.GOAL_PAUSE: self._card_goal_action,
            CardControlName.GOAL_RESUME: self._card_goal_action,
            CardControlName.GOAL_CLEAR: self._card_goal_action,
        }

    async def _card_side_close(self, intent: CardControlIntent) -> None:
        assert intent.side_id is not None
        record = self._bindings.get_side_topic(intent.side_id)
        try:
            closed = await self._management.close_side(
                target=CurrentSideTarget(
                    side_id=record.id,
                    app_id=intent.scope.app_id,
                    chat_id=intent.scope.chat_id,
                    topic_id=intent.scope.topic_id,
                    root_message_id=intent.source_id,
                )
            )
        except SideIdentityMismatch as error:
            raise CardActionError(
                "Side 卡片与当前话题或根消息不一致，本次未执行。"
            ) from error
        if closed.outcome is None and not closed.missing_runtime_session:
            await self._update_side_card(
                closed.record,
                notice="这个 Side 已经结束。",
            )
            return
        if closed.missing_runtime_session:
            await self._update_side_card(
                closed.record,
                notice=(
                    "服务内已没有对应 Side Session；该话题已过期。"
                    if closed.record.state is SideTopicState.EXPIRED
                    else f"Side 已进入 {closed.record.state.value}。"
                ),
            )

    async def _card_open_settings_section(self, intent: CardControlIntent) -> None:
        assert intent.settings_section is not None
        await self._update_card(
            intent.source_id,
            self._settings_card(
                intent.scope,
                section=intent.settings_section,
            ),
        )

    async def _card_register_project(self, intent: CardControlIntent) -> None:
        assert intent.project_alias is not None
        assert intent.create_directory is not None
        project = await self._management.register_project(
            alias=intent.project_alias,
            path=intent.project_path,
            create_directory=intent.create_directory,
        )
        await self._safe_update_card(
            intent.source_id,
            self._settings_card(
                intent.scope,
                section=SettingsSection.PROJECTS,
                notice=(
                    f"已登记项目 {project.alias}：{project.cwd}。"
                    "下一步：发送 /new，选择这个项目创建会话。"
                ),
            ),
        )

    async def _card_set_project_enabled(self, intent: CardControlIntent) -> None:
        assert intent.project_alias is not None
        assert intent.enabled is not None
        assert intent.expected_revision is not None
        project = await self._management.set_project_enabled(
            alias=intent.project_alias,
            enabled=intent.enabled,
            expected_revision=intent.expected_revision,
        )
        await self._safe_update_card(
            intent.source_id,
            self._settings_card(
                intent.scope,
                section=SettingsSection.PROJECTS,
                notice=(
                    f"已{'启用' if project.enabled else '停用'}项目 "
                    f"{project.alias}。"
                    + (
                        "下一步：发送 /new，选择这个项目创建会话。"
                        if project.enabled else "已有会话仍可继续使用。"
                    )
                ),
            ),
        )

    async def _card_prepare_new_workspace(self, intent: CardControlIntent) -> None:
        assert intent.project_alias is not None
        assert intent.expected_revision is not None
        chat_kind = _public_chat_kind(await self._channel.get_chat_info(intent.scope.chat_id))
        card = await self._new_binding_picker(
            scope=intent.scope, initial_project_alias=intent.project_alias,
            expected_project_revision=intent.expected_revision,
            allow_context_mode=chat_kind == "group",
            settings_draft=intent.new_settings_draft,
        )
        if not await self._safe_update_card(intent.source_id, card):
            await self._safe_reply_to_card(
                intent, "worktree 选项已查询，但卡片更新失败；未创建会话。请重新发送 /new。",
            )

    async def _card_create_binding(self, intent: CardControlIntent) -> None:
        assert intent.project_alias is not None
        assert intent.expected_revision is not None
        assert intent.reaction_pulse_enabled is not None
        assert intent.progress_card_enabled is not None
        assert intent.completion_mention_enabled is not None
        message_context_mode = (
            intent.message_context_mode or MentionContextMode.CURRENT_ONLY
        )
        context_anchor = None
        if message_context_mode is MentionContextMode.CATCH_UP:
            context_anchor = await self._resolve_context_anchor(
                intent.scope,
                intent.source_id,
            )
        settings = await self._resolve_card_model_settings(intent)
        turn_settings = _binding_turn_settings(settings)
        task_feedback = BindingTaskFeedback(
            reaction_pulse_enabled=bool(intent.reaction_pulse_enabled),
            progress_card_enabled=bool(intent.progress_card_enabled),
            completion_mention_enabled=bool(intent.completion_mention_enabled),
            progress_card_settings=intent.progress_card_settings,
        )
        project, binding = await self._create_binding(
            scope=intent.scope,
            sender_id=intent.sender_id,
            project_alias=intent.project_alias,
            expected_revision=intent.expected_revision,
            turn_settings=turn_settings,
            task_feedback=task_feedback,
            message_context_mode=message_context_mode,
            context_anchor=context_anchor,
            worktree_id=intent.worktree_id,
            worktree_create=intent.worktree_create,
        )
        workspace_cwd = binding.working_cwd
        workspace_summary = (
            f"实际工作目录：`{_markdown_code(workspace_cwd)}`。" if workspace_cwd else ""
        )
        updated = await self._safe_update_card(
            intent.source_id,
            binding_created_card(
                short_id=binding.short_id,
                project_alias=project.alias,
                settings=settings,
                task_feedback=binding.task_feedback,
                message_context_mode=binding.message_context_mode,
                workspace_cwd=workspace_cwd,
            ),
        )
        if not updated:
            await self._safe_reply_to_card(
                intent,
                f"✅ 会话创建成功：已选择项目 `{project.alias}`，"
                f"并创建、切换到会话 `{binding.short_id}`。"
                f"{workspace_summary}"
                f"Model 来源：{'继承 Codex' if settings is None else '显式配置'}；"
                f"@ 时读取的消息范围："
                f"{context_mode_display(binding.message_context_mode)}。"
                f"执行中表情闪烁："
                f"{'开启' if binding.task_feedback.reaction_pulse_enabled else '关闭'}；"
                f"进度卡：{'开启' if binding.task_feedback.progress_card_enabled else '关闭'}；"
                f"结束时 @ 提醒：{'开启' if binding.task_feedback.completion_mention_enabled else '关闭'}。"
                "现在可以直接发送任务，例如：梳理这个项目的结构。"
                "如果刚才的任务因没有会话而未执行，请重新发送。"
                "在群聊和群话题中，每条消息都需要 @机器人。",
            )

    async def _card_configure_binding(self, intent: CardControlIntent) -> None:
        assert intent.binding_id is not None
        assert intent.expected_settings_revision is not None
        assert intent.expected_context_revision is not None
        assert intent.feedback_revision is not None
        assert intent.message_context_mode is not None
        assert intent.reaction_pulse_enabled is not None
        assert intent.progress_card_enabled is not None
        assert intent.completion_mention_enabled is not None
        settings = await self._resolve_card_model_settings(intent)
        before = self._bindings.get(intent.binding_id)
        task_feedback = replace(
            before.task_feedback,
            reaction_pulse_enabled=bool(intent.reaction_pulse_enabled),
            progress_card_enabled=bool(intent.progress_card_enabled),
            completion_mention_enabled=bool(intent.completion_mention_enabled),
        )
        context_anchor = None
        if (
            before.message_context_mode is MentionContextMode.CURRENT_ONLY
            and intent.message_context_mode is MentionContextMode.CATCH_UP
        ):
            context_anchor = await self._resolve_context_anchor(
                intent.scope,
                intent.source_id,
            )
        try:
            binding = await self._management.configure_current_binding(
                target=CurrentBindingTarget(
                    intent.scope.key,
                    intent.binding_id,
                ),
                expected_settings_revision=intent.expected_settings_revision,
                expected_context_revision=intent.expected_context_revision,
                expected_feedback_revision=intent.feedback_revision,
                settings=_binding_turn_settings(settings),
                task_feedback=task_feedback,
                message_context_mode=intent.message_context_mode,
                context_anchor=context_anchor,
            )
        except NoCurrentBinding as error:
            raise CardActionError(
                "当前 Scope 已没有 active 会话，请重新发送 /new。"
            ) from error
        except CurrentBindingChanged as error:
            raise CardActionError(
                "active 会话已切换，本卡片未执行；请重新发送 /config。"
            ) from error
        except (
            BindingSettingsRevisionConflict,
            BindingContextRevisionConflict,
            BindingFeedbackRevisionConflict,
        ) as error:
            raise CardActionError(
                "会话配置已变化，本卡片未执行；请重新发送 /config。"
            ) from error
        updated = await self._safe_update_card(
            intent.source_id,
            binding_configured_card(
                short_id=binding.short_id,
                project_alias=binding.project_alias,
                settings=settings,
                task_feedback=binding.task_feedback,
                message_context_mode=binding.message_context_mode,
            ),
        )
        if not updated:
            await self._safe_reply_to_card(
                intent,
                f"✅ 会话 `{binding.short_id}` 配置已保存："
                f"Model 来源：{'继承 Codex' if settings is None else '显式配置'}；"
                f"@ 时读取的消息范围："
                f"{context_mode_display(binding.message_context_mode)}。"
                f"执行中表情闪烁："
                f"{'开启' if binding.task_feedback.reaction_pulse_enabled else '关闭'}；"
                f"进度卡：{'开启' if binding.task_feedback.progress_card_enabled else '关闭'}；"
                f"结束时 @ 提醒：{'开启' if binding.task_feedback.completion_mention_enabled else '关闭'}。"
                "会话后续每条新 Turn 都会应用。",
            )

    async def _card_generate_binding_name(self, intent: CardControlIntent) -> None:
        assert intent.binding_id is not None
        if intent.source_id in self._rename_name_generations:
            return
        binding = self._bindings.get(intent.binding_id)
        current = self._bindings.active_binding(intent.scope.key)
        if current is None or current.id != binding.id:
            raise CardActionError("active 会话已切换，本重命名卡片未执行；请重新发送 /rename。")
        generation = _RenameNameGeneration()
        self._rename_name_generations[intent.source_id] = generation
        # The SDK serializes card actions by chat. Release its callback
        # before model I/O so other controls and ordinary input can proceed.
        generation.task = asyncio.create_task(
            self._generate_rename_name(intent, binding, generation),
            name=f"rename-name:{intent.source_id}",
        )

    async def _card_rename_binding(self, intent: CardControlIntent) -> None:
        assert intent.binding_id is not None
        assert intent.thread_name is not None
        generation = self._rename_name_generations.get(intent.source_id)
        if generation is not None:
            generation.invalidated = True
            # Drain any in-flight redraw before either the success or
            # shared error receipt. Invalidated jobs cannot redraw again.
            async with generation.update_lock:
                pass
        try:
            renamed = await self._management.rename_current_binding(
                target=CurrentBindingTarget(
                    intent.scope.key,
                    intent.binding_id,
                ),
                name=intent.thread_name,
            )
        except (NoCurrentBinding, CurrentBindingChanged) as error:
            raise CardActionError(
                "active 会话已切换，本重命名卡片未执行；请重新发送 /rename。"
            ) from error
        binding = renamed.binding
        updated = await self._safe_update_card(
            intent.source_id,
            binding_lifecycle_result_card(
                title="会话已重命名",
                short_id=binding.short_id,
                project_alias=binding.project_alias,
                message=(
                    "✅ 会话名称已更新为："
                    f"`{_markdown_code(renamed.name)}`"
                ),
            ),
        )
        if not updated:
            await self._safe_reply_to_card(
                intent,
                f"✅ 会话 `{binding.short_id}` 已重命名为："
                f"`{_markdown_code(renamed.name)}`",
            )

    async def _card_archive_binding(self, intent: CardControlIntent) -> None:
        assert intent.binding_id is not None
        try:
            archived = await self._management.archive_current_binding(
                target=CurrentBindingTarget(
                    intent.scope.key,
                    intent.binding_id,
                ),
            )
        except (NoCurrentBinding, CurrentBindingChanged) as error:
            raise CardActionError(
                "active 会话已切换，本归档卡片未执行；请重新发送 /archive。"
            ) from error
        updated = await self._safe_update_card(
            intent.source_id,
            binding_lifecycle_result_card(
                title="会话已归档",
                short_id=archived.short_id,
                project_alias=archived.project_alias,
                message=(
                    "✅ Codex 会话已归档，当前 Scope 已没有 active 会话。"
                    "\n\n发送 `/sessions archived` 可查看和恢复。"
                ),
            ),
        )
        if not updated:
            await self._safe_reply_to_card(
                intent,
                "✅ 当前会话已归档；发送 /sessions archived 可查看和恢复。",
            )

    async def _card_delete_binding(self, intent: CardControlIntent) -> None:
        assert intent.binding_id is not None
        try:
            deleted = await self._management.delete_current_binding(
                target=CurrentBindingTarget(
                    intent.scope.key,
                    intent.binding_id,
                ),
                expected_native_thread_id=intent.expected_native_thread_id,
            )
        except (NoCurrentBinding, CurrentBindingChanged) as error:
            raise CardActionError(
                "active 会话已切换，本删除卡片未执行；请重新发送 /delete。"
            ) from error
        updated = await self._safe_update_card(
            intent.source_id,
            binding_lifecycle_result_card(
                title="会话已永久删除",
                short_id=deleted.short_id,
                project_alias=deleted.project_alias,
                message=(
                    (
                        "✅ Codex 会话及 Netizen 中的会话记录已永久删除，"
                        "当前 Scope 已没有 active 会话。"
                        if deleted.native_thread_id is not None
                        else "✅ 当前 Lazy 会话已删除，当前 Scope 已没有 active 会话。"
                    )
                    + "\n\n发送 `/new` 创建新会话，或 `/resume <短 ID>` 切换。"
                ),
            ),
        )
        if not updated:
            await self._safe_reply_to_card(
                intent,
                "✅ 当前会话已永久删除。",
            )

    async def _card_unarchive_binding(self, intent: CardControlIntent) -> None:
        assert intent.binding_id is not None
        binding = self._bindings.get(intent.binding_id)
        if binding.scope_key != intent.scope.key:
            raise BindingNotFound(intent.binding_id)
        context_anchor = None
        if binding.message_context_mode is MentionContextMode.CATCH_UP:
            context_anchor = await self._resolve_context_anchor(
                intent.scope,
                intent.source_id,
            )
        restored = await self._management.restore_current_binding(
            scope_key=intent.scope.key,
            reference=binding.id,
            context_anchor=context_anchor,
        )
        updated = await self._safe_update_card(
            intent.source_id,
            binding_lifecycle_result_card(
                title="会话已恢复并切换",
                short_id=restored.short_id,
                project_alias=restored.project_alias,
                message="✅ 已恢复 Codex 会话，并切换为当前会话。",
            ),
        )
        if not updated:
            await self._safe_reply_to_card(
                intent,
                f"✅ 已恢复并切换到会话 {restored.short_id}。",
            )

    async def _card_activate_binding(self, intent: CardControlIntent) -> None:
        assert intent.binding_id is not None
        binding = self._bindings.get(intent.binding_id)
        if binding.scope_key != intent.scope.key:
            raise BindingNotFound(intent.binding_id)
        context_anchor = None
        if binding.message_context_mode is MentionContextMode.CATCH_UP:
            context_anchor = await self._resolve_context_anchor(
                intent.scope,
                intent.source_id,
            )
        activated = await self._management.resume_current_binding(
            scope_key=intent.scope.key,
            reference=binding.id,
            context_anchor=context_anchor,
        )
        success_notice = (
            f"✅ {'已恢复并切换' if activated.native_thread_id else '已切换'}到会话 {activated.short_id}"
            f"（{activated.project_alias}）。"
        )
        try:
            refreshed = await self._safe_update_card(
                intent.source_id,
                await self._sessions_card(
                    scope=intent.scope,
                    page=0,
                    session_cursor=intent.session_cursor,
                    notice=success_notice,
                ),
            )
        except Exception:
            logger.exception(
                "failed to rebuild sessions card after binding activation",
            )
            refreshed = False
        if not refreshed:
            await self._safe_reply_to_card(intent, success_notice)

    async def _card_archive_exact_binding(self, intent: CardControlIntent) -> None:
        assert intent.binding_id is not None
        assert intent.page is not None
        binding = self._bindings.get(intent.binding_id)
        if binding.scope_key != intent.scope.key:
            raise BindingNotFound(intent.binding_id)
        archived = await self._management.archive_exact_binding(
            target=ExactBindingTarget(
                scope_key=intent.scope.key,
                binding_id=binding.id,
                expected_active_binding_id=None,
            ),
        )
        success_notice = (
            f"✅ 已归档会话 {archived.short_id}"
            f"（{archived.project_alias}）；历史仍可恢复。"
        )
        try:
            refreshed = await self._safe_update_card(
                intent.source_id,
                await self._sessions_card(
                    scope=intent.scope,
                    page=intent.page,
                    session_cursor=intent.session_cursor,
                    notice=success_notice,
                ),
            )
        except Exception:
            logger.exception(
                "failed to rebuild sessions card after exact archive",
            )
            refreshed = False
        if not refreshed:
            await self._safe_reply_to_card(intent, success_notice)

    async def _card_prepare_exact_delete_binding(self, intent: CardControlIntent) -> None:
        assert intent.binding_id is not None
        assert intent.page is not None
        binding = self._bindings.get(intent.binding_id)
        if binding.scope_key != intent.scope.key:
            raise BindingNotFound(intent.binding_id)
        if binding.native_thread_id != intent.expected_native_thread_id:
            raise CardActionError(
                "会话的 Codex 历史已变化，请重新发送 /sessions 后再删除。"
            )
        if (
            binding.native_thread_id is not None
            and NativeCapability.DELETE
            not in self._runtime.available_capabilities
        ):
            raise ThreadDeleteUnavailable(
                "当前 SDK/App Server 的 Thread Delete 兼容契约未通过；"
                "本次未调用 Codex。"
            )
        metadata = await self._management.binding_metadata_exact(
            binding.id,
            deadline=asyncio.get_running_loop().time() + constants.SESSION_QUERY_TIMEOUT_SECONDS,
        )
        confirmation = sessions_delete_binding_card(
            scope=intent.scope,
            binding_id=binding.id,
            short_id=binding.short_id,
            project_alias=binding.project_alias,
            title=_session_title(
                binding,
                metadata,
            ),
            native_thread_id=binding.native_thread_id,
            page=intent.page,
            session_cursor=intent.session_cursor,
        )
        updated = await self._safe_update_card(
            intent.source_id,
            confirmation,
        )
        if not updated:
            await self._safe_reply_to_card(
                intent,
                "无法打开删除确认卡，请重新发送 /sessions。",
            )

    async def _card_delete_exact_binding(self, intent: CardControlIntent) -> None:
        assert intent.binding_id is not None
        assert intent.page is not None
        binding = self._bindings.get(intent.binding_id)
        if binding.scope_key != intent.scope.key:
            raise BindingNotFound(intent.binding_id)
        try:
            deleted = await self._management.delete_exact_binding(
                target=ExactBindingTarget(
                    scope_key=intent.scope.key,
                    binding_id=binding.id,
                    expected_active_binding_id=None,
                ),
                expected_native_thread_id=intent.expected_native_thread_id,
            )
        except ThreadDeleteTargetChanged as error:
            raise CardActionError(
                "会话的 Codex 历史已变化，本次删除未执行；"
                "请重新发送 /sessions。"
            ) from error
        success_notice = (
            (
                f"✅ 已永久删除 Codex 会话 {deleted.short_id}"
                f"（{deleted.project_alias}）及 Netizen 中的会话记录。"
            )
            if deleted.native_thread_id is not None
            else (
                f"✅ 已删除 Lazy 会话 {deleted.short_id}"
                f"（{deleted.project_alias}）。"
            )
        )
        try:
            refreshed = await self._safe_update_card(
                intent.source_id,
                await self._sessions_card(
                    scope=intent.scope,
                    page=intent.page,
                    session_cursor=intent.session_cursor,
                    notice=success_notice,
                ),
            )
        except Exception:
            logger.exception(
                "failed to rebuild sessions card after exact delete",
            )
            refreshed = False
        if not refreshed:
            await self._safe_reply_to_card(intent, success_notice)

    async def _card_stop_exact_binding(self, intent: CardControlIntent) -> None:
        assert intent.binding_id is not None
        assert intent.page is not None
        binding = self._bindings.get(intent.binding_id)
        if binding.scope_key != intent.scope.key:
            raise BindingNotFound(intent.binding_id)
        try:
            stopped = await self._management.stop_exact_binding(
                target=ExactBindingTarget(
                    scope_key=intent.scope.key,
                    binding_id=binding.id,
                    expected_active_binding_id=(
                        intent.expected_active_binding_id
                    ),
                ),
                runtime_precondition=self._runtime_precondition(intent),
            )
        except ActivePointerChanged as error:
            raise CardActionError(
                "会话列表已变化，本次停止未执行；请刷新 /sessions。"
            ) from error
        notices = {
            StopDisposition.NOT_RUNNING: "✅ 该会话的任务刚刚已经结束。",
            StopDisposition.REQUESTED: (
                "已请求中断这一轮 Codex 任务；确认终态前会话仍显示为停止中。"
            ),
            StopDisposition.STOPPING: (
                "该会话正在停止；已再次尝试完成中断与终端清理。"
            ),
            StopDisposition.GOAL_REQUESTED: (
                "已请求暂停 Goal 并中断当前物理 Turn。"
            ),
            StopDisposition.GOAL_STOPPING: "该 Goal 正在暂停。",
            StopDisposition.COMPACTING: (
                "该会话正在压缩；当前没有已验证的安全取消能力。"
            ),
            StopDisposition.EXTERNAL_GOAL: (
                "这是外部 active Goal，当前无法安全重挂并暂停。"
            ),
        }
        notice = notices[stopped.disposition]
        try:
            refreshed = await self._safe_update_card(
                intent.source_id,
                await self._sessions_card(
                    scope=intent.scope,
                    page=intent.page,
                    session_cursor=intent.session_cursor,
                    notice=notice,
                ),
            )
        except Exception:
            logger.exception("failed to rebuild sessions card after exact stop")
            refreshed = False
        if not refreshed:
            await self._safe_reply_to_card(intent, notice)

    async def _card_recheck_exact_turn(self, intent: CardControlIntent) -> None:
        assert intent.binding_id is not None
        assert intent.page is not None
        binding = self._bindings.get(intent.binding_id)
        if binding.scope_key != intent.scope.key:
            raise BindingNotFound(intent.binding_id)
        try:
            await self._management.recheck_exact_turn(
                target=ExactBindingTarget(
                    scope_key=intent.scope.key,
                    binding_id=binding.id,
                    expected_active_binding_id=(
                        intent.expected_active_binding_id
                    ),
                ),
                runtime_precondition=self._runtime_precondition(intent),
            )
        except ActivePointerChanged as error:
            raise CardActionError(
                "会话列表已变化，本次重新检查未执行；请刷新 /sessions。"
            ) from error
        notice = (
            "正在重新检查上一轮任务；确认已结束后会补发结果或失败原因，并恢复续聊。"
            "若任务仍在执行，仅恢复状态检查，旧进度卡不再更新；"
            "停止尚未完成时仍需等待或重试 /stop。仍无法确认时会返回具体原因。"
        )
        try:
            refreshed = await self._safe_update_card(
                intent.source_id,
                await self._sessions_card(
                    scope=intent.scope,
                    page=intent.page,
                    session_cursor=intent.session_cursor,
                    notice=notice,
                ),
            )
        except Exception:
            logger.exception(
                "failed to rebuild sessions card after exact Turn recheck"
            )
            refreshed = False
        if not refreshed:
            await self._safe_reply_to_card(intent, notice)

    async def _card_prepare_archived_delete_binding(self, intent: CardControlIntent) -> None:
        assert intent.binding_id is not None
        assert intent.expected_native_thread_id is not None
        binding = self._bindings.get(intent.binding_id)
        if binding.scope_key != intent.scope.key:
            raise BindingNotFound(intent.binding_id)
        if binding.native_thread_id != intent.expected_native_thread_id:
            raise CardActionError(
                "归档会话的 Codex 历史已变化，请刷新后再删除。"
            )
        if NativeCapability.DELETE not in self._runtime.available_capabilities:
            raise ThreadDeleteUnavailable(
                "当前 SDK/App Server 的 Thread Delete 兼容契约未通过；"
                "本次未调用 Codex。"
            )
        metadata = await self._management.binding_metadata_exact(
            binding.id,
            deadline=asyncio.get_running_loop().time() + constants.SESSION_QUERY_TIMEOUT_SECONDS,
        )
        confirmation = archived_sessions_delete_binding_card(
            scope=intent.scope,
            binding_id=binding.id,
            short_id=binding.short_id,
            project_alias=binding.project_alias,
            title=_session_title(
                binding,
                metadata,
            ),
            native_thread_id=intent.expected_native_thread_id,
            session_cursor=intent.session_cursor,
        )
        updated = await self._safe_update_card(
            intent.source_id,
            confirmation,
        )
        if not updated:
            await self._safe_reply_to_card(
                intent,
                "无法打开删除确认卡，请重新发送 /sessions archived。",
            )

    async def _card_delete_archived_binding(self, intent: CardControlIntent) -> None:
        assert intent.binding_id is not None
        assert intent.expected_native_thread_id is not None
        binding = self._bindings.get(intent.binding_id)
        if binding.scope_key != intent.scope.key:
            raise BindingNotFound(intent.binding_id)
        try:
            deleted = await self._management.delete_archived_exact_binding(
                target=ExactBindingTarget(
                    scope_key=intent.scope.key,
                    binding_id=binding.id,
                    expected_active_binding_id=None,
                ),
                expected_native_thread_id=intent.expected_native_thread_id,
            )
        except ThreadDeleteTargetChanged as error:
            raise CardActionError(
                "归档会话的 Codex 历史已变化，本次删除未执行；请刷新。"
            ) from error
        success_notice = (
            f"✅ 已永久删除归档会话 {deleted.short_id}"
            f"（{deleted.project_alias}）、派生会话及 Netizen 中的会话记录。"
        )
        try:
            refreshed = await self._safe_update_card(
                intent.source_id,
                await self._archived_sessions_card(
                    scope=intent.scope,
                    session_cursor=intent.session_cursor,
                    notice=success_notice,
                ),
            )
        except Exception:
            logger.exception(
                "failed to rebuild archived sessions card after exact delete"
            )
            refreshed = False
        if not refreshed:
            await self._safe_reply_to_card(intent, success_notice)

    async def _card_refresh_archived_sessions(self, intent: CardControlIntent) -> None:
        await self._safe_update_card(
            intent.source_id,
            await self._archived_sessions_card(scope=intent.scope, session_cursor=intent.session_cursor),
        )

    async def _card_sessions_page(self, intent: CardControlIntent) -> None:
        page = intent.page or 0
        await self._safe_update_card(
            intent.source_id,
            await self._sessions_card(scope=intent.scope, page=page, session_cursor=intent.session_cursor),
        )

    async def _card_goal_action(self, intent: CardControlIntent) -> None:
        assert intent.binding_id is not None
        async with self._scope_coordinator.hold(intent.scope.key):
            binding = self._bindings.get(intent.binding_id)
            if binding.scope_key != intent.scope.key:
                raise BindingNotFound(intent.binding_id)
            if intent.name is CardControlName.GOAL_REFRESH:
                await self._safe_update_card(intent.source_id, await self._goal_management_card(binding, intent.scope))
                return
            before = await self._runtime.goal_snapshot(binding)
            if (
                before is None
                or before.thread_id != binding.native_thread_id
                or goal_generation(before) != intent.goal_generation
                or before.status.value != intent.expected_goal_status
            ):
                raise CardActionError("Goal 已变化，本卡片操作未执行；请刷新或重新发送 /goal。")
            if intent.name is CardControlName.GOAL_PAUSE:
                await self._runtime.pause_goal(binding, expected_created_at=before.created_at)
                notice = "Goal 已暂停；不会中断正在执行的任务，后续不再自动续跑。"
            elif intent.name is CardControlName.GOAL_CLEAR:
                await self._runtime.clear_goal(binding, expected_created_at=before.created_at)
                notice = "Goal 已结束；不会中断正在执行的任务。"
            else:
                origin = _CardReplyTarget(
                    id=intent.source_id, message_id=intent.source_id,
                    chat_id=intent.scope.chat_id,
                    conversation=_CardReplyConversation(
                        thread_id=intent.scope.topic_id,
                        chat_type="p2p" if intent.scope.kind is ScopeKind.DIRECT else "group",
                    ),
                )
                submission = await self._runtime.resume_goal(
                    binding=binding, owner_id=intent.sender_id, origin=origin,
                    expected_created_at=before.created_at,
                )
                try:
                    await self._present_running_goal(
                        binding=binding, submission=submission, origin=origin,
                    )
                finally:
                    submission.release_receipt_attempt()
                notice = "Goal 已恢复。"
            await self._goal_management_receipt(
                binding, intent.scope, _CardReplyTarget(
                    id=intent.source_id, message_id=intent.source_id,
                    chat_id=intent.scope.chat_id,
                    conversation=_CardReplyConversation(
                        thread_id=intent.scope.topic_id,
                        chat_type="p2p" if intent.scope.kind is ScopeKind.DIRECT else "group",
                    ),
                ), notice=notice, source_id=intent.source_id,
            )
