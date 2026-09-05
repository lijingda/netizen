"""Pinned pre-router observation for causal root-Turn file statistics.

This is the complete private-SDK surface approved for task diff composition.
It copies a small semantic allowlist before the SDK routes notifications and
never registers, consumes, or mutates an SDK notification queue.
"""

from __future__ import annotations

import importlib.metadata
import threading
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from types import MethodType
from typing import Any, Protocol

import openai_codex
from openai_codex import AsyncCodex
from openai_codex._message_router import MessageRouter
from openai_codex.async_client import AsyncCodexClient
from openai_codex.client import CodexClient
from openai_codex.generated import v2_all as _generated
from openai_codex.models import Notification

from .task_diff import (
    TaskCaptureInvalid,
    TaskCollabToolCall,
    TaskDiffComposition,
    TaskDiffEvent,
    TaskFileChangeCompleted,
    TaskThreadStarted,
    TaskTurnCompleted,
    TaskTurnDiffUpdated,
    TaskTurnStarted,
    compose_task_diff,
)
from .terminal_cleanup import _source_tree_fingerprint


SUPPORTED_SDK_VERSION = "0.147.0"
SUPPORTED_RUNTIME_VERSION = "0.147.0"
_PACKAGE_SOURCE_FINGERPRINT = (
    "35ec9419cb9f42577080f9bf410e81cb5a97ae64e5297c4302878c73749d39eb"
)
_RUNTIME_DISTRIBUTION = "openai-codex-cli-bin"
_DEFAULT_MAX_EVENTS = 4_096
_DEFAULT_MAX_BYTES = 32 * 1024 * 1024
_MAX_PROJECTED_FILE_CHANGES = 256
_MAX_PROJECTED_RECEIVERS = 256
_MAX_PROJECTED_PATH_CHARS = 4_096
_MAX_NATIVE_ID_CHARS = 1_024
_ROUTER_MARKER = "_netizen_task_diff_observer"


class TaskDiffObservationUnavailable(RuntimeError):
    """The pinned SDK/runtime contract cannot support task diff observation."""


@dataclass(frozen=True, slots=True)
class TaskDiffCapture:
    cursor: int
    available: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.cursor, bool) or not isinstance(self.cursor, int):
            raise ValueError("task diff cursor must be an integer")
        if self.cursor < 0:
            raise ValueError("task diff cursor must be non-negative")


class TaskDiffObserver(Protocol):
    def begin(self) -> TaskDiffCapture: ...

    def finish(
        self,
        capture: TaskDiffCapture,
        *,
        root_thread_id: str,
        root_turn_id: str,
        cwd: Path,
        include_prior_root_turns: bool = False,
    ) -> TaskDiffComposition: ...


class UnavailableTaskDiffObserver:
    """Production fallback that suppresses unverifiable physical-Turn counts."""

    __slots__ = ()

    def begin(self) -> TaskDiffCapture:
        return TaskDiffCapture(0, available=False)

    def finish(
        self,
        capture: TaskDiffCapture,
        *,
        root_thread_id: str,
        root_turn_id: str,
        cwd: Path,
        include_prior_root_turns: bool = False,
    ) -> TaskDiffComposition:
        del capture, root_thread_id, root_turn_id, cwd, include_prior_root_turns
        return TaskDiffComposition.unavailable("task diff observer is unavailable")


@dataclass(frozen=True, slots=True)
class _Record:
    event: TaskDiffEvent
    size: int


