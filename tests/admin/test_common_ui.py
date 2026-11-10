from __future__ import annotations

import unittest

from tests.admin.ui_harness import harness_source, read_fixture, run_harness


class CommonUiTest(unittest.TestCase):
    def test_confirmation_menu_and_feedback_interactions(self) -> None:
        before, after = read_fixture("common_ui_harness.js", "// SHIPPED_COMMON_UI\n")
        source = harness_source("index.html", ("chat-picker.js", "admin.js"), before, after)
        result = run_harness(self, source, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
