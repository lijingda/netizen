"""Pinned, non-consuming observation of one active Turn's activity queue.

This is the complete private-SDK exception approved by ADR 0020 and extended
by ADR 0052.  It never registers, consumes, or mutates notifications and
exposes no generic queue or RPC access.
"""

from __future__ import annotations

import itertools
import queue
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import openai_codex
from openai_codex import AsyncCodex
from openai_codex._message_router import MessageRouter
from openai_codex.async_client import AsyncCodexClient
from openai_codex.client import CodexClient
from openai_codex.generated.v2_all import (
    ItemCompletedNotification,
    ItemStartedNotification,
    TurnCompletedNotification,
    TurnPlanStep,
    TurnPlanStepStatus,
    TurnPlanUpdatedNotification,
    TurnStartedNotification,
)
from openai_codex.models import Notification

from .terminal_cleanup import _source_tree_fingerprint
from .turn_activity import (
    TurnActivityEvent,
    TurnActivityProjectionUnavailable,
    TurnPlanStepSnapshot,
    TurnPlanStepState,
    project_turn_activity_notification,
)


SUPPORTED_SDK_VERSION = "0.147.0"
_PACKAGE_SOURCE_FINGERPRINT = (
    "35ec9419cb9f42577080f9bf410e81cb5a97ae64e5297c4302878c73749d39eb"
)
_LOCK_TYPE = type(threading.Lock())


class TurnActivityObservationUnavailable(RuntimeError):
    """The pinned read-only observation contract cannot be satisfied."""


@dataclass(frozen=True, slots=True)
class TurnActivityObservation:
    next_cursor: int
    plan_updated: bool
    plan_cursor: int | None = None
    steps: tuple[TurnPlanStepSnapshot, ...] = ()
    events: tuple[TurnActivityEvent, ...] = ()
    turn_completed: bool = False

    def __post_init__(self) -> None:
        if self.next_cursor < 0:
            raise ValueError("plan observation cursor must be non-negative")
        if self.plan_updated != (self.plan_cursor is not None):
            raise ValueError("plan cursor must identify exactly one plan update")
        if self.plan_cursor is not None and not 0 < self.plan_cursor <= self.next_cursor:
            raise ValueError("plan update cursor must fall within the observation")


class TurnActivityObserver(Protocol):
    def observe(
        self,
        *,
        thread_id: str,
        turn_id: str,
        after_cursor: int,
    ) -> TurnActivityObservation: ...


class PinnedTurnActivityObserver:
    """Peek at already-routed safe activity without consuming notifications."""

    __slots__ = ("_router",)

    def __init__(self, codex: AsyncCodex) -> None:
        self._router = _validate_contract(codex)

    def observe(
        self,
        *,
        thread_id: str,
        turn_id: str,
        after_cursor: int,
    ) -> TurnActivityObservation:
        _validate_exact_id(thread_id, label="Thread")
        _validate_exact_id(turn_id, label="Turn")
        if isinstance(after_cursor, bool) or not isinstance(after_cursor, int):
            raise ValueError("activity cursor must be an integer")
        if after_cursor < 0:
            raise ValueError("activity cursor must be non-negative")

        items, next_cursor = self._snapshot_queue(turn_id, after_cursor)
        latest_steps: tuple[TurnPlanStepSnapshot, ...] = ()
        latest_cursor: int | None = None
        events: list[TurnActivityEvent] = []
        turn_completed = False
        for item_cursor, item in enumerate(items, start=after_cursor + 1):
            if isinstance(item, BaseException):
                raise TurnActivityObservationUnavailable(
                    "native Turn notification queue contains a transport failure"
                )
            if type(item) is not Notification:
                raise TurnActivityObservationUnavailable(
                    "native Turn notification item shape changed"
                )
            try:
                projection = project_turn_activity_notification(
                    item,
                    expected_thread_id=thread_id,
                    expected_turn_id=turn_id,
                )
            except TurnActivityProjectionUnavailable as error:
                raise TurnActivityObservationUnavailable(str(error)) from error
            if projection.plan_updated:
                latest_steps = projection.steps
                latest_cursor = item_cursor
            if projection.event is not None:
                events.append(projection.event)
            turn_completed = turn_completed or projection.turn_completed

        return TurnActivityObservation(
            next_cursor=next_cursor,
            plan_updated=latest_cursor is not None,
            plan_cursor=latest_cursor,
            steps=latest_steps,
            events=tuple(events),
            turn_completed=turn_completed,
        )

    def _snapshot_queue(
        self,
        turn_id: str,
        after_cursor: int,
    ) -> tuple[tuple[object, ...], int]:
        router = self._router
        lock = getattr(router, "_lock", None)
        notifications = getattr(router, "_turn_notifications", None)
        if type(lock) is not _LOCK_TYPE or type(notifications) is not dict:
            raise TurnActivityObservationUnavailable(
                "native notification router shape changed"
            )
        with lock:
            turn_queue = notifications.get(turn_id)
            if type(turn_queue) is not queue.Queue:
                raise TurnActivityObservationUnavailable(
                    "exact native Turn notification queue is unavailable"
                )
            mutex = getattr(turn_queue, "mutex", None)
            raw_items = getattr(turn_queue, "queue", None)
            if type(mutex) is not _LOCK_TYPE or type(raw_items) is not deque:
                raise TurnActivityObservationUnavailable(
                    "native Turn notification queue shape changed"
                )
            with mutex:
                next_cursor = len(raw_items)
                if after_cursor > next_cursor:
                    raise TurnActivityObservationUnavailable(
                        "native Turn notification queue was consumed unexpectedly"
                    )
                items = tuple(
                    itertools.islice(raw_items, after_cursor, next_cursor)
                )
        return items, next_cursor


