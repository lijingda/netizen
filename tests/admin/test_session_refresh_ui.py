from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from tests.admin.test_session_filter_ui import _HtmlTree


class SessionRefreshUiTest(unittest.TestCase):
    def test_lists_read_only_on_demand_and_keep_action_refresh_and_runtime_labels(self) -> None:
        node = shutil.which("node")
        if node is None:
            reason = "Node.js 22 is required for Admin JavaScript behavior tests"
            if os.environ.get("CI") == "true":
                self.fail(reason)
            self.skipTest(reason)
        static = Path(__file__).resolve().parents[2] / "netizen_cli/admin/static"
        tree = _HtmlTree()
        tree.feed((static / "index.html").read_text(encoding="utf-8"))
        bootstrap = Path(__file__).with_name("side_sessions_ui_harness.js").read_text(
            encoding="utf-8"
        ).split("// SHIPPED_ADMIN_PAGE\n")[0]
        harness = Path(__file__).with_name("session_refresh_ui_harness.js").read_text(
            encoding="utf-8"
        )
        dom = Path(__file__).with_name("dom_harness.js").read_text(encoding="utf-8")
        result = subprocess.run(
            [node], input="const initialSearch = '';\nconst htmlTree = " + json.dumps(tree.root) + ";\n"
             + dom + bootstrap + (static / "chat-picker.js").read_text(encoding="utf-8")
             + (static / "admin.js").read_text(encoding="utf-8") + harness,
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
