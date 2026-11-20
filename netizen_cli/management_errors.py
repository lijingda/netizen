"""Netizen errors shared by management operations and their adapters."""

from typing import Literal


class ManagementError(RuntimeError):
    """Base class for stable application-boundary precondition failures."""


class ConversationReadError(ManagementError):
    """The exact native conversation read failed before a page was available."""

    def __init__(self, kind: Literal["invalid_cursor", "rpc_unavailable", "unavailable"]) -> None:
        super().__init__(kind)
        self.kind = kind