class PinnedTaskDiffObserver:
    """Copy exact 0.147 notifications before ``MessageRouter`` can drop them."""

    __slots__ = (
        "_lock",
        "_records",
        "_next_cursor",
        "_retained_bytes",
        "_max_events",
        "_max_bytes",
        "_original_route",
        "_original_fail_all",
    )

    def __init__(
        self,
        codex: AsyncCodex,
        *,
        max_events: int = _DEFAULT_MAX_EVENTS,
        max_bytes: int = _DEFAULT_MAX_BYTES,
    ) -> None:
        if (
            isinstance(max_events, bool)
            or not isinstance(max_events, int)
            or max_events < 1
        ):
            raise ValueError("task diff event bound must be positive")
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes < 1
        ):
            raise ValueError("task diff byte bound must be positive")
        router = _validate_contract(codex)
        self._lock = threading.Lock()
        self._records: deque[_Record] = deque()
        self._next_cursor = 0
        self._retained_bytes = 0
        self._max_events = max_events
        self._max_bytes = max_bytes
        self._original_route = router.route_notification
        self._original_fail_all = router.fail_all

        def route(notification: Notification) -> None:
            try:
                self._observe_notification(notification)
            except BaseException:
                try:
                    self._append_invalid("task notification projection failed")
                except BaseException:
                    pass
            self._original_route(notification)

        def fail_all(error: BaseException) -> None:
            try:
                self._append_invalid("Codex notification transport failed")
            except BaseException:
                pass
            self._original_fail_all(error)

        router.route_notification = route  # type: ignore[method-assign]
        router.fail_all = fail_all  # type: ignore[method-assign]
        setattr(router, _ROUTER_MARKER, self)

    def begin(self) -> TaskDiffCapture:
        with self._lock:
            return TaskDiffCapture(self._next_cursor)

    def finish(
        self,
        capture: TaskDiffCapture,
        *,
        root_thread_id: str,
        root_turn_id: str,
        cwd: Path,
        include_prior_root_turns: bool = False,
    ) -> TaskDiffComposition:
        if not capture.available:
            return TaskDiffComposition.unavailable("task capture did not start")
        _validate_id(root_thread_id, label="Thread")
        _validate_id(root_turn_id, label="Turn")
        with self._lock:
            oldest = (
                self._records[0].event.sequence
                if self._records
                else self._next_cursor
            )
            if capture.cursor < oldest or capture.cursor > self._next_cursor:
                return TaskDiffComposition.unavailable(
                    "task notification buffer did not retain the complete capture"
                )
            events = tuple(
                record.event
                for record in self._records
                if record.event.sequence >= capture.cursor
            )
        return compose_task_diff(
            events,
            root_thread_id=root_thread_id,
            root_turn_id=root_turn_id,
            cwd=cwd,
            include_prior_root_turns=include_prior_root_turns,
        )

    def _observe_notification(self, notification: Notification) -> None:
        if type(notification) is not Notification:
            self._append_invalid("Codex notification envelope changed")
            return
        projected = _project_notification(notification)
        if projected is None:
            return
        self._append(projected, _event_size(projected))

    def _append_invalid(self, reason: str) -> None:
        self._append(TaskCaptureInvalid(0, reason), 128)

    def _append(self, event: TaskDiffEvent, size: int) -> None:
        with self._lock:
            event = replace(event, sequence=self._next_cursor)
            self._next_cursor += 1
            record = _Record(event, max(1, size))
            self._records.append(record)
            self._retained_bytes += record.size
            while (
                len(self._records) > self._max_events
                or self._retained_bytes > self._max_bytes
            ):
                removed = self._records.popleft()
                self._retained_bytes -= removed.size


