"""Narrow read and naming providers consumed by session management."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .bindings import BindingCursor, ThreadBinding
from .ordinary_sessions import LocalExecutionSnapshot, LocalSessionManagement, LocalStatusSnapshot
from .runtime.codex_contracts import BindingRuntimeSnapshot
from .session_views import SessionInventoryPage, SessionObservations, SessionQuery
from .session_inventory import NativeSessionCursor
from .runtime.contracts import NativeThreadCatalogState, NativeThreadMetadata


@runtime_checkable
class _NativeTitleWrite(Protocol):
    """Validate materialization and lifecycle at the exact Binding's native write boundary."""

    async def rename_exact(self, binding_id: str, name: str) -> str: ...


class ManualTitleWriter(_NativeTitleWrite, Protocol):
    @property
    def sync_hint(self) -> str | None: ...

    def eligible(self, binding: ThreadBinding, catalog_state: NativeThreadCatalogState | None) -> bool: ...


@dataclass(frozen=True, slots=True)
class _ExactTitleWriter:
    sync_hint = "保存后名称同步显示在 Codex App/CLI。"
    write: Callable[[str, str], Awaitable[str]]

    def eligible(self, binding: ThreadBinding, catalog_state: NativeThreadCatalogState | None) -> bool:
        return binding.native_thread_id is not None and catalog_state is NativeThreadCatalogState.ACTIVE

    async def rename_exact(self, binding_id: str, name: str) -> str:
        return await self.write(binding_id, name)


@runtime_checkable
class _CandidateSource(Protocol):
    @property
    def name_generator(self) -> Callable[[str], Awaitable[str]] | None: ...


@dataclass(frozen=True, slots=True)
class SessionNames:
    writer: ManualTitleWriter | None = None
    generate: Callable[[str], Awaitable[str]] | None = None

    @property
    def sync_hint(self) -> str:
        hint = self.writer.sync_hint if self.writer is not None else None
        return hint or "保存后才会改名，新名称将同步到原生会话。"

    @classmethod
    def from_runtime(cls, runtime: object) -> "SessionNames":
        # Keep the native methods responsible for their exact identity and locking.
        writer = None
        if isinstance(runtime, _NativeTitleWrite):
            native_writer = runtime
            writer = _ExactTitleWriter(
                lambda binding_id, name: native_writer.rename_exact(binding_id, name)
            )
        return cls(writer, runtime.name_generator if isinstance(runtime, _CandidateSource) else None)


@dataclass(frozen=True, slots=True)
class StatusCapture:
    snapshot: BindingRuntimeSnapshot
    local: LocalExecutionSnapshot | None = None
    observations: SessionObservations | None = None


class StatusSource(Protocol):
    def capture(self, binding: ThreadBinding, snapshot: BindingRuntimeSnapshot | None = None) -> StatusCapture: ...


@dataclass(frozen=True, slots=True)
class CodexStatusSource:
    snapshot: Callable[[str], BindingRuntimeSnapshot]

    def capture(self, binding: ThreadBinding, snapshot: BindingRuntimeSnapshot | None = None) -> StatusCapture:
        return StatusCapture(snapshot or self.snapshot(binding.id))


@dataclass(frozen=True, slots=True)
class LocalStatusSource:
    runtime: LocalSessionManagement

    def capture(self, binding: ThreadBinding, snapshot: BindingRuntimeSnapshot | None = None) -> StatusCapture:
        if snapshot is None:
            snapshot = self.runtime.capture_status(binding.id)
        if (isinstance(snapshot, LocalStatusSnapshot) and snapshot.session_id == binding.native_thread_id
                and snapshot.activity_revision == snapshot.local.revision and snapshot.turn == snapshot.local.turn):
            return StatusCapture(snapshot, snapshot.local, snapshot.observations)
        local = LocalExecutionSnapshot(snapshot.activity_revision, snapshot.turn, unavailable=False)
        return StatusCapture(snapshot, local, self.runtime.observation_defaults)


@dataclass(frozen=True, slots=True)
class SessionListReads:
    native_catalog: bool
    page: Callable[[SessionQuery, BindingCursor | NativeSessionCursor | None, int, float], Awaitable[SessionInventoryPage]]
    metadata: Callable[[str, float], Awaitable[NativeThreadMetadata | None]]
