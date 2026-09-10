from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from tests.admin.test_session_filter_ui import _HtmlTree


class ScheduleUiTest(unittest.TestCase):
    def test_crud_preview_revision_and_ordinary_session_links(self) -> None:
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
        payload = source[source.index("function actionPayload("):source.index("async function mutate(")]
        helpers = source[source.index("function cell("):source.index("function confirmMaterializedDelete(")]
        project_options = source[source.index("async function queryProjectOptions("):source.index("async function loadSessionProjectOptions(")]
        controller = source[source.index("function scheduleDate("):source.index("function mergeDeferredBindingRuntime(")]
        listeners = source[source.index('scheduleInput("filter").addEventListener'):source.index('document.querySelector("#update-check").addEventListener')]
        fixture = Path(__file__).with_name("schedule_ui_harness.js")
        before, after = fixture.read_text(encoding="utf-8").split("// SHIPPED_SCHEDULE_CONTROLLER\n")
        dom = fixture.with_name("dom_harness.js").read_text(encoding="utf-8")
        result = subprocess.run(
            [node, "-e", "const htmlTree = " + json.dumps(tree.root) + ";\n"
             + dom + before + payload + helpers + project_options + controller + listeners + after],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
