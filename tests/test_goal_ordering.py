"""Explore the physical Goal boundary through decoded Feishu writes."""
import asyncio
import itertools
import json
from dataclasses import dataclass, replace
from unittest.mock import patch

from lark_channel import OutboundCard, SendResult
from openai_codex.generated.v2_all import ThreadItem, Turn, TurnStartedNotification

from netizen_cli.channel.codex_reply_events import CodexReplyEvents
from netizen_cli.channel.physical_delivery import INCOMPLETE
from netizen_cli.domain import GoalOperationState, GoalStatus
from tests.support.channel_results import sent_result
from tests.test_goal_presentation_traces import GoalPresentationCase


@dataclass(frozen=True)
class Schedule:
    turns: int = 2
    order: tuple = ("partial", "body", "file")
    control: str = ""
    end: str = "history"
    slow: bool = False
    progress: bool = True
    # Index in the wire sequence, including opens, results, partials and reminder.
    fault: int = -1
    receipt: str = "ok"


class GoalOrderingTest(GoalPresentationCase):
    def observe_writes(self, row):
        self.writes = []
        self.addressable = {}
        for method in ("reply", "update_card", "send"):
            original = getattr(self.channel, method)

            async def record(*args, method=method, original=original, **kwargs):
                payload = args[1]
                card = payload.card if isinstance(payload, OutboundCard) else payload
                text = json.dumps(card, ensure_ascii=False, default=str) if isinstance(card, dict) else str(card)
                heading = card.get("config", {}).get("summary", {}).get("content") if isinstance(card, dict) else None
                mention = [u.open_id for u in getattr(payload, "mentions", ()) or ()]
                if isinstance(card, dict) and '<at id=ou_owner' in text:
                    mention = ["ou_owner"]
                mid = args[0] if method == "update_card" else None
                index = len(self.writes)
                failed = index == row.fault
                self.writes.append((method, mid, text, heading, mention, not failed))
                if method == "send":
                    self.reminder_anchor = args[2].reply_to
                if failed:
                    return SendResult(success=False, raw={"code": 400 if row.receipt == "rejected" else -1})
                if method == "send":
                    return sent_result("om_reminder", chat_id="oc_direct", parent_id=args[2].reply_to, root_id="om_goal")
                sent = await original(*args, **kwargs)
                address = mid or sent.message_id
                if address and sent.success:
                    self.addressable.setdefault(address, False)
                    if "body[" in text or "part[" in text:
                        self.addressable[address] = True
                return sent
            self.enterContext(patch.object(self.channel, method, record))
        self.enterContext(patch("netizen_cli.channel.reply_presenter._TERMINAL_CARD_RETRY_SECONDS", 0))

    async def run_schedule(self, row, mutation=None):
        self.observe_writes(row)
        self.injections = []
        await self.start(progress=row.progress)
        async with asyncio.timeout(2):
            while self.handle.activity_sink is None:
                await asyncio.sleep(0)
        if mutation:
            self.mutate(mutation)
        for index in range(row.turns):
            final = index == row.turns - 1
            physical = self.handle.record.id
            for item in row.order:
                if item == "partial":
                    await self.answer("partial_answer", f"part[{index}]")
                elif item == "body" and not (final and row.end == "history"):
                    await self.answer("final_answer", f"body[{index}]")
                elif item == "file":
                    await self.file(f"file-{index}.txt")
            if index == 0:
                if row.control == "resume":
                    await self.command("/goal pause", "om_pause")
                    await self.command("/goal resume", "om_resume", sender="ou_other")
            if final and row.control == "clear":
                await self.command("/goal clear", "om_clear")
            interrupted = final and row.end == "stop"
            status = "interrupted" if interrupted else "completed"
            if interrupted:
                native_pause = self.handle.pause

                async def pause():
                    result = await native_pause()
                    await self.turn_event("completed", status)
                    return result

                with patch.object(self.handle, "pause", pause):
                    await self.command("/stop", "om_stop")
                break
            await self.turn_event("completed", status)
            await self.turn_event("completed", status)  # duplicated terminal
            if self.injections:
                await asyncio.gather(*self.injections)
            self.assertFalse(any(f"body[{index}]" in w[2] for w in self.writes), "published unresolved candidate")
            self.assertFalse(any(w[4] for w in self.writes), "intermediate mention")
            if not final:
                self.handle.rollover(f"physical-{index+2}")
                await self.turn_event("started", "inProgress")
                # A repeated old start interleaved with current items cannot reopen it.
                await self.notify("turn/started", TurnStartedNotification(
                    threadId=self.handle.thread_id, turn=Turn(id=physical, items=[], status="inProgress")))
                if not row.slow:
                    await self.runtime._goals[self.target.id].delivery.drain()
                continue
            if row.end == "lost":
                self.codex.full_read_errors.append(OSError("lost final history"))
            history = list(self.handle.record.items)
            if row.end == "history":
                (self.cwd / "history-file.txt").write_text("materialized")
                history.append(ThreadItem.model_validate({
                    "type": "fileChange", "id": "history-patch", "status": "completed",
                    "changes": [{"path": "history-file.txt", "kind": {"type": "add"}, "diff": "materialized\n"}],
                }))
            if row.slow:
                self.codex.read_gate = asyncio.Event()
                initial_reads = len(self.codex.read_calls)
            self.handle.finish(goal_status=None if row.control == "clear" else GoalStatus.COMPLETE,
                               response=f"body[{index}]")
            if row.end == "history":
                history.extend(self.handle.record.items)
            self.handle.record.items = history
            if row.slow:
                async with asyncio.timeout(2):
                    while len(self.codex.read_calls) == initial_reads:
                        await asyncio.sleep(0)
                self.assertFalse(any(w[3] == "任务已完成" for w in self.writes), "published unresolved candidate")
                self.codex.read_gate.set()
        # Confirmed, addressable card with the newest answer owns a separate reminder.
        await self.wait_and_assert(row)

    async def wait_and_assert(self, row):
        self.assertTrue(await self.runtime.wait_idle(timeout=3))
        self.assertEqual(self.app._progress_cards._sessions, {})
        self.assertEqual(self.app._reactions._pulses, {})
        if row.end == "lost":
            self.assertIs(self.runtime.active_goal(self.target.id).state, GoalOperationState.UNKNOWN)
            self.assertTrue(any("观察已停止" in w[2] for w in self.writes), "missing observation notice")
        self.assert_wire(row)

    def assert_wire(self, row):
        writes = self.writes
        if row.fault >= 0:
            self.assertTrue(any(not w[5] for w in writes), "faulted write was never reached")
        confirmed = [w for w in writes if w[5]]
        notices = [w for w in confirmed if INCOMPLETE in w[2]]
        for i in range(row.turns):
            last = i == row.turns - 1
            body = f"body[{i}]"
            owned = [w for w in confirmed if body in w[2]]
            interrupted = last and row.end == "stop"
            successful = not last or row.end not in {"lost", "stop"}
            if successful:
                self.assertEqual(len(owned), 1, "proved answer lost or published twice")
                if row.progress and owned[0][0] == "update_card":
                    self.assertEqual(owned[0][3], "任务已完成" if last else "本轮已完成", "wrong terminal heading")
                self.assertTrue(any(f"file-{i}.txt" in w[2] for w in owned) or notices, "missing proved Files")
                if last and row.end == "history":
                    self.assertTrue(any("history-file.txt" in w[2] for w in owned) or notices, "missing history Files")
            elif interrupted:
                self.assertFalse(any("turn-file.send" in w[2] for w in confirmed if w[3] == "任务已中断"), "Files on interrupted turn")
            parts = [w for w in confirmed if f"part[{i}]" in w[2]]
            if row.end != "lost" or not last:
                self.assertTrue(parts or notices, "proved partial lost")
            for w in owned + parts:
                self.assertFalse(any(f"part[{j}]" in w[2] or f"body[{j}]" in w[2] or f"file-{j}.txt" in w[2]
                                     for j in range(row.turns) if j != i), "cross-turn content")
        mentions = [w for w in writes if w[4]]
        self.assertEqual([w[4] for w in mentions], [] if row.end in {"stop", "lost"} else [["ou_owner"]],
                         "logical completion mention")
        if mentions and mentions[0][0] == "send":
            anchor = next(mid for mid in reversed(self.addressable) if self.addressable[mid])
            self.assertEqual(self.reminder_anchor, anchor, "completion mention anchor")

    def mutate(self, mutation):
        if mutation == "early":
            apply = self.runtime._apply_goal_activity_projection
            def premature(active, projection):
                apply(active, projection)
                if projection and projection.goal_terminal:
                    self.runtime._present_goal_turn(active, active.physical(projection.turn_id))
                    self.injections.append(asyncio.create_task(active.delivery.drain()))
            self.enterContext(patch.object(self.runtime, "_apply_goal_activity_projection", premature))
        elif mutation == "mention":
            project = CodexReplyEvents.result
            def mention(native):
                result = project(native)
                if native.run_continues:
                    return replace(result, end=project(replace(native, run_continues=False)).end)
                return result
            self.enterContext(patch.object(CodexReplyEvents, "result", side_effect=mention))
        else:
            read = self.runtime._read_goal_terminal
            async def change_history(active):
                result = await read(active)
                if mutation == "files":
                    prior = self.codex.goal_turns[0][1].items
                    active.final_items += tuple(prior)
                else:
                    active.final_response = active.physical(active.current_id).facts.final_response
                    active.final_items = active.physical(active.current_id).facts.file_items
                return result
            self.enterContext(patch.object(self.runtime, "_read_goal_terminal", change_history))


