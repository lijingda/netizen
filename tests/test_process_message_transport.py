"""ADR 0107 contracts at the pinned lark-oapi HTTP boundary."""

import asyncio
import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import lark_oapi as lark

from netizen_cli.process_messages import (
    CONTENT_LIMIT, ActivitySnapshot, Created, Ok, ProcessMessages, Rejected, RejectionCode,
    SnapshotContent, Unknown, build_snapshot_content, snapshot_revision,
)


RESOURCE = "/open-apis/im/v1/message_cot"


class LocalFeishu(ThreadingHTTPServer):
    def __init__(self, loop):
        super().__init__(("127.0.0.1", 0), RequestHandler)
        self.loop = loop
        self.entered = asyncio.Event()
        self.release = threading.Event()
        self.release.set()
        self.writer = threading.Lock()
        self.completed = False
        self.disconnect = False
        self.malformed = False
        self.updates = []


class RequestHandler(BaseHTTPRequestHandler):
    server: LocalFeishu

    def log_message(self, *_):
        pass

    def reply(self, payload):
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass  # The timeout/cancellation tests have already closed the socket.

    def do_POST(self):
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        if url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            self.reply({"code": 0, "tenant_access_token": "loopback-only", "expire": 7200})
        elif self.headers.get("Authorization") != "Bearer loopback-only":
            self.send_error(403)
        elif url.path == RESOURCE and query == {"receive_id_type": ["chat_id"]}:
            self.reply({"code": 0, "data": {"cot_id": "cot", "message_id": "msg"}})
        elif (url.path == RESOURCE + "/complete/cot"
              and query == {"message_id": ["msg"], "reason": ["done"]}):
            self.server.completed = True
            self.reply({"code": 0})
        else:
            self.send_error(404)

    def do_PUT(self):
        if self.path != RESOURCE or self.headers.get("Authorization") != "Bearer loopback-only":
            self.send_error(404)
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.updates.append(body)
        if self.server.disconnect:
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        if self.server.malformed:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"unexpected response")
            return
        if self.server.completed:
            self.reply({"code": 10001})
        elif not self.server.writer.acquire(blocking=False):
            self.reply({"code": 10020})
        else:
            try:
                self.server.loop.call_soon_threadsafe(self.server.entered.set)
                if self.server.release.wait(10):
                    self.reply({"code": 0})
                else:
                    self.send_error(503)
            finally:
                self.server.writer.release()


class ProcessMessageTransport(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = LocalFeishu(asyncio.get_running_loop())
        self.thread = threading.Thread(target=self.server.serve_forever, args=(0.02,), daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.release.set)
        self.app_id = uuid4().hex
        self.port = self.client(timeout=5)
        content = build_snapshot_content("turn", "进度", ('新"\\\n🍏' * 800,))
        self.snapshot = snapshot_revision("turn", content, now_seconds=1_800_000_000)

    def client(self, timeout):
        domain = "http://127.0.0.1:%d" % self.server.server_port
        return ProcessMessages(lark.Client.builder().domain(domain).app_id(self.app_id)
                               .app_secret("fixture").timeout(timeout).build())

    async def test_topic_create_overlap_then_terminal_rejection(self):
        self.assertEqual(await self.port.create("chat", "parent", True), Created("cot", "msg"))
        self.server.release.clear()
        async with asyncio.TaskGroup() as group:
            first = group.create_task(self.port.update("cot", "msg", self.snapshot))
            await asyncio.wait_for(self.server.entered.wait(), 5)
            self.assertEqual(await self.port.update("cot", "msg", self.snapshot),
                             Rejected(RejectionCode.CONCURRENT))
            self.server.release.set()
        self.assertEqual(first.result(), Ok())
        self.assertEqual(await self.port.complete("cot", "msg", "done"), Ok())
        self.assertEqual(await self.port.update("cot", "msg", self.snapshot),
                         Rejected(RejectionCode.TERMINAL_OR_INVALID))
        first_body, overlap, terminal = self.server.updates
        self.assertEqual(first_body, overlap)
        self.assertEqual(first_body, terminal)
        self.assertEqual((first_body["cot_id"], first_body["message_id"]), ("cot", "msg"))
        event, = first_body["events"]
        self.assertEqual(event["timestamp"], "1800000000")
        self.assertLessEqual(len(event["content"]), CONTENT_LIMIT)
        self.assertEqual(json.loads(event["content"])["content"]["rows"], list(self.snapshot.content.rows))
        self.assertTrue(self.snapshot.content.rows[0].endswith("…"))

    async def test_timeout_on_dispatched_write_is_unknown(self):
        self.server.release.clear()
        result = await self.client(timeout=0.4).update("cot", "msg", self.snapshot)
        self.assertEqual(result, Unknown())
        self.assertEqual(len(self.server.updates), 1)

    async def test_connection_lost_after_dispatch_is_unknown(self):
        self.server.disconnect = True
        self.assertEqual(await self.port.update("cot", "msg", self.snapshot), Unknown())
        self.assertEqual(len(self.server.updates), 1)

    async def test_non_json_success_status_is_unknown(self):
        self.server.malformed = True
        self.assertEqual(await self.port.update("cot", "msg", self.snapshot), Unknown())

    async def test_cancellation_on_dispatched_write_propagates(self):
        self.server.release.clear()
        async with asyncio.TaskGroup() as group:
            pending = group.create_task(self.port.update("cot", "msg", self.snapshot))
            await asyncio.wait_for(self.server.entered.wait(), 5)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
        self.assertEqual(len(self.server.updates), 1)

    async def test_oversized_snapshot_never_reaches_transport(self):
        oversized = ActivitySnapshot("turn", SnapshotContent("", ("甲" * CONTENT_LIMIT,)), 1_800_000_001)
        with self.assertRaises(ValueError):
            await self.port.update("cot", "msg", oversized)
        self.assertEqual(self.server.updates, [])
