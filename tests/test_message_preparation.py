from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from lark_channel import InboundPipeline, InteractiveContent, QuotedContext, TextContent
from lark_channel.channel.normalize.pipeline import PipelineConfig, PipelineDeps
from lark_channel.channel.quote import QuoteResolver

from netizen.message_content import UnsupportedHistoricalMessage
from netizen.message_preparation import (
    MessagePreparationError,
    prepare_message_content,
)


class MessagePreparationTest(unittest.IsolatedAsyncioTestCase):
    def message(self, content=None, text="[interactive]"):
        content = content or InteractiveContent(card={})
        return SimpleNamespace(
            id="om_exact", content=content,
            raw_content_type=content.kind, content_text=text,
        )

    async def test_already_normalized_text_and_card_do_not_read_again(self):
        channel = SimpleNamespace(fetch_quoted_context=AsyncMock())
        for message in (
            self.message(TextContent(text="plain"), "plain"),
            self.message(text="visible card"),
        ):
            with self.subTest(kind=message.content.kind):
                self.assertIsNone(await prepare_message_content(
                    channel, message, timeout_seconds=1,
                ))
        channel.fetch_quoted_context.assert_not_awaited()

    async def test_legacy_card_is_rejected_before_placeholder_fallback(self):
        channel = SimpleNamespace(fetch_quoted_context=AsyncMock())
        for text in ("[interactive]", "SDK only extracted the title"):
            with self.subTest(text=text):
                content = InteractiveContent(card={"elements": []}, card_version="v1")
                with self.assertRaisesRegex(UnsupportedHistoricalMessage, "不支持飞书 1.0"):
                    await prepare_message_content(
                        channel, self.message(content, text), timeout_seconds=1,
                    )
        channel.fetch_quoted_context.assert_not_awaited()

    async def test_real_sdk_fallback_cannot_accept_v1_after_initial_fetch_failure(self):
        async def failed_initial_fetch(message_id):
            raise RuntimeError("temporary fetch failure")

        pipeline = InboundPipeline(
            PipelineConfig(), PipelineDeps(fetch_message=failed_initial_fetch),
        )
        message = await pipeline.normalize(
            message_event={
                "message_id": "om_exact", "chat_id": "oc_chat",
                "chat_type": "group", "message_type": "interactive", "content": "{}",
            },
            sender={"sender_id": {"open_id": "ou_user"}},
        )
        self.assertEqual(message.content.card_version, "unknown")
        self.assertEqual(message.content_text, "[interactive]")

        async def fetch_legacy_card(message_id):
            return {"data": {"items": [{
                "message_id": message_id, "msg_type": "interactive",
                "body": {"content": json.dumps({"elements": [{
                    "tag": "div", "text": {"tag": "plain_text", "content": "Legacy body"},
                }]})},
            }]}}

        resolver = QuoteResolver(fetcher=fetch_legacy_card)
        fallback = await resolver.fetch_quoted_context("om_exact")
        self.assertEqual(fallback.text, "Legacy body")
        channel = SimpleNamespace(fetch_quoted_context=AsyncMock(return_value=fallback))
        with self.assertRaisesRegex(UnsupportedHistoricalMessage, "不支持飞书 1.0"):
            await prepare_message_content(channel, message, timeout_seconds=1)
        channel.fetch_quoted_context.assert_awaited_once_with("om_exact")

    async def test_placeholder_uses_public_exact_card_fallback(self):
        channel = SimpleNamespace(fetch_quoted_context=AsyncMock(return_value=QuotedContext(
            message_id="om_exact", content_type="interactive", text="Visible content",
        )))
        self.assertEqual(await prepare_message_content(
            channel, self.message(), timeout_seconds=1,
        ), "Visible content")
        channel.fetch_quoted_context.assert_awaited_once_with("om_exact")

    async def test_missing_id_does_not_query(self):
        channel = SimpleNamespace(fetch_quoted_context=AsyncMock())
        message = self.message()
        message.id = ""
        with self.assertRaises(MessagePreparationError) as raised:
            await prepare_message_content(channel, message, timeout_seconds=1)
        self.assertEqual(raised.exception.reason, "identity")
        channel.fetch_quoted_context.assert_not_awaited()

    async def test_mismatched_fallback_is_never_used(self):
        for fallback in (
            None,
            QuotedContext(message_id="om_wrong", content_type="interactive", text="x"),
            QuotedContext(message_id="om_exact", content_type="text", text="x"),
        ):
            with self.subTest(fallback=fallback):
                channel = SimpleNamespace(fetch_quoted_context=AsyncMock(return_value=fallback))
                with self.assertRaises(MessagePreparationError) as raised:
                    await prepare_message_content(channel, self.message(), timeout_seconds=1)
                self.assertEqual(raised.exception.reason, "identity")

    async def test_fetch_failure_and_timeout_are_typed_and_never_retried(self):
        for error, reason in ((RuntimeError("denied"), "unavailable"),
                              (TimeoutError(), "timeout")):
            with self.subTest(reason=reason):
                channel = SimpleNamespace(fetch_quoted_context=AsyncMock(side_effect=error))
                with self.assertRaises(MessagePreparationError) as raised:
                    await prepare_message_content(channel, self.message(), timeout_seconds=1)
                self.assertEqual(raised.exception.reason, reason)
                channel.fetch_quoted_context.assert_awaited_once()

    async def test_cancellation_is_not_converted_to_a_content_failure(self):
        channel = SimpleNamespace(fetch_quoted_context=AsyncMock(side_effect=asyncio.CancelledError))
        with self.assertRaises(asyncio.CancelledError):
            await prepare_message_content(channel, self.message(), timeout_seconds=1)

    async def test_fallback_request_has_its_own_deadline(self):
        async def never_returns(message_id):
            await asyncio.Event().wait()

        channel = SimpleNamespace(fetch_quoted_context=AsyncMock(side_effect=never_returns))
        with self.assertRaises(MessagePreparationError) as raised:
            await prepare_message_content(channel, self.message(), timeout_seconds=0.001)
        self.assertEqual(raised.exception.reason, "timeout")
