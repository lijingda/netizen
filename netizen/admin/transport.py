"""Bounded HTTP/1.1 transport for the in-process Admin Web.

This module deliberately stops at the transport boundary.  Authentication,
routing, and business operations belong to the Admin Web application layer.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Final, TypeAlias

import h11


logger = logging.getLogger(__name__)


MAX_ACTIVE_CONNECTIONS: Final = 32
HEADER_TIMEOUT_SECONDS: Final = 5.0
BODY_TIMEOUT_SECONDS: Final = 5.0
KEEPALIVE_TIMEOUT_SECONDS: Final = 15.0
MAX_REQUEST_OR_HEADER_LINE_BYTES: Final = 8192
MAX_HEADER_SECTION_BYTES: Final = 32768
MAX_BODY_BYTES: Final = 65536

_READ_CHUNK_BYTES: Final = 16384
_DRAIN_CANCELLATION_GRACE_SECONDS: Final = 0.05
_HEADER_TERMINATOR: Final = b"\r\n\r\n"
_RESERVED_RESPONSE_HEADERS: Final = frozenset(
    {b"connection", b"content-length", b"transfer-encoding"}
)

Address: TypeAlias = tuple[object, ...] | str | None
RawHeaders: TypeAlias = tuple[tuple[bytes, bytes], ...]
Handler: TypeAlias = Callable[["Request"], Awaitable["Response"]]


@dataclass(frozen=True, slots=True)
class Request:
    """One fully buffered, parser-validated HTTP request."""

    method: bytes
    target: bytes
    http_version: bytes
    raw_headers: RawHeaders
    body: bytes
    peer: Address
    sockname: Address
    request_id: str

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.method, "method"),
            (self.target, "target"),
            (self.http_version, "http_version"),
            (self.body, "body"),
        ):
            if not isinstance(value, bytes):
                raise TypeError(f"request {field_name} must be bytes")
        if not isinstance(self.request_id, str):
            raise TypeError("request_id must be a string")
        object.__setattr__(self, "raw_headers", _freeze_headers(self.raw_headers))
        object.__setattr__(self, "peer", _immutable_address(self.peer))
        object.__setattr__(self, "sockname", _immutable_address(self.sockname))

    def header_values(self, name: bytes) -> tuple[bytes, ...]:
        lowered = name.lower()
        return tuple(
            value for key, value in self.raw_headers if key.lower() == lowered
        )


@dataclass(frozen=True, slots=True)
class Response:
    """A complete response returned by an Admin HTTP handler."""

    status: int
    headers: RawHeaders = field(default_factory=tuple)
    body: bytes = b""

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", _freeze_headers(self.headers))
        if not isinstance(self.body, bytes):
            raise TypeError("response body must be bytes")


class AdminHttpState(str, Enum):
    NEW = "new"
    BOUND_CLOSED = "bound_closed"
    CLOSED = "closed"


class _RequestRejected(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


class _ClientDisconnected(Exception):
    pass


@dataclass(slots=True, eq=False)
class _Connection:
    writer: asyncio.StreamWriter
    accepted_at: float
    task: asyncio.Task[None] | None = None
    handler_task: asyncio.Task[Response] | None = None


class AdminHttpTransport:
    """A loop-owned, bounded HTTP transport with explicit admission control.

    ``bind`` reserves the listener but leaves the transport in
    :attr:`AdminHttpState.BOUND_CLOSED`.  Call ``open_admission`` only after
    the surrounding application is ready.  Shutdown is intentionally split:
    ``close`` closes admission and the listener first, while ``drain`` waits
    for tracked connection/handler tasks until an absolute monotonic deadline.
    """

    def __init__(self, host: str, port: int, handler: Handler) -> None:
        if not isinstance(host, str) or not host:
            raise ValueError("host must be a non-empty string")
        if not isinstance(port, int) or isinstance(port, bool):
            raise TypeError("port must be an integer")
        if not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        if not callable(handler):
            raise TypeError("handler must be callable")

        self._host = host
        self._port = port
        self._handler = handler
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.AbstractServer | None = None
        self._closed_servers: list[asyncio.AbstractServer] = []
        self._state = AdminHttpState.NEW
        self._admission_open = False
        self._closing = False
        self._connections: set[_Connection] = set()
        self._connection_tasks: set[asyncio.Task[None]] = set()
        self._handler_tasks: set[asyncio.Task[Response]] = set()

    @property
    def state(self) -> AdminHttpState:
        return self._state

    @property
    def admission_open(self) -> bool:
        return self._admission_open

    @property
    def active_connection_count(self) -> int:
        return len(self._connections)

    @property
    def active_handler_count(self) -> int:
        return len(self._handler_tasks)

    @property
    def addresses(self) -> tuple[Address, ...]:
        server = self._server
        if server is None or server.sockets is None:
            return ()
        return tuple(sock.getsockname() for sock in server.sockets)

    async def bind(self) -> None:
        """Bind the listener on the current loop with admission still closed."""

        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        self._assert_loop()
        if self._state is not AdminHttpState.NEW:
            raise RuntimeError("Admin HTTP transport can only be bound once")

        try:
            server = await asyncio.start_server(
                self._accepted,
                self._host,
                self._port,
                start_serving=True,
                limit=MAX_HEADER_SECTION_BYTES + MAX_BODY_BYTES + 1,
                reuse_port=False,
            )
        except BaseException:
            self._state = AdminHttpState.CLOSED
            self._closing = True
            raise

        self._server = server
        self._state = AdminHttpState.BOUND_CLOSED

    def open_admission(self) -> None:
        self._assert_loop()
        if self._state is not AdminHttpState.BOUND_CLOSED or self._closing:
            raise RuntimeError("Admin HTTP listener is not available")
        self._admission_open = True

    def close_admission(self) -> None:
        self._assert_loop()
        self._admission_open = False

    async def close(self) -> None:
        """Idempotently close admission and the listener, but not handlers."""

        self._assert_loop()
        if self._state is AdminHttpState.CLOSED:
            return

        self._admission_open = False
        self._closing = True
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            # Python 3.12's wait_closed() also waits for accepted clients.
            # Retain the server and defer that wait to drain(), so close()
            # remains the listener-first half of shutdown.
            self._closed_servers.append(server)
        self._state = AdminHttpState.CLOSED

        # Once the listener is closed, sockets which are not executing a
        # handler cannot contribute useful shutdown work.  Active handlers get
        # the drain budget and close their socket when they finish.
        for connection in tuple(self._connections):
            if connection.handler_task is None:
                connection.writer.close()

    async def drain(self, deadline: float) -> None:
        """Drain tracked work until an absolute ``loop.time()`` deadline.

        The listener is always closed before waiting.  At the deadline all
        remaining transport-owned work is cancelled and every socket is
        closed.  Application handlers which shield independently tracked
        native mutations may continue their application-owned work.
        """

        self._assert_loop()
        if not isinstance(deadline, (int, float)) or isinstance(deadline, bool):
            raise TypeError("deadline must be a monotonic timestamp")
        await self.close()

        tasks = self._all_tasks()
        remaining = max(0.0, deadline - self._owned_loop().time())
        if tasks and remaining > 0:
            cancellation_grace = min(
                _DRAIN_CANCELLATION_GRACE_SECONDS,
                remaining,
            )
            _done, pending = await asyncio.wait(
                tasks,
                timeout=max(0.0, remaining - cancellation_grace),
            )
        else:
            pending = tasks

        for connection in tuple(self._connections):
            connection.writer.close()
        for task in pending:
            task.cancel()
        if pending:
            # Give cooperative cancellation a chance without allowing an
            # application handler which suppresses cancellation to overrun
            # the caller's absolute deadline.
            await asyncio.sleep(0)
            remaining = max(0.0, deadline - self._owned_loop().time())
            if remaining > 0:
                await asyncio.wait(pending, timeout=remaining)
        remaining = max(0.0, deadline - self._owned_loop().time())
        if remaining > 0:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._wait_writers_closed(),
                    timeout=remaining,
                )
        remaining = max(0.0, deadline - self._owned_loop().time())
        servers, self._closed_servers = self._closed_servers, []
        if servers and remaining > 0:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*(server.wait_closed() for server in servers)),
                    timeout=remaining,
                )

    def _assert_loop(self) -> None:
        loop = self._loop
        if loop is None:
            raise RuntimeError("Admin HTTP transport is not bound")
        try:
            running = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise RuntimeError(
                "Admin HTTP lifecycle requires its creator event loop"
            ) from exc
        if running is not loop:
            raise RuntimeError(
                "Admin HTTP lifecycle requires its creator event loop"
            )

    def _owned_loop(self) -> asyncio.AbstractEventLoop:
        loop = self._loop
        assert loop is not None
        return loop

    def _accepted(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        # The callback runs synchronously on the owner loop.  Reserving the
        # slot before creating the task prevents simultaneous accepts from
        # exceeding the application-visible cap.
        if (
            not self._admission_open
            or self._closing
            or len(self._connections) >= MAX_ACTIVE_CONNECTIONS
        ):
            # Do not let a flood above the admitted-connection cap create an
            # unbounded task set.  StreamWriter.close() flushes the already
            # buffered fixed response while preventing further reads.
            with contextlib.suppress(ConnectionError, OSError, RuntimeError):
                writer.write(_minimal_response_bytes(503, b"service unavailable"))
            writer.close()
            return

        connection = _Connection(
            writer=writer,
            accepted_at=self._owned_loop().time(),
        )
        self._connections.add(connection)
        task = self._owned_loop().create_task(
            self._serve_connection(connection, reader)
        )
        connection.task = task
        self._track_task(task, self._connection_tasks)

    async def _serve_connection(
        self,
        connection: _Connection,
        reader: asyncio.StreamReader,
    ) -> None:
        writer = connection.writer
        peer = _immutable_address(writer.get_extra_info("peername"))
        sockname = _immutable_address(writer.get_extra_info("sockname"))
        parser = h11.Connection(
            h11.SERVER,
            max_incomplete_event_size=MAX_HEADER_SECTION_BYTES,
        )
        pending = b""
        first_request = True

        try:
            while not self._closing:
                request_id = secrets.token_hex(16)
                try:
                    request, pending = await self._read_request(
                        reader=reader,
                        parser=parser,
                        pending=pending,
                        first_request=first_request,
                        accepted_at=connection.accepted_at,
                        peer=peer,
                        sockname=sockname,
                        request_id=request_id,
                    )
                except _ClientDisconnected:
                    return
                except asyncio.TimeoutError:
                    await self._safe_error(
                        parser, writer, 408, b"request timeout", request_id
                    )
                    return
                except _RequestRejected as exc:
                    self._log_rejection(request_id, exc.code)
                    await self._safe_error(
                        parser,
                        writer,
                        exc.status,
                        _status_body(exc.status),
                        request_id,
                    )
                    return
                except (h11.RemoteProtocolError, h11.LocalProtocolError):
                    self._log_rejection(request_id, "protocol_error")
                    await self._safe_error(
                        parser, writer, 400, b"bad request", request_id
                    )
                    return

                first_request = False
                if self._closing:
                    return
                if not self._admission_open:
                    await self._safe_error(
                        parser,
                        writer,
                        503,
                        b"service unavailable",
                        request_id,
                    )
                    return
                try:
                    await _reject_immediately_buffered_pipeline(reader)
                except _ClientDisconnected:
                    return
                except _RequestRejected as exc:
                    self._log_rejection(request_id, exc.code)
                    await self._safe_error(
                        parser,
                        writer,
                        exc.status,
                        _status_body(exc.status),
                        request_id,
                    )
                    return
                force_close = _must_close_request(request)
                handler_task = self._owned_loop().create_task(self._handler(request))
                connection.handler_task = handler_task
                self._track_task(handler_task, self._handler_tasks)
                watcher = self._owned_loop().create_task(reader.read(1))

                done, _pending_tasks = await asyncio.wait(
                    {handler_task, watcher},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if watcher in done:
                    extra = watcher.result()
                    if not handler_task.done():
                        handler_task.cancel()
                    await asyncio.gather(handler_task, return_exceptions=True)
                    connection.handler_task = None
                    if extra:
                        self._log_rejection(request_id, "pipelined_request")
                        await self._safe_error(
                            parser, writer, 400, b"bad request", request_id
                        )
                    return

                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
                connection.handler_task = None
                try:
                    response = handler_task.result()
                    await self._write_response(
                        parser,
                        writer,
                        response,
                        force_close=force_close or self._closing,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Never render exception strings or tracebacks into either
                    # the response or logs; handler inputs can reach both.
                    logger.error(
                        "admin HTTP handler failed request_id=%s",
                        request_id,
                    )
                    await self._safe_error(
                        parser, writer, 500, b"internal server error", request_id
                    )
                    return

                if force_close or self._closing:
                    return
                try:
                    parser.start_next_cycle()
                except h11.LocalProtocolError:
                    return
                pending = b""
        except asyncio.CancelledError:
            handler = connection.handler_task
            if handler is not None and not handler.done():
                handler.cancel()
                await asyncio.gather(handler, return_exceptions=True)
            raise
        except (ConnectionError, OSError, h11.LocalProtocolError):
            return
        finally:
            connection.handler_task = None
            self._connections.discard(connection)
            await _close_writer(writer)

    async def _read_request(
        self,
        *,
        reader: asyncio.StreamReader,
        parser: h11.Connection,
        pending: bytes,
        first_request: bool,
        accepted_at: float,
        peer: Address,
        sockname: Address,
        request_id: str,
    ) -> tuple[Request, bytes]:
        loop = self._owned_loop()

        if first_request:
            header_deadline = accepted_at + HEADER_TIMEOUT_SECONDS
        elif pending:
            header_deadline = loop.time() + HEADER_TIMEOUT_SECONDS
        else:
            try:
                first_byte = await asyncio.wait_for(
                    reader.read(1), timeout=KEEPALIVE_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError as exc:
                raise _ClientDisconnected from exc
            if not first_byte:
                raise _ClientDisconnected
            pending = first_byte
            header_deadline = loop.time() + HEADER_TIMEOUT_SECONDS

        header, pending = await self._read_header_section(
            reader, pending, header_deadline
        )
        _validate_raw_header_section(header)

        parser.receive_data(header)
        event = parser.next_event()
        if not isinstance(event, h11.Request):
            raise _RequestRejected(400, "missing_request")
        _validate_request_event(event)

        content_length = _content_length(event)
        if content_length > MAX_BODY_BYTES:
            raise _RequestRejected(413, "body_too_large")
        if event.method != b"POST" and content_length:
            raise _RequestRejected(400, "body_not_allowed")

        body = bytearray()
        body_deadline = loop.time() + BODY_TIMEOUT_SECONDS
        while True:
            next_event = parser.next_event()
            if isinstance(next_event, h11.Data):
                body.extend(next_event.data)
                if len(body) > MAX_BODY_BYTES:
                    raise _RequestRejected(413, "body_too_large")
                continue
            if isinstance(next_event, h11.EndOfMessage):
                break
            if next_event is h11.PAUSED:
                raise _RequestRejected(400, "pipelined_request")
            if next_event is not h11.NEED_DATA:
                raise _RequestRejected(400, "invalid_request_event")

            if pending:
                data, pending = pending, b""
            else:
                data = await _read_before(reader, body_deadline)
                if not data:
                    raise _ClientDisconnected
            parser.receive_data(data)

        parser_pending, _closed = parser.trailing_data
        pending = parser_pending + pending
        if pending:
            # Any bytes following one complete request before its handler is
            # entered are pipelining, including a same-write second request.
            raise _RequestRejected(400, "pipelined_request")

        request = Request(
            method=event.method,
            target=event.target,
            http_version=event.http_version,
            raw_headers=tuple(event.headers.raw_items()),
            body=bytes(body),
            peer=peer,
            sockname=sockname,
            request_id=request_id,
        )
        return request, b""

    async def _read_header_section(
        self,
        reader: asyncio.StreamReader,
        pending: bytes,
        deadline: float,
    ) -> tuple[bytes, bytes]:
        collected = bytearray(pending)
        while True:
            terminator_at = collected.find(_HEADER_TERMINATOR)
            if terminator_at >= 0:
                end = terminator_at + len(_HEADER_TERMINATOR)
                if end > MAX_HEADER_SECTION_BYTES:
                    raise _RequestRejected(431, "headers_too_large")
                return bytes(collected[:end]), bytes(collected[end:])
            if len(collected) > MAX_HEADER_SECTION_BYTES:
                raise _RequestRejected(431, "headers_too_large")
            _validate_incomplete_header_lines(collected)
            data = await _read_before(reader, deadline)
            if not data:
                raise _ClientDisconnected
            collected.extend(data)

    async def _write_response(
        self,
        parser: h11.Connection,
        writer: asyncio.StreamWriter,
        response: Response,
        *,
        force_close: bool,
    ) -> None:
        if not isinstance(response, Response):
            raise TypeError("handler must return Response")
        if not 200 <= response.status <= 599:
            raise ValueError("response status is invalid")
        if response.status in {204, 304} and response.body:
            raise ValueError("response status does not permit a body")

        headers: list[tuple[bytes, bytes]] = []
        for name, value in response.headers:
            if not isinstance(name, bytes) or not isinstance(value, bytes):
                raise TypeError("response headers must be bytes pairs")
            if name.lower() in _RESERVED_RESPONSE_HEADERS:
                raise ValueError("response contains a transport-owned header")
            headers.append((name, value))
        headers.append((b"Content-Length", str(len(response.body)).encode("ascii")))
        if force_close:
            headers.append((b"Connection", b"close"))

        head = parser.send(
            h11.Response(status_code=response.status, headers=headers)
        )
        data = parser.send(h11.Data(data=response.body))
        end = parser.send(h11.EndOfMessage())
        writer.write(head + data + end)
        await writer.drain()

    async def _safe_error(
        self,
        parser: h11.Connection,
        writer: asyncio.StreamWriter,
        status: int,
        body: bytes,
        request_id: str,
    ) -> None:
        try:
            if parser.our_state is h11.IDLE:
                await self._write_response(
                    parser,
                    writer,
                    Response(status=status, body=body),
                    force_close=True,
                )
                return
        except (h11.ProtocolError, ConnectionError, OSError, RuntimeError):
            pass
        try:
            await self._write_minimal_response(writer, status, body)
        except (ConnectionError, OSError, RuntimeError):
            logger.info(
                "admin HTTP error response unavailable request_id=%s",
                request_id,
            )

    async def _write_minimal_response(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        body: bytes,
    ) -> None:
        writer.write(_minimal_response_bytes(status, body))
        await writer.drain()

    def _log_rejection(self, request_id: str, code: str) -> None:
        logger.info(
            "admin HTTP request rejected request_id=%s code=%s",
            request_id,
            code,
        )

    def _track_task(self, task: asyncio.Task, bucket: set) -> None:
        bucket.add(task)

        def finished(done: asyncio.Task) -> None:
            bucket.discard(done)
            if not done.cancelled():
                # Retrieve failures even when shutdown races with the normal
                # owner await, preventing unobserved-task diagnostics.
                done.exception()

        task.add_done_callback(finished)

    def _all_tasks(self) -> set[asyncio.Task]:
        return {
            *self._connection_tasks,
            *self._handler_tasks,
        }

    async def _wait_writers_closed(self) -> None:
        writers = [connection.writer for connection in tuple(self._connections)]
        if writers:
            await asyncio.gather(
                *(_wait_writer_closed(writer) for writer in writers),
                return_exceptions=True,
            )


async def _read_before(
    reader: asyncio.StreamReader,
    deadline: float,
) -> bytes:
    loop = asyncio.get_running_loop()
    remaining = deadline - loop.time()
    if remaining <= 0:
        raise asyncio.TimeoutError
    return await asyncio.wait_for(reader.read(_READ_CHUNK_BYTES), timeout=remaining)


async def _reject_immediately_buffered_pipeline(
    reader: asyncio.StreamReader,
) -> None:
    """Reject bytes already queued behind a complete request.

    h11 exposes bytes delivered to it through ``trailing_data``.  A public
    StreamReader probe covers bytes from the same socket write which asyncio
    retained in its own buffer, without reaching into StreamReader internals.
    """

    probe = asyncio.create_task(reader.read(1))
    await asyncio.sleep(0)
    if probe.done():
        try:
            data = probe.result()
        except asyncio.CancelledError as exc:
            raise _ClientDisconnected from exc
        if data:
            raise _RequestRejected(400, "pipelined_request")
        raise _ClientDisconnected
    probe.cancel()
    await asyncio.gather(probe, return_exceptions=True)


def _validate_incomplete_header_lines(data: bytearray) -> None:
    if b"\x00" in data:
        raise _RequestRejected(400, "nul_byte")
    previous = -1
    for index, byte in enumerate(data):
        if byte == 0x0A and (index == 0 or data[index - 1] != 0x0D):
            raise _RequestRejected(400, "bare_lf")
        if byte == 0x0A:
            if index - previous > MAX_REQUEST_OR_HEADER_LINE_BYTES:
                raise _RequestRejected(431, "header_line_too_large")
            previous = index
    if len(data) - (previous + 1) > MAX_REQUEST_OR_HEADER_LINE_BYTES:
        raise _RequestRejected(431, "header_line_too_large")


def _validate_raw_header_section(header: bytes) -> None:
    data = bytearray(header)
    _validate_incomplete_header_lines(data)
    if not header.endswith(_HEADER_TERMINATOR):
        raise _RequestRejected(400, "incomplete_headers")

    lines = header[:-4].split(b"\r\n")
    if not lines or not lines[0]:
        raise _RequestRejected(400, "missing_request_line")
    names: list[bytes] = []
    for line in lines[1:]:
        if b":" not in line:
            raise _RequestRejected(400, "invalid_header")
        name, _value = line.split(b":", 1)
        names.append(name.lower())
    if names.count(b"host") != 1:
        raise _RequestRejected(400, "invalid_host_count")
    if names.count(b"content-length") > 1:
        raise _RequestRejected(400, "duplicate_content_length")


def _validate_request_event(event: h11.Request) -> None:
    if event.http_version != b"1.1":
        raise _RequestRejected(505, "unsupported_http_version")
    if event.method == b"CONNECT":
        raise _RequestRejected(405, "connect_not_allowed")
    if event.method not in {b"GET", b"POST"}:
        raise _RequestRejected(405, "method_not_allowed")

    grouped: dict[bytes, list[bytes]] = {}
    for name, value in event.headers:
        grouped.setdefault(name.lower(), []).append(value)
    host_values = grouped.get(b"host", [])
    if len(host_values) != 1 or not host_values[0]:
        raise _RequestRejected(400, "invalid_host")
    if b"transfer-encoding" in grouped or b"te" in grouped:
        raise _RequestRejected(400, "transfer_encoding_not_allowed")
    if b"content-encoding" in grouped:
        raise _RequestRejected(400, "content_encoding_not_allowed")
    if b"expect" in grouped:
        raise _RequestRejected(417, "expect_not_allowed")
    if b"upgrade" in grouped:
        raise _RequestRejected(400, "upgrade_not_allowed")
    for value in grouped.get(b"connection", []):
        if b"upgrade" in {token.strip().lower() for token in value.split(b",")}:
            raise _RequestRejected(400, "upgrade_not_allowed")
    for value in grouped.get(b"content-type", []):
        media_type = value.split(b";", 1)[0].strip().lower()
        if media_type.startswith(b"multipart/"):
            raise _RequestRejected(415, "multipart_not_allowed")


def _content_length(event: h11.Request) -> int:
    values = [value for name, value in event.headers if name == b"content-length"]
    if not values:
        return 0
    if len(values) != 1:
        raise _RequestRejected(400, "duplicate_content_length")
    value = values[0]
    if not value or not value.isdigit():
        raise _RequestRejected(400, "invalid_content_length")
    try:
        return int(value)
    except ValueError as exc:
        raise _RequestRejected(400, "invalid_content_length") from exc


def _must_close_request(request: Request) -> bool:
    if request.method == b"POST":
        return True
    for value in request.header_values(b"connection"):
        if b"close" in {token.strip().lower() for token in value.split(b",")}:
            return True
    return False


def _freeze_headers(headers: RawHeaders) -> RawHeaders:
    try:
        iterator = iter(headers)
    except TypeError as exc:
        raise TypeError("headers must contain name/value pairs") from exc
    frozen: list[tuple[bytes, bytes]] = []
    for item in iterator:
        try:
            name, value = item
        except (TypeError, ValueError) as exc:
            raise TypeError("headers must contain name/value pairs") from exc
        if not isinstance(name, bytes) or not isinstance(value, bytes):
            raise TypeError("header names and values must be bytes")
        frozen.append((name, value))
    return tuple(frozen)


def _immutable_address(value: object) -> Address:
    if isinstance(value, tuple):
        return tuple(value)
    if isinstance(value, str) or value is None:
        return value
    return repr(type(value).__name__)


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    await _wait_writer_closed(writer)


async def _wait_writer_closed(writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(ConnectionError, OSError, RuntimeError):
        await writer.wait_closed()


def _status_body(status: int) -> bytes:
    return {
        400: b"bad request",
        405: b"method not allowed",
        413: b"payload too large",
        415: b"unsupported media type",
        417: b"expectation failed",
        431: b"request header fields too large",
        505: b"http version not supported",
    }.get(status, b"bad request")


def _reason_phrase(status: int) -> bytes:
    return {
        200: b"OK",
        204: b"No Content",
        400: b"Bad Request",
        405: b"Method Not Allowed",
        408: b"Request Timeout",
        413: b"Payload Too Large",
        415: b"Unsupported Media Type",
        417: b"Expectation Failed",
        431: b"Request Header Fields Too Large",
        500: b"Internal Server Error",
        503: b"Service Unavailable",
        505: b"HTTP Version Not Supported",
    }.get(status, b"Error")


def _minimal_response_bytes(status: int, body: bytes) -> bytes:
    return (
        b"HTTP/1.1 "
        + str(status).encode("ascii")
        + b" "
        + _reason_phrase(status)
        + b"\r\nConnection: close\r\nContent-Type: text/plain; charset=utf-8\r\n"
        + b"Content-Length: "
        + str(len(body)).encode("ascii")
        + b"\r\n\r\n"
        + body
    )


__all__ = [
    "AdminHttpState",
    "AdminHttpTransport",
    "BODY_TIMEOUT_SECONDS",
    "HEADER_TIMEOUT_SECONDS",
    "KEEPALIVE_TIMEOUT_SECONDS",
    "MAX_ACTIVE_CONNECTIONS",
    "MAX_BODY_BYTES",
    "MAX_HEADER_SECTION_BYTES",
    "MAX_REQUEST_OR_HEADER_LINE_BYTES",
    "Request",
    "Response",
]
