from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from netizen_cli.instance import (
    INSTANCE_ROOT_MARKER,
    INSTANCE_ROOT_MARKER_CONTENT,
    require_instance_root_marker,
)
from netizen_cli import service_launcher as launcher


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
        home = Path("/home/service-user").resolve()
        root = home / ".netizen"
        environment = launcher.service_environment(
            {
                "NETIZEN_ROOT": "/wrong/instance",
                "CODEX_HOME": "/wrong/codex",
                "CUSTOM_TOKEN": "available-to-codex",
                "FEISHU_APP_SECRET": "must-be-removed",
                "FEISHU_APP_SECRET_FILE": "/wrong/feishu-file",
                "NETIZEN_LARK_APP_CONFIG": "/wrong/lark-config",
                "NETIZEN_ADMIN_SECRET": "must-also-be-removed",
                "NETIZEN_ADMIN_SECRET_FILE": "/wrong/admin-file",
                "PATH": "/home/service-user/.nvm/current/bin:/usr/bin",
                "PYTHONPATH": "/wrong/python",
                "XDG_CONFIG_HOME": "/home/service-user/.xdg-config",
                "https_proxy": "http://proxy",
            },
            instance_root=root,
            home=home,
            username="service-user",
            shell=Path("/bin/bash"),
            codex_home="/home/service-user/.codex",
            config_path=str(root / "config.yaml"),
            lark_app_config=str(root / "lark-app/config.json"),
            admin_secret_file=str(root / "credentials/admin-web-secret"),
            ready_file=str(root / "state/service.ready"),
            lifetime_lock_file=str(root / "state/service.lifetime.lock"),
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
        self.assertEqual(environment["NETIZEN_ROOT"], str(home / ".netizen"))
        self.assertNotIn("FEISHU_APP_SECRET", environment)
        self.assertNotIn("FEISHU_APP_SECRET_FILE", environment)
        self.assertNotIn("NETIZEN_ADMIN_SECRET", environment)
        self.assertEqual(
            environment["NETIZEN_LARK_APP_CONFIG"],
            str(root / "lark-app/config.json"),
        )
        self.assertEqual(
            environment["NETIZEN_ADMIN_SECRET_FILE"],
            str(root / "credentials/admin-web-secret"),
        )
        self.assertNotIn("PYTHONPATH", environment)
        self.assertEqual(environment["PYTHONUNBUFFERED"], "1")

    def test_launch_execs_bound_python_with_the_captured_environment(self) -> None:
        account = SimpleNamespace(
            pw_dir="/home/service-user",
            pw_name="service-user",
            pw_shell="/bin/bash",
        )
        root = Path("/home/service-user/.netizen").resolve()
        managed = {
            "NETIZEN_ROOT": str(root),
            "CODEX_HOME": "/home/service-user/.codex",
            **launcher._instance_paths(root),
        }
        with (
            patch.dict(launcher.os.environ, managed, clear=True),
            patch.object(launcher.pwd, "getpwuid", return_value=account),
            patch.object(
                launcher,
                "capture_profile_environment",
                return_value={
                    "NETIZEN_ROOT": "/wrong/instance/from-profile",
                    "PATH": "/home/service-user/.nvm/current/bin:/usr/bin",
                    "PYTHONOPTIMIZE": "2",
                    "NETIZEN_CLI_PREFIX": "/wrong/profile-env",
                    "NETIZEN_CLI_SERVICE": "0",
                },
            ),
            patch.object(launcher.os, "execve") as execute,
            patch.object(launcher, "require_instance_root_marker") as check_root,
            patch.object(launcher, "acquire_lifetime_lock", return_value=9),
            patch.object(launcher, "clear_ready_marker") as clear_ready,
            patch.object(launcher, "publish_service_identity", return_value=(1, 2)),
            patch.object(launcher, "clear_own_service_identity"),
            patch.object(launcher.os, "set_inheritable") as set_inheritable,
        ):
            launcher.launch()

        executable, argv, environment = execute.call_args.args
        check_root.assert_called_once_with(root)
        self.assertEqual(executable, launcher.sys.executable)
        self.assertEqual(
            argv,
            [launcher.sys.executable, "-E", "-P", "-B", "-u", "-m", "netizen_cli.main"],
        )
        self.assertEqual(
            environment["PATH"],
            "/home/service-user/.nvm/current/bin:/usr/bin",
        )
        self.assertEqual(environment["PYTHONOPTIMIZE"], "2")
        self.assertEqual(environment["CODEX_HOME"], managed["CODEX_HOME"])
        self.assertEqual(environment["NETIZEN_ROOT"], managed["NETIZEN_ROOT"])
        self.assertEqual(environment["NETIZEN_LIFETIME_LOCK_FD"], "9")
        self.assertEqual(environment["NETIZEN_CLI_PREFIX"], launcher.sys.prefix)
        self.assertEqual(environment["NETIZEN_CLI_SERVICE"], "1")
        clear_ready.assert_called_once_with(Path(managed["NETIZEN_READY_FILE"]))
        self.assertEqual(
            [call.args for call in set_inheritable.call_args_list],
            [(9, True), (9, False), (9, False)],
        )

    def test_codex_home_defaults_to_shared_profile_then_account_home(self) -> None:
        home = Path("/home/service-user").resolve()
        root = home / ".netizen"
        for captured, expected in (({"CODEX_HOME": "/tmp/shared-profile-codex"}, "/tmp/shared-profile-codex"),
                                   ({}, str(home / ".codex"))):
            with self.subTest(captured=captured):
                environment = launcher.service_environment(
                    captured, instance_root=root, home=home, username="service-user",
                    shell=Path("/bin/bash"), codex_home=None,
                    config_path=str(root / "config.yaml"),
                    lark_app_config=str(root / "lark-app/config.json"),
                    admin_secret_file=str(root / "credentials/admin-web-secret"),
                    ready_file=str(root / "state/service.ready"),
                    lifetime_lock_file=str(root / "state/service.lifetime.lock"),
                )
                self.assertEqual(environment["CODEX_HOME"], expected)

    def test_explicit_root_derives_paths_and_keeps_bound_codex_home(self) -> None:
        root = Path("/tmp/instance-a").resolve()
        with (
            patch.dict(launcher.os.environ, {"CODEX_HOME": "/tmp/shared-codex"}, clear=True),
            patch.object(launcher, "require_instance_root_marker"),
            patch.object(launcher, "acquire_lifetime_lock", return_value=9),
            patch.object(launcher, "clear_ready_marker"),
            patch.object(launcher, "publish_service_identity", return_value=(1, 2)),
            patch.object(launcher, "clear_own_service_identity"),
            patch.object(launcher, "_launch_with_lifetime_lock") as execute,
            patch.object(launcher.os, "set_inheritable"),
        ):
            launcher.launch(root)
        managed = execute.call_args.kwargs["managed"]
        self.assertEqual(managed["CODEX_HOME"], "/tmp/shared-codex")
        self.assertEqual(managed["NETIZEN_CONFIG_PATH"], str(root / "config.yaml"))

    def test_wrong_environment_binding_rejected_before_instance_mutation(self) -> None:
        with (
            patch.dict(launcher.os.environ, {"NETIZEN_CLI_PREFIX": "/wrong/environment"}, clear=True),
            patch.object(launcher, "acquire_lifetime_lock") as acquire,
            self.assertRaisesRegex(launcher.ServiceLaunchError, "bound Python environment"),
        ):
            launcher.launch(Path("/tmp/instance-a").resolve())
        acquire.assert_not_called()

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
            root = Path(directory).resolve() / "share with trailing space "
            state = root / "state"
            state.mkdir(parents=True)
            marker = root / INSTANCE_ROOT_MARKER
            marker.write_bytes(INSTANCE_ROOT_MARKER_CONTENT)
            marker.chmod(0o600)
            ready = state / "service.ready"
            ready.write_text("stale", encoding="utf-8")
            with (
                patch.dict(
                    launcher.os.environ,
                    {
                        **launcher._instance_paths(root),
                        "NETIZEN_ROOT": str(root),
                        "CODEX_HOME": str(root / "codex-user-state"),
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
            self.assertEqual(launch_main.call_args.kwargs["instance_root"], root)
            descriptor = launch_main.call_args.args[0]
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_invalid_root_marker_fails_before_any_startup_mutation(self) -> None:
        for case in ("missing", "content", "mode", "symlink", "directory"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                marker = root / INSTANCE_ROOT_MARKER
                if case == "directory":
                    marker.mkdir(mode=0o600)
                elif case == "symlink":
                    target = root / "marker-target"
                    target.write_bytes(INSTANCE_ROOT_MARKER_CONTENT)
                    target.chmod(0o600)
                    marker.symlink_to(target)
                elif case != "missing":
                    marker.write_bytes(b"unknown-root\n" if case == "content" else INSTANCE_ROOT_MARKER_CONTENT)
                    marker.chmod(0o644 if case == "mode" else 0o600)
                with (
                    patch.dict(launcher.os.environ, {
                        **launcher._instance_paths(root),
                        "NETIZEN_ROOT": str(root),
                        "CODEX_HOME": str(root / "shared-codex"),
                    }, clear=True),
                    patch.object(launcher, "acquire_lifetime_lock") as acquire,
                    patch.object(launcher, "clear_ready_marker") as clear,
                    patch.object(launcher, "capture_profile_environment") as capture,
                    patch.object(launcher.os, "execve") as execute,
                ):
                    with self.assertRaisesRegex(launcher.ServiceLaunchError, "instance root marker"):
                        launcher.launch()
                    acquire.assert_not_called()
                    clear.assert_not_called()
                    capture.assert_not_called()
                    execute.assert_not_called()
                self.assertFalse((root / "state").exists())

    def test_root_marker_requires_expected_owner_and_exact_content_without_rewriting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / INSTANCE_ROOT_MARKER
            marker.write_bytes(INSTANCE_ROOT_MARKER_CONTENT)
            marker.chmod(0o600)
            before = marker.stat()
            require_instance_root_marker(root, uid=os.geteuid())
            with self.assertRaisesRegex(ValueError, "instance root marker"):
                require_instance_root_marker(root, uid=os.geteuid() + 1)
            after = marker.stat()
            self.assertEqual((before.st_ino, before.st_mode, before.st_mtime_ns),
                             (after.st_ino, after.st_mode, after.st_mtime_ns))
            marker.write_bytes(INSTANCE_ROOT_MARKER_CONTENT + b"extra")
            with self.assertRaisesRegex(ValueError, "instance root marker"):
                require_instance_root_marker(root)

    def test_mixed_instance_paths_fail_before_lock_or_ready_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            other = root / "another-instance"
            values = {
                **launcher._instance_paths(root),
                "NETIZEN_ROOT": str(root),
                "CODEX_HOME": str(root / "shared-codex"),
            }
            for field in launcher._instance_paths(root):
                with (
                    self.subTest(field=field),
                    patch.dict(launcher.os.environ, {
                        **values, field: launcher._instance_paths(other)[field],
                    }, clear=True),
                    patch.object(launcher, "acquire_lifetime_lock") as acquire,
                    patch.object(launcher, "clear_ready_marker") as clear,
                    self.assertRaisesRegex(launcher.ServiceLaunchError, "does not match NETIZEN_ROOT"),
                ):
                    launcher.launch()
                acquire.assert_not_called()
                clear.assert_not_called()

    def test_instance_lifetime_locks_are_independent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / name / "state/service.lifetime.lock" for name in ("a", "b")]
            for path in paths:
                path.parent.mkdir(parents=True)
            first = launcher.acquire_lifetime_lock(paths[0])
            second = launcher.acquire_lifetime_lock(paths[1])
            try:
                with self.assertRaises(launcher.ServiceLaunchError):
                    launcher.acquire_lifetime_lock(paths[0])
                self.assertNotEqual(os.fstat(first).st_ino, os.fstat(second).st_ino)
            finally:
                os.close(second)
                os.close(first)

    def test_identity_publishes_fixed_interpreter_and_replaces_stale_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "state").mkdir(mode=0o700)
            path = root / "state/service.identity.json"
            path.write_text("stale", encoding="utf-8")
            path.chmod(0o600)
            with patch.object(launcher.sys, "executable", "/selected/env/bin/python"):
                identity = launcher.publish_service_identity(root)
            self.assertEqual(json.loads(path.read_text()), {
                "format": 1, "pid": os.getpid(), "python": "/selected/env/bin/python",
                "prefix": str(Path(sys.prefix).resolve()), "root": str(root),
            })
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(identity, (path.stat().st_dev, path.stat().st_ino))
            launcher.clear_own_service_identity(root, identity)
            self.assertFalse(path.exists())

    def test_identity_cleanup_preserves_replaced_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "state").mkdir(mode=0o700)
            identity = launcher.publish_service_identity(root)
            path = root / "state/service.identity.json"
            replacement = root / "state/replacement"
            replacement.write_text("new owner's evidence", encoding="utf-8")
            replacement.replace(path)
            launcher.clear_own_service_identity(root, identity)
            self.assertEqual(path.read_text(), "new owner's evidence")

    def test_identity_rejects_symlinks_and_unsafe_markers(self) -> None:
        for kind in ("symlink", "mode", "directory", "hardlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                (root / "state").mkdir(mode=0o700)
                path = root / "state/service.identity.json"
                target = root / "unrelated"
                target.write_text("preserve", encoding="utf-8")
                target.chmod(0o600)
                if kind == "symlink":
                    path.symlink_to(target)
                elif kind == "directory":
                    path.mkdir()
                elif kind == "hardlink":
                    os.link(target, path)
                else:
                    path.write_text("preserve", encoding="utf-8")
                    path.chmod(0o644)
                with self.assertRaisesRegex(launcher.ServiceLaunchError, "unsafe service identity"):
                    launcher.publish_service_identity(root)
                self.assertEqual(target.read_text(), "preserve")

    def test_failed_launch_removes_only_its_identity_before_releasing_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "state").mkdir(mode=0o700)
            marker = root / INSTANCE_ROOT_MARKER
            marker.write_bytes(INSTANCE_ROOT_MARKER_CONTENT)
            marker.chmod(0o600)
            with (
                patch.dict(launcher.os.environ, {}, clear=True),
                patch.object(launcher, "_launch_with_lifetime_lock", side_effect=RuntimeError("profile failed")),
                self.assertRaisesRegex(RuntimeError, "profile failed"),
            ):
                launcher.launch(root)
            self.assertFalse((root / "state/service.identity.json").exists())
            descriptor = launcher.acquire_lifetime_lock(root / "state/service.lifetime.lock")
            os.close(descriptor)


if __name__ == "__main__":
    unittest.main()
