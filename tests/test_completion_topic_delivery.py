from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from lark_channel import OutboundConfig, OutboundSender, RetryConfig

from netizen.channel.completion_mentions import send_completion_mention
from netizen.domain import FeishuScope, ScopeKind


class CompletionTopicDeliveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_public_sdk_serializes_real_at_in_a_card_topic_reply(self):
        driver = SimpleNamespace(
            reply_message=AsyncMock(return_value={
                "code": 0,
                "data": {
                    "message_id": "om_reminder", "chat_id": "oc_chat",
                    "thread_id": "omt_card", "root_id": "om_older_root",
                    "parent_id": "om_result_card",
                },
            }),
            create_message=AsyncMock(),
        )
        sender = OutboundSender(driver, OutboundConfig(retry=RetryConfig(max_attempts=1)))

        async def send(to, content, opts):
            return await sender.send(
                content, receive_id=to, receive_id_type=opts.receive_id_type,
                reply_to=opts.reply_to, reply_in_thread=opts.reply_in_thread,
                reply_target_gone=opts.reply_target_gone, uuid_=opts.uuid,
            )

        confirmed = await send_completion_mention(
            SimpleNamespace(send=send),
            scope=FeishuScope("cli_app", "oc_chat", ScopeKind.GROUP),
            card_message_id="om_result_card", user_id="ou_owner", operation_id="turn-one",
        )
        self.assertTrue(confirmed)
        driver.reply_message.assert_awaited_once()
        request = driver.reply_message.call_args.kwargs
        self.assertEqual(request["message_id"], "om_result_card")
        self.assertEqual(request["msg_type"], "text")
        self.assertTrue(request["reply_in_thread"])
        self.assertTrue(request["uuid"])
        text = json.loads(request["content"])["text"]
        self.assertEqual(text.count('<at user_id="ou_owner">'), 1)
        self.assertIn("本轮任务已结束。", text)
        driver.create_message.assert_not_awaited()

    async def test_receipts_must_confirm_the_expected_card_topic_without_resending(self):
        base = {
            "message_id": "om_reminder", "chat_id": "oc_chat",
            "thread_id": "omt_topic", "root_id": "om_older_root", "parent_id": "om_card",
        }
        cases = (
            (ScopeKind.GROUP, {}, True),
            (ScopeKind.GROUP, {"parent_id": "om_user"}, False),
            (ScopeKind.TOPIC, {"parent_id": "om_topic_root"}, True),
            (ScopeKind.TOPIC, {"thread_id": "omt_wrong"}, False),
            (ScopeKind.GROUP, {"chat_id": "oc_wrong"}, False),
            (ScopeKind.GROUP, {"root_id": None}, False),
            (ScopeKind.GROUP, {"thread_id": None}, False),
            (ScopeKind.GROUP, {"message_id": "om_mismatch"}, False),
        )
        for kind, changes, expected in cases:
            with self.subTest(kind=kind, changes=changes):
                channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(
                    success=True, message_id="om_reminder", chunk_ids=(),
                    raw={"code": 0, "data": base | changes},
                )))
                kwargs = dict(
                    scope=FeishuScope("cli_app", "oc_chat", kind, "omt_topic" if kind is ScopeKind.TOPIC else None),
                    card_message_id="om_card", user_id="ou_owner", operation_id="turn-one",
                )
                if expected:
                    result = await send_completion_mention(channel, **kwargs)
                else:
                    with self.assertLogs("netizen.channel.completion_mentions", level="WARNING"):
                        result = await send_completion_mention(channel, **kwargs)
                self.assertEqual(result, expected)
                channel.send.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
