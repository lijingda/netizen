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
    GoalOutcome, GoalFinalizationStatus, GoalSubmission, SideTurnOutcome, Submission, SubmitDisposition, TurnOutcome,
)
from netizen_cli.domain import FeishuScope, NativeCapability, ScopeKind
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
            turn_id="logical-one", side=False, logical_turn=True, reply=reply)
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
        self.assertEqual(len(self.channel.replies), 2)
        self.assertEqual(self.channel.replies[0][1].markdown,
                         "**阶段性答案**\n\nfirst stable answer")
        closing = self.channel.replies[-1][1]
        text = closing.markdown if isinstance(closing, OutboundPost) else closing
        self.assertIn("自动结束结果未确认", text)
        self.assertIn("clear result lost", text)
        self.assertNotIn("first stable answer", text)
        self.assertNotIsInstance(closing, OutboundCard)

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
        self.assertEqual(len(self.channel.replies), 2)
        self.assertIsInstance(self.channel.replies[0][1], OutboundPost)
        last = self.channel.replies[1][1]
        text = last.markdown if isinstance(last, OutboundPost) else last
        self.assertIn("投递未确认", text)
        self.assertNotIn("答案见阶段性答案", text)
        self.assertNotIsInstance(self.channel.replies[-1][1], OutboundCard)

    async def test_goal_fallback_preserves_all_physical_turn_partials_without_management_card(self):
        partials = (answer("earlier physical turn", turn="turn-old"),
                    answer("x" * 55_001, item="oversized", turn="turn-final"))
        self.channel.reply_results.extend(sent_result(f"om_answer_{index}", chat_id="oc_direct")
                                          for index in range(3))
        outcome = GoalOutcome(binding_id=self.binding.id, thread_id="native-one", logical_turn_id="goal-one",
            owner_id="ou_user", origin=FakeMessage("work", message_id="om_origin"),
            goal=native_goal(status=GoalStatus.PAUSED),
            final_physical_turn_id="turn-final", final_turn_status="completed",
            final_response="final answer", partial_answers=partials,
            task_feedback=BindingTaskFeedback(progress_card_enabled=True))
        await self.app.handle_completion(outcome)
        self.assertEqual(len(self.channel.replies), 3)
        self.assertEqual([post.markdown for _, post in self.channel.replies[:2]],
                         ["**阶段性答案**\n\n" + partial.text for partial in partials])
        closing = self.channel.replies[-1][1]
        self.assertEqual(closing.markdown if isinstance(closing, OutboundPost) else closing, "final answer")
        self.assertEqual(self.channel.updates, [])
        self.assertTrue(all(not isinstance(content, OutboundCard) for _, content in self.channel.replies))

    async def test_goal_progress_off_still_streams_rollover_partial_answers(self):
        first = answer(turn="physical-one")
        second = answer("next stable answer", turn="physical-two")
        self.runtime.goal_activity_values[self.binding.id] = replace(
            goal_activity_snapshot(binding_id=self.binding.id), partial_answers=(first,), revision=2)
        delivered = asyncio.Queue()

        async def reply(message, post, opts=None):
            self.channel.replies.append((message.id, post))
            delivered.put_nowait(post)
            return sent_result("om_partial", chat_id="oc_direct")

        with patch.object(self.channel, "reply", side_effect=reply):
            self.assertTrue(await self.app._present_running_goal(
                binding=self.binding,
                submission=GoalSubmission(self.binding.id, "native-one", "goal-one", lambda: None),
                origin=FakeMessage("work", message_id="om_origin")))
            first_post = await asyncio.wait_for(delivered.get(), 1)
            self.runtime.goal_activity_values[self.binding.id] = replace(
                self.runtime.goal_activity_values[self.binding.id],
                physical_turn_id="physical-two", partial_answers=(first, second), revision=3)
            second_post = await asyncio.wait_for(delivered.get(), 1)
        self.assertEqual(first_post.markdown, "**阶段性答案**\n\nfirst stable answer")
        self.assertEqual(second_post.markdown, "**阶段性答案**\n\nnext stable answer")
        self.assertEqual(delivered.qsize(), 0)
        self.assertEqual(len(self.channel.replies), 2)
        self.assertEqual(self.channel.updates, [])

    async def test_goal_management_delivery_failure_releases_handoff_and_keeps_result(self):
        self.runtime.available_capabilities = frozenset({NativeCapability.GOAL})
        for action in ("ship safely", "resume"):
            with self.subTest(action=action):
                released = []
                self.channel.replies.clear()
                self.runtime.goal_snapshot_value = native_goal(
                    GoalStatus.PAUSED if action == "resume" else GoalStatus.ACTIVE,
                )
                self.runtime.goal_activity_values[self.binding.id] = goal_activity_snapshot(binding_id=self.binding.id)
                self.runtime.goal_submission = GoalSubmission(
                    self.binding.id, "native-one", "goal-one", lambda: released.append(True),
                )
                self.channel.reply_results.append(RuntimeError("management card unavailable"))
                origin = FakeMessage("/goal " + action, message_id="om_goal_input")
                with self.assertLogs("netizen_cli.channel_app", "ERROR"):
                    await self.app.handle_message(origin)
                self.assertEqual(released, [True])
                await self.app.handle_completion(GoalOutcome(
                    binding_id=self.binding.id, thread_id="native-one", logical_turn_id="goal-one",
                    owner_id="ou_user", origin=origin, goal=native_goal(GoalStatus.COMPLETE),
                    final_physical_turn_id="physical-last", final_turn_status="completed",
                    final_response="confirmed final answer",
                ))
                closing = self.channel.replies[-1][1]
                self.assertEqual(closing.markdown if isinstance(closing, OutboundPost) else closing,
                                 "confirmed final answer")
                self.assertEqual(self.app._progress_cards._sessions, {})


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
        outcome = replace(outcome, cwd=self.fixture.project)
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
    async def test_logical_reply_keeps_rollover_answers_on_one_card_then_releases_session(self):
        from netizen_cli.cards import turn_progress_card
        channel, runtime = FakeChannel(), StubRuntime()
        presenter = _ReplyCardPresenter(channel, runtime, poll_seconds=0.001)
        self.addAsyncCleanup(presenter.close)
        first = answer("first turn answer", turn="physical-one")
        second = answer("second turn answer", turn="physical-two")
        current = replace(turn_activity_snapshot(binding_id="binding-one", turn_id="logical-one"),
                          partial_answers=(first,), revision=1)
        updated = asyncio.Event()
        channel.reply_results.append(sent_result("om_reply", chat_id="oc_direct"))

        async def update(message_id, card):
            channel.updates.append((message_id, card))
            if "second turn answer" in str(card):
                updated.set()
            return sent_result(message_id, chat_id="oc_direct")

        with patch.object(channel, "update_card", side_effect=update):
            self.assertTrue(await presenter.start(binding_id="binding-one", thread_id="native-one",
                turn_id="logical-one", origin=FakeMessage("work", message_id="om_origin"),
                snapshot_reader=lambda: current, logical_turn=True))
            current = replace(current, partial_answers=(first, second), steps=(), revision=2)
            await asyncio.wait_for(updated.wait(), 1)
            receipt = await presenter.finish(binding_id="binding-one", thread_id="native-one",
                turn_id="logical-one", activity=current,
                render=lambda snapshot: turn_progress_card(snapshot=snapshot,
                    partial_answers=(first, second), final_response="final physical turn result",
                    terminal_status="completed", collapsed=True))
        self.assertTrue(receipt.confirmed)
        self.assertEqual(len(channel.replies), 1)
        card = channel.updates[-1][1]
        self.assertEqual(channel.updates[-1][0], "om_reply")
        for text in (first.text, second.text, "final physical turn result"):
            self.assertIn(text, str(card))
        self.assertNotIn("暂停 Goal", str(card))
        self.assertEqual(presenter._sessions, {})

    async def test_resuming_same_logical_reply_keeps_running_delivery_and_card(self):
        channel, runtime = FakeChannel(), StubRuntime()
        presenter = _ReplyCardPresenter(channel, runtime)
        self.addAsyncCleanup(presenter.close)
        first = answer(turn="physical-one")
        snapshot = replace(turn_activity_snapshot(binding_id="binding-one", turn_id="logical-one"),
                           partial_answers=(first,))
        channel.reply_results.append(sent_result("om_reply", chat_id="oc_direct"))
        options = dict(binding_id="binding-one", thread_id="native-one", turn_id="logical-one",
                       origin=FakeMessage("work", message_id="om_origin"),
                       snapshot_reader=lambda: snapshot, logical_turn=True)
        self.assertTrue(await presenter.start(**options))
        self.assertTrue(await presenter.start(**options))
        self.assertEqual(len(channel.replies), 1)
        self.assertIn(first.text, str(channel.replies[0][1].card))
        self.assertEqual(len(presenter._sessions), 1)
        await presenter.abandon(binding_id="binding-one", thread_id="native-one", turn_id="logical-one")
        self.assertEqual(presenter._sessions, {})
