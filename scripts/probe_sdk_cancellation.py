#!/usr/bin/env python3
"""Bounded native Goal/cancellation probes using a disposable mock provider.

The Goal case verifies that an SDK observation gap affects only that Goal, and
ordinary input resumes after native idle is confirmed. It does not verify Goal
continuation support. It traverses production GoalControl and Runtime. Dynamic-tool cases
use the public App Server wire because the Python facade does not expose tool
registration. No real model, credentials, Feishu or user configuration is used.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import traceback
from typing import Any

from probe_sdk_native_boundaries import final_reply, native_server, run_turn, start_thread
from probe_sdk_partial_answers import _json, isolated_config, message_events, mock_provider, response_events


async def goal_observation_gap_case(root: Path) -> dict[str, Any]:
    """Contain stale SDK final identity locally, then resume ordinary input."""
    from openai_codex import AsyncCodex, CodexConfig
    from openai_codex.generated.v2_all import (
        ConfigBatchWriteParams, ConfigWriteResponse, HooksListParams, HooksListResponse,
    )

    source_root = str(Path(__file__).resolve().parents[1])
    if source_root not in sys.path:
        sys.path.insert(0, source_root)
    from netizen_cli.bindings import BindingStore
    from netizen_cli.codex_runtime import CodexRuntime, GoalStateUnknown
    from netizen_cli.domain import FeishuScope, ScopeKind
    from netizen_cli.runtime.contracts import GoalOperationState
    from netizen_cli.sdk_gap_adapter import AppServerGoalControl, GoalStatus
    from netizen_cli.terminal_cleanup import PinnedExperimentalTerminalCleanup

    events = [
        {"type": "response.created", "response": {"id": "goal-progress"}},
        *message_events("progress", "Ready for the next step", "final_answer"),
        {"type": "response.completed", "response": {
            "id": "goal-progress", "usage": {
                "input_tokens": 329990, "output_tokens": 10, "total_tokens": 330000,
            },
        }},
    ]
    reply = "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()
    with mock_provider([
        (200, reply), final_reply("other", "OTHER-THREAD-OK"),
        final_reply("compact", "COMPACTED-CONTEXT"), final_reply("followup", "SAME-THREAD-OK"),
    ]) as provider:
        gate = threading.Event()
        provider.response_gates = {1: gate}
        cwd, env = isolated_config(root, provider)
        config_path = Path(env["CODEX_HOME"]) / "config.toml"
        # Hold the third physical Turn inside its real PreCompact hook. The
        # earlier interrupted Turn has ended the SDK stream while this later
        # Turn remains in progress, so the mismatch is independent of timing.
        hook_entered = root / "later-hook-entered"
        hook_release = root / "release-later-hook"
        allow_compaction = root / "allow-compaction"
        hook_script = root / "precompact.py"
        hook_script.write_text('''import json, pathlib, sys, time
root = pathlib.Path(__file__).parent
counter = root / "hook-count"
count = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(count))
if count == 2:
    (root / "later-hook-entered").touch()
    deadline = time.monotonic() + 30
    while not (root / "release-later-hook").exists():
        if time.monotonic() >= deadline:
            sys.exit("probe hook barrier timed out")
        time.sleep(0.01)
print(json.dumps({"continue": (root / "allow-compaction").exists()}))
''')
        config = config_path.read_text().replace(
            "model_auto_compact_token_limit = 1000000",
            "model_auto_compact_token_limit = 200000\nmodel_post_turn_compact_threshold_percent = 0",
        )
        config_path.write_text(config + '''
[features]
goals = true
hooks = true
[[hooks.PreCompact]]
matcher = "auto"
[[hooks.PreCompact.hooks]]
type = "command"
''' + "command = " + json.dumps(shlex.join([sys.executable, str(hook_script)])) + "\n")
        store = BindingStore(root / "channel.sqlite3")
        store.bootstrap_project(alias="goal-probe", cwd=str(cwd))
        binding = store.create_channel_binding(
            scope=FeishuScope("probe-app", "probe-chat", ScopeKind.GROUP),
            project_alias="goal-probe", creator_id="probe-owner",
        )
        other_binding = store.create_channel_binding(
            scope=FeishuScope("probe-app", "other-probe-chat", ScopeKind.GROUP),
            project_alias="goal-probe", creator_id="probe-owner",
        )
        completed = asyncio.Event()
        outcomes = []

        async def on_completion(outcome: Any) -> None:
            outcomes.append(outcome)
            completed.set()

        try:
            async with AsyncCodex(CodexConfig(cwd=str(cwd), env=env)) as codex:
                # Trust only the disposable hook, using SDK-generated models.
                hooks = await codex._client.request(
                    "hooks/list", _json(HooksListParams(cwds=[str(cwd)])),
                    response_model=HooksListResponse,
                )
                assert len(hooks.data) == 1 and not hooks.data[0].errors, hooks
                assert len(hooks.data[0].hooks) == 1, hooks
                hook = hooks.data[0].hooks[0].root
                params = ConfigBatchWriteParams.model_validate({
                    "edits": [{"keyPath": "hooks.state", "mergeStrategy": "upsert",
                               "value": {hook.key: {"trusted_hash": hook.current_hash}}}],
                    "reloadUserConfig": True,
                })
                await codex._client.request(
                    "config/batchWrite", _json(params), response_model=ConfigWriteResponse,
                )
                class RecordingGoalControl(AppServerGoalControl):
                    """Record only Runtime clear calls; all behavior stays native."""

                    clear_calls = 0

                    async def clear(self, thread_id: str) -> bool:
                        self.clear_calls += 1
                        return await super().clear(thread_id)

                control = RecordingGoalControl(codex)
                runtime = CodexRuntime(
                    codex=codex, bindings=store, goal_control=control,
                    terminal_cleanup=PinnedExperimentalTerminalCleanup(codex),
                    on_completion=on_completion, automatic_thread_naming=False,
                )
                try:
                    submission = await runtime.start_goal(
                        binding=store.get(binding.id), cwd=cwd,
                        objective="Finish the scripted work.", owner_id="probe-owner", origin=object(),
                    )
                    submission.release_receipt_attempt()
                    gate.set()
                    await asyncio.wait_for(completed.wait(), 30)
                    assert await runtime.wait_idle(timeout=5)
                    assert len(outcomes) == 1, outcomes
                    outcome = outcomes[0]
                    assert isinstance(outcome.error, GoalStateUnknown), outcome
                    assert outcome.finalization_error is None, outcome
                    assert outcome.goal is None and outcome.final_turn_status is None, outcome
                    assert outcome.final_response is None, outcome
                    assert outcome.final_items == (), outcome
                    assert outcome.turn_diff is None and outcome.patch_children.batches == (), outcome
                    assert outcome.partial_answers == (), outcome
                    assert outcome.finalization.value == "not-applicable", outcome
                    retained = runtime.active_goal(binding.id)
                    assert retained is not None and retained.state is GoalOperationState.UNKNOWN, retained
                    assert retained.logical_turn_id == submission.logical_turn_id, retained
                    assert retained.thread_id == submission.thread_id, retained
                    assert runtime._accepting, "Goal observation gap closed the whole Runtime"
                    try:
                        await runtime.capture_submission_admission(binding.id)
                    except GoalStateUnknown:
                        pass
                    else:
                        raise AssertionError("unknown active Goal admitted input before native idle")
                    assert control.clear_calls == 0, control.clear_calls
                    async with asyncio.timeout(5):
                        while not hook_entered.exists():
                            await asyncio.sleep(0.01)
                    assert not hook_release.exists(), "later native Turn was not held"
                    thread = await codex.thread_resume(submission.thread_id)
                    in_progress = _json(await thread.read(include_turns=True))["thread"]
                    assert in_progress["turns"][-1]["status"] == "inProgress", in_progress
                    assert in_progress["turns"][-1]["id"] != outcome.final_physical_turn_id, outcome
                    active_goal = await control.get(submission.thread_id)
                    assert active_goal is not None and active_goal.status is GoalStatus.ACTIVE, active_goal

                    completed.clear()
                    other = await runtime.submit(
                        binding=store.get(other_binding.id), cwd=cwd,
                        input="Complete an independent ordinary message while the Goal is held.",
                        owner_id="probe-owner", origin="other-binding-probe",
                    )
                    assert other.disposition.value == "started", other
                    assert other.release_receipt_attempt is not None
                    other.release_receipt_attempt()
                    await asyncio.wait_for(completed.wait(), 15)
                    assert await runtime.wait_idle(timeout=5)
                    assert len(outcomes) == 2, outcomes
                    other_outcome = outcomes[1]
                    assert other_outcome.binding_id == other_binding.id, other_outcome
                    assert other_outcome.status == "completed" and other_outcome.error is None, other_outcome
                    assert other_outcome.final_response == "OTHER-THREAD-OK", other_outcome
                    assert runtime.active_goal(binding.id) == retained, "unrelated input released the unknown Goal"
                    assert not hook_release.exists(), "independent Turn did not finish during native barrier"
                    still_running = _json(await thread.read(include_turns=True))["thread"]
                    assert still_running["turns"][-1]["status"] == "inProgress", still_running

                    hook_release.touch()
                    async with asyncio.timeout(10):
                        while True:
                            goal = await control.get(submission.thread_id)
                            if goal is not None and goal.status is GoalStatus.BLOCKED:
                                break
                            await asyncio.sleep(0.02)
                    snapshot = _json(await thread.read(include_turns=True))["thread"]
                    assert snapshot["status"]["type"] == "idle", snapshot
                    turns = snapshot["turns"]
                    assert [turn["status"] for turn in turns] == [
                        "completed", "interrupted", "interrupted", "interrupted",
                    ], turns
                    assert turns[0]["id"] == submission.logical_turn_id, turns
                    assert turns[1]["id"] == outcome.final_physical_turn_id, outcome
                    assert turns[-1]["id"] != outcome.final_physical_turn_id, outcome
                    assert runtime.active_goal(binding.id) == retained, "unknown slot was released"
                    assert control.clear_calls == 0, control.clear_calls
                    assert all(not turn.get("error") for turn in turns), turns
                    assert all(not any(item["type"] in {"agentMessage", "dynamicToolCall", "commandExecution"}
                                       for item in turn["items"]) for turn in turns[1:]), turns
                    assert len(provider.requests) == 2, provider.requests

                    allow_compaction.touch()
                    admission = await runtime.capture_submission_admission(binding.id)
                    assert runtime.active_goal(binding.id) is None, "native idle did not release local Goal admission"
                    assert control.clear_calls == 0, control.clear_calls
                    completed.clear()
                    followup = await runtime.submit(
                        binding=store.get(binding.id), cwd=cwd,
                        input="Continue this ordinary conversation after the hook allows compaction.",
                        owner_id="probe-owner", origin="recovered-goal-probe", admission=admission,
                    )
                    assert followup.disposition.value == "started", followup
                    assert followup.thread_id == submission.thread_id, followup
                    assert followup.turn_id not in {turn["id"] for turn in turns}, followup
                    assert followup.release_receipt_attempt is not None
                    followup.release_receipt_attempt()
                    await asyncio.wait_for(completed.wait(), 15)
                    assert await runtime.wait_idle(timeout=5)
                    assert len(outcomes) == 3, outcomes
                    followup_outcome = outcomes[2]
                    assert followup_outcome.binding_id == binding.id, followup_outcome
                    assert followup_outcome.status == "completed" and followup_outcome.error is None, followup_outcome
                    assert followup_outcome.final_response == "SAME-THREAD-OK", followup_outcome
                    assert runtime.active_turn(binding.id) is None, "ordinary followup did not release admission"
                    assert runtime.active_goal(binding.id) is None, "ordinary followup recreated local Goal ownership"
                    last_goal = await control.get(submission.thread_id)
                    assert last_goal is not None and last_goal.status is GoalStatus.BLOCKED, last_goal
                    assert control.clear_calls == 0, control.clear_calls
                    assert runtime._accepting, "ordinary followup closed the Runtime"
                    assert len(provider.requests) == 4 and not provider.errors and not provider.replies, provider.errors
                    return {
                        "scenario": "goal_interrupted_observation_gap", "passed": True,
                        "verification_scope": "Goal-local observation gap and ordinary input recovery",
                        "native_goal_continuation_supported": False,
                        "exact_final_delivery_verified": False,
                        "thread_id": thread.id, "physical_turn_ids": [turn["id"] for turn in turns],
                        "physical_statuses": [turn["status"] for turn in turns],
                        "model_request_count": len(provider.requests),
                        "production_goal_control_and_runtime": True,
                        "goal_logical_outcome_count": 1, "ordinary_outcome_count": 2,
                        "persisted_goal_status": last_goal.status.value,
                        "native_thread_idle_after_barrier_release": True,
                        "guard_detected_active_goal_with_later_in_progress_turn": True,
                        "goal_state_after_observation_gap": retained.state.value,
                        "instance_admission_remained_open": True,
                        "affected_goal_input_rejected_while_native_active": True,
                        "other_binding_completed_while_native_active": True,
                        "ordinary_input_recheck_released_idle_goal": True,
                        "same_thread_ordinary_followup_completed": True,
                        "followup_turn_id": followup.turn_id,
                        "runtime_clear_calls": control.clear_calls,
                        "stale_result_and_files_withheld": True,
                        "stream_final_turn_id_preserved": outcome.final_physical_turn_id,
                        "native_final_turn_id": turns[-1]["id"],
                    }
                finally:
                    gate.set()
                    hook_release.touch()
                    await runtime.interrupt_all()
        finally:
            store.close()


def tool_reply(call_id: str, name: str, arguments: dict[str, Any]) -> tuple[int, bytes]:
    return 200, response_events(call_id, {"type": "response.output_item.done", "item": {
        "type": "function_call", "call_id": call_id, "name": name,
        "arguments": json.dumps(arguments),
    }})


def exec_reply(call_id: str, code: str) -> tuple[int, bytes]:
    return 200, response_events(call_id, {"type": "response.output_item.done", "item": {
        "type": "custom_tool_call", "call_id": call_id, "name": "exec", "input": code,
    }})


def call_output(request: dict[str, Any], call_id: str) -> str:
    outputs = [item["output"] for item in request["body"]["input"]
               if item.get("call_id") == call_id and item["type"] in {
                   "function_call_output", "custom_tool_call_output",
               }]
    assert len(outputs) == 1, (call_id, outputs)
    return json.dumps(outputs[0])


async def cancellation_case(root: Path, *, code_mode: bool) -> dict[str, Any]:
    replies = []
    if code_mode:
        replies.extend([
            exec_reply("store", 'store("persisted", "preserved");'), final_reply("stored", "Stored."),
            exec_reply("background", "yield_control(); await new Promise(() => {});"),
            final_reply("background-running", "Running."),
            exec_reply("nested", "await tools.blocking({});"),
        ])
    else:
        replies.append(tool_reply("direct", "blocking", {}))
    with mock_provider(replies) as provider:
        cwd, env = isolated_config(root, provider)
        if code_mode:
            path = Path(env["CODEX_HOME"]) / "config.toml"
            # Deliberately omit code_mode_interrupt: this probes its new default.
            path.write_text(path.read_text() + "\n[features]\ncode_mode = true\n")
        async with native_server(cwd, env) as server:
            client = await server.connect()
            thread_id = await start_thread(client, cwd, dynamicTools=[{
                "type": "function", "name": "blocking", "description": "Wait until cancelled.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
                "deferLoading": False,
            }])
            background_cell_id = None
            if code_mode:
                await run_turn(client, thread_id, "Store the scripted value.")
                await run_turn(client, thread_id, "Start a background cell.")
                output = call_output(provider.requests[3], "background")
                match = re.search(r"Script running with cell ID ([A-Za-z0-9-]+)", output)
                assert match is not None, output
                background_cell_id = match[1]
            after = len(client.notifications)
            started = await client.request("turn/start", {
                "threadId": thread_id, "input": [{"type": "text", "text": "Start the blocked tool."}],
            })
            turn_id = started["turn"]["id"]
            request = await client.wait(lambda event: event.get("method") == "item/tool/call"
                                        and event["params"]["turnId"] == turn_id, after=after)
            call_id = request["params"]["callId"]
            assert request["params"]["tool"] == "blocking", request
            await client.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
            terminal = await client.wait(lambda event: event.get("method") == "turn/completed"
                                         and event["params"]["turn"]["id"] == turn_id, after=after)
            assert terminal["params"]["turn"]["status"] == "interrupted", terminal
            done = await client.wait(lambda event: event.get("method") == "item/completed"
                                     and event["params"]["item"].get("id") == call_id, after=after)
            item = done["params"]["item"]
            assert done["params"]["threadId"] == thread_id and done["params"]["turnId"] == turn_id, done
            assert item["type"] == "dynamicToolCall" and item["status"] == "failed", item
            assert item["tool"] == "blocking" and item["arguments"] == {}, item
            assert item["success"] is False and item["contentItems"] == [], item
            # Public ThreadItem omits the core DynamicToolCallItem.error field.
            assert isinstance(item["durationMs"], int) and item["durationMs"] >= 0, item
            recovery_start = len(provider.requests)
            if code_mode:
                with provider.reply_lock:
                    provider.replies.extend([
                        tool_reply("wait-background", "wait", {"cell_id": background_cell_id, "yield_time_ms": 1}),
                        exec_reply("recover", 'text(load("persisted"));'),
                        final_reply("recovered", "Recovered."),
                    ])
            else:
                with provider.reply_lock:
                    provider.replies.append(final_reply("recovered", "Recovered."))
            recovered = await run_turn(client, thread_id, "Continue after interruption.")
            assert recovered["id"] != turn_id, recovered
            if code_mode:
                assert "not found" in call_output(provider.requests[recovery_start + 1], "wait-background"), provider.requests
                assert "preserved" in call_output(provider.requests[recovery_start + 2], "recover"), provider.requests
            snapshot = await client.request("thread/read", {"threadId": thread_id, "includeTurns": True})
            assert snapshot["thread"]["status"]["type"] == "idle", snapshot
            original = next(turn for turn in snapshot["thread"]["turns"] if turn["id"] == turn_id)
            persisted = [i for i in original["items"] if i["id"] == call_id]
            assert len(persisted) == 1 and persisted[0]["status"] == "failed", original
            completed = [event for event in client.notifications[after:]
                         if event.get("method") == "item/completed" and event["params"]["item"].get("id") == call_id]
            assert len(completed) == 1, completed
            assert not provider.errors and not provider.replies, (provider.errors, len(provider.replies))
            return {
                "scenario": "code_mode_interrupt" if code_mode else "dynamic_tool_interrupt", "passed": True,
                "thread_id": thread_id, "interrupted_turn_id": turn_id,
                "dynamic_item_id": call_id, "dynamic_item_status": item["status"],
                "dynamic_item_success": item["success"], "item_completed_count": len(completed),
                "history_terminal_verified": True, "followup_completed": True,
                "model_request_count": len(provider.requests),
                "code_mode": ({"background_cell_removed": True, "nested_tool_terminal": True,
                               "store_preserved": True, "interrupt_default_tested": True,
                               "foreground_cell_id_independently_waited": False}
                              if code_mode else None),
            }


CASES = ("goal_interrupted_observation_gap", "dynamic_tool_interrupt", "code_mode_interrupt")


async def run_probe(selected: str) -> dict[str, Any]:
    from codex_cli_bin import bundled_codex_path

    version = subprocess.run([str(bundled_codex_path()), "--version"], check=True,
                             capture_output=True, text=True, timeout=10).stdout.strip()
    cases = []
    with tempfile.TemporaryDirectory(prefix="netizen-native-cancellation-") as directory:
        for name in CASES if selected == "all" else (selected,):
            root = Path(directory) / name
            root.mkdir()
            try:
                async with asyncio.timeout(55):
                    cases.append(await (goal_observation_gap_case(root) if name == "goal_interrupted_observation_gap"
                                        else cancellation_case(root, code_mode=name == "code_mode_interrupt")))
            except Exception as error:
                # Keep first-failure evidence and still run independent cases.
                # Failed/timeout cases always make the command exit nonzero.
                cases.append({"scenario": name, "passed": False, "error_type": type(error).__name__,
                              "error": str(error), "traceback": traceback.format_exc()})
    return {"sdk_version": importlib.metadata.version("openai-codex"), "bundled_version": version,
            "transport": "bundled App Server + local mock Responses HTTP SSE",
            "real_model_used": False, "cases": cases}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("all", *CASES), default="all")
    parser.add_argument("--output", type=Path, help="Optional JSON evidence file")
    args = parser.parse_args()
    result = asyncio.run(run_probe(args.case))
    output = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    print(output, end="")
    if not all(case["passed"] for case in result["cases"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
