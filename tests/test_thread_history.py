from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from openai_codex import AsyncCodex, AsyncThread, CodexConfig
from openai_codex.errors import InvalidRequestError
from openai_codex.generated import v2_all as generated

from netizen_cli.sdk_gap_adapter import SdkFacadeMigrationRequired, SdkGapCapabilityUnavailable
from netizen_cli.thread_history import (
    AppServerThreadHistory,
    ThreadHistoryUnavailable,
    facade_migration_requirements,
)


_SERVER = r'''
import json, sys
log_path, payload_path, mode = sys.argv[1:]
payload = json.load(open(payload_path))
pending = None
def send(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    with open(log_path, "a") as log:
        log.write(json.dumps(message) + "\n")
    if message.get("id") is None:
        continue
    request_id = message["id"]
    method = message["method"]
    if method == "initialize":
        send({"id":request_id,"result":{"userAgent":"history-test/1","serverInfo":{"name":"fake","version":"1"}}})
    elif method in payload:
        if message["params"].get("cursor") == "bad-cursor":
            send({"id":request_id,"error":{"code":-32600,"message":"invalid cursor"}})
        elif mode == "hold-first" and pending is None:
            pending = request_id
        else:
            if pending is not None:
                send({"id":pending,"result":payload["thread/turns/list"]})
            send({"id":request_id,"result":payload[method]})
    else:
        send({"id":request_id,"error":{"code":-32601,"message":"unexpected method"}})
'''


def _requests(path):
    return [message for line in path.read_text().splitlines() if "id" in (message := json.loads(line))] if path.exists() else []


@asynccontextmanager
async def server(payload, mode="success"):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        fixture = root / "payload.json"
        fixture.write_text(json.dumps(payload))
        log = root / "requests.jsonl"
        codex = AsyncCodex(CodexConfig(launch_args_override=(sys.executable, "-u", "-c", _SERVER, str(log), str(fixture), mode), experimental_api=False))
        await codex.__aenter__()
        process = codex._client._sync._proc
        try:
            yield codex, log
        finally:
            await codex.close()
            if process:
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=1)
                for stream in (process.stdout, process.stderr):
                    if stream:
                        stream.close()


def turn(turn_id="turn", *, items_view="full"):
    return {"id": turn_id, "status": "completed", "items": [], "itemsView": items_view}


def item(turn_id="turn", item_id="answer"):
    return {"turnId": turn_id, "item": {"type": "agentMessage", "id": item_id, "text": "hello"}}


def adapter(payload):
    def request(method, params, *, response_model):
        return response_model.model_validate(payload)
    return AppServerThreadHistory(SimpleNamespace(_initialized=True, _client=SimpleNamespace(request=AsyncMock(side_effect=request))))


class ThreadHistoryTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_sdk_transport_uses_fixed_typed_native_pages(self):
        payload = {
            "thread/turns/list": {"data": [turn()], "nextCursor": "opaque-next", "backwardsCursor": "opaque-back"},
            "thread/items/list": {"data": [item()]},
        }
        async with server(payload) as (codex, log):
            bridge = AppServerThreadHistory(codex)
            page = await bridge.list_turns("thread", cursor="opaque-input", limit=2)
            self.assertIsInstance(page, generated.ThreadTurnsListResponse)
            self.assertEqual(page.next_cursor, "opaque-next")
            self.assertEqual(page.backwards_cursor, "opaque-back")
            items = await bridge.list_items("thread", "turn")
            self.assertIsInstance(items, generated.ThreadItemsListResponse)
            self.assertEqual(items.data[0].item.root.text, "hello")
            calls = _requests(log)[1:]
        self.assertEqual([call["method"] for call in calls], ["thread/turns/list", "thread/items/list"])
        self.assertEqual(calls[0]["params"], {"threadId": "thread", "cursor": "opaque-input", "limit": 2, "sortDirection": "desc", "itemsView": "full"})
        self.assertEqual(calls[1]["params"], {"threadId": "thread", "turnId": "turn", "limit": 20, "sortDirection": "desc"})
        self.assertFalse(hasattr(bridge, "request"))
        self.assertFalse(hasattr(bridge, "close"))

    async def test_empty_pages_and_summary(self):
        self.assertEqual((await adapter({"data": []}).list_turns("thread")).data, [])
        bridge = adapter({"data": [turn(items_view="summary")]})
        page = await bridge.list_turns("thread", items_view="summary")
        self.assertEqual(page.data[0].items_view.value, "summary")

    async def test_invalid_cursor_is_not_retried_and_connection_remains_usable(self):
        async with server({"thread/turns/list": {"data": []}}) as (codex, log):
            bridge = AppServerThreadHistory(codex)
            with self.assertRaises(InvalidRequestError):
                await bridge.list_turns("thread", cursor="bad-cursor")
            self.assertEqual(len(_requests(log)), 2)
            self.assertEqual((await bridge.list_turns("thread")).data, [])
            self.assertEqual(len(_requests(log)), 3)

    async def test_cancellation_and_timeout_do_not_close_shared_transport_or_retry(self):
        for timeout in (False, True):
            with self.subTest(timeout=timeout):
                async with server({"thread/turns/list": {"data": []}}, "hold-first") as (codex, log):
                    bridge = AppServerThreadHistory(codex)
                    task = asyncio.create_task(bridge.list_turns("thread"))
                    async with asyncio.timeout(3):
                        while len(_requests(log)) < 2:
                            await asyncio.sleep(0.01)
                    if timeout:
                        with self.assertRaises(TimeoutError):
                            async with asyncio.timeout(0):
                                await task
                    else:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    self.assertEqual(len(_requests(log)), 2)
                    # The next independent read releases the cancelled read's
                    # late response. Correlation still belongs to the SDK.
                    self.assertEqual((await bridge.list_turns("thread")).data, [])
                    self.assertEqual(len(_requests(log)), 3)

    async def test_page_identity_shape_and_view_fail_closed(self):
        for payload in (
            {"data": [turn(), turn()]},
            {"data": [turn(items_view="summary")]},
            {"data": [turn("")]},
            {"data": [], "nextCursor": "again"},
        ):
            with self.subTest(payload=payload), self.assertRaises(ThreadHistoryUnavailable):
                await adapter(payload).list_turns("thread", cursor="again", limit=2)
        for payload in ({"data": [item("other")]}, {"data": [item(), item()]}):
            with self.subTest(payload=payload), self.assertRaises(ThreadHistoryUnavailable):
                await adapter(payload).list_items("thread", "turn")

    def test_shape_and_public_facade_migration_gates(self):
        self.assertEqual(facade_migration_requirements(), ())
        with patch.object(AsyncThread, "list_turns", create=True):
            with self.assertRaises(SdkFacadeMigrationRequired):
                adapter({"data": []})
        with patch.object(generated, "ThreadTurnsListParams", None):
            with self.assertRaises(SdkGapCapabilityUnavailable):
                adapter({"data": []})
        with self.assertRaises(SdkGapCapabilityUnavailable):
            AppServerThreadHistory(SimpleNamespace(_initialized=False))
        field = generated.ThreadTurnsListParams.model_fields["cursor"]
        with patch.object(field, "alias", "changedCursor"):
            with self.assertRaises(SdkGapCapabilityUnavailable):
                adapter({"data": []})
