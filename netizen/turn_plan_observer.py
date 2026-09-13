"""Pinned, non-consuming observation of one active Turn's retained activity.

This is the complete private-SDK exception approved by ADR 0020 and extended
by ADR 0052.  It never registers, consumes, or mutates notifications and
exposes no generic queue or RPC access.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import openai_codex
from openai_codex import AsyncCodex
from openai_codex._message_router import MessageRouter, _TurnState
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


SUPPORTED_SDK_VERSION = "0.154.0"
_PACKAGE_SOURCE_FINGERPRINT = (
    "9db021b08bbcc75f18206d64ecf8a7d5ba63b380d91181718a3c9153ed4a053f"
)
_LOCK_TYPE = type(threading.RLock())


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
    retained_count: int = 0

    def __post_init__(self) -> None:
        if self.next_cursor < 0:
            raise ValueError("plan observation cursor must be non-negative")
        if (
            type(self.retained_count) is not int
            or not 0 <= self.retained_count <= self.next_cursor
        ):
            raise ValueError("retained notification count must fall within the cursor")
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

        items, next_cursor, retained_count = self._snapshot_events(
            thread_id, turn_id, after_cursor
        )
        latest_steps: tuple[TurnPlanStepSnapshot, ...] = ()
        latest_cursor: int | None = None
        events: list[TurnActivityEvent] = []
        turn_completed = False
        for item_cursor, item in enumerate(items, start=after_cursor + 1):
            if isinstance(item, BaseException):
                raise TurnActivityObservationUnavailable(
                    "native Turn event store contains a transport failure"
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
            retained_count=retained_count,
        )

    def _snapshot_events(
        self,
        thread_id: str,
        turn_id: str,
        after_cursor: int,
    ) -> tuple[tuple[object, ...], int, int]:
        router = self._router
        lock = getattr(router, "_lock", None)
        states = getattr(router, "_turn_states", None)
        if type(lock) is not _LOCK_TYPE or type(states) is not dict:
            raise TurnActivityObservationUnavailable(
                "native notification router shape changed"
            )
        with lock:
            state = states.get(turn_id)
            if type(state) is not _TurnState:
                raise TurnActivityObservationUnavailable(
                    "exact native Turn event store is unavailable"
                )
            if (
                getattr(state, "id", None) != turn_id
                or getattr(state, "thread_id", None) != thread_id
            ):
                raise TurnActivityObservationUnavailable(
                    "native Turn event store identity changed"
                )
            raw_items = getattr(state, "events", None)
            first_cursor = getattr(state, "first_event", None)
            next_cursor = getattr(state, "next_event", None)
            subscribers = getattr(state, "subscribers", None)
            if (
                type(raw_items) is not dict
                or type(first_cursor) is not int
                or type(next_cursor) is not int
                or not 0 <= first_cursor <= next_cursor
                or type(getattr(state, "completed", None)) is not bool
                or type(subscribers) is not dict
                or not subscribers
                or any(
                    type(cursor) is not int
                    or not first_cursor <= cursor <= next_cursor
                    for cursor in subscribers.values()
                )
            ):
                raise TurnActivityObservationUnavailable(
                    "native Turn event store shape changed"
                )
            retained_count = len(raw_items)
            if retained_count != next_cursor - first_cursor or any(
                type(cursor) is not int or not first_cursor <= cursor < next_cursor
                for cursor in raw_items
            ):
                raise TurnActivityObservationUnavailable(
                    "native Turn event store has a cursor gap"
                )
            if after_cursor < first_cursor:
                raise TurnActivityObservationUnavailable(
                    "native Turn events were pruned before the observation cursor"
                )
            if after_cursor > next_cursor:
                raise TurnActivityObservationUnavailable(
                    "native Turn event cursor moved backwards"
                )
            # Cursor positions are absolute event indices. Copy only references;
            # the SDK subscriptions retain exclusive consumption/pruning rights.
            items = tuple(raw_items[cursor] for cursor in range(after_cursor, next_cursor))
        return items, next_cursor, retained_count


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
    if type(getattr(router, "_turn_states", None)) is not dict:
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
