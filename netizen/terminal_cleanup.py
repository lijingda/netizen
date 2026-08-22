"""Version-gated access to fixed background-terminal App Server methods.

This module is intentionally the only place for the experimental terminal
inspection/cleanup compatibility contract.  It is not a general JSON-RPC adapter: the
method name, request shape, response shape, SDK version, and relevant SDK
implementation fingerprints are all fixed here.  ADR 0014's independently
removable Goal/Skills facade-gap adapters do not weaken this gate.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol

import openai_codex
from openai_codex import AsyncCodex
from openai_codex.async_client import AsyncCodexClient
from openai_codex.client import CodexClient
from pydantic import BaseModel, ConfigDict, Field


SUPPORTED_SDK_VERSION = "0.147.0"
_CLEAN_METHOD = "thread/backgroundTerminals/clean"
_LIST_METHOD = "thread/backgroundTerminals/list"
_PACKAGE_SOURCE_FINGERPRINT = (
    "35ec9419cb9f42577080f9bf410e81cb5a97ae64e5297c4302878c73749d39eb"
)


class UnsupportedCleanupSdk(RuntimeError):
    """The installed SDK no longer matches the explicitly approved shim."""


class TerminalCleanup(Protocol):
    """Request cleanup of App Server-registered background terminals only."""

    async def clean_thread(self, thread_id: str) -> None: ...


class BackgroundTerminalInspector(Protocol):
    """Report only whether App Server has a registered terminal for a Thread."""

    async def has_running(self, thread_id: str) -> bool: ...


class _CleanupResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _BackgroundTerminalListResponse(BaseModel):
    """Minimal envelope; terminal identities never leave this adapter."""

    model_config = ConfigDict(extra="forbid")

    data: tuple[dict[str, object], ...]
    next_cursor: str | None = Field(default=None, alias="nextCursor")


class PinnedExperimentalTerminalCleanup:
    """Inspect or clean registered background terminals for one exact Thread.

    Construction validates the private ownership edge before the service can
    accept a Turn.  The underlying low-level client's typed ``request`` method
    is public, but the high-level ``AsyncCodex._client`` reference is not; that
    single reach-through is the narrow, temporary compatibility shim approved
    in ADR 0009.  A successful empty response attests only that the request was
    accepted; it does not attest that a foreground tool process exited (ADR
    0010).
    """

    __slots__ = ("_client",)

    def __init__(self, codex: AsyncCodex) -> None:
        _validate_contract(codex)
        self._client = codex._client

    async def clean_thread(self, thread_id: str) -> None:
        _validate_thread_id(thread_id)
        await self._client.request(
            _CLEAN_METHOD,
            {"threadId": thread_id},
            response_model=_CleanupResponse,
        )

    async def has_running(self, thread_id: str) -> bool:
        """Return a presence bit without exposing process metadata to Runtime."""

        _validate_thread_id(thread_id)
        response = await self._client.request(
            _LIST_METHOD,
            {"threadId": thread_id, "limit": 1},
            response_model=_BackgroundTerminalListResponse,
        )
        # A non-empty cursor with no first-page item is an unexpected shape;
        # conservatively report presence so Runtime cannot unsubscribe.
        return bool(response.data or response.next_cursor)


def _validate_thread_id(thread_id: str) -> None:
    if not thread_id or thread_id.strip() != thread_id:
        raise ValueError("native Thread ID must be a non-empty trimmed string")


def _validate_contract(codex: AsyncCodex) -> None:
    if openai_codex.__version__ != SUPPORTED_SDK_VERSION:
        raise UnsupportedCleanupSdk(
            "experimental terminal cleanup supports only openai-codex=="
            f"{SUPPORTED_SDK_VERSION}; found {openai_codex.__version__}"
        )
    if type(codex) is not AsyncCodex:
        raise UnsupportedCleanupSdk("AsyncCodex implementation type changed")
    if getattr(codex, "_initialized", False) is not True:
        raise UnsupportedCleanupSdk(
            "AsyncCodex must be initialized before terminal cleanup is enabled"
        )

    client = getattr(codex, "_client", None)
    if type(client) is not AsyncCodexClient:
        raise UnsupportedCleanupSdk("AsyncCodex private client shape changed")
    sync_client = getattr(client, "_sync", None)
    if type(sync_client) is not CodexClient:
        raise UnsupportedCleanupSdk("AsyncCodexClient private sync client shape changed")
    if getattr(getattr(sync_client, "config", None), "experimental_api", None) is not True:
        raise UnsupportedCleanupSdk(
            "Codex App Server experimentalApi capability is not enabled"
        )

    package_file = getattr(openai_codex, "__file__", None)
    if not isinstance(package_file, str) or not package_file.endswith(".py"):
        raise UnsupportedCleanupSdk("cannot locate SDK source package")
    try:
        actual = _source_tree_fingerprint(Path(package_file).resolve().parent)
    except OSError as error:
        raise UnsupportedCleanupSdk("cannot read SDK source package") from error
    if actual != _PACKAGE_SOURCE_FINGERPRINT:
        raise UnsupportedCleanupSdk("SDK package source fingerprint changed")


def _source_tree_fingerprint(package_root: Path) -> str:
    source_files = sorted(
        (path for path in package_root.rglob("*.py") if path.is_file()),
        key=lambda path: path.relative_to(package_root).as_posix(),
    )
    if not source_files:
        raise OSError("SDK source package contains no Python files")
    digest = hashlib.sha256()
    for source_path in source_files:
        relative = source_path.relative_to(package_root).as_posix().encode("utf-8")
        source = source_path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(source).to_bytes(8, "big"))
        digest.update(source)
    return digest.hexdigest()
