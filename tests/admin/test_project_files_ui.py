from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from tests.admin.test_session_filter_ui import _HtmlTree


class ProjectFilesUiTest(unittest.TestCase):
    def _run_harness(self, name: str):
        node = shutil.which("node")
        if node is None:
            reason = "Node.js 22 is required for Admin JavaScript behavior tests"
            if os.environ.get("CI") == "true":
                self.fail(reason)
            self.skipTest(reason)
        static = Path(__file__).resolve().parents[2] / "netizen_cli/admin/static"
        tree = _HtmlTree()
        tree.feed((static / "index.html").read_text(encoding="utf-8"))
        fixtures = Path(__file__).parent
        common, project_test = (fixtures / "project_files_ui_harness.js").read_text(encoding="utf-8").split("// SHIPPED_PROJECT_FILES\n")
        source = "const htmlTree = " + json.dumps(tree.root) + ";\n"
        source += (fixtures / "dom_harness.js").read_text(encoding="utf-8") + common
        for script in ("content-preview.js", "file-browser.js", "project-files.js", "attachment-files.js"):
            source += (static / script).read_text(encoding="utf-8")
        source += project_test if name == "project_files_ui_harness.js" else (fixtures / name).read_text(encoding="utf-8")
        result = subprocess.run([node], input=source, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_readonly_navigation_identity_cancellation_and_preview_controls(self):
        self._run_harness("project_files_ui_harness.js")

    def test_attachment_metadata_exact_deletion_and_provider_races(self):
        self._run_harness("attachment_files_ui_harness.js")

    def test_mobile_directory_preview_navigation_and_shared_provider_races(self):
        self._run_harness("mobile_files_ui_harness.js")
