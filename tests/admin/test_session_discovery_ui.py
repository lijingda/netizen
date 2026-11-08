from __future__ import annotations

import unittest

from tests.admin.ui_harness import harness_source, read_fixture, run_harness


class SessionDiscoveryUiTest(unittest.TestCase):
    def test_search_project_defaults_current_page_layout_and_latest_answer(self) -> None:
        before, after = read_fixture("session_discovery_ui_harness.js", "// SHIPPED_ADMIN_PAGE\n")
        common, _ = read_fixture("side_sessions_ui_harness.js", "// SHIPPED_ADMIN_PAGE\n")
        before = common + before
        script = harness_source(
            "index.html", ("chat-picker.js", "content-preview.js", "admin.js"), before, after,
        )
        script = "const initialSearch = ''; " + script
        result = run_harness(self, script, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