def _project_notification(notification: Notification) -> TaskDiffEvent | None:
    payload = notification.payload
    method = notification.method
    if method == "thread/started":
        if type(payload) is not _generated.ThreadStartedNotification:
            return TaskCaptureInvalid(0, "thread/started payload changed")
        thread = payload.thread
        return TaskThreadStarted(
            0,
            _validated_id(getattr(thread, "id", None), label="Thread"),
            _optional_id(getattr(thread, "parent_thread_id", None), label="parent Thread"),
        )
    if method == "turn/started":
        if type(payload) is not _generated.TurnStartedNotification:
            return TaskCaptureInvalid(0, "turn/started payload changed")
        return TaskTurnStarted(
            0,
            _validated_id(payload.thread_id, label="Thread"),
            _validated_id(getattr(payload.turn, "id", None), label="Turn"),
        )
    if method == "turn/completed":
        if type(payload) is not _generated.TurnCompletedNotification:
            return TaskCaptureInvalid(0, "turn/completed payload changed")
        status = getattr(getattr(payload.turn, "status", None), "value", None)
        if status not in {"completed", "interrupted", "failed"}:
            return TaskCaptureInvalid(0, "turn/completed status changed")
        return TaskTurnCompleted(
            0,
            _validated_id(payload.thread_id, label="Thread"),
            _validated_id(getattr(payload.turn, "id", None), label="Turn"),
            status,
        )
    if method == "turn/diff/updated":
        if type(payload) is not _generated.TurnDiffUpdatedNotification:
            return TaskCaptureInvalid(0, "turn/diff/updated payload changed")
        if not isinstance(payload.diff, str):
            return TaskCaptureInvalid(0, "turn/diff/updated diff changed")
        return TaskTurnDiffUpdated(
            0,
            _validated_id(payload.thread_id, label="Thread"),
            _validated_id(payload.turn_id, label="Turn"),
            payload.diff,
        )
    if method not in {"item/started", "item/completed"}:
        return None
    payload_type = (
        _generated.ItemStartedNotification
        if method == "item/started"
        else _generated.ItemCompletedNotification
    )
    if type(payload) is not payload_type:
        return TaskCaptureInvalid(0, f"{method} payload changed")
    root = getattr(payload.item, "root", None)
    if type(root) is _generated.FileChangeThreadItem:
        if method != "item/completed":
            return None
        status = getattr(getattr(root, "status", None), "value", None)
        if status not in {"completed", "failed", "declined"}:
            return TaskCaptureInvalid(0, "file change status changed")
        return TaskFileChangeCompleted(
            0,
            _validated_id(payload.thread_id, label="Thread"),
            _validated_id(payload.turn_id, label="Turn"),
            _validated_id(root.id, label="file change item"),
            status,
            _file_change_paths(root),
        )
    if type(root) is not _generated.CollabAgentToolCallThreadItem:
        return None
    tool = getattr(getattr(root, "tool", None), "value", None)
    status = getattr(getattr(root, "status", None), "value", None)
    if tool not in {"spawnAgent", "sendInput", "resumeAgent", "wait", "closeAgent"}:
        return TaskCaptureInvalid(0, "collab tool changed")
    if status not in {"inProgress", "completed", "failed"}:
        return TaskCaptureInvalid(0, "collab status changed")
    raw_receivers = root.receiver_thread_ids
    if type(raw_receivers) is not list or len(raw_receivers) > _MAX_PROJECTED_RECEIVERS:
        return TaskCaptureInvalid(0, "collab receiver list changed")
    receivers = tuple(
        _validated_id(value, label="receiver Thread") for value in raw_receivers
    )
    return TaskCollabToolCall(
        0,
        _validated_id(payload.thread_id, label="Thread"),
        _validated_id(payload.turn_id, label="Turn"),
        _validated_id(root.id, label="collab item"),
        "started" if method == "item/started" else "completed",
        tool,
        status,
        _validated_id(root.sender_thread_id, label="sender Thread"),
        receivers,
    )


def _event_size(event: TaskDiffEvent) -> int:
    if isinstance(event, TaskTurnDiffUpdated):
        return 256 + len(event.diff) * 4
    if isinstance(event, TaskFileChangeCompleted):
        return 256 + sum(len(path) * 4 for path in event.paths)
    return 256


def _file_change_paths(root: _generated.FileChangeThreadItem) -> tuple[str, ...]:
    changes = root.changes
    if type(changes) is not list or not changes:
        raise TaskDiffObservationUnavailable("file change list changed")
    if len(changes) > _MAX_PROJECTED_FILE_CHANGES:
        raise TaskDiffObservationUnavailable("file change list is too large")
    paths: list[str] = []
    for change in changes:
        if type(change) is not _generated.FileUpdateChange:
            raise TaskDiffObservationUnavailable("file change entry type changed")
        path = getattr(change, "path", None)
        if (
            not isinstance(path, str)
            or not path
            or len(path) > _MAX_PROJECTED_PATH_CHARS
        ):
            raise TaskDiffObservationUnavailable("file change path changed")
        paths.append(path)
        kind = getattr(change, "kind", None)
        if type(kind) is not _generated.PatchChangeKind:
            raise TaskDiffObservationUnavailable("file change kind changed")
        kind_root = getattr(kind, "root", None)
        if type(kind_root) not in {
            _generated.AddPatchChangeKind,
            _generated.DeletePatchChangeKind,
            _generated.UpdatePatchChangeKind,
        }:
            raise TaskDiffObservationUnavailable("patch change kind changed")
        if type(kind_root) is _generated.UpdatePatchChangeKind:
            move_path = kind_root.move_path
            if move_path is not None:
                if (
                    not isinstance(move_path, str)
                    or not move_path
                    or len(move_path) > _MAX_PROJECTED_PATH_CHARS
                ):
                    raise TaskDiffObservationUnavailable("file move path changed")
                paths.append(move_path)
    return tuple(paths)


