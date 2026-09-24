from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from lark_channel import CardActionPayload, ChatQueueConfig, OutboundCard, PolicyConfig, SafetyPipeline, TextBatchConfig
from lark_channel.channel.channel import _card_action_identity

from netizen.cards.questions import render_question_card
from netizen.channel.question_inputs import CardAnswerOrigin
from netizen.channel.reply_presenter import GoalCardOrigin
from netizen.domain import FeishuScope, MentionContextMode, MessageContextAnchor, ScopeKind
from netizen.message_history import MessageHistoryStats, MessageHistoryWindow
from netizen.runtime.contracts import ContextBoundaryCommitFailed, SteerRace, Submission, SubmitDisposition, TurnStartFailed
from netizen.sdk_gap_adapter import GoalControlError
from netizen.user_questions import BindingQuestionTarget, QuestionRequest, UserQuestion
from tests.support.channel_cards import callback, elements, form_values
from tests.support.channel_fixtures import channel_fixture
from tests.support.channel_messages import FakeMessage
from tests.support.channel_results import sent_result


class QuestionChannelTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fx = await self.enterAsyncContext(channel_fixture())
        self.app, self.runtime, self.channel = self.fx.app, self.fx.runtime, self.fx.channel
        self.scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        self.binding = self.fx.store.create_binding(
            scope=self.scope, project_alias="test", creator_id="ou_creator",
        )
        self.channel.chat_types[self.scope.chat_id] = "p2p"
        self.channel.get_chat_members = AsyncMock(return_value=[
            SimpleNamespace(id="ou_answerer", name="Answering Person"),
        ])
        self.request = QuestionRequest("native-question", (
            UserQuestion("Should we use $unselected?", ("Yes", "No")),
            UserQuestion("Another question?"),
        ))
        self.released = []
        self.runtime.submission = Submission(
            SubmitDisposition.STARTED, self.binding.id, "native-thread", "new-turn",
            lambda: self.released.append(True),
        )

    def event(self, *, card=None, text="Use the smaller change", scope=None):
        scope = scope or self.scope
        card = card or render_question_card(BindingQuestionTarget(self.binding.id), self.request, 0)
        form = form_values(card)
        if text is not None:
            field = elements(card.card, "input")[0]["name"]
            form[field] = text
        self.channel.fetched_messages["om_question"] = {"code": 0, "data": {"items": [{
            "message_id": "om_question", "chat_id": scope.chat_id, "thread_id": scope.topic_id,
        }]}}
        return SimpleNamespace(
            message_id="om_question", chat_id=scope.chat_id,
            operator=SimpleNamespace(open_id="ou_answerer"),
            action=CardActionPayload(tag="button", value=callback(card, "提交回答"), form_value=form),
        )

    def queue_anchor(self, *, scope=None):
        scope = scope or self.scope
        self.channel.send_results.append(sent_result(
            "om_answer", chat_id=scope.chat_id, thread_id=scope.topic_id,
            parent_id="om_question", root_id="om_root",
        ))

    def updated_card(self):
        return OutboundCard(card=self.channel.updates[-1][1])

    async def test_late_answer_starts_ordinary_input_with_real_operator_and_new_anchor(self):
        self.queue_anchor()
        await self.app.handle_card_action(self.event())
        self.assertEqual(self.runtime.capture_calls, [self.binding.id])
        submit, = self.runtime.submit_calls
        self.assertEqual(submit["owner_id"], "ou_answerer")
        self.assertEqual(submit["binding"].id, self.binding.id)
        self.assertIsInstance(submit["origin"], CardAnswerOrigin)
        self.assertEqual(submit["origin"].message_id, "om_answer")
        self.assertEqual(submit["origin"].source_card_id, "om_question")
        self.assertEqual(submit["skill_names"], ())
        prompt = submit["input"]
        self.assertTrue(prompt.startswith("<send_user_message_question_reply>\n"))
        reply = json.loads(prompt.split("\n", 1)[1].split("\n</send_user_message_question_reply>")[0])[0]
        self.assertEqual(reply["questionItemId"], '["request_user_input_async","native-question",0]')
        self.assertEqual(reply["answer"], "Use the smaller change")
        metadata = json.loads(prompt.split("<feishu_card_answer_context>\n")[1].split("\n</feishu_card_answer_context>")[0])
        self.assertEqual(metadata["sender"]["display_name"], "Answering Person")
        self.assertEqual(metadata["source_card_id"], "om_question")
        self.assertEqual(self.channel.fetch_inbound_calls, [])
        self.assertEqual(self.released, [True])
        self.assertEqual(self.channel.updates, [])

    async def test_answer_uses_normal_steer_feedback_and_literal_slash_text(self):
        self.runtime.submission = Submission(SubmitDisposition.STEERED, self.binding.id, "thread", "running-turn")
        self.queue_anchor()
        await self.app.handle_card_action(self.event(text="/new"))
        self.assertEqual(len(self.runtime.submit_calls), 1)
        self.assertIn('"answer":"/new"', self.runtime.submit_calls[0]["input"])
        self.assertEqual(self.fx.store.active_binding(self.scope.key).id, self.binding.id)
        self.assertEqual(self.channel.reactions[0][0], "om_answer")
        self.assertEqual(self.released, [])

    async def test_inactive_binding_rejected_without_admission_or_input(self):
        event = self.event()
        self.fx.store.create_binding(scope=self.scope, project_alias="test", creator_id="ou_other")
        await self.app.handle_card_action(event)
        self.assertEqual(self.runtime.capture_calls, [])
        self.assertEqual(self.runtime.submit_calls, [])
        self.assertEqual(self.channel.send_calls, [])
        self.assertIn("切回", str(self.updated_card().card))
        self.assertNotEqual(callback(self.updated_card(), "提交回答"), event.action.value)

    async def test_callback_cannot_move_question_to_another_scope(self):
        other = FeishuScope("cli_test", "oc_other", ScopeKind.GROUP)
        await self.app.handle_card_action(self.event(scope=other))
        self.assertEqual(self.runtime.capture_calls, [])
        self.assertEqual(self.runtime.submit_calls, [])
        self.assertIn("位置不一致", str(self.updated_card().card))

    async def test_switch_during_answer_preparation_keeps_captured_binding_and_rejects(self):
        self.queue_anchor()
        self.runtime.enforce_active_submission = True
        async def members(*args, **kwargs):
            self.assertEqual(self.runtime.capture_calls, [self.binding.id])
            self.fx.store.create_binding(scope=self.scope, project_alias="test", creator_id="ou_other")
            return [SimpleNamespace(id="ou_answerer", name="Answering Person")]
        self.channel.get_chat_members = members
        await self.app.handle_card_action(self.event())
        self.assertEqual(self.runtime.submit_calls, [])
        self.assertEqual(self.channel.replies[-1][0], "om_answer")
        self.assertIn("会话已切换", self.channel.replies[-1][1])
        self.assertEqual(self.channel.updates, [])

    async def test_turn_race_does_not_start_or_retry(self):
        self.queue_anchor()
        self.runtime.submit = AsyncMock(side_effect=SteerRace("原 Turn 已结束，请重发。"))
        await self.app.handle_card_action(self.event())
        self.runtime.submit.assert_awaited_once()
        self.assertEqual(self.channel.replies[-1], ("om_answer", "原 Turn 已结束，请重发。"))
        self.assertEqual(self.channel.updates, [])

    async def test_native_errors_keep_shared_recovery_guidance_without_card_state(self):
        errors = (
            GoalControlError("无法确认当前 Thread 是否存在 active Goal；本条消息未执行。"),
            TurnStartFailed("Codex Turn 启动结果未确认；服务已停止接收新任务，请重启服务。"),
            ContextBoundaryCommitFailed("输入已被 Codex 接受，但上下文边界未持久化；请重启后对账。"),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.queue_anchor()
                self.runtime.submit = AsyncMock(side_effect=error)
                await self.app.handle_card_action(self.event())
                self.runtime.submit.assert_awaited_once()
                self.assertEqual(self.channel.replies[-1], ("om_answer", str(error)))
                self.assertEqual(self.channel.updates, [])

    async def test_unconfirmed_native_submit_uses_shared_error_feedback(self):
        self.queue_anchor()
        self.runtime.submit = AsyncMock(side_effect=OSError("lost response"))
        with self.assertLogs("netizen.channel_app", level="ERROR"):
            await self.app.handle_card_action(self.event())
        self.runtime.submit.assert_awaited_once()
        self.assertEqual(self.channel.replies[-1][0], "om_answer")
        self.assertIn("lost response", self.channel.replies[-1][1])
        self.assertNotIn("未执行", self.channel.replies[-1][1])
        self.assertEqual(self.channel.updates, [])

    async def test_feedback_failure_after_acceptance_does_not_claim_rejection(self):
        self.queue_anchor()
        self.app._present_ordinary_receipt = AsyncMock(side_effect=OSError("feedback failed"))
        with self.assertLogs("netizen.channel_app", level="ERROR"):
            await self.app.handle_card_action(self.event())
        self.assertEqual(len(self.runtime.submit_calls), 1)
        self.assertEqual(self.channel.replies[-1][0], "om_answer")
        self.assertIn("feedback failed", self.channel.replies[-1][1])
        self.assertNotIn("未执行", self.channel.replies[-1][1])
        self.assertEqual(self.channel.updates, [])

    async def test_preparation_rejection_replies_if_card_update_fails(self):
        self.runtime.capture_error = SteerRace("原 Turn 已结束，请重发。")
        self.app._safe_update_card = AsyncMock(return_value=False)
        await self.app.handle_card_action(self.event())
        self.assertEqual(self.runtime.submit_calls, [])
        self.assertEqual(self.channel.replies[-1], ("om_question", "回答未提交：原 Turn 已结束，请重发。"))

    async def test_wrong_anchor_identity_never_reaches_native(self):
        self.channel.send_results.append(sent_result("om_answer", chat_id=self.scope.chat_id, thread_id="wrong-topic"))
        await self.app.handle_card_action(self.event())
        self.assertEqual(self.runtime.submit_calls, [])
        self.assertTrue(elements(self.updated_card().card, "form"))

    async def test_missing_operator_name_does_not_use_bot_author(self):
        self.channel.get_chat_members.return_value = []
        await self.app.handle_card_action(self.event())
        self.assertEqual(self.runtime.submit_calls, [])
        self.assertEqual(self.channel.send_calls, [])
        self.assertIn("姓名", str(self.updated_card().card))

    async def test_catch_up_uses_answer_anchor_and_keeps_native_reply_unescaped(self):
        group = FeishuScope("cli_test", "oc_group", ScopeKind.GROUP)
        lower = MessageContextAnchor("om_lower", 1000)
        upper = MessageContextAnchor("om_answer", 9000)
        self.binding = self.fx.store.create_binding(
            scope=group, project_alias="test", creator_id="ou_creator",
            message_context_mode=MentionContextMode.CATCH_UP, context_anchor=lower,
        )
        self.fx.message_history.window = MessageHistoryWindow(
            lower, upper, (), MessageHistoryStats(1, 2, 0, 0, 0, False, False),
        )
        self.runtime.submission = Submission(SubmitDisposition.STEERED, self.binding.id, "thread", "turn")
        self.queue_anchor(scope=group)
        await self.app.handle_card_action(self.event(scope=group))
        self.assertEqual(self.fx.message_history.read_calls, [(group, lower, "om_answer")])
        submit, = self.runtime.submit_calls
        self.assertEqual(submit["context_commit"].anchor, upper)
        prompt = submit["input"]
        self.assertTrue(prompt.startswith("<send_user_message_question_reply>\n"))
        fragment = prompt.split("\n", 1)[1].split("\n</send_user_message_question_reply>")[0]
        self.assertEqual(json.loads(fragment)[0]["answer"], "Use the smaller change")
        metadata = json.loads(prompt.split("<feishu_card_answer_context>\n")[1].split("\n</feishu_card_answer_context>")[0])
        self.assertEqual(metadata["current_message"]["sender"]["open_id"], "ou_answerer")
        self.assertNotIn("request_text", metadata["current_message"])
        self.assertEqual(self.channel.fetch_inbound_calls, [])

    async def test_topic_question_and_answer_stay_in_original_topic(self):
        topic = FeishuScope("cli_test", "oc_group", ScopeKind.TOPIC, "omt_topic")
        self.binding = self.fx.store.create_binding(scope=topic, project_alias="test", creator_id="ou_creator")
        self.channel.send_results.append(sent_result(
            "om_answer", chat_id=topic.chat_id, thread_id=topic.topic_id,
            root_id="om_root", parent_id="om_root",
        ))
        await self.app.handle_card_action(self.event(scope=topic))
        self.assertTrue(self.channel.send_calls[0][2].reply_in_thread)
        self.assertEqual(self.channel.send_calls[0][2].reply_target_gone, "fail")
        self.assertEqual(self.runtime.submit_calls[0]["origin"].conversation.thread_id, "omt_topic")

    async def test_display_sends_each_question_using_binding_scope(self):
        self.channel.send_results.extend([
            sent_result("om_q1", chat_id=self.scope.chat_id),
            sent_result("om_q2", chat_id=self.scope.chat_id),
        ])
        await self.app.handle_questions(BindingQuestionTarget(self.binding.id), FakeMessage("go", message_id="om_start"), self.request)
        self.assertEqual(len(self.channel.send_calls), 2)
        self.assertEqual(self.runtime.submit_calls, [])
        self.assertNotEqual(self.channel.send_calls[0][2].uuid, self.channel.send_calls[1][2].uuid)

    async def test_goal_question_can_use_original_prompt_if_goal_card_was_not_delivered(self):
        self.channel.send_results.append(sent_result("om_q", chat_id=self.scope.chat_id))
        origin = GoalCardOrigin(
            None, self.scope, self.binding.id, self.binding.short_id, "test",
            fallback_origin=FakeMessage("/goal finish", message_id="om_goal_input"),
        )
        await self.app.handle_questions(BindingQuestionTarget(self.binding.id), origin, QuestionRequest("question", (UserQuestion("Which one?"),)))
        self.assertEqual(self.channel.send_calls[0][2].reply_to, "om_goal_input")

    async def test_sdk_redelivery_is_deduplicated_and_rejected_card_can_retry(self):
        event = self.event()
        self.runtime.capture_error = SteerRace("稍后重试")
        pipeline = SafetyPipeline(
            loop=asyncio.get_running_loop(), on_message=lambda _: None,
            policy=PolicyConfig(group_policy="open", require_mention=False),
            batch_config=TextBatchConfig(delay_ms=0, long_delay_ms=0),
            queue_config=ChatQueueConfig(enabled=False, merge_while_busy=False),
        )
        try:
            done = asyncio.Event()
            async def dispatch(event):
                await self.app.handle_card_action(event)
                done.set()
            def identity(event):
                return f"card:om_question:ou_answerer:{_card_action_identity(event.action)}"
            await pipeline.push_action(identity(event), self.scope.chat_id, lambda: dispatch(event))
            await asyncio.wait_for(done.wait(), 1)
            await pipeline.push_action(identity(event), self.scope.chat_id, lambda: dispatch(event))
            await asyncio.sleep(0.01)
            self.assertEqual(len(self.runtime.capture_calls), 1)
            retry = self.event(card=self.updated_card())
            self.assertNotEqual(identity(event), identity(retry))
            self.runtime.capture_error = None
            self.queue_anchor()
            done.clear()
            await pipeline.push_action(identity(retry), self.scope.chat_id, lambda: dispatch(retry))
            await asyncio.wait_for(done.wait(), 1)
            self.assertEqual(len(self.runtime.submit_calls), 1)
            update_count = len(self.channel.updates)
            await pipeline.push_action(identity(retry), self.scope.chat_id, lambda: dispatch(retry))
            await asyncio.sleep(0.01)
            self.assertEqual(len(self.runtime.submit_calls), 1)
            self.assertEqual(len(self.channel.updates), update_count)
        finally:
            await pipeline.dispose()

    async def test_native_failure_does_not_refresh_nonce_or_resubmit_on_redelivery(self):
        self.queue_anchor()
        event = self.event()
        self.runtime.submit = AsyncMock(side_effect=TurnStartFailed("结果未确认，请重启服务。"))
        pipeline = SafetyPipeline(
            loop=asyncio.get_running_loop(), on_message=lambda _: None,
            policy=PolicyConfig(group_policy="open", require_mention=False),
            batch_config=TextBatchConfig(delay_ms=0, long_delay_ms=0),
            queue_config=ChatQueueConfig(enabled=False, merge_while_busy=False),
        )
        try:
            done = asyncio.Event()
            async def dispatch():
                try:
                    await self.app.handle_card_action(event)
                finally:
                    done.set()
            identity = f"card:om_question:ou_answerer:{_card_action_identity(event.action)}"
            await pipeline.push_action(identity, self.scope.chat_id, dispatch)
            await asyncio.wait_for(done.wait(), 1)
            await pipeline.push_action(identity, self.scope.chat_id, dispatch)
            await asyncio.sleep(0.01)
            self.runtime.submit.assert_awaited_once()
            self.assertEqual(self.channel.updates, [])
            self.assertEqual(self.channel.replies[-1], ("om_answer", "结果未确认，请重启服务。"))
        finally:
            await pipeline.dispose()
