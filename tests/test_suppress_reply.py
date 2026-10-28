from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from lark_channel import Identity, OutboundCard, OutboundText

from netizen_cli.bindings import BindingTaskFeedback
from netizen_cli.channel import reply_presenter
from netizen_cli.domain import ActiveState, FeishuScope, GoalOperationState, GoalStatus, ScopeKind
from netizen_cli.partial_answers import PartialAnswer
from netizen_cli.runtime.contracts import (
    GoalFinalizationStatus,
    GoalOutcome,
    GoalSubmission,
    SideSessionSnapshot,
    SideSessionState,
    SideTurnOutcome,
    TerminalStateUnknown,
    ThreadActivityDiscardedOutcome,
    TurnObservationUnavailableOutcome,
    TurnOutcome,
)
from netizen_cli.schedules.models import ScheduleRule
from netizen_cli.session_settings import SessionSettings
from netizen_cli.sessions.errors import SessionToolError
from netizen_cli.worktrees.policy import WorkspacePolicy
from tests.support.channel_cards import _elements
from tests.support.channel_fixtures import channel_fixture, scheduled_channel_fixture
from tests.support.channel_messages import FakeMessage, PNG
from tests.support.channel_results import (
    completed_turn_result,
    file_change_item,
    goal_activity_snapshot,
    native_goal,
    retryable_sent_result,
    sent_result,
    side_turn_activity_snapshot,
    turn_activity_snapshot,
)


class SuppressReplyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch.object(reply_presenter, "_TERMINAL_CARD_RETRY_SECONDS", 0.01))
        self.fixture = await self.enterAsyncContext(channel_fixture())
        self.app, self.channel, self.runtime = (
            self.fixture.app, self.fixture.channel, self.fixture.runtime,
        )
        await self.fixture.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.fixture.store.active_binding(scope.key)
        self.fixture.store.assign_native_thread_id(binding.id, "native-one")
        self.binding = self.fixture.store.get(binding.id)
        self.origin = FakeMessage("work silently", message_id="om_origin")
        self.feedback = BindingTaskFeedback(completion_mention_enabled=False)

    def outcome(self, **changes):
        values = dict(
            binding_id=self.binding.id, thread_id="native-one", turn_id="turn-one",
            owner_id="ou_initiator", origin=self.origin, cwd=self.fixture.project,
            task_feedback=self.feedback,
            result=completed_turn_result(final_response="private result"),
        )
        values.update(changes)
        return TurnOutcome(**values)

    def goal_outcome(self, **changes):
        values = dict(
            binding_id=self.binding.id, thread_id="native-one", logical_turn_id="goal-one",
            owner_id="ou_initiator", origin=self.origin, cwd=self.fixture.project,
            task_feedback=self.feedback, goal=native_goal(GoalStatus.COMPLETE),
            final_physical_turn_id="goal-turn-final", final_turn_status="completed",
            final_response="private Goal result",
        )
        values.update(changes)
        return GoalOutcome(**values)

    async def suppress(self, outcome):
        if isinstance(outcome, GoalOutcome):
            snapshot = replace(
                goal_activity_snapshot(binding_id=self.binding.id),
                logical_turn_id=outcome.logical_turn_id,
                physical_turn_id=outcome.final_physical_turn_id,
            )
            self.runtime.goal_activity_values[self.binding.id] = snapshot
            turn_id = outcome.final_physical_turn_id
        elif isinstance(outcome, SideTurnOutcome):
            self.runtime.side_turn_activity_values[outcome.side_id] = side_turn_activity_snapshot(
                side_id=outcome.side_id, thread_id=outcome.thread_id, turn_id=outcome.turn_id,
            )
            turn_id = outcome.turn_id
        else:
            self.runtime.turn_activity_values[self.binding.id] = turn_activity_snapshot(
                binding_id=self.binding.id, thread_id=outcome.thread_id, turn_id=outcome.turn_id,
            )
            turn_id = outcome.turn_id
        return await self.app.suppress_reply(source_thread_id=outcome.thread_id, source_turn_id=turn_id)

    async def start_progress(self, outcome):
        self.channel.reply_results.append(sent_result("om_progress", chat_id="oc_direct"))
        if isinstance(outcome, GoalOutcome):
            snapshot = replace(
                goal_activity_snapshot(binding_id=self.binding.id),
                logical_turn_id=outcome.logical_turn_id,
                physical_turn_id=outcome.final_physical_turn_id,
            )
            self.runtime.goal_activity_values[self.binding.id] = snapshot
            started = await self.app._present_running_goal(
                binding=self.binding, origin=outcome.origin,
                submission=GoalSubmission(
                    self.binding.id, outcome.thread_id, outcome.logical_turn_id,
                    lambda: None, task_feedback=outcome.task_feedback,
                ),
            )
        else:
            snapshot = turn_activity_snapshot(
                binding_id=self.binding.id, thread_id=outcome.thread_id, turn_id=outcome.turn_id,
            )
            self.runtime.turn_activity_values[self.binding.id] = snapshot
            started = await self.app._progress_cards.start(
                binding_id=self.binding.id, thread_id=outcome.thread_id,
                turn_id=outcome.turn_id, origin=outcome.origin,
            )
        self.assertTrue(started)
        return replace(outcome, activity=snapshot)

    def queue_mention(self, anchor):
        self.channel.send_results.append(sent_result(
            "om_notification", chat_id="oc_direct", root_id="om_origin", parent_id=anchor,
        ))

    def assert_cleaned(self, outcome):
        reply_id = outcome.logical_turn_id if isinstance(outcome, GoalOutcome) else outcome.turn_id
        physical_id = outcome.final_physical_turn_id if isinstance(outcome, GoalOutcome) else outcome.turn_id
        self.assertFalse(self.app._reply_suppression.is_suppressed(outcome.thread_id, reply_id, physical_id))
        self.assertEqual(self.app._progress_cards._sessions, {})
        self.assertEqual(self.app._reactions._pulses, {})

    async def test_tool_is_idempotent_without_ending_native_execution_or_sending_messages(self):
        outcome = self.outcome()
        first = await self.suppress(outcome)
        second = await self.app.suppress_reply(source_thread_id="native-one", source_turn_id="turn-one")
        self.assertEqual(first, second)
        self.assertTrue(first["suppressed"])
        self.assertEqual(self.runtime.turn_activity_values[self.binding.id].state, ActiveState.RUNNING)
        self.assertTrue(self.app._reply_suppression.is_suppressed("native-one", "turn-one", "turn-one"))
        self.assertEqual(self.channel.replies, [])
        self.assertEqual(self.channel.send_calls, [])
        self.assertEqual(self.channel.reactions, [])

    async def test_wrong_turn_unregistered_child_and_stopping_turn_cannot_suppress_parent(self):
        self.runtime.turn_activity_values[self.binding.id] = turn_activity_snapshot(binding_id=self.binding.id)
        for thread_id, turn_id in (("native-one", "old-turn"), ("child-thread", "turn-one")):
            with self.subTest(thread_id=thread_id, turn_id=turn_id):
                with self.assertRaises(SessionToolError):
                    await self.app.suppress_reply(source_thread_id=thread_id, source_turn_id=turn_id)
        self.runtime.turn_activity_values[self.binding.id] = turn_activity_snapshot(
            binding_id=self.binding.id, state=ActiveState.STOPPING,
        )
        with self.assertRaises(SessionToolError):
            await self.app.suppress_reply(source_thread_id="native-one", source_turn_id="turn-one")
        self.assertFalse(self.app._reply_suppression.is_suppressed("native-one", "turn-one", "turn-one"))

    async def test_goal_stopping_rejects_tool_but_paused_goal_with_running_turn_can_suppress(self):
        snapshot = replace(
            goal_activity_snapshot(binding_id=self.binding.id), state=GoalOperationState.PAUSING,
        )
        self.runtime.goal_activity_values[self.binding.id] = snapshot
        with self.assertRaises(SessionToolError):
            await self.app.suppress_reply(
                source_thread_id=snapshot.thread_id, source_turn_id=snapshot.physical_turn_id,
            )
        self.assertFalse(self.app._reply_suppression.is_suppressed(
            snapshot.thread_id, snapshot.logical_turn_id, snapshot.physical_turn_id,
        ))
        # Native Goal pause does not interrupt the current physical Turn.
        self.runtime.goal_snapshot_value = native_goal(GoalStatus.PAUSED)
        self.runtime.goal_activity_values[self.binding.id] = replace(snapshot, state=GoalOperationState.RUNNING)
        response = await self.app.suppress_reply(
            source_thread_id=snapshot.thread_id, source_turn_id=snapshot.physical_turn_id,
        )
        self.assertTrue(response["suppressed"])

    async def test_normal_completion_omits_nonempty_empty_and_missing_final_without_creating_card(self):
        for index, final in enumerate(("private result", "", None)):
            for progress in (False, True):
                with self.subTest(final=final, progress=progress):
                    outcome = self.outcome(
                        turn_id=f"turn-{index}-{progress}", result=completed_turn_result(final_response=final),
                        task_feedback=replace(self.feedback, progress_card_enabled=progress),
                    )
                    await self.suppress(outcome)
                    await self.app.handle_completion(outcome)
                    self.assertEqual(self.channel.replies, [])
                    self.assertEqual(self.channel.updates, [])
                    self.assertEqual(self.channel.send_calls, [])
                    self.assert_cleaned(outcome)
        self.assertEqual(self.channel.reactions, [(self.origin.id, "DONE")] * 6)

    async def test_final_files_and_images_are_not_prepared_or_delivered(self):
        (self.fixture.project / "secret.txt").write_text("private file")
        (self.fixture.project / "secret.png").write_bytes(PNG)
        outcome = self.outcome(
            result=completed_turn_result(file_change_item("secret.txt"), final_response="![secret](secret.png)"),
        )
        await self.suppress(outcome)
        await self.app.handle_completion(outcome)
        self.assertEqual(self.channel.upload_calls, [])
        self.assertEqual(self.channel.replies, [])
        self.assertEqual(self.channel.send_calls, [])
        self.assert_cleaned(outcome)

    async def test_partial_answers_after_tool_and_terminal_catchup_keep_existing_delivery(self):
        self.app._progress_cards._poll_seconds = 0.001
        first = PartialAnswer("native-one", "turn-one", "running", "answer after suppress tool")
        later = PartialAnswer("native-one", "turn-one", "terminal", "answer first seen at completion")
        for progress in (False, True):
            with self.subTest(progress=progress):
                self.channel.replies.clear()
                self.channel.updates.clear()
                outcome = self.outcome(
                    partial_answers=(first, later),
                    task_feedback=replace(self.feedback, progress_card_enabled=progress),
                )
                await self.suppress(outcome)
                observed = asyncio.Event()
                original_reply, original_update = self.channel.reply, self.channel.update_card

                async def reply(origin, content, opts=None):
                    result = await original_reply(origin, content, opts)
                    if first.text in str(content):
                        observed.set()
                    return result

                async def update(message_id, card):
                    result = await original_update(message_id, card)
                    if first.text in str(card):
                        observed.set()
                    return result

                if progress:
                    self.channel.reply_results.append(sent_result("om_progress", chat_id="oc_direct"))
                with (patch.object(self.channel, "reply", side_effect=reply),
                      patch.object(self.channel, "update_card", side_effect=update)):
                    self.assertTrue(await self.app._progress_cards.start(
                        binding_id=self.binding.id, thread_id=outcome.thread_id, turn_id=outcome.turn_id,
                        origin=self.origin, progress_enabled=progress,
                    ))
                    self.runtime.turn_activity_values[self.binding.id] = replace(
                        self.runtime.turn_activity_values[self.binding.id], revision=2, partial_answers=(first,),
                    )
                    await asyncio.wait_for(observed.wait(), 2)
                    await self.app.handle_completion(outcome)
                if progress:
                    rendered = str(self.channel.updates[-1][1])
                    self.assertEqual(len(self.channel.replies), 1)
                    self.assertIn(first.text, rendered)
                    self.assertIn(later.text, rendered)
                else:
                    rendered = str(self.channel.replies)
                    self.assertEqual(len(self.channel.replies), 2)
                    self.assertEqual(rendered.count(first.text), 1)
                    self.assertEqual(rendered.count(later.text), 1)
                self.assertNotIn("private result", rendered)
                self.assert_cleaned(outcome)

    async def test_existing_progress_finishes_collapsed_without_result_files_or_new_card(self):
        (self.fixture.project / "secret.txt").write_text("private file")
        outcome = await self.start_progress(self.outcome(
            task_feedback=replace(self.feedback, progress_card_enabled=True),
            result=completed_turn_result(file_change_item("secret.txt"), final_response="private result"),
        ))
        await self.suppress(outcome)
        await self.app.handle_completion(outcome)
        self.assertEqual(len(self.channel.replies), 1)
        self.assertEqual(len(self.channel.updates), 1)
        message_id, card = self.channel.updates[0]
        self.assertEqual(message_id, "om_progress")
        visible = json.dumps(card, ensure_ascii=False)
        self.assertNotIn("private result", visible)
        self.assertNotIn("secret.txt", visible)
        self.assertNotIn("未产生文本回复", visible)
        self.assertEqual(_elements(card, "button"), [])
        panels = _elements(card, "collapsible_panel")
        self.assertTrue(panels)
        self.assertTrue(all(not panel["expanded"] for panel in panels))
        self.assertIn((self.origin.id, "DONE"), self.channel.reactions)
        self.assert_cleaned(outcome)

    async def test_unconfirmed_partial_only_preserves_failure_reply_files_and_single_mention(self):
        (self.fixture.project / "result.txt").write_text("task file")
        for files in (False, True):
            for mention in (False, True):
                with self.subTest(files=files, mention=mention):
                    presentations = []
                    for silent in (False, True):
                        self.channel.replies.clear()
                        outcome = self.outcome(
                            result=completed_turn_result(
                                *((file_change_item("result.txt"),) if files else ()), final_response="",
                            ),
                            partial_answers=(PartialAnswer("native-one", "turn-one", "answer", "partial"),),
                            task_feedback=BindingTaskFeedback(
                                progress_card_enabled=False, completion_mention_enabled=mention,
                            ),
                        )
                        if silent:
                            await self.suppress(outcome)
                        self.channel.reply_results.append(retryable_sent_result())
                        with self.assertLogs("netizen_cli.channel_app", "WARNING"):
                            await self.app.handle_completion(outcome)
                        self.assertEqual(len(self.channel.replies), 2)
                        _, failure_reply = self.channel.replies[-1]
                        self.assertIn("任务已完成，但阶段性答案投递未确认。", str(failure_reply))
                        self.assertEqual(isinstance(failure_reply, OutboundCard), files)
                        self.assertEqual(str(failure_reply).count("ou_initiator"), int(mention))
                        self.assertEqual(self.channel.send_calls, [])
                        self.assert_cleaned(outcome)
                        presentations.append(list(self.channel.replies))
                    self.assertEqual(presentations[0], presentations[1])

    async def test_unconfirmed_partial_with_nonempty_final_keeps_existing_suppression(self):
        outcome = self.outcome(
            partial_answers=(PartialAnswer("native-one", "turn-one", "answer", "partial"),),
        )
        await self.suppress(outcome)
        self.channel.reply_results.append(retryable_sent_result())
        with self.assertLogs("netizen_cli.channel_app", "WARNING"):
            await self.app.handle_completion(outcome)
        self.assertEqual(len(self.channel.replies), 1)
        self.assertNotIn("private result", str(self.channel.replies))
        self.assertNotIn("投递未确认", str(self.channel.replies))
        self.assert_cleaned(outcome)

    async def test_completion_mentions_follow_setting_with_and_without_existing_card(self):
        for progress in (False, True):
            with self.subTest(progress=progress):
                self.channel.replies.clear()
                self.channel.updates.clear()
                self.channel.send_calls.clear()
                outcome = self.outcome(
                    turn_id=f"mention-{progress}",
                    task_feedback=BindingTaskFeedback(progress_card_enabled=progress, completion_mention_enabled=True),
                )
                if progress:
                    outcome = await self.start_progress(outcome)
                anchor = "om_progress" if progress else self.origin.id
                self.queue_mention(anchor)
                await self.suppress(outcome)
                await self.app.handle_completion(outcome)
                self.assertEqual(len(self.channel.replies), int(progress))
                self.assertEqual(len(self.channel.send_calls), 1)
                chat_id, content, opts = self.channel.send_calls[0]
                self.assertEqual(chat_id, "oc_direct")
                self.assertIsInstance(content, OutboundText)
                self.assertEqual(content.mentions, [Identity(open_id="ou_initiator")])
                self.assertNotIn("private result", content.text)
                self.assertEqual(opts.reply_to, anchor)
                self.assertFalse(opts.reply_in_thread)
                self.assertEqual(opts.reply_target_gone, "fail")
                self.assert_cleaned(outcome)

    async def test_notification_failure_is_not_retried_or_used_to_republish_suppressed_result(self):
        outcome = self.outcome(task_feedback=BindingTaskFeedback(completion_mention_enabled=True))
        self.channel.send_results.append(TimeoutError("unknown notification receipt"))
        await self.suppress(outcome)
        with self.assertLogs("netizen_cli.channel.completion_mentions", "WARNING"):
            await self.app.handle_completion(outcome)
        self.assertEqual(len(self.channel.send_calls), 1)
        self.assertEqual(self.channel.replies, [])
        self.assert_cleaned(outcome)

    async def test_no_card_bot_notification_keeps_original_topic_and_real_mention(self):
        origin = FakeMessage(
            "work silently", message_id="om_bot", chat_id="oc_group", chat_type="group",
            thread_id="omt_existing", sender_id="ou_other_bot", is_bot=True,
        )
        outcome = self.outcome(
            origin=origin, owner_id="ou_other_bot",
            task_feedback=BindingTaskFeedback(completion_mention_enabled=True),
        )
        self.channel.send_results.append(sent_result(
            "om_notification", chat_id="oc_group", thread_id="omt_existing",
            root_id="om_topic_root", parent_id=origin.id,
        ))
        await self.suppress(outcome)
        await self.app.handle_completion(outcome)
        self.assertEqual(self.channel.replies, [])
        self.assertEqual(len(self.channel.send_calls), 1)
        chat_id, content, opts = self.channel.send_calls[0]
        self.assertEqual(chat_id, "oc_group")
        self.assertEqual(content.mentions, [Identity(open_id="ou_other_bot")])
        self.assertEqual(opts.reply_to, origin.id)
        self.assertTrue(opts.reply_in_thread)
        self.assertEqual(opts.reply_target_gone, "fail")

    async def test_next_physical_turn_is_visible_without_another_tool_call(self):
        first = self.outcome()
        await self.suppress(first)
        await self.app.handle_completion(first)
        second = self.outcome(turn_id="turn-two")
        await self.app.handle_completion(second)
        self.assertEqual(self.channel.replies, [(self.origin.id, "private result")])

    async def test_accepted_input_in_same_physical_turn_does_not_reset_suppression(self):
        outcome = self.outcome()
        await self.suppress(outcome)
        self.runtime.turn_activity_values[self.binding.id] = replace(
            self.runtime.turn_activity_values[self.binding.id], revision=2, steer_count=1,
        )
        await self.app.handle_completion(outcome)
        self.assertEqual(self.channel.replies, [])
        self.assert_cleaned(outcome)

    async def test_exception_paths_preserve_existing_rendering_and_reactions(self):
        cases = (
            ("failed", None), ("interrupted", None),
            ("completed", RuntimeError("execution failed")),
            (None, TerminalStateUnknown("terminal state unknown")),
        )
        for progress in (False, True):
            for status, error in cases:
                with self.subTest(progress=progress, status=status, error=error):
                    presentations = []
                    for suppress in (False, True):
                        self.channel.replies.clear()
                        self.channel.updates.clear()
                        self.channel.reactions.clear()
                        outcome = self.outcome(
                            error=error,
                            result=SimpleNamespace(status=status, final_response="original answer", items=()),
                            task_feedback=replace(self.feedback, progress_card_enabled=progress),
                            partial_answers=(PartialAnswer("native-one", "turn-one", "answer", "original partial answer"),),
                        )
                        if progress:
                            outcome = await self.start_progress(outcome)
                        if suppress:
                            await self.suppress(outcome)
                        await self.app.handle_completion(outcome)
                        presentations.append((list(self.channel.replies), list(self.channel.updates), list(self.channel.reactions)))
                        self.assert_cleaned(outcome)
                    self.assertEqual(presentations[0], presentations[1])

    async def test_observation_unavailable_notice_is_preserved_and_recovery_keeps_turn_choice(self):
        outcome = self.outcome()
        await self.suppress(outcome)
        await self.app.handle_completion(TurnObservationUnavailableOutcome(
            binding_id=self.binding.id, thread_id=outcome.thread_id, turn_id=outcome.turn_id,
            owner_id=outcome.owner_id, origin=self.origin,
            error=TerminalStateUnknown("bounded observation failed"),
        ))
        self.assertIn("bounded observation failed", self.channel.replies[-1][1])
        replies = list(self.channel.replies)
        await self.app.handle_completion(outcome)
        self.assertEqual(self.channel.replies, replies)
        self.assertNotIn("private result", str(self.channel.replies))
        self.assert_cleaned(outcome)

    async def test_failed_progress_update_restores_original_answer_image_files_and_partial_fallback(self):
        (self.fixture.project / "secret.txt").write_text("private file")
        (self.fixture.project / "secret.png").write_bytes(PNG)
        outcome = await self.start_progress(self.outcome(
            result=completed_turn_result(
                file_change_item("secret.txt"), final_response="original answer\n\n![image](secret.png)",
            ),
            partial_answers=(PartialAnswer("native-one", "turn-one", "late", "original partial answer"),),
            task_feedback=replace(self.feedback, progress_card_enabled=True),
        ))
        await self.suppress(outcome)
        self.channel.card_update_results.extend(retryable_sent_result() for _ in range(3))
        with self.assertLogs("netizen_cli.channel.reply_presenter", "ERROR"):
            await self.app.handle_completion(outcome)
        self.assertEqual(len(self.channel.upload_calls), 1)
        rendered = str(self.channel.replies[1:])
        self.assertIn("original answer", rendered)
        self.assertIn("original partial answer", rendered)
        self.assertIn("secret.txt", rendered)
        self.assertTrue(any(isinstance(content, OutboundCard) for _, content in self.channel.replies[1:]))
        self.assert_cleaned(outcome)

    async def test_goal_final_physical_turn_is_suppressed_and_existing_logical_card_finishes(self):
        outcome = await self.start_progress(self.goal_outcome(
            task_feedback=replace(self.feedback, progress_card_enabled=True),
        ))
        await self.suppress(outcome)
        await self.app.handle_completion(outcome)
        self.assertEqual(len(self.channel.replies), 1)
        self.assertEqual(len(self.channel.updates), 1)
        self.assertNotIn("private Goal result", str(self.channel.updates))
        self.assertTrue(all(not item["expanded"] for item in _elements(self.channel.updates[0][1], "collapsible_panel")))
        self.assert_cleaned(outcome)

    async def test_goal_rollover_does_not_inherit_silence_or_emit_early_completion(self):
        first = self.goal_outcome(final_physical_turn_id="goal-turn-one")
        await self.suppress(first)
        self.assertEqual(self.channel.replies, [])
        self.assertEqual(self.channel.reactions, [])
        second = replace(first, final_physical_turn_id="goal-turn-two", final_response="visible next Turn result")
        self.runtime.goal_activity_values[self.binding.id] = replace(
            self.runtime.goal_activity_values[self.binding.id], physical_turn_id=second.final_physical_turn_id,
        )
        await self.app.handle_completion(second)
        self.assertEqual(self.channel.replies, [(self.origin.id, "visible next Turn result")])
        self.assert_cleaned(first)

    async def test_completed_goal_uses_terminal_status_with_or_without_stale_stopping_activity(self):
        for progress in (False, True):
            with self.subTest(progress=progress):
                self.channel.replies.clear()
                self.channel.updates.clear()
                outcome = self.goal_outcome(task_feedback=replace(self.feedback, progress_card_enabled=progress))
                if progress:
                    outcome = await self.start_progress(outcome)
                await self.suppress(outcome)
                # A stop request may leave old display activity while the authoritative
                # completion already says completed. No stop or Runtime behavior changes.
                snapshot = replace(
                    self.runtime.goal_activity_values[self.binding.id], state=GoalOperationState.PAUSING,
                )
                self.runtime.goal_activity_values[self.binding.id] = snapshot
                await self.app.handle_completion(replace(outcome, activity=snapshot if progress else None))
                self.assertNotIn(outcome.final_response, str(self.channel.replies) + str(self.channel.updates))
                self.assertEqual(len(self.channel.replies), int(progress))
                self.assertEqual(len(self.channel.updates), int(progress))
                self.assert_cleaned(outcome)

    async def test_goal_silent_final_delivers_partial_answers_from_every_physical_turn(self):
        earlier = PartialAnswer("native-one", "goal-turn-one", "earlier", "retained earlier answer")
        current = PartialAnswer("native-one", "goal-turn-final", "current", "current physical Turn answer")
        outcome = self.goal_outcome(partial_answers=(earlier, current))
        await self.suppress(outcome)
        await self.app.handle_completion(outcome)
        self.assertEqual(len(self.channel.replies), 2)
        self.assertIn(earlier.text, self.channel.replies[0][1].markdown)
        self.assertIn(current.text, self.channel.replies[1][1].markdown)
        self.assertNotIn(outcome.final_response, str(self.channel.replies))
        self.assert_cleaned(outcome)

    async def test_goal_control_and_finalization_errors_restore_original_answer_and_files(self):
        (self.fixture.project / "goal.txt").write_text("goal output")
        for error, finalization, finalization_error in (
            (RuntimeError("control receipt unknown"), GoalFinalizationStatus.NOT_APPLICABLE, None),
            (None, GoalFinalizationStatus.UNKNOWN, RuntimeError("automatic clear unknown")),
        ):
            with self.subTest(error=error, finalization=finalization):
                self.channel.replies.clear()
                outcome = self.goal_outcome(
                    error=error, finalization=finalization, finalization_error=finalization_error,
                    final_items=(file_change_item("goal.txt"),),
                )
                await self.suppress(outcome)
                await self.app.handle_completion(outcome)
                rendered = str(self.channel.replies)
                self.assertIn("private Goal result", rendered)
                self.assertIn("goal.txt", rendered)
                self.assertIn(str(error or finalization_error), rendered)
                self.assert_cleaned(outcome)

    async def test_open_side_suppresses_only_its_own_turn(self):
        route = self.fixture.store.create_side_topic(
            app_id="cli_test", chat_id="oc_direct", source_message_id="om_side_source",
            parent_binding_id=self.binding.id, creator_id="ou_initiator", requires_mention=False,
        )
        self.fixture.store.set_side_topic_root(route.id, "om_side_root")
        self.fixture.store.open_side_topic(route.id, "omt_side")
        self.runtime.side_snapshots[route.id] = SideSessionSnapshot(
            side_id=route.id, parent_binding_id=self.binding.id, parent_thread_id="native-one",
            thread_id="native-side", project_alias="test", cwd=self.fixture.project,
            creator_id="ou_initiator", state=SideSessionState.OPEN,
            topic_id="omt_side", root_message_id="om_side_root", turn_id="side-turn",
            turn_state=ActiveState.RUNNING, last_activity=0,
        )
        side = SideTurnOutcome(
            side_id=route.id, parent_binding_id=self.binding.id, thread_id="native-side",
            turn_id="side-turn", owner_id="ou_initiator",
            origin=FakeMessage("side work", message_id="om_side", thread_id="omt_side"),
            cwd=self.fixture.project, task_feedback=self.feedback,
            result=completed_turn_result(final_response="private side result"),
        )
        await self.suppress(side)
        self.assertFalse(self.app._reply_suppression.is_suppressed("native-one", "turn-one", "turn-one"))
        await self.app.handle_completion(side)
        self.assertEqual(self.channel.replies, [])
        await self.app.handle_completion(self.outcome())
        self.assertEqual(self.channel.replies, [(self.origin.id, "private result")])
        self.assert_cleaned(side)

    async def test_native_discard_and_app_close_release_suppression_state(self):
        outcome = self.outcome()
        await self.suppress(outcome)
        await self.app.handle_completion(ThreadActivityDiscardedOutcome(
            binding_id=self.binding.id, thread_id=outcome.thread_id, turn_id=outcome.turn_id,
        ))
        self.assert_cleaned(outcome)
        await self.suppress(outcome)
        await self.app.close()
        self.assert_cleaned(outcome)


