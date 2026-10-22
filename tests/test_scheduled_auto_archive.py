from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from lark_channel import OutboundCard

from netizen_cli.bindings import BindingTaskFeedback
from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.partial_answers import PartialAnswer
from netizen_cli.runtime.contracts import ActiveState, ActiveTurnSnapshot, TurnOutcome
from netizen_cli.schedules.models import ScheduleNotFound, ScheduleRule
from netizen_cli.session_settings import SessionSettings
from netizen_cli.worktrees.policy import WorkspacePolicy
from tests.support.channel_fixtures import scheduled_channel_fixture
from tests.support.channel_results import (
    completed_turn_result, file_change_item, sent_result, turn_activity_snapshot,
)


class ScheduledAutoArchiveTest(unittest.IsolatedAsyncioTestCase):
    async def start(self, *, enabled=True, manual=False, progress=False, after_claim=None):
        fixture = await self.enterAsyncContext(scheduled_channel_fixture())
        store, channel = fixture.store, fixture.channel
        settings = SessionSettings(task_feedback=BindingTaskFeedback(
            reaction_pulse_enabled=False, progress_card_enabled=progress,
        ))
        result = store.schedules.create(
            name="daily", instructions="check", project_alias="work", app_id="app",
            chat_id="oc_group", auto_archive=enabled, session_settings=settings,
            workspace=WorkspacePolicy(),
            schedule=ScheduleRule("interval", "UTC", every_minutes=1, anchor=100),
            request_id="create", now=100,
        )
        if manual:
            _, claim = store.schedules.claim_manual(
                result.plan_id, app_id="app", expected_revision=1,
                request_id="manual", now=160,
            )
        else:
            claim = store.schedules.claim_due(result.plan_id, app_id="app", now=160)
        assert claim is not None
        if after_claim is not None:
            after_claim(store.schedules, claim)
        channel.send_results.extend((
            sent_result("om_root", chat_id="oc_group"),
            sent_result("om_seed", chat_id="oc_group", thread_id="omt_fresh",
                        root_id="om_root", parent_id="om_root"),
        ))
        if progress:
            submit = fixture.runtime.submit_initial

            async def with_activity(**kwargs):
                submission = await submit(**kwargs)
                fixture.runtime.turn_activity_values[submission.binding_id] = turn_activity_snapshot(
                    binding_id=submission.binding_id, thread_id=submission.thread_id,
                    turn_id=submission.turn_id,
                )
                return submission

            fixture.runtime.submit_initial = with_activity
            fixture.app._reactions.start = AsyncMock(return_value=True)
            channel.reply_results.append(sent_result(
                "om_progress", chat_id="oc_group", thread_id="omt_fresh",
            ))
        await fixture.app.dispatch_scheduled_run(claim)
        request, = fixture.submissions
        self.assertEqual(request["origin"].auto_archive, enabled)
        # The ordinary Runtime releases the native barrier before presentation.
        store.schedules.release(claim.run.id)
        outcome = TurnOutcome(
            binding_id=request["binding"].id, thread_id="native-" + claim.run.id,
            turn_id="turn-initial", owner_id=request["owner_id"], origin=request["origin"],
            cwd=request["cwd"], result=completed_turn_result(final_response="report"),
            task_feedback=settings.task_feedback,
        )
        return fixture, claim, outcome

    def confirmed(self, fixture):
        fixture.channel.reply_results.append(sent_result(
            "om_final", chat_id="oc_group", thread_id="omt_fresh",
        ))

    async def test_scheduled_and_manual_archive_only_after_confirmed_delivery(self):
        for manual in (False, True):
            with self.subTest(manual=manual):
                fixture, claim, outcome = await self.start(manual=manual)
                entered, finish = asyncio.Event(), asyncio.Event()
                original = fixture.channel.reply
                self.confirmed(fixture)

                async def delayed_reply(*args, **kwargs):
                    entered.set()
                    await finish.wait()
                    return await original(*args, **kwargs)

                fixture.channel.reply = delayed_reply
                task = asyncio.create_task(fixture.app.handle_completion(outcome))
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    self.assertEqual(fixture.runtime.archive_binding_calls, [])
                finally:
                    finish.set()
                    await asyncio.wait_for(task, 1)
                self.assertEqual(fixture.runtime.archive_binding_calls, [outcome.binding_id])
                binding = fixture.store.get(outcome.binding_id)
                self.assertIsNone(fixture.store.active_binding(binding.scope_key))
                self.assertEqual(binding.native_thread_id, outcome.thread_id)
                run = fixture.store.schedules.get_run(claim.run.id)
                self.assertEqual((run.barrier, run.delivery_state, run.error_code), ("released", "sent", None))
                self.assertEqual(fixture.channel.replies, [("om_seed", "report")])
                self.assertEqual(fixture.channel.updates, [])

    async def test_claim_policy_survives_edit_pause_and_delete(self):
        actions = (
            lambda store, claim: store.update(claim.plan.id, expected_revision=1,
                request_id="edit", changes={"auto_archive": False, "enabled": False}, now=161),
            lambda store, claim: store.delete(claim.plan.id, expected_revision=1,
                request_id="delete", now=161),
        )
        for action in actions:
            with self.subTest(action=action):
                fixture, _, outcome = await self.start(after_claim=action)
                self.confirmed(fixture)
                await fixture.app.handle_completion(outcome)
                self.assertEqual(fixture.runtime.archive_binding_calls, [outcome.binding_id])

    async def test_disabled_snapshot_does_not_follow_later_enablement(self):
        fixture, _, outcome = await self.start(enabled=False, after_claim=lambda store, claim:
            store.update(claim.plan.id, expected_revision=1, request_id="edit",
                         changes={"auto_archive": True}, now=161))
        self.confirmed(fixture)
        await fixture.app.handle_completion(outcome)
        self.assertEqual(fixture.runtime.archive_binding_calls, [])

    async def test_subsequent_input_and_changed_current_do_not_cancel_exact_archive(self):
        fixture, _, outcome = await self.start()
        entered, finish = asyncio.Event(), asyncio.Event()
        original = fixture.channel.reply
        self.confirmed(fixture)

        async def delayed_reply(*args, **kwargs):
            entered.set()
            await finish.wait()
            return await original(*args, **kwargs)

        fixture.channel.reply = delayed_reply
        task = asyncio.create_task(fixture.app.handle_completion(outcome))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            fixture.runtime.active[outcome.binding_id] = ActiveTurnSnapshot(
                outcome.binding_id, outcome.thread_id, "later-turn", "human", ActiveState.RUNNING,
            )
            other = fixture.store.create_channel_binding(
                scope=FeishuScope("app", "oc_group", ScopeKind.TOPIC, "omt_fresh"),
                project_alias="work", creator_id="human",
            )
        finally:
            finish.set()
            await asyncio.wait_for(task, 1)
        self.assertEqual(fixture.runtime.archive_binding_calls, [outcome.binding_id])
        self.assertNotIn(outcome.binding_id, fixture.runtime.active)
        self.assertEqual(fixture.store.active_binding(other.scope_key).id, other.id)

    async def test_failed_interrupted_or_error_outcome_is_not_archived(self):
        for status, error in (("failed", None), ("interrupted", None),
                              ("completed", RuntimeError("execution failed"))):
            with self.subTest(status=status, error=error):
                fixture, claim, outcome = await self.start()
                self.confirmed(fixture)
                await fixture.app.handle_completion(replace(outcome, error=error,
                    result=SimpleNamespace(status=status, final_response="report", items=())))
                self.assertEqual(fixture.runtime.archive_binding_calls, [])
                self.assertEqual(fixture.store.schedules.get_run(claim.run.id).delivery_state, "sent")

    async def test_unconfirmed_rejected_or_wrong_topic_delivery_is_not_archived(self):
        for result in (
            SimpleNamespace(success=False),
            sent_result("om_final", chat_id="oc_group", thread_id="omt_fresh", success=False, code=230017),
            sent_result("om_final", chat_id="oc_group", thread_id="omt_other"),
            TimeoutError("reply response lost"),
        ):
            with self.subTest(result=result):
                fixture, claim, outcome = await self.start()
                fixture.channel.reply_results.append(result)
                if isinstance(result, BaseException):
                    with self.assertRaises(type(result)):
                        await fixture.app.handle_completion(outcome)
                else:
                    await fixture.app.handle_completion(outcome)
                self.assertEqual(fixture.runtime.archive_binding_calls, [])
                self.assertNotEqual(fixture.store.schedules.get_run(claim.run.id).delivery_state, "sent")
                self.assertEqual(len(fixture.channel.replies), 1)

    async def test_missing_partial_receipt_prevents_archive_after_final_send(self):
        fixture, claim, outcome = await self.start()
        fixture.channel.reply_results.append(SimpleNamespace(success=False))
        self.confirmed(fixture)
        outcome = replace(outcome, partial_answers=(PartialAnswer(
            thread_id=outcome.thread_id, turn_id=outcome.turn_id, item_id="partial", text="step",
        ),))
        await fixture.app.handle_completion(outcome)
        self.assertEqual(len(fixture.channel.replies), 2)
        self.assertEqual(fixture.store.schedules.get_run(claim.run.id).delivery_state, "unknown")
        self.assertEqual(fixture.runtime.archive_binding_calls, [])

    async def test_files_card_is_delivered_unchanged_before_archive(self):
        fixture, _, outcome = await self.start()
        (fixture.project_root / "report.txt").write_text("report")
        self.confirmed(fixture)
        await fixture.app.handle_completion(replace(outcome,
            result=completed_turn_result(file_change_item("report.txt"), final_response="report")))
        self.assertEqual(fixture.runtime.archive_binding_calls, [outcome.binding_id])
        self.assertEqual(len(fixture.channel.replies), 1)
        self.assertIsInstance(fixture.channel.replies[0][1], OutboundCard)
        self.assertEqual(fixture.channel.updates, [])
        self.assertTrue((fixture.project_root / "report.txt").is_file())

    async def test_progress_card_requires_confirmed_final_destination(self):
        for topic in ("omt_fresh", "omt_other"):
            with self.subTest(topic=topic):
                fixture, claim, outcome = await self.start(progress=True)
                fixture.channel.card_update_results.append(SimpleNamespace(success=True, raw={"code": 0}))
                fixture.channel.fetched_messages["om_progress"] = {"code": 0, "data": {"items": [
                    {"message_id": "om_progress", "chat_id": "oc_group", "thread_id": topic}]}}
                await fixture.app.handle_completion(outcome)
                self.assertEqual(fixture.runtime.archive_binding_calls,
                                 [outcome.binding_id] if topic == "omt_fresh" else [])
                self.assertEqual(len(fixture.channel.replies), 1)
                self.assertEqual(len(fixture.channel.updates), 1)
                self.assertEqual(fixture.store.schedules.get_run(claim.run.id).delivery_state,
                                 "sent" if topic == "omt_fresh" else "unknown")

    async def test_archive_error_preserves_success_and_next_claim_without_extra_reply(self):
        fixture, claim, outcome = await self.start()
        self.confirmed(fixture)
        fixture.runtime.archive_binding_error = RuntimeError("archive response unknown")
        with patch.object(fixture.runtime, "archive_exact", wraps=fixture.runtime.archive_exact) as archive:
            with self.assertLogs("netizen_cli.channel_app", "WARNING"):
                await fixture.app.handle_completion(outcome)
            archive.assert_awaited_once_with(outcome.binding_id)
        run = fixture.store.schedules.get_run(claim.run.id)
        self.assertEqual((run.delivery_state, run.barrier, run.error_code), ("sent", "released", None))
        self.assertIsNotNone(fixture.store.schedules.claim_due(claim.plan.id, app_id="app", now=220))
        self.assertEqual(fixture.channel.replies, [("om_seed", "report")])

    async def test_cancellation_during_delivery_does_not_archive(self):
        fixture, claim, outcome = await self.start()
        fixture.channel.reply_results.append(asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await fixture.app.handle_completion(outcome)
        self.assertEqual(fixture.runtime.archive_binding_calls, [])
        self.assertEqual(fixture.store.schedules.get_run(claim.run.id).delivery_state, "unknown")
        self.assertEqual(fixture.app._scheduled_completions, {})

    async def test_run_pruning_at_confirmed_delivery_does_not_lose_archive_policy(self):
        fixture, claim, outcome = await self.start()
        for index in range(100):
            later = fixture.store.schedules.claim_due(claim.plan.id, app_id="app", now=220 + index * 60)
            self.assertIsNotNone(later)
            fixture.store.schedules.release(later.run.id, error_code="no_start")
        self.assertIsNone(fixture.store.schedules.get_run(claim.run.id).delivery_state)
        self.confirmed(fixture)
        await fixture.app.handle_completion(outcome)
        with self.assertRaises(ScheduleNotFound):
            fixture.store.schedules.get_run(claim.run.id)
        self.assertEqual(fixture.runtime.archive_binding_calls, [outcome.binding_id])
