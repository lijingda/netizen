"""Explore the public Claude input, reader, stop and delivery boundaries at native trace cuts."""
import asyncio
from collections import Counter
from contextlib import ExitStack, asynccontextmanager, closing
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from claude_agent_sdk import AssistantMessage, ProcessError, RateLimitEvent, RateLimitInfo, StreamEvent, ToolResultBlock, ToolUseBlock, UserMessage

from netizen_cli.bindings import BindingStore
from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.ordinary_sessions import OrdinaryConclusion, OrdinaryInputRejected, PreparedPrompt
from netizen_cli.runtime.claude_client import BundledClaudeClients, ClaudeBackground
from netizen_cli.runtime.claude_ordinary import ClaudeOrdinarySessions, DEGRADED, UNKNOWN
from netizen_cli.runtime.contracts import TerminalStateUnknown, ThreadActivityChanged, TurnObservationUnavailableOutcome
from tests.support.claude_client import FakeClaudeClients
from tests.support.claude_mutants import MUTANTS
from tests.support.claude_explorer import (BATCH_WAKE, BUFFERED, TWO_TASKS, FAMILIES, LEDGER_CASES, LEDGER_FRAMES, callback_schedules,
                                          emit, evidence_frame, malformed, shape_cases, uid, unresolved_at_cut)


SINGLE = ("replay", "human_init", "tool", "members", "start", "first_stop", "first",
          "pending_idle", "terminal", "empty", "wake_init", "wake_stop", "wake", "idle")
ACTIONS = ("stop", "abandon", "input", "close_stream", "shutdown", "cancel_delivery", "slow_delivery", "cancel_submit")


def traces():
    # Single, early terminal and cold replay are the orders observed at CLI 2.1.329.
    rng = random.Random(329)
    cases = [(SINGLE, cut, action) for cut in range(len(SINGLE) + 1) for action in ACTIONS]
    cases.append((SINGLE, 0, "observe"))
    early = list(SINGLE)
    early.remove("terminal")
    early.remove("empty")
    early[5:5] = ["terminal", "empty"]
    cases.append((tuple(early), 0, "observe"))
    for frame in SINGLE:
        if frame not in ("first_stop", "wake_stop"):
            cases.append((tuple(x for x in SINGLE if x != frame), 0, "observe"))
        index = SINGLE.index(frame)
        cases.append((SINGLE[:index] + (frame,) + SINGLE[index:], 0, "observe"))
    for frame in ("first", "wake"):
        for corruption in ("_no_uuid", "_bad_uuid"):
            cases.append((tuple(x + corruption if x == frame else x for x in SINGLE), 0, "observe"))
    for mode in ("failed", "stopped"):
        cases.append((("human_init", mode, "idle"), 0, "observe"))
    cases.append((("human_init", "members", "empty", "first_stop", "first", "idle"), 0, "observe"))
    rng.shuffle(cases)
    for frames, cut, action in cases:
        # Native quota observations can arrive between arbitrary frames.
        index = rng.randrange(len(frames) + 1)
        yield frames[:index] + ("rate",) + frames[index:], cut + (index < cut), action


def send_frame(client, name, *, terminal_seen=False):
    sid = client.session
    human = client.inputs[0]["uuid"]
    if name == "replay":
        client.system("task_notification", status="stopped", task_id="previous", tool_use_id="previous-tool")
    elif name in ("human_init", "wake_init"):
        client.system("session_state_changed", state="running")
        client.system("init", permissionMode="auto", cwd=str(client.cwd), uuid="00000000-0000-4000-8000-00000000000" +
                      ("1" if name == "human_init" else "2"), user_message_uuids=[human] if name == "human_init" else [])
    elif name == "tool":
        client.stream.put_nowait(AssistantMessage([ToolUseBlock("tool", "Bash", {"run_in_background": True})],
                                                  "fixture", session_id=sid, user_message_uuid=human))
    elif name in ("members", "empty"):
        rows = [{"task_type": "local_bash", "task_id": "task", "run_id": "run"}] if name == "members" else []
        client.system("background_tasks_changed", tasks=rows)
    elif name == "start":
        client.system("task_started", run_id="run", task_id="task", tool_use_id="tool", task_type="local_bash", is_backgrounded=True)
    elif name in ("first_stop", "wake_stop"):
        client.prompt_id = "prompt-" + name
        client.stop_snapshot(["task"] if name == "first_stop" and not terminal_seen else [])
    elif name.startswith("first") or name.startswith("wake"):
        is_wake = name.startswith("wake")
        result = client.wake_result if is_wake else client.result
        uid = "00000000-0000-4000-8000-00000000000" + ("4" if is_wake else "3")
        if name.endswith("_no_uuid"):
            uid = None
        elif name.endswith("_bad_uuid"):
            uid = "invalid-result"
        result(uuid=uid,
               result="wake answer" if is_wake else "first answer", snapshot=False, idle=False)
    elif name == "terminal":
        client.system("task_updated", task_id="task", run_id="run", patch={"status": "completed"})
    elif name in ("idle", "pending_idle"):
        client.system("session_state_changed", state="idle")
    elif name in ("failed", "stopped"):
        client.result(subtype="error_during_execution", reason="aborted_streaming" if name == "stopped" else "api_error",
                      is_error=True, snapshot=False, idle=False)
    else:
        assert name == "rate"
        client.stream.put_nowait(RateLimitEvent(RateLimitInfo("allowed"), "rate", sid))