def install_case(name, row, mutation=None, witness=None):
    async def test(self):
        if mutation:
            with self.assertRaisesRegex(AssertionError, witness):
                await self.run_schedule(row, mutation)
        else:
            await self.run_schedule(row)
    setattr(GoalOrderingTest, "test_" + name, test)


for n, order in enumerate(itertools.permutations(("partial", "file", "body"))):
    for turns in (2, 3):
        install_case(f"ordering_{n}_{turns}", Schedule(
            turns, order, ("", "resume", "clear")[n % 3], ("history", "tap", "stop", "lost", "history", "tap")[n], n % 2 == 0))
for progress, writes in ((True, 6), (False, 5)):
    for position, state in itertools.product(range(writes), ("unknown", "rejected")):
        install_case(f"write_{int(progress)}_{position}_{state}", Schedule(progress=progress, fault=position, receipt=state))
for name, expected in (("early", "published unresolved candidate"), ("files", "cross-turn content"),
                       ("mention", "intermediate mention"), ("history", "proved answer lost")):
    install_case("mutant_" + name, Schedule(), name, expected)


class GoalDeliveryDeadlineTest(GoalPresentationCase):
    async def test_slow_rollover_reserves_final_handoff_and_reports_missing_delivery(self):
        entered = asyncio.Event()

        send = self.channel.update_card
        async def stalled(mid, card):
            if "first" in str(card):
                entered.set()
                await asyncio.Event().wait()
            return await send(mid, card)
        self.enterContext(patch.object(self.channel, "update_card", stalled))
        await self.start()
        await self.answer("final_answer", "first")
        await self.turn_event("completed", "completed")
        self.handle.rollover("physical-two")
        await self.turn_event("started", "inProgress")
        await asyncio.wait_for(entered.wait(), 1)
        with patch("netizen_cli.runtime.limits.GOAL_COMPLETION_DELIVERY_TIMEOUT_SECONDS", 1):
            await self.finish(history_answer="final")
        self.assertEqual(sum(content == INCOMPLETE for _, content in self.channel.replies), 1)
        self.assertTrue(any("final" in str(card) for _, card in self.channel.updates))
        self.assertIsNone(self.runtime.active_goal(self.target.id))
        self.assertFalse(self.app._progress_cards._sessions)


