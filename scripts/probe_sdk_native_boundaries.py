#!/usr/bin/env python3
"""Disposable native compaction and lifecycle probes; no real model or account.

Uses the installed bundled App Server over its public WebSocket JSON-RPC wire
protocol. This harness is not a production SDK adapter. Slow teardown locks only
the probe's temporary writer-coordination file, following the upstream test.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager, suppress
import fcntl
import importlib.metadata
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
from typing import Any

from probe_sdk_partial_answers import isolated_config, message_events, mock_provider, response_events


class RpcError(RuntimeError):
    def __init__(self, error: dict[str, Any]) -> None:
        self.error = error
        super().__init__(str(error))


class Rpc:
    def __init__(self, socket: Any) -> None:
        self.socket = socket
        self.sequence = 0
        self.pending: dict[int, asyncio.Future[Any]] = {}
        self.notifications: list[dict[str, Any]] = []
        self.condition = asyncio.Condition()
        self.reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        try:
            async for raw in self.socket:
                message = json.loads(raw)
                if "id" in message and "method" not in message:
                    future = self.pending.pop(message["id"], None)
                    if future is not None and not future.done():
                        if "error" in message:
                            future.set_exception(RpcError(message["error"]))
                        else:
                            future.set_result(message["result"])
                else:
                    async with self.condition:
                        self.notifications.append(message)
                        self.condition.notify_all()
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("probe connection closed"))

    async def request(self, method: str, params: dict[str, Any], timeout: float = 15) -> Any:
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        await self.socket.send(json.dumps({"id": request_id, "method": method, "params": params}))
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(request_id, None)

    async def wait(self, predicate: Any, *, after: int = 0, timeout: float = 15) -> dict[str, Any]:
        async with asyncio.timeout(timeout):
            async with self.condition:
                while True:
                    for item in self.notifications[after:]:
                        if predicate(item):
                            return item
                    await self.condition.wait()

    async def close(self) -> None:
        await self.socket.close()
        with suppress(Exception):
            await self.reader


class NativeServer:
    def __init__(self, process: asyncio.subprocess.Process, url: str) -> None:
        self.process = process
        self.url = url
        self.logs: list[str] = []
        self.condition = asyncio.Condition()
        self.log_reader = asyncio.create_task(self._read_logs())
        self.clients: list[Rpc] = []

    async def _read_logs(self) -> None:
        assert self.process.stderr is not None
        while line := await self.process.stderr.readline():
            async with self.condition:
                self.logs.append(line.decode(errors="replace"))
                self.condition.notify_all()

    async def wait_log(self, text: str, *, timeout: float) -> str:
        async with asyncio.timeout(timeout):
            async with self.condition:
                while True:
                    for line in self.logs:
                        if text in line:
                            return line
                    await self.condition.wait()

    async def connect(self) -> Rpc:
        from websockets.asyncio.client import connect

        async with asyncio.timeout(10):
            while True:
                if self.process.returncode is not None:
                    raise RuntimeError("App Server exited: " + "".join(self.logs))
                try:
                    websocket = await connect(self.url, proxy=None)
                    break
                except OSError:
                    await asyncio.sleep(0.05)
        client = Rpc(websocket)
        self.clients.append(client)
        await client.request("initialize", {
            "clientInfo": {"name": "netizen_native_probe", "version": "1"},
            "capabilities": {"experimentalApi": True},
        })
        await websocket.send(json.dumps({"method": "initialized", "params": {}}))
        return client


@asynccontextmanager
async def native_server(cwd: Path, env: dict[str, str]):
    from codex_cli_bin import bundled_codex_path

    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    url = f"ws://127.0.0.1:{port}"
    process = await asyncio.create_subprocess_exec(
        str(bundled_codex_path()), "app-server", "--listen", url,
        cwd=cwd, env={**os.environ, **env, "LOG_FORMAT": "json", "RUST_LOG": "warn", "TOKIO_WORKER_THREADS": "2"},
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    server = NativeServer(process, url)
    try:
        yield server
    finally:
        for client in server.clients:
            with suppress(Exception):
                await asyncio.wait_for(client.close(), 3)
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 4)
            except TimeoutError:
                process.kill()
                await process.wait()
        try:
            await asyncio.wait_for(server.log_reader, 3)
        except TimeoutError:
            server.log_reader.cancel()


def final_reply(response_id: str, text: str) -> tuple[int, bytes]:
    return 200, response_events(response_id, *message_events(response_id + "-message", text, "final_answer"))


async def start_thread(client: Rpc, cwd: Path, **extra: Any) -> str:
    result = await client.request("thread/start", {
        "cwd": str(cwd), "approvalPolicy": "never", "approvalsReviewer": "user",
        "sandbox": "read-only", **extra,
    })
    return result["thread"]["id"]


async def run_turn(client: Rpc, thread_id: str, text: str) -> dict[str, Any]:
    after = len(client.notifications)
    started = await client.request("turn/start", {
        "threadId": thread_id, "input": [{"type": "text", "text": text}],
    })
    turn_id = started["turn"]["id"]
    terminal = await client.wait(lambda event: (
        event.get("method") == "turn/completed" and event["params"]["turn"]["id"] == turn_id
    ), after=after)
    assert terminal["params"]["turn"]["status"] == "completed", terminal
    return terminal["params"]["turn"]


async def compaction_case(root: Path, *, fails: bool, remote: bool = False) -> dict[str, Any]:
    compact_reply = (
        (400, json.dumps({"error": {"type": "invalid_request_error", "message": "NATIVE-COMPACT-REJECT"}}).encode())
        if fails else final_reply("compact-summary", "NATIVE-COMPACT-SUMMARY: retained user context.")
    )
    if remote and not fails:
        compact_reply = (200, response_events("compact-summary", {
            "type": "response.output_item.done", "item": {
                "type": "compaction", "encrypted_content": "NATIVE-COMPACT-SUMMARY",
            },
        }))
    with mock_provider([final_reply("before", "NATIVE-SEED-ANSWER"), compact_reply, final_reply("after", "NATIVE-AFTER-COMPACT")]) as provider:
        cwd, env = isolated_config(root, provider)
        if remote:
            config = Path(env["CODEX_HOME"]) / "config.toml"
            config.write_text(config.read_text() + '\n[model_providers.native_probe.capabilities]\nremote_compaction = "v2"\n')
        async with native_server(cwd, env) as server:
            client = await server.connect()
            thread_id = await start_thread(client, cwd, baseInstructions="NATIVE-BASE-INSTRUCTION", developerInstructions="NATIVE-DEVELOPER-INSTRUCTION")
            first = await run_turn(client, thread_id, "NATIVE-USER-CONTEXT: keep this context.")
            after = len(client.notifications)
            ack = await client.request("thread/compact/start", {"threadId": thread_id})
            assert ack == {}, ack
            terminal = await client.wait(lambda event: (
                event.get("method") == "turn/completed" and event["params"]["threadId"] == thread_id
            ), after=after)
            compact = terminal["params"]["turn"]
            assert compact["id"] != first["id"], compact
            assert compact["status"] == ("failed" if fails else "completed"), compact
            third = await run_turn(client, thread_id, "NATIVE-NEXT-PROMPT")
            assert third["id"] not in {first["id"], compact["id"]}
            assert third.get("rootTurnId") == third["id"], third
            requests = provider.requests
            assert len(requests) == 3 and not provider.errors and not provider.replies, (len(requests), provider.errors)
            seed, request, subsequent = [entry["body"] for entry in requests]
            if remote:
                assert any(item.get("type") == "compaction_trigger" for item in request["input"]), request
            else:
                assert "NATIVE-COMPACT-PROBE" in json.dumps(request), request
            assert "NATIVE-USER-CONTEXT" in json.dumps(request), request
            assert "NATIVE-BASE-INSTRUCTION" in json.dumps(seed), "base instruction absent before compaction"
            assert "NATIVE-BASE-INSTRUCTION" in json.dumps(subsequent), "base instruction lost after compaction"
            assert "NATIVE-DEVELOPER-INSTRUCTION" in json.dumps(subsequent["input"]), subsequent
            assert str(cwd) in json.dumps(subsequent["input"]), "cwd context lost after compact"
            assert subsequent["tools"] == seed["tools"], "tool baseline changed after compact"
            expected_context = "NATIVE-USER-CONTEXT" if fails else "NATIVE-COMPACT-SUMMARY"
            assert expected_context in json.dumps(subsequent["input"]), subsequent
            snapshot = await client.request("thread/read", {"threadId": thread_id, "includeTurns": True})
            matching = [turn for turn in snapshot["thread"]["turns"] if turn["id"] == compact["id"]]
            assert len(matching) == 1 and matching[0]["status"] == compact["status"], snapshot
            events = client.notifications[after:]
            if fails:
                assert any(event.get("method") == "error" for event in events), events
            else:
                assert any(event.get("method") == "item/completed" and event["params"]["item"]["type"] == "contextCompaction" for event in events), events
            return {
                "scenario": ("remote_" if remote else "local_") + ("compaction_http_error" if fails else "compaction_context"),
                "passed": True, "thread_id": thread_id, "compact_turn_id": compact["id"],
                "compact_status": compact["status"], "ack": ack,
                "base_instructions_preserved": True, "developer_instructions_preserved": True,
                "cwd_context_preserved": True,
                "tool_baseline_preserved": True, "expected_context_present": expected_context,
                "next_turn_completed": True, "next_turn_self_root": True,
                "model_request_count": len(requests),
                "route": "custom provider remote compaction v2" if remote else "custom provider local compaction",
            }


async def idle_unload_case(root: Path) -> dict[str, Any]:
    with mock_provider([final_reply("seed", "NATIVE-IDLE-SEED")]) as provider:
        cwd, env = isolated_config(root, provider)
        config = Path(env["CODEX_HOME"]) / "config.toml"
        config.write_text("thread_unload_delay_secs = 0\n" + config.read_text())
        async with native_server(cwd, env) as server:
            client = await server.connect()
            thread_id = await start_thread(client, cwd)
            await run_turn(client, thread_id, "NATIVE-IDLE-SEED")
            coordination = Path(env["CODEX_HOME"]) / "thread-writer-locks/.coordination.lock"
            with coordination.open("r+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                began = time.monotonic()
                result = await client.request("thread/unsubscribe", {"threadId": thread_id})
                assert result == {"status": "unsubscribed"}, result
                await server.wait_log("codex.app_server.thread_shutdown_slow", timeout=22)
                elapsed = time.monotonic() - began
                assert elapsed >= 10, elapsed
                loaded = await client.request("thread/loaded/list", {})
                assert thread_id in loaded["data"], loaded
                try:
                    await client.request("thread/resume", {"threadId": thread_id})
                except RpcError as error:
                    closing = error.error
                else:
                    raise AssertionError("closing thread unexpectedly resumed")
                assert closing == {"code": -32600, "message": f"thread {thread_id} is closing; retry thread/resume after the thread is closed"}, closing
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            await client.wait(lambda event: event.get("method") == "thread/closed" and event["params"]["threadId"] == thread_id, timeout=12)
            loaded = await client.request("thread/loaded/list", {})
            assert thread_id not in loaded["data"], loaded
            resumed = await client.request("thread/resume", {"threadId": thread_id})
            assert resumed["thread"]["id"] == thread_id, resumed
            assert not provider.errors and not provider.replies, provider.errors
            return {"scenario": "idle_unload_over_ten_seconds", "passed": True,
                    "thread_id": thread_id, "shutdown_warning_seconds": round(elapsed, 3),
                    "loaded_during_closing": True, "closing_error": closing,
                    "closed_after_lock_release": True, "resumed_after_closed": True}


async def compaction_preparation_error_case(root: Path) -> dict[str, Any]:
    """A valid dynamic tool collides only when the native step is prepared."""
    with mock_provider([]) as provider:
        cwd, env = isolated_config(root, provider)
        config = Path(env["CODEX_HOME"]) / "config.toml"
        config.write_text(config.read_text() + '\n[features.tool_registry]\nerror_on_tool_collisions = true\n')
        async with native_server(cwd, env) as server:
            client = await server.connect()
            thread_id = await start_thread(client, cwd, dynamicTools=[{
                "type": "function", "name": "exec_command",
                "description": "Deliberate disposable collision for preparation failure.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            }])
            after = len(client.notifications)
            ack = await client.request("thread/compact/start", {"threadId": thread_id})
            assert ack == {}, ack
            terminal = await client.wait(lambda event: (
                event.get("method") == "turn/completed" and event["params"]["threadId"] == thread_id
            ), after=after)
            turn = terminal["params"]["turn"]
            assert turn["status"] == "failed", turn
            events = client.notifications[after:]
            started = [event for event in events if event.get("method") == "turn/started"]
            errors = [event for event in events if event.get("method") == "error"]
            assert len(started) == 1 and started[0]["params"]["turn"]["id"] == turn["id"], events
            assert len(errors) == 1, errors
            assert errors[0]["params"]["error"]["codexErrorInfo"] == "other", errors
            assert errors[0]["params"]["error"]["message"] == "duplicate tool: functions.exec_command", errors
            assert errors[0]["params"]["willRetry"] is False, errors
            assert errors[0]["params"]["threadId"] == thread_id, errors
            assert errors[0]["params"]["turnId"] == turn["id"], errors
            await server.wait_log("skipping external tool with reserved name", timeout=3)
            collision_logs = [json.loads(line) for line in server.logs if "skipping external tool with reserved name" in line]
            assert any(log["fields"]["tool_name"] == "exec_command" for log in collision_logs), collision_logs
            assert not any("is ignored" in line for line in server.logs), server.logs
            methods = [event.get("method") for event in events]
            assert methods.index("turn/started") < methods.index("error") < methods.index("turn/completed"), methods
            assert not provider.requests and not provider.errors, provider.requests
            snapshot = await client.request("thread/read", {"threadId": thread_id, "includeTurns": True})
            matching = [item for item in snapshot["thread"]["turns"] if item["id"] == turn["id"]]
            assert len(matching) == 1 and matching[0]["status"] == "failed", snapshot
            return {"scenario": "compaction_preparation_tool_collision", "passed": True,
                    "thread_id": thread_id, "compact_turn_id": turn["id"], "ack": ack,
                    "turn_status": "failed", "model_request_count": 0,
                    "started_before_error_and_terminal": True,
                    "error": errors[0]["params"]["error"], "public_read_failed": True,
                    "reserved_tool_collision_logged": "exec_command"}


def mcp_server(directory: Path, *, large_result: bool = False) -> None:
    (directory / "mcp.pid").write_text(str(os.getpid()))
    for line in sys.stdin:
        request = json.loads(line)
        method = request.get("method")
        if method == "initialize":
            (directory / "initialize-seen").touch()
            deadline = time.monotonic() + 60
            while not large_result and not (directory / "allow-initialize").exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("probe MCP barrier was not released")
                time.sleep(0.05)
            result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "netizen_probe_mcp", "version": "1"}}
        elif method == "tools/list":
            (directory / "tools-listed").touch()
            result = {"tools": [{
                "name": "large_result", "description": "Return fixed local probe data.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
                "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
            }] if large_result else []}
        elif method == "tools/call" and large_result:
            assert request["params"]["name"] == "large_result", request
            (directory / "tool-called").touch()
            result = {
                "content": [{"type": "text", "text": "NATIVE-MCP-HEAD\n" + "X" * 200000 + "\nNATIVE-MCP-TAIL"}],
                "structuredContent": {"probe": "native-large-result"}, "isError": False,
            }
        elif "id" in request:
            result = {}
        else:
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)


async def wait_condition(predicate: Any, timeout: float) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.05)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def slow_mcp_case(root: Path) -> dict[str, Any]:
    with mock_provider([]) as provider:
        cwd, env = isolated_config(root, provider)
        config = Path(env["CODEX_HOME"]) / "config.toml"
        config.write_text("thread_unload_delay_secs = 1\n" + config.read_text() +
            '\n[mcp_servers.blocked]\nrequired = true\nstartup_timeout_sec = 65\n'
            f'command = {json.dumps(sys.executable)}\n'
            f'args = {json.dumps([str(Path(__file__).resolve()), "--mcp-server", str(root)])}\n')
        async with native_server(cwd, env) as server:
            owner = await server.connect()
            observer = await server.connect()
            starting = asyncio.create_task(start_thread(owner, cwd, ephemeral=True))
            try:
                await wait_condition(lambda: (root / "initialize-seen").exists(), 12)
                pid = int((root / "mcp.pid").read_text())
                assert pid_alive(pid)
                began = time.monotonic()
                await owner.close()
                with suppress(ConnectionError):
                    await starting
                await server.wait_log("timed out waiting for connection RPCs to drain", timeout=38)
                elapsed = time.monotonic() - began
                assert elapsed >= 30, elapsed
                (root / "allow-initialize").touch()
                closed = await observer.wait(lambda event: event.get("method") == "thread/closed", timeout=15)
                thread_id = closed["params"]["threadId"]
                loaded = await observer.request("thread/loaded/list", {})
                assert thread_id not in loaded["data"], loaded
                await wait_condition(lambda: not pid_alive(pid), 8)
                assert not provider.requests and not provider.errors, provider.requests
                return {"scenario": "slow_mcp_disconnect", "passed": True,
                        "thread_id": thread_id, "connection_drain_seconds": round(elapsed, 3),
                        "startup_completed_after_disconnect": True, "thread_closed": True,
                        "absent_from_loaded": True, "owned_mcp_exited": True, "model_request_count": 0}
            finally:
                (root / "allow-initialize").touch()
                if not starting.done():
                    starting.cancel()
                with suppress(asyncio.CancelledError, ConnectionError, RpcError):
                    await starting


async def run_probe(selected: str) -> dict[str, Any]:
    cases = []
    names = [selected] if selected != "all" else ["compact", "compact-error", "compact-remote", "compact-remote-error", "compact-prepare-error", "idle", "mcp"]
    with tempfile.TemporaryDirectory(prefix="netizen-native-boundary-") as directory:
        for name in names:
            root = Path(directory) / name
            root.mkdir()
            async with asyncio.timeout(90):
                if name == "compact-prepare-error":
                    result = await compaction_preparation_error_case(root)
                elif name.startswith("compact"):
                    result = await compaction_case(root, fails=name.endswith("error"), remote="remote" in name)
                elif name == "idle":
                    result = await idle_unload_case(root)
                else:
                    result = await slow_mcp_case(root)
                cases.append(result)
                print(f"PASS {name}", file=sys.stderr, flush=True)
    return {"sdk_version": importlib.metadata.version("openai-codex"),
            "transport": "bundled App Server WebSocket + local mock provider",
            "real_model_used": False, "cases": cases}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("all", "compact", "compact-error", "compact-remote", "compact-remote-error", "compact-prepare-error", "idle", "mcp"), default="all")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--mcp-server", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--mcp-large-result", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.mcp_server:
        mcp_server(args.mcp_server, large_result=args.mcp_large_result)
        return
    result = json.dumps(asyncio.run(run_probe(args.case)), indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(result, encoding="utf-8")
    print(result, end="")


if __name__ == "__main__":
    main()
