import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from netizen_cli.process_messages import Created, Ok, ProcessMessages, Rejected, Unknown
from scripts.check_process_messages import GateFailed, check_writes, exact


class LiveProbeContract(unittest.IsolatedAsyncioTestCase):
    async def test_one_burst_replay_and_post_complete_challenge(self):
        port = AsyncMock(spec=ProcessMessages)
        port.update.side_effect = [Ok(), *[Rejected(10020), Ok()] * 3, Ok(), Ok(), Ok(), Rejected(10001)]
        port.complete.return_value = Ok()
        report = {}
        await check_writes(port, Created("c", "m"), report)
        self.assertEqual(report["phase"], "finished")
        self.assertEqual(report["inner_characters"], 1800)
        self.assertTrue(report["concurrent_10020_informational"])
        port.complete.assert_awaited_once_with("c", "m", "done")
        snapshots = [call.args[2] for call in port.update.await_args_list]
        self.assertIs(snapshots[-3], snapshots[-2])
        times = [s.timestamp for s in snapshots]
        self.assertEqual(times, sorted(times))
        self.assertEqual(len(set(times)), len(times) - 1)

    async def test_unknown_receipts_do_not_retry(self):
        port = AsyncMock(spec=ProcessMessages)
        port.update.return_value = Unknown()
        port.complete.return_value = Unknown()
        report = {}
        with self.assertRaises(GateFailed):
            await check_writes(port, Created("c", "m"), report)
        port.update.assert_awaited_once()
        port.complete.assert_awaited_once_with("c", "m", "error")
        self.assertEqual(report["phase"], "budget")
        self.assertIsNone(report["complete_code"])

    async def test_all_overlapping_writes_can_succeed(self):
        port = AsyncMock(spec=ProcessMessages)
        port.update.side_effect = [Ok()] * 10 + [Rejected(10001)]
        port.complete.return_value = Ok()
        report = {}
        await check_writes(port, Created("c", "m"), report)
        self.assertEqual(report["post_complete_code"], 10001)
        self.assertFalse(report["concurrent_10020_informational"])
        self.assertEqual(report["phase"], "finished")

    async def test_unexpected_concurrent_code_fails_the_gate(self):
        port = AsyncMock(spec=ProcessMessages)
        port.update.side_effect = [Ok(), *[Ok()] * 5, Rejected(10001)]
        report = {}
        port.complete.return_value = Ok()
        with self.assertRaises(GateFailed):
            await check_writes(port, Created("c", "m"), report)
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
