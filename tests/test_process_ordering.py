"""Explore process-list observations and public Feishu write ordering."""

import asyncio
import itertools
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from netizen_cli.channel.process_presentation import ProcessList, ProcessLocation, ProcessPresentation
from netizen_cli.channel.reply_events import PresentationKey
from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.bindings import BindingTaskFeedback
from tests.support.channel_fixtures import channel_fixture
from tests.support.channel_messages import FakeMessage
from netizen_cli.process_activity import ProcessActivityWindow
from netizen_cli.process_messages import Ok, Rejected, Unknown, item_events
from netizen_cli.progress_card_settings import ProgressCardSettings
from netizen_cli.turn_activity import (
    TurnActivityEvent, TurnActivityKind as Kind, TurnActivityStatus as Status,
    TurnPlanStepSnapshot, TurnPlanStepState,
)
from tests.support.channel_results import turn_activity_snapshot
from tests.support.process_transport import ProcessTransport, check_pairs
from tests.test_process_presentation import process_port, process_snapshot


T = 1_800_000_000


class ProcessOrderingTest(unittest.IsolatedAsyncioTestCase):
    def message(self, port, *, channel=None, topic=False):
        key = PresentationKey("binding", "native-one", "turn-one")
        scope = FeishuScope("app", "chat", ScopeKind.TOPIC if topic else ScopeKind.DIRECT, "topic" if topic else None)
        location = ProcessLocation(scope, "input", "root" if topic else None)
        state = ProcessPresentation(key, port, channel, lambda: location, lambda: T+1, 1,
                                    claimed_cot_ids=set())
        async def close():
            state.revoke()
            if state.task:
                await asyncio.gather(state.task, return_exceptions=True)
        self.addAsyncCleanup(close)
        return state

    async def test_observation_and_terminal_cuts_with_suspended_write(self):
        for order in itertools.permutations(("observe", "release", "finish")):
            with self.subTest(order=order):
                events, active, peak = [], 0, 0
                entered, release = asyncio.Event(), asyncio.Event()
                wire = ProcessTransport(events)
                send = wire.arequest
                async def held(request):
                    nonlocal active, peak
                    active += 1
                    peak = max(peak, active)
                    try:
                        if request.http_method.name == "PUT" and not entered.is_set():
                            entered.set()
                            await release.wait()
                        return await send(request)
                    finally:
                        active -= 1
                wire.arequest = held
                state = self.message(wire.port)
                async with asyncio.timeout(2):
                    state.sample(process_snapshot(1))
                    await entered.wait()
                    for action in order:
                        if action == "observe":
                            state.sample(process_snapshot(2))
                        elif action == "release":
                            release.set()
                        else:
                            state.finish("done")
                        await asyncio.sleep(0)
                    await state.task
                self.assertEqual(peak, 1)
                names = [name for name, _, _ in events]
                self.assertEqual((names.count("process.create"), names.count("process.complete")), (1,1))
                self.assertEqual(names[-1], "process.complete")
                updates = [args[1] for name, args, _ in events if name == "process.update"]
                for update in updates:
                    check_pairs(update)
                starts, ends = check_pairs(updates[-1])
                self.assertEqual(starts.keys(), ends)
                self.assertEqual(len(starts), 2 if order.index("observe") < order.index("finish") else 1)

    async def test_unknown_create_is_spent_and_close_revokes_unconfirmed_placement(self):
        for cut in ("create", "placement"):
            with self.subTest(cut=cut):
                port = process_port()
                entered, release = asyncio.Event(), asyncio.Event()
                async def block(*args):
                    entered.set()
                    await release.wait()
                    return Unknown()
                channel = AsyncMock()
                if cut == "create":
                    port.create.side_effect = block
                else:
                    channel.fetch_message.side_effect = block
                state = self.message(port, channel=channel, topic=True)
                async with asyncio.timeout(1):
                    state.sample(process_snapshot(1))
                    await entered.wait()
                    state.finish("timeout")
                    release.set()
                    await asyncio.gather(state.task, return_exceptions=True)
                    state.sample(process_snapshot(2), terminal=True)
                self.assertEqual(port.create.await_count, 1)
                self.assertEqual(channel.fetch_message.await_count, int(cut == "placement"))
                port.update.assert_not_awaited()
                port.complete.assert_not_awaited()

    async def test_final_write_failure_still_completes_once(self):
        for response in (Rejected(10020), Rejected(10001), Unknown(), OSError()):
            with self.subTest(response=response):
                port = process_port()
                port.update.side_effect = [Ok(), response]
                state = self.message(port)
                state.sample(process_snapshot(1))
                await state.ready.wait()
                state.finish("error")
                async with asyncio.timeout(1):
                    await state.task
                self.assertEqual(port.update.await_count, 2)
                port.complete.assert_awaited_once_with("cot","process-mid","error")
                state.sample(process_snapshot(2))
                self.assertTrue(state.closed)

    async def test_only_uncertain_replacement_invalidates_previously_confirmed_list(self):
        for result in (Unknown(), Rejected(10020), Rejected(10001)):
            with self.subTest(result=result):
                port = process_port()
                state = self.message(port)
                delivered = asyncio.Queue()
                async def update(cot, mid, items):
                    delivered.put_nowait(tuple(item.text for item in items))
                    return result if items[0].text == "B" else Ok()
                port.update.side_effect = update
                def sample(rev, text):
                    snap = process_snapshot(1)
                    return replace(snap, revision=rev, process_items=(replace(snap.process_items[0], identity=text,
                                   text=text, finished_at=T),))
                async with asyncio.timeout(1):
                    state.sample(sample(1,"A"))
                    self.assertEqual(await delivered.get(), ("A",))
                    state.sample(sample(2,"B"))
                    self.assertEqual(await delivered.get(), ("B",))
                    state.sample(sample(2,"B"))
                    await asyncio.sleep(0)
                    self.assertTrue(delivered.empty())
                    state.sample(sample(3,"A"))
                    while state.sampled != state.generation:
                        await asyncio.sleep(0)
                    if result == Rejected(10001):
                        self.assertTrue(delivered.empty())
                    else:
                        self.assertFalse(delivered.empty(), "the uncertain B must invalidate the known A")
                        self.assertEqual(delivered.get_nowait(), ("A",))

    def test_eviction_uses_immutable_samples_and_preserves_both_ends_above_finished_quota(self):
        for order in itertools.permutations(("a", "b", "c")):
            for failure in (True, False):
                with self.subTest(order=order, failure=failure):
                    clock = [T]
                    window = ProcessActivityWindow(lambda:clock[0])
                    source = replace(turn_activity_snapshot(binding_id="b"), plan_available=False)
                    settings = ProgressCardSettings(operation_limit=1, operation_max_chars=5)
                    output = ProcessList(PresentationKey("binding", "native-one", "turn-one"))
                    for key in "ab":
                        window.observe(TurnActivityEvent(key, Kind.COMMAND, Status.IN_PROGRESS, 0, key*10), settings)
                    first = replace(source, process_items=window.rows)
                    output.observe(first,T)
                    output.offered(output.desired)
                    initial,_ = check_pairs(item_events(output.desired))
                    self.assertTrue(all(item.text.endswith("…") for item in output.desired))
                    if not failure:
                        output.confirmed(output.desired)
                    output.observe(replace(source, plan_available=True, process_items=None),T+1)
                    self.assertEqual(check_pairs(item_events(output.desired)), (initial,set()))
                    clock[0] -= 5
                    for key in order:
                        window.observe(TurnActivityEvent(key, Kind.COMMAND, Status.COMPLETED, 0, "changed"),settings)
                    output.observe(replace(source,process_items=window.rows), T+2)
                    offered = output.desired
                    starts, ends = check_pairs(item_events(offered))
                    self.assertTrue(initial.keys() <= ends)
                    self.assertTrue(all(starts[key] == initial[key] for key in initial))
                    output.offered(offered)
                    output.confirmed(offered)
                    self.assertFalse(output.running_offers)
                    self.assertTrue(all(row.finished_at is None for row in first.process_items))

    def test_mutations_break_independent_pair_and_retention_oracles(self):
        import netizen_cli.process_messages as adapter
        original = adapter._events_for
        payload = ProcessList(PresentationKey("binding", "native-one", "turn-one"))
        payload.observe(process_snapshot(1),T)
        payload.offered(payload.desired)
        payload.observe(replace(process_snapshot(1), process_items=()),T+1)
        with patch.object(adapter, "_events_for", side_effect=lambda item:original(item)[1:]):
            with self.assertRaises(AssertionError):
                check_pairs(item_events(payload.desired))
        with patch.object(ProcessList, "offered", return_value=None):
            broken = ProcessList(PresentationKey("binding", "native-one", "turn-one"))
            broken.observe(process_snapshot(1),T)
            required,_ = check_pairs(item_events(broken.desired))
            broken.offered(broken.desired)
            broken.observe(replace(process_snapshot(1),process_items=()),T+1)
            _,ended = check_pairs(item_events(broken.desired))
            with self.assertRaises(AssertionError):
                assert required.keys() <= ended

    def test_oldest_sixty_then_promotes_waiting_rows(self):
        window = ProcessActivityWindow(lambda:T)
        settings = ProgressCardSettings(show_plan=False)
        for n in range(63):
            window.observe(TurnActivityEvent(str(n),Kind.TOOL,Status.IN_PROGRESS,0),settings)
        snapshot = replace(process_snapshot(1),process_items=window.rows)
        output = ProcessList(PresentationKey("binding", "native-one", "turn-one"))
        output.observe(snapshot,T)
        expected = sorted(row.identity for row in window.rows)
        self.assertEqual([item.item_id for item in output.desired], expected[:60])
        output.offered(output.desired)
        remaining = [row for row in window.rows if row.identity not in expected[:3]]
        output.observe(replace(snapshot,process_items=tuple(remaining)),T+1)
        ends = output.desired
        self.assertEqual({item.item_id for item in ends}, set(expected[:60]))
        output.offered(ends)
        output.confirmed(ends)
        self.assertEqual({item.item_id for item in output.desired}, set(expected[3:]))

    def test_an_offered_start_keeps_a_slot_until_its_end_is_confirmed(self):
        for count in (1, 2, 59, 60):
            with self.subTest(count=count):
                window = ProcessActivityWindow(lambda: T)
                settings = ProgressCardSettings(operation_limit=1)
                output = ProcessList(PresentationKey("binding", "native-one", "turn-one"))
                starts, ends = set(), set()
                def observe(keys, status):
                    for key in keys:
                        window.observe(TurnActivityEvent(str(key), Kind.TOOL, status, 0), settings)
                def write(*, confirm=True):
                    output.observe(replace(process_snapshot(1), process_items=window.rows), T)
                    sent = output.desired
                    offered, finished = check_pairs(item_events(sent))
                    starts.update(offered)
                    if confirm:
                        ends.update(finished)
                    output.offered(sent)
                    if confirm:
                        output.confirmed(sent)
                    self.assertLessEqual(len(output.running_offers), 60)
                    return finished
                observe(range(count), Status.IN_PROGRESS)
                write()
                first_starts = starts.copy()
                retire = min(2, count)
                observe(range(retire), Status.COMPLETED)
                observe(range(count, count + retire), Status.IN_PROGRESS)
                unconfirmed = write(confirm=False)
                self.assertEqual(len(unconfirmed & first_starts), retire)
                self.assertEqual(unconfirmed, write())
                write()
                observe(range(retire, count + retire), Status.COMPLETED)
                write()
                self.assertEqual(starts, ends)
                self.assertFalse(output.running_offers)

    def test_reappearing_steps_retain_original_start_and_stay_finished(self):
        clock = [T]
        window = ProcessActivityWindow(lambda: clock[0])
        output = ProcessList(PresentationKey("binding", "native-one", "turn-one"))
        history = []
        for plan in ("A", "B", "BC", "AC"):
            window.plan(tuple(TurnPlanStepSnapshot(text, TurnPlanStepState.IN_PROGRESS) for text in plan))
            output.observe(replace(process_snapshot(1), plan_generated=True, process_items=window.rows), clock[0])
            sent = output.desired
            history.append({item.text: (item.started_at, item.finished_at) for item in sent})
            output.offered(sent)
            output.confirmed(sent)
            clock[0] += 1
        self.assertEqual(history[0]["A"], (T, None))
        self.assertEqual(history[1]["A"], (T, T+1))
        self.assertNotIn("A", history[2])
        self.assertEqual(history[3]["A"], history[1]["A"])

    def test_same_window_and_new_physical_turn_prove_distinct_ends(self):
        key = PresentationKey("binding", "native-one", "goal")
        model = ProcessList(key)
        window = ProcessActivityWindow(lambda: T)
        window.observe(TurnActivityEvent("read", Kind.COMMAND, Status.IN_PROGRESS, 0), ProgressCardSettings())
        window.plan((TurnPlanStepSnapshot("Inspect", TurnPlanStepState.IN_PROGRESS),))
        first = replace(process_snapshot(1), physical_turn_id="A", process_items=window.rows, plan_generated=True)
        def write(snap, now):
            model.observe(snap, now)
            payload = model.desired
            model.offered(payload)
            model.confirmed(payload)
            return check_pairs(item_events(payload))
        starts, ends = write(first, T)
        self.assertEqual(len(starts), 2)
        self.assertFalse(ends)
        for candidate in (first, replace(first, process_items=None), replace(first, physical_turn_id=None, process_items=())):
            self.assertEqual(write(replace(candidate, plan_available=False), T+1), (starts, set()))
        second = replace(first, physical_turn_id="B", process_items=None, plan_generated=False)
        after, ends = write(second, T+2)
        self.assertEqual(after, starts)
        self.assertEqual(ends, {k for k in starts if k[0] == "tool"})
        after, ends = write(replace(second, process_items=(), plan_generated=True), T+3)
        self.assertEqual(after, {("step", "Inspect"): T})
        self.assertEqual(ends, set(after))

    def test_goal_checklist_across_three_rollovers_and_optional_polls(self):
        for cuts in itertools.product(("skip", "empty", "operation"), repeat=3):
            with self.subTest(cuts=cuts):
                now = T
                output = ProcessList(PresentationKey("binding", "native-one", "goal"))
                def sample(window, *, plan=False, available=True, finished=False):
                    snap = replace(process_snapshot(1), physical_turn_id=physical, plan_generated=plan,
                                   process_items=window.rows if available else None)
                    output.observe(snap, now)
                    payload = output.desired
                    starts, ends = check_pairs(item_events(payload))
                    step = ("step", "Inspect")
                    self.assertEqual(starts[step], T)
                    self.assertEqual(step in ends, finished)
                    output.offered(payload)
                    output.confirmed(payload)
                for i, cut in enumerate(("initial", *cuts)):
                    physical, now = f"physical-{i}", T + i
                    window = ProcessActivityWindow(lambda: now)
                    if cut == "empty":
                        sample(window, available=False)
                    elif cut == "operation":
                        window.observe(TurnActivityEvent("op", Kind.TOOL, Status.IN_PROGRESS, 0), ProgressCardSettings())
                        sample(window)
                    window.plan((TurnPlanStepSnapshot("Inspect", TurnPlanStepState.IN_PROGRESS),))
                    sample(window, plan=True)
                now += 1
                window.plan(())
                sample(window, plan=True, finished=True)
                window.plan((TurnPlanStepSnapshot("Inspect", TurnPlanStepState.IN_PROGRESS),))
                sample(window, plan=True, finished=True)

    def test_visibility_and_step_text_identity(self):
        window = ProcessActivityWindow(lambda:T)
        settings = ProgressCardSettings(show_operations=False, commentary_max_chars=4)
        window.observe(TurnActivityEvent("op",Kind.TOOL,Status.IN_PROGRESS,0),settings)
        window.observe(TurnActivityEvent("text",Kind.COMMENTARY,Status.COMPLETED,0,"hello"),settings)
        window.plan(tuple(TurnPlanStepSnapshot(name, status) for name,status in (
            ("same",TurnPlanStepState.PENDING),("same",TurnPlanStepState.IN_PROGRESS),
            ("done",TurnPlanStepState.COMPLETED))))
        output = ProcessList(PresentationKey("binding", "native-one", "turn-one"))
        output.observe(replace(process_snapshot(1),plan_generated=True,process_items=window.rows),T)
        self.assertEqual({item.text for item in output.desired},{"hel…","done"})
        starts,ends = check_pairs(item_events(output.desired))
        self.assertEqual(starts.keys(),ends)

    async def test_lost_create_is_never_replayed_at_result(self):
        port = process_port()
        port.create.return_value = Unknown()
        state = self.message(port)
        state.sample(process_snapshot(1))
        async with asyncio.timeout(1):
            await state.task
        state.sample(process_snapshot(2), terminal=True)
        state.finish("done")
        port.create.assert_awaited_once()
        port.complete.assert_not_awaited()


    async def test_queued_running_sample_cannot_first_dispatch_after_result(self):
        port = process_port()
        state = self.message(port)
        state.sample(process_snapshot(1))
        state.sample(process_snapshot(2), terminal=True)
        state.finish("done")
        await state.task
        port.create.assert_not_awaited()


    async def test_only_process_ids_are_exclusive_across_codex_turns_and_lifecycle(self):
        from netizen_cli.process_messages import Created
        from netizen_cli.runtime.contracts import (
            Submission, SubmitDisposition, ThreadActivityDiscardedOutcome, TurnOutcome,
        )
        from tests.support.channel_results import completed_turn_result

        first = Created("cot", "process-mid")
        receipts = (first, Created("cot", "other-mid"), Created("other", "process-mid"))
        for second, owner, phase in itertools.product(receipts, ("A", "B"), ("open", "ended", "discarded")):
            with self.subTest(second=second, owner=owner, phase=phase):
                async with channel_fixture() as fx, asyncio.timeout(4):
                    presenter = fx.app._progress_cards
                    port = process_port()
                    presenter._process_port = port
                    snapshots = {}

                    def observe(binding, *, thread_id, turn_id):
                        return snapshots.get((binding, thread_id, turn_id))

                    async def open_turn(binding, thread, turn, receipt):
                        key = PresentationKey(binding, thread, turn)
                        snapshot = replace(process_snapshot(1, binding=binding),
                                           thread_id=thread, turn_id=turn, physical_turn_id=turn)
                        snapshots[key.index] = snapshot
                        origin = FakeMessage("work", message_id=turn)
                        submission = Submission(SubmitDisposition.STARTED, binding, thread, turn, lambda: None)
                        created = asyncio.get_running_loop().create_future()

                        async def create(*_):
                            created.set_result(presenter._process[key])
                            return receipt

                        port.create.side_effect = create
                        await fx.app._present_ordinary_receipt(submission, origin)
                        state = await created
                        await state.ready.wait()
                        outcome = TurnOutcome(binding_id=binding, thread_id=thread, turn_id=turn,
                                              owner_id="owner", origin=origin, cwd=fx.project, activity=snapshot,
                                              result=completed_turn_result(final_response=turn))
                        return outcome, state

                    async def finish(outcome, state):
                        await fx.app.handle_completion(outcome)
                        await state.task

                    with patch.object(fx.app._ordinary, "turn_activity", side_effect=observe):
                        first_turn = await open_turn("A", "native-A", "first", first)
                        if phase != "open":
                            await finish(*first_turn)
                        if phase == "discarded":
                            await fx.app.handle_codex_completion(ThreadActivityDiscardedOutcome("A", "native-A", "first"))
                        thread = f"native-{owner}-new" if phase == "discarded" else f"native-{owner}"
                        next_turn = await open_turn(owner, thread, "next", second)
                        if phase == "open":
                            await finish(*first_turn)
                        await finish(*next_turn)

                    expected = [(first.cot_id, first.message_id)]
                    if second.cot_id != first.cot_id:
                        expected.append((second.cot_id, second.message_id))
                    self.assertEqual(port.create.await_count, 2)
                    self.assertEqual([call.args[:2] for call in port.complete.await_args_list], expected)
                    self.assertCountEqual([call.args[:2] for call in port.update.await_args_list], expected * 2)

    async def test_malformed_sdk_id_does_not_interrupt_ordinary_codex_delivery(self):
        from types import SimpleNamespace
        from lark_channel.channel.outbound.sender import OutboundSender
        from netizen_cli.runtime.contracts import TurnOutcome
        from tests.support.channel_results import completed_turn_result

        async with channel_fixture() as fx:
            receipt = OutboundSender._to_result(SimpleNamespace(_on_success=None),
                                                {"code": 0, "data": {"message_id": {"malformed": 1}}})
            fx.channel.reply_results.append(receipt)
            await fx.app.handle_completion(TurnOutcome(binding_id="binding", thread_id="s", turn_id="p",
                owner_id="owner", origin=FakeMessage("start", message_id="input"), cwd=fx.project,
                result=completed_turn_result(final_response="actual body"), task_feedback=BindingTaskFeedback()))
            self.assertEqual([mid for mid, content in fx.channel.replies if "actual body" in str(content)], ["input"])


