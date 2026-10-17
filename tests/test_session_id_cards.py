from __future__ import annotations

import json
import unittest
from dataclasses import replace

from netizen_cli.cards import (
    ArchivedSessionCardItem,
    CardActionError,
    SessionCardItem,
    archived_sessions_card,
    sessions_card,
)
from netizen_cli.channel.session_pages import SessionCardPage
from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.session_inventory import NativeSessionCursor
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
            active_binding_id="22222222-2222-4222-8222-222222222222",
            native_delete_available=True,
        ).card

        visible = "\n".join(item["content"] for item in _elements(card, "plain_text"))
        self.assertIn(f"Codex 会话 ID（跨会话工具）：{self.native_id}", visible)
        self.assertIn(f"Netizen 会话编号（/resume）：{self.binding_id[:8]}", visible)
        self.assertIn("归档状态：未确认", visible)
        self.assertNotIn("尚未生成", visible)
        self.assertNotIn("● 当前", visible)
        buttons = [item for item in _elements(card, "button")
                   if "binding_id" in item["behaviors"][0]["value"]]
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
        buttons = [item for item in _elements(card, "button")
                   if "binding_id" in item["behaviors"][0]["value"]]
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
            active_binding_id=self.binding_id,
            native_delete_available=True,
            lazy=True,
        ).card
        visible = "\n".join(item["content"] for item in _elements(card, "plain_text"))
        self.assertIn("Codex 会话 ID（跨会话工具）：尚未生成，首条消息后创建", visible)
        self.assertIn(f"Netizen 会话编号（/resume）：{self.binding_id[:8]}", visible)
        self.assertEqual(
            [item["text"]["content"] for item in _elements(card, "button")
             if "binding_id" in item["behaviors"][0]["value"]],
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
            page=1,
            active_binding_id=self.binding_id,
            native_delete_available=True,
        ).card
        self.assertLess(len(json.dumps(card, ensure_ascii=False).encode("utf-8")), 55_000)
        visible = "\n".join(item["content"] for item in _elements(card, "plain_text"))
        for session in sessions:
            self.assertIn(session.native_thread_id, visible)

    def test_session_lists_always_offer_admin_without_an_error(self):
        cards = (
            sessions_card(
                scope=self.scope, sessions=(), native_delete_available=True,
                active_binding_id=None,
            ),
            sessions_card(
                scope=self.scope, sessions=(), native_delete_available=True,
                active_binding_id=None, lazy=True,
            ),
            archived_sessions_card(
                scope=self.scope, sessions=(), native_delete_available=True,
            ),
        )
        for card in cards:
            visible = "\n".join(item["content"] for item in _elements(card.card, "markdown"))
            self.assertIn("Admin 会话列表", visible)
            self.assertIn("`/admin`", visible)

    def test_deep_page_retains_exact_row_positions_without_repeating_back_history(self):
        # Match production's 48-character title projection, with every control
        # available and a current session outside the ten native page rows.
        scope = FeishuScope("cli_" + "a" * 16, "oc_" + "b" * 32, ScopeKind.TOPIC, "omt_" + "c" * 32)
        sessions = tuple(SessionCardItem(
            binding_id=f"{index:08d}-1111-4111-8111-111111111111",
            short_id=f"{index:08d}",
            native_thread_id=f"{index:08d}-6ea8-733c-93ad-d2946375446c",
            title="名" * 48,
            project_alias="project-alias" * 5,
            state="turn-observation-unavailable",
            active=False,
            activity_revision=100,
            turn_id="019ab839-0001-733c-93ad-d2946375446c",
        ) for index in range(10))
        current = replace(
            sessions[0], binding_id=self.binding_id, short_id=self.binding_id[:8],
            native_thread_id=self.native_id, active=True,
        )
        navigation = SessionCardPage()
        for index in range(8):
            navigation = navigation.next(NativeSessionCursor(f"{index}" + "x" * 64))
        token = navigation.encode(scope)
        self.assertGreater(len(token), 450)
        previous = navigation.previous().encode(scope)
        following = navigation.next(NativeSessionCursor("next" + "x" * 64)).encode(scope)
        card = sessions_card(
            scope=scope, sessions=sessions, current=current,
            active_binding_id=self.binding_id, native_delete_available=True,
            page=navigation.page, session_cursor=token,
            previous_cursor=previous, next_cursor=following,
        ).card
        self.assertLess(len(json.dumps(card, ensure_ascii=False).encode("utf-8")), 55_000)
        values = {"上一页": previous, "下一页": following}
        for button in _elements(card, "button"):
            value = button["behaviors"][0]["value"]
            if "binding_id" in value:
                decoded = SessionCardPage.decode(value["session_cursor"], scope)
                self.assertEqual(decoded.page, navigation.page)
                self.assertEqual(decoded.positions, (navigation.cursor,))
                self.assertIn(
                    value["binding_id"],
                    {f"binding:v1:{item.binding_id}" for item in (*sessions, current)},
                )
            elif button["text"]["content"] in values:
                self.assertEqual(value["session_cursor"], values[button["text"]["content"]])

        # A single oversized native cursor cannot be shortened or silently
        # discarded. Fail explicitly rather than send an invalid Feishu card.
        oversized = SessionCardPage("active", 1, (NativeSessionCursor("x" * 450),))
        with self.assertRaisesRegex(CardActionError, "卡片内容过长"):
            sessions_card(
                scope=scope, sessions=sessions, current=current,
                active_binding_id=self.binding_id, native_delete_available=True,
                page=1, session_cursor=oversized.encode(scope),
            )


if __name__ == "__main__":
    unittest.main()
