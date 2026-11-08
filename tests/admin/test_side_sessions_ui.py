from __future__ import annotations

import json
import unittest

from tests.admin.ui_harness import harness_source, read_fixture, run_harness


class SideSessionsUiTest(unittest.TestCase):
    def test_full_page_tabs_side_filters_navigation_actions_and_sorting(self) -> None:
        before, after = read_fixture("side_sessions_ui_harness.js", "// SHIPPED_ADMIN_PAGE\n")
        script = harness_source(
            "index.html", ("chat-picker.js", "admin.js"), before, after,
        )
        for search in ("", "?binding_id=binding-parent", "?binding_id=binding-parent&inventoryState=archived"):
            with self.subTest(search=search):
                result = run_harness(
                    self, "const initialSearch = " + json.dumps(search) + ";\n" + script,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
