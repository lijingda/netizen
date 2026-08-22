from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import socket
import unittest
from collections.abc import Awaitable, Callable
from unittest.mock import patch

from netizen.admin.transport import (
    AdminHttpState,
    AdminHttpTransport,
    MAX_ACTIVE_CONNECTIONS,
    MAX_BODY_BYTES,
    MAX_HEADER_SECTION_BYTES,
    MAX_REQUEST_OR_HEADER_LINE_BYTES,
    Request,
    Response,
)


Handler = Callable[[Request], Awaitable[Response]]


async def _ok_handler(request: Request) -> Response:
    return Response(
        status=200,
        headers=((b"Content-Type", b"text/plain"),),
        body=request.target,
    )


class AdminHttpTransportTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.transports: list[AdminHttpTransport] = []

    async def asyncTearDown(self) -> None:
        loop = asyncio.get_running_loop()
        for transport in reversed(self.transports):
            with contextlib.suppress(Exception):
                await transport.drain(loop.time() + 1.0)

    async def _transport(
        self,
        handler: Handler = _ok_handler,
        *,
        open_admission: bool = True,
        port: int = 0,
    ) -> AdminHttpTransport:
        transport = AdminHttpTransport("127.0.0.1", port, handler)
        self.transports.append(transport)
        await transport.bind()
        if open_admission:
            transport.open_admission()
        return transport

    async def _connect(
        self,
        transport: AdminHttpTransport,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        address = transport.addresses[0]
        assert isinstance(address, tuple)
        return await asyncio.open_connection("127.0.0.1", int(address[1]))

    async def _request(
        self,
        transport: AdminHttpTransport,
        request: bytes,
    ) -> tuple[int, dict[bytes, bytes], bytes]:
        reader, writer = await self._connect(transport)
        try:
            writer.write(request)
            await writer.drain()
            return await _read_response(reader)
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_request_and_response_are_immutable(self) -> None:
        seen: list[Request] = []

        async def handler(request: Request) -> Response:
            seen.append(request)
            return Response(204)

        transport = await self._transport(handler)
        status, _headers, body = await self._request(
            transport,
            b"GET /immutable HTTP/1.1\r\nHost: localhost\r\n\r\n",
        )

        self.assertEqual(status, 204)
        self.assertEqual(body, b"")
        self.assertEqual(seen[0].method, b"GET")
        self.assertEqual(seen[0].target, b"/immutable")
        self.assertEqual(seen[0].http_version, b"1.1")
        self.assertEqual(seen[0].header_values(b"HOST"), (b"localhost",))
        self.assertTrue(seen[0].request_id)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            seen[0].body = b"changed"  # type: ignore[misc]
        response = Response(200, [[b"X-Test", b"yes"]], b"ok")  # type: ignore[arg-type]
        self.assertIsInstance(response.headers, tuple)
        self.assertIsInstance(response.headers[0], tuple)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            response.status = 201  # type: ignore[misc]

    async def test_bound_closed_then_open_admission(self) -> None:
        calls = 0

        async def handler(_request: Request) -> Response:
            nonlocal calls
            calls += 1
            return Response(204)

        transport = await self._transport(handler, open_admission=False)
        self.assertEqual(transport.state, AdminHttpState.BOUND_CLOSED)
        self.assertFalse(transport.admission_open)

        status, _headers, _body = await self._request(
            transport,
            b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n",
        )
        self.assertEqual(status, 503)
        self.assertEqual(calls, 0)

        transport.open_admission()
        status, _headers, _body = await self._request(
            transport,
            b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n",
        )
        self.assertEqual(status, 204)
        self.assertEqual(calls, 1)

        reader, writer = await self._connect(transport)
        try:
            writer.write(b"GET /late HTTP/1.1\r\n")
            await writer.drain()
            await _wait_until(lambda: transport.active_connection_count == 1)
            transport.close_admission()
            writer.write(b"Host: localhost\r\n\r\n")
            await writer.drain()
            status, _headers, _body = await _read_response(reader)
        finally:
            writer.close()
            await writer.wait_closed()
        self.assertEqual(status, 503)
        self.assertEqual(calls, 1)

    async def test_connection_cap_rejects_33rd_and_returns_capacity(self) -> None:
        transport = await self._transport()
        held: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = []
        try:
            for _ in range(MAX_ACTIVE_CONNECTIONS):
                held.append(await self._connect(transport))
            await _wait_until(
                lambda: transport.active_connection_count
                == MAX_ACTIVE_CONNECTIONS
            )

            status, _headers, _body = await self._request(
                transport,
                b"GET /overflow HTTP/1.1\r\nHost: localhost\r\n\r\n",
            )
            self.assertEqual(status, 503)
            self.assertEqual(
                transport.active_connection_count,
                MAX_ACTIVE_CONNECTIONS,
            )

            _reader, released = held.pop()
            released.close()
            await released.wait_closed()
            await _wait_until(
                lambda: transport.active_connection_count
                == MAX_ACTIVE_CONNECTIONS - 1
            )

            status, _headers, body = await self._request(
                transport,
                b"GET /capacity-returned HTTP/1.1\r\nHost: localhost\r\n\r\n",
            )
            self.assertEqual(status, 200)
            self.assertEqual(body, b"/capacity-returned")
        finally:
            for _reader, writer in held:
                writer.close()
            await asyncio.gather(
                *(writer.wait_closed() for _reader, writer in held),
                return_exceptions=True,
            )

    async def test_header_deadline_is_absolute_despite_drip(self) -> None:
        with patch("netizen.admin.transport.HEADER_TIMEOUT_SECONDS", 0.15):
            transport = await self._transport()
            reader, writer = await self._connect(transport)
            try:
                writer.write(b"GET / HTTP/1.1\r\n")
                await writer.drain()
                await asyncio.sleep(0.08)
                writer.write(b"Host: local")
                await writer.drain()
                await asyncio.sleep(0.09)
                with contextlib.suppress(ConnectionError):
                    writer.write(b"host\r\n\r\n")
                    await writer.drain()
                status, _headers, body = await _read_response(reader)
            finally:
                writer.close()
                await writer.wait_closed()

        self.assertEqual(status, 408)
        self.assertEqual(body, b"request timeout")

    async def test_body_deadline_is_absolute_despite_drip(self) -> None:
        calls = 0

        async def handler(_request: Request) -> Response:
            nonlocal calls
            calls += 1
            return Response(204)

        with patch("netizen.admin.transport.BODY_TIMEOUT_SECONDS", 0.12):
            transport = await self._transport(handler)
            reader, writer = await self._connect(transport)
            try:
                writer.write(
                    b"POST / HTTP/1.1\r\nHost: localhost\r\n"
                    b"Content-Length: 2\r\n\r\na"
                )
                await writer.drain()
                await asyncio.sleep(0.14)
                status, _headers, body = await _read_response(reader)
            finally:
                writer.close()
                await writer.wait_closed()

        self.assertEqual(status, 408)
        self.assertEqual(body, b"request timeout")
        self.assertEqual(calls, 0)

    async def test_exact_wire_and_body_boundaries(self) -> None:
        async def sizes(request: Request) -> Response:
            return Response(200, body=str(len(request.body)).encode("ascii"))

        transport = await self._transport(sizes)

        request_line = _request_line(MAX_REQUEST_OR_HEADER_LINE_BYTES)
        status, _headers, _body = await self._request(
            transport,
            request_line + b"Host: x\r\n\r\n",
        )
        self.assertEqual(status, 200)
        status, _headers, _body = await self._request(
            transport,
            _request_line(MAX_REQUEST_OR_HEADER_LINE_BYTES + 1)
            + b"Host: x\r\n\r\n",
        )
        self.assertEqual(status, 431)

        exact_line = b"X-Test: " + b"a" * (
            MAX_REQUEST_OR_HEADER_LINE_BYTES - len(b"X-Test: \r\n")
        ) + b"\r\n"
        status, _headers, _body = await self._request(
            transport,
            b"GET / HTTP/1.1\r\nHost: x\r\n" + exact_line + b"\r\n",
        )
        self.assertEqual(status, 200)
        too_long_line = exact_line[:-2] + b"a\r\n"
        status, _headers, _body = await self._request(
            transport,
            b"GET / HTTP/1.1\r\nHost: x\r\n"
            + too_long_line
            + b"\r\n",
        )
        self.assertEqual(status, 431)

        exact_headers = _header_section(MAX_HEADER_SECTION_BYTES)
        self.assertEqual(len(exact_headers), MAX_HEADER_SECTION_BYTES)
        status, _headers, _body = await self._request(transport, exact_headers)
        self.assertEqual(status, 200)
        too_many_headers = _header_section(MAX_HEADER_SECTION_BYTES + 1)
        status, _headers, _body = await self._request(
            transport, too_many_headers
        )
        self.assertEqual(status, 431)

        exact_body = b"x" * MAX_BODY_BYTES
        status, _headers, response_body = await self._request(
            transport,
            b"POST /body HTTP/1.1\r\nHost: x\r\nContent-Length: "
            + str(len(exact_body)).encode("ascii")
            + b"\r\n\r\n"
            + exact_body,
        )
        self.assertEqual(status, 200)
        self.assertEqual(response_body, str(MAX_BODY_BYTES).encode("ascii"))
        status, _headers, _body = await self._request(
            transport,
            b"POST /body HTTP/1.1\r\nHost: x\r\nContent-Length: "
            + str(MAX_BODY_BYTES + 1).encode("ascii")
            + b"\r\n\r\n",
        )
        self.assertEqual(status, 413)

    async def test_malformed_and_forbidden_requests_are_sanitized(self) -> None:
        calls = 0

        async def handler(_request: Request) -> Response:
            nonlocal calls
            calls += 1
            return Response(204)

        transport = await self._transport(handler)
        marker = b"ATTACKER_SECRET"
        cases = {
            "bare LF": b"GET / HTTP/1.1\nHost: x\n\n" + marker,
            "NUL": b"GET /\x00 HTTP/1.1\r\nHost: x\r\n\r\n" + marker,
            "missing Host": b"GET / HTTP/1.1\r\nX-Test: "
            + marker
            + b"\r\n\r\n",
            "duplicate Host": b"GET / HTTP/1.1\r\nHost: x\r\nHost: y\r\n\r\n",
            "duplicate Content-Length": (
                b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n"
                b"Content-Length: 0\r\n\r\n"
            ),
            "invalid Content-Length": (
                b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: nope\r\n\r\n"
            ),
            "chunked": (
                b"POST / HTTP/1.1\r\nHost: x\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n"
            ),
            "TE plus CL": (
                b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n"
                b"Content-Length: 0\r\n\r\n"
            ),
            "Content-Encoding": (
                b"POST / HTTP/1.1\r\nHost: x\r\nContent-Encoding: gzip\r\n"
                b"Content-Length: 0\r\n\r\n"
            ),
            "multipart": (
                b"POST / HTTP/1.1\r\nHost: x\r\n"
                b"Content-Type: multipart/form-data; boundary=x\r\n"
                b"Content-Length: 0\r\n\r\n"
            ),
            "Expect": (
                b"POST / HTTP/1.1\r\nHost: x\r\nExpect: 100-continue\r\n"
                b"Content-Length: 0\r\n\r\n"
            ),
            "Upgrade": (
                b"GET / HTTP/1.1\r\nHost: x\r\nConnection: Upgrade\r\n"
                b"Upgrade: websocket\r\n\r\n"
            ),
            "CONNECT": b"CONNECT x:443 HTTP/1.1\r\nHost: x\r\n\r\n",
            "GET body": (
                b"GET / HTTP/1.1\r\nHost: x\r\nContent-Length: 1\r\n\r\nx"
            ),
        }
        for label, request in cases.items():
            with self.subTest(label=label):
                status, _headers, body = await self._request(transport, request)
                self.assertGreaterEqual(status, 400)
                self.assertNotIn(marker, body)
                self.assertNotIn(b"Traceback", body)
        self.assertEqual(calls, 0)

    async def test_handler_failure_does_not_echo_exception_or_traceback(self) -> None:
        async def handler(_request: Request) -> Response:
            raise RuntimeError("ATTACKER_SECRET")

        transport = await self._transport(handler)
        with self.assertLogs("netizen.admin.transport", level="ERROR") as captured:
            status, _headers, body = await self._request(
                transport,
                b"GET / HTTP/1.1\r\nHost: x\r\n\r\n",
            )

        self.assertEqual(status, 500)
        self.assertNotIn(b"ATTACKER_SECRET", body)
        self.assertNotIn(b"Traceback", body)
        rendered_logs = "\n".join(captured.output)
        self.assertNotIn("ATTACKER_SECRET", rendered_logs)
        self.assertNotIn("Traceback", rendered_logs)

    async def test_get_keepalive_then_second_header_timeout(self) -> None:
        with (
            patch("netizen.admin.transport.HEADER_TIMEOUT_SECONDS", 0.10),
            patch("netizen.admin.transport.KEEPALIVE_TIMEOUT_SECONDS", 0.40),
        ):
            transport = await self._transport()
            reader, writer = await self._connect(transport)
            try:
                writer.write(b"GET /one HTTP/1.1\r\nHost: x\r\n\r\n")
                await writer.drain()
                first_status, _headers, first_body = await _read_response(reader)
                self.assertEqual(first_status, 200)
                self.assertEqual(first_body, b"/one")

                writer.write(b"G")
                await writer.drain()
                second_status, _headers, second_body = await _read_response(reader)
                self.assertEqual(second_status, 408)
                self.assertEqual(second_body, b"request timeout")
                self.assertEqual(await reader.read(), b"")
            finally:
                writer.close()
                await writer.wait_closed()

    async def test_post_always_closes_connection(self) -> None:
        transport = await self._transport()
        reader, writer = await self._connect(transport)
        try:
            writer.write(
                b"POST /post HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n"
            )
            await writer.drain()
            status, headers, body = await _read_response(reader)
            self.assertEqual(status, 200)
            self.assertEqual(body, b"/post")
            self.assertEqual(headers[b"connection"], b"close")
            self.assertEqual(await reader.read(), b"")
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_same_write_pipelining_is_rejected_before_handler(self) -> None:
        calls = 0

        async def handler(_request: Request) -> Response:
            nonlocal calls
            calls += 1
            return Response(204)

        transport = await self._transport(handler)
        status, _headers, _body = await self._request(
            transport,
            b"GET /one HTTP/1.1\r\nHost: x\r\n\r\n"
            b"GET /two HTTP/1.1\r\nHost: x\r\n\r\n",
        )
        self.assertEqual(status, 400)
        self.assertEqual(calls, 0)

    async def test_disconnect_cancels_handler_and_returns_capacity(self) -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def handler(_request: Request) -> Response:
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        transport = await self._transport(handler)
        _reader, writer = await self._connect(transport)
        writer.write(b"GET /query HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        await asyncio.wait_for(started.wait(), timeout=1)
        self.assertEqual(transport.active_handler_count, 1)

        writer.close()
        await writer.wait_closed()
        await asyncio.wait_for(cancelled.wait(), timeout=1)
        await _wait_until(lambda: transport.active_connection_count == 0)
        self.assertEqual(transport.active_handler_count, 0)

    async def test_close_is_listener_first_and_drain_leaves_no_tasks(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def handler(_request: Request) -> Response:
            started.set()
            await release.wait()
            return Response(204)

        transport = await self._transport(handler)
        reader, writer = await self._connect(transport)
        writer.write(b"GET /drain HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        await asyncio.wait_for(started.wait(), timeout=1)

        address = transport.addresses[0]
        assert isinstance(address, tuple)
        await transport.close()
        await transport.close()
        self.assertEqual(transport.state, AdminHttpState.CLOSED)
        self.assertEqual(transport.active_handler_count, 1)
        with self.assertRaises(OSError):
            await asyncio.open_connection("127.0.0.1", int(address[1]))

        release.set()
        await transport.drain(asyncio.get_running_loop().time() + 1)
        status, _headers, _body = await _read_response(reader)
        self.assertEqual(status, 204)
        self.assertEqual(await reader.read(), b"")
        self.assertEqual(transport.active_connection_count, 0)
        self.assertEqual(transport.active_handler_count, 0)
        writer.close()
        await writer.wait_closed()

    async def test_drain_deadline_cancels_remaining_handler(self) -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def handler(_request: Request) -> Response:
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        transport = await self._transport(handler)
        _reader, writer = await self._connect(transport)
        writer.write(b"GET /stuck HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        await asyncio.wait_for(started.wait(), timeout=1)

        await transport.drain(asyncio.get_running_loop().time() + 0.03)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(transport.active_connection_count, 0)
        self.assertEqual(transport.active_handler_count, 0)
        writer.close()
        await writer.wait_closed()

    async def test_port_conflict_leaves_failed_transport_closed(self) -> None:
        first = await self._transport()
        address = first.addresses[0]
        assert isinstance(address, tuple)
        failed = AdminHttpTransport("127.0.0.1", int(address[1]), _ok_handler)
        self.transports.append(failed)

        with self.assertRaises(OSError):
            await failed.bind()

        self.assertEqual(failed.state, AdminHttpState.CLOSED)
        self.assertEqual(failed.addresses, ())
        status, _headers, body = await self._request(
            first,
            b"GET /original HTTP/1.1\r\nHost: x\r\n\r\n",
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"/original")

    async def test_lifecycle_rejects_a_different_event_loop(self) -> None:
        transport = await self._transport()

        async def wrong_loop_call() -> None:
            await transport.close()

        with self.assertRaisesRegex(RuntimeError, "creator event loop"):
            await asyncio.to_thread(lambda: asyncio.run(wrong_loop_call()))

        self.assertEqual(transport.state, AdminHttpState.BOUND_CLOSED)
        self.assertTrue(transport.admission_open)


async def _read_response(
    reader: asyncio.StreamReader,
) -> tuple[int, dict[bytes, bytes], bytes]:
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2)
    lines = head[:-4].split(b"\r\n")
    status = int(lines[0].split(b" ", 2)[1])
    headers: dict[bytes, bytes] = {}
    for line in lines[1:]:
        name, value = line.split(b":", 1)
        headers[name.strip().lower()] = value.strip()
    length = int(headers.get(b"content-length", b"0"))
    body = await asyncio.wait_for(reader.readexactly(length), timeout=2)
    return status, headers, body


async def _wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 2.0,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("condition was not reached before timeout")
        await asyncio.sleep(0.005)


def _request_line(wire_bytes: int) -> bytes:
    fixed = b"GET /" + b" HTTP/1.1\r\n"
    if wire_bytes < len(fixed):
        raise ValueError("request line is too small")
    return b"GET /" + b"x" * (wire_bytes - len(fixed)) + b" HTTP/1.1\r\n"


def _header_section(wire_bytes: int) -> bytes:
    parts = [b"GET / HTTP/1.1\r\n", b"Host: x\r\n"]
    remaining = wire_bytes - sum(map(len, parts)) - len(b"\r\n")
    index = 0
    while remaining:
        prefix = f"X-{index}: ".encode("ascii")
        overhead = len(prefix) + len(b"\r\n")
        if remaining < overhead:
            # Grow the preceding value when the final fragment is too small
            # to form a syntactically valid header line.
            previous = parts.pop()
            assert previous.endswith(b"\r\n")
            parts.append(previous[:-2] + b"x" * remaining + b"\r\n")
            remaining = 0
            break
        line_length = min(MAX_REQUEST_OR_HEADER_LINE_BYTES, remaining)
        value_length = line_length - overhead
        parts.append(prefix + b"x" * value_length + b"\r\n")
        remaining -= line_length
        index += 1
    result = b"".join(parts) + b"\r\n"
    assert len(result) == wire_bytes
    assert all(
        len(line) + 2 <= MAX_REQUEST_OR_HEADER_LINE_BYTES
        for line in result[:-4].split(b"\r\n")
    )
    return result


if __name__ == "__main__":
    unittest.main()
