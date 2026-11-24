"""Pin native CLI ordering and identity using the public client and a loopback model."""
import asyncio
from contextlib import asynccontextmanager
from importlib.metadata import version
import json
import os
from pathlib import Path
from uuid import uuid4

from claude_agent_sdk import AssistantMessage, HookMatcher, ResultMessage, SystemMessage, ToolUseBlock, get_session_messages

from netizen_cli.claude_installation import CLI_VERSION
from netizen_cli.ordinary_sessions import PreparedPrompt
from netizen_cli.runtime.claude_client import BundledClaudeClients, last_assistant, single_message, user_message
from tests.support.claude_provider import blocks


async def check_idle_history(session, cwd, *, case, result_uuid, role="retained", baseline=None):
    """Only the retained client's ordered idle promises a quiet transcript."""
    idle = f"case={case} role={role} result_uuid={result_uuid}"
    print(f"Idle {idle} hold_seconds={0 if role == 'terminal' else 2.55}", flush=True)
    if role == "terminal":
        return None  # The foreign writer may already be processing its query.
    if baseline is None:
        baseline = await last_assistant(session, cwd)
    await asyncio.sleep(2.55)
    checked = await last_assistant(session, cwd)
    assert checked == baseline, ("false divergence", idle, baseline, checked)
    return baseline


