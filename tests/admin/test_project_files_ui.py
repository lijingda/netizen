from __future__ import annotations

import unittest

from tests.admin.ui_harness import harness_source, read_fixture, run_harness


class ProjectFilesUiTest(unittest.TestCase):
    def _run_harness(self, name: str):
        before, after = read_fixture("project_files_ui_harness.js", "// SHIPPED_PROJECT_FILES\n")
        if name != "project_files_ui_harness.js":
            _, after = read_fixture(name)
        script = harness_source(
            "index.html",
            ("content-preview.js", "file-browser.js", "project-files.js", "attachment-files.js"),
            before, after,
        )
        result = run_harness(self, script, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_readonly_navigation_identity_cancellation_and_preview_controls(self):
        self._run_harness("project_files_ui_harness.js")

    def test_attachment_metadata_exact_deletion_and_provider_races(self):
        self._run_harness("attachment_files_ui_harness.js")

    def test_mobile_directory_preview_navigation_and_shared_provider_races(self):
        self._run_harness("mobile_files_ui_harness.js")