class PhysicalTurnEvidenceTest(GoalPresentationCase):
    """Exercise independent native evidence and presenter timing on the same Binding."""

    def cards(self, mid):
        return [json.dumps(card, ensure_ascii=False) for address, card in self.channel.updates if address == mid]

    async def late_completion(self, discard=False):
        from openai_codex.generated.v2_all import TurnCompletedNotification
        await self.start()
        first = self.handle.id
        await self.answer("partial_answer", "early partial")
        await self.answer("final_answer", "late completion body")
        self.handle.rollover("second")
        await self.turn_event("started", "inProgress")
        apply = self.runtime._apply_goal_activity_projection
        if discard:
            def current_only(active, projection):
                if projection.turn_id == active.current_id:
                    apply(active, projection)
            self.enterContext(patch.object(self.runtime, "_apply_goal_activity_projection", current_only))
        await self.notify("turn/completed", TurnCompletedNotification(threadId=self.handle.thread_id,
            turn=Turn(id=first, items=[], status="completed")))
        await self.finish(history_answer="last body")
        self.assertIn("late completion body", self.cards("om_reply_2")[-1], "late terminal lost")
        self.assertNotIn("late completion body", self.cards("om_reply_3")[-1])

    async def test_late_completion(self):
        await self.late_completion()

    async def test_mutant_drops_late_completion(self):
        with self.assertRaisesRegex(AssertionError, "late terminal lost"):
            await self.late_completion(discard=True)

    async def history_conflict(self, view, expected, override=False):
        from openai_codex.generated.v2_all import TurnCompletedNotification
        await self.start()
        first = ThreadItem.model_validate({"type": "agentMessage", "id": "a", "phase": "final_answer", "text": "older"})
        latest = ThreadItem.model_validate({"type": "agentMessage", "id": "b", "phase": "final_answer", "text": "newer"})
        await self.item(first)
        await self.item(latest)
        await self.notify("turn/completed", TurnCompletedNotification(threadId=self.handle.thread_id,
            turn=Turn(id=self.handle.id, items=[first], itemsView="summary", status="completed")))
        if override:
            from netizen_cli.partial_answers import final_agent_answer
            self.enterContext(patch("netizen_cli.runtime.goals.merge_final_answer",
                                    side_effect=lambda observed, turn: final_agent_answer(turn.items)))
        self.handle.finish()
        self.handle.record.items_view = view
        self.handle.record.items = [first]
        self.assertTrue(await self.runtime.wait_idle(timeout=2))
        self.assertIn(expected, self.cards("om_reply_2")[-1], "wrong final answer")
        self.assertEqual(self.control.clear_calls, [self.handle.thread_id])

    async def test_full_history_wins(self):
        await self.history_conflict("full", "older")

    async def test_summary_history_preserves_latest_answer(self):
        await self.history_conflict("summary", "newer")

    async def test_mutant_overrides_from_summary_history(self):
        with self.assertRaisesRegex(AssertionError, "wrong final answer"):
            await self.history_conflict("summary", "newer", override=True)

    async def live_snapshot(self, static=False):
        await self.start()
        session = next(iter(self.app._progress_cards._sessions.values()))
        opening = session.observe()
        await self.answer("partial_answer", "append only")
        await self.turn_event("completed", "completed")
        self.handle.rollover("second")
        await self.turn_event("started", "inProgress")
        if static:
            session.observe = lambda: opening
        observed = session.observe().snapshot
        self.assertEqual([p.text for p in observed.partial_answers], ["append only"], "snapshot rewound")
        self.assertEqual(observed.turn_id, self.handle.id)
        await self.finish(history_answer="end")

    async def test_reader_keeps_exact_facts_after_rollover(self):
        await self.live_snapshot()

    async def test_mutant_static_opening_rewinds_snapshot(self):
        with self.assertRaisesRegex(AssertionError, "snapshot rewound"):
            await self.live_snapshot(static=True)

    async def test_missing_start_leaves_previous_terminal_unknown(self):
        await self.start()
        await self.answer("partial_answer", "old partial")
        await self.answer("final_answer", "unsettled answer")
        self.handle.rollover("unannounced")
        await self.answer("partial_answer", "new partial")
        await self.finish(history_answer="proved")
        old = self.cards("om_reply_2")[-1]
        self.assertIn("终态未确认", old)
        self.assertIn("old partial", old)
        self.assertNotIn("unsettled answer", old)
        self.assertIn("proved", self.cards("om_reply_3")[-1])
        self.assertFalse(self.app._progress_cards._sessions)

    async def test_incomplete_projection_preserves_later_answer_and_unknown_total(self):
        await self.start()
        with patch("netizen_cli.sdk_gap_adapter.project_turn_activity_notification", side_effect=ValueError("item")):
            await self.file("unprojectable.txt")
        await self.file("retained.txt")
        await self.answer("final_answer", "still observed")
        await self.rollover()
        first = self.cards("om_reply_2")[-1]
        self.assertIn("unprojectable.txt", first)
        self.assertIn("retained.txt", first)
        self.assertIn("still observed", first)
        self.assertNotIn("累计修改 <font", first)
        await self.finish(history_answer="final")

    async def test_conflicting_successful_patch_views_retain_both_paths(self):
        await self.start()
        await self.file("one.txt")
        await self.file("two.txt")
        await self.rollover()
        first = self.cards("om_reply_2")[-1]
        self.assertIn("one.txt", first)
        self.assertIn("two.txt", first)
        self.assertNotIn("累计修改 <font", first)
        await self.finish(history_answer="final")

    async def test_history_answer_in_tap_skips_materialization_retries(self):
        await self.start()
        await self.answer("final_answer", "already observed")
        await self.turn_event("completed", "completed")
        self.handle.finish()
        self.handle.record.items.clear()
        before = len(self.codex.read_calls)
        self.assertTrue(await self.runtime.wait_idle(timeout=2))
        self.assertEqual(sum(full for _, full in self.codex.read_calls[before:]), 1)
        self.assertIn("already observed", self.cards("om_reply_2")[-1])

    async def independent_results(self, cancelled):
        await self.start(progress=False)
        receipts = []
        send = self.channel.reply
        async def write(source, content, *args, **kwargs):
            text = getattr(content, "markdown", content)
            receipts.append(text)
            if text == "body-1":
                if cancelled:
                    raise asyncio.CancelledError
                await asyncio.Event().wait()
            return await send(source, content, *args, **kwargs)
        self.enterContext(patch.object(self.channel, "reply", write))
        with patch("netizen_cli.runtime.limits.GOAL_COMPLETION_DELIVERY_TIMEOUT_SECONDS", 0.5):
            for index in range(1, 4):
                await self.answer("final_answer", f"body-{index}")
                await self.turn_event("completed", "completed")
                self.handle.rollover(f"p{index + 1}")
                await self.turn_event("started", "inProgress")
            await self.finish(history_answer="body-4")
        for i in range(1, 5):
            self.assertEqual(receipts.count(f"body-{i}"), 1, "unrelated write lost")
        self.assertEqual(receipts.count(INCOMPLETE), 1)
        self.assertIsNone(self.runtime.active_goal(self.target.id))

    async def test_stalled_first_does_not_block_three_other_results(self):
        await self.independent_results(False)

    async def test_cancelled_first_does_not_block_three_other_results(self):
        await self.independent_results(True)

    async def reject_location(self, duplicate):
        await self.start()
        await self.answer("final_answer", "old content")
        self.channel.reply_results.insert(0, sent_result(
            "om_reply_2" if duplicate else "om_elsewhere", chat_id="oc_direct" if duplicate else "oc_other"))
        await self.rollover()
        await self.finish(history_answer="private final")
        self.assertFalse(any("private final" in card for mid in ("om_elsewhere", "om_reply_2") for card in self.cards(mid)))
        self.assertTrue(any("private final" in str(value) for _, value in self.channel.replies))
        self.assertFalse(any(opts.reply_to == "om_elsewhere" for _, _, opts in self.channel.send_calls))

    async def test_inconsistent_location_cannot_own_a_card(self):
        await self.reject_location(False)

    async def test_same_card_cannot_belong_to_two_turns(self):
        await self.reject_location(True)

    async def delayed_management(self, unknown):
        self.create_target()
        writing, release = asyncio.Event(), asyncio.Event()
        management = self.app._goal_management_receipt
        async def delayed(*args, notice, **kwargs):
            if notice.startswith("Goal 已启动"):
                writing.set()
                await release.wait()
            await management(*args, notice=notice, **kwargs)
        self.enterContext(patch.object(self.app, "_goal_management_receipt", delayed))
        command = asyncio.create_task(self.command("/goal ship", "om_goal"))
        try:
            async with asyncio.timeout(2):
                await writing.wait()
            await self.open_stream()
            active = self.runtime._goals[self.target.id]
            await self.command("/goal pause", "om_pause")
            await self.command("/goal resume", "om_resume", sender="ou_other")
            self.assertFalse(active.receipt_attempted.is_set(), "resumer released another start's receipt")
            await self.answer("final_answer", "observed")
            if unknown:
                self.codex.full_read_errors.append(OSError("lost read"))
            with patch("netizen_cli.runtime.limits.GOAL_COMPLETION_DELIVERY_TIMEOUT_SECONDS", .3):
                async with asyncio.timeout(1):
                    await self.finish(history_answer="proved")
            self.assertFalse(active.turns)
            self.assertIsNone(active.final_response)
            if unknown:
                self.assertIs(self.runtime.active_goal(self.target.id).state, GoalOperationState.UNKNOWN)
                self.assertIn("观察已停止", str(self.channel.replies))
            else:
                self.assertIsNone(self.runtime.active_goal(self.target.id))
        finally:
            release.set()
            await command
        self.assertFalse(self.app._progress_cards._sessions)
        self.assertFalse(self.app._reactions._pulses)

    async def test_same_stream_resume_races_logical_end(self):
        await self.delayed_management(False)

    async def test_unknown_receipt_wait_retires_evidence_and_attempts_notice(self):
        await self.delayed_management(True)

    async def test_updated_codex_card_does_not_duplicate_result_in_text(self):
        await self.start()
        self.channel.card_update_results.append(SendResult(success=True, raw={"code": 100}))
        await self.finish(history_answer="delivered once")
        self.assertEqual(sum("delivered once" in card for card in self.cards("om_reply_2")), 1)
        self.assertFalse(any("delivered once" in str(content) for _, content in self.channel.replies))

    async def test_final_handoff_bounds_the_notice_and_drops_local_state(self):
        await self.start(progress=False)
        attempts = []
        async def stall(source, content, *args, **kwargs):
            attempts.append(getattr(content, "markdown", content))
            await asyncio.Event().wait()
        self.enterContext(patch.object(self.channel, "reply", stall))
        with patch("netizen_cli.runtime.limits.GOAL_COMPLETION_DELIVERY_TIMEOUT_SECONDS", .4):
            async with asyncio.timeout(.65):
                await self.finish(history_answer="final")
        self.assertEqual(attempts, ["final", INCOMPLETE])
        self.assertIsNone(self.runtime.active_goal(self.target.id))
        self.assertFalse(self.app._reactions._pulses)

    async def test_file_resolution_failure_keeps_body_and_reports_files_once(self):
        await self.start(progress=False)
        await self.file("output.txt")
        with patch.object(self.app, "_task_completion_files", side_effect=OSError("unavailable")):
            await self.finish(history_answer="file result")
        sent = [getattr(content, "markdown", content) for _, content in self.channel.replies]
        self.assertEqual(sent.count("file result"), 1)
        self.assertEqual(sent.count(INCOMPLETE), 1)
