from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from tests.admin.test_session_filter_ui import _HtmlTree


class QuestionNotificationsUiTest(unittest.TestCase):
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
            "chat-picker.js", "question-notifications.js", "admin.js",
        ))
        fixture = Path(__file__).with_name("question_notifications_ui_harness.js")
        before, after = fixture.read_text(encoding="utf-8").split("// SHIPPED_QUESTION_NOTIFICATIONS_CONTROLLER\n")
        dom = fixture.with_name("dom_harness.js").read_text(encoding="utf-8")
        script = "const htmlTree = " + json.dumps(tree.root) + ";\n" + dom + before + scripts + after
        for case in ("save_discard", "readonly", "conflict", "expiry", "late_refresh", "navigation"):
            with self.subTest(case=case):
                result = subprocess.run([node, "-", case], input=script, capture_output=True,
                                        text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
