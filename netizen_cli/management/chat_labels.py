"""Best-effort Feishu chat labels for management inventory pages."""

from __future__ import annotations

import asyncio
import math
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


_POSITIVE_TTL_SECONDS = 30 * 60.0
_P2P_POSITIVE_TTL_SECONDS = 7 * 24 * 60 * 60.0
_NEGATIVE_TTL_SECONDS = 30.0
_CACHE_CAPACITY = 4_096
_LOOKUP_CONCURRENCY = 10
_LOOKUPS_PER_SECOND = 30
_LOOKUPS_PER_MINUTE = 600
_LOOKUP_BUDGET_SECONDS = 2.5


class ChatLabelProvider(Protocol):
    async def get_chat_info(self, chat_id: str) -> Any: ...

    async def get_chat_members(
        self,
        chat_id: str,
        *,
        page_size: int = 100,
        max_pages: int = 10,
        id_type: str = "open_id",
        force: bool = False,
    ) -> Sequence[Any]: ...


@dataclass(frozen=True, slots=True)
class ChatLabel:
    chat_id: str
    display_name: str
    chat_mode: str | None
    resolved: bool = True
    p2p_target_open_id: str | None = None
    chat_type: str | None = None


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    label: ChatLabel
    expires_at: float


@dataclass(frozen=True, slots=True)
class _Lookup:
    task: asyncio.Task[ChatLabel]
    budget: _LookupBudget


@dataclass(slots=True)
class _LookupBudget:
    started_at: float
    deadline: float
    timeout: asyncio.Timeout | None = None

    def extend(self, *, deadline: float, seconds: float) -> None:
        # Repeated waiters may enlarge the original budget, not renew its start.
        extended = min(deadline, self.started_at + seconds)
        if extended > self.deadline:
            self.deadline = extended
            if self.timeout is not None:
                self.timeout.reschedule(extended)


class _LookupTimedOut(Exception):
    def __init__(self, label: ChatLabel) -> None:
        super().__init__("chat label lookup deadline elapsed")
        self.label = label


