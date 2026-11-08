from __future__ import annotations

import unittest

from tests.admin.ui_harness import harness_source, read_fixture, run_harness


class ChatPickerUiTest(unittest.TestCase):
    def test_remote_search_pagination_races_selection_and_keyboard(self) -> None:
        before, after = read_fixture("chat_picker_ui_harness.js", "// SHIPPED_CHAT_PICKER\n")
        tree = {"tag": "html", "attrs": {}, "children": [
            {"tag": "body", "attrs": {}, "children": []},
        ]}
        script = harness_source(
            tree, ("chat-picker.js",), before, after,
        )
        result = run_harness(
            self, script, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
