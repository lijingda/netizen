from __future__ import annotations

import unittest

from openai_codex.generated.v2_all import (
    ItemCompletedNotification,
    ItemStartedNotification,
    ThreadItem,
    TurnPlanStep,
    TurnPlanStepStatus,
)
from openai_codex.models import Notification, UnknownNotification

from netizen.turn_activity import (
    ACTIVITY_TEXT_LIMIT,
    TurnActivityKind,
    TurnActivityProjectionUnavailable,
    TurnActivityStatus,
    project_plan_steps,
    project_turn_activity_notification,
    sanitize_activity_text,
)


def _project_item(
    value: dict[str, object],
    *,
    completed: bool = False,
):
    item = ThreadItem.model_validate(value)
    if completed:
        payload = ItemCompletedNotification(
            completedAtMs=2,
            item=item,
            threadId="thread-one",
            turnId="turn-one",
        )
        method = "item/completed"
    else:
        payload = ItemStartedNotification(
            startedAtMs=1,
            item=item,
            threadId="thread-one",
            turnId="turn-one",
        )
        method = "item/started"
    return project_turn_activity_notification(
        Notification(method=method, payload=payload),
        expected_thread_id="thread-one",
        expected_turn_id="turn-one",
    ).event


class TurnActivityProjectionTest(unittest.TestCase):
    def test_allowlisted_operations_expose_only_safe_categories(self) -> None:
        cases = (
            (
                {
                    "type": "commandExecution",
                    "id": "command-one",
                    "command": "cat /Users/user/private.txt",
                    "commandActions": [],
                    "cwd": "/Users/user",
                    "status": "inProgress",
                },
                TurnActivityKind.COMMAND,
                1,
            ),
            (
                {
                    "type": "mcpToolCall",
                    "id": "mcp-one",
                    "arguments": {"secret": "do-not-show"},
                    "server": "private-server",
                    "status": "inProgress",
                    "tool": "private-tool",
                },
                TurnActivityKind.TOOL,
                1,
            ),
            (
                {
                    "type": "dynamicToolCall",
                    "id": "dynamic-one",
                    "arguments": {"path": "/Users/user/private.txt"},
                    "status": "completed",
                    "tool": "private-tool",
                },
                TurnActivityKind.TOOL,
                1,
            ),
            (
                {
                    "type": "fileChange",
                    "id": "file-one",
                    "changes": [],
                    "status": "completed",
                },
                TurnActivityKind.FILE_CHANGE,
                0,
            ),
            (
                {
                    "type": "webSearch",
                    "id": "search-one",
                    "query": "private search terms",
                },
                TurnActivityKind.WEB_SEARCH,
                1,
            ),
            (
                {
                    "type": "imageView",
                    "id": "image-view-one",
                    "path": "/Users/user/private.png",
                },
                TurnActivityKind.IMAGE,
                1,
            ),
            (
                {
                    "type": "imageGeneration",
                    "id": "image-generation-one",
                    "result": "private result",
                    "status": "completed",
                },
                TurnActivityKind.IMAGE,
                1,
            ),
            (
                {
                    "type": "collabAgentToolCall",
                    "id": "collab-one",
                    "agentsStates": {},
                    "prompt": "private prompt",
                    "receiverThreadIds": ["private-thread"],
                    "senderThreadId": "sender-thread",
                    "status": "inProgress",
                    "tool": "spawnAgent",
                },
                TurnActivityKind.SUBAGENT,
                1,
            ),
            (
                {
                    "type": "subAgentActivity",
                    "id": "subagent-one",
                    "agentPath": "private/path",
                    "agentThreadId": "private-thread",
                    "kind": "started",
                },
                TurnActivityKind.SUBAGENT,
                1,
            ),
            (
                {
                    "type": "enteredReviewMode",
                    "id": "review-one",
                    "review": "private review",
                },
                TurnActivityKind.REVIEW,
                1,
            ),
            (
                {
                    "type": "contextCompaction",
                    "id": "compact-one",
                },
                TurnActivityKind.COMPACTION,
                1,
            ),
        )

        projected = []
        for payload, kind, count in cases:
            event = _project_item(payload)
            assert event is not None
            self.assertIs(event.kind, kind)
            self.assertEqual(event.count, count)
            self.assertIsNone(event.text)
            projected.append(event)

        safe = repr(projected)
        for forbidden in (
            "private search terms",
            "private prompt",
            "private-server",
            "private-tool",
            "/Users/user",
            "do-not-show",
            "private-thread",
        ):
            self.assertNotIn(forbidden, safe)

    def test_completed_commentary_is_sanitized_and_final_answer_is_ignored(self) -> None:
        commentary = _project_item(
            {
                "type": "agentMessage",
                "id": "commentary-one",
                "phase": "commentary",
                "text": "Finished `secret --flag`; ETA 5m; key user@example.com",
            },
            completed=True,
        )
        final_answer = _project_item(
            {
                "type": "agentMessage",
                "id": "final-one",
                "phase": "final_answer",
                "text": "must stay in Result only",
            },
            completed=True,
        )

        assert commentary is not None
        self.assertIs(commentary.kind, TurnActivityKind.COMMENTARY)
        self.assertIs(commentary.status, TurnActivityStatus.COMPLETED)
        self.assertNotIn("secret", commentary.text or "")
        self.assertNotIn("ETA", commentary.text or "")
        self.assertNotIn("user@example.com", commentary.text or "")
        self.assertIsNone(final_answer)

    def test_unknown_notifications_are_ignored_but_allowlisted_shape_drift_fails(self) -> None:
        ignored = project_turn_activity_notification(
            Notification(
                method="item/agentMessage/delta",
                payload=UnknownNotification({"delta": "private reasoning"}),
            ),
            expected_thread_id="thread-one",
            expected_turn_id="turn-one",
        )
        self.assertIsNone(ignored.event)

        with self.assertRaises(TurnActivityProjectionUnavailable):
            project_turn_activity_notification(
                Notification(
                    method="item/completed",
                    payload=UnknownNotification({"item": {}}),
                ),
                expected_thread_id="thread-one",
                expected_turn_id="turn-one",
            )

    def test_text_sanitization_is_bounded_and_hides_time_and_progress(self) -> None:
        value = sanitize_activity_text(
            "Worked for 18m; ETA 2m; 73%; https://private.example/x; "
            "/etc/private.conf; src/private/file.py " + "x" * 500
        )
        assert value is not None
        self.assertLessEqual(len(value), ACTIVITY_TEXT_LIMIT)
        self.assertNotIn("18m", value)
        self.assertNotIn("ETA", value)
        self.assertNotIn("73%", value)
        self.assertNotIn("private.example", value)
        self.assertNotIn("/etc/private.conf", value)
        self.assertNotIn("src/private/file.py", value)

    def test_plan_projection_sanitizes_before_runtime_receives_it(self) -> None:
        steps = project_plan_steps(
            [
                TurnPlanStep(
                    step="Inspect https://private.example/x and /etc/private.conf",
                    status=TurnPlanStepStatus.in_progress,
                )
            ]
        )

        self.assertNotIn("private.example", steps[0].step)
        self.assertNotIn("/etc/private.conf", steps[0].step)


if __name__ == "__main__":
    unittest.main()
