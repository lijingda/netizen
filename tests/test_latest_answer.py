from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from openai_codex import InvalidRequestError

from netizen_cli.codex_runtime import CodexRuntime
from netizen_cli.runtime.contracts import NativeLatestAnswer


def message(text, phase=None, kind="agentMessage"):
    return SimpleNamespace(root=SimpleNamespace(type=kind, text=text, phase=phase))


class LatestAnswerTest(unittest.IsolatedAsyncioTestCase):
    async def test_reads_latest_full_turn_once_and_never_uses_a_tail(self):
        runtime = object.__new__(CodexRuntime)
        runtime._codex = object()
        # The answer is intentionally outside the last 20 items.
        turn = SimpleNamespace(id="latest", status="completed", started_at=100, completed_at=120,
                               items=[message("final", "final_answer")]
                               + [message("tool", kind="commandExecution") for _ in range(30)])
        history = SimpleNamespace(list_turns=AsyncMock(return_value=SimpleNamespace(data=[turn])))
        with patch("netizen_cli.codex_runtime.AppServerThreadHistory", return_value=history):
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
                with patch("netizen_cli.codex_runtime.AppServerThreadHistory", return_value=history):
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
        with patch("netizen_cli.codex_runtime.AppServerThreadHistory", return_value=history):
            self.assertEqual(await runtime.latest_thread_answer("thread"), NativeLatestAnswer("thread", None, None, None))
            for rejection in ("thread not found: thread", message.replace("thread thread", "thread other"), "history unavailable"):
                history.list_turns.side_effect = InvalidRequestError(-32600, rejection)
                with self.assertRaises(InvalidRequestError):
                    await runtime.latest_thread_answer("thread")

    async def test_empty_history_is_not_an_old_answer_and_errors_propagate(self):
        runtime = object.__new__(CodexRuntime)
        runtime._codex = object()
        history = SimpleNamespace(list_turns=AsyncMock(return_value=SimpleNamespace(data=[])))
        with patch("netizen_cli.codex_runtime.AppServerThreadHistory", return_value=history):
            result = await runtime.latest_thread_answer("thread")
            self.assertEqual(result, NativeLatestAnswer("thread", None, None, None))
            history.list_turns.side_effect = TimeoutError("unavailable")
            with self.assertRaises(TimeoutError):
                await runtime.latest_thread_answer("thread")
