import asyncio
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

from netizen_cli.bindings import BindingTaskFeedback, ThreadBinding
from netizen_cli.channel import constants
from netizen_cli.domain import ActiveState
from netizen_cli.file_evidence import FileEvidence
from netizen_cli.management import ExactBindingTarget, RuntimePrecondition
from netizen_cli.ordinary_sessions import (
    OrdinaryCompletionHandler, OrdinaryConclusion, OrdinaryInputRejected, PreparedPrompt, StopPreview,
)
from netizen_cli.runtime.codex_ordinary import CodexOrdinarySessions
from netizen_cli.runtime.contracts import (
    ActiveTurnSnapshot, ContextCursorCommit, StopAcknowledger, StopDisposition, Submission, SubmissionAdmission, SubmitDisposition, TerminalStateUnknown,
    ThreadActivityChanged, TurnActivitySnapshot, TurnObservationUnavailableOutcome,
)
from tests.support.channel_fixtures import channel_fixture
from tests.support.channel_messages import FakeMessage
from tests.support.channel_results import sent_result


class StartOnlySessions:
    """An ordinary backend with one active execution and no busy-input operation."""

    def __init__(self):
        self.activity: TurnActivitySnapshot | None = None
        self.receipt: Submission | None = None
        self.inputs: list[PreparedPrompt] = []
        self.receipt_released = asyncio.Event()
        self.sink: OrdinaryCompletionHandler
        self.revision = 0
        self.previewed = asyncio.Event()
        self.interrupts: list[str] = []

    async def capture_submission_admission(self, binding_id: str) -> SubmissionAdmission:
        if self.activity is not None:
            raise OrdinaryInputRejected("当前任务仍在运行，本条消息未执行。")
        return SubmissionAdmission(binding_id, self.revision, None, None, 1)

    async def submit(
        self, *, binding: ThreadBinding, cwd: Path | None, input: PreparedPrompt, owner_id: str,
        origin: object, admission: SubmissionAdmission, context_commit: ContextCursorCommit | None,
        skill_names: tuple[str, ...],
    ) -> Submission:
        if self.activity is not None or admission.revision != self.revision or admission.binding_id != binding.id:
            raise OrdinaryInputRejected("目标已经变化，本条消息未执行。")
        self.inputs.append(input)
        self.revision += 1
        self.activity = TurnActivitySnapshot(
            binding.id, "thread", "turn", 1, ActiveState.RUNNING, 0, False, False, False, (),
        )
        self.receipt = Submission(
            SubmitDisposition.STARTED, binding.id, "thread", "turn",
            self.receipt_released.set, binding.task_feedback, binding.feedback_revision,
        )
        return self.receipt

    def turn_activity(self, binding_id: str, *, thread_id: str, turn_id: str) -> TurnActivitySnapshot | None:
        activity = self.activity
        if activity and (activity.binding_id, activity.thread_id, activity.turn_id) == (binding_id, thread_id, turn_id):
            return activity
        return None

    def reply_blocked_by_lifecycle(self, binding_id: str) -> bool:
        return False

    async def preview_stop(self, binding: ThreadBinding) -> StopPreview | None:
        self.previewed.set()
        return StopPreview("正在停止任务。") if self.activity is not None else None

    def _require_current(self, binding_id: str, revision: int, turn_id: str | None) -> TurnActivitySnapshot:
        current = self.activity
        if current is None or (current.binding_id, current.revision, current.turn_id) != (binding_id, revision, turn_id):
            raise ThreadActivityChanged("stale activity")
        return current

    async def stop_exact(
        self, binding_id: str, *, acknowledge: StopAcknowledger | None = None,
        expected_activity_revision: int | None = None, expected_turn_id: str | None = None,
    ) -> StopDisposition:
        if expected_activity_revision is not None:
            self._require_current(binding_id, expected_activity_revision, expected_turn_id)
        current = self.activity
        if current is None or current.binding_id != binding_id:
            return StopDisposition.NOT_RUNNING
        self.activity = replace(current, state=ActiveState.STOPPING)
        self.interrupts.append(current.turn_id)
        if acknowledge is not None:
            await acknowledge()
        return StopDisposition.REQUESTED

    async def recheck_turn_exact(
        self, binding_id: str, *, expected_activity_revision: int, expected_turn_id: str,
    ) -> ActiveTurnSnapshot:
        current = self._require_current(binding_id, expected_activity_revision, expected_turn_id)
        return ActiveTurnSnapshot(binding_id, current.thread_id, current.turn_id, "ou_user", current.state)


class CodexOrdinarySessionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_exact_preserves_preconditions(self):
        runtime = AsyncMock()
        sessions = CodexOrdinarySessions(runtime)
        with self.assertRaisesRegex(ValueError, "expected_activity_revision"):
            await sessions.stop_exact("binding", expected_turn_id="turn")
        runtime.stop_exact.assert_not_awaited()

        for turn_id in ("turn", None):
            await sessions.stop_exact("binding", expected_activity_revision=3, expected_turn_id=turn_id)
            runtime.stop_exact.assert_awaited_with(
                "binding", acknowledge=None, expected_activity_revision=3, expected_turn_id=turn_id,
            )
        await sessions.stop_exact("binding")
        runtime.stop_exact.assert_awaited_with("binding", acknowledge=None)


class OrdinaryChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_busy_rejection_parking_and_recovered_conclusion(self):
        backend = StartOnlySessions()
        async with channel_fixture(ordinary=backend) as fixture:
            backend.sink = fixture.app.handle_ordinary_completion
            message = FakeMessage("run first", message_id="om_first")
            binding = fixture.store.create_channel_binding(
                scope=fixture.app._scope(message), project_alias="test", creator_id="ou_user",
                task_feedback=BindingTaskFeedback(progress_card_enabled=True),
            )
            fixture.channel.reply_results.append(sent_result("om_progress", chat_id="oc_direct"))
            stale = await backend.capture_submission_admission(binding.id)
            await fixture.app.handle_message(message)
            self.assertTrue(backend.receipt_released.is_set())
            self.assertEqual(backend.receipt.disposition, SubmitDisposition.STARTED)
            self.assertEqual(len(backend.inputs), 1)
            self.assertIn("run first", backend.inputs[0].text)
            with self.assertRaises(OrdinaryInputRejected):
                await backend.submit(
                    binding=binding, cwd=fixture.project, input=PreparedPrompt("late"), owner_id="ou_user",
                    origin=message, admission=stale, context_commit=None, skill_names=(),
                )
            await fixture.app.handle_message(FakeMessage("busy", message_id="om_busy"))
            self.assertIn("本条消息未执行", str(fixture.channel.replies[-1][1]))
            self.assertNotIn(("om_busy", constants.STEER_REACTION), fixture.channel.reactions)
            self.assertEqual(len(backend.inputs), 1)

            backend.activity = replace(backend.activity, state=ActiveState.OBSERVATION_UNAVAILABLE, revision=2)
            await backend.sink(TurnObservationUnavailableOutcome(
                binding.id, "thread", "turn", "ou_user", message, TerminalStateUnknown("stream lost"),
            ))
            self.assertEqual(fixture.channel.updates[-1][0], "om_progress")
            self.assertIn("观测不可用", str(fixture.channel.updates[-1][1]))
            self.assertIn("尚未确认本轮是否结束", str(fixture.channel.replies[-1][1]))
            self.assertNotIn(("om_first", constants.DONE_REACTION), fixture.channel.reactions)
            self.assertEqual(fixture.channel.reaction_removals[0][0], "om_first")

            conclusion = OrdinaryConclusion(
                binding.id, "thread", "turn", "ou_user", message, "completed", "recovered answer",
                cwd=fixture.project, task_feedback=binding.task_feedback,
            )
            self.assertEqual(conclusion.file_evidence(), FileEvidence())
            backend.activity = None
            await backend.sink(conclusion)
            self.assertIn(("om_first", constants.DONE_REACTION), fixture.channel.reactions)
            self.assertTrue(any("recovered answer" in str(item) for _, item in fixture.channel.replies))
            self.assertEqual(fixture.runtime.submit_calls, [])
            self.assertEqual(fixture.runtime.capture_calls, [])

    async def test_bare_stop_selects_current_execution_and_card_controls_keep_exact_target(self):
        backend = StartOnlySessions()
        async with channel_fixture(ordinary=backend) as fixture:
            await fixture.new()
            message = FakeMessage("/stop", message_id="om_stop")
            scope = fixture.app._scope(message)
            binding = fixture.store.active_binding(scope.key)
            target = ExactBindingTarget(scope.key, binding.id, binding.id)
            backend.activity = TurnActivitySnapshot(
                binding.id, "thread", "first", 1, ActiveState.RUNNING, 0, False, False, False, (),
            )
            async with asyncio.TaskGroup() as tasks:
                async with fixture.management.scope_coordinator.hold(scope.key):
                    tasks.create_task(fixture.app.handle_message(message))
                    await asyncio.wait_for(backend.previewed.wait(), timeout=2)
                    backend.activity = replace(backend.activity, turn_id="second", revision=2)
            self.assertEqual(backend.interrupts, ["second"])
            self.assertIn((message.id, "正在停止任务。"), fixture.channel.replies)

            stale = RuntimePrecondition(1, "first")
            with self.assertRaises(ThreadActivityChanged):
                await fixture.management.stop_exact_binding(target=target, runtime_precondition=stale)
            with self.assertRaises(ThreadActivityChanged):
                await fixture.management.recheck_exact_turn(target=target, runtime_precondition=stale)
            self.assertEqual(backend.interrupts, ["second"])
            current = await fixture.management.recheck_exact_turn(
                target=target, runtime_precondition=RuntimePrecondition(2, "second"),
            )
            self.assertEqual((current.thread_id, current.turn_id, current.state),
                             ("thread", "second", ActiveState.STOPPING))
            self.assertEqual(fixture.runtime.stop_calls, [])

    async def test_exact_stop_preview_cannot_follow_a_replacement_execution(self):
        class ExactPreviewSessions(StartOnlySessions):
            async def preview_stop(self, binding):
                self.previewed.set()
                return StopPreview("正在停止任务。", self.activity.revision, self.activity.turn_id)

        backend = ExactPreviewSessions()
        async with channel_fixture(ordinary=backend) as fixture:
            await fixture.new()
            message = FakeMessage("/stop", message_id="om_stop")
            scope = fixture.app._scope(message)
            binding = fixture.store.active_binding(scope.key)
            backend.activity = TurnActivitySnapshot(
                binding.id, "thread", "first", 1, ActiveState.RUNNING, 0, False, False, False, (),
            )
            async with fixture.management.scope_coordinator.hold(scope.key):
                task = asyncio.create_task(fixture.app.handle_message(message))
                await asyncio.wait_for(backend.previewed.wait(), 2)
                backend.activity = replace(backend.activity, turn_id="second", revision=2)
            await asyncio.wait_for(task, 2)
            self.assertEqual(backend.interrupts, [])
            self.assertNotIn((message.id, "正在停止任务。"), fixture.channel.replies)


class OrdinaryPortContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_busy_policy_completion_and_stale_admission_on_both_runtimes(self):
        from tempfile import TemporaryDirectory

        from netizen_cli.bindings import BindingStore
        from netizen_cli.codex_runtime import CodexRuntime
        from netizen_cli.domain import FeishuScope, ScopeKind
        from netizen_cli.runtime.claude_ordinary import ClaudeOrdinarySessions
        from netizen_cli.runtime.codex_replies import codex_completion_handler
        from netizen_cli.runtime.contracts import SteerRace
        from tests.support.claude_client import FakeClaudeClients
        from tests.test_codex_runtime import FakeCodex, FakeTerminalCleanup

        for backend in ("codex", "claude"):
            with self.subTest(backend=backend), TemporaryDirectory() as tmp:
                store = BindingStore()
                cwd = Path(tmp)
                store.bootstrap_project(alias="test", cwd=str(cwd))
                binding = store.create_channel_binding(scope=FeishuScope("app", "chat", ScopeKind.DIRECT),
                                                       project_alias="test", creator_id="owner")
                sink = AsyncMock()
                if backend == "codex":
                    sdk = FakeCodex()
                    runtime = CodexRuntime(codex=sdk, bindings=store, terminal_cleanup=FakeTerminalCleanup(sdk.events),
                                           automatic_thread_naming=False, poll_interval_seconds=0,
                                           on_completion=codex_completion_handler(sink, AsyncMock(), AsyncMock()))
                    ordinary = CodexOrdinarySessions(runtime)
                else:
                    now = 0.
                    clients = FakeClaudeClients()
                    ordinary = runtime = ClaudeOrdinarySessions(bindings=store, clients=clients, owner_id="owner",
                                                                notice=AsyncMock(), on_completion=sink, clock=lambda: now)
                    runtime.open_admission()
                    now = 2.
                try:
                    async def submit(admission):
                        current = store.get(binding.id)
                        return await ordinary.submit(binding=current, cwd=None if current.native_thread_id else cwd,
                                                     input=PreparedPrompt("input"), owner_id="owner", origin="origin",
                                                     admission=admission, context_commit=None, skill_names=())

                    stale = await ordinary.capture_submission_admission(binding.id)
                    first = await submit(stale)
                    self.assertEqual(first.disposition, SubmitDisposition.STARTED)
                    self.assertEqual(store.get(binding.id).native_thread_id, first.thread_id)
                    if backend == "codex":
                        follow = await submit(await ordinary.capture_submission_admission(binding.id))
                        self.assertEqual((follow.disposition, follow.turn_id), (SubmitDisposition.STEERED, first.turn_id))
                        self.assertEqual(len(sdk.handles), 1)
                        sdk.handles[0].complete(response="answer")
                    else:
                        with self.assertRaises(OrdinaryInputRejected):
                            await ordinary.capture_submission_admission(binding.id)
                        self.assertEqual(len(clients.calls[0].inputs), 1)
                        clients.calls[0].init()
                        clients.calls[0].result()
                    first.release_receipt_attempt()
                    self.assertTrue(await runtime.wait_idle(2))
                    event = sink.await_args.args[0]
                    self.assertEqual((event.binding_id, event.thread_id, event.turn_id, event.owner_id, event.origin,
                                      event.final_response, event.status),
                                     (binding.id, first.thread_id, first.turn_id, "owner", "origin", "answer", "completed"))
                    self.assertEqual(sink.await_count, 1)
                    with self.assertRaises((SteerRace, OrdinaryInputRejected)):
                        await submit(stale)
                finally:
                    runtime.close_admission()
                    await runtime.cancel_tasks()
                    store.close()
