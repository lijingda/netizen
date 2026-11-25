"""Controllable public Claude client double. No process, credentials or model calls."""
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from claude_agent_sdk import ResultMessage, SystemMessage


@dataclass
class FakeClaudeClient:
    session: str
    cwd: Path
    resume: bool
    connected: asyncio.Event = field(default_factory=asyncio.Event)
    writing: asyncio.Event = field(default_factory=asyncio.Event)
    closing: asyncio.Event = field(default_factory=asyncio.Event)
    connect_gate: asyncio.Event = field(default_factory=asyncio.Event)
    write_gate: asyncio.Event = field(default_factory=asyncio.Event)
    close_gate: asyncio.Event = field(default_factory=asyncio.Event)
    stream: asyncio.Queue = field(default_factory=asyncio.Queue)
    inputs: list = field(default_factory=list)
    consumers: int = 0
    closes: int = 0
    interrupts: int = 0
    stopped_tasks: list[str] = field(default_factory=list)
    failure: str | None = None

    def release(self):
        self.connect_gate.set()
        self.write_gate.set()
        self.close_gate.set()

    async def connect(self):
        self.connected.set()
        await self.connect_gate.wait()
        if self.failure == "connect":
            raise OSError("private provider diagnostic")

    async def query(self, prompt):
        assert self.connected.is_set()
        self.inputs.extend([item async for item in prompt])
        self.writing.set()
        await self.write_gate.wait()
        if self.failure == "query":
            raise OSError("private provider diagnostic")

    async def disconnect(self):
        self.closes += 1
        self.closing.set()
        await self.close_gate.wait()
        if self.failure == "close":
            raise RuntimeError("private process diagnostic")

    async def interrupt(self):
        self.interrupts += 1

    async def stop_task(self, task_id):
        self.stopped_tasks.append(task_id)

    async def receive_messages(self):
        self.consumers += 1
        assert self.consumers == 1
        while True:
            msg = await self.stream.get()
            if msg is None:
                return
            if isinstance(msg, Exception):
                raise msg
            yield msg

    def init(self, mode="auto", **kwargs):
        data = {"uuid": str(uuid4()), "session_id": self.session, "cwd": str(self.cwd)}
        if self.inputs:
            data["user_message_uuids"] = [self.inputs[-1]["uuid"]]
        if mode is not None:
            data["permissionMode"] = mode
        self.stream.put_nowait(SystemMessage("init", data | kwargs))

    def result(self, subtype="success", reason="completed", is_error=False, *, idle=True, **kwargs):
        values = dict(uuid=str(uuid4()), session_id=self.session, result="answer", user_message_uuid=self.inputs[-1]["uuid"])
        self.stream.put_nowait(ResultMessage(subtype=subtype, terminal_reason=reason, is_error=is_error,
                                             duration_ms=1, duration_api_ms=1, num_turns=1, **(values | kwargs)))
        if idle and kwargs.get("user_message_uuid", self.inputs[-1]["uuid"]) == self.inputs[-1]["uuid"]:
            self.stream.put_nowait(SystemMessage("session_state_changed", {"session_id": self.session, "state": "idle"}))


class FakeClaudeClients:
    def __init__(self):
        self.titles = {}
        self.forks = []
        self.calls = []
        self.lookups = []
        self.cwds = {}
        self.configure = None
        self.created = asyncio.Event()
        self.history = "assistant"

    async def session_metadata(self, session):
        from netizen_cli.runtime.contracts import NativeThreadMetadata
        return NativeThreadMetadata(session, self.titles.get(session), "")

    def rename(self, session, name):
        self.titles[session] = name
        return name

    def fork(self, session_id, cwd, title):
        child_id = str(uuid4())
        self.forks.append((session_id, cwd, title, child_id))
        self.cwds[child_id] = cwd
        return child_id

    async def last_assistant(self, session, cwd, reader):
        return self.history

    def create(self, session_id, cwd, *, resume, live=None):
        client = FakeClaudeClient(session_id, cwd, resume)
        client.live = live
        client.release()
        if self.configure:
            self.configure(client)
        self.calls.append(client)
        self.cwds[session_id] = cwd
        self.created.set()
        return client

    async def session_cwd(self, session_id, candidates=()):
        self.lookups.append(session_id)
        return self.cwds.get(session_id)
