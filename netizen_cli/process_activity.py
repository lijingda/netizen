"""Ephemeral, ungrouped identities for the safe Activity projection of one physical Turn."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from hashlib import blake2b
from secrets import token_bytes
from typing import Literal

from .progress_card_settings import ProgressCardSettings
from .turn_activity import (
    ACTIVITY_PLAN_LIMIT, TurnActivityEvent, TurnActivityKind, TurnActivityStatus,
    TurnPlanStepSnapshot, TurnPlanStepState, activity_operation_label, bounded_activity_text, sanitize_activity_text,
)


def process_plan_steps(steps: tuple[TurnPlanStepSnapshot, ...]) -> Iterator[tuple[str, TurnPlanStepState, bool]]:
    """Yield the first status for each wire name and whether it can admit a new step."""
    seen: set[str] = set()
    for index, step in enumerate(steps):
        name = sanitize_activity_text(step.step) or "未命名步骤"
        if name not in seen:
            seen.add(name)
            yield name, step.status, index < ACTIVITY_PLAN_LIMIT


@dataclass(frozen=True, slots=True)
class ObservedProcessItem:
    """One immutable observation, including the local second of each observed transition."""

    identity: str = field(repr=False)
    kind: Literal["commentary", "operation", "step"]
    text: str
    started_at: int
    finished_at: int | None = None

    def finish(self, now: int) -> ObservedProcessItem:
        return self if self.finished_at is not None else replace(self, finished_at=max(self.started_at, now))


class ProcessActivityWindow:
    """Rows leave only after they finish. A replacement plan ends removed steps,
    and closing the physical Turn ends every remaining row. Earlier snapshots
    stay immutable even when a later observation evicts a finished row.
    """

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
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
            text, limit = bounded_activity_text(event.text, settings.commentary_max_chars), settings.commentary_limit
        else:
            if not settings.show_operations:
                return
            label = activity_operation_label(event.kind.value, event.text, event.count)
            text = bounded_activity_text(label, settings.operation_max_chars)
            limit = settings.operation_limit
        identity = self._identity(kind, event.item_id)
        now = int(self._clock())
        row = self._rows.get(identity)
        if row is None:
            row = self._rows[identity] = ObservedProcessItem(identity, kind, text, now)
        elif row.finished_at is not None or (row.text == text and event.status is TurnActivityStatus.IN_PROGRESS):
            return
        elif row.text != text:
            row = self._rows[identity] = replace(row, text=text)
        if event.status is not TurnActivityStatus.IN_PROGRESS:
            row = row.finish(now)
            # Dict order among completed rows is the order their END was observed.
            self._rows.pop(identity)
            self._rows[identity] = row
            finished = [key for key, item in self._rows.items() if item.kind == kind and item.finished_at is not None]
            for key in finished[:-limit]:
                del self._rows[key]
        self.revision += 1

    def plan(self, steps: tuple[TurnPlanStepSnapshot, ...]) -> None:
        now = int(self._clock())
        current: set[str] = set()
        for text, status, admit in process_plan_steps(steps):
            identity = self._identity("step", text)
            row = self._rows.get(identity)
            if row is None and not admit:
                continue
            current.add(identity)
            if status is TurnPlanStepState.PENDING:
                if row is not None and row.finished_at is None:
                    self._rows[identity] = row.finish(now)
                    self.revision += 1
                continue
            if row is None:
                row = self._rows[identity] = ObservedProcessItem(identity, "step", text, now)
                self.revision += 1
            if status is TurnPlanStepState.COMPLETED and row.finished_at is None:
                self._rows[identity] = row.finish(now)
                self.revision += 1
        for key, row in tuple(self._rows.items()):
            if row.kind == "step" and key not in current:
                self._rows[key] = row.finish(now)
                del self._rows[key]
                self.revision += 1

    def close(self) -> None:
        now = int(self._clock())
        for key, row in self._rows.items():
            if row.finished_at is None:
                self._rows[key] = row.finish(now)
                self.revision += 1
