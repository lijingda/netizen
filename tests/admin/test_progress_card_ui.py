from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from tests.admin.test_session_filter_ui import _HtmlTree


class ProgressCardUiTest(unittest.TestCase):
    def test_global_settings_save_discard_errors_and_expiry(self) -> None:
        node = shutil.which("node")
        if node is None:
            reason = "Node.js is required for Admin JavaScript behavior tests"
            if os.environ.get("CI") == "true":
                self.fail(reason)
            self.skipTest(reason)
        static = Path(__file__).resolve().parents[2] / "netizen_cli/admin/static"
        tree = _HtmlTree()
        tree.feed((static / "index.html").read_text(encoding="utf-8"))
        scripts = "\n".join((static / name).read_text(encoding="utf-8") for name in (
            "chat-picker.js", "progress-card.js", "admin.js",
        ))
        fixture = Path(__file__).with_name("progress_card_ui_harness.js")
        before, after = fixture.read_text(encoding="utf-8").split("// SHIPPED_PROGRESS_CARD_CONTROLLER\n")
        dom = fixture.with_name("dom_harness.js").read_text(encoding="utf-8")
        script = "const htmlTree = " + json.dumps(tree.root) + ";\n" + dom + before + scripts + after
        for case in ("save_discard", "readonly", "conflict", "expiry", "read_failure", "late_refresh", "preview_order"):
            with self.subTest(case=case):
                result = subprocess.run([node, "-", case], input=script, capture_output=True,
                                        text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
