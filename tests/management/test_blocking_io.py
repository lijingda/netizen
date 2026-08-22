from __future__ import annotations

import asyncio
import threading
import unittest

from netizen.management.blocking_io import (
    BlockingIODrainTimeout,
    BlockingIOExecutorClosed,
    BlockingIOExecutorSaturated,
    BlockingIOResultUnknown,
    BoundedBlockingIOExecutor,
)


class BoundedBlockingIOExecutorTest(unittest.IsolatedAsyncioTestCase):
    async def test_saturation_rejects_before_callable_reaches_executor(self) -> None:
        executor = BoundedBlockingIOExecutor(max_workers=1, capacity=1)
        entered = threading.Event()
        release = threading.Event()
        rejected_ran = threading.Event()

        def first() -> str:
            entered.set()
            release.wait(timeout=2)
            return "first"

        first_task = asyncio.create_task(executor.submit(first))
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))

        with self.assertRaises(BlockingIOExecutorSaturated):
            await executor.submit(lambda: rejected_ran.set())

        self.assertFalse(rejected_ran.is_set())
        self.assertEqual(executor.in_flight, 1)
        release.set()
        self.assertEqual(await first_task, "first")
        await executor.aclose(deadline=asyncio.get_running_loop().time() + 1)

    async def test_one_worker_serializes_a_bounded_queue(self) -> None:
        executor = BoundedBlockingIOExecutor(max_workers=1, capacity=2)
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()
        order: list[str] = []

        def first() -> str:
            order.append("first-start")
            first_entered.set()
            release_first.wait(timeout=2)
            order.append("first-end")
            return "first"

        def second() -> str:
            order.append("second-start")
            second_entered.set()
            return "second"

        first_task = asyncio.create_task(executor.submit(first))
        self.assertTrue(await asyncio.to_thread(first_entered.wait, 1))
        second_task = asyncio.create_task(executor.submit(second))
        await asyncio.sleep(0)
        self.assertEqual(executor.in_flight, 2)
        self.assertFalse(second_entered.is_set())

        with self.assertRaises(BlockingIOExecutorSaturated):
            await executor.submit(lambda: "third")

        release_first.set()
        self.assertEqual(
            await asyncio.gather(first_task, second_task),
            ["first", "second"],
        )
        self.assertEqual(order, ["first-start", "first-end", "second-start"])
        await executor.aclose(deadline=asyncio.get_running_loop().time() + 1)

    async def test_blocking_callable_does_not_stop_event_loop_heartbeat(self) -> None:
        executor = BoundedBlockingIOExecutor(max_workers=1, capacity=1)
        entered = threading.Event()
        release = threading.Event()
        heartbeat_count = 0

        def blocking() -> None:
            entered.set()
            release.wait(timeout=2)

        async def heartbeat() -> None:
            nonlocal heartbeat_count
            for _ in range(8):
                await asyncio.sleep(0.005)
                heartbeat_count += 1

        operation = asyncio.create_task(executor.submit(blocking))
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        await heartbeat()
        self.assertEqual(heartbeat_count, 8)
        self.assertFalse(operation.done())
        release.set()
        await operation
        await executor.aclose(deadline=asyncio.get_running_loop().time() + 1)

    async def test_cancelled_waiter_does_not_cancel_work_or_release_capacity(self) -> None:
        executor = BoundedBlockingIOExecutor(max_workers=1, capacity=1)
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()

        def blocking() -> None:
            entered.set()
            release.wait(timeout=2)
            completed.set()

        waiter = asyncio.create_task(executor.submit(blocking))
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter

        self.assertEqual(executor.in_flight, 1)
        with self.assertRaises(BlockingIOExecutorSaturated):
            await executor.submit(lambda: None)

        release.set()
        await executor.drain(deadline=asyncio.get_running_loop().time() + 1)
        self.assertTrue(completed.is_set())
        self.assertEqual(executor.in_flight, 0)
        self.assertEqual(await executor.submit(lambda: 42), 42)
        await executor.aclose(deadline=asyncio.get_running_loop().time() + 1)

    async def test_deadline_reports_unknown_while_single_operation_continues(self) -> None:
        executor = BoundedBlockingIOExecutor(max_workers=1, capacity=1)
        entered = threading.Event()
        release = threading.Event()
        calls = 0
        effects: list[str] = []

        def operation() -> None:
            nonlocal calls
            calls += 1
            entered.set()
            release.wait(timeout=2)
            effects.append("created")

        loop = asyncio.get_running_loop()
        with self.assertRaises(BlockingIOResultUnknown) as caught:
            await executor.submit(operation, deadline=loop.time() + 0.02)

        self.assertTrue(entered.is_set())
        self.assertGreaterEqual(caught.exception.deadline, loop.time() - 0.1)
        self.assertEqual(calls, 1)
        self.assertEqual(executor.in_flight, 1)
        self.assertEqual(effects, [])

        release.set()
        await executor.drain(deadline=loop.time() + 1)
        self.assertEqual(calls, 1)
        self.assertEqual(effects, ["created"])
        await executor.aclose(deadline=loop.time() + 1)

    async def test_tracked_failure_drains_after_waiter_cancellation(self) -> None:
        executor = BoundedBlockingIOExecutor(max_workers=1, capacity=1)
        entered = threading.Event()
        release = threading.Event()

        def failing() -> None:
            entered.set()
            release.wait(timeout=2)
            raise RuntimeError("late failure")

        waiter = asyncio.create_task(executor.submit(failing))
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        release.set()
        await executor.drain(deadline=asyncio.get_running_loop().time() + 1)
        self.assertEqual(executor.in_flight, 0)
        await executor.aclose(deadline=asyncio.get_running_loop().time() + 1)

    async def test_close_rejects_new_work_and_can_resume_after_drain_timeout(self) -> None:
        executor = BoundedBlockingIOExecutor(max_workers=1, capacity=1)
        entered = threading.Event()
        release = threading.Event()

        def blocking() -> None:
            entered.set()
            release.wait(timeout=2)

        operation = asyncio.create_task(executor.submit(blocking))
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))

        executor.close_admission()
        executor.close_admission()
        self.assertFalse(executor.accepting)
        with self.assertRaises(BlockingIOExecutorClosed):
            await executor.submit(lambda: None)

        loop = asyncio.get_running_loop()
        with self.assertRaises(BlockingIODrainTimeout) as caught:
            await executor.aclose(deadline=loop.time() + 0.01)
        self.assertEqual(caught.exception.in_flight, 1)
        self.assertFalse(operation.done())

        release.set()
        await operation
        await executor.aclose(deadline=loop.time() + 1)
        self.assertTrue(executor.joined)
        await executor.aclose(deadline=loop.time() + 1)

    async def test_submit_validates_configuration_and_deadline(self) -> None:
        with self.assertRaises(ValueError):
            BoundedBlockingIOExecutor(max_workers=0)
        with self.assertRaises(ValueError):
            BoundedBlockingIOExecutor(max_workers=2, capacity=1)
        with self.assertRaises(ValueError):
            BoundedBlockingIOExecutor(max_workers=1.5)  # type: ignore[arg-type]

        executor = BoundedBlockingIOExecutor()
        with self.assertRaises(ValueError):
            await executor.submit(lambda: None, deadline=float("nan"))
        self.assertEqual(executor.in_flight, 0)
        await executor.aclose(deadline=asyncio.get_running_loop().time() + 1)


if __name__ == "__main__":
    unittest.main()
