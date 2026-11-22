import asyncio
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from netizen_cli.backends import BackendPolicy
from netizen_cli.bindings import BindingStore, BindingTaskFeedback, ThreadBinding
from netizen_cli.channel import constants
from netizen_cli.codex_runtime import CodexRuntime
from netizen_cli.domain import ActiveState, FeishuScope, MentionContextMode, MessageContextAnchor, ScopeKind
from netizen_cli.file_evidence import FileEvidence
from netizen_cli.management import (
    ExactBindingTarget, InstanceManagementService, ManagementRuntimePort, RuntimePrecondition, ScopeCoordinator,
)
from netizen_cli.ordinary_sessions import (
    OrdinaryCompletionHandler, OrdinaryConclusion, OrdinaryInputRejected, PreparedPrompt, StopPreview,
)
from netizen_cli.projects import ProjectRegistry
from netizen_cli.runtime.claude_ordinary import ClaudeOrdinarySessions
from netizen_cli.runtime.codex_ordinary import CodexOrdinarySessions
from netizen_cli.runtime.codex_replies import codex_completion_handler
from netizen_cli.runtime.contracts import (
    ActiveTurnSnapshot, ContextBoundaryCommitFailed, ContextCursorCommit, RuntimeClosed, SteerRace, StopAcknowledger,
    StopDisposition, Submission, SubmissionAdmission, SubmitDisposition, TerminalStateUnknown, ThreadActivityChanged,
    ThreadStopping, TurnActivitySnapshot, TurnObservationUnavailableOutcome,
)
from tests.support.channel_fixtures import channel_fixture
from tests.support.channel_messages import FakeMessage
from tests.support.channel_results import sent_result
from tests.support.claude_client import FakeClaudeClients
from tests.test_codex_runtime import FakeCodex, FakeTerminalCleanup


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


