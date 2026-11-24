"""Explore process-list observations and public Feishu write ordering."""

import asyncio
import itertools
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from netizen_cli.channel.process_presentation import ProcessList, ProcessLocation, ProcessPresentation
from netizen_cli.channel.reply_events import PresentationKey, ReplyTarget
from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.bindings import BindingTaskFeedback
from netizen_cli.ordinary_sessions import ClaudeInputAnchor, ConversationTarget, DegradedTurn, LogicalReply, LogicalTerminal, PhysicalAnswer, PhysicalStarted
from netizen_cli.runtime.contracts import TerminalStateUnknown
from tests.support.channel_fixtures import channel_fixture
from tests.support.channel_messages import FakeMessage
from netizen_cli.process_activity import ProcessActivityWindow
from netizen_cli.process_messages import Created, Ok, Rejected, Unknown, item_events
from netizen_cli.progress_card_settings import ProgressCardSettings
from netizen_cli.turn_activity import (
    TurnActivityEvent, TurnActivityKind as Kind, TurnActivityStatus as Status,
    TurnPlanStepSnapshot, TurnPlanStepState,
)
from tests.support.channel_results import sent_result, turn_activity_snapshot
from tests.support.process_transport import ProcessTransport, check_pairs
from tests.test_process_presentation import process_port, process_snapshot


T = 1_800_000_000


