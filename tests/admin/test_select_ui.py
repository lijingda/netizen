from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from tests.admin.test_session_filter_ui import _HtmlTree


class SelectUiTest(unittest.TestCase):
    def test_native_fields_use_shared_menu_without_changing_form_semantics(self) -> None:
        node = shutil.which("node")
        if node is None:
            if os.environ.get("CI") == "true":
                self.fail("Node.js is required for Admin JavaScript behavior tests")
            self.skipTest("Node.js is required for Admin JavaScript behavior tests")
        static = Path(__file__).resolve().parents[2] / "netizen_cli/admin/static"
        tree = _HtmlTree()
        tree.feed((static / "index.html").read_text(encoding="utf-8"))
        source = (static / "admin.js").read_text(encoding="utf-8")
        source = source[source.index("function initializeSelectControls("):
                        source.index("function initializeFilter(")]
        fixture = Path(__file__).with_name("select_ui_harness.js")
        before, after = fixture.read_text(encoding="utf-8").split("// SHIPPED_SELECT_CONTROLS\n")
        dom = fixture.with_name("dom_harness.js").read_text(encoding="utf-8")
        result = subprocess.run(
            [node], input="const htmlTree = " + json.dumps(tree.root) + ";\n" + dom + before + source + after,
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
