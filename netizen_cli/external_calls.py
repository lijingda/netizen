"""Cancellation boundary shared by native SDK and Channel operations."""
import asyncio
from collections.abc import Awaitable
from typing import TypeVar

_T = TypeVar("_T")


class ExternalCallCancelled(RuntimeError):
    """The SDK call was cancelled independently of the task awaiting it."""


async def external_call(operation: Awaitable[_T]) -> _T:
    try:
        return await operation
    except asyncio.CancelledError:
        caller = asyncio.current_task()
        if caller is not None and caller.cancelling():
            raise
        raise ExternalCallCancelled("external operation cancelled") from None
