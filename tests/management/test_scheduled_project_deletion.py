from __future__ import annotations

import asyncio
import unittest

from netizen.bindings import BindingNotFound, ProjectDeleting
from netizen.domain import ActiveState, FeishuScope, ScopeKind
from netizen.management import ExactBindingTarget
from netizen.runtime.contracts import ActiveTurnSnapshot, BindingRuntimeSnapshot, ThreadLifecycleStateUnknown
from netizen.schedules.models import ScheduleNotFound, ScheduleRule
from tests.management import test_project_deletion as fixture


class ScheduledProjectDeletionTest(unittest.IsolatedAsyncioTestCase):
    """Real shared storage; the existing fake owns exact native deletion."""

    asyncTearDown = fixture.ProjectDeletionServiceTest.asyncTearDown
    binding = fixture.ProjectDeletionServiceTest.binding
    preview = fixture.ProjectDeletionServiceTest.preview
    delete = fixture.ProjectDeletionServiceTest.delete
    native_deletes = fixture.ProjectDeletionServiceTest.native_deletes
    assert_retained = fixture.ProjectDeletionServiceTest.assert_retained

    async def asyncSetUp(self) -> None:
        await fixture.ProjectDeletionServiceTest.asyncSetUp(self)
        self.plan_counter = 0

    def plan(self, *, project: str = "test"):
        self.plan_counter += 1
        result = self.store.schedules.create(
            name=f"Scheduled work {self.plan_counter}", instructions="Private instructions.",
            project_alias=project, app_id="cli_test", chat_id="oc_scheduled",
            schedule=ScheduleRule("interval", "UTC", every_minutes=1, anchor=0),
            request_id=f"create-{self.plan_counter}", now=0,
        )
        return self.store.schedules.get(result.plan_id)

    def claim(self, plan):
        claimed = self.store.schedules.claim_due(plan.id, app_id="cli_test", now=60)
        assert claimed is not None
        return claimed.run

    def scheduled_binding(self, plan, *, started: bool = True):
        run = self.claim(plan)
        self.store.schedules.begin_publication(run.id)
        self.store.schedules.set_run(
            run.id, root_message_id=f"om_root_{plan.id}", topic_id=f"omt_{plan.id}",
            origin_message_id=f"om_seed_{plan.id}",
        )
        binding = self.store.create_scheduled_binding(
            run_id=run.id,
            scope=FeishuScope(plan.app_id, plan.chat_id, ScopeKind.TOPIC, f"omt_{plan.id}"),
        )
        self.store.begin_scheduled_initial(run.id, binding.id)
        if started:
            self.store.assign_native_thread_id(binding.id, f"native-{plan.id}")
            self.store.mark_scheduled_turn_started(run.id, binding.id, f"turn-{plan.id}")
        return self.store.schedules.get_run(run.id), self.store.get(binding.id)

    def assert_tombstone(self, plan) -> None:
        with self.assertRaises(ScheduleNotFound):
            self.store.schedules.get(plan.id)
        deleted = self.store.schedules.get(plan.id, include_deleted=True)
        self.assertTrue(deleted.deleted)
        self.assertFalse(deleted.enabled)
        self.assertEqual(deleted.instructions, "")
        self.assertIsNone(deleted.schedule)
        self.assertIsNone(deleted.next_due_at)
        self.assertIsNone(self.store.schedules.claim_due(plan.id, app_id="cli_test", now=120))

    async def test_plan_only_project_deletion_tombstones_and_alias_reuse_never_revives_it(self) -> None:
        plan = self.plan()
        snapshot = await self.preview()
        self.assertEqual(snapshot.scheduled_plans, ((plan.id, plan.revision),))
        self.assertEqual(snapshot.scheduled_runs, ())
        result = await self.delete(snapshot)
        self.assertTrue(result.deleted)
        self.assertEqual(result.deleted_plan_count, 1)
        self.assertEqual(result.remaining_scheduled_run_count, 0)
        self.assertEqual(self.native_deletes(), [])
        self.assert_tombstone(plan)
        self.assertTrue(self.store.get_project("test", include_deleted=True).deleted)
        self.assertEqual(self.marker.read_text(), "project code stays")

        self.projects.register(alias="test", path=str(self.cwd), create_directory=False)
        self.assertTrue(self.store.get_project("test").enabled)
        self.assertEqual(self.store.schedules.list(app_id="cli_test", project_alias="test"), ())
        self.assert_tombstone(plan)
        fresh = self.plan()
        self.assertNotEqual(fresh.id, plan.id)
        self.assertEqual(self.store.schedules.due_plans(app_id="cli_test", now=60), (fresh,))

    async def test_running_scheduled_binding_uses_ordinary_exact_delete_without_prestop(self) -> None:
        plan = self.plan()
        run, binding = self.scheduled_binding(plan)
        self.runtime.runtime_snapshots[binding.id] = BindingRuntimeSnapshot(
            binding.id, 9, ActiveTurnSnapshot(binding.id, binding.native_thread_id,
                run.initial_turn_id, "scheduled_plan", ActiveState.RUNNING),
            None, False, None, None, None,
        )
        drain_calls = []

        async def drain(alias, deadline):
            drain_calls.append((alias, deadline))
            self.assertTrue(self.store.project_delete_in_progress(alias))
            self.assert_tombstone(plan)
            return True

        self.service.set_schedule_creation_drain(drain)
        result = await self.delete()
        self.assertTrue(result.deleted)
        self.assertEqual(result.deleted_plan_count, 1)
        self.assertEqual(result.deleted_session_count, 1)
        self.assertEqual(result.remaining_scheduled_run_count, 0)
        self.assertEqual(len(drain_calls), 1)
        self.assertEqual(self.native_deletes(), [("delete", binding.id, binding.native_thread_id)])
        forbidden = {"stop", "archive", "release", "recheck", "runtime-snapshot", "goal-snapshot"}
        self.assertFalse(any(call[0] in forbidden for call in self.runtime.calls))
        retired = self.store.schedules.get_run(run.id)
        self.assertEqual(retired.barrier, "released")
        self.assertTrue(retired.binding_removed)
        self.assertIsNone(self.store.active_binding(binding.scope_key))
        with self.assertRaises(BindingNotFound):
            self.store.get(binding.id)

    async def test_late_native_identity_during_drain_rejects_old_inventory_without_reviving_plan(self) -> None:
        plan = self.plan()
        run, binding = self.scheduled_binding(plan, started=False)
        snapshot = await self.preview()
        self.assertIsNone(snapshot.bindings[0].binding.native_thread_id)

        async def drain(_alias, _deadline):
            # The native start had already been dispatched before deletion froze
            # admission. Its late result is evidence, not a new permitted start.
            self.store.assign_native_thread_id(binding.id, "late-native")
            self.store.mark_scheduled_turn_started(run.id, binding.id, "late-turn")
            return True

        self.service.set_schedule_creation_drain(drain)
        result = await self.delete(snapshot)
        self.assertFalse(result.deleted)
        self.assertEqual(result.code, "inventory_changed")
        self.assertEqual(result.deleted_plan_count, 1)
        self.assertEqual(result.remaining_scheduled_run_count, 1)
        self.assertEqual(result.remaining_sessions[0].native_thread_id, "late-native")
        self.assertEqual(self.native_deletes(), [])
        self.assert_tombstone(plan)
        self.assert_retained(binding)
        current = await self.preview()
        self.assertNotEqual(current.fingerprint, snapshot.fingerprint)
        self.assertEqual(current.scheduled_plans, ())
        self.assertEqual(current.scheduled_runs[0].initial_turn_id, "late-turn")

    async def test_incomplete_drain_retains_project_and_exact_pending_run(self) -> None:
        plan = self.plan()
        run = self.claim(plan)
        calls = []

        async def drain(alias, deadline):
            calls.append((alias, deadline))
            return False

        self.service.set_schedule_creation_drain(drain)
        result = await self.delete()
        self.assertFalse(result.deleted)
        self.assertEqual(result.code, "schedule_creation_in_progress")
        self.assertEqual(result.deleted_plan_count, 1)
        self.assertEqual(result.remaining_scheduled_run_count, 1)
        self.assertEqual(self.store.schedules.project_pending_runs("test")[0].id, run.id)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.native_deletes(), [])
        self.assert_tombstone(plan)
        self.assert_retained()

    async def test_unknown_publication_retains_inventory_even_with_released_execution_barrier(self) -> None:
        plan = self.plan()
        run = self.claim(plan)
        self.store.schedules.begin_publication(run.id)
        # No native execution remains to block, but a possibly published Feishu
        # message cannot be discarded by deleting its schedule definition.
        self.store.schedules.release(run.id, error_code="publishing_unknown")
        result = await self.delete()
        self.assertFalse(result.deleted)
        self.assertEqual(result.code, "schedule_creation_in_progress")
        self.assertEqual(result.remaining_scheduled_run_count, 1)
        self.assert_tombstone(plan)
        self.assert_retained()
        snapshot = await self.preview()
        self.assertEqual(tuple(item.id for item in snapshot.scheduled_runs), (run.id,))
        self.assertEqual(snapshot.scheduled_runs[0].barrier, "released")

    async def test_partial_native_failure_does_not_restore_plans_or_release_unknown_run(self) -> None:
        first = self.binding("first", native="native-first")
        plan = self.plan()
        run, binding = self.scheduled_binding(plan)
        self.runtime.delete_errors[binding.id] = ThreadLifecycleStateUnknown("native response lost")
        result = await self.delete()
        self.assertFalse(result.deleted)
        self.assertEqual(result.code, "outcome_unknown")
        self.assertEqual(result.deleted_session_count, 1)
        self.assertEqual(result.deleted_plan_count, 1)
        self.assertEqual(result.remaining_scheduled_run_count, 1)
        self.assertEqual(tuple(item.id for item in result.remaining_sessions), (binding.id,))
        self.assertEqual(self.native_deletes(), [
            ("delete", first.id, "native-first"), ("delete", binding.id, binding.native_thread_id),
        ])
        self.assert_tombstone(plan)
        self.assert_retained(binding)
        self.assertFalse(self.store.schedules.get_run(run.id).binding_removed)
        self.assertNotEqual(self.store.schedules.get_run(run.id).barrier, "released")
        with self.assertRaises(BindingNotFound):
            self.store.get(first.id)

    async def test_waiting_for_schedule_drain_keeps_existing_scope_work_and_other_project_claims_available(self) -> None:
        plan = self.plan()
        _, scheduled = self.scheduled_binding(plan)
        ordinary = self.binding("ordinary", native="native-ordinary")
        other_plan = self.plan(project="other")
        entered, release = asyncio.Event(), asyncio.Event()

        async def drain(alias, _deadline):
            self.assertTrue(self.store.project_delete_in_progress(alias))
            entered.set()
            await release.wait()
            return False

        self.service.set_schedule_creation_drain(drain)
        task = asyncio.create_task(self.delete())
        self.tasks.append(task)
        await asyncio.wait_for(entered.wait(), 1)
        self.assert_tombstone(plan)
        with self.assertRaises(ProjectDeleting):
            self.binding("new-during-delete")
        # Existing work in the same canonical Project remains callable while
        # deletion waits for publication. No Scope or database lock spans await.
        async with asyncio.timeout(1):
            async with self.service.scope_coordinator.hold(scheduled.scope_key):
                pass
            renamed = await self.service.rename_exact_binding(
                target=ExactBindingTarget(ordinary.scope_key, ordinary.id, ordinary.id), name="Existing task",
            )
            self.assertEqual(renamed.binding.id, ordinary.id)
            project = await asyncio.to_thread(self.store.get_project, "test")
            self.assertFalse(project.enabled)
        self.assertIsNotNone(self.store.schedules.claim_due(other_plan.id, app_id="cli_test", now=60))
        self.assertFalse(task.done())
        self.assertEqual(self.native_deletes(), [])
        release.set()
        result = await asyncio.wait_for(task, 1)
        self.assertFalse(result.deleted)
        self.assertEqual(result.code, "schedule_creation_in_progress")
        self.assert_retained(scheduled, ordinary)
