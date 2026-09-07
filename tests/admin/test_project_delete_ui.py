from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from tests.admin.test_session_filter_ui import _HtmlTree


class ProjectDeleteUiTest(unittest.TestCase):
    def test_preview_confirmation_partial_results_and_connection_loss(self) -> None:
        node = shutil.which("node")
        if node is None:
            reason = "Node.js 22 is required for Admin JavaScript behavior tests"
            if os.environ.get("CI") == "true":
                self.fail(reason)
            self.skipTest(reason)
        root = Path(__file__).resolve().parents[2]
        static = root / "netizen/admin/static"
        tree = _HtmlTree()
        tree.feed((static / "index.html").read_text(encoding="utf-8"))
        source = (static / "admin.js").read_text(encoding="utf-8")
        payload = source[
            source.index("function actionPayload("):
            source.index("async function mutate(")
        ]
        controller = source[
            source.index("function cell("):
            source.index("const timeRangeControllers =")
        ]
        fixture = Path(__file__).with_name("project_delete_ui_harness.js")
        before, after = fixture.read_text(encoding="utf-8").split(
            "// SHIPPED_PROJECT_CONTROLLER\n"
        )
        dom = fixture.with_name("dom_harness.js").read_text(encoding="utf-8")
        result = subprocess.run(
            [node, "-e", "const htmlTree = " + json.dumps(tree.root) + ";\n"
             + dom + before + payload + controller + after],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
