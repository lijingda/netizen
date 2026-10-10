from __future__ import annotations

import json
import unittest

from netizen_cli.cards.callbacks import CardActionError
from netizen_cli.cards.pagination import decode_page_selection, pagination_controls
from netizen_cli.pagination import PageError, paginate_items


class PaginationTest(unittest.TestCase):
    def test_arbitrary_navigation_preserves_complete_order_and_item_identity(self) -> None:
        items = tuple(object() for _ in range(23))
        for index in (2, 0, 1, 2, 0):
            with self.subTest(page=index):
                page = paginate_items(items, index, page_size=10)
                self.assertEqual((page.page, page.total_pages, page.total_items), (index, 3, 23))
                self.assertEqual(page.items, items[index * 10 : (index + 1) * 10])
                self.assertIs(page.items[0], items[index * 10])
        self.assertEqual(tuple(
            item for index in range(3)
            for item in paginate_items(items, index, page_size=10).items
        ), items)

    def test_empty_sequence_has_one_empty_page(self) -> None:
        page = paginate_items([], 0, page_size=10)
        self.assertEqual((page.items, page.page, page.total_pages, page.total_items), ((), 0, 1, 0))
        with self.assertRaises(PageError):
            paginate_items([], 1, page_size=10)

    def test_page_and_page_size_are_strict(self) -> None:
        for value in (-1, 2, True, "1", 1.5, None):
            with self.subTest(page=value), self.assertRaises(PageError):
                paginate_items(range(20), value, page_size=10)
        for value in (0, -1, True, "10", 1.5, None):
            with self.subTest(page_size=value), self.assertRaises(ValueError):
                paginate_items(range(20), 0, page_size=value)

    def test_page_control_fits_existing_form_with_payload_only_on_button(self) -> None:
        button = {
            "tag": "button", "name": "jump", "form_action_type": "submit",
            "text": {"tag": "plain_text", "content": "跳转"},
            "behaviors": [{"type": "callback", "value": {"snapshot": ["one", "two"]}}],
        }
        control = pagination_controls(page_field="result_page", page=1, total_pages=3, button=button)
        assert control is not None
        self.assertEqual(control["tag"], "column_set")
        selector = control["columns"][0]["elements"][0]
        self.assertEqual(selector["name"], "result_page")
        self.assertEqual(selector["initial_option"], "1")
        self.assertTrue(selector["required"])
        self.assertEqual(selector["options"], [
            {"text": {"tag": "plain_text", "content": f"第{index + 1}页"}, "value": str(index)}
            for index in range(3)
        ])
        self.assertIs(control["columns"][1]["elements"][0], button)
        encoded = json.dumps(control)
        self.assertEqual(encoded.count('"snapshot"'), 1)
        self.assertNotIn('"tag": "form"', encoded)

    def test_single_page_needs_no_control(self) -> None:
        self.assertIsNone(pagination_controls(
            page_field="result_page", page=0, total_pages=1, button={"tag": "button"},
        ))

    def test_page_selection_accepts_only_advertised_canonical_values(self) -> None:
        for value in (None, {}, 0, True, "", "01", "+1", "-1", " 1", "1\n", "１", "١", "3", "9" * 5000):
            with self.subTest(value=repr(value)[:20]), self.assertRaises(CardActionError):
                decode_page_selection(value, 3)
        for value in ("0", "1", "2"):
            self.assertEqual(decode_page_selection(value, 3), int(value))


if __name__ == "__main__":
    unittest.main()
