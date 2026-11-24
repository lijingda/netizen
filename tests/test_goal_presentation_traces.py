"""Decoded Goal deliveries through the notification tap, Runtime and Channel."""
from __future__ import annotations

import itertools
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from openai_codex.generated.v2_all import (
    ItemCompletedNotification, ThreadItem, Turn, TurnCompletedNotification, TurnStartedNotification,
)
from openai_codex.models import Notification

from netizen_cli.bindings import BindingTaskFeedback
from netizen_cli.domain import FeishuScope, GoalOperationState, ScopeKind
from netizen_cli.sdk_gap_adapter import GoalStatus, _GoalActivityTap
from tests.support.channel_messages import FakeMessage
from tests.support.channel_results import sent_result
from tests.support.presenter_traces import expect_presenter_trace
from tests.test_binding_scheduled_channel import BindingChannelCase
from tests.test_codex_runtime import FakeGoalControl


REFERENCE = Path(__file__).parent / "fixtures" / "goal_presenter_e94430d.json"


class GoalPresentationTraceTest(BindingChannelCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.control = FakeGoalControl(self.codex)
        self.runtime._goal_control = self.control
        self.scope = FeishuScope("app", "oc_direct", ScopeKind.DIRECT)
        self.channel.chat_types["oc_direct"] = "p2p"
        ids = itertools.count(1)
        self.enterContext(patch("uuid.uuid4", side_effect=lambda: uuid.UUID(int=next(ids))))
        self.channel.reply_results.extend(
            sent_result(f"om_reply_{i}", chat_id="oc_direct") for i in range(1, 20)
        )
        # Exercise a fast rollover occurring entirely between presenter polls.
        self.app._progress_cards._poll_seconds = 60
        self.client = AsyncMock()
        fixture = SimpleNamespace(channel=self.channel, project_root=self.cwd)
        name = self._testMethodName.removeprefix("test_")
        expect_presenter_trace(self, fixture, name, reference=REFERENCE)

    async def command(self, text, message_id, sender="ou_owner"):
        await self.app.handle_message(FakeMessage(text, message_id=message_id, sender_id=sender))

    async def start(self, *, progress=True):
        self.target = self.store.create_channel_binding(
            scope=self.scope, project_alias="work", creator_id="ou_owner",
            task_feedback=BindingTaskFeedback(progress_card_enabled=progress),
        )
        await self.command("/goal ship safely", "om_goal")
        await self.open_stream()

    async def open_stream(self):
        self.handle = self.control.handles[-1]
        self.tap = _GoalActivityTap(self.handle.thread_id)
        self.tap.bind(self.handle.emit_activity)
        await self.turn_event("started", "inProgress")

    async def notify(self, method, payload):
        raw = Notification(method=method, payload=payload)
        self.client.next_goal_notification.return_value = raw
        self.assertIs(await self.tap.next_notification(self.client, self.handle), raw)

    async def turn_event(self, phase, status):
        event_type = TurnStartedNotification if phase == "started" else TurnCompletedNotification
        await self.notify(f"turn/{phase}", event_type(
            threadId=self.handle.thread_id, turn=Turn(id=self.handle.record.id, items=[], status=status),
        ))

    async def item(self, item):
        await self.notify("item/completed", ItemCompletedNotification(
            threadId=self.handle.thread_id, turnId=self.handle.record.id, completedAtMs=1000, item=item,
        ))
        self.handle.record.items.append(item)

    async def answer(self, phase, text):
        # Item IDs intentionally recur across the two physical turns.
        await self.item(ThreadItem.model_validate({
            "type": "agentMessage", "id": phase, "phase": phase, "text": text,
        }))

    async def file(self, name):
        path = self.cwd / name
        path.write_text(name + "\n")
        await self.item(ThreadItem.model_validate({
            "type": "fileChange", "id": "patch", "status": "completed",
            "changes": [{"path": name, "kind": {"type": "add"}, "diff": name + "\n"}],
        }))

    async def rollover(self):
        await self.turn_event("completed", "completed")
        self.handle.rollover("physical-two")
        await self.turn_event("started", "inProgress")

    async def finish(self, *, status=GoalStatus.COMPLETE, history_answer=None):
        await self.turn_event("completed", "completed")
        history = list(self.handle.record.items)
        self.handle.finish(goal_status=status, response=history_answer or "")
        if history_answer is not None:
            history.extend(self.handle.record.items)
        self.handle.record.items = history
        self.assertTrue(await self.runtime.wait_idle(timeout=5))

    def reminder(self, card, origin="om_goal"):
        self.channel.send_results.append(sent_result(
            "om_reminder", chat_id="oc_direct", parent_id=card, root_id=origin,
        ))

    async def two_answers(self):
        await self.answer("partial_answer", "first partial")
        await self.answer("final_answer", "first answer")
        await self.rollover()
        await self.answer("partial_answer", "second partial")
        await self.answer("final_answer", "second answer")
        await self.finish()

    async def test_multiturn_partials(self):
        await self.start()
        self.reminder("om_reply_2")
        await self.two_answers()

    async def test_progress_off(self):
        await self.start(progress=False)
        await self.two_answers()

    async def test_intermediate_and_final_files(self):
        await self.start()
        await self.file("first.txt")
        await self.answer("final_answer", "first file")
        await self.rollover()
        await self.file("second.txt")
        self.reminder("om_reply_2")
        await self.finish(history_answer="second file")

    async def test_pause_resume_same_stream(self):
        await self.start()
        await self.command("/goal pause", "om_pause")
        await self.command("/goal resume", "om_resume", sender="ou_resumer")
        self.assertEqual(len(self.control.handles), 1)
        self.reminder("om_reply_2")
        await self.finish(history_answer="final answer")

    async def test_resume_after_stream_ends(self):
        await self.start()
        await self.command("/goal pause", "om_pause")
        self.reminder("om_reply_2")
        await self.finish(status=GoalStatus.PAUSED, history_answer="before resume")
        await self.command("/goal resume", "om_resume", sender="ou_resumer")
        self.assertEqual(len(self.control.handles), 2)
        await self.open_stream()
        self.reminder("om_reply_4", origin="om_resume")
        await self.finish(history_answer="after resume")

    async def test_interrupted(self):
        await self.start()
        await self.file("unfinished.txt")
        await self.answer("partial_answer", "before stop")
        native_pause = self.handle.pause

        async def pause():
            receipt = await native_pause()
            await self.turn_event("completed", "interrupted")
            return receipt

        with patch.object(self.handle, "pause", pause):
            await self.command("/stop", "om_stop")
        self.assertTrue(await self.runtime.wait_idle(timeout=5))
        self.assertEqual(self.outcomes[-1].final_turn_status, "interrupted")

    async def test_clear_while_completing(self):
        await self.start()
        await self.command("/goal clear", "om_clear")
        self.reminder("om_reply_2")
        await self.finish(status=None, history_answer="finished after clear")
        self.assertEqual(self.control.clear_calls, [self.handle.thread_id])

    async def test_observation_loss_mid_candidate(self):
        await self.start()
        await self.answer("final_answer", "candidate before failed read")
        self.codex.full_read_errors.append(OSError("history unavailable"))
        with self.assertLogs("netizen_cli.codex_runtime", level="ERROR"):
            await self.finish()
        self.assertEqual(self.runtime.active_goal(self.target.id).state, GoalOperationState.UNKNOWN)

    async def test_scheduled_binding_input(self):
        await self.start()
        claim = self.claim(self.target)
        self.channel.send_results.append(sent_result("om_trigger", chat_id="oc_direct"))
        await self.app.dispatch_scheduled_run(claim)
        run = self.store.schedules.get_run(claim.run.id)
        self.assertEqual((run.disposition, run.initial_turn_id), ("steered", self.handle.id))
        self.reminder("om_reply_2")
        await self.two_answers()
        self.assertEqual(self.outcomes[-1].origin.id, "om_goal")
