from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from netizen.deployment import update_protocol as protocol
from scripts import netizen_installer as installer
from scripts import netizen_updater as updater


BOOTSTRAP = b"#!/bin/sh\nexit 0\n"
TARGET = {
    "version": "0.5.0", "releaseId": 123,
    "installerSha256": hashlib.sha256(BOOTSTRAP).hexdigest(),
    "archiveSha256": "a" * 64,
}


class UpdateProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / ".netizen"
        (self.root / "state").mkdir(parents=True, mode=0o700)
        self.operation = protocol.new_operation(TARGET, "b" * 64)

    def test_private_bounded_atomic_result_roundtrip_and_exact_shape(self) -> None:
        self.assertIsNone(protocol.read_operation(self.root))
        protocol.write_operation(self.root, self.operation)
        self.assertEqual(protocol.read_operation(self.root), self.operation)
        self.assertEqual(stat.S_IMODE((self.root / "state/update.json").stat().st_mode), 0o600)
        for changes in ({"token": "secret"}, {"target": {**TARGET, "url": "https://other"}},
                        {"phase": "running"}, {"code": "raw exception"}, {"schema": True}):
            with self.subTest(changes=changes), self.assertRaises(protocol.UpdateProtocolError):
                protocol.write_operation(self.root, {**self.operation, **changes})
        self.assertEqual(protocol.read_operation(self.root), self.operation)

    def test_restart_schema_is_exact_and_does_not_invent_release_assets(self) -> None:
        operation = protocol.new_restart_operation("0.5.0", "b" * 64)
        protocol.write_operation(self.root, operation)
        self.assertEqual(protocol.read_operation(self.root), operation)
        for changes in (
            {"schema": 1}, {"kind": "install"}, {"phase": "installing"},
            {"target": {"version": "0.5.0", "releaseDigest": "c" * 64}},
            {"target": {**operation["target"], "command": "restart"}},
            {"target": TARGET},
        ):
            with self.subTest(changes=changes), self.assertRaises(protocol.UpdateProtocolError):
                protocol.write_operation(self.root, {**operation, **changes})

    def test_rejects_symlink_hardlink_public_and_oversized_state(self) -> None:
        path = self.root / "state/update.json"
        other = self.root / "outside"
        other.write_text("untouched")
        path.symlink_to(other)
        with self.assertRaises(protocol.UpdateProtocolError):
            protocol.write_operation(self.root, self.operation)
        with self.assertRaises(protocol.UpdateProtocolError):
            protocol.read_operation(self.root)
        self.assertEqual(other.read_text(), "untouched")
        path.unlink()
        protocol.write_operation(self.root, self.operation)
        os.link(path, self.root / "hardlink")
        with self.assertRaises(protocol.UpdateProtocolError):
            protocol.read_operation(self.root)
        (self.root / "hardlink").unlink()
        path.chmod(0o644)
        with self.assertRaises(protocol.UpdateProtocolError):
            protocol.read_operation(self.root)
        path.chmod(0o600)
        path.write_bytes(b" " * (protocol.MAX_OPERATION_BYTES + 1))
        with self.assertRaises(protocol.UpdateProtocolError):
            protocol.read_operation(self.root)
        for malformed in (b'{"schema":1,"schema":2}', b"[" * 1500 + b"]" * 1500):
            path.write_bytes(malformed)
            with self.assertRaises(protocol.UpdateProtocolError):
                protocol.read_operation(self.root)
        path.unlink()
        os.mkfifo(path, 0o600)
        with self.assertRaises(protocol.UpdateProtocolError):
            protocol.read_operation(self.root)

    def test_lock_requires_exact_held_descriptor_and_does_not_leak_to_children(self) -> None:
        with protocol.install_lock(self.root) as descriptor:
            os.set_inheritable(descriptor, True)
            protocol.validate_inherited_lock(self.root, descriptor)
            self.assertFalse(os.get_inheritable(descriptor))
            with self.assertRaises(BlockingIOError):
                protocol.acquire_install_lock(self.root)
            unrelated = os.open(self.root / "state/other.lock", os.O_RDWR | os.O_CREAT, 0o600)
            try:
                with self.assertRaises(protocol.UpdateProtocolError):
                    protocol.validate_inherited_lock(self.root, unrelated)
            finally:
                os.close(unrelated)
        descriptor = os.open(self.root / "state/.install.lock", os.O_RDWR)
        try:
            with self.assertRaises(protocol.UpdateProtocolError):
                protocol.validate_inherited_lock(self.root, descriptor)
        finally:
            os.close(descriptor)


class UpdateWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / ".netizen"
        (self.root / "state").mkdir(parents=True, mode=0o700)
        previous = self.root / "releases" / ("b" * 64)
        previous.mkdir(parents=True)
        (self.root / "current").symlink_to(previous)
        self.operation = protocol.new_operation(TARGET, previous.name)
        protocol.write_operation(self.root, self.operation)
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def _run(self, *, installer_phase: str | None = None, code: str = "none",
             exit_code: int = 0, bootstrap: bytes = BOOTSTRAP) -> int:
        def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            self.calls.append((command, kwargs))
            if command[0] == "curl":
                Path(command[command.index("-o") + 1]).write_bytes(bootstrap)
                self.assertEqual((self.root / "current").resolve().name, "b" * 64)
                return subprocess.CompletedProcess(command, 0)
            self.assertEqual(command[0], "/bin/sh")
            self.assertEqual(len(command), 2)
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            descriptor = kwargs["pass_fds"][0]
            protocol.validate_inherited_lock(self.root, descriptor)
            with self.assertRaises(BlockingIOError):
                protocol.acquire_install_lock(self.root)
            environment = kwargs["env"]
            self.assertEqual(environment[protocol.ENV_VERSION], TARGET["version"])
            self.assertEqual(environment[protocol.ENV_ARCHIVE_SHA256], TARGET["archiveSha256"])
            self.assertEqual(environment[protocol.ENV_LOCK_FD], str(descriptor))
            if installer_phase:
                protocol.advance_operation(self.root, self.operation["operationId"], installer_phase, code)
            return subprocess.CompletedProcess(command, exit_code)

        return updater.run_update(self.operation["operationId"], product_root=self.root,
                                  runner=runner, environment_loader=lambda: {"PATH": "/usr/bin:/bin"})

    def test_download_is_exact_and_zero_exit_is_authoritative_success(self) -> None:
        self.assertEqual(self._run(), 0)
        self.assertEqual(self.calls[0][0][-1],
                         "https://github.com/lijingda/netizen/releases/download/v0.5.0/install.sh")
        self.assertEqual(protocol.read_operation(self.root)["phase"], "succeeded")
        with protocol.install_lock(self.root):
            pass

    def test_wrong_installer_hash_never_executes(self) -> None:
        self.assertEqual(self._run(bootstrap=b"tampered"), 1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(protocol.read_operation(self.root)["code"], "installer_invalid")

    def test_exact_rollback_is_preserved_and_abrupt_activation_is_unknown(self) -> None:
        for phase, code, expected in (
            ("rolled_back", "activation_failed", "rolled_back"),
            ("requires_action", "permissions_required", "requires_action"),
            ("restarting", "none", "recovery_required"),
            ("preparing", "none", "failed"),
        ):
            with self.subTest(phase=phase):
                protocol.write_operation(self.root, self.operation)
                self.assertEqual(self._run(installer_phase=phase, code=code, exit_code=1), 1)
                self.assertEqual(protocol.read_operation(self.root)["phase"], expected)

    def test_zero_exit_does_not_overwrite_a_contradictory_typed_failure(self) -> None:
        self.assertEqual(self._run(installer_phase="recovery_required", code="rollback_incomplete"), 1)
        self.assertEqual(protocol.read_operation(self.root)["phase"], "recovery_required")

    def test_nonzero_bootstrap_exit_invalidates_premature_success(self) -> None:
        self.assertEqual(self._run(installer_phase="succeeded", exit_code=1), 1)
        operation = protocol.read_operation(self.root)
        self.assertEqual(operation["phase"], "recovery_required")
        self.assertEqual(operation["code"], "installer_failed")

    def test_success_waits_for_real_outer_bootstrap_exit(self) -> None:
        # The fixture invokes the real installer reporting context, then keeps
        # its enclosing /bin/sh process alive before returning to the worker.
        fixture = self.root / "report_then_wait.py"
        fixture.write_text(
            "import os, sys, time\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, os.environ['TEST_REPOSITORY'])\n"
            "from scripts.netizen_installer import InstallerUpdate, _report_install_update\n"
            "root = Path(os.environ['TEST_PRODUCT_ROOT'])\n"
            "update = InstallerUpdate(root, os.environ['NETIZEN_UPDATE_OPERATION_ID'], "
            "int(os.environ['NETIZEN_UPDATE_LOCK_FD']))\n"
            "with _report_install_update(update):\n"
            "    update.report('restarting')\n"
            "(root / 'installer-returned').touch()\n"
            "deadline = time.monotonic() + 10\n"
            "while not (root / 'allow-exit').exists():\n"
            "    if time.monotonic() >= deadline: raise SystemExit(99)\n"
            "    time.sleep(0.01)\n",
            encoding="utf-8",
        )
        bootstrap = b'#!/bin/sh\n"$TEST_PYTHON" -E -B "$TEST_FIXTURE"\n'
        operation = {**self.operation, "target": {
            **self.operation["target"], "installerSha256": hashlib.sha256(bootstrap).hexdigest(),
        }}
        protocol.write_operation(self.root, operation)

        def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            if command[0] == "curl":
                Path(command[command.index("-o") + 1]).write_bytes(bootstrap)
                return subprocess.CompletedProcess(command, 0)
            return subprocess.run(command, **kwargs)

        result: list[int] = []
        thread = threading.Thread(target=lambda: result.append(updater.run_update(
            operation["operationId"], product_root=self.root, runner=runner,
            environment_loader=lambda: {
                "TEST_PYTHON": sys.executable, "TEST_FIXTURE": str(fixture),
                "TEST_PRODUCT_ROOT": str(self.root),
                "TEST_REPOSITORY": str(Path(__file__).resolve().parents[1]),
            },
        )))
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not (self.root / "installer-returned").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue((self.root / "installer-returned").exists())
            self.assertTrue(thread.is_alive())
            phase = protocol.read_operation(self.root)["phase"]
            self.assertEqual(phase, "restarting")
            self.assertFalse(protocol.terminal_phase(phase))
        finally:
            (self.root / "allow-exit").touch()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [0])
        self.assertEqual(protocol.read_operation(self.root)["phase"], "succeeded")

    def test_stale_operation_or_changed_current_never_downloads(self) -> None:
        operation = {**self.operation, "phase": "downloading"}
        protocol.write_operation(self.root, operation)
        self.assertEqual(self._run(), 1)
        self.assertEqual(self.calls, [])
        protocol.write_operation(self.root, {**self.operation, "previousRelease": "c" * 64})
        self.assertEqual(self._run(), 1)
        self.assertEqual(self.calls, [])
        self.assertEqual(protocol.read_operation(self.root)["code"], "previous_release_changed")

    def test_lock_handoff_timeout_does_not_mutate_another_installation(self) -> None:
        with protocol.install_lock(self.root):
            result = updater.run_update(self.operation["operationId"], product_root=self.root,
                                        lock_timeout=0, environment_loader=MagicMock())
        self.assertEqual(result, 1)
        self.assertEqual(protocol.read_operation(self.root), self.operation)

    def test_profile_environment_preserves_native_settings_and_scrubs_service_identity(self) -> None:
        with patch.object(updater, "capture_profile_environment", return_value={
            "PATH": "/account/bin", "CODEX_HOME": "/account/codex", "HTTPS_PROXY": "https://proxy",
            "NETIZEN_READY_FILE": "/old/ready", "NETIZEN_LIFETIME_LOCK_FD": "5",
            "NETIZEN_UPDATE_OPERATION_ID": "stale", "FEISHU_APP_SECRET": "secret",
            "PYTHONPATH": "/old/python",
        }) as capture:
            environment = updater._worker_environment()
        capture.assert_called_once()
        self.assertEqual(environment["CODEX_HOME"], "/account/codex")
        self.assertEqual(environment["PATH"], "/account/bin")
        self.assertEqual(environment["HTTPS_PROXY"], "https://proxy")
        self.assertFalse(any(key.startswith("NETIZEN_") for key in environment))
        self.assertNotIn("FEISHU_APP_SECRET", environment)
        self.assertNotIn("PYTHONPATH", environment)


class RestartWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / ".netizen"
        (self.root / "state").mkdir(parents=True, mode=0o700)
        self.release = self.root / "releases" / ("b" * 64)
        (self.release / "source").mkdir(parents=True)
        (self.root / "current").symlink_to(self.release)
        self.service = self.release / "source/service.sh"
        self.service.write_text("#!/bin/sh\nexit 0\n")
        self.operation = protocol.new_restart_operation("0.5.0", self.release.name)
        protocol.write_operation(self.root, self.operation)

    def run_restart(self, runner):
        return updater.run_update(
            self.operation["operationId"], product_root=self.root, runner=runner,
            environment_loader=lambda: {"PATH": "/usr/bin:/bin"},
        )

    def test_exact_restart_retains_lock_without_downloading_or_inheriting_it(self) -> None:
        def restart(command, **kwargs):
            self.assertEqual(command, ["/bin/sh", str(self.service), "restart"])
            self.assertTrue(kwargs["close_fds"])
            self.assertNotIn("pass_fds", kwargs)
            self.assertFalse(any(key.startswith("NETIZEN_UPDATE_") for key in kwargs["env"]))
            with self.assertRaises(BlockingIOError):
                protocol.acquire_install_lock(self.root)
            self.assertEqual(protocol.read_operation(self.root)["phase"], "restarting")
            return subprocess.CompletedProcess(command, 0)
        self.assertEqual(self.run_restart(restart), 0)
        self.assertEqual(protocol.read_operation(self.root)["phase"], "succeeded")
        self.assertEqual((self.root / "current").resolve(), self.release)
        with protocol.install_lock(self.root):
            pass

    def test_failure_timeout_and_changed_pointer_never_claim_success(self) -> None:
        for failure in (1, subprocess.TimeoutExpired("restart", 180), OSError("SECRET"), "changed"):
            with self.subTest(failure=failure):
                protocol.write_operation(self.root, self.operation)
                def restart(command, **kwargs):
                    if isinstance(failure, Exception):
                        raise failure
                    if failure == "changed":
                        (self.root / "current").unlink()
                        (self.root / "current").symlink_to("missing")
                        return subprocess.CompletedProcess(command, 0)
                    return subprocess.CompletedProcess(command, failure)
                self.assertEqual(self.run_restart(restart), 1)
                result = protocol.read_operation(self.root)
                self.assertEqual(result["phase"], "recovery_required")
                self.assertNotIn("SECRET", str(result))

    def test_pending_activation_stale_claim_and_changed_release_do_not_restart(self) -> None:
        runner = MagicMock()
        intent = self.root / "state/.activation-intent.json"
        intent.touch()
        self.assertEqual(self.run_restart(runner), 1)
        self.assertEqual(protocol.read_operation(self.root)["phase"], "recovery_required")
        intent.unlink()
        # A late worker cannot claim an operation already reconciled by Admin.
        self.assertEqual(self.run_restart(runner), 1)
        protocol.write_operation(self.root, self.operation)
        (self.root / "current").unlink()
        self.assertEqual(self.run_restart(runner), 1)
        runner.assert_not_called()

    def test_service_path_must_remain_in_exact_physical_release(self) -> None:
        self.service.unlink()
        self.service.symlink_to("/bin/sh")
        runner = MagicMock()
        self.assertEqual(self.run_restart(runner), 1)
        runner.assert_not_called()
        self.assertEqual(protocol.read_operation(self.root)["phase"], "failed")

    def test_real_child_observes_pending_result_and_held_install_lock(self) -> None:
        fixture = self.root / "restart_fixture.py"
        fixture.write_text(
            "import sys\nfrom pathlib import Path\n"
            f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n"
            "from netizen.deployment.update_protocol import acquire_install_lock, read_operation\n"
            f"root = Path({str(self.root)!r})\n"
            "assert read_operation(root)['phase'] == 'restarting'\n"
            "try:\n    acquire_install_lock(root)\nexcept BlockingIOError:\n    pass\n"
            "else:\n    raise SystemExit(98)\n",
        )
        self.service.write_text(
            f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -E -B {shlex.quote(str(fixture))}\n"
        )
        self.assertEqual(self.run_restart(subprocess.run), 0)
        self.assertEqual(protocol.read_operation(self.root)["phase"], "succeeded")
        with protocol.install_lock(self.root):
            pass


class InstallerUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name) / "home"
        home.mkdir()
        self.layout = installer.resolve_layout(environ={}, account_home=home, uid=os.geteuid(),
                                                username="test-user", platform_name="linux")
        installer.prepare_directories(self.layout)
        self.operation = protocol.new_operation(TARGET, "b" * 64)
        self.operation["phase"] = "downloading"
        protocol.write_operation(self.layout.product_root, self.operation)
        self.manifest = installer.PublishedReleaseManifest("0.5.0", "f" * 40, "a" * 64, "e" * 64)

    def _environment(self, descriptor: int) -> dict[str, str]:
        return {protocol.ENV_OPERATION_ID: self.operation["operationId"],
                protocol.ENV_LOCK_FD: str(descriptor), protocol.ENV_VERSION: "0.5.0",
                protocol.ENV_ARCHIVE_SHA256: "a" * 64}

    def test_handoff_requires_exact_target_and_held_lock(self) -> None:
        with protocol.install_lock(self.layout.product_root) as descriptor:
            environment = self._environment(descriptor)
            with patch.dict(os.environ, environment):
                update = installer._installer_update(self.layout, self.manifest)
                self.assertEqual(update.operation_id, self.operation["operationId"])
                self.assertFalse(os.get_inheritable(descriptor))
            environment[protocol.ENV_VERSION] = "9.9.9"
            with patch.dict(os.environ, environment), self.assertRaises(installer.InstallError):
                installer._installer_update(self.layout, self.manifest)

    def test_preparation_failures_are_typed_and_never_claim_rollback(self) -> None:
        for exception, phase, code in (
            (installer.ConfigurationRequired("credentials"), "requires_action", "configuration_required"),
            (installer.FeishuPermissionsRequired("scopes"), "requires_action", "permissions_required"),
            (installer.InstallError("validation"), "failed", "preparation_failed"),
        ):
            with self.subTest(code=code), protocol.install_lock(self.layout.product_root) as descriptor:
                protocol.write_operation(self.layout.product_root, self.operation)
                update = installer.InstallerUpdate(self.layout.product_root, self.operation["operationId"], descriptor)
                with self.assertRaises(type(exception)), installer._report_install_update(update):
                    raise exception
                result = protocol.read_operation(self.layout.product_root)
                self.assertEqual((result["phase"], result["code"]), (phase, code))

    def test_real_activation_records_complete_and_incomplete_rollback_separately(self) -> None:
        for stop_failed in (False, True):
            with self.subTest(stop_failed=stop_failed):
                old_root = self.layout.releases / ("b" * 64)
                old_root.mkdir(exist_ok=True)
                candidate_root = self.layout.releases / ("c" * 64)
                candidate_root.mkdir(exist_ok=True)
                candidate = installer.Release(candidate_root.name, candidate_root,
                                              candidate_root / "source", candidate_root / "venv")
                installer._set_release_link(self.layout.current, old_root, self.layout)
                protocol.write_operation(self.layout.product_root, self.operation)
                backend = MagicMock()
                backend.capture_definition.return_value = installer.FileSnapshot(True, b"old")
                backend.inspect_state.return_value = installer.ServiceState(True, True)
                backend.inspect_legacy.return_value = installer.LegacyServiceState()
                backend.render_definition.return_value = b"new"
                backend.start_and_wait.side_effect = [installer.InstallError("start failed"), None]
                if stop_failed:
                    backend.stop_and_confirm.side_effect = [None, installer.InstallError("stop failed")]
                with (
                    protocol.install_lock(self.layout.product_root) as descriptor,
                    patch.object(installer, "_service_backend", return_value=backend),
                    patch.object(installer, "install_user_guide_skill"),
                ):
                    update = installer.InstallerUpdate(self.layout.product_root, self.operation["operationId"], descriptor)
                    with self.assertRaises(installer.InstallError):
                        installer.activate_release(candidate, self.layout, interactive=False, update=update)
                result = protocol.read_operation(self.layout.product_root)
                self.assertEqual(result["phase"], "recovery_required" if stop_failed else "rolled_back")
                self.assertEqual(result["code"], "rollback_incomplete" if stop_failed else "activation_failed")
                self.assertEqual(backend.start_and_wait.call_count, 1 if stop_failed else 2)

    def test_subprocess_environment_never_carries_update_or_service_lock_identity(self) -> None:
        with patch.dict(os.environ, {**self._environment(3), "NETIZEN_LIFETIME_LOCK_FD": "4",
                                     "NETIZEN_READY_FILE": "/main/ready"}):
            environment = installer._clean_subprocess_environment()
        for key in self._environment(3):
            self.assertNotIn(key, environment)
        self.assertNotIn("NETIZEN_LIFETIME_LOCK_FD", environment)
        self.assertNotIn("NETIZEN_READY_FILE", environment)

    def test_only_successful_cli_activation_resolves_prior_unknown_without_rewriting_target(self) -> None:
        candidate = installer.Release("c" * 64, self.layout.releases / ("c" * 64),
                                      self.layout.releases / ("c" * 64) / "source", Path("/candidate/venv"))
        validation = installer.RuntimeValidation(self.layout.state_dir, installer.AdminBind(False, "127.0.0.1", 8787))
        for failure in (True, False):
            with self.subTest(failure=failure):
                operation = {**self.operation, "phase": "recovery_required", "code": "worker_lost"}
                protocol.write_operation(self.layout.product_root, operation)
                with (
                    patch.object(installer, "require_supported_platform"),
                    patch.object(installer, "_service_backend"),
                    patch.object(installer, "prepare_configuration"),
                    patch.object(installer, "require_codex_login"),
                    patch.object(installer, "validate_runtime", return_value=validation),
                    patch.object(installer, "require_feishu_permissions"),
                    patch.object(installer, "activate_release", side_effect=installer.InstallError("failed") if failure else None),
                ):
                    def run() -> None:
                        installer._install(source_root=Path(__file__).resolve().parents[1],
                                           prepare_candidate=MagicMock(return_value=candidate),
                                           rerun_instruction="rerun installer", layout=self.layout,
                                           runner=None, interactive=False)
                    if failure:
                        with self.assertRaises(installer.InstallError):
                            run()
                    else:
                        run()
                result = protocol.read_operation(self.layout.product_root)
                self.assertEqual(result["phase"], "recovery_required" if failure else "recovered")
                self.assertEqual(result["target"], self.operation["target"])
                self.assertEqual(result["operationId"], self.operation["operationId"])

    def test_admin_install_uses_shared_orchestration_without_second_lock_or_browser(self) -> None:
        candidate = installer.Release("c" * 64, self.layout.releases / ("c" * 64),
                                      self.layout.releases / ("c" * 64) / "source", Path("/candidate/venv"))
        validation = installer.RuntimeValidation(self.layout.state_dir, installer.AdminBind(False, "127.0.0.1", 8787))
        with (
            protocol.install_lock(self.layout.product_root) as descriptor,
            patch.dict(os.environ, self._environment(descriptor)),
            patch.object(installer, "read_published_release_manifest", return_value=self.manifest),
            patch.object(installer, "require_supported_platform"),
            patch.object(installer, "_service_backend"),
            patch.object(installer, "prepare_configuration"),
            patch.object(installer, "prepare_published_release", return_value=candidate),
            patch.object(installer, "require_codex_login"),
            patch.object(installer, "validate_runtime", return_value=validation),
            patch.object(installer, "require_feishu_permissions") as permissions,
            patch.object(installer, "activate_release") as activate,
            patch.object(installer, "installation_lock") as second_lock,
        ):
            installer.install_published(source_root=Path(__file__).resolve().parents[1],
                                        layout=self.layout, interactive=True)
            permissions.assert_called_once()
            self.assertFalse(permissions.call_args.kwargs["repair_existing_app"])
            self.assertFalse(activate.call_args.kwargs["interactive"])
            self.assertIsInstance(activate.call_args.kwargs["update"], installer.InstallerUpdate)
            second_lock.assert_not_called()
        self.assertEqual(protocol.read_operation(self.layout.product_root)["phase"], "preparing")


if __name__ == "__main__":
    unittest.main()
