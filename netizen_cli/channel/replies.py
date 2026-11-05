"""Replies, error reports and Goal management cards shared by every interaction."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from lark_channel import (
    Identity,
    OutboundCard,
    OutboundPost,
)

from ..bindings import (
    AmbiguousBinding,
    BindingNotFound,
    SideTopicConflict,
    SideTopicNotFound,
    ThreadBinding,
)
from ..cards import (
    account_usage_card,
    CardActionError,
    SettingsCardActionError,
    error_card,
    goal_card,
)
from .messages import _send_result_error_code
from ..file_inputs import FileInputError
from ..defaults import DefaultConfigurationError
from ..runtime.contracts import (
    ContextBoundaryCommitFailed,
    GoalNotFound,
    GoalNotMaterialized,
    GoalStateUnknown,
    RuntimeClosed,
    SideCloseFailed,
    SideSessionClosing,
    SideSessionConflict,
    SideSessionNotFound,
    SideStartFailed,
    SkillReferenceError,
    SteerRace,
    TerminalCleanupFailed,
    ThreadCompactStartFailed,
    ThreadCompacting,
    ThreadGoalActive,
    ThreadLifecycleError,
    ThreadNotMaterialized,
    ThreadRunningConfiguration,
    ThreadStopping,
    ThreadOccupied,
    TurnInterruptFailed,
    TurnStartFailed,
)
from ..domain import (
    CardControlIntent,
    CardControlName,
    FeishuScope,
    GoalOperationState,
    ScopeKind,
)
from ..experience import (
    InvalidInteraction,
)
from ..error_messages import describe_error
from ..model_settings import ModelCatalogError
from ..management import (
    NativeThreadMissing,
)
from ..image_inputs import (
    ImageInputError,
)
from ..prompt_projection import (
    PromptProjectionError,
)
from ..projects import ProjectError, UnknownProject
from ..quoted_context import (
    QuotedMessageError,
)
from ..message_history import (
    MessageHistoryError,
)
from ..message_projection import (
    HistoricalMessageError,
)
from ..sdk_gap_adapter import SkillCatalogError
from ..sdk_gap_adapter import GoalControlError
from ..schedules.models import ScheduleError

from .app_values import (
    _CardReplyConversation,
    _CardReplyTarget,
    _reply_failure_notice,
    _message_chat_type,
    _message_id,
)
from .base import (
    CHANNEL_LOGGER_NAME,
    SideTopicCreateFailed,
)
from .base import ChannelState


logger = logging.getLogger(CHANNEL_LOGGER_NAME)


class ChannelReplies(ChannelState):
    """Replies, error reports and Goal management cards shared by every interaction."""

    async def _report_input_error(self, message: Any, error: Exception) -> None:
        """One user-visible failure policy for message, card and scheduled inputs."""
        if isinstance(error, (InvalidInteraction, ScheduleError, DefaultConfigurationError)):
            await self._reply(message, str(error))
        elif isinstance(error, UnknownProject):
            await self._reply(message, f"未知 Project：{error.args[0]}。")
        elif isinstance(error, ProjectError):
            await self._reply(message, str(error))
        elif isinstance(error, BindingNotFound):
            await self._reply(
                message,
                f"当前聊天或话题找不到会话：{error.args[0]}。"
                "发送 /sessions 查看会话短 ID；已归档会话用 /sessions archived 查找。"
                "发送 /help 查看用法。",
            )
        elif isinstance(error, AmbiguousBinding):
            await self._reply(
                message,
                f"会话短 ID 不唯一：{error.args[0]}。"
                "发送 /sessions 查看并选择目标会话，或使用更长的 ID 重试。",
            )
        elif isinstance(error, (
            QuotedMessageError,
            MessageHistoryError,
            HistoricalMessageError,
            ContextBoundaryCommitFailed,
        )):
            await self._reply(message, describe_error(error))
        elif isinstance(error, (ImageInputError, FileInputError)):
            await self._reply(message, str(error))
        elif isinstance(error, PromptProjectionError):
            await self._reply(message, str(error))
        elif isinstance(error, (
            ModelCatalogError,
            NativeThreadMissing,
            RuntimeClosed,
            SkillCatalogError,
            SkillReferenceError,
            GoalControlError,
            GoalNotFound,
            GoalNotMaterialized,
            GoalStateUnknown,
            SideCloseFailed,
            SideSessionClosing,
            SideSessionConflict,
            SideSessionNotFound,
            SideStartFailed,
            SideTopicConflict,
            SideTopicCreateFailed,
            SideTopicNotFound,
            ThreadGoalActive,
            ThreadLifecycleError,
            ThreadCompactStartFailed,
            ThreadCompacting,
            ThreadNotMaterialized,
            ThreadRunningConfiguration,
            ThreadStopping,
            ThreadOccupied,
            SteerRace,
            TerminalCleanupFailed,
            TurnInterruptFailed,
            TurnStartFailed,
        )):
            await self._reply(message, describe_error(error))
        else:
            logger.exception(
                "channel interaction failed",
                extra={"error_type": type(error).__name__},
            )
            await self._reply(
                message,
                f"Codex 请求处理未完成：{describe_error(error)}。"
                "请用 /status 或 /sessions 查看当前状态，再决定是否重试；"
                "本提示不代表正在执行的任务已结束。",
            )

    async def _report_card_action_error(
        self,
        intent: CardControlIntent | None,
        message_id: str,
        callback_chat_id: str,
        error: Exception,
        *,
        scope: FeishuScope | None,
        unexpected: bool = False,
        recovery_command: str = "/sessions",
    ) -> None:
        # Choose the feedback destination once, independently of the exception
        # category. A failed redraw never replays the business operation.
        goal = intent is not None and intent.name in {
            CardControlName.GOAL_REFRESH, CardControlName.GOAL_PAUSE,
            CardControlName.GOAL_RESUME, CardControlName.GOAL_CLEAR,
        }
        settings_scope = error.scope if isinstance(error, SettingsCardActionError) else (
            intent.scope if intent is not None and intent.settings_section is not None else None
        )
        settings_section = error.section if isinstance(error, SettingsCardActionError) else (
            intent.settings_section if intent is not None else None
        )
        detail = str(error) if isinstance(error, CardActionError) else describe_error(error)
        if unexpected:
            command = "/goal" if goal else "/projects" if settings_scope is not None else recovery_command
            detail = f"卡片操作未完成：{detail}。请发送 {command} 检查当前状态。"
        source_id = intent.source_id if intent is not None else message_id
        updated = False
        try:
            if goal:
                assert intent is not None
                updated = await self._update_goal_action_notice(intent, detail)
            elif settings_scope is not None:
                updated = await self._safe_update_card(message_id, self._settings_card(
                    settings_scope, section=settings_section,
                    notice=detail, notice_is_error=True,
                ))
            else:
                side = self._bindings.side_topic_for_message(
                    app_id=self._app_id, chat_id=callback_chat_id,
                    topic_id=None, root_message_id=message_id,
                )
                if side is not None:
                    # The persisted exact root supplies a verified destination
                    # even if decoding this callback could not produce an intent.
                    scope = self._side_scope(side) if side.topic_id is not None else FeishuScope(
                        side.app_id, side.chat_id,
                        ScopeKind.GROUP if side.requires_mention else ScopeKind.DIRECT,
                    )
                    source_id = side.root_message_id
                    updated = await self._update_side_card(side, notice=detail, notice_is_error=True)
                else:
                    updated = await self._safe_update_card(message_id, error_card(detail))
        except Exception:
            logger.exception("failed to prepare card action error feedback")
        if not updated and scope is not None and source_id:
            target = _CardReplyTarget(
                id=source_id, message_id=source_id, chat_id=scope.chat_id,
                conversation=_CardReplyConversation(thread_id=scope.topic_id),
            )
            notice = detail if unexpected else f"❌ 会话操作失败：{detail}"
            try:
                await self._reply(target, notice)
            except Exception:
                logger.exception("failed to send card action error feedback")

    async def _goal_management_card(
        self, binding: ThreadBinding, scope: FeishuScope, *,
        notice: str | None = None, notice_is_error: bool = False,
    ) -> OutboundCard:
        snapshot = await self._runtime.goal_snapshot(binding)
        active = self._runtime.active_goal(binding.id)
        unresolved = active is not None and active.state is GoalOperationState.UNKNOWN
        if snapshot is None and unresolved:
            snapshot = active.persisted
        return goal_card(
            scope=scope, binding_id=binding.id, short_id=binding.short_id,
            project_alias=binding.project_alias, goal=snapshot,
            runtime_state=(
                active.state.value
                if snapshot is not None and active is not None
                and active.state in {GoalOperationState.UNKNOWN, GoalOperationState.EXTERNAL_ACTIVE}
                else None
            ),
            notice=(notice or "Goal 状态未确认；请稍后重新查询。" if unresolved else notice),
            notice_is_error=notice_is_error or unresolved,
        )

    async def _reply_no_current_binding(
        self,
        message: Any,
        scope: FeishuScope,
        *,
        task_not_executed: bool = False,
    ) -> None:
        has_sessions = self._bindings.has_bindings(scope.key)
        has_projects = bool(self._projects.list(enabled_only=True))
        lines = [
            "当前聊天或话题尚未选择会话。"
            if has_sessions
            else "当前聊天或话题还没有会话。"
        ]
        if has_sessions:
            lines.append(
                "发送 /sessions 查看并切换已有会话；"
                "已归档的会话可用 /sessions archived 查找并恢复。"
            )
        if has_projects:
            lines.append(
                ("如需新建会话，" if has_sessions else "")
                + "发送 /new，在卡片中选择项目并创建会话。"
            )
        else:
            lines.append(
                "新建会话需要一个已启用的项目（Codex 工作的目录）。"
                "发送 /projects 创建、登记或启用项目，再发送 /new 创建会话。"
            )
        if task_not_executed:
            lines.append("这条任务尚未执行；准备好会话后，请重新发送刚才的任务。")
        if _message_chat_type(message) == "group":
            lines.append("本群中的每条消息都需要 @机器人，包括命令。")
        lines.append("发送 /help 查看快速开始和全部可用命令。")
        await self._reply(message, "\n".join(lines))

    async def _update_goal_action_notice(
        self, intent: CardControlIntent, message: str,
    ) -> bool:
        if intent.binding_id is None:
            return False
        try:
            binding = self._bindings.get(intent.binding_id)
            if binding.scope_key != intent.scope.key:
                return False
            card = await self._goal_management_card(
                binding, intent.scope, notice=message[:4_000], notice_is_error=True,
            )
            return await self._safe_update_card(intent.source_id, card)
        except Exception:
            logger.exception("failed to refresh Goal management feedback")
            return False

    async def _reply(
        self,
        message: Any,
        content: Any,
        *,
        completion_mention_user_id: str | None = None,
    ) -> None:
        if isinstance(content, str) and completion_mention_user_id is not None:
            content = OutboundPost(
                markdown=content,
                mentions=[Identity(open_id=completion_mention_user_id)],
            )
        result = await self._send_completion_reply(message, content)
        failure_notice = _reply_failure_notice(result)
        if failure_notice is None:
            return
        message_id = _message_id(message)
        logger.warning(
            "reply rejected by Feishu content audit; sending safe failure notice",
            extra={
                "message_id": message_id,
                "error_code": _send_result_error_code(result),
            },
        )
        try:
            # A long reply may already have delivered its first, mentioned
            # chunk before another chunk fails audit. The SDK failure does not
            # prove zero publication, so this recovery must not mention again.
            fallback_result = await self._reply_to_origin(message, failure_notice)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "failed to send safe reply failure notice",
                extra={"message_id": message_id},
            )
            return
        if not fallback_result.success:
            logger.error(
                "failed to send safe reply failure notice",
                extra={
                    "message_id": message_id,
                    "error_code": _send_result_error_code(fallback_result),
                },
            )

    async def _safe_reply_to_card(
        self,
        intent: CardControlIntent,
        content: str,
    ) -> None:
        target = _CardReplyTarget(
            id=intent.source_id,
            message_id=intent.source_id,
            chat_id=intent.scope.chat_id,
            conversation=_CardReplyConversation(thread_id=intent.scope.topic_id),
        )
        try:
            await self._reply(target, content)
        except Exception:
            logger.exception("failed to send card action fallback feedback")

    async def _usage(self, message: Any) -> None:
        try:
            snapshot = await self._runtime.account_rate_limits()
        except Exception as error:
            # Account responses can contain identity and billing details. Keep
            # this control's failure projection independent of backend text.
            logger.warning("account rate limits read failed: %s", type(error).__name__)
            await self._reply(
                message,
                "账号额度暂不可用。请确认服务使用的 Codex 已登录且账号支持额度查询，"
                "或稍后重新发送 /usage。",
            )
            return
        await self._reply(message, account_usage_card(snapshot))
