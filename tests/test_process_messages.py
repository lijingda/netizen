import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import lark_oapi as lark

from netizen_cli.process_messages import (
    ActivitySnapshot, Created, Ok, ProcessMessages, Rejected, RejectionCode,
    SnapshotContent, Unknown,
)


RESOURCE = "/open-apis/im/v1/message_cot"
SNAPSHOT = ActivitySnapshot("turn1", SnapshotContent("进度", ("甲", "乙")), 1_800_000_001)


def response(payload, status=200):
    return SimpleNamespace(raw=SimpleNamespace(status_code=status, content=json.dumps(payload).encode()))


class ProcessMessageShapes(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = AsyncMock(spec=lark.Client)
        self.port = ProcessMessages(self.client)

    def request(self, method, uri, queries=(), paths=None):
        request, = self.client.arequest.await_args.args
        self.assertIsInstance(request, lark.BaseRequest)
        self.assertEqual((request.http_method, request.uri), (method, uri))
        self.assertEqual(request.queries, list(queries))
        self.assertEqual(request.paths, paths or {})
        self.assertEqual(request.token_types, {lark.AccessTokenType.TENANT})
        return request.body

    async def test_create_anchor_or_chat(self):
        self.client.arequest.return_value = response({
            "code": 0, "data": {"cot_id": "cot", "message_id": "msg", "unused": 4},
        })
        for parent, in_topic in (("parent", True), (None, False)):
            with self.subTest(parent=parent):
                self.assertEqual(await self.port.create("chat", parent, in_topic), Created("cot", "msg"))
                body = self.request(lark.HttpMethod.POST, RESOURCE, [("receive_id_type", "chat_id")])
                expected = {"receive_id": "chat", "reply_in_thread": in_topic}
                if parent:
                    expected["origin_message_id"] = parent
                self.assertEqual(body, expected)

    async def test_update_has_one_replaceable_event(self):
        self.client.arequest.return_value = response({"code": 0})
        self.assertEqual(await self.port.update("cot", "msg", SNAPSHOT), Ok())
        body = self.request(lark.HttpMethod.PUT, RESOURCE)
        event, = body["events"]
        self.assertEqual(body, {"cot_id": "cot", "message_id": "msg", "events": [event]})
        self.assertEqual(event, {"event_type": "ACTIVITY_SNAPSHOT", "timestamp": "1800000001",
                                 "content": event["content"]})
        self.assertEqual(json.loads(event["content"]), {
            "type": "ACTIVITY_SNAPSHOT", "messageId": "turn1", "activityType": "PLAN",
            "content": {"title": "进度", "rows": ["甲", "乙"]}, "replace": True,
        })

    async def test_complete_has_only_exact_target_and_reason(self):
        self.client.arequest.return_value = response({"code": 0})
        for reason in ("done", "error", "timeout"):
            self.assertEqual(await self.port.complete("cot", "msg", reason), Ok())
            body = self.request(lark.HttpMethod.POST, RESOURCE + "/complete/:cot_id",
                                [("message_id", "msg"), ("reason", reason)], paths={"cot_id": "cot"})
            self.assertIsNone(body)

    async def test_acknowledgement_requires_valid_envelope_for_each_operation(self):
        for operation, args in ((self.port.create, ("chat", None, False)),
                                (self.port.update, ("cot", "msg", SNAPSHOT)),
                                (self.port.complete, ("cot", "msg", "done"))):
            for status, payload, expected in (
                (200, {"code": 10020}, Rejected(RejectionCode.CONCURRENT)),
                (400, {"code": 10001}, Rejected(RejectionCode.TERMINAL_OR_INVALID)),
                (200, {"code": 999}, Rejected(999)), (500, {"code": 0}, Unknown()),
                (200, {"code": "0"}, Unknown()), (200, {"code": False}, Unknown()),
                (200, {}, Unknown()), (200, [], Unknown()),
            ):
                with self.subTest(operation=operation.__name__, payload=payload, status=status):
                    self.client.arequest.return_value = response(payload, status)
                    self.assertEqual(await operation(*args), expected)

    async def test_create_requires_both_opaque_ids(self):
        for data in (None, [], {"cot_id": "1"}, {"message_id": "m"}, {"cot_id": 1, "message_id": "m"},
                     {"cot_id": "1", "message_id": " "}, {"cot_id": "..", "message_id": "m"}):
            self.client.arequest.return_value = response({"code": 0, "data": data})
            self.assertEqual(await self.port.create("chat", None, False), Unknown())

    async def test_code_only_logging_and_no_retry(self):
        self.client.arequest.return_value = response({"code": 10001, "msg": "private error text"})
        with self.assertLogs("netizen_cli.process_messages") as logs:
            self.assertEqual(await self.port.update("cot", "msg", SNAPSHOT), Rejected(10001))
        self.assertIn("10001", logs.output[0])
        self.assertNotIn("private", logs.output[0])
        self.client.arequest.assert_awaited_once()

    async def test_transport_error_and_external_cancellation(self):
        self.client.arequest.side_effect = OSError()
        self.assertEqual(await self.port.create("chat", None, False), Unknown())
        self.client.arequest.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.port.complete("cot", "msg", "done")
