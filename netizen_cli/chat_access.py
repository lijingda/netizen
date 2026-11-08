"""Shared exact chat-ID validation for instance access rules."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def exact_chat_ids(values: tuple[str, ...], *, field: str,
                   error: type[ValueError]) -> tuple[str, ...]:
    if any(not isinstance(value, str) or not value or len(value) > 256
           or value != value.strip() or any(ord(c) < 32 for c in value)
           for value in values):
        raise error(f"{field} must contain non-empty exact chat IDs")
    return tuple(sorted(set(values)))


def parse_chat_rule(value: object, *, label: str, field: str,
                    error: type[ValueError]) -> tuple[Any, tuple[str, ...]]:
    if not isinstance(value, Mapping) or value.keys() - {"mode", "chatIds"}:
        raise error(f"{label} must contain only mode and chatIds")
    raw_ids = value.get("chatIds", [])
    if not isinstance(raw_ids, list):
        raise error(f"{field} must be a list")
    return value.get("mode", "all"), tuple(raw_ids)
