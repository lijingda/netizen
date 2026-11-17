from __future__ import annotations

import json
from pathlib import Path
import re
import unittest

from tests.admin.ui_harness import CONFIG_SCRIPTS, harness_source, read_fixture, run_harness


class AuthExpiryUiTest(unittest.TestCase):
    def test_every_dialog_releases_modal_and_invalidates_pending_editor_reads(self) -> None:
        common, _ = read_fixture("side_sessions_ui_harness.js", "// SHIPPED_ADMIN_PAGE\n")
        before, after = read_fixture("auth_expiry_ui_harness.js", "// SHIPPED_ADMIN_PAGE\n")
        source = harness_source(
            "index.html",
            ("chat-picker.js", *CONFIG_SCRIPTS, "progress-card.js", "worktrees.js",
             "content-preview.js", "file-browser.js", "project-files.js", "admin.js"),
            common + before, after,
        )
        page = Path("netizen_cli/admin/static/index.html").read_text()
        dialogs = re.findall(r'<dialog\b[^>]*\bid="([^"]+)"', page)
        self.assertTrue(dialogs)
        for dialog in [*dialogs, "admin-confirm"]:
            with self.subTest(dialog=dialog):
                bootstrap = "const initialSearch = ''; const testDialog = " + json.dumps(dialog) + ";\n"
                result = run_harness(self, bootstrap + source, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(f"passed {dialog}", result.stdout)
