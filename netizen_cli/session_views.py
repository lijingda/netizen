"""Immutable display values. Observations never participate in session admission."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
import time
from typing import TYPE_CHECKING, Literal

from .bindings import BindingCursor, BindingInventoryRecord, BindingQuery, ThreadBinding
from .runtime.contracts import NativeThreadCatalogState, NativeThreadMetadata
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

    def display(self) -> str:
        if self.availability is not Availability.OBSERVED:
            return self.availability.value
        assert self.sampled_at is not None
        stamp = datetime.fromtimestamp(self.sampled_at, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        return f"{self.value}（{self.source} · {stamp}）"

    def json(self) -> dict[str, object]:
        return {"availability": self.availability.value, "source": self.source,
                "value": self.value, "sampledAt": self.sampled_at}


@dataclass(frozen=True, slots=True)
class SessionObservations:
    fields: tuple[tuple[str, SessionField], ...] = ()

    def get(self, name: str, source: str) -> SessionField:
        return next((value for key, value in self.fields if key == name),
                    SessionField(Availability.UNOBSERVED, source))

    def json(self) -> dict[str, object]:
        result = {name: self.get(name, source).json() for source, names in (
            ("Claude init", ("model", "cwd")),
            ("Claude Result", ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "totalCostUsd")),
            ("Claude rate_limit", ("limitStatus", "limitWindow", "limitReset", "limitUtilization")),
        ) for name in names}
        result["context"] = SessionField(Availability.UNAVAILABLE, "Claude context").json()
        result["subscription"] = SessionField(Availability.NOT_APPLICABLE, "ordinary session").json()
        return dict(result)


@dataclass(frozen=True, slots=True)
class SessionStatusView:
    """Text sections at one display capture, including the Codex v1 snapshot projection."""
    short_id: str
    project_alias: str
    native_thread_id: str | None
    state: str
    metadata_lines: tuple[str, str]
    git_branch_lines: tuple[str, ...]
    context_window_line: str
    context_mode_lines: tuple[str, ...]
    model_lines: tuple[str, str, str, str]
    progress_lines: tuple[str, ...]
    subscription_line: str
    source: str = "Codex status snapshot"
    sampled_at: float = field(default_factory=time.time)

    def callback_snapshot(self) -> dict[str, object]:
        return {key: getattr(self, key) for key in (
            "short_id", "project_alias", "native_thread_id", "state", "metadata_lines",
            "git_branch_lines", "context_window_line", "context_mode_lines", "model_lines",
            "progress_lines", "subscription_line",
        )}


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
