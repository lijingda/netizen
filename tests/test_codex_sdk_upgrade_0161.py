from __future__ import annotations

import unittest

from openai_codex.client import CodexClient, CodexConfig
from openai_codex.generated.v2_all import (
    CodexErrorInfo,
    ErrorNotification,
    McpServerOauthLoginCompletedNotification,
    McpServerOauthLoginResponse,
    ServerNotification,
    ThreadPredictionUpdatedNotification,
    ThreadPredictionUpdatedServerNotification,
    TurnCompletedNotification,
    TurnStatus,
)
from openai_codex.models import UnknownNotification
from pydantic import ValidationError

from netizen_cli.error_messages import describe_error, native_turn_failure
from netizen_cli.turn_activity import (
    TurnActivityNotificationProjection,
    project_turn_activity_notification,
)


class CodexSdkProtocolRegressionTest(unittest.TestCase):
    """Exercise the installed parser/router without starting a transport or model."""

    def prepare_turn(self, client: CodexClient, turn_id: str):
        router = client._router
        with router.pending_turn("thread-one") as cursors:
            subscription = router.prepare_turn(
                turn_id, "thread-one", cursors, for_handle=True,
            )
        self.assertIsNotNone(subscription)
        self.addCleanup(subscription.close)
        return subscription

    def test_forward_compatible_errors_keep_exact_turn_and_safe_failure_display(self) -> None:
        cases = (
            ("contextWindowExceeded", "contextWindowExceeded"),
            ("futureError", "futureError"),
            ("futureError Authorization: Bearer private-code", None),
            ({"futureError": {"arbitrary": "private-payload"}}, None),
            ({"httpConnectionFailed": {"httpStatusCode": "private-status"}}, None),
            ({"httpConnectionFailed": {"httpStatusCode": 503}}, "httpConnectionFailed, HTTP 503"),
        )
        for info, public_code in cases:
            with self.subTest(info=info):
                client = CodexClient(CodexConfig())
                failed = self.prepare_turn(client, "turn-failed")
                active = self.prepare_turn(client, "turn-active")
                error = {
                    "message": "upstream request failed",
                    "codexErrorInfo": info,
                    "additionalDetails": "private-request-details",
                }
                notification = client._coerce_notification("error", {
                    "threadId": "thread-one", "turnId": "turn-failed",
                    "willRetry": False, "error": error,
                })
                self.assertIsInstance(notification.payload, ErrorNotification)
                self.assertEqual(
                    notification.payload.error.codex_error_info.model_dump(mode="json", by_alias=True),
                    info,
                )
                client._router.route_notification(notification)
                self.assertEqual(failed._state.next_event, 1)
                self.assertIs(failed.next(), notification)
                self.assertFalse(failed._state.completed)
                self.assertEqual(active._state.next_event, 0)

                completed = client._coerce_notification("turn/completed", {
                    "threadId": "thread-one",
                    "turn": {"id": "turn-failed", "status": "failed", "items": [], "error": error},
                })
                self.assertIsInstance(completed.payload, TurnCompletedNotification)
                client._router.route_notification(completed)
                self.assertEqual(failed._state.next_event, 2)
                delivered = failed.next().payload
                self.assertEqual(delivered.turn.id, "turn-failed")
                self.assertIs(delivered.turn.status, TurnStatus.failed)
                self.assertTrue(failed._state.completed)
                self.assertEqual(active._state.next_event, 0)
                self.assertFalse(active._state.completed)
                self.assertTrue(client._router._global_notifications.empty())

                message = describe_error(native_turn_failure(delivered.turn.error))
                self.assertIn("upstream request failed", message)
                self.assertNotIn("private-", message)
                if public_code is None:
                    self.assertEqual(message, "upstream request failed")
                else:
                    self.assertIn(public_code, message)
                self.assertEqual(
                    project_turn_activity_notification(
                        completed, expected_thread_id="thread-one", expected_turn_id="turn-active",
                    ),
                    TurnActivityNotificationProjection(),
                )

    def test_error_fallback_still_rejects_non_string_non_object_values(self) -> None:
        client = CodexClient(CodexConfig())
        for info in (123, ["futureError"]):
            with self.subTest(info=info):
                with self.assertRaises(ValidationError):
                    CodexErrorInfo.model_validate(info)
                notification = client._coerce_notification("error", {
                    "threadId": "thread-one", "turnId": "turn-failed",
                    "willRetry": False, "error": {"message": "public reason", "codexErrorInfo": info},
                })
                self.assertIsInstance(notification.payload, UnknownNotification)
                # An invalid error payload must not silently acquire typed display authority.
                self.assertEqual(
                    project_turn_activity_notification(
                        notification, expected_thread_id="thread-one", expected_turn_id="turn-failed",
                    ),
                    TurnActivityNotificationProjection(),
                )

    def test_predictions_remain_global_without_turn_goal_or_activity_effects(self) -> None:
        cases = (
            ({"type": "completed", "text": "private prediction text"}, {"emittedAtMs": 1_791_417_600_123}),
            ({"type": "completed"}, {}),
            ({"type": "completed", "text": None}, {"emittedAtMs": None}),
            ({"type": "failed"}, {}),
        )
        for goal_owned in (False, True):
            for source_turn_id in ("turn-source", "turn-active"):
                for result, envelope_fields in cases:
                    with self.subTest(goal=goal_owned, source=source_turn_id, result=result):
                        client = CodexClient(CodexConfig())
                        router = client._router
                        source = self.prepare_turn(client, "turn-source")
                        active = self.prepare_turn(client, "turn-active")
                        goal = router.register_goal("thread-one") if goal_owned else None
                        if goal is not None:
                            router.route_notification(client._coerce_notification("turn/started", {
                                "threadId": "thread-one",
                                "turn": {"id": "turn-active", "status": "inProgress", "items": []},
                            }))
                            self.assertEqual(goal.current_turn(), "turn-active")
                            self.assertEqual(goal._notifications.qsize(), 1)
                        envelope = ServerNotification.model_validate({
                            "method": "thread/prediction/updated",
                            "params": {
                                "threadId": "thread-one", "sourceTurnId": source_turn_id, "result": result,
                            },
                            **envelope_fields,
                        }).root
                        self.assertIsInstance(envelope, ThreadPredictionUpdatedServerNotification)
                        self.assertEqual(envelope.emitted_at_ms, envelope_fields.get("emittedAtMs"))
                        notification = client._coerce_notification(
                            envelope.method, envelope.params.model_dump(mode="json", by_alias=True),
                        )
                        self.assertIsInstance(notification.payload, ThreadPredictionUpdatedNotification)
                        self.assertEqual(notification.payload.source_turn_id, source_turn_id)
                        if result["type"] == "completed":
                            self.assertEqual(notification.payload.result.root.text, result.get("text"))
                        router.route_notification(notification)
                        self.assertEqual(router._global_notifications.qsize(), 1)
                        self.assertIs(router.next_global_notification(), notification)
                        for subscription in (source, active):
                            self.assertEqual(subscription._state.next_event, 0)
                            self.assertFalse(subscription._state.completed)
                        if goal is not None:
                            self.assertEqual(goal.current_turn(), "turn-active")
                            self.assertEqual(goal._notifications.qsize(), 1)
                            self.assertFalse(goal.is_finished())
                        self.assertEqual(
                            project_turn_activity_notification(
                                notification, expected_thread_id="thread-one", expected_turn_id="turn-active",
                            ),
                            TurnActivityNotificationProjection(),
                        )

    def test_mcp_login_id_is_optional_and_preserved_without_account_login_routing(self) -> None:
        for login_fields in ({}, {"loginId": None}, {"loginId": "mcp-login-one"}):
            with self.subTest(login_fields=login_fields):
                response = McpServerOauthLoginResponse.model_validate({
                    "authorizationUrl": "https://example.invalid/oauth", **login_fields,
                })
                self.assertEqual(response.authorization_url, "https://example.invalid/oauth")
                self.assertEqual(response.login_id, login_fields.get("loginId"))
                self.assertEqual(
                    McpServerOauthLoginResponse.model_validate_json(
                        response.model_dump_json(by_alias=True),
                    ).login_id,
                    response.login_id,
                )
                client = CodexClient(CodexConfig())
                client._router.register_login("mcp-login-one")
                notification = client._coerce_notification("mcpServer/oauthLogin/completed", {
                    "name": "test-mcp", "success": True, **login_fields,
                })
                self.assertIsInstance(notification.payload, McpServerOauthLoginCompletedNotification)
                self.assertEqual(notification.payload.login_id, response.login_id)
                self.assertTrue(notification.payload.success)
                client._router.route_notification(notification)
                self.assertEqual(client._router._global_notifications.qsize(), 1)
                self.assertIs(client._router.next_global_notification(), notification)
                self.assertTrue(client._router._login_notifications["mcp-login-one"].empty())


if __name__ == "__main__":
    unittest.main()
