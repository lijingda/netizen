from __future__ import annotations

import unittest

from tests.admin.ui_harness import harness_source, read_fixture, run_harness


class ProjectFormUiTest(unittest.TestCase):
    def test_drawer_modes_drafts_submission_and_result_checks(self) -> None:
        before, after = read_fixture("project_form_ui_harness.js", "// SHIPPED_PROJECT_FORM_HANDLERS\n")
        script = harness_source(
            "index.html", ("chat-picker.js", "admin.js"), before, after,
        )
        result = run_harness(self, script, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
