"""Process-owned authenticated MCP transport shared by Netizen tool adapters."""
from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import socket
from collections.abc import Awaitable, Callable
from typing import Any

import uvicorn
from mcp.server import Server
from mcp.server.transport_security import TransportSecuritySettings

MAX_BODY_BYTES = 65536


def native_thread_id(meta: dict[str, Any] | None) -> str | None:
    """Read only native per-call metadata, never arguments or connection state."""
    if meta is None:
        return None
    if not isinstance(meta, dict):
        raise ValueError("Invalid native call metadata")
    thread_id = meta.get("threadId")
    other_id = _native_turn_metadata(meta).get("thread_id")
    for value in (thread_id, other_id):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError("Invalid native Thread ID")
    if thread_id is not None and other_id is not None and thread_id != other_id:
        raise ValueError("Conflicting native Thread IDs")
    # The second metadata form is a cross-check, not an alternative source.
    return thread_id


def native_turn_id(meta: dict[str, Any] | None) -> str | None:
    """Read the physical Turn from native per-call metadata, with Thread cross-checks."""
    native_thread_id(meta)
    turn_id = _native_turn_metadata(meta).get("turn_id")
    if turn_id is not None and (not isinstance(turn_id, str) or not turn_id.strip()):
        raise ValueError("Invalid native Turn ID")
    return turn_id


def native_subagent_kind(meta: dict[str, Any] | None) -> str | None:
    """Read Codex's caller classification, never a model-supplied argument."""
    kind = _native_turn_metadata(meta).get("subagent_kind")
    if kind is not None and (not isinstance(kind, str) or not kind.strip()):
        raise ValueError("Invalid native subagent kind")
    return kind


def _native_turn_metadata(meta: dict[str, Any] | None) -> dict[str, Any]:
    if meta is None:
        return {}
    turn_metadata = meta.get("x-codex-turn-metadata")
    if isinstance(turn_metadata, str):
        turn_metadata = json.loads(turn_metadata)
    if turn_metadata is not None and not isinstance(turn_metadata, dict):
        raise ValueError("Invalid native turn metadata")
    return turn_metadata or {}


class _EmbeddedServer(uvicorn.Server):
    def capture_signals(self):
        # The Netizen process already owns signal handling and shutdown.
        return contextlib.nullcontext()


