from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace

from openai_codex import ImageInput, TextInput

from netizen.image_inputs import (
    ImageInputContractError,
    ImageInputUnavailable,
    ImageReference,
    PreparedImage,
    UnsupportedPromptMedia,
    compose_multimodal_input,
    current_message_image_references,
    image_references,
    prepare_images,
)


PNG = b"\x89PNG\r\n\x1a\ncontent"


def message(
    message_type: str,
    *,
    message_id: str = "om_message",
    content_text: str = "",
    resources: list[object] | None = None,
    image_key: str | None = None,
    post: dict[str, object] | None = None,
) -> object:
    content = SimpleNamespace(kind=message_type)
    if image_key is not None:
        content.image_key = image_key
    if post is not None:
        content.post = post
    return SimpleNamespace(
        id=message_id,
        content=content,
        content_text=content_text,
        resources=resources or [],
        raw_content_type=message_type,
    )


def resource(kind: str, key: str) -> object:
    return SimpleNamespace(type=kind, file_key=key)


class FakeDownloader:
    def __init__(self, bodies: dict[tuple[str, str], object]) -> None:
        self.bodies = bodies
        self.calls: list[tuple[str, str, str | None]] = []
        self.active = 0
        self.max_active = 0

    async def download_resource(
        self,
        file_key: str,
        resource_type: str = "image",
        message_id: str | None = None,
    ) -> bytes | None:
        self.calls.append((file_key, resource_type, message_id))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            value = self.bodies[(str(message_id), file_key)]
            if isinstance(value, BaseException):
                raise value
            if isinstance(value, asyncio.Event):
                await value.wait()
                return PNG
            return value  # type: ignore[return-value]
        finally:
            self.active -= 1


class ImageReferenceTest(unittest.TestCase):
    def test_image_and_post_refs_are_ordered_deduplicated_and_source_labeled(self) -> None:
        standalone = message(
            "image",
            image_key="img_one",
            content_text="![image](img_one)",
            resources=[resource("image", "img_one")],
        )
        post = message(
            "post",
            message_id="om_post",
            content_text="before ![image](img_two) after ![image](img_three)",
            post={
                "zh_cn": {
                    "content": [[
                        {"tag": "text", "text": "before "},
                        {"tag": "img", "image_key": "img_two"},
                        {"tag": "text", "text": " after "},
                        {"tag": "img", "image_key": "img_three"},
                    ]]
                }
            },
            resources=[
                resource("image", "img_two"),
                resource("image", "img_two"),
                resource("image", "img_three"),
            ],
        )

        current = current_message_image_references(standalone)
        quoted = image_references(post, source="quoted_message")
        supplemental = image_references(post, source="supplemental_message")

        self.assertEqual(
            current,
            (ImageReference("current_message", "om_message", "img_one"),),
        )
        self.assertEqual(
            quoted,
            (
                ImageReference("quoted_message", "om_post", "img_two"),
                ImageReference("quoted_message", "om_post", "img_three"),
            ),
        )
        self.assertEqual(
            supplemental,
            (
                ImageReference("supplemental_message", "om_post", "img_two"),
                ImageReference("supplemental_message", "om_post", "img_three"),
            ),
        )

    def test_rendered_post_markers_recover_sdk_content_v2_resource_gap(self) -> None:
        post = message(
            "post",
            content_text="text ![image](img_visible)",
            post={
                "zh_cn": {
                    "content_v2": [[
                        {"tag": "text", "text": "text "},
                        {"tag": "img", "image_key": "img_visible"},
                    ]]
                }
            },
            resources=[],
        )

        self.assertEqual(
            image_references(post, source="current_message"),
            (
                ImageReference(
                    "current_message",
                    "om_message",
                    "img_visible",
                ),
            ),
        )

    def test_rendered_post_order_wins_over_hidden_sdk_resource_variant(self) -> None:
        post = message(
            "post",
            content_text="![image](img_visible)",
            post={
                "zh_cn": {
                    "content": [[
                        {"tag": "img", "image_key": "img_hidden"},
                    ]],
                    "content_v2": [[
                        {"tag": "img", "image_key": "img_visible"},
                    ]],
                }
            },
            resources=[
                resource("image", "img_hidden"),
                resource("file", "file_hidden"),
            ],
        )

        self.assertEqual(
            [
                ref.file_key
                for ref in current_message_image_references(post)
            ],
            ["img_visible"],
        )

    def test_post_resource_without_typed_ast_fails_closed(self) -> None:
        post = message(
            "post",
            content_text="text without a visible image marker",
            resources=[resource("image", "img_unmapped")],
        )

        with self.assertRaisesRegex(ImageInputContractError, "没有提供可验证的富文本结构"):
            image_references(post, source="current_message")

    def test_literal_markdown_in_text_and_fenced_code_is_not_an_image(self) -> None:
        post = message(
            "post",
            content_text=(
                "literal ![not an image](img_text)\n\n"
                "```md\n![also not an image](img_code)\n```"
            ),
            post={
                "zh_cn": {
                    "content_v2": [[
                        {
                            "tag": "text",
                            "text": "literal ![not an image](img_text)",
                        },
                        {
                            "tag": "md",
                            "text": "```md\n![also not an image](img_code)\n```",
                        },
                    ]]
                }
            },
        )

        self.assertEqual(image_references(post, source="current_message"), ())

    def test_markdown_image_outside_fence_is_supported_in_order(self) -> None:
        post = message(
            "post",
            post={
                "zh_cn": {
                    "content": [[
                        {
                            "tag": "md",
                            "text": (
                                "![first](img_one)\n"
                                "```\n![ignored](img_code)\n```\n"
                                "![second](img_two)"
                            ),
                        }
                    ]]
                }
            },
        )

        self.assertEqual(
            [ref.file_key for ref in image_references(post, source="current_message")],
            ["img_one", "img_two"],
        )

    def test_unclosed_markdown_fence_does_not_create_image_refs(self) -> None:
        post = message(
            "post",
            post={
                "zh_cn": {
                    "content_v2": [[
                        {
                            "tag": "md",
                            "text": "```md\n![code sample](img_not_resource)",
                        }
                    ]]
                }
            },
        )

        self.assertEqual(image_references(post, source="current_message"), ())

    def test_standalone_image_conflicting_public_keys_fail_closed(self) -> None:
        standalone = message(
            "image",
            image_key="img_content",
            resources=[resource("image", "img_resource")],
        )

        with self.assertRaisesRegex(ImageInputContractError, "相互冲突"):
            image_references(standalone, source="current_message")

    def test_current_prompt_rejects_non_image_media_and_unknown_message_types(self) -> None:
        with self.assertRaisesRegex(UnsupportedPromptMedia, "暂不支持的附件"):
            current_message_image_references(
                message("post", resources=[resource("file", "file_one")])
            )
        with self.assertRaisesRegex(UnsupportedPromptMedia, "暂不支持的附件"):
            current_message_image_references(
                message(
                    "post",
                    post={
                        "zh_cn": {
                            "content_v2": [[
                                {"tag": "media", "file_key": "file_video"},
                            ]]
                        }
                    },
                )
            )
        video = message("video")
        video.content.kind = "media"
        with self.assertRaisesRegex(UnsupportedPromptMedia, "media"):
            current_message_image_references(video)


