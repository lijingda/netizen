from __future__ import annotations

import unittest
from unittest.mock import patch

import openai_codex
from openai_codex import AsyncCodex
from openai_codex.generated.v2_all import (
    TurnPlanStep,
    TurnPlanStepStatus,
    TurnPlanUpdatedNotification,
)
from openai_codex.models import Notification, UnknownNotification

from netizen import turn_plan_observer
from netizen.turn_plan_observer import (
    PinnedTurnPlanObserver,
    TurnPlanObservationUnavailable,
    TurnPlanStepState,
)


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


class PinnedTurnPlanObserverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.codex = AsyncCodex()
        self.codex._initialized = True
        self.router = self.codex._client._sync._router
        self.router.register_turn("turn-one")
        self.observer = PinnedTurnPlanObserver(self.codex)

    def test_snapshot_is_non_consuming_and_maps_exact_native_plan(self) -> None:
        first = _plan()
        self.router.route_notification(first)
        turn_queue = self.router._turn_notifications["turn-one"]
        with turn_queue.mutex:
            before = tuple(turn_queue.queue)

        observation = self.observer.observe(
            thread_id="thread-one",
            turn_id="turn-one",
            after_cursor=0,
        )

        with turn_queue.mutex:
            after = tuple(turn_queue.queue)
        self.assertEqual(len(after), len(before))
        self.assertEqual(
            tuple(id(item) for item in after),
            tuple(id(item) for item in before),
        )
        self.assertEqual(observation.next_cursor, 1)
        self.assertEqual(observation.plan_cursor, 1)
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

    def test_mismatched_thread_and_turn_payloads_cannot_update_exact_turn(self) -> None:
        turn_queue = self.router._turn_notifications["turn-one"]
        turn_queue.put(_plan(thread_id="thread-other"))
        turn_queue.put(_plan(turn_id="turn-other"))

        observation = self.observer.observe(
            thread_id="thread-one",
            turn_id="turn-one",
            after_cursor=0,
        )

        self.assertEqual(observation.next_cursor, 2)
        self.assertFalse(observation.plan_updated)
        self.assertEqual(observation.steps, ())

    def test_invalid_plan_payload_and_consumed_cursor_fail_closed(self) -> None:
        turn_queue = self.router._turn_notifications["turn-one"]
        turn_queue.put(
            Notification(
                method="turn/plan/updated",
                payload=UnknownNotification(
                    {"threadId": "thread-one", "turnId": "turn-one"}
                ),
            )
        )
        with self.assertRaisesRegex(
            TurnPlanObservationUnavailable,
            "payload shape changed",
        ):
            self.observer.observe(
                thread_id="thread-one",
                turn_id="turn-one",
                after_cursor=0,
            )

        turn_queue.get_nowait()
        with self.assertRaisesRegex(
            TurnPlanObservationUnavailable,
            "consumed unexpectedly",
        ):
            self.observer.observe(
                thread_id="thread-one",
                turn_id="turn-one",
                after_cursor=1,
            )

    def test_version_fingerprint_and_queue_shape_changes_fail_closed(self) -> None:
        with patch.object(openai_codex, "__version__", "0.147.1"):
            with self.assertRaisesRegex(
                TurnPlanObservationUnavailable,
                "supports only openai-codex==0.147.0",
            ):
                PinnedTurnPlanObserver(self.codex)
        with patch.object(
            turn_plan_observer,
            "_PACKAGE_SOURCE_FINGERPRINT",
            "0" * 64,
        ):
            with self.assertRaisesRegex(
                TurnPlanObservationUnavailable,
                "package source fingerprint changed",
            ):
                PinnedTurnPlanObserver(self.codex)

        self.router._turn_notifications["turn-one"] = object()  # type: ignore[assignment]
        with self.assertRaisesRegex(
            TurnPlanObservationUnavailable,
            "queue is unavailable",
        ):
            self.observer.observe(
                thread_id="thread-one",
                turn_id="turn-one",
                after_cursor=0,
            )


if __name__ == "__main__":
    unittest.main()
