from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from netizen.deployment.update_executor import UpdateExecutor
from netizen.deployment import update_protocol as protocol
from scripts import netizen_installer as installer


TARGET = {
    "version": "0.5.0", "releaseId": 123,
    "installerSha256": "a" * 64, "archiveSha256": "b" * 64,
}


class UninstallMaintenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve() / "home"
        self.home.mkdir()
        self.layout = self.create_instance("共享 instance", platform="darwin")
        self.other = self.create_instance("another instance", platform="darwin")
        self.operation = protocol.new_operation(TARGET, "c" * 64)
        self.operation["phase"] = "succeeded"
        for layout in (self.layout, self.other):
            protocol.write_operation(layout.product_root, self.operation)
            self.submission(layout).write_bytes(b"disposable submission")
        self.backend = MagicMock()
        self.backend.capture_definition.return_value = installer.FileSnapshot(True, b"owned definition")
        self.backend.inspect_state.return_value = installer.ServiceState(False, False)

    def create_instance(self, name: str, *, platform: str) -> installer.Layout:
        layout = installer.resolve_layout(
            root=self.home / name, environ={}, account_home=self.home,
            uid=os.geteuid(), username="test-user", platform_name=platform,
        )
        installer.prepare_directories(layout)
        release = layout.releases / ("c" * 64)
        release.mkdir()
        (release / "program").write_bytes(b"installed program")
        layout.current.symlink_to(release)
        layout.service_file.write_bytes(b"owned definition")
        return layout

    def submission(self, layout: installer.Layout) -> Path:
        return UpdateExecutor(layout.home, layout.platform, root=layout.product_root)._plist_path(
            self.operation["operationId"]
        )

    def uninstall(self, *, layout: installer.Layout | None = None) -> None:
        with (
            patch.object(installer, "require_supported_platform"),
            patch.object(installer, "_service_backend", return_value=self.backend),
        ):
            installer.uninstall(layout=self.layout if layout is None else layout)

    def test_macos_cleans_exact_terminal_job_under_lock_before_removing_program(self) -> None:
        calls: list[list[str]] = []

        def manager(arguments, **kwargs):
            calls.append(arguments)
            with self.assertRaises(BlockingIOError):
                protocol.acquire_install_lock(self.layout.product_root)
            self.assertTrue(self.layout.releases.is_dir())
            self.assertEqual(self.backend.capture_definition.call_count, 2)
            self.backend.uninstall_definition.assert_not_called()
            return subprocess.CompletedProcess(arguments, 0)

        with patch.object(UpdateExecutor, "_command", side_effect=manager):
            self.uninstall()
        label = UpdateExecutor(self.home, "darwin", root=self.layout.product_root)._label(
            self.operation["operationId"]
        )
        self.assertEqual(calls[-1], ["launchctl", "bootout", f"gui/{os.geteuid()}/{label}"])
        self.assertFalse(self.submission(self.layout).exists())
        self.assertFalse(self.layout.releases.exists())
        self.assertTrue(self.other.releases.is_dir())
        self.assertEqual(self.submission(self.other).read_bytes(), b"disposable submission")
        self.assertEqual(protocol.read_operation(self.layout.product_root), self.operation)
        self.assertEqual(protocol.read_operation(self.other.product_root), self.operation)

    def test_cleanup_failure_preserves_program_pointer_definition_and_result(self) -> None:
        def manager(arguments, **kwargs):
            return subprocess.CompletedProcess(arguments, 1 if arguments[1] == "bootout" else 0)

        with patch.object(UpdateExecutor, "_command", side_effect=manager):
            with self.assertRaisesRegex(installer.InstallError, "maintenance job"):
                self.uninstall()
        self.backend.uninstall_definition.assert_not_called()
        self.assertTrue(self.layout.releases.is_dir())
        self.assertTrue(self.layout.cache_dir.is_dir())
        self.assertTrue(self.layout.current.is_symlink())
        self.assertTrue(self.layout.service_file.is_file())
        self.assertTrue(self.submission(self.layout).exists())
        self.assertEqual(protocol.read_operation(self.layout.product_root), self.operation)

    def test_nonterminal_result_is_not_inferred_completed_or_cleaned(self) -> None:
        operation = {**self.operation, "phase": "accepted"}
        protocol.write_operation(self.layout.product_root, operation)
        with patch.object(UpdateExecutor, "_command") as manager:
            self.uninstall()
        manager.assert_not_called()
        self.assertTrue(self.submission(self.layout).exists())
        self.assertEqual(protocol.read_operation(self.layout.product_root), operation)

    def test_durable_recovery_required_cleanup_does_not_rewrite_unknown_outcome(self) -> None:
        operation = {**self.operation, "phase": "recovery_required", "code": "worker_lost"}
        protocol.write_operation(self.layout.product_root, operation)
        with patch.object(UpdateExecutor, "_command", return_value=subprocess.CompletedProcess([], 0)):
            self.uninstall()
        self.assertFalse(self.submission(self.layout).exists())
        self.assertEqual(protocol.read_operation(self.layout.product_root), operation)

    def test_unreadable_result_fails_before_cleanup_or_program_deletion(self) -> None:
        (self.layout.state_dir / protocol.OPERATION_FILE).write_bytes(b"invalid JSON")
        with patch.object(UpdateExecutor, "_command") as manager:
            with self.assertRaisesRegex(installer.InstallError, "maintenance job"):
                self.uninstall()
        manager.assert_not_called()
        self.backend.uninstall_definition.assert_not_called()
        self.assertTrue(self.layout.releases.is_dir())
        self.assertTrue(self.layout.current.is_symlink())

    def test_changed_definition_is_revalidated_before_cleanup(self) -> None:
        self.backend.capture_definition.side_effect = [
            installer.FileSnapshot(True, b"owned definition"),
            installer.InstallError("another instance definition"),
        ]
        with patch.object(UpdateExecutor, "_command") as manager:
            with self.assertRaisesRegex(installer.InstallError, "another instance"):
                self.uninstall()
        manager.assert_not_called()
        self.backend.uninstall_definition.assert_not_called()
        self.assertTrue(self.layout.releases.is_dir())

    def test_linux_terminal_cleanup_never_invokes_an_extra_manager_command(self) -> None:
        linux = self.create_instance("linux instance", platform="linux")
        protocol.write_operation(linux.product_root, self.operation)
        with patch.object(UpdateExecutor, "_command") as manager:
            self.uninstall(layout=linux)
        manager.assert_not_called()
        self.assertFalse(linux.releases.exists())
        self.assertEqual(protocol.read_operation(linux.product_root), self.operation)


if __name__ == "__main__":
    unittest.main()
