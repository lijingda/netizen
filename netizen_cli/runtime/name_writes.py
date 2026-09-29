"""One process-local write lock per Binding, shared by every naming policy."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _NameLock:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    contenders: int = 0

    async def acquire(self, *, wait: bool) -> bool:
        # Include queued owners, not just lock.locked(): release can wake a
        # manual waiter before that waiter has actually resumed. An automatic
        # writer must neither overtake it nor join its queue.
        if not wait and self.contenders:
            return False
        self.contenders += 1
        try:
            await self.lock.acquire()
        except BaseException:
            self.contenders -= 1
            raise
        return True

    def release(self) -> None:
        self.lock.release()
        self.contenders -= 1


class ThreadNameWrites:
    def __init__(self) -> None:
        self._locks: dict[str, _NameLock] = {}
        self._tasks: set[asyncio.Task[str | None]] = set()

    async def write(
        self,
        binding_id: str,
        operation: Callable[[], Coroutine[Any, Any, str | None]],
        *,
        wait: bool,
    ) -> str | None:
        lock = self._locks.setdefault(binding_id, _NameLock())
        if not await lock.acquire(wait=wait):
            return None
        # Once admitted, the actual writer owns the lock. Cancelling a caller
        # cannot retract an SDK RPC or let a later write overtake it. Cancelling
        # a waiter above, however, never starts an operation.
        try:
            worker = asyncio.create_task(operation(), name=f"thread-name-write:{binding_id}")
        except BaseException:
            lock.release()
            raise
        self._tasks.add(worker)

        def finished(done: asyncio.Task[str | None]) -> None:
            lock.release()
            self._tasks.discard(done)
            if not done.cancelled():
                done.exception()

        worker.add_done_callback(finished)
        return await asyncio.shield(worker)
