import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import UUID

from netizen_cli.bindings import BindingStore
from netizen_cli.domain import FeishuScope, MessageContextAnchor, ScopeKind
from netizen_cli.file_evidence import FileEvidence
from netizen_cli.ordinary_sessions import OrdinaryConclusion, OrdinaryInputRejected, PreparedPrompt, PromptImage
from netizen_cli.runtime.claude_client import BundledClaudeClients
from netizen_cli.runtime.claude_ordinary import (
    CLOSE_FAILED, DEFAULT_MODE, GRACE, MODE_UNKNOWN, START_FAILED, UNKNOWN, ClaudeOrdinarySessions,
)
from netizen_cli.runtime.contracts import ContextCursorCommit, StopDisposition, TerminalStateUnknown, ThreadActivityChanged
from tests.support.claude_client import FakeClaudeClients


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
        self.notices = []
        self.delivered = asyncio.Event()

        async def completion(event):
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
        self.assertEqual((event.partial_answers, event.activity, event.file_evidence()), ((), None, FileEvidence()))
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
                    self.assertEqual(str(event.error), UNKNOWN)
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

    async def test_foreign_result_can_precede_our_attributed_result_and_duplicate_is_not_delivered(self):
        receipt = await self.submit()
        client = self.clients.calls[-1]
        client.init()
        client.result(user_message_uuid="other", result="foreign")
        client.result(user_message_uuid="other", user_message_uuids=[receipt.turn_id], result="ours")
        client.result(result="duplicate")
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
                with self.assertRaisesRegex(OrdinaryInputRejected, "正在执行"):
                    await self.submit()
                client.close_gate.set()
                self.assertTrue(await self.runtime.wait_idle(1))
                self.assertEqual(str(self.events[-1].error), UNKNOWN)
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
        self.assertEqual(self.events, [])
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

    async def test_cursor_updates_only_on_attribution_and_failure_preserves_outcome(self):
        commit = ContextCursorCommit(1, MessageContextAnchor("new", 200))
        with patch.object(self.store, "commit_context_anchor", side_effect=ValueError("private")) as save:
            first = await self.submit(context_commit=commit)
            save.assert_not_called()
            first.release_receipt_attempt()
            client = self.clients.calls[-1]
            client.init()
            client.result(user_message_uuid="other")
            client.stream.put_nowait(None)
            await self.runtime.wait_idle(1)
            save.assert_called_once()
            save.reset_mock()
            await self.finish(await self.submit(context_commit=commit))
            save.assert_called_once_with(binding_id=self.binding.id, expected_context_revision=1, anchor=commit.anchor)
            self.assertEqual(self.events[-1].status, "completed")
            self.assertIn(("origin", "已接收，但上下文边界更新失败。"), self.notices)

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
        self.assertEqual([str(e.error) for e in self.events], [UNKNOWN, UNKNOWN])

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

    async def test_public_scoped_read_recovers_large_image_without_a_summary(self):
        import json
        import os
        from uuid import uuid4

        from claude_agent_sdk import get_session_info, project_key_for_directory

        config = self.cwd / "claude"
        project = config / "projects" / project_key_for_directory(self.cwd)
        project.mkdir(parents=True)
        session = str(uuid4())
        image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A" * 100_000}}
        entry = {"type": "user", "sessionId": session, "uuid": str(uuid4()), "parentUuid": None,
                 "cwd": str(self.cwd), "message": {"role": "user", "content": [image]}}
        (project / f"{session}.jsonl").write_text(json.dumps(entry, separators=(",", ":")) + "\n")
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config)}):
            self.assertIsNone(get_session_info(session))
            self.assertEqual(await BundledClaudeClients().session_cwd(session, (self.cwd,)), self.cwd)

    async def test_summaryless_lookup_requires_exactly_one_positive_candidate(self):
        from types import SimpleNamespace
        candidates = (self.cwd / "worktree", self.cwd)
        candidates[0].mkdir()
        clients = BundledClaudeClients()
        for results, expected in (
            ([[], [SimpleNamespace(session_id="id")]], self.cwd),
            ([[], []], None),
            ([[SimpleNamespace(session_id="id")], [SimpleNamespace(session_id="id")]], None),
            ([[SimpleNamespace(session_id="foreign")]], None),
        ):
            with (patch("netizen_cli.runtime.claude_client.get_session_info", return_value=None),
                  patch("netizen_cli.runtime.claude_client.get_session_messages", side_effect=results) as read):
                self.assertEqual(await clients.session_cwd("id", candidates), expected)
                self.assertTrue(all(call.kwargs["limit"] == 1 for call in read.call_args_list))

    async def test_missing_summary_cwd_requires_positive_messages_and_conflicts_refuse(self):
        from types import SimpleNamespace
        clients = BundledClaudeClients()
        for summary, expected, scoped in (
            (SimpleNamespace(session_id="id", cwd=None), self.cwd, True),
            (SimpleNamespace(session_id="id", cwd=""), self.cwd, True),
            (SimpleNamespace(session_id="id", cwd=str(self.cwd.parent)), None, False),
            (SimpleNamespace(session_id="foreign", cwd=None), None, False),
        ):
            with (patch("netizen_cli.runtime.claude_client.get_session_info", return_value=summary),
                  patch("netizen_cli.runtime.claude_client.get_session_messages",
                        return_value=[SimpleNamespace(session_id="id")]) as read):
                self.assertEqual(await clients.session_cwd("id", (self.cwd,)), expected)
                self.assertEqual(read.called, scoped)

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
        self.assertEqual([str(event.error) for event in self.events], [UNKNOWN])
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
        self.assertEqual(str(self.events[-1].error), UNKNOWN)

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
            client.init()
            client.result(user_message_uuid="foreign", origin={"kind": "peer"})
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
            client.init()
            client.result(reason=None, user_message_uuid=None, user_message_uuids=[],
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
        with self.assertRaisesRegex(OrdinaryInputRejected, "结果正在送出"):
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
        self.assertEqual(str(self.events[-1].error), UNKNOWN)

    async def test_interrupt_failure_after_confirmed_completion_returns_finished(self):
        receipt = await self.submit()
        receipt.release_receipt_attempt()
        client = self.clients.calls[-1]
        async def race():
            client.init()
            client.result()
            while not self.runtime._active[self.binding.id].closed:
                await asyncio.sleep(0)
            raise OSError("closed")
        async def unexpected_ack():
            self.fail("stop success must not be acknowledged")
        with patch.object(client, "interrupt", side_effect=race):
            preview = await self.runtime.preview_stop(self.binding)
            result = await asyncio.wait_for(self.runtime.stop_exact(self.binding.id, acknowledge=unexpected_ack,
                expected_activity_revision=preview.activity_revision, expected_turn_id=preview.turn_id), 1)
        self.assertEqual(result, StopDisposition.NOT_RUNNING)
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

    async def test_native_scan_does_not_hold_binding_lock_or_return_stale_usage(self):
        first = await self.submit()
        await self.finish(first)
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_lookup(_, candidates):
            entered.set()
            await release.wait()
            return self.cwd
        with patch.object(self.clients, "session_cwd", side_effect=slow_lookup):
            query = asyncio.create_task(self.runtime.resource_usage(self.binding.id))
            await asyncio.wait_for(entered.wait(), 1)
            opening = asyncio.create_task(self.submit())
            while not self.runtime.local_snapshot(self.binding.id).turn:
                await asyncio.sleep(0)
            with self.assertRaisesRegex(OrdinaryInputRejected, "正在执行"):
                await self.runtime.capture_submission_admission(self.binding.id)
            release.set()
            self.assertEqual(await query, {"cwd": None, "status": "unknown"})
            await self.finish(await opening)

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
