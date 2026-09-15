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
    ACTIVITY_OPERATION_TEXT_LIMIT,
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
                    "aggregatedOutput": "private command output",
                },
                TurnActivityKind.COMMAND,
                1,
                "执行命令 · cat /Users/user/private.txt",
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
                    "query": "Codex SDK",
                    "results": [{"text": "private search results"}],
                },
                TurnActivityKind.WEB_SEARCH,
                1,
                "搜索网页 · Codex SDK",
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
            "private command output",
            "private search results",
            "private prompt",
            "private-server",
            "do-not-show",
            "private-thread",
        ):
            self.assertNotIn(forbidden, safe)

    def test_command_summaries_use_native_details_with_raw_command_fallback(self) -> None:
        fallback = "执行命令 · pytest tests/test_forms.py -q"
        cases = (
            ([], fallback),
            (
                [
                    {
                        "type": "read",
                        "command": "cat /Users/user/private.txt",
                        "name": "private.txt",
                        "path": "/Users/user/private.txt",
                    }
                ],
                "读取文件 · /Users/user/private.txt",
            ),
            (
                [
                    {
                        "type": "listFiles",
                        "command": "find /Users/user/private",
                        "path": "/Users/user/private",
                    }
                ],
                "列出文件 · /Users/user/private",
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
                "搜索内容 · secret · /Users/user/private",
            ),
            ([{"type": "unknown", "command": "another action command"}], fallback),
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
                fallback,
            ),
            (
                [{"type": "read", "command": "cat x", "name": "x", "path": ""}],
                fallback,
            ),
            ([{"type": "listFiles", "command": "ls"}], fallback),
            (
                [{"type": "search", "command": "rg reset", "query": "reset"}],
                "搜索内容 · reset",
            ),
            (
                [{"type": "search", "command": "rg reset src", "path": "src"}],
                "搜索内容 · src",
            ),
            ([{"type": "search", "command": "rg reset"}], fallback),
        )
        for index, (actions, expected) in enumerate(cases):
            with self.subTest(expected=expected):
                event = _project_item(
                    {
                        "type": "commandExecution",
                        "id": f"command-{index}",
                        "command": "pytest tests/test_forms.py -q",
                        "commandActions": actions,
                        "cwd": "/Users/user/private",
                        "status": "inProgress",
                    }
                )
                assert event is not None
                self.assertEqual(event.text, expected)
                self.assertEqual(event.item_id, f"command-{index}")
                self.assertIs(event.status, TurnActivityStatus.IN_PROGRESS)
                self.assertNotIn("another action command", repr(event))

    def test_command_preview_is_bounded_and_preserves_native_exit_code(self) -> None:
        for exit_code in (None, 0, 1, -9):
            with self.subTest(exit_code=exit_code):
                event = _project_item(
                    {
                        "type": "commandExecution",
                        "id": "command-long",
                        "command": "pytest " + "tests/test_form.py " * 30,
                        "commandActions": [],
                        "cwd": "/tmp",
                        "status": "completed",
                        "exitCode": exit_code,
                        "aggregatedOutput": "FAILURE must not infer a failed status",
                    },
                    completed=True,
                    completed_at_ms=1234,
                )
                assert event is not None and event.text is not None
                self.assertLessEqual(len(event.text), ACTIVITY_OPERATION_TEXT_LIMIT)
                self.assertIn("…", event.text)
                self.assertTrue(event.text.startswith("执行命令 · pytest "))
                if exit_code:
                    self.assertTrue(event.text.endswith(f" · 退出码 {exit_code}"))
                else:
                    self.assertNotIn("退出码", event.text)
                self.assertEqual(event.item_id, "command-long")
                self.assertEqual(event.event_timestamp_ms, 1234)
                self.assertIs(event.status, TurnActivityStatus.COMPLETED)
                self.assertNotIn("FAILURE", repr(event))

    def test_command_details_preserve_literal_time_and_progress_terms(self) -> None:
        command = "rg 'ETA|elapsed|73%' tests/test_timing.py"
        event = _project_item(
            {
                "type": "commandExecution",
                "id": "literal-search",
                "command": command,
                "commandActions": [],
                "cwd": "/tmp",
                "status": "inProgress",
            }
        )
        assert event is not None
        self.assertEqual(event.text, f"执行命令 · {command}")

    def test_file_changes_show_native_kinds_and_paths_without_diffs(self) -> None:
        changes = [
            {"path": path, "kind": {"type": kind}, "diff": "private file diff"}
            for path, kind in (
                ("src/new.py", "add"),
                ("src/old.py", "delete"),
                ("src/form.py", "update"),
                ("src/omitted.py", "update"),
            )
        ]
        for count in (3, 4):
            with self.subTest(count=count):
                event = _project_item(
                    {
                        "type": "fileChange",
                        "id": "files-one",
                        "changes": changes[:count],
                        "status": "declined",
                    },
                    completed=True,
                )
                assert event is not None and event.text is not None
                expected = "修改文件 · 新增 src/new.py、删除 src/old.py、更新 src/form.py"
                if count == 3:
                    self.assertEqual(event.text, expected)
                else:
                    self.assertEqual(event.text, expected + "、…")
                    self.assertNotIn("src/omitted.py", event.text)
                self.assertIs(event.status, TurnActivityStatus.DECLINED)
                self.assertEqual(event.count, count)
                self.assertLessEqual(len(event.text), ACTIVITY_OPERATION_TEXT_LIMIT)
                self.assertNotIn("private file diff", repr(event))

    def test_web_actions_show_only_native_query_url_and_pattern(self) -> None:
        cases = (
            (None, "搜索网页 · fallback query"),
            ({"type": "search", "query": "Codex SDK"}, "搜索网页 · Codex SDK"),
            (
                {
                    "type": "search",
                    "queries": ["Codex SDK", "Turn events"],
                    "query": "unused singular query",
                },
                "搜索网页 · Codex SDK、Turn events",
            ),
            (
                {"type": "search", "queries": ["one", "two", "three", "four"]},
                "搜索网页 · one、two、three、…",
            ),
            (
                {"type": "search", "queries": [], "query": "singular query"},
                "搜索网页 · singular query",
            ),
            ({"type": "search"}, "搜索网页 · fallback query"),
            (
                {"type": "openPage", "url": "https://example.com/docs"},
                "打开网页 · https://example.com/docs",
            ),
            (
                {
                    "type": "findInPage",
                    "pattern": "CommandExecution",
                    "url": "https://example.com/docs",
                },
                "查找网页内容 · CommandExecution · https://example.com/docs",
            ),
            ({"type": "openPage"}, "打开网页 · fallback query"),
            ({"type": "findInPage"}, "查找网页内容 · fallback query"),
            ({"type": "other"}, "搜索网页 · fallback query"),
        )
        for action, expected in cases:
            with self.subTest(action=action):
                event = _project_item(
                    {
                        "type": "webSearch",
                        "id": "web-one",
                        "action": action,
                        "query": "fallback query",
                        "results": [{"text": "private page content"}],
                    },
                    completed=True,
                )
                assert event is not None
                self.assertEqual(event.text, expected)
                self.assertNotIn("private page content", repr(event))

    def test_detailed_operations_are_bounded_and_redact_explicit_credentials(self) -> None:
        for detail, expected in (
            ("src/" + "a" * 200, None),
            ("https://user:credential-value@example.com/docs", "credential-value"),
            ("api_key=credential-value", "credential-value"),
            ('{"api_key": "credential-value"}', "credential-value"),
            ("curl --password credential-value", "credential-value"),
            ("client --token credential-value", "credential-value"),
            ("https://example.com/?token=credential-value", "credential-value"),
            ("https://example.com/?key=credential-value", "credential-value"),
            ("https://example.com/?signature=credential-value", "credential-value"),
            ("a" * 200 + " --password credential-value", "credential-value"),
            ("sk-" + "a" * 40, "sk-"),
        ):
            for payload in (
                {
                    "type": "commandExecution",
                    "id": "operation",
                    "command": detail,
                    "commandActions": [],
                    "cwd": "/tmp",
                    "status": "inProgress",
                },
                {
                    "type": "fileChange",
                    "id": "operation",
                    "changes": [{"path": detail, "kind": {"type": "add"}, "diff": ""}],
                    "status": "inProgress",
                },
                {"type": "webSearch", "id": "operation", "query": detail},
            ):
                with self.subTest(detail=detail, kind=payload["type"]):
                    event = _project_item(payload)
                    assert event is not None and event.text is not None
                    self.assertLessEqual(len(event.text), ACTIVITY_OPERATION_TEXT_LIMIT)
                    if expected is None:
                        self.assertEqual(len(event.text), 120)
                        self.assertIn("…", event.text)
                    else:
                        self.assertNotIn(expected, event.text)
                        self.assertIn("[敏感内容已隐藏]", event.text)

    def test_new_collab_tools_preserve_safe_operation_status_and_counts(self) -> None:
        for tool in ("sendMessage", "followupTask", "interruptAgent", "listAgents"):
            for native_status, expected_status in (
                ("inProgress", TurnActivityStatus.IN_PROGRESS),
                ("completed", TurnActivityStatus.COMPLETED),
                ("failed", TurnActivityStatus.FAILED),
                ("interrupted", TurnActivityStatus.INTERRUPTED),
            ):
                with self.subTest(tool=tool, status=native_status):
                    event = _project_item(
                        {
                            "type": "collabAgentToolCall",
                            "id": "operation",
                            "agentsStates": {},
                            "prompt": "private message or task",
                            "receiverThreadIds": (
                                [] if tool == "listAgents" else ["private-child"]
                            ),
                            "senderThreadId": "private-parent",
                            "status": native_status,
                            "tool": tool,
                        },
                        completed=native_status != "inProgress",
                    )
                    assert event is not None
                    self.assertIs(event.kind, TurnActivityKind.SUBAGENT)
                    self.assertIs(event.status, expected_status)
                    self.assertEqual(event.count, 1)
                    self.assertIsNone(event.text)
                    self.assertNotIn("private", repr(event))

    def test_child_completion_and_interruption_are_terminal_in_both_envelopes(
        self,
    ) -> None:
        for kind, status in (
            ("completed", TurnActivityStatus.COMPLETED),
            ("interrupted", TurnActivityStatus.INTERRUPTED),
        ):
            for completed in (False, True):
                with self.subTest(kind=kind, completed=completed):
                    event = _project_item(
                        {
                            "type": "subAgentActivity",
                            "id": "activity",
                            "agentThreadId": "private-child",
                            "agentPath": "/root/private-child",
                            "kind": kind,
                        },
                        completed=completed,
                        started_at_ms=100,
                        completed_at_ms=101,
                    )
                    assert event is not None
                    self.assertIs(event.kind, TurnActivityKind.SUBAGENT)
                    self.assertIs(event.status, status)
                    self.assertEqual(event.count, 1)
                    self.assertEqual(event.event_timestamp_ms, 101 if completed else 100)
                    self.assertIsNone(event.text)
                    self.assertNotIn("private", repr(event))

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
        self.assertIn("`secret --flag`", commentary.text or "")
        self.assertNotIn("ETA", commentary.text or "")
        self.assertIn("user@example.com", commentary.text or "")
        self.assertIsNone(final_answer)

    def test_reasoning_content_and_summary_are_not_projected(self) -> None:
        self.assertIsNone(
            _project_item(
                {
                    "type": "reasoning",
                    "id": "reasoning-one",
                    "content": ["private reasoning"],
                    "summary": ["private reasoning summary"],
                },
                completed=True,
            )
        )

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
        self.assertEqual(len(value), 160)
        self.assertNotIn("18m", value)
        self.assertNotIn("ETA", value)
        self.assertNotIn("73%", value)
        self.assertIn("https://private.example/x", value)
        self.assertIn("/etc/private.conf", value)
        self.assertIn("src/private/file.py", value)

    def test_text_sanitization_keeps_normal_working_information(self) -> None:
        for value in (
            "已复现 `TypeError`，检查 /Users/user/app.py 和 src/forms.py。",
            "修复 `elapsed` 字段并检查 tests/test_forms.py",
            "读取 /tmp/ETA.md 并继续修复",
            "参考 https://example.com/ETA 和 src/elapsed.py",
            "参考 https://example.com/docs，联系 user@example.com。",
            "提交 " + "abcdef0123456789" * 3,
            "读取 ~/project/form.py 和 C:\\Users\\user\\form.py。",
        ):
            with self.subTest(value=value):
                self.assertEqual(sanitize_activity_text(value), value)

    def test_time_estimate_filter_keeps_the_following_working_information(self) -> None:
        self.assertEqual(
            sanitize_activity_text("本步耗时 18m 继续检查 tests/test_forms.py"),
            "本步[时间信息已隐藏] 继续检查 tests/test_forms.py",
        )
        self.assertEqual(
            sanitize_activity_text("ETA：5m，正在修复 `TypeError`"),
            "[时间估算已隐藏]，正在修复 `TypeError`",
        )

    def test_text_sanitization_still_hides_explicit_credentials(self) -> None:
        for value in (
            "password: credential-value",
            "Bearer credential-value",
            "https://user:credential-value@example.com/docs",
            "-----BEGIN PRIVATE KEY-----\ncredential-value",
            "api_key=credential-value",
            '{"api_key": "credential-value"}',
            "curl --password credential-value",
            "client --token credential-value",
            "https://example.com/?token=credential-value",
            "https://example.com/?key=credential-value",
            "https://example.com/?signature=credential-value",
            "a" * 200 + " password: credential-value",
            "a" * 200 + " --token credential-value",
        ):
            with self.subTest(value=value):
                self.assertEqual(sanitize_activity_text(value), "[敏感内容已隐藏]")
        for token in ("sk-" + "a" * 40, "ghp_" + "a" * 40, "AKIA" + "A" * 16):
            with self.subTest(token=token):
                self.assertEqual(
                    sanitize_activity_text(f"credential {token}"),
                    "credential [敏感内容已隐藏]",
                )

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

    def test_plan_projection_preserves_details_but_redacts_credentials(self) -> None:
        steps = project_plan_steps(
            [
                TurnPlanStep(
                    step="Inspect https://private.example/x and /etc/private.conf",
                    status=TurnPlanStepStatus.in_progress,
                ),
                TurnPlanStep(
                    step="Run request with api_key=credential-value",
                    status=TurnPlanStepStatus.pending,
                ),
            ]
        )

        self.assertEqual(
            steps[0].step,
            "Inspect https://private.example/x and /etc/private.conf",
        )
        self.assertEqual(steps[1].step, "[敏感内容已隐藏]")


if __name__ == "__main__":
    unittest.main()
