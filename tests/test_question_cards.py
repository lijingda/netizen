from __future__ import annotations

import json
import unittest

from netizen.cards.callbacks import CardActionError
from netizen.cards.questions import (
    decode_question_answer,
    decode_question_context,
    is_question_card_action,
    render_question_card,
    render_question_context_card,
)
from netizen.user_questions import QuestionRequest, UserQuestion
from tests.support.channel_cards import callback, elements, form_values


class QuestionCardsTest(unittest.TestCase):
    def setUp(self):
        self.request = QuestionRequest("native-item", (
            UserQuestion("First question", ("First answer",)),
            UserQuestion("Which design?", ("Keep the full first option", "Another option")),
        ))

    def card(self):
        return render_question_card("binding-original", self.request, 1)

    def test_selected_suggestion_preserves_exact_question_index_text_and_original_binding(self):
        card = self.card()
        values = {**form_values(card), "netizen_question_choice": "1"}
        value = callback(card, "提交回答")
        self.assertTrue(is_question_card_action(value, values))
        answer = decode_question_answer(value, values)
        self.assertEqual(answer.binding_id, "binding-original")
        self.assertEqual(answer.item_id, "native-item")
        self.assertEqual(answer.question_index, 1)
        self.assertEqual(answer.question, self.request.questions[1])
        self.assertEqual(answer.answer, "Another option")
        self.assertIn("request_user_input_async", answer.prompt)
        self.assertIn('native-item\\",1]', answer.prompt)
        self.assertTrue(all(len(option["value"]) < 5 for option in elements(card.card, "select_static")[0]["options"]))

    def test_free_text_requires_free_choice_without_parsing_commands(self):
        for options in ((), ("Suggested",)):
            with self.subTest(options=options):
                card = render_question_card("binding-one", QuestionRequest("item", (UserQuestion("Title", options),)), 0)
                values = {**form_values(card), "netizen_question_text": " /new $skill\nmy own answer "}
                answer = decode_question_answer(callback(card, "提交回答"), values)
                self.assertEqual(answer.answer, " /new $skill\nmy own answer ")
                self.assertEqual(answer.choice, "free")
                if options:
                    values["netizen_question_choice"] = "0"
                    answer = decode_question_answer(callback(card, "提交回答"), values)
                    self.assertEqual(answer.answer, "Suggested")
                    self.assertEqual(answer.choice, "0")

    def test_option_ignores_unselected_free_text_even_when_invalid_or_too_long(self):
        card = self.card()
        for text in ("old answer", "x" * 1001, {"invalid": "text"}):
            values = {**form_values(card), "netizen_question_choice": "1", "netizen_question_text": text}
            answer = decode_question_answer(callback(card, "提交回答"), values)
            self.assertEqual(answer.answer, "Another option")

    def test_empty_or_invalid_answer_has_recoverable_context_for_explicit_retry(self):
        card = self.card()
        value = callback(card, "提交回答")
        for bad in ("", "  ", {"bad": "type"}, "x" * 1001, False):
            with self.subTest(answer=type(bad).__name__):
                submitted = {**form_values(card), "netizen_question_text": bad}
                with self.assertRaises(CardActionError):
                    decode_question_answer(value, submitted)
                context = decode_question_context(value)
                self.assertEqual(context.question_index, 1)
                retry = render_question_context_card(context, notice="请重新填写")
                retry_value = callback(retry, "提交回答")
                self.assertNotEqual(retry_value["nonce"], value["nonce"])
                self.assertEqual(decode_question_context(retry_value), context)

    def test_retry_keeps_free_answer_and_choice_with_fresh_nonce(self):
        card = self.card()
        value = callback(card, "提交回答")
        values = {**form_values(card), "netizen_question_text": "我的回答"}
        answer = decode_question_answer(value, values)
        retried = render_question_context_card(answer, notice="原会话不是当前会话")
        retry_value = callback(retried, "提交回答")
        self.assertNotEqual(value["nonce"], retry_value["nonce"])
        self.assertEqual(decode_question_answer(retry_value, form_values(retried)), answer)

    def test_retry_preserves_option_index_and_allows_selecting_another_option(self):
        card = render_question_card("binding-one", QuestionRequest("item", (
            UserQuestion("Choose", ("A", "B", "A")),
        )), 0)
        answer = decode_question_answer(callback(card, "提交回答"), {
            **form_values(card), "netizen_question_choice": "2", "netizen_question_text": "old text",
        })
        retry = render_question_context_card(answer, notice="请切回原会话")
        values = form_values(retry)
        self.assertEqual(values["netizen_question_choice"], "2")
        self.assertEqual(values["netizen_question_text"], "")
        values["netizen_question_choice"] = "1"
        self.assertEqual(decode_question_answer(callback(retry, "提交回答"), values).answer, "B")

    def test_titles_and_suggestions_render_as_complete_plain_text(self):
        title = '<at id="all">all</at> **Question**'
        option = "*" + "long option " * 100 + "*"
        card = render_question_card("binding-one", QuestionRequest("item", (UserQuestion(title, (option,)),)), 0)
        self.assertEqual(elements(card.card, "markdown"), [])
        plain = [item["content"] for item in elements(card.card, "plain_text")]
        self.assertIn(title, plain)
        self.assertIn("1. " + option, plain)
        values = {**form_values(card), "netizen_question_choice": "0"}
        answer = decode_question_answer(callback(card, "提交回答"), values)
        self.assertEqual(answer.answer, option)
        retry = render_question_context_card(answer)
        self.assertEqual(decode_question_answer(callback(retry, "提交回答"), form_values(retry)).answer, option)

    def test_invalid_payloads_do_not_retarget_or_select_default_answers(self):
        card = self.card()
        values = form_values(card)
        value = callback(card, "提交回答")
        for changes in (
            {"binding_id": "bad/identity"}, {"item_id": ""}, {"question_index": True},
            {"question_index": -1}, {"title": ""}, {"options": [None]},
            {"v": 2}, {"extra": "unexpected"},
        ):
            with self.subTest(changes=changes), self.assertRaises(CardActionError):
                decode_question_context({**value, **changes})
        for form in (None, {}, {**values, "unrelated": "bad"},
                     {**values, "netizen_question_choice": "5"},
                     {**values, "netizen_question_choice": False}):
            with self.subTest(form=form), self.assertRaises(CardActionError):
                decode_question_answer(value, form)
        with self.assertRaises(CardActionError):
            decode_question_context({})
        self.assertFalse(is_question_card_action({}, {"normal": "input"}))
        self.assertTrue(is_question_card_action({}, values))

    def test_transport_nonce_is_not_a_business_precondition(self):
        value = callback(self.card(), "提交回答")
        expected = decode_question_context(value)
        del value["nonce"]
        self.assertEqual(decode_question_context(value), expected)
        self.assertEqual(decode_question_context({**value, "nonce": "old transport value"}), expected)

    def test_oversized_card_rejects_instead_of_silently_truncating(self):
        for question in (UserQuestion("问" * 10_000), UserQuestion("Title", tuple("选项" * 1000 for _ in range(15)))):
            with self.subTest(question=question.title[:20]), self.assertRaises(CardActionError):
                render_question_card("binding-one", QuestionRequest("item", (question,)), 0)


if __name__ == "__main__":
    unittest.main()
