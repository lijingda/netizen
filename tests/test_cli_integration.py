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

import yaml

from netizen_cli import cli
from netizen_cli.admin.users import AdminUsers
from netizen_cli.cli_data import prepare_instance
from netizen_cli.cli_services import ServiceBinding, ServiceManager, _service_environment
from netizen_cli.cli_setup import prepare_configuration
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
                return {"version": 1, "missingScopes": getattr(self, "missing_scopes", {"tenant": [], "user": []})}
            raise AssertionError(f"unexpected external setup helper: {module}")

        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                patch("netizen_cli.cli_services.ServiceManager", return_value=self.manager), \
                patch("netizen_cli.cli._current_binding", side_effect=lambda root: self.current), \
                patch("netizen_cli.cli_setup.require_codex_login"), \
                patch("netizen_cli.cli_setup._helper", side_effect=helper), \
                patch("netizen_cli.deployment.restart_worker.finish_manual_restart", return_value=None), \
                patch("sys.stdin", io.StringIO()):
            defaults = ["--admin-open-id", "ou_fixture_admin"] if command == "setup" else []
            code = cli.main([command, "--root", str(self.root), "--json", *defaults, *arguments])
        return code, json.loads(stdout.getvalue()), stderr.getvalue()

    def setup_instance(self) -> None:
        code, report, _ = self.invoke("setup")
        self.assertEqual(code, 0, report)
        self.assertEqual(report["status"], "prepared")

    def test_setup_reports_fixed_backend_and_preserves_explicit_and_legacy_configs(self) -> None:
        code, report, _ = self.invoke("setup")
        self.assertEqual((code, report["status"], report["backend"]), (0, "prepared", "codex"))
        config = self.root / "config.yaml"
        saved = yaml.safe_load(config.read_bytes())
        self.assertEqual(saved["instance"]["backend"], "codex")
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                if legacy:
                    del saved["instance"]["backend"]
                original = b"# preserved setup configuration\n" + yaml.safe_dump(saved).encode()
                config.write_bytes(original)
                for status in ("already_registered", "configured"):
                    if status == "configured":
                        (self.root / "state/admin-users.json").unlink()
                    code, report, _ = self.invoke("setup", "--backend", "codex")
                    self.assertEqual((code, report["status"], report["backend"]), (0, status, "codex"))
                    self.assertEqual(config.read_bytes(), original)

    def test_registered_setup_requires_readable_valid_backend_only_when_requested(self) -> None:
        self.setup_instance()
        config = self.root / "config.yaml"
        for content, mode, error in (
            (b"instance: {backend: other}", 0o600, "invalid instance.backend"),
            (b"instance: {backend: null}", 0o600, "invalid instance.backend"),
            (b"instance: [", 0o600, "configuration is invalid"),
            (b"instance: {}", 0o644, "expected a private current-user regular file"),
            (None, None, "cannot verify --backend"),
        ):
            with self.subTest(content=content, mode=mode):
                if content is None:
                    config.unlink()
                else:
                    config.write_bytes(content)
                    config.chmod(mode)
                code, report, _ = self.invoke("setup")
                self.assertEqual((code, report["status"]), (0, "already_registered"), report)
                self.assertNotIn("backend", report)
                code, report, _ = self.invoke("setup", "--backend", "codex")
                self.assertEqual(code, 1)
                self.assertIn(error, report["reason"])
                if content is not None:
                    self.assertEqual(config.read_bytes(), content)

    def test_registered_setup_repairs_missing_administrator_without_rebinding(self) -> None:
        self.setup_instance()
        definition = self.manager.service_file(self.root).read_bytes()
        database = (self.root / "state/channel.sqlite3").read_bytes()
        (self.root / "state/admin-users.json").unlink()
        self.current = self.environments[1]
        code, report, _ = self.invoke("setup", "--admin-open-id", "ou_repaired")
        self.assertEqual(code, 0, report)
        self.assertEqual(report["status"], "configured")
        self.assertEqual(self.manager.service_file(self.root).read_bytes(), definition)
        self.assertEqual((self.root / "state/channel.sqlite3").read_bytes(), database)
        self.assertEqual(AdminUsers(self.root, "cli_testapp").list()[0].identity.open_id, "ou_repaired")

    def test_retained_unregistered_root_with_lost_config_requires_explicit_backend(self):
        self.setup_instance()
        self.assertEqual(self.invoke("remove", "-y")[0], 0)
        config = self.root / "config.yaml"
        config.unlink()
        code, report, _ = self.invoke("setup")
        self.assertEqual(code, 1, report)
        self.assertIn("original --backend", report["reason"])
        self.assertFalse(config.exists())
        self.assertIsNone(self.manager.inspect(self.root))
        code, report, _ = self.invoke("setup", "--backend", "codex")
        self.assertEqual((code, report["status"], report["backend"]), (0, "prepared", "codex"))

    def test_config_disappearing_after_preflight_is_not_regenerated_implicitly(self):
        self.setup_instance()
        self.assertEqual(self.invoke("remove", "-y")[0], 0)
        def lose_config(root, **kwargs):
            (root / "config.yaml").unlink()
            return prepare_configuration(root, **kwargs)
        with patch("netizen_cli.cli_setup.prepare_configuration", side_effect=lose_config):
            code, report, _ = self.invoke("setup")
        self.assertEqual(code, 1, report)
        self.assertIn("original --backend", report["reason"])
        self.assertFalse((self.root / "config.yaml").exists())

    def test_setup_rechecks_backend_under_lock_without_implying_an_explicit_request(self) -> None:
        self.setup_instance()
        self.assertEqual(self.invoke("remove", "-y")[0], 0)
        config = self.root / "config.yaml"
        saved = yaml.safe_load(config.read_bytes())

        def prepare_after_change(root, **kwargs):
            saved["instance"]["backend"] = "other"
            config.write_text(yaml.safe_dump(saved))
            return prepare_configuration(root, **kwargs)

        with (
            patch("netizen_cli.backends.KNOWN_BACKENDS", ("codex", "other")),
            patch("netizen_cli.cli_setup.prepare_configuration", side_effect=prepare_after_change),
        ):
            code, report, _ = self.invoke("setup")
        self.assertEqual(code, 0, report)
        self.assertEqual(report["backend"], "other")

    def test_start_rejects_missing_administrator_without_starting_service(self) -> None:
        self.setup_instance()
        (self.root / "state/admin-users.json").unlink()
        code, report, _ = self.invoke("start")
        self.assertEqual(code, 1, report)
        self.assertIn("administrator configuration", report["reason"])
        self.assertFalse(self.manager.inspect(self.root).running)

    def test_failed_authorization_setup_can_purge_its_exact_saved_credentials(self) -> None:
        self.missing_scopes = {"tenant": ["im:message"], "user": []}
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

    def test_unbound_start_refuses_invalid_backend_before_registration(self) -> None:
        self.setup_instance()
        self.assertEqual(self.invoke("remove", "-y")[0], 0)
        config = self.root / "config.yaml"
        saved = yaml.safe_load(config.read_bytes())
        for backend in ("other", None, 123):
            with self.subTest(backend=backend), patch.object(self.manager, "register") as register:
                saved["instance"]["backend"] = backend
                config.write_text(yaml.safe_dump(saved))
                code, report, _ = self.invoke("start")
                self.assertEqual(code, 1, report)
                self.assertIn("invalid instance.backend", report["reason"])
                register.assert_not_called()
                self.assertIsNone(self.manager.inspect(self.root))

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
        self.assertEqual(_service_environment(self.home, self.current)["NETIZEN_CONFIG_PATH"],
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
        environment = _service_environment(self.home, self.current)
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
                                      self.root / "state")
        self.setup_instance()
        self.current = self.environments[1]
        with patch.dict(os.environ, {"CODEX_HOME": str(self.directory / "B-codex-home")}):
            code, report, _ = self.invoke("remove", "--purge", "-y")
        self.assertEqual(code, 1, report)
        self.assertEqual(report["phase"], "preflight")
        self.assertIn("overlaps", report["reason"])
        self.assertIsNotNone(self.manager.inspect(self.root))
        self.assertTrue((self.root / "state/admin-users.json").exists())

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
