from __future__ import annotations

import argparse
import io
import json
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from scripts import probe_feishu_completion_mention as probe


class CompletionMentionProbeTest(unittest.IsolatedAsyncioTestCase):
    def args(self, **overrides):
        return argparse.Namespace(**{
            "config": Path("nonexistent.yaml"), "chat_id": "oc_probe",
            "user_id": "ou_owner", "reply_to_message_id": None,
            "reply_in_thread": False, "delay_seconds": 0, "dry_run": False,
            **overrides,
        })

    def channel(self):
        return SimpleNamespace(
            send=AsyncMock(return_value=SimpleNamespace(
                success=True, message_id="om_probe",
            )),
            update_card=AsyncMock(return_value=SimpleNamespace(success=True)),
            fetch_message=AsyncMock(return_value={"data": {"items": [{
                "message_id": "om_source", "chat_id": "oc_probe", "thread_id": "omt_topic",
            }]}}),
            stop=Mock(),
        )

    async def run_probe(self, channel, **overrides):
        with (
            patch.object(probe.Settings, "from_file", return_value=SimpleNamespace(
                app_id="cli_test", app_secret="test-only-do-not-print",
            )),
            patch.object(probe, "FeishuChannel", return_value=channel),
            patch.object(probe.asyncio, "sleep", new_callable=AsyncMock) as sleep,
            redirect_stderr(io.StringIO()),
        ):
            result = await probe._probe(self.args(**overrides))
        return result, sleep

    async def test_dry_run_previews_three_payloads_without_settings_or_network(self):
        with (
            patch.object(probe.Settings, "from_file") as settings,
            patch.object(probe, "FeishuChannel") as channel,
            patch.object(probe.asyncio, "sleep", new_callable=AsyncMock) as sleep,
        ):
            result = await probe._probe(self.args(dry_run=True))
        settings.assert_not_called()
        channel.assert_not_called()
        sleep.assert_not_awaited()
        self.assertNotIn("<at ", json.dumps(result["send"]))
        self.assertIn("<at id=ou_owner></at>", json.dumps(result["completion_update"]))
        self.assertNotIn("<at ", json.dumps(result["redraw_preview"]))
        self.assertNotIn("turn-file.", json.dumps(result))
        self.assertFalse(result["client_notification_verified"])
        with self.assertRaises(ValueError):
            await probe._probe(self.args(dry_run=True, user_id="all"))

    async def test_success_updates_one_exact_card_and_never_attests_notification(self):
        for reply in (False, True):
            with self.subTest(reply=reply):
                channel = self.channel()
                result, sleep = await self.run_probe(
                    channel, delay_seconds=15,
                    reply_to_message_id="om_source" if reply else None,
                    reply_in_thread=reply,
                )
                self.assertTrue(result["send_success"])
                self.assertTrue(result["completion_update_success"])
                self.assertFalse(result["client_notification_verified"])
                self.assertEqual(result["message_id"], "om_probe")
                channel.send.assert_awaited_once()
                destination, initial, opts = channel.send.await_args.args
                self.assertEqual(destination, "oc_probe")
                self.assertEqual(opts.receive_id_type, "chat_id")
                self.assertEqual(opts.reply_target_gone, "fail")
                self.assertEqual(opts.reply_to, "om_source" if reply else None)
                self.assertEqual(opts.reply_in_thread, True if reply else None)
                self.assertNotIn("<at ", json.dumps(initial.card))
                sleep.assert_awaited_once_with(15)
                channel.update_card.assert_awaited_once()
                message_id, completed = channel.update_card.await_args.args
                self.assertEqual(message_id, "om_probe")
                self.assertIn("<at id=ou_owner></at>", json.dumps(completed))
                channel.stop.assert_called_once()
                if reply:
                    channel.fetch_message.assert_awaited_once_with("om_source")
                else:
                    channel.fetch_message.assert_not_awaited()

    async def test_unconfirmed_results_and_exceptions_do_not_leak_or_retry(self):
        for stage in ("send", "update"):
            for raises in (False, True):
                with self.subTest(stage=stage, raises=raises):
                    channel = self.channel()
                    operation = channel.send if stage == "send" else channel.update_card
                    if raises:
                        operation.side_effect = RuntimeError("test-only-do-not-print")
                    else:
                        operation.return_value = SimpleNamespace(
                            success=False, raw={"credential": "test-only-do-not-print"},
                        )
                    result, sleep = await self.run_probe(channel)
                    self.assertIn("error", result)
                    self.assertFalse(result["completion_update_success"])
                    self.assertFalse(result["client_notification_verified"])
                    self.assertNotIn("test-only-do-not-print", json.dumps(result))
                    channel.send.assert_awaited_once()
                    if stage == "send":
                        channel.update_card.assert_not_awaited()
                        sleep.assert_not_awaited()
                    else:
                        channel.update_card.assert_awaited_once()
                    channel.stop.assert_called_once()

    async def test_wrong_chat_or_missing_topic_flag_rejects_before_send(self):
        for wrong_chat in (False, True):
            with self.subTest(wrong_chat=wrong_chat):
                channel = self.channel()
                if wrong_chat:
                    channel.fetch_message.return_value["data"]["items"][0]["chat_id"] = "oc_other"
                result, _ = await self.run_probe(
                    channel, reply_to_message_id="om_source", reply_in_thread=wrong_chat,
                )
                self.assertIn("error", result)
                channel.send.assert_not_awaited()
                channel.update_card.assert_not_awaited()
                channel.stop.assert_called_once()

    def test_argument_validation_also_applies_to_dry_run(self):
        for overrides in (
            {"chat_id": "ou_user"}, {"user_id": "all"},
            {"reply_to_message_id": "om_bad\n"}, {"reply_in_thread": True},
            {"delay_seconds": -1}, {"delay_seconds": 61},
            {"delay_seconds": float("nan")},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                probe._validate_args(self.args(dry_run=True, **overrides))
        parsed = probe._parse_args([
            "--config", "nonexistent.yaml", "--chat-id", "oc_probe",
            "--user-id", "ou_owner", "--dry-run",
        ])
        self.assertEqual(parsed.delay_seconds, 15)
