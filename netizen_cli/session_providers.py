"""Narrow providers for ordinary session operations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .bindings import BindingCursor, BindingStore, ThreadBinding
from .runtime.codex_contracts import BindingRuntimeSnapshot
from .session_views import SessionInventoryPage, SessionQuery
from .session_inventory import NativeSessionCursor
from .runtime.contracts import NativeThreadCatalogState, NativeThreadMetadata

if TYPE_CHECKING:
    from .codex_runtime import CodexRuntime


@dataclass(frozen=True, slots=True)
class PersistentFork:
    child_id: str
    source: ThreadBinding
    adopt: Callable[[ThreadBinding], Awaitable[bool]]
    release_unbound: Callable[[], Awaitable[bool]]


class PersistentForkProvider(Protocol):
    def track_fork_creation(self, project_alias: str) -> AbstractAsyncContextManager[None]: ...
    def require_fork_creation_open(self, project_alias: str) -> None: ...
    def project_has_fork_creation(self, project_alias: str) -> bool: ...
    async def copy_fork(self, source: ThreadBinding, *, expected_project_revision: int,
                        title: str) -> PersistentFork: ...


@dataclass(frozen=True, slots=True)
class CodexPersistentForks:
    runtime: CodexRuntime
    bindings: BindingStore

    def track_fork_creation(self, project_alias: str) -> AbstractAsyncContextManager[None]:
        return self.runtime.track_fork_creation(project_alias)

    def require_fork_creation_open(self, project_alias: str) -> None:
        self.runtime.require_fork_creation_open(project_alias)

    def project_has_fork_creation(self, project_alias: str) -> bool:
        return self.runtime.project_has_fork_creation(project_alias)

    async def copy_fork(self, source: ThreadBinding, *, expected_project_revision: int,
                        title: str) -> PersistentFork:
        current = self.bindings.get(source.id)
        cwd = await self.runtime.resolve_binding_cwd(
            current.id, default_cwd=Path(self.bindings.get_project(current.project_alias).cwd))
        project = self.bindings.get_project(source.project_alias)
        worktree = self.bindings.worktrees.find_by_cwd(str(cwd), project_alias=project.alias,
                                                       project_generation=project.generation)
        source = replace(source, working_cwd=str(cwd), worktree_id=worktree.id if worktree else None)
        thread = await self.runtime.fork_exact(source, expected_project_revision=expected_project_revision)
        return PersistentFork(thread.id, source, adopt=lambda binding: self.runtime.adopt_fork(binding, thread, name=title),
                              release_unbound=lambda: self.runtime.release_unbound_fork(thread))


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


class StatusSource(Protocol):
    def capture(self, binding: ThreadBinding, snapshot: BindingRuntimeSnapshot | None = None) -> StatusCapture: ...


@dataclass(frozen=True, slots=True)
class CodexStatusSource:
    snapshot: Callable[[str], BindingRuntimeSnapshot]

    def capture(self, binding: ThreadBinding, snapshot: BindingRuntimeSnapshot | None = None) -> StatusCapture:
        return StatusCapture(snapshot or self.snapshot(binding.id))


@dataclass(frozen=True, slots=True)
class SessionListReads:
    page: Callable[[SessionQuery, BindingCursor | NativeSessionCursor | None, int, float], Awaitable[SessionInventoryPage]]
    metadata: Callable[[str, float], Awaitable[NativeThreadMetadata | None]]
