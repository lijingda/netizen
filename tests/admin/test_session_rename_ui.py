from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from tests.admin.test_session_filter_ui import _HtmlTree


class SessionRenameUiTest(unittest.TestCase):
    def test_candidate_generation_and_save_boundaries(self):
        node = shutil.which("node")
        if node is None:
            if os.environ.get("CI") == "true":
                self.fail("Node.js is required for Admin JavaScript behavior tests")
            self.skipTest("Node.js is required for Admin JavaScript behavior tests")
        static = Path(__file__).resolve().parents[2] / "netizen_cli/admin/static"
        tree = _HtmlTree()
        tree.feed((static / "index.html").read_text(encoding="utf-8"))
        fixture = Path(__file__).with_name("session_rename_ui_harness.js")
        before, after = fixture.read_text(encoding="utf-8").split("// SHIPPED_RENAME_CONTROLLER\n")
        program = ("const htmlTree = " + json.dumps(tree.root) + ";\n"
            + fixture.with_name("dom_harness.js").read_text(encoding="utf-8")
            + before + (static / "chat-picker.js").read_text(encoding="utf-8")
            + (static / "progress-card.js").read_text(encoding="utf-8")
            + (static / "admin.js").read_text(encoding="utf-8") + after)
        for case in ("generate_edit_save", "failure_and_retry", "expired_grants_refresh_together",
                     "close_and_late_response", "save_failure", "expired_and_changed_target",
                     "navigation_closes_generation"):
            with self.subTest(case=case):
                result = subprocess.run([node, "-", case], input=program,
                    capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
