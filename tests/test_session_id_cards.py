from __future__ import annotations

import json
import unittest

from netizen_cli.cards import (
    ArchivedSessionCardItem,
    SessionCardItem,
    archived_sessions_card,
    sessions_card,
)
from netizen_cli.domain import FeishuScope, ScopeKind
from support.channel_cards import _elements


class SessionIdentityCardTest(unittest.TestCase):
    scope = FeishuScope("cli_test", "oc_group", ScopeKind.TOPIC, "omt_topic")
    binding_id = "11111111-1111-4111-8111-111111111111"
    native_id = "019ab839-6ea8-733c-93ad-d2946375446c"

    def test_background_list_exposes_complete_native_id_without_an_id_action(self):
        card = sessions_card(
            scope=self.scope,
            sessions=(SessionCardItem(
                binding_id=self.binding_id,
                short_id=self.binding_id[:8],
                native_thread_id=self.native_id,
                title="Background task",
                project_alias="test",
                state="idle",
                active=False,
                catalog_unconfirmed=True,
            ),),
            total_count=1,
            active_binding_id="22222222-2222-4222-8222-222222222222",
            native_delete_available=True,
        ).card

        visible = "\n".join(item["content"] for item in _elements(card, "plain_text"))
        self.assertIn(f"Codex 会话 ID（跨会话工具）：{self.native_id}", visible)
        self.assertIn(f"Netizen 会话编号（/resume）：{self.binding_id[:8]}", visible)
        self.assertIn("归档状态：未确认", visible)
        self.assertNotIn("尚未生成", visible)
        self.assertNotIn("● 当前", visible)
        buttons = _elements(card, "button")
        self.assertEqual(
            [item["text"]["content"] for item in buttons],
            ["设为当前", "归档", "删除"],
        )
        for button in buttons:
            self.assertEqual(
                button["behaviors"][0]["value"]["binding_id"],
                f"binding:v1:{self.binding_id}",
            )

    def test_archived_id_is_visible_without_restoring_and_actions_keep_binding(self):
        card = archived_sessions_card(
            scope=self.scope,
            sessions=(ArchivedSessionCardItem(
                binding_id=self.binding_id,
                short_id=self.binding_id[:8],
                native_thread_id=self.native_id,
                title="Archived task",
                project_alias="test",
            ),),
            native_delete_available=True,
        ).card

        visible = "\n".join(item["content"] for item in _elements(card, "plain_text"))
        self.assertIn(f"Codex 会话 ID（跨会话工具）：{self.native_id}", visible)
        buttons = _elements(card, "button")
        self.assertEqual(
            [item["text"]["content"] for item in buttons],
            ["恢复并切换", "删除"],
        )
        for button in buttons:
            self.assertEqual(
                button["behaviors"][0]["value"]["binding_id"],
                f"binding:v1:{self.binding_id}",
            )
        self.assertEqual(
            buttons[-1]["behaviors"][0]["value"]["expected_native_thread_id"],
            f"native-thread:v1:{self.native_id}",
        )

    def test_lazy_list_displays_absent_native_id_without_substituting_binding(self):
        card = sessions_card(
            scope=self.scope,
            sessions=(SessionCardItem(
                binding_id=self.binding_id,
                short_id=self.binding_id[:8],
                native_thread_id=None,
                title="New task",
                project_alias="test",
                state="idle",
                active=True,
            ),),
            total_count=1,
            active_binding_id=self.binding_id,
            native_delete_available=True,
        ).card
        visible = "\n".join(item["content"] for item in _elements(card, "plain_text"))
        self.assertIn("Codex 会话 ID（跨会话工具）：尚未生成，首条消息后创建", visible)
        self.assertIn(f"Netizen 会话编号（/resume）：{self.binding_id[:8]}", visible)
        self.assertEqual(
            [item["text"]["content"] for item in _elements(card, "button")],
            ["删除"],
        )

    def test_full_page_with_complete_ids_and_controls_fits_card_budget(self):
        sessions = tuple(SessionCardItem(
            binding_id=f"{index:08d}-1111-4111-8111-111111111111",
            short_id=f"{index:08d}",
            native_thread_id=f"{index:08d}-6ea8-733c-93ad-d2946375446c",
            title="名" * 120,
            project_alias="project-alias" * 5,
            state="turn-observation-unavailable",
            active=False,
            activity_revision=100,
            turn_id="019ab839-0001-733c-93ad-d2946375446c",
        ) for index in range(10))
        card = sessions_card(
            scope=self.scope,
            sessions=sessions,
            total_count=30,
            page=1,
            active_binding_id=self.binding_id,
            native_delete_available=True,
        ).card
        self.assertLess(len(json.dumps(card, ensure_ascii=False).encode("utf-8")), 55_000)
        visible = "\n".join(item["content"] for item in _elements(card, "plain_text"))
        for session in sessions:
            self.assertIn(session.native_thread_id, visible)


if __name__ == "__main__":
    unittest.main()
