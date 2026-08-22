"""Shared per-Scope coordination for Channel and Admin management actions."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator


class ScopeCoordinator:
    """Own per-Scope locks on exactly one asyncio event loop."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def hold(self, scope_key: str) -> AsyncIterator[None]:
        if not scope_key:
            raise ValueError("scope_key must not be empty")
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("ScopeCoordinator cannot be used across event loops")

        lock = self._locks.get(scope_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[scope_key] = lock
        async with lock:
            yield
