from __future__ import annotations

import copy
import json
import unittest
from types import SimpleNamespace

from netizen_cli.card_content import (
    CardContentError,
    extract_card_content,
    original_card,
    original_message_card,
    request_locale,
)


def card(*elements: dict, **kwargs: object) -> dict:
    return {"schema": "2.0", "body": {"elements": list(elements)}, **kwargs}


def markdown(text: str) -> dict:
    return {"tag": "markdown", "content": text}


class OriginalCardTest(unittest.TestCase):
    def test_original_versions_and_event_dsl_are_recognized_without_sdk_label(self):
        old = {"header": {"title": {"tag": "plain_text", "content": "Title"}}, "elements": [markdown("Body")]}
        new = card(markdown("Body"))
        for source, expected in ((old, old), (new, new), (json.dumps(new), new),
                                 ({"user_dsl": json.dumps(new)}, new)):
            self.assertEqual(original_card(source), expected)
        for summary in ({"title": "Summary", "elements": [[{"tag": "text", "text": "Upgrade client"}]]},
                        {"header": {"title": {"content": "Title"}}, "elements": [[{"tag": "text"}]]},
                        {"schema": "3.0", "body": {"elements": []}}, {}, {"body": {"elements": []}}):
            self.assertIsNone(original_card(summary))

    def test_original_event_is_reused_when_typed_card_is_only_received_summary(self):
        original = card(markdown("Body"))
        message = SimpleNamespace(content=SimpleNamespace(card={"title": "Summary", "elements": [[]]}, raw={}),
                                  raw={"message": {"content": json.dumps({"user_dsl": json.dumps(original)})}})
        self.assertEqual(original_message_card(message), original)

    def test_request_locale_uses_only_explicit_current_event_ua_marker(self):
        for ua, expected in (("client LarkLocale/en_US extra", "en_us"),
                             ("LarkLocale/zh-CN", "zh_cn"), ("client en_US", None),
                             ("LarkLocale/en_US/injected", None)):
            self.assertEqual(request_locale(SimpleNamespace(raw={"message": {"user_agent": ua}})), expected)
        self.assertIsNone(request_locale(SimpleNamespace(raw={"locale": "zh_cn"})))

    def test_raw_string_limit_is_checked_before_decoding(self):
        with self.assertRaises(CardContentError):
            original_card(" " * 256_001)

    def test_recognition_bounds_shallow_legacy_element_lists(self):
        for source in ({"elements": [markdown("x")] * 4097},
                       {"i18n_elements": {"zh_cn": [markdown("x")] * 3000,
                                          "en_us": [markdown("x")] * 3000}}):
            with self.assertRaises(CardContentError):
                original_card(source)

    def test_extraction_rejects_oversized_deep_and_cyclic_structures(self):
        deep: dict = markdown("Body")
        for _ in range(40):
            deep = {"tag": "column", "elements": [deep]}
        cyclic = card()
        cyclic["body"]["elements"].append(cyclic)
        for value in (card(deep), cyclic,
                      card(*[markdown("Body") for _ in range(5000)]), card(markdown("x" * 256_001))):
            with self.subTest(kind=type(value).__name__), self.assertRaises(CardContentError):
                extract_card_content(value)


