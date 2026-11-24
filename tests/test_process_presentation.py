"""Decoded outbound operations at a short Turn's terminal handoff."""
import asyncio
import itertools
import json
import unittest
from contextlib import ExitStack
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from netizen_cli.bindings import BindingTaskFeedback
from netizen_cli.channel.codex_reply_events import CodexReplyEvents
from netizen_cli.channel.process_presentation import ProcessPresentation
from netizen_cli.channel.reply_events import PresentationKey
from netizen_cli.channel.topics import BindingCardLocation, CardReplyLocation
from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.ordinary_sessions import LogicalReply, PhysicalAnswer, PhysicalStarted
from netizen_cli.process_messages import Created, Ok, Unknown
from netizen_cli.partial_answers import PartialAnswer
from netizen_cli.process_activity import ObservedProcessItem, ProcessActivityWindow
from netizen_cli.progress_card_settings import ProgressCardSettings
from netizen_cli.runtime.codex_contracts import GoalOutcome
from netizen_cli.runtime.contracts import GoalSubmission, SideTurnOutcome, TurnOutcome
from netizen_cli.turn_activity import TurnActivityEvent, TurnActivityKind, TurnActivityStatus, TurnPlanStepSnapshot, TurnPlanStepState
from tests.support.channel_fixtures import channel_fixture
from tests.support.channel_messages import FakeMessage
from tests.support.channel_results import completed_turn_result, file_change_item, goal_activity_snapshot, side_turn_activity_snapshot, turn_activity_snapshot
from tests.support.presenter_traces import delivery_recorder
from tests.support.process_transport import ProcessTransport

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


async def short_turn_trace(*, card, files, install=None, wire=None):
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
        window = ProcessActivityWindow(lambda: 1_800_000_000)
        window.plan(activity.steps)
        activity = replace(activity, process_items=window.rows if not card else ())
        partial = PartialAnswer(activity.thread_id, activity.turn_id, "partial-1", "阶段正文")
        items = (file_change_item("output.txt"),) if files else ()
        with ExitStack() as stack:
            numbers = itertools.count(1)
            stack.enter_context(patch("secrets.token_hex", side_effect=lambda n: next(numbers).to_bytes(n, "big").hex()))
            events = delivery_recorder(fx.channel, stack, include_reads=True)
            if wire is not None:
                wire.events = events
                fx.app._progress_cards._process_port = wire.port
            await fx.app.handle_completion(TurnOutcome(binding_id=binding.id, thread_id=activity.thread_id,
                turn_id=activity.turn_id, owner_id="ou_user", origin=origin, activity=activity,
                cwd=fx.project, result=completed_turn_result(*items, final_response="最终正文"),
                task_feedback=BindingTaskFeedback(progress_card_enabled=card), partial_answers=(partial,)))
        return json.loads(json.dumps(wire.content_trace() if wire and not card else events, ensure_ascii=False).replace(str(fx.project_root.resolve()), "<root>"))


class TerminalProcessDifferential(unittest.IsolatedAsyncioTestCase):
    async def test_short_turn_content(self):
        expected = json.loads(REFERENCE.read_text())
        for card in (True, False):
            for files in (True, False):
                with self.subTest(card=card, files=files):
                    wire = ProcessTransport([])
                    self.assertEqual(await short_turn_trace(card=card, files=files, wire=wire), expected[f"{card}/{files}"])
                    names = [op for op, _, _ in wire.events]
                    self.assertFalse(any(name.startswith("process.") for name in names))


def process_snapshot(revision, *, binding="binding"):
    snapshot = turn_activity_snapshot(binding_id=binding, revision=revision)
    rows = tuple(ObservedProcessItem(str(n), "operation", str(n), 1_800_000_000)
                 for n in range(1, revision + 1))
    return replace(snapshot, process_items=rows)


def process_port():
    return SimpleNamespace(create=AsyncMock(return_value=Created("cot", "process-mid")),
                           update=AsyncMock(return_value=Ok()), complete=AsyncMock(return_value=Ok()))


