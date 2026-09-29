from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from netizen_cli import cli_setup as setup
from netizen_cli.cli_data import ensure_instance_root
from netizen_cli.lark_app import encode_lark_app, load_lark_app


class TerminalInput(io.StringIO):
    def isatty(self) -> bool:
        return True


class SetupFileTests(unittest.TestCase):
    def test_private_atomic_write_preserves_parent_mode_and_previous_file_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parent = Path(raw)
            parent.chmod(0o750)
            target = parent / "config"
            setup._write_atomic(target, b"previous", mode=0o600)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(parent.stat().st_mode & 0o777, 0o750)
            with patch.object(setup.os, "replace", side_effect=OSError("failed")):
                with self.assertRaises(OSError):
                    setup._write_atomic(target, b"replacement", mode=0o600)
            self.assertEqual(target.read_bytes(), b"previous")
            self.assertEqual(list(parent.iterdir()), [target])

    def test_atomic_write_rejects_symlink_parent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parent = Path(raw)
            link = parent / "link"
            link.symlink_to(parent, target_is_directory=True)
            with self.assertRaises(setup.SetupError):
                setup._write_atomic(link / "config", b"secret", mode=0o600)
            self.assertFalse((parent / "config").exists())

    def test_permission_failure_suggests_exact_app_repair_without_echoing_secret(self) -> None:
        runner = Mock(return_value=SimpleNamespace(returncode=1, stdout="private-secret", stderr="private-secret"))
        with self.assertRaisesRegex(setup.SetupError, "keep appId") as caught:
            setup._missing_permissions(Path("/instance"), runner=runner)
        self.assertNotIn("private-secret", str(caught.exception))


class CodexSetupLoginTests(unittest.TestCase):
    def test_uses_selected_environment_without_running_shell_profile(self) -> None:
        account = SimpleNamespace(pw_dir="/home/user", pw_name="user", pw_shell="/bin/bash")
        runner = Mock(return_value=SimpleNamespace(returncode=0))
        for selected in (None, "/tmp/selected-codex"):
            source = {"PATH": "/caller/tools:/bin", "HOME": "/wrong-home",
                      "PYTHONPATH": "/wrong/imports", "__PYVENV_LAUNCHER__": "/wrong/python"}
            if selected:
                source["CODEX_HOME"] = selected
            with (
                self.subTest(selected=selected),
                patch.dict(os.environ, source, clear=True),
                patch.object(setup.pwd, "getpwuid", return_value=account),
                patch("netizen_cli.service_launcher.capture_profile_environment") as capture,
            ):
                setup.require_codex_login(runner=runner)
                capture.assert_not_called()
                environment = runner.call_args.kwargs["env"]
                self.assertEqual(environment["CODEX_HOME"], selected or "/home/user/.codex")
                self.assertEqual(environment["HOME"], "/home/user")
                self.assertEqual(environment["PATH"], "/caller/tools:/bin")
                self.assertNotIn("PYTHONPATH", environment)
                self.assertNotIn("__PYVENV_LAUNCHER__", environment)
                self.assertEqual(runner.call_args.kwargs["cwd"], "/home/user")
                self.assertEqual(runner.call_args.kwargs["timeout"], 30)

    def test_login_failure_never_repeats_captured_output(self) -> None:
        runner = Mock(return_value=SimpleNamespace(returncode=1, stdout="secret", stderr="secret"))
        with self.assertRaises(setup.SetupError) as caught:
            setup.require_codex_login(runner=runner)
        self.assertNotIn("secret", str(caught.exception))


class ConfigurationSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="netizen-setup-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / ".netizen"
        ensure_instance_root(self.root)
        self.output = io.StringIO()
        self.enterContext(contextlib.redirect_stderr(self.output))

    def incomplete_profile(self, app_id: str) -> None:
        profile = self.root / "lark-app/config.json"
        profile.write_bytes(encode_lark_app(app_id, ""))
        profile.chmod(0o600)

    def test_tty_browser_failure_has_one_manual_fallback_without_echoing_error(self) -> None:
        with (
            patch.object(setup, "_register", side_effect=setup.SetupError("private SDK response")) as browser,
            patch.object(setup, "_missing_permissions", return_value=[]) as permissions,
        ):
            setup.prepare_configuration(self.root, input_stream=TerminalInput("\ncli_manual\n"),
                                        secret_prompt=lambda _: "manual-secret")
        browser.assert_called_once()
        permissions.assert_called_once()
        self.assertEqual(load_lark_app(self.root / "lark-app/config.json").app_id, "cli_manual")
        self.assertIn("did not complete", self.output.getvalue())
        self.assertNotIn("private SDK response", self.output.getvalue())
        self.assertNotIn("manual-secret", self.output.getvalue())

    def test_exact_app_repair_fallback_keeps_the_requested_app(self) -> None:
        self.incomplete_profile("cli_existing")
        with (
            patch.object(setup, "_register", side_effect=setup.SetupError("failed")) as browser,
            patch.object(setup, "_missing_permissions", return_value=[]),
        ):
            setup.prepare_configuration(self.root, input_stream=TerminalInput("\n"),
                                        secret_prompt=lambda _: "replacement-secret")
        self.assertEqual(browser.call_args.args[1], "cli_existing")
        self.assertEqual(load_lark_app(self.root / "lark-app/config.json").app_id, "cli_existing")
        self.assertNotIn("Feishu App ID:", self.output.getvalue())

    def test_non_tty_failure_does_not_prompt_and_preserves_repair_intent(self) -> None:
        self.incomplete_profile("cli_existing")
        prompt = Mock(side_effect=AssertionError("must not prompt"))
        with (
            patch.object(setup, "_register", side_effect=setup.SetupError("failed")) as browser,
            patch.object(setup, "_missing_permissions") as permissions,
            self.assertRaises(setup.SetupError),
        ):
            setup.prepare_configuration(self.root, input_stream=io.StringIO(), secret_prompt=prompt)
        browser.assert_called_once()
        prompt.assert_not_called()
        permissions.assert_not_called()
        credential = load_lark_app(self.root / "lark-app/config.json", allow_incomplete=True)
        self.assertEqual((credential.app_id, credential.app_secret), ("cli_existing", ""))

    def test_cancelled_browser_never_falls_back_to_manual(self) -> None:
        prompt = Mock(side_effect=AssertionError("must not prompt"))
        with (
            patch.object(setup, "_register", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            setup.prepare_configuration(self.root, input_stream=TerminalInput("\n"), secret_prompt=prompt)
        prompt.assert_not_called()

    def test_manual_fallback_does_not_start_a_second_browser_for_missing_permissions(self) -> None:
        with (
            patch.object(setup, "_register", side_effect=setup.SetupError("failed")) as browser,
            patch.object(setup, "_missing_permissions", return_value=["im:message"]),
            self.assertRaisesRegex(setup.SetupError, "tenant approval"),
        ):
            setup.prepare_configuration(self.root, input_stream=TerminalInput("\ncli_manual\n"),
                                        secret_prompt=lambda _: "manual-secret")
        browser.assert_called_once()
        self.assertEqual(load_lark_app(self.root / "lark-app/config.json").app_id, "cli_manual")

    def test_browser_helper_timeout_does_not_echo_sensitive_output(self) -> None:
        runner = Mock(side_effect=subprocess.TimeoutExpired("helper", 660, output="secret"))
        with self.assertRaises(setup.SetupError) as caught:
            setup._register(self.root, None, runner=runner)
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(runner.call_args.kwargs["timeout"], 660)

    def test_same_admin_port_can_retry_after_browser_failure_without_rewriting_config(self) -> None:
        with (
            patch.object(setup, "_register", side_effect=setup.SetupError("browser timed out")),
            self.assertRaises(setup.SetupError),
        ):
            setup.prepare_configuration(self.root, admin_port=8890, input_stream=io.StringIO())
        config = self.root / "config.yaml"
        before = config.read_bytes()
        before_stat = config.stat()
        with patch.object(setup, "_missing_permissions", return_value=[]):
            setup.prepare_configuration(self.root, admin_port=8890,
                                        input_stream=TerminalInput("2\ncli_retry\n"),
                                        secret_prompt=lambda _: "retry-secret")
        self.assertEqual(config.read_bytes(), before)
        self.assertEqual((config.stat().st_ino, config.stat().st_mtime_ns),
                         (before_stat.st_ino, before_stat.st_mtime_ns))
        self.assertEqual(load_lark_app(self.root / "lark-app/config.json").app_id, "cli_retry")

    def test_different_admin_port_refuses_before_authorization_or_config_changes(self) -> None:
        with (
            patch.object(setup, "_register", side_effect=setup.SetupError("browser timed out")),
            self.assertRaises(setup.SetupError),
        ):
            setup.prepare_configuration(self.root, admin_port=8890, input_stream=io.StringIO())
        config = self.root / "config.yaml"
        before = config.read_bytes()
        with (
            patch.object(setup, "_register") as browser,
            self.assertRaisesRegex(setup.SetupError, "differs from existing configuration"),
        ):
            setup.prepare_configuration(self.root, admin_port=8891, input_stream=io.StringIO())
        browser.assert_not_called()
        self.assertEqual(config.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
