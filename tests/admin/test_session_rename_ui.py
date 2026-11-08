from __future__ import annotations

import unittest

from tests.admin.ui_harness import CHAT_CONFIG_SCRIPTS, harness_source, read_fixture, run_harness


class SessionRenameUiTest(unittest.TestCase):
    def test_candidate_generation_and_save_boundaries(self):
        before, after = read_fixture("session_rename_ui_harness.js", "// SHIPPED_RENAME_CONTROLLER\n")
        script = harness_source(
            "index.html", (*CHAT_CONFIG_SCRIPTS, "progress-card.js", "admin.js"), before, after,
        )
        for case in ("generate_edit_save", "failure_and_retry", "expired_grants_refresh_together",
                     "close_and_late_response", "save_failure", "expired_and_changed_target",
                     "navigation_closes_generation"):
            with self.subTest(case=case):
                result = run_harness(
                    self, script, timeout=10, args=("-", case),
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
