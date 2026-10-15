"""Known session-tool failures, including exact partial-operation receipts."""
from __future__ import annotations

from typing import Any


class SessionToolError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}

    def result(self) -> dict[str, Any]:
        return {**self.details, "error": {"code": self.code, "message": str(self)}}
