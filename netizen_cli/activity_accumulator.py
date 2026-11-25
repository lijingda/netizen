"""Bounded display state accumulated from one physical Turn's safe native events."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from time import time

from .process_activity import ObservedProcessItem, ProcessActivityWindow
from .progress_card_settings import ProgressCardSettings
from .session_settings import BindingTaskFeedback
from .turn_activity import (
    TurnActivityEntrySnapshot, TurnActivityEvent, TurnActivityKind, TurnActivityStatus, TurnPlanStepSnapshot,
)


@dataclass(slots=True)
class ActivityAccumulator:
    plan_available: bool = True
    plan_generated: bool = False
    plan_may_be_stale: bool = False
    steps: tuple[TurnPlanStepSnapshot, ...] = ()
    process_window: ProcessActivityWindow | None = field(default=None, repr=False)
    _sequence: int = 0
    _segment: int = 0
    _commentary: dict[str, TurnActivityEntrySnapshot] = field(default_factory=dict)
    _commentary_order: list[str] = field(default_factory=list)
    _operations: dict[str, TurnActivityEntrySnapshot] = field(default_factory=dict)
    _operation_order: list[str] = field(default_factory=list)

    @property
    def process_items(self) -> tuple[ObservedProcessItem, ...] | None:
        return self.process_window.rows if self.process_window is not None else None

    def close(self) -> None:
        if self.process_window is not None:
            self.process_window.close()

    def visible_state(self, settings: ProgressCardSettings) -> tuple[object, ...]:
        return (
            self.plan_available,
            (self.plan_generated, self.plan_may_be_stale, self.steps)
            if settings.show_plan and self.plan_available else (),
            self.commentary if settings.show_commentary else (),
            self.operations(settings) if settings.show_operations else (),
            self.process_window.revision if self.process_window is not None else None,
        )

    @property
    def commentary(self) -> tuple[TurnActivityEntrySnapshot, ...]:
        return tuple(
            self._commentary[item_id]
            for item_id in self._commentary_order
        )

    def operations(self, settings: ProgressCardSettings) -> tuple[TurnActivityEntrySnapshot, ...]:
        ordered: list[tuple[int, TurnActivityEntrySnapshot]] = []
        subagents: dict[
            tuple[int, TurnActivityStatus], tuple[int, TurnActivityEntrySnapshot]
        ] = {}
        by_time = settings.organization == "time"
        for position, item_id in enumerate(self._operation_order):
            entry = self._operations[item_id]
            if entry.kind is TurnActivityKind.SUBAGENT:
                # Hidden/evicted commentary still separates operation segments.
                # Within each segment keep the existing status-based grouping.
                key = (entry.segment if by_time else 0, entry.status)
                previous = subagents.get(key)
                count = entry.count + (previous[1].count if previous else 0)
                subagents[key] = (position, replace(entry, count=count))
                continue
            ordered.append((position, entry))
        ordered.extend(subagents.values())
        ordered.sort(key=lambda item: item[0])
        return tuple(item for _position, item in ordered)

    def _process_window(self, feedback: BindingTaskFeedback) -> ProcessActivityWindow | None:
        if feedback.progress_card_enabled:
            return None
        if self.process_window is None:
            self.process_window = ProcessActivityWindow(time)
        return self.process_window

    def apply_plan(self, steps: tuple[TurnPlanStepSnapshot, ...], feedback: BindingTaskFeedback) -> None:
        self.plan_generated = True
        self.steps = steps
        settings = feedback.progress_card_settings
        assert settings is not None  # Execution captures the instance fallback.
        if settings.show_plan and (window := self._process_window(feedback)):
            window.plan(steps)

    def observe(
        self,
        events: tuple[TurnActivityEvent, ...],
        feedback: BindingTaskFeedback,
    ) -> None:
        settings = feedback.progress_card_settings
        assert settings is not None  # Execution captures the instance fallback.
        window = self._process_window(feedback) if events else None
        for event in events:
            if window is not None:
                window.observe(event, settings)
            commentary = event.kind is TurnActivityKind.COMMENTARY
            if commentary and event.text is None:
                continue
            self._sequence += 1
            if commentary:
                self._segment += 1
            entry = TurnActivityEntrySnapshot(
                kind=event.kind,
                status=event.status,
                event_timestamp_ms=event.event_timestamp_ms,
                text=event.text,
                count=event.count,
                sequence=self._sequence,
                segment=self._segment,
            )
            if commentary:
                entries = self._commentary
                order = self._commentary_order
                limit = settings.commentary_limit
            else:
                entries = self._operations
                order = self._operation_order
                limit = settings.operation_limit
            item_id = event.item_id
            entries[item_id] = entry
            if item_id in order:
                order.remove(item_id)
            order.append(item_id)
            while len(order) > limit:
                discarded = order.pop(0)
                entries.pop(discarded, None)
