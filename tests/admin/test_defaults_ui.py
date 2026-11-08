from __future__ import annotations

import unittest

from tests.admin.ui_harness import CHAT_CONFIG_SCRIPTS, harness_source, read_fixture, run_harness


class DefaultsUiTest(unittest.TestCase):
    def test_edit_preservation_ordering_exact_fallback_and_pagination(self) -> None:
        before, after = read_fixture("defaults_ui_harness.js", "// SHIPPED_DEFAULTS_CONTROLLER\n")
        script = harness_source(
            "index.html", (*CHAT_CONFIG_SCRIPTS, "progress-card.js", "admin.js"), before, after,
        )
        result = run_harness(
            self, script, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
