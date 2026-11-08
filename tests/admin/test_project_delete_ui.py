from __future__ import annotations

import unittest

from tests.admin.ui_harness import harness_source, read_fixture, run_harness


class ProjectDeleteUiTest(unittest.TestCase):
    def test_preview_confirmation_partial_results_and_connection_loss(self) -> None:
        before, after = read_fixture("project_delete_ui_harness.js", "// SHIPPED_PROJECT_CONTROLLER\n")
        script = harness_source(
            "index.html", ("chat-picker.js", "admin.js"), before, after,
        )
        result = run_harness(self, script, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
