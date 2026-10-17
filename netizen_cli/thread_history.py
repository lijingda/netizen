"""Removable, read-only pagination ports on the initialized SDK connection."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Protocol

from openai_codex import AsyncCodex, AsyncThread

if TYPE_CHECKING:
    from openai_codex.generated.v2_all import ThreadItemsListResponse, ThreadTurnsListResponse

from .sdk_gap_adapter import (
    SdkFacadeMigrationRequired,
    _generated_type,
    _initialized_client,
    _require_model_fields,
)

_CAPABILITY = "thread-history-pages"


class ThreadHistoryUnavailable(RuntimeError):
    """A native page cannot be used as an exact history projection."""


class ThreadHistory(Protocol):
    async def list_turns(self, thread_id: str, *, cursor: str | None = None,
                         limit: int = 1, items_view: Literal["full", "summary"] = "full"
                         ) -> ThreadTurnsListResponse: ...

    async def list_items(self, thread_id: str, turn_id: str, *, limit: int = 20
                         ) -> ThreadItemsListResponse: ...


def facade_migration_requirements() -> tuple[str, ...]:
    candidates = {
        AsyncCodex: ("thread_turns_list", "thread_items_list", "list_thread_turns", "list_thread_items"),
        AsyncThread: ("turns", "items", "list_turns", "list_items", "turns_list", "items_list"),
    }
    names = tuple(f"{owner.__name__}.{name}" for owner, methods in candidates.items()
                  for name in methods if hasattr(owner, name))
    return (f"migration-required:{_CAPABILITY}:{','.join(names)}",) if names else ()


class AppServerThreadHistory:
    """Two fixed reads; no retry, subscription, cache, or client lifecycle.

    Cancellation ends the caller's wait; the SDK-owned synchronous request may
    still finish later. Never close the shared transport or retry that request.
    """

    def __init__(self, codex: AsyncCodex) -> None:
        if requirements := facade_migration_requirements():
            raise SdkFacadeMigrationRequired("; ".join(requirements))
        self._client = _initialized_client(codex, capability=_CAPABILITY)
        self._turn_params = _model("ThreadTurnsListParams", {
            "cursor": "cursor", "items_view": "itemsView", "limit": "limit",
            "sort_direction": "sortDirection", "thread_id": "threadId",
        })
        self._item_params = _model("ThreadItemsListParams", {
            "cursor": "cursor", "limit": "limit", "sort_direction": "sortDirection",
            "thread_id": "threadId", "turn_id": "turnId",
        })
        aliases = {"data": "data", "next_cursor": "nextCursor", "backwards_cursor": "backwardsCursor"}
        self._turn_response = _model("ThreadTurnsListResponse", aliases)
        self._item_response = _model("ThreadItemsListResponse", aliases)

    async def list_turns(self, thread_id: str, *, cursor: str | None = None,
                         limit: int = 1, items_view: Literal["full", "summary"] = "full"
                         ) -> ThreadTurnsListResponse:
        _identity(thread_id)
        _limit(limit, 10)
        if items_view not in {"full", "summary"}:
            raise ValueError("unknown Turn items view")
        if cursor is not None:
            _identity(cursor)
        params = self._turn_params(thread_id=thread_id, cursor=cursor, limit=limit,
                                   sort_direction="desc", items_view=items_view)
        response = await self._client.request(
            "thread/turns/list", params.model_dump(mode="json", by_alias=True, exclude_none=True),
            response_model=self._turn_response,
        )
        _page(response, self._turn_response, limit, cursor)
        ids = []
        for turn in response.data:
            ids.append(_identity(turn.id))
            if getattr(turn.items_view, "value", turn.items_view) != items_view:
                raise ThreadHistoryUnavailable("Native Turn items view differs from the requested view")
            _unique([_identity(item.root.id) for item in turn.items])
        _unique(ids)
        return response

    async def list_items(self, thread_id: str, turn_id: str, *, limit: int = 20
                         ) -> ThreadItemsListResponse:
        _identity(thread_id)
        _identity(turn_id)
        _limit(limit, 20)
        params = self._item_params(thread_id=thread_id, turn_id=turn_id, limit=limit,
                                   sort_direction="desc")
        response = await self._client.request(
            "thread/items/list", params.model_dump(mode="json", by_alias=True, exclude_none=True),
            response_model=self._item_response,
        )
        _page(response, self._item_response, limit, None)
        ids = []
        for entry in response.data:
            if entry.turn_id != turn_id:
                raise ThreadHistoryUnavailable("Native item belongs to a different Turn")
            ids.append(_identity(entry.item.root.id))
        _unique(ids)
        return response


def _model(name: str, aliases: dict[str, str]) -> type[Any]:
    model = _generated_type(name, capability=_CAPABILITY)
    _require_model_fields(model, capability=_CAPABILITY, aliases=aliases)
    return model


def _identity(value: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ThreadHistoryUnavailable("Native history identity or cursor is invalid")
    return value


def _limit(value: int, maximum: int) -> None:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError("History page limit is out of range")


def _unique(values: list[str]) -> None:
    if len(set(values)) != len(values):
        raise ThreadHistoryUnavailable("Native history page repeats an identity")


def _page(response: Any, model: type[Any], limit: int, cursor: str | None) -> None:
    if not isinstance(response, model) or len(response.data) > limit:
        raise ThreadHistoryUnavailable("Native history page has an invalid shape")
    for value in (response.next_cursor, response.backwards_cursor):
        if value is not None:
            _identity(value)
    if response.next_cursor is not None and response.next_cursor == cursor:
        raise ThreadHistoryUnavailable("Native history pagination did not advance")
