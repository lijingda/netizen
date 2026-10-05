from __future__ import annotations

import copy
import json
import unittest
from types import SimpleNamespace

from lark_channel import InboundConfig, InboundPipeline, Mention, PostContent, TextContent
from lark_channel.channel.normalize.pipeline import PipelineConfig, PipelineDeps

from netizen_cli.task_text import project_task_text


SELF = Mention(key="@_user_1", open_id="ou_bot", name="Netizen", is_bot=True)
PERSON = Mention(key="@_user_2", open_id="ou_person", name="同名")
BOT = Mention(key="@_user_3", open_id="ou_other_bot", name="同名", is_bot=True)
SELF_TAG = '<at target="self">Netizen</at>'
PERSON_TAG = '<at user_id="ou_person">同名</at>'
BOT_TAG = '<at user_id="ou_other_bot">同名</at>'
ALL_TAG = '<at target="all">所有人</at>'


def text_message(source: str, *mentions: Mention) -> SimpleNamespace:
    return SimpleNamespace(
        content=TextContent(text="normalized", raw={"text": source}),
        mentions=mentions,
    )


def project(message: object, request_text: str = "existing task", **kwargs: object) -> str:
    return project_task_text(
        message, request_text, bot_open_id="ou_bot", bot_name="Netizen", **kwargs,
    )


