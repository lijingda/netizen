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

    def test_doctor_without_root_lists_all_environments_and_stopped_instances(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            environments = [directory / name for name in ("current", "other")]
            statuses = []
            for index, prefix in enumerate(environments):
                (prefix / "bin").mkdir(parents=True)
                python = prefix / "bin/python"
                python.symlink_to(sys.executable)
                binding = ServiceBinding(directory / f"instance-{index}", python, prefix)
                statuses.append(ServiceStatus(binding, index == 0, True, index == 0, True))
            alias = directory / "environment-alias"
            alias.symlink_to(environments[0], target_is_directory=True)
            manager = Mock()
            manager.list_instances.return_value = statuses
            with patch("netizen_cli.cli_services.ServiceManager", return_value=manager), \
                 patch("sys.prefix", str(alias)), \
                 patch.dict(os.environ, {"NETIZEN_ROOT": str(statuses[0].binding.root)}), \
                 patch("netizen_cli.cli.resolve_instance_root") as resolve:
                code, output, _ = self.invoke(["doctor", "--json"])
            self.assertEqual(code, 0)
            result = json.loads(output)
            self.assertEqual(result["count"], 2)
            self.assertEqual(result["cli"]["prefix"], str(alias))
            self.assertEqual([row["root"] for row in result["instances"]],
                             [str(status.binding.root) for status in statuses])
            self.assertEqual([row["current_environment"] for row in result["instances"]], [True, False])
            self.assertEqual([row["running"] for row in result["instances"]], [True, False])
            self.assertEqual([row["ready"] for row in result["instances"]], [True, False])
            self.assertTrue(all(row["bound"] for row in result["instances"]))
            self.assertEqual(manager.method_calls, [call.list_instances()])
            resolve.assert_not_called()

    def test_doctor_empty_inventory_ignores_invalid_environment_root(self):
        manager = Mock()
        manager.list_instances.return_value = []
        with patch("netizen_cli.cli_services.ServiceManager", return_value=manager), \
             patch.dict(os.environ, {"NETIZEN_ROOT": "/"}), \
             patch("netizen_cli.cli_data.ensure_instance_root") as create, \
             patch("netizen_cli.cli_data.root_maintenance_lock") as lock:
            code, output, _ = self.invoke(["--json", "doctor"])
        self.assertEqual(code, 0)
        result = json.loads(output)
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["instances"], [])
        self.assertEqual(result["cli"]["python"], os.path.abspath(sys.executable))
        self.assertEqual(manager.method_calls, [call.list_instances()])
        create.assert_not_called()
        lock.assert_not_called()

    def test_doctor_explicit_root_keeps_single_instance_output_in_both_positions(self):
        root = Path("/tmp/cli-doctor").resolve()
        status = ServiceStatus(cli._current_binding(root), True, True, True)
        for arguments in (["doctor", "--root", str(root), "--json"],
                          ["--root", str(root), "--json", "doctor"]):
            for observed in (status, None):
                with self.subTest(arguments=arguments, bound=observed is not None):
                    manager = Mock()
                    manager.inspect.return_value = observed
                    with patch("netizen_cli.cli_services.ServiceManager", return_value=manager), \
                         patch.dict(os.environ, {"NETIZEN_ROOT": "/tmp/other-instance"}):
                        code, output, _ = self.invoke(arguments)
                    self.assertEqual(code, 0)
                    result = json.loads(output)
                    self.assertEqual(result["root"], str(root))
                    self.assertEqual(result["bound"], observed is not None)
                    self.assertEqual(result["cli"]["version"], cli.__version__)
                    self.assertNotIn("instances", result)
                    self.assertEqual(manager.method_calls, [call.inspect(root)])

    def test_doctor_inventory_failure_is_not_reported_as_an_empty_success(self):
        manager = Mock()
        manager.list_instances.side_effect = RuntimeError("unrecognized service definition")
        with patch("netizen_cli.cli_services.ServiceManager", return_value=manager):
            code, output, _ = self.invoke(["doctor", "--json"])
        self.assertEqual(code, 1)
        result = json.loads(output)
        self.assertEqual(result["status"], "failed")
        self.assertIn("unrecognized service definition", result["reason"])
        self.assertNotIn("instances", result)
        self.assertEqual(manager.method_calls, [call.list_instances()])

    def test_status_without_root_still_uses_environment_root(self):
        root = Path("/tmp/cli-status-default").resolve()
        manager = Mock()
        manager.inspect.return_value = None
        with patch("netizen_cli.cli_services.ServiceManager", return_value=manager), \
             patch.dict(os.environ, {"NETIZEN_ROOT": str(root)}):
            code, output, _ = self.invoke(["status", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["root"], str(root))
        self.assertEqual(manager.method_calls, [call.inspect(root)])

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
