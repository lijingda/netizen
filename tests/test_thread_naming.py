from __future__ import annotations

import asyncio
import unittest
from copy import deepcopy
from types import SimpleNamespace

from openai_codex import InternalRpcError, InvalidRequestError

from netizen.model_settings import TurnModelSettings
from netizen.runtime.thread_naming import NAMING_PROMPT, NamingJob, ThreadNamer


class NamingHandle:
    def __init__(self, events: list[tuple[str, str]]) -> None:
        self.id = "name-turn"
        self.thread_id = "private-fork"
        self.events = events
        self.finished = asyncio.Event()
        self.running = asyncio.Event()
        self.interrupt_entered = asyncio.Event()
        self.interrupt_gate: asyncio.Event | None = None
        self.interrupt_error: Exception | None = None
        self.interrupt_completes = True
        self.error: Exception | None = None
        self.result = SimpleNamespace(
            id=self.id, status="completed", final_response="会话自动命名",
        )
        self.run_calls = 0
        self.run_cancelled = False

    async def run(self) -> object:
        self.run_calls += 1
        self.running.set()
        try:
            await self.finished.wait()
        except asyncio.CancelledError:
            self.run_cancelled = True
            raise
        if self.error is not None:
            raise self.error
        return self.result

    async def interrupt(self) -> object:
        self.events.append(("interrupt", self.thread_id))
        self.interrupt_entered.set()
        if self.interrupt_gate is not None:
            await self.interrupt_gate.wait()
        if self.interrupt_error is not None:
            raise self.interrupt_error
        if self.interrupt_completes:
            self.result.status = "interrupted"
            self.finished.set()
        return object()


class NamingThread:
    def __init__(
        self, thread_id: str, events: list[tuple[str, str]], *, ephemeral: bool,
    ) -> None:
        self.id = thread_id
        self.events = events
        self.native = SimpleNamespace(
            id=thread_id, name=None, ephemeral=ephemeral,
            path=None if ephemeral else "/tmp/parent.jsonl",
            forked_from_id="parent" if ephemeral else None,
            turns=[SimpleNamespace(
                id="parent-turn", status="inProgress",
                items=[SimpleNamespace(root=SimpleNamespace(type="userMessage"))],
            )],
        )
        self.read_gate: asyncio.Event | None = None
        self.read_entered = asyncio.Event()
        self.summary_read_count = 0
        self.summary_read_errors: list[Exception] = []
        self.full_read_entered = asyncio.Event()
        self.full_read_count = 0
        self.full_read_errors: list[Exception] = []
        self.read_error: Exception | None = None
        self.turn_gate: asyncio.Event | None = None
        self.turn_entered = asyncio.Event()
        self.turn_error: Exception | None = None
        self.turn_calls: list[tuple[str, dict[str, object]]] = []
        self.handle = NamingHandle(events)

    async def read(self, *, include_turns: bool = False) -> object:
        if include_turns:
            self.full_read_entered.set()
            self.full_read_count += 1
            if self.full_read_errors:
                raise self.full_read_errors.pop(0)
        else:
            self.summary_read_count += 1
            if self.summary_read_errors:
                raise self.summary_read_errors.pop(0)
        self.events.append(("read", self.id))
        self.read_entered.set()
        if self.read_gate is not None:
            await self.read_gate.wait()
        if self.read_error is not None:
            raise self.read_error
        return SimpleNamespace(thread=deepcopy(self.native))

    async def turn(self, input: str, **kwargs: object) -> NamingHandle:
        self.events.append(("turn", self.id))
        self.turn_calls.append((input, kwargs))
        self.turn_entered.set()
        if self.turn_gate is not None:
            await self.turn_gate.wait()
        if self.turn_error is not None:
            raise self.turn_error
        return self.handle


class NamingCodex:
    def __init__(self, events: list[tuple[str, str]]) -> None:
        self.child = NamingThread("private-fork", events, ephemeral=True)
        self.fork_gate: asyncio.Event | None = None
        self.fork_entered = asyncio.Event()
        self.fork_error: Exception | None = None
        self.fork_calls: list[tuple[str, dict[str, object]]] = []
        self.fork_cancelled = False

    async def thread_fork(self, thread_id: str, **kwargs: object) -> NamingThread:
        self.fork_calls.append((thread_id, kwargs))
        self.fork_entered.set()
        try:
            if self.fork_gate is not None:
                await self.fork_gate.wait()
        except asyncio.CancelledError:
            self.fork_cancelled = True
            raise
        if self.fork_error is not None:
            raise self.fork_error
        return self.child