class ChatLabelResolver:
    """Resolve only requested chat IDs with bounded TTL/LRU caching."""

    def __init__(
        self,
        provider: ChatLabelProvider,
        *,
        clock: Callable[[], float] = time.monotonic,
        positive_ttl_seconds: float = _POSITIVE_TTL_SECONDS,
        p2p_positive_ttl_seconds: float = _P2P_POSITIVE_TTL_SECONDS,
        negative_ttl_seconds: float = _NEGATIVE_TTL_SECONDS,
        capacity: int = _CACHE_CAPACITY,
        concurrency: int = _LOOKUP_CONCURRENCY,
        lookups_per_second: int = _LOOKUPS_PER_SECOND,
        lookups_per_minute: int = _LOOKUPS_PER_MINUTE,
        lookup_budget_seconds: float = _LOOKUP_BUDGET_SECONDS,
    ) -> None:
        if min(positive_ttl_seconds, p2p_positive_ttl_seconds, negative_ttl_seconds) <= 0:
            raise ValueError("chat label TTLs must be positive")
        if capacity < 1:
            raise ValueError("chat label cache capacity must be positive")
        if concurrency < 1:
            raise ValueError("chat label lookup concurrency must be positive")
        if lookups_per_second < 1 or lookups_per_minute < 1:
            raise ValueError("chat label lookup rates must be positive")
        if not math.isfinite(lookup_budget_seconds) or lookup_budget_seconds <= 0:
            raise ValueError("chat label lookup budget must be finite and positive")
        self._provider = provider
        self._clock = clock
        self._positive_ttl_seconds = positive_ttl_seconds
        self._p2p_positive_ttl_seconds = p2p_positive_ttl_seconds
        self._negative_ttl_seconds = negative_ttl_seconds
        self._capacity = capacity
        self._semaphore = asyncio.Semaphore(concurrency)
        self._lookups_per_second = lookups_per_second
        self._lookups_per_minute = lookups_per_minute
        self._lookup_starts: deque[float] = deque()
        self._lookup_budget_seconds = lookup_budget_seconds
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._inflight: dict[str, _Lookup] = {}
        self._workers: set[asyncio.Task[ChatLabel]] = set()

    def cached_many(self, chat_ids: Iterable[str]) -> dict[str, ChatLabel]:
        """Read current cached labels without starting or waiting for lookups."""
        return {
            chat_id: self._get_cached(chat_id) or self.fallback(chat_id)
            for chat_id in dict.fromkeys(chat_ids)
        }

    async def resolve_many(
        self,
        chat_ids: Iterable[str],
        *,
        deadline: float,
        lookup_budget_seconds: float | None = None,
    ) -> dict[str, ChatLabel]:
        budget = (
            self._lookup_budget_seconds
            if lookup_budget_seconds is None
            else lookup_budget_seconds
        )
        if not math.isfinite(budget) or budget <= 0:
            raise ValueError("chat label lookup budget must be finite and positive")
        unique = tuple(dict.fromkeys(chat_ids))
        labels: dict[str, ChatLabel] = {}
        tasks: dict[str, asyncio.Task[ChatLabel]] = {}
        loop = asyncio.get_running_loop()
        lookup_deadline = min(deadline, loop.time() + budget)

        for chat_id in unique:
            lookup = self._inflight.get(chat_id)
            if lookup is not None and lookup.task.done():
                self._finish(chat_id, lookup.task)
                lookup = None
            cached = self._get_cached(chat_id)
            if cached is not None:
                labels[chat_id] = cached
                continue
            if lookup is not None and (
                lookup.budget.deadline <= loop.time()
                or (
                    lookup.budget.timeout is not None
                    and lookup.budget.timeout.expired()
                )
            ):
                # A refresh must not join work whose budget has already elapsed.
                # Keep its worker tracked until cancellation releases its slot.
                del self._inflight[chat_id]
                lookup.task.cancel()
                lookup = None
            if lookup is None:
                shared_budget = _LookupBudget(loop.time(), lookup_deadline)
                task = asyncio.create_task(
                    self._resolve_one(chat_id, budget=shared_budget)
                )
                self._inflight[chat_id] = _Lookup(task, shared_budget)
                self._workers.add(task)
                task.add_done_callback(
                    lambda completed, resolved_id=chat_id: self._finish(
                        resolved_id,
                        completed,
                    )
                )
            else:
                lookup.budget.extend(deadline=deadline, seconds=budget)
                task = lookup.task
            tasks[chat_id] = task

        if tasks:
            timeout = max(0.0, lookup_deadline - loop.time())
            await asyncio.wait(set(tasks.values()), timeout=timeout)
            for chat_id, task in tasks.items():
                if not task.done() or task.cancelled():
                    labels[chat_id] = self.fallback(chat_id)
                    continue
                try:
                    labels[chat_id] = task.result()
                except _LookupTimedOut as error:
                    labels[chat_id] = error.label
                except Exception:
                    labels[chat_id] = self.fallback(chat_id)

        return labels

    async def aclose(self) -> None:
        tasks = tuple(self._workers)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()
        self._workers.clear()
        self._cache.clear()
        self._lookup_starts.clear()

    @staticmethod
    def fallback(
        chat_id: str,
        *,
        chat_mode: str | None = None,
        chat_type: str | None = None,
        p2p_target_open_id: str | None = None,
    ) -> ChatLabel:
        return ChatLabel(
            chat_id=chat_id,
            display_name=chat_id,
            chat_mode=chat_mode,
            resolved=False,
            p2p_target_open_id=p2p_target_open_id,
            chat_type=chat_type,
        )

    def _get_cached(self, chat_id: str) -> ChatLabel | None:
        entry = self._cache.get(chat_id)
        if entry is None:
            return None
        if entry.expires_at <= self._clock():
            del self._cache[chat_id]
            return None
        self._cache.move_to_end(chat_id)
        return entry.label

    def _finish(self, chat_id: str, task: asyncio.Task[ChatLabel]) -> None:
        self._workers.discard(task)
        lookup = self._inflight.get(chat_id)
        if lookup is None or lookup.task is not task:
            if not task.cancelled():
                task.exception()
            return
        del self._inflight[chat_id]
        if task.cancelled():
            return
        try:
            label = task.result()
        except _LookupTimedOut:
            return
        except Exception:
            label = self.fallback(chat_id)
        if not label.resolved:
            ttl = self._negative_ttl_seconds
        elif label.chat_mode == "p2p":
            ttl = self._p2p_positive_ttl_seconds
        else:
            ttl = self._positive_ttl_seconds
        self._cache[chat_id] = _CacheEntry(label, self._clock() + ttl)
        self._cache.move_to_end(chat_id)
        while len(self._cache) > self._capacity:
            self._cache.popitem(last=False)

    async def _resolve_one(self, chat_id: str, *, budget: _LookupBudget) -> ChatLabel:
        chat_mode = None
        chat_type = None
        try:
            async with asyncio.timeout_at(budget.deadline) as timeout:
                budget.timeout = timeout
                await self._semaphore.acquire()
                try:
                    await self._wait_for_lookup_start(budget=budget)
                    info = await self._before_deadline(
                        self._provider.get_chat_info(chat_id),
                        budget=budget,
                    )
                    if info is None:
                        return self.fallback(chat_id)
                    name = self._text(getattr(info, "name", None))
                    chat_mode = self._text(getattr(info, "chat_mode", None))
                    chat_type = self._text(getattr(info, "chat_type", None))
                    p2p_target_open_id = None
                    if chat_mode == "p2p":
                        try:
                            members = await self._before_deadline(
                                self._provider.get_chat_members(
                                    chat_id,
                                    page_size=100,
                                    max_pages=1,
                                    id_type="open_id",
                                ),
                                budget=budget,
                            )
                        except asyncio.CancelledError:
                            raise
                        except TimeoutError:
                            raise
                        except Exception:
                            if not name:
                                return self.fallback(
                                    chat_id,
                                    chat_mode=chat_mode,
                                    chat_type=chat_type,
                                )
                            members = ()
                        member_name, p2p_target_open_id = self._p2p_member(members)
                        name = member_name or name
                    if not name:
                        return self.fallback(
                            chat_id,
                            chat_mode=chat_mode,
                            chat_type=chat_type,
                            p2p_target_open_id=p2p_target_open_id,
                        )
                    return ChatLabel(
                        chat_id=chat_id,
                        display_name=name,
                        chat_mode=chat_mode,
                        resolved=True,
                        p2p_target_open_id=p2p_target_open_id,
                        chat_type=chat_type,
                    )
                finally:
                    self._semaphore.release()
        except asyncio.CancelledError:
            raise
        except TimeoutError as error:
            raise _LookupTimedOut(self.fallback(
                chat_id, chat_mode=chat_mode, chat_type=chat_type,
            )) from error
        except Exception:
            return self.fallback(chat_id)
        finally:
            budget.timeout = None

    async def _wait_for_lookup_start(self, *, budget: _LookupBudget) -> None:
        """Count one chat, including its member lookup, in both rolling windows."""
        while True:
            if asyncio.get_running_loop().time() >= budget.deadline:
                raise TimeoutError("chat label lookup deadline elapsed")
            now = self._clock()
            starts = self._lookup_starts
            while starts and starts[0] <= now - 60.0:
                starts.popleft()
            delay = 0.0
            if len(starts) >= self._lookups_per_second:
                delay = max(delay, starts[-self._lookups_per_second] + 1.0 - now)
            if len(starts) >= self._lookups_per_minute:
                delay = max(delay, starts[0] + 60.0 - now)
            if delay <= 0:
                # All callers share this loop; checking and recording without an
                # await prevents concurrent pages from spending the same allowance.
                starts.append(now)
                return
            await self._before_deadline(asyncio.sleep(delay), budget=budget)

    @staticmethod
    async def _before_deadline(awaitable: Any, *, budget: _LookupBudget) -> Any:
        remaining = budget.deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            if hasattr(awaitable, "close"):
                awaitable.close()
            raise TimeoutError("chat label lookup deadline elapsed")
        return await awaitable

    @classmethod
    def _p2p_member(cls, members: Sequence[Any]) -> tuple[str, str | None]:
        candidates: list[tuple[str, str | None]] = []
        for member in members:
            name = cls._text(getattr(member, "name", None))
            member_id = cls._text(getattr(member, "id", None))
            id_type = cls._text(getattr(member, "id_type", None))
            if name:
                candidates.append(
                    (name, member_id if id_type == "open_id" else None)
                )
        if len(candidates) != 1:
            return "", None
        return candidates[0]

    @staticmethod
    def _text(value: Any) -> str:
        return value.strip() if isinstance(value, str) else ""