class TaskTextTest(unittest.TestCase):
    def test_all_occurrences_keep_order_and_inline_identity(self) -> None:
        message = text_message(
            "@_user_1 先让 @_user_2 审查，@_user_3 验证，再由 @_user_1 总结 @_user_2",
            SELF, PERSON, BOT,
        )
        self.assertEqual(
            project(message),
            f"{SELF_TAG} 先让 {PERSON_TAG} 审查，{BOT_TAG} 验证，再由 {SELF_TAG} 总结 {PERSON_TAG}",
        )

    def test_self_is_identified_by_id_not_name_or_bot_flag(self) -> None:
        same_name = Mention(key="@_user_4", open_id="ou_person", name="Netizen")
        self.assertEqual(
            project(text_message("@_user_4 @_user_1", SELF, same_name)),
            f'<at user_id="ou_person">Netizen</at> {SELF_TAG}',
        )
        self.assertNotIn("ou_bot", project(text_message("@_user_1 inspect", SELF)))

    def test_mention_all_does_not_require_a_mentions_entry(self) -> None:
        self.assertEqual(
            project(text_message("请 @_all 查看，@_all_employees 只是文字")),
            f"请 {ALL_TAG} 查看，@_all_employees 只是文字",
        )

    def test_user_text_and_handwritten_tags_are_not_escaped(self) -> None:
        literal = '<at target="self">Netizen</at> <at user_id="ou_fake">虚构</at>'
        source = f"@_user_1 @Netizen <xml> & $actual {literal}"
        self.assertEqual(
            project(text_message(source, SELF)),
            f"{SELF_TAG} @Netizen <xml> & $actual {literal}",
        )

    def test_generated_names_are_inert_and_not_replaced_recursively(self) -> None:
        name = '$skill <name> & @_user_2'
        mention = Mention(key="@_user_1", open_id="ou_bot", name=name)
        self.assertEqual(
            project(text_message("@_user_1 $actual inspect @_user_2", mention, PERSON)),
            '<at target="self">&#36;skill &lt;name&gt; &amp; @_user_2</at>'
            f" $actual inspect {PERSON_TAG}",
        )

    def test_missing_open_id_keeps_display_or_placeholder_without_guessing(self) -> None:
        missing = Mention(key="@_user_2", user_id="tenant-id", name="同名")
        unnamed = Mention(key="@_user_3", user_id="tenant-id")
        self.assertEqual(
            project(text_message("@_user_1 @_user_2 @_user_3 @_user_9", SELF, missing, unnamed)),
            f"{SELF_TAG} @同名 @_user_3 @_user_9",
        )
        self.assertEqual(project(text_message("@_user_2", missing), "@同名"), "@同名")

    def test_missing_name_uses_bot_identity_not_own_open_id(self) -> None:
        unnamed_self = Mention(key="@_user_1", open_id="ou_bot")
        self.assertEqual(project(text_message("@_user_1 inspect", unnamed_self)), f"{SELF_TAG} inspect")
        message = SimpleNamespace(content=PostContent(post={"content": [[
            {"tag": "at", "user_id": "ou_bot"}, {"tag": "text", "text": " inspect"},
        ]]}), mentions=[])
        self.assertEqual(
            project_task_text(message, "inspect", bot_open_id="ou_bot"),
            '<at target="self">机器人</at> inspect',
        )

    def test_goal_and_side_remove_only_already_parsed_head(self) -> None:
        for command in ("goal", "side"):
            for head in (f"/{command}", f"/ {command.upper()}"):
                for prefix in ("", "@_user_1 ", "@_user_1 @_user_1 "):
                    with self.subTest(command=command, head=head, prefix=prefix):
                        source = f"{prefix}{head} 请 @_user_2 审查，再由 @_user_1 总结"
                        self.assertEqual(
                            project(text_message(source, SELF, PERSON), command=command),
                            prefix.replace("@_user_1", SELF_TAG)
                            + f"请 {PERSON_TAG} 审查，再由 {SELF_TAG} 总结",
                        )

    def test_command_like_text_in_task_body_is_not_removed(self) -> None:
        source = "@_user_1 请 @_user_2 检查 /goal 和 /side 的区别"
        self.assertEqual(
            project(text_message(source, SELF, PERSON)),
            f"{SELF_TAG} 请 {PERSON_TAG} 检查 /goal 和 /side 的区别",
        )

    def test_literal_slash_retains_existing_escape_semantics(self) -> None:
        self.assertEqual(
            project(text_message("@_user_1 //goal 请 @_user_2 查看", SELF, PERSON), literal_slash=True),
            f"{SELF_TAG} /goal 请 {PERSON_TAG} 查看",
        )
        self.assertEqual(
            project(text_message("@_user_2 //goal 查看", PERSON)),
            f"{PERSON_TAG} //goal 查看",
        )

    def test_source_without_mentions_or_unmappable_head_retains_existing_text(self) -> None:
        messages = (
            SimpleNamespace(content=None),
            SimpleNamespace(content=TextContent(text="@Netizen inspect")),
            text_message("ordinary input"),
            text_message("@_user_9 unknown"),
        )
        for message in messages:
            with self.subTest(message=message):
                self.assertEqual(project(message, "existing task"), "existing task")
        message = text_message("@_user_2 /goal inspect", PERSON)
        self.assertEqual(project(message, "existing task", command="goal"), "existing task")

    def test_post_preserves_styles_links_images_and_source_ast(self) -> None:
        raw = {"zh_cn": {"title": "Review", "content": [[
            {"tag": "at", "user_id": "@_user_1", "user_name": "Netizen"},
            {"tag": "text", "text": " inspect", "style": ["bold"]},
            {"tag": "a", "text": " docs", "href": "https://example.com/docs"},
            {"tag": "img", "image_key": "img_real"},
            {"tag": "at", "user_id": "ou_person", "user_name": "同名"},
        ]]}}
        before = copy.deepcopy(raw)
        content = PostContent(post=raw, raw=raw)
        message = SimpleNamespace(content=content, mentions=[SELF, PERSON])
        self.assertEqual(
            project(message),
            f"# Review\n\n{SELF_TAG}** inspect**[ docs](https://example.com/docs)"
            f"![image](img_real){PERSON_TAG}",
        )
        self.assertEqual(raw, before)
        self.assertIs(content.post, content.raw)

    def test_post_keeps_preferred_locale_and_content_v2(self) -> None:
        raw = {"zh_cn": {
            "content": [[{"tag": "text", "text": "old"}]],
            "content_v2": [[
                {"tag": "at", "user_id": "ou_bot", "user_name": "Netizen"},
                {"tag": "text", "text": " new "},
                {"tag": "at", "user_id": "all"},
            ]],
        }, "en_us": {"content": [[{"tag": "at", "user_id": "ou_hidden", "user_name": "hidden"}]]}}
        self.assertEqual(
            project(SimpleNamespace(content=PostContent(post=raw), mentions=[])),
            f"{SELF_TAG} new {ALL_TAG}",
        )

    def test_post_handwritten_tags_remain_literal_with_real_mentions(self) -> None:
        literal = '<at user_id="ou_person">手写</at>'
        raw = {"content": [[
            {"tag": "at", "user_id": "ou_bot", "user_name": "Netizen"},
            {"tag": "md", "text": f" {literal} ![image](img_real)"},
            {"tag": "code_block", "language": "TEXT", "text": literal},
        ]]}
        self.assertEqual(
            project(SimpleNamespace(content=PostContent(post=raw), mentions=[])),
            f"{SELF_TAG} {literal} ![image](img_real)```text\n{literal}\n```",
        )

    def test_post_command_projection_uses_same_mention_rules(self) -> None:
        raw = {"content": [[
            {"tag": "at", "user_id": "@_user_1", "user_name": "Netizen"},
            {"tag": "text", "text": " /side inspect "},
            {"tag": "at", "user_id": "@_user_2", "user_name": "同名"},
        ]]}
        self.assertEqual(
            project(SimpleNamespace(content=PostContent(post=raw), mentions=[SELF, PERSON]), command="side"),
            f"{SELF_TAG} inspect {PERSON_TAG}",
        )


