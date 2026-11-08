from __future__ import annotations

import unittest

from tests.admin.ui_harness import harness_source, read_fixture, run_harness


class SelectUiTest(unittest.TestCase):
    def test_native_fields_use_shared_menu_without_changing_form_semantics(self) -> None:
        before, after = read_fixture("select_ui_harness.js", "// SHIPPED_SELECT_CONTROLS\n")
        common, _ = read_fixture("side_sessions_ui_harness.js", "// SHIPPED_ADMIN_PAGE\n")
        before = common + before
        script = harness_source(
            "index.html", ("chat-picker.js", "admin.js"), before, after,
        )
        script = "const initialSearch = '';\n" + script
        result = run_harness(
            self, script, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
