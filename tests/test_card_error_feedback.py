from __future__ import annotations

import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from lark_channel import FeishuChannel, OutboundConfig, OutboundPost, OutboundSender, RetryConfig

from netizen_cli.bindings import SideTopicState
from netizen_cli.cards import CardActionError
from netizen_cli.codex_runtime import RuntimeClosed, SideCloseFailed
from netizen_cli.domain import CardControlIntent, CardControlName, FeishuScope, ScopeKind, SettingsSection
from tests.support.channel_fixtures import channel_fixture


class CardErrorFeedbackTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = await self.enterAsyncContext(channel_fixture())
        self.app = self.fixture.app
        self.scope = FeishuScope("cli_test", "oc_group", ScopeKind.TOPIC, "omt_card")
        self.event = SimpleNamespace(
            message_id="om_card", chat_id=self.scope.chat_id,
            operator=SimpleNamespace(open_id="ou_user"),
            action=SimpleNamespace(tag="button", value={}, form_value=None),
        )

    def form_event(self, fields):
        self.fixture.channel.fetched_messages["om_card"] = {"code": 0, "data": {"items": [{
            "message_id": "om_card", "chat_id": self.scope.chat_id, "thread_id": self.scope.topic_id,
        }]}}
        self.event.action.form_value = fields
        return self.event

    async def test_same_action_feedback_does_not_depend_on_exception_category(self):
        intent = CardControlIntent(
            scope=self.scope, source_id="om_card", sender_id="ou_user",
            name=CardControlName.CONFIGURE_BINDING,
        )
        for error in (CardActionError("配置已变化"), RuntimeClosed("准入已关闭"), RuntimeError("请求结果未知")):
            for updated in (True, False):
                with self.subTest(error=type(error).__name__, updated=updated):
                    self.fixture.channel.replies.clear()
                    with (
                        patch.object(self.app, "_decode_card_event", AsyncMock(return_value=intent)),
                        patch.object(self.app, "_card_control", AsyncMock(side_effect=error)) as control,
                        patch.object(self.app, "_safe_update_card", AsyncMock(return_value=updated)),
                        self.assertLogs("netizen_cli.channel_app", level="ERROR")
                        if type(error) is RuntimeError else nullcontext(),
                    ):
                        await self.app.handle_card_action(self.event)
                    control.assert_awaited_once_with(intent)
                    self.assertEqual(len(self.fixture.channel.replies), 0 if updated else 1)
                    if not updated:
                        source, text = self.fixture.channel.replies[0]
                        self.assertEqual(source, "om_card")
                        self.assertIn(str(error), text)
                        target = self.fixture.channel.reply_targets[-1]
                        self.assertEqual(target.chat_id, self.scope.chat_id)
                        self.assertEqual(target.conversation.thread_id, self.scope.topic_id)
                        if type(error) is RuntimeError:
                            self.assertIn("未完成", text)
                            self.assertNotIn("未执行", text)

    async def test_goal_redraw_failure_uses_same_text_feedback(self):
        intent = CardControlIntent(
            scope=self.scope, source_id="om_card", sender_id="ou_user",
            name=CardControlName.GOAL_REFRESH, binding_id="binding",
        )
        with (
            patch.object(self.app, "_decode_card_event", AsyncMock(return_value=intent)),
            patch.object(self.app, "_card_control", AsyncMock(side_effect=CardActionError("Goal 已变化"))),
            patch.object(self.app, "_update_goal_action_notice", AsyncMock(return_value=False)) as redraw,
        ):
            await self.app.handle_card_action(self.event)
        redraw.assert_awaited_once_with(intent, "Goal 已变化")
        self.assertEqual(len(self.fixture.channel.replies), 1)
        self.assertIn("Goal 已变化", self.fixture.channel.replies[0][1])

    async def test_settings_decode_error_can_reply_using_verified_scope(self):
        event = self.form_event({})
        with (
            patch.object(self.app, "_safe_update_card", AsyncMock(return_value=False)),
            patch.object(self.app, "_settings_card", wraps=self.app._settings_card) as settings_card,
        ):
            await self.app.handle_card_action(event)
        self.assertEqual(settings_card.call_args.args, (self.scope,))
        self.assertEqual(settings_card.call_args.kwargs["section"], SettingsSection.PROJECTS)
        self.assertEqual(len(self.fixture.channel.replies), 1)
        self.assertEqual(self.fixture.channel.reply_targets[-1].conversation.thread_id, "omt_card")

    async def test_verified_form_decode_errors_keep_feedback_location(self):
        for fields in ({"config_model": "old-card"}, {"new_project": "old-card"},
                       {"rename_name_v1__binding": ""}):
            for updated in (True, False):
                with self.subTest(fields=fields, updated=updated):
                    event = self.form_event(fields)
                    self.fixture.channel.replies.clear()
                    with (
                        patch.object(self.app, "_safe_update_card", AsyncMock(return_value=updated)) as redraw,
                        patch.object(self.app, "_card_control", AsyncMock()) as control,
                    ):
                        await self.app.handle_card_action(event)
                    redraw.assert_awaited_once()
                    control.assert_not_awaited()
                    self.assertEqual(len(self.fixture.channel.replies), 0 if updated else 1)
                    if not updated:
                        target = self.fixture.channel.reply_targets[-1]
                        self.assertEqual((target.message_id, target.chat_id, target.conversation.thread_id),
                                         ("om_card", self.scope.chat_id, self.scope.topic_id))

    async def test_unexpected_form_decode_error_keeps_verified_location(self):
        event = self.form_event({"config_model": "old-card"})
        with (
            patch("netizen_cli.channel_app.decode_card_form", side_effect=RuntimeError("decode unavailable")),
            patch.object(self.app, "_safe_update_card", AsyncMock(return_value=False)),
            patch.object(self.app, "_card_control", AsyncMock()) as control,
            self.assertLogs("netizen_cli.channel_app", level="ERROR"),
        ):
            await self.app.handle_card_action(event)
        control.assert_not_awaited()
        self.assertEqual(len(self.fixture.channel.replies), 1)
        self.assertIn("未完成", self.fixture.channel.replies[0][1])
        self.assertIn("/sessions", self.fixture.channel.replies[0][1])
        self.assertEqual(self.fixture.channel.reply_targets[-1].conversation.thread_id, self.scope.topic_id)

    async def test_unverified_callback_does_not_guess_text_destination(self):
        for fork in (False, True):
            with self.subTest(fork=fork):
                event = self.form_event({"config_model": "old-card"})
                event.action.value = {"kind": "netizen_fork", "v": 4} if fork else {}
                self.fixture.channel.fetched_messages["om_card"]["data"]["items"][0]["chat_id"] = "oc_other"
                with patch.object(self.app, "_safe_update_card", AsyncMock(return_value=False)):
                    await self.app.handle_card_action(event)
                self.assertEqual(self.fixture.channel.replies, [])

    async def test_fork_decode_error_only_replies_when_redraw_fails(self):
        event = self.form_event(None)
        event.action.value = {"kind": "netizen_fork", "v": 4}
        for updated in (True, False):
            with self.subTest(updated=updated):
                self.fixture.channel.replies.clear()
                with (
                    patch.object(self.app, "_safe_update_card", AsyncMock(return_value=updated)) as redraw,
                    patch.object(self.app, "_create_fork_from_card", AsyncMock()) as create,
                ):
                    await self.app.handle_card_action(event)
                redraw.assert_awaited_once()
                create.assert_not_awaited()
                self.assertEqual(len(self.fixture.channel.replies), 0 if updated else 1)
                if not updated:
                    self.assertEqual(self.fixture.channel.reply_targets[-1].conversation.thread_id, self.scope.topic_id)

    async def test_sdk_target_gone_never_moves_error_feedback_to_main_chat(self):
        for fork in (False, True):
            with self.subTest(fork=fork):
                event = self.form_event({"config_model": "old-card"})
                event.action.value = {"kind": "netizen_fork", "v": 4} if fork else {}
                driver = SimpleNamespace(
                    reply_message=AsyncMock(return_value={"code": 230017, "msg": "reply target unavailable"}),
                    create_message=AsyncMock(),
                )
                sender = OutboundSender(driver, OutboundConfig(retry=RetryConfig(max_attempts=1)))

                async def send(to, content, opts):
                    return await sender.send(
                        OutboundPost(markdown=content), receive_id=to, receive_id_type=opts.receive_id_type,
                        reply_to=opts.reply_to, reply_in_thread=opts.reply_in_thread,
                        reply_target_gone=opts.reply_target_gone, uuid_=opts.uuid,
                    )

                peer = SimpleNamespace(send=send)

                async def sdk_reply(origin, content, opts=None):
                    return await FeishuChannel.reply(peer, origin, content, opts)

                with (
                    patch.object(self.fixture.channel, "reply", sdk_reply),
                    patch.object(self.app, "_safe_update_card", AsyncMock(return_value=False)),
                ):
                    await self.app.handle_card_action(event)
                driver.reply_message.assert_awaited_once()
                self.assertEqual(driver.reply_message.call_args.kwargs["message_id"], "om_card")
                self.assertTrue(driver.reply_message.call_args.kwargs["reply_in_thread"])
                driver.create_message.assert_not_awaited()

    async def test_side_root_redraw_failure_keeps_exact_feedback_destination(self):
        created = await self.fixture.create_binding(self.scope)
        side = self.fixture.store.create_side_topic(
            app_id=self.scope.app_id, chat_id=self.scope.chat_id, source_message_id="om_source",
            parent_binding_id=created.binding.id, creator_id="ou_user", requires_mention=True,
        )
        self.fixture.store.set_side_topic_root(side.id, "om_card")
        with (
            patch.object(self.app, "_decode_card_event", AsyncMock(side_effect=CardActionError("Side 已变化"))),
            patch.object(self.app, "_update_side_card", AsyncMock(return_value=False)),
        ):
            await self.app.handle_card_action(self.event)
        self.assertEqual(len(self.fixture.channel.replies), 1)
        self.assertEqual(self.fixture.channel.replies[0][0], "om_card")
        self.assertEqual(self.fixture.channel.reply_targets[-1].chat_id, self.scope.chat_id)

    async def test_side_close_failure_reports_text_without_replaying_close(self):
        created = await self.fixture.create_binding(self.scope)
        side = self.fixture.store.create_side_topic(
            app_id=self.scope.app_id, chat_id=self.scope.chat_id, source_message_id="om_source",
            parent_binding_id=created.binding.id, creator_id="ou_user", requires_mention=True,
        )
        self.fixture.store.set_side_topic_root(side.id, "om_card")
        intent = CardControlIntent(
            scope=FeishuScope(self.scope.app_id, self.scope.chat_id, ScopeKind.GROUP),
            source_id="om_card", sender_id="ou_user", name=CardControlName.SIDE_CLOSE,
            side_id=side.id,
        )
        self.fixture.runtime.side_close_error = SideCloseFailed("关闭结果未确认")
        with (
            patch.object(self.app, "_decode_card_event", AsyncMock(return_value=intent)),
            patch.object(self.app, "_update_side_card", AsyncMock(return_value=False)) as redraw,
        ):
            await self.app.handle_card_action(self.event)
        self.assertEqual(self.fixture.runtime.close_side_calls, [(side.id, SideTopicState.FAILED)])
        self.assertEqual(self.fixture.store.get_side_topic(side.id).state, SideTopicState.CREATING)
        redraw.assert_awaited_once()
        self.assertEqual(len(self.fixture.channel.replies), 1)
        self.assertEqual(self.fixture.channel.replies[0][0], "om_card")
        self.assertIn("关闭结果未确认", self.fixture.channel.replies[0][1])
