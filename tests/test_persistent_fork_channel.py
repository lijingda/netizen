from __future__ import annotations

import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from lark_channel import OutboundCard

from netizen_cli.cards.fork import ForkSource, fork_confirm_card
from netizen_cli.codex_runtime import NativeThreadMetadata, RuntimeClosed, Submission, SubmitDisposition, ThreadLifecycleError
from netizen_cli.domain import FeishuScope, MentionContextMode, MessageContextAnchor, ScopeKind
from netizen_cli.management.chat_directory import AvailableChat, AvailableChatPage, ChatDirectoryError
from netizen_cli.session_settings import BindingTaskFeedback, BindingTurnSettings, SessionSettings
from tests.support.channel_cards import callback, elements, form_values
from tests.support.channel_fixtures import channel_fixture, side_channel_fixture
from tests.support.channel_messages import FakeMessage
from tests.support.channel_results import sent_result


class PersistentForkChannelTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = await self.enterAsyncContext(channel_fixture())
        self.app, self.store = self.fixture.app, self.fixture.store
        self.runtime, self.channel = self.fixture.runtime, self.fixture.channel
        self.management, self.history = self.fixture.management, self.fixture.message_history
        self.scope = FeishuScope("cli_test", "oc_source", ScopeKind.TOPIC, "omt_source")
        self.target_scope = FeishuScope("cli_test", "oc_source", ScopeKind.TOPIC, "omt_fork")
        self.channel.chat_types["oc_source"] = "group"
        self.source = self.store.create_channel_binding(
            scope=self.scope, project_alias="test", creator_id="ou_source",
            turn_settings=BindingTurnSettings("future-model", "ultra", "priority-v2"),
            task_feedback=BindingTaskFeedback(True, True, False),
            message_context_mode=MentionContextMode.CATCH_UP,
            context_anchor=MessageContextAnchor("om_source_anchor", 1000),
        )
        self.store.assign_native_thread_id(self.source.id, "native-source")
        self.source = self.store.get(self.source.id)
        self.native = SimpleNamespace(id="native-fork")
        metadata = NativeThreadMetadata("native-source", "来源方案", "原始上下文")
        self.runtime.thread_metadata_values[metadata.thread_id] = metadata
        self.runtime.thread_summary_values[metadata.thread_id] = metadata
        self.events = []
        self.runtime_closing = False
        self.name_confirmed = True

        @asynccontextmanager
        async def track(project_alias):
            self.events.append("track")
            self.require_open(project_alias)
            try:
                yield
            finally:
                self.events.append("untrack")

        async def native_fork(source, *, expected_project_revision):
            self.events.append("native-fork")
            self.assertEqual(source.id, self.source.id)
            self.assertEqual(expected_project_revision, self.reference().project_revision)
            return self.native

        async def adopt(binding, thread, *, name):
            # Adoption must observe a complete persisted Binding, never a
            # placeholder whose native identity will be assigned afterwards.
            stored = self.store.get(binding.id)
            self.assertEqual(stored.native_thread_id, thread.id)
            self.assertEqual(self.store.active_binding(binding.scope_key).id, binding.id)
            self.assertEqual(name, "新方案")
            self.events.append("adopt")
            return self.name_confirmed

        self.runtime.track_fork_creation = track
        self.runtime.require_fork_creation_open = self.require_open
        self.runtime.fork_exact = AsyncMock(side_effect=native_fork)
        self.runtime.adopt_fork = AsyncMock(side_effect=adopt)
        self.runtime.release_unbound_fork = AsyncMock(return_value=True)
        self.runtime.thread_resume = AsyncMock()
        self.runtime.thread_start = AsyncMock()
        self.runtime.rename_exact = AsyncMock(return_value="新方案")
        self.management.query_available_chats = AsyncMock()
        self.management.validate_available_chat = AsyncMock()
        self.history.anchors["om_root"] = MessageContextAnchor("om_root", 2000)
        self.history.anchors["om_seed"] = MessageContextAnchor("om_seed", 2001)

        original_send = self.channel.send
        async def send(to, content, opts=None):
            self.events.append("send-root" if isinstance(content, OutboundCard) else "send-seed")
            return await original_send(to, content, opts)
        self.channel.send = send
        original_create = self.store.create_fork_binding
        def create(**kwargs):
            result = original_create(**kwargs)
            self.events.append("bind")
            return result
        self.store.create_fork_binding = create

    def require_open(self, project_alias):
        if self.runtime_closing:
            raise RuntimeClosed("服务正在停止，本次分支创建未完成。")
        self.store.require_project_not_deleting(project_alias)

    def reference(self):
        return ForkSource(
            self.source.id, self.source.native_thread_id,
            self.source.settings_revision, self.source.context_revision,
            self.source.feedback_revision, self.store.get_project("test").revision,
        )

    def event(self, *, form=None, value=None, scope=None, message_id="om_fork_form"):
        scope = scope or self.scope
        self.channel.fetched_messages[message_id] = {"data": {"items": [{
            "chat_id": scope.chat_id, "thread_id": scope.topic_id,
        }]}}
        return SimpleNamespace(
            message_id=message_id, chat_id=scope.chat_id,
            operator=SimpleNamespace(open_id="ou_creator"),
            action=SimpleNamespace(tag="button", value=value or {}, form_value=form),
        )

    def confirm_event(self, *, target=None):
        card = fork_confirm_card(self.scope, self.reference(), source_title="来源方案",
                                 project_alias="test", target_chat=target)
        return self.event(form={**form_values(card), "fork_name_v1": "新方案"})

    def queue_topic(self, *, chat_id=None, promoted=False):
        chat_id = chat_id or self.target_scope.chat_id
        self.channel.send_results.append(sent_result(
            "om_root", chat_id=chat_id, thread_id=None if promoted else "omt_fork",
        ))
        if promoted:
            self.channel.send_results.append(sent_result("om_seed", chat_id=chat_id,
                thread_id="omt_fork", root_id="om_root", parent_id="om_root"))

    def updates_text(self):
        return json.dumps(self.channel.updates, ensure_ascii=False)

    def assert_no_execution(self):
        self.assertEqual(self.runtime.submit_calls, [])
        self.assertEqual(self.runtime.start_goal_calls, [])
        self.runtime.thread_resume.assert_not_awaited()
        self.runtime.thread_start.assert_not_awaited()
        self.runtime.rename_exact.assert_not_awaited()

    async def test_command_opens_destination_without_native_or_directory_work(self):
        message = FakeMessage("/fork", message_id="om_command", chat_id=self.scope.chat_id,
                              thread_id=self.scope.topic_id, chat_type="group")
        await self.app.handle_message(message)
        card = self.channel.replies[-1][1]
        self.assertIsInstance(card, OutboundCard)
        self.assertIn("当前聊天的新话题", str(card.card))
        self.assertIn("选择其他群", str(card.card))
        self.runtime.fork_exact.assert_not_awaited()
        self.management.query_available_chats.assert_not_awaited()
        self.management.validate_available_chat.assert_not_awaited()
        self.assertEqual(self.channel.send_calls, [])
        self.assert_no_execution()
        await self.app.handle_message(FakeMessage("/fork", message_id="om_unmentioned",
            chat_id=self.scope.chat_id, thread_id=self.scope.topic_id,
            chat_type="group", mentioned_bot=False))
        self.assertEqual(len(self.channel.replies), 1)

    async def test_same_chat_fork_is_complete_before_adoption_and_keeps_source(self):
        self.queue_topic()
        await self.app.handle_card_action(self.confirm_event())
        target = self.store.active_binding(self.target_scope.key)
        self.assertIsNotNone(target, self.updates_text())
        self.assertEqual(target.native_thread_id, self.native.id)
        self.assertEqual(target.creator_id, "ou_creator")
        self.assertEqual(target.project_alias, self.source.project_alias)
        self.assertEqual(target.turn_settings, self.source.turn_settings)
        self.assertEqual(target.task_feedback, self.source.task_feedback)
        self.assertEqual(target.message_context_mode, self.source.message_context_mode)
        self.assertEqual(target.context_anchor, MessageContextAnchor("om_root", 2000))
        self.assertEqual(self.store.active_binding(self.scope.key), self.source)
        self.assertEqual(self.store.get_scope(self.target_scope.key).topic_id, "omt_fork")
        self.assertEqual(self.events, ["track", "native-fork", "send-root", "bind", "adopt", "untrack"])
        self.runtime.fork_exact.assert_awaited_once()
        self.runtime.adopt_fork.assert_awaited_once_with(target, self.native, name="新方案")
        self.runtime.rename_exact.assert_not_awaited()
        self.runtime.release_unbound_fork.assert_not_awaited()
        self.assertEqual(len(self.channel.send_calls), 1)
        root = self.channel.send_calls[0]
        self.assertEqual(root[0], self.scope.chat_id)
        self.assertIsNone(root[2].reply_to)
        self.assertIn("请等待创建完成后再发送消息", str(root[1].card))
        root_updates = [card for message_id, card in self.channel.updates if message_id == "om_root"]
        self.assertEqual(len(root_updates), 1)
        self.assertIn("会话分支已创建", str(root_updates[0]))
        self.assert_no_execution()

    async def test_cross_group_picker_reuses_directory_then_promotes_one_root_with_seed(self):
        target = AvailableChat("oc_target", "研发群", "group", False)
        self.target_scope = FeishuScope("cli_test", target.chat_id, ScopeKind.TOPIC, "omt_fork")
        self.management.query_available_chats.return_value = AvailableChatPage((target,), "page-two")
        self.management.validate_available_chat.return_value = target
        await self.app.handle_message(FakeMessage("/fork", message_id="om_command",
            chat_id=self.scope.chat_id, thread_id=self.scope.topic_id, chat_type="group"))
        select = elements(self.channel.replies[-1][1].card, "select_static")[0]
        await self.app.handle_card_action(self.event(form={select["name"]: select["options"][1]["value"]}))
        search = OutboundCard(card=self.channel.updates[-1][1])
        await self.app.handle_card_action(self.event(form={**form_values(search), "fork_query_v1": "研发"}))
        self.management.query_available_chats.assert_awaited_once_with(query="研发", page_token=None)
        results = OutboundCard(card=self.channel.updates[-1][1])
        select = elements(results.card, "select_static")[0]
        await self.app.handle_card_action(self.event(form={select["name"]: select["options"][0]["value"]}))
        self.management.validate_available_chat.assert_awaited_once_with(target.chat_id)
        confirmation = OutboundCard(card=self.channel.updates[-1][1])
        self.assertIn("目标群的参与者", str(confirmation.card))
        self.runtime.fork_exact.assert_not_awaited()
        self.assertEqual(self.channel.send_calls, [])
        self.queue_topic(chat_id=target.chat_id, promoted=True)
        await self.app.handle_card_action(self.event(form={**form_values(confirmation), "fork_name_v1": "新方案"}))
        self.assertEqual(self.management.validate_available_chat.await_count, 2)
        binding = self.store.active_binding(self.target_scope.key)
        self.assertIsNotNone(binding, self.updates_text())
        self.assertEqual(binding.context_anchor, MessageContextAnchor("om_seed", 2001))
        self.assertEqual([call[0] for call in self.channel.send_calls], [target.chat_id, target.chat_id])
        self.assertEqual(self.channel.send_calls[1][2].reply_to, "om_root")
        self.assertTrue(self.channel.send_calls[1][2].reply_in_thread)
        self.assertNotEqual(self.channel.send_calls[0][2].uuid, self.channel.send_calls[1][2].uuid)
        self.assertEqual(self.store.active_binding(self.scope.key), self.source)
        self.assertEqual([event for event in self.events if event.startswith("send-")], ["send-root", "send-seed"])
        self.assert_no_execution()

    async def test_source_switch_and_wrong_fetched_scope_reject_before_fork(self):
        event = self.confirm_event()
        second = self.store.create_channel_binding(scope=self.scope, project_alias="test", creator_id="ou_other")
        await self.app.handle_card_action(event)
        self.runtime.fork_exact.assert_not_awaited()
        self.assertEqual(self.store.active_binding(self.scope.key), second)
        self.assertEqual(self.channel.send_calls, [])
        self.assertIn("变化", self.updates_text())
        self.store.activate(scope_key=self.scope.key, binding_id=self.source.id,
                            context_anchor=self.source.context_anchor)
        self.source = self.store.get(self.source.id)
        event = self.confirm_event()
        self.channel.fetched_messages[event.message_id]["data"]["items"][0]["thread_id"] = "omt_other"
        await self.app.handle_card_action(event)
        self.runtime.fork_exact.assert_not_awaited()
        self.assertIn("不一致", self.updates_text())

    async def test_target_removed_before_confirm_is_rejected_before_native_creation(self):
        target = AvailableChat("oc_target", "已移除的群", None, None)
        self.management.validate_available_chat.side_effect = ChatDirectoryError("not_member", "机器人已不在目标群。")
        await self.app.handle_card_action(self.confirm_event(target=target))
        self.runtime.fork_exact.assert_not_awaited()
        self.assertEqual(self.channel.send_calls, [])
        self.assertIn("目标群", self.updates_text())

    async def test_unknown_native_result_is_not_retried_or_published(self):
        self.runtime.fork_exact.side_effect = ThreadLifecycleError("原生分支创建结果未确认；本次不自动重试。")
        await self.app.handle_card_action(self.confirm_event())
        self.runtime.fork_exact.assert_awaited_once()
        self.assertEqual(self.channel.send_calls, [])
        self.assertIsNone(self.store.active_binding(self.target_scope.key))
        self.assertEqual(self.store.active_binding(self.scope.key), self.source)
        self.runtime.release_unbound_fork.assert_not_awaited()
        self.assertIn("未确认", self.updates_text())
        self.assert_no_execution()

    async def test_invalid_root_and_seed_cannot_bind_or_delete_native_fork(self):
        cases = [
            [sent_result("om_root", chat_id=self.scope.chat_id, thread_id="omt_fork", parent_id="om_existing")],
            [sent_result("om_root", chat_id=self.scope.chat_id),
             sent_result("om_seed", chat_id=self.scope.chat_id, thread_id="omt_fork",
                         root_id="om_wrong", parent_id="om_root")],
        ]
        for results in cases:
            with self.subTest(results=results):
                self.channel.send_results.extend(results)
                await self.app.handle_card_action(self.confirm_event())
                self.assertIsNone(self.store.active_binding(self.target_scope.key))
        self.assertEqual(self.runtime.fork_exact.await_count, 2)
        self.assertEqual(self.runtime.release_unbound_fork.await_count, 2)
        self.runtime.adopt_fork.assert_not_awaited()
        self.assertEqual(self.runtime.delete_binding_calls, [])
        self.assertIn("绑定未完成", self.updates_text())

    async def test_project_deletion_during_publish_preserves_orphan_without_binding(self):
        event = self.confirm_event()
        self.queue_topic()
        send = self.channel.send
        async def deleting_send(*args, **kwargs):
            result = await send(*args, **kwargs)
            snapshot = self.store.preview_project_delete("test")
            self.store.begin_project_delete(alias="test", expected_revision=snapshot.project.revision,
                expected_inventory_fingerprint=snapshot.fingerprint)
            return result
        self.channel.send = deleting_send
        await self.app.handle_card_action(event)
        self.assertTrue(self.store.project_delete_in_progress("test"))
        self.assertIsNone(self.store.active_binding(self.target_scope.key))
        self.runtime.release_unbound_fork.assert_awaited_once_with(self.native)
        self.runtime.adopt_fork.assert_not_awaited()
        self.assertEqual(self.runtime.delete_binding_calls, [])
        self.assertEqual(self.store.active_binding(self.scope.key), self.source)

    async def test_shutdown_between_publish_and_binding_retains_native_only(self):
        self.queue_topic()
        send = self.channel.send
        async def closing_send(*args, **kwargs):
            result = await send(*args, **kwargs)
            self.runtime_closing = True
            return result
        self.channel.send = closing_send
        await self.app.handle_card_action(self.confirm_event())
        self.assertIsNone(self.store.active_binding(self.target_scope.key))
        self.runtime.release_unbound_fork.assert_awaited_once_with(self.native)
        self.runtime.adopt_fork.assert_not_awaited()
        self.assertIn("停止", self.updates_text())

    async def test_early_auto_created_binding_wins_and_is_never_overwritten(self):
        self.store.defaults.save(app_id="cli_test", kind="chat", chat_id=self.target_scope.chat_id,
            keyword=None, project="test", session_settings=SessionSettings(),
            rule_id=None, expected_revision=None)
        self.queue_topic()
        async def submit(**kwargs):
            self.runtime.submit_calls.append(kwargs)
            binding = kwargs["binding"]
            self.store.assign_native_thread_id(binding.id, "native-early")
            return Submission(SubmitDisposition.STARTED, binding.id, "native-early", "turn-early",
                              lambda: None, task_feedback=binding.task_feedback)
        self.runtime.submit = submit
        send = self.channel.send
        early_binding = None
        async def early_prompt_send(*args, **kwargs):
            nonlocal early_binding
            result = await send(*args, **kwargs)
            await self.app.handle_message(FakeMessage("提前的问题", message_id="om_early",
                chat_id=self.target_scope.chat_id, chat_type="group", thread_id=self.target_scope.topic_id))
            early_binding = self.store.active_binding(self.target_scope.key)
            return result
        self.channel.send = early_prompt_send
        await self.app.handle_card_action(self.confirm_event())
        self.assertIsNotNone(early_binding)
        self.assertEqual(self.store.active_binding(self.target_scope.key), early_binding)
        self.assertNotEqual(early_binding.native_thread_id, self.native.id)
        self.assertEqual(len(self.runtime.submit_calls), 1)
        self.assertEqual(self.runtime.submit_calls[0]["binding"].id, early_binding.id)
        self.runtime.adopt_fork.assert_not_awaited()
        self.runtime.release_unbound_fork.assert_awaited_once_with(self.native)
        self.assertIn("绑定未完成", self.updates_text())

    async def test_name_failure_keeps_successful_binding_and_reports_success(self):
        self.queue_topic()
        self.name_confirmed = False
        await self.app.handle_card_action(self.confirm_event())
        target = self.store.active_binding(self.target_scope.key)
        self.assertIsNotNone(target)
        self.assertEqual(target.native_thread_id, self.native.id)
        self.runtime.adopt_fork.assert_awaited_once_with(target, self.native, name="新方案")
        self.runtime.release_unbound_fork.assert_not_awaited()
        self.assertEqual(self.runtime.delete_binding_calls, [])
        self.runtime.fork_exact.assert_awaited_once()
        self.assertEqual(len(self.channel.send_calls), 1)
        self.assertIn("会话分支已创建", self.updates_text())
        self.assertIn("名称更新未确认", self.updates_text())
        self.assert_no_execution()

    async def test_success_receipt_update_failure_falls_back_to_same_success_card(self):
        self.queue_topic()
        self.channel.fail_card_updates = True
        with self.assertLogs("netizen_cli.channel_app", level="ERROR"):
            await self.app.handle_card_action(self.confirm_event())
        target = self.store.active_binding(self.target_scope.key)
        self.assertIsNotNone(target)
        self.assertEqual(target.native_thread_id, self.native.id)
        self.runtime.adopt_fork.assert_awaited_once_with(target, self.native, name="新方案")
        self.runtime.release_unbound_fork.assert_not_awaited()
        self.runtime.fork_exact.assert_awaited_once()
        self.assertEqual(len(self.channel.send_calls), 1)
        self.assertEqual(len(self.channel.replies), 1)
        message_id, fallback = self.channel.replies[0]
        self.assertEqual(message_id, "om_fork_form")
        self.assertIsInstance(fallback, OutboundCard)
        self.assertEqual(fallback.card, self.channel.updates[-1][1])
        self.assertIn("会话分支已创建", str(fallback.card))
        self.assert_no_execution()

    async def test_failed_success_update_and_fallback_do_not_replace_success_with_error(self):
        self.queue_topic()
        self.channel.fail_card_updates = True
        self.channel.reply_results.append(RuntimeError("receipt unavailable"))
        with self.assertLogs("netizen_cli.channel_app", level="WARNING"):
            await self.app.handle_card_action(self.confirm_event())
        target = self.store.active_binding(self.target_scope.key)
        self.assertIsNotNone(target)
        self.assertEqual(target.native_thread_id, self.native.id)
        self.runtime.adopt_fork.assert_awaited_once_with(target, self.native, name="新方案")
        self.runtime.release_unbound_fork.assert_not_awaited()
        self.runtime.fork_exact.assert_awaited_once()
        self.assertEqual(len(self.channel.send_calls), 1)
        self.assertEqual(len(self.channel.replies), 1)
        attempted_success = self.channel.replies[0][1]
        self.assertIsInstance(attempted_success, OutboundCard)
        # Both outbound attempts carry the same successful result. A generic
        # outer error card after the failed fallback would replace this value.
        self.assertEqual(self.channel.updates[-1][1], attempted_success.card)
        self.assertIn("会话分支已创建", str(attempted_success.card))
        self.assertNotIn("会话分支创建未完成", self.updates_text())
        self.assertEqual(self.runtime.delete_binding_calls, [])
        self.assert_no_execution()

    async def test_adoption_failure_reports_saved_binding_without_orphan_cleanup(self):
        self.queue_topic()
        self.runtime.adopt_fork.side_effect = ThreadLifecycleError("订阅交接未确认。")
        await self.app.handle_card_action(self.confirm_event())
        binding = self.store.active_binding(self.target_scope.key)
        self.assertIsNotNone(binding)
        self.assertEqual(binding.native_thread_id, self.native.id)
        self.assertEqual(self.store.active_binding(self.scope.key), self.source)
        self.runtime.release_unbound_fork.assert_not_awaited()
        self.runtime.rename_exact.assert_not_awaited()
        self.assertEqual(self.runtime.delete_binding_calls, [])
        self.assertIn("本地会话已保存", self.updates_text())
        self.assertNotIn("绑定未完成", self.updates_text())
        self.assert_no_execution()

    async def test_cancellation_after_native_creation_releases_only_known_handle(self):
        self.channel.send_results.append(asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await self.app.handle_card_action(self.confirm_event())
        self.runtime.fork_exact.assert_awaited_once()
        self.runtime.release_unbound_fork.assert_awaited_once_with(self.native)
        self.assertIsNone(self.store.active_binding(self.target_scope.key))
        self.assertEqual(self.store.active_binding(self.scope.key), self.source)
        self.assertEqual(len(self.channel.send_calls), 1)
        self.assertEqual(self.runtime.delete_binding_calls, [])
        self.assertEqual(self.events[-1], "untrack")
        self.assert_no_execution()

    async def test_side_fork_command_uses_existing_side_rejection(self):
        async with side_channel_fixture() as fixture:
            source = FakeMessage("/side", message_id="om_side")
            fixture.binding_for(source)
            fixture.queue_promoted_topic(chat_id="oc_direct", root_id="om_side_root",
                                         seed_id="om_side_seed", topic_id="omt_side")
            await fixture.app.handle_message(source)
            fixture.runtime.fork_exact = AsyncMock()
            await fixture.app.handle_message(FakeMessage("/fork", message_id="om_side_fork", thread_id="omt_side"))
            fixture.runtime.fork_exact.assert_not_awaited()
            self.assertEqual(len(fixture.channel.send_calls), 2)
            self.assertEqual(fixture.runtime.submit_side_calls, [])
            self.assertIn("Side", str(fixture.channel.replies[-1][1]))


if __name__ == "__main__":
    unittest.main()
