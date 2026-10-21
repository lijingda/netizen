from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from tests.admin.test_session_filter_ui import _HtmlTree


class NavigationUiTest(unittest.TestCase):
    def test_deep_links_history_applied_filters_and_cursor_pages(self) -> None:
        node = shutil.which("node")
        if node is None:
            reason = "Node.js 22 is required for Admin JavaScript behavior tests"
            if os.environ.get("CI") == "true":
                self.fail(reason)
            self.skipTest(reason)
        static = Path(__file__).resolve().parents[2] / "netizen_cli/admin/static"
        tree = _HtmlTree()
        tree.feed((static / "index.html").read_text(encoding="utf-8"))
        fixtures = Path(__file__).parent
        dom = (fixtures / "dom_harness.js").read_text(encoding="utf-8")
        common = (fixtures / "side_sessions_ui_harness.js").read_text(encoding="utf-8").split(
            "// SHIPPED_ADMIN_PAGE\n"
        )[0]
        before, after = (fixtures / "navigation_ui_harness.js").read_text(encoding="utf-8").split(
            "// SHIPPED_ADMIN_PAGE\n"
        )
        script = "const htmlTree = " + json.dumps(tree.root) + ";\n" + dom + common + before
        script += (static / "chat-picker.js").read_text(encoding="utf-8")
        script += (static / "identity.js").read_text(encoding="utf-8")
        script += (static / "worktrees.js").read_text(encoding="utf-8")
        script += (static / "session-access.js").read_text(encoding="utf-8")
        script += (static / "progress-card.js").read_text(encoding="utf-8")
        script += (static / "admin.js").read_text(encoding="utf-8") + after
        cases = [(url, None) for url in (
            "/projects",
            "/worktrees?cursor=stale&actionToken=secret",
            "/sessions/ordinary",
            "/sessions/ordinary?updatedFrom=2026-07-01T00%3A00%3A00Z",
            "/sessions/ordinary?updatedFrom=2026-07-01T00%3A00%3A00Z&updatedRange=last-3-months",
            "/sessions/ordinary?project=docs&project=netizen&inventoryState=archived&chatId=oc_selected"
            "&createdFrom=2026-10-01T00%3A00%3A00Z&createdBefore=2026-10-02T00%3A00%3A00Z"
            "&pageSize=50&sortBy=createdAt&sortDirection=asc&cursor=stale&actionToken=secret",
            "/sessions/ordinary?project=docs&project=netizen&inventoryState=archived&chatId=oc_selected"
            "&createdFrom=2026-10-01T00%3A00%3A00Z&createdBefore=2026-10-02T00%3A00%3A00Z"
            "&pageSize=50&sortBy=createdAt&sortDirection=asc&createdRange=today&updatedRange=all",
            "/sessions/temporary?project=docs&state=closed&pageSize=50&sortDirection=asc"
            "&createdFrom=2026-10-01T00%3A00%3A00Z&createdBefore=2026-10-08T00%3A00%3A00Z&createdRange=last-7-days",
            "/schedules?project=netizen&ended=all&enabled=false",
            "/defaults?kind=group_name",
            "/system",
        )]
        cases.extend(("/projects", kind) for kind in ("project", "worktree-create", "worktree-settings"))
        for initial_url, auth_drawer in cases:
            with self.subTest(initial_url=initial_url, auth_drawer=auth_drawer):
                bootstrap = ("const initialSearch = ''; const initialUrl = " + json.dumps(initial_url)
                             + "; const authDrawer = " + json.dumps(auth_drawer) + ";\n")
                result = subprocess.run([node], input=bootstrap + script, capture_output=True,
                                        text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
