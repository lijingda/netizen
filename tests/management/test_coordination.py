import asyncio
import threading
import unittest

from netizen.management.coordination import ScopeCoordinator


class ScopeCoordinatorTest(unittest.IsolatedAsyncioTestCase):
    async def test_same_scope_is_serial_and_different_scopes_can_overlap(self) -> None:
        coordinator = ScopeCoordinator()
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        same_scope_entered = asyncio.Event()
        other_scope_entered = asyncio.Event()

        async def first() -> None:
            async with coordinator.hold("scope-a"):
                first_entered.set()
                await release_first.wait()

        async def same_scope() -> None:
            await first_entered.wait()
            async with coordinator.hold("scope-a"):
                same_scope_entered.set()

        async def other_scope() -> None:
            await first_entered.wait()
            async with coordinator.hold("scope-b"):
                other_scope_entered.set()

        tasks = tuple(
            asyncio.create_task(operation())
            for operation in (first, same_scope, other_scope)
        )
        await first_entered.wait()
        await asyncio.wait_for(other_scope_entered.wait(), timeout=1)
        self.assertFalse(same_scope_entered.is_set())
        release_first.set()
        await asyncio.gather(*tasks)
        self.assertTrue(same_scope_entered.is_set())

    async def test_exception_releases_scope(self) -> None:
        coordinator = ScopeCoordinator()

        with self.assertRaisesRegex(RuntimeError, "boom"):
            async with coordinator.hold("scope-a"):
                raise RuntimeError("boom")

        async with coordinator.hold("scope-a"):
            pass

    async def test_empty_scope_is_rejected(self) -> None:
        coordinator = ScopeCoordinator()

        with self.assertRaisesRegex(ValueError, "scope_key"):
            async with coordinator.hold(""):
                self.fail("empty Scope must not be acquired")

    async def test_cross_event_loop_use_is_rejected(self) -> None:
        coordinator = ScopeCoordinator()
        async with coordinator.hold("scope-a"):
            pass

        result: list[BaseException | None] = []

        def use_from_another_loop() -> None:
            async def operation() -> None:
                async with coordinator.hold("scope-a"):
                    pass

            try:
                asyncio.run(operation())
            except BaseException as error:
                result.append(error)
            else:
                result.append(None)

        thread = threading.Thread(target=use_from_another_loop)
        thread.start()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], RuntimeError)
        self.assertIn("event loops", str(result[0]))


if __name__ == "__main__":
    unittest.main()