class OrdinaryProcessTest(unittest.IsolatedAsyncioTestCase):
    async def test_each_changed_version_attempted_once_when_confirmed_write_rereads(self):
        port = process_port()
        first, second, third, release = (asyncio.Event() for _ in range(4))
        calls = []
        async def update(cot, mid, payload):
            calls.append(len(payload))
            if len(calls) == 1:
                first.set()
                await release.wait()
                return Ok()
            if len(calls) == 2:
                second.set()
                return Unknown()
            third.set()
            return Ok()
        port.update.side_effect = update
        key = PresentationKey("binding", "native-one", "turn-one")
        scope = FeishuScope("app", "chat", ScopeKind.DIRECT)
        message = ProcessPresentation(key, key, port, None,
            lambda: CardReplyLocation(scope, "input", None, BindingCardLocation({})),
            lambda *_: True, lambda: 1_800_000_001, 1)
        try:
            async with asyncio.timeout(2):
                message.sample(process_snapshot(1))
                await first.wait()
                message.sample(process_snapshot(2))
                release.set()
                await second.wait()
                message.sample(process_snapshot(2))
                await asyncio.sleep(0)
                self.assertEqual(calls, [1, 2])
                message.sample(process_snapshot(3))
                await third.wait()
                self.assertEqual(calls, [1, 2, 3])
        finally:
            message.revoke()
            if message.task:
                await asyncio.gather(message.task, return_exceptions=True)

    async def test_terminal_only_codex_activity_keeps_the_short_turn_baseline(self):
        port = process_port()
        actual = await short_turn_trace(card=False, files=False,
            install=lambda fx: setattr(fx.app._progress_cards, "_process_port", port))
        self.assertEqual(actual, json.loads(REFERENCE.read_text())["False/False"])
        port.create.assert_not_awaited()

    async def test_claude_safe_items_flow_through_start_and_result_without_becoming_receipts(self):
        async with channel_fixture() as fx:
            origin = await fx.new()
            port = process_port()
            reply = LogicalReply()
            fx.app._progress_cards._process_port = port
            snapshot = process_snapshot(1)
            key = PresentationKey(snapshot.binding_id, snapshot.thread_id, snapshot.turn_id)
            with patch.object(fx.app._ordinary, "turn_activity", return_value=snapshot):
                await fx.app.handle_ordinary_completion(PhysicalStarted(*key.index, origin,
                    BindingTaskFeedback(), lambda: True, lambda: True, reply))
            state = fx.app._progress_cards._process[key]
            async with asyncio.timeout(1):
                await state.ready.wait()
            await fx.app.handle_ordinary_completion(PhysicalAnswer(*key.index, "user", origin, "completed",
                "final", logical_id=key.turn_id, activity=snapshot, reply=reply))
            await state.task
            port.create.assert_awaited_once_with("oc_direct", "om_new", False)
            port.complete.assert_awaited_once_with("cot", "process-mid", "done")
            self.assertNotIn("process-mid", reply.messages)

    async def test_topic_requires_one_exact_placement_read_before_writes(self):
        key = PresentationKey("binding", "native-one", "turn-one")
        scope = FeishuScope("app", "chat", ScopeKind.TOPIC, "topic")
        exact = dict(message_id="process-mid", chat_id="chat", thread_id="topic", root_id="root", parent_id="input", deleted=False)
        for variant in ("exact", "missing", "wrong", "deleted"):
            with self.subTest(variant=variant):
                row = dict(exact)
                if variant == "missing":
                    del row["root_id"]
                elif variant == "wrong":
                    row["parent_id"] = "another-parent"
                elif variant == "deleted":
                    row["deleted"] = True
                channel = SimpleNamespace(fetch_message=AsyncMock(return_value={"code":0,"data":{"items":[row]}}))
                binding = BindingCardLocation({})
                port = process_port()
                message = ProcessPresentation(key, key, port, channel,
                    lambda: CardReplyLocation(scope, "input", "root", binding), lambda *_: True, lambda: 1_800_000_001, 1)
                message.sample(process_snapshot(1))
                await message.ready.wait()
                message.finish("done")
                async with asyncio.timeout(1):
                    await message.task
                port.create.assert_awaited_once_with("chat", "input", True)
                channel.fetch_message.assert_awaited_once_with("process-mid")
                self.assertEqual(port.update.await_count, 2 if variant == "exact" else 0)
                self.assertEqual(port.complete.await_count, int(variant == "exact"))
                self.assertEqual("process-mid" in binding.unusable, variant == "wrong")


