from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import netizen_service_launcher as launcher


class NetizenServiceLauncherTest(unittest.TestCase):
    def _fake_bash(self, home: Path, body: str) -> Path:
        shell = home / "bash"
        shell.write_text("#!/bin/sh\nset -eu\n" + body, encoding="utf-8")
        shell.chmod(0o700)
        return shell

    def test_profile_capture_returns_exports_and_ignores_profile_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            shell = self._fake_bash(
                home,
                """
printf 'startup-noise=ignored'
export PROFILE_ONLY='from-profile'
export PATH="$HOME/.nvm/versions/node/v24/bin:/usr/bin"
eval "$2"
printf 'logout-noise=ignored'
""",
            )

            environment = launcher.capture_profile_environment(
                shell=shell,
                home=home,
                username="service-user",
                python_executable=Path(sys.executable),
                base_environment={
                    "CUSTOM_BASE": "kept",
                    "HOME": "/wrong",
                    "PATH": "/bin",
                },
                timeout=2,
            )

            self.assertEqual(environment["PROFILE_ONLY"], "from-profile")
            self.assertEqual(environment["CUSTOM_BASE"], "kept")
            self.assertEqual(environment["HOME"], str(home))
            self.assertEqual(
                environment["PATH"],
                f"{home}/.nvm/versions/node/v24/bin:/usr/bin",
            )
            self.assertNotIn("startup-noise", environment)
            self.assertNotIn("logout-noise", environment)

    def test_real_bash_profile_is_loaded_without_running_logout_hook(self) -> None:
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash is not installed")
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            sentinel = home / "logout-ran"
            (home / ".bash_profile").write_text(
                '. "$HOME/.bashrc"\n',
                encoding="utf-8",
            )
            (home / ".bashrc").write_text(
                "export NETIZEN_PROFILE_SENTINEL=loaded\n",
                encoding="utf-8",
            )
            (home / ".bash_logout").write_text(
                f"touch {sentinel!s}\n",
                encoding="utf-8",
            )

            environment = launcher.capture_profile_environment(
                shell=Path(bash),
                home=home,
                username="service-user",
                python_executable=Path(sys.executable),
                base_environment={"PATH": "/usr/bin:/bin"},
                timeout=2,
            )

            self.assertEqual(environment["NETIZEN_PROFILE_SENTINEL"], "loaded")
            self.assertFalse(sentinel.exists())

    def test_profile_failure_does_not_echo_captured_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            shell = self._fake_bash(
                home,
                "echo 'do-not-log-this-secret' >&2\nexit 9\n",
            )

            with self.assertRaises(launcher.ServiceLaunchError) as raised:
                launcher.capture_profile_environment(
                    shell=shell,
                    home=home,
                    username="service-user",
                    python_executable=Path(sys.executable),
                    base_environment={"PATH": "/bin"},
                    timeout=2,
                )

            self.assertIn("status 9", str(raised.exception))
            self.assertNotIn("do-not-log-this-secret", str(raised.exception))

    def test_profile_timeout_kills_the_shell_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            shell = self._fake_bash(home, "sleep 5\n")
            started = time.monotonic()

            with self.assertRaisesRegex(
                launcher.ServiceLaunchError,
                "did not finish within",
            ):
                launcher.capture_profile_environment(
                    shell=shell,
                    home=home,
                    username="service-user",
                    python_executable=Path(sys.executable),
                    base_environment={"PATH": "/bin"},
                    timeout=0.05,
                )

            self.assertLess(time.monotonic() - started, 2)

    def test_interrupt_always_requests_profile_process_group_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            shell = self._fake_bash(home, "eval \"$2\"\n")
            stdout = SimpleNamespace(fileno=lambda: 7, close=lambda: None)
            process = SimpleNamespace(stdout=stdout)

            with (
                patch.object(launcher.subprocess, "Popen", return_value=process),
                patch.object(
                    launcher,
                    "_read_profile_snapshot",
                    side_effect=KeyboardInterrupt,
                ),
                patch.object(launcher, "_terminate_process_group") as terminate,
                self.assertRaises(KeyboardInterrupt),
            ):
                launcher.capture_profile_environment(
                    shell=shell,
                    home=home,
                    username="service-user",
                    python_executable=Path(sys.executable),
                    base_environment={"PATH": "/bin"},
                    timeout=2,
                )

            terminate.assert_called_once_with(process)

    def test_environment_frame_detects_concurrent_stdout_corruption(self) -> None:
        start_token = "NETIZEN_ENV_START_test"
        end_token = "NETIZEN_ENV_END_test"
        payload = b"FIRST=one\0SECOND=two\0"
        digest = hashlib.sha256(payload).hexdigest().encode()
        frame = (
            b"startup noise\0"
            + start_token.encode()
            + b"\0"
            + str(len(payload)).encode()
            + b"\0"
            + digest
            + b"\0"
            + payload
            + b"\0"
            + end_token.encode()
            + b"\0"
        )

        self.assertEqual(
            launcher._parse_environment_dump(
                frame,
                start_token=start_token,
                end_token=end_token,
            ),
            {"FIRST": "one", "SECOND": "two"},
        )
        corrupted = frame.replace(b"SECOND=two", b"INJECTED=x", 1)
        with self.assertRaisesRegex(
            launcher.ServiceLaunchError,
            "integrity check",
        ):
            launcher._parse_environment_dump(
                corrupted,
                start_token=start_token,
                end_token=end_token,
            )

    def test_profile_output_limit_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            shell = self._fake_bash(home, "eval \"$2\"\n")

            with (
                patch.object(launcher, "PROFILE_CAPTURE_MAX_BYTES", 32),
                self.assertRaisesRegex(
                    launcher.ServiceLaunchError,
                    "exceeded the 4 MiB safety limit",
                ),
            ):
                launcher.capture_profile_environment(
                    shell=shell,
                    home=home,
                    username="service-user",
                    python_executable=Path(sys.executable),
                    base_environment={"PATH": "/bin"},
                    timeout=2,
                )

    def test_unsupported_or_non_executable_shell_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            unsupported = home / "nushell"
            unsupported.write_text("", encoding="utf-8")
            unsupported.chmod(0o700)

            with self.assertRaisesRegex(
                launcher.ServiceLaunchError,
                "unsupported account login shell",
            ):
                launcher.capture_profile_environment(
                    shell=unsupported,
                    home=home,
                    username="service-user",
                    python_executable=Path(sys.executable),
                    base_environment={"PATH": "/bin"},
                )

            unsupported.chmod(0o600)
            with self.assertRaisesRegex(
                launcher.ServiceLaunchError,
                "not executable",
            ):
                launcher.capture_profile_environment(
                    shell=unsupported,
                    home=home,
                    username="service-user",
                    python_executable=Path(sys.executable),
                    base_environment={"PATH": "/bin"},
                )

    def test_managed_values_override_profile_without_losing_tool_environment(self) -> None:
        home = Path("/home/service-user")
        environment = launcher.service_environment(
            {
                "CODEX_HOME": "/wrong/codex",
                "CUSTOM_TOKEN": "available-to-codex",
                "FEISHU_APP_SECRET": "must-be-removed",
                "FEISHU_APP_SECRET_FILE": "/wrong/feishu-file",
                "NETIZEN_ADMIN_SECRET": "must-also-be-removed",
                "NETIZEN_ADMIN_SECRET_FILE": "/wrong/admin-file",
                "PATH": "/home/service-user/.nvm/current/bin:/usr/bin",
                "PYTHONPATH": "/wrong/python",
                "XDG_CONFIG_HOME": "/home/service-user/.xdg-config",
                "https_proxy": "http://proxy",
            },
            home=home,
            username="service-user",
            shell=Path("/bin/bash"),
            codex_home="/home/service-user/.codex",
            config_path="/home/service-user/.netizen/config.yaml",
            secret_file="/home/service-user/.netizen/credentials/feishu-app-secret",
            admin_secret_file=(
                "/home/service-user/.netizen/credentials/admin-web-secret"
            ),
            ready_file="/home/service-user/.netizen/state/service.ready",
            lifetime_lock_file=(
                "/home/service-user/.netizen/state/service.lifetime.lock"
            ),
        )

        self.assertEqual(
            environment["PATH"],
            "/home/service-user/.nvm/current/bin:/usr/bin",
        )
        self.assertEqual(environment["CUSTOM_TOKEN"], "available-to-codex")
        self.assertEqual(environment["https_proxy"], "http://proxy")
        self.assertEqual(
            environment["XDG_CONFIG_HOME"],
            "/home/service-user/.xdg-config",
        )
        self.assertEqual(environment["CODEX_HOME"], "/home/service-user/.codex")
        self.assertNotIn("FEISHU_APP_SECRET", environment)
        self.assertNotIn("NETIZEN_ADMIN_SECRET", environment)
        self.assertEqual(
            environment["FEISHU_APP_SECRET_FILE"],
            "/home/service-user/.netizen/credentials/feishu-app-secret",
        )
        self.assertEqual(
            environment["NETIZEN_ADMIN_SECRET_FILE"],
            "/home/service-user/.netizen/credentials/admin-web-secret",
        )
        self.assertNotIn("PYTHONPATH", environment)
        self.assertEqual(environment["PYTHONUNBUFFERED"], "1")

    def test_launch_execs_release_python_with_the_captured_environment(self) -> None:
        account = SimpleNamespace(
            pw_dir="/home/service-user",
            pw_name="service-user",
            pw_shell="/bin/bash",
        )
        managed = {
            "CODEX_HOME": "/home/service-user/.codex",
            "FEISHU_APP_SECRET_FILE": (
                "/home/service-user/.netizen/credentials/feishu-app-secret"
            ),
            "NETIZEN_ADMIN_SECRET_FILE": (
                "/home/service-user/.netizen/credentials/admin-web-secret"
            ),
            "NETIZEN_CONFIG_PATH": (
                "/home/service-user/.netizen/config.yaml"
            ),
            "NETIZEN_READY_FILE": (
                "/home/service-user/.netizen/state/service.ready"
            ),
            "NETIZEN_LIFETIME_LOCK_FILE": (
                "/home/service-user/.netizen/state/service.lifetime.lock"
            ),
        }
        with (
            patch.dict(launcher.os.environ, managed, clear=True),
            patch.object(launcher.pwd, "getpwuid", return_value=account),
            patch.object(
                launcher,
                "capture_profile_environment",
                return_value={
                    "PATH": "/home/service-user/.nvm/current/bin:/usr/bin",
                    "PYTHONOPTIMIZE": "2",
                },
            ),
            patch.object(launcher.os, "execve") as execute,
            patch.object(launcher, "acquire_lifetime_lock", return_value=9),
            patch.object(launcher, "clear_ready_marker") as clear_ready,
            patch.object(launcher.os, "set_inheritable") as set_inheritable,
        ):
            launcher.launch()

        executable, argv, environment = execute.call_args.args
        self.assertEqual(executable, launcher.sys.executable)
        self.assertEqual(
            argv,
            [launcher.sys.executable, "-E", "-B", "-u", "-m", "netizen.main"],
        )
        self.assertEqual(
            environment["PATH"],
            "/home/service-user/.nvm/current/bin:/usr/bin",
        )
        self.assertEqual(environment["PYTHONOPTIMIZE"], "2")
        self.assertEqual(environment["CODEX_HOME"], managed["CODEX_HOME"])
        self.assertEqual(environment["NETIZEN_LIFETIME_LOCK_FD"], "9")
        clear_ready.assert_called_once_with(Path(managed["NETIZEN_READY_FILE"]))
        self.assertEqual(
            [call.args for call in set_inheritable.call_args_list],
            [(9, True), (9, False), (9, False)],
        )

    def test_lifetime_lock_uses_one_stable_cloexec_inode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "service.lifetime.lock"
            first = launcher.acquire_lifetime_lock(path)
            first_inode = os.fstat(first).st_ino
            try:
                self.assertFalse(os.get_inheritable(first))
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                with self.assertRaisesRegex(
                    launcher.ServiceLaunchError,
                    "still owns the lifetime lock",
                ):
                    launcher.acquire_lifetime_lock(path)
            finally:
                fcntl.flock(first, fcntl.LOCK_UN)
                os.close(first)

            second = launcher.acquire_lifetime_lock(path)
            try:
                self.assertEqual(os.fstat(second).st_ino, first_inode)
                self.assertFalse(os.get_inheritable(second))
            finally:
                fcntl.flock(second, fcntl.LOCK_UN)
                os.close(second)

    def test_launcher_removes_stale_ready_marker_before_profile_capture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ready = root / "service.ready"
            lock = root / "service.lifetime.lock"
            ready.write_text("stale", encoding="utf-8")
            with (
                patch.dict(
                    launcher.os.environ,
                    {
                        "NETIZEN_LIFETIME_LOCK_FILE": str(lock),
                        "NETIZEN_READY_FILE": str(ready),
                    },
                    clear=True,
                ),
                patch.object(
                    launcher,
                    "_launch_with_lifetime_lock",
                ) as launch_main,
            ):
                launcher.launch()

            self.assertFalse(ready.exists())
            descriptor = launch_main.call_args.args[0]
            with self.assertRaises(OSError):
                os.fstat(descriptor)


if __name__ == "__main__":
    unittest.main()
