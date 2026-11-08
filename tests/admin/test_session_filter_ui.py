from __future__ import annotations

import unittest

from tests.admin.ui_harness import harness_source, read_fixture, run_harness


class SessionFilterUiTest(unittest.TestCase):
    def test_multiselect_queries_search_pagination_accessibility_and_reset(self) -> None:
        before, after = read_fixture("session_filter_ui_harness.js", "// SHIPPED_FILTER_CONTROLLERS\n")
        script = harness_source(
            "index.html", ("chat-picker.js", "admin.js"), before, after,
        )
        result = run_harness(self, script, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
