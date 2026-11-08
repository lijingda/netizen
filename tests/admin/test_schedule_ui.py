from __future__ import annotations

import unittest

from tests.admin.ui_harness import harness_source, read_fixture, run_harness


class ScheduleUiTest(unittest.TestCase):
    def test_crud_preview_revision_and_ordinary_session_links(self) -> None:
        before, after = read_fixture("schedule_ui_harness.js", "// SHIPPED_SCHEDULE_CONTROLLER\n")
        script = harness_source(
            "index.html", ("chat-picker.js", "admin.js"), before, after,
        )
        result = run_harness(self, script, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
