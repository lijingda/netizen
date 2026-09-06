"""Reply Card, Activity, and Files rendering with strict manifest decoding."""

from __future__ import annotations

import html
import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

from lark_channel import OutboundCard, new_card

from ..domain import (
    ACTIVE_STATE_VALUES,
    ActiveState,
    CardControlName,
    FeishuScope,
    GoalOperationState,
    GoalStatus,
    ReplyCardActivityModule,
    ReplyCardFileItem,
    ReplyCardFilesModule,
    ReplyCardGoalModule,
    ReplyCardManifest,
    ReplyCardProjection,
    ReplyCardResultModule,
    ScopeKind,
    TurnActivityManifestEntry,
    TurnCommentaryManifestEntry,
    TurnFileActionIntent,
    TurnFileActionName,
    TurnFileManifestItem,
    TurnProgressManifest,
    TurnProgressManifestStep,
)
from ..turn_files import (
    TurnFile,
    TurnFilePage,
    inspect_turn_file_path,
    paginate_turn_files,
)
from ..turn_activity import (
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
from .callbacks import (
    CardActionError,
    REPLY_CARD_ACTION_VERSION,
    TURN_FILE_ACTION_VERSION,
    TurnFileCardLimitError,
    _REPEATABLE_CARD_CONTROL_NAMES,
    _binding_reference,
    _button_row,
    _callback_button,
    _decode_binding_reference,
    _decode_goal_generation,
    _decode_turn_reference,
    _envelope,
    _md_code,
    _notice,
    _plain,
    _repeatable_callback_button,
    _required_string,
    _scope_from_envelope,
    _turn_reference,
)


TURN_FILE_MANIFEST_LIMIT = 400
TURN_FILE_CARD_JSON_LIMIT_BYTES = 55_000
_TURN_ANSWER_ELEMENT_ID = "turnanswerv1"
_TURN_FILES_ELEMENT_ID = "turnfilesv4"
_TURN_PROGRESS_ELEMENT_ID = "turnprogressv1"
_GOAL_ELEMENT_ID = "goalmodulev1"
_GOAL_OBJECTIVE_PREVIEW_CHARS = 200
_TURN_PROGRESS_MAX_STEPS = ACTIVITY_PLAN_LIMIT
_TURN_PROGRESS_STEP_MAX_CHARS = ACTIVITY_TEXT_LIMIT


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
            "\n累计修改 "
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
                "<font color='grey'>行数累计成功的文件修改，重复修改会重复计数；"
                "统计不完整时省略总计或相应文件的数字。</font>\n"
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
