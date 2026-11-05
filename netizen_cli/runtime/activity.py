"""Activity, progress and Partial Answer snapshots for live work."""

from __future__ import annotations

import logging
from dataclasses import replace


from ..partial_answers import PartialAnswer, partial_answers_from_items
from .contracts import (
    SideSessionState,
    ActiveTurnSnapshot,
    TurnProgressSnapshot,
    TurnActivitySnapshot,
    SideTurnActivitySnapshot,
    GoalActivitySnapshot,
    ActiveGoalSnapshot,
    BindingRuntimeSnapshot,
)
from ..turn_activity import (
    TurnActivityEntrySnapshot,
    TurnActivityEvent,
    TurnActivityKind,
    TurnActivityNotificationProjection,
    TurnActivityStatus,
)
from ..turn_plan_observer import TurnActivityObservation

from .state import (
    RUNTIME_LOGGER_NAME,
    _ActiveTurn,
    _ActiveGoal,
    _ActiveSideTurn,
)
from .queries import NativeQueryRuntime


logger = logging.getLogger(RUNTIME_LOGGER_NAME)


class TurnActivityRuntime(NativeQueryRuntime):
    """Activity, progress and Partial Answer snapshots for live work."""

    def active_turn(self, binding_id: str) -> ActiveTurnSnapshot | None:
        active = self._active.get(binding_id)
        if active is None:
            return None
        return ActiveTurnSnapshot(
            binding_id=binding_id,
            thread_id=active.handle.thread_id,
            turn_id=active.handle.id,
            owner_id=active.owner_id,
            state=active.state,
        )

    def turn_progress(self, binding_id: str) -> TurnProgressSnapshot | None:
        """Project the latest native plan for the current active Turn only."""

        activity = self.turn_activity(binding_id, refresh_plan=True)
        if activity is None:
            return None
        return TurnProgressSnapshot(
            binding_id=activity.binding_id,
            thread_id=activity.thread_id,
            turn_id=activity.turn_id,
            steer_count=activity.steer_count,
            plan_available=activity.plan_available,
            plan_generated=activity.plan_generated,
            plan_may_be_stale=activity.plan_may_be_stale,
            steps=activity.steps,
        )

    def turn_activity(
        self,
        binding_id: str,
        *,
        thread_id: str | None = None,
        turn_id: str | None = None,
        refresh_plan: bool = False,
    ) -> TurnActivitySnapshot | None:
        """Return one exact Turn's latest process-local display projection.

        Callers opt into a refresh explicitly; the runtime also observes native
        questions independently of the progress presenter. Exact IDs fail closed
        to ``None`` so a delayed card updater cannot render a replacement Turn
        into an older card.
        """

        active = self._active.get(binding_id)
        if active is None or active.terminal_observed:
            return None
        if thread_id is not None and active.handle.thread_id != thread_id:
            return None
        if turn_id is not None and active.handle.id != turn_id:
            return None
        if refresh_plan:
            self._refresh_turn_activity(active)
        return self._turn_activity_snapshot(active)

    def _turn_activity_snapshot(self, active: _ActiveTurn) -> TurnActivitySnapshot:
        return TurnActivitySnapshot(
            binding_id=active.binding_id,
            thread_id=active.handle.thread_id,
            turn_id=active.handle.id,
            revision=active.activity_revision,
            state=active.state,
            steer_count=active.steer_count,
            plan_available=active.plan_available,
            plan_generated=active.plan_generated,
            plan_may_be_stale=active.plan_may_be_stale,
            steps=active.plan_steps,
            commentary=self._activity_commentary(active),
            operations=self._activity_operations(active),
            partial_answers=tuple(active.partial_answers.values()),
            progress_card_settings=active.task_feedback.progress_card_settings,
        )

    def side_turn_activity(
        self,
        side_id: str,
        *,
        thread_id: str | None = None,
        turn_id: str | None = None,
        refresh_plan: bool = False,
    ) -> SideTurnActivitySnapshot | None:
        """Return one exact Side Turn's process-local display projection."""

        session = self._sides.get(side_id)
        if session is None or session.state is SideSessionState.CLOSING:
            return None
        active = session.active
        if active is None or active.terminal_observed:
            return None
        if thread_id is not None and active.handle.thread_id != thread_id:
            return None
        if turn_id is not None and active.handle.id != turn_id:
            return None
        if refresh_plan:
            self._refresh_turn_activity(active)
        return self._side_turn_activity_snapshot(side_id, active)

    def _side_turn_activity_snapshot(
        self,
        side_id: str,
        active: _ActiveSideTurn,
    ) -> SideTurnActivitySnapshot:
        return SideTurnActivitySnapshot(
            side_id=side_id,
            thread_id=active.handle.thread_id,
            turn_id=active.handle.id,
            revision=active.activity_revision,
            state=active.state,
            steer_count=active.steer_count,
            plan_available=active.plan_available,
            plan_generated=active.plan_generated,
            plan_may_be_stale=active.plan_may_be_stale,
            steps=active.plan_steps,
            commentary=self._activity_commentary(active),
            operations=self._activity_operations(active),
            partial_answers=tuple(active.partial_answers.values()),
            progress_card_settings=active.task_feedback.progress_card_settings,
        )

    def _turn_activity_visible_state(
        self,
        active: _ActiveTurn | _ActiveSideTurn,
    ) -> tuple[object, ...]:
        settings = active.task_feedback.progress_card_settings
        return (
            active.state,
            active.steer_count,
            active.plan_available,
            (
                active.plan_generated, active.plan_may_be_stale, active.plan_steps,
            ) if settings.show_plan and active.plan_available else (),
            self._activity_commentary(active) if settings.show_commentary else (),
            self._activity_operations(active) if settings.show_operations else (),
            tuple(active.partial_answers.values()),
        )

    def _refresh_turn_activity(
        self,
        active: _ActiveTurn | _ActiveSideTurn,
    ) -> TurnActivityObservation | None:
        before = self._turn_activity_visible_state(active)
        if not active.activity_observation_enabled:
            return None
        observer = self._turn_plan_observer
        if observer is None:
            active.plan_available = False
            active.activity_observation_enabled = False
            if self._turn_activity_visible_state(active) != before:
                active.activity_revision += 1
            return None
        try:
            observation = observer.observe(
                thread_id=active.handle.thread_id,
                turn_id=active.handle.id,
                after_cursor=active.plan_cursor,
                progress_card_settings=active.task_feedback.progress_card_settings,
            )
        except Exception as error:
            active.plan_available = False
            active.activity_observation_enabled = False
            if self._turn_activity_visible_state(active) != before:
                active.activity_revision += 1
            logger.warning(
                "native Turn activity observation unavailable",
                extra={
                    "thread_id": active.handle.thread_id,
                    "turn_id": active.handle.id,
                    "error_type": type(error).__name__,
                },
            )
            return None
        active.plan_available = True
        active.plan_cursor = observation.next_cursor
        for request in observation.questions:
            self._deliver_question(active, active.handle.id, request)
        if isinstance(active, _ActiveSideTurn) and observation.turn_completed:
            # Steer freshness reads can see completion before the Side loop.
            # Preserve the drain trigger; only handle.run() proves terminal.
            active.completion_notification_seen = True
        self._apply_partial_answers(active, observation.partial_answers)
        self._apply_activity_events(active, observation.events)
        if observation.plan_updated:
            active.plan_steps = observation.steps
            active.plan_generated = True
            active.plan_last_update_cursor = observation.plan_cursor
            stale_after = active.plan_stale_after_cursor
            if (
                active.plan_may_be_stale
                and stale_after is not None
                and observation.plan_cursor is not None
                and observation.plan_cursor > stale_after
            ):
                active.plan_may_be_stale = False
                active.plan_stale_after_cursor = None
        if self._turn_activity_visible_state(active) != before:
            active.activity_revision += 1
        return observation

    @staticmethod
    def _activity_commentary(
        active: _ActiveTurn | _ActiveSideTurn | _ActiveGoal,
    ) -> tuple[TurnActivityEntrySnapshot, ...]:
        return tuple(
            active.activity_commentary[item_id]
            for item_id in active.activity_commentary_order
        )

    @staticmethod
    def _activity_operations(
        active: _ActiveTurn | _ActiveSideTurn | _ActiveGoal,
    ) -> tuple[TurnActivityEntrySnapshot, ...]:
        ordered: list[tuple[int, TurnActivityEntrySnapshot]] = []
        subagents: dict[
            tuple[int, TurnActivityStatus], tuple[int, TurnActivityEntrySnapshot]
        ] = {}
        by_time = active.task_feedback.progress_card_settings.organization == "time"
        for position, item_id in enumerate(active.activity_operation_order):
            entry = active.activity_operations[item_id]
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

    @staticmethod
    def _apply_activity_events(
        active: _ActiveTurn | _ActiveSideTurn | _ActiveGoal,
        events: tuple[TurnActivityEvent, ...],
    ) -> None:
        settings = active.task_feedback.progress_card_settings
        for event in events:
            commentary = event.kind is TurnActivityKind.COMMENTARY
            if commentary and event.text is None:
                continue
            active.activity_sequence += 1
            if commentary:
                active.activity_segment += 1
            entry = TurnActivityEntrySnapshot(
                kind=event.kind,
                status=event.status,
                event_timestamp_ms=event.event_timestamp_ms,
                text=event.text,
                count=event.count,
                sequence=active.activity_sequence,
                segment=active.activity_segment,
            )
            if commentary:
                entries = active.activity_commentary
                order = active.activity_commentary_order
                limit = settings.commentary_limit
            else:
                entries = active.activity_operations
                order = active.activity_operation_order
                limit = settings.operation_limit
            item_id = event.item_id
            entries[item_id] = entry
            if item_id in order:
                order.remove(item_id)
            order.append(item_id)
            while len(order) > limit:
                discarded = order.pop(0)
                entries.pop(discarded, None)

    @staticmethod
    def _apply_partial_answers(
        active: _ActiveTurn | _ActiveSideTurn | _ActiveGoal,
        answers: tuple[PartialAnswer, ...],
    ) -> None:
        if isinstance(active, _ActiveGoal):
            thread_id, turn_id = active.thread_id, active.activity_turn_id
        else:
            thread_id, turn_id = active.handle.thread_id, active.handle.id
        for answer in answers:
            if answer.thread_id != thread_id or answer.turn_id != turn_id:
                continue
            key = (answer.thread_id, answer.turn_id, answer.item_id)
            # Completed fragments are stable. Duplicates never reorder or
            # rewrite a fragment that may already have been delivered.
            active.partial_answers.setdefault(key, answer)

    def _supplement_partial_answers(
        self,
        active: _ActiveTurn | _ActiveSideTurn | _ActiveGoal,
        *,
        thread_id: str,
        turn_id: str,
        items: tuple[object, ...],
    ) -> None:
        """Supplement only the already-proven terminal physical Turn."""
        before = len(active.partial_answers)
        try:
            answers = partial_answers_from_items(
                items, thread_id=thread_id, turn_id=turn_id,
            )
        except (ValueError, TypeError):
            logger.warning("terminal partial answer projection unavailable", exc_info=True)
            return
        for answer in answers:
            key = (answer.thread_id, answer.turn_id, answer.item_id)
            active.partial_answers.setdefault(key, answer)
        if len(active.partial_answers) != before:
            active.activity_revision += 1

    @staticmethod
    def _reset_activity(active: _ActiveGoal) -> None:
        active.plan_cursor = 0
        active.activity_sequence = 0
        active.activity_segment = 0
        active.plan_generated = False
        active.plan_steps = ()
        active.activity_commentary.clear()
        active.activity_commentary_order.clear()
        active.activity_operations.clear()
        active.activity_operation_order.clear()

    def is_compacting(self, binding_id: str) -> bool:
        return binding_id in self._compacting

    def active_goal(self, binding_id: str) -> ActiveGoalSnapshot | None:
        active = self._goals.get(binding_id)
        if active is None:
            return None
        return ActiveGoalSnapshot(
            binding_id=active.binding_id,
            thread_id=active.thread_id,
            logical_turn_id=(active.handle.id if active.handle is not None else None),
            owner_id=active.owner_id,
            state=active.state,
            persisted=active.persisted,
        )

    def goal_activity(
        self,
        binding_id: str,
        *,
        thread_id: str | None = None,
        logical_turn_id: str | None = None,
    ) -> GoalActivitySnapshot | None:
        """Return Goal Activity maintained by its unique stream consumer."""

        active = self._goals.get(binding_id)
        handle = active.handle if active is not None else None
        if active is None or handle is None or handle.id is None:
            return None
        if active.terminal_observed:
            return None
        if thread_id is not None and active.thread_id != thread_id:
            return None
        if logical_turn_id is not None and handle.id != logical_turn_id:
            return None
        return self._goal_activity_snapshot(active)

    def _goal_activity_snapshot(self, active: _ActiveGoal) -> GoalActivitySnapshot:
        handle = active.handle
        assert handle is not None and handle.id is not None
        return GoalActivitySnapshot(
            binding_id=active.binding_id,
            thread_id=active.thread_id,
            logical_turn_id=handle.id,
            physical_turn_id=active.activity_turn_id,
            revision=active.activity_revision,
            state=active.state,
            plan_available=active.plan_available,
            plan_generated=active.plan_generated,
            steps=active.plan_steps,
            commentary=self._activity_commentary(active),
            operations=self._activity_operations(active),
            partial_answers=tuple(active.partial_answers.values()),
            progress_card_settings=active.task_feedback.progress_card_settings,
        )

    def _goal_activity_visible_state(self, active: _ActiveGoal) -> tuple[object, ...]:
        settings = active.task_feedback.progress_card_settings
        return (
            active.state,
            active.activity_turn_id,
            active.plan_available,
            (active.plan_generated, active.plan_steps)
            if settings.show_plan and active.plan_available else (),
            self._activity_commentary(active) if settings.show_commentary else (),
            self._activity_operations(active) if settings.show_operations else (),
            tuple(active.partial_answers.values()),
        )

    def _apply_goal_activity_projection(
        self,
        active: _ActiveGoal,
        projection: TurnActivityNotificationProjection | None,
    ) -> None:
        if not active.activity_observation_enabled:
            return
        before = self._goal_activity_visible_state(active)
        if projection is None:
            active.plan_available = False
            if self._goal_activity_visible_state(active) != before:
                active.activity_revision += 1
            return
        turn_id = projection.turn_id
        if turn_id is None:
            return
        active.plan_available = True
        if projection.turn_started and turn_id != active.activity_turn_id:
            if turn_id in active.activity_turn_ids:
                return
            active.activity_turn_ids.add(turn_id)
            active.activity_turn_id = turn_id
            self._reset_activity(active)
        if turn_id != active.activity_turn_id:
            # A late event from an earlier physical Turn can never repopulate
            # the new Turn's Activity Module.
            return
        if projection.plan_updated:
            active.plan_steps = projection.steps
            active.plan_generated = True
        if projection.event is not None:
            self._apply_activity_events(active, (projection.event,))
        if projection.question is not None:
            self._deliver_question(active, turn_id, projection.question)
        if projection.partial_answer is not None:
            self._apply_partial_answers(active, (projection.partial_answer,))
        if self._goal_activity_visible_state(active) != before:
            active.activity_revision += 1

    def binding_runtime_snapshot(self, binding_id: str) -> BindingRuntimeSnapshot:
        """Return all process-local management state without consuming native data."""

        return BindingRuntimeSnapshot(
            binding_id=binding_id,
            activity_revision=self._admission_revision(binding_id),
            turn=self.active_turn(binding_id),
            goal=self.active_goal(binding_id),
            compacting=self.is_compacting(binding_id),
            lifecycle=self.lifecycle_state(binding_id),
            subscription=self.thread_subscription_snapshot(binding_id),
            context_window_usage=self.context_window_usage(binding_id),
        )