class NativeSession:
    def __init__(self, cwd, provider, case):
        self.cwd, self.provider, self.case = cwd, provider, case
        self.session, self.h1, self.h2 = (str(uuid4()) for _ in range(3))
        self.count = 3 if case == "three" else 2
        self.frames = []
        self.stops = []
        self.requests = 0
        self.cron_fired = False
        self.client = None
        self.idle_history = None
        self.reader = None

    def reply(self, messages):
        texts = [p.get("text", "") for m in messages if m["role"] == "user" for p in blocks(m)]
        if any("child-signal" in t for t in texts) and not any("[[S:text/h1]]" in t for t in texts):
            return None, "hold-stream", self.case + "-child"
        self.requests += 1
        n, case = self.requests, self.case
        tool = None
        if n == 1 and case == "cron":
            tool = "CronCreate", {"cron": "* * * * *", "recurring": True, "durable": False, "prompt": "cron-signal"}
        elif n == 1 and case.startswith("agent"):
            tool = "Agent", {"description": "Fixture agent", "prompt": "child-signal", "subagent_type": "general-purpose",
                             "run_in_background": True}
        elif n == 1 or (n == 2 and case in {"reverse", "wake_interrupt", "stop_busy", "stop_idle", "empty-two"}):
            tool = "Bash", {"command": "./shell-" + case + str(n), "description": "Wait on fixture", "run_in_background": True}
        if case in {"empty-one", "empty-two"} and n > (2 if case == "empty-one" else 3):
            return None, "empty-answer", case
        self.cron_fired |= any("cron-signal" in t for t in texts)
        held = {"early": 2, "foreground_interrupt": 2, "overlap": 3, "wake": 3, "stop_busy": 3,
                "wake_interrupt": 4, "agent_esc": 2}
        return tool, ("tool-partial" if case == "reverse" and n == 1 else
                      "hold-stream" if held.get(case) == n else "text"), case + "-model"

    async def hook(self, data, callback_id, context):
        self.stops.append((data["background_tasks"], data["session_crons"]))
        return {}

    @asynccontextmanager
    async def connected(self, *, resume=False, role="retained"):
        self.frames.clear()
        self.client = BundledClaudeClients().create(self.session, self.cwd, resume=resume)
        self.client.options.hooks = {"Stop": [HookMatcher(hooks=[self.hook])]}
        self.client.options.env = {"CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS": "1"}
        if self.case in {"agent", "agent_esc"}:
            self.client.options.permission_mode = None  # Standalone native permission control.
        await self.client.connect()

        async def read():
            async for frame in self.client.receive_messages():
                if isinstance(frame, SystemMessage) and frame.subtype == "session_state_changed" and frame.data["state"] == "idle":
                    results = self.results
                    self.idle_history = await check_idle_history(self.session, self.cwd, case=self.case, role=role,
                                                                 result_uuid=results[-1].uuid if results else None)
                self.frames.append(frame)

        reader = self.reader = asyncio.create_task(read())
        try:
            yield
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            await self.client.disconnect()
            if not reader.cancelled():
                reader.result()

    async def query(self, human):
        uid = self.h1 if human == "h1" else self.h2
        await self.client.query(single_message(user_message(PreparedPrompt(f"[[S:text/{human}]]"), uid)))

    def system(self, subtype):
        return [m for m in self.frames if isinstance(m, SystemMessage) and m.subtype == subtype]

    @property
    def results(self):
        return [m for m in self.frames if isinstance(m, ResultMessage)]

    @property
    def state(self):
        states = self.system("session_state_changed")
        return states[-1].data["state"] if states else None

    async def until(self, test):
        while not test():
            if self.reader and self.reader.done():
                self.reader.result()
                raise AssertionError("native reader ended before fixture condition")
            await asyncio.sleep(.02)

    async def idle_after(self, number):
        await self.until(lambda: len(self.results) >= number and any(
            m.data["state"] == "idle" and self.frames.index(m) > self.frames.index(self.results[number - 1])
            for m in self.system("session_state_changed")))

    async def gate(self, label):
        await self.until(lambda: (self.cwd / ("streaming-" + self.case + "-" + label)).exists())

    def release(self, label):
        self.provider.release(self.case + "-" + label)

    def wake(self, result, task, reason="completed"):
        assert result.origin == {"kind": "task-notification", "producer": "session-task", "runId": task.data["run_id"]}
        assert result.session_id == self.session and result.uuid and result.user_message_uuid is None
        assert result.terminal_reason == reason and bool(result.result) == (reason == "completed" and not self.case.startswith("empty-"))

    async def external_turn(self):
        """Leave the task gated while a second bundled CLI resumes the same session."""
        async def history():
            return await asyncio.to_thread(get_session_messages, self.session, directory=str(self.cwd))

        assert len(self.system("background_tasks_changed")[-1].data["tasks"]) == 1
        original = await history()
        assert original
        baseline = self.idle_history
        assert baseline
        self.provider.live_session_reply = None
        terminal = NativeSession(self.cwd, self.provider, self.case)
        terminal.session = self.session
        async with terminal.connected(resume=True, role="terminal"):
            assert await terminal.client.get_server_info()  # CLI initialized without a human query.
            silent = await history()
            print("Pure headless resume changed visible history:", silent != original, flush=True)
            await terminal.query("external")
            await terminal.until(lambda: any(r.user_message_uuid == terminal.h2 and
                                             r.result == "fixture-reply-external" for r in terminal.results))
            await terminal.idle_after(len(terminal.results))
        changed = await history()
        assert changed != original
        checked = await last_assistant(self.session, self.cwd)
        assert checked and checked != baseline, "foreign turn passed send check"
        assert any(m.type == "assistant" and {"type": "text", "text": "fixture-reply-external"}
                   in m.message["content"] for m in changed)

    async def exercise(self):
        case = self.case
        await self.query("h1")
        if case == "cron":
            await self.idle_after(1)
            first = self.stops[0][1]
            assert len(first) == 1
            await self.query("h2")
            await self.until(lambda: any(r.user_message_uuid == self.h2 for r in self.results))
            await self.until(lambda: self.cron_fired and any(r.user_message_uuid is None for r in self.results))
            fired = next(r for r in self.results if r.user_message_uuid is None)
            assert fired.origin is None and fired.user_message_uuids is None and fired.uuid and fired.result
            assert self.stops[-1][1][0]["id"] == first[0]["id"]
            await self.idle_after(self.results.index(fired) + 1)
            return
        if case == "agent_denied":
            await self.idle_after(1)
            assert self.system("permission_denied")[0].data["decision_reason"] == "Classifier unavailable"
            assert not self.system("task_started")
            return
        multiple = case in {"reverse", "wake_interrupt", "stop_busy", "stop_idle", "empty-two"}
        await self.until(lambda: len(self.system("task_started")) == 1 + multiple)
        starts = self.system("task_started")
        uses = [(b.id, m.user_message_uuid) for m in self.frames if isinstance(m, AssistantMessage)
                for b in m.content if isinstance(b, ToolUseBlock)]
        assert uses == [(t.tool_use_id, self.h1 if i == 0 and case != "reverse" else None) for i, t in enumerate(starts)]
        assert self.system("init")[0].data["user_message_uuids"] == [self.h1]
        assert [(t["task_id"], t["run_id"]) for t in self.system("background_tasks_changed")[-1].data["tasks"]] == [
            (t.task_id, t.data["run_id"]) for t in starts]
        if case in {"foreground_interrupt", "early", "agent_esc", "stop_busy"}:
            await self.gate("model")
        elif case.startswith("agent"):
            await self.until(lambda: self.results)
        else:
            await self.idle_after(1)
        if case == "reverse":
            frames = [m for m in self.frames if isinstance(m, AssistantMessage) and m.parent_tool_use_id is None]
            partial = next(m for m in frames if any(getattr(b, "text", None) == "fixture-partial" for b in m.content))
            tool_frame = next(m for m in frames if any(isinstance(b, ToolUseBlock) for b in m.content))
            assert partial.uuid and partial.message_id == tool_frame.message_id
            assert partial.user_message_uuid == self.h1 and frames.index(partial) <= frames.index(tool_frame)
        if case == "cold":
            return
        if case == "handoff":
            await self.external_turn()
            return
        if case.startswith("agent"):
            await self.gate("child")
            assert starts[0].task_type == "local_agent" and self.state == "running"
            if case == "agent_esc":
                await self.client.interrupt()
                self.release("model")
            self.release("child")
        if case in {"foreground_interrupt", "stop_busy"}:
            await self.client.interrupt()
            self.release("model")
        if case.startswith("stop_"):
            for t in starts:
                await self.client.stop_task(t.task_id)
        if case in {"stop_busy", "stop_idle", "agent_esc"}:
            await self.until(lambda: self.system("background_tasks_changed")[-1].data["tasks"] == [] and self.state == "idle")
            assert {m.task_id for m in self.system("task_updated") if m.status == "killed"} == {t.task_id for t in starts}
            assert {m.task_id for m in self.system("task_notification") if m.status == "stopped"} == {t.task_id for t in starts}
            assert self.idle_history is not None
            await check_idle_history(self.session, self.cwd, case=case, result_uuid=self.results[-1].uuid, baseline=self.idle_history)
            return
        if case == "foreground_interrupt":
            await self.idle_after(1)
            assert self.results[0].terminal_reason == "aborted_streaming"
        if case == "overlap":
            await self.query("h2")
            await self.gate("model")
        for index in reversed(range(len(starts))):
            self.release(str(index + 1))
            task = starts[index]
            await self.until(lambda: any(t.task_id == task.task_id and t.status == "completed" for t in self.system("task_updated")))
            if case in {"early", "overlap"}:
                assert len(self.results) == (case == "overlap")
                self.release("model")
            if case == "wake":
                empty = self.system("background_tasks_changed")[-1]
                assert empty.data["tasks"] == []
                states = [m.data["state"] for m in self.frames[:self.frames.index(empty)]
                          if isinstance(m, SystemMessage) and m.subtype == "session_state_changed"]
                assert states[-1] == "idle"
            if case == "wake" or (case == "wake_interrupt" and index == 1):
                await self.gate("model")
                assert self.state == "running"
                if case == "wake_interrupt":
                    await self.client.interrupt()
                self.release("model")
            count = 1 + len(starts) - index + (case == "overlap")
            await self.idle_after(count)
            self.wake(self.results[count - 1], task, "aborted_streaming" if case == "wake_interrupt" and index == 1 else "completed")
        assert self.results[0].user_message_uuid == self.h1
        if case == "overlap":
            assert self.results[1].user_message_uuid == self.h2 and self.results[1].origin is None
        assert self.system("background_tasks_changed")[-1].data["tasks"] == []


