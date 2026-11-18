from __future__ import annotations

import asyncio
from dataclasses import asdict
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from openai_codex import CodexError, InvalidRequestError
from openai_codex.errors import MethodNotFoundError
from openai_codex.generated.v2_all import ThreadItem, ThreadItemsListResponse
from pydantic import ValidationError

from netizen_cli.codex_runtime import CodexRuntime
from netizen_cli.runtime.contracts import NativeAnswerImage, NativeLatestAnswer
from netizen_cli.runtime.state import RUNTIME_LOGGER_NAME
from netizen_cli.delegation_envelope import SIDE_SOURCE_NOTICE, delegated_prompt
from netizen_cli.thread_history import ThreadHistoryUnavailable


def message(text, phase=None, kind="agentMessage"):
    return SimpleNamespace(root=SimpleNamespace(type=kind, text=text, phase=phase))


class LatestAnswerTest(unittest.IsolatedAsyncioTestCase):
    async def test_images_come_only_from_completed_generation_in_the_same_completed_turn(self):
        runtime = object.__new__(CodexRuntime)
        runtime._codex = object()
        image = ThreadItem.model_validate({
            "type": "imageGeneration", "id": "image", "status": "completed",
            "result": "native-png-base64", "savedPath": "/original-thread/image.png",
        })
        ignored = [SimpleNamespace(root=SimpleNamespace(type="imageView", path="/view.png")),
                   SimpleNamespace(root=SimpleNamespace(type="imageGeneration", status="inProgress",
                                                       saved_path="/pending.png", result="pending")),
                   SimpleNamespace(root=SimpleNamespace(type="imageGeneration", status="completed",
                                                       saved_path=None, result="no-path"))]
        for status in ("completed", "failed", "interrupted", "inProgress"):
            with self.subTest(status=status):
                turn = SimpleNamespace(id="latest", status=status,
                                       items=[image, *ignored, message("![image](/original-thread/image.png)", "final_answer")])
                history = SimpleNamespace(list_turns=AsyncMock(return_value=SimpleNamespace(data=[turn])))
                with patch("netizen_cli.runtime.queries.AppServerThreadHistory", return_value=history):
                    result = await runtime.latest_thread_answer("fork")
                expected = (NativeAnswerImage("/original-thread/image.png", "native-png-base64"),) if status == "completed" else ()
                self.assertEqual(result.images, expected)
                history.list_turns.assert_awaited_once_with("fork", limit=1, items_view="full")
        turn.status = "completed"
        turn.items = [image]
        with patch("netizen_cli.runtime.queries.AppServerThreadHistory", return_value=history):
            self.assertEqual((await runtime.latest_thread_answer("fork")).images, ())

    async def test_reads_latest_full_turn_once_and_never_uses_a_tail(self):
        runtime = object.__new__(CodexRuntime)
        runtime._codex = object()
        # The answer is intentionally outside the last 20 items.
        turn = SimpleNamespace(id="latest", status="completed", started_at=100, completed_at=120,
                               items=[message("final", "final_answer")]
                               + [message("tool", kind="commandExecution") for _ in range(30)])
        history = SimpleNamespace(list_turns=AsyncMock(return_value=SimpleNamespace(data=[turn])))
        with patch("netizen_cli.runtime.queries.AppServerThreadHistory", return_value=history):
            result = await runtime.latest_thread_answer("archived-thread")
        self.assertEqual(result, NativeLatestAnswer("archived-thread", "latest", "completed", "final", 100, 120))
        history.list_turns.assert_awaited_once_with("archived-thread", limit=1, items_view="full")

    async def test_phase_compatibility_and_no_answer_states(self):
        runtime = object.__new__(CodexRuntime)
        runtime._codex = object()
        cases = [
            ("completed", [message("legacy")], "legacy"),
            ("inProgress", [message("unfinished legacy")], None),
            ("failed", [message("legacy terminal")], "legacy terminal"),
            ("interrupted", [message("legacy terminal")], "legacy terminal"),
            ("completed", [message("commentary", "commentary"), message("partial", "partial_answer")], None),
            ("inProgress", [message("streaming final", "final_answer")], None),
            ("failed", [message("published final", "final_answer")], "published final"),
            ("completed", [message("old", "final_answer"), message("new", "final_answer")], "new"),
        ]
        for status, items, expected in cases:
            with self.subTest(status=status, expected=expected):
                turn = SimpleNamespace(id="latest", status=status, items=items)
                history = SimpleNamespace(list_turns=AsyncMock(return_value=SimpleNamespace(data=[turn])))
                with patch("netizen_cli.runtime.queries.AppServerThreadHistory", return_value=history):
                    result = await runtime.latest_thread_answer("thread")
                self.assertEqual(result.answer, expected)
                self.assertEqual(result.status, status)
                self.assertIsNone(result.started_at)
                self.assertIsNone(result.completed_at)
                self.assertEqual(history.list_turns.await_count, 1)

    async def test_only_exact_zero_input_rejection_becomes_no_turn(self):
        runtime = object.__new__(CodexRuntime)
        runtime._codex = object()
        message = "thread thread is not materialized yet; thread/turns/list is unavailable before first user message"
        history = SimpleNamespace(list_turns=AsyncMock(side_effect=InvalidRequestError(-32600, message)))
        with patch("netizen_cli.runtime.queries.AppServerThreadHistory", return_value=history):
            self.assertEqual(await runtime.latest_thread_answer("thread"), NativeLatestAnswer("thread", None, None, None))
            for rejection in ("thread not found: thread", message.replace("thread thread", "thread other"), "history unavailable"):
                history.list_turns.side_effect = InvalidRequestError(-32600, rejection)
                with self.assertRaises(InvalidRequestError):
                    await runtime.latest_thread_answer("thread")

    async def test_empty_history_is_not_an_old_answer_and_errors_propagate(self):
        runtime = object.__new__(CodexRuntime)
        runtime._codex = object()
        history = SimpleNamespace(list_turns=AsyncMock(return_value=SimpleNamespace(data=[])))
        with patch("netizen_cli.runtime.queries.AppServerThreadHistory", return_value=history):
            result = await runtime.latest_thread_answer("thread")
            self.assertEqual(result, NativeLatestAnswer("thread", None, None, None))
            history.list_turns.side_effect = TimeoutError("unavailable")
            with self.assertRaises(TimeoutError):
                await runtime.latest_thread_answer("thread")


class ConversationPageTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime = object.__new__(CodexRuntime)
        self.runtime._codex = object()
        self.runtime._bindings = SimpleNamespace(find_by_native_thread_id=Mock(return_value=None))

    def inputless_turn(self, identity, status="completed"):
        turn = self.turn(identity, status)
        turn.items = [message("final", "final_answer")]
        return turn

    def first_item_page(self, item=None, turn_id="turn"):
        return ThreadItemsListResponse.model_validate({
            "data": [] if item is None else [{"turnId": turn_id, "item": item}],
        })

    def summary_history(self, **list_turns):
        return SimpleNamespace(list_turns=AsyncMock(**list_turns), first_item=AsyncMock(return_value=self.first_item_page()))

    def delegation(self, prompt="request", namespace="codex_tui"):
        return {"type": "functionCallOutput", "id": "input", "name": "send_message_to_thread",
                "namespace": namespace, "output": delegated_prompt("source-full-thread-id", prompt)}

    async def test_first_item_projects_full_delegation_and_side_source_including_running_turn(self):
        prompt = "检查 <tag> &amp;\n" + "完整正文" * 3000
        for namespace in ("codex_tui", "codex_app"):
            for side in (False, True):
                for status in ("completed", "inProgress"):
                    with self.subTest(namespace=namespace, side=side, status=status):
                        lookup = self.runtime._bindings.find_by_native_thread_id
                        lookup.reset_mock()
                        history = SimpleNamespace(
                            list_turns=AsyncMock(return_value=SimpleNamespace(
                                data=[self.inputless_turn("turn", status)], next_cursor="next")),
                            first_item=AsyncMock(return_value=self.first_item_page(
                                self.delegation((SIDE_SOURCE_NOTICE if side else "") + prompt, namespace))),
                        )
                        with patch("netizen_cli.runtime.queries.AppServerThreadHistory", return_value=history):
                            page = await self.runtime.thread_conversation("thread")
                        projected = page.turns[0]
                        self.assertEqual(projected.status, status)
                        self.assertEqual(projected.answer, "final" if status == "completed" else None)
                        self.assertEqual(len(projected.inputs), 1)
                        part = projected.inputs[0]
                        self.assertEqual((part.kind, part.text), ("session_agent", prompt))
                        self.assertEqual(part.source_thread_id, "source-full-thread-id")
                        self.assertEqual(part.source_is_side, side)
                        if side:
                            lookup.assert_called_once_with("source-full-thread-id")
                        else:
                            lookup.assert_not_called()
                        self.assertNotIn("send_message_to_thread", str(asdict(page)))
                        history.first_item.assert_awaited_once_with("thread", "turn")

    async def test_side_notice_is_verbatim_for_binding_source_or_failed_lookup(self):
        prompt = SIDE_SOURCE_NOTICE + "full request"
        for result in (object(), RuntimeError("lookup failed")):
            with self.subTest(result=result):
                lookup = Mock(side_effect=[result])
                self.runtime._bindings.find_by_native_thread_id = lookup
                history = self.summary_history(return_value=SimpleNamespace(data=[self.turn("turn")], next_cursor=None))
                history.first_item.return_value = self.first_item_page(self.delegation(prompt))
                with patch("netizen_cli.runtime.queries.AppServerThreadHistory", return_value=history):
                    page = await self.runtime.thread_conversation("thread")
                part, = page.turns[0].inputs
                self.assertEqual((part.kind, part.source_thread_id, part.text), ("session_agent", "source-full-thread-id", prompt))
                self.assertFalse(part.source_is_side)
                lookup.assert_called_once_with("source-full-thread-id")

    async def test_unrecognized_external_inputs_never_expose_output_or_legacy_envelope(self):
        private = "PRIVATE-TOOL-OUTPUT"
        for changes in (
            {"namespace": "another", "output": delegated_prompt("source", private)},
            {"name": "read_thread", "output": delegated_prompt("source", private)},
            {"output": delegated_prompt("", private)}, {"output": delegated_prompt(" \t ", private)},
            {"namespace": "netizen_sessions", "output": '{"sourceThreadId":"source","sourceBindingId":"binding","message":"' + private + '"}'},
            {"output": private}, {"output": delegated_prompt("source", private)[:-1]},
            {"output": [{"type": "input_text", "text": private}]},
        ):
            with self.subTest(changes=changes):
                history = SimpleNamespace(
                    list_turns=AsyncMock(return_value=SimpleNamespace(data=[self.turn("turn")], next_cursor=None)),
                    first_item=AsyncMock(return_value=self.first_item_page({**self.delegation(), **changes})),
                )
                with patch("netizen_cli.runtime.queries.AppServerThreadHistory", return_value=history):
                    page = await self.runtime.thread_conversation("thread")
                self.assertEqual([part.kind for part in page.turns[0].inputs], ["external_unrecognized"])
                self.assertEqual(page.turns[0].inputs[0].text, "〔外部输入；格式未识别，此处不显示内容〕")
                self.assertNotIn(private, str(asdict(page)))

    async def test_other_first_items_and_empty_pages_never_become_input(self):
        for item in (None,
                     {"type": "hookPrompt", "id": "hook", "fragments": []},
                     {"type": "contextCompaction", "id": "compaction"},
                     {"type": "reasoning", "id": "reason", "summary": ["PRIVATE-REASONING"], "content": []},
                     {"type": "agentMessage", "id": "agent", "text": "PRIVATE-AGENT"},
                     {"type": "userMessage", "id": "user", "content": [{"type": "text", "text": "PRIVATE-USER"}]}):
            with self.subTest(item=item):
                turns = [self.inputless_turn("empty"), self.turn("user")]
                history = self.summary_history(return_value=SimpleNamespace(data=turns, next_cursor=None))
                history.first_item.side_effect = [self.first_item_page(item, turn.id) for turn in turns]
                with patch("netizen_cli.runtime.queries.AppServerThreadHistory", return_value=history):
                    page = await self.runtime.thread_conversation("thread")
                self.assertEqual(page.turns[0].inputs, ())
                self.assertEqual(page.turns[1].inputs[0].text, "first")
                self.assertNotIn("PRIVATE", str(asdict(page)))

    async def test_first_item_failure_stops_page_reads_and_warns_once_without_content(self):
        with self.assertRaises(ValidationError) as validation:
            ThreadItemsListResponse.model_validate({"data": "PRIVATE"})
        for error in (MethodNotFoundError(-32601, "PRIVATE"), CodexError("PRIVATE"),
                      ThreadHistoryUnavailable("PRIVATE"), validation.exception):
            with self.subTest(error=error):
                turns = [self.turn("ok"), self.inputless_turn("failed"), self.turn("user"),
                         self.inputless_turn("remaining")]
                # A userMessage with no content is still a Summary input.
                empty_user = self.turn("empty-user")
                empty_user.items[0].root.content = []
                turns.append(empty_user)
                history = SimpleNamespace(
                    list_turns=AsyncMock(return_value=SimpleNamespace(data=turns, next_cursor="next")),
                    first_item=AsyncMock(side_effect=[self.first_item_page(self.delegation(), "ok"), error]),
                )
                legacy = isinstance(error, MethodNotFoundError)
                log_check = self.assertNoLogs if legacy else self.assertLogs
                with patch("netizen_cli.runtime.queries.AppServerThreadHistory", return_value=history), \
                        log_check(RUNTIME_LOGGER_NAME, level="WARNING") as logs:
                    page = await self.runtime.thread_conversation("thread")
                self.assertEqual([turn.inputs[0].kind for turn in page.turns[:-1]],
                                 ["session_agent", "unavailable", "user", "unavailable"])
                self.assertEqual(page.turns[-1].inputs, ())
                self.assertEqual(page.turns[1].inputs[0].text, "〔未能读取本轮首条输入〕")
                self.assertEqual(page.next_cursor, "next")
                self.assertEqual([call.args for call in history.first_item.await_args_list],
                                 [("thread", "ok"), ("thread", "failed")])
                if not legacy:
                    self.assertEqual(len(logs.records), 1)
                    self.assertEqual(logs.records[0].thread_id, "thread")
                    self.assertEqual(logs.records[0].turn_id, "failed")
                    self.assertEqual(logs.records[0].error_type, type(error).__name__)
                    self.assertNotIn("PRIVATE", str(logs.output))

    async def test_first_item_deadline_is_checked_before_each_read(self):
        turns = [self.turn("steer"), self.inputless_turn("missing"), self.turn("user")]
        for start in (99, 100):
            with self.subTest(start=start):
                history = self.summary_history(return_value=SimpleNamespace(data=turns, next_cursor=None))
                with patch.object(asyncio.get_running_loop(), "time", return_value=start) as clock:
                    async def first(_thread_id, turn_id):
                        self.assertEqual(turn_id, "steer")
                        clock.return_value = 100
                        return self.first_item_page(self.delegation(), turn_id)
                    history.first_item.side_effect = first
                    with patch("netizen_cli.runtime.queries.AppServerThreadHistory", return_value=history), \
                            self.assertLogs(RUNTIME_LOGGER_NAME, level="WARNING"):
                        page = await self.runtime.thread_conversation("thread", deadline=100)
                self.assertEqual(history.first_item.await_count, 1 if start < 100 else 0)
                self.assertEqual([turn.inputs[0].kind for turn in page.turns],
                                 ["session_agent" if start < 100 else "user", "unavailable", "user"])

    async def test_first_item_cancellation_and_outer_timeout_propagate(self):
        for error in (asyncio.CancelledError(), TimeoutError()):
            history = SimpleNamespace(
                list_turns=AsyncMock(return_value=SimpleNamespace(data=[self.inputless_turn("turn")], next_cursor=None)),
                first_item=AsyncMock(side_effect=error),
            )
            with patch("netizen_cli.runtime.queries.AppServerThreadHistory", return_value=history):
                with self.assertRaises(type(error)):
                    await self.runtime.thread_conversation("thread")

    async def test_first_item_reads_are_sequential_in_page_order(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def read(_thread_id, turn_id):
            if turn_id == "first":
                entered.set()
                await release.wait()
            return self.first_item_page(turn_id=turn_id)
        history = SimpleNamespace(
            list_turns=AsyncMock(return_value=SimpleNamespace(
                data=[self.turn("first"), self.inputless_turn("second")], next_cursor=None)),
            first_item=AsyncMock(side_effect=read),
        )
        with patch("netizen_cli.runtime.queries.AppServerThreadHistory", return_value=history):
            task = asyncio.create_task(self.runtime.thread_conversation("thread"))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                self.assertEqual(history.first_item.await_count, 1)
            finally:
                release.set()
                await asyncio.wait_for(task, 1)
        self.assertEqual([call.args for call in history.first_item.await_args_list],
                         [("thread", "first"), ("thread", "second")])

    async def test_smaller_page_reuses_first_input_projection_including_empty_and_unavailable(self):
        for item in (self.delegation(), None, MethodNotFoundError(-32601, "unsupported")):
            for identity in ("turn", "different"):
                with self.subTest(item=item, identity=identity):
                    history = SimpleNamespace(list_turns=AsyncMock(side_effect=[
                        SimpleNamespace(data=[self.inputless_turn("turn")], next_cursor="next"),
                        SimpleNamespace(data=[self.inputless_turn(identity)], next_cursor="single-next"),
                    ]), first_item=AsyncMock())
                    history.first_item.side_effect = [
                        item if isinstance(item, Exception) else self.first_item_page(item),
                        self.first_item_page(self.delegation("changed"), identity),
                    ]
                    with patch("netizen_cli.runtime.queries.AppServerThreadHistory", return_value=history):
                        first = await self.runtime.thread_conversation("thread", cursor="older")
                        retry = await self.runtime.thread_conversation("thread", cursor="older", limit=1, previous_page=first)
                    self.assertEqual(history.first_item.await_count, 1 if identity == "turn" else 2)
                    if identity == "turn":
                        self.assertEqual(retry.turns[0].inputs, first.turns[0].inputs)
                    else:
                        self.assertEqual(retry.turns[0].inputs[0].text, "changed")
                    self.assertEqual(retry.next_cursor, "single-next")

    def turn(self, identity, status="completed", *, answer="final", error=None):
        def user(text):
            return SimpleNamespace(root=SimpleNamespace(type="userMessage", content=[
                SimpleNamespace(root=SimpleNamespace(type="text", text=text)),
                SimpleNamespace(root=SimpleNamespace(type="localImage", path="/private/image.png")),
                SimpleNamespace(root=SimpleNamespace(type="skill", name="secret", path="/private/skill")),
            ]))
        return SimpleNamespace(id=identity, status=status, started_at=100, completed_at=None,
                               error=SimpleNamespace(message=error), items=[user("first"), user("steered"),
                               message("commentary", "commentary"), message(answer, "final_answer")])

    async def test_summary_page_projects_first_input_in_all_states_and_terminal_answers(self):
        from netizen_cli.runtime.contracts import NativeConversationPage
        from dataclasses import asdict
        turns = [self.turn("running", "inProgress"), self.turn("failed", "failed", error="provider failed"),
                 self.turn("interrupted", "interrupted", answer=None), self.turn("complete")]
        history = self.summary_history(return_value=SimpleNamespace(data=turns, next_cursor="older"))
        with patch("netizen_cli.runtime.queries.AppServerThreadHistory", return_value=history):
            result = await self.runtime.thread_conversation("thread", cursor="opaque")
        self.assertIsInstance(result, NativeConversationPage)
        history.list_turns.assert_awaited_once_with("thread", cursor="opaque", limit=10, items_view="summary")
        self.assertEqual([call.args for call in history.first_item.await_args_list], [("thread", turn.id) for turn in turns])
        self.assertEqual(result.next_cursor, "older")
        self.assertEqual(result.turns[0].inputs[0].text, "first")
        self.assertIsNone(result.turns[0].answer)
        self.assertEqual(result.turns[1].error, "provider failed")
        self.assertIsNone(result.turns[2].answer, "legacy interrupted commentary must never become final")
        self.assertEqual(result.turns[3].inputs[0].text, "first")
        serialized = str(asdict(result))
        for private in ("steered", "commentary", "/private", "secret"):
            self.assertNotIn(private, serialized)
        self.assertEqual(len(result.turns[3].inputs), 3)
        self.assertEqual(result.turns[3].started_at, 100)
        self.assertIsNone(result.turns[3].completed_at)

    async def test_first_page_images_use_exact_latest_identity_without_retry(self):
        image = NativeAnswerImage("/image.png", "bytes")
        summary = SimpleNamespace(data=[self.turn("latest", answer="![image](/image.png)"), self.turn("older")], next_cursor="next")
        history = self.summary_history(return_value=summary)
        for identity in ("latest", "new-turn-started"):
            full = AsyncMock(return_value=NativeLatestAnswer("thread", identity, "completed", "answer", images=(image,)))
            with patch("netizen_cli.runtime.queries.AppServerThreadHistory", return_value=history), \
                    patch.object(self.runtime, "latest_thread_answer", full):
                page = await self.runtime.thread_conversation("thread")
            self.assertEqual(page.turns[0].images, (image,) if identity == "latest" else ())
            self.assertTrue(page.turns[0].latest)
            self.assertEqual(page.turns[1].images, ())
            full.assert_awaited_once_with("thread")
        history.list_turns.assert_awaited_with("thread", cursor=None, limit=10, items_view="summary")

    async def test_smaller_page_reuses_only_matching_images_without_another_full_read(self):
        image = NativeAnswerImage("/image.png", "bytes")
        for identity in ("latest", "different"):
            with self.subTest(identity=identity):
                history = self.summary_history(side_effect=[
                    SimpleNamespace(data=[self.turn("latest", answer="![image](/image.png)")], next_cursor="next"),
                    SimpleNamespace(data=[self.turn(identity, answer="![image](/image.png)")], next_cursor="single-next"),
                ])
                full = AsyncMock(return_value=NativeLatestAnswer("thread", "latest", "completed", "answer", images=(image,)))
                with patch("netizen_cli.runtime.queries.AppServerThreadHistory", return_value=history), \
                        patch.object(self.runtime, "latest_thread_answer", full):
                    first = await self.runtime.thread_conversation("thread")
                    retry = await self.runtime.thread_conversation("thread", limit=1, previous_page=first)
                full.assert_awaited_once_with("thread")
                self.assertEqual(retry.turns[0].images, (image,) if identity == "latest" else ())
                self.assertEqual(retry.next_cursor, "single-next")

    async def test_feishu_image_labels_merge_with_their_following_image(self):
        import json
        from openai_codex import TextInput
        from netizen_cli.image_inputs import ImageReference, PreparedImage, compose_multimodal_input

        async def project(content):
            turn = self.turn("image-turn")
            turn.items[0].root.content = content
            history = self.summary_history(return_value=SimpleNamespace(data=[turn], next_cursor=None))
            with patch("netizen_cli.runtime.queries.AppServerThreadHistory", return_value=history):
                return (await self.runtime.thread_conversation("thread")).turns[0].inputs

        for sources in (("current_message",), ("current_message", "current_message"),
                        ("supplemental_message", "quoted_message", "current_message")):
            rendered = compose_multimodal_input("authored request", images=tuple(
                PreparedImage(ImageReference(source, f"message-{i}", f"key-{i}"), "image/png", 10, "data:image/png;base64,eA==")
                for i, source in enumerate(sources)
            ))
            # The history text/image kinds correspond to the public input objects.
            content = [SimpleNamespace(type="text", text=part.text) if isinstance(part, TextInput)
                       else SimpleNamespace(type="image", url=part.url) for part in rendered]
            inputs = await project(content)
            self.assertEqual(len(inputs), len(sources) + 1)
            self.assertEqual(inputs[-1].text, "authored request")
            self.assertTrue(all(not part.raw for part in inputs))
            self.assertTrue(all("feishu_image_input" not in part.text and "data:" not in part.text for part in inputs))
            for source, part in zip(sources, inputs):
                self.assertIn({"current_message": "当前消息", "quoted_message": "引用", "supplemental_message": "补充上下文"}[source], part.text)
        label = json.loads(content[0].text)
        for changes in ({"extra": True}, {"count": "1"}, {"index": True}, {"size_bytes": None},
                        {"source": "future"}, {"ref": None}, {"mime_type": 1}):
            raw = json.dumps({**label, **changes})
            inputs = await project([SimpleNamespace(type="text", text=raw), content[1], content[-1]])
            self.assertEqual(inputs[0].text, raw)
            self.assertTrue(inputs[0].raw)
            self.assertEqual(len(inputs), 3)
        inputs = await project([content[0], content[-1]])
        self.assertTrue(inputs[0].raw, "a label without its following image stays raw")
        self.assertEqual(inputs[0].text, content[0].text)

    async def test_full_read_is_only_for_completed_latest_with_possible_markdown_image(self):
        for status, answer in (("completed", "plain answer"), ("completed", None),
                               ("inProgress", "![image](/image.png)"), ("failed", "![image](/image.png)"),
                               ("interrupted", "![image](/image.png)")):
            with self.subTest(status=status, answer=answer):
                page = SimpleNamespace(data=[self.turn("latest", status, answer=answer),
                                             self.turn("older", answer="![older](/image.png)")], next_cursor="next")
                history = self.summary_history(return_value=page)
                with patch("netizen_cli.runtime.queries.AppServerThreadHistory", return_value=history), \
                        patch.object(self.runtime, "latest_thread_answer", AsyncMock()) as full:
                    result = await self.runtime.thread_conversation("thread")
                    full.assert_not_awaited()
                self.assertEqual(result.turns[0].inputs[0].text, "first")
                self.assertTrue(result.turns[0].latest)
                self.assertEqual(result.turns[0].images, ())
        missing_input = self.turn("running", "inProgress")
        missing_input.items = [message("commentary", "commentary")]
        history.list_turns.return_value = SimpleNamespace(data=[missing_input], next_cursor=None)
        history.first_item = AsyncMock(return_value=self.first_item_page())
        with patch("netizen_cli.runtime.queries.AppServerThreadHistory", return_value=history):
            result = await self.runtime.thread_conversation("thread")
        self.assertEqual(result.turns[0].inputs, ())
        self.assertEqual(result.turns[0].status, "inProgress")
        self.assertIsNone(result.turns[0].answer)

    async def test_optional_image_read_errors_preserve_summary_and_warn_without_content(self):
        from openai_codex import CodexError
        from netizen_cli.thread_history import ThreadHistoryUnavailable
        from netizen_cli.runtime.state import RUNTIME_LOGGER_NAME

        summary = SimpleNamespace(data=[self.turn("latest", answer="private answer ![image](/image.png)")], next_cursor="next")
        history = self.summary_history(return_value=summary)
        for error in (CodexError("private error content"), ThreadHistoryUnavailable("private error content")):
            full = AsyncMock(side_effect=error)
            with patch("netizen_cli.runtime.queries.AppServerThreadHistory", return_value=history), \
                    patch.object(self.runtime, "latest_thread_answer", full), \
                    self.assertLogs(RUNTIME_LOGGER_NAME, level="WARNING") as logs:
                result = await self.runtime.thread_conversation("thread")
            self.assertEqual(result.turns[0].answer, "private answer ![image](/image.png)")
            self.assertEqual(result.turns[0].images, ())
            self.assertEqual(result.next_cursor, "next")
            self.assertNotIn("private", str(logs.output))
            self.assertEqual(logs.records[0].error_type, type(error).__name__)
            full.assert_awaited_once_with("thread")
        with patch("netizen_cli.runtime.queries.AppServerThreadHistory", return_value=history), \
                patch.object(self.runtime, "latest_thread_answer", AsyncMock(side_effect=TimeoutError())):
            with self.assertRaises(TimeoutError):
                await self.runtime.thread_conversation("thread")

    async def test_empty_unmaterialized_invalid_cursor_and_other_errors(self):
        from openai_codex.errors import JsonRpcError
        history = SimpleNamespace(list_turns=AsyncMock(return_value=SimpleNamespace(data=[], next_cursor=None)))
        with patch("netizen_cli.runtime.queries.AppServerThreadHistory", return_value=history), \
                patch.object(self.runtime, "latest_thread_answer", AsyncMock()) as full:
            self.assertEqual((await self.runtime.thread_conversation("thread")).turns, ())
            full.assert_not_awaited()
            history.list_turns.side_effect = InvalidRequestError(-32600,
                "thread thread is not materialized yet; thread/turns/list is unavailable before first user message")
            self.assertEqual((await self.runtime.thread_conversation("thread")).turns, ())
            for error in (JsonRpcError(-32600, "invalid cursor"), InvalidRequestError(-32600, "missing history"), TimeoutError()):
                history.list_turns.side_effect = error
                with self.assertRaises(type(error)):
                    await self.runtime.thread_conversation("thread", cursor="bad", limit=1)
