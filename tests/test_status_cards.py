from __future__ import annotations

import unittest

from netizen_cli.cards.status import session_status_card, side_status_card
from netizen_cli.channel_app import _normalized_thread_preview
from support.channel_cards import _elements


def _session_card(**overrides):
    values = {
        "short_id": "12345678",
        "project_alias": "test",
        "native_thread_id": "01a0f202-31e1-7f62-81c9-3db3ee56ff04",
        "state": "idle",
        "metadata_lines": ("名称：打招呼", "会话预览：哈喽"),
        "git_branch_lines": ("Git Branch：main...origin/main [ahead 2]",),
        "context_window_line": "上下文窗口：24,500 / 100,000 tokens（24.5% 已用）",
        "context_mode_lines": (),
        "model_lines": ("Model：gpt-6.1-sol", "Effort：low", "Speed：Standard", "配置来源：Netizen 会话配置"),
        "progress_lines": (),
        "subscription_line": "Netizen 订阅：本进程未订阅（服务重启不会重建空闲计时）",
    }
    values.update(overrides)
    return session_status_card(**values).card


class StatusCardTest(unittest.TestCase):
    def test_snapshot_keeps_technical_details_collapsed_and_has_no_actions(self):
        card = _session_card()
        self.assertEqual(card["schema"], "2.0")
        panel, = _elements(card, "collapsible_panel")
        self.assertFalse(panel["expanded"])
        details = "\n".join(item["content"] for item in _elements(panel, "plain_text"))
        self.assertIn("会话：12345678", details)
        self.assertIn("Native Thread：01a0f202-31e1-7f62-81c9-3db3ee56ff04", details)
        self.assertIn("本进程未订阅", details)
        self.assertNotIn("24,500", details)
        self.assertNotIn("Model：", details)
        self.assertEqual(_elements(card, "button"), [])
        self.assertEqual(_elements(card, "form"), [])

    def test_dynamic_fields_are_inert_plain_text_including_plans_and_settings(self):
        literal = '<at id="all">`[link](https://example.com)`</at>'
        card = _session_card(
            project_alias=literal,
            metadata_lines=(f"名称：{literal}", f"会话预览：{literal}"),
            git_branch_lines=(f"Git Branch：{literal}",),
            context_mode_lines=(f"@ 时读取的消息范围：{literal}",),
            model_lines=(f"Model：{literal}", f"Effort：{literal}", f"Speed：{literal}", "配置来源：Codex"),
            progress_lines=("任务进展", "已接收调整：1 次", f"→ {literal}"),
        )
        for item in _elements(card, "markdown"):
            self.assertNotIn(literal, item["content"])
        plain = "\n".join(item["content"] for item in _elements(card, "plain_text"))
        for label in ("名称", "会话预览", "Project", "Git Branch", "Model", "Effort", "Speed"):
            self.assertIn(f"{label}：{literal}", plain)
        self.assertIn(f"→ {literal}", plain)
        self.assertIn(f"@ 时读取的消息范围：{literal}", plain)

    def test_state_color_does_not_imply_unknown_or_running_is_complete(self):
        for state, color in (
            ("idle", "green"), ("running", "blue"), ("compacting", "blue"),
            ("goal-running", "blue"), ("goal-complete", "green"),
            ("goal-paused", "orange"), ("goal-unknown", "orange"),
            ("turn-observation-unavailable", "orange"), ("future-state", "orange"),
        ):
            with self.subTest(state=state):
                card = _session_card(state=state)
                self.assertEqual(card["header"]["template"], color)
                self.assertIn(f"状态：{state}", [item["content"] for item in _elements(card, "plain_text")])

    def test_side_keeps_lifecycle_separate_from_current_turn(self):
        for state, turn_state, color in (("open", "running", "blue"), ("open", "idle", "green"), ("failed", "idle", "red")):
            with self.subTest(state=state, turn_state=turn_state):
                card = side_status_card(
                    short_id="side-one", parent_short_id="parent-one", state=state,
                    turn_state=turn_state, git_branch_lines=(),
                ).card
                self.assertEqual(card["header"]["template"], color)
                plain = "\n".join(item["content"] for item in _elements(card, "plain_text"))
                self.assertIn(f"状态：{state}", plain)
                self.assertIn(f"当前 Turn：{turn_state}", plain)
                self.assertIn("Parent 会话：parent-one", plain)
                self.assertNotIn("Git Branch", plain)
                self.assertIn("运行中的 Turn 不计入空闲时间", plain)
                self.assertEqual(_elements(card, "button"), [])


class ThreadPreviewTest(unittest.TestCase):
    def test_other_known_source_trailers_are_removed_before_display_truncation(self):
        for tag, kind in (
            ("feishu_card_answer_context", "feishu_question_answer"),
            ("scheduled_plan", "scheduled_plan"),
        ):
            with self.subTest(tag=tag):
                text = f'  Read\n this\n\n<{tag}>\n{{"kind": "{kind}", "version":'
                self.assertEqual(_normalized_thread_preview(text, max_chars=12), "Read this")

    def test_empty_and_long_previews_keep_existing_display_bounds(self):
        self.assertIsNone(_normalized_thread_preview(None, max_chars=10))
        self.assertIsNone(_normalized_thread_preview(" \n ", max_chars=10))
        self.assertIsNone(_normalized_thread_preview(
            '<feishu_current_message_context> {"kind":"feishu_current_message"', max_chars=10,
        ))
        self.assertEqual(_normalized_thread_preview("x " * 100, max_chars=10), "x x x x x…")


if __name__ == "__main__":
    unittest.main()
