"""Decoded outbound operations at a short Turn's terminal handoff."""
import itertools
import json
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from netizen_cli.bindings import BindingTaskFeedback
from netizen_cli.partial_answers import PartialAnswer
from netizen_cli.runtime.contracts import TurnOutcome
from netizen_cli.turn_activity import TurnPlanStepSnapshot, TurnPlanStepState
from tests.support.channel_fixtures import channel_fixture
from tests.support.channel_results import completed_turn_result, file_change_item, turn_activity_snapshot
from tests.support.presenter_traces import delivery_recorder

REFERENCE = Path(__file__).with_name("fixtures") / "short_turn_presenter_15548d5.json"


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