class EmbeddedMcpRunner:
    """Bind before AsyncCodex creation; open management only after app readiness.

    ``drain`` accepts an absolute event-loop deadline. ``close`` is idempotent
    and also closes the listener with bounded ASGI lifespan shutdown.
    """

    def __init__(self, *, name: str, namespace_prefix: str, env_prefix: str,
                 instructions: str, tool_names: tuple[str, ...]) -> None:
        nonce = secrets.token_hex(12)
        self.namespace = f"{namespace_prefix}_{nonce}"
        self._env_key = f"{env_prefix}_{nonce.upper()}"
        self._token = secrets.token_urlsafe(32)
        self._tool_names = tool_names
        self._callback: Callable[..., Awaitable[dict[str, Any]]] | None = None
        self._admission = False
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._http: _EmbeddedServer | None = None
        self._server_task: asyncio.Task[None] | None = None
        self._calls: set[asyncio.Task[Any]] = set()
        self._url: str | None = None
        self._mcp = Server(name, instructions=instructions,
                           on_list_tools=self._list_tools, on_call_tool=self._call_tool)

    @property
    def _http_timeout(self) -> float:
        return 20.0

    @property
    def url(self) -> str:
        if self._url is None:
            raise RuntimeError("Embedded MCP listener has not been bound")
        return self._url

    @property
    def config_overrides(self) -> tuple[str, ...]:
        # A single owned table entry cannot replace the user's mcp_servers table.
        entry = (
            f'{{url={json.dumps(self.url)}, '
            f'bearer_token_env_var={json.dumps(self._env_key)}, '
            f'enabled=true, enabled_tools={json.dumps(self._tool_names)}}}'
        )
        return (f"mcp_servers.{self.namespace}={entry}",)

    @property
    def app_server_env(self) -> dict[str, str]:
        return {self._env_key: self._token}

    def attach(self, callback: Callable[..., Awaitable[dict[str, Any]]]) -> None:
        if self._closed or self._callback is not None:
            raise RuntimeError("Embedded MCP callback is already attached or closed")
        if not callable(callback):
            raise TypeError("MCP callback must be callable")
        self._callback = callback

    def open_admission(self) -> None:
        self._assert_loop()
        if self._closed or self._callback is None or self._server_task is None or self._server_task.done():
            raise RuntimeError("Embedded MCP is not ready")
        self._admission = True

    def close_admission(self) -> None:
        self._admission = False

    async def bind(self) -> None:
        if self._closed or self._server_task is not None:
            raise RuntimeError("Embedded MCP listener cannot be bound twice")
        self._loop = asyncio.get_running_loop()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setblocking(False)
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            self._url = f"http://127.0.0.1:{port}/mcp"
            app = self._mcp.streamable_http_app(
                json_response=True,
                stateless_http=True,
                max_request_body_size=MAX_BODY_BYTES,
                transport_security=TransportSecuritySettings(
                    allowed_hosts=[f"127.0.0.1:{port}"],
                    allowed_origins=[],
                ),
            )
            self._http = _EmbeddedServer(uvicorn.Config(
                self._guard(app), host="127.0.0.1", port=port,
                loop="asyncio", http="h11", ws="none", lifespan="on",
                log_config=None, access_log=False, proxy_headers=False,
                server_header=False, limit_concurrency=32,
                h11_max_incomplete_event_size=32768,
                timeout_keep_alive=5, timeout_graceful_shutdown=5,
            ))
            self._server_task = asyncio.create_task(self._serve(listener), name=self.namespace)
            async with asyncio.timeout(10):
                while not self._http.started:
                    if self._server_task.done():
                        await self._server_task
                        raise RuntimeError("Embedded MCP listener did not start")
                    await asyncio.sleep(0.01)
        except BaseException:
            listener.close()
            await self.close()
            raise

    async def _serve(self, listener: socket.socket) -> None:
        assert self._http is not None
        try:
            await self._http.serve(sockets=[listener])
        except SystemExit as exc:
            raise RuntimeError("Embedded MCP listener failed to start") from exc
        finally:
            listener.close()

    async def drain(self, deadline: float) -> bool:
        self._assert_loop()
        if not self._calls:
            return True
        _, pending = await asyncio.wait(self._calls, timeout=max(0, deadline - self._loop.time()))
        return not pending

    async def close(self) -> None:
        self.close_admission()
        self._closed = True
        if self._http is not None:
            self._http.should_exit = True
            for listener in getattr(self._http, "servers", ()):
                listener.close()
        task = self._server_task
        if task is not None and not task.done():
            try:
                async with asyncio.timeout(7):
                    await asyncio.shield(task)
            except (TimeoutError, asyncio.CancelledError) as exc:
                # An outer shutdown budget may expire before Uvicorn's own
                # graceful deadline. Shielding the server must not leave
                # management callbacks accessing a subsequently closed Store.
                pending = {task, *self._calls}
                for active in pending:
                    active.cancel()
                await asyncio.wait(pending, timeout=0.1)
                if isinstance(exc, asyncio.CancelledError):
                    raise

    def _assert_loop(self) -> None:
        if self._loop is None or asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("Embedded MCP must run on its owning event loop")

    def _guard(self, app):
        async def guarded(scope, receive, send):
            if scope["type"] != "http":
                await app(scope, receive, send)
                return
            headers = scope.get("headers", [])
            authorization = [value for key, value in headers if key.lower() == b"authorization"]
            expected = f"Bearer {self._token}".encode("ascii")
            if len(authorization) != 1 or not secrets.compare_digest(authorization[0], expected):
                await self._http_error(send, 401, "unauthorized")
                return
            # No browser client is part of this private process endpoint.
            if any(key.lower() == b"origin" for key, _ in headers):
                await self._http_error(send, 403, "origin_not_allowed")
                return
            response_started = False

            async def tracked_send(message):
                nonlocal response_started
                if message["type"] == "http.response.start":
                    response_started = True
                await send(message)

            try:
                async with asyncio.timeout(self._http_timeout):
                    await app(scope, receive, tracked_send)
            except TimeoutError:
                if not response_started:
                    await self._http_error(send, 408, "request_timeout")

        return guarded

    @staticmethod
    async def _http_error(send, status: int, code: str) -> None:
        body = json.dumps({"error": code}).encode("ascii")
        await send({"type": "http.response.start", "status": status, "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]})
        await send({"type": "http.response.body", "body": body})
