"""ADR 0107 contracts at the pinned lark-oapi HTTP boundary."""

import asyncio
import json
import socket
import threading
import unittest
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import lark_oapi as lark

from netizen_cli.process_messages import (
    CONTENT_LIMIT, Created, Ok, ProcessMessages, Rejected, RejectionCode, StepItem, TextItem, ToolItem,
    Unknown, build_items, finish_items,
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
        self.raw_response = None
        self.updates = []


class RequestHandler(BaseHTTPRequestHandler):
    server: LocalFeishu

    def log_message(self, *_):
        pass

    def reply(self, payload):
        self.reply_raw(json.dumps(payload).encode())

    def reply_raw(self, raw):
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
        if self.server.raw_response is not None:
            self.reply_raw(self.server.raw_response)
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
        self.items = (ToolItem("tool", '新"\\\n🍏' * 800, 1_800_000_000, 1_800_000_001),)

    def client(self, timeout):
        domain = "http://127.0.0.1:%d" % self.server.server_port
        return ProcessMessages(lark.Client.builder().domain(domain).app_id(self.app_id)
                               .app_secret("fixture").timeout(timeout).build())

    async def test_topic_create_overlap_then_terminal_rejection(self):
        self.assertEqual(await self.port.create("chat", "parent", True), Created("cot", "msg"))
        self.server.release.clear()
        async with asyncio.TaskGroup() as group:
            first = group.create_task(self.port.update("cot", "msg", self.items))
            await asyncio.wait_for(self.server.entered.wait(), 5)
            self.assertEqual(await self.port.update("cot", "msg", self.items),
                             Rejected(RejectionCode.CONCURRENT))
            self.server.release.set()
        self.assertEqual(first.result(), Ok())
        self.assertEqual(await self.port.complete("cot", "msg", "done"), Ok())
        self.assertEqual(await self.port.update("cot", "msg", self.items),
                         Rejected(RejectionCode.TERMINAL_OR_INVALID))
        first_body, overlap, terminal = self.server.updates
        self.assertEqual(first_body, overlap)
        self.assertEqual(first_body, terminal)
        self.assertEqual((first_body["cot_id"], first_body["message_id"]), ("cot", "msg"))
        start, end = first_body["events"]
        self.assertEqual([start["timestamp"], end["timestamp"]], ["1800000000", "1800000001"])
        self.assertEqual([start["event_type"], end["event_type"]], ["TOOL_CALL_START", "TOOL_CALL_END"])
        self.assertTrue(all(len(e["content"]) <= CONTENT_LIMIT for e in (start, end)))
        label = json.loads(start["content"])["toolCallName"]
        self.assertEqual(label, self.items[0].text[:299] + "…")
        self.assertEqual(json.loads(end["content"])["toolCallId"], sha256(b"tool").hexdigest()[:32])

    async def test_timeout_on_dispatched_write_is_unknown(self):
        self.server.release.clear()
        result = await self.client(timeout=0.4).update("cot", "msg", self.items)
        self.assertEqual(result, Unknown())
        self.assertEqual(len(self.server.updates), 1)

    async def test_connection_lost_after_dispatch_is_unknown(self):
        self.server.disconnect = True
        self.assertEqual(await self.port.update("cot", "msg", self.items), Unknown())
        self.assertEqual(len(self.server.updates), 1)

    async def test_non_json_success_status_is_unknown(self):
        self.server.raw_response = b"unexpected response"
        self.assertEqual(await self.port.update("cot", "msg", self.items), Unknown())

    async def test_duplicate_keys_anywhere_in_a_receipt_are_unknown(self):
        receipts = (b'{"code":10001,"code":0}', b'{"code":0,"code":10020}',
                    b'{"code":0,"data":[{"extra":1,"extra":1}]}')
        for raw in receipts:
            with self.subTest(receipt=raw):
                self.server.raw_response = raw
                self.assertEqual(await self.port.update("cot", "msg", self.items), Unknown())
        self.assertEqual(len(self.server.updates), len(receipts))

    async def test_cancellation_on_dispatched_write_propagates(self):
        self.server.release.clear()
        async with asyncio.TaskGroup() as group:
            pending = group.create_task(self.port.update("cot", "msg", self.items))
            await asyncio.wait_for(self.server.entered.wait(), 5)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
        self.assertEqual(len(self.server.updates), 1)

    async def test_long_local_identities_fit_all_event_envelopes_on_resend(self):
        t = 1_800_000_000
        long_id = "标" * 1800
        cases = ((ToolItem(long_id, "工具", t), "toolCallId", 2),
                 (StepItem(long_id, "步骤", t), "stepName", 2),
                 (TextItem(long_id, "文字", t, t), "messageId", 3),
                 (TextItem("x" * 1738, "p", t, t), "messageId", 3))
        for item, field, count in cases:
            with self.subTest(item_type=type(item).__name__, length=len(item.item_id)):
                self.assertEqual(build_items((item,)), (item,))
                final = finish_items((item,), t + 1)
                self.assertEqual(final, (item.finish(t + 1),))
                self.assertEqual(await self.port.update("cot", "msg", final), Ok())
                self.assertEqual(await self.port.update("cot", "msg", final), Ok())
                first, replay = self.server.updates[-2:]
                self.assertEqual(first, replay)
                self.assertEqual(len(first["events"]), count)
                expected = item.text if field == "stepName" else sha256(item.item_id.encode()).hexdigest()[:32]
                for event in first["events"]:
                    self.assertLessEqual(len(event["content"]), CONTENT_LIMIT)
                    self.assertEqual(json.loads(event["content"])[field], expected)
