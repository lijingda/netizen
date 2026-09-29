from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from lark_channel import ImageContent, OutboundCard, ResourceDescriptor
from openai_codex import ImageInput

from netizen_cli.cards.defaults import decode_defaults_action
from netizen_cli.codex_runtime import SteerRace, Submission, SubmitDisposition
from netizen_cli.domain import MentionContextMode, MessageContextAnchor, ScopeKind
from netizen_cli.message_history import MessageHistoryStats, MessageHistoryWindow
from netizen_cli.session_settings import BindingTaskFeedback, BindingTurnSettings, SessionSettings
from tests.support.channel_cards import callback, elements, form_values, new_form_values
from tests.support.channel_fixtures import channel_fixture
from tests.support.channel_messages import PNG, FakeMessage, plain_prompt_projection


class DefaultsChannelTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = await self.enterAsyncContext(channel_fixture())
        self.app = self.fixture.app
        self.store = self.fixture.store
        self.runtime = self.fixture.runtime
        self.channel = self.fixture.channel
        self.history = self.fixture.message_history
        self.defaults = self.fixture.management.defaults
        self.submissions = []
        self.channel.chat_types["oc_direct"] = "p2p"
        self.submit_patch = patch.object(self.runtime, "submit", side_effect=self.accept)
        self.submit_patch.start()
        self.addCleanup(self.submit_patch.stop)

    async def accept(self, **kwargs):
        self.submissions.append(kwargs)
        binding = kwargs["binding"]
        self.store.assign_native_thread_id(binding.id, "native-" + binding.id)
        commit = kwargs["context_commit"]
        if commit is not None:
            self.store.commit_context_anchor(
                binding_id=binding.id,
                expected_context_revision=commit.expected_context_revision,
                anchor=commit.anchor,
            )
        return Submission(
            SubmitDisposition.STARTED, binding.id, "native-" + binding.id,
            "turn-" + kwargs["origin"].id, lambda: None,
            task_feedback=binding.task_feedback,
        )

    def save(self, *, chat_id="oc_direct", keyword=None, project="test", settings=None):
        return self.store.defaults.save(
            app_id="cli_test", kind="group_name" if keyword is not None else "chat",
            chat_id=None if keyword is not None else chat_id, keyword=keyword,
            project=project, session_settings=settings or SessionSettings(),
            rule_id=None, expected_revision=None,
        )

    def message(self, *, chat_type="group", thread_id=None, message_id="om_prompt", **kwargs):
        return FakeMessage(
            "请分析当前告警", message_id=message_id, chat_id="oc_group",
            chat_type=chat_type, thread_id=thread_id, **kwargs,
        )

    def card_event(self, *, form=None, value=None, chat_id="oc_group", topic_id=None,
                   message_id="om_defaults", fetched_chat=None):
        self.channel.fetched_messages[message_id] = {"data": {"items": [{
            "chat_id": fetched_chat or chat_id, "thread_id": topic_id,
        }]}}
        return SimpleNamespace(
            message_id=message_id, chat_id=chat_id,
            operator=SimpleNamespace(open_id="ou_user"),
            action=SimpleNamespace(tag="button", value=value or {}, form_value=form),
        )

    async def test_auto_creation_preserves_message_scope_and_original_input(self):
        self.save()
        self.save(chat_id="oc_group")
        messages = [
            FakeMessage("分析单聊", message_id="om_direct", sender_id="ou_alice"),
            self.message(message_id="om_group"),
            self.message(thread_id="omt_existing", message_id="om_topic"),
        ]
        for message in messages:
            with self.subTest(message=message.id):
                await self.app.handle_message(message)
                binding = self.store.active_binding(self.app._scope(message).key)
                self.assertIsNotNone(binding)
                submitted = self.submissions[-1]
                self.assertEqual(submitted["binding"].id, binding.id)
                self.assertEqual(submitted["cwd"], self.fixture.project.resolve())
                self.assertIs(submitted["origin"], message)
                self.assertEqual(submitted["owner_id"], message.sender.open_id)
                request, source = plain_prompt_projection(submitted["input"])
                self.assertEqual(request, message.body_text)
                self.assertEqual(source["message_id"], message.id)
                self.assertEqual(source["sender"]["open_id"], message.sender.open_id)
        self.assertEqual(len({item["binding"].id for item in self.submissions}), 3)
        self.assertEqual(self.channel.send_calls, [])

    async def test_private_topic_uses_its_own_scope_and_parent_chat_defaults(self):
        self.save()
        message = FakeMessage("私聊话题", message_id="om_p2p_topic", thread_id="omt_private")
        await self.app.handle_message(message)
        scope = self.app._scope(message)
        self.assertIs(scope.kind, ScopeKind.TOPIC)
        self.assertEqual(self.submissions[0]["binding"].scope_key, scope.key)

    async def test_group_name_rule_uses_parent_name_and_saved_settings(self):
        settings = SessionSettings(task_feedback=BindingTaskFeedback(completion_mention_enabled=False))
        self.save(keyword="ONCALL", settings=settings)
        with patch.object(self.channel, "get_chat_info", AsyncMock(return_value=SimpleNamespace(
            chat_mode="group", name="支付 oncall 值班群",
        ))) as chat_info:
            message = self.message(thread_id="omt_incident")
            await self.app.handle_message(message)
        chat_info.assert_awaited_once_with("oc_group")
        binding = self.submissions[0]["binding"]
        self.assertEqual(SessionSettings.from_binding(binding), settings)
        self.assertEqual(binding.scope_key, self.app._scope(message).key)

    async def test_existing_binding_skips_defaults_even_if_invalid(self):
        message = FakeMessage("继续", message_id="om_continue")
        created = await self.fixture.create_binding(self.app._scope(message))
        self.save(project="removed")
        with patch.object(self.defaults, "resolve", AsyncMock(side_effect=AssertionError("must not match"))):
            await self.app.handle_message(message)
        self.assertEqual(self.submissions[0]["binding"].id, created.binding.id)

    async def test_no_match_preserves_existing_no_session_guidance(self):
        message = FakeMessage("hello", message_id="om_missing")
        await self.app.handle_message(message)
        self.assertEqual(self.submissions, [])
        self.assertIsNone(self.store.active_binding(self.app._scope(message).key))
        self.assertEqual(len(self.channel.replies), 1)
        self.assertIn("/new", self.channel.replies[0][1])
        self.assertIn("尚未执行", self.channel.replies[0][1])

    async def test_invalid_first_match_notifies_then_guides_without_fallback(self):
        self.save(keyword="oncall", project="removed")
        self.save(keyword="oncall")
        with patch.object(self.channel, "get_chat_info", AsyncMock(return_value=SimpleNamespace(
            chat_mode="group", name="oncall",
        ))):
            await self.app.handle_message(self.message())
        self.assertEqual(self.submissions, [])
        self.assertEqual(len(self.channel.replies), 2)
        self.assertIn("默认会话配置不可用", self.channel.replies[0][1])
        self.assertIn("removed", self.channel.replies[0][1])
        self.assertIn("/new", self.channel.replies[1][1])

    async def test_invalid_exact_model_does_not_fall_back_to_name_rule(self):
        self.save(chat_id="oc_group", settings=SessionSettings(BindingTurnSettings("removed", "low", "default")))
        self.save(keyword="oncall")
        await self.app.handle_message(self.message())
        self.assertEqual(self.submissions, [])
        self.assertEqual(len(self.channel.replies), 2)
        self.assertIn("默认会话配置不可用", self.channel.replies[0][1])
        self.assertEqual(self.channel.chat_info_calls, [])

    async def test_group_name_failure_notifies_before_no_session_guidance(self):
        self.save(keyword="oncall")
        with patch.object(self.channel, "get_chat_info", AsyncMock(side_effect=RuntimeError("unavailable"))):
            await self.app.handle_message(self.message())
        self.assertEqual(self.submissions, [])
        self.assertEqual(len(self.channel.replies), 2)
        self.assertIn("默认会话配置不可用", self.channel.replies[0][1])
        self.assertIn("/new", self.channel.replies[1][1])

    async def test_commands_and_unmentioned_group_messages_do_not_auto_create(self):
        self.save()
        self.save(chat_id="oc_group")
        with patch.object(self.defaults, "resolve", AsyncMock(side_effect=AssertionError("must not match"))) as resolve:
            for index, command in enumerate(("/status", "/stop", "/not-a-command", "/new")):
                await self.app.handle_message(FakeMessage(command, message_id=f"om_control_{index}"))
            await self.app.handle_message(self.message(mentioned_bot=False))
        resolve.assert_not_awaited()
        self.assertEqual(self.submissions, [])
        self.assertIsNone(self.store.active_binding(self.app._scope(FakeMessage("", message_id="om_scope")).key))

    async def test_new_form_keeps_native_defaults_instead_of_chat_settings(self):
        self.save(settings=SessionSettings(task_feedback=BindingTaskFeedback(True, False, False)))
        await self.app.handle_message(FakeMessage("/new", message_id="om_new"))
        card = self.channel.replies[-1][1]
        self.assertIsInstance(card, OutboundCard)
        values = new_form_values(card)
        self.assertEqual(values["new_task_reactions"], "task-feedback:v2:off")
        self.assertEqual(values["new_progress_card"], "task-feedback:v2:on")
        self.assertEqual(values["new_completion_mention"], "task-feedback:v2:on")

    async def test_first_catchup_input_is_origin_and_subsequent_input_reads_window(self):
        self.save(chat_id="oc_group", settings=SessionSettings(message_context_mode=MentionContextMode.CATCH_UP))
        first = self.message(message_id="om_first", thread_id="omt_incident")
        scope = self.app._scope(first)
        lower = MessageContextAnchor(first.id, 1000)
        self.history.anchors[first.id] = lower
        await self.app.handle_message(first)
        self.assertEqual(self.history.read_calls, [])
        self.assertEqual(self.history.resolve_calls, [(scope, first.id)])
        submitted = self.submissions[0]
        self.assertEqual(plain_prompt_projection(submitted["input"])[0], first.body_text)
        self.assertEqual(submitted["context_commit"].anchor, lower)
        self.assertEqual(submitted["context_commit"].expected_context_revision, 1)
        binding = self.store.get(submitted["binding"].id)
        self.assertEqual(binding.context_revision, 2)
        second = self.message(message_id="om_second", thread_id="omt_incident")
        upper = MessageContextAnchor(second.id, 2000)
        self.history.window = MessageHistoryWindow(
            lower, upper, (), MessageHistoryStats(1, 2, 0, 0, 0, False, False),
        )
        await self.app.handle_message(second)
        self.assertEqual(self.history.read_calls, [(scope, lower, second.id)])
        self.assertEqual(self.submissions[1]["binding"].id, binding.id)
        self.assertEqual(self.submissions[1]["context_commit"].anchor, upper)
        self.assertEqual(self.submissions[1]["context_commit"].expected_context_revision, 2)
        self.assertEqual(json.loads(self.submissions[1]["input"])["current_message"]["request_text"], second.body_text)

    async def test_failed_first_input_keeps_created_binding_and_context_boundary(self):
        self.save(chat_id="oc_group", settings=SessionSettings(message_context_mode=MentionContextMode.CATCH_UP))
        message = self.message(message_id="om_failure")
        with patch.object(self.runtime, "submit", AsyncMock(side_effect=SteerRace("未执行，请重发"))):
            await self.app.handle_message(message)
        binding = self.store.active_binding(self.app._scope(message).key)
        self.assertIsNotNone(binding)
        self.assertIsNone(binding.native_thread_id)
        self.assertEqual(binding.context_anchor.message_id, message.id)
        self.assertEqual(binding.context_revision, 1)
        self.assertEqual(len(self.store.list_bindings(binding.scope_key)), 1)
        self.assertEqual(self.history.read_calls, [])
        self.assertEqual(len(self.channel.replies), 1)
        self.assertNotIn("/new", self.channel.replies[0][1])
        later = self.message(message_id="om_retry")
        upper = MessageContextAnchor(later.id, 2000)
        self.history.window = MessageHistoryWindow(
            binding.context_anchor, upper, (), MessageHistoryStats(1, 2, 0, 0, 0, False, False),
        )
        await self.app.handle_message(later)
        self.assertEqual(self.submissions[0]["binding"].id, binding.id)
        self.assertEqual(self.history.read_calls[0][2], later.id)

    async def test_first_catchup_input_preserves_current_image_and_explicit_quote(self):
        self.save(chat_id="oc_group", settings=SessionSettings(message_context_mode=MentionContextMode.CATCH_UP))
        self.channel.inbound_messages["om_quote"] = FakeMessage(
            "此前的告警详情", message_id="om_quote", chat_id="oc_group", chat_type="group",
        )
        self.channel.resource_bodies[("om_image", "img_current")] = PNG
        message = FakeMessage(
            "![image](img_current)", message_id="om_image", chat_id="oc_group", chat_type="group",
            raw_content_type="image", content=ImageContent(image_key="img_current"),
            resources=[ResourceDescriptor(type="image", file_key="img_current")],
            reply_id="om_quote",
        )
        await self.app.handle_message(message)
        self.assertEqual(self.history.read_calls, [])
        self.assertEqual(self.channel.fetch_inbound_calls, ["om_quote"])
        self.assertEqual(self.channel.download_resource_calls, [("img_current", "image", "om_image")])
        submitted = self.submissions[0]
        native_input = submitted["input"]
        self.assertEqual(sum(isinstance(item, ImageInput) for item in native_input), 1)
        envelope = json.loads(native_input[-1].text)
        self.assertEqual(envelope["current_message"]["request_text"], "![image](img1)")
        self.assertEqual(envelope["quoted_message"]["text"], "此前的告警详情")
        self.assertIs(submitted["origin"], message)
        self.assertEqual(submitted["context_commit"].anchor.message_id, message.id)

    async def test_private_topic_rejects_catchup_defaults_before_creation(self):
        self.save(settings=SessionSettings(message_context_mode=MentionContextMode.CATCH_UP))
        message = FakeMessage("hello", message_id="om_private", thread_id="omt_private")
        await self.app.handle_message(message)
        self.assertIsNone(self.store.active_binding(self.app._scope(message).key))
        self.assertEqual(self.submissions, [])
        self.assertEqual(self.history.resolve_calls, [])
        self.assertEqual(len(self.channel.replies), 2)

    async def test_manual_creation_during_default_resolution_wins(self):
        self.save()
        message = FakeMessage("hello", message_id="om_auto")
        entered, release = asyncio.Event(), asyncio.Event()
        validate = self.defaults.validate

        async def delayed(rule):
            entered.set()
            await release.wait()
            await validate(rule)

        with patch.object(self.defaults, "validate", delayed):
            pending = asyncio.create_task(self.app.handle_message(message))
            await asyncio.wait_for(entered.wait(), 1)
            try:
                manual = await self.fixture.create_binding(self.app._scope(message))
            finally:
                release.set()
            await asyncio.wait_for(pending, 1)
        self.assertEqual(self.submissions[0]["binding"].id, manual.binding.id)
        self.assertEqual(len(self.store.list_bindings(manual.binding.scope_key)), 1)

    async def test_manual_switch_after_auto_creation_rejects_original_without_retargeting(self):
        self.save()
        message = FakeMessage("hello", message_id="om_auto")
        prepare = self.app._input_preparer.prepare
        entered, release = asyncio.Event(), asyncio.Event()
        captured = []

        async def delayed(**kwargs):
            entered.set()
            await release.wait()
            return await prepare(**kwargs)

        async def checked_submit(**kwargs):
            captured.append(kwargs["binding"].id)
            if not self.store.get(kwargs["binding"].id).active:
                raise SteerRace("active 会话已切换，请重新发送")
            return await self.accept(**kwargs)

        with patch.object(self.app._input_preparer, "prepare", delayed), patch.object(self.runtime, "submit", checked_submit):
            pending = asyncio.create_task(self.app.handle_message(message))
            await asyncio.wait_for(entered.wait(), 1)
            scope = self.app._scope(message)
            original = self.store.active_binding(scope.key)
            try:
                manual = await self.fixture.create_binding(scope)
            finally:
                release.set()
            await asyncio.wait_for(pending, 1)
        self.assertEqual(captured, [original.id])
        self.assertEqual(self.submissions, [])
        self.assertEqual(self.store.active_binding(scope.key).id, manual.binding.id)
        self.assertEqual(len(self.store.list_bindings(scope.key)), 2)
        self.assertIn("重新发送", self.channel.replies[-1][1])

    async def test_defaults_form_saves_parent_chat_updates_and_deletes(self):
        message = FakeMessage("/defaults", message_id="om_open", chat_id="oc_group",
                              chat_type="group", thread_id="omt_config")
        await self.app.handle_message(message)
        self.assertEqual(self.submissions, [])
        card = self.channel.replies[-1][1]
        form = form_values(card)
        project = next(field for field in elements(card.card, "select_static")
                       if field["name"].startswith("defaults_project"))
        form[project["name"]] = project["options"][0]["value"]
        event = self.card_event(form=form, topic_id="omt_config")
        await self.app.handle_card_action(event)
        saved = self.store.defaults.exact("cli_test", "oc_group")
        self.assertIsNotNone(saved)
        self.assertEqual(saved.project, "test")
        self.assertIsNone(self.store.active_binding(self.app._scope(message).key))
        refreshed = OutboundCard(card=self.channel.updates[-1][1])
        updated_form = form_values(refreshed)
        updated_form["defaults_session_completion_mention"] = "task-feedback:v2:off"
        await self.app.handle_card_action(self.card_event(form=updated_form, topic_id="omt_config"))
        updated = self.store.defaults.exact("cli_test", "oc_group")
        self.assertEqual(updated.revision, saved.revision + 1)
        self.assertFalse(updated.session_settings.task_feedback.completion_mention_enabled)
        refreshed = OutboundCard(card=self.channel.updates[-1][1])
        await self.app.handle_card_action(self.card_event(value=callback(refreshed, "删除默认配置"), topic_id="omt_config"))
        self.assertIsNone(self.store.defaults.exact("cli_test", "oc_group"))
        self.assertIn("尚未配置", str(self.channel.updates[-1][1]))

    async def test_defaults_stale_form_cannot_overwrite_newer_configuration(self):
        saved = self.save(chat_id="oc_group")
        scope = self.app._scope(self.message())
        card = await self.app._defaults_card(scope)
        newer = self.store.defaults.save(
            app_id="cli_test", kind="chat", chat_id="oc_group", keyword=None,
            project="test", session_settings=SessionSettings(task_feedback=BindingTaskFeedback(True)),
            rule_id=saved.id, expected_revision=saved.revision,
        )
        await self.app.handle_card_action(self.card_event(form=form_values(card)))
        self.assertEqual(self.store.defaults.exact("cli_test", "oc_group"), newer)
        self.assertIn("刷新", str(self.channel.updates[-1][1]))

    async def test_defaults_catalog_runtime_failure_preserves_form_and_allows_delete(self):
        settings = SessionSettings(BindingTurnSettings("future-model", "ultra", "priority-v2"))
        saved = self.save(chat_id="oc_group", settings=settings)
        message = FakeMessage("/defaults", message_id="om_open", chat_id="oc_group", chat_type="group")
        with patch.object(self.runtime, "model_catalog", AsyncMock(side_effect=RuntimeError("transport unavailable"))):
            await self.app.handle_message(message)
            card = self.channel.replies[-1][1]
            self.assertIsInstance(card, OutboundCard)
            request = decode_defaults_action(self.app._scope(message), {}, form_values(card))
            self.assertEqual(request["project"], saved.project)
            self.assertEqual(request["session_settings"], settings.to_dict())
            await self.app.handle_card_action(self.card_event(value=callback(card, "删除默认配置")))
        self.assertIsNone(self.store.defaults.exact("cli_test", "oc_group"))
        self.assertIn("尚未配置", str(self.channel.updates[-1][1]))

    async def test_defaults_callback_rejects_different_real_chat_or_topic(self):
        saved = self.save(chat_id="oc_group")
        scope = self.app._scope(self.message(thread_id="omt_original"))
        card = await self.app._defaults_card(scope)
        for fetched_chat, topic in (("oc_other", "omt_original"), ("oc_group", "omt_other")):
            with self.subTest(chat=fetched_chat, topic=topic):
                await self.app.handle_card_action(self.card_event(
                    form=form_values(card), fetched_chat=fetched_chat, topic_id=topic,
                ))
                self.assertEqual(self.store.defaults.exact("cli_test", "oc_group"), saved)
                self.assertIsNone(self.store.defaults.exact("cli_test", "oc_other"))


if __name__ == "__main__":
    unittest.main()
