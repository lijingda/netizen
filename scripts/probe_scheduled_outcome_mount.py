#!/usr/bin/env python3
"""Check scheduled topic MCP isolation with the bundled App Server and a loopback mock provider."""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import re
import subprocess
import sys
import tempfile
import threading
from contextlib import AsyncExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.probe_sdk_partial_answers import isolated_config, message_events, response_events
from netizen_cli.mcp_transport import native_subagent_kind, native_thread_id, native_turn_id
from netizen_cli.schedules.mcp import ScheduleMcpRunner
from netizen_cli.schedules.outcome_mcp import OutcomeMcpRunner, TOOL
from netizen_cli.sessions.mcp import SessionMcpRunner
from netizen_cli.sdk_gap_adapter import AppServerThreadSubscriptionControl

MARK = re.compile(r"PROBE\[([a-z0-9_-]+):(call|final|spawn)\]")
CHILD = re.compile(r"CHILD-TASK\[([a-z0-9_-]+)\]")


class ObservedOutcomeRunner(OutcomeMcpRunner):
    """Capture native caller classification and the production handler's decision."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = []

    async def _call_tool(self, ctx, params):
        result = await super()._call_tool(ctx, params)
        self.calls.append((native_thread_id(ctx.meta), native_turn_id(ctx.meta),
                           native_subagent_kind(ctx.meta), result.structured_content))
        return result


def advertised_tools(tools: list[dict[str, Any]]) -> set[tuple[str, str]]:
    found = set()
    for tool in tools:
        if tool.get("type") == "namespace":
            found.update((tool["name"], nested["name"]) for nested in tool.get("tools", []))
        elif tool.get("type") == "function":
            found.add(("", tool["name"]))
    return found


def _texts(item: dict[str, Any]) -> str:
    if item.get("type") != "message":
        return ""
    return " ".join(part.get("text", "") for part in item.get("content", []) if isinstance(part, dict))


def _final(tag: str, idx: int) -> bytes:
    return response_events(f"r-{idx}", *message_events(f"m-{idx}", tag, "final_answer"))


def _call(idx: int, call_id: str, namespace: str, name: str, arguments: dict[str, Any]) -> bytes:
    return response_events(f"r-{idx}", {"type": "response.output_item.done", "item": {
        "type": "function_call", "call_id": call_id, "name": name,
        "namespace": namespace, "arguments": json.dumps(arguments),
    }})


class Router:
    """Choose each mock reply from request content so concurrent agents cannot cross."""

    def __init__(self) -> None:
        self.process_namespaces: dict[str, tuple[str, str]] = {}
        self.log: list[dict[str, Any]] = []

    def __call__(self, body: dict[str, Any], idx: int) -> bytes:
        items = body.get("input", [])
        tools = advertised_tools(body.get("tools", []))
        users = [_texts(item) for item in items if item.get("type") == "message" and item.get("role") == "user"]
        child = next((m.group(1) for text in users for m in CHILD.finditer(text)), None)
        marks = [m for text in users for m in MARK.finditer(text)]
        label, action = (marks[-1].group(1), marks[-1].group(2)) if marks else (None, None)
        role = "child" if child else "parent"
        label = f"{child}-child" if child else label
        mounted = sorted(ns for ns, name in tools if name == TOOL)
        entry: dict[str, Any] = {
            "idx": idx, "role": role, "label": label, "action": action,
            "outcome_tool_namespaces": mounted,
            "process_tools": {key: (ns, tool) in tools for key, (ns, tool) in self.process_namespaces.items()},
        }
        self.log.append(entry)
        last = items[-1] if items else {}
        outputs = [item.get("call_id", "") for item in items if item.get("type") == "function_call_output"]
        if (action == "spawn" and role == "parent" and last.get("type") != "function_call_output"
                and any(call_id.startswith(f"wait-{label}-") for call_id in outputs)):
            entry["reply"] = "final-after-wait"
            return _final(f"DONE[{label}]", idx)
        if last.get("type") == "function_call_output":
            call_id = last.get("call_id", "")
            output = last.get("output")
            text = output if isinstance(output, str) else json.dumps(output)
            if call_id.startswith("spawn-"):
                found = re.search(r'agent_id\\?"?\s*[:=]\s*\\?"?([0-9A-Za-z_-]{8,})', text) or \
                    re.search(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', text)
                if found:
                    entry["reply"] = "wait_agent"
                    return _call(idx, f"wait-{label}-{idx}", "multi_agent_v1", "wait_agent",
                                 {"targets": [found.group(1)], "timeout_ms": 30000})
            entry["reply"] = "final"
            return _final(f"DONE[{label}]", idx)
        if label is None:
            entry["reply"] = "unrouted-final"
            return _final("UNROUTED", idx)
        if role == "child" or action == "call":
            if mounted:
                entry["reply"] = "outcome_call"
                return _call(idx, f"outcome-{label}-{idx}", mounted[0], TOOL, {"outcome": "succeeded"})
            entry["reply"] = "notool-final"
            return _final(f"NOTOOL[{label}]", idx)
        if action == "spawn":
            entry["reply"] = "spawn_agent"
            return _call(idx, f"spawn-{label}-{idx}", "multi_agent_v1", "spawn_agent",
                         {"message": f"CHILD-TASK[{label}] report the outcome if you can.",
                          "fork_context": label == "delegated-full"})
        entry["reply"] = "final"
        return _final(f"DONE[{label}]", idx)


class RoutingServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, router: Router) -> None:
        super().__init__(("127.0.0.1", 0), RoutingHandler)
        self.router = router
        self.errors: list[str] = []
        self.lock = threading.Lock()


class RoutingHandler(BaseHTTPRequestHandler):
    server: RoutingServer

    def log_message(self, *_args: object) -> None:
        pass

    def do_POST(self) -> None:
        if self.path != "/v1/responses" or self.headers.get("Authorization"):
            self.server.errors.append("unexpected provider route or credentials")
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        with self.server.lock:
            idx = len(self.server.router.log)
            try:
                reply = self.server.router(body, idx)
            except Exception as error:
                self.server.errors.append(f"route error at {idx}: {error!r}")
                reply = _final("ROUTE-ERROR", idx)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        try:
            self.wfile.write(reply)
        except (BrokenPipeError, ConnectionResetError):
            self.server.errors.append("model response connection closed early")


async def run_turn(thread: Any, prompt: str, *, timeout: float = 60) -> dict[str, Any]:
    async with asyncio.timeout(timeout):
        handle = await thread.turn(prompt)
        result = await handle.run()
    assert result.status.value == "completed", (prompt, result.status.value)
    return {"turn_id": handle.id, "status": result.status.value, "final": result.final_response}


def entries_for(router: Router, label: str) -> list[dict[str, Any]]:
    return [entry for entry in router.log if entry["label"] == label]


CASES = ("isolation", "resume", "fork", "subagent", "unreachable")


async def load_state(thread):
    snapshot = (await thread.read(include_turns=False)).model_dump(mode="json", by_alias=True)
    return snapshot["thread"]["status"]["type"]


async def unload(codex, thread):
    await AppServerThreadSubscriptionControl(codex).unsubscribe(thread.id)
    async with asyncio.timeout(20):
        while await load_state(thread) != "notLoaded":
            await asyncio.sleep(0.1)


async def probe(case: str) -> dict:
    from codex_cli_bin import bundled_codex_path
    from openai_codex import AsyncCodex, CodexConfig

    selected = CASES if case == "all" else (case,)
    router = Router()
    session, schedule = SessionMcpRunner(), ScheduleMcpRunner()
    outcome, rotated = ObservedOutcomeRunner(), ObservedOutcomeRunner()
    router.process_namespaces = {
        "session": ("mcp__" + session.namespace, "suppress_reply"),
        "schedule": ("mcp__" + schedule.namespace, "cron_manage"),
        "user": ("mcp__user_existing", "cron_manage"),
    }

    async def acknowledge(**_kwargs):
        return {"recorded": True}

    async def check(thread, label, expected, *, process=True):
        result = await run_turn(thread, f"PROBE[{label}:call]")
        entries = entries_for(router, label)
        assert entries, label
        for entry in entries:
            assert entry["outcome_tool_namespaces"] == (["mcp__" + expected.namespace] if expected else []), label
            assert entry["process_tools"] == {"session": process, "schedule": process, "user": True}, entry
        if expected:
            assert (thread.id, result["turn_id"], None, {"recorded": True}) in expected.calls, label
        return result

    with tempfile.TemporaryDirectory(prefix="netizen-outcome-") as tmp:
        async with AsyncExitStack() as stack:
            server = RoutingServer(router)
            stack.callback(server.server_close)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            stack.callback(worker.join, 3)
            stack.callback(server.shutdown)
            cwd, env = isolated_config(Path(tmp), server)
            for runner in (session, schedule, outcome, rotated):
                await runner.bind()
                stack.push_async_callback(runner.close)
                runner.attach(acknowledge)
                runner.open_admission()
            path = Path(env["CODEX_HOME"]) / "config.toml"
            user_entry = schedule.config_overrides[0].split("=", 1)[1]
            path.write_text(f"thread_unload_delay_secs = 1\nmcp_servers.user_existing={user_entry}\n" + path.read_text())
            original = path.read_bytes()

            def config(runner):
                return CodexConfig(cwd=str(cwd), env={**env, **session.app_server_env,
                    **schedule.app_server_env, **rotated.app_server_env, **runner.app_server_env},
                    config_overrides=(*session.config_overrides, *schedule.config_overrides))

            async with AsyncCodex(config(outcome)) as codex:
                ordinary = await codex.thread_start(cwd=str(cwd))
                await check(ordinary, "ordinary-before", None)
                mounted = await codex.thread_start(cwd=str(cwd), config=outcome.thread_config())
                await check(mounted, "mounted", outcome)
                if "isolation" in selected:
                    # Deliberately reproduce the unsupported whole-table shape.
                    entry, = outcome.thread_config().values()
                    nested = await codex.thread_start(cwd=str(cwd), config={"mcp_servers": {outcome.namespace: entry}})
                    await check(nested, "nested", outcome, process=False)
                    await check(await codex.thread_start(cwd=str(cwd)), "ordinary-after", None)
                if "resume" in selected:
                    await check(mounted, "second", outcome)
                    warm = await codex.thread_resume(mounted.id, include_turns=False)
                    await check(warm, "warm", outcome)
                    other = await codex.thread_resume(mounted.id, config=rotated.thread_config())
                    await check(other, "warm-replace", outcome)
                    ordinary = await codex.thread_resume(ordinary.id, config=outcome.thread_config())
                    await check(ordinary, "warm-add", None)
                    await unload(codex, mounted)
                    await check(await codex.thread_resume(mounted.id), "cold-none", None)
                    await unload(codex, mounted)
                    mounted = await codex.thread_resume(mounted.id, config=outcome.thread_config())
                    await check(mounted, "cold-mounted", outcome)
                if "fork" in selected:
                    for kind in ("persistent", "ephemeral"):
                        warm_fork = await codex.thread_fork(mounted.id, ephemeral=kind == "ephemeral", include_turns=False)
                        await check(warm_fork, f"warm-{kind}", None)
                        await unload(codex, mounted)
                        fork = await codex.thread_fork(mounted.id, ephemeral=kind == "ephemeral", include_turns=False)
                        assert await load_state(mounted) == "notLoaded", kind
                        await check(fork, kind, None)
                        mounted = await codex.thread_resume(mounted.id, config=outcome.thread_config())
                        await check(mounted, f"after-{kind}", outcome)
                if "subagent" in selected:
                    for label in ("delegated-fresh", "delegated-full"):
                        parent = await codex.thread_start(cwd=str(cwd), config=outcome.thread_config())
                        before = len(outcome.calls)
                        turn = await run_turn(parent, f"PROBE[{label}:spawn]", timeout=90)
                        async with asyncio.timeout(20):
                            while not any(entry.get("reply") == "final" for entry in entries_for(router, f"{label}-child")):
                                await asyncio.sleep(0.1)
                        assert any(thread_id and thread_id != parent.id and turn_id and turn_id != turn["turn_id"]
                                   and kind == "thread_spawn" and value.get("error", {}).get("code") == "invalid_call_context"
                                   for thread_id, turn_id, kind, value in outcome.calls[before:]), label
                if "unreachable" in selected:
                    # Use an actual closed production endpoint.
                    await rotated.close()
                    dead = await codex.thread_start(cwd=str(cwd), config=rotated.thread_config())
                    await check(dead, "unreachable", None)
            if "resume" in selected:
                # Rotate the bearer and namespace with the next App Server.
                restart = ObservedOutcomeRunner()
                await restart.bind()
                stack.push_async_callback(restart.close)
                restart.attach(acknowledge)
                restart.open_admission()
                async with AsyncCodex(config(restart)) as codex:
                    resumed = await codex.thread_resume(mounted.id, config=restart.thread_config())
                    await check(resumed, "restart", restart)
            assert path.read_bytes() == original
            assert not server.errors, server.errors
    return {"openai_codex": importlib.metadata.version("openai-codex"),
            "bundled_app_server": subprocess.check_output([str(bundled_codex_path()), "--version"], text=True).strip(),
            "cases": [{"name": name, "passed": True} for name in selected], "config_unchanged": True, "requests": len(router.log)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("all", *CASES), default="all")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    evidence = json.dumps(asyncio.run(probe(args.case)), indent=2) + "\n"
    if args.output:
        args.output.write_text(evidence, encoding="utf-8")
    print(evidence, end="")


if __name__ == "__main__":
    main()
