from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from tests.admin.test_session_filter_ui import _HtmlTree


class SessionGroupingUiTest(unittest.TestCase):
    def run_scenario(self, scenario: str) -> None:
        node = shutil.which("node")
        if node is None:
            if os.environ.get("CI") == "true":
                self.fail("Node.js is required for Admin behavior tests")
            self.skipTest("Node.js is required for Admin behavior tests")
        static = Path(__file__).resolve().parents[2] / "netizen_cli/admin/static"
        fixtures = Path(__file__).parent
        tree = _HtmlTree()
        tree.feed((static / "index.html").read_text(encoding="utf-8"))
        bootstrap = (fixtures / "side_sessions_ui_harness.js").read_text().split("// SHIPPED_ADMIN_PAGE\n")[0]
        before, after = (fixtures / "session_grouping_ui_harness.js").read_text().split("// SHIPPED_ADMIN_PAGE\n")
        script = f"const initialSearch = ''; const scenario = {json.dumps(scenario)};\n"
        script += "const htmlTree = " + json.dumps(tree.root) + ";\n"
        script += (fixtures / "dom_harness.js").read_text() + bootstrap + before
        script += (static / "chat-picker.js").read_text() + (static / "content-preview.js").read_text()
        script += (static / "admin.js").read_text() + after
        result = subprocess.run([node], input=script, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_chat_identity_grouping_preserves_rows_and_exact_actions(self) -> None:
        self.run_scenario("identity")

    def test_independent_collapsing_refresh_and_current_page_operations(self) -> None:
        self.run_scenario("collapse")

    def test_async_group_names_preserve_focus_collapse_and_expiry(self) -> None:
        self.run_scenario("labels")
