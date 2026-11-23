import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

from netizen_cli.bindings import BindingStore
from netizen_cli.domain import FeishuScope, MentionContextMode, MessageContextAnchor, ScopeKind
from netizen_cli.file_evidence import FileEvidence
from netizen_cli.ordinary_sessions import (
    ClaudeInputAnchor, ConversationTarget, DegradedTurn, LiveSubmissionUnknown, PhysicalAnswer, LogicalTerminal, OrdinaryConclusion, OrdinaryInputRejected, PreparedPrompt, PromptImage,
)
from netizen_cli.runtime.contracts import ContextCursorCommit, StopDisposition, TerminalStateUnknown, ThreadActivityChanged
from tests.support import has_claude_sdk, requires_claude_sdk

if has_claude_sdk:
    from netizen_cli.runtime.claude_client import BundledClaudeClients
    from netizen_cli.runtime.claude_ordinary import (
        CLOSE_FAILED, DEFAULT_MODE, DEGRADED, MODE_UNKNOWN, GRACE, START_FAILED, UNKNOWN, ClaudeOrdinarySessions,
    )
    from tests.support.claude_client import FakeClaudeClients


@requires_claude_sdk
class ClaudeOrdinaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cwd = Path(self.temp.name).resolve()
        self.store = BindingStore()
        self.addCleanup(self.store.close)
        self.store.bootstrap_project(alias="work", cwd=str(self.cwd))
        self.binding = self.new_binding()
        self.clients = FakeClaudeClients()
        self.now = 10.
        self.events = []
        self.all_events = []
        self.notices = []
        self.delivered = asyncio.Event()

        async def completion(event):
            self.all_events.append(event)
            if not isinstance(event, LogicalTerminal):
                self.events.append(event)
            self.delivered.set()

        async def notice(origin, text):
            self.notices.append((origin, text))

        self.runtime = ClaudeOrdinarySessions(bindings=self.store, clients=self.clients, owner_id="owner",
                                             on_completion=completion, notice=notice, clock=lambda: self.now)
        self.runtime.open_admission()
        self.now += 2.
        async def cleanup():
            if self.runtime._unavailable:
                with self.assertRaisesRegex(RuntimeError, "未能确认"):
                    await self.runtime.cancel_tasks()
            else:
                await self.runtime.cancel_tasks()
        self.addAsyncCleanup(cleanup)

    def new_binding(self, chat="chat"):
        return self.store.create_channel_binding(scope=FeishuScope("app", chat, ScopeKind.DIRECT),
                                                 project_alias="work", creator_id="owner")

    async def submit(self, binding=None, *, origin="origin", **kwargs):
        binding = self.store.get((binding or self.binding).id)
        admission = kwargs.pop("admission", None)
        if admission is None:
            admission = await self.runtime.capture_submission_admission(binding.id)
        params = dict(binding=binding, cwd=None if binding.native_thread_id else self.cwd,
                      input=PreparedPrompt("hello"), owner_id="owner", origin=origin, admission=admission,
                      context_commit=None, skill_names=())
        return await self.runtime.submit(**(params | kwargs))

    async def finish(self, receipt, client=None):
        client = client or self.clients.calls[-1]
        receipt.release_receipt_attempt()
        client.init()
        client.result()
        self.assertTrue(await self.runtime.wait_idle(1))

    async def wait_until(self, predicate):
        async with asyncio.timeout(15):
            while not predicate():
                await asyncio.sleep(.005)

    @staticmethod
    async def cron(cli, registered):
        await cli.live.stop_hook({"session_id": cli.session, "session_crons": [{"id": "cron"}] if registered else []},
                                 None, None)

    async def retain(self):
        h1 = await self.submit(origin="H1")
        h1.release_receipt_attempt()
        cli = self.clients.calls[-1]
        cli.init()
        await self.cron(cli, True)
        cli.result()
        await self.wait_until(lambda: cli.live.ordered_idle)
        return h1, cli

    async def test_unwritten_init_on_either_side_of_rollback_degrades_before_result(self):
        for rollback_first in (False, True):
            with self.subTest(rollback_first=rollback_first):
                _, cli = await self.retain()
                reserved, proceed = asyncio.Event(), asyncio.Event()
                write = self.runtime._write_next
                async def delayed(*args):
                    reserved.set()
                    await proceed.wait()
                    await write(*args)
                with patch.object(self.runtime, "_write_next", side_effect=delayed):
                    submit = asyncio.create_task(self.submit(origin="never written"))
                    await asyncio.wait_for(reserved.wait(), 5)
                    execution = self.runtime._active[self.binding.id]
                    h2 = execution.pending.input_id
                    if rollback_first:
                        await self.runtime.binding_pointer_changed(self.binding.id, None)
                        proceed.set()
                        with self.assertRaises(OrdinaryInputRejected):
                            await asyncio.wait_for(submit, 5)
                    cli.init(user_message_uuids=[h2])
                    await self.wait_until(lambda: execution.broken)
                    proceed.set()
                    cli.result(user_message_uuid=h2, result="wrong body")
                    with self.assertRaises(OrdinaryInputRejected):
                        await asyncio.wait_for(submit, 5)
                self.assertTrue(await self.runtime.wait_idle(5))
                self.assertIsInstance(self.events[-1], DegradedTurn)
                self.assertFalse(any(getattr(e, "final_response", None) == "wrong body" for e in self.events))
                self.assertEqual((len(cli.inputs), cli.closes), (1, 1))

    async def exercise_shutdown_queue(self, *, cancel_send):
        from claude_agent_sdk import SystemMessage
        _, cli = await self.retain()
        execution = self.runtime._active[self.binding.id]
        entered, release = asyncio.Event(), asyncio.Event()
        attempted = []
        async def deliver(event):
            if not isinstance(event, PhysicalAnswer):
                return
            attempted.append(event.turn_id)
            if len(attempted) == 1:
                entered.set()
                await release.wait()
                raise asyncio.CancelledError()
        with patch.object(self.runtime, "_completion", side_effect=deliver):
            for uid in ("cron-one", "cron-two"):
                cli.init(user_message_uuids=[])
                cli.result(uuid=uid, user_message_uuid=None)
                cli.stream.put_nowait(SystemMessage("session_state_changed", {"session_id": cli.session, "state": "idle"}))
            await asyncio.wait_for(entered.wait(), 5)
            await self.wait_until(lambda: "cron-two" in cli.live.seen)
            deadline = asyncio.get_running_loop().time() + (5 if cancel_send else .2)
            shutdown = asyncio.create_task(self.runtime.interrupt_all(deadline=deadline))
            await asyncio.wait_for(cli.closing.wait(), 5)
            if cancel_send:
                release.set()
            await asyncio.wait_for(shutdown, 5)
            self.assertFalse(any(not t.done() for t in execution.delivery.workers))
            release.set()
            self.assertEqual(attempted, ["cron-one", "cron-two"] if cancel_send else ["cron-one"])

    async def test_send_cancelled_during_shutdown_drains_following_body(self):
        await self.exercise_shutdown_queue(cancel_send=True)

    async def test_presentation_joined_at_service_deadline(self):
        await self.exercise_shutdown_queue(cancel_send=False)

    async def test_stream_ping_and_last_cron_removal_share_one_reader_wakeup(self):
        from claude_agent_sdk import StreamEvent

        _, cli = await self.retain()
        # Queue the pump first. The Stop callback wakes the consumer behind it,
        # so it reads both changes at the cut carrying the ping.
        cli.stream.put_nowait(StreamEvent("ping", cli.session, {}))
        await self.cron(cli, False)
        self.assertTrue(await self.runtime.wait_idle(10))
        self.assertEqual(cli.closes, 1)

    async def test_reservation_rollback_and_last_cron_removal_commute(self):
        for clear_first in (True, False):
            with self.subTest(clear_first=clear_first):
                h1, cli = await self.retain()
                reserved, proceed = asyncio.Event(), asyncio.Event()
                write = self.runtime._write_next
                async def delayed(*args):
                    reserved.set()
                    await proceed.wait()
                    await write(*args)
                with patch.object(self.runtime, "_write_next", side_effect=delayed):
                    submit = asyncio.create_task(self.submit(origin="never written"))
                    await asyncio.wait_for(reserved.wait(), 2)
                    self.assertIsNone(await self.runtime.preview_stop(self.binding))
                    await self.runtime.binding_pointer_changed(self.binding.id, None)
                    if clear_first:
                        await self.cron(cli, False)
                    proceed.set()
                    with self.assertRaisesRegex(OrdinaryInputRejected, "未发送"):
                        await asyncio.wait_for(submit, 2)
                    if not clear_first:
                        await self.cron(cli, False)
                self.assertTrue(await self.runtime.wait_idle(2))
                self.assertEqual((len(cli.inputs), cli.closes), (1, 1))
                self.assertIsNone(self.runtime.local_snapshot(self.binding.id).turn)
                self.assertEqual(self.runtime._activity[self.binding.id].last_execution_id, h1.turn_id)
                self.assertFalse(any(isinstance(e, DegradedTurn) for e in self.events))

    async def test_reader_failure_distinguishes_unwritten_reservation(self):
        from claude_agent_sdk import StreamEvent
        for malformed_uuid in (True, False):
            with self.subTest(malformed_uuid=malformed_uuid):
                before = len(self.events)
                h1, cli = await self.retain()
                reserved = asyncio.Event()
                async def never_wrote(*args):
                    reserved.set()
                    await asyncio.Future()
                with patch.object(self.runtime, "_write_next", side_effect=never_wrote):
                    submit = asyncio.create_task(self.submit(origin="H2"))
                    await asyncio.wait_for(reserved.wait(), 3)
                    cli.stream.put_nowait(StreamEvent("frame", cli.session, {}, user_message_uuid="") if malformed_uuid else None)
                    with self.assertRaisesRegex(OrdinaryInputRejected, "未发送"):
                        await asyncio.wait_for(submit, 3)
                self.assertTrue(await self.runtime.wait_idle(3))
                self.assertEqual([i["uuid"] for i in cli.inputs], [h1.turn_id])
                notice, = [e for e in self.events[before:] if isinstance(e, DegradedTurn)]
                self.assertEqual(notice.origin.source, "H1")
                self.assertTrue(set(notice.affected_inputs) <= {h1.turn_id})

    async def test_idle_with_any_outstanding_physical_or_pending_input_has_bounded_exit(self):
        from claude_agent_sdk import SystemMessage
        for current_init in (False, True):
            with self.subTest(current_init=current_init), patch("netizen_cli.runtime.claude_ordinary._TRANSITION_SECONDS", .04):
                h = await self.submit()
                h.release_receipt_attempt()
                cli = self.clients.calls[-1]
                if current_init:
                    cli.init()
                    cli.stream.put_nowait(SystemMessage("session_state_changed", {"session_id": cli.session, "state": "running"}))
                cli.stream.put_nowait(SystemMessage("session_state_changed", {"session_id": cli.session, "state": "idle"}))
                self.assertTrue(await self.runtime.wait_idle(3))
                self.assertEqual(cli.closes, 1)
                self.assertIsInstance(self.events[-1], DegradedTurn)
                self.assertEqual(self.events[-1].affected_inputs, (h.turn_id,))
                await self.runtime.capture_submission_admission(self.binding.id)

    async def test_anonymous_stream_event_cannot_accept_a_reserved_input(self):
        from claude_agent_sdk import StreamEvent, SystemMessage

        _, cli = await self.retain()
        reserved, proceed = asyncio.Event(), asyncio.Event()
        write = self.runtime._write_next
        async def delayed(*args):
            reserved.set()
            await proceed.wait()
            await write(*args)
        with patch.object(self.runtime, "_write_next", side_effect=delayed):
            submission = asyncio.create_task(self.submit(origin="H2"))
            await asyncio.wait_for(reserved.wait(), 3)
            human = self.runtime._active[self.binding.id].pending
            for kwargs in ({}, {"user_message_uuid": None}, {"user_message_uuid": human.input_id}):
                cli.stream.put_nowait(StreamEvent("frame", cli.session, {}, **kwargs))
            cli.stream.put_nowait(SystemMessage("session_state_changed", {"session_id": cli.session, "state": "running"}))
            await self.wait_until(lambda: cli.live.state == "running")
            self.assertFalse(human.ready.done() or human.proof.is_set() or human.written)
            self.assertEqual(len(cli.inputs), 1)
            proceed.set()
            with self.assertRaisesRegex(OrdinaryInputRejected, "未发送"):
                await asyncio.wait_for(submission, 3)
        cli.stream.put_nowait(None)
        self.assertTrue(await self.runtime.wait_idle(3))
        self.assertNotIn(human.input_id, self.events[-1].affected_inputs)

    async def assert_shutdown_during_settlement(self, *, uncertain_write):
        binding = self.new_binding("overlapping-shutdown")
        scope = self.store.get_scope(binding.scope_key).scope
        anchor = ClaudeInputAnchor(scope, "H1", None, AsyncMock())
        before = len(self.events)
        self.clients.created.clear()
        self.clients.configure = lambda c: setattr(c, "failure", "query" if uncertain_write else "connect")
        entered, release = asyncio.Event(), asyncio.Event()
        disconnect = self.runtime._disconnect
        async def gated(execution):
            entered.set()
            await release.wait()
            await disconnect(execution)
        with patch.object(self.runtime, "_disconnect", side_effect=gated):
            submit = asyncio.create_task(self.submit(binding, origin=anchor))
            await asyncio.wait_for(self.clients.created.wait(), 5)
            client = self.clients.calls[-1]
            if uncertain_write:
                await asyncio.wait_for(client.writing.wait(), 5)
                client.stream.put_nowait(None)
            await asyncio.wait_for(entered.wait(), 5)
            execution = self.runtime._active[binding.id]
            human = execution.human
            resolved = Mock()
            human.ready.add_done_callback(resolved)
            self.assertTrue(execution.ending)
            self.assertFalse(execution.closing or human.ready.done())
            # Reach forced cleanup while the ending owner is blocked before the SDK close.
            forced = asyncio.Event()
            begin = self.runtime._begin_client_cleanup
            def begin_cleanup():
                begin()
                forced.set()
            with (patch("netizen_cli.runtime.claude_ordinary._CONTROL_SECONDS", 0),
                  patch.object(self.runtime, "_begin_client_cleanup", side_effect=begin_cleanup)):
                shutdown = asyncio.create_task(self.runtime.interrupt_all() if uncertain_write else self.runtime.cancel_tasks())
                try:
                    await asyncio.wait_for(forced.wait(), 5)
                    self.assertFalse(await self.runtime.wait_idle(0))
                finally:
                    release.set()
                await asyncio.wait_for(shutdown, 5)
            try:
                self.assertTrue(await self.runtime.wait_idle(0))
                self.assertFalse(self.runtime._active)
                self.assertTrue(human.ready.done() and human.receipt.is_set() and anchor.retired)
                with self.assertRaises(LiveSubmissionUnknown if uncertain_write else OrdinaryInputRejected):
                    await asyncio.wait_for(submit, 5)
                resolved.assert_called_once_with(human.ready)
                self.assertEqual(anchor.stop_reactions.await_count, int(uncertain_write))
                self.assertEqual([type(e) for e in self.events[before:]], [DegradedTurn] if uncertain_write else [])
                self.assertEqual((len(client.inputs), client.closes), (int(uncertain_write), 1))
            finally:
                if not submit.done():
                    submit.cancel()
                await asyncio.gather(submit, return_exceptions=True)

    async def test_settlement_survives_uncertain_write_eof_and_shutdown(self):
        await self.assert_shutdown_during_settlement(uncertain_write=True)

    async def assert_contradictory_result_during_reservation(self):
        h1, cli = await self.retain()
        reserved, release = asyncio.Event(), asyncio.Event()
        write = self.runtime._write_next
        async def delayed(*args):
            reserved.set()
            await release.wait()
            await write(*args)
        scope = self.store.get_scope(self.binding.scope_key).scope
        anchor = ClaudeInputAnchor(scope, "H2", None, AsyncMock())
        with patch.object(self.runtime, "_write_next", side_effect=delayed):
            submit = asyncio.create_task(self.submit(origin=anchor))
            await asyncio.wait_for(reserved.wait(), 5)
            execution = self.runtime._active[self.binding.id]
            h2 = execution.pending
            resolved = Mock()
            h2.ready.add_done_callback(resolved)
            cli.result(uuid="contradiction", user_message_uuid=h2.input_id, result="wrong recipient")
            try:
                await self.wait_until(lambda: execution.broken or "contradiction" in cli.live.seen)
            finally:
                release.set()
            with self.assertRaisesRegex(OrdinaryInputRejected, "未发送"):
                await asyncio.wait_for(submit, 5)
        await self.wait_until(lambda: execution.broken or any(e.origin == "H2" for e in self.events))
        self.assertFalse(any(e.origin == "H2" for e in self.events))
        self.assertTrue(await self.runtime.wait_idle(5))
        notices = [e for e in self.events if isinstance(e, DegradedTurn)]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0].turn_id, h1.turn_id)
        self.assertNotIn(h2.input_id, notices[0].affected_inputs)
        self.assertTrue(anchor.retired and h2.receipt.is_set())
        self.assertFalse(h2.proof.is_set() or execution.humans or execution.delivery.queues)
        resolved.assert_called_once_with(h2.ready)
        anchor.stop_reactions.assert_not_awaited()
        self.assertEqual((len(cli.inputs), cli.closes), (1, 1))

    async def test_every_exit_settles_submission_delivery_cleanup_and_cursor_once(self):
        for exit_path in ("normal", "invalid-result", "invalid-state", "delivery-error", "cancel-physical",
                          "cancel-logical", "cancel-cleanup", "reservation-conflict", "eof", "shutdown"):
            with self.subTest(exit_path=exit_path):
                if exit_path == "reservation-conflict":
                    await self.assert_contradictory_result_during_reservation()
                    continue
                scope = FeishuScope("app", exit_path, ScopeKind.GROUP)
                binding = self.store.create_channel_binding(scope=scope, project_alias="work", creator_id="owner",
                    message_context_mode=MentionContextMode.CATCH_UP, context_anchor=MessageContextAnchor("old", 100))
                commit = ContextCursorCommit(binding.context_revision, MessageContextAnchor("new", 200))
                anchor = ClaudeInputAnchor(scope, "origin", None, AsyncMock())
                if exit_path == "cancel-cleanup":
                    anchor.stop_reactions.side_effect = asyncio.CancelledError()
                attempted = []
                async def deliver(event):
                    if isinstance(event, (DegradedTurn, PhysicalAnswer, LogicalTerminal)):
                        attempted.append(type(event))
                    if exit_path == "delivery-error" and isinstance(event, LogicalTerminal):
                        raise OSError()
                    if ((exit_path == "cancel-physical" and isinstance(event, PhysicalAnswer)) or
                            (exit_path == "cancel-logical" and isinstance(event, LogicalTerminal))):
                        raise asyncio.CancelledError()

                self.clients.configure = lambda c: setattr(c, "failure", "query" if exit_path == "invalid-result" else None)
                self.clients.created.clear()
                with (patch.object(self.runtime, "_completion", side_effect=deliver),
                      patch.object(self.store, "commit_context_anchor", wraps=self.store.commit_context_anchor) as save):
                    submit = asyncio.create_task(self.submit(binding, origin=anchor, context_commit=commit))
                    await asyncio.wait_for(self.clients.created.wait(), 3)
                    cli = self.clients.calls[-1]
                    await asyncio.wait_for(cli.writing.wait(), 3)
                    execution = self.runtime._active[binding.id]
                    human = execution.human
                    resolved = Mock()
                    human.ready.add_done_callback(resolved)
                    cli.init()
                    if exit_path == "invalid-result":
                        cli.result(reason="unrecognized", idle=False)
                        with self.assertRaises(LiveSubmissionUnknown):
                            await asyncio.wait_for(submit, 3)
                    else:
                        receipt = await asyncio.wait_for(submit, 3)
                        if exit_path != "shutdown":
                            receipt.release_receipt_attempt()
                        await self.wait_until(human.proof.is_set)
                        if exit_path == "eof":
                            cli.stream.put_nowait(None)
                        elif exit_path == "invalid-state":
                            cli.result(session_id="foreign")
                        elif exit_path == "shutdown":
                            # One input is active and another is settling its failed connect.
                            await self.assert_shutdown_during_settlement(uncertain_write=False)
                        else:
                            cli.result()
                    self.assertTrue(await self.runtime.wait_idle(5))
                    if exit_path in ("invalid-result", "eof", "invalid-state"):
                        self.assertEqual(attempted, [DegradedTurn])
                    elif exit_path == "shutdown":
                        self.assertEqual(attempted, [])
                    else:
                        self.assertEqual(attempted, [PhysicalAnswer, LogicalTerminal])
                    self.assertEqual(save.call_count, int(exit_path != "invalid-result"))
                    self.assertEqual(self.store.get(binding.id).context_anchor,
                                     binding.context_anchor if exit_path == "invalid-result" else commit.anchor)
                    self.assertIsNone(human.commit)
                    resolved.assert_called_once_with(human.ready)
                    self.assertTrue(human.receipt.is_set())
                    self.assertTrue(anchor.retired)
                    anchor.stop_reactions.assert_awaited_once_with(human.input_id)
                    self.assertFalse(execution.humans or execution.delivery.queues or execution.delivery.receipts or execution.delivery.cleanups)
                    self.assertEqual((len(cli.inputs), cli.closes), (1, 1))

    async def test_context_mode_change_after_write_supersedes_the_unconfirmed_cursor(self):
        binding = self.store.create_channel_binding(
            scope=FeishuScope("app", "group", ScopeKind.GROUP), project_alias="work", creator_id="owner",
            message_context_mode=MentionContextMode.CATCH_UP, context_anchor=MessageContextAnchor("old", 100))
        commit = ContextCursorCommit(binding.context_revision, MessageContextAnchor("new", 200))
        accepted = await self.submit(binding, context_commit=commit)
        current = await self.runtime.configure_context_exact(binding_id=binding.id,
            expected_settings_revision=binding.settings_revision, expected_context_revision=binding.context_revision,
            expected_feedback_revision=binding.feedback_revision, settings=None, task_feedback=binding.task_feedback,
            message_context_mode=MentionContextMode.CURRENT_ONLY, context_anchor=None)
        await self.finish(accepted)
        self.assertEqual(self.store.get(binding.id), current)
        self.assertEqual(current.context_revision, binding.context_revision + 1)
        self.assertIsNone(current.context_anchor)
        self.assertEqual(self.notices, [])
        await self.finish(await self.submit(self.binding))

    async def test_local_configuration_works_during_execution_and_cron_retention(self):
        h = await self.submit()
        h.release_receipt_attempt()
        cli = self.clients.calls[-1]
        cli.init()
        await self.cron(cli, True)
        for enabled in (False, True):
            current = self.store.get(self.binding.id)
            changed = await self.runtime.configure_context_exact(binding_id=current.id,
                expected_settings_revision=current.settings_revision, expected_feedback_revision=current.feedback_revision,
                expected_context_revision=current.context_revision, settings=None,
                task_feedback=replace(current.task_feedback, completion_mention_enabled=enabled),
                message_context_mode=MentionContextMode.CURRENT_ONLY, context_anchor=None)
            self.assertEqual(changed.task_feedback.completion_mention_enabled, enabled)
            if not enabled:
                cli.result()
                await self.wait_until(lambda: cli.live.ordered_idle)
        self.assertEqual(len(cli.inputs), 1)
        await self.cron(cli, False)
        self.assertTrue(await self.runtime.wait_idle(2))

    async def test_service_shutdown_disconnects_retained_clients_without_waiting_for_idle(self):
        from claude_agent_sdk import SystemMessage
        _, cron_client = await self.retain()
        h2 = await self.submit(self.new_binding("shell"))
        h2.release_receipt_attempt()
        shell = self.clients.calls[-1]
        shell.init()
        shell.stream.put_nowait(SystemMessage("background_tasks_changed", {
            "session_id": shell.session, "tasks": [{"task_id": "shell", "run_id": "r"}]}))
        shell.result()
        await self.wait_until(lambda: shell.live.ordered_idle)
        await asyncio.wait_for(self.runtime.interrupt_all(), 2)
        self.assertEqual([(c.closes, c.interrupts) for c in (cron_client, shell)], [(1, 0), (1, 0)])
        self.assertFalse(any(isinstance(e, DegradedTurn) for e in self.events))

    async def test_slow_conversation_queue_of_1100_bodies_cancels_iteratively(self):
        from claude_agent_sdk import SystemMessage
        _, cli = await self.retain()
        rendering = asyncio.Event()
        async def slow(event):
            if isinstance(event.origin, ConversationTarget):
                rendering.set()
                await asyncio.Future()
        self.runtime._completion = slow
        cli.init(user_message_uuids=[])
        for i in range(1100):
            cli.result(result=f"body {i}", user_message_uuid=None, idle=False)
        await asyncio.wait_for(rendering.wait(), 3)
        await self.wait_until(lambda: len(cli.live.seen) == 1101)
        await self.cron(cli, False)
        cli.stream.put_nowait(SystemMessage("session_state_changed", {"session_id": cli.session, "state": "idle"}))
        await asyncio.wait_for(cli.closing.wait(), 3)
        # Exercise the forced-cancellation phase without waiting the full production drain budget.
        with patch("netizen_cli.runtime.claude_ordinary._CLOSE_SECONDS", .05):
            await asyncio.wait_for(self.runtime.cancel_tasks(), 3)
        self.assertTrue(await self.runtime.wait_idle(1))
        await self.wait_until(lambda: not self.runtime._tasks and not self.runtime._closes)

    async def test_unowned_body_does_not_wait_for_h2_receipt_or_consume_its_notice(self):
        _, cli = await self.retain()
        h2 = await self.submit(origin="H2")
        cli.init(mode="default")
        cli.result(result="human")
        cli.init(user_message_uuids=[])
        cli.result(user_message_uuid=None, result="cron")
        await self.wait_until(lambda: any(isinstance(e.origin, ConversationTarget) for e in self.events))
        self.assertFalse(self.notices)
        self.assertFalse(any(e.origin == "H2" for e in self.events))
        h2.release_receipt_attempt()
        await self.wait_until(lambda: self.notices)
        self.assertEqual(self.notices, [("H2", DEFAULT_MODE)])
        await self.cron(cli, False)
        from claude_agent_sdk import SystemMessage
        cli.stream.put_nowait(SystemMessage("session_state_changed", {"session_id": cli.session, "state": "idle"}))
        self.assertTrue(await self.runtime.wait_idle(2))

    async def test_all_native_frames_share_one_pump_task(self):
        from claude_agent_sdk import SystemMessage
        callers = set()
        def configure(cli):
            original = cli.receive_messages
            async def capture():
                async for message in original():
                    callers.add(asyncio.current_task())
                    yield message
            cli.receive_messages = capture
        self.clients.configure = configure
        receipt = await self.submit()
        cli = self.clients.calls[-1]
        cli.init()
        for _ in range(30):
            cli.stream.put_nowait(SystemMessage("session_state_changed", {"session_id": cli.session, "state": "running"}))
        cli.result()
        receipt.release_receipt_attempt()
        self.assertTrue(await self.runtime.wait_idle(2))
        self.assertEqual(len(callers), 1)

    async def test_foreign_or_malformed_task_inventory_cannot_complete_the_turn(self):
        from claude_agent_sdk import SystemMessage
        for data in ({"session_id": "foreign", "tasks": []}, {"tasks": [None]},
                     {"tasks": [{"task_id": "a", "run_id": "same"}, {"task_id": "b", "run_id": "same"}]}):
            receipt = await self.submit()
            client = self.clients.calls[-1]
            client.init()
            client.stream.put_nowait(SystemMessage("background_tasks_changed", {"session_id": client.session} | data))
            client.result(result="wrong answer")
            receipt.release_receipt_attempt()
            self.assertTrue(await self.runtime.wait_idle(2))
            self.assertNotIsInstance(self.events[-1], OrdinaryConclusion)
            self.assertEqual(client.consumers, 1)
            self.assertEqual(client.closes, 1)

    async def test_idle_binding_reuses_its_client_and_routes_a_late_wake_to_h1(self):
        from claude_agent_sdk import AssistantMessage, SystemMessage, ToolUseBlock
        h1 = await self.submit(origin="first")
        h1.release_receipt_attempt()
        cli = self.clients.calls[0]
        cli.init(user_message_uuids=[h1.turn_id])
        cli.stream.put_nowait(AssistantMessage([ToolUseBlock("tool", "Bash", {})], "model", session_id=cli.session,
                                               user_message_uuid=h1.turn_id))
        def frame(subtype, **data):
            cli.stream.put_nowait(SystemMessage(subtype, {"session_id": cli.session, **data}))
        frame("background_tasks_changed", tasks=[{"task_id": "task", "run_id": "run"}])
        frame("task_started", task_id="task", run_id="run", tool_use_id="tool")
        cli.result(result="H1 body")
        frame("session_state_changed", state="idle")
        async with asyncio.timeout(2):
            while not self.events:
                await asyncio.sleep(.01)
        h2 = await self.submit(origin="second")
        h2.release_receipt_attempt()
        self.assertEqual(len(self.clients.calls), 1)
        cli.init(user_message_uuids=[h2.turn_id])
        frame("session_state_changed", state="running")
        frame("background_tasks_changed", tasks=[])
        cli.result(result="H2 body", user_message_uuid=h2.turn_id, idle=False)
        cli.init(user_message_uuids=[])
        cli.result(result="wake A", user_message_uuid=None, origin={"kind":"task-notification", "producer":"session-task", "runId":"run"})
        frame("session_state_changed", state="idle")
        self.assertTrue(await self.runtime.wait_idle(2))
        self.assertEqual([(e.origin,e.final_response) for e in self.events if isinstance(e, OrdinaryConclusion)],
                         [("first","H1 body"),("second","H2 body"),("first","wake A")])
        self.assertEqual([e.turn_id for e in self.all_events if isinstance(e, LogicalTerminal)], [h1.turn_id, h2.turn_id])
        self.assertEqual([type(e) for e in self.all_events[-2:]], [LogicalTerminal, LogicalTerminal])
        self.assertEqual((cli.consumers, cli.closes), (1, 1))

    async def test_timer_only_bounds_empty_unconfirmed_native_transition(self):
        from claude_agent_sdk import SystemMessage
        receipt = await self.submit()
        receipt.release_receipt_attempt()
        cli = self.clients.calls[0]
        def frame(kind, **data):
            cli.stream.put_nowait(SystemMessage(kind, {"session_id": cli.session, **data}))
        with patch("netizen_cli.runtime.claude_ordinary._TRANSITION_SECONDS", .25):
            cli.init()
            frame("background_tasks_changed", tasks=[{"task_id": "A", "run_id": "runA"}])
            cli.result(result="first")
            await asyncio.wait_for(self.delivered.wait(), 2)
            self.now += 36000
            self.assertFalse(await self.runtime.wait_idle(.35))
            await self.runtime.capture_submission_admission(self.binding.id)
            self.assertFalse(cli.closing.is_set())
            # Natural removal invalidates the earlier idle. Positive running
            # evidence disarms the deadline for the model's own latency.
            frame("background_tasks_changed", tasks=[])
            frame("session_state_changed", state="running")
            cli.init(user_message_uuids=[])
            async with asyncio.timeout(2):
                while cli.live.physical is None:
                    await asyncio.sleep(.002)
            self.assertFalse(await self.runtime.wait_idle(.35))
            self.assertFalse(cli.closing.is_set())
            with self.assertRaises(OrdinaryInputRejected):
                await self.submit()
            cli.result(user_message_uuid=None, result="unowned", idle=False)
            self.assertTrue(await self.runtime.wait_idle(2))
        self.assertEqual(len([e for e in self.events if isinstance(e, DegradedTurn)]), 1)
        self.assertEqual(cli.closes, 1)

    async def test_close_rechecks_cron_and_bodyless_receipts_do_not_render(self):
        from claude_agent_sdk import SystemMessage
        receipt = await self.submit()
        receipt.release_receipt_attempt()
        cli = self.clients.calls[0]
        disconnect = self.runtime._disconnect
        async def register_before_disconnect(execution):
            self.assertFalse(execution.closing)
            if not execution.ending and not cli.live.crons and not getattr(cli, "registered", False):
                cli.registered = True
                await cli.live.stop_hook({"session_id": cli.session, "session_crons": [{"id": "cron"}]}, None, None)
            await disconnect(execution)
        async def state_idle():
            cli.stream.put_nowait(SystemMessage("session_state_changed", {"session_id": cli.session, "state": "idle"}))
            async with asyncio.timeout(30):
                while (not cli.stream.empty() or not cli.live.ordered_idle or not cli.live.crons
                       or self.runtime._active[self.binding.id].closing):
                    await asyncio.sleep(.002)
        with patch.object(self.runtime, "_disconnect", side_effect=register_before_disconnect):
            cli.init()
            cli.result(result=None, reason=None)
            await state_idle()
            self.assertEqual(cli.closes, 0)
            await asyncio.wait_for(self.delivered.wait(), 2)
            self.assertEqual([type(e) for e in self.all_events], [LogicalTerminal])
            h2 = await self.submit(origin="second")
            h2.release_receipt_attempt()
            cli.init()
            cli.result(result="H2 only")
            await state_idle()
            # A recurring cron has no human origin. Its wake uses the same
            # conversation, including after thousands of duplicate receipts.
            cli.init(user_message_uuids=[])
            for i in range(10000):
                cli.result(reason=None, result=None, user_message_uuid=None, uuid=str(UUID(int=i)), idle=False)
            cli.result(reason=None, result=None, user_message_uuid=None, uuid=str(UUID(int=0)), idle=False)
            cli.result(result="cron", user_message_uuid=None, idle=False)
            await state_idle()
            self.assertEqual(len(cli.live.seen), 10003)
            self.assertFalse(cli.live.tool_owner or cli.live.run_tool)
            await cli.live.stop_hook({"session_id": cli.session, "session_crons": []}, None, None)
            cli.stream.put_nowait(SystemMessage("session_state_changed", {"session_id": cli.session, "state": "idle"}))
            self.assertTrue(await self.runtime.wait_idle(3))
        bodies = [(e.origin, e.final_response) for e in self.events if isinstance(e, OrdinaryConclusion)]
        self.assertEqual(bodies[0], ("second", "H2 only"))
        self.assertIsInstance(bodies[1][0], ConversationTarget)
        self.assertEqual([body for _, body in bodies], ["H2 only", "cron"])
        self.assertEqual((len(self.clients.calls), len(cli.inputs), cli.closes), (1, 2, 1))

    async def test_lost_h2_write_uses_same_reader_and_reserves_until_proven(self):
        h1 = await self.submit(origin="first")
        h1.release_receipt_attempt()
        cli = self.clients.calls[0]
        cli.init()
        await cli.live.stop_hook({"session_id": cli.session, "session_crons": [{"id": "cron"}]}, None, None)
        cli.result()
        async with asyncio.timeout(3):
            while not cli.live.ordered_idle:
                await asyncio.sleep(.002)
        cli.failure = "query"
        h2 = asyncio.create_task(self.submit(origin="second"))
        async with asyncio.timeout(3):
            while len(cli.inputs) < 2:
                await asyncio.sleep(.002)
        with self.assertRaises(OrdinaryInputRejected):
            await self.submit(origin="contender")
        cli.init()
        cli.result(result="confirmed once")
        accepted = await asyncio.wait_for(h2, 3)
        accepted.release_receipt_attempt()
        async with asyncio.timeout(3):
            while len([e for e in self.all_events if isinstance(e, LogicalTerminal)]) != 2:
                await asyncio.sleep(.002)
        await cli.live.stop_hook({"session_id": cli.session, "session_crons": []}, None, None)
        self.assertTrue(await self.runtime.wait_idle(3))
        self.assertEqual([(e.origin, e.final_response) for e in self.events], [("first", "answer"), ("second", "confirmed once")])
        self.assertEqual((len(cli.inputs), cli.consumers, cli.closes), (2, 1, 1))

    def test_stop_confirmation_only_preserves_an_existing_fresh_idle(self):
        from claude_agent_sdk import SystemMessage
        from netizen_cli.runtime.claude_client import LiveSession
        for natural_removal in (False, True):
            live = LiveSession("session", self.fail)
            def frame(kind, **data):
                live.system(SystemMessage(kind, {"session_id": live.session, **data}))
            tasks = [{"task_id": "A", "run_id": "runA"}, {"task_id": "B", "run_id": "runB"}]
            frame("background_tasks_changed", tasks=tasks)
            frame("session_state_changed", state="idle")
            live.stop_requested.update(["B"] if natural_removal else ["A", "B"])
            frame("task_notification", task_id="A", status="stopped")
            frame("background_tasks_changed", tasks=tasks[1:])
            frame("task_updated", task_id="B", patch={"status":"killed"})
            frame("background_tasks_changed", tasks=[])
            self.assertEqual(live.ordered_idle, not natural_removal)
            self.assertFalse(live.stop_requested or live.stopped)

    def test_wake_tools_and_nested_agent_tasks_keep_the_physical_launcher(self):
        from claude_agent_sdk import AssistantMessage, SystemMessage, ToolUseBlock, ResultMessage
        from netizen_cli.runtime.claude_client import LiveSession
        live = LiveSession("session", self.fail)
        def init(ids):
            live.init(SystemMessage("init", {"uuid": "turn", "user_message_uuids": ids}))
        def tool(name, parent=None, human=None):
            live.assistant(AssistantMessage([ToolUseBlock(name, "Agent", {})], "model",
                           parent_tool_use_id=parent, user_message_uuid=human))
        def started(run, tool_id):
            live.system(SystemMessage("task_started", {"task_id": run, "run_id": run, "tool_use_id": tool_id}))
        def result(human, run=None):
            origin = {"kind": "task-notification", "producer": "session-task", "runId": run} if run else None
            return ResultMessage("success", 1, 1, False, 1, "session", user_message_uuid=human, origin=origin)
        init(["H1"])
        tool("first", human="H1")
        started("A", "first")
        self.assertEqual(live.route(result("H1"))[0], "H1")
        init([])
        tool("child-agent")
        tool("nested", parent="child-agent")
        started("B", "nested")
        # One merged body goes to H2. The newly launched task belongs to A's wake.
        self.assertEqual(live.route(result("H2", "A"))[0], "H2")
        live.system(SystemMessage("background_tasks_changed", {"tasks": [{"task_id": "B", "run_id": "B"}]}))
        self.assertTrue(live.has_work("H1"))
        self.assertFalse(live.has_work("H2"))
        init([])
        self.assertEqual(live.route(result(None, "B"))[0], "H1")
        init(["H1"])
        with self.assertRaises(ValueError):
            tool("wrong-tool", human="H2")
        with self.assertRaises(ValueError):
            live.route(result("H2"))
        init([])
        tool("named", human="H1")
        with self.assertRaises(ValueError):
            live.route(result("H2"))

    async def test_missing_error_body_still_delivers_safe_failure(self):
        for subtype, reason, text in (("error_max_turns", "max_turns", "轮数"),
                                      ("error_during_execution", "private", "unknown"),
                                      ("error_max_budget_usd", None, "预算")):
            receipt = await self.submit()
            receipt.release_receipt_attempt()
            cli = self.clients.calls[-1]
            cli.init()
            cli.result(subtype, reason, True, result=None)
            self.assertTrue(await self.runtime.wait_idle(2))
            self.assertIn(text, self.events[-1].error_detail)

    async def test_closed_grace_and_owner_gate_precede_native_calls(self):
        for self.now in (9., 11.999):
            with self.assertRaisesRegex(OrdinaryInputRejected, GRACE):
                await self.submit()
        self.now = 12.
        with self.assertRaisesRegex(OrdinaryInputRejected, "所有者"):
            await self.submit(owner_id="other")
        with self.assertRaisesRegex(OrdinaryInputRejected, "Skill"):
            await self.submit(skill_names=("unknown",))
        self.assertEqual(self.clients.calls, [])
        self.runtime.close_admission()
        self.runtime.open_admission()
        with self.assertRaisesRegex(OrdinaryInputRejected, "关闭"):
            await self.submit()

    async def test_exact_uuid_native_resume_cwd_and_receipt_handoff(self):
        stale = await self.runtime.capture_submission_admission(self.binding.id)
        prompt = PreparedPrompt("final text", (PromptImage("first image", "data:image/png;base64,AAAA"),))
        first = await self.submit(input=prompt)
        client = self.clients.calls[0]
        self.assertFalse(client.resume)
        self.assertEqual(str(UUID(first.thread_id)), self.store.get(self.binding.id).native_thread_id)
        self.assertEqual(client.inputs[0]["uuid"], first.turn_id)
        blocks = client.inputs[0]["message"]["content"]
        self.assertEqual([i["type"] for i in blocks], ["text", "image", "text"])
        self.assertEqual(blocks[1]["source"], {"type": "base64", "media_type": "image/png", "data": "AAAA"})
        client.init()
        client.result()
        await asyncio.wait_for(client.closing.wait(), 1)
        self.assertEqual(self.events, [])
        first.release_receipt_attempt()
        self.assertTrue(await self.runtime.wait_idle(1))
        event = self.events.pop()
        self.assertEqual((event.thread_id, event.turn_id, event.final_response), (first.thread_id, first.turn_id, "answer"))
        self.assertEqual((event.partial_answers, event.activity.turn_id, event.file_evidence()), ((), event.turn_id, FileEvidence()))
        with self.assertRaises(OrdinaryInputRejected):
            await self.submit(admission=stale)
        other = self.cwd / "native-cwd"
        other.mkdir()
        self.clients.cwds[first.thread_id] = other
        second = await self.submit()
        client = self.clients.calls[-1]
        self.assertTrue(client.resume)
        self.assertEqual((client.cwd, client.session), (other, first.thread_id))
        self.assertNotEqual(second.turn_id, first.turn_id)
        await self.finish(second)
        del self.clients.cwds[first.thread_id]
        with self.assertRaisesRegex(OrdinaryInputRejected, "实际目录"):
            await self.submit()
        self.assertEqual(len(self.clients.calls), 2)

    async def test_terminal_uses_all_three_fields_and_never_renders_raw_diagnostics(self):
        for subtype, reason, is_error, status in (
            ("success", "completed", False, "completed"), ("error_during_execution", "aborted_streaming", True, "interrupted"),
            ("error_during_execution", "aborted_tools", True, "interrupted"), ("error_max_turns", "max_turns", True, "failed"),
            ("success", "api_error", True, "failed"), ("success", "api_error", False, None),
            ("unexpected", "completed", False, None),
            ("error_max_budget_usd", None, True, "failed"),
            ("error_during_execution", "private raw secret", True, "failed"),
            ("error_during_execution", "api_error", False, None),
        ):
            with self.subTest(reason=reason, status=status):
                receipt = await self.submit()
                receipt.release_receipt_attempt()
                client = self.clients.calls[-1]
                client.init()
                client.result(subtype, reason, is_error, result="answer" if status=="completed" else "raw secret",
                              api_error_status=429, errors=["raw secret"])
                self.assertTrue(await self.runtime.wait_idle(1))
                event = self.events[-1]
                self.assertEqual(getattr(event, "status", None), status)
                self.assertNotIn("raw secret", repr(event))
                if status is None:
                    self.assertEqual(str(event.error), DEGRADED)
                if reason == "private raw secret":
                    self.assertEqual(event.error_detail, "Claude 执行失败（error_during_execution/unknown）。")
                if reason == "api_error" and status == "failed":
                    self.assertIn("429", event.error_detail)

    async def test_permissions_are_read_only_from_exact_init(self):
        for mode in ("default", "bypassPermissions", None):
            with self.subTest(mode=mode):
                receipt = await self.submit()
                receipt.release_receipt_attempt()
                client = self.clients.calls[-1]
                client.init(mode)
                client.init(mode)
                client.result()
                self.assertTrue(await self.runtime.wait_idle(1))
                if mode == "default":
                    self.assertEqual(self.notices, [("origin", DEFAULT_MODE)])
                    self.assertEqual(client.interrupts, 0)
                else:
                    self.assertEqual(client.interrupts, 1)
                    self.assertIn(MODE_UNKNOWN, str(self.events[-1].error))

    async def test_permission_notice_precedes_only_its_attributed_result(self):
        denied = [{"tool_name": "Bash", "tool_input": {"command": "private-command"}},
                  {"tool_name": "Write", "tool_input": {"file_path": "/private/path"}}]
        summary = "本次有 2 个操作被 Claude Code 的权限设置拒绝（Bash、Write）。如需允许，请在 Claude Code 设置中调整权限规则。"
        seen_at_completion = []
        async def complete(event):
            if isinstance(event, OrdinaryConclusion):
                seen_at_completion.append((event.final_response, list(self.notices)))
        self.runtime._completion = complete

        for mode, denials, expected in (
            ("auto", denied, [summary]),
            ("default", denied, [DEFAULT_MODE + "\n" + summary]),
            ("auto", [{"tool_name": name} for name in ("Bash", "Bash", "Write", "Edit", "Read", "Glob", "Grep", "Agent")],
             ["本次有 8 个操作被 Claude Code 的权限设置拒绝（Bash、Write、Edit、Read、Glob、Grep）。如需允许，请在 Claude Code 设置中调整权限规则。"]),
            ("auto", None, []), ("auto", [], []), ("auto", "private-command", []),
            ("auto", [{"tool_name": []}, {"tool_name": "/private/path"}, None],
             ["本次有 3 个操作被 Claude Code 的权限设置拒绝。如需允许，请在 Claude Code 设置中调整权限规则。"]),
        ):
            with self.subTest(mode=mode, denials=denials):
                self.notices.clear()
                seen_at_completion.clear()
                receipt = await self.submit()
                client = self.clients.calls[-1]
                client.init(mode, user_message_uuids=[])
                client.result(user_message_uuid="foreign", permission_denials=denied)
                client.result(user_message_uuid=None, origin={"kind": "task-notification"}, permission_denials=denied)
                client.init(mode)
                client.result(permission_denials=denials, uuid="result")
                client.result(permission_denials=denied, uuid="result")
                receipt.release_receipt_attempt()
                self.assertTrue(await self.runtime.wait_idle(1))
                self.assertEqual(seen_at_completion, [("answer", [("origin", text) for text in expected])])
                self.assertEqual(self.notices, seen_at_completion[0][1])

    async def test_stream_end_subsumes_pending_permission_notice(self):
        receipt = await self.submit()
        receipt.release_receipt_attempt()
        client = self.clients.calls[-1]
        client.init("default")
        client.stream.put_nowait(None)
        self.assertTrue(await self.runtime.wait_idle(1))
        self.assertEqual(self.notices, [])
        self.assertEqual(str(self.events[-1].error), DEGRADED)

    async def test_foreign_result_can_precede_our_attributed_result_and_duplicate_is_not_delivered(self):
        receipt = await self.submit()
        client = self.clients.calls[-1]
        client.result(user_message_uuid="other", result="foreign")
        client.init()
        client.result(uuid="same", result="ours", idle=False)
        client.result(uuid="same", result="ours")
        receipt.release_receipt_attempt()
        self.assertTrue(await self.runtime.wait_idle(1))
        self.assertEqual([event.final_response for event in self.events], ["ours"])
        self.assertEqual((client.consumers, client.closes, len(client.inputs)), (1, 1, 1))

    async def test_unknown_releases_only_after_close_and_never_replays(self):
        for cause in ("absent-id", "foreign-session", "process", "stream-end", "wrong-init-cwd"):
            with self.subTest(cause=cause):
                receipt = await self.submit()
                receipt.release_receipt_attempt()
                client = self.clients.calls[-1]
                client.close_gate.clear()
                if cause == "wrong-init-cwd":
                    client.init(cwd="wrong")
                else:
                    client.init()
                if cause == "absent-id":
                    client.result(user_message_uuid=None, uuid=receipt.turn_id)
                elif cause == "foreign-session":
                    client.result(session_id="another")
                elif cause == "process":
                    client.stream.put_nowait(OSError("secret"))
                client.stream.put_nowait(None)
                await asyncio.wait_for(client.closing.wait(), 1)
                with self.assertRaisesRegex(OrdinaryInputRejected, "正在收尾"):
                    await self.submit()
                client.close_gate.set()
                self.assertTrue(await self.runtime.wait_idle(1))
                self.assertEqual(str(self.events[-1].error), DEGRADED)
                self.assertEqual(len(client.inputs), 1)
        self.assertEqual(len(self.events), 5)
        self.assertEqual(len({client.session for client in self.clients.calls}), 1)

    async def test_start_and_query_failures_have_distinct_receipts(self):
        self.clients.configure = lambda c: setattr(c, "failure", "connect")
        with self.assertRaisesRegex(OrdinaryInputRejected, START_FAILED):
            await self.submit()
        self.assertIsNone(self.store.get(self.binding.id).native_thread_id)
        self.assertEqual(self.clients.calls[-1].closes, 1)
        self.clients.configure = lambda c: setattr(c, "failure", "query")
        with self.assertRaisesRegex(TerminalStateUnknown, UNKNOWN):
            await self.submit()
        self.assertIsNotNone(self.store.get(self.binding.id).native_thread_id)
        self.assertEqual(len(self.clients.calls[-1].inputs), 1)
        self.assertTrue(await self.runtime.wait_idle(2))
        self.assertEqual([str(e.error) for e in self.events], [DEGRADED])
        self.clients.configure = None
        await self.finish(await self.submit())

    async def test_close_failure_quarantines_only_that_binding(self):
        receipt = await self.submit()
        receipt.release_receipt_attempt()
        client = self.clients.calls[-1]
        client.failure = "close"
        client.stream.put_nowait(None)
        self.assertFalse(await self.runtime.wait_idle(1))
        with self.assertRaisesRegex(OrdinaryInputRejected, CLOSE_FAILED):
            await self.submit()
        self.assertEqual(self.notices[-1][1], CLOSE_FAILED)
        other = self.new_binding("other")
        second = await self.submit(other)
        self.clients.calls[-1].init()
        self.clients.calls[-1].result()
        second.release_receipt_attempt()
        await self.runtime.wait_idle(1)
        self.assertIsInstance(self.events[-1], OrdinaryConclusion)

    async def test_stop_while_opening_and_cancellation_during_write(self):
        self.clients.configure = lambda c: c.connect_gate.clear()
        task = asyncio.create_task(self.submit())
        await asyncio.wait_for(self.clients.created.wait(), 1)
        first = self.clients.calls[-1]
        with self.assertRaisesRegex(OrdinaryInputRejected, "正在执行"):
            await self.submit()
        preview = await self.runtime.preview_stop(self.binding)
        await self.runtime.stop_exact(self.binding.id, expected_activity_revision=preview.activity_revision,
                                      expected_turn_id=preview.turn_id)
        with self.assertRaisesRegex(OrdinaryInputRejected, "取消"):
            await task
        self.assertEqual((first.inputs, first.closes), ([], 1))
        self.clients.created.clear()
        self.clients.configure = lambda c: c.write_gate.clear()
        task = asyncio.create_task(self.submit())
        await asyncio.wait_for(self.clients.created.wait(), 1)
        second = self.clients.calls[-1]
        await asyncio.wait_for(second.writing.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        second.write_gate.set()
        second.init()
        second.result()
        self.assertTrue(await self.runtime.wait_idle(1))
        self.assertEqual(len(self.events), 1)
        self.assertEqual(len(second.inputs), 1)

    async def test_cursor_failure_closes_instance_admission_and_preserves_accepted_result(self):
        old = MessageContextAnchor("old", 100)
        binding = self.store.create_channel_binding(
            scope=FeishuScope("app", "group", ScopeKind.GROUP), project_alias="work", creator_id="owner",
            message_context_mode=MentionContextMode.CATCH_UP, context_anchor=old,
        )
        commit = ContextCursorCommit(binding.context_revision, MessageContextAnchor("new", 200))
        prepared = await self.runtime.capture_submission_admission(self.binding.id)
        with patch.object(self.store, "commit_context_anchor", side_effect=OSError("disk failure")) as save:
            first = await self.submit(binding, context_commit=commit)
            save.assert_not_called()
            client = self.clients.calls[-1]
            client.result(user_message_uuid="other")
            client.init()
            client.result()
            await asyncio.wait_for(client.closing.wait(), 1)
            save.assert_called_once_with(binding_id=binding.id, expected_context_revision=binding.context_revision,
                                         anchor=commit.anchor)
            self.assertEqual(self.store.get(binding.id).context_anchor, old)
            self.assertEqual(len(self.notices), 1)
            origin, notice = self.notices[0]
            self.assertEqual(origin, "origin")
            self.assertIn("已接收", notice)
            self.assertIn("上下文边界未能持久化", notice)
            self.assertEqual(self.events, [])
            with self.assertRaisesRegex(OrdinaryInputRejected, "上下文边界未能持久化"):
                await self.submit(self.binding, admission=prepared)
            first.release_receipt_attempt()
            self.assertTrue(await self.runtime.wait_idle(1))
            self.assertEqual([(e.turn_id, e.final_response, e.status) for e in self.events],
                             [(first.turn_id, "answer", "completed")])
            for target in (binding, self.binding):
                with self.assertRaisesRegex(OrdinaryInputRejected, "上下文边界未能持久化"):
                    await self.submit(target)
            self.assertEqual(len(self.clients.calls), 1)
            self.assertEqual(len(client.inputs), 1)

    async def test_failed_write_init_and_injected_result_do_not_advance_group_cursor(self):
        from claude_agent_sdk import ResultMessage
        old = MessageContextAnchor("old", 100)
        binding = self.store.create_channel_binding(scope=FeishuScope("app", "group", ScopeKind.GROUP),
            project_alias="work", creator_id="owner", message_context_mode=MentionContextMode.CATCH_UP, context_anchor=old)
        async def reject(_prompt):
            raise OSError("before intake")
        self.clients.configure = lambda client: setattr(client, "query", reject)
        submit = asyncio.create_task(self.submit(binding, context_commit=ContextCursorCommit(
            binding.context_revision, MessageContextAnchor("new", 200))))
        await asyncio.wait_for(self.clients.created.wait(), 1)
        client = self.clients.calls[-1]
        client.init()
        client.stream.put_nowait(ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id=client.session, origin={"kind": "task-notification"}))
        client.stream.put_nowait(None)
        with self.assertRaises(TerminalStateUnknown):
            await submit
        self.assertFalse(client.inputs)
        self.assertEqual(self.store.get(binding.id).context_anchor, old)

    async def test_startup_stop_preview_survives_write_and_cancelled_interrupt_propagates(self):
        self.clients.configure = lambda c: c.connect_gate.clear()
        submit = asyncio.create_task(self.submit())
        await asyncio.wait_for(self.clients.created.wait(), 1)
        client = self.clients.calls[-1]
        preview = await self.runtime.preview_stop(self.binding)
        fields = dict(expected_activity_revision=preview.activity_revision, expected_turn_id=preview.turn_id)
        client.connect_gate.set()
        receipt = await submit
        receipt.release_receipt_attempt()
        async def cancel_on_terminal():
            client.init()
            client.result()
            await client.closing.wait()
            raise asyncio.CancelledError()
        with patch.object(client, "interrupt", side_effect=cancel_on_terminal), self.assertRaises(asyncio.CancelledError):
            await self.runtime.stop_exact(self.binding.id, **fields)
        self.assertTrue(await self.runtime.wait_idle(1))

    async def test_cancelled_shutdown_starts_pending_closes_and_joins_an_existing_close_once(self):
        await self.submit()
        await self.submit(self.new_binding("second"))
        closing, active = self.clients.calls
        for client in self.clients.calls:
            client.close_gate.clear()
        closing.init()
        closing.result()
        await asyncio.wait_for(closing.closing.wait(), 1)
        interrupt_started = asyncio.Event()
        async def hang():
            interrupt_started.set()
            await asyncio.Future()

        try:
            with patch.object(active, "interrupt", side_effect=hang):
                shutdown = asyncio.create_task(self.runtime.interrupt_all())
                await asyncio.wait_for(interrupt_started.wait(), 1)
                shutdown.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await shutdown
            await asyncio.wait_for(active.closing.wait(), 1)
            self.assertEqual([client.closes for client in self.clients.calls], [1, 1])
        finally:
            for client in self.clients.calls:
                client.close_gate.set()
        self.assertTrue(await self.runtime.wait_idle(1))

    async def test_busy_bindings_share_cwd_and_shutdown_closes_in_parallel(self):
        first = await self.submit()
        second = await self.submit(self.new_binding("other"))
        self.assertNotEqual(first.thread_id, second.thread_id)
        for c in self.clients.calls:
            c.close_gate.clear()
        self.runtime.close_admission()
        shutdown = asyncio.create_task(self.runtime.cancel_tasks())
        await asyncio.wait_for(asyncio.gather(*(c.closing.wait() for c in self.clients.calls)), 1)
        for c in self.clients.calls:
            c.close_gate.set()
        await asyncio.wait_for(shutdown, 1)
        self.assertTrue(await self.runtime.wait_idle(1))
        self.assertEqual(self.events, [])

    async def test_stale_preview_and_changed_settings_reject_before_native_action(self):
        admission = await self.runtime.capture_submission_admission(self.binding.id)
        with self.assertRaises(OrdinaryInputRejected):
            await self.submit(admission=replace(admission, feedback_revision=2))
        first = await self.submit()
        preview = await self.runtime.preview_stop(self.binding)
        await self.finish(first)
        second = await self.submit()
        with self.assertRaises(ThreadActivityChanged):
            await self.runtime.stop_exact(self.binding.id, expected_activity_revision=preview.activity_revision,
                                          expected_turn_id=preview.turn_id)
        self.assertEqual(self.clients.calls[-1].interrupts, 0)
        await self.finish(second)

    async def test_summaryless_worktree_transcript_cannot_prove_cwd(self):
        import json
        import os
        import subprocess
        from uuid import uuid4
        from claude_agent_sdk import get_session_info, get_session_messages, project_key_for_directory

        project, sibling = self.cwd / "project", self.cwd / "sibling"
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        subprocess.run(["git", "-C", str(project), "-c", "user.name=test", "-c", "user.email=test@example.test",
                        "commit", "--allow-empty", "-qm", "fixture"], check=True)
        subprocess.run(["git", "-C", str(project), "worktree", "add", "-q", "--detach", str(sibling)], check=True)
        config = self.cwd / "claude"
        native = config / "projects" / project_key_for_directory(sibling)
        native.mkdir(parents=True)
        session = str(uuid4())
        image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A" * 100_000}}
        entry = {"type": "user", "sessionId": session, "uuid": str(uuid4()), "parentUuid": None,
                 "cwd": str(sibling), "message": {"role": "user", "content": [image]}}
        (native / f"{session}.jsonl").write_text(json.dumps(entry, separators=(",", ":")) + "\n")
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config)}):
            self.assertIsNone(get_session_info(session))
            for path in (project, sibling):
                self.assertEqual(get_session_messages(session, directory=str(path))[0].session_id, session)
            for candidates in ((project,), (sibling, project)):
                self.assertIsNone(await BundledClaudeClients().session_cwd(session, candidates))

    async def test_missing_or_conflicting_summary_cwd_is_refused(self):
        from types import SimpleNamespace
        clients = BundledClaudeClients()
        for identity, cwd in (("id", None), ("id", ""), ("id", str(self.cwd.parent)), ("foreign", str(self.cwd))):
            with patch("netizen_cli.runtime.claude_client.get_session_info", return_value=SimpleNamespace(session_id=identity, cwd=cwd)):
                self.assertIsNone(await clients.session_cwd("id", (self.cwd,)))

    async def test_public_factory_selects_exact_verified_cli_and_native_defaults(self):
        with (patch("netizen_cli.runtime.claude_client.verified_claude_cli", return_value=self.cwd/"bundled"),
              patch("netizen_cli.runtime.claude_client.ClaudeSDKClient") as create):
            clients = BundledClaudeClients()
            for resume in (False, True):
                clients.create("identity", self.cwd, resume=resume)
                options = create.call_args.kwargs["options"]
                self.assertEqual((options.session_id, options.resume), (None, "identity") if resume else ("identity", None))
                self.assertEqual((options.permission_mode, options.cli_path, options.cwd), ("auto", self.cwd/"bundled", self.cwd))
                self.assertEqual((options.setting_sources, options.env, options.allowed_tools), (None, {}, []))
                self.assertEqual(options.system_prompt, {"type": "preset", "preset": "claude_code"})
                self.assertTrue(options.verbatim_prompts)

    async def test_cancelled_handler_during_connect_closes_without_assigning_or_submitting(self):
        self.clients.configure = lambda c: c.connect_gate.clear()
        submit = asyncio.create_task(self.submit())
        await asyncio.wait_for(self.clients.created.wait(), 1)
        submit.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await submit
        self.assertTrue(await self.runtime.wait_idle(1))
        client = self.clients.calls[0]
        self.assertEqual((client.inputs, client.closes), ([], 1))
        self.assertIsNone(self.store.get(self.binding.id).native_thread_id)

    async def test_pointer_changes_during_connect_invalidate_opening_even_after_switch_back(self):
        self.clients.configure = lambda c: c.connect_gate.clear()
        submit = asyncio.create_task(self.submit())
        await asyncio.wait_for(self.clients.created.wait(), 1)
        await self.runtime.binding_pointer_changed(self.binding.id, None)
        await self.runtime.binding_pointer_changed(None, self.binding.id)
        self.clients.calls[0].connect_gate.set()
        with self.assertRaisesRegex(OrdinaryInputRejected, "会话已变化"):
            await submit
        self.assertIsNone(self.store.get(self.binding.id).native_thread_id)
        self.assertEqual(self.clients.calls[0].inputs, [])

    async def test_failed_write_after_handler_cancellation_reports_unknown_once(self):
        def stall(client):
            client.failure = "query"
            client.write_gate.clear()
        self.clients.configure = stall
        submit = asyncio.create_task(self.submit())
        await asyncio.wait_for(self.clients.created.wait(), 1)
        client = self.clients.calls[-1]
        await asyncio.wait_for(client.writing.wait(), 1)
        submit.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await submit
        client.stream.put_nowait(None)
        client.write_gate.set()
        self.assertTrue(await self.runtime.wait_idle(1))
        self.assertEqual([str(event.error) for event in self.events], [DEGRADED])
        self.assertEqual((len(client.inputs), client.closes), (1, 1))

    async def test_unknown_handoff_cannot_retire_the_next_execution(self):
        first = await self.submit()
        client = self.clients.calls[-1]
        client.stream.put_nowait(None)
        await asyncio.wait_for(client.closing.wait(), 1)
        # The slot is released only after the owned close is confirmed, which can
        # take a few more loop iterations; wait for that boundary, not a fixed delay.
        async with asyncio.timeout(1):
            while True:
                try:
                    await self.runtime.capture_submission_admission(self.binding.id)
                    break
                except OrdinaryInputRejected:
                    await asyncio.sleep(0)
        # Feishu still owns the first receipt barrier when the caller resumes the same UUID.
        second = await self.submit()
        self.assertEqual(first.thread_id, second.thread_id)
        first.release_receipt_attempt()
        await asyncio.wait_for(self.delivered.wait(), 1)
        preview = await self.runtime.preview_stop(self.binding)
        self.assertEqual(preview.turn_id, second.turn_id)
        await self.finish(second)
        self.assertEqual([e.turn_id for e in self.events], [first.turn_id, second.turn_id])

    async def test_missing_init_interrupts_exact_client_and_unattributed_result_has_bounded_reconciliation(self):
        first = await self.submit()
        first.release_receipt_attempt()
        client = self.clients.calls[-1]
        client.result()
        self.assertTrue(await self.runtime.wait_idle(1))
        self.assertEqual(client.interrupts, 1)
        self.assertIn(MODE_UNKNOWN, str(self.events[-1].error))
        with patch("netizen_cli.runtime.claude_ordinary._RECONCILE_SECONDS", 0.01):
            second = await self.submit()
            second.release_receipt_attempt()
            client = self.clients.calls[-1]
            client.init()
            client.result(user_message_uuid=None)
            self.assertTrue(await self.runtime.wait_idle(1))
        self.assertEqual(str(self.events[-1].error), DEGRADED)

    async def test_close_timeout_quarantines_binding_and_shutdown_reports_incomplete(self):
        with patch("netizen_cli.runtime.claude_ordinary._CLOSE_SECONDS", 0.01):
            receipt = await self.submit()
            receipt.release_receipt_attempt()
            client = self.clients.calls[-1]
            client.close_gate.clear()
            client.stream.put_nowait(None)
            self.assertFalse(await self.runtime.wait_idle(1))
            with self.assertRaisesRegex(OrdinaryInputRejected, CLOSE_FAILED):
                await self.submit()
            with self.assertRaisesRegex(OrdinaryInputRejected, "无法确认 Claude 客户端已关闭"):
                await self.runtime.preview_stop(self.binding)
            self.runtime.close_admission()
            self.runtime._shutdown_deadline = asyncio.get_running_loop().time() + 0.05
            with self.assertRaisesRegex(RuntimeError, "未能确认"):
                await asyncio.wait_for(self.runtime.interrupt_all(), 1)
            self.assertFalse(await self.runtime.wait_idle(0))
            client.close_gate.set()
            await asyncio.gather(*self.runtime._closes)
            self.assertTrue(await self.runtime.wait_idle(0))
            self.assertEqual((len(client.inputs), client.closes), (1, 1))

    async def test_stop_receipt_precedes_terminal_handoff_and_duplicate_stop_is_local(self):
        receipt = await self.submit()
        receipt.release_receipt_attempt()
        client = self.clients.calls[-1]
        preview = await self.runtime.preview_stop(self.binding)
        fields = dict(expected_activity_revision=preview.activity_revision, expected_turn_id=preview.turn_id)
        acknowledged, release = asyncio.Event(), asyncio.Event()

        async def acknowledge():
            acknowledged.set()
            await release.wait()

        stop = asyncio.create_task(self.runtime.stop_exact(self.binding.id, acknowledge=acknowledge, **fields))
        await asyncio.wait_for(acknowledged.wait(), 1)
        self.assertEqual(client.interrupts, 1)
        self.assertEqual((await self.runtime.recheck_turn_exact(self.binding.id, **fields)).state.value, "stopping")
        self.assertEqual((await self.runtime.stop_exact(self.binding.id, **fields)).value, "stopping")
        client.init()
        client.result("error_during_execution", "aborted_tools", True)
        await asyncio.wait_for(client.closing.wait(), 1)
        self.assertFalse(self.delivered.is_set())
        release.set()
        self.assertEqual((await stop).value, "requested")
        self.assertTrue(await self.runtime.wait_idle(1))
        self.assertEqual(self.events[-1].status, "interrupted")

    async def test_recognized_peer_result_does_not_start_reconciliation_deadline(self):
        with patch("netizen_cli.runtime.claude_ordinary._RECONCILE_SECONDS", 0):
            receipt = await self.submit()
            receipt.release_receipt_attempt()
            client = self.clients.calls[-1]
            client.result(user_message_uuid="foreign", origin={"kind": "peer"})
            client.init()
            while not client.stream.empty():
                await asyncio.sleep(0)
            await asyncio.sleep(0)
            client.result()
            self.assertTrue(await self.runtime.wait_idle(1))
            self.assertEqual(self.events[-1].final_response, "answer")

    async def test_unattributed_injected_result_on_resume_does_not_start_reconciliation(self):
        # Live Claude Code 2.1.329: resuming after a killed background task first emits
        # a task-notification Result with no input UUIDs and no terminal reason.
        with patch("netizen_cli.runtime.claude_ordinary._RECONCILE_SECONDS", 0):
            receipt = await self.submit()
            receipt.release_receipt_attempt()
            client = self.clients.calls[-1]
            client.init(user_message_uuids=[])
            client.result(reason=None, result=None, user_message_uuid=None, user_message_uuids=[],
                          origin={"kind": "task-notification", "producer": "session-task"})
            client.init()
            while not client.stream.empty():
                await asyncio.sleep(0)
            await asyncio.sleep(0)
            client.result()
            self.assertTrue(await self.runtime.wait_idle(1))
            self.assertEqual(self.events[-1].final_response, "answer")

    async def test_stop_preview_during_close_reports_the_finished_execution(self):
        receipt = await self.submit()
        receipt.release_receipt_attempt()
        client = self.clients.calls[-1]
        client.close_gate.clear()
        client.init()
        client.result()
        await asyncio.wait_for(client.closing.wait(), 1)
        await asyncio.wait_for(self.delivered.wait(), 1)
        with self.assertRaisesRegex(OrdinaryInputRejected, "收尾"):
            await self.runtime.preview_stop(self.binding)
        client.close_gate.set()
        self.assertTrue(await self.runtime.wait_idle(1))
        self.assertIsNone(await self.runtime.preview_stop(self.binding))
        self.assertEqual(client.interrupts, 0)

    async def test_unattributed_result_without_origin_still_reconciles(self):
        with patch("netizen_cli.runtime.claude_ordinary._RECONCILE_SECONDS", 0.01):
            receipt = await self.submit()
            receipt.release_receipt_attempt()
            client = self.clients.calls[-1]
            client.init()
            client.result(user_message_uuid=None, user_message_uuids=[])
            self.assertTrue(await self.runtime.wait_idle(1))
        self.assertEqual(str(self.events[-1].error), DEGRADED)

    async def test_interrupt_failure_after_confirmed_completion_returns_finished(self):
        receipt = await self.submit()
        receipt.release_receipt_attempt()
        client = self.clients.calls[-1]
        client.close_gate.clear()
        async def race():
            client.init()
            client.result()
            await client.closing.wait()
            raise OSError("closed")
        async def unexpected_ack():
            self.fail("stop success must not be acknowledged")
        with patch.object(client, "interrupt", side_effect=race):
            preview = await self.runtime.preview_stop(self.binding)
            result = await asyncio.wait_for(self.runtime.stop_exact(self.binding.id, acknowledge=unexpected_ack,
                expected_activity_revision=preview.activity_revision, expected_turn_id=preview.turn_id), 1)
        self.assertEqual(result, StopDisposition.NOT_RUNNING)
        client.close_gate.set()
        self.assertTrue(await self.runtime.wait_idle(1))

    async def test_lost_write_receipt_reconciles_without_replay(self):
        self.clients.configure = lambda client: setattr(client, "failure", "query")
        submitting = asyncio.create_task(self.submit())
        await asyncio.wait_for(self.clients.created.wait(), 1)
        client, = self.clients.calls
        await asyncio.wait_for(client.writing.wait(), 1)
        client.init()
        client.result()
        receipt = await asyncio.wait_for(submitting, 1)
        receipt.release_receipt_attempt()
        self.assertTrue(await self.runtime.wait_idle(1))
        self.assertEqual((client.consumers, len(client.inputs), self.events[-1].final_response), (1, 1, "answer"))

    async def test_live_actual_cwd_remains_owned_through_late_close(self):
        first = await self.submit()
        await self.finish(first)
        target = self.cwd / "managed" / "subdir"
        target.mkdir(parents=True)
        self.clients.cwds[first.thread_id] = target
        with patch("netizen_cli.runtime.claude_ordinary._CLOSE_SECONDS", 0.01):
            second = await self.submit()
            client = self.clients.calls[-1]
            self.assertEqual(self.runtime.occupying_bindings(target.parent), {self.binding.id})
            client.close_gate.clear()
            client.init()
            client.result()
            second.release_receipt_attempt()
            await asyncio.wait_for(self.delivered.wait(), 1)
            await asyncio.wait_for(client.closing.wait(), 1)
            await asyncio.gather(*self.runtime._tasks)
            self.assertEqual(self.runtime.occupying_bindings(target), {self.binding.id})
            self.assertTrue(self.runtime.local_snapshot(self.binding.id).unavailable)
            client.close_gate.set()
            self.assertTrue(await self.runtime.wait_idle(1))
            self.assertFalse(self.runtime.occupying_bindings(target))
            await self.finish(await self.submit())

    async def test_materialized_worktree_usage_blocks_without_transcript_scan(self):
        first = await self.submit()
        self.assertEqual((await self.runtime.resource_usage(self.binding.id))["cwd"], str(self.cwd))
        await self.finish(first)
        with patch.object(self.clients, "session_cwd") as lookup:
            usage = await self.runtime.resource_usage(self.binding.id)
            self.assertEqual(usage["status"], "unknown")
            self.assertTrue(usage["reason"])
            lookup.assert_not_called()

    async def test_shutdown_during_receipt_or_opening_drains_each_owned_client(self):
        receipt = await self.submit()
        first = self.clients.calls[-1]
        first.init()
        first.result()
        await asyncio.wait_for(first.closing.wait(), 1)
        self.clients.created.clear()
        self.clients.configure = lambda c: c.connect_gate.clear()
        opening = asyncio.create_task(self.submit(self.new_binding("second")))
        await asyncio.wait_for(self.clients.created.wait(), 1)
        await asyncio.wait_for(self.runtime.interrupt_all(), 1)
        with self.assertRaises(OrdinaryInputRejected):
            await opening
        self.assertTrue(await self.runtime.wait_idle(0))
        self.assertEqual([event.turn_id for event in self.events], [receipt.turn_id])
        self.assertEqual([client.closes for client in self.clients.calls], [1, 1])

    async def test_lost_interrupt_ack_keeps_existing_reader_and_never_targets_successor(self):
        from unittest.mock import AsyncMock

        first = await self.submit()
        first.release_receipt_attempt()
        client = self.clients.calls[-1]
        preview = await self.runtime.preview_stop(self.binding)
        fields = dict(expected_activity_revision=preview.activity_revision, expected_turn_id=preview.turn_id)
        with patch.object(client, "interrupt", new=AsyncMock(side_effect=OSError("private"))):
            with self.assertRaisesRegex(TerminalStateUnknown, "无法确认"):
                await self.runtime.stop_exact(self.binding.id, **fields)
        with self.assertRaises(OrdinaryInputRejected):
            await self.submit()
        self.assertEqual(await self.runtime.stop_exact(self.binding.id, **fields), StopDisposition.REQUESTED)
        client.init()
        client.result("error_during_execution", "aborted_streaming", True)
        self.assertTrue(await self.runtime.wait_idle(1))
        second = await self.submit()
        with self.assertRaises(ThreadActivityChanged):
            await self.runtime.stop_exact(self.binding.id, **fields)
        self.assertEqual((client.consumers, self.clients.calls[-1].interrupts), (1, 0))
        await self.finish(second)

    async def test_start_timeout_is_unsent_and_write_timeout_is_unknown(self):
        with patch("netizen_cli.runtime.claude_ordinary._START_SECONDS", 0.01):
            self.clients.configure = lambda client: client.connect_gate.clear()
            with self.assertRaisesRegex(OrdinaryInputRejected, START_FAILED):
                await self.submit()
        self.assertIsNone(self.store.get(self.binding.id).native_thread_id)
        with patch("netizen_cli.runtime.claude_ordinary._WRITE_SECONDS", 0.01):
            self.clients.configure = lambda client: client.write_gate.clear()
            with self.assertRaisesRegex(TerminalStateUnknown, UNKNOWN):
                await self.submit()
        self.assertIsNotNone(self.store.get(self.binding.id).native_thread_id)
        self.assertEqual([len(c.inputs) for c in self.clients.calls], [0, 1])
        self.assertEqual([c.closes for c in self.clients.calls], [1, 1])

    async def test_cancelling_feishu_receipt_releases_exact_terminal_once(self):
        from unittest.mock import AsyncMock
        from tests.support.channel_fixtures import channel_fixture
        from tests.support.channel_messages import FakeMessage

        message = FakeMessage("input", message_id="om_receipt")
        receipt = await self.submit(origin=message)
        client = self.clients.calls[-1]
        client.init()
        client.result()
        await asyncio.wait_for(client.closing.wait(), 1)
        started = asyncio.Event()

        async def slow_reaction(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        async with channel_fixture() as fixture:
            with (patch.object(fixture.app._reactions, "start", side_effect=slow_reaction),
                  patch.object(fixture.app._progress_cards, "start", new=AsyncMock(return_value=True))):
                delivery = asyncio.create_task(fixture.app._present_ordinary_receipt(receipt, message))
                await asyncio.wait_for(started.wait(), 1)
                self.assertEqual(self.events, [])
                delivery.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await delivery
        self.assertTrue(await self.runtime.wait_idle(1))
        self.assertEqual([e.turn_id for e in self.events], [receipt.turn_id])
        self.assertEqual((client.consumers, len(client.inputs)), (1, 1))
