"""CLI lifecycle integration: real data/locks/definitions, synthetic managers.

Only external login/onboarding and operating-system process management are fake.
No network requests, user services, shell startup files, or real instances run.
"""
from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from netizen_cli import cli
from netizen_cli.cli_data import prepare_instance
from netizen_cli.cli_services import ServiceBinding, ServiceManager
from tests.test_cli_services import ManagerDouble


class RuntimeDouble(ManagerDouble):
    """The fake manager launches a lock-owning runtime with real startup checks."""
    validate_database = True

    def start(self, name: str) -> None:
        super().start(name)
        if self.validate_database:
            try:
                prepare_instance(self.binding(name).root,
                                 lifetime_descriptor=self.descriptors[name])
            except BaseException:
                self.stop(name)
                raise


class LinuxCliIntegrationTest(unittest.TestCase):
    platform = "linux"

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / "work/.netizen"
        self.home = self.directory / "home"
        self.home.mkdir()
        self.environments = []
        for name in ("A", "B"):
            prefix = self.directory / name
            (prefix / "bin").mkdir(parents=True)
            python = prefix / "bin/python"
            python.symlink_to(sys.executable)
            self.environments.append(ServiceBinding(self.root, python, prefix))
        self.current = self.environments[0]
        self.native = RuntimeDouble()
        self.manager = ServiceManager(home=self.home, platform=self.platform, runner=self.native)
        self.native.manager = self.manager
        self.addCleanup(lambda: [os.close(fd) for fd in self.native.descriptors.values()])

    def invoke(self, command: str, *arguments: str):
        stdout, stderr = io.StringIO(), io.StringIO()

        def helper(module: str, _arguments: list[str], **_: object):
            if module == "netizen_cli.feishu_app_onboarding":
                if getattr(self, "onboarding_error", None) is not None:
                    raise self.onboarding_error
                return {"version": 1, "appId": "cli_testapp", "appSecret": "not-a-real-secret"}
            if module == "netizen_cli.feishu_app_permissions":
                return {"version": 1, "missingScopes": getattr(self, "missing_scopes", [])}
            raise AssertionError(f"unexpected external setup helper: {module}")

        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                patch("netizen_cli.cli_services.ServiceManager", return_value=self.manager), \
                patch("netizen_cli.cli._current_binding", side_effect=lambda root: self.current), \
                patch("netizen_cli.cli_setup.require_codex_login"), \
                patch("netizen_cli.cli_setup._helper", side_effect=helper), \
                patch("netizen_cli.deployment.restart_worker.finish_manual_restart", return_value=None), \
                patch("sys.stdin", io.StringIO()):
            code = cli.main([command, "--root", str(self.root), "--json", *arguments])
        return code, json.loads(stdout.getvalue()), stderr.getvalue()

    def setup_instance(self) -> None:
        code, report, _ = self.invoke("setup")
        self.assertEqual(code, 0, report)
        self.assertEqual(report["status"], "prepared")

    def test_failed_authorization_setup_can_purge_its_exact_saved_credentials(self) -> None:
        self.missing_scopes = ["im:message"]
        code, report, _ = self.invoke("setup")
        self.assertEqual(code, 1, report)
        self.assertEqual(report["phase"], "prepare_configuration")
        profile = self.root / "lark-app/config.json"
        self.assertTrue(profile.exists())
        self.assertFalse((self.root / "state/channel.sqlite3").exists())
        foreign = self.root / "user-notes.txt"
        foreign.write_text("keep this")
        code, report, _ = self.invoke("remove", "--purge", "-y")
        self.assertEqual(code, 0, report)
        self.assertIn(str(profile), report["deleted"])
        self.assertFalse(profile.exists())
        self.assertEqual(foreign.read_text(), "keep this")

    def test_cancelled_browser_setup_can_be_cleaned_without_finishing_authorization(self) -> None:
        self.onboarding_error = KeyboardInterrupt()
        code, report, _ = self.invoke("setup")
        self.assertEqual(code, 130, report)
        self.assertEqual(report["phase"], "prepare_configuration")
        code, report, _ = self.invoke("remove", "--purge", "-y")
        self.assertEqual(code, 0, report)
        self.assertFalse((self.root / "credentials/admin-web-secret").exists())

    def test_failed_start_after_registration_reports_the_confirmed_new_binding(self) -> None:
        self.setup_instance()
        code, report, _ = self.invoke("remove", "-y")
        self.assertEqual(code, 0, report)
        self.current = self.environments[1]
        with patch.object(self.manager, "start", side_effect=RuntimeError("ready not confirmed")):
            code, report, _ = self.invoke("start")
        self.assertEqual(code, 1, report)
        self.assertEqual(report["phase"], "start")
        self.assertEqual(report["registered_binding"]["python"], str(self.environments[1].python))
        self.assertEqual(self.manager.inspect(self.root).binding, self.environments[1])
        self.assertEqual(report["instance"], "unknown until ready is verified")

    def test_complete_setup_start_remove_preserve_then_start_in_new_environment(self) -> None:
        self.setup_instance()
        database = self.root / "state/channel.sqlite3"
        before = database.read_bytes()
        status = self.manager.inspect(self.root)
        self.assertFalse(status.running)
        self.assertEqual(status.binding, self.environments[0])
        code, report, _ = self.invoke("start")
        self.assertEqual(code, 0, report)
        self.assertTrue(report["instance"]["ready"])
        self.assertEqual(database.read_bytes(), before)
        self.current = self.environments[1]
        code, report, scope = self.invoke("remove", "-y")
        self.assertEqual(code, 0, report)
        self.assertIn(str(self.root), scope)
        self.assertEqual(report["deleted"], [])
        self.assertEqual(database.read_bytes(), before)
        self.assertIsNone(self.manager.inspect(self.root))
        code, report, _ = self.invoke("start")
        self.assertEqual(code, 0, report)
        self.assertEqual(self.manager.inspect(self.root).binding, self.environments[1])
        self.assertTrue(report["instance"]["ready"])

    def test_other_environment_controls_binding_without_opening_newer_database(self) -> None:
        self.setup_instance()
        database = self.root / "state/channel.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE schema_version SET version=999")
        before = database.read_bytes()
        self.native.validate_database = False  # A is modeled as understanding its own newer schema.
        self.current = self.environments[1]
        for command in ("status", "start", "restart", "stop"):
            code, report, _ = self.invoke(command)
            self.assertEqual(code, 0, report)
            self.assertEqual(self.manager.inspect(self.root).binding, self.environments[0])
            self.assertEqual(database.read_bytes(), before)
        code, report, _ = self.invoke("remove", "-y")
        self.assertEqual(code, 0, report)
        code, report, _ = self.invoke("start")
        self.assertEqual(code, 1, report)
        self.assertIn("newer", report["reason"])
        self.assertIsNone(self.manager.inspect(self.root))
        self.assertEqual(database.read_bytes(), before)

    def test_missing_bound_python_requires_explicit_remove_before_replacement(self) -> None:
        self.setup_instance()
        self.environments[0].python.unlink()
        self.current = self.environments[1]
        code, report, _ = self.invoke("start")
        self.assertEqual(code, 1, report)
        self.assertIn("environment is missing", report["reason"])
        self.assertEqual(self.manager.inspect(self.root).binding, self.environments[0])
        code, report, _ = self.invoke("remove", "-y")
        self.assertEqual(code, 0, report)
        code, report, _ = self.invoke("start")
        self.assertEqual(code, 0, report)
        self.assertEqual(self.manager.inspect(self.root).binding, self.environments[1])

    def test_yes_purge_discloses_exact_files_preserves_foreign_content(self) -> None:
        self.setup_instance()
        code, report, _ = self.invoke("start")
        self.assertEqual(code, 0, report)
        note = self.root / "state/user-notes.txt"
        note.write_text("keep")
        project = self.root / "Projects/my-project"
        project.mkdir(parents=True)
        (project / "important.txt").write_text("keep project")
        code, report, scope = self.invoke("remove", "--purge", "-y")
        self.assertEqual(code, 0, report)
        self.assertFalse((self.root / "state/channel.sqlite3").exists())
        self.assertFalse((self.root / "config.yaml").exists())
        self.assertEqual(note.read_text(), "keep")
        self.assertEqual((project / "important.txt").read_text(), "keep project")
        self.assertTrue((self.root / ".netizen-root").exists())
        for deleted in report["deleted"]:
            self.assertIn(f"Delete: {deleted}", scope)
        self.assertNotIn("not-a-real-secret", scope)
        code, report, _ = self.invoke("start")
        self.assertEqual(code, 1, report)
        self.assertIn("not initialized", report["reason"])

    def test_without_yes_no_mutation_and_symlink_purge_rejected_before_stop(self) -> None:
        self.setup_instance()
        code, report, _ = self.invoke("start")
        self.assertEqual(code, 0, report)
        code, report, _ = self.invoke("remove", "--purge")
        self.assertEqual(code, 1, report)
        self.assertIn("confirmation required", report["reason"])
        self.assertTrue(self.manager.inspect(self.root).running)
        database = self.root / "state/channel.sqlite3"
        elsewhere = self.directory / "external.sqlite3"
        database.rename(elsewhere)
        database.symlink_to(elsewhere)
        before = elsewhere.read_bytes()
        code, report, _ = self.invoke("remove", "--purge", "-y")
        self.assertEqual(code, 1, report)
        self.assertEqual(report["phase"], "preflight")
        self.assertTrue(self.manager.inspect(self.root).running)
        self.assertEqual(elsewhere.read_bytes(), before)
        self.assertTrue(database.is_symlink())

    def test_explicit_root_setup_ignores_stale_instance_environment(self) -> None:
        stale = self.directory / "stale"
        with patch.dict(os.environ, {
            "NETIZEN_ROOT": str(stale), "NETIZEN_CONFIG_PATH": str(stale / "config.yaml"),
            "NETIZEN_ADMIN_SECRET_FILE": str(stale / "credentials/admin-web-secret"),
            "NETIZEN_LARK_APP_CONFIG": str(stale / "lark-app/config.json"),
        }):
            self.setup_instance()
        self.assertFalse(stale.exists())
        self.assertEqual(self.manager.inspect(self.root).binding.root, self.root)
        self.assertEqual(self.manager._environment(self.current)["NETIZEN_CONFIG_PATH"],
                         str(self.root / "config.yaml"))

    def test_missing_database_failure_preserves_existing_binding_and_reports_start_phase(self) -> None:
        self.setup_instance()
        (self.root / "state/channel.sqlite3").unlink()
        code, report, _ = self.invoke("start")
        self.assertEqual(code, 1, report)
        self.assertEqual(report["phase"], "start")
        self.assertIn("database is missing", report["reason"])
        self.assertEqual(self.manager.inspect(self.root).binding, self.current)
        self.assertFalse(self.manager.inspect(self.root).running)

    def test_generated_service_environment_reaches_launcher_with_real_lock_and_pinned_root(self) -> None:
        from netizen_cli import service_launcher

        self.setup_instance()
        environment = self.manager._environment(self.current)
        stale = self.directory / "stale-profile-instance"
        snapshot = {
            "PATH": "/some/other/python/bin:/usr/bin", "NETIZEN_ROOT": str(stale),
            "NETIZEN_CONFIG_PATH": str(stale / "config.yaml"),
            "NETIZEN_LARK_APP_CONFIG": str(stale / "lark-app/config.json"),
            "NETIZEN_READY_FILE": str(stale / "state/service.ready"),
            "NETIZEN_ADMIN_SECRET_FILE": str(stale / "credentials/admin-web-secret"),
            "PYTHONPATH": "/untrusted/pythonpath",
            "CODEX_HOME": str(self.directory / "shared-native-codex"),
        }

        class ReachedExec(Exception):
            pass

        captured = {}

        def exec_boundary(executable, arguments, exported):
            captured.update(executable=executable, arguments=arguments, environment=exported)
            descriptor = int(exported["NETIZEN_LIFETIME_LOCK_FD"])
            self.assertTrue(os.get_inheritable(descriptor))
            probe = os.open(self.root / "state/service.lifetime.lock", os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(probe)
            identity = json.loads((self.root / "state/service.identity.json").read_bytes())
            self.assertEqual(identity["root"], str(self.root))
            self.assertEqual(identity["prefix"], str(self.current.prefix))
            self.assertEqual(identity["python"], str(self.current.python))
            raise ReachedExec

        with patch.dict(os.environ, environment, clear=True), \
                patch.object(sys, "prefix", str(self.current.prefix)), \
                patch.object(sys, "executable", str(self.current.python)), \
                patch.object(service_launcher, "capture_profile_environment", return_value=snapshot), \
                patch.object(service_launcher.os, "execve", side_effect=exec_boundary), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(ReachedExec):
                service_launcher.launch(self.root)
        self.assertEqual(captured["executable"], str(self.current.python))
        self.assertEqual(captured["environment"]["NETIZEN_ROOT"], str(self.root))
        self.assertEqual(captured["environment"]["NETIZEN_CONFIG_PATH"], str(self.root / "config.yaml"))
        self.assertEqual(captured["environment"]["CODEX_HOME"], snapshot["CODEX_HOME"])
        self.assertNotIn("PYTHONPATH", captured["environment"])
        self.assertFalse(stale.exists())
        self.assertFalse((self.root / "state/service.identity.json").exists())

    def test_setup_sqlite_failure_has_structured_phase_report(self) -> None:
        with patch("netizen_cli.cli_data.BindingStore", side_effect=sqlite3.OperationalError("synthetic disk full")):
            code, report, _ = self.invoke("setup")
        self.assertEqual(code, 1, report)
        self.assertEqual(report["phase"], "initialize")
        self.assertEqual(report["completed"], 1)
        self.assertIn("database initialization failed", report["reason"])
        self.assertIsNone(self.manager.inspect(self.root))

    def test_cross_environment_purge_protects_bound_environments_native_state(self) -> None:
        original = self.current
        self.current = ServiceBinding(original.root, original.python, original.prefix,
                                      self.root / "credentials")
        self.setup_instance()
        self.current = self.environments[1]
        with patch.dict(os.environ, {"CODEX_HOME": str(self.directory / "B-codex-home")}):
            code, report, _ = self.invoke("remove", "--purge", "-y")
        self.assertEqual(code, 1, report)
        self.assertEqual(report["phase"], "preflight")
        self.assertIn("overlaps", report["reason"])
        self.assertIsNotNone(self.manager.inspect(self.root))
        self.assertTrue((self.root / "credentials/admin-web-secret").exists())

    def test_unregister_failure_after_unlink_reports_unknown_not_stale_bound_state(self) -> None:
        if self.platform != "linux":
            self.skipTest("systemd post-unlink daemon-reload failure")
        self.setup_instance()
        native_runner = self.manager._runner

        def fail_after_unlink(args, **kwargs):
            if (args[0] == "systemctl" and "daemon-reload" in args
                    and not self.manager.service_file(self.root).exists()):
                return subprocess.CompletedProcess(args, 1, "", "synthetic daemon-reload failure")
            return native_runner(args, **kwargs)

        self.manager._runner = fail_after_unlink
        code, report, _ = self.invoke("remove", "-y")
        self.assertEqual(code, 1, report)
        self.assertEqual(report["phase"], "unregister")
        self.assertFalse(self.manager.service_file(self.root).exists())
        self.assertIsInstance(report["instance"], str)
        self.assertIn("unknown", report["instance"])
        self.assertTrue(report["last_confirmed"]["bound"])
        self.assertFalse(report["last_confirmed"]["running"])


class DarwinCliIntegrationTest(LinuxCliIntegrationTest):
    platform = "darwin"
