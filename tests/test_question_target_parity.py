"""The same question interaction contract for existing Binding and Side targets.

These are Channel port tests. Runtime admission and native lifecycle behavior are
covered by the runtime suites, not simulated as guarantees by this fixture.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

from lark_channel import CardActionPayload, ChatQueueConfig, OutboundCard, PolicyConfig, SafetyPipeline, TextBatchConfig
from lark_channel.channel.channel import _card_action_identity

from netizen.bindings import BindingTaskFeedback, SideTopicState
from netizen.cards.questions import decode_question_context, render_question_card
from netizen.channel.question_inputs import CardAnswerOrigin
from netizen.domain import FeishuScope, ScopeKind
from netizen.runtime.contracts import (
    ActiveState, SideSessionClosing, SideSubmission, SideSubmissionAdmission,
    SideTurnActivitySnapshot, SteerRace, Submission, SubmissionAdmission,
    SubmitDisposition, TurnStartFailed,
)
from netizen.user_questions import BindingQuestionTarget, QuestionRequest, SideQuestionTarget, UserQuestion
from tests.support.channel_cards import callback, elements, form_values
from tests.support.channel_fixtures import side_channel_fixture
from tests.support.channel_messages import FakeMessage
from tests.support.channel_results import sent_result


REQUEST = QuestionRequest("native-question-$unselected", (
    UserQuestion("Should we use $unselected?", ("Yes", "No")),
    UserQuestion("Another question?"),
))


@dataclass
class _Case:
    fx: object
    kind: str
    scope: FeishuScope
    parent: object
    recipient: object
    target: BindingQuestionTarget | SideQuestionTarget
    released: list = field(default_factory=list)

    @property
    def submits(self):
        return self.fx.runtime.submit_calls if self.kind == "binding" else self.fx.runtime.submit_side_calls

    @property
    def captures(self):
        return self.fx.runtime.capture_calls if self.kind == "binding" else self.fx.runtime.capture_side_calls

    @property
    def submit_method(self):
        return "submit" if self.kind == "binding" else "submit_side"

    @property
    def capture_method(self):
        return "capture_submission_admission" if self.kind == "binding" else "capture_side_submission_admission"

    def event(self, *, card=None, text="Use the smaller change", scope=None):
        scope = scope or self.scope
        card = card or render_question_card(self.target, REQUEST, 0)
        form = form_values(card)
        form[elements(card.card, "input")[0]["name"]] = text
        self.fx.channel.fetched_messages["om_question"] = {"code": 0, "data": {"items": [{
            "message_id": "om_question", "chat_id": scope.chat_id, "thread_id": scope.topic_id,
        }]}}
        return SimpleNamespace(
            message_id="om_question", chat_id=scope.chat_id,
            operator=SimpleNamespace(open_id="ou_answerer"),
            action=CardActionPayload(tag="button", value=callback(card, "提交回答"), form_value=form),
        )

    def queue_anchor(self):
        self.fx.channel.send_results.append(sent_result(
            "om_answer", chat_id=self.scope.chat_id, thread_id=self.scope.topic_id,
            parent_id="om_question", root_id="om_root",
        ))

    def updated_card(self):
        return OutboundCard(card=self.fx.channel.updates[-1][1])

    def set_submission(self, disposition):
        result = (Submission if self.kind == "binding" else SideSubmission)(
            disposition, self.recipient.id, "native-target", "current-turn",
            (lambda: self.released.append(True)) if disposition is SubmitDisposition.STARTED else None,
        )
        setattr(self.fx.runtime, "submission" if self.kind == "binding" else "side_submission", result)


class QuestionTargetParityTest(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def case(self, kind, *, app_id="cli_test"):
        async with side_channel_fixture() as fx:
            parent_scope = FeishuScope(app_id, "oc_group", ScopeKind.GROUP)
            parent = fx.store.create_binding(scope=parent_scope, project_alias="test", creator_id="ou_creator")
            fx.store.assign_native_thread_id(parent.id, "native-parent")
            parent = fx.store.get(parent.id)
            scope = FeishuScope(app_id, "oc_group", ScopeKind.TOPIC, "omt_target")
            if kind == "binding":
                recipient = fx.store.create_binding(scope=scope, project_alias="test", creator_id="ou_creator")
                fx.store.assign_native_thread_id(recipient.id, "native-target")
                recipient = fx.store.get(recipient.id)
                target = BindingQuestionTarget(recipient.id)
            else:
                recipient = fx.store.create_side_topic(
                    app_id=app_id, chat_id=scope.chat_id, source_message_id="om_side_source",
                    parent_binding_id=parent.id, creator_id="ou_creator", requires_mention=True,
                )
                fx.store.set_side_topic_root(recipient.id, "om_root")
                recipient = fx.store.open_side_topic(recipient.id, scope.topic_id)
                await fx.runtime.create_side(
                    side_id=recipient.id, binding=parent, cwd=fx.project, creator_id="ou_creator",
                )
                await fx.runtime.attach_side_topic(
                    side_id=recipient.id, topic_id=scope.topic_id, root_message_id="om_root",
                )
                target = SideQuestionTarget(recipient.id)
            fx.channel.chat_types[scope.chat_id] = "group"
            fx.channel.get_chat_members = AsyncMock(return_value=[
                SimpleNamespace(id="ou_answerer", name="Answering Person"),
            ])
            case = _Case(fx, kind, scope, parent, recipient, target)
            case.set_submission(SubmitDisposition.STARTED)
            yield case

    async def test_each_question_displays_in_exact_target_topic(self):
        for kind in ("binding", "side"):
            with self.subTest(kind=kind):
                async with self.case(kind) as c:
                    c.fx.channel.send_results.extend([
                        sent_result("om_q1", chat_id=c.scope.chat_id, thread_id=c.scope.topic_id),
                        sent_result("om_q2", chat_id=c.scope.chat_id, thread_id=c.scope.topic_id),
                    ])
                    await c.fx.app.handle_questions(c.target, FakeMessage("go", message_id="om_start"), REQUEST)
                    calls = c.fx.channel.send_calls
                    self.assertEqual(len(calls), 2)
                    self.assertNotEqual(calls[0][2].uuid, calls[1][2].uuid)
                    for index, (chat_id, card, opts) in enumerate(calls):
                        self.assertEqual(chat_id, c.scope.chat_id)
                        context = decode_question_context(callback(card, "提交回答"))
                        self.assertEqual((context.target, context.question_index), (c.target, index))
                        self.assertEqual(opts.reply_to, "om_start")
                        self.assertTrue(opts.reply_in_thread)
                        self.assertEqual(opts.reply_target_gone, "fail")
                    self.assertEqual(c.submits, [])

    async def test_late_answer_starts_same_target_with_real_operator_and_new_anchor(self):
        for kind in ("binding", "side"):
            with self.subTest(kind=kind):
                async with self.case(kind) as c:
                    c.queue_anchor()
                    await c.fx.app.handle_card_action(c.event(text="$chosen use the smaller change"))
                    self.assertEqual(c.captures, [c.recipient.id])
                    submit, = c.submits
                    identity = submit["binding"].id if kind == "binding" else submit["side_id"]
                    self.assertEqual(identity, c.recipient.id)
                    self.assertEqual(submit["owner_id"], "ou_answerer")
                    self.assertEqual(submit["skill_names"], ("chosen",))
                    origin = submit["origin"]
                    self.assertIsInstance(origin, CardAnswerOrigin)
                    self.assertEqual(origin.target, c.target)
                    self.assertEqual(origin.message_id, "om_answer")
                    self.assertEqual(origin.source_card_id, "om_question")
                    self.assertEqual(origin.conversation.thread_id, c.scope.topic_id)
                    prompt = submit["input"]
                    self.assertTrue(prompt.startswith("<send_user_message_question_reply>\n"))
                    fragment = json.loads(prompt.split("\n", 1)[1].split("\n</send_user_message_question_reply>")[0])[0]
                    self.assertEqual(fragment["answer"], "$chosen use the smaller change")
                    self.assertIn("native-question-$unselected", fragment["questionItemId"])
                    self.assertNotIn("$unselected", prompt)
                    context = json.loads(prompt.split("<feishu_card_answer_context>\n")[1].split("\n</feishu_card_answer_context>")[0])
                    self.assertEqual(context["sender"]["display_name"], "Answering Person")
                    self.assertEqual(context["sender"]["open_id"], "ou_answerer")
                    self.assertEqual(context["source_card_id"], "om_question")
                    self.assertEqual(c.fx.channel.fetch_inbound_calls, [])
                    self.assertEqual(c.released, [True])
                    self.assertEqual(c.fx.channel.updates, [])

    async def test_running_answer_passes_exact_admission_and_treats_slash_literally(self):
        for kind in ("binding", "side"):
            with self.subTest(kind=kind):
                async with self.case(kind) as c:
                    admission = (
                        SubmissionAdmission(c.recipient.id, 7, "native-target", "running-turn", 1)
                        if kind == "binding" else
                        SideSubmissionAdmission(c.recipient.id, 7, "native-target", "running-turn")
                    )
                    capture = AsyncMock(return_value=admission)
                    setattr(c.fx.runtime, c.capture_method, capture)
                    c.set_submission(SubmitDisposition.STEERED)
                    c.queue_anchor()
                    await c.fx.app.handle_card_action(c.event(text="/new"))
                    capture.assert_awaited_once_with(c.recipient.id)
                    submit, = c.submits
                    self.assertIs(submit["admission"], admission)
                    self.assertIn('"answer":"/new"', submit["input"])
                    self.assertEqual(submit["skill_names"], ())
                    self.assertEqual(c.fx.channel.reactions[0][0], "om_answer")
                    self.assertEqual(c.released, [])
                    self.assertEqual(c.fx.store.active_binding(c.parent.scope_key).id, c.parent.id)

    async def test_preparation_captures_once_and_runtime_race_is_not_card_retry(self):
        for kind in ("binding", "side"):
            with self.subTest(kind=kind):
                async with self.case(kind) as c:
                    async def members(*args, **kwargs):
                        self.assertEqual(c.captures, [c.recipient.id])
                        return [SimpleNamespace(id="ou_answerer", name="Answering Person")]
                    c.fx.channel.get_chat_members = members
                    submit = AsyncMock(side_effect=SteerRace("原 Turn 已结束，请重发。"))
                    setattr(c.fx.runtime, c.submit_method, submit)
                    c.queue_anchor()
                    await c.fx.app.handle_card_action(c.event())
                    submit.assert_awaited_once()
                    self.assertEqual(c.captures, [c.recipient.id])
                    self.assertEqual(c.fx.channel.replies[-1], ("om_answer", "原 Turn 已结束，请重发。"))
                    self.assertEqual(c.fx.channel.updates, [])

    async def test_foreign_app_chat_or_topic_rejects_before_admission(self):
        for kind in ("binding", "side"):
            for mismatch in ("app", "chat", "topic"):
                with self.subTest(kind=kind, mismatch=mismatch):
                    async with self.case(kind, app_id="cli_other" if mismatch == "app" else "cli_test") as c:
                        scope = FeishuScope(
                            "cli_test", "oc_other" if mismatch == "chat" else c.scope.chat_id,
                            ScopeKind.TOPIC, "omt_sibling" if mismatch == "topic" else c.scope.topic_id,
                        )
                        await c.fx.app.handle_card_action(c.event(scope=scope))
                        self.assertEqual(c.captures, [])
                        self.assertEqual(c.submits, [])
                        self.assertEqual(c.fx.channel.send_calls, [])
                        self.assertIn("位置不一致", str(c.updated_card().card))

    async def test_failed_answer_anchor_never_submits_and_refreshes_retry_nonce(self):
        for kind in ("binding", "side"):
            with self.subTest(kind=kind):
                async with self.case(kind) as c:
                    event = c.event()
                    c.fx.channel.send_results.append(OSError("answer unavailable"))
                    await c.fx.app.handle_card_action(event)
                    self.assertEqual(c.submits, [])
                    updated = c.updated_card()
                    self.assertNotEqual(callback(updated, "提交回答")["nonce"], event.action.value["nonce"])
                    self.assertEqual(decode_question_context(callback(updated, "提交回答")).target, c.target)
                    self.assertTrue(elements(updated.card, "form"))

    async def test_rejection_fallback_stays_on_original_card_without_fresh_send(self):
        for kind in ("binding", "side"):
            with self.subTest(kind=kind):
                async with self.case(kind) as c:
                    capture = AsyncMock(side_effect=SteerRace("原 Turn 已结束，请重发。"))
                    setattr(c.fx.runtime, c.capture_method, capture)
                    c.fx.app._safe_update_card = AsyncMock(return_value=False)
                    c.fx.channel.reply = AsyncMock(wraps=c.fx.channel.reply)
                    await c.fx.app.handle_card_action(c.event())
                    capture.assert_awaited_once_with(c.recipient.id)
                    self.assertEqual(c.submits, [])
                    self.assertEqual(c.fx.channel.send_calls, [])
                    c.fx.channel.reply.assert_awaited_once()
                    origin, content, opts = c.fx.channel.reply.await_args.args
                    self.assertEqual(origin.message_id, "om_question")
                    self.assertEqual(origin.conversation.thread_id, c.scope.topic_id)
                    self.assertIn("回答未提交", content)
                    self.assertEqual(opts.reply_to, "om_question")
                    self.assertTrue(opts.reply_in_thread)
                    self.assertEqual(opts.reply_target_gone, "fail")

    async def test_retry_nonce_deduplicates_redelivery_but_allows_explicit_retry(self):
        for kind in ("binding", "side"):
            with self.subTest(kind=kind):
                async with self.case(kind) as c:
                    original_capture = getattr(c.fx.runtime, c.capture_method)
                    capture = AsyncMock(side_effect=SteerRace("稍后重试"))
                    setattr(c.fx.runtime, c.capture_method, capture)
                    pipeline = SafetyPipeline(
                        loop=asyncio.get_running_loop(), on_message=lambda _: None,
                        policy=PolicyConfig(group_policy="open", require_mention=False),
                        batch_config=TextBatchConfig(delay_ms=0, long_delay_ms=0),
                        queue_config=ChatQueueConfig(enabled=False, merge_while_busy=False),
                    )
                    try:
                        done = asyncio.Event()
                        async def push(event):
                            async def dispatch():
                                await c.fx.app.handle_card_action(event)
                                done.set()
                            identity = f"card:om_question:ou_answerer:{_card_action_identity(event.action)}"
                            await pipeline.push_action(identity, c.scope.chat_id, dispatch)
                        event = c.event()
                        await push(event)
                        await asyncio.wait_for(done.wait(), 1)
                        await push(event)
                        await asyncio.sleep(0.01)
                        capture.assert_awaited_once()
                        retry = c.event(card=c.updated_card())
                        self.assertNotEqual(_card_action_identity(event.action), _card_action_identity(retry.action))
                        setattr(c.fx.runtime, c.capture_method, original_capture)
                        c.queue_anchor()
                        done.clear()
                        await push(retry)
                        await asyncio.wait_for(done.wait(), 1)
                        await push(retry)
                        await asyncio.sleep(0.01)
                        self.assertEqual(len(c.submits), 1)
                        self.assertEqual(len(c.fx.channel.updates), 1)
                    finally:
                        await pipeline.dispose()

    async def test_unknown_native_result_preserves_guidance_without_refresh_or_retry(self):
        for kind in ("binding", "side"):
            with self.subTest(kind=kind):
                async with self.case(kind) as c:
                    submit = AsyncMock(side_effect=TurnStartFailed("结果未确认，请重启服务。"))
                    setattr(c.fx.runtime, c.submit_method, submit)
                    c.queue_anchor()
                    await c.fx.app.handle_card_action(c.event())
                    submit.assert_awaited_once()
                    self.assertEqual(c.fx.channel.replies[-1], ("om_answer", "结果未确认，请重启服务。"))
                    self.assertEqual(c.fx.channel.updates, [])

    async def test_side_answer_ignores_later_parent_metadata_changes(self):
        # Mutate persisted parent metadata only: the Channel must not consult it
        # when routing an answer. Native lifecycle independence has separate tests.
        for change in ("switch", "archive", "delete", "restore"):
            with self.subTest(change=change):
                async with self.case("side") as c:
                    event = c.event()
                    if change == "switch":
                        c.fx.store.create_binding(
                            scope=FeishuScope("cli_test", "oc_group", ScopeKind.GROUP),
                            project_alias="test", creator_id="ou_other",
                        )
                    elif change == "delete":
                        c.fx.store.delete_binding(c.parent.id)
                    else:
                        c.fx.store.archive_binding(c.parent.id)
                        if change == "restore":
                            c.fx.store.activate(scope_key=c.parent.scope_key, binding_id=c.parent.id)
                    c.queue_anchor()
                    await c.fx.app.handle_card_action(event)
                    self.assertEqual(c.captures, [c.recipient.id])
                    self.assertEqual(c.submits[0]["side_id"], c.recipient.id)
                    self.assertEqual(c.fx.runtime.submit_calls, [])
                    self.assertEqual(c.fx.store.list_bindings(c.scope.key), [])

    async def test_unavailable_side_rejects_without_creating_binding(self):
        for state in ("closed", "expired", "missing-runtime", "closing"):
            with self.subTest(state=state):
                async with self.case("side") as c:
                    if state in {"closed", "expired"}:
                        c.fx.store.transition_side_topic(c.recipient.id, SideTopicState(state))
                    elif state == "missing-runtime":
                        c.fx.runtime.side_snapshots.clear()
                    else:
                        c.fx.runtime.capture_side_submission_admission = AsyncMock(
                            side_effect=SideSessionClosing("Side 正在结束，本条消息未执行。"),
                        )
                    await c.fx.app.handle_card_action(c.event())
                    self.assertEqual(c.fx.runtime.submit_calls, [])
                    self.assertEqual(c.fx.runtime.submit_side_calls, [])
                    self.assertEqual(c.fx.channel.send_calls, [])
                    self.assertEqual(c.fx.store.list_bindings(c.scope.key), [])
                    self.assertIn("回答未提交", str(c.updated_card().card))

    async def test_side_close_during_preparation_uses_shared_input_error(self):
        async with self.case("side") as c:
            async def members(*args, **kwargs):
                self.assertEqual(c.captures, [c.recipient.id])
                c.fx.store.transition_side_topic(c.recipient.id, SideTopicState.CLOSED)
                return [SimpleNamespace(id="ou_answerer", name="Answering Person")]
            c.fx.channel.get_chat_members = members
            c.fx.runtime.submit_side = AsyncMock(side_effect=SideSessionClosing("Side 已结束，本条消息未执行。"))
            c.queue_anchor()
            await c.fx.app.handle_card_action(c.event())
            c.fx.runtime.submit_side.assert_awaited_once()
            self.assertEqual(c.captures, [c.recipient.id])
            self.assertEqual(c.fx.channel.replies[-1], ("om_answer", "Side 已结束，本条消息未执行。"))
            self.assertEqual(c.fx.channel.updates, [])
            self.assertEqual(c.fx.store.list_bindings(c.scope.key), [])

    async def test_side_progress_reply_keeps_exact_answer_anchor_and_no_fresh_fallback(self):
        async with self.case("side") as c:
            c.fx.runtime.side_submission = replace(
                c.fx.runtime.side_submission, task_feedback=BindingTaskFeedback(progress_card_enabled=True),
            )
            c.fx.runtime.side_turn_activity_values[c.recipient.id] = SideTurnActivitySnapshot(
                c.recipient.id, "native-target", "current-turn", 1, ActiveState.RUNNING,
                0, True, False, False, (),
            )
            c.fx.channel.reply_results.append(sent_result(
                "om_progress", chat_id=c.scope.chat_id, thread_id=c.scope.topic_id,
                parent_id="om_answer", root_id="om_root",
            ))
            c.fx.channel.reply = AsyncMock(wraps=c.fx.channel.reply)
            c.queue_anchor()
            await c.fx.app.handle_card_action(c.event())
            c.fx.channel.reply.assert_awaited_once()
            origin, _, opts = c.fx.channel.reply.await_args.args
            self.assertEqual(origin.target, c.target)
            self.assertEqual(opts.reply_to, "om_answer")
            self.assertTrue(opts.reply_in_thread)
            self.assertEqual(opts.reply_target_gone, "fail")
            self.assertEqual(c.released, [True])
