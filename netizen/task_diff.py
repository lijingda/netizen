"""Compose one root task's causal file changes into verified net line counts."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import threading
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence, TypeVar

from .turn_files import (
    TurnDiffFileStats,
    TurnDiffSummary,
    _git_header_paths,
    _metadata_path,
    _normalize_diff_path,
)


_ZERO_OID = "0" * 40
_INDEX_LINE = re.compile(r"^index ([0-9a-f]{40})\.\.([0-9a-f]{40})$")
_HUNK_HEADER = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@$"
)
_TRIGGER_TOOLS = frozenset({"spawnAgent", "sendInput"})
_MAX_EVENTS = 4_096
_MAX_EVENT_BYTES = 32 * 1024 * 1024
_MAX_TURNS = 256
_MAX_COLLAB_CALLS = 256
_MAX_PATHS = 256
_MAX_PATH_CHARS = 4_096
_MAX_EDGES_PER_PATH = 64
_MAX_TOTAL_EDGES = 256
_MAX_DIFF_CHARS = 8 * 1024 * 1024
_MAX_DIFF_LINES = 200_000
_MAX_HUNKS = 8_192
_MAX_LINE_COUNT = 50_000
_MAX_TOTAL_LINE_COUNT = 200_000
_MAX_MYERS_WORK = 2_000_000
_MAX_TEXT_WORK = 64 * 1024 * 1024
_MAX_FILE_BYTES = 16 * 1024 * 1024
_MAX_TOTAL_ANCHOR_BYTES = 32 * 1024 * 1024
_COMPOSE_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class TaskThreadStarted:
    sequence: int
    thread_id: str
    parent_thread_id: str | None


@dataclass(frozen=True, slots=True)
class TaskTurnStarted:
    sequence: int
    thread_id: str
    turn_id: str


@dataclass(frozen=True, slots=True)
class TaskTurnCompleted:
    sequence: int
    thread_id: str
    turn_id: str
    status: str


@dataclass(frozen=True, slots=True)
class TaskTurnDiffUpdated:
    sequence: int
    thread_id: str
    turn_id: str
    diff: str


@dataclass(frozen=True, slots=True)
class TaskFileChangeCompleted:
    sequence: int
    thread_id: str
    turn_id: str
    item_id: str
    status: str
    paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TaskCollabToolCall:
    sequence: int
    thread_id: str
    turn_id: str
    item_id: str
    phase: Literal["started", "completed"]
    tool: str
    status: str
    sender_thread_id: str
    receiver_thread_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TaskCaptureInvalid:
    sequence: int
    reason: str


TaskDiffEvent = (
    TaskThreadStarted
    | TaskTurnStarted
    | TaskTurnCompleted
    | TaskTurnDiffUpdated
    | TaskFileChangeCompleted
    | TaskCollabToolCall
    | TaskCaptureInvalid
)


@dataclass(frozen=True, slots=True)
class TaskDiffComposition:
    """A display override; ``None`` preserves one exact root physical diff."""

    override: TurnDiffSummary | None
    complete: bool
    descendant_turns: int = 0
    reason: str | None = None

    @classmethod
    def unavailable(cls, reason: str) -> TaskDiffComposition:
        return cls(TurnDiffSummary(), False, reason=reason)


@dataclass(frozen=True, slots=True)
class _TurnWindow:
    thread_id: str
    turn_id: str
    started: int
    completed: int


@dataclass(frozen=True, slots=True)
class _CollabCall:
    thread_id: str
    turn_id: str
    item_id: str
    started: int
    completed: int
    tool: str
    status: str
    sender_thread_id: str
    receiver_thread_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    old_lines: tuple[str, ...]
    new_lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _VersionEdge:
    path: str
    old_oid: str
    new_oid: str
    hunks: tuple[_Hunk, ...]


@dataclass(frozen=True, slots=True)
class _OidEdge:
    path: str
    old_oid: str
    new_oid: str


_Edge = TypeVar("_Edge", _VersionEdge, _OidEdge)


@dataclass(slots=True)
class _CompositionBudget:
    diff_chars: int = 0
    diff_lines: int = 0
    hunks: int = 0
    edges: int = 0
    text_work: int = 0
    text_lines: int = 0
    myers_work: int = 0

    def charge_diff(self, value: str) -> None:
        self.diff_chars += len(value)
        if self.diff_chars > _MAX_DIFF_CHARS:
            raise _Unavailable("task diff text exceeds its aggregate bound")
        self.diff_lines += _count_text_lines(value)
        if self.diff_lines > _MAX_DIFF_LINES:
            raise _Unavailable("task diff has too many aggregate lines")

    def charge_edge(self) -> None:
        self.edges += 1
        if self.edges > _MAX_TOTAL_EDGES:
            raise _Unavailable("task diff contains too many version edges")

    def charge_hunk(self) -> None:
        self.hunks += 1
        if self.hunks > _MAX_HUNKS:
            raise _Unavailable("task diff contains too many hunks")

    def split_text(self, value: str) -> list[str]:
        self.text_work += len(value)
        if self.text_work > _MAX_TEXT_WORK:
            raise _Unavailable("task text reconstruction exceeded its work bound")
        lines = _text_lines(value)
        if len(lines) > _MAX_LINE_COUNT:
            raise _Unavailable("task file has too many lines to diff safely")
        self.text_lines += len(lines)
        if self.text_lines > _MAX_TOTAL_LINE_COUNT:
            raise _Unavailable("task files have too many aggregate lines")
        return lines

    def charge_myers(self) -> None:
        self.myers_work += 1
        if self.myers_work > _MAX_MYERS_WORK:
            raise _Unavailable("task line diff exceeded its work bound")


class _Unavailable(ValueError):
    pass


def compose_task_diff(
    events: Sequence[TaskDiffEvent],
    *,
    root_thread_id: str,
    root_turn_id: str,
    cwd: Path,
    include_prior_root_turns: bool = False,
) -> TaskDiffComposition:
    """Return a verified task-level override or fail closed.

    A single root physical Turn without descendants deliberately returns
    ``override=None`` so callers retain the App Server's exact Turn diff and
    its existing parsing semantics.
    """

    try:
        ordered = _validated_order(events)
        windows, descendants = _causal_turns(
            ordered,
            root_thread_id=root_thread_id,
            root_turn_id=root_turn_id,
            include_prior_root_turns=include_prior_root_turns,
        )
        if not descendants and len(windows) == 1:
            return TaskDiffComposition(None, True)
        if not _COMPOSE_LOCK.acquire(blocking=False):
            return TaskDiffComposition.unavailable("task diff composer is busy")
        try:
            summary = _compose_multi_turn_diff(ordered, windows=windows, cwd=cwd)
        finally:
            _COMPOSE_LOCK.release()
        return TaskDiffComposition(
            summary,
            True,
            descendant_turns=len(descendants),
        )
    except (OSError, UnicodeError, ValueError, TypeError, OverflowError) as error:
        return TaskDiffComposition.unavailable(str(error) or "task diff unavailable")


def _validated_order(events: Sequence[TaskDiffEvent]) -> tuple[TaskDiffEvent, ...]:
    ordered = tuple(events)
    if len(ordered) > _MAX_EVENTS:
        raise _Unavailable("task capture contains too many notifications")
    previous = -1
    retained_bytes = 0
    for event in ordered:
        sequence = getattr(event, "sequence", None)
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence < 0
            or sequence <= previous
        ):
            raise _Unavailable("task notification order is invalid")
        previous = sequence
        if isinstance(event, TaskCaptureInvalid):
            raise _Unavailable(event.reason)
        retained_bytes += _event_size(event)
        if retained_bytes > _MAX_EVENT_BYTES:
            raise _Unavailable("task capture exceeds its aggregate size bound")
    return ordered


def _event_size(event: TaskDiffEvent) -> int:
    if isinstance(event, TaskTurnDiffUpdated):
        return 256 + len(event.diff) * 4
    if isinstance(event, TaskFileChangeCompleted):
        return 256 + sum(len(path) * 4 for path in event.paths)
    return 256


def _causal_turns(
    events: Sequence[TaskDiffEvent],
    *,
    root_thread_id: str,
    root_turn_id: str,
    include_prior_root_turns: bool,
) -> tuple[tuple[_TurnWindow, ...], frozenset[tuple[str, str]]]:
    starts: dict[tuple[str, str], int] = {}
    completions: dict[tuple[str, str], tuple[int, str]] = {}
    thread_parents: dict[str, tuple[int, str | None]] = {}
    collab_parts: dict[
        tuple[str, str, str], list[TaskCollabToolCall]
    ] = defaultdict(list)

    for event in events:
        if isinstance(event, TaskThreadStarted):
            previous = thread_parents.get(event.thread_id)
            current = (event.sequence, event.parent_thread_id)
            if previous is not None:
                if previous[1] != current[1]:
                    raise _Unavailable("child Thread parent identity changed")
                raise _Unavailable("duplicate Thread start notification")
            thread_parents[event.thread_id] = current
        elif isinstance(event, TaskTurnStarted):
            key = (event.thread_id, event.turn_id)
            if key in starts:
                raise _Unavailable("duplicate Turn start notification")
            starts[key] = event.sequence
        elif isinstance(event, TaskTurnCompleted):
            key = (event.thread_id, event.turn_id)
            if key in completions:
                raise _Unavailable("duplicate Turn completion notification")
            completions[key] = (event.sequence, event.status)
        elif isinstance(event, TaskCollabToolCall):
            collab_parts[(event.thread_id, event.turn_id, event.item_id)].append(event)

    if len(set(starts) | set(completions)) > _MAX_TURNS:
        raise _Unavailable("task capture contains too many Turns")
    if len(collab_parts) > _MAX_COLLAB_CALLS:
        raise _Unavailable("task capture contains too many collab calls")

    root_key = (root_thread_id, root_turn_id)
    root_start = starts.get(root_key)
    root_completion = completions.get(root_key)
    if root_start is None or root_completion is None:
        raise _Unavailable("root Turn lifecycle is incomplete")
    root_completed, root_status = root_completion
    if root_start >= root_completed or root_status != "completed":
        raise _Unavailable("root Turn did not complete successfully")
    root = _TurnWindow(
        root_thread_id,
        root_turn_id,
        root_start,
        root_completed,
    )

    root_keys = {root_key}
    if include_prior_root_turns:
        root_candidates = {
            key
            for key, sequence in starts.items()
            if key[0] == root_thread_id and sequence < root.completed
        } | {
            key
            for key, (sequence, _status) in completions.items()
            if key[0] == root_thread_id and sequence <= root.completed
        }
        for key in root_candidates:
            started = starts.get(key)
            completed = completions.get(key)
            if started is None or completed is None:
                raise _Unavailable("captured root Turn lifecycle is incomplete")
            if not started < completed[0] <= root.completed:
                raise _Unavailable("captured root Turn lifecycle order is invalid")
            if started > root.started:
                raise _Unavailable("final root Turn is not the last captured root Turn")
            if completed[1] != "completed":
                raise _Unavailable("captured root Turn did not complete successfully")
        root_keys.update(root_candidates)
    scope_start = min(starts[key] for key in root_keys)

    included: set[tuple[str, str]] = set(root_keys)
    descendants: set[tuple[str, str]] = set()
    claimed_starts: set[tuple[str, str]] = set(root_keys)
    processed_calls: set[tuple[str, str, str]] = set()
    mentioned_receivers: set[str] = set()

    while True:
        progress = False
        calls = _collab_calls(
            {
                key: value
                for key, value in collab_parts.items()
                if (key[0], key[1]) in included
            }
        )
        for call in calls:
            call_key = (call.thread_id, call.turn_id, call.item_id)
            parent_key = (call.thread_id, call.turn_id)
            if call_key in processed_calls or parent_key not in included:
                continue
            processed_calls.add(call_key)
            progress = True
            parent_start = starts[parent_key]
            parent_completed = completions[parent_key][0]
            if not (parent_start <= call.started <= call.completed <= parent_completed):
                raise _Unavailable("collab call falls outside its parent Turn")
            if call.sender_thread_id != call.thread_id:
                raise _Unavailable("collab sender does not match its parent Turn")
            mentioned_receivers.update(call.receiver_thread_ids)
            if call.tool not in _TRIGGER_TOOLS:
                continue
            if call.status != "completed":
                if call.status == "inProgress":
                    raise _Unavailable("collab call lifecycle is incomplete")
                continue
            if not call.receiver_thread_ids:
                raise _Unavailable("collab call has no receiver Thread")
            for receiver in call.receiver_thread_ids:
                active = next(
                    (
                        key
                        for key in included
                        if key[0] == receiver
                        and starts[key] <= call.started < completions[key][0]
                    ),
                    None,
                )
                if call.tool == "sendInput" and active is not None:
                    continue
                candidates = sorted(
                    (
                        (sequence, key)
                        for key, sequence in starts.items()
                        if key[0] == receiver
                        and key not in claimed_starts
                        and call.started <= sequence < root.completed
                    ),
                    key=lambda item: item[0],
                )
                if not candidates:
                    raise _Unavailable("collab-triggered child Turn is missing")
                _, child_key = candidates[0]
                child_completion = completions.get(child_key)
                if child_completion is None or child_completion[0] > root.completed:
                    raise _Unavailable("child Turn lifecycle is incomplete")
                if starts[child_key] >= child_completion[0]:
                    raise _Unavailable("child Turn lifecycle order is invalid")
                if child_completion[1] != "completed":
                    raise _Unavailable("child Turn did not complete successfully")
                if call.tool == "spawnAgent":
                    parent = thread_parents.get(receiver)
                    if (
                        parent is None
                        or parent[1] != call.thread_id
                        or not call.started <= parent[0] < root.completed
                    ):
                        raise _Unavailable("spawned child Thread ancestry is unverified")
                claimed_starts.add(child_key)
                included.add(child_key)
                descendants.add(child_key)
        if not progress:
            break

    causal_threads = {thread_id for thread_id, _ in included}
    known_descendant_threads = {
        thread_id for thread_id, _ in descendants
    } | mentioned_receivers
    spawned_in_scope: set[str] = set()
    for thread_id, (sequence, parent_thread_id) in thread_parents.items():
        if (
            parent_thread_id in causal_threads
            and scope_start < sequence < root.completed
        ):
            known_descendant_threads.add(thread_id)
            spawned_in_scope.add(thread_id)
    if any(
        not any(
            key[0] == thread_id
            and scope_start < sequence < root.completed
            for key, sequence in starts.items()
        )
        for thread_id in spawned_in_scope
    ):
        raise _Unavailable("descendant Thread has no observable Turn start")
    unclaimed = {
        key
        for key, sequence in starts.items()
        if key[0] in known_descendant_threads
        and scope_start < sequence < root.completed
        and key not in included
    }
    if unclaimed:
        raise _Unavailable("descendant Thread has an unclaimed Turn")

    windows = tuple(
        _TurnWindow(
            thread_id,
            turn_id,
            starts[(thread_id, turn_id)],
            completions[(thread_id, turn_id)][0],
        )
        for thread_id, turn_id in sorted(
            included,
            key=lambda key: starts[key],
        )
    )
    return windows, frozenset(descendants)


def _collab_calls(
    parts: dict[tuple[str, str, str], list[TaskCollabToolCall]],
) -> tuple[_CollabCall, ...]:
    result: list[_CollabCall] = []
    for (thread_id, turn_id, item_id), notifications in parts.items():
        started = [item for item in notifications if item.phase == "started"]
        completed = [item for item in notifications if item.phase == "completed"]
        if len(started) != 1 or len(completed) != 1:
            raise _Unavailable("collab call lifecycle is incomplete")
        final = completed[0]
        first_sequence = started[0].sequence
        all_parts = (*started, final)
        if any(
            item.thread_id != thread_id
            or item.turn_id != turn_id
            or item.item_id != item_id
            or item.tool != final.tool
            or item.sender_thread_id != final.sender_thread_id
            for item in all_parts
        ):
            raise _Unavailable("collab call identity changed")
        if started[0].status != "inProgress" or first_sequence >= final.sequence:
            raise _Unavailable("collab call lifecycle order is invalid")
        if (
            started
            and started[0].receiver_thread_ids
            and final.receiver_thread_ids
            and started[0].receiver_thread_ids != final.receiver_thread_ids
        ):
            raise _Unavailable("collab receiver identity changed")
        receivers = final.receiver_thread_ids or started[0].receiver_thread_ids
        result.append(
            _CollabCall(
                thread_id,
                turn_id,
                item_id,
                first_sequence,
                final.sequence,
                final.tool,
                final.status,
                final.sender_thread_id,
                receivers,
            )
        )
    return tuple(sorted(result, key=lambda item: (item.started, item.completed)))


def _compose_multi_turn_diff(
    events: Sequence[TaskDiffEvent],
    *,
    windows: Sequence[_TurnWindow],
    cwd: Path,
) -> TurnDiffSummary:
    try:
        root = cwd.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise _Unavailable("task Project directory is unavailable") from error
    if not root.is_dir():
        raise _Unavailable("task Project path is not a directory")

    budget = _CompositionBudget()
    final_edges, checkpoint_edges = _turn_snapshot_edges(
        events,
        windows=windows,
        cwd=root,
        budget=budget,
    )
    edges_by_path: dict[str, list[_VersionEdge]] = defaultdict(list)
    for edge in final_edges:
        bucket = edges_by_path[edge.path]
        bucket.append(edge)
        if len(bucket) > _MAX_EDGES_PER_PATH:
            raise _Unavailable("one task path contains too many version edges")
    checkpoints_by_path: dict[str, list[_OidEdge]] = defaultdict(list)
    for edge in checkpoint_edges:
        bucket = checkpoints_by_path[edge.path]
        bucket.append(edge)
        if len(bucket) > _MAX_EDGES_PER_PATH:
            raise _Unavailable("one task path contains too many checkpoints")

    causal_paths = set(edges_by_path)
    if not causal_paths:
        raise _Unavailable("multi-Turn task has no provable file change")
    if set(checkpoints_by_path) != causal_paths:
        raise _Unavailable("task checkpoints disagree with final Turn paths")
    if len(causal_paths) > _MAX_PATHS:
        raise _Unavailable("task diff contains too many paths")
    _reject_noncausal_path_conflicts(
        events,
        windows=windows,
        causal_paths=causal_paths,
        cwd=root,
        budget=budget,
    )

    files: list[TurnDiffFileStats] = []
    additions = 0
    deletions = 0
    changed = False
    anchor_bytes = 0
    for path in sorted(causal_paths):
        final_content, file_bytes = _read_current_text(root, path)
        anchor_bytes += file_bytes
        if anchor_bytes > _MAX_TOTAL_ANCHOR_BYTES:
            raise _Unavailable("task files exceed the aggregate read bound")
        final_oid = _git_blob_oid(final_content) if final_content is not None else _ZERO_OID
        checkpoints = _unique_linear_chain(checkpoints_by_path[path])
        chain = _unique_linear_chain(edges_by_path[path])
        if (
            checkpoints[0].old_oid != chain[0].old_oid
            or checkpoints[-1].new_oid != chain[-1].new_oid
        ):
            raise _Unavailable("task checkpoints disagree with the content chain")
        if final_oid != checkpoints[-1].new_oid:
            raise _Unavailable("current task file does not match its terminal mutation")
        baseline = final_content
        for edge in reversed(chain):
            baseline = _reverse_edge(baseline, edge, budget=budget)
        if baseline == final_content:
            continue
        path_additions, path_deletions = _minimal_line_counts(
            baseline or "",
            final_content or "",
            budget=budget,
        )
        additions += path_additions
        deletions += path_deletions
        changed = True
        if final_content is not None:
            files.append(
                TurnDiffFileStats(path, path_additions, path_deletions)
            )
    if not changed:
        return TurnDiffSummary(0, 0)
    return TurnDiffSummary(additions, deletions, tuple(files))


def _turn_snapshot_edges(
    events: Sequence[TaskDiffEvent],
    *,
    windows: Sequence[_TurnWindow],
    cwd: Path,
    budget: _CompositionBudget,
) -> tuple[tuple[_VersionEdge, ...], tuple[_OidEdge, ...]]:
    """Use every snapshot as OID evidence and retain only final Turn hunks.

    Codex 0.147 uses the same empty payload for a legitimate net-zero tracker
    and for tracker invalidation, while a pure rename can produce no payload at
    all. Neither case is distinguishable at this boundary, so both fail closed.
    """

    selected: list[_VersionEdge] = []
    checkpoints: list[_OidEdge] = []
    for window in windows:
        turn_events = tuple(
            event
            for event in events
            if getattr(event, "thread_id", None) == window.thread_id
            and getattr(event, "turn_id", None) == window.turn_id
            and window.started < event.sequence < window.completed
            and isinstance(event, (TaskFileChangeCompleted, TaskTurnDiffUpdated))
        )
        pending_paths: set[str] | None = None
        touched_paths: set[str] = set()
        baseline_oids: dict[str, str] = {}
        latest_oids: dict[str, str] = {}
        final_snapshot: dict[str, _VersionEdge] | None = None
        for event in turn_events:
            if isinstance(event, TaskFileChangeCompleted):
                if pending_paths is not None:
                    raise _Unavailable("completed file change has no following Turn diff")
                if event.status != "completed":
                    raise _Unavailable("file change did not complete successfully")
                pending_paths = _normalized_reported_paths(event.paths, cwd)
                touched_paths.update(pending_paths)
                continue
            if pending_paths is None:
                raise _Unavailable("Turn diff has no preceding completed file change")
            if not event.diff:
                raise _Unavailable("empty Turn diff cannot prove tracker completeness")
            parsed = {
                edge.path: edge
                for edge in _parse_aggregate_diff(event.diff, budget=budget)
            }
            if set(parsed) != touched_paths:
                raise _Unavailable(
                    "file change paths disagree with an aggregate snapshot"
                )
            for path, edge in parsed.items():
                baseline = baseline_oids.setdefault(path, edge.old_oid)
                if edge.old_oid != baseline:
                    raise _Unavailable("Turn diff baseline changed between snapshots")
                previous = latest_oids.get(path)
                if path not in pending_paths:
                    if previous is None or edge.new_oid != previous:
                        raise _Unavailable("untouched path changed between Turn snapshots")
                    continue
                source = edge.old_oid if previous is None else previous
                if source == edge.new_oid:
                    raise _Unavailable("task checkpoint has no content change")
                checkpoints.append(_OidEdge(path, source, edge.new_oid))
                latest_oids[path] = edge.new_oid
            final_snapshot = parsed
            pending_paths = None
        if pending_paths is not None:
            raise _Unavailable("completed file change has no following Turn diff")
        if not touched_paths:
            if final_snapshot is not None:
                raise _Unavailable("Turn diff has no completed file change")
            continue
        if final_snapshot is None:
            raise _Unavailable("completed file change has no final Turn diff snapshot")
        selected.extend(final_snapshot.values())
    return tuple(selected), tuple(checkpoints)


def _unique_linear_chain(
    edges: Sequence[_Edge],
) -> tuple[_Edge, ...]:
    if not edges:
        raise _Unavailable("task path has no version edge")
    outgoing: dict[str, _Edge] = {}
    incoming: dict[str, _Edge] = {}
    for edge in edges:
        if edge.old_oid in outgoing or edge.new_oid in incoming:
            raise _Unavailable("task path has no unique linear version chain")
        outgoing[edge.old_oid] = edge
        incoming[edge.new_oid] = edge
    sources = set(outgoing) - set(incoming)
    sinks = set(incoming) - set(outgoing)
    if len(sources) != 1 or len(sinks) != 1:
        raise _Unavailable("task path has no unique linear version chain")
    source = next(iter(sources))
    sink = next(iter(sinks))
    chain: list[_Edge] = []
    visited = {source}
    current = source
    while current in outgoing:
        edge = outgoing[current]
        if edge.new_oid in visited:
            raise _Unavailable("task path has no unique linear version chain")
        chain.append(edge)
        current = edge.new_oid
        visited.add(current)
    if current != sink or len(chain) != len(edges):
        raise _Unavailable("task path has no unique linear version chain")
    return tuple(chain)


def _normalized_reported_paths(paths: Sequence[str], cwd: Path) -> set[str]:
    normalized: set[str] = set()
    for raw in paths:
        if not isinstance(raw, str) or not raw or "\0" in raw:
            raise _Unavailable("file change path is invalid")
        path = Path(raw)
        if path.is_absolute():
            try:
                path = path.relative_to(cwd)
            except ValueError as error:
                raise _Unavailable("file change belongs to another environment") from error
        value = _validated_relative_path(path.as_posix())
        if value in normalized:
            raise _Unavailable("file change repeats a path")
        normalized.add(value)
    if not normalized:
        raise _Unavailable("file change has no paths")
    return normalized


def _reject_noncausal_path_conflicts(
    events: Sequence[TaskDiffEvent],
    *,
    windows: Sequence[_TurnWindow],
    causal_paths: set[str],
    cwd: Path,
    budget: _CompositionBudget,
) -> None:
    included = {(window.thread_id, window.turn_id) for window in windows}
    for event in events:
        key = (
            getattr(event, "thread_id", None),
            getattr(event, "turn_id", None),
        )
        if key in included:
            continue
        paths: set[str]
        if isinstance(event, TaskFileChangeCompleted):
            paths = _normalized_reported_paths(event.paths, cwd)
        elif isinstance(event, TaskTurnDiffUpdated):
            if not event.diff:
                raise _Unavailable("non-causal Turn emitted an ambiguous empty diff")
            paths = {
                edge.path
                for edge in _parse_aggregate_diff(event.diff, budget=budget)
            }
        else:
            continue
        if paths & causal_paths:
            raise _Unavailable("a non-causal Turn touched a task path")


def _read_current_text(cwd: Path, display_path: str) -> tuple[str | None, int]:
    relative = _validated_relative_path(display_path)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if (
        not isinstance(nofollow, int)
        or not isinstance(directory, int)
        or os.open not in os.supports_dir_fd
    ):
        raise _Unavailable("safe task file traversal is unsupported")
    common_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | nofollow
    directory_fd: int | None = None
    descriptor: int | None = None
    try:
        try:
            directory_fd = os.open(cwd, common_flags | directory)
            parts = relative.split("/")
            for part in parts[:-1]:
                child_fd = os.open(
                    part,
                    common_flags | directory,
                    dir_fd=directory_fd,
                )
                os.close(directory_fd)
                directory_fd = child_fd
            descriptor = os.open(
                parts[-1],
                common_flags | getattr(os, "O_NONBLOCK", 0),
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            return None, 0
        except OSError as error:
            raise _Unavailable("current task file cannot be opened safely") from error
        assert descriptor is not None
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise _Unavailable("current task path is not a regular file")
        if metadata.st_size > _MAX_FILE_BYTES:
            raise _Unavailable("task file exceeds the read bound")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(_MAX_FILE_BYTES + 1)
        if len(data) > _MAX_FILE_BYTES:
            raise _Unavailable("task file exceeds the read bound")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory_fd is not None:
            os.close(directory_fd)
    return data.decode("utf-8"), len(data)


def _reverse_edge(
    content: str | None,
    edge: _VersionEdge,
    *,
    budget: _CompositionBudget,
) -> str | None:
    actual_new = _git_blob_oid(content) if content is not None else _ZERO_OID
    if actual_new != edge.new_oid:
        raise _Unavailable("task diff new blob verification failed")
    lines = [] if content is None else budget.split_text(content)
    for hunk in reversed(edge.hunks):
        start = _hunk_offset(hunk.new_start, hunk.new_count)
        stop = start + hunk.new_count
        if start < 0 or stop > len(lines) or tuple(lines[start:stop]) != hunk.new_lines:
            raise _Unavailable("task diff hunk cannot be reversed exactly")
        lines[start:stop] = hunk.old_lines
    previous = "".join(lines)
    budget.text_work += len(previous)
    if budget.text_work > _MAX_TEXT_WORK:
        raise _Unavailable("task text reconstruction exceeded its work bound")
    if edge.old_oid == _ZERO_OID:
        if previous:
            raise _Unavailable("new-file diff reverses to non-empty content")
        result: str | None = None
    else:
        result = previous
    if (_git_blob_oid(result) if result is not None else _ZERO_OID) != edge.old_oid:
        raise _Unavailable("task diff old blob verification failed")
    return result


def _parse_aggregate_diff(
    diff: str,
    *,
    budget: _CompositionBudget,
) -> tuple[_VersionEdge, ...]:
    if not isinstance(diff, str):
        raise _Unavailable("Turn diff payload is not text")
    if not diff:
        return ()
    budget.charge_diff(diff)
    lines = _text_lines(diff)
    starts = [index for index, line in enumerate(lines) if line.startswith("diff --git ")]
    if not starts or any(line.strip() for line in lines[: starts[0]]):
        raise _Unavailable("Turn diff framing is malformed")
    starts.append(len(lines))
    edges: list[_VersionEdge] = []
    seen_paths: set[str] = set()
    for offset in range(len(starts) - 1):
        block = lines[starts[offset] : starts[offset + 1]]
        edge = _parse_diff_block(block, budget=budget)
        if edge.path in seen_paths:
            raise _Unavailable("Turn diff repeats a current-side path")
        seen_paths.add(edge.path)
        edges.append(edge)
    return tuple(edges)


def _parse_diff_block(
    lines: Sequence[str],
    *,
    budget: _CompositionBudget,
) -> _VersionEdge:
    header = _git_header_paths(_without_newline(lines[0])[len("diff --git ") :])
    if header is None:
        raise _Unavailable("Turn diff path header is malformed")
    header_old = _normalize_diff_path(header[0])
    header_new = _normalize_diff_path(header[1])
    if header_old is None or header_new is None or header_old != header_new:
        raise _Unavailable("rename task diffs are unsupported")
    header_old = _validated_relative_path(header_old)
    header_new = _validated_relative_path(header_new)

    old_oid: str | None = None
    new_oid: str | None = None
    old_path: str | None = None
    new_path: str | None = None
    mode: str | None = None
    hunks: list[_Hunk] = []
    index = 1
    while index < len(lines):
        line = _without_newline(lines[index])
        if line.startswith("@@"):
            hunk, index = _parse_hunk(lines, index, budget=budget)
            hunks.append(hunk)
            continue
        match = _INDEX_LINE.fullmatch(line)
        if match is not None:
            if old_oid is not None:
                raise _Unavailable("Turn diff contains duplicate blob metadata")
            old_oid, new_oid = match.groups()
        elif line.startswith("--- "):
            if old_path is not None:
                raise _Unavailable("Turn diff contains duplicate old path")
            old_path = _metadata_path(line[4:])
        elif line.startswith("+++ "):
            if new_path is not None:
                raise _Unavailable("Turn diff contains duplicate new path")
            new_path = _metadata_path(line[4:])
        elif line in {"new file mode 100644", "deleted file mode 100644"}:
            if mode is not None:
                raise _Unavailable("Turn diff contains duplicate mode metadata")
            mode = line
        else:
            raise _Unavailable("Turn diff contains unsupported metadata")
        index += 1

    if old_oid is None or new_oid is None:
        raise _Unavailable("Turn diff is missing content metadata")
    expected_mode = (
        "new file mode 100644"
        if old_oid == _ZERO_OID
        else "deleted file mode 100644"
        if new_oid == _ZERO_OID
        else None
    )
    if mode != expected_mode:
        raise _Unavailable("Turn diff mode metadata is inconsistent")
    if old_path is None or new_path is None:
        if hunks or old_path is not None or new_path is not None or expected_mode is None:
            raise _Unavailable("Turn diff is missing path metadata")
    else:
        normalized_old = _normalize_diff_path(old_path)
        normalized_new = _normalize_diff_path(new_path)
        if normalized_old is not None:
            normalized_old = _validated_relative_path(normalized_old)
        if normalized_new is not None:
            normalized_new = _validated_relative_path(normalized_new)
        if normalized_old not in {None, header_old} or normalized_new not in {
            None,
            header_new,
        }:
            raise _Unavailable("Turn diff path metadata disagrees with its header")
        if (old_path == "/dev/null") != (old_oid == _ZERO_OID):
            raise _Unavailable("Turn diff old blob presence is inconsistent")
        if (new_path == "/dev/null") != (new_oid == _ZERO_OID):
            raise _Unavailable("Turn diff new blob presence is inconsistent")
    if old_oid == new_oid:
        raise _Unavailable("Turn diff contains a content-identical edge")
    if not hunks and old_oid != _ZERO_OID and new_oid != _ZERO_OID:
        raise _Unavailable("Turn diff contains no reversible hunks")
    _validate_hunk_order(hunks)
    budget.charge_edge()
    return _VersionEdge(header_new, old_oid, new_oid, tuple(hunks))


def _parse_hunk(
    lines: Sequence[str],
    start: int,
    *,
    budget: _CompositionBudget,
) -> tuple[_Hunk, int]:
    budget.charge_hunk()
    match = _HUNK_HEADER.fullmatch(_without_newline(lines[start]))
    if match is None:
        raise _Unavailable("Turn diff hunk header is malformed")
    old_start = int(match.group(1))
    old_count = int(match.group(2) or "1")
    new_start = int(match.group(3))
    new_count = int(match.group(4) or "1")
    if (old_start == 0 and old_count != 0) or (
        new_start == 0 and new_count != 0
    ):
        raise _Unavailable("Turn diff hunk uses an invalid zero range")
    old_lines: list[str] = []
    new_lines: list[str] = []
    prior_prefix: str | None = None
    index = start + 1
    while index < len(lines) and not lines[index].startswith("@@"):
        raw = lines[index]
        if raw.startswith("diff --git "):
            break
        if _without_newline(raw) == "\\ No newline at end of file":
            if prior_prefix is None:
                raise _Unavailable("Turn diff newline marker has no content line")
            if prior_prefix in {" ", "-"}:
                old_lines[-1] = _strip_terminal_newline(old_lines[-1])
            if prior_prefix in {" ", "+"}:
                new_lines[-1] = _strip_terminal_newline(new_lines[-1])
            prior_prefix = None
            index += 1
            continue
        if not raw or raw[0] not in {" ", "+", "-"}:
            break
        prefix = raw[0]
        value = raw[1:]
        if prefix in {" ", "-"}:
            old_lines.append(value)
        if prefix in {" ", "+"}:
            new_lines.append(value)
        prior_prefix = prefix
        index += 1
    if len(old_lines) != old_count or len(new_lines) != new_count:
        raise _Unavailable("Turn diff hunk length is inconsistent")
    return (
        _Hunk(
            old_start,
            old_count,
            new_start,
            new_count,
            tuple(old_lines),
            tuple(new_lines),
        ),
        index,
    )


def _hunk_offset(start: int, count: int) -> int:
    return start if count == 0 else start - 1


def _validate_hunk_order(hunks: Sequence[_Hunk]) -> None:
    previous_old_end = 0
    previous_new_end = 0
    for index, hunk in enumerate(hunks):
        old_offset = _hunk_offset(hunk.old_start, hunk.old_count)
        new_offset = _hunk_offset(hunk.new_start, hunk.new_count)
        if index and (
            old_offset < previous_old_end or new_offset < previous_new_end
        ):
            raise _Unavailable("Turn diff hunks overlap or are out of order")
        previous_old_end = old_offset + hunk.old_count
        previous_new_end = new_offset + hunk.new_count


def _minimal_line_counts(
    old: str,
    new: str,
    *,
    budget: _CompositionBudget,
) -> tuple[int, int]:
    left = budget.split_text(old)
    right = budget.split_text(new)
    if not left:
        return len(right), 0
    if not right:
        return 0, len(left)

    n = len(left)
    m = len(right)
    frontier: dict[int, int] = {1: 0}
    for distance in range(n + m + 1):
        for diagonal in range(-distance, distance + 1, 2):
            budget.charge_myers()
            if diagonal == -distance or (
                diagonal != distance
                and frontier.get(diagonal - 1, -1) < frontier.get(diagonal + 1, -1)
            ):
                x = frontier.get(diagonal + 1, 0)
            else:
                x = frontier.get(diagonal - 1, 0) + 1
            y = x - diagonal
            while x < n and y < m and left[x] == right[y]:
                budget.charge_myers()
                x += 1
                y += 1
            frontier[diagonal] = x
            if x >= n and y >= m:
                deletions = (distance + n - m) // 2
                additions = distance - deletions
                return additions, deletions
    raise _Unavailable("task line diff did not converge")


def _git_blob_oid(content: str | None) -> str:
    if content is None:
        return _ZERO_OID
    data = content.encode("utf-8")
    digest = hashlib.sha1(usedforsecurity=False)
    digest.update(f"blob {len(data)}\0".encode("ascii"))
    digest.update(data)
    return digest.hexdigest()


def _without_newline(value: str) -> str:
    if value.endswith("\r\n"):
        return value[:-2]
    if value.endswith(("\r", "\n")):
        return value[:-1]
    return value


def _strip_terminal_newline(value: str) -> str:
    stripped = _without_newline(value)
    if stripped == value:
        raise _Unavailable("Turn diff newline marker is duplicated")
    return stripped


def _text_lines(value: str) -> list[str]:
    """Split exactly like similar 2.7: CR, LF and CRLF are line endings."""

    result: list[str] = []
    start = 0
    index = 0
    while index < len(value):
        character = value[index]
        if character == "\r":
            index += 1
            if index < len(value) and value[index] == "\n":
                index += 1
            result.append(value[start:index])
            start = index
            continue
        if character == "\n":
            index += 1
            result.append(value[start:index])
            start = index
            continue
        index += 1
    if start < len(value):
        result.append(value[start:])
    return result


def _count_text_lines(value: str) -> int:
    count = 0
    start = 0
    index = 0
    while index < len(value):
        character = value[index]
        if character == "\r":
            index += 1
            if index < len(value) and value[index] == "\n":
                index += 1
            count += 1
            start = index
            continue
        if character == "\n":
            index += 1
            count += 1
            start = index
            continue
        index += 1
    return count + int(start < len(value))


def _validated_relative_path(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\0" in value
        or value.startswith("/")
        or len(value) > _MAX_PATH_CHARS
    ):
        raise _Unavailable("task diff path is invalid")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise _Unavailable("task diff path is not a canonical relative path")
    return value
