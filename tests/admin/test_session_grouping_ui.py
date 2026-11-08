from __future__ import annotations

import json
import unittest

from tests.admin.ui_harness import harness_source, read_fixture, run_harness


class SessionGroupingUiTest(unittest.TestCase):
    def run_scenario(self, scenario: str) -> None:
        before, after = read_fixture("session_grouping_ui_harness.js", "// SHIPPED_ADMIN_PAGE\n")
        common, _ = read_fixture("side_sessions_ui_harness.js", "// SHIPPED_ADMIN_PAGE\n")
        before = common + before
        script = harness_source(
            "index.html", ("chat-picker.js", "content-preview.js", "admin.js"), before, after,
        )
        script = f"const initialSearch = ''; const scenario = {json.dumps(scenario)};\n" + script
        result = run_harness(self, script, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_chat_identity_grouping_preserves_rows_and_exact_actions(self) -> None:
        self.run_scenario("identity")

    def test_independent_collapsing_refresh_and_current_page_operations(self) -> None:
        self.run_scenario("collapse")

    def test_async_group_names_preserve_focus_collapse_and_expiry(self) -> None:
        self.run_scenario("labels")
