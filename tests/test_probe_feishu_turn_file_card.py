from __future__ import annotations

import argparse
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from scripts import probe_feishu_turn_file_card as probe


class FileCardCapacityProbeTest(unittest.IsolatedAsyncioTestCase):
    async def test_capacity_probe_rebuilds_selected_page(self) -> None:
        chat_id = "oc_" + "a" * 32
        for count in (18, 100, 400):
            with self.subTest(count=count):
                channel = SimpleNamespace(
                    send=AsyncMock(return_value=SimpleNamespace(
                        success=True, message_id="om_capacity",
                    )),
                    update_card=AsyncMock(return_value=SimpleNamespace(success=True)),
                    fetch_message=AsyncMock(return_value={
                        "data": {"items": [{
                            "message_id": "om_capacity", "chat_id": chat_id,
                        }]},
                    }),
                    stop=Mock(),
                )
                with (
                    patch.object(probe.Settings, "from_file", return_value=SimpleNamespace(
                        app_id="cli_test", app_secret="test-only",
                    )),
                    patch.object(probe, "FeishuChannel", return_value=channel),
                ):
                    result = await probe._probe(argparse.Namespace(
                        config=Path("unused.yaml"), chat_id=chat_id, count=count,
                    ))
                self.assertEqual(result["pagination"], "select")
                self.assertLessEqual(result["card_json_bytes"], 55_000)
                self.assertTrue(result["full_card_update_success"])
                self.assertFalse(result["live_client_callback_verified"])
                channel.update_card.assert_awaited_once()
                message_id, card = channel.update_card.await_args.args
                self.assertEqual(message_id, "om_capacity")
                page_value = probe._page_value(card)
                self.assertEqual(page_value["page"], 1)
                self.assertEqual(page_value["pagination"], "select")
                channel.stop.assert_called_once()
