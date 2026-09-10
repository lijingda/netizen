from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from netizen.bindings import BindingStore
from netizen.runtime.contracts import TurnObservationUnavailable
from netizen.schedules.models import ScheduleRule
from netizen.schedules.scheduler import Scheduler


class Reader:
    def __init__(self):
        self.status = {}
        self.calls = []
        self.gate = None

    async def read_scheduled_turn(self, binding_id, turn_id, *, deadline=None):
        self.calls.append((binding_id, turn_id))
        if self.gate is not None:
            await self.gate.wait()
        result = self.status.get(turn_id, "inProgress")
        if isinstance(result, Exception):
            raise result
        return result


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 100.0
        self.directory = tempfile.TemporaryDirectory()
        self.bindings = BindingStore(wall_clock=lambda: self.now)
        self.bindings.register_project(alias="p", cwd=self.directory.name)
        other = Path(self.directory.name) / "other"
        other.mkdir()
        self.bindings.register_project(alias="q", cwd=str(other))
        self.store = self.bindings.schedules
        self.reader = Reader()
        self.dispatched = []
        self.dispatch_gate = None
        self.sequence = 0

        async def dispatch(claim):
            self.dispatched.append(claim)
            if self.dispatch_gate is not None:
                await self.dispatch_gate.wait()
            self.native(claim.run.id)

        self.dispatch = dispatch
        self.scheduler = self.make_scheduler()

    async def asyncTearDown(self):
        await self.scheduler.close()
        await self.scheduler.drain(asyncio.get_running_loop().time() + 0.05)
        self.bindings.close()
        self.directory.cleanup()

    def make_scheduler(self, dispatch=None):
        return Scheduler(self.bindings, self.reader, "app", dispatch or self.dispatch, lambda: self.now)

    def plan(self, *, project="p", app_id="app", once=False, schedule=None):
        self.sequence += 1
        schedule = schedule or (
            ScheduleRule("once", "UTC", at="1970-01-01T00:03+00:00") if once
            else ScheduleRule("interval", "UTC", every_minutes=1, anchor=100)
        )
        return self.store.create(name="Plan " + str(self.sequence), instructions="Original instructions",
                                 project_alias=project, app_id=app_id, chat_id="chat",
                                 schedule=schedule, request_id="create-" + str(self.sequence), now=100).plan_id

    def occurrence(self, *, phase="claimed", app_id="app"):
        plan_id = self.plan(app_id=app_id)
        claim = self.store.claim_due(plan_id, app_id=app_id, now=160)
        self.store.set_run(claim.run.id, phase=phase)
        return self.store.get_run(claim.run.id)

    def native(self, run_id, status="inProgress"):
        turn_id = "turn-" + run_id
        self.reader.status[turn_id] = status
        return self.store.set_run(run_id, phase="handed_off", binding_id="binding-" + run_id, initial_turn_id=turn_id)

    async def until(self, predicate):
        async with asyncio.timeout(1):
            while not predicate():
                await asyncio.sleep(0)

    async def start(self):
        await self.scheduler.recover()
        self.scheduler.start()

    async def test_due_tasks_dispatch_concurrently_and_claim_only_once(self):
        self.plan()
        self.plan()
        self.dispatch_gate = asyncio.Event()
        await self.start()
        self.now = 160
        self.assertEqual(await self.scheduler.tick(), 2)
        await self.until(lambda: len(self.dispatched) == 2)
        self.assertEqual(await self.scheduler.tick(), 0)
        self.assertFalse(await self.scheduler.drain_project_creation("p", asyncio.get_running_loop().time()))
        self.dispatch_gate.set()
        self.assertTrue(await self.scheduler.drain(asyncio.get_running_loop().time() + 1))

    async def test_cutoff_waits_for_dispatch_settlement_before_ending(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def dispatch(claim):
            self.dispatched.append(claim)
            entered.set()
            await release.wait()
            raise RuntimeError("fixture startup failed")

        self.scheduler = self.make_scheduler(dispatch)
        plan_id = self.plan(schedule=ScheduleRule("interval", "UTC", every_minutes=1, anchor=100, end_at="1970-01-01T00:03Z"))
        await self.start()
        self.now = 160
        self.assertEqual(await self.scheduler.tick(), 1)
        await self.until(entered.is_set)
        self.assertIsNone(self.store.get(plan_id).next_due_at)
        self.assertEqual(self.store.list(app_id="app", ended=True, now=160), ())
        self.now = 220
        self.assertEqual(await self.scheduler.tick(), 0)
        with self.assertLogs("netizen.schedules.scheduler", level="WARNING"):
            release.set()
            self.assertTrue(await self.scheduler.drain(asyncio.get_running_loop().time() + 1))
        self.assertEqual(len(self.dispatched), 1)
        self.assertEqual(self.store.list_runs(plan_id)[0].error_code, "dispatch_failed")
        self.assertEqual([plan.id for plan in self.store.list(app_id="app", ended=True, now=220)], [plan_id])

    async def test_recovery_misses_before_cutoff_and_prevents_later_dispatch(self):
        plan_id = self.plan(schedule=ScheduleRule("interval", "UTC", every_minutes=1, anchor=0,
                                                end_at="1970-01-01T00:03Z"))
        self.now = 121
        await self.start()
        self.assertEqual(self.store.list_runs(plan_id)[0].error_code, "missed")
        self.now = 180
        self.assertEqual(await self.scheduler.tick(), 1)
        self.assertTrue(await self.scheduler.drain(asyncio.get_running_loop().time() + 1))
        last = self.store.get_run(self.dispatched[0].run.id)
        self.assertEqual(last.due_at, 180)
        self.reader.status[last.initial_turn_id] = "completed"
        self.assertEqual(await self.scheduler.refresh(plan_id), "completed")
        self.now = 240
        self.assertEqual(await self.scheduler.tick(), 0)
        self.assertEqual(len(self.dispatched), 1)
        self.assertEqual([plan.id for plan in self.store.list(app_id="app", ended=True, now=240)], [plan_id])

    async def test_busy_skips_without_polling_fresh_ordinary_consumer(self):
        plan_id = self.plan()
        await self.start()
        self.now = 160
        await self.scheduler.tick()
        await self.scheduler.drain(asyncio.get_running_loop().time() + 1)
        run = self.dispatched[0].run
        self.now = 220
        self.assertEqual(await self.scheduler.tick(), 0)
        self.assertEqual(self.store.list_runs(plan_id)[0].error_code, "skipped_busy")
        self.assertEqual(self.reader.calls, [])
        self.store.release(run.id)
        self.now = 280
        self.assertEqual(await self.scheduler.tick(), 1)

    async def test_clock_jump_skips_backlog_and_rollback_never_replays(self):
        plan_id = self.plan()
        await self.start()
        self.now = 340
        self.assertEqual(await self.scheduler.tick(), 1)
        await self.scheduler.drain(asyncio.get_running_loop().time() + 1)
        self.assertEqual(self.dispatched[0].run.due_at, 340)
        missed = [run for run in self.store.list_runs(plan_id) if run.error_code == "missed"]
        self.assertEqual(missed[0].missed_count, 3)
        self.store.release(self.dispatched[0].run.id)
        self.now = 160
        self.assertEqual(await self.scheduler.tick(), 0)
        self.now = 340
        self.assertEqual(await self.scheduler.tick(), 0)

    async def test_mutations_after_claim_keep_the_dispatch_snapshot(self):
        plan_id = self.plan()
        self.dispatch_gate = asyncio.Event()
        await self.start()
        self.now = 160
        await self.scheduler.tick()
        await self.until(lambda: bool(self.dispatched))
        self.store.update(plan_id, expected_revision=1, request_id="update", changes={"instructions": "New instructions", "enabled": False})
        self.store.delete(plan_id, expected_revision=2, request_id="delete")
        self.dispatch_gate.set()
        await self.scheduler.drain(asyncio.get_running_loop().time() + 1)
        self.assertEqual(self.dispatched[0].plan.instructions, "Original instructions")
        self.assertEqual(self.store.get_run(self.dispatched[0].run.id).phase, "handed_off")
        self.now = 220
        self.assertEqual(await self.scheduler.tick(), 0)

    async def test_restart_skips_offline_points_including_once(self):
        recurring = self.plan()
        once = self.plan(once=True)
        self.now = 240
        await self.start()
        self.assertEqual(self.dispatched, [])
        self.assertEqual(self.store.list_runs(recurring)[0].missed_count, 2)
        self.assertEqual(self.store.list_runs(once)[0].error_code, "missed")
        self.assertIsNone(self.store.get(once).next_due_at)
        self.now = 280
        self.assertEqual(await self.scheduler.tick(), 1)

    async def test_recovery_releases_only_proven_pre_native_phases(self):
        rows = {phase: self.occurrence(phase=phase) for phase in ("claimed", "publishing_topic", "binding_ready", "starting_turn")}
        other_app = self.occurrence(app_id="old-app")
        self.now = 200
        await self.scheduler.recover()
        for phase in ("claimed", "publishing_topic", "binding_ready"):
            run = self.store.get_run(rows[phase].id)
            self.assertEqual(run.barrier, "released")
            self.assertEqual(run.error_code, "publishing_unknown" if phase == "publishing_topic" else "recovery_no_start")
        self.assertEqual(self.store.get_run(rows["starting_turn"].id).barrier, "unknown")
        self.assertEqual(self.store.get_run(other_app.id).barrier, "held")
        self.assertEqual(self.reader.calls, [])

    async def test_recovered_initial_turn_is_rechecked_only_at_due_or_refresh(self):
        run = self.native(self.occurrence().id)
        self.now = 200
        await self.start()
        self.assertEqual(self.reader.calls, [(run.binding_id, run.initial_turn_id)])
        await self.scheduler.tick()
        self.assertEqual(len(self.reader.calls), 1)
        self.now = 220
        self.assertEqual(await self.scheduler.tick(), 0)
        self.assertEqual(len(self.reader.calls), 2)
        self.reader.status[run.initial_turn_id] = "completed"
        self.now = 280
        self.assertEqual(await self.scheduler.tick(), 1)
        self.assertEqual(self.store.get_run(run.id).barrier, "released")
        self.assertEqual(len(self.reader.calls), 3)

    async def test_read_failure_stops_automatic_rechecks_but_explicit_refresh_works(self):
        run = self.native(self.occurrence().id)
        self.reader.status[run.initial_turn_id] = TurnObservationUnavailable("ordinary observation stopped")
        self.now = 200
        await self.start()
        self.assertEqual(self.store.get_run(run.id).barrier, "unknown")
        self.now = 220
        await self.scheduler.tick()
        self.now = 280
        await self.scheduler.tick()
        self.assertEqual(len(self.reader.calls), 1)
        self.reader.status[run.initial_turn_id] = "inProgress"
        self.assertEqual(await self.scheduler.refresh(run.plan_id), "inProgress")
        self.assertEqual(self.store.get_run(run.id).barrier, "held")
        self.reader.status[run.initial_turn_id] = "interrupted"
        self.assertEqual(await self.scheduler.refresh(run.plan_id), "interrupted")
        self.assertEqual(self.store.get_run(run.id).barrier, "released")

    async def test_persisted_unknown_is_not_retried_at_restart(self):
        run = self.native(self.occurrence().id)
        self.store.set_run(run.id, barrier="unknown", error_code="read_unavailable")
        self.now = 200
        await self.scheduler.recover()
        self.assertEqual(self.reader.calls, [])
        self.assertEqual(self.store.get_run(run.id).barrier, "unknown")

    async def test_restart_marks_unfinished_final_delivery_unknown_without_replaying(self):
        run = self.native(self.occurrence().id, "completed")
        self.store.release(run.id)
        self.now = 200
        await self.scheduler.recover()
        self.assertEqual(self.store.get_run(run.id).delivery_state, "unknown")
        self.assertEqual(self.reader.calls, [])
        self.assertEqual(self.dispatched, [])

    async def test_restart_abandons_delivery_before_reconciling_pending_turns(self):
        completed = self.native(self.occurrence().id, "completed")
        running = self.native(self.occurrence().id, "inProgress")
        unknown = self.native(self.occurrence().id)
        self.store.set_run(unknown.id, barrier="unknown")
        self.now = 200
        await self.scheduler.recover()
        for run in (completed, running, unknown):
            self.assertEqual(self.store.get_run(run.id).delivery_state, "unknown")
        self.assertEqual(self.store.get_run(completed.id).barrier, "released")
        self.assertEqual(self.store.get_run(running.id).barrier, "held")
        self.assertEqual(self.store.get_run(unknown.id).barrier, "unknown")
        self.assertEqual(len(self.reader.calls), 2)
        self.assertEqual(self.dispatched, [])

    async def test_recovery_read_budget_is_shared_and_does_not_block_startup(self):
        first = self.native(self.occurrence().id)
        second = self.native(self.occurrence().id)
        self.reader.gate = asyncio.Event()
        self.now = 200
        with patch("netizen.schedules.scheduler.RECOVERY_TIMEOUT_SECONDS", 0.01):
            async with asyncio.timeout(0.2):
                await self.scheduler.recover()
        self.assertEqual(self.store.get_run(first.id).barrier, "unknown")
        self.assertEqual(self.store.get_run(second.id).barrier, "unknown")
        self.assertEqual(len(self.reader.calls), 1)

    async def test_terminal_release_and_pruning_can_finish_before_dispatch_returns(self):
        async def dispatch(claim):
            self.native(claim.run.id, "completed")
            # A long initial Turn may accumulate >100 skipped occurrences. Its
            # final delivery can release/prune it before the starter returns.
            for occurrence in range(101):
                self.store.claim_due(claim.plan.id, app_id="app", now=220 + 60 * occurrence)
            self.store.release(claim.run.id)
            self.store.set_run(claim.run.id, delivery_state="sent")

        self.scheduler = self.make_scheduler(dispatch)
        plan_id = self.plan()
        await self.start()
        self.now = 160
        await self.scheduler.tick()
        await self.scheduler.drain(asyncio.get_running_loop().time() + 1)
        self.assertIsNone(self.store.pending_for_plan(plan_id))
        self.now = 220 + 60 * 101
        self.assertEqual(await self.scheduler.tick(), 1)

    async def test_dispatch_timeout_classification_uses_persisted_phase(self):
        async def dispatch(claim):
            self.store.set_run(claim.run.id, phase="starting_turn")
            await asyncio.Event().wait()

        self.scheduler = self.make_scheduler(dispatch)
        plan_id = self.plan()
        await self.start()
        self.now = 160
        with patch("netizen.schedules.scheduler.DISPATCH_TIMEOUT_SECONDS", 0.01):
            await self.scheduler.tick()
            await self.scheduler.drain(asyncio.get_running_loop().time() + 1)
        run = self.store.pending_for_plan(plan_id)
        self.assertEqual(run.barrier, "unknown")
        self.assertEqual(run.error_code, "dispatch_timeout")

    async def test_shutdown_cancellation_retains_unknown_publication_evidence(self):
        entered = asyncio.Event()

        async def dispatch(claim):
            self.store.set_run(claim.run.id, phase="publishing_topic")
            entered.set()
            await asyncio.Event().wait()

        self.scheduler = self.make_scheduler(dispatch)
        plan_id = self.plan()
        await self.start()
        self.now = 160
        await self.scheduler.tick()
        await entered.wait()
        await self.scheduler.close()
        self.assertFalse(await self.scheduler.drain(asyncio.get_running_loop().time()))
        run = self.store.list_runs(plan_id)[0]
        self.assertEqual((run.barrier, run.error_code), ("released", "publishing_unknown"))

    async def test_outer_cleanup_cancellation_settles_and_cancels_dispatch(self):
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def dispatch(claim):
            self.store.set_run(claim.run.id, phase="starting_turn")
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.scheduler = self.make_scheduler(dispatch)
        plan_id = self.plan()
        await self.start()
        self.now = 160
        await self.scheduler.tick()
        await entered.wait()
        await self.scheduler.close()
        draining = asyncio.create_task(self.scheduler.drain(asyncio.get_running_loop().time() + 60))
        await asyncio.sleep(0)
        draining.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await draining
        self.assertTrue(cancelled.is_set())
        run = self.store.pending_for_plan(plan_id)
        self.assertEqual((run.barrier, run.error_code), ("unknown", "dispatch_cancelled"))

    async def test_project_drain_does_not_cancel_or_wait_for_other_projects(self):
        self.plan(project="q")
        self.dispatch_gate = asyncio.Event()
        await self.start()
        self.now = 160
        await self.scheduler.tick()
        await self.until(lambda: bool(self.dispatched))
        deadline = asyncio.get_running_loop().time()
        self.assertTrue(await self.scheduler.drain_project_creation("p", deadline))
        self.assertFalse(await self.scheduler.drain_project_creation("q", deadline))
        self.assertEqual(self.store.get_run(self.dispatched[0].run.id).barrier, "held")
        self.dispatch_gate.set()
        self.assertTrue(await self.scheduler.drain_project_creation("q", deadline + 1))
        self.assertEqual(self.store.get_run(self.dispatched[0].run.id).phase, "handed_off")

    async def test_wake_runs_timer_and_close_stops_new_claims(self):
        self.plan()
        await self.start()
        self.now = 160
        self.scheduler.wake()
        await self.until(lambda: bool(self.dispatched))
        await self.scheduler.close()
        await self.scheduler.drain(asyncio.get_running_loop().time() + 1)
        self.now = 280
        self.assertEqual(await self.scheduler.tick(), 0)
        self.assertEqual(len(self.dispatched), 1)


if __name__ == "__main__":
    unittest.main()
