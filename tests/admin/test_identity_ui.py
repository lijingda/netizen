from __future__ import annotations

import unittest

from tests.admin.ui_harness import harness_source, read_fixture, run_harness


class IdentityUiTest(unittest.TestCase):
    def run_controller(self, page: str, scripts: tuple[str, ...], harness: str) -> None:
        before, after = read_fixture(harness, "// SHIPPED_IDENTITY_CONTROLLER\n")
        script = harness_source(page, scripts, before, after)
        result = run_harness(
            self, script, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_profile_selector_membership_and_session_expiry(self) -> None:
        self.run_controller(
            "index.html", ("chat-picker.js", "identity.js", "admin.js"), "identity_ui_harness.js",
        )

    def test_login_only_starts_on_click_and_ignores_cancelled_results(self) -> None:
        self.run_controller("login.html", ("login.js",), "identity_login_ui_harness.js")

    def test_login_success_returns_to_requested_page_and_filters(self) -> None:
        self.run_controller("login.html", ("login.js",), "login_return_ui_harness.js")
