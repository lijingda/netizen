from __future__ import annotations

import unittest

from tests.admin.ui_harness import CHAT_CONFIG_SCRIPTS, harness_source, read_fixture, run_harness


class QuestionNotificationsUiTest(unittest.TestCase):
    def test_global_settings_save_discard_errors_and_expiry(self) -> None:
        before, after = read_fixture("question_notifications_ui_harness.js", "// SHIPPED_QUESTION_NOTIFICATIONS_CONTROLLER\n")
        script = harness_source(
            "index.html", (*CHAT_CONFIG_SCRIPTS, "question-notifications.js", "admin.js"), before, after,
        )
        for case in ("save_discard", "readonly", "conflict", "expiry", "late_refresh", "navigation"):
            with self.subTest(case=case):
                result = run_harness(
                    self, script, timeout=10, args=("-", case),
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
