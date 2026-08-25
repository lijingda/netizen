from __future__ import annotations

import json
import unittest

from lark_channel import (
    AudioContent,
    CalendarContent,
    Conversation,
    FileContent,
    FolderContent,
    GeneralCalendarContent,
    HongbaoContent,
    Identity,
    ImageContent,
    InboundMessage,
    InteractiveContent,
    LocationContent,
    MediaContent,
    Mention,
    MergeForwardContent,
    PostContent,
    ReplyRef,
    ResourceDescriptor,
    QuotedContext,
    ShareCalendarEventContent,
    ShareChatContent,
    ShareUserContent,
    StickerContent,
    SystemContent,
    TextContent,
    TodoContent,
    UnknownContent,
    VideoChatContent,
    VoteContent,
)

from netizen.quoted_context import (
    QuotedMessageContractError,
    QuotedMessageUnavailable,
    UnsupportedQuotedMessage,
    compose_quoted_prompt as _compose_quoted_prompt,
    interactive_quote_visible_text,
    needs_interactive_fallback,
    quoted_message_id,
    validate_quoted_message,
)
from netizen.message_projection import (
    HistoricalMessageContractError,
    HistoricalMessageUnavailable,
    SupplementalContextStats,
    SupplementalMessageOmission,
    compose_message_context_prompt,
    project_quoted_message,
    project_supplemental_message,
    select_supplemental_messages,
)
from netizen.prompt_projection import CurrentMessageProjection, project_identity


def inbound(
    content: object | None = None,
    *,
    message_id: str = "om_quoted",
    chat_id: str = "oc_chat",
    thread_id: str | None = None,
    reply: ReplyRef | None = None,
    raw: dict[str, object] | None = None,
    content_text: str = "quoted text",
    resources: list[ResourceDescriptor] | None = None,
    mentions: list[Mention] | None = None,
    sender: Identity | None = None,
    raw_content_type: str = "",
) -> InboundMessage:
    return InboundMessage(
        id=message_id,
        create_time=123,
        conversation=Conversation(
            chat_id=chat_id,
            chat_type="topic" if thread_id else "group",
            thread_id=thread_id,
        ),
        sender=sender or Identity(open_id="ou_sender", display_name="Alice"),
        mentions=mentions or [],
        reply=reply,
        content=content or TextContent(text=content_text),
        raw=raw or {"message_id": message_id},
        content_text=content_text,
        resources=resources or [],
        body_text=content_text,
        raw_content_type=raw_content_type,
    )


def current(
    request_text: str,
    *,
    sender: Identity | None = None,
) -> CurrentMessageProjection:
    return CurrentMessageProjection(
        message_id="om_current",
        message_type="text",
        content_fidelity="full_text",
        sender=project_identity(
            sender or Identity(open_id="ou_current", display_name="Bob")
        ),
        request_text=request_text,
    )


def compose_quoted_prompt(
    message: InboundMessage,
    current_text: str,
    **kwargs: object,
) -> str:
    return _compose_quoted_prompt(message, current(current_text), **kwargs)


class QuotedRelationTest(unittest.TestCase):
    def test_sdk_120_first_level_gap_is_recovered(self) -> None:
        message = inbound(
            raw={"parent_id": "om_parent", "root_id": "om_parent"}
        )

        self.assertEqual(
            quoted_message_id(message, sdk_version="1.2.0"),
            "om_parent",
        )

    def test_public_nested_reply_is_preferred_and_checked(self) -> None:
        message = inbound(
            reply=ReplyRef("om_parent"),
            raw={"parent_id": "om_parent", "root_id": "om_root"},
        )

        self.assertEqual(quoted_message_id(message), "om_parent")

    def test_public_and_raw_reply_conflict_fails_closed(self) -> None:
        message = inbound(
            reply=ReplyRef("om_public"),
            raw={"parent_id": "om_raw", "root_id": "om_root"},
        )

        with self.assertRaises(QuotedMessageContractError):
            quoted_message_id(message)

    def test_unexplained_missing_reply_ref_fails_closed(self) -> None:
        message = inbound(
            raw={"parent_id": "om_parent", "root_id": "om_root"}
        )

        with self.assertRaises(QuotedMessageContractError):
            quoted_message_id(message, sdk_version="1.2.0")

    def test_changed_sdk_version_requires_compatibility_review(self) -> None:
        message = inbound(
            raw={"parent_id": "om_parent", "root_id": "om_parent"}
        )

        with self.assertRaises(QuotedMessageContractError):
            quoted_message_id(message, sdk_version="1.2.1")

    def test_topic_relation_is_never_a_per_message_quote(self) -> None:
        message = inbound(
            thread_id="omt_topic",
            reply=ReplyRef("om_parent"),
            raw={"parent_id": "om_parent", "root_id": "om_parent"},
        )

        self.assertIsNone(quoted_message_id(message, sdk_version="1.2.0"))

    def test_plain_message_has_no_quote(self) -> None:
        self.assertIsNone(quoted_message_id(inbound()))


