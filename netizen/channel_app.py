"""Feishu product controls over the native Codex runtime."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from lark_channel import MediaSource, OutboundFile, OutboundImage, SendOpts

from .bindings import (
    AmbiguousBinding,
    BindingNotFound,
    BindingSettingsRevisionConflict,
    BindingStore,
    BindingTurnSettings,
    SideTopicConflict,
    SideTopicNotFound,
    SideTopicRecord,
    SideTopicState,
    ThreadBinding,
)
from .cards import (
    ArchivedSessionCardItem,
    CardActionError,
    SettingsCardActionError,
    TURN_FILE_ACTION_VERSION,
    TurnFileCardLimitError,
    archive_binding_card,
    archived_sessions_card,
    binding_configured_card,
    binding_created_card,
    binding_lifecycle_result_card,
    config_card,
    decode_button_action,
    decode_card_form,
    decode_turn_file_action,
    error_card,
    fetched_card_topic_id,
    new_binding_card,
    goal_card,
    is_turn_file_action,
    scope_from_fetched_card,
    delete_binding_card,
    rename_binding_card,
    settings_card,
    side_topic_card,
    turn_files_card,
    turn_files_card_from_manifest,
)
from .codex_runtime import (
    ActiveState,
    CodexRuntime,
    CompactionOutcome,
    ContextWindowUsage,
    ExternalGoalActive,
    GoalNotFound,
    GoalNotMaterialized,
    GoalOutcome,
    GoalStateUnknown,
    GoalOperationState,
    NativeThreadMetadata,
    ReleaseDisposition,
    RuntimeClosed,
    SideCloseFailed,
    SideLifecycleOutcome,
    SideSessionClosing,
    SideSessionConflict,
    SideSessionNotFound,
    SideSessionState,
    SideStartFailed,
    SideTurnOutcome,
    SkillReferenceError,
    SteerRace,
    StopDisposition,
    SubmitDisposition,
    TerminalCleanupFailed,
    ThreadCompactStartFailed,
    ThreadCompacting,
    ThreadGoalActive,
    ThreadArchived,
    ThreadDeleteUnavailable,
    ThreadLifecycleError,
    ThreadNotMaterialized,
    ThreadReleaseError,
    ThreadRunningConfiguration,
    ThreadSubscriptionSnapshot,
    ThreadSubscriptionState,
    ThreadStopping,
    TurnProgressSnapshot,
    TurnInterruptFailed,
    TurnStartFailed,
    TurnOutcome,
)
from .domain import (
    CardControlIntent,
    CardControlName,
    ControlIntent,
    ControlName,
    FeishuScope,
    NativeCapability,
    PromptInput,
    SettingsSection,
    ScopeKind,
    TurnFileActionIntent,
    TurnFileActionName,
)
from .experience import (
    InvalidInteraction,
    command_help,
    parse_message,
    side_command_help,
)
from .model_settings import ModelCatalogError, TurnModelSettings
from .management import (
    CurrentBindingChanged,
    CurrentBindingTarget,
    CurrentSideTarget,
    InstanceManagementService,
    ManagementRuntimePort,
    NoCurrentBinding,
    ScopeCoordinator,
    SideIdentityMismatch,
)
from .image_inputs import (
    ImageInputError,
    ImageInputUnavailable,
    ImageReference,
    compose_multimodal_input,
    current_message_image_references,
    image_references,
    normalized_message_type,
    prepare_images,
)
from .prompt_projection import (
    PromptProjectionError,
    project_current_message,
    render_plain_prompt,
)
from .projects import ProjectError, ProjectRegistry, UnknownProject
from .quoted_context import (
    QuotedMessageError,
    QuotedMessageUnavailable,
    compose_quoted_prompt,
    interactive_quote_visible_text,
    needs_interactive_fallback,
    quoted_message_id,
    validate_quoted_message,
)
from .sdk_gap_adapter import SkillCatalogError
from .sdk_gap_adapter import GoalControlError, GoalStatus
from .skill_references import InvalidSkillReference, parse_skill_references
from .turn_files import (
    TurnFile,
    TurnFileError,
    extract_turn_files,
    has_turn_file_references,
    require_turn_file_path,
)


logger = logging.getLogger(__name__)

_TURN_FILES_WITHOUT_FINAL_RESPONSE = "任务已完成，已生成以下文件。"

_TEXTUAL_CONTENT_TYPES = frozenset({"text", "post"})
_QUOTE_FETCH_TIMEOUT_SECONDS = 10.0
_DONE_REACTION = "DONE"
_ERROR_REACTION = "ERROR"
_INTERRUPTED_REACTION = "CrossMark"
_STEER_REACTION = "OnIt"
_TYPING_REACTION = "Typing"
_THINKING_REACTION = "THINKING"
_THINKING_VISIBLE_SECONDS = 2.0
_THINKING_HIDDEN_SECONDS = 13.0
_REACTION_OPERATION_TIMEOUT_SECONDS = 3.0
_SESSION_TITLE_MAX_CHARS = 48
_STATUS_THREAD_NAME_MAX_CHARS = 120
_STATUS_THREAD_PREVIEW_MAX_CHARS = 240
_STATUS_PLAN_MAX_STEPS = 12
_STATUS_PLAN_STEP_MAX_CHARS = 160
_SIDE_ROOT_UUID_PREFIX = "side-root-"
_SIDE_SEED_UUID_PREFIX = "side-seed-"
_SIDE_INITIAL_QUESTION_MAX_CHARS = 3000
_SIDE_EMPTY_TOPIC_PROMPT = "在本话题发送第一条问题，开始 Side 对话。"
_FEISHU_AT_TAG_START = re.compile(r"<(?=/?at(?:\s|>|/))", re.IGNORECASE)


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
            max_chars=_SESSION_TITLE_MAX_CHARS,
        )
        or _normalized_thread_text(
            metadata.preview,
            max_chars=_SESSION_TITLE_MAX_CHARS,
        )
        or "未命名会话"
    )


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
        max_chars=_STATUS_THREAD_NAME_MAX_CHARS,
    )
    preview = _normalized_thread_text(
        metadata.preview,
        max_chars=_STATUS_THREAD_PREVIEW_MAX_CHARS,
    )
    return (
        f"名称：{name or '未设置'}",
        f"会话预览：{preview or '暂无'}",
    )


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
    return "Netizen 订阅：本进程已订阅（当前有原生活动）"


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
    visible = progress.steps[:_STATUS_PLAN_MAX_STEPS]
    if not visible:
        lines.append("（当前为空）")
    for item in visible:
        normalized = " ".join(item.step.split())
        if len(normalized) > _STATUS_PLAN_STEP_MAX_CHARS:
            normalized = normalized[: _STATUS_PLAN_STEP_MAX_CHARS - 1].rstrip() + "…"
        icon = icons.get(item.status.value, "○")
        lines.append(f"{icon} {normalized}")
    remaining = len(progress.steps) - len(visible)
    if remaining > 0:
        lines.append(f"… 另有 {remaining} 项未展示")
    return tuple(lines)


@dataclass(frozen=True, slots=True)
class GoalCardOrigin:
    message_id: str
    scope: FeishuScope
    binding_id: str
    short_id: str
    project_alias: str


@dataclass(frozen=True, slots=True)
class _CardReplyConversation:
    thread_id: str | None


@dataclass(frozen=True, slots=True)
class _CardReplyTarget:
    id: str
    message_id: str
    chat_id: str
    conversation: _CardReplyConversation


@dataclass(frozen=True, slots=True)
class _SentMessage:
    message_id: str
    chat_id: str
    thread_id: str | None
    root_id: str | None
    parent_id: str | None


class ReplyChannel(Protocol):
    @property
    def bot_identity(self) -> Any: ...

    async def reply(self, message: Any, content: Any, opts: Any = None) -> object: ...

    async def send(self, to: str, content: Any, opts: Any = None) -> object: ...

    async def add_reaction(self, message_id: str, emoji_type: str) -> object: ...

    async def remove_reaction(
        self,
        message_id: str,
        reaction_id: str,
    ) -> object: ...

    async def update_card(self, message_id: str, card: dict[str, Any]) -> object: ...

    async def fetch_message(self, message_id: str) -> dict[str, Any]: ...

    async def fetch_inbound_message(self, message_id: str) -> Any: ...

    async def fetch_quoted_context(self, message_id: str) -> Any: ...

    async def download_resource(
        self,
        file_key: str,
        resource_type: str = "image",
        message_id: str | None = None,
    ) -> bytes | None: ...

    async def get_chat_info(self, chat_id: str) -> Any: ...


@dataclass(slots=True)
class _TurnReactionPulse:
    turn_id: str
    message_id: str
    stopped: asyncio.Event
    typing_reaction_id: str | None = None
    thinking_reaction_id: str | None = None
    task: asyncio.Task[None] | None = None


class _ReactionController:
    """Best-effort, in-memory lifecycle for one exact Turn's reactions."""

    def __init__(
        self,
        channel: ReplyChannel,
        *,
        visible_seconds: float = _THINKING_VISIBLE_SECONDS,
        hidden_seconds: float = _THINKING_HIDDEN_SECONDS,
        operation_timeout_seconds: float = _REACTION_OPERATION_TIMEOUT_SECONDS,
    ) -> None:
        if visible_seconds <= 0 or hidden_seconds <= 0:
            raise ValueError("thinking reaction pulse intervals must be positive")
        if operation_timeout_seconds <= 0:
            raise ValueError("reaction operation timeout must be positive")
        self._channel = channel
        self._visible_seconds = visible_seconds
        self._hidden_seconds = hidden_seconds
        self._operation_timeout_seconds = operation_timeout_seconds
        self._pulses: dict[str, _TurnReactionPulse] = {}
        self._closed = False

    async def start(self, turn_id: str, message_id: str) -> bool:
        if not turn_id or not message_id:
            logger.error(
                "failed to start turn reactions: missing identity",
                extra={"turn_id": turn_id, "message_id": message_id},
            )
            return False
        if self._closed:
            logger.warning(
                "turn reaction controller is closed",
                extra={"turn_id": turn_id, "message_id": message_id},
            )
            return False
        if turn_id in self._pulses:
            await self.stop(turn_id)
        pulse = _TurnReactionPulse(
            turn_id=turn_id,
            message_id=message_id,
            stopped=asyncio.Event(),
        )
        self._pulses[turn_id] = pulse
        reaction_id = await self._add_reaction(pulse, _TYPING_REACTION)
        if self._pulses.get(turn_id) is not pulse:
            if reaction_id is not None:
                await self._remove_reaction(
                    pulse,
                    reaction_id,
                    _TYPING_REACTION,
                )
            return False
        if reaction_id is None:
            self._pulses.pop(turn_id, None)
            return False
        pulse.typing_reaction_id = reaction_id

        reaction_id = await self._add_reaction(pulse, _THINKING_REACTION)
        if self._pulses.get(turn_id) is not pulse:
            if reaction_id is not None:
                await self._remove_reaction(
                    pulse,
                    reaction_id,
                    _THINKING_REACTION,
                )
            return False
        if reaction_id is None:
            # Keep the stable Typing placeholder even when the optional pulse
            # cannot start. Terminal/shutdown cleanup still owns its exact ID.
            return True
        pulse.thinking_reaction_id = reaction_id
        pulse.task = asyncio.create_task(
            self._pulse(pulse),
            name=f"netizen-thinking-{turn_id}",
        )
        return True

    async def freeze(self, turn_id: str) -> None:
        """Stop future pulse operations without removing visible reactions."""

        pulse = self._pulses.get(turn_id)
        if pulse is None:
            return
        pulse.stopped.set()
        await self._wait_for_task(pulse)

    async def stop(self, turn_id: str) -> None:
        pulse = self._pulses.pop(turn_id, None)
        if pulse is None:
            return
        pulse.stopped.set()
        await self._wait_for_task(pulse)
        reaction_id = pulse.thinking_reaction_id
        if reaction_id is not None:
            if await self._remove_reaction(
                pulse,
                reaction_id,
                _THINKING_REACTION,
            ):
                pulse.thinking_reaction_id = None
        reaction_id = pulse.typing_reaction_id
        if reaction_id is not None:
            if await self._remove_reaction(
                pulse,
                reaction_id,
                _TYPING_REACTION,
            ):
                pulse.typing_reaction_id = None

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await asyncio.gather(
            *(self.stop(turn_id) for turn_id in tuple(self._pulses)),
            return_exceptions=False,
        )

    async def _wait_for_task(self, pulse: _TurnReactionPulse) -> None:
        task = pulse.task
        if task is None or task is asyncio.current_task():
            return
        try:
            await task
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "thinking reaction pulse failed while stopping",
                extra={
                    "turn_id": pulse.turn_id,
                    "message_id": pulse.message_id,
                },
            )

    async def _pulse(self, pulse: _TurnReactionPulse) -> None:
        while not pulse.stopped.is_set():
            if await self._wait(pulse.stopped, self._visible_seconds):
                return
            reaction_id = pulse.thinking_reaction_id
            if reaction_id is None:
                return
            if not await self._remove_reaction(
                pulse,
                reaction_id,
                _THINKING_REACTION,
            ):
                # Preserve the exact ID so terminal/shutdown cleanup gets one
                # final best-effort removal attempt without a retry storm.
                return
            pulse.thinking_reaction_id = None
            if await self._wait(pulse.stopped, self._hidden_seconds):
                return
            reaction_id = await self._add_reaction(pulse, _THINKING_REACTION)
            if reaction_id is None:
                return
            pulse.thinking_reaction_id = reaction_id

    @staticmethod
    async def _wait(stopped: asyncio.Event, seconds: float) -> bool:
        try:
            await asyncio.wait_for(stopped.wait(), timeout=seconds)
        except TimeoutError:
            return False
        return True

    async def _add_reaction(
        self,
        pulse: _TurnReactionPulse,
        emoji_type: str,
    ) -> str | None:
        try:
            async with asyncio.timeout(self._operation_timeout_seconds):
                result = await self._channel.add_reaction(
                    pulse.message_id,
                    emoji_type,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "failed to add turn reaction",
                extra={
                    "turn_id": pulse.turn_id,
                    "message_id": pulse.message_id,
                    "emoji_type": emoji_type,
                },
            )
            return None
        if getattr(result, "success", False) is not True:
            logger.error(
                "failed to add turn reaction: unsuccessful result",
                extra={
                    "turn_id": pulse.turn_id,
                    "message_id": pulse.message_id,
                    "emoji_type": emoji_type,
                },
            )
            return None
        raw = getattr(result, "raw", None)
        if raw is None and isinstance(result, dict):
            raw = result
        data = raw.get("data") if isinstance(raw, dict) else None
        reaction_id = data.get("reaction_id") if isinstance(data, dict) else None
        if not isinstance(reaction_id, str) or not reaction_id:
            logger.error(
                "failed to add turn reaction: missing reaction ID",
                extra={
                    "turn_id": pulse.turn_id,
                    "message_id": pulse.message_id,
                    "emoji_type": emoji_type,
                },
            )
            return None
        return reaction_id

    async def _remove_reaction(
        self,
        pulse: _TurnReactionPulse,
        reaction_id: str,
        emoji_type: str,
    ) -> bool:
        try:
            async with asyncio.timeout(self._operation_timeout_seconds):
                result = await self._channel.remove_reaction(
                    pulse.message_id,
                    reaction_id,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "failed to remove turn reaction",
                extra={
                    "turn_id": pulse.turn_id,
                    "message_id": pulse.message_id,
                    "reaction_id": reaction_id,
                    "emoji_type": emoji_type,
                },
            )
            return False
        if getattr(result, "success", False) is not True:
            logger.error(
                "failed to remove turn reaction: unsuccessful result",
                extra={
                    "turn_id": pulse.turn_id,
                    "message_id": pulse.message_id,
                    "reaction_id": reaction_id,
                    "emoji_type": emoji_type,
                },
            )
            return False
        return True


