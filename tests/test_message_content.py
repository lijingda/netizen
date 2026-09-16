from __future__ import annotations

import copy
import json
import unittest
from types import SimpleNamespace

from lark_channel import (
    AudioContent,
    FileContent,
    ImageContent,
    InboundConfig,
    InboundPipeline,
    InteractiveContent,
    MediaContent,
    Mention,
    MergeForwardContent,
    MergeForwardItem,
    PostContent,
    TextContent,
    UnknownContent,
    flatten_content,
)
from lark_channel.channel import normalize as sdk_normalize
from lark_channel.channel.normalize.pipeline import PipelineConfig, PipelineDeps

from netizen.message_content import (
    HistoricalMessageContractError,
    HistoricalMessageUnavailable,
    UnsupportedHistoricalMessage,
    project_message_content,
)


def forward(*contents: object, **kwargs: object) -> MergeForwardContent:
    return MergeForwardContent(
        items=[
            MergeForwardItem(
                message_id=f"om_child_{index}",
                sender_name="Alice",
                sender_open_id="ou_alice",
                create_time=1_000 + index,
                content=content,
            )
            for index, content in enumerate(contents)
        ],
        **kwargs,
    )


def card(elements: list[dict[str, object]]) -> InteractiveContent:
    return InteractiveContent(
        card={"schema": "2.0", "body": {"elements": elements}},
        card_version="v2",
    )


def project(
    content: object,
    *,
    text: str | None = None,
    mentions: tuple[Mention, ...] = (),
    **kwargs: object,
):
    sdk_text, resources = flatten_content(content)
    message = SimpleNamespace(
        content=content,
        content_text=sdk_text if text is None else text,
        resources=resources,
        mentions=mentions,
    )
    return project_message_content(message, message_type=content.kind, **kwargs)


