from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from tests.admin.test_session_filter_ui import _HtmlTree


class ProjectFormUiTest(unittest.TestCase):
    def test_async_submit_resets_only_after_success_and_keeps_failed_input(self) -> None:
        node = shutil.which("node")
        if node is None:
            reason = "Node.js 22 is required for Admin JavaScript behavior tests"
            if os.environ.get("CI") == "true":
                self.fail(reason)
            self.skipTest(reason)
        static = Path(__file__).resolve().parents[2] / "netizen/admin/static"
        tree = _HtmlTree()
        tree.feed((static / "index.html").read_text(encoding="utf-8"))
        source = (static / "admin.js").read_text(encoding="utf-8")
        handlers = source[
            source.index('document.querySelector("#register-project").addEventListener'):
            source.index('document.querySelector("#session-filter").addEventListener')
        ]
        fixture = Path(__file__).with_name("project_form_ui_harness.js")
        before, after = fixture.read_text(encoding="utf-8").split(
            "// SHIPPED_PROJECT_FORM_HANDLERS\n"
        )
        dom = fixture.with_name("dom_harness.js").read_text(encoding="utf-8")
        result = subprocess.run(
            [node, "-e", "const htmlTree = " + json.dumps(tree.root) + ";\n"
             + dom + before + handlers + after],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
