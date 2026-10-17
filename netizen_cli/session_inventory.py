"""Bounded native catalog pages joined to this instance's ordinary sessions."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from .bindings import BindingInventoryRecord, BindingQuery, BindingStore
from .runtime.contracts import NativeThreadCatalogPage, NativeThreadMetadata, ThreadCatalogError


NATIVE_SESSION_PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class NativeSessionCursor:
    cursor: str | None
    offset: int = 0

    def __post_init__(self) -> None:
        if (
            self.cursor is not None and (not isinstance(self.cursor, str) or not self.cursor)
            or type(self.offset) is not int or not 0 <= self.offset < NATIVE_SESSION_PAGE_SIZE
        ):
            raise ValueError("Session native cursor is invalid")


@dataclass(frozen=True, slots=True)
class NativeSessionItem:
    record: BindingInventoryRecord
    metadata: NativeThreadMetadata
    # Position immediately after this item, including unconsumed source-page
    # rows. Callers that hit an output budget can resume without skipping them.
    next_cursor: NativeSessionCursor | None


@dataclass(frozen=True, slots=True)
class NativeSessionPage:
    items: tuple[NativeSessionItem, ...]
    next_cursor: NativeSessionCursor | None


def _time_matches(value: int | None, lower: float | None, upper: float | None) -> bool:
    if lower is None and upper is None:
        return True
    return value is not None and (lower is None or value >= lower) and (upper is None or value < upper)


async def query_native_sessions(
    *,
    bindings: BindingStore,
    read_page: Callable[..., Awaitable[NativeThreadCatalogPage]],
    query: BindingQuery,
    archived: bool,
    cursor: NativeSessionCursor | None,
    limit: int,
    deadline: float,
    sort_key: Literal["created_at", "updated_at"] = "updated_at",
    sort_direction: Literal["asc", "desc"] = "desc",
    created_from: float | None = None,
    created_before: float | None = None,
    updated_from: float | None = None,
    updated_before: float | None = None,
    match: Callable[[BindingInventoryRecord, NativeThreadMetadata], bool] | None = None,
) -> NativeSessionPage:
    """Fill one page in native order without reading all local candidates.

    The continuation describes unexamined native candidates, not a promise of
    another matching result. Catalog changes between requests retain native
    pagination's consistency limits; no snapshot or persistent buffer is made.
    """
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("page size must be between 1 and 100")
    scan_cursor = cursor.cursor if cursor is not None else None
    offset = cursor.offset if cursor is not None else 0
    seen_cursors = {scan_cursor} if scan_cursor is not None else set()
    selected: list[NativeSessionItem] = []
    selected_ids: set[str] = set()
    lower, upper = (
        (created_from, created_before) if sort_key == "created_at"
        else (updated_from, updated_before)
    )
    while len(selected) < limit:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("Session query deadline expired")
        page = await read_page(
            archived=archived, cursor=scan_cursor, limit=NATIVE_SESSION_PAGE_SIZE,
            sort_key=sort_key, sort_direction=sort_direction, deadline=deadline,
        )
        if len(page.threads) > NATIVE_SESSION_PAGE_SIZE or (
            page.next_cursor is not None and page.next_cursor in seen_cursors
        ):
            raise ThreadCatalogError("native Thread catalog returned an invalid page")
        eligible: list[tuple[int, NativeThreadMetadata]] = []
        range_exhausted = False
        for index, metadata in enumerate(page.threads[offset:], start=offset):
            ordered_time = getattr(metadata, sort_key)
            if ordered_time is not None and (
                sort_direction == "desc" and lower is not None and ordered_time < lower
                or sort_direction == "asc" and upper is not None and ordered_time >= upper
            ):
                range_exhausted = True
                break
            if (
                _time_matches(metadata.created_at, created_from, created_before)
                and _time_matches(metadata.updated_at, updated_from, updated_before)
                and metadata.thread_id not in selected_ids
            ):
                eligible.append((index, metadata))
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("Session query deadline expired")
        records = await bindings.query_bindings_by_native_thread_ids(
            thread_ids=tuple(metadata.thread_id for _, metadata in eligible),
            query=query, deadline_seconds=min(remaining, 0.5),
        ) if eligible else ()
        by_id = {record.binding.native_thread_id: record for record in records}
        for index, metadata in eligible:
            record = by_id.get(metadata.thread_id)
            if record is None or metadata.thread_id in selected_ids or (
                match is not None and not match(record, metadata)
            ):
                continue
            after = (
                NativeSessionCursor(scan_cursor, index + 1)
                if index + 1 < len(page.threads)
                else NativeSessionCursor(page.next_cursor) if page.next_cursor is not None
                else None
            )
            selected.append(NativeSessionItem(record, metadata, after))
            selected_ids.add(metadata.thread_id)
            if len(selected) == limit:
                return NativeSessionPage(tuple(selected), after)
        if range_exhausted or page.next_cursor is None:
            return NativeSessionPage(tuple(selected), None)
        scan_cursor = page.next_cursor
        seen_cursors.add(scan_cursor)
        offset = 0
    raise AssertionError("native session page loop did not finish")
