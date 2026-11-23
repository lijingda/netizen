"""Pin contract for observable Claude background Bash handoffs, using the loopback provider."""
import asyncio
from importlib.metadata import version
import json
import os
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import psutil
from claude_agent_sdk import AssistantMessage, HookMatcher, ProcessError, ResultMessage, SystemMessage, ToolUseBlock

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
    for case in ("single", "interrupt_stop", "continue_stop", "reverse", "early", "merge", "stop", "omitted", "interrupt", "cron", "batch", "stale"):
        async with asyncio.timeout(30):
            cron = await _scenario(case, cwd, provider)
            if cron:
                await _cold_cron(cwd, *cron)
                evidence.check("cold failure without Stop preserves session cron for next Stop", True)
        evidence.check("background " + case, True)
    for ending in ("reader_disconnect", "process_exit"):
        async with asyncio.timeout(30):
            await _scenario(ending, cwd, provider)
        evidence.check("background " + ending, True)


async def _scenario(case, cwd, provider):
    session, human, extra = (str(uuid4()) for _ in range(3))
    commands = {}
    for slot in ("a", "b", "wake"):
        name = f"native-{case}-{slot}"
        script = cwd / name
        script.write_text(f'#!/bin/sh\ntouch started-{name}\ni=0\nwhile [ ! -f release-{name} ] && [ "$i" -lt 400 ]; do sleep .1; i=$((i+1)); done\ntouch finished-{name}\n')
        script.chmod(0o700)
        commands[slot] = ("Bash", {"command": "./" + name, "run_in_background": slot != "wake", "description": "Fixture gate"})
    settings = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "settings.json"
    settings.write_text(json.dumps({"model": "claude-sonnet-4-6", "permissions": {
        "allow": ["Bash(" + args["command"] + ")" for _, args in commands.values()] + ["CronCreate"]}}))
    if case == "continue_stop":
        # A native Stop command asks for one more response in this same turn.
        command = cwd / "continue-once"
        command.write_text("#!/bin/sh\nif [ ! -e continued ]; then\n touch continued\n"
                           " echo '{\"decision\":\"block\",\"reason\":\"fixture continuation\"}'\nfi\n")
        command.chmod(0o700)
        config = json.loads(settings.read_text())
        config["hooks"] = {"Stop": [{"hooks": [{"type": "command", "command": "./continue-once"}]}]}
        settings.write_text(json.dumps(config))
    count = 0
    multi = case in ("reverse", "batch", "stale")
    def script():
        nonlocal count
        count += 1
        if case in ("interrupt_stop", "continue_stop"):
            return None, "text", case
        if count == 1 and case == "cron":
            return ("CronCreate", {"cron": "0 0 1 1 *", "prompt": "fixture", "recurring": True, "durable": False}), "text", case
        if count == 1 or (count == 2 and multi):
            return commands["a" if count == 1 else "b"], "text", case
        if count == 3 and case == "merge":
            return commands["wake"], "text", case
        if (case, count) in (("early", 2), ("batch", 3), ("interrupt", 3), ("stale", 5)):
            return None, "hold-stream", "native-stream-" + case
        if (case, count) == ("batch", 4):
            return None, "hold-stream", "batch-wake"
        return None, "text", f"{case}-{count}"

    hooks = []
    prompts = {}
    repeats = []
    continuation_entries = []
    entries_at_result = []
    entered, cancelled = asyncio.Event(), asyncio.Event()
    hook_release = asyncio.Event()
    async def stop_hook(data, tool, ctx):
        assert isinstance(data.get("prompt_id"), str) and data["prompt_id"]
        prompts[data["prompt_id"]] = data.get("last_assistant_message")
        if case == "continue_stop":
            continuation_entries.append((tool, data.get("stop_hook_active")))
        entered.set()
        hooks.append(([item["id"] for item in data["background_tasks"]], [c["id"] for c in data["session_crons"]]))
        if case in ("interrupt_stop", "reader_disconnect", "process_exit"):
            try:
                await hook_release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        return {}
    client = BundledClaudeClients().create(session, cwd, resume=False)
    client.options.env = {"CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS": "1"}
    client.options.hooks = {"Stop": [HookMatcher(hooks=[stop_hook])]}
    if case == "single":
        async def repeat(data, tool, ctx):
            repeats.append(data["prompt_id"])
            return {}
        client.options.hooks["Stop"].append(HookMatcher(hooks=[repeat]))
    results, starts, updates, notes, states, membership, uses = [], [], [], [], [], [], []
    async def read():
        async for msg in client.receive_messages():
            if isinstance(msg, ResultMessage):
                assert msg.session_id == session and msg.uuid
                entries_at_result.append(dict(prompts))
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
            if case in ("reader_disconnect", "process_exit"):
                await until(lambda: hooks and starts)
                assert hooks == [([starts[0][0]], [])] and not results
                info = await client.get_server_info()
                cli = psutil.Process(info["pid"])
                assert cli.parent() == psutil.Process() and Path(cli.exe()).samefile(client.options.cli_path)
                if case == "reader_disconnect":
                    # Hold only the verified fixture child so that reaping cannot win the EOF race.
                    cli.suspend()
                    closing = asyncio.create_task(client.disconnect())
                    try:
                        await reader
                        assert cli.is_running() and not closing.done() and not results
                    finally:
                        cli.resume()
                        await closing
                else:
                    cli.kill()
                    try:
                        await reader
                    except ProcessError as exc:
                        assert exc.exit_code is not None and not results
                    else:
                        raise AssertionError("abrupt CLI exit lost its public ProcessError")
                    await client.disconnect()
                    release("a")
                    await until(lambda: (cwd / "finished-native-process_exit-a").exists())
                return
            if case == "interrupt_stop":
                await entered.wait()
                assert not results
                await client.interrupt()
                await until(lambda: results and states[-1] == "idle")
                await cancelled.wait()
                assert results[0].user_message_uuid == human
                assert (results[0].subtype, results[0].terminal_reason, results[0].is_error) == ("success", "completed", False)
                assert results[0].result and len(entries_at_result[0]) == 1 and not hook_release.is_set()
                return
            if case in ("early", "batch"):
                # Keep the foreground model response open until both Bash exits
                # are observed. The CLI can then batch both pending notifications.
                await until(lambda: (cwd / ("streaming-native-stream-" + case)).exists())
                if case == "batch":
                    release("b")
                release("a")
                await until(lambda: len(updates) == (2 if case == "batch" else 1))
                assert not results
                provider.release("native-stream-" + case)
            await until(lambda: results)
            assert results[0].user_message_uuid == human and results[0].terminal_reason == "completed"
            assert len(entries_at_result[0]) == 1
            if case == "continue_stop":
                assert len(hooks) == count == 2 and len(prompts) == 1 and results[0].result
                first, second = continuation_entries
                assert all(isinstance(key, str) and key for key in (first[0], second[0]))
                assert first[0] != second[0] and first[1] is False and second[1] is True
                return
            if case == "cron":
                assert hooks[0][0] == [] and hooks[0][1]
                return session, hooks[0][1]
            assert starts and (starts[0][:2] in membership[0])
            if case in ("stop", "omitted"):
                await client.stop_task(starts[0][0])
                await until(lambda: updates and membership[-1] == [] and states[-1] == "idle")
                assert updates == [starts[0][:2]] and (not notes if case == "omitted" else notes == starts)
                return
            if case == "batch":
                # The CLI emits the extra receipt without a model response. Hold
                # the subsequent body turn until the reader has consumed that receipt.
                await until(lambda: len(results) == 2 and (cwd / "streaming-batch-wake").exists())
                assert results[1].terminal_reason is None and not results[1].result
                provider.release("batch-wake")
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
            # F1′/F2. In these controlled scenarios the next turn is released
            # separately (batch has one extra receipt). Each body has its own
            # Stop entry before its Result, including the native task wake.
            body_count = _check_stop_entries(results, entries_at_result)
            assert set(prompts) == set(entries_at_result[-1])
            if case == "single":
                assert len(repeats) == 2 and set(repeats) == set(prompts)
            if case == "batch":
                assert body_count == 2 and sum(r.terminal_reason is None and not r.result and not r.is_error for r in results) == 1
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
            hook_release.set()
            if case in ("reader_disconnect", "process_exit"):
                release("a")
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            await client.disconnect()
    finally:
        provider.background_script = None

