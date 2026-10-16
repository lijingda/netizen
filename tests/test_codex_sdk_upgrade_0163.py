from __future__ import annotations

import unittest
from itertools import product

from openai_codex.client import CodexClient, CodexConfig
from openai_codex.errors import TransportClosedError
from openai_codex.generated.v2_all import ThreadReadStateChangedNotification

from netizen_cli.turn_activity import (
    TurnActivityNotificationProjection,
    project_turn_activity_notification,
)


class CodexReadStateRoutingTest(unittest.TestCase):
    def test_read_state_does_not_complete_or_consume_a_turn_or_goal(self) -> None:
        # A read receipt can precede turn/completed. Its nested turnId is an
        # unread position, not the notification's execution identity.
        positions = (None, {"type": "threadStart"},
                     {"type": "turn", "turnId": "turn-active"})
        for goal_owned, first_unread in product((False, True), positions):
            with self.subTest(goal_owned=goal_owned, first_unread=first_unread):
                client = CodexClient(CodexConfig())
                router = client._router
                with router.pending_turn("thread-one") as cursors:
                    subscription = router.prepare_turn(
                        "turn-active", "thread-one", cursors, for_handle=True,
                    )
                self.assertIsNotNone(subscription)
                self.addCleanup(subscription.close)
                goal = router.register_goal("thread-one") if goal_owned else None
                started = client._coerce_notification("turn/started", {
                    "threadId": "thread-one",
                    "turn": {"id": "turn-active", "status": "inProgress", "items": []},
                })
                router.route_notification(started)
                before = subscription._state.next_event
                goal_count = goal._notifications.qsize() if goal is not None else None

                notification = client._coerce_notification("thread/readState/changed", {
                    "threadId": "thread-one",
                    "readState": {"revision": "revision-one", "firstUnread": first_unread},
                })
                self.assertIsInstance(notification.payload, ThreadReadStateChangedNotification)
                router.route_notification(notification)
                self.assertIs(router.next_global_notification(), notification)
                self.assertEqual(subscription._state.next_event, before)
                self.assertFalse(subscription._state.completed)
                if goal is not None:
                    self.assertEqual(goal.current_turn(), "turn-active")
                    self.assertEqual(goal._notifications.qsize(), goal_count)
                    self.assertFalse(goal.is_finished())
                self.assertEqual(
                    project_turn_activity_notification(
                        notification, expected_thread_id="thread-one", expected_turn_id="turn-active",
                    ),
                    TurnActivityNotificationProjection(),
                )

                completed = client._coerce_notification("turn/completed", {
                    "threadId": "thread-one",
                    "turn": {"id": "turn-active", "status": "completed", "items": []},
                })
                router.route_notification(completed)
                if goal is None:
                    self.assertTrue(subscription._state.completed)
                    self.assertIs(subscription.next(), started)
                    self.assertIs(subscription.next(), completed)
                    with self.assertRaises(TransportClosedError):
                        subscription.next()
                else:
                    self.assertEqual(subscription._state.next_event, before)
                    self.assertEqual(goal._notifications.qsize(), goal_count + 1)


if __name__ == "__main__":
    unittest.main()
