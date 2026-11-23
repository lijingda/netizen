"""Public Claude SDK boundary for an ordinary execution."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterable, AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ContentBlock, HookJSONOutput, HookMatcher, Message, ResultMessage,
    StreamEvent, SystemMessage, ToolUseBlock, get_session_info,
)

from .. import claude_installation
from ..external_calls import external_call
from ..claude_installation import verified_claude_cli
from ..ordinary_sessions import OrdinaryInputRejected, PreparedPrompt


class ClaudeClient(Protocol):
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def interrupt(self) -> None: ...
    async def query(self, prompt: AsyncIterable[dict[str, Any]]) -> None: ...
    def receive_messages(self) -> AsyncIterator[Message]: ...


# Validate only the public metadata used by this adapter. Optional SDK attributes
# may be null. Optional system-envelope keys must be valid if the CLI emits them.
def _identifier(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _optional(check: Callable[[Any], bool]) -> Callable[[Any], bool]:
    return lambda value: value is None or check(value)


def _ids(value: Any) -> bool:
    return isinstance(value, list) and all(_identifier(x) for x in value)


def _membership(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    for key in ("task_id", "run_id"):
        if not all(isinstance(row, dict) and _identifier(row.get(key)) for row in value):
            return False
        if len({row[key] for row in value}) != len(value):
            return False
    return True


def _content(value: Any) -> bool:
    return isinstance(value, list) and all(
        isinstance(block, ContentBlock) and (not isinstance(block, ToolUseBlock) or _identifier(block.id))
        for block in value
    )


def _origin(value: Any) -> bool:
    return isinstance(value, dict) and _identifier(value.get("kind")) and all(
        _identifier(value[key]) for key in ("producer", "runId") if key in value)


_session = {"session_id": _identifier}
_human = {"user_message_uuid": _optional(_identifier), "user_message_uuids": _optional(_ids)}
_task = _session | {"task_id": _identifier, "run_id": _identifier}
CONSUMED_FIELDS: dict[str, dict[str, Callable[[Any], bool]]] = {
    "system": {"uuid?": _identifier, "session_id?": _identifier},
    "init": _session | {"uuid": _identifier, "cwd": _identifier, "user_message_uuids?": _optional(_ids)},
    "session_state_changed": _session | {"state": lambda s: s in ("idle", "running", "requires_action")},
    "background_tasks_changed": _session | {"tasks": _membership},
    "task_started": _task | {"tool_use_id": _optional(_identifier)},
    "task_updated": _session | {"task_id": _identifier, "patch": lambda p: isinstance(p, dict)
                                and ("status" not in p or _identifier(p["status"]))},
    "task_notification": _session | {"task_id": _identifier, "status": _identifier},
    "assistant": _session | _human | {"parent_tool_use_id": _optional(_identifier), "content": _content},
    "stream": _session | _human | {"parent_tool_use_id": _optional(_identifier)},
    "result": _session | _human | {"uuid": _identifier, "subtype": _identifier, "is_error": lambda x: type(x) is bool,
                                   "terminal_reason": _optional(_identifier), "result": _optional(lambda x: isinstance(x, str)),
                                   "origin": _optional(_origin)},
    "Stop": _session | {"session_crons": lambda items: isinstance(items, list)
                        and all(isinstance(c, dict) and _identifier(c.get("id")) for c in items)},
}


def validate_fields(kind: str, data: Any, session: str) -> None:
    for path, check in CONSUMED_FIELDS.get(kind, {}).items():
        key = path.removesuffix("?")
        if isinstance(data, dict):
            if key not in data and path.endswith("?"):
                continue
            value = data.get(key)
        else:
            value = getattr(data, key, None)
        if not check(value) or (key == "session_id" and value != session):
            raise ValueError("invalid Claude session metadata")


async def ordered_messages(client: ClaudeClient, session: str) -> AsyncGenerator[Message, None]:
    """One public stream. Cancellation of the SDK child is a reader failure."""
    messages = aiter(client.receive_messages())
    while True:
        try:
            message = await external_call(anext(messages))
        except StopAsyncIteration:
            return
        if isinstance(message, SystemMessage):
            if not _identifier(message.subtype) or not isinstance(message.data, dict):
                raise ValueError("invalid Claude system frame")
            validate_fields("system", message.data, session)
            validate_fields(message.subtype, message.data, session)
        elif isinstance(message, ResultMessage):
            validate_fields("result", message, session)
        elif isinstance(message, AssistantMessage):
            validate_fields("assistant", message, session)
        elif isinstance(message, StreamEvent):
            validate_fields("stream", message, session)
        yield message


@dataclass(slots=True)
class _PhysicalOwner:
    # A wake's tools (and their children) share this reference until its Result identifies the launcher.
    human: str | None = None


@dataclass(slots=True)
class LiveSession:
    """Latest CLI snapshots and the two native identity links for this connection."""
    session: str
    fail: Callable[[], None]
    state: str | None = None
    initialized: bool = False
    physical: str | None = None
    init_human: str | None = None
    reported_human: str | None = None
    ordered_idle: bool = False
    awaiting_boundary: bool = False
    crons: bool = False
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    tasks: dict[str, str] = field(default_factory=dict)
    stop_requested: set[str] = field(default_factory=set)
    stopped: set[str] = field(default_factory=set)
    owner: _PhysicalOwner = field(default_factory=_PhysicalOwner)
    tool_owner: dict[str, _PhysicalOwner] = field(default_factory=dict)
    run_tool: dict[str, str] = field(default_factory=dict)
    seen: set[str] = field(default_factory=set)

    async def stop_hook(self, data: Any, callback_id: Any, context: Any) -> HookJSONOutput:
        try:
            validate_fields("Stop", data, self.session)
            crons = bool(data["session_crons"])
            if self.crons != crons:
                self.crons = crons
                self.changed.set()
        except Exception:
            self.fail()
        return {}

    def system(self, frame: SystemMessage) -> None:
        data = frame.data
        if frame.subtype == "session_state_changed":
            self.state = data["state"]
            self.ordered_idle = self.state == "idle"
            self.awaiting_boundary = False
        elif frame.subtype == "background_tasks_changed":
            latest = {row["task_id"]: row["run_id"] for row in data["tasks"]}
            if any(latest[key] != self.tasks[key] for key in latest.keys() & self.tasks.keys()):
                raise ValueError("conflicting task run")
            removed = self.tasks.keys() - latest.keys()
            if removed - self.stopped:
                self.ordered_idle = False
                self.awaiting_boundary = True
            self.stopped.difference_update(removed)
            self.stop_requested.difference_update(removed)
            self.tasks = latest
        elif frame.subtype in ("task_updated", "task_notification"):
            status = data["patch"].get("status") if frame.subtype == "task_updated" else data["status"]
            if status in ("killed", "stopped") and data["task_id"] in self.stop_requested:
                self.stopped.add(data["task_id"])
        elif frame.subtype == "task_started" and data.get("tool_use_id"):
            if data["task_id"] in self.tasks and self.tasks[data["task_id"]] != data["run_id"]:
                raise ValueError("conflicting task run")
            if self.run_tool.setdefault(data["run_id"], data["tool_use_id"]) != data["tool_use_id"]:
                raise ValueError("conflicting task tool")

    def init(self, frame: SystemMessage) -> None:
        ids = frame.data.get("user_message_uuids")
        if ids and len(set(ids)) != 1:
            raise ValueError("conflicting init human UUIDs")
        self.init_human = ids[0] if ids else None
        self.reported_human = self.init_human
        self.owner = _PhysicalOwner(self.init_human)
        self.physical = frame.data["uuid"]
        self.awaiting_boundary = self.ordered_idle = False

    def assistant(self, frame: AssistantMessage) -> str | None:
        parent = frame.parent_tool_use_id
        declared = native_human(frame) if parent is None else None
        if declared:
            if self.reported_human and declared != self.reported_human:
                raise ValueError("conflicting physical input")
            self.reported_human = declared
        owner = self.owner if parent is None else self.tool_owner.get(parent)
        if owner is not None:
            for block in frame.content:
                if isinstance(block, ToolUseBlock) and self.tool_owner.setdefault(block.id, owner) is not owner:
                    raise ValueError("conflicting tool owner")
        return declared

    def route(self, frame: ResultMessage) -> tuple[str | None, bool]:
        human = native_human(frame)
        if self.reported_human and human and self.reported_human != human:
            raise ValueError("conflicting result owner")
        origin: Mapping[str, object] = frame.origin or {}
        wake = origin.get("kind") == "task-notification"
        run = origin.get("runId") if wake and origin.get("producer") == "session-task" else None
        launcher = self.tool_owner.get(self.run_tool.get(run, "")) if isinstance(run, str) else None
        if wake:
            self.owner.human = launcher.human if launcher else None
        owner = self.owner.human
        self.owner = _PhysicalOwner()
        self.physical = self.init_human = self.reported_human = None
        self.ordered_idle = False
        self.awaiting_boundary = True
        return human or owner, wake

    def has_work(self, owner: str) -> bool:
        return any(link.human == owner for run in self.tasks.values()
                   if (link := self.tool_owner.get(self.run_tool.get(run, ""))) is not None)

    def prune(self, humans: set[str]) -> None:
        self.tool_owner = {tool: h for tool, h in self.tool_owner.items() if h.human in humans}
        self.run_tool = {run: tool for run, tool in self.run_tool.items()
                         if tool in self.tool_owner or run in self.tasks.values()}


async def live_messages(client: ClaudeClient, live: LiveSession) -> AsyncGenerator[Message | None, None]:
    """Wake the same ordered reader for a concurrent Stop snapshot, even when idle."""
    frame: Message | None = None
    consumed = asyncio.Event()

    async def pump() -> None:
        nonlocal frame
        try:
            async for item in ordered_messages(client, live.session):
                frame = item
                live.changed.set()
                await consumed.wait()
                consumed.clear()
        finally:
            live.changed.set()

    reader = asyncio.create_task(pump())
    try:
        while True:
            await live.changed.wait()
            live.changed.clear()
            if reader.done():
                await reader
                return
            item, frame = frame, None
            yield item
            if item is not None:
                consumed.set()
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


def native_human(frame: AssistantMessage | ResultMessage | StreamEvent) -> str | None:
    ids = frame.user_message_uuids or []
    if len(set(ids)) > 1 or (ids and frame.user_message_uuid and frame.user_message_uuid != ids[0]):
        raise ValueError("conflicting human UUIDs")
    return frame.user_message_uuid or (ids[0] if ids else None)


class ClaudeClients(Protocol):
    def create(self, session_id: str, cwd: Path, *, resume: bool, live: LiveSession | None = None) -> ClaudeClient: ...
    async def session_cwd(self, session_id: str, candidates: tuple[Path, ...] = ()) -> Path | None: ...


class BundledClaudeClients:
    def __init__(self) -> None:
        self._cli = verified_claude_cli()
        if (claude_installation.SDK_VERSION, claude_installation.CLI_VERSION) != ("0.2.186", "2.1.329"):
            raise RuntimeError("Claude 后台会话适配需要重新验证 SDK 和 CLI 版本。")

    def create(self, session_id: str, cwd: Path, *, resume: bool, live: LiveSession | None = None) -> ClaudeClient:
        return ClaudeSDKClient(options=ClaudeAgentOptions(
            cli_path=self._cli, cwd=cwd, session_id=None if resume else session_id,
            resume=session_id if resume else None, permission_mode="auto", verbatim_prompts=True,
            env={"CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS": "1"} if live else {},
            hooks={"Stop": [HookMatcher(hooks=[live.stop_hook])]} if live else None,
            system_prompt={"type": "preset", "preset": "claude_code"},
        ))

    async def session_cwd(self, session_id: str, candidates: tuple[Path, ...] = ()) -> Path | None:
        return await asyncio.to_thread(self._lookup_cwd, session_id, candidates)

    @staticmethod
    def _lookup_cwd(session_id: str, candidates: tuple[Path, ...]) -> Path | None:
        # The public message reader searches sibling worktrees and its messages
        # carry no source cwd. Only a native summary with an exact cwd can resume.
        info = get_session_info(session_id)
        if info is None or info.session_id != session_id or not info.cwd:
            return None
        cwd = Path(info.cwd)
        if not cwd.is_absolute() or not cwd.is_dir():
            return None
        resolved = cwd.resolve(strict=True)
        if candidates and not any(path.is_absolute() and path.resolve() == resolved for path in candidates):
            return None
        return resolved


def user_message(prompt: PreparedPrompt, input_id: str) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = []
    for image in prompt.images:
        header, separator, data = image.data_url.partition(",")
        media = header.removeprefix("data:").removesuffix(";base64")
        if (not separator or not data or header != f"data:{media};base64"
                or media not in {"image/png", "image/jpeg", "image/webp", "image/gif"}):
            raise OrdinaryInputRejected("图片格式无法提交，本条输入未发送。")
        blocks.append({"type": "text", "text": image.label})
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": data}})
    if prompt.text:
        blocks.append({"type": "text", "text": prompt.text})
    return {"type": "user", "uuid": input_id, "session_id": "default", "parent_tool_use_id": None,
            "message": {"role": "user", "content": blocks}}


async def single_message(message: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    yield message
