from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from netizen_cli.codex_runtime import ThreadResumeNotFound
from netizen_cli.domain import FeishuScope, ScopeKind
from tests.support.channel_cards import callback, direct_button_event
from tests.support.channel_fixtures import channel_fixture
from tests.support.channel_messages import FakeMessage


class ResumeChannelTest(unittest.IsolatedAsyncioTestCase):
    async def test_slash_resume_distinguishes_materialized_and_lazy_success(self) -> None:
        for materialized in (False, True):
            with self.subTest(materialized=materialized):
                async with channel_fixture() as fixture:
                    scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
                    target = (await fixture.create_binding(scope)).binding
                    if materialized:
                        fixture.store.assign_native_thread_id(target.id, "native-unlisted")
                    current = (await fixture.create_binding(scope)).binding

                    await fixture.app.handle_message(FakeMessage(
                        f"/resume {target.short_id}", message_id="om_resume",
                    ))

                    self.assertEqual(fixture.store.active_binding(scope.key).id, target.id)
                    notice = fixture.channel.replies[-1][1]
                    self.assertIn("已恢复并切换" if materialized else "已切换", notice)
                    if not materialized:
                        self.assertNotIn("恢复", notice)
                    self.assertEqual(
                        fixture.runtime.active_binding_change_calls[-1],
                        (current.id, target.id),
                    )
                    self.assertEqual(fixture.runtime.submit_calls, [])

    async def test_unlisted_materialized_session_card_can_resume(self) -> None:
        async with channel_fixture() as fixture:
            scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
            target = (await fixture.create_binding(scope)).binding
            fixture.store.assign_native_thread_id(target.id, "native-unlisted")
            await fixture.create_binding(scope)
            await fixture.app.handle_message(FakeMessage("/sessions", message_id="om_sessions"))
            card = fixture.channel.replies[-1][1]
            self.assertIn("归档状态：未确认", str(card))

            await fixture.app.handle_card_action(direct_button_event(
                callback(card, "设为当前"), message_id="om_sessions",
            ))

            self.assertEqual(fixture.store.active_binding(scope.key).id, target.id)
            self.assertIn("已恢复并切换", str(fixture.channel.updates[-1][1]))
            self.assertEqual(fixture.runtime.submit_calls, [])

    async def test_native_missing_is_a_business_error_for_slash_and_card(self) -> None:
        for card_action in (False, True):
            with self.subTest(card_action=card_action):
                async with channel_fixture() as fixture:
                    scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
                    target = (await fixture.create_binding(scope)).binding
                    fixture.store.assign_native_thread_id(target.id, "native-unlisted")
                    current = (await fixture.create_binding(scope)).binding
                    await fixture.app.handle_message(FakeMessage("/sessions", message_id="om_sessions"))
                    card = fixture.channel.replies[-1][1]
                    changes = tuple(fixture.runtime.active_binding_change_calls)
                    with (
                        patch.object(fixture.runtime, "activate_exact", AsyncMock(
                            side_effect=ThreadResumeNotFound("未找到该原生会话的可恢复记录"),
                        )) as resume,
                        patch("netizen_cli.channel_app.logger.exception") as unexpected,
                    ):
                        if card_action:
                            await fixture.app.handle_card_action(direct_button_event(
                                callback(card, "设为当前"), message_id="om_sessions",
                            ))
                            notice = str(fixture.channel.updates[-1][1])
                        else:
                            await fixture.app.handle_message(FakeMessage(
                                f"/resume {target.short_id}", message_id="om_resume",
                            ))
                            notice = fixture.channel.replies[-1][1]
                        resume.assert_awaited_once_with(target.id, context_anchor=None)
                        unexpected.assert_not_called()
                    self.assertIn("可恢复记录", notice)
                    self.assertIn("本次未切换", notice)
                    self.assertNotIn("请求处理未完成", notice)
                    self.assertEqual(fixture.store.active_binding(scope.key).id, current.id)
                    self.assertEqual(fixture.store.get(target.id).native_thread_id, "native-unlisted")
                    self.assertEqual(tuple(fixture.runtime.active_binding_change_calls), changes)
