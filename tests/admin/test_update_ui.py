from __future__ import annotations

from pathlib import Path
import json
import os
import shutil
import subprocess
import unittest
from unittest import mock

from netizen.deployment.update_protocol import CODES, PHASES, TERMINAL_PHASES


class UpdateUiTest(unittest.TestCase):
    def test_update_submission_reconnect_and_terminal_evidence(self) -> None:
        node = shutil.which("node")
        if node is None:
            reason = "Node.js 22 is required for Admin JavaScript behavior tests"
            if os.environ.get("CI") == "true":
                self.fail(reason)
            self.skipTest(reason)
        root = Path(__file__).resolve().parents[2]
        source = (root / "netizen/admin/static/admin.js").read_text(encoding="utf-8")
        # Exercise the shipped controller with deterministic HTTP and timers.
        core = source[source.index("async function api("):source.index("async function mutate(")]
        harness = Path(__file__).with_name("update_ui_harness.js").read_text(encoding="utf-8")
        before, after = harness.split("// SHIPPED_UPDATE_CONTROLLER\n")
        contract = (
            "assert.deepEqual(Object.keys(updatePhaseLabels).sort(), "
            + json.dumps(sorted(PHASES)) + ");\n"
            + "assert.deepEqual([...updateTerminalPhases].sort(), "
            + json.dumps(sorted(TERMINAL_PHASES)) + ");\n"
            + "assert.deepEqual(Object.keys(updateCodeMessages).sort(), "
            + json.dumps(sorted(CODES - {"none"})) + ");\n"
        )
        result = subprocess.run(
            [node, "-e", before + core + contract + after],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class UpdateUiPrerequisitesTest(unittest.TestCase):
    def test_ci_without_node_fails_instead_of_skipping(self) -> None:
        result = unittest.TestResult()
        with (
            mock.patch.object(shutil, "which", return_value=None),
            mock.patch.dict(os.environ, {"CI": "true"}),
        ):
            UpdateUiTest("test_update_submission_reconnect_and_terminal_evidence").run(result)

        self.assertEqual(len(result.failures), 1)
        self.assertIn("Node.js 22 is required", result.failures[0][1])
        self.assertEqual(result.errors, [])
        self.assertEqual(result.skipped, [])

    def test_local_without_node_explicitly_skips(self) -> None:
        result = unittest.TestResult()
        with (
            mock.patch.object(shutil, "which", return_value=None),
            mock.patch.dict(os.environ, {"CI": ""}),
        ):
            UpdateUiTest("test_update_submission_reconnect_and_terminal_evidence").run(result)

        self.assertEqual(len(result.skipped), 1)
        self.assertIn("Node.js 22 is required", result.skipped[0][1])
        self.assertEqual(result.errors, [])
        self.assertEqual(result.failures, [])
