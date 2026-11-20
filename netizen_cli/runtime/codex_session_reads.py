"""Exact Codex reads for session collaboration, without subscribing or resuming."""

from __future__ import annotations

from typing import Any

from openai_codex import AsyncCodex, AsyncThread
from openai_codex.errors import InvalidRequestError

from ..sessions.errors import SessionToolError
from ..sessions.presentation import native_json
from ..thread_history import AppServerThreadHistory, ThreadHistory


class CodexSessionReads:
    def __init__(self, codex: AsyncCodex, history: ThreadHistory | None = None) -> None:
        self._codex = codex
        self._history = history

    async def read_metadata(self, thread_id: str) -> dict[str, Any]:
        # Constructing an SDK handle does not resume a Thread or subscribe to it.
        response = await AsyncThread(self._codex, thread_id).read(include_turns=False)
        thread = native_json(response.thread)
        if not isinstance(thread, dict) or thread.get("id") != thread_id:
            raise SessionToolError("invalid_native_response", "Native read returned a different Thread identity.")
        return thread

    async def read_turn_page(
        self, thread_id: str, *, cursor: str | None, limit: int,
    ) -> tuple[list[dict[str, Any]], str | None]:
        try:
            page = await self._history_reader().list_turns(
                thread_id, cursor=cursor, limit=limit, items_view="full",
            )
        except InvalidRequestError as error:
            if cursor is not None and "cursor" in error.message.lower():
                raise SessionToolError("invalid_cursor", "Invalid native history cursor. Start from the newest page.") from error
            raise
        return [native_json(turn) for turn in page.data], page.next_cursor

    def _history_reader(self) -> ThreadHistory:
        if self._history is None:
            self._history = AppServerThreadHistory(self._codex)
        return self._history

    async def read_wait_snapshot(self, thread_id: str) -> dict[str, Any]:
        thread = await self.read_metadata(thread_id)
        history = self._history_reader()
        # Legacy history supports Turn pages, but not item pages. Select that
        # known native mode before issuing reads; other failures never trigger
        # a whole-Thread retry or turn an unavailable snapshot into success.
        legacy = thread.get("historyMode") == "legacy"
        page = await history.list_turns(thread_id, limit=1, items_view="full" if legacy else "summary")
        thread["turns"] = [native_json(turn) for turn in page.data]
        if page.data:
            turn = thread["turns"][0]
            if legacy:
                turn["items"] = turn["items"][-20:]
            else:
                items = await history.list_items(thread_id, turn["id"], limit=20)
                # Native item pages are newest-first; the established snapshot
                # projection consumes chronological items within its one Turn.
                turn["items"] = [native_json(entry.item) for entry in reversed(items.data)]
        return thread
