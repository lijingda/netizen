"""Bounded field access and reply identity checks for Channel send results."""

from __future__ import annotations

from collections.abc import Mapping


def _object_field(value: object, name: str) -> object | None:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _nonempty_field(value: object, name: str) -> str | None:
    field = _object_field(value, name)
    return field if isinstance(field, str) and field else None


def _progress_card_message_id(result: object) -> str | None:
    """Return an exact reply ID only when Feishu did not report failure."""

    if getattr(result, "success", True) is False:
        return None
    if getattr(result, "chunk_ids", None):
        return None
    direct = _nonempty_field(result, "message_id")
    raw = _object_field(result, "raw")
    data = _object_field(raw, "data")
    nested = _nonempty_field(data, "message_id")
    if direct is not None and nested is not None and direct != nested:
        return None
    return direct or nested