class MessageContentTest(unittest.TestCase):
    def test_current_text_override_preserves_body_exactly(self) -> None:
        for content in (TextContent(text="SDK body"), PostContent(text="SDK body")):
            for request_text in ("", "  current body \n"):
                with self.subTest(kind=content.kind, request=request_text):
                    result = project(content, request_text=request_text)
                    self.assertEqual(result["text"], request_text)
            self.assertEqual(project(content, text="  history \n")["text"], "history")
        with self.assertRaises(TypeError):
            project(TextContent(text="body"), request_text=123)

    def test_mixed_forward_reuses_sdk_text_and_marks_all_media_unread(self) -> None:
        content = forward(
            TextContent(text="Discussion"),
            card([{"tag": "markdown", "content": "Card status"}]),
            PostContent(post={"en_us": {"content": [[
                {"tag": "text", "text": "Post status"},
            ]]}}),
            FileContent(file_key="file_private", file_name="report.pdf"),
            ImageContent(image_key="image_private"),
            AudioContent(file_key="audio_private", duration_ms=2_000),
            MediaContent(file_key="video_private", file_name="clip.mp4"),
            forward(TextContent(text="Nested discussion")),
        )
        original = copy.deepcopy(content)

        result = project(content, read_image_keys=("image_private",))

        for expected in (
            "Discussion", "Card status", "Post status", "report.pdf",
            "clip.mp4", "Nested discussion", "Alice", "2000ms", "未读取",
        ):
            self.assertIn(expected, result["text"])
        for private in ("file_private", "image_private", "audio_private", "video_private"):
            self.assertNotIn(private, result["text"])
        self.assertTrue(result["resources"])
        self.assertTrue(all(not value["content_read"] for value in result["resources"]))
        self.assertEqual(result["content_fidelity"], "bounded_aggregate")
        self.assertFalse(result["truncated"])
        self.assertEqual(content, original)

    def test_uncut_forward_is_rendered_from_the_typed_tree(self) -> None:
        result = project(
            forward(TextContent(text="@_user_1 hello")),
            text="stale SDK text must not replace the validated tree",
            mentions=(Mention("@_user_1", name="Alice"),),
        )
        self.assertIn("@Alice hello", result["text"])
        self.assertNotIn("@_user_1", result["text"])
        self.assertNotIn("stale SDK text", result["text"])

    def test_public_sdk_mention_resolution_survives_mixed_and_truncated_forward(self) -> None:
        self.assertIn("MentionExtraction", sdk_normalize.__all__)
        self.assertIn("resolve_mentions", sdk_normalize.__all__)
        mention = Mention("@_user_1", open_id="ou_bob", name="Bob")
        for content in (
            forward(
                TextContent(text="@_user_1 hello @_all @_user_2"),
                FileContent(file_key="file_private", file_name="report.pdf"),
            ),
            forward(
                TextContent(text="@_user_1 hello @_all @_user_2"),
                *(TextContent(text=f"message-{index}!") for index in range(60)),
            ),
        ):
            with self.subTest(item_count=len(content.items)):
                result = project(content, mentions=(mention,))
                self.assertIn("@Bob hello @all @_user_2", result["text"])
                self.assertNotIn("@_user_1", result["text"])
                self.assertNotIn("message-59!", result["text"])
        current = project(
            TextContent(text="SDK text"),
            mentions=(mention,),
            request_text="@_user_1 current authored request",
        )
        self.assertEqual(current["text"], "@_user_1 current authored request")

    def test_normalized_sender_name_is_not_resolved_a_second_time(self) -> None:
        result = project(
            forward(TextContent(text="@_user_1 hello")),
            text="<forwarded_messages>\n@Alice @_user_2 hello\n</forwarded_messages>",
            mentions=(
                Mention("@_user_1", name="Alice @_user_2"),
                Mention("@_user_2", name="Bob"),
            ),
        )
        self.assertIn("@Alice @_user_2 hello", result["text"])
        self.assertNotIn("@Bob", result["text"])

    def test_forward_post_masks_only_generated_visible_resource_targets(self) -> None:
        user_literal = "literal img_private file_private video_private folder_private"
        content = forward(PostContent(post={
            "en_us": {
                "content": [[{"tag": "img", "image_key": "img_old"}]],
                "content_v2": [[
                    {"tag": "text", "text": user_literal},
                    {"tag": "code_block", "text": "![literal](img_private)"},
                    {"tag": "md", "text": "![user-authored](img_private)"},
                    {"tag": "md", "text": "![md image](img_md_only)\n```\n![fenced](img_code)\n```"},
                    {"tag": "md", "text": "```\n![unclosed](img_unclosed)"},
                    {"tag": "img", "image_key": "img_private"},
                    {"tag": "media", "file_key": "video_private"},
                ]],
            },
            "zh_cn": {"content": [[{"tag": "img", "image_key": "img_hidden"}]]},
            "files": [
                {"file_key": "file_private", "file_name": "report.pdf"},
                {"file_key": "folder_private", "file_name": "Design", "is_folder": True},
            ],
        }))
        original = copy.deepcopy(content)

        result = project(content)

        self.assertIn(user_literal, result["text"])
        self.assertIn("![literal](img_private)", result["text"])
        self.assertIn("![user-authored](unread-image)", result["text"])
        self.assertIn("![md image](unread-image)", result["text"])
        self.assertNotIn("img_md_only", result["text"])
        self.assertIn("![fenced](img_code)", result["text"])
        self.assertIn("![unclosed](img_unclosed)", result["text"])
        self.assertNotIn("![image](img_private)", result["text"])
        self.assertNotIn("[media:video_private]", result["text"])
        self.assertNotIn('key="file_private"', result["text"])
        self.assertNotIn('key="folder_private"', result["text"])
        self.assertNotIn("img_old", result["text"])
        self.assertNotIn("img_hidden", result["text"])
        for target in ("unread-image", "unread-media", "unread-file", "unread-folder"):
            self.assertIn(target, result["text"])
        self.assertTrue(any(item.get("file_key") == "file_private" for item in result["resources"]))
        self.assertEqual(content, original)

    def test_direct_ast_and_files_only_post_resources_keep_names_without_keys(self) -> None:
        for post in (
            {"content": [[{"tag": "img", "image_key": "img_private"}]]},
            {"files": [{"file_key": "file_private", "file_name": "report.pdf"}]},
        ):
            with self.subTest(post=post):
                result = project(forward(PostContent(post=post)))
                self.assertNotIn("img_private", result["text"])
                self.assertNotIn("file_private", result["text"])
                self.assertIn("unread-", result["text"])

    def test_global_item_budget_counts_nested_containers(self) -> None:
        content = forward(
            forward(*(TextContent(text=f"first-{index}!") for index in range(25))),
            forward(*(TextContent(text=f"second-{index}!") for index in range(25))),
        )
        original = copy.deepcopy(content)

        result = project(content)

        # Two container items plus the first 48 leaf items consume 50 slots.
        self.assertIn("first-24!", result["text"])
        self.assertIn("second-22!", result["text"])
        self.assertNotIn("second-23!", result["text"])
        self.assertTrue(result["truncated"])
        self.assertIn("截断", result["text"])
        self.assertEqual(content, original)

    def test_depth_limit_rejects_the_whole_message_without_mutating_sdk_tree(self) -> None:
        content = forward(TextContent(text="too-deep"))
        for _ in range(4):
            content = forward(TextContent(text="retained"), content)
        original = copy.deepcopy(content)

        with self.assertRaisesRegex(HistoricalMessageUnavailable, "最大深度 3"):
            project(content)
        self.assertEqual(content, original)

    def test_forward_post_resources_match_selected_locale_and_body(self) -> None:
        content = forward(PostContent(post={
            "en_us": {
                "content": [[{"tag": "file", "file_key": "old", "file_name": "old.pdf"}]],
                "content_v2": [[
                    {"tag": "text", "text": "Visible discussion"},
                    {"tag": "img", "image_key": "visible_image"},
                    {"tag": "media", "file_key": "visible_video"},
                    {"tag": "audio", "file_key": "visible_audio"},
                    {"tag": "file", "file_key": "visible_file", "file_name": "current.pdf"},
                ]],
            },
            "zh_cn": {"content": [[
                {"tag": "file", "file_key": "hidden", "file_name": "hidden.pdf"},
            ]]},
            "files": [{"file_key": "attachment", "file_name": "attachment.pdf"}],
        }))
        original = copy.deepcopy(content)

        result = project(content)

        self.assertIn("Visible discussion", result["text"])
        self.assertIn("[media:unread-media]", result["text"])
        self.assertEqual(
            {(item["type"], item["file_key"]) for item in result["resources"]},
            {("image", "visible_image"), ("video", "visible_video"),
             ("audio", "visible_audio"), ("file", "visible_file"), ("file", "attachment")},
        )
        self.assertEqual(
            {item["file_name"] for item in result["resources"] if "file_name" in item},
            {"current.pdf", "attachment.pdf"},
        )
        self.assertFalse(result["truncated"])
        self.assertEqual(content, original)

    def test_nested_truncation_propagates_to_outer_projection(self) -> None:
        result = project(forward(forward(TextContent(text="kept"), truncated=True)))
        self.assertTrue(result["truncated"])
        self.assertIn("kept", result["text"])
        self.assertIn("截断", result["text"])

    def test_text_budget_includes_unread_and_truncation_notices(self) -> None:
        result = project(forward(TextContent(text="x" * 20_000)))
        self.assertLessEqual(len(result["text"]), 16_000)
        self.assertTrue(result["truncated"])
        self.assertIn("未读取", result["text"])
        self.assertIn("截断", result["text"])

    def test_loading_and_error_fail_at_every_selected_level(self) -> None:
        for kwargs in ({"loading": True}, {"error": "fetch_failed"}):
            for nested in (False, True):
                with self.subTest(kwargs=kwargs, nested=nested):
                    content = forward(TextContent(text="partial"), **kwargs)
                    if nested:
                        content = forward(TextContent(text="other"), content)
                    with self.assertRaises(HistoricalMessageUnavailable):
                        project(content, text="apparently readable")

    def test_unknown_missing_and_malformed_leaves_never_silently_disappear(self) -> None:
        for child in (
            UnknownContent(message_type="new-kind"),
            None,
            FileContent(file_key="file", file_name={"not": "a name"}),
            SimpleNamespace(kind="text"),
        ):
            with self.subTest(child=child):
                with self.assertRaises((HistoricalMessageUnavailable, UnsupportedHistoricalMessage)):
                    project(forward(TextContent(text="readable"), child))

    def test_empty_nested_forward_is_not_treated_as_read(self) -> None:
        with self.assertRaises(HistoricalMessageUnavailable):
            project(forward(TextContent(text="readable"), MergeForwardContent()))

    def test_bad_item_metadata_is_rejected_before_sdk_can_hide_it(self) -> None:
        for field, value in (
            ("sender_name", {"tag": "markdown", "content": "not a name"}),
            ("sender_open_id", ["not-an-id"]),
            ("message_id", None),
            ("create_time", "not-a-time"),
            ("create_time", True),
            ("create_time", -1),
            ("create_time", 10 ** 100),
        ):
            with self.subTest(field=field, value=value):
                content = forward(TextContent(text="one"), TextContent(text="two"))
                setattr(content.items[1], field, value)
                with self.assertRaises(HistoricalMessageContractError):
                    project(content, text="SDK already skipped the malformed item")

    def test_empty_typed_aggregate_fails_even_with_forged_visible_text(self) -> None:
        for content in (
            MergeForwardContent(),
            MergeForwardContent(truncated=True),
            MergeForwardContent(error="fetch_failed"),
        ):
            for text in (None, "forged visible text"):
                with self.subTest(content=content, text=text):
                    with self.assertRaises(HistoricalMessageUnavailable):
                        project(content, text=text)

    def test_cycle_is_rejected_without_reaching_sdk_recursive_conversion(self) -> None:
        content = forward()
        content.items.append(MergeForwardItem(message_id="cycle", content=content))
        message = SimpleNamespace(content=content, content_text="cycle", resources=[])
        with self.assertRaises(HistoricalMessageContractError):
            project_message_content(message, message_type="merge_forward")

    def test_sdk_hidden_card_text_leak_is_rejected_for_top_level_and_forward(self) -> None:
        for field in (
            "value", "confirm", "options", "behaviors", "events",
            "disabled_tips", "initial_option", "initial_value",
            "selected_values", "tooltip",
        ):
            with self.subTest(field=field):
                content = card([{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "Visible button"},
                    field: {"tag": "markdown", "content": "hidden payload"},
                }])
                sdk_text, _ = flatten_content(content)
                self.assertIn("hidden payload", sdk_text)
                for source in (content, forward(content)):
                    with self.assertRaisesRegex(HistoricalMessageUnavailable, "非可见"):
                        project(source)

    def test_normal_card_buttons_and_scalar_action_values_keep_sdk_text(self) -> None:
        content = card([{
            "tag": "button",
            "text": {"tag": "plain_text", "content": "Visible button"},
            "value": {"action": "scalar-secret"},
        }])
        self.assertEqual(project(content)["text"], "Visible button")
        result = project(forward(content))
        self.assertIn("Visible button", result["text"])
        self.assertNotIn("scalar-secret", result["text"])

    def test_card_gate_has_node_depth_and_text_bounds(self) -> None:
        deep: dict[str, object] = {"tag": "plain_text", "content": "body"}
        for _ in range(40):
            deep = {"elements": [deep]}
        for content in (
            card([{}] * 5_000),
            card([deep]),
            card([{"tag": "markdown", "content": "x" * 256_001}]),
        ):
            with self.subTest(card=content.card.get("schema")):
                with self.assertRaisesRegex(HistoricalMessageUnavailable, "可验证范围"):
                    project(content)


