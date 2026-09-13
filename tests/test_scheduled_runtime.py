from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from openai_codex import AsyncCodex, CodexConfig

from netizen.bindings import BindingNotFound, BindingStore, ProjectDisabled
from netizen.codex_runtime import (
    ActiveState, CodexRuntime, RuntimeClosed, ScheduledInitialStartConflict,
    ScheduledTurnReadError, SkillReferenceError, SubmitDisposition, ThreadLifecycleState,
    ThreadLifecycleStateUnknown, TurnObservationUnavailable, TurnStartFailed,
)
from netizen.domain import FeishuScope, MentionContextMode, MessageContextAnchor, ScopeKind
from netizen.model_settings import ModelCatalogError
from netizen.runtime.contracts import ContextCursorCommit
from netizen.schedules.models import ScheduleConflict, ScheduleRule
from netizen.session_settings import BindingTaskFeedback, BindingTurnSettings, SessionSettings
from tests.test_codex_runtime import (
    FakeCodex, FakeSkillCatalog, FakeTerminalCleanup, FakeThread,
    FakeThreadDeleteControl, fake_skills,
)
from tests.test_sdk_gap_adapter import _close_probe_pipes


class ScheduledRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.cwd = Path(self.directory.name)
        self.store = BindingStore()
        self.store.register_project(alias="p", cwd=str(self.cwd))
        self.codex = FakeCodex()
        # Behavioral fixtures replace the public handle constructor, rather
        # than inventing a Thread-read method on the high-level Codex facade.
        reader = patch(
            "netizen.codex_runtime.AsyncThread",
            side_effect=lambda codex, thread_id: FakeThread(thread_id, codex),
        )
        reader.start()
        self.addCleanup(reader.stop)
        self.cleanup = FakeTerminalCleanup(self.codex.events)
        self.delete = FakeThreadDeleteControl()
        self.outcomes = []

        async def capture(outcome):
            self.outcomes.append(outcome)

        self.runtime = CodexRuntime(
            codex=self.codex, bindings=self.store,
            terminal_cleanup=self.cleanup, thread_delete_control=self.delete,
            on_completion=capture, poll_interval_seconds=0,
            skill_catalog=FakeSkillCatalog(fake_skills()),
        )
        self.sequence = 0

    async def asyncTearDown(self):
        await self.runtime.cancel_tasks()
        self.store.close()
        self.directory.cleanup()

    def occurrence(self, *, session_settings=SessionSettings(), context_anchor=None):
        self.sequence += 1
        suffix = str(self.sequence)
        plan = self.store.schedules.create(
            name="Routine " + suffix, instructions="Inspect the project",
            project_alias="p", app_id="app", chat_id="chat",
            schedule=ScheduleRule("interval", "UTC", every_minutes=1, anchor=100),
            request_id="create-" + suffix, now=100,
        )
        claim = self.store.schedules.claim_due(plan.plan_id, app_id="app", now=160)
        assert claim is not None
        topic = "topic-" + suffix
        self.store.schedules.set_run(
            claim.run.id, phase="publishing_topic", root_message_id="root-" + suffix,
            topic_id=topic, origin_message_id="seed-" + suffix,
        )
        binding = self.store.create_scheduled_binding(
            run_id=claim.run.id,
            scope=FeishuScope("app", "chat", ScopeKind.TOPIC, topic),
            session_settings=session_settings, context_anchor=context_anchor,
        )
        return claim.run.id, binding

    async def start(self, run_id, binding, **extra):
        return await self.runtime.submit_initial(
            run_id=run_id, binding=binding, cwd=self.cwd,
            input="Inspect the project", owner_id="scheduled_plan:plan",
            origin=SimpleNamespace(message_id="seed"), **extra,
        )

    async def until(self, predicate):
        async with asyncio.timeout(1):
            while not predicate():
                await asyncio.sleep(0)

    async def test_immediate_terminal_releases_before_receipt_delivery(self):
        run_id, binding = self.occurrence()
        self.codex.complete_immediately = True
        submission = await self.start(run_id, binding)
        self.assertEqual(submission.disposition, SubmitDisposition.STARTED)
        self.assertEqual(self.store.schedules.get_run(run_id).initial_turn_id, submission.turn_id)
        self.assertEqual(self.codex.start_kwargs, [{"cwd": str(self.cwd)}])
        self.assertEqual(self.codex.resume_calls, [])
        await self.until(lambda: self.store.schedules.get_run(run_id).barrier == "released")
        self.assertEqual(self.outcomes, [])
        submission.release_receipt_attempt()
        self.assertTrue(await self.runtime.wait_idle(timeout=1))
        self.assertEqual(len(self.outcomes), 1)
        self.assertEqual(self.outcomes[0].turn_id, submission.turn_id)

    async def test_catch_up_initial_preserves_new_topic_anchor_and_followup_requires_cursor(self):
        anchor = MessageContextAnchor("seed-1", 100_000)
        run_id, binding = self.occurrence(
            session_settings=SessionSettings(message_context_mode=MentionContextMode.CATCH_UP),
            context_anchor=anchor,
        )
        initial = await self.start(run_id, binding)
        initial.release_receipt_attempt()
        self.assertEqual(self.codex.turn_inputs[0][1], "Inspect the project")
        self.assertEqual(self.store.get(binding.id).context_anchor, anchor)
        self.assertEqual(self.store.get(binding.id).context_revision, binding.context_revision)
        self.codex.handles[0].complete()
        self.assertTrue(await self.runtime.wait_idle(timeout=1))

        with self.assertRaisesRegex(ValueError, "catch-up submission requires"):
            await self.runtime.submit(binding=binding, cwd=self.cwd, input="Follow-up",
                owner_id="person", origin=object())
        admission = await self.runtime.capture_submission_admission(binding.id)
        with self.assertRaisesRegex(ValueError, "catch-up submission requires"):
            await self.runtime.submit(binding=binding, cwd=self.cwd, input="Follow-up",
                owner_id="person", origin=object(), admission=admission)
        self.assertEqual(len(self.codex.turn_inputs), 1)
        upper = MessageContextAnchor("human-followup", 101_000)
        followup = await self.runtime.submit(binding=binding, cwd=self.cwd, input="Follow-up",
            owner_id="person", origin=object(), admission=admission,
            context_commit=ContextCursorCommit(admission.context_revision, upper))
        followup.release_receipt_attempt()
        self.assertEqual(self.store.get(binding.id).context_anchor, upper)
        self.assertEqual(self.store.schedules.get_run(run_id).initial_turn_id, initial.turn_id)
        self.assertEqual(self.store.schedules.get_run(run_id).barrier, "released")
        self.assertEqual(self.codex.resume_calls, [(initial.thread_id, {"include_turns": False})])

    async def test_initial_model_and_feedback_settings_apply_and_followup_uses_ordinary_configuration(self):
        effort = SimpleNamespace(value="dynamic-effort")
        self.codex.model_response = SimpleNamespace(data=[SimpleNamespace(
            id="catalog-model", model="wire-model", display_name="Model", description="",
            is_default=True, default_reasoning_effort=effort, default_service_tier=None,
            supported_reasoning_efforts=[SimpleNamespace(reasoning_effort=effort, description="")],
            service_tiers=[SimpleNamespace(id="priority-v2", name="Fast", description="")],
        )], next_cursor=None)
        settings = BindingTurnSettings("catalog-model", "dynamic-effort", "priority-v2")
        feedback = BindingTaskFeedback(reaction_pulse_enabled=True, progress_card_enabled=True)
        run_id, binding = self.occurrence(session_settings=SessionSettings(settings, feedback))
        initial = await self.start(run_id, binding)
        initial.release_receipt_attempt()
        native_settings = {"model": "wire-model", "effort": effort, "service_tier": "priority-v2"}
        self.assertEqual(self.codex.start_kwargs, [{"cwd": str(self.cwd)}])
        self.assertEqual(self.codex.turn_calls[-1][2], native_settings)
        self.assertEqual(initial.task_feedback, feedback)
        # The running Turn owns its feedback snapshot, even if the persistent
        # Binding is subsequently configured for ordinary follow-up Turns.
        current = self.store.set_configuration(binding_id=binding.id,
            expected_settings_revision=binding.settings_revision,
            expected_context_revision=binding.context_revision,
            expected_feedback_revision=binding.feedback_revision, settings=settings,
            task_feedback=BindingTaskFeedback(), message_context_mode=MentionContextMode.CURRENT_ONLY,
            context_anchor=None)
        self.codex.handles[0].complete()
        self.assertTrue(await self.runtime.wait_idle(timeout=1))
        self.assertEqual(self.outcomes[0].task_feedback, feedback)
        self.assertIsNotNone(self.outcomes[0].activity)
        followup = await self.runtime.submit(binding=current, cwd=self.cwd, input="Follow-up",
            owner_id="person", origin=object())
        followup.release_receipt_attempt()
        self.assertEqual(self.codex.turn_calls[-1][2], native_settings)
        self.assertEqual(followup.task_feedback, BindingTaskFeedback())
        self.assertEqual(self.codex.model_calls, 2)
        self.assertEqual(self.codex.resume_calls, [(initial.thread_id, {"include_turns": False})])
        self.assertEqual(self.store.schedules.get_run(run_id).barrier, "released")

    async def test_model_unavailable_at_trigger_releases_without_native_start(self):
        run_id, binding = self.occurrence(session_settings=SessionSettings(
            turn_settings=BindingTurnSettings("removed-model", "high", "default"),
        ))
        with self.assertRaises(ModelCatalogError):
            await self.start(run_id, binding)
        self.assertEqual(self.codex.start_kwargs, [])
        self.assertEqual(self.codex.turn_inputs, [])
        self.assertEqual(self.store.schedules.get_run(run_id).barrier, "released")
        await self.runtime.capture_submission_admission(binding.id)

    async def test_reserved_initial_rejects_ordinary_input_without_waiting(self):
        run_id, binding = self.occurrence()
        entered, gate = asyncio.Event(), asyncio.Event()
        self.codex.turn_entered["native-1"] = entered
        self.codex.turn_gates["native-1"] = gate
        pending = asyncio.create_task(self.start(run_id, binding))
        await entered.wait()
        try:
            async with asyncio.timeout(0.1):
                with self.assertRaises(ScheduledInitialStartConflict):
                    await self.runtime.capture_submission_admission(binding.id)
                with self.assertRaises(ScheduledInitialStartConflict):
                    await self.runtime.submit(
                        binding=binding, cwd=self.cwd, input="Manual message",
                        owner_id="person", origin=object(),
                    )
            with self.assertRaises(ScheduleConflict):
                await self.start(run_id, binding)
            self.assertEqual(self.store.schedules.get_run(run_id).barrier, "held")
        finally:
            gate.set()
            submission = await pending
            submission.release_receipt_attempt()
        self.assertEqual(len(self.codex.start_kwargs), 1)
        self.assertEqual(self.codex.handles[0].steers, [])

    async def test_initial_never_resumes_an_already_materialized_binding(self):
        run_id, binding = self.occurrence()
        self.store.assign_native_thread_id(binding.id, "existing-thread")
        with self.assertRaises((ScheduleConflict, ScheduledInitialStartConflict)):
            await self.start(run_id, binding)
        self.assertEqual(self.codex.start_kwargs, [])
        self.assertEqual(self.codex.resume_calls, [])
        self.assertEqual(self.codex.turn_inputs, [])

    async def test_failed_initial_releases_even_without_outcome_status(self):
        run_id, binding = self.occurrence()
        submission = await self.start(run_id, binding)
        submission.release_receipt_attempt()
        self.codex.handles[0].fail("business failure")
        self.assertTrue(await self.runtime.wait_idle(timeout=1))
        self.assertEqual(self.store.schedules.get_run(run_id).barrier, "released")
        self.assertIsNotNone(self.outcomes[0].error)
        self.assertIsNone(self.outcomes[0].status)

    async def test_initial_uses_ordinary_steer_stop_and_later_turns(self):
        run_id, binding = self.occurrence()
        first = await self.start(run_id, binding)
        first.release_receipt_attempt()
        manual = await self.runtime.submit(
            binding=binding, cwd=self.cwd, input="Adjustment",
            owner_id="person", origin=object(),
        )
        self.assertEqual(manual.disposition, SubmitDisposition.STEERED)
        self.assertEqual(manual.turn_id, first.turn_id)
        await self.runtime.stop_exact(binding.id)
        self.assertTrue(await self.runtime.wait_idle(timeout=1))
        self.assertEqual(self.codex.handles[0].interrupt_count, 1)
        self.assertEqual(self.store.schedules.get_run(run_id).barrier, "released")
        later = await self.runtime.submit(
            binding=binding, cwd=self.cwd, input="Follow-up",
            owner_id="person", origin=object(),
        )
        later.release_receipt_attempt()
        self.assertNotEqual(later.turn_id, first.turn_id)
        self.assertEqual(self.store.schedules.get_run(run_id).initial_turn_id, first.turn_id)
        self.assertEqual(self.store.schedules.get_run(run_id).barrier, "released")
        self.assertEqual(await self.runtime.read_scheduled_turn(binding.id, first.turn_id), "interrupted")

    async def test_same_cwd_bindings_start_concurrently(self):
        run_id, binding = self.occurrence()
        second_id, second_binding = self.occurrence()
        entered, gate = asyncio.Event(), asyncio.Event()
        self.codex.turn_entered["native-1"] = entered
        self.codex.turn_gates["native-1"] = gate
        first = asyncio.create_task(self.start(run_id, binding))
        await entered.wait()
        try:
            async with asyncio.timeout(0.1):
                second = await self.start(second_id, second_binding)
            second.release_receipt_attempt()
            self.assertFalse(first.done())
        finally:
            gate.set()
            (await first).release_receipt_attempt()
        self.assertEqual(len(self.codex.start_kwargs), 2)

    async def test_skills_compile_without_redeeming_ordinary_reservation(self):
        run_id, binding = self.occurrence()
        submission = await self.start(run_id, binding, skill_names=("code-review",))
        submission.release_receipt_attempt()
        inputs = self.codex.turn_inputs[0][1]
        self.assertEqual(inputs[-1].name, "code-review")
        self.assertEqual(inputs[0].text, "Inspect the project")

    async def test_native_start_unknown_closes_global_admission(self):
        run_id, binding = self.occurrence()
        other = self.store.create_channel_binding(
            scope=FeishuScope("app", "another", ScopeKind.GROUP),
            project_alias="p", creator_id="person",
        )
        self.codex.turn_errors_after_start.append(ConnectionError("lost response"))
        with self.assertRaises(TurnStartFailed):
            await self.start(run_id, binding)
        run = self.store.schedules.get_run(run_id)
        self.assertIsNone(run.initial_turn_id)
        self.assertEqual(run.barrier, "unknown")
        with self.assertRaises(RuntimeClosed):
            await self.runtime.submit(
                binding=other, cwd=self.cwd, input="Unrelated",
                owner_id="person", origin=object(),
            )
        self.assertEqual(len(self.codex.handles), 1)

    async def test_invalid_skill_releases_claim_without_native_start(self):
        run_id, binding = self.occurrence()
        with self.assertRaises(SkillReferenceError):
            await self.start(run_id, binding, skill_names=("not-installed",))
        self.assertEqual(self.store.schedules.get_run(run_id).barrier, "released")
        self.assertEqual(self.codex.start_kwargs, [])

    async def test_cancelled_native_start_stays_unknown(self):
        run_id, binding = self.occurrence()
        entered = asyncio.Event()
        self.codex.turn_entered["native-1"] = entered
        self.codex.turn_gates["native-1"] = asyncio.Event()
        pending = asyncio.create_task(self.start(run_id, binding))
        await entered.wait()
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(self.store.schedules.get_run(run_id).barrier, "unknown")
        with self.assertRaises(RuntimeClosed):
            await self.runtime.capture_submission_admission(binding.id)

    async def test_initial_reference_commit_failure_keeps_the_native_consumer(self):
        run_id, binding = self.occurrence()
        with patch.object(self.store, "mark_scheduled_turn_started", side_effect=OSError("disk")):
            with self.assertRaises(TurnStartFailed):
                await self.start(run_id, binding)
        self.assertEqual(self.store.schedules.get_run(run_id).barrier, "unknown")
        self.assertIsNotNone(self.runtime.active_turn(binding.id))
        self.codex.handles[0].complete()
        self.assertTrue(await self.runtime.wait_idle(timeout=1))
        self.assertEqual(len(self.outcomes), 1)
        self.assertEqual(self.store.schedules.get_run(run_id).barrier, "released")

    async def test_project_freeze_after_thread_start_prevents_initial_turn(self):
        run_id, binding = self.occurrence()
        start_native = self.codex.thread_start

        async def start_and_disable(**kwargs):
            thread = await start_native(**kwargs)
            project = self.store.get_project("p")
            self.store.set_project_enabled(
                alias="p", enabled=False, expected_revision=project.revision,
            )
            return thread

        with patch.object(self.codex, "thread_start", side_effect=start_and_disable):
            with self.assertRaises(ProjectDisabled):
                await self.start(run_id, binding)
        self.assertEqual(self.codex.turn_inputs, [])
        self.assertEqual(self.store.get(binding.id).native_thread_id, "native-1")
        self.assertEqual(self.store.schedules.get_run(run_id).barrier, "released")

    async def test_archive_and_delete_release_without_local_stop(self):
        for operation in ("archive", "delete"):
            with self.subTest(operation=operation):
                run_id, binding = self.occurrence()
                submission = await self.start(run_id, binding)
                submission.release_receipt_attempt()
                if operation == "archive":
                    await self.runtime.archive_exact(binding.id)
                    self.assertFalse(self.store.get(binding.id).active)
                else:
                    await self.runtime.delete_exact(
                        binding.id, expected_native_thread_id=submission.thread_id,
                    )
                    with self.assertRaises(BindingNotFound):
                        self.store.get(binding.id)
                run = self.store.schedules.get_run(run_id)
                self.assertEqual(run.barrier, "released")
                self.assertEqual(run.binding_removed, operation == "delete")
                self.assertEqual(run.initial_turn_id, submission.turn_id)
                self.assertEqual(self.codex.handles[-1].interrupt_count, 0)
        self.assertEqual(self.cleanup.calls, [])

    async def test_read_after_restart_is_exact_read_only_and_bounded(self):
        run_id, binding = self.occurrence()
        submission = await self.start(run_id, binding)
        submission.release_receipt_attempt()
        await self.runtime.cancel_tasks()
        self.codex.read_calls.clear()
        self.assertEqual(await self.runtime.read_scheduled_turn(binding.id, submission.turn_id), "inProgress")
        self.assertEqual(self.codex.read_calls, [(submission.thread_id, True)])
        self.assertEqual(self.codex.resume_calls, [])
        with self.assertRaises(ScheduledTurnReadError) as caught:
            await self.runtime.read_scheduled_turn(binding.id, "another-turn")
        self.assertEqual(caught.exception.code, "turn_unavailable")
        self.codex.read_gate = asyncio.Event()
        with self.assertRaises(ScheduledTurnReadError) as caught:
            await self.runtime.read_scheduled_turn(
                binding.id, submission.turn_id,
                deadline=asyncio.get_running_loop().time() + 0.01,
            )
        self.assertEqual(caught.exception.code, "read_timeout")
        self.assertEqual(self.store.schedules.get_run(run_id).barrier, "held")

    async def test_read_respects_ordinary_observation_and_lifecycle_unknown(self):
        run_id, binding = self.occurrence()
        submission = await self.start(run_id, binding)
        submission.release_receipt_attempt()
        active = self.runtime._active[binding.id]
        active.state = ActiveState.OBSERVATION_UNAVAILABLE
        before = len(self.codex.read_calls)
        with self.assertRaises(TurnObservationUnavailable):
            await self.runtime.read_scheduled_turn(binding.id, submission.turn_id)
        self.assertEqual(len(self.codex.read_calls), before)
        await self.runtime.cancel_tasks()
        self.runtime._lifecycles[binding.id] = SimpleNamespace(state=ThreadLifecycleState.UNKNOWN)
        with self.assertRaises(ThreadLifecycleStateUnknown):
            await self.runtime.read_scheduled_turn(binding.id, submission.turn_id)
        self.assertEqual(len(self.codex.read_calls), before)

    async def test_read_rejects_wrong_identity_and_inconsistent_state(self):
        run_id, binding = self.occurrence()
        submission = await self.start(run_id, binding)
        submission.release_receipt_attempt()
        await self.runtime.cancel_tasks()
        for identity, state, code in (
            ("another-thread", "active", "identity_mismatch"),
            (submission.thread_id, "idle", "status_conflict"),
            (submission.thread_id, "notLoaded", "status_conflict"),
        ):
            with self.subTest(code=code, state=state):
                result = SimpleNamespace(thread=SimpleNamespace(
                    id=identity,
                    status=SimpleNamespace(root=SimpleNamespace(type=state)),
                    turns=[self.codex.handles[0].record],
                ))
                with patch.object(FakeThread, "read", return_value=result):
                    with self.assertRaises(ScheduledTurnReadError) as caught:
                        await self.runtime.read_scheduled_turn(binding.id, submission.turn_id)
                self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.store.schedules.get_run(run_id).barrier, "held")


