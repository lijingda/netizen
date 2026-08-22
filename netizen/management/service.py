"""Typed application boundary shared by Feishu and the Admin control plane."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, NoReturn, Protocol

from .blocking_io import BoundedBlockingIOExecutor
from .coordination import ScopeCoordinator
from ..bindings import (
    BindingCursor,
    BindingInventoryRecord,
    BindingPage,
    BindingQuery,
    BindingStore,
    BindingTurnSettings,
    ProjectAggregate,
    ProjectAggregatePage,
    ProjectDisabled as StoredProjectDisabled,
    ProjectNotFound as StoredProjectNotFound,
    ProjectRevisionConflict as StoredProjectRevisionConflict,
    ScopeRecord,
    SideTopicCursor,
    SideTopicConflict,
    SideTopicInventoryRecord,
    SideTopicPage,
    SideTopicQuery,
    SideTopicRecord,
    SideTopicState,
    ThreadBinding,
)
from ..codex_runtime import (
    BindingRuntimeSnapshot,
    CodexRuntime,
    NativeThreadCatalog,
    NativeThreadCatalogState,
    NativeThreadMetadata,
    ReleaseDisposition,
    SideLifecycleOutcome,
    SideSessionSnapshot,
    SideSessionNotFound,
    StopAcknowledger,
    StopDisposition,
    ThreadArchived,
    ThreadCatalogIdentityMissing,
)
from ..domain import FeishuScope
from ..projects import (
    Project,
    ProjectDisabled,
    ProjectRegistry,
    StaleProject,
    UnknownProject,
)


class ManagementError(RuntimeError):
    """Base class for stable application-boundary precondition failures."""


class NoCurrentBinding(ManagementError):
    pass


class CurrentBindingChanged(ManagementError):
    pass


class BindingScopeMismatch(ManagementError):
    pass


class ActivePointerChanged(ManagementError):
    pass


class SideIdentityMismatch(ManagementError):
    pass


class NativeThreadMissing(ManagementError):
    pass


class NativeCatalogInconsistent(ManagementError):
    pass


class RuntimeStateChanged(ManagementError):
    pass


class ChatLabelProvider(Protocol):
    async def get_chat_info(self, chat_id: str) -> Any: ...


@dataclass(frozen=True, slots=True)
class ChatLabel:
    chat_id: str
    display_name: str
    chat_type: str | None


@dataclass(frozen=True, slots=True)
class NativeThreadView:
    state: NativeThreadCatalogState
    metadata: NativeThreadMetadata | None


@dataclass(frozen=True, slots=True)
class SessionQuery:
    local: BindingQuery = BindingQuery()
    native_state: NativeThreadCatalogState | None = None


@dataclass(frozen=True, slots=True)
class SessionInventoryItem:
    record: BindingInventoryRecord
    native: NativeThreadView | None
    chat: ChatLabel


@dataclass(frozen=True, slots=True)
class SessionInventoryPage:
    items: tuple[SessionInventoryItem, ...]
    next_cursor: BindingCursor | None


@dataclass(frozen=True, slots=True)
class SideTopicInventoryItem:
    record: SideTopicInventoryRecord
    chat: ChatLabel
    runtime: SideSessionSnapshot | None


@dataclass(frozen=True, slots=True)
class SideTopicInventoryPage:
    items: tuple[SideTopicInventoryItem, ...]
    next_cursor: SideTopicCursor | None


@dataclass(frozen=True, slots=True)
class ProjectInventoryItem:
    aggregate: ProjectAggregate
    archived_binding_count: int


@dataclass(frozen=True, slots=True)
class ProjectInventoryPage:
    items: tuple[ProjectInventoryItem, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class RuntimeSnapshots:
    bindings: tuple[BindingRuntimeSnapshot, ...]
    sides: tuple[SideSessionSnapshot, ...]
    missing_side_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RuntimePrecondition:
    activity_revision: int
    physical_turn_id: str | None

    def __post_init__(self) -> None:
        if self.activity_revision < 0:
            raise ValueError("runtime activity revision must be non-negative")


@dataclass(frozen=True, slots=True)
class CurrentBindingTarget:
    scope_key: str
    binding_id: str

    def __post_init__(self) -> None:
        if not self.scope_key or not self.binding_id:
            raise ValueError("current Binding target identity must not be empty")


@dataclass(frozen=True, slots=True)
class ExactBindingTarget:
    scope_key: str
    binding_id: str
    expected_active_binding_id: str | None

    def __post_init__(self) -> None:
        if not self.scope_key or not self.binding_id:
            raise ValueError("exact Binding target identity must not be empty")


@dataclass(frozen=True, slots=True)
class CurrentSideTarget:
    side_id: str
    app_id: str
    chat_id: str
    topic_id: str | None
    root_message_id: str | None

    def __post_init__(self) -> None:
        if not self.side_id or not self.app_id or not self.chat_id:
            raise ValueError("Side target identity must not be empty")


@dataclass(frozen=True, slots=True)
class CreatedBinding:
    project: Project
    binding: ThreadBinding


@dataclass(frozen=True, slots=True)
class RenamedBinding:
    binding: ThreadBinding
    name: str


@dataclass(frozen=True, slots=True)
class ReleasedBinding:
    binding: ThreadBinding
    disposition: ReleaseDisposition


@dataclass(frozen=True, slots=True)
class StoppedBinding:
    binding: ThreadBinding
    disposition: StopDisposition


@dataclass(frozen=True, slots=True)
class ClosedSide:
    record: SideTopicRecord
    outcome: SideLifecycleOutcome | None
    missing_runtime_session: bool = False


class ManagementRuntimePort:
    """Concrete capability wrapper exposing only approved management seams.

    Keeping this as a real object, rather than merely a structural Protocol,
    prevents a future HTTP handler from accidentally receiving the complete
    Runtime and reaching Prompt, Turn, Goal, Compact, Side-create, or
    materialized-delete capabilities.
    """

    __slots__ = ("__runtime",)

    def __init__(self, runtime: CodexRuntime) -> None:
        self.__runtime = runtime

    async def configure_exact(
        self,
        *,
        binding_id: str,
        expected_revision: int,
        settings: BindingTurnSettings | None,
    ) -> ThreadBinding:
        return await self.__runtime.configure_exact(
            binding_id=binding_id,
            expected_revision=expected_revision,
            settings=settings,
        )

    async def activate_exact(self, binding_id: str) -> ThreadBinding:
        return await self.__runtime.activate_exact(binding_id)

    async def resolve_turn_settings(
        self,
        *,
        model_id: str,
        effort_id: str,
        service_tier_id: str,
    ) -> BindingTurnSettings:
        resolved = await self.__runtime.resolve_model_settings(
            model_id=model_id,
            effort_id=effort_id,
            service_tier_id=service_tier_id,
        )
        return BindingTurnSettings(
            model_id=resolved.model_id,
            effort_id=resolved.effort_id,
            service_tier_id=resolved.service_tier_id,
        )

    async def rename_exact(self, binding_id: str, name: str) -> str:
        return await self.__runtime.rename_exact(binding_id, name)

    async def archive_exact(self, binding_id: str) -> ThreadBinding:
        return await self.__runtime.archive_exact(binding_id)

    async def restore_exact(self, binding_id: str) -> ThreadBinding:
        return await self.__runtime.restore_exact(binding_id)

    async def restore_as_current_exact(self, binding_id: str) -> ThreadBinding:
        return await self.__runtime.restore_as_current_exact(binding_id)

    async def delete_lazy_exact(self, binding_id: str) -> ThreadBinding:
        return await self.__runtime.delete_lazy_exact(binding_id)

    async def stop_exact(
        self,
        binding_id: str,
        *,
        acknowledge: StopAcknowledger | None = None,
    ) -> StopDisposition:
        return await self.__runtime.stop_exact(binding_id, acknowledge=acknowledge)

    async def release_exact(self, binding_id: str) -> ReleaseDisposition:
        return await self.__runtime.release_exact(binding_id)

    async def close_side_exact(
        self,
        side_id: str,
        *,
        state: SideTopicState,
    ) -> SideLifecycleOutcome:
        return await self.__runtime.close_side_exact(side_id, state=state)

    async def binding_pointer_changed(
        self,
        previous_binding_id: str | None,
        current_binding_id: str | None,
    ) -> None:
        await self.__runtime.binding_pointer_changed(
            previous_binding_id,
            current_binding_id,
        )

    async def is_thread_archived(self, thread_id: str) -> bool:
        return await self.__runtime.thread_is_archived(thread_id)

    async def thread_catalog_state_exact(
        self,
        thread_id: str,
    ) -> NativeThreadCatalogState:
        return await self.__runtime.thread_catalog_state(thread_id)

    def runtime_snapshot_exact(self, binding_id: str) -> BindingRuntimeSnapshot:
        return self.__runtime.binding_runtime_snapshot(binding_id)

    def side_snapshot_exact(self, side_id: str) -> SideSessionSnapshot | None:
        try:
            return self.__runtime.side_snapshot(side_id)
        except SideSessionNotFound:
            return None

    async def thread_metadata_exact(
        self,
        thread_ids: tuple[str, ...],
        *,
        archived: bool,
        deadline: float,
    ) -> dict[str, NativeThreadMetadata]:
        return await self.__runtime.thread_metadata(
            thread_ids,
            archived=archived,
            deadline=deadline,
            max_pages=1_000,
            max_items=100_000,
        )

    async def thread_catalog_exact(
        self,
        *,
        archived: bool,
        deadline: float,
    ) -> NativeThreadCatalog:
        return await self.__runtime.thread_catalog(
            archived=archived,
            deadline=deadline,
        )


class InstanceManagementService:
    """Coordinate management mutations over the one Store and Runtime."""

    def __init__(
        self,
        *,
        bindings: BindingStore,
        projects: ProjectRegistry,
        runtime: ManagementRuntimePort,
        scope_coordinator: ScopeCoordinator,
        blocking_io: BoundedBlockingIOExecutor | None = None,
        chat_labels: ChatLabelProvider | None = None,
    ) -> None:
        self._bindings = bindings
        self._projects = projects
        self._runtime = runtime
        self._scope_coordinator = scope_coordinator
        self._chat_labels = chat_labels
        self._blocking_io = blocking_io or BoundedBlockingIOExecutor(
            max_workers=1,
            capacity=2,
        )

    @property
    def scope_coordinator(self) -> ScopeCoordinator:
        return self._scope_coordinator

    async def close(self, *, deadline: float | None = None) -> None:
        await self._blocking_io.aclose(deadline=deadline)

    async def register_project(
        self,
        *,
        alias: str,
        path: str | None,
        create_directory: bool,
        deadline: float | None = None,
    ) -> Project:
        return await self._blocking_io.submit(
            self._projects.register,
            alias=alias,
            path=path,
            create_directory=create_directory,
            deadline=deadline,
        )

    async def set_project_enabled(
        self,
        *,
        alias: str,
        enabled: bool,
        expected_revision: int,
    ) -> Project:
        return self._projects.set_enabled(
            alias=alias,
            enabled=enabled,
            expected_revision=expected_revision,
        )

    async def query_projects(
        self,
        *,
        cursor: str | None = None,
        limit: int = 25,
        deadline: float,
    ) -> ProjectInventoryPage:
        page = await self._bindings.query_project_aggregates(
            cursor=cursor,
            limit=limit,
            deadline_seconds=self._query_seconds(deadline),
        )
        archived = await self._runtime.thread_catalog_exact(
            archived=True,
            deadline=deadline,
        )
        project_by_thread = await self._bindings.project_aliases_for_native_threads(
            tuple(thread.thread_id for thread in archived.threads),
            deadline_seconds=self._query_seconds(deadline),
        )
        counts = Counter(project_by_thread.values())
        return ProjectInventoryPage(
            items=tuple(
                ProjectInventoryItem(
                    aggregate=item,
                    archived_binding_count=counts[item.project.alias],
                )
                for item in page.items
            ),
            next_cursor=page.next_cursor,
        )

    async def query_sessions(
        self,
        *,
        query: SessionQuery = SessionQuery(),
        cursor: BindingCursor | None = None,
        limit: int = 25,
        deadline: float,
    ) -> SessionInventoryPage:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError("page size must be between 1 and 50")
        if query.native_state is None:
            page = await self._bindings.query_bindings(
                query=query.local,
                cursor=cursor,
                limit=limit,
                deadline_seconds=self._query_seconds(deadline),
            )
            native = await self._targeted_native_views(page.items, deadline=deadline)
            return await self._session_page(
                page.items,
                native=native,
                next_cursor=page.next_cursor,
                deadline=deadline,
            )

        if not isinstance(query.native_state, NativeThreadCatalogState):
            raise ValueError("native Thread state filter is invalid")
        active, archived = await self._complete_native_views(deadline=deadline)
        native = {**active, **archived}
        selected: list[BindingInventoryRecord] = []
        scan_cursor = cursor
        next_cursor: BindingCursor | None = None
        exhausted = False
        while len(selected) < limit and not exhausted:
            page = await self._bindings.query_bindings(
                query=query.local,
                cursor=scan_cursor,
                limit=50,
                deadline_seconds=self._query_seconds(deadline),
            )
            if not page.items:
                break
            for index, record in enumerate(page.items):
                binding = record.binding
                scan_cursor = BindingCursor(binding.created_at, binding.id)
                view = self._native_view(binding, active=active, archived=archived)
                if view is None or view.state is not query.native_state:
                    continue
                selected.append(record)
                if len(selected) == limit:
                    has_more_local = (
                        index + 1 < len(page.items) or page.next_cursor is not None
                    )
                    next_cursor = scan_cursor if has_more_local else None
                    break
            else:
                if page.next_cursor is None:
                    exhausted = True
                else:
                    scan_cursor = page.next_cursor
                continue
            break

        return await self._session_page(
            tuple(selected),
            native=native,
            next_cursor=next_cursor,
            deadline=deadline,
        )

    async def query_side_topics(
        self,
        *,
        query: SideTopicQuery = SideTopicQuery(),
        cursor: SideTopicCursor | None = None,
        limit: int = 25,
        deadline: float,
    ) -> SideTopicInventoryPage:
        page = await self._bindings.query_side_topics(
            query=query,
            cursor=cursor,
            limit=limit,
            deadline_seconds=self._query_seconds(deadline),
        )
        labels = await self._chat_label_map(
            (item.side_topic.chat_id for item in page.items),
            deadline=deadline,
        )
        return SideTopicInventoryPage(
            items=tuple(
                SideTopicInventoryItem(
                    record=item,
                    chat=labels[item.side_topic.chat_id],
                    runtime=self._runtime.side_snapshot_exact(item.side_topic.id),
                )
                for item in page.items
            ),
            next_cursor=page.next_cursor,
        )

    def runtime_snapshots(
        self,
        *,
        binding_ids: Sequence[str] = (),
        side_ids: Sequence[str] = (),
    ) -> RuntimeSnapshots:
        binding_ids = self._validated_snapshot_ids(binding_ids, "Binding")
        side_ids = self._validated_snapshot_ids(side_ids, "Side")
        if len(binding_ids) + len(side_ids) > 50:
            raise ValueError("runtime snapshot request accepts at most 50 IDs")
        binding_snapshots = tuple(
            self._runtime.runtime_snapshot_exact(self._bindings.get(binding_id).id)
            for binding_id in binding_ids
        )
        sides: list[SideSessionSnapshot] = []
        missing: list[str] = []
        for side_id in side_ids:
            snapshot = self._runtime.side_snapshot_exact(side_id)
            if snapshot is None:
                missing.append(side_id)
            else:
                sides.append(snapshot)
        return RuntimeSnapshots(
            bindings=binding_snapshots,
            sides=tuple(sides),
            missing_side_ids=tuple(missing),
        )

    async def create_current_binding(
        self,
        *,
        scope: FeishuScope,
        creator_id: str,
        project_alias: str,
        expected_project_revision: int | None = None,
        turn_settings: BindingTurnSettings | None = None,
        deadline: float | None = None,
    ) -> CreatedBinding:
        project = await self._blocking_io.submit(
            self._projects.resolve_for_new,
            project_alias,
            expected_revision=expected_project_revision,
            deadline=deadline,
        )
        async with self._scope_coordinator.hold(scope.key):
            previous_id = self._active_id(scope.key)
            try:
                binding = self._bindings.create_channel_binding(
                    scope=scope,
                    project_alias=project.alias,
                    creator_id=creator_id,
                    expected_project_revision=expected_project_revision,
                    turn_settings=turn_settings,
                )
            except (
                StoredProjectNotFound,
                StoredProjectDisabled,
                StoredProjectRevisionConflict,
            ) as error:
                self._raise_project_create_error(project_alias, error)
            await self._runtime.binding_pointer_changed(previous_id, binding.id)
        return CreatedBinding(project=project, binding=binding)

    async def resolve_turn_settings(
        self,
        *,
        model_id: str,
        effort_id: str,
        service_tier_id: str,
    ) -> BindingTurnSettings:
        return await self._runtime.resolve_turn_settings(
            model_id=model_id,
            effort_id=effort_id,
            service_tier_id=service_tier_id,
        )

    async def create_exact_lazy_binding(
        self,
        *,
        scope_key: str,
        project_alias: str,
        expected_project_revision: int,
        expected_active_binding_id: str | None,
        activate: bool,
        turn_settings: BindingTurnSettings | None = None,
        deadline: float | None = None,
    ) -> CreatedBinding:
        project = await self._blocking_io.submit(
            self._projects.resolve_for_new,
            project_alias,
            expected_revision=expected_project_revision,
            deadline=deadline,
        )
        async with self._scope_coordinator.hold(scope_key):
            scope = self._bindings.get_scope(scope_key).scope
            previous_id = self._active_id(scope_key)
            if previous_id != expected_active_binding_id:
                raise ActivePointerChanged(
                    "Scope active Binding changed before Lazy creation"
                )
            try:
                binding = self._bindings.create_admin_binding(
                    scope=scope,
                    project_alias=project.alias,
                    expected_project_revision=expected_project_revision,
                    activate=activate,
                    turn_settings=turn_settings,
                )
            except (
                StoredProjectNotFound,
                StoredProjectDisabled,
                StoredProjectRevisionConflict,
            ) as error:
                self._raise_project_create_error(project_alias, error)
            if activate:
                await self._runtime.binding_pointer_changed(previous_id, binding.id)
        return CreatedBinding(project=project, binding=binding)

    async def resume_current_binding(
        self,
        *,
        scope_key: str,
        reference: str,
    ) -> ThreadBinding:
        async with self._scope_coordinator.hold(scope_key):
            previous_id = self._active_id(scope_key)
            binding = self._bindings.resolve_reference(
                scope_key=scope_key,
                reference=reference,
            )
            try:
                activated = await self._runtime.activate_exact(binding.id)
            except ThreadCatalogIdentityMissing as error:
                raise NativeThreadMissing(
                    "原生会话不在 active 或 archived catalog；本次未设为当前。"
                ) from error
            await self._runtime.binding_pointer_changed(previous_id, activated.id)
            return activated

    async def restore_current_binding(
        self,
        *,
        scope_key: str,
        reference: str,
    ) -> ThreadBinding:
        async with self._scope_coordinator.hold(scope_key):
            previous_id = self._active_id(scope_key)
            binding = self._bindings.resolve_reference(
                scope_key=scope_key,
                reference=reference,
            )
            restored = await self._runtime.restore_as_current_exact(binding.id)
            await self._runtime.binding_pointer_changed(previous_id, restored.id)
            return restored

    async def configure_current_binding(
        self,
        *,
        target: CurrentBindingTarget,
        expected_settings_revision: int,
        settings: BindingTurnSettings | None,
    ) -> ThreadBinding:
        async with self._scope_coordinator.hold(target.scope_key):
            binding = self._require_current(target)
            return await self._runtime.configure_exact(
                binding_id=binding.id,
                expected_revision=expected_settings_revision,
                settings=settings,
            )

    async def rename_current_binding(
        self,
        *,
        target: CurrentBindingTarget,
        name: str,
    ) -> RenamedBinding:
        async with self._scope_coordinator.hold(target.scope_key):
            binding = self._require_current(target)
            normalized = await self._runtime.rename_exact(binding.id, name)
            return RenamedBinding(
                binding=self._bindings.get(binding.id),
                name=normalized,
            )

    async def archive_current_binding(
        self,
        *,
        target: CurrentBindingTarget,
    ) -> ThreadBinding:
        async with self._scope_coordinator.hold(target.scope_key):
            binding = self._require_current(target)
            previous_id = binding.id
            archived = await self._runtime.archive_exact(binding.id)
            current_id = self._active_id(target.scope_key)
            await self._runtime.binding_pointer_changed(previous_id, current_id)
            return archived

    async def delete_current_lazy_binding(
        self,
        *,
        target: CurrentBindingTarget,
    ) -> ThreadBinding:
        async with self._scope_coordinator.hold(target.scope_key):
            binding = self._require_current(target)
            deleted = await self._runtime.delete_lazy_exact(binding.id)
            current_id = self._active_id(target.scope_key)
            await self._runtime.binding_pointer_changed(binding.id, current_id)
            return deleted

    async def release_current_binding(
        self,
        *,
        target: CurrentBindingTarget,
    ) -> ReleasedBinding:
        async with self._scope_coordinator.hold(target.scope_key):
            binding = self._require_current(target)
            disposition = await self._runtime.release_exact(binding.id)
            return ReleasedBinding(binding=binding, disposition=disposition)

    async def stop_current_binding(
        self,
        *,
        target: CurrentBindingTarget,
        acknowledge: StopAcknowledger | None = None,
    ) -> StoppedBinding:
        async with self._scope_coordinator.hold(target.scope_key):
            binding = self._require_current(target)
            disposition = await self._runtime.stop_exact(
                binding.id,
                acknowledge=acknowledge,
            )
            return StoppedBinding(binding=binding, disposition=disposition)

    async def activate_exact_binding(
        self,
        *,
        target: ExactBindingTarget,
    ) -> ThreadBinding:
        async with self._scope_coordinator.hold(target.scope_key):
            binding, previous_id = self._require_exact(target)
            try:
                activated = await self._runtime.activate_exact(binding.id)
            except ThreadCatalogIdentityMissing as error:
                raise NativeThreadMissing(
                    "原生会话不在 active 或 archived catalog；本次未设为当前。"
                ) from error
            await self._runtime.binding_pointer_changed(previous_id, activated.id)
            return activated

    async def configure_exact_binding(
        self,
        *,
        target: ExactBindingTarget,
        expected_settings_revision: int,
        settings: BindingTurnSettings | None,
    ) -> ThreadBinding:
        async with self._scope_coordinator.hold(target.scope_key):
            binding, _ = self._require_exact(target)
            return await self._runtime.configure_exact(
                binding_id=binding.id,
                expected_revision=expected_settings_revision,
                settings=settings,
            )

    async def rename_exact_binding(
        self,
        *,
        target: ExactBindingTarget,
        name: str,
    ) -> RenamedBinding:
        async with self._scope_coordinator.hold(target.scope_key):
            binding, _ = self._require_exact(target)
            normalized = await self._runtime.rename_exact(binding.id, name)
            return RenamedBinding(self._bindings.get(binding.id), normalized)

    async def archive_exact_binding(
        self,
        *,
        target: ExactBindingTarget,
    ) -> ThreadBinding:
        async with self._scope_coordinator.hold(target.scope_key):
            binding, previous_id = self._require_exact(target)
            archived = await self._runtime.archive_exact(binding.id)
            current_id = self._active_id(target.scope_key)
            await self._runtime.binding_pointer_changed(previous_id, current_id)
            return archived

    async def restore_exact_binding(
        self,
        *,
        target: ExactBindingTarget,
    ) -> ThreadBinding:
        async with self._scope_coordinator.hold(target.scope_key):
            binding, _ = self._require_exact(target)
            return await self._runtime.restore_exact(binding.id)

    async def restore_exact_binding_as_current(
        self,
        *,
        target: ExactBindingTarget,
    ) -> ThreadBinding:
        async with self._scope_coordinator.hold(target.scope_key):
            binding, previous_id = self._require_exact(target)
            restored = await self._runtime.restore_as_current_exact(binding.id)
            await self._runtime.binding_pointer_changed(previous_id, restored.id)
            return restored

    async def delete_exact_lazy_binding(
        self,
        *,
        target: ExactBindingTarget,
    ) -> ThreadBinding:
        async with self._scope_coordinator.hold(target.scope_key):
            binding, previous_id = self._require_exact(target)
            deleted = await self._runtime.delete_lazy_exact(binding.id)
            current_id = self._active_id(target.scope_key)
            await self._runtime.binding_pointer_changed(previous_id, current_id)
            return deleted

    async def release_exact_binding(
        self,
        *,
        target: ExactBindingTarget,
        runtime_precondition: RuntimePrecondition | None = None,
    ) -> ReleasedBinding:
        async with self._scope_coordinator.hold(target.scope_key):
            binding, _ = self._require_exact(target)
            self._require_runtime_precondition(binding.id, runtime_precondition)
            disposition = await self._runtime.release_exact(binding.id)
            return ReleasedBinding(binding, disposition)

    async def stop_exact_binding(
        self,
        *,
        target: ExactBindingTarget,
        acknowledge: StopAcknowledger | None = None,
        runtime_precondition: RuntimePrecondition | None = None,
    ) -> StoppedBinding:
        async with self._scope_coordinator.hold(target.scope_key):
            binding, _ = self._require_exact(target)
            self._require_runtime_precondition(binding.id, runtime_precondition)
            disposition = await self._runtime.stop_exact(
                binding.id,
                acknowledge=acknowledge,
            )
            return StoppedBinding(binding, disposition)

    async def close_side(
        self,
        *,
        target: CurrentSideTarget,
    ) -> ClosedSide:
        record = self._bindings.get_side_topic(target.side_id)
        self._require_side_identity(record, target)
        if record.state.terminal:
            return ClosedSide(record=record, outcome=None)
        state = (
            SideTopicState.FAILED
            if record.state is SideTopicState.CREATING
            else SideTopicState.CLOSED
        )
        try:
            outcome = await self._runtime.close_side_exact(record.id, state=state)
        except SideSessionNotFound:
            current = self._bindings.get_side_topic(record.id)
            self._require_side_identity(current, target)
            if current.state is SideTopicState.OPEN:
                terminal = SideTopicState.EXPIRED
            elif current.state is SideTopicState.CREATING:
                terminal = SideTopicState.FAILED
            else:
                return ClosedSide(
                    record=current,
                    outcome=None,
                    missing_runtime_session=True,
                )
            try:
                current = self._bindings.transition_side_topic(current.id, terminal)
            except SideTopicConflict:
                current = self._bindings.get_side_topic(current.id)
            return ClosedSide(
                record=current,
                outcome=None,
                missing_runtime_session=True,
            )
        return ClosedSide(
            record=self._bindings.get_side_topic(record.id),
            outcome=outcome,
        )

    async def _targeted_native_views(
        self,
        records: Sequence[BindingInventoryRecord],
        *,
        deadline: float,
    ) -> dict[str, NativeThreadView]:
        thread_ids = tuple(
            dict.fromkeys(
                record.binding.native_thread_id
                for record in records
                if record.binding.native_thread_id is not None
            )
        )
        if not thread_ids:
            return {}
        active = await self._runtime.thread_metadata_exact(
            thread_ids,
            archived=False,
            deadline=deadline,
        )
        archived = await self._runtime.thread_metadata_exact(
            thread_ids,
            archived=True,
            deadline=deadline,
        )
        self._require_disjoint_native_catalogs(active, archived)
        return {
            thread_id: (
                NativeThreadView(NativeThreadCatalogState.ACTIVE, active[thread_id])
                if thread_id in active
                else NativeThreadView(
                    NativeThreadCatalogState.ARCHIVED,
                    archived[thread_id],
                )
                if thread_id in archived
                else NativeThreadView(NativeThreadCatalogState.MISSING, None)
            )
            for thread_id in thread_ids
        }

    async def _complete_native_views(
        self,
        *,
        deadline: float,
    ) -> tuple[dict[str, NativeThreadView], dict[str, NativeThreadView]]:
        active_catalog = await self._runtime.thread_catalog_exact(
            archived=False,
            deadline=deadline,
        )
        archived_catalog = await self._runtime.thread_catalog_exact(
            archived=True,
            deadline=deadline,
        )
        active_metadata = active_catalog.by_id()
        archived_metadata = archived_catalog.by_id()
        self._require_disjoint_native_catalogs(active_metadata, archived_metadata)
        return (
            {
                thread_id: NativeThreadView(
                    NativeThreadCatalogState.ACTIVE,
                    metadata,
                )
                for thread_id, metadata in active_metadata.items()
            },
            {
                thread_id: NativeThreadView(
                    NativeThreadCatalogState.ARCHIVED,
                    metadata,
                )
                for thread_id, metadata in archived_metadata.items()
            },
        )

    @staticmethod
    def _native_view(
        binding: ThreadBinding,
        *,
        active: dict[str, NativeThreadView],
        archived: dict[str, NativeThreadView],
    ) -> NativeThreadView | None:
        thread_id = binding.native_thread_id
        if thread_id is None:
            return None
        return active.get(thread_id) or archived.get(thread_id) or NativeThreadView(
            NativeThreadCatalogState.MISSING,
            None,
        )

    async def _session_page(
        self,
        records: Sequence[BindingInventoryRecord],
        *,
        native: dict[str, NativeThreadView],
        next_cursor: BindingCursor | None,
        deadline: float,
    ) -> SessionInventoryPage:
        labels = await self._chat_label_map(
            (record.scope.chat_id for record in records),
            deadline=deadline,
        )
        items: list[SessionInventoryItem] = []
        for record in records:
            thread_id = record.binding.native_thread_id
            view = None
            if thread_id is not None:
                view = native.get(thread_id) or NativeThreadView(
                    NativeThreadCatalogState.MISSING,
                    None,
                )
            items.append(
                SessionInventoryItem(
                    record=record,
                    native=view,
                    chat=labels[record.scope.chat_id],
                )
            )
        return SessionInventoryPage(tuple(items), next_cursor)

    async def _chat_label_map(
        self,
        chat_ids: Iterable[str],
        *,
        deadline: float,
    ) -> dict[str, ChatLabel]:
        unique = tuple(dict.fromkeys(chat_ids))
        labels = {
            chat_id: ChatLabel(chat_id, chat_id, None) for chat_id in unique
        }
        provider = self._chat_labels
        if provider is None or not unique:
            return labels

        async def fetch(chat_id: str) -> tuple[str, Any]:
            try:
                return chat_id, await provider.get_chat_info(chat_id)
            except Exception:
                return chat_id, None

        tasks = tuple(asyncio.create_task(fetch(chat_id)) for chat_id in unique)
        timeout = max(
            0.0,
            min(0.5, deadline - asyncio.get_running_loop().time()),
        )
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            chat_id, info = task.result()
            if info is None:
                continue
            raw_name = getattr(info, "name", None)
            name = raw_name.strip() if isinstance(raw_name, str) else ""
            raw_type = getattr(info, "chat_type", None)
            chat_type = str(raw_type) if raw_type else None
            labels[chat_id] = ChatLabel(
                chat_id=chat_id,
                display_name=name or chat_id,
                chat_type=chat_type,
            )
        return labels

    @staticmethod
    def _require_disjoint_native_catalogs(
        active: dict[str, object],
        archived: dict[str, object],
    ) -> None:
        overlap = active.keys() & archived.keys()
        if overlap:
            raise NativeCatalogInconsistent(
                "原生会话同时出现在 active 和 archived catalog；本次查询已取消。"
            )

    @staticmethod
    def _query_seconds(deadline: float) -> float:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("management query deadline elapsed")
        return min(0.5, remaining)

    @staticmethod
    def _validated_snapshot_ids(
        values: Sequence[str],
        label: str,
    ) -> tuple[str, ...]:
        result = tuple(values)
        if len(set(result)) != len(result):
            raise ValueError(f"duplicate {label} snapshot ID")
        if any(
            not isinstance(value, str) or not value or value.strip() != value
            for value in result
        ):
            raise ValueError(f"{label} snapshot IDs must be non-empty exact strings")
        return result

    @staticmethod
    def _raise_project_create_error(alias: str, error: Exception) -> NoReturn:
        if isinstance(error, StoredProjectNotFound):
            raise UnknownProject(alias) from error
        if isinstance(error, StoredProjectDisabled):
            raise ProjectDisabled(f"Project {alias} 已停用，不能创建新会话。") from error
        if isinstance(error, StoredProjectRevisionConflict):
            raise StaleProject(
                f"Project {alias} 已被其他操作修改，请刷新后重试。"
            ) from error
        raise AssertionError("unexpected Project create error") from error

    def _require_current(self, target: CurrentBindingTarget) -> ThreadBinding:
        binding = self._bindings.active_binding(target.scope_key)
        if binding is None:
            raise NoCurrentBinding(target.scope_key)
        if binding.id != target.binding_id:
            raise CurrentBindingChanged(target.binding_id)
        return binding

    def _require_runtime_precondition(
        self,
        binding_id: str,
        expected: RuntimePrecondition | None,
    ) -> None:
        if expected is None:
            return
        snapshot = self._runtime.runtime_snapshot_exact(binding_id)
        physical_turn_id = snapshot.turn.turn_id if snapshot.turn is not None else None
        if (
            snapshot.activity_revision != expected.activity_revision
            or physical_turn_id != expected.physical_turn_id
        ):
            raise RuntimeStateChanged(binding_id)

    def _require_exact(
        self,
        target: ExactBindingTarget,
    ) -> tuple[ThreadBinding, str | None]:
        current_id = self._active_id(target.scope_key)
        if current_id != target.expected_active_binding_id:
            raise ActivePointerChanged(target.scope_key)
        binding = self._bindings.get(target.binding_id)
        if binding.scope_key != target.scope_key:
            raise BindingScopeMismatch(target.binding_id)
        return binding, current_id

    def _active_id(self, scope_key: str) -> str | None:
        active = self._bindings.active_binding(scope_key)
        return active.id if active is not None else None

    @staticmethod
    def _require_side_identity(
        record: SideTopicRecord,
        target: CurrentSideTarget,
    ) -> None:
        if (
            record.app_id != target.app_id
            or record.chat_id != target.chat_id
            or record.topic_id != target.topic_id
            or record.root_message_id != target.root_message_id
        ):
            raise SideIdentityMismatch(target.side_id)
