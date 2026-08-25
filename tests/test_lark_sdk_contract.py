from __future__ import annotations

import asyncio
import importlib.metadata
import inspect
import json
import time
import unittest

from lark_channel import (
    CardActionEvent,
    ChatInfo,
    ChatQueueConfig,
    Conversation,
    Events,
    FeishuChannel,
    Identity,
    InboundConfig,
    InboundPipeline,
    InboundMessage,
    MediaSource,
    OutboundFile,
    OutboundImage,
    PolicyConfig,
    SafetyPipeline,
    SendOpts,
    SendResult,
    TextBatchConfig,
    TextContent,
)
from lark_channel.channel.normalize.pipeline import PipelineConfig, PipelineDeps
from lark_channel.channel.quote import QuoteResolver
from lark_oapi.api.im.v1 import (
    GetMessageRequest,
    GetMessageResponse,
    ListMessageRequest,
    ListMessageResponse,
)
from openai_codex import ImageInput, TextInput

from netizen.image_inputs import image_references
from netizen.quoted_context import (
    interactive_quote_visible_text,
    quoted_message_id,
)


class LarkSdkContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_lark_oapi_message_history_generated_contract(self) -> None:
        self.assertEqual(importlib.metadata.version("lark-oapi"), "1.7.2")

        exact = (
            GetMessageRequest.builder()
            .message_id("om_exact")
            .user_id_type("open_id")
            .with_sender_name(True)
            .build()
        )
        self.assertEqual(exact.uri, "/open-apis/im/v1/messages/:message_id")
        self.assertEqual(exact.message_id, "om_exact")
        self.assertEqual(exact.user_id_type, "open_id")
        self.assertTrue(exact.with_sender_name)

        history = (
            ListMessageRequest.builder()
            .container_id_type("thread")
            .container_id("omt_topic")
            .sort_type("ByCreateTimeDesc")
            .page_size(50)
            .with_sender_name(True)
            .page_token("next-page")
            .build()
        )
        self.assertEqual(history.uri, "/open-apis/im/v1/messages")
        self.assertEqual(history.container_id_type, "thread")
        self.assertEqual(history.container_id, "omt_topic")
        self.assertEqual(history.sort_type, "ByCreateTimeDesc")
        self.assertEqual(history.page_size, 50)
        self.assertTrue(history.with_sender_name)
        self.assertEqual(history.page_token, "next-page")
        self.assertIsNone(history.start_time)
        self.assertIsNone(history.end_time)

        raw_item = {
            "message_id": "om_exact",
            "chat_id": "oc_group",
            "thread_id": "omt_topic",
            "create_time": "1700000000123",
            "msg_type": "text",
            "deleted": False,
            "sender": {
                "id": "ou_alice",
                "id_type": "open_id",
                "sender_type": "user",
                "sender_name": "Alice",
            },
        }
        exact_response = GetMessageResponse(
            {"code": 0, "msg": "success", "data": {"items": [raw_item]}}
        )
        list_response = ListMessageResponse(
            {
                "code": 0,
                "msg": "success",
                "data": {
                    "items": [raw_item],
                    "has_more": True,
                    "page_token": "next-page",
                },
            }
        )
        self.assertTrue(exact_response.success())
        self.assertEqual(exact_response.data.items[0].message_id, "om_exact")
        self.assertEqual(exact_response.data.items[0].sender.sender_name, "Alice")
        self.assertTrue(list_response.success())
        self.assertTrue(list_response.data.has_more)
        self.assertEqual(list_response.data.page_token, "next-page")

    async def test_public_identity_fields_and_inbound_id_aliases(self) -> None:
        sender = Identity(
            open_id="ou_current",
            union_id="on_current",
            user_id="user_current",
            display_name="Alice",
            is_bot=False,
            sender_type="user",
        )
        message = InboundMessage(
            id="om_current",
            create_time=0,
            conversation=Conversation(chat_id="oc_chat", chat_type="group"),
            sender=sender,
        )

        self.assertEqual(
            {
                name: getattr(message.sender, name)
                for name in (
                    "open_id",
                    "union_id",
                    "user_id",
                    "display_name",
                    "is_bot",
                    "sender_type",
                )
            },
            {
                "open_id": "ou_current",
                "union_id": "on_current",
                "user_id": "user_current",
                "display_name": "Alice",
                "is_bot": False,
                "sender_type": "user",
            },
        )
        self.assertEqual(message.message_id, message.id)
        self.assertEqual(message.sender_id, message.sender.open_id)

    async def test_public_send_exposes_topic_opts_and_result_shape(self) -> None:
        self.assertTrue(inspect.iscoroutinefunction(FeishuChannel.send))
        self.assertEqual(
            tuple(inspect.signature(FeishuChannel.send).parameters),
            ("self", "to", "message", "opts"),
        )
        opts = SendOpts(
            receive_id_type="chat_id",
            reply_to="om_root",
            reply_in_thread=True,
            reply_target_gone="fail",
            uuid="side-seed-contract",
        )
        self.assertEqual(opts.reply_to, "om_root")
        self.assertTrue(opts.reply_in_thread)
        self.assertEqual(opts.reply_target_gone, "fail")
        self.assertEqual(SendOpts().reply_target_gone, "fresh")
        result = SendResult(
            success=True,
            message_id="om_seed",
            raw={
                "code": 0,
                "data": {
                    "message_id": "om_seed",
                    "chat_id": "oc_chat",
                    "thread_id": "omt_side",
                    "root_id": "om_root",
                    "parent_id": "om_root",
                },
            },
        )
        self.assertEqual(result.raw["data"]["thread_id"], "omt_side")

        source = MediaSource(kind="file", path="/project/report.xlsx")
        file_message = OutboundFile(source=source, file_name="report.xlsx")
        image_message = OutboundImage(source=source)
        self.assertIs(file_message.source, source)
        self.assertEqual(file_message.file_name, "report.xlsx")
        self.assertIs(image_message.source, source)

    async def test_p2p_topic_normalization_retains_underlying_chat_type(self) -> None:
        pipeline = InboundPipeline(
            PipelineConfig(inbound=InboundConfig(include_raw=True)),
            PipelineDeps(),
        )
        message = await pipeline.normalize(
            message_event={
                "message_id": "om_p2p_topic",
                "chat_id": "oc_p2p",
                "chat_type": "p2p",
                "thread_id": "omt_p2p",
                "root_id": "om_root",
                "parent_id": "om_root",
                "message_type": "text",
                "content": json.dumps({"text": "hello"}),
            },
            sender={"sender_id": {"open_id": "ou_user"}},
        )
        assert message is not None
        self.assertEqual(message.conversation.chat_type, "p2p")
        self.assertEqual(message.conversation.thread_id, "omt_p2p")
        self.assertEqual(message.raw["root_id"], "om_root")

    async def test_card_action_and_public_scope_recovery_surfaces_are_present(
        self,
    ) -> None:
        self.assertEqual(Events.CARD_ACTION, "cardAction")
        self.assertNotIn("topic_id", CardActionEvent.__dataclass_fields__)
        self.assertTrue(inspect.iscoroutinefunction(FeishuChannel.fetch_message))
        self.assertTrue(
            inspect.iscoroutinefunction(FeishuChannel.fetch_inbound_message)
        )
        self.assertTrue(
            inspect.iscoroutinefunction(FeishuChannel.fetch_quoted_context)
        )
        self.assertTrue(inspect.iscoroutinefunction(FeishuChannel.get_chat_info))
        self.assertTrue(inspect.iscoroutinefunction(FeishuChannel.update_card))
        self.assertTrue(inspect.iscoroutinefunction(FeishuChannel.add_reaction))
        self.assertEqual(
            tuple(inspect.signature(FeishuChannel.add_reaction).parameters),
            ("self", "message_id", "emoji_type"),
        )
        self.assertTrue(inspect.iscoroutinefunction(FeishuChannel.remove_reaction))
        self.assertEqual(
            tuple(inspect.signature(FeishuChannel.remove_reaction).parameters),
            ("self", "message_id", "reaction_id"),
        )
        self.assertTrue(
            inspect.iscoroutinefunction(FeishuChannel.download_resource)
        )
        self.assertEqual(
            tuple(inspect.signature(FeishuChannel.download_resource).parameters),
            ("self", "file_key", "resource_type", "message_id"),
        )
        self.assertEqual(
            ChatInfo("oc_test", raw={"chat_mode": "p2p"}).chat_mode,
            "p2p",
        )

    async def test_public_image_and_post_resources_map_to_native_image_inputs(
        self,
    ) -> None:
        pipeline = InboundPipeline(
            PipelineConfig(inbound=InboundConfig(include_raw=True)),
            PipelineDeps(),
        )

        async def normalize(
            message_id: str,
            message_type: str,
            content: dict[str, object],
        ) -> InboundMessage:
            result = await pipeline.normalize(
                message_event={
                    "message_id": message_id,
                    "chat_id": "oc_chat",
                    "chat_type": "p2p",
                    "message_type": message_type,
                    "content": json.dumps(content),
                },
                sender={"sender_id": {"open_id": "ou_user"}},
            )
            assert result is not None
            return result

        image = await normalize(
            "om_image",
            "image",
            {"image_key": "img_one"},
        )
        post = await normalize(
            "om_post",
            "post",
            {
                "zh_cn": {
                    "title": "Report",
                    "content": [
                        [
                            {"tag": "text", "text": "before "},
                            {"tag": "img", "image_key": "img_one"},
                            {"tag": "text", "text": " after "},
                            {"tag": "img", "image_key": "img_two"},
                        ]
                    ],
                }
            },
        )
        post_v2 = await normalize(
            "om_post_v2",
            "post",
            {
                "zh_cn": {
                    "title": "Report v2",
                    "content_v2": [
                        [
                            {"tag": "text", "text": "before "},
                            {"tag": "img", "image_key": "img_v2_only"},
                        ]
                    ],
                }
            },
        )
        literal_text = await normalize(
            "om_post_literal_text",
            "post",
            {
                "zh_cn": {
                    "content_v2": [[
                        {
                            "tag": "text",
                            "text": "literal ![not an image](img_text)",
                        }
                    ]]
                }
            },
        )
        fenced_markdown = await normalize(
            "om_post_fenced_markdown",
            "post",
            {
                "zh_cn": {
                    "content_v2": [[
                        {
                            "tag": "md",
                            "text": "```md\n![not an image](img_code)\n```",
                        }
                    ]]
                }
            },
        )
        unclosed_fenced_markdown = await normalize(
            "om_post_unclosed_fenced_markdown",
            "post",
            {
                "zh_cn": {
                    "content_v2": [[
                        {
                            "tag": "md",
                            "text": "```md\n![not an image](img_unclosed_code)",
                        }
                    ]]
                }
            },
        )

        self.assertEqual(
            [(resource.type, resource.file_key) for resource in image.resources],
            [("image", "img_one")],
        )
        self.assertEqual(
            [(resource.type, resource.file_key) for resource in post.resources],
            [("image", "img_one"), ("image", "img_two")],
        )
        self.assertIn("![image](img_one)", post.content_text)
        self.assertIn("![image](img_two)", post.content_text)
        # Pinned SDK 1.2.0 renders content_v2's image marker but omits its
        # ResourceDescriptor. Netizen recovers only that public rendered key;
        # remove this assertion when the upstream resources contract is fixed.
        self.assertEqual(post_v2.resources, [])
        self.assertEqual(
            [
                reference.file_key
                for reference in image_references(
                    post_v2,
                    source="current_message",
                )
            ],
            ["img_v2_only"],
        )
        # Literal Markdown-looking text and fenced code are rendered into
        # content_text but are not downloadable post image nodes.
        self.assertIn("img_text", literal_text.content_text)
        self.assertIn("img_code", fenced_markdown.content_text)
        self.assertEqual(literal_text.resources, [])
        self.assertEqual(fenced_markdown.resources, [])
        self.assertEqual(
            image_references(literal_text, source="current_message"),
            (),
        )
        self.assertEqual(
            image_references(fenced_markdown, source="current_message"),
            (),
        )
        # SDK 1.2.0 treats an unmatched opening fence as ordinary Markdown
        # and emits a descriptor. Netizen follows fenced-code semantics and
        # refuses to turn a code sample into a pixel download.
        self.assertEqual(
            [resource.file_key for resource in unclosed_fenced_markdown.resources],
            ["img_unclosed_code"],
        )
        self.assertEqual(
            image_references(
                unclosed_fenced_markdown,
                source="current_message",
            ),
            (),
        )
        native = [TextInput("context"), ImageInput("data:image/png;base64,AA==")]
        self.assertEqual(native[0].text, "context")
        self.assertTrue(native[1].url.startswith("data:image/png;base64,"))

    async def test_disabled_chat_queue_dispatches_same_chat_topics_concurrently(
        self,
    ) -> None:
        first_entered = asyncio.Event()
        second_entered = asyncio.Event()
        release_first = asyncio.Event()

        async def dispatch(message: InboundMessage) -> None:
            if message.id == "message-first":
                first_entered.set()
                await release_first.wait()
            elif message.id == "message-second":
                second_entered.set()

        pipeline = SafetyPipeline(
            loop=asyncio.get_running_loop(),
            on_message=dispatch,
            policy=PolicyConfig(group_policy="open", require_mention=False),
            batch_config=TextBatchConfig(delay_ms=0, long_delay_ms=0),
            queue_config=ChatQueueConfig(
                enabled=False,
                merge_while_busy=False,
            ),
        )

        def message(message_id: str, thread_id: str) -> InboundMessage:
            return InboundMessage(
                id=message_id,
                create_time=int(time.time()),
                conversation=Conversation(
                    chat_id="oc_same_group",
                    chat_type="topic",
                    thread_id=thread_id,
                ),
                sender=Identity(open_id="ou_user"),
                content=TextContent(text=message_id),
                content_text=message_id,
                body_text=message_id,
            )

        first_task = asyncio.create_task(
            pipeline.push_message(message("message-first", "omt_topic_one"))
        )
        second_task: asyncio.Task[None] | None = None
        try:
            await asyncio.wait_for(first_entered.wait(), timeout=0.1)
            second_task = asyncio.create_task(
                pipeline.push_message(
                    message("message-second", "omt_topic_two")
                )
            )
            await asyncio.wait_for(second_entered.wait(), timeout=0.1)
        finally:
            release_first.set()
            tasks = [first_task]
            if second_task is not None:
                tasks.append(second_task)
            await asyncio.gather(*tasks, return_exceptions=True)
            await pipeline.dispose()

    async def test_enabled_chat_queue_serializes_same_chat_topics(self) -> None:
        first_entered = asyncio.Event()
        second_entered = asyncio.Event()
        release_first = asyncio.Event()

        async def dispatch(message: InboundMessage) -> None:
            if message.id == "message-first":
                first_entered.set()
                await release_first.wait()
            elif message.id == "message-second":
                second_entered.set()

        pipeline = SafetyPipeline(
            loop=asyncio.get_running_loop(),
            on_message=dispatch,
            policy=PolicyConfig(group_policy="open", require_mention=False),
            batch_config=TextBatchConfig(delay_ms=0, long_delay_ms=0),
            queue_config=ChatQueueConfig(
                enabled=True,
                merge_while_busy=False,
            ),
        )

        def message(message_id: str, thread_id: str) -> InboundMessage:
            return InboundMessage(
                id=message_id,
                create_time=int(time.time()),
                conversation=Conversation(
                    chat_id="oc_same_group",
                    chat_type="topic",
                    thread_id=thread_id,
                ),
                sender=Identity(open_id="ou_user"),
                content=TextContent(text=message_id),
                content_text=message_id,
                body_text=message_id,
            )

        try:
            await pipeline.push_message(
                message("message-first", "omt_topic_one")
            )
            await asyncio.wait_for(first_entered.wait(), timeout=0.1)
            await pipeline.push_message(
                message("message-second", "omt_topic_two")
            )
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(second_entered.wait(), timeout=0.05)
            release_first.set()
            await asyncio.wait_for(second_entered.wait(), timeout=0.1)
        finally:
            release_first.set()
            await pipeline.dispose()

    async def test_version_pinned_reply_relation_gap_and_compatibility_boundary(
        self,
    ) -> None:
        self.assertEqual(importlib.metadata.version("lark-channel-sdk"), "1.2.0")
        pipeline = InboundPipeline(
            PipelineConfig(
                inbound=InboundConfig(
                    expand_merge_forward=False,
                    fetch_interactive_card=False,
                    include_raw=True,
                )
            ),
            PipelineDeps(),
        )

        async def normalize(
            message_id: str,
            *,
            parent_id: str,
            root_id: str,
            thread_id: str | None = None,
        ) -> InboundMessage:
            result = await pipeline.normalize(
                message_event={
                    "message_id": message_id,
                    "parent_id": parent_id,
                    "root_id": root_id,
                    "thread_id": thread_id,
                    "chat_id": "oc_chat",
                    "chat_type": "group",
                    "message_type": "text",
                    "content": json.dumps({"text": "request"}),
                },
                sender={"sender_id": {"open_id": "ou_user"}},
            )
            assert result is not None
            return result

        first_level = await normalize(
            "om_first_level",
            parent_id="om_parent",
            root_id="om_parent",
        )
        nested = await normalize(
            "om_nested",
            parent_id="om_parent",
            root_id="om_root",
        )
        topic = await normalize(
            "om_topic",
            parent_id="om_root",
            root_id="om_root",
            thread_id="omt_topic",
        )

        # Removal trigger: when this becomes non-None in a fixed SDK, delete
        # the 1.2.0 raw relation adapter instead of updating the assertion.
        self.assertIsNone(first_level.reply)
        self.assertEqual(
            quoted_message_id(first_level, sdk_version="1.2.0"),
            "om_parent",
        )
        self.assertEqual(nested.reply.message_id, "om_parent")
        self.assertEqual(quoted_message_id(nested), "om_parent")
        self.assertIsNone(topic.reply)
        self.assertIsNone(quoted_message_id(topic, sdk_version="1.2.0"))

    async def test_interactive_v1_gap_and_v2_visible_text_contract(self) -> None:
        pipeline = InboundPipeline(
            PipelineConfig(
                inbound=InboundConfig(
                    expand_merge_forward=False,
                    fetch_interactive_card=False,
                )
            ),
            PipelineDeps(),
        )

        async def normalize(message_id: str, card: dict[str, object]) -> InboundMessage:
            result = await pipeline.normalize(
                message_event={
                    "message_id": message_id,
                    "chat_id": "oc_chat",
                    "chat_type": "group",
                    "message_type": "interactive",
                    "content": json.dumps(card),
                },
                sender={"sender_id": {"open_id": "ou_user"}},
            )
            assert result is not None
            return result

        v1 = await normalize(
            "om_v1",
            {
                "elements": [
                    {
                        "tag": "div",
                        "text": {"tag": "plain_text", "content": "Legacy body"},
                    }
                ]
            },
        )
        v2 = await normalize(
            "om_v2",
            {
                "schema": "2.0",
                "header": {
                    "title": {"tag": "plain_text", "content": "Card title"}
                },
                "body": {
                    "elements": [
                        {"tag": "markdown", "content": "Visible body"}
                    ]
                },
            },
        )

        # The public quote-context fallback remains necessary only for this
        # pinned v1/default-card placeholder.
        self.assertEqual(v1.content_text, "[interactive]")
        self.assertEqual(v2.content_text, "Card title\nVisible body")

    async def test_interactive_v1_placeholder_survives_production_refetch(self) -> None:
        v1_card = {
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "plain_text", "content": "Legacy body"},
                }
            ]
        }

        async def fetch_message(message_id: str) -> dict[str, object]:
            self.assertEqual(message_id, "om_v1")
            return {
                "data": {
                    "items": [
                        {"body": {"content": json.dumps(v1_card)}}
                    ]
                }
            }

        pipeline = InboundPipeline(
            PipelineConfig(
                inbound=InboundConfig(
                    expand_merge_forward=False,
                    fetch_interactive_card=True,
                )
            ),
            PipelineDeps(fetch_message=fetch_message),
        )
        result = await pipeline.normalize(
            message_event={
                "message_id": "om_v1",
                "chat_id": "oc_chat",
                "chat_type": "group",
                "message_type": "interactive",
                "content": json.dumps({"elements": []}),
            },
            sender={"sender_id": {"open_id": "ou_user"}},
        )

        assert result is not None
        self.assertEqual(result.content.card_version, "v1")
        self.assertEqual(result.content_text, "[interactive]")

    async def test_quote_flattener_cardkit_v2_gap_and_adapter_boundary(self) -> None:
        card = {
            "schema": "2.0",
            "header": {
                "title": {"tag": "plain_text", "content": "Card title"}
            },
            "body": {
                "elements": [
                    {"tag": "markdown", "content": "Visible body"}
                ]
            },
        }

        async def fetch_message(message_id: str) -> dict[str, object]:
            self.assertEqual(message_id, "om_cardkit_v2")
            return {
                "data": {
                    "items": [
                        {
                            "message_id": message_id,
                            "msg_type": "interactive",
                            "body": {"content": json.dumps(card)},
                        }
                    ]
                }
            }

        context = await QuoteResolver(fetcher=fetch_message).fetch_quoted_context(
            "om_cardkit_v2"
        )

        assert context is not None
        # Removal trigger: when the pinned SDK returns visible text here,
        # delete the raw CardKit adapter instead of weakening this assertion.
        self.assertEqual(context.text, "")
        self.assertEqual(
            interactive_quote_visible_text(context, sdk_version="1.2.0"),
            "Card title\nVisible body",
        )


if __name__ == "__main__":
    unittest.main()