def _check_stop_entries(results, entries_at_result):
    previous = set()
    body_count = 0
    for result, entries in zip(results, entries_at_result, strict=True):
        new = entries.keys() - previous
        if (result.subtype, result.terminal_reason, result.is_error) == ("success", "completed", False) and result.result:
            body_count += 1
            assert len(new) == 1 and entries[new.pop()] == result.result, "wrong Stop for body turn"
        else:
            assert not new, "unexpected Stop for receipt or error"
        previous = set(entries)
    return body_count


async def _cold_cron(cwd, session, registered):
    for scenario, reason in (("http-401", "api_error"), ("text", "completed")):
        uid = str(uuid4())
        snapshots = []

        async def stop(data, tool, ctx):
            snapshots.append([c["id"] for c in data["session_crons"]])
            return {}

        client = BundledClaudeClients().create(session, cwd, resume=True)
        client.options.env = {"CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS": "1"}
        client.options.hooks = {"Stop": [HookMatcher(hooks=[stop])]}
        await client.connect()
        try:
            await client.query(single_message(user_message(PreparedPrompt(f"[[S:{scenario}/cold-cron]]"), uid)))
            ended = idle_after_result = False
            async for msg in client.receive_messages():
                if isinstance(msg, ResultMessage) and msg.user_message_uuid == uid:
                    assert msg.session_id == session and msg.terminal_reason == reason
                    ended = True
                if ended and isinstance(msg, SystemMessage) and msg.subtype == "session_state_changed" and msg.data["state"] == "idle":
                    idle_after_result = True
                    break
            assert idle_after_result and snapshots == ([] if reason == "api_error" else [registered])
        finally:
            await client.disconnect()