def _validate_contract(codex: AsyncCodex) -> MessageRouter:
    if openai_codex.__version__ != SUPPORTED_SDK_VERSION:
        raise TurnActivityObservationUnavailable(
            "Turn activity observation supports only openai-codex=="
            f"{SUPPORTED_SDK_VERSION}; found {openai_codex.__version__}"
        )
    if type(codex) is not AsyncCodex:
        raise TurnActivityObservationUnavailable("AsyncCodex implementation type changed")
    if getattr(codex, "_initialized", False) is not True:
        raise TurnActivityObservationUnavailable(
            "AsyncCodex must be initialized before Turn activity observation is enabled"
        )
    client = getattr(codex, "_client", None)
    if type(client) is not AsyncCodexClient:
        raise TurnActivityObservationUnavailable("AsyncCodex private client shape changed")
    sync_client = getattr(client, "_sync", None)
    if type(sync_client) is not CodexClient:
        raise TurnActivityObservationUnavailable(
            "AsyncCodexClient private sync client shape changed"
        )
    router = getattr(sync_client, "_router", None)
    if type(router) is not MessageRouter:
        raise TurnActivityObservationUnavailable("Codex notification router shape changed")
    if type(getattr(router, "_lock", None)) is not _LOCK_TYPE:
        raise TurnActivityObservationUnavailable("Codex notification router lock changed")
    if type(getattr(router, "_turn_notifications", None)) is not dict:
        raise TurnActivityObservationUnavailable("Codex Turn route catalog shape changed")

    _validate_generated_models()
    package_file = getattr(openai_codex, "__file__", None)
    if not isinstance(package_file, str) or not package_file.endswith(".py"):
        raise TurnActivityObservationUnavailable("cannot locate SDK source package")
    try:
        actual = _source_tree_fingerprint(Path(package_file).resolve().parent)
    except OSError as error:
        raise TurnActivityObservationUnavailable(
            "cannot read SDK source package"
        ) from error
    if actual != _PACKAGE_SOURCE_FINGERPRINT:
        raise TurnActivityObservationUnavailable(
            "SDK package source fingerprint changed"
        )
    return router


def _validate_generated_models() -> None:
    payload_fields = getattr(TurnPlanUpdatedNotification, "model_fields", {})
    if set(payload_fields) != {"explanation", "plan", "thread_id", "turn_id"}:
        raise TurnActivityObservationUnavailable("Turn plan payload fields changed")
    if {
        name: getattr(payload_fields.get(name), "alias", None)
        for name in ("explanation", "thread_id", "turn_id", "plan")
    } != {
        "explanation": None,
        "thread_id": "threadId",
        "turn_id": "turnId",
        "plan": None,
    }:
        raise TurnActivityObservationUnavailable("Turn plan payload fields changed")
    if getattr(payload_fields["explanation"], "default", object()) is not None:
        raise TurnActivityObservationUnavailable("Turn plan explanation default changed")
    step_fields = getattr(TurnPlanStep, "model_fields", {})
    if set(step_fields) != {"step", "status"}:
        raise TurnActivityObservationUnavailable("Turn plan step fields changed")
    if {member.value for member in TurnPlanStepStatus} != {
        "pending",
        "inProgress",
        "completed",
    }:
        raise TurnActivityObservationUnavailable("Turn plan step statuses changed")

    expected_notification_fields = {
        ItemStartedNotification: {"item", "started_at_ms", "thread_id", "turn_id"},
        ItemCompletedNotification: {
            "completed_at_ms",
            "item",
            "thread_id",
            "turn_id",
        },
        TurnStartedNotification: {"thread_id", "turn"},
        TurnCompletedNotification: {"thread_id", "turn"},
    }
    for model, expected_fields in expected_notification_fields.items():
        if set(getattr(model, "model_fields", {})) != expected_fields:
            raise TurnActivityObservationUnavailable(
                f"{model.__name__} fields changed"
            )


def _validate_exact_id(value: str, *, label: str) -> None:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"native {label} ID must be a non-empty trimmed string")
