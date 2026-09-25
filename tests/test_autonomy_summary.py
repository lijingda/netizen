from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from openai_codex import ApprovalMode, Sandbox

from netizen.autonomy.models import AutonomyError
from netizen.autonomy.summary import CodexSummarizer


class SummaryHandle:
    def __init__(self, thread_id: str, events: list[tuple[str, str]]) -> None:
        self.id = thread_id + "-turn"
        self.thread_id = thread_id
        self.events = events
        self.running = asyncio.Event()
        self.finished = asyncio.Event()
        self.result = SimpleNamespace(id=self.id, status="completed", final_response="摘要")
        self.error: Exception | None = None
        self.interrupt_error: Exception | None = None
        self.interrupt_finishes = True
        self.run_calls = 0
        self.cancelled = False

    async def run(self) -> object:
        self.run_calls += 1
        self.running.set()
        try:
            await self.finished.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.error:
            raise self.error
        return self.result

    async def interrupt(self) -> None:
        self.events.append(("interrupt", self.thread_id))
        if self.interrupt_error:
            raise self.interrupt_error
        if self.interrupt_finishes:
            self.result.status = "interrupted"
            self.finished.set()


class SummaryThread:
    def __init__(self, thread_id: str, events: list[tuple[str, str]]) -> None:
        self.id = thread_id
        self.events = events
        self.handle = SummaryHandle(thread_id, events)
        self.native = SimpleNamespace(
            id=thread_id, ephemeral=True, path=None, forked_from_id=None, turns=[],
        )
        self.turn_entered = asyncio.Event()
        self.turn_gate: asyncio.Event | None = None
        self.turn_error: Exception | None = None
        self.read_gate: asyncio.Event | None = None
        self.read_entered = asyncio.Event()
        self.read_error: Exception | None = None
        self.prompts: list[str] = []
        self.turn_cancelled = False
        self.full_reads = 0

    async def turn(self, prompt: str) -> SummaryHandle:
        self.prompts.append(prompt)
        self.turn_entered.set()
        try:
            if self.turn_gate:
                await self.turn_gate.wait()
        except asyncio.CancelledError:
            self.turn_cancelled = True
            raise
        if self.turn_error:
            raise self.turn_error
        return self.handle

    async def read(self, *, include_turns: bool = False) -> object:
        self.events.append(("read", self.id))
        self.read_entered.set()
        self.full_reads += int(include_turns)
        if self.read_gate:
            await self.read_gate.wait()
        if self.read_error:
            raise self.read_error
        return SimpleNamespace(thread=self.native)


class SummaryCodex:
    def __init__(self, events: list[tuple[str, str]]) -> None:
        self.events = events
        self.threads = [SummaryThread("private-summary-1", events), SummaryThread("private-summary-2", events)]
        self.started = asyncio.Event()
        self.gate: asyncio.Event | None = None
        self.error: Exception | None = None
        self.calls: list[dict[str, object]] = []
        self.cancelled = False

    async def thread_start(self, **kwargs: object) -> SummaryThread:
        thread = self.threads[len(self.calls)]
        self.calls.append(kwargs)
        self.started.set()
        try:
            if self.gate:
                await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.error:
            raise self.error
        return thread


class SummaryCleanup:
    def __init__(self, events: list[tuple[str, str]]) -> None:
        self.events = events
        self.error: Exception | None = None
        self.gate: asyncio.Event | None = None

    async def clean_thread(self, thread_id: str) -> None:
        self.events.append(("clean", thread_id))
        if self.gate:
            await self.gate.wait()
        if self.error:
            raise self.error


class SummarySubscriptions:
    def __init__(self, events: list[tuple[str, str]]) -> None:
        self.events = events
        self.error: Exception | None = None
        self.unsubscribed = asyncio.Event()

    async def unsubscribe(self, thread_id: str) -> str:
        self.events.append(("unsubscribe", thread_id))
        self.unsubscribed.set()
        if self.error:
            raise self.error
        return "unsubscribed"


class CodexSummarizerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.codex = SummaryCodex(self.events)
        self.cleanup = SummaryCleanup(self.events)
        self.subscriptions = SummarySubscriptions(self.events)
        self.summarizer = CodexSummarizer(
            codex=self.codex, terminal_cleanup=self.cleanup,
            subscription_control=self.subscriptions,
            timeout_seconds=0.1, cleanup_timeout_seconds=0.01,
        )
        self.thread = self.codex.threads[0]

    async def asyncTearDown(self) -> None:
        self.summarizer.close()
        for gate in (self.codex.gate, self.cleanup.gate):
            if gate:
                gate.set()
        for thread in self.codex.threads:
            for gate in (thread.turn_gate, thread.read_gate):
                if gate:
                    gate.set()
            thread.handle.finished.set()
        await self.drain()

    async def drain(self) -> None:
        if self.summarizer._tasks:
            await asyncio.wait_for(
                asyncio.gather(*tuple(self.summarizer._tasks), return_exceptions=True), 1,
            )

    def request(self, *, key: str = "binding-1", budget: int = 80) -> asyncio.Task[str]:
        return asyncio.create_task(self.summarizer(
            "/project", "old summary plus selected records", budget, key=key,
        ))

    async def running(self) -> None:
        await asyncio.wait_for(self.thread.handle.running.wait(), 1)

    def assert_cleanup(self) -> None:
        self.assertEqual(
            [event for event in self.events if event[0] in {"clean", "unsubscribe"}],
            [("clean", self.thread.id), ("unsubscribe", self.thread.id)],
        )

    async def test_success_uses_fresh_ephemeral_thread_and_only_supplied_records(self) -> None:
        task = self.request()
        await self.running()
        self.thread.handle.finished.set()
        self.assertEqual(await task, "摘要")
        await self.drain()
        self.assertEqual(self.codex.calls, [{
            "cwd": "/project", "ephemeral": True,
            "sandbox": Sandbox.read_only, "approval_mode": ApprovalMode.deny_all,
        }])
        self.assertIn("old summary plus selected records", self.thread.prompts[0])
        self.assertIn("不得调用工具", self.thread.prompts[0])
        self.assertIn("80 字节", self.thread.prompts[0])
        self.assertEqual(self.thread.handle.run_calls, 1)
        self.assertNotIn(("interrupt", self.thread.id), self.events)
        self.assert_cleanup()
        self.assertEqual(self.summarizer._jobs, {})

    async def test_invalid_output_is_not_truncated_or_returned(self) -> None:
        task = self.request(budget=2)
        await self.running()
        self.thread.handle.finished.set()
        with self.assertRaisesRegex(AutonomyError, "summary failed"):
            await task
        await self.drain()
        self.assert_cleanup()
        self.assertEqual(self.summarizer._jobs, {})

    async def test_failed_native_run_uses_exact_terminal_read_only_for_cleanup(self) -> None:
        self.thread.handle.error = RuntimeError("private response text")
        self.thread.native.turns = [SimpleNamespace(id=self.thread.handle.id, status="failed")]
        task = self.request()
        await self.running()
        self.thread.handle.finished.set()
        with self.assertRaisesRegex(AutonomyError, "^decision summary failed$"):
            await task
        await self.drain()
        self.assertEqual(self.thread.full_reads, 1)
        self.assert_cleanup()
        self.assertEqual(self.summarizer._jobs, {})

    async def test_timeout_interrupts_and_releases_exact_temporary_turn(self) -> None:
        task = self.request()
        await self.running()
        with self.assertRaisesRegex(AutonomyError, "timed out"):
            await task
        await self.drain()
        self.assertIn(("interrupt", self.thread.id), self.events)
        self.assertFalse(self.thread.handle.cancelled)
        self.assert_cleanup()

    async def test_caller_cancellation_does_not_cancel_late_thread_acquisition(self) -> None:
        self.codex.gate = asyncio.Event()
        task = self.request()
        await self.codex.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with self.assertRaisesRegex(AutonomyError, "not settled"):
            await self.request()
        self.assertFalse(self.codex.cancelled)
        self.codex.gate.set()
        await self.drain()
        self.assertEqual(self.thread.prompts, [])
        self.assert_cleanup()

    async def test_timeout_retains_late_turn_identity_and_cleans_it(self) -> None:
        self.thread.turn_gate = asyncio.Event()
        task = self.request()
        await self.thread.turn_entered.wait()
        with self.assertRaisesRegex(AutonomyError, "timed out"):
            await task
        self.assertFalse(self.thread.turn_cancelled)
        self.thread.turn_gate.set()
        await self.drain()
        self.assertEqual(self.thread.handle.run_calls, 1)
        self.assertIn(("interrupt", self.thread.id), self.events)
        self.assert_cleanup()

    async def test_cancel_during_metadata_read_still_unsubscribes(self) -> None:
        self.thread.read_gate = asyncio.Event()
        task = self.request()
        await self.thread.read_entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(self.subscriptions.unsubscribed.wait(), 1)
        self.assertEqual(self.thread.prompts, [])
        self.assert_cleanup()
        self.assertIn("binding-1", self.summarizer._jobs)
        self.thread.read_gate.set()
        await self.drain()

    async def test_unknown_acquisition_blocks_only_same_binding_not_shared_cwd(self) -> None:
        self.codex.error = RuntimeError("unknown start response")
        with self.assertRaises(AutonomyError):
            await self.request()
        await self.drain()
        self.codex.error = None
        with self.assertRaisesRegex(AutonomyError, "not settled"):
            await self.request()
        task = self.request(key="binding-2")
        other = self.codex.threads[1]
        await asyncio.wait_for(other.handle.running.wait(), 1)
        other.handle.finished.set()
        self.assertEqual(await task, "摘要")
        await self.drain()
        self.assertEqual(len(self.codex.calls), 2)

    async def test_same_cwd_distinct_binding_summaries_run_concurrently(self) -> None:
        first = self.request()
        await self.running()
        second = self.request(key="binding-2")
        other = self.codex.threads[1]
        await asyncio.wait_for(other.handle.running.wait(), 1)
        self.assertFalse(first.done())
        self.thread.handle.finished.set()
        other.handle.finished.set()
        self.assertEqual(await asyncio.gather(first, second), ["摘要", "摘要"])
        await self.drain()
        self.assertEqual(self.summarizer._jobs, {})

    async def test_wrong_turn_identity_is_never_interrupted_or_consumed(self) -> None:
        self.thread.handle.thread_id = "user-thread"
        with self.assertRaises(AutonomyError):
            await self.request()
        await self.drain()
        self.assertEqual(self.thread.handle.run_calls, 0)
        self.assertNotIn(("interrupt", "user-thread"), self.events)
        self.assert_cleanup()
        self.assertIn("binding-1", self.summarizer._jobs)

    async def test_cleanup_failure_does_not_skip_unsubscribe_or_poison_valid_output(self) -> None:
        self.cleanup.error = RuntimeError("cleanup failure")
        task = self.request()
        await self.running()
        self.thread.handle.finished.set()
        self.assertEqual(await task, "摘要")
        await self.drain()
        self.assert_cleanup()
        self.assertIn("binding-1", self.summarizer._jobs)

    async def test_cleanup_timeout_still_unsubscribes_and_retains_worker(self) -> None:
        self.cleanup.gate = asyncio.Event()
        task = self.request()
        await self.running()
        self.thread.handle.finished.set()
        self.assertEqual(await task, "摘要")
        await asyncio.wait_for(self.subscriptions.unsubscribed.wait(), 1)
        self.assert_cleanup()
        self.assertIn("binding-1", self.summarizer._jobs)
        self.cleanup.gate.set()
        await self.drain()

    async def test_unknown_terminal_stays_reserved_without_restarting_observer(self) -> None:
        self.thread.handle.error = RuntimeError("lost stream")
        task = self.request()
        await self.running()
        self.thread.read_error = RuntimeError("unavailable history")
        self.thread.handle.finished.set()
        with self.assertRaises(AutonomyError):
            await task
        await self.drain()
        self.assertEqual(self.thread.handle.run_calls, 1)
        self.assertEqual(self.thread.full_reads, 1)
        self.assertIn("binding-1", self.summarizer._jobs)
        self.assert_cleanup()

    async def test_exact_terminal_read_can_release_stale_run_consumer(self) -> None:
        self.thread.handle.interrupt_finishes = False
        self.thread.native.turns = [SimpleNamespace(id=self.thread.handle.id, status="interrupted")]
        task = self.request()
        await self.running()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.drain()
        self.assertEqual(self.thread.handle.run_calls, 1)
        self.assertTrue(self.thread.handle.cancelled)
        self.assertEqual(self.summarizer._jobs, {})
        self.assert_cleanup()

    async def test_non_ephemeral_metadata_is_rejected_before_starting_turn(self) -> None:
        self.thread.native.ephemeral = False
        self.thread.native.path = "/unexpected/rollout"
        with self.assertRaises(AutonomyError):
            await self.request()
        await self.drain()
        self.assertEqual(self.thread.prompts, [])
        self.assertEqual(self.thread.handle.run_calls, 0)
        self.assert_cleanup()
        self.assertIn("binding-1", self.summarizer._jobs)

    async def test_shutdown_is_bounded_without_dropping_late_resource_owner(self) -> None:
        self.codex.gate = asyncio.Event()
        task = self.request()
        await self.codex.started.wait()
        await asyncio.wait_for(self.summarizer.aclose(), 0.3)
        with self.assertRaisesRegex(AutonomyError, "closed"):
            await task
        self.assertFalse(self.codex.cancelled)
        self.assertTrue(self.summarizer._tasks)
        self.codex.gate.set()
        await self.drain()
        self.assert_cleanup()

    async def test_invalid_input_never_creates_a_thread(self) -> None:
        with self.assertRaises(AutonomyError):
            await self.summarizer("/project", "records", 0, key="binding-1")
        self.assertEqual(self.codex.calls, [])
