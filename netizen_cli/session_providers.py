"""Narrow read and naming providers consumed by session management."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .bindings import BindingCursor, ThreadBinding
from .ordinary_sessions import LocalExecutionSnapshot, LocalSessionManagement
from .runtime.codex_contracts import BindingRuntimeSnapshot
from .session_views import SessionInventoryPage, SessionObservations, SessionQuery
from .session_inventory import NativeSessionCursor
from .runtime.contracts import NativeThreadMetadata


@runtime_checkable
class ManualTitleWriter(Protocol):
    """Validate materialization and lifecycle at the exact Binding's native write boundary."""

    async def rename_exact(self, binding_id: str, name: str) -> str: ...


@dataclass(frozen=True, slots=True)
class _ExactTitleWriter:
    write: Callable[[str, str], Awaitable[str]]

    async def rename_exact(self, binding_id: str, name: str) -> str:
        return await self.write(binding_id, name)


@runtime_checkable
class _CandidateSource(Protocol):
    async def generate_name_exact(self, binding_id: str) -> str: ...


@dataclass(frozen=True, slots=True)
class SessionNames:
    writer: ManualTitleWriter | None = None
    generate: Callable[[str], Awaitable[str]] | None = None

    @classmethod
    def from_runtime(cls, runtime: object) -> "SessionNames":
        # Keep the native methods responsible for their exact identity and locking.
        writer = None
        if isinstance(runtime, ManualTitleWriter):
            native_writer = runtime
            writer = _ExactTitleWriter(
                lambda binding_id, name: native_writer.rename_exact(binding_id, name)
            )
        if isinstance(runtime, _CandidateSource):
            return cls(
                writer, lambda binding_id: runtime.generate_name_exact(binding_id)
            )
        return cls(writer)


@dataclass(frozen=True, slots=True)
class StatusCapture:
    snapshot: BindingRuntimeSnapshot
    local: LocalExecutionSnapshot | None = None
    observations: SessionObservations = SessionObservations()


class StatusSource(Protocol):
    def capture(self, binding: ThreadBinding) -> StatusCapture: ...


@dataclass(frozen=True, slots=True)
class CodexStatusSource:
    snapshot: Callable[[str], BindingRuntimeSnapshot]

    def capture(self, binding: ThreadBinding) -> StatusCapture:
        return StatusCapture(self.snapshot(binding.id))


@dataclass(frozen=True, slots=True)
class LocalStatusSource:
    runtime: LocalSessionManagement
    read_samples: Callable[[str, str | None], SessionObservations]

    def capture(self, binding: ThreadBinding) -> StatusCapture:
        local = self.runtime.local_snapshot(binding.id)
        snapshot = BindingRuntimeSnapshot(binding.id, local.revision, local.turn, None, False, None, None, None)
        return StatusCapture(snapshot, local, self.read_samples(binding.id, binding.native_thread_id))


@dataclass(frozen=True, slots=True)
class SessionListReads:
    native_catalog: bool
    page: Callable[[SessionQuery, BindingCursor | NativeSessionCursor | None, int, float], Awaitable[SessionInventoryPage]]
    metadata: Callable[[str, float], Awaitable[NativeThreadMetadata | None]]
