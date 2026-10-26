"""Authenticated collaboration callers; targets remain ordinary Bindings."""
from __future__ import annotations

from typing import Any

from ..bindings import BindingStore, SideTopicNotFound, SideTopicState, ThreadBinding
from ..runtime.contracts import SideSessionSnapshot, SideSessionState
from .errors import SessionToolError


SIDE_SESSION_TOOLS = frozenset({
    "list_threads", "list_archived_threads", "read_thread", "wait_threads",
    "send_message_to_thread",
})
SessionSource = ThreadBinding | SideSessionSnapshot


def resolve_session_source(
    bindings: BindingStore, runtime: Any, app_id: str, thread_id: str | None,
) -> SessionSource:
    if thread_id:
        binding = bindings.find_by_native_thread_id(thread_id)
        if binding is not None:
            if bindings.get_scope(binding.scope_key).app_id == app_id:
                return binding
        else:
            side = runtime.side_snapshot_for_thread(thread_id)
            if side is not None and side.state is SideSessionState.OPEN:
                try:
                    route = bindings.get_side_topic(side.side_id)
                except SideTopicNotFound:
                    pass
                else:
                    if (route.app_id == app_id and route.state is SideTopicState.OPEN
                            and side.topic_id is not None and side.topic_id == route.topic_id
                            and side.root_message_id == route.root_message_id):
                        return side
    raise SessionToolError(
        "invalid_call_context",
        "The caller must be an ordinary session or an open Side registered to this Netizen instance.",
    )
