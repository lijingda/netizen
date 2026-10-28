"""Process-local final-result choices, scoped to exact physical Turns."""

from __future__ import annotations


class ReplySuppression:
    """Use the existing reply lifetime only to clean up physical-Turn choices."""

    def __init__(self) -> None:
        self._replies: dict[tuple[str, str], set[str]] = {}

    def suppress(self, thread_id: str, reply_turn_id: str, physical_turn_id: str) -> bool:
        self._replies.setdefault((thread_id, reply_turn_id), set()).add(physical_turn_id)
        return True

    def is_suppressed(self, thread_id: str, reply_turn_id: str, physical_turn_id: str) -> bool:
        return physical_turn_id in self._replies.get((thread_id, reply_turn_id), ())

    def discard(self, thread_id: str, reply_turn_id: str) -> None:
        self._replies.pop((thread_id, reply_turn_id), None)

    def discard_thread(self, thread_id: str) -> None:
        for key in tuple(self._replies):
            if key[0] == thread_id:
                del self._replies[key]

    def clear(self) -> None:
        self._replies.clear()