class GoalSideProcessTest(unittest.IsolatedAsyncioTestCase):
    async def test_rollover_reuses_one_process_and_finishes_the_previous_physical_item(self):
        async with channel_fixture() as fx:
            origin = await fx.new()
            binding = fx.store.active_binding(fx.app._scope(origin).key)
            port = process_port()
            fx.app._progress_cards._process_port = port
            fx.app._progress_cards._poll_seconds = 0.001
            written = asyncio.Queue()
            async def update(cot, mid, payload):
                written.put_nowait(payload)
                return Ok()
            port.update.side_effect = update
            def sample(n):
                window = ProcessActivityWindow(lambda: 1_800_000_000 + n)
                window.observe(TurnActivityEvent("same-native-id", TurnActivityKind.TOOL, TurnActivityStatus.IN_PROGRESS, 0),
                               ProgressCardSettings())
                return replace(goal_activity_snapshot(binding_id=binding.id, revision=n),
                               physical_turn_id=f"physical-{n}", process_items=window.rows)
            first, last = sample(1), sample(2)
            fx.runtime.goal_activity_values[binding.id] = first
            submission = GoalSubmission(binding.id, "native-one", "goal-one", lambda: None)
            async with asyncio.timeout(2):
                await fx.app._present_running_goal(binding=binding, origin=origin, submission=submission)
                initial, = await written.get()
                await fx.app._present_running_goal(binding=binding, origin=origin, submission=replace(submission, continued=True))
                fx.runtime.goal_activity_values[binding.id] = last
                rollover = await written.get()
                self.assertEqual([(r.item_id, r.finished_at is None) for r in rollover],
                                 [(initial.item_id, False), (last.process_items[0].identity, True)])
                state, = fx.app._progress_cards._process.values()
                await fx.app.handle_completion(GoalOutcome(binding.id, "native-one", "goal-one", "user", origin,
                    final_physical_turn_id="physical-2", final_turn_status="completed", final_response="done", activity=last))
            await state.task
            port.create.assert_awaited_once()
            port.complete.assert_awaited_once_with("cot", "process-mid", "done")
            self.assertFalse(fx.app._progress_cards._process)

    async def test_side_verifies_root_and_disables_contradiction_at_parent_binding(self):
        async with channel_fixture() as fx:
            port = process_port()
            presenter = fx.app._progress_cards
            presenter._process_port = port
            origin = FakeMessage("round", message_id="input", chat_id="chat", thread_id="topic")
            activity = replace(side_turn_activity_snapshot(side_id="side"),
                               process_items=process_snapshot(1).process_items)
            with patch.object(fx.channel, "fetch_message", AsyncMock(return_value={"code":0,"data":{"items":[{
                "message_id":"process-mid","chat_id":"chat","thread_id":"topic","root_id":"wrong","parent_id":"input","deleted":False
            }]}})) as read:
                key = PresentationKey("side", activity.thread_id, activity.turn_id, True)
                event = CodexReplyEvents(fx.runtime, fx.app._ordinary).opened(
                    key, origin, BindingTaskFeedback(), reply=AsyncMock(), reply_partial=AsyncMock(),
                    parent_binding_id="parent", root_message_id="root")
                fx.runtime.side_turn_activity_values["side"] = activity
                await presenter.open(event)
                state = presenter._process[key]
                await state.task
                await fx.app.handle_completion(SideTurnOutcome("side", "parent", activity.thread_id, activity.turn_id,
                    "owner", origin, fx.project, result=completed_turn_result(final_response="side result"),
                    activity=activity, root_message_id="root"))
            port.create.assert_awaited_once_with("chat", "input", True)
            read.assert_awaited_once_with("process-mid")
            self.assertIn("process-mid", presenter.binding_location("parent").unusable)
            self.assertNotIn("side", presenter._binding_locations)
            port.update.assert_not_awaited()
            port.complete.assert_not_awaited()
            self.assertIn("side result", str(fx.channel.replies))