_READ_ONLY_SERVER = r'''
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    with open(sys.argv[1], "a", encoding="utf-8") as log:
        log.write(json.dumps(request) + "\n")
    if "id" not in request:
        continue
    if request["method"] == "initialize":
        result = {"userAgent": "netizen-schedule-read-test/1", "serverInfo": {"name": "fake", "version": "1"}}
    elif request["method"] == "thread/read":
        result = {"thread": {
            "id": request["params"]["threadId"], "preview": "scheduled test",
            "ephemeral": False, "modelProvider": "openai",
            "createdAt": 0, "updatedAt": 2, "status": {"type": "active", "activeFlags": []},
            "cwd": "/tmp/project", "path": "/tmp/scheduled-test.jsonl",
            "cliVersion": "0.147.0", "source": "appServer", "sessionId": "session",
            "turns": [
                {"id": "initial-turn", "items": [], "status": "completed"},
                {"id": "manual-turn", "items": [], "status": "inProgress"},
            ],
        }}
    else:
        sys.stdout.write(json.dumps({"id": request["id"], "error": {"code": -32601, "message": "Unexpected mutation or subscription"}}) + "\n")
        sys.stdout.flush()
        continue
    sys.stdout.write(json.dumps({"id": request["id"], "result": result}) + "\n")
    sys.stdout.flush()
'''


