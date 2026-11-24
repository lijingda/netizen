"""Goal management is static; each SDK logical execution uses ordinary replies."""
from __future__ import annotations

import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from lark_channel import OutboundCard

from netizen_cli.bindings import BindingTaskFeedback
from netizen_cli.cards import decode_turn_file_action
from netizen_cli.domain import (
    FeishuScope, GoalOperationState, GoalStatus,
    NativeCapability, ScopeKind,
)
from netizen_cli.partial_answers import PartialAnswer
from netizen_cli.runtime.codex_contracts import ActiveGoalSnapshot, GoalOutcome
from netizen_cli.runtime.contracts import GoalFinalizationStatus, GoalSubmission
from tests.support.presenter_traces import expect_presenter_trace
from tests.test_goal_presentation_traces import REFERENCE
from tests.support.channel_cards import _elements, direct_button_event
from tests.support.channel_fixtures import channel_fixture
from tests.support.channel_messages import FakeMessage
from tests.support.channel_results import (
    file_change_item, goal_activity_snapshot, native_goal, sent_result,
)


def rendered(value):
    return json.dumps(value.card if isinstance(value, OutboundCard) else value, ensure_ascii=False, default=str)


def button(card, intent):
    return next(
        value for node in _elements(card.card, "button")
        for behavior in node.get("behaviors", ())
        if isinstance(value := behavior.get("value"), dict) and value.get("intent") == intent
    )


class GoalReplySeparationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fx = await self.enterAsyncContext(channel_fixture())
        self.app, self.runtime, self.channel = self.fx.app, self.fx.runtime, self.fx.channel
        self.scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        await self.fx.new()
        binding = self.fx.store.active_binding(self.scope.key)
        self.fx.store.assign_native_thread_id(binding.id, "native-one")
        self.binding = self.fx.store.get(binding.id)
        self.runtime.available_capabilities = frozenset({NativeCapability.GOAL})
        self.runtime.goal_snapshot_value = native_goal()
        self.activity = replace(goal_activity_snapshot(binding_id=binding.id), physical_turn_id="goal-one")
        self.runtime.goal_activity_values[binding.id] = self.activity
        self.feedback = BindingTaskFeedback()
        self.release_count = 0
        self.origin = FakeMessage("/goal ship safely", message_id="om_goal")
        self.runtime.goal_submission = self.submission()

    def submission(self, logical_id="goal-one", **kwargs):
        def release():
            self.release_count += 1
        return GoalSubmission(self.binding.id, "native-one", logical_id, release,
            task_feedback=self.feedback, **kwargs)

    def outcome(self, **kwargs):
        values = dict(
            binding_id=self.binding.id, thread_id="native-one", logical_turn_id="goal-one",
            owner_id="ou_user", origin=self.origin, goal=native_goal(GoalStatus.COMPLETE),
            final_physical_turn_id=self.activity.physical_turn_id, final_turn_status="completed",
            final_response="final answer", task_feedback=self.feedback, activity=self.activity,
            finalization=GoalFinalizationStatus.CLEARED,
        )
        values.update(kwargs)
        return GoalOutcome(**values)

    async def start(self, *, progress=False):
        self.feedback = BindingTaskFeedback(progress_card_enabled=progress)
        self.runtime.goal_submission = self.submission()
        self.channel.reply_results.extend([
            sent_result("om_management", chat_id="oc_direct"),
            sent_result("om_task", chat_id="oc_direct"),
        ])
        await self.app.handle_message(self.origin)

    async def test_status_is_standalone_static_and_refresh_reads_exact_binding(self):
        await self.app.handle_message(FakeMessage("/goal", message_id="om_query"))
        card = self.channel.replies[-1][1]
        self.assertIsInstance(card, OutboundCard)
        self.assertIn("goal.refresh", rendered(card))
        self.assertEqual(self.app._progress_cards._sessions, {})
        self.assertEqual(self.runtime.goal_activity_calls, [])
        await self.fx.create_binding(self.scope)
        self.runtime.goal_snapshot_value = native_goal(GoalStatus.PAUSED)
        await self.app.handle_card_action(direct_button_event(button(card, "goal.refresh"), message_id="om_management"))
        self.assertEqual(self.runtime.goal_snapshot_calls[-1], self.binding.id)
        self.assertEqual(self.channel.updates[-1][0], "om_management")
        self.assertIn("goal-paused", rendered(self.channel.updates[-1][1]))
        self.assertEqual(self.app._progress_cards._sessions, {})

    async def test_no_progress_uses_normal_text_reply_and_releases_logical_state(self):
        await self.start()
        self.assertEqual(len(self.channel.replies), 1)
        self.assertIn("Goal 已启动", rendered(self.channel.replies[0][1]))
        await self.app.handle_completion(self.outcome())
        self.assertEqual(getattr(self.channel.replies[-1][1], "markdown", self.channel.replies[-1][1]), "final answer")
        self.assertEqual(self.channel.updates, [])
        self.assertEqual(self.app._progress_cards._sessions, {})
        self.assertEqual(self.release_count, 1)

    async def test_progress_updates_task_card_without_changing_management(self):
        await self.start(progress=True)
        self.assertEqual(len(self.channel.replies), 2)
        self.assertIn("goal.pause", rendered(self.channel.replies[0][1]))
        self.assertNotIn("goal.pause", rendered(self.channel.replies[1][1]))
        await self.app.handle_completion(self.outcome())
        self.assertEqual([item[0] for item in self.channel.updates], ["om_task"])
        self.assertIn("final answer", rendered(self.channel.updates[-1][1]))
        self.assertNotIn("goalmodulev1", rendered(self.channel.updates[-1][1]))
        self.assertEqual(self.app._progress_cards._sessions, {})

    async def test_pause_and_clear_only_control_goal_and_keep_live_reply(self):
        for action in ("pause", "clear"):
            with self.subTest(action=action):
                self.app._progress_cards.retire_binding(self.binding.id)
                self.channel.replies.clear()
                self.channel.updates.clear()
                self.runtime.goal_snapshot_value = native_goal()
                self.runtime.active_goals[self.binding.id] = ActiveGoalSnapshot(
                    binding_id=self.binding.id, thread_id="native-one", logical_turn_id="goal-one",
                    owner_id="ou_user", state=GoalOperationState.RUNNING, persisted=native_goal(),
                )
                await self.start(progress=True)
                session = self.app._progress_cards._sessions[(self.binding.id, "native-one", "goal-one")]
                management = self.channel.replies[0][1]
                await self.app.handle_card_action(direct_button_event(button(management, "goal." + action), message_id="om_management"))
                self.assertEqual(self.runtime.stop_calls, [])
                self.assertFalse(session.stopped.is_set())
                self.assertEqual(self.channel.updates[-1][0], "om_management")
                self.assertNotIn("turnanswerv1", rendered(self.channel.updates[-1][1]))
                if action == "pause":
                    self.assertIn("goal-paused", rendered(self.channel.updates[-1][1]))
                else:
                    self.assertIn("当前 Codex 会话没有 Goal", rendered(self.channel.updates[-1][1]))
                await self.app.handle_completion(self.outcome(
                    goal=None if action == "clear" else native_goal(GoalStatus.PAUSED),
                    finalization=GoalFinalizationStatus.CLEARED if action == "clear" else GoalFinalizationStatus.NOT_APPLICABLE,
                ))
                self.assertEqual(self.channel.updates[-1][0], "om_task")
                self.assertIn("final answer", rendered(self.channel.updates[-1][1]))
                self.assertEqual(self.app._progress_cards._sessions, {})

    async def test_resume_running_logical_stream_does_not_create_second_task_reply(self):
        await self.start(progress=True)
        self.runtime.goal_snapshot_value = native_goal(GoalStatus.PAUSED)
        self.runtime.goal_submission = self.submission(continued=True)
        await self.app.handle_message(FakeMessage("/goal resume", message_id="om_resume"))
        self.assertEqual(len(self.app._progress_cards._sessions), 1)
        self.assertEqual(len(self.channel.replies), 3)
        self.assertIn("Goal 已恢复", rendered(self.channel.replies[-1][1]))
        self.assertEqual(self.runtime.resume_goal_calls[-1]["origin"].id, "om_resume")

    async def test_resume_after_logical_terminal_creates_new_reply(self):
        await self.start(progress=True)
        await self.app.handle_completion(self.outcome(goal=native_goal(GoalStatus.PAUSED), finalization=GoalFinalizationStatus.NOT_APPLICABLE))
        self.runtime.goal_snapshot_value = native_goal(GoalStatus.PAUSED)
        self.runtime.goal_activity_values[self.binding.id] = replace(self.activity, logical_turn_id="goal-two", physical_turn_id="goal-two")
        self.runtime.goal_submission = self.submission("goal-two")
        self.channel.reply_results.append(sent_result("om_task_two", chat_id="oc_direct"))
        await self.app.handle_message(FakeMessage("/goal resume", message_id="om_resume"))
        self.assertIn((self.binding.id, "native-one", "goal-two"), self.app._progress_cards._sessions)
        self.assertNotIn((self.binding.id, "native-one", "goal-one"), self.app._progress_cards._sessions)
        self.assertEqual([item[0] for item in self.channel.updates], ["om_task"])

    async def test_controls_survive_restart_but_changed_goal_has_no_mutation(self):
        await self.app.handle_message(FakeMessage("/goal", message_id="om_query"))
        card = self.channel.replies[-1][1]
        self.assertEqual(self.app._progress_cards._sessions, {})
        await self.fx.create_binding(self.scope)
        await self.app.handle_card_action(direct_button_event(button(card, "goal.pause")))
        self.assertEqual([b.id for b in self.runtime.pause_goal_calls], [self.binding.id])
        self.channel.updates.clear()
        self.runtime.goal_snapshot_value = native_goal(created_at=4)
        await self.app.handle_card_action(direct_button_event(button(card, "goal.pause")))
        self.assertEqual(len(self.runtime.pause_goal_calls), 1)
        self.assertIn("Goal 已变化", rendered(self.channel.updates[-1][1]))

    async def test_files_use_final_physical_turn_and_page_without_goal_state(self):
        await self.start(progress=True)
        await self.assert_files_page()

    async def rollover(self, first, second):
        previous = self.outcome(final_response="first final", partial_answers=(first,), run_continues=True)
        self.activity = replace(self.activity, physical_turn_id=second.turn_id, partial_answers=(second,), revision=2)
        self.runtime.goal_activity_values[self.binding.id] = self.activity
        await self.app.handle_goal_turn(previous, False)
        await self.app.handle_goal_turn(replace(previous, final_physical_turn_id=self.activity.physical_turn_id, activity=self.activity), True)

    async def test_rollover_partials_stay_on_their_own_cards_through_file_paging(self):
        expect_presenter_trace(self, self.fx, "goal_rollover_and_paging", reference=REFERENCE)
        first = PartialAnswer("native-one", "goal-one", "item-1", "first partial")
        second = PartialAnswer("native-one", "physical-final", "item-1", "second partial")
        await self.start(progress=True)
        self.channel.reply_results.append(sent_result("om_final", chat_id="oc_direct"))
        await self.rollover(first, second)
        self.assertEqual(self.channel.updates[0][0], "om_task")
        self.assertIn(first.text, rendered(self.channel.updates[0][1]))
        self.assertNotIn(second.text, rendered(self.channel.updates[0][1]))
        await self.assert_files_page(partials=(second,), card_id="om_final")

    async def assert_files_page(self, *, partials=(), card_id="om_task"):
        paths = [self.fx.project / f"goal-{index}.txt" for index in range(10)]
        for path in paths:
            path.write_text("content")
        self.channel.send_results.append(sent_result(
            "om_reminder", chat_id="oc_direct", parent_id=card_id, root_id="om_goal",
        ))
        await self.app.handle_completion(self.outcome(
            final_items=(file_change_item(*(str(p) for p in paths)),), cwd=self.fx.project,
            partial_answers=partials,
        ))
        card = OutboundCard(card=self.channel.updates[-1][1])
        value = button(card, "turn-file.page")
        self.assertEqual(value["turn_id"], "turn:v1:" + self.activity.physical_turn_id)
        self.assertIsNone(value.get("reply", {}).get("goal"))
        self.runtime.goal_snapshot_calls.clear()
        intent = decode_turn_file_action(
            app_id="cli_test", message_id=card_id, callback_chat_id="oc_direct",
            sender_id="ou_user", tag="button", form_value={"turn_file_page": "1"}, value=value,
        )
        await self.app._turn_file_action(intent)
        self.assertEqual(self.runtime.goal_snapshot_calls, [])
        self.assertIn("final answer", rendered(self.channel.updates[-1][1]))
        self.assertEqual(button(OutboundCard(card=self.channel.updates[-1][1]), "turn-file.page")["page"], 1)
        for partial in partials:
            self.assertIn(partial.text, rendered(self.channel.updates[-1][1]))
        self.assertEqual(self.app._progress_cards._sessions, {})

    async def check_both_file_pages(self, progress):
        await self.start(progress=progress)
        self.channel.reply_results.extend(sent_result(f"om_files_{i}", chat_id="oc_direct") for i in range(4))
        pages = []
        for physical, continues in (("goal-one", True), ("second", False)):
            paths = [self.fx.project / f"{physical}-{i}.txt" for i in range(10)]
            for path in paths:
                path.touch()
            result = self.outcome(final_items=(file_change_item(*(str(p) for p in paths)),),
                                  cwd=self.fx.project, final_response=physical, run_continues=continues)
            if continues:
                self.activity = replace(self.activity, physical_turn_id="second")
                self.runtime.goal_activity_values[self.binding.id] = self.activity
                await self.app.handle_goal_turn(result, False)
                await self.app.handle_goal_turn(replace(result, final_physical_turn_id=self.activity.physical_turn_id, activity=self.activity), True)
            else:
                await self.app.handle_completion(result)
            card = OutboundCard(card=self.channel.updates[-1][1]) if progress else self.channel.replies[-1][1]
            pages.append((physical, continues, button(card, "turn-file.page")))
        for physical, continues, value in pages:
            self.assertEqual(value["turn_id"], f"turn:v1:{physical}")
            self.assertEqual(bool(value.get("reply", {}).get("run_continues")), continues)
            intent = decode_turn_file_action(app_id="cli_test", message_id=f"om_{physical}", callback_chat_id="oc_direct",
                sender_id="ou_user", tag="button", form_value={"turn_file_page": "1"}, value=value)
            await self.app._turn_file_action(intent)
            redrawn = self.channel.updates[-1][1]
            self.assertEqual(redrawn["header"]["title"]["content"], "本轮已完成" if continues else "任务已完成")
            self.assertIn(f"{physical}-8.txt", rendered(redrawn))
            self.assertNotIn(f"{'second' if continues else 'goal-one'}-8.txt", rendered(redrawn))

    async def test_both_progress_manifests_redraw_after_goal_disposal(self):
        await self.check_both_file_pages(True)

    async def test_both_files_only_manifests_redraw_after_goal_disposal(self):
        await self.check_both_file_pages(False)

    async def test_plain_goal_delivers_each_partial_and_final_once(self):
        expect_presenter_trace(self, self.fx, "goal_plain_rollover", reference=REFERENCE)
        first = PartialAnswer("native-one", "goal-one", "item", "first partial")
        second = PartialAnswer("native-one", "physical-final", "item", "second partial")
        await self.start()
        self.channel.reply_results.extend(sent_result(f"om_post_{i}", chat_id="oc_direct") for i in range(4))
        await self.rollover(first, second)
        await self.app.handle_completion(self.outcome(partial_answers=(second,)))
        posts = [getattr(post, "markdown", post) for _, post in self.channel.replies[1:]]
        self.assertEqual(posts, ["**阶段性答案**\n\nfirst partial", "first final",
                                 "**阶段性答案**\n\nsecond partial", "final answer"])

    async def test_interrupted_goal_preserves_earlier_partial_without_files_or_mention(self):
        expect_presenter_trace(self, self.fx, "goal_interrupted", reference=REFERENCE)
        earlier = PartialAnswer("native-one", "goal-one", "item", "earlier partial")
        self.runtime.goal_activity_values[self.binding.id] = replace(
            self.activity, physical_turn_id=earlier.turn_id, partial_answers=(earlier,),
        )
        await self.start(progress=True)
        path = self.fx.project / "unfinished.txt"
        path.touch()
        await self.app.handle_completion(self.outcome(
            final_turn_status="interrupted", final_items=(file_change_item(str(path)),), cwd=self.fx.project,
            partial_answers=(earlier,), goal=native_goal(GoalStatus.PAUSED),
            background_cleanup_requested=True, finalization=GoalFinalizationStatus.NOT_APPLICABLE,
        ))
        message_id, terminal = self.channel.updates[-1]
        self.assertEqual(message_id, "om_task")
        self.assertIn("中断", rendered(terminal))
        self.assertIn(earlier.text, rendered(terminal))
        self.assertNotIn("turn-file.send", rendered(terminal))
        self.assertFalse(self.channel.send_calls)

    async def test_interrupted_final_turn_never_gets_files(self):
        path = self.fx.project / "interrupted.txt"
        path.write_text("unfinished")
        await self.app.handle_completion(self.outcome(
            final_turn_status="interrupted", final_items=(file_change_item(str(path)),),
            goal=native_goal(GoalStatus.PAUSED), finalization=GoalFinalizationStatus.NOT_APPLICABLE,
        ))
        self.assertNotIn("turn-file.send", rendered(self.channel.replies[-1][1]))
        self.assertIn("中断", rendered(self.channel.replies[-1][1]))

    async def test_confirmed_answer_survives_uncertain_control_or_finalization(self):
        for change in ({"error": RuntimeError("steer receipt unknown")}, {
            "finalization": GoalFinalizationStatus.UNKNOWN,
            "finalization_error": RuntimeError("clear receipt unknown"),
        }):
            with self.subTest(change=change):
                await self.app.handle_completion(self.outcome(**change))
                text = rendered(self.channel.replies[-1][1])
                self.assertIn("final answer", text)
                self.assertIn("未确认", text)
        await self.app.handle_completion(self.outcome(error=RuntimeError("unknown"), final_turn_status=None, final_physical_turn_id=None))
        self.assertNotIn("final answer", rendered(self.channel.replies[-1][1]))

    async def test_goal_query_unknown_without_snapshot_does_not_claim_absent(self):
        self.runtime.goal_snapshot_value = None
        self.runtime.active_goals[self.binding.id] = ActiveGoalSnapshot(
            binding_id=self.binding.id, thread_id="native-one", logical_turn_id=None,
            owner_id="ou_user", state=GoalOperationState.UNKNOWN, persisted=None,
        )
        await self.app.handle_message(FakeMessage("/goal", message_id="om_unknown"))
        text = rendered(self.channel.replies[-1][1])
        self.assertIn("Goal 状态未确认", text)
        self.assertNotIn("当前 Codex 会话没有 Goal", text)
        self.assertNotIn('"intent": "goal.clear"', text)

    async def test_accepted_start_keeps_receipt_and_task_presentation_when_status_card_fails(self):
        self.feedback = BindingTaskFeedback(progress_card_enabled=True)
        self.runtime.goal_submission = self.submission()
        self.channel.reply_results.extend([
            sent_result("om_receipt", chat_id="oc_direct"),
            sent_result("om_task", chat_id="oc_direct"),
        ])
        with patch.object(self.app, "_goal_management_card", side_effect=RuntimeError("display unavailable")):
            await self.app.handle_message(self.origin)
        self.assertEqual(len(self.runtime.start_goal_calls), 1)
        self.assertEqual(self.release_count, 1)
        self.assertIn("Goal 已启动", rendered(self.channel.replies[0][1]))
        self.assertNotIn("请求处理未完成", rendered(self.channel.replies))
        self.assertIn((self.binding.id, "native-one", "goal-one"), self.app._progress_cards._sessions)

    async def test_accepted_clear_receipt_is_preserved_when_management_refresh_fails(self):
        await self.app.handle_message(FakeMessage("/goal", message_id="om_query"))
        card = self.channel.replies[-1][1]
        with patch.object(self.app, "_goal_management_card", side_effect=RuntimeError("display unavailable")):
            await self.app.handle_card_action(direct_button_event(button(card, "goal.clear")))
        self.assertEqual(len(self.runtime.clear_goal_calls), 1)
        self.assertIn("Goal 已结束", rendered(self.channel.replies[-1][1]))
        self.assertNotIn("操作未完成", rendered(self.channel.replies[-1][1]))

    async def test_progress_delivery_failure_falls_back_and_releases_state(self):
        await self.start(progress=True)
        self.channel.fail_card_updates = True
        with patch("netizen_cli.channel.reply_presenter._TERMINAL_CARD_RETRY_SECONDS", 0):
            await self.app.handle_completion(self.outcome())
        self.assertEqual(getattr(self.channel.replies[-1][1], "markdown", self.channel.replies[-1][1]), "final answer")
        self.assertEqual(self.app._progress_cards._sessions, {})
