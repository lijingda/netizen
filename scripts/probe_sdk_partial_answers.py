#!/usr/bin/env python3
"""Exercise the bundled App Server against a disposable local Responses provider.

This is a native integration probe, not an App Server notification fixture. It
never uses a real model account. Run explicitly; it binds a loopback HTTP port.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import deque
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.metadata
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
from typing import Any, Iterator


def message_events(item_id: str, text: str, phase: str) -> list[dict[str, Any]]:
    item = {
        "type": "message", "role": "assistant", "id": item_id, "phase": phase,
        "content": [{"type": "output_text", "text": text}],
    }
    return [
        {"type": "response.output_item.added", "item": {**item, "content": []}},
        {"type": "response.output_text.delta", "delta": text},
        {"type": "response.output_item.done", "item": item},
    ]


def response_events(response_id: str, *items: dict[str, Any]) -> bytes:
    events = [
        {"type": "response.created", "response": {"id": response_id}},
        *items,
        {"type": "response.completed", "response": {
            "id": response_id,
            "usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
        }},
    ]
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()


class ResponsesServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, replies: list[tuple[int, bytes]]) -> None:
        super().__init__(("127.0.0.1", 0), ResponsesHandler)
        self.replies = deque(replies)
        self.requests: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.reply_lock = threading.Lock()
        self.response_gates: dict[int, threading.Event] = {}


class ResponsesHandler(BaseHTTPRequestHandler):
    server: ResponsesServer

    def log_message(self, *_args: object) -> None:
        pass

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        try:
            request = json.loads(body)
        except (ValueError, UnicodeDecodeError) as error:
            self.server.errors.append(f"invalid request body: {error}")
            self.send_error(400)
            return
        with self.server.reply_lock:
            self.server.requests.append({"path": self.path, "body": request})
            gate = self.server.response_gates.get(len(self.server.requests))
            if self.headers.get("Authorization"):
                self.server.errors.append("unexpected Authorization header")
            if self.path != "/v1/responses":
                self.server.errors.append(f"unexpected path: {self.path}")
            if self.server.replies:
                status, reply = self.server.replies.popleft()
            else:
                self.server.errors.append("unexpected extra model request")
                status, reply = 500, b'{"error":{"message":"probe reply exhausted"}}'
        if gate is not None and not gate.wait(timeout=20):
            self.server.errors.append("model response barrier timed out")
            self.send_error(504)
            return
        self.send_response(status)
        self.send_header(
            "Content-Type", "text/event-stream" if status == 200 else "application/json"
        )
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        try:
            self.wfile.write(reply)
        except (BrokenPipeError, ConnectionResetError):
            self.server.errors.append("model response connection closed early")


@contextmanager
def mock_provider(replies: list[tuple[int, bytes]]) -> Iterator[ResponsesServer]:
    server = ResponsesServer(replies)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        for gate in server.response_gates.values():
            gate.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


def isolated_config(root: Path, server: ResponsesServer) -> tuple[Path, dict[str, str]]:
    home = root / "home"
    codex_home = root / "codex"
    cwd = root / "project"
    for directory in (home, codex_home, cwd):
        directory.mkdir()
    (codex_home / "config.toml").write_text(
        'model = "gpt-5.4"\nmodel_provider = "native_probe"\n'
        'approval_policy = "never"\napprovals_reviewer = "user"\n'
        'model_auto_compact_token_limit = 1000000\n'
        'compact_prompt = "Summarize the conversation for NATIVE-COMPACT-PROBE."\n'
        '[model_providers.native_probe]\nname = "Disposable local probe"\n'
        f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n'
        'wire_api = "responses"\nrequires_openai_auth = false\n'
        'request_max_retries = 0\nstream_max_retries = 0\n'
        f'[projects.{json.dumps(str(cwd))}]\ntrust_level = "trusted"\n',
        encoding="utf-8",
    )
    env = {
        "HOME": str(home), "CODEX_HOME": str(codex_home),
        "OPENAI_API_KEY": "", "CODEX_API_KEY": "",
        "OPENAI_BASE_URL": "", "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "netizen_native_probe",
        "HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": "",
        "http_proxy": "", "https_proxy": "", "all_proxy": "",
        "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
    }
    return cwd, env


def _json(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)


def _answers(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {key: item[key] for key in ("id", "text", "phase")}
        for item in items if item.get("type") == "agentMessage"
    ]


async def partial_case(name: str, root: Path) -> dict[str, Any]:
    from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, Sandbox

    if name == "partial_tool_partial_final":
        expected = [
            {"id": "partial-before", "text": "Stable first answer.", "phase": "partial_answer"},
            {"id": "partial-after", "text": "Stable second answer.", "phase": "partial_answer"},
            {"id": "final", "text": "Final distinct answer.", "phase": "final_answer"},
        ]
        tool = {"type": "response.output_item.done", "item": {
            "type": "function_call", "call_id": "probe-command", "name": "exec_command",
            "arguments": json.dumps({"cmd": "printf NATIVE-PARTIAL-TOOL", "login": False}),
        }}
        replies = [
            (200, response_events("response-one", *message_events(
                expected[0]["id"], expected[0]["text"], expected[0]["phase"]
            ), tool)),
            (200, response_events("response-two", *message_events(
                expected[1]["id"], expected[1]["text"], expected[1]["phase"]
            ), *message_events(expected[2]["id"], expected[2]["text"], expected[2]["phase"]))),
        ]
    else:
        expected = [{"id": "partial-only", "text": "Only stable answer.", "phase": "partial_answer"}]
        replies = [(200, response_events("response-only", *message_events(
            expected[0]["id"], expected[0]["text"], expected[0]["phase"]
        )))]
    with mock_provider(replies) as server:
        cwd, env = isolated_config(root, server)
        async with AsyncCodex(CodexConfig(cwd=str(cwd), env=env)) as codex:
            thread = await codex.thread_start(
                cwd=str(cwd), approval_mode=ApprovalMode.deny_all, sandbox=Sandbox.read_only
            )
            handle = await thread.turn("NATIVE-PARTIAL-PROBE: return the scripted response.")
            notifications = []
            async with asyncio.timeout(30):
                async for event in handle.stream():
                    notifications.append({"method": event.method, "params": _json(event.payload)})
            terminals = [x["params"]["turn"] for x in notifications if x["method"] == "turn/completed"]
            assert len(terminals) == 1 and terminals[0]["status"] == "completed", terminals
            assert terminals[0]["id"] == handle.id
            completed_items = [x["params"]["item"] for x in notifications if x["method"] == "item/completed"]
            assert _answers(completed_items) == expected, completed_items
            snapshot = _json(await thread.read(include_turns=True))["thread"]
            actual = [turn for turn in snapshot["turns"] if turn["id"] == handle.id]
            assert len(actual) == 1 and actual[0]["status"] == "completed", actual
            assert actual[0]["itemsView"] == "full", actual[0]
            assert _answers(actual[0]["items"]) == expected, actual[0]
            if name == "partial_tool_partial_final":
                followup = server.requests[1]["body"]["input"]
                outputs = [item for item in followup if item.get("type") == "function_call_output"]
                assert any(item.get("call_id") == "probe-command" and "NATIVE-PARTIAL-TOOL" in str(item.get("output")) for item in outputs), outputs
                commands = [item for item in completed_items if item.get("type") == "commandExecution"]
                assert len(commands) == 1 and commands[0]["status"] == "completed", commands
                assert commands[0].get("exitCode") == 0, commands
                order = [item["type"] for item in completed_items if item["type"] in {"agentMessage", "commandExecution"}]
                assert order == ["agentMessage", "commandExecution", "agentMessage", "agentMessage"], order
            assert not server.errors and not server.replies, (server.errors, len(server.replies))
            return {
                "scenario": name, "passed": True, "thread_id": thread.id, "turn_id": handle.id,
                "root_turn_id": actual[0].get("rootTurnId"),
                "status": actual[0]["status"], "answers": _answers(actual[0]["items"]),
                "terminal_items_view": terminals[0].get("itemsView"),
                "terminal_answers": _answers(terminals[0].get("items", [])),
                "notification_methods": [x["method"] for x in notifications],
                "model_request_count": len(server.requests),
                "tool_execution_verified": name == "partial_tool_partial_final",
            }


async def large_output_case(root: Path, *, mcp: bool = False) -> dict[str, Any]:
    """Distinguish persisted command/MCP truncation from stable assistant text."""
    from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, Sandbox

    partial = "P" * (72 * 1024)
    final = "F" * (72 * 1024)
    command = shlex.join([sys.executable, "-c", 'print("X" * (80 * 1024))'])
    tool = {"type": "response.output_item.done", "item": {
        "type": "function_call", "call_id": "large-output", "name": "exec_command",
        "arguments": json.dumps({"cmd": command, "login": False, "max_output_tokens": 50000}),
    }}
    if mcp:
        tool["item"].update(name="large_result", namespace="mcp__large", arguments="{}")
    replies = [
        (200, response_events("large-one", *message_events("large-partial", partial, "partial_answer"), tool)),
        (200, response_events("large-two", *message_events("large-final", final, "final_answer"))),
    ]
    with mock_provider(replies) as server:
        cwd, env = isolated_config(root, server)
        if mcp:
            path = Path(env["CODEX_HOME"]) / "config.toml"
            args = [str(Path(__file__).with_name("probe_sdk_native_boundaries.py").resolve()),
                    "--mcp-server", str(root), "--mcp-large-result"]
            path.write_text(path.read_text() + '\n[mcp_servers.large]\nrequired = true\nstartup_timeout_sec = 15\n'
                            f'command = {json.dumps(sys.executable)}\nargs = {json.dumps(args)}\n')
        config = CodexConfig(cwd=str(cwd), env=env)
        async with AsyncCodex(config) as codex:
            thread = await codex.thread_start(
                cwd=str(cwd), approval_mode=ApprovalMode.deny_all, sandbox=Sandbox.read_only
            )
            handle = await thread.turn("NATIVE-LARGE-PROBE")
            completed = []
            terminal = None
            async with asyncio.timeout(30):
                async for event in handle.stream():
                    payload = _json(event.payload)
                    if event.method == "item/completed":
                        completed.append(payload["item"])
                    elif event.method == "turn/completed":
                        terminal = payload["turn"]
            assert terminal is not None and terminal["status"] == "completed", terminal
            item_type = "mcpToolCall" if mcp else "commandExecution"
            live_tools = [item for item in completed if item["type"] == item_type]
            assert len(live_tools) == 1 and live_tools[0]["status"] == "completed", live_tools
            if mcp:
                assert (root / "initialize-seen").exists() and (root / "tools-listed").exists()
                assert (root / "tool-called").exists(), "real MCP tools/call did not execute"
                assert live_tools[0]["server"] == "large" and live_tools[0]["tool"] == "large_result", live_tools[0]
                live_output = json.dumps(live_tools[0]["result"], separators=(",", ":"))
                assert live_tools[0]["result"]["structuredContent"] == {"probe": "native-large-result"}
            else:
                assert live_tools[0].get("exitCode") == 0, live_tools
                live_output = live_tools[0]["aggregatedOutput"]
            assert len(live_output.encode()) > 64 * 1024, len(live_output)
            snapshot = _json(await thread.read(include_turns=True))["thread"]
            live_turn = next(turn for turn in snapshot["turns"] if turn["id"] == handle.id)
            immediate_tools = [item for item in live_turn["items"] if item["type"] == item_type]
            assert len(immediate_tools) == 1
            immediate_output = (json.dumps(immediate_tools[0]["result"], separators=(",", ":"))
                                if mcp else immediate_tools[0]["aggregatedOutput"])
        # A new process/read prevents an in-memory payload from masking persistence.
        async with AsyncCodex(config) as codex:
            resumed = await codex.thread_resume(thread.id)
            snapshot = _json(await resumed.read(include_turns=True))["thread"]
            stored_turn = next(turn for turn in snapshot["turns"] if turn["id"] == handle.id)
            stored_tools = [item for item in stored_turn["items"] if item["type"] == item_type]
            assert len(stored_tools) == 1 and stored_tools[0]["status"] == "completed", stored_tools
            if mcp:
                stored_result = stored_tools[0]["result"]
                stored_output = json.dumps(stored_result, separators=(",", ":"))
                assert len(stored_output.encode()) <= 64 * 1024, len(stored_output)
                preview = stored_result["content"][0]["text"]
                assert "chars truncated" in preview and "NATIVE-MCP-HEAD" in preview and "NATIVE-MCP-TAIL" in preview
                assert "structuredContent" not in stored_result, "large MCP result should become a text preview"
                assert stored_tools[0]["arguments"] == live_tools[0]["arguments"] == {}
            else:
                stored_output = stored_tools[0]["aggregatedOutput"]
                assert len(stored_output.encode()) == 64 * 1024, len(stored_output)
                assert "command output truncated for persistence" in stored_output, "missing persistence truncation marker"
            assert immediate_tools[0]["status"] == "completed", immediate_tools
            assert immediate_output == stored_output, "immediate and cold history truncation differ"
            expected = [
                {"id": "large-partial", "text": partial, "phase": "partial_answer"},
                {"id": "large-final", "text": final, "phase": "final_answer"},
            ]
            assert _answers(completed) == expected, "live assistant text changed"
            assert _answers(live_turn["items"]) == expected, "immediate read assistant text changed"
            assert _answers(stored_turn["items"]) == expected, "persisted assistant text changed"
            assert not server.errors and not server.replies and len(server.requests) == 2, server.errors
            result = {
                "scenario": "large_mcp_history" if mcp else "large_command_history", "passed": True,
                "thread_id": thread.id, "turn_id": handle.id,
                "persistence_truncation_marker": True,
                "partial_bytes_preserved": len(partial.encode()),
                "final_bytes_preserved": len(final.encode()),
                "fresh_app_server_read_verified": True, "mcp_result_tested": mcp,
            }
            label = "mcp_result" if mcp else "command_output"
            result.update({f"live_{label}_bytes": len(live_output.encode()),
                           f"immediate_read_{label}_bytes": len(immediate_output.encode()),
                           f"persisted_{label}_bytes": len(stored_output.encode())})
            if mcp:
                result.update(real_mcp_call_verified=True, bounded_text_preview=True,
                              structured_content_replaced=True, arguments_preserved=True)
            return result


async def goal_rollover_case(root: Path) -> dict[str, Any]:
    """Real Goal continuation, SDK routing, production tap and Runtime retention."""
    from dataclasses import asdict

    from openai_codex import AsyncCodex, CodexConfig

    source_root = str(Path(__file__).resolve().parents[1])
    if source_root not in sys.path:
        sys.path.insert(0, source_root)
    from netizen_cli.bindings import BindingStore
    from netizen_cli.codex_runtime import CodexRuntime
    from netizen_cli.domain import FeishuScope, ScopeKind
    from netizen_cli.sdk_gap_adapter import AppServerGoalControl, GoalStatus
    from netizen_cli.terminal_cleanup import PinnedExperimentalTerminalCleanup

    complete_tool = {"type": "response.output_item.done", "item": {
        "type": "function_call", "call_id": "complete-goal", "name": "update_goal",
        "arguments": json.dumps({"status": "complete"}),
    }}
    replies = [
        (200, response_events("goal-one",
            *message_events("goal-partial-one", "First goal stage.", "partial_answer"),
            *message_events("goal-draft-one", "First physical draft.", "final_answer"))),
        (200, response_events("goal-two",
            *message_events("goal-partial-two", "Second goal stage.", "partial_answer"), complete_tool)),
        (200, response_events("goal-final",
            *message_events("goal-final-two", "Goal completed final.", "final_answer"))),
    ]
    with mock_provider(replies) as provider:
        first_gate, final_gate = threading.Event(), threading.Event()
        provider.response_gates = {1: first_gate, 3: final_gate}
        cwd, env = isolated_config(root, provider)
        config_path = Path(env["CODEX_HOME"]) / "config.toml"
        config_path.write_text(config_path.read_text() + "\n[features]\ngoals = true\n")
        store = BindingStore(root / "channel.sqlite3")
        store.bootstrap_project(alias="goal-probe", cwd=str(cwd))
        binding = store.create_channel_binding(
            scope=FeishuScope("probe-app", "probe-chat", ScopeKind.GROUP),
            project_alias="goal-probe", creator_id="probe-owner",
        )
        outcomes = []
        completed = asyncio.Event()

        async def on_completion(outcome: Any) -> None:
            outcomes.append(outcome)
            completed.set()

        try:
            async with AsyncCodex(CodexConfig(cwd=str(cwd), env=env)) as codex:
                control = AppServerGoalControl(codex)
                runtime = CodexRuntime(
                    codex=codex, bindings=store, goal_control=control,
                    terminal_cleanup=PinnedExperimentalTerminalCleanup(codex),
                    on_completion=on_completion, automatic_thread_naming=False,
                )
                try:
                    submission = await runtime.start_goal(
                        binding=store.get(binding.id), cwd=cwd,
                        objective="Complete the two scripted stages and mark the goal complete.",
                        owner_id="probe-owner", origin=object(),
                    )
                    thread_id = submission.thread_id
                    submission.release_receipt_attempt()
                    first_gate.set()
                    async with asyncio.timeout(15):
                        while True:
                            active = runtime.goal_activity(binding.id)
                            if active is not None and len(active.partial_answers) == 2 and len(provider.requests) == 3:
                                break
                            await asyncio.sleep(0.02)
                    partials = active.partial_answers
                    assert [answer.text for answer in partials] == ["First goal stage.", "Second goal stage."], partials
                    turn_ids = [answer.turn_id for answer in partials]
                    assert len(set(turn_ids)) == 2 and active.physical_turn_id == turn_ids[1], active
                    assert all(answer.thread_id == thread_id for answer in partials), partials
                    assert submission.logical_turn_id == turn_ids[0], submission
                    assert not completed.is_set(), "physical rollover prematurely completed logical Goal"
                    # The third model request proves the native update_goal tool executed.
                    tool_outputs = [item for item in provider.requests[2]["body"]["input"]
                                    if item.get("type") == "function_call_output" and item.get("call_id") == "complete-goal"]
                    assert len(tool_outputs) == 1 and "complete" in str(tool_outputs[0]["output"]), tool_outputs
                    native_goal = await control.get(thread_id)
                    assert native_goal is not None and native_goal.status is GoalStatus.COMPLETE, native_goal
                    final_gate.set()
                    await asyncio.wait_for(completed.wait(), 15)
                    assert await runtime.wait_idle(timeout=5)
                    assert len(outcomes) == 1, outcomes
                    outcome = outcomes[0]
                    assert outcome.error is None and outcome.finalization_error is None, outcome
                    assert outcome.partial_answers == partials, outcome
                    assert outcome.final_physical_turn_id == turn_ids[1], outcome
                    assert outcome.final_response == "Goal completed final.", outcome
                    assert outcome.final_turn_status == "completed", outcome
                    assert outcome.finalization.value == "cleared", outcome
                    assert outcome.goal.status is GoalStatus.COMPLETE, outcome
                    assert await control.get(thread_id) is None, "completed Goal was not cleared"
                    thread = await codex.thread_resume(thread_id)
                    snapshot = _json(await thread.read(include_turns=True))["thread"]
                    assert snapshot["status"]["type"] == "idle", snapshot
                    turns = snapshot["turns"]
                    assert [turn["id"] for turn in turns] == turn_ids, turns
                    assert all(turn["status"] == "completed" for turn in turns), turns
                    persisted_partials = [
                        {"thread_id": thread_id, "turn_id": turn["id"], "item_id": item["id"], "text": item["text"]}
                        for turn in turns for item in turn["items"]
                        if item.get("type") == "agentMessage" and item.get("phase") == "partial_answer"
                    ]
                    assert persisted_partials == [asdict(answer) for answer in partials], persisted_partials
                    assert not provider.errors and not provider.replies and len(provider.requests) == 3, provider.errors
                    return {
                        "scenario": "goal_partial_automatic_rollover", "passed": True,
                        "thread_id": thread_id, "physical_turn_ids": turn_ids,
                        "partial_answers": persisted_partials,
                        "model_request_count": 3, "automatic_continuation": True,
                        "real_update_goal_complete_verified": True,
                        "production_goal_tap_and_runtime_retention": True,
                        "partials_observed_before_final": True,
                        "final_response": outcome.final_response,
                        "final_turn_id": outcome.final_physical_turn_id,
                        "four_proof_runtime_finalization": outcome.finalization.value,
                        "goal_absence_confirmed": True, "native_history_two_completed_turns": True,
                        "feishu_delivery_tested": False,
                    }
                finally:
                    first_gate.set()
                    final_gate.set()
                    await runtime.interrupt_all()
        finally:
            store.close()


async def run_probe(selected: str = "all") -> dict[str, Any]:
    from codex_cli_bin import bundled_codex_path

    binary = str(bundled_codex_path())
    version = subprocess.run([binary, "--version"], check=True, capture_output=True, text=True, timeout=10).stdout.strip()
    cases = []
    with tempfile.TemporaryDirectory(prefix="netizen-native-partial-") as directory:
        names = ("partial_tool_partial_final", "partial_only_completed", "large_command_history", "large_mcp_history", "goal_partial_rollover") if selected == "all" else (selected,)
        for name in names:
            root = Path(directory) / name
            root.mkdir()
            async with asyncio.timeout(45):
                if name == "goal_partial_rollover":
                    cases.append(await goal_rollover_case(root))
                else:
                    cases.append(await (large_output_case(root, mcp=name == "large_mcp_history") if name.startswith("large_") else partial_case(name, root)))
    return {
        "sdk_version": importlib.metadata.version("openai-codex"),
        "bundled_version": version, "transport": "bundled App Server + local mock Responses HTTP SSE",
        "real_model_used": False, "cases": cases,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("all", "partial_tool_partial_final", "partial_only_completed", "large_command_history", "large_mcp_history", "goal_partial_rollover"), default="all")
    parser.add_argument("--output", type=Path, help="Optional JSON evidence file")
    args = parser.parse_args()
    result = asyncio.run(run_probe(args.case))
    output = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    print(output, end="")


if __name__ == "__main__":
    main()