class ProcessMutationTest(unittest.TestCase):
    def test_transport_and_lifecycle_oracles_reject_faulty_implementations(self):
        from netizen_cli.channel import process_presentation
        from tests.test_process_presentation import OrdinaryProcessTest

        from tests.test_goal_reply_separation import GoalReplySeparationTest

        start_goal = GoalReplySeparationTest.start
        async def create_in_card_mode(case, **kwargs):
            await case.fx.app._progress_cards._process_port.create("oc_direct", "om_goal", False)
            return await start_goal(case, **kwargs)

        create = ProcessPresentation._create
        write = ProcessPresentation._write
        async def replay_create(state):
            return await create(state) or await create(state)
        async def keep_unknown(state, payload):
            result = await write(state,payload)
            return "rejected" if result == "unknown" else result
        for owner, name, change, case in (
            (GoalReplySeparationTest, "start", create_in_card_mode,
             GoalReplySeparationTest("test_rollover_partials_share_one_card_through_final_files_page")),
            (ProcessPresentation,"_create",replay_create,
             ProcessOrderingTest("test_lost_create_is_never_replayed_at_result")),
            (ProcessPresentation,"_write",keep_unknown,
             ProcessOrderingTest("test_only_uncertain_replacement_invalidates_previously_confirmed_list")),
            (process_presentation,"_verify",AsyncMock(return_value=True),
             OrdinaryProcessTest("test_topic_requires_one_exact_placement_read_before_writes")),
        ):
            with self.subTest(case=case.id()), patch.object(owner,name,change):
                outcome = unittest.TestResult()
                outcome.buffer = True
                case.run(outcome)
                self.assertEqual(outcome.testsRun,1)
                self.assertFalse(outcome.errors, outcome.errors)
                self.assertTrue(outcome.failures, "mutation survived its oracle")
