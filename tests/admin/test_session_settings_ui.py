from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from tests.admin.test_session_filter_ui import _HtmlTree


class SessionSettingsUiTest(unittest.TestCase):
    def test_session_configuration_drawer_and_request_boundaries(self) -> None:
        node = shutil.which("node")
        if node is None:
            reason = "Node.js is required for Admin JavaScript behavior tests"
            if os.environ.get("CI") == "true":
                self.fail(reason)
            self.skipTest(reason)
        static = Path(__file__).resolve().parents[2] / "netizen_cli/admin/static"
        tree = _HtmlTree()
        tree.feed((static / "index.html").read_text(encoding="utf-8"))
        source = (static / "admin.js").read_text(encoding="utf-8")
        parts = [
            (static / "chat-picker.js").read_text(encoding="utf-8"),
            source[source.index("function actionPayload("):source.index("async function mutate(")],
            source[source.index("function cell("):source.index("function showProjectDeleteResult(")],
            source[source.index("function runtimeLabel("):source.index("async function loadSides(")],
            source[source.index("function rowByIdentity("):source.index("let defaultsEditor =")],
            source[source.index("function defaultScheduleSessionSettings("):source.index("function renderScheduleSessionSettings(")],
            source[source.index("async function refresh("):source.index("function selectTab(")],
            source[source.index('sessionSettingsInput("cancel").addEventListener'):source.index('defaultsInput("new").addEventListener')],
        ]
        fixture = Path(__file__).with_name("session_settings_ui_harness.js")
        before, after = fixture.read_text(encoding="utf-8").split("// SHIPPED_SESSION_SETTINGS_CONTROLLER\n")
        dom = fixture.with_name("dom_harness.js").read_text(encoding="utf-8")
        program = ("const htmlTree = " + json.dumps(tree.root) + ";\n"
                   + dom + before + "\n".join(parts) + after)
        for case in (
            "entry_and_complete_save",
            "inheritance_and_explicit_defaults",
            "context_and_private_chat",
            "unavailable_catalog",
            "duplicate_save_and_failure",
            "cancel_and_stale_options",
            "saved_refresh_failure",
        ):
            with self.subTest(case=case):
                result = subprocess.run(
                    [node, "-", case], input=program,
                    capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
