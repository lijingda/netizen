"""Drive the registered Feishu handlers and observe the bundled CLI through its public SDK."""
import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
import json
import os
from pathlib import Path
import time
from unittest.mock import AsyncMock, Mock, patch

from claude_agent_sdk import ResultMessage, SystemMessage
from lark_channel import OutboundCard, OutboundPost, SendResult
import psutil

from netizen_cli.admin.users import AdminUsers
from netizen_cli.bindings import BindingStore
from netizen_cli.claude_installation import verified_claude_cli
from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.main import Events, ServiceCore, _register_channel_handlers, _SHUTDOWN_BUDGET_SECONDS
from netizen_cli.projects import ProjectRegistry
from netizen_cli.runtime.claude_client import BundledClaudeClients
from netizen_cli.runtime.claude_ordinary import DEFAULT_MODE, GRACE, UNKNOWN
from netizen_cli.settings import AdminWebSettings, Settings
from tests.support.channel_cards import _card_button_value, direct_button_event, direct_card_event, new_form_values
from tests.support.channel_messages import FakeChannel, FakeMessage
from tests.support.claude_provider import PROJECT_SENTINEL

OWNER = "ou_user"
SCOPE = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
BUSY = "当前会话正在执行，本条消息未发送。"
REFUSED = "这个 Claude 实例仅限所有者使用。"
STOPPED = "Claude 本次执行已停止。"


class RecordingChannel(FakeChannel):
    def __init__(self):
        super().__init__()
        self.handlers = {}
        self.events = []
        self.chat_types["oc_direct"] = "p2p"

    def on(self, key, callback):
        self.handlers[key] = callback

    def update_policy(self, **policy):
        pass

    async def reply(self, message, content, opts=None):
        self.events.append(("reply", message.id, content))
        result = await super().reply(message, content, opts)
        if isinstance(content, OutboundCard):
            card_id = "card-" + message.id
            self.fetched_messages[card_id] = {"data": {"items": [{"chat_id": message.conversation.chat_id,
                                                                  "thread_id": message.conversation.thread_id}]}}
            return SendResult.ok(message_id=card_id)
        return result

    async def add_reaction(self, message_id, emoji_type):
        self.events.append(("add", message_id, emoji_type))
        return await super().add_reaction(message_id, emoji_type)

    async def remove_reaction(self, message_id, reaction_id):
        self.events.append(("remove", message_id, reaction_id))
        return await super().remove_reaction(message_id, reaction_id)

    async def update_card(self, message_id, card):
        self.events.append(("update", message_id, card))
        return await super().update_card(message_id, card)


async def wait_for(predicate, seconds=25):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(.05)


def running(process):
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def cli_children():
    cli = verified_claude_cli().resolve()
    children = []
    for child in psutil.Process().children(recursive=True):
        try:
            if Path(child.exe()).resolve() == cli:
                children.append(child)
        except psutil.NoSuchProcess:
            continue
    return children


class NativeExecution:
    def __init__(self, client, session, close_gate):
        self.client, self.session, self.close_gate = client, session, close_gate
        self.inputs = []
        self.attributed = False
        self.notification = asyncio.Event()
        self.closing = asyncio.Event()
        self.finished = asyncio.Event()
        self.processes = []
        self.init_sessions = []

    async def connect(self):
        await self.client.connect()
        self.processes = cli_children()
        assert self.processes, "bundled CLI was not observed"

    async def query(self, prompt):
        async def observe():
            async for item in prompt:
                self.inputs.append(item["uuid"])
                yield item
        await self.client.query(observe())

    async def receive_messages(self):
        async for msg in self.client.receive_messages():
            if isinstance(msg, SystemMessage) and msg.subtype == "init":
                self.init_sessions.append(msg.data.get("session_id"))
            if isinstance(msg, ResultMessage):
                assert msg.session_id == self.session
                if (isinstance(msg.origin, dict) and msg.origin.get("kind") == "task-notification"
                        and not msg.user_message_uuid and not msg.user_message_uuids and msg.terminal_reason is None):
                    self.notification.set()
                if set(self.inputs).intersection([msg.user_message_uuid, *(msg.user_message_uuids or ())]):
                    self.attributed = True
            yield msg

    async def disconnect(self):
        self.closing.set()
        if self.close_gate:
            await self.close_gate.wait()
        await self.client.disconnect()
        self.finished.set()

    async def interrupt(self):
        await self.client.interrupt()


