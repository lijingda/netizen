"""Serialized replacement of one Feishu message from its current desired state."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

from .physical_delivery import Outcome

Payload = TypeVar("Payload")


class ReplacementWriter(asyncio.Lock, Generic[Payload]):
    """One replacement at a time. Only a confirmed payload can suppress a write."""

    def __init__(self, key: Callable[[Payload], object]) -> None:
        super().__init__()
        self.key = key
        self.known: Payload | None = None

    async def reconcile(
        self, desired: Callable[[], Payload | None], write: Callable[[Payload], Awaitable[Outcome]],
    ) -> bool:
        async with self:
            return await self.reconcile_locked(desired, write)

    async def reconcile_locked(
        self, desired: Callable[[], Payload | None], write: Callable[[Payload], Awaitable[Outcome]],
        *, unknown_retry_delays: tuple[float, ...] = (),
    ) -> bool:
        """Recompute while the caller holds this message's replacement lock."""
        delivered = False
        delays = iter(unknown_retry_delays)
        while (payload := desired()) is not None:
            if self.known is not None and self.key(self.known) == self.key(payload):
                return True
            previous, self.known = self.known, None
            outcome = await write(payload)
            if outcome == "confirmed":
                self.known = payload
                if unknown_retry_delays:
                    return True  # Queued state changes reconcile after terminal delivery settles.
                delivered = True
            elif outcome == "unknown" and (delay := next(delays, None)) is not None:
                await asyncio.sleep(delay)
            else:
                if outcome == "rejected":
                    self.known = previous
                return False
        return delivered
