from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest


class ChatPickerUiTest(unittest.TestCase):
    def test_remote_search_pagination_races_selection_and_keyboard(self) -> None:
        node = shutil.which("node")
        if node is None:
            if os.environ.get("CI") == "true":
                self.fail("Node.js is required for Admin JavaScript behavior tests")
            self.skipTest("Node.js is required for Admin JavaScript behavior tests")
        static = Path(__file__).resolve().parents[2] / "netizen_cli/admin/static"
        fixture = Path(__file__).with_name("chat_picker_ui_harness.js")
        before, after = fixture.read_text(encoding="utf-8").split("// SHIPPED_CHAT_PICKER\n")
        dom = fixture.with_name("dom_harness.js").read_text(encoding="utf-8")
        tree = {"tag": "html", "attrs": {}, "children": [
            {"tag": "body", "attrs": {}, "children": []},
        ]}
        result = subprocess.run(
            [node], input="const htmlTree = " + json.dumps(tree) + ";\n" + dom + before
            + (static / "chat-picker.js").read_text(encoding="utf-8") + after,
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
