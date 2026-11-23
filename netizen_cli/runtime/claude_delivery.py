"""Per-anchor Claude presentation, independent of native execution and client lifetime."""
from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
import logging
from typing import Any

from ..external_calls import external_call
from ..ordinary_sessions import ClaudeInputAnchor

logger = logging.getLogger(__name__)
Send = Callable[[], Coroutine[Any, Any, None]]


@dataclass(slots=True)
class AnchorDelivery:
    queues: dict[str | None, deque[Send]] = field(default_factory=dict)
    receipts: set[asyncio.Event] = field(default_factory=set)
    workers: set[asyncio.Task[None]] = field(default_factory=set)
    cleanups: set[asyncio.Task[None]] = field(default_factory=set)
    closed: bool = False

    def enqueue(self, key: str | None, receipts: list[asyncio.Event], stop_receipt: asyncio.Event | None,
                send: Send, *, binding_id: str, changed: asyncio.Event, settled: Callable[[], None] | None) -> None:
        if self.closed:
            return
        self.receipts.update(receipts)

        async def deliver() -> None:
            try:
                for receipt in receipts:
                    await receipt.wait()
                    self.receipts.discard(receipt)
                if stop_receipt:
                    await stop_receipt.wait()
                await external_call(send())
            finally:
                if settled:
                    settled()

        if key in self.queues:
            self.queues[key].append(deliver)
            return
        queue = self.queues[key] = deque([deliver])

        async def drain_anchor() -> None:
            try:
                while queue:
                    try:
                        await queue.popleft()()
                    except Exception:
                        logger.warning("Claude completion delivery failed for Binding %s", binding_id)
            finally:
                del self.queues[key]
                changed.set()

        worker = asyncio.create_task(drain_anchor())
        self.workers.add(worker)
        worker.add_done_callback(self.workers.discard)

    def release_receipts(self) -> None:
        for receipt in self.receipts:
            receipt.set()

    def retire(self, anchor: ClaudeInputAnchor, turn_id: str, binding_id: str) -> None:
        if self.closed:
            return

        async def cleanup() -> None:
            try:
                await external_call(anchor.stop_reactions(turn_id))
            except Exception:
                logger.warning("Claude reaction cleanup failed for Binding %s", binding_id)

        task = asyncio.create_task(cleanup())
        self.cleanups.add(task)
        task.add_done_callback(self.cleanups.discard)

    async def notice(self, send: Send) -> None:
        if not self.closed:
            task = asyncio.create_task(send())
            self.workers.add(task)
            try:
                await asyncio.gather(task, return_exceptions=True)
            finally:
                self.workers.discard(task)

    async def drain(self) -> None:
        try:
            await asyncio.gather(*tuple(self.workers), return_exceptions=True)
        finally:
            self.queues.clear()
            self.receipts.clear()

    async def join_cleanups(self) -> None:
        await asyncio.gather(*tuple(self.cleanups), return_exceptions=True)

    async def cancel(self) -> None:
        self.closed = True
        tasks = (*self.workers, *self.cleanups)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