class ImagePreparationTest(unittest.IsolatedAsyncioTestCase):
    async def test_png_jpeg_gif_and_webp_are_admitted_by_magic_bytes(self) -> None:
        bodies = [
            (PNG, "image/png"),
            (b"\xff\xd8\xffjpeg", "image/jpeg"),
            (b"GIF89aimage", "image/gif"),
            (b"RIFF\x04\x00\x00\x00WEBPdata", "image/webp"),
        ]
        for index, (body, expected) in enumerate(bodies):
            key = f"img_{index}"
            with self.subTest(mime=expected):
                result = await prepare_images(
                    FakeDownloader({("om", key): body}),
                    (ImageReference("current_message", "om", key),),
                )
                self.assertEqual(result[0].mime_type, expected)

    async def test_images_download_sequentially_and_use_exact_message_resource(self) -> None:
        refs = (
            ImageReference("quoted_message", "om_quote", "img_quote"),
            ImageReference("current_message", "om_current", "img_current"),
        )
        channel = FakeDownloader(
            {
                ("om_quote", "img_quote"): PNG,
                ("om_current", "img_current"): b"\xff\xd8\xffjpeg",
            }
        )

        result = await prepare_images(channel, refs)

        self.assertEqual(
            channel.calls,
            [
                ("img_quote", "image", "om_quote"),
                ("img_current", "image", "om_current"),
            ],
        )
        self.assertEqual(channel.max_active, 1)
        self.assertEqual([item.mime_type for item in result], ["image/png", "image/jpeg"])
        self.assertTrue(result[0].data_url.startswith("data:image/png;base64,"))

    async def test_count_size_total_format_failure_and_download_failure_are_atomic(self) -> None:
        one = ImageReference("current_message", "om", "img_one")
        two = ImageReference("current_message", "om", "img_two")

        cases = [
            (
                "count",
                FakeDownloader({("om", "img_one"): PNG, ("om", "img_two"): PNG}),
                (one, two),
                {"max_count": 1},
                "超过当前 1 张上限",
            ),
            (
                "single-size",
                FakeDownloader({("om", "img_one"): PNG}),
                (one,),
                {"max_image_bytes": 1},
                "图片大小超过",
            ),
            (
                "total-size",
                FakeDownloader({("om", "img_one"): PNG, ("om", "img_two"): PNG}),
                (one, two),
                {"max_total_bytes": len(PNG)},
                "图片总大小超过",
            ),
            (
                "format",
                FakeDownloader({("om", "img_one"): b"not-image"}),
                (one,),
                {},
                "图片格式不受支持",
            ),
            (
                "download",
                FakeDownloader({("om", "img_one"): None}),
                (one,),
                {},
                "无法读取消息中的图片",
            ),
        ]
        for name, channel, refs, limits, pattern in cases:
            with self.subTest(name=name):
                with self.assertRaisesRegex(ImageInputUnavailable, pattern):
                    await prepare_images(channel, refs, **limits)

    async def test_per_image_and_total_timeouts_are_explicit(self) -> None:
        ref = ImageReference("current_message", "om", "img_one")
        gate = asyncio.Event()

        with self.assertRaisesRegex(ImageInputUnavailable, "读取图片超时"):
            await prepare_images(
                FakeDownloader({("om", "img_one"): gate}),
                (ref,),
                fetch_timeout_seconds=0.001,
            )
        with self.assertRaisesRegex(ImageInputUnavailable, "全部图片超时"):
            await prepare_images(
                FakeDownloader({("om", "img_one"): gate}),
                (ref,),
                fetch_timeout_seconds=1,
                total_timeout_seconds=0.001,
            )


