from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, call, patch

from netizen_cli import cli
from netizen_cli.cli_services import ServiceBinding, ServiceStatus


class CliTest(unittest.TestCase):
    def invoke(self, arguments, *, pending_restart=None):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), \
             patch("netizen_cli.deployment.restart_worker.assert_no_pending_restart", side_effect=pending_restart), \
             patch("netizen_cli.deployment.restart_worker.finish_manual_restart", return_value=None):
            code = cli.main(arguments)
        return code, output.getvalue(), errors.getvalue()

    def test_help_and_version_do_not_import_runtime(self):
        command = "import sys; from netizen_cli.cli import main; main([]); assert 'netizen_cli.main' not in sys.modules; assert 'codex' not in sys.modules"
        result = subprocess.run([sys.executable, "-c", command], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("update", result.stdout)
        with self.assertRaises(SystemExit) as stopped:
            self.invoke(["--version"])
        self.assertEqual(stopped.exception.code, 0)

    def test_update_rejects_root_before_any_work_in_both_positions(self):
        for arguments in (["--root", "/tmp/x", "update"], ["update", "--root", "/tmp/x"]):
            with self.subTest(arguments=arguments), patch("netizen_cli.instance.resolve_instance_root") as resolve:
                with self.assertRaises(SystemExit) as stopped:
                    self.invoke(arguments)
                self.assertEqual(stopped.exception.code, 2)
                resolve.assert_not_called()

    def test_cross_environment_start_uses_existing_binding_without_data_preflight(self):
        root = Path("/tmp/cli-instance-a").resolve()
        binding = ServiceBinding(root, Path("/env-a/bin/python"), Path("/env-a"))
        status = ServiceStatus(binding, True, True, True)
        manager = Mock()
        manager.inspect.return_value = status
        manager.start.return_value = status
        with patch("netizen_cli.cli_services.ServiceManager", return_value=manager), \
             patch("netizen_cli.cli_data.root_maintenance_lock", return_value=contextlib.nullcontext()), \
             patch("netizen_cli.cli_data.validate_prepared_instance") as validate:
            code, output, _ = self.invoke(["start", "--root", str(root), "--json"])
        self.assertEqual(code, 0)
        manager.register.assert_not_called()
        validate.assert_not_called()
        self.assertEqual(json.loads(output)["instance"]["binding"]["python"], "/env-a/bin/python")

    def test_retained_unbound_start_registers_current_environment(self):
        root = Path("/tmp/cli-instance-retained").resolve()
        manager = Mock()
        manager.inspect.return_value = None
        manager.start.return_value = ServiceStatus(cli._current_binding(root), True, True, True)
        with patch("netizen_cli.cli_services.ServiceManager", return_value=manager), \
             patch("netizen_cli.cli_data.root_maintenance_lock", return_value=contextlib.nullcontext()), \
             patch("netizen_cli.cli_data.instance_lifetime_lock", return_value=contextlib.nullcontext(9)), \
             patch("netizen_cli.cli_data.validate_prepared_instance") as validate:
            code, _, _ = self.invoke(["start", "--root", str(root)])
        self.assertEqual(code, 0)
        validate.assert_called_once_with(root)
        manager.register.assert_called_once_with(cli._current_binding(root), lifetime_descriptor=9)
        manager.start.assert_called_once_with(root)

    def test_missing_data_never_registers_or_initializes_on_start(self):
        manager = Mock()
        manager.inspect.return_value = None
        with patch("netizen_cli.cli_services.ServiceManager", return_value=manager), \
             patch("netizen_cli.cli_data.root_maintenance_lock", return_value=contextlib.nullcontext()), \
             patch("netizen_cli.cli_data.instance_lifetime_lock", return_value=contextlib.nullcontext(9)), \
             patch("netizen_cli.cli_data.validate_prepared_instance", side_effect=RuntimeError("missing database")), \
             patch("netizen_cli.cli_data.initialize_instance_data") as initialize:
            code, output, _ = self.invoke(["start", "--root", "/tmp/cli-lost", "--json"])
        self.assertEqual(code, 1)
        self.assertIn("missing database", output)
        manager.register.assert_not_called()
        manager.start.assert_not_called()
        initialize.assert_not_called()

    def test_remove_without_confirmation_is_side_effect_free(self):
        manager = Mock()
        root = Path("/tmp/cli-remove").resolve()
        manager.inspect.return_value = ServiceStatus(cli._current_binding(root), True, True, True)
        with patch("netizen_cli.cli_services.ServiceManager", return_value=manager), \
             patch("netizen_cli.cli_data.root_maintenance_lock", return_value=contextlib.nullcontext()), \
             patch("sys.stdin", io.StringIO()):
            code, output, errors = self.invoke(["remove", "--root", str(root), "--json"])
        self.assertEqual(code, 1)
        self.assertIn("confirmation required", output)
        self.assertIn(str(root), errors)
        manager.stop.assert_not_called()
        manager.remove.assert_not_called()

    def test_yes_does_not_bypass_failed_stop_or_purge(self):
        root = Path("/tmp/cli-stop-fail").resolve()
        manager = Mock()
        manager.inspect.return_value = ServiceStatus(cli._current_binding(root), True, True, True)
        manager.stop.side_effect = RuntimeError("stop timed out")
        with patch("netizen_cli.cli_services.ServiceManager", return_value=manager), \
             patch("netizen_cli.cli_data.root_maintenance_lock", return_value=contextlib.nullcontext()), \
             patch("netizen_cli.cli_data.purge_inventory", return_value=(root / "config.yaml",)), \
             patch("netizen_cli.cli_data.purge_instance_data") as purge:
            code, output, errors = self.invoke(["remove", "--root", str(root), "--purge", "-y", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["phase"], "stop")
        self.assertIn(str(root / "config.yaml"), errors)
        manager.remove.assert_not_called()
        purge.assert_not_called()

    def test_existing_setup_does_not_rebind_or_initialize(self):
        root = Path("/tmp/cli-existing-setup").resolve()
        binding = ServiceBinding(root, Path("/env-a/bin/python"), Path("/env-a"))
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                manager = Mock()
                manager.inspect.return_value = ServiceStatus(binding, False, enabled, False)
                with patch("netizen_cli.cli_services.ServiceManager", return_value=manager), \
                     patch("netizen_cli.cli_setup.require_codex_login") as login, \
                     patch("netizen_cli.cli_setup.prepare_configuration") as configure, \
                     patch("netizen_cli.cli_data.initialize_instance_data") as initialize:
                    code, output, _ = self.invoke(["setup", "--root", str(root), "--json"])
                self.assertEqual(code, 0)
                result = json.loads(output)
                self.assertEqual(result["status"], "already_registered")
                self.assertEqual(result["instance"]["enabled"], enabled)
                self.assertEqual(result["instance"]["binding"]["python"], "/env-a/bin/python")
                self.assertTrue(result["recommendation"])
                login.assert_not_called()
                configure.assert_not_called()
                initialize.assert_not_called()
                self.assertEqual(manager.method_calls, [call.inspect(root)])

    def test_status_is_read_only(self):
        manager = Mock()
        manager.inspect.return_value = None
        with patch("netizen_cli.cli_services.ServiceManager", return_value=manager), \
             patch("netizen_cli.cli_data.ensure_instance_root") as create, \
             patch("netizen_cli.cli_data.root_maintenance_lock") as lock:
            code, output, _ = self.invoke(["status", "--root", "/tmp/no-instance", "--json"])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(output)["bound"])
        create.assert_not_called()
        lock.assert_not_called()

    def test_pending_admin_restart_blocks_mutations_before_service_or_data_changes(self):
        for command in ("start", "stop", "restart", "remove"):
            with self.subTest(command=command):
                manager = Mock()
                with patch("netizen_cli.cli_services.ServiceManager", return_value=manager), \
                     patch("netizen_cli.cli_data.root_maintenance_lock", return_value=contextlib.nullcontext(9)), \
                     patch("netizen_cli.cli_data.purge_inventory") as inventory:
                    code, output, _ = self.invoke(
                        [command, "--root", "/tmp/cli-pending", "--json"],
                        pending_restart=RuntimeError("Admin restart is pending dispatch"))
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(output)["phase"], "preflight")
                manager.inspect.assert_not_called()
                manager.stop.assert_not_called()
                manager.start.assert_not_called()
                manager.remove.assert_not_called()
                inventory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
