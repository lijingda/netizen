"""Source-neutral asynchronous preparation of SDK-normalized message content.

Acquisition and exact source validation belong to the caller. In particular a
quote/history target must be checked before a card fallback can read it again.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .message_content import validate_interactive_version
from .quoted_context import (
    interactive_quote_visible_text,
    needs_interactive_fallback,
)


class MessagePreparationError(RuntimeError):
    """A preparation failure; callers retain their source-specific messages."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def prepare_message_content(
    channel: Any,
    message: Any,
    *,
    timeout_seconds: float,
) -> str | None:
    """Complete visible content once using the public SDK's existing fallback.

    The SDK has already normalized the inbound/fetched message (including forward
    expansion). Only its known interactive placeholder needs an extra public read.
    The returned visible text is consumed by the same pure content projection for
    current, quoted and supplemental messages. No source semantics are inferred.
    """

    validate_interactive_version(getattr(message, "content", None))
    if not needs_interactive_fallback(message):
        return None
    message_id = getattr(message, "id", None) or getattr(message, "message_id", None)
    if not isinstance(message_id, str) or not message_id:
        raise MessagePreparationError("identity")
    try:
        async with asyncio.timeout(timeout_seconds):
            fallback = await channel.fetch_quoted_context(message_id)
    except TimeoutError as error:
        raise MessagePreparationError("timeout") from error
    except Exception as error:
        raise MessagePreparationError("unavailable") from error
    if (
        fallback is None
        or getattr(fallback, "message_id", None) != message_id
        or getattr(fallback, "content_type", None) != "interactive"
    ):
        raise MessagePreparationError("identity")
    return interactive_quote_visible_text(fallback)
