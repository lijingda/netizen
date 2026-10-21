from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from tests.admin.test_session_filter_ui import _HtmlTree


class IdentityUiTest(unittest.TestCase):
    def run_controller(self, page: str, source: str, harness: str) -> None:
        node = shutil.which("node")
        if node is None:
            if os.environ.get("CI") == "true":
                self.fail("Node.js is required for Admin JavaScript behavior tests")
            self.skipTest("Node.js is required for Admin JavaScript behavior tests")
        directory = Path(__file__).resolve().parent
        static = directory.parents[1] / "netizen_cli/admin/static"
        tree = _HtmlTree()
        tree.feed((static / page).read_text(encoding="utf-8"))
        dom = (directory / "dom_harness.js").read_text(encoding="utf-8")
        before, after = (directory / harness).read_text(encoding="utf-8").split(
            "// SHIPPED_IDENTITY_CONTROLLER\n"
        )
        result = subprocess.run(
            [node], input="const htmlTree = " + json.dumps(tree.root) + ";\n"
            + dom + before + source + after,
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_profile_selector_membership_and_session_expiry(self) -> None:
        static = Path(__file__).resolve().parents[2] / "netizen_cli/admin/static"
        source = (static / "admin.js").read_text(encoding="utf-8")
        core = "\n".join(source[source.index(start):source.index(end)] for start, end in (
            ("let pendingSessionChatLabels =", "async function loadSessionChatLabels("),
            ("async function api(", "const updateTerminalPhases ="),
            ("function actionPayload(", "async function mutate("),
            ("function actionButton(", "function actionsCell("),
        ))
        self.run_controller("index.html", core + (static / "identity.js").read_text(
            encoding="utf-8"
        ), "identity_ui_harness.js")

    def test_login_only_starts_on_click_and_ignores_cancelled_results(self) -> None:
        static = Path(__file__).resolve().parents[2] / "netizen_cli/admin/static"
        self.run_controller("login.html", (static / "login.js").read_text(
            encoding="utf-8"
        ), "identity_login_ui_harness.js")

    def test_login_success_returns_to_requested_page_and_filters(self) -> None:
        static = Path(__file__).resolve().parents[2] / "netizen_cli/admin/static"
        self.run_controller("login.html", (static / "login.js").read_text(
            encoding="utf-8"
        ), "login_return_ui_harness.js")
