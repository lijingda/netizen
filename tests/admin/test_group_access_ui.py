from __future__ import annotations

import unittest

from tests.admin.ui_harness import CONFIG_SCRIPTS, harness_source, read_fixture, run_harness


class GroupAccessUiTest(unittest.TestCase):
    def test_group_editor_confirms_mode_changes_and_preserves_drafts(self) -> None:
        before, after = read_fixture("group_access_ui_harness.js", "// SHIPPED_GROUP_ACCESS\n")
        script = harness_source(
            "index.html", (*CONFIG_SCRIPTS, "group-access.js"), before, after,
        )
        result = run_harness(self, script, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
