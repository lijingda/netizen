from __future__ import annotations

import asyncio
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from lark_channel import InboundPipeline, InteractiveContent, QuotedContext, TextContent
from lark_channel.channel.normalize.pipeline import PipelineConfig, PipelineDeps
from lark_channel.channel.quote import QuoteResolver

from netizen_cli.message_content import HistoricalMessageUnavailable, project_message_content
from netizen_cli.message_preparation import MessagePreparationError, prepare_message_content


def original(text="Full original answer"):
    return {"schema": "2.0", "body": {"elements": [
        {"tag": "markdown", "content": text},
    ]}}


class MessagePreparationTest(unittest.IsolatedAsyncioTestCase):
    def message(self, content=None, text="[interactive]"):
        if content is None:
            content = InteractiveContent(card={})
        return SimpleNamespace(
            id="om_exact", content=content, raw={},
            conversation=SimpleNamespace(chat_id="oc_chat"),
            raw_content_type=content.kind, content_text=text,
            body_text=text, safe_content_text=text,
        )

    def context(self, card, **changes):
        raw = {
            "message_id": "om_exact", "msg_type": "interactive", "chat_id": "oc_chat",
            "body": {"content": json.dumps(card)},
        }
        raw.update(changes)
        return QuotedContext(
            message_id="om_exact", content_type="interactive", text="Untrusted SDK preview",
            raw=raw,
        )

    def channel(self, context=None):
        return SimpleNamespace(fetch_quoted_context=AsyncMock(return_value=context))

    async def test_non_card_does_not_read_again(self):
        channel = self.channel()
        self.assertIsNone(await prepare_message_content(
            channel, self.message(TextContent(text="plain"), "plain"), timeout_seconds=1,
        ))
        channel.fetch_quoted_context.assert_not_awaited()

    async def test_original_card_reuses_raw_and_ignores_sdk_version_and_preview(self):
        for card in (original(), {"elements": [{"tag": "markdown", "content": "Legacy body"}]}):
            with self.subTest(card=card):
                message = self.message(InteractiveContent(card=card, card_version="v1"), "Only title")
                channel = self.channel()
                prepared = await prepare_message_content(channel, message, timeout_seconds=1)
                self.assertIsNotNone(prepared)
                self.assertNotIn("Only title", prepared.text)
                if "schema" not in card:
                    self.assertIn("1.0", prepared.text)
                    self.assertIn("om_exact", prepared.text)
                    self.assertIn("Legacy body", prepared.text)
                else:
                    self.assertTrue(prepared.text.endswith("\nFull original answer"))
                channel.fetch_quoted_context.assert_not_awaited()

    async def test_event_user_dsl_survives_sdk_refetch_and_needs_no_extra_request(self):
        message = self.message(InteractiveContent(
            card={"title": "Summary", "elements": [[{"tag": "text", "text": "Upgrade"}]]},
            raw={"user_dsl": json.dumps(original())},
        ), "Summary")
        channel = self.channel()
        prepared = await prepare_message_content(channel, message, timeout_seconds=1)
        self.assertTrue(prepared.text.endswith("\nFull original answer"))
        channel.fetch_quoted_context.assert_not_awaited()

    async def test_nonempty_summary_never_wins_and_message_is_unchanged(self):
        for extra in ({}, {"locale": "en_us"}):
            with self.subTest(extra=extra):
                message = self.message(InteractiveContent(card={
                    "title": "Task completed", "elements": [[
                        {"tag": "text", "text": "Please upgrade your client"},
                    ]], **extra,
                }, card_version="v1"), "Only the summary title")
                before = copy.deepcopy(message)
                channel = self.channel(self.context(original()))
                prepared = await prepare_message_content(channel, message, timeout_seconds=1)
                result = project_message_content(message, message_type="interactive", prepared_card=prepared)
                self.assertTrue(result["text"].endswith("\nFull original answer"))
                self.assertEqual(vars(message), vars(before))
                channel.fetch_quoted_context.assert_awaited_once_with("om_exact")

    async def test_read_failure_then_original_v1_can_deliver_body(self):
        async def failed_initial_fetch(message_id):
            raise RuntimeError("temporary fetch failure")

        pipeline = InboundPipeline(PipelineConfig(), PipelineDeps(fetch_message=failed_initial_fetch))
        message = await pipeline.normalize(
            message_event={
                "message_id": "om_exact", "chat_id": "oc_chat", "chat_type": "group",
                "message_type": "interactive", "content": "{}",
            },
            sender={"sender_id": {"open_id": "ou_user"}},
        )
        channel = self.channel(self.context({"elements": [{
            "tag": "div", "text": {"tag": "plain_text", "content": "Legacy body"},
        }]}))
        prepared = await prepare_message_content(channel, message, timeout_seconds=1)
        self.assertIn("Legacy body", prepared.text)
        self.assertIn("1.0", prepared.text)
        self.assertEqual(message.content_text, "[interactive]")

    async def test_fetched_target_must_have_its_own_exact_raw_identity(self):
        for change in (
            {"message_id": "om_wrong"}, {"message_id": None},
            {"chat_id": "oc_other"}, {"chat_id": None},
            {"msg_type": "text"}, {"deleted": True},
        ):
            with self.subTest(change=change):
                channel = self.channel(self.context(original(), **change))
                with self.assertRaises(MessagePreparationError) as raised:
                    await prepare_message_content(channel, self.message(), timeout_seconds=1)
                self.assertEqual(raised.exception.reason, "identity")

    async def test_sdk_backfilled_id_does_not_prove_response_identity(self):
        raw = self.context(original()).raw
        del raw["message_id"]
        context = await QuoteResolver(fetcher=lambda mid: {"data": {"items": [raw]}}).fetch_quoted_context("om_exact")
        self.assertEqual(context.message_id, "om_exact")
        with self.assertRaises(MessagePreparationError) as raised:
            await prepare_message_content(self.channel(context), self.message(), timeout_seconds=1)
        self.assertEqual(raised.exception.reason, "identity")

    async def test_sdk_text_without_original_card_evidence_is_not_used(self):
        cases = (
            QuotedContext(message_id="om_exact", content_type="interactive", text="Some text"),
            self.context({"title": "Summary", "elements": [[{"tag": "text", "text": "preview"}]]}),
            self.context({}),
        )
        for context in cases:
            with self.subTest(context=context):
                with self.assertRaises(MessagePreparationError):
                    await prepare_message_content(self.channel(context), self.message(), timeout_seconds=1)

    async def test_hidden_fields_are_excluded_and_structural_limits_still_apply(self):
        card = original()
        card["body"]["elements"].append({
            "tag": "button", "text": {"tag": "plain_text", "content": "Visible label"},
            "value": {"secret": {"tag": "markdown", "content": "Hidden payload"}},
        })
        prepared = await prepare_message_content(self.channel(self.context(card)), self.message(), timeout_seconds=1)
        self.assertIn("Visible label", prepared.text)
        self.assertNotIn("Hidden payload", prepared.text)
        deep = {"tag": "markdown", "content": "Deep body"}
        for _ in range(40):
            deep = {"tag": "column", "elements": [deep]}
        with self.assertRaises(HistoricalMessageUnavailable):
            await prepare_message_content(
                self.channel(self.context({"schema": "2.0", "body": {"elements": [deep]}})),
                self.message(), timeout_seconds=1,
            )

    async def test_request_locale_selects_translation_without_mutating_original(self):
        card = original("Default body")
        card["body"]["elements"][0]["i18n_content"] = {"zh_cn": "中文正文", "en_us": "English body"}
        message = self.message(InteractiveContent(card=card))
        prepared = await prepare_message_content(self.channel(), message, timeout_seconds=1, preferred_locale="zh_cn")
        self.assertTrue(prepared.text.endswith("\n中文正文"))
        self.assertEqual(card["body"]["elements"][0]["content"], "Default body")

    async def test_missing_id_does_not_query(self):
        channel = self.channel()
        message = self.message()
        message.id = ""
        with self.assertRaises(MessagePreparationError) as raised:
            await prepare_message_content(channel, message, timeout_seconds=1)
        self.assertEqual(raised.exception.reason, "identity")
        channel.fetch_quoted_context.assert_not_awaited()

    async def test_fetch_failure_and_timeout_are_typed_and_never_retried(self):
        for error, reason in ((RuntimeError("denied"), "unavailable"), (TimeoutError(), "timeout")):
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

    async def test_original_card_request_has_its_own_deadline(self):
        async def never_returns(message_id):
            await asyncio.Event().wait()

        channel = SimpleNamespace(fetch_quoted_context=AsyncMock(side_effect=never_returns))
        with self.assertRaises(MessagePreparationError) as raised:
            await prepare_message_content(channel, self.message(), timeout_seconds=0.001)
        self.assertEqual(raised.exception.reason, "timeout")