class OrdinaryPortContract:
    """Run the same ordinary operations against each production runtime and its SDK double."""

    async def asyncSetUp(self):
        self.cwd = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.store = BindingStore()
        self.addCleanup(self.store.close)
        self.projects = ProjectRegistry(store=self.store, project_root=self.cwd, projects={"test": self.cwd})
        self.binding = self.new_binding()
        self.outcomes = asyncio.Queue()
        self.notices = asyncio.Queue()

        if self.backend == "codex":
            self.codex = FakeCodex()
            self.runtime = CodexRuntime(
                codex=self.codex, bindings=self.store, terminal_cleanup=FakeTerminalCleanup(self.codex.events),
                automatic_thread_naming=False, poll_interval_seconds=0,
                on_completion=codex_completion_handler(self.outcomes.put, AsyncMock(), AsyncMock()),
            )
            self.ordinary = CodexOrdinarySessions(self.runtime)
            self.port = ManagementRuntimePort(self.runtime, ordinary=self.ordinary)
        else:
            async def notice(origin, text):
                await self.notices.put((origin, text))

            now = 0.
            self.clients = FakeClaudeClients()
            self.ordinary = self.runtime = ClaudeOrdinarySessions(
                bindings=self.store, clients=self.clients, owner_id="owner", on_completion=self.outcomes.put,
                notice=notice, clock=lambda: now,
            )
            self.port = ManagementRuntimePort(None, ordinary=self.ordinary, local=self.ordinary)
            self.runtime.open_admission()
            now = 2.

        self.addAsyncCleanup(self.runtime.cancel_tasks)
        self.addCleanup(self.runtime.close_admission)

    def new_binding(self, chat="chat", *, kind=ScopeKind.DIRECT, **settings):
        return self.store.create_channel_binding(
            scope=FeishuScope("app", chat, kind), project_alias="test", creator_id="owner", **settings,
        )

    async def submit(self, binding, *, admission=None, origin="origin", context_commit=None):
        current = self.store.get(binding.id)
        if admission is None:
            admission = await self.ordinary.capture_submission_admission(current.id)
        return await self.ordinary.submit(
            binding=current, cwd=None if current.native_thread_id else self.cwd, input=PreparedPrompt("input"),
            owner_id="owner", origin=origin, admission=admission, context_commit=context_commit, skill_names=(),
        )

    def complete(self, turn_id, answer="answer"):
        if self.backend == "codex":
            handle = next(handle for handle in self.codex.handles if handle.id == turn_id)
            handle.complete(response=answer)
        else:
            client = next(client for client in self.clients.calls if client.inputs[0]["uuid"] == turn_id)
            client.init()
            client.result(result=answer)

    def assert_native_starts(self, count):
        if self.backend == "codex":
            self.assertEqual(len(self.codex.handles), count)
        else:
            self.assertEqual([len(client.inputs) for client in self.clients.calls], [1] * count)

    async def conclusion(self):
        # Interrupted Codex Turns take a second native read after a two-second confirmation interval.
        event = await asyncio.wait_for(self.outcomes.get(), 10)
        self.assertIsInstance(event, OrdinaryConclusion)
        return event

    async def finish(self, receipt):
        receipt.release_receipt_attempt()
        self.complete(receipt.turn_id)
        self.assertTrue(await self.runtime.wait_idle(2))
        event = await self.conclusion()
        self.assertEqual((event.turn_id, event.status), (receipt.turn_id, "completed"))
        self.assertTrue(self.outcomes.empty())
        return event

    async def test_start_busy_policy_completion_and_stale_admission(self):
        binding = self.binding
        admission = await self.ordinary.capture_submission_admission(binding.id)
        first = await self.submit(binding, admission=admission)
        self.assertEqual(first.disposition, SubmitDisposition.STARTED)
        self.assertEqual(self.store.get(binding.id).native_thread_id, first.thread_id)
        if self.backend == "codex":
            follow = await self.submit(binding)
            self.assertEqual((follow.disposition, follow.turn_id), (SubmitDisposition.STEERED, first.turn_id))
            self.assertEqual(len(self.codex.handles[0].steers), 1)
        else:
            with self.assertRaisesRegex(OrdinaryInputRejected, "正在执行"):
                await self.submit(binding)
        event = await self.finish(first)
        self.assertEqual((event.binding_id, event.thread_id, event.owner_id, event.origin, event.final_response),
                         (binding.id, first.thread_id, "owner", "origin", "answer"))
        with self.assertRaises(SteerRace if self.backend == "codex" else OrdinaryInputRejected):
            await self.submit(binding, admission=admission)
        self.assert_native_starts(1)

    async def test_failed_catch_up_commit_keeps_exact_result_and_closes_instance_admission(self):
        old = MessageContextAnchor("previous", 100)
        binding = self.new_binding("group", kind=ScopeKind.GROUP,
                                   message_context_mode=MentionContextMode.CATCH_UP, context_anchor=old)
        cursor = ContextCursorCommit(binding.context_revision, MessageContextAnchor("upper", 200))
        other = await self.ordinary.capture_submission_admission(self.binding.id)
        with patch.object(self.store, "commit_context_anchor", side_effect=OSError("CAS failed")) as save:
            if self.backend == "codex":
                with self.assertRaisesRegex(ContextBoundaryCommitFailed, "上下文边界未能持久化"):
                    await self.submit(binding, context_commit=cursor)
                rejected = RuntimeClosed
            else:
                accepted = await self.submit(binding, context_commit=cursor)
                self.clients.calls[0].init()
                origin, notice = await asyncio.wait_for(self.notices.get(), 2)
                self.assertEqual(origin, "origin")
                self.assertIn("上下文边界未能持久化", notice)
                accepted.release_receipt_attempt()
                rejected = OrdinaryInputRejected
            active = self.port.runtime_snapshot_exact(binding.id).turn
            self.assertIsNotNone(active)
            for target in (binding, self.binding):
                with self.assertRaises(rejected):
                    await self.ordinary.capture_submission_admission(target.id)
            with self.assertRaises(rejected):
                await self.submit(self.binding, admission=other)
            if self.backend == "claude":
                self.clients.calls[0].result()
            else:
                self.complete(active.turn_id)
            self.assertTrue(await self.runtime.wait_idle(2))
            save.assert_called_once_with(binding_id=binding.id, expected_context_revision=binding.context_revision,
                                         anchor=cursor.anchor)
        self.assertEqual(self.store.get(binding.id).context_anchor, old)
        event = await self.conclusion()
        self.assertEqual((event.binding_id, event.turn_id, event.status, event.final_response),
                         (binding.id, active.turn_id, "completed", "answer"))
        self.assertTrue(self.outcomes.empty())
        self.assert_native_starts(1)

    async def test_switch_away_and_back_invalidates_captured_input(self):
        second = self.new_binding()
        management = InstanceManagementService(
            bindings=self.store, projects=self.projects, runtime=self.port, scope_coordinator=ScopeCoordinator(),
        )
        self.addAsyncCleanup(management.close)

        async def activate(binding, previous):
            target = ExactBindingTarget(binding.scope_key, binding.id, previous.id)
            await management.activate_exact_binding(target=target)

        await activate(self.binding, second)
        captured = await self.ordinary.capture_submission_admission(self.binding.id)
        await activate(second, self.binding)
        await activate(self.binding, second)
        self.assertEqual(self.store.active_binding(self.binding.scope_key).id, self.binding.id)
        with self.assertRaises(SteerRace if self.backend == "codex" else OrdinaryInputRejected):
            await self.submit(self.binding, admission=captured)
        self.assert_native_starts(0)
        self.assertIsNone(self.store.get(self.binding.id).native_thread_id)
        await self.finish(await self.submit(self.binding))

    async def test_cancelled_feishu_receipt_releases_one_attributed_conclusion(self):
        message = FakeMessage("input", message_id="om_cancelled")
        receipt = await self.submit(self.binding, origin=message)
        started = asyncio.Event()

        async def held_reaction(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        async with channel_fixture(ordinary=self.ordinary) as fixture:
            with (patch.object(fixture.app, "_policy", BackendPolicy(self.backend, "owner")),
                  patch.object(fixture.app._reactions, "start", side_effect=held_reaction)):
                delivery = asyncio.create_task(fixture.app._present_ordinary_receipt(receipt, message))
                try:
                    await asyncio.wait_for(started.wait(), 2)
                    self.complete(receipt.turn_id)
                    if self.backend == "codex":
                        async with asyncio.timeout(2):
                            while self.runtime.active_turn(self.binding.id) is not None:
                                await asyncio.sleep(0)
                    else:
                        await asyncio.wait_for(self.clients.calls[0].closing.wait(), 2)
                    self.assertTrue(self.outcomes.empty())
                finally:
                    delivery.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await delivery
        self.assertTrue(await self.runtime.wait_idle(2))
        event = await self.conclusion()
        self.assertEqual((event.origin, event.turn_id, event.final_response), (message, receipt.turn_id, "answer"))
        self.assertTrue(self.outcomes.empty())
        self.assert_native_starts(1)

    async def test_old_exact_stop_cannot_interrupt_successor(self):
        first = await self.submit(self.binding)
        old = self.port.runtime_snapshot_exact(self.binding.id)
        self.assertEqual(old.turn.turn_id, first.turn_id)
        await self.finish(first)
        successor = await self.submit(self.binding)
        self.assertEqual(successor.thread_id, first.thread_id)
        self.assertNotEqual(successor.turn_id, first.turn_id)
        ack = AsyncMock()
        with self.assertRaises(ThreadActivityChanged):
            await self.ordinary.stop_exact(self.binding.id, acknowledge=ack,
                expected_activity_revision=old.activity_revision, expected_turn_id=old.turn.turn_id)
        ack.assert_not_awaited()
        if self.backend == "codex":
            self.assertEqual(self.codex.handles[-1].interrupt_count, 0)
        else:
            self.assertEqual(self.clients.calls[-1].interrupts, 0)
        await self.finish(successor)

    async def test_late_native_confirmation_allows_an_explicit_successor(self):
        first = await self.submit(self.binding)
        first.release_receipt_attempt()
        if self.backend == "claude":
            client = self.clients.calls[0]
            client.close_gate.clear()
            try:
                with patch("netizen_cli.runtime.claude_ordinary._CLOSE_SECONDS", 0):
                    self.complete(first.turn_id)
                    event = await self.conclusion()
                    self.assertEqual((event.turn_id, event.status), (first.turn_id, "completed"))
                    _, notice = await asyncio.wait_for(self.notices.get(), 2)
                    self.assertIn("无法确认 Claude 客户端已关闭", notice)
                    with self.assertRaisesRegex(OrdinaryInputRejected, "无法确认 Claude 客户端已关闭"):
                        await self.submit(self.binding)
            finally:
                client.close_gate.set()
        else:
            # Codex retains the shared client. Its stop receipt needs a later native Turn terminal.
            handle = self.codex.handles[0]
            handle.complete_on_interrupt = False
            snapshot = self.port.runtime_snapshot_exact(self.binding.id)
            disposition = await self.ordinary.stop_exact(self.binding.id, expected_activity_revision=snapshot.activity_revision,
                                                         expected_turn_id=first.turn_id)
            self.assertEqual(disposition, StopDisposition.REQUESTED)
            with self.assertRaises(ThreadStopping):
                await self.submit(self.binding)
            self.assertTrue(self.outcomes.empty())
            handle.complete(status="interrupted")
            event = await self.conclusion()
            self.assertEqual((event.turn_id, event.status), (first.turn_id, "interrupted"))
        self.assertTrue(await self.runtime.wait_idle(2))
        self.assert_native_starts(1)
        self.assertTrue(self.outcomes.empty())
        successor = await self.submit(self.binding)
        self.assertEqual(successor.thread_id, first.thread_id)
        self.assertNotEqual(successor.turn_id, first.turn_id)
        await self.finish(successor)

    async def test_two_bindings_run_in_the_same_directory_and_finish_independently(self):
        other = self.new_binding("other")
        async with asyncio.timeout(2):
            first, second = await asyncio.gather(self.submit(self.binding, origin="first"), self.submit(other, origin="second"))
        self.assertNotEqual(first.thread_id, second.thread_id)
        self.assert_native_starts(2)
        first.release_receipt_attempt()
        second.release_receipt_attempt()
        for binding, receipt, label in ((other, second, "second"), (self.binding, first, "first")):
            self.assertEqual(self.port.runtime_snapshot_exact(binding.id).turn.turn_id, receipt.turn_id)
            self.complete(receipt.turn_id, label)
            event = await self.conclusion()
            self.assertEqual((event.binding_id, event.thread_id, event.turn_id, event.cwd, event.origin, event.final_response),
                             (binding.id, receipt.thread_id, receipt.turn_id, self.cwd, label, label))
        self.assertTrue(await self.runtime.wait_idle(2))
        self.assertTrue(self.outcomes.empty())


class CodexOrdinaryPortContractTests(OrdinaryPortContract, unittest.IsolatedAsyncioTestCase):
    backend = "codex"


class ClaudeOrdinaryPortContractTests(OrdinaryPortContract, unittest.IsolatedAsyncioTestCase):
    backend = "claude"
