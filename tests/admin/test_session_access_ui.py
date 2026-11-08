from __future__ import annotations

import unittest

from tests.admin.ui_harness import CONFIG_SCRIPTS, harness_source, read_fixture, run_harness


class SessionAccessUiTest(unittest.TestCase):
    def test_independent_rule_editor_preserves_drafts_and_never_replays_save(self) -> None:
        before, after = read_fixture("session_access_ui_harness.js", "// SHIPPED_SESSION_ACCESS\n")
        script = harness_source(
            "index.html", (*CONFIG_SCRIPTS, "session-access.js"), before, after,
        )
        result = run_harness(self, script, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
