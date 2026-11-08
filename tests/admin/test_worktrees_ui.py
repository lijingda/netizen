from __future__ import annotations

import unittest

from tests.admin.ui_harness import harness_source, read_fixture, run_harness


class WorktreeUiTest(unittest.TestCase):
    def test_drawer_drafts_branch_modes_and_non_replayed_mutations(self):
        before, after = read_fixture("worktrees_ui_harness.js", "// SHIPPED_WORKTREES\n")
        script = harness_source(
            "index.html", ("worktrees.js",), before, after,
        )
        result = run_harness(self, script, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
