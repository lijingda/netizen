from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from netizen.bindings import BindingStore
from netizen.codex_runtime import CodexRuntime, SubmitDisposition
from netizen.domain import FeishuScope, ScopeKind
from netizen.management.coordination import ScopeCoordinator
from netizen.management.service import (
    CurrentBindingTarget,
    ExactBindingTarget,
    InstanceManagementService,
    ManagementRuntimePort,
)
from netizen.projects import ProjectRegistry
from tests.test_codex_runtime import (
    FakeCodex,
    FakeThread,
    FakeThreadDeleteControl,
    FakeThreadSubscriptionControl,
    FakeTerminalCleanup,
    token_usage_notification,
)


class NamingThread(FakeThread):
    async def read(self, *, include_turns=False):
        response = await super().read(include_turns=include_turns)
        response.thread.name = self.codex.names.get(self.id)
        response.thread.preview = "user request"
        if self.ephemeral:
            response.thread.path = None
        for turn in response.thread.turns:
            if not any(getattr(item.root, "type", None) == "userMessage" for item in turn.items):
                turn.items.insert(0, SimpleNamespace(root=SimpleNamespace(type="userMessage")))
        return response

    async def turn(self, input, **kwargs):
        handle = await super().turn(input, **kwargs)
        original_run = handle.run

        async def run():
            result = await original_run()
            result.id = handle.id
            return result

        handle.run = run
        return handle

    async def set_name(self, name):
        self.codex.set_name_calls.append((self.id, name))
        self.codex.write_entered.set()
        if name == "Automatic" and self.codex.write_gate is not None:
            await self.codex.write_gate.wait()
        if name in self.codex.manual_write_gates:
            await self.codex.manual_write_gates[name].wait()
        if self.codex.set_name_errors:
            raise self.codex.set_name_errors.pop(0)
        self.codex.names[self.id] = name


class NamingCodex(FakeCodex):
    def __init__(self):
        super().__init__()
        self.names = {}
        self.write_entered = asyncio.Event()
        self.write_gate = None
        self.manual_write_gates = {}
        self.fork_gate = None

    async def thread_start(self, **kwargs):
        thread = await super().thread_start(**kwargs)
        return NamingThread(thread.id, self)

    async def thread_resume(self, thread_id, **kwargs):
        thread = await super().thread_resume(thread_id, **kwargs)
        return NamingThread(thread.id, self)

    async def thread_fork(self, thread_id, **kwargs):
        self.fork_calls.append((thread_id, kwargs))
        if self.fork_gate is not None:
            await self.fork_gate.wait()
        if self.fork_errors:
            raise self.fork_errors.pop(0)
        return NamingThread(
            f"private-name-{len(self.fork_calls)}", self,
            ephemeral=True, forked_from_id=thread_id,
        )