class ScheduledSuppressReplyTest(unittest.IsolatedAsyncioTestCase):
    async def test_intentional_suppression_records_distinct_receipt_without_sent_or_auto_archive(self):
        for progress in (False, True):
            with self.subTest(progress=progress):
                await self.assert_suppressed_receipt(progress=progress)

    async def test_unconfirmed_partial_failure_receipt_is_not_overwritten_by_suppression(self):
        with self.assertLogs("netizen_cli.channel_app", "WARNING"):
            await self.assert_suppressed_receipt(progress=False, partial_failure=True)

    async def assert_suppressed_receipt(self, *, progress, partial_failure=False):
        fixture = await self.enterAsyncContext(scheduled_channel_fixture())
        settings = SessionSettings(task_feedback=BindingTaskFeedback(
            reaction_pulse_enabled=False, progress_card_enabled=progress,
        ))
        created = fixture.store.schedules.create(
            name="silent", instructions="work silently", project_alias="work", app_id="app",
            chat_id="oc_group", auto_archive=True, session_settings=settings,
            workspace=WorkspacePolicy(),
            schedule=ScheduleRule("interval", "UTC", every_minutes=1, anchor=100),
            request_id="create-silent", now=100,
        )
        claim = fixture.store.schedules.claim_due(created.plan_id, app_id="app", now=160)
        self.assertIsNotNone(claim)
        fixture.channel.send_results.append(sent_result(
            "om_root", chat_id="oc_group", thread_id="omt_fresh", root_id="om_root",
        ))
        if progress:
            fixture.channel.reply_results.append(sent_result(
                "om_progress", chat_id="oc_group", thread_id="omt_fresh",
            ))
        submit_initial = fixture.runtime.submit_initial

        async def submit_with_activity(**kwargs):
            submission = await submit_initial(**kwargs)
            fixture.runtime.turn_activity_values[submission.binding_id] = turn_activity_snapshot(
                binding_id=submission.binding_id, thread_id=submission.thread_id,
                turn_id=submission.turn_id,
            )
            return submission

        fixture.runtime.submit_initial = submit_with_activity
        await fixture.app.dispatch_scheduled_run(claim)
        request, = fixture.submissions
        fixture.store.schedules.release(claim.run.id)
        thread_id = "native-" + claim.run.id
        fixture.runtime.turn_activity_values[request["binding"].id] = turn_activity_snapshot(
            binding_id=request["binding"].id, thread_id=thread_id, turn_id="turn-initial",
        )
        await fixture.app.suppress_reply(source_thread_id=thread_id, source_turn_id="turn-initial")
        if partial_failure:
            fixture.channel.reply_results.extend((
                retryable_sent_result(),
                sent_result("om_failure_notice", chat_id="oc_group", thread_id="omt_fresh"),
            ))
        with patch.object(fixture.app, "_archive_scheduled_completion", new_callable=AsyncMock) as archive:
            await fixture.app.handle_completion(TurnOutcome(
                binding_id=request["binding"].id, thread_id=thread_id, turn_id="turn-initial",
                owner_id=request["owner_id"], origin=request["origin"], cwd=request["cwd"],
                task_feedback=settings.task_feedback,
                result=completed_turn_result(final_response="" if partial_failure else "private scheduled result"),
                partial_answers=(
                    (PartialAnswer(thread_id, "turn-initial", "answer", "partial"),) if partial_failure else ()
                ),
            ))
        run = fixture.store.schedules.get_run(claim.run.id)
        self.assertEqual((run.delivery_state, run.barrier), ("unknown" if partial_failure else "suppressed", "released"))
        self.assertEqual(len(fixture.channel.replies), 2 if partial_failure else int(progress))
        if partial_failure:
            self.assertIn("任务已完成，但阶段性答案投递未确认。", str(fixture.channel.replies[-1]))
        self.assertEqual(len(fixture.channel.updates), int(progress))
        self.assertNotIn("private scheduled result", str(fixture.channel.updates))
        self.assertEqual(len(fixture.channel.send_calls), 1)
        archive.assert_not_awaited()
