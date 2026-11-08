from __future__ import annotations

import unittest

from tests.admin.ui_harness import harness_source, read_fixture, run_harness


class SessionRefreshUiTest(unittest.TestCase):
    def test_lists_read_only_on_demand_and_keep_action_refresh_and_runtime_labels(self) -> None:
        before, after = read_fixture("session_refresh_ui_harness.js")
        common, _ = read_fixture("side_sessions_ui_harness.js", "// SHIPPED_ADMIN_PAGE\n")
        before = common + before
        script = harness_source(
            "index.html", ("chat-picker.js", "admin.js"), before, after,
        )
        script = "const initialSearch = '';\n" + script
        result = run_harness(self, script, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