class SequenceTest(unittest.IsolatedAsyncioTestCase):
    async def test_pinned_sequences_and_interleaved_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            store = BindingStore()
            self.addCleanup(store.close)
            store.bootstrap_project(alias="test", cwd=str(cwd))
            for i, (frames, cut, action) in enumerate(traces()):
                with self.subTest(frames=frames, cut=cut, action=action):
                    await self.run_trace(store, cwd, str(i), frames, cut, action)

    async def test_cold_failure_then_restored_cron_inventory(self):
        for mode in ("stopped", "failed"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                cwd = Path(directory)
                store = BindingStore()
                self.addCleanup(store.close)
                store.bootstrap_project(alias="test", cwd=str(cwd))
                binding = store.create_channel_binding(scope=FeishuScope("app", mode, ScopeKind.DIRECT),
                    project_alias="test", creator_id="owner")
                session = str(uuid4())
                store.assign_native_thread_id(binding.id, session)
                binding = store.get(binding.id)
                clients = FakeClaudeClients()
                clients.cwds[session] = cwd
                held = asyncio.Event()
                answers = []

                async def deliver(event):
                    answers.append(event)

                async def notify(origin, text):
                    held.set()

                clock = [0.]
                runtime = ClaudeOrdinarySessions(bindings=store, clients=clients, owner_id="owner",
                    on_completion=deliver, notice=notify, clock=lambda: clock[0])
                runtime.open_admission()
                clock[0] = 2.
                try:
                    for index in range(2):
                        admission = await runtime.capture_submission_admission(binding.id)
                        receipt = await runtime.submit(binding=binding, cwd=None, input=PreparedPrompt("fixture"),
                            owner_id="owner", origin=index, admission=admission, context_commit=None, skill_names=())
                        receipt.release_receipt_attempt()
                        client = clients.calls[index]
                        client.init()
                        if index == 0:
                            send_frame(client, mode)
                            send_frame(client, "idle")
                            self.assertTrue(await runtime.wait_idle(.5))
                        else:
                            client.stop_snapshot(crons=["restored"])
                            client.result(snapshot=False)
                            await asyncio.wait_for(held.wait(), 1)
                            self.assertEqual(client.closes, 0)
                    self.assertEqual([c.session for c in clients.calls], [session, session])
                finally:
                    await runtime.cancel_tasks()

    async def run_trace(self, store, cwd, anchor, frames, cut, action):
        scope = FeishuScope("app", anchor, ScopeKind.DIRECT)
        binding = store.create_channel_binding(scope=scope, project_alias="test", creator_id="owner")
        session = str(uuid4())
        store.assign_native_thread_id(binding.id, session)
        binding = store.get(binding.id)
        clients = FakeClaudeClients()
        clients.cwds[session] = cwd
        events, notices = [], []
        release_io = asyncio.Event()
        release_io.set()
        cancel_delivery = False

        async def deliver(event):
            if isinstance(event, TurnObservationUnavailableOutcome) and str(event.error) == DEGRADED:
                notices.append((event.origin, str(event.error)))
                return
            events.append(event)
            if cancel_delivery and len(events) == 1:
                raise asyncio.CancelledError
            await release_io.wait()

        async def notify(origin, text):
            notices.append((origin, text))
            await release_io.wait()

        now = [0.]
        runtime = ClaudeOrdinarySessions(bindings=store, clients=clients, owner_id="owner",
                                        on_completion=deliver, notice=notify, clock=lambda: now[0])
        runtime.open_admission()
        now[0] = 2
        admission = await runtime.capture_submission_admission(binding.id)
        if action == "cancel_submit":
            clients.configure = lambda client: setattr(client, "failure", "query")
        submitter = asyncio.create_task(runtime.submit(binding=binding, cwd=None, input=PreparedPrompt("fixture"),
            owner_id="owner", origin=anchor, admission=admission, context_commit=None, skill_names=()))
        await clients.created.wait()
        client = clients.calls[0]
        await client.writing.wait()
        client.close_gate.clear()
        seen = []
        explicit_close = False
        last = None
        try:
            for index in range(len(frames) + 1):
                if submitter.done() and not submitter.cancelled() and not submitter.exception():
                    submitter.result().release_receipt_attempt()
                local = runtime.local_snapshot(binding.id)
                try:
                    preview = await runtime.preview_stop(binding)
                except OrdinaryInputRejected:
                    preview = None
                visible = (local.turn.state if local.turn else None, bool(preview))
                if last and last[0] != visible:
                    self.assertGreater(local.revision, last[1], "I6: visible transition reused credentials")
                last = (visible, local.revision)
                if index == cut:
                    if action == "stop" and preview:
                        try:
                            await runtime.stop_exact(binding.id, expected_activity_revision=preview.activity_revision,
                                                     expected_turn_id=preview.turn_id)
                        except (OrdinaryInputRejected, ThreadActivityChanged):
                            pass
                        if client.interrupts:
                            self.assertIn("human_init", seen, "I3: interrupt before the exact human init")
                            self.assertNotIn("tool", seen, "I3: interrupt after a background-starting tool")
                    elif action == "abandon":
                        try:
                            await runtime.abandon_background(binding.id, client.inputs[0]["uuid"], "owner")
                            explicit_close = True
                        except OrdinaryInputRejected:
                            pass
                    elif action == "input":
                        with self.assertRaises(OrdinaryInputRejected, msg="I4: old disconnect is still gated"):
                            await runtime.capture_submission_admission(binding.id)
                    elif action == "shutdown":
                        client.close_gate.set()
                        await runtime.cancel_tasks()
                        explicit_close = True
                    elif action == "close_stream":
                        client.stream.put_nowait(None)
                        async with asyncio.timeout(1):
                            while not (client.closing.is_set() or notices):
                                await asyncio.sleep(0)
                        if {"tool", "members", "start", "first"}.intersection(seen) and "wake" not in seen and "idle" not in seen:
                            await self.until(lambda: (anchor, DEGRADED) in notices, "D: EOF lost degraded notice")
                        break
                    elif action == "slow_delivery":
                        release_io.clear()
                    elif action == "cancel_delivery":
                        cancel_delivery = True
                    elif action == "cancel_submit" and not submitter.done():
                        submitter.cancel()
                        await asyncio.gather(submitter, return_exceptions=True)
                if explicit_close or index == len(frames):
                    break
                name = frames[index]
                seen.append(name)
                send_frame(client, name, terminal_seen="terminal" in seen)
                # The next iterator pull proves consumption even with blocked Feishu delivery.
                barrier = asyncio.Event()
                client.stream.put_nowait(barrier)
                async with asyncio.timeout(1):
                    while not (barrier.is_set() or client.closing.is_set() or
                               (notices and "human_init" not in frames)):
                        await asyncio.sleep(0)
                if client.closing.is_set():
                    break

            release_io.set()
            if submitter.done() and not submitter.cancelled() and not submitter.exception():
                submitter.result().release_receipt_attempt()
            if client.closing.is_set() and not explicit_close:
                # The held close keeps the generation active. Finish any Channel
                # handoff before asserting what appeared at its anchor.
                tail = runtime._active[binding.id].delivery
                if tail is not None:
                    await asyncio.wait_for(asyncio.shield(tail), .5)
            if not explicit_close and action != "close_stream":
                plain = bool({"failed", "stopped"}.intersection(seen))
                if action == "observe" and sorted(x for x in frames if x != "rate") == sorted(SINGLE):
                    self.assertTrue(client.closing.is_set(), "complete native evidence must release the client")
                if plain:
                    self.assertTrue(client.closing.is_set(), "I5: ordinary resumed failure must close")
                degraded = (anchor, DEGRADED) in notices
                if client.closing.is_set() and not plain and not degraded:
                    self.assertTrue({"idle", "wake", "terminal", "empty", "wake_init", "start"}.issubset(seen), "I2: insufficient positive close evidence")
                if not degraded and "wake" in seen and {"human_init", "first", "start", "tool", "terminal", "wake_init"}.issubset(seen):
                    async with asyncio.timeout(1):
                        while len(events) < 2:
                            await asyncio.sleep(0)
                    self.assertEqual(events[-1].final_response, "wake answer", "I1: attributed wake lost")
                if {"wake_no_uuid", "wake_bad_uuid", "first_no_uuid", "first_bad_uuid"}.intersection(seen):
                    self.assertIn((anchor, DEGRADED), notices)
            identities = [(e.turn_id, e.origin) for e in events if isinstance(e, OrdinaryConclusion)]
            self.assertEqual(len(identities), len(set(identities)), "I1: repeated delivery identity")
            self.assertTrue(all(e.origin == anchor for e in events))
            self.assertTrue(all(origin == anchor for origin, _ in notices))
            if not explicit_close:
                with self.assertRaises(OrdinaryInputRejected, msg="I4: client may still be connected"):
                    await runtime.capture_submission_admission(binding.id)
        finally:
            release_io.set()
            client.close_gate.set()
            if not submitter.done():
                submitter.cancel()
            await asyncio.gather(submitter, return_exceptions=True)
            await runtime.cancel_tasks()

    async def test_extended_families(self):
        with tempfile.TemporaryDirectory() as directory:
            store = BindingStore()
            self.addCleanup(store.close)
            store.bootstrap_project(alias="test", cwd=directory)
            for family, scenario in FAMILIES.items():
                for action in ("observe", *ACTIONS, "process_exit"):
                    for cut in range(len(scenario.frames) + 1) if action != "observe" else (0,):
                        with self.subTest(family=family, action=action, cut=cut):
                            await self.explore(store, Path(directory), family, cut, action)

    async def explore(self, store, cwd, family, cut, action):
        if family.startswith("shape-"):
            kind, path, index = family[6:].split("/")
            return await self.shape_trace(store, cwd, kind, path, int(index), cut, action)
        if family.startswith("replay-"):
            return await self.replay_trace(store, cwd, family[7:])
        if family.startswith("ledger-"):
            return await self.ledger_trace(store, cwd, family[7:], action, cut)
        if family.startswith("proof-"):
            return await self.proof_trace(store, cwd, family[6:])
        with ExitStack() as patches:
            if family in ("slow_batch", "deadline", "lost_write"):
                patches.enter_context(patch("netizen_cli.runtime.claude_ordinary._RECONCILE_SECONDS", .06))
            if family == "close_timeout":
                patches.enter_context(patch("netizen_cli.runtime.claude_ordinary._CLOSE_SECONDS", .05))
            await self._explore(store, cwd, family, cut, action)

    async def _explore(self, store, cwd, family, cut, action):
        scenario = FAMILIES[family]
        if family == "restart_replay" and action == "observe":
            with patch("netizen_cli.runtime.claude_client.verified_claude_cli", return_value=cwd), \
                    patch("netizen_cli.claude_installation.CLI_VERSION", "next"):
                with self.assertRaises(RuntimeError, msg="I2: unsupported pin admitted the adapter"):
                    BundledClaudeClients()
        anchor = f"{family}/{action}/{cut}"
        binding = store.create_channel_binding(scope=FeishuScope("app", anchor, ScopeKind.DIRECT),
            project_alias="test", creator_id="owner")
        clients = FakeClaudeClients()
        if family in ("failure", "restart_replay"):
            session = str(uuid4())
            store.assign_native_thread_id(binding.id, session)
            clients.cwds[session] = cwd
            binding = store.get(binding.id)
        deliveries, notices = [], []
        release = asyncio.Event()
        release.set()
        cancelled = False
        completed = []
        if family == "close_timeout":
            release.clear()
        release_notice = asyncio.Event()
        if family != "held_notice":
            release_notice.set()

        async def deliver(event):
            if isinstance(event, TurnObservationUnavailableOutcome) and str(event.error) == DEGRADED:
                notices.append((event.origin, str(event.error)))
                return
            deliveries.append(event)
            if cancelled and len(deliveries) == 1:
                raise asyncio.CancelledError
            await release.wait()
            completed.append(event)

        async def notice(origin, text):
            notices.append((origin, text))
            await release_notice.wait()

        clock = [0.]
        runtime = ClaudeOrdinarySessions(bindings=store, clients=clients, owner_id="owner", on_completion=deliver,
                                   notice=notice, clock=lambda: clock[0])
        runtime.open_admission()
        clock[0] = 2.
        admission = await runtime.capture_submission_admission(binding.id)
        if action == "cancel_submit" or family in ("lost_write", "detached"):
            clients.configure = lambda c: setattr(c, "failure", "query")
        submit = asyncio.create_task(runtime.submit(binding=binding, cwd=cwd, input=PreparedPrompt("fixture"),
            owner_id="owner", origin=anchor, admission=admission, context_commit=None, skill_names=()))
        await clients.created.wait()
        client = clients.calls[0]
        await client.writing.wait()
        if family == "detached":
            submit.cancel()
            await asyncio.gather(submit, return_exceptions=True)
        client.close_gate.clear()
        human = client.inputs[0]["uuid"]
        previous = stale_preview = None
        seen = []
        end = False
        try:
            for i in range(len(scenario.frames) + 1):
                if submit.done() and not submit.cancelled() and not submit.exception():
                    submit.result().release_receipt_attempt()
                local = runtime.local_snapshot(binding.id)
                try:
                    preview = await runtime.preview_stop(binding)
                except OrdinaryInputRejected:
                    preview = None
                # Public input refusal distinguishes a presently releasable generation from
                # a speculative stop hint. Physical identity comes from the native init trace.
                with self.assertRaises(OrdinaryInputRejected, msg="I4: delivery released an active Binding") as refusal:
                    await runtime.capture_submission_admission(binding.id)
                can_release = "/stop abandon-background" in str(refusal.exception)
                physical = next((x for x in reversed(seen) if x in ("wake_a", "wake_b")),
                                "human" if "human" in seen else None)
                members = next((x for x in reversed(seen) if x.startswith("members_") or x in ("initial_members", "empty")), None)
                if members == "initial_members":
                    members = "members_a"
                stop_identity = None
                if preview:
                    stop_identity = (preview.acknowledgement, members if "后台任务" in preview.acknowledgement else preview.turn_id)
                signature = (local.turn.state, local.unavailable, physical, stop_identity, human if can_release else None)
                if previous and signature != previous[0]:
                    self.assertGreater(local.revision, previous[1], "I6: visible control or physical turn changed")
                previous = (signature, local.revision)
                if (family in ("single", "duplicates", "batch", "reverse") and len(seen) >= 2
                        and seen[-2:] in (["first", "idle"], ["answer_b", "idle"])):
                    latest = next((x for x in reversed(seen) if x.startswith("members_")), None)
                    if latest == "members_a":
                        self.assertIsNotNone(preview, "I3: single native task lost its stop grant")
                if family in ("plain", "failure") and ("first" in seen or "failed" in seen):
                    self.assertIn("收尾", str(refusal.exception), "I5: plain Result displayed background work")
                if preview and stale_preview and stale_preview.activity_revision != local.revision:
                    with self.assertRaises(ThreadActivityChanged, msg="I3: stale preview stopped another target"):
                        await runtime.stop_exact(binding.id, expected_activity_revision=stale_preview.activity_revision,
                                                 expected_turn_id=stale_preview.turn_id)
                if preview and stale_preview is None:
                    stale_preview = preview
                if i == cut:
                    if action in ("close_stream", "process_exit") and not client.closing.is_set():
                        typed = action == "process_exit"
                        client.stream.put_nowait(ProcessError("fixture exit", exit_code=-9) if typed else None)
                        await self.until(client.closing.is_set, "I2: reader ending did not disconnect")
                        if unresolved_at_cut(seen):
                            release.set()  # Allow an already handed-off body to precede the terminal.
                            await self.until(lambda: (anchor, DEGRADED) in notices or
                                (submit.done() and not submit.cancelled() and str(submit.exception()) == DEGRADED),
                                "D: unresolved work failed to degrade")
                        with self.assertRaises(OrdinaryInputRejected):
                            await runtime.capture_submission_admission(binding.id)
                        end = True
                    elif action == "stop" and preview:
                        self.assertEqual(preview.turn_id, human)
                        await runtime.stop_exact(binding.id, expected_activity_revision=preview.activity_revision,
                                            expected_turn_id=preview.turn_id)
                        if client.stops:
                            self.assertIn(members, ("members_a", "members_b"), "I3: ambiguous native task")
                            self.assertEqual(client.stops, ["task-" + members[-1]], "I3: wrong task")
                            current = await runtime.preview_stop(binding)
                            again = await runtime.stop_exact(binding.id, expected_activity_revision=current.activity_revision,
                                                             expected_turn_id=current.turn_id)
                            self.assertEqual((again.value, len(client.stops)), ("stopping", 1), "I3: duplicate stop_task")
                        else:
                            self.assertEqual((client.interrupts, client.inputs[0]["uuid"]), (1, human))
                            self.assertTrue("human" in seen and "first" not in seen and not unresolved_at_cut(seen),
                                            "I3: interrupt outside proven human execution")
                    elif action == "abandon":
                        with self.assertRaises(OrdinaryInputRejected, msg="I3: stale abandon generation accepted"):
                            await runtime.abandon_background(binding.id, uid(99), "owner")
                        if can_release:
                            receipt = await runtime.abandon_background(None, human, "owner")
                            self.assertIn(human, receipt, "I3: abandon targeted another generation")
                            end = True
                        else:
                            with self.assertRaises(OrdinaryInputRejected):
                                await runtime.abandon_background(binding.id, None, "owner")
                    elif action == "input":
                        with self.assertRaises(OrdinaryInputRejected):
                            await runtime.submit(binding=binding, cwd=cwd, input=PreparedPrompt("second"), owner_id="owner",
                                            origin="second", admission=admission, context_commit=None, skill_names=())
                        self.assertEqual(len(clients.calls), 1, "I4: busy input opened another client")
                    elif action == "shutdown":
                        client.close_gate.set()
                        await runtime.cancel_tasks()
                        end = True
                    elif action == "cancel_submit" and not submit.done():
                        submit.cancel()
                        await asyncio.gather(submit, return_exceptions=True)
                    elif action == "cancel_delivery":
                        cancelled = not deliveries
                    elif action == "slow_delivery":
                        release.clear()
                if end or i == len(scenario.frames) or client.closing.is_set():
                    break
                frame = scenario.frames[i]
                seen.append(frame)
                if frame == "replay_result":
                    emit(client, frame)
                    await client.consumed()
                    self.assertFalse(deliveries, "I1: delivered a prior generation's replay")
                    continue
                if frame in ("slow_wake", "slow_human"):
                    self.assertFalse(await runtime.wait_idle(.10), "I1: native turn timed out after acceptance")
                    self.assertFalse(client.closing.is_set(), "I1: native turn timed out after acceptance")
                    self.assertFalse(notices, "I2: healthy native work was quarantined")
                    with self.assertRaises(OrdinaryInputRejected, msg="I2: healthy work was abandonable"):
                        await runtime.abandon_background(binding.id, human, "owner")
                    continue
                if frame == "quota_window":
                    loop = asyncio.get_running_loop()
                    until = loop.time() + .15
                    while loop.time() < until:
                        send_frame(client, "rate")
                        await client.consumed()
                        await asyncio.sleep(.002)
                    self.assertTrue(client.closing.is_set(), "I2: unrelated traffic changed the proof deadline")
                    continue
                emit(client, frame)
                barrier = asyncio.Event()
                client.stream.put_nowait(barrier)
                await self.until(lambda: barrier.is_set() or client.closing.is_set(), "I7: reader stalled")
            if not end and family == "close_timeout" and not release.is_set():
                before = runtime.local_snapshot(binding.id)
                await self.until(lambda: runtime.local_snapshot(binding.id).unavailable, "I6: missing close quarantine")
                quarantined = runtime.local_snapshot(binding.id)
                self.assertGreater(quarantined.revision, before.revision, "I6: close quarantine reused revision")
                client.close_gate.set()
                await self.until(lambda: not runtime.local_snapshot(binding.id).unavailable, "I6: late close not reported")
                self.assertGreater(runtime.local_snapshot(binding.id).revision, quarantined.revision, "I6: late close reused revision")
                # Keep H1's Feishu send blocked with its successor queued; a single-attempt
                # cancellation may already have allowed the second send to start.
                # Bound shutdown independently of the held send's ordinary 30s I/O timeout.
                shutdown = asyncio.create_task(runtime.cancel_tasks())
                try:
                    await self.until(shutdown.done, "I4: Feishu send still blocked during shutdown")
                    await shutdown
                finally:
                    release.set()
                    await asyncio.gather(shutdown, return_exceptions=True)
                self.assertTrue(await runtime.wait_idle(0), "I4: shutdown left pending deliveries")
                old = list(completed)
                release.set()
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                self.assertEqual(completed, old, "I4: Feishu send completed after shutdown")
                end = True
            if not end:
                release.set()
                anomaly = family.startswith("conflict_") or family in ("foreign", "held_notice", "deadline", "mixed_inventory")
                if anomaly:
                    await self.until(client.closing.is_set, "D: no disconnect")
                    if submit.done() and not submit.cancelled() and not submit.exception():
                        submit.result().release_receipt_attempt()
                    release.set()
                    await self.until(lambda: notices, "D: missing or repeated notice")
                    self.assertEqual(notices, [(anchor, DEGRADED)], "D: missing or repeated notice")
                    self.assertTrue(all(isinstance(e, OrdinaryConclusion) and e.turn_id == human for e in deliveries))
                    return
                expected = list(scenario.results)
                # A cancelled submitter still owns the receipt and its original anchor.
                if expected:
                    await self.until(lambda: len(deliveries) >= len(expected), "I1: attributable disposition lost")
                # The Channel uses the human input ID for H1 and the native Result ID
                # for wakes. Join each actual delivery back to its emitted fixture Result.
                native = {"first": (OrdinaryConclusion, human, "completed"), "failed": (OrdinaryConclusion, human, "failed"),
                          "answer_a": (OrdinaryConclusion, uid(24), "completed"), "answer_b": (OrdinaryConclusion, uid(25), "completed"),
                          "unknown_a": (TurnObservationUnavailableOutcome, "run-a", None),
                          "unknown_h": (TurnObservationUnavailableOutcome, human, None)}
                native["interrupted"] = (OrdinaryConclusion, human, "interrupted")
                identities = [(type(e), e.turn_id, getattr(e, "status", None), e.origin) for e in deliveries]
                self.assertEqual(Counter(identities), Counter((*native[x], anchor) for x in expected), "I1: delivery attribution")
                for event in deliveries:
                    if isinstance(event, OrdinaryConclusion) and event.status == "completed":
                        self.assertEqual(event.final_response, "same body", "I1: wrong Result body")
                self.assertEqual(client.closing.is_set(), scenario.closes, "I2: wrong close disposition")
            self.assertTrue(all(origin == anchor for origin, _ in notices))
        finally:
            release_notice.set()
            release.set()
            client.close_gate.set()
            if not submit.done():
                submit.cancel()
            await asyncio.gather(submit, return_exceptions=True)
            await runtime.cancel_tasks()

    @asynccontextmanager
    async def fixture(self, store, cwd, label, *, hold_receipt=False, clients=None, resumed=False):
        binding = store.create_channel_binding(scope=FeishuScope("app", str(uuid4()), ScopeKind.DIRECT),
                                              project_alias="test", creator_id="owner")
        events, notices = [], []
        async def deliver(event):
            if isinstance(event, TurnObservationUnavailableOutcome) and str(event.error) == DEGRADED:
                notices.append((event.origin, str(event.error)))
                return
            events.append(event)
        async def notice(origin, body):
            notices.append((origin, body))
        clients = clients or FakeClaudeClients()
        self.clients, self.store = clients, store
        if resumed:
            session = str(uuid4())
            store.assign_native_thread_id(binding.id, session)
            clients.cwds[session] = cwd
            binding = store.get(binding.id)
        clock = [0.]
        runtime = ClaudeOrdinarySessions(bindings=store, clients=clients, owner_id="owner",
            on_completion=deliver, notice=notice, clock=lambda: clock[0])
        runtime.open_admission()
        clock[0] = 2.
        receipt = await runtime.submit(binding=binding, cwd=cwd, input=PreparedPrompt("fixture"),
            owner_id="owner", origin=label, admission=await runtime.capture_submission_admission(binding.id),
            context_commit=None, skill_names=())
        if not hold_receipt:
            receipt.release_receipt_attempt()
        try:
            yield clients.calls[0], runtime, binding, events, notices, receipt
        finally:
            for client in clients.calls:
                client.close_gate.set()
            try:
                await runtime.cancel_tasks()
            except RuntimeError:
                if not runtime.local_snapshot(binding.id).unavailable:
                    raise

    async def ledger_trace(self, store, cwd, kind, action, cut):
        original, other = LEDGER_CASES[kind]
        label = (kind, action, cut)
        async with self.fixture(store, cwd, label) as (c, runtime, binding, events, notices, receipt):
            human = receipt.turn_id
            for index in range(len(LEDGER_FRAMES) + 1):
                if index == cut:
                    c.close_gate.clear()
                    for _ in range(2):
                        evidence_frame(c, other if action == "conflict" else original)
                    if action == "conflict":
                        before = [identity for frame, identity in (("first", human), ("answer_a", uid(24)))
                                  if LEDGER_FRAMES.index(frame) < cut]
                        await self.assert_degraded(c, runtime, binding, events, notices, human, label, before=before)
                        return
                    await c.consumed()
                    c.close_gate.set()
                if index == len(LEDGER_FRAMES):
                    break
                frame = LEDGER_FRAMES[index]
                evidence_frame(c, frame)
                barrier = asyncio.Event()
                c.stream.put_nowait(barrier)
                await self.until(lambda: barrier.is_set() or c.closes, "I7: ledger reader stalled")
                self.assertFalse(c.closes, "I1: exact replay degraded")
                if frame in ("first", "answer_a"):
                    identity = human if frame == "first" else uid(24)
                    await self.until(lambda: any(e.turn_id == identity for e in events), "I1: attributable disposition lost")
            self.assertEqual([(e.turn_id, e.origin, e.final_response) for e in events],
                             [(human, label, "same body"), (uid(24), label, "same body")], "I1: ledger outcome identity")
            self.assertEqual((len(c.inputs), c.consumers, c.closes), (1, 1, 0), "I2: registered cron lost")

    async def test_ledger_replays_and_conflicts_at_every_later_cut(self):
        with tempfile.TemporaryDirectory() as directory, closing(BindingStore()) as store:
            cwd = Path(directory)
            store.bootstrap_project(alias="test", cwd=directory)
            for kind, (original, _) in LEDGER_CASES.items():
                for cut in range(LEDGER_FRAMES.index(original) + 1, len(LEDGER_FRAMES) + 1):
                    for action in ("duplicate", "conflict"):
                        with self.subTest(kind=kind, cut=cut, action=action):
                            await self.ledger_trace(store, cwd, kind, action, cut)

    async def test_callbacks_at_each_buffered_reader_cut(self):
        with tempfile.TemporaryDirectory() as directory, closing(BindingStore()) as store:
            cwd = Path(directory)
            store.bootstrap_project(alias="test", cwd=directory)
            for h, w in callback_schedules():
                with self.subTest(h=h, w=w):
                    async with self.fixture(store, cwd, (h, w)) as (c, runtime, binding, events, notices, receipt):
                        # Queue the entire native stream before dispatching either control callback.
                        gates = []
                        for index, frame in enumerate(BUFFERED):
                            for at, name in ((h, "stop_a"), (w, "stop_empty_a")):
                                if at == index:
                                    arrived, release = asyncio.Event(), asyncio.Event()
                                    c.stream.put_nowait((arrived, release))
                                    gates.append((name, arrived, release))
                            emit(c, frame)
                        for name, arrived, release in gates:
                            await self.until(arrived.is_set, "I7: reader missed callback cut")
                            emit(c, name)
                            release.set()
                        self.assertTrue(await runtime.wait_idle(.5), "I2: attributed handoff remained held")
                        self.assertEqual([(e.turn_id, e.origin) for e in events], [(c.inputs[0]["uuid"], (h, w)), (uid(24), (h, w))])
                        self.assertEqual((c.closes, c.consumers, len(c.inputs)), (1, 1, 1))

    async def proof_trace(self, store, cwd, kind):
        if kind == "cold_replay_conflict":
            return await self.cold_replay_conflict(store, cwd)
        if kind in ("lost_receipt_conflict", "quarantine_deadline", "unattributed_init"):
            with patch("netizen_cli.runtime.claude_ordinary._RECONCILE_SECONDS", .08):
                return await self.lost_receipt_trace(store, cwd, kind)
        async with self.fixture(store, cwd, kind, hold_receipt=kind in ("receipt_order", "queued_conflict", "terminal_receipt")) as (c, runtime, binding, events, notices, receipt):
            human = c.inputs[0]["uuid"]
            if kind == "terminal_receipt":
                c.system("session_state_changed", state=17)
                await self.until(c.closing.is_set, "D: no disconnect")
                try:
                    self.assertFalse(await runtime.wait_idle(.02), "D: terminal bypassed input receipt")
                    self.assertFalse(notices, "D: terminal bypassed input receipt")
                finally:
                    receipt.release_receipt_attempt()
                self.assertTrue(await runtime.wait_idle(.5))
                self.assertEqual(notices, [(kind, DEGRADED)])
                return
            if kind == "task_launcher":
                c.init()
                c.stream.put_nowait(AssistantMessage([ToolUseBlock("launch", "Task", {"run_in_background": True})],
                                                     "model", session_id=c.session))
                await c.consumed()
                c.stream.put_nowait(None)
                self.assertTrue(await runtime.wait_idle(.5))
                self.assertEqual(notices, [(kind, DEGRADED)], "I2: native Task launch ignored")
                return
            if kind == "handoff_order":
                # Block inside Channel completion, after the runtime handed H1 over.
                entered, finish = asyncio.Event(), asyncio.Event()
                order = []
                async def complete(outcome):
                    if isinstance(outcome, OrdinaryConclusion):
                        entered.set()
                        await finish.wait()
                        order.append(outcome.final_response)
                    else:
                        order.append((outcome.origin, outcome.turn_id, str(outcome.error)))
                runtime._completion = complete
                c.init()
                c.result(idle=False)
                await self.until(entered.is_set, "I1: no Channel handoff")
                old_task = runtime._active[binding.id].task
                try:
                    c.system("session_state_changed", state=17)
                    await self.until(lambda: not runtime.local_snapshot(binding.id).turn, "D: delivery delayed readmission")
                    self.assertFalse(order or notices, "D: terminal overtook handed-off body")
                    fresh = store.get(binding.id)
                    next_turn = await runtime.submit(binding=fresh, cwd=c.cwd, input=PreparedPrompt("next"), owner_id="owner",
                        origin="new", admission=await runtime.capture_submission_admission(binding.id), context_commit=None, skill_names=())
                    next_turn.release_receipt_attempt()
                finally:
                    finish.set()
                await asyncio.wait_for(asyncio.shield(old_task), .5)
                self.assertEqual(order, ["answer", (kind, human, DEGRADED)], "D: ordinary terminal chain or turn identity lost")
                with self.assertRaises(OrdinaryInputRejected, msg="I4: old delivery released current generation"):
                    await runtime.capture_submission_admission(binding.id)
                new = self.clients.calls[-1]
                new.init()
                new.result()
                self.assertTrue(await runtime.wait_idle(.5))
                return
            if kind == "no_work_reader_end":
                c.init()
                c.stream.put_nowait(asyncio.CancelledError())
                self.assertTrue(await runtime.wait_idle(.5))
                self.assertFalse(notices, "I1: clean reader ending degraded")
                self.assertEqual([(e.turn_id, str(e.error)) for e in events], [(human, UNKNOWN)], "I1: ordinary unknown lost")
                return
            if kind == "mismatched_stop":
                for frame in BUFFERED[:5]:
                    emit(c, frame)
                c.stream.put_nowait(AssistantMessage([ToolUseBlock("stop", "TaskStop", {"task_id": "task-a"})],
                                                     "model", session_id=c.session))
                c.stream.put_nowait(UserMessage([ToolResultBlock("stop", "", False)],
                    tool_use_result={"task_id": "other", "task_type": "local_bash"}))
                c.result()
                await self.until(lambda: notices or events, "I1: no TaskStop disposition")
                self.assertEqual(notices, [(kind, DEGRADED)], "I1: TaskStop accepted another task")
                self.assertFalse(events)
                return
            if kind == "killed_then_stopped":
                for frame in BUFFERED[:5] + ("stop_a", "first", "idle"):
                    emit(c, frame)
                await c.consumed()
                preview = await runtime.preview_stop(binding)
                await runtime.stop_exact(binding.id, expected_activity_revision=preview.activity_revision, expected_turn_id=human)
                c.system("task_updated", task_id="task-a", run_id="run-a", patch={"status": "killed"})
                c.system("task_notification", task_id="task-a", run_id="run-a", tool_use_id="tool-a", status="stopped")
                for frame in ("empty", "idle"):
                    emit(c, frame)
                self.assertTrue(await runtime.wait_idle(.5))
                self.assertEqual(notices, [], "I1: stopped contradicted killed")
                self.assertEqual([e.turn_id for e in events], [human])
                return
            if kind in ("mcp_background", "sdk_cancel", "denials_shape", "api_status_shape"):
                c.init()
                if kind == "mcp_background":
                    for i, value in enumerate(("native-looking", True)):
                        tool = f"mcp-{i}"
                        c.stream.put_nowait(AssistantMessage([ToolUseBlock(tool, "mcp__fixture__work", {"run_in_background": value})],
                                                             "model", session_id=c.session))
                        c.stream.put_nowait(UserMessage([ToolResultBlock(tool, "", False)],
                            tool_use_result={"backgroundTaskId": {"opaque": True}, "task_id": 42}))
                    c.result()
                    self.assertTrue(await runtime.wait_idle(.5))
                    self.assertEqual([e.turn_id for e in events], [human], "I1: opaque MCP tool lost body")
                    self.assertEqual(notices, [])
                elif kind == "sdk_cancel":
                    c.start_background()
                    c.result()
                    await self.until(lambda: events, "I1: first body missing")
                    c.stream.put_nowait(asyncio.CancelledError())
                    self.assertTrue(await runtime.wait_idle(.5))
                    self.assertEqual(notices, [(kind, DEGRADED)], "D: SDK pull cancellation lost terminal")
                else:
                    if kind == "denials_shape":
                        c.result(permission_denials=17)
                    else:
                        c.result(reason="api_error", is_error=True, api_error_status={"status": 500})
                    self.assertTrue(await runtime.wait_idle(.5))
                    self.assertEqual(notices, [(kind, DEGRADED)], "D: invalid Result passed to Channel")
                    self.assertEqual(events, [])
                return
            if kind == "reordered_inventory":
                for frame in TWO_TASKS[:8]:
                    emit(c, frame)
                tasks = [dict(id=t, type="shell", status="running") for t in ("task-a", "task-b")]
                for inventory in (tasks, list(reversed(tasks))):
                    c.callback(dict(session_id=c.session, prompt_id=uid(104), session_crons=[], background_tasks=inventory), uid(105))
                for frame in ("first", "idle") + BATCH_WAKE:
                    emit(c, frame)
                self.assertTrue(await runtime.wait_idle(.5))
                self.assertEqual([e.turn_id for e in events], [human, uid(25), uid(24)], "I1: reordered inventory suppressed bodies")
                self.assertEqual(notices, [])
                return
            if kind == "reader_behind_stop":
                for frame in BUFFERED[:5] + ("stop_a", "first", "idle"):
                    emit(c, frame)
                await c.consumed()
                await runtime.preview_stop(binding)
                c.stop_snapshot(tasks=["task-a"])
                try:
                    preview = await runtime.preview_stop(binding)
                    await runtime.stop_exact(binding.id, expected_activity_revision=preview.activity_revision, expected_turn_id=preview.turn_id)
                except (OrdinaryInputRejected, ThreadActivityChanged):
                    pass
                self.assertEqual(c.stops, [], "I3: task stop dispatched while reader lags")
                return
            if kind == "reader_exception":
                c.close_gate.clear()
                with patch.object(ClaudeBackground, "observe", side_effect=TypeError("bad shape")):
                    emit(c, "human")
                    await self.until(c.closing.is_set, "D: reader exception escaped")
                await self.assert_degraded(c, runtime, binding, events, notices, human, kind)
                return
            if kind == "degraded_late_close":
                with patch("netizen_cli.runtime.claude_ordinary._CLOSE_SECONDS", .02):
                    c.close_gate.clear()
                    evidence_frame(c, "state_id")
                    evidence_frame(c, "changed_state")
                    await self.until(lambda: runtime.local_snapshot(binding.id).unavailable, "D: missing close quarantine")
                    await self.assert_degraded(c, runtime, binding, events, notices, human, kind)
                return
            if kind == "degraded_close_failure":
                c.failure = "close"
                evidence_frame(c, "state_id")
                evidence_frame(c, "changed_state")
                await self.until(lambda: runtime.local_snapshot(binding.id).turn is None, "D: failed close hung")
                with self.assertRaisesRegex(OrdinaryInputRejected, "无法确认 Claude 客户端已关闭", msg="D: failed close readmitted"):
                    await runtime.capture_submission_admission(binding.id)
                self.assertEqual(c.closes, 1)
                self.assertEqual(notices, [(kind, DEGRADED)])
                return
            if kind == "stop_receipt_order":
                emit(c, "running")
                emit(c, "human")
                await c.consumed()
                preview = await runtime.preview_stop(binding)
                started, release = asyncio.Event(), asyncio.Event()
                async def acknowledge():
                    started.set()
                    await release.wait()
                stop = asyncio.create_task(runtime.stop_exact(binding.id, acknowledge=acknowledge,
                    expected_activity_revision=preview.activity_revision, expected_turn_id=preview.turn_id))
                try:
                    await self.until(started.is_set, "I3: stop receipt missing")
                    emit(c, "aborted")
                    emit(c, "idle")
                    await self.until(c.closing.is_set, "I2: interrupted turn held")
                    self.assertEqual(events, [], "I4: completion preceded stop receipt")
                finally:
                    release.set()
                    await stop
                self.assertTrue(await runtime.wait_idle(.5))
                self.assertEqual([(e.turn_id, e.status) for e in events], [(human, "interrupted")])
                return
            if kind == "foreign_session":
                for frame in BUFFERED[:5] + ("registered", "first", "idle", "end_a", "empty", "wake_a", "stop_empty_a"):
                    evidence_frame(c, frame)
                await self.until(lambda: events, "I1: first body missing")
                c.close_gate.clear()
                c.wake_result("run-a", uuid=uid(95), result="foreign body", session_id=uid(96), snapshot=False)
                await self.assert_degraded(c, runtime, binding, events, notices, human, kind, before=[human])
                return
            if kind in ("quarantine_exit", "quarantine_eof"):
                c.close_gate.clear()
                emit(c, "human")
                for frame in ("state_id", "changed_state"):
                    evidence_frame(c, frame)
                c.system("init", uuid=uid(92), cwd=str(cwd / "other"), permissionMode="auto", user_message_uuids=[human])
                await self.assert_degraded(c, runtime, binding, events, notices, human, kind,
                                           ending="exit" if kind == "quarantine_exit" else "eof")
                return
            if kind == "stop_continuation":
                for frame in BUFFERED[:5]:
                    emit(c, frame)
                await c.consumed()
                first = dict(session_id=c.session, prompt_id=uid(80), stop_hook_active=False, session_crons=[],
                             background_tasks=[dict(id="task-a", type="shell", status="running")])
                c.callback(first, uid(81))
                for frame in ("end_a", "empty"):
                    emit(c, frame)
                await c.consumed()
                continued = first | dict(stop_hook_active=True, background_tasks=[])
                c.callback(continued, uid(82))
                c.callback(continued, uid(82))
                for frame in ("first", "idle", "wake_a", "stop_empty_a", "answer_a", "idle"):
                    emit(c, frame)
                self.assertTrue(await runtime.wait_idle(.5), "I2: native Stop continuation quarantined")
                self.assertEqual([(e.turn_id, e.origin) for e in events], [(human, kind), (uid(24), kind)], "I1: native Stop continuation lost a body")
                return
            if kind in ("close_error", "permission_notice"):
                emit(c, "running")
                emit(c, "human")
                emit(c, "stop_empty_h")
                if kind == "close_error":
                    c.failure = "close"
                c.result(snapshot=False, permission_denials=[{"tool_name": "Bash"}])
                await self.until(lambda: runtime.local_snapshot(binding.id).turn is None, "I2: cleanup incomplete")
                if kind == "close_error":
                    self.assertTrue(runtime.local_snapshot(binding.id).unavailable, "I2: failed disconnect readmitted")
                    with self.assertRaises(OrdinaryInputRejected):
                        await runtime.capture_submission_admission(binding.id)
                else:
                    self.assertTrue(any("Bash" in text for _, text in notices), "I1: permission denial lost")
                return
            if kind == "cron_count":
                c.stop_snapshot(crons=["one", "two"])
                for frame in ("running", "human", "first", "idle"):
                    emit(c, frame)
                await self.until(lambda: notices, "I2: missing cron notice")
                with self.assertRaises(OrdinaryInputRejected, msg="I3: stale abandon generation accepted"):
                    await runtime.abandon_background(binding.id, uid(999), "owner")
                try:
                    summary = await runtime.abandon_background(None, human, "owner")
                except OrdinaryInputRejected:
                    self.fail("I3: exact generation unreachable")
                self.assertIn("定时唤醒：2", summary, "I2: distinct cron count lost")
                return
            if kind == "failed_task":
                for frame in BUFFERED[:5] + ("stop_a", "first", "idle"):
                    emit(c, frame)
                c.system("task_updated", task_id="task-a", run_id="run-a", patch={"status": "failed"})
                for frame in ("empty", "wake_a", "stop_empty_a", "answer_a", "idle"):
                    emit(c, frame)
                self.assertTrue(await runtime.wait_idle(.5), "I2: failed-task wake remained busy")
                self.assertEqual([e.turn_id for e in events], [human, uid(24)], "I1: failed task lost wake")
                return
            if kind in ("before_disconnect", "after_disconnect"):
                # Schedule a native callback on the next loop iteration at the idle
                # observation or at the final decision. Record which side of the
                # public disconnect call its entry actually reaches.
                original = ClaudeBackground.closable.fget
                probes, arrivals = [], []
                def callback():
                    arrivals.append(c.closing.is_set())
                    c.stop_snapshot(tasks=["restored-task"], crons=["restored-cron"])
                def choose(bg):
                    valid = original(bg)
                    if valid:
                        probes.append(True)
                        if len(probes) == (1 if kind == "before_disconnect" else 2):
                            asyncio.get_running_loop().call_soon(callback)
                    return valid
                with patch.object(ClaudeBackground, "closable", property(choose)):
                    for frame in ("running", "human", "failed", "idle"):
                        emit(c, frame)
                    await self.until(lambda: arrivals, "I2: no disconnect boundary observed")
                    if kind == "after_disconnect":
                        self.assertEqual(arrivals, [True], "I2: yielded after final close recheck")
                        self.assertTrue(await runtime.wait_idle(.5))
                    else:
                        await self.until(lambda: notices or c.closes, "I2: callback lost before disconnect")
                        self.assertEqual((arrivals, c.closes, c.consumers), ([False], 0, 1), "I2: premature disconnect")
                        self.assertIn("定时唤醒", await runtime.abandon_background(binding.id, human, "owner"))
                return
            if kind == "foreign_task_stop":
                for frame in BUFFERED[:5]:
                    emit(c, frame)
                c.stream.put_nowait(AssistantMessage([ToolUseBlock("stop-other", "TaskStop", {"task_id": "task-a"})],
                    "fixture", session_id=c.session, user_message_uuid="other"))
                c.stream.put_nowait(UserMessage([ToolResultBlock("stop-other", "", False)],
                    tool_use_result={"task_id": "task-a", "task_type": "local_bash"}))
                for frame in ("stop_a", "first", "idle"):
                    emit(c, frame)
                c.system("task_updated", task_id="task-a", run_id="run-a", patch={"status": "killed"})
                emit(c, "empty")
                emit(c, "idle")
                self.assertTrue(await runtime.wait_idle(.5))
                self.assertEqual(notices, [(kind, DEGRADED)])
                self.assertEqual(events, [])
                return
            if kind == "contrary_result_run":
                for frame in BUFFERED[:5] + ("stop_a", "first", "idle", "end_a", "empty", "wake_a", "stop_empty_a", "answer_a"):
                    emit(c, frame)
                await self.until(lambda: len(events) == 2, "I1: wake delivery missing")
                c.close_gate.clear()
                c.wake_result("other-run", uuid=uid(24), result="contrary", snapshot=False, idle=False)
                await self.assert_degraded(c, runtime, binding, events, notices, human, kind, before=[human, uid(24)])
                return
            if kind in ("sticky", "conflicted_init_tool", "early_callback", "tool_eof", "tool_exit", "list_eof", "list_exit"):
                c.close_gate.clear()
                frames = {"sticky": ("running", "human", "changed_human", "wake_b"),
                          "conflicted_init_tool": ("human", "changed_human", "bare_tool", "members_a", "start_a"),
                          "early_callback": ("human", "stop_empty_h", "stop_a")}
                if kind.startswith("tool_"):
                    for frame in ("running", "human", "tool_a", "receipt_tool", "changed_tool_receipt"):
                        evidence_frame(c, frame)
                else:
                    for frame in frames.get(kind, ("running", "human", "empty_id", "changed_list", "empty")):
                        evidence_frame(c, frame)
                await self.assert_degraded(c, runtime, binding, events, notices, human, kind,
                                           ending="eof" if kind.endswith("eof") else "exit")
                return
            if kind in ("queued_conflict", "wake_usage", "bodyless"):
                for frame in BUFFERED[:5]:
                    emit(c, frame)
                for frame in ("stop_a", "first", "idle", "end_a", "empty", "wake_a"):
                    emit(c, frame)
                await c.consumed()
                if kind == "bodyless":
                    evidence_frame(c, "receipt_a")
                else:
                    emit(c, "stop_empty_a")
                    c.wake_result("run-a", result="wake body", usage={"input_tokens": 27}, uuid=uid(24), snapshot=False, idle=False)
                if kind == "queued_conflict":
                    c.wake_result("run-a", subtype="success", reason="api_error", is_error=True,
                                  uuid=uid(91), snapshot=False, idle=False)
                emit(c, "idle")
                if kind == "queued_conflict":
                    await self.until(c.closing.is_set, "D: queued outcome did not degrade")
                    receipt.release_receipt_attempt()
                self.assertTrue(await runtime.wait_idle(.5), "I2: disposition remained busy")
                if kind == "queued_conflict":
                    self.assertEqual(notices, [(kind, DEGRADED)])
                    self.assertEqual(events, [], "D: queued conclusion survived generation conflict")
                elif kind == "bodyless":
                    self.assertEqual([(type(e), e.turn_id) for e in events], [(OrdinaryConclusion, human)],
                                     "I1: bodyless receipt produced an event")
                else:
                    usage = runtime.capture_status(binding.id).observations.get("input_tokens")
                    self.assertEqual((usage.execution_id, usage.value), (uid(12), 27), "I6: wrong wake usage")
                return
            if kind in ("receipt_order", "subagent"):
                if kind == "subagent":
                    await c.background.stop_hook(dict(hook_event_name="Stop", agent_id="child", session_id=c.session,
                        prompt_id=uid(94), background_tasks=[], session_crons=[dict(id="child-cron", recurring=True)]), None, {})
                for frame in ("running", "human", "stop_empty_h", "first", "idle"):
                    emit(c, frame)
                if kind == "receipt_order":
                    await self.until(c.closing.is_set, "I2: plain execution not closing")
                    self.assertEqual(events, [], "I4: delivery preceded receipt attempt")
                    receipt.release_receipt_attempt()
                self.assertTrue(await runtime.wait_idle(.5), "I2: subagent callback held main session")
                self.assertEqual([(e.turn_id, e.origin) for e in events], [(human, kind)], "I1: ordinary receipt lost a body")
                return
            if kind == "cron_touch":
                for frame in ("running", "human", "cron", "stop_empty_h", "first", "idle"):
                    evidence_frame(c, frame)
                await self.until(lambda: bool(notices) or c.closing.is_set(), "I2: no cron disposition")
                self.assertFalse(c.closing.is_set(), "I2: Cron tool ignored")
                return
            for frame in BUFFERED[:5]:
                evidence_frame(c, frame)
            if kind == "stop_inventory":
                c.callback(dict(session_id=c.session, prompt_id=uid(60), session_crons=[],
                    background_tasks=[dict(id=t, type="shell", status="running") for t in ("task-a", "unseen")]))
            else:
                emit(c, "stop_a")
            emit(c, "first")
            emit(c, "idle")
            await c.consumed()
            if kind in ("callback_only", "entry"):
                data = dict(hook_event_name="Stop", session_id=c.session, prompt_id=uid(ord("h")),
                            background_tasks=[], session_crons=[dict(id="cron", recurring=True)])
                hook = asyncio.create_task(c.background.stop_hook(data, uid(300 + ord("h")), {}))
                if kind == "entry":
                    # A callback cancelled at its first suspension must already have recorded its inventory.
                    asyncio.get_running_loop().call_soon(hook.cancel)
                await asyncio.gather(hook, return_exceptions=True)
                await self.until(lambda: bool(notices), "I7: callback failed to wake reader")
                self.assertTrue(await runtime.wait_idle(.5))
                self.assertEqual(notices, [(kind, DEGRADED)])
                return
            for frame in ("end_a", "empty", "wake_a"):
                emit(c, frame)
            await c.consumed()
            if kind == "foreign":
                emit(c, "stop_empty_a")
                evidence_frame(c, "foreign_producer")
                emit(c, "idle")
                await self.until(lambda: bool(notices) or c.closing.is_set(), "I2: missing foreign disposition")
                self.assertEqual([e.turn_id for e in events], [human], "I1: routed foreign producer")
                return
            if kind == "stop_inventory":
                for frame in ("stop_empty_a", "answer_a", "idle"):
                    emit(c, frame)
                await self.until(lambda: len(events) == 2, "I1: attributable disposition lost")
                c.stream.put_nowait(None)
                await self.until(lambda: notices or c.closing.is_set(), "I2: unknown task lost")
                self.assertTrue(await runtime.wait_idle(.5))
                self.assertIn((kind, DEGRADED), notices, "I2: Stop proved disposition")
                return
            # The out-of-contract version is covered separately in
            # docs/design.md, Claude Code 后端（试点）, F2 residual.
            self.assertIn(kind, ("ahead", "f2_residual"))
            evidence_frame(c, "receipt_a" if kind == "ahead" else "answer_a")
            emit(c, "idle")
            for frame in ("running", "wake_b", "cron", "answer_b", "idle"):
                emit(c, frame)
            emit(c, "stop_empty_b")
            if kind == "f2_residual":
                self.assertTrue(await runtime.wait_idle(.5))
                self.assertEqual(c.stream.qsize(), 5, "F2 residual: buffered next turn masked")
            else:
                await self.until(lambda: notices, "I2: credited bodyless receipt or ignored future Stop")
                self.assertIn((kind, DEGRADED), notices, "I2: credited bodyless receipt or ignored future Stop")
                self.assertEqual([e.turn_id for e in events], [human])

    async def assert_degraded(self, c, runtime, binding, events, notices, human, anchor, *, ending="exit", before=()):
        await self.until(c.closing.is_set, "D: no disconnect")
        await self.until(lambda: any("后续回答" in text for _, text in notices), "D: missing anchored notice")
        with self.assertRaises(OrdinaryInputRejected, msg="D: readmission before disconnect"):
            await runtime.capture_submission_admission(binding.id)
        with self.assertRaises(OrdinaryInputRejected):
            await runtime.abandon_background(binding.id, human, "owner")
        emit(c, "first")
        emit(c, "idle")
        try:
            await c.consumed()
        except TimeoutError:
            self.fail("D: reader stopped before disconnect")
        c.stream.put_nowait(ProcessError("fixture", exit_code=-9) if ending == "exit" else None)
        await self.until(c.stream.empty, "D: stream ending was not consumed")
        c.close_gate.set()
        self.assertTrue(await runtime.wait_idle(.5), "D: disconnect hung")
        self.assertEqual([e.turn_id for e in events if e.origin == anchor], list(before), "D: delivery after latch")
        self.assertEqual([(origin, text) for origin, text in notices if "后续回答" in text],
                         [(anchor, DEGRADED)], "D: repeated or misrouted notice")
        self.assertEqual((c.closes, c.consumers, len(c.inputs)), (1, 1, 1), "D: leaked client")
        binding = self.store.get(binding.id)
        receipt = await runtime.submit(binding=binding, cwd=c.cwd, input=PreparedPrompt("next"), owner_id="owner",
            origin="next", admission=await runtime.capture_submission_admission(binding.id), context_commit=None, skill_names=())
        receipt.release_receipt_attempt()
        successor = self.clients.calls[-1]
        self.assertTrue(successor is not c and successor.resume and successor.session == c.session)
        successor.init()
        successor.result()
        self.assertTrue(await runtime.wait_idle(.5), "D: successor did not finish")
        self.assertEqual([(e.turn_id, e.final_response) for e in events if e.origin == "next"],
                         [(receipt.turn_id, "answer")], "D: state leaked into next generation")

    async def shape_trace(self, store, cwd, kind, path, index, cut, ending):
        label = (kind, path, index, cut, ending)
        async with self.fixture(store, cwd, label, resumed=True) as (c, runtime, binding, events, notices, receipt):
            human = receipt.turn_id
            emit(c, "running")
            if cut or kind == "user":
                emit(c, "human")
            if kind == "user":
                c.stream.put_nowait(AssistantMessage([ToolUseBlock("probe-tool", "Bash", {})], "model", session_id=c.session))
            if cut >= 2:
                emit(c, "stop_empty_h")
                emit(c, "first")
                await self.until(lambda: events, "I1: first body missing")
            c.close_gate.clear()
            if cut == 3:
                for frame in ("state_id", "changed_state"):
                    evidence_frame(c, frame)
            callback = malformed(c, kind, path, index)
            if callback is not None:
                await callback
            await self.assert_degraded(c, runtime, binding, events, notices, human, label, ending=ending,
                                       before=[human] if cut >= 2 else [])

    async def test_shape_family(self):
        with tempfile.TemporaryDirectory() as directory, closing(BindingStore()) as store:
            store.bootstrap_project(alias="test", cwd=directory)
            for kind, path, index in shape_cases():
                for cut in range(4):
                    for ending in ("eof", "exit"):
                        with self.subTest(kind=kind, path=path, value=index, cut=cut, ending=ending):
                            await self.shape_trace(store, Path(directory), kind, path, index, cut, ending)

    async def replay_trace(self, store, cwd, kind):
        clients = FakeClaudeClients()
        async with self.fixture(store, cwd, "old", clients=clients) as (old, runtime, binding, events, notices, _):
            old.init()
            old.result()
            self.assertTrue(await runtime.wait_idle(.5))
            binding = store.get(binding.id)
            receipt = await runtime.submit(binding=binding, cwd=cwd, input=PreparedPrompt("new"), owner_id="owner",
                origin=kind, admission=await runtime.capture_submission_admission(binding.id), context_commit=None, skill_names=())
            receipt.release_receipt_attempt()
            c = clients.calls[-1]
            human = receipt.turn_id
            c.close_gate.clear()
            old_row = dict(run_id="old", task_id="old-task", task_type="local_bash")
            new_row = dict(run_id="new", task_id="new-task", task_type="local_bash")
            c.system("background_tasks_changed", uuid=uid(98), tasks=[old_row])
            c.result(uuid=uid(99), user_message_uuid=old.inputs[0]["uuid"], result="prior", snapshot=False, idle=False)
            if kind != "unowned":
                c.system("task_updated", task_id="old-task", run_id="old", patch={"status": "completed"})
            c.init(user_message_uuids=[])
            if kind == "unowned":
                c.system("init", uuid=uid(100), cwd=str(cwd), permissionMode="auto", user_message_uuids=[human])
                emit(c, "failed")
                emit(c, "idle")
                barrier = asyncio.Event()
                c.stream.put_nowait(barrier)
                await self.until(lambda: barrier.is_set() or c.closes, "I7: reader stalled")
                self.assertEqual(c.closes, 0, "I2: unowned unresolved work permitted close")
                self.assertEqual([e.turn_id for e in events if e.origin == kind], [human])
                c.stream.put_nowait(None)
                await self.until(c.closing.is_set, "D: unresolved work failed to disconnect")
                c.close_gate.set()
                self.assertTrue(await runtime.wait_idle(.5))
                self.assertEqual(notices, [(kind, DEGRADED)])
                return
            if kind == "result_conflict":
                c.result(uuid=uid(99), result="forged", idle=False)
            elif kind == "assistant":
                c.stream.put_nowait(AssistantMessage([], "model", session_id=c.session, user_message_uuid=human))
            elif kind == "stream":
                c.stream.put_nowait(StreamEvent(uid(101), c.session, {}, user_message_uuids=[human]))
            elif kind == "terminal":
                c.stop_snapshot(crons=["cron"])
                c.result()
                await self.until(lambda: any(e.origin == kind for e in events), "I1: attributed H2 missing")
            else:
                c.system("init", uuid=uid(100), cwd=str(cwd), permissionMode="auto", user_message_uuids=[human])
            if kind == "membership_conflict":
                c.system("background_tasks_changed", uuid=uid(98), tasks=[new_row])
            elif kind == "prior_terminal_conflict":
                c.system("task_updated", task_id="old-task", run_id="old", patch={"status": "failed"})
            elif kind != "result_conflict":
                c.system("background_tasks_changed", uuid=uid(102), tasks=[old_row, new_row])
                await c.consumed()
                self.assertEqual(c.background.members, {"new": "new-task"}, "I1: current input did not end cold replay")
                for frame in ("state_id", "changed_state"):
                    evidence_frame(c, frame)
            current_events = [e for e in events if e.origin == kind]
            events.clear()
            events.extend(current_events)
            await self.assert_degraded(c, runtime, binding, events, notices, human, kind,
                                       before=[human] if kind == "terminal" else [])

    async def test_replay_ownership_and_classification(self):
        with tempfile.TemporaryDirectory() as directory, closing(BindingStore()) as store:
            store.bootstrap_project(alias="test", cwd=directory)
            for kind in ("membership_conflict", "result_conflict", "prior_terminal_conflict", "terminal", "assistant", "stream", "mixed", "unowned"):
                with self.subTest(kind=kind):
                    await self.replay_trace(store, Path(directory), kind)

    async def cold_replay_conflict(self, store, cwd):
        clients = FakeClaudeClients()
        async with self.fixture(store, cwd, "old", clients=clients) as (old, runtime, binding, events, notices, _):
            for frame in BUFFERED[:5] + ("receipt_tool", "stop_a", "first", "idle", "end_a", "empty", "wake_a", "receipt_a", "idle"):
                evidence_frame(old, frame)
            self.assertTrue(await runtime.wait_idle(.5))
            self.assertEqual([e.origin for e in events], ["old"])
            binding = store.get(binding.id)
            receipt = await runtime.submit(binding=binding, cwd=cwd, input=PreparedPrompt("new"), owner_id="owner",
                origin="new", admission=await runtime.capture_submission_admission(binding.id), context_commit=None, skill_names=())
            receipt.release_receipt_attempt()
            c = clients.calls[1]
            self.assertTrue(c.resume and c.session == old.session)
            c.close_gate.clear()
            for frame in ("state_id", "changed_state", "receipt_a", "receipt_tool", "start_a", "end_a", "empty"):
                evidence_frame(c, frame)
            emit(c, "human")
            evidence_frame(c, "receipt_a")
            c.wake_result("run-a", uuid=uid(97), result="retired", snapshot=False, idle=False)
            await self.assert_degraded(c, runtime, binding, events, notices, receipt.turn_id, "new")

    async def lost_receipt_trace(self, store, cwd, kind):
        binding = store.create_channel_binding(scope=FeishuScope("app", str(uuid4()), ScopeKind.DIRECT),
                                              project_alias="test", creator_id="owner")
        clients, events, notices = FakeClaudeClients(), [], []
        clients.configure = lambda c: setattr(c, "failure", "query")
        async def deliver(event):
            events.append(event)
        async def notice(origin, body):
            notices.append(body)
        clock = [0.]
        runtime = ClaudeOrdinarySessions(bindings=store, clients=clients, owner_id="owner",
                                         on_completion=deliver, notice=notice, clock=lambda: clock[0])
        runtime.open_admission()
        clock[0] = 2.
        submit = asyncio.create_task(runtime.submit(binding=binding, cwd=cwd, input=PreparedPrompt("fixture"),
            owner_id="owner", origin="lost", admission=await runtime.capture_submission_admission(binding.id),
            context_commit=None, skill_names=()))
        try:
            await clients.created.wait()
            c = clients.calls[0]
            await c.writing.wait()
            if kind == "unattributed_init":
                c.init(user_message_uuids=[])
                await c.consumed()
                self.assertFalse(submit.done(), "I1: unrelated init confirmed write")
                c.result()
                (await asyncio.wait_for(submit, .5)).release_receipt_attempt()
                self.assertTrue(await runtime.wait_idle(.5))
                return
            for source in ("other", c.inputs[0]["uuid"]):
                c.result(user_message_uuid=source, uuid=uid(98), snapshot=False, idle=False)
            if kind != "quarantine_deadline":
                c.stream.put_nowait(None)
            with self.assertRaisesRegex(TerminalStateUnknown, DEGRADED):
                await asyncio.wait_for(asyncio.shield(submit), .8)
            self.assertTrue(await runtime.wait_idle(.5), "I4: receipt wait leaked delivery")
            self.assertEqual([(e.origin, e.turn_id, str(e.error)) for e in events], [("lost", c.inputs[0]["uuid"], DEGRADED)])
            self.assertEqual((notices, c.closes, c.consumers), ([], 1, 1))
        finally:
            await runtime.cancel_tasks()
            await asyncio.gather(submit, return_exceptions=True)

    async def test_restored_cron_first_seen_after_disconnect_call_is_outside_close_proof(self):
        with tempfile.TemporaryDirectory() as directory, closing(BindingStore()) as store:
            store.bootstrap_project(alias="test", cwd=directory)
            await self.proof_trace(store, Path(directory), "after_disconnect")

    async def test_generation_proofs(self):
        with tempfile.TemporaryDirectory() as directory, closing(BindingStore()) as store:
            store.bootstrap_project(alias="test", cwd=directory)
            for kind in ("list_eof", "list_exit", "sticky", "cron_touch", "callback_only", "entry", "foreign", "stop_inventory", "ahead", "receipt_order", "subagent", "tool_eof", "tool_exit", "early_callback", "before_disconnect",
                         "lost_receipt_conflict", "queued_conflict", "wake_usage", "bodyless",
                         "close_error", "permission_notice", "failed_task", "cron_count", "conflicted_init_tool", "foreign_task_stop", "contrary_result_run", "stop_continuation", "quarantine_exit", "quarantine_eof", "cold_replay_conflict",
                         "foreign_session", "stop_receipt_order", "quarantine_deadline", "reordered_inventory",
                         "terminal_receipt", "task_launcher", "handoff_order", "no_work_reader_end", "mismatched_stop", "killed_then_stopped", "unattributed_init",
                         "mcp_background", "sdk_cancel", "denials_shape", "api_status_shape", "reader_behind_stop", "reader_exception", "degraded_close_failure", "degraded_late_close"):
                with self.subTest(kind=kind):
                    await self.proof_trace(store, Path(directory), kind)

    async def test_f2_violation_can_mask_a_buffered_turn_at_idle(self):
        with tempfile.TemporaryDirectory() as directory, closing(BindingStore()) as store:
            store.bootstrap_project(alias="test", cwd=directory)
            await self.proof_trace(store, Path(directory), "f2_residual")

    def until(self, condition, invariant):
        async def wait():
            try:
                async with asyncio.timeout(.5):
                    while not condition():
                        await asyncio.sleep(0)
            except TimeoutError:
                self.fail(invariant)
        return wait()

    async def test_every_mutant_breaks_its_explorer_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            for mutant in MUTANTS:
                with self.subTest(mutant=mutant.name):
                    store = BindingStore()
                    try:
                        store.bootstrap_project(alias="test", cwd=directory)
                        await self.explore(store, cwd, mutant.family, mutant.cut, mutant.action)
                        store.close()
                        store = BindingStore()
                        store.bootstrap_project(alias="test", cwd=directory)
                        with mutant.applied(SequenceTest), self.assertRaisesRegex(AssertionError, mutant.invariant):
                            await self.explore(store, cwd, mutant.family, mutant.cut, mutant.action)
                    finally:
                        store.close()
