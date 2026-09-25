from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from netizen.autonomy import AutonomyService, Candidate
from netizen.bindings import BindingStore, BindingTurnSettings
from netizen.codex_runtime import CodexRuntime, SteerRace, SubmitDisposition
from netizen.domain import FeishuScope, ScopeKind
from tests.test_codex_runtime import FakeCodex, FakeTerminalCleanup


class _ConsumeProvider:
    async def decide(self, config, state):
        return "consume"


class AutonomyRuntimeGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.cwd = Path(self.directory.name)
        self.store = BindingStore()
        self.codex = FakeCodex()
        self.runtime = CodexRuntime(
            codex=self.codex,
            bindings=self.store,
            terminal_cleanup=FakeTerminalCleanup(self.codex.events),
            poll_interval_seconds=0,
        )
        self.autonomy = AutonomyService(
            self.store.autonomy, self.cwd / "decision.json", provider=_ConsumeProvider(),
        )
        await self.autonomy.configure({"provider": "laya", "expected_revision": 0})

    async def asyncTearDown(self) -> None:
        await self.autonomy.aclose()
        try:
            await self.runtime.interrupt_all()
        finally:
            if not await self.runtime.wait_idle(timeout=0.1):
                await self.runtime.cancel_tasks()
            self.store.close()
            self.directory.cleanup()

    def binding(self, *, settings=None):
        return self.store.create_binding(
            scope=FeishuScope("cli_test", "oc_group", ScopeKind.GROUP),
            project_alias="project", creator_id="ou_user",
            autonomy_enabled=True, turn_settings=settings,
        )

    async def clear(self) -> None:
        await self.autonomy.configure({
            "expected_revision": self.autonomy.get_status()["revision"], "clear": True,
        })

    async def guarded_input(self, binding):
        admission = await self.runtime.capture_submission_admission(binding.id)
        decision = await self.autonomy.decide(binding.id, Candidate("om_candidate", "help"), str(self.cwd))
        self.assertEqual(decision.outcome, "consume")

        def guard():
            if not self.autonomy.token_current(decision.token):
                raise SteerRace("autonomous configuration was cleared")

        return dict(admission=admission, input_guard=guard)

    async def submit(self, binding, **kwargs):
        return await self.runtime.submit(
            binding=binding, cwd=self.cwd, input="candidate", owner_id="ou_user",
            origin=object(), **kwargs,
        )

    async def test_clear_while_resolving_model_rejects_before_thread_creation(self):
        binding = self.binding(settings=BindingTurnSettings("model", "effort", "tier"))
        kwargs = await self.guarded_input(binding)
        entered, release = asyncio.Event(), asyncio.Event()

        async def resolve(**kwargs):
            entered.set()
            await release.wait()
            return SimpleNamespace(model="model", effort=None, service_tier_id=None)

        with patch.object(self.runtime, "resolve_model_settings", side_effect=resolve):
            task = asyncio.create_task(self.submit(binding, **kwargs))
            await asyncio.wait_for(entered.wait(), 1)
            await self.clear()
            release.set()
            with self.assertRaisesRegex(SteerRace, "configuration was cleared"):
                await task
        self.assertEqual(self.codex.start_kwargs, [])
        self.assertEqual(self.codex.turn_inputs, [])
        await self.runtime.capture_submission_admission(binding.id)

    async def test_clear_while_waiting_binding_lock_rejects_before_thread_creation(self):
        binding = self.binding()
        kwargs = await self.guarded_input(binding)
        lock = self.runtime._lock(binding.id)
        async with lock:
            task = asyncio.create_task(self.submit(binding, **kwargs))
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            await self.clear()
        with self.assertRaisesRegex(SteerRace, "configuration was cleared"):
            await task
        self.assertEqual(self.codex.start_kwargs, [])
        self.assertEqual(self.codex.turn_inputs, [])
        await self.runtime.capture_submission_admission(binding.id)

    async def test_clear_during_goal_check_does_not_steer_existing_turn(self):
        binding = self.binding()
        first = await self.submit(binding)
        first.release_receipt_attempt()
        kwargs = await self.guarded_input(binding)
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.runtime._prompt_goal_locked

        async def goal_check(binding):
            entered.set()
            await release.wait()
            return await original(binding)

        with patch.object(self.runtime, "_prompt_goal_locked", side_effect=goal_check):
            task = asyncio.create_task(self.submit(binding, **kwargs))
            await asyncio.wait_for(entered.wait(), 1)
            await self.clear()
            release.set()
            with self.assertRaisesRegex(SteerRace, "configuration was cleared"):
                await task
        self.assertEqual(self.codex.handles[0].steers, [])
        self.assertEqual(self.codex.handles[0].interrupt_count, 0)
        # Clearing optional configuration leaves explicit input and the existing
        # Turn available, including its original exact identity.
        explicit = await self.submit(binding)
        self.assertEqual(explicit.disposition, SubmitDisposition.STEERED)
        self.assertEqual(explicit.turn_id, first.turn_id)

    async def test_clear_during_thread_start_retains_identity_but_sends_no_turn(self):
        binding = self.binding()
        kwargs = await self.guarded_input(binding)
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.codex.thread_start

        async def start(**kwargs):
            thread = await original(**kwargs)
            entered.set()
            await release.wait()
            return thread

        with patch.object(self.codex, "thread_start", side_effect=start):
            task = asyncio.create_task(self.submit(binding, **kwargs))
            await asyncio.wait_for(entered.wait(), 1)
            await self.clear()
            release.set()
            with self.assertRaisesRegex(SteerRace, "configuration was cleared"):
                await task
        self.assertEqual(self.codex.turn_inputs, [])
        self.assertEqual(self.store.get(binding.id).native_thread_id, "native-1")
        await self.runtime.capture_submission_admission(binding.id)
        self.codex.complete_immediately = True
        explicit = await self.submit(binding)
        explicit.release_receipt_attempt()
        self.assertEqual(explicit.thread_id, "native-1")
        self.assertEqual(len(self.codex.start_kwargs), 1)

    async def test_clear_during_thread_resume_sends_no_turn_and_keeps_runtime_open(self):
        binding = self.binding()
        self.store.assign_native_thread_id(binding.id, "persisted-thread")
        kwargs = await self.guarded_input(binding)
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.codex.thread_resume

        async def resume(thread_id, **kwargs):
            entered.set()
            await release.wait()
            return await original(thread_id, **kwargs)

        with patch.object(self.codex, "thread_resume", side_effect=resume):
            task = asyncio.create_task(self.submit(binding, **kwargs))
            await asyncio.wait_for(entered.wait(), 1)
            await self.clear()
            release.set()
            with self.assertRaisesRegex(SteerRace, "configuration was cleared"):
                await task
        self.assertEqual(self.codex.turn_inputs, [])
        self.assertEqual(self.store.get(binding.id).native_thread_id, "persisted-thread")
        await self.runtime.capture_submission_admission(binding.id)


if __name__ == "__main__":
    unittest.main()
