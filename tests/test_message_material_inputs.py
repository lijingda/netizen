from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lark_channel import (
    AudioContent,
    FileContent,
    ImageContent,
    InteractiveContent,
    MergeForwardContent,
    MergeForwardItem,
    PostContent,
    QuotedContext,
    TextContent,
    UnknownContent,
    flatten_content,
)

from netizen import channel_app
from netizen.bindings import BindingStore, SideTopicState
from netizen.channel_app import ChannelApplication
from netizen.codex_runtime import Submission, SubmitDisposition
from netizen.domain import (
    FeishuScope,
    MentionContextMode,
    MessageContextAnchor,
    NativeCapability,
    ScopeKind,
)
from netizen.management import (
    InstanceManagementService,
    ManagementRuntimePort,
    ScopeCoordinator,
)
from netizen.message_history import (
    MessageHistoryRef,
    MessageHistoryStats,
    MessageHistoryWindow,
)
from netizen.projects import ProjectRegistry
from test_channel_app import (
    FakeChannel,
    FakeMessage,
    FakeMessageHistory,
    StubRuntime,
    plain_prompt_projection,
)


def card_content(text: str) -> InteractiveContent:
    return InteractiveContent(
        card={
            "schema": "2.0",
            "body": {"elements": [{"tag": "markdown", "content": text}]},
        },
        card_version="v2",
    )


def forwarded_content(*children: object, **kwargs) -> MergeForwardContent:
    return MergeForwardContent(
        items=[
            MergeForwardItem(
                message_id=f"om_child_{index}",
                sender_open_id="ou_historical",
                sender_name="Historical Author",
                create_time=1_000 + index,
                content=child,
            )
            for index, child in enumerate(children)
        ],
        **kwargs,
    )


def material_message(content: object, *, message_id: str, **kwargs) -> FakeMessage:
    text, resources = flatten_content(content)
    return FakeMessage(
        text,
        message_id=message_id,
        raw_content_type=content.kind,
        content=content,
        resources=resources,
        **kwargs,
    )


