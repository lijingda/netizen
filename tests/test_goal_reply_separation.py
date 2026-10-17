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
from netizen_cli.runtime.contracts import ActiveGoalSnapshot, GoalFinalizationStatus, GoalOutcome, GoalSubmission
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
        self.activity = goal_activity_snapshot(binding_id=binding.id)
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
            final_physical_turn_id="physical-final", final_turn_status="completed",
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
                    self.assertIn("当前原生 Thread 没有 Goal", rendered(self.channel.updates[-1][1]))
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
        self.runtime.goal_activity_values[self.binding.id] = replace(self.activity, logical_turn_id="goal-two")
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
        paths = [self.fx.project / f"goal-{index}.txt" for index in range(10)]
        for path in paths:
            path.write_text("content")
        await self.app.handle_completion(self.outcome(final_items=(file_change_item(*(str(p) for p in paths)),)))
        card = OutboundCard(card=self.channel.updates[-1][1])
        value = button(card, "turn-file.page")
        self.assertEqual(value["turn_id"], "turn:v1:physical-final")
        self.assertNotIn("goal", value.get("reply", {}))
        self.runtime.goal_snapshot_calls.clear()
        intent = decode_turn_file_action(
            app_id="cli_test", message_id="om_task", callback_chat_id="oc_direct",
            sender_id="ou_user", tag="button", form_value={"turn_file_page": "1"}, value=value,
        )
        await self.app._turn_file_action(intent)
        self.assertEqual(self.runtime.goal_snapshot_calls, [])
        self.assertIn("final answer", rendered(self.channel.updates[-1][1]))
        self.assertEqual(self.app._progress_cards._sessions, {})

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
        self.assertNotIn("当前原生 Thread 没有 Goal", text)
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
