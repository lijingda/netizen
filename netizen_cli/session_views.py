"""Immutable display values. Observations never participate in session admission."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Literal

from .bindings import BindingCursor, BindingInventoryRecord, BindingQuery, ThreadBinding
from .runtime.contracts import ContextWindowUsage, NativeThreadCatalogState, NativeThreadMetadata
from .session_inventory import NativeSessionCursor as SessionNativeCursor

if TYPE_CHECKING:
    from .management.chat_labels import ChatLabel


class Availability(str, Enum):
    OBSERVED = "observed"
    NOT_APPLICABLE = "不适用"
    UNOBSERVED = "未观测"
    UNAVAILABLE = "取不到"


@dataclass(frozen=True, slots=True)
class SessionField:
    availability: Availability
    source: str
    value: str | int | float | None = None
    sampled_at: float | None = None
    execution_id: str | None = None

    def display(self) -> str:
        if self.availability is not Availability.OBSERVED:
            return self.availability.value
        if self.sampled_at is None:
            return str(self.value)
        stamp = datetime.fromtimestamp(self.sampled_at, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        return f"{self.value}（{self.source} · {stamp}）"

    def json(self) -> dict[str, object]:
        return {"availability": self.availability.value, "source": self.source,
                "value": self.value, "sampledAt": self.sampled_at,
                **({"executionId": self.execution_id} if self.execution_id is not None else {})}


@dataclass(frozen=True, slots=True)
class ObservationPresentation:
    provider: str = ""
    identity_label: str = "原生会话 ID"
    workspace: tuple[tuple[str, str], ...] = ()
    model_rows: tuple[tuple[tuple[str, str], ...], ...] = ()
    usage: tuple[tuple[str, str], ...] = ()
    details: tuple[tuple[str, str], ...] = ()
    row: tuple[tuple[str, str], ...] = ()
    admin: tuple[tuple[str, str], ...] | None = None


@dataclass(frozen=True, slots=True)
class SessionObservations:
    fields: tuple[tuple[str, SessionField], ...] = ()
    defaults: tuple[tuple[str, SessionField], ...] = ()
    presentation: ObservationPresentation = ObservationPresentation()

    def lines(self, labels: tuple[tuple[str, str], ...]) -> tuple[str, ...]:
        fields = dict((*self.defaults, *self.fields))
        return tuple(f"{label}：{fields[key].display()}" for key, label in labels if key in fields)

    def get(self, name: str, source: str = "") -> SessionField:
        for key, value in (*self.fields, *self.defaults):
            if key == name:
                return value
        return SessionField(Availability.UNOBSERVED, source)

    def json(self) -> dict[str, object]:
        labels = dict(self.presentation.admin) if self.presentation.admin is not None else None
        return {key: {**value.json(), "label": key if labels is None else labels.get(key)}
                for key, value in (*self.defaults, *self.fields)}


@dataclass(frozen=True, slots=True)
class ContextUsageField:
    availability: Availability
    source: str
    sampled_at: float | None
    usage: ContextWindowUsage | None = None
    previous_turn: bool = False


@dataclass(frozen=True, slots=True)
class ModelStatusFields:
    model: SessionField
    effort: SessionField
    speed: SessionField
    configuration: SessionField


@dataclass(frozen=True, slots=True)
class SessionStatusView:
    """All ordinary status cards present these rows, including callback redraws."""

    project_alias: str
    state: str
    state_label: str
    metadata_lines: tuple[str, str]
    workspace_lines: tuple[str, ...]
    model_rows: tuple[tuple[str, ...], ...]
    context_lines: tuple[str, ...]
    progress_lines: tuple[str, ...]
    identity_lines: tuple[str, str]
    subscription_lines: tuple[str, ...]

    @classmethod
    def from_observations(
        cls, binding: ThreadBinding, state: str, label: str, observed: SessionObservations, *, title: str,
    ) -> "SessionStatusView":
        layout = observed.presentation
        return cls(
            project_alias=binding.project_alias, state=state, state_label=label,
            metadata_lines=(f"{layout.provider} · {title}", f"本地任务：{label}"), workspace_lines=observed.lines(layout.workspace),
            model_rows=tuple(lines for group in layout.model_rows if (lines := observed.lines(group))),
            context_lines=(*observed.lines(layout.usage), f"回复消息类型：{binding.task_feedback.reply_type_label}"), progress_lines=(),
            identity_lines=(f"Netizen 会话编号（/resume）：{binding.short_id}",
                            f"{layout.identity_label}：{binding.native_thread_id or '尚未建立'}"),
            subscription_lines=observed.lines(layout.details),
        )


@dataclass(frozen=True, slots=True)
class NativeThreadView:
    # None means the management catalog could not classify a materialized Thread.
    # An absent NativeThreadView, rather than an absent state, represents Lazy.
    state: NativeThreadCatalogState | None
    metadata: NativeThreadMetadata | None


class SessionInventoryState(str, Enum):
    ACTIVE = "active"
    LAZY = "lazy"
    ARCHIVED = "archived"
    MISSING = "missing"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SessionQuery:
    search_term: str | None = None
    local: BindingQuery = BindingQuery()
    inventory_state: SessionInventoryState = SessionInventoryState.ACTIVE
    sort_by: Literal["created_at", "updated_at"] = "updated_at"
    sort_direction: Literal["asc", "desc"] = "desc"
    # Native Unix seconds, independent of the Binding's registration time.
    created_from: float | None = None
    created_before: float | None = None
    updated_from: float | None = None
    updated_before: float | None = None


def _session_inventory_state(
    binding: ThreadBinding, native: NativeThreadView | None,
) -> SessionInventoryState:
    if binding.native_thread_id is None:
        return SessionInventoryState.LAZY
    if native is None or native.state is None:
        return SessionInventoryState.UNKNOWN
    return SessionInventoryState(native.state.value)


@dataclass(frozen=True, slots=True)
class SessionInventoryItem:
    record: BindingInventoryRecord
    native: NativeThreadView | None
    chat: ChatLabel

    @property
    def inventory_state(self) -> SessionInventoryState:
        return _session_inventory_state(self.record.binding, self.native)


@dataclass(frozen=True, slots=True)
class SessionInventoryPage:
    items: tuple[SessionInventoryItem, ...]
    next_cursor: BindingCursor | SessionNativeCursor | None


@dataclass(frozen=True, slots=True)
class ScopeSessionPage:
    items: tuple[SessionInventoryItem, ...]
    next_cursor: BindingCursor | SessionNativeCursor | None
    current: SessionInventoryItem | None
    active_binding_id: str | None
    has_lazy: bool
    previous_cursor: BindingCursor | None = None
