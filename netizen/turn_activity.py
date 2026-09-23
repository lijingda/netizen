"""Safe, bounded projection of native Turn notifications for reply cards."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from openai_codex.generated.v2_all import (
    AgentMessageThreadItem,
    AsyncUserInputQuestion,
    CollabAgentToolCallThreadItem,
    CommandExecutionThreadItem,
    ContextCompactionThreadItem,
    DynamicToolCallThreadItem,
    EnteredReviewModeThreadItem,
    ExitedReviewModeThreadItem,
    FileChangeThreadItem,
    FindInPageWebSearchAction,
    ImageGenerationThreadItem,
    ImageViewThreadItem,
    ItemCompletedNotification,
    ItemStartedNotification,
    ListFilesCommandAction,
    MessagePhase,
    McpToolCallThreadItem,
    OpenPageWebSearchAction,
    ReadCommandAction,
    SearchCommandAction,
    SearchWebSearchAction,
    SubAgentActivityThreadItem,
    ThreadItem,
    TurnCompletedNotification,
    TurnPlanStep,
    TurnPlanStepStatus,
    TurnPlanUpdatedNotification,
    TurnStartedNotification,
    WebSearchThreadItem,
)
from openai_codex.models import Notification

from .user_questions import QuestionRequest, UserQuestion


ACTIVITY_COMMENTARY_LIMIT = 4
ACTIVITY_OPERATION_LIMIT = 8
ACTIVITY_PLAN_LIMIT = 12
ACTIVITY_TEXT_LIMIT = 160
ACTIVITY_OPERATION_TEXT_LIMIT = 120
ACTIVITY_TAB_SPACES = 4
SIDE_ACTIVITY_QUEUE_HIGH_WATER = 4_096

_ITEM_STARTED_METHOD = "item/started"
_ITEM_COMPLETED_METHOD = "item/completed"
_PLAN_METHOD = "turn/plan/updated"
_TURN_STARTED_METHOD = "turn/started"
_TURN_COMPLETED_METHOD = "turn/completed"

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?:api[ _-]?key|access[ _-]?token|refresh[ _-]?token|"
    r"auth(?:entication|orization)?|bearer|cookie|credential|password|"
    r"passwd|secret|session[ _-]?token|密码|口令|密钥|令牌|凭据|授权|认证)"
    r"[\"']?\s*(?:=|:|：|(?<![A-Za-z0-9_])is(?![A-Za-z0-9_]))"
    r"\s*[^\s,;，；]+"
)
_SECRET_FLAG = re.compile(
    r"(?i)(?<!\S)--(?:api[ _-]?key|access[ _-]?token|refresh[ _-]?token|"
    r"session[ _-]?token|token|password|passwd|secret|credential|cookie)"
    r"(?:\s+|=)\S+"
)
_URL_SECRET_QUERY = re.compile(
    r"(?i)[?&](?:token|key|signature|sig|x-amz-signature)=[^\s&#]+"
)
_BEARER_TOKEN = re.compile(
    r"(?i)(?<![A-Za-z0-9_])bearer(?![A-Za-z0-9_])\s+\S+"
)
_PEM = re.compile(r"-----BEGIN [^-]+-----", re.IGNORECASE)
_URL_CREDENTIAL = re.compile(
    r"(?i)(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9+.-]*://"
    r"[^\s/@:]*:[^\s/@]+@"
)
_KNOWN_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_])(?:AKIA[0-9A-Z]{16}|"
    r"(?:sk|gh[pousr])[-_][A-Za-z0-9_-]{16,}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"
    r"(?![A-Za-z0-9_])"
)
_TIME_VALUE = (
    r"\d+(?:\.\d+)?\s*(?:milliseconds?|seconds?|minutes?|hours?|"
    r"ms|s|m|h|毫秒|秒钟?|分钟?|小时)(?![A-Za-z0-9_])"
)
_ELAPSED = re.compile(
    r"(?i)(?:(?<![A-Za-z0-9_])(?:elapsed|worked\s+for)"
    r"(?![A-Za-z0-9_])|(?:耗时|用时))"
    r"\s*[:=：]?\s*" + _TIME_VALUE
)
_PERCENT = re.compile(r"(?<!\d)\d{1,3}(?:[.．]\d+)?\s*[%％]")
_ETA = re.compile(
    r"(?i)(?<![A-Za-z0-9_])ETA(?![A-Za-z0-9_])"
    r"\s*[:=：]?\s*" + _TIME_VALUE
)


class TurnActivityProjectionUnavailable(RuntimeError):
    """An allowlisted native notification no longer has its pinned shape."""


class TurnPlanStepState(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "inProgress"
    COMPLETED = "completed"


class TurnActivityKind(str, Enum):
    COMMENTARY = "commentary"
    COMMAND = "command"
    TOOL = "tool"
    FILE_CHANGE = "fileChange"
    WEB_SEARCH = "webSearch"
    IMAGE = "image"
    SUBAGENT = "subagent"
    REVIEW = "review"
    COMPACTION = "compaction"


ACTIVITY_DETAIL_KINDS = frozenset(
    {TurnActivityKind.COMMAND, TurnActivityKind.FILE_CHANGE, TurnActivityKind.WEB_SEARCH}
)


class TurnActivityStatus(str, Enum):
    IN_PROGRESS = "inProgress"
    COMPLETED = "completed"
    FAILED = "failed"
    DECLINED = "declined"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class TurnPlanStepSnapshot:
    step: str
    status: TurnPlanStepState


@dataclass(frozen=True, slots=True)
class TurnActivityEvent:
    item_id: str
    kind: TurnActivityKind
    status: TurnActivityStatus
    event_timestamp_ms: int
    text: str | None = None
    count: int = 1

    def __post_init__(self) -> None:
        _validate_exact_id(self.item_id, label="item")
        _validate_timestamp_ms(self.event_timestamp_ms)
        _validate_activity_text(self.kind, self.text)
        if isinstance(self.count, bool) or not isinstance(self.count, int) or self.count < 0:
            raise ValueError("activity count must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class TurnActivityEntrySnapshot:
    """One identity-free activity row safe to hand to the Channel layer."""

    kind: TurnActivityKind
    status: TurnActivityStatus
    event_timestamp_ms: int
    text: str | None = None
    count: int = 1

    def __post_init__(self) -> None:
        _validate_timestamp_ms(self.event_timestamp_ms)
        _validate_activity_text(self.kind, self.text)
        if isinstance(self.count, bool) or not isinstance(self.count, int) or self.count < 0:
            raise ValueError("activity count must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class TurnActivityNotificationProjection:
    turn_id: str | None = None
    turn_started: bool = False
    turn_completed: bool = False
    plan_updated: bool = False
    steps: tuple[TurnPlanStepSnapshot, ...] = ()
    event: TurnActivityEvent | None = None
    question: QuestionRequest | None = None

    def __post_init__(self) -> None:
        if self.turn_id is not None:
            _validate_exact_id(self.turn_id, label="Turn")
        if self.turn_id is None and (
            self.turn_started
            or self.turn_completed
            or self.plan_updated
            or self.event is not None
            or self.question is not None
        ):
            raise ValueError("activity projection requires an exact Turn ID")
        if not self.plan_updated and self.steps:
            raise ValueError("activity plan steps require a full plan replacement")


def project_turn_activity_notification(
    notification: Notification,
    *,
    expected_thread_id: str,
    expected_turn_id: str | None,
) -> TurnActivityNotificationProjection:
    """Project one raw notification into a safe internal control event.

    Exact Turn and item identities remain process-local so consumers can reject
    stale events and coalesce lifecycle updates. Channel-facing snapshots strip
    item identities and never receive arguments, output, or native payloads.
    """

    if type(notification) is not Notification:
        raise TurnActivityProjectionUnavailable(
            "native Turn notification item shape changed"
        )
    _validate_exact_id(expected_thread_id, label="Thread")
    if expected_turn_id is not None:
        _validate_exact_id(expected_turn_id, label="Turn")

    method = notification.method
    payload = notification.payload
    if method == _TURN_STARTED_METHOD:
        if type(payload) is not TurnStartedNotification:
            raise TurnActivityProjectionUnavailable("Turn started payload shape changed")
        turn_id = _turn_identity(payload.thread_id, payload.turn.id, expected_thread_id)
        if turn_id is None:
            return TurnActivityNotificationProjection()
        return TurnActivityNotificationProjection(turn_id=turn_id, turn_started=True)
    if method == _TURN_COMPLETED_METHOD:
        if type(payload) is not TurnCompletedNotification:
            raise TurnActivityProjectionUnavailable("Turn completed payload shape changed")
        turn_id = _turn_identity(payload.thread_id, payload.turn.id, expected_thread_id)
        if turn_id is None or (
            expected_turn_id is not None and turn_id != expected_turn_id
        ):
            return TurnActivityNotificationProjection()
        return TurnActivityNotificationProjection(turn_id=turn_id, turn_completed=True)
    if method == _PLAN_METHOD:
        if type(payload) is not TurnPlanUpdatedNotification:
            raise TurnActivityProjectionUnavailable("Turn plan payload shape changed")
        turn_id = _turn_identity(payload.thread_id, payload.turn_id, expected_thread_id)
        if turn_id is None or (
            expected_turn_id is not None and turn_id != expected_turn_id
        ):
            return TurnActivityNotificationProjection()
        return TurnActivityNotificationProjection(
            turn_id=turn_id,
            plan_updated=True,
            steps=project_plan_steps(payload.plan),
        )
    if method not in {_ITEM_STARTED_METHOD, _ITEM_COMPLETED_METHOD}:
        return TurnActivityNotificationProjection()

    expected_payload_type = (
        ItemStartedNotification
        if method == _ITEM_STARTED_METHOD
        else ItemCompletedNotification
    )
    if type(payload) is not expected_payload_type:
        raise TurnActivityProjectionUnavailable("Turn item payload shape changed")
    turn_id = _turn_identity(payload.thread_id, payload.turn_id, expected_thread_id)
    if turn_id is None or (
        expected_turn_id is not None and turn_id != expected_turn_id
    ):
        return TurnActivityNotificationProjection()
    if type(payload.item) is not ThreadItem:
        raise TurnActivityProjectionUnavailable("native Thread item shape changed")
    completed = method == _ITEM_COMPLETED_METHOD
    event_timestamp_ms = (
        payload.completed_at_ms if completed else payload.started_at_ms
    )
    if (
        isinstance(event_timestamp_ms, bool)
        or not isinstance(event_timestamp_ms, int)
        or event_timestamp_ms < 0
    ):
        raise TurnActivityProjectionUnavailable(
            "native Turn item lifecycle timestamp changed"
        )
    event = _project_item(
        payload.item.root,
        completed=completed,
        event_timestamp_ms=event_timestamp_ms,
    )
    return TurnActivityNotificationProjection(
        turn_id=turn_id,
        event=event,
        question=_project_question(payload.item.root) if completed else None,
    )


def _project_question(item: object) -> QuestionRequest | None:
    """Project the native user-facing question fields, independently of Activity.

    Delivery and phase describe the message, not whether its structured questions
    should be shown. Preserve full question/option text for the answer protocol.
    """

    if type(item) is not AgentMessageThreadItem or not item.questions:
        return None
    if type(item.questions) is not list:
        raise TurnActivityProjectionUnavailable("native questions shape changed")
    try:
        _validate_exact_id(item.id, label="item")
    except ValueError as error:
        raise TurnActivityProjectionUnavailable("native question item ID changed") from error
    questions: list[UserQuestion] = []
    for question in item.questions:
        if (
            type(question) is not AsyncUserInputQuestion
            or not isinstance(question.title, str)
            or not question.title.strip()
            or (
                question.options is not None
                and (
                    type(question.options) is not list
                    or any(not isinstance(option, str) for option in question.options)
                )
            )
        ):
            raise TurnActivityProjectionUnavailable("native question fields changed")
        questions.append(UserQuestion(question.title, tuple(question.options or ())))
    return QuestionRequest(item.id, tuple(questions))


def project_plan_steps(items: object) -> tuple[TurnPlanStepSnapshot, ...]:
    if not isinstance(items, list):
        raise TurnActivityProjectionUnavailable("native Turn plan is not a list")
    steps: list[TurnPlanStepSnapshot] = []
    for item in items:
        if type(item) is not TurnPlanStep:
            raise TurnActivityProjectionUnavailable("native Turn plan step shape changed")
        step = item.step
        if not isinstance(step, str) or not step.strip():
            raise TurnActivityProjectionUnavailable("native Turn plan step is empty")
        status = getattr(item.status, "value", None)
        try:
            mapped = TurnPlanStepState(status)
        except (TypeError, ValueError) as error:
            raise TurnActivityProjectionUnavailable(
                "native Turn plan step status changed"
            ) from error
        safe_step = sanitize_activity_text(step)
        if safe_step is None:
            raise TurnActivityProjectionUnavailable("native Turn plan step is empty")
        steps.append(TurnPlanStepSnapshot(step=safe_step, status=mapped))
    return tuple(steps)


def sanitize_activity_text(value: str) -> str | None:
    """Retain useful commentary/checklist text while filtering credentials and estimates."""

    normalized = normalize_activity_text_layout(value)
    if not normalized.strip():
        return None
    redacted = _redact_activity_credentials(normalized)
    redacted = _ELAPSED.sub("[时间信息已隐藏]", redacted)
    redacted = _PERCENT.sub("[百分比已隐藏]", redacted)
    redacted = _ETA.sub("[时间估算已隐藏]", redacted)
    return _bounded_activity_text(redacted)


def sanitize_activity_operation_text(
    value: str, *, limit: int = ACTIVITY_OPERATION_TEXT_LIMIT
) -> str | None:
    """Bound a literal native-field preview without interpreting command syntax."""

    normalized = normalize_activity_text_layout(value)
    if not normalized.strip():
        return None
    # Filter before truncation so a clipped credential never becomes visible.
    redacted = _redact_activity_credentials(normalized)
    return _bounded_activity_text(" ".join(redacted.split()), limit)


def _redact_activity_credentials(value: str) -> str:
    if (
        _SECRET_ASSIGNMENT.search(value)
        or _SECRET_FLAG.search(value)
        or _URL_SECRET_QUERY.search(value)
        or _BEARER_TOKEN.search(value)
        or _PEM.search(value)
        or _URL_CREDENTIAL.search(value)
    ):
        return "[敏感内容已隐藏]"
    return _KNOWN_TOKEN.sub("[敏感内容已隐藏]", value)


def _bounded_activity_text(value: str, limit: int = ACTIVITY_TEXT_LIMIT) -> str:
    if len(value) > limit:
        return value[: limit - 1].rstrip() + "…"
    return value


def normalize_activity_text_layout(value: str) -> str:
    """Preserve supported Markdown layout and replace other controls."""

    if not isinstance(value, str):
        raise TurnActivityProjectionUnavailable("activity text shape changed")
    canonical = (
        value.replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\t", " " * ACTIVITY_TAB_SPACES)
    )
    return "".join(
        character
        if character == "\n" or character.isprintable()
        else "�"
        for character in canonical
    )


def _project_item(
    item: object,
    *,
    completed: bool,
    event_timestamp_ms: int,
) -> TurnActivityEvent | None:
    lifecycle_status = (
        TurnActivityStatus.COMPLETED
        if completed
        else TurnActivityStatus.IN_PROGRESS
    )
    if type(item) is AgentMessageThreadItem:
        if not completed or item.phase is not MessagePhase.commentary:
            return None
        text = sanitize_activity_text(item.text)
        if text is None:
            return None
        return TurnActivityEvent(
            item_id=item.id,
            kind=TurnActivityKind.COMMENTARY,
            status=TurnActivityStatus.COMPLETED,
            event_timestamp_ms=event_timestamp_ms,
            text=text,
        )
    if type(item) is CommandExecutionThreadItem:
        return _status_event(
            item.id,
            TurnActivityKind.COMMAND,
            item.status,
            lifecycle_status,
            event_timestamp_ms=event_timestamp_ms,
            text=_command_activity_summary(item),
        )
    if type(item) is McpToolCallThreadItem:
        return _status_event(
            item.id,
            TurnActivityKind.TOOL,
            item.status,
            lifecycle_status,
            event_timestamp_ms=event_timestamp_ms,
            text=_tool_name(item.tool),
        )
    if type(item) is DynamicToolCallThreadItem:
        tool = _tool_name(item.tool)
        namespace = item.namespace
        if namespace:
            namespace = _tool_name(namespace)
            tool = f"{namespace}.{tool}"
        return _status_event(
            item.id,
            TurnActivityKind.TOOL,
            item.status,
            lifecycle_status,
            event_timestamp_ms=event_timestamp_ms,
            text=tool,
        )
    if type(item) is FileChangeThreadItem:
        return _status_event(
            item.id,
            TurnActivityKind.FILE_CHANGE,
            item.status,
            lifecycle_status,
            event_timestamp_ms=event_timestamp_ms,
            text=_file_change_activity_summary(item),
            count=len(item.changes),
        )
    if type(item) is WebSearchThreadItem:
        return TurnActivityEvent(
            item.id,
            TurnActivityKind.WEB_SEARCH,
            lifecycle_status,
            event_timestamp_ms,
            text=_web_search_activity_summary(item),
        )
    if type(item) in {ImageViewThreadItem, ImageGenerationThreadItem}:
        status = item.status if type(item) is ImageGenerationThreadItem else None
        return _status_event(
            item.id,
            TurnActivityKind.IMAGE,
            status,
            lifecycle_status,
            event_timestamp_ms=event_timestamp_ms,
        )
    if type(item) is CollabAgentToolCallThreadItem:
        count = max(len(item.receiver_thread_ids), len(item.agents_states), 1)
        return _status_event(
            item.id,
            TurnActivityKind.SUBAGENT,
            item.status,
            lifecycle_status,
            event_timestamp_ms=event_timestamp_ms,
            count=count,
        )
    if type(item) is SubAgentActivityThreadItem:
        # The SDK emits started/completed item envelopes for these activity
        # records. A completed child must not briefly appear to be running
        # when its activity record first arrives.
        status = {
            "interrupted": TurnActivityStatus.INTERRUPTED,
            "completed": TurnActivityStatus.COMPLETED,
        }.get(getattr(item.kind, "value", None), lifecycle_status)
        return TurnActivityEvent(
            item.id,
            TurnActivityKind.SUBAGENT,
            status,
            event_timestamp_ms,
        )
    if type(item) in {EnteredReviewModeThreadItem, ExitedReviewModeThreadItem}:
        return TurnActivityEvent(
            item.id,
            TurnActivityKind.REVIEW,
            lifecycle_status,
            event_timestamp_ms,
        )
    if type(item) is ContextCompactionThreadItem:
        return TurnActivityEvent(
            item.id,
            TurnActivityKind.COMPACTION,
            lifecycle_status,
            event_timestamp_ms,
        )
    return None


def _status_event(
    item_id: str,
    kind: TurnActivityKind,
    native_status: object,
    lifecycle_status: TurnActivityStatus,
    *,
    event_timestamp_ms: int,
    text: str | None = None,
    count: int = 1,
) -> TurnActivityEvent:
    value = getattr(native_status, "value", native_status)
    try:
        status = TurnActivityStatus(value)
    except (TypeError, ValueError):
        status = lifecycle_status
    return TurnActivityEvent(
        item_id,
        kind,
        status,
        event_timestamp_ms,
        text=text,
        count=count,
    )


def _operation_summary(label: str, *details: str | None) -> str:
    # Sanitize each native field before combining it with display labels.
    parts = [label]
    for detail in details:
        if detail is not None:
            safe = sanitize_activity_operation_text(detail)
            if safe:
                parts.append(safe)
    return _bounded_activity_text(" · ".join(parts), ACTIVITY_OPERATION_TEXT_LIMIT)


def _command_activity_summary(item: CommandExecutionThreadItem) -> str:
    summary = None
    # commandActions is Codex's best-effort classification. Do not build a
    # second shell parser, infer purposes, or summarize composite commands.
    if len(item.command_actions) == 1:
        action = item.command_actions[0].root
        if type(action) is ReadCommandAction and action.path.root.strip():
            summary = _operation_summary("读取文件", action.path.root)
        elif (
            type(action) is ListFilesCommandAction
            and action.path
            and action.path.strip()
        ):
            summary = _operation_summary("列出文件", action.path)
        elif type(action) is SearchCommandAction and (
            (action.query and action.query.strip()) or (action.path and action.path.strip())
        ):
            summary = _operation_summary("搜索内容", action.query, action.path)
    if summary is None:
        summary = _operation_summary("执行命令", item.command)
    if item.exit_code is not None and item.exit_code != 0:
        suffix = f" · 退出码 {item.exit_code}"
        summary = (
            _bounded_activity_text(summary, ACTIVITY_OPERATION_TEXT_LIMIT - len(suffix))
            + suffix
        )
    return summary


def _file_change_activity_summary(item: FileChangeThreadItem) -> str | None:
    if not item.changes:
        return None
    labels = {"add": "新增", "delete": "删除", "update": "更新"}
    changes = []
    for change in item.changes[:3]:
        kind = change.kind.root
        label = labels.get(kind.type, "修改")
        path = change.path
        if kind.type == "update" and kind.move_path:
            path += f" → {kind.move_path}"
        safe_path = sanitize_activity_operation_text(path)
        changes.append(f"{label} {safe_path}" if safe_path else label)
    detail = "、".join(changes) + ("、…" if len(item.changes) > 3 else "")
    return _operation_summary("修改文件", detail)


def _web_search_activity_summary(item: WebSearchThreadItem) -> str:
    action = item.action.root if item.action is not None else None
    if type(action) is OpenPageWebSearchAction:
        return _operation_summary("打开网页", action.url or item.query)
    if type(action) is FindInPageWebSearchAction:
        return _operation_summary(
            "查找网页内容", action.pattern, action.url or item.query
        )
    if type(action) is SearchWebSearchAction:
        queries = [query for query in (action.queries or ()) if query.strip()]
        if queries:
            detail = "、".join(
                sanitize_activity_operation_text(query) or "" for query in queries[:3]
            ) + ("、…" if len(queries) > 3 else "")
            return _operation_summary("搜索网页", detail)
        return _operation_summary("搜索网页", action.query or item.query)
    return _operation_summary("搜索网页", item.query)


def _tool_name(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise TurnActivityProjectionUnavailable("native tool name shape changed")
    return value


def _validate_timestamp_ms(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("activity timestamp must be a non-negative integer")


def _validate_activity_text(
    kind: TurnActivityKind,
    value: str | None,
) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value:
        raise ValueError("activity text must be a non-empty string")
    if kind == TurnActivityKind.COMMENTARY and len(value) > ACTIVITY_TEXT_LIMIT:
        raise ValueError("activity text must be bounded")
    if kind in ACTIVITY_DETAIL_KINDS and len(value) > ACTIVITY_OPERATION_TEXT_LIMIT:
        raise ValueError("activity text must be bounded")
    if kind not in {
        TurnActivityKind.COMMENTARY,
        TurnActivityKind.TOOL,
        *ACTIVITY_DETAIL_KINDS,
    }:
        raise ValueError("activity text is unsupported for this kind")


def _turn_identity(
    actual_thread_id: object,
    actual_turn_id: object,
    expected_thread_id: str,
) -> str | None:
    if actual_thread_id != expected_thread_id:
        return None
    if not isinstance(actual_turn_id, str) or not actual_turn_id:
        raise TurnActivityProjectionUnavailable("native Turn ID shape changed")
    return actual_turn_id


def _validate_exact_id(value: str, *, label: str) -> None:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"native {label} ID must be a non-empty trimmed string")
