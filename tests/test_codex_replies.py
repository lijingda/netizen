import unittest
from dataclasses import fields, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from openai_codex.types import TurnError

from netizen_cli.bindings import BindingTaskFeedback
from netizen_cli.file_evidence import DiffPath, OutputPath
from netizen_cli.partial_answers import PartialAnswer
from netizen_cli.runtime.codex_replies import codex_completion_handler, ordinary_conclusion
from netizen_cli.runtime.contracts import (
    CompactionOutcome, TerminalStateUnknown, TurnObservationUnavailableOutcome, TurnOutcome,
)

from netizen_cli.turn_patch_children import TaskPatchChildren
from tests.support.channel_results import completed_turn_result, file_change_item, turn_activity_snapshot


class OrdinaryProjectionTests(unittest.TestCase):
    def test_every_outcome_field_is_accounted_for(self):
        carried = dict(
            binding_id="binding", thread_id="thread", turn_id="turn", owner_id="owner", origin=object(),
            cwd=Path("/project"), background_cleanup_requested=True, feedback_revision=3,
            task_feedback=BindingTaskFeedback(progress_card_enabled=True),
            activity=turn_activity_snapshot(binding_id="binding", thread_id="thread", turn_id="turn"),
            partial_answers=(PartialAnswer("thread", "turn", "item", "partial"),),
        )
        derived = {"result", "turn_diff", "patch_children", "error"}
        excluded: dict[str, str] = {}  # Each intentional omission needs a reason.
        self.assertEqual({field.name for field in fields(TurnOutcome)}, carried.keys() | derived | excluded.keys())
        self.assertTrue(all(reason.strip() for reason in excluded.values()))

        turn = TurnOutcome(
            **carried, result=completed_turn_result(file_change_item("written"), final_response="answer"),
            turn_diff="diff --git a/aggregate b/aggregate", patch_children=TaskPatchChildren(complete=False),
        )
        conclusion = ordinary_conclusion(turn)
        for name, value in carried.items():
            with self.subTest(field=name):
                self.assertEqual(getattr(conclusion, name), value)
        self.assertEqual((conclusion.status, conclusion.final_response), ("completed", "answer"))
        self.assertEqual(conclusion.files.outputs, (OutputPath("written"),))
        self.assertEqual(conclusion.files.aggregate_paths, (DiffPath("aggregate", False),))
        self.assertFalse(conclusion.files.patch_counts_complete)
        failed = ordinary_conclusion(replace(turn, error=RuntimeError("delivery failed")))
        self.assertEqual(failed.error_detail, "delivery failed")


class CompletionDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_ordinary_sink_receives_only_projected_result_and_exact_notice(self):
        ordinary = AsyncMock()
        feature = AsyncMock()
        dispatch = codex_completion_handler(ordinary, feature, AsyncMock())
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
