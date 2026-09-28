from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from tests.admin.test_session_filter_ui import _HtmlTree


class DefaultsUiTest(unittest.TestCase):
    def test_edit_preservation_ordering_exact_fallback_and_pagination(self) -> None:
        node = shutil.which("node")
        if node is None:
            if os.environ.get("CI") == "true":
                self.fail("Node.js is required for Admin JavaScript behavior tests")
            self.skipTest("Node.js is required for Admin JavaScript behavior tests")
        static = Path(__file__).resolve().parents[2] / "netizen/admin/static"
        tree = _HtmlTree()
        tree.feed((static / "index.html").read_text(encoding="utf-8"))
        source = (static / "admin.js").read_text(encoding="utf-8")
        parts = [
            source[source.index("function actionPayload("):source.index("async function mutate(")],
            source[source.index("function cell("):source.index("function confirmMaterializedDelete(")],
            source[source.index("async function queryProjectOptions("):source.index("async function loadSessionProjectOptions(")],
            source[source.index("function defaultScheduleSessionSettings("):source.index("function renderScheduleSessionSettings(")],
            source[source.index("let defaultsEditor ="):source.index("function scheduleDate(")],
            source[source.index('defaultsInput("new").addEventListener'):source.index('scheduleInput("filter").addEventListener')],
        ]
        fixture = Path(__file__).with_name("defaults_ui_harness.js")
        before, after = fixture.read_text(encoding="utf-8").split("// SHIPPED_DEFAULTS_CONTROLLER\n")
        dom = fixture.with_name("dom_harness.js").read_text(encoding="utf-8")
        result = subprocess.run([node], input="const htmlTree = " + json.dumps(tree.root) + ";\n"
                                 + dom + before + "\n".join(parts) + after,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
