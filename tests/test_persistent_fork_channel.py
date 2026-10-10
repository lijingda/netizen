from __future__ import annotations

import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

from lark_channel import OutboundCard

from netizen_cli.cards.fork import ForkSource, decode_fork_action, fork_form_card
from netizen_cli.chat_targets import ChatTargetError, ValidatedChatTarget
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
        self.management.query_available_chats = AsyncMock(return_value=AvailableChatPage((), None))
        self.management.validate_available_chat = AsyncMock(return_value=None)
        self.management.validate_chat_target = AsyncMock(
            side_effect=lambda chat_id: ValidatedChatTarget(chat_id, "group"),
        )
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
        card = fork_form_card(self.scope, self.reference(), source_title="来源方案",
                             project_alias="test", chats=(target,) if target else (),
                             target_mode="group" if target else "current",
                             target_choice=target.chat_id if target else "")
        return self.event(form={**form_values(card), "fork_name_v5": "新方案"},
                          value=callback(card, "创建分支"))

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

    async def open_form(self):
        await self.app.handle_message(FakeMessage("/fork", message_id="om_command",
            chat_id=self.scope.chat_id, thread_id=self.scope.topic_id, chat_type="group"))
        return self.channel.replies[-1][1]

    async def search_form(self, card, *, query="", values=None, label="查找群聊"):
        submitted = {**form_values(card), **(values or {})}
        if label == "查找群聊":
            submitted["fork_query_v5"] = query
        await self.app.handle_card_action(self.event(value=callback(card, label), form=submitted))
        return OutboundCard(card=self.channel.updates[-1][1])

    async def jump_form(self, card, page_index, *, values=None):
        page_field = next(item for item in elements(card.card, "select_static") if item["name"] == "fork_page_v5")
        await self.app.handle_card_action(self.event(value=callback(card, "跳转"), form={
            **form_values(card), **(values or {}), "fork_page_v5": page_field["options"][page_index]["value"],
        }))
        return OutboundCard(card=self.channel.updates[-1][1])

    async def test_command_opens_single_form_without_directory_or_native_work(self):
        card = await self.open_form()
        self.assertIsInstance(card, OutboundCard)
        self.assertEqual(form_values(card)["fork_mode_v5"], "current")
        self.assertNotIn("fork_target_v5", form_values(card))
        self.assertEqual(form_values(card)["fork_name_v5"], "来源方案 · 分支")
        self.assertEqual(len(elements(card.card, "button")), 2)
        self.runtime.fork_exact.assert_not_awaited()
        self.management.query_available_chats.assert_not_awaited()
        self.management.validate_available_chat.assert_not_awaited()
        self.assertIn("fork_query_v5", form_values(card))
        self.assertEqual(self.channel.send_calls, [])
        self.assertEqual(self.channel.updates, [])
        self.assert_no_execution()
        await self.app.handle_message(FakeMessage("/fork", message_id="om_unmentioned",
            chat_id=self.scope.chat_id, thread_id=self.scope.topic_id,
            chat_type="group", mentioned_bot=False))
        self.assertEqual(len(self.channel.replies), 1)

    async def test_same_chat_fork_is_complete_before_adoption_and_keeps_source(self):
        self.queue_topic()
        event = self.confirm_event()
        event.action.form_value.update(fork_target_v5={"malformed": "inactive"}, fork_chat_id_v5=["oc_other"])
        await self.app.handle_card_action(event)
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

    async def test_empty_name_submission_preserves_form_and_target_without_creation(self):
        target = AvailableChat("oc_target", "研发群", "group", False)
        self.management.query_available_chats.return_value = AvailableChatPage((target,), None)
        event = self.confirm_event(target=target)
        event.action.form_value["fork_name_v5"] = "  "
        await self.app.handle_card_action(event)
        card = OutboundCard(card=self.channel.updates[-1][1])
        self.assertEqual(form_values(card)["fork_name_v5"], "  ")
        self.assertEqual(form_values(card)["fork_target_v5"], target.chat_id)
        self.assertIn("会话名称不能为空", str(card.card))
        self.assertEqual(len(elements(card.card, "form")), 1)
        self.assertEqual(self.channel.send_calls, [])
        self.runtime.fork_exact.assert_not_awaited()
        self.assert_no_execution()

    async def test_cross_group_single_submit_promotes_one_root_with_seed(self):
        target = AvailableChat("oc_target", "研发群", "group", False,
            "https://p3-lark-file.byteimg.com/img/avatar.jpg")
        self.target_scope = FeishuScope("cli_test", target.chat_id, ScopeKind.TOPIC, "omt_fork")
        self.management.query_available_chats.return_value = AvailableChatPage((target,), None)
        self.management.validate_available_chat.side_effect = lambda chat_id: target if chat_id == target.chat_id else None
        self.channel.upload_results.append("img_v3_avatar")
        card = await self.open_form()
        self.management.query_available_chats.assert_not_awaited()
        card = await self.search_form(card)
        self.management.query_available_chats.assert_awaited_once_with(query="", page_token=None, page_size=20)
        select = next(item for item in elements(card.card, "select_static") if item["name"] == "fork_target_v5")
        self.assertEqual(select["options"][0]["icon"],
            {"tag": "custom_icon", "img_key": "img_v3_avatar"})
        self.assertIn("目标聊天的参与者", str(card.card))
        self.assertEqual(len(self.channel.updates), 1)
        self.assertEqual(len(self.channel.upload_calls), 1)
        source, kind = self.channel.upload_calls[0]
        self.assertEqual((source.kind, source.url, kind), ("url", target.avatar_url, "image"))
        self.runtime.fork_exact.assert_not_awaited()
        self.assertEqual(self.channel.send_calls, [])
        self.queue_topic(chat_id=target.chat_id, promoted=True)
        self.management.validate_available_chat.reset_mock()
        await self.app.handle_card_action(self.event(value=callback(card, "创建分支"), form={
            **form_values(card), "fork_name_v5": "新方案", "fork_mode_v5": "group", "fork_target_v5": target.chat_id,
        }))
        self.management.validate_chat_target.assert_awaited_once_with(target.chat_id)
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

    async def test_avatar_upload_failure_preserves_cross_group_creation(self):
        target = AvailableChat("oc_target", "研发群", "group", False,
            "https://p3-lark-file.byteimg.com/img/avatar.jpg")
        self.target_scope = FeishuScope("cli_test", target.chat_id, ScopeKind.TOPIC, "omt_fork")
        self.management.query_available_chats.return_value = AvailableChatPage((target,), None)
        self.management.validate_available_chat.side_effect = lambda chat_id: target if chat_id == target.chat_id else None
        self.channel.upload_results.append(RuntimeError("avatar unavailable"))
        card = await self.open_form()
        card = await self.search_form(card)
        self.assertEqual(next(item for item in elements(card.card, "select_static") if item["name"] == "fork_target_v5")["options"][0]["icon"],
            {"tag": "standard_icon", "token": "group_outlined"})
        self.runtime.fork_exact.assert_not_awaited()
        self.queue_topic(chat_id=target.chat_id)
        self.management.validate_available_chat.reset_mock()
        await self.app.handle_card_action(self.event(value=callback(card, "创建分支"), form={
            **form_values(card), "fork_name_v5": "新方案", "fork_mode_v5": "group", "fork_target_v5": target.chat_id,
        }))
        binding = self.store.active_binding(self.target_scope.key)
        self.assertIsNotNone(binding, self.updates_text())
        self.assertEqual(binding.native_thread_id, self.native.id)
        self.management.validate_chat_target.assert_awaited_once_with(target.chat_id)
        self.assertEqual(len(self.channel.upload_calls), 1)
        self.assertEqual([call[0] for call in self.channel.send_calls], [target.chat_id])
        self.assertEqual(self.store.active_binding(self.scope.key), self.source)
        self.assert_no_execution()

    async def test_current_chat_does_not_require_group_directory_for_creation(self):
        self.management.validate_available_chat.side_effect = AssertionError("Current chat needs no group display lookup")
        card = await self.open_form()
        self.assertNotIn("fork_target_v5", form_values(card))
        self.assertIn("当前聊天", str(card.card))
        self.assertEqual(self.channel.upload_calls, [])
        self.management.validate_available_chat.assert_not_awaited()
        self.queue_topic()
        await self.app.handle_card_action(self.event(value=callback(card, "创建分支"),
            form={**form_values(card), "fork_name_v5": "新方案"}))
        target = self.store.active_binding(self.target_scope.key)
        self.assertIsNotNone(target, self.updates_text())
        self.assertEqual(target.native_thread_id, self.native.id)
        self.management.validate_available_chat.assert_not_awaited()
        self.assertEqual(self.store.active_binding(self.scope.key), self.source)
        self.assert_no_execution()

    async def test_complete_snapshot_prepares_all_avatars_once_and_jumps_without_directory_calls(self):
        groups = tuple(AvailableChat(f"oc_group_{i}", f"研发群 {i}", "group", False,
            f"https://p3-lark-file.byteimg.com/{i}.jpg") for i in range(23))
        self.management.query_available_chats.side_effect = [
            AvailableChatPage(groups[:20], "next"), AvailableChatPage(groups[20:], None),
        ]
        card = await self.open_form()
        self.assertNotIn("fork_target_v5", form_values(card))
        self.management.query_available_chats.assert_not_awaited()
        self.assertEqual(self.channel.upload_calls, [])
        avatar_keys = {group.chat_id: f"img_group_{index}" for index, group in enumerate(groups)}
        self.app._chat_avatars.prepare = AsyncMock(return_value=avatar_keys)
        card = await self.search_form(card)
        options = next(item for item in elements(card.card, "select_static") if item["name"] == "fork_target_v5")["options"]
        self.assertEqual([item["value"] for item in options], [g.chat_id for g in groups[:10]])
        self.assertTrue(all(item["icon"]["tag"] == "custom_icon" for item in options))
        self.app._chat_avatars.prepare.assert_awaited_once()
        self.assertEqual({group.chat_id for group in self.app._chat_avatars.prepare.await_args.args[0]},
                         {group.chat_id for group in groups})
        self.assertEqual(self.management.query_available_chats.await_args_list, [
            call(query="", page_token=None, page_size=20), call(query="", page_token="next", page_size=20),
        ])
        snapshot = callback(card, "跳转")["snapshot"]
        self.management.query_available_chats.reset_mock()
        self.management.validate_available_chat.reset_mock()
        self.app._chat_avatars.prepare.reset_mock()
        before = self.store._connection.total_changes
        for page_index in (2, 1, 0):
            card = await self.jump_form(card, page_index)
            options = next(item for item in elements(card.card, "select_static") if item["name"] == "fork_target_v5")["options"]
            self.assertEqual([item["value"] for item in options],
                             [group.chat_id for group in groups[page_index * 10:(page_index + 1) * 10]])
            self.assertEqual({item["value"]: item["icon"]["img_key"] for item in options},
                             {group.chat_id: avatar_keys[group.chat_id] for group in groups[page_index * 10:(page_index + 1) * 10]})
            self.assertEqual(callback(card, "跳转")["snapshot"], snapshot)
        self.management.query_available_chats.assert_not_awaited()
        self.management.validate_available_chat.assert_not_awaited()
        self.app._chat_avatars.prepare.assert_not_awaited()
        self.assertEqual(self.store._connection.total_changes, before)
        self.assertFalse(any(button["text"]["content"] == "下一页" for button in elements(card.card, "button")))
        self.runtime.fork_exact.assert_not_awaited()
        self.assertEqual(self.channel.send_calls, [])

    async def test_snapshot_jump_preserves_pending_input_selection_and_name_until_new_search(self):
        groups = tuple(AvailableChat(f"oc_group_{i}", f"研发群 {i}", "group", False) for i in range(12))
        self.management.query_available_chats.return_value = AvailableChatPage(groups, None)
        card = await self.open_form()
        card = await self.search_form(card, query="研发")
        self.management.query_available_chats.assert_awaited_once_with(query="研发", page_token=None, page_size=20)
        values = {**form_values(card), "fork_name_v5": "已编辑名称", "fork_mode_v5": "group", "fork_target_v5": groups[0].chat_id,
                  "fork_query_v5": " 产品 ", "fork_chat_id_v5": "oc_private"}
        self.management.query_available_chats.reset_mock()
        before = self.store._connection.total_changes
        updated = await self.jump_form(card, 1, values=values)
        self.management.query_available_chats.assert_not_awaited()
        self.assertEqual(form_values(updated)["fork_name_v5"], "已编辑名称")
        self.assertEqual(form_values(updated)["fork_mode_v5"], "group")
        self.assertEqual(form_values(updated)["fork_target_v5"], groups[0].chat_id)
        self.assertEqual(form_values(updated)["fork_query_v5"], "产品")
        self.assertEqual(form_values(updated)["fork_chat_id_v5"], "oc_private")
        self.assertIn(groups[-1].chat_id, [o["value"] for o in next(item for item in elements(updated.card, "select_static") if item["name"] == "fork_target_v5")["options"]])
        updated = await self.search_form(updated, query=" 产品 ")
        self.management.query_available_chats.assert_awaited_once_with(query="产品", page_token=None, page_size=20)
        self.assertEqual(form_values(updated)["fork_name_v5"], "已编辑名称")
        self.assertEqual(form_values(updated)["fork_target_v5"], "")
        self.assertEqual(form_values(updated)["fork_chat_id_v5"], "oc_private")
        await self.app.handle_card_action(self.event(value=callback(updated, "查找群聊"), form={
            **form_values(updated), "fork_query_v5": "", "fork_name_v5": "",
        }))
        self.management.query_available_chats.assert_awaited_with(query="", page_token=None, page_size=20)
        self.assertEqual(form_values(OutboundCard(card=self.channel.updates[-1][1]))["fork_name_v5"], "")
        self.assertEqual(self.store._connection.total_changes, before)
        self.runtime.fork_exact.assert_not_awaited()
        self.assertEqual(self.channel.send_calls, [])

    async def test_paging_retains_selected_current_group_but_new_search_does_not_inject_it(self):
        current = AvailableChat(self.scope.chat_id, "当前来源群", "group", False)
        others = tuple(AvailableChat(f"oc_other_{i}", f"其他群 {i}", "group", False) for i in range(10))
        self.management.validate_available_chat.side_effect = lambda chat_id: current if chat_id == current.chat_id else None
        self.management.query_available_chats.side_effect = [
            AvailableChatPage((current, *others), None), AvailableChatPage((others[-1],), None),
        ]
        card = await self.search_form(await self.open_form(), query="来源")
        card = await self.jump_form(card, 1, values={
            "fork_mode_v5": "group", "fork_target_v5": current.chat_id, "fork_name_v5": "保留当前来源群",
        })
        picker = next(item for item in elements(card.card, "select_static") if item["name"] == "fork_target_v5")
        self.assertEqual(picker["initial_option"], current.chat_id)
        self.assertEqual({option["value"] for option in picker["options"]}, {current.chat_id, others[-1].chat_id})
        decoded = decode_fork_action(self.scope, callback(card, "创建分支"), form_values(card))
        self.assertEqual((decoded.action, decoded.target_chat_id, decoded.name),
                         ("create", current.chat_id, "保留当前来源群"))
        card = await self.search_form(card, query="其他")
        picker = next(item for item in elements(card.card, "select_static") if item["name"] == "fork_target_v5")
        self.assertNotIn("initial_option", picker)
        self.assertEqual([option["value"] for option in picker["options"]], [others[-1].chat_id])
        self.assertEqual(form_values(card)["fork_name_v5"], "保留当前来源群")
        self.runtime.fork_exact.assert_not_awaited()
        self.assertEqual(self.channel.send_calls, [])

    async def test_oversize_complete_snapshot_offers_narrower_search_without_partial_results(self):
        groups = tuple(AvailableChat(f"oc_group_{i}", "很长的群名" * 800, "group", False) for i in range(10))
        self.management.query_available_chats.return_value = AvailableChatPage(groups, None)
        card = await self.search_form(await self.open_form(), values={
            "fork_name_v5": "保留草稿", "fork_chat_id_v5": "oc_private",
        })
        self.assertEqual(form_values(card)["fork_name_v5"], "保留草稿")
        self.assertEqual(form_values(card)["fork_chat_id_v5"], "oc_private")
        self.assertEqual(form_values(card).get("fork_target_v5", ""), "")
        self.assertIn("fork_query_v5", form_values(card))
        self.assertNotIn("oc_group_", str(card.card))
        self.assertFalse(any(button["text"]["content"] == "跳转" for button in elements(card.card, "button")))
        self.runtime.fork_exact.assert_not_awaited()
        self.assertEqual(self.channel.send_calls, [])

    async def test_midstream_search_failure_discards_partial_results_and_preserves_editable_draft(self):
        group = AvailableChat("oc_target", "研发群", "group", False)
        self.management.query_available_chats.side_effect = [
            AvailableChatPage((group,), "retry-page"), ChatDirectoryError("unavailable", "目录不可用"),
            AvailableChatPage((group,), None),
        ]
        card = await self.search_form(await self.open_form(), values={
            "fork_name_v5": "未完成的分支名", "fork_chat_id_v5": "oc_private",
        })
        self.assertNotIn(group.chat_id, str(card.card))
        self.assertFalse(any(button["text"]["content"] == "跳转" for button in elements(card.card, "button")))
        self.assertIn("fork_query_v5", form_values(card))
        self.assertEqual(form_values(card).get("fork_target_v5", ""), "")
        self.assertEqual(form_values(card)["fork_name_v5"], "未完成的分支名")
        self.assertEqual(form_values(card)["fork_chat_id_v5"], "oc_private")
        card = await self.search_form(card, query="研发")
        self.management.query_available_chats.assert_awaited_with(query="研发", page_token=None, page_size=20)
        self.assertEqual(form_values(card)["fork_target_v5"], "")
        self.assertIn(group.chat_id, [o["value"] for o in next(item for item in elements(card.card, "select_static") if item["name"] == "fork_target_v5")["options"]])
        self.runtime.fork_exact.assert_not_awaited()
        self.assertEqual(self.channel.send_calls, [])

    async def test_list_timeout_or_failure_does_not_block_same_chat_creation(self):
        cancelled = asyncio.Event()
        async def stalled_query(**kwargs):
            if kwargs["page_token"] is None:
                return AvailableChatPage((AvailableChat("oc_partial_timeout", "未收齐的群", "group", False),), "stalled")
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        self.management.query_available_chats.side_effect = stalled_query
        initial = await self.open_form()
        self.management.query_available_chats.assert_not_awaited()
        with patch("netizen_cli.channel_app._CARD_CHAT_QUERY_SECONDS", 0.01):
            card = await self.search_form(initial)
        self.assertTrue(cancelled.is_set())
        self.assertNotIn("oc_partial_timeout", str(card.card))
        self.assertFalse(any(button["text"]["content"] == "跳转" for button in elements(card.card, "button")))
        self.queue_topic()
        await self.app.handle_card_action(self.event(value=callback(card, "创建分支"), form={
            **form_values(card), "fork_name_v5": "新方案", "fork_mode_v5": "current", "fork_chat_id_v5": "oc_residual",
        }))
        self.assertIsNotNone(self.store.active_binding(self.target_scope.key), self.updates_text())

    async def test_empty_platform_pages_are_consumed_before_exposing_complete_empty_snapshot(self):
        self.management.query_available_chats.side_effect = [
            AvailableChatPage((), "first"), AvailableChatPage((), None),
        ]
        card = await self.search_form(await self.open_form())
        self.assertEqual(self.management.query_available_chats.await_args_list, [
            call(query="", page_token=None, page_size=20), call(query="", page_token="first", page_size=20),
        ])
        self.assertIn("fork_query_v5", form_values(card))
        self.assertFalse(any(b["text"]["content"] == "跳转" for b in elements(card.card, "button")))
        self.runtime.fork_exact.assert_not_awaited()
        self.assertEqual(self.channel.send_calls, [])

    async def test_selected_group_is_revalidated_at_submission(self):
        group = AvailableChat("oc_target", "研发群", "group", False)
        self.management.query_available_chats.return_value = AvailableChatPage((group,), None)
        card = await self.search_form(await self.open_form())
        self.management.validate_chat_target.side_effect = ChatTargetError("chat_unavailable", "机器人已退出该群")
        await self.app.handle_card_action(self.event(value=callback(card, "创建分支"), form={
            **form_values(card), "fork_name_v5": "新方案", "fork_mode_v5": "group", "fork_target_v5": group.chat_id,
        }))
        self.runtime.fork_exact.assert_not_awaited()
        self.assertEqual(self.channel.send_calls, [])
        self.assertIn("已退出", self.updates_text())

    async def test_id_target_p2p_normalizes_catch_up_and_ignores_inactive_group(self):
        self.management.validate_chat_target.side_effect = lambda chat_id: ValidatedChatTarget(chat_id, "p2p")
        event = self.confirm_event()
        event.action.form_value.update(
            fork_mode_v5="id", fork_chat_id_v5="oc_private", fork_target_v5="oc_ignored",
        )
        self.queue_topic(chat_id="oc_private", promoted=True)
        with patch.object(self.app, "_resolve_context_anchor", new_callable=AsyncMock) as anchor:
            await self.app.handle_card_action(event)
        anchor.assert_not_awaited()
        self.management.validate_chat_target.assert_awaited_once_with("oc_private")
        destination = FeishuScope("cli_test", "oc_private", ScopeKind.TOPIC, "omt_fork")
        branch = self.store.active_binding(destination.key)
        self.assertIsNotNone(branch, self.updates_text())
        self.assertEqual(branch.message_context_mode, MentionContextMode.CURRENT_ONLY)
        self.assertIsNone(branch.context_anchor)
        self.assertEqual(branch.native_thread_id, self.native.id)
        self.assertEqual(branch.turn_settings, self.source.turn_settings)
        self.assertEqual(self.store.get(self.source.id), self.source)
        self.assert_no_execution()

    async def test_empty_group_or_id_stays_editable_without_validation_or_creation(self):
        for mode, selected, chat_id in (("group", "", "oc_other"), ("id", "oc_other", "  ")):
            with self.subTest(mode=mode):
                event = self.confirm_event()
                event.action.form_value.update(
                    fork_mode_v5=mode, fork_target_v5=selected, fork_chat_id_v5=chat_id,
                )
                await self.app.handle_card_action(event)
                retry = OutboundCard(card=self.channel.updates[-1][1])
                values = form_values(retry)
                self.assertEqual(values["fork_mode_v5"], mode)
                self.assertEqual(values["fork_chat_id_v5"], chat_id)
                self.assertEqual(values.get("fork_target_v5", ""), selected)
                self.assertEqual(values["fork_name_v5"], "新方案")
                self.assertEqual(len(elements(retry.card, "form")), 1)
                self.management.validate_chat_target.assert_not_awaited()
                self.runtime.fork_exact.assert_not_awaited()
                self.assertEqual(self.channel.send_calls, [])
                self.assertIsNone(self.store.active_binding(self.target_scope.key))

    async def test_target_validation_failure_preserves_full_draft_and_renews_retry(self):
        event = self.confirm_event()
        event.action.form_value.update(
            fork_mode_v5="id", fork_chat_id_v5="oc_private", fork_target_v5=self.scope.chat_id,
            fork_query_v5="研发",
        )
        self.management.validate_chat_target.side_effect = ChatTargetError("chat_unavailable", "聊天暂不可访问")
        await self.app.handle_card_action(event)
        self.runtime.fork_exact.assert_not_awaited()
        self.assertEqual(self.channel.send_calls, [])
        restored = OutboundCard(card=self.channel.updates[-1][1])
        values = form_values(restored)
        for key in ("fork_mode_v5", "fork_chat_id_v5", "fork_target_v5", "fork_name_v5", "fork_query_v5"):
            self.assertEqual(values[key], event.action.form_value[key])
        retry_value = callback(restored, "创建分支")
        self.assertNotEqual(retry_value["nonce"], event.action.value["nonce"])
        self.management.validate_chat_target.side_effect = lambda chat_id: ValidatedChatTarget(chat_id, "p2p")
        self.queue_topic(chat_id="oc_private")
        await self.app.handle_card_action(self.event(value=retry_value, form=values))
        self.runtime.fork_exact.assert_awaited_once()
        self.assertIsNotNone(self.store.active_binding(FeishuScope(
            "cli_test", "oc_private", ScopeKind.TOPIC, "omt_fork",
        ).key))

    async def test_current_chat_is_validated_before_fork_too(self):
        self.management.validate_chat_target.side_effect = ChatTargetError("chat_unavailable", "机器人无法访问当前聊天")
        await self.app.handle_card_action(self.confirm_event())
        self.management.validate_chat_target.assert_awaited_once_with(self.scope.chat_id)
        self.runtime.fork_exact.assert_not_awaited()
        self.assertEqual(self.channel.send_calls, [])

    async def test_current_p2p_main_keeps_its_exact_source(self):
        await self.assert_current_p2p_source(ScopeKind.DIRECT)

    async def test_current_p2p_topic_keeps_its_exact_source(self):
        await self.assert_current_p2p_source(ScopeKind.TOPIC)

    async def assert_current_p2p_source(self, source_kind):
        chat_id = f"oc_private_{source_kind.value}"
        self.scope = FeishuScope("cli_test", chat_id, source_kind,
            "omt_private_source" if source_kind is ScopeKind.TOPIC else None)
        source = self.store.create_channel_binding(
            scope=self.scope, project_alias="test", creator_id="ou_source",
        )
        self.store.assign_native_thread_id(source.id, f"native-source-{source_kind.value}")
        self.source = self.store.get(source.id)
        self.native = SimpleNamespace(id=f"native-fork-{source_kind.value}")
        self.target_scope = FeishuScope("cli_test", chat_id, ScopeKind.TOPIC, "omt_fork")
        self.channel.chat_types[chat_id] = "p2p"
        self.management.validate_chat_target.side_effect = lambda target: ValidatedChatTarget(target, "p2p")
        self.queue_topic(promoted=True)
        await self.app.handle_card_action(self.confirm_event())
        branch = self.store.active_binding(self.target_scope.key)
        self.assertIsNotNone(branch, self.updates_text())
        self.assertEqual(branch.native_thread_id, self.native.id)
        self.assertEqual(branch.message_context_mode, MentionContextMode.CURRENT_ONLY)
        self.assertIsNone(branch.context_anchor)
        self.assertEqual(self.store.active_binding(self.scope.key), self.source)
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

    async def test_snapshot_page_rechecks_exact_source_without_querying_directory(self):
        groups = tuple(AvailableChat(f"oc_scope_{index}", f"查询群 {index}", "group", False) for index in range(12))
        self.management.query_available_chats.return_value = AvailableChatPage(groups, None)
        card = await self.search_form(await self.open_form())
        self.management.query_available_chats.reset_mock()
        self.management.validate_available_chat.reset_mock()
        second = self.store.create_channel_binding(scope=self.scope, project_alias="test", creator_id="ou_other")
        denied = await self.jump_form(card, 1)
        self.assertIn("变化", str(denied.card))
        self.assertEqual(self.store.active_binding(self.scope.key), second)
        self.management.query_available_chats.assert_not_awaited()
        self.management.validate_available_chat.assert_not_awaited()
        self.runtime.fork_exact.assert_not_awaited()
        self.assertEqual(self.channel.send_calls, [])

    async def test_target_removed_before_confirm_is_rejected_before_native_creation(self):
        target = AvailableChat("oc_target", "已移除的群", None, None)
        self.management.validate_chat_target.side_effect = ChatTargetError("not_member", "机器人已不在目标群。")
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
