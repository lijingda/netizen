"""Feishu product controls over the native Codex runtime."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Protocol

from lark_channel import MediaSource, OutboundCard, OutboundFile, OutboundImage, SendOpts

from .bindings import (
    AmbiguousBinding,
    BindingContextRevisionConflict,
    BindingFeedbackRevisionConflict,
    BindingNotFound,
    BindingSettingsRevisionConflict,
    BindingStore,
    BindingTaskFeedback,
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
    SessionCardItem,
    SettingsCardActionError,
    TURN_FILE_ACTION_VERSION,
    REPLY_CARD_ACTION_VERSION,
    TurnFileCardLimitError,
    archive_binding_card,
    archived_sessions_card,
    archived_sessions_delete_binding_card,
    activity_step_display,
    sessions_card,
    binding_configured_card,
    binding_created_card,
    binding_lifecycle_result_card,
    config_card,
    context_mode_display,
    decode_button_action,
    decode_card_form,
    decode_turn_file_action,
    error_card,
    fetched_card_topic_id,
    new_binding_card,
    goal_card,
    goal_generation,
    is_turn_file_action,
    scope_from_fetched_card,
    delete_binding_card,
    sessions_delete_binding_card,
    rename_binding_card,
    settings_card,
    side_topic_card,
    turn_files_card,
    turn_files_card_from_manifest,
    turn_progress_card,
    turn_progress_card_from_manifest,
    reply_card,
    reply_card_from_manifest,
)
from .codex_runtime import (
    BindingRuntimeSnapshot,
    CodexRuntime,
    CompactionOutcome,
    ContextAnchorRequired,
    ContextBoundaryCommitFailed,
    ContextCursorCommit,
    ContextWindowUsage,
    ExternalGoalActive,
    GoalNotFound,
    GoalNotMaterialized,
    GoalActivitySnapshot,
    GoalFinalizationStatus,
    GoalOutcome,
    GoalSubmission,
    GoalStateUnknown,
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
    SideTurnActivitySnapshot,
    SideTurnOutcome,
    SkillReferenceError,
    SteerRace,
    StopDisposition,
    SubmitDisposition,
    TerminalCleanupFailed,
    ThreadCompactStartFailed,
    ThreadCompacting,
    ThreadActivityDiscardedOutcome,
    ThreadGoalActive,
    ThreadArchived,
    ThreadDeleteUnavailable,
    ThreadDeleteTargetChanged,
    ThreadLifecycleError,
    ThreadNotMaterialized,
    ThreadReleaseError,
    ThreadRunningConfiguration,
    ThreadSubscriptionSnapshot,
    ThreadSubscriptionState,
    ThreadStopping,
    TurnProgressSnapshot,
    TurnActivitySnapshot,
    TurnInterruptFailed,
    TurnObservationUnavailableOutcome,
    TurnStartFailed,
    TurnOutcome,
)
from .domain import (
    ACTIVE_STATE_VALUES,
    ActiveState,
    CardControlIntent,
    CardControlName,
    ControlIntent,
    ControlName,
    FeishuScope,
    GoalOperationState,
    GoalStatus,
    MessageContextAnchor,
    MentionContextMode,
    NativeCapability,
    PromptInput,
    SettingsSection,
    ScopeKind,
    TurnFileActionIntent,
    TurnFileActionName,
    ReplyCardActivityModule,
    ReplyCardFileItem,
    ReplyCardFilesModule,
    ReplyCardGoalModule,
    ReplyCardManifest,
    ReplyCardProjection,
    ReplyCardResultModule,
    SESSION_IDLE_STATE,
    TurnProgressManifest,
    TurnProgressManifestStep,
    persisted_goal_session_state,
)
from .experience import (
    InvalidInteraction,
    command_help,
    parse_message,
    side_command_help,
)
from .model_settings import ModelCatalogError, TurnModelSettings
from .management import (
    ActivePointerChanged,
    CurrentBindingChanged,
    CurrentBindingTarget,
    CurrentSideTarget,
    ExactBindingTarget,
    InstanceManagementService,
    ManagementRuntimePort,
    NoCurrentBinding,
    RuntimePrecondition,
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
    CurrentMessageProjection,
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
from .message_history import (
    MessageHistoryError,
    MessageHistoryReader,
    MessageHistoryRef,
    MessageHistoryUnavailable,
)
from .message_projection import (
    HistoricalMessageError,
    HistoricalMessageProjection,
    SupplementalContextStats,
    SupplementalMessageOmission,
    compose_message_context_prompt,
    historical_message_deleted,
    normalized_historical_message_type,
    project_quoted_message,
    project_supplemental_message,
    select_supplemental_messages,
)
from .sdk_gap_adapter import SkillCatalogError
from .sdk_gap_adapter import GoalControlError, GoalSnapshot
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
_CONTEXT_PREPARATION_TIMEOUT_SECONDS = 60.0
_CONTEXT_FETCH_TIMEOUT_SECONDS = 10.0
_CONTEXT_FETCH_CONCURRENCY = 4
_CONTEXT_RECEIPT_TIMEOUT_SECONDS = 5.0
_CONTEXT_MESSAGE_LIMIT = 50
_CONTEXT_TEXT_LIMIT = 64_000
_DONE_REACTION = "DONE"
_ERROR_REACTION = "ERROR"
_INTERRUPTED_REACTION = "CrossMark"
_STEER_REACTION = "OnIt"
_TYPING_REACTION = "Typing"
_THINKING_REACTION = "THINKING"
_THINKING_VISIBLE_SECONDS = 2.0
_THINKING_HIDDEN_SECONDS = 13.0
_REACTION_OPERATION_TIMEOUT_SECONDS = 3.0
# lark-channel-sdk 1.2.0 exposes the nested transient lock only in SendError.hint.
# Remove this retry after card-action dispatch is ordered behind its Feishu ack.
_FEISHU_CARD_ACTION_LOCK_OUTER_CODE = 230099
_FEISHU_CARD_ACTION_LOCK_INNER_CODE = 11310
_CARD_ACTION_LOCK_RETRY_DELAYS_SECONDS = (0.2, 0.5)
_PROGRESS_CARD_POLL_SECONDS = 1.0
_PROGRESS_CARD_OPERATION_TIMEOUT_SECONDS = 5.0
_GOAL_REPLY_CARD_CACHE_LIMIT = 256
_SESSION_TITLE_MAX_CHARS = 48
_STATUS_THREAD_NAME_MAX_CHARS = 120
_STATUS_THREAD_PREVIEW_MAX_CHARS = 240
_STATUS_PLAN_MAX_STEPS = 12
_SIDE_ROOT_UUID_PREFIX = "side-root-"
_SIDE_SEED_UUID_PREFIX = "side-seed-"
_SIDE_INITIAL_QUESTION_MAX_CHARS = 3000
_SIDE_EMPTY_TOPIC_PROMPT = "在本话题发送第一条问题，开始 Side 对话。"
_FEISHU_AT_TAG_START = re.compile(r"<(?=/?at(?:\s|>|/))", re.IGNORECASE)
_FEISHU_CONTENT_AUDIT_REJECTION_CODE = 230028
_FEISHU_AUDIT_REASON_LABELS = {
    "EMAIL_ADDRESS": "邮箱地址",
}


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
        normalized = activity_step_display(item.step)
        icon = icons.get(item.status.value, "○")
        lines.append(f"{icon} {normalized}")
    remaining = len(progress.steps) - len(visible)
    if remaining > 0:
        lines.append(f"… 另有 {remaining} 项未展示")
    return tuple(lines)


def _reply_goal_module(
    *,
    binding: ThreadBinding,
    goal: GoalSnapshot | None,
    runtime_state: str | None = None,
    notice: str | None = None,
    notice_is_error: bool = False,
) -> ReplyCardGoalModule:
    return _reply_goal_module_for_identity(
        binding_id=binding.id,
        short_id=binding.short_id,
        project_alias=binding.project_alias,
        goal=goal,
        runtime_state=runtime_state,
        notice=notice,
        notice_is_error=notice_is_error,
    )


def _reply_goal_module_for_identity(
    *,
    binding_id: str,
    short_id: str,
    project_alias: str,
    goal: GoalSnapshot | None,
    runtime_state: str | None = None,
    notice: str | None = None,
    notice_is_error: bool = False,
) -> ReplyCardGoalModule:
    return ReplyCardGoalModule(
        binding_id=binding_id,
        short_id=short_id,
        project_alias=project_alias,
        goal_generation=None if goal is None else goal_generation(goal),
        status=None if goal is None else goal.status.value,
        runtime_state=None if goal is None else runtime_state,
        objective=None if goal is None else goal.objective,
        token_budget=None if goal is None else goal.token_budget,
        tokens_used=0 if goal is None else goal.tokens_used,
        notice=notice,
        notice_is_error=notice_is_error,
    )


def _reply_activity_module(
    snapshot: TurnActivitySnapshot | GoalActivitySnapshot,
    *,
    terminal_status: str | None = None,
    collapsed: bool = False,
) -> ReplyCardActivityModule:
    state = getattr(snapshot.state, "value", snapshot.state)
    if state in {ActiveState.STOPPING.value, GoalOperationState.PAUSING.value}:
        state = ActiveState.STOPPING.value
    else:
        state = ActiveState.RUNNING.value
    visible_steps = snapshot.steps[:_STATUS_PLAN_MAX_STEPS]
    return ReplyCardActivityModule(
        progress=TurnProgressManifest(
            state=state,
            steer_count=max(0, int(getattr(snapshot, "steer_count", 0))),
            plan_available=bool(snapshot.plan_available),
            plan_generated=bool(snapshot.plan_generated),
            plan_may_be_stale=bool(
                getattr(snapshot, "plan_may_be_stale", False)
            ),
            steps=tuple(
                TurnProgressManifestStep(
                    step=activity_step_display(item.step),
                    status=getattr(item.status, "value", item.status),
                )
                for item in visible_steps
            ),
        ),
        terminal_status=terminal_status,
        collapsed=collapsed,
        hidden_steps=max(0, len(snapshot.steps) - len(visible_steps)),
    )


def _reply_files_module(
    *,
    binding_id: str,
    turn_id: str,
    files: tuple[TurnFile, ...],
) -> ReplyCardFilesModule:
    return ReplyCardFilesModule(
        binding_id=binding_id,
        turn_id=turn_id,
        items=tuple(
            ReplyCardFileItem(
                path=str(item.resolved_path),
                label=item.display_path,
                size=item.size,
                media_kind=item.media_kind,
            )
            for item in files
        ),
        action_version=REPLY_CARD_ACTION_VERSION,
    )


@dataclass(slots=True)
class GoalCardOrigin:
    message_id: str | None
    scope: FeishuScope
    binding_id: str
    short_id: str
    project_alias: str
    fallback_origin: object | None = None
    goal_generation: str | None = None


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


@dataclass(slots=True)
class _TurnProgressCardSession:
    binding_id: str
    thread_id: str
    turn_id: str
    message_id: str
    stopped: asyncio.Event
    snapshot: TurnActivitySnapshot
    failed: bool = False
    task: asyncio.Task[None] | None = None


@dataclass(slots=True)
class _SideTurnProgressCardSession:
    side_id: str
    thread_id: str
    turn_id: str
    message_id: str
    stopped: asyncio.Event
    snapshot: SideTurnActivitySnapshot
    failed: bool = False
    task: asyncio.Task[None] | None = None


@dataclass(slots=True)
class _GoalReplyCardSession:
    binding_id: str
    thread_id: str
    goal_generation: str
    logical_turn_id: str
    message_id: str
    stopped: asyncio.Event
    projection: ReplyCardProjection
    revision: object
    refresh: Callable[
        [],
        Awaitable[tuple[object, ReplyCardProjection] | None],
    ] | None = None
    failed: bool = False
    task: asyncio.Task[None] | None = None


class _GoalCardDelivery(Enum):
    DELIVERED = "delivered"
    SUPERSEDED = "superseded"
    FAILED = "failed"


class _ReplyCardPresenter:
    """One best-effort owner for Turn, Side Turn, and Goal Reply Cards."""

    def __init__(
        self,
        channel: ReplyChannel,
        runtime: CodexRuntime,
        *,
        poll_seconds: float = _PROGRESS_CARD_POLL_SECONDS,
        operation_timeout_seconds: float = _PROGRESS_CARD_OPERATION_TIMEOUT_SECONDS,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("progress card poll interval must be positive")
        if operation_timeout_seconds <= 0:
            raise ValueError("progress card operation timeout must be positive")
        self._channel = channel
        self._runtime = runtime
        self._poll_seconds = poll_seconds
        self._operation_timeout_seconds = operation_timeout_seconds
        self._sessions: dict[
            tuple[str, str, str],
            _TurnProgressCardSession,
        ] = {}
        self._side_sessions: dict[
            tuple[str, str, str],
            _SideTurnProgressCardSession,
        ] = {}
        self._goal_sessions: dict[
            tuple[str, str, str],
            _GoalReplyCardSession,
        ] = {}
        self._goal_cards: dict[
            tuple[str, str],
            ReplyCardProjection,
        ] = {}
        self._goal_lock = asyncio.Lock()
        self._goal_card_lock = asyncio.Lock()
        self._retired_goal_runs: set[tuple[str, str, str]] = set()
        self._goal_latest_runs: dict[tuple[str, str, str], str] = {}
        self._closed = False

    async def start(
        self,
        *,
        binding_id: str,
        thread_id: str,
        turn_id: str,
        origin: object,
    ) -> bool:
        if self._closed:
            return False
        try:
            snapshot = self._runtime.turn_activity(
                binding_id,
                thread_id=thread_id,
                turn_id=turn_id,
                refresh_plan=True,
            )
        except Exception:
            logger.exception(
                "failed to read initial progress-card activity",
                extra={"binding_id": binding_id, "turn_id": turn_id},
            )
            return False
        if snapshot is None:
            logger.error(
                "failed to start progress card: exact Turn activity unavailable",
                extra={
                    "binding_id": binding_id,
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                },
            )
            return False
        try:
            card = turn_progress_card(snapshot=snapshot)
            async with asyncio.timeout(self._operation_timeout_seconds):
                result = await self._channel.reply(origin, card)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "failed to send initial progress card",
                extra={"binding_id": binding_id, "turn_id": turn_id},
            )
            return False
        message_id = _progress_card_message_id(result)
        if message_id is None:
            logger.error(
                "failed to start progress card: reply message ID unavailable",
                extra={"binding_id": binding_id, "turn_id": turn_id},
            )
            return False
        current = self._runtime.turn_activity(
            binding_id,
            thread_id=thread_id,
            turn_id=turn_id,
            refresh_plan=False,
        )
        if (
            current is None
            or self._runtime.lifecycle_state(binding_id) is not None
        ):
            return False
        snapshot = current
        key = (binding_id, thread_id, turn_id)
        previous = self._sessions.pop(key, None)
        if previous is not None:
            await self._stop_session(previous)
        session = _TurnProgressCardSession(
            binding_id=binding_id,
            thread_id=thread_id,
            turn_id=turn_id,
            message_id=message_id,
            stopped=asyncio.Event(),
            snapshot=snapshot,
        )
        self._sessions[key] = session
        session.task = asyncio.create_task(
            self._poll(session),
            name=f"netizen-progress-card-{turn_id}",
        )
        return True

    async def finish(
        self,
        *,
        binding_id: str,
        thread_id: str,
        turn_id: str,
        activity: TurnActivitySnapshot | None,
        render: Callable[[TurnActivitySnapshot], OutboundCard],
    ) -> bool:
        session = self._sessions.pop((binding_id, thread_id, turn_id), None)
        if session is None:
            return False
        await self._stop_session(session)
        if session.failed:
            return False
        snapshot = activity or session.snapshot
        if (
            snapshot.binding_id != session.binding_id
            or snapshot.thread_id != session.thread_id
            or snapshot.turn_id != session.turn_id
        ):
            logger.error(
                "failed to finish progress card: activity identity mismatch",
                extra={
                    "binding_id": session.binding_id,
                    "turn_id": session.turn_id,
                },
            )
            return False
        try:
            card = render(snapshot)
            return await self._update(session, card)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "failed to render terminal progress card",
                extra={
                    "binding_id": session.binding_id,
                    "turn_id": session.turn_id,
                },
            )
            return False

    async def abandon(
        self,
        *,
        binding_id: str,
        thread_id: str,
        turn_id: str,
    ) -> None:
        session = self._sessions.pop((binding_id, thread_id, turn_id), None)
        if session is not None:
            await self._stop_session(session)

    async def abandon_thread(
        self,
        *,
        binding_id: str,
        thread_id: str,
    ) -> None:
        """Stop every ordinary/Goal presenter owned by one removed Thread."""

        ordinary_keys = tuple(
            key
            for key in self._sessions
            if key[0] == binding_id and key[1] == thread_id
        )
        for key in ordinary_keys:
            session = self._sessions.pop(key, None)
            if session is not None:
                await self._stop_session(session)

        async with self._goal_lock:
            goal_keys = tuple(
                key
                for key in self._goal_sessions
                if key[0] == binding_id and key[1] == thread_id
            )
            self._goal_latest_runs = {
                key: run_id
                for key, run_id in self._goal_latest_runs.items()
                if key[:2] != (binding_id, thread_id)
            }
            removed: list[_GoalReplyCardSession] = []
            for key in goal_keys:
                session = self._goal_sessions.pop(key, None)
                if session is not None:
                    removed.append(session)
                    await self._stop_session(session)
            if removed:
                async with self._goal_card_lock:
                    for session in removed:
                        self._goal_cards.pop(
                            (session.message_id, session.goal_generation),
                            None,
                        )

    async def park_unavailable(
        self,
        *,
        binding_id: str,
        thread_id: str,
        turn_id: str,
    ) -> bool:
        """Render one unavailable snapshot and stop all future card polling."""

        session = self._sessions.pop((binding_id, thread_id, turn_id), None)
        if session is None:
            return False
        await self._stop_session(session)
        if session.failed:
            return False
        try:
            snapshot = self._runtime.turn_activity(
                binding_id,
                thread_id=thread_id,
                turn_id=turn_id,
                refresh_plan=False,
            )
        except Exception:
            logger.exception(
                "failed to read unavailable progress-card activity",
                extra={"binding_id": binding_id, "turn_id": turn_id},
            )
            return False
        if snapshot is None:
            return False
        try:
            return await self._update(
                session,
                turn_progress_card(snapshot=snapshot),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "failed to render unavailable progress card",
                extra={"binding_id": binding_id, "turn_id": turn_id},
            )
            return False

    async def start_side(
        self,
        *,
        side_id: str,
        thread_id: str,
        turn_id: str,
        origin: object,
    ) -> bool:
        if self._closed:
            return False
        try:
            snapshot = self._runtime.side_turn_activity(
                side_id,
                thread_id=thread_id,
                turn_id=turn_id,
                refresh_plan=True,
            )
        except Exception:
            logger.exception(
                "failed to read initial Side progress-card activity",
                extra={"side_id": side_id, "turn_id": turn_id},
            )
            return False
        if snapshot is None:
            logger.error(
                "failed to start Side progress card: exact activity unavailable",
                extra={
                    "side_id": side_id,
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                },
            )
            return False
        try:
            card = turn_progress_card(snapshot=snapshot)
            async with asyncio.timeout(self._operation_timeout_seconds):
                result = await self._channel.reply(origin, card)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "failed to send initial Side progress card",
                extra={"side_id": side_id, "turn_id": turn_id},
            )
            return False
        message_id = _progress_card_message_id(result)
        if message_id is None:
            logger.error(
                "failed to start Side progress card: reply message ID unavailable",
                extra={"side_id": side_id, "turn_id": turn_id},
            )
            return False
        key = (side_id, thread_id, turn_id)
        previous = self._side_sessions.pop(key, None)
        if previous is not None:
            await self._stop_session(previous)
        session = _SideTurnProgressCardSession(
            side_id=side_id,
            thread_id=thread_id,
            turn_id=turn_id,
            message_id=message_id,
            stopped=asyncio.Event(),
            snapshot=snapshot,
        )
        self._side_sessions[key] = session
        session.task = asyncio.create_task(
            self._poll_side(session),
            name=f"netizen-side-progress-card-{turn_id}",
        )
        return True

    async def finish_side(
        self,
        *,
        side_id: str,
        thread_id: str,
        turn_id: str,
        activity: SideTurnActivitySnapshot | None,
        render: Callable[[SideTurnActivitySnapshot], OutboundCard],
    ) -> bool:
        session = self._side_sessions.pop((side_id, thread_id, turn_id), None)
        if session is None:
            return False
        await self._stop_session(session)
        if session.failed:
            return False
        snapshot = activity or session.snapshot
        if (
            snapshot.side_id != session.side_id
            or snapshot.thread_id != session.thread_id
            or snapshot.turn_id != session.turn_id
        ):
            logger.error(
                "failed to finish Side progress card: activity identity mismatch",
                extra={"side_id": session.side_id, "turn_id": session.turn_id},
            )
            return False
        try:
            return await self._update_side(session, render(snapshot))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "failed to render terminal Side progress card",
                extra={"side_id": session.side_id, "turn_id": session.turn_id},
            )
            return False

    async def abandon_side(
        self,
        *,
        side_id: str,
        thread_id: str,
        turn_id: str,
    ) -> None:
        session = self._side_sessions.pop((side_id, thread_id, turn_id), None)
        if session is not None:
            await self._stop_session(session)

    async def start_goal(
        self,
        *,
        binding_id: str,
        thread_id: str,
        logical_turn_id: str,
        generation: str,
        origin: GoalCardOrigin,
        projection: ReplyCardProjection,
        revision: object,
        refresh: Callable[
            [],
            Awaitable[tuple[object, ReplyCardProjection] | None],
        ]
        | None,
    ) -> bool:
        async with self._goal_lock:
            return await self._start_goal_locked(
                binding_id=binding_id,
                thread_id=thread_id,
                logical_turn_id=logical_turn_id,
                generation=generation,
                origin=origin,
                projection=projection,
                revision=revision,
                refresh=refresh,
            )

    async def _start_goal_locked(
        self,
        *,
        binding_id: str,
        thread_id: str,
        logical_turn_id: str,
        generation: str,
        origin: GoalCardOrigin,
        projection: ReplyCardProjection,
        revision: object,
        refresh: Callable[
            [],
            Awaitable[tuple[object, ReplyCardProjection] | None],
        ]
        | None,
    ) -> bool:
        if self._closed:
            return False
        key = (binding_id, thread_id, generation)
        self._goal_latest_runs = {
            existing: run_id
            for existing, run_id in self._goal_latest_runs.items()
            if existing[:2] != (binding_id, thread_id) or existing == key
        }
        self._goal_latest_runs[key] = logical_turn_id
        previous = self._goal_sessions.pop(key, None)
        message_id = origin.message_id
        if previous is not None:
            await self._stop_session(previous)
            if not previous.failed:
                message_id = previous.message_id
        try:
            card = reply_card(projection)
            if message_id is None:
                fallback = origin.fallback_origin
                if fallback is None:
                    return False
                async with asyncio.timeout(self._operation_timeout_seconds):
                    result = await self._channel.reply(fallback, card)
                message_id = _progress_card_message_id(result)
            else:
                async with self._goal_card_lock:
                    if not await self._update_message(
                        message_id,
                        card,
                        binding_id=binding_id,
                        operation_id=logical_turn_id,
                    ):
                        return False
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "failed to establish Goal Reply Card",
                extra={
                    "binding_id": binding_id,
                    "logical_turn_id": logical_turn_id,
                },
            )
            return False
        if message_id is None:
            logger.error(
                "failed to establish Goal Reply Card: reply message ID unavailable",
                extra={
                    "binding_id": binding_id,
                    "logical_turn_id": logical_turn_id,
                },
            )
            return False
        if self._runtime.lifecycle_state(binding_id) is not None:
            return False
        origin.message_id = message_id
        origin.goal_generation = generation
        self._retired_goal_runs = {
            item
            for item in self._retired_goal_runs
            if item[:2] != (message_id, generation)
        }
        session = _GoalReplyCardSession(
            binding_id=binding_id,
            thread_id=thread_id,
            goal_generation=generation,
            logical_turn_id=logical_turn_id,
            message_id=message_id,
            stopped=asyncio.Event(),
            projection=projection,
            revision=revision,
            refresh=refresh,
        )
        self._goal_sessions[key] = session
        self._remember_goal_projection(message_id, generation, projection)
        if refresh is not None:
            session.task = asyncio.create_task(
                self._poll_goal(session),
                name=f"netizen-goal-reply-card-{logical_turn_id}",
            )
        return True

    async def finish_goal(
        self,
        *,
        binding_id: str,
        thread_id: str,
        logical_turn_id: str | None,
        generation: str,
        origin: GoalCardOrigin,
        projection: ReplyCardProjection,
        retain_session: bool,
    ) -> _GoalCardDelivery:
        async with self._goal_lock:
            return await self._finish_goal_locked(
                binding_id=binding_id,
                thread_id=thread_id,
                logical_turn_id=logical_turn_id,
                generation=generation,
                origin=origin,
                projection=projection,
                retain_session=retain_session,
            )

    async def reply_goal_fallback(
        self,
        *,
        binding_id: str,
        thread_id: str,
        logical_turn_id: str | None,
        generation: str,
        target: object,
        card: OutboundCard,
        origin: GoalCardOrigin,
        projection: ReplyCardProjection,
        retain_session: bool,
    ) -> _GoalCardDelivery:
        """CAS, reply, and adopt one fallback while the Goal route is stable."""

        async with self._goal_lock:
            if self._closed:
                return _GoalCardDelivery.FAILED
            key = (binding_id, thread_id, generation)
            latest_run = self._goal_latest_runs.get(key)
            if latest_run is not None and latest_run != logical_turn_id:
                return _GoalCardDelivery.SUPERSEDED
            current = self._goal_sessions.get(key)
            if current is not None and current.logical_turn_id != logical_turn_id:
                return _GoalCardDelivery.SUPERSEDED
            if current is not None:
                self._goal_sessions.pop(key, None)
                await self._stop_session(current)
            try:
                async with asyncio.timeout(self._operation_timeout_seconds):
                    result = await self._channel.reply(target, card)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "terminal Goal card fallback failed",
                    extra={"binding_id": binding_id},
                )
                return _GoalCardDelivery.FAILED
            if getattr(result, "success", True) is False:
                logger.error(
                    "terminal Goal card fallback was not confirmed",
                    extra={"binding_id": binding_id},
                )
                return _GoalCardDelivery.FAILED
            message_id = _progress_card_message_id(result)
            if message_id is None:
                logger.warning(
                    "terminal Goal fallback lacks a reusable message identity",
                    extra={"binding_id": binding_id},
                )
                return _GoalCardDelivery.DELIVERED
            origin.message_id = message_id
            origin.goal_generation = generation
            if logical_turn_id is not None:
                self._goal_latest_runs[key] = logical_turn_id
            async with self._goal_card_lock:
                if not retain_session:
                    return _GoalCardDelivery.DELIVERED
                self._remember_goal_projection(
                    message_id,
                    generation,
                    projection,
                )
            if retain_session:
                self._goal_sessions[key] = _GoalReplyCardSession(
                    binding_id=binding_id,
                    thread_id=thread_id,
                    goal_generation=generation,
                    logical_turn_id=logical_turn_id or generation,
                    message_id=message_id,
                    stopped=asyncio.Event(),
                    projection=projection,
                    revision=("terminal-fallback",),
                )
            return _GoalCardDelivery.DELIVERED

    async def _finish_goal_locked(
        self,
        *,
        binding_id: str,
        thread_id: str,
        logical_turn_id: str | None,
        generation: str,
        origin: GoalCardOrigin,
        projection: ReplyCardProjection,
        retain_session: bool,
    ) -> _GoalCardDelivery:
        key = (binding_id, thread_id, generation)
        latest_run = self._goal_latest_runs.get(key)
        if latest_run is not None and latest_run != logical_turn_id:
            # The exact Goal generation has already advanced to a newer
            # logical run, even if that newer run has also reached terminal.
            return _GoalCardDelivery.SUPERSEDED
        session = self._goal_sessions.get(key)
        if (
            session is not None
            and session.logical_turn_id != logical_turn_id
        ):
            # A resumed run already owns this Goal generation and card.  The
            # previous run's delayed terminal projection must not overwrite it.
            return _GoalCardDelivery.SUPERSEDED
        retired_key = (
            origin.message_id or "",
            generation,
            logical_turn_id or "",
        )
        if session is None and retired_key in self._retired_goal_runs:
            self._retired_goal_runs.discard(retired_key)
            return _GoalCardDelivery.SUPERSEDED
        if session is not None:
            self._goal_sessions.pop(key, None)
        if session is not None:
            await self._stop_session(session)
            if session.failed:
                return _GoalCardDelivery.FAILED
            message_id = session.message_id
        else:
            message_id = origin.message_id
        if message_id is None:
            return _GoalCardDelivery.FAILED
        try:
            card = reply_card(projection)
        except Exception:
            logger.exception(
                "failed to render terminal Goal Reply Card",
                extra={"binding_id": binding_id},
            )
            return _GoalCardDelivery.FAILED
        async with self._goal_card_lock:
            delivered = await self._update_message(
                message_id,
                card,
                binding_id=binding_id,
                operation_id=origin.goal_generation or generation,
            )
            if delivered and retain_session:
                self._remember_goal_projection(
                    message_id,
                    generation,
                    projection,
                )
            elif delivered:
                self._goal_cards.pop((message_id, generation), None)
        if delivered and retain_session:
            self._goal_sessions[key] = _GoalReplyCardSession(
                binding_id=binding_id,
                thread_id=thread_id,
                goal_generation=generation,
                logical_turn_id=(
                    session.logical_turn_id
                    if session is not None
                    else (logical_turn_id or generation)
                ),
                message_id=message_id,
                stopped=asyncio.Event(),
                projection=projection,
                revision=("terminal",),
            )
        elif delivered:
            self._retired_goal_runs.discard(retired_key)
        return (
            _GoalCardDelivery.DELIVERED
            if delivered
            else _GoalCardDelivery.FAILED
        )

    async def update_goal(
        self,
        *,
        source_id: str,
        generation: str,
        projection: ReplyCardProjection,
        retain_session: bool = True,
    ) -> bool:
        async with self._goal_lock:
            return await self._update_goal_locked(
                source_id=source_id,
                generation=generation,
                projection=projection,
                retain_session=retain_session,
            )

    async def refresh_goal_snapshot(
        self,
        *,
        source_id: str,
        generation: str,
        logical_turn_id: str | None,
        projection: ReplyCardProjection,
    ) -> bool:
        """Merge `/goal` status into the canonical card without regressing it."""

        async with self._goal_lock:
            matched = next(
                (
                    session
                    for session in self._goal_sessions.values()
                    if session.message_id == source_id
                    and session.goal_generation == generation
                ),
                None,
            )
            current = self._goal_projection(source_id, generation)
            if matched is None:
                # A terminal update won the race after the read-only native
                # snapshot.  Its newer projection already answers the status
                # request and must not be overwritten by the stale read.
                return current is not None
            incoming_goal = projection.goal
            assert incoming_goal is not None
            if matched.refresh is not None and (
                (
                    logical_turn_id is not None
                    and logical_turn_id != matched.logical_turn_id
                )
                or incoming_goal.status != GoalStatus.ACTIVE.value
            ):
                return True
            merged = projection
            if current is not None:
                merged_goal = incoming_goal
                if (
                    current.goal is not None
                    and current.goal.goal_generation == generation
                    and current.goal.status == incoming_goal.status
                ):
                    merged_goal = replace(
                        incoming_goal,
                        notice=current.goal.notice,
                        notice_is_error=current.goal.notice_is_error,
                    )
                merged = replace(
                    current,
                    scope=projection.scope or current.scope,
                    goal=merged_goal,
                    activity=(
                        projection.activity
                        if matched.refresh is not None
                        else current.activity
                    ),
                )
            return await self._update_goal_locked(
                source_id=source_id,
                generation=generation,
                projection=merged,
                retain_session=True,
            )

    async def update_goal_module(
        self,
        *,
        source_id: str,
        generation: str,
        scope: FeishuScope,
        goal: ReplyCardGoalModule,
        retain_session: bool,
    ) -> bool:
        """Atomically replace only Goal while preserving other card modules."""

        async with self._goal_lock:
            current = self._goal_projection(source_id, generation)
            projection = (
                ReplyCardProjection(scope=scope, goal=goal)
                if current is None
                else replace(current, scope=scope, goal=goal)
            )
            return await self._update_goal_locked(
                source_id=source_id,
                generation=generation,
                projection=projection,
                retain_session=retain_session,
            )

    async def _update_goal_locked(
        self,
        *,
        source_id: str,
        generation: str,
        projection: ReplyCardProjection,
        retain_session: bool,
    ) -> bool:
        matched_key: tuple[str, str, str] | None = None
        matched: _GoalReplyCardSession | None = None
        for key, session in self._goal_sessions.items():
            if (
                session.message_id == source_id
                and session.goal_generation == generation
            ):
                matched_key = key
                matched = session
                break
        if matched_key is not None and matched is not None:
            self._goal_sessions.pop(matched_key, None)
            await self._stop_session(matched)
            if not retain_session and matched.refresh is not None:
                self._retired_goal_runs.add(
                    (source_id, generation, matched.logical_turn_id)
                )
        try:
            card = reply_card(projection)
        except Exception:
            logger.exception("failed to render Goal Reply Card update")
            return False
        async with self._goal_card_lock:
            delivered = await self._update_message(
                source_id,
                card,
                binding_id=(
                    matched.binding_id if matched is not None else "unknown"
                ),
                operation_id=generation,
            )
            if delivered and retain_session:
                self._remember_goal_projection(
                    source_id,
                    generation,
                    projection,
                )
            elif delivered:
                self._goal_cards.pop((source_id, generation), None)
        if delivered and retain_session and matched_key is not None and matched is not None:
            refreshed_session = _GoalReplyCardSession(
                binding_id=matched.binding_id,
                thread_id=matched.thread_id,
                goal_generation=matched.goal_generation,
                logical_turn_id=matched.logical_turn_id,
                message_id=source_id,
                stopped=asyncio.Event(),
                projection=projection,
                revision=matched.revision,
                refresh=matched.refresh,
            )
            self._goal_sessions[matched_key] = refreshed_session
            if refreshed_session.refresh is not None:
                refreshed_session.task = asyncio.create_task(
                    self._poll_goal(refreshed_session),
                    name=(
                        "netizen-goal-reply-card-"
                        f"{refreshed_session.logical_turn_id}"
                    ),
                )
        return delivered

    async def abandon_goal(
        self,
        *,
        binding_id: str,
        thread_id: str,
        logical_turn_id: str | None,
        generation: str,
    ) -> None:
        async with self._goal_lock:
            key = (binding_id, thread_id, generation)
            session = self._goal_sessions.get(key)
            if (
                session is None
                or session.logical_turn_id != logical_turn_id
            ):
                return
            self._goal_sessions.pop(key, None)
            await self._stop_session(session)

    def goal_projection(
        self,
        *,
        source_id: str,
        generation: str,
    ) -> ReplyCardProjection | None:
        return self._goal_projection(source_id, generation)

    def _goal_projection(
        self,
        source_id: str,
        generation: str,
    ) -> ReplyCardProjection | None:
        current = self._goal_cards.get((source_id, generation))
        if current is not None:
            return current
        for session in self._goal_sessions.values():
            if (
                session.message_id == source_id
                and session.goal_generation == generation
            ):
                return session.projection
        return None

    async def update_goal_page(
        self,
        *,
        source_id: str,
        binding_id: str,
        generation: str,
        page: int,
        render: Callable[[ReplyCardProjection | None], OutboundCard],
    ) -> bool:
        """Serialize one Goal file-page rebuild with every card mutation."""

        async with self._goal_lock:
            async with self._goal_card_lock:
                current = self._goal_projection(source_id, generation)
                if (
                    current is not None
                    and current.goal is not None
                    and current.goal.binding_id != binding_id
                ):
                    raise CardActionError("Goal 文件卡片的会话身份不一致。")
                card = render(current)
                delivered = await self._update_message(
                    source_id,
                    card,
                    binding_id=binding_id,
                    operation_id=generation,
                )
                if delivered and current is not None and current.files is not None:
                    current = replace(
                        current,
                        files=replace(current.files, page=page),
                    )
                    self._remember_goal_projection(
                        source_id,
                        generation,
                        current,
                    )
                    for session in self._goal_sessions.values():
                        if (
                            session.message_id == source_id
                            and session.goal_generation == generation
                        ):
                            session.projection = current
                            break
                return delivered

    def _remember_goal_projection(
        self,
        source_id: str,
        generation: str,
        projection: ReplyCardProjection,
    ) -> None:
        key = (source_id, generation)
        if key not in self._goal_cards and (
            len(self._goal_cards) >= _GOAL_REPLY_CARD_CACHE_LIMIT
        ):
            self._goal_cards.pop(next(iter(self._goal_cards)))
        self._goal_cards[key] = projection

    def goal_message_id(
        self,
        *,
        binding_id: str,
        thread_id: str,
        generation: str,
    ) -> str | None:
        session = self._goal_sessions.get((binding_id, thread_id, generation))
        return None if session is None else session.message_id

    def owns_goal_card(
        self,
        *,
        source_id: str,
        binding_id: str,
        thread_id: str,
        generation: str,
    ) -> bool:
        """Return whether one live exact Goal route owns this control card."""

        session = self._goal_sessions.get((binding_id, thread_id, generation))
        return session is not None and session.message_id == source_id

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        sessions = tuple(self._sessions.values())
        self._sessions.clear()
        side_sessions = tuple(self._side_sessions.values())
        self._side_sessions.clear()
        async with self._goal_lock:
            goal_sessions = tuple(self._goal_sessions.values())
            self._goal_sessions.clear()
            self._retired_goal_runs.clear()
            self._goal_latest_runs.clear()
            self._goal_cards.clear()
        await asyncio.gather(
            *(
                self._stop_session(session)
                for session in (*sessions, *side_sessions, *goal_sessions)
            ),
            return_exceptions=False,
        )

    async def _poll(self, session: _TurnProgressCardSession) -> None:
        while not session.stopped.is_set():
            try:
                await asyncio.wait_for(
                    session.stopped.wait(),
                    timeout=self._poll_seconds,
                )
                return
            except TimeoutError:
                pass
            try:
                snapshot = self._runtime.turn_activity(
                    session.binding_id,
                    thread_id=session.thread_id,
                    turn_id=session.turn_id,
                    refresh_plan=True,
                )
            except Exception:
                logger.exception(
                    "failed to refresh running progress card",
                    extra={
                        "binding_id": session.binding_id,
                        "turn_id": session.turn_id,
                    },
                )
                session.failed = True
                return
            if snapshot is None:
                continue
            if snapshot.revision == session.snapshot.revision:
                continue
            session.snapshot = snapshot
            try:
                card = turn_progress_card(snapshot=snapshot)
            except Exception:
                logger.exception(
                    "failed to render running progress card",
                    extra={
                        "binding_id": session.binding_id,
                        "turn_id": session.turn_id,
                    },
                )
                session.failed = True
                return
            if not await self._update(session, card):
                session.failed = True
                return

    async def _poll_side(self, session: _SideTurnProgressCardSession) -> None:
        while not session.stopped.is_set():
            try:
                await asyncio.wait_for(
                    session.stopped.wait(),
                    timeout=self._poll_seconds,
                )
                return
            except TimeoutError:
                pass
            try:
                snapshot = self._runtime.side_turn_activity(
                    session.side_id,
                    thread_id=session.thread_id,
                    turn_id=session.turn_id,
                    refresh_plan=True,
                )
            except Exception:
                logger.exception(
                    "failed to refresh running Side progress card",
                    extra={
                        "side_id": session.side_id,
                        "turn_id": session.turn_id,
                    },
                )
                session.failed = True
                return
            if snapshot is None:
                continue
            if snapshot.revision == session.snapshot.revision:
                continue
            session.snapshot = snapshot
            try:
                card = turn_progress_card(snapshot=snapshot)
            except Exception:
                logger.exception(
                    "failed to render running Side progress card",
                    extra={
                        "side_id": session.side_id,
                        "turn_id": session.turn_id,
                    },
                )
                session.failed = True
                return
            if not await self._update_side(session, card):
                session.failed = True
                return

    async def _poll_goal(self, session: _GoalReplyCardSession) -> None:
        refresh = session.refresh
        if refresh is None:
            return
        while not session.stopped.is_set():
            try:
                await asyncio.wait_for(
                    session.stopped.wait(),
                    timeout=self._poll_seconds,
                )
                return
            except TimeoutError:
                pass
            try:
                async with asyncio.timeout(self._operation_timeout_seconds):
                    refreshed = await refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "failed to refresh running Goal Reply Card",
                    extra={
                        "binding_id": session.binding_id,
                        "logical_turn_id": session.logical_turn_id,
                    },
                )
                session.failed = True
                return
            if refreshed is None:
                continue
            revision, projection = refreshed
            if session.stopped.is_set():
                return
            async with self._goal_card_lock:
                key = (
                    session.binding_id,
                    session.thread_id,
                    session.goal_generation,
                )
                if (
                    session.stopped.is_set()
                    or self._goal_sessions.get(key) is not session
                ):
                    return
                if revision == session.revision:
                    continue
                session.revision = revision
                session.projection = projection
                try:
                    card = reply_card(projection)
                except Exception:
                    logger.exception(
                        "failed to render running Goal Reply Card",
                        extra={"binding_id": session.binding_id},
                    )
                    session.failed = True
                    return
                if not await self._update_message(
                    session.message_id,
                    card,
                    binding_id=session.binding_id,
                    operation_id=session.logical_turn_id,
                ):
                    session.failed = True
                    return
                self._remember_goal_projection(
                    session.message_id,
                    session.goal_generation,
                    projection,
                )

    async def _update(
        self,
        session: _TurnProgressCardSession,
        card: OutboundCard,
    ) -> bool:
        return await self._update_message(
            session.message_id,
            card,
            binding_id=session.binding_id,
            operation_id=session.turn_id,
        )

    async def _update_side(
        self,
        session: _SideTurnProgressCardSession,
        card: OutboundCard,
    ) -> bool:
        return await self._update_message(
            session.message_id,
            card,
            binding_id=f"side:{session.side_id}",
            operation_id=session.turn_id,
        )

    async def _update_message(
        self,
        message_id: str,
        card: OutboundCard,
        *,
        binding_id: str,
        operation_id: str,
    ) -> bool:
        try:
            async with asyncio.timeout(self._operation_timeout_seconds):
                result = await self._channel.update_card(
                    message_id,
                    card.card,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "failed to update progress card",
                extra={
                    "binding_id": binding_id,
                    "operation_id": operation_id,
                    "message_id": message_id,
                },
            )
            return False
        if getattr(result, "success", True) is False:
            logger.error(
                "failed to update progress card: unsuccessful result",
                extra={
                    "binding_id": binding_id,
                    "operation_id": operation_id,
                    "message_id": message_id,
                },
            )
            return False
        return True

    @staticmethod
    async def _stop_session(
        session: (
            _TurnProgressCardSession
            | _SideTurnProgressCardSession
            | _GoalReplyCardSession
        ),
    ) -> None:
        session.stopped.set()
        task = session.task
        if task is None or task is asyncio.current_task():
            return
        try:
            await task
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "progress card updater failed while stopping",
                extra={
                    "binding_id": getattr(session, "binding_id", None),
                    "side_id": getattr(session, "side_id", None),
                    "operation_id": getattr(
                        session,
                        "turn_id",
                        getattr(session, "logical_turn_id", "unknown"),
                    ),
                },
            )


# Compatibility construction seam retained for focused ordinary-Turn tests.
_ProgressCardController = _ReplyCardPresenter


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

    async def start(
        self,
        turn_id: str,
        message_id: str,
        *,
        pulse_enabled: bool,
    ) -> bool:
        if type(pulse_enabled) is not bool:
            raise ValueError("reaction pulse setting must be a boolean")
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
        if not pulse_enabled:
            return True

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
        message_history: MessageHistoryReader | None = None,
        scope_coordinator: ScopeCoordinator | None = None,
        management: InstanceManagementService | None = None,
    ) -> None:
        self._app_id = app_id
        self._channel = channel
        self._runtime = runtime
        self._bindings = bindings
        self._projects = projects
        self._message_history = message_history
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
        self._progress_cards = _ProgressCardController(channel, runtime)
        runtime.set_completion_handler(self.handle_completion)

    async def close(self) -> None:
        try:
            await self._progress_cards.close()
        finally:
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
        except (
            QuotedMessageError,
            MessageHistoryError,
            HistoricalMessageError,
            ContextBoundaryCommitFailed,
        ) as error:
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
            if (
                intent is not None
                and intent.name
                in {
                    CardControlName.GOAL_PAUSE,
                    CardControlName.GOAL_RESUME,
                    CardControlName.GOAL_CLEAR,
                }
            ):
                if not await self._update_goal_action_notice(intent, str(error)):
                    await self._safe_reply_to_card(intent, str(error))
                return
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
            BindingContextRevisionConflict,
            BindingNotFound,
            ContextAnchorRequired,
            GoalControlError,
            GoalNotFound,
            GoalNotMaterialized,
            GoalStateUnknown,
            ModelCatalogError,
            MessageHistoryError,
            ProjectError,
            RuntimeClosed,
            ThreadCompacting,
            ThreadGoalActive,
            ThreadLifecycleError,
            ThreadRunningConfiguration,
            ThreadStopping,
            TurnStartFailed,
        ) as error:
            if (
                intent is not None
                and intent.name
                in {
                    CardControlName.GOAL_PAUSE,
                    CardControlName.GOAL_RESUME,
                    CardControlName.GOAL_CLEAR,
                }
            ):
                if not await self._update_goal_action_notice(intent, str(error)):
                    await self._safe_reply_to_card(intent, str(error))
                return
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
                    CardControlName.ARCHIVE_EXACT_BINDING,
                    CardControlName.DELETE_BINDING,
                    CardControlName.PREPARE_EXACT_DELETE_BINDING,
                    CardControlName.DELETE_EXACT_BINDING,
                    CardControlName.PREPARE_ARCHIVED_DELETE_BINDING,
                    CardControlName.DELETE_ARCHIVED_BINDING,
                    CardControlName.UNARCHIVE_BINDING,
                    CardControlName.ACTIVATE_BINDING,
                    CardControlName.STOP_EXACT_BINDING,
                    CardControlName.RECHECK_EXACT_TURN,
                    CardControlName.REFRESH_ARCHIVED_SESSIONS,
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
            if (
                intent is not None
                and intent.name
                in {
                    CardControlName.GOAL_PAUSE,
                    CardControlName.GOAL_RESUME,
                    CardControlName.GOAL_CLEAR,
                }
            ):
                if not await self._update_goal_action_notice(
                    intent,
                    "卡片操作失败，请重新发送 /goal。",
                ):
                    await self._safe_reply_to_card(
                        intent,
                        "卡片操作失败，请重新发送 /goal。",
                    )
                return
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
                    CardControlName.ARCHIVE_EXACT_BINDING,
                    CardControlName.DELETE_BINDING,
                    CardControlName.PREPARE_EXACT_DELETE_BINDING,
                    CardControlName.DELETE_EXACT_BINDING,
                    CardControlName.PREPARE_ARCHIVED_DELETE_BINDING,
                    CardControlName.DELETE_ARCHIVED_BINDING,
                    CardControlName.UNARCHIVE_BINDING,
                    CardControlName.ACTIVATE_BINDING,
                    CardControlName.STOP_EXACT_BINDING,
                    CardControlName.RECHECK_EXACT_TURN,
                    CardControlName.REFRESH_ARCHIVED_SESSIONS,
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
            | TurnObservationUnavailableOutcome
            | ThreadActivityDiscardedOutcome
            | CompactionOutcome
            | GoalOutcome
            | SideTurnOutcome
            | SideLifecycleOutcome
        ),
    ) -> None:
        if isinstance(outcome, ThreadActivityDiscardedOutcome):
            await self._progress_cards.abandon_thread(
                binding_id=outcome.binding_id,
                thread_id=outcome.thread_id,
            )
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
                "本次 Codex Turn 在短暂重试后仍无法确认状态，已停止后台读取。"
                "当前会话及上下文仍保留；可发送 `/sessions` 重新检查、停止、"
                "归档或删除这个会话。"
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
        if outcome.task_feedback.progress_card_enabled:
            try:
                progress_delivered = await self._complete_task_progress_card(outcome)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "terminal progress-card delivery failed",
                    extra={
                        "binding_id": getattr(outcome, "binding_id", None),
                        "side_id": getattr(outcome, "side_id", None),
                        "turn_id": outcome.turn_id,
                    },
                )
                await self._abandon_task_progress_card(outcome)
                progress_delivered = False
            if progress_delivered:
                return
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
        await self._complete_task_with_files(outcome)

    async def _complete_task_progress_card(
        self,
        outcome: TurnOutcome | SideTurnOutcome,
    ) -> bool:
        terminal_status = "failed"
        final_response: str
        files: tuple[TurnFile, ...] = ()
        scope: FeishuScope | None = None
        file_provenance_id: str | None = None
        if outcome.error is not None:
            detail = str(outcome.error).strip() or type(outcome.error).__name__
            final_response = f"任务未完成：{detail[:500]}"
        elif outcome.status == "interrupted":
            terminal_status = "interrupted"
            if outcome.background_cleanup_requested:
                final_response = (
                    "Codex Turn 已中断；已请求清理该 Thread 中已登记的后台终端。"
                    "前台工具进程不受此接口保证，可能仍在运行。"
                )
            else:
                final_response = (
                    "Codex Turn 已被外部中断；本服务未请求清理已登记的后台终端。"
                    "前台工具进程可能仍在运行。"
                )
        elif outcome.status != "completed":
            detail = outcome.final_response or f"Codex Turn 状态为 {outcome.status!r}。"
            final_response = f"任务未完成：{detail[:500]}"
        else:
            terminal_status = "completed"
            final_response = outcome.final_response or "任务已结束，未产生文本回复。"
            if has_turn_file_references(
                tuple(getattr(outcome.result, "items", ())),
                turn_diff=self._task_turn_diff(outcome),
            ):
                try:
                    scope, files, file_provenance_id = (
                        self._task_completion_files(outcome)
                    )
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
                    return False

        def render(
            snapshot: TurnActivitySnapshot | SideTurnActivitySnapshot,
        ) -> OutboundCard:
            return turn_progress_card(
                snapshot=snapshot,
                final_response=final_response,
                files=files,
                terminal_status=terminal_status,
                collapsed=True,
                scope=scope,
                binding_id=(file_provenance_id if files else None),
                turn_id=(outcome.turn_id if files else None),
            )

        if isinstance(outcome, TurnOutcome):
            return await self._progress_cards.finish(
                binding_id=outcome.binding_id,
                thread_id=outcome.thread_id,
                turn_id=outcome.turn_id,
                activity=outcome.activity,
                render=render,
            )
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

    @staticmethod
    def _task_turn_diff(outcome: TurnOutcome | SideTurnOutcome) -> str | None:
        return outcome.turn_diff if isinstance(outcome, TurnOutcome) else None

    def _completion_files(
        self,
        outcome: TurnOutcome,
    ) -> tuple[FeishuScope, tuple[TurnFile, ...]]:
        binding = self._bindings.get(outcome.binding_id)
        scope = self._scope(outcome.origin)
        if (
            binding.scope_key != scope.key
            or binding.native_thread_id != outcome.thread_id
        ):
            raise TurnFileError("本轮文件与完成消息的会话身份不一致。")
        project = self._projects.resolve_for_binding(binding.project_alias)
        return scope, extract_turn_files(
            tuple(getattr(outcome.result, "items", ())),
            project.cwd,
            turn_diff=outcome.turn_diff,
        )

    def _side_completion_files(
        self,
        outcome: SideTurnOutcome,
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
        )

    def _task_completion_files(
        self,
        outcome: TurnOutcome | SideTurnOutcome,
    ) -> tuple[FeishuScope, tuple[TurnFile, ...], str]:
        if isinstance(outcome, TurnOutcome):
            scope, files = self._completion_files(outcome)
            return scope, files, outcome.binding_id
        scope, files = self._side_completion_files(outcome)
        # v4 callbacks are self-contained. The captured Parent Binding is
        # provenance and deterministic-action identity only; paging and send
        # never read its current state or reinterpret the Side as a Binding.
        return scope, files, outcome.parent_binding_id

    async def _complete_task_with_files(
        self,
        outcome: TurnOutcome | SideTurnOutcome,
    ) -> None:
        final_response = outcome.final_response or "任务已结束，未产生文本回复。"
        items = tuple(getattr(outcome.result, "items", ()))
        if not has_turn_file_references(
            items,
            turn_diff=self._task_turn_diff(outcome),
        ):
            await self._reply(outcome.origin, final_response)
            return
        try:
            scope, files, file_provenance_id = self._task_completion_files(outcome)
            if not files:
                await self._reply(outcome.origin, final_response)
                return
            card = turn_files_card(
                scope=scope,
                binding_id=file_provenance_id,
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
                    "binding_id": getattr(outcome, "binding_id", None),
                    "side_id": getattr(outcome, "side_id", None),
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
                    "binding_id": getattr(outcome, "binding_id", None),
                    "side_id": getattr(outcome, "side_id", None),
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
        try:
            binding: ThreadBinding | None = self._bindings.get(outcome.binding_id)
        except BindingNotFound:
            binding = None
        if isinstance(outcome.origin, GoalCardOrigin):
            origin = outcome.origin
            scope = origin.scope
            fallback_origin = origin.fallback_origin
            if origin.binding_id != outcome.binding_id:
                logger.error(
                    "terminal Goal origin identity mismatch",
                    extra={"binding_id": outcome.binding_id},
                )
                target = fallback_origin
                if target is not None:
                    await self._reply(
                        target,
                        outcome.final_response or "Goal 已结束，但卡片身份校验失败。",
                    )
                return
        else:
            if binding is None:
                await self._reply(
                    outcome.origin,
                    outcome.final_response or "Goal 已结束，但会话已不存在。",
                )
                return
            scope = self._scope(outcome.origin)
            fallback_origin = outcome.origin
            origin = GoalCardOrigin(
                message_id=None,
                scope=scope,
                binding_id=binding.id,
                short_id=binding.short_id,
                project_alias=binding.project_alias,
                fallback_origin=fallback_origin,
            )
        identity_binding_id = outcome.binding_id
        identity_short_id = (
            binding.short_id if binding is not None else origin.short_id
        )
        identity_project_alias = (
            binding.project_alias if binding is not None else origin.project_alias
        )
        goal = outcome.goal
        status = goal.status.value if goal is not None else "unknown"
        if outcome.error is not None:
            detail = str(outcome.error).strip() or type(outcome.error).__name__
            notice = f"Goal 未能确认终态：{detail[:500]}"
            notice_is_error = True
            runtime_state = GoalOperationState.UNKNOWN.value
            result_text = None
        elif outcome.finalization is GoalFinalizationStatus.UNKNOWN:
            detail = (
                str(outcome.finalization_error).strip()
                if outcome.finalization_error is not None
                else "自动结束结果未确认"
            )
            notice = f"Goal 已完成，但自动结束结果未知：{detail[:500]}"
            notice_is_error = True
            runtime_state = GoalOperationState.UNKNOWN.value
            result_text = (
                outcome.final_response
                or "Goal 已完成，未产生文本回复。"
            )
        elif goal is not None and goal.status is GoalStatus.PAUSED:
            notice = (
                "Goal 已暂停；已请求清理该 Thread 中已登记的后台终端。"
                "前台工具进程不受此接口保证，可能仍在运行。"
            )
            notice_is_error = False
            runtime_state = f"goal-{status}"
            result_text = (
                outcome.final_response
                or "Goal 已暂停，未产生文本回复。"
            )
        else:
            notice = (
                "Goal 已完成并自动结束。"
                if outcome.finalization is GoalFinalizationStatus.CLEARED
                else f"Goal 已进入 {status}。"
            )
            notice_is_error = False
            runtime_state = (
                "goal-cleared"
                if outcome.finalization is GoalFinalizationStatus.CLEARED
                else f"goal-{status}"
            )
            result_text = (
                outcome.final_response
                or f"Goal 已进入 {status}，未产生文本回复。"
            )

        activity = None
        if (
            outcome.task_feedback.progress_card_enabled
            and outcome.activity is not None
            and outcome.final_turn_status in {"completed", "interrupted", "failed"}
        ):
            activity = _reply_activity_module(
                outcome.activity,
                terminal_status=outcome.final_turn_status,
                collapsed=True,
            )
        files: tuple[TurnFile, ...] = ()
        if (
            outcome.final_turn_status == "completed"
            and outcome.final_physical_turn_id is not None
            and has_turn_file_references(outcome.final_items)
        ):
            try:
                if binding is not None and (
                    binding.scope_key != scope.key
                    or binding.native_thread_id != outcome.thread_id
                ):
                    raise TurnFileError("Goal 文件与完成卡片的会话身份不一致。")
                project = self._projects.resolve_for_binding(
                    identity_project_alias
                )
                files = extract_turn_files(outcome.final_items, project.cwd)
            except Exception:
                logger.exception(
                    "failed to prepare terminal Goal Reply Card files",
                    extra={"binding_id": outcome.binding_id},
                )

        projection = ReplyCardProjection(
            scope=scope,
            goal=_reply_goal_module_for_identity(
                binding_id=identity_binding_id,
                short_id=identity_short_id,
                project_alias=identity_project_alias,
                goal=goal,
                runtime_state=runtime_state,
                notice=notice,
                notice_is_error=notice_is_error,
            ),
            activity=activity,
            result=(
                ReplyCardResultModule(result_text)
                if result_text is not None
                else None
            ),
            files=(
                _reply_files_module(
                    binding_id=identity_binding_id,
                    turn_id=outcome.final_physical_turn_id,
                    files=files,
                )
                if files and outcome.final_physical_turn_id is not None
                and result_text is not None
                else None
            ),
        )
        terminal_card: OutboundCard | None = None
        plain_result_required = False
        try:
            terminal_card = reply_card(projection)
        except TurnFileCardLimitError as error:
            if projection.files is None:
                logger.warning(
                    "terminal Goal Reply Card exceeds card limit",
                    extra={"binding_id": outcome.binding_id},
                )
            else:
                logger.warning(
                    "terminal Goal Reply Card files exceed card limit",
                    extra={"binding_id": outcome.binding_id},
                )
                goal_module = projection.goal
                assert goal_module is not None
                projection = replace(
                    projection,
                    goal=replace(
                        goal_module,
                        notice=f"{notice} 文件模块未生成：{error}",
                        notice_is_error=True,
                    ),
                    files=None,
                )
                try:
                    terminal_card = reply_card(projection)
                except Exception:
                    logger.exception(
                        "terminal Goal Reply Card without files could not be rendered",
                        extra={"binding_id": outcome.binding_id},
                    )
        except Exception:
            logger.exception(
                "terminal Goal Reply Card could not be rendered",
                extra={"binding_id": outcome.binding_id},
            )

        if terminal_card is None:
            goal_module = projection.goal
            assert goal_module is not None
            projection = replace(
                projection,
                goal=replace(
                    goal_module,
                    notice=(
                        f"{notice} 结果正文无法完整放入卡片，已另行回复。"
                    ),
                    notice_is_error=True,
                ),
                result=None,
                files=None,
            )
            plain_result_required = result_text is not None
            try:
                terminal_card = reply_card(projection)
            except Exception:
                logger.exception(
                    "compact terminal Goal Reply Card could not be rendered",
                    extra={"binding_id": outcome.binding_id},
                )

        generation = (
            goal_generation(goal)
            if goal is not None
            else origin.goal_generation
        )
        retain_terminal_session = (
            goal is not None
            and outcome.finalization is not GoalFinalizationStatus.CLEARED
        )
        delivery = _GoalCardDelivery.FAILED
        if generation is not None:
            origin.goal_generation = generation
            if terminal_card is not None:
                delivery = await self._progress_cards.finish_goal(
                    binding_id=identity_binding_id,
                    thread_id=outcome.thread_id,
                    logical_turn_id=outcome.logical_turn_id,
                    generation=generation,
                    origin=origin,
                    projection=projection,
                    retain_session=retain_terminal_session,
                )
            else:
                await self._progress_cards.abandon_goal(
                    binding_id=identity_binding_id,
                    thread_id=outcome.thread_id,
                    logical_turn_id=outcome.logical_turn_id,
                    generation=generation,
                )
        target = fallback_origin
        if target is None and origin.message_id is not None:
            target = _CardReplyTarget(
                id=origin.message_id,
                message_id=origin.message_id,
                chat_id=scope.chat_id,
                conversation=_CardReplyConversation(thread_id=scope.topic_id),
            )
        if target is None:
            logger.error("cannot deliver terminal Goal card without reply target")
            return
        if delivery is _GoalCardDelivery.SUPERSEDED:
            return
        if delivery is _GoalCardDelivery.DELIVERED:
            if plain_result_required:
                await self._reply(target, result_text or notice)
            return
        if terminal_card is None:
            await self._reply(target, result_text or notice)
            return
        if generation is None:
            await self._reply(target, terminal_card)
            if plain_result_required:
                await self._reply(target, result_text or notice)
            return
        fallback_delivery = await self._progress_cards.reply_goal_fallback(
            binding_id=identity_binding_id,
            thread_id=outcome.thread_id,
            logical_turn_id=outcome.logical_turn_id,
            generation=generation,
            target=target,
            card=terminal_card,
            origin=origin,
            projection=projection,
            retain_session=retain_terminal_session,
        )
        if fallback_delivery is _GoalCardDelivery.SUPERSEDED:
            return
        if fallback_delivery is _GoalCardDelivery.FAILED:
            await self._reply(target, result_text or notice)
            return
        if plain_result_required:
            await self._reply(target, result_text or notice)

    async def _present_running_goal(
        self,
        *,
        binding: ThreadBinding,
        scope: FeishuScope,
        submission: GoalSubmission,
        origin: GoalCardOrigin,
        notice: str,
    ) -> bool:
        snapshot = await self._runtime.goal_snapshot(binding)
        if snapshot is None:
            return False
        generation = goal_generation(snapshot)
        origin.goal_generation = generation
        activity_enabled = submission.task_feedback.progress_card_enabled

        def current_projection(
            goal_snapshot: GoalSnapshot,
            activity_snapshot: GoalActivitySnapshot | None,
            *,
            runtime_state: str,
            card_notice: str | None,
        ) -> ReplyCardProjection:
            return ReplyCardProjection(
                scope=scope,
                goal=_reply_goal_module(
                    binding=binding,
                    goal=goal_snapshot,
                    runtime_state=runtime_state,
                    notice=card_notice,
                ),
                activity=(
                    _reply_activity_module(activity_snapshot)
                    if activity_enabled and activity_snapshot is not None
                    else None
                ),
            )

        activity = (
            self._runtime.goal_activity(
                binding.id,
                thread_id=submission.thread_id,
                logical_turn_id=submission.logical_turn_id,
                refresh_plan=True,
            )
            if activity_enabled
            else None
        )
        projection = current_projection(
            snapshot,
            activity,
            runtime_state=GoalOperationState.RUNNING.value,
            card_notice=notice,
        )

        return await self._progress_cards.start_goal(
            binding_id=binding.id,
            thread_id=submission.thread_id,
            logical_turn_id=submission.logical_turn_id,
            generation=generation,
            origin=origin,
            projection=projection,
            revision=(
                snapshot.updated_at,
                GoalOperationState.RUNNING.value,
                None if activity is None else activity.revision,
            ),
            refresh=self._goal_refresh_callback(
                binding=binding,
                scope=scope,
                thread_id=submission.thread_id,
                logical_turn_id=submission.logical_turn_id,
                activity_enabled=activity_enabled,
            ),
        )

    def _goal_refresh_callback(
        self,
        *,
        binding: ThreadBinding,
        scope: FeishuScope,
        thread_id: str,
        logical_turn_id: str,
        activity_enabled: bool,
    ) -> Callable[
        [],
        Awaitable[tuple[object, ReplyCardProjection] | None],
    ]:
        """Build the exact-run refresh used by start/resume and `/goal` recovery."""

        async def refresh() -> tuple[object, ReplyCardProjection] | None:
            current = await self._runtime.goal_snapshot(binding)
            active = self._runtime.active_goal(binding.id)
            if current is None or active is None:
                return None
            display_goal = current
            if (
                active.state
                in {
                    GoalOperationState.STARTING,
                    GoalOperationState.RUNNING,
                    GoalOperationState.PAUSING,
                }
                and current.status is not GoalStatus.ACTIVE
            ):
                display_goal = replace(current, status=GoalStatus.ACTIVE)
            current_activity = (
                self._runtime.goal_activity(
                    binding.id,
                    thread_id=thread_id,
                    logical_turn_id=logical_turn_id,
                    refresh_plan=True,
                )
                if activity_enabled
                else None
            )
            revision = (
                current.updated_at,
                active.state.value,
                None if current_activity is None else current_activity.revision,
            )
            return revision, ReplyCardProjection(
                scope=scope,
                goal=_reply_goal_module(
                    binding=binding,
                    goal=display_goal,
                    runtime_state=active.state.value,
                ),
                activity=(
                    _reply_activity_module(current_activity)
                    if activity_enabled and current_activity is not None
                    else None
                ),
            )

        return refresh

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
                "当前聊天或话题还没有会话，请先发送 /new。",
            )
            return
        project = self._projects.resolve_for_binding(binding.project_alias)
        target_id = quoted_message_id(message)
        current = project_current_message(
            message,
            expected_message_id=prompt.source_id,
            expected_sender_id=prompt.sender_id,
            message_type=normalized_message_type(message),
            content_fidelity=(
                "full_multimodal" if current_images else "full_text"
            ),
            request_text=prompt.text,
        )
        admission = await self._runtime.capture_submission_admission(binding.id)
        context_commit = None
        if binding.message_context_mode is MentionContextMode.CATCH_UP:
            self._require_catch_up_message_scope(message, prompt.scope)
            if binding.context_anchor is None:
                raise MessageHistoryUnavailable(
                    "当前会话缺少群聊上下文边界，本条消息未执行；"
                    "请重新发送 /config 并重新选择 @ 时读取的消息范围。"
                )
            input_value, context_commit, context_stats = (
                await self._compose_catch_up_prompt_input(
                    source_message=message,
                    scope=prompt.scope,
                    lower=binding.context_anchor,
                    upper_id=prompt.source_id,
                    quoted_target_id=target_id,
                    current=current,
                    current_images=current_images,
                    expected_context_revision=admission.context_revision,
                )
            )
            await self._send_context_receipt(message, context_stats)
        else:
            input_value = await self._compose_prompt_input(
                source_message=message,
                quoted_target_id=target_id,
                current=current,
                current_images=current_images,
            )

        submit_kwargs: dict[str, Any] = dict(
            binding=binding,
            cwd=project.cwd,
            input=input_value,
            owner_id=prompt.sender_id,
            origin=message,
            skill_names=prompt.skill_names,
            context_commit=context_commit,
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
            presenters: list[Awaitable[bool]] = [
                self._reactions.start(
                    submission.turn_id,
                    _message_id(message),
                    pulse_enabled=(
                        submission.task_feedback.reaction_pulse_enabled
                    ),
                )
            ]
            if submission.task_feedback.progress_card_enabled:
                presenters.append(
                    self._progress_cards.start(
                        binding_id=submission.binding_id,
                        thread_id=submission.thread_id,
                        turn_id=submission.turn_id,
                        origin=message,
                    )
                )
            if presenters:
                await asyncio.gather(*presenters)
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
        current = project_current_message(
            source_message,
            expected_message_id=prompt.source_id,
            expected_sender_id=prompt.sender_id,
            message_type=normalized_message_type(source_message),
            content_fidelity=(
                "full_multimodal" if current_images else "full_text"
            ),
            request_text=prompt.text,
        )
        input_value = await self._compose_prompt_input(
            source_message=source_message,
            quoted_target_id=quoted_target_id,
            current=current,
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
            presenters: list[Awaitable[bool]] = [
                self._reactions.start(
                    submission.turn_id,
                    _message_id(reply_origin),
                    pulse_enabled=(
                        submission.task_feedback.reaction_pulse_enabled
                    ),
                )
            ]
            if submission.task_feedback.progress_card_enabled:
                presenters.append(
                    self._progress_cards.start_side(
                        side_id=submission.side_id,
                        thread_id=submission.thread_id,
                        turn_id=submission.turn_id,
                        origin=reply_origin,
                    )
                )
            if presenters:
                await asyncio.gather(*presenters)
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
                "当前聊天或话题还没有会话，请先发送 /new。",
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
        quoted_target_id: str | None,
        current: CurrentMessageProjection,
        current_images: tuple[ImageReference, ...],
    ) -> Any:
        try:
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

    async def _compose_catch_up_prompt_input(
        self,
        *,
        source_message: Any,
        scope: FeishuScope,
        lower: MessageContextAnchor,
        upper_id: str,
        quoted_target_id: str | None,
        current: CurrentMessageProjection,
        current_images: tuple[ImageReference, ...],
        expected_context_revision: int,
    ) -> tuple[Any, ContextCursorCommit, SupplementalContextStats]:
        reader = self._message_history
        if reader is None:
            raise MessageHistoryUnavailable(
                "群聊上下文读取能力尚不可用，本条消息未执行；请联系维护者。"
            )

        try:
            async with asyncio.timeout(_CONTEXT_PREPARATION_TIMEOUT_SECONDS):
                window = await reader.read_window(scope, lower, upper_id)
                fetched = await self._fetch_history_candidates(
                    scope,
                    window.candidates,
                )
                by_id = {
                    reference.message_id: value
                    for reference, value in zip(window.candidates, fetched)
                }
                attribution_names = {
                    reference.message_id: reference.sender_name
                    for reference in window.candidates
                }

                quoted_input: tuple[Any, str | None] | None = None
                quoted_projection: HistoricalMessageProjection | None = None
                if quoted_target_id is not None:
                    quoted_input = by_id.get(quoted_target_id)
                    if quoted_input is None:
                        quoted_input = await self._fetch_normalized_history_message(
                            quoted_target_id
                        )
                    quoted_message, quoted_fallback = quoted_input
                    validate_quoted_message(
                        quoted_message,
                        expected_message_id=quoted_target_id,
                        expected_chat_id=scope.chat_id,
                    )
                    quoted_projection = project_quoted_message(
                        quoted_message,
                        interactive_fallback_text=quoted_fallback,
                    )

                eligible_inputs: list[
                    tuple[Any, str | None, HistoricalMessageProjection]
                ] = []
                projection_omissions: list[SupplementalMessageOmission] = []
                for message, fallback in fetched:
                    projection = project_supplemental_message(
                        message,
                        interactive_fallback_text=fallback,
                        attribution_name=attribution_names.get(_message_id(message)),
                    )
                    if isinstance(projection, SupplementalMessageOmission):
                        projection_omissions.append(projection)
                    else:
                        eligible_inputs.append((message, fallback, projection))

                supplemental_stats = SupplementalContextStats(
                    scanned_count=window.stats.raw_messages_scanned,
                    omitted_count=(
                        window.stats.omitted_messages + len(projection_omissions)
                    ),
                    unsupported_omitted_count=sum(
                        omission.reason == "unsupported_message_type"
                        for omission in projection_omissions
                    ),
                    truncated_before=window.stats.truncated_before,
                    message_limit_reached=window.stats.scan_limit_hit,
                )
                selection = select_supplemental_messages(
                    tuple(item[2] for item in eligible_inputs),
                    quoted_message_id=(
                        quoted_projection.message_id
                        if quoted_projection is not None
                        else None
                    ),
                    supplemental_stats=supplemental_stats,
                    max_supplemental_messages=_CONTEXT_MESSAGE_LIMIT,
                    max_supplemental_text=_CONTEXT_TEXT_LIMIT,
                )
                image_eligible_ids = frozenset(
                    projection.message_id for projection in selection.messages
                )

                supplemental_image_references: list[ImageReference] = []
                for message, _, projection in eligible_inputs:
                    if projection.message_id not in image_eligible_ids:
                        continue
                    supplemental_image_references.extend(
                        image_references(
                            message,
                            source="supplemental_message",
                        )
                    )
                quoted_image_references: tuple[ImageReference, ...] = ()
                if quoted_input is not None:
                    quoted_image_references = image_references(
                        quoted_input[0],
                        source="quoted_message",
                    )
        except (MessageHistoryError, HistoricalMessageError, ImageInputError):
            raise
        except TimeoutError as error:
            raise MessageHistoryUnavailable(
                "读取并整理群聊上下文超过时间限制，本条消息未执行；请重试。"
            ) from error
        except Exception as error:
            logger.warning(
                "catch-up context preparation failed",
                extra={"error_type": type(error).__name__},
            )
            raise MessageHistoryUnavailable(
                "无法安全整理群聊上下文，本条消息未执行；请重试。"
            ) from error

        prepared_images = await prepare_images(
            self._channel,
            tuple(supplemental_image_references)
            + quoted_image_references
            + current_images,
        )

        selected_inputs = {
            projection.message_id: (message, fallback)
            for message, fallback, projection in eligible_inputs
            if projection.message_id in image_eligible_ids
        }
        supplemental_projections: list[HistoricalMessageProjection] = []
        for selected in selection.messages:
            assert selected.message_id is not None
            message, fallback = selected_inputs[selected.message_id]
            projection = project_supplemental_message(
                message,
                interactive_fallback_text=fallback,
                read_image_keys=self._prepared_image_keys(
                    prepared_images,
                    source="supplemental_message",
                    message_id=_message_id(message),
                ),
                attribution_name=attribution_names.get(_message_id(message)),
            )
            if isinstance(projection, SupplementalMessageOmission):
                raise MessageHistoryUnavailable(
                    "补充上下文消息在整理期间发生变化，本条消息未执行；请重试。"
                )
            supplemental_projections.append(projection)
        selection = selection.reproject(supplemental_projections)
        final_supplemental_ids = frozenset(
            projection.message_id for projection in selection.messages
        )
        prepared_images = tuple(
            image
            for image in prepared_images
            if image.reference.source != "supplemental_message"
            or image.reference.message_id in final_supplemental_ids
        )

        final_quoted_projection = None
        if quoted_input is not None:
            quoted_message, quoted_fallback = quoted_input
            final_quoted_projection = project_quoted_message(
                quoted_message,
                interactive_fallback_text=quoted_fallback,
                read_image_keys=self._prepared_image_keys(
                    prepared_images,
                    source="quoted_message",
                    message_id=_message_id(quoted_message),
                ),
            )

        context = compose_message_context_prompt(
            supplemental_selection=selection,
            quoted_message=final_quoted_projection,
            current=current,
        )
        return (
            compose_multimodal_input(context.text, images=prepared_images),
            ContextCursorCommit(
                expected_context_revision=expected_context_revision,
                anchor=window.upper,
            ),
            context.stats,
        )

    async def _fetch_history_candidates(
        self,
        scope: FeishuScope,
        references: tuple[MessageHistoryRef, ...],
    ) -> tuple[tuple[Any, str | None], ...]:
        semaphore = asyncio.Semaphore(_CONTEXT_FETCH_CONCURRENCY)

        async def fetch(
            reference: MessageHistoryRef,
        ) -> tuple[Any, str | None]:
            async with semaphore:
                value = await self._fetch_normalized_history_message(
                    reference.message_id
                )
                self._validate_history_candidate(scope, reference, value[0])
                return value

        tasks = tuple(asyncio.create_task(fetch(reference)) for reference in references)
        if not tasks:
            return ()
        try:
            return tuple(await asyncio.gather(*tasks))
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _fetch_normalized_history_message(
        self,
        message_id: str,
    ) -> tuple[Any, str | None]:
        try:
            async with asyncio.timeout(_CONTEXT_FETCH_TIMEOUT_SECONDS):
                message = await self._channel.fetch_inbound_message(message_id)
        except TimeoutError as error:
            raise MessageHistoryUnavailable(
                "读取补充上下文消息超时，本条消息未执行；请重试。"
            ) from error
        except Exception as error:
            raise MessageHistoryUnavailable(
                "无法读取补充上下文消息，本条消息未执行；请重试。"
            ) from error
        if message is None:
            raise MessageHistoryUnavailable(
                "补充上下文消息已不可读取，本条消息未执行；请重试。"
            )

        fallback_text = None
        if needs_interactive_fallback(message):
            try:
                async with asyncio.timeout(_CONTEXT_FETCH_TIMEOUT_SECONDS):
                    fallback = await self._channel.fetch_quoted_context(message_id)
            except TimeoutError as error:
                raise MessageHistoryUnavailable(
                    "读取历史应用消息可见内容超时，本条消息未执行；请重试。"
                ) from error
            except Exception as error:
                raise MessageHistoryUnavailable(
                    "无法读取历史应用消息可见内容，本条消息未执行；请重试。"
                ) from error
            if (
                fallback is None
                or getattr(fallback, "message_id", None) != message_id
                or getattr(fallback, "content_type", None) != "interactive"
            ):
                raise MessageHistoryUnavailable(
                    "历史应用消息没有可验证的可见内容，本条消息未执行；请重试。"
                )
            fallback_text = interactive_quote_visible_text(fallback)
        return message, fallback_text

    @staticmethod
    def _validate_history_candidate(
        scope: FeishuScope,
        reference: MessageHistoryRef,
        message: Any,
    ) -> None:
        if _message_id(message) != reference.message_id:
            raise MessageHistoryUnavailable(
                "补充上下文 exact message ID 不一致，本条消息未执行。"
            )
        conversation = getattr(message, "conversation", None)
        if str(getattr(conversation, "chat_id", "") or "") != scope.chat_id:
            raise MessageHistoryUnavailable(
                "补充上下文消息不属于当前会话，本条消息未执行。"
            )
        thread_id = str(getattr(conversation, "thread_id", "") or "") or None
        if scope.kind is ScopeKind.GROUP and thread_id is not None:
            raise MessageHistoryUnavailable(
                "补充上下文消息不属于当前群聊主线，本条消息未执行。"
            )
        if scope.kind is ScopeKind.TOPIC and thread_id != scope.topic_id:
            raise MessageHistoryUnavailable(
                "补充上下文消息不属于当前话题，本条消息未执行。"
            )
        create_time = getattr(message, "create_time", None)
        if isinstance(create_time, bool):
            actual_create_time = None
        elif isinstance(create_time, int):
            actual_create_time = create_time
        elif isinstance(create_time, str) and create_time.isdigit():
            actual_create_time = int(create_time)
        else:
            actual_create_time = None
        if actual_create_time != reference.create_time_ms:
            raise MessageHistoryUnavailable(
                "补充上下文消息时间与历史索引不一致，本条消息未执行；请重试。"
            )
        if historical_message_deleted(message):
            return
        sender = getattr(message, "sender", None)
        sender_id = str(getattr(sender, "open_id", "") or "")
        if (
            sender_id != reference.sender_id
            or bool(getattr(sender, "is_bot", False))
            or str(getattr(sender, "sender_type", "user") or "") != "user"
        ):
            raise MessageHistoryUnavailable(
                "补充上下文消息发送者与历史索引不一致，本条消息未执行；请重试。"
            )
        actual_type = normalized_historical_message_type(message)
        expected_type = "media" if reference.message_type == "video" else reference.message_type
        if actual_type != expected_type:
            raise MessageHistoryUnavailable(
                "补充上下文消息类型与历史索引不一致，本条消息未执行；请重试。"
            )

    @staticmethod
    def _prepared_image_keys(
        images: Sequence[Any],
        *,
        source: str,
        message_id: str,
    ) -> tuple[str, ...]:
        return tuple(
            image.reference.file_key
            for image in images
            if image.reference.source == source
            and image.reference.message_id == message_id
        )

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
            async with asyncio.timeout(_CONTEXT_RECEIPT_TIMEOUT_SECONDS):
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
                    "当前聊天或话题还没有会话，请先发送 /new。",
                )
                return
            project = self._projects.resolve_for_binding(binding.project_alias)
            argument = intent.arguments[0] if intent.arguments else None
            action = argument.lower() if argument is not None else None
            if argument is None:
                snapshot = await self._runtime.goal_snapshot(binding)
                active_goal = self._runtime.active_goal(binding.id)
                unresolved_goal = (
                    snapshot is None
                    and active_goal is not None
                    and active_goal.state is GoalOperationState.UNKNOWN
                )
                activity = None
                if (
                    binding.task_feedback.progress_card_enabled
                    and active_goal is not None
                    and active_goal.logical_turn_id is not None
                ):
                    activity = self._runtime.goal_activity(
                        binding.id,
                        thread_id=active_goal.thread_id,
                        logical_turn_id=active_goal.logical_turn_id,
                        refresh_plan=True,
                    )
                projection = ReplyCardProjection(
                    scope=intent.scope,
                    goal=_reply_goal_module(
                        binding=binding,
                        goal=snapshot,
                        runtime_state=(
                            active_goal.state.value
                            if active_goal is not None
                            else None
                        ),
                        notice=(
                            "Goal 状态未确认；服务仍保留该会话占用，"
                            "不会把 absent 读取解释为已清除。"
                            if unresolved_goal
                            else None
                        ),
                        notice_is_error=unresolved_goal,
                    ),
                    activity=(
                        _reply_activity_module(activity)
                        if activity is not None
                        else None
                    ),
                )
                if snapshot is not None:
                    generation = goal_generation(snapshot)
                    message_id = self._progress_cards.goal_message_id(
                        binding_id=binding.id,
                        thread_id=snapshot.thread_id,
                        generation=generation,
                    )
                    if (
                        message_id is not None
                        and await self._progress_cards.refresh_goal_snapshot(
                            source_id=message_id,
                            generation=generation,
                            logical_turn_id=(
                                active_goal.logical_turn_id
                                if active_goal is not None
                                else None
                            ),
                            projection=projection,
                        )
                    ):
                        return
                    snapshot_origin = GoalCardOrigin(
                        message_id=None,
                        scope=intent.scope,
                        binding_id=binding.id,
                        short_id=binding.short_id,
                        project_alias=binding.project_alias,
                        fallback_origin=message,
                        goal_generation=generation,
                    )
                    logical_turn_id = (
                        active_goal.logical_turn_id
                        if active_goal is not None
                        and active_goal.logical_turn_id is not None
                        else f"snapshot:{generation}"
                    )
                    refresh = (
                        self._goal_refresh_callback(
                            binding=binding,
                            scope=intent.scope,
                            thread_id=snapshot.thread_id,
                            logical_turn_id=logical_turn_id,
                            activity_enabled=(
                                binding.task_feedback.progress_card_enabled
                            ),
                        )
                        if active_goal is not None
                        and active_goal.logical_turn_id is not None
                        and active_goal.state
                        in {
                            GoalOperationState.STARTING,
                            GoalOperationState.RUNNING,
                            GoalOperationState.PAUSING,
                        }
                        else None
                    )
                    if await self._progress_cards.start_goal(
                        binding_id=binding.id,
                        thread_id=snapshot.thread_id,
                        logical_turn_id=logical_turn_id,
                        generation=generation,
                        origin=snapshot_origin,
                        projection=projection,
                        revision=("snapshot", snapshot.updated_at),
                        refresh=refresh,
                    ):
                        return
                    await self._reply(
                        message,
                        "Goal 状态已读取，但状态卡暂时无法展示。",
                    )
                    return
                await self._reply(message, reply_card(projection))
                return
            if action == "pause":
                goal_before_pause = await self._runtime.goal_snapshot(binding)
                if goal_before_pause is None:
                    await self._reply(message, "当前没有 Goal。")
                    return
                generation = goal_generation(goal_before_pause)
                message_id = self._progress_cards.goal_message_id(
                    binding_id=binding.id,
                    thread_id=goal_before_pause.thread_id,
                    generation=generation,
                )

                async def acknowledge_goal_pause() -> None:
                    if message_id is None:
                        return
                    active_goal = self._runtime.active_goal(binding.id)
                    activity = None
                    if (
                        binding.task_feedback.progress_card_enabled
                        and active_goal is not None
                        and active_goal.logical_turn_id is not None
                    ):
                        activity = self._runtime.goal_activity(
                            binding.id,
                            thread_id=active_goal.thread_id,
                            logical_turn_id=active_goal.logical_turn_id,
                        )
                    await self._progress_cards.update_goal(
                        source_id=message_id,
                        generation=generation,
                        projection=ReplyCardProjection(
                            scope=intent.scope,
                            goal=_reply_goal_module(
                                binding=binding,
                                goal=goal_before_pause,
                                runtime_state=GoalOperationState.PAUSING.value,
                                notice=(
                                    "正在暂停 Goal 并中断当前物理 Turn。"
                                ),
                            ),
                            activity=(
                                _reply_activity_module(activity)
                                if activity is not None
                                else None
                            ),
                        ),
                    )

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
                elif result is StopDisposition.NOT_RUNNING:
                    await self._reply(message, "当前没有本服务可控的 running Goal。")
                return
            if action == "resume":
                goal_before_resume = await self._runtime.goal_snapshot(binding)
                if goal_before_resume is None:
                    await self._reply(message, "当前没有 Goal。")
                    return
                generation = goal_generation(goal_before_resume)
                message_id = self._progress_cards.goal_message_id(
                    binding_id=binding.id,
                    thread_id=goal_before_resume.thread_id,
                    generation=generation,
                )
                origin = GoalCardOrigin(
                    message_id=message_id,
                    scope=intent.scope,
                    binding_id=binding.id,
                    short_id=binding.short_id,
                    project_alias=binding.project_alias,
                    fallback_origin=message,
                )
                submission = await self._runtime.resume_goal(
                    binding=binding,
                    owner_id=intent.sender_id,
                    origin=origin,
                    expected_created_at=goal_before_resume.created_at,
                )
                try:
                    presented = await self._present_running_goal(
                        binding=binding,
                        scope=intent.scope,
                        submission=submission,
                        origin=origin,
                        notice="Goal 已恢复。",
                    )
                    if not presented:
                        await self._reply(
                            message,
                            "Goal 已恢复并在原生 Codex 中执行，"
                            "但状态卡暂时无法展示。",
                        )
                finally:
                    submission.release_receipt_attempt()
                return
            if action == "clear":
                before = await self._runtime.goal_snapshot(binding)
                generation = None if before is None else goal_generation(before)
                message_id = (
                    None
                    if before is None
                    else self._progress_cards.goal_message_id(
                        binding_id=binding.id,
                        thread_id=before.thread_id,
                        generation=generation,
                    )
                )
                cleared = await self._runtime.clear_goal(
                    binding,
                    expected_created_at=(
                        None if before is None else before.created_at
                    ),
                )
                goal_module = _reply_goal_module(
                    binding=binding,
                    goal=None,
                    notice=("Goal 已结束。" if cleared else "当前没有 Goal。"),
                )
                if (
                    message_id is not None
                    and generation is not None
                    and await self._progress_cards.update_goal_module(
                        source_id=message_id,
                        generation=generation,
                        scope=intent.scope,
                        goal=goal_module,
                        retain_session=False,
                    )
                ):
                    return
                await self._reply(
                    message,
                    reply_card(
                        ReplyCardProjection(
                            scope=intent.scope,
                            goal=goal_module,
                        )
                    ),
                )
                return
            assert argument is not None
            origin = GoalCardOrigin(
                message_id=None,
                scope=intent.scope,
                binding_id=binding.id,
                short_id=binding.short_id,
                project_alias=binding.project_alias,
                fallback_origin=message,
            )
            submission = await self._runtime.start_goal(
                binding=binding,
                cwd=project.cwd,
                objective=argument,
                owner_id=intent.sender_id,
                origin=origin,
            )
            try:
                presented = await self._present_running_goal(
                    binding=self._bindings.get(binding.id),
                    scope=intent.scope,
                    submission=submission,
                    origin=origin,
                    notice="Goal 已启动；原生 Codex 可自动继续多个物理 Turn。",
                )
                if not presented:
                    await self._reply(
                        message,
                        "Goal 已启动并在原生 Codex 中执行，"
                        "但状态卡暂时无法展示。",
                    )
            finally:
                submission.release_receipt_attempt()
            return
        if intent.name is ControlName.CONFIG:
            binding = self._bindings.active_binding(intent.scope.key)
            if binding is None:
                await self._reply(
                    message,
                    "当前聊天或话题还没有会话，请先发送 /new。",
                )
                return
            goal = await self._runtime.goal_snapshot(binding)
            active_goal = self._runtime.active_goal(binding.id)
            if active_goal is not None or (
                goal is not None and goal.status is GoalStatus.ACTIVE
            ):
                await self._reply(
                    message,
                    "当前 Goal 正在执行或状态未完成，不能修改会话配置；"
                    "请先暂停 Goal。",
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
            return
        if intent.name is ControlName.COMPACT:
            binding = self._bindings.active_binding(intent.scope.key)
            if binding is None:
                await self._reply(
                    message,
                    "当前聊天或话题还没有会话，请先发送 /new。",
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
            card = new_binding_card(
                scope=intent.scope,
                projects=projects,
                catalog=catalog,
                catalog_error=catalog_error,
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
            return
        if intent.name is ControlName.SESSIONS:
            archived_view = bool(intent.arguments)
            if archived_view:
                await self._reply(
                    message,
                    await self._archived_sessions_card(scope=intent.scope),
                )
                return

            await self._reply(
                message,
                await self._sessions_card(scope=intent.scope, page=0),
            )
            return
        if intent.name is ControlName.RESUME:
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
                f"已切换到会话 {binding.short_id}（{binding.project_alias}）。",
            )
            return
        if intent.name is ControlName.UNARCHIVE:
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
                        *context_mode_lines,
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
            if (
                intent.name is TurnFileActionName.PAGE
                and intent.reply is not None
                and intent.reply.goal is not None
            ):
                async with self._scope_coordinator.hold(intent.scope.key):
                    await self._turn_file_action(intent)
            else:
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
            if intent.reply is not None:
                if intent.reply.goal is not None:
                    frozen = await self._validated_goal_page_manifest(intent)

                    def render_goal_page(
                        current: ReplyCardProjection | None,
                    ) -> OutboundCard:
                        reply = frozen
                        if current is not None:
                            current_files = current.files
                            if (
                                current.result is None
                                or current_files is None
                                or current_files.binding_id != intent.binding_id
                                or current_files.turn_id != intent.turn_id
                                or tuple(
                                    (item.path, item.label)
                                    for item in current_files.items
                                )
                                != tuple(
                                    (item.path, item.label)
                                    for item in intent.files
                                )
                            ):
                                raise CardActionError(
                                    "Goal 结果或文件已变化，本次翻页未执行。"
                                )
                            reply = ReplyCardManifest(
                                goal=current.goal,
                                activity=current.activity,
                                result=current.result,
                            )
                        return reply_card_from_manifest(
                            scope=intent.scope,
                            binding_id=intent.binding_id,
                            turn_id=intent.turn_id,
                            manifest=intent.files,
                            reply=reply,
                            page=intent.page,
                        )

                    updated = await self._progress_cards.update_goal_page(
                        source_id=intent.source_id,
                        binding_id=intent.binding_id,
                        generation=intent.reply.goal.goal_generation or "",
                        page=intent.page,
                        render=render_goal_page,
                    )
                    if not updated:
                        raise TurnFileError("飞书未确认本轮文件卡片翻页成功。")
                    return
                card = reply_card_from_manifest(
                    scope=intent.scope,
                    binding_id=intent.binding_id,
                    turn_id=intent.turn_id,
                    manifest=intent.files,
                    reply=intent.reply,
                    page=intent.page,
                )
            elif intent.progress is None:
                card = turn_files_card_from_manifest(
                    scope=intent.scope,
                    binding_id=intent.binding_id,
                    turn_id=intent.turn_id,
                    final_response=intent.answer,
                    manifest=intent.files,
                    page=intent.page,
                )
            else:
                card = turn_progress_card_from_manifest(
                    scope=intent.scope,
                    binding_id=intent.binding_id,
                    turn_id=intent.turn_id,
                    final_response=intent.answer,
                    manifest=intent.files,
                    progress=intent.progress,
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

    async def _validated_goal_page_manifest(
        self,
        intent: TurnFileActionIntent,
    ) -> ReplyCardManifest:
        """Reject a stale Goal page callback before it can replace the card."""

        reply = intent.reply
        assert reply is not None and reply.goal is not None
        frozen = reply.goal
        try:
            binding = self._bindings.get(intent.binding_id)
        except BindingNotFound:
            # v5 file callbacks remain self-contained across restart and exact
            # Binding deletion.  Filesystem validation still occurs while the
            # card is rebuilt.
            return reply
        if binding.scope_key != intent.scope.key:
            raise CardActionError("Goal 文件卡片与当前会话不一致。")
        current = await self._runtime.goal_snapshot(binding)
        if frozen.status is None or frozen.runtime_state == "goal-cleared":
            # A cleared card is a frozen, self-contained record of its exact
            # completed Goal.  A later Goal owns another generation/card and
            # cannot be overwritten by paging this historical result.
            return reply
        if (
            current is None
            or current.thread_id != binding.native_thread_id
            or goal_generation(current) != frozen.goal_generation
            or current.status.value != frozen.status
        ):
            raise CardActionError(
                "Goal 已变化，本次翻页未执行；请重新发送 /goal。"
            )
        return reply

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
            assert intent.reaction_pulse_enabled is not None
            assert intent.progress_card_enabled is not None
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
            )
            updated = await self._safe_update_card(
                intent.source_id,
                binding_created_card(
                    short_id=binding.short_id,
                    project_alias=project.alias,
                    settings=settings,
                    task_feedback=binding.task_feedback,
                    message_context_mode=binding.message_context_mode,
                ),
            )
            if not updated:
                await self._safe_reply_to_card(
                    intent,
                    f"✅ Project 选择成功：已选择 `{project.alias}`，"
                    f"并创建、切换到会话 `{binding.short_id}`。"
                    f"Model 来源：{'继承 Codex' if settings is None else '显式配置'}；"
                    f"@ 时读取的消息范围："
                    f"{context_mode_display(binding.message_context_mode)}。"
                    f"执行中表情闪烁："
                    f"{'开启' if binding.task_feedback.reaction_pulse_enabled else '关闭'}；"
                    f"进度卡：{'开启' if binding.task_feedback.progress_card_enabled else '关闭'}。"
                    "现在可以直接发送任务。",
                )
            return
        if intent.name is CardControlName.CONFIGURE_BINDING:
            assert intent.binding_id is not None
            assert intent.expected_settings_revision is not None
            assert intent.expected_context_revision is not None
            assert intent.feedback_revision is not None
            assert intent.message_context_mode is not None
            assert intent.reaction_pulse_enabled is not None
            assert intent.progress_card_enabled is not None
            settings = await self._resolve_card_model_settings(intent)
            task_feedback = BindingTaskFeedback(
                reaction_pulse_enabled=bool(intent.reaction_pulse_enabled),
                progress_card_enabled=bool(intent.progress_card_enabled),
            )
            before = self._bindings.get(intent.binding_id)
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
                    f"进度卡：{'开启' if binding.task_feedback.progress_card_enabled else '关闭'}。"
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
                            "✅ 原生 Codex 会话及本地 Binding 已永久删除，"
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
            return
        if intent.name is CardControlName.UNARCHIVE_BINDING:
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
                    message="✅ 已恢复原生 Codex 会话，并切换为当前会话。",
                ),
            )
            if not updated:
                await self._safe_reply_to_card(
                    intent,
                    f"✅ 已恢复并切换到会话 {restored.short_id}。",
                )
            return
        if intent.name is CardControlName.ACTIVATE_BINDING:
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
                f"✅ 已切换到会话 {activated.short_id}"
                f"（{activated.project_alias}）。"
            )
            try:
                refreshed = await self._safe_update_card(
                    intent.source_id,
                    await self._sessions_card(
                        scope=intent.scope,
                        page=0,
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
            return
        if intent.name is CardControlName.ARCHIVE_EXACT_BINDING:
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
            return
        if intent.name is CardControlName.PREPARE_EXACT_DELETE_BINDING:
            assert intent.binding_id is not None
            assert intent.page is not None
            binding = self._bindings.get(intent.binding_id)
            if binding.scope_key != intent.scope.key:
                raise BindingNotFound(intent.binding_id)
            if binding.native_thread_id != intent.expected_native_thread_id:
                raise CardActionError(
                    "会话的原生历史已变化，请重新发送 /sessions 后再删除。"
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
            metadata = (
                await self._read_thread_metadata((binding,))
                if binding.native_thread_id is not None
                else {}
            )
            confirmation = sessions_delete_binding_card(
                scope=intent.scope,
                binding_id=binding.id,
                short_id=binding.short_id,
                project_alias=binding.project_alias,
                title=_session_title(
                    binding,
                    metadata.get(binding.native_thread_id),
                ),
                native_thread_id=binding.native_thread_id,
                page=intent.page,
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
            return
        if intent.name is CardControlName.DELETE_EXACT_BINDING:
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
                    "会话的原生历史已变化，本次删除未执行；"
                    "请重新发送 /sessions。"
                ) from error
            success_notice = (
                (
                    f"✅ 已永久删除原生 Codex 会话 {deleted.short_id}"
                    f"（{deleted.project_alias}）及本地 Binding。"
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
            return
        if intent.name is CardControlName.STOP_EXACT_BINDING:
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
                    "已请求中断 exact Codex Turn；确认终态前会话仍显示为停止中。"
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
                        notice=notice,
                    ),
                )
            except Exception:
                logger.exception("failed to rebuild sessions card after exact stop")
                refreshed = False
            if not refreshed:
                await self._safe_reply_to_card(intent, notice)
            return
        if intent.name is CardControlName.RECHECK_EXACT_TURN:
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
                "已启动一次有界的 exact Turn 状态重读；"
                "成功后会恢复正常观察，仍不可验证则停止后台读取。"
            )
            try:
                refreshed = await self._safe_update_card(
                    intent.source_id,
                    await self._sessions_card(
                        scope=intent.scope,
                        page=intent.page,
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
            return
        if intent.name is CardControlName.PREPARE_ARCHIVED_DELETE_BINDING:
            assert intent.binding_id is not None
            assert intent.expected_native_thread_id is not None
            binding = self._bindings.get(intent.binding_id)
            if binding.scope_key != intent.scope.key:
                raise BindingNotFound(intent.binding_id)
            if binding.native_thread_id != intent.expected_native_thread_id:
                raise CardActionError(
                    "归档会话的原生历史已变化，请刷新后再删除。"
                )
            if NativeCapability.DELETE not in self._runtime.available_capabilities:
                raise ThreadDeleteUnavailable(
                    "当前 SDK/App Server 的 Thread Delete 兼容契约未通过；"
                    "本次未调用 Codex。"
                )
            metadata = await self._read_thread_metadata(
                (binding,),
                archived=True,
            )
            confirmation = archived_sessions_delete_binding_card(
                scope=intent.scope,
                binding_id=binding.id,
                short_id=binding.short_id,
                project_alias=binding.project_alias,
                title=_session_title(
                    binding,
                    metadata.get(intent.expected_native_thread_id),
                ),
                native_thread_id=intent.expected_native_thread_id,
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
            return
        if intent.name is CardControlName.DELETE_ARCHIVED_BINDING:
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
                    "归档会话的原生历史已变化，本次删除未执行；请刷新。"
                ) from error
            success_notice = (
                f"✅ 已永久删除归档会话 {deleted.short_id}"
                f"（{deleted.project_alias}）、派生会话及本地 Binding。"
            )
            try:
                refreshed = await self._safe_update_card(
                    intent.source_id,
                    await self._archived_sessions_card(
                        scope=intent.scope,
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
            return
        if intent.name is CardControlName.REFRESH_ARCHIVED_SESSIONS:
            await self._safe_update_card(
                intent.source_id,
                await self._archived_sessions_card(scope=intent.scope),
            )
            return
        if intent.name is CardControlName.SESSIONS_PAGE:
            page = intent.page or 0
            await self._safe_update_card(
                intent.source_id,
                await self._sessions_card(scope=intent.scope, page=page),
            )
            return
        if intent.name in {
            CardControlName.GOAL_PAUSE,
            CardControlName.GOAL_RESUME,
            CardControlName.GOAL_CLEAR,
        }:
            assert intent.binding_id is not None
            assert intent.goal_generation is not None
            assert intent.expected_goal_status is not None
            async with self._scope_coordinator.hold(intent.scope.key):
                binding = self._bindings.get(intent.binding_id)
                if binding.scope_key != intent.scope.key:
                    raise BindingNotFound(intent.binding_id)
                native_thread_id = binding.native_thread_id
                if (
                    native_thread_id is None
                    or not self._progress_cards.owns_goal_card(
                        source_id=intent.source_id,
                        binding_id=binding.id,
                        thread_id=native_thread_id,
                        generation=intent.goal_generation,
                    )
                ):
                    raise CardActionError(
                        "Goal 卡片已过期或已被新卡片取代；"
                        "请重新发送 /goal。"
                    )
                goal_before = await self._runtime.goal_snapshot(binding)
                if (
                    goal_before is None
                    or goal_generation(goal_before) != intent.goal_generation
                    or goal_before.status.value != intent.expected_goal_status
                ):
                    raise CardActionError(
                        "Goal 已变化，本卡片操作未执行；请重新发送 /goal。"
                    )
                if intent.name is CardControlName.GOAL_PAUSE:
                    async def acknowledge_goal_card_pause() -> None:
                        current = self._progress_cards.goal_projection(
                            source_id=intent.source_id,
                            generation=intent.goal_generation,
                        )
                        goal_module = _reply_goal_module(
                            binding=binding,
                            goal=goal_before,
                            runtime_state=GoalOperationState.PAUSING.value,
                            notice="正在暂停 Goal 并中断当前物理 Turn。",
                        )
                        projection = (
                            ReplyCardProjection(
                                scope=intent.scope,
                                goal=goal_module,
                            )
                            if current is None
                            else replace(current, goal=goal_module)
                        )
                        await self._progress_cards.update_goal(
                            source_id=intent.source_id,
                            generation=intent.goal_generation,
                            projection=projection,
                        )

                    result = await self._runtime.stop(
                        binding.id,
                        acknowledge=acknowledge_goal_card_pause,
                    )
                    if result is StopDisposition.EXTERNAL_GOAL:
                        raise CardActionError(
                            "这是外部 active Goal，当前 SDK 无法安全重挂并暂停。"
                        )
                    if result is StopDisposition.NOT_RUNNING:
                        raise CardActionError(
                            "Goal 已结束运行，本次暂停未执行；请重新发送 /goal。"
                        )
                    return
                if intent.name is CardControlName.GOAL_RESUME:
                    origin = GoalCardOrigin(
                        message_id=intent.source_id,
                        scope=intent.scope,
                        binding_id=binding.id,
                        short_id=binding.short_id,
                        project_alias=binding.project_alias,
                        fallback_origin=_CardReplyTarget(
                            id=intent.source_id,
                            message_id=intent.source_id,
                            chat_id=intent.scope.chat_id,
                            conversation=_CardReplyConversation(
                                thread_id=intent.scope.topic_id
                            ),
                        ),
                        goal_generation=intent.goal_generation,
                    )
                    submission = await self._runtime.resume_goal(
                        binding=binding,
                        owner_id=intent.sender_id,
                        origin=origin,
                        expected_created_at=goal_before.created_at,
                    )
                    try:
                        presented = await self._present_running_goal(
                            binding=binding,
                            scope=intent.scope,
                            submission=submission,
                            origin=origin,
                            notice="Goal 已恢复。",
                        )
                        if not presented:
                            await self._safe_reply_to_card(
                                intent,
                                "Goal 已恢复并在原生 Codex 中执行，"
                                "但原卡片暂时无法更新。",
                            )
                    finally:
                        submission.release_receipt_attempt()
                    return
                cleared = await self._runtime.clear_goal(
                    binding,
                    expected_created_at=goal_before.created_at,
                )
                current = self._progress_cards.goal_projection(
                    source_id=intent.source_id,
                    generation=intent.goal_generation,
                )
                goal_module = _reply_goal_module(
                    binding=binding,
                    goal=None,
                    notice=("Goal 已结束。" if cleared else "当前没有 Goal。"),
                )
                projection = (
                    ReplyCardProjection(scope=intent.scope, goal=goal_module)
                    if current is None
                    else replace(current, goal=goal_module)
                )
                if not await self._progress_cards.update_goal(
                    source_id=intent.source_id,
                    generation=intent.goal_generation,
                    projection=projection,
                    retain_session=False,
                ):
                    raise CardActionError("飞书未确认 Goal 卡片已更新。")
            return
        raise CardActionError(f"尚未处理的卡片动作：{intent.name.value}")

    async def _update_goal_action_notice(
        self,
        intent: CardControlIntent,
        message: str,
    ) -> bool:
        generation = intent.goal_generation
        if generation is None:
            return False
        current = self._progress_cards.goal_projection(
            source_id=intent.source_id,
            generation=generation,
        )
        if current is None or current.goal is None:
            return False
        return await self._progress_cards.update_goal(
            source_id=intent.source_id,
            generation=generation,
            projection=replace(
                current,
                goal=replace(
                    current.goal,
                    notice=message[:4_000],
                    notice_is_error=True,
                ),
            ),
        )

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

    @staticmethod
    def _runtime_precondition(intent: CardControlIntent) -> RuntimePrecondition:
        if intent.expected_activity_revision is None:
            raise CardActionError(
                "会话运行状态前置条件缺失，请重新发送原命令。"
            )
        return RuntimePrecondition(
            activity_revision=intent.expected_activity_revision,
            physical_turn_id=intent.expected_turn_id,
        )

    async def _binding_state(
        self,
        binding: ThreadBinding,
        *,
        snapshot: BindingRuntimeSnapshot | None = None,
    ) -> str:
        snapshot = snapshot or self._runtime.binding_runtime_snapshot(binding.id)
        lifecycle = snapshot.lifecycle
        if lifecycle is not None:
            return lifecycle.state.value
        active = snapshot.turn
        if active is not None:
            return active.state.value
        if snapshot.compacting:
            return "compacting"
        active_goal = snapshot.goal
        if active_goal is not None:
            return active_goal.state.value
        persisted = await self._runtime.goal_snapshot(binding)
        return (
            SESSION_IDLE_STATE
            if persisted is None
            else persisted_goal_session_state(persisted.status)
        )

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

    async def _sessions_card(
        self,
        *,
        scope: FeishuScope,
        page: int = 0,
        notice: str | None = None,
    ) -> OutboundCard:
        bindings = self._bindings.list_bindings(scope.key)
        metadata = await self._read_thread_metadata(bindings)
        archived_metadata = await self._read_thread_metadata(
            bindings,
            archived=True,
            strict=True,
        )
        sessions: list[SessionCardItem] = []
        for binding in bindings:
            if binding.native_thread_id in archived_metadata:
                continue
            snapshot = self._runtime.binding_runtime_snapshot(binding.id)
            state = await self._binding_state(binding, snapshot=snapshot)
            title = _session_title(
                binding,
                metadata.get(binding.native_thread_id),
            )
            sessions.append(
                SessionCardItem(
                    binding_id=binding.id,
                    short_id=binding.short_id,
                    project_alias=binding.project_alias,
                    native_thread_id=binding.native_thread_id,
                    title=title,
                    state=state,
                    active=binding.active,
                    activity_revision=snapshot.activity_revision,
                    turn_id=(
                        snapshot.turn.turn_id
                        if snapshot.turn is not None
                        else None
                    ),
                )
            )
        return sessions_card(
            scope=scope,
            sessions=tuple(sessions),
            native_delete_available=(
                NativeCapability.DELETE in self._runtime.available_capabilities
            ),
            page=page,
            notice=notice,
        )

    async def _archived_sessions_card(
        self,
        *,
        scope: FeishuScope,
        notice: str | None = None,
        notice_is_error: bool = False,
    ) -> OutboundCard:
        bindings = self._bindings.list_bindings(scope.key)
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
        return archived_sessions_card(
            scope=scope,
            sessions=sessions,
            native_delete_available=(
                NativeCapability.DELETE in self._runtime.available_capabilities
            ),
            notice=notice,
            notice_is_error=notice_is_error,
        )

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
            chat_kind = _public_chat_kind(chat_info)
            scope = scope_from_fetched_card(
                app_id=self._app_id,
                callback_chat_id=chat_id,
                fetched_message=fetched,
                chat_type=chat_kind,
            )
            intent = decode_card_form(
                scope=scope,
                message_id=message_id,
                sender_id=sender_id,
                tag=tag,
                form_value=form_value,
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

    async def _create_binding(
        self,
        *,
        scope: FeishuScope,
        sender_id: str,
        project_alias: str,
        expected_revision: int | None = None,
        turn_settings: BindingTurnSettings | None = None,
        task_feedback: BindingTaskFeedback = BindingTaskFeedback(),
        message_context_mode: MentionContextMode = MentionContextMode.CURRENT_ONLY,
        context_anchor: MessageContextAnchor | None = None,
    ):
        created = await self._management.create_current_binding(
            scope=scope,
            creator_id=sender_id,
            project_alias=project_alias,
            expected_project_revision=expected_revision,
            turn_settings=turn_settings,
            task_feedback=task_feedback,
            message_context_mode=message_context_mode,
            context_anchor=context_anchor,
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
        result = await self._channel.reply(message, content)
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
            fallback_result = await self._channel.reply(message, failure_notice)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "failed to send safe reply failure notice",
                extra={"message_id": message_id},
            )
            return
        if getattr(fallback_result, "success", True) is False:
            logger.error(
                "failed to send safe reply failure notice",
                extra={
                    "message_id": message_id,
                    "error_code": _send_result_error_code(fallback_result),
                },
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
        for attempt in range(len(_CARD_ACTION_LOCK_RETRY_DELAYS_SECONDS) + 1):
            try:
                result = await self._update_card(message_id, content)
            except Exception:
                logger.exception("failed to render card action result")
                return False
            if getattr(result, "success", True) is not False:
                return True
            if (
                not _is_feishu_card_action_lock(result)
                or attempt == len(_CARD_ACTION_LOCK_RETRY_DELAYS_SECONDS)
            ):
                logger.error(
                    "failed to render card action result: unsuccessful update"
                )
                return False
            delay = _CARD_ACTION_LOCK_RETRY_DELAYS_SECONDS[attempt]
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


def _is_feishu_card_action_lock(result: object) -> bool:
    if getattr(result, "success", None) is not False:
        return False
    error = getattr(result, "error", None)
    if getattr(error, "raw_code", None) != _FEISHU_CARD_ACTION_LOCK_OUTER_CODE:
        return False
    hint = getattr(error, "hint", None)
    if not isinstance(hint, str):
        return False
    return (
        re.search(
            rf"(?<!\d){_FEISHU_CARD_ACTION_LOCK_INNER_CODE}(?!\d)",
            hint,
        )
        is not None
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


def _reply_failure_notice(result: object) -> str | None:
    if getattr(result, "success", None) is not False:
        return None
    code = _send_result_error_code(result)
    if code != _FEISHU_CONTENT_AUDIT_REJECTION_CODE:
        return None
    raw = _object_field(result, "raw")
    message = _object_field(raw, "msg")
    if not isinstance(message, str):
        error = _object_field(result, "error")
        message = _object_field(error, "hint")
    normalized = message.upper() if isinstance(message, str) else ""
    for reason, label in _FEISHU_AUDIT_REASON_LABELS.items():
        if reason in normalized:
            return (
                "消息发送失败：飞书内容审核认为回复中包含"
                f"{label}。（错误码 {code}）"
            )
    return f"消息发送失败：回复内容未通过飞书审核。（错误码 {code}）"


def _send_result_error_code(result: object) -> int | None:
    raw = _object_field(result, "raw")
    code = _object_field(raw, "code")
    if isinstance(code, int) and not isinstance(code, bool):
        return code
    error = _object_field(result, "error")
    raw_code = _object_field(error, "raw_code")
    if isinstance(raw_code, int) and not isinstance(raw_code, bool):
        return raw_code
    return None


def _nonempty_field(value: object, name: str) -> str | None:
    field = _object_field(value, name)
    return field if isinstance(field, str) and field else None


def _progress_card_message_id(result: object) -> str | None:
    """Return an exact reply ID only when Feishu did not report failure."""

    if getattr(result, "success", True) is False:
        return None
    if getattr(result, "chunk_ids", None):
        return None
    direct = _nonempty_field(result, "message_id")
    raw = _object_field(result, "raw")
    data = _object_field(raw, "data")
    nested = _nonempty_field(data, "message_id")
    if direct is not None and nested is not None and direct != nested:
        return None
    return direct or nested


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