class ChannelApplication:
    def __init__(
        self,
        *,
        app_id: str,
        channel: ReplyChannel,
        runtime: CodexRuntime,
        bindings: BindingStore,
        projects: ProjectRegistry,
        scope_coordinator: ScopeCoordinator | None = None,
        management: InstanceManagementService | None = None,
    ) -> None:
        self._app_id = app_id
        self._channel = channel
        self._runtime = runtime
        self._bindings = bindings
        self._projects = projects
        if management is None:
            self._scope_coordinator = scope_coordinator or ScopeCoordinator()
            self._management = InstanceManagementService(
                bindings=bindings,
                projects=projects,
                runtime=ManagementRuntimePort(runtime),
                scope_coordinator=self._scope_coordinator,
            )
        else:
            if (
                scope_coordinator is not None
                and scope_coordinator is not management.scope_coordinator
            ):
                raise ValueError(
                    "ChannelApplication and management must share one "
                    "ScopeCoordinator"
                )
            self._management = management
            self._scope_coordinator = management.scope_coordinator
        self._reactions = _ReactionController(channel)
        runtime.set_completion_handler(self.handle_completion)

    async def close(self) -> None:
        try:
            await self._reactions.close()
        finally:
            await self._management.close()

    async def refresh_expired_side_cards(
        self,
        records: Sequence[SideTopicRecord],
    ) -> None:
        await asyncio.gather(
            *(
                self._update_side_card(
                    record,
                    notice="服务已重启；ephemeral Side 已过期。",
                )
                for record in records
                if record.root_message_id is not None
            ),
            return_exceptions=True,
        )

    async def handle_message(self, message: Any) -> None:
        scope = self._scope(message)
        sender_id = _sender_id(message)
        try:
            side = self._side_route(message, scope)
            if side is not None:
                if side.requires_mention and not bool(
                    getattr(message, "mentioned_bot", False)
                ):
                    return
                await self._side_message(message, side)
                return

            direct = _message_chat_type(message) == "p2p"
            if not direct and not bool(getattr(message, "mentioned_bot", False)):
                return
            current_images = current_message_image_references(message)
            interaction = parse_message(
                scope=scope,
                message_id=_message_id(message),
                sender_id=sender_id,
                text=_body_text(
                    message,
                    bot_open_id=_channel_bot_open_id(self._channel),
                ),
                available_capabilities=self._runtime.available_capabilities,
            )
            if isinstance(interaction, PromptInput):
                await self._prompt(
                    message,
                    interaction,
                    current_images=current_images,
                )
            else:
                if current_images:
                    raise InvalidInteraction("控制命令不能携带图片，请拆分后重试。")
                await self._control(message, interaction)
        except InvalidInteraction as error:
            await self._reply(message, str(error))
        except UnknownProject as error:
            await self._reply(message, f"未知 Project：{error.args[0]}。")
        except ProjectError as error:
            await self._reply(message, str(error))
        except BindingNotFound as error:
            await self._reply(message, f"当前 Scope 找不到会话：{error.args[0]}。")
        except AmbiguousBinding as error:
            await self._reply(message, f"会话短 ID 不唯一：{error.args[0]}。")
        except QuotedMessageError as error:
            await self._reply(message, str(error))
        except ImageInputError as error:
            await self._reply(message, str(error))
        except PromptProjectionError as error:
            await self._reply(message, str(error))
        except (
            ModelCatalogError,
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
            SteerRace,
            TerminalCleanupFailed,
            TurnInterruptFailed,
            TurnStartFailed,
        ) as error:
            await self._reply(message, str(error))
        except Exception as error:
            logger.exception(
                "channel interaction failed",
                extra={"error_type": type(error).__name__},
            )
            await self._reply(message, "Codex 后端处理失败，请发送一条新消息重试。")

    async def handle_card_action(self, event: Any) -> None:
        action = getattr(event, "action", None)
        if is_turn_file_action(getattr(action, "value", None)):
            await self._handle_turn_file_card_action(event)
            return
        intent: CardControlIntent | None = None
        message_id = str(getattr(event, "message_id", "") or "")
        callback_chat_id = str(getattr(event, "chat_id", "") or "")
        try:
            intent = await self._decode_card_event(event)
            await self._card_control(intent)
        except SettingsCardActionError as error:
            await self._safe_update_card(
                message_id,
                self._settings_card(
                    error.scope,
                    section=error.section,
                    notice=str(error),
                    notice_is_error=True,
                ),
            )
        except CardActionError as error:
            record = self._bindings.side_topic_for_message(
                app_id=self._app_id,
                chat_id=callback_chat_id,
                topic_id=None,
                root_message_id=message_id,
            )
            if record is not None:
                await self._update_side_card(
                    record,
                    notice=str(error),
                    notice_is_error=True,
                )
                return
            await self._safe_update_card(message_id, error_card(str(error)))
        except (
            BindingNotFound,
            GoalControlError,
            GoalNotFound,
            GoalNotMaterialized,
            GoalStateUnknown,
            ModelCatalogError,
            ProjectError,
            RuntimeClosed,
            ThreadCompacting,
            ThreadGoalActive,
            ThreadLifecycleError,
            ThreadRunningConfiguration,
            ThreadStopping,
            TurnStartFailed,
        ) as error:
            card = (
                self._settings_card(
                    intent.scope,
                    section=intent.settings_section,
                    notice=str(error),
                    notice_is_error=True,
                )
                if intent is not None and intent.settings_section is not None
                else error_card(str(error))
            )
            updated = await self._safe_update_card(message_id, card)
            if (
                not updated
                and intent is not None
                and intent.name
                in {
                    CardControlName.CREATE_BINDING,
                    CardControlName.CONFIGURE_BINDING,
                    CardControlName.RENAME_BINDING,
                    CardControlName.ARCHIVE_BINDING,
                    CardControlName.DELETE_BINDING,
                    CardControlName.UNARCHIVE_BINDING,
                }
            ):
                await self._safe_reply_to_card(
                    intent,
                    f"❌ 会话操作失败：{str(error)[:500]}",
                )
        except Exception as error:
            logger.exception(
                "card interaction failed",
                extra={"error_type": type(error).__name__},
            )
            card = (
                self._settings_card(
                    intent.scope,
                    section=intent.settings_section,
                    notice="设置操作失败，请重新发送 /settings。",
                    notice_is_error=True,
                )
                if intent is not None and intent.settings_section is not None
                else error_card(
                    "卡片操作失败，请重新发送原命令。",
                )
            )
            updated = await self._safe_update_card(message_id, card)
            if (
                not updated
                and intent is not None
                and intent.name
                in {
                    CardControlName.CREATE_BINDING,
                    CardControlName.CONFIGURE_BINDING,
                    CardControlName.RENAME_BINDING,
                    CardControlName.ARCHIVE_BINDING,
                    CardControlName.DELETE_BINDING,
                    CardControlName.UNARCHIVE_BINDING,
                }
            ):
                await self._safe_reply_to_card(
                    intent,
                    "❌ 会话操作失败：请重新发送原命令。",
                )

    async def handle_completion(
        self,
        outcome: (
            TurnOutcome
            | CompactionOutcome
            | GoalOutcome
            | SideTurnOutcome
            | SideLifecycleOutcome
        ),
    ) -> None:
        if isinstance(outcome, SideLifecycleOutcome):
            await self._complete_side_lifecycle(outcome)
            return
        if isinstance(outcome, GoalOutcome):
            await self._complete_goal(outcome)
            return
        if isinstance(outcome, CompactionOutcome):
            if outcome.error is not None:
                detail = str(outcome.error).strip() or type(outcome.error).__name__
                await self._reply(
                    outcome.origin,
                    f"会话上下文压缩未完成：{detail[:500]}",
                )
            elif outcome.status == "completed":
                await self._reply(outcome.origin, "会话上下文压缩已完成。")
            else:
                await self._reply(
                    outcome.origin,
                    f"会话上下文压缩以未知状态结束：{outcome.status!r}。",
                )
            return
        if outcome.error is not None:
            terminal_reaction = _ERROR_REACTION
        elif outcome.status == "completed":
            terminal_reaction = _DONE_REACTION
        elif outcome.status == "interrupted":
            terminal_reaction = _INTERRUPTED_REACTION
        else:
            terminal_reaction = _ERROR_REACTION
        await self._reactions.freeze(outcome.turn_id)
        await self._safe_add_reaction(outcome.origin, terminal_reaction)
        await self._reactions.stop(outcome.turn_id)
        if outcome.error is not None:
            detail = str(outcome.error).strip() or type(outcome.error).__name__
            await self._reply(outcome.origin, f"任务未完成：{detail[:500]}")
            return
        if outcome.status == "interrupted":
            if outcome.background_cleanup_requested:
                await self._reply(
                    outcome.origin,
                    "Codex Turn 已中断；已请求清理该 Thread 中已登记的后台终端。"
                    "前台工具进程不受此接口保证，可能仍在运行。",
                )
            else:
                await self._reply(
                    outcome.origin,
                    "Codex Turn 已被外部中断；本服务未请求清理已登记的后台终端。"
                    "前台工具进程可能仍在运行。",
                )
            return
        if outcome.status != "completed":
            detail = outcome.final_response or f"Codex Turn 状态为 {outcome.status!r}。"
            await self._reply(outcome.origin, f"任务未完成：{detail[:500]}")
            return
        if isinstance(outcome, TurnOutcome):
            await self._complete_turn_with_files(outcome)
            return
        await self._reply(
            outcome.origin,
            outcome.final_response or "任务已结束，未产生文本回复。",
        )

    async def _complete_turn_with_files(self, outcome: TurnOutcome) -> None:
        final_response = outcome.final_response or "任务已结束，未产生文本回复。"
        items = tuple(getattr(outcome.result, "items", ()))
        if not has_turn_file_references(items, turn_diff=outcome.turn_diff):
            await self._reply(outcome.origin, final_response)
            return
        try:
            binding = self._bindings.get(outcome.binding_id)
            scope = self._scope(outcome.origin)
            if (
                binding.scope_key != scope.key
                or binding.native_thread_id != outcome.thread_id
            ):
                raise TurnFileError("本轮文件与完成消息的会话身份不一致。")
            project = self._projects.resolve_for_binding(binding.project_alias)
            files = extract_turn_files(
                items,
                project.cwd,
                turn_diff=outcome.turn_diff,
            )
            if not files:
                await self._reply(outcome.origin, final_response)
                return
            card = turn_files_card(
                scope=scope,
                binding_id=binding.id,
                turn_id=outcome.turn_id,
                final_response=(
                    outcome.final_response or _TURN_FILES_WITHOUT_FINAL_RESPONSE
                ),
                files=files,
            )
            result = await self._channel.reply(outcome.origin, card)
            if getattr(result, "success", True) is False:
                raise TurnFileError("飞书未确认本轮文件卡片发送成功。")
        except asyncio.CancelledError:
            raise
        except TurnFileCardLimitError as error:
            logger.warning(
                "completed Turn file manifest exceeds card limit",
                extra={
                    "binding_id": outcome.binding_id,
                    "turn_id": outcome.turn_id,
                },
            )
            await self._reply(
                outcome.origin,
                f"{final_response}\n\n⚠️ 本轮文件卡片未生成：{error}",
            )
        except Exception:
            logger.exception(
                "failed to deliver completed Turn file card",
                extra={
                    "binding_id": outcome.binding_id,
                    "turn_id": outcome.turn_id,
                },
            )
            await self._reply(outcome.origin, final_response)

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
                SideTopicState.CLOSED: "Side 已结束并释放原生订阅。",
                SideTopicState.EXPIRED: "Side 已因空闲超时或服务重启而过期。",
                SideTopicState.FAILED: "Side 创建失败，已释放可确认的原生资源。",
            }.get(record.state)
            await self._update_side_card(record, notice=notice)
            return
        detail = str(outcome.error).strip() or type(outcome.error).__name__
        await self._update_side_card(
            record,
            notice=f"Side 清理尚未确认，可再次结束重试：{detail[:300]}",
            notice_is_error=True,
        )

    async def _complete_goal(self, outcome: GoalOutcome) -> None:
        status = outcome.goal.status.value if outcome.goal is not None else "unknown"
        if outcome.error is not None:
            detail = str(outcome.error).strip() or type(outcome.error).__name__
            message = f"Goal 未能确认终态：{detail[:500]}"
            error = True
        elif outcome.goal is not None and outcome.goal.status is GoalStatus.PAUSED:
            message = (
                "Goal 已暂停；已请求清理该 Thread 中已登记的后台终端。"
                "前台工具进程不受此接口保证，可能仍在运行。"
            )
            error = False
        else:
            message = (
                outcome.final_response
                or f"Goal 已进入 {status}，未产生文本回复。"
            )
            error = False
        if isinstance(outcome.origin, GoalCardOrigin):
            await self._safe_update_card(
                outcome.origin.message_id,
                goal_card(
                    scope=outcome.origin.scope,
                    binding_id=outcome.origin.binding_id,
                    short_id=outcome.origin.short_id,
                    project_alias=outcome.origin.project_alias,
                    goal=outcome.goal,
                    runtime_state=(
                        f"goal-{status}" if outcome.goal is not None else None
                    ),
                    notice=message,
                    notice_is_error=error,
                ),
            )
            return
        await self._reply(outcome.origin, message)

    async def _prompt(
        self,
        message: Any,
        prompt: PromptInput,
        *,
        current_images: tuple[ImageReference, ...] = (),
    ) -> None:
        binding = self._bindings.active_binding(prompt.scope.key)
        if binding is None:
            await self._reply(
                message,
                "当前聊天或话题还没有会话，请先发送 /new [project|none]。",
            )
            return
        project = self._projects.resolve_for_binding(binding.project_alias)
        target_id = quoted_message_id(message)
        admission = await self._runtime.capture_submission_admission(binding.id)
        input_value = await self._compose_prompt_input(
            source_message=message,
            source_id=prompt.source_id,
            sender_id=prompt.sender_id,
            quoted_target_id=target_id,
            current_text=prompt.text,
            current_images=current_images,
        )

        submit_kwargs: dict[str, Any] = dict(
            binding=binding,
            cwd=project.cwd,
            input=input_value,
            owner_id=prompt.sender_id,
            origin=message,
            skill_names=prompt.skill_names,
        )
        submit_kwargs["admission"] = admission
        try:
            submission = await self._runtime.submit(**submit_kwargs)
        finally:
            # The native RPC has consumed the input (or owns its in-flight
            # serialization). Do not retain a second large data-URL reference
            # while the reaction receipt below waits on Feishu I/O.
            submit_kwargs.pop("input", None)
            input_value = None
        if submission.disposition is SubmitDisposition.STEERED:
            if not await self._safe_add_reaction(message, _STEER_REACTION):
                await self._reply(message, "已接收调整。")
            return

        release = submission.release_receipt_attempt
        assert release is not None
        try:
            await self._reactions.start(
                submission.turn_id,
                _message_id(message),
            )
        finally:
            release()

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

    async def _side_message(
        self,
        message: Any,
        record: SideTopicRecord,
    ) -> None:
        if record.state is SideTopicState.CREATING:
            interaction = parse_message(
                scope=self._scope(message),
                message_id=_message_id(message),
                sender_id=_sender_id(message),
                text=_body_text(
                    message,
                    bot_open_id=_channel_bot_open_id(self._channel),
                ),
                available_capabilities=self._runtime.available_capabilities,
            )
            if (
                isinstance(interaction, ControlIntent)
                and interaction.name is ControlName.SIDE
                and interaction.arguments
                and interaction.arguments[0].strip().lower() == "close"
            ):
                if current_message_image_references(message):
                    raise InvalidInteraction(
                        "控制命令不能携带图片，请拆分后重试。"
                    )
                await self._close_creating_side_from_message(message, record)
                return
            await self._reply(
                message,
                "这个 Side 仍在创建，或创建结果尚未确认；"
                "可发送 /side close 重试清理。",
            )
            return
        if record.state.terminal:
            labels = {
                SideTopicState.CLOSED: "已结束",
                SideTopicState.EXPIRED: "已过期（服务重启或空闲超时）",
                SideTopicState.FAILED: "创建失败",
            }
            await self._reply(
                message,
                f"这个 Side {labels[record.state]}，不会转成普通会话；"
                "请回到 Parent 会话重新发送 /side。",
            )
            return
        if record.topic_id is None or record.root_message_id is None:
            raise SideTopicConflict("open Side 缺少话题或根消息标识")
        try:
            self._runtime.side_snapshot(record.id)
        except SideSessionNotFound:
            await self._report_missing_side_session(message, record)
            return

        current_images = current_message_image_references(message)
        interaction = parse_message(
            scope=self._side_scope(record),
            message_id=_message_id(message),
            sender_id=_sender_id(message),
            text=_body_text(
                message,
                bot_open_id=_channel_bot_open_id(self._channel),
            ),
            available_capabilities=self._runtime.available_capabilities,
        )
        if isinstance(interaction, PromptInput):
            target_id = quoted_message_id(message)
            await self._side_prompt(
                source_message=message,
                prompt=interaction,
                side_id=record.id,
                reply_origin=message,
                quoted_target_id=target_id,
                current_images=current_images,
            )
            return
        if current_images:
            raise InvalidInteraction("控制命令不能携带图片，请拆分后重试。")
        await self._side_control(message, interaction, record)

    async def _side_prompt(
        self,
        source_message: Any,
        prompt: PromptInput,
        *,
        side_id: str,
        reply_origin: Any,
        quoted_target_id: str | None,
        current_images: tuple[ImageReference, ...] = (),
    ) -> None:
        admission = await self._runtime.capture_side_submission_admission(side_id)
        input_value = await self._compose_prompt_input(
            source_message=source_message,
            source_id=prompt.source_id,
            sender_id=prompt.sender_id,
            quoted_target_id=quoted_target_id,
            current_text=prompt.text,
            current_images=current_images,
        )
        submit_kwargs: dict[str, Any] = dict(
            side_id=side_id,
            input=input_value,
            owner_id=prompt.sender_id,
            origin=reply_origin,
            admission=admission,
            skill_names=prompt.skill_names,
        )
        try:
            submission = await self._runtime.submit_side(**submit_kwargs)
        finally:
            submit_kwargs.pop("input", None)
            input_value = None
        if submission.disposition is SubmitDisposition.STEERED:
            if not await self._safe_add_reaction(reply_origin, _STEER_REACTION):
                await self._reply(reply_origin, "已接收 Side 调整。")
            return
        release = submission.release_receipt_attempt
        assert release is not None
        try:
            await self._reactions.start(
                submission.turn_id,
                _message_id(reply_origin),
            )
        finally:
            release()

    async def _side_control(
        self,
        message: Any,
        intent: ControlIntent,
        record: SideTopicRecord,
    ) -> None:
        if intent.name in {ControlName.MENU, ControlName.HELP}:
            await self._reply(
                message,
                side_command_help(requires_mention=record.requires_mention),
            )
            return
        if intent.name is ControlName.STATUS:
            snapshot = self._runtime.side_snapshot(record.id)
            turn_state = (
                snapshot.turn_state.value
                if snapshot.turn_state is not None
                else "idle"
            )
            await self._reply(
                message,
                "当前 Side\n"
                f"Side：{record.short_id}\n"
                f"Parent 会话：{record.parent_binding_id[:8]}\n"
                f"状态：{snapshot.state.value}\n"
                f"当前 Turn：{turn_state}\n"
                "空闲 2 小时后自动结束；运行中的 Turn 不计入空闲时间。",
            )
            return
        if intent.name is ControlName.STOP:
            snapshot = self._runtime.side_snapshot(record.id)
            if snapshot.turn_id is None:
                await self._reply(message, "当前 Side 没有正在执行的 Turn。")
                return
            acknowledged = False

            async def acknowledge() -> None:
                nonlocal acknowledged
                await self._reply(
                    message,
                    "正在中断当前 Side Turn；Side 话题仍会保留。",
                )
                acknowledged = True

            disposition = await self._runtime.stop_side(
                record.id,
                acknowledge=acknowledge,
            )
            if disposition is StopDisposition.NOT_RUNNING and not acknowledged:
                await self._reply(message, "当前 Side Turn 恰好已经结束。")
            elif disposition is StopDisposition.STOPPING and not acknowledged:
                await self._reply(
                    message,
                    "当前 Side 正在结束；如清理曾失败，可再次发送 /side close。",
                )
            return
        if intent.name is ControlName.SIDE:
            argument = intent.arguments[0].strip().lower() if intent.arguments else ""
            if argument != "close":
                raise InvalidInteraction(
                    "Side 中不能再创建嵌套 Side；使用 /side close 结束当前 Side。"
                )
            await self._close_side_from_message(message, record)
            return
        if intent.name is ControlName.RELEASE:
            raise InvalidInteraction(
                "Side 中不开放 /release；请使用 /side close 结束并取消 Side 订阅。"
            )
        raise InvalidInteraction(
            "该命令在 Side 中不可用。发送 /help 查看 Side 支持的操作。"
        )

    async def _close_side_from_message(
        self,
        message: Any,
        record: SideTopicRecord,
    ) -> None:
        try:
            closed = await self._management.close_side(
                target=CurrentSideTarget(
                    side_id=record.id,
                    app_id=record.app_id,
                    chat_id=record.chat_id,
                    topic_id=record.topic_id,
                    root_message_id=record.root_message_id,
                )
            )
        except SideIdentityMismatch as error:
            raise InvalidInteraction("Side 身份已变化，本次未执行。") from error
        except SideCloseFailed as error:
            current = self._bindings.get_side_topic(record.id)
            await self._update_side_card(
                current,
                notice=str(error),
                notice_is_error=True,
            )
            raise
        if closed.missing_runtime_session:
            await self._report_missing_side_session(message, closed.record)
            return
        outcome = closed.outcome
        if outcome is None:
            await self._reply(message, "Side 已经结束。")
            return
        await self._reply(
            message,
            (
                "Side 已结束，并已请求清理已登记的后台终端。"
                if outcome.state is SideTopicState.CLOSED
                else f"Side 已进入 {outcome.state.value}。"
            ),
        )

    async def _close_creating_side_from_message(
        self,
        message: Any,
        record: SideTopicRecord,
    ) -> None:
        try:
            closed = await self._management.close_side(
                target=CurrentSideTarget(
                    side_id=record.id,
                    app_id=record.app_id,
                    chat_id=record.chat_id,
                    topic_id=record.topic_id,
                    root_message_id=record.root_message_id,
                )
            )
        except SideIdentityMismatch as error:
            raise InvalidInteraction("Side 身份已变化，本次未执行。") from error
        except SideCloseFailed as error:
            current = self._bindings.get_side_topic(record.id)
            await self._update_side_card(
                current,
                notice=str(error),
                notice_is_error=True,
            )
            raise
        if closed.missing_runtime_session:
            await self._update_side_card(
                closed.record,
                notice="Side 创建未完成，且没有可重试的原生 Session。",
                notice_is_error=True,
            )
            await self._reply(
                message,
                f"Side 当前状态为 {closed.record.state.value}，不会转成普通会话。",
            )
            return
        await self._reply(
            message,
            "未完成的 Side 已清理并保留失败墓碑，不会转成普通会话。",
        )

    async def _report_missing_side_session(
        self,
        message: Any,
        record: SideTopicRecord,
    ) -> None:
        current = self._bindings.get_side_topic(record.id)
        if current.state is SideTopicState.OPEN:
            try:
                current = self._bindings.transition_side_topic(
                    current.id,
                    SideTopicState.EXPIRED,
                )
            except SideTopicConflict:
                current = self._bindings.get_side_topic(current.id)
        if current.state is SideTopicState.EXPIRED:
            notice = "服务内已没有对应 Side Session；该话题已过期。"
            reply = (
                "这个 Side 已随服务重启失效；"
                "请回到 Parent 会话重新发送 /side。"
            )
        else:
            notice = f"Side 已进入 {current.state.value}。"
            reply = (
                f"这个 Side 已进入 {current.state.value}，不会转成普通会话。"
            )
        await self._update_side_card(current, notice=notice)
        await self._reply(message, reply)

    async def _create_side(
        self,
        message: Any,
        intent: ControlIntent,
    ) -> None:
        if intent.arguments and intent.arguments[0].strip().lower() == "close":
            await self._reply(
                message,
                "当前不在 Side 话题中；请在要结束的 Side 话题里发送 /side close。",
            )
            return
        initial_text = intent.arguments[0].strip() if intent.arguments else None
        initial_skill_names: tuple[str, ...] = ()
        if initial_text is not None:
            try:
                initial_skill_names = parse_skill_references(initial_text)
            except InvalidSkillReference as error:
                raise InvalidInteraction(str(error)) from error
            if (
                initial_skill_names
                and NativeCapability.SKILLS
                not in self._runtime.available_capabilities
            ):
                raise InvalidInteraction(
                    "当前原生 Skills discovery 不可用，$skill 引用未执行。"
                )
        source_message_id = _message_id(message)
        requires_mention = _message_chat_type(message) != "p2p"
        existing: SideTopicRecord | None = None
        binding: ThreadBinding | None = None
        async with self._scope_coordinator.hold(intent.scope.key):
            existing = self._bindings.side_topic_for_source(
                app_id=self._app_id,
                source_message_id=source_message_id,
            )
            if existing is not None and (
                existing.chat_id != intent.scope.chat_id
                or existing.creator_id != intent.sender_id
                or existing.requires_mention is not requires_mention
            ):
                raise SideTopicConflict(
                    "Side source message is already reserved with different identity"
                )
            if existing is None:
                binding = self._bindings.active_binding(intent.scope.key)
                if binding is not None:
                    existing = self._bindings.create_side_topic(
                        app_id=self._app_id,
                        chat_id=intent.scope.chat_id,
                        source_message_id=source_message_id,
                        parent_binding_id=binding.id,
                        creator_id=intent.sender_id,
                        requires_mention=requires_mention,
                    )
        if existing is None or binding is None:
            if existing is not None:
                await self._reply_existing_side(message, existing)
                return
            await self._reply(
                message,
                "当前聊天或话题还没有会话，请先发送 /new [project|none]。",
            )
            return
        record = existing
        root: _SentMessage | None = None
        topic_id: str | None = None
        origin: _CardReplyTarget | None = None
        try:
            project = self._projects.resolve_for_binding(binding.project_alias)
            await self._runtime.create_side(
                side_id=record.id,
                binding=binding,
                cwd=project.cwd,
                creator_id=intent.sender_id,
            )
            root_scope = FeishuScope(
                self._app_id,
                record.chat_id,
                ScopeKind.GROUP if record.requires_mention else ScopeKind.DIRECT,
            )
            root_card = side_topic_card(
                scope=root_scope,
                side_id=record.id,
                parent_short_id=binding.short_id,
                creator_id=record.creator_id,
                created_at=record.created_at,
                state=SideTopicState.CREATING,
            )
            root = await self._send_side_message(
                record.chat_id,
                root_card,
                SendOpts(
                    receive_id_type="chat_id",
                    uuid=_side_send_uuid(_SIDE_ROOT_UUID_PREFIX, record.id),
                ),
            )
            record = self._bindings.set_side_topic_root(record.id, root.message_id)
            if (
                root.parent_id is not None
                or root.root_id not in {None, root.message_id}
            ):
                raise SideTopicCreateFailed(
                    "飞书返回的 Side 根消息不是新话题根消息，本次创建已停止。"
                )
            if root.thread_id is not None:
                topic_id = root.thread_id
                origin = _side_reply_target(root, topic_id)
            if root.thread_id is None or initial_text is not None:
                seed_content = (
                    _side_initial_question_echo(initial_text)
                    if initial_text is not None
                    else _SIDE_EMPTY_TOPIC_PROMPT
                )
                seed = await self._send_side_message(
                    record.chat_id,
                    seed_content,
                    SendOpts(
                        receive_id_type="chat_id",
                        reply_to=root.message_id,
                        reply_in_thread=True,
                        reply_target_gone="fail",
                        uuid=_side_send_uuid(_SIDE_SEED_UUID_PREFIX, record.id),
                    ),
                )
                if (
                    seed.thread_id is None
                    or seed.root_id != root.message_id
                    or seed.parent_id != root.message_id
                    or (
                        root.thread_id is not None
                        and seed.thread_id != root.thread_id
                    )
                ):
                    raise SideTopicCreateFailed(
                        "飞书没有确认 Side 首轮消息与新话题的关系，本次创建已停止。"
                    )
                topic_id = seed.thread_id
                origin = _side_reply_target(seed, topic_id)
            if intent.scope.topic_id == topic_id:
                raise SideTopicCreateFailed(
                    "飞书把 Side 留在了原话题中，未创建同级话题。"
                )
            record = self._bindings.set_side_topic_topic(record.id, topic_id)
            await self._runtime.attach_side_topic(
                side_id=record.id,
                topic_id=topic_id,
                root_message_id=root.message_id,
            )
            record = self._bindings.open_side_topic(record.id, topic_id)
        except BaseException as error:
            await self._compensate_side_creation(record, error)
            raise

        assert root is not None
        await self._update_side_card(
            record,
            notice=(
                _SIDE_EMPTY_TOPIC_PROMPT
                if initial_text is None and root.thread_id is not None
                else None
            ),
        )
        if initial_text is not None:
            assert origin is not None
            await self._start_initial_side_prompt(
                record=record,
                source_message=message,
                reply_origin=origin,
                text=initial_text,
                sender_id=intent.sender_id,
                skill_names=initial_skill_names,
            )

    async def _reply_existing_side(
        self,
        message: Any,
        record: SideTopicRecord,
    ) -> None:
        if record.state is SideTopicState.OPEN:
            return
        elif record.state is SideTopicState.CREATING:
            await self._reply(
                message,
                "这条 /side 已在创建中，或创建结果尚未确认；未重复 fork。",
            )
        else:
            await self._reply(
                message,
                f"这条 /side 已处理过，当前状态为 {record.state.value}；"
                "为避免重复副作用，不会再次创建。",
            )

    async def _start_initial_side_prompt(
        self,
        *,
        record: SideTopicRecord,
        source_message: Any,
        reply_origin: _CardReplyTarget,
        text: str,
        sender_id: str,
        skill_names: tuple[str, ...],
    ) -> None:
        try:
            prompt = PromptInput(
                scope=self._side_scope(record),
                source_id=_message_id(source_message),
                sender_id=sender_id,
                text=text,
                skill_names=skill_names,
            )
            await self._side_prompt(
                source_message=source_message,
                prompt=prompt,
                side_id=record.id,
                reply_origin=reply_origin,
                quoted_target_id=None,
            )
        except InvalidInteraction as error:
            await self._reply(reply_origin, str(error))
        except PromptProjectionError as error:
            await self._update_side_card(
                self._bindings.get_side_topic(record.id),
                notice=str(error),
                notice_is_error=True,
            )
            await self._reply(reply_origin, str(error))
        except (
            RuntimeClosed,
            SideSessionClosing,
            SideStartFailed,
        ) as error:
            await self._update_side_card(
                self._bindings.get_side_topic(record.id),
                notice=str(error),
                notice_is_error=True,
            )
            await self._reply(reply_origin, str(error))
        except Exception as error:
            logger.exception(
                "initial Side prompt failed",
                extra={"side_id": record.id, "error_type": type(error).__name__},
            )
            await self._reply(
                reply_origin,
                "Side 已创建，但首轮问题未执行；请在本话题重新发送。",
            )

    async def _compensate_side_creation(
        self,
        record: SideTopicRecord,
        cause: BaseException,
    ) -> None:
        try:
            snapshot = self._runtime.side_snapshot(record.id)
        except SideSessionNotFound:
            try:
                self._bindings.transition_side_topic(
                    record.id,
                    SideTopicState.FAILED,
                )
            except (SideTopicConflict, SideTopicNotFound):
                pass
        else:
            if snapshot.state is SideSessionState.CLOSING:
                logger.error(
                    "Side creation compensation remains closing",
                    extra={"side_id": record.id},
                )
            else:
                try:
                    await self._runtime.close_side(
                        record.id,
                        state=SideTopicState.FAILED,
                    )
                except Exception:
                    logger.exception(
                        "Side creation compensation remains unconfirmed",
                        extra={"side_id": record.id},
                    )
        try:
            current = self._bindings.get_side_topic(record.id)
        except SideTopicNotFound:
            return
        if current.root_message_id is not None:
            detail = str(cause).strip() or type(cause).__name__
            await self._update_side_card(
                current,
                notice=f"Side 未创建完成：{detail[:300]}",
                notice_is_error=True,
            )

    async def _send_side_message(
        self,
        chat_id: str,
        content: Any,
        opts: SendOpts,
    ) -> _SentMessage:
        if not opts.uuid:
            raise SideTopicCreateFailed("Side 话题消息缺少确定性发送 UUID。")
        unknowns: list[BaseException] = []
        for attempt in range(2):
            try:
                result = await self._channel.send(chat_id, content, opts)
            except Exception as error:
                unknowns.append(error)
                if attempt == 0:
                    continue
                raise SideTopicCreateFailed(
                    "飞书 Side 话题消息发送结果未确认；同 UUID 对账重试也失败。"
                ) from BaseExceptionGroup(
                    "Side message send attempts failed",
                    unknowns,
                )
            if _retryable_send_result(result):
                unknowns.append(
                    SideTopicCreateFailed(
                        "飞书返回了可重试的 Side 消息发送结果。"
                    )
                )
                if attempt == 0:
                    continue
                raise SideTopicCreateFailed(
                    "飞书 Side 话题消息发送结果未确认；"
                    "同 UUID 对账重试仍返回可重试失败。"
                ) from BaseExceptionGroup(
                    "Side message send attempts were retryable",
                    unknowns,
                )
            return _validated_sent_message(result, expected_chat_id=chat_id)
        raise AssertionError("unreachable Side message attempt budget")

    def _side_scope(self, record: SideTopicRecord) -> FeishuScope:
        if record.topic_id is None:
            raise SideTopicConflict("Side Topic 尚未取得 topic_id")
        return FeishuScope(
            record.app_id,
            record.chat_id,
            ScopeKind.TOPIC,
            record.topic_id,
        )

    async def _update_side_card(
        self,
        record: SideTopicRecord,
        *,
        notice: str | None = None,
        notice_is_error: bool = False,
    ) -> bool:
        if record.root_message_id is None:
            return False
        if record.topic_id is not None:
            scope = self._side_scope(record)
        else:
            scope = FeishuScope(
                record.app_id,
                record.chat_id,
                ScopeKind.GROUP if record.requires_mention else ScopeKind.DIRECT,
            )
        try:
            parent_short_id = self._bindings.get(record.parent_binding_id).short_id
        except BindingNotFound:
            parent_short_id = record.parent_binding_id[:8]
        return await self._safe_update_card(
            record.root_message_id,
            side_topic_card(
                scope=scope,
                side_id=record.id,
                parent_short_id=parent_short_id,
                creator_id=record.creator_id,
                created_at=record.created_at,
                state=record.state,
                notice=notice,
                notice_is_error=notice_is_error,
            ),
        )

    async def _compose_prompt_input(
        self,
        *,
        source_message: Any,
        source_id: str,
        sender_id: str,
        quoted_target_id: str | None,
        current_text: str,
        current_images: tuple[ImageReference, ...],
    ) -> Any:
        try:
            current = project_current_message(
                source_message,
                expected_message_id=source_id,
                expected_sender_id=sender_id,
                message_type=normalized_message_type(source_message),
                content_fidelity=(
                    "full_multimodal" if current_images else "full_text"
                ),
                request_text=current_text,
            )
            quoted = None
            fallback_text = None
            quoted_images: tuple[ImageReference, ...] = ()
            if quoted_target_id is not None:
                async with asyncio.timeout(_QUOTE_FETCH_TIMEOUT_SECONDS):
                    quoted = await self._channel.fetch_inbound_message(
                        quoted_target_id
                    )
                if quoted is None:
                    raise QuotedMessageUnavailable(
                        "无法读取被引用的消息；它可能已撤回，"
                        "或应用缺少消息读取权限。本条消息未执行。"
                    )
                validate_quoted_message(
                    quoted,
                    expected_message_id=quoted_target_id,
                    expected_chat_id=str(source_message.conversation.chat_id),
                )
                quoted_images = image_references(
                    quoted,
                    source="quoted_message",
                )

                if needs_interactive_fallback(quoted):
                    # This is a separate SDK request. Give each network operation
                    # its own bounded timeout instead of turning two healthy calls
                    # into a false shared-budget timeout.
                    async with asyncio.timeout(_QUOTE_FETCH_TIMEOUT_SECONDS):
                        fallback = await self._channel.fetch_quoted_context(
                            quoted_target_id
                        )
                    if (
                        fallback is None
                        or getattr(fallback, "message_id", None)
                        != quoted_target_id
                        or getattr(fallback, "content_type", None) != "interactive"
                    ):
                        raise QuotedMessageUnavailable(
                            "被引用的应用消息没有可验证的可见内容，"
                            "请复制内容后重试。本条消息未执行。"
                        )
                    fallback_text = interactive_quote_visible_text(fallback)

            image_references_to_prepare = quoted_images + current_images
            prepared_images = await prepare_images(
                self._channel,
                image_references_to_prepare,
            )
            quoted_read_keys = tuple(
                image.reference.file_key
                for image in prepared_images
                if image.reference.source == "quoted_message"
            )
            prompt_text = render_plain_prompt(current)
            if quoted is not None:
                prompt_text = compose_quoted_prompt(
                    quoted,
                    current,
                    interactive_fallback_text=fallback_text,
                    read_image_keys=quoted_read_keys,
                )

            return compose_multimodal_input(
                prompt_text,
                images=prepared_images,
            )
        except (QuotedMessageError, ImageInputError, PromptProjectionError):
            raise
        except TimeoutError as error:
            raise QuotedMessageUnavailable(
                "读取被引用消息超时，本条消息未执行；"
                "请重新发送。"
            ) from error
        except Exception as error:
            logger.warning(
                "prompt context preparation failed",
                extra={"error_type": type(error).__name__},
            )
            if quoted_target_id is not None:
                raise QuotedMessageUnavailable(
                    "无法读取被引用的消息；它可能已撤回，"
                    "或应用缺少消息读取权限。本条消息未执行。"
                ) from error
            raise ImageInputUnavailable(
                "无法处理消息中的图片，本条消息未执行；请重新发送。"
            ) from error

    async def _control(self, message: Any, intent: ControlIntent) -> None:
        if intent.name in {ControlName.MENU, ControlName.HELP}:
            await self._reply(message, self._help())
            return
        if intent.name is ControlName.SIDE:
            await self._create_side(message, intent)
            return
        if intent.name is ControlName.SETTINGS:
            await self._reply(message, self._settings_card(intent.scope))
            return
        if intent.name is ControlName.GOAL:
            binding = self._bindings.active_binding(intent.scope.key)
            if binding is None:
                await self._reply(
                    message,
                    "当前聊天或话题还没有会话，请先发送 /new [project|none]。",
                )
                return
            project = self._projects.resolve_for_binding(binding.project_alias)
            argument = intent.arguments[0] if intent.arguments else None
            action = argument.lower() if argument is not None else None
            if argument is None:
                snapshot = await self._runtime.goal_snapshot(binding)
                active_goal = self._runtime.active_goal(binding.id)
                await self._reply(
                    message,
                    goal_card(
                        scope=intent.scope,
                        binding_id=binding.id,
                        short_id=binding.short_id,
                        project_alias=binding.project_alias,
                        goal=snapshot,
                        runtime_state=(
                            active_goal.state.value if active_goal is not None else None
                        ),
                    ),
                )
                return
            if action == "pause":
                await self._runtime.goal_snapshot(binding)
                acknowledged = False

                async def acknowledge_goal_pause() -> None:
                    nonlocal acknowledged
                    await self._reply(
                        message,
                        "正在暂停 Codex Goal 并中断当前物理 Turn。",
                    )
                    acknowledged = True

                result = await self._runtime.stop(
                    binding.id,
                    acknowledge=acknowledge_goal_pause,
                )
                if result is StopDisposition.EXTERNAL_GOAL:
                    await self._reply(
                        message,
                        "这是重启前或外部客户端启动的 active Goal；"
                        "当前 SDK 无法安全重挂并暂停，请先在原生 Codex 中暂停。",
                    )
                elif result is StopDisposition.NOT_RUNNING and not acknowledged:
                    await self._reply(message, "当前没有本服务可控的 running Goal。")
                return
            if action == "resume":
                submission = await self._runtime.resume_goal(
                    binding=binding,
                    owner_id=intent.sender_id,
                    origin=message,
                )
                try:
                    snapshot = await self._runtime.goal_snapshot(binding)
                    await self._reply(
                        message,
                        goal_card(
                            scope=intent.scope,
                            binding_id=binding.id,
                            short_id=binding.short_id,
                            project_alias=binding.project_alias,
                            goal=snapshot,
                            runtime_state=GoalOperationState.RUNNING.value,
                            notice="Goal 已恢复。",
                        ),
                    )
                finally:
                    submission.release_receipt_attempt()
                return
            if action == "clear":
                cleared = await self._runtime.clear_goal(binding)
                await self._reply(
                    message,
                    goal_card(
                        scope=intent.scope,
                        binding_id=binding.id,
                        short_id=binding.short_id,
                        project_alias=binding.project_alias,
                        goal=None,
                        notice=("Goal 已清除。" if cleared else "当前没有 Goal。"),
                    ),
                )
                return
            assert argument is not None
            submission = await self._runtime.start_goal(
                binding=binding,
                cwd=project.cwd,
                objective=argument,
                owner_id=intent.sender_id,
                origin=message,
            )
            try:
                snapshot = await self._runtime.goal_snapshot(
                    self._bindings.get(binding.id)
                )
                await self._reply(
                    message,
                    goal_card(
                        scope=intent.scope,
                        binding_id=binding.id,
                        short_id=binding.short_id,
                        project_alias=binding.project_alias,
                        goal=snapshot,
                        runtime_state=GoalOperationState.RUNNING.value,
                        notice="Goal 已启动；原生 Codex 可自动继续多个物理 Turn。",
                    ),
                )
            finally:
                submission.release_receipt_attempt()
            return
        if intent.name is ControlName.CONFIG:
            binding = self._bindings.active_binding(intent.scope.key)
            if binding is None:
                await self._reply(
                    message,
                    "当前聊天或话题还没有会话，请先发送 /new [project|none]。",
                )
                return
            goal = await self._runtime.goal_snapshot(binding)
            active_goal = self._runtime.active_goal(binding.id)
            if active_goal is not None or (
                goal is not None and goal.status is GoalStatus.ACTIVE
            ):
                await self._reply(
                    message,
                    "当前 Goal 正在执行或状态未完成，不能修改 "
                    "Model / Effort / Speed；请先暂停 Goal。",
                )
                return
            active = self._runtime.active_turn(binding.id)
            if self._runtime.is_compacting(binding.id):
                await self._reply(
                    message,
                    "当前会话正在压缩上下文，完成前不能修改 "
                    "Model / Effort / Speed。",
                )
                return
            if active is not None:
                if active.state is ActiveState.STOPPING:
                    notice = (
                        "当前 Turn 正在停止，不能修改 Model / Effort / Speed；"
                        "若 /stop 曾提示清理失败，请再次发送 /stop 重试。"
                    )
                else:
                    notice = (
                        "当前 Turn 正在执行，不能修改 Model / Effort / Speed；"
                        "请等待完成或先发送 /stop。"
                    )
                await self._reply(
                    message,
                    notice,
                )
                return
            catalog = await self._runtime.model_catalog()
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
                ),
            )
            return
        if intent.name is ControlName.COMPACT:
            binding = self._bindings.active_binding(intent.scope.key)
            if binding is None:
                await self._reply(
                    message,
                    "当前聊天或话题还没有会话，请先发送 /new [project|none]。",
                )
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
            return
        if intent.name is ControlName.NEW:
            if not intent.arguments:
                projects = self._projects.list(enabled_only=True)
                catalog = None
                catalog_error = None
                try:
                    catalog = await self._runtime.model_catalog()
                except Exception as error:
                    logger.warning(
                        "native model catalog unavailable for /new settings",
                        extra={"error_type": type(error).__name__},
                    )
                    catalog_error = "Model / Effort / Speed 暂不可用。"
                await self._reply(
                    message,
                    new_binding_card(
                        scope=intent.scope,
                        projects=projects,
                        catalog=catalog,
                        catalog_error=catalog_error,
                    ),
                )
                return
            project, binding = await self._create_binding(
                scope=intent.scope,
                sender_id=intent.sender_id,
                project_alias=intent.arguments[0],
            )
            await self._reply(
                message,
                f"已创建并切换会话 {binding.short_id}（{project.alias}）；"
                "首条消息将创建原生 Codex Thread。",
            )
            return
        if intent.name is ControlName.SESSIONS:
            bindings = self._bindings.list_bindings(intent.scope.key)
            archived_view = bool(intent.arguments)
            if not bindings and not archived_view:
                await self._reply(message, "当前聊天或话题还没有会话。")
                return
            if archived_view:
                metadata = await self._read_thread_metadata(
                    bindings,
                    archived=True,
                    strict=True,
                )
                sessions = tuple(
                    ArchivedSessionCardItem(
                        binding_id=binding.id,
                        short_id=binding.short_id,
                        project_alias=binding.project_alias,
                        native_thread_id=binding.native_thread_id,
                        title=_session_title(
                            binding,
                            metadata[binding.native_thread_id],
                        ),
                    )
                    for binding in bindings
                    if binding.native_thread_id is not None
                    and binding.native_thread_id in metadata
                )
                await self._reply(
                    message,
                    archived_sessions_card(
                        scope=intent.scope,
                        sessions=sessions,
                    ),
                )
                return

            metadata = await self._read_thread_metadata(bindings)
            archived_metadata = await self._read_thread_metadata(
                bindings,
                archived=True,
                strict=True,
            )
            entries: list[str] = []
            for binding in bindings:
                if binding.native_thread_id in archived_metadata:
                    continue
                state = await self._binding_state(binding)
                native = (
                    binding.native_thread_id[:8]
                    if binding.native_thread_id is not None
                    else "pending"
                )
                title = _session_title(
                    binding,
                    metadata.get(binding.native_thread_id),
                )
                entries.append(
                    f"{'●' if binding.active else '○'} {title}\n"
                    f"  会话：{binding.short_id} · Project：{binding.project_alias} · "
                    f"Native：{native} · 状态：{state}"
                )
            if entries:
                await self._reply(
                    message,
                    "当前 Scope 的会话：\n\n" + "\n\n".join(entries),
                )
            else:
                await self._reply(
                    message,
                    "当前 Scope 没有普通会话；发送 /sessions archived 查看归档。",
                )
            return
        if intent.name is ControlName.RESUME:
            binding = await self._management.resume_current_binding(
                scope_key=intent.scope.key,
                reference=intent.arguments[0],
            )
            await self._reply(
                message,
                f"已切换到会话 {binding.short_id}（{binding.project_alias}）。",
            )
            return
        if intent.name is ControlName.UNARCHIVE:
            binding = await self._management.restore_current_binding(
                scope_key=intent.scope.key,
                reference=intent.arguments[0],
            )
            await self._reply(
                message,
                f"已恢复并切换到会话 {binding.short_id}（{binding.project_alias}）。",
            )
            return
        if intent.name is ControlName.RENAME:
            binding = self._bindings.active_binding(intent.scope.key)
            if binding is None:
                await self._reply(message, "当前 Scope 没有 active 会话。")
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
                f"当前会话已重命名为：`{_markdown_code(name)}`",
            )
            return
        if intent.name is ControlName.ARCHIVE:
            binding = self._bindings.active_binding(intent.scope.key)
            if binding is None:
                await self._reply(message, "当前 Scope 没有 active 会话。")
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
            return
        if intent.name is ControlName.DELETE:
            binding = self._bindings.active_binding(intent.scope.key)
            if binding is None:
                await self._reply(message, "当前 Scope 没有 active 会话。")
                return
            if binding.native_thread_id is not None:
                raise ThreadDeleteUnavailable(
                    "已有原生历史的会话暂不支持删除：当前 Python SDK 尚未提供"
                    "公开、可靠的 Thread Delete。本次未调用 Codex，Binding 与"
                    "原生历史均未改变；请等待 SDK 升级。"
                )
            await self._reply(
                message,
                delete_binding_card(
                    scope=intent.scope,
                    binding_id=binding.id,
                    short_id=binding.short_id,
                    project_alias=binding.project_alias,
                    title=_session_title(binding, None),
                ),
            )
            return
        if intent.name is ControlName.RELEASE:
            binding = self._bindings.active_binding(intent.scope.key)
            if binding is None:
                await self._reply(message, "当前 Scope 没有 active 会话。")
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
            return
        if intent.name is ControlName.STATUS:
            binding = self._bindings.active_binding(intent.scope.key)
            if binding is None:
                await self._reply(message, "当前 Scope 没有 active 会话。")
                return
            state = await self._binding_state(binding)
            native = binding.native_thread_id or "pending（首条消息后创建）"
            metadata = await self._read_thread_metadata((binding,))
            thread_metadata_lines = _thread_metadata_status_lines(
                binding,
                metadata.get(binding.native_thread_id),
            )
            context_window_line = _context_window_status_line(
                binding,
                self._runtime.context_window_usage(binding.id),
                active_turn=state in {
                    ActiveState.RUNNING.value,
                    ActiveState.STOPPING.value,
                },
            )
            progress_lines = _turn_progress_status_lines(
                self._runtime.turn_progress(binding.id)
            )
            model_lines = await self._binding_model_status_lines(binding)
            subscription_line = _thread_subscription_status_line(
                binding,
                self._runtime.thread_subscription_snapshot(binding.id),
            )
            await self._reply(
                message,
                "\n".join(
                    (
                        "当前会话",
                        f"会话：{binding.short_id}",
                        *thread_metadata_lines,
                        f"Project：{binding.project_alias}",
                        f"Native Thread：{native}",
                        f"状态：{state}",
                        subscription_line,
                        *progress_lines,
                        context_window_line,
                        *model_lines,
                    )
                ),
            )
            return
        if intent.name is ControlName.STOP:
            binding = self._bindings.active_binding(intent.scope.key)
            if binding is None:
                await self._reply(message, "当前没有 active 会话。")
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
            return
        raise InvalidInteraction(f"尚未处理的命令：/{intent.name.value}")

    async def _handle_turn_file_card_action(self, event: Any) -> None:
        message_id = str(getattr(event, "message_id", "") or "")
        chat_id = str(getattr(event, "chat_id", "") or "")
        operator = getattr(event, "operator", None)
        sender_id = str(getattr(operator, "open_id", "") or "")
        action = getattr(event, "action", None)
        intent: TurnFileActionIntent | None = None
        try:
            if action is None:
                raise CardActionError("本轮文件回调缺少 action。")
            intent = decode_turn_file_action(
                app_id=self._app_id,
                message_id=message_id,
                callback_chat_id=chat_id,
                sender_id=sender_id,
                tag=str(getattr(action, "tag", "") or ""),
                value=getattr(action, "value", None),
            )
            await self._turn_file_action(intent)
        except (CardActionError, TurnFileError) as error:
            await self._safe_turn_file_feedback(
                message_id=message_id,
                chat_id=chat_id,
                sender_id=sender_id,
                action=getattr(action, "value", None),
                message=str(error),
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.exception(
                "Turn file card interaction failed",
                extra={
                    "error_type": type(error).__name__,
                    "binding_id": getattr(intent, "binding_id", None),
                    "turn_id": getattr(intent, "turn_id", None),
                },
            )
            await self._safe_turn_file_feedback(
                message_id=message_id,
                chat_id=chat_id,
                sender_id=sender_id,
                action=getattr(action, "value", None),
                message="本轮文件操作失败，请重新执行任务后再试。",
            )

    async def _turn_file_action(self, intent: TurnFileActionIntent) -> None:
        if intent.name is TurnFileActionName.PAGE:
            assert intent.page is not None
            assert intent.answer is not None
            card = turn_files_card_from_manifest(
                scope=intent.scope,
                binding_id=intent.binding_id,
                turn_id=intent.turn_id,
                final_response=intent.answer,
                manifest=intent.files,
                page=intent.page,
            )
            updated = await self._safe_update_card(intent.source_id, card)
            if not updated:
                raise TurnFileError("飞书未确认本轮文件卡片翻页成功。")
            return

        assert intent.name is TurnFileActionName.SEND
        assert intent.path is not None
        turn_file = require_turn_file_path(intent.path)
        await self._send_turn_file(intent, turn_file)

    async def _send_turn_file(
        self,
        intent: TurnFileActionIntent,
        turn_file: TurnFile,
    ) -> None:
        source = MediaSource(kind="file", path=str(turn_file.resolved_path))
        content: OutboundFile | OutboundImage
        if turn_file.media_kind == "image":
            content = OutboundImage(source=source)
        else:
            content = OutboundFile(
                source=source,
                file_name=turn_file.resolved_path.name,
            )
        sent = await self._channel.send(
            intent.scope.chat_id,
            content,
            SendOpts(
                receive_id_type="chat_id",
                reply_to=intent.source_id,
                reply_in_thread=True,
                reply_target_gone="fail",
                uuid=_turn_file_action_uuid("turn-file-send-", intent),
            ),
        )
        _validate_turn_file_reply(sent, intent=intent)

    async def _safe_turn_file_feedback(
        self,
        *,
        message_id: str,
        chat_id: str,
        sender_id: str,
        action: object,
        message: str,
    ) -> None:
        if not message_id or not chat_id:
            logger.error("cannot report Turn file failure without card identity")
            return
        digest_input = repr((message_id, sender_id, action)).encode("utf-8")
        uuid = "turn-file-error-" + hashlib.sha256(digest_input).hexdigest()[:32]
        try:
            result = await self._channel.send(
                chat_id,
                f"⚠️ 本轮文件操作未完成：{message[:500]}",
                SendOpts(
                    receive_id_type="chat_id",
                    reply_to=message_id,
                    reply_in_thread=True,
                    reply_target_gone="fail",
                    uuid=uuid,
                ),
            )
        except Exception:
            logger.exception("failed to send Turn file callback feedback")
            return
        if getattr(result, "success", True) is False:
            logger.error("Turn file callback feedback was not confirmed")

    async def _card_control(self, intent: CardControlIntent) -> None:
        if intent.name is CardControlName.SIDE_CLOSE:
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
            except SideCloseFailed as error:
                current = self._bindings.get_side_topic(record.id)
                await self._update_side_card(
                    current,
                    notice=str(error),
                    notice_is_error=True,
                )
                return
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
            return
        if intent.name in {
            CardControlName.OPEN_SETTINGS_SECTION,
            CardControlName.REFRESH_SETTINGS,
        }:
            assert intent.settings_section is not None
            await self._update_card(
                intent.source_id,
                self._settings_card(
                    intent.scope,
                    section=intent.settings_section,
                ),
            )
            return
        if intent.name is CardControlName.REGISTER_PROJECT:
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
                    notice=f"已登记 Project {project.alias}：{project.cwd}",
                ),
            )
            return
        if intent.name is CardControlName.SET_PROJECT_ENABLED:
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
                        f"已{'启用' if project.enabled else '停用'} Project "
                        f"{project.alias}。"
                    ),
                ),
            )
            return
        if intent.name is CardControlName.CREATE_BINDING:
            assert intent.project_alias is not None
            assert intent.expected_revision is not None
            settings = await self._resolve_card_model_settings(intent)
            turn_settings = _binding_turn_settings(settings)
            project, binding = await self._create_binding(
                scope=intent.scope,
                sender_id=intent.sender_id,
                project_alias=intent.project_alias,
                expected_revision=intent.expected_revision,
                turn_settings=turn_settings,
            )
            updated = await self._safe_update_card(
                intent.source_id,
                binding_created_card(
                    short_id=binding.short_id,
                    project_alias=project.alias,
                    settings=settings,
                ),
            )
            if not updated:
                await self._safe_reply_to_card(
                    intent,
                    f"✅ Project 选择成功：已选择 `{project.alias}`，"
                    f"并创建、切换到会话 `{binding.short_id}`。"
                    "现在可以直接发送任务。",
                )
            return
        if intent.name is CardControlName.CONFIGURE_BINDING:
            assert intent.binding_id is not None
            assert intent.expected_settings_revision is not None
            settings = await self._resolve_card_model_settings(intent)
            if settings is None:
                raise CardActionError(
                    "会话配置卡片已过期，请重新发送 /config。"
                )
            try:
                binding = await self._management.configure_current_binding(
                    target=CurrentBindingTarget(
                        intent.scope.key,
                        intent.binding_id,
                    ),
                    expected_settings_revision=intent.expected_settings_revision,
                    settings=_binding_turn_settings(settings),
                )
            except NoCurrentBinding as error:
                raise CardActionError(
                    "当前 Scope 已没有 active 会话，请重新发送 /new。"
                ) from error
            except CurrentBindingChanged as error:
                raise CardActionError(
                    "active 会话已切换，本卡片未执行；请重新发送 /config。"
                ) from error
            except BindingSettingsRevisionConflict as error:
                raise CardActionError(
                    "会话配置已变化，本卡片未执行；请重新发送 /config。"
                ) from error
            updated = await self._safe_update_card(
                intent.source_id,
                binding_configured_card(
                    short_id=binding.short_id,
                    project_alias=binding.project_alias,
                    settings=settings,
                ),
            )
            if not updated:
                await self._safe_reply_to_card(
                    intent,
                    f"✅ 会话 `{binding.short_id}` 配置已保存："
                    "会话后续每条新 Turn 都会应用。",
                )
            return
        if intent.name is CardControlName.RENAME_BINDING:
            assert intent.binding_id is not None
            assert intent.thread_name is not None
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
                        "✅ 当前会话名称已更新为："
                        f"`{_markdown_code(renamed.name)}`"
                    ),
                ),
            )
            if not updated:
                await self._safe_reply_to_card(
                    intent,
                    "✅ 当前会话已重命名为："
                    f"`{_markdown_code(renamed.name)}`",
                )
            return
        if intent.name is CardControlName.ARCHIVE_BINDING:
            assert intent.binding_id is not None
            try:
                archived = await self._management.archive_current_binding(
                    target=CurrentBindingTarget(
                        intent.scope.key,
                        intent.binding_id,
                    )
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
                        "✅ 原生 Codex 会话已归档，当前 Scope 已没有 active 会话。"
                        "\n\n发送 `/sessions archived` 可查看和恢复。"
                    ),
                ),
            )
            if not updated:
                await self._safe_reply_to_card(
                    intent,
                    "✅ 当前会话已归档；发送 /sessions archived 可查看和恢复。",
                )
            return
        if intent.name is CardControlName.DELETE_BINDING:
            assert intent.binding_id is not None
            try:
                deleted = await self._management.delete_current_lazy_binding(
                    target=CurrentBindingTarget(
                        intent.scope.key,
                        intent.binding_id,
                    )
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
                        "✅ 当前会话已删除，当前 Scope 已没有 active 会话。"
                        "\n\n发送 `/new` 创建新会话，或 `/resume <短 ID>` 切换。"
                    ),
                ),
            )
            if not updated:
                await self._safe_reply_to_card(
                    intent,
                    "✅ 当前会话已永久删除。",
                )
            return
        if intent.name is CardControlName.UNARCHIVE_BINDING:
            assert intent.binding_id is not None
            binding = self._bindings.get(intent.binding_id)
            if binding.scope_key != intent.scope.key:
                raise BindingNotFound(intent.binding_id)
            restored = await self._management.restore_current_binding(
                scope_key=intent.scope.key,
                reference=binding.id,
            )
            updated = await self._safe_update_card(
                intent.source_id,
                binding_lifecycle_result_card(
                    title="会话已恢复并切换",
                    short_id=restored.short_id,
                    project_alias=restored.project_alias,
                    message="✅ 已恢复原生 Codex 会话，并切换为当前会话。",
                ),
            )
            if not updated:
                await self._safe_reply_to_card(
                    intent,
                    f"✅ 已恢复并切换到会话 {restored.short_id}。",
                )
            return
        if intent.name in {
            CardControlName.GOAL_PAUSE,
            CardControlName.GOAL_RESUME,
            CardControlName.GOAL_CLEAR,
        }:
            assert intent.binding_id is not None
            async with self._scope_coordinator.hold(intent.scope.key):
                binding = self._bindings.active_binding(intent.scope.key)
                if binding is None or binding.id != intent.binding_id:
                    raise CardActionError(
                        "active 会话已切换，本 Goal 卡片未执行；请重新发送 /goal。"
                    )
                if intent.name is CardControlName.GOAL_PAUSE:
                    goal_before_pause = await self._runtime.goal_snapshot(binding)

                    async def acknowledge_goal_card_pause() -> None:
                        await self._safe_update_card(
                            intent.source_id,
                            goal_card(
                                scope=intent.scope,
                                binding_id=binding.id,
                                short_id=binding.short_id,
                                project_alias=binding.project_alias,
                                goal=goal_before_pause,
                                runtime_state=GoalOperationState.PAUSING.value,
                                notice="正在暂停 Goal 并中断当前物理 Turn。",
                            ),
                        )

                    result = await self._runtime.stop(
                        binding.id,
                        acknowledge=acknowledge_goal_card_pause,
                    )
                    if result is StopDisposition.EXTERNAL_GOAL:
                        raise CardActionError(
                            "这是外部 active Goal，当前 SDK 无法安全重挂并暂停。"
                        )
                    if result in {
                        StopDisposition.GOAL_REQUESTED,
                        StopDisposition.GOAL_STOPPING,
                    }:
                        paused = await self._runtime.goal_snapshot(binding)
                        await self._safe_update_card(
                            intent.source_id,
                            goal_card(
                                scope=intent.scope,
                                binding_id=binding.id,
                                short_id=binding.short_id,
                                project_alias=binding.project_alias,
                                goal=paused,
                                runtime_state=(
                                    f"goal-{paused.status.value}"
                                    if paused is not None
                                    else GoalOperationState.PAUSING.value
                                ),
                                notice=(
                                    "Goal 已暂停；已请求清理该 Thread 中已登记的"
                                    "后台终端。前台工具进程可能仍在运行。"
                                ),
                            ),
                        )
                    return
                if intent.name is CardControlName.GOAL_RESUME:
                    origin = GoalCardOrigin(
                        message_id=intent.source_id,
                        scope=intent.scope,
                        binding_id=binding.id,
                        short_id=binding.short_id,
                        project_alias=binding.project_alias,
                    )
                    submission = await self._runtime.resume_goal(
                        binding=binding,
                        owner_id=intent.sender_id,
                        origin=origin,
                    )
                    try:
                        await self._safe_update_card(
                            intent.source_id,
                            goal_card(
                                scope=intent.scope,
                                binding_id=binding.id,
                                short_id=binding.short_id,
                                project_alias=binding.project_alias,
                                goal=await self._runtime.goal_snapshot(binding),
                                runtime_state=GoalOperationState.RUNNING.value,
                                notice="Goal 已恢复。",
                            ),
                        )
                    finally:
                        submission.release_receipt_attempt()
                    return
                cleared = await self._runtime.clear_goal(binding)
                await self._safe_update_card(
                    intent.source_id,
                    goal_card(
                        scope=intent.scope,
                        binding_id=binding.id,
                        short_id=binding.short_id,
                        project_alias=binding.project_alias,
                        goal=None,
                        notice=("Goal 已清除。" if cleared else "当前没有 Goal。"),
                    ),
                )
            return
        raise CardActionError(f"尚未处理的卡片动作：{intent.name.value}")

    async def _resolve_card_model_settings(
        self,
        intent: CardControlIntent,
    ) -> TurnModelSettings | None:
        if intent.model_id is None:
            if intent.effort_id is not None or intent.service_tier_id is not None:
                raise CardActionError("Model / Effort / Speed 必须成组设置。")
            return None
        assert intent.model_id is not None
        assert intent.effort_id is not None
        assert intent.service_tier_id is not None
        return await self._runtime.resolve_model_settings(
            model_id=intent.model_id,
            effort_id=intent.effort_id,
            service_tier_id=intent.service_tier_id,
        )

    async def _binding_state(self, binding: ThreadBinding) -> str:
        lifecycle = self._runtime.lifecycle_state(binding.id)
        if lifecycle is not None:
            return lifecycle.state.value
        active = self._runtime.active_turn(binding.id)
        if active is not None:
            return active.state.value
        if self._runtime.is_compacting(binding.id):
            return "compacting"
        active_goal = self._runtime.active_goal(binding.id)
        if active_goal is not None:
            return active_goal.state.value
        persisted = await self._runtime.goal_snapshot(binding)
        return "idle" if persisted is None else f"goal-{persisted.status.value}"

    async def _read_thread_metadata(
        self,
        bindings: Sequence[ThreadBinding],
        *,
        archived: bool = False,
        strict: bool = False,
    ) -> dict[str, NativeThreadMetadata]:
        thread_ids = tuple(
            dict.fromkeys(
                binding.native_thread_id
                for binding in bindings
                if binding.native_thread_id is not None
            )
        )
        if not thread_ids:
            return {}
        try:
            return await self._runtime.thread_metadata(
                thread_ids,
                archived=archived,
            )
        except Exception as error:
            if strict:
                raise ThreadLifecycleError(
                    "无法读取 Codex 归档会话列表，请稍后重试。"
                ) from error
            # Titles are useful display metadata, never an admission or
            # correctness signal. Keep read-only controls available if the
            # native history list is temporarily unavailable.
            logger.warning(
                "native Thread metadata unavailable for Channel display",
                extra={"error_type": type(error).__name__},
            )
            return {}

    async def _binding_model_status_lines(
        self,
        binding: ThreadBinding,
    ) -> tuple[str, ...]:
        configured = binding.turn_settings
        if configured is None:
            value = "继承 Codex"
            return (
                f"Model：{value}",
                f"Effort：{value}",
                f"Speed：{value}",
                "配置来源：Codex",
            )

        try:
            catalog = await self._runtime.model_catalog()
        except Exception as error:
            # Status is read-only and must remain useful when the live catalog
            # cannot be read. The persisted IDs remain the exact Binding
            # selection; submission will revalidate them before every new Turn.
            logger.warning(
                "native model catalog unavailable for /status Binding settings",
                extra={"error_type": type(error).__name__},
            )
            return (
                f"Model：{configured.model_id}",
                f"Effort：{configured.effort_id}",
                f"Speed：{configured.service_tier_id}",
                "配置来源：Netizen 会话配置（模型目录暂不可用）",
            )

        try:
            settings = catalog.resolve(
                model_id=configured.model_id,
                effort_id=configured.effort_id,
                service_tier_id=configured.service_tier_id,
            )
        except ModelCatalogError:
            return (
                f"Model：{configured.model_id}",
                f"Effort：{configured.effort_id}",
                f"Speed：{configured.service_tier_id}",
                "配置来源：Netizen 会话配置（已失效，请使用 /config 更新）",
            )

        return (
            f"Model：{settings.model}",
            f"Effort：{settings.effort_id}",
            f"Speed：{settings.service_tier_name}",
            "配置来源：Netizen 会话配置",
        )

    async def _decode_card_event(self, event: Any) -> CardControlIntent:
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
            scope = scope_from_fetched_card(
                app_id=self._app_id,
                callback_chat_id=chat_id,
                fetched_message=fetched,
                chat_type=_public_chat_kind(chat_info),
            )
            return decode_card_form(
                scope=scope,
                message_id=message_id,
                sender_id=sender_id,
                tag=tag,
                form_value=form_value,
            )
        return decode_button_action(
            app_id=self._app_id,
            message_id=message_id,
            callback_chat_id=chat_id,
            sender_id=sender_id,
            tag=tag,
            value=getattr(action, "value", None),
        )

    async def _create_binding(
        self,
        *,
        scope: FeishuScope,
        sender_id: str,
        project_alias: str,
        expected_revision: int | None = None,
        turn_settings: BindingTurnSettings | None = None,
    ):
        created = await self._management.create_current_binding(
            scope=scope,
            creator_id=sender_id,
            project_alias=project_alias,
            expected_project_revision=expected_revision,
            turn_settings=turn_settings,
        )
        return created.project, created.binding

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

    async def _reply(self, message: Any, content: Any) -> None:
        await self._channel.reply(message, content)

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
        if getattr(result, "success", True) is False:
            logger.error(
                "failed to add message reaction: unsuccessful result",
                extra={"message_id": message_id, "emoji_type": emoji_type},
            )
            return False
        return True

    async def _update_card(self, message_id: str, content: Any) -> object:
        return await self._channel.update_card(message_id, content.card)

    async def _safe_update_card(self, message_id: str, content: Any) -> bool:
        if not message_id:
            return False
        try:
            result = await self._update_card(message_id, content)
        except Exception:
            logger.exception("failed to render card action result")
            return False
        if getattr(result, "success", True) is False:
            logger.error("failed to render card action result: unsuccessful update")
            return False
        return True

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

    def _help(self) -> str:
        return (
            command_help(self._runtime.available_capabilities)
            + "\n普通图片和富文本图片可直接发送，也可随逐条引用一起交给 Codex。"
        )


