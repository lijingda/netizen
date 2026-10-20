from __future__ import annotations

import asyncio
import copy
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from lark_channel import (
    CardActionPayload, ChatQueueConfig, OutboundCard, PolicyConfig,
    SafetyPipeline, TextBatchConfig,
)
from lark_channel.channel.channel import _card_action_identity

from netizen_cli.bindings import BindingTaskFeedback, SideTopicState
from netizen_cli.codex_runtime import ActiveTurnSnapshot, StopDisposition
from netizen_cli.domain import ActiveState
from tests.support.channel_cards import _card_button_value, _elements
from tests.support.channel_fixtures import side_channel_fixture
from tests.support.channel_messages import FakeMessage


def without_nonces(value):
    if isinstance(value, OutboundCard):
        return without_nonces(value.card)
    if isinstance(value, dict):
        return {key: without_nonces(item) for key, item in value.items() if key != "nonce"}
    if isinstance(value, (list, tuple)):
        return [without_nonces(item) for item in value]
    return value


class StatusCommandsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = await self.enterAsyncContext(side_channel_fixture())
        self.app = self.fixture.app
        self.channel = self.fixture.channel
        self.store = self.fixture.store
        self.runtime = self.fixture.runtime
        self.channel.chat_types["oc_direct"] = "p2p"

    async def panel(self, *, chat_id="oc_direct", chat_type="p2p", thread_id=None, create=True):
        message = FakeMessage("/status", message_id="om_status_request", chat_id=chat_id,
                              chat_type=chat_type, thread_id=thread_id)
        scope = self.app._scope(message)
        if create:
            await self.fixture.create_binding(scope)
        await self.app.handle_message(message)
        card = self.channel.replies[-1][1]
        self.assertIsInstance(card, OutboundCard)
        self.channel.chat_types[chat_id] = chat_type
        self.channel.fetched_messages["om_status_card"] = {
            "data": {"items": [{"message_id": "om_status_card", "chat_id": chat_id, "thread_id": thread_id}]},
        }
        return scope, card

    def event(self, card, label, *, chat_id="oc_direct"):
        return SimpleNamespace(
            message_id="om_status_card", chat_id=chat_id,
            operator=SimpleNamespace(open_id="ou_other_participant"),
            action=SimpleNamespace(tag="button", value=copy.deepcopy(_card_button_value(card, label)), form_value=None),
        )

    async def test_ordinary_aliases_have_the_same_replies_as_slash(self):
        await self.assert_ordinary_aliases_match_slash()

    async def test_git_directory_aliases_have_the_same_replies_as_slash(self):
        with patch("netizen_cli.channel_app.git_branch_status", return_value="main...origin/main [ahead 2]"):
            await self.assert_ordinary_aliases_match_slash()

    async def test_missing_directory_aliases_have_the_same_replies_as_slash(self):
        with patch.object(self.app, "_binding_cwd", return_value=self.fixture.project / "missing"):
            await self.assert_ordinary_aliases_match_slash()

    async def assert_ordinary_aliases_match_slash(self):
        scope, card = await self.panel()
        for label, text in (("查看状态", "/status"), ("停止", "/stop"),
                            ("会话配置", "/config"), ("会话列表", "/sessions")):
            with self.subTest(command=text):
                self.channel.replies.clear()
                await self.app.handle_message(FakeMessage(text, message_id="om_slash"))
                expected = [without_nonces(content) for _, content in self.channel.replies]
                self.channel.replies.clear()
                self.channel.updates.clear()
                with patch.object(self.app, "_control", wraps=self.app._control) as control:
                    await self.app.handle_card_action(self.event(card, label))
                self.assertEqual([without_nonces(content) for _, content in self.channel.replies], expected)
                self.assertEqual({mid for mid, _ in self.channel.replies}, {"om_status_card"})
                control.assert_awaited_once()
                self.assertEqual(control.await_args.args[1].sender_id, "ou_other_participant")
                self.assertEqual(control.await_args.kwargs["expected_binding_id"], self.store.active_binding(scope.key).id)
                self.assertEqual(len(self.channel.updates), 1)
                self.assertEqual(without_nonces(self.channel.updates[0][1]), without_nonces(card))

    async def test_callback_preserves_chat_type_and_reply_location_for_all_scopes(self):
        for chat_type, topic in (("p2p", None), ("group", None), ("p2p", "omt_private"), ("group", "omt_group")):
            with self.subTest(chat_type=chat_type, topic=topic):
                chat_id = "oc_" + chat_type
                _, card = await self.panel(chat_id=chat_id, chat_type=chat_type, thread_id=topic)
                with patch.object(self.channel, "reply", wraps=self.channel.reply) as reply:
                    await self.app.handle_card_action(self.event(card, "会话配置", chat_id=chat_id))
                origin, configured, opts = reply.await_args.args
                self.assertEqual(origin.conversation.thread_id, topic)
                self.assertEqual(origin.conversation.chat_type, chat_type)
                self.assertEqual(opts.reply_to, "om_status_card")
                self.assertEqual(opts.reply_in_thread, topic is not None)
                self.assertEqual(opts.reply_target_gone, "fail")
                names = [item["name"] for item in _elements(configured.card, "select_static")]
                self.assertEqual("config_context_mode" in names, chat_type == "group")

    async def test_controls_are_available_with_progress_cards_off_or_on(self):
        for enabled in (False, True):
            with self.subTest(progress_card_enabled=enabled):
                self.fixture.binding_for(
                    FakeMessage("/status", message_id="om_request"),
                    task_feedback=BindingTaskFeedback(progress_card_enabled=enabled),
                )
                _, card = await self.panel(create=False)
                self.assertEqual([item["text"]["content"] for item in _elements(card.card, "button")],
                                 ["查看状态", "停止", "会话配置", "会话列表"])
                await self.app.handle_card_action(self.event(card, "停止"))
                self.assertIn("当前没有正在执行的任务", self.channel.replies[-1][1])

    async def test_switching_or_removing_current_session_rejects_every_old_button(self):
        scope, card = await self.panel()
        await self.fixture.create_binding(scope)
        for label in ("查看状态", "停止", "会话配置", "会话列表"):
            with self.subTest(label=label):
                self.channel.replies.clear()
                await self.app.handle_card_action(self.event(card, label))
                self.assertEqual(len(self.channel.replies), 1)
                self.assertIn("当前会话已切换", self.channel.replies[0][1])
        self.assertEqual(self.runtime.stop_calls, [])

    async def test_lazy_panel_survives_materialization_and_stops_the_current_turn(self):
        scope, card = await self.panel()
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-one")
        for turn_id in ("turn-one", "turn-two"):
            with self.subTest(turn_id=turn_id):
                self.runtime.active[binding.id] = ActiveTurnSnapshot(
                    binding.id, "native-one", turn_id, "ou_owner", ActiveState.RUNNING,
                )
                with patch.object(self.runtime, "stop_exact", wraps=self.runtime.stop_exact) as stop:
                    await self.app.handle_card_action(self.event(card, "停止"))
                self.assertEqual(stop.await_args.args[0], binding.id)
                self.assertNotIn("expected_turn_id", stop.await_args.kwargs)
                self.assertNotIn("expected_activity_revision", stop.await_args.kwargs)
                self.assertIn("正在中断当前 Codex Turn", self.channel.replies[-1][1])
        self.assertEqual(self.runtime.stop_calls, [binding.id, binding.id])

    async def test_execution_state_rejections_and_goal_stop_match_slash(self):
        scope, card = await self.panel()
        binding = self.store.active_binding(scope.key)
        for state in ("running", "goal", "compacting"):
            for label, text in (("会话配置", "/config"), ("停止", "/stop")):
                with self.subTest(state=state, command=text):
                    self.runtime.active.clear()
                    self.runtime.active_goals.clear()
                    self.runtime.compacting.clear()
                    self.runtime.stop_result = StopDisposition.REQUESTED
                    if state == "running":
                        self.runtime.active[binding.id] = ActiveTurnSnapshot(binding.id, "native-one", "turn-one", "ou_owner", ActiveState.RUNNING)
                    elif state == "goal":
                        self.runtime.active_goals[binding.id] = SimpleNamespace()
                    else:
                        self.runtime.compacting.add(binding.id)
                        self.runtime.stop_result = StopDisposition.COMPACTING
                    self.channel.replies.clear()
                    await self.app.handle_message(FakeMessage(text, message_id="om_slash"))
                    expected = [without_nonces(content) for _, content in self.channel.replies]
                    self.channel.replies.clear()
                    await self.app.handle_card_action(self.event(card, label))
                    self.assertEqual([without_nonces(content) for _, content in self.channel.replies], expected)

    async def test_switch_while_stop_waits_for_scope_lock_cannot_stop_new_session(self):
        scope, card = await self.panel()
        binding = self.store.active_binding(scope.key)
        self.runtime.active[binding.id] = ActiveTurnSnapshot(binding.id, "native-one", "turn-one", "ou_owner", ActiveState.RUNNING)
        entered = asyncio.Event()
        original = self.fixture.management.stop_current_binding

        async def stop(**kwargs):
            entered.set()
            return await original(**kwargs)

        with patch.object(self.fixture.management, "stop_current_binding", side_effect=stop):
            async with self.app._scope_coordinator.hold(scope.key):
                task = asyncio.create_task(self.app.handle_card_action(self.event(card, "停止")))
                await asyncio.wait_for(entered.wait(), 2)
                replacement = self.store.create_channel_binding(scope=scope, project_alias="test", creator_id="ou_other")
            await asyncio.wait_for(task, 2)
        self.assertEqual(self.runtime.stop_calls, [])
        self.assertIn("已切换", self.channel.replies[-1][1])
        self.assertEqual(self.store.active_binding(scope.key).id, replacement.id)

    async def test_snapshot_is_display_only_and_renewal_failure_does_not_replay(self):
        scope, card = await self.panel()
        binding = self.store.active_binding(scope.key)
        self.runtime.active[binding.id] = ActiveTurnSnapshot(binding.id, "native-one", "turn-new", "ou_owner", ActiveState.RUNNING)
        event = self.event(card, "停止")
        event.action.value["snapshot"]["state"] = "idle"
        event.action.value["snapshot"]["native_thread_id"] = "unrelated-display-id"
        self.channel.card_update_success = False
        await self.app.handle_card_action(event)
        self.assertEqual(self.runtime.stop_calls, [binding.id])
        self.assertIn("入口未能更新", self.channel.replies[-1][1])
        self.assertEqual(len(self.channel.updates), 1)

    async def test_sdk_deduplicates_redelivery_but_allows_renewed_button_after_rollover(self):
        scope, card = await self.panel()
        binding = self.store.active_binding(scope.key)
        self.runtime.active[binding.id] = ActiveTurnSnapshot(binding.id, "native-one", "turn-one", "ou_owner", ActiveState.RUNNING)
        pipeline = SafetyPipeline(
            loop=asyncio.get_running_loop(), on_message=lambda _message: None,
            policy=PolicyConfig(group_policy="open", require_mention=False),
            batch_config=TextBatchConfig(delay_ms=0, long_delay_ms=0),
            queue_config=ChatQueueConfig(enabled=False, merge_while_busy=False),
        )
        finished = asyncio.Event()
        calls = []

        async def push(event, label):
            identity = f"card:{event.message_id}:{event.operator.open_id}:" + _card_action_identity(
                CardActionPayload(tag="button", value=event.action.value),
            )

            async def dispatch():
                await self.app.handle_card_action(event)
                calls.append(label)
                finished.set()

            await pipeline.push_action(identity, event.chat_id, dispatch)

        try:
            original = self.event(card, "停止")
            await push(original, "first")
            await asyncio.wait_for(finished.wait(), 2)
            finished.clear()
            renewed = OutboundCard(card=self.channel.updates[-1][1])
            self.runtime.active[binding.id] = replace(self.runtime.active[binding.id], turn_id="turn-two")
            await push(original, "redelivery")
            await push(self.event(renewed, "停止"), "renewed")
            await asyncio.wait_for(finished.wait(), 2)
            self.assertEqual(calls, ["first", "renewed"])
            self.assertEqual(self.runtime.stop_calls, [binding.id, binding.id])
            self.assertEqual(without_nonces(renewed), without_nonces(card))
        finally:
            await pipeline.dispose()

    async def test_real_card_scope_and_operator_are_required_before_dispatch(self):
        _, card = await self.panel(thread_id="omt_original")
        for mutation in ("topic", "app", "chat", "operator", "command", "oversized"):
            with self.subTest(mutation=mutation):
                event = self.event(card, "停止")
                if mutation in {"topic", "app"}:
                    event.action.value["scope"] += "-other"
                elif mutation == "chat":
                    event.chat_id = "oc_foreign"
                elif mutation == "operator":
                    event.operator.open_id = ""
                elif mutation == "command":
                    event.action.value["command"] = "delete"
                else:
                    event.action.value["snapshot"]["progress_lines"] = ["大" * 30_000]
                with patch.object(self.app, "_control", new_callable=AsyncMock) as control:
                    await self.app.handle_card_action(event)
                control.assert_not_awaited()

    async def open_side(self):
        source = FakeMessage("/side", message_id="om_side_source")
        parent = self.fixture.binding_for(source)
        self.fixture.queue_promoted_topic(chat_id="oc_direct", root_id="om_side_root", seed_id="om_side_seed", topic_id="omt_side")
        await self.app.handle_message(source)
        record = self.store.side_topic_for_source(app_id="cli_test", source_message_id=source.id)
        _, card = await self.panel(thread_id=record.topic_id, create=False)
        return parent, record, card

    async def test_side_aliases_share_commands_and_survive_parent_switch(self):
        parent, record, card = await self.open_side()
        await self.fixture.create_binding(self.app._scope(FakeMessage("/new", message_id="om_parent_new")))
        self.runtime.side_snapshots[record.id] = replace(self.runtime.side_snapshot(record.id), turn_id="side-turn-two", turn_state=ActiveState.RUNNING)
        for label, command in (("查看状态", "/status"), ("停止", "/stop")):
            with self.subTest(command=command):
                self.channel.replies.clear()
                await self.app.handle_message(FakeMessage(command, message_id="om_side_slash", thread_id=record.topic_id))
                expected = [without_nonces(content) for _, content in self.channel.replies]
                self.channel.replies.clear()
                await self.app.handle_card_action(self.event(card, label))
                self.assertEqual([without_nonces(content) for _, content in self.channel.replies], expected)
        self.assertEqual(self.runtime.stop_side_calls, [record.id, record.id])
        self.assertEqual(self.runtime.stop_calls, [])
        await self.app.handle_card_action(self.event(card, "结束临时会话"))
        self.assertEqual(self.runtime.close_side_calls, [(record.id, SideTopicState.CLOSED)])
        self.assertIn("Side 已结束", self.channel.replies[-1][1])
        self.assertEqual(self.store.get(parent.id).native_thread_id, parent.native_thread_id)

    async def test_closed_side_panel_does_not_fall_back_to_ordinary_session(self):
        _, record, card = await self.open_side()
        self.store.transition_side_topic(record.id, SideTopicState.CLOSED)
        for label in ("查看状态", "停止", "结束临时会话"):
            await self.app.handle_card_action(self.event(card, label))
            self.assertIn("不会转成普通会话", self.channel.replies[-1][1])
        self.assertEqual(self.runtime.stop_calls, [])
        self.assertEqual(self.runtime.stop_side_calls, [])
        self.assertEqual(self.runtime.close_side_calls, [])

    async def test_missing_side_runtime_uses_existing_expiry_feedback(self):
        _, record, card = await self.open_side()
        self.runtime.side_snapshots.pop(record.id)
        await self.app.handle_card_action(self.event(card, "停止"))
        self.assertEqual(self.store.get_side_topic(record.id).state, SideTopicState.EXPIRED)
        self.assertEqual(self.runtime.stop_side_calls, [])
        self.assertEqual(self.runtime.stop_calls, [])


if __name__ == "__main__":
    unittest.main()
