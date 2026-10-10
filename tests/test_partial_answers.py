from __future__ import annotations

import unittest
from types import SimpleNamespace

from openai_codex.generated.v2_all import ThreadItem

from netizen_cli.codex_runtime import _final_agent_response
from netizen_cli.partial_answers import PartialAnswer, partial_answers_from_items


class PartialAnswerProjectionTest(unittest.TestCase):
    def test_typed_fragments_preserve_full_text_and_native_item_order(self) -> None:
        values = [
            ("p1", "partial_answer", "  stable answer\n" * 200),
            ("comment", "commentary", "working"),
            ("p2", "partial_answer", "second"),
            ("p1", "partial_answer", "duplicate replacement ignored"),
            ("empty", "partial_answer", " \n"),
            ("final", "final_answer", "final recap"),
        ]
        items = [ThreadItem.model_validate({
            "id": item_id, "type": "agentMessage", "phase": phase, "text": text,
        }) for item_id, phase, text in values]
        answers = partial_answers_from_items(items, thread_id="thread", turn_id="turn")
        self.assertEqual(answers, (
            PartialAnswer("thread", "turn", "p1", values[0][2]),
            PartialAnswer("thread", "turn", "p2", "second"),
        ))
        self.assertEqual(_final_agent_response(items), "final recap")

    def test_partial_only_never_becomes_final_and_legacy_final_still_works(self) -> None:
        items = [ThreadItem.model_validate({
            "id": "p", "type": "agentMessage", "phase": "partial_answer", "text": "answer",
        })]
        self.assertIsNone(_final_agent_response(items))
        items.extend(ThreadItem.model_validate({
            "id": item_id, "type": "agentMessage", "phase": phase, "text": text,
        }) for item_id, phase, text in (
            ("earlier", "final_answer", "before steer"),
            ("latest", "final_answer", "after steer"),
        ))
        self.assertEqual(_final_agent_response(items), "after steer")
        self.assertEqual(len(partial_answers_from_items(items, thread_id="t", turn_id="u")), 1)
        legacy = ThreadItem.model_validate({
            "id": "legacy", "type": "agentMessage", "text": "legacy final",
        })
        self.assertEqual(_final_agent_response([items[0], legacy]), "legacy final")

    def test_non_typed_items_are_not_promoted_into_answers(self) -> None:
        item = SimpleNamespace(root=SimpleNamespace(
            type="agentMessage", phase="partial_answer", id="p", text="untyped",
        ))
        self.assertEqual(partial_answers_from_items([item], thread_id="t", turn_id="u"), ())
