"""Controllable public Claude client double. No process, credentials or model calls."""
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, ToolUseBlock


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
    background: object | None = None
    prompt_id: str = field(default_factory=lambda: str(uuid4()))
    consumers: int = 0
    closes: int = 0
    interrupts: int = 0
    stops: list[str] = field(default_factory=list)
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
        self.stops.append(task_id)

    async def receive_messages(self):
        self.consumers += 1
        assert self.consumers == 1
        while True:
            msg = await self.stream.get()
            if msg is None:
                return
            if isinstance(msg, (Exception, asyncio.CancelledError)):
                raise msg
            if isinstance(msg, tuple):
                arrived, release = msg
                arrived.set()
                await release.wait()
                continue
            if isinstance(msg, asyncio.Event):
                msg.set()  # All preceding frames have returned to the public reader.
                continue
            yield msg

    async def consumed(self):
        barrier = asyncio.Event()
        self.stream.put_nowait(barrier)
        await asyncio.wait_for(barrier.wait(), 1)

    def init(self, mode="auto", **kwargs):
        self.prompt_id = str(uuid4())
        data = {"uuid": str(uuid4()), "session_id": self.session, "cwd": str(self.cwd), "user_message_uuids": [self.inputs[0]["uuid"]] if self.inputs else []}
        if mode is not None:
            data["permissionMode"] = mode
        self.stream.put_nowait(SystemMessage("session_state_changed", {"session_id": self.session, "state": "running"}))
        self.stream.put_nowait(SystemMessage("init", data | kwargs))

    def system(self, subtype, **fields):
        self.stream.put_nowait(SystemMessage(subtype, {"session_id": self.session, **fields}))

    def start_background(self, run="run", task="task", tool="tool", *, members=None):
        self.stream.put_nowait(AssistantMessage(content=[ToolUseBlock(id=tool, name="Bash",
            input={"run_in_background": True})], model="fixture", session_id=self.session))
        rows = members if members is not None else [{"task_id": task, "run_id": run}]
        self.system("background_tasks_changed", tasks=[{"task_type": "local_bash", **row} for row in rows])
        self.system("task_started", task_id=task, run_id=run, tool_use_id=tool, task_type="local_bash", is_backgrounded=True)

    def callback(self, data, callback_id=None):
        self.background.snapshot({"prompt_id": self.prompt_id, **data}, callback_id or str(uuid4()))

    def stop_snapshot(self, tasks=(), crons=()):
        self.callback({"session_id": self.session,
            "background_tasks": [{"id": t, "type": "shell", "status": "running"} for t in tasks],
            "session_crons": [{"id": c, "recurring": True} for c in crons]})

    def end_background(self, status="completed", *, run="run", task="task", members=()):
        self.system("task_updated", task_id=task, run_id=run, patch={"status": status})
        self.system("background_tasks_changed", tasks=[{"task_type": "local_bash", **row} for row in members])

    def wake_result(self, run="run", **kwargs):
        self.result(**({"user_message_uuid": None, "origin": {"kind": "task-notification",
            "producer": "session-task", "runId": run}} | kwargs))

    def result(self, subtype="success", reason="completed", is_error=False, *, snapshot=True, idle=True, **kwargs):
        if snapshot and reason == "completed" and not is_error and kwargs.get("result", "answer"):
            self.callback({"session_id": self.session, "prompt_id": self.prompt_id,
                                    "background_tasks": [], "session_crons": []})
        values = dict(uuid=str(uuid4()), session_id=self.session, result="answer", user_message_uuid=self.inputs[0]["uuid"])
        self.stream.put_nowait(ResultMessage(subtype=subtype, terminal_reason=reason, is_error=is_error,
                                             duration_ms=1, duration_api_ms=1, num_turns=1, **(values | kwargs)))
        if idle:
            self.stream.put_nowait(SystemMessage("session_state_changed", {"session_id": self.session, "state": "idle"}))


class FakeClaudeClients:
    def __init__(self):
        self.calls = []
        self.lookups = []
        self.cwds = {}
        self.configure = None
        self.created = asyncio.Event()

    def create(self, session_id, cwd, *, resume, background=None):
        client = FakeClaudeClient(session_id, cwd, resume)
        client.background = background
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
