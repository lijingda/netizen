from __future__ import annotations

import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

from netizen_cli.bindings import BindingTurnSettings
from netizen_cli.cards import goal_generation
from netizen_cli.cards.fork import ForkCardAction, ForkSource
from netizen_cli.domain import ActiveState, CardControlIntent, CardControlName, FeishuScope, GoalStatus, NativeCapability, ScopeKind
from netizen_cli.runtime.contracts import ActiveTurnSnapshot, GoalSubmission, SteerRace, Submission, SubmissionAdmission, SubmitDisposition, TurnOutcome
from netizen_cli.schedules.models import ScheduleRule
from netizen_cli.turn_files import TurnDiffSummary, TurnFileError
from netizen_cli.worktrees.models import WorktreeConflict
from tests.support.channel_fixtures import channel_fixture
from tests.support.channel_messages import FakeMessage
from tests.support.channel_results import completed_turn_result, file_change_item, native_goal, sent_result


class WorktreeChannelCwdTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = await self.enterAsyncContext(channel_fixture())
        self.app, self.store = self.fixture.app, self.fixture.store
        self.runtime, self.channel = self.fixture.runtime, self.fixture.channel
        self.selected = self.fixture.project_root / "selected"
        self.selected.mkdir()
        self.scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        self.binding = self.store.create_channel_binding(
            scope=self.scope, project_alias="test", creator_id="user", working_cwd=str(self.selected),
            turn_settings=BindingTurnSettings("future-model", "low", "priority-v2"),
        )
        self.runtime.submission = Submission(
            SubmitDisposition.STARTED, self.binding.id, "native", "turn",
            release_receipt_attempt=lambda: None,
        )

    def materialize(self, cwd=None):
        self.store.assign_native_thread_id(self.binding.id, "native")
        self.binding = self.store.get(self.binding.id)
        self.runtime.native_cwds["native"] = cwd or self.selected

    def outcome(self, *, cwd=None):
        return TurnOutcome(
            binding_id=self.binding.id, thread_id="native", turn_id="turn",
            owner_id="user", origin=FakeMessage("work", message_id="om_origin"), cwd=cwd,
            result=completed_turn_result(file_change_item("answer.txt")),
        )

    async def test_prompt_passes_selected_lazy_directory(self):
        self.runtime.resolve_binding_cwd = AsyncMock(side_effect=RuntimeError("native read unavailable"))
        await self.app.handle_message(FakeMessage("work", message_id="om_work"))
        self.assertEqual(self.runtime.submit_calls[-1]["cwd"], self.selected)
        self.runtime.resolve_binding_cwd.assert_not_awaited()

    async def test_prompt_leaves_materialized_directory_to_runtime_without_pre_read(self):
        native = self.fixture.project_root / "native-directory"
        native.mkdir()
        self.materialize(native)
        self.runtime.resolve_binding_cwd = AsyncMock(side_effect=RuntimeError("native read unavailable"))
        await self.app.handle_message(FakeMessage("work", message_id="om_work"))
        self.assertIsNone(self.runtime.submit_calls[-1]["cwd"])
        self.runtime.resolve_binding_cwd.assert_not_awaited()

    async def test_steer_retains_exact_admission_without_reading_directory(self):
        self.materialize()
        self.runtime.resolve_binding_cwd = AsyncMock(side_effect=RuntimeError("native read unavailable"))
        self.runtime.active[self.binding.id] = ActiveTurnSnapshot(
            self.binding.id, "native", "running-turn", "user", ActiveState.RUNNING,
        )
        self.runtime.admission = SubmissionAdmission(
            self.binding.id, 7, "native", "running-turn", self.binding.settings_revision,
            self.binding.context_revision, self.binding.feedback_revision,
        )
        self.runtime.submission = Submission(SubmitDisposition.STEERED, self.binding.id, "native", "running-turn")
        await self.app.handle_message(FakeMessage("adjust", message_id="om_steer"))
        request = self.runtime.submit_calls[-1]
        self.assertIsNone(request["cwd"])
        self.assertIs(request["admission"], self.runtime.admission)
        self.assertEqual(self.runtime.capture_calls, [self.binding.id])
        self.runtime.resolve_binding_cwd.assert_not_awaited()

    async def test_missing_native_directory_does_not_switch_or_globally_block_input(self):
        missing = self.fixture.project_root / "missing"
        self.materialize(missing)
        await self.app.handle_message(FakeMessage("pure text", message_id="om_work"))
        self.assertIsNone(self.runtime.submit_calls[-1]["cwd"])

    async def test_missing_project_default_does_not_block_materialized_worktree(self):
        self.materialize()
        self.fixture.project.rmdir()
        self.assertIsNone(self.app._binding_creation_cwd(self.binding))
        self.assertEqual(await self.app._binding_cwd(self.binding), self.selected)

    async def test_existing_binding_schedule_leaves_execution_directory_to_runtime(self):
        self.materialize()
        self.runtime.resolve_binding_cwd = AsyncMock(side_effect=RuntimeError("native read unavailable"))
        created = self.store.schedules.create(workspace=None,
            name="inspect", instructions="inspect", project_alias="test",
            app_id="cli_test", chat_id="oc_direct", target_kind="binding",
            target_binding_id=self.binding.id, session_settings=None,
            schedule=ScheduleRule("interval", "UTC", every_minutes=1, anchor=100),
            request_id="schedule-cwd", now=100,
        )
        claim = self.store.schedules.claim_due(created.plan_id, app_id="cli_test", now=160)
        self.channel.send_results.append(sent_result("om_scheduled", chat_id="oc_direct"))
        await self.app.dispatch_scheduled_run(claim)
        self.assertIsNone(self.runtime.submit_calls[-1]["cwd"])
        self.runtime.resolve_binding_cwd.assert_not_awaited()

    async def test_goal_creation_leaves_materialized_directory_to_runtime(self):
        self.materialize()
        self.runtime.resolve_binding_cwd = AsyncMock(side_effect=RuntimeError("native read unavailable"))
        self.runtime.available_capabilities = frozenset({NativeCapability.GOAL})
        self.runtime.goal_submission = GoalSubmission(self.binding.id, "native", "goal", lambda: None)
        self.app._goal_management_receipt = AsyncMock()
        self.app._present_running_goal = AsyncMock()
        await self.app.handle_message(FakeMessage("/goal work", message_id="om_goal_cwd"))
        self.assertIsNone(self.runtime.start_goal_calls[-1]["cwd"])
        self.runtime.resolve_binding_cwd.assert_not_awaited()

    async def test_goal_creation_passes_lazy_selected_directory_without_native_read(self):
        self.runtime.resolve_binding_cwd = AsyncMock(side_effect=RuntimeError("native read unavailable"))
        self.runtime.available_capabilities = frozenset({NativeCapability.GOAL})
        self.runtime.goal_submission = GoalSubmission(self.binding.id, "native", "goal", lambda: None)
        self.app._goal_management_receipt = AsyncMock()
        self.app._present_running_goal = AsyncMock()
        await self.app.handle_message(FakeMessage("/goal work", message_id="om_goal_lazy"))
        self.assertEqual(self.runtime.start_goal_calls[-1]["cwd"], self.selected)
        self.runtime.resolve_binding_cwd.assert_not_awaited()

    async def test_goal_resume_command_does_not_pre_read_native_workspace(self):
        self.materialize()
        self.runtime.available_capabilities = frozenset({NativeCapability.GOAL})
        self.runtime.goal_snapshot_value = replace(native_goal(), thread_id="native", status=GoalStatus.PAUSED)
        self.runtime.goal_submission = GoalSubmission(self.binding.id, "native", "goal", lambda: None)
        self.app._goal_management_receipt = AsyncMock()
        self.app._present_running_goal = AsyncMock()
        self.runtime.resolve_binding_cwd = AsyncMock(side_effect=RuntimeError("native read unavailable"))
        await self.app.handle_message(FakeMessage("/goal resume", message_id="om_resume_cwd"))
        self.runtime.resolve_binding_cwd.assert_not_awaited()
        self.assertEqual(len(self.runtime.resume_goal_calls), 1)

    async def test_goal_resume_card_does_not_pre_read_native_workspace(self):
        self.materialize()
        goal = replace(native_goal(), thread_id="native", status=GoalStatus.PAUSED)
        self.runtime.goal_snapshot_value = goal
        self.runtime.goal_submission = GoalSubmission(self.binding.id, "native", "goal", lambda: None)
        self.runtime.resolve_binding_cwd = AsyncMock(side_effect=RuntimeError("native read unavailable"))
        self.app._goal_management_receipt = AsyncMock()
        self.app._present_running_goal = AsyncMock()
        await self.app._card_control(CardControlIntent(
            scope=self.scope, source_id="om_resume_card", sender_id="user",
            name=CardControlName.GOAL_RESUME, binding_id=self.binding.id,
            goal_generation=goal_generation(goal), expected_goal_status=goal.status.value,
        ))
        self.runtime.resolve_binding_cwd.assert_not_awaited()
        self.assertEqual(len(self.runtime.resume_goal_calls), 1)
        self.assertEqual(self.runtime.resume_goal_calls[-1]["expected_created_at"], goal.created_at)

    async def test_side_creation_uses_native_cwd(self):
        self.materialize()
        self.runtime.available_capabilities = frozenset({NativeCapability.SIDE})
        self.channel.send_results.append(sent_result(
            "om_side_root", chat_id="oc_direct", thread_id="omt_side", root_id="om_side_root",
        ))
        await self.app.handle_message(FakeMessage("/side", message_id="om_side_cwd"))
        self.assertEqual(self.runtime.create_side_calls[-1]["cwd"], self.selected)

    async def test_external_message_target_does_not_pre_read_native_cwd(self):
        self.materialize()
        self.runtime.resolve_binding_cwd = AsyncMock(side_effect=RuntimeError("native read unavailable"))
        source = self.store.create_channel_binding(
            scope=FeishuScope("cli_test", "oc_source", ScopeKind.DIRECT),
            project_alias="test", creator_id="user",
        )
        self.store.assign_native_thread_id(source.id, "native-source")
        self.channel.send_results.append(sent_result("om_external", chat_id="oc_direct"))
        self.runtime.submit_external = AsyncMock(return_value=self.runtime.submission)
        self.app._present_session_submission = AsyncMock()
        await self.app.send_session_message("native-source", "native", "work")
        self.assertIsNone(self.runtime.submit_external.call_args.kwargs["cwd"])
        self.runtime.resolve_binding_cwd.assert_not_awaited()

    async def test_workspace_rejections_are_not_reported_as_unknown_delivery(self):
        for rejection in (WorktreeConflict("deleting"), SteerRace("execution cwd unavailable")):
            with self.subTest(rejection=type(rejection).__name__):
                error = self.app._session_failure(
                    rejection, binding=self.binding, scope=self.scope,
                    root=None, submission_attempted=True,
                )
                self.assertIs(error.details["netizen"]["accepted"], False)
                self.assertEqual(error.code, "input_rejected")

    async def test_files_use_outcome_directory_even_if_project_has_same_filename(self):
        self.materialize()
        (self.fixture.project / "answer.txt").write_text("wrong source")
        (self.selected / "answer.txt").write_text("correct source")
        alias = self.fixture.project_root / "selected-alias"
        alias.symlink_to(self.selected, target_is_directory=True)
        for cwd in (self.selected, alias):
            with self.subTest(cwd=cwd):
                _, files = self.app._completion_files(self.outcome(cwd=cwd), TurnDiffSummary())
                self.assertEqual(files[0].resolved_path, (self.selected / "answer.txt").resolve())
                self.assertEqual(files[0].size, len("correct source"))

    async def test_files_without_cwd_evidence_do_not_use_project_default(self):
        self.materialize()
        (self.fixture.project / "answer.txt").write_text("must not be attributed")
        with self.assertRaises(TurnFileError):
            self.app._completion_files(self.outcome(), TurnDiffSummary())

    async def test_missing_turn_cwd_keeps_text_without_reading_current_native_cwd(self):
        self.materialize()
        (self.selected / "answer.txt").write_text("must not be attributed")
        (self.selected / "image.png").write_bytes(b"not needed: must not be read")
        self.app._binding_cwd = AsyncMock(return_value=self.selected)
        outcome = replace(self.outcome(), result=completed_turn_result(
            file_change_item("answer.txt"),
            final_response="answer before\n\n![preview](image.png)\n\nanswer after",
        ))
        await self.app.handle_completion(outcome)
        self.app._binding_cwd.assert_not_awaited()
        self.assertEqual(self.channel.upload_calls, [])
        self.assertEqual(self.channel.send_calls, [])
        self.assertEqual(len(self.channel.replies), 1)
        rendered = str(self.channel.replies[-1][1])
        self.assertIn("answer before", rendered)
        self.assertIn("answer after", rendered)
        self.assertNotIn("answer.txt", rendered)

    async def test_inline_images_follow_outcome_directory(self):
        outcome = replace(self.outcome(cwd=self.selected), result=completed_turn_result(final_response="![a](image.png)"))
        self.app._prepare_result_images = AsyncMock(return_value="prepared")
        self.assertEqual(await self.app._prepare_task_result_images(outcome), "prepared")
        self.assertEqual(self.app._prepare_result_images.call_args.kwargs["cwd"], self.selected)

    async def test_model_create_inherits_native_cwd_and_managed_relationship(self):
        await self._assert_model_create_inherits_cwd()

    async def test_model_create_inherits_native_cwd_when_project_default_is_missing(self):
        self.fixture.project.rmdir()
        await self._assert_model_create_inherits_cwd()

    async def _assert_model_create_inherits_cwd(self):
        self.materialize()
        record = self.store.worktrees.register(
            project_alias="test", expected_project_revision=self.store.get_project("test").revision,
            root=str(self.selected), cwd=str(self.selected), common_dir=str(self.fixture.project / ".git"),
        )
        self.store.worktrees.mark_ready(record.id, git_dir=str(self.selected / ".git-data"), identity_token="test-token")
        self.runtime.native_thread_model = AsyncMock(return_value="gpt-future-codex")

        @asynccontextmanager
        async def track(alias):
            yield

        self.runtime.track_topic_creation = track
        self.runtime.require_topic_creation_open = lambda alias: None
        self.channel.send_results.append(sent_result("om_root", chat_id="oc_direct", thread_id="omt_created", root_id="om_root"))
        submissions = []

        async def submit(**kwargs):
            submissions.append(kwargs)
            binding = kwargs["binding"]
            self.store.assign_native_thread_id(binding.id, "native-created")
            return Submission(SubmitDisposition.STARTED, binding.id, "native-created", "turn-created")

        self.runtime.submit_external = AsyncMock(side_effect=submit)
        self.app._present_session_submission = AsyncMock()
        result = await self.app.create_session_thread("native", "task")
        created = self.store.get(result["netizen"]["bindingId"])
        self.assertEqual(created.working_cwd, str(self.selected))
        self.assertEqual(created.worktree_id, record.id)
        self.assertEqual(submissions[0]["cwd"], self.selected)
        self.assertEqual(self.store.active_binding(self.scope.key).id, self.binding.id)

    async def test_persistent_fork_inherits_native_cwd_when_project_default_is_missing(self):
        self.materialize()
        self.fixture.project.rmdir()
        project = self.store.get_project("test")
        source = ForkSource(
            self.binding.id, "native", self.binding.settings_revision,
            self.binding.context_revision, self.binding.feedback_revision, project.revision,
        )

        @asynccontextmanager
        async def track(alias):
            yield

        self.runtime.track_fork_creation = track
        self.runtime.require_fork_creation_open = lambda alias: None
        self.runtime.fork_exact = AsyncMock(return_value=SimpleNamespace(id="native-fork"))
        self.runtime.adopt_fork = AsyncMock(return_value=True)
        self.runtime.release_unbound_fork = AsyncMock()
        self.channel.send_results.append(sent_result(
            "om_fork_root", chat_id="oc_direct", thread_id="omt_fork", root_id="om_fork_root",
        ))
        await self.app._create_fork_from_card(
            intent=ForkCardAction("create", source, target_chat_id="oc_direct", name="fork"),
            scope=self.scope, source=self.binding, source_title="source",
            message_id="om_card", sender_id="user",
        )
        created = self.store.find_by_native_thread_id("native-fork")
        self.assertIsNotNone(created)
        self.assertEqual(created.working_cwd, str(self.selected))
        self.assertEqual(self.runtime.fork_exact.call_args.args[0].working_cwd, str(self.selected))
