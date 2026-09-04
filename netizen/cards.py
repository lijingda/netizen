"""Card 2.0 rendering and strict callback decoding for Channel controls."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from lark_channel import OutboundCard, new_card

from .bindings import BindingTaskFeedback, BindingTurnSettings, SideTopicState
from .domain import (
    ACTIVE_STATE_VALUES,
    ActiveState,
    CardControlIntent,
    CardControlName,
    FeishuScope,
    GoalOperationState,
    GoalStatus,
    MentionContextMode,
    ReplyCardActivityModule,
    ReplyCardFileItem,
    ReplyCardFilesModule,
    ReplyCardGoalModule,
    ReplyCardManifest,
    ReplyCardProjection,
    ReplyCardResultModule,
    SettingsSection,
    SESSION_IDLE_STATE,
    ScopeKind,
    TurnActivityManifestEntry,
    TurnCommentaryManifestEntry,
    TurnFileActionIntent,
    TurnFileActionName,
    TurnFileManifestItem,
    TurnProgressManifest,
    TurnProgressManifestStep,
    session_stop_available,
)
from .model_settings import ModelCatalog, TurnModelSettings
from .projects import Project
from .sdk_gap_adapter import GoalSnapshot
from .turn_files import (
    TurnFile,
    TurnFilePage,
    inspect_turn_file_path,
    paginate_turn_files,
)
from .turn_activity import (
    ACTIVITY_COMMENTARY_LIMIT,
    ACTIVITY_OPERATION_LIMIT,
    ACTIVITY_PLAN_LIMIT,
    ACTIVITY_TEXT_LIMIT,
    COMMAND_ACTIVITY_SUMMARIES,
    TurnActivityKind,
    TurnActivityStatus,
    normalize_activity_text_layout,
    sanitize_activity_text,
)


ACTION_VERSION = 4
TURN_FILE_ACTION_VERSION = 4
REPLY_CARD_ACTION_VERSION = 5
SESSIONS_PAGE_SIZE = 10
TURN_FILE_MANIFEST_LIMIT = 400
TURN_FILE_CARD_JSON_LIMIT_BYTES = 55_000
_TURN_ANSWER_ELEMENT_ID = "turnanswerv1"
_TURN_FILES_ELEMENT_ID = "turnfilesv4"
_TURN_PROGRESS_ELEMENT_ID = "turnprogressv1"
_GOAL_ELEMENT_ID = "goalmodulev1"
_GOAL_OBJECTIVE_PREVIEW_CHARS = 200
_TURN_PROGRESS_MAX_STEPS = ACTIVITY_PLAN_LIMIT
_TURN_PROGRESS_STEP_MAX_CHARS = ACTIVITY_TEXT_LIMIT
MAX_THREAD_NAME_CHARS = 120
MAX_MODEL_ID_CHARS = 256
MAX_ENCODED_MODEL_ID_CHARS = 1368
MAX_SETTING_ID_CHARS = 128
_INHERIT_MODEL_CHOICE = "inherit"
_PROJECT_REFERENCE = re.compile(
    r"project:v1:([a-z0-9][a-z0-9_-]{0,63}):([1-9][0-9]*)"
)
_BINDING_REFERENCE = re.compile(r"binding:v1:([A-Za-z0-9][A-Za-z0-9-]{0,127})")
_NATIVE_THREAD_REFERENCE = re.compile(
    r"native-thread:v1:([A-Za-z0-9][A-Za-z0-9._-]{0,191})"
)
_NEW_MODEL_REFERENCE = re.compile(
    r"new-model:v1:(inherit|explicit:([A-Za-z0-9_-]+))"
)
_CONFIG_MODEL_REFERENCE = re.compile(
    r"config-model:v4:([A-Za-z0-9][A-Za-z0-9-]{0,127}):"
    r"([1-9][0-9]{0,18}):([1-9][0-9]{0,18}):"
    r"([1-9][0-9]{0,18}):(inherit|explicit:([A-Za-z0-9_-]+))"
)
_CONTEXT_MODE_REFERENCE = re.compile(
    r"context-mode:v1:(current-only|catch-up)"
)
_TASK_FEEDBACK_REFERENCE = re.compile(r"task-feedback:v2:(off|on)")
_CALLBACK_NONCE = re.compile(r"[0-9a-f]{32}")
_PROJECT_MODE_REFERENCE = re.compile(
    r"project-mode:v2:(create|existing):([0-9a-f]{32})"
)
_RENAME_NAME_FIELD = re.compile(
    r"rename_name_v1__([A-Za-z0-9][A-Za-z0-9-]{0,127})"
)
_SIDE_REFERENCE = re.compile(r"side:v1:([A-Za-z0-9][A-Za-z0-9-]{0,127})")
_TURN_REFERENCE = re.compile(
    r"turn:v1:([A-Za-z0-9][A-Za-z0-9._-]{0,191})"
)
_GOAL_GENERATION = re.compile(r"[A-Za-z0-9_-]{43}")

_REPEATABLE_CARD_CONTROL_NAMES = frozenset(
    {
        CardControlName.OPEN_SETTINGS_SECTION,
        CardControlName.REFRESH_SETTINGS,
        CardControlName.PREPARE_EXACT_DELETE_BINDING,
        CardControlName.PREPARE_ARCHIVED_DELETE_BINDING,
        CardControlName.ACTIVATE_BINDING,
        CardControlName.RECHECK_EXACT_TURN,
        CardControlName.SESSIONS_PAGE,
        CardControlName.REFRESH_ARCHIVED_SESSIONS,
        CardControlName.GOAL_PAUSE,
        CardControlName.GOAL_RESUME,
        CardControlName.SIDE_CLOSE,
    }
)
# These actions can legitimately reappear with the same semantic payload on
# one updated message. Revision-bearing and one-shot actions stay excluded.
_REPEATABLE_CALLBACK_INTENTS = frozenset(
    name.value for name in _REPEATABLE_CARD_CONTROL_NAMES
) | {TurnFileActionName.PAGE.value}


@dataclass(frozen=True, slots=True)
class ArchivedSessionCardItem:
    binding_id: str
    short_id: str
    project_alias: str
    native_thread_id: str
    title: str


@dataclass(frozen=True, slots=True)
class SessionCardItem:
    binding_id: str
    short_id: str
    project_alias: str
    native_thread_id: str | None
    title: str
    state: str
    active: bool
    activity_revision: int = 0
    turn_id: str | None = None


class _TurnPlanStepLike(Protocol):
    step: str
    status: object


class _TurnActivityEntryLike(Protocol):
    kind: object
    status: object
    event_timestamp_ms: int | None
    text: str | None
    count: int


class _TurnCommentaryEntryLike(Protocol):
    event_timestamp_ms: int | None
    text: str | None


class _TurnActivitySnapshotLike(Protocol):
    state: object
    steer_count: int
    plan_available: bool
    plan_generated: bool
    plan_may_be_stale: bool
    steps: tuple[_TurnPlanStepLike, ...]
    commentary: tuple[_TurnCommentaryEntryLike, ...]
    operations: tuple[_TurnActivityEntryLike, ...]


class CardActionError(ValueError):
    pass


class TurnFileCardLimitError(CardActionError):
    pass


class SettingsCardActionError(CardActionError):
    def __init__(
        self,
        message: str,
        *,
        scope: FeishuScope,
        section: SettingsSection,
    ) -> None:
        super().__init__(message)
        self.scope = scope
        self.section = section


def goal_generation(snapshot: GoalSnapshot) -> str:
    """Return the strongest stable native Goal fingerprint the SDK exposes."""

    return _goal_generation(snapshot)


def _goal_generation(snapshot: GoalSnapshot) -> str:
    if not snapshot.thread_id or isinstance(snapshot.created_at, bool):
        raise ValueError("Goal generation requires a native identity and creation time")
    material = (
        "netizen-goal-generation:v2\0"
        f"{snapshot.thread_id}\0{snapshot.created_at}\0"
        f"{snapshot.objective}\0{snapshot.token_budget}"
    ).encode("utf-8")
    return base64.urlsafe_b64encode(hashlib.sha256(material).digest()).decode(
        "ascii"
    ).rstrip("=")


def reply_card(projection: ReplyCardProjection) -> OutboundCard:
    """Render the closed Reply Card module set as one atomic Card 2.0 value.

    The renderer is deterministic and does not read Runtime, SQLite, or the
    filesystem.  When Files is present every advertised page is rendered and
    size-checked before the selected page is returned.
    """

    normalized = _normalize_reply_projection(projection)
    files_module = normalized.files
    if files_module is None:
        return _render_reply_card_page(normalized)
    turn_files = _reply_turn_files(files_module.items)
    requested = paginate_turn_files(turn_files, files_module.page)
    selected: OutboundCard | None = None
    for page in range(requested.total_pages):
        candidate = _render_reply_card_page(
            replace(
                normalized,
                files=replace(files_module, page=page),
            )
        )
        if page == requested.page:
            selected = candidate
    assert selected is not None
    return selected


def reply_card_from_manifest(
    *,
    scope: FeishuScope,
    binding_id: str,
    turn_id: str,
    manifest: tuple[TurnFileManifestItem, ...],
    reply: ReplyCardManifest,
    page: int,
    additions: int | None = None,
    deletions: int | None = None,
) -> OutboundCard:
    """Rebuild a v5 Reply Card from one strict self-contained page callback."""

    if not manifest:
        raise CardActionError("本轮文件清单为空，请重新执行任务。")
    if len(manifest) > TURN_FILE_MANIFEST_LIMIT:
        raise TurnFileCardLimitError(
            f"本轮文件共 {len(manifest)} 个，超过卡片完整分页上限 "
            f"{TURN_FILE_MANIFEST_LIMIT} 个；未截断文件清单。"
        )
    inspected = tuple(_inspect_manifest_file(entry) for entry in manifest)
    return reply_card(
        ReplyCardProjection(
            scope=scope,
            goal=reply.goal,
            activity=reply.activity,
            result=reply.result,
            files=ReplyCardFilesModule(
                binding_id=binding_id,
                turn_id=turn_id,
                items=tuple(_reply_file_item(item) for item in inspected),
                page=page,
                action_version=REPLY_CARD_ACTION_VERSION,
                additions=additions,
                deletions=deletions,
            ),
        )
    )


def _normalize_reply_projection(
    projection: ReplyCardProjection,
) -> ReplyCardProjection:
    if not any(
        (projection.goal, projection.activity, projection.result, projection.files)
    ):
        raise ValueError("a Reply Card requires at least one module")
    goal = _normalize_goal_module(projection.goal)
    activity = _normalize_activity_module(projection.activity)
    result = projection.result
    if result is not None:
        _bounded_card_text(result.content, "result", 100_000)
    files = projection.files
    if files is not None:
        if projection.scope is None:
            raise ValueError("a Files module requires scope")
        if result is None:
            raise ValueError("a Files module requires a Result module")
        if not files.binding_id or not files.turn_id:
            raise ValueError("a Files module requires binding_id and turn_id")
        if not files.items:
            raise CardActionError("本轮文件当前已不可用。")
        if len(files.items) > TURN_FILE_MANIFEST_LIMIT:
            raise TurnFileCardLimitError(
                f"本轮文件共 {len(files.items)} 个，超过卡片完整分页上限 "
                f"{TURN_FILE_MANIFEST_LIMIT} 个；未截断文件清单。"
            )
        if files.action_version not in {
            TURN_FILE_ACTION_VERSION,
            REPLY_CARD_ACTION_VERSION,
        }:
            raise ValueError("unsupported Reply Card file action version")
        if (
            files.action_version == TURN_FILE_ACTION_VERSION
            and goal is not None
        ):
            raise ValueError("a Goal + Files Reply Card requires v5 callbacks")
        if goal is not None and goal.binding_id != files.binding_id:
            raise ValueError("Goal and Files modules require the same binding_id")
        _optional_line_counts(
            files.additions,
            files.deletions,
            field="files",
        )
        normalized_files = _reply_turn_files(files.items)
        files = replace(
            files,
            items=tuple(_reply_file_item(item) for item in normalized_files),
        )
    if goal is not None and projection.scope is None:
        raise ValueError("a Goal module requires scope")
    if activity is not None:
        if activity.terminal_status is None:
            if activity.collapsed:
                raise ValueError("a running progress card must remain expanded")
            if result is not None or files is not None:
                raise ValueError(
                    "a running Activity module cannot contain Result or Files"
                )
        elif files is not None and activity.terminal_status != "completed":
            raise ValueError("only completed Activity may contain Files")
    return replace(
        projection,
        goal=goal,
        activity=activity,
        files=files,
    )


def _normalize_goal_module(
    goal: ReplyCardGoalModule | None,
) -> ReplyCardGoalModule | None:
    if goal is None:
        return None
    _bounded_card_text(goal.binding_id, "goal.binding_id", 128)
    _bounded_card_text(goal.short_id, "goal.short_id", 32)
    _bounded_card_text(goal.project_alias, "goal.project_alias", 128)
    if goal.status is None:
        if any(
            value is not None
            for value in (
                goal.goal_generation,
                goal.runtime_state,
                goal.objective,
                goal.token_budget,
            )
        ) or goal.tokens_used != 0:
            raise ValueError("an empty Goal module cannot carry Goal state")
    else:
        if goal.status not in {item.value for item in GoalStatus}:
            raise ValueError("unsupported Goal status")
        _decode_goal_generation(goal.goal_generation)
        assert goal.objective is not None
        _bounded_card_text(goal.objective, "goal.objective", 10_000)
        if goal.runtime_state is not None:
            _bounded_card_text(goal.runtime_state, "goal.runtime_state", 128)
        _bounded_nonnegative_int(goal.tokens_used, "goal.tokens_used")
        if goal.token_budget is not None:
            _bounded_nonnegative_int(goal.token_budget, "goal.token_budget")
    if goal.notice is not None:
        _bounded_card_text(goal.notice, "goal.notice", 4_000)
    if type(goal.notice_is_error) is not bool:
        raise ValueError("goal.notice_is_error must be a boolean")
    return goal


def _normalize_activity_module(
    activity: ReplyCardActivityModule | None,
) -> ReplyCardActivityModule | None:
    if activity is None:
        return None
    terminal_status = _terminal_progress_status(activity.terminal_status)
    progress = _sanitize_turn_progress_manifest(activity.progress)
    _bounded_nonnegative_int(activity.hidden_steps, "activity.hidden_steps")
    return replace(
        activity,
        progress=progress,
        terminal_status=terminal_status,
        collapsed=activity.collapsed or terminal_status is not None,
    )


def _sanitize_turn_progress_manifest(
    progress: TurnProgressManifest,
) -> TurnProgressManifest:
    return _decode_turn_progress_manifest(_encode_turn_progress_manifest(progress))


def _activity_timestamp_ms(value: object) -> int | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= 10**18
    ):
        raise ValueError("activity timestamp is invalid")
    return value


def _decode_activity_timestamp_ms(value: object, field: str) -> int | None:
    try:
        return _activity_timestamp_ms(value)
    except ValueError as error:
        raise CardActionError(f"{field} 无效。") from error


def _activity_operation_text(kind: object, value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("activity operation text is invalid")
    if kind == TurnActivityKind.COMMAND.value:
        if value not in COMMAND_ACTIVITY_SUMMARIES:
            raise ValueError("activity command summary is invalid")
        return value
    if kind == TurnActivityKind.TOOL.value:
        return value
    raise ValueError("activity text is unsupported for this operation")


def _bounded_card_text(value: Any, field: str, limit: int) -> str:
    text = _required_string(value, field)
    if len(text) > limit or "\x00" in text:
        raise ValueError(f"{field} is invalid")
    return text


def _bounded_nonnegative_int(value: Any, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= 10**18
    ):
        raise ValueError(f"{field} must be a bounded non-negative integer")
    return value


def _optional_line_counts(
    additions: Any,
    deletions: Any,
    *,
    field: str,
) -> tuple[int | None, int | None]:
    if additions is None and deletions is None:
        return None, None
    if additions is None or deletions is None:
        raise ValueError(f"{field} line counts must be present together")
    return (
        _bounded_nonnegative_int(additions, f"{field}.additions"),
        _bounded_nonnegative_int(deletions, f"{field}.deletions"),
    )


def _inspect_manifest_file(entry: TurnFileManifestItem) -> TurnFile:
    inspected = inspect_turn_file_path(entry.path, entry.label)
    if not inspected.available or inspected.media_kind == "image":
        return inspected
    return replace(
        inspected,
        additions=entry.additions,
        deletions=entry.deletions,
    )


def _reply_file_item(turn_file: TurnFile) -> ReplyCardFileItem:
    return ReplyCardFileItem(
        path=str(turn_file.resolved_path),
        label=turn_file.display_path,
        size=turn_file.size,
        media_kind=turn_file.media_kind,
        additions=turn_file.additions,
        deletions=turn_file.deletions,
    )


def _reply_turn_files(
    items: tuple[ReplyCardFileItem, ...],
) -> tuple[TurnFile, ...]:
    results: list[TurnFile] = []
    seen: set[str] = set()
    for item in items:
        path = _decode_turn_file_path(item.path)
        label = _bounded_card_text(item.label, "file.label", 1024)
        if path in seen:
            raise CardActionError("本轮文件清单包含重复路径。")
        seen.add(path)
        size = item.size
        media_kind = item.media_kind
        if (size is None) != (media_kind is None):
            raise ValueError("file availability fields must change together")
        if size is not None:
            _bounded_nonnegative_int(size, "file.size")
            if media_kind not in {"image", "file"}:
                raise ValueError("file.media_kind is invalid")
        additions, deletions = _optional_line_counts(
            item.additions,
            item.deletions,
            field="file",
        )
        if media_kind != "file":
            additions = None
            deletions = None
        results.append(
            TurnFile(
                display_path=label,
                resolved_path=Path(path),
                size=size,
                media_kind=media_kind,
                additions=additions,
                deletions=deletions,
            )
        )
    return tuple(results)


def turn_files_card(
    *,
    scope: FeishuScope,
    binding_id: str,
    turn_id: str,
    final_response: str,
    files: tuple[TurnFile, ...],
    page: int = 0,
    additions: int | None = None,
    deletions: int | None = None,
) -> OutboundCard:
    return reply_card(
        ReplyCardProjection(
            scope=scope,
            result=ReplyCardResultModule(final_response),
            files=ReplyCardFilesModule(
                binding_id=binding_id,
                turn_id=turn_id,
                items=tuple(_reply_file_item(item) for item in files),
                page=page,
                action_version=TURN_FILE_ACTION_VERSION,
                additions=additions,
                deletions=deletions,
            ),
        )
    )


def turn_progress_card(
    *,
    snapshot: _TurnActivitySnapshotLike,
    final_response: str | None = None,
    files: tuple[TurnFile, ...] = (),
    terminal_status: str | None = None,
    collapsed: bool = False,
    scope: FeishuScope | None = None,
    binding_id: str | None = None,
    turn_id: str | None = None,
    additions: int | None = None,
    deletions: int | None = None,
) -> OutboundCard:
    """Render one replaceable Phase 1 Turn progress card.

    The activity panel reads only the bounded, safety-projected status,
    commentary, generic operations, and checklist.  It never inspects
    reasoning, tool arguments, or tool output.
    A terminal render always collapses that panel and appends the authoritative
    final response plus the existing optional Turn-file controls.
    """

    normalized_terminal_status = _terminal_progress_status(terminal_status)
    if normalized_terminal_status is None:
        if collapsed:
            raise ValueError("a running progress card must remain expanded")
        if final_response is not None or files:
            raise ValueError(
                "a running progress card cannot contain a final response or files"
            )
    elif files and normalized_terminal_status != "completed":
        raise ValueError("only a completed progress card may contain files")

    activity = ReplyCardActivityModule(
        progress=_turn_progress_manifest(snapshot),
        terminal_status=normalized_terminal_status,
        collapsed=collapsed,
        # v4 file callbacks freeze only the already-bounded progress manifest.
        # Preserve their established initial/paged behavior until that legacy
        # schema ages out; v5 Goal cards carry hidden_steps explicitly.
        hidden_steps=(
            0
            if files
            else max(0, len(snapshot.steps) - _TURN_PROGRESS_MAX_STEPS)
        ),
    )
    files_module = None
    if files:
        if scope is None or not binding_id or not turn_id:
            raise ValueError(
                "a progress card with files requires scope, binding_id, and turn_id"
            )
        files_module = ReplyCardFilesModule(
            binding_id=binding_id,
            turn_id=turn_id,
            items=tuple(_reply_file_item(item) for item in files),
            action_version=TURN_FILE_ACTION_VERSION,
            additions=additions,
            deletions=deletions,
        )
    result = None
    if normalized_terminal_status is not None:
        result = ReplyCardResultModule(
            final_response or _default_terminal_response(normalized_terminal_status)
        )
    return reply_card(
        ReplyCardProjection(
            scope=scope,
            activity=activity,
            result=result,
            files=files_module,
        )
    )


def _turn_file_manifest(
    files: tuple[TurnFile, ...],
) -> tuple[TurnFileManifestItem, ...]:
    return tuple(
        TurnFileManifestItem(
            path=str(turn_file.resolved_path),
            label=turn_file.display_path,
            additions=turn_file.additions,
            deletions=turn_file.deletions,
        )
        for turn_file in files
    )


def _turn_progress_manifest(
    snapshot: _TurnActivitySnapshotLike,
) -> TurnProgressManifest:
    state = getattr(snapshot.state, "value", snapshot.state)
    if state not in ACTIVE_STATE_VALUES:
        state = ActiveState.RUNNING.value
    steps = tuple(
        TurnProgressManifestStep(
            step=activity_step_display(item.step),
            status=(
                getattr(item.status, "value", item.status)
                if getattr(item.status, "value", item.status)
                in {"pending", "inProgress", "completed"}
                else "pending"
            ),
        )
        for item in snapshot.steps[:_TURN_PROGRESS_MAX_STEPS]
    )
    snapshot_commentary = tuple(getattr(snapshot, "commentary", ()))
    snapshot_operations = tuple(getattr(snapshot, "operations", ()))
    commentary = tuple(
        TurnCommentaryManifestEntry(
            text=sanitized,
            event_timestamp_ms=_activity_timestamp_ms(item.event_timestamp_ms),
        )
        for item in snapshot_commentary[-ACTIVITY_COMMENTARY_LIMIT:]
        if item.text is not None
        and (sanitized := sanitize_activity_text(item.text)) is not None
    )
    operations = tuple(
        TurnActivityManifestEntry(
            kind=(
                getattr(item.kind, "value", item.kind)
                if getattr(item.kind, "value", item.kind)
                in {kind.value for kind in TurnActivityKind if kind is not TurnActivityKind.COMMENTARY}
                else TurnActivityKind.TOOL.value
            ),
            status=(
                getattr(item.status, "value", item.status)
                if getattr(item.status, "value", item.status)
                in {status.value for status in TurnActivityStatus}
                else TurnActivityStatus.IN_PROGRESS.value
            ),
            event_timestamp_ms=_activity_timestamp_ms(item.event_timestamp_ms),
            text=_activity_operation_text(
                getattr(item.kind, "value", item.kind),
                item.text,
            ),
            count=max(0, item.count),
        )
        for item in snapshot_operations[-ACTIVITY_OPERATION_LIMIT:]
    )
    return TurnProgressManifest(
        state=state,
        steer_count=max(0, snapshot.steer_count),
        plan_available=bool(snapshot.plan_available),
        plan_generated=bool(snapshot.plan_generated),
        plan_may_be_stale=bool(snapshot.plan_may_be_stale),
        steps=steps,
        commentary=commentary,
        operations=operations,
    )


def _render_reply_card_page(projection: ReplyCardProjection) -> OutboundCard:
    """Pure single-page renderer; callers validate every page atomically."""

    title, subtitle, template, summary = _reply_card_chrome(projection)
    builder = (
        new_card()
        .config(
            update_multi=True,
            width_mode="default",
            summary={"content": summary},
        )
        .header(
            title,
            subtitle=subtitle,
            template=template,
            icon={"tag": "standard_icon", "token": "todo_colorful"},
        )
    )
    if projection.goal is not None:
        builder.raw(
            _reply_goal_block(
                scope=projection.scope,
                goal=projection.goal,
            )
        )
    if projection.activity is not None:
        activity = projection.activity
        builder.raw(
            _turn_progress_panel(
                activity.progress,
                terminal_status=activity.terminal_status,
                expanded=not activity.collapsed,
                hidden_steps=activity.hidden_steps,
            )
        )
    if projection.result is not None:
        builder.raw(_turn_answer_block(projection.result.content))
    if projection.files is not None:
        assert projection.scope is not None
        files = projection.files
        turn_files = _reply_turn_files(files.items)
        visible = paginate_turn_files(turn_files, files.page)
        builder.raw(
            _turn_files_block(
                scope=projection.scope,
                binding_id=files.binding_id,
                turn_id=files.turn_id,
                page=visible,
                manifest=tuple(
                    TurnFileManifestItem(
                        item.path,
                        item.label,
                        item.additions,
                        item.deletions,
                    )
                    for item in files.items
                ),
                final_response=(
                    projection.result.content
                    if projection.result is not None
                    else ""
                ),
                progress=(
                    projection.activity.progress
                    if projection.activity is not None
                    else None
                ),
                reply=_reply_card_manifest(projection),
                action_version=files.action_version,
                additions=files.additions,
                deletions=files.deletions,
            )
        )
    card = builder.to_dict()
    body = card.get("body")
    if isinstance(body, dict):
        body.update(
            {
                "direction": "vertical",
                "padding": "12px 12px 20px 12px",
                "vertical_spacing": "12px",
            }
        )
    _validate_turn_card_size(
        card,
        label="回复卡片",
        untruncated="卡片内容",
    )
    return OutboundCard(card=card)


def _reply_card_chrome(
    projection: ReplyCardProjection,
) -> tuple[str, str | None, str, str]:
    goal = projection.goal
    if goal is not None:
        state = goal.status
        if state is None and goal.notice_is_error:
            template = "red"
            summary = "Goal 状态未确认"
        elif state == GoalStatus.COMPLETE.value:
            template = "green"
            summary = "Goal 已完成"
        elif state in {
            GoalStatus.BLOCKED.value,
            GoalStatus.USAGE_LIMITED.value,
            GoalStatus.BUDGET_LIMITED.value,
        }:
            template = "orange"
            summary = "Goal 等待处理"
        elif state == GoalStatus.PAUSED.value:
            template = "orange"
            summary = "Goal 已暂停"
        else:
            template = "blue"
            summary = "Goal 正在执行" if state else "Codex Goal"
        return (
            "Codex Goal",
            f"{goal.short_id} · {goal.project_alias}",
            template,
            summary,
        )
    if projection.activity is not None:
        title, template, summary = _progress_card_chrome(
            projection.activity.terminal_status
        )
        return title, None, template, summary
    if projection.files is not None:
        files = projection.files
        visible = paginate_turn_files(_reply_turn_files(files.items), files.page)
        subtitle = (
            f"本轮文件 {visible.total_items} 个 · "
            f"第 {visible.page + 1}/{visible.total_pages} 页"
        )
        return (
            "任务已完成",
            subtitle,
            "green",
            f"任务已完成 · 本轮文件 {visible.total_items} 个",
        )
    return "任务已完成", None, "green", "任务已完成"


def _reply_goal_block(
    *,
    scope: FeishuScope | None,
    goal: ReplyCardGoalModule,
) -> dict[str, Any]:
    elements: list[dict[str, Any]] = []
    if goal.notice:
        elements.append(_notice(goal.notice, error=goal.notice_is_error))
    if goal.status is None:
        if goal.notice_is_error:
            elements.append(
                {
                    "tag": "markdown",
                    "content": (
                        "无法安全确认原生 Goal 是否存在；当前会话仍保持占用，"
                        "请勿启动新的原生操作。"
                    ),
                }
            )
        else:
            elements.append(
                {
                    "tag": "markdown",
                    "content": (
                        "当前原生 Thread 没有 Goal。使用 `/goal <objective>` 启动；"
                        "Goal 可跨多个物理 Turn 自动继续。"
                    ),
                }
            )
    else:
        state = goal.runtime_state or f"goal-{goal.status}"
        budget = "未设置" if goal.token_budget is None else str(goal.token_budget)
        objective = _goal_objective_preview(goal.objective or "")
        elements.append(
            {
                "tag": "markdown",
                "content": (
                    f"**状态**：`{_md_code(state)}`\n"
                    f"**Objective**：{_md_code(objective)}\n"
                    f"**Tokens**：{goal.tokens_used} / {budget}"
                ),
            }
        )
        assert scope is not None and goal.goal_generation is not None
        buttons: list[dict[str, Any]] = []
        external = goal.runtime_state == "externally-active-goal"
        controls_unknown = goal.runtime_state == "goal-unknown"
        pausing = goal.runtime_state == GoalOperationState.PAUSING.value
        if (
            goal.status == GoalStatus.ACTIVE.value
            and not external
            and not controls_unknown
            and not pausing
        ):
            buttons.append(
                _goal_control_button(
                    scope=scope,
                    goal=goal,
                    name=CardControlName.GOAL_PAUSE,
                    label="暂停 Goal",
                    style="primary",
                    confirm=("暂停 Goal", "将暂停 Goal 并中断当前物理 Turn。"),
                )
            )
        if (
            goal.status == GoalStatus.PAUSED.value
            and not external
            and not controls_unknown
        ):
            buttons.append(
                _goal_control_button(
                    scope=scope,
                    goal=goal,
                    name=CardControlName.GOAL_RESUME,
                    label="恢复 Goal",
                    style="primary_filled",
                )
            )
        if (
            goal.status != GoalStatus.ACTIVE.value
            and not controls_unknown
            and goal.runtime_state != "goal-cleared"
        ):
            buttons.append(
                _goal_control_button(
                    scope=scope,
                    goal=goal,
                    name=CardControlName.GOAL_CLEAR,
                    label="结束 Goal",
                    confirm=("结束 Goal", "结束后将无法从此 Goal 状态恢复。"),
                )
            )
        if buttons:
            elements.append(_button_row(*buttons))
        elif external:
            elements.append(
                _notice(
                    "这是重启前或外部客户端启动的 active Goal；"
                    "当前 SDK 无法安全补收通知并重挂。请先在原生 Codex 中暂停。"
                )
            )
    return {
        "tag": "column_set",
        "element_id": _GOAL_ELEMENT_ID,
        "flex_mode": "none",
        "background_style": "grey-50",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "padding": "12px",
                "vertical_spacing": "8px",
                "elements": elements,
            }
        ],
    }


def _goal_objective_preview(objective: str) -> str:
    if len(objective) <= _GOAL_OBJECTIVE_PREVIEW_CHARS:
        return objective
    return f"{objective[:_GOAL_OBJECTIVE_PREVIEW_CHARS]}…"


def _goal_control_button(
    *,
    scope: FeishuScope,
    goal: ReplyCardGoalModule,
    name: CardControlName,
    label: str,
    style: str | None = None,
    confirm: tuple[str, str] | None = None,
) -> dict[str, Any]:
    button = (
        _repeatable_callback_button
        if name in _REPEATABLE_CARD_CONTROL_NAMES
        else _callback_button
    )
    return button(
        label=label,
        value=_envelope(
            scope,
            name,
            binding_id=_binding_reference(goal.binding_id),
            goal_generation=goal.goal_generation,
            expected_goal_status=goal.status,
        ),
        style=style or "default",
        confirm=confirm,
    )


def _reply_card_manifest(projection: ReplyCardProjection) -> ReplyCardManifest:
    return ReplyCardManifest(
        goal=projection.goal,
        activity=projection.activity,
        result=projection.result,
    )


def _terminal_progress_status(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = getattr(value, "value", value)
    if normalized not in {"completed", "interrupted", "failed"}:
        raise ValueError(f"unsupported terminal progress status: {normalized!r}")
    return normalized


def _progress_card_chrome(status: str | None) -> tuple[str, str, str]:
    if status == "completed":
        return "任务已完成", "green", "任务已完成"
    if status == "interrupted":
        return "任务已中断", "orange", "任务已中断"
    if status == "failed":
        return "任务未完成", "red", "任务未完成"
    return "任务进行中", "blue", "任务正在执行，进度会逐步更新"


def _default_terminal_response(status: str) -> str:
    if status == "completed":
        return "任务已结束，未产生文本回复。"
    if status == "interrupted":
        return "Codex Turn 已中断。"
    return "任务未完成。"


def _turn_progress_panel(
    snapshot: _TurnActivitySnapshotLike,
    *,
    terminal_status: str | None,
    expanded: bool,
    hidden_steps: int = 0,
) -> dict[str, Any]:
    status_label = _progress_status_label(snapshot, terminal_status)
    return {
        "tag": "collapsible_panel",
        "element_id": _TURN_PROGRESS_ELEMENT_ID,
        "expanded": expanded,
        "border": {"color": "grey", "corner_radius": "8px"},
        "padding": "8px 12px 12px 12px",
        "vertical_spacing": "8px",
        "header": {
            "title": {
                "tag": "plain_text",
                "content": f"执行过程 · {status_label}",
            },
            "vertical_align": "center",
            "icon": {
                "tag": "standard_icon",
                "token": "down-small-ccm_outlined",
                "size": "16px 16px",
            },
            "icon_position": "right",
            "icon_expanded_angle": -180,
        },
        "elements": _turn_activity_elements(
            snapshot,
            status_label=status_label,
            hidden_steps=hidden_steps,
        ),
    }


def _progress_status_label(
    snapshot: _TurnActivitySnapshotLike,
    terminal_status: str | None,
) -> str:
    if terminal_status == "completed":
        return "已完成"
    if terminal_status == "interrupted":
        return "已中断"
    if terminal_status == "failed":
        return "未完成"
    state = getattr(snapshot.state, "value", snapshot.state)
    if state == ActiveState.STOPPING.value:
        return "正在停止"
    if state == ActiveState.OBSERVATION_UNAVAILABLE.value:
        return "Turn 观测不可用"
    return "正在执行"


def _turn_activity_elements(
    snapshot: _TurnActivitySnapshotLike,
    *,
    status_label: str,
    hidden_steps: int = 0,
) -> list[dict[str, Any]]:
    elements = [_plain(f"状态：{status_label}")]
    if snapshot.steer_count:
        elements.append(_plain(f"已接收调整：{snapshot.steer_count} 次"))
    commentary = tuple(getattr(snapshot, "commentary", ()))[
        -ACTIVITY_COMMENTARY_LIMIT:
    ]
    if commentary:
        elements.append({"tag": "markdown", "content": "**最近进展**"})
        for item in commentary:
            if item.text is None:
                continue
            elements.append(
                _activity_markdown_row(
                    item.event_timestamp_ms,
                    f"• {activity_step_display(item.text)}",
                )
            )
    operations = tuple(getattr(snapshot, "operations", ()))[
        -ACTIVITY_OPERATION_LIMIT:
    ]
    if operations:
        elements.append({"tag": "markdown", "content": "**最近操作**"})
        for item in operations:
            elements.append(
                _activity_markdown_row(
                    item.event_timestamp_ms,
                    _activity_operation_display(item),
                )
            )
    if not snapshot.plan_available:
        elements.append(_plain("过程信息：暂不可用"))
        return elements
    if not snapshot.plan_generated:
        suffix = (
            "（最近一次调整后仍在等待更新）"
            if snapshot.plan_may_be_stale
            else ""
        )
        elements.append(
            {
                "tag": "markdown",
                "content": f"**任务清单**：Codex 尚未生成{suffix}",
            }
        )
        return elements

    title = "**任务清单**"
    if snapshot.plan_may_be_stale:
        title += "（可能尚未反映最近一次调整）"
    elements.append({"tag": "markdown", "content": title})
    visible = snapshot.steps[:_TURN_PROGRESS_MAX_STEPS]
    if not visible:
        elements.append(_plain("（当前为空）"))
    icons = {"completed": "✓", "inProgress": "→", "pending": "○"}
    for item in visible:
        status = getattr(item.status, "value", item.status)
        elements.append(
            _plain(
                f"{icons.get(status, '○')} "
                f"{activity_step_display(item.step)}"
            )
        )
    remaining = len(snapshot.steps) - len(visible) + hidden_steps
    if remaining > 0:
        elements.append(_plain(f"… 另有 {remaining} 项未展示"))
    return elements


def _activity_operation_display(item: _TurnActivityEntryLike) -> str:
    kind = getattr(item.kind, "value", item.kind)
    status = getattr(item.status, "value", item.status)
    labels = {
        TurnActivityKind.COMMAND.value: "执行命令",
        TurnActivityKind.TOOL.value: "调用工具",
        TurnActivityKind.FILE_CHANGE.value: "修改文件",
        TurnActivityKind.WEB_SEARCH.value: "搜索网页",
        TurnActivityKind.IMAGE.value: "处理图片",
        TurnActivityKind.SUBAGENT.value: "子任务",
        TurnActivityKind.REVIEW.value: "代码审查",
        TurnActivityKind.COMPACTION.value: "压缩上下文",
    }
    icons = {
        TurnActivityStatus.IN_PROGRESS.value: "→",
        TurnActivityStatus.COMPLETED.value: "✓",
        TurnActivityStatus.FAILED.value: "×",
        TurnActivityStatus.DECLINED.value: "×",
        TurnActivityStatus.INTERRUPTED.value: "×",
    }
    label = labels.get(kind, "执行操作")
    if kind == TurnActivityKind.COMMAND.value and item.text:
        label = item.text
    count = item.count
    if kind in {
        TurnActivityKind.FILE_CHANGE.value,
        TurnActivityKind.SUBAGENT.value,
    } and count != 1:
        label += f"（{max(0, count)} 项）"
    if kind == TurnActivityKind.TOOL.value and item.text:
        label += f"：{item.text}"
    return f"{icons.get(status, '→')} {label}"


def _activity_markdown_row(
    timestamp_ms: int | None,
    content: str,
) -> dict[str, Any]:
    prefix = ""
    if timestamp_ms is not None:
        timestamp = _activity_timestamp_ms(timestamp_ms)
        assert timestamp is not None
        prefix = (
            f"<local_datetime millisecond='{timestamp}' "
            "format_type='date_num'></local_datetime> "
            f"<local_datetime millisecond='{timestamp}' "
            "format_type='time'></local_datetime> · "
        )
    return {
        "tag": "markdown",
        "content": prefix + _escape_activity_markdown(content),
    }


def _escape_activity_markdown(value: str) -> str:
    visible = normalize_activity_text_layout(value)
    escaped = html.escape(visible.replace("\\", "\\\\"), quote=False)
    for marker in ("`", "*", "_", "~", "[", "]", "(", ")"):
        escaped = escaped.replace(marker, f"\\{marker}")
    return escaped


def activity_step_display(value: str) -> str:
    """Return bounded display text with conservative credential redaction."""

    return sanitize_activity_text(value) or "未命名步骤"


def turn_files_card_from_manifest(
    *,
    scope: FeishuScope,
    binding_id: str,
    turn_id: str,
    final_response: str,
    manifest: tuple[TurnFileManifestItem, ...],
    page: int,
    additions: int | None = None,
    deletions: int | None = None,
) -> OutboundCard:
    """Rebuild a v4 card using only state carried by its page callback."""

    if not manifest:
        raise CardActionError("本轮文件清单为空，请重新执行任务。")
    if len(manifest) > TURN_FILE_MANIFEST_LIMIT:
        raise TurnFileCardLimitError(
            f"本轮文件共 {len(manifest)} 个，超过卡片完整分页上限 "
            f"{TURN_FILE_MANIFEST_LIMIT} 个；未截断文件清单。"
        )
    files = tuple(_inspect_manifest_file(entry) for entry in manifest)
    return reply_card(
        ReplyCardProjection(
            scope=scope,
            result=ReplyCardResultModule(final_response),
            files=ReplyCardFilesModule(
                binding_id=binding_id,
                turn_id=turn_id,
                items=tuple(_reply_file_item(item) for item in files),
                page=page,
                action_version=TURN_FILE_ACTION_VERSION,
                additions=additions,
                deletions=deletions,
            ),
        )
    )


def turn_progress_card_from_manifest(
    *,
    scope: FeishuScope,
    binding_id: str,
    turn_id: str,
    final_response: str,
    manifest: tuple[TurnFileManifestItem, ...],
    progress: TurnProgressManifest,
    page: int,
    additions: int | None = None,
    deletions: int | None = None,
) -> OutboundCard:
    """Rebuild a completed progress card from its self-contained callback."""

    if not manifest:
        raise CardActionError("本轮文件清单为空，请重新执行任务。")
    if len(manifest) > TURN_FILE_MANIFEST_LIMIT:
        raise TurnFileCardLimitError(
            f"本轮文件共 {len(manifest)} 个，超过卡片完整分页上限 "
            f"{TURN_FILE_MANIFEST_LIMIT} 个；未截断文件清单。"
        )
    files = tuple(_inspect_manifest_file(entry) for entry in manifest)
    return reply_card(
        ReplyCardProjection(
            scope=scope,
            activity=ReplyCardActivityModule(
                progress=progress,
                terminal_status="completed",
                collapsed=True,
            ),
            result=ReplyCardResultModule(final_response),
            files=ReplyCardFilesModule(
                binding_id=binding_id,
                turn_id=turn_id,
                items=tuple(_reply_file_item(item) for item in files),
                page=page,
                action_version=TURN_FILE_ACTION_VERSION,
                additions=additions,
                deletions=deletions,
            ),
        )
    )


def _validate_turn_card_size(
    card: Mapping[str, Any],
    *,
    label: str,
    untruncated: str,
) -> None:
    # Match the Channel SDK's actual outbound Card serialization rather than
    # undercounting with compact separators.
    encoded_size = len(json.dumps(card, ensure_ascii=False).encode("utf-8"))
    if encoded_size > TURN_FILE_CARD_JSON_LIMIT_BYTES:
        raise TurnFileCardLimitError(
            f"{label}编码后为 {encoded_size} bytes，"
            "超过已验证的平台安全上限 "
            f"{TURN_FILE_CARD_JSON_LIMIT_BYTES} bytes；未截断{untruncated}。"
        )


def is_turn_file_action(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    return value.get("intent") in {item.value for item in TurnFileActionName}


def decode_turn_file_action(
    *,
    app_id: str,
    message_id: str,
    callback_chat_id: str,
    sender_id: str,
    tag: str,
    value: Any,
) -> TurnFileActionIntent:
    if tag != "button":
        raise CardActionError(f"不支持的本轮文件组件：{tag or 'unknown'}")
    if not message_id or not callback_chat_id or not sender_id:
        raise CardActionError("本轮文件回调缺少消息、聊天或操作者标识。")
    if not isinstance(value, Mapping):
        raise CardActionError("本轮文件动作 value 必须是对象。")
    payload = dict(value)
    payload.pop("nonce", None)
    try:
        name = TurnFileActionName(payload.get("intent"))
    except (TypeError, ValueError) as error:
        raise CardActionError("未知本轮文件动作。") from error
    version = payload.get("v")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version
        not in {TURN_FILE_ACTION_VERSION, REPLY_CARD_ACTION_VERSION}
    ):
        raise CardActionError("本轮文件卡片已过期，请重新执行任务。")
    try:
        scope_kind = ScopeKind(payload.get("scope_kind"))
    except (TypeError, ValueError) as error:
        raise CardActionError("未知 Scope kind。") from error
    common = {
        "v",
        "intent",
        "chat_id",
        "scope_kind",
        "binding_id",
        "turn_id",
    }
    scope_fields = {"topic_id"} if scope_kind is ScopeKind.TOPIC else set()
    action_fields = {"path"}
    allowed_action_fields = (action_fields,)
    if name is TurnFileActionName.PAGE:
        if version == TURN_FILE_ACTION_VERSION:
            action_fields = {"page", "files", "answer"}
            allowed_action_fields = (
                action_fields,
                action_fields | {"a", "d"},
                action_fields | {"progress"},
                action_fields | {"progress", "a", "d"},
            )
        else:
            action_fields = {"page", "files", "reply"}
            allowed_action_fields = (
                action_fields,
                action_fields | {"a", "d"},
            )
    if not any(
        set(payload) == common | scope_fields | fields
        for fields in allowed_action_fields
    ):
        raise CardActionError("本轮文件动作字段不完整或包含未知字段。")
    if payload["chat_id"] != callback_chat_id:
        raise CardActionError("本轮文件卡片与当前聊天不一致。")
    scope = _scope_from_envelope(
        app_id=app_id,
        chat_id=callback_chat_id,
        kind=scope_kind,
        topic_id=payload.get("topic_id"),
    )
    binding_id = _decode_binding_reference(
        _required_string(payload["binding_id"], "binding_id")
    )
    turn_id = _decode_turn_reference(
        _required_string(payload["turn_id"], "turn_id")
    )
    page = None
    path = None
    files: tuple[TurnFileManifestItem, ...] = ()
    answer = None
    progress = None
    reply = None
    additions = None
    deletions = None
    if name is TurnFileActionName.PAGE:
        raw_page = payload["page"]
        if (
            isinstance(raw_page, bool)
            or not isinstance(raw_page, int)
            or raw_page < 0
        ):
            raise CardActionError("本轮文件页码必须是非负整数。")
        page = raw_page
        files = _decode_turn_file_manifest(payload["files"])
        if "a" in payload:
            try:
                additions, deletions = _optional_line_counts(
                    payload["a"],
                    payload["d"],
                    field="files",
                )
            except ValueError as error:
                raise CardActionError("本轮文件总行数统计无效。") from error
        if version == TURN_FILE_ACTION_VERSION:
            answer = _required_string(payload["answer"], "answer")
            if len(answer) > 100_000 or "\x00" in answer:
                raise CardActionError("本轮文件卡片回答内容无效。")
            if "progress" in payload:
                progress = _decode_turn_progress_manifest(payload["progress"])
        else:
            reply = _decode_reply_card_manifest(
                payload["reply"],
                binding_id=binding_id,
            )
            if reply.result is None:
                raise CardActionError("组合回复分页缺少结果模块。")
            answer = reply.result.content
            progress = (
                reply.activity.progress if reply.activity is not None else None
            )
    else:
        path = _decode_turn_file_path(payload["path"])
    return TurnFileActionIntent(
        scope=scope,
        source_id=message_id,
        sender_id=sender_id,
        name=name,
        binding_id=binding_id,
        turn_id=turn_id,
        page=page,
        path=path,
        files=files,
        answer=answer,
        progress=progress,
        reply=reply,
        additions=additions,
        deletions=deletions,
    )


def _decode_turn_file_manifest(value: Any) -> tuple[TurnFileManifestItem, ...]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > TURN_FILE_MANIFEST_LIMIT
    ):
        raise CardActionError(
            f"本轮文件清单必须包含 1–{TURN_FILE_MANIFEST_LIMIT} 个文件。"
        )
    results: list[TurnFileManifestItem] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) not in (
            {"path", "label"},
            {"path", "label", "a", "d"},
        ):
            raise CardActionError("本轮文件清单条目字段无效。")
        path = _decode_turn_file_path(item["path"])
        label = _required_string(item["label"], "label")
        if len(label) > 1024 or "\x00" in label:
            raise CardActionError("本轮文件显示名称无效。")
        if path in seen:
            raise CardActionError("本轮文件清单包含重复路径。")
        seen.add(path)
        additions = None
        deletions = None
        if "a" in item:
            try:
                additions, deletions = _optional_line_counts(
                    item["a"],
                    item["d"],
                    field="file",
                )
            except ValueError as error:
                raise CardActionError("本轮文件行数统计无效。") from error
        results.append(
            TurnFileManifestItem(
                path=path,
                label=label,
                additions=additions,
                deletions=deletions,
            )
        )
    return tuple(results)


def _decode_turn_progress_manifest(value: Any) -> TurnProgressManifest:
    if not isinstance(value, Mapping):
        raise CardActionError("进度卡过程字段无效。")
    payload = dict(value)
    expected = {
        "state",
        "steer_count",
        "plan_available",
        "plan_generated",
        "plan_may_be_stale",
        "steps",
        "commentary",
        "operations",
    }
    if set(payload) != expected:
        raise CardActionError("进度卡过程字段不完整或包含未知字段。")
    state = _required_string(payload["state"], "progress.state")
    if state not in ACTIVE_STATE_VALUES:
        raise CardActionError("进度卡过程状态无效。")
    steer_count = payload["steer_count"]
    if (
        isinstance(steer_count, bool)
        or not isinstance(steer_count, int)
        or not 0 <= steer_count <= 1_000_000
    ):
        raise CardActionError("进度卡调整次数无效。")
    flags: dict[str, bool] = {}
    for field in (
        "plan_available",
        "plan_generated",
        "plan_may_be_stale",
    ):
        raw = payload[field]
        if type(raw) is not bool:
            raise CardActionError("进度卡计划状态无效。")
        flags[field] = raw
    raw_steps = payload["steps"]
    if not isinstance(raw_steps, list) or len(raw_steps) > _TURN_PROGRESS_MAX_STEPS:
        raise CardActionError("进度卡计划步骤无效。")
    steps: list[TurnProgressManifestStep] = []
    for raw in raw_steps:
        if not isinstance(raw, Mapping) or set(raw) != {"step", "status"}:
            raise CardActionError("进度卡计划步骤字段无效。")
        step = _required_string(raw["step"], "progress.step")
        if len(step) > _TURN_PROGRESS_STEP_MAX_CHARS or "\x00" in step:
            raise CardActionError("进度卡计划步骤内容无效。")
        status = _required_string(raw["status"], "progress.status")
        if status not in {"pending", "inProgress", "completed"}:
            raise CardActionError("进度卡计划步骤状态无效。")
        steps.append(
            TurnProgressManifestStep(
                step=activity_step_display(step),
                status=status,
            )
        )
    if not flags["plan_generated"] and steps:
        raise CardActionError("未生成计划的进度卡不能携带步骤。")
    raw_commentary = payload["commentary"]
    if (
        not isinstance(raw_commentary, list)
        or len(raw_commentary) > ACTIVITY_COMMENTARY_LIMIT
    ):
        raise CardActionError("进度卡进展摘要无效。")
    commentary: list[TurnCommentaryManifestEntry] = []
    for raw in raw_commentary:
        event_timestamp_ms: int | None
        if isinstance(raw, str):
            text = _required_string(raw, "progress.commentary")
            event_timestamp_ms = None
        elif isinstance(raw, Mapping) and set(raw) == {
            "text",
            "event_timestamp_ms",
        }:
            text = _required_string(raw["text"], "progress.commentary.text")
            event_timestamp_ms = _decode_activity_timestamp_ms(
                raw["event_timestamp_ms"],
                "progress.commentary.event_timestamp_ms",
            )
        else:
            raise CardActionError("进度卡进展摘要字段无效。")
        sanitized = sanitize_activity_text(text)
        if sanitized is None or len(text) > _TURN_PROGRESS_STEP_MAX_CHARS:
            raise CardActionError("进度卡进展摘要内容无效。")
        commentary.append(
            TurnCommentaryManifestEntry(
                text=sanitized,
                event_timestamp_ms=event_timestamp_ms,
            )
        )
    raw_operations = payload["operations"]
    if (
        not isinstance(raw_operations, list)
        or len(raw_operations) > ACTIVITY_OPERATION_LIMIT
    ):
        raise CardActionError("进度卡操作清单无效。")
    operations: list[TurnActivityManifestEntry] = []
    allowed_kinds = {
        kind.value for kind in TurnActivityKind if kind is not TurnActivityKind.COMMENTARY
    }
    allowed_statuses = {status.value for status in TurnActivityStatus}
    for raw in raw_operations:
        if not isinstance(raw, Mapping):
            raise CardActionError("进度卡操作字段无效。")
        fields = set(raw)
        legacy_fields = {"kind", "status", "text", "count"}
        timestamped_fields = legacy_fields | {"event_timestamp_ms"}
        if frozenset(fields) not in {
            frozenset(legacy_fields),
            frozenset(timestamped_fields),
        }:
            raise CardActionError("进度卡操作字段无效。")
        kind = _required_string(raw["kind"], "progress.operation.kind")
        status = _required_string(raw["status"], "progress.operation.status")
        if kind not in allowed_kinds or status not in allowed_statuses:
            raise CardActionError("进度卡操作类型或状态无效。")
        text_value = raw["text"]
        text: str | None
        if text_value is None:
            text = None
        else:
            text = _required_string(text_value, "progress.operation.text")
            try:
                text = _activity_operation_text(kind, text)
            except ValueError as error:
                raise CardActionError("进度卡操作内容无效。") from error
        event_timestamp_ms = (
            _decode_activity_timestamp_ms(
                raw["event_timestamp_ms"],
                "progress.operation.event_timestamp_ms",
            )
            if "event_timestamp_ms" in raw
            else None
        )
        count = raw["count"]
        if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 1_000_000:
            raise CardActionError("进度卡操作数量无效。")
        operations.append(
            TurnActivityManifestEntry(
                kind=kind,
                status=status,
                event_timestamp_ms=event_timestamp_ms,
                text=text,
                count=count,
            )
        )
    return TurnProgressManifest(
        state=state,
        steer_count=steer_count,
        plan_available=flags["plan_available"],
        plan_generated=flags["plan_generated"],
        plan_may_be_stale=flags["plan_may_be_stale"],
        steps=tuple(steps),
        commentary=tuple(commentary),
        operations=tuple(operations),
    )


def _decode_reply_card_manifest(
    value: Any,
    *,
    binding_id: str,
) -> ReplyCardManifest:
    if not isinstance(value, Mapping) or set(value) != {
        "goal",
        "activity",
        "result",
    }:
        raise CardActionError("组合回复清单字段不完整或包含未知字段。")
    goal = _decode_reply_goal_module(value["goal"], binding_id=binding_id)
    activity = _decode_reply_activity_module(value["activity"])
    result = _decode_reply_result_module(value["result"])
    if goal is None and activity is None and result is None:
        raise CardActionError("组合回复清单不能为空。")
    return ReplyCardManifest(goal=goal, activity=activity, result=result)


def _decode_reply_goal_module(
    value: Any,
    *,
    binding_id: str,
) -> ReplyCardGoalModule | None:
    if value is None:
        return None
    expected = {
        "binding_id",
        "short_id",
        "project_alias",
        "goal_generation",
        "status",
        "runtime_state",
        "objective",
        "token_budget",
        "tokens_used",
        "notice",
        "notice_is_error",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise CardActionError("Goal 模块字段不完整或包含未知字段。")
    manifest_binding_id = _decode_binding_reference(
        _required_string(value["binding_id"], "goal.binding_id")
    )
    if manifest_binding_id != binding_id:
        raise CardActionError("Goal 与 Files 模块的会话身份不一致。")
    status = value["status"]
    if status is not None:
        status = _required_string(status, "goal.status")
        if status not in {item.value for item in GoalStatus}:
            raise CardActionError("Goal 模块状态无效。")
    generation = value["goal_generation"]
    if status is None:
        if generation is not None:
            raise CardActionError("空 Goal 模块不能携带 generation。")
    else:
        generation = _decode_goal_generation(generation)
    runtime_state = _optional_bounded_string(
        value["runtime_state"], "goal.runtime_state", 128
    )
    objective = _optional_bounded_string(
        value["objective"], "goal.objective", 10_000
    )
    token_budget = _optional_nonnegative_int(
        value["token_budget"], "goal.token_budget"
    )
    tokens_used = _decode_nonnegative_int(value["tokens_used"], "goal.tokens_used")
    notice = _optional_bounded_string(value["notice"], "goal.notice", 4_000)
    notice_is_error = value["notice_is_error"]
    if type(notice_is_error) is not bool:
        raise CardActionError("Goal 模块 notice_is_error 无效。")
    if status is None and any(
        item is not None
        for item in (runtime_state, objective, token_budget)
    ):
        raise CardActionError("空 Goal 模块不能携带 Goal 状态。")
    if status is None and tokens_used != 0:
        raise CardActionError("空 Goal 模块不能携带 Token 用量。")
    if status is not None and objective is None:
        raise CardActionError("Goal 模块缺少 Objective。")
    goal = ReplyCardGoalModule(
        binding_id=binding_id,
        short_id=_bounded_decode_string(value["short_id"], "goal.short_id", 32),
        project_alias=_bounded_decode_string(
            value["project_alias"], "goal.project_alias", 128
        ),
        goal_generation=generation,
        status=status,
        runtime_state=runtime_state,
        objective=objective,
        token_budget=token_budget,
        tokens_used=tokens_used,
        notice=notice,
        notice_is_error=notice_is_error,
    )
    try:
        return _normalize_goal_module(goal)
    except ValueError as error:
        raise CardActionError("Goal 模块内容无效。") from error


def _decode_reply_activity_module(value: Any) -> ReplyCardActivityModule | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {
        "progress",
        "terminal_status",
        "collapsed",
        "hidden_steps",
    }:
        raise CardActionError("Activity 模块字段不完整或包含未知字段。")
    progress = _decode_turn_progress_manifest(value["progress"])
    terminal_status = value["terminal_status"]
    if terminal_status is not None:
        terminal_status = _required_string(
            terminal_status, "activity.terminal_status"
        )
        try:
            terminal_status = _terminal_progress_status(terminal_status)
        except ValueError as error:
            raise CardActionError("Activity 模块终态无效。") from error
    collapsed = value["collapsed"]
    if type(collapsed) is not bool:
        raise CardActionError("Activity 模块折叠状态无效。")
    if collapsed != (terminal_status is not None):
        raise CardActionError("Activity 模块折叠状态与终态不一致。")
    hidden_steps = _decode_nonnegative_int(
        value["hidden_steps"], "activity.hidden_steps"
    )
    return ReplyCardActivityModule(
        progress=progress,
        terminal_status=terminal_status,
        collapsed=collapsed,
        hidden_steps=hidden_steps,
    )


def _decode_reply_result_module(value: Any) -> ReplyCardResultModule | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"content"}:
        raise CardActionError("Result 模块字段不完整或包含未知字段。")
    return ReplyCardResultModule(
        _bounded_decode_string(value["content"], "result.content", 100_000)
    )


def _decode_goal_generation(value: Any) -> str:
    generation = _required_string(value, "goal_generation")
    if _GOAL_GENERATION.fullmatch(generation) is None:
        raise CardActionError("Goal generation 无效或已过期。")
    return generation


def _bounded_decode_string(value: Any, field: str, limit: int) -> str:
    result = _required_string(value, field)
    if len(result) > limit or "\x00" in result:
        raise CardActionError(f"{field} 内容无效。")
    return result


def _optional_bounded_string(
    value: Any,
    field: str,
    limit: int,
) -> str | None:
    if value is None:
        return None
    return _bounded_decode_string(value, field, limit)


def _decode_nonnegative_int(value: Any, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= 10**18
    ):
        raise CardActionError(f"{field} 数值无效。")
    return value


def _optional_nonnegative_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    return _decode_nonnegative_int(value, field)


def _decode_turn_file_path(value: Any) -> str:
    path = _required_string(value, "path")
    if len(path) > 8192 or "\x00" in path or not Path(path).is_absolute():
        raise CardActionError("本轮文件路径必须是有效的绝对路径。")
    return path


def settings_card(
    *,
    scope: FeishuScope,
    projects: tuple[Project, ...],
    project_root: str,
    section: SettingsSection = SettingsSection.PROJECTS,
    notice: str | None = None,
    notice_is_error: bool = False,
) -> OutboundCard:
    builder = _builder("Netizen 设置", _settings_section_label(section))
    builder.raw(_settings_navigation(scope=scope, active=section))
    builder.markdown(
        "当前 Scope 的参与者均可操作；实例级设置会影响其他 Scope。"
    )
    if notice:
        builder.raw(_notice(notice, error=notice_is_error))

    if section is SettingsSection.PROJECTS:
        _render_projects_settings(
            builder,
            scope=scope,
            projects=projects,
            project_root=project_root,
        )
    else:  # pragma: no cover - enum and renderer must evolve together
        raise ValueError(f"unsupported settings section: {section}")

    return OutboundCard(card=builder.to_dict())


def _render_projects_settings(
    builder: Any,
    *,
    scope: FeishuScope,
    projects: tuple[Project, ...],
    project_root: str,
) -> None:
    builder.markdown(
        "**实例级 Project Registry**\n"
        "Project 对整个 Netizen 实例共享。"
    )
    builder.markdown("**管理 Project**")
    if projects:
        builder.raw(_project_management_form(projects))
    else:
        builder.markdown("当前没有可管理的 Project，请在下方新增。")

    builder.raw(
        _repeatable_callback_button(
            label="刷新当前配置",
            value=_envelope(
                scope,
                CardControlName.REFRESH_SETTINGS,
                settings_section=SettingsSection.PROJECTS.value,
            ),
        )
    )
    builder.divider()
    builder.markdown("**新增 Project**")
    builder.raw(_project_create_form(project_root))
    builder.markdown(
        f"<font color='grey'>空 Project 默认创建在 `{_md_code(project_root)}`；"
        "Netizen 从不删除 Project 目录。</font>"
    )


def _settings_navigation(
    *,
    scope: FeishuScope,
    active: SettingsSection,
) -> dict[str, Any]:
    return _button_row(
        *(
            _repeatable_callback_button(
                label=_settings_section_label(section),
                value=_envelope(
                    scope,
                    CardControlName.OPEN_SETTINGS_SECTION,
                    settings_section=section.value,
                ),
                style="primary_filled" if section is active else "default",
            )
            for section in SettingsSection
        )
    )


def _settings_section_label(section: SettingsSection) -> str:
    if section is SettingsSection.PROJECTS:
        return "Projects"
    raise ValueError(f"unsupported settings section: {section}")


def _project_management_form(projects: tuple[Project, ...]) -> dict[str, Any]:
    return {
        "tag": "form",
        "name": "project_manage_v1",
        "elements": [
            {
                "tag": "select_static",
                "name": "project_manage_target",
                "required": True,
                "placeholder": _plain_text("选择 Project"),
                "options": [
                    {
                        "text": _plain_text(
                            f"{project.alias} · "
                            f"{'已启用' if project.enabled else '已停用'}"
                        ),
                        "value": _project_reference(project),
                    }
                    for project in projects
                ],
            },
            {
                "tag": "select_static",
                "name": "project_manage_operation",
                "required": True,
                "placeholder": _plain_text("选择操作"),
                "options": [
                    {"text": _plain_text("启用"), "value": "enable"},
                    {"text": _plain_text("停用"), "value": "disable"},
                ],
            },
            {
                "tag": "button",
                "name": "project_manage_submit_v1",
                "text": _plain_text("应用到所选 Project"),
                "type": "primary",
                "width": "fill",
                "form_action_type": "submit",
                "confirm": {
                    "title": _plain_text("确认修改 Project？"),
                    "text": _plain_text(
                        "状态会立即更新；停用只阻止创建新会话，已有会话仍可继续。"
                    ),
                },
            },
        ],
    }


def _project_create_form(project_root: str) -> dict[str, Any]:
    callback_nonce = _new_callback_nonce()
    return {
        "tag": "form",
        "name": "project_create_v1",
        "elements": [
            {
                "tag": "input",
                "name": "project_alias",
                "required": True,
                "label": _plain_text("Alias"),
                "placeholder": _plain_text("例如：demo_project"),
                "max_length": 64,
            },
            {
                "tag": "div",
                "text": _plain_text("模式"),
            },
            {
                "tag": "select_static",
                "name": "project_mode",
                "required": True,
                "initial_option": _project_mode_reference(
                    "create",
                    callback_nonce,
                ),
                "options": [
                    {
                        "text": _plain_text("创建空目录"),
                        "value": _project_mode_reference(
                            "create",
                            callback_nonce,
                        ),
                    },
                    {
                        "text": _plain_text("登记已有目录"),
                        "value": _project_mode_reference(
                            "existing",
                            callback_nonce,
                        ),
                    },
                ],
            },
            {
                "tag": "input",
                "name": "project_path",
                "label": _plain_text("绝对路径（创建模式可留空）"),
                "placeholder": _plain_text(str(project_root)),
                "max_length": 1000,
            },
            {
                "tag": "button",
                "name": "project_submit_v1",
                "text": _plain_text("保存 Project"),
                "type": "primary_filled",
                "width": "fill",
                "form_action_type": "submit",
            },
        ],
    }


def _new_binding_form(
    projects: tuple[Project, ...],
    catalog: ModelCatalog | None,
    *,
    initial_project_alias: str | None,
    allow_context_mode: bool,
    message_context_mode: MentionContextMode,
    task_feedback: BindingTaskFeedback,
) -> dict[str, Any]:
    initial_project = next(
        (
            project
            for project in projects
            if project.alias == initial_project_alias
        ),
        None,
    )
    elements = [
        _form_label("Project"),
        _static_select(
            name="new_project",
            placeholder="选择 Project",
            options=tuple(
                (
                    f"{project.alias} · {project.cwd}",
                    _project_reference(project),
                )
                for project in projects
            ),
            initial_option=(
                _project_reference(initial_project)
                if initial_project is not None
                else None
            ),
        ),
    ]
    if catalog is None:
        elements.extend(
            [
                _form_label("Model"),
                _static_select(
                    name="new_model",
                    placeholder="选择 Model 来源",
                    options=(("继承 Codex", _new_model_reference(None)),),
                    initial_option=_new_model_reference(None),
                ),
            ]
        )
    else:
        elements.extend(
            _model_settings_form_elements(
                prefix="new",
                catalog=catalog,
                model_value_encoder=_new_model_reference,
                inherit_initial_when_unset=False,
            )
        )
    if allow_context_mode:
        elements.extend(
            _context_mode_form_elements(
                prefix="new",
                initial_mode=message_context_mode,
            )
        )
    elements.extend(
        _task_feedback_form_elements(prefix="new", initial=task_feedback)
    )
    elements.append(
        _form_submit_button(name="new_binding_submit_v6", label="新建会话")
    )
    return {
        "tag": "form",
        "name": "new_binding_v6",
        "elements": elements,
    }


def _binding_config_form(
    *,
    binding_id: str,
    settings_revision: int,
    context_revision: int,
    feedback_revision: int,
    turn_settings: BindingTurnSettings | None,
    message_context_mode: MentionContextMode,
    task_feedback: BindingTaskFeedback,
    allow_context_mode: bool,
    catalog: ModelCatalog | None,
) -> dict[str, Any]:
    def model_reference(model_id: str | None) -> str:
        return _config_model_reference(
            binding_id=binding_id,
            settings_revision=settings_revision,
            context_revision=context_revision,
            feedback_revision=feedback_revision,
            model_id=model_id,
        )

    elements: list[dict[str, Any]] = []
    if catalog is None:
        elements.extend(
            [
                _form_label("Model"),
                _static_select(
                    name="config_model",
                    placeholder="选择 Model 来源",
                    options=(("继承 Codex", model_reference(None)),),
                    initial_option=model_reference(None),
                ),
            ]
        )
    else:
        elements.extend(
            _model_settings_form_elements(
                prefix="config",
                catalog=catalog,
                turn_settings=turn_settings,
                model_value_encoder=model_reference,
                inherit_initial_when_unset=True,
            )
        )
    if allow_context_mode:
        elements.extend(
            _context_mode_form_elements(
                prefix="config",
                initial_mode=message_context_mode,
            )
        )
    elements.extend(
        _task_feedback_form_elements(prefix="config", initial=task_feedback)
    )
    elements.append(
        _form_submit_button(
            name="binding_config_submit_v6",
            label="保存会话配置",
        )
    )
    return {
        "tag": "form",
        "name": "binding_config_v6",
        "elements": elements,
    }


def _model_settings_form_elements(
    *,
    prefix: str,
    catalog: ModelCatalog,
    turn_settings: BindingTurnSettings | None = None,
    model_value_encoder: Callable[[str | None], str],
    inherit_initial_when_unset: bool,
    model_label: str = "Model",
) -> list[dict[str, Any]]:
    default = catalog.default_model
    model_id: str | None = (
        None if inherit_initial_when_unset else default.id
    )
    effort_id = default.default_effort_id
    service_tier_id = default.default_service_tier_id
    if turn_settings is not None:
        try:
            catalog.resolve(
                model_id=turn_settings.model_id,
                effort_id=turn_settings.effort_id,
                service_tier_id=turn_settings.service_tier_id,
            )
        except ValueError:
            pass
        else:
            model_id = turn_settings.model_id
            effort_id = turn_settings.effort_id
            service_tier_id = turn_settings.service_tier_id

    return [
        _form_label(model_label),
        _static_select(
            name=f"{prefix}_model",
            placeholder="选择 Model",
            options=tuple(
                [("继承 Codex", model_value_encoder(None))]
                + [
                    (
                        model.display_name
                        + (" · 默认" if model.is_default else ""),
                        model_value_encoder(model.id),
                    )
                    for model in catalog.models
                ]
            ),
            initial_option=model_value_encoder(model_id),
        ),
        _form_label("Effort"),
        _static_select(
            name=f"{prefix}_effort",
            placeholder="选择 Effort",
            options=tuple(
                (option.id, option.id) for option in catalog.effort_options
            ),
            initial_option=effort_id,
        ),
        _form_label("Speed"),
        _static_select(
            name=f"{prefix}_speed",
            placeholder="选择 Speed",
            options=tuple(
                (option.name, option.id)
                for option in catalog.service_tier_options
            ),
            initial_option=service_tier_id,
        ),
    ]


def context_mode_display(mode: MentionContextMode) -> str:
    if mode is MentionContextMode.CATCH_UP:
        return "自动带上期间的群聊讨论"
    return "仅这条 @ 消息"


def _context_mode_form_elements(
    *,
    prefix: str,
    initial_mode: MentionContextMode,
) -> list[dict[str, Any]]:
    return [
        _form_label("@ 时读取的消息范围"),
        _static_select(
            name=f"{prefix}_context_mode",
            placeholder="选择 @ 时读取的消息范围",
            options=(
                (
                    "仅这条 @ 消息（默认）",
                    _context_mode_reference(MentionContextMode.CURRENT_ONLY),
                ),
                (
                    "自动带上期间的群聊讨论",
                    _context_mode_reference(MentionContextMode.CATCH_UP),
                ),
            ),
            initial_option=_context_mode_reference(initial_mode),
        ),
        _form_hint(
            "机器人始终只响应 @ 它的消息。选择“自动带上”后，"
            "两次 @ 之间群里其他成员未 @ 机器人的消息，"
            "也会在下一次 @ 时被读取，作为背景交给 Codex。"
        ),
    ]


def _task_feedback_form_elements(
    *,
    prefix: str,
    initial: BindingTaskFeedback,
) -> list[dict[str, Any]]:
    return [
        _form_label("执行中表情闪烁"),
        _static_select(
            name=f"{prefix}_task_reactions",
            placeholder="选择是否显示执行中表情闪烁",
            options=(
                ("关闭（默认）", _task_feedback_reference(False)),
                ("开启", _task_feedback_reference(True)),
            ),
            initial_option=_task_feedback_reference(
                initial.reaction_pulse_enabled
            ),
        ),
        _form_hint(
            "任务接收、成功调整和结束时始终显示表情。"
            "开启后，执行中还会间歇显示动态表情；"
            "部分移动端可能将其显示为单独消息。"
        ),
        _form_label("进度卡"),
        _static_select(
            name=f"{prefix}_progress_card",
            placeholder="选择是否使用进度卡",
            options=(
                ("关闭（默认）", _task_feedback_reference(False)),
                ("开启", _task_feedback_reference(True)),
            ),
            initial_option=_task_feedback_reference(
                initial.progress_card_enabled
            ),
        ),
        _form_hint(
            "开启后会逐步更新任务状态与清单；"
            "完成后执行过程自动折叠。"
        ),
    ]


def _form_label(label: str) -> dict[str, Any]:
    return {"tag": "div", "text": _plain_text(label)}


def _form_hint(text: str) -> dict[str, Any]:
    return {
        "tag": "markdown",
        "content": f"<font color='grey'>{text}</font>",
    }


def _static_select(
    *,
    name: str,
    placeholder: str,
    options: tuple[tuple[str, str], ...],
    initial_option: str | None,
) -> dict[str, Any]:
    select = {
        "tag": "select_static",
        "name": name,
        "required": True,
        "placeholder": _plain_text(placeholder),
        "options": [
            {"text": _plain_text(label), "value": value}
            for label, value in options
        ],
    }
    if initial_option is not None:
        select["initial_option"] = initial_option
    return select


def _form_submit_button(*, name: str, label: str) -> dict[str, Any]:
    return {
        "tag": "button",
        "name": name,
        "text": _plain_text(label),
        "type": "primary_filled",
        "width": "fill",
        "form_action_type": "submit",
    }


def new_binding_card(
    *,
    scope: FeishuScope,
    projects: tuple[Project, ...],
    initial_project_alias: str | None = None,
    catalog: ModelCatalog | None = None,
    catalog_error: str | None = None,
    allow_context_mode: bool = True,
    message_context_mode: MentionContextMode = MentionContextMode.CURRENT_ONLY,
    task_feedback: BindingTaskFeedback | None = None,
) -> OutboundCard:
    builder = _builder("新建会话", "选择 Project 与会话配置")
    builder.markdown(
        "只创建 lazy Binding，不会立即启动任务。Model 可继承 Codex；"
        "显式选择的 Model / Effort / Speed 会应用于后续每条新 Turn。"
    )
    if not projects:
        builder.markdown("当前没有可用 Project。请先发送 `/settings` 新增或启用。")
    else:
        if catalog is None:
            builder.raw(
                _notice(
                    (catalog_error or "Model / Effort / Speed 暂不可用。")
                    + " 当前仍可选择 Project 并继承 Codex 创建会话。",
                    error=True,
                )
            )
        builder.raw(
            _new_binding_form(
                projects,
                catalog,
                initial_project_alias=initial_project_alias,
                allow_context_mode=allow_context_mode,
                message_context_mode=message_context_mode,
                task_feedback=task_feedback or BindingTaskFeedback(),
            )
        )
    return OutboundCard(card=builder.to_dict())


def config_card(
    *,
    scope: FeishuScope,
    binding_id: str,
    short_id: str,
    project_alias: str,
    settings_revision: int,
    turn_settings: BindingTurnSettings | None,
    catalog: ModelCatalog | None,
    context_revision: int = 1,
    feedback_revision: int = 1,
    message_context_mode: MentionContextMode = MentionContextMode.CURRENT_ONLY,
    task_feedback: BindingTaskFeedback | None = None,
    allow_context_mode: bool = True,
    catalog_error: str | None = None,
) -> OutboundCard:
    builder = _builder("当前会话配置", f"{short_id} · {project_alias}")
    builder.markdown(
        "Model 可继承 Codex，也可显式选择 Model / Effort / Speed；"
        "保存不会启动任务。"
    )
    if catalog is None:
        builder.raw(
            _notice(
                (catalog_error or "Model / Effort / Speed 暂不可用。")
                + " 当前仍可清除显式配置并改为继承 Codex。",
                error=True,
            )
        )
    elif turn_settings is not None:
        try:
            catalog.resolve(
                model_id=turn_settings.model_id,
                effort_id=turn_settings.effort_id,
                service_tier_id=turn_settings.service_tier_id,
            )
        except ValueError:
            builder.raw(
                _notice(
                    "已保存的会话配置不再出现在当前模型目录中；"
                    "发送任务会明确失败并保留配置。请在下方重新选择三项配置。",
                    error=True,
                )
            )
    builder.raw(
        _binding_config_form(
            binding_id=binding_id,
            settings_revision=settings_revision,
            context_revision=context_revision,
            feedback_revision=feedback_revision,
            turn_settings=turn_settings,
            message_context_mode=message_context_mode,
            task_feedback=task_feedback or BindingTaskFeedback(),
            allow_context_mode=allow_context_mode,
            catalog=catalog,
        )
    )
    return OutboundCard(card=builder.to_dict())


def rename_binding_card(
    *,
    scope: FeishuScope,
    binding_id: str,
    short_id: str,
    project_alias: str,
    current_title: str,
) -> OutboundCard:
    builder = _builder("重命名当前会话", f"{short_id} · {project_alias}")
    builder.markdown(
        f"当前名称：`{_md_code(current_title)}`\n"
        "名称直接写入原生 Codex Thread，并同步显示在 Codex App/CLI。"
    )
    builder.raw(
        {
            "tag": "form",
            "name": "binding_rename_v1",
            "elements": [
                {
                    "tag": "input",
                    "name": _rename_name_field(binding_id),
                    "required": True,
                    "label": _plain_text("新名称"),
                    "placeholder": _plain_text("输入新的会话名称"),
                    "max_length": MAX_THREAD_NAME_CHARS,
                },
                _form_submit_button(
                    name="binding_rename_submit_v1",
                    label="保存新名称",
                ),
            ],
        }
    )
    return OutboundCard(card=builder.to_dict())


def archive_binding_card(
    *,
    scope: FeishuScope,
    binding_id: str,
    short_id: str,
    project_alias: str,
    title: str,
) -> OutboundCard:
    builder = _builder("归档当前会话", f"{short_id} · {project_alias}")
    builder.markdown(
        f"即将归档：`{_md_code(title)}`\n"
        "Codex 会处理该 Thread 当前的原生活动后执行归档。"
        "归档会同步影响 Codex App/CLI；历史不会删除，之后可以恢复。"
    )
    builder.raw(
        _callback_button(
            label="确认归档当前会话",
            value=_envelope(
                scope,
                CardControlName.ARCHIVE_BINDING,
                binding_id=_binding_reference(binding_id),
            ),
            style="primary_filled",
            confirm=(
                "确认归档当前会话？",
                "成功后当前会话会清空，后续消息不会自动切换到其他会话。",
            ),
        )
    )
    return OutboundCard(card=builder.to_dict())


def delete_binding_card(
    *,
    scope: FeishuScope,
    binding_id: str,
    short_id: str,
    project_alias: str,
    title: str,
    native_thread_id: str | None = None,
) -> OutboundCard:
    builder = _builder(
        "永久删除当前会话",
        f"{short_id} · {project_alias}",
        template="red",
    )
    if native_thread_id is None:
        builder.markdown(
            f"即将删除：`{_md_code(title)}`\n"
            "该 Lazy 会话尚无原生历史；确认后只删除本地 Binding。"
        )
        confirm_body = "请再次确认：该本地会话映射将被永久删除。"
        callback_extra: dict[str, object] = {}
    else:
        builder.markdown(
            f"即将删除：`{_md_code(title)}`\n"
            "Codex 会处理该 Thread 当前的原生活动。确认后将永久删除原生 "
            "Codex Thread、其 spawned descendants 与本地 "
            "Binding；Codex App/CLI 中的对应历史也会消失。"
        )
        confirm_body = (
            "请再次确认：原生会话、派生会话与本地 Binding 都将永久删除，无法恢复。"
        )
        callback_extra = {
            "expected_native_thread_id": _native_thread_reference(
                native_thread_id
            )
        }
    builder.raw(
        _callback_button(
            label="永久删除当前会话",
            value=_envelope(
                scope,
                CardControlName.DELETE_BINDING,
                binding_id=_binding_reference(binding_id),
                **callback_extra,
            ),
            confirm=(
                "永久删除且无法恢复",
                confirm_body,
            ),
        )
    )
    return OutboundCard(card=builder.to_dict())


def sessions_delete_binding_card(
    *,
    scope: FeishuScope,
    binding_id: str,
    short_id: str,
    project_alias: str,
    title: str,
    native_thread_id: str | None,
    page: int,
) -> OutboundCard:
    builder = _builder(
        "永久删除会话",
        f"{short_id} · {project_alias}",
        template="red",
    )
    if native_thread_id is None:
        builder.markdown(
            f"即将删除：`{_md_code(title)}`\n"
            "该 Lazy 会话尚无原生历史；确认后只永久删除本地 Binding。"
        )
        confirm_body = "请再次确认：该本地会话映射将被永久删除。"
    else:
        builder.markdown(
            f"即将删除：`{_md_code(title)}`\n"
            "确认后将永久删除原生 Codex Thread、其 spawned descendants 与本地 "
            "Binding；Codex App/CLI 中的对应历史也会消失。"
        )
        confirm_body = (
            "请再次确认：原生会话、派生会话与本地 Binding 都将永久删除，无法恢复。"
        )
    expected_native_thread_id = (
        _native_thread_reference(native_thread_id)
        if native_thread_id is not None
        else None
    )
    builder.raw(
        _button_row(
            _callback_button(
                label="永久删除此会话",
                value=_envelope(
                    scope,
                    CardControlName.DELETE_EXACT_BINDING,
                    binding_id=_binding_reference(binding_id),
                    expected_native_thread_id=expected_native_thread_id,
                    page=page,
                ),
                style="danger",
                confirm=("永久删除且无法恢复", confirm_body),
            ),
            _repeatable_callback_button(
                label="返回会话列表",
                value=_sessions_page_envelope(scope=scope, target=page),
            ),
        )
    )
    return OutboundCard(card=builder.to_dict())


def archived_sessions_card(
    *,
    scope: FeishuScope,
    sessions: tuple[ArchivedSessionCardItem, ...],
    native_delete_available: bool,
    notice: str | None = None,
    notice_is_error: bool = False,
) -> OutboundCard:
    builder = _builder("已归档会话", "恢复后自动切换")
    if notice:
        builder.raw(_notice(notice, error=notice_is_error))
    builder.markdown(
        "已归档会话不会出现在普通 `/sessions` 中。恢复不会修改历史或会话配置；"
        "删除会永久移除原生 Thread、其 spawned descendants 与本地 Binding。"
    )
    if not sessions:
        builder.markdown("当前 Scope 没有已归档会话。")
        return OutboundCard(card=builder.to_dict())
    for session in sessions:
        builder.raw(
            _archived_session_row(
                scope=scope,
                session=session,
                native_delete_available=native_delete_available,
            )
        )
    return OutboundCard(card=builder.to_dict())


def archived_sessions_delete_binding_card(
    *,
    scope: FeishuScope,
    binding_id: str,
    short_id: str,
    project_alias: str,
    title: str,
    native_thread_id: str,
) -> OutboundCard:
    builder = _builder(
        "永久删除已归档会话",
        f"{short_id} · {project_alias}",
        template="red",
    )
    builder.markdown(
        f"即将删除：`{_md_code(title)}`\n"
        "确认后将永久删除原生 Codex Thread、其 spawned descendants 与本地 "
        "Binding；Codex App/CLI 中的对应历史也会消失。"
    )
    builder.raw(
        _button_row(
            _callback_button(
                label="永久删除已归档会话",
                value=_envelope(
                    scope,
                    CardControlName.DELETE_ARCHIVED_BINDING,
                    binding_id=_binding_reference(binding_id),
                    expected_native_thread_id=_native_thread_reference(
                        native_thread_id
                    ),
                ),
                style="danger",
                confirm=(
                    "永久删除且无法恢复",
                    "原生会话、派生会话与本地 Binding 都将永久删除。",
                ),
            ),
            _repeatable_callback_button(
                label="返回归档列表",
                value=_envelope(
                    scope,
                    CardControlName.REFRESH_ARCHIVED_SESSIONS,
                ),
            ),
        )
    )
    return OutboundCard(card=builder.to_dict())


def sessions_card(
    *,
    scope: FeishuScope,
    sessions: tuple[SessionCardItem, ...],
    native_delete_available: bool,
    page: int = 0,
    notice: str | None = None,
    notice_is_error: bool = False,
) -> OutboundCard:
    builder = _builder("会话", f"{len(sessions)} 个普通会话")
    if notice:
        builder.raw(_notice(notice, error=notice_is_error))
    builder.markdown(
        "切换不会停止其他会话正在运行的任务。"
        "归档与删除会直接委托 Codex 处理当前原生活动。归档保留历史，可从 "
        "`/sessions archived` 恢复；删除会级联移除派生会话，需进入红色确认卡。"
    )
    if not sessions:
        builder.markdown(
            "当前 Scope 没有普通会话；发送 `/sessions archived` 查看归档。"
        )
        return OutboundCard(card=builder.to_dict())

    ordered = sorted(sessions, key=lambda item: (not item.active,))
    total_pages = max(
        1,
        (len(ordered) + SESSIONS_PAGE_SIZE - 1) // SESSIONS_PAGE_SIZE,
    )
    clamped_page = max(0, min(page, total_pages - 1))
    start = clamped_page * SESSIONS_PAGE_SIZE
    end = start + SESSIONS_PAGE_SIZE
    visible = ordered[start:end]
    expected_active_binding_id = next(
        (session.binding_id for session in ordered if session.active),
        None,
    )

    for session in visible:
        builder.raw(
            _session_row(
                scope=scope,
                session=session,
                page=clamped_page,
                expected_active_binding_id=expected_active_binding_id,
                native_delete_available=native_delete_available,
            )
        )

    if total_pages > 1:
        builder.raw(
            _sessions_pagination(
                scope=scope,
                page=clamped_page,
                total_pages=total_pages,
            )
        )
    return OutboundCard(card=builder.to_dict())


def _session_row(
    *,
    scope: FeishuScope,
    session: SessionCardItem,
    page: int,
    expected_active_binding_id: str | None,
    native_delete_available: bool,
) -> dict[str, Any]:
    expected_turn_id = (
        _turn_reference(session.turn_id) if session.turn_id is not None else None
    )
    native = (
        session.native_thread_id[:8]
        if session.native_thread_id is not None
        else "pending"
    )
    marker_text = "● 当前" if session.active else "○"
    text = (
        f"{marker_text} {session.title}\n"
        f"会话：{session.short_id} · Project：{session.project_alias} · "
        f"Native：{native} · 状态：{session.state}"
    )
    controls: list[dict[str, Any]] = []
    if not session.active:
        controls.append(
            _repeatable_callback_button(
                label="设为当前",
                value=_envelope(
                    scope,
                    CardControlName.ACTIVATE_BINDING,
                    binding_id=_binding_reference(session.binding_id),
                ),
                style="primary_filled",
            )
        )
    if session.native_thread_id is not None:
        controls.append(
            _callback_button(
                label="归档",
                value=_envelope(
                    scope,
                    CardControlName.ARCHIVE_EXACT_BINDING,
                    binding_id=_binding_reference(session.binding_id),
                    page=page,
                ),
                confirm=(
                    "确认归档此会话？",
                    (
                        f"将归档会话 {session.short_id}（{session.title}）。"
                        "历史不会删除，之后可以恢复。"
                    ),
                ),
            )
        )
    if (
        (
            session.native_thread_id is None
            and session.state == SESSION_IDLE_STATE
        )
        or (
            session.native_thread_id is not None
            and native_delete_available
        )
    ):
        controls.append(
            _repeatable_callback_button(
                label="删除",
                value=_envelope(
                    scope,
                    CardControlName.PREPARE_EXACT_DELETE_BINDING,
                    binding_id=_binding_reference(session.binding_id),
                    expected_native_thread_id=(
                        _native_thread_reference(session.native_thread_id)
                        if session.native_thread_id is not None
                        else None
                    ),
                    page=page,
                ),
                style="danger",
            )
        )
    if session_stop_available(session.state):
        controls.append(
            _callback_button(
                label="停止",
                value=_envelope(
                    scope,
                    CardControlName.STOP_EXACT_BINDING,
                    binding_id=_binding_reference(session.binding_id),
                    expected_active_binding_id=(
                        _binding_reference(expected_active_binding_id)
                        if expected_active_binding_id is not None
                        else None
                    ),
                    expected_activity_revision=session.activity_revision,
                    expected_turn_id=expected_turn_id,
                    page=page,
                ),
            )
        )
    if (
        session.state == ActiveState.OBSERVATION_UNAVAILABLE.value
        and session.turn_id is not None
    ):
        controls.append(
            _repeatable_callback_button(
                label="重新检查",
                value=_envelope(
                    scope,
                    CardControlName.RECHECK_EXACT_TURN,
                    binding_id=_binding_reference(session.binding_id),
                    expected_active_binding_id=(
                        _binding_reference(expected_active_binding_id)
                        if expected_active_binding_id is not None
                        else None
                    ),
                    expected_activity_revision=session.activity_revision,
                    expected_turn_id=expected_turn_id,
                    page=page,
                ),
            )
        )
    columns = [
        {
            "tag": "column",
            "width": "weighted",
            "weight": 5,
            "padding": "8px",
            "elements": [_plain(text)],
        },
    ]
    if controls:
        columns.extend(
            {
                "tag": "column",
                "width": "auto",
                "vertical_align": "center",
                "padding": "8px",
                "elements": [control],
            }
            for control in controls
        )
    background = "blue-50" if session.active else "grey-50"
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "background_style": background,
        "margin": "0 0 8px 0",
        "columns": columns,
    }


def _sessions_pagination(
    *,
    scope: FeishuScope,
    page: int,
    total_pages: int,
) -> dict[str, Any]:
    columns: list[dict[str, Any]] = [
        {
            "tag": "column",
            "width": "weighted",
            "weight": 1,
            "vertical_align": "center",
            "elements": [
                {
                    "tag": "markdown",
                    "content": f"<font color='grey'>第 {page + 1}/{total_pages} 页</font>",
                }
            ],
        }
    ]
    if page > 0:
        columns.append(
            {
                "tag": "column",
                "width": "auto",
                "elements": [
                    _repeatable_callback_button(
                        label="上一页",
                        value=_sessions_page_envelope(
                            scope=scope,
                            target=page - 1,
                        ),
                    )
                ],
            }
        )
    if page + 1 < total_pages:
        columns.append(
            {
                "tag": "column",
                "width": "auto",
                "elements": [
                    _repeatable_callback_button(
                        label="下一页",
                        value=_sessions_page_envelope(
                            scope=scope,
                            target=page + 1,
                        ),
                    )
                ],
            }
        )
    return {"tag": "column_set", "flex_mode": "none", "columns": columns}


def _sessions_page_envelope(*, scope: FeishuScope, target: int) -> dict[str, Any]:
    return _envelope(
        scope,
        CardControlName.SESSIONS_PAGE,
        page=target,
    )


def binding_lifecycle_result_card(
    *,
    title: str,
    short_id: str,
    project_alias: str,
    message: str,
) -> OutboundCard:
    builder = _builder(
        title,
        f"{short_id} · {project_alias}",
        template="green",
    )
    builder.markdown(message)
    return OutboundCard(card=builder.to_dict())


def goal_card(
    *,
    scope: FeishuScope,
    binding_id: str,
    short_id: str,
    project_alias: str,
    goal: GoalSnapshot | None,
    runtime_state: str | None = None,
    notice: str | None = None,
    notice_is_error: bool = False,
    goal_generation: str | None = None,
) -> OutboundCard:
    generation = (
        goal_generation
        if goal_generation is not None
        else (None if goal is None else _goal_generation(goal))
    )
    return reply_card(
        ReplyCardProjection(
            scope=scope,
            goal=ReplyCardGoalModule(
                binding_id=binding_id,
                short_id=short_id,
                project_alias=project_alias,
                goal_generation=generation,
                status=None if goal is None else goal.status.value,
                runtime_state=runtime_state,
                objective=None if goal is None else goal.objective,
                token_budget=None if goal is None else goal.token_budget,
                tokens_used=0 if goal is None else goal.tokens_used,
                notice=notice,
                notice_is_error=notice_is_error,
            ),
        )
    )


def side_topic_card(
    *,
    scope: FeishuScope,
    side_id: str,
    parent_short_id: str,
    creator_id: str,
    created_at: str,
    state: SideTopicState,
    notice: str | None = None,
    notice_is_error: bool = False,
) -> OutboundCard:
    labels = {
        SideTopicState.CREATING: "创建中",
        SideTopicState.OPEN: "可用",
        SideTopicState.CLOSED: "已结束",
        SideTopicState.EXPIRED: "已过期",
        SideTopicState.FAILED: "创建或清理失败",
    }
    templates = {
        SideTopicState.CREATING: "blue",
        SideTopicState.OPEN: "green",
        SideTopicState.CLOSED: "grey",
        SideTopicState.EXPIRED: "grey",
        SideTopicState.FAILED: "red",
    }
    builder = _builder(
        "Codex Side",
        f"{labels[state]} · Parent {parent_short_id}",
        template=templates[state],
    )
    if notice:
        builder.raw(_notice(notice, error=notice_is_error))
    builder.markdown(
        f"**状态**：`{state.value}`\n"
        f"**Parent 会话**：`{_md_code(parent_short_id)}`\n"
        f"**创建者**：`{_md_code(creator_id)}`\n"
        f"**创建时间**：`{_md_code(created_at)}`\n\n"
        "Side 使用独立的 ephemeral Codex Thread，并与 Parent 共享 Project cwd；"
        "文件改动彼此可见。服务重启后本 Side 会过期，历史消息仅保留在飞书中。"
    )
    if state is SideTopicState.OPEN and scope.kind is not ScopeKind.TOPIC:
        raise ValueError("an open Side card requires its exact Topic scope")
    if (
        state in {SideTopicState.CREATING, SideTopicState.OPEN}
        and scope.kind is ScopeKind.TOPIC
    ):
        creating = state is SideTopicState.CREATING
        builder.raw(
            _button_row(
                _repeatable_callback_button(
                    label="取消 Side" if creating else "结束 Side",
                    value=_envelope(
                        scope,
                        CardControlName.SIDE_CLOSE,
                        side_id=_side_reference(side_id),
                    ),
                    style="danger",
                    confirm=(
                        "取消 Side" if creating else "结束 Side",
                        (
                            "将重试清理未完成的 Side，并取消原生订阅。"
                            if creating
                            else "将中断当前 Side Turn、清理后台终端并取消原生订阅。"
                        ),
                    ),
                )
            )
        )
    return OutboundCard(card=builder.to_dict())


def binding_created_card(
    *,
    short_id: str,
    project_alias: str,
    settings: TurnModelSettings | None = None,
    message_context_mode: MentionContextMode = MentionContextMode.CURRENT_ONLY,
    task_feedback: BindingTaskFeedback | None = None,
) -> OutboundCard:
    builder = _builder(
        "Project 选择成功",
        f"{project_alias} · {short_id}",
        template="green",
    )
    builder.markdown(
        f"✅ 已选择 Project `{_md_code(project_alias)}`，"
        f"并创建、切换到会话 **{short_id}**。"
    )
    builder.markdown(
        "现在可以直接发送任务；首条普通消息将创建原生 Codex Thread。"
    )
    builder.markdown(_model_source_summary(settings))
    builder.markdown(_context_mode_summary(message_context_mode))
    builder.markdown(
        _task_feedback_summary(task_feedback or BindingTaskFeedback())
    )
    return OutboundCard(card=builder.to_dict())


def binding_configured_card(
    *,
    short_id: str,
    project_alias: str,
    settings: TurnModelSettings | None,
    message_context_mode: MentionContextMode = MentionContextMode.CURRENT_ONLY,
    task_feedback: BindingTaskFeedback | None = None,
) -> OutboundCard:
    builder = _builder(
        "会话配置已保存",
        f"{project_alias} · {short_id}",
        template="green",
    )
    builder.markdown(_model_source_summary(settings))
    builder.markdown(_context_mode_summary(message_context_mode))
    builder.markdown(
        _task_feedback_summary(task_feedback or BindingTaskFeedback())
    )
    return OutboundCard(card=builder.to_dict())


def _model_source_summary(settings: TurnModelSettings | None) -> str:
    if settings is None:
        return "Model 来源：继承 Codex（不发送 Model / Effort / Speed override）。"
    return (
        "Model 来源：Netizen 会话显式配置。后续新 Turn 将使用："
        f"Model=`{_md_code(settings.model)}` · "
        f"Effort=`{_md_code(settings.effort_id)}` · "
        f"Speed=`{_md_code(settings.service_tier_name)}`"
    )


def _context_mode_summary(mode: MentionContextMode) -> str:
    if mode is MentionContextMode.CATCH_UP:
        return (
            "@ 时读取的消息范围：自动带上期间的群聊讨论。"
            "两次 @ 之间群里未 @ 机器人的成员消息，"
            "也会在下一次 @ 时作为背景交给 Codex。"
        )
    return "@ 时读取的消息范围：仅这条 @ 消息。"


def _task_feedback_summary(feedback: BindingTaskFeedback) -> str:
    pulse = "开启" if feedback.reaction_pulse_enabled else "关闭"
    progress = "开启" if feedback.progress_card_enabled else "关闭"
    return f"执行中表情闪烁：{pulse}\n进度卡：{progress}"


def error_card(message: str, *, scope: FeishuScope | None = None) -> OutboundCard:
    builder = _builder("操作失败", "请修正后重试", template="red")
    builder.raw(_notice(message, error=True))
    if scope is not None:
        builder.raw(
            _repeatable_callback_button(
                label="刷新设置",
                value=_envelope(
                    scope,
                    CardControlName.REFRESH_SETTINGS,
                    settings_section=SettingsSection.PROJECTS.value,
                ),
                style="primary_filled",
            )
        )
    return OutboundCard(card=builder.to_dict())


def decode_button_action(
    *,
    app_id: str,
    message_id: str,
    callback_chat_id: str,
    sender_id: str,
    tag: str,
    value: Any,
) -> CardControlIntent:
    if tag != "button":
        raise CardActionError(f"不支持的卡片组件：{tag or 'unknown'}")
    if not message_id or not callback_chat_id or not sender_id:
        raise CardActionError("卡片回调缺少消息、聊天或操作者标识。")
    if not isinstance(value, Mapping):
        raise CardActionError("卡片动作 value 必须是对象。")
    payload = dict(value)
    payload.pop("nonce", None)
    raw_intent = payload.get("intent")
    try:
        name = CardControlName(raw_intent)
    except (TypeError, ValueError) as error:
        raise CardActionError("未知卡片动作。") from error
    if payload.get("v") != ACTION_VERSION or isinstance(payload.get("v"), bool):
        raise CardActionError("卡片版本已过期，请重新发送原命令打开新卡片。")
    form_only = {
        CardControlName.REGISTER_PROJECT,
        CardControlName.CREATE_BINDING,
        CardControlName.CONFIGURE_BINDING,
        CardControlName.RENAME_BINDING,
    }
    if name in form_only:
        raise CardActionError("该操作只能通过对应表单提交。")
    common = {"v", "intent", "chat_id", "scope_kind"}
    extra_by_name = {
        CardControlName.OPEN_SETTINGS_SECTION: {"settings_section"},
        CardControlName.REFRESH_SETTINGS: {"settings_section"},
        CardControlName.SET_PROJECT_ENABLED: {
            "project_alias",
            "enabled",
            "expected_revision",
        },
        CardControlName.ARCHIVE_BINDING: {"binding_id"},
        CardControlName.ARCHIVE_EXACT_BINDING: {
            "binding_id",
            "page",
        },
        CardControlName.DELETE_BINDING: {
            "binding_id",
        },
        CardControlName.PREPARE_EXACT_DELETE_BINDING: {
            "binding_id",
            "expected_native_thread_id",
            "page",
        },
        CardControlName.DELETE_EXACT_BINDING: {
            "binding_id",
            "expected_native_thread_id",
            "page",
        },
        CardControlName.PREPARE_ARCHIVED_DELETE_BINDING: {
            "binding_id",
            "expected_native_thread_id",
        },
        CardControlName.DELETE_ARCHIVED_BINDING: {
            "binding_id",
            "expected_native_thread_id",
        },
        CardControlName.UNARCHIVE_BINDING: {"binding_id"},
        CardControlName.ACTIVATE_BINDING: {"binding_id"},
        CardControlName.STOP_EXACT_BINDING: {
            "binding_id",
            "expected_active_binding_id",
            "expected_activity_revision",
            "expected_turn_id",
            "page",
        },
        CardControlName.RECHECK_EXACT_TURN: {
            "binding_id",
            "expected_active_binding_id",
            "expected_activity_revision",
            "expected_turn_id",
            "page",
        },
        CardControlName.SESSIONS_PAGE: {"page"},
        CardControlName.REFRESH_ARCHIVED_SESSIONS: set(),
        CardControlName.GOAL_PAUSE: {
            "binding_id",
            "goal_generation",
            "expected_goal_status",
        },
        CardControlName.GOAL_RESUME: {
            "binding_id",
            "goal_generation",
            "expected_goal_status",
        },
        CardControlName.GOAL_CLEAR: {
            "binding_id",
            "goal_generation",
            "expected_goal_status",
        },
        CardControlName.SIDE_CLOSE: {"side_id"},
    }
    try:
        scope_kind = ScopeKind(payload.get("scope_kind"))
    except (TypeError, ValueError) as error:
        raise CardActionError("未知 Scope kind。") from error
    scope_fields = {"topic_id"} if scope_kind is ScopeKind.TOPIC else set()
    expected = common | scope_fields | extra_by_name[name]
    if (
        name is CardControlName.DELETE_BINDING
        and "expected_native_thread_id" in payload
    ):
        expected.add("expected_native_thread_id")
    if set(payload) != expected:
        raise CardActionError("卡片动作字段不完整或包含未知字段。")
    if payload["chat_id"] != callback_chat_id:
        raise CardActionError("卡片 Scope 与当前聊天不一致。")
    scope = _scope_from_envelope(
        app_id=app_id,
        chat_id=callback_chat_id,
        kind=scope_kind,
        topic_id=payload.get("topic_id"),
    )

    alias = None
    binding_id = None
    expected_active_binding_id = None
    expected_native_thread_id = None
    side_id = None
    revision = None
    enabled = None
    section = None
    page = None
    expected_activity_revision = None
    expected_turn_id = None
    goal_generation_value = None
    expected_goal_status = None
    if "settings_section" in payload:
        try:
            section = SettingsSection(payload["settings_section"])
        except (TypeError, ValueError) as error:
            raise CardActionError("未知 Settings 分区。") from error
    if "project_alias" in payload:
        alias = _required_string(payload["project_alias"], "project_alias")
    if "binding_id" in payload:
        binding_id = _decode_binding_reference(
            _required_string(payload["binding_id"], "binding_id")
        )
    if "expected_active_binding_id" in payload:
        raw_active_binding_id = payload["expected_active_binding_id"]
        if raw_active_binding_id is not None:
            expected_active_binding_id = _decode_binding_reference(
                _required_string(
                    raw_active_binding_id,
                    "expected_active_binding_id",
                )
            )
    if "expected_native_thread_id" in payload:
        raw_native_thread_id = payload["expected_native_thread_id"]
        if raw_native_thread_id is not None:
            expected_native_thread_id = _decode_native_thread_reference(
                _required_string(
                    raw_native_thread_id,
                    "expected_native_thread_id",
                )
            )
    if "expected_activity_revision" in payload:
        raw_activity_revision = payload["expected_activity_revision"]
        if (
            isinstance(raw_activity_revision, bool)
            or not isinstance(raw_activity_revision, int)
            or raw_activity_revision < 0
        ):
            raise CardActionError(
                "expected_activity_revision 必须是非负整数。"
            )
        expected_activity_revision = raw_activity_revision
    if "expected_turn_id" in payload:
        raw_turn_id = payload["expected_turn_id"]
        if raw_turn_id is not None:
            expected_turn_id = _decode_turn_reference(
                _required_string(raw_turn_id, "expected_turn_id")
            )
    if "side_id" in payload:
        side_id = _decode_side_reference(
            _required_string(payload["side_id"], "side_id")
        )
    if "expected_revision" in payload:
        revision = payload["expected_revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise CardActionError("expected_revision 必须是正整数。")
    if "enabled" in payload:
        enabled = payload["enabled"]
        if not isinstance(enabled, bool):
            raise CardActionError("enabled 必须是布尔值。")
    if "page" in payload:
        raw_page = payload["page"]
        if (
            isinstance(raw_page, bool)
            or not isinstance(raw_page, int)
            or raw_page < 0
        ):
            raise CardActionError("页码必须是非负整数。")
        page = raw_page
    if "goal_generation" in payload:
        goal_generation_value = _decode_goal_generation(
            payload["goal_generation"]
        )
    if "expected_goal_status" in payload:
        expected_goal_status = _required_string(
            payload["expected_goal_status"],
            "expected_goal_status",
        )
        valid_goal_statuses = {item.value for item in GoalStatus}
        if expected_goal_status not in valid_goal_statuses:
            raise CardActionError("Goal 预期状态无效。")
        expected_by_action = {
            CardControlName.GOAL_PAUSE: {GoalStatus.ACTIVE.value},
            CardControlName.GOAL_RESUME: {GoalStatus.PAUSED.value},
            CardControlName.GOAL_CLEAR: valid_goal_statuses
            - {GoalStatus.ACTIVE.value},
        }
        if expected_goal_status not in expected_by_action[name]:
            raise CardActionError("Goal 动作与预期状态不一致。")
    if name is CardControlName.SET_PROJECT_ENABLED:
        section = SettingsSection.PROJECTS
    if name is CardControlName.SIDE_CLOSE and scope.kind is not ScopeKind.TOPIC:
        raise CardActionError("Side 结束动作必须来自原 Side 话题。")
    if (
        name is CardControlName.RECHECK_EXACT_TURN
        and expected_turn_id is None
    ):
        raise CardActionError("Turn 重新检查动作缺少 exact Turn。")
    return CardControlIntent(
        scope=scope,
        source_id=message_id,
        sender_id=sender_id,
        name=name,
        settings_section=section,
        project_alias=alias,
        expected_revision=revision,
        enabled=enabled,
        binding_id=binding_id,
        expected_active_binding_id=expected_active_binding_id,
        expected_native_thread_id=expected_native_thread_id,
        expected_activity_revision=expected_activity_revision,
        expected_turn_id=expected_turn_id,
        side_id=side_id,
        page=page,
        goal_generation=goal_generation_value,
        expected_goal_status=expected_goal_status,
    )


def decode_card_form(
    *,
    scope: FeishuScope,
    message_id: str,
    sender_id: str,
    tag: str,
    form_value: Any,
) -> CardControlIntent:
    if not isinstance(form_value, Mapping):
        raise CardActionError("卡片表单值必须是对象。")
    fields = set(form_value)
    has_new = any(field.startswith("new_") for field in fields)
    has_config = any(field.startswith("config_") for field in fields)
    has_rename = any(field.startswith("rename_name_v1__") for field in fields)
    if sum((has_new, has_config, has_rename)) > 1:
        raise CardActionError("卡片表单混合了不同操作的字段。")
    if has_new:
        return _decode_new_binding_form(
            scope=scope,
            message_id=message_id,
            sender_id=sender_id,
            tag=tag,
            form_value=form_value,
        )
    if has_config:
        return _decode_config_form(
            scope=scope,
            message_id=message_id,
            sender_id=sender_id,
            tag=tag,
            form_value=form_value,
        )
    if has_rename:
        return _decode_rename_form(
            scope=scope,
            message_id=message_id,
            sender_id=sender_id,
            tag=tag,
            form_value=form_value,
        )
    return decode_settings_form(
        scope=scope,
        message_id=message_id,
        sender_id=sender_id,
        tag=tag,
        form_value=form_value,
    )


def _decode_new_binding_form(
    *,
    scope: FeishuScope,
    message_id: str,
    sender_id: str,
    tag: str,
    form_value: Mapping[str, Any],
) -> CardControlIntent:
    if tag != "button" or not message_id or not sender_id:
        raise CardActionError("会话配置表单回调不完整。")
    payload = dict(form_value)
    base_fields = {
        "new_project",
        "new_model",
        "new_task_reactions",
        "new_progress_card",
    }
    context_fields = (
        {"new_context_mode"} if "new_context_mode" in payload else set()
    )
    catalog_fields = {"new_effort", "new_speed"}
    if frozenset(payload) not in {
        frozenset(base_fields | context_fields),
        frozenset(base_fields | context_fields | catalog_fields),
    }:
        raise CardActionError("会话配置表单字段不完整或包含未知字段。")

    project_alias, expected_revision = _decode_project_reference(
        _required_string(payload["new_project"], "new_project")
    )
    model_id = _decode_new_model_reference(
        _required_string(payload["new_model"], "new_model")
    )
    message_context_mode = _decode_context_mode_reference(
        _required_string(
            payload.get(
                "new_context_mode",
                _context_mode_reference(MentionContextMode.CURRENT_ONLY),
            ),
            "new_context_mode",
        )
    )
    reaction_pulse_enabled = _decode_task_feedback_reference(
        payload["new_task_reactions"],
        "new_task_reactions",
    )
    progress_card_enabled = _decode_task_feedback_reference(
        payload["new_progress_card"],
        "new_progress_card",
    )
    effort_id = None
    service_tier_id = None
    has_catalog_fields = catalog_fields.issubset(payload)
    if model_id is None:
        if has_catalog_fields:
            # A catalog-backed card renders these fields even when inherit is
            # selected. Validate their bounds, then deliberately discard them.
            _bounded_string(
                payload["new_effort"],
                "new_effort",
                max_chars=MAX_SETTING_ID_CHARS,
            )
            _bounded_string(
                payload["new_speed"],
                "new_speed",
                max_chars=MAX_SETTING_ID_CHARS,
            )
    else:
        if not has_catalog_fields:
            raise CardActionError(
                "显式 Model 必须同时选择 Effort 与 Speed，请重新打开卡片。"
            )
        effort_id = _bounded_string(
            payload["new_effort"],
            "new_effort",
            max_chars=MAX_SETTING_ID_CHARS,
        )
        service_tier_id = _bounded_string(
            payload["new_speed"],
            "new_speed",
            max_chars=MAX_SETTING_ID_CHARS,
        )
    return CardControlIntent(
        scope=scope,
        source_id=message_id,
        sender_id=sender_id,
        name=CardControlName.CREATE_BINDING,
        project_alias=project_alias,
        expected_revision=expected_revision,
        model_id=model_id,
        effort_id=effort_id,
        service_tier_id=service_tier_id,
        message_context_mode=message_context_mode,
        reaction_pulse_enabled=reaction_pulse_enabled,
        progress_card_enabled=progress_card_enabled,
    )


def _decode_config_form(
    *,
    scope: FeishuScope,
    message_id: str,
    sender_id: str,
    tag: str,
    form_value: Mapping[str, Any],
) -> CardControlIntent:
    if tag != "button" or not message_id or not sender_id:
        raise CardActionError("会话配置表单回调不完整。")
    payload = dict(form_value)
    base_fields = {
        "config_model",
        "config_task_reactions",
        "config_progress_card",
    }
    context_fields = (
        {"config_context_mode"} if "config_context_mode" in payload else set()
    )
    catalog_fields = {"config_effort", "config_speed"}
    if frozenset(payload) not in {
        frozenset(base_fields | context_fields),
        frozenset(base_fields | context_fields | catalog_fields),
    }:
        raise CardActionError("会话配置表单字段不完整或包含未知字段。")
    (
        binding_id,
        expected_settings_revision,
        expected_context_revision,
        feedback_revision,
        model_id,
    ) = _decode_config_model_reference(
        _required_string(payload["config_model"], "config_model")
    )
    message_context_mode = _decode_context_mode_reference(
        _required_string(
            payload.get(
                "config_context_mode",
                _context_mode_reference(MentionContextMode.CURRENT_ONLY),
            ),
            "config_context_mode",
        )
    )
    reaction_pulse_enabled = _decode_task_feedback_reference(
        payload["config_task_reactions"],
        "config_task_reactions",
    )
    progress_card_enabled = _decode_task_feedback_reference(
        payload["config_progress_card"],
        "config_progress_card",
    )
    effort_id = None
    service_tier_id = None
    has_catalog_fields = catalog_fields.issubset(payload)
    if model_id is None:
        if has_catalog_fields:
            _bounded_string(
                payload["config_effort"],
                "config_effort",
                max_chars=MAX_SETTING_ID_CHARS,
            )
            _bounded_string(
                payload["config_speed"],
                "config_speed",
                max_chars=MAX_SETTING_ID_CHARS,
            )
    else:
        if not has_catalog_fields:
            raise CardActionError(
                "显式 Model 必须同时选择 Effort 与 Speed，请重新打开卡片。"
            )
        effort_id = _bounded_string(
            payload["config_effort"],
            "config_effort",
            max_chars=MAX_SETTING_ID_CHARS,
        )
        service_tier_id = _bounded_string(
            payload["config_speed"],
            "config_speed",
            max_chars=MAX_SETTING_ID_CHARS,
        )
    return CardControlIntent(
        scope=scope,
        source_id=message_id,
        sender_id=sender_id,
        name=CardControlName.CONFIGURE_BINDING,
        expected_settings_revision=expected_settings_revision,
        expected_context_revision=expected_context_revision,
        feedback_revision=feedback_revision,
        binding_id=binding_id,
        model_id=model_id,
        effort_id=effort_id,
        service_tier_id=service_tier_id,
        message_context_mode=message_context_mode,
        reaction_pulse_enabled=reaction_pulse_enabled,
        progress_card_enabled=progress_card_enabled,
    )


def _decode_rename_form(
    *,
    scope: FeishuScope,
    message_id: str,
    sender_id: str,
    tag: str,
    form_value: Mapping[str, Any],
) -> CardControlIntent:
    if tag != "button" or not message_id or not sender_id:
        raise CardActionError("会话重命名表单回调不完整。")
    payload = dict(form_value)
    if len(payload) != 1:
        raise CardActionError("会话重命名表单字段不完整或包含未知字段。")
    field, raw_name = next(iter(payload.items()))
    match = _RENAME_NAME_FIELD.fullmatch(field)
    if match is None:
        raise CardActionError("会话重命名卡片已过期，请重新发送 /rename。")
    name = " ".join(_required_string(raw_name, "thread_name").split())
    if not name:
        raise CardActionError("会话名称不能为空。")
    if len(name) > MAX_THREAD_NAME_CHARS:
        raise CardActionError(
            f"会话名称不能超过 {MAX_THREAD_NAME_CHARS} 个字符。"
        )
    return CardControlIntent(
        scope=scope,
        source_id=message_id,
        sender_id=sender_id,
        name=CardControlName.RENAME_BINDING,
        binding_id=match.group(1),
        thread_name=name,
    )


def decode_settings_form(
    *,
    scope: FeishuScope,
    message_id: str,
    sender_id: str,
    tag: str,
    form_value: Any,
) -> CardControlIntent:
    try:
        return _decode_settings_form(
            scope=scope,
            message_id=message_id,
            sender_id=sender_id,
            tag=tag,
            form_value=form_value,
        )
    except SettingsCardActionError:
        raise
    except CardActionError as error:
        raise SettingsCardActionError(
            str(error),
            scope=scope,
            section=SettingsSection.PROJECTS,
        ) from error


def _decode_settings_form(
    *,
    scope: FeishuScope,
    message_id: str,
    sender_id: str,
    tag: str,
    form_value: Any,
) -> CardControlIntent:
    if tag != "button" or not message_id or not sender_id:
        raise CardActionError("Settings 表单回调不完整。")
    if not isinstance(form_value, Mapping):
        raise CardActionError("Settings 表单值必须是对象。")
    payload = dict(form_value)
    if set(payload) == {"project_manage_target", "project_manage_operation"}:
        return _decode_project_management_form(
            scope=scope,
            message_id=message_id,
            sender_id=sender_id,
            payload=payload,
        )

    required = {"project_alias", "project_mode"}
    allowed = required | {"project_path"}
    if not required.issubset(payload) or not set(payload).issubset(allowed):
        raise CardActionError("未知、字段不完整或包含额外字段的 Settings 表单。")
    alias = _required_string(payload["project_alias"], "project_alias").strip()
    mode = _decode_project_mode_reference(
        _required_string(payload["project_mode"], "project_mode")
    )
    raw_path = payload.get("project_path", "")
    if not isinstance(raw_path, str):
        raise CardActionError("project_path 必须是字符串。")
    path = raw_path.strip() or None
    if mode == "existing" and path is None:
        raise CardActionError("登记已有目录时必须填写绝对路径。")
    return CardControlIntent(
        scope=scope,
        source_id=message_id,
        sender_id=sender_id,
        name=CardControlName.REGISTER_PROJECT,
        settings_section=SettingsSection.PROJECTS,
        project_alias=alias,
        project_path=path,
        create_directory=mode == "create",
    )


def _decode_project_management_form(
    *,
    scope: FeishuScope,
    message_id: str,
    sender_id: str,
    payload: Mapping[str, Any],
) -> CardControlIntent:
    alias, revision = _decode_project_reference(
        _required_string(payload["project_manage_target"], "project_manage_target")
    )
    operation = _required_string(
        payload["project_manage_operation"],
        "project_manage_operation",
    )
    if operation == "enable":
        enabled = True
    elif operation == "disable":
        enabled = False
    else:
        raise CardActionError("未知 Project 操作。")
    return CardControlIntent(
        scope=scope,
        source_id=message_id,
        sender_id=sender_id,
        name=CardControlName.SET_PROJECT_ENABLED,
        settings_section=SettingsSection.PROJECTS,
        project_alias=alias,
        expected_revision=revision,
        enabled=enabled,
    )


def scope_from_fetched_card(
    *,
    app_id: str,
    callback_chat_id: str,
    fetched_message: Any,
    chat_type: str | None,
) -> FeishuScope:
    item = _fetched_card_item(
        callback_chat_id=callback_chat_id,
        fetched_message=fetched_message,
    )
    raw_topic_id = item.get("thread_id")
    if isinstance(raw_topic_id, str) and raw_topic_id:
        return FeishuScope(
            app_id,
            callback_chat_id,
            ScopeKind.TOPIC,
            raw_topic_id,
        )
    if chat_type == "p2p":
        kind = ScopeKind.DIRECT
    elif chat_type == "group":
        kind = ScopeKind.GROUP
    else:
        raise CardActionError("无法判断卡片来自单聊还是群聊。")
    return FeishuScope(app_id, callback_chat_id, kind)


def fetched_card_topic_id(
    *,
    callback_chat_id: str,
    fetched_message: Any,
) -> str | None:
    """Return the public thread id without requiring an unrelated chat lookup."""
    item = _fetched_card_item(
        callback_chat_id=callback_chat_id,
        fetched_message=fetched_message,
    )
    raw_topic_id = item.get("thread_id")
    return raw_topic_id if isinstance(raw_topic_id, str) and raw_topic_id else None


def _fetched_card_item(
    *,
    callback_chat_id: str,
    fetched_message: Any,
) -> Mapping[str, Any]:
    if not isinstance(fetched_message, Mapping):
        raise CardActionError("无法读取卡片原消息。")
    data = fetched_message.get("data")
    if not isinstance(data, Mapping):
        raise CardActionError("卡片原消息缺少 data。")
    items = data.get("items")
    if not isinstance(items, list) or not items or not isinstance(items[0], Mapping):
        raise CardActionError("卡片原消息缺少 items。")
    item = items[0]
    if item.get("chat_id") != callback_chat_id:
        raise CardActionError("卡片原消息与回调聊天不一致。")
    return item


def _scope_from_envelope(
    *,
    app_id: str,
    chat_id: str,
    kind: Any,
    topic_id: Any,
) -> FeishuScope:
    try:
        scope_kind = ScopeKind(kind)
    except (TypeError, ValueError) as error:
        raise CardActionError("未知 Scope kind。") from error
    if scope_kind is ScopeKind.TOPIC:
        topic = _required_string(topic_id, "topic_id")
    else:
        if topic_id is not None:
            raise CardActionError("非话题 Scope 不能携带 topic_id。")
        topic = None
    return FeishuScope(app_id, chat_id, scope_kind, topic)


def _envelope(
    scope: FeishuScope,
    name: CardControlName,
    **extra: Any,
) -> dict[str, Any]:
    value = {
        "v": ACTION_VERSION,
        "intent": name.value,
        "chat_id": scope.chat_id,
        "scope_kind": scope.kind.value,
        **extra,
    }
    if scope.kind is ScopeKind.TOPIC:
        value["topic_id"] = scope.topic_id
    return value


def _project_reference(project: Project) -> str:
    return f"project:v1:{project.alias}:{project.revision}"


def _decode_project_reference(value: str) -> tuple[str, int]:
    match = _PROJECT_REFERENCE.fullmatch(value)
    if match is None:
        raise CardActionError("Project 选择值无效，请刷新卡片后重试。")
    return match.group(1), int(match.group(2))


def _project_mode_reference(mode: str, callback_nonce: str) -> str:
    value = f"project-mode:v2:{mode}:{callback_nonce}"
    if _PROJECT_MODE_REFERENCE.fullmatch(value) is None:
        raise ValueError("invalid Project mode callback reference")
    return value


def _decode_project_mode_reference(value: str) -> str:
    if value in {"create", "existing"}:
        return value
    prefix = "project-mode:v2:"
    if value.startswith(prefix):
        mode = value[len(prefix) :].split(":", 1)[0]
        if mode in {"create", "existing"}:
            return mode
    raise CardActionError("未知 Project 创建模式。")


def _binding_reference(binding_id: str) -> str:
    return f"binding:v1:{binding_id}"


def _decode_binding_reference(value: str) -> str:
    match = _BINDING_REFERENCE.fullmatch(value)
    if match is None:
        raise CardActionError("会话引用无效，请重新打开原卡片。")
    return match.group(1)


def _native_thread_reference(thread_id: str) -> str:
    value = f"native-thread:v1:{thread_id}"
    if _NATIVE_THREAD_REFERENCE.fullmatch(value) is None:
        raise ValueError("invalid native Thread reference")
    return value


def _decode_native_thread_reference(value: str) -> str:
    match = _NATIVE_THREAD_REFERENCE.fullmatch(value)
    if match is None:
        raise CardActionError("原生会话引用无效，请重新发送 /delete。")
    return match.group(1)


def _side_reference(side_id: str) -> str:
    value = f"side:v1:{side_id}"
    if _SIDE_REFERENCE.fullmatch(value) is None:
        raise ValueError("invalid Side reference")
    return value


def _decode_side_reference(value: str) -> str:
    match = _SIDE_REFERENCE.fullmatch(value)
    if match is None:
        raise CardActionError("Side 引用无效，请回到原 Side 话题重试。")
    return match.group(1)


def _turn_reference(turn_id: str) -> str:
    value = f"turn:v1:{turn_id}"
    if _TURN_REFERENCE.fullmatch(value) is None:
        raise ValueError("invalid native Turn reference")
    return value


def _decode_turn_reference(value: str) -> str:
    match = _TURN_REFERENCE.fullmatch(value)
    if match is None:
        raise CardActionError("原生 Turn 引用无效，请重新执行任务。")
    return match.group(1)


def _rename_name_field(binding_id: str) -> str:
    if _BINDING_REFERENCE.fullmatch(_binding_reference(binding_id)) is None:
        raise ValueError("invalid Binding rename reference")
    return f"rename_name_v1__{binding_id}"


def _new_model_reference(model_id: str | None) -> str:
    choice = _encode_model_choice(model_id)
    value = f"new-model:v1:{choice}"
    if _NEW_MODEL_REFERENCE.fullmatch(value) is None:
        raise ValueError("invalid new Binding model reference")
    return value


def _decode_new_model_reference(value: str) -> str | None:
    match = _NEW_MODEL_REFERENCE.fullmatch(value)
    if match is None:
        raise CardActionError("新建会话卡片已过期，请重新发送 /new。")
    return _decode_model_choice(match.group(1), match.group(2), command="/new")


def _config_model_reference(
    *,
    binding_id: str,
    settings_revision: int,
    context_revision: int,
    feedback_revision: int,
    model_id: str | None,
) -> str:
    choice = _encode_model_choice(model_id)
    value = (
        f"config-model:v4:{binding_id}:{settings_revision}:"
        f"{context_revision}:{feedback_revision}:{choice}"
    )
    if _CONFIG_MODEL_REFERENCE.fullmatch(value) is None:
        raise ValueError("invalid Binding model settings reference")
    return value


def _decode_config_model_reference(
    value: str,
) -> tuple[str, int, int, int, str | None]:
    match = _CONFIG_MODEL_REFERENCE.fullmatch(value)
    if match is None:
        raise CardActionError("会话配置卡片已过期，请重新发送 /config。")
    model_id = _decode_model_choice(
        match.group(5),
        match.group(6),
        command="/config",
    )
    return (
        match.group(1),
        int(match.group(2)),
        int(match.group(3)),
        int(match.group(4)),
        model_id,
    )


def _encode_model_choice(model_id: str | None) -> str:
    if model_id is None:
        return _INHERIT_MODEL_CHOICE
    if (
        not isinstance(model_id, str)
        or not model_id
        or len(model_id) > MAX_MODEL_ID_CHARS
        or "\x00" in model_id
    ):
        raise ValueError("invalid model ID")
    encoded_model = base64.urlsafe_b64encode(model_id.encode("utf-8")).decode(
        "ascii"
    ).rstrip("=")
    return f"explicit:{encoded_model}"


def _decode_model_choice(
    choice: str,
    encoded_model: str | None,
    *,
    command: str,
) -> str | None:
    if choice == _INHERIT_MODEL_CHOICE:
        if encoded_model is not None:
            raise CardActionError(
                f"会话配置卡片已过期，请重新发送 {command}。"
            )
        return None
    if encoded_model is None:
        raise CardActionError(
            f"会话配置卡片已过期，请重新发送 {command}。"
        )
    if len(encoded_model) > MAX_ENCODED_MODEL_ID_CHARS:
        raise CardActionError(
            f"会话配置卡片已过期，请重新发送 {command}。"
        )
    padding = "=" * (-len(encoded_model) % 4)
    try:
        model_id = base64.b64decode(
            encoded_model + padding,
            altchars=b"-_",
            validate=True,
        ).decode("utf-8")
    except (UnicodeDecodeError, ValueError) as error:
        raise CardActionError(
            f"会话配置卡片已过期，请重新发送 {command}。"
        ) from error
    if (
        not model_id
        or len(model_id) > MAX_MODEL_ID_CHARS
        or "\x00" in model_id
    ):
        raise CardActionError(
            f"会话配置卡片已过期，请重新发送 {command}。"
        )
    return model_id


def _context_mode_reference(mode: MentionContextMode) -> str:
    return f"context-mode:v1:{mode.value}"


def _decode_context_mode_reference(value: str) -> MentionContextMode:
    match = _CONTEXT_MODE_REFERENCE.fullmatch(value)
    if match is None:
        raise CardActionError("消息范围选择已过期，请重新打开卡片。")
    return MentionContextMode(match.group(1))


def _task_feedback_reference(enabled: bool) -> str:
    return f"task-feedback:v2:{'on' if enabled else 'off'}"


def _decode_task_feedback_reference(value: Any, field: str) -> bool:
    match = _TASK_FEEDBACK_REFERENCE.fullmatch(_required_string(value, field))
    if match is None:
        raise CardActionError("任务反馈选择已过期，请重新打开卡片。")
    return match.group(1) == "on"


def _builder(title: str, subtitle: str, *, template: str = "blue"):
    return (
        new_card()
        .config(update_multi=True, width_mode="default")
        .header(title, subtitle=subtitle, template=template)
    )


def _turn_answer_block(final_response: str) -> dict[str, Any]:
    return {
        "tag": "column_set",
        "element_id": _TURN_ANSWER_ELEMENT_ID,
        "flex_mode": "none",
        "background_style": "default",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "padding": "12px",
                "vertical_spacing": "4px",
                "elements": [
                    {"tag": "markdown", "content": final_response},
                ],
            }
        ],
    }


def _turn_files_block(
    *,
    scope: FeishuScope,
    binding_id: str,
    turn_id: str,
    page: TurnFilePage,
    manifest: tuple[TurnFileManifestItem, ...],
    final_response: str,
    progress: TurnProgressManifest | None = None,
    reply: ReplyCardManifest | None = None,
    action_version: int = TURN_FILE_ACTION_VERSION,
    additions: int | None = None,
    deletions: int | None = None,
) -> dict[str, Any]:
    line_counts = ""
    if additions is not None and deletions is not None:
        line_counts = (
            "\n"
            f"<font color='green'>+{additions}</font> "
            f"<font color='red'>-{deletions}</font>"
        )
    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": (
                f"**本轮文件** · 共 {page.total_items} 个 · "
                f"第 {page.page + 1}/{page.total_pages} 页"
                f"{line_counts}\n"
                "<font color='grey'>点击“发送”后，文件将以图片或文件消息发送到本卡片话题。</font>"
            ),
        }
    ]
    elements.extend(
        _turn_file_row(
            scope=scope,
            binding_id=binding_id,
            turn_id=turn_id,
            turn_file=turn_file,
            action_version=action_version,
        )
        for turn_file in page.items
    )
    if page.total_pages > 1:
        elements.append(
            _turn_file_pagination(
                scope=scope,
                binding_id=binding_id,
                turn_id=turn_id,
                page=page.page,
                total_pages=page.total_pages,
                manifest=manifest,
                final_response=final_response,
                progress=progress,
                reply=reply,
                action_version=action_version,
                additions=additions,
                deletions=deletions,
            )
        )
    return {
        "tag": "column_set",
        "element_id": _TURN_FILES_ELEMENT_ID,
        "flex_mode": "none",
        "background_style": "grey-50",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "padding": "12px",
                "vertical_spacing": "8px",
                "elements": elements,
            }
        ],
    }


def _turn_file_row(
    *,
    scope: FeishuScope,
    binding_id: str,
    turn_id: str,
    turn_file: TurnFile,
    action_version: int = TURN_FILE_ACTION_VERSION,
) -> dict[str, Any]:
    if not turn_file.available:
        return {
            "tag": "column_set",
            "flex_mode": "none",
            "background_style": "default",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "padding": "8px",
                    "vertical_spacing": "2px",
                    "elements": [
                        {
                            "tag": "markdown",
                            "content": (
                                f"⚠️ `{_turn_file_label(turn_file.display_path)}`\n"
                                "<font color='grey'>文件当前不可用</font>"
                            ),
                        }
                    ],
                }
            ],
        }
    assert turn_file.size is not None
    assert turn_file.media_kind is not None
    icon = "🖼️" if turn_file.media_kind == "image" else "📄"
    line_counts = ""
    if (
        turn_file.media_kind == "file"
        and turn_file.additions is not None
        and turn_file.deletions is not None
    ):
        line_counts = (
            "  "
            f"<font color='green'>+{turn_file.additions}</font> "
            f"<font color='red'>-{turn_file.deletions}</font>"
        )
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "background_style": "default",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 5,
                "padding": "8px",
                "vertical_spacing": "2px",
                "elements": [
                    {
                        "tag": "markdown",
                        "content": f"{icon} `{_turn_file_label(turn_file.display_path)}`{line_counts}",
                    }
                ],
            },
            {
                "tag": "column",
                "width": "auto",
                "vertical_align": "center",
                "padding": "8px",
                "elements": [
                    _callback_button(
                        label="发送",
                        value=_turn_file_envelope(
                            scope,
                            TurnFileActionName.SEND,
                            version=action_version,
                            binding_id=_binding_reference(binding_id),
                            turn_id=_turn_reference(turn_id),
                            path=str(turn_file.resolved_path),
                        ),
                    )
                ],
            },
        ],
    }


def _encode_turn_file_manifest_item(
    item: TurnFileManifestItem,
) -> dict[str, Any]:
    encoded: dict[str, Any] = {"path": item.path, "label": item.label}
    if item.additions is not None and item.deletions is not None:
        encoded["a"] = item.additions
        encoded["d"] = item.deletions
    return encoded


def _turn_file_pagination(
    *,
    scope: FeishuScope,
    binding_id: str,
    turn_id: str,
    page: int,
    total_pages: int,
    manifest: tuple[TurnFileManifestItem, ...],
    final_response: str,
    progress: TurnProgressManifest | None,
    reply: ReplyCardManifest | None,
    action_version: int,
    additions: int | None,
    deletions: int | None,
) -> dict[str, Any]:
    columns: list[dict[str, Any]] = [
        {
            "tag": "column",
            "width": "weighted",
            "weight": 1,
            "vertical_align": "center",
            "elements": [
                {
                    "tag": "markdown",
                    "content": f"<font color='grey'>第 {page + 1}/{total_pages} 页</font>",
                }
            ],
        }
    ]
    target = 0 if page + 1 >= total_pages else page + 1
    label = "回到第一页" if target == 0 else "下一页"
    page_value: dict[str, Any] = {
        "binding_id": _binding_reference(binding_id),
        "turn_id": _turn_reference(turn_id),
        "page": target,
        "files": [
            _encode_turn_file_manifest_item(item)
            for item in manifest
        ],
    }
    if additions is not None and deletions is not None:
        page_value["a"] = additions
        page_value["d"] = deletions
    if action_version == TURN_FILE_ACTION_VERSION:
        page_value["answer"] = final_response
        if progress is not None:
            page_value["progress"] = _encode_turn_progress_manifest(progress)
    else:
        if reply is None:
            raise ValueError("v5 pagination requires a Reply Card manifest")
        page_value["reply"] = _encode_reply_card_manifest(reply)
    columns.append(
        {
            "tag": "column",
            "width": "auto",
            "elements": [
                _repeatable_callback_button(
                    label=label,
                    value=_turn_file_envelope(
                        scope,
                        TurnFileActionName.PAGE,
                        version=action_version,
                        **page_value,
                    ),
                )
            ],
        }
    )
    return {"tag": "column_set", "flex_mode": "none", "columns": columns}


def _turn_file_envelope(
    scope: FeishuScope,
    name: TurnFileActionName,
    *,
    version: int = TURN_FILE_ACTION_VERSION,
    **extra: Any,
) -> dict[str, Any]:
    value = {
        "v": version,
        "intent": name.value,
        "chat_id": scope.chat_id,
        "scope_kind": scope.kind.value,
        **extra,
    }
    if scope.kind is ScopeKind.TOPIC:
        value["topic_id"] = scope.topic_id
    return value


def _encode_turn_progress_manifest(
    progress: TurnProgressManifest,
) -> dict[str, Any]:
    return {
        "state": progress.state,
        "steer_count": progress.steer_count,
        "plan_available": progress.plan_available,
        "plan_generated": progress.plan_generated,
        "plan_may_be_stale": progress.plan_may_be_stale,
        "steps": [
            {"step": item.step, "status": item.status}
            for item in progress.steps
        ],
        "commentary": [
            (
                item.text
                if item.event_timestamp_ms is None
                else {
                    "text": item.text,
                    "event_timestamp_ms": item.event_timestamp_ms,
                }
            )
            for item in progress.commentary
        ],
        "operations": [
            {
                "kind": item.kind,
                "status": item.status,
                "text": item.text,
                "count": item.count,
                **(
                    {}
                    if item.event_timestamp_ms is None
                    else {"event_timestamp_ms": item.event_timestamp_ms}
                ),
            }
            for item in progress.operations
        ],
    }


def _encode_reply_card_manifest(
    reply: ReplyCardManifest,
) -> dict[str, Any]:
    return {
        "goal": _encode_reply_goal_module(reply.goal),
        "activity": _encode_reply_activity_module(reply.activity),
        "result": (
            None if reply.result is None else {"content": reply.result.content}
        ),
    }


def _encode_reply_goal_module(
    goal: ReplyCardGoalModule | None,
) -> dict[str, Any] | None:
    if goal is None:
        return None
    return {
        "binding_id": _binding_reference(goal.binding_id),
        "short_id": goal.short_id,
        "project_alias": goal.project_alias,
        "goal_generation": goal.goal_generation,
        "status": goal.status,
        "runtime_state": goal.runtime_state,
        "objective": goal.objective,
        "token_budget": goal.token_budget,
        "tokens_used": goal.tokens_used,
        "notice": goal.notice,
        "notice_is_error": goal.notice_is_error,
    }


def _encode_reply_activity_module(
    activity: ReplyCardActivityModule | None,
) -> dict[str, Any] | None:
    if activity is None:
        return None
    # Encode the sanitized frozen view even if a caller bypassed a wrapper.
    progress = _sanitize_turn_progress_manifest(activity.progress)
    return {
        "progress": _encode_turn_progress_manifest(progress),
        "terminal_status": activity.terminal_status,
        "collapsed": activity.collapsed,
        "hidden_steps": activity.hidden_steps,
    }


def _turn_file_label(value: str) -> str:
    visible = "".join(
        character if character.isprintable() else "�" for character in value
    )
    return html.escape(visible, quote=False).replace("`", "ˋ")


def _project_row(
    project: Project,
    controls: list[dict[str, Any]],
    *,
    status: str,
) -> dict[str, Any]:
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "background_style": "grey-50",
        "margin": "0 0 8px 0",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 5,
                "padding": "8px",
                "elements": [
                    _plain(f"{project.alias} · {status}\n{project.cwd}"),
                ],
            },
            {
                "tag": "column",
                "width": "auto",
                "vertical_align": "center",
                "padding": "8px",
                "elements": controls,
            },
        ],
    }


def _archived_session_row(
    *,
    scope: FeishuScope,
    session: ArchivedSessionCardItem,
    native_delete_available: bool,
) -> dict[str, Any]:
    controls = [
        _callback_button(
            label="恢复并切换",
            value=_envelope(
                scope,
                CardControlName.UNARCHIVE_BINDING,
                binding_id=_binding_reference(session.binding_id),
            ),
            style="primary_filled",
        )
    ]
    if native_delete_available:
        controls.append(
            _repeatable_callback_button(
                label="删除",
                value=_envelope(
                    scope,
                    CardControlName.PREPARE_ARCHIVED_DELETE_BINDING,
                    binding_id=_binding_reference(session.binding_id),
                    expected_native_thread_id=_native_thread_reference(
                        session.native_thread_id
                    ),
                ),
                style="danger",
            )
        )
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "background_style": "grey-50",
        "margin": "0 0 8px 0",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 5,
                "padding": "8px",
                "elements": [
                    _plain(
                        f"{session.title}\n"
                        f"会话：{session.short_id} · "
                        f"Project：{session.project_alias} · "
                        f"Native：{session.native_thread_id[:8]}"
                    )
                ],
            },
            {
                "tag": "column",
                "width": "auto",
                "vertical_align": "center",
                "padding": "8px",
                "elements": controls,
            },
        ],
    }


def _button_row(*buttons: dict[str, Any]) -> dict[str, Any]:
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "elements": [button],
            }
            for button in buttons
        ],
    }


def _new_callback_nonce() -> str:
    return secrets.token_hex(16)


def _valid_callback_nonce(value: Any) -> bool:
    return isinstance(value, str) and _CALLBACK_NONCE.fullmatch(value) is not None


def _repeatable_callback_button(
    *,
    label: str,
    value: dict[str, Any],
    style: str = "default",
    confirm: tuple[str, str] | None = None,
) -> dict[str, Any]:
    if "nonce" in value:
        raise ValueError("callback value already contains a nonce")
    return _callback_button(
        label=label,
        value={**value, "nonce": _new_callback_nonce()},
        style=style,
        confirm=confirm,
    )


def _callback_button(
    *,
    label: str,
    value: dict[str, Any],
    style: str = "default",
    confirm: tuple[str, str] | None = None,
) -> dict[str, Any]:
    intent = value.get("intent")
    is_repeatable = intent in _REPEATABLE_CALLBACK_INTENTS
    has_valid_nonce = _valid_callback_nonce(value.get("nonce"))
    if (is_repeatable and not has_valid_nonce) or (
        not is_repeatable and "nonce" in value
    ):
        raise ValueError(
            "repeatable callback values must carry exactly one valid nonce"
        )
    button: dict[str, Any] = {
        "tag": "button",
        "text": _plain_text(label),
        "type": style,
        "behaviors": [{"type": "callback", "value": value}],
    }
    if confirm is not None:
        button["confirm"] = {
            "title": _plain_text(confirm[0]),
            "text": _plain_text(confirm[1]),
        }
    return button


def _notice(message: str, *, error: bool = False) -> dict[str, Any]:
    return {
        "tag": "column_set",
        "background_style": "red-50" if error else "blue-50",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "padding": "8px",
                "elements": [_plain(message)],
            }
        ],
    }


def _plain(content: str) -> dict[str, Any]:
    return {"tag": "div", "text": _plain_text(content)}


def _plain_text(content: str) -> dict[str, str]:
    return {"tag": "plain_text", "content": content}


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise CardActionError(f"{field} 必须是非空字符串。")
    return value


def _bounded_string(value: Any, field: str, *, max_chars: int) -> str:
    result = _required_string(value, field)
    if len(result) > max_chars or "\x00" in result:
        raise CardActionError(f"{field} 内容无效或超过 {max_chars} 个字符。")
    return result


def _md_code(value: str) -> str:
    return value.replace("`", "ˋ")
