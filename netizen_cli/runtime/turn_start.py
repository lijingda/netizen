"""In-memory identity of one ordinary turn/start, including an external start/join."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum

from .contracts import TurnStartFailed


@dataclass(frozen=True, slots=True)
class StartedTurn:
    binding_id: str
    thread_id: str
    turn_id: str


class StartFailure(Enum):
    REJECTED = "本次启动未获接受，未请求中断。"
    UNKNOWN = "Codex Turn 启动结果未确认；本次未请求中断。服务已停止接收新任务，请重启服务。"


@dataclass(eq=False, slots=True)
class TurnStartAttempt:
    resolved: asyncio.Event = field(default_factory=asyncio.Event)
    _outcome: StartedTurn | StartFailure | None = None
    _acknowledgements: set[asyncio.Event] = field(default_factory=set)

    def resolve(self, outcome: StartedTurn | StartFailure) -> None:
        self._outcome = outcome
        self.resolved.set()

    async def confirmed_target(self) -> StartedTurn:
        await self.resolved.wait()
        if isinstance(self._outcome, StartFailure):
            raise TurnStartFailed(self._outcome.value)
        assert self._outcome is not None
        return self._outcome

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

    def attach_acknowledgements(self, terminal_receipts: set[asyncio.Event]) -> None:
        terminal_receipts.update(self._acknowledgements)
        self._acknowledgements = terminal_receipts
