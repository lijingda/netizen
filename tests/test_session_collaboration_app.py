from __future__ import annotations

import asyncio
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from openai_codex import ExternalMessage, InvalidRequestError
from mcp.types import CallToolRequestParams

from netizen_cli.admin.port_config import ConfigFileSnapshot
from netizen_cli.sessions.policy import SessionAccessPolicy, SessionAccessRule, SessionPolicyService
from netizen_cli.sessions.service import SessionService
from netizen_cli.sessions.mcp import SessionMcpRunner
from netizen_cli.bindings import BindingTaskFeedback, BindingTurnSettings, ProjectDisabled
from netizen_cli.channel_app import _outcome_completion_mention_user_id
from netizen_cli.domain import (
    FeishuScope, MentionContextMode, MessageContextAnchor, ScopeKind, SessionAgentOrigin,
)
from netizen_cli.runtime.contracts import (
    GoalStateUnknown, NativeThreadMetadata, ScheduledInitialStartConflict, Submission, SubmitDisposition,
    TurnObservationUnavailable, ThreadArchived,
)
from netizen_cli.sdk_gap_adapter import GoalControlError, GoalMutationStateUnknown
from netizen_cli.sessions.errors import SessionToolError
from netizen_cli.progress_card_settings import ProgressCardSettings
from tests.support.channel_fixtures import channel_fixture
from tests.support.channel_results import sent_result


class SessionCollaborationApplicationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fixture = await self.enterAsyncContext(channel_fixture())
        self.app, self.store = fixture.app, fixture.store
        self.channel, self.runtime = fixture.channel, fixture.runtime
        self.scope = FeishuScope("cli_test", "oc_source", ScopeKind.TOPIC, "omt_source")
        self.channel.chat_types.update(oc_source="group", oc_target="group")
        self.source = self.store.create_channel_binding(
            scope=self.scope, project_alias="test", creator_id="ou_source",
            turn_settings=BindingTurnSettings("future-model", "low", "priority-v2"),
            task_feedback=BindingTaskFeedback(True, True, True, ProgressCardSettings(
                organization="time", commentary_limit=8, show_operations=False,
            )),
        )
        self.store.assign_native_thread_id(self.source.id, "native-source")
        self.source = self.store.get(self.source.id)
        self.runtime.thread_summary_values["native-source"] = NativeThreadMetadata(
            "native-source", "来源任务", "不应展示的摘要",
        )
        self.runtime.native_thread_model = AsyncMock(return_value="gpt-future-codex")
        self.runtime.rename_exact = AsyncMock()
        self.owned = False

        @asynccontextmanager
        async def track(project_alias):
            self.assertEqual(project_alias, "test")
            self.owned = True
            try:
                yield
            finally:
                self.owned = False

        def require_open(project_alias):
            self.assertTrue(self.owned)
            self.assertEqual(project_alias, "test")

        self.runtime.track_topic_creation = track
        self.runtime.require_topic_creation_open = require_open
        self.receipts = []
        self.submissions = []
        self.disposition = SubmitDisposition.STARTED

        async def submit(**kwargs):
            self.submissions.append(kwargs)
            binding = kwargs["binding"]
            if binding.native_thread_id is None:
                self.store.assign_native_thread_id(binding.id, "native-created")
                binding = self.store.get(binding.id)
            return Submission(
                self.disposition, binding.id, binding.native_thread_id, "turn-external",
                (lambda: self.receipts.append(binding.id)) if self.disposition is SubmitDisposition.STARTED else None,
                task_feedback=binding.task_feedback,
            )

        self.runtime.submit_external = AsyncMock(side_effect=submit)

        async def receipt(submission, origin):
            submission.release_receipt_attempt()

        self.app._present_ordinary_receipt = AsyncMock(side_effect=receipt)
        policy_path = fixture.project_root / "session-policy.yaml"
        policy_path.write_text("{}\n")
        self.policy = SessionPolicyService(config_snapshot=ConfigFileSnapshot.read(policy_path))
        self.service = SessionService(
            bindings=self.store, runtime=self.runtime, codex=None, app_id="cli_test",
            policy=self.policy, create_thread=self.app.create_session_thread,
            send_message=self.app.send_session_message,
        )

    def queue_topic(self, *, promote=False):
        if promote:
            self.channel.send_results.extend((
                sent_result("om_root", chat_id="oc_source"),
                sent_result("om_seed", chat_id="oc_source", thread_id="omt_created",
                            root_id="om_root", parent_id="om_root"),
            ))
        else:
            self.channel.send_results.append(sent_result(
                "om_root", chat_id="oc_source", thread_id="omt_created", root_id="om_root",
            ))

    def target(self, *, topic=False):
        scope = FeishuScope("cli_test", "oc_target", ScopeKind.TOPIC if topic else ScopeKind.GROUP,
                            "omt_target" if topic else None)
        target = self.store.create_channel_binding(scope=scope, project_alias="test", creator_id="ou_target")
        self.store.assign_native_thread_id(target.id, "native-target")
        return self.store.get(target.id), scope

    def queue_target_message(self, *, topic=False):
        self.channel.send_results.append(sent_result(
            "om_delivery", chat_id="oc_target", thread_id="omt_target" if topic else None,
            root_id="om_topic_root" if topic else None, parent_id="om_reply" if topic else None,
        ))
        if topic:
            self.app._message_history.resolve_topic_reply_anchor = AsyncMock(
                return_value=SimpleNamespace(message_id="om_reply"),
            )

    async def test_create_ordinary_sibling_inherits_configuration_and_external_authority(self):
        self.queue_topic()
        result = await self.app.create_session_thread("native-source", "请核对方案", title="方案核对")
        binding = self.store.get(result["netizen"]["bindingId"])
        self.assertEqual(self.store.active_binding(self.scope.key).id, self.source.id)
        self.assertEqual(binding.project_alias, self.source.project_alias)
        self.assertEqual(binding.turn_settings, self.source.turn_settings)
        self.assertEqual(binding.task_feedback, BindingTaskFeedback(
            False, True, False, self.source.task_feedback.progress_card_settings,
        ))
        self.assertEqual(result["threadId"], "native-created")
        self.assertTrue(result["netizen"]["accepted"])
        self.assertEqual(result["netizen"]["topicId"], "omt_created")
        self.assertIsNone(self.channel.send_calls[0][2].reply_to)
        self.assertEqual(len(self.channel.send_calls), 1)
        self.assertEqual(self.receipts, [binding.id])
        submission, = self.submissions
        self.assertIsInstance(submission["message"], ExternalMessage)
        self.assertEqual(submission["message"].namespace, "codex_tui")
        self.assertEqual(submission["message"].tool_name, "create_thread")
        self.assertEqual(submission["message"].content,
                         "<codex_delegation>\n  <source_thread_id>native-source</source_thread_id>\n"
                         "  <input>请核对方案</input>\n</codex_delegation>")
        self.assertIsInstance(submission["origin"], SessionAgentOrigin)
        self.assertEqual(submission["origin"].source_thread_id, "native-source")
        self.assertEqual(submission["origin"].target_binding_id, binding.id)
        self.assertEqual(submission["origin"].message_id, "om_root")
        self.assertNotIn("sender", submission["message"].content)
        self.assertNotIn("feishu_current_message", submission["message"].content)
        self.assertNotIn("model", submission)
        self.assertEqual(submission["title"], "方案核对")
        self.runtime.rename_exact.assert_not_awaited()
        self.assertFalse(self.owned)

    async def test_create_promotes_root_once_and_rebuilds_context_anchor(self):
        self.store.set_configuration(
            binding_id=self.source.id, expected_settings_revision=1, expected_context_revision=1,
            expected_feedback_revision=1, settings=self.source.turn_settings,
            task_feedback=self.source.task_feedback, message_context_mode=MentionContextMode.CATCH_UP,
            context_anchor=MessageContextAnchor("om_old", 1000),
        )
        self.queue_topic(promote=True)
        result = await self.app.create_session_thread("native-source", "任务")
        binding = self.store.get(result["netizen"]["bindingId"])
        self.assertEqual(binding.context_anchor.message_id, "om_seed")
        self.assertEqual(len(self.channel.send_calls), 2)
        self.assertNotEqual(self.channel.send_calls[0][2].uuid, self.channel.send_calls[1][2].uuid)
        self.assertEqual(self.channel.send_calls[1][2].reply_to, "om_root")
        self.assertEqual(self.submissions[0]["origin"].message_id, "om_seed")

    async def test_create_does_not_replace_binding_created_during_topic_publication(self):
        self.queue_topic()
        original_send = self.channel.send
        occupied = None

        async def racing_send(*args):
            nonlocal occupied
            response = await original_send(*args)
            occupied = self.store.create_channel_binding(
                scope=FeishuScope("cli_test", "oc_source", ScopeKind.TOPIC, "omt_created"),
                project_alias="test", creator_id="ou_real_user",
            )
            return response

        self.channel.send = racing_send
        with self.assertRaises(SessionToolError):
            await self.app.create_session_thread("native-source", "任务")
        self.assertEqual(self.store.active_binding(occupied.scope_key).id, occupied.id)
        self.assertEqual(self.store.active_binding(self.scope.key).id, self.source.id)
        self.runtime.submit_external.assert_not_awaited()

    async def test_creation_permission_is_checked_once_at_tool_admission(self):
        disabled = SessionAccessPolicy(read_send=SessionAccessRule("disabled"))
        self.policy.update(disabled, expected_revision=self.policy.revision)
        denied = await self.service.call("create_thread", {"prompt": "任务"}, "native-source")
        self.assertEqual(denied["error"]["code"], "access_denied")
        self.assertEqual(self.channel.send_calls, [])
        self.runtime.submit_external.assert_not_awaited()

        self.policy.update(SessionAccessPolicy(), expected_revision=self.policy.revision)
        self.queue_topic()
        original_send = self.channel.send

        async def revoke_during_publication(*args):
            response = await original_send(*args)
            self.policy.update(disabled, expected_revision=self.policy.revision)
            return response

        self.channel.send = revoke_during_publication
        accepted = await self.service.call("create_thread", {"prompt": "任务"}, "native-source")
        self.assertTrue(accepted["netizen"]["accepted"])
        denied = await self.service.call("create_thread", {"prompt": "下一次任务"}, "native-source")
        self.assertEqual(denied["error"]["code"], "access_denied")
        self.assertEqual(len(self.channel.send_calls), 1)
        self.runtime.submit_external.assert_awaited_once()

    async def test_create_failure_reports_materialized_identity_and_unknown_acceptance(self):
        self.queue_topic()

        async def unknown(**kwargs):
            self.store.assign_native_thread_id(kwargs["binding"].id, "native-unknown")
            raise RuntimeError("lost response")

        self.runtime.submit_external.side_effect = unknown
        with self.assertRaises(SessionToolError) as failed:
            await self.app.create_session_thread("native-source", "任务")
        self.assertEqual(failed.exception.code, "submission_unknown")
        self.assertEqual(failed.exception.details["threadId"], "native-unknown")
        self.assertIsNone(failed.exception.details["netizen"]["accepted"])
        self.assertEqual(failed.exception.details["netizen"]["topicId"], "omt_created")
        self.runtime.submit_external.assert_awaited_once()
        self.assertEqual(self.channel.updates[-1][0], "om_root")
        self.assertIn("投递结果未确认", str(self.channel.updates[-1][1]))
        self.assertNotIn("该协作请求未执行", str(self.channel.updates[-1][1]))

    async def test_create_title_is_submitted_without_waiting_for_manual_rename(self):
        self.queue_topic()
        self.runtime.rename_exact.side_effect = RuntimeError("rename failed")
        result = await self.app.create_session_thread("native-source", "任务", title="名字")
        self.assertTrue(result["netizen"]["accepted"])
        self.assertEqual(self.submissions[0]["title"], "名字")
        self.runtime.rename_exact.assert_not_awaited()
        self.assertEqual(len(self.receipts), 1)

    async def test_accepted_create_receipt_survives_tool_deadline_during_display(self):
        self.queue_topic()
        entered = asyncio.Event()
        async with asyncio.timeout(None) as deadline:
            async def blocked_display(submission, origin):
                try:
                    entered.set()
                    deadline.reschedule(asyncio.get_running_loop().time())
                    await asyncio.Event().wait()
                finally:
                    submission.release_receipt_attempt()

            self.app._present_ordinary_receipt.side_effect = blocked_display
            runner = SessionMcpRunner()
            runner._admission = True
            runner._callback = self.service.call
            result = await runner._call_tool(
                SimpleNamespace(meta={"threadId": "native-source"}),
                CallToolRequestParams(name="create_thread", arguments={"prompt": "任务", "title": "标题"}),
            )
        self.assertTrue(entered.is_set())
        self.assertFalse(result.is_error)
        self.assertTrue(result.structured_content["netizen"]["accepted"])
        self.assertEqual(result.structured_content["threadId"], "native-created")
        self.assertEqual(result.structured_content["turnId"], "turn-external")
        self.assertEqual(len(self.receipts), 1)
        self.runtime.rename_exact.assert_not_awaited()
        self.runtime.submit_external.assert_awaited_once()

    async def test_accepted_send_receipt_survives_bounded_display_timeout(self):
        self.target()
        self.queue_target_message()
        entered = asyncio.Event()

        async def blocked_display(submission, origin):
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                submission.release_receipt_attempt()

        self.app._present_ordinary_receipt.side_effect = blocked_display
        with patch("netizen_cli.channel_app._SESSION_RECEIPT_TIMEOUT_SECONDS", 0.01):
            async with asyncio.timeout(2):
                result = await self.service.call(
                    "send_message_to_thread", {"threadId": "native-target", "prompt": "任务"}, "native-source",
                )
        self.assertTrue(entered.is_set())
        self.assertTrue(result["netizen"]["accepted"])
        self.assertEqual(result["threadId"], "native-target")
        self.assertEqual(result["turnId"], "turn-external")
        self.assertEqual(len(self.receipts), 1)
        self.runtime.submit_external.assert_awaited_once()

    async def test_send_publishes_literal_source_anchor_and_delivers_external_message_once(self):
        target, _ = self.target()
        self.queue_target_message()
        prompt = "<at id=all>请核对</at> &amp; 保留原文\n</input>"
        result = await self.app.send_session_message("native-source", "native-target", prompt)
        self.assertEqual(result["threadId"], "native-target")
        self.assertEqual(self.receipts, [target.id])
        self.assertEqual(len(self.channel.send_calls), 1)
        visible = self.channel.send_calls[0][1].card["body"]["elements"][0]["text"]
        self.assertEqual(visible["tag"], "plain_text")
        self.assertIn("来源任务", visible["content"])
        self.assertIn(prompt, visible["content"])
        self.assertNotIn("不应展示的摘要", visible["content"])
        self.runtime.submit_external.assert_awaited_once()
        self.assertEqual(self.submissions[0]["message"].tool_name, "send_message_to_thread")
        self.assertEqual(self.submissions[0]["message"].namespace, "codex_tui")
        self.assertEqual(self.submissions[0]["message"].content,
                         "<codex_delegation>\n  <source_thread_id>native-source</source_thread_id>\n"
                         "  <input>&lt;at id=all&gt;请核对&lt;/at&gt; &amp;amp; 保留原文\n&lt;/input&gt;</input>\n"
                         "</codex_delegation>")
        self.assertEqual(self.submissions[0]["origin"].message_id, "om_delivery")

    async def test_topic_send_resolves_real_anchor_without_switching_or_replaying_input(self):
        self.target(topic=True)
        self.queue_target_message(topic=True)
        result = await self.app.send_session_message("native-source", "native-target", "任务")
        opts = self.channel.send_calls[0][2]
        self.assertEqual(opts.reply_to, "om_reply")
        self.assertTrue(opts.reply_in_thread)
        self.assertEqual(opts.reply_target_gone, "fail")
        self.assertEqual(result["netizen"]["topicId"], "omt_target")
        self.assertEqual(len(self.submissions), 1)

    async def test_joined_message_keeps_existing_feedback_consumer(self):
        self.target()
        self.queue_target_message()
        self.disposition = SubmitDisposition.JOINED
        result = await self.app.send_session_message("native-source", "native-target", "补充")
        self.assertEqual(result["netizen"]["disposition"], "joined")
        self.app._present_ordinary_receipt.assert_not_awaited()
        self.assertEqual(self.receipts, [])

    async def test_send_background_target_reports_exact_status_without_activating(self):
        target, scope = self.target()
        active = self.store.create_channel_binding(scope=scope, project_alias="test", creator_id="ou_user")
        with self.assertRaises(SessionToolError) as failed:
            await self.app.send_session_message("native-source", "native-target", "任务")
        self.assertEqual(failed.exception.code, "target_not_current")
        self.assertEqual(failed.exception.details["threadId"], "native-target")
        self.assertFalse(failed.exception.details["netizen"]["isCurrent"])
        self.assertFalse(failed.exception.details["netizen"]["accepted"])
        self.assertEqual(self.store.active_binding(scope.key).id, active.id)
        self.assertFalse(self.store.get(target.id).active)
        self.assertEqual(self.channel.send_calls, [])
        self.runtime.submit_external.assert_not_awaited()

    async def test_archived_target_uses_exact_native_rejection_without_catalog_precheck(self):
        self.target()
        self.queue_target_message()
        self.runtime.thread_is_archived = AsyncMock(side_effect=AssertionError("must not scan archive catalog"))
        self.runtime.submit_external.side_effect = ThreadArchived("目标已归档；未自动恢复归档。")
        with self.assertRaises(SessionToolError) as failed:
            await self.app.send_session_message("native-source", "native-target", "任务")
        self.assertEqual(failed.exception.code, "target_archived")
        self.assertFalse(failed.exception.details["netizen"]["accepted"])
        self.runtime.thread_is_archived.assert_not_awaited()
        self.runtime.submit_external.assert_awaited_once()
        self.assertEqual(len(self.channel.send_calls), 1)
        self.assertIn("该协作请求未执行", str(self.channel.updates[-1][1]))

    async def test_unavailable_native_handle_is_not_guessed_to_be_archived(self):
        self.target()
        self.queue_target_message()
        self.runtime.thread_is_archived = AsyncMock(side_effect=AssertionError("must not scan archive catalog"))
        self.runtime.submit_external.side_effect = InvalidRequestError(-32600, "thread not found: native-target")
        with self.assertRaises(SessionToolError) as failed:
            await self.app.send_session_message("native-source", "native-target", "任务")
        self.assertEqual(failed.exception.code, "input_rejected")
        self.assertFalse(failed.exception.details["netizen"]["accepted"])
        self.runtime.thread_is_archived.assert_not_awaited()
        self.runtime.submit_external.assert_awaited_once()

    async def test_target_switch_during_publication_rejects_instead_of_retargeting(self):
        target, scope = self.target()
        self.queue_target_message()
        original_send = self.channel.send
        active = None

        async def racing_send(*args):
            nonlocal active
            sent = await original_send(*args)
            active = self.store.create_channel_binding(scope=scope, project_alias="test", creator_id="ou_other")
            return sent

        self.channel.send = racing_send
        with self.assertRaises(SessionToolError) as failed:
            await self.app.send_session_message("native-source", "native-target", "任务")
        self.assertEqual(failed.exception.code, "target_not_current")
        self.assertEqual(failed.exception.details["netizen"]["messageId"], "om_delivery")
        self.assertEqual(self.store.active_binding(scope.key).id, active.id)
        self.runtime.submit_external.assert_not_awaited()
        self.assertEqual(self.channel.updates[-1][0], "om_delivery")
        self.assertIn("该协作请求未执行", str(self.channel.updates[-1][1]))
        self.assertEqual(len(self.channel.send_calls), 1)

    async def test_unknown_sources_and_wrong_instance_cannot_publish(self):
        with self.assertRaises(SessionToolError):
            await self.app.create_session_thread("external-app-thread", "任务")
        source = self.store.create_channel_binding(
            scope=FeishuScope("another_app", "oc_other", ScopeKind.GROUP),
            project_alias="test", creator_id="ou_other",
        )
        self.store.assign_native_thread_id(source.id, "wrong-app-thread")
        with self.assertRaises(SessionToolError):
            await self.app.create_session_thread("wrong-app-thread", "任务")
        self.assertEqual(self.channel.send_calls, [])

    async def test_feedback_uses_exact_anchor_and_never_mentions_a_synthetic_owner(self):
        self.target(topic=True)
        self.queue_target_message(topic=True)
        await self.app.send_session_message("native-source", "native-target", "任务")
        origin = self.submissions[0]["origin"]
        self.channel.reply = AsyncMock()
        await self.app._reply_to_origin(origin, "结果")
        opts = self.channel.reply.call_args.args[2]
        self.assertEqual(opts.reply_to, "om_delivery")
        self.assertTrue(opts.reply_in_thread)
        self.assertEqual(opts.reply_target_gone, "fail")
        outcome = SimpleNamespace(task_feedback=BindingTaskFeedback(False, False, True), origin=origin)
        self.assertIsNone(_outcome_completion_mention_user_id(outcome))

    async def test_invalid_creation_model_is_rejected_before_any_topic_is_created(self):
        with self.assertRaises(SessionToolError) as failed:
            await self.app.create_session_thread("native-source", "任务", model="missing-model")
        self.assertEqual(failed.exception.code, "invalid_model")
        self.assertEqual(self.channel.send_calls, [])
        self.runtime.native_thread_model.assert_not_awaited()

    async def test_preparation_failure_is_known_not_delivered(self):
        self.runtime.native_thread_model.side_effect = RuntimeError("source metadata unavailable")
        with self.assertRaises(SessionToolError) as failed:
            await self.app.create_session_thread("native-source", "任务")
        self.assertEqual(failed.exception.code, "input_rejected")
        self.assertFalse(failed.exception.details["netizen"]["accepted"])
        self.assertEqual(self.channel.send_calls, [])
        self.runtime.submit_external.assert_not_awaited()

    async def test_native_and_runtime_rejections_preserve_known_non_delivery(self):
        self.target()
        for error in (
            InvalidRequestError(-32600, "external input rejected"),
            ProjectDisabled("test"),
            ScheduledInitialStartConflict("initial pending"),
            TurnObservationUnavailable("observation unavailable"),
            GoalControlError("Goal 状态读取失败，本条消息未执行。"),
            GoalStateUnknown("Goal 状态未确认，本条消息未执行。"),
        ):
            with self.subTest(error=type(error).__name__):
                self.queue_target_message()
                self.runtime.submit_external.side_effect = error
                with self.assertRaises(SessionToolError) as failed:
                    await self.app.send_session_message("native-source", "native-target", "任务")
                self.assertEqual(failed.exception.code, "input_rejected")
                self.assertFalse(failed.exception.details["netizen"]["accepted"])
                self.assertEqual(self.channel.updates[-1][0], "om_delivery")
                self.assertIn("该协作请求未执行", str(self.channel.updates[-1][1]))

    async def test_unknown_goal_mutation_is_not_classified_as_read_only_rejection(self):
        self.target()
        self.queue_target_message()
        self.runtime.submit_external.side_effect = GoalMutationStateUnknown("mutation response lost")
        with self.assertRaises(SessionToolError) as failed:
            await self.app.send_session_message("native-source", "native-target", "任务")
        self.assertEqual(failed.exception.code, "submission_unknown")
        self.assertIsNone(failed.exception.details["netizen"]["accepted"])
        self.assertIn("投递结果未确认", str(self.channel.updates[-1][1]))

    async def test_failure_receipt_survives_tool_deadline_during_display(self):
        target, scope = self.target()
        original_timeout = asyncio.timeout
        for error, code, accepted in (
            (GoalControlError("Goal 状态读取失败，本条消息未执行。"), "input_rejected", False),
            (GoalMutationStateUnknown("mutation response lost"), "submission_unknown", None),
        ):
            with self.subTest(error=type(error).__name__):
                self.queue_target_message()
                self.runtime.submit_external.reset_mock(side_effect=True)
                self.runtime.submit_external.side_effect = error
                tool_deadline = original_timeout(None)

                def timeout(seconds):
                    return tool_deadline if seconds == 140 else original_timeout(seconds)

                async def blocked_update(*args, **kwargs):
                    tool_deadline.reschedule(asyncio.get_running_loop().time())
                    await asyncio.Event().wait()

                self.channel.update_card = AsyncMock(side_effect=blocked_update)
                runner = SessionMcpRunner()
                runner._admission = True
                runner._callback = self.service.call
                with patch("netizen_cli.sessions.mcp.asyncio.timeout", side_effect=timeout):
                    result = await runner._call_tool(
                        SimpleNamespace(meta={"threadId": "native-source"}),
                        CallToolRequestParams(name="send_message_to_thread", arguments={
                            "threadId": "native-target", "prompt": "任务",
                        }),
                    )
                self.channel.update_card.assert_awaited_once()
                self.runtime.submit_external.assert_awaited_once()
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content["error"], {
                    "code": code, "message": str(error),
                })
                self.assertEqual(result.structured_content["threadId"], "native-target")
                self.assertEqual(result.structured_content["netizen"], {
                    "accepted": accepted, "bindingId": target.id, "isCurrent": True,
                    "chatId": scope.chat_id, "topicId": scope.topic_id, "messageId": "om_delivery",
                })
                self.assertEqual(runner._calls, set())

    async def test_target_deleted_after_native_acceptance_keeps_successful_receipt(self):
        target, _ = self.target()
        self.queue_target_message()

        async def delete_during_display(submission, origin):
            submission.release_receipt_attempt()
            self.store.delete_binding(target.id)

        self.app._present_ordinary_receipt.side_effect = delete_during_display
        result = await self.app.send_session_message("native-source", "native-target", "任务")
        self.assertTrue(result["netizen"]["accepted"])
        self.assertEqual(result["threadId"], "native-target")
        self.assertTrue(result["netizen"]["targetDeleted"])
        self.assertFalse(result["netizen"]["isCurrent"])

    async def test_unknown_delivery_marks_anchor_but_display_failure_preserves_tool_error(self):
        self.target()
        self.queue_target_message()
        self.runtime.submit_external.side_effect = RuntimeError("response lost")
        with self.assertRaises(SessionToolError) as failed:
            await self.app.send_session_message("native-source", "native-target", "任务")
        self.assertEqual(failed.exception.code, "submission_unknown")
        self.assertIsNone(failed.exception.details["netizen"]["accepted"])
        self.assertEqual(self.channel.updates[-1][0], "om_delivery")
        self.assertIn("投递结果未确认", str(self.channel.updates[-1][1]))
        self.assertNotIn("该协作请求未执行", str(self.channel.updates[-1][1]))
        self.queue_target_message()
        self.channel.update_card = AsyncMock(side_effect=RuntimeError("display unavailable"))
        with self.assertRaises(SessionToolError) as failed:
            await self.app.send_session_message("native-source", "native-target", "任务")
        self.assertEqual(failed.exception.code, "submission_unknown")
        self.assertIsNone(failed.exception.details["netizen"]["accepted"])
        self.assertEqual(self.runtime.submit_external.await_count, 2)