def _validate_contract(codex: AsyncCodex) -> MessageRouter:
    if openai_codex.__version__ != SUPPORTED_SDK_VERSION:
        raise TaskDiffObservationUnavailable(
            "task diff observation supports only openai-codex=="
            f"{SUPPORTED_SDK_VERSION}; found {openai_codex.__version__}"
        )
    try:
        runtime_version = importlib.metadata.version(_RUNTIME_DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError as error:
        raise TaskDiffObservationUnavailable("Codex runtime distribution is missing") from error
    if runtime_version != SUPPORTED_RUNTIME_VERSION:
        raise TaskDiffObservationUnavailable(
            "task diff observation supports only openai-codex-cli-bin=="
            f"{SUPPORTED_RUNTIME_VERSION}; found {runtime_version}"
        )
    if type(codex) is not AsyncCodex:
        raise TaskDiffObservationUnavailable("AsyncCodex implementation type changed")
    if getattr(codex, "_initialized", False) is not True:
        raise TaskDiffObservationUnavailable(
            "AsyncCodex must be initialized before task diff observation is enabled"
        )
    client = getattr(codex, "_client", None)
    if type(client) is not AsyncCodexClient:
        raise TaskDiffObservationUnavailable("AsyncCodex private client shape changed")
    sync_client = getattr(client, "_sync", None)
    if type(sync_client) is not CodexClient:
        raise TaskDiffObservationUnavailable("AsyncCodexClient private sync client changed")
    router = getattr(sync_client, "_router", None)
    if type(router) is not MessageRouter:
        raise TaskDiffObservationUnavailable("Codex notification router shape changed")
    if hasattr(router, _ROUTER_MARKER):
        raise TaskDiffObservationUnavailable("task diff observer is already installed")
    _require_original_method(
        router.route_notification,
        MessageRouter.route_notification,
        label="route_notification",
    )
    _require_original_method(router.fail_all, MessageRouter.fail_all, label="fail_all")
    package_file = getattr(openai_codex, "__file__", None)
    if not isinstance(package_file, str) or not package_file.endswith(".py"):
        raise TaskDiffObservationUnavailable("cannot locate SDK source package")
    try:
        actual = _source_tree_fingerprint(Path(package_file).resolve().parent)
    except OSError as error:
        raise TaskDiffObservationUnavailable("cannot read SDK source package") from error
    if actual != _PACKAGE_SOURCE_FINGERPRINT:
        raise TaskDiffObservationUnavailable("SDK package source fingerprint changed")
    return router


def _require_original_method(value: Any, expected: Any, *, label: str) -> None:
    if (
        not isinstance(value, MethodType)
        or value.__self__.__class__ is not MessageRouter
        or value.__func__ is not expected
    ):
        raise TaskDiffObservationUnavailable(f"Codex router {label} changed")


def _validated_id(value: Any, *, label: str) -> str:
    _validate_id(value, label=label)
    return value


def _optional_id(value: Any, *, label: str) -> str | None:
    if value is None:
        return None
    return _validated_id(value, label=label)


def _validate_id(value: Any, *, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_NATIVE_ID_CHARS
        or value.strip() != value
    ):
        raise TaskDiffObservationUnavailable(
            f"native {label} ID must be a non-empty trimmed string"
        )
