"""Acquire authoritative card content without changing SDK message identities."""

from __future__ import annotations

import asyncio
from typing import Any

from .card_content import (
    CardContentError,
    PreparedCardContent,
    extract_card_content,
    original_card,
    original_message_card,
)
from .message_content import HistoricalMessageUnavailable, normalized_content_type


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
    preferred_locale: str | None = None,
) -> PreparedCardContent | None:
    """Use an original card already present, or fetch it through the public SDK.

    A normalized card label or nonempty preview is not original-card evidence.
    The returned projection is the only card text consumed downstream. Source
    identities, resources and routing remain on the unchanged input message.
    """

    if normalized_content_type(message) != "interactive":
        return None
    message_id = getattr(message, "id", None) or getattr(message, "message_id", None)
    if not isinstance(message_id, str) or not message_id:
        raise MessagePreparationError("identity")
    try:
        card = original_message_card(message)
        if card is None:
            try:
                async with asyncio.timeout(timeout_seconds):
                    context = await channel.fetch_quoted_context(message_id)
            except TimeoutError as error:
                raise MessagePreparationError("timeout") from error
            except Exception as error:
                raise MessagePreparationError("unavailable") from error

            # The SDK fills a missing response ID with the requested ID. Only
            # the public raw message item proves which message was returned.
            raw = getattr(context, "raw", None)
            chat_id = getattr(getattr(message, "conversation", None), "chat_id", None)
            if (
                not isinstance(raw, dict)
                or raw.get("message_id") != message_id
                or raw.get("msg_type") != "interactive"
                or raw.get("deleted") is True
                or (chat_id and raw.get("chat_id") != chat_id)
            ):
                raise MessagePreparationError("identity")
            body = raw.get("body")
            card = original_card(body.get("content") if isinstance(body, dict) else None)
            if card is None:
                raise MessagePreparationError("unavailable")
        return extract_card_content(
            card, message_id=message_id, preferred_locale=preferred_locale,
        )
    except CardContentError as error:
        raise HistoricalMessageUnavailable(str(error)) from error
