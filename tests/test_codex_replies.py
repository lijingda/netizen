import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from openai_codex.types import TurnError

from netizen_cli.runtime.codex_replies import codex_completion_handler
from netizen_cli.runtime.contracts import (
    CompactionOutcome, TerminalStateUnknown, TurnObservationUnavailableOutcome, TurnOutcome,
)


class CompletionDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_ordinary_sink_receives_only_projected_result_and_exact_notice(self):
        ordinary = AsyncMock()
        feature = AsyncMock()
        dispatch = codex_completion_handler(ordinary, feature)
        origin = object()
        turn = TurnOutcome(
            "binding", "thread", "turn", "owner", origin,
            result=SimpleNamespace(status="failed", final_response=None,
                                   error=TurnError(message="provider reason")),
            error=RuntimeError("delivery reason"),
        )
        await dispatch(turn)
        ordinary.assert_awaited_once()
        conclusion = ordinary.await_args.args[0]
        self.assertIs(conclusion.origin, origin)
        self.assertEqual((conclusion.binding_id, conclusion.thread_id, conclusion.turn_id),
                         ("binding", "thread", "turn"))
        self.assertEqual(conclusion.status, "failed")
        self.assertEqual(conclusion.error_detail, "delivery reason")
        self.assertEqual(conclusion.native_failure_detail, "provider reason")
        self.assertFalse(hasattr(conclusion, "result"))
        notice = TurnObservationUnavailableOutcome(
            "binding", "thread", "turn", "owner", origin, TerminalStateUnknown("unavailable"),
        )
        await dispatch(notice)
        self.assertIs(ordinary.await_args.args[0], notice)
        feature.assert_not_awaited()
        compaction = CompactionOutcome(
            binding_id="binding", thread_id="thread", compact_turn_id="turn", owner_id="owner", origin=origin,
        )
        await dispatch(compaction)
        feature.assert_awaited_once_with(compaction)
        self.assertEqual(ordinary.await_count, 2)
