"""Service shutdown: interrupt, drain and task cancellation."""

from __future__ import annotations

import asyncio
import logging


from .state import RUNTIME_LOGGER_NAME
from ..bindings import (
    SideTopicState,
)

from . import limits
from .sides import SideRuntime


logger = logging.getLogger(RUNTIME_LOGGER_NAME)


class ShutdownRuntime(SideRuntime):
    """Service shutdown: interrupt, drain and task cancellation."""

    async def interrupt_all(self) -> None:
        self._shutdown_requested = True
        self.close_admission()
        await self._drain_subscription_idle_tasks()
        snapshot = tuple(self._active.items())
        goal_snapshot = tuple(self._goals.items())
        side_snapshot = tuple(self._sides.values())
        for active in self._compacting.values():
            active.receipt_attempted.set()
        for _binding_id, active in snapshot:
            active.receipt_attempted.set()
        for active in self._retiring_turns.values():
            active.receipt_attempted.set()
        for _binding_id, active in goal_snapshot:
            active.receipt_attempted.set()
        for session in side_snapshot:
            if session.active is not None:
                session.active.receipt_attempted.set()
        main_operations = [
            self._interrupt(binding_id, active)
            for binding_id, active in snapshot
        ] + [
            self._interrupt_goal(binding_id, active)
            for binding_id, active in goal_snapshot
        ]
        side_operations = [
                self.close_side(
                    session.side_id,
                    state=SideTopicState.EXPIRED,
                )
                for session in side_snapshot
        ]
        combined_results = await asyncio.gather(
            *main_operations,
            *side_operations,
            return_exceptions=True,
        )
        results = combined_results[: len(main_operations)]
        side_results = combined_results[len(main_operations) :]
        labels: list[tuple[str, str | None, str | None]] = [
            (active.handle.thread_id, active.handle.id, None)
            for _binding_id, active in snapshot
        ] + [
            (
                active.thread_id,
                None,
                active.handle.id if active.handle is not None else None,
            )
            for _binding_id, active in goal_snapshot
        ]
        for (thread_id, turn_id, logical_turn_id), result in zip(
            labels,
            results,
            strict=True,
        ):
            if isinstance(result, BaseException):
                logger.error(
                    "native operation shutdown cleanup failed",
                    exc_info=(type(result), result, result.__traceback__),
                    extra={
                        "thread_id": thread_id,
                        "turn_id": turn_id,
                        "logical_turn_id": logical_turn_id,
                    },
                )
        for session, result in zip(side_snapshot, side_results, strict=True):
            if isinstance(result, BaseException):
                logger.error(
                    "Side shutdown cleanup failed",
                    exc_info=(type(result), result, result.__traceback__),
                    extra={
                        "side_id": session.side_id,
                        "thread_id": session.thread.id,
                    },
                )
        all_results = [*results, *side_results]
        errors = [result for result in all_results if isinstance(result, Exception)]
        if errors:
            raise ExceptionGroup("one or more native Turn cleanups failed", errors)
        cancellation = next(
            (
                result
                for result in all_results
                if isinstance(result, BaseException)
            ),
            None,
        )
        if cancellation is not None:
            raise cancellation

    async def wait_idle(self, timeout: float | None = None) -> bool:
        deadline = (
            asyncio.get_running_loop().time() + timeout
            if timeout is not None
            else None
        )
        while self._tasks:
            tasks = tuple(self._tasks)
            if deadline is None:
                await asyncio.gather(*tasks, return_exceptions=True)
                self._tasks.difference_update(
                    task for task in tasks if task.done()
                )
                continue
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            done, pending = await asyncio.wait(tasks, timeout=remaining)
            self._tasks.difference_update(done)
            if pending:
                return False
        return True

    async def cancel_tasks(self) -> None:
        tasks = tuple(self._tasks)
        side_idle_tasks = tuple(self._side_idle_tasks)
        subscription_idle_tasks = tuple(self._subscription_idle_tasks)
        # Final service teardown owns every remaining live or poisoned slot,
        # including a consumer that already ended after an unknown terminal.
        for binding_id in tuple(self._active):
            self._advance_admission_revision(binding_id)
        for binding_id in tuple(self._compacting):
            self._advance_admission_revision(binding_id)
        for binding_id in tuple(self._goals):
            self._advance_admission_revision(binding_id)
        for binding_id in tuple(self._lifecycles):
            self._advance_admission_revision(binding_id)
        self._active.clear()
        self._retiring_turns.clear()
        self._compacting.clear()
        self._lifecycles.clear()
        for record in self._subscriptions.values():
            self._cancel_subscription_idle(record)
        self._subscriptions.clear()
        for session in self._sides.values():
            self._cancel_side_idle(session)
            if session.active is not None:
                session.active.receipt_attempted.set()
        self._sides.clear()
        goal_handles = tuple(
            active.handle
            for active in self._goals.values()
            if active.handle is not None
        )
        self._goals.clear()
        for handle in goal_handles:
            try:
                await handle.aclose()
            except Exception:
                logger.debug("failed to close Goal stream during final teardown")
        for task in tasks:
            task.cancel()
        for task in side_idle_tasks:
            task.cancel()
        for task in subscription_idle_tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if side_idle_tasks:
            await asyncio.gather(*side_idle_tasks, return_exceptions=True)
        if subscription_idle_tasks:
            await asyncio.gather(*subscription_idle_tasks, return_exceptions=True)
        self._side_idle_tasks.difference_update(side_idle_tasks)
        self._subscription_idle_tasks.difference_update(subscription_idle_tasks)
        # Main task teardown is already complete. Naming was invalidated by
        # close_admission and cleaned independently while native work drained;
        # it cannot consume either the main interrupt or cancellation budget.
        if self._thread_namer is not None:
            try:
                await asyncio.wait_for(
                    self._thread_namer.shutdown(), limits.NAMING_SHUTDOWN_WAIT_SECONDS,
                )
            except TimeoutError:
                logger.warning("automatic naming final cleanup still pending")

    async def _drain_subscription_idle_tasks(self) -> None:
        tasks = tuple(self._subscription_idle_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._subscription_idle_tasks.difference_update(tasks)
