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
    started_at_ms: int = 1,
    completed_at_ms: int = 2,
):
    item = ThreadItem.model_validate(value)
    if completed:
        payload = ItemCompletedNotification(
            completedAtMs=completed_at_ms,
            item=item,
            threadId="thread-one",
            turnId="turn-one",
        )
        method = "item/completed"
    else:
        payload = ItemStartedNotification(
            startedAtMs=started_at_ms,
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
    def test_allowlisted_operations_expose_only_approved_details(self) -> None:
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
                None,
            ),
            (
                {
                    "type": "mcpToolCall",
                    "id": "mcp-one",
                    "arguments": {"secret": "do-not-show"},
                    "server": "private-server",
                    "status": "inProgress",
                    "tool": "mcp-tool",
                },
                TurnActivityKind.TOOL,
                1,
                "mcp-tool",
            ),
            (
                {
                    "type": "dynamicToolCall",
                    "id": "dynamic-one",
                    "arguments": {"path": "/Users/user/private.txt"},
                    "namespace": "workspace",
                    "status": "completed",
                    "tool": "dynamic-tool",
                },
                TurnActivityKind.TOOL,
                1,
                "workspace.dynamic-tool",
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
                None,
            ),
            (
                {
                    "type": "webSearch",
                    "id": "search-one",
                    "query": "private search terms",
                },
                TurnActivityKind.WEB_SEARCH,
                1,
                None,
            ),
            (
                {
                    "type": "imageView",
                    "id": "image-view-one",
                    "path": "/Users/user/private.png",
                },
                TurnActivityKind.IMAGE,
                1,
                None,
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
                None,
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
                None,
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
                None,
            ),
            (
                {
                    "type": "enteredReviewMode",
                    "id": "review-one",
                    "review": "private review",
                },
                TurnActivityKind.REVIEW,
                1,
                None,
            ),
            (
                {
                    "type": "contextCompaction",
                    "id": "compact-one",
                },
                TurnActivityKind.COMPACTION,
                1,
                None,
            ),
        )

        projected = []
        for payload, kind, count, text in cases:
            event = _project_item(payload)
            assert event is not None
            self.assertIs(event.kind, kind)
            self.assertEqual(event.count, count)
            self.assertEqual(event.event_timestamp_ms, 1)
            self.assertEqual(event.text, text)
            projected.append(event)

        safe = repr(projected)
        for forbidden in (
            "private search terms",
            "private prompt",
            "private-server",
            "/Users/user",
            "do-not-show",
            "private-thread",
        ):
            self.assertNotIn(forbidden, safe)

    def test_command_actions_map_without_exposing_command_details(self) -> None:
        cases = (
            ([], None),
            (
                [
                    {
                        "type": "read",
                        "command": "cat /Users/user/private.txt",
                        "name": "private.txt",
                        "path": "/Users/user/private.txt",
                    }
                ],
                "读取文件",
            ),
            (
                [
                    {
                        "type": "listFiles",
                        "command": "find /Users/user/private",
                        "path": "/Users/user/private",
                    }
                ],
                "列出文件",
            ),
            (
                [
                    {
                        "type": "search",
                        "command": "rg secret /Users/user/private",
                        "path": "/Users/user/private",
                        "query": "secret",
                    }
                ],
                "搜索内容",
            ),
            ([{"type": "unknown", "command": "printenv SECRET"}], None),
            (
                [
                    {
                        "type": "read",
                        "command": "cat first",
                        "name": "first",
                        "path": "first",
                    },
                    {
                        "type": "read",
                        "command": "cat second",
                        "name": "second",
                        "path": "second",
                    },
                ],
                "执行复合命令",
            ),
        )
        for index, (actions, expected) in enumerate(cases):
            with self.subTest(expected=expected):
                event = _project_item(
                    {
                        "type": "commandExecution",
                        "id": f"command-{index}",
                        "command": "raw command must not appear",
                        "commandActions": actions,
                        "cwd": "/Users/user/private",
                        "status": "inProgress",
                    }
                )
                assert event is not None
                self.assertEqual(event.text, expected)
                self.assertNotIn("raw command", repr(event))
                self.assertNotIn("/Users/user", repr(event))

    def test_tool_names_are_direct_and_not_subject_to_commentary_limit(self) -> None:
        long_name = "tool_" + "x" * (ACTIVITY_TEXT_LIMIT + 40)
        mcp = _project_item(
            {
                "type": "mcpToolCall",
                "id": "mcp-long",
                "arguments": {"secret": "hidden"},
                "server": "hidden-server",
                "status": "inProgress",
                "tool": long_name,
            }
        )
        dynamic = _project_item(
            {
                "type": "dynamicToolCall",
                "id": "dynamic-empty-namespace",
                "arguments": {},
                "namespace": "",
                "status": "inProgress",
                "tool": "plain-tool",
            }
        )

        assert mcp is not None and dynamic is not None
        self.assertEqual(mcp.text, long_name)
        self.assertEqual(dynamic.text, "plain-tool")

    def test_item_lifecycle_uses_exact_native_event_timestamp(self) -> None:
        payload = {
            "type": "commandExecution",
            "id": "command-time",
            "command": "true",
            "commandActions": [],
            "cwd": "/tmp",
            "status": "completed",
        }
        started = _project_item(payload, started_at_ms=1_788_329_220_001)
        completed = _project_item(
            payload,
            completed=True,
            completed_at_ms=1_788_329_229_999,
        )

        assert started is not None and completed is not None
        self.assertEqual(started.event_timestamp_ms, 1_788_329_220_001)
        self.assertEqual(completed.event_timestamp_ms, 1_788_329_229_999)

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
        self.assertEqual(commentary.event_timestamp_ms, 2)
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

    def test_text_sanitization_preserves_layout_and_replaces_other_controls(
        self,
    ) -> None:
        value = sanitize_activity_text(
            "第一行\r\n\r第二行\t缩进\x00\x1b\b末尾"
        )

        self.assertEqual(
            value,
            "第一行\n\n第二行    缩进���末尾",
        )
        self.assertIsNone(sanitize_activity_text("\r\n\t"))
        self.assertEqual(
            sanitize_activity_text("第一行\npassword: secret-value\n第三行"),
            "[敏感内容已隐藏]",
        )

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
