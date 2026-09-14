"""Validate the single Feishu user targeted by a completion mention."""

from __future__ import annotations

import re


_OPEN_ID = re.compile(r"ou_[A-Za-z0-9_]{1,125}")


def valid_completion_mention_user_id(value: object) -> str | None:
    """Return an inert open_id, excluding broadcast IDs and markup injection."""

    if isinstance(value, str) and _OPEN_ID.fullmatch(value) is not None:
        return value
    return None
