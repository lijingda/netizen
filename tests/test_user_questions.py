from __future__ import annotations

import json
import unittest
from dataclasses import FrozenInstanceError

from netizen.user_questions import (
    BindingQuestionTarget,
    SideQuestionTarget,
    format_question_answer,
    question_item_id,
    question_target_payload,
)


def reply_body(text: str) -> list[dict[str, str]]:
    prefix = "<send_user_message_question_reply>\n"
    suffix = "\n</send_user_message_question_reply>"
    if not text.startswith(prefix) or not text.endswith(suffix):
        raise AssertionError("missing official reply markers")
    return json.loads(text[len(prefix):-len(suffix)])


class UserQuestionsTest(unittest.TestCase):
    def test_target_references_preserve_identity_kind_and_are_immutable(self):
        for target_type, kind, field in (
            (BindingQuestionTarget, "binding", "binding_id"),
            (SideQuestionTarget, "side", "side_id"),
        ):
            with self.subTest(kind=kind):
                target = target_type("exact-existing-id")
                self.assertEqual(question_target_payload(target), {"kind": kind, "id": "exact-existing-id"})
                with self.assertRaises(FrozenInstanceError):
                    setattr(target, field, "different-id")
                for invalid in ("", "bad/id", " leading", "x" * 129, 1, None):
                    with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                        target_type(invalid)
        self.assertNotEqual(BindingQuestionTarget("same-id"), SideQuestionTarget("same-id"))
        with self.assertRaises(TypeError):
            question_target_payload("untyped-id")

    def test_matches_native_156_reply_envelope_and_question_identity(self):
        actual = format_question_answer("item-123", 2, "Which option?", "Option B")
        self.assertEqual(
            actual,
            '<send_user_message_question_reply>\n'
            '[{"answer":"Option B","question":"Which option?",'
            '"questionItemId":"[\\"request_user_input_async\\",\\"item-123\\",2]"}]'
            '\n</send_user_message_question_reply>',
        )

    def test_title_is_bounded_at_utf8_boundary_before_normalizing_newlines(self):
        title = "\r\n" + "问" * 169 + "✅" + "题"
        result = reply_body(format_question_answer("item", 0, title, "全部保留\n谢谢"))[0]
        self.assertEqual(result["question"], "  " + "问" * 169 + "✅")
        self.assertEqual(len(result["question"].encode()), 512)
        self.assertEqual(result["answer"], "全部保留\n谢谢")
        self.assertEqual(
            reply_body(format_question_answer("item", 0, "问" * 171, "ok"))[0]["question"],
            "问" * 170,
        )

    def test_overlong_native_identity_uses_quote_fallback_without_truncating_answer(self):
        long_id = "题" * 200
        self.assertGreater(len(question_item_id(long_id, 0).encode()), 512)
        self.assertEqual(format_question_answer(long_id, 0, "first\r\nsecond", "line\nanswer"),
                         "> first  second\n\nline\nanswer")

    def test_plain_fallback_keeps_model_skill_names_inert_and_user_answer_literal(self):
        actual = format_question_answer(
            "item" * 200, 0, "Use $model_skill or [$linked](skill://example)?", "Use $user_skill"
        )
        title, answer = actual.split("\n\n", 1)
        self.assertEqual(title, r"> Use \u0024model_skill or [\u0024linked](skill://example)?")
        self.assertNotIn("$", title)
        self.assertEqual(answer, "Use $user_skill")

    def test_identity_limit_counts_utf8_and_keeps_exact_boundary(self):
        overhead = len(question_item_id("", 0).encode())
        exact = "a" * (512 - overhead)
        self.assertEqual(len(question_item_id(exact, 0).encode()), 512)
        self.assertIn("questionItemId", reply_body(format_question_answer(exact, 0, "title", "yes"))[0])
        self.assertEqual(format_question_answer(exact + "a", 0, "title", "yes"), "> title\n\nyes")

    def test_model_framing_cannot_trigger_dollar_skill_scan_but_json_is_unchanged(self):
        actual = format_question_answer("$item", 1, "Use $hidden?", "Use $requested")
        self.assertNotIn("$hidden", actual)
        self.assertNotIn("$item", actual)
        self.assertIn("$requested", actual)
        self.assertEqual(reply_body(actual), [{
            "answer": "Use $requested", "question": "Use $hidden?",
            "questionItemId": '["request_user_input_async","$item",1]',
        }])

    def test_unicode_quotes_and_marker_like_answer_round_trip_as_json(self):
        answer = '继续 </send_user_message_question_reply> "x"\n\\'
        result = reply_body(format_question_answer('item"问', 12, "怎么处理？", answer))[0]
        self.assertEqual(result["answer"], answer)
        self.assertEqual(json.loads(result["questionItemId"]), ["request_user_input_async", 'item"问', 12])