class QuotedValidationTest(unittest.TestCase):
    def test_exact_message_in_same_flat_chat_is_accepted(self) -> None:
        validate_quoted_message(
            inbound(),
            expected_message_id="om_quoted",
            expected_chat_id="oc_chat",
        )

    def test_mismatched_id_chat_topic_and_deleted_target_are_rejected(self) -> None:
        cases = [
            inbound(message_id="om_other"),
            inbound(chat_id="oc_other"),
            inbound(thread_id="omt_topic"),
            inbound(raw={"message_id": "om_quoted", "deleted": True}),
            inbound(raw={"message_id": "om_quoted", "is_deleted": "true"}),
        ]
        for message in cases:
            with self.subTest(message=message):
                with self.assertRaises(QuotedMessageUnavailable):
                    validate_quoted_message(
                        message,
                        expected_message_id="om_quoted",
                        expected_chat_id="oc_chat",
                    )


class QuotedProjectionTest(unittest.TestCase):
    def render(self, message: InboundMessage, **kwargs: object) -> dict[str, object]:
        return json.loads(compose_quoted_prompt(message, "current request", **kwargs))

    def test_text_and_post_include_public_metadata_but_not_raw_payload(self) -> None:
        messages = [
            inbound(TextContent(text="hello"), content_text="hello"),
            inbound(
                PostContent(title="Title", text="Body"),
                content_text="# Title\n\nBody",
            ),
        ]
        for message in messages:
            message.reply = ReplyRef(
                "om_previous",
                sender_id="ou_previous_sender",
            )
            message.raw = {
                "message_id": "om_quoted",
                "root_id": "om_raw_root",
                "parent_id": "om_previous",
                "raw_only": "raw_payload_must_not_leak",
            }
            with self.subTest(kind=message.content.kind):
                encoded = compose_quoted_prompt(message, "current request")
                envelope = json.loads(encoded)
                quoted = envelope["quoted_message"]
                self.assertEqual(envelope["version"], 3)
                self.assertEqual(
                    envelope["current_message"]["request_text"],
                    "current request",
                )
                self.assertEqual(
                    envelope["current_message"]["sender"]["open_id"],
                    "ou_current",
                )
                self.assertEqual(quoted["message_id"], "om_quoted")
                self.assertEqual(quoted["conversation"]["chat_id"], "oc_chat")
                self.assertEqual(quoted["sender"]["open_id"], "ou_sender")
                self.assertEqual(quoted["created_at"], 123)
                self.assertEqual(quoted["reply"]["message_id"], "om_previous")
                self.assertEqual(
                    quoted["reply"]["sender_id"],
                    "ou_previous_sender",
                )
                self.assertNotIn("om_raw_root", encoded)
                self.assertNotIn("raw_payload_must_not_leak", encoded)

    def test_quoted_skill_marker_is_inert_but_round_trips(self) -> None:
        encoded = compose_quoted_prompt(
            inbound(content_text="$old-skill historical text"),
            "$new-skill current request",
        )

        quoted_prefix, current_suffix = encoded.split('"current_message"', 1)
        self.assertNotIn("$old-skill", quoted_prefix)
        self.assertIn(r"\u0024old-skill", quoted_prefix)
        self.assertIn("$new-skill", current_suffix)
        decoded = json.loads(encoded)
        self.assertEqual(
            decoded["quoted_message"]["text"],
            "$old-skill historical text",
        )
        self.assertEqual(
            decoded["current_message"]["request_text"],
            "$new-skill current request",
        )

    def test_current_and_quoted_senders_remain_distinct(self) -> None:
        encoded = _compose_quoted_prompt(
            inbound(
                content_text="quoted request",
                sender=Identity(open_id="ou_alice", display_name="Alice"),
            ),
            current(
                "current request",
                sender=Identity(open_id="ou_bob", display_name="Bob"),
            ),
        )

        envelope = json.loads(encoded)
        self.assertEqual(
            envelope["quoted_message"]["sender"],
            {
                "display_name": "Alice",
                "is_bot": False,
                "open_id": "ou_alice",
            },
        )
        self.assertEqual(
            envelope["current_message"]["sender"],
            {
                "display_name": "Bob",
                "is_bot": False,
                "open_id": "ou_bob",
            },
        )
        self.assertIn("attribution only", envelope["handling"])

    def test_all_structured_types_preserve_normalized_visible_text(self) -> None:
        contents = [
            InteractiveContent(card={"schema": "2.0"}, card_version="v2"),
            CalendarContent(summary="invite"),
            GeneralCalendarContent(summary="event"),
            ShareCalendarEventContent(summary="shared"),
            LocationContent(name="office", longitude=1.0, latitude=2.0),
            VideoChatContent(topic="meeting"),
            TodoContent(title="task"),
            VoteContent(topic="choice", options=["A", "B"]),
            HongbaoContent(text="packet"),
        ]
        for content in contents:
            with self.subTest(kind=content.kind):
                message = inbound(content, content_text=f"visible:{content.kind}")
                envelope = self.render(message)
                quoted = envelope["quoted_message"]
                self.assertEqual(quoted["message_type"], content.kind)
                self.assertEqual(quoted["text"], f"visible:{content.kind}")
                self.assertTrue(quoted["content_read"])

    def test_interactive_placeholder_requires_and_accepts_public_fallback(self) -> None:
        message = inbound(
            InteractiveContent(card={}, card_version="v1"),
            content_text="[interactive]",
        )
        self.assertTrue(needs_interactive_fallback(message))
        with self.assertRaises(QuotedMessageUnavailable):
            compose_quoted_prompt(message, "current")

        envelope = json.loads(
            compose_quoted_prompt(
                message,
                "current",
                interactive_fallback_text="Card title\nCard body",
            )
        )
        self.assertEqual(
            envelope["quoted_message"]["text"],
            "Card title\nCard body",
        )

    def test_empty_sdk_text_recovers_only_visible_cardkit_v2_text(self) -> None:
        card = {
            "schema": "2.0",
            "config": {"update_multi": True},
            "header": {
                "title": {"tag": "plain_text", "content": "Netizen 设置"},
                "subtitle": {
                    "tag": "plain_text",
                    "content": "Project Registry",
                },
            },
            "body": {
                "elements": [
                    {
                        "tag": "markdown",
                        "content": "**实例级 Project Registry**\n当前项目列表",
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "刷新"},
                        "value": {"action": "raw_action_must_not_leak"},
                        "confirm": {
                            "title": {
                                "tag": "plain_text",
                                "content": "hidden confirmation",
                            }
                        },
                    },
                    {
                        "tag": "select_static",
                        "placeholder": {
                            "tag": "plain_text",
                            "content": "请选择 Project",
                        },
                        "options": [
                            {
                                "text": {
                                    "tag": "plain_text",
                                    "content": "hidden option",
                                },
                                "value": "hidden_value",
                            }
                        ],
                    },
                ]
            },
        }
        context = QuotedContext(
            message_id="om_card",
            content_type="interactive",
            text="",
            raw={"body": {"content": json.dumps(card)}},
        )

        text = interactive_quote_visible_text(context, sdk_version="1.2.0")

        self.assertEqual(
            text,
            "Netizen 设置\nProject Registry\n"
            "**实例级 Project Registry**\n当前项目列表\n刷新\n请选择 Project",
        )
        self.assertNotIn("raw_action_must_not_leak", text)
        self.assertNotIn("hidden confirmation", text)
        self.assertNotIn("hidden option", text)
        self.assertNotIn("hidden_value", text)

    def test_sdk_quote_text_is_preferred_without_reading_raw_card(self) -> None:
        context = QuotedContext(
            message_id="om_card",
            content_type="interactive",
            text="SDK visible text",
            raw={"body": {"content": "not-json"}},
        )

        self.assertEqual(
            interactive_quote_visible_text(context, sdk_version="9.9.9"),
            "SDK visible text",
        )

    def test_cardkit_raw_adapter_requires_exact_sdk_version(self) -> None:
        context = QuotedContext(
            message_id="om_card",
            content_type="interactive",
            text="",
            raw={
                "body": {
                    "content": json.dumps(
                        {
                            "schema": "2.0",
                            "header": {
                                "title": {
                                    "tag": "plain_text",
                                    "content": "Card title",
                                }
                            },
                            "body": {"elements": []},
                        }
                    )
                }
            },
        )

        with self.assertRaises(QuotedMessageContractError):
            interactive_quote_visible_text(context, sdk_version="1.2.1")

    def test_resource_types_preserve_public_ids_without_reading_content(self) -> None:
        cases = [
            (ImageContent(image_key="img_public"), ("img_public",)),
            (
                FileContent(file_key="file_public", file_name="report.pdf"),
                ("file_public",),
            ),
            (
                FolderContent(file_key="file_folder", file_name="Folder"),
                ("file_folder",),
            ),
            (
                AudioContent(file_key="file_audio", duration_ms=1000),
                ("file_audio",),
            ),
            (
                MediaContent(
                    file_key="file_video",
                    image_key="img_cover",
                    duration_ms=2000,
                    file_name="clip.mp4",
                ),
                ("file_video", "img_cover"),
            ),
            (StickerContent(file_key="file_sticker"), ("file_sticker",)),
            (ShareChatContent(chat_id="oc_shared"), ("oc_shared",)),
            (ShareUserContent(user_id="ou_shared"), ("ou_shared",)),
        ]
        for content, expected_ids in cases:
            with self.subTest(kind=content.kind):
                resources = []
                if content.kind in {"image", "file", "audio", "media", "sticker"}:
                    resources = [
                        ResourceDescriptor(
                            type="video" if content.kind == "media" else content.kind,
                            file_key="file_or_img_public",
                            file_name=getattr(content, "file_name", None),
                            duration_ms=getattr(content, "duration_ms", None),
                            cover_image_key=getattr(content, "image_key", None),
                        )
                    ]
                encoded = compose_quoted_prompt(
                    inbound(content, content_text="opaque", resources=resources),
                    "current request",
                )
                quoted = json.loads(encoded)["quoted_message"]
                self.assertEqual(quoted["content_fidelity"], "metadata_only")
                self.assertFalse(quoted["content_read"])
                for identifier in expected_ids:
                    self.assertIn(identifier, encoded)
                for resource in quoted["resources"]:
                    self.assertFalse(resource["content_read"])
                if resources:
                    self.assertIn("file_or_img_public", encoded)

    def test_image_and_post_mark_successfully_supplied_pixels_as_read(self) -> None:
        image = self.render(
            inbound(
                ImageContent(image_key="img_one"),
                content_text="![image](img_one)",
                resources=[ResourceDescriptor(type="image", file_key="img_one")],
            ),
            read_image_keys={"img_one"},
        )["quoted_message"]
        post = self.render(
            inbound(
                PostContent(text="mixed"),
                content_text="before ![image](img_one) after ![image](img_two)",
                resources=[
                    ResourceDescriptor(type="image", file_key="img_one"),
                    ResourceDescriptor(type="image", file_key="img_two"),
                ],
            ),
            read_image_keys={"img_one", "img_two"},
        )["quoted_message"]

        self.assertEqual(image["content_fidelity"], "full_multimodal")
        self.assertTrue(image["content_read"])
        self.assertIn("像素已作为原生视觉输入", image["text"])
        self.assertTrue(image["resources"][0]["content_read"])
        self.assertEqual(post["content_fidelity"], "full_multimodal")
        self.assertTrue(post["content_read"])
        self.assertTrue(all(item["content_read"] for item in post["resources"]))

        content_v2_gap = self.render(
            inbound(
                PostContent(text="visible v2"),
                content_text="visible ![image](img_v2)",
                resources=[
                    # SDK 1.2.0 may expose an unrendered content-v1 image here.
                    ResourceDescriptor(type="image", file_key="img_hidden_v1")
                ],
            ),
            read_image_keys=["img_v2"],
        )["quoted_message"]
        self.assertEqual(content_v2_gap["content_fidelity"], "full_multimodal")
        self.assertEqual(
            content_v2_gap["resources"],
            [
                {
                    "type": "image",
                    "content_read": True,
                    "file_key": "img_v2",
                }
            ],
        )

    def test_sdk_video_alias_projects_as_media(self) -> None:
        envelope = self.render(
            inbound(
                MediaContent(file_key="file_video"),
                content_text="opaque",
                raw_content_type="video",
            )
        )

        self.assertEqual(envelope["quoted_message"]["message_type"], "media")
        self.assertEqual(
            envelope["quoted_message"]["content_fidelity"],
            "metadata_only",
        )

    def test_merge_forward_is_bounded_aggregate(self) -> None:
        envelope = self.render(
            inbound(
                MergeForwardContent(loading=False, truncated=True),
                content_text="forwarded visible text",
            )
        )

        quoted = envelope["quoted_message"]
        self.assertEqual(quoted["content_fidelity"], "bounded_aggregate")
        self.assertTrue(quoted["truncated"])

    def test_sender_ids_are_minimized_without_redacting_message_metadata(self) -> None:
        message = inbound(
            TextContent(text="ignored"),
            content_text=(
                "ou_sender @_user_1 om_message img_asset file_asset omt_topic"
            ),
            mentions=[
                Mention(
                    "@_user_1",
                    open_id="ou_mentioned",
                    user_id="user_mentioned",
                    union_id="on_mentioned",
                    tenant_key="tenant_public",
                    name="Bob",
                )
            ],
            resources=[
                ResourceDescriptor(
                    type="file",
                    file_key="file_asset",
                    file_name="ou_sender-report.txt",
                )
            ],
            sender=Identity(
                open_id="ou_sender",
                union_id="on_sender",
                user_id="user_sender",
                display_name="ou_sender",
                sender_type="user",
            ),
        )

        encoded = compose_quoted_prompt(message, "current request")
        quoted = json.loads(encoded)["quoted_message"]
        self.assertEqual(
            quoted["text"],
            "ou_sender @_user_1 om_message img_asset file_asset omt_topic",
        )
        self.assertEqual(
            quoted["sender"],
            {
                "display_name": "ou_sender",
                "is_bot": False,
                "open_id": "ou_sender",
                "sender_type": "user",
            },
        )
        self.assertEqual(
            quoted["mentions"],
            [
                {
                    "is_bot": False,
                    "key": "@_user_1",
                    "name": "Bob",
                    "open_id": "ou_mentioned",
                    "union_id": "on_mentioned",
                    "user_id": "user_mentioned",
                    "tenant_key": "tenant_public",
                }
            ],
        )
        self.assertEqual(
            quoted["resources"],
            [
                {
                    "type": "file",
                    "content_read": False,
                    "file_key": "file_asset",
                    "file_name": "ou_sender-report.txt",
                }
            ],
        )
        self.assertNotIn("identifiers_redacted", quoted)

    def test_mentions_and_resources_have_explicit_envelope_limits(self) -> None:
        message = inbound(
            TextContent(text="quoted"),
            content_text="quoted",
            mentions=[Mention(f"@_user_{index}") for index in range(65)],
            resources=[
                ResourceDescriptor(type="file", file_key=f"file_{index}")
                for index in range(65)
            ],
        )

        quoted = self.render(message)["quoted_message"]

        self.assertEqual(len(quoted["mentions"]), 64)
        self.assertTrue(quoted["mentions_truncated"])
        self.assertEqual(len(quoted["resources"]), 64)
        self.assertTrue(quoted["resources_truncated"])
        self.assertTrue(quoted["truncated"])
        self.assertEqual(quoted["mentions"][-1]["key"], "@_user_63")
        self.assertEqual(quoted["resources"][-1]["file_key"], "file_63")

    def test_quoted_text_truncates_but_current_request_remains_last_and_complete(self) -> None:
        encoded = compose_quoted_prompt(
            inbound(content_text="abcdefghij"),
            "current request must remain complete",
            text_limit=5,
        )
        envelope = json.loads(encoded)

        self.assertEqual(envelope["quoted_message"]["text"], "abcde")
        self.assertTrue(envelope["quoted_message"]["truncated"])
        self.assertEqual(
            envelope["current_message"]["request_text"],
            "current request must remain complete",
        )
        self.assertEqual(list(envelope)[-1], "current_message")

    def test_system_and_unknown_types_are_explicitly_unsupported(self) -> None:
        for content in (
            SystemContent(template="system"),
            UnknownContent(message_type="new_type"),
        ):
            with self.subTest(kind=content.kind):
                with self.assertRaises(UnsupportedQuotedMessage):
                    compose_quoted_prompt(
                        inbound(content, content_text="visible"),
                        "current",
                    )

    def test_conflicting_typed_and_raw_content_types_fail_closed(self) -> None:
        with self.assertRaises(QuotedMessageContractError):
            compose_quoted_prompt(
                inbound(
                    TextContent(text="visible"),
                    content_text="visible",
                    raw_content_type="interactive",
                ),
                "current",
            )


