"""Public Claude SDK boundary for an ordinary execution."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, AsyncIterator
from pathlib import Path
from typing import Any, Protocol

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, Message, get_session_info

from ..claude_installation import verified_claude_cli
from ..ordinary_sessions import OrdinaryInputRejected, PreparedPrompt


class ClaudeClient(Protocol):
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def interrupt(self) -> None: ...
    async def query(self, prompt: AsyncIterable[dict[str, Any]]) -> None: ...
    def receive_messages(self) -> AsyncIterator[Message]: ...


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
