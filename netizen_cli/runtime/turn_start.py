"""In-memory identity of one ordinary turn/start, including an external start/join."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass(eq=False, slots=True)
class TurnStartAttempt:
    binding_id: str
    thread_id: str
    resolved: asyncio.Event = field(default_factory=asyncio.Event)
    turn_id: str | None = None
    unconfirmed: bool = False
    _acknowledgements: set[asyncio.Event] = field(default_factory=set)

    @contextmanager
    def acknowledging(self) -> Iterator[None]:
        """An already-started receipt may still be in flight when native confirms."""
        done = asyncio.Event()
        self._acknowledgements.add(done)
        try:
            yield
        finally:
            done.set()
            self._acknowledgements.discard(done)

    def confirm(self, turn_id: str, terminal_receipts: set[asyncio.Event]) -> None:
        self.turn_id = turn_id
        terminal_receipts.update(self._acknowledgements)
        self._acknowledgements = terminal_receipts
