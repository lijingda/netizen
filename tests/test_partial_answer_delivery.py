"""Stable answer delivery stays separate from native completion and progress preferences."""
from __future__ import annotations

import asyncio
import unittest
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from lark_channel import Identity, OutboundCard, OutboundPost

from netizen_cli.bindings import BindingTaskFeedback
from netizen_cli.channel.reply_presenter import _ReplyCardPresenter
from netizen_cli.codex_runtime import (
    GoalOperationState, GoalOutcome, GoalFinalizationStatus, SideTurnOutcome, Submission, SubmitDisposition, TurnOutcome,
)
from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.partial_answers import PartialAnswer
from netizen_cli.sdk_gap_adapter import GoalStatus
from tests.support.channel_fixtures import channel_fixture
from tests.support.channel_messages import FakeChannel, FakeMessage
from tests.support.channel_results import (
    completed_turn_result, file_change_item, goal_activity_snapshot, native_goal,
    sent_result, side_turn_activity_snapshot, turn_activity_snapshot,
)
from tests.support.channel_runtime import StubRuntime


def answer(text="first stable answer", item="partial-one", *, thread="native-one", turn="turn-one"):
    return PartialAnswer(thread_id=thread, turn_id=turn, item_id=item, text=text)


class PartialAnswerPresenterTest(unittest.IsolatedAsyncioTestCase):
    async def test_no_card_delivers_once_and_terminal_fills_only_missing_exact_items(self):
        for side in (False, True):
            with self.subTest(side=side):
                channel, runtime = FakeChannel(), StubRuntime()
                presenter = _ReplyCardPresenter(channel, runtime, poll_seconds=0.001)
                self.addAsyncCleanup(presenter.close)
                owner = "side-one" if side else "binding-one"
                thread = "native-side-1" if side else "native-one"
                turn = "side-turn-1" if side else "turn-one"
                first = answer(thread=thread, turn=turn)
                second = answer("second", "partial-two", thread=thread, turn=turn)
                snapshot = side_turn_activity_snapshot(side_id=owner) if side else turn_activity_snapshot(binding_id=owner)
                snapshot = replace(snapshot, partial_answers=(first,), revision=2)
                values = runtime.side_turn_activity_values if side else runtime.turn_activity_values
                values[owner] = snapshot
                delivered = asyncio.Event()
                posts = []

                async def reply(post):
                    posts.append(post)
                    delivered.set()
                    return sent_result("om_partial", chat_id="oc_direct")

                started = await (presenter.start_side(side_id=owner, thread_id=thread, turn_id=turn,
                    origin=FakeMessage("work", message_id="om_origin"), progress_enabled=False, reply_partial=reply) if side else
                    presenter.start(binding_id=owner, thread_id=thread, turn_id=turn,
                    origin=FakeMessage("work", message_id="om_origin"), progress_enabled=False, reply_partial=reply))
                self.assertTrue(started)
                await asyncio.wait_for(delivered.wait(), 1)
                self.assertEqual(channel.replies, [])
                self.assertEqual(channel.reactions, [])
                self.assertEqual(posts, [OutboundPost(markdown="**阶段性答案**\n\nfirst stable answer")])
                delivery = await presenter.prepare_partial_answers(
                    owner_id=owner, thread_id=thread, turn_id=turn, side=side, reply=reply,
                )
                confirmed = await presenter.deliver_partial_answers(
                    delivery, (first, first, answer(thread="wrong"), second),
                )
                self.assertTrue(confirmed)
                self.assertEqual(len(posts), 2)
                self.assertEqual(posts[-1].markdown, "**阶段性答案**\n\nsecond")
                self.assertEqual(channel.updates, [])

    async def test_lost_send_receipt_is_not_replayed_after_observation_unavailable(self):
        channel, runtime = FakeChannel(), StubRuntime()
        presenter = _ReplyCardPresenter(channel, runtime, poll_seconds=0.001)
        self.addAsyncCleanup(presenter.close)
        partial = answer()
        runtime.turn_activity_values["binding-one"] = replace(
            turn_activity_snapshot(binding_id="binding-one"), partial_answers=(partial,), revision=2,
        )
        attempted = asyncio.Event()
        attempts = []

        async def reply(post):
            attempts.append(post)
            attempted.set()
            raise TimeoutError("response lost after publication")

        await presenter.start(binding_id="binding-one", thread_id="native-one", turn_id="turn-one",
            origin=FakeMessage("work", message_id="om_origin"), progress_enabled=False, reply_partial=reply)
        await asyncio.wait_for(attempted.wait(), 1)
        await presenter.park_unavailable(binding_id="binding-one", thread_id="native-one", turn_id="turn-one")
        delivery = await presenter.prepare_partial_answers(owner_id="binding-one", thread_id="native-one",
            turn_id="turn-one", side=False, reply=reply)
        confirmed = await presenter.deliver_partial_answers(delivery, (partial,))
        self.assertFalse(confirmed)
        self.assertEqual(len(attempts), 1)

    async def test_inflight_partial_finishes_before_terminal_and_stops_remaining_running_sends(self):
        channel, runtime = FakeChannel(), StubRuntime()
        presenter = _ReplyCardPresenter(channel, runtime, poll_seconds=0.001)
        self.addAsyncCleanup(presenter.close)
        partials = (answer(), answer("second", "two"))
        runtime.turn_activity_values["binding-one"] = replace(
            turn_activity_snapshot(binding_id="binding-one"), partial_answers=partials, revision=2,
        )
        entered, release = asyncio.Event(), asyncio.Event()
        attempts = []

        async def reply(post):
            attempts.append(post)
            entered.set()
            if len(attempts) == 1:
                await release.wait()
            return sent_result("om_partial", chat_id="oc_direct")

        await presenter.start(binding_id="binding-one", thread_id="native-one", turn_id="turn-one",
            origin=FakeMessage("work", message_id="om_origin"), progress_enabled=False, reply_partial=reply)
        await asyncio.wait_for(entered.wait(), 1)
        preparing = asyncio.create_task(presenter.prepare_partial_answers(owner_id="binding-one",
            thread_id="native-one", turn_id="turn-one", side=False, reply=reply))
        session = presenter._sessions[("binding-one", "native-one", "turn-one")]
        await asyncio.wait_for(session.stopped.wait(), 1)
        release.set()
        delivery = await asyncio.wait_for(preparing, 1)
        self.assertEqual(len(attempts), 1)
        self.assertIs(delivery.attempted_partials, session.attempted_partials)
        self.assertIs(delivery.confirmed_partials, session.confirmed_partials)
        self.assertIn(("binding-one", "native-one", "turn-one"), presenter._sessions)
        self.assertTrue(await presenter.deliver_partial_answers(delivery, partials))
        self.assertEqual(len(attempts), 2)

    async def test_formatting_failure_before_transport_can_be_retried_at_terminal(self):
        channel, runtime = FakeChannel(), StubRuntime()
        presenter = _ReplyCardPresenter(channel, runtime, poll_seconds=10)
        self.addAsyncCleanup(presenter.close)
        partial = answer()
        runtime.turn_activity_values["binding-one"] = turn_activity_snapshot(binding_id="binding-one")
        reply = AsyncMock(return_value=sent_result("om_partial", chat_id="oc_direct"))
        await presenter.start(binding_id="binding-one", thread_id="native-one", turn_id="turn-one",
            origin=FakeMessage("work", message_id="om_origin"), progress_enabled=False, reply_partial=reply)
        session = presenter._sessions[("binding-one", "native-one", "turn-one")]
        with patch("netizen_cli.channel.reply_presenter.OutboundPost", side_effect=ValueError("format failed")):
            await presenter._deliver_partial_answers(session, (partial,))
        reply.assert_not_awaited()
        delivery = await presenter.prepare_partial_answers(owner_id="binding-one", thread_id="native-one",
            turn_id="turn-one", side=False, reply=reply)
        confirmed = await presenter.deliver_partial_answers(delivery, (partial,))
        self.assertTrue(confirmed)
        reply.assert_awaited_once()

    async def test_receipt_check_failure_or_timeout_is_bounded_and_never_replays_the_post(self):
        for stalled in (False, True):
            with self.subTest(stalled=stalled):
                channel, runtime = FakeChannel(), StubRuntime()
                presenter = _ReplyCardPresenter(channel, runtime,
                    poll_seconds=10, operation_timeout_seconds=0.01)
                self.addAsyncCleanup(presenter.close)
                runtime.turn_activity_values["binding-one"] = turn_activity_snapshot(binding_id="binding-one")
                reply = AsyncMock(return_value=sent_result("om_partial", chat_id="oc_direct"))

                async def validate(result):
                    if stalled:
                        await asyncio.Event().wait()
                    raise RuntimeError("receipt lookup unavailable")

                await presenter.start(binding_id="binding-one", thread_id="native-one", turn_id="turn-one",
                    origin=FakeMessage("work", message_id="om_origin"), progress_enabled=False,
                    reply_partial=reply, validate_reply=validate)
                session = presenter._sessions[("binding-one", "native-one", "turn-one")]
                await asyncio.wait_for(presenter._deliver_partial_answers(session, (answer(),)), timeout=1)
                delivery = await presenter.prepare_partial_answers(owner_id="binding-one",
                    thread_id="native-one", turn_id="turn-one", side=False, reply=reply)
                confirmed = await presenter.deliver_partial_answers(delivery, (answer(),))
                self.assertFalse(confirmed)
                reply.assert_awaited_once()


    async def test_initial_card_failure_keeps_running_partial_delivery_without_a_card(self):
        for side in (False, True):
            for failure in ("send", "render", "missing-id", "validation"):
                if side and failure == "validation":
                    continue
                with self.subTest(side=side, failure=failure):
                    channel, runtime = FakeChannel(), StubRuntime()
                    presenter = _ReplyCardPresenter(channel, runtime, poll_seconds=0.001)
                    self.addAsyncCleanup(presenter.close)
                    owner = "side-one" if side else "binding-one"
                    thread = "native-side-1" if side else "native-one"
                    turn = "side-turn-1" if side else "turn-one"
                    snapshot = (side_turn_activity_snapshot(side_id=owner) if side
                                else turn_activity_snapshot(binding_id=owner))
                    partial = answer(thread=thread, turn=turn)
                    snapshot = replace(snapshot, partial_answers=(partial,), revision=2)
                    values = runtime.side_turn_activity_values if side else runtime.turn_activity_values
                    values[owner] = snapshot
                    delivered = asyncio.Event()
                    posts = []

                    async def reply_partial(post):
                        posts.append(post)
                        delivered.set()
                        return sent_result("om_partial", chat_id="oc_direct")

                    reply = AsyncMock(return_value=(object() if failure == "missing-id" else
                        sent_result("om_untrusted" if failure == "validation" else "om_card", chat_id="oc_direct")))
                    if failure == "send":
                        reply.side_effect = RuntimeError("initial card unavailable")
                    context = (patch("netizen_cli.channel.reply_presenter.turn_progress_card",
                                     side_effect=ValueError("card render failed"))
                               if failure == "render" else nullcontext())
                    with context:
                        common = dict(thread_id=thread, turn_id=turn, reply=reply,
                            origin=FakeMessage("work", message_id="om_origin"),
                            progress_enabled=True, reply_partial=reply_partial)
                        started = (await presenter.start_side(side_id=owner, **common) if side else
                            await presenter.start(binding_id=owner, **common,
                                validate_reply=AsyncMock(return_value=False) if failure == "validation" else None))
                    self.assertTrue(started)
                    await asyncio.wait_for(delivered.wait(), 1)
                    sessions = presenter._side_sessions if side else presenter._sessions
                    self.assertIsNone(sessions[(owner, thread, turn)].message_id)
                    self.assertEqual(posts, [OutboundPost(markdown="**阶段性答案**\n\nfirst stable answer")])
                    self.assertEqual(channel.updates, [])

    async def test_prepare_receipts_survive_failed_terminal_card_then_fill_only_missing_items(self):
        channel, runtime = FakeChannel(), StubRuntime()
        presenter = _ReplyCardPresenter(channel, runtime, poll_seconds=10)
        self.addAsyncCleanup(presenter.close)
        runtime.turn_activity_values["binding-one"] = turn_activity_snapshot(binding_id="binding-one")
        reply = AsyncMock(return_value=sent_result("om_partial", chat_id="oc_direct"))
        common = dict(binding_id="binding-one", thread_id="native-one", turn_id="turn-one",
            origin=FakeMessage("work", message_id="om_origin"), reply_partial=reply)
        await presenter.start(**common, progress_enabled=False)
        first, second = answer(), answer("second", "two")
        session = presenter._sessions[("binding-one", "native-one", "turn-one")]
        await presenter._deliver_partial_answers(session, (first,))
        await presenter.start(**common, progress_enabled=True,
            reply=AsyncMock(return_value=sent_result("om_card", chat_id="oc_direct")))
        delivery = await presenter.prepare_partial_answers(owner_id="binding-one", thread_id="native-one",
            turn_id="turn-one", side=False, reply=reply)
        self.assertEqual(reply.await_count, 1)

        def render(snapshot):
            raise ValueError("terminal card too large")

        failed = await presenter.finish(binding_id="binding-one", thread_id="native-one", turn_id="turn-one",
            activity=None, render=render)
        self.assertIsNone(failed)
        self.assertEqual(presenter._sessions, {})
        self.assertTrue(await presenter.deliver_partial_answers(delivery, (first, second)))
        self.assertEqual(reply.await_count, 2)
        self.assertEqual(reply.call_args.args[0].markdown, "**阶段性答案**\n\nsecond")

    async def test_terminal_delivery_budget_covers_whole_batch_and_does_not_replay_timeout(self):
        presenter = _ReplyCardPresenter(FakeChannel(), StubRuntime(), operation_timeout_seconds=1)
        self.addAsyncCleanup(presenter.close)
        entered = asyncio.Event()
        posts = []

        async def reply(post):
            posts.append(post)
            entered.set()
            await asyncio.Event().wait()

        delivery = await presenter.prepare_partial_answers(owner_id="binding-one", thread_id="native-one",
            turn_id="turn-one", side=False, reply=reply)
        partials = (answer(), answer("second", "two"), answer("third", "three"))
        result = await asyncio.wait_for(
            presenter.deliver_partial_answers(delivery, partials, timeout_seconds=0.01), 1,
        )
        self.assertFalse(result)
        self.assertTrue(entered.is_set())
        self.assertEqual(len(posts), 1)
        self.assertFalse(await presenter.deliver_partial_answers(delivery, partials, timeout_seconds=0))
        self.assertEqual(len(posts), 1)
        self.assertTrue(await presenter.deliver_partial_answers(delivery, (), timeout_seconds=0))
        self.assertFalse(await presenter.deliver_partial_answers(
            delivery, (answer(thread="wrong-thread"),), timeout_seconds=0,
        ))
        self.assertEqual(delivery.attempted_partials, {("native-one", "turn-one", "partial-one")})
        self.assertEqual(delivery.confirmed_partials, set())

    async def test_goal_delivery_allows_multiple_physical_turns_but_requires_exact_thread(self):
        presenter = _ReplyCardPresenter(FakeChannel(), StubRuntime())
        self.addAsyncCleanup(presenter.close)
        reply = AsyncMock(return_value=sent_result("om_partial", chat_id="oc_direct"))
        delivery = await presenter.prepare_partial_answers(owner_id="binding-one", thread_id="native-one",
            turn_id=None, side=False, reply=reply)
        first, second = answer(turn="physical-one"), answer("second", turn="physical-two")
        partials = (first, answer("wrong thread", thread="another-thread"), second, first)
        self.assertTrue(await presenter.deliver_partial_answers(delivery, partials))
        self.assertEqual([call.args[0].markdown for call in reply.await_args_list],
            ["**阶段性答案**\n\nfirst stable answer", "**阶段性答案**\n\nsecond"])
        self.assertTrue(await presenter.deliver_partial_answers(delivery, partials))
        self.assertEqual(reply.await_count, 2)


class PartialAnswerChannelTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = await self.enterAsyncContext(channel_fixture())
        await self.fixture.new()
        self.app = self.fixture.app
        self.runtime = self.fixture.runtime
        self.channel = self.fixture.channel
        self.binding = self.fixture.store.active_binding(FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT).key)
        self.app._progress_cards = _ReplyCardPresenter(self.channel, self.runtime, poll_seconds=0.001)

    async def test_running_without_progress_sends_answer_without_completion_side_effects(self):
        snapshot = replace(turn_activity_snapshot(binding_id=self.binding.id), partial_answers=(answer(),), revision=2)
        self.runtime.turn_activity_values[self.binding.id] = snapshot
        prompt = FakeMessage("work", message_id="om_origin")
        self.runtime.submission = Submission(SubmitDisposition.STARTED, self.binding.id,
            "native-one", "turn-one", lambda: None)
        delivered = asyncio.Event()

        async def reply(message, post, opts=None):
            self.channel.replies.append((message.id, post))
            delivered.set()
            return sent_result("om_partial", chat_id="oc_direct")

        with patch.object(self.channel, "reply", side_effect=reply), patch.object(
            self.app, "_send_completion_reply", new_callable=AsyncMock,
        ) as completion:
            await self.app.handle_message(prompt)
            await asyncio.wait_for(delivered.wait(), 1)
            completion.assert_not_awaited()
        self.assertEqual(self.channel.reactions, [("om_origin", "Typing")])
        self.assertEqual(len(self.channel.replies), 1)
        self.assertIsInstance(self.channel.replies[0][1], OutboundPost)
        self.assertEqual(self.channel.replies[0][1].mentions, [])

    async def test_no_progress_partial_only_completion_is_truthful_for_confirmed_and_unknown_delivery(self):
        for confirmed in (True, False):
            with self.subTest(confirmed=confirmed):
                self.channel.replies.clear()
                self.channel.reply_results.append(sent_result("om_partial", chat_id="oc_direct") if confirmed else object())
                outcome = TurnOutcome(binding_id=self.binding.id, thread_id="native-one", turn_id="turn-one",
                    owner_id="ou_user", origin=FakeMessage("work", message_id="om_origin"),
                    result=completed_turn_result(final_response=None), partial_answers=(answer(),))
                await self.app.handle_completion(outcome)
                self.assertEqual(len(self.channel.replies), 2)
                self.assertIsInstance(self.channel.replies[0][1], OutboundPost)
                closing = self.channel.replies[-1][1]
                text = closing.markdown if isinstance(closing, OutboundPost) else closing
                self.assertIn("答案见阶段性答案" if confirmed else "投递未确认", text)
                self.assertNotIn("未产生文本回复", text)
                self.assertNotIn("first stable answer", text)

    async def test_missing_progress_card_sends_partial_before_final(self):
        self.channel.reply_results.append(sent_result("om_partial", chat_id="oc_direct"))
        outcome = TurnOutcome(binding_id=self.binding.id, thread_id="native-one", turn_id="turn-one",
            owner_id="ou_user", origin=FakeMessage("work", message_id="om_origin"),
            result=completed_turn_result(final_response="final answer"), partial_answers=(answer(),),
            task_feedback=BindingTaskFeedback(progress_card_enabled=True))
        await self.app.handle_completion(outcome)
        self.assertEqual(len(self.channel.replies), 2)
        self.assertEqual(self.channel.replies[0][1], OutboundPost(markdown="**阶段性答案**\n\nfirst stable answer"))
        closing = self.channel.replies[1][1]
        self.assertEqual(closing.markdown if isinstance(closing, OutboundPost) else closing, "final answer")

    async def test_partial_only_unknown_post_never_claims_answer_delivery(self):
        outcome = TurnOutcome(binding_id=self.binding.id, thread_id="native-one", turn_id="turn-one",
            owner_id="ou_user", origin=FakeMessage("work", message_id="om_origin"),
            result=completed_turn_result(final_response=None), partial_answers=(answer(),),
            task_feedback=BindingTaskFeedback(progress_card_enabled=True))
        self.channel.reply_results.append(SimpleNamespace(success=False))
        await self.app.handle_completion(outcome)
        self.assertEqual(len(self.channel.replies), 2)
        closing = self.channel.replies[-1][1]
        text = closing.markdown if isinstance(closing, OutboundPost) else closing
        self.assertIn("投递未确认", text)
        self.assertNotIn("答案见阶段性答案", text)

    async def test_goal_partial_preserves_unknown_finalization_explanation(self):
        outcome = GoalOutcome(binding_id=self.binding.id, thread_id="native-one", logical_turn_id="goal-one",
            owner_id="ou_user", origin=FakeMessage("work", message_id="om_origin"), goal=native_goal(status=GoalStatus.COMPLETE),
            final_physical_turn_id="turn-one", final_turn_status="completed",
            finalization=GoalFinalizationStatus.UNKNOWN, finalization_error=RuntimeError("clear result lost"),
            partial_answers=(answer(),))
        await self.app.handle_completion(outcome)
        card = self.channel.replies[-1][1]
        self.assertIsInstance(card, OutboundCard)
        self.assertIn("自动结束结果未知", str(card.card))
        self.assertNotIn("first stable answer", str(card.card))
        self.assertEqual(self.channel.replies[0][1].markdown,
                         "**阶段性答案**\n\nfirst stable answer")
        self.assertIn("自动结束结果未知", str(self.channel.replies[1][1]))
        self.assertNotIn("未产生文本回复", str(card.card))

    async def test_failed_and_interrupted_fallback_sends_partial_before_status(self):
        for status in ("failed", "interrupted"):
            with self.subTest(status=status):
                self.channel.replies.clear()
                outcome = TurnOutcome(binding_id=self.binding.id, thread_id="native-one", turn_id="turn-one",
                    owner_id="ou_user", origin=FakeMessage("work", message_id="om_origin"),
                    result=SimpleNamespace(status=status, final_response=None, items=[]),
                    partial_answers=(answer(),), task_feedback=BindingTaskFeedback(progress_card_enabled=True))
                await self.app.handle_completion(outcome)
                self.assertEqual(len(self.channel.replies), 2)
                self.assertEqual(self.channel.replies[0][1], OutboundPost(markdown="**阶段性答案**\n\nfirst stable answer"))
                closing = self.channel.replies[-1][1]
                self.assertIsInstance(closing, (str, OutboundPost))
                text = closing.markdown if isinstance(closing, OutboundPost) else closing
                self.assertIn("中断" if status == "interrupted" else "未完成", text)
                self.assertNotIn("first stable answer", text)

    async def test_goal_failed_fallback_send_does_not_claim_partial_delivery(self):
        self.channel.reply_results.append(SimpleNamespace(success=False))
        outcome = GoalOutcome(binding_id=self.binding.id, thread_id="native-one", logical_turn_id="goal-one",
            owner_id="ou_user", origin=FakeMessage("work", message_id="om_origin"), goal=native_goal(status=GoalStatus.COMPLETE),
            final_physical_turn_id="turn-one", final_turn_status="completed", partial_answers=(answer(),))
        await self.app.handle_completion(outcome)
        self.assertEqual(len(self.channel.replies), 3)
        self.assertIsInstance(self.channel.replies[0][1], OutboundPost)
        last = self.channel.replies[1][1]
        text = last.markdown if isinstance(last, OutboundPost) else last
        self.assertIn("投递未确认", text)
        self.assertNotIn("答案见阶段性答案", text)
        self.assertIsInstance(self.channel.replies[-1][1], OutboundCard)

    async def test_oversized_goal_partial_still_updates_same_control_card_to_terminal(self):
        from netizen_cli.cards import goal_generation
        from netizen_cli.channel.reply_presenter import GoalCardOrigin
        from netizen_cli.domain import ReplyCardPartialAnswerModule
        for prior_delivered, final_response in ((False, None), (True, None), (True, "final result")):
            with self.subTest(prior_delivered=prior_delivered, final_response=final_response):
                goal = native_goal()
                scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
                await self.fixture.register_goal_card(scope=scope, binding=self.binding, goal=goal,
                    message_id="om_goal_large", runtime_state=GoalOperationState.RUNNING.value)
                origin = GoalCardOrigin(message_id="om_goal_large", scope=scope,
                    binding_id=self.binding.id, short_id=self.binding.short_id,
                    project_alias=self.binding.project_alias, goal_generation=goal_generation(goal))
                previous = answer("previously displayed small answer")
                if prior_delivered:
                    current = self.app._progress_cards.goal_projection(
                        source_id="om_goal_large", generation=goal_generation(goal))
                    self.assertTrue(await self.app._progress_cards.update_goal(
                        source_id="om_goal_large", generation=goal_generation(goal),
                        projection=replace(current, partial_answer=ReplyCardPartialAnswerModule((previous.text,)))))
                self.channel.updates.clear()
                self.channel.replies.clear()
                oversized = answer("x" * 55_001, item="oversized")
                partials = (previous, oversized) if prior_delivered else (oversized,)
                self.channel.reply_results.extend(
                    sent_result(f"om_partial_{index}", chat_id="oc_direct")
                    for index in range(len(partials))
                )
                outcome = GoalOutcome(binding_id=self.binding.id, thread_id="native-one", logical_turn_id="goal-one",
                    owner_id="ou_user", origin=origin, goal=native_goal(status=GoalStatus.COMPLETE),
                    final_physical_turn_id="turn-one", final_turn_status="completed", final_response=final_response,
                    partial_answers=partials)
                await self.app.handle_completion(outcome)
                self.assertTrue(self.channel.updates)
                message_id, card = self.channel.updates[-1]
                self.assertEqual(message_id, "om_goal_large")
                self.assertEqual(card["header"]["template"], "green")
                self.assertIn("答案正文另行发送", str(card))
                self.assertNotIn(oversized.text, str(card))
                self.assertEqual(len(self.channel.replies), len(partials) + 1)
                for (_, post), partial in zip(self.channel.replies, partials):
                    self.assertEqual(post.markdown, "**阶段性答案**\n\n" + partial.text)
                plain = self.channel.replies[-1][1]
                text = plain.markdown if isinstance(plain, OutboundPost) else plain
                self.assertNotIn(oversized.text, text)
                if final_response is None:
                    self.assertIn("答案见阶段性答案", text)
                    self.assertNotIn("投递未确认", text)
                    self.assertNotIn("阶段性答案未发送", text)
                else:
                    self.assertEqual(text, final_response)

    async def test_goal_failed_original_card_posts_rollover_answers_before_compact_controls(self):
        from netizen_cli.cards import goal_generation
        from netizen_cli.channel.reply_presenter import GoalCardOrigin

        goal = native_goal()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        await self.fixture.register_goal_card(scope=scope, binding=self.binding, goal=goal,
            message_id="om_goal", runtime_state=GoalOperationState.RUNNING.value)
        origin = GoalCardOrigin(message_id="om_goal", scope=scope,
            binding_id=self.binding.id, short_id=self.binding.short_id,
            project_alias=self.binding.project_alias, goal_generation=goal_generation(goal),
            fallback_origin=FakeMessage("work", message_id="om_origin"))
        self.channel.replies.clear()
        self.channel.updates.clear()
        self.channel.card_update_results.extend(TimeoutError("response lost") for _ in range(3))
        self.channel.reply_results.extend(sent_result(f"om_answer_{index}", chat_id="oc_direct")
                                          for index in range(4))
        partials = (answer("earlier physical turn", turn="turn-old"),
                    answer("last physical turn", turn="turn-final"))
        outcome = GoalOutcome(binding_id=self.binding.id, thread_id="native-one", logical_turn_id="goal-one",
            owner_id="ou_user", origin=origin, goal=replace(goal, status=GoalStatus.PAUSED),
            final_physical_turn_id="turn-final", final_turn_status="interrupted",
            final_response="final answer", partial_answers=partials)
        await self.app.handle_completion(outcome)
        self.assertEqual(len(self.channel.updates), 3)
        self.assertEqual([post.markdown for _, post in self.channel.replies[:2]], [
            "**阶段性答案**\n\nearlier physical turn", "**阶段性答案**\n\nlast physical turn",
        ])
        self.assertEqual(self.channel.replies[2][1], "final answer")
        card = self.channel.replies[3][1]
        self.assertIsInstance(card, OutboundCard)
        self.assertIn("恢复 Goal", str(card.card))
        self.assertNotIn("earlier physical turn", str(card.card))
        self.assertNotIn("final answer", str(card.card))

    async def test_goal_control_recovery_cannot_consume_answer_fallback_budget(self):
        control_started = asyncio.Event()
        never = asyncio.Event()

        async def reply(message, content, opts=None):
            self.channel.replies.append((message.id, content))
            if isinstance(content, OutboundCard):
                control_started.set()
                await never.wait()
            return sent_result("om_answer", chat_id="oc_direct")

        outcome = GoalOutcome(binding_id=self.binding.id, thread_id="native-one", logical_turn_id="goal-one",
            owner_id="ou_user", origin=FakeMessage("work", message_id="om_origin"),
            goal=native_goal(status=GoalStatus.PAUSED), final_physical_turn_id="turn-one",
            final_turn_status="interrupted", final_response="final answer", partial_answers=(answer(),))
        with patch.object(self.channel, "reply", side_effect=reply), patch(
            "netizen_cli.codex_runtime._GOAL_COMPLETION_DELIVERY_TIMEOUT_SECONDS", 1.0,
        ), self.assertLogs("netizen_cli.channel_app", level="WARNING"):
            completed = asyncio.create_task(self.app.handle_completion(outcome))
            await asyncio.wait_for(control_started.wait(), 1)
            self.assertEqual(self.channel.replies[0][1].markdown, "**阶段性答案**\n\nfirst stable answer")
            self.assertEqual(self.channel.replies[1][1], "final answer")
            await asyncio.wait_for(completed, 2)
        self.assertEqual(len(self.channel.replies), 3)

    async def test_goal_progress_off_still_refreshes_partial_answers(self):
        goal = native_goal()
        self.runtime.goal_snapshot_value = goal
        self.runtime.active_goals[self.binding.id] = SimpleNamespace(state=GoalOperationState.RUNNING)
        snapshot = replace(goal_activity_snapshot(binding_id=self.binding.id), partial_answers=(answer(),), revision=2)
        self.runtime.goal_activity_values[self.binding.id] = snapshot
        refresh = self.app._goal_refresh_callback(binding=self.binding,
            scope=FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT),
            thread_id="native-one", logical_turn_id="goal-one", activity_enabled=False)
        revision, projection = await refresh()
        self.assertIsNone(projection.activity)
        self.assertEqual(projection.partial_answer.contents, ("first stable answer",))
        self.assertEqual(revision[-1], 2)


class OrdinarySideTerminalFallbackTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = await self.enterAsyncContext(channel_fixture())
        await self.fixture.new()
        self.app, self.channel, self.runtime = self.fixture.app, self.fixture.channel, self.fixture.runtime
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.fixture.store.active_binding(scope.key)
        self.fixture.store.assign_native_thread_id(binding.id, "native-one")
        self.binding = self.fixture.store.get(binding.id)
        self.origin = FakeMessage("work", message_id="om_origin")
        self.app._progress_cards = _ReplyCardPresenter(self.channel, self.runtime, poll_seconds=3600)

    def outcome(self, *, side=False, content="final original", partials=(), progress=True, mention=False, result=None):
        values = dict(
            thread_id="native-one", turn_id="turn-one", owner_id="ou_user", origin=self.origin,
            result=result or completed_turn_result(final_response=content), partial_answers=partials,
            task_feedback=BindingTaskFeedback(progress_card_enabled=progress, completion_mention_enabled=mention),
        )
        if side:
            return SideTurnOutcome(side_id="side-one", parent_binding_id=self.binding.id,
                cwd=self.fixture.project, **values)
        return TurnOutcome(binding_id=self.binding.id, **values)

    async def start_progress(self, outcome):
        side = isinstance(outcome, SideTurnOutcome)
        snapshot = (side_turn_activity_snapshot(side_id=outcome.side_id,
            thread_id=outcome.thread_id, turn_id=outcome.turn_id) if side else
            turn_activity_snapshot(binding_id=outcome.binding_id,
                thread_id=outcome.thread_id, turn_id=outcome.turn_id))
        values = self.runtime.side_turn_activity_values if side else self.runtime.turn_activity_values
        values[outcome.side_id if side else outcome.binding_id] = snapshot
        self.channel.reply_results.append(sent_result("om_progress", chat_id="oc_direct"))
        kwargs = dict(thread_id=outcome.thread_id, turn_id=outcome.turn_id, origin=outcome.origin)
        started = (await self.app._progress_cards.start_side(side_id=outcome.side_id, **kwargs) if side else
            await self.app._progress_cards.start(binding_id=outcome.binding_id, **kwargs))
        self.assertTrue(started)
        self.channel.replies.clear()
        return replace(outcome, activity=snapshot)

    @staticmethod
    def post_text(message):
        return message.markdown if isinstance(message, OutboundPost) else message

    async def test_same_card_recovery_or_exhausted_updates_choose_one_terminal_delivery(self):
        partials = (answer("first original<br><br>", "one"), answer("second original", "two"))
        for side in (False, True):
            for recovers in (True, False):
                with self.subTest(side=side, recovers=recovers):
                    self.channel.replies.clear()
                    self.channel.updates.clear()
                    outcome = await self.start_progress(self.outcome(side=side, partials=partials))
                    self.channel.card_update_results.extend([
                        SimpleNamespace(success=False, raw={"code": 2200}),
                        *([sent_result("om_progress", chat_id="oc_direct")] if recovers else [
                            SimpleNamespace(success=False, raw={"code": 2200}),
                            SimpleNamespace(success=False, raw={"code": 2200}),
                        ]),
                    ])
                    if not recovers:
                        self.channel.reply_results.extend(sent_result(f"om_post_{index}", chat_id="oc_direct") for index in range(3))
                    with patch("netizen_cli.channel.reply_presenter._TERMINAL_CARD_RETRY_SECONDS", 0), self.assertLogs(
                        "netizen_cli.channel.reply_presenter", level="ERROR",
                    ):
                        await self.app.handle_completion(outcome)
                    self.assertEqual(len(self.channel.updates), 2 if recovers else 3)
                    self.assertEqual({message_id for message_id, _ in self.channel.updates}, {"om_progress"})
                    if recovers:
                        self.assertEqual(self.channel.replies, [])
                        visible = str(self.channel.updates[-1][1])
                        for text in (partials[0].text, partials[1].text, "final original"):
                            self.assertIn(text, visible)
                    else:
                        self.assertEqual([self.post_text(message) for _, message in self.channel.replies], [
                            f"**阶段性答案**\n\n{partials[0].text}",
                            f"**阶段性答案**\n\n{partials[1].text}", "final original",
                        ])
                        self.assertFalse(any(isinstance(message, OutboundCard) for _, message in self.channel.replies))
                    self.assertEqual(outcome.partial_answers, partials)

    async def test_missing_card_fallback_preserves_exact_sources_and_mentions_only_final(self):
        partials = (answer("8. 原始编号\n\n   - 嵌套<br><br>内容", "one"), answer("second original", "two"))
        final = "| key | value |\n| --- | --- |\n| original | value |\n"
        for side in (False, True):
            with self.subTest(side=side):
                self.channel.replies.clear()
                self.channel.reply_results.extend(sent_result(f"om_post_{index}", chat_id="oc_direct") for index in range(3))
                outcome = self.outcome(side=side, content=final, partials=partials, mention=True)
                await self.app.handle_completion(outcome)
                messages = [message for _, message in self.channel.replies]
                self.assertEqual([self.post_text(message) for message in messages], [
                    f"**阶段性答案**\n\n{partials[0].text}", f"**阶段性答案**\n\n{partials[1].text}", final,
                ])
                self.assertTrue(all(isinstance(message, OutboundPost) for message in messages))
                self.assertEqual([message.mentions for message in messages], [[], [], [Identity(open_id="ou_user")]])
                self.assertEqual([source for source, _ in self.channel.replies], [self.origin.id] * 3)
                self.assertEqual(outcome.final_response, final)

    async def test_unknown_running_partial_is_not_replayed_during_terminal_fallback(self):
        first, last = answer("unknown first", "one"), answer("last original", "two")
        for side in (False, True):
            with self.subTest(side=side):
                self.channel.replies.clear()
                outcome = self.outcome(side=side, partials=(first, first, answer(thread="wrong"), last), progress=False)
                snapshot = (side_turn_activity_snapshot(side_id="side-one", thread_id="native-one", turn_id="turn-one") if side else
                    turn_activity_snapshot(binding_id=self.binding.id))
                values = self.runtime.side_turn_activity_values if side else self.runtime.turn_activity_values
                values["side-one" if side else self.binding.id] = snapshot
                kwargs = dict(thread_id="native-one", turn_id="turn-one", origin=self.origin, progress_enabled=False)
                if side:
                    await self.app._progress_cards.start_side(side_id="side-one", **kwargs)
                    session = self.app._progress_cards._side_sessions[("side-one", "native-one", "turn-one")]
                else:
                    await self.app._progress_cards.start(binding_id=self.binding.id, **kwargs)
                    session = self.app._progress_cards._sessions[(self.binding.id, "native-one", "turn-one")]
                self.channel.reply_results.append(SimpleNamespace(success=False))
                await self.app._progress_cards._deliver_partial_answers(session, (first,))
                self.channel.reply_results.extend(sent_result(f"om_post_{index}", chat_id="oc_direct") for index in range(2))
                await self.app.handle_completion(outcome)
                self.assertEqual([self.post_text(message) for _, message in self.channel.replies], [
                    "**阶段性答案**\n\nunknown first", "**阶段性答案**\n\nlast original", "final original",
                ])

    async def test_no_progress_files_card_follows_partials_without_embedding_them(self):
        path = self.fixture.project / "result.txt"
        path.write_text("result", encoding="utf-8")
        partial = answer("standalone partial")
        outcome = self.outcome(progress=False, partials=(partial,), result=completed_turn_result(
            file_change_item(str(path)), final_response="final original",
        ))
        self.channel.reply_results.extend(sent_result(f"om_post_{index}", chat_id="oc_direct") for index in range(2))
        await self.app.handle_completion(outcome)
        self.assertEqual(len(self.channel.replies), 2)
        self.assertEqual(self.channel.replies[0][1], OutboundPost(markdown="**阶段性答案**\n\nstandalone partial"))
        card = self.channel.replies[1][1]
        self.assertIsInstance(card, OutboundCard)
        self.assertIn("final original", str(card.card))
        self.assertIn("result.txt", str(card.card))
        self.assertNotIn(partial.text, str(card.card))

    async def test_slow_partial_batch_leaves_time_for_final_attempt(self):
        outcome = self.outcome(partials=(answer("slow first", "one"), answer("not reached", "two")))
        attempted = []

        async def reply(origin, message, opts=None):
            attempted.append(self.post_text(message))
            if len(attempted) == 1:
                await asyncio.Event().wait()
            return sent_result("om_final", chat_id="oc_direct")

        with patch.object(self.channel, "reply", side_effect=reply):
            await asyncio.wait_for(self.app._reply_task_answers(outcome, "final original", timeout_seconds=0.1), 1)
        self.assertEqual(attempted, ["**阶段性答案**\n\nslow first", "final original"])