class QuiescenceProbe(NativeSession):
    """Keep the client open beyond the first idle following natural removal."""

    def __init__(self, cwd, provider, case):
        super().__init__(cwd, provider, case)
        self.batches = []

    def reply(self, messages):
        # In a batched wake a single model request may consume multiple
        # notifications. Keep only their exact fixture task IDs and reader cut.
        parts = [p.get("text", "") for p in blocks(messages[-1]) if p.get("type") == "text"]
        self.requests += 1
        n = self.requests
        label = f"{self.case}-model{n}"
        tasks = {t.task_id for t in self.system("task_started")
                 if any("<task-notification>" in p and t.task_id in p for p in parts)}
        self.batches.append((len(self.frames), tasks, f"fixture-reply-{label}"))
        tool = ("Bash", {"command": f"./shell-{self.case}{n}", "description": "Wait on fixture",
                         "run_in_background": True}) if n <= self.count else None
        held = {"during-wake": (4,), "simultaneous": (), "during-h2": (4,), "three": (4, 5)}
        return tool, "hold-stream" if n in held[self.case] else "text", label

    async def removed(self, ordinal):
        task = self.system("task_started")[ordinal - 1].task_id
        await self.until(lambda: all(row["task_id"] != task for row in self.system("background_tasks_changed")[-1].data["tasks"]))

    async def exercise(self):
        await self.query("h1")
        await self.until(lambda: len(self.system("task_started")) == self.count)
        if self.case == "three":
            await self.gate("model4")  # A finishes during H1.
            self.release("1")
            await self.removed(1)
            self.release("model4")
            await self.gate("model5")  # B finishes while A's wake is streaming.
            self.release("2")
            await self.removed(2)
            self.release("model5")
            await self.until(lambda: self.state == "idle")
            self.release("3")
        else:
            await self.idle_after(1)
            if self.case == "simultaneous":
                self.release("both")  # Both waiting shells observe the same release.
            elif self.case == "during-h2":
                await self.query("h2")
                await self.gate("model4")
                self.release("1")
                await self.removed(1)
                self.release("2")
                await self.removed(2)
                self.release("model4")
            else:
                self.release("1")
                await self.gate("model4")
                self.release("2")
                await self.removed(2)
                self.release("model4")
        await self.until(lambda: self.system("background_tasks_changed")[-1].data["tasks"] == [])
        empty = self.frames.index(self.system("background_tasks_changed")[-1])
        def fresh_idle():
            return next((i for i, m in enumerate(self.frames) if i > empty and isinstance(m, SystemMessage)
                         and m.subtype == "session_state_changed" and m.data["state"] == "idle"), None)
        await self.until(lambda: fresh_idle() is not None)
        # Negative evidence requires leaving the actual CLI, reader and model
        # connected. A model barrier or a new query here could mask a late wake.
        await asyncio.sleep(3)
        boundary = fresh_idle()
        assert not any(isinstance(m, ResultMessage) or (isinstance(m, SystemMessage)
                       and (m.subtype == "init" or m.data.get("state") == "running")) for m in self.frames[boundary+1:])
        self.check_removals()

    def check_removals(self):
        latest = {}
        for i, msg in enumerate(self.frames):
            if not isinstance(msg, SystemMessage) or msg.subtype != "background_tasks_changed":
                continue
            tasks = {row["task_id"]: row["run_id"] for row in msg.data["tasks"]}
            for task in latest.keys() - tasks.keys():
                following = next(j for j, m in enumerate(self.frames) if j > i and isinstance(m, SystemMessage)
                                 and m.subtype == "session_state_changed" and m.data["state"] == "idle")
                receipted = any(isinstance(m, ResultMessage) and (m.origin or {}).get("runId") == latest[task]
                                for m in self.frames[i+1:following])
                batch_cuts = [(cut, answer) for cut, ids, answer in self.batches
                              if i < cut and task in ids]
                assert receipted or batch_cuts, "fresh idle preceded task processing"
                for cut, answer in batch_cuts:
                    assert any(isinstance(m, ResultMessage) and m.result == answer for m in self.frames[cut:following]), (
                        "fresh idle preceded the notification batch Result")
            latest = tasks


