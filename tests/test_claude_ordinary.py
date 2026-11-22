import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from netizen_cli.bindings import BindingStore
from netizen_cli.domain import FeishuScope, MentionContextMode, MessageContextAnchor, ScopeKind
from netizen_cli.file_evidence import FileEvidence
from netizen_cli.ordinary_sessions import OrdinaryConclusion, OrdinaryInputRejected, PreparedPrompt, PromptImage
from netizen_cli.runtime.claude_client import BundledClaudeClients, ClaudeBackground
from netizen_cli.runtime.claude_ordinary import (
    CLOSE_FAILED, DEFAULT_MODE, DEGRADED, GRACE, START_FAILED, UNKNOWN, ClaudeOrdinarySessions,
)
from netizen_cli.runtime.contracts import TurnObservationUnavailableOutcome, ContextCursorCommit, StopDisposition, TerminalStateUnknown, ThreadActivityChanged
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
            if isinstance(event, TurnObservationUnavailableOutcome) and str(event.error) == DEGRADED:
                self.notices.append((event.origin, str(event.error)))
            else:
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

    async def test_background_handoff_keeps_exact_origin_and_one_reader_through_slow_delivery(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def slow(event):
            self.events.append(event)
            started.set()
            await release.wait()
            if len(self.events) == 1:
                raise OSError("unconfirmed Feishu delivery")
        self.runtime._completion = slow
        stale = await self.runtime.capture_submission_admission(self.binding.id)
        receipt = await self.submit(origin="first-anchor")
        receipt.release_receipt_attempt()
        client = self.clients.calls[-1]
        client.init()
        client.start_background()
        client.stop_snapshot(["task"])
        client.result(snapshot=False)
        await asyncio.wait_for(started.wait(), 1)
        with self.assertRaisesRegex(OrdinaryInputRejected, "^首答已发，后台任务仍在运行$"):
            await self.submit(origin="later-anchor")
        with patch.object(self.store, "commit_context_anchor") as save:
            with self.assertRaisesRegex(OrdinaryInputRejected, "^首答已发，后台任务仍在运行$"):
                await self.submit(admission=stale, context_commit=ContextCursorCommit(
                    self.binding.context_revision, MessageContextAnchor("later-anchor", 200)))
            save.assert_not_called()
        self.assertEqual(len(self.clients.calls), 1)
        other = self.new_binding("other")
        second = await self.submit(other)
        second.release_receipt_attempt()
        client.end_background()
        client.init(user_message_uuids=[])
        wake_id = str(uuid4())
        client.wake_result(uuid=wake_id, result="wake", idle=False)
        client.wake_result(uuid=wake_id, result="wake")
        await asyncio.wait_for(client.closing.wait(), 1)
        self.assertEqual([e.final_response for e in self.events], ["answer"])
        release.set()
        other_client = self.clients.calls[-1]
        other_client.init()
        other_client.result()
        self.assertTrue(await self.runtime.wait_idle(1))
        self.assertEqual([(e.final_response, e.turn_id) for e in self.events if e.origin == "first-anchor"],
                         [("answer", receipt.turn_id), ("wake", wake_id)])
        self.assertEqual((client.closes, client.consumers, len(client.inputs)), (1, 1, 1))

    async def test_mixed_wake_ends_generation_without_delivering(self):
        receipt = await self.submit()
        receipt.release_receipt_attempt()
        client = self.clients.calls[-1]
        client.init()
        client.start_background()
        client.end_background()  # Terminal update only, even before H1's Stop.
        client.result()
        await asyncio.wait_for(self.delivered.wait(), 1)
        self.assertFalse(client.closing.is_set())
        client.init(user_message_uuids=[])
        client.wake_result(user_message_uuid="foreign", user_message_uuids=["foreign"], result="mixed")
        self.assertTrue(await self.runtime.wait_idle(1))
        self.assertEqual(self.notices, [("origin", DEGRADED)])
        self.assertEqual([e.final_response for e in self.events], ["answer"])
        for token, owner in ((receipt.turn_id, "other"), ("old-generation", "owner")):
            with self.assertRaises(OrdinaryInputRejected):
                await self.runtime.abandon_background(self.binding.id, token, owner)
        self.assertEqual((client.interrupts, client.closes), (0, 1))

    async def wait_for_abandon_notice(self):
        async with asyncio.timeout(1):
            while not any("/stop abandon-background " in text for _, text in self.notices):
                await asyncio.sleep(0)

    async def pending_background(self, *, two=False):
        receipt = await self.submit()
        receipt.release_receipt_attempt()
        client = self.clients.calls[-1]
        client.init()
        await client.consumed()
        foreground = await self.runtime.preview_stop(self.binding)
        client.start_background()
        if two:
            client.start_background("run2", "task2", "tool2", members=[
                {"run_id": "run", "task_id": "task"}, {"run_id": "run2", "task_id": "task2"}])
        client.stop_snapshot(["task", "task2"] if two else ["task"])
        client.result(snapshot=False)
        async with asyncio.timeout(1):
            while not (client.background.first_done and client.background.idle):
                await asyncio.sleep(0)
        return client, foreground

    async def stop_using(self, preview):
        return await self.runtime.stop_exact(self.binding.id, expected_activity_revision=preview.activity_revision,
                                             expected_turn_id=preview.turn_id)

    async def test_exact_task_stop_requires_update_removal_and_fresh_idle(self):
        client, foreground = await self.pending_background()
        with self.assertRaises(ThreadActivityChanged):
            await self.stop_using(foreground)
        preview = await self.runtime.preview_stop(self.binding)
        self.assertEqual(await self.stop_using(preview), StopDisposition.REQUESTED)
        with self.assertRaises(ThreadActivityChanged):
            await self.stop_using(preview)
        self.assertEqual(await self.stop_using(await self.runtime.preview_stop(self.binding)), StopDisposition.STOPPING)
        client.end_background("killed")
        async with asyncio.timeout(1):
            while client.background.members:
                await asyncio.sleep(0)
        self.assertFalse(client.closing.is_set())
        client.system("session_state_changed", state="idle")
        self.assertTrue(await self.runtime.wait_idle(1))
        self.assertEqual((client.stops, client.interrupts, len(self.events)), (["task"], 0, 1))
        # The next client may even recycle the same native task ID.
        successor, _ = await self.pending_background()
        with self.assertRaises(ThreadActivityChanged):
            await self.stop_using(preview)
        self.assertEqual((successor.stops, successor.interrupts), ([], 0))

    async def test_ambiguous_and_waking_targets_cannot_use_foreground_interrupt(self):
        client, _ = await self.pending_background(two=True)
        with self.assertRaisesRegex(OrdinaryInputRejected, "多个后台任务") as refusal:
            await self.runtime.preview_stop(self.binding)
        self.assertEqual(str(refusal.exception).splitlines()[-1], f"/stop abandon-background {client.inputs[0]['uuid']}")
        with self.assertRaisesRegex(OrdinaryInputRejected, "不可放弃"):
            await self.runtime.abandon_background(self.binding.id, client.inputs[0]["uuid"], "owner")
        client.end_background(run="run2", task="task2", members=[{"run_id": "run", "task_id": "task"}])
        client.init(user_message_uuids=[])
        async with asyncio.timeout(1):
            while not client.background.wake:
                await asyncio.sleep(0)
        with self.assertRaisesRegex(OrdinaryInputRejected, "后台完成轮") as refusal:
            await self.runtime.preview_stop(self.binding)
        self.assertEqual(str(refusal.exception).splitlines()[-1], f"/stop abandon-background {client.inputs[0]['uuid']}")
        client.stop_snapshot(["task"])
        client.wake_result("run2", result="second first", snapshot=False)
        async with asyncio.timeout(1):
            while client.background.single_task is None:
                await asyncio.sleep(0)
        grant = await self.runtime.preview_stop(self.binding)
        client.end_background()
        client.init(user_message_uuids=[])
        async with asyncio.timeout(1):
            while not client.background.wake:
                await asyncio.sleep(0)
        with self.assertRaises(ThreadActivityChanged):
            await self.stop_using(grant)
        client.wake_result(result="first last")
        self.assertTrue(await self.runtime.wait_idle(1))
        self.assertEqual([e.final_response for e in self.events], ["answer", "second first", "first last"])
        self.assertEqual((client.stops, client.interrupts), ([], 0))

    async def test_lost_task_control_receipt_reconciles_only_the_captured_task(self):
        client, _ = await self.pending_background()
        grant = await self.runtime.preview_stop(self.binding)
        with patch.object(client, "stop_task", side_effect=OSError("lost")) as stop:
            with self.assertRaisesRegex(TerminalStateUnknown, "无法确认"):
                await self.stop_using(grant)
            stop.assert_awaited_once_with("task")
        self.assertTrue(await self.runtime.wait_idle(1))
        self.assertIn(("origin", DEGRADED), self.notices)
        client.end_background("killed")
        client.system("session_state_changed", state="idle")
        self.assertTrue(await self.runtime.wait_idle(1))
        self.assertEqual((client.interrupts, len(self.events)), (0, 1))

    async def test_terminal_update_without_wake_degrades_at_deadline(self):
        with patch("netizen_cli.runtime.claude_ordinary._RECONCILE_SECONDS", .02):
            client, _ = await self.pending_background()
            client.end_background()
            client.system("session_state_changed", state="idle")
            self.assertTrue(await self.runtime.wait_idle(1))
            self.assertEqual(self.notices, [("origin", DEGRADED)])
            self.assertEqual([event.final_response for event in self.events], ["answer"])

    async def test_task_finishes_while_stop_receipt_is_outstanding(self):
        client, _ = await self.pending_background()
        grant = await self.runtime.preview_stop(self.binding)

        async def finished(task_id):
            self.assertEqual(task_id, "task")
            client.end_background()
            client.init(user_message_uuids=[])
            client.wake_result(result="finished before stop")
            await asyncio.wait_for(client.closing.wait(), 1)
            raise OSError("already finished")

        with patch.object(client, "stop_task", side_effect=finished):
            self.assertEqual(await self.stop_using(grant), StopDisposition.NOT_RUNNING)
        self.assertTrue(await self.runtime.wait_idle(1))
        self.assertEqual(self.notices, [])
        self.assertEqual([event.final_response for event in self.events], ["answer", "finished before stop"])

    async def test_foreign_or_malformed_run_metadata_never_routes_a_wake_body(self):
        for override in ({"producer": "peer"}, {"runId": "unknown"}, {"subkind": "cron"}, {"runId": None}):
            self.notices.clear()
            client, _ = await self.pending_background()
            client.end_background()
            client.init(user_message_uuids=[])
            client.wake_result(result="foreign", origin={
                "kind": "task-notification", "producer": "session-task", "runId": "run", **override})
            self.assertTrue(await self.runtime.wait_idle(1))
            self.assertEqual(self.notices, [("origin", DEGRADED)])
        self.assertEqual([e.final_response for e in self.events], ["answer"] * 4)

    async def test_registered_cron_holds_and_missing_stop_alone_allows_close(self):
        for missing in (False, True):
            self.notices.clear()
            receipt = await self.submit()
            receipt.release_receipt_attempt()
            client = self.clients.calls[-1]
            client.init()
            if not missing:
                client.stop_snapshot(crons=["registered"])
            client.result(snapshot=False)
            if missing:
                self.assertTrue(await self.runtime.wait_idle(1))
                continue
            await self.wait_for_abandon_notice()
            self.assertFalse(client.closing.is_set())
            await self.runtime.abandon_background(self.binding.id, receipt.turn_id, "owner")
            self.assertTrue(await self.runtime.wait_idle(1))

    async def test_abandon_receipt_excludes_settled_task_from_earlier_stop_inventory(self):
        receipt = await self.submit()
        receipt.release_receipt_attempt()
        client = self.clients.calls[-1]
        client.init()
        client.start_background()
        client.stop_snapshot(["task"], ["registered"])
        client.result(snapshot=False)
        await client.consumed()
        await self.stop_using(await self.runtime.preview_stop(self.binding))
        client.end_background("killed")
        await client.consumed()
        with self.assertRaises(OrdinaryInputRejected):
            await self.runtime.abandon_background(None, receipt.turn_id, "owner")
        client.system("session_state_changed", state="idle")
        await client.consumed()
        self.assertTrue(client.background.runs["run"].settled)
        await self.wait_for_abandon_notice()
        summary = await self.runtime.abandon_background(None, receipt.turn_id, "owner")
        self.assertIn("任务：0，会话定时唤醒：1", summary)
        self.assertTrue(await self.runtime.wait_idle(1))

    async def test_empty_correlated_receipt_settles_only_that_run_without_a_reply(self):
        receipt = await self.submit()
        receipt.release_receipt_attempt()
        client = self.clients.calls[-1]
        client.init()
        client.start_background()
        client.stop_snapshot(["task"])
        client.result(snapshot=False)
        client.end_background()
        client.init(user_message_uuids=[])
        client.wake_result(reason=None, result="", snapshot=False)
        self.assertTrue(await self.runtime.wait_idle(1))
        self.assertEqual([e.final_response for e in self.events], ["answer"])

    async def test_record_each_tool_source_before_later_assistants(self):
        from claude_agent_sdk import AssistantMessage, ToolUseBlock

        for ids in (["foreign"], ["foreign", "human"]):
            with self.subTest(ids=ids):
                self.notices.clear()
                receipt = await self.submit()
                receipt.release_receipt_attempt()
                client = self.clients.calls[-1]
                client.init()
                for tool, run, task, sources in (("bad-tool", "bad", "foreign-task", ids),
                                                 ("good-tool", "good", "own-task", ["human"])):
                    values = [receipt.turn_id if v == "human" else v for v in sources]
                    client.stream.put_nowait(AssistantMessage([ToolUseBlock(tool, "Bash", {"run_in_background": True})],
                        "fixture", session_id=client.session, user_message_uuid=values[-1], user_message_uuids=values))
                    client.system("task_started", task_id=task, run_id=run, tool_use_id=tool,
                                  task_type="local_bash", is_backgrounded=True)
                client.stop_snapshot(["own-task", "foreign-task"])
                client.result(snapshot=False)
                for run, task in (("bad", "foreign-task"), ("good", "own-task")):
                    client.end_background(run=run, task=task)
                    client.init(user_message_uuids=[])
                    client.wake_result(run=run, result=run)
                self.assertTrue(await self.runtime.wait_idle(1))
                self.assertEqual(self.notices, [("origin", DEGRADED)])
                self.assertEqual(self.events, [])

    async def test_slow_degraded_notice_does_not_block_reader_or_disconnect(self):
        blocked, release = asyncio.Event(), asyncio.Event()
        async def complete(event):
            self.assertEqual(str(event.error), DEGRADED)
            blocked.set()
            await release.wait()
        self.runtime._completion = complete
        receipt = await self.submit()
        receipt.release_receipt_attempt()
        client = self.clients.calls[-1]
        client.init()
        client.start_background()
        client.close_gate.clear()
        client.callback({"session_id": client.session, "background_tasks": None, "session_crons": []})
        client.result(snapshot=False)
        await asyncio.wait_for(blocked.wait(), 1)
        try:
            await client.background.stop_hook({"hook_event_name": "Stop", "session_id": client.session,
                "background_tasks": [], "session_crons": [], "last_assistant_message": "private"}, None, {})
            client.end_background()
            client.init(user_message_uuids=[])
            client.wake_result(result="independently attributed", snapshot=False)
            await client.consumed()
            self.assertEqual(self.events, [])
            self.assertTrue(client.closing.is_set())
        finally:
            client.close_gate.set()
            release.set()

    async def test_lost_write_verified_before_slow_human_terminal(self):
        self.clients.configure = lambda c: setattr(c, "failure", "query")
        with patch("netizen_cli.runtime.claude_ordinary._RECONCILE_SECONDS", .04):
            submit = asyncio.create_task(self.submit())
            await self.clients.created.wait()
            client = self.clients.calls[-1]
            await client.writing.wait()
            client.init()
            receipt = await asyncio.wait_for(submit, 1)
            receipt.release_receipt_attempt()
            self.assertFalse(await self.runtime.wait_idle(.08))
            client.start_background()
            client.stop_snapshot(["task"])
            client.result(snapshot=False)
            client.end_background()
            client.init(user_message_uuids=[])
            client.wake_result(result="follow")
            self.assertTrue(await self.runtime.wait_idle(1))
            self.assertEqual([e.final_response for e in self.events], ["answer", "follow"])
            self.assertEqual((client.consumers, len(client.inputs)), (1, 1))

    async def test_exact_stream_verifies_write_before_permission_init(self):
        from claude_agent_sdk import StreamEvent

        self.clients.configure = lambda c: setattr(c, "failure", "query")
        with patch("netizen_cli.runtime.claude_ordinary._RECONCILE_SECONDS", .04):
            submit = asyncio.create_task(self.submit())
            await self.clients.created.wait()
            client = self.clients.calls[-1]
            await client.writing.wait()
            client.stream.put_nowait(StreamEvent(str(uuid4()), client.session, {}, user_message_uuid=client.inputs[0]["uuid"]))
            receipt = await asyncio.wait_for(submit, 1)
            receipt.release_receipt_attempt()
            self.assertFalse(await self.runtime.wait_idle(.08))
            with self.assertRaises(OrdinaryInputRejected):
                await self.runtime.preview_stop(self.binding)
            client.init()
            client.result()
            self.assertTrue(await self.runtime.wait_idle(1))
            self.assertEqual([(e.final_response, e.turn_id) for e in self.events], [("answer", receipt.turn_id)])
            self.assertEqual(client.interrupts, 0)

    async def test_terminal_proof_deadline_is_independent_of_quota_and_other_task(self):
        from claude_agent_sdk import RateLimitEvent, RateLimitInfo
        with patch("netizen_cli.runtime.claude_ordinary._RECONCILE_SECONDS", .04):
            client, _ = await self.pending_background(two=True)
            client.end_background(run="run2", task="task2", members=[{"run_id": "run", "task_id": "task"}])
            client.system("session_state_changed", state="idle")
            client.close_gate.clear()
            try:
                while not self.notices:
                    client.stream.put_nowait(RateLimitEvent(RateLimitInfo("allowed"), "rate", client.session))
                    await client.consumed()
                client.wake_result(run="run2", result="late")
                await client.consumed()
                self.assertEqual(self.notices, [("origin", DEGRADED)])
                self.assertEqual([e.final_response for e in self.events], ["answer"])
            finally:
                client.close_gate.set()

    async def test_detached_input_receives_one_notice_during_stop_receipt(self):
        self.clients.configure = lambda c: setattr(c, "failure", "query")
        submit = asyncio.create_task(self.submit(origin="detached"))
        await self.clients.created.wait()
        client = self.clients.calls[-1]
        await client.writing.wait()
        submit.cancel()
        await asyncio.gather(submit, return_exceptions=True)
        client.init()
        await client.consumed()
        grant = await self.runtime.preview_stop(self.binding)
        entered, release = asyncio.Event(), asyncio.Event()
        async def ack():
            entered.set()
            await release.wait()
        stop = asyncio.create_task(self.runtime.stop_exact(self.binding.id, acknowledge=ack,
            expected_activity_revision=grant.activity_revision, expected_turn_id=grant.turn_id))
        await asyncio.wait_for(entered.wait(), 1)
        try:
            client.start_background()
            client.stream.put_nowait(None)
            await asyncio.wait_for(client.closing.wait(), 1)
            await asyncio.wait_for(client.closing.wait(), 1)
            self.assertFalse(self.events)
        finally:
            release.set()
        await stop
        self.assertTrue(await self.runtime.wait_idle(1))
        self.assertEqual(self.events, [])
        self.assertEqual(self.notices, [("detached", DEGRADED)])

    async def test_unresolved_agent_and_missing_members_degrade(self):
        from claude_agent_sdk import AssistantMessage, ToolUseBlock
        for mode in ("agent", "member"):
            with self.subTest(mode=mode):
                self.notices.clear()
                receipt = await self.submit()
                receipt.release_receipt_attempt()
                client = self.clients.calls[-1]
                client.init()
                if mode == "agent":
                    client.stream.put_nowait(AssistantMessage([ToolUseBlock("agent", "Agent", {"run_in_background": True})],
                        "fixture", session_id=client.session, user_message_uuid=receipt.turn_id))
                    client.result("success", "api_error", True, snapshot=False)
                else:
                    client.system("background_tasks_changed", tasks=[{"task_type":"local_bash", "task_id":"lost", "run_id":"run"}])
                    client.system("background_tasks_changed", tasks=[])
                    with patch("netizen_cli.runtime.claude_ordinary._RECONCILE_SECONDS", .01):
                        client.result()
                        self.assertTrue(await self.runtime.wait_idle(1))
                self.assertTrue(await self.runtime.wait_idle(1))
                self.assertEqual(self.notices, [("origin", DEGRADED)])

    async def test_replay_retires_foreground_grant_and_quota_keeps_revision(self):
        from claude_agent_sdk import RateLimitEvent, RateLimitInfo
        receipt = await self.submit()
        receipt.release_receipt_attempt()
        client = self.clients.calls[-1]
        client.init()
        await client.consumed()
        proven = await self.runtime.preview_stop(self.binding)
        client.stream.put_nowait(RateLimitEvent(RateLimitInfo("allowed"), "rate", client.session))
        await client.consumed()
        self.assertEqual(await self.runtime.preview_stop(self.binding), proven)
        client.system("task_notification", status="stopped", task_id="old", tool_use_id="old-tool")
        await client.consumed()
        with self.assertRaises(ThreadActivityChanged):
            await self.stop_using(proven)
        with self.assertRaises(OrdinaryInputRejected):
            await self.runtime.preview_stop(self.binding)
        client.init(user_message_uuids=[])
        await client.consumed()
        with self.assertRaises(OrdinaryInputRejected):
            await self.runtime.preview_stop(self.binding)
        self.assertEqual(client.interrupts, 0)

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
                if status is None:
                    self.assertEqual(self.notices[-1], ("origin", DEGRADED))
                    continue
                event = self.events[-1]
                self.assertEqual(event.status, status)
                self.assertNotIn("raw secret", repr(event))
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
                    self.assertEqual(client.interrupts, int(mode is not None))
                    self.assertEqual(self.notices[-1], ("origin", DEGRADED))

    async def test_permission_notice_precedes_only_its_attributed_result(self):
        denied = [{"tool_name": "Bash", "tool_input": {"command": "private-command"}},
                  {"tool_name": "Write", "tool_input": {"file_path": "/private/path"}}]
        summary = "本次有 2 个操作被 Claude Code 的权限设置拒绝（Bash、Write）。如需允许，请在 Claude Code 设置中调整权限规则。"
        seen_at_completion = []
        async def complete(event):
            seen_at_completion.append((event.final_response if isinstance(event, OrdinaryConclusion) else str(event.error), list(self.notices)))
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
                client.init(mode)
                client.init(mode)
                client.result(user_message_uuid="foreign", permission_denials=denied)
                client.result(user_message_uuid=None, origin={"kind": "task-notification"}, permission_denials=denied)
                client.result(permission_denials=denials)
                client.result(permission_denials=denied)
                receipt.release_receipt_attempt()
                self.assertTrue(await self.runtime.wait_idle(1))
                answer = DEGRADED if isinstance(denials, str) else "answer"
                self.assertEqual(seen_at_completion, [(answer, [("origin", text) for text in expected])])
                self.assertEqual(self.notices, seen_at_completion[0][1])

    async def test_stream_ending_without_work_uses_ordinary_unknown(self):
        receipt = await self.submit()
        receipt.release_receipt_attempt()
        client = self.clients.calls[-1]
        client.init("default")
        client.stream.put_nowait(None)
        self.assertTrue(await self.runtime.wait_idle(1))
        self.assertEqual(self.notices, [("origin", DEFAULT_MODE)])
        self.assertEqual([str(e.error) for e in self.events], [UNKNOWN])

    async def test_foreign_result_can_precede_our_attributed_result_and_duplicate_is_not_delivered(self):
        receipt = await self.submit()
        client = self.clients.calls[-1]
        client.init()
        client.result(user_message_uuid="other", result="foreign")
        client.result(user_message_uuid=None, user_message_uuids=[receipt.turn_id], result="ours")
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
                with self.assertRaisesRegex(OrdinaryInputRejected, "正在收尾"):
                    await self.submit()
                client.close_gate.set()
                self.assertTrue(await self.runtime.wait_idle(1))
                if cause in ("wrong-init-cwd", "foreign-session"):
                    self.assertEqual(self.notices[-1], ("origin", DEGRADED))
                else:
                    self.assertEqual((self.events[-1].turn_id, str(self.events[-1].error)), (receipt.turn_id, UNKNOWN))
                self.assertEqual(len(client.inputs), 1)
        self.assertEqual((len(self.events), len(self.notices)), (3, 2))
        self.assertEqual(len({client.session for client in self.clients.calls}), 1)

    async def test_start_and_query_failures_have_distinct_receipts(self):
        self.clients.configure = lambda c: setattr(c, "failure", "connect")
        with self.assertRaisesRegex(OrdinaryInputRejected, START_FAILED):
            await self.submit()
        self.assertIsNone(self.store.get(self.binding.id).native_thread_id)
        self.assertEqual(self.clients.calls[-1].closes, 1)
        self.clients.configure = lambda c: setattr(c, "failure", "query")
        with self.assertRaisesRegex(TerminalStateUnknown, DEGRADED):
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
        self.assertEqual(self.notices, [("origin", CLOSE_FAILED)])
        self.assertEqual([str(e.error) for e in self.events], [UNKNOWN])
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
            client.init()
            client.result(user_message_uuid="other")
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

    async def test_startup_stop_preview_expires_on_write_and_cancelled_interrupt_propagates(self):
        self.clients.configure = lambda c: c.connect_gate.clear()
        submit = asyncio.create_task(self.submit())
        await asyncio.wait_for(self.clients.created.wait(), 1)
        client = self.clients.calls[-1]
        preview = await self.runtime.preview_stop(self.binding)
        fields = dict(expected_activity_revision=preview.activity_revision, expected_turn_id=preview.turn_id)
        client.connect_gate.set()
        receipt = await submit
        receipt.release_receipt_attempt()
        with self.assertRaises(ThreadActivityChanged):
            await self.runtime.stop_exact(self.binding.id, **fields)
        client.init()
        await client.consumed()
        preview = await self.runtime.preview_stop(self.binding)
        fields = dict(expected_activity_revision=preview.activity_revision, expected_turn_id=preview.turn_id)
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
        active.init()
        await active.consumed()
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
        self.assertEqual([str(e.error) for e in self.events], [UNKNOWN, UNKNOWN])

    async def test_stale_preview_and_changed_settings_reject_before_native_action(self):
        admission = await self.runtime.capture_submission_admission(self.binding.id)
        with self.assertRaises(OrdinaryInputRejected):
            await self.submit(admission=replace(admission, feedback_revision=2))
        first = await self.submit()
        self.clients.calls[-1].init()
        await self.clients.calls[-1].consumed()
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

            option = "CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS"
            for explicit in (None, "0", "1"):
                background = ClaudeBackground("session", "human")
                with patch.dict("os.environ", {} if explicit is None else {option: explicit}, clear=True):
                    clients.create("session", self.cwd, resume=True, background=background)
                options = create.call_args.kwargs["options"]
                self.assertEqual(options.env, {option: "1"} if explicit is None else {})
                self.assertEqual(background.degraded, explicit == "0")
                self.assertEqual(options.hooks["Stop"][0].hooks, [background.stop_hook])

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
        self.assertEqual([str(e.error) for e in self.events], [UNKNOWN])
        self.assertEqual(self.notices, [])
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
        self.assertEqual(str(self.events[-1].error), UNKNOWN)
        self.clients.calls[-1].init()
        await self.clients.calls[-1].consumed()
        preview = await self.runtime.preview_stop(self.binding)
        self.assertEqual(preview.turn_id, second.turn_id)
        await self.finish(second)
        self.assertEqual([e.turn_id for e in self.events], [first.turn_id, second.turn_id])

    async def test_missing_init_never_interrupts_and_unattributed_result_has_bounded_reconciliation(self):
        first = await self.submit()
        first.release_receipt_attempt()
        client = self.clients.calls[-1]
        client.result()
        self.assertTrue(await self.runtime.wait_idle(1))
        self.assertEqual(client.interrupts, 0)
        self.assertEqual(self.notices[-1][1], DEGRADED)
        with patch("netizen_cli.runtime.claude_ordinary._RECONCILE_SECONDS", 0.01):
            second = await self.submit()
            second.release_receipt_attempt()
            client = self.clients.calls[-1]
            client.init()
            client.result(user_message_uuid=None)
            self.assertTrue(await self.runtime.wait_idle(1))
        self.assertEqual(self.notices[-1][1], DEGRADED)

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
        client.init()
        await client.consumed()
        preview = await self.runtime.preview_stop(self.binding)
        fields = dict(expected_activity_revision=preview.activity_revision, expected_turn_id=preview.turn_id)
        acknowledged, release = asyncio.Event(), asyncio.Event()

        async def acknowledge():
            acknowledged.set()
            await release.wait()

        stop = asyncio.create_task(self.runtime.stop_exact(self.binding.id, acknowledge=acknowledge, **fields))
        await asyncio.wait_for(acknowledged.wait(), 1)
        self.assertEqual(client.interrupts, 1)
        preview = await self.runtime.preview_stop(self.binding)
        fields = dict(expected_activity_revision=preview.activity_revision, expected_turn_id=preview.turn_id)
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
        with patch("netizen_cli.runtime.claude_ordinary._RECONCILE_SECONDS", .03):
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
        with patch("netizen_cli.runtime.claude_ordinary._RECONCILE_SECONDS", .03):
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
        self.assertEqual(self.notices[-1][1], DEGRADED)

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
        client.init()
        await client.consumed()
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
        client.init()
        await client.consumed()
        preview = await self.runtime.preview_stop(self.binding)
        fields = dict(expected_activity_revision=preview.activity_revision, expected_turn_id=preview.turn_id)
        with patch.object(client, "interrupt", new=AsyncMock(side_effect=OSError("private"))):
            with self.assertRaisesRegex(TerminalStateUnknown, "无法确认"):
                await self.runtime.stop_exact(self.binding.id, **fields)
        with self.assertRaises(OrdinaryInputRejected):
            await self.submit()
        with self.assertRaises(ThreadActivityChanged):
            await self.runtime.stop_exact(self.binding.id, **fields)
        preview = await self.runtime.preview_stop(self.binding)
        fields = dict(expected_activity_revision=preview.activity_revision, expected_turn_id=preview.turn_id)
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
            with self.assertRaisesRegex(TerminalStateUnknown, DEGRADED):
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

    async def test_typed_exit_readmits_only_after_old_sdk_disconnect(self):
        from claude_agent_sdk import ProcessError

        first = await self.submit(origin="lost")
        first.release_receipt_attempt()
        old = self.clients.calls[0]
        old.close_gate.clear()
        try:
            old.init()
            old.stop_snapshot(["shell"], ["cron"])
            old.stream.put_nowait(ProcessError("fixture", exit_code=-9))
            await asyncio.wait_for(old.closing.wait(), 1)
            with self.assertRaises(OrdinaryInputRejected):
                await self.submit(origin="early")
            old.close_gate.set()
            self.assertTrue(await self.runtime.wait_idle(1))
            second = await self.submit(origin="next")
            self.assertEqual(first.thread_id, second.thread_id)
            await self.finish(second)
            self.assertEqual(self.notices, [("lost", DEGRADED)])
            self.assertEqual([(e.origin, e.turn_id) for e in self.events], [("next", second.turn_id)])
            self.assertEqual(old.interrupts, 0)
            self.assertFalse(self.runtime.local_snapshot(self.binding.id).unavailable)
        finally:
            old.close_gate.set()
