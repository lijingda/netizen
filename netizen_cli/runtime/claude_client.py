"""Public Claude SDK boundary for an ordinary execution."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, AsyncIterator, Callable
from pathlib import Path
from typing import Any, Protocol

from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ContentBlock, Message, ResultMessage,
    StreamEvent, SystemMessage, ToolUseBlock, get_session_info,
)

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
    "init": _session | {"cwd": _identifier, "user_message_uuids?": _optional(_ids)},
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
    for field, check in CONSUMED_FIELDS.get(kind, {}).items():
        key = field.removesuffix("?")
        if isinstance(data, dict):
            if key not in data and field.endswith("?"):
                continue
            value = data.get(key)
        else:
            value = getattr(data, key, None)
        if not check(value) or (key == "session_id" and value != session):
            raise ValueError("invalid Claude session metadata")


async def ordered_messages(client: ClaudeClient, session: str) -> AsyncIterator[Message]:
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


class ClaudeClients(Protocol):
    def create(self, session_id: str, cwd: Path, *, resume: bool) -> ClaudeClient: ...
    async def session_cwd(self, session_id: str, candidates: tuple[Path, ...] = ()) -> Path | None: ...


class BundledClaudeClients:
    def __init__(self) -> None:
        self._cli = verified_claude_cli()

    def create(self, session_id: str, cwd: Path, *, resume: bool) -> ClaudeClient:
        return ClaudeSDKClient(options=ClaudeAgentOptions(
            cli_path=self._cli, cwd=cwd, session_id=None if resume else session_id,
            resume=session_id if resume else None, permission_mode="auto", verbatim_prompts=True,
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