async def session_facts(cwd, provider, evidence):
    evidence.check("live session fact pins", (version("claude-agent-sdk"), CLI_VERSION) == ("0.2.186", "2.1.329"))
    for case in ("early", "reverse", "wake", "foreground_interrupt", "wake_interrupt", "overlap", "stop_idle",
                 "stop_busy", "empty-one", "empty-two", "agent_denied", "agent", "agent_esc", "cold", "handoff", "cron",
                 "during-wake", "simultaneous", "during-h2", "three"):
        quiescence = case in {"during-wake", "simultaneous", "during-h2", "three"}
        trace = QuiescenceProbe(cwd, provider, case) if quiescence else NativeSession(cwd, provider, case)
        commands = []
        for i in range(1, trace.count+1):
            command = cwd / ("shell-" + case + str(i))
            gate = "both" if case == "simultaneous" else str(i)
            interval = ".002" if quiescence else ".1"
            command.write_text(f"#!/bin/sh\nwhile [ ! -e release-{case}-{gate} ]; do sleep {interval}; done\n")
            command.chmod(0o700)
            commands.append("Bash(./" + command.name + ")")
        (Path(os.environ["CLAUDE_CONFIG_DIR"]) / "settings.json").write_text(json.dumps({
            "model": "claude-sonnet-4-6", "permissions": {"allow": commands + ["Agent(general-purpose)", "CronCreate"]}}))
        provider.live_session_reply = trace.reply
        try:
            async with asyncio.timeout(150 if case == "cron" else 30):
                async with trace.connected():
                    if case == "stop_busy":
                        # Keep a dormant native cron after both listed tasks stop.
                        def register(messages):
                            tool = None if any(p.get("type") == "tool_result" for p in blocks(messages[-1])) else (
                                "CronCreate", {"cron": "0 0 1 1 *", "recurring": True, "durable": False, "prompt": "hold"})
                            return tool, "text", "dormant-cron"
                        provider.live_session_reply = register
                        await trace.query("setup-cron")
                        await trace.idle_after(1)
                        assert trace.stops[-1][1]
                        trace.frames.clear()
                        provider.live_session_reply = trace.reply
                    await trace.exercise()
                if case in {"cold", "handoff", "cron"}:
                    trace.h2 = str(uuid4())
                    async with trace.connected(resume=True):
                        start = len(provider.rows)
                        await trace.query("h2")
                        await trace.until(lambda: any(r.user_message_uuid == trace.h2 and r.result for r in trace.results))
                        await trace.idle_after(len(trace.results))
                        if case == "cold":
                            current = next(m for m in trace.system("init") if m.data.get("user_message_uuids") == [trace.h2])
                            assert any(isinstance(m, ResultMessage) and not m.result and m.user_message_uuid is None
                                       for m in trace.frames[:trace.frames.index(current)])
                        elif case == "handoff":
                            # First model request after the holder's confirmed disconnect.
                            row = next(r for r in provider.rows[start:] if r["path"] == "/v1/messages" and r["label"] == "h2")
                            assert any(m["role"] == "user" and ("text", "external") in m["markers"]
                                       for m in row["history"])
                            assert any(m["role"] == "assistant" and "fixture-reply-external" in m["answers"]
                                       for m in row["history"])
                        else:
                            assert trace.stops[0][1][0]["id"] == trace.stops[-1][1][0]["id"]
                evidence.check("live session " + case, True)
        finally:
            provider.live_session_reply = None
            for label in ("1", "2", "3", "both", "model", "model4", "model5", "child"):
                trace.release(label)
