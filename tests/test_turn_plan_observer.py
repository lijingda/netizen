from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

import openai_codex
from openai_codex import AsyncCodex
from openai_codex.generated.v2_all import (
    AgentMessageThreadItem,
    CommandExecutionStatus,
    CommandExecutionThreadItem,
    ItemCompletedNotification,
    ItemStartedNotification,
    MessagePhase,
    ThreadItem,
    Turn,
    TurnCompletedNotification,
    TurnPlanStep,
    TurnPlanStepStatus,
    TurnPlanUpdatedNotification,
    TurnStatus,
)
from openai_codex.models import Notification, UnknownNotification

from netizen import turn_plan_observer
from netizen.turn_plan_observer import (
    PinnedTurnActivityObserver,
    TurnActivityObservationUnavailable,
    TurnPlanStepState,
)
from netizen.turn_activity import TurnActivityKind, TurnActivityStatus


def _plan(
    *,
    thread_id: str = "thread-one",
    turn_id: str = "turn-one",
    explanation: str | None = "current native plan",
    steps: tuple[tuple[str, TurnPlanStepStatus], ...] = (
        ("inspect", TurnPlanStepStatus.in_progress),
        ("verify", TurnPlanStepStatus.pending),
    ),
) -> Notification:
    return Notification(
        method="turn/plan/updated",
        payload=TurnPlanUpdatedNotification(
            explanation=explanation,
            threadId=thread_id,
            turnId=turn_id,
            plan=[TurnPlanStep(step=step, status=status) for step, status in steps],
        ),
    )


class PinnedTurnActivityObserverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.codex = AsyncCodex()
        self.codex._initialized = True
        self.router = self.codex._client._sync._router
        with self.router.pending_turn("thread-one") as cursors:
            self.subscription = self.router.prepare_turn(
                "turn-one", "thread-one", cursors, for_handle=True
            )
        self.assertIsNotNone(self.subscription)
        self.state = self.router._turn_states["turn-one"]
        self.observer = PinnedTurnActivityObserver(self.codex)

    def tearDown(self) -> None:
        self.subscription.close()

    def _append_raw(self, item: object) -> None:
        with self.router._lock:
            self.state.events[self.state.next_event] = item
            self.state.next_event += 1

    def test_snapshot_is_non_consuming_and_maps_exact_native_plan(self) -> None:
        first = _plan()
        self.router.route_notification(first)
        self.assertNotIn("turn-one", self.router._turn_notifications)
        with self.router._lock:
            before = tuple(self.state.events.items())
            subscribers_before = dict(self.state.subscribers)
            cursors_before = (self.state.first_event, self.state.next_event)

        observation = self.observer.observe(
            thread_id="thread-one",
            turn_id="turn-one",
            after_cursor=0,
        )

        with self.router._lock:
            after = tuple(self.state.events.items())
            self.assertEqual(self.state.subscribers, subscribers_before)
            self.assertEqual(
                (self.state.first_event, self.state.next_event), cursors_before
            )
        self.assertEqual(
            tuple((cursor, id(item)) for cursor, item in after),
            tuple((cursor, id(item)) for cursor, item in before),
        )
        self.assertEqual(observation.next_cursor, 1)
        self.assertEqual(observation.plan_cursor, 1)
        self.assertEqual(observation.retained_count, 1)
        self.assertEqual(
            tuple((item.step, item.status) for item in observation.steps),
            (
                ("inspect", TurnPlanStepState.IN_PROGRESS),
                ("verify", TurnPlanStepState.PENDING),
            ),
        )

    def test_later_plan_is_a_full_replacement_and_cursor_is_incremental(self) -> None:
        self.router.route_notification(_plan())
        first = self.observer.observe(
            thread_id="thread-one",
            turn_id="turn-one",
            after_cursor=0,
        )
        self.router.route_notification(
            _plan(
                steps=(("ship", TurnPlanStepStatus.completed),),
            )
        )

        second = self.observer.observe(
            thread_id="thread-one",
            turn_id="turn-one",
            after_cursor=first.next_cursor,
        )

        self.assertEqual(second.next_cursor, 2)
        self.assertEqual(second.plan_cursor, 2)
        self.assertEqual(len(second.steps), 1)
        self.assertEqual(second.steps[0].step, "ship")
        self.assertIs(second.steps[0].status, TurnPlanStepState.COMPLETED)

    def test_allowlisted_activity_is_sanitized_and_terminal_is_only_a_signal(
        self,
    ) -> None:
        commentary = AgentMessageThreadItem(
            id="commentary-one",
            phase=MessagePhase.commentary,
            text="Checked `/Users/user/private.py` with api_key=do-not-show",
            type="agentMessage",
        )
        command = CommandExecutionThreadItem(
            id="command-one",
            command="cat /Users/user/private.py",
            commandActions=[],
            cwd="/Users/user",
            status=CommandExecutionStatus.in_progress,
            type="commandExecution",
        )
        self.router.route_notification(
            Notification(
                method="item/started",
                payload=ItemStartedNotification(
                    item=ThreadItem(root=command),
                    startedAtMs=1,
                    threadId="thread-one",
                    turnId="turn-one",
                ),
            )
        )
        self.router.route_notification(
            Notification(
                method="item/completed",
                payload=ItemCompletedNotification(
                    completedAtMs=2,
                    item=ThreadItem(root=commentary),
                    threadId="thread-one",
                    turnId="turn-one",
                ),
            )
        )
        self.router.route_notification(
            Notification(
                method="turn/completed",
                payload=TurnCompletedNotification(
                    threadId="thread-one",
                    turn=Turn(
                        id="turn-one",
                        items=[],
                        status=TurnStatus.completed,
                    ),
                ),
            )
        )

        observation = self.observer.observe(
            thread_id="thread-one",
            turn_id="turn-one",
            after_cursor=0,
        )

        self.assertTrue(observation.turn_completed)
        self.assertEqual(len(observation.events), 2)
        self.assertEqual(
            (observation.events[0].kind, observation.events[0].status),
            (TurnActivityKind.COMMAND, TurnActivityStatus.IN_PROGRESS),
        )
        self.assertEqual(observation.events[0].event_timestamp_ms, 1)
        self.assertIsNone(observation.events[0].text)
        self.assertEqual(observation.events[1].event_timestamp_ms, 2)
        self.assertEqual(
            observation.events[1].text,
            "[敏感内容已隐藏]",
        )
        self.assertNotIn("cat", repr(observation.events))
        self.assertNotIn("private.py", repr(observation.events))

    def test_mismatched_thread_and_turn_payloads_cannot_update_exact_turn(self) -> None:
        self._append_raw(_plan(thread_id="thread-other"))
        self._append_raw(_plan(turn_id="turn-other"))

        observation = self.observer.observe(
            thread_id="thread-one",
            turn_id="turn-one",
            after_cursor=0,
        )

        self.assertEqual(observation.next_cursor, 2)
        self.assertFalse(observation.plan_updated)
        self.assertEqual(observation.steps, ())

    def test_invalid_plan_payload_fails_closed(self) -> None:
        self._append_raw(
            Notification(
                method="turn/plan/updated",
                payload=UnknownNotification(
                    {"threadId": "thread-one", "turnId": "turn-one"}
                ),
            )
        )
        with self.assertRaisesRegex(
            TurnActivityObservationUnavailable,
            "payload shape changed",
        ):
            self.observer.observe(
                thread_id="thread-one",
                turn_id="turn-one",
                after_cursor=0,
            )

    def test_pruning_observed_events_preserves_absolute_cursor(self) -> None:
        first = _plan()
        self.router.route_notification(first)
        observed = self.observer.observe(
            thread_id="thread-one", turn_id="turn-one", after_cursor=0
        )
        self.assertIs(self.subscription.next(), first)
        self.assertEqual(self.state.first_event, 1)
        self.router.route_notification(_plan(steps=(("ship", TurnPlanStepStatus.completed),)))

        next_observed = self.observer.observe(
            thread_id="thread-one",
            turn_id="turn-one",
            after_cursor=observed.next_cursor,
        )

        self.assertEqual(next_observed.next_cursor, 2)
        self.assertEqual(next_observed.plan_cursor, 2)
        self.assertEqual(next_observed.retained_count, 1)
        self.assertEqual(next_observed.steps[0].step, "ship")
        self.subscription.next()
        empty = self.observer.observe(
            thread_id="thread-one", turn_id="turn-one", after_cursor=2
        )
        self.assertEqual(empty.next_cursor, 2)
        self.assertEqual(empty.retained_count, 0)
        self.assertFalse(empty.plan_updated)

    def test_pruning_unobserved_events_and_cursor_rewind_fail_closed(self) -> None:
        self.router.route_notification(_plan())
        self.subscription.next()
        with self.assertRaisesRegex(
            TurnActivityObservationUnavailable,
            "pruned before the observation cursor",
        ):
            self.observer.observe(
                thread_id="thread-one", turn_id="turn-one", after_cursor=0
            )
        with self.assertRaisesRegex(
            TurnActivityObservationUnavailable, "cursor moved backwards"
        ):
            self.observer.observe(
                thread_id="thread-one", turn_id="turn-one", after_cursor=2
            )

    def test_retained_count_includes_ignored_notifications_before_cursor(self) -> None:
        ignored = Notification(
            method="item/agentMessage/delta",
            payload=UnknownNotification({"threadId": "thread-one", "turnId": "turn-one"}),
        )
        for _ in range(4096):
            self.router.route_notification(ignored)
        observation = self.observer.observe(
            thread_id="thread-one", turn_id="turn-one", after_cursor=4096
        )
        self.assertEqual(observation.retained_count, 4096)
        self.assertEqual(observation.next_cursor, 4096)
        self.assertEqual(observation.events, ())
        self.assertFalse(observation.plan_updated)

    def test_event_cursor_gap_fails_closed_without_consuming_other_events(self) -> None:
        self.router.route_notification(_plan())
        self.router.route_notification(_plan())
        before = dict(self.state.events)
        for broken_events in ({1: before[1]}, {0: before[0], 2: before[1]}):
            with self.subTest(events=broken_events):
                with patch.object(self.state, "events", broken_events):
                    with self.assertRaisesRegex(
                        TurnActivityObservationUnavailable, "cursor gap"
                    ):
                        self.observer.observe(
                            thread_id="thread-one", turn_id="turn-one", after_cursor=1
                        )
        self.assertEqual(self.state.events, before)
        self.assertIs(self.subscription.next(), before[0])
        self.assertIs(self.subscription.next(), before[1])

    def test_completion_keeps_events_until_handle_closes(self) -> None:
        completed = Notification(
            method="turn/completed",
            payload=TurnCompletedNotification(
                threadId="thread-one",
                turn=Turn(id="turn-one", items=[], status=TurnStatus.completed),
            ),
        )
        self.router.route_notification(completed)
        observation = self.observer.observe(
            thread_id="thread-one", turn_id="turn-one", after_cursor=0
        )
        self.assertTrue(observation.turn_completed)
        self.assertIs(self.subscription.next(), completed)
        self.subscription.close()
        self.assertNotIn("turn-one", self.router._turn_states)
        with self.assertRaisesRegex(TurnActivityObservationUnavailable, "store is unavailable"):
            self.observer.observe(
                thread_id="thread-one", turn_id="turn-one", after_cursor=1
            )

    def test_transport_failure_is_observation_only_and_remains_for_consumer(self) -> None:
        failure = RuntimeError("transport stopped")
        self.router.fail_all(failure)
        with self.assertRaisesRegex(TurnActivityObservationUnavailable, "transport failure"):
            self.observer.observe(
                thread_id="thread-one", turn_id="turn-one", after_cursor=0
            )
        with self.assertRaises(RuntimeError) as caught:
            self.subscription.next()
        self.assertIs(caught.exception, failure)

    def test_concurrent_turns_have_independent_state_and_cursors(self) -> None:
        with self.router.pending_turn("thread-two") as cursors:
            second_subscription = self.router.prepare_turn(
                "turn-two", "thread-two", cursors, for_handle=True
            )
        try:
            self.router.route_notification(_plan())
            self.router.route_notification(
                _plan(thread_id="thread-two", turn_id="turn-two", steps=())
            )
            first = self.observer.observe(
                thread_id="thread-one", turn_id="turn-one", after_cursor=0
            )
            second = self.observer.observe(
                thread_id="thread-two", turn_id="turn-two", after_cursor=0
            )
            self.assertEqual(first.next_cursor, 1)
            self.assertEqual(second.next_cursor, 1)
            self.assertEqual(len(first.steps), 2)
            self.assertEqual(second.steps, ())
            self.assertTrue(second.plan_updated)
        finally:
            second_subscription.close()

    def test_version_fingerprint_and_router_shape_changes_fail_closed(self) -> None:
        with patch.object(openai_codex, "__version__", "0.154.1"):
            with self.assertRaisesRegex(
                TurnActivityObservationUnavailable,
                "supports only openai-codex==0.154.0",
            ):
                PinnedTurnActivityObserver(self.codex)
        with patch.object(
            turn_plan_observer,
            "_PACKAGE_SOURCE_FINGERPRINT",
            "0" * 64,
        ):
            with self.assertRaisesRegex(
                TurnActivityObservationUnavailable,
                "package source fingerprint changed",
            ):
                PinnedTurnActivityObserver(self.codex)

        with patch.object(self.router, "_lock", threading.Lock()):
            with self.assertRaisesRegex(TurnActivityObservationUnavailable, "router lock changed"):
                PinnedTurnActivityObserver(self.codex)
        with patch.dict(self.router._turn_states, {"turn-one": object()}):
            with self.assertRaisesRegex(TurnActivityObservationUnavailable, "store is unavailable"):
                self.observer.observe(
                    thread_id="thread-one", turn_id="turn-one", after_cursor=0
                )

    def test_state_identity_and_shape_changes_fail_closed(self) -> None:
        for field, value, error in (
            ("id", "turn-other", "identity changed"),
            ("thread_id", "thread-other", "identity changed"),
            ("events", [], "shape changed"),
            ("first_event", True, "shape changed"),
            ("next_event", -1, "shape changed"),
            ("completed", None, "shape changed"),
            ("subscribers", {}, "shape changed"),
        ):
            with self.subTest(field=field):
                with patch.object(self.state, field, value):
                    with self.assertRaisesRegex(TurnActivityObservationUnavailable, error):
                        self.observer.observe(
                            thread_id="thread-one", turn_id="turn-one", after_cursor=0
                        )


if __name__ == "__main__":
    unittest.main()
