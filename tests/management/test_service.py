from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from netizen.bindings import BindingQuery, BindingStore, SideTopicState
from netizen.channel_app import ChannelApplication
from netizen.codex_runtime import (
    ActiveGoalSnapshot,
    ActiveTurnSnapshot,
    BindingRuntimeSnapshot,
    CodexRuntime,
    NativeThreadCatalogState,
    NativeThreadCatalog,
    NativeThreadMetadata,
    ReleaseDisposition,
    SideLifecycleOutcome,
    SideSessionNotFound,
    StopDisposition,
    ThreadLifecycleSnapshot,
    ThreadLifecycleState,
    ThreadLifecycleError,
    ThreadSubscriptionSnapshot,
    ThreadSubscriptionState,
    ThreadArchived,
    ThreadCatalogError,
    ThreadCatalogIdentityMissing,
    ThreadDeleteTargetChanged,
)
from netizen.domain import (
    ActiveState,
    FeishuScope,
    GoalOperationState,
    GoalStatus,
    NativeCapability,
    ScopeKind,
)
from netizen.management import (
    ActivePointerChanged,
    BindingScopeMismatch,
    CurrentBindingChanged,
    CurrentBindingTarget,
    CurrentSideTarget,
    ExactBindingTarget,
    InstanceManagementService,
    ManagementRuntimePort,
    NativeThreadMissing,
    RuntimePrecondition,
    RuntimeStateChanged,
    ScopeCoordinator,
    SessionInventoryState,
    SessionQuery,
    SideIdentityMismatch,
    classify_native_thread_view,
)
from netizen.projects import ProjectRegistry
from netizen.sdk_gap_adapter import GoalControlError, GoalSnapshot
from netizen.management.service import _project_binding_status


class FakeManagementRuntime:
    def __init__(self, store: BindingStore) -> None:
        self.native_delete_available = True
        self.store = store
        self.calls: list[tuple[object, ...]] = []
        self.archived: set[str] = set()
        self.missing: set[str] = set()
        self.rename_entered: asyncio.Event | None = None
        self.rename_release: asyncio.Event | None = None
        self.archive_entered: asyncio.Event | None = None
        self.archive_release: asyncio.Event | None = None
        self.side_missing = False
        self.active_metadata: dict[str, NativeThreadMetadata] = {}
        self.archived_metadata: dict[str, NativeThreadMetadata] = {}
        self.summary_metadata: dict[str, NativeThreadMetadata] = {}
        self.side_snapshots: dict[str, object] = {}
        self.runtime_snapshots: dict[str, BindingRuntimeSnapshot] = {}
        self.goal_snapshots: dict[str, GoalSnapshot | None] = {}
        self.goal_snapshot_after: dict[str, BindingRuntimeSnapshot] = {}
        self.goal_snapshot_errors: set[str] = set()
        self.goal_snapshot_gate: asyncio.Event | None = None
        self.goal_snapshot_eight_entered: asyncio.Event | None = None
        self.goal_snapshot_concurrency = 0
        self.goal_snapshot_max_concurrency = 0

    async def configure_exact(self, **values):
        self.calls.append(("configure", values["binding_id"]))
        return self.store.set_turn_settings(**values)

    async def activate_exact(self, binding_id: str):
        self.calls.append(("activate", binding_id))
        binding = self.store.get(binding_id)
        if binding.native_thread_id in self.archived:
            raise ThreadArchived("archived")
        if binding.native_thread_id in self.missing:
            raise ThreadCatalogIdentityMissing("missing")
        return self.store.activate(
            scope_key=binding.scope_key,
            binding_id=binding.id,
        )

    async def rename_exact(self, binding_id: str, name: str) -> str:
        self.calls.append(("rename", binding_id))
        if self.rename_entered is not None:
            self.rename_entered.set()
        if self.rename_release is not None:
            await self.rename_release.wait()
        return " ".join(name.split())

    async def archive_exact(self, binding_id: str):
        self.calls.append(("archive", binding_id))
        if self.archive_entered is not None:
            self.archive_entered.set()
        if self.archive_release is not None:
            await self.archive_release.wait()
        binding = self.store.get(binding_id)
        assert binding.native_thread_id is not None
        self.archived.add(binding.native_thread_id)
        return self.store.deactivate_if_active(
            scope_key=binding.scope_key,
            binding_id=binding.id,
        )

    async def restore_exact(self, binding_id: str):
        self.calls.append(("restore", binding_id))
        binding = self.store.get(binding_id)
        assert binding.native_thread_id is not None
        self.archived.discard(binding.native_thread_id)
        return binding

    async def restore_as_current_exact(self, binding_id: str):
        self.calls.append(("restore-current", binding_id))
        binding = await self.restore_exact(binding_id)
        return self.store.activate(
            scope_key=binding.scope_key,
            binding_id=binding.id,
        )

    async def delete_lazy_exact(self, binding_id: str):
        self.calls.append(("delete", binding_id))
        binding = self.store.get(binding_id)
        assert binding.native_thread_id is None
        return self.store.delete_binding(binding_id)

    async def delete_exact(
        self,
        binding_id: str,
        *,
        expected_native_thread_id: str | None,
    ):
        binding = self.store.get(binding_id)
        if binding.native_thread_id != expected_native_thread_id:
            raise AssertionError("unexpected native Thread identity")
        self.calls.append(("delete", binding_id))
        return self.store.delete_binding(binding_id)

    async def delete_archived_exact(
        self,
        binding_id: str,
        *,
        expected_native_thread_id: str,
    ):
        if expected_native_thread_id not in self.archived:
            raise AssertionError("expected an archived native Thread")
        self.calls.append(
            ("delete-archived", binding_id, expected_native_thread_id)
        )
        return self.store.delete_binding(binding_id)

    async def stop_exact(
        self,
        binding_id: str,
        *,
        acknowledge=None,
        expected_activity_revision: int | None = None,
        expected_turn_id: str | None = None,
    ):
        if expected_activity_revision is not None:
            snapshot = self.runtime_snapshot_exact(binding_id)
            if not isinstance(snapshot, BindingRuntimeSnapshot) or (
                snapshot.activity_revision != expected_activity_revision
                or (
                    snapshot.turn.turn_id if snapshot.turn is not None else None
                )
                != expected_turn_id
            ):
                raise RuntimeStateChanged("runtime state changed")
        self.calls.append(("stop", binding_id))
        if acknowledge is not None:
            await acknowledge()
        return StopDisposition.REQUESTED

    async def recheck_turn_exact(
        self,
        binding_id: str,
        *,
        expected_activity_revision: int,
        expected_turn_id: str,
    ):
        self.calls.append(
            (
                "recheck",
                binding_id,
                expected_activity_revision,
                expected_turn_id,
            )
        )
        return SimpleNamespace(turn_id=expected_turn_id)

    async def release_exact(self, binding_id: str):
        self.calls.append(("release", binding_id))
        return ReleaseDisposition.NOT_SUBSCRIBED

    async def close_side_exact(self, side_id: str, *, state: SideTopicState):
        self.calls.append(("close-side", side_id, state))
        if self.side_missing:
            raise SideSessionNotFound(side_id)
        record = self.store.transition_side_topic(side_id, state)
        return SideLifecycleOutcome(side_id=side_id, state=record.state)

    async def binding_pointer_changed(self, previous, current) -> None:
        self.calls.append(("pointer", previous, current))

    async def is_thread_archived(self, thread_id: str) -> bool:
        self.calls.append(("is-archived", thread_id))
        return thread_id in self.archived

    async def thread_catalog_state_exact(
        self,
        thread_id: str,
    ) -> NativeThreadCatalogState:
        self.calls.append(("catalog-state", thread_id))
        if thread_id in self.archived:
            return NativeThreadCatalogState.ARCHIVED
        if thread_id in self.missing:
            return NativeThreadCatalogState.MISSING
        return NativeThreadCatalogState.ACTIVE

    async def thread_metadata_exact(
        self,
        thread_ids: tuple[str, ...],
        *,
        archived: bool,
        deadline: float,
        use_state_db_only: bool | None = None,
    ):
        self.calls.append(("metadata", archived, thread_ids, deadline, use_state_db_only))
        source = self.archived_metadata if archived else self.active_metadata
        return {thread_id: source[thread_id] for thread_id in thread_ids if thread_id in source}

    async def thread_summary_exact(self, thread_id: str):
        self.calls.append(("summary", thread_id))
        if thread_id not in self.summary_metadata:
            raise RuntimeError("summary unavailable")
        return self.summary_metadata[thread_id]

    async def thread_catalog_exact(
        self, *, archived: bool, deadline: float, use_state_db_only=None,
    ):
        self.calls.append(("catalog", archived, deadline, use_state_db_only))
        source = self.archived_metadata if archived else self.active_metadata
        return NativeThreadCatalog(archived, tuple(source.values()))

    def runtime_snapshot_exact(self, binding_id: str):
        self.calls.append(("runtime-snapshot", binding_id))
        return self.runtime_snapshots.get(
            binding_id,
            BindingRuntimeSnapshot(
                binding_id,
                0,
                None,
                None,
                False,
                None,
                None,
                None,
            ),
        )

    async def goal_snapshot_exact(self, binding) -> GoalSnapshot | None:
        self.calls.append(("goal-snapshot", binding.id))
        if binding.id in self.goal_snapshot_errors:
            raise GoalControlError("unavailable")
        self.goal_snapshot_concurrency += 1
        self.goal_snapshot_max_concurrency = max(
            self.goal_snapshot_max_concurrency,
            self.goal_snapshot_concurrency,
        )
        if self.goal_snapshot_concurrency == 8 and self.goal_snapshot_eight_entered is not None:
            self.goal_snapshot_eight_entered.set()
        try:
            if self.goal_snapshot_gate is not None:
                await self.goal_snapshot_gate.wait()
            after = self.goal_snapshot_after.get(binding.id)
            if after is not None:
                self.runtime_snapshots[binding.id] = after
            return self.goal_snapshots.get(binding.id)
        finally:
            self.goal_snapshot_concurrency -= 1

    def side_snapshot_exact(self, side_id: str):
        self.calls.append(("side-snapshot", side_id))
        return self.side_snapshots.get(side_id)

    def project_side_snapshots(self, alias: str, *, limit: int = 1000):
        self.calls.append(("project-side-snapshots", alias))
        snapshots = tuple(
            snapshot for snapshot in self.side_snapshots.values()
            if getattr(snapshot, "project_alias", None) == alias
        )
        if len(snapshots) > limit:
            raise ValueError("Project Side snapshot exceeds the limit")
        return snapshots