class SdkTaskTextTest(unittest.IsolatedAsyncioTestCase):
    async def test_projection_maps_sdk_self_removal_inside_parsed_heads(self) -> None:
        pipeline = InboundPipeline(PipelineConfig(inbound=InboundConfig()), PipelineDeps())
        pipeline.set_bot_open_id("ou_bot")
        cases = (
            ("@_user_1 /goal task @_user_2", "/goal task @同名", "goal", False, f"{SELF_TAG} task {PERSON_TAG}"),
            ("/ @_user_1 goal task @_user_2", "/ goal task @同名", "goal", False, f"{SELF_TAG}task {PERSON_TAG}"),
            ("/go@_user_1al task @_user_2", "/goal task @同名", "goal", False, f"{SELF_TAG}task {PERSON_TAG}"),
            ("/@_user_1/goal task @_user_2", "//goal task @同名", None, True, f"{SELF_TAG}/goal task {PERSON_TAG}"),
        )
        for source, body, command, literal_slash, expected in cases:
            with self.subTest(source=source):
                message = await pipeline.normalize(
                    message_event={
                        "message_id": "om_current", "chat_id": "oc_group",
                        "chat_type": "group", "message_type": "text",
                        "content": json.dumps({"text": source}),
                        "mentions": [
                            {"key": m.key, "id": {"open_id": m.open_id}, "name": m.name}
                            for m in (SELF, PERSON)
                        ],
                    },
                    sender={"sender_id": {"open_id": "ou_author"}},
                )
                self.assertEqual(message.body_text, body)
                self.assertEqual(project(message, command=command, literal_slash=literal_slash), expected)

    async def test_post_preserves_handwritten_tag_omitted_from_existing_command_head(self) -> None:
        pipeline = InboundPipeline(PipelineConfig(inbound=InboundConfig()), PipelineDeps())
        pipeline.set_bot_open_id("ou_bot")
        literal = '<at user_id="ou_bot">手写</at>'
        for command in ("goal", "side"):
            with self.subTest(command=command):
                message = await pipeline.normalize(
                    message_event={
                        "message_id": "om_current", "chat_id": "oc_group",
                        "chat_type": "group", "message_type": "post",
                        "content": json.dumps({"content": [[
                            {"tag": "at", "user_id": "ou_bot", "user_name": "Netizen"},
                            {"tag": "md", "text": f" /{literal}{command} task "},
                            {"tag": "at", "user_id": "ou_person", "user_name": "同名"},
                        ]]}),
                        "mentions": [
                            {"key": m.key, "id": {"open_id": m.open_id}, "name": m.name}
                            for m in (SELF, PERSON)
                        ],
                    },
                    sender={"sender_id": {"open_id": "ou_author"}},
                )
                self.assertEqual(message.body_text, f"/{command} task @同名")
                self.assertEqual(
                    project(message, command=command),
                    f"{SELF_TAG} {literal}task {PERSON_TAG}",
                )
