from __future__ import annotations

import asyncio
import unittest

from netizen.runtime.name_writes import ThreadNameWrites


class ControlledWrite:
    def __init__(self, name: str, completed: list[str]) -> None:
        self.name = name
        self.completed = completed
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()
        self.error: Exception | None = None

    async def __call__(self) -> str:
        self.started.set()
        try:
            await self.release.wait()
            if self.error is not None:
                raise self.error
            self.completed.append(self.name)
            return self.name
        finally:
            self.finished.set()


class ThreadNameWritesTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.writes = ThreadNameWrites()
        self.completed: list[str] = []
        self.operations: list[ControlledWrite] = []
        self.requests: list[asyncio.Task[str | None]] = []

    async def asyncTearDown(self) -> None:
        for operation in self.operations:
            operation.release.set()
        for request in self.requests:
            if not request.done():
                request.cancel()
        await asyncio.gather(*self.requests, return_exceptions=True)
        for operation in self.operations:
            if operation.started.is_set():
                await asyncio.wait_for(operation.finished.wait(), 1)
        await asyncio.sleep(0)

    def operation(self, name: str) -> ControlledWrite:
        operation = ControlledWrite(name, self.completed)
        self.operations.append(operation)
        return operation

    def request(
        self,
        operation: ControlledWrite,
        *,
        binding_id: str = "binding",
        wait: bool = True,
        writes: ThreadNameWrites | None = None,
    ) -> asyncio.Task[str | None]:
        task = asyncio.create_task(
            (writes or self.writes).write(binding_id, operation, wait=wait),
        )
        self.requests.append(task)
        return task

    async def entered(self, operation: ControlledWrite) -> None:
        await asyncio.wait_for(operation.started.wait(), 1)

    async def result(self, task: asyncio.Task[str | None]) -> str | None:
        return await asyncio.wait_for(asyncio.shield(task), 1)

    async def test_automatic_write_acquires_an_uncontended_lock(self) -> None:
        automatic = self.operation("automatic")
        automatic.release.set()

        self.assertEqual(await self.result(self.request(automatic, wait=False)), "automatic")
        self.assertEqual(self.completed, ["automatic"])

    async def test_automatic_write_drops_when_any_writer_owns_lock(self) -> None:
        for owner_waits in (False, True):
            with self.subTest(owner_waits=owner_waits):
                owner = self.operation("owner")
                owner_request = self.request(owner, wait=owner_waits)
                await self.entered(owner)
                automatic = self.operation("automatic")

                self.assertIsNone(await self.result(self.request(automatic, wait=False)))
                self.assertFalse(automatic.started.is_set())
                self.assertFalse(owner_request.done())

                owner.release.set()
                self.assertEqual(await self.result(owner_request), "owner")
                self.assertFalse(automatic.started.is_set())

    async def test_manual_requests_write_in_arrival_order(self) -> None:
        first, second, third = (self.operation(name) for name in ("first", "second", "third"))
        first_request = self.request(first)
        await self.entered(first)
        second_request = self.request(second)
        await asyncio.sleep(0)
        third_request = self.request(third)
        await asyncio.sleep(0)
        self.assertFalse(second.started.is_set())
        self.assertFalse(third.started.is_set())

        first.release.set()
        await self.entered(second)
        self.assertFalse(third.started.is_set())
        second.release.set()
        await self.entered(third)
        third.release.set()

        self.assertEqual(
            await asyncio.gather(first_request, second_request, third_request),
            ["first", "second", "third"],
        )
        self.assertEqual(self.completed, ["first", "second", "third"])

    async def test_automatic_write_drops_during_handoff_to_a_manual_waiter(self) -> None:
        owner = self.operation("owner")
        owner_request = self.request(owner, wait=False)
        await self.entered(owner)
        manual = self.operation("manual")
        manual_request = self.request(manual)
        await asyncio.sleep(0)
        automatic = self.operation("late automatic")
        auto_created = asyncio.Event()
        automatic_request: asyncio.Task[str | None] | None = None

        def arrive_during_handoff() -> None:
            nonlocal automatic_request
            automatic_request = self.request(automatic, wait=False)
            auto_created.set()

        # The owner's completion schedules lock release, then this callback
        # queues the automatic attempt ahead of the awakened manual waiter.
        owner.release.set()
        asyncio.get_running_loop().call_soon(arrive_during_handoff)
        await asyncio.wait_for(auto_created.wait(), 1)
        assert automatic_request is not None

        self.assertIsNone(await self.result(automatic_request))
        self.assertFalse(automatic.started.is_set())
        await self.entered(manual)
        manual.release.set()
        await asyncio.gather(owner_request, manual_request)
        self.assertEqual(self.completed, ["owner", "manual"])

    async def test_different_bindings_can_write_concurrently(self) -> None:
        first = self.operation("first")
        first_request = self.request(first, binding_id="first")
        await self.entered(first)
        second = self.operation("second")
        second_request = self.request(second, binding_id="second", wait=False)
        await self.entered(second)
        self.assertFalse(first_request.done())

        second.release.set()
        self.assertEqual(await self.result(second_request), "second")
        first.release.set()
        self.assertEqual(await self.result(first_request), "first")

    async def test_cancelled_waiter_never_writes_or_blocks_its_successor(self) -> None:
        owner = self.operation("owner")
        owner_request = self.request(owner)
        await self.entered(owner)
        cancelled = self.operation("cancelled")
        cancelled_request = self.request(cancelled)
        await asyncio.sleep(0)
        successor = self.operation("successor")
        successor_request = self.request(successor)
        await asyncio.sleep(0)
        cancelled_request.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled_request

        owner.release.set()
        await self.entered(successor)
        successor.release.set()
        await asyncio.gather(owner_request, successor_request)
        self.assertFalse(cancelled.started.is_set())
        self.assertEqual(self.completed, ["owner", "successor"])

        automatic = self.operation("automatic")
        automatic.release.set()
        self.assertEqual(await self.result(self.request(automatic, wait=False)), "automatic")

    async def test_cancelling_started_request_keeps_actual_write_ordered(self) -> None:
        for owner_waits in (False, True):
            with self.subTest(owner_waits=owner_waits):
                owner = self.operation("owner")
                owner_request = self.request(owner, wait=owner_waits)
                await self.entered(owner)
                owner_request.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await owner_request
                self.assertFalse(owner.finished.is_set())

                successor = self.operation("manual successor")
                successor_request = self.request(successor)
                await asyncio.sleep(0)
                self.assertFalse(successor.started.is_set())
                automatic = self.operation("automatic")
                self.assertIsNone(await self.result(self.request(automatic, wait=False)))

                owner.release.set()
                await self.entered(successor)
                self.assertTrue(owner.finished.is_set())
                successor.release.set()
                self.assertEqual(await self.result(successor_request), "manual successor")
                self.assertEqual(self.completed[-2:], ["owner", "manual successor"])

    async def test_writer_exception_releases_lock_for_next_manual_write(self) -> None:
        failing = self.operation("failing")
        failing.error = RuntimeError("native write failed")
        failed_request = self.request(failing, wait=False)
        await self.entered(failing)
        successor = self.operation("manual successor")
        successor_request = self.request(successor)
        await asyncio.sleep(0)

        failing.release.set()
        with self.assertRaisesRegex(RuntimeError, "native write failed"):
            await self.result(failed_request)
        await self.entered(successor)
        successor.release.set()
        self.assertEqual(await self.result(successor_request), "manual successor")
        self.assertEqual(self.completed, ["manual successor"])

    async def test_cancelled_request_before_it_runs_does_not_acquire_lock(self) -> None:
        cancelled = self.operation("cancelled")
        cancelled_request = self.request(cancelled)
        cancelled_request.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled_request

        successor = self.operation("automatic")
        successor.release.set()
        self.assertEqual(await self.result(self.request(successor, wait=False)), "automatic")
        self.assertFalse(cancelled.started.is_set())

    async def test_new_registry_has_no_lock_left_by_another_instance(self) -> None:
        old = self.operation("old instance")
        old_request = self.request(old)
        await self.entered(old)
        fresh = self.operation("new instance")
        fresh.release.set()

        self.assertEqual(
            await self.result(self.request(fresh, writes=ThreadNameWrites(), wait=False)),
            "new instance",
        )
        self.assertFalse(old_request.done())
        old.release.set()
        await self.result(old_request)


if __name__ == "__main__":
    unittest.main()
