from __future__ import annotations

import unittest

from openai_codex.client import CodexClient, CodexConfig
from openai_codex.generated.v2_all import (
    CodexErrorInfoValue,
    ErrorNotification,
    TurnCompletedNotification,
    TurnStatus,
)

from netizen_cli.error_messages import describe_error, native_turn_failure
from netizen_cli.turn_activity import project_turn_activity_notification


class CodexSdkUpgradeContractTest(unittest.TestCase):
    def prepare_turn(self, client: CodexClient, turn_id: str):
        router = client._router
        with router.pending_turn("thread-one") as cursors:
            subscription = router.prepare_turn(turn_id, "thread-one", cursors, for_handle=True)
        self.assertIsNotNone(subscription)
        self.addCleanup(subscription.close)
        return subscription

    def test_failed_turn_routes_typed_errors_and_displays_only_public_details(self) -> None:
        for code, expected in (
            ("apiKeySpendLimitExceeded", CodexErrorInfoValue.api_key_spend_limit_exceeded),
            ("projectSpendLimitExceeded", CodexErrorInfoValue.project_spend_limit_exceeded),
            ("organizationSpendLimitExceeded", CodexErrorInfoValue.organization_spend_limit_exceeded),
            ("bioPolicy", CodexErrorInfoValue.bio_policy),
        ):
            with self.subTest(code=code):
                client = CodexClient(CodexConfig())
                failed = self.prepare_turn(client, "turn-failed")
                other = self.prepare_turn(client, "turn-other")
                error = {
                    "message": "Upstream request failed",
                    "codexErrorInfo": code,
                    "additionalDetails": "private request details",
                }
                note = client._coerce_notification("error", {
                    "threadId": "thread-one", "turnId": "turn-failed", "willRetry": False, "error": error,
                })
                client._router.route_notification(note)
                self.assertEqual(failed._state.next_event, 1)
                delivered = failed.next()
                self.assertIsInstance(delivered.payload, ErrorNotification)
                self.assertIs(delivered.payload.error.codex_error_info.root, expected)
                self.assertFalse(failed._state.completed)

                completed = client._coerce_notification("turn/completed", {
                    "threadId": "thread-one",
                    "turn": {"id": "turn-failed", "status": "failed", "items": [], "error": error},
                })
                client._router.route_notification(completed)
                self.assertEqual(failed._state.next_event, 2)
                delivered = failed.next()
                self.assertIsInstance(delivered.payload, TurnCompletedNotification)
                turn = delivered.payload.turn
                self.assertIs(turn.status, TurnStatus.failed)
                self.assertIs(turn.error.codex_error_info.root, expected)
                self.assertTrue(failed._state.completed)
                self.assertEqual(other._state.next_event, 0)
                self.assertFalse(other._state.completed)

                message = describe_error(native_turn_failure(turn.error))
                self.assertIn(code, message)
                self.assertIn(error["message"], message)
                self.assertNotIn(error["additionalDetails"], message)

    def test_mcp_environment_id_preserves_activity_projection(self) -> None:
        client = CodexClient(CodexConfig())
        target = {"expected_thread_id": "thread-one", "expected_turn_id": "turn-one"}
        for method, status, timestamp in (
            ("item/started", "inProgress", {"startedAtMs": 42}),
            ("item/completed", "completed", {"completedAtMs": 43}),
        ):
            with self.subTest(method=method):
                item = {
                    "type": "mcpToolCall", "id": "mcp-one", "server": "server", "tool": "lookup",
                    "arguments": {}, "status": status,
                }
                payload = {"threadId": "thread-one", "turnId": "turn-one", **timestamp}
                plain = client._coerce_notification(method, {**payload, "item": item})
                scoped = client._coerce_notification(method, {
                    **payload, "item": {**item, "environmentId": "environment-one"},
                })
                self.assertEqual(scoped.payload.item.root.environment_id, "environment-one")
                baseline = project_turn_activity_notification(plain, **target)
                self.assertIsNotNone(baseline.event)
                self.assertEqual(project_turn_activity_notification(scoped, **target), baseline)
