from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from lark_channel import (
    Conversation,
    Identity,
    ImageContent,
    InboundMessage,
    InteractiveContent,
    TextContent,
)

from netizen.channel.input_preparation import MessageInputPreparer
from netizen.domain import FeishuScope, MessageContextAnchor, ScopeKind
from netizen.image_inputs import ImageInputUnavailable, ImageReference
from netizen.message_history import (
    MessageHistoryRef,
    MessageHistoryStats,
    MessageHistoryUnavailable,
    MessageHistoryWindow,
)
from netizen.prompt_projection import (
    PromptProjectionError,
    ScheduledInputProjection,
    project_current_message,
    render_plain_prompt,
)


class FeedbackOnlyAnchor:
    """The scheduler's anchor is not an SDK inbound message or request body."""

    def __getattr__(self, name):
        raise AssertionError(f"scheduled feedback anchor must not be decoded: {name}")


class MessageInputPreparerTest(unittest.IsolatedAsyncioTestCase):
    """The preparation boundary needs readers, not an application or runtime."""

    def setUp(self) -> None:
        self.scope = FeishuScope("cli_test", "oc_group", ScopeKind.GROUP)
        self.lower = MessageContextAnchor("om_lower", 1_000)
        self.upper = MessageContextAnchor("om_current", 4_000)
        self.source = self.message(self.upper.message_id, self.upper.create_time_ms)
        self.current = project_current_message(
            self.source,
            expected_message_id=self.source.id,
            expected_sender_id="ou_user",
            message_type="text",
            content_fidelity="full_text",
            request_text=self.source.content_text,
        )
        self.channel = SimpleNamespace(
            fetch_inbound_message=AsyncMock(),
            fetch_quoted_context=AsyncMock(),
            download_resource=AsyncMock(),
        )
        self.preparer = MessageInputPreparer(channel=self.channel)

    def message(
        self, message_id: str, created_at: int, *, chat_id="oc_group", content=None,
    ) -> InboundMessage:
        content = content or TextContent(text="请总结 $reports")
        text = content.text if isinstance(content, TextContent) else "[interactive]"
        return InboundMessage(
            id=message_id,
            create_time=created_at,
            conversation=Conversation(chat_id=chat_id, chat_type="group"),
            sender=Identity(
                open_id="ou_user", display_name="Participant",
                sender_type="user", is_bot=False,
            ),
            content=content,
            content_text=text,
            body_text=text,
            raw_content_type=content.kind,
            raw={},
        )

    def reader(self, *references: MessageHistoryRef):
        window = MessageHistoryWindow(
            lower=self.lower,
            upper=self.upper,
            candidates=references,
            stats=MessageHistoryStats(
                pages_scanned=1,
                raw_messages_scanned=len(references) + 2,
                duplicate_messages=0,
                ignored_after_upper=0,
                omitted_messages=0,
                truncated_before=False,
                scan_limit_hit=False,
            ),
        )
        return SimpleNamespace(read_window=AsyncMock(return_value=window))

    async def catch_up(self, reader, *, quoted_target_id=None):
        return await self.preparer.prepare_catch_up(
            source_message=self.source,
            scope=self.scope,
            lower=self.lower,
            upper_id=self.upper.message_id,
            quoted_target_id=quoted_target_id,
            current=self.current,
            current_images=(),
            message_history=reader,
        )

    async def test_current_only_returns_native_input_without_context_commit_data(self):
        prepared = await self.preparer.prepare(
            source_message=self.source,
            quoted_target_id=None,
            current=self.current,
            current_images=(),
        )

        self.assertEqual(prepared.native_input, render_plain_prompt(self.current))
        self.assertIsNone(prepared.context_anchor)
        self.assertIsNone(prepared.context_stats)
        self.channel.fetch_inbound_message.assert_not_awaited()
        self.channel.fetch_quoted_context.assert_not_awaited()
        self.channel.download_resource.assert_not_awaited()

    def scheduled(self):
        self.source = FeedbackOnlyAnchor()
        self.current = ScheduledInputProjection(
            message_id=self.upper.message_id,
            request_text="$live-skill 继续检查进度",
            plan={"id": "plan-one", "title": "$metadata-skill"},
        )

    async def test_scheduled_current_only_uses_saved_instructions_without_decoding_anchor(self):
        self.scheduled()

        prepared = await self.preparer.prepare(
            source_message=self.source, quoted_target_id=None, current=self.current,
        )

        self.assertEqual(prepared.native_input, render_plain_prompt(self.current))
        self.assertIsNone(prepared.context_anchor)
        self.assertIsNone(prepared.context_stats)
        self.channel.fetch_inbound_message.assert_not_awaited()
        self.channel.fetch_quoted_context.assert_not_awaited()
        self.channel.download_resource.assert_not_awaited()

    async def test_scheduled_catch_up_uses_exact_window_with_inert_history(self):
        self.scheduled()
        reference = MessageHistoryRef("om_history", 2_000, "ou_user", "text")
        reader = self.reader(reference)
        self.channel.fetch_inbound_message.return_value = self.message("om_history", 2_000)

        prepared = await self.catch_up(reader)

        reader.read_window.assert_awaited_once_with(
            self.scope, self.lower, self.upper.message_id,
        )
        self.assertEqual(prepared.context_anchor, self.upper)
        self.assertEqual(prepared.context_stats.selected_count, 1)
        envelope = json.loads(prepared.native_input)
        self.assertEqual(envelope["supplemental_messages"][0]["text"], "请总结 $reports")
        self.assertIn(r"\u0024reports", prepared.native_input)
        self.assertIn(r"\u0024metadata-skill", prepared.native_input)
        self.assertIn("$live-skill", prepared.native_input)
        self.assertEqual(envelope["current_message"]["kind"], "scheduled_plan")
        self.assertEqual(envelope["current_message"]["request_text"], self.current.request_text)
        self.assertNotIn("sender", envelope["current_message"])
        self.assertNotIn("quoted_message", envelope)

    async def test_scheduled_catch_up_preserves_supplemental_image_input(self):
        self.scheduled()
        reference = MessageHistoryRef("om_image", 2_000, "ou_user", "image")
        self.channel.fetch_inbound_message.return_value = self.message(
            "om_image", 2_000, content=ImageContent(image_key="img_history"),
        )
        self.channel.download_resource.return_value = b"\x89PNG\r\n\x1a\ncontent"

        prepared = await self.catch_up(self.reader(reference))

        self.channel.download_resource.assert_awaited_once_with(
            "img_history", resource_type="image", message_id="om_image",
        )
        self.assertEqual(len(prepared.native_input), 3)
        label = json.loads(prepared.native_input[0].text)
        self.assertEqual(label["source"], "supplemental_message")
        self.assertEqual(label["ref"], "img1")
        envelope = json.loads(prepared.native_input[-1].text)
        self.assertEqual(
            envelope["supplemental_messages"][0]["attachments"],
            [{"type": "image", "ref": "img1"}],
        )
        self.assertEqual(envelope["current_message"]["kind"], "scheduled_plan")
        self.assertEqual(prepared.context_anchor, self.upper)

    async def test_scheduled_catch_up_drops_old_image_before_downloading_at_message_limit(self):
        self.scheduled()
        references = [MessageHistoryRef("om_old_image", 2_000, "ou_user", "image")]
        messages = {
            "om_old_image": self.message(
                "om_old_image", 2_000, content=ImageContent(image_key="img_old"),
            ),
        }
        for index in range(50):
            reference = MessageHistoryRef(f"om_text_{index}", 2_001 + index, "ou_user", "text")
            references.append(reference)
            messages[reference.message_id] = self.message(
                reference.message_id, reference.create_time_ms,
            )
        self.channel.fetch_inbound_message.side_effect = messages.__getitem__

        prepared = await self.catch_up(self.reader(*references))

        self.assertEqual(prepared.context_stats.selected_count, 50)
        self.assertTrue(prepared.context_stats.message_limit_reached)
        self.assertTrue(json.loads(prepared.native_input)["context_status"]["truncated"])
        self.channel.download_resource.assert_not_awaited()

    async def test_scheduled_catch_up_keeps_selected_history_failures_fatal(self):
        self.scheduled()
        reference = MessageHistoryRef("om_image", 2_000, "ou_user", "image")
        self.channel.fetch_inbound_message.return_value = self.message(
            "om_image", 2_000, content=ImageContent(image_key="img_history"),
        )
        self.channel.download_resource.return_value = None

        with self.assertRaises(ImageInputUnavailable):
            await self.catch_up(self.reader(reference))

    async def test_scheduled_quote_and_current_images_are_rejected_before_reads(self):
        self.scheduled()
        reader = self.reader()
        for quote, images in (
            ("om_quote", ()),
            (None, (ImageReference("current_message", "om_current", "img_anchor"),)),
        ):
            with self.subTest(quote=quote, images=images):
                arguments = dict(
                    source_message=self.source, quoted_target_id=quote,
                    current=self.current, current_images=images,
                )
                with self.assertRaises(PromptProjectionError):
                    await self.preparer.prepare(**arguments)
                with self.assertRaises(PromptProjectionError):
                    await self.preparer.prepare_catch_up(
                        **arguments, scope=self.scope, lower=self.lower,
                        upper_id=self.upper.message_id, message_history=reader,
                    )
        reader.read_window.assert_not_awaited()
        self.channel.fetch_inbound_message.assert_not_awaited()
        self.channel.fetch_quoted_context.assert_not_awaited()
        self.channel.download_resource.assert_not_awaited()

    async def test_scheduled_catch_up_rejects_upper_id_different_from_anchor(self):
        self.scheduled()
        reader = self.reader()

        with self.assertRaises(PromptProjectionError):
            await self.preparer.prepare_catch_up(
                source_message=self.source, quoted_target_id=None, current=self.current,
                scope=self.scope, lower=self.lower, upper_id="om_wrong",
                message_history=reader,
            )
        reader.read_window.assert_not_awaited()

    async def test_catch_up_returns_exact_anchor_and_stats_without_duplicate_quote_read(self):
        reference = MessageHistoryRef("om_history", 2_000, "ou_user", "text")
        reader = self.reader(reference)
        self.channel.fetch_inbound_message.return_value = self.message("om_history", 2_000)

        prepared = await self.catch_up(reader, quoted_target_id="om_history")

        self.assertEqual(prepared.context_anchor, self.upper)
        self.assertEqual(prepared.context_stats.selected_count, 0)
        self.assertEqual(prepared.context_stats.quoted_deduplicated_count, 1)
        envelope = json.loads(prepared.native_input)
        self.assertEqual(envelope["supplemental_messages"], [])
        self.assertEqual(envelope["quoted_message"]["text"], "请总结 $reports")
        self.assertIn("\\u0024reports", prepared.native_input)
        self.assertEqual(envelope["current_message"]["request_text"], self.current.request_text)
        reader.read_window.assert_awaited_once_with(
            self.scope, self.lower, self.upper.message_id,
        )
        self.channel.fetch_inbound_message.assert_awaited_once_with("om_history")
        self.channel.download_resource.assert_not_awaited()

    async def test_wrong_scope_is_rejected_before_card_fallback_or_resource_reads(self):
        reference = MessageHistoryRef("om_card", 2_000, "ou_user", "interactive")
        self.channel.fetch_inbound_message.return_value = self.message(
            reference.message_id,
            reference.create_time_ms,
            chat_id="oc_other",
            content=InteractiveContent(card={}, card_version="v2"),
        )

        with self.assertRaises(MessageHistoryUnavailable):
            await self.catch_up(self.reader(reference))

        self.channel.fetch_quoted_context.assert_not_awaited()
        self.channel.download_resource.assert_not_awaited()

    async def test_candidate_failure_cancels_and_joins_other_pending_reads(self):
        pending_started = asyncio.Event()
        pending_finished = asyncio.Event()

        async def fetch(message_id):
            if message_id == "om_fails":
                await pending_started.wait()
                raise OSError("unavailable")
            pending_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                pending_finished.set()

        self.channel.fetch_inbound_message.side_effect = fetch
        reader = self.reader(
            MessageHistoryRef("om_fails", 2_000, "ou_user", "text"),
            MessageHistoryRef("om_pending", 3_000, "ou_user", "text"),
        )
        async with asyncio.timeout(2):
            with self.assertRaises(MessageHistoryUnavailable):
                await self.catch_up(reader)

        self.assertTrue(pending_finished.is_set())
        self.channel.fetch_quoted_context.assert_not_awaited()
        self.channel.download_resource.assert_not_awaited()

    async def test_caller_cancellation_propagates_after_candidate_reads_are_joined(self):
        started = {message_id: asyncio.Event() for message_id in ("om_a", "om_b")}
        finished = set()

        async def fetch(message_id):
            started[message_id].set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.add(message_id)

        self.channel.fetch_inbound_message.side_effect = fetch
        reader = self.reader(
            MessageHistoryRef("om_a", 2_000, "ou_user", "text"),
            MessageHistoryRef("om_b", 3_000, "ou_user", "text"),
        )
        task = asyncio.create_task(self.catch_up(reader))
        try:
            async with asyncio.timeout(2):
                await asyncio.gather(*(event.wait() for event in started.values()))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertEqual(finished, set(started))
        self.channel.fetch_quoted_context.assert_not_awaited()
        self.channel.download_resource.assert_not_awaited()