class RealSdkMaterialContentTest(unittest.IsolatedAsyncioTestCase):
    async def normalize_forward(self, items, *, mentions=()):
        async def fetch_message(message_id):
            self.assertEqual(message_id, "om_forward")
            return {"data": {"items": items}}

        pipeline = InboundPipeline(
            PipelineConfig(inbound=InboundConfig()),
            PipelineDeps(fetch_message=fetch_message),
        )
        message = await pipeline.normalize(
            message_event={
                "message_id": "om_forward", "chat_id": "oc_chat",
                "chat_type": "group", "message_type": "merge_forward",
                "content": "{}", "mentions": list(mentions),
            },
            sender={"sender_id": {"open_id": "ou_user"}},
        )
        self.assertIsNotNone(message)
        return message

    async def test_single_render_matches_sdk_mentions_without_double_resolution(self):
        message = await self.normalize_forward(
            [{"message_id": "om_child", "upper_message_id": "om_forward",
              "msg_type": "text", "body": {"content": json.dumps({
                  "text": "@_user_1 hello @_user_2 @_all",
              })}}],
            mentions=(
                {"key": "@_user_1", "id": {"open_id": "ou_alice"}, "name": "Alice @_user_2"},
                {"key": "@_user_2", "id": {"open_id": "ou_bob"}, "name": "Bob"},
            ),
        )
        result = project_message_content(message, message_type="merge_forward")
        self.assertIn("@Alice @_user_2 hello @Bob @all", result["text"])
        self.assertTrue(result["text"].startswith(message.content_text))

    async def test_real_sdk_depth_boundary_accepts_three_and_rejects_four(self):
        for depth in (3, 4):
            with self.subTest(depth=depth):
                items = []
                parent_id = "om_forward"
                for level in range(1, depth + 1):
                    message_id = f"om_nested_{level}"
                    items.append({
                        "message_id": message_id, "upper_message_id": parent_id,
                        "msg_type": "merge_forward", "body": {"content": "{}"},
                    })
                    parent_id = message_id
                items.append({
                    "message_id": "om_leaf", "upper_message_id": parent_id,
                    "msg_type": "text", "body": {"content": '{"text":"deep leaf"}'},
                })
                message = await self.normalize_forward(items)
                if depth == 3:
                    result = project_message_content(message, message_type="merge_forward")
                    self.assertIn("deep leaf", result["text"])
                    self.assertFalse(result["truncated"])
                else:
                    node = message.content
                    for _ in range(depth):
                        node = node.items[0].content
                    self.assertEqual(node.error, "max_depth_exceeded")
                    with self.assertRaisesRegex(HistoricalMessageUnavailable, "最大深度 3"):
                        project_message_content(message, message_type="merge_forward")

    async def test_v1_card_with_header_is_rejected_even_if_sdk_labels_it_v2(self):
        legacy_card = {
            "header": {"title": {"tag": "plain_text", "content": "Title only"}},
            "elements": [{"tag": "div", "text": {
                "tag": "plain_text", "content": "Invisible to SDK converter",
            }}],
        }
        message = await self.normalize_forward([{
            "message_id": "om_card", "upper_message_id": "om_forward",
            "msg_type": "interactive", "body": {"content": json.dumps(legacy_card)},
        }])
        child = message.content.items[0].content
        self.assertEqual(child.card_version, "v2")  # Pinned parser treats a header as v2.
        with self.assertRaisesRegex(UnsupportedHistoricalMessage, "不支持飞书 1.0"):
            project_message_content(message, message_type="merge_forward")

    async def test_item_truncation_cannot_hide_real_sdk_depth_error(self):
        items = [{
            "message_id": f"om_text_{index}", "upper_message_id": "om_forward",
            "msg_type": "text", "body": {"content": '{"text":"retained"}'},
        } for index in range(49)]
        parent_id = "om_forward"
        for level in range(1, 5):
            message_id = f"om_nested_{level}"
            items.append({
                "message_id": message_id, "upper_message_id": parent_id,
                "msg_type": "merge_forward", "body": {"content": "{}"},
            })
            parent_id = message_id
        message = await self.normalize_forward(items)
        self.assertEqual(len(message.content.items), 50)
        node = message.content.items[-1].content
        for _ in range(3):
            node = node.items[0].content
        self.assertEqual(node.error, "max_depth_exceeded")
        with self.assertRaisesRegex(HistoricalMessageUnavailable, "最大深度 3"):
            project_message_content(message, message_type="merge_forward")


if __name__ == "__main__":
    unittest.main()
