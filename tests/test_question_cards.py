from __future__ import annotations

import json
import unittest

from netizen_cli.cards.callbacks import CardActionError
from netizen_cli.cards.questions import (
    decode_question_answer,
    decode_question_context,
    is_question_card_action,
    render_question_card,
    render_question_context_card,
    render_question_receipt_card,
    render_question_submission_card,
)
from netizen_cli.user_questions import (
    BindingQuestionTarget,
    QuestionRequest,
    SideQuestionTarget,
    UserQuestion,
    question_target_payload,
)
from tests.support.channel_cards import callback, elements, form_values


class QuestionCardsTest(unittest.TestCase):
    def setUp(self):
        self.target = BindingQuestionTarget("binding-original")
        self.request = QuestionRequest("native-item", (
            UserQuestion("First question", ("First answer",)),
            UserQuestion("Which design?", ("Keep the full first option", "Another option")),
        ))

    def card(self):
        return render_question_card(self.target, self.request, 1)

    def test_free_answer_is_the_last_numbered_option_with_a_single_line_input(self):
        card = self.card()
        form = elements(card.card, "form")[0]
        input_field, = elements(form, "input")
        selector, = elements(form, "select_static")
        self.assertEqual(input_field["input_type"], "text")
        self.assertEqual(input_field["label"]["content"], "3. 自行填写")
        self.assertEqual([option["value"] for option in selector["options"]], ["0", "1", "free"])
        self.assertIn("自行填写", selector["options"][-1]["text"]["content"])
        self.assertEqual([item["text"]["content"] for item in form["elements"][:2]], [
            "1. Keep the full first option", "2. Another option",
        ])
        self.assertLess(form["elements"].index(input_field), form["elements"].index(selector))

    def test_selected_suggestion_preserves_exact_question_index_text_and_original_target(self):
        card = self.card()
        values = {**form_values(card), "netizen_question_choice": "1"}
        value = callback(card, "提交回答")
        self.assertTrue(is_question_card_action(value, values))
        answer = decode_question_answer(value, values)
        self.assertEqual(answer.target, self.target)
        self.assertEqual(value["v"], 2)
        self.assertEqual(value["target"], question_target_payload(self.target))
        self.assertNotIn("binding_id", value)
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
                card = render_question_card(self.target, QuestionRequest("item", (UserQuestion("Title", options),)), 0)
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

    def test_answer_validation_explains_how_to_correct_or_send_outside_the_card(self):
        value = callback(self.card(), "提交回答")
        cases = (
            ({"netizen_question_choice": "free", "netizen_question_text": "  "}, ("自行填写", "输入", "改选")),
            ({"netizen_question_choice": "9"}, ("重新选择", "自行填写")),
            ({"netizen_question_choice": "free", "netizen_question_text": "x" * 1001}, ("1,000", "缩短", "原会话")),
            ({"unrecognized_field": "answer"}, ("表单", "原会话直接发送")),
        )
        for form, expected in cases:
            with self.subTest(form_fields=list(form)):
                with self.assertRaises(CardActionError) as caught:
                    decode_question_answer(value, form)
                for action in expected:
                    self.assertIn(action, str(caught.exception))

    def test_retry_keeps_free_answer_and_choice_with_fresh_nonce(self):
        card = self.card()
        value = callback(card, "提交回答")
        values = {**form_values(card), "netizen_question_text": "我的回答"}
        answer = decode_question_answer(value, values)
        retried = render_question_context_card(answer, notice="原会话不是当前会话")
        retry_value = callback(retried, "提交回答")
        self.assertNotEqual(value["nonce"], retry_value["nonce"])
        self.assertEqual(decode_question_answer(retry_value, form_values(retried)), answer)
        self.assertEqual(retried.card["header"]["title"]["content"], "回答提交失败")
        self.assertIn("尚未交给 Codex", retried.card["header"]["subtitle"]["content"])

    def test_answer_receipt_contains_full_question_and_answer_as_plain_text(self):
        title = '<at id="all">all</at> **Use the standard module name?**'
        option = "*" + "long answer " * 300 + "*"
        card = render_question_card(self.target, QuestionRequest("item", (UserQuestion(title, (option,)),)), 0)
        answer = decode_question_answer(callback(card, "提交回答"), {"netizen_question_choice": "0"})
        receipt = render_question_receipt_card(answer, sender_name="Answering Person")
        self.assertEqual(elements(receipt.card, "markdown"), [])
        plain = [item["content"] for item in elements(receipt.card, "plain_text")]
        self.assertIn("问题\n" + title, plain)
        self.assertIn("Answering Person 的回答\n" + option, plain)
        self.assertEqual(elements(receipt.card, "button"), [])
        self.assertIn("通过问题卡提交的回答", receipt.card["header"]["subtitle"]["content"])
        self.assertTrue(any("提交结果" in text and "原问题卡" in text and "聊天反馈" in text for text in plain))
        self.assertNotIn("正在处理", json.dumps(receipt.card, ensure_ascii=False))

    def test_submission_summary_keeps_question_answer_and_actual_acceptance_without_submit_controls(self):
        card = self.card()
        answer = decode_question_answer(callback(card, "提交回答"), {
            "netizen_question_choice": "free", "netizen_question_text": "我的回答",
        })
        for accepted, notice in ((True, None), (True, "反馈失败"), (False, "接收结果未确认")):
            with self.subTest(accepted=accepted, notice=notice):
                result = render_question_submission_card(
                    answer, sender_name="Answering Person", accepted=accepted, notice=notice,
                )
                self.assertEqual(result.card["header"]["title"]["content"],
                                 "回答已提交" if accepted else "回答提交异常")
                plain = [item["content"] for item in elements(result.card, "plain_text")]
                self.assertIn("问题\nWhich design?", plain)
                self.assertIn("Answering Person 的回答\n我的回答", plain)
                if notice:
                    self.assertIn(notice, plain)
                for tag in ("form", "button", "input", "select_static"):
                    self.assertEqual(elements(result.card, tag), [])

    def test_retry_preserves_option_index_and_allows_selecting_another_option(self):
        card = render_question_card(self.target, QuestionRequest("item", (
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
        card = render_question_card(self.target, QuestionRequest("item", (UserQuestion(title, (option,)),)), 0)
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
            {"target": {"kind": "binding", "id": "bad/identity"}},
            {"binding_id": "binding-ambiguous"}, {"item_id": ""}, {"question_index": True},
            {"question_index": -1}, {"title": ""}, {"options": [None]},
            {"v": 3}, {"v": True}, {"extra": "unexpected"},
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
                render_question_card(self.target, QuestionRequest("item", (question,)), 0)

    def test_invalid_target_is_never_inferred_or_retargeted(self):
        value = callback(self.card(), "提交回答")
        for target in (
            None, "binding-original", {}, {"id": "existing"}, {"kind": "binding"},
            {"kind": "thread", "id": "existing"}, {"kind": True, "id": "existing"},
            {"kind": "side", "id": "side-one", "binding_id": "binding-one"},
            {"kind": "binding", "id": "binding-one", "side_id": "side-one"},
            {"kind": "side", "id": ""}, {"kind": "side", "id": "bad/identity"},
            {"kind": "side", "id": 12}, {"kind": "side", "id": "x" * 129},
        ):
            with self.subTest(target=target), self.assertRaises(CardActionError):
                decode_question_context({**value, "target": target})


class SideQuestionCardsTest(QuestionCardsTest):
    """The entire presentation/answer contract is shared by both target kinds."""

    def setUp(self):
        super().setUp()
        self.target = SideQuestionTarget("side-original")


class LegacyQuestionCardsTest(unittest.TestCase):
    def setUp(self):
        self.value = {
            "kind": "netizen_question", "v": 1, "binding_id": "binding-original",
            "item_id": "old-item", "question_index": 2, "title": "Old question?",
            "options": ["Keep", "Change"], "nonce": "original-nonce",
        }

    def test_legacy_binding_card_decodes_and_retry_upgrades_without_retargeting(self):
        answer = decode_question_answer(self.value, {"netizen_question_choice": "1"})
        self.assertEqual(answer.target, BindingQuestionTarget("binding-original"))
        self.assertEqual(answer.item_id, "old-item")
        self.assertEqual(answer.question_index, 2)
        self.assertEqual(answer.answer, "Change")
        retry = render_question_context_card(answer)
        value = callback(retry, "提交回答")
        self.assertEqual(value["v"], 2)
        self.assertEqual(value["target"], {"kind": "binding", "id": "binding-original"})
        self.assertNotIn("binding_id", value)
        self.assertEqual(decode_question_answer(value, form_values(retry)), answer)

    def test_legacy_card_requires_only_exact_binding_identity(self):
        for changes in (
            {"target": {"kind": "side", "id": "side-one"}},
            {"side_id": "side-one"}, {"binding_id": "bad/identity"},
            {"binding_id": None}, {"binding_id": ""}, {"binding_id": "x" * 129},
            {"v": 2},
        ):
            with self.subTest(changes=changes), self.assertRaises(CardActionError):
                decode_question_context({**self.value, **changes})


if __name__ == "__main__":
    unittest.main()
