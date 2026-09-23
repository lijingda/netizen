from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from lark_channel import Identity, OutboundPost

from netizen.bindings import BindingStore, BindingTaskFeedback, BindingTurnSettings
from netizen.channel_app import ChannelApplication
from netizen.codex_runtime import CodexRuntime
from netizen.domain import (
    FeishuScope,
    MentionContextMode,
    MessageContextAnchor,
    ScheduledBindingOrigin,
    ScopeKind,
)
from netizen.management import (
    InstanceManagementService,
    ManagementRuntimePort,
    ScopeCoordinator,
)
from netizen.message_history import (
    MessageHistoryRef,
    MessageHistoryStats,
    MessageHistoryUnavailable,
    MessageHistoryWindow,
)
from netizen.projects import ProjectRegistry
from netizen.schedules.models import ScheduleRule
from tests.support.channel_messages import FakeChannel, FakeMessage, FakeMessageHistory
from tests.support.channel_results import sent_result
from tests.test_codex_runtime import FakeCodex, FakeGoalControl, FakeTerminalCleanup, FakeThread


class BindingScheduledChannelTest(unittest.IsolatedAsyncioTestCase):
    """Exercise saved inputs through the real Channel preparation and Runtime."""

    async def asyncSetUp(self):
        self.cwd = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.now = 100.0
        self.store = self.enterContext(closing(BindingStore(wall_clock=lambda: self.now)))
        self.channel = FakeChannel()
        self.channel.reply = AsyncMock(wraps=self.channel.reply)
        self.history = FakeMessageHistory()
        self.history.resolve_topic_reply_anchor = AsyncMock(
            return_value=MessageContextAnchor("om_existing_root", 1_000),
        )
        self.codex = FakeCodex()
        self.enterContext(patch(
            "netizen.codex_runtime.AsyncThread",
            side_effect=lambda codex, thread_id: FakeThread(thread_id, codex),
        ))
        self.runtime = CodexRuntime(
            codex=self.codex, bindings=self.store,
            terminal_cleanup=FakeTerminalCleanup(self.codex.events),
            poll_interval_seconds=0, automatic_thread_naming=False,
        )
        self.projects = ProjectRegistry(
            store=self.store, project_root=self.cwd, projects={"work": self.cwd},
        )
        self.management = InstanceManagementService(
            bindings=self.store, projects=self.projects,
            runtime=ManagementRuntimePort(self.runtime), scope_coordinator=ScopeCoordinator(),
        )
        self.app = ChannelApplication(
            app_id="app", channel=self.channel, runtime=self.runtime,
            bindings=self.store, projects=self.projects, management=self.management,
            message_history=self.history,
        )
        self.outcomes = []

        async def complete(outcome):
            self.outcomes.append(outcome)
            await self.app.handle_completion(outcome)

        self.runtime.set_completion_handler(complete)
        self.sequence = 0

    async def asyncTearDown(self):
        await self.runtime.cancel_tasks()
        await self.app.close()
        await self.management.close()

    def binding(self, *, kind=ScopeKind.GROUP, catch_up=False, materialized=True):
        chat_id = "oc_direct" if kind is ScopeKind.DIRECT else "oc_group"
        self.channel.chat_types[chat_id] = "p2p" if kind is ScopeKind.DIRECT else "group"
        scope = FeishuScope("app", chat_id, kind, "omt_existing" if kind is ScopeKind.TOPIC else None)
        binding = self.store.create_channel_binding(
            scope=scope, project_alias="work", creator_id="ou_creator",
            message_context_mode=MentionContextMode.CATCH_UP if catch_up else MentionContextMode.CURRENT_ONLY,
            context_anchor=MessageContextAnchor("om_boundary", 1_000) if catch_up else None,
        )
        if materialized:
            self.store.assign_native_thread_id(binding.id, "native-" + binding.id)
        return scope, self.store.get(binding.id)

    def claim(self, binding, instructions="检查刚才的结果", *, due=160):
        self.sequence += 1
        scope = self.store.get_scope(binding.scope_key)
        created = self.store.schedules.create(
            name="原会话检查", instructions=instructions, project_alias=binding.project_alias,
            app_id=scope.app_id, chat_id=scope.chat_id,
            schedule=ScheduleRule("interval", "UTC", every_minutes=1, anchor=100),
            request_id=f"create-{self.sequence}", now=100,
            target_kind="binding", target_binding_id=binding.id, session_settings=None,
        )
        return self.next_claim(created.plan_id, due=due)

    def next_claim(self, plan_id, *, due):
        self.now = float(due)
        claim = self.store.schedules.claim_due(plan_id, app_id="app", now=self.now)
        self.assertIsNotNone(claim)
        return claim

    def queue_anchor(self, scope, message_id="om_trigger"):
        self.channel.send_results.append(sent_result(
            message_id, chat_id=scope.chat_id, thread_id=scope.topic_id,
            root_id="om_existing_root" if scope.topic_id else None,
            parent_id="om_existing_root" if scope.topic_id else None,
        ))

    async def finish(self, response="检查完成"):
        self.codex.handles[-1].complete(response=response)
        self.assertTrue(await self.runtime.wait_idle(timeout=1))

    def history_window(self, lower, upper, *candidates):
        self.history.window = MessageHistoryWindow(
            lower=lower, upper=upper, candidates=tuple(candidates),
            stats=MessageHistoryStats(1, len(candidates), 0, 0, 0, False, False),
        )

    async def test_idle_input_resumes_exact_thread_and_finishes_without_human_mention(self):
        scope, binding = self.binding(kind=ScopeKind.DIRECT)
        claim = self.claim(binding, "/stop 只是保存的检查指令")
        self.queue_anchor(scope)

        await self.app.dispatch_scheduled_run(claim)

        self.assertEqual(self.codex.start_kwargs, [])
        self.assertEqual(self.codex.resume_calls, [(binding.native_thread_id, {"include_turns": False})])
        self.assertEqual(self.store.active_binding(scope.key).id, binding.id)
        self.assertEqual(len(self.codex.handles), 1)
        self.assertEqual(self.codex.handles[0].interrupt_count, 0)
        native_input = self.codex.turn_inputs[0][1]
        text, trailer = native_input.split("\n\n<scheduled_plan>\n")
        metadata = json.loads(trailer.removesuffix("\n</scheduled_plan>"))
        self.assertEqual(text, claim.plan.instructions)
        self.assertEqual(metadata["kind"], "scheduled_plan")
        self.assertEqual(metadata["message_id"], "om_trigger")
        self.assertEqual(metadata["plan"]["run_id"], claim.run.id)
        self.assertNotIn("sender", metadata)
        self.assertNotIn("feishu_current_message", native_input)
        self.assertEqual(self.channel.fetch_inbound_calls, [])
        self.assertIn(("om_trigger", "Typing"), self.channel.reactions)
        run = self.store.schedules.get_run(claim.run.id)
        self.assertEqual((run.barrier, run.disposition, run.initial_turn_id), ("released", "started", "turn-1"))
        self.assertEqual(self.outcomes, [])
        send_opts = self.channel.send_calls[0][2]
        self.assertEqual(send_opts.uuid, run.root_uuid)
        self.assertIsNone(send_opts.reply_to)
        self.assertFalse(send_opts.reply_in_thread)

        await self.finish()

        self.assertEqual(self.channel.replies, [("om_trigger", "检查完成")])
        self.assertIsInstance(self.outcomes[0].origin, ScheduledBindingOrigin)
        self.assertFalse(hasattr(self.outcomes[0].origin, "sender"))
        reply_opts = self.channel.reply.await_args.args[2]
        self.assertFalse(reply_opts.reply_in_thread)
        self.assertEqual(reply_opts.reply_target_gone, "fail")

    async def test_next_tick_steers_same_running_turn_and_has_only_original_completion(self):
        scope, binding = self.binding()
        first = self.claim(binding)
        self.queue_anchor(scope, "om_first")
        await self.app.dispatch_scheduled_run(first)
        second = self.next_claim(first.plan.id, due=220)
        self.queue_anchor(scope, "om_second")

        await self.app.dispatch_scheduled_run(second)

        self.assertEqual(len(self.codex.turn_inputs), 1)
        self.assertEqual(len(self.codex.handles[0].steers), 1)
        self.assertIn(second.run.id, self.codex.handles[0].steers[0])
        self.assertIn(("om_second", "OnIt"), self.channel.reactions)
        self.assertNotIn(("om_second", "Typing"), self.channel.reactions)
        runs = [self.store.schedules.get_run(claim.run.id) for claim in (first, second)]
        self.assertEqual([run.disposition for run in runs], ["started", "steered"])
        self.assertEqual([run.initial_turn_id for run in runs], ["turn-1", "turn-1"])
        self.assertEqual([run.barrier for run in runs], ["released", "released"])

        await self.finish()

        self.assertEqual(self.channel.replies, [("om_first", "检查完成")])
        self.assertEqual(len(self.outcomes), 1)

    async def test_steering_human_task_retains_original_origin_and_completion_mention(self):
        scope, binding = self.binding()
        human = FakeMessage(
            "检查资源", message_id="om_human", sender_id="ou_initiator",
            chat_id=scope.chat_id, chat_type="group",
        )
        await self.app.handle_message(human)
        claim = self.claim(binding)
        self.queue_anchor(scope)

        await self.app.dispatch_scheduled_run(claim)
        await self.finish()

        self.assertEqual(len(self.codex.turn_inputs), 1)
        self.assertEqual(len(self.codex.handles[0].steers), 1)
        self.assertEqual(len(self.channel.replies), 1)
        reply_id, content = self.channel.replies[0]
        self.assertEqual(reply_id, human.id)
        self.assertIsInstance(content, OutboundPost)
        self.assertEqual(content.mentions, [Identity(open_id="ou_initiator")])
        self.assertIs(self.outcomes[0].origin, human)
        self.assertEqual(self.outcomes[0].owner_id, "ou_initiator")

    async def test_current_binding_settings_drive_model_and_progress_after_plan_claim(self):
        scope, binding = self.binding()
        claim = self.claim(binding)
        effort = SimpleNamespace(value="high")
        self.codex.model_response = SimpleNamespace(data=[SimpleNamespace(
            id="selected", model="wire-model", display_name="Model", description="",
            is_default=True, default_reasoning_effort=effort, default_service_tier=None,
            supported_reasoning_efforts=[SimpleNamespace(reasoning_effort=effort, description="")],
            service_tiers=[SimpleNamespace(id="priority", name="Fast", description="")],
        )], next_cursor=None)
        feedback = BindingTaskFeedback(True, True, True)
        self.store.set_configuration(
            binding_id=binding.id, expected_settings_revision=binding.settings_revision,
            expected_context_revision=binding.context_revision,
            expected_feedback_revision=binding.feedback_revision,
            settings=BindingTurnSettings("selected", "high", "priority"), task_feedback=feedback,
            message_context_mode=binding.message_context_mode, context_anchor=None,
        )
        self.queue_anchor(scope)
        self.channel.reply_results.append(sent_result("om_progress", chat_id=scope.chat_id))

        await self.app.dispatch_scheduled_run(claim)

        self.assertIsNone(claim.plan.session_settings)
        self.assertEqual(self.codex.turn_calls[0][2], {
            "model": "wire-model", "effort": effort, "service_tier": "priority",
        })
        self.assertIn(("om_trigger", "THINKING"), self.channel.reactions)
        self.assertEqual(len(self.app._progress_cards._sessions), 1)
        self.assertEqual(len(self.channel.replies), 1)
        original_presenters = dict(self.app._progress_cards._sessions)
        next_claim = self.next_claim(claim.plan.id, due=220)
        self.queue_anchor(scope, "om_followup")

        await self.app.dispatch_scheduled_run(next_claim)

        self.assertEqual(self.app._progress_cards._sessions, original_presenters)
        self.assertEqual(len(self.channel.replies), 1)
        self.assertEqual(self.codex.model_calls, 1)
        self.assertIn(("om_followup", "OnIt"), self.channel.reactions)
        self.assertNotIn(("om_followup", "Typing"), self.channel.reactions)

        await self.finish()

        self.assertEqual(self.outcomes[0].task_feedback, feedback)
        self.assertEqual(self.channel.updates[-1][0], "om_progress")
        self.assertIn("检查完成", str(self.channel.updates[-1][1]))
        self.assertNotIn("<at ", str(self.channel.updates[-1][1]))
        self.assertEqual(len(self.channel.send_calls), 2)
        self.assertEqual(len(self.channel.replies), 1)

    async def test_existing_topic_uses_verified_reply_anchor_and_original_scope(self):
        scope, binding = self.binding(kind=ScopeKind.TOPIC)
        claim = self.claim(binding)
        self.queue_anchor(scope)

        await self.app.dispatch_scheduled_run(claim)
        await self.finish()

        self.history.resolve_topic_reply_anchor.assert_awaited_once_with(scope)
        self.assertEqual(len(self.channel.send_calls), 1)
        opts = self.channel.send_calls[0][2]
        self.assertEqual(opts.reply_to, "om_existing_root")
        self.assertTrue(opts.reply_in_thread)
        self.assertEqual(opts.reply_target_gone, "fail")
        self.assertEqual(self.store.active_binding(scope.key).id, binding.id)
        self.assertEqual(self.store.schedules.get_run(claim.run.id).topic_id, scope.topic_id)
        origin = self.outcomes[0].origin
        self.assertEqual(origin.conversation.thread_id, scope.topic_id)
        self.assertEqual(origin.message_id, "om_trigger")
        self.assertTrue(self.channel.reply.await_args.args[2].reply_in_thread)

    async def test_anchor_failure_or_unconfirmed_destination_never_submits_input(self):
        for label, results in (
            ("rejected", [sent_result("om_no", chat_id="oc_group", success=False, code=230001)]),
            ("unknown", [TimeoutError("lost response"), TimeoutError("lost reconciliation")]),
            ("wrong-topic", [sent_result("om_other", chat_id="oc_group", thread_id="omt_wrong")]),
        ):
            with self.subTest(label=label):
                scope, binding = self.binding()
                claim = self.claim(binding)
                self.channel.send_results.extend(results)
                sends_before = len(self.channel.send_calls)

                await self.app.dispatch_scheduled_run(claim)

                run = self.store.schedules.get_run(claim.run.id)
                self.assertEqual(run.barrier, "released")
                self.assertIsNone(run.initial_turn_id)
                self.assertIsNone(run.disposition)
                self.assertEqual(self.codex.resume_calls, [])
                self.assertEqual(self.codex.turn_inputs, [])
                self.assertEqual(self.channel.replies, [])
                sends = self.channel.send_calls[sends_before:]
                self.assertEqual({opts.uuid for _, _, opts in sends}, {claim.run.root_uuid})
                self.assertEqual(len(sends), len(results))

    async def test_changed_binding_before_dispatch_never_redirects_to_current_session(self):
        scope, original = self.binding()
        claim = self.claim(original)
        _, replacement = self.binding()

        await self.app.dispatch_scheduled_run(claim)

        self.assertEqual(self.store.active_binding(scope.key).id, replacement.id)
        self.assertEqual(self.channel.send_calls, [])
        self.assertEqual(self.codex.turn_inputs, [])
        run = self.store.schedules.get_run(claim.run.id)
        self.assertEqual(run.binding_id, original.id)
        self.assertEqual(run.barrier, "released")

    async def test_anchor_publication_aba_rejects_original_admission_in_both_context_modes(self):
        for catch_up in (False, True):
            with self.subTest(catch_up=catch_up):
                scope, original = self.binding(catch_up=catch_up, materialized=False)
                _, other = self.binding(materialized=False)
                async with self.management.scope_coordinator.hold(scope.key):
                    await self.runtime.activate_exact(original.id, context_anchor=original.context_anchor)
                original = self.store.get(original.id)
                claim = self.claim(original)
                self.queue_anchor(scope)
                if catch_up:
                    self.history_window(original.context_anchor, MessageContextAnchor("om_trigger", 3_000))
                send = self.channel.send

                async def send_after_switching_away_and_back(*args, **kwargs):
                    async with self.management.scope_coordinator.hold(scope.key):
                        await self.runtime.activate_exact(other.id)
                        await self.runtime.activate_exact(original.id, context_anchor=original.context_anchor)
                    return await send(*args, **kwargs)

                with patch.object(self.channel, "send", new=send_after_switching_away_and_back):
                    await self.app.dispatch_scheduled_run(claim)

                self.assertEqual(self.store.active_binding(scope.key).id, original.id)
                self.assertEqual(self.store.get(original.id).context_anchor, original.context_anchor)
                self.assertEqual(self.codex.start_kwargs, [])
                self.assertEqual(self.codex.resume_calls, [])
                self.assertEqual(self.codex.turn_inputs, [])
                run = self.store.schedules.get_run(claim.run.id)
                self.assertEqual((run.barrier, run.error_code), ("released", "input_rejected"))
                self.assertIsNone(run.disposition)
                self.assertEqual(self.channel.replies[-1][0], "om_trigger")

    async def test_cancelled_anchor_send_is_unknown_without_native_input_or_future_barrier(self):
        scope, binding = self.binding()
        first = self.claim(binding)
        self.channel.send_results.append(asyncio.CancelledError())

        with self.assertRaises(asyncio.CancelledError):
            await self.app.dispatch_scheduled_run(first)

        run = self.store.schedules.get_run(first.run.id)
        self.assertEqual((run.barrier, run.error_code), ("released", "publishing_unknown"))
        self.assertIsNone(run.origin_message_id)
        self.assertIsNone(run.initial_turn_id)
        self.assertEqual(self.codex.turn_inputs, [])
        self.assertEqual(self.codex.resume_calls, [])
        self.assertEqual(self.channel.replies, [])
        next_claim = self.next_claim(first.plan.id, due=220)
        self.assertNotEqual(next_claim.run.id, first.run.id)
        self.queue_anchor(scope, "om_next")

        await self.app.dispatch_scheduled_run(next_claim)

        self.assertEqual(len(self.codex.turn_inputs), 1)
        self.assertIn(next_claim.run.id, self.codex.turn_inputs[0][1])
        self.assertNotIn(first.run.id, self.codex.turn_inputs[0][1])
        self.assertEqual(self.store.schedules.get_run(next_claim.run.id).disposition, "started")

    async def test_catch_up_consumes_same_scope_history_and_commits_only_accepted_anchors(self):
        scope, binding = self.binding(catch_up=True)
        first = self.claim(binding)
        upper = MessageContextAnchor("om_first", 3_000)
        self.history_window(binding.context_anchor, upper, MessageHistoryRef("om_history", 2_000, "ou_other", "text"))
        self.channel.inbound_messages["om_history"] = FakeMessage(
            "新增的历史事实", message_id="om_history", sender_id="ou_other",
            chat_id=scope.chat_id, chat_type="group", create_time=2_000,
        )
        self.queue_anchor(scope, upper.message_id)

        await self.app.dispatch_scheduled_run(first)

        self.assertEqual(self.history.read_calls, [(scope, binding.context_anchor, upper.message_id)])
        self.assertEqual(self.store.get(binding.id).context_anchor, upper)
        envelope = json.loads(self.codex.turn_inputs[0][1])
        self.assertEqual(envelope["current_message"]["kind"], "scheduled_plan")
        self.assertNotIn("sender", envelope["current_message"])
        self.assertEqual(envelope["supplemental_messages"][0]["text"], "新增的历史事实")
        self.assertEqual(self.channel.fetch_inbound_calls, ["om_history"])
        second = self.next_claim(first.plan.id, due=220)
        later = MessageContextAnchor("om_second", 4_000)
        self.history_window(upper, later)
        self.queue_anchor(scope, later.message_id)

        await self.app.dispatch_scheduled_run(second)

        self.assertEqual(self.history.read_calls[-1], (scope, upper, later.message_id))
        self.assertEqual(self.store.get(binding.id).context_anchor, later)
        self.assertEqual(len(self.codex.turn_inputs), 1)
        self.assertEqual(len(self.codex.handles[0].steers), 1)
        self.assertEqual(self.store.schedules.get_run(second.run.id).disposition, "steered")

    async def test_pointer_switch_during_catch_up_rejects_without_advancing_old_cursor(self):
        scope, binding = self.binding(catch_up=True)
        claim = self.claim(binding)
        self.history_window(binding.context_anchor, MessageContextAnchor("om_trigger", 3_000))
        self.queue_anchor(scope)
        entered, resume = asyncio.Event(), asyncio.Event()
        read_window = self.history.read_window

        async def delayed_read(*args):
            entered.set()
            await resume.wait()
            return await read_window(*args)

        self.history.read_window = delayed_read
        task = asyncio.create_task(self.app.dispatch_scheduled_run(claim))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            _, replacement = self.binding()
        finally:
            resume.set()
            await asyncio.wait_for(task, 1)

        self.assertEqual(self.store.active_binding(scope.key).id, replacement.id)
        self.assertEqual(self.store.get(binding.id).context_anchor, binding.context_anchor)
        self.assertEqual(self.codex.turn_inputs, [])
        self.assertEqual(self.store.schedules.get_run(claim.run.id).error_code, "input_rejected")
        self.assertEqual(self.channel.replies[-1][0], "om_trigger")
        self.assertIn("切换", str(self.channel.replies[-1][1]))

    async def test_history_failure_uses_ordinary_visible_error_and_keeps_context_boundary(self):
        scope, binding = self.binding(catch_up=True)
        claim = self.claim(binding)
        self.queue_anchor(scope)
        self.history.read_window = AsyncMock(side_effect=MessageHistoryUnavailable("无法读取历史"))

        await self.app.dispatch_scheduled_run(claim)

        self.assertEqual(self.codex.turn_inputs, [])
        self.assertEqual(self.store.get(binding.id).context_anchor, binding.context_anchor)
        self.assertEqual(self.channel.replies, [("om_trigger", "无法读取历史")])
        run = self.store.schedules.get_run(claim.run.id)
        self.assertEqual((run.barrier, run.error_code), ("released", "input_rejected"))

    async def test_failed_steer_reaction_falls_back_on_trigger_without_new_turn_feedback(self):
        scope, binding = self.binding()
        first = self.claim(binding)
        self.queue_anchor(scope, "om_first")
        await self.app.dispatch_scheduled_run(first)
        second = self.next_claim(first.plan.id, due=220)
        self.queue_anchor(scope, "om_second")
        self.channel.fail_once_reaction_on = "OnIt"

        await self.app.dispatch_scheduled_run(second)

        self.assertEqual(len(self.codex.turn_inputs), 1)
        self.assertEqual(len(self.codex.handles[0].steers), 1)
        self.assertEqual(self.channel.replies, [("om_second", "已接收调整。")])
        self.assertEqual(self.store.schedules.get_run(second.run.id).disposition, "steered")
        self.assertNotIn(("om_second", "Typing"), self.channel.reactions)

    async def test_goal_inputs_follow_exact_physical_turn_without_duplicate_logical_results(self):
        scope, binding = self.binding(materialized=False)
        control = FakeGoalControl(self.codex)
        self.runtime._goal_control = control
        human = FakeMessage(
            "完成检查", message_id="om_goal", sender_id="ou_initiator",
            chat_id=scope.chat_id, chat_type="group",
        )
        goal = await self.runtime.start_goal(
            binding=binding, cwd=self.cwd, objective="完成检查",
            owner_id="ou_initiator", origin=human,
        )
        goal.release_receipt_attempt()
        first = self.claim(binding)
        self.queue_anchor(scope, "om_first")

        await self.app.dispatch_scheduled_run(first)

        handle = control.handles[0]
        self.assertEqual(handle.steers[0][0], "goal-turn-1")
        self.assertEqual(self.store.schedules.get_run(first.run.id).initial_turn_id, "goal-turn-1")
        handle.rollover("physical-2")
        second = self.next_claim(first.plan.id, due=220)
        self.queue_anchor(scope, "om_second")

        await self.app.dispatch_scheduled_run(second)

        run = self.store.schedules.get_run(second.run.id)
        self.assertEqual((run.disposition, run.initial_turn_id, run.barrier), ("steered", "physical-2", "released"))
        self.assertEqual(handle.steers[1][0], "physical-2")
        self.assertEqual(self.codex.turn_inputs, [])
        self.assertEqual(self.outcomes, [])
        self.assertEqual(self.channel.replies, [])
        handle.finish(response="Goal 检查完成")
        self.assertTrue(await self.runtime.wait_idle(timeout=1))
        self.assertEqual(len(self.outcomes), 1)
        self.assertIs(self.outcomes[0].origin, human)
        self.assertEqual(self.outcomes[0].final_physical_turn_id, "physical-2")

    async def test_turn_completion_during_catch_up_never_changes_captured_steer_to_start(self):
        scope, binding = self.binding(catch_up=True)
        initial = self.claim(binding)
        upper = MessageContextAnchor("om_first", 3_000)
        self.history_window(binding.context_anchor, upper)
        self.queue_anchor(scope, upper.message_id)
        await self.app.dispatch_scheduled_run(initial)
        followup = self.next_claim(initial.plan.id, due=220)
        self.history_window(upper, MessageContextAnchor("om_second", 4_000))
        self.queue_anchor(scope, "om_second")
        read_window = self.history.read_window

        async def finish_before_read(*args):
            await self.finish()
            return await read_window(*args)

        self.history.read_window = finish_before_read

        await self.app.dispatch_scheduled_run(followup)

        self.assertEqual(len(self.codex.turn_inputs), 1)
        self.assertEqual(self.codex.handles[0].steers, [])
        self.assertEqual(self.store.get(binding.id).context_anchor, upper)
        run = self.store.schedules.get_run(followup.run.id)
        self.assertEqual((run.barrier, run.error_code), ("released", "input_rejected"))
        self.assertEqual(self.channel.replies[-1][0], "om_second")

    async def test_uncertain_start_releases_plan_but_next_input_obeys_closed_runtime(self):
        scope, binding = self.binding()
        first = self.claim(binding)
        self.queue_anchor(scope, "om_first")
        self.codex.turn_errors_after_start.append(TimeoutError("native response lost"))

        await self.app.dispatch_scheduled_run(first)

        run = self.store.schedules.get_run(first.run.id)
        self.assertEqual((run.barrier, run.error_code), ("released", "input_unknown"))
        self.assertIsNone(run.initial_turn_id)
        self.assertIn("重启", str(self.channel.replies[-1][1]))
        second = self.next_claim(first.plan.id, due=220)
        self.queue_anchor(scope, "om_second")

        await self.app.dispatch_scheduled_run(second)

        self.assertEqual(len(self.codex.turn_inputs), 1)
        next_run = self.store.schedules.get_run(second.run.id)
        self.assertEqual((next_run.barrier, next_run.error_code), ("released", "input_rejected"))
        self.assertEqual(self.channel.replies[-1][0], "om_second")

    async def test_uncertain_steer_is_not_replayed_and_next_occurrence_uses_ordinary_runtime(self):
        scope, binding = self.binding()
        first = self.claim(binding)
        self.queue_anchor(scope, "om_first")
        await self.app.dispatch_scheduled_run(first)
        handle = self.codex.handles[0]
        second = self.next_claim(first.plan.id, due=220)
        self.queue_anchor(scope, "om_second")
        handle.steer_error = TimeoutError("steer acknowledgement lost")

        await self.app.dispatch_scheduled_run(second)

        run = self.store.schedules.get_run(second.run.id)
        self.assertEqual((run.barrier, run.error_code), ("released", "input_unknown"))
        self.assertIsNone(run.disposition)
        self.assertNotIn(("om_second", "OnIt"), self.channel.reactions)
        third = self.next_claim(first.plan.id, due=280)
        self.queue_anchor(scope, "om_third")
        handle.steer_error = None

        await self.app.dispatch_scheduled_run(third)

        self.assertEqual(len(handle.steers), 1)
        self.assertIn(third.run.id, handle.steers[0])
        self.assertNotIn(second.run.id, handle.steers[0])
        self.assertEqual(self.store.schedules.get_run(third.run.id).disposition, "steered")
        self.assertEqual(len(self.codex.turn_inputs), 1)
