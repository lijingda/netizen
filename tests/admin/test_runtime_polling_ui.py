from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from tests.admin.test_session_filter_ui import _HtmlTree


class RuntimePollingUiTest(unittest.TestCase):
    def test_archival_retry_backoff_and_visible_runtime_updates(self) -> None:
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
        controller = "\n".join(
            source[source.index(start):source.index(end)]
            for start, end in (
                ("function runtimeLabel(", "function chatModeLabel("),
                ("function sideRuntimeLabel(", "function scheduleDate("),
                ("const runtimeResolutionRetries =", "async function refresh("),
                ("function chunkValues(", "let initialTab ="),
            )
        )
        harness = Path(__file__).with_name("runtime_polling_ui_harness.js").read_text(
            encoding="utf-8"
        )
        before, after = harness.split("// SHIPPED_RUNTIME_CONTROLLER\n")
        dom = Path(__file__).with_name("dom_harness.js").read_text(encoding="utf-8")
        result = subprocess.run(
            [node, "-e", "const htmlTree = " + json.dumps(tree.root) + ";\n"
             + dom + before + controller + after],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
