from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from netizen.bindings import BindingStore, ProjectConflict, SideTopicState
from netizen.codex_runtime import (
    CodexRuntime,
    SideCloseFailed,
    SideSessionConflict,
    SideSessionNotFound,
    SideSessionState,
    ThreadDeleteTargetChanged,
    ThreadDeleteUnavailable,
)
from netizen.domain import FeishuScope, ScopeKind
from netizen.management import InstanceManagementService, ManagementRuntimePort, ScopeCoordinator
from netizen.projects import ProjectRegistry, UnknownProject
from tests import test_codex_runtime as fixtures


class ProjectDeleteSideRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.cwd = Path(self.directory.name)
        self.store = BindingStore()
        self.store.bootstrap_project(alias="test", cwd=str(self.cwd))
        self.codex = fixtures.FakeCodex()
        self.cleanup = fixtures.FakeTerminalCleanup(self.codex.events)
        self.side_control = fixtures.FakeSideThreadControl()
        self.runtime = CodexRuntime(
            codex=self.codex,
            bindings=self.store,
            terminal_cleanup=self.cleanup,
            side_boundary_control=self.side_control,
            thread_subscription_control=self.side_control,
            poll_interval_seconds=0,
            side_idle_seconds=60,
        )
        self.scope = FeishuScope("cli_test", "oc_delete", ScopeKind.DIRECT)
        self.binding = self.store.create_binding(
            scope=self.scope,
            project_alias="test",
            creator_id="ou_owner",
        )
        self.store.assign_native_thread_id(self.binding.id, "native-parent")
        self.binding = self.store.get(self.binding.id)
        self.route = self.store.create_side_topic(
            app_id=self.scope.app_id,
            chat_id=self.scope.chat_id,
            source_message_id="om-side-source",
            parent_binding_id=self.binding.id,
            creator_id="ou_owner",
            requires_mention=False,
        )
        self.tasks: list[asyncio.Task[object]] = []

    async def asyncTearDown(self) -> None:
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        try:
            await self.runtime.interrupt_all()
        finally:
            await self.runtime.cancel_tasks()
            self.store.close()
            self.directory.cleanup()

    def begin_delete(self) -> None:
        snapshot = self.store.preview_project_delete("test")
        self.store.begin_project_delete(
            alias="test",
            expected_revision=snapshot.project.revision,
            expected_inventory_fingerprint=snapshot.fingerprint,
        )

    async def create_side(self):
        return await self.runtime.create_side(
            side_id=self.route.id,
            binding=self.binding,
            cwd=self.cwd,
            creator_id="ou_owner",
        )

    async def test_preexisting_reservation_cannot_fork_after_deletion_starts(self) -> None:
        self.begin_delete()

        with self.assertRaises(ProjectConflict):
            await self.create_side()

        self.assertEqual(self.codex.resume_calls, [])
        self.assertEqual(self.codex.fork_calls, [])

    async def test_deletion_started_during_parent_read_is_checked_before_fork(self) -> None:
        gate = asyncio.Event()
        self.codex.resume_gate = gate
        creating = asyncio.create_task(self.create_side())
        self.tasks.append(creating)
        async with asyncio.timeout(1):
            while not self.codex.resume_calls:
                await asyncio.sleep(0)
        self.begin_delete()
        draining = asyncio.create_task(
            self.runtime.drain_project_side_creation(self.binding.id)
        )
        self.tasks.append(draining)
        await asyncio.sleep(0)
        self.assertFalse(draining.done())

        gate.set()
        with self.assertRaises(ProjectConflict):
            await creating
        await asyncio.wait_for(draining, 1)

        self.assertEqual(self.codex.fork_calls, [])
        with self.assertRaises(SideSessionNotFound):
            self.runtime.side_snapshot(self.route.id)

    async def test_drain_waits_for_admitted_fork_before_side_close(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        original_fork = self.codex.thread_fork

        async def blocked_fork(thread_id: str, **kwargs):
            thread = await original_fork(thread_id, **kwargs)
            entered.set()
            await release.wait()
            return thread

        self.codex.thread_fork = blocked_fork
        creating = asyncio.create_task(self.create_side())
        self.tasks.append(creating)
        await asyncio.wait_for(entered.wait(), 1)
        self.begin_delete()

        # A bounded management wait can expire without cancelling the admitted
        # native fork or falsely proving that its session is absent.
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(
                self.runtime.drain_project_side_creation(self.binding.id),
                0.01,
            )
        self.assertFalse(creating.done())
        await asyncio.wait_for(
            self.runtime.drain_project_side_creation("unrelated-binding"),
            1,
        )

        draining = asyncio.create_task(
            self.runtime.drain_project_side_creation(self.binding.id)
        )
        self.tasks.append(draining)
        await asyncio.sleep(0)
        self.assertFalse(draining.done())
        release.set()
        created = await asyncio.wait_for(creating, 1)
        await asyncio.wait_for(draining, 1)
        self.assertEqual(
            self.runtime.side_snapshot(self.route.id).thread_id,
            created.thread_id,
        )
        outcome = await self.runtime.close_side_exact(
            self.route.id,
            state=SideTopicState.FAILED,
        )

        self.assertIsNone(outcome.error)
        self.assertEqual(
            self.store.get_side_topic(self.route.id).state,
            SideTopicState.FAILED,
        )
        self.assertEqual(self.side_control.unsubscribe_calls, [created.thread_id])
        with self.assertRaises(SideSessionNotFound):
            self.runtime.side_snapshot(self.route.id)

    async def test_terminal_route_cannot_resurrect_an_ephemeral_side(self) -> None:
        self.store.transition_side_topic(self.route.id, SideTopicState.FAILED)

        with self.assertRaises(SideSessionConflict):
            await self.create_side()

        self.assertEqual(self.codex.fork_calls, [])

    async def test_disabled_project_retains_existing_binding_side_behavior(self) -> None:
        project = self.store.get_project("test")
        self.store.set_project_enabled(
            alias=project.alias,
            enabled=False,
            expected_revision=project.revision,
        )

        created = await self.create_side()

        self.assertEqual(
            self.runtime.side_snapshot(self.route.id).thread_id,
            created.thread_id,
        )

    async def test_terminal_route_with_retained_session_still_needs_cleanup(self) -> None:
        created = await self.create_side()
        self.cleanup.failures.append(RuntimeError("cleanup response lost"))
        with self.assertRaises(SideCloseFailed):
            await self.runtime.close_side_exact(
                self.route.id,
                state=SideTopicState.FAILED,
            )
        self.assertEqual(
            self.runtime.side_snapshot(self.route.id).state,
            SideSessionState.CLOSING,
        )
        self.store.transition_side_topic(self.route.id, SideTopicState.FAILED)

        outcome = await self.runtime.close_side_exact(
            self.route.id,
            state=SideTopicState.FAILED,
        )

        self.assertIsNone(outcome.error)
        self.assertEqual(self.cleanup.calls, [created.thread_id, created.thread_id])
        self.assertEqual(self.side_control.unsubscribe_calls, [created.thread_id])
        self.assertEqual(
            self.store.get_side_topic(self.route.id).state,
            SideTopicState.FAILED,
        )
        with self.assertRaises(SideSessionNotFound):
            self.runtime.side_snapshot(self.route.id)

    async def test_project_side_projection_is_filtered_bounded_and_local(self) -> None:
        created = await self.create_side()
        second_route = self.store.create_side_topic(
            app_id=self.scope.app_id,
            chat_id=self.scope.chat_id,
            source_message_id="om-second-side",
            parent_binding_id=self.binding.id,
            creator_id="ou_owner",
            requires_mention=False,
        )
        second = await self.runtime.create_side(
            side_id=second_route.id,
            binding=self.binding,
            cwd=self.cwd,
            creator_id="ou_owner",
        )
        before = (len(self.codex.resume_calls), len(self.codex.fork_calls))

        snapshots = self.runtime.project_side_snapshots("test", limit=2)

        self.assertEqual({item.side_id for item in snapshots}, {created.side_id, second.side_id})
        self.assertEqual(self.runtime.project_side_snapshots("another-project", limit=1), ())
        with self.assertRaises(ValueError):
            self.runtime.project_side_snapshots("test", limit=1)
        self.assertEqual((len(self.codex.resume_calls), len(self.codex.fork_calls)), before)

    async def test_project_deletion_closes_side_after_ordinary_parent_was_deleted(self) -> None:
        created = await self.create_side()
        self.store.set_side_topic_root(created.side_id, "om-published-root")
        await self.runtime.attach_side_topic(
            side_id=created.side_id,
            topic_id="omt-published-side",
            root_message_id="om-published-root",
        )
        self.store.open_side_topic(created.side_id, "omt-published-side")
        delete_control = fixtures.FakeThreadDeleteControl()
        self.runtime._thread_delete_control = delete_control
        await self.runtime.delete_exact(
            self.binding.id,
            expected_native_thread_id=self.binding.native_thread_id,
        )
        self.assertEqual(self.runtime.project_side_snapshots("test")[0].side_id, created.side_id)
        projects = ProjectRegistry(store=self.store, project_root=self.cwd, projects={})
        service = InstanceManagementService(
            bindings=self.store,
            projects=projects,
            runtime=ManagementRuntimePort(self.runtime),
            scope_coordinator=ScopeCoordinator(),
        )
        try:
            snapshot = await service.preview_project_delete(
                alias="test",
                expected_revision=self.store.get_project("test").revision,
                deadline=asyncio.get_running_loop().time() + 5,
            )
            self.assertEqual(snapshot.bindings, ())
            self.assertEqual(tuple(item.id for item in snapshot.sides), (created.side_id,))
            result = await service.delete_project(
                alias="test",
                expected_revision=snapshot.project.revision,
                expected_inventory_fingerprint=snapshot.fingerprint,
            )
        finally:
            await service.close()

        self.assertTrue(result.deleted)
        self.assertEqual(result.deleted_session_count, 0)
        self.assertEqual(result.remaining_side_count, 0)
        self.assertEqual(self.runtime.project_side_snapshots("test"), ())
        self.assertEqual(self.store.get_side_topic(created.side_id).state, SideTopicState.CLOSED)
        self.assertEqual(delete_control.calls, ["native-parent"])
        self.assertEqual(self.side_control.unsubscribe_calls, [created.thread_id])
        with self.assertRaises(UnknownProject):
            projects.resolve("test")

    async def test_inflight_ordinary_materialization_stales_both_lazy_deletes(self) -> None:
        await self._assert_inflight_materialization_keeps_native_binding(goal=False)

    async def test_inflight_goal_materialization_stales_both_lazy_deletes(self) -> None:
        await self._assert_inflight_materialization_keeps_native_binding(goal=True)

    async def _assert_inflight_materialization_keeps_native_binding(
        self,
        *,
        goal: bool,
    ) -> None:
        binding = self.store.create_binding(
            scope=FeishuScope("cli_test", "oc_lazy", ScopeKind.DIRECT),
            project_alias="test",
            creator_id="ou_owner",
        )
        delete_control = fixtures.FakeThreadDeleteControl()
        self.runtime._thread_delete_control = delete_control
        entered = asyncio.Event()
        release = asyncio.Event()
        original_start = self.codex.thread_start

        async def blocked_start(**kwargs):
            thread = await original_start(**kwargs)
            entered.set()
            await release.wait()
            return thread

        self.codex.thread_start = blocked_start
        if goal:
            self.runtime._goal_control = fixtures.FakeGoalControl(self.codex)
            operation = self.runtime.start_goal(
                binding=binding,
                cwd=self.cwd,
                objective="finish the admitted task",
                owner_id="ou_owner",
                origin=object(),
            )
        else:
            operation = self.runtime.submit(
                binding=binding,
                cwd=self.cwd,
                input="finish the admitted task",
                owner_id="ou_owner",
                origin=object(),
            )
        starting = asyncio.create_task(operation)
        self.tasks.append(starting)
        await asyncio.wait_for(entered.wait(), 1)
        self.begin_delete()
        exact_delete = asyncio.create_task(
            self.runtime.delete_exact(binding.id, expected_native_thread_id=None)
        )
        lazy_delete = asyncio.create_task(self.runtime.delete_lazy_exact(binding.id))
        self.tasks.extend((exact_delete, lazy_delete))
        await asyncio.sleep(0)

        self.assertFalse(exact_delete.done())
        self.assertFalse(lazy_delete.done())
        self.assertIsNone(self.store.get(binding.id).native_thread_id)
        self.assertEqual(delete_control.calls, [])
        release.set()
        started = await asyncio.wait_for(starting, 1)
        assert started.release_receipt_attempt is not None
        started.release_receipt_attempt()
        with self.assertRaises(ThreadDeleteTargetChanged):
            await exact_delete
        with self.assertRaises(ThreadDeleteUnavailable):
            await lazy_delete

        self.assertEqual(
            self.store.get(binding.id).native_thread_id,
            started.thread_id,
        )
        self.assertEqual(delete_control.calls, [])
        self.assertFalse(self.store.get_project("test").enabled)
        self.assertTrue(self.runtime._accepting)