class GoalPartialAnswerPresenterTest(unittest.IsolatedAsyncioTestCase):
    def projection(self, *contents, status="active", activity=None, result=None):
        from netizen_cli.domain import (
            ReplyCardGoalModule, ReplyCardPartialAnswerModule, ReplyCardProjection,
        )
        return ReplyCardProjection(
            scope=FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT),
            goal=ReplyCardGoalModule(
                binding_id="binding-one", short_id="binding1", project_alias="test",
                goal_generation="g" * 43, status=status, runtime_state=f"goal-{status}",
                objective="finish the task", token_budget=None, tokens_used=20,
            ),
            partial_answer=ReplyCardPartialAnswerModule(tuple(contents)) if contents else None,
            activity=activity, result=result,
        )

    def origin(self):
        from netizen_cli.channel.reply_presenter import GoalCardOrigin
        return GoalCardOrigin(
            message_id="om_goal", scope=FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT),
            binding_id="binding-one", short_id="binding1", project_alias="test",
            goal_generation="g" * 43,
        )

    async def start(self, presenter, projection, *, logical_turn_id="goal-one", refresh=None):
        self.assertTrue(await presenter.start_goal(
            binding_id="binding-one", thread_id="native-one", logical_turn_id=logical_turn_id,
            generation="g" * 43, origin=self.origin(), projection=projection,
            revision=1, refresh=refresh,
        ))

    async def test_status_refresh_waits_for_inflight_partial_before_merging(self):
        channel, runtime = FakeChannel(), StubRuntime()
        presenter = _ReplyCardPresenter(channel, runtime, poll_seconds=0.001)
        self.addAsyncCleanup(presenter.close)
        initial = self.projection("first")
        latest = self.projection("first", "newly delivered")
        entered, release = asyncio.Event(), asyncio.Event()

        async def refresh():
            return 2, latest

        async def update(message_id, card):
            if "newly delivered" in str(card) and not entered.is_set():
                entered.set()
                await release.wait()
            channel.updates.append((message_id, card))
            return sent_result(message_id, chat_id="oc_direct")

        with patch.object(channel, "update_card", side_effect=update):
            await self.start(presenter, initial, refresh=refresh)
            await asyncio.wait_for(entered.wait(), 1)
            session = presenter._goal_sessions[("binding-one", "native-one", "g" * 43)]
            status_update = asyncio.create_task(presenter.refresh_goal_snapshot(
                source_id="om_goal", generation="g" * 43,
                logical_turn_id="goal-one", projection=initial,
            ))
            await asyncio.wait_for(session.stopped.wait(), 1)
            release.set()
            self.assertTrue(await asyncio.wait_for(status_update, 1))
        current = presenter.goal_projection(source_id="om_goal", generation="g" * 43)
        self.assertEqual(current.partial_answer.contents, ("first", "newly delivered"))
        self.assertIn("newly delivered", str(channel.updates[-1][1]))

    async def test_pause_preserves_partials_and_manual_resume_resets_the_new_run(self):
        from netizen_cli.channel.reply_presenter import _GoalCardDelivery
        from netizen_cli.domain import ReplyCardResultModule
        channel, runtime = FakeChannel(), StubRuntime()
        presenter = _ReplyCardPresenter(channel, runtime)
        self.addAsyncCleanup(presenter.close)
        await self.start(presenter, self.projection("previous run answer"))
        paused = self.projection(
            "previous run answer", status="paused", result=ReplyCardResultModule("Goal 已暂停。"),
        )
        receipt = await presenter.finish_goal(
            binding_id="binding-one", thread_id="native-one", logical_turn_id="goal-one",
            generation="g" * 43, origin=self.origin(), projection=paused, retain_session=True,
        )
        self.assertEqual(receipt.status, _GoalCardDelivery.DELIVERED)
        self.assertEqual(
            presenter.goal_projection(source_id="om_goal", generation="g" * 43).partial_answer.contents,
            ("previous run answer",),
        )
        await self.start(presenter, self.projection(), logical_turn_id="goal-resumed")
        resumed_card = channel.updates[-1][1]
        self.assertNotIn("previous run answer", str(resumed_card))
        self.assertIsNone(presenter.goal_projection(source_id="om_goal", generation="g" * 43).partial_answer)
        delayed = await presenter.finish_goal(
            binding_id="binding-one", thread_id="native-one", logical_turn_id="goal-one",
            generation="g" * 43, origin=self.origin(), projection=paused, retain_session=True,
        )
        self.assertEqual(delayed.status, _GoalCardDelivery.SUPERSEDED)
        self.assertIs(channel.updates[-1][1], resumed_card)

    async def test_rollover_projection_keeps_prior_answers_separate_from_final_result(self):
        from netizen_cli.channel.reply_presenter import _GoalCardDelivery
        from netizen_cli.domain import (
            ReplyCardActivityModule, ReplyCardResultModule, TurnProgressManifest, TurnProgressManifestStep,
        )
        channel, runtime = FakeChannel(), StubRuntime()
        presenter = _ReplyCardPresenter(channel, runtime, poll_seconds=0.001)
        self.addAsyncCleanup(presenter.close)
        first_activity = ReplyCardActivityModule(TurnProgressManifest(
            state="running", steer_count=0, plan_available=True, plan_generated=True,
            plan_may_be_stale=False, steps=(TurnProgressManifestStep("old physical turn step", "completed"),),
        ))
        next_activity = ReplyCardActivityModule(replace(first_activity.progress, steps=()))
        initial = self.projection("first turn answer", activity=first_activity)
        next_turn = self.projection("first turn answer", "second turn answer", activity=next_activity)
        updated = asyncio.Event()

        async def refresh():
            return 2, next_turn

        async def update(message_id, card):
            channel.updates.append((message_id, card))
            if "second turn answer" in str(card):
                updated.set()
            return sent_result(message_id, chat_id="oc_direct")

        with patch.object(channel, "update_card", side_effect=update):
            await self.start(presenter, initial, refresh=refresh)
            await asyncio.wait_for(updated.wait(), 1)
            terminal = replace(
                next_turn,
                activity=replace(next_activity, terminal_status="completed", collapsed=True),
                result=ReplyCardResultModule("final physical turn result"),
            )
            receipt = await presenter.finish_goal(
                binding_id="binding-one", thread_id="native-one", logical_turn_id="goal-one",
                generation="g" * 43, origin=self.origin(), projection=terminal, retain_session=True,
            )
        self.assertEqual(receipt.status, _GoalCardDelivery.DELIVERED)
        current = presenter.goal_projection(source_id="om_goal", generation="g" * 43)
        self.assertEqual(current.partial_answer.contents, ("first turn answer", "second turn answer"))
        self.assertEqual(current.result.content, "final physical turn result")
        self.assertEqual(current.activity.progress.steps, ())
        self.assertNotIn("old physical turn step", str(channel.updates[-1][1]))

    async def test_body_fallback_guard_blocks_resume_and_rejects_the_superseded_run(self):
        presenter = _ReplyCardPresenter(FakeChannel(), StubRuntime())
        self.addAsyncCleanup(presenter.close)
        await self.start(presenter, self.projection("first"))
        entered = asyncio.Event()

        async def resume():
            entered.set()
            await self.start(presenter, self.projection(), logical_turn_id="resumed-goal")

        guard = dict(binding_id="binding-one", thread_id="native-one", generation="g" * 43,
            logical_turn_id="goal-one", origin=self.origin())
        async with presenter.goal_fallback_guard(**guard) as current:
            self.assertTrue(current)
            resuming = asyncio.create_task(resume())
            await asyncio.wait_for(entered.wait(), 1)
            self.assertFalse(resuming.done())
        await asyncio.wait_for(resuming, 1)
        async with presenter.goal_fallback_guard(**guard) as current:
            self.assertFalse(current)

    async def test_body_fallback_guard_rejects_retired_and_closed_routes(self):
        presenter = _ReplyCardPresenter(FakeChannel(), StubRuntime(), poll_seconds=10)
        self.addAsyncCleanup(presenter.close)
        await self.start(presenter, self.projection("first"), refresh=AsyncMock(return_value=None))
        await presenter.update_goal(source_id="om_goal", generation="g" * 43,
            projection=self.projection("first", status="paused"), retain_session=False)
        guard = dict(binding_id="binding-one", thread_id="native-one", generation="g" * 43,
            logical_turn_id="goal-one", origin=self.origin())
        async with presenter.goal_fallback_guard(**guard) as current:
            self.assertFalse(current)
        async with presenter.goal_fallback_guard(**dict(guard, generation=None)) as current:
            self.assertTrue(current)
        await presenter.close()
        async with presenter.goal_fallback_guard(**dict(guard, generation=None)) as current:
            self.assertFalse(current)
