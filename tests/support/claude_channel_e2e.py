"""Drive the registered Feishu handlers and observe the bundled CLI through its public SDK."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import time
from unittest.mock import AsyncMock, Mock, patch

from claude_agent_sdk import ResultMessage, SystemMessage
from lark_channel import OutboundCard, SendResult
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
from tests.support.channel_messages import PNG, FakeChannel, FakeMessage

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
        return SendResult.ok(message_id=f"reply-{message.id}-{len(self.replies)}") if result.success else result

    async def send(self, to, content, opts=None):
        self.send_calls.append((to, content, opts))
        return SendResult.ok(message_id="sent", raw={"code": 0, "data": {"message_id": "sent", "chat_id": to,
                                                             "parent_id": opts.reply_to, "root_id": opts.reply_to}})

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
        self.state = None
        self.attributed = False
        self.answer = None
        self.reported_turns = 0
        self.bash_denials = 0
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
            if isinstance(msg, SystemMessage) and msg.subtype == "session_state_changed":
                self.state = msg.data["state"]
            if isinstance(msg, SystemMessage) and msg.subtype == "init":
                self.init_sessions.append(msg.data.get("session_id"))
            if isinstance(msg, ResultMessage):
                assert msg.session_id == self.session
                self.reported_turns += msg.num_turns
                if (isinstance(msg.origin, dict) and msg.origin.get("kind") == "task-notification"
                        and not msg.user_message_uuid and not msg.user_message_uuids and msg.terminal_reason is None):
                    self.notification.set()
                if set(self.inputs).intersection([msg.user_message_uuid, *(msg.user_message_uuids or ())]):
                    self.attributed = True
                    self.bash_denials = sum(isinstance(item, dict) and item.get("tool_name") == "Bash"
                                            for item in msg.permission_denials or [])
                    self.answer = msg.result
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
    def __init__(self, live):
        super().__init__()
        self.calls = []
        self.live = live
        self.turn_limit = 1
        self.expected_turns = 1
        self.close_gate = None

    def create(self, session_id, cwd, *, resume, live=None):
        public_client = super().create(session_id, cwd, resume=resume, live=live)
        if self.live:
            self.live.budget.reserve(self.turn_limit * self.expected_turns)
            public_client.options.max_turns = self.turn_limit
        observed = NativeExecution(public_client, session_id, self.close_gate)
        self.calls.append(observed)
        return observed


def install_tools(cwd):
    for mode in ("foreground", "background"):
        script = cwd / ("fixture-" + mode)
        # exec preserves this PID, so the harness can follow the tool even if it is reparented.
        body = ('i=0; while [ ! -e background-release ] && [ "$i" -lt 900 ]; do sleep .1; i=$((i+1)); done' if mode == "background" else
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
    live: object
    cli_submissions: int = 0
    cli_turns: int = 0
    tools: list = field(default_factory=list)
    session: str | None = None

    def rows(self, label=None):
        return [r for r in self.provider.rows if r["path"] == "/v1/messages" and (label is None or r["label"] == label)]

    def prompt(self, label, scenario="text"):
        return self.live.prompt(label, scenario) if self.live else f"[[S:{scenario}/{label}]]"

    def submissions(self):
        return self.cli_submissions + sum(len(call.inputs) for call in self.clients.calls)

    def reported_turns(self):
        return self.cli_turns + sum(call.reported_turns for call in self.clients.calls)

    def check_answer(self, label, answer):
        if self.live:
            self.live.check_answer(label, answer)
        else:
            assert answer == "fixture-reply-" + label, "incorrect answer"

    async def send(self, label, text, **sender):
        await self.channel.handlers[Events.MESSAGE](FakeMessage(text, message_id=label, **sender))

    async def click(self, event):
        await self.channel.handlers[Events.CARD_ACTION](event)

    def replies(self, label):
        return [content for target, content in self.channel.replies if target == label]

    async def create(self):
        await self.send("new", "/new")
        card, = self.replies("new")
        assert isinstance(card, OutboundCard)
        await self.click(direct_card_event(self.channel, new_form_values(card) | {"new_progress_card": "progress-card:v1:[false,null]"}, message_id="card-new"))
        assert self.store.active_binding(SCOPE.key) is not None
        self.session = None

    async def submit(self, label, scenario="text"):
        before = len(self.clients.calls)
        self.clients.turn_limit = 3 if scenario in ("foreground", "background", "denied-touch") else 1
        if scenario == "background":
            # One tool round and one answer per turn. Reserve H1, H2 and the wake.
            self.clients.turn_limit, self.clients.expected_turns = 2, 3
        try:
            await self.send(label, self.prompt(label, scenario))
        finally:
            self.clients.turn_limit = self.clients.expected_turns = 1
        assert len(self.clients.calls) == before + 1, "message did not start one execution"
        call = self.clients.calls[-1]
        binding = self.store.active_binding(SCOPE.key)
        assert binding.native_thread_id == call.session
        if self.session:
            assert call.session == self.session, "resume changed native session ID"
        self.session = call.session
        return call

    async def terminal(self, label, *, interrupted=False, denied_bash=False):
        assert await self.core._claude.wait_idle(45), "execution did not finish"
        call = self.clients.calls[-1]
        replies = self.replies(label)
        if denied_bash:
            assert call.bash_denials > 0, "attributed Result did not report a Bash denial"
            notice = f"本次有 {call.bash_denials} 个操作被 Claude Code 的权限设置拒绝（Bash）。如需允许，请在 Claude Code 设置中调整权限规则。"
            assert replies[0] in (notice, DEFAULT_MODE + "\n" + notice)
            replies = replies[1:]
        else:
            replies = [r for r in replies if r != DEFAULT_MODE]
        assert UNKNOWN not in replies, "channel delivered unknown"
        assert set(call.init_sessions) == {self.session} and len(call.inputs) == 1
        assert call.finished.is_set() and call.attributed, "missing attributed native Result or disconnect"
        events = self.channel.events
        terminal = "CrossMark" if interrupted else "DONE"
        typing = events.index(("add", label, "Typing"))
        finished = events.index(("add", label, terminal))
        assert typing < finished
        reminders = [content for _, content, opts in self.channel.send_calls if opts.reply_to.startswith("reply-" + label + "-")]
        if interrupted:
            assert [s.split("\n\n")[0] for s in replies] == ([STOPPED] if call.answer else []) and not reminders
        else:
            post, = replies
            assert isinstance(post, str) and typing < events.index(("reply", label, post)) < finished
            assert [[m.open_id for m in r.mentions] for r in reminders] == [[OWNER]]
            if self.provider and label == "ordinary":
                assert call.answer == "fixture-reply-ordinary ![pixel](result.png)"
                assert post.strip() == "fixture-reply-ordinary ![pixel](img_uploaded_1)"
                (source, kind), = self.channel.upload_calls
                assert kind == "image" and source.buffer == PNG
            else:
                assert call.answer and post == call.answer, "terminal did not deliver the attributed answer"
                self.check_answer(label, post)
        await self.reaped()

    async def reaped(self):
        await wait_for(lambda: all(not running(p) for c in self.clients.calls for p in c.processes)
                       and all(not running(p) for p in self.tools) and not cli_children(), 8)

    async def ready_tool(self, mode):
        path = self.cwd / f"fixture-{mode}.pid"
        await wait_for(lambda: path.exists() and path.read_text().strip().isdigit())
        proc = psutil.Process(int(path.read_text()))
        # Require our disposable CLI to own the tool while it is active.
        assert any(parent in self.clients.calls[-1].processes for parent in proc.parents())
        self.tools.extend([proc, *proc.children(recursive=True)])
        path.unlink()
        return proc

    def release(self):
        (self.cwd / "fixture-release").touch()

    async def startup_grace(self):
        await self.send("grace-early", self.prompt("grace-early"))
        assert self.replies("grace-early") == [GRACE] and not self.clients.calls
        self.clock[0] = 2
        await self.create()
        await self.submit("grace-ready")
        await self.terminal("grace-ready")
        assert self.replies("grace-early") == [GRACE] and len(self.clients.calls) == 1

    async def ordinary(self):
        if self.provider:
            (self.cwd / "result.png").write_bytes(PNG)
        await self.submit("ordinary", "inline-image" if self.provider else "text")
        await self.terminal("ordinary")
        if self.provider:
            row, = self.rows("ordinary")
            assert row["project_sentinel"], "project CLAUDE.md did not reach model"

    def saw_exchange(self, label, previous):
        if self.live:
            # The model must recall the unpredictable token in its observed answer.
            return
        row, = self.rows(label)
        previous_scenario = "inline-image" if previous == "ordinary" else "text"
        assert any(m["role"] == "user" and (previous_scenario, previous) in m["markers"] for m in row["history"])
        assert any(m["role"] == "assistant" and "fixture-reply-" + previous in m["answers"] for m in row["history"])

    async def resume(self):
        await self.submit("resume")
        await self.terminal("resume")
        self.saw_exchange("resume", "ordinary")

    async def handoff(self):
        limits = ["--max-turns", str(self.live.budget.reserve(1))] if self.live else []
        child = await asyncio.create_subprocess_exec(str(verified_claude_cli()), "-p", "--resume", self.session, *limits,
            "--permission-mode", "auto", "--output-format", "json", self.prompt("handoff"), cwd=self.cwd,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            self.cli_submissions += 1
            output, _ = await asyncio.wait_for(child.communicate(), timeout=45)
            assert child.returncode == 0
            result = json.loads(output)
            assert result["session_id"] == self.session and not result["is_error"]
            self.cli_turns += result["num_turns"]
            self.check_answer("handoff", result["result"])
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
        before = len(self.clients.calls)
        await self.send("busy-b", self.prompt("busy-b"))
        assert self.replies("busy-b") == [BUSY] and len(self.clients.calls) == before
        self.release()
        await self.terminal("busy-a")
        assert not running(tool)
        (self.cwd / "fixture-release").unlink()
        await self.submit("after-busy")
        await self.terminal("after-busy")

    async def exact_stop(self):
        await self.submit("stop-a", "foreground")
        tool = await self.ready_tool("foreground")
        await self.send("sessions", "/sessions")
        card, = self.replies("sessions")
        stop = _card_button_value(card, "停止")
        await self.send("status", "/status")
        status_card, = self.replies("status")
        await self.click(direct_button_event(_card_button_value(status_card, "停止"), message_id="card-status"))
        assert "当前会话" in json.dumps(self.channel.updates[-1], ensure_ascii=False)
        assert self.channel.updates[-1][0] == "card-status"
        await self.terminal("stop-a", interrupted=True)
        ack, = self.replies("card-status")
        assert ack == "正在请求停止当前 Claude 执行。"
        assert self.channel.events.index(("reply", "card-status", ack)) < self.channel.events.index(
            ("add", "stop-a", "CrossMark"))
        assert not running(tool)
        await self.submit("stop-b", "foreground")
        await self.ready_tool("foreground")
        await self.click(direct_button_event(stop, message_id="card-sessions"))
        assert any("变化" in str(text) for text in self.replies("card-sessions")), "stale stop was not rejected"
        self.release()
        await self.terminal("stop-b")
        (self.cwd / "fixture-release").unlink()

    async def background(self):
        first = await self.submit("bg-a", "background")
        try:
            tool = await self.ready_tool("background")
            await wait_for(lambda: first.state == "idle" and self.replies("bg-a"))
            await self.send("bg-b", self.prompt("bg-b"))
            await wait_for(lambda: first.state == "idle" and self.replies("bg-b"))
            assert self.clients.calls[-1] is first and len(first.inputs) == 2 and running(tool)
        finally:
            (self.cwd / "background-release").touch()
        assert await self.core._claude.wait_idle(45)
        assert not running(tool) and first.finished.is_set()
        bodies = [r for r in self.replies("bg-a") if r != DEFAULT_MODE]
        assert len(bodies) == 2 and len([r for r in self.replies("bg-b") if r != DEFAULT_MODE]) == 1
        for label in ("bg-a", "bg-b"):
            last_reply = max(i for i, (target, _) in enumerate(self.channel.replies, 1) if target == label)
            reminders = [opts.reply_to for _, _, opts in self.channel.send_calls if opts.reply_to.startswith(f"reply-{label}-")]
            assert reminders == [f"reply-{label}-{last_reply}"]

    async def stop_during_close(self):
        gate = self.clients.close_gate = asyncio.Event()
        try:
            call = await self.submit("close")
            await asyncio.wait_for(call.closing.wait(), 30)
            await wait_for(lambda: any(reply != DEFAULT_MODE for reply in self.replies("close")))
            await self.send("closing-stop", "/stop")
            assert self.replies("closing-stop") == ["这次 Claude 执行已经结束，客户端正在收尾。"]
            await self.send("close-busy", "more")
            assert "上一轮正在收尾" in self.replies("close-busy")[0]
        finally:
            gate.set()
            self.clients.close_gate = None
        await self.terminal("close")

    async def owner_gate(self):
        calls = len(self.clients.calls)
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
        assert len(self.clients.calls) == calls
        assert tuple(self.store._connection.iterdump()) == before

    async def permission_denials(self):
        settings = self.cwd / ".claude" / "settings.json"
        settings.parent.mkdir(exist_ok=True)
        project = json.loads(settings.read_text()) if settings.exists() else {}
        project.setdefault("permissions", {})["deny"] = ["Bash(touch:*)"]
        settings.write_text(json.dumps(project))
        probe = self.cwd / "permission-probe"
        assert not probe.exists()
        await self.submit("denied-touch", "denied-touch")
        await self.terminal("denied-touch", denied_bash=True)
        assert not probe.exists()

    async def fallback(self):
        settings = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "settings.json"
        original = settings.read_text()
        settings.write_text(json.dumps({**json.loads(original), "disableAutoMode": "disable"}))
        try:
            for label in ("fallback-one", "fallback-two"):
                await self.submit(label)
                await self.terminal(label)
                assert self.replies(label)[0] == DEFAULT_MODE and self.replies(label).count(DEFAULT_MODE) == 1
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


@asynccontextmanager
async def channel_driver(cwd, provider, live):
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
    clients = NativeClients(live)
    clock = [0.]
    driver = Driver(cwd, core, store, channel, clients, clock, provider, live)
    try:
        with (patch("netizen_cli.main.AdminWebRunner", return_value=admin),
              patch("netizen_cli.runtime.claude_client.BundledClaudeClients", return_value=clients)):
            await core.start()
        core._claude._clock = lambda: clock[0]
        core.open_admission()
        _register_channel_handlers(channel, core.application)
        yield driver
    finally:
        if clients.close_gate:
            clients.close_gate.set()
        await core.close()
        await driver.reaped()


EXPECTED_REQUESTS = {
    "startup_grace": 1, "ordinary": 1, "resume": 1, "handoff": 2, "busy": 3, "exact_stop": 3,
    "background": 4, "stop_during_close": 1, "owner_gate": 0, "permission_denials": 2, "fallback": 2, "shutdown": 1,
}
CASES = tuple(EXPECTED_REQUESTS)


async def channel_cases(cwd, provider):
    from tests.support.claude_provider import PROJECT_SENTINEL

    (cwd / "CLAUDE.md").write_text(f"The project sentinel is {PROJECT_SENTINEL}.\n")
    install_tools(cwd)
    (Path(os.environ["CLAUDE_CONFIG_DIR"]) / "settings.json").write_text(json.dumps({
        "model": "claude-sonnet-4-6", "permissions": {"allow": ["Bash(./fixture-foreground)", "Bash(./fixture-background)"]},
    }))
    await run_cases(cwd, provider=provider)


async def run_cases(cwd, *, provider=None, live=None, report=print):
    async with channel_driver(cwd, provider, live) as driver:
        for name in CASES:
            started = time.monotonic()
            requests = len(driver.rows()) if provider else None
            submissions = driver.submissions()
            reported_turns = driver.reported_turns()
            location = {}
            status = "fail"
            try:
                if live and name == "fallback":
                    status = "skipped_mock_only"
                    continue
                async with asyncio.timeout(150 if live else 65):
                    await getattr(driver, name)()
                    await driver.reaped()
                    if live:
                        assert not live.budget.refused, "live turn cap reached"
                    if provider:
                        assert len(driver.rows()) - requests == EXPECTED_REQUESTS[name], "unexpected model request count"
                status = "pass"
            except AssertionError as error:
                frame = error.__traceback__
                while frame.tb_next is not None:
                    frame = frame.tb_next
                location = {"file": Path(frame.tb_frame.f_code.co_filename).name, "line": frame.tb_lineno}
                raise
            finally:
                report(json.dumps({"scenario": name, "status": status, **location, "seconds": round(time.monotonic()-started, 3),
                                   "submissions": driver.submissions()-submissions,
                                   "reportedModelTurns": driver.reported_turns()-reported_turns,
                                   "providerRequests": len(driver.rows())-requests if provider else None,
                                   **({"modelTurnsReserved": live.budget.reserved} if live else {})}), flush=True)
