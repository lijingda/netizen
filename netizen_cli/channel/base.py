"""Shared state and utilities every ChannelApplication layer builds on.

ChannelApplication is split into a linear chain of layer classes (see ``channel_app``).
Each layer may call only itself and the layers below it; mypy enforces this,
because a lower class has no attribute that only a higher class defines."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from lark_channel import (
    SendOpts,
    SendResult,
)

from ..bindings import (
    BindingNotFound,
    BindingStore,
    BindingTurnSettings,
    SideTopicConflict,
    SideTopicRecord,
    ThreadBinding,
)
from ..chat_avatars import ChatAvatarImages
from ..cards import (
    CardActionError,
    SideTopicCardContext,
    activity_step_display,
    decode_button_action,
    decode_card_form,
    fetched_card_topic_id,
    scope_from_fetched_card,
    settings_card,
    side_topic_card,
)
from .messages import _nonempty_field, _object_field, _send_result_error_code, public_chat_kind as _public_chat_kind
from .session_pages import SessionCardPage
from .input_preparation import MessageInputPreparer
from ..file_inputs import file_references, file_free_message
from .question_inputs import CardAnswerOrigin
from .ports import ReplyChannel
from ..cards.chat_target import CHAT_SEARCH_LIMIT, ChatSearchSnapshot
from ..feishu_links import topic_open_url
from .reactions import _REACTION_OPERATION_TIMEOUT_SECONDS, _ReactionController
from .reply_presenter import (
    _ReplyCardPresenter,
)
from .reply_suppression import ReplySuppression
from ..codex_runtime import CodexRuntime
from ..runtime.contracts import (
    ContextWindowUsage,
    NativeThreadMetadata,
    SideSessionConflict,
    SideSessionNotFound,
    ThreadSubscriptionSnapshot,
    ThreadSubscriptionState,
    TurnProgressSnapshot,
)
from ..domain import (
    CardControlIntent,
    ControlIntent,
    FeishuScope,
    MessageContextAnchor,
    MentionContextMode,
    PromptInput,
    ScheduledBindingOrigin,
    ScheduledOrigin,
    SessionAgentOrigin,
    SettingsSection,
    ScopeKind,
)
from ..experience import (
    command_help,
    parse_message,
)
from ..model_settings import TurnModelSettings
from ..management import (
    InstanceManagementService,
)
from ..management.coordination import ScopeCoordinator
from ..management.chat_directory import AvailableChat
from ..git_status import git_branch_status
from ..feishu_links import chat_open_url
from ..image_inputs import (
    ImageReference,
    current_message_image_references,
    normalized_message_type,
)
from ..prompt_projection import (
    MATERIAL_MESSAGE_TYPES,
    MATERIAL_REQUEST,
)
from ..group_access import GroupAccessService
from ..projects import ProjectRegistry
from ..task_text import project_task_text
from ..message_history import (
    MessageHistoryReader,
    MessageHistoryUnavailable,
)

from . import constants
from .app_values import (
    _CardReplyTarget,
    _CardCallbackContext,
    _ScheduledCompletion,
    _RenameNameGeneration,
    _admin_url_is_loopback,
    _is_feishu_card_action_lock,
    _message_chat_type,
    _inbound_root_message_id,
    _message_id,
    _chat_id,
    _sender_id,
    _body_text,
    _channel_bot_open_id,
)


# Every Channel layer logs under the historical module name, which operators and
# tests already filter on; the split into layers is not an operational change.
CHANNEL_LOGGER_NAME = "netizen_cli.channel_app"
logger = logging.getLogger(CHANNEL_LOGGER_NAME)

class SideTopicCreateFailed(RuntimeError):
    pass


def _binding_turn_settings(
    settings: TurnModelSettings | None,
) -> BindingTurnSettings | None:
    if settings is None:
        return None
    return BindingTurnSettings(
        model_id=settings.model_id,
        effort_id=settings.effort_id,
        service_tier_id=settings.service_tier_id,
    )


def _normalized_thread_text(value: str | None, *, max_chars: int) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split())
    if not normalized:
        return None
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 1].rstrip() + "…"


def _markdown_code(value: str) -> str:
    return value.replace("`", "ˋ")


def _normalized_thread_preview(value: str | None, *, max_chars: int) -> str | None:
    # Native previews can end midway through the appended provenance JSON.
    # Strip only recognized tag + kind pairs in this display projection; names
    # and native history remain untouched, as do literal tag mentions.
    if value is not None and (trailer := constants.THREAD_PREVIEW_TRAILER.search(value)):
        value = value[:trailer.start()]
    return _normalized_thread_text(value, max_chars=max_chars)


def _session_title(
    binding: ThreadBinding,
    metadata: NativeThreadMetadata | None,
) -> str:
    if binding.native_thread_id is None:
        return "新会话"
    if metadata is None:
        return "会话信息暂不可用"
    return (
        _normalized_thread_text(
            metadata.name,
            max_chars=constants.SESSION_TITLE_MAX_CHARS,
        )
        or _normalized_thread_preview(
            metadata.preview,
            max_chars=constants.SESSION_TITLE_MAX_CHARS,
        )
        or "未命名会话"
    )


def _session_card_cursor(page: SessionCardPage, scope: FeishuScope) -> str:
    try:
        return page.encode(scope)
    except ValueError:
        raise CardActionError(
            "会话分页信息超过卡片容量限制，暂不能展示；请使用 Admin 会话列表。"
        ) from None


def _thread_metadata_status_lines(
    binding: ThreadBinding,
    metadata: NativeThreadMetadata | None,
) -> tuple[str, str]:
    if binding.native_thread_id is None:
        return (
            "名称：新会话",
            "会话预览：暂无（首条消息后生成）",
        )
    if metadata is None:
        return (
            "名称：暂不可用",
            "会话预览：暂不可用",
        )
    name = _normalized_thread_text(
        metadata.name,
        max_chars=constants.STATUS_THREAD_NAME_MAX_CHARS,
    )
    preview = _normalized_thread_preview(
        metadata.preview,
        max_chars=constants.STATUS_THREAD_PREVIEW_MAX_CHARS,
    )
    return (
        f"名称：{name or '未设置'}",
        f"会话预览：{preview or '暂无'}",
    )


async def _git_branch_status_lines(cwd: Path) -> tuple[str, ...]:
    status = await git_branch_status(cwd)
    if status is None:
        return ()
    return (f"Git Branch：{status}",)


def _context_window_status_line(
    binding: ThreadBinding,
    usage: ContextWindowUsage | None,
    *,
    active_turn: bool,
) -> str:
    if binding.native_thread_id is None:
        return "上下文窗口：暂无（首条消息后生成）"
    if usage is None:
        return "上下文窗口：暂不可用（下次可观测 Turn 完成后更新）"
    label = "上下文窗口（上一轮完成时）" if active_turn else "上下文窗口"
    if usage.context_window_tokens is None:
        return (
            f"{label}：已用 {usage.used_tokens:,} tokens"
            "（窗口大小暂不可用）"
        )
    percent_used = min(
        100.0,
        usage.used_tokens * 100 / usage.context_window_tokens,
    )
    return (
        f"{label}：{usage.used_tokens:,} / "
        f"{usage.context_window_tokens:,} tokens（{percent_used:.1f}% 已用）"
    )


def _thread_subscription_status_line(
    binding: ThreadBinding,
    snapshot: ThreadSubscriptionSnapshot | None,
) -> str:
    if binding.native_thread_id is None:
        return "Netizen 订阅：未建立（会话尚未物化）"
    if snapshot is None:
        return "Netizen 订阅：本进程未订阅（服务重启不会重建空闲计时）"
    if snapshot.state is ThreadSubscriptionState.RELEASED:
        return (
            "Netizen 订阅：本进程已取消；"
            "不表示 App Server writer 已立即释放"
        )
    if snapshot.state is ThreadSubscriptionState.RELEASING:
        return "Netizen 订阅：正在请求取消本进程订阅"
    if snapshot.state is ThreadSubscriptionState.RELEASE_UNKNOWN:
        suffix = _subscription_release_countdown(snapshot.release_in_seconds)
        return f"Netizen 订阅：取消结果未确认；{suffix}"
    if snapshot.state is ThreadSubscriptionState.RELEASE_PENDING:
        suffix = _subscription_release_countdown(snapshot.release_in_seconds)
        return f"Netizen 订阅：本进程已订阅；{suffix}"
    return "Netizen 订阅：本进程已订阅（当前有 Codex 活动）"


def _subscription_release_countdown(seconds: float | None) -> str:
    if seconds is None:
        return "等待下一次空闲检查"
    if seconds < 60:
        return f"约 {max(0, int(seconds + 0.999))} 秒后检查并尝试取消"
    minutes = max(1, int((seconds + 59.999) // 60))
    return f"约 {minutes} 分钟后检查并尝试取消"


def _turn_progress_status_lines(
    progress: TurnProgressSnapshot | None,
) -> tuple[str, ...]:
    if progress is None:
        return ()
    lines = [
        "任务进展",
        f"已接收调整：{progress.steer_count} 次",
    ]
    if not progress.plan_available:
        lines.append("任务清单：暂不可用")
        return tuple(lines)
    if not progress.plan_generated:
        suffix = (
            "（最近一次调整后仍在等待更新）"
            if progress.plan_may_be_stale
            else ""
        )
        lines.append(f"任务清单：Codex 尚未生成{suffix}")
        return tuple(lines)

    title = "任务清单"
    if progress.plan_may_be_stale:
        title += "（可能尚未反映最近一次调整）"
    lines.append(f"{title}：")
    icons = {
        "completed": "✓",
        "inProgress": "→",
        "pending": "○",
    }
    visible = progress.steps[:constants.STATUS_PLAN_MAX_STEPS]
    if not visible:
        lines.append("（当前为空）")
    for item in visible:
        normalized = activity_step_display(item.step)
        icon = icons.get(item.status.value, "○")
        lines.append(f"{icon} {normalized}")
    remaining = len(progress.steps) - len(visible)
    if remaining > 0:
        lines.append(f"… 另有 {remaining} 项未展示")
    return tuple(lines)


class ChannelState:
    """Shared Channel state and reply, card and scope utilities."""

    _app_id: str
    _group_access: GroupAccessService
    _is_instance_admin: Callable[[str, str], bool] | None
    _channel: ReplyChannel
    _chat_avatars: ChatAvatarImages
    _runtime: CodexRuntime
    _bindings: BindingStore
    _projects: ProjectRegistry
    _message_history: MessageHistoryReader | None
    _admin_urls: tuple[str, ...]
    _admin_loopback_only: bool | None
    _instance_root: Path | None
    _input_preparer: MessageInputPreparer
    _management: InstanceManagementService
    _scope_coordinator: ScopeCoordinator
    _scheduled_completions: dict[str, _ScheduledCompletion]
    _rename_name_generations: dict[str, _RenameNameGeneration]
    _reactions: _ReactionController
    _reply_suppression: ReplySuppression
    _progress_cards: _ReplyCardPresenter

    async def close(self) -> None:
        """Close Channel presentation resources; ServiceCore owns management."""
        try:
            generations = tuple(self._rename_name_generations.values())
            for generation in generations:
                generation.invalidated = True
                if generation.task is not None:
                    generation.task.cancel()
            await asyncio.gather(
                *(item.task for item in generations if item.task is not None),
                return_exceptions=True,
            )
        finally:
            self._rename_name_generations.clear()
            try:
                await self._progress_cards.close()
            finally:
                try:
                    await self._reactions.close()
                finally:
                    self._reply_suppression.clear()
                    await self._input_preparer.close()

    async def _binding_cwd(self, binding: ThreadBinding) -> Path:
        # Project registration remains the admission boundary, but its default
        # directory is not the working directory of a materialized Thread.
        binding = self._bindings.get(binding.id)
        if binding.native_thread_id is None and binding.working_cwd is None:
            default_cwd = self._projects.resolve_for_binding(binding.project_alias).cwd
        else:
            default_cwd = Path(self._bindings.get_project(binding.project_alias).cwd)
        return await self._runtime.resolve_binding_cwd(binding.id, default_cwd=default_cwd)

    def _binding_creation_cwd(self, binding: ThreadBinding) -> Path | None:
        """Supply only lazy creation intent; Runtime owns materialized cwd."""
        binding = self._bindings.get(binding.id)
        if binding.native_thread_id is not None:
            self._bindings.get_project(binding.project_alias)
            return None
        if binding.working_cwd is not None:
            self._bindings.get_project(binding.project_alias)
            return Path(binding.working_cwd)
        return self._projects.resolve_for_binding(binding.project_alias).cwd

    async def _reply_to_origin(self, message: Any, content: Any) -> SendResult:
        if isinstance(message, (ScheduledBindingOrigin, SessionAgentOrigin, CardAnswerOrigin, _CardReplyTarget)):
            return await self._channel.reply(message, content, SendOpts(
                receive_id_type="chat_id", reply_to=message.message_id,
                reply_in_thread=message.conversation.thread_id is not None,
                reply_target_gone="fail",
            ))
        if isinstance(message, ScheduledOrigin):
            # A scheduled result belongs to this exact execution topic. The
            # SDK's default target-gone fallback would create a main-chat message.
            return await self._channel.reply(message, content, SendOpts(
                receive_id_type="chat_id", reply_to=message.message_id,
                reply_in_thread=True, reply_target_gone="fail",
            ))
        return await self._channel.reply(message, content)

    @staticmethod
    def _scheduled_message_matches(message: ScheduledOrigin, data: object, message_id: str | None) -> bool:
        return bool(message_id) and (
            _nonempty_field(data, "message_id") == message_id
            and _nonempty_field(data, "chat_id") == message.chat_id
            and _nonempty_field(data, "thread_id") == message.conversation.thread_id
        )

    async def _scheduled_message_confirmed(self, origin: ScheduledOrigin, message_id: str) -> bool:
        response = await self._channel.fetch_message(message_id)
        items = _object_field(_object_field(response, "data"), "items")
        return (
            _object_field(response, "code") == 0
            and isinstance(items, list) and len(items) == 1
            and self._scheduled_message_matches(origin, items[0], message_id)
        )

    async def _record_scheduled_card_delivery(self, origin: ScheduledOrigin, message_id: str, result: SendResult | None) -> None:
        # Updating a card acknowledges content only; fetch its exact destination
        # before declaring the scheduled result delivered. Unknown is not a retry.
        state = "unknown"
        try:
            if result is None:
                return
            code = _send_result_error_code(result)
            raw_code = _object_field(result.raw, "code")
            if not result.success and code is not None and code > 0:
                state = "failed"
            elif result.success and type(raw_code) is int and raw_code == 0:
                async with asyncio.timeout(5):
                    if await self._scheduled_message_confirmed(origin, message_id):
                        state = "sent"
        except Exception:
            logger.warning("scheduled result card destination was not confirmed", extra={"run_id": origin.run_id})
        finally:
            self._record_scheduled_delivery(origin, state)

    def _record_scheduled_delivery(self, origin: ScheduledOrigin, state: str) -> None:
        self._bindings.schedules.set_run(origin.run_id, delivery_state=state)
        completion = self._scheduled_completions.get(origin.run_id)
        if completion is not None:
            completion.delivery_state = state

    async def _scheduled_reply_confirmed(self, message: ScheduledOrigin, result: SendResult) -> bool:

        raw = result.raw
        raw_code = _object_field(raw, "code")
        if type(raw_code) is not int or raw_code != 0:
            return False
        chunks = result.chunk_ids
        if not chunks:
            return self._scheduled_message_matches(message, _object_field(raw, "data"), _nonempty_field(result, "message_id"))
        # The SDK returns only the final chunk's raw response. Confirm every
        # exact chunk instead of treating that last response as proof of all.
        try:
            async with asyncio.timeout(5):
                for chunk_id in chunks:
                    if not isinstance(chunk_id, str) or not chunk_id:
                        return False
                    if not await self._scheduled_message_confirmed(message, chunk_id):
                        return False
        except Exception:
            return False
        return True

    async def _send_completion_reply(self, message: Any, content: Any) -> SendResult:
        scheduled = isinstance(message, ScheduledOrigin) and message.run_id in self._scheduled_completions
        try:
            result = await self._reply_to_origin(message, content)
            if scheduled:
                success = result.success
                # The SDK also returns success=False for transport exceptions.
                # An explicit rejection describes a failed result, not proof
                # that earlier SDK chunks/retries had no side effects.
                code = _send_result_error_code(result)
                state = "failed" if success is False and code is not None and code > 0 else "unknown"
                if success is True:
                    raw = result.raw
                    data = _object_field(raw, "data")
                    if await self._scheduled_reply_confirmed(message, result):
                        state = "sent" if self._scheduled_completions[message.run_id].partial_answers_confirmed else "unknown"
                    else:
                        logger.warning("scheduled reply destination was not confirmed", extra={
                            "run_id": message.run_id,
                            "expected_topic_id": message.conversation.thread_id,
                            "returned_topic_id": _nonempty_field(data, "thread_id"),
                        })
                self._record_scheduled_delivery(message, state)
        except BaseException:
            if scheduled:
                self._record_scheduled_delivery(message, "unknown")
            raise
        return result

    def _side_route(
        self,
        message: Any,
        scope: FeishuScope,
    ) -> SideTopicRecord | None:
        return self._bindings.side_topic_for_message(
            app_id=self._app_id,
            chat_id=scope.chat_id,
            topic_id=scope.topic_id,
            root_message_id=_inbound_root_message_id(message),
        )

    def _parse_current_interaction(
        self, message: Any, scope: FeishuScope,
    ) -> PromptInput | ControlIntent:
        if self._has_current_files(message):
            # Filenames and SDK-generated file labels are material, never
            # command heads or user-authored Skill invocations.
            bot_open_id = _channel_bot_open_id(self._channel)
            message = file_free_message(message, bot_open_id=bot_open_id)
            if (normalized_message_type(message) == "file"
                    or not _body_text(message, bot_open_id=bot_open_id).strip()):
                return PromptInput(scope=scope, source_id=_message_id(message),
                                   sender_id=_sender_id(message), text="")
        if normalized_message_type(message) in MATERIAL_MESSAGE_TYPES:
            # Sharing a card/forward supplies material, not commands authored
            # by its original senders. Do not run either slash or Skill parsing.
            return PromptInput(
                scope=scope,
                source_id=_message_id(message),
                sender_id=_sender_id(message),
                text=MATERIAL_REQUEST,
            )
        return parse_message(
            scope=scope,
            message_id=_message_id(message),
            sender_id=_sender_id(message),
            text=_body_text(message, bot_open_id=_channel_bot_open_id(self._channel)),
            available_capabilities=self._runtime.available_capabilities,
        )

    def _model_task_text(
        self, message: Any, request_text: str, *, command: str | None = None,
    ) -> str:
        bot_open_id = _channel_bot_open_id(self._channel)
        if self._has_current_files(message):
            if normalized_message_type(message) == "file":
                return ""
            message = file_free_message(message, bot_open_id=bot_open_id)
        return project_task_text(
            message, request_text,
            bot_open_id=bot_open_id,
            bot_name=getattr(getattr(self._channel, "bot_identity", None), "name", None),
            command=command,
            literal_slash=(
                command is None
                and _body_text(message, bot_open_id=bot_open_id).strip().startswith("//")
            ),
        )

    def _has_current_files(self, message: Any) -> bool:
        return (self._input_preparer.supports_files
                and bool(file_references(message, source="current_message")))

    def _current_image_references(self, message: Any) -> tuple[ImageReference, ...]:
        # Validate the ordinary-file AST before allowing it through the shared
        # image/media gate. Hidden locale descriptors are never selected.
        self._has_current_files(message)
        return current_message_image_references(message, allow_files=self._input_preparer.supports_files)

    async def _card_chat_snapshot(self, *, query: str) -> ChatSearchSnapshot:
        """Collect one complete bounded result, then let the card own navigation."""
        chats: dict[str, AvailableChat] = {}
        cursor = notice = None
        try:
            async with asyncio.timeout(constants.CARD_CHAT_QUERY_SECONDS):
                for _ in range(constants.CARD_CHAT_QUERY_MAX_PAGES):
                    result = await self._management.query_available_chats(
                        query=query, page_token=cursor, page_size=constants.CARD_CHAT_QUERY_PAGE_SIZE,
                    )
                    chats.update((chat.chat_id, chat) for chat in result.items)
                    if len(chats) > CHAT_SEARCH_LIMIT:
                        raise CardActionError(f"匹配群聊超过 {CHAT_SEARCH_LIMIT} 个，请使用更具体的群名查找，或填写聊天 ID；未展示不完整结果。")
                    notice = result.notice or notice
                    cursor = result.next_page_token
                    if cursor is None:
                        break
                else:
                    raise CardActionError("群聊查找范围过大，请使用更具体的群名；未展示不完整结果。")
        except CardActionError:
            raise
        except TimeoutError:
            raise CardActionError("群聊查找超时，请缩小关键词范围后重试，或填写聊天 ID；未展示不完整结果。") from None
        except Exception:
            raise CardActionError("群聊查找未完成，请重新查找，或填写聊天 ID；未展示不完整结果。") from None
        items = tuple(chats.values())
        avatars = await self._chat_avatars.prepare(items)
        return ChatSearchSnapshot(query, items, avatars, notice)

    async def _card_display_group(self, chat_id: str) -> AvailableChat | None:
        # Presentation only: failure must not make same-chat creation depend
        # on group discovery (P2P chats may also contain ordinary topics).
        try:
            async with asyncio.timeout(2.0):
                return await self._management.validate_available_chat(chat_id)
        except Exception:
            return None

    def _side_scope(self, record: SideTopicRecord) -> FeishuScope:
        if record.topic_id is None:
            raise SideTopicConflict("Side Topic 尚未取得 topic_id")
        return FeishuScope(
            record.app_id,
            record.chat_id,
            ScopeKind.TOPIC,
            record.topic_id,
        )

    async def _side_card_context(
        self, record: SideTopicRecord, *, creator_name: str | None = None,
    ) -> SideTopicCardContext:
        """Optional display facts; never consult the chat's current Binding."""
        title = "原会话信息暂不可用"
        project_alias = None
        project_cwd = None
        source_url = chat_open_url(record.chat_id)
        source_is_topic = False
        can_resume_parent = False
        binding = None
        try:
            binding = self._bindings.get(record.parent_binding_id)
        except BindingNotFound:
            title = "原会话已删除"
        except Exception:
            logger.warning("Side Parent display lookup failed", exc_info=True)
        if binding is not None:
            project_alias = binding.project_alias
            try:
                source = self._bindings.get_scope(binding.scope_key).scope
                if source.app_id != record.app_id or source.chat_id != record.chat_id:
                    raise SideTopicConflict("Side Parent display Scope identity mismatch")
                if source.topic_id is not None:
                    source_url = topic_open_url(source.chat_id, source.topic_id)
                    source_is_topic = True
                can_resume_parent = True
            except Exception:
                logger.warning("Side source location unavailable", exc_info=True)
            try:
                metadata = await self._management.binding_metadata_exact(
                    binding.id,
                    deadline=asyncio.get_running_loop().time() + constants.SIDE_CARD_METADATA_TIMEOUT_SECONDS,
                )
                title = _session_title(binding, metadata)
            except Exception:
                logger.warning("Side Parent title unavailable", exc_info=True)
        try:
            snapshot = self._runtime.side_snapshot(record.id)
            if snapshot.parent_binding_id != record.parent_binding_id:
                raise SideSessionConflict("Side display Parent identity mismatch")
            project_alias = snapshot.project_alias
            project_cwd = str(snapshot.cwd)
        except SideSessionNotFound:
            pass
        except Exception:
            logger.warning("Side project snapshot unavailable", exc_info=True)
        if project_cwd is None and binding is not None:
            try:
                project_cwd = str(await self._binding_cwd(binding))
            except Exception:
                logger.warning("Side project display lookup failed", exc_info=True)
        return SideTopicCardContext(
            parent_title=title,
            project_alias=project_alias,
            project_cwd=project_cwd,
            source_url=source_url,
            source_is_topic=source_is_topic,
            can_resume_parent=can_resume_parent,
            creator_name=_normalized_thread_text(creator_name, max_chars=120),
        )

    async def _update_side_card(
        self,
        record: SideTopicRecord,
        *,
        notice: str | None = None,
        notice_is_error: bool = False,
        context: SideTopicCardContext | None = None,
    ) -> bool:
        if record.root_message_id is None:
            return False
        if context is None:
            context = await self._side_card_context(record)
        for show_creator_profile in (True, False):
            # Recheck after enrichment or fallback so a close/expiry during those
            # waits is reflected here. Already in-flight updates may still race.
            current = self._bindings.get_side_topic(record.id)
            if current.root_message_id is None:
                return False
            scope = (
                self._side_scope(current) if current.topic_id is not None
                else FeishuScope(
                    current.app_id,
                    current.chat_id,
                    ScopeKind.GROUP if current.requires_mention else ScopeKind.DIRECT,
                )
            )
            same_state = current.state is record.state
            card = side_topic_card(
                scope=scope,
                side_id=current.id,
                parent_short_id=current.parent_binding_id[:8],
                creator_id=current.creator_id,
                created_at=current.created_at,
                state=current.state,
                notice=notice if same_state else None,
                notice_is_error=notice_is_error if same_state else False,
                context=context,
                requires_mention=current.requires_mention,
                show_creator_profile=show_creator_profile,
            )
            if await self._safe_update_card(current.root_message_id, card):
                return True
            # Replacing the exact existing message is safe even if an update's
            # result was lost. Never resend the fresh root with different content.
        return False

    def _require_catch_up_message_scope(
        self,
        message: Any,
        scope: FeishuScope,
    ) -> None:
        if (
            scope.kind not in {ScopeKind.GROUP, ScopeKind.TOPIC}
            or _message_chat_type(message) != "group"
        ):
            raise MessageHistoryUnavailable(
                "私聊和私聊话题不支持补充群聊上下文，本条消息未执行。"
            )

    async def _resolve_context_anchor(
        self,
        scope: FeishuScope,
        message_id: str,
    ) -> MessageContextAnchor:
        reader = self._message_history
        if reader is None:
            raise MessageHistoryUnavailable(
                "群聊上下文读取能力尚不可用，本次会话操作未执行。"
            )
        return await reader.resolve_anchor(scope, message_id)

    async def _decode_card_event(self, event: Any, context: _CardCallbackContext) -> CardControlIntent:
        action = getattr(event, "action", None)
        if action is None:
            raise CardActionError("卡片回调缺少 action。")
        message_id = str(getattr(event, "message_id", "") or "")
        chat_id = str(getattr(event, "chat_id", "") or "")
        operator = getattr(event, "operator", None)
        sender_id = str(getattr(operator, "open_id", "") or "")
        tag = str(getattr(action, "tag", "") or "")
        form_value = getattr(action, "form_value", None)
        if form_value is not None:
            fetched = await self._channel.fetch_message(message_id)
            topic_id = fetched_card_topic_id(
                callback_chat_id=chat_id,
                fetched_message=fetched,
            )
            chat_info = (
                None
                if topic_id is not None
                else await self._channel.get_chat_info(chat_id)
            )
            chat_kind = _public_chat_kind(chat_info)
            scope = scope_from_fetched_card(
                app_id=self._app_id,
                callback_chat_id=chat_id,
                fetched_message=fetched,
                chat_type=chat_kind,
            )
            context.scope = scope
            intent = decode_card_form(
                scope=scope,
                message_id=message_id,
                sender_id=sender_id,
                tag=tag,
                form_value=form_value,
                value=getattr(action, "value", None),
            )
            if (
                intent.message_context_mode is MentionContextMode.CATCH_UP
            ):
                if topic_id is not None:
                    chat_kind = _public_chat_kind(
                        await self._channel.get_chat_info(chat_id)
                    )
                if chat_kind == "group":
                    return intent
                raise CardActionError(
                    "私聊和私聊话题不支持补充群聊上下文，本次未执行。"
                )
            return intent
        return decode_button_action(
            app_id=self._app_id,
            message_id=message_id,
            callback_chat_id=chat_id,
            sender_id=sender_id,
            tag=tag,
            value=getattr(action, "value", None),
        )

    def _settings_card(
        self,
        scope: FeishuScope,
        *,
        section: SettingsSection = SettingsSection.PROJECTS,
        notice: str | None = None,
        notice_is_error: bool = False,
    ):
        return settings_card(
            scope=scope,
            projects=self._projects.list(),
            project_root=str(self._projects.project_root),
            section=section,
            notice=notice,
            notice_is_error=notice_is_error,
        )

    def _scope(self, message: Any) -> FeishuScope:
        conversation = getattr(message, "conversation", None)
        topic_id = getattr(conversation, "thread_id", None)
        chat_type = str(
            getattr(message, "chat_type", None)
            or getattr(conversation, "chat_type", "unknown")
        )
        if topic_id:
            kind = ScopeKind.TOPIC
        elif chat_type == "p2p":
            kind = ScopeKind.DIRECT
        else:
            kind = ScopeKind.GROUP
        return FeishuScope(
            app_id=self._app_id,
            chat_id=_chat_id(message),
            kind=kind,
            topic_id=str(topic_id) if topic_id else None,
        )

    async def _safe_add_reaction(self, message: Any, emoji_type: str) -> bool:
        message_id = _message_id(message)
        if not message_id:
            logger.error(
                "failed to add message reaction: missing message ID",
                extra={"emoji_type": emoji_type},
            )
            return False
        try:
            async with asyncio.timeout(_REACTION_OPERATION_TIMEOUT_SECONDS):
                result = await self._channel.add_reaction(message_id, emoji_type)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "failed to add message reaction",
                extra={"message_id": message_id, "emoji_type": emoji_type},
            )
            return False
        if not result.success:
            logger.error(
                "failed to add message reaction: unsuccessful result",
                extra={"message_id": message_id, "emoji_type": emoji_type},
            )
            return False
        return True

    async def _update_card(self, message_id: str, content: Any) -> SendResult:
        return await self._channel.update_card(message_id, content.card)

    async def _safe_update_card(self, message_id: str, content: Any) -> bool:
        if not message_id:
            return False
        for attempt in range(len(constants.CARD_ACTION_LOCK_RETRY_DELAYS_SECONDS) + 1):
            try:
                result = await self._update_card(message_id, content)
            except Exception:
                logger.exception("failed to render card action result")
                return False
            if result.success:
                return True
            if (
                not _is_feishu_card_action_lock(result)
                or attempt == len(constants.CARD_ACTION_LOCK_RETRY_DELAYS_SECONDS)
            ):
                logger.error(
                    "failed to render card action result: unsuccessful update"
                )
                return False
            delay = constants.CARD_ACTION_LOCK_RETRY_DELAYS_SECONDS[attempt]
            logger.warning(
                "transient Feishu card update lock; retrying",
                extra={
                    "message_id": message_id,
                    "retry": attempt + 1,
                    "delay_seconds": delay,
                },
            )
            await asyncio.sleep(delay)
        return False

    def _admin_entry(self) -> str:
        """Present only the successfully bound listener, never configuration guesses."""
        root = (
            f"`{_markdown_code(str(self._instance_root))}`"
            if self._instance_root is not None
            else "未提供"
        )
        lines = [f"实例根目录：{root}"]
        if not self._admin_urls:
            lines.append("当前实例的 Admin Web 管理地址暂不可用。")
            return "\n\n".join(lines)
        lines.extend(("Admin Web 管理地址：", *self._admin_urls))
        loopback_only = self._admin_loopback_only is True or all(
            _admin_url_is_loopback(url) for url in self._admin_urls
        )
        if loopback_only:
            lines.append("以上地址仅供服务器本机访问；远程访问需自行建立隧道。")
        elif len(self._admin_urls) > 1:
            lines.append(
                "请使用可访问的服务器地址；可在本实例 config.yaml 中设置 "
                "adminWeb.accessHost 指定展示地址。"
            )
        lines.append("请使用管理员名单中的飞书账号登录管理页；请勿在聊天中发送登录凭证。")
        return "\n\n".join(lines)

    def _help(self) -> str:
        return command_help(self._runtime.available_capabilities)
