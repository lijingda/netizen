import io
import json
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from netizen_cli.process_messages import Created, Ok, ProcessMessages, Rejected, Unknown, item_events
from scripts import check_process_messages
from scripts.check_process_messages import GateFailed, check_writes, exact


class LiveProbeContract(unittest.IsolatedAsyncioTestCase):
    async def test_lookup_labels_go_to_stderr_and_message_text_stays_out_of_json(self):
        stamp = "2026-01-01T00:00:00Z"
        labels = [f"[过程消息验收 {case}] {stamp}" for case in "ABC"]
        stderr = io.StringIO()
        report = {"run_utc": stamp}
        port = AsyncMock(spec=ProcessMessages)
        port.update.side_effect = [Ok()] * 3 + [Rejected(10001)] + [Ok()] * 6
        port.complete.return_value = Ok()
        with (
            patch.object(check_process_messages, "load_lark_app",
                         return_value=SimpleNamespace(app_id="fixture", app_secret="fixture")),
            patch.object(check_process_messages, "FeishuChannel"),
            patch.object(check_process_messages, "ProcessMessages", return_value=port),
            patch.object(check_process_messages, "create_in_topic",
                         side_effect=[Created(f"c{c}", f"m{c}") for c in "ABC"]) as create,
            redirect_stderr(stderr),
        ):
            await check_process_messages.probe(Path("."), "chat", report)
        self.assertEqual(stderr.getvalue().splitlines(), labels)
        self.assertEqual([call.args[4] for call in create.await_args_list], labels)
        transmitted_text = [text for call in create.await_args_list for text in call.args[4:6]]
        transmitted_text.extend(item.text for call in port.update.await_args_list for item in call.args[2])
        encoded = json.dumps(report, ensure_ascii=False)
        for text in transmitted_text:
            self.assertNotIn(json.dumps(text, ensure_ascii=False), encoded)
        self.assertEqual(report["phase"], "finished")

    async def test_items_are_closed_in_the_final_list_before_completion(self):
        port = AsyncMock(spec=ProcessMessages)
        port.update.return_value = port.complete.return_value = Ok()
        report = {}
        await check_writes(port, Created("cA", "mA"), "A", report)
        self.assertEqual(report["phase"], "finished")
        self.assertEqual([call[0] for call in port.mock_calls], ["update", "update", "complete"])
        initial, final = [item_events(call.args[2]) for call in port.update.await_args_list]
        self.assertEqual({e["event_type"] for e in initial}, {"TOOL_CALL_START", "STEP_STARTED"})
        self.assertTrue(all(e in final for e in initial))
        self.assertCountEqual([e["event_type"] for e in final],
                              ["TOOL_CALL_START", "TOOL_CALL_END", "STEP_STARTED", "STEP_FINISHED",
                               "TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END"])
        port.complete.assert_awaited_once_with("cA", "mA", "done")

    async def test_budget_reaches_the_boundary_then_challenges_confirmed_completion(self):
        port = AsyncMock(spec=ProcessMessages)
        port.update.side_effect = [Ok(), Rejected(10001)]
        port.complete.return_value = Ok()
        report = {}
        await check_writes(port, Created("cB", "mB"), "B", report)
        self.assertEqual([call[0] for call in port.mock_calls], ["update", "complete", "update"])
        events = item_events(port.update.await_args_list[0].args[2])
        self.assertEqual(len(events[0]["content"]), 1800)
        self.assertEqual(report["max_event_characters"], 1800)
        self.assertEqual(report["post_complete_code"], 10001)
        port.complete.assert_awaited_once_with("cB", "mB", "done")

    async def test_unknown_receipts_do_not_retry(self):
        port = AsyncMock(spec=ProcessMessages)
        port.update.return_value = Unknown()
        port.complete.return_value = Unknown()
        report = {}
        with self.assertRaises(GateFailed):
            await check_writes(port, Created("c", "m"), "B", report)
        port.update.assert_awaited_once()
        port.complete.assert_awaited_once_with("c", "m", "error")
        self.assertEqual(report["phase"], "budget")
        self.assertIsNone(report["complete_code"])

    async def test_only_the_overlap_sample_permits_10020(self):
        for outcomes in ([Ok()] * 6, [Ok(), Rejected(10020)] * 3):
            port = AsyncMock(spec=ProcessMessages)
            port.update.side_effect = outcomes
            port.complete.return_value = Ok()
            report = {}
            await check_writes(port, Created("cC", "mC"), "C", report)
            self.assertEqual([call[0] for call in port.mock_calls], ["update"] * 6 + ["complete"])
            self.assertEqual(report["concurrent_10020_informational"], Rejected(10020) in outcomes)
            self.assertEqual(report["phase"], "finished")
            port.complete.assert_awaited_once_with("cC", "mC", "done")

    async def test_unexpected_concurrent_code_fails_the_gate(self):
        port = AsyncMock(spec=ProcessMessages)
        port.update.side_effect = [Ok()] * 5 + [Rejected(10001)]
        report = {}
        port.complete.return_value = Ok()
        with self.assertRaises(GateFailed):
            await check_writes(port, Created("c", "m"), "C", report)
        self.assertEqual(report["phase"], "concurrent_updates")
        port.complete.assert_awaited_once_with("c", "m", "error")

    async def test_exact_read_checks_single_item_and_its_identity(self):
        client = AsyncMock()
        fetch = client.im.v1.message.aget
        item = SimpleNamespace(message_id="m", chat_id="chat", deleted=False)
        for items, chat, message in (([], "chat", "m"), ([item, item], "chat", "m"),
                                     ([item], "other", "m"), ([item], "chat", "other")):
            fetch.return_value = SimpleNamespace(code=0, data=SimpleNamespace(items=items))
            with self.assertRaises(GateFailed):
                await exact(client, chat, message)
        self.assertIs(await exact(client, "chat", "m"), item)
        request, = fetch.await_args.args
        self.assertEqual(request.message_id, "m")