class MessageMaterialInputTest(unittest.IsolatedAsyncioTestCase):
    """Exercise the real Channel boundary without inheriting unrelated tests."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.project = root / "project"
        self.project.mkdir()
        self.store = BindingStore()
        self.channel = FakeChannel()
        self.history = FakeMessageHistory()
        self.runtime = StubRuntime()
        self.runtime.binding_store = self.store
        self.runtime.available_capabilities = frozenset(
            {NativeCapability.SKILLS, NativeCapability.SIDE}
        )
        self.projects = ProjectRegistry(
            store=self.store,
            project_root=root,
            projects={"test": self.project},
        )
        self.management = InstanceManagementService(
            bindings=self.store,
            projects=self.projects,
            runtime=ManagementRuntimePort(self.runtime),
            scope_coordinator=ScopeCoordinator(),
        )
        self.app = ChannelApplication(
            app_id="cli_test",
            channel=self.channel,
            runtime=self.runtime,
            bindings=self.store,
            projects=self.projects,
            management=self.management,
            message_history=self.history,
        )

    async def asyncTearDown(self) -> None:
        try:
            await self.app.close()
        finally:
            await self.management.close()
        self.store.close()
        self.tmp.cleanup()

    async def binding(self, scope: FeishuScope | None = None, **kwargs):
        scope = scope or FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        created = await self.management.create_current_binding(
            scope=scope,
            creator_id="ou_user",
            project_alias="test",
            **kwargs,
        )
        binding = created.binding
        self.runtime.submission = Submission(
            SubmitDisposition.STARTED,
            binding.id,
            "native-material",
            "turn-material",
            lambda: None,
        )
        return binding

    def submitted(self) -> dict[str, object]:
        self.assertEqual(
            len(self.runtime.submit_calls),
            1,
            msg=f"Expected one native submission; replies: {self.channel.replies!r}",
        )
        return self.runtime.submit_calls[0]

    async def handle_material_source(
        self, content: object, *, source: str, case: str
    ) -> None:
        """Put the same public SDK content through each real input boundary."""

        self.runtime.submit_calls.clear()
        self.channel.replies.clear()
        self.channel.fetch_quoted_calls.clear()
        if source in {"current", "quoted"}:
            await self.binding()
            message = material_message(content, message_id=f"om_material_{case}")
            if source == "current":
                await self.app.handle_message(message)
            else:
                self.channel.inbound_messages[message.id] = message
                await self.app.handle_message(
                    FakeMessage(
                        "Inspect this material",
                        message_id=f"om_request_{case}",
                        reply_id=message.id,
                    )
                )
            return

        self.assertEqual(source, "supplemental")
        scope = FeishuScope("cli_test", f"oc_group_{case}", ScopeKind.GROUP)
        lower = MessageContextAnchor("om_lower", 1_000)
        upper = MessageContextAnchor(f"om_request_{case}", 3_000)
        await self.binding(
            scope,
            message_context_mode=MentionContextMode.CATCH_UP,
            context_anchor=lower,
        )
        message = material_message(
            content,
            message_id=f"om_material_{case}",
            chat_id=scope.chat_id,
            chat_type="group",
            create_time=2_000,
        )
        self.channel.inbound_messages[message.id] = message
        self.history.window = MessageHistoryWindow(
            lower=lower,
            upper=upper,
            candidates=(
                MessageHistoryRef(
                    message_id=message.id,
                    create_time_ms=message.create_time,
                    sender_id="ou_user",
                    message_type=message.raw_content_type,
                ),
            ),
            stats=MessageHistoryStats(1, 3, 0, 0, 0, False, False),
        )
        await self.app.handle_message(
            FakeMessage(
                "Inspect this material",
                message_id=upper.message_id,
                chat_id=scope.chat_id,
                chat_type="group",
                create_time=upper.create_time_ms,
            )
        )

    async def test_direct_card_preserves_current_provenance(self) -> None:
        binding = await self.binding()
        message = material_message(
            card_content("Application status: ready"),
            message_id="om_current_card",
            sender_id="ou_alice",
            display_name="Alice",
            union_id="on_not_for_prompt",
            user_id="tenant_not_for_prompt",
        )

        await self.app.handle_message(message)

        call = self.submitted()
        text, context = plain_prompt_projection(call["input"])
        self.assertIn("Application status: ready", text)
        self.assertEqual(context["kind"], "feishu_current_message")
        self.assertEqual(context["version"], 1)
        self.assertEqual(context["message_id"], message.id)
        self.assertEqual(context["message_type"], "interactive")
        self.assertEqual(
            context["sender"],
            {
                "display_name": "Alice",
                "open_id": "ou_alice",
                "is_bot": False,
                "sender_type": "user",
            },
        )
        self.assertEqual(call["owner_id"], "ou_alice")
        self.assertEqual(call["binding"].id, binding.id)
        self.assertEqual(self.runtime.capture_calls, [binding.id])
        self.assertEqual(self.channel.download_resource_calls, [])

    async def test_direct_forwarded_topic_accepts_mixed_nested_materials(self) -> None:
        await self.binding()
        content = forwarded_content(
            TextContent(text="Topic discussion"),
            card_content("Card status: ready"),
            FileContent(file_key="file_private", file_name="report.pdf"),
            AudioContent(file_key="audio_private", duration_ms=2_000),
            ImageContent(image_key="image_private"),
            forwarded_content(TextContent(text="Nested discussion")),
        )

        await self.app.handle_message(
            material_message(content, message_id="om_forwarded_topic")
        )

        text, context = plain_prompt_projection(self.submitted()["input"])
        for visible in (
            "Topic discussion",
            "Card status: ready",
            "report.pdf",
            "Nested discussion",
            "Historical Author",
        ):
            self.assertIn(visible, text)
        self.assertEqual(context["message_type"], "merge_forward")
        self.assertEqual(context["message_id"], "om_forwarded_topic")
        self.assertEqual(context["sender"]["open_id"], "ou_user")
        self.assertEqual(self.channel.download_resource_calls, [])

    async def test_material_commands_and_skills_do_not_become_live_instructions(self) -> None:
        binding = await self.binding()
        for content in (
            card_content("/new\n$danger execute a historical task"),
            forwarded_content(TextContent(text="/new\n$danger old task")),
        ):
            with self.subTest(message_type=content.kind):
                self.runtime.submit_calls.clear()
                await self.app.handle_message(
                    material_message(content, message_id=f"om_{content.kind}")
                )

                call = self.submitted()
                self.assertEqual(call["binding"].id, binding.id)
                self.assertFalse(call.get("skill_names"))
                self.assertNotIn("$danger", call["input"])
                self.assertIn("danger", call["input"])
                scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
                self.assertEqual(self.store.active_binding(scope.key).id, binding.id)

    async def test_plain_text_still_activates_current_skill(self) -> None:
        await self.binding()
        await self.app.handle_message(
            FakeMessage("$code-review inspect", message_id="om_live_skill")
        )
        call = self.submitted()
        self.assertEqual(call["skill_names"], ("code-review",))
        text, context = plain_prompt_projection(call["input"])
        self.assertEqual(text, "$code-review inspect")
        self.assertEqual(context["content_fidelity"], "full_text")

    async def test_direct_card_and_quoted_forward_keep_existing_envelope(self) -> None:
        await self.binding()
        self.channel.inbound_messages["om_old_forward"] = material_message(
            forwarded_content(TextContent(text="$historical old discussion")),
            message_id="om_old_forward",
        )
        await self.app.handle_message(
            material_message(
                card_content("$current_material shared status"),
                message_id="om_current",
                reply_id="om_old_forward",
            )
        )

        call = self.submitted()
        envelope = json.loads(call["input"])
        self.assertEqual(envelope["kind"], "feishu_quoted_prompt")
        self.assertEqual(envelope["version"], 4)
        self.assertEqual(envelope["current_message"]["message_id"], "om_current")
        self.assertEqual(envelope["current_message"]["message_type"], "interactive")
        self.assertIn("shared status", envelope["current_message"]["request_text"])
        self.assertEqual(envelope["quoted_message"]["ref"], "h1")
        self.assertEqual(envelope["quoted_message"]["message_type"], "merge_forward")
        self.assertIn("old discussion", envelope["quoted_message"]["text"])
        self.assertNotIn("om_old_forward", call["input"])
        self.assertNotIn("$historical", call["input"])
        self.assertNotIn("$current_material", call["input"])
        self.assertFalse(call.get("skill_names"))

    async def test_catch_up_and_quote_dedup_keep_materials_in_original_roles(self) -> None:
        scope = FeishuScope("cli_test", "oc_group", ScopeKind.GROUP)
        lower = MessageContextAnchor("om_lower", 1_000)
        await self.binding(
            scope,
            message_context_mode=MentionContextMode.CATCH_UP,
            context_anchor=lower,
        )
        upper = MessageContextAnchor("om_current", 4_000)
        history_card = material_message(
            card_content("$old_card Card background"),
            message_id="om_history_card",
            chat_id=scope.chat_id,
            chat_type="group",
            create_time=2_000,
        )
        quoted_forward = material_message(
            forwarded_content(TextContent(text="$old_forward Forward background")),
            message_id="om_history_forward",
            chat_id=scope.chat_id,
            chat_type="group",
            create_time=3_000,
        )
        candidates = (history_card, quoted_forward)
        self.history.window = MessageHistoryWindow(
            lower=lower,
            upper=upper,
            candidates=tuple(
                MessageHistoryRef(
                    message_id=message.id,
                    create_time_ms=message.create_time,
                    sender_id="ou_user",
                    message_type=message.raw_content_type,
                )
                for message in candidates
            ),
            stats=MessageHistoryStats(1, 4, 0, 0, 0, False, False),
        )
        self.channel.inbound_messages.update(
            {message.id: message for message in candidates}
        )

        await self.app.handle_message(
            material_message(
                card_content("Current material"),
                message_id=upper.message_id,
                chat_id=scope.chat_id,
                chat_type="group",
                create_time=upper.create_time_ms,
                reply_id=quoted_forward.id,
            )
        )

        call = self.submitted()
        envelope = json.loads(call["input"])
        self.assertEqual(envelope["kind"], "feishu_message_context_prompt")
        self.assertEqual(envelope["version"], 2)
        self.assertEqual(len(envelope["supplemental_messages"]), 1)
        self.assertEqual(envelope["supplemental_messages"][0]["ref"], "h1")
        self.assertEqual(envelope["quoted_message"]["ref"], "h2")
        self.assertIn("Card background", envelope["supplemental_messages"][0]["text"])
        self.assertIn("Forward background", envelope["quoted_message"]["text"])
        self.assertIn("Current material", envelope["current_message"]["request_text"])
        self.assertEqual(envelope["context_status"], {"omitted_count": 0, "truncated": False})
        self.assertEqual(call["context_commit"].anchor, upper)
        self.assertFalse(call.get("skill_names"))
        self.assertNotIn("$old_card", call["input"])
        self.assertNotIn("$old_forward", call["input"])

    async def test_same_material_text_is_shared_by_current_quote_and_history(self) -> None:
        for content in (
            card_content("Shared status\n$historical /new"),
            forwarded_content(
                TextContent(text="Shared discussion\n$historical /new"),
                card_content("Nested status"),
                FileContent(file_key="file_report", file_name="report.pdf"),
                AudioContent(file_key="file_audio", duration_ms=2_000),
            ),
        ):
            with self.subTest(message_type=content.kind):
                self.runtime.submit_calls.clear()
                await self.binding()
                direct = material_message(
                    content,
                    message_id=f"om_shared_{content.kind}",
                )
                await self.app.handle_message(direct)
                direct_call = self.submitted()
                request_text, current_context = plain_prompt_projection(
                    direct_call["input"]
                )
                material = json.loads(request_text.split("\n\n", 1)[1])
                self.assertEqual(material["kind"], "feishu_message_material")
                self.assertEqual(material["message_type"], content.kind)
                self.assertEqual(current_context["kind"], "feishu_current_message")
                self.assertEqual(current_context["version"], 1)
                self.assertEqual(current_context["message_id"], direct.id)
                self.assertEqual(current_context["message_type"], content.kind)
                self.assertIn("$historical", material["text"])
                self.assertNotIn("$historical", direct_call["input"])

                self.runtime.submit_calls.clear()
                self.channel.inbound_messages[direct.id] = direct
                await self.app.handle_message(
                    FakeMessage(
                        "Compare this material",
                        message_id=f"om_quote_{content.kind}",
                        reply_id=direct.id,
                    )
                )
                quoted = json.loads(self.submitted()["input"])
                self.assertEqual(quoted["kind"], "feishu_quoted_prompt")
                self.assertEqual(quoted["version"], 4)
                self.assertEqual(quoted["quoted_message"]["message_type"], content.kind)
                self.assertEqual(quoted["quoted_message"]["text"], material["text"])
                self.assertEqual(
                    quoted["current_message"]["request_text"],
                    "Compare this material",
                )

                self.runtime.submit_calls.clear()
                scope = FeishuScope(
                    "cli_test", f"oc_group_{content.kind}", ScopeKind.GROUP
                )
                lower = MessageContextAnchor("om_lower", 1_000)
                upper = MessageContextAnchor(f"om_catchup_{content.kind}", 3_000)
                await self.binding(
                    scope,
                    message_context_mode=MentionContextMode.CATCH_UP,
                    context_anchor=lower,
                )
                historical = material_message(
                    content,
                    message_id=f"om_history_{content.kind}",
                    chat_id=scope.chat_id,
                    chat_type="group",
                    create_time=2_000,
                )
                self.channel.inbound_messages[historical.id] = historical
                self.history.window = MessageHistoryWindow(
                    lower=lower,
                    upper=upper,
                    candidates=(
                        MessageHistoryRef(
                            message_id=historical.id,
                            create_time_ms=historical.create_time,
                            sender_id="ou_user",
                            message_type=content.kind,
                        ),
                    ),
                    stats=MessageHistoryStats(
                        pages_scanned=1,
                        raw_messages_scanned=3,
                        duplicate_messages=0,
                        ignored_after_upper=0,
                        omitted_messages=0,
                        truncated_before=False,
                        scan_limit_hit=False,
                    ),
                )
                await self.app.handle_message(
                    FakeMessage(
                        "Compare this material",
                        message_id=upper.message_id,
                        chat_id=scope.chat_id,
                        chat_type="group",
                        create_time=upper.create_time_ms,
                    )
                )
                catch_up = json.loads(self.submitted()["input"])
                self.assertEqual(catch_up["kind"], "feishu_message_context_prompt")
                self.assertEqual(catch_up["version"], 2)
                self.assertEqual(len(catch_up["supplemental_messages"]), 1)
                supplemental = catch_up["supplemental_messages"][0]
                self.assertEqual(supplemental["message_type"], content.kind)
                self.assertEqual(supplemental["text"], material["text"])
                self.assertEqual(supplemental["ref"], "h1")
                self.assertEqual(
                    catch_up["current_message"]["request_text"],
                    "Compare this material",
                )
                self.assertEqual(self.channel.download_resource_calls, [])

    async def test_forward_loading_and_fetch_failure_do_not_submit_partial_content(self) -> None:
        await self.binding()
        for status in ({"loading": True}, {"error": "fetch failed"}):
            with self.subTest(status=status):
                self.channel.replies.clear()
                await self.app.handle_message(
                    material_message(
                        forwarded_content(TextContent(text="partial content"), **status),
                        message_id=f"om_failed_{next(iter(status))}",
                    )
                )
                self.assertEqual(self.runtime.submit_calls, [])
                self.assertTrue(self.channel.replies)

    async def test_unknown_forward_child_rejects_entire_input(self) -> None:
        await self.binding()
        await self.app.handle_message(
            material_message(
                forwarded_content(
                    TextContent(text="Readable portion"),
                    UnknownContent(message_type="future_attachment"),
                ),
                message_id="om_unknown_child",
            )
        )

        self.assertEqual(self.runtime.submit_calls, [])
        self.assertIn("不支持的子消息类型", str(self.channel.replies[-1][1]))

    async def test_forward_truncation_submits_retained_content_and_status(self) -> None:
        await self.binding()
        await self.app.handle_message(
            material_message(
                forwarded_content(TextContent(text="retained content"), truncated=True),
                message_id="om_truncated",
            )
        )
        request, _ = plain_prompt_projection(self.submitted()["input"])
        material = json.loads(request.split("\n\n", 1)[1])
        self.assertIn("retained content", material["text"])
        self.assertIs(material["truncated"], True)
        self.assertIn("截断", material["text"])

    async def test_forward_total_budget_submits_bounded_content_and_status(self) -> None:
        await self.binding()
        await self.app.handle_message(
            material_message(
                forwarded_content(
                    *(TextContent(text=f"child {index}") for index in range(1_001))
                ),
                message_id="om_oversize_forward",
            )
        )
        request, _ = plain_prompt_projection(self.submitted()["input"])
        material = json.loads(request.split("\n\n", 1)[1])
        self.assertIn("child 0", material["text"])
        self.assertIn("child 49", material["text"])
        self.assertNotIn("child 50", material["text"])
        self.assertNotIn("child 1000", material["text"])
        self.assertIs(material["truncated"], True)
        self.assertIn("截断", material["text"])

    async def test_card_v1_is_rejected_without_fallback_in_every_source(self) -> None:
        body = [{"tag": "div", "text": {"tag": "lark_md", "content": "Old body"}}]
        titled = InteractiveContent(
            card={
                "header": {"title": {"tag": "plain_text", "content": "Old title"}},
                "elements": body,
            },
            card_version="v1",
        )
        untitled = InteractiveContent(card={"elements": body}, card_version="v1")
        for source in ("current", "quoted", "supplemental"):
            for shape, content in (
                ("titled", titled),
                ("untitled", untitled),
                ("forwarded", forwarded_content(titled)),
            ):
                with self.subTest(source=source, shape=shape):
                    await self.handle_material_source(
                        content, source=source, case=f"v1_{source}_{shape}"
                    )
                    self.assertEqual(self.runtime.submit_calls, [])
                    self.assertEqual(self.channel.fetch_quoted_calls, [])
                    self.assertRegex(
                        str(self.channel.replies[-1][1]),
                        r"1\.0.*不支持|不支持.*1\.0",
                    )

    async def test_forward_depth_limit_rejects_entire_input_in_every_source(self) -> None:
        deep = forwarded_content(TextContent(text="Deepest discussion"))
        sdk_limited = MergeForwardContent(error="max_depth_exceeded")
        for _ in range(4):
            deep = forwarded_content(deep)
            sdk_limited = forwarded_content(sdk_limited)
        for source in ("current", "quoted", "supplemental"):
            for shape, content in (("depth4", deep), ("sdk_error", sdk_limited)):
                with self.subTest(source=source, shape=shape):
                    await self.handle_material_source(
                        content, source=source, case=f"depth_{source}_{shape}"
                    )
                    self.assertEqual(self.runtime.submit_calls, [])
                    self.assertRegex(str(self.channel.replies[-1][1]), r"深度|嵌套")

    async def test_forward_at_maximum_depth_still_submits(self) -> None:
        await self.binding()
        content = forwarded_content(TextContent(text="Deepest allowed discussion"))
        for _ in range(3):
            content = forwarded_content(content)
        await self.app.handle_message(
            material_message(content, message_id="om_depth3_forward")
        )
        request, _ = plain_prompt_projection(self.submitted()["input"])
        material = json.loads(request.split("\n\n", 1)[1])
        self.assertIn("Deepest allowed discussion", material["text"])
        self.assertIs(material["truncated"], False)

    async def test_forwarded_post_attachments_match_visible_body_in_every_source(self) -> None:
        content = forwarded_content(
            PostContent(
                post={
                    "en_us": {
                        "title": "Visible post",
                        "content": [[{
                            "tag": "file",
                            "file_key": "file_old",
                            "file_name": "outdated.pdf",
                        }]],
                        "content_v2": [[
                            {"tag": "text", "text": "Current visible body"},
                            {"tag": "media", "file_key": "file_visible_video"},
                            {
                                "tag": "file",
                                "file_key": "file_visible_report",
                                "file_name": "visible-report.pdf",
                            },
                        ]],
                    },
                    "zh_cn": {
                        "title": "Unselected locale title",
                        "content": [[{
                            "tag": "file",
                            "file_key": "file_hidden",
                            "file_name": "hidden-locale.pdf",
                        }]],
                    },
                    "files": [{
                        "file_key": "file_visible_attachment",
                        "file_name": "visible-attachment.pdf",
                    }],
                },
            )
        )
        for source in ("current", "quoted", "supplemental"):
            with self.subTest(source=source):
                await self.handle_material_source(
                    content, source=source, case=f"visible_post_{source}"
                )
                call = self.submitted()
                if source == "current":
                    request, _ = plain_prompt_projection(call["input"])
                    material = json.loads(request.split("\n\n", 1)[1])
                else:
                    envelope = json.loads(call["input"])
                    material = (
                        envelope["quoted_message"]
                        if source == "quoted"
                        else envelope["supplemental_messages"][0]
                    )
                self.assertIn("Current visible body", material["text"])
                self.assertIn("visible-attachment.pdf", material["text"])
                self.assertEqual(
                    [
                        (item["type"], item.get("file_name", item.get("name")))
                        for item in material["attachments"]
                    ],
                    [
                        ("video", None),
                        ("file", "visible-report.pdf"),
                        ("file", "visible-attachment.pdf"),
                    ],
                )
                for hidden in (
                    "outdated.pdf", "hidden-locale.pdf", "Unselected locale title"
                ):
                    self.assertNotIn(hidden, call["input"])
                self.assertEqual(self.channel.download_resource_calls, [])

    async def test_direct_file_and_audio_remain_outside_material_scope(self) -> None:
        await self.binding()
        for content in (
            FileContent(file_key="file_private", file_name="report.pdf"),
            AudioContent(file_key="audio_private", duration_ms=2_000),
        ):
            with self.subTest(message_type=content.kind):
                await self.app.handle_message(
                    material_message(content, message_id=f"om_direct_{content.kind}")
                )
                self.assertEqual(self.runtime.submit_calls, [])
        self.assertEqual(self.channel.download_resource_calls, [])

    async def test_missing_current_sender_name_blocks_fallback_and_submission(self) -> None:
        await self.binding()
        await self.app.handle_message(
            material_message(
                InteractiveContent(card={}),
                message_id="om_missing_sender",
                display_name="",
            )
        )
        self.assertEqual(self.runtime.submit_calls, [])
        self.assertEqual(self.channel.fetch_quoted_calls, [])
        self.assertEqual(self.channel.fetch_inbound_calls, [])
        self.assertIn("im:chat.members:read", str(self.channel.replies[-1][1]))

    async def test_material_without_binding_does_not_fetch_placeholder_content(self) -> None:
        for content in (InteractiveContent(card={}), MergeForwardContent(loading=True)):
            with self.subTest(message_type=content.kind):
                await self.app.handle_message(
                    material_message(content, message_id=f"om_unbound_{content.kind}")
                )
                self.assertEqual(self.runtime.capture_calls, [])
                self.assertEqual(self.runtime.submit_calls, [])
                self.assertEqual(self.channel.fetch_quoted_calls, [])
                self.assertEqual(self.channel.fetch_inbound_calls, [])
                self.assertEqual(self.channel.download_resource_calls, [])
                self.assertIn("还没有会话", str(self.channel.replies[-1][1]))

    async def test_current_card_fallback_failure_and_identity_drift_do_not_submit(self) -> None:
        await self.binding()
        for fallback in (
            None,
            RuntimeError("resource unavailable"),
            QuotedContext(
                message_id="om_different",
                content_type="interactive",
                text="Different card",
            ),
            QuotedContext(
                message_id="om_placeholder",
                content_type="text",
                text="Different content type",
            ),
            QuotedContext(
                message_id="om_placeholder",
                content_type="interactive",
                text="",
            ),
        ):
            with self.subTest(fallback=fallback):
                self.channel.fetch_quoted_calls.clear()
                self.channel.replies.clear()
                self.channel.quoted_contexts["om_placeholder"] = fallback
                await self.app.handle_message(
                    material_message(
                        InteractiveContent(card={}),
                        message_id="om_placeholder",
                    )
                )
                self.assertEqual(self.channel.fetch_quoted_calls, ["om_placeholder"])
                self.assertEqual(self.runtime.submit_calls, [])
                self.assertEqual(self.channel.download_resource_calls, [])
                self.assertTrue(self.channel.replies)

    async def test_current_card_fallback_timeout_does_not_submit(self) -> None:
        binding = await self.binding()
        cancelled = asyncio.Event()

        async def never_returns(message_id: str) -> None:
            self.assertEqual(message_id, "om_slow_placeholder")
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with (
            patch.object(channel_app, "_QUOTE_FETCH_TIMEOUT_SECONDS", 0.01),
            patch.object(
                self.channel, "fetch_quoted_context", side_effect=never_returns
            ) as fetch,
        ):
            await self.app.handle_message(
                material_message(
                    InteractiveContent(card={}),
                    message_id="om_slow_placeholder",
                )
            )
            fetch.assert_awaited_once_with("om_slow_placeholder")

        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.runtime.capture_calls, [binding.id])
        self.assertEqual(self.runtime.submit_calls, [])
        self.assertEqual(self.channel.download_resource_calls, [])
        self.assertIn("本条消息未执行", str(self.channel.replies[-1][1]))

    async def test_unmentioned_group_material_does_not_prepare_or_submit(self) -> None:
        scope = FeishuScope("cli_test", "oc_group", ScopeKind.GROUP)
        await self.binding(scope)
        await self.app.handle_message(
            material_message(
                InteractiveContent(card={}),
                message_id="om_unmentioned",
                chat_id=scope.chat_id,
                chat_type="group",
                mentioned_bot=False,
            )
        )
        self.assertEqual(self.runtime.capture_calls, [])
        self.assertEqual(self.runtime.submit_calls, [])
        self.assertEqual(self.channel.fetch_quoted_calls, [])
        self.assertEqual(self.channel.replies, [])

    async def test_creating_side_does_not_execute_close_from_material(self) -> None:
        binding = await self.binding()
        record = self.store.create_side_topic(
            app_id="cli_test",
            chat_id="oc_direct",
            source_message_id="om_source",
            parent_binding_id=binding.id,
            creator_id="ou_user",
            requires_mention=False,
        )
        self.store.set_side_topic_root(record.id, "om_side_root")
        await self.app.handle_message(
            material_message(
                card_content("/side close"),
                message_id="om_material_close",
                thread_id="omt_side",
                raw={"root_id": "om_side_root"},
            )
        )
        self.assertEqual(self.store.get_side_topic(record.id).state, SideTopicState.CREATING)
        self.assertEqual(self.runtime.close_side_calls, [])
        self.assertEqual(self.runtime.submit_calls, [])
        self.assertTrue(self.channel.replies)

    async def test_open_side_accepts_material_without_running_embedded_control(self) -> None:
        binding = await self.binding()
        self.store.assign_native_thread_id(binding.id, "native-parent")
        binding = self.store.get(binding.id)
        record = self.store.create_side_topic(
            app_id="cli_test",
            chat_id="oc_direct",
            source_message_id="om_side_source",
            parent_binding_id=binding.id,
            creator_id="ou_user",
            requires_mention=False,
        )
        self.store.set_side_topic_root(record.id, "om_side_root")
        self.store.open_side_topic(record.id, "omt_side")
        await self.runtime.create_side(
            side_id=record.id,
            binding=binding,
            cwd=self.project,
            creator_id="ou_user",
        )
        await self.runtime.attach_side_topic(
            side_id=record.id,
            topic_id="omt_side",
            root_message_id="om_side_root",
        )
        for content in (
            card_content("/side close\n$historical shared card"),
            forwarded_content(TextContent(text="/side close\n$historical discussion")),
        ):
            with self.subTest(message_type=content.kind):
                self.runtime.submit_side_calls.clear()
                await self.app.handle_message(
                    material_message(
                        content,
                        message_id=f"om_side_material_{content.kind}",
                        thread_id="omt_side",
                        raw={"root_id": "om_side_root"},
                    )
                )

                self.assertEqual(
                    len(self.runtime.submit_side_calls),
                    1,
                    msg=f"Expected one Side submission; replies: {self.channel.replies!r}",
                )
                call = self.runtime.submit_side_calls[0]
                text, context = plain_prompt_projection(call["input"])
                self.assertIn("/side close", text)
                self.assertNotIn("$historical", call["input"])
                self.assertFalse(call.get("skill_names"))
                self.assertEqual(context["message_type"], content.kind)
                self.assertEqual(call["side_id"], record.id)
                self.assertEqual(call["admission"].thread_id, "native-side-1")
                self.assertEqual(self.store.get_side_topic(record.id).state, SideTopicState.OPEN)
                self.assertEqual(self.runtime.close_side_calls, [])
                self.assertEqual(self.runtime.submit_calls, [])
                self.assertEqual(self.history.read_calls, [])

    async def test_current_card_fallback_keeps_original_admission_across_binding_switch(self) -> None:
        first = await self.binding()
        self.runtime.enforce_active_submission = True
        started = asyncio.Event()
        finish = asyncio.Event()

        async def delayed_context(message_id: str) -> QuotedContext:
            self.assertEqual(message_id, "om_slow_card")
            started.set()
            await finish.wait()
            return QuotedContext(
                message_id=message_id,
                content_type="interactive",
                text="Visible recovered card",
            )

        with patch.object(self.channel, "fetch_quoted_context", side_effect=delayed_context):
            pending = asyncio.create_task(
                self.app.handle_message(
                    material_message(InteractiveContent(card={}), message_id="om_slow_card")
                )
            )
            try:
                await asyncio.wait_for(started.wait(), timeout=1.0)
                second = await self.binding()
            finally:
                finish.set()
                await asyncio.wait_for(pending, timeout=1.0)

        self.assertNotEqual(first.id, second.id)
        self.assertEqual(self.runtime.capture_calls, [first.id])
        self.assertEqual(self.runtime.submit_calls, [])
        self.assertIn("active 会话已切换", str(self.channel.replies[-1][1]))
