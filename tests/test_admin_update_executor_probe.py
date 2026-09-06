from __future__ import annotations

import fcntl
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import probe_admin_update_executor as probe


class AdminUpdateExecutorProbeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix=probe._FIXTURE_PREFIX)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.home, self.config = probe._prepare(self.root, time.monotonic() + 5)
        self.state = self.home / ".netizen" / "state"

    def test_fixture_has_physical_worker_and_separate_random_job_identities(self) -> None:
        config = probe._fixture(self.home)
        self.assertNotEqual(config["parent_id"], config["helper_id"])
        release = self.home / ".netizen" / "releases" / config["release_id"]
        self.assertEqual((self.home / ".netizen" / "current").resolve(), release)
        self.assertTrue((release / "source" / "scripts" / "netizen_updater.py").is_file())
        self.assertTrue((release / "venv" / "bin" / "python").is_file())
        parent, parent_id = probe._job(self.home, config, "parent")
        helper, helper_id = probe._job(self.home, config, "helper")
        self.assertEqual(parent._label(parent_id), f"netizen-update-probe-parent-{parent_id}")
        self.assertEqual(helper._label(helper_id), f"netizen-update-{helper_id}")
        self.assertFalse((self.home / ".codex").exists())
        self.assertFalse((self.home / ".netizen" / "credentials").exists())

    def test_fixture_rejects_production_or_injected_job_targets(self) -> None:
        with self.assertRaises(probe.ProbeError):
            probe._fixture(Path.home())
        self.config["parent_id"] = "netizen.service"
        probe._write(self.state / "probe.json", self.config)
        with self.assertRaisesRegex(probe.ProbeError, "identity"):
            probe._fixture(self.home)
        with self.assertRaises(probe.ProbeError):
            probe._job(self.home, self.config, "netizen.service")

    def test_helper_cannot_report_success_if_parent_was_already_stopped(self) -> None:
        probe._write(self.state / "parent-submitted.json", {"helper_id": self.config["helper_id"]})
        with patch.object(probe, "_stop_job") as stop:
            with self.assertRaisesRegex(probe.ProbeError, "already exited"):
                probe._helper(self.home, self.config)
        stop.assert_not_called()
        self.assertFalse((self.state / "completed.json").exists())

    def test_helper_writes_completion_only_after_stop_and_parent_lock_release(self) -> None:
        probe._write(self.state / "parent-submitted.json", {"helper_id": self.config["helper_id"]})
        with (self.state / "parent.lifetime.lock").open("a+b") as parent_lock:
            fcntl.flock(parent_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            def stop(home: Path, config: dict[str, object], role: str, deadline: float) -> None:
                self.assertEqual(home, self.home)
                self.assertEqual(config, self.config)
                self.assertEqual(role, "parent")
                self.assertGreater(deadline, time.monotonic())
                self.assertFalse((self.state / "completed.json").exists())
                self.assertFalse(probe._lock_available(self.state / "helper.execution.lock"))
                fcntl.flock(parent_lock.fileno(), fcntl.LOCK_UN)

            with patch.object(probe, "_stop_job", side_effect=stop):
                probe._helper(self.home, self.config)
        completed = json.loads((self.state / "completed.json").read_text())
        self.assertTrue(completed["parent_stopped"])
        self.assertTrue(completed["parent_lock_released"])
        self.assertTrue(probe._lock_available(self.state / "helper.execution.lock"))

    def test_helper_retained_parent_fd_prevents_a_false_completion(self) -> None:
        probe._write(self.state / "parent-submitted.json", {"helper_id": self.config["helper_id"]})
        with (self.state / "parent.lifetime.lock").open("a+b") as parent_lock:
            fcntl.flock(parent_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(probe, "_stop_job"):
                with self.assertRaisesRegex(probe.ProbeError, "remained held"):
                    probe._helper(self.home, self.config)
        self.assertFalse((self.state / "completed.json").exists())

    def test_dispatch_failure_cleans_both_disposable_targets_and_fixture(self) -> None:
        with (
            patch.object(probe.tempfile, "mkdtemp", return_value=str(self.root)),
            patch.object(probe, "_prepare", return_value=(self.home, self.config)),
            patch.object(probe, "_ParentExecutor") as parent,
            patch.object(probe, "UpdateExecutor") as helper,
            patch.object(probe, "_stop_job") as stop,
        ):
            parent.return_value.launch.side_effect = probe.ProbeError("test manager unavailable")
            result = probe._run_probe(5)
        self.assertFalse(result["ok"])
        self.assertIn("test manager unavailable", result["error"])
        self.assertEqual([call.args[2] for call in stop.call_args_list], ["helper", "parent"])
        helper.return_value.cleanup.assert_called_once_with(self.config["helper_id"])
        parent.return_value.cleanup.assert_called_once_with(self.config["parent_id"])
        self.assertTrue(result["cleanup_complete"])
        self.assertFalse(self.root.exists())

    def test_cleanup_failure_retains_fixture_and_returns_explicit_diagnostic(self) -> None:
        with (
            patch.object(probe.tempfile, "mkdtemp", return_value=str(self.root)),
            patch.object(probe, "_prepare", return_value=(self.home, self.config)),
            patch.object(probe, "_ParentExecutor") as parent,
            patch.object(probe, "_stop_job", side_effect=probe.ProbeError("test cleanup unavailable")),
        ):
            parent.return_value.launch.side_effect = probe.ProbeError("test manager unavailable")
            result = probe._run_probe(5)
        self.assertFalse(result["ok"])
        self.assertEqual(result["retained_fixture"], str(self.root))
        self.assertEqual(len(result["cleanup_errors"]), 2)
        self.assertTrue(self.root.exists())


if __name__ == "__main__":
    unittest.main()
