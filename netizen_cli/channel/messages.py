"""Public chat classification, bounded fields, and Channel reply identity checks."""

from __future__ import annotations

from collections.abc import Mapping

from lark_channel import SendResult


def public_chat_kind(chat_info: object) -> str | None:
    """Classify public ChatInfo, keeping chat visibility separate from mode."""
    # The SDK may normalize chat_type, or expose Feishu's private/public
    # visibility there. Both group and topic modes are group conversations.
    chat_type = getattr(chat_info, "chat_type", None)
    if chat_type in {"group", "p2p"}:
        return chat_type
    chat_mode = getattr(chat_info, "chat_mode", None)
    if chat_mode in {"group", "topic"}:
        return "group"
    return "p2p" if chat_mode == "p2p" else None


def _object_field(value: object, name: str) -> object | None:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _nonempty_field(value: object, name: str) -> str | None:
    field = _object_field(value, name)
    return field if isinstance(field, str) and field else None


def _send_result_error_code(result: SendResult | None) -> int | None:
    if result is None:
        return None
    code = result.raw.get("code") if result.raw is not None else None
    if isinstance(code, int) and not isinstance(code, bool):
        return code
    raw_code = result.error.raw_code if result.error is not None else None
    if isinstance(raw_code, int) and not isinstance(raw_code, bool):
        return raw_code
    return None


def _progress_card_message_id(result: SendResult) -> str | None:
    """Return an exact reply ID only when Feishu did not report failure."""

    if not result.success:
        return None
    if result.chunk_ids:
        return None
    direct = result.message_id or None
    data = result.raw.get("data") if result.raw is not None else None
    nested = _nonempty_field(data, "message_id")
    if direct is not None and nested is not None and direct != nested:
        return None
    return direct or nested