async def eventually(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


class RuntimeThreadNamingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.cwd = Path(self.directory.name)
        self.store = BindingStore()
        self.store.register_project(alias="test", cwd=str(self.cwd))
        self.scope = FeishuScope("app", "chat", ScopeKind.DIRECT)
        self.binding = self.new_binding()
        self.codex = NamingCodex()
        self.subscription = FakeThreadSubscriptionControl()
        self.cleanup = FakeTerminalCleanup(self.codex.events)
        self.outcomes = []

        async def completed(outcome):
            self.outcomes.append(outcome)

        self.runtime = CodexRuntime(
            codex=self.codex, bindings=self.store,
            terminal_cleanup=self.cleanup,
            thread_subscription_control=self.subscription,
            thread_delete_control=FakeThreadDeleteControl(),
            on_completion=completed, poll_interval_seconds=0.001,
        )

    async def asyncTearDown(self):
        if self.codex.write_gate is not None:
            self.codex.write_gate.set()
        if self.codex.fork_gate is not None:
            self.codex.fork_gate.set()
        for gate in self.codex.manual_write_gates.values():
            gate.set()
        await eventually(lambda: not self.runtime._name_writes._tasks)
        await self.runtime.interrupt_all()
        await self.runtime.cancel_tasks()
        self.store.close()
        self.directory.cleanup()

    def new_binding(self):
        return self.store.create_binding(
            scope=self.scope, project_alias="test", creator_id="user",
        )

    async def submit(self, binding=None, text="user request"):
        submission = await self.runtime.submit(
            binding=binding or self.binding, cwd=self.cwd, input=text,
            owner_id="user", origin=object(),
        )
        if submission.release_receipt_attempt is not None:
            submission.release_receipt_attempt()
        return submission

    async def naming_handle(self):
        await eventually(lambda: any(h.thread_id.startswith("private-name-") for h in self.codex.handles))
        return next(h for h in reversed(self.codex.handles) if h.thread_id.startswith("private-name-"))

    async def finish_parent(self, turn_id):
        next(h for h in self.codex.handles if h.id == turn_id).complete()
        self.assertTrue(await self.runtime.wait_idle(timeout=1))

    async def test_new_turn_names_once_steer_and_private_work_are_invisible(self):
        first = await self.submit()
        child = await self.naming_handle()
        before = self.runtime.binding_runtime_snapshot(self.binding.id)
        steered = await self.submit(text="more detail")
        self.assertIs(steered.disposition, SubmitDisposition.STEERED)
        self.assertEqual(len(self.codex.fork_calls), 1)
        child.complete(response="Automatic")
        await eventually(lambda: self.codex.names.get(first.thread_id) == "Automatic")
        await eventually(lambda: child.thread_id in self.subscription.calls)
        self.assertIsNone(self.runtime.lifecycle_state(self.binding.id))
        self.assertEqual(self.runtime.project_side_snapshots("test"), ())
        self.assertEqual(len(self.store.list_bindings(self.scope.key)), 1)
        self.assertEqual(self.outcomes, [])
        self.assertEqual(self.runtime.active_turn(self.binding.id).turn_id, first.turn_id)
        self.assertEqual(self.runtime.binding_runtime_snapshot(self.binding.id).activity_revision,
                         before.activity_revision + 1)
        await self.finish_parent(first.turn_id)
        second = await self.submit()
        await eventually(lambda: not self.runtime._thread_namer._jobs)
        self.assertEqual(len(self.codex.fork_calls), 1)
        await self.finish_parent(second.turn_id)
        self.assertEqual(len(self.outcomes), 2)

    async def test_manual_rename_invalidates_generation_before_it_finishes(self):
        first = await self.submit()
        child = await self.naming_handle()
        self.assertEqual(await self.runtime.rename_exact(self.binding.id, "Manual"), "Manual")
        child.complete(response="Automatic")
        await eventually(lambda: not self.runtime._thread_namer._jobs)
        self.assertEqual(self.codex.names[first.thread_id], "Manual")
        self.assertNotIn((first.thread_id, "Automatic"), self.codex.set_name_calls)

    async def test_auxiliary_setup_failure_does_not_change_accepted_submission(self):
        with patch.object(self.runtime._thread_namer, "start", side_effect=RuntimeError("setup")):
            first = await self.submit()
        self.assertIs(first.disposition, SubmitDisposition.STARTED)
        self.assertTrue(self.runtime._accepting)
        await self.finish_parent(first.turn_id)
        self.assertEqual(len(self.outcomes), 1)

    async def test_missing_unsubscribe_capability_disables_only_automatic_naming(self):
        runtime = CodexRuntime(
            codex=self.codex, bindings=self.store, terminal_cleanup=self.cleanup,
            poll_interval_seconds=0.001,
        )
        try:
            first = await runtime.submit(
                binding=self.binding, cwd=self.cwd, input="user request",
                owner_id="user", origin=object(),
            )
            first.release_receipt_attempt()
            self.assertIs(first.disposition, SubmitDisposition.STARTED)
            self.assertIsNone(runtime._thread_namer)
            self.codex.handles[0].complete()
            self.assertTrue(await runtime.wait_idle(timeout=1))
            self.assertEqual(self.codex.fork_calls, [])
            self.assertEqual(await runtime.rename_exact(self.binding.id, "Manual"), "Manual")
        finally:
            await runtime.interrupt_all()
            await runtime.cancel_tasks()

    async def test_existing_native_name_before_write_is_preserved(self):
        first = await self.submit()
        child = await self.naming_handle()
        self.codex.names[first.thread_id] = "Changed elsewhere"
        child.complete(response="Automatic")
        await eventually(lambda: not self.runtime._thread_namer._jobs)
        self.assertEqual(self.codex.names[first.thread_id], "Changed elsewhere")
        self.assertEqual(self.codex.set_name_calls, [])

    async def test_project_deletion_freeze_prevents_write(self):
        first = await self.submit()
        child = await self.naming_handle()
        preview = self.store.preview_project_delete("test")
        self.store.begin_project_delete(
            alias="test", expected_revision=preview.project.revision,
            expected_inventory_fingerprint=preview.fingerprint,
        )
        await self.runtime.drain_project_side_creation(self.binding.id)
        child.complete(response="Automatic")
        await eventually(lambda: not self.runtime._thread_namer._jobs)
        self.assertNotIn(first.thread_id, self.codex.names)

    async def test_naming_usage_and_other_binding_stay_independent(self):
        first = await self.submit()
        child = await self.naming_handle()
        other = self.new_binding()
        second = await self.submit(other)
        await eventually(lambda: len(self.codex.fork_calls) == 2)
        child.notifications.put_nowait(token_usage_notification(
            thread_id=child.thread_id, turn_id=child.id,
        ))
        child.complete(response="Automatic")
        await eventually(lambda: self.codex.names.get(first.thread_id) == "Automatic")
        self.assertIsNone(self.runtime.context_window_usage(self.binding.id))
        self.assertIsNone(self.runtime.context_window_usage(other.id))
        self.assertEqual(self.runtime.active_turn(other.id).turn_id, second.turn_id)
        self.assertEqual(len(self.store.list_bindings(self.scope.key)), 2)
        self.assertEqual(self.outcomes, [])

    async def test_switching_scope_keeps_exact_original_target(self):
        first = await self.submit()
        child = await self.naming_handle()
        other = self.new_binding()
        child.complete(response="Automatic")
        await eventually(lambda: self.codex.names.get(first.thread_id) == "Automatic")
        self.assertEqual(self.store.active_binding(self.scope.key).id, other.id)

    async def test_archive_during_generation_discards_result_without_waiting(self):
        first = await self.submit()
        child = await self.naming_handle()
        await asyncio.wait_for(self.runtime.archive_exact(self.binding.id), 0.1)
        child.complete(response="Automatic")
        await eventually(lambda: not self.runtime._thread_namer._jobs)
        self.assertNotIn(first.thread_id, self.codex.names)
        self.assertIsNone(self.runtime.lifecycle_state(self.binding.id))

    async def test_delayed_automatic_write_does_not_hold_binding_or_block_steer(self):
        first = await self.submit()
        child = await self.naming_handle()
        self.codex.write_gate = asyncio.Event()
        child.complete(response="Automatic")
        await self.codex.write_entered.wait()
        self.assertFalse(self.runtime._lock(self.binding.id).locked())
        steered = await asyncio.wait_for(self.submit(text="keep working"), 0.1)
        self.assertIs(steered.disposition, SubmitDisposition.STEERED)
        manual = asyncio.create_task(self.runtime.rename_exact(self.binding.id, "Manual"))
        await asyncio.sleep(0)
        self.assertFalse(manual.done())
        self.assertIsNone(self.runtime.lifecycle_state(self.binding.id))
        await self.finish_parent(first.turn_id)
        second = await asyncio.wait_for(self.submit(), 0.1)
        self.assertIs(second.disposition, SubmitDisposition.STARTED)
        self.codex.write_gate.set()
        self.assertEqual(await asyncio.wait_for(manual, 1), "Manual")
        self.assertEqual(self.codex.names[first.thread_id], "Manual")

    async def test_waiting_manual_name_does_not_block_delete_or_write_deleted_target(self):
        first = await self.submit()
        child = await self.naming_handle()
        self.codex.write_gate = asyncio.Event()
        child.complete(response="Automatic")
        await self.codex.write_entered.wait()
        manual = asyncio.create_task(self.runtime.rename_exact(self.binding.id, "Manual"))
        await asyncio.sleep(0)
        self.assertFalse(manual.done())
        self.assertTrue(self.runtime._accepting)
        self.assertIsNone(self.runtime.lifecycle_state(self.binding.id))
        await asyncio.wait_for(self.runtime.delete_exact(
            self.binding.id, expected_native_thread_id=first.thread_id,
        ), 0.1)
        self.codex.write_gate.set()
        with self.assertRaises(LookupError):
            await asyncio.wait_for(manual, 1)
        self.assertNotIn((first.thread_id, "Manual"), self.codex.set_name_calls)

    async def test_management_rename_does_not_hold_scope_waiting_for_automatic_write(self):
        first = await self.submit()
        child = await self.naming_handle()
        self.codex.write_gate = asyncio.Event()
        child.complete(response="Automatic")
        await asyncio.wait_for(self.codex.write_entered.wait(), 2)
        coordinator = ScopeCoordinator()
        service = InstanceManagementService(
            bindings=self.store,
            projects=ProjectRegistry(store=self.store, project_root=self.cwd, projects={}),
            runtime=ManagementRuntimePort(self.runtime), scope_coordinator=coordinator,
        )
        try:
            calls = (
                (service.rename_current_binding, CurrentBindingTarget(self.scope.key, self.binding.id)),
                (service.rename_exact_binding, ExactBindingTarget(
                    self.scope.key, self.binding.id, self.binding.id,
                )),
            )
            pending = []
            for index, (rename, target) in enumerate(calls):
                pending.append(asyncio.create_task(rename(target=target, name=f"Manual-{index}")))
                await asyncio.sleep(0)
                self.assertFalse(pending[-1].done())
                async with asyncio.timeout(0.1):
                    async with coordinator.hold(self.scope.key):
                        steered = await self.submit(text="scope is available")
                self.assertIs(steered.disposition, SubmitDisposition.STEERED)
            # A subsequently accepted pointer switch must neither block nor
            # redirect either already accepted rename to the new Binding.
            async with asyncio.timeout(0.1):
                async with coordinator.hold(self.scope.key):
                    other = self.new_binding()
            self.codex.write_gate.set()
            results = await asyncio.wait_for(asyncio.gather(*pending), 1)
            self.assertEqual([result.name for result in results], ["Manual-0", "Manual-1"])
            self.assertEqual(self.codex.names[first.thread_id], "Manual-1")
            self.assertEqual(self.store.active_binding(self.scope.key).id, other.id)
            self.assertEqual(self.codex.set_name_calls[-2:], [
                (first.thread_id, "Manual-0"), (first.thread_id, "Manual-1"),
            ])
        finally:
            await asyncio.wait_for(service.close(), 1)

    async def test_automatic_result_drops_immediately_while_manual_writer_owns_lock(self):
        first = await self.submit()
        child = await self.naming_handle()
        self.codex.manual_write_gates["Manual"] = asyncio.Event()
        job = self.runtime._thread_namer._jobs[self.binding.id]
        manual = asyncio.create_task(self.runtime.rename_exact(self.binding.id, "Manual"))
        await asyncio.wait_for(self.codex.write_entered.wait(), 1)
        await asyncio.wait_for(self.runtime._commit_automatic_thread_name(job, "Automatic"), 0.1)
        self.assertNotIn((first.thread_id, "Automatic"), self.codex.set_name_calls)
        self.codex.manual_write_gates["Manual"].set()
        self.assertEqual(await asyncio.wait_for(manual, 1), "Manual")
        child.complete(response="Automatic")
        await eventually(lambda: not self.runtime._thread_namer._jobs)
        self.assertEqual(self.codex.names[first.thread_id], "Manual")

    async def test_cancelled_manual_waiter_does_not_write_or_block_later_manual(self):
        first = await self.submit()
        child = await self.naming_handle()
        self.codex.write_gate = asyncio.Event()
        child.complete(response="Automatic")
        await asyncio.wait_for(self.codex.write_entered.wait(), 1)
        cancelled = asyncio.create_task(self.runtime.rename_exact(self.binding.id, "Cancelled"))
        await asyncio.sleep(0)
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        latest = asyncio.create_task(self.runtime.rename_exact(self.binding.id, "Latest"))
        await asyncio.sleep(0)
        self.assertFalse(latest.done())
        self.codex.write_gate.set()
        await asyncio.wait_for(latest, 1)
        self.assertEqual(self.codex.names[first.thread_id], "Latest")
        self.assertNotIn((first.thread_id, "Cancelled"), self.codex.set_name_calls)

    async def test_cancelled_manual_caller_does_not_release_actual_write_lock(self):
        first = await self.submit()
        await self.naming_handle()
        self.codex.manual_write_gates["First"] = asyncio.Event()
        first_manual = asyncio.create_task(self.runtime.rename_exact(self.binding.id, "First"))
        await asyncio.wait_for(self.codex.write_entered.wait(), 1)
        first_manual.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first_manual
        latest = asyncio.create_task(self.runtime.rename_exact(self.binding.id, "Latest"))
        await asyncio.sleep(0)
        self.assertFalse(latest.done())
        self.assertNotIn((first.thread_id, "Latest"), self.codex.set_name_calls)
        self.codex.manual_write_gates["First"].set()
        await asyncio.wait_for(latest, 1)
        self.assertEqual(self.codex.names[first.thread_id], "Latest")
        self.assertIsNone(self.runtime.lifecycle_state(self.binding.id))

    async def test_failed_write_does_not_poison_binding_and_next_turn_retries(self):
        first = await self.submit()
        child = await self.naming_handle()
        self.codex.set_name_errors.append(RuntimeError("lost response"))
        child.complete(response="Automatic")
        await eventually(lambda: not self.runtime._thread_namer._jobs)
        self.assertTrue(self.runtime._accepting)
        self.assertIsNone(self.runtime.lifecycle_state(self.binding.id))
        await self.finish_parent(first.turn_id)
        await self.submit()
        await eventually(lambda: len(self.codex.fork_calls) == 2)

    async def test_unknown_fork_only_stops_automatic_attempts_not_main_or_manual_name(self):
        self.codex.fork_errors.append(RuntimeError("fork response lost"))
        first = await self.submit()
        await eventually(lambda: len(self.codex.fork_calls) == 1)
        await eventually(lambda: not self.runtime._thread_namer._tasks)
        await self.finish_parent(first.turn_id)
        second = await self.submit()
        self.assertIs(second.disposition, SubmitDisposition.STARTED)
        self.assertEqual(len(self.codex.fork_calls), 1)
        self.assertTrue(self.runtime._accepting)
        self.assertIsNone(self.runtime.lifecycle_state(self.binding.id))
        self.assertEqual(await self.runtime.rename_exact(self.binding.id, "Manual"), "Manual")
        self.assertEqual(self.codex.names[first.thread_id], "Manual")

    async def test_hanging_fork_does_not_block_stop_or_count_as_ordinary_work(self):
        self.codex.fork_gate = asyncio.Event()
        first = await self.submit()
        await eventually(lambda: len(self.codex.fork_calls) == 1)
        await self.finish_parent(first.turn_id)
        self.assertTrue(await self.runtime.wait_idle(timeout=0.01))
        second = await self.submit()
        self.assertIs(second.disposition, SubmitDisposition.STARTED)
        self.assertEqual(len(self.codex.fork_calls), 1)
        await asyncio.wait_for(self.runtime.stop(self.binding.id), 0.1)
        self.codex.fork_gate.set()

    async def test_hanging_naming_does_not_spend_main_shutdown_budget(self):
        self.codex.fork_gate = asyncio.Event()
        await self.submit()
        await eventually(lambda: len(self.codex.fork_calls) == 1)
        await asyncio.wait_for(self.runtime.interrupt_all(), 0.1)
        with patch("netizen.codex_runtime._NAMING_SHUTDOWN_WAIT_SECONDS", 0.01):
            await asyncio.wait_for(self.runtime.cancel_tasks(), 0.1)
        self.assertEqual(self.runtime._active, {})
        self.assertTrue(all(task.done() for task in self.runtime._tasks))
        self.codex.fork_gate.set()


if __name__ == "__main__":
    unittest.main()