class SupplementalProjectionTest(unittest.TestCase):
    def project(
        self,
        text: str,
        *,
        message_id: str,
        create_time: int,
        sender: Identity | None = None,
    ) -> object:
        message = inbound(
            TextContent(text=text),
            message_id=message_id,
            content_text=text,
            sender=sender,
        )
        message.create_time = create_time
        return project_supplemental_message(message)

    def test_supplemental_requires_resolved_human_identity(self) -> None:
        bot = project_supplemental_message(
            inbound(
                sender=Identity(
                    open_id="ou_bot",
                    display_name="Bot",
                    is_bot=True,
                )
            )
        )
        system = project_supplemental_message(
            inbound(SystemContent(template="system"), content_text="system")
        )
        unknown = project_supplemental_message(
            inbound(UnknownContent(message_type="new_type"), content_text="unknown")
        )

        self.assertEqual(
            (bot.reason, system.reason, unknown.reason),  # type: ignore[union-attr]
            ("bot_sender", "system_message", "unsupported_message_type"),
        )
        self.assertTrue(
            all(
                isinstance(item, SupplementalMessageOmission)
                for item in (bot, system, unknown)
            )
        )
        for sender in (
            Identity(open_id="", display_name="Alice"),
            Identity(open_id="ou_sender", display_name=None),
        ):
            with self.subTest(sender=sender):
                with self.assertRaises(HistoricalMessageUnavailable):
                    project_supplemental_message(inbound(sender=sender))

    def test_context_envelope_orders_history_deduplicates_quote_and_keeps_request_last(
        self,
    ) -> None:
        first = self.project(
            "/stop is historical",
            message_id="om_first",
            create_time=100,
        )
        duplicate = self.project(
            "$old-skill quoted duplicate",
            message_id="om_quote",
            create_time=200,
        )
        latest = self.project(
            "$old-skill supplemental",
            message_id="om_latest",
            create_time=300,
        )
        quoted = project_quoted_message(
            inbound(
                content_text="$old-skill quoted",
                message_id="om_quote",
            )
        )
        assert not isinstance(first, SupplementalMessageOmission)
        assert not isinstance(duplicate, SupplementalMessageOmission)
        assert not isinstance(latest, SupplementalMessageOmission)

        result = compose_message_context_prompt(
            supplemental_messages=(first, duplicate, latest),
            quoted_message=quoted,
            current=current("$live-skill answer this"),
            supplemental_stats=SupplementalContextStats(
                scanned_count=5,
                omitted_count=2,
                unsupported_omitted_count=1,
                truncated_before=True,
            ),
        )

        current_prefix, current_json = result.text.split('"current_message"', 1)
        self.assertNotIn("$old-skill", current_prefix)
        self.assertIn(r"\u0024old-skill", current_prefix)
        self.assertIn("$live-skill answer this", current_json)
        envelope = json.loads(result.text)
        self.assertEqual(envelope["kind"], "feishu_message_context_prompt")
        self.assertEqual(envelope["version"], 1)
        self.assertEqual(
            [item["message_id"] for item in envelope["supplemental_messages"]],
            ["om_first", "om_latest"],
        )
        self.assertEqual(
            envelope["supplemental_messages"][0]["text"],
            "/stop is historical",
        )
        self.assertEqual(envelope["quoted_message"]["message_id"], "om_quote")
        self.assertEqual(
            envelope["current_message"]["request_text"],
            "$live-skill answer this",
        )
        self.assertEqual(list(envelope)[-1], "current_message")
        self.assertIn("slash-prefixed", envelope["handling"])
        self.assertEqual(result.stats.selected_count, 2)
        self.assertEqual(result.stats.quoted_deduplicated_count, 1)
        self.assertEqual(result.stats.omitted_count, 2)
        self.assertTrue(result.stats.truncated_before)

    def test_aggregate_limits_retain_the_newest_complete_suffix_and_report_stats(
        self,
    ) -> None:
        projected = []
        for index, text in enumerate(("aaaa", "bb", "ccc", "dddd"), start=1):
            item = self.project(
                text,
                message_id=f"om_{index}",
                create_time=index,
            )
            assert not isinstance(item, SupplementalMessageOmission)
            projected.append(item)

        result = compose_message_context_prompt(
            supplemental_messages=projected,
            quoted_message=None,
            current=current("now"),
            max_supplemental_messages=3,
            max_supplemental_text=7,
        )
        envelope = json.loads(result.text)

        self.assertEqual(
            [item["message_id"] for item in envelope["supplemental_messages"]],
            ["om_3", "om_4"],
        )
        self.assertEqual(result.stats.selected_count, 2)
        self.assertEqual(result.stats.truncated_count, 2)
        self.assertTrue(result.stats.message_limit_reached)
        self.assertTrue(result.stats.text_limit_reached)

        text_limited = compose_message_context_prompt(
            supplemental_messages=projected,
            quoted_message=None,
            current=current("now"),
            max_supplemental_messages=4,
            max_supplemental_text=7,
        )
        self.assertEqual(text_limited.stats.selected_count, 2)
        self.assertEqual(text_limited.stats.truncated_count, 2)
        self.assertTrue(text_limited.stats.text_limit_reached)

    def test_decreasing_snapshot_order_fails_closed(self) -> None:
        newer = self.project("newer", message_id="om_new", create_time=2)
        older = self.project("older", message_id="om_old", create_time=1)
        assert not isinstance(newer, SupplementalMessageOmission)
        assert not isinstance(older, SupplementalMessageOmission)

        with self.assertRaises(HistoricalMessageContractError):
            compose_message_context_prompt(
                supplemental_messages=(newer, older),
                quoted_message=None,
                current=current("now"),
            )

    def test_pre_media_selection_can_be_reprojected_without_double_counting(self) -> None:
        first = self.project("a", message_id="om_first", create_time=1)
        second = self.project("b", message_id="om_second", create_time=2)
        assert not isinstance(first, SupplementalMessageOmission)
        assert not isinstance(second, SupplementalMessageOmission)
        selection = select_supplemental_messages(
            (first, second),
            max_supplemental_text=7,
        )

        revised_first = self.project(
            "xxxx",
            message_id="om_first",
            create_time=1,
        )
        revised_second = self.project(
            "yyyy",
            message_id="om_second",
            create_time=2,
        )
        assert not isinstance(revised_first, SupplementalMessageOmission)
        assert not isinstance(revised_second, SupplementalMessageOmission)
        revised = selection.reproject((revised_first, revised_second))
        result = compose_message_context_prompt(
            supplemental_selection=revised,
            quoted_message=None,
            current=current("now"),
        )

        self.assertEqual(
            [message.message_id for message in result.supplemental_messages],
            ["om_second"],
        )
        self.assertEqual(result.stats.selected_count, 1)
        self.assertEqual(result.stats.truncated_count, 1)
        self.assertTrue(result.stats.text_limit_reached)


if __name__ == "__main__":
    unittest.main()
