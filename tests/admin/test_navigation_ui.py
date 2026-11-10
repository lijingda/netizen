from __future__ import annotations

import json
import unittest

from tests.admin.ui_harness import CONFIG_SCRIPTS, harness_source, read_fixture, run_harness


class NavigationUiTest(unittest.TestCase):
    def test_late_lists_during_navigation_confirmation_preserve_history_and_drafts(self) -> None:
        before, after = read_fixture("navigation_ui_harness.js", "// SHIPPED_ADMIN_PAGE\n")
        common, _ = read_fixture("side_sessions_ui_harness.js", "// SHIPPED_ADMIN_PAGE\n")
        script = harness_source(
            "index.html",
            ("chat-picker.js", "identity.js", *CONFIG_SCRIPTS, "worktrees.js",
             "session-access.js", "group-access.js", "progress-card.js", "question-notifications.js", "admin.js"),
            common + before, after,
        )
        for page in ("ordinary", "schedules"):
            for navigation in ("back", "forward", "link"):
                for accept in (True, False):
                    with self.subTest(page=page, navigation=navigation, accept=accept):
                        initial_url = (
                            "/sessions/ordinary?updatedRange=all" if page == "ordinary" else "/schedules"
                        )
                        bootstrap = (
                            "const initialSearch = ''; const authDrawer = null; const initialUrl = "
                            + json.dumps(initial_url) + "; const navigationRace = "
                            + json.dumps({"page": page, "navigation": navigation, "accept": accept}) + ";\n"
                        )
                        result = run_harness(self, bootstrap + script, timeout=10)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_deep_links_history_applied_filters_and_cursor_pages(self) -> None:
        before, after = read_fixture("navigation_ui_harness.js", "// SHIPPED_ADMIN_PAGE\n")
        common, _ = read_fixture("side_sessions_ui_harness.js", "// SHIPPED_ADMIN_PAGE\n")
        before = common + before
        script = harness_source(
            "index.html",
            ("chat-picker.js", "identity.js", *CONFIG_SCRIPTS, "worktrees.js",
             "session-access.js", "group-access.js", "progress-card.js", "question-notifications.js", "admin.js"),
            before, after,
        )
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
            "/system", "/admins", "/group-access", "/session-tools",
            "/progress-card", "/question-notifications", "/attachments",
        )]
        cases.extend(("/projects", kind) for kind in ("project", "worktree-create", "worktree-settings"))
        for initial_url, auth_drawer in cases:
            with self.subTest(initial_url=initial_url, auth_drawer=auth_drawer):
                bootstrap = ("const initialSearch = ''; const initialUrl = " + json.dumps(initial_url)
                             + "; const authDrawer = " + json.dumps(auth_drawer) + ";\n")
                result = run_harness(self, bootstrap + script, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
