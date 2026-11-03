from __future__ import annotations

import asyncio
import errno
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from lark_channel import FileContent, InboundConfig, InboundPipeline, PostContent, ResourceDescriptor
from lark_channel.channel.normalize.pipeline import PipelineConfig, PipelineDeps
from openai_codex import ImageInput, TextInput

from netizen_cli.attachments import AttachmentInput, AttachmentStore
from netizen_cli.channel.input_preparation import MessageInputPreparer
from netizen_cli.domain import FeishuScope, MentionContextMode, MessageContextAnchor, NativeCapability, ScopeKind
from netizen_cli.message_history import MessageHistoryRef, MessageHistoryStats, MessageHistoryWindow
from netizen_cli.runtime.contracts import (
    ContextBoundaryCommitFailed, SteerRace, Submission, SubmissionAdmission,
    SubmitDisposition, TurnStartFailed,
)
from tests.support.channel_fixtures import channel_fixture, side_channel_fixture
from tests.support.channel_messages import FakeMessage, PNG, plain_prompt_projection, quoted_inbound


def file_message(*, message_id="om_file", key="file_report", name="report.txt", **kwargs):
    return FakeMessage(
        name, message_id=message_id, raw_content_type="file",
        content=FileContent(file_key=key, file_name=name),
        resources=[ResourceDescriptor(type="file", file_key=key, file_name=name)],
        **kwargs,
    )


def post_message(text, *, message_id="om_post", files=None, images=(), **kwargs):
    files = files or [("file_report", "$untrusted.txt")]
    nodes = [{"tag": "text", "text": text}]
    nodes.extend({"tag": "img", "image_key": key} for key in images)
    post = {
        "content": [nodes],
        "files": [{"file_key": key, "file_name": name} for key, name in files],
    }
    resources = [ResourceDescriptor(type="file", file_key=key, file_name=name)
                 for key, name in files]
    resources.extend(ResourceDescriptor(type="image", file_key=key) for key in images)
    return FakeMessage(
        text + " " + " ".join(name for _, name in files), message_id=message_id,
        raw_content_type="post", content=PostContent(post=post), resources=resources,
        **kwargs,
    )


def split_attachments(native_input):
    text = native_input if isinstance(native_input, str) else native_input[-1].text
    prefix, attachment_text = text.split("\n\n<feishu_file_attachments>\n", 1)
    return prefix, json.loads(attachment_text.removesuffix("\n</feishu_file_attachments>"))


class FileInputChannelTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = await self.enterAsyncContext(channel_fixture())
        self.app = self.fixture.app
        self.channel = self.fixture.channel
        self.runtime = self.fixture.runtime
        self.attachments = AttachmentStore(self.fixture.project_root / "attachments")
        self.app._input_preparer = MessageInputPreparer(
            channel=self.channel, attachment_store=self.attachments,
        )
        await self.fixture.new()
        self.scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        self.binding = self.fixture.store.active_binding(self.scope.key)
        self.runtime.submission = Submission(
            SubmitDisposition.STARTED, self.binding.id, "native-one", "turn-one", lambda: None,
        )

    def retained_batches(self):
        retained = self.attachments.root / "retained"
        return list(retained.iterdir()) if retained.exists() else []

    def assert_original(self, metadata, *, expected=b"original document"):
        self.assertEqual(metadata["kind"], "feishu_file_attachments")
        self.assertEqual(len(metadata["files"]), 1)
        path = Path(metadata["files"][0]["local_path"])
        self.assertTrue(path.is_relative_to(self.attachments.root / "retained"))
        self.assertEqual(path.read_bytes(), expected)
        return path

    async def test_disk_full_rejects_whole_input_and_cleans_only_its_partial_write(self):
        old = await self.attachments.prepare_files([AttachmentInput("old.txt", b"keep")])
        await self.attachments.publish_files(old)
        message = post_message("分析这些文件", files=[("first", "first.txt"), ("second", "second.txt")])
        self.channel.resource_bodies[(message.id, "first")] = b"first"
        self.channel.resource_bodies[(message.id, "second")] = b"second"
        fsync = os.fsync
        writes = 0

        def disk_full(fd):
            nonlocal writes
            writes += 1
            if writes == 2:
                raise OSError(errno.ENOSPC, "private filesystem detail")
            return fsync(fd)

        with patch("netizen_cli.attachments.os.fsync", side_effect=disk_full):
            await self.app.handle_message(message)

        self.assertEqual(writes, 2)
        self.assertEqual(self.runtime.submit_calls, [])
        self.assertEqual(self.runtime.stop_calls, [])
        self.assertEqual(self.retained_batches(), [old.files[0].path.parent])
        self.assertEqual(old.files[0].path.read_bytes(), b"keep")
        self.assertEqual(list((self.attachments.root / ".staging").iterdir()), [])
        self.assertEqual(len(self.channel.replies), 1)
        feedback = str(self.channel.replies[0][1])
        self.assertIn("无法保存", feedback)
        self.assertIn("本条消息未提交", feedback)
        self.assertNotIn("private", feedback)

    async def test_file_only_submits_empty_authored_request_and_original_bytes(self):
        message = file_message(name="/stop $untrusted.txt")
        self.channel.resource_bodies[(message.id, "file_report")] = b"original document"

        await self.app.handle_message(message)

        self.assertEqual(len(self.runtime.submit_calls), 1)
        submitted = self.runtime.submit_calls[0]
        prefix, metadata = split_attachments(submitted["input"])
        request, current = plain_prompt_projection(prefix)
        self.assertEqual(request, "")
        self.assertEqual(current["message_id"], message.id)
        self.assertEqual(submitted["skill_names"], ())
        self.assertEqual(self.runtime.stop_calls, [])
        self.assertEqual(self.channel.download_resource_calls, [("file_report", "file", message.id)])
        self.assertEqual(metadata["files"][0]["source"], "current_message")
        self.assertEqual(metadata["files"][0]["file_name"], "/stop $untrusted.txt")
        self.assert_original(metadata)
        self.assertEqual(self.channel.replies, [])

    async def normalized_post(self, post, *, topic=False):
        pipeline = InboundPipeline(
            PipelineConfig(inbound=InboundConfig(include_raw=True)),
            PipelineDeps(resolve_names=lambda ids: {item: "Current User" for item in ids}),
        )
        pipeline.set_bot_open_id("ou_bot")
        event = {
            "message_id": "om_normalized_file", "chat_id": "oc_direct",
            "chat_type": "p2p", "message_type": "post", "content": json.dumps(post),
        }
        if topic:
            event.update(
                chat_id="oc_group", chat_type="group", thread_id="omt_files",
                mentions=[{"key": "@_user_1", "id": {"open_id": "ou_bot"}, "name": "椰羊"}],
            )
        message = await pipeline.normalize(
            message_event=event,
            sender={"sender_id": {"open_id": "ou_user"}, "sender_type": "user"},
        )
        assert message is not None
        self.channel.resource_bodies[(message.id, "file_report")] = b"original document"
        return message

    async def test_topic_placeholder_mention_and_file_without_authored_body_submits(self):
        message = await self.normalized_post({
            "content": [[{"tag": "at", "user_id": "@_user_1", "user_name": "椰羊"}]],
            "files": [{"file_key": "file_report", "file_name": "report.txt"}],
        }, topic=True)
        self.assertIn("@椰羊", message.body_text)
        self.assertTrue(message.mentioned_bot)
        created = await self.fixture.create_binding(self.app._scope(message))
        self.runtime.submission = Submission(
            SubmitDisposition.STARTED, created.binding.id, "native-topic", "turn-topic", lambda: None,
        )

        await self.app.handle_message(message)

        self.assertEqual(len(self.runtime.submit_calls), 1)
        prefix, metadata = split_attachments(self.runtime.submit_calls[0]["input"])
        request, _ = plain_prompt_projection(prefix)
        self.assertEqual(request, '<at target="self">椰羊</at>')
        self.assertEqual(self.channel.download_resource_calls, [("file_report", "file", message.id)])
        self.assert_original(metadata)
        self.assertEqual(self.channel.replies, [])

    async def test_post_with_only_top_level_files_submits_without_authored_body(self):
        message = await self.normalized_post({
            "files": [{"file_key": "file_report", "file_name": "$untrusted.txt"}],
        })

        await self.app.handle_message(message)

        self.assertEqual(len(self.runtime.submit_calls), 1)
        submitted = self.runtime.submit_calls[0]
        prefix, metadata = split_attachments(submitted["input"])
        request, _ = plain_prompt_projection(prefix)
        self.assertEqual(request, "")
        self.assertEqual(submitted["skill_names"], ())
        self.assertEqual(self.channel.download_resource_calls, [("file_report", "file", message.id)])
        self.assert_original(metadata)
        self.assertEqual(self.channel.replies, [])

    async def test_missing_file_ast_or_other_resource_without_body_still_rejects(self):
        for resources, post in (
            ([ResourceDescriptor(type="file", file_key="file_report")], {}),
            ([ResourceDescriptor(type="file", file_key="file_report")], {"title": "no body AST"}),
            ([ResourceDescriptor(type="file", file_key="file_report"),
              ResourceDescriptor(type="audio", file_key="audio_hidden")],
             {"files": [{"file_key": "file_report", "file_name": "report.txt"}]}),
        ):
            with self.subTest(post=post):
                message = FakeMessage(
                    "", message_id="om_invalid_post", raw_content_type="post",
                    content=PostContent(post=post), resources=resources,
                )
                await self.app.handle_message(message)
        self.assertEqual(self.runtime.submit_calls, [])
        self.assertEqual(self.channel.download_resource_calls, [])
        self.assertEqual(len(self.channel.replies), 3)

    async def test_post_preserves_authored_text_and_skill_without_parsing_filename(self):
        self.runtime.available_capabilities = frozenset({NativeCapability.SKILLS})
        message = post_message("$real 分析附件", files=[("file_report", "/stop $fake.txt")])
        self.channel.resource_bodies[(message.id, "file_report")] = b"original document"

        await self.app.handle_message(message)

        self.assertEqual(len(self.runtime.submit_calls), 1)
        submitted = self.runtime.submit_calls[0]
        prefix, metadata = split_attachments(submitted["input"])
        request, _ = plain_prompt_projection(prefix)
        self.assertEqual(request, "$real 分析附件")
        self.assertEqual(submitted["skill_names"], ("real",))
        self.assertEqual(self.runtime.stop_calls, [])
        self.assertNotIn("$fake", submitted["input"])
        self.assertEqual(metadata["files"][0]["file_name"], "/stop $fake.txt")
        self.assert_original(metadata)

    async def test_current_image_and_file_keep_one_complete_final_text_prompt(self):
        message = post_message("分析这两个附件", images=("img_chart",))
        self.channel.resource_bodies[(message.id, "file_report")] = b"original document"
        self.channel.resource_bodies[(message.id, "img_chart")] = PNG

        await self.app.handle_message(message)

        self.assertEqual(len(self.runtime.submit_calls), 1)
        native = self.runtime.submit_calls[0]["input"]
        self.assertIsInstance(native, list)
        self.assertTrue(any(isinstance(item, ImageInput) for item in native))
        self.assertIsInstance(native[-1], TextInput)
        prefix, metadata = split_attachments(native)
        self.assertIn("分析这两个附件", prefix)
        self.assert_original(metadata)
        self.assertEqual(sum("分析这两个附件" in item.text for item in native if isinstance(item, TextInput)), 1)

    async def test_explicit_quote_downloads_only_that_exact_message_file(self):
        quoted = quoted_inbound(
            message_id="om_exact_quote", raw_content_type="file", content_text="quoted.pdf",
            content=FileContent(file_key="file_quote", file_name="quoted.pdf"),
            resources=[ResourceDescriptor(type="file", file_key="file_quote", file_name="quoted.pdf")],
        )
        self.channel.inbound_messages[quoted.id] = quoted
        self.channel.resource_bodies[(quoted.id, "file_quote")] = b"original document"
        message = FakeMessage("查看这个文件", message_id="om_reply", reply_id=quoted.id)

        await self.app.handle_message(message)

        self.assertEqual(len(self.runtime.submit_calls), 1)
        self.assertEqual(self.channel.fetch_inbound_calls, [quoted.id])
        self.assertEqual(self.channel.download_resource_calls, [("file_quote", "file", quoted.id)])
        _, metadata = split_attachments(self.runtime.submit_calls[0]["input"])
        self.assertEqual(metadata["files"][0]["source"], "quoted_message")
        self.assertNotIn("message_id", metadata["files"][0])
        self.assertNotIn("file_key", metadata["files"][0])
        self.assert_original(metadata)

    async def test_catch_up_keeps_history_file_metadata_without_downloading_it(self):
        scope = FeishuScope("cli_test", "oc_group", ScopeKind.GROUP)
        lower = MessageContextAnchor("om_lower", 1000)
        upper = MessageContextAnchor("om_current_file", 3000)
        created = await self.fixture.management.create_current_binding(
            scope=scope, creator_id="ou_user", project_alias="test",
            message_context_mode=MentionContextMode.CATCH_UP, context_anchor=lower,
        )
        reference = MessageHistoryRef("om_history_file", 2000, "ou_alice", "file")
        self.fixture.message_history.window = MessageHistoryWindow(
            lower, upper, (reference,), MessageHistoryStats(1, 3, 0, 0, 0, False, False),
        )
        history = file_message(
            message_id=reference.message_id, key="file_history", name="history.pdf",
            chat_id=scope.chat_id, chat_type="group", sender_id=reference.sender_id,
            create_time=2000,
        )
        self.channel.inbound_messages[history.id] = history
        current = file_message(
            message_id=upper.message_id, chat_id=scope.chat_id, chat_type="group", create_time=3000,
        )
        self.channel.resource_bodies[(current.id, "file_report")] = b"original document"
        self.runtime.submission = Submission(
            SubmitDisposition.STARTED, created.binding.id, "native-group", "turn-group", lambda: None,
        )

        await self.app.handle_message(current)

        self.assertEqual(len(self.runtime.submit_calls), 1)
        submitted = self.runtime.submit_calls[0]
        prefix, metadata = split_attachments(submitted["input"])
        envelope = json.loads(prefix)
        self.assertIn("history.pdf", json.dumps(envelope["supplemental_messages"]))
        self.assertEqual(self.channel.download_resource_calls, [("file_report", "file", current.id)])
        self.assertEqual(submitted["context_commit"].anchor, upper)
        self.assert_original(metadata)

    async def test_any_file_download_failure_rejects_whole_input_without_retention(self):
        message = post_message("分析文件", files=[("file_good", "good.txt"), ("file_bad", "bad.txt")])
        self.channel.resource_bodies[(message.id, "file_good")] = b"first file"
        self.channel.resource_bodies[(message.id, "file_bad")] = RuntimeError("unavailable")

        await self.app.handle_message(message)

        self.assertEqual(self.runtime.submit_calls, [])
        self.assertEqual(self.retained_batches(), [])
        self.assertEqual(len(self.channel.download_resource_calls), 2)
        self.assertEqual(len(self.channel.replies), 1)
        self.assertIn("bad.txt", str(self.channel.replies[0][1]))
        self.assertIn("未提交", str(self.channel.replies[0][1]))

    async def test_preparation_race_retains_exact_admission_and_discards_rejected_files(self):
        old_admission = SubmissionAdmission(
            self.binding.id, 7, "native-old", "turn-old", self.binding.settings_revision,
            self.binding.context_revision, self.binding.feedback_revision,
        )
        self.runtime.admission = old_admission
        started = asyncio.Event()
        release = asyncio.Event()
        submissions = []

        async def download(*_args, **_kwargs):
            self.assertEqual(self.runtime.capture_calls, [self.binding.id])
            started.set()
            await release.wait()
            return b"original document"

        async def reject(**kwargs):
            submissions.append(kwargs)
            raise SteerRace("original turn ended")

        self.channel.download_resource = download
        self.runtime.submit = reject
        task = asyncio.create_task(self.app.handle_message(file_message()))
        self.addAsyncCleanup(self._cancel_pending, task)
        await asyncio.wait_for(started.wait(), 2)
        self.runtime.admission = SubmissionAdmission(
            self.binding.id, 8, None, None, self.binding.settings_revision,
            self.binding.context_revision, self.binding.feedback_revision,
        )
        release.set()
        await asyncio.wait_for(task, 2)

        self.assertEqual(len(submissions), 1)
        self.assertIs(submissions[0]["admission"], old_admission)
        self.assertEqual(self.runtime.capture_calls, [self.binding.id])
        _, metadata = split_attachments(submissions[0]["input"])
        self.assertFalse(Path(metadata["files"][0]["local_path"]).exists())
        self.assertEqual(self.retained_batches(), [])

    @staticmethod
    async def _cancel_pending(task):
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_unknown_native_result_and_accepted_cursor_failure_keep_originals(self):
        for index, error in enumerate((
            TurnStartFailed("RPC response lost"),
            ContextBoundaryCommitFailed("native accepted; cursor commit failed"),
        )):
            with self.subTest(error=type(error).__name__):
                submissions = []

                async def fail(**kwargs):
                    submissions.append(kwargs)
                    raise error

                self.runtime.submit = fail
                message = file_message(message_id=f"om_uncertain_{index}")
                self.channel.resource_bodies[(message.id, "file_report")] = b"original document"

                await self.app.handle_message(message)

                self.assertEqual(len(submissions), 1)
                _, metadata = split_attachments(submissions[0]["input"])
                self.assert_original(metadata)

    async def test_control_and_goal_commands_with_files_are_rejected_before_download(self):
        self.runtime.available_capabilities = frozenset({NativeCapability.GOAL, NativeCapability.SIDE})
        for index, text in enumerate(("/stop", "/status", "/goal 修复项目", "/side")):
            with self.subTest(text=text):
                message = post_message(text, message_id=f"om_control_{index}")
                await self.app.handle_message(message)
                self.assertIn("不能携带", str(self.channel.replies[-1][1]))
        self.assertEqual(self.channel.download_resource_calls, [])
        self.assertEqual(self.runtime.submit_calls, [])
        self.assertEqual(self.runtime.start_goal_calls, [])
        self.assertEqual(self.runtime.stop_calls, [])
        self.assertEqual(self.runtime.create_side_calls, [])


class SideFileInputChannelTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = await self.enterAsyncContext(side_channel_fixture())
        self.app = self.fixture.app
        self.channel = self.fixture.channel
        self.runtime = self.fixture.runtime
        self.attachments = AttachmentStore(self.fixture.project_root / "attachments")
        self.app._input_preparer = MessageInputPreparer(
            channel=self.channel, attachment_store=self.attachments,
        )
        source = FakeMessage("/side", message_id="om_source", chat_id="oc-direct", mentioned_bot=False)
        self.binding = self.fixture.binding_for(source)
        self.fixture.queue_promoted_topic(
            chat_id="oc-direct", root_id="om_root", seed_id="om_seed", topic_id="omt_side",
        )
        await self.app.handle_message(source)
        self.side = self.fixture.store.side_topic_for_source(app_id="cli_test", source_message_id=source.id)
        assert self.side is not None

    def message(self, **kwargs):
        return file_message(chat_id="oc-direct", thread_id=self.side.topic_id, mentioned_bot=False, **kwargs)

    async def test_side_file_uses_its_exact_admission_without_submitting_parent(self):
        message = self.message()
        self.channel.resource_bodies[(message.id, "file_report")] = b"side document"

        await self.app.handle_message(message)

        self.assertEqual(self.runtime.submit_calls, [])
        self.assertEqual(len(self.runtime.submit_side_calls), 1)
        submitted = self.runtime.submit_side_calls[0]
        self.assertEqual(submitted["side_id"], self.side.id)
        self.assertEqual(submitted["admission"].side_id, self.side.id)
        self.assertEqual(submitted["admission"].thread_id, self.runtime.side_snapshot(self.side.id).thread_id)
        self.assertEqual(submitted["skill_names"], ())
        _, metadata = split_attachments(submitted["input"])
        self.assertEqual(Path(metadata["files"][0]["local_path"]).read_bytes(), b"side document")

    async def test_side_rejected_steer_cleans_files_but_unknown_rpc_keeps_them(self):
        for index, error in enumerate((SteerRace("ended"), TurnStartFailed("unknown result"))):
            with self.subTest(error=type(error).__name__):
                submissions = []

                async def fail(**kwargs):
                    submissions.append(kwargs)
                    raise error

                self.runtime.submit_side = fail
                message = self.message(message_id=f"om_side_error_{index}")
                self.channel.resource_bodies[(message.id, "file_report")] = b"side document"
                await self.app.handle_message(message)

                self.assertEqual(len(submissions), 1)
                _, metadata = split_attachments(submissions[0]["input"])
                path = Path(metadata["files"][0]["local_path"])
                self.assertEqual(path.exists(), isinstance(error, TurnStartFailed))
        self.assertEqual(self.runtime.submit_calls, [])
