"""Decoded outbound operations at a short Turn's terminal handoff."""
import itertools
import json
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from netizen_cli.bindings import BindingTaskFeedback
from netizen_cli.partial_answers import PartialAnswer
from netizen_cli.process_activity import ProcessActivityWindow
from netizen_cli.progress_card_settings import ProgressCardSettings
from netizen_cli.runtime.contracts import TurnOutcome
from netizen_cli.turn_activity import TurnActivityEvent, TurnActivityKind, TurnActivityStatus, TurnPlanStepSnapshot, TurnPlanStepState
from tests.support.channel_fixtures import channel_fixture
from tests.support.channel_results import completed_turn_result, file_change_item, turn_activity_snapshot
from tests.support.presenter_traces import delivery_recorder

REFERENCE = Path(__file__).with_name("fixtures") / "short_turn_presenter_15548d5.json"


class ProcessActivityTest(unittest.TestCase):
    def test_immutable_samples_and_eviction_of_finished_items_only(self):
        settings = ProgressCardSettings(operation_limit=1)
        window = ProcessActivityWindow(lambda: 1_900_000_000)
        next_turn = ProcessActivityWindow(lambda: 1_900_000_000)
        started = TurnActivityEvent("private-native-id", TurnActivityKind.TOOL, TurnActivityStatus.IN_PROGRESS, 0)
        window.observe(started, settings)
        next_turn.observe(started, settings)
        row, = window.rows
        self.assertNotIn(started.item_id, row.identity)
        self.assertNotEqual(row.identity, next_turn.rows[0].identity)
        window.observe(TurnActivityEvent(started.item_id, started.kind, TurnActivityStatus.COMPLETED, 0), settings)
        self.assertEqual(window.rows[0].finished_at, 1_900_000_000)
        window.observe(TurnActivityEvent("second", started.kind, TurnActivityStatus.COMPLETED, 0), settings)
        self.assertIsNone(row.finished_at)
        self.assertNotIn(row, window.rows)
        self.assertEqual(len(window.rows), 1)

    def test_step_insertion_duplicate_and_rename(self):
        window = ProcessActivityWindow(lambda: 1_900_000_000)
        def step(name, status=TurnPlanStepState.IN_PROGRESS):
            return TurnPlanStepSnapshot(name, status)
        window.plan((step("check"), step("check", TurnPlanStepState.COMPLETED)))
        original, = window.rows
        self.assertIsNone(original.finished_at)
        window.plan((step("first"), step("check")))
        self.assertIn(original, window.rows)
        window.plan((step("first"), step("new")))
        self.assertIsNone(original.finished_at)
        self.assertNotIn(original, window.rows)
        self.assertEqual({row.text for row in window.rows}, {"first", "new"})


async def short_turn_trace(*, card, files, install=None):
    async with channel_fixture() as fx:
        origin = await fx.new()
        binding = fx.store.active_binding(fx.app._scope(origin).key)
        fx.runtime.materialize(binding.id, "native-one", cwd=fx.project)
        if install is not None:
            install(fx)
        (fx.project / "output.txt").write_text("synthetic")
        activity = turn_activity_snapshot(binding_id=binding.id, steps=(
            TurnPlanStepSnapshot("检查清单", TurnPlanStepState.IN_PROGRESS),
        ))
        partial = PartialAnswer(activity.thread_id, activity.turn_id, "partial-1", "阶段正文")
        items = (file_change_item("output.txt"),) if files else ()
        with ExitStack() as stack:
            numbers = itertools.count(1)
            stack.enter_context(patch("secrets.token_hex", side_effect=lambda n: next(numbers).to_bytes(n, "big").hex()))
            events = delivery_recorder(fx.channel, stack, include_reads=True)
            await fx.app.handle_completion(TurnOutcome(binding_id=binding.id, thread_id=activity.thread_id,
                turn_id=activity.turn_id, owner_id="ou_user", origin=origin, activity=activity,
                cwd=fx.project, result=completed_turn_result(*items, final_response="最终正文"),
                task_feedback=BindingTaskFeedback(progress_card_enabled=card), partial_answers=(partial,)))
        return json.loads(json.dumps(events, ensure_ascii=False).replace(str(fx.project_root.resolve()), "<root>"))


class TerminalProcessDifferential(unittest.IsolatedAsyncioTestCase):
    async def test_short_turn_content(self):
        expected = json.loads(REFERENCE.read_text())
        for card in (True, False):
            for files in (True, False):
                with self.subTest(card=card, files=files):
                    self.assertEqual(await short_turn_trace(card=card, files=files), expected[f"{card}/{files}"])
