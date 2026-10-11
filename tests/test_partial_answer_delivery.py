"""Stable answer delivery stays separate from native completion and progress preferences."""
from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from lark_channel import OutboundCard, OutboundPost

from netizen_cli.bindings import BindingTaskFeedback
from netizen_cli.channel.reply_presenter import _ReplyCardPresenter
from netizen_cli.codex_runtime import (
    GoalOperationState, GoalOutcome, GoalFinalizationStatus, Submission, SubmitDisposition, TurnOutcome,
)
from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.partial_answers import PartialAnswer
from netizen_cli.sdk_gap_adapter import GoalStatus
from tests.support.channel_fixtures import channel_fixture
from tests.support.channel_messages import FakeChannel, FakeMessage
from tests.support.channel_results import (
    completed_turn_result, goal_activity_snapshot, native_goal,
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
                confirmed = await presenter.finish_partial_answers(
                    owner_id=owner, thread_id=thread, turn_id=turn,
                    partial_answers=(first, first, answer(thread="wrong"), second),
                    side=side, progress_enabled=False, reply=reply,
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
        confirmed = await presenter.finish_partial_answers(owner_id="binding-one", thread_id="native-one",
            turn_id="turn-one", partial_answers=(partial,), side=False, progress_enabled=False, reply=reply)
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
        finishing = asyncio.create_task(presenter.finish_partial_answers(owner_id="binding-one",
            thread_id="native-one", turn_id="turn-one", partial_answers=partials,
            side=False, progress_enabled=False, reply=reply))
        release.set()
        self.assertTrue(await asyncio.wait_for(finishing, 1))
        self.assertEqual(len(attempts), 2)
        self.assertEqual(presenter._sessions, {})

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
        confirmed = await presenter.finish_partial_answers(owner_id="binding-one", thread_id="native-one",
            turn_id="turn-one", partial_answers=(partial,), side=False, progress_enabled=False, reply=reply)
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
                confirmed = await presenter.finish_partial_answers(owner_id="binding-one",
                    thread_id="native-one", turn_id="turn-one", partial_answers=(answer(),),
                    side=False, progress_enabled=False, reply=reply)
                self.assertFalse(confirmed)
                reply.assert_awaited_once()


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

    async def test_running_card_failure_keeps_partial_module_in_replacement_card(self):
        self.channel.reply_results.append(sent_result("om_replacement", chat_id="oc_direct"))
        outcome = TurnOutcome(binding_id=self.binding.id, thread_id="native-one", turn_id="turn-one",
            owner_id="ou_user", origin=FakeMessage("work", message_id="om_origin"),
            result=completed_turn_result(final_response="final answer"), partial_answers=(answer(),),
            task_feedback=BindingTaskFeedback(progress_card_enabled=True))
        await self.app.handle_completion(outcome)
        self.assertEqual(len(self.channel.replies), 1)
        card = self.channel.replies[0][1]
        self.assertIsInstance(card, OutboundCard)
        self.assertIn("first stable answer", str(card.card))
        self.assertIn("final answer", str(card.card))

    async def test_partial_only_card_capacity_failure_never_claims_answer_delivery(self):
        outcome = TurnOutcome(binding_id=self.binding.id, thread_id="native-one", turn_id="turn-one",
            owner_id="ou_user", origin=FakeMessage("work", message_id="om_origin"),
            result=completed_turn_result(final_response=None), partial_answers=(answer(),),
            task_feedback=BindingTaskFeedback(progress_card_enabled=True))
        with patch("netizen_cli.channel_app.reply_card", side_effect=ValueError("existing capacity guard")):
            await self.app.handle_completion(outcome)
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
        self.assertIn("first stable answer", str(card.card))
        self.assertNotIn("未产生文本回复", str(card.card))

    async def test_partial_replacement_card_keeps_failed_and_interrupted_chrome(self):
        for status in ("failed", "interrupted"):
            with self.subTest(status=status):
                self.channel.replies.clear()
                outcome = TurnOutcome(binding_id=self.binding.id, thread_id="native-one", turn_id="turn-one",
                    owner_id="ou_user", origin=FakeMessage("work", message_id="om_origin"),
                    result=SimpleNamespace(status=status, final_response=None, items=[]),
                    partial_answers=(answer(),), task_feedback=BindingTaskFeedback(progress_card_enabled=True))
                await self.app.handle_completion(outcome)
                card = self.channel.replies[-1][1]
                self.assertIsInstance(card, OutboundCard)
                self.assertNotEqual(card.card["header"]["template"], "green")
                self.assertIn("first stable answer", str(card.card))

    async def test_goal_failed_fallback_send_does_not_claim_partial_delivery(self):
        self.channel.reply_results.append(SimpleNamespace(success=False))
        outcome = GoalOutcome(binding_id=self.binding.id, thread_id="native-one", logical_turn_id="goal-one",
            owner_id="ou_user", origin=FakeMessage("work", message_id="om_origin"), goal=native_goal(status=GoalStatus.COMPLETE),
            final_physical_turn_id="turn-one", final_turn_status="completed", partial_answers=(answer(),))
        await self.app.handle_completion(outcome)
        self.assertEqual(len(self.channel.replies), 2)
        last = self.channel.replies[-1][1]
        text = last.markdown if isinstance(last, OutboundPost) else last
        self.assertIn("投递未确认", text)
        self.assertNotIn("答案见阶段性答案", text)

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
                outcome = GoalOutcome(binding_id=self.binding.id, thread_id="native-one", logical_turn_id="goal-one",
                    owner_id="ou_user", origin=origin, goal=native_goal(status=GoalStatus.COMPLETE),
                    final_physical_turn_id="turn-one", final_turn_status="completed", final_response=final_response,
                    partial_answers=((previous, oversized) if prior_delivered else (oversized,)))
                await self.app.handle_completion(outcome)
                self.assertTrue(self.channel.updates)
                message_id, card = self.channel.updates[-1]
                self.assertEqual(message_id, "om_goal_large")
                self.assertEqual(card["header"]["template"], "green")
                self.assertIn("阶段性答案未能完整保留在终态卡片", str(card))
                self.assertNotIn(oversized.text, str(card))
                self.assertEqual(len(self.channel.replies), 1)
                plain = self.channel.replies[-1][1]
                text = plain.markdown if isinstance(plain, OutboundPost) else plain
                self.assertNotIn(oversized.text, text)
                if final_response is None:
                    self.assertIn("完整投递未确认", text)
                    self.assertNotIn("答案见阶段性答案", text)
                    self.assertNotIn("阶段性答案未发送", text)
                else:
                    self.assertEqual(text, final_response)

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