class NativeClients(BundledClaudeClients):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.close_gate = None

    def create(self, session_id, cwd, *, resume):
        public_client = super().create(session_id, cwd, resume=resume)
        observed = NativeExecution(public_client, session_id, self.close_gate)
        self.calls.append(observed)
        return observed


def install_tools(cwd):
    for mode in ("foreground", "background"):
        script = cwd / ("fixture-" + mode)
        # exec preserves this PID, so the harness can follow the tool even if it is reparented.
        body = ("exec sleep 90" if mode == "background" else
                'i=0; while [ ! -e fixture-release ] && [ "$i" -lt 900 ]; do sleep .1; i=$((i+1)); done')
        script.write_text(f'#!/bin/sh\necho "$$" > fixture-{mode}.pid\n{body}\n')
        script.chmod(0o700)


@dataclass
class Driver:
    cwd: Path
    core: ServiceCore
    store: BindingStore
    channel: RecordingChannel
    clients: NativeClients
    clock: list
    provider: object
    session: str | None = None

    def rows(self, label=None):
        return [r for r in self.provider.rows if r["path"] == "/v1/messages" and (label is None or r["label"] == label)]

    async def send(self, label, text, **sender):
        await self.channel.handlers[Events.MESSAGE](FakeMessage(text, message_id=label, **sender))

    async def click(self, event):
        await self.channel.handlers[Events.CARD_ACTION](event)

    def replies(self, label):
        return [content for target, content in self.channel.replies if target == label]

    async def create(self):
        await self.send("new", "/new")
        card, = self.replies("new")[-1:]
        assert isinstance(card, OutboundCard)
        await self.click(direct_card_event(self.channel, new_form_values(card), message_id="card-new"))
        assert self.store.active_binding(SCOPE.key) is not None
        self.session = None

    async def submit(self, label, scenario="text"):
        before = len(self.clients.calls)
        await self.send(label, f"[[S:{scenario}/{label}]]")
        assert len(self.clients.calls) == before + 1, "message did not start one execution"
        call = self.clients.calls[-1]
        binding = self.store.active_binding(SCOPE.key)
        assert binding.native_thread_id == call.session
        if self.session:
            assert call.session == self.session, "resume changed native session ID"
        self.session = call.session
        return call

    async def terminal(self, label, *, interrupted=False):
        assert await self.core._claude.wait_idle(45), "execution did not finish"
        call = self.clients.calls[-1]
        replies = [r for r in self.replies(label) if r != DEFAULT_MODE]
        assert UNKNOWN not in replies, "channel delivered unknown"
        assert set(call.init_sessions) == {self.session} and len(call.inputs) == 1
        assert call.finished.is_set() and call.attributed, "missing attributed native Result or disconnect"
        post, = replies
        if interrupted:
            assert post == STOPPED, "incorrect interrupted terminal"
        else:
            assert isinstance(post, OutboundPost) and [m.open_id for m in post.mentions] == [OWNER]
            assert post.markdown == "fixture-reply-" + label, "incorrect answer"
        events = self.channel.events
        terminal = "CrossMark" if interrupted else "DONE"
        assert events.index(("add", label, "Typing")) < events.index(("add", label, terminal)) < events.index(("reply", label, post))
        await self.reaped()
        return post if interrupted else post.markdown

    async def reaped(self):
        await wait_for(lambda: all(not running(p) for c in self.clients.calls for p in c.processes)
                       and not cli_children(), 8)

    async def ready_tool(self, mode):
        path = self.cwd / f"fixture-{mode}.pid"
        await wait_for(lambda: path.exists() and path.read_text().strip().isdigit())
        proc = psutil.Process(int(path.read_text()))
        # Require our disposable CLI to own the tool while it is active.
        assert any(parent in self.clients.calls[-1].processes for parent in proc.parents())
        path.unlink()
        return proc

    def release(self):
        (self.cwd / "fixture-release").touch()

    async def startup_grace(self):
        await self.send("grace-early", "[[S:text/grace-early]]")
        assert self.replies("grace-early") == [GRACE] and not self.clients.calls and not self.rows("grace-early")
        self.clock[0] = 2
        await self.create()
        await self.submit("grace-ready")
        await self.terminal("grace-ready")
        assert self.replies("grace-early") == [GRACE] and not self.rows("grace-early")
        assert len(self.rows("grace-ready")) == 1 and len(self.clients.calls) == 1

    async def ordinary(self):
        await self.submit("ordinary")
        await self.terminal("ordinary")
        row, = self.rows("ordinary")
        assert row["project_sentinel"], "project CLAUDE.md did not reach model"

    def saw_exchange(self, label, previous):
        row, = self.rows(label)
        assert any(m["role"] == "user" and ("text", previous) in m["markers"] for m in row["history"])
        assert any(m["role"] == "assistant" and "fixture-reply-" + previous in m["answers"] for m in row["history"])

    async def resume(self):
        await self.submit("resume")
        await self.terminal("resume")
        self.saw_exchange("resume", "ordinary")

    async def handoff(self):
        child = await asyncio.create_subprocess_exec(str(verified_claude_cli()), "-p", "--resume", self.session,
            "--permission-mode", "auto", "--output-format", "json", "[[S:text/handoff]]", cwd=self.cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            output, _ = await asyncio.wait_for(child.communicate(), timeout=45)
            assert child.returncode == 0 and json.loads(output)["session_id"] == self.session
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
        self.saw_exchange("handoff", "resume")
        await self.submit("after-cli")
        await self.terminal("after-cli")
        self.saw_exchange("after-cli", "handoff")

    async def busy(self):
        await self.submit("busy-a", "foreground")
        tool = await self.ready_tool("foreground")
        before = len(self.rows())
        await self.send("busy-b", "[[S:text/busy-b]]")
        assert self.replies("busy-b") == [BUSY] and len(self.rows()) == before
        self.release()
        await self.terminal("busy-a")
        assert not running(tool)
        (self.cwd / "fixture-release").unlink()
        await self.submit("after-busy")
        await self.terminal("after-busy")
        assert [len(self.rows(k)) for k in ("busy-a", "busy-b", "after-busy")] == [2, 0, 1]

    async def exact_stop(self):
        await self.submit("stop-a", "foreground")
        tool = await self.ready_tool("foreground")
        await self.send("sessions", "/sessions")
        card, = self.replies("sessions")
        stop = _card_button_value(card, "停止")
        await self.send("stop-command", "/stop")
        await self.terminal("stop-a", interrupted=True)
        ack, = self.replies("stop-command")
        assert ack == "正在请求停止当前 Claude 执行。"
        assert self.channel.events.index(("reply", "stop-command", ack)) < self.channel.events.index(
            ("reply", "stop-a", self.replies("stop-a")[-1]))
        assert not running(tool)
        await self.submit("stop-b", "foreground")
        await self.ready_tool("foreground")
        await self.click(direct_button_event(stop, message_id="card-sessions"))
        assert any("变化" in str(text) for text in self.replies("card-sessions")), "stale stop was not rejected"
        self.release()
        await self.terminal("stop-b")
        (self.cwd / "fixture-release").unlink()
        assert [len(self.rows(k)) for k in ("stop-a", "stop-b")] == [1, 2]

    async def background(self):
        # Stop A at its real disconnect so the harness can witness the tool before it is killed.
        gate = self.clients.close_gate = asyncio.Event()
        try:
            first = await self.submit("bg-a", "background")
            await asyncio.wait_for(first.closing.wait(), 30)
            tool = await self.ready_tool("background")
        finally:
            gate.set()
            self.clients.close_gate = None
        await self.terminal("bg-a")
        await wait_for(lambda: not running(tool))
        second = await self.submit("bg-b", "hold-stream")
        try:
            await asyncio.wait_for(second.notification.wait(), 20)
            # Keep the real model response pending for the documented reconciliation window.
            # A spurious unknown closes the client during that window on the old runtime.
            with suppress(TimeoutError):
                await asyncio.wait_for(second.closing.wait(), timeout=3.5)
        finally:
            self.provider.release("bg-b")
        await self.terminal("bg-b")
        assert [len(self.rows(k)) for k in ("bg-a", "bg-b")] == [2, 1]

    async def stop_during_close(self):
        gate = self.clients.close_gate = asyncio.Event()
        try:
            call = await self.submit("close")
            await asyncio.wait_for(call.closing.wait(), 30)
            await self.send("closing-stop", "/stop")
            assert self.replies("closing-stop") == ["这次 Claude 执行已经结束，结果正在送出。"]
            assert not self.replies("close")
        finally:
            gate.set()
            self.clients.close_gate = None
        await self.terminal("close")
        assert len(self.rows("close")) == 1

    async def owner_gate(self):
        rows, calls = len(self.rows()), len(self.clients.calls)
        before = tuple(self.store._connection.iterdump())
        await self.send("other-dm", "hello", sender_id="other")
        await self.send("other-group", "hello", sender_id="other", chat_id="oc_group", chat_type="group", mentioned_bot=True)
        await self.send("owner-card", "/sessions")
        card, = self.replies("owner-card")
        refresh = _card_button_value(card, "刷新列表")
        event = direct_button_event(refresh, message_id="card-owner-card")
        event.operator.open_id = "other"
        await self.click(event)
        assert self.replies("other-dm") == self.replies("other-group") == [REFUSED]
        assert self.replies("card-owner-card") == [REFUSED]
        await self.send("other-help", "/help", sender_id="other")
        assert "/new" in self.replies("other-help")[0]
        assert (len(self.rows()), len(self.clients.calls)) == (rows, calls)
        assert tuple(self.store._connection.iterdump()) == before

    async def fallback(self):
        settings = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "settings.json"
        original = settings.read_text()
        settings.write_text(json.dumps({**json.loads(original), "disableAutoMode": "disable"}))
        try:
            for label in ("fallback-one", "fallback-two"):
                await self.submit(label)
                await self.terminal(label)
                assert self.replies(label)[0] == DEFAULT_MODE and self.replies(label).count(DEFAULT_MODE) == 1
                assert len(self.rows(label)) == 1
        finally:
            settings.write_text(original)

    async def shutdown(self):
        await self.submit("shutdown", "foreground")
        tool = await self.ready_tool("foreground")
        async with asyncio.timeout(_SHUTDOWN_BUDGET_SECONDS):
            await self.core.close()
            await self.reaped()
            await wait_for(lambda: not running(tool))
        await self.terminal("shutdown", interrupted=True)
        assert len(self.rows("shutdown")) == 1


@asynccontextmanager
async def channel_driver(cwd, provider):
    root = cwd.parent / "channel-instance"
    root.mkdir()
    AdminUsers(root, "cli_test").add_initial(OWNER)
    config = Settings(app_id="cli_test", app_secret="fixture", data_dir=root / "data", project_root=cwd.parent,
                      projects={"test": cwd}, security_mode="audit", admin_web=AdminWebSettings(), backend="claude")
    channel = RecordingChannel()
    store = BindingStore()
    core = ServiceCore(settings=config, instance_root=root, channel=channel, store=store,
                       projects=ProjectRegistry(store=store, projects=config.projects, project_root=config.project_root))
    admin = Mock(bind=AsyncMock(), close_listener=AsyncMock(), drain=AsyncMock(), config_snapshot=None, urls=())
    clients = NativeClients()
    clock = [0.]
    try:
        with (patch("netizen_cli.main.AdminWebRunner", return_value=admin),
              patch("netizen_cli.runtime.claude_client.BundledClaudeClients", return_value=clients)):
            await core.start()
        core._claude._clock = lambda: clock[0]
        core.open_admission()
        _register_channel_handlers(channel, core.application)
        yield Driver(cwd, core, store, channel, clients, clock, provider)
    finally:
        if clients.close_gate:
            clients.close_gate.set()
        await core.close()


EXPECTED_REQUESTS = {
    "startup_grace": 1, "ordinary": 1, "resume": 1, "handoff": 2, "busy": 3, "exact_stop": 3,
    "background": 3, "stop_during_close": 1, "owner_gate": 0, "fallback": 2, "shutdown": 1,
}
CASES = tuple(EXPECTED_REQUESTS)


async def channel_cases(cwd, provider):
    (cwd / "CLAUDE.md").write_text(f"The project sentinel is {PROJECT_SENTINEL}.\n")
    install_tools(cwd)
    (Path(os.environ["CLAUDE_CONFIG_DIR"]) / "settings.json").write_text(json.dumps({
        "model": "claude-sonnet-4-6", "permissions": {"allow": ["Bash(./fixture-foreground)", "Bash(./fixture-background)"]},
    }))
    async with channel_driver(cwd, provider) as driver:
        for name in CASES:
            started = time.monotonic()
            requests = len(driver.rows())
            passed = False
            try:
                async with asyncio.timeout(65):
                    await getattr(driver, name)()
                    await driver.reaped()
                    assert len(driver.rows()) - requests == EXPECTED_REQUESTS[name], "unexpected model request count"
                passed = True
            finally:
                print(json.dumps({"scenario": name, "passed": passed, "seconds": round(time.monotonic()-started, 3),
                                  "requests": len(driver.rows())-requests}), flush=True)
