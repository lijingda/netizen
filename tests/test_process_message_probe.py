import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from netizen_cli.process_messages import Created, Ok, ProcessMessages, Rejected, Unknown
from scripts.check_process_messages import GateFailed, check_writes, exact


class LiveProbeContract(unittest.IsolatedAsyncioTestCase):
    async def test_replacement_only_writes_v1_v2_and_identical_replay_then_completes(self):
        port = AsyncMock(spec=ProcessMessages)
        port.update.return_value = port.complete.return_value = Ok()
        report = {}
        await check_writes(port, Created("cA", "mA"), "A", report)
        self.assertEqual(report["phase"], "finished")
        self.assertEqual([call[0] for call in port.mock_calls], ["update"] * 3 + ["complete"])
        initial, replacement, replay = [call.args[2] for call in port.update.await_args_list]
        self.assertEqual((initial.content.title, initial.content.rows), ("初版", ("甲",)))
        self.assertEqual((replacement.content.title, replacement.content.rows), ("终版", ("乙",)))
        self.assertGreater(replacement.timestamp, initial.timestamp)
        self.assertEqual(initial.activity_id, replacement.activity_id)
        self.assertIs(replacement, replay)
        port.complete.assert_awaited_once_with("cA", "mA", "done")

    async def test_budget_only_writes_one_snapshot_then_challenges_confirmed_completion(self):
        port = AsyncMock(spec=ProcessMessages)
        port.update.side_effect = [Ok(), Rejected(10001)]
        port.complete.return_value = Ok()
        report = {}
        await check_writes(port, Created("cB", "mB"), "B", report)
        self.assertEqual([call[0] for call in port.mock_calls], ["update", "complete", "update"])
        self.assertEqual(report["inner_characters"], 1800)
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
