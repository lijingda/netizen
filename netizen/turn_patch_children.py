"""Read successful patches from children created by one exact native Turn.

These bounded, best-effort reads use the public SDK and do not resume Threads.
They are a completion-time snapshot, not an atomic snapshot of the task tree.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from openai_codex import AsyncThread
from openai_codex.generated.v2_all import (
    CollabAgentToolCallThreadItem,
    FileChangeThreadItem,
    SubAgentActivityThreadItem,
    SubAgentSessionSource,
    Thread,
    ThreadItem,
    ThreadReadResponse,
    ThreadSpawnSubAgentSource,
)


_MAX_CHILDREN = 32
_READ_TIMEOUT_SECONDS = 3.0


@dataclass(frozen=True, slots=True)
class TurnPatchBatch:
    thread_id: str
    turn_id: str
    cwd: Path
    items: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class TaskPatchChildren:
    batches: tuple[TurnPatchBatch, ...] = ()
    complete: bool = True


def _item(value: object) -> object:
    return value.root if type(value) is ThreadItem else value


def _value(value: object) -> object:
    return getattr(value, "value", value)


def _child_edges(
    items: Sequence[object], parent_id: str
) -> tuple[set[str], set[str], bool]:
    spawned: set[str] = set()
    referenced: set[str] = set()
    complete = True
    for wrapped in items:
        item = _item(wrapped)
        if type(item) is CollabAgentToolCallThreadItem:
            if item.sender_thread_id != parent_id:
                complete = False
                continue
            if _value(item.status) != "completed":
                if _value(item.status) == "inProgress":
                    complete = False
                continue
            receivers = set(item.receiver_thread_ids)
            if not receivers or any(not receiver for receiver in receivers):
                complete = False
                continue
            if _value(item.tool) == "spawnAgent":
                spawned.update(receivers)
            elif _value(item.tool) in {"sendInput", "resumeAgent", "wait"}:
                referenced.update(receivers)
        elif type(item) is SubAgentActivityThreadItem:
            if not item.agent_thread_id:
                complete = False
            elif _value(item.kind) == "started":
                spawned.add(item.agent_thread_id)
            elif _value(item.kind) == "interacted":
                referenced.add(item.agent_thread_id)
    return spawned, referenced, complete


def _has_parent(thread: Thread, parent_id: str) -> bool:
    parents = []
    if thread.parent_thread_id is not None:
        parents.append(thread.parent_thread_id)
    source = thread.source.root
    if type(source) is SubAgentSessionSource:
        child_source = source.sub_agent.root
        if type(child_source) is ThreadSpawnSubAgentSource:
            parents.append(child_source.thread_spawn.parent_thread_id.root)
    return (
        bool(parents)
        and all(parent == parent_id for parent in parents)
        and thread.forked_from_id in {None, parent_id}
    )


async def _read_thread(codex: object, thread_id: str) -> Thread:
    response = await AsyncThread(codex, thread_id).read(include_turns=True)
    if type(response) is not ThreadReadResponse or response.thread.id != thread_id:
        raise ValueError("native child patch read identity mismatch")
    thread = response.thread
    ids = [turn.id for turn in thread.turns]
    if any(not turn_id for turn_id in ids) or len(set(ids)) != len(ids):
        raise ValueError("native child patch read has ambiguous Turn identities")
    return thread


async def collect_turn_patch_children(
    codex: object,
    *,
    thread_id: str,
    turn_id: str,
    items: Sequence[object],
) -> TaskPatchChildren:
    """Collect new descendants, retaining known patches when evidence is partial.

    Old children cannot be assigned to this Turn using the SDK's Thread-only
    collab references. They make the result partial without importing history.
    Ancestor Turn IDs exclude the history copied by native subagent forks.
    """
    spawned, references, complete = _child_edges(items, thread_id)
    if not spawned:
        return TaskPatchChildren(complete=complete and not references)

    batches: list[TurnPatchBatch] = []
    parents = {thread_id: ""}
    pending: deque[tuple[str, str, frozenset[str]]] = deque()

    def enqueue(
        child_ids: set[str], parent_id: str, ancestor_turns: frozenset[str]
    ) -> None:
        nonlocal complete
        for child_id in sorted(child_ids):
            if child_id in parents:
                if parents[child_id] != parent_id:
                    complete = False
                continue
            if len(parents) - 1 >= _MAX_CHILDREN:
                complete = False
                continue
            parents[child_id] = parent_id
            pending.append((child_id, parent_id, ancestor_turns))

    try:
        async with asyncio.timeout(_READ_TIMEOUT_SECONDS):
            try:
                root = await _read_thread(codex, thread_id)
            except Exception:
                return TaskPatchChildren(complete=False)
            root_ids = [turn.id for turn in root.turns]
            # A newer root Turn may already have sent another task to the same
            # child. Its child Turn identity is not exposed by this SDK.
            # Root items are already frozen by the caller. Here only Turn IDs
            # are used; items_view describes item contents, not those identities.
            if not root_ids or root_ids[-1] != turn_id:
                return TaskPatchChildren(complete=False)
            enqueue(spawned, thread_id, frozenset(root_ids))
            while pending:
                child_id, parent_id, ancestor_turns = pending.popleft()
                try:
                    child = await _read_thread(codex, child_id)
                    if not _has_parent(child, parent_id):
                        raise ValueError("native child patch read parent mismatch")
                    cwd = Path(child.cwd.root)
                    if not cwd.is_absolute():
                        raise ValueError("native child patch read cwd is not absolute")
                except Exception:
                    complete = False
                    continue
                own_turns = [
                    turn for turn in child.turns if turn.id not in ancestor_turns
                ]
                if not own_turns:
                    complete = False
                child_ancestors = ancestor_turns | frozenset(
                    turn.id for turn in child.turns
                )
                for turn in own_turns:
                    if (
                        _value(turn.status) == "inProgress"
                        or _value(turn.items_view) != "full"
                    ):
                        complete = False
                    patches = {}
                    conflicting = set()
                    for wrapped in turn.items:
                        item = _item(wrapped)
                        if type(item) is not FileChangeThreadItem:
                            continue
                        if _value(item.status) == "inProgress":
                            complete = False
                        elif _value(item.status) == "completed":
                            if item.id in conflicting:
                                continue
                            if item.id in patches and patches[item.id] != item:
                                complete = False
                                conflicting.add(item.id)
                                del patches[item.id]
                                continue
                            patches[item.id] = item
                    if patches:
                        batches.append(
                            TurnPatchBatch(
                                child_id, turn.id, cwd, tuple(patches.values())
                            )
                        )
                    new_children, related, edges_complete = _child_edges(
                        turn.items, child_id
                    )
                    complete = complete and edges_complete
                    references.update(related)
                    enqueue(new_children, child_id, child_ancestors)
            # Admission may open while this best-effort display read is running.
            # A new root Turn can follow up with these same children, so validate
            # the boundary again before publishing any child evidence.
            if len(parents) > 1:
                try:
                    final_root = await _read_thread(codex, thread_id)
                except Exception:
                    return TaskPatchChildren(complete=False)
                if not final_root.turns or final_root.turns[-1].id != turn_id:
                    return TaskPatchChildren(complete=False)
    except TimeoutError:
        # Without the closing root read, earlier successful child reads may
        # already include work from a newly admitted root Turn.
        return TaskPatchChildren(complete=False)
    # Resolve references after the traversal: siblings may communicate before
    # their spawn item is encountered in another member of this task tree.
    return TaskPatchChildren(
        batches=tuple(batches),
        complete=complete and references.issubset(parents.keys() - {thread_id}),
    )
