from __future__ import annotations

import argparse
import io
import json
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from lark_channel import Identity, OutboundText

from scripts import probe_feishu_completion_mention as probe


class CompletionMentionProbeTest(unittest.IsolatedAsyncioTestCase):
    def args(self, **overrides):
        return argparse.Namespace(**{
            "config": Path("nonexistent.yaml"), "chat_id": "oc_probe",
            "user_id": "ou_owner", "reply_to_message_id": None,
            "reply_in_thread": False, "delay_seconds": 0, "dry_run": False,
            **overrides,
        })

    def sent(self, message_id, *, thread_id=None, root_id=None, parent_id=None):
        return SimpleNamespace(
            success=True, message_id=message_id,
            raw={"code": 0, "data": {
                "message_id": message_id, "chat_id": "oc_probe",
                "thread_id": thread_id, "root_id": root_id, "parent_id": parent_id,
            }},
        )

    def channel(self, *, reply=False, topic=False, target_topic=False):
        thread_id = "omt_topic" if topic else None
        root_id = "om_root" if target_topic else "om_source"
        return SimpleNamespace(
            send=AsyncMock(side_effect=[
                self.sent(
                    "om_probe", thread_id=thread_id,
                    root_id=root_id if topic else ("om_older_root" if reply else None),
                    parent_id="om_source" if reply else None,
                ),
                self.sent(
                    "om_mention", thread_id=thread_id,
                    root_id=root_id if topic else ("om_older_root" if reply else "om_probe"),
                    parent_id=root_id if topic else "om_probe",
                ),
            ]),
            update_card=AsyncMock(return_value=SimpleNamespace(success=True)),
            fetch_message=AsyncMock(return_value={"code": 0, "data": {"items": [{
                "message_id": "om_source", "chat_id": "oc_probe",
                "thread_id": thread_id if target_topic else None,
                "root_id": root_id if target_topic else None,
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

    async def test_dry_run_previews_card_and_quoted_mention_without_settings_or_network(self):
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
        self.assertNotIn("<at ", json.dumps(result["completion_update"]))
        self.assertNotIn("<at ", json.dumps(result["redraw_preview"]))
        mention = result["completion_mention"]
        self.assertEqual([user["open_id"] for user in mention["mentions"]], ["ou_owner"])
        self.assertEqual(mention["opts"]["reply_to"], "<sent.message_id>")
        self.assertIs(mention["opts"]["reply_in_thread"], False)
        self.assertEqual(mention["opts"]["reply_target_gone"], "fail")
        self.assertTrue(mention["opts"]["uuid"])
        self.assertNotIn("turn-file.", json.dumps(result))
        self.assertFalse(result["client_notification_verified"])
        with self.assertRaises(ValueError):
            await probe._probe(self.args(dry_run=True, user_id="all"))

    async def test_success_replies_to_card_in_same_scope_without_attesting_notification(self):
        for reply, topic, target_topic in (
            (False, False, False), (True, False, False),
            (True, True, False), (True, True, True),
        ):
            with self.subTest(reply=reply, topic=topic, target_topic=target_topic):
                channel = self.channel(reply=reply, topic=topic, target_topic=target_topic)
                calls = Mock()
                calls.attach_mock(channel.send, "send")
                calls.attach_mock(channel.update_card, "update_card")
                result, sleep = await self.run_probe(
                    channel, delay_seconds=15,
                    reply_to_message_id="om_source" if reply else None,
                    reply_in_thread=topic,
                )
                self.assertTrue(result["send_success"])
                self.assertTrue(result["completion_update_success"])
                self.assertTrue(result["completion_mention_success"])
                self.assertFalse(result["client_notification_verified"])
                self.assertEqual(result["message_id"], "om_probe")
                self.assertEqual(result["completion_mention_message_id"], "om_mention")
                self.assertEqual([call[0] for call in calls.mock_calls], ["send", "update_card", "send"])
                self.assertEqual(channel.send.await_count, 2)
                destination, initial, opts = channel.send.await_args_list[0].args
                self.assertEqual(destination, "oc_probe")
                self.assertEqual(opts.receive_id_type, "chat_id")
                self.assertEqual(opts.reply_target_gone, "fail")
                self.assertEqual(opts.reply_to, "om_source" if reply else None)
                self.assertEqual(opts.reply_in_thread, True if topic else None)
                self.assertNotIn("<at ", json.dumps(initial.card))
                sleep.assert_awaited_once_with(15)
                channel.update_card.assert_awaited_once()
                message_id, completed = channel.update_card.await_args.args
                self.assertEqual(message_id, "om_probe")
                self.assertNotIn("<at ", json.dumps(completed))
                destination, mention, opts = channel.send.await_args_list[1].args
                self.assertEqual(destination, "oc_probe")
                self.assertIsInstance(mention, OutboundText)
                self.assertEqual(mention.mentions, [Identity(open_id="ou_owner")])
                self.assertEqual(opts.reply_to, "om_probe")
                self.assertIs(opts.reply_in_thread, topic)
                self.assertEqual(opts.reply_target_gone, "fail")
                self.assertEqual(opts.uuid, probe._mention(self.args(), "om_probe")[1].uuid)
                channel.stop.assert_called_once()
                if reply:
                    channel.fetch_message.assert_awaited_once_with("om_source")
                else:
                    channel.fetch_message.assert_not_awaited()

    async def test_unconfirmed_results_and_exceptions_do_not_leak_or_retry(self):
        for stage in ("send", "update", "mention"):
            for raises in (False, True):
                with self.subTest(stage=stage, raises=raises):
                    channel = self.channel()
                    failure = RuntimeError("test-only-do-not-print") if raises else SimpleNamespace(
                        success=False, raw={"credential": "test-only-do-not-print"},
                    )
                    if stage == "send":
                        channel.send.side_effect = [failure]
                    elif stage == "mention":
                        channel.send.side_effect = [self.sent("om_probe"), failure]
                    elif raises:
                        channel.update_card.side_effect = failure
                    else:
                        channel.update_card.return_value = failure
                    result, sleep = await self.run_probe(channel)
                    self.assertIn("error", result)
                    self.assertEqual(result["completion_update_success"], stage == "mention")
                    self.assertFalse(result["completion_mention_success"])
                    self.assertFalse(result["client_notification_verified"])
                    self.assertNotIn("test-only-do-not-print", json.dumps(result))
                    self.assertEqual(channel.send.await_count, 2 if stage == "mention" else 1)
                    if stage == "send":
                        channel.update_card.assert_not_awaited()
                        sleep.assert_not_awaited()
                    else:
                        channel.update_card.assert_awaited_once()
                    channel.stop.assert_called_once()

    async def test_wrong_chat_or_missing_topic_flag_rejects_before_send(self):
        for wrong_chat in (False, True):
            with self.subTest(wrong_chat=wrong_chat):
                channel = self.channel(reply=True, topic=True, target_topic=True)
                if wrong_chat:
                    channel.fetch_message.return_value["data"]["items"][0]["chat_id"] = "oc_other"
                result, _ = await self.run_probe(
                    channel, reply_to_message_id="om_source", reply_in_thread=wrong_chat,
                )
                self.assertIn("error", result)
                channel.send.assert_not_awaited()
                channel.update_card.assert_not_awaited()
                channel.stop.assert_called_once()

    async def test_unconfirmed_initial_identity_stops_before_update_and_mention(self):
        for mismatch in ("chat_id", "message_id", "parent_id", "thread_id", "chunks"):
            with self.subTest(mismatch=mismatch):
                channel = self.channel()
                sent = self.sent("om_probe")
                if mismatch == "chunks":
                    sent.chunk_ids = ["om_probe", "om_other"]
                else:
                    sent.raw["data"][mismatch] = "unexpected"
                channel.send.side_effect = [sent]
                result, sleep = await self.run_probe(channel)
                self.assertIn("error", result)
                self.assertFalse(result["send_success"])
                self.assertFalse(result["completion_mention_success"])
                channel.send.assert_awaited_once()
                channel.update_card.assert_not_awaited()
                sleep.assert_not_awaited()

    async def test_unconfirmed_mention_identity_does_not_retry_or_claim_delivery(self):
        for topic in (False, True):
            for mismatch in ("chat_id", "message_id", "parent_id", "root_id", "thread_id", "chunks"):
                with self.subTest(topic=topic, mismatch=mismatch):
                    channel = self.channel(reply=topic, topic=topic, target_topic=topic)
                    sent = self.sent(
                        "om_probe", thread_id="omt_topic" if topic else None,
                        root_id="om_root" if topic else None,
                        parent_id="om_source" if topic else None,
                    )
                    mentioned = self.sent(
                        "om_mention", thread_id="omt_topic" if topic else None,
                        root_id="om_root" if topic else "om_probe", parent_id="om_probe",
                    )
                    if mismatch == "chunks":
                        mentioned.chunk_ids = ["om_mention", "om_other"]
                    else:
                        mentioned.raw["data"][mismatch] = (
                            None if (
                                mismatch == "root_id" or (mismatch == "parent_id" and topic)
                            ) else "unexpected"
                        )
                    channel.send.side_effect = [sent, mentioned]
                    result, _ = await self.run_probe(
                        channel, reply_to_message_id="om_source" if topic else None,
                        reply_in_thread=topic,
                    )
                    self.assertIn("error", result)
                    self.assertTrue(result["completion_update_success"])
                    self.assertFalse(result["completion_mention_success"])
                    self.assertFalse(result["client_notification_verified"])
                    self.assertEqual(channel.send.await_count, 2)
                    channel.update_card.assert_awaited_once()

    def test_mention_uuid_is_stable_for_exact_card_and_recipient(self):
        opts = probe._mention(self.args(), "om_probe")[1]
        self.assertEqual(opts.uuid, probe._mention(self.args(), "om_probe")[1].uuid)
        for args, message_id in (
            (self.args(), "om_other"), (self.args(user_id="ou_other"), "om_probe"),
            (self.args(chat_id="oc_other"), "om_probe"),
        ):
            self.assertNotEqual(opts.uuid, probe._mention(args, message_id)[1].uuid)

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
