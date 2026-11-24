"""Ephemeral, ungrouped identities for the safe Activity projection of one physical Turn."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import blake2b
from secrets import token_bytes
from time import time
from typing import Literal

from .progress_card_settings import ProgressCardSettings
from .turn_activity import (
    ACTIVITY_PLAN_LIMIT, TurnActivityEvent, TurnActivityKind, TurnActivityStatus,
    TurnPlanStepSnapshot, TurnPlanStepState, activity_operation_label, sanitize_activity_text,
)


@dataclass(slots=True)
class ObservedProcessItem:
    """A held reference can observe END even after the finished window evicts the row."""

    identity: str = field(repr=False)
    kind: Literal["commentary", "operation", "step"]
    text: str
    started_at: int
    finished_at: int | None = None

    def finish(self, now: int) -> None:
        if self.finished_at is None:
            self.finished_at = max(self.started_at, now)


class ProcessActivityWindow:
    def __init__(self) -> None:
        self._salt = token_bytes(16)
        self._rows: dict[str, ObservedProcessItem] = {}
        self.revision = 0

    @property
    def rows(self) -> tuple[ObservedProcessItem, ...]:
        return tuple(self._rows.values())

    def _identity(self, kind: str, key: str) -> str:
        return blake2b((kind + "\0" + key).encode("utf-8", "surrogatepass"),
                       key=self._salt, digest_size=16).hexdigest()

    def observe(self, event: TurnActivityEvent, settings: ProgressCardSettings) -> None:
        commentary = event.kind is TurnActivityKind.COMMENTARY
        kind: Literal["commentary", "operation"] = "commentary" if commentary else "operation"
        if commentary:
            if not settings.show_commentary or not event.text or event.status is TurnActivityStatus.IN_PROGRESS:
                return
            text, limit = event.text[:settings.commentary_max_chars], settings.commentary_limit
        else:
            if not settings.show_operations:
                return
            text = activity_operation_label(event.kind.value, event.text, event.count)[:settings.operation_max_chars]
            limit = settings.operation_limit
        identity = self._identity(kind, event.item_id)
        now = int(time())
        row = self._rows.get(identity)
        if row is None:
            row = self._rows[identity] = ObservedProcessItem(identity, kind, text, now)
        elif row.finished_at is not None or (row.text == text and event.status is TurnActivityStatus.IN_PROGRESS):
            return
        row.text = text
        if event.status is not TurnActivityStatus.IN_PROGRESS:
            row.finish(now)
            # Dict order among completed rows is the order their END was observed.
            self._rows.pop(identity)
            self._rows[identity] = row
            finished = [key for key, item in self._rows.items() if item.kind == kind and item.finished_at is not None]
            for key in finished[:-limit]:
                del self._rows[key]
        self.revision += 1

    def plan(self, steps: tuple[TurnPlanStepSnapshot, ...]) -> None:
        now = int(time())
        current: set[str] = set()
        for step in steps[:ACTIVITY_PLAN_LIMIT]:
            text = sanitize_activity_text(step.step) or "未命名步骤"
            identity = self._identity("step", text)
            if identity in current:
                continue
            current.add(identity)
            row = self._rows.get(identity)
            if step.status is TurnPlanStepState.PENDING:
                if row is not None and row.finished_at is None:
                    row.finish(now)
                    self.revision += 1
                continue
            if row is None:
                row = self._rows[identity] = ObservedProcessItem(identity, "step", text, now)
                self.revision += 1
            if step.status is TurnPlanStepState.COMPLETED and row.finished_at is None:
                row.finish(now)
                self.revision += 1
        for key, row in tuple(self._rows.items()):
            if row.kind == "step" and key not in current:
                row.finish(now)
                del self._rows[key]
                self.revision += 1

    def close(self) -> None:
        now = int(time())
        for row in self._rows.values():
            row.finish(now)
