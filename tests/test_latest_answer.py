from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from openai_codex import InvalidRequestError
from openai_codex.generated.v2_all import ThreadItem

from netizen_cli.codex_runtime import CodexRuntime
from netizen_cli.runtime.contracts import NativeAnswerImage, NativeLatestAnswer


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
        history = SimpleNamespace(list_turns=AsyncMock(return_value=SimpleNamespace(data=turns, next_cursor="older")))
        with patch("netizen_cli.runtime.queries.AppServerThreadHistory", return_value=history):
            result = await self.runtime.thread_conversation("thread", cursor="opaque")
        self.assertIsInstance(result, NativeConversationPage)
        history.list_turns.assert_awaited_once_with("thread", cursor="opaque", limit=10, items_view="summary")
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
        history = SimpleNamespace(list_turns=AsyncMock(return_value=summary))
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

    async def test_full_read_is_only_for_completed_latest_with_possible_markdown_image(self):
        for status, answer in (("completed", "plain answer"), ("completed", None),
                               ("inProgress", "![image](/image.png)"), ("failed", "![image](/image.png)"),
                               ("interrupted", "![image](/image.png)")):
            with self.subTest(status=status, answer=answer):
                page = SimpleNamespace(data=[self.turn("latest", status, answer=answer),
                                             self.turn("older", answer="![older](/image.png)")], next_cursor="next")
                history = SimpleNamespace(list_turns=AsyncMock(return_value=page))
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
        history = SimpleNamespace(list_turns=AsyncMock(return_value=summary))
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
            for limit in (0, 2, 11, True):
                with self.assertRaises(ValueError):
                    await self.runtime.thread_conversation("thread", limit=limit)