class FakeChatLabels:
    def __init__(self) -> None:
        self.info_calls: list[str] = []

    async def get_chat_info(self, chat_id: str):
        self.info_calls.append(chat_id)
        return SimpleNamespace(
            name=f"Name {chat_id}",
            chat_mode="group",
            chat_type="private",
        )

    async def get_chat_members(self, _chat_id: str, **_kwargs):
        return []


class InstanceManagementServiceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        project = root / "project"
        project.mkdir()
        ids = iter(
            (
                "aaaaaaaa-0000-0000-0000-000000000001",
                "bbbbbbbb-0000-0000-0000-000000000002",
                "cccccccc-0000-0000-0000-000000000003",
                "dddddddd-0000-0000-0000-000000000004",
                "eeeeeeee-0000-0000-0000-000000000005",
            )
        )
        self.store = BindingStore(id_factory=lambda: next(ids))
        self.projects = ProjectRegistry(
            store=self.store,
            project_root=root,
            projects={"test": project},
        )
        self.runtime = FakeManagementRuntime(self.store)
        self.coordinator = ScopeCoordinator()
        self.service = InstanceManagementService(
            bindings=self.store,
            projects=self.projects,
            runtime=self.runtime,  # type: ignore[arg-type]
            scope_coordinator=self.coordinator,
        )
        self.scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        self.other_scope = FeishuScope("cli_test", "oc_other", ScopeKind.DIRECT)

    async def asyncTearDown(self) -> None:
        await self.service.close()
        self.store.close()
        self.tmp.cleanup()

    async def _create(self, scope: FeishuScope | None = None):
        return (
            await self.service.create_current_binding(
                scope=scope or self.scope,
                creator_id="ou_user",
                project_alias="test",
            )
        ).binding

    async def test_simultaneous_scheduled_cwd_checks_wait_for_bounded_io(self):
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()
        original = self.projects.resolve_for_new

        def resolve(alias):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(2):
                raise TimeoutError("test filesystem gate")
            return original(alias)

        with patch.object(self.projects, "resolve_for_new", side_effect=resolve):
            tasks = [asyncio.create_task(self.service.resolve_new_project(
                "test", deadline=loop.time() + 3,
            )) for _ in range(3)]
            try:
                await asyncio.wait_for(entered.wait(), 1)
                self.assertTrue(all(not task.done() for task in tasks))
            finally:
                release.set()
                results = await asyncio.gather(*tasks)
        self.assertEqual([project.alias for project in results], ["test"] * 3)

    async def test_native_delete_availability_uses_the_narrow_runtime_projection(
        self,
    ) -> None:
        self.assertTrue(self.service.native_delete_available)
        self.runtime.native_delete_available = False
        self.assertFalse(self.service.native_delete_available)

    async def test_current_target_rejects_stale_binding_before_runtime(self) -> None:
        first = await self._create()
        second = await self._create()
        before = tuple(self.runtime.calls)

        with self.assertRaises(CurrentBindingChanged):
            await self.service.rename_current_binding(
                target=CurrentBindingTarget(self.scope.key, first.id),
                name="stale",
            )

        self.assertEqual(tuple(self.runtime.calls), before)
        self.assertEqual(self.store.active_binding(self.scope.key).id, second.id)

    async def test_exact_inactive_rename_and_archive_preserve_other_pointer(self) -> None:
        first = await self._create()
        self.store.assign_native_thread_id(first.id, "native-first")
        second = await self._create()
        target = ExactBindingTarget(
            scope_key=self.scope.key,
            binding_id=first.id,
            expected_active_binding_id=second.id,
        )

        renamed = await self.service.rename_exact_binding(
            target=target,
            name="  inactive   thread  ",
        )
        archived = await self.service.archive_exact_binding(target=target)

        self.assertEqual(renamed.name, "inactive thread")
        self.assertEqual(archived.id, first.id)
        self.assertEqual(self.store.active_binding(self.scope.key).id, second.id)
        self.assertNotIn(("pointer", second.id, second.id), self.runtime.calls)

    async def test_active_archive_clears_pointer_and_notifies_after_commit(self) -> None:
        binding = await self._create()
        self.store.assign_native_thread_id(binding.id, "native-active")

        archived = await self.service.archive_current_binding(
            target=CurrentBindingTarget(self.scope.key, binding.id)
        )

        self.assertEqual(archived.id, binding.id)
        self.assertIsNone(self.store.active_binding(self.scope.key))
        self.assertEqual(self.runtime.calls[-1], ("pointer", binding.id, None))

    async def test_exact_archive_ignores_stale_scope_pointer_projection(
        self,
    ) -> None:
        binding = await self._create()
        self.store.assign_native_thread_id(binding.id, "native-active")
        other = await self._create()

        archived = await self.service.archive_exact_binding(
            target=ExactBindingTarget(
                self.scope.key,
                binding.id,
                "stale-pointer",
            ),
        )

        self.assertEqual(archived.id, binding.id)
        self.assertIn(("archive", binding.id), self.runtime.calls)
        self.assertEqual(self.store.active_binding(self.scope.key).id, other.id)

    async def test_native_archive_does_not_hold_the_scope_lock(self) -> None:
        binding = await self._create()
        self.store.assign_native_thread_id(binding.id, "native-active")
        self.runtime.archive_entered = asyncio.Event()
        self.runtime.archive_release = asyncio.Event()

        archiving = asyncio.create_task(
            self.service.archive_exact_binding(
                target=ExactBindingTarget(
                    self.scope.key,
                    binding.id,
                    binding.id,
                )
            )
        )
        await self.runtime.archive_entered.wait()

        replacement = await asyncio.wait_for(self._create(), timeout=0.1)
        self.assertEqual(
            self.store.active_binding(self.scope.key).id,
            replacement.id,
        )
        self.runtime.archive_release.set()
        archived = await archiving

        self.assertEqual(archived.id, binding.id)
        self.assertEqual(
            self.store.active_binding(self.scope.key).id,
            replacement.id,
        )
        self.assertEqual(
            self.runtime.calls.count(("pointer", binding.id, replacement.id)),
            1,
        )

    async def test_current_delete_requires_exact_native_identity(self) -> None:
        binding = await self._create()
        self.store.assign_native_thread_id(binding.id, "native-one")

        with self.assertRaises(CurrentBindingChanged):
            await self.service.delete_current_binding(
                target=CurrentBindingTarget(self.scope.key, binding.id),
                expected_native_thread_id=None,
            )

        deleted = await self.service.delete_current_binding(
            target=CurrentBindingTarget(self.scope.key, binding.id),
            expected_native_thread_id="native-one",
        )

        self.assertEqual(deleted.id, binding.id)
        self.assertIsNone(self.store.active_binding(self.scope.key))
        self.assertEqual(
            self.runtime.calls[-2:],
            [("delete", binding.id), ("pointer", binding.id, None)],
        )

    async def test_exact_inactive_delete_preserves_other_pointer(self) -> None:
        target = await self._create()
        self.store.assign_native_thread_id(target.id, "native-target")
        current = await self._create()

        deleted = await self.service.delete_exact_binding(
            target=ExactBindingTarget(
                scope_key=self.scope.key,
                binding_id=target.id,
                expected_active_binding_id=current.id,
            ),
            expected_native_thread_id="native-target",
        )

        self.assertEqual(deleted.id, target.id)
        self.assertEqual(self.store.active_binding(self.scope.key).id, current.id)
        self.assertEqual(self.runtime.calls[-1], ("delete", target.id))
        self.assertNotIn(("pointer", current.id, current.id), self.runtime.calls)

    async def test_materialized_delete_ignores_stale_scope_pointer_projection(
        self,
    ) -> None:
        binding = await self._create()
        self.store.assign_native_thread_id(binding.id, "native-active")
        other = await self._create()

        deleted = await self.service.delete_exact_binding(
            target=ExactBindingTarget(
                self.scope.key,
                binding.id,
                "stale-pointer",
            ),
            expected_native_thread_id="native-active",
        )

        self.assertEqual(deleted.id, binding.id)
        self.assertIn(("delete", binding.id), self.runtime.calls)
        self.assertEqual(self.store.active_binding(self.scope.key).id, other.id)

    async def test_archived_delete_and_recheck_forward_exact_preconditions(
        self,
    ) -> None:
        archived = await self._create()
        self.store.assign_native_thread_id(archived.id, "native-archived")
        self.store.deactivate(
            scope_key=self.scope.key,
            binding_id=archived.id,
        )
        self.runtime.archived.add("native-archived")

        await self.service.delete_archived_exact_binding(
            target=ExactBindingTarget(self.scope.key, archived.id, None),
            expected_native_thread_id="native-archived",
        )
        self.assertIn(
            ("delete-archived", archived.id, "native-archived"),
            self.runtime.calls,
        )

        current = await self._create()
        await self.service.recheck_exact_turn(
            target=ExactBindingTarget(
                self.scope.key,
                current.id,
                current.id,
            ),
            runtime_precondition=RuntimePrecondition(9, "turn-nine"),
        )
        self.assertIn(
            ("recheck", current.id, 9, "turn-nine"),
            self.runtime.calls,
        )

    async def test_exact_current_lazy_delete_clears_pointer(self) -> None:
        target = await self._create()

        deleted = await self.service.delete_exact_binding(
            target=ExactBindingTarget(
                scope_key=self.scope.key,
                binding_id=target.id,
                expected_active_binding_id=target.id,
            ),
            expected_native_thread_id=None,
        )

        self.assertEqual(deleted.id, target.id)
        self.assertIsNone(self.store.active_binding(self.scope.key))
        self.assertEqual(
            self.runtime.calls[-2:],
            [("delete", target.id), ("pointer", target.id, None)],
        )

    async def test_exact_delete_rejects_native_identity_change_before_runtime(
        self,
    ) -> None:
        target = await self._create()
        self.store.assign_native_thread_id(target.id, "native-target")
        before = tuple(self.runtime.calls)

        with self.assertRaises(ThreadDeleteTargetChanged):
            await self.service.delete_exact_binding(
                target=ExactBindingTarget(
                    scope_key=self.scope.key,
                    binding_id=target.id,
                    expected_active_binding_id=target.id,
                ),
                expected_native_thread_id=None,
            )

        self.assertEqual(tuple(self.runtime.calls), before)
        self.assertEqual(
            self.store.get(target.id).native_thread_id,
            "native-target",
        )

    async def test_exact_delete_rejects_cross_scope_target_before_runtime(
        self,
    ) -> None:
        current = await self._create()
        other = await self._create(self.other_scope)
        before = tuple(self.runtime.calls)

        with self.assertRaises(BindingScopeMismatch):
            await self.service.delete_exact_binding(
                target=ExactBindingTarget(
                    scope_key=self.scope.key,
                    binding_id=other.id,
                    expected_active_binding_id=current.id,
                ),
                expected_native_thread_id=None,
            )

        self.assertEqual(tuple(self.runtime.calls), before)
        self.assertEqual(self.store.get(other.id).id, other.id)
        self.assertEqual(self.store.active_binding(self.scope.key).id, current.id)

    async def test_exact_pointer_precondition_distinguishes_none(self) -> None:
        binding = await self._create()

        with self.assertRaises(ActivePointerChanged):
            await self.service.delete_exact_lazy_binding(
                target=ExactBindingTarget(
                    scope_key=self.scope.key,
                    binding_id=binding.id,
                    expected_active_binding_id=None,
                )
            )

        self.assertEqual(self.store.get(binding.id).id, binding.id)

    async def test_exact_activate_rejects_missing_native_catalog_identity(self) -> None:
        binding = await self._create()
        self.store.assign_native_thread_id(binding.id, "native-missing")
        self.store.deactivate_if_active(
            scope_key=self.scope.key,
            binding_id=binding.id,
        )
        self.runtime.missing.add("native-missing")

        with self.assertRaises(NativeThreadMissing):
            await self.service.activate_exact_binding(
                target=ExactBindingTarget(
                    scope_key=self.scope.key,
                    binding_id=binding.id,
                    expected_active_binding_id=None,
                )
            )

        self.assertIsNone(self.store.active_binding(self.scope.key))

    async def test_name_writer_wait_does_not_block_same_or_other_scope(self) -> None:
        first = await self._create()
        other = await self._create(self.other_scope)
        self.runtime.rename_entered = asyncio.Event()
        self.runtime.rename_release = asyncio.Event()

        rename = asyncio.create_task(
            self.service.rename_current_binding(
                target=CurrentBindingTarget(self.scope.key, first.id),
                name="held",
            )
        )
        await self.runtime.rename_entered.wait()
        same_scope = asyncio.create_task(self._create())
        other_scope = asyncio.create_task(
            self.service.release_current_binding(
                target=CurrentBindingTarget(self.other_scope.key, other.id)
            )
        )

        await asyncio.wait_for(other_scope, timeout=1)
        replacement = await asyncio.wait_for(same_scope, timeout=1)
        self.assertFalse(rename.done())
        self.runtime.rename_release.set()
        renamed = await asyncio.wait_for(rename, timeout=1)
        self.assertEqual(renamed.binding.id, first.id)
        self.assertEqual(self.store.active_binding(self.scope.key).id, replacement.id)

    async def test_admin_lazy_create_supports_inactive_and_current_modes(self) -> None:
        current = await self._create()
        revision = self.projects.resolve_for_new("test").revision

        inactive = await self.service.create_exact_lazy_binding(
            scope_key=self.scope.key,
            project_alias="test",
            expected_project_revision=revision,
            expected_active_binding_id=current.id,
            activate=False,
        )
        self.assertFalse(inactive.binding.active)
        self.assertIsNone(inactive.binding.activated_at)
        self.assertEqual(inactive.binding.creator_id, "admin:web")
        self.assertEqual(self.store.active_binding(self.scope.key).id, current.id)

        selected = await self.service.create_exact_lazy_binding(
            scope_key=self.scope.key,
            project_alias="test",
            expected_project_revision=revision,
            expected_active_binding_id=current.id,
            activate=True,
        )
        self.assertTrue(selected.binding.active)
        self.assertIsNotNone(selected.binding.activated_at)
        self.assertEqual(
            self.store.active_binding(self.scope.key).id,
            selected.binding.id,
        )
        self.assertIn(
            ("pointer", current.id, selected.binding.id),
            self.runtime.calls,
        )

        with self.assertRaises(ActivePointerChanged):
            await self.service.create_exact_lazy_binding(
                scope_key=self.scope.key,
                project_alias="test",
                expected_project_revision=revision,
                expected_active_binding_id=current.id,
                activate=False,
            )

    async def test_session_query_hydrates_only_materialized_page_records(self) -> None:
        materialized = await self._create()
        self.store.assign_native_thread_id(materialized.id, "native-active")
        lazy = await self._create()
        self.runtime.active_metadata["native-active"] = NativeThreadMetadata(
            "native-active",
            "Active title",
            "preview",
        )

        page = await self.service.query_sessions(
            deadline=asyncio.get_running_loop().time() + 1,
        )

        by_id = {item.record.binding.id: item for item in page.items}
        self.assertEqual(
            by_id[materialized.id].native.state,
            NativeThreadCatalogState.ACTIVE,
        )
        self.assertEqual(
            by_id[materialized.id].native.metadata.name,
            "Active title",
        )
        self.assertIsNone(by_id[lazy.id].native)
        metadata_calls = [call for call in self.runtime.calls if call[0] == "metadata"]
        self.assertEqual(len(metadata_calls), 2)
        self.assertEqual(metadata_calls[0][2], ("native-active",))

    async def test_session_pages_resolve_only_page_cache_misses(self) -> None:
        for index in range(3):
            await self._create(
                FeishuScope(
                    "cli_test",
                    f"oc_page_{index}",
                    ScopeKind.DIRECT,
                )
            )
        labels = FakeChatLabels()
        service = InstanceManagementService(
            bindings=self.store,
            projects=self.projects,
            runtime=self.runtime,  # type: ignore[arg-type]
            scope_coordinator=self.coordinator,
            chat_labels=labels,
        )
        try:
            first = await service.query_sessions(
                limit=2,
                deadline=asyncio.get_running_loop().time() + 1,
            )
            first_chat_ids = tuple(item.record.scope.chat_id for item in first.items)
            self.assertEqual(tuple(labels.info_calls), first_chat_ids)

            await service.query_sessions(
                limit=2,
                deadline=asyncio.get_running_loop().time() + 1,
            )
            self.assertEqual(tuple(labels.info_calls), first_chat_ids)

            second = await service.query_sessions(
                cursor=first.next_cursor,
                limit=2,
                deadline=asyncio.get_running_loop().time() + 1,
            )
            second_chat_ids = tuple(
                item.record.scope.chat_id for item in second.items
            )
            self.assertEqual(
                tuple(labels.info_calls),
                first_chat_ids + second_chat_ids,
            )
        finally:
            await service.close()

    async def test_scope_sessions_share_index_classification_with_instance_query(self) -> None:
        bindings = {}
        for name in ("active", "archived", "unindexed", "conflict", "lazy"):
            binding = await self._create()
            bindings[name] = binding
            if name != "lazy":
                self.store.assign_native_thread_id(binding.id, name)
        self.runtime.active_metadata = {
            name: NativeThreadMetadata(name, name, "preview")
            for name in ("active", "conflict")
        }
        self.runtime.archived_metadata = {
            name: NativeThreadMetadata(name, name, "preview")
            for name in ("archived", "conflict")
        }
        self.runtime.summary_metadata["unindexed"] = NativeThreadMetadata(
            "unindexed", "Recovered title", "",
        )
        self.runtime.calls.clear()
        instance = await self.service.query_sessions(
            deadline=asyncio.get_running_loop().time() + 1,
        )
        ordinary = await self.service.query_scope_sessions(
            scope=self.scope, deadline=asyncio.get_running_loop().time() + 1,
        )
        archived = await self.service.query_scope_sessions(
            scope=self.scope, archived=True, limit=None,
            deadline=asyncio.get_running_loop().time() + 1,
        )

        expected = {
            bindings[name].id: state for name, state in (
                ("active", SessionInventoryState.ACTIVE),
                ("archived", SessionInventoryState.ARCHIVED),
                ("unindexed", SessionInventoryState.UNKNOWN),
                ("conflict", SessionInventoryState.UNKNOWN),
                ("lazy", SessionInventoryState.LAZY),
            )
        }
        self.assertEqual(
            {item.record.binding.id: item.inventory_state for item in instance.items},
            expected,
        )
        self.assertEqual(
            {item.record.binding.id: item.inventory_state
             for item in (*ordinary.items, *archived.items)},
            expected,
        )
        self.assertEqual([item.record.binding.id for item in archived.items], [bindings["archived"].id])
        self.assertEqual(ordinary.total_count, 4)
        self.assertEqual(archived.total_count, 1)
        self.assertEqual(ordinary.unconfirmed_count, 2)
        self.assertEqual(archived.unconfirmed_count, 2)
        ordinary_by_id = {item.record.binding.id: item for item in ordinary.items}
        self.assertEqual(ordinary_by_id[bindings["unindexed"].id].native.metadata.name, "Recovered title")
        self.assertIsNone(ordinary_by_id[bindings["conflict"].id].native.metadata)
        self.assertTrue(all(call[-1] is True for call in self.runtime.calls if call[0] in {"metadata", "catalog"}))
        self.assertTrue(all(call[0] in {"metadata", "catalog", "summary"} for call in self.runtime.calls))

    async def test_scope_session_pages_pin_current_preserve_activation_order_and_only_hydrate_visible(self) -> None:
        bindings = []
        for index in range(4):
            with patch("netizen.bindings._now", return_value=f"2026-01-0{index + 1}T00:00:00+00:00"):
                binding = await self._create()
            self.store.assign_native_thread_id(binding.id, f"native-{index}")
            self.runtime.summary_metadata[f"native-{index}"] = NativeThreadMetadata(
                f"native-{index}", f"Title {index}", "",
            )
            bindings.append(binding)
        outside = await self._create(self.other_scope)
        self.store.assign_native_thread_id(outside.id, "native-outside")
        with patch("netizen.bindings._now", return_value="2030-01-01T00:00:00+00:00"):
            self.store.activate(scope_key=self.scope.key, binding_id=bindings[1].id)
        # A wall-clock rollback must not put the current Binding on a later page.
        with patch("netizen.bindings._now", return_value="2020-01-01T00:00:00+00:00"):
            self.store.activate(scope_key=self.scope.key, binding_id=bindings[0].id)

        for requested_page, expected_indices in ((0, (0, 1)), (1, (3, 2)), (99, (3, 2))):
            with self.subTest(page=requested_page):
                self.runtime.calls.clear()
                result = await self.service.query_scope_sessions(
                    scope=self.scope, page=requested_page, limit=2,
                    deadline=asyncio.get_running_loop().time() + 1,
                )
                self.assertEqual([item.record.binding.id for item in result.items], [bindings[index].id for index in expected_indices])
                self.assertEqual(result.page, min(requested_page, 1))
                self.assertEqual(result.total_count, 4)
                self.assertEqual(result.active_binding_id, bindings[0].id)
                self.assertEqual(
                    [call[1] for call in self.runtime.calls if call[0] == "summary"],
                    [f"native-{index}" for index in expected_indices],
                )
                self.assertTrue(all(item.native.metadata.name == f"Title {index}" for item, index in zip(result.items, expected_indices)))
                metadata_calls = [call for call in self.runtime.calls if call[0] == "metadata"]
                self.assertEqual(len(metadata_calls), 2)
                self.assertTrue(all(set(call[2]) == {f"native-{index}" for index in range(4)} and call[-1] is True for call in metadata_calls))

        self.runtime.archived_metadata = {
            f"native-{index}": NativeThreadMetadata(f"native-{index}", "Archived", "")
            for index in (1, 2, 3)
        }
        self.runtime.calls.clear()
        clamped = await self.service.query_scope_sessions(
            scope=self.scope, page=99, limit=2,
            deadline=asyncio.get_running_loop().time() + 1,
        )
        self.assertEqual(clamped.page, 0)
        self.assertEqual(clamped.total_count, 1)
        self.assertEqual([item.record.binding.id for item in clamped.items], [bindings[0].id])
        self.assertEqual([call[1] for call in self.runtime.calls if call[0] == "summary"], ["native-0"])

    async def test_scope_index_failure_preserves_ordinary_rows_but_rejects_archived_query(self) -> None:
        active = await self._create()
        self.store.assign_native_thread_id(active.id, "active")
        archived = await self._create()
        self.store.assign_native_thread_id(archived.id, "archived")
        lazy = await self._create()
        self.runtime.active_metadata["active"] = NativeThreadMetadata("active", "Active", "")
        self.runtime.archived_metadata["archived"] = NativeThreadMetadata("archived", "Archived", "")
        read_metadata = self.runtime.thread_metadata_exact

        for error in (RuntimeError("unavailable"), TimeoutError("incomplete")):
            async def fail_archived(*args, **kwargs):
                if kwargs["archived"]:
                    raise error
                return await read_metadata(*args, **kwargs)

            with self.subTest(error=type(error).__name__), patch.object(
                self.runtime, "thread_metadata_exact", side_effect=fail_archived,
            ):
                ordinary = await self.service.query_scope_sessions(
                    scope=self.scope, deadline=asyncio.get_running_loop().time() + 1,
                )
                self.assertFalse(ordinary.catalog_available)
                self.assertEqual(ordinary.total_count, 3)
                self.assertEqual(ordinary.unconfirmed_count, 2)
                self.assertEqual(
                    {item.record.binding.id: item.inventory_state for item in ordinary.items},
                    {active.id: SessionInventoryState.UNKNOWN, archived.id: SessionInventoryState.UNKNOWN, lazy.id: SessionInventoryState.LAZY},
                )
                with self.assertRaises(ThreadLifecycleError):
                    await self.service.query_scope_sessions(
                        scope=self.scope, archived=True, limit=None,
                        deadline=asyncio.get_running_loop().time() + 1,
                    )

    async def test_empty_and_lazy_scope_sessions_need_no_native_reads(self) -> None:
        self.runtime.calls.clear()
        empty = await self.service.query_scope_sessions(
            scope=self.scope, page=99,
            deadline=asyncio.get_running_loop().time() + 1,
        )
        self.assertEqual(empty.items, ())
        self.assertEqual(empty.total_count, 0)
        self.assertEqual(empty.page, 0)
        self.assertIsNone(empty.active_binding_id)
        self.assertEqual(self.runtime.calls, [])
        lazy = await self._create()
        self.runtime.calls.clear()
        for archived in (False, True):
            result = await self.service.query_scope_sessions(
                scope=self.scope, archived=archived,
                deadline=asyncio.get_running_loop().time() + 1,
            )
            self.assertEqual(result.total_count, 0 if archived else 1)
            self.assertTrue(result.catalog_available)
            self.assertEqual(result.unconfirmed_count, 0)
            self.assertEqual(result.active_binding_id, lazy.id)
        self.assertEqual(self.runtime.calls, [])

    async def test_session_query_accepts_one_hundred_only(self) -> None:
        page = await self.service.query_sessions(
            limit=100,
            deadline=asyncio.get_running_loop().time() + 1,
        )
        self.assertEqual(page.items, ())
        with self.assertRaises(ValueError):
            await self.service.query_sessions(
                limit=101,
                deadline=asyncio.get_running_loop().time() + 1,
            )

    async def test_session_state_filter_uses_both_indexed_native_catalogs(
        self,
    ) -> None:
        active = await self._create()
        self.store.assign_native_thread_id(active.id, "native-active")
        archived = await self._create()
        self.store.assign_native_thread_id(archived.id, "native-archived")
        missing = await self._create()
        self.store.assign_native_thread_id(missing.id, "native-missing")
        lazy = await self._create()
        self.runtime.active_metadata["native-active"] = NativeThreadMetadata(
            "native-active", None, "active"
        )
        self.runtime.archived_metadata["native-archived"] = NativeThreadMetadata(
            "native-archived", None, "archived"
        )
        cases = (
            (SessionInventoryState.ACTIVE, active.id, (False, True)),
            (SessionInventoryState.LAZY, lazy.id, ()),
            (SessionInventoryState.ARCHIVED, archived.id, (False, True)),
            (SessionInventoryState.UNKNOWN, missing.id, (False, True)),
        )
        for state, expected_id, expected_catalogs in cases:
            with self.subTest(state=state):
                self.runtime.calls.clear()
                page = await self.service.query_sessions(
                    query=SessionQuery(
                        local=BindingQuery(project_alias="test"),
                        inventory_states=(state,),
                    ),
                    deadline=asyncio.get_running_loop().time() + 1,
                )

                self.assertEqual(
                    [item.record.binding.id for item in page.items],
                    [expected_id],
                )
                self.assertEqual(
                    tuple(
                        call[1]
                        for call in self.runtime.calls
                        if call[0] == "catalog"
                    ),
                    expected_catalogs,
                )
                if state is SessionInventoryState.LAZY:
                    self.assertFalse(
                        any(call[0] == "metadata" for call in self.runtime.calls)
                    )

    async def test_session_multiselect_combines_native_and_lazy_with_indexed_catalogs(self) -> None:
        active = await self._create()
        self.store.assign_native_thread_id(active.id, "native-active")
        archived = await self._create()
        self.store.assign_native_thread_id(archived.id, "native-archived")
        missing = await self._create()
        self.store.assign_native_thread_id(missing.id, "native-missing")
        lazy = await self._create()
        self.runtime.active_metadata["native-active"] = NativeThreadMetadata(
            "native-active", "Active", "active"
        )
        self.runtime.archived_metadata["native-archived"] = NativeThreadMetadata(
            "native-archived", "Archived", "archived"
        )
        cases = (
            (("active", "lazy"), {active.id, lazy.id}, (False, True)),
            (("lazy", "archived"), {lazy.id, archived.id}, (False, True)),
            (("active", "archived"), {active.id, archived.id}, (False, True)),
            (("lazy", "unknown"), {lazy.id, missing.id}, (False, True)),
            (("active", "archived", "unknown"), {active.id, archived.id, missing.id}, (False, True)),
            (("missing",), set(), (False, True)),
            (("active", "lazy", "archived", "missing", "unknown"), {active.id, archived.id, missing.id, lazy.id}, ()),
        )
        for states, expected, catalogs in cases:
            with self.subTest(states=states):
                self.runtime.calls.clear()
                page = await self.service.query_sessions(
                    query=SessionQuery(inventory_states=tuple(SessionInventoryState(s) for s in states)),
                    deadline=asyncio.get_running_loop().time() + 1,
                )
                self.assertEqual({item.record.binding.id for item in page.items}, expected)
                self.assertEqual(
                    tuple(call[1] for call in self.runtime.calls if call[0] == "catalog"),
                    catalogs,
                )
                self.assertTrue(all(
                    call[3] is True for call in self.runtime.calls if call[0] == "catalog"
                ))
                for item in page.items:
                    if item.record.binding.id == lazy.id:
                        self.assertIsNone(item.native)
                    elif item.record.binding.id == missing.id:
                        self.assertIsNone(item.native.state)
                    else:
                        self.assertIsNotNone(item.native.metadata)

    async def test_session_multiselect_paginates_across_excluded_materialized_rows(self) -> None:
        first = await self._create()
        excluded = await self._create()
        self.store.assign_native_thread_id(excluded.id, "not-active")
        second = await self._create()
        self.store.assign_native_thread_id(second.id, "native-active")
        self.runtime.active_metadata["native-active"] = NativeThreadMetadata("native-active", "Active", "active")
        third = await self._create()
        query = SessionQuery(inventory_states=(SessionInventoryState.ACTIVE, SessionInventoryState.LAZY))
        first_page = await self.service.query_sessions(
            query=query, limit=2, deadline=asyncio.get_running_loop().time() + 1,
        )
        self.assertEqual([item.record.binding.id for item in first_page.items], [third.id, second.id])
        self.assertIsNotNone(first_page.next_cursor)
        second_page = await self.service.query_sessions(
            query=query, limit=2, cursor=first_page.next_cursor,
            deadline=asyncio.get_running_loop().time() + 1,
        )
        self.assertEqual([item.record.binding.id for item in second_page.items], [first.id])
        self.assertIsNone(second_page.next_cursor)

    async def test_project_options_include_disabled_and_paginate_without_native_reads(self) -> None:
        project = self.projects.register(alias="disabled", path=None, create_directory=True)
        self.projects.set_enabled(alias=project.alias, enabled=False, expected_revision=project.revision)
        self.runtime.calls.clear()
        first = await self.service.query_project_options(
            limit=1, deadline=asyncio.get_running_loop().time() + 1,
        )
        self.assertEqual([item.project.alias for item in first.items], ["disabled"])
        self.assertFalse(first.items[0].project.enabled)
        second = await self.service.query_project_options(
            limit=1, cursor=first.next_cursor, deadline=asyncio.get_running_loop().time() + 1,
        )
        self.assertEqual([item.project.alias for item in second.items], ["test"])
        self.assertIsNone(second.next_cursor)
        self.assertEqual(self.runtime.calls, [])

    async def test_sessions_use_indexed_identity_despite_duplicate_and_stale_rollouts(self) -> None:
        active = await self._create()
        self.store.assign_native_thread_id(active.id, "native-active")
        archived = await self._create()
        self.store.assign_native_thread_id(archived.id, "native-archived")
        missing = await self._create()
        self.store.assign_native_thread_id(missing.id, "native-missing")
        lazy = await self._create()
        active_row = SimpleNamespace(id="native-active", name="Current active", preview="active")
        archived_row = SimpleNamespace(id="native-archived", name="Current archived", preview="archived")
        old = SimpleNamespace(id=archived_row.id, name="Old", preview="old rollout")

        async def list_threads(**kwargs):
            if not kwargs.get("use_state_db_only"):
                # A reverted Thread has multiple rollouts, including an old
                # active copy of the currently archived Thread.
                return SimpleNamespace(data=[old, old], next_cursor=None)
            if kwargs["cursor"] is None:
                return SimpleNamespace(
                    data=[SimpleNamespace(id="unrelated", name=None, preview="other")]
                    if not kwargs["archived"] else [],
                    next_cursor="selected-thread",
                )
            self.assertEqual(kwargs["cursor"], "selected-thread")
            return SimpleNamespace(
                data=[archived_row if kwargs["archived"] else active_row],
                next_cursor=None,
            )

        codex = SimpleNamespace(thread_list=AsyncMock(side_effect=list_threads))
        port = ManagementRuntimePort(CodexRuntime(
            codex=codex, bindings=self.store, terminal_cleanup=SimpleNamespace(),
        ))
        cases = (
            ((SessionInventoryState.ACTIVE, SessionInventoryState.LAZY), {active.id, lazy.id}),
            ((SessionInventoryState.ARCHIVED,), {archived.id}),
            ((SessionInventoryState.UNKNOWN,), {missing.id}),
            (None, {active.id, archived.id, missing.id, lazy.id}),
        )
        with (
            patch.object(self.runtime, "thread_catalog_exact", port.thread_catalog_exact),
            patch.object(self.runtime, "thread_metadata_exact", port.thread_metadata_exact),
        ):
            for states, expected in cases:
                with self.subTest(states=states):
                    page = await self.service.query_sessions(
                        query=SessionQuery(inventory_states=states),
                        deadline=asyncio.get_running_loop().time() + 1,
                    )
                    by_id = {item.record.binding.id: item for item in page.items}
                    self.assertEqual(set(by_id), expected)
                    if active.id in by_id:
                        self.assertEqual(by_id[active.id].native.metadata.name, active_row.name)
                    if archived.id in by_id:
                        self.assertEqual(by_id[archived.id].native.metadata.name, archived_row.name)
                        self.assertEqual(by_id[archived.id].native.state, NativeThreadCatalogState.ARCHIVED)
                    if missing.id in by_id:
                        self.assertIsNone(by_id[missing.id].native.state)

    async def test_sessions_keep_unindexed_bindings_and_only_read_visible_summaries(self) -> None:
        indexed = await self._create()
        self.store.assign_native_thread_id(indexed.id, "indexed-active")
        unindexed = await self._create()
        self.store.assign_native_thread_id(unindexed.id, "unindexed")
        lazy = await self._create()
        self.runtime.active_metadata["indexed-active"] = NativeThreadMetadata("indexed-active", "Indexed", "active")
        # A stored Thread with an empty preview is readable but absent from both lists.
        self.runtime.summary_metadata["unindexed"] = NativeThreadMetadata(
            "unindexed", "Existing", "", updated_at=1_730_831_111,
        )
        cases = (
            ((SessionInventoryState.ACTIVE, SessionInventoryState.LAZY, SessionInventoryState.UNKNOWN), [lazy.id, unindexed.id, indexed.id]),
            ((SessionInventoryState.ACTIVE,), [indexed.id]),
            ((SessionInventoryState.UNKNOWN,), [unindexed.id]),
            ((SessionInventoryState.MISSING,), []),
            (None, [lazy.id, unindexed.id, indexed.id]),
        )
        for states, expected in cases:
            with self.subTest(states=states):
                cursor = None
                items = []
                for _ in range(4):
                    self.runtime.calls.clear()
                    page = await self.service.query_sessions(
                        query=SessionQuery(inventory_states=states),
                        limit=1, cursor=cursor,
                        deadline=asyncio.get_running_loop().time() + 1,
                    )
                    visible_unknown = [
                        item.record.binding.native_thread_id for item in page.items
                        if item.inventory_state is SessionInventoryState.UNKNOWN
                    ]
                    self.assertEqual([call[1] for call in self.runtime.calls if call[0] == "summary"], visible_unknown)
                    self.assertTrue(all(
                        call[-1] is True for call in self.runtime.calls
                        if call[0] in {"metadata", "catalog"}
                    ))
                    for item in page.items:
                        if item.record.binding.id == unindexed.id:
                            self.assertIsNone(item.native.state)
                            self.assertEqual(item.native.metadata.name, "Existing")
                            self.assertEqual(item.native.metadata.updated_at, 1_730_831_111)
                    items.extend(page.items)
                    cursor = page.next_cursor
                    if cursor is None:
                        break
                self.assertIsNone(cursor)
                self.assertEqual([item.record.binding.id for item in items], expected)

    async def test_session_index_conflicts_and_failures_preserve_unknown_rows(self) -> None:
        active = await self._create()
        self.store.assign_native_thread_id(active.id, "active")
        conflict = await self._create()
        self.store.assign_native_thread_id(conflict.id, "conflict")
        lazy = await self._create()
        self.runtime.active_metadata = {
            item: NativeThreadMetadata(item, item, "preview") for item in ("active", "conflict")
        }
        self.runtime.archived_metadata["conflict"] = self.runtime.active_metadata["conflict"]
        page = await self.service.query_sessions(deadline=asyncio.get_running_loop().time() + 1)
        self.assertTrue(page.catalog_available)
        self.assertEqual(
            {item.record.binding.id: item.inventory_state for item in page.items},
            {active.id: SessionInventoryState.ACTIVE, conflict.id: SessionInventoryState.UNKNOWN, lazy.id: SessionInventoryState.LAZY},
        )
        for states in (None, (SessionInventoryState.ACTIVE, SessionInventoryState.LAZY, SessionInventoryState.UNKNOWN)):
            with (
                self.subTest(states=states),
                patch.object(self.runtime, "thread_catalog_exact", side_effect=TimeoutError("incomplete")),
                patch.object(self.runtime, "thread_metadata_exact", side_effect=RuntimeError("unavailable")),
            ):
                first = await self.service.query_sessions(
                    query=SessionQuery(inventory_states=states), limit=2,
                    deadline=asyncio.get_running_loop().time() + 1,
                )
                self.assertFalse(first.catalog_available)
                self.assertEqual([item.record.binding.id for item in first.items], [lazy.id, conflict.id])
                second = await self.service.query_sessions(
                    query=SessionQuery(inventory_states=states), limit=2, cursor=first.next_cursor,
                    deadline=asyncio.get_running_loop().time() + 1,
                )
                self.assertEqual([item.record.binding.id for item in second.items], [active.id])
                self.assertIsNone(second.items[0].native.state)
                self.assertIsNone(second.items[0].native.metadata)

    async def test_summary_enrichment_covers_large_pages_with_shared_concurrency(self) -> None:
        self.store._id_factory = lambda: str(uuid.uuid4())
        for index in range(100):
            binding = await self._create()
            self.store.assign_native_thread_id(binding.id, f"unindexed-{index}")
        concurrency = 0
        maximum = 0
        reads = []

        async def read(thread_id):
            nonlocal concurrency, maximum
            reads.append(thread_id)
            concurrency += 1
            maximum = max(maximum, concurrency)
            try:
                await asyncio.sleep(0)
                return NativeThreadMetadata(thread_id, "Summary", "")
            finally:
                concurrency -= 1

        for page_size in (50, 100):
            records = await self.store.query_bindings(limit=page_size)
            reads.clear()
            with (
                self.subTest(page_size=page_size),
                patch.object(self.runtime, "thread_summary_exact", side_effect=read),
                patch.object(self.store, "query_bindings", return_value=records),
            ):
                pages = await asyncio.gather(*(
                    self.service.query_sessions(limit=page_size, deadline=asyncio.get_running_loop().time() + 2)
                    for _ in range(2)
                ))
                self.assertEqual(len(reads), page_size * 2)
                self.assertEqual(set(reads), {record.binding.native_thread_id for record in records.items})
                self.assertLessEqual(maximum, 4)
                for page in pages:
                    self.assertEqual(len(page.items), page_size)
                    self.assertTrue(all(item.inventory_state is SessionInventoryState.UNKNOWN for item in page.items))
                    self.assertTrue(all(item.native.metadata is not None for item in page.items))

    async def test_summary_uses_request_budget_and_isolates_individual_failures(self) -> None:
        for thread_id in ("slow", "failed"):
            binding = await self._create()
            self.store.assign_native_thread_id(binding.id, thread_id)

        async def read(thread_id):
            if thread_id == "failed":
                raise RuntimeError("summary unavailable")
            # A healthy response after one second must still appear on the page.
            await asyncio.sleep(1.1)
            return NativeThreadMetadata(thread_id, "Slow summary", "")

        with patch.object(self.runtime, "thread_summary_exact", side_effect=read):
            page = await self.service.query_sessions(deadline=asyncio.get_running_loop().time() + 5)
        items = {item.record.binding.native_thread_id: item for item in page.items}
        self.assertEqual(set(items), {"slow", "failed"})
        self.assertEqual(items["slow"].native.metadata.name, "Slow summary")
        self.assertIsNone(items["failed"].native.metadata)
        self.assertTrue(all(item.inventory_state is SessionInventoryState.UNKNOWN for item in page.items))

    async def test_summary_timeouts_keep_sdk_workers_counted_until_completion(self) -> None:
        self.store._id_factory = lambda: str(uuid.uuid4())
        for index in range(10):
            binding = await self._create()
            self.store.assign_native_thread_id(binding.id, f"unindexed-{index}")
        release = threading.Event()
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()
        reads = []

        def blocking_read(thread_id):
            release.wait()
            return NativeThreadMetadata(thread_id, "Summary", "")

        async def read(thread_id):
            reads.append(thread_id)
            if len(reads) == 4:
                entered.set()
            # Match the public SDK: cancelling this await cannot stop its worker.
            return await asyncio.to_thread(blocking_read, thread_id)

        try:
            with patch.object(self.runtime, "thread_summary_exact", side_effect=read):
                # Allow the local query and catalog lookup to start all four SDK
                # workers on busy CI runners before testing deadline expiry.
                first = asyncio.create_task(self.service.query_sessions(deadline=loop.time() + 1))
                await asyncio.wait_for(entered.wait(), 2)
                for page in (
                    await asyncio.wait_for(first, 2),
                    await asyncio.wait_for(self.service.query_sessions(deadline=loop.time() + 0.1), 0.5),
                ):
                    self.assertEqual(len(page.items), 10)
                    self.assertTrue(all(item.native.metadata is None for item in page.items))
                self.assertEqual(len(reads), 4)
                self.assertEqual(len(self.service._summary_reads), 4)
                release.set()
                await asyncio.wait_for(asyncio.gather(*self.service._summary_reads), 1)
                page = await self.service.query_sessions(deadline=loop.time() + 1)
                self.assertTrue(all(item.native.metadata is not None for item in page.items))

                # Shutdown closes admission even when an already-issued read is pending.
                release.clear()
                page = await self.service.query_sessions(deadline=loop.time() + 0.1)
                count_before_close = len(reads)
                await asyncio.wait_for(self.service.close(deadline=loop.time() + 0.01), 0.5)
                await self.service.query_sessions(deadline=loop.time() + 0.1)
                self.assertEqual(len(reads), count_before_close)
        finally:
            release.set()
            if self.service._summary_reads:
                await asyncio.wait_for(asyncio.gather(*self.service._summary_reads), 1)

    async def test_project_counts_distinguish_confirmed_unknown_and_unavailable(self) -> None:
        for thread_id in ("active", "archived", "unindexed"):
            binding = await self._create()
            self.store.assign_native_thread_id(binding.id, thread_id)
        await self._create()  # Lazy never adds uncertainty to archive counts.
        self.runtime.active_metadata["active"] = NativeThreadMetadata("active", None, "active")
        self.runtime.archived_metadata["archived"] = NativeThreadMetadata("archived", None, "archived")
        page = await self.service.query_projects(deadline=asyncio.get_running_loop().time() + 1)
        item = page.items[0]
        self.assertEqual(item.archived_binding_count, 1)
        self.assertEqual(item.unconfirmed_binding_count, 1)
        self.assertEqual(item.aggregate.binding_count, 4)
        self.assertFalse(any(call[0] == "summary" for call in self.runtime.calls))
        self.runtime.archived_metadata["active"] = self.runtime.active_metadata["active"]
        page = await self.service.query_projects(deadline=asyncio.get_running_loop().time() + 1)
        self.assertEqual(page.items[0].archived_binding_count, 1)
        self.assertEqual(page.items[0].unconfirmed_binding_count, 2)
        for target, attribute in ((self.runtime, "thread_catalog_exact"), (self.store, "project_aliases_for_native_threads")):
            with self.subTest(attribute=attribute), patch.object(target, attribute, side_effect=TimeoutError("unavailable")):
                page = await self.service.query_projects(deadline=asyncio.get_running_loop().time() + 1)
                self.assertIsNone(page.items[0].archived_binding_count)
                self.assertEqual(page.items[0].unconfirmed_binding_count, 3)
                self.assertEqual(page.items[0].aggregate.binding_count, 4)

    async def test_project_archive_counts_fail_closed_if_local_inventory_changes(self) -> None:
        binding = await self._create()
        self.store.assign_native_thread_id(binding.id, "original")
        self.runtime.archived_metadata["original"] = NativeThreadMetadata("original", None, "")
        original_catalog = self.runtime.thread_catalog_exact

        for operation in ("create", "delete"):
            mutated = False

            async def catalog(**kwargs):
                nonlocal mutated
                if not mutated:
                    mutated = True
                    if operation == "create":
                        added = await self._create()
                        self.store.assign_native_thread_id(added.id, "added")
                        self.runtime.archived_metadata["added"] = NativeThreadMetadata("added", None, "")
                    else:
                        self.store.delete_binding(binding.id)
                return await original_catalog(**kwargs)

            with self.subTest(operation=operation), patch.object(self.runtime, "thread_catalog_exact", side_effect=catalog):
                page = await self.service.query_projects(deadline=asyncio.get_running_loop().time() + 1)
                item = page.items[0]
                self.assertEqual(item.aggregate.binding_count, 1 if operation == "create" else 2)
                self.assertIsNone(item.archived_binding_count)
                self.assertEqual(item.unconfirmed_binding_count, item.aggregate.materialized_binding_count)
            # The failed read transaction must not poison subsequent queries.
            page = await self.service.query_projects(deadline=asyncio.get_running_loop().time() + 1)
            self.assertEqual(page.items[0].archived_binding_count, 2 if operation == "create" else 1)
            self.assertEqual(page.items[0].unconfirmed_binding_count, 0)

    async def test_projects_without_materialized_bindings_do_not_read_native_catalogs(self) -> None:
        await self._create()
        self.runtime.calls.clear()
        page = await self.service.query_projects(deadline=asyncio.get_running_loop().time() + 1)
        self.assertEqual(page.items[0].archived_binding_count, 0)
        self.assertEqual(page.items[0].unconfirmed_binding_count, 0)
        self.assertFalse(self.runtime.calls)

    async def test_stalled_index_leaves_budget_for_local_sessions_and_projects(self) -> None:
        binding = await self._create()
        self.store.assign_native_thread_id(binding.id, "unindexed")

        async def stalled_catalog(**_kwargs):
            await asyncio.sleep(10)

        with (
            patch.object(self.runtime, "thread_catalog_exact", side_effect=stalled_catalog),
            patch("netizen.management.service._NATIVE_INDEX_READ_SECONDS", 0.01),
        ):
            sessions = await asyncio.wait_for(self.service.query_sessions(
                query=SessionQuery(inventory_states=(SessionInventoryState.UNKNOWN,)),
                deadline=asyncio.get_running_loop().time() + 1,
            ), 0.5)
            self.assertFalse(sessions.catalog_available)
            self.assertEqual(sessions.items[0].record.binding.id, binding.id)
            projects = await asyncio.wait_for(self.service.query_projects(
                deadline=asyncio.get_running_loop().time() + 1,
            ), 0.5)
            self.assertIsNone(projects.items[0].archived_binding_count)
            self.assertEqual(projects.items[0].unconfirmed_binding_count, 1)

    async def test_project_query_counts_indexed_threads_with_multiple_rollouts(self) -> None:
        archived = await self._create()
        self.store.assign_native_thread_id(archived.id, "native-archived")
        current = SimpleNamespace(id="native-archived", name="Current", preview="current")
        old = SimpleNamespace(id=current.id, name="Old", preview="old rollout")
        unrelated = SimpleNamespace(id="unrelated", name=None, preview="other project")

        async def list_threads(**kwargs):
            if not kwargs.get("use_state_db_only"):
                return SimpleNamespace(data=[old, current], next_cursor=None)
            if not kwargs["archived"]:
                return SimpleNamespace(data=[], next_cursor=None)
            if kwargs["cursor"] is None:
                return SimpleNamespace(data=[unrelated], next_cursor="second-page")
            self.assertEqual(kwargs["cursor"], "second-page")
            return SimpleNamespace(data=[current], next_cursor=None)

        codex = SimpleNamespace(thread_list=AsyncMock(side_effect=list_threads))
        runtime = CodexRuntime(
            codex=codex,
            bindings=self.store,
            terminal_cleanup=SimpleNamespace(),
        )
        port = ManagementRuntimePort(runtime)

        with patch.object(self.runtime, "thread_catalog_exact", port.thread_catalog_exact):
            page = await self.service.query_projects(
                deadline=asyncio.get_running_loop().time() + 1,
            )

        by_alias = {item.aggregate.project.alias: item for item in page.items}
        self.assertEqual(by_alias["test"].archived_binding_count, 1)
        self.assertEqual(set(by_alias), {"test"})
        self.assertEqual(codex.thread_list.await_count, 3)

        # The same port's default read still validates the complete scan view.
        with self.assertRaisesRegex(ThreadCatalogError, "repeated a native Thread ID"):
            await port.thread_catalog_exact(
                archived=True, deadline=asyncio.get_running_loop().time() + 1,
            )

    async def test_runtime_snapshot_request_is_bounded_and_reports_missing_side(
        self,
    ) -> None:
        binding = await self._create()

        snapshots = self.service.runtime_snapshots(
            binding_ids=(binding.id,),
            side_ids=("side-missing",),
        )

        self.assertEqual(snapshots.bindings[0].binding_id, binding.id)
        self.assertIsNone(snapshots.bindings[0].primary_status)
        self.assertEqual(
            snapshots.bindings[0].primary_status_resolution.value,
            "deferred",
        )
        self.assertNotIn(("goal-snapshot", binding.id), self.runtime.calls)
        self.assertEqual(snapshots.sides, ())
        self.assertEqual(snapshots.missing_side_ids, ("side-missing",))
        with self.assertRaises(ValueError):
            self.service.runtime_snapshots(binding_ids=(binding.id, binding.id))

    async def test_primary_status_uses_one_canonical_priority(self) -> None:
        binding = await self._create()
        persisted = GoalSnapshot(
            "native-goal",
            "ship it",
            GoalStatus.PAUSED,
            None,
            10,
            2,
            1,
            2,
        )
        turn = ActiveTurnSnapshot(
            binding.id,
            "native-goal",
            "turn-1",
            "ou_user",
            ActiveState.RUNNING,
        )
        goal = ActiveGoalSnapshot(
            binding.id,
            "native-goal",
            "goal-1",
            "ou_user",
            GoalOperationState.RUNNING,
            persisted,
        )
        lifecycle = ThreadLifecycleSnapshot(
            binding.id,
            "native-goal",
            ThreadLifecycleState.ARCHIVING,
        )
        cases = (
            ((turn, goal, True, lifecycle), "archiving"),
            ((turn, goal, True, None), "running"),
            ((None, goal, True, None), "compacting"),
            ((None, goal, False, None), "goal-running"),
            ((None, None, False, None), "goal-paused"),
        )

        for (active, active_goal, compacting, active_lifecycle), expected in cases:
            with self.subTest(expected=expected):
                status = _project_binding_status(
                    binding=binding,
                    snapshot=BindingRuntimeSnapshot(
                        binding.id,
                        1,
                        active,
                        active_goal,
                        compacting,
                        active_lifecycle,
                        None,
                        None,
                    ),
                    persisted_goal=persisted,
                    persisted_goal_resolved=True,
                )
                self.assertEqual(status.primary_status, expected)

        idle = _project_binding_status(
            binding=binding,
            snapshot=BindingRuntimeSnapshot(
                binding.id,
                1,
                None,
                None,
                False,
                None,
                None,
                None,
            ),
            persisted_goal_resolved=True,
        )
        self.assertEqual(idle.primary_status, "idle")

    async def test_exact_status_resolves_persisted_goal_and_release_policy(
        self,
    ) -> None:
        binding = await self._create()
        self.store.assign_native_thread_id(binding.id, "native-goal")
        binding = self.store.get(binding.id)
        self.runtime.runtime_snapshots[binding.id] = BindingRuntimeSnapshot(
            binding.id,
            3,
            None,
            None,
            False,
            None,
            ThreadSubscriptionSnapshot(
                binding.id,
                "native-goal",
                ThreadSubscriptionState.SUBSCRIBED,
                None,
            ),
            None,
        )
        self.runtime.goal_snapshots[binding.id] = GoalSnapshot(
            "native-goal",
            "ship it",
            GoalStatus.PAUSED,
            None,
            10,
            2,
            1,
            2,
        )

        status = await self.service.binding_status_exact(
            binding.id,
            catalog_state=NativeThreadCatalogState.ACTIVE,
        )

        self.assertEqual(status.primary_status, "goal-paused")
        self.assertEqual(status.primary_status_resolution.value, "resolved")
        self.assertFalse(status.can_stop)
        self.assertTrue(status.can_release)
        self.assertIn(("goal-snapshot", binding.id), self.runtime.calls)

    async def test_exact_status_reprojects_post_read_local_state(self) -> None:
        binding = await self._create()
        self.store.assign_native_thread_id(binding.id, "native-goal")
        binding = self.store.get(binding.id)
        persisted = GoalSnapshot(
            "native-goal",
            "external",
            GoalStatus.ACTIVE,
            None,
            1,
            1,
            1,
            2,
        )
        self.runtime.goal_snapshots[binding.id] = persisted
        self.runtime.goal_snapshot_after[binding.id] = BindingRuntimeSnapshot(
            binding.id,
            4,
            None,
            ActiveGoalSnapshot(
                binding.id,
                "native-goal",
                None,
                "external",
                GoalOperationState.EXTERNAL_ACTIVE,
                persisted,
            ),
            False,
            None,
            None,
            None,
        )

        status = await self.service.binding_status_exact(
            binding.id,
            catalog_state=NativeThreadCatalogState.ACTIVE,
        )

        self.assertEqual(status.primary_status, "externally-active-goal")
        self.assertEqual(status.primary_status_resolution.value, "local")
        self.assertEqual(status.snapshot.activity_revision, 4)

    async def test_exact_status_batch_isolates_goal_read_failure(self) -> None:
        binding = await self._create()
        self.store.assign_native_thread_id(binding.id, "native-goal")
        binding = self.store.get(binding.id)
        self.runtime.goal_snapshot_errors.add(binding.id)

        for catalog_state in (NativeThreadCatalogState.ACTIVE, None):
            with self.subTest(catalog_state=catalog_state):
                self.runtime.calls.clear()
                (status,) = await self.service.binding_statuses_exact(
                    binding_ids=(binding.id,),
                    catalog_states={binding.id: catalog_state},
                    deadline=asyncio.get_running_loop().time() + 1,
                )

                self.assertIsNone(status.primary_status)
                self.assertEqual(status.primary_status_resolution.value, "unavailable")
                self.assertIn(("goal-snapshot", binding.id), self.runtime.calls)

    async def test_expired_status_budget_preserves_local_states_without_native_reads(self) -> None:
        native = await self._create()
        self.store.assign_native_thread_id(native.id, "native-goal")
        lazy = await self._create()
        compacting = await self._create()
        self.store.assign_native_thread_id(compacting.id, "native-compacting")
        self.runtime.runtime_snapshots[compacting.id] = BindingRuntimeSnapshot(
            compacting.id, 1, None, None, True, None, None, None,
        )
        archived = await self._create()
        self.store.assign_native_thread_id(archived.id, "native-archived")
        statuses = await self.service.binding_statuses_exact(
            binding_ids=(native.id, lazy.id, compacting.id, archived.id),
            catalog_states={archived.id: NativeThreadCatalogState.ARCHIVED},
            deadline=asyncio.get_running_loop().time() - 1,
        )
        self.assertEqual(
            [(status.primary_status, status.primary_status_resolution.value) for status in statuses],
            [
                (None, "unavailable"),
                ("idle", "resolved"),
                ("compacting", "local"),
                (None, "archived"),
            ],
        )
        self.assertFalse(any(call[0] == "goal-snapshot" for call in self.runtime.calls))

    async def test_exact_status_batch_bounds_native_goal_reads(self) -> None:
        self.store._id_factory = lambda: str(uuid.uuid4())
        bindings = []
        for index in range(18):
            binding = await self._create()
            self.store.assign_native_thread_id(binding.id, f"native-{index}")
            bindings.append(self.store.get(binding.id))
        self.runtime.goal_snapshot_gate = asyncio.Event()
        self.runtime.goal_snapshot_eight_entered = asyncio.Event()
        resolving = tuple(
            asyncio.create_task(
                self.service.binding_statuses_exact(
                    binding_ids=tuple(item.id for item in batch),
                    catalog_states={
                        item.id: NativeThreadCatalogState.ACTIVE for item in batch
                    },
                    deadline=asyncio.get_running_loop().time() + 10,
                )
            )
            for batch in (bindings[:9], bindings[9:])
        )
        try:
            await asyncio.wait_for(self.runtime.goal_snapshot_eight_entered.wait(), timeout=5)
            self.assertEqual(self.runtime.goal_snapshot_max_concurrency, 8)
            self.assertEqual(
                sum(call[0] == "goal-snapshot" for call in self.runtime.calls),
                8,
            )
            self.runtime.goal_snapshot_gate.set()
            statuses = await asyncio.wait_for(asyncio.gather(*resolving), timeout=5)
            self.assertEqual(sum(map(len, statuses)), 18)
            self.assertEqual(
                sum(call[0] == "goal-snapshot" for call in self.runtime.calls),
                18,
            )
            self.assertEqual(self.runtime.goal_snapshot_max_concurrency, 8)
            self.assertEqual(self.runtime.goal_snapshot_concurrency, 0)
        finally:
            self.runtime.goal_snapshot_gate.set()
            for task in resolving:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*resolving, return_exceptions=True)

    async def test_archived_status_skips_persisted_goal_until_restored(self) -> None:
        binding = await self._create()
        self.store.assign_native_thread_id(binding.id, "native-archived")
        binding = self.store.get(binding.id)
        self.runtime.goal_snapshots[binding.id] = GoalSnapshot(
            "native-archived",
            "keep this goal",
            GoalStatus.PAUSED,
            None,
            10,
            2,
            1,
            2,
        )

        status = await self.service.binding_status_exact(
            binding.id,
            catalog_state=NativeThreadCatalogState.ARCHIVED,
        )

        self.assertIsNone(status.primary_status)
        self.assertIsNone(status.persisted_goal_status)
        self.assertEqual(status.primary_status_resolution.value, "archived")
        self.assertFalse(status.can_stop)
        self.assertFalse(status.can_release)
        self.assertNotIn(("goal-snapshot", binding.id), self.runtime.calls)

        status = await self.service.binding_status_exact(
            binding.id,
            catalog_state=NativeThreadCatalogState.ACTIVE,
        )

        self.assertEqual(status.primary_status, "goal-paused")
        self.assertEqual(status.primary_status_resolution.value, "resolved")
        self.assertIn(("goal-snapshot", binding.id), self.runtime.calls)

    async def test_archived_status_preserves_local_activity_without_goal_reads(self) -> None:
        binding = await self._create()
        self.store.assign_native_thread_id(binding.id, "native-archived")
        self.runtime.goal_snapshot_errors.add(binding.id)
        base = BindingRuntimeSnapshot(
            binding.id, 1, None, None, False, None, None, None,
        )
        turn = ActiveTurnSnapshot(
            binding.id, "native-archived", "turn-1", "ou_user", ActiveState.RUNNING,
        )
        goal = ActiveGoalSnapshot(
            binding.id, "native-archived", "goal-1", "ou_user",
            GoalOperationState.RUNNING, None,
        )
        lifecycle = ThreadLifecycleSnapshot(
            binding.id, "native-archived", ThreadLifecycleState.ARCHIVING,
        )
        unknown = ThreadLifecycleSnapshot(
            binding.id, "native-archived", ThreadLifecycleState.UNKNOWN,
        )
        cases = (
            ((turn, goal, True, lifecycle), "archiving"),
            ((turn, goal, True, unknown), "lifecycle-unknown"),
            ((turn, goal, True, None), "running"),
            ((None, goal, True, None), "compacting"),
            ((None, goal, False, None), "goal-running"),
        )
        for (active, active_goal, compacting, active_lifecycle), expected in cases:
            with self.subTest(expected=expected):
                self.runtime.runtime_snapshots[binding.id] = BindingRuntimeSnapshot(
                    binding.id, 1, active, active_goal, compacting, active_lifecycle,
                    None, None,
                )
                status = await self.service.binding_status_exact(
                    binding.id,
                    catalog_state=NativeThreadCatalogState.ARCHIVED,
                )
                self.assertEqual(status.primary_status, expected)
                self.assertEqual(status.primary_status_resolution.value, "local")

        self.runtime.runtime_snapshots[binding.id] = base
        status = await self.service.binding_status_exact(
            binding.id, catalog_state=NativeThreadCatalogState.ARCHIVED,
        )
        self.assertIsNone(status.primary_status)
        self.assertEqual(status.primary_status_resolution.value, "archived")
        self.assertFalse(any(call[0] == "goal-snapshot" for call in self.runtime.calls))

    async def test_archived_projection_is_not_an_unavailable_runtime_state(self) -> None:
        binding = await self._create()
        self.store.assign_native_thread_id(binding.id, "native-archived")
        binding = self.store.get(binding.id)

        status = _project_binding_status(
            binding=binding,
            snapshot=self.runtime.runtime_snapshot_exact(binding.id),
            catalog_state=NativeThreadCatalogState.ARCHIVED,
            primary_status_unavailable=True,
        )

        self.assertIsNone(status.primary_status)
        self.assertEqual(status.primary_status_resolution.value, "archived")

    async def test_missing_status_resolves_idle_without_goal_read(self) -> None:
        binding = await self._create()
        self.store.assign_native_thread_id(binding.id, "native-missing")
        binding = self.store.get(binding.id)

        status = await self.service.binding_status_exact(
            binding.id,
            catalog_state=NativeThreadCatalogState.MISSING,
        )

        self.assertEqual(status.primary_status, "idle")
        self.assertNotIn(("goal-snapshot", binding.id), self.runtime.calls)

    def test_native_catalog_classification_is_shared_with_archived_precedence(
        self,
    ) -> None:
        active = {
            "active": NativeThreadMetadata("active", "Active", "preview")
        }
        archived = {
            "archived": NativeThreadMetadata("archived", "Old", "preview")
        }

        self.assertIs(
            classify_native_thread_view(
                "active", active=active, archived=archived
            ).state,
            NativeThreadCatalogState.ACTIVE,
        )
        self.assertIs(
            classify_native_thread_view(
                "archived", active=active, archived=archived
            ).state,
            NativeThreadCatalogState.ARCHIVED,
        )
        self.assertIs(
            classify_native_thread_view(
                "missing", active=active, archived=archived
            ).state,
            NativeThreadCatalogState.MISSING,
        )
        overlap = classify_native_thread_view(
            "active",
            active=active,
            archived={"active": active["active"]},
        )
        self.assertIs(overlap.state, NativeThreadCatalogState.ARCHIVED)

    async def test_exact_stop_rejects_a_new_runtime_revision(self) -> None:
        binding = await self._create()
        self.runtime.runtime_snapshot_exact = lambda binding_id: BindingRuntimeSnapshot(
            binding_id,
            7,
            None,
            None,
            False,
            None,
            None,
            None,
        )
        before = tuple(self.runtime.calls)

        with self.assertRaises(RuntimeStateChanged):
            await self.service.stop_exact_binding(
                target=ExactBindingTarget(
                    self.scope.key,
                    binding.id,
                    binding.id,
                ),
                runtime_precondition=RuntimePrecondition(6, None),
            )

        self.assertEqual(tuple(self.runtime.calls), before)

    async def test_missing_side_session_commits_expired_tombstone(self) -> None:
        parent = await self._create()
        side = self.store.create_side_topic(
            app_id="cli_test",
            chat_id="oc_direct",
            source_message_id="om_source",
            parent_binding_id=parent.id,
            creator_id="ou_user",
            requires_mention=False,
        )
        side = self.store.set_side_topic_root(side.id, "om_root")
        side = self.store.open_side_topic(side.id, "omt_topic")
        self.runtime.side_missing = True

        closed = await self.service.close_side(
            target=CurrentSideTarget(
                side_id=side.id,
                app_id=side.app_id,
                chat_id=side.chat_id,
                topic_id=side.topic_id,
                root_message_id=side.root_message_id,
            )
        )

        self.assertTrue(closed.missing_runtime_session)
        self.assertEqual(closed.record.state, SideTopicState.EXPIRED)

    async def test_terminal_side_close_only_checks_local_runtime_snapshot(self) -> None:
        parent = await self._create()
        side = self.store.create_side_topic(
            app_id="cli_test",
            chat_id="oc_direct",
            source_message_id="om_source_terminal",
            parent_binding_id=parent.id,
            creator_id="ou_user",
            requires_mention=False,
        )
        side = self.store.transition_side_topic(side.id, SideTopicState.FAILED)
        before = tuple(self.runtime.calls)

        closed = await self.service.close_side(
            target=CurrentSideTarget(
                side_id=side.id,
                app_id=side.app_id,
                chat_id=side.chat_id,
                topic_id=side.topic_id,
                root_message_id=side.root_message_id,
            )
        )

        self.assertIsNone(closed.outcome)
        self.assertEqual(closed.record.state, SideTopicState.FAILED)
        self.assertEqual(
            tuple(self.runtime.calls),
            (*before, ("side-snapshot", side.id)),
        )

    async def test_side_close_rejects_exact_identity_mismatch(self) -> None:
        parent = await self._create()
        side = self.store.create_side_topic(
            app_id="cli_test",
            chat_id="oc_direct",
            source_message_id="om_source_mismatch",
            parent_binding_id=parent.id,
            creator_id="ou_user",
            requires_mention=False,
        )
        before = tuple(self.runtime.calls)

        with self.assertRaises(SideIdentityMismatch):
            await self.service.close_side(
                target=CurrentSideTarget(
                    side_id=side.id,
                    app_id=side.app_id,
                    chat_id="oc_wrong",
                    topic_id=side.topic_id,
                    root_message_id=side.root_message_id,
                )
            )

        self.assertEqual(tuple(self.runtime.calls), before)

    async def test_channel_uses_shared_management_scope_coordinator(self) -> None:
        class RuntimeWithCompletion(FakeManagementRuntime):
            def set_completion_handler(self, _handler) -> None:
                pass

            def set_question_handler(self, _handler) -> None:
                pass

        runtime = RuntimeWithCompletion(self.store)

        application = ChannelApplication(
            app_id="cli_test",
            channel=object(),  # type: ignore[arg-type]
            runtime=runtime,  # type: ignore[arg-type]
            bindings=self.store,
            projects=self.projects,
            management=self.service,
        )
        try:
            self.assertIs(application._management, self.service)
            self.assertIs(application._scope_coordinator, self.service.scope_coordinator)
        finally:
            await application.close()


class ManagementRuntimePortSurfaceTest(unittest.TestCase):
    def test_native_delete_availability_is_projected_without_exposing_runtime(self) -> None:
        available = ManagementRuntimePort(
            SimpleNamespace(
                available_capabilities=frozenset({NativeCapability.DELETE})
            )
        )
        unavailable = ManagementRuntimePort(
            SimpleNamespace(available_capabilities=frozenset())
        )

        self.assertTrue(available.native_delete_available)
        self.assertFalse(unavailable.native_delete_available)

    def test_forbidden_runtime_capabilities_are_not_exposed(self) -> None:
        port = ManagementRuntimePort(object())  # type: ignore[arg-type]

        for name in (
            "submit",
            "compact",
            "start_goal",
            "resume_goal",
            "clear_goal",
            "create_side",
            "stop_side",
            "delete_binding",
        ):
            self.assertFalse(hasattr(port, name), name)


if __name__ == "__main__":
    unittest.main()
