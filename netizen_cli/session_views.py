"""Immutable display values. Observations never participate in session admission."""
from __future__ import annotations

from dataclasses import dataclass
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
