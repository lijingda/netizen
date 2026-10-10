"""Stateless pagination over a caller-owned complete sequence."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar


T = TypeVar("T")


class PageError(ValueError):
    """The requested page is malformed or outside the complete result."""


@dataclass(frozen=True, slots=True)
class ItemPage(Generic[T]):
    items: tuple[T, ...]
    page: int
    total_pages: int
    total_items: int


def paginate_items(
    items: Sequence[T], page: int, *, page_size: int,
) -> ItemPage[T]:
    if isinstance(page, bool) or not isinstance(page, int) or page < 0:
        raise PageError("页码必须是非负整数。")
    if (
        isinstance(page_size, bool)
        or not isinstance(page_size, int)
        or page_size < 1
    ):
        raise ValueError("page_size must be a positive integer")
    total_items = len(items)
    total_pages = max(1, (total_items + page_size - 1) // page_size)
    if page >= total_pages:
        raise PageError("页码超出范围。")
    start = page * page_size
    return ItemPage(
        items=tuple(items[start : start + page_size]),
        page=page,
        total_pages=total_pages,
        total_items=total_items,
    )
