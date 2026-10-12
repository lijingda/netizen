from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from tests.admin.test_session_filter_ui import _HtmlTree


class SideSessionsUiTest(unittest.TestCase):
    def test_full_page_tabs_side_filters_navigation_actions_and_polling(self) -> None:
        node = shutil.which("node")
        if node is None:
            reason = "Node.js 22 is required for Admin JavaScript behavior tests"
            if os.environ.get("CI") == "true":
                self.fail(reason)
            self.skipTest(reason)
        static = Path(__file__).resolve().parents[2] / "netizen_cli/admin/static"
        tree = _HtmlTree()
        tree.feed((static / "index.html").read_text(encoding="utf-8"))
        harness = Path(__file__).with_name("side_sessions_ui_harness.js").read_text(encoding="utf-8")
        before, after = harness.split("// SHIPPED_ADMIN_PAGE\n")
        dom = Path(__file__).with_name("dom_harness.js").read_text(encoding="utf-8")
        script = "const htmlTree = " + json.dumps(tree.root) + ";\n" + dom + before
        script += (static / "chat-picker.js").read_text(encoding="utf-8")
        script += (static / "admin.js").read_text(encoding="utf-8") + after
        for search in ("", "?binding_id=binding-parent", "?binding_id=binding-parent&inventoryState=all"):
            with self.subTest(search=search):
                result = subprocess.run(
                    [node], input="const initialSearch = " + json.dumps(search) + ";\n" + script,
                    capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
