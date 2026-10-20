from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from tests.admin.test_session_filter_ui import _HtmlTree


class WorktreeUiTest(unittest.TestCase):
    def test_drawer_drafts_branch_modes_and_non_replayed_mutations(self):
        node = shutil.which("node")
        if node is None:
            reason = "Node.js 22 is required for Admin JavaScript behavior tests"
            if os.environ.get("CI") == "true":
                self.fail(reason)
            self.skipTest(reason)
        static = Path(__file__).resolve().parents[2] / "netizen_cli/admin/static"
        tree = _HtmlTree()
        tree.feed((static / "index.html").read_text(encoding="utf-8"))
        fixture = Path(__file__).with_name("worktrees_ui_harness.js")
        before, after = fixture.read_text(encoding="utf-8").split("// SHIPPED_WORKTREES\n")
        dom = fixture.with_name("dom_harness.js").read_text(encoding="utf-8")
        result = subprocess.run([node], input="const htmlTree = " + json.dumps(tree.root) + ";\n"
            + dom + before + (static / "worktrees.js").read_text(encoding="utf-8") + after,
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
