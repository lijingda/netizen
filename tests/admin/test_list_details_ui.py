from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from tests.admin.test_session_filter_ui import _HtmlTree


class ListDetailsUiTest(unittest.TestCase):
    def test_session_disclosures_and_readonly_plan_details_pagination(self) -> None:
        node = shutil.which("node")
        if node is None:
            reason = "Node.js 22 is required for Admin JavaScript behavior tests"
            if os.environ.get("CI") == "true":
                self.fail(reason)
            self.skipTest(reason)
        static = Path(__file__).resolve().parents[2] / "netizen_cli/admin/static"
        tree = _HtmlTree()
        tree.feed((static / "index.html").read_text(encoding="utf-8"))
        source = (static / "admin.js").read_text(encoding="utf-8")
        parts = [
            source[source.index("function cell("):source.index("function showProjectDeleteResult(")],
            source[source.index("function chatModeLabel("):source.index("function sessionUpdatedAtLabel(")],
            source[source.index("function wireSessionActions("):source.index("async function loadSides(")],
            source[source.index("function scheduleDate("):source.index("let scheduleEditor =")],
            source[source.index("let scheduleDetailSerial ="):source.index("let scheduleListCursor =")],
            source[source.index("function scheduleInput("):source.index("const scheduleFieldErrors =")],
            source[source.index("function renderSchedulePreview("):source.index("async function previewSchedule(")],
            source[source.index("function closeScheduleDetail("):source.index("async function refresh(")],
            source[source.index('scheduleInput("runs-prev").addEventListener'):source.index('document.querySelector("#service-restart").addEventListener')],
        ]
        fixture = Path(__file__).with_name("list_details_ui_harness.js")
        before, after = fixture.read_text(encoding="utf-8").split("// SHIPPED_LIST_DETAILS\n")
        dom = fixture.with_name("dom_harness.js").read_text(encoding="utf-8")
        result = subprocess.run(
            [node], input="const htmlTree = " + json.dumps(tree.root) + ";\n"
             + dom + before + "\n".join(parts) + after,
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
