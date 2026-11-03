from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from tests.admin.test_session_filter_ui import _HtmlTree


class ProjectFilesUiTest(unittest.TestCase):
    def test_readonly_navigation_identity_cancellation_and_preview_controls(self):
        node = shutil.which("node")
        if node is None:
            reason = "Node.js 22 is required for Admin JavaScript behavior tests"
            if os.environ.get("CI") == "true":
                self.fail(reason)
            self.skipTest(reason)
        static = Path(__file__).resolve().parents[2] / "netizen_cli/admin/static"
        tree = _HtmlTree()
        tree.feed((static / "index.html").read_text(encoding="utf-8"))
        fixture = Path(__file__).with_name("project_files_ui_harness.js")
        before, after = fixture.read_text(encoding="utf-8").split("// SHIPPED_PROJECT_FILES\n")
        dom = fixture.with_name("dom_harness.js").read_text(encoding="utf-8")
        result = subprocess.run(
            [node], input="const htmlTree = " + json.dumps(tree.root) + ";\n"
            + dom + before + (static / "file-browser.js").read_text(encoding="utf-8")
            + (static / "project-files.js").read_text(encoding="utf-8") + after,
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_attachment_metadata_exact_deletion_and_provider_races(self):
        node = shutil.which("node")
        if node is None:
            if os.environ.get("CI") == "true":
                self.fail("Node.js 22 is required for Admin JavaScript behavior tests")
            self.skipTest("Node.js 22 is required for Admin JavaScript behavior tests")
        static = Path(__file__).resolve().parents[2] / "netizen_cli/admin/static"
        tree = _HtmlTree()
        tree.feed((static / "index.html").read_text(encoding="utf-8"))
        fixtures = Path(__file__).parent
        common = (fixtures / "project_files_ui_harness.js").read_text(encoding="utf-8").split("// SHIPPED_PROJECT_FILES\n")[0]
        source = "const htmlTree = " + json.dumps(tree.root) + ";\n"
        source += (fixtures / "dom_harness.js").read_text(encoding="utf-8") + common
        for name in ("file-browser.js", "project-files.js", "attachment-files.js"):
            source += (static / name).read_text(encoding="utf-8")
        source += (fixtures / "attachment_files_ui_harness.js").read_text(encoding="utf-8")
        result = subprocess.run([node], input=source, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
