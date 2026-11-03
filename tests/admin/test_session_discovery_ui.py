from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from tests.admin.test_session_filter_ui import _HtmlTree


class SessionDiscoveryUiTest(unittest.TestCase):
    def test_search_project_defaults_current_page_layout_and_latest_answer(self) -> None:
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
        before, after = (fixtures / "session_discovery_ui_harness.js").read_text().split("// SHIPPED_ADMIN_PAGE\n")
        script = "const initialSearch = ''; const htmlTree = " + json.dumps(tree.root) + ";\n"
        script += (fixtures / "dom_harness.js").read_text() + bootstrap + before
        script += (static / "chat-picker.js").read_text() + (static / "admin.js").read_text() + after
        result = subprocess.run([node], input=script, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
