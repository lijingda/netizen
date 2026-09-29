"""Best-effort Feishu chat labels for management inventory pages."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


_POSITIVE_TTL_SECONDS = 10 * 60.0
_NEGATIVE_TTL_SECONDS = 30.0
_CACHE_CAPACITY = 4_096
_LOOKUP_CONCURRENCY = 10
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


class ChatLabelResolver:
    """Resolve only requested chat IDs with bounded TTL/LRU caching."""

    def __init__(
        self,
        provider: ChatLabelProvider,
        *,
        clock: Callable[[], float] = time.monotonic,
        positive_ttl_seconds: float = _POSITIVE_TTL_SECONDS,
        negative_ttl_seconds: float = _NEGATIVE_TTL_SECONDS,
        capacity: int = _CACHE_CAPACITY,
        concurrency: int = _LOOKUP_CONCURRENCY,
        lookup_budget_seconds: float = _LOOKUP_BUDGET_SECONDS,
    ) -> None:
        if positive_ttl_seconds <= 0 or negative_ttl_seconds <= 0:
            raise ValueError("chat label TTLs must be positive")
        if capacity < 1:
            raise ValueError("chat label cache capacity must be positive")
        if concurrency < 1:
            raise ValueError("chat label lookup concurrency must be positive")
        if lookup_budget_seconds <= 0:
            raise ValueError("chat label lookup budget must be positive")
        self._provider = provider
        self._clock = clock
        self._positive_ttl_seconds = positive_ttl_seconds
        self._negative_ttl_seconds = negative_ttl_seconds
        self._capacity = capacity
        self._semaphore = asyncio.Semaphore(concurrency)
        self._lookup_budget_seconds = lookup_budget_seconds
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._inflight: dict[str, asyncio.Task[ChatLabel]] = {}

    async def resolve_many(
        self,
        chat_ids: Iterable[str],
        *,
        deadline: float,
    ) -> dict[str, ChatLabel]:
        unique = tuple(dict.fromkeys(chat_ids))
        labels: dict[str, ChatLabel] = {}
        tasks: dict[str, asyncio.Task[ChatLabel]] = {}
        loop = asyncio.get_running_loop()
        lookup_deadline = min(deadline, loop.time() + self._lookup_budget_seconds)

        for chat_id in unique:
            cached = self._get_cached(chat_id)
            if cached is not None:
                labels[chat_id] = cached
                continue
            task = self._inflight.get(chat_id)
            if task is None:
                task = asyncio.create_task(
                    self._resolve_one(chat_id, deadline=lookup_deadline)
                )
                self._inflight[chat_id] = task
                task.add_done_callback(
                    lambda completed, resolved_id=chat_id: self._finish(
                        resolved_id,
                        completed,
                    )
                )
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
                except Exception:
                    labels[chat_id] = self.fallback(chat_id)

        return labels

    async def aclose(self) -> None:
        tasks = tuple(self._inflight.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()
        self._cache.clear()

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
        if self._inflight.get(chat_id) is task:
            del self._inflight[chat_id]
        if task.cancelled():
            return
        try:
            label = task.result()
        except Exception:
            label = self.fallback(chat_id)
        ttl = (
            self._positive_ttl_seconds
            if label.resolved
            else self._negative_ttl_seconds
        )
        self._cache[chat_id] = _CacheEntry(label, self._clock() + ttl)
        self._cache.move_to_end(chat_id)
        while len(self._cache) > self._capacity:
            self._cache.popitem(last=False)

    async def _resolve_one(self, chat_id: str, *, deadline: float) -> ChatLabel:
        try:
            async with self._semaphore:
                info = await self._before_deadline(
                    self._provider.get_chat_info(chat_id),
                    deadline=deadline,
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
                            deadline=deadline,
                        )
                    except asyncio.CancelledError:
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
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return self.fallback(chat_id)
        except Exception:
            return self.fallback(chat_id)

    @staticmethod
    async def _before_deadline(awaitable: Any, *, deadline: float) -> Any:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            if hasattr(awaitable, "close"):
                awaitable.close()
            raise TimeoutError("chat label lookup deadline elapsed")
        return await asyncio.wait_for(awaitable, timeout=remaining)

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
