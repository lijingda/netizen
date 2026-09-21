from __future__ import annotations

import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from netizen.bindings import BindingStore
from netizen.domain import FeishuScope, ScopeKind
from netizen.schedules.models import ScheduleRule
from netizen.schedules.scheduler import Scheduler
from netizen.schedules.service import ScheduleService


class ScheduleRunNowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.now = 100.0
        self.store = BindingStore(wall_clock=lambda: self.now)
        self.store.register_project(alias="p", cwd=self.directory.name)
        self.scope = FeishuScope("app", "calling-chat", ScopeKind.GROUP)
        self.binding = self.store.create_channel_binding(
            scope=self.scope, project_alias="p", creator_id="person",
        )
        self.store.assign_native_thread_id(self.binding.id, "calling-thread")
        self.runtime = SimpleNamespace(read_scheduled_turn=AsyncMock(return_value="inProgress"))
        self.dispatch_gate = asyncio.Event()
        self.dispatched = []

        async def dispatch(claim):
            self.dispatched.append(claim)
            await self.dispatch_gate.wait()
            self.store.schedules.release(claim.run.id, error_code="fixture_finished")

        self.scheduler = Scheduler(self.store, self.runtime, "app", dispatch, lambda: self.now)
        self.service = ScheduleService(
            bindings=self.store, runtime=self.runtime, app_id="app",
            wall_clock=lambda: self.now, default_timezone="UTC",
        )
        self.service.set_run_now_handler(self.scheduler.run_now)
        self.service.set_refresh_handler(self.scheduler.refresh)
        await self.scheduler.recover()
        self.scheduler.start()
        self.plan = self.store.schedules.create(
            name="Saved plan", instructions="Saved work in the saved destination.",
            project_alias="p", app_id="app", chat_id="destination-chat",
            schedule=ScheduleRule("interval", "UTC", every_minutes=1, anchor=100),
            request_id="create", now=self.now,
        )
        self.request = {"mode": "run_now", "plan_id": self.plan.plan_id,
                        "expected_revision": 1, "request_id": "manual-request"}

    async def asyncTearDown(self):
        await self.scheduler.close()
        self.dispatch_gate.set()
        await self.scheduler.drain(asyncio.get_running_loop().time() + 1)
        self.store.close()
        self.directory.cleanup()

    async def trigger(self, **changes):
        return await self.service.manage(
            {**self.request, **changes}, native_thread_id="calling-thread",
        )

    async def until(self, predicate):
        async with asyncio.timeout(1):
            while not predicate():
                await asyncio.sleep(0)

    async def test_receipt_precedes_dispatch_and_uses_saved_plan_without_switching_source(self):
        before = self.store.schedules.get(self.plan.plan_id)
        receipt = await self.trigger()
        self.assertTrue(receipt["accepted"], receipt)
        self.assertFalse(receipt["replayed"])
        self.assertEqual(receipt["revision"], before.revision)
        self.assertEqual(receipt["run"]["id"], receipt["run_id"])
        self.assertEqual(receipt["run"]["status"], "starting")
        self.assertEqual(receipt["run"]["trigger_source"], "manual")
        self.assertEqual(receipt["run"]["chat_id"], "destination-chat")
        self.assertIsNone(receipt["run"]["feishu_url"])
        self.assertEqual(self.store.schedules.get(before.id), before)
        self.assertEqual(self.dispatched, [])
        self.assertEqual(self.store.active_binding(self.scope.key).id, self.binding.id)
        await self.until(lambda: len(self.dispatched) == 1)
        self.assertEqual(self.dispatched[0].plan, before)
        self.assertFalse(await self.scheduler.drain_project_creation("p", asyncio.get_running_loop().time()))
        view = await self.service.manage({"mode": "view", "plan_id": before.id})
        self.assertEqual(view["plan"]["execution"]["trigger_source"], "manual")
        self.assertEqual(view["plan"]["latest_run"]["trigger_source"], "manual")

    async def test_duplicate_and_simultaneous_requests_do_not_duplicate_execution(self):
        first, duplicate, other = await asyncio.gather(
            self.trigger(), self.trigger(), self.trigger(request_id="different-request"),
        )
        self.assertTrue(first["ok"], first)
        self.assertEqual(duplicate["run_id"], first["run_id"])
        self.assertTrue(duplicate["replayed"])
        self.assertEqual(other["error"]["code"], "run_in_progress")
        await self.until(lambda: len(self.dispatched) == 1)
        self.assertEqual(len(self.store.schedules.list_runs(self.plan.plan_id)), 1)
        self.assertEqual(self.runtime.read_scheduled_turn.await_count, 0)

    async def test_manual_claim_competes_with_timer_using_the_same_barrier(self):
        self.now = 160
        receipt = await self.trigger()
        self.assertTrue(receipt["ok"], receipt)
        self.assertEqual(await self.scheduler.tick(), 0)
        await self.until(lambda: len(self.dispatched) == 1)
        runs = self.store.schedules.list_runs(self.plan.plan_id)
        self.assertEqual({run.trigger_source for run in runs}, {"manual", "scheduled"})
        skipped, = [run for run in runs if run.trigger_source == "scheduled"]
        self.assertEqual(skipped.error_code, "skipped_busy")
        self.assertEqual(self.store.schedules.get(self.plan.plan_id).next_due_at, 220)

    async def test_paused_and_ended_plans_run_without_changing_definition(self):
        for ended in (False, True):
            with self.subTest(ended=ended):
                current = self.store.schedules.get(self.plan.plan_id)
                changes = {"enabled": False}
                if ended:
                    changes["schedule"] = ScheduleRule("interval", "UTC", every_minutes=1,
                                                       anchor=100, end_at="1970-01-01T00:01Z")
                updated = self.store.schedules.update(
                    current.id, expected_revision=current.revision,
                    request_id=f"pause-{ended}", changes=changes, now=self.now,
                )
                before = self.store.schedules.get(current.id)
                receipt = await self.trigger(expected_revision=updated.revision, request_id=f"run-{ended}")
                self.assertTrue(receipt["ok"], receipt)
                self.assertEqual(self.store.schedules.get(current.id), before)
                self.dispatch_gate.set()
                self.assertTrue(await self.scheduler.drain(asyncio.get_running_loop().time() + 1))

    async def test_service_rejects_stale_foreign_override_and_unavailable_requests(self):
        for request, code in (
            ({"expected_revision": 2}, "revision_conflict"),
            ({"expected_revision": True}, "invalid_schedule"),
            ({"instructions": "override"}, "invalid_schedule"),
            ({"instructions": None}, "invalid_schedule"),
            ({"chat_id": "redirect"}, "invalid_schedule"),
            ({"session_settings": {}}, "invalid_schedule"),
            ({"enabled": True}, "invalid_schedule"),
        ):
            with self.subTest(request=request):
                result = await self.trigger(**request)
                self.assertEqual(result["error"]["code"], code)
        foreign = ScheduleService(bindings=self.store, runtime=self.runtime, app_id="other")
        foreign.set_run_now_handler(self.scheduler.run_now)
        self.assertEqual((await foreign.manage(self.request))["error"]["code"], "not_found")
        unbound = ScheduleService(bindings=self.store, runtime=self.runtime, app_id="app")
        self.assertEqual((await unbound.manage(self.request))["error"]["code"], "unavailable")
        self.scheduler.close_admission()
        self.assertEqual((await self.trigger())["error"]["code"], "unavailable")
        self.assertFalse(self.store.schedules.list_runs(self.plan.plan_id))

    async def test_unknown_remains_blocked_until_explicit_reconciliation(self):
        receipt = await self.trigger()
        self.store.schedules.set_run(receipt["run_id"], barrier="unknown", error_code="read_unavailable")
        result = await self.trigger(request_id="after-unknown")
        self.assertEqual(result["error"]["code"], "blocked_unknown")
        self.runtime.read_scheduled_turn.assert_not_awaited()

    async def test_replay_after_plan_deletion_keeps_exact_receipt_and_does_not_redispatch(self):
        original = await self.trigger()
        self.store.schedules.delete(self.plan.plan_id, expected_revision=1, request_id="delete", now=self.now)
        self.dispatch_gate.set()
        self.assertTrue(await self.scheduler.drain(asyncio.get_running_loop().time() + 1))
        replay = await self.trigger()
        self.assertTrue(replay["accepted"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["run_id"], original["run_id"])
        self.assertEqual(len(self.dispatched), 1)

    async def test_shutdown_accounts_for_manual_dispatch_and_replay_does_not_restart_it(self):
        receipt = await self.trigger()
        await self.until(lambda: len(self.dispatched) == 1)
        await self.scheduler.close()
        self.assertFalse(await self.scheduler.drain(asyncio.get_running_loop().time()))
        self.assertEqual(self.store.schedules.get_run(receipt["run_id"]).barrier, "released")
        self.assertEqual((await self.trigger())["run_id"], receipt["run_id"])
        fresh = await self.trigger(request_id="after-stop")
        self.assertEqual(fresh["error"]["code"], "unavailable")
        self.assertEqual(len(self.dispatched), 1)


if __name__ == "__main__":
    unittest.main()
