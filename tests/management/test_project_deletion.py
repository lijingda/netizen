from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from netizen.bindings import (
    BindingNotFound,
    BindingStore,
    ProjectConflict,
    SideTopicState,
)
from netizen.codex_runtime import (
    SideCloseFailed,
    SideLifecycleOutcome,
    SideSessionNotFound,
    ThreadDeleteUnavailable,
    ThreadLifecycleStateUnknown,
)
from netizen.domain import FeishuScope, ScopeKind
from netizen.management import InstanceManagementService, ScopeCoordinator
from netizen.projects import ProjectError, ProjectRegistry, UnknownProject
from tests.management.test_service import FakeManagementRuntime


class ProjectRuntime(FakeManagementRuntime):
    def __init__(self, store: BindingStore) -> None:
        super().__init__(store)
        self.delete_errors: dict[str, Exception] = {}
        self.side_errors: dict[str, Exception] = {}
        self.delete_entered = asyncio.Event()
        self.delete_release: asyncio.Event | None = None
        self.cancelled_deletes: list[str] = []
        self.drain_hook = None
        self.delete_hook = None
        self.keep_side_snapshot = False

    async def drain_project_side_creation(self, binding_id: str) -> None:
        self.calls.append(("drain-side", binding_id))
        try:
            binding = self.store.get(binding_id)
        except BindingNotFound:
            return
        if not self.store.project_delete_in_progress(binding.project_alias):
            raise AssertionError("Side drain requires Project admission reservation")
        if self.drain_hook is not None:
            self.drain_hook(binding_id)

    async def close_side_exact(self, side_id: str, *, state: SideTopicState):
        self.calls.append(("close-side", side_id, state))
        error = self.side_errors.get(side_id)
        if error is not None:
            raise error
        if side_id not in self.side_snapshots:
            raise SideSessionNotFound(side_id)
        record = self.store.transition_side_topic(side_id, state)
        if not self.keep_side_snapshot:
            self.side_snapshots.pop(side_id)
        return SideLifecycleOutcome(side_id=side_id, state=record.state)

    async def delete_exact(self, binding_id: str, *, expected_native_thread_id):
        self.calls.append(("delete", binding_id, expected_native_thread_id))
        binding = self.store.get(binding_id)
        if binding.native_thread_id != expected_native_thread_id:
            raise AssertionError("Project delete changed its confirmed native target")
        self.delete_entered.set()
        if self.delete_release is not None:
            try:
                await self.delete_release.wait()
            except asyncio.CancelledError:
                self.cancelled_deletes.append(binding_id)
                raise
        error = self.delete_errors.get(binding_id)
        if error is not None:
            raise error
        if self.delete_hook is not None:
            self.delete_hook(binding_id)
        return self.store.delete_binding(binding_id)


class ProjectDeletionServiceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.cwd = self.root / "project"
        self.cwd.mkdir()
        self.other_cwd = self.root / "other"
        self.other_cwd.mkdir()
        self.marker = self.cwd / "keep.txt"
        self.marker.write_text("project code stays", encoding="utf-8")
        self.next_id = 0

        def make_id() -> str:
            self.next_id += 1
            return f"record-{self.next_id:03d}"

        self.store = BindingStore(id_factory=make_id)
        self.projects = ProjectRegistry(
            store=self.store,
            project_root=self.root,
            projects={"test": self.cwd, "other": self.other_cwd},
        )
        self.runtime = ProjectRuntime(self.store)
        self.service = InstanceManagementService(
            bindings=self.store,
            projects=self.projects,
            runtime=self.runtime,  # type: ignore[arg-type]
            scope_coordinator=ScopeCoordinator(),
        )
        self.tasks: list[asyncio.Task[object]] = []

    async def asyncTearDown(self) -> None:
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.service.close()
        self.store.close()
        self.directory.cleanup()

    def binding(self, name: str, *, native: str | None = None, project: str = "test"):
        binding = self.store.create_binding(
            scope=FeishuScope("cli_test", f"oc_{name}", ScopeKind.DIRECT),
            project_alias=project,
            creator_id="ou_owner",
        )
        if native is not None:
            self.store.assign_native_thread_id(binding.id, native)
        return self.store.get(binding.id)

    def side(self, binding, state: SideTopicState, *, live: bool):
        record = self.store.create_side_topic(
            app_id="cli_test",
            chat_id=f"oc_side_{self.next_id}",
            source_message_id=f"om_source_{self.next_id}",
            parent_binding_id=binding.id,
            creator_id="ou_owner",
            requires_mention=False,
        )
        if state is SideTopicState.OPEN:
            self.store.set_side_topic_root(record.id, f"om_root_{record.id}")
            record = self.store.open_side_topic(record.id, f"omt_{record.id}")
        elif state.terminal:
            record = self.store.transition_side_topic(record.id, state)
        if live:
            self.runtime.side_snapshots[record.id] = SimpleNamespace(
                side_id=record.id,
                parent_binding_id=binding.id,
                project_alias=binding.project_alias,
            )
        return record

    async def preview(self):
        return await self.service.preview_project_delete(
            alias="test",
            expected_revision=self.store.get_project("test").revision,
            deadline=asyncio.get_running_loop().time() + 5,
        )

    async def delete(self, snapshot=None, *, deadline=None):
        snapshot = snapshot or await self.preview()
        return await self.service.delete_project(
            alias="test",
            expected_revision=snapshot.project.revision,
            expected_inventory_fingerprint=snapshot.fingerprint,
            deadline=deadline,
        )

    def native_deletes(self):
        return [call for call in self.runtime.calls if call[0] == "delete"]

    def assert_retained(self, *bindings) -> None:
        self.assertFalse(self.store.get_project("test").enabled)
        self.assertFalse(self.store.project_delete_in_progress("test"))
        for binding in bindings:
            self.assertEqual(self.store.get(binding.id).id, binding.id)
        self.assertEqual(self.marker.read_text(encoding="utf-8"), "project code stays")

    async def test_deletes_all_inventory_and_sides_before_each_parent_retaining_files(self) -> None:
        active = self.binding("active", native="native-active")
        archived = self.binding("archived", native="native-archived")
        self.runtime.archived.add("native-archived")
        other = self.binding("archived", native="native-other", project="other")
        lazy = self.binding("lazy")
        open_side = self.side(active, SideTopicState.OPEN, live=True)
        missing_side = self.side(archived, SideTopicState.OPEN, live=False)
        retained_side = self.side(active, SideTopicState.FAILED, live=True)
        closed_side = self.side(lazy, SideTopicState.CLOSED, live=False)
        expired_side = self.side(lazy, SideTopicState.EXPIRED, live=False)
        snapshot = await self.preview()
        self.assertEqual(len(snapshot.bindings), 3)
        self.assertEqual(len(snapshot.sides), 5)

        result = await self.delete(snapshot)

        self.assertTrue(result.deleted)
        self.assertEqual(result.code, "deleted")
        self.assertEqual(result.deleted_session_count, 3)
        self.assertEqual(result.remaining_sessions, ())
        self.assertEqual(result.remaining_side_count, 0)
        self.assertEqual(self.native_deletes(), [
            ("delete", active.id, "native-active"),
            ("delete", archived.id, "native-archived"),
            ("delete", lazy.id, None),
        ])
        self.assertIsNone(self.store.active_binding(active.scope_key))
        self.assertIsNone(self.store.active_binding(lazy.scope_key))
        self.assertEqual(self.store.active_binding(other.scope_key).id, other.id)
        for side, parent in ((open_side, active), (retained_side, active), (missing_side, archived)):
            close_index = next(i for i, call in enumerate(self.runtime.calls) if call[:2] == ("close-side", side.id))
            delete_index = next(i for i, call in enumerate(self.runtime.calls) if call[:2] == ("delete", parent.id))
            self.assertLess(close_index, delete_index)
        self.assertTrue(all(side.state.terminal for side in self.store.list_side_topics()))
        closed_ids = {call[1] for call in self.runtime.calls if call[0] == "close-side"}
        self.assertNotIn(closed_side.id, closed_ids)
        self.assertNotIn(expired_side.id, closed_ids)
        forbidden = {"stop", "archive", "restore", "activate", "release", "catalog", "goal-snapshot"}
        self.assertFalse(any(call[0] in forbidden for call in self.runtime.calls))
        with self.assertRaises(UnknownProject):
            self.projects.resolve("test")
        self.assertEqual(self.marker.read_text(encoding="utf-8"), "project code stays")

    async def test_missing_binding_uses_exact_delete_without_catalog_filter(self) -> None:
        missing = self.binding("missing", native="native-missing")
        self.runtime.missing.add("native-missing")
        result = await self.delete()
        self.assertTrue(result.deleted)
        self.assertEqual(self.native_deletes(), [("delete", missing.id, "native-missing")])

    async def test_preexisting_orphan_side_is_closed_before_removing_empty_project(self) -> None:
        parent = self.binding("deleted-parent", native="native-parent")
        side = self.side(parent, SideTopicState.OPEN, live=True)
        self.store.delete_binding(parent.id)

        snapshot = await self.preview()
        self.assertEqual(snapshot.bindings, ())
        self.assertEqual(tuple(item.id for item in snapshot.sides), (side.id,))
        result = await self.delete(snapshot)

        self.assertTrue(result.deleted)
        self.assertEqual(result.deleted_session_count, 0)
        self.assertEqual(result.remaining_side_count, 0)
        self.assertEqual(self.native_deletes(), [])
        self.assertEqual(self.store.get_side_topic(side.id).state, SideTopicState.CLOSED)
        self.assertEqual(self.runtime.project_side_snapshots("test"), ())

    async def test_preexisting_orphan_side_failure_retains_empty_project(self) -> None:
        parent = self.binding("deleted-parent", native="native-parent")
        side = self.side(parent, SideTopicState.OPEN, live=True)
        self.store.delete_binding(parent.id)
        self.runtime.side_errors[side.id] = SideCloseFailed("close unconfirmed")

        result = await self.delete()

        self.assertFalse(result.deleted)
        self.assertEqual(result.code, "side_close_failed")
        self.assertEqual(result.remaining_sessions, ())
        self.assertEqual(result.remaining_side_count, 1)
        self.assertEqual(result.failed_side_id, side.id)
        self.assertEqual(self.native_deletes(), [])
        self.assert_retained()

    async def test_unknown_after_partial_success_stops_without_retry_or_rollback(self) -> None:
        first = self.binding("first", native="native-first")
        failed = self.binding("failed", native="native-failed")
        untouched = self.binding("untouched")
        self.runtime.delete_errors[failed.id] = ThreadLifecycleStateUnknown("response lost")

        result = await self.delete()

        self.assertFalse(result.deleted)
        self.assertEqual(result.code, "outcome_unknown")
        self.assertEqual(result.deleted_session_count, 1)
        self.assertEqual(tuple(item.id for item in result.remaining_sessions), (failed.id, untouched.id))
        self.assertEqual(result.failed_binding_id, failed.id)
        self.assertEqual(self.native_deletes(), [
            ("delete", first.id, "native-first"),
            ("delete", failed.id, "native-failed"),
        ])
        with self.assertRaises(BindingNotFound):
            self.store.get(first.id)
        self.assert_retained(failed, untouched)
        other = self.binding("unaffected", project="other")
        self.assertEqual(self.store.get(other.id).project_alias, "other")

    async def test_failed_side_keeps_parent_and_remaining_side_identity(self) -> None:
        binding = self.binding("side-parent", native="native-parent")
        side = self.side(binding, SideTopicState.OPEN, live=True)
        self.runtime.side_errors[side.id] = SideCloseFailed("unsubscribe unknown")

        result = await self.delete()

        self.assertEqual(result.code, "side_close_failed")
        self.assertEqual(result.failed_binding_id, binding.id)
        self.assertEqual(result.failed_side_id, side.id)
        self.assertEqual(result.remaining_side_count, 1)
        self.assertEqual(self.native_deletes(), [])
        self.assert_retained(binding)

    async def test_successful_close_with_retained_runtime_side_cannot_delete_parent(self) -> None:
        binding = self.binding("side-parent", native="native-parent")
        side = self.side(binding, SideTopicState.OPEN, live=True)
        self.runtime.keep_side_snapshot = True

        result = await self.delete()

        self.assertEqual(result.code, "side_close_failed")
        self.assertTrue(self.store.get_side_topic(side.id).state.terminal)
        self.assertEqual(result.remaining_side_count, 1)
        self.assertEqual(self.native_deletes(), [])
        self.assert_retained(binding)

    async def test_materialization_after_preview_invalidates_confirmation_before_mutation(self) -> None:
        lazy = self.binding("lazy")
        snapshot = await self.preview()
        self.store.assign_native_thread_id(lazy.id, "native-created-later")

        with self.assertRaises(ProjectError):
            await self.delete(snapshot)

        self.assertEqual(self.native_deletes(), [])
        self.assertTrue(self.store.get_project("test").enabled)
        self.assertFalse(self.store.project_delete_in_progress("test"))
        self.assertEqual(self.store.get(lazy.id).native_thread_id, "native-created-later")

    async def test_side_identity_change_during_drain_is_not_reinterpreted(self) -> None:
        binding = self.binding("side-parent", native="native-parent")
        side = self.side(binding, SideTopicState.CREATING, live=False)

        def finish_publication(_binding_id):
            self.store.set_side_topic_root(side.id, "om-new-root")
            self.store.open_side_topic(side.id, "omt-new-topic")

        self.runtime.drain_hook = finish_publication

        result = await self.delete()

        self.assertEqual(result.code, "inventory_changed")
        self.assertEqual(result.failed_side_id, side.id)
        self.assertEqual(self.native_deletes(), [])
        self.assertFalse(any(call[0] == "close-side" for call in self.runtime.calls))
        self.assert_retained(binding)

    async def test_creating_side_keeps_route_for_pending_feishu_publication(self) -> None:
        binding = self.binding("side-parent", native="native-parent")
        side = self.side(binding, SideTopicState.CREATING, live=True)

        result = await self.delete()

        self.assertFalse(result.deleted)
        self.assertEqual(result.code, "side_creation_in_progress")
        self.assertEqual(result.failed_side_id, side.id)
        self.assertEqual(result.remaining_side_count, 1)
        self.assertEqual(self.store.get_side_topic(side.id).state, SideTopicState.CREATING)
        self.assertEqual(self.native_deletes(), [])
        self.assertFalse(any(call[0] == "close-side" for call in self.runtime.calls))
        self.assert_retained(binding)

    async def test_materialization_during_drain_preserves_new_native_identity(self) -> None:
        binding = self.binding("lazy-parent")
        self.side(binding, SideTopicState.FAILED, live=False)
        self.runtime.drain_hook = lambda _: self.store.assign_native_thread_id(binding.id, "native-new")

        result = await self.delete()

        self.assertEqual(result.code, "inventory_changed")
        self.assertEqual(self.native_deletes(), [])
        self.assertEqual(result.remaining_sessions[0].native_thread_id, "native-new")
        self.assert_retained(binding)

    async def test_concurrent_exact_delete_can_remove_a_later_confirmed_binding(self) -> None:
        first = self.binding("first")
        second = self.binding("second")
        self.side(second, SideTopicState.CLOSED, live=False)
        self.runtime.delete_hook = lambda _: self.store.delete_binding(second.id)

        result = await self.delete()

        self.assertTrue(result.deleted)
        self.assertEqual(result.deleted_session_count, 2)
        self.assertEqual(self.native_deletes(), [("delete", first.id, None)])

    async def test_unavailable_native_delete_never_disables_a_materialized_project(self) -> None:
        self.binding("materialized", native="native-one")
        snapshot = await self.preview()
        self.runtime.native_delete_available = False
        with self.assertRaises(ThreadDeleteUnavailable):
            await self.preview()
        with self.assertRaises(ThreadDeleteUnavailable):
            await self.delete(snapshot)
        self.assertTrue(self.store.get_project("test").enabled)
        self.assertEqual(self.native_deletes(), [])

    async def test_lazy_only_project_does_not_require_native_delete_capability(self) -> None:
        lazy = self.binding("lazy")
        self.runtime.native_delete_available = False
        result = await self.delete()
        self.assertTrue(result.deleted)
        self.assertEqual(self.native_deletes(), [("delete", lazy.id, None)])

    async def test_deadline_keeps_project_and_releases_only_project_fence(self) -> None:
        await self._assert_interrupted_delete_preserves_project(cancel=False)

    async def test_cancellation_keeps_project_and_releases_only_project_fence(self) -> None:
        await self._assert_interrupted_delete_preserves_project(cancel=True)

    async def _assert_interrupted_delete_preserves_project(self, *, cancel: bool) -> None:
        binding = self.binding("blocked", native="native-blocked")
        snapshot = await self.preview()
        self.runtime.delete_release = asyncio.Event()
        pending = asyncio.create_task(self.delete(
            snapshot,
            deadline=asyncio.get_running_loop().time() + (5 if cancel else 0.2),
        ))
        self.tasks.append(pending)
        await asyncio.wait_for(self.runtime.delete_entered.wait(), 1)
        self.assertTrue(self.store.project_delete_in_progress("test"))
        project = self.store.get_project("test")
        with self.assertRaises(ProjectError):
            await self.service.set_project_enabled(
                alias="test", enabled=True, expected_revision=project.revision,
            )
        with self.assertRaises(ProjectConflict):
            self.binding("newly-blocked")
        with self.assertRaises(ProjectConflict):
            self.side(binding, SideTopicState.CREATING, live=False)
        # The native wait releases Scope coordination too: another Project may
        # create a new current Binding in the same Scope during this deletion.
        other = await asyncio.wait_for(self.service.create_current_binding(
            scope=FeishuScope("cli_test", "oc_blocked", ScopeKind.DIRECT),
            project_alias="other",
            creator_id="ou_owner",
        ), 1)
        self.assertEqual(self.store.active_binding(binding.scope_key).id, other.binding.id)

        if cancel:
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
        else:
            result = await pending
            self.assertEqual(result.code, "deadline_exceeded")
            self.assertEqual(result.remaining_sessions[0].id, binding.id)
        self.assertEqual(self.runtime.cancelled_deletes, [binding.id])
        self.assertEqual(self.native_deletes(), [("delete", binding.id, "native-blocked")])
        self.assert_retained(binding)