class ProcessOrderingTest(unittest.IsolatedAsyncioTestCase):
    def message(self, port, *, channel=None, topic=False):
        key = PresentationKey("binding", "native-one", "turn-one")
        scope = FeishuScope("app", "chat", ScopeKind.TOPIC if topic else ScopeKind.DIRECT, "topic" if topic else None)
        location = ProcessLocation(scope, "input", "root" if topic else None)
        state = ProcessPresentation(key, key, port, channel, lambda: location, lambda _: None, lambda: T+1, 1,
                                    claimed_cot_ids=set(), binding_id=key.owner_id)
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

    async def test_supersession_closes_only_the_previous_process_for_the_same_input(self):
        async with channel_fixture() as fx:
            wire = ProcessTransport([])
            presenter = fx.app._progress_cards
            presenter._process_port = wire.port
            replies = {"A":LogicalReply(), "B":LogicalReply()}
            async def answer(owner, turn):
                snap = replace(process_snapshot(1),turn_id=turn)
                key = PresentationKey("binding", "native-one", turn)
                origin = FakeMessage("input", message_id=owner)
                state = presenter._process_for(ReplyTarget(key, origin, replace(key, turn_id=owner)), "binding")
                state.sample(snap)
                await state.ready.wait()
                await fx.app.handle_ordinary_completion(PhysicalAnswer(*key.index, "user", origin, "completed",
                    "answer", activity=snap, logical_id=owner, run_continues=True, reply=replies[owner]))
                return state
            async with asyncio.timeout(2):
                first = await answer("A","A1")
                other = await answer("B","B1")
                self.assertFalse(wire.completed)
                latest = await answer("A","A2")
                await first.task
                self.assertEqual(wire.completed, {"synthetic-process-1"})
                presenter.end_process_run(PresentationKey("binding","native-one","B"),"timeout")
                await other.task
                self.assertEqual(wire.completed, {"synthetic-process-1","synthetic-process-2"})
                presenter.end_process_run(PresentationKey("binding","native-one","A"),"done")
                await latest.task
            completions = [args for name,args,_ in wire.events if name == "process.complete"]
            self.assertEqual(completions, [["synthetic-process-1","done"],["synthetic-process-2","done"],
                                          ["synthetic-process-3","done"]])
            self.assertFalse(presenter._process)
            for reply in replies.values():
                self.assertTrue(wire.completed.isdisjoint(reply.messages))

    async def test_bodyless_claude_interruption_supplies_the_typed_verdict(self):
        async with channel_fixture() as fx, asyncio.timeout(2):
            port = process_port()
            presenter = fx.app._progress_cards
            presenter._process_port = port
            snap = process_snapshot(1)
            key = PresentationKey(snap.binding_id, snap.thread_id, snap.turn_id)
            origin = FakeMessage("prompt", message_id="in")
            reply = LogicalReply()
            with patch.object(fx.app._ordinary, "turn_activity", return_value=snap):
                await fx.app.handle_ordinary_completion(PhysicalStarted(*key.index, origin,
                    BindingTaskFeedback(), lambda: True, lambda: True, reply))
            state = presenter._process[key]
            await state.ready.wait()
            await fx.app.handle_ordinary_completion(LogicalTerminal(*key.index, "user", origin, "interrupted",
                reply, None, lambda: True))
            await state.task
            port.complete.assert_awaited_once_with("cot", "process-mid", "done")

    async def test_bodyless_logical_terminal_prevents_a_queued_first_create(self):
        for publishable in (True, False):
            async with channel_fixture() as fx, asyncio.timeout(2):
                port = process_port()
                presenter = fx.app._progress_cards
                presenter._process_port = port
                snap = process_snapshot(1)
                key = PresentationKey(snap.binding_id, snap.thread_id, snap.turn_id)
                origin = FakeMessage("prompt", message_id="in")
                reply = LogicalReply()
                with patch.object(fx.app._ordinary, "turn_activity", return_value=snap) as observe:
                    await fx.app.handle_ordinary_completion(PhysicalStarted(*key.index, origin,
                        BindingTaskFeedback(), lambda: True, lambda: True, reply))
                    state = presenter._process[key]
                    self.assertIsNotNone(state.task)
                    port.create.assert_not_awaited()
                    observe.return_value = None
                    await fx.app.handle_ordinary_completion(LogicalTerminal(*key.index, "user", origin, "interrupted",
                        reply, None, lambda: publishable))
                    await state.task
                port.create.assert_not_awaited()
                port.complete.assert_not_awaited()
                self.assertNotIn(key, presenter._process)

    async def test_bodyless_interruptions_release_each_closed_turn_key(self):
        async with channel_fixture() as fx, asyncio.timeout(2):
            presenter = fx.app._progress_cards
            presenter._process_port = port = process_port()
            port.create.return_value = Unknown()
            origin = FakeMessage("prompt", message_id="in")
            for n in range(4):
                snap = replace(process_snapshot(1), turn_id=f"turn-{n}")
                if n < 3:
                    snap = replace(snap, process_items=())
                key = PresentationKey(snap.binding_id, snap.thread_id, snap.turn_id)
                reply = LogicalReply()
                with patch.object(fx.app._ordinary, "turn_activity", return_value=snap):
                    await fx.app.handle_ordinary_completion(PhysicalStarted(*key.index, origin, BindingTaskFeedback(),
                        lambda: True, lambda: True, reply))
                    state = presenter._process[key]
                    if state.task:
                        await state.task
                        self.assertIn(key, presenter._process_tombstones)
                    await fx.app.handle_ordinary_completion(LogicalTerminal(*key.index, "user", origin, "interrupted",
                        reply, None, lambda: True))
                    self.assertFalse(presenter._process)
                    self.assertFalse(presenter._process_tombstones)
                    self.assertEqual(port.create.await_count, int(n == 3))
            port.update.assert_not_awaited()
            port.complete.assert_not_awaited()

    async def test_terminal_only_claude_sample_cannot_spend_the_answer_deadline(self):
        async with channel_fixture() as fx:
            port = process_port()
            fx.app._progress_cards._process_port = port
            origin = FakeMessage("prompt", message_id="in")
            event = PhysicalAnswer("binding", "native-one", "turn-one", "user", origin, "completed", "answer",
                logical_id="turn-one", activity=process_snapshot(1),
                delivery_deadline=asyncio.get_running_loop().time() + .08)
            await fx.app.handle_ordinary_completion(event)
            self.assertIn("answer", str(fx.channel.replies))
            port.create.assert_not_awaited()

    async def test_queued_running_sample_cannot_first_dispatch_after_result(self):
        port = process_port()
        state = self.message(port)
        state.sample(process_snapshot(1))
        state.sample(process_snapshot(2), terminal=True)
        state.finish("done")
        await state.task
        port.create.assert_not_awaited()

    async def test_late_running_receipt_is_owned_while_content_or_unknown_notice_proceeds(self):
        for cut, unknown in itertools.product(("create", "placement", "complete"), (False, True)):
            with self.subTest(cut=cut, unknown=unknown):
                async with channel_fixture() as fx, asyncio.timeout(2):
                    port = process_port()
                    presenter = fx.app._progress_cards
                    presenter._process_port = port
                    snap = process_snapshot(1)
                    key = PresentationKey(snap.binding_id, snap.thread_id, snap.turn_id)
                    origin = FakeMessage("prompt", message_id="in", thread_id="topic", raw={"root_id":"root"})
                    entered, release = asyncio.Event(), asyncio.Event()
                    async def hold(result):
                        entered.set()
                        await release.wait()
                        return result
                    async def create(*args):
                        return await hold(Created("cot","process-mid"))
                    async def complete(*args):
                        return await hold(Ok())
                    async def read(mid):
                        result = {"code":0,"data":{"items":[{"message_id":mid,"chat_id":origin.conversation.chat_id,
                            "thread_id":"topic","root_id":"root","parent_id":"in","deleted":False}]}}
                        return await hold(result) if cut == "placement" else result
                    if cut == "create":
                        port.create.side_effect = create
                    elif cut == "complete":
                        port.complete.side_effect = complete
                    reply = LogicalReply()
                    with patch.object(fx.app._ordinary, "turn_activity", return_value=snap), patch.object(
                        fx.channel, "fetch_message", side_effect=read) as fetched:
                        await fx.app.handle_ordinary_completion(PhysicalStarted(*key.index, origin,
                            BindingTaskFeedback(), lambda:True, lambda:True, reply))
                        state = presenter._process[key]
                        if cut == "complete":
                            await state.ready.wait()
                        else:
                            await entered.wait()
                        if unknown:
                            event = DegradedTurn(*key.index, "user", origin, TerminalStateUnknown("uncertain"))
                            expected = "uncertain"
                        else:
                            event = PhysicalAnswer(*key.index, "user", origin, "completed", "ordinary answer",
                                logical_id=key.turn_id, activity=snap, reply=reply)
                            expected = "ordinary answer"
                        await fx.app.handle_ordinary_completion(event)
                        self.assertIn(expected, str(fx.channel.replies))
                        await entered.wait()
                        release.set()
                        await state.task
                        fetched.assert_awaited_once_with("process-mid")
                    port.create.assert_awaited_once()
                    self.assertTrue(port.update.await_count >= 1)
                    port.complete.assert_awaited_once_with("cot","process-mid","timeout" if unknown else "done")
                    self.assertNotIn(key, presenter._process)

    async def test_only_cot_ids_are_exclusive_across_bindings_and_retirement(self):
        receipts = (Created("cot", "process-mid"), Created("other", "process-mid"), Created("cot", "other-mid"))
        for receipt, binding, retire in itertools.product(receipts, ("A", "B"), (False, True)):
            with self.subTest(receipt=receipt, binding=binding, retire=retire):
                async with channel_fixture() as fx, asyncio.timeout(2):
                    presenter = fx.app._progress_cards
                    port = process_port()
                    presenter._process_port = port
                    port.create.side_effect = [Created("cot", "process-mid"), receipt]
                    for owner, turn in (("A", "first"), (binding, "next")):
                        snap = replace(process_snapshot(1, binding=owner), turn_id=turn)
                        key = PresentationKey(owner, snap.thread_id, turn)
                        origin = FakeMessage("work", message_id=turn)
                        with patch.object(fx.app._ordinary, "turn_activity", return_value=snap):
                            await fx.app.handle_ordinary_completion(PhysicalStarted(*key.index, origin,
                                BindingTaskFeedback(), lambda: True, lambda: True, LogicalReply()))
                        state = presenter._process[key]
                        await state.ready.wait()
                        await fx.app.handle_ordinary_completion(PhysicalAnswer(*key.index, "owner", origin,
                            "completed", "body", logical_id=turn, activity=snap))
                        await state.task
                        if retire and turn == "first":
                            await presenter.retire_binding(owner)
                    expected = [("cot", "process-mid")]
                    if receipt.cot_id != "cot":
                        expected.append((receipt.cot_id, receipt.message_id))
                    self.assertEqual(port.create.await_count, 2)
                    self.assertEqual([c.args[:2] for c in port.complete.await_args_list], expected)
                    self.assertEqual([c.args[:2] for c in port.update.await_args_list],
                                     [pair for pair in expected for _ in range(2)])

    async def test_process_ids_do_not_change_ordinary_ledger_or_explicit_mention(self):
        async def run(order):
            async with channel_fixture() as fx, asyncio.timeout(2):
                presenter = fx.app._progress_cards
                port = process_port()
                presenter._process_port = port if order != "off" else None
                entered, release = asyncio.Event(), asyncio.Event()
                async def create(*_):
                    entered.set()
                    await release.wait()
                    return Created("cot", "process-mid")
                port.create.side_effect = create
                snap = process_snapshot(1)
                key = PresentationKey(snap.binding_id, snap.thread_id, snap.turn_id)
                origin = FakeMessage("start", message_id="input")
                reply = LogicalReply()
                with patch.object(fx.app._ordinary, "turn_activity", return_value=snap):
                    await fx.app.handle_ordinary_completion(PhysicalStarted(*key.index, origin, BindingTaskFeedback(),
                        lambda: True, lambda: True, reply))
                state = presenter._process.get(key)
                if state:
                    await entered.wait()
                    if order == "early":
                        release.set()
                        await state.ready.wait()
                        state.finish("done")
                        await state.task
                fx.channel.reply_results.append(sent_result("process-mid", chat_id="oc_direct"))
                await fx.app.handle_ordinary_completion(PhysicalAnswer(*key.index, "owner", origin, "completed",
                    "actual body", logical_id=key.turn_id, activity=snap, reply=reply))
                ledger = {mid: row.confirmed_answer_items.copy() for mid, row in reply.messages.items()}
                await fx.app.handle_ordinary_completion(LogicalTerminal(*key.index, "owner", origin, "completed",
                    reply, "ou_owner", lambda: True))
                anchors = [opts.reply_to for _, _, opts in fx.channel.send_calls]
                if state:
                    release.set()
                    await state.task
                    port.complete.assert_awaited_once_with("cot", "process-mid", "done")
                return ledger, anchors
        baseline = await run("off")
        self.assertEqual(set(baseline[0]), {"process-mid"})
        self.assertTrue(baseline[0]["process-mid"])
        self.assertEqual(baseline[1], ["process-mid"])
        for order in ("early", "late"):
            self.assertEqual(await run(order), baseline)

    async def test_cross_binding_split_receipts_match_ordinary_delivery(self):
        async def run(enabled):
            async with channel_fixture() as fx, asyncio.timeout(2):
                if enabled:
                    port = process_port()
                    presenter = fx.app._progress_cards
                    presenter._process_port = port
                    snap = process_snapshot(1, binding="A")
                    key = PresentationKey("A", snap.thread_id, snap.turn_id)
                    origin = FakeMessage("work", message_id="input-A")
                    with patch.object(fx.app._ordinary, "turn_activity", return_value=snap):
                        await fx.app.handle_ordinary_completion(PhysicalStarted(*key.index, origin,
                            BindingTaskFeedback(), lambda: True, lambda: True, LogicalReply()))
                    state = presenter._process[key]
                    await state.ready.wait()
                    await fx.app.handle_ordinary_completion(PhysicalAnswer(*key.index, "owner", origin,
                        "completed", "first body", logical_id=key.turn_id, activity=snap))
                    await state.task
                    port.complete.assert_awaited_once_with("cot", "process-mid", "done")
                scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
                human = FakeMessage("start", message_id="input-B")
                anchor = ClaudeInputAnchor(scope, human, None, AsyncMock())
                target = ConversationTarget(scope, anchor, "turn-B")
                reply = LogicalReply()
                fx.channel.send_results.extend(sent_result(mid, chat_id=scope.chat_id)
                                               for mid in ("first-chunk", "process-mid"))
                await fx.app.handle_ordinary_completion(PhysicalAnswer("B", "thread-B", "turn-B", "owner", target,
                    "completed", "x" * 2000 + "\n" + "y" * 2000, logical_id="turn-B", reply=reply))
                self.assertEqual(len(fx.channel.send_calls), 2)
                return {mid: row.confirmed_answer_items.copy() for mid, row in reply.messages.items()}, reply.mention_target(human.id)
        baseline = await run(False)
        self.assertEqual(set(baseline[0]), {"first-chunk"})
        self.assertTrue(baseline[0]["first-chunk"])
        self.assertEqual(baseline[1], "first-chunk")
        self.assertEqual(await run(True), baseline)

    async def test_malformed_sdk_id_does_not_interrupt_ordinary_codex_delivery(self):
        from types import SimpleNamespace
        from lark_channel.channel.outbound.sender import OutboundSender
        from netizen_cli.runtime.contracts import TurnOutcome
        from tests.support.channel_results import completed_turn_result

        async with channel_fixture() as fx:
            fx.app._progress_cards.binding_location("binding")
            receipt = OutboundSender._to_result(SimpleNamespace(_on_success=None),
                                                {"code": 0, "data": {"message_id": {"malformed": 1}}})
            fx.channel.reply_results.append(receipt)
            await fx.app.handle_completion(TurnOutcome(binding_id="binding", thread_id="s", turn_id="p",
                owner_id="owner", origin=FakeMessage("start", message_id="input"), cwd=fx.project,
                result=completed_turn_result(final_response="actual body"), task_feedback=BindingTaskFeedback()))
            self.assertEqual([mid for mid, content in fx.channel.replies if "actual body" in str(content)], ["input"])

    async def test_execution_unknown_then_exact_result_cannot_reopen_its_writer(self):
        async with channel_fixture() as fx:
            port = process_port()
            presenter = fx.app._progress_cards
            presenter._process_port = port
            key = PresentationKey("binding","native-one","turn-one")
            presenter.tombstone_unknown_process(key)
            await fx.app.handle_ordinary_completion(PhysicalAnswer(*key.index,"user",FakeMessage("prompt",message_id="in"),
                "completed","recovered content",logical_id=key.turn_id,activity=process_snapshot(1)))
            port.create.assert_not_awaited()
            self.assertIn("recovered content",str(fx.channel.replies))
            self.assertFalse(presenter._process)


