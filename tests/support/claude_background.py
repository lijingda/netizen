"""Pin contract for observable Claude background Bash handoffs, using the loopback provider."""
import asyncio
from importlib.metadata import version
import json
import os
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from claude_agent_sdk import AssistantMessage, HookMatcher, ResultMessage, SystemMessage, ToolUseBlock

from netizen_cli.claude_installation import CLI_VERSION
from netizen_cli.ordinary_sessions import PreparedPrompt
from netizen_cli.runtime.claude_client import BundledClaudeClients, single_message, user_message


async def background_cases(cwd, provider, evidence):
    # Requalify the untyped run_id / origin.runId and membership shapes together on upgrades.
    evidence.check("background metadata qualified pin", (version("claude-agent-sdk"), CLI_VERSION) == ("0.2.186", "2.1.329"))
    with (patch("netizen_cli.runtime.claude_client.verified_claude_cli", return_value=Path("/unused")),
          patch("netizen_cli.claude_installation.CLI_VERSION", "next")):
        try:
            BundledClaudeClients()
        except RuntimeError as exc:
            evidence.check("changed pin refuses background decoder", "重新验证" in str(exc))
        else:
            raise AssertionError("unqualified pin enabled the decoder")
    for case in ("single", "reverse", "early", "merge", "stop", "omitted", "interrupt", "cron", "batch", "stale"):
        async with asyncio.timeout(30):
            await _scenario(case, cwd, provider)
        evidence.check("background " + case, True)


async def _scenario(case, cwd, provider):
    session, human, extra = (str(uuid4()) for _ in range(3))
    commands = {}
    for slot in ("a", "b", "wake"):
        name = f"native-{case}-{slot}"
        script = cwd / name
        script.write_text(f'#!/bin/sh\ntouch started-{name}\ni=0\nwhile [ ! -f release-{name} ] && [ "$i" -lt 400 ]; do sleep .1; i=$((i+1)); done\n')
        script.chmod(0o700)
        commands[slot] = ("Bash", {"command": "./" + name, "run_in_background": slot != "wake", "description": "Fixture gate"})
    settings = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "settings.json"
    settings.write_text(json.dumps({"model": "claude-sonnet-4-6", "permissions": {
        "allow": ["Bash(" + args["command"] + ")" for _, args in commands.values()] + ["CronCreate"]}}))
    count = 0
    multi = case in ("reverse", "batch", "stale")
    def script():
        nonlocal count
        count += 1
        if count == 1 and case == "cron":
            return ("CronCreate", {"cron": "0 0 1 1 *", "prompt": "fixture", "recurring": True}), "text", case
        if count == 1 or (count == 2 and multi):
            return commands["a" if count == 1 else "b"], "text", case
        if count == 3 and case == "merge":
            return commands["wake"], "text", case
        if (case, count) in (("early", 2), ("interrupt", 3), ("stale", 5)):
            return None, "hold-stream", "native-stream-" + case
        return None, "text", case

    hooks = []
    async def stop_hook(data, tool, ctx):
        hooks.append(([item["id"] for item in data["background_tasks"]], [c["id"] for c in data["session_crons"]]))
        return {}
    client = BundledClaudeClients().create(session, cwd, resume=False)
    client.options.env = {"CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS": "1"}
    client.options.hooks = {"Stop": [HookMatcher(hooks=[stop_hook])]}
    results, starts, updates, notes, states, membership, uses = [], [], [], [], [], [], []
    async def read():
        async for msg in client.receive_messages():
            if isinstance(msg, ResultMessage):
                assert msg.session_id == session and msg.uuid
                results.append(msg)
            elif isinstance(msg, AssistantMessage):
                uses.extend(b.id for b in msg.content if isinstance(b, ToolUseBlock))
            elif isinstance(msg, SystemMessage):
                assert msg.data.get("session_id") == session
                if msg.subtype == "task_started":
                    assert msg.tool_use_id in uses
                    starts.append((msg.task_id, msg.data["run_id"], msg.tool_use_id))
                elif msg.subtype == "task_updated" and msg.status in ("killed", "completed"):
                    updates.append((msg.task_id, msg.data["run_id"]))
                elif msg.subtype == "task_notification" and case != "omitted":
                    notes.append((msg.task_id, msg.data["run_id"], msg.tool_use_id))
                elif msg.subtype == "session_state_changed":
                    states.append(msg.data["state"])
                elif msg.subtype == "background_tasks_changed":
                    membership.append([(item["task_id"], item["run_id"]) for item in msg.data["tasks"]])

    async def until(predicate):
        while not predicate():
            await asyncio.sleep(.02)
    def release(slot):
        provider.release(f"native-{case}-{slot}")
    async def query(uid):
        await client.query(single_message(user_message(PreparedPrompt("[[S:text/native]]"), uid)))

    provider.background_script = script
    try:
        await client.connect()
        reader = asyncio.create_task(read())
        try:
            await query(human)
            if case == "early":
                await until(lambda: (cwd / "streaming-native-stream-early").exists())
                release("a")
                await until(lambda: updates)
                assert not results
                provider.release("native-stream-early")
            await until(lambda: results)
            assert results[0].user_message_uuid == human and results[0].terminal_reason == "completed"
            if case == "cron":
                assert hooks[0][0] == [] and hooks[0][1]
                return
            assert starts and (starts[0][:2] in membership[0])
            if case in ("stop", "omitted"):
                await client.stop_task(starts[0][0])
                await until(lambda: updates and membership[-1] == [] and states[-1] == "idle")
                assert updates == [starts[0][:2]] and (not notes if case == "omitted" else notes == starts)
                return
            if multi:
                assert len(starts) == 2
                release("b")
                if case != "batch":
                    await until(lambda: len(results) == 2)
            release("a")
            if case == "merge":
                await until(lambda: (cwd / "started-native-merge-wake").exists())
                await query(extra)
                release("wake")
            if case in ("interrupt", "stale"):
                await until(lambda: (cwd / ("streaming-native-stream-" + case)).exists())
                await client.interrupt()
                provider.release("native-stream-" + case)
            await until(lambda: len(results) == len(starts) + 1 and states[-1] == "idle")
            expected = [s[1] for s in reversed(starts)]
            observed = [r.origin["runId"] for r in results[1:]]
            assert sorted(observed) == sorted(expected) if case == "batch" else observed == expected
            assert set(notes) == set(starts) and set(updates) == {s[:2] for s in starts}
            for r in results[1:]:
                assert r.origin == {"kind": "task-notification", "producer": "session-task", "runId": r.origin["runId"]}
                assert r.user_message_uuid == (extra if case == "merge" else None)
            if case in ("interrupt", "stale"):
                assert (results[-1].terminal_reason, results[-1].is_error) == ("aborted_streaming", True)
            else:
                assert results[-1].terminal_reason == "completed" and results[-1].result
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            await client.disconnect()
    finally:
        provider.background_script = None
