from __future__ import annotations

import unittest

from tests.admin.ui_harness import harness_source, read_fixture, run_harness


class ListDetailsUiTest(unittest.TestCase):
    def test_session_disclosures_and_readonly_plan_details_pagination(self) -> None:
        before, after = read_fixture("list_details_ui_harness.js", "// SHIPPED_LIST_DETAILS\n")
        script = harness_source(
            "index.html", ("chat-picker.js", "admin.js"), before, after,
        )
        result = run_harness(self, script, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