class ProcessMutationTest(unittest.TestCase):
    def test_transport_and_lifecycle_oracles_reject_faulty_implementations(self):
        from netizen_cli.channel import process_presentation
        from netizen_cli.channel.reply_presenter import _ReplyCardPresenter
        from tests.test_process_presentation import OrdinaryProcessTest

        from tests.test_goal_reply_separation import GoalReplySeparationTest

        start_goal = GoalReplySeparationTest.start
        async def create_in_card_mode(case, **kwargs):
            await case.fx.app._progress_cards._process_port.create("oc_direct", "om_goal", False)
            return await start_goal(case, **kwargs)

        create = ProcessPresentation._create
        supersede = _ReplyCardPresenter._supersede_process
        write = ProcessPresentation._write
        async def replay_create(state):
            return await create(state) or await create(state)
        async def keep_unknown(state, payload):
            result = await write(state,payload)
            return "rejected" if result == "unknown" else result
        def publish_receipt(presenter, state):
            if session := presenter._sessions.get(state.key.index):
                session.run_reply.published(state.created.message_id,state.key.turn_id,(),lambda _:True)
            supersede(presenter,state)
        def supersede_other_inputs(presenter,state):
            for previous in presenter._process.values():
                if previous is not state and previous.created:
                    previous.finish("done")
            supersede(presenter,state)
        for owner, name, change, case in (
            (GoalReplySeparationTest, "start", create_in_card_mode,
             GoalReplySeparationTest("test_rollover_partials_share_one_card_through_final_files_page")),
            (ProcessPresentation,"_create",replay_create,
             ProcessOrderingTest("test_lost_create_is_never_replayed_at_result")),
            (ProcessPresentation,"_write",keep_unknown,
             ProcessOrderingTest("test_only_uncertain_replacement_invalidates_previously_confirmed_list")),
            (process_presentation,"_verify",AsyncMock(return_value=True),
             OrdinaryProcessTest("test_topic_requires_one_exact_placement_read_before_writes")),
            (_ReplyCardPresenter,"_supersede_process",publish_receipt,
             OrdinaryProcessTest("test_claude_safe_items_flow_through_start_and_result_without_becoming_receipts")),
            (_ReplyCardPresenter,"_supersede_process",supersede_other_inputs,
             ProcessOrderingTest("test_supersession_closes_only_the_previous_process_for_the_same_input")),
        ):
            with self.subTest(case=case.id()), patch.object(owner,name,change):
                outcome = unittest.TestResult()
                outcome.buffer = True
                case.run(outcome)
                self.assertEqual(outcome.testsRun,1)
                self.assertFalse(outcome.errors, outcome.errors)
                self.assertTrue(outcome.failures, "mutation survived its oracle")
