from __future__ import annotations

import argparse
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from lark_channel import OutboundCard, OutboundPost

from scripts import probe_feishu_partial_answers as probe


class PartialAnswerProbeTest(unittest.IsolatedAsyncioTestCase):
    def args(self, *, dry_run=False):
        return argparse.Namespace(config=Path("/unused/probe/config.yaml"), chat_id="oc_synthetic_probe", dry_run=dry_run)

    async def test_dry_run_never_reads_config_and_covers_projection_boundaries(self):
        with patch.object(probe.Settings, "from_file") as config, patch.object(probe, "FeishuChannel") as channel:
            result = await probe._probe(self.args(dry_run=True))
        config.assert_not_called()
        channel.assert_not_called()
        self.assertTrue(result["payload_validation"])
        self.assertEqual(len(result["cards"]) + len(result["posts"]), 11)
        self.assertEqual(len(result["posts"]), 5)
        for scenario in result["cards"]:
            initial = json.dumps(scenario["states"][0]["card"], ensure_ascii=False)
            if scenario["scenario"] != "Files v5":
                self.assertNotIn("partialanswerv1", initial)
            if scenario["scenario"].startswith("Goal"):
                last = json.dumps(scenario["states"][-1]["card"], ensure_ascii=False)
                self.assertIn("手动恢复后的新执行段答案", last)
                self.assertNotIn("第一物理 Turn 的稳定答案", last)
                self.assertNotIn("第二物理 Turn 的稳定答案", last)
                paused = next(state for state in scenario["states"] if state["step"] == "pause_retains_partials")
                self.assertNotIn("任务已完成", json.dumps(paused["card"], ensure_ascii=False))
        self.assertTrue(all(post["mentions"] == [] for post in result["posts"]))
        self.assertFalse(result["client_visual"])
        self.assertFalse(result["client_callback"])
        self.assertFalse(result["native_e2e"])

    def channel(self, *, wrong_chat=False, omit_body=False):
        messages = {}
        async def send(chat_id, content, opts):
            message_id = f"om_probe_{len(messages)}"
            messages[message_id] = content.card if isinstance(content, OutboundCard) else {"markdown": content.markdown}
            return SimpleNamespace(success=True, message_id=message_id, raw={"code": 0})
        async def update(message_id, content):
            messages[message_id] = content
            return SimpleNamespace(success=True, raw={"code": 0})
        async def fetch(message_id):
            item = {"message_id": message_id, "chat_id": "oc_wrong" if wrong_chat else "oc_synthetic_probe"}
            if not omit_body:
                item["body"] = {"content": json.dumps(messages[message_id], ensure_ascii=False)}
            return {"code": 0, "data": {"items": [item]}}
        return SimpleNamespace(send=AsyncMock(side_effect=send), update_card=AsyncMock(side_effect=update),
            fetch_message=AsyncMock(side_effect=fetch), stop=Mock())

    async def test_transport_probe_sends_eleven_messages_and_refetches_every_update(self):
        for omit_body in (False, True):
            with self.subTest(omit_body=omit_body):
                channel = self.channel(omit_body=omit_body)
                with patch.object(probe.Settings, "from_file", return_value=SimpleNamespace(
                    app_id="cli_synthetic", app_secret="secret-not-for-output")), patch.object(probe, "FeishuChannel", return_value=channel) as factory:
                    result = await probe._probe(self.args())
                self.assertTrue(result["success"])
                self.assertEqual((result["sent_messages"], result["updated_messages"]), (11, 20))
                self.assertEqual(channel.fetch_message.await_count, 31)
                self.assertTrue(all(op["exact_message_refetch"] for op in result["operations"]))
                file_update = next(op for op in result["operations"] if op["scenario"] == "Files v5" and op["action"] == "update")
                self.assertEqual(file_update["refetched_partial_preserved"], None if omit_body else True)
                self.assertEqual(factory.call_args.kwargs["outbound"].retry.max_attempts, 1)
                posts = [call.args[1] for call in channel.send.await_args_list if isinstance(call.args[1], OutboundPost)]
                self.assertEqual(len(posts), 5)
                self.assertTrue(all(post.mentions == [] for post in posts))
                channel.stop.assert_called_once()
                self.assertNotIn("secret-not-for-output", json.dumps(result))

    async def test_wrong_destination_stops_and_preserves_confirmed_send_count_without_raw_data(self):
        channel = self.channel(wrong_chat=True)
        with patch.object(probe.Settings, "from_file", return_value=SimpleNamespace(
            app_id="cli_synthetic", app_secret="secret-not-for-output")), patch.object(probe, "FeishuChannel", return_value=channel):
            result = await probe._probe(self.args())
        self.assertFalse(result["success"])
        self.assertEqual(result["send_attempts"], 1)
        self.assertEqual(result["sent_messages"], 1)
        self.assertFalse(result["operations"][0]["exact_message_refetch"])
        channel.update_card.assert_not_awaited()
        channel.stop.assert_called_once()
        output = json.dumps(result)
        self.assertNotIn("secret-not-for-output", output)
        self.assertNotIn("oc_wrong", output)

    async def test_only_rich_text_never_renders_or_sends_cards(self):
        args = probe._parse_args(["--config", "/unused/probe/config.yaml", "--chat-id", "oc_synthetic_probe",
            "--dry-run", "--only-rich-text"])
        with patch.object(probe, "_card_scenarios", side_effect=AssertionError("must not render cards")), patch.object(
            probe.Settings, "from_file", side_effect=AssertionError("must not read config")):
            dry = await probe._probe(args)
        self.assertEqual(dry["planned_messages"], 5)
        self.assertEqual(dry["cards"], [])
        self.assertEqual(len(dry["posts"]), 5)
        args.dry_run = False
        channel = self.channel()
        with patch.object(probe, "_card_scenarios", side_effect=AssertionError("must not render cards")), patch.object(
            probe.Settings, "from_file", return_value=SimpleNamespace(app_id="cli_synthetic", app_secret="test")), patch.object(
            probe, "FeishuChannel", return_value=channel):
            result = await probe._probe(args)
        self.assertTrue(result["success"])
        self.assertEqual((result["sent_messages"], result["updated_messages"]), (5, 0))
        self.assertEqual(channel.fetch_message.await_count, 5)
        channel.update_card.assert_not_awaited()
        self.assertTrue(all(isinstance(call.args[1], OutboundPost) for call in channel.send.await_args_list))
        self.assertFalse(result["local_v5_callback_roundtrip"])

    async def test_known_upgrade_placeholder_is_unverifiable_but_other_missing_markers_fail(self):
        placeholder = {"title": "任务已完成\n本轮文件 9 个 · 第 2/2 页", "elements": [[
            {"tag": "img", "image_key": "img_synthetic"},
            {"tag": "text", "text": "请升级至最新版本客户端，以查看内容"}, {"tag": "text", "text": ""},
        ]]}
        for body, unavailable in (
            (placeholder, True),
            ({**placeholder, "extra": "not the known placeholder"}, False),
            ({"title": "任务已完成", "elements": [[{"tag": "text", "text": "different missing content"}]]}, False),
        ):
            with self.subTest(unavailable=unavailable):
                channel = SimpleNamespace(fetch_message=AsyncMock(return_value={"code": 0, "data": {"items": [{
                    "message_id": "om_synthetic", "chat_id": "oc_synthetic_probe",
                    "body": {"content": json.dumps(body, ensure_ascii=False)},
                }]}}))
                if unavailable:
                    self.assertIsNone(await probe._refetch(channel, "oc_synthetic_probe", "om_synthetic",
                        expected_partial=probe._FILES_MARKER))
                else:
                    with self.assertRaisesRegex(RuntimeError, "did not retain"):
                        await probe._refetch(channel, "oc_synthetic_probe", "om_synthetic",
                            expected_partial=probe._FILES_MARKER)
