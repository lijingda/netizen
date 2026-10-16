#!/usr/bin/env python3
"""Probe native incomplete responses and cold-resume permissions in disposable state.

Incomplete responses run through production CodexRuntime, including its only
notification consumer and terminal read recovery. Permission responses use the
public App Server wire harness. No real model account or user state is used.
Run explicitly: this probe binds loopback ports and executes sandboxed commands.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from dataclasses import asdict
import importlib.metadata
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import tomllib
from typing import Any

CASES = (
    "incomplete_max_output_tokens", "incomplete_unknown", "incomplete_content_filter",
    "incomplete_max_output_tokens_retries", "incomplete_unknown_retries",
    "interrupted_continuation", "cold_permissions",
)


def final_reply(name: str, text: str) -> tuple[int, bytes]:
    from probe_sdk_partial_answers import message_events, response_events

    return 200, response_events(name, *message_events(name + "-message", text, "final_answer"))


def incomplete_reply(reason: str | None) -> tuple[int, bytes]:
    from probe_sdk_partial_answers import message_events

    response: dict[str, Any] = {
        "id": "incomplete", "usage": {"input_tokens": 50, "output_tokens": 20, "total_tokens": 70},
    }
    if reason is not None:
        response["incomplete_details"] = {"reason": reason}
    events = [
        {"type": "response.created", "response": {"id": "incomplete"}},
        *message_events("stable-partial", "A stable fragment before failure.", "partial_answer"),
        {"type": "response.incomplete", "response": response},
    ]
    return 200, "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()


async def incomplete_case(root: Path, reason: str | None, *, retries: int = 0) -> dict[str, Any]:
    from openai_codex import AsyncCodex, AsyncThread, CodexConfig
    from probe_sdk_partial_answers import isolated_config, mock_provider

    source_root = str(Path(__file__).resolve().parents[1])
    if source_root not in sys.path:
        sys.path.insert(0, source_root)
    from netizen_cli.bindings import BindingStore
    from netizen_cli.codex_runtime import CodexRuntime
    from netizen_cli.domain import FeishuScope, ScopeKind
    from netizen_cli.terminal_cleanup import PinnedExperimentalTerminalCleanup

    automatic_continuation = reason == "interrupted"
    expected_status = "completed" if automatic_continuation else "failed"
    replies = [final_reply("seed", "Initial normal answer."), incomplete_reply(reason)]
    if automatic_continuation:
        replies.append(final_reply("resumed", "Continued within the original Turn."))
    replies.append(final_reply("continued", "Continued in a subsequent Turn."))
    with mock_provider(replies) as provider:
        response_gate, continuation_gate = threading.Event(), threading.Event()
        provider.response_gates[2] = response_gate
        if automatic_continuation:
            provider.response_gates[3] = continuation_gate
        cwd, env = isolated_config(root, provider)
        config_path = Path(env["CODEX_HOME"]) / "config.toml"
        config_path.write_text(config_path.read_text().replace(
            "request_max_retries = 0\nstream_max_retries = 0",
            f"request_max_retries = {retries}\nstream_max_retries = {retries}",
        ), encoding="utf-8")
        configured = tomllib.loads(config_path.read_text())["model_providers"]["native_probe"]
        assert configured["request_max_retries"] == configured["stream_max_retries"] == retries
        store = BindingStore(root / "channel.sqlite3")
        store.bootstrap_project(alias="response-probe", cwd=str(cwd))
        binding = store.create_channel_binding(
            scope=FeishuScope("probe-app", "probe-chat", ScopeKind.GROUP),
            project_alias="response-probe", creator_id="probe-owner",
        )
        outcomes: list[Any] = []
        completion = asyncio.Condition()

        async def completed(outcome: Any) -> None:
            async with completion:
                outcomes.append(outcome)
                completion.notify_all()

        async def wait_completion(count: int) -> Any:
            async with asyncio.timeout(15):
                async with completion:
                    await completion.wait_for(lambda: len(outcomes) >= count)
            assert len(outcomes) == count, outcomes
            assert await runtime.wait_idle(timeout=5), "Runtime consumer did not drain"
            assert runtime.active_turn(binding.id) is None, "completed Turn still owns admission"
            return outcomes[-1]

        try:
            async with AsyncCodex(CodexConfig(cwd=str(cwd), env=env)) as codex:
                runtime = CodexRuntime(
                    codex=codex, bindings=store,
                    terminal_cleanup=PinnedExperimentalTerminalCleanup(codex),
                    on_completion=completed, poll_interval_seconds=0.01,
                    automatic_thread_naming=False,
                )

                async def submit(text: str) -> Any:
                    submission = await runtime.submit(
                        binding=store.get(binding.id), cwd=cwd, input=text,
                        owner_id="probe-owner", origin="response-probe",
                    )
                    assert submission.disposition.value == "started", submission
                    assert submission.release_receipt_attempt is not None
                    submission.release_receipt_attempt()
                    return submission

                try:
                    initial = await submit("NATIVE-RESPONSE-SEED")
                    seed = await wait_completion(1)
                    assert seed.status == "completed" and seed.error is None, seed
                    prior_usage = runtime.context_window_usage(binding.id)
                    assert prior_usage is not None and prior_usage.used_tokens == 30, prior_usage
                    response_submission = await submit("NATIVE-RESPONSE-INCOMPLETE")
                    assert runtime.context_window_usage(binding.id) == prior_usage
                    response_gate.set()
                    if automatic_continuation:
                        async with asyncio.timeout(15):
                            while len(provider.requests) < 3:
                                await asyncio.sleep(0.01)
                        active = runtime.active_turn(binding.id)
                        assert active is not None and active.turn_id == response_submission.turn_id, active
                        assert len(outcomes) == 1, "incomplete interrupted prematurely completed the Turn"
                        continuation_gate.set()
                    response_outcome = await wait_completion(2)
                    assert response_outcome.status == expected_status, response_outcome
                    if automatic_continuation:
                        assert response_outcome.error is None, response_outcome
                        assert response_outcome.final_response == "Continued within the original Turn.", response_outcome
                    else:
                        assert response_outcome.error is not None and response_outcome.final_response is None, response_outcome
                    response_request_count = len(provider.requests) - 1
                    assert response_request_count == (2 if automatic_continuation else 1), "unexpected model retry"
                    assert len(response_outcome.partial_answers) == 1, response_outcome.partial_answers
                    partial = response_outcome.partial_answers[0]
                    assert (partial.thread_id, partial.turn_id, partial.item_id, partial.text) == (
                        initial.thread_id, response_submission.turn_id, "stable-partial",
                        "A stable fragment before failure.",
                    ), partial
                    response_usage = runtime.context_window_usage(binding.id)
                    # Native emits the existing last snapshot for this Turn.
                    # Incomplete adds 70 to total except for content_filter;
                    # last stays 30. Never display failed 70 or cumulative 100.
                    assert response_usage is not None and response_usage.used_tokens == 30, response_usage
                    continued_submission = await submit("NATIVE-RESPONSE-CONTINUE")
                    continued = await wait_completion(3)
                    assert continued.status == "completed" and continued.error is None, continued
                    assert continued.final_response == "Continued in a subsequent Turn.", continued
                    assert continued.partial_answers == (), continued
                    submissions = (initial, response_submission, continued_submission)
                    assert {entry.thread_id for entry in submissions} == {initial.thread_id}
                    assert len({entry.turn_id for entry in submissions}) == 3
                    snapshot = (await AsyncThread(codex, initial.thread_id).read(include_turns=True)).thread
                    assert [turn.id for turn in snapshot.turns] == [entry.turn_id for entry in submissions]
                    assert [turn.status.value for turn in snapshot.turns] == ["completed", expected_status, "completed"]
                    persisted = snapshot.turns[1].model_dump(mode="json", by_alias=True)
                    assert persisted["itemsView"] == "full", persisted
                    fragments = [item for item in persisted["items"] if item["type"] == "agentMessage"]
                    assert len(fragments) == (2 if automatic_continuation else 1), fragments
                    assert fragments[0]["text"] == partial.text, fragments
                    assert fragments[0]["phase"] == "partial_answer", fragments
                    if automatic_continuation:
                        assert fragments[1]["text"] == response_outcome.final_response, fragments
                        assert fragments[1]["phase"] == "final_answer", fragments
                    assert not provider.errors and not provider.replies, (provider.errors, len(provider.replies))
                    assert len(provider.requests) == (4 if automatic_continuation else 3)
                    scenario = "interrupted_continuation" if automatic_continuation else "incomplete_" + (reason or "unknown")
                    return {
                        "scenario": scenario + ("_retries" if retries else ""), "passed": True,
                        "reason": reason, "production_runtime": True, "thread_id": initial.thread_id,
                        "turn_ids": [entry.turn_id for entry in submissions],
                        "statuses": [entry.status for entry in outcomes],
                        "partial_answers": [asdict(partial)],
                        "response_error": str(response_outcome.error) if response_outcome.error else None,
                        "context_before_response": asdict(prior_usage),
                        "context_after_response": asdict(response_usage),
                        "incomplete_response_reported_total_tokens": 70,
                        "request_max_retries": retries, "stream_max_retries": retries,
                        "same_turn_automatic_continuation": automatic_continuation,
                        "response_model_request_count": response_request_count,
                        "same_thread_continuation": True, "admission_released": True,
                        "model_request_count": len(provider.requests), "feishu_delivery_tested": False,
                    }
                finally:
                    response_gate.set()
                    continuation_gate.set()
                    try:
                        await runtime.interrupt_all()
                    finally:
                        await runtime.cancel_tasks()
        finally:
            store.close()


def permissions(response: dict[str, Any]) -> dict[str, Any]:
    return {key: response.get(key) for key in (
        "approvalPolicy", "approvalsReviewer", "sandbox", "activePermissionProfile",
    )}


def command_reply(name: str, command: str) -> tuple[int, bytes]:
    from probe_sdk_partial_answers import response_events

    tool = {"type": "response.output_item.done", "item": {
        "type": "function_call", "call_id": name, "name": "exec_command",
        "arguments": json.dumps({"cmd": command, "login": False, "max_output_tokens": 1000}),
    }}
    return 200, response_events(name, tool)


def tool_output(provider: Any, call_id: str) -> str:
    outputs = [item for item in provider.requests[-1]["body"]["input"]
               if item.get("type") == "function_call_output" and item.get("call_id") == call_id]
    assert len(outputs) == 1 and isinstance(outputs[0]["output"], str), outputs
    return outputs[0]["output"]


async def cold_permissions_case(root: Path) -> dict[str, Any]:
    from probe_sdk_native_boundaries import native_server, run_turn
    from probe_sdk_partial_answers import isolated_config, mock_provider

    read_marker, write_marker = "NATIVE-READ-ALLOWED", "NATIVE-WRITE-ALLOWED"
    denied_name, allowed_name = "denied.txt", "allowed.txt"
    denied_script = (
        f"from pathlib import Path; print(Path('input.txt').read_text(), flush=True); "
        f"Path({denied_name!r}).write_text('must-not-write')"
    )
    allowed_script = (
        f"from pathlib import Path; Path({allowed_name!r}).write_text({write_marker!r}); "
        f"print(Path({allowed_name!r}).read_text())"
    )
    replies = [
        final_reply("seed", "Persist the read-only session."),
        command_reply("denied-write", shlex.join([sys.executable, "-c", denied_script])),
        final_reply("denied-result", "Read-only command finished."),
        command_reply("allowed-write", shlex.join([sys.executable, "-c", allowed_script])),
        final_reply("allowed-result", "Workspace write command finished."),
    ]
    with mock_provider(replies) as provider:
        cwd, env = isolated_config(root, provider)
        (cwd / "input.txt").write_text(read_marker, encoding="utf-8")
        config = Path(env["CODEX_HOME"]) / "config.toml"
        config.write_text(
            'sandbox_mode = "workspace-write"\n' + config.read_text().replace(
                'approval_policy = "never"', 'approval_policy = "on-request"',
            ), encoding="utf-8",
        )
        async with native_server(cwd, env) as server:
            rpc = await server.connect()
            started = await rpc.request("thread/start", {
                "cwd": str(cwd), "approvalPolicy": "never", "approvalsReviewer": "user", "sandbox": "read-only",
            })
            thread_id = started["thread"]["id"]
            await run_turn(rpc, thread_id, "NATIVE-COLD-PERMISSION-SEED")
        async with native_server(cwd, env) as server:
            rpc = await server.connect()
            resumed = await rpc.request("thread/resume", {"threadId": thread_id, "excludeTurns": True})
            assert resumed["thread"]["id"] == thread_id, resumed
            assert resumed["sandbox"]["type"] == "readOnly", resumed
            assert resumed["approvalPolicy"] == "never", resumed
            await run_turn(rpc, thread_id, "NATIVE-COLD-READ-AND-DENIED-WRITE")
            output = tool_output(provider, "denied-write")
            assert read_marker in output, "read-only command did not execute its allowed read: " + output
            assert "PermissionError" in output, output
            assert not (cwd / denied_name).exists(), "read-only sandbox allowed a write"
        async with native_server(cwd, env) as server:
            rpc = await server.connect()
            overridden = await rpc.request("thread/resume", {
                "threadId": thread_id, "excludeTurns": True,
                "sandbox": "workspace-write", "approvalPolicy": "on-request",
            })
            assert overridden["thread"]["id"] == thread_id, overridden
            assert overridden["sandbox"]["type"] == "workspaceWrite", overridden
            assert overridden["approvalPolicy"] == "on-request", overridden
            await run_turn(rpc, thread_id, "NATIVE-EXPLICIT-OVERRIDE-WRITE")
            assert write_marker in tool_output(provider, "allowed-write")
            assert (cwd / allowed_name).read_text() == write_marker
        assert not provider.errors and not provider.replies and len(provider.requests) == 5, provider.errors
        return {
            "scenario": "cold_permissions", "passed": True, "thread_id": thread_id,
            "started": permissions(started), "cold_resumed": permissions(resumed),
            "explicit_override": permissions(overridden), "independent_app_servers": 3,
            "actual_read_allowed": True, "actual_read_only_write_denied": True,
            "actual_override_write_allowed": True, "model_request_count": len(provider.requests),
            "network_grants_tested": False, "managed_policy_tested": False,
        }


async def run_probe(selected: str) -> dict[str, Any]:
    cases = []
    with tempfile.TemporaryDirectory(prefix="netizen-response-recovery-") as temporary:
        for name in CASES if selected == "all" else (selected,):
            root = Path(temporary) / name
            root.mkdir()
            async with asyncio.timeout(60):
                if name == "cold_permissions":
                    cases.append(await cold_permissions_case(root))
                else:
                    reason = "interrupted" if name == "interrupted_continuation" else name.removeprefix("incomplete_").removesuffix("_retries")
                    cases.append(await incomplete_case(
                        root, None if reason == "unknown" else reason,
                        retries=2 if name.endswith("_retries") else 0,
                    ))
    return {
        "sdk_version": importlib.metadata.version("openai-codex"),
        "cli_version": importlib.metadata.version("openai-codex-cli-bin"),
        "transport": "bundled App Server + local mock Responses HTTP SSE",
        "real_model_used": False, "cases": cases,
        "not_tested": ["Responses WebSocket", "Feishu delivery", "network grants", "managed policy"],
    }


def _stop_worker(process: subprocess.Popen[Any]) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=5)


def _run_worker(case: str, output: Path | None) -> int:
    command = [sys.executable, "-B", str(Path(__file__).resolve()), "--worker", "--case", case]
    if output:
        command.extend(("--output", str(output.resolve())))
    timeout = 60 * len(CASES) + 40 if case == "all" else 75
    # Kill only this probe's newly created process group on a timeout or parent
    # interruption, including native servers if cooperative cleanup is stuck.
    # Avoid Popen.__exit__, which can wait without a deadline after an exception.
    process = subprocess.Popen(command, start_new_session=True)
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _stop_worker(process)
        print(f"FAIL: response recovery probe exceeded {timeout}s deadline.", file=sys.stderr)
        return 124
    except BaseException:
        with suppress(OSError, subprocess.TimeoutExpired):
            _stop_worker(process)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("all", *CASES), default="all")
    parser.add_argument("--output", type=Path, help="Optional JSON evidence file")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.worker:
        raise SystemExit(_run_worker(args.case, args.output))
    result = asyncio.run(run_probe(args.case))
    output = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    print(output, end="")


if __name__ == "__main__":
    main()
