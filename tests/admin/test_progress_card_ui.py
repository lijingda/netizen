from __future__ import annotations

import unittest

from tests.admin.ui_harness import CHAT_CONFIG_SCRIPTS, harness_source, read_fixture, run_harness


class ProgressCardUiTest(unittest.TestCase):
    def test_global_settings_save_discard_errors_and_expiry(self) -> None:
        before, after = read_fixture("progress_card_ui_harness.js", "// SHIPPED_PROGRESS_CARD_CONTROLLER\n")
        script = harness_source(
            "index.html", (*CHAT_CONFIG_SCRIPTS, "progress-card.js", "admin.js"), before, after,
        )
        for case in ("save_discard", "readonly", "conflict", "expiry", "read_failure", "refresh_failure", "late_refresh", "preview_order"):
            with self.subTest(case=case):
                result = run_harness(
                    self, script, timeout=10, args=("-", case),
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