class CardProjectionTest(unittest.TestCase):
    def test_v1_is_best_effort_with_locator_even_when_no_body_was_extracted(self):
        for old in ({"elements": [markdown("Body")]}, {"elements": []}):
            result = extract_card_content(old, message_id="om_child", parent_message_id="om_forward")
            self.assertTrue(result.text.startswith("此为飞书 1.0 卡片"))
            self.assertIn("可能缺少标题或部分正文", result.text)
            self.assertIn("om_child", result.text)
            self.assertIn("om_forward", result.text)
        self.assertIn("未提取到卡片正文", result.text)
        self.assertFalse(result.content_read)

    def test_v2_text_order_repetitions_and_source_object_are_preserved(self):
        original = card(markdown("Repeat"), markdown("Repeat"), header={"title": {"tag": "plain_text", "content": "Title"}})
        before = copy.deepcopy(original)
        result = extract_card_content(original, message_id="om_message")
        self.assertTrue(result.text.endswith("\nTitle\nRepeat\nRepeat"))
        self.assertIn('"message_id": "om_message"', result.text)
        self.assertFalse(result.truncated)
        self.assertTrue(result.content_read)
        self.assertEqual(original, before)

    def test_v2_header_only_keeps_title_and_marks_body_unread(self):
        source = {"schema": "2.0", "header": {"title": {"tag": "plain_text", "content": "Title"}}}
        self.assertEqual(original_card(source), source)
        result = extract_card_content(source)
        self.assertIn("Title", result.text)
        self.assertIn("未提取到卡片正文", result.text)
        self.assertTrue(result.truncated)
        self.assertTrue(result.content_read)
        for invalid in ({**source, "body": None}, {**source, "body": []}, {**source, "schema": "3.0"}):
            self.assertIsNone(original_card(invalid))

    def test_localized_text_prefers_request_then_default_then_one_translation(self):
        source = card({"tag": "markdown", "content": "Default", "i18n_content": {"en_us": "English", "zh_cn": "中文"}})
        self.assertEqual(extract_card_content(source, preferred_locale="en-US").text.splitlines()[-1], "English")
        self.assertEqual(extract_card_content(source, preferred_locale="fr_fr").text.splitlines()[-1], "Default")
        self.assertEqual(extract_card_content(source).text.splitlines()[-1], "Default")
        del source["body"]["elements"][0]["content"]
        self.assertEqual(extract_card_content(source).text.splitlines()[-1], "中文")
        source["body"]["elements"][0]["i18n_content"] = {"de_de": "Deutsch", "fr_fr": "Français"}
        self.assertEqual(extract_card_content(source).text.splitlines()[-1], "Deutsch")
        source["body"]["elements"][0]["i18n_content"].update({"zh_cn": "  ", "en_us": {"content": "HIDDEN"}})
        self.assertEqual(extract_card_content(source).text.splitlines()[-1], "Deutsch")

    def test_v1_multilingual_body_selects_one_version(self):
        source = {"i18n_elements": {"zh_cn": [markdown("中文")], "en_us": [markdown("English")]}}
        result = extract_card_content(source, preferred_locale="en_us")
        self.assertIn("English", result.text)
        self.assertNotIn("中文", result.text)

    def test_multilingual_header_tags_override_default_with_one_language(self):
        def text_tag(text: str) -> dict:
            return {"tag": "text_tag", "text": {"tag": "plain_text", "content": text}}

        source = card(markdown("Body"), header={
            "text_tag_list": [text_tag("SUCCESS")],
            "i18n_text_tag_list": {"zh_cn": [text_tag("FAILED")], "en_us": [text_tag("RETRY")]},
        })
        for locale, expected in (("zh-CN", "FAILED"), ("en_us", "RETRY"), ("fr_fr", "FAILED"), (None, "FAILED")):
            result = extract_card_content(source, preferred_locale=locale)
            self.assertTrue(result.text.endswith("\n" + expected + "\nBody"))
            self.assertFalse(result.truncated)
        source["header"]["i18n_text_tag_list"]["zh_cn"] = []
        result = extract_card_content(source, preferred_locale="zh_cn")
        self.assertTrue(result.text.endswith("\nBody"))
        for tag in ("SUCCESS", "FAILED", "RETRY"):
            self.assertNotIn(tag, result.text)
        source["header"]["i18n_text_tag_list"] = {}
        self.assertTrue(extract_card_content(source).text.endswith("\nSUCCESS\nBody"))

    def test_columns_fields_and_containers_keep_visible_body(self):
        source = card({"tag": "column_set", "columns": [{"tag": "column", "elements": [
            {"tag": "div", "text": {"tag": "plain_text", "content": "Intro"},
             "fields": [{"text": {"tag": "lark_md", "content": "Field"}}]},
            {"tag": "collapsible_panel", "header": {"title": {"tag": "plain_text", "content": "Panel"}},
             "elements": [markdown("Details")]},
        ]}]})
        self.assertTrue(extract_card_content(source).text.endswith("\nIntro\nField\nPanel\nDetails"))

    def test_button_payloads_are_not_body_and_unknown_tags_do_not_leak(self):
        hidden = {"tag": "plain_text", "content": "HIDDEN"}
        source = card({"tag": "button", "text": {"tag": "plain_text", "content": "Submit"},
                       "value": hidden, "confirm": {"title": hidden}, "events": [hidden],
                       "behaviors": [hidden], "options": [hidden], "disabled_tips": hidden})
        result = extract_card_content(source)
        self.assertTrue(result.text.endswith("\nSubmit"))
        self.assertNotIn("HIDDEN", result.text)
        self.assertFalse(result.truncated)
        source["body"]["elements"].append({"tag": "future_widget", "content": "UNVERIFIED", "elements": [hidden]})
        result = extract_card_content(source, message_id="om_card")
        self.assertIn("未提取的卡片组件", result.text)
        self.assertIn("om_card", result.text)
        self.assertNotIn("UNVERIFIED", result.text)
        self.assertNotIn("HIDDEN", result.text)
        self.assertTrue(result.truncated)

    def test_containers_use_only_their_own_displayed_fields(self):
        hidden = {"tag": "plain_text", "content": "NON_RENDERED"}
        for tag in ("column", "note", "form", "interactive_container"):
            element = {"tag": tag, "header": {"title": hidden},
                       "elements": [markdown("Visible")], "actions": [hidden]}
            if tag == "column":
                element = {"tag": "column_set", "columns": [element]}
            with self.subTest(tag=tag):
                result = extract_card_content({"elements": [element]})
                self.assertIn("Visible", result.text)
                self.assertNotIn("NON_RENDERED", result.text)
        result = extract_card_content({"elements": [{
            "tag": "action", "header": {"title": hidden}, "elements": [hidden],
            "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "Visible button"}}],
        }]})
        self.assertIn("Visible button", result.text)
        self.assertNotIn("NON_RENDERED", result.text)

    def test_text_fields_keep_source_and_share_media_limitation_and_locator(self):
        text = "  Text ![diagram](img_v2_example) <audio file_key='file_v3_example'></audio>\n"
        for element in (
            markdown(text),
            {"tag": "plain_text", "content": text},
            {"tag": "div", "text": {"tag": "lark_md", "content": text}},
            {"tag": "collapsible_panel", "header": {"title": markdown(text)},
             "elements": [markdown("Body")]},
            *({"tag": "table", "columns": [{"name": "value", "data_type": data_type}],
               "rows": [{"value": text}]} for data_type in ("text", "lark_md", "markdown")),
        ):
            with self.subTest(tag=element["tag"], columns=element.get("columns")):
                result = extract_card_content(card(element), message_id="om_card", parent_message_id="om_forward")
                body = text + ("\nBody" if element["tag"] == "collapsible_panel" else "")
                self.assertTrue(result.text.endswith("\n" + body))
                self.assertIn("源码", result.text)
                self.assertIn("未渲染", result.text)
                self.assertIn("未读取", result.text)
                self.assertIn('"message_id": "om_card"', result.text)
                self.assertIn('"parent_message_id": "om_forward"', result.text)
                self.assertFalse(result.truncated)
                self.assertTrue(result.content_read)

    def test_markdown_syntax_is_preserved_without_interpreting_rendered_media(self):
        for text in (
            "<raw>\\</raw> ![real](img_real)",
            "<raw>`</raw> ![real](img_real)`",
            "<raw>\n```\n</raw>\n![real](img_real)",
            "    ![example](img_example)\n\n<raw>&lt;/raw&gt;</raw>\n<!-- literal -->",
        ):
            for element in (
                markdown(text),
                {"tag": "collapsible_panel", "header": {"title": markdown(text)},
                 "elements": [markdown("Body")]},
            ):
                with self.subTest(text=text, tag=element["tag"]):
                    result = extract_card_content(card(element), message_id="om_card")
                    self.assertIn(text, result.text)
                    self.assertIn("未读取", result.text)
                    self.assertIn("om_card", result.text)
                    self.assertFalse(result.truncated)
                    self.assertTrue(result.content_read)

    def test_table_only_reads_declared_visible_cells_including_a_column_named_value(self):
        source = card({"tag": "table", "columns": [{"name": "value", "display_name": "Status", "data_type": "text"},
                                                     {"name": "count", "display_name": "Count", "data_type": "number"}],
                       "rows": [{"value": "Ready", "count": 2, "secret": "HIDDEN"}]})
        self.assertTrue(extract_card_content(source).text.endswith("\nStatus | Count\nReady | 2"))
        source["body"]["elements"][0]["rows"][0]["value"] = {"content": "HIDDEN"}
        result = extract_card_content(source)
        self.assertTrue(result.truncated)
        self.assertNotIn("HIDDEN", result.text)
        source["body"]["elements"][0]["columns"][0]["data_type"] = []
        self.assertTrue(extract_card_content(source).truncated)

    def test_table_empty_display_name_never_exposes_internal_column_name(self):
        source = card({"tag": "table", "columns": [{"name": "internal_status", "display_name": ""}],
                       "rows": [{"internal_status": "Ready"}]})
        result = extract_card_content(source)
        self.assertTrue(result.text.endswith("\nReady"))
        self.assertNotIn("internal_status", result.text)
        self.assertFalse(result.truncated)
        source["body"]["elements"][0]["columns"].append({"name": "internal_count", "display_name": "Count"})
        source["body"]["elements"][0]["rows"][0]["internal_count"] = 2
        self.assertTrue(extract_card_content(source).text.endswith("\n | Count\nReady | 2"))

    def test_table_only_projects_the_first_fifty_column_positions(self):
        columns = [{"name": f"c{i}", "display_name": f"Column {i}", "data_type": "text"}
                   for i in range(51)]
        row = {f"c{i}": f"Value {i}" for i in range(51)}
        for count, malformed_first in ((50, False), (51, False), (51, True)):
            selected = columns[:count]
            if malformed_first:
                selected[0] = {}
            with self.subTest(count=count, malformed_first=malformed_first):
                result = extract_card_content(card({"tag": "table", "columns": selected, "rows": [row]}))
                self.assertIn("Column 49", result.text)
                self.assertIn("Value 49", result.text)
                self.assertNotIn("Column 50", result.text)
                self.assertNotIn("Value 50", result.text)
                self.assertEqual(result.truncated, count > 50)

    def test_checker_reports_unread_state_and_additional_content(self):
        for checked in (True, False):
            source = card({"tag": "checker", "text": {"tag": "plain_text", "content": "Task"},
                           "checked": checked, "button_area": {"buttons": [
                               {"tag": "button", "text": {"tag": "plain_text", "content": "NOT_READ"}},
                           ]}})
            result = extract_card_content(source)
            self.assertIn("Task", result.text)
            self.assertIn("未读取勾选状态或附加内容", result.text)
            self.assertNotIn("NOT_READ", result.text)
            self.assertTrue(result.truncated)

    def test_media_and_controls_expose_only_labels_and_mark_unread(self):
        source = card({"tag": "img", "alt": {"tag": "plain_text", "content": "Chart image"}, "img_key": "PRIVATE"},
                      {"tag": "select_static", "label": {"tag": "plain_text", "content": "Choose"},
                       "options": [{"text": {"tag": "plain_text", "content": "HIDDEN"}}]})
        result = extract_card_content(source)
        self.assertTrue(result.truncated)
        self.assertIn("Chart image", result.text)
        self.assertIn("Choose", result.text)
        self.assertNotIn("PRIVATE", result.text)
        self.assertNotIn("HIDDEN", result.text)

    def test_malformed_components_are_explicit_and_do_not_crash(self):
        result = extract_card_content(card(markdown("Known"), {"tag": []}, {"tag": "div", "fields": {"content": "HIDDEN"}}))
        self.assertTrue(result.truncated)
        self.assertIn("Known", result.text)
        self.assertIn("未提取", result.text)
        self.assertNotIn("HIDDEN", result.text)

    def test_output_limits_include_card_notices(self):
        for source in (card(markdown("x" * 64_001)), card(markdown("x" * 63_999)),
                       {"elements": [markdown("x" * 63_999)]}):
            with self.assertRaises(CardContentError):
                extract_card_content(source, message_id="om_card")

    def test_many_short_table_rows_fit_existing_structure_and_text_budgets(self):
        tables = [{"tag": "table", "columns": [{"name": "value", "display_name": "Value"}],
                   "rows": [{"value": str(i)} for i in range(100)]} for _ in range(5)]
        result = extract_card_content(card(*tables, *[markdown(f"Note {i}") for i in range(8)]))
        lines = result.text.splitlines()
        self.assertEqual(lines.count("Value"), 5)
        self.assertEqual(lines.count("99"), 5)
        self.assertEqual(lines[-8:], [f"Note {i}" for i in range(8)])
        self.assertLess(len(result.text), 2000)
        self.assertTrue(result.text.endswith("Note 7"))
        self.assertFalse(result.truncated)


if __name__ == "__main__":
    unittest.main()
