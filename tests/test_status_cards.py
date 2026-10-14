from __future__ import annotations

import copy
import json
import unittest

from netizen_cli.cards.callbacks import CardActionError
from netizen_cli.cards.status import decode_status_command, session_status_card, side_status_card
from netizen_cli.domain import ControlName, FeishuScope, ScopeKind
from netizen_cli.channel_app import _normalized_thread_preview
from support.channel_cards import _elements


def _session_card(**overrides):
    values = {
        "scope": FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT),
        "binding_id": "12345678-full-binding-id",
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
    def test_snapshot_keeps_technical_details_collapsed_and_exposes_command_aliases(self):
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
        buttons = _elements(card, "button")
        self.assertEqual([button["text"]["content"] for button in buttons], ["查看状态", "停止", "会话配置", "会话列表"])
        for button, command in zip(buttons, (ControlName.STATUS, ControlName.STOP, ControlName.CONFIG, ControlName.SESSIONS)):
            decoded = decode_status_command(
                scope=FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT),
                message_id="om_status", sender_id="ou_clicker",
                value=json.loads(json.dumps(button["behaviors"][0]["value"])),
            )
            self.assertEqual(decoded.target_id, "12345678-full-binding-id")
            self.assertEqual(decoded.command.name, command)
            self.assertEqual(decoded.command.sender_id, "ou_clicker")
            self.assertEqual(decoded.command.arguments, ())
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
                    scope=FeishuScope("cli_test", "oc_direct", ScopeKind.TOPIC, "omt_side"),
                    side_id="side-one-full",
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
                buttons = _elements(card, "button")
                self.assertEqual([button["text"]["content"] for button in buttons], ["查看状态", "停止", "结束临时会话"])
                close = decode_status_command(
                    scope=FeishuScope("cli_test", "oc_direct", ScopeKind.TOPIC, "omt_side"),
                    message_id="om_status", sender_id="ou_clicker",
                    value=buttons[-1]["behaviors"][0]["value"],
                )
                self.assertEqual(close.command.name, ControlName.SIDE)
                self.assertEqual(close.command.arguments, ("close",))

    def test_renewal_changes_only_transport_nonces(self):
        card = _session_card()
        value = _elements(card, "button")[0]["behaviors"][0]["value"]
        decoded = decode_status_command(
            scope=FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT),
            message_id="om_status", sender_id="ou_clicker", value=value,
        )
        renewed = decoded.card().card
        for old, new in zip(_elements(card, "button"), _elements(renewed, "button")):
            self.assertNotEqual(old["behaviors"][0]["value"].pop("nonce"), new["behaviors"][0]["value"].pop("nonce"))
        self.assertEqual(card, renewed)

    def test_decoder_rejects_unknown_commands_scope_and_untyped_snapshots(self):
        value = _elements(_session_card(), "button")[0]["behaviors"][0]["value"]
        invalid = [
            {"v": True}, {"v": 2}, {"command": "delete"}, {"target_kind": "thread"},
            {"scope": "other-app-or-topic"}, {"target_id": ""}, {"turn_id": "turn-old"},
            {"arguments": ["arbitrary"]}, {"snapshot": {}},
            {"snapshot": {**value["snapshot"], "metadata_lines": ["missing preview"]}},
            {"snapshot": {**value["snapshot"], "state": {"tag": "button"}}},
            {"snapshot": {**value["snapshot"], "progress_lines": ["x"] * 33}},
        ]
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(CardActionError):
                decode_status_command(
                    scope=FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT),
                    message_id="om_status", sender_id="ou_clicker", value={**copy.deepcopy(value), **change},
                )

    def test_complete_card_capacity_counts_repeated_non_ascii_snapshot(self):
        with self.assertRaisesRegex(CardActionError, "内容过长"):
            _session_card(progress_lines=("进展" * 10_000,))


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