class MultimodalCompositionTest(unittest.TestCase):
    def test_labels_are_grouped_supplemental_then_quoted_then_current(self) -> None:
        images = tuple(
            PreparedImage(
                reference,
                "image/png",
                len(PNG),
                "data:image/png;base64,AA==",
            )
            for reference in (
                ImageReference("current_message", "om_current", "img_current"),
                ImageReference("supplemental_message", "om_two", "img_two"),
                ImageReference("quoted_message", "om_quote", "img_quote"),
                ImageReference("supplemental_message", "om_one", "img_one"),
            )
        )

        result = compose_multimodal_input("request", images=images)

        assert isinstance(result, list)
        labels = [
            json.loads(item.text)
            for item in result[:-1]
            if isinstance(item, TextInput)
        ]
        self.assertEqual(
            [label["source"] for label in labels],
            [
                "supplemental_message",
                "supplemental_message",
                "quoted_message",
                "current_message",
            ],
        )
        self.assertEqual([label["index"] for label in labels], [1, 2, 1, 1])
        self.assertEqual([label["count"] for label in labels], [2, 2, 1, 1])

    def test_images_are_labeled_before_one_complete_final_prompt(self) -> None:
        images = (
            PreparedImage(
                ImageReference("quoted_message", "om_quote", "img_quote"),
                "image/png",
                len(PNG),
                "data:image/png;base64,AA==",
            ),
            PreparedImage(
                ImageReference("current_message", "om_current", "img_current"),
                "image/jpeg",
                4,
                "data:image/jpeg;base64,AA==",
            ),
        )
        prompt = json.dumps(
            {
                "kind": "feishu_quoted_prompt",
                "version": 3,
                "current_message": {
                    "message_id": "om_current",
                    "message_type": "image",
                    "sender": {"open_id": "ou_current"},
                    "content_fidelity": "full_multimodal",
                    "request_text": "compare them",
                },
            }
        )

        result = compose_multimodal_input(
            prompt,
            images=images,
        )

        assert isinstance(result, list)
        self.assertIsInstance(result[0], TextInput)
        self.assertIsInstance(result[1], ImageInput)
        self.assertIsInstance(result[3], ImageInput)
        quoted_label = json.loads(result[0].text)
        current_label = json.loads(result[2].text)
        final = json.loads(result[4].text)
        self.assertEqual(quoted_label["source"], "quoted_message")
        self.assertEqual(current_label["source"], "current_message")
        self.assertEqual(
            final["current_message"]["request_text"],
            "compare them",
        )
        self.assertEqual(result[-1].text, prompt)
        rendered_text = "\n".join(
            item.text for item in result if isinstance(item, TextInput)
        )
        self.assertNotIn("feishu_quoted_context", rendered_text)
        self.assertNotIn("feishu_multimodal_instruction", rendered_text)
        self.assertEqual(
            sum(
                item.text.count("compare them")
                for item in result
                if isinstance(item, TextInput)
            ),
            1,
        )

    def test_image_only_input_does_not_repeat_live_request_as_context(self) -> None:
        image = PreparedImage(
            ImageReference("current_message", "om_current", "img_current"),
            "image/png",
            len(PNG),
            "data:image/png;base64,AA==",
        )

        result = compose_multimodal_input(
            "inspect this image",
            images=(image,),
        )

        assert isinstance(result, list)
        self.assertEqual(len(result), 3)
        self.assertEqual(json.loads(result[0].text)["kind"], "feishu_image_input")
        self.assertIsInstance(result[1], ImageInput)
        self.assertEqual(result[2].text, "inspect this image")
        self.assertEqual(
            sum(
                item.text.count("inspect this image")
                for item in result
                if isinstance(item, TextInput)
            ),
            1,
        )

    def test_no_images_preserves_plain_string_input(self) -> None:
        self.assertEqual(
            compose_multimodal_input("plain", images=()),
            "plain",
        )


if __name__ == "__main__":
    unittest.main()