class ScheduledSdkReadShapeTest(unittest.IsolatedAsyncioTestCase):
    async def test_installed_public_sdk_reads_exact_history_without_resume_or_stream(self):
        """Exercise the actual exported facade/handle and generated wire schema."""
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "requests.jsonl"
            with closing(BindingStore()) as store:
                binding = store.create_binding(
                    scope=FeishuScope("app", "chat", ScopeKind.TOPIC, "topic"),
                    project_alias="p", creator_id="scheduled_plan:test",
                )
                store.assign_native_thread_id(binding.id, "exact-native-thread")
                config = CodexConfig(
                    launch_args_override=(sys.executable, "-u", "-c", _READ_ONLY_SERVER, str(log_path)),
                    experimental_api=False,
                )
                process = None
                try:
                    async with AsyncCodex(config) as codex:
                        process = codex._client._sync._proc
                        runtime = CodexRuntime(
                            codex=codex, bindings=store,
                            terminal_cleanup=FakeTerminalCleanup([]),
                        )
                        self.assertEqual(
                            await runtime.read_scheduled_turn(binding.id, "initial-turn"),
                            "completed",
                        )
                        self.assertEqual(
                            await runtime.read_scheduled_turn(binding.id, "manual-turn"),
                            "inProgress",
                        )
                        self.assertIsNone(runtime.active_turn(binding.id))
                        await runtime.cancel_tasks()
                finally:
                    if process is not None:
                        _close_probe_pipes(process)
            requests = [json.loads(line) for line in log_path.read_text().splitlines()]
        methods = [request["method"] for request in requests if "id" in request]
        self.assertEqual(methods, ["initialize", "thread/read", "thread/read"])
        reads = [request["params"] for request in requests if request["method"] == "thread/read"]
        self.assertEqual(reads, [
            {"threadId": "exact-native-thread", "includeTurns": True},
            {"threadId": "exact-native-thread", "includeTurns": True},
        ])


if __name__ == "__main__":
    unittest.main()
