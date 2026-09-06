from __future__ import annotations

import fcntl
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from netizen.deployment.update_executor import UpdateDispatchUnknown, UpdateExecutor, UpdateExecutorError


OPERATION_ID = "abcdef0123456789" * 2


class UpdateExecutorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name).resolve()
        self.release = self.home / ".netizen" / "releases" / "v0.4.6"
        self.worker = self.release / "source" / "scripts" / "netizen_updater.py"
        self.worker.parent.mkdir(parents=True)
        self.worker.write_text("pass\n", encoding="utf-8")
        self.python = self.release / "venv" / "bin" / "python"
        self.python.parent.mkdir(parents=True)
        self.python.symlink_to(sys.executable)
        self.state = self.home / ".netizen" / "state"
        self.state.mkdir(mode=0o700)
        self.uid = os.geteuid()

    @staticmethod
    def result(code: int = 0, output: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], code, stdout=output)

    def test_linux_launch_is_a_distinct_transient_service_with_a_physical_release(self) -> None:
        current = self.home / ".netizen" / "current"
        current.symlink_to(self.release)
        executor = UpdateExecutor(self.home, "linux")
        with patch("netizen.deployment.update_executor.subprocess.run", return_value=self.result()) as run:
            executor.launch(OPERATION_ID, current)
        arguments = run.call_args.args[0]
        self.assertIn(f"--unit=netizen-update-{OPERATION_ID}.service", arguments)
        self.assertIn("--user", arguments)
        self.assertIn("--service-type=exec", arguments)
        self.assertIn("--collect", arguments)
        self.assertIn("--property=Restart=no", arguments)
        self.assertIn("--property=KillMode=control-group", arguments)
        self.assertEqual(
            arguments[arguments.index("--") + 1 :],
            ["/usr/bin/env", "--", str(self.python), "-E", "-B", "-u", str(self.worker), "--operation-id", OPERATION_ID],
        )
        for forbidden in ("--scope", "--slice-inherit", "--wait", "--pipe", "--pty"):
            self.assertNotIn(forbidden, arguments)
        self.assertFalse(any("PartOf=" in value or "BindsTo=" in value for value in arguments))
        self.assertNotIn("netizen.service", " ".join(arguments))
        self.assertTrue(run.call_args.kwargs["close_fds"])
        self.assertNotIn("pass_fds", run.call_args.kwargs)
        self.assertLessEqual(run.call_args.kwargs["timeout"], 10)
        self.assertFalse(run.call_args.kwargs.get("shell", False))

    def test_dispatch_does_not_persist_or_pass_profile_exports_or_main_service_fds(self) -> None:
        with (
            patch.dict(os.environ, {"PROFILE_ONLY": "do-not-persist", "FEISHU_APP_SECRET": "secret", "NETIZEN_LIFETIME_LOCK_FD": "19"}),
            patch("netizen.deployment.update_executor.subprocess.run", return_value=self.result()) as run,
        ):
            UpdateExecutor(self.home, "linux").launch(OPERATION_ID, self.release)
        self.assertNotIn("secret", " ".join(run.call_args.args[0]))
        self.assertNotIn("do-not-persist", " ".join(run.call_args.args[0]))
        self.assertNotIn("FEISHU_APP_SECRET", run.call_args.kwargs["env"])
        self.assertNotIn("NETIZEN_LIFETIME_LOCK_FD", run.call_args.kwargs["env"])
        self.assertEqual(list(self.state.iterdir()), [])

    def test_inheritable_lifetime_descriptor_does_not_reach_manager_command(self) -> None:
        lock = self.state / "service.lifetime.lock"
        descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.set_inheritable(descriptor, True)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code = (
                "import os,sys; fd=int(sys.argv[1]); "
                "target=os.stat(sys.argv[2]); "
                "\ntry: found=os.fstat(fd)\nexcept OSError: sys.exit(0)\n"
                "sys.exit(int((found.st_dev,found.st_ino)==(target.st_dev,target.st_ino)))"
            )
            result = UpdateExecutor(self.home, "linux")._command(
                [sys.executable, "-E", "-B", "-c", code, str(descriptor), str(lock)]
            )
            self.assertEqual(result.returncode, 0)
        finally:
            os.close(descriptor)

    def test_invalid_operation_ids_never_invoke_a_manager(self) -> None:
        executor = UpdateExecutor(self.home, "linux")
        with patch("netizen.deployment.update_executor.subprocess.run") as run:
            for value in ("", "a" * 31, "a" * 33, "A" * 32, "--user", "../x", OPERATION_ID + "\n"):
                with self.subTest(value=value):
                    for action in (lambda: executor.launch(value, self.release), lambda: executor.is_active(value), lambda: executor.cleanup(value)):
                        with self.assertRaises(UpdateExecutorError):
                            action()
            run.assert_not_called()

    def test_worker_outside_the_installed_release_is_rejected(self) -> None:
        executor = UpdateExecutor(self.home, "linux")
        with patch("netizen.deployment.update_executor.subprocess.run") as run:
            for path in (self.home, Path("relative"), self.home / "missing"):
                with self.subTest(path=path), self.assertRaises(UpdateExecutorError):
                    executor.launch(OPERATION_ID, path)
            self.worker.unlink()
            other = self.home / "untrusted.py"
            other.write_text("pass\n", encoding="utf-8")
            self.worker.symlink_to(other)
            with self.assertRaises(UpdateExecutorError):
                executor.launch(OPERATION_ID, self.release)
            run.assert_not_called()

    def test_linux_paths_preserve_dollars_percent_spaces_and_shell_punctuation(self) -> None:
        renamed = self.release.with_name("release ${UNEXPECTED} $x %h; ' \\")
        self.release.rename(renamed)
        with patch("netizen.deployment.update_executor.subprocess.run", return_value=self.result()) as run:
            UpdateExecutor(self.home, "linux").launch(OPERATION_ID, renamed)
        arguments = run.call_args.args[0]
        worker_arguments = arguments[arguments.index("--") + 1 :]
        self.assertEqual(worker_arguments[:2], ["/usr/bin/env", "--"])
        self.assertEqual(worker_arguments[2], str(renamed / "venv" / "bin" / "python").replace("$", "$$"))
        self.assertEqual(worker_arguments[6], str(renamed / "source" / "scripts" / "netizen_updater.py").replace("$", "$$"))
        self.assertIn("%h", worker_arguments[2])
        self.assertNotIn("%%h", worker_arguments[2])
        # No additional shell command or environment override is synthesized.
        self.assertEqual(worker_arguments[-2:], ["--operation-id", OPERATION_ID])

    def test_linux_home_is_literal_in_manager_properties_and_escaped_only_in_argv(self) -> None:
        account_home = self.home / "account ${OTHER} %h; space"
        account_home.mkdir()
        (self.home / ".netizen").rename(account_home / ".netizen")
        release = account_home / ".netizen" / "releases" / "v0.4.6"
        with patch("netizen.deployment.update_executor.subprocess.run", return_value=self.result()) as run:
            UpdateExecutor(account_home, "linux").launch(OPERATION_ID, release)
        arguments = run.call_args.args[0]
        self.assertIn(f"--working-directory={account_home}", arguments)
        self.assertIn(f"--setenv=HOME={account_home}", arguments)
        self.assertEqual(run.call_args.kwargs["cwd"], account_home)
        worker_arguments = arguments[arguments.index("--") + 1 :]
        self.assertIn("$${OTHER} %h; space", worker_arguments[2])

    def test_launch_timeout_and_nonzero_are_ambiguous_and_never_retried(self) -> None:
        for outcome in (subprocess.TimeoutExpired("systemd-run", 10, stderr="secret"), OSError("secret"), self.result(1, "secret")):
            with self.subTest(outcome=outcome):
                options = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
                with patch("netizen.deployment.update_executor.subprocess.run", **options) as run:
                    with self.assertRaises(UpdateDispatchUnknown) as caught:
                        UpdateExecutor(self.home, "linux").launch(OPERATION_ID, self.release)
                self.assertNotIn("secret", str(caught.exception))
                self.assertEqual(run.call_count, 1)

    def test_missing_manager_is_a_definite_pre_dispatch_error(self) -> None:
        with patch("netizen.deployment.update_executor.subprocess.run", side_effect=FileNotFoundError):
            with self.assertRaises(UpdateExecutorError) as caught:
                UpdateExecutor(self.home, "linux").launch(OPERATION_ID, self.release)
        self.assertNotIsInstance(caught.exception, UpdateDispatchUnknown)

    def test_linux_observation_keeps_activating_and_deactivating_reserved(self) -> None:
        executor = UpdateExecutor(self.home, "linux")
        for state in ("activating", "active", "deactivating", "reloading", "refreshing"):
            with self.subTest(state=state), patch("netizen.deployment.update_executor.subprocess.run", return_value=self.result(output=f"LoadState=loaded\nActiveState={state}\n")):
                self.assertTrue(executor.is_active(OPERATION_ID))
        for output in ("LoadState=not-found\nActiveState=inactive\n", "LoadState=loaded\nActiveState=failed\n", "LoadState=loaded\nActiveState=inactive\n"):
            with self.subTest(output=output), patch("netizen.deployment.update_executor.subprocess.run", return_value=self.result(output=output)):
                self.assertFalse(executor.is_active(OPERATION_ID))

    def test_observation_error_is_not_reported_as_an_exited_worker(self) -> None:
        for outcome in (subprocess.TimeoutExpired("systemctl", 10), self.result(1), self.result(output="unexpected")):
            options = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
            with self.subTest(outcome=outcome), patch("netizen.deployment.update_executor.subprocess.run", **options):
                with self.assertRaises(UpdateExecutorError):
                    UpdateExecutor(self.home, "linux").is_active(OPERATION_ID)

    def test_macos_job_is_once_only_outside_login_discovery_and_has_literal_arguments(self) -> None:
        # Root containers cannot manufacture an owned regular user's directory.
        if os.stat(self.state).st_uid != self.uid:
            self.skipTest("requires a non-root current-user state directory")
        renamed = self.release.with_name("release $x; ' %")
        self.release.rename(renamed)
        with patch("netizen.deployment.update_executor.subprocess.run", return_value=self.result()) as run:
            UpdateExecutor(self.home, "darwin").launch(OPERATION_ID, renamed)
        path = self.state / f"netizen-update-{OPERATION_ID}.plist"
        payload = plistlib.loads(path.read_bytes())
        self.assertEqual(payload["ProgramArguments"][0], str(renamed / "venv" / "bin" / "python"))
        self.assertEqual(payload["ProgramArguments"][4], str(renamed / "source" / "scripts" / "netizen_updater.py"))
        self.assertEqual(payload["ProgramArguments"][-2:], ["--operation-id", OPERATION_ID])
        self.assertTrue(payload["RunAtLoad"])
        self.assertFalse(payload["KeepAlive"])
        self.assertEqual(payload["EnvironmentVariables"], {"HOME": str(self.home), "PATH": "/usr/local/bin:/usr/bin:/bin"})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(run.call_args.args[0], ["launchctl", "bootstrap", f"gui/{self.uid}", str(path)])
        self.assertFalse((self.home / "Library" / "LaunchAgents").exists())

    def test_macos_existing_submission_is_not_overwritten_or_dispatched_twice(self) -> None:
        if os.stat(self.state).st_uid != self.uid:
            self.skipTest("requires a non-root current-user state directory")
        executor = UpdateExecutor(self.home, "darwin")
        with patch("netizen.deployment.update_executor.subprocess.run", return_value=self.result()) as run:
            executor.launch(OPERATION_ID, self.release)
            with self.assertRaises(UpdateDispatchUnknown):
                executor.launch(OPERATION_ID, self.release)
        self.assertEqual(sum(call.args[0][1] == "bootstrap" for call in run.call_args_list), 1)

    def test_macos_dispatch_timeout_preserves_submission_for_reconciliation(self) -> None:
        if os.stat(self.state).st_uid != self.uid:
            self.skipTest("requires a non-root current-user state directory")
        with patch("netizen.deployment.update_executor.subprocess.run", side_effect=[self.result(), subprocess.TimeoutExpired("launchctl", 10)]) as run:
            with self.assertRaises(UpdateDispatchUnknown):
                UpdateExecutor(self.home, "darwin").launch(OPERATION_ID, self.release)
        self.assertTrue((self.state / f"netizen-update-{OPERATION_ID}.plist").is_file())
        self.assertEqual(run.call_count, 2)

    def test_missing_macos_gui_domain_fails_before_creating_a_submission(self) -> None:
        with patch("netizen.deployment.update_executor.subprocess.run", return_value=self.result(125)) as run:
            with self.assertRaisesRegex(UpdateExecutorError, "GUI") as caught:
                UpdateExecutor(self.home, "darwin").launch(OPERATION_ID, self.release)
        self.assertNotIsInstance(caught.exception, UpdateDispatchUnknown)
        self.assertEqual(list(self.state.iterdir()), [])
        self.assertEqual(run.call_count, 1)

    def test_macos_rejects_a_symlinked_state_directory(self) -> None:
        replacement = self.home / "other-state"
        self.state.rename(replacement)
        self.state.symlink_to(replacement)
        with patch("netizen.deployment.update_executor.subprocess.run", return_value=self.result()) as run:
            with self.assertRaisesRegex(UpdateExecutorError, "state directory"):
                UpdateExecutor(self.home, "darwin").launch(OPERATION_ID, self.release)
        self.assertEqual(run.call_count, 1)

    def test_macos_observation_uses_only_exit_codes_and_checks_the_domain(self) -> None:
        executor = UpdateExecutor(self.home, "darwin")
        for status, expected in ((0, True), (113, False)):
            with self.subTest(status=status), patch("netizen.deployment.update_executor.subprocess.run", side_effect=[self.result(), self.result(status, "untrusted arbitrary print text")]) as run:
                self.assertEqual(executor.is_active(OPERATION_ID), expected)
                self.assertEqual(run.call_args.kwargs["stdout"], subprocess.DEVNULL)
        for outcomes in ([self.result(125)], [self.result(), self.result(1)]):
            with self.subTest(outcomes=outcomes), patch("netizen.deployment.update_executor.subprocess.run", side_effect=outcomes):
                with self.assertRaises(UpdateExecutorError):
                    executor.is_active(OPERATION_ID)

    def test_macos_cleanup_removes_only_the_operation_job_and_submission(self) -> None:
        path = self.state / f"netizen-update-{OPERATION_ID}.plist"
        path.write_bytes(b"retained submission")
        with patch("netizen.deployment.update_executor.subprocess.run", return_value=self.result()) as run:
            UpdateExecutor(self.home, "darwin").cleanup(OPERATION_ID)
        self.assertEqual(run.call_args.args[0], ["launchctl", "bootout", f"gui/{self.uid}/netizen-update-{OPERATION_ID}"])
        self.assertFalse(path.exists())
        self.assertNotIn("netizen.service", str(run.call_args_list))

    def test_macos_ambiguous_cleanup_keeps_the_submission(self) -> None:
        path = self.state / f"netizen-update-{OPERATION_ID}.plist"
        path.write_bytes(b"retained submission")
        outcomes = [self.result(), self.result(), self.result(1), self.result(), self.result()]
        with patch("netizen.deployment.update_executor.subprocess.run", side_effect=outcomes):
            with self.assertRaises(UpdateExecutorError):
                UpdateExecutor(self.home, "darwin").cleanup(OPERATION_ID)
        self.assertTrue(path.exists())

    def test_linux_cleanup_relies_on_collect_and_never_stops_a_worker(self) -> None:
        with patch("netizen.deployment.update_executor.subprocess.run") as run:
            UpdateExecutor(self.home, "linux").cleanup(OPERATION_ID)
            run.assert_not_called()

    def test_root_account_still_uses_its_own_user_manager_without_escalation(self) -> None:
        with (
            patch("netizen.deployment.update_executor.os.geteuid", return_value=0),
            patch("netizen.deployment.update_executor.subprocess.run", return_value=self.result()) as run,
        ):
            UpdateExecutor(self.home, "linux").launch(OPERATION_ID, self.release)
        self.assertIn("--user", run.call_args.args[0])
        self.assertNotIn("sudo", run.call_args.args[0])
        self.assertNotIn("--system", run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs["env"]["XDG_RUNTIME_DIR"], os.environ.get("XDG_RUNTIME_DIR", "/run/user/0"))

    def test_unsupported_platform_is_rejected(self) -> None:
        with self.assertRaises(UpdateExecutorError):
            UpdateExecutor(self.home, "win32")


if __name__ == "__main__":
    unittest.main()