class NamingCleanup:
    def __init__(self, events: list[tuple[str, str]]) -> None:
        self.events = events
        self.gate: asyncio.Event | None = None
        self.entered = asyncio.Event()
        self.error: Exception | None = None

    async def clean_thread(self, thread_id: str) -> None:
        self.events.append(("clean", thread_id))
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error


class NamingSubscriptions:
    def __init__(self, events: list[tuple[str, str]]) -> None:
        self.events = events
        self.entered = asyncio.Event()
        self.error: Exception | None = None

    async def unsubscribe(self, thread_id: str) -> str:
        self.events.append(("unsubscribe", thread_id))
        self.entered.set()
        if self.error is not None:
            raise self.error
        return "unsubscribed"


class ThreadNamerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.parent = NamingThread("parent", self.events, ephemeral=False)
        self.codex = NamingCodex(self.events)
        self.cleanup = NamingCleanup(self.events)
        self.subscriptions = NamingSubscriptions(self.events)
        self.commits: list[tuple[str, str, str]] = []

        async def commit(job: NamingJob, title: str) -> None:
            self.assertTrue(self.namer.is_current(job))
            self.commits.append((job.binding_id, job.parent.id, title))

        self.namer = ThreadNamer(
            codex=self.codex, terminal_cleanup=self.cleanup,
            subscription_control=self.subscriptions, commit=commit,
            timeout_seconds=0.1, cleanup_timeout_seconds=0.01,
        )

    async def asyncTearDown(self) -> None:
        self.namer.close()
        for gate in (
            self.parent.read_gate, self.codex.fork_gate,
            self.codex.child.read_gate, self.codex.child.turn_gate,
            self.codex.child.handle.interrupt_gate, self.cleanup.gate,
        ):
            if gate is not None:
                gate.set()
        self.codex.child.handle.finished.set()
        await self.drain()

    async def drain(self) -> None:
        if self.namer._tasks:
            await asyncio.wait_for(
                asyncio.gather(*tuple(self.namer._tasks), return_exceptions=True),
                timeout=1,
            )

    def start(self) -> None:
        self.namer.start("binding", self.parent, "parent-turn")

    async def running(self) -> None:
        await asyncio.wait_for(self.codex.child.handle.running.wait(), timeout=1)

    def assert_child_cleanup(self) -> None:
        mutations = [event for event in self.events if event[0] in {"clean", "unsubscribe"}]
        self.assertEqual(mutations, [("clean", "private-fork"), ("unsubscribe", "private-fork")])

    async def test_success_uses_fork_context_and_releases_only_private_thread(self) -> None:
        self.start()
        await self.running()
        self.codex.child.handle.finished.set()
        await self.drain()
        self.assertEqual(self.commits, [("binding", "parent", "会话自动命名")])
        self.assertEqual(self.codex.fork_calls, [("parent", {"ephemeral": True, "include_turns": False})])
        self.assertEqual(self.codex.child.turn_calls, [(NAMING_PROMPT, {})])
        self.assertEqual(self.codex.child.handle.run_calls, 1)
        self.assertNotIn(("interrupt", "private-fork"), self.events)
        self.assert_child_cleanup()

    async def test_named_parent_skips_generation(self) -> None:
        self.parent.native.name = "用户设置的名称"
        self.start()
        await self.drain()
        self.assertEqual(self.codex.fork_calls, [])
        self.assertEqual(self.commits, [])
        self.assertEqual(self.parent.summary_read_count, 1)
        self.assertEqual(self.parent.full_read_count, 0)

    async def test_missing_name_shape_skips_generation(self) -> None:
        del self.parent.native.name
        self.start()
        await self.drain()
        self.assertEqual(self.codex.fork_calls, [])

    async def test_start_is_synchronous_and_duplicate_attempts_share_slot(self) -> None:
        self.parent.read_gate = asyncio.Event()
        self.start()
        self.assertEqual(self.events, [])
        await self.parent.read_entered.wait()
        self.start()
        self.parent.read_gate.set()
        await self.running()
        self.start()
        self.codex.child.handle.finished.set()
        await self.drain()
        self.assertEqual(len(self.codex.fork_calls), 1)

    async def test_fork_waits_for_exact_started_turn_input_without_waiting_for_completion(self) -> None:
        self.parent.native.turns[0].id = "previous-turn"
        self.start()
        await self.parent.full_read_entered.wait()
        self.assertEqual(self.codex.fork_calls, [])
        self.parent.native.turns.append(SimpleNamespace(
            id="parent-turn", status="inProgress", items=[],
        ))
        await asyncio.sleep(0)
        self.assertEqual(self.codex.fork_calls, [])
        self.parent.native.turns[-1].items.append(SimpleNamespace(
            root=SimpleNamespace(type="userMessage"),
        ))
        await self.running()
        self.assertEqual(self.parent.native.turns[-1].status, "inProgress")
        self.codex.child.handle.finished.set()
        await self.drain()
        self.assertGreaterEqual(self.parent.full_read_count, 2)
        self.assertEqual(len(self.commits), 1)

    async def test_missing_exact_input_times_out_without_fork(self) -> None:
        self.parent.native.turns[0].id = "previous-turn"
        self.start()
        await self.drain()
        self.assertEqual(self.codex.fork_calls, [])
        self.assertEqual(self.commits, [])

    async def test_known_unmaterialized_context_can_become_visible(self) -> None:
        self.parent.full_read_errors.append(InvalidRequestError(
            -32600,
            "thread parent is not materialized yet; includeTurns is unavailable before first user message",
        ))
        self.start()
        await self.running()
        self.codex.child.handle.finished.set()
        await self.drain()
        self.assertEqual(self.parent.full_read_count, 2)
        self.assertEqual(len(self.commits), 1)

    async def test_initial_summary_empty_rollout_can_become_readable(self) -> None:
        self.parent.summary_read_errors.append(InternalRpcError(
            -32603,
            "failed to read session metadata: rollout at /tmp/first-turn.jsonl is empty",
        ))
        self.start()
        await self.running()
        self.codex.child.handle.finished.set()
        await self.drain()
        self.assertEqual(self.parent.summary_read_count, 2)
        self.assertEqual(self.parent.full_read_count, 1)
        self.assertEqual(len(self.commits), 1)

    async def test_initial_summary_nonretryable_error_does_not_fork(self) -> None:
        self.parent.summary_read_errors.append(InternalRpcError(
            -32603,
            "failed to read session metadata: rollout at /tmp/first-turn.jsonl is unreadable",
        ))
        self.start()
        await self.drain()
        self.assertEqual(self.parent.summary_read_count, 1)
        self.assertEqual(self.parent.full_read_count, 0)
        self.assertEqual(self.codex.fork_calls, [])

    async def test_unexpected_context_read_error_does_not_retry_or_fork(self) -> None:
        self.parent.full_read_errors.append(InternalRpcError(-32603, "transport unavailable"))
        self.start()
        await self.drain()
        self.assertEqual(self.parent.full_read_count, 1)
        self.assertEqual(self.codex.fork_calls, [])

    async def test_parent_renamed_while_waiting_for_input_skips_fork(self) -> None:
        self.parent.native.turns = []
        self.start()
        await self.parent.full_read_entered.wait()
        self.parent.native.name = "外部命名"
        await self.drain()
        self.assertEqual(self.codex.fork_calls, [])

    async def test_invalidation_interrupts_without_committing(self) -> None:
        self.start()
        await self.running()
        self.namer.invalidate("binding")
        await self.drain()
        self.assertEqual(self.commits, [])
        self.assertIn(("interrupt", "private-fork"), self.events)
        self.assertFalse(self.codex.child.handle.run_cancelled)
        self.assertEqual(self.codex.child.handle.run_calls, 1)
        self.assert_child_cleanup()

    async def test_invalidation_before_background_work_does_not_fork(self) -> None:
        self.start()
        self.namer.invalidate("binding")
        await self.drain()
        self.assertEqual(self.events, [])
        self.assertEqual(self.codex.fork_calls, [])

    async def test_late_fork_after_timeout_is_cleaned_without_starting_turn(self) -> None:
        self.codex.fork_gate = asyncio.Event()
        self.start()
        await self.codex.fork_entered.wait()
        await self.namer._jobs["binding"].invalidated.wait()
        self.start()
        self.assertEqual(len(self.codex.fork_calls), 1)
        self.assertFalse(self.codex.fork_cancelled)
        self.codex.fork_gate.set()
        await self.drain()
        self.assertEqual(self.codex.child.turn_calls, [])
        self.assertEqual(self.commits, [])
        self.assert_child_cleanup()

    async def test_late_turn_after_invalidation_is_consumed_interrupted_and_cleaned(self) -> None:
        self.codex.child.turn_gate = asyncio.Event()
        self.start()
        await self.codex.child.turn_entered.wait()
        self.namer.invalidate("binding")
        self.start()
        self.codex.child.turn_gate.set()
        await self.drain()
        self.assertEqual(self.commits, [])
        self.assertEqual(self.codex.child.handle.run_calls, 1)
        self.assertIn(("interrupt", "private-fork"), self.events)
        self.assert_child_cleanup()

    async def test_cancelled_owner_still_cleans_late_fork(self) -> None:
        self.codex.fork_gate = asyncio.Event()
        self.start()
        await self.codex.fork_entered.wait()
        task = self.namer._jobs["binding"].task
        assert task is not None
        task.cancel()
        await asyncio.sleep(0)
        self.codex.fork_gate.set()
        await self.drain()
        self.assertFalse(self.codex.fork_cancelled)
        self.assertEqual(self.codex.child.turn_calls, [])
        self.assert_child_cleanup()

    async def test_failed_generation_retries_on_later_start(self) -> None:
        self.codex.child.handle.result.status = "failed"
        self.codex.child.handle.finished.set()
        self.start()
        await self.drain()
        self.assertEqual(self.commits, [])
        self.codex.child.handle.result.status = "completed"
        self.start()
        await self.drain()
        self.assertEqual(len(self.codex.fork_calls), 2)
        self.assertEqual(len(self.commits), 1)

    async def test_unknown_fork_response_prevents_more_forks_without_parent_cleanup(self) -> None:
        self.codex.fork_error = RuntimeError("fork response lost")
        self.start()
        await self.drain()
        self.codex.fork_error = None
        self.start()
        await self.drain()
        self.assertEqual(len(self.codex.fork_calls), 1)
        self.assertTrue(self.namer._jobs["binding"].acquisition_unknown)
        self.assertTrue(self.namer._jobs["binding"].invalidated.is_set())
        self.assertEqual(self.commits, [])
        self.assertEqual([event for event in self.events if event[0] != "read"], [])

    async def test_unknown_turn_response_cleans_known_child_and_prevents_more_forks(self) -> None:
        self.codex.child.turn_error = RuntimeError("Turn response lost")
        self.start()
        await self.drain()
        self.codex.child.turn_error = None
        self.start()
        await self.drain()
        self.assertEqual(len(self.codex.fork_calls), 1)
        self.assertTrue(self.namer._jobs["binding"].acquisition_unknown)
        self.assertEqual(self.commits, [])
        self.assertNotIn(("interrupt", "private-fork"), self.events)
        self.assert_child_cleanup()

    async def test_unknown_run_failure_does_not_allow_another_fork(self) -> None:
        self.codex.child.handle.error = RuntimeError("consumer lost terminal notification")
        self.start()
        await self.running()
        self.codex.child.handle.finished.set()
        await self.drain()
        self.start()
        await self.drain()
        self.assertEqual(len(self.codex.fork_calls), 1)
        self.assertFalse(self.namer._jobs["binding"].terminal_observed)
        self.assertEqual(self.commits, [])
        self.assert_child_cleanup()

    async def fail_with_terminal_read(
        self, *, thread_id: str = "private-fork", turn_id: str = "name-turn",
        status: str = "failed", error: Exception | None = None,
    ) -> None:
        self.codex.child.handle.error = RuntimeError("SDK raises for failed Turn")
        self.start()
        await self.running()
        self.codex.child.native.id = thread_id
        self.codex.child.native.turns = [SimpleNamespace(id=turn_id, status=status)]
        if error is not None:
            self.codex.child.full_read_errors.append(error)
        self.codex.child.handle.finished.set()
        await self.drain()
        self.assertEqual(self.codex.child.full_read_count, 1)
        self.assertEqual(self.commits, [])
        self.assert_child_cleanup()

    async def test_exact_failed_read_makes_sdk_run_error_retryable(self) -> None:
        await self.fail_with_terminal_read()
        self.assertNotIn("binding", self.namer._jobs)
        last_read = max(index for index, event in enumerate(self.events) if event == ("read", "private-fork"))
        self.assertLess(last_read, self.events.index(("unsubscribe", "private-fork")))
        self.codex.child.handle.error = None
        self.codex.child.handle.result.status = "completed"
        self.start()
        await self.drain()
        self.assertEqual(len(self.codex.fork_calls), 2)
        self.assertEqual(len(self.commits), 1)

    async def test_terminal_read_wrong_thread_keeps_slot(self) -> None:
        await self.fail_with_terminal_read(thread_id="parent")
        self.assertFalse(self.namer._jobs["binding"].terminal_observed)
        self.start()
        await self.drain()
        self.assertEqual(len(self.codex.fork_calls), 1)

    async def test_terminal_read_wrong_turn_keeps_slot(self) -> None:
        await self.fail_with_terminal_read(turn_id="parent-turn")
        self.assertFalse(self.namer._jobs["binding"].terminal_observed)
        self.start()
        await self.drain()
        self.assertEqual(len(self.codex.fork_calls), 1)

    async def test_terminal_read_nonterminal_keeps_slot(self) -> None:
        await self.fail_with_terminal_read(status="inProgress")
        self.assertFalse(self.namer._jobs["binding"].terminal_observed)
        self.start()
        await self.drain()
        self.assertEqual(len(self.codex.fork_calls), 1)

    async def test_terminal_read_unavailable_keeps_slot(self) -> None:
        await self.fail_with_terminal_read(error=InternalRpcError(-32603, "ephemeral history unavailable"))
        self.assertFalse(self.namer._jobs["binding"].terminal_observed)
        self.start()
        await self.drain()
        self.assertEqual(len(self.codex.fork_calls), 1)

    async def test_exact_terminal_read_can_close_stalled_consumer(self) -> None:
        self.codex.child.handle.interrupt_completes = False
        self.start()
        await self.running()
        self.codex.child.native.turns = [SimpleNamespace(id="name-turn", status="completed")]
        self.namer.invalidate("binding")
        await self.drain()
        self.assertTrue(self.codex.child.handle.run_cancelled)
        self.assertNotIn("binding", self.namer._jobs)
        self.assertEqual(self.codex.child.handle.run_calls, 1)
        self.assertEqual(self.codex.child.full_read_count, 1)
        self.assertEqual(self.commits, [])
        self.assert_child_cleanup()

    async def test_output_must_be_completed_single_line_and_bounded(self) -> None:
        handle = self.codex.child.handle
        for title, status, result_id in (
            ("", "completed", handle.id),
            ("a" * 121, "completed", handle.id),
            ("name\nexplanation", "completed", handle.id),
            ("title", "interrupted", handle.id),
            ("title", "completed", "wrong-turn"),
        ):
            with self.subTest(title=title, status=status, result_id=result_id):
                handle.result.final_response = title
                handle.result.status = status
                handle.result.id = result_id
                handle.finished.set()
                self.start()
                await self.drain()
        self.assertEqual(self.commits, [])

    async def test_wrong_fork_cannot_clean_parent(self) -> None:
        self.codex.child.id = self.parent.id
        self.start()
        await self.drain()
        self.start()
        await self.drain()
        self.assertEqual(len(self.codex.fork_calls), 1)
        self.assertEqual(self.codex.child.turn_calls, [])
        self.assertEqual([event for event in self.events if event[0] != "read"], [])

    async def test_persisted_fork_shape_never_starts_title_turn(self) -> None:
        self.codex.child.native.path = "/tmp/unexpected-persisted.jsonl"
        self.start()
        await self.drain()
        self.assertEqual(self.codex.child.turn_calls, [])
        self.assert_child_cleanup()

    async def test_cleanup_failure_does_not_skip_unsubscribe(self) -> None:
        self.cleanup.error = RuntimeError("cleanup unavailable")
        self.start()
        await self.running()
        self.codex.child.handle.finished.set()
        await self.drain()
        self.assertEqual(len(self.commits), 1)
        self.assert_child_cleanup()

    async def test_interrupt_error_does_not_skip_remaining_cleanup(self) -> None:
        self.codex.child.handle.interrupt_error = RuntimeError("interrupt unknown")
        self.start()
        await self.running()
        self.namer.invalidate("binding")
        await asyncio.wait_for(self.subscriptions.entered.wait(), timeout=1)
        self.assert_child_cleanup()
        self.assertFalse(self.codex.child.handle.run_cancelled)
        self.codex.child.handle.finished.set()
        await self.drain()

    async def test_failed_turn_and_unsubscribe_error_remain_private(self) -> None:
        self.codex.child.handle.error = RuntimeError("native Turn failed")
        self.subscriptions.error = RuntimeError("unsubscribe unavailable")
        self.start()
        await self.running()
        self.codex.child.handle.finished.set()
        await self.drain()
        self.assertEqual(self.commits, [])
        self.assert_child_cleanup()

    async def test_cancelled_owner_still_consumes_and_cleans_late_turn(self) -> None:
        self.codex.child.turn_gate = asyncio.Event()
        self.start()
        await self.codex.child.turn_entered.wait()
        task = self.namer._jobs["binding"].task
        assert task is not None
        task.cancel()
        await asyncio.sleep(0)
        self.codex.child.turn_gate.set()
        await self.drain()
        self.assertEqual(self.commits, [])
        self.assertEqual(self.codex.child.handle.run_calls, 1)
        self.assertIn(("interrupt", "private-fork"), self.events)
        self.assert_child_cleanup()

    async def test_slow_cleanup_is_bounded_and_retains_slot_until_rpc_finishes(self) -> None:
        self.cleanup.gate = asyncio.Event()
        self.start()
        await self.running()
        self.codex.child.handle.finished.set()
        await asyncio.wait_for(self.subscriptions.entered.wait(), timeout=1)
        self.start()
        self.assertEqual(len(self.codex.fork_calls), 1)
        self.cleanup.gate.set()
        await self.drain()
        self.assert_child_cleanup()

    async def test_hanging_commit_does_not_delay_child_cleanup_or_get_cancelled(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        cancelled = False

        async def commit(job: NamingJob, title: str) -> None:
            nonlocal cancelled
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled = True
                raise

        self.namer._commit = commit
        self.start()
        await self.running()
        self.codex.child.handle.finished.set()
        await entered.wait()
        self.namer.invalidate("binding")
        await asyncio.wait_for(self.subscriptions.entered.wait(), timeout=1)
        self.start()
        self.assertEqual(len(self.codex.fork_calls), 1)
        release.set()
        await self.drain()
        self.assertFalse(cancelled)
        self.assert_child_cleanup()

    async def test_shutdown_is_bounded_and_late_fork_still_gets_cleaned(self) -> None:
        self.codex.fork_gate = asyncio.Event()
        self.start()
        await self.codex.fork_entered.wait()
        await asyncio.wait_for(self.namer.shutdown(), timeout=0.5)
        self.assertFalse(self.codex.fork_cancelled)
        self.assertTrue(self.namer._jobs)
        self.start()
        self.assertEqual(len(self.codex.fork_calls), 1)
        self.codex.fork_gate.set()
        await self.drain()
        self.assert_child_cleanup()

    async def test_unknown_terminal_preserves_consumer_and_prevents_more_forks(self) -> None:
        handle = self.codex.child.handle
        handle.interrupt_completes = False
        self.start()
        await self.running()
        self.namer.invalidate("binding")
        await asyncio.wait_for(self.subscriptions.entered.wait(), timeout=1)
        self.start()
        self.assertEqual(len(self.codex.fork_calls), 1)
        self.assertFalse(handle.run_cancelled)
        handle.finished.set()
        await self.drain()
        self.assertEqual(self.commits, [])
        self.assertEqual(handle.run_calls, 1)
        self.assertNotIn("binding", self.namer._jobs)
        self.assert_child_cleanup()

    async def test_explicit_parent_turn_settings_are_captured_for_naming(self) -> None:
        settings = TurnModelSettings(
            model_id="model-id", model="model-wire", effort_id="low",
            effort="low", service_tier_id="default", service_tier_name="Standard",
        )
        self.namer.start("binding", self.parent, "parent-turn", settings)
        await self.running()
        self.codex.child.handle.finished.set()
        await self.drain()
        self.assertEqual(self.codex.child.turn_calls[0][1], {
            "model": "model-wire", "effort": "low", "service_tier": "default",
        })
