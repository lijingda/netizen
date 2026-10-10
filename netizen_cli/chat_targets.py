"""Shared, read-only validation contract for submitted Feishu destinations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal


class ChatTargetError(RuntimeError):
    """Stable public error; never expose upstream credentials or response text."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ValidatedChatTarget:
    chat_id: str
    chat_kind: Literal["group", "p2p"]


ChatTargetValidator = Callable[[str], Awaitable[ValidatedChatTarget]]