def _public_chat_kind(chat_info: Any) -> str | None:
    """Normalize only the public chat type fields exposed by Channel SDK."""
    if chat_info is None:
        return None
    chat_type = str(getattr(chat_info, "chat_type", "") or "")
    if chat_type in {"p2p", "group"}:
        return chat_type
    chat_mode = str(getattr(chat_info, "chat_mode", "") or "")
    return chat_mode if chat_mode in {"p2p", "group"} else None


def _side_send_uuid(prefix: str, side_id: str) -> str:
    digest = hashlib.sha256(side_id.encode("utf-8")).hexdigest()[:32]
    return prefix + digest


def _turn_file_action_uuid(
    prefix: str,
    intent: TurnFileActionIntent,
) -> str:
    identity = "\0".join(
        (
            intent.scope.key,
            intent.source_id,
            intent.sender_id,
            intent.name.value,
            intent.binding_id,
            intent.turn_id,
            str(TURN_FILE_ACTION_VERSION),
            intent.path or "",
            "" if intent.page is None else str(intent.page),
        )
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    return prefix + digest


def _validate_turn_file_reply(
    result: object,
    *,
    intent: TurnFileActionIntent,
) -> _SentMessage:
    raw = getattr(result, "raw", None)
    code = _object_field(raw, "code")
    if code == 230071:
        raise TurnFileError(
            "当前飞书会话不支持将本卡片转为话题（230071），文件未发送。"
        )
    if getattr(result, "success", None) is not True:
        raise TurnFileError(
            f"飞书未确认本轮文件发送成功（code={code!r}）。"
        )
    if getattr(result, "chunk_ids", None):
        raise TurnFileError("本轮文件消息被意外拆分，无法确认话题关系。")
    if code != 0:
        raise TurnFileError(f"飞书本轮文件响应 code 异常：{code!r}。")
    data = _object_field(raw, "data")
    if data is None:
        raise TurnFileError("飞书本轮文件响应缺少 data。")
    message_id = _nonempty_field(result, "message_id")
    if message_id is None or _nonempty_field(data, "message_id") != message_id:
        raise TurnFileError("飞书本轮文件消息标识不一致。")
    chat_id = _nonempty_field(data, "chat_id")
    if chat_id != intent.scope.chat_id:
        raise TurnFileError("飞书把本轮文件发送到了其他聊天。")
    sent = _SentMessage(
        message_id=message_id,
        chat_id=chat_id,
        thread_id=_nonempty_field(data, "thread_id"),
        root_id=_nonempty_field(data, "root_id"),
        parent_id=_nonempty_field(data, "parent_id"),
    )
    if intent.scope.kind is ScopeKind.TOPIC:
        if (
            sent.thread_id != intent.scope.topic_id
            or sent.root_id is None
            or sent.parent_id is None
        ):
            raise TurnFileError("飞书未确认文件消息留在原话题中。")
    elif (
        sent.thread_id is None
        or sent.root_id is None
        or sent.parent_id != intent.source_id
    ):
        raise TurnFileError("飞书未确认本轮文件卡片已转为话题。")
    return sent


def _side_initial_question_echo(text: str) -> str:
    safe_text = _FEISHU_AT_TAG_START.sub("‹", text)
    if len(safe_text) <= _SIDE_INITIAL_QUESTION_MAX_CHARS:
        label = "首轮问题（来自 /side 发起消息）"
        visible = safe_text
    else:
        label = "首轮问题（来自 /side 发起消息，内容节选）"
        visible = (
            safe_text[: _SIDE_INITIAL_QUESTION_MAX_CHARS - 1].rstrip() + "…"
        )
    return f"{label}\n\n{visible}"


def _side_reply_target(
    message: _SentMessage,
    topic_id: str,
) -> _CardReplyTarget:
    return _CardReplyTarget(
        id=message.message_id,
        message_id=message.message_id,
        chat_id=message.chat_id,
        conversation=_CardReplyConversation(thread_id=topic_id),
    )


def _validated_sent_message(
    result: object,
    *,
    expected_chat_id: str,
) -> _SentMessage:
    raw = getattr(result, "raw", None)
    code = _object_field(raw, "code")
    if code == 230071:
        raise SideTopicCreateFailed(
            "当前飞书会话不支持创建 Side 话题（230071）；本次未创建。"
        )
    if getattr(result, "success", None) is not True:
        raise SideTopicCreateFailed(
            f"飞书未确认 Side 消息发送成功（code={code!r}）。"
        )
    chunk_ids = getattr(result, "chunk_ids", None)
    if chunk_ids:
        raise SideTopicCreateFailed("Side 卡片或种子消息被意外拆分。")
    if code != 0:
        raise SideTopicCreateFailed(
            f"飞书 Side 消息响应 code 异常：{code!r}。"
        )
    data = _object_field(raw, "data")
    if data is None:
        raise SideTopicCreateFailed("飞书 Side 消息响应缺少 data。")
    message_id = _nonempty_field(result, "message_id")
    data_message_id = _nonempty_field(data, "message_id")
    if message_id is None or data_message_id != message_id:
        raise SideTopicCreateFailed("飞书 Side 消息标识不一致。")
    chat_id = _nonempty_field(data, "chat_id")
    if chat_id != expected_chat_id:
        raise SideTopicCreateFailed("飞书 Side 消息返回了其他 chat_id。")
    return _SentMessage(
        message_id=message_id,
        chat_id=chat_id,
        thread_id=_nonempty_field(data, "thread_id"),
        root_id=_nonempty_field(data, "root_id"),
        parent_id=_nonempty_field(data, "parent_id"),
    )


def _retryable_send_result(result: object) -> bool:
    if getattr(result, "success", None) is True:
        return False
    error = getattr(result, "error", None)
    return getattr(error, "retryable", None) is True


def _object_field(value: object, name: str) -> object | None:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _nonempty_field(value: object, name: str) -> str | None:
    field = _object_field(value, name)
    return field if isinstance(field, str) and field else None


def _message_chat_type(message: Any) -> str:
    conversation = getattr(message, "conversation", None)
    return str(
        getattr(message, "chat_type", None)
        or getattr(conversation, "chat_type", "unknown")
    )


def _inbound_root_message_id(message: Any) -> str | None:
    raw = getattr(message, "raw", None)
    if not isinstance(raw, Mapping):
        return None
    value = raw.get("root_id")
    return value if isinstance(value, str) and value else None


def _message_id(message: Any) -> str:
    return str(getattr(message, "message_id", None) or getattr(message, "id", ""))


def _chat_id(message: Any) -> str:
    conversation = getattr(message, "conversation", None)
    return str(
        getattr(message, "chat_id", None)
        or getattr(conversation, "chat_id", "")
    )


def _sender_id(message: Any) -> str:
    sender = getattr(message, "sender", None)
    return str(
        getattr(message, "sender_id", None)
        or getattr(sender, "open_id", "")
    )


def _body_text(message: Any, *, bot_open_id: str | None = None) -> str:
    # Channel SDK's ``body_text`` has already removed the bot mention.  Its
    # empty string is meaningful for a bare @bot and must not fall back to the
    # rendered safe text (which would send "@bot" to Codex).
    body = getattr(message, "body_text", None)
    if body is not None:
        text = str(body)
    else:
        text = str(
            getattr(message, "safe_content_text", None)
            or getattr(message, "content_text", "")
        )
    return _repair_leading_post_bot_mention(
        message,
        text,
        bot_open_id=bot_open_id,
    )


def _channel_bot_open_id(channel: ReplyChannel) -> str | None:
    identity = getattr(channel, "bot_identity", None)
    value = getattr(identity, "open_id", None)
    return str(value) if value else None


def _repair_leading_post_bot_mention(
    message: Any,
    text: str,
    *,
    bot_open_id: str | None,
) -> str:
    """Repair lark-channel-sdk 1.2.0's post mention placeholder mismatch.

    Feishu topic-root posts encode the leading at-node's ``user_id`` as a
    placeholder such as ``@_user_1``.  The pinned SDK tries to remove it by
    comparing that value with the bot open_id, leaving a rendered ``@Bot`` in
    ``body_text``.  Match the public bot identity, normalized mention key and
    first public PostContent AST node before removing only that exact prefix.
    """
    if (
        _raw_content_type(message) != "post"
        or not bool(getattr(message, "mentioned_bot", False))
        or not bot_open_id
    ):
        return text

    bot_mention = next(
        (
            mention
            for mention in (getattr(message, "mentions", None) or [])
            if getattr(mention, "open_id", None) == bot_open_id
        ),
        None,
    )
    mention_key = getattr(bot_mention, "key", None)
    if bot_mention is None or not mention_key:
        return text

    first_node = _first_post_node(message)
    if (
        first_node is None
        or first_node.get("tag") != "at"
        or first_node.get("user_id") not in {mention_key, bot_open_id}
    ):
        return text

    display_name = (
        first_node.get("user_name")
        or getattr(bot_mention, "name", None)
        or first_node.get("user_id")
    )
    prefix = f"@{display_name}"
    if not text.startswith(prefix):
        return text
    return text[len(prefix):].lstrip()


def _first_post_node(message: Any) -> dict[str, Any] | None:
    content = getattr(message, "content", None)
    post = getattr(content, "post", None)
    if not isinstance(post, dict) or not post:
        return None
    documents = (
        [post]
        if "content" in post or "content_v2" in post
        else [value for value in post.values() if isinstance(value, dict)]
    )
    if not documents:
        return None
    document = documents[0]
    paragraphs = document.get("content_v2")
    if not isinstance(paragraphs, list) or not paragraphs:
        paragraphs = document.get("content")
    if not isinstance(paragraphs, list):
        return None
    for paragraph in paragraphs:
        if not isinstance(paragraph, list):
            continue
        for node in paragraph:
            if isinstance(node, dict):
                return node
    return None


def _raw_content_type(message: Any) -> str:
    value = getattr(message, "raw_content_type", None)
    return str(value or "text")
