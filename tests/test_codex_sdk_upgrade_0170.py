from __future__ import annotations

from contextlib import closing
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from openai_codex import AsyncCodex

from netizen_cli.partial_answers import PartialAnswer, partial_answers_from_items
from netizen_cli.runtime.native_values import _final_agent_response
from netizen_cli.thread_history import AppServerThreadHistory
from netizen_cli.turn_activity import project_turn_activity_notification
from netizen_cli.turn_plan_observer import PinnedTurnActivityObserver


SHOW = {
    "mode": "show", "version": 1, "trustedContacts": "check",
    "annotation": {"kind": "wellbeingHelpline", "subject": "answer"},
}
HIDE = {"mode": "hide", "version": 1, "replacement": {"kind": "wellbeingPromptHelpline"}}
BLOCK_PROTECTION = {
    "mode": "block", "version": 1, "reason": {"kind": "protection", "protectionType": "example"},
}
BLOCK_CYBER = {"mode": "block", "version": 1, "reason": {"kind": "cyber", "trustedAccess": False}}


class CodexAnswerDisplayContractTest(unittest.TestCase):
    def test_display_directives_preserve_answer_delivery_and_single_consumer(self) -> None:
        codex = AsyncCodex()
        codex._initialized = True
        client = codex._client._sync
        router = client._router
        observer = PinnedTurnActivityObserver(codex)
        partial_text = "  Stable answer fragment.\n"
        final_text = "  Final answer.\n"
        for case, moderated, display, expected_moderated, expected_display in (
            ("show-annotation", False, SHOW, False, SHOW),
            ("hide", True, HIDE, True, HIDE),
            ("block-protection", True, BLOCK_PROTECTION, True, BLOCK_PROTECTION),
            ("block-cyber", True, BLOCK_CYBER, True, BLOCK_CYBER),
            ("extra-field", False, {**SHOW, "futureField": 42}, False, SHOW),
            ("unknown-mode", False, {"mode": "blur", "version": 1}, True, None),
            ("future-version", False, {**SHOW, "version": 2}, True, None),
            ("legacy-action", False, {"action": "block", "blockType": "unknown"}, True, None),
            ("null-display", False, None, False, None),
        ):
            with self.subTest(case=case):
                turn_id = f"turn-{case}"
                with router.pending_turn("thread-one") as cursors:
                    subscription = router.prepare_turn(turn_id, "thread-one", cursors, for_handle=True)
                self.assertIsNotNone(subscription)
                with closing(subscription):
                    notifications = []
                    baseline_events = []
                    for phase, text in (
                        ("commentary", "Checking the answer."),
                        ("partial_answer", partial_text),
                        ("final_answer", final_text),
                    ):
                        item = {"type": "agentMessage", "id": phase, "phase": phase, "text": text}
                        payload = {"threadId": "thread-one", "turnId": turn_id, "completedAtMs": 42}
                        baseline = client._coerce_notification("item/completed", {**payload, "item": item})
                        # Also exercise the non-final projections with synthetic moderation metadata.
                        marked_item = {**item, "moderation": {"moderated": moderated, "display": display}}
                        note = client._coerce_notification("item/completed", {**payload, "item": marked_item})
                        root = note.payload.item.root
                        self.assertEqual(root.text, text)
                        self.assertEqual(
                            root.moderation.model_dump(mode="json", by_alias=True),
                            {"moderated": expected_moderated, "display": expected_display},
                        )

                        target = {"expected_thread_id": "thread-one", "expected_turn_id": turn_id}
                        plain = project_turn_activity_notification(baseline, **target)
                        self.assertEqual(project_turn_activity_notification(note, **target), plain)
                        if plain.event is not None:
                            baseline_events.append(plain.event)
                        notifications.append(note)
                        router.route_notification(note)

                    state = router._turn_states[turn_id]
                    subscribers = dict(state.subscribers)
                    self.assertEqual(len(subscribers), 1)
                    observation = observer.observe(thread_id="thread-one", turn_id=turn_id, after_cursor=0)
                    self.assertEqual(state.subscribers, subscribers)
                    self.assertEqual(tuple(state.events.values()), tuple(notifications))
                    self.assertEqual(observation.next_cursor, len(notifications))
                    self.assertFalse(observation.turn_completed)
                    self.assertEqual(observation.events, tuple(baseline_events))
                    partial = PartialAnswer("thread-one", turn_id, "partial_answer", partial_text)
                    self.assertEqual(observation.partial_answers, (partial,))

                    items = []
                    for notification in notifications:
                        consumed = subscription.next()
                        self.assertIs(consumed, notification)
                        items.append(consumed.payload.item)
                    self.assertEqual(_final_agent_response(items), final_text)
                    self.assertEqual(partial_answers_from_items(items, thread_id="thread-one", turn_id=turn_id),
                                     (partial,))


class CodexHistoryDisplayContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_items_list_preserves_answer_text_with_hide_directive(self) -> None:
        def respond(method, params, *, response_model):
            self.assertEqual(method, "thread/items/list")
            return response_model.model_validate({"data": [{
                "turnId": "turn-one",
                "item": {
                    "type": "agentMessage", "id": "answer", "phase": "final_answer",
                    "text": "History answer.", "moderation": {"moderated": True, "display": HIDE},
                },
            }], "nextCursor": None, "backwardsCursor": None})

        request = AsyncMock(side_effect=respond)
        codex = SimpleNamespace(_initialized=True, _client=SimpleNamespace(request=request))
        page = await AppServerThreadHistory(codex).list_items("thread-one", "turn-one", limit=1)
        entry, = page.data
        self.assertEqual(entry.turn_id, "turn-one")
        item = entry.item.root
        self.assertTrue(item.moderation.moderated)
        self.assertEqual(item.moderation.display.root.mode, "hide")
        self.assertEqual(item.text, "History answer.")
