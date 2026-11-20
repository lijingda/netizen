"""Public Claude SDK boundary for an ordinary execution."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, AsyncIterator
from pathlib import Path
from typing import Any, Protocol

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, Message, get_session_info, get_session_messages

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
        ))

    async def session_cwd(self, session_id: str, candidates: tuple[Path, ...] = ()) -> Path | None:
        return await asyncio.to_thread(self._lookup_cwd, session_id, candidates)

    @staticmethod
    def _lookup_cwd(session_id: str, candidates: tuple[Path, ...]) -> Path | None:
        info = get_session_info(session_id)
        if info is not None and info.session_id != session_id:
            return None
        if info is not None and info.cwd:
            cwd = Path(info.cwd)
            if not cwd.is_absolute() or not cwd.is_dir():
                return None
            resolved = cwd.resolve(strict=True)
            if candidates and not any(path.is_absolute() and path.resolve() == resolved for path in candidates):
                return None
            return resolved

        # Large first messages can exceed the SDK's summary window. Only
        # positive evidence for the exact ID can authorize a scoped resume.
        matches = []
        if any(not path.is_absolute() or not path.is_dir() for path in candidates):
            return None
        for candidate in dict.fromkeys(path.resolve(strict=True) for path in candidates):
            messages = get_session_messages(session_id, directory=str(candidate), limit=1)
            if messages:
                if messages[0].session_id != session_id:
                    return None
                matches.append(candidate)
        return matches[0] if len(matches) == 1 else None


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
