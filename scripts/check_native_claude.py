"""Exercise Netizen's Claude port using the bundled CLI and a disposable loopback provider."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def fixture_environment(home: Path) -> dict[str, str]:
    return {
        "HOME": str(home), "CLAUDE_CONFIG_DIR": str(home / ".claude"), "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8", "ANTHROPIC_API_KEY": "synthetic-fixture", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1", "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_DISABLE_FEATURE_FLAGS": "1", "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9", "NO_PROXY": "127.0.0.1,localhost",
        # Every child initially points at a refusing address until the worker opens its fixture.
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:9", "CLAUDE_CODE_MAX_RETRIES": "2",
    }


@dataclass
class Evidence:
    names: list[str] = field(default_factory=list)

    def check(self, name, condition):
        if not condition:
            raise AssertionError(name)
        self.names.append(name)
        print("PASS", name, flush=True)


class ObservedClient:
    """Count application writes and classify public SDK events without saving their content."""

    def __init__(self, client, clients):
        self.client, self.clients = client, clients
        self.ids = []
        self.modes = []
        self.results = []
        self.closed = False

    async def connect(self):
        await self.client.connect()

    async def disconnect(self):
        await self.client.disconnect()
        self.closed = True

    async def interrupt(self):
        await self.client.interrupt()

    async def query(self, prompt):
        async def observe_input():
            async for message in prompt:
                self.ids.append(message["uuid"])
                yield message
        await self.client.query(observe_input())
        if self.clients.drop_receipt:
            # The real CLI has issued the HTTP request. Drop only Netizen's write receipt.
            await self.clients.provider.wait_request("lost")
            raise OSError("synthetic lost submission receipt")

    async def receive_messages(self):
        from claude_agent_sdk import ResultMessage, SystemMessage

        async for msg in self.client.receive_messages():
            if isinstance(msg, SystemMessage) and msg.subtype == "init":
                self.modes.append((msg.data.get("permissionMode"), "AskUserQuestion" in msg.data.get("tools", [])))
            if isinstance(msg, ResultMessage):
                self.results.append((msg.subtype, msg.terminal_reason, msg.is_error,
                                     msg.user_message_uuid, tuple(msg.user_message_uuids or ())))
            yield msg


class ObservedClients:
    def __init__(self, provider):
        from netizen_cli.runtime.claude_client import BundledClaudeClients

        self.bundled = BundledClaudeClients()
        self.provider = provider
        self.calls = []
        self.drop_receipt = False

    def create(self, session_id, cwd, *, resume):
        client = ObservedClient(self.bundled.create(session_id, cwd, resume=resume), self)
        self.calls.append(client)
        return client

    async def session_cwd(self, session_id, candidates=()):
        return await self.bundled.session_cwd(session_id, candidates)


async def main_cases(cwd, provider, evidence):
    import psutil
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage
    from netizen_cli.bindings import BindingStore
    from netizen_cli.claude_installation import verified_claude_cli
    from netizen_cli.domain import FeishuScope, ScopeKind
    from netizen_cli.ordinary_sessions import OrdinaryInputRejected, PreparedPrompt, PromptImage
    from netizen_cli.runtime.claude_client import single_message, user_message
    from netizen_cli.runtime.claude_ordinary import ClaudeOrdinarySessions, DEFAULT_MODE
    from netizen_cli.runtime.contracts import ThreadActivityChanged

    cli = verified_claude_cli().resolve()
    store = BindingStore()
    store.bootstrap_project(alias="work", cwd=str(cwd))
    def new_binding():
        return store.create_channel_binding(
            scope=FeishuScope("fixture", "chat-" + uuid4().hex, ScopeKind.DIRECT),
            project_alias="work", creator_id="owner")
    clients = ObservedClients(provider)
    completions = asyncio.Queue()
    notices = []
    async def notice(origin, text):
        notices.append(text)
    clock = [0.]
    runtime = ClaudeOrdinarySessions(bindings=store, clients=clients, owner_id="owner",
                                     on_completion=completions.put, notice=notice, clock=lambda: clock[0])
    runtime.open_admission()
    clock[0] = 3.
    def image_first_input():
        # Real, deterministic PNG large enough to span the SDK summary window.
        import base64
        import random
        import struct
        import zlib
        def chunk(tag, data):
            return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))
        rng = random.Random(104)
        pixels = b"".join(b"\0" + rng.randbytes(256) for _ in range(256))
        header = struct.pack(">2I5B", 256, 256, 8, 0, 0, 0, 0)
        png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b"")
        return PromptImage("fixture image", "data:image/png;base64," + base64.b64encode(png).decode("ascii"))

    def native_settings(fallback=False):
        # Only the exact benign fixture commands are pre-approved by this test account.
        values = {"model": "claude-sonnet-4-6", "permissions": {"allow": ["Bash(exit 17)", "Bash(./wait-for-fixture)"]}}
        if fallback:
            values["disableAutoMode"] = "disable"
        (Path(os.environ["CLAUDE_CONFIG_DIR"]) / "settings.json").write_text(json.dumps(values))
    async def submit(binding, scenario, label, images=()):
        current = store.get(binding.id)
        admission = await runtime.capture_submission_admission(binding.id)
        result = await runtime.submit(binding=current, cwd=None if current.native_thread_id else cwd,
            input=PreparedPrompt(f"[[S:{scenario}/{label}]]", images), owner_id="owner",
            origin=label, admission=admission, context_commit=None, skill_names=())
        result.release_receipt_attempt()
        return result
    async def done(label):
        event = await asyncio.wait_for(completions.get(), 45)
        if event.origin != label:
            raise AssertionError("different execution produced the Result")
        if not await runtime.wait_idle(20):
            raise AssertionError("client cleanup deadline")
        return event
    def rows(label):
        return [row for row in provider.rows if row["path"] == "/v1/messages" and row["label"] == label]
    async def native_cli(session):
        child = await asyncio.create_subprocess_exec(str(cli), "-p", "--resume", session, "--permission-mode", "auto",
            "--output-format", "json", "[[S:text/cli]]", cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            output, _ = await asyncio.wait_for(child.communicate(), 35)
            return json.loads(output) if child.returncode == 0 else {}
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()

    try:
        native_settings()
        first = new_binding()
        pixel = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO5WcGQAAAAASUVORK5CYII="
        receipt = await submit(first, "text", "first", (PromptImage("fixture image", "data:image/png;base64," + pixel),))
        event = await done("first")
        observed = clients.calls[-1]
        evidence.check("typed auto init; no host question", observed.modes == [("auto", False)])
        evidence.check("one input and attributed success", observed.ids == [receipt.turn_id] and
                       observed.results[0][:3] == ("success", "completed", False) and
                       (observed.results[0][3] == receipt.turn_id or receipt.turn_id in observed.results[0][4])
                       and event.final_response == "fixture-reply-first")
        first_rows = rows("first")
        evidence.check("native setting and ordered image input", first_rows and
                       first_rows[-1]["model"] == "claude-sonnet-4-6" and
                       any("image" in msg["blocks"] and msg["blocks"].index("image") < len(msg["blocks"])-1
                           for msg in first_rows[-1]["history"]))
        session = store.get(first.id).native_thread_id
        evidence.check("new UUID and native cwd", session == receipt.thread_id and await clients.session_cwd(session) == cwd)
        shell = await native_cli(session)
        evidence.check("CLI resumes SDK transcript", shell.get("session_id") == session and shell.get("result") == "fixture-reply-cli"
                       and any("fixture-reply-first" in item["answers"] for row in rows("cli") for item in row["history"]))
        native_settings(fallback=True)
        resumed = await submit(first, "text", "resumed")
        await done("resumed")
        evidence.check("SDK resumes CLI transcript and reports strict fallback", resumed.thread_id == session and
                       clients.calls[-1].modes == [("default", False)] and notices.count(DEFAULT_MODE) == 1 and
                       any("fixture-reply-cli" in item["answers"] for row in rows("resumed") for item in row["history"]))

        image_session = new_binding()
        with_image = await submit(image_session, "text", "image-first", (image_first_input(),))
        await done("image-first")
        resumed_image = await submit(image_session, "text", "image-resume")
        await done("image-resume")
        evidence.check("large image first message resumes exact native transcript",
                       resumed_image.thread_id == with_image.thread_id and
                       any("fixture-reply-image-first" in item["answers"] for row in rows("image-resume") for item in row["history"]))

        missing = new_binding()
        store.assign_native_thread_id(missing.id, str(uuid4()))
        count = len(clients.calls)
        try:
            await submit(missing, "text", "missing")
        except OrdinaryInputRejected:
            pass
        evidence.check("missing exact resume opens no replacement", len(clients.calls) == count and not rows("missing"))

        native_settings()
        for scenario, label, status in (("tool-error", "tool", "completed"), ("http-529", "api", "failed"),
                                        ("disconnect-mid-stream", "fallback", "completed")):
            target = new_binding()
            original = await submit(target, scenario, label)
            outcome = await done(label)
            evidence.check(label + " exact terminal", outcome.status == status and len(clients.calls[-1].ids) == 1 and rows(label))
            if label == "api":
                evidence.check("native retries one application input", len(rows(label)) > 1
                               and clients.calls[-1].results[0][:3] == ("success", "api_error", True))
                next_input = await submit(target, "text", "aftererror")
                followup = await done("aftererror")
                evidence.check("first request failure retains exact resumable UUID",
                               next_input.thread_id == original.thread_id and followup.status == "completed")
            if label == "tool":
                evidence.check("tool failure returned to provider", any(r["tool_error"] for r in rows(label)))
            if label == "fallback":
                evidence.check("native retry uses nonstreaming fallback", any(not r["stream"] for r in rows(label)))

        limited_id = str(uuid4())
        limit = ClaudeSDKClient(options=ClaudeAgentOptions(cli_path=cli, cwd=cwd, session_id=limited_id,
                                                           permission_mode="auto", max_turns=1, verbatim_prompts=True))
        try:
            await limit.connect()
            await limit.query(single_message(user_message(PreparedPrompt("[[S:tool-error/limit]]"), str(uuid4()))))
            async for msg in limit.receive_messages():
                if isinstance(msg, ResultMessage):
                    evidence.check("native max-turn terminal", (msg.subtype, msg.terminal_reason, msg.is_error)
                                   == ("error_max_turns", "max_turns", True))
                    break
        finally:
            await limit.disconnect()

        for scenario, label, phase in (("hold-stream", "textstop", "streaming"), ("tool-barrier", "toolstop", "starts")):
            current = new_binding()
            await submit(current, scenario, label)
            async with asyncio.timeout(25):
                while not (cwd / (phase + "-" + label)).exists():
                    await asyncio.sleep(.05)
            preview = await runtime.preview_stop(current)
            try:
                await submit(current, "text", "busy")
            except OrdinaryInputRejected:
                pass
            evidence.check(label + " rejects busy", not rows("busy") and preview and preview.turn_id)
            await runtime.stop_exact(current.id, expected_activity_revision=preview.activity_revision, expected_turn_id=preview.turn_id)
            provider.release(label)
            stopped = await done(label)
            evidence.check(label + " native interrupt", stopped.status == "interrupted")
            await submit(current, "hold-stream", "successor")
            try:
                await runtime.stop_exact(current.id, expected_activity_revision=preview.activity_revision, expected_turn_id=preview.turn_id)
            except ThreadActivityChanged:
                pass
            else:
                raise AssertionError("stale stop accepted")
            provider.release("successor")
            evidence.check(label + " successor unaffected", (await done("successor")).status == "completed")
            (cwd / "release-successor").unlink()

        lost = new_binding()
        clients.drop_receipt = True
        await submit(lost, "text", "lost")
        reconciled = await done("lost")
        evidence.check("lost write receipt reconciled on the same stream", reconciled.final_response == "fixture-reply-lost")
        clients.drop_receipt = False
        evidence.check("no application replay", len(clients.calls[-1].ids) == 1 and len(rows("lost")) == 1)
        pair = [new_binding(), new_binding()]
        await asyncio.gather(*(submit(binding, "hold-stream", f"parallel{i}") for i,binding in enumerate(pair)))
        await asyncio.gather(*(provider.wait_request(f"parallel{i}") for i in range(2)))
        pids = []
        for process in psutil.Process().children(recursive=True):
            try:
                if Path(process.exe()).resolve() == cli:
                    pids.append(process)
            except psutil.NoSuchProcess:
                # A completed tool can race enumeration (including a transient zombie).
                continue
        evidence.check("two same-cwd sessions own distinct CLIs", len(pids) >= 2 and
                       store.get(pair[0].id).native_thread_id != store.get(pair[1].id).native_thread_id)
        runtime.close_admission()
        async with asyncio.timeout(30):
            await runtime.interrupt_all()
            for i in range(2):
                provider.release(f"parallel{i}")
            await runtime.cancel_tasks()
            while any(p.is_running() for p in pids):
                await asyncio.sleep(.05)
        evidence.check("bounded shutdown releases owned CLIs", await runtime.wait_idle(1) and all(c.closed for c in clients.calls))
    finally:
        await runtime.cancel_tasks()
        store.close()


async def worker():
    from tests.support.claude_provider import Provider

    home = Path(os.environ["HOME"])
    cwd = Path.cwd()
    expected = fixture_environment(home)
    if (cwd.parent != home or any(os.environ.get(k) != v for k, v in expected.items())
            or set(os.environ) - expected.keys() - {"LC_CTYPE"}):
        raise RuntimeError("worker requires its disposable fixture")
    gate = cwd / "wait-for-fixture"
    gate.write_text("#!/bin/sh\n: > starts-toolstop\ni=0\nwhile [ ! -e release-toolstop ] && [ \"$i\" -lt 250 ]; do\n"
                    "  sleep .1\n  i=$((i+1))\ndone\n")
    gate.chmod(0o700)
    provider = Provider(cwd)
    os.environ["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{await provider.start()}"
    try:
        evidence = Evidence()
        async with asyncio.timeout(245):
            await main_cases(cwd, provider, evidence)
        print(json.dumps({"checks": evidence.names, "mockRequests": len(provider.rows)}), flush=True)
    finally:
        await provider.close()


if __name__ == "__main__":
    if sys.argv[1:] == ["--worker"]:
        asyncio.run(worker())
    elif not sys.argv[1:]:
        with tempfile.TemporaryDirectory(prefix="netizen-claude-native-") as name:
            home = Path(name).resolve()
            cwd = home / "project"
            cwd.mkdir()
            (home / ".claude").mkdir(mode=0o700)
            command = [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--worker"]
            with subprocess.Popen(command, env=fixture_environment(home), cwd=cwd, start_new_session=True) as worker_process:
                try:
                    status = worker_process.wait(timeout=270)
                except (subprocess.TimeoutExpired, KeyboardInterrupt):
                    # This group belongs exclusively to the disposable fixture worker.
                    os.killpg(worker_process.pid, signal.SIGTERM)
                    try:
                        worker_process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        os.killpg(worker_process.pid, signal.SIGKILL)
                    raise
                raise SystemExit(status)
    else:
        raise SystemExit("unexpected arguments")
