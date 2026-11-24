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
                return await original(*args, **kwargs)
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
                    await self.runtime._goals[self.target.id].delivery_task
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
            final_mid = next(w[1] for w in reversed(confirmed) if w[0] == "update_card" and w[3] == "任务已完成")
            self.assertEqual(self.reminder_anchor, final_mid, "completion mention anchor")

    def mutate(self, mutation):
        if mutation == "early":
            apply = self.runtime._apply_goal_activity_projection
            def premature(active, projection):
                apply(active, projection)
                if projection and projection.goal_terminal:
                    outcome = self.runtime._intermediate_goal_outcome(active)
                    self.injections.append(asyncio.create_task(self.app._complete_goal(outcome)))
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
                    active.final_response = active.candidate.final_response
                    active.final_items = active.candidate.file_items
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

        async def stalled(event):
            entered.set()
            await asyncio.Event().wait()

        self.runtime.set_goal_rollover_handler(stalled)
        await self.start()
        await self.answer("final_answer", "first")
        await self.rollover()
        await asyncio.wait_for(entered.wait(), 1)
        with patch("netizen_cli.runtime.limits.GOAL_COMPLETION_DELIVERY_TIMEOUT_SECONDS", 1):
            await self.finish(history_answer="final")
        self.assertEqual(sum(content == INCOMPLETE for _, content in self.channel.replies), 1)
        self.assertTrue(any(getattr(content, "markdown", content) == "final" for _, content in self.channel.replies))
        self.assertIsNone(self.runtime.active_goal(self.target.id))
        self.assertFalse(self.app._progress_cards._sessions)
