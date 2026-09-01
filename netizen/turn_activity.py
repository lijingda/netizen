"""Safe, bounded projection of native Turn notifications for reply cards."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from openai_codex.generated.v2_all import (
    AgentMessageThreadItem,
    CollabAgentToolCallThreadItem,
    CommandExecutionThreadItem,
    ContextCompactionThreadItem,
    DynamicToolCallThreadItem,
    EnteredReviewModeThreadItem,
    ExitedReviewModeThreadItem,
    FileChangeThreadItem,
    ImageGenerationThreadItem,
    ImageViewThreadItem,
    ItemCompletedNotification,
    ItemStartedNotification,
    MessagePhase,
    McpToolCallThreadItem,
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


ACTIVITY_COMMENTARY_LIMIT = 3
ACTIVITY_OPERATION_LIMIT = 8
ACTIVITY_PLAN_LIMIT = 12
ACTIVITY_TEXT_LIMIT = 160
SIDE_ACTIVITY_QUEUE_HIGH_WATER = 4_096

_ITEM_STARTED_METHOD = "item/started"
_ITEM_COMPLETED_METHOD = "item/completed"
_PLAN_METHOD = "turn/plan/updated"
_TURN_STARTED_METHOD = "turn/started"
_TURN_COMPLETED_METHOD = "turn/completed"

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?:api[ _-]?key|access[ _-]?token|"
    r"auth(?:entication|orization)?|bearer|cookie|credential|password|"
    r"passwd|secret|session[ _-]?token|密码|口令|密钥|令牌|凭据|授权|认证)"
    r"\s*(?:=|:|：|(?<![A-Za-z0-9_])is(?![A-Za-z0-9_]))"
    r"\s*[^\s,;，；]+"
)
_BEARER_TOKEN = re.compile(
    r"(?i)(?<![A-Za-z0-9_])bearer(?![A-Za-z0-9_])\s+\S+"
)
_PEM = re.compile(r"-----BEGIN [^-]+-----", re.IGNORECASE)
_URL_CREDENTIAL = re.compile(
    r"(?i)(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9+.-]*://"
    r"[^\s/@:]*:[^\s/@]+@"
)
_URL = re.compile(
    r"(?i)(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9+.-]*://[^\s,;，；]+"
)
_KNOWN_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_])(?:AKIA[0-9A-Z]{16}|"
    r"(?:sk|gh[pousr])[-_][A-Za-z0-9_-]{16,}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"
    r"(?![A-Za-z0-9_])"
)
_LONG_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_])[A-Za-z0-9_+/=-]{32,}(?![A-Za-z0-9_])"
)
_EMAIL = re.compile(
    r"(?<![A-Za-z0-9_])[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?![A-Za-z0-9_])"
)
_HOME_PATH = re.compile(
    r"(?i)(?:~[/\\]|/(?:Users|home)/[^\s/\\]+[/\\]|"
    r"[A-Z]:\\Users\\[^\s\\]+\\)[^\s,;]*"
)
_ABSOLUTE_PATH = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(?:/(?:[^\s/]+/?)+|[A-Z]:\\[^\s,;，；]+)"
)
_RELATIVE_PATH = re.compile(
    r"(?<![A-Za-z0-9_])(?:(?:\.\.?/)+|[A-Za-z0-9_.-]+/)"
    r"[A-Za-z0-9_./-]+"
)
_INLINE_CODE = re.compile(r"`[^`]*`")
_ELAPSED = re.compile(
    r"(?i)(?:(?<![A-Za-z0-9_])(?:elapsed|worked\s+for)"
    r"(?![A-Za-z0-9_])|(?:耗时|用时))"
    r"\s*[:=：]?\s*[^,;，。；]*"
)
_PERCENT = re.compile(r"(?<!\d)\d{1,3}(?:[.．]\d+)?\s*[%％]")
_ETA = re.compile(
    r"(?i)(?<![A-Za-z0-9_])ETA(?![A-Za-z0-9_])"
    r"(?:\s*[:=：]?\s*[^,;，。；]*)?"
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
    text: str | None = None
    count: int = 1

    def __post_init__(self) -> None:
        _validate_exact_id(self.item_id, label="item")
        if self.text is not None and (
            not isinstance(self.text, str)
            or not self.text
            or len(self.text) > ACTIVITY_TEXT_LIMIT
        ):
            raise ValueError("activity text must be non-empty and bounded")
        if isinstance(self.count, bool) or not isinstance(self.count, int) or self.count < 0:
            raise ValueError("activity count must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class TurnActivityEntrySnapshot:
    """One identity-free activity row safe to hand to the Channel layer."""

    kind: TurnActivityKind
    status: TurnActivityStatus
    text: str | None = None
    count: int = 1

    def __post_init__(self) -> None:
        if self.text is not None and (
            not isinstance(self.text, str)
            or not self.text
            or len(self.text) > ACTIVITY_TEXT_LIMIT
        ):
            raise ValueError("activity text must be non-empty and bounded")
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

    def __post_init__(self) -> None:
        if self.turn_id is not None:
            _validate_exact_id(self.turn_id, label="Turn")
        if (self.turn_started or self.turn_completed or self.plan_updated or self.event) and (
            self.turn_id is None
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
    event = _project_item(
        payload.item.root,
        completed=method == _ITEM_COMPLETED_METHOD,
    )
    return TurnActivityNotificationProjection(turn_id=turn_id, event=event)


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
    """Return conservative, bounded display text or ``None`` for empty input."""

    if not isinstance(value, str):
        raise TurnActivityProjectionUnavailable("activity text shape changed")
    normalized = " ".join(
        "".join(character if character.isprintable() else "�" for character in value)
        .split()
    )
    if not normalized:
        return None
    if (
        _SECRET_ASSIGNMENT.search(normalized)
        or _BEARER_TOKEN.search(normalized)
        or _PEM.search(normalized)
        or _URL_CREDENTIAL.search(normalized)
    ):
        return "[敏感内容已隐藏]"
    redacted = normalized
    redacted = _KNOWN_TOKEN.sub("[敏感内容已隐藏]", redacted)
    redacted = _LONG_TOKEN.sub("[敏感内容已隐藏]", redacted)
    redacted = _EMAIL.sub("[敏感内容已隐藏]", redacted)
    redacted = _HOME_PATH.sub("[路径已隐藏]", redacted)
    redacted = _URL.sub("[链接已隐藏]", redacted)
    redacted = _ABSOLUTE_PATH.sub("[路径已隐藏]", redacted)
    redacted = _RELATIVE_PATH.sub("[路径已隐藏]", redacted)
    redacted = _INLINE_CODE.sub("[代码或参数已隐藏]", redacted)
    redacted = _ELAPSED.sub("[时间信息已隐藏]", redacted)
    redacted = _PERCENT.sub("[百分比已隐藏]", redacted)
    redacted = _ETA.sub("[时间估算已隐藏]", redacted)
    redacted = " ".join(redacted.split()) or "内容已隐藏"
    if len(redacted) > ACTIVITY_TEXT_LIMIT:
        return redacted[: ACTIVITY_TEXT_LIMIT - 1].rstrip() + "…"
    return redacted


def _project_item(item: object, *, completed: bool) -> TurnActivityEvent | None:
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
            text=text,
        )
    if type(item) is CommandExecutionThreadItem:
        return _status_event(item.id, TurnActivityKind.COMMAND, item.status, lifecycle_status)
    if type(item) in {McpToolCallThreadItem, DynamicToolCallThreadItem}:
        return _status_event(item.id, TurnActivityKind.TOOL, item.status, lifecycle_status)
    if type(item) is FileChangeThreadItem:
        return _status_event(
            item.id,
            TurnActivityKind.FILE_CHANGE,
            item.status,
            lifecycle_status,
            count=len(item.changes),
        )
    if type(item) is WebSearchThreadItem:
        return TurnActivityEvent(item.id, TurnActivityKind.WEB_SEARCH, lifecycle_status)
    if type(item) in {ImageViewThreadItem, ImageGenerationThreadItem}:
        status = item.status if type(item) is ImageGenerationThreadItem else None
        return _status_event(item.id, TurnActivityKind.IMAGE, status, lifecycle_status)
    if type(item) is CollabAgentToolCallThreadItem:
        count = max(len(item.receiver_thread_ids), len(item.agents_states), 1)
        return _status_event(
            item.id,
            TurnActivityKind.SUBAGENT,
            item.status,
            lifecycle_status,
            count=count,
        )
    if type(item) is SubAgentActivityThreadItem:
        status = (
            TurnActivityStatus.INTERRUPTED
            if getattr(item.kind, "value", None) == "interrupted"
            else lifecycle_status
        )
        return TurnActivityEvent(item.id, TurnActivityKind.SUBAGENT, status)
    if type(item) in {EnteredReviewModeThreadItem, ExitedReviewModeThreadItem}:
        return TurnActivityEvent(item.id, TurnActivityKind.REVIEW, lifecycle_status)
    if type(item) is ContextCompactionThreadItem:
        return TurnActivityEvent(item.id, TurnActivityKind.COMPACTION, lifecycle_status)
    return None


def _status_event(
    item_id: str,
    kind: TurnActivityKind,
    native_status: object,
    lifecycle_status: TurnActivityStatus,
    *,
    count: int = 1,
) -> TurnActivityEvent:
    value = getattr(native_status, "value", native_status)
    try:
        status = TurnActivityStatus(value)
    except (TypeError, ValueError):
        status = lifecycle_status
    return TurnActivityEvent(item_id, kind, status, count=count)


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
