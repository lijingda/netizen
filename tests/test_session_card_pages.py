from __future__ import annotations

import unittest
import uuid
from dataclasses import replace
from unittest.mock import patch

from netizen_cli.cards import CardActionError, decode_button_action
from netizen_cli.channel.session_pages import SessionCardPage
from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.management import ScopeSessionPage, SessionNativeCursor
from netizen_cli.codex_runtime import NativeThreadMetadata
from tests.support.channel_fixtures import channel_fixture
from tests.support.channel_cards import direct_button_event, _elements
from tests.support.channel_messages import FakeMessage


def action(card, label):
    value = card.card if hasattr(card, "card") else card
    return next(button["behaviors"][0]["value"] for button in _elements(value, "button")
                if button["text"]["content"] == label)


class SessionCardNavigationTest(unittest.TestCase):
    def setUp(self):
        self.scope = FeishuScope("cli_test", "chat", ScopeKind.GROUP)

    def test_navigation_is_scope_bound_and_has_bounded_back_history(self):
        page = SessionCardPage()
        for index in range(100):
            page = SessionCardPage.decode(page.next(SessionNativeCursor(str(index))).encode(self.scope), self.scope)
        self.assertEqual(page.page, 100)
        self.assertEqual(page.cursor, SessionNativeCursor("99"))
        self.assertLessEqual(len(page.positions), 9)
        self.assertLessEqual(len(page.encode(self.scope)), 1024)
        self.assertEqual(page.previous().cursor, SessionNativeCursor("98"))
        with self.assertRaisesRegex(ValueError, "分页"):
            SessionCardPage.decode(page.encode(self.scope), replace(self.scope, chat_id="other"))

    def test_invalid_cursor_and_page_mismatch_fail_before_query(self):
        value = {"v": 4, "intent": "sessions.page", "chat_id": "chat", "scope_kind": "group", "page": 1,
                 "session_cursor": SessionCardPage().encode(self.scope)}
        with self.assertRaises(CardActionError):
            decode_button_action(app_id="cli_test", message_id="message", callback_chat_id="chat",
                                 sender_id="user", tag="button", value=value)
        for raw in ("{}", "false", "[]", '[1,"key","active",0,[null]]'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                SessionCardPage.decode(raw, self.scope)

    def test_larger_navigation_preserves_all_eight_back_positions(self):
        page = SessionCardPage()
        for index in range(8):
            page = page.next(SessionNativeCursor(f"{index}" + "x" * 64, index))
        token = page.encode(self.scope)
        self.assertGreater(len(token), 512)
        self.assertEqual(SessionCardPage.decode(token, self.scope), page)

    def test_long_history_drops_oldest_positions_but_retains_current_cursor(self):
        page = SessionCardPage()
        for index in range(8):
            page = page.next(SessionNativeCursor(f"{index}" + "x" * 400, index))
        token = page.encode(self.scope)
        restored = SessionCardPage.decode(token, self.scope)
        self.assertLessEqual(len(token), 1024)
        self.assertEqual(restored.page, page.page)
        self.assertEqual(restored.positions, page.positions[-2:])


class SessionListApplicationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fx = await self.enterAsyncContext(channel_fixture())
        self.scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        self.fx.store._id_factory = lambda: str(uuid.uuid4())

    async def create(self, count, *, archived=False):
        bindings = []
        for index in range(count):
            await self.fx.new(message_id=f"create-{index}")
            binding = self.fx.store.active_binding(self.scope.key)
            thread_id = f"native-{index:03}"
            self.fx.store.assign_native_thread_id(binding.id, thread_id)
            binding = self.fx.store.get(binding.id)
            metadata = NativeThreadMetadata(thread_id, f"Task {index}", "task", updated_at=count-index)
            target = self.fx.runtime.archived_thread_metadata_values if archived else self.fx.runtime.thread_metadata_values
            target[thread_id] = metadata
            bindings.append(binding)
        return bindings

    async def test_pages_keep_native_tail_current_and_lazy_without_full_catalog(self):
        bindings = await self.create(15)
        # A current Lazy remains directly reachable, separate from native ordering.
        await self.fx.new(message_id="lazy")
        current = self.fx.store.active_binding(self.scope.key)
        with patch.object(self.fx.store, "list_bindings", side_effect=AssertionError("full inventory")):
            first = await self.fx.app._sessions_card(scope=self.scope)
            self.assertIn(current.short_id, str(first.card))
            self.assertIn("Task 0", str(first.card))
            self.assertNotIn("Task 10", str(first.card))
            next_value = action(first, "下一页")
            await self.fx.app.handle_card_action(direct_button_event(next_value, message_id="list"))
            second = self.fx.channel.updates[-1][1]
            self.assertIn("Task 10", str(second))
            self.assertNotIn("Task 0", str(second))
            self.assertNotIn("下一页", str(second))
            await self.fx.app.handle_card_action(direct_button_event(action(first, "未开始"), message_id="list"))
            lazy = self.fx.channel.updates[-1][1]
            self.assertIn(current.short_id, str(lazy))
            self.assertNotIn(bindings[0].short_id, str(lazy))
        self.assertEqual(self.fx.runtime.thread_metadata_calls, [])
        self.assertTrue(all(not call["archived"] for call in self.fx.runtime.thread_catalog_calls))
        self.assertEqual(len(self.fx.runtime.thread_catalog_calls), 2)

    async def test_archived_confirmation_returns_to_same_scope_and_page(self):
        from netizen_cli.domain import NativeCapability
        await self.create(12, archived=True)
        self.fx.runtime.available_capabilities = frozenset({NativeCapability.DELETE})
        first = await self.fx.app._archived_sessions_card(scope=self.scope)
        await self.fx.app.handle_card_action(direct_button_event(action(first, "下一页"), message_id="archived"))
        second = self.fx.channel.updates[-1][1]
        self.assertIn("Task 10", str(second))
        self.assertNotIn("Task 0", str(second))
        delete = action(second, "删除")
        await self.fx.app.handle_card_action(direct_button_event(delete, message_id="archived"))
        confirmation = self.fx.channel.updates[-1][1]
        back = action(confirmation, "返回归档列表")
        self.assertEqual(back["session_cursor"], delete["session_cursor"])
        await self.fx.app.handle_card_action(direct_button_event(back, message_id="archived"))
        self.assertIn("Task 10", str(self.fx.channel.updates[-1][1]))
        self.assertTrue(all(call["archived"] for call in self.fx.runtime.thread_catalog_calls))
        self.assertEqual(self.fx.runtime.thread_metadata_calls, [])

    async def test_old_numbered_pagination_is_explicitly_expired(self):
        with self.assertRaisesRegex(CardActionError, "过期"):
            await self.fx.app._sessions_card(scope=self.scope, page=99)
        self.assertEqual(self.fx.runtime.thread_catalog_calls, [])

    async def test_long_native_cursor_round_trips_through_next_page_callback(self):
        cursor = SessionNativeCursor("x" * 600, 5)
        first = ScopeSessionPage(
            items=(), next_cursor=cursor, current=None,
            active_binding_id=None, has_lazy=False,
        )
        following = replace(first, next_cursor=None)
        for state, command in (("active", "/sessions"), ("archived", "/sessions archived")):
            with self.subTest(state=state), patch.object(
                self.fx.app._management, "query_scope_sessions", side_effect=(first, following),
            ) as query:
                await self.fx.app.handle_message(FakeMessage(command, message_id=f"long-{state}"))
                card = self.fx.channel.replies[-1][1]
                await self.fx.app.handle_card_action(direct_button_event(
                    action(card, "下一页"), message_id=f"long-card-{state}",
                ))
                self.assertEqual(query.call_args.kwargs["cursor"], cursor)
                updated = self.fx.channel.updates[-1][1]
                self.assertIn("上一页", str(updated))
                self.assertNotIn("下一页", str(updated))

    async def test_oversized_native_cursor_directs_message_and_card_users_to_admin(self):
        page = ScopeSessionPage(
            items=(), next_cursor=SessionNativeCursor("x" * 1200), current=None,
            active_binding_id=None, has_lazy=False,
        )
        with patch.object(self.fx.app._management, "query_scope_sessions", return_value=page):
            for state, command in (("active", "/sessions"), ("archived", "/sessions archived")):
                with self.subTest(state=state):
                    await self.fx.app.handle_message(FakeMessage(command, message_id=f"list-{state}"))
                    reply = str(self.fx.channel.replies[-1][1])
                    self.assertIn("卡片容量限制", reply)
                    self.assertIn("Admin 会话列表", reply)
                    self.assertNotIn("ValueError", reply)
                    self.assertNotIn("查看当前状态", reply)

                    await self.fx.app.handle_card_action(direct_button_event({
                        "v": 4, "intent": "sessions.page", "chat_id": self.scope.chat_id,
                        "scope_kind": "direct", "page": 0,
                        "session_cursor": SessionCardPage(state).encode(self.scope),
                    }, message_id=f"card-{state}"))
                    updated = str(self.fx.channel.updates[-1][1])
                    self.assertIn("卡片容量限制", updated)
                    self.assertIn("Admin 会话列表", updated)
                    self.assertNotIn("ValueError", updated)
                    self.assertNotIn("查看当前状态", updated)
