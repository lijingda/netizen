"""Pinned, non-consuming observation of one active Turn's native plan queue.

This is the complete private-SDK exception approved by ADR 0020.  It never
registers, consumes, or mutates notifications and exposes no generic queue or
RPC access.
"""

from __future__ import annotations

import itertools
import queue
import threading
from collections import deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

import openai_codex
from openai_codex import AsyncCodex
from openai_codex._message_router import MessageRouter
from openai_codex.async_client import AsyncCodexClient
from openai_codex.client import CodexClient
from openai_codex.generated.v2_all import (
    TurnPlanStep,
    TurnPlanStepStatus,
    TurnPlanUpdatedNotification,
)
from openai_codex.models import Notification

from .terminal_cleanup import _source_tree_fingerprint


SUPPORTED_SDK_VERSION = "0.147.0"
_PACKAGE_SOURCE_FINGERPRINT = (
    "35ec9419cb9f42577080f9bf410e81cb5a97ae64e5297c4302878c73749d39eb"
)
_PLAN_METHOD = "turn/plan/updated"
_LOCK_TYPE = type(threading.Lock())


class TurnPlanObservationUnavailable(RuntimeError):
    """The pinned read-only observation contract cannot be satisfied."""


class TurnPlanStepState(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "inProgress"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class TurnPlanStepSnapshot:
    step: str
    status: TurnPlanStepState


@dataclass(frozen=True, slots=True)
class TurnPlanObservation:
    next_cursor: int
    plan_updated: bool
    plan_cursor: int | None = None
    steps: tuple[TurnPlanStepSnapshot, ...] = ()

    def __post_init__(self) -> None:
        if self.next_cursor < 0:
            raise ValueError("plan observation cursor must be non-negative")
        if self.plan_updated != (self.plan_cursor is not None):
            raise ValueError("plan cursor must identify exactly one plan update")
        if self.plan_cursor is not None and not 0 < self.plan_cursor <= self.next_cursor:
            raise ValueError("plan update cursor must fall within the observation")


class TurnPlanObserver(Protocol):
    def observe(
        self,
        *,
        thread_id: str,
        turn_id: str,
        after_cursor: int,
    ) -> TurnPlanObservation: ...


class PinnedTurnPlanObserver:
    """Peek at already-routed plan notifications without consuming them."""

    __slots__ = ("_router",)

    def __init__(self, codex: AsyncCodex) -> None:
        self._router = _validate_contract(codex)

    def observe(
        self,
        *,
        thread_id: str,
        turn_id: str,
        after_cursor: int,
    ) -> TurnPlanObservation:
        _validate_exact_id(thread_id, label="Thread")
        _validate_exact_id(turn_id, label="Turn")
        if isinstance(after_cursor, bool) or not isinstance(after_cursor, int):
            raise ValueError("plan cursor must be an integer")
        if after_cursor < 0:
            raise ValueError("plan cursor must be non-negative")

        items, next_cursor = self._snapshot_queue(turn_id, after_cursor)
        latest_steps: tuple[TurnPlanStepSnapshot, ...] = ()
        latest_cursor: int | None = None
        for item_cursor, item in enumerate(items, start=after_cursor + 1):
            if isinstance(item, BaseException):
                raise TurnPlanObservationUnavailable(
                    "native Turn notification queue contains a transport failure"
                )
            if type(item) is not Notification:
                raise TurnPlanObservationUnavailable(
                    "native Turn notification item shape changed"
                )
            if item.method != _PLAN_METHOD:
                continue
            payload = item.payload
            if type(payload) is not TurnPlanUpdatedNotification:
                raise TurnPlanObservationUnavailable(
                    "native Turn plan payload shape changed"
                )
            if payload.thread_id != thread_id or payload.turn_id != turn_id:
                # A mismatched Turn can never update this exact active Turn.
                continue
            latest_steps = _plan_steps(payload.plan)
            latest_cursor = item_cursor

        return TurnPlanObservation(
            next_cursor=next_cursor,
            plan_updated=latest_cursor is not None,
            plan_cursor=latest_cursor,
            steps=latest_steps,
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
            raise TurnPlanObservationUnavailable(
                "native notification router shape changed"
            )
        with lock:
            turn_queue = notifications.get(turn_id)
            if type(turn_queue) is not queue.Queue:
                raise TurnPlanObservationUnavailable(
                    "exact native Turn notification queue is unavailable"
                )
            mutex = getattr(turn_queue, "mutex", None)
            raw_items = getattr(turn_queue, "queue", None)
            if type(mutex) is not _LOCK_TYPE or type(raw_items) is not deque:
                raise TurnPlanObservationUnavailable(
                    "native Turn notification queue shape changed"
                )
            with mutex:
                next_cursor = len(raw_items)
                if after_cursor > next_cursor:
                    raise TurnPlanObservationUnavailable(
                        "native Turn notification queue was consumed unexpectedly"
                    )
                items = tuple(
                    itertools.islice(raw_items, after_cursor, next_cursor)
                )
        return items, next_cursor


def _validate_contract(codex: AsyncCodex) -> MessageRouter:
    if openai_codex.__version__ != SUPPORTED_SDK_VERSION:
        raise TurnPlanObservationUnavailable(
            "Turn plan observation supports only openai-codex=="
            f"{SUPPORTED_SDK_VERSION}; found {openai_codex.__version__}"
        )
    if type(codex) is not AsyncCodex:
        raise TurnPlanObservationUnavailable("AsyncCodex implementation type changed")
    if getattr(codex, "_initialized", False) is not True:
        raise TurnPlanObservationUnavailable(
            "AsyncCodex must be initialized before Turn plan observation is enabled"
        )
    client = getattr(codex, "_client", None)
    if type(client) is not AsyncCodexClient:
        raise TurnPlanObservationUnavailable("AsyncCodex private client shape changed")
    sync_client = getattr(client, "_sync", None)
    if type(sync_client) is not CodexClient:
        raise TurnPlanObservationUnavailable(
            "AsyncCodexClient private sync client shape changed"
        )
    router = getattr(sync_client, "_router", None)
    if type(router) is not MessageRouter:
        raise TurnPlanObservationUnavailable("Codex notification router shape changed")
    if type(getattr(router, "_lock", None)) is not _LOCK_TYPE:
        raise TurnPlanObservationUnavailable("Codex notification router lock changed")
    if type(getattr(router, "_turn_notifications", None)) is not dict:
        raise TurnPlanObservationUnavailable("Codex Turn route catalog shape changed")

    _validate_generated_models()
    package_file = getattr(openai_codex, "__file__", None)
    if not isinstance(package_file, str) or not package_file.endswith(".py"):
        raise TurnPlanObservationUnavailable("cannot locate SDK source package")
    try:
        actual = _source_tree_fingerprint(Path(package_file).resolve().parent)
    except OSError as error:
        raise TurnPlanObservationUnavailable(
            "cannot read SDK source package"
        ) from error
    if actual != _PACKAGE_SOURCE_FINGERPRINT:
        raise TurnPlanObservationUnavailable(
            "SDK package source fingerprint changed"
        )
    return router


def _validate_generated_models() -> None:
    payload_fields = getattr(TurnPlanUpdatedNotification, "model_fields", {})
    if set(payload_fields) != {"explanation", "plan", "thread_id", "turn_id"}:
        raise TurnPlanObservationUnavailable("Turn plan payload fields changed")
    if {
        name: getattr(payload_fields.get(name), "alias", None)
        for name in ("explanation", "thread_id", "turn_id", "plan")
    } != {
        "explanation": None,
        "thread_id": "threadId",
        "turn_id": "turnId",
        "plan": None,
    }:
        raise TurnPlanObservationUnavailable("Turn plan payload fields changed")
    if getattr(payload_fields["explanation"], "default", object()) is not None:
        raise TurnPlanObservationUnavailable("Turn plan explanation default changed")
    step_fields = getattr(TurnPlanStep, "model_fields", {})
    if set(step_fields) != {"step", "status"}:
        raise TurnPlanObservationUnavailable("Turn plan step fields changed")
    if {member.value for member in TurnPlanStepStatus} != {
        "pending",
        "inProgress",
        "completed",
    }:
        raise TurnPlanObservationUnavailable("Turn plan step statuses changed")


def _plan_steps(items: object) -> tuple[TurnPlanStepSnapshot, ...]:
    if not isinstance(items, list):
        raise TurnPlanObservationUnavailable("native Turn plan is not a list")
    steps: list[TurnPlanStepSnapshot] = []
    for item in items:
        if type(item) is not TurnPlanStep:
            raise TurnPlanObservationUnavailable("native Turn plan step shape changed")
        step = item.step
        if not isinstance(step, str) or not step.strip():
            raise TurnPlanObservationUnavailable("native Turn plan step is empty")
        status = getattr(item.status, "value", None)
        try:
            mapped = TurnPlanStepState(status)
        except (TypeError, ValueError) as error:
            raise TurnPlanObservationUnavailable(
                "native Turn plan step status changed"
            ) from error
        steps.append(TurnPlanStepSnapshot(step=step, status=mapped))
    return tuple(steps)


def _validate_exact_id(value: str, *, label: str) -> None:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"native {label} ID must be a non-empty trimmed string")
