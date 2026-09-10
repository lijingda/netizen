from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from lark_channel import FeishuChannel, OutboundCard, OutboundFile, OutboundImage, OutboundPost, OutboundSender, OutboundConfig, RetryConfig

from netizen.bindings import BindingStore, BindingTaskFeedback, BindingTurnSettings
from netizen.cards import decode_turn_file_action
from netizen.channel import reply_presenter
from netizen.channel_app import ChannelApplication
from netizen.domain import FeishuScope, ScopeKind, ScheduledOrigin, MentionContextMode, MessageContextAnchor
from netizen.management import InstanceManagementService, ScopeCoordinator
from netizen.projects import ProjectRegistry
from netizen.runtime.contracts import ActiveState, ActiveTurnSnapshot, Submission, SubmitDisposition, TurnOutcome
from netizen.management.service import ManagementRuntimePort
from netizen.schedules.models import ScheduleRule
from netizen.session_settings import SessionSettings
from netizen.turn_plan_observer import TurnPlanStepSnapshot, TurnPlanStepState

from test_channel_app import (
    PNG, FakeChannel, FakeMessage, FakeMessageHistory, StubRuntime, _card_button_values, _elements,
    completed_turn_result, file_change_item, image_generation_item, sent_result,
    turn_activity_snapshot,
)
from test_schedule_cards import callback, form_values, manager_form


class ScheduledChannelTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = BindingStore(wall_clock=lambda: 160.0)
        self.projects = ProjectRegistry(store=self.store, project_root=Path(self.tmp.name), projects={"work": Path(self.tmp.name)})
        self.runtime = StubRuntime()
        self.runtime.binding_store = self.store
        self.channel = FakeChannel()
        self.history = FakeMessageHistory()
        self.management = InstanceManagementService(bindings=self.store, projects=self.projects,
            runtime=ManagementRuntimePort(self.runtime), scope_coordinator=ScopeCoordinator())
        self.app = ChannelApplication(app_id="app", channel=self.channel, runtime=self.runtime, bindings=self.store,
                                      projects=self.projects, management=self.management, message_history=self.history)
        self.submissions = []
        self.receipts = []

        async def submit_initial(**kwargs):
            self.submissions.append(kwargs)
            binding = kwargs["binding"]
            self.store.assign_native_thread_id(binding.id, "native-" + kwargs["run_id"])
            self.store.schedules.set_run(kwargs["run_id"], phase="handed_off", initial_turn_id="turn-initial")
            return Submission(SubmitDisposition.STARTED, binding.id, "native-" + kwargs["run_id"], "turn-initial", lambda: self.receipts.append(kwargs["run_id"]), task_feedback=binding.task_feedback)

        self.runtime.submit_initial = submit_initial

    async def asyncTearDown(self):
        await self.app.close()
        await self.management.close()
        self.store.close()
        self.tmp.cleanup()

    def claim(self, instructions="检查明确资源", *, session_settings=SessionSettings()):
        result = self.store.schedules.create(name="日报", instructions=instructions, project_alias="work", app_id="app", chat_id="oc_group",
            schedule=ScheduleRule("interval", "UTC", every_minutes=1, anchor=100), request_id="create-plan", now=100, session_settings=session_settings)
        claimed = self.store.schedules.claim_due(result.plan_id, app_id="app", now=160)
        self.assertIsNotNone(claimed)
        return claimed

    def queue_topic(self, *, promote=False):
        if promote:
            self.channel.send_results.extend([
                sent_result("om_root", chat_id="oc_group"),
                sent_result("om_seed", chat_id="oc_group", thread_id="omt_fresh", root_id="om_root", parent_id="om_root"),
            ])
        else:
            self.channel.send_results.append(sent_result("om_root", chat_id="oc_group", thread_id="omt_fresh", root_id="om_root"))

    async def test_fresh_topic_has_ordinary_defaults_real_origin_and_keeps_group_selection(self):
        claim = self.claim("$reports 检查资源")
        group = FeishuScope("app", "oc_group", ScopeKind.GROUP)
        previous = self.store.create_channel_binding(scope=group, project_alias="work", creator_id="ou_user")
        self.queue_topic()
        await self.app.dispatch_scheduled_run(claim)
        self.assertEqual(self.store.active_binding(group.key).id, previous.id)
        run = self.store.schedules.get_run(claim.run.id)
        binding = self.store.get(run.binding_id)
        self.assertEqual(binding.message_context_mode.value, "current-only")
        self.assertIsNone(binding.turn_settings)
        self.assertFalse(binding.task_feedback.progress_card_enabled)
        self.assertFalse(binding.task_feedback.reaction_pulse_enabled)
        submission = self.submissions[0]
        self.assertIsInstance(submission["origin"], ScheduledOrigin)
        self.assertEqual(submission["origin"].message_id, "om_root")
        self.assertEqual(submission["skill_names"], ("reports",))
        self.assertTrue(submission["input"].startswith(claim.plan.instructions))
        metadata = json.loads(submission["input"].split("<scheduled_plan>\n")[1].split("\n</scheduled_plan>")[0])
        self.assertEqual(metadata["kind"], "scheduled_plan")
        self.assertNotIn("sender_id", metadata)
        self.assertEqual(self.receipts, [run.id])
        self.assertIsNone(self.channel.send_calls[0][2].reply_to)

    async def test_promotion_uses_distinct_stable_uuid_and_seed_completion_origin(self):
        claim = self.claim()
        self.queue_topic(promote=True)
        await self.app.dispatch_scheduled_run(claim)
        root, seed = self.channel.send_calls
        self.assertEqual(root[2].uuid, claim.run.root_uuid)
        self.assertEqual(seed[2].uuid, claim.run.seed_uuid)
        self.assertNotEqual(root[2].uuid, seed[2].uuid)
        self.assertEqual(seed[2].reply_to, "om_root")
        self.assertTrue(seed[2].reply_in_thread)
        self.assertEqual(seed[2].reply_target_gone, "fail")
        self.assertEqual(self.submissions[0]["origin"].message_id, "om_seed")

    async def assert_catch_up_snapshot(self, *, promote):
        settings = SessionSettings(turn_settings=BindingTurnSettings("chosen-model", "high", "default"),
            message_context_mode=MentionContextMode.CATCH_UP)
        claim = self.claim(session_settings=settings)
        self.store.schedules.update(claim.plan.id, expected_revision=1, request_id="changed-after-claim",
            changes={"session_settings": SessionSettings()}, now=161)
        message_id = "om_seed" if promote else "om_root"
        anchor = MessageContextAnchor(message_id, 12345)
        self.history.anchors[message_id] = anchor
        self.queue_topic(promote=promote)
        await self.app.dispatch_scheduled_run(claim)
        binding = self.submissions[0]["binding"]
        self.assertEqual(SessionSettings.from_binding(binding), settings)
        self.assertEqual(binding.context_anchor, anchor)
        self.assertEqual(self.history.resolve_calls, [(FeishuScope("app", "oc_group", ScopeKind.TOPIC, "omt_fresh"), message_id)])
        self.assertEqual(self.history.read_calls, [])
        self.assertNotIn("context_commit", self.submissions[0])

    async def test_claimed_settings_and_new_group_topic_seed_anchor_are_frozen(self):
        await self.assert_catch_up_snapshot(promote=True)

    async def test_topic_group_uses_its_exact_new_root_as_context_boundary(self):
        await self.assert_catch_up_snapshot(promote=False)

    async def test_missing_context_evidence_does_not_start_native_task(self):
        claim = self.claim(session_settings=SessionSettings(message_context_mode=MentionContextMode.CATCH_UP))
        self.queue_topic(promote=True)
        self.app._message_history = None
        await self.app.dispatch_scheduled_run(claim)
        self.assertFalse(self.submissions)
        run = self.store.schedules.get_run(claim.run.id)
        self.assertEqual(run.barrier, "released")
        self.assertIsNone(run.binding_id)

    async def test_private_target_never_enables_catch_up_even_with_saved_group_settings(self):
        claim = self.claim(session_settings=SessionSettings(message_context_mode=MentionContextMode.CATCH_UP))
        self.channel.chat_types["oc_group"] = "p2p"
        await self.app.dispatch_scheduled_run(claim)
        self.assertFalse(self.channel.send_calls)
        self.assertFalse(self.submissions)

    async def progress_fixture(self):
        settings = SessionSettings(task_feedback=BindingTaskFeedback(True, True))
        claim = self.claim(session_settings=settings)
        self.queue_topic(promote=True)
        original_submit = self.runtime.submit_initial

        async def submit(**kwargs):
            submission = await original_submit(**kwargs)
            self.runtime.turn_activity_values[submission.binding_id] = turn_activity_snapshot(
                binding_id=submission.binding_id, thread_id=submission.thread_id, turn_id=submission.turn_id)
            return submission

        self.runtime.submit_initial = submit
        self.app._reactions.start = AsyncMock(return_value=True)
        self.channel.reply_results.append(sent_result("om_progress", chat_id="oc_group", thread_id="omt_fresh"))
        await self.app.dispatch_scheduled_run(claim)
        request = self.submissions[0]
        self.app._reactions.start.assert_awaited_once_with("turn-initial", "om_seed", pulse_enabled=True)
        self.assertEqual(self.receipts, [claim.run.id])
        self.assertEqual(len(self.app._progress_cards._sessions), 1)
        return claim, request

    async def assert_progress_completion(self, *, topic):
        claim, request = await self.progress_fixture()
        self.channel.card_update_results.append(SimpleNamespace(success=True, raw={"code": 0}))
        self.channel.fetched_messages["om_progress"] = {"code": 0, "data": {"items": [
            {"message_id": "om_progress", "chat_id": "oc_group", "thread_id": topic}]}}
        self.store.schedules.release(claim.run.id)
        await self.app.handle_completion(TurnOutcome(binding_id=request["binding"].id,
            thread_id="native-" + claim.run.id, turn_id="turn-initial", owner_id=request["owner_id"], origin=request["origin"],
            result=completed_turn_result(final_response="scheduled card result"), task_feedback=BindingTaskFeedback(True, True)))
        self.assertEqual(len(self.channel.replies), 1)
        self.assertEqual(self.channel.updates[-1][0], "om_progress")
        self.assertIn("scheduled card result", str(self.channel.updates[-1][1]))
        run = self.store.schedules.get_run(claim.run.id)
        self.assertEqual(run.delivery_state, "sent" if topic == "omt_fresh" else "unknown")
        self.assertEqual(run.barrier, "released")

    async def test_progress_card_records_confirmed_delivery_without_extra_reply(self):
        await self.assert_progress_completion(topic="omt_fresh")

    async def test_progress_card_destination_unknown_does_not_duplicate_result(self):
        await self.assert_progress_completion(topic="omt_other")

    async def assert_uncertain_progress_update(self, response, *, state="unknown"):
        claim, request = await self.progress_fixture()
        self.store.schedules.release(claim.run.id)
        self.channel.card_update_results.append(response)
        await self.app.handle_completion(TurnOutcome(
            binding_id=request["binding"].id, thread_id="native-" + claim.run.id,
            turn_id="turn-initial", owner_id=request["owner_id"], origin=request["origin"],
            result=completed_turn_result(final_response="only one terminal result"),
            task_feedback=BindingTaskFeedback(True, True)))
        self.assertEqual(len(self.channel.replies), 1)  # initial progress card only
        self.assertEqual(len(self.channel.updates), 1)
        run = self.store.schedules.get_run(claim.run.id)
        self.assertEqual(run.delivery_state, state)
        self.assertEqual(run.barrier, "released")

    async def test_terminal_update_response_lost_does_not_send_another_result(self):
        await self.assert_uncertain_progress_update(TimeoutError("response lost after patch"))

    async def test_terminal_update_false_without_receipt_stays_unknown(self):
        await self.assert_uncertain_progress_update(SimpleNamespace(success=False))

    async def test_sdk_missing_patch_response_is_not_delivery_confirmation(self):
        peer = SimpleNamespace(_patch_card=AsyncMock(return_value=None))
        result = await FeishuChannel.update_card(peer, "om_progress", {})
        self.assertTrue(result.success)  # SDK permits a missing raw receipt.
        await self.assert_uncertain_progress_update(result)

    async def test_terminal_update_explicit_rejection_does_not_resend_result(self):
        await self.assert_uncertain_progress_update(
            SimpleNamespace(success=False, raw={"code": 230017}), state="failed")

    async def test_terminal_render_failure_before_update_can_fall_back(self):
        claim, request = await self.progress_fixture()
        self.store.schedules.release(claim.run.id)
        self.channel.reply_results.append(sent_result("om_final", chat_id="oc_group", thread_id="omt_fresh"))
        with patch("netizen.channel_app.turn_progress_card", side_effect=ValueError("cannot render")):
            await self.app.handle_completion(TurnOutcome(
                binding_id=request["binding"].id, thread_id="native-" + claim.run.id,
                turn_id="turn-initial", owner_id=request["owner_id"], origin=request["origin"],
                result=completed_turn_result(final_response="render fallback"),
                task_feedback=BindingTaskFeedback(True, True)))
        self.assertEqual(len(self.channel.replies), 2)
        self.assertEqual(self.channel.replies[-1][1], "render fallback")
        self.assertEqual(self.channel.updates, [])
        self.assertEqual(self.store.schedules.get_run(claim.run.id).delivery_state, "sent")

    async def test_old_run_survives_busy_retention_until_final_card_delivery_is_confirmed(self):
        claim, request = await self.progress_fixture()
        self.channel.card_update_results.append(SimpleNamespace(success=True, raw={"code": 0}))
        for occurrence in range(1, 102):
            self.assertIsNone(self.store.schedules.claim_due(
                claim.plan.id, app_id="app", now=160 + 60 * occurrence))
        self.store.schedules.release(claim.run.id)
        self.channel.fetched_messages["om_progress"] = {"code": 0, "data": {"items": [
            {"message_id": "om_progress", "chat_id": "oc_group", "thread_id": "omt_fresh"}]}}
        entered = asyncio.Event()
        proceed = asyncio.Event()
        original_fetch = self.channel.fetch_message

        async def blocked_confirmation(message_id):
            self.assertEqual(message_id, "om_progress")
            entered.set()
            await proceed.wait()
            return await original_fetch(message_id)

        self.channel.fetch_message = blocked_confirmation
        completion = asyncio.create_task(self.app.handle_completion(TurnOutcome(
            binding_id=request["binding"].id, thread_id="native-" + claim.run.id,
            turn_id="turn-initial", owner_id=request["owner_id"], origin=request["origin"],
            result=completed_turn_result(final_response="retained result"),
            task_feedback=BindingTaskFeedback(True, True))))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            pending_receipt = self.store.schedules.get_run(claim.run.id)
            self.assertEqual(pending_receipt.barrier, "released")
            self.assertIsNone(pending_receipt.delivery_state)
            retained = self.store.schedules.list_runs(claim.plan.id, limit=1001)
            self.assertEqual(sum(run.error_code == "skipped_busy" for run in retained), 100)
            self.assertIn(claim.run.id, {run.id for run in retained})
        finally:
            proceed.set()
            await asyncio.wait_for(completion, timeout=1)
        retained = self.store.schedules.list_runs(claim.plan.id, limit=1001)
        self.assertEqual(len(retained), 100)
        self.assertNotIn(claim.run.id, {run.id for run in retained})
        self.assertEqual(len(self.channel.replies), 1)
        self.assertEqual(self.channel.updates[-1][0], "om_progress")
        self.assertIn("retained result", str(self.channel.updates[-1][1]))
        self.assertEqual(self.store.get(request["binding"].id).native_thread_id, "native-" + claim.run.id)

    async def test_publication_reconciles_only_same_uuid_and_never_starts_when_unknown(self):
        claim = self.claim()
        self.channel.send_results.extend([RuntimeError("lost"), RuntimeError("lost")])
        await self.app.dispatch_scheduled_run(claim)
        self.assertEqual(len(self.channel.send_calls), 2)
        self.assertIs(self.channel.send_calls[0][2], self.channel.send_calls[1][2])
        self.assertFalse(self.submissions)
        run = self.store.schedules.get_run(claim.run.id)
        self.assertEqual((run.barrier, run.error_code), ("released", "publishing_unknown"))

    async def test_replay_of_handed_off_claim_never_publishes_or_releases_it(self):
        claim = self.claim()
        self.queue_topic()
        await self.app.dispatch_scheduled_run(claim)
        await self.app.dispatch_scheduled_run(claim)
        self.assertEqual(len(self.channel.send_calls), 1)
        self.assertEqual(len(self.submissions), 1)
        run = self.store.schedules.get_run(claim.run.id)
        self.assertEqual((run.phase, run.barrier), ("handed_off", "held"))

    async def test_definite_reconciliation_error_does_not_erase_first_unknown_send(self):
        claim = self.claim()
        self.channel.send_results.extend([
            RuntimeError("first response lost"),
            sent_result("", chat_id="oc_group", success=False, code=230071),
        ])
        await self.app.dispatch_scheduled_run(claim)
        self.assertEqual(self.store.schedules.get_run(claim.run.id).error_code, "publishing_unknown")
        self.assertFalse(self.submissions)

    async def test_claimed_deleted_plan_still_dispatches_snapshot(self):
        claim = self.claim()
        self.store.schedules.delete(claim.plan.id, expected_revision=1, request_id="delete-plan", now=161)
        self.queue_topic()
        await self.app.dispatch_scheduled_run(claim)
        self.assertEqual(len(self.submissions), 1)
        self.assertTrue(self.submissions[0]["input"].startswith("检查明确资源"))

    async def test_user_created_topic_binding_is_never_overwritten(self):
        claim = self.claim()
        target = FeishuScope("app", "oc_group", ScopeKind.TOPIC, "omt_fresh")
        user_binding = self.store.create_channel_binding(scope=target, project_alias="work", creator_id="ou_user")
        self.queue_topic()
        await self.app.dispatch_scheduled_run(claim)
        self.assertFalse(self.submissions)
        self.assertEqual(self.store.active_binding(target.key).id, user_binding.id)
        self.assertEqual(self.store.schedules.get_run(claim.run.id).error_code, "scope_conflict")

    async def test_known_root_blocks_input_until_initial_handoff(self):
        claim = self.claim()
        seed_started, finish_seed = asyncio.Event(), asyncio.Event()
        async def send(_to, _content, opts):
            if opts.reply_to is None:
                return sent_result("om_root", chat_id="oc_group")
            seed_started.set()
            await finish_seed.wait()
            return sent_result("om_seed", chat_id="oc_group", thread_id="omt_fresh", root_id="om_root", parent_id="om_root")
        self.channel.send = send
        dispatch = asyncio.create_task(self.app.dispatch_scheduled_run(claim))
        await asyncio.wait_for(seed_started.wait(), timeout=2)
        try:
            await self.app.handle_message(FakeMessage("调整计划", message_id="om_input", chat_id="oc_group", thread_id="omt_fresh", chat_type="group", raw={"root_id": "om_root"}))
            self.assertIn("正在启动", str(self.channel.replies[-1][1]))
            self.assertFalse(self.runtime.submit_calls)
        finally:
            finish_seed.set()
            await dispatch

    async def test_cancelled_send_is_publication_unknown(self):
        claim = self.claim()
        begun = asyncio.Event()
        async def send(*_args):
            begun.set()
            await asyncio.Event().wait()
        self.channel.send = send
        task = asyncio.create_task(self.app.dispatch_scheduled_run(claim))
        await asyncio.wait_for(begun.wait(), timeout=2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.store.schedules.get_run(claim.run.id).error_code, "publishing_unknown")
        self.assertFalse(self.submissions)

    async def test_unknown_target_is_rejected_before_sending(self):
        claim = self.claim()
        self.channel.chat_types["oc_group"] = "unknown"
        await self.app.dispatch_scheduled_run(claim)
        self.assertFalse(self.channel.send_calls)
        self.assertFalse(self.submissions)
        self.assertEqual(self.store.schedules.get_run(claim.run.id).error_code, "dispatch_rejected")

    async def test_private_dispatch_and_stop_preserve_direct_conversation(self):
        source_scope = FeishuScope("app", "oc_group", ScopeKind.DIRECT)
        source = self.store.create_channel_binding(scope=source_scope, project_alias="work", creator_id="ou_user")
        self.channel.chat_types["oc_group"] = "p2p"
        claim = self.claim()
        self.queue_topic(promote=True)
        await self.app.dispatch_scheduled_run(claim)
        self.assertEqual(len(self.submissions), 1)
        run = self.store.schedules.get_run(claim.run.id)
        self.assertEqual(run.topic_id, "omt_fresh")
        self.assertEqual(self.store.get(run.binding_id).scope_key, FeishuScope("app", "oc_group", ScopeKind.TOPIC, "omt_fresh").key)
        self.assertEqual(self.submissions[0]["origin"].message_id, "om_seed")
        self.runtime.active[run.binding_id] = ActiveTurnSnapshot(run.binding_id, "native-" + run.id, "turn-initial", "scheduled_plan:" + claim.plan.id, ActiveState.RUNNING)
        await self.app.handle_message(FakeMessage("/stop", message_id="om_stop_private", chat_id="oc_group", thread_id="omt_fresh", chat_type="p2p"))
        self.assertEqual(self.runtime.stop_calls, [run.binding_id])
        self.assertEqual(self.store.active_binding(source_scope.key).id, source.id)

    async def test_topic_group_dispatch_starts_an_independent_topic(self):
        source_scope = FeishuScope("app", "oc_group", ScopeKind.TOPIC, "omt_source")
        source = self.store.create_channel_binding(scope=source_scope, project_alias="work", creator_id="ou_user")
        self.channel.chat_types["oc_group"] = "topic"
        claim = self.claim()
        self.queue_topic()
        await self.app.dispatch_scheduled_run(claim)
        self.assertEqual(len(self.submissions), 1)
        self.assertEqual(len(self.channel.send_calls), 1)
        run = self.store.schedules.get_run(claim.run.id)
        self.assertEqual(run.topic_id, "omt_fresh")
        self.assertEqual(self.store.get(run.binding_id).scope_key, FeishuScope("app", "oc_group", ScopeKind.TOPIC, "omt_fresh").key)
        self.assertEqual(self.store.active_binding(source_scope.key).id, source.id)

    async def test_completion_delivery_records_real_result_without_reopening_barrier(self):
        claim = self.claim()
        self.queue_topic()
        await self.app.dispatch_scheduled_run(claim)
        request = self.submissions[0]
        self.store.schedules.release(claim.run.id)
        self.channel.reply_results.append(SimpleNamespace(success=False))
        await self.app.handle_completion(TurnOutcome(binding_id=request["binding"].id, thread_id="native-" + claim.run.id,
            turn_id="turn-initial", owner_id=request["owner_id"], origin=request["origin"], result=SimpleNamespace(status="completed", final_response="报告", items=())))
        run = self.store.schedules.get_run(claim.run.id)
        self.assertEqual(run.delivery_state, "unknown")
        self.assertEqual(run.barrier, "released")

    async def completion_fixture(self):
        claim = self.claim()
        self.queue_topic(promote=True)
        await self.app.dispatch_scheduled_run(claim)
        self.store.schedules.release(claim.run.id)
        request = self.submissions[0]
        return claim, request

    def use_sdk_reply(self, reply):
        # Exercise the real public reply and sender, including its default
        # target-gone downgrade. Only the HTTP driver is replaced.
        driver = SimpleNamespace(reply_message=AsyncMock(side_effect=reply), create_message=AsyncMock())
        sender = OutboundSender(driver, OutboundConfig(retry=RetryConfig(max_attempts=1)))

        async def send(to, content, opts):
            outbound = OutboundPost(markdown=content) if isinstance(content, str) else content
            return await sender.send(outbound, receive_id=to, receive_id_type=opts.receive_id_type,
                reply_to=opts.reply_to, reply_in_thread=opts.reply_in_thread,
                reply_target_gone=opts.reply_target_gone, uuid_=opts.uuid)

        peer = SimpleNamespace(send=send)

        async def sdk_reply(origin, content, opts=None):
            return await FeishuChannel.reply(peer, origin, content, opts)

        self.channel.reply = sdk_reply
        return driver

    async def finish_fixture(self, claim, request, content="验收成功"):
        await self.app.handle_completion(TurnOutcome(binding_id=request["binding"].id,
            thread_id="native-" + claim.run.id, turn_id="turn-initial",
            owner_id=request["owner_id"], origin=request["origin"],
            result=completed_turn_result(final_response=content)))

    async def finish_files_fixture(self, claim, request):
        (Path(self.tmp.name) / "report.pdf").write_bytes(b"pdf")
        await self.app.handle_completion(TurnOutcome(
            binding_id=request["binding"].id, thread_id="native-" + claim.run.id,
            turn_id="turn-initial", owner_id=request["owner_id"], origin=request["origin"],
            result=completed_turn_result(file_change_item("report.pdf"), final_response="report complete")))

    async def test_file_card_response_lost_does_not_publish_fallback_text(self):
        claim, request = await self.completion_fixture()
        self.channel.reply_results.append(TimeoutError("response lost after send"))
        await self.finish_files_fixture(claim, request)
        self.assertEqual(len(self.channel.replies), 1)
        self.assertIsInstance(self.channel.replies[0][1], OutboundCard)
        run = self.store.schedules.get_run(claim.run.id)
        self.assertEqual(run.delivery_state, "unknown")
        self.assertEqual(run.barrier, "released")

    async def test_sdk_file_card_timeout_is_unknown_without_fallback(self):
        claim, request = await self.completion_fixture()
        driver = self.use_sdk_reply(TimeoutError("response lost after send"))
        await self.finish_files_fixture(claim, request)
        driver.reply_message.assert_awaited_once()
        driver.create_message.assert_not_awaited()
        self.assertEqual(self.store.schedules.get_run(claim.run.id).delivery_state, "unknown")

    async def test_file_card_preparation_failure_can_fall_back_once(self):
        claim, request = await self.completion_fixture()
        self.channel.reply_results.append(sent_result("om_final", chat_id="oc_group", thread_id="omt_fresh"))
        with patch("netizen.channel_app.turn_files_card", side_effect=ValueError("cannot render")):
            await self.finish_files_fixture(claim, request)
        self.assertEqual([content for _, content in self.channel.replies], ["report complete"])
        self.assertEqual(self.store.schedules.get_run(claim.run.id).delivery_state, "sent")

    async def test_sdk_partial_text_then_timeout_is_unknown_without_resending(self):
        claim, request = await self.completion_fixture()
        calls = 0

        async def reply(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return {"code": 0, "data": {"message_id": "om_first", "chat_id": "oc_group", "thread_id": "omt_fresh"}}
            raise TimeoutError("second chunk response lost")

        driver = self.use_sdk_reply(reply)
        await self.finish_fixture(claim, request, "验收结果\n" * 2000)
        self.assertEqual(driver.reply_message.await_count, 2)
        driver.create_message.assert_not_awaited()
        self.assertEqual(self.store.schedules.get_run(claim.run.id).delivery_state, "unknown")

    async def test_sdk_target_gone_never_moves_scheduled_completion_to_main_chat(self):
        claim, request = await self.completion_fixture()

        async def reject(**kwargs):
            return {"code": 230017, "msg": "Bot is NOT the owner of the resource."}

        driver = self.use_sdk_reply(reject)
        await self.finish_fixture(claim, request)
        driver.create_message.assert_not_awaited()
        driver.reply_message.assert_awaited_once()
        sent = driver.reply_message.call_args.kwargs
        self.assertEqual(sent["message_id"], "om_seed")
        self.assertTrue(sent["reply_in_thread"])
        self.assertEqual(self.store.schedules.get_run(claim.run.id).delivery_state, "failed")

    async def test_sdk_every_completion_chunk_stays_in_execution_topic(self):
        claim, request = await self.completion_fixture()
        calls = 0

        async def reply(**kwargs):
            nonlocal calls
            calls += 1
            data = {"message_id": "om_reply_" + str(calls), "chat_id": "oc_group",
                "thread_id": "omt_fresh", "root_id": "om_root", "parent_id": "om_seed"}
            self.channel.fetched_messages[data["message_id"]] = {"code": 0, "data": {"items": [data]}}
            return {"code": 0, "data": data}

        driver = self.use_sdk_reply(reply)
        await self.finish_fixture(claim, request, "验收结果\n" * 2000)
        self.assertGreaterEqual(driver.reply_message.await_count, 2)
        for call in driver.reply_message.call_args_list:
            self.assertEqual(call.kwargs["message_id"], "om_seed")
            self.assertTrue(call.kwargs["reply_in_thread"])
        driver.create_message.assert_not_awaited()
        self.assertEqual(self.store.schedules.get_run(claim.run.id).delivery_state, "sent")

    async def test_correct_last_chunk_does_not_hide_a_wrong_first_chunk(self):
        claim, request = await self.completion_fixture()
        calls = 0

        async def reply(**kwargs):
            nonlocal calls
            calls += 1
            data = {"message_id": "om_chunk_" + str(calls), "chat_id": "oc_group",
                "thread_id": None if calls == 1 else "omt_fresh"}
            self.channel.fetched_messages[data["message_id"]] = {"code": 0, "data": {"items": [data]}}
            return {"code": 0, "data": data}

        driver = self.use_sdk_reply(reply)
        with self.assertLogs("netizen.channel_app", level="WARNING"):
            await self.finish_fixture(claim, request, "验收结果\n" * 2000)
        self.assertGreaterEqual(driver.reply_message.await_count, 2)
        driver.create_message.assert_not_awaited()
        self.assertEqual(self.store.schedules.get_run(claim.run.id).delivery_state, "unknown")

    async def test_success_without_exact_destination_is_not_marked_delivered_or_retried(self):
        claim, request = await self.completion_fixture()
        for chat, topic in (("oc_group", None), ("oc_group", "omt_other"), ("oc_other", "omt_fresh")):
            async def reply(**kwargs):
                return {"code": 0, "data": {"message_id": "om_wrong", "chat_id": chat, "thread_id": topic}}

            driver = self.use_sdk_reply(reply)
            with self.assertLogs("netizen.channel_app", level="WARNING"):
                await self.finish_fixture(claim, request)
            self.assertEqual(self.store.schedules.get_run(claim.run.id).delivery_state, "unknown")
            driver.reply_message.assert_awaited_once()
            driver.create_message.assert_not_awaited()

    async def test_scheduled_audit_failure_notice_uses_same_topic_without_fresh_fallback(self):
        claim, request = await self.completion_fixture()
        calls = 0

        async def reply(**kwargs):
            nonlocal calls
            calls += 1
            return {"code": 230028 if calls == 1 else 230017, "msg": "rejected"}

        driver = self.use_sdk_reply(reply)
        with self.assertLogs("netizen.channel_app", level="WARNING"):
            await self.finish_fixture(claim, request)
        self.assertEqual(driver.reply_message.await_count, 2)
        for call in driver.reply_message.call_args_list:
            self.assertEqual(call.kwargs["message_id"], "om_seed")
            self.assertTrue(call.kwargs["reply_in_thread"])
        driver.create_message.assert_not_awaited()
        self.assertEqual(self.store.schedules.get_run(claim.run.id).delivery_state, "failed")

    async def test_scheduled_result_files_use_exact_new_topic_and_callbacks_preserve_run(self):
        group = FeishuScope("app", "oc_group", ScopeKind.GROUP)
        source = self.store.create_channel_binding(scope=group, project_alias="work", creator_id="ou_user")
        self.store.assign_native_thread_id(source.id, "native-source")
        claim = self.claim()
        self.queue_topic(promote=True)
        await self.app.dispatch_scheduled_run(claim)
        request = self.submissions[0]
        binding = self.store.get(request["binding"].id)
        origin = request["origin"]
        self.assertIsInstance(origin, ScheduledOrigin)
        report = Path(self.tmp.name) / "report.pdf"
        image = Path(self.tmp.name) / "trend.png"
        report.write_bytes(b"pdf")
        image.write_bytes(PNG)
        (Path(self.tmp.name) / "unreferenced-source.txt").write_text("source only", encoding="utf-8")
        activity = turn_activity_snapshot(
            binding_id=binding.id, thread_id=binding.native_thread_id, turn_id="turn-initial",
            steps=(TurnPlanStepSnapshot("initial hidden activity", TurnPlanStepState.COMPLETED),),
        )
        # Runtime, rather than any display action, owns the exact terminal release.
        self.store.schedules.release(claim.run.id)
        self.channel.reply_results.append(sent_result("om_result_card", chat_id="oc_group", thread_id="omt_fresh"))
        await self.app.handle_completion(TurnOutcome(
            binding_id=binding.id, thread_id=binding.native_thread_id, turn_id="turn-initial",
            owner_id=request["owner_id"], origin=origin,
            result=completed_turn_result(file_change_item("report.pdf"), image_generation_item(image), final_response="scheduled report ready"),
            activity=activity,
        ))

        self.assertEqual(self.channel.replies[-1][0], "om_seed")
        self.assertIs(self.channel.reply_targets[-1], origin)
        self.assertEqual(origin.conversation.thread_id, "omt_fresh")
        card = self.channel.replies[-1][1]
        self.assertIsInstance(card, OutboundCard)
        self.assertIn("scheduled report ready", str(card.card))
        self.assertNotIn("unreferenced-source.txt", str(card.card))
        self.assertNotIn("initial hidden activity", str(card.card))
        self.assertFalse(_elements(card.card, "collapsible_panel"))
        values = _card_button_values(card, "发送")
        self.assertEqual(len(values), 2)
        paths = set()
        for value in values:
            intent = decode_turn_file_action(app_id="app", message_id="om_result_card", callback_chat_id="oc_group",
                sender_id="ou_user", tag="button", value=value)
            self.assertEqual(intent.scope.key, binding.scope_key)
            self.assertNotEqual(intent.scope.key, group.key)
            self.assertEqual((intent.binding_id, intent.turn_id), (binding.id, "turn-initial"))
            paths.add(Path(intent.path))
        self.assertEqual(paths, {report.resolve(), image.resolve()})
        run_before = self.store.schedules.get_run(claim.run.id)
        self.assertEqual((run_before.delivery_state, run_before.barrier), ("sent", "released"))
        changes_before = self.store._connection.total_changes
        self.channel.send_results.extend([
            sent_result("om_file", chat_id="oc_group", thread_id="omt_fresh", root_id="om_root", parent_id="om_result_card"),
            sent_result("om_image", chat_id="oc_group", thread_id="omt_fresh", root_id="om_root", parent_id="om_result_card"),
        ])
        for value in values:
            await self.app.handle_card_action(SimpleNamespace(message_id="om_result_card", chat_id="oc_group",
                operator=SimpleNamespace(open_id="ou_other"), action=SimpleNamespace(tag="button", value=value, form_value=None)))
        deliveries = self.channel.send_calls[-2:]
        self.assertEqual({type(content) for _, content, _ in deliveries}, {OutboundFile, OutboundImage})
        for chat_id, content, opts in deliveries:
            self.assertEqual(chat_id, "oc_group")
            self.assertIn(Path(content.source.path), paths)
            self.assertEqual(opts.reply_to, "om_result_card")
            self.assertTrue(opts.reply_in_thread)
            self.assertEqual(opts.reply_target_gone, "fail")
        self.assertEqual(self.store.schedules.get_run(claim.run.id), run_before)
        self.assertEqual(self.store._connection.total_changes, changes_before)
        self.assertEqual(self.store.active_binding(group.key).id, source.id)
        self.assertEqual(self.channel.updates, [])

    async def test_scheduled_topic_followup_activity_and_files_stay_ordinary_and_exact(self):
        group = FeishuScope("app", "oc_group", ScopeKind.GROUP)
        source = self.store.create_channel_binding(scope=group, project_alias="work", creator_id="ou_user")
        self.store.assign_native_thread_id(source.id, "native-source")
        claim = self.claim()
        self.queue_topic()
        await self.app.dispatch_scheduled_run(claim)
        request = self.submissions[0]
        binding = self.store.get(request["binding"].id)
        self.store.schedules.release(claim.run.id)
        self.channel.reply_results.append(sent_result("om_initial_result", chat_id="oc_group", thread_id="omt_fresh"))
        await self.app.handle_completion(TurnOutcome(
            binding_id=binding.id, thread_id=binding.native_thread_id, turn_id="turn-initial",
            owner_id=request["owner_id"], origin=request["origin"], result=completed_turn_result(final_response="initial done"),
        ))
        run_before = self.store.schedules.get_run(claim.run.id)
        feedback = BindingTaskFeedback(progress_card_enabled=True)
        self.store.set_configuration(binding_id=binding.id, expected_settings_revision=binding.settings_revision,
            expected_context_revision=binding.context_revision, expected_feedback_revision=binding.feedback_revision,
            settings=binding.turn_settings, task_feedback=feedback, message_context_mode=binding.message_context_mode, context_anchor=None)
        self.app._progress_cards = reply_presenter._ReplyCardPresenter(self.channel, self.runtime, poll_seconds=0.01)
        source_activity = turn_activity_snapshot(binding_id=source.id, thread_id="native-source", turn_id="turn-followup",
            steps=(TurnPlanStepSnapshot("source scope activity", TurnPlanStepState.IN_PROGRESS),))
        initial = turn_activity_snapshot(binding_id=binding.id, thread_id=binding.native_thread_id, turn_id="turn-followup")
        self.runtime.turn_activity_values.update({source.id: source_activity, binding.id: initial})
        self.channel.reply_results.extend([
            sent_result("om_source_progress", chat_id="oc_group"),
            sent_result("om_followup_progress", chat_id="oc_group", thread_id="omt_fresh"),
        ])
        self.assertTrue(await self.app._progress_cards.start(binding_id=source.id, thread_id="native-source", turn_id="turn-followup",
            origin=FakeMessage("source work", message_id="om_source", chat_id="oc_group", chat_type="group")))
        self.runtime.submission = Submission(SubmitDisposition.STARTED, binding.id, binding.native_thread_id, "turn-followup", lambda: None, task_feedback=feedback)
        prompt = FakeMessage("生成后续报告", message_id="om_followup", chat_id="oc_group", thread_id="omt_fresh", chat_type="group")
        await self.app.handle_message(prompt)
        self.assertEqual(self.runtime.submit_calls[-1]["binding"].id, binding.id)
        self.assertIs(self.channel.reply_targets[-1], prompt)
        self.assertEqual(prompt.conversation.thread_id, "omt_fresh")
        updated = turn_activity_snapshot(binding_id=binding.id, thread_id=binding.native_thread_id, turn_id="turn-followup", revision=2,
            steps=(TurnPlanStepSnapshot("generate scheduled topic files", TurnPlanStepState.COMPLETED),))
        self.runtime.turn_activity_values[binding.id] = updated
        async with asyncio.timeout(1):
            while not self.channel.updates:
                await asyncio.sleep(0.01)
        self.assertEqual(self.channel.updates[-1][0], "om_followup_progress")
        self.assertIn("generate scheduled topic files", str(self.channel.updates[-1][1]))
        self.assertNotIn("source scope activity", str(self.channel.updates[-1][1]))
        self.assertEqual(self.store.schedules.get_run(claim.run.id), run_before)
        paths = tuple(f"followup-{index:02}.txt" for index in range(10))
        for path in paths:
            (Path(self.tmp.name) / path).write_text(path, encoding="utf-8")
        await self.app.handle_completion(TurnOutcome(binding_id=binding.id, thread_id=binding.native_thread_id, turn_id="turn-followup",
            owner_id="ou_user", origin=prompt, result=completed_turn_result(file_change_item(*paths), final_response="followup files ready"),
            task_feedback=feedback, activity=updated))
        terminal = self.channel.updates[-1][1]
        self.assertFalse(_elements(terminal, "collapsible_panel")[0]["expanded"])
        self.assertIn("followup files ready", str(terminal))
        page = next(behavior["value"] for button in _elements(terminal, "button") for behavior in button.get("behaviors", ())
            if behavior["value"]["intent"] == "turn-file.page")
        intent = decode_turn_file_action(app_id="app", message_id="om_followup_progress", callback_chat_id="oc_group", sender_id="ou_user",
            tag="button", value=page, form_value={"turn_file_page": "1"})
        self.assertEqual((intent.scope.key, intent.binding_id, intent.turn_id), (binding.scope_key, binding.id, "turn-followup"))
        self.assertIsNone(intent.reply)
        self.assertIsNotNone(intent.progress)
        changes_before = self.store._connection.total_changes
        await self.app.handle_card_action(SimpleNamespace(message_id="om_followup_progress", chat_id="oc_group", operator=SimpleNamespace(open_id="ou_other"),
            action=SimpleNamespace(tag="button", value=page, form_value={"turn_file_page": "1"})))
        paged = self.channel.updates[-1][1]
        self.assertEqual({message_id for message_id, _ in self.channel.updates}, {"om_followup_progress"})
        self.assertFalse(_elements(paged, "collapsible_panel")[0]["expanded"])
        self.assertIn("generate scheduled topic files", str(paged))
        self.assertIn("followup files ready", str(paged))
        self.assertIn("followup-08.txt", str(paged))
        self.assertNotIn("source scope activity", str(paged))
        self.assertEqual(self.store.schedules.get_run(claim.run.id), run_before)
        self.assertEqual(self.store._connection.total_changes, changes_before)
        self.assertEqual(self.store.active_binding(group.key).id, source.id)
        self.assertEqual(set(self.app._progress_cards._sessions), {(source.id, "native-source", "turn-followup")})

    async def card_action(self, *, value=None, form=None, topic_id=None):
        self.channel.fetched_messages["om_card"] = {"code": 0, "data": {"items": [{"message_id": "om_card", "chat_id": "oc_group", **({"thread_id": topic_id} if topic_id else {})}]}}
        event = SimpleNamespace(message_id="om_card", chat_id="oc_group", operator=SimpleNamespace(open_id="ou_user"),
            action=SimpleNamespace(tag="button", value=value, form_value=form))
        await self.app.handle_card_action(event)
        return SimpleNamespace(card=self.channel.updates[-1][1])

    def manager_plan(self, name, *, chat_id="oc_group", enabled=True):
        created = self.store.schedules.create(name=name, instructions="独立指令：" + name,
            project_alias="work", app_id="app", chat_id=chat_id, enabled=enabled,
            schedule=ScheduleRule("interval", "UTC", every_minutes=60, anchor=100),
            request_id="manager-" + name, now=100)
        return created.plan_id

    async def test_manager_defaults_to_enabled_unfinished_and_refreshes_exact_selection(self):
        active = self.manager_plan("可执行任务")
        paused = self.manager_plan("暂停任务", enabled=False)
        ended = self.store.schedules.create(name="已结束任务", instructions="曾经执行",
            project_alias="work", app_id="app", chat_id="oc_group",
            schedule=ScheduleRule("once", "UTC", at="1970-01-01T00:02+00:00"),
            request_id="ended-fixture", now=100)
        ended_claim = self.store.schedules.claim_due(ended.plan_id, app_id="app", now=160)
        self.assertIsNotNone(ended_claim)
        self.store.schedules.release(ended_claim.run.id, error_code="recovery_no_start")
        await self.app.handle_message(FakeMessage("/cron", message_id="om_open", chat_id="oc_group", chat_type="group"))
        card = self.channel.replies[-1][1]
        self.assertEqual(callback(card, "新建定时任务")["navigation"], {"filter": "current_enabled"})
        self.assertIn("可执行任务", str(card.card))
        for hidden in ("暂停任务", "已结束任务", "刷新任务"):
            self.assertNotIn(hidden, str(card.card))
        selected = await self.card_action(form=manager_form(card, "cron_manage", plan_id=active))
        refresh = callback(selected, "刷新任务")
        self.assertEqual(refresh["payload"], {"plan_id": active})
        self.store.schedules.update(active, expected_revision=1, request_id="external-edit",
            changes={"instructions": "刷新后的指令"}, now=160)
        selected = await self.card_action(value=refresh)
        self.assertIn("刷新后的指令", str(selected.card))
        self.assertEqual(callback(selected, "编辑")["payload"]["expected_revision"], 2)
        all_plans = await self.card_action(form=manager_form(selected, "cron_filter", option="current"))
        for plan_id in (paused, ended.plan_id):
            selected = await self.card_action(form=manager_form(all_plans, "cron_manage", plan_id=plan_id))
            self.assertEqual(callback(selected, "刷新任务")["payload"]["plan_id"], plan_id)

    async def test_default_selection_retains_last_pending_occurrence_without_next_due(self):
        created = self.store.schedules.create(name="最后一次启动中", instructions="独立执行",
            project_alias="work", app_id="app", chat_id="oc_group",
            schedule=ScheduleRule("once", "UTC", at="1970-01-01T00:02+00:00"),
            request_id="pending-selection", now=100)
        claim = self.store.schedules.claim_due(created.plan_id, app_id="app", now=160)
        self.assertIsNotNone(claim)
        self.assertIsNone(self.store.schedules.get(created.plan_id).next_due_at)
        scope = FeishuScope("app", "oc_group", ScopeKind.GROUP)
        card = await self.app._schedule_manager_card(scope, navigation={"plan_id": created.plan_id})
        self.assertEqual(callback(card, "刷新任务")["payload"]["plan_id"], created.plan_id)
        self.assertNotIn("不符合当前筛选", str(card.card))
        self.store.schedules.release(claim.run.id, error_code="recovery_no_start")
        card = await self.app._schedule_manager_card(scope, navigation={"plan_id": created.plan_id})
        self.assertIn("不符合当前筛选", str(card.card))

    async def test_single_form_can_cancel_change_frequency_and_reject_stale_save(self):
        plan_id = self.manager_plan("编辑任务")
        scope = FeishuScope("app", "oc_group", ScopeKind.GROUP)
        detail = await self.app._schedule_manager_card(scope, navigation={"plan_id": plan_id})
        editor = await self.card_action(value=callback(detail, "编辑"))
        values = form_values(editor)
        values.update(cron_kind="weekly", cron_weekdays=["1", "4"], cron_at="18:30")
        cancelled = await self.card_action(value=callback(editor, "取消"))
        self.assertEqual(self.store.schedules.get(plan_id).schedule.kind, "interval")
        self.assertEqual(callback(cancelled, "编辑")["payload"]["plan_id"], plan_id)
        saved = await self.card_action(form=values)
        plan = self.store.schedules.get(plan_id)
        self.assertEqual((plan.schedule.kind, plan.schedule.weekdays, plan.schedule.at), ("weekly", (1, 4), "18:30"))
        editor = await self.card_action(value=callback(saved, "编辑"))
        stale = form_values(editor)
        stale[next(key for key in stale if key.startswith("cron_instructions"))] = "陈旧表单内容"
        self.store.schedules.update(plan_id, expected_revision=plan.revision, request_id="concurrent-edit",
            changes={"instructions": "其他人已保存的内容"}, now=160)
        updates = len(self.channel.updates)
        retry = await self.card_action(form=stale)
        self.assertEqual(len(self.channel.updates), updates + 1)
        self.assertIn("修改", str(retry.card))
        restored = form_values(retry)
        self.assertEqual(restored[next(key for key in restored if key.startswith("cron_instructions"))], "陈旧表单内容")
        self.assertEqual(self.store.schedules.get(plan_id).instructions, "其他人已保存的内容")

    async def test_manager_selects_only_one_plan_and_preserves_filter_through_edit(self):
        first = self.manager_plan("任务甲")
        second = self.manager_plan("任务乙")
        other = self.manager_plan("其他会话任务", chat_id="oc_other")
        await self.app.handle_message(FakeMessage("/cron", message_id="om_open", chat_id="oc_group", chat_type="group"))
        card = self.channel.replies[-1][1]
        self.assertNotIn("独立指令", str(card.card))
        self.assertNotIn("其他会话任务", str(card.card))
        for plan_id in (first, second, first):
            card = await self.card_action(form=manager_form(card, "cron_manage", plan_id=plan_id))
            self.assertEqual(callback(card, "编辑")["payload"]["plan_id"], plan_id)
            self.assertEqual(callback(card, "编辑")["navigation"]["plan_id"], plan_id)
        card = await self.card_action(form=manager_form(card, "cron_filter", option="all"))
        self.assertNotIn("plan_id", callback(card, "新建定时任务")["navigation"])
        self.assertIn("其他会话任务", str(card.card))
        card = await self.card_action(form=manager_form(card, "cron_manage", plan_id=other))
        editor = await self.card_action(value=callback(card, "编辑"))
        values = form_values(editor)
        values[next(key for key in values if key.startswith("cron_instructions"))] = "修改后的完整指令"
        saved = await self.card_action(form=values)
        self.assertEqual(callback(saved, "编辑")["navigation"], {"filter": "all", "plan_id": other})
        plan = self.store.schedules.get(other)
        self.assertEqual((plan.instructions, plan.chat_id), ("修改后的完整指令", "oc_other"))
        self.assertEqual(self.store.schedules.get(first).revision, 1)

    async def test_topic_card_refresh_does_not_depend_on_an_unrelated_chat_lookup(self):
        plan_id = self.manager_plan("话题内任务")
        scope = FeishuScope("app", "oc_group", ScopeKind.TOPIC, "omt_current")
        card = await self.app._schedule_manager_card(scope, navigation={"plan_id": plan_id})
        self.channel.get_chat_info = AsyncMock(side_effect=RuntimeError("chat lookup must not run"))
        refreshed = await self.card_action(value=callback(card, "刷新任务"), topic_id=scope.topic_id)
        self.assertEqual(callback(refreshed, "编辑")["payload"]["plan_id"], plan_id)
        self.channel.get_chat_info.assert_not_awaited()
        # A topic card cannot be reused at a different actual message location.
        updates = len(self.channel.updates)
        await self.card_action(value=callback(refreshed, "刷新任务"), topic_id="omt_other")
        self.assertEqual(len(self.channel.updates), updates)
        self.assertIn("不一致", str(self.channel.replies[-1]))
        self.channel.get_chat_info.assert_not_awaited()

    async def test_manager_pause_and_delete_clear_selection_without_changing_filter(self):
        plan_id = self.manager_plan("唯一任务")
        scope = FeishuScope("app", "oc_group", ScopeKind.GROUP)
        card = await self.app._schedule_manager_card(scope, navigation={"filter": "current_enabled", "plan_id": plan_id})
        paused = await self.card_action(value=callback(card, "暂停"))
        self.assertEqual(callback(paused, "新建定时任务")["navigation"], {"filter": "current_enabled"})
        self.assertFalse(self.store.schedules.get(plan_id).enabled)
        self.assertIn("不符合当前筛选", str(paused.card))
        paused = await self.card_action(form=manager_form(paused, "cron_filter", option="current_paused"))
        paused = await self.card_action(form=manager_form(paused, "cron_manage", plan_id=plan_id))
        deleted = await self.card_action(value=callback(paused, "删除计划"))
        self.assertTrue(self.store.schedules.get(plan_id, include_deleted=True).deleted)
        self.assertEqual(callback(deleted, "新建定时任务")["navigation"], {"filter": "current_paused"})
        self.assertNotIn("唯一任务", str(deleted.card))
        # An old read-only selector refreshes the manager after external deletion.
        deleted = await self.card_action(value=callback(paused, "刷新任务"))
        self.assertIn("已删除", str(deleted.card))

    async def test_manager_pagination_and_filter_change_use_independent_navigation(self):
        for index in range(51):
            self.manager_plan(f"任务{index:02}")
        self.manager_plan("其他会话", chat_id="oc_other")
        scope = FeishuScope("app", "oc_group", ScopeKind.GROUP)
        card = await self.app._schedule_manager_card(scope)
        page_two = await self.card_action(value=callback(card, "下一页任务"))
        self.assertIn("cursor", callback(page_two, "新建定时任务")["navigation"])
        all_plans = await self.card_action(form=manager_form(page_two, "cron_filter", option="all"))
        self.assertEqual(callback(all_plans, "新建定时任务")["navigation"], {"filter": "all"})

    async def test_form_save_survives_new_application_without_server_draft(self):
        from netizen.cards.scheduled import decode_schedule_action, schedule_form_card
        scope = FeishuScope("app", "oc_group", ScopeKind.GROUP)
        editor = schedule_form_card(scope, projects=self.projects.list(enabled_only=True),
            default_timezone="UTC", navigation={"filter": "all"})
        values = form_values(editor)
        values[next(key for key in values if key.startswith("cron_name"))] = "无需服务端草稿"
        values["cron_instructions"] = "完整指令在卡片中"
        values["cron_kind"] = "daily"
        saved_settings = decode_schedule_action(scope=scope, value=None, form=values).payload["session_settings"]
        self.assertEqual(self.store.schedules.list(app_id="app"), ())
        await self.app.close()
        self.app = ChannelApplication(app_id="app", channel=self.channel, runtime=self.runtime,
            bindings=self.store, projects=self.projects, management=self.management, message_history=self.history)
        result = await self.card_action(form=values)
        plans = self.store.schedules.list(app_id="app")
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].instructions, "完整指令在卡片中")
        self.assertEqual(plans[0].session_settings.to_dict(), saved_settings)
        self.assertEqual(callback(result, "编辑")["navigation"]["filter"], "all")
        await self.card_action(form=values)
        self.assertEqual(len(self.store.schedules.list(app_id="app")), 1)

    async def test_cron_without_binding_can_create_directly_pause_and_delete(self):
        scope = FeishuScope("app", "oc_group", ScopeKind.GROUP)
        await self.app.handle_message(FakeMessage("/cron", message_id="om_open", chat_id="oc_group", chat_type="group"))
        listed = self.channel.replies[-1][1]
        self.assertIn("新建定时任务", str(listed.card))
        form_card = await self.card_action(value=callback(listed, "新建定时任务"))
        form = form_values(form_card)
        form[next(key for key in form if key.startswith("cron_name"))] = "日报"
        form["cron_instructions"] = "检查明确资源"
        form["cron_timezone"] = "UTC"
        form["cron_kind"] = "daily"
        self.assertEqual(self.store.schedules.list(app_id="app"), ())
        detail = await self.card_action(form=form)
        plans = self.store.schedules.list(app_id="app")
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].chat_id, scope.chat_id)
        await self.card_action(form=form)
        self.assertEqual(len(self.store.schedules.list(app_id="app")), 1)
        paused = await self.card_action(value=callback(detail, "暂停"))
        self.assertFalse(self.store.schedules.get(plans[0].id).enabled)
        self.assertFalse(self.store.schedules.get(plans[0].id).deleted)
        paused = await self.card_action(form=manager_form(paused, "cron_filter", option="current_paused"))
        paused = await self.card_action(form=manager_form(paused, "cron_manage", plan_id=plans[0].id))
        await self.card_action(value=callback(paused, "删除计划"))
        self.assertTrue(self.store.schedules.get(plans[0].id, include_deleted=True).deleted)
        replay = await self.card_action(form=form)
        self.assertIn("不会重新创建", str(replay.card))
        self.assertEqual(self.store.schedules.list(app_id="app"), ())
        self.assertIsNone(self.store.active_binding(scope.key))

    async def test_private_cron_direct_save_and_list_default_to_current_chat(self):
        self.channel.chat_types["oc_group"] = "p2p"
        scope = FeishuScope("app", "oc_group", ScopeKind.DIRECT)
        await self.app.handle_message(FakeMessage("/cron", message_id="om_open_private", chat_id="oc_group", chat_type="p2p"))
        listed = self.channel.replies[-1][1]
        form_card = await self.card_action(value=callback(listed, "新建定时任务"))
        form = form_values(form_card)
        form[next(key for key in form if key.startswith("cron_name"))] = "私聊计划"
        form["cron_instructions"] = "回复验收成功"
        form["cron_timezone"] = "UTC"
        form["cron_kind"] = "daily"
        await self.card_action(form=form)
        plans = self.store.schedules.list(app_id="app")
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].chat_id, scope.chat_id)
        self.assertEqual(self.app._schedule_default_chat(scope, {"mode": "list"}), {"mode": "list", "chat_id": scope.chat_id})
        self.assertEqual(self.app._schedule_default_chat(scope, {"mode": "list", "all": True}), {"mode": "list", "all": True})
        self.assertIsNone(self.store.active_binding(scope.key))

    def test_invalid_explicit_chat_is_not_replaced_with_current_group(self):
        scope = FeishuScope("app", "oc_group", ScopeKind.GROUP)
        from netizen.schedules.models import ScheduleError
        for invalid in (False, "", 0, []):
            with self.subTest(value=invalid), self.assertRaises(ScheduleError):
                self.app._schedule_default_chat(scope, {"mode": "list", "chat_id": invalid})

    async def test_stale_card_does_not_overwrite_new_plan_revision(self):
        claim = self.claim()
        service = self.management.schedules
        result = await service.manage({"mode": "view", "plan_id": claim.plan.id}, source="card")
        from netizen.cards.scheduled import schedule_manager_card
        detail = schedule_manager_card(FeishuScope("app", "oc_group", ScopeKind.GROUP), {"plans": [result["plan"]]}, selected=result)
        self.store.schedules.update(claim.plan.id, expected_revision=1, request_id="rename", changes={"name": "新名称"}, now=160)
        await self.card_action(value=callback(detail, "暂停"))
        self.assertTrue(self.store.schedules.get(claim.plan.id).enabled)
        self.assertIn("修改", str(self.channel.updates[-1]))

    async def test_rejected_card_patch_shows_refresh_feedback_without_replaying_write(self):
        claim = self.claim()
        result = await self.management.schedules.manage({"mode": "view", "plan_id": claim.plan.id}, source="card")
        from netizen.cards.scheduled import schedule_manager_card
        detail = schedule_manager_card(FeishuScope("app", "oc_group", ScopeKind.GROUP), {"plans": [result["plan"]]}, selected=result)
        self.channel.card_update_results.append(sent_result("om_card", chat_id="oc_group", success=False, code=230099))
        with self.assertLogs("netizen.channel_app", level="ERROR"):
            await self.card_action(value=callback(detail, "暂停"), form={})
        self.assertFalse(self.store.schedules.get(claim.plan.id).enabled)
        self.assertEqual(self.store.schedules.get(claim.plan.id).revision, 2)
        self.assertEqual(len(self.channel.updates), 2)
        self.assertIn("卡片刷新失败", str(self.channel.updates[-1]))

    async def test_invalid_form_redraws_with_inputs_and_validation_error(self):
        from netizen.cards.scheduled import schedule_form_card
        scope = FeishuScope("app", "oc_group", ScopeKind.GROUP)
        card = schedule_form_card(scope, projects=self.projects.list(enabled_only=True), default_timezone="UTC")
        form = form_values(card)
        form[next(key for key in form if key.startswith("cron_name"))] = "保留我的输入"
        form.update(cron_instructions="保留完整任务内容", cron_kind="daily", cron_timezone="invalid/timezone")
        self.channel.fetched_messages["om_card"] = {"code": 0, "data": {"items": [{"message_id": "om_card", "chat_id": "oc_group"}]}}
        await self.app.handle_card_action(SimpleNamespace(message_id="om_card", chat_id="oc_group", operator=SimpleNamespace(open_id="ou_user"),
            action=SimpleNamespace(tag="button", value=None, form_value=form)))
        self.assertEqual(len(self.channel.updates), 1)
        self.assertEqual(self.channel.updates[-1][0], "om_card")
        self.assertIn("时区", str(self.channel.updates[-1][1]))
        self.assertEqual(self.store.schedules.list(app_id="app"), ())
        # Correct the recovered form, whose transport identity was renewed.
        form = form_values(SimpleNamespace(card=self.channel.updates[-1][1]))
        form["cron_timezone"] = "UTC"
        await self.card_action(form=form)
        plans = self.store.schedules.list(app_id="app")
        self.assertEqual([(plan.name, plan.instructions) for plan in plans], [("保留我的输入", "保留完整任务内容")])

    async def test_topic_group_card_direct_save_defaults_to_current_group(self):
        from netizen.cards.scheduled import schedule_form_card
        self.channel.chat_types["oc_group"] = "topic"
        scope = FeishuScope("app", "oc_group", ScopeKind.TOPIC, "omt_source")
        form = form_values(schedule_form_card(scope, projects=self.projects.list(enabled_only=True), default_timezone="UTC"))
        form[next(key for key in form if key.startswith("cron_name"))] = "话题群计划"
        form["cron_instructions"] = "检查指定资源"
        form["cron_kind"] = "daily"
        await self.card_action(form=form, topic_id=scope.topic_id)
        self.assertEqual(len(self.store.schedules.list(app_id="app")), 1)
        self.assertEqual(self.store.schedules.list(app_id="app")[0].chat_id, scope.chat_id)


if __name__ == "__main__":
    unittest.main()
