"""Slash commands dispatched by name."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import asyncio
import logging
from dataclasses import replace
from typing import Any

from lark_channel import (
    OutboundCard,
)

from ..bindings import (
    SideTopicState,
)
from ..cards import (
    CardActionError,
    archive_binding_card,
    config_card,
    context_mode_display,
    fetched_card_topic_id,
    scope_from_fetched_card,
    delete_binding_card,
    rename_binding_card,
)
from .messages import public_chat_kind as _public_chat_kind
from ..cards.status import (
    decode_status_command, session_status_card,
)
from ..runtime.contracts import (
    ReleaseDisposition,
    SteerRace,
    StopDisposition,
    ThreadDeleteUnavailable,
    ThreadLifecycleError,
    ThreadNotMaterialized,
    ThreadReleaseError,
)
from ..domain import (
    ACTIVE_STATE_VALUES,
    ActiveState,
    ControlIntent,
    ControlName,
    GoalStatus,
    MentionContextMode,
    NativeCapability,
)
from ..experience import (
    InvalidInteraction,
)
from ..management import (
    CurrentBindingChanged,
    CurrentBindingTarget,
    NoCurrentBinding,
)

from . import constants
from .app_values import (
    _CardReplyConversation,
    _CardCommandOrigin,
    _message_chat_type,
)
from .base import (
    CHANNEL_LOGGER_NAME,
    _markdown_code,
    _session_title,
    _thread_metadata_status_lines,
    _git_branch_status_lines,
    _context_window_status_line,
    _thread_subscription_status_line,
    _turn_progress_status_lines,
)
from .card_controls import CardControlChannel


logger = logging.getLogger(CHANNEL_LOGGER_NAME)


class CommandChannel(CardControlChannel):
    """Slash commands dispatched by name."""

    async def _handle_status_command(self, event: Any) -> None:
        message_id = str(getattr(event, "message_id", "") or "")
        chat_id = str(getattr(event, "chat_id", "") or "")
        sender_id = getattr(getattr(event, "operator", None), "open_id", None)
        action = getattr(event, "action", None)
        if not message_id or not chat_id or not isinstance(sender_id, str) or not sender_id:
            logger.warning("status command callback missing message or operator identity")
            return
        origin = _CardCommandOrigin(message_id, message_id, chat_id, _CardReplyConversation(None))
        renewed_card: OutboundCard | None = None
        try:
            if getattr(action, "tag", None) != "button" or getattr(action, "form_value", None):
                raise CardActionError("状态卡操作无效，请重新发送 /status。")
            fetched = await self._channel.fetch_message(message_id)
            topic_id = fetched_card_topic_id(callback_chat_id=chat_id, fetched_message=fetched)
            # Establish the real reply location before any further I/O. Chat
            # type is also required for /config and /status inside topics.
            origin = replace(origin, conversation=_CardReplyConversation(topic_id))
            chat_kind = _public_chat_kind(await self._channel.get_chat_info(chat_id))
            if chat_kind not in {"p2p", "group"}:
                raise CardActionError("无法判断状态卡所在聊天类型，请重新发送 /status。")
            scope = scope_from_fetched_card(
                app_id=self._app_id, callback_chat_id=chat_id,
                fetched_message=fetched, chat_type=chat_kind,
            )
            origin = replace(origin, conversation=_CardReplyConversation(topic_id, chat_kind))
            decoded = decode_status_command(
                scope=scope, message_id=message_id, sender_id=sender_id,
                value=getattr(action, "value", None),
            )
            side = self._bindings.side_topic_for_message(
                app_id=self._app_id, chat_id=chat_id, topic_id=scope.topic_id,
                root_message_id=None,
            )
            if decoded.target_kind == "side":
                if side is None or side.id != decoded.target_id:
                    raise CardActionError("状态卡对应的 Side 已变化或不可用，本次未执行。")
                if side.state is SideTopicState.CREATING:
                    raise CardActionError("这个 Side 尚未就绪，本次未执行。")
                if not await self._side_session_available(origin, side):
                    return
                renewed_card = decoded.card()
                await self._side_control(origin, decoded.command, side)
            else:
                if side is not None:
                    raise CardActionError("状态卡与当前 Side 话题不一致，本次未执行。")
                renewed_card = decoded.card()
                await self._control(origin, decoded.command, expected_binding_id=decoded.target_id)
        except CardActionError as error:
            await self._report_input_error(origin, InvalidInteraction(str(error)))
        except Exception as error:
            await self._report_input_error(origin, error)
        finally:
            # The SDK deduplicates source/operator/payload, so renew only its
            # nonce. The original snapshot stays unchanged; command replies
            # always use the same path as slash commands. Never replay a command
            # when this best-effort transport update fails.
            if renewed_card is not None:
                if not await self._safe_update_card(message_id, renewed_card):
                    await self._reply(origin, "操作入口未能更新；再次操作请重新发送 /status。刚才的操作不会自动重试。")

    async def _control(
        self, message: Any, intent: ControlIntent, *, expected_binding_id: str | None = None,
    ) -> None:
        if expected_binding_id is not None:
            # No await before the selected command captures its Binding. The
            # ordinary command's existing exact-target checks protect mutations
            # across subsequent I/O; never follow a later current pointer.
            current = self._bindings.active_binding(intent.scope.key)
            if current is None or current.id != expected_binding_id:
                raise InvalidInteraction("当前会话已切换或不可用，本次未执行；请重新发送 /status。")
        handler = self._command_handlers().get(intent.name)
        if handler is None:
            raise InvalidInteraction(f"尚未处理的命令：/{intent.name.value}")
        await handler(message, intent)

    def _command_handlers(self) -> dict[ControlName, Callable[[Any, ControlIntent], Awaitable[None]]]:
        """Bound handler for each ControlName; the dispatcher rejects a name without one."""
        return {
            ControlName.FORK: self._command_fork,
            ControlName.USAGE: self._command_usage,
            ControlName.ADMIN: self._command_admin,
            ControlName.MENU: self._command_help,
            ControlName.HELP: self._command_help,
            ControlName.SIDE: self._command_side,
            ControlName.PROJECTS: self._command_projects,
            ControlName.DEFAULTS: self._command_defaults,
            ControlName.CRON: self._command_cron,
            ControlName.GOAL: self._command_goal,
            ControlName.CONFIG: self._command_config,
            ControlName.COMPACT: self._command_compact,
            ControlName.NEW: self._command_new,
            ControlName.SESSIONS: self._command_sessions,
            ControlName.RESUME: self._command_resume,
            ControlName.UNARCHIVE: self._command_unarchive,
            ControlName.RENAME: self._command_rename,
            ControlName.ARCHIVE: self._command_archive,
            ControlName.DELETE: self._command_delete,
            ControlName.RELEASE: self._command_release,
            ControlName.STATUS: self._command_status,
            ControlName.STOP: self._command_stop,
        }

    async def _command_fork(self, message: Any, intent: ControlIntent) -> None:
        await self._fork_card(message, intent)

    async def _command_usage(self, message: Any, intent: ControlIntent) -> None:
        await self._usage(message)

    async def _command_admin(self, message: Any, intent: ControlIntent) -> None:
        await self._reply(message, self._admin_entry())

    async def _command_help(self, message: Any, intent: ControlIntent) -> None:
        await self._reply(message, self._help())

    async def _command_side(self, message: Any, intent: ControlIntent) -> None:
        await self._create_side(message, intent)

    async def _command_projects(self, message: Any, intent: ControlIntent) -> None:
        await self._reply(message, self._settings_card(intent.scope))

    async def _command_defaults(self, message: Any, intent: ControlIntent) -> None:
        await self._reply(message, await self._defaults_card(intent.scope))

    async def _command_cron(self, message: Any, intent: ControlIntent) -> None:
        await self._reply(message, await self._schedule_manager_card(intent.scope))

    async def _command_goal(self, message: Any, intent: ControlIntent) -> None:
        binding = self._bindings.active_binding(intent.scope.key)
        if binding is None:
            await self._reply_no_current_binding(message, intent.scope)
            return
        argument = intent.arguments[0] if intent.arguments else None
        action = argument.lower() if argument is not None else None
        if argument is None:
            await self._reply(message, await self._goal_management_card(binding, intent.scope))
            return
        if action in {"pause", "resume", "clear"}:
            before = await self._runtime.goal_snapshot(binding)
            if before is None:
                await self._reply(message, await self._goal_management_card(binding, intent.scope))
                return
            if action == "pause":
                await self._runtime.pause_goal(binding, expected_created_at=before.created_at)
                notice = "Goal 已暂停；不会中断正在执行的任务，后续不再自动续跑。"
            elif action == "clear":
                await self._runtime.clear_goal(binding, expected_created_at=before.created_at)
                notice = "Goal 已结束；不会中断正在执行的任务。"
            else:
                submission = await self._runtime.resume_goal(
                    binding=binding, owner_id=intent.sender_id, origin=message,
                    expected_created_at=before.created_at,
                )
                try:
                    await self._present_running_goal(
                        binding=binding, submission=submission, origin=message,
                    )
                finally:
                    submission.release_receipt_attempt()
                notice = "Goal 已恢复。"
            await self._goal_management_receipt(binding, intent.scope, message, notice=notice)
            return
        cwd = self._binding_creation_cwd(binding)
        submission = await self._runtime.start_goal(
            binding=binding, cwd=cwd,
            objective=self._model_task_text(message, argument, command="goal"),
            owner_id=intent.sender_id, origin=message,
        )
        try:
            current = self._bindings.get(binding.id)
            await self._goal_management_receipt(
                current, intent.scope, message,
                notice="Goal 已启动。此卡为查询时的状态，可点击刷新查看最新状态。",
            )
            await self._present_running_goal(
                binding=current, submission=submission, origin=message,
            )
        finally:
            submission.release_receipt_attempt()

    async def _command_config(self, message: Any, intent: ControlIntent) -> None:
        binding = self._bindings.active_binding(intent.scope.key)
        if binding is None:
            await self._reply_no_current_binding(message, intent.scope)
            return
        goal = await self._runtime.goal_snapshot(binding)
        active_goal = self._runtime.active_goal(binding.id)
        if active_goal is not None or (
            goal is not None and goal.status is GoalStatus.ACTIVE
        ):
            await self._reply(
                message,
                "当前 Goal 正在执行或状态未完成，不能修改会话配置；"
                "请先使用 /stop 停止当前任务，或等待执行结束。",
            )
            return
        active = self._runtime.active_turn(binding.id)
        if self._runtime.is_compacting(binding.id):
            await self._reply(
                message,
                "当前会话正在压缩上下文，完成前不能修改会话配置。",
            )
            return
        if active is not None:
            if active.state is ActiveState.STOPPING:
                notice = (
                    "当前 Turn 正在停止，不能修改会话配置；"
                    "若 /stop 曾提示清理失败，请再次发送 /stop 重试。"
                )
            else:
                notice = (
                    "当前 Turn 正在执行，不能修改会话配置；"
                    "请等待完成或先发送 /stop。"
                )
            await self._reply(
                message,
                notice,
            )
            return
        catalog = None
        catalog_error = None
        try:
            catalog = await self._runtime.model_catalog()
        except Exception as error:
            logger.warning(
                "native model catalog unavailable for /config settings",
                extra={"error_type": type(error).__name__},
            )
            catalog_error = "Model / Effort / Speed 暂不可用。"
        await self._reply(
            message,
            config_card(
                scope=intent.scope,
                binding_id=binding.id,
                short_id=binding.short_id,
                project_alias=binding.project_alias,
                settings_revision=binding.settings_revision,
                turn_settings=binding.turn_settings,
                catalog=catalog,
                context_revision=binding.context_revision,
                message_context_mode=binding.message_context_mode,
                feedback_revision=binding.feedback_revision,
                task_feedback=binding.task_feedback,
                allow_context_mode=(
                    _message_chat_type(message) == "group"
                ),
                catalog_error=catalog_error,
            ),
        )

    async def _command_compact(self, message: Any, intent: ControlIntent) -> None:
        binding = self._bindings.active_binding(intent.scope.key)
        if binding is None:
            await self._reply_no_current_binding(message, intent.scope)
            return
        submission = await self._runtime.compact(
            binding=binding,
            owner_id=intent.sender_id,
            origin=message,
        )
        try:
            await self._reply(
                message,
                "已开始压缩当前 Codex 会话；"
                "完成前该会话暂不接受新任务。",
            )
        finally:
            submission.release_receipt_attempt()

    async def _command_new(self, message: Any, intent: ControlIntent) -> None:
        projects = self._projects.list(enabled_only=True)
        enabled_aliases = {project.alias for project in projects}
        current = self._bindings.active_binding(intent.scope.key)
        if current is not None and current.project_alias in enabled_aliases:
            initial_project_alias = current.project_alias
        else:
            previous = self._bindings.get_latest_activated_binding(
                intent.scope.key, project_aliases=tuple(enabled_aliases),
            )
            initial_project_alias = previous.project_alias if previous else None
        card = await self._new_binding_picker(
            scope=intent.scope,
            initial_project_alias=initial_project_alias,
            allow_context_mode=(_message_chat_type(message) == "group"),
        )
        try:
            await self._reply(
                message,
                card,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Feishu rejected the complete /new Project card",
                extra={"project_count": len(projects)},
            )
            await self._reply(
                message,
                "飞书未接受包含全部 Project 的新建卡片；"
                "本次不会静默截断、分页或改走快捷创建。请稍后重试 /new。",
            )

    async def _command_sessions(self, message: Any, intent: ControlIntent) -> None:
        try:
            card = (
                await self._archived_sessions_card(scope=intent.scope)
                if intent.arguments
                else await self._sessions_card(scope=intent.scope, page=0)
            )
        except CardActionError as error:
            await self._reply(message, str(error))
            return
        await self._reply(message, card)

    async def _command_resume(self, message: Any, intent: ControlIntent) -> None:
        target = self._bindings.resolve_reference(
            scope_key=intent.scope.key,
            reference=intent.arguments[0],
        )
        context_anchor = None
        if target.message_context_mode is MentionContextMode.CATCH_UP:
            self._require_catch_up_message_scope(message, intent.scope)
            context_anchor = await self._resolve_context_anchor(
                intent.scope,
                intent.source_id,
            )
        binding = await self._management.resume_current_binding(
            scope_key=intent.scope.key,
            reference=intent.arguments[0],
            context_anchor=context_anchor,
        )
        await self._reply(
            message,
            f"{'已恢复并切换' if binding.native_thread_id else '已切换'}到会话 {binding.short_id}（{binding.project_alias}）。",
        )

    async def _command_unarchive(self, message: Any, intent: ControlIntent) -> None:
        target = self._bindings.resolve_reference(
            scope_key=intent.scope.key,
            reference=intent.arguments[0],
        )
        context_anchor = None
        if target.message_context_mode is MentionContextMode.CATCH_UP:
            self._require_catch_up_message_scope(message, intent.scope)
            context_anchor = await self._resolve_context_anchor(
                intent.scope,
                intent.source_id,
            )
        binding = await self._management.restore_current_binding(
            scope_key=intent.scope.key,
            reference=intent.arguments[0],
            context_anchor=context_anchor,
        )
        await self._reply(
            message,
            f"已恢复并切换到会话 {binding.short_id}（{binding.project_alias}）。",
        )

    async def _command_rename(self, message: Any, intent: ControlIntent) -> None:
        binding = self._bindings.active_binding(intent.scope.key)
        if binding is None:
            await self._reply_no_current_binding(message, intent.scope)
            return
        if binding.native_thread_id is None:
            raise ThreadNotMaterialized(
                "当前会话尚未创建原生 Codex Thread；"
                "请先发送一条真实任务，再使用 /rename。"
            )
        if not intent.arguments:
            metadata = await self._read_thread_metadata((binding,))
            await self._reply(
                message,
                rename_binding_card(
                    scope=intent.scope,
                    binding_id=binding.id,
                    short_id=binding.short_id,
                    project_alias=binding.project_alias,
                    current_title=_session_title(
                        binding,
                        metadata.get(binding.native_thread_id),
                    ),
                ),
            )
            return
        try:
            renamed = await self._management.rename_current_binding(
                target=CurrentBindingTarget(intent.scope.key, binding.id),
                name=intent.arguments[0],
            )
        except (NoCurrentBinding, CurrentBindingChanged) as error:
            raise ThreadLifecycleError(
                "当前会话已切换，本次重命名未执行。"
            ) from error
        name = renamed.name
        await self._reply(
            message,
            f"会话 `{renamed.binding.short_id}` 已重命名为：`{_markdown_code(name)}`",
        )

    async def _command_archive(self, message: Any, intent: ControlIntent) -> None:
        binding = self._bindings.active_binding(intent.scope.key)
        if binding is None:
            await self._reply_no_current_binding(message, intent.scope)
            return
        if binding.native_thread_id is None:
            raise ThreadNotMaterialized(
                "Lazy 会话没有原生历史可归档；如不再需要，请使用 /delete。"
            )
        metadata = await self._read_thread_metadata((binding,))
        await self._reply(
            message,
            archive_binding_card(
                scope=intent.scope,
                binding_id=binding.id,
                short_id=binding.short_id,
                project_alias=binding.project_alias,
                title=_session_title(
                    binding,
                    metadata.get(binding.native_thread_id),
                ),
            ),
        )

    async def _command_delete(self, message: Any, intent: ControlIntent) -> None:
        binding = self._bindings.active_binding(intent.scope.key)
        if binding is None:
            await self._reply_no_current_binding(message, intent.scope)
            return
        if (
            binding.native_thread_id is not None
            and NativeCapability.DELETE
            not in self._runtime.available_capabilities
        ):
            raise ThreadDeleteUnavailable(
                "当前 SDK/App Server 的 Thread Delete 兼容契约未通过；"
                "本次未调用 Codex，Binding 与原生历史均未改变。"
            )
        metadata = (
            await self._read_thread_metadata((binding,))
            if binding.native_thread_id is not None
            else {}
        )
        await self._reply(
            message,
            delete_binding_card(
                scope=intent.scope,
                binding_id=binding.id,
                short_id=binding.short_id,
                project_alias=binding.project_alias,
                title=_session_title(
                    binding,
                    metadata.get(binding.native_thread_id),
                ),
                native_thread_id=binding.native_thread_id,
            ),
        )

    async def _command_release(self, message: Any, intent: ControlIntent) -> None:
        binding = self._bindings.active_binding(intent.scope.key)
        if binding is None:
            await self._reply_no_current_binding(message, intent.scope)
            return
        try:
            released = await self._management.release_current_binding(
                target=CurrentBindingTarget(intent.scope.key, binding.id),
            )
        except (NoCurrentBinding, CurrentBindingChanged) as error:
            raise ThreadReleaseError(
                "当前会话已切换，本次订阅释放未执行。"
            ) from error
        disposition = released.disposition
        if disposition is ReleaseDisposition.NOT_MATERIALIZED:
            notice = "当前会话尚未物化，没有原生 Thread 订阅可释放。"
        elif disposition is ReleaseDisposition.NOT_SUBSCRIBED:
            notice = (
                "本进程当前没有该 Thread 的订阅；Binding 与原生历史均保留。"
            )
        else:
            notice = (
                "已取消本进程对当前 Thread 的订阅；Binding 与原生历史均保留，"
                "下次消息仍会 resume 同一 Thread。"
                "这不表示 App Server writer 已立即释放。"
            )
        await self._reply(message, notice)

    async def _command_status(self, message: Any, intent: ControlIntent) -> None:
        binding = self._bindings.active_binding(intent.scope.key)
        if binding is None:
            await self._reply_no_current_binding(message, intent.scope)
            return
        state = await self._binding_state(binding)
        try:
            metadata = await self._management.binding_metadata_exact(
                binding.id,
                deadline=asyncio.get_running_loop().time() + constants.SESSION_QUERY_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.warning("Status metadata unavailable", exc_info=True)
            metadata = None
        thread_metadata_lines = _thread_metadata_status_lines(binding, metadata)
        try:
            cwd = await self._binding_cwd(binding)
        except Exception:
            git_branch_lines = ("工作目录状态暂不可用；未改用项目目录。",)
        else:
            git_branch_lines = (f"工作目录：{cwd}", *await _git_branch_status_lines(cwd))
            if not cwd.is_dir():
                git_branch_lines += ("工作目录当前不可用；保留原目录，执行结果以 Codex 返回为准。",)
        context_window_line = _context_window_status_line(
            binding,
            self._runtime.context_window_usage(binding.id),
            active_turn=state in ACTIVE_STATE_VALUES,
        )
        progress_lines = _turn_progress_status_lines(
            self._runtime.turn_progress(binding.id)
        )
        model_lines = await self._binding_model_status_lines(binding)
        subscription_line = _thread_subscription_status_line(
            binding,
            self._runtime.thread_subscription_snapshot(binding.id),
        )
        context_mode_lines = (
            ()
            if _message_chat_type(message) == "p2p"
            else (
                "@ 时读取的消息范围："
                f"{context_mode_display(binding.message_context_mode)}",
            )
        )
        await self._reply(
            message,
            session_status_card(
                scope=intent.scope,
                binding_id=binding.id,
                short_id=binding.short_id,
                project_alias=binding.project_alias,
                native_thread_id=binding.native_thread_id,
                state=state,
                metadata_lines=thread_metadata_lines,
                git_branch_lines=git_branch_lines,
                context_window_line=context_window_line,
                context_mode_lines=context_mode_lines,
                model_lines=model_lines,
                progress_lines=progress_lines,
                subscription_line=subscription_line,
            ),
        )

    async def _command_stop(self, message: Any, intent: ControlIntent) -> None:
        binding = self._bindings.active_binding(intent.scope.key)
        if binding is None:
            await self._reply_no_current_binding(message, intent.scope)
            return
        active = self._runtime.active_turn(binding.id)
        active_goal = self._runtime.active_goal(binding.id)
        compacting = self._runtime.is_compacting(binding.id)
        if active is None and active_goal is None and not compacting:
            persisted_goal = await self._runtime.goal_snapshot(binding)
            active_goal = self._runtime.active_goal(binding.id)
            if persisted_goal is None or active_goal is None:
                await self._reply(message, "当前没有正在执行的任务。")
                return
        if active is None and active_goal is None and not compacting:
            await self._reply(message, "当前没有正在执行的任务。")
            return
        acknowledged = False

        async def acknowledge() -> None:
            nonlocal acknowledged
            # Runtime holds the Binding lock while awaiting this receipt,
            # so native terminal delivery cannot overtake it.
            await self._reply(
                message,
                (
                    "正在暂停 Codex Goal 并中断当前物理 Turn。"
                    if active_goal is not None
                    else "正在中断当前 Codex Turn。"
                ),
            )
            acknowledged = True

        try:
            stopped = await self._management.stop_current_binding(
                target=CurrentBindingTarget(intent.scope.key, binding.id),
                acknowledge=acknowledge,
            )
        except (NoCurrentBinding, CurrentBindingChanged) as error:
            raise SteerRace(
                "active 会话已切换，本次停止未执行；请重新发送 /stop。"
            ) from error
        result = stopped.disposition
        if result is StopDisposition.NOT_RUNNING and not acknowledged:
            await self._reply(message, "当前任务恰好已经结束。")
        elif result is StopDisposition.COMPACTING:
            await self._reply(
                message,
                "当前会话正在压缩上下文；/stop 只中断普通 Turn，"
                "请等待压缩完成。",
            )
        elif result is StopDisposition.EXTERNAL_GOAL:
            await self._reply(
                message,
                "当前是重启前或外部客户端启动的 active Goal；"
                "本服务无法安全重挂并中断，请先在原生 Codex 中暂停。",
            )
