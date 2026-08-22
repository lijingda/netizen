from __future__ import annotations

import io
import json
import os
import shutil
import socket
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import netizen_installer as installer


ROOT = Path(__file__).resolve().parents[1]


class NetizenInstallerTest(unittest.TestCase):
    def _layout(self, root: Path) -> installer.Layout:
        home = root / "home"
        home.mkdir(parents=True, exist_ok=True)
        return installer.resolve_layout(
            environ={},
            account_home=home,
            uid=os.geteuid(),
            username="current-user",
        )

    def test_layout_uses_fixed_current_user_paths_and_ignores_xdg_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "account-home"
            home.mkdir()
            layout = installer.resolve_layout(
                environ={
                    "HOME": str(root / "wrong-home"),
                    "XDG_DATA_HOME": str(root / "xdg-data"),
                    "XDG_CONFIG_HOME": str(root / "xdg-config"),
                    "XDG_STATE_HOME": str(root / "xdg-state"),
                    "XDG_CACHE_HOME": str(root / "xdg-cache"),
                    "CODEX_HOME": str(root / "codex"),
                },
                account_home=home,
                uid=os.geteuid(),
                username="chosen-user",
            )

            self.assertEqual(layout.home, home)
            self.assertEqual(layout.product_root, home / ".netizen")
            self.assertEqual(layout.config_file, home / ".netizen/config.yaml")
            self.assertEqual(
                layout.credentials_dir,
                home / ".netizen/credentials",
            )
            self.assertEqual(
                layout.admin_secret_file,
                home / ".netizen/credentials/admin-web-secret",
            )
            self.assertEqual(layout.state_dir, home / ".netizen/state")
            self.assertEqual(layout.cache_dir, home / ".netizen/cache")
            self.assertEqual(layout.releases, home / ".netizen/releases")
            self.assertEqual(
                layout.unit_file,
                home / ".config/systemd/user/netizen.service",
            )
            self.assertEqual(layout.codex_home, root / "codex")
            self.assertEqual(layout.username, "chosen-user")

    def test_layout_rejects_uninstall_targets_that_overlap_preserved_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()

            with self.assertRaisesRegex(installer.InstallError, "overlap"):
                installer.resolve_layout(
                    environ={
                        "CODEX_HOME": str(home / ".netizen/releases/codex"),
                    },
                    account_home=home,
                    uid=os.geteuid(),
                    username="current-user",
                )

    def test_source_checkout_must_not_overlap_managed_install_or_data_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            layout = installer.resolve_layout(
                environ={},
                account_home=home,
                uid=os.geteuid(),
                username="current-user",
            )
            source = layout.product_root / "checkout"
            source.mkdir(parents=True)

            with self.assertRaisesRegex(installer.InstallError, "source overlaps"):
                installer._validate_source_location(source, layout)

    def test_noninteractive_configuration_prepares_files_without_prompting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._layout(Path(directory))
            installer.prepare_directories(layout)

            for managed_root in (layout.releases, layout.cache_dir):
                marker = managed_root / installer.MANAGED_DIRECTORY_MARKER
                self.assertEqual(
                    marker.read_bytes(),
                    installer.MANAGED_DIRECTORY_MARKER_CONTENT,
                )
                self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)

            with self.assertRaisesRegex(
                installer.ConfigurationRequired,
                "non-interactive install will not prompt",
            ):
                installer.prepare_configuration(layout, interactive=False)

            self.assertIn("cli_REPLACE_ME", layout.config_file.read_text())
            self.assertEqual(layout.secret_file.read_bytes(), b"")
            generated_admin_secret = layout.admin_secret_file.read_bytes()
            self.assertEqual(len(generated_admin_secret), 43)
            self.assertNotIn(b"\n", generated_admin_secret)
            self.assertTrue((layout.home / "projects").is_dir())
            self.assertEqual(stat.S_IMODE(layout.config_file.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(layout.secret_file.stat().st_mode), 0o600)
            self.assertEqual(
                stat.S_IMODE(layout.admin_secret_file.stat().st_mode),
                0o600,
            )

            layout.config_file.write_text(
                layout.config_file.read_text().replace("cli_REPLACE_ME", "cli_agent"),
                encoding="utf-8",
            )
            layout.secret_file.write_text("agent-secret", encoding="utf-8")
            installer.prepare_configuration(layout, interactive=False)
            self.assertEqual(
                layout.admin_secret_file.read_bytes(),
                generated_admin_secret,
            )

    def test_install_lock_lives_in_preserved_state_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._layout(Path(directory))

            with installer.installation_lock(layout):
                self.assertTrue((layout.state_dir / ".install.lock").is_file())
                self.assertFalse((layout.product_root / ".install.lock").exists())

    def test_whitespace_only_secret_is_still_treated_as_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._layout(Path(directory))
            installer.prepare_directories(layout)
            with self.assertRaises(installer.ConfigurationRequired):
                installer.prepare_configuration(layout, interactive=False)
            layout.config_file.write_text(
                layout.config_file.read_text().replace("cli_REPLACE_ME", "cli_agent"),
                encoding="utf-8",
            )
            layout.secret_file.write_text(" \n", encoding="utf-8")

            installer.prepare_configuration(
                layout,
                interactive=True,
                secret_prompt=lambda _prompt: "real-secret",
            )

            self.assertEqual(layout.secret_file.read_text(), "real-secret")

    def test_interactive_configuration_reads_id_and_hidden_secret_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._layout(Path(directory))
            installer.prepare_directories(layout)

            installer.prepare_configuration(
                layout,
                interactive=True,
                input_stream=io.StringIO("not-an-id\ncli_valid\n"),
                secret_prompt=lambda _prompt: "hidden-secret",
            )

            self.assertIn("cli_valid", layout.config_file.read_text())
            self.assertNotIn("hidden-secret", layout.config_file.read_text())
            self.assertEqual(layout.secret_file.read_text(), "hidden-secret")

    def test_interactive_configuration_defaults_to_browser_app_setup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._layout(Path(directory))
            installer.prepare_directories(layout)
            requested_app_ids: list[str | None] = []

            def register_app(app_id: str | None) -> installer.FeishuAppCredentials:
                requested_app_ids.append(app_id)
                return installer.FeishuAppCredentials(
                    app_id="cli_browser",
                    app_secret="browser-secret",
                )

            installer.prepare_configuration(
                layout,
                interactive=True,
                input_stream=io.StringIO("\n"),
                secret_prompt=lambda _prompt: self.fail("manual secret prompt was used"),
                app_registrar=register_app,
            )

            self.assertEqual(requested_app_ids, [None])
            self.assertIn("cli_browser", layout.config_file.read_text())
            self.assertNotIn("browser-secret", layout.config_file.read_text())
            self.assertEqual(layout.secret_file.read_text(), "browser-secret")
            self.assertEqual(stat.S_IMODE(layout.config_file.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(layout.secret_file.stat().st_mode), 0o600)

    def test_browser_setup_updates_an_existing_app_with_a_missing_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._layout(Path(directory))
            installer.prepare_directories(layout)
            with self.assertRaises(installer.ConfigurationRequired):
                installer.prepare_configuration(layout, interactive=False)
            layout.config_file.write_text(
                layout.config_file.read_text().replace(
                    "cli_REPLACE_ME",
                    "cli_existing",
                ),
                encoding="utf-8",
            )
            requested_app_ids: list[str | None] = []

            def register_app(app_id: str | None) -> installer.FeishuAppCredentials:
                requested_app_ids.append(app_id)
                return installer.FeishuAppCredentials(
                    app_id="cli_existing",
                    app_secret="recovered-secret",
                )

            installer.prepare_configuration(
                layout,
                interactive=True,
                input_stream=io.StringIO("\n"),
                app_registrar=register_app,
            )

            self.assertEqual(requested_app_ids, ["cli_existing"])
            self.assertEqual(layout.secret_file.read_text(), "recovered-secret")
            self.assertEqual(
                layout.config_file.read_text().count("cli_existing"),
                1,
            )

    def test_browser_setup_failure_falls_back_to_manual_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._layout(Path(directory))
            installer.prepare_directories(layout)

            def fail_registration(_app_id: str | None) -> installer.FeishuAppCredentials:
                raise installer.InstallError("private SDK detail")

            with patch("sys.stderr", new=io.StringIO()) as stderr:
                installer.prepare_configuration(
                    layout,
                    interactive=True,
                    input_stream=io.StringIO("\ncli_manual\n"),
                    secret_prompt=lambda _prompt: "manual-secret",
                    app_registrar=fail_registration,
                )

            self.assertIn("did not complete", stderr.getvalue())
            self.assertNotIn("private SDK detail", stderr.getvalue())
            self.assertIn("cli_manual", layout.config_file.read_text())
            self.assertEqual(layout.secret_file.read_text(), "manual-secret")

    def test_browser_credentials_roll_back_both_files_on_write_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._layout(Path(directory))
            installer.prepare_directories(layout)
            with self.assertRaises(installer.ConfigurationRequired):
                installer.prepare_configuration(layout, interactive=False)
            original_config = layout.config_file.read_bytes()
            original_secret = layout.secret_file.read_bytes()
            real_write = installer._write_atomic

            def fail_new_secret(path: Path, content: bytes, *, mode: int) -> None:
                if path == layout.secret_file and content == b"new-secret":
                    raise OSError("simulated write failure")
                real_write(path, content, mode=mode)

            with (
                patch.object(installer, "_write_atomic", side_effect=fail_new_secret),
                self.assertRaisesRegex(OSError, "simulated write failure"),
            ):
                installer._store_registered_feishu_credentials(
                    layout,
                    config_text=original_config.decode(),
                    expected_app_id=None,
                    credentials=installer.FeishuAppCredentials(
                        app_id="cli_new",
                        app_secret="new-secret",
                    ),
                )

            self.assertEqual(layout.config_file.read_bytes(), original_config)
            self.assertEqual(layout.secret_file.read_bytes(), original_secret)

    def test_release_app_registrar_keeps_secret_out_of_command_and_errors(self) -> None:
        release = installer.Release(
            digest="a" * 64,
            root=ROOT,
            source=ROOT,
            venv=ROOT / ".venv",
        )
        calls: list[tuple[list[str], dict[str, object]]] = []

        def fake_runner(
            argv: list[object],
            **kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            rendered = [os.fspath(value) for value in argv]
            calls.append((rendered, kwargs))
            return subprocess.CompletedProcess(
                rendered,
                0,
                json.dumps(
                    {
                        "version": 1,
                        "appId": "cli_existing",
                        "appSecret": "sdk-secret",
                    }
                ),
                None,
            )

        credentials = installer._register_feishu_app_from_release(
            release,
            "cli_existing",
            runner=fake_runner,
        )

        self.assertEqual(credentials.app_id, "cli_existing")
        self.assertEqual(credentials.app_secret, "sdk-secret")
        self.assertNotIn("sdk-secret", repr(credentials))
        command, kwargs = calls[0]
        self.assertNotIn("sdk-secret", command)
        self.assertEqual(command[-2:], ["--app-id", "cli_existing"])
        self.assertIs(kwargs["capture_stdout"], True)
        self.assertIs(kwargs["check"], False)
        self.assertEqual(kwargs["timeout"], 660.0)

        def malformed_runner(
            argv: list[object],
            **_kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 0, "sdk-secret", None)

        with self.assertRaises(installer.InstallError) as raised:
            installer._register_feishu_app_from_release(
                release,
                None,
                runner=malformed_runner,
            )
        self.assertNotIn("sdk-secret", str(raised.exception))

    def test_noninteractive_install_does_not_build_before_credentials_are_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._layout(Path(directory))
            with (
                patch.object(installer, "require_linux"),
                patch.object(installer, "prepare_release") as prepare_release,
                patch.object(installer, "info") as installer_info,
                self.assertRaises(installer.ConfigurationRequired),
            ):
                installer.install(
                    source_root=ROOT,
                    layout=layout,
                    interactive=False,
                )

            prepare_release.assert_not_called()
            installer_info.assert_not_called()
            self.assertIn("cli_REPLACE_ME", layout.config_file.read_text())
            self.assertEqual(layout.secret_file.read_bytes(), b"")

    def test_interactive_install_builds_candidate_before_browser_setup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._layout(Path(directory))
            release = installer.Release(
                digest="b" * 64,
                root=ROOT,
                source=ROOT,
                venv=ROOT / ".venv",
            )
            events: list[str] = []
            messages: list[str] = []
            expected_warning = (
                "Feishu/Lark credentials are incomplete; interactive setup follows "
                "release preparation. Agent/CI callers should cancel now and rerun "
                "./install.sh </dev/null."
            )

            def prepare_release(*_args: object, **_kwargs: object) -> installer.Release:
                self.assertIn("cli_REPLACE_ME", layout.config_file.read_text())
                self.assertEqual(messages, [expected_warning])
                events.append("release")
                return release

            def register_app(
                selected_release: installer.Release,
                app_id: str | None,
                **_kwargs: object,
            ) -> installer.FeishuAppCredentials:
                self.assertEqual(selected_release, release)
                self.assertIsNone(app_id)
                self.assertEqual(events, ["release"])
                events.append("register")
                return installer.FeishuAppCredentials(
                    app_id="cli_installed",
                    app_secret="installed-secret",
                )

            with (
                patch.object(installer, "require_linux"),
                patch.object(
                    installer,
                    "prepare_release",
                    side_effect=prepare_release,
                ),
                patch.object(
                    installer,
                    "_register_feishu_app_from_release",
                    side_effect=register_app,
                ),
                patch.object(
                    installer,
                    "validate_runtime",
                    return_value=installer.RuntimeValidation(
                        data_dir=layout.state_dir,
                        admin_bind=installer.AdminBind(False, "127.0.0.1", 8787),
                    ),
                ),
                patch.object(installer, "ensure_linger"),
                patch.object(installer, "activate_release"),
                patch.object(installer, "info", side_effect=messages.append),
                patch("sys.stdin", new=io.StringIO("\n")),
            ):
                installed = installer.install(
                    source_root=ROOT,
                    layout=layout,
                    interactive=True,
                )

            self.assertEqual(installed, release)
            self.assertEqual(events, ["release", "register"])
            self.assertEqual(messages[0], expected_warning)
            self.assertIn("cli_installed", layout.config_file.read_text())
            self.assertEqual(layout.secret_file.read_text(), "installed-secret")

    def test_admin_secret_is_preserved_and_invalid_existing_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._layout(Path(directory))
            installer.prepare_directories(layout)
            with self.assertRaises(installer.ConfigurationRequired):
                installer.prepare_configuration(layout, interactive=False)
            original = layout.admin_secret_file.read_bytes()
            layout.config_file.write_text(
                layout.config_file.read_text().replace(
                    "cli_REPLACE_ME", "cli_agent"
                ),
                encoding="utf-8",
            )
            layout.secret_file.write_text("agent-secret", encoding="utf-8")

            installer.prepare_configuration(layout, interactive=False)
            self.assertEqual(layout.admin_secret_file.read_bytes(), original)

            layout.admin_secret_file.chmod(0o640)
            with self.assertRaisesRegex(installer.InstallError, "exactly 0600"):
                installer.prepare_configuration(layout, interactive=False)
            layout.admin_secret_file.chmod(0o600)
            layout.admin_secret_file.write_text("not-canonical", encoding="ascii")
            with self.assertRaisesRegex(installer.InstallError, "canonical"):
                installer.prepare_configuration(layout, interactive=False)

    def test_admin_bind_preflight_rejects_an_occupied_port(self) -> None:
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(blocker.close)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]

        with self.assertRaisesRegex(installer.InstallError, "already in use"):
            installer.preflight_admin_bind(
                installer.AdminBind(True, "127.0.0.1", port)
            )
        installer.preflight_admin_bind(
            installer.AdminBind(False, "127.0.0.1", port)
        )

    def test_source_snapshot_is_content_addressed_and_excludes_caches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first"
            second = Path(directory) / "second"

            first_digest, first_count = installer.snapshot_source(ROOT, first)
            second_digest, second_count = installer.snapshot_source(ROOT, second)

            self.assertEqual(first_digest, second_digest)
            self.assertEqual(first_count, second_count)
            self.assertTrue((first / "install.sh").is_file())
            self.assertTrue((first / ".gitignore").is_file())
            self.assertTrue((first / "LOCAL_ENVIRONMENT.example.md").is_file())
            self.assertFalse((first / "LOCAL_ENVIRONMENT.md").exists())
            self.assertTrue((first / "scripts/netizen_installer.py").is_file())
            self.assertTrue((first / "scripts/netizen_service_launcher.py").is_file())
            self.assertFalse(any(path.name == "__pycache__" for path in first.rglob("*")))

    def test_release_build_is_content_addressed_and_reused_only_after_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._layout(Path(directory))
            installer.prepare_directories(layout)
            calls: list[list[str]] = []

            def fake_runner(argv: list[object], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                rendered = [os.fspath(value) for value in argv]
                calls.append(rendered)
                if len(rendered) >= 4 and rendered[1:3] == ["-m", "venv"]:
                    candidate_bin = Path(rendered[3]) / "bin"
                    candidate_bin.mkdir(parents=True)
                    (candidate_bin / "python").write_text("candidate", encoding="utf-8")
                return subprocess.CompletedProcess(rendered, 0, "", "")

            first = installer.prepare_release(layout, source_root=ROOT, runner=fake_runner)

            self.assertEqual(first.root.name, first.digest)
            self.assertTrue((first.root / installer.RELEASE_METADATA).is_file())
            self.assertTrue((first.source / "netizen/main.py").is_file())
            self.assertTrue(any(call[1:3] == ["-m", "venv"] for call in calls))

            calls.clear()
            second = installer.prepare_release(layout, source_root=ROOT, runner=fake_runner)

            self.assertEqual(second, first)
            self.assertFalse(any(call[1:3] == ["-m", "venv"] for call in calls))
            self.assertTrue(any(call[-2:] == ["pip", "check"] for call in calls))

    def test_user_unit_has_no_user_or_sudo_and_targets_current_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._layout(Path(directory))
            release = installer.Release(
                digest="a" * 64,
                root=ROOT,
                source=ROOT,
                venv=ROOT / ".venv",
            )

            unit = installer.render_user_unit(release, layout)

            self.assertNotIn("User=", unit)
            self.assertNotIn("Group=", unit)
            self.assertNotIn("sudo", unit)
            self.assertIn("WantedBy=default.target", unit)
            self.assertIn("WorkingDirectory=%h", unit)
            self.assertNotIn("EnvironmentFile=", unit)
            self.assertNotIn("ExecStartPre=", unit)
            self.assertNotIn("service.env", unit)
            self.assertNotIn('WorkingDirectory="', unit)
            self.assertIn(str(layout.current / "venv/bin/python"), unit)
            self.assertIn(
                str(layout.current / "source/scripts/netizen_service_launcher.py"),
                unit,
            )
            self.assertIn(" -E -B -u ", unit)
            self.assertIn(str(layout.home / ".local/bin"), unit)
            self.assertIn("KillMode=control-group", unit)
            self.assertIn(
                "UnsetEnvironment=FEISHU_APP_SECRET NETIZEN_ADMIN_SECRET ",
                unit,
            )
            self.assertIn(str(layout.admin_secret_file), unit)
            self.assertIn("TimeoutStopSec=75s", unit)

    def test_public_shell_scripts_reject_unsupported_arguments_before_installing(self) -> None:
        cases = (
            ("install.sh", ["unexpected"]),
            ("uninstall.sh", ["unexpected"]),
            ("service.sh", []),
            ("service.sh", ["enable"]),
        )
        for script, arguments in cases:
            with self.subTest(script=script, arguments=arguments):
                result = subprocess.run(
                    ["sh", ROOT / script, *arguments],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn("usage:", result.stderr)
        for script in ("install.sh", "service.sh", "uninstall.sh"):
            with self.subTest(executable=script):
                self.assertTrue((ROOT / script).stat().st_mode & stat.S_IXUSR)

    def test_noninteractive_linger_setup_returns_an_actionable_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._layout(Path(directory))
            calls: list[list[str]] = []

            def fake_runner(argv: list[object], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                rendered = [os.fspath(value) for value in argv]
                calls.append(rendered)
                return subprocess.CompletedProcess(rendered, 0, "no\n", "")

            with patch.object(installer.os, "geteuid", return_value=501):
                with self.assertRaisesRegex(
                    installer.InstallError,
                    r"sudo loginctl enable-linger current-user",
                ):
                    installer.ensure_linger(
                        layout,
                        interactive=False,
                        runner=fake_runner,
                    )

            self.assertEqual(
                calls,
                [["loginctl", "show-user", str(layout.uid), "--property=Linger", "--value"]],
            )

    def test_service_action_invokes_only_systemd_user_manager(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._layout(Path(directory))
            layout.unit_file.parent.mkdir(parents=True)
            layout.unit_file.write_text(installer.UNIT_MARKER, encoding="utf-8")
            calls: list[list[str]] = []

            def fake_runner(argv: list[object], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                rendered = [os.fspath(value) for value in argv]
                calls.append(rendered)
                return subprocess.CompletedProcess(rendered, 0, "", "")

            with (
                patch.object(installer, "require_linux"),
                patch.object(installer, "_wait_for_ready") as wait_for_ready,
            ):
                code = installer.service_action(
                    "restart",
                    layout=layout,
                    runner=fake_runner,
                )

            self.assertEqual(code, 0)
            self.assertEqual(calls, [["systemctl", "--user", "restart", "netizen.service"]])
            self.assertNotIn("sudo", calls[0])
            wait_for_ready.assert_called_once()
            self.assertEqual(
                wait_for_ready.call_args.kwargs["timeout"],
                installer.SERVICE_READY_TIMEOUT_SECONDS,
            )

    def test_service_restart_surfaces_a_post_systemctl_ready_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._layout(Path(directory))
            layout.unit_file.parent.mkdir(parents=True)
            layout.unit_file.write_text(installer.UNIT_MARKER, encoding="utf-8")
            calls: list[list[str]] = []

            def fake_runner(
                argv: list[object],
                **_kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                rendered = [os.fspath(value) for value in argv]
                calls.append(rendered)
                return subprocess.CompletedProcess(rendered, 0, "", "")

            with (
                patch.object(installer, "require_linux"),
                patch.object(
                    installer,
                    "_wait_for_ready",
                    side_effect=installer.InstallError("profile failed"),
                ),
                self.assertRaisesRegex(installer.InstallError, "profile failed"),
            ):
                installer.service_action(
                    "restart",
                    layout=layout,
                    runner=fake_runner,
                )

            self.assertEqual(
                calls,
                [["systemctl", "--user", "restart", "netizen.service"]],
            )

    def test_service_start_is_idempotent_when_unit_is_already_active(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._layout(Path(directory))
            layout.unit_file.parent.mkdir(parents=True)
            layout.unit_file.write_text(installer.UNIT_MARKER, encoding="utf-8")
            calls: list[list[str]] = []

            def fake_runner(
                argv: list[object],
                **_kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                rendered = [os.fspath(value) for value in argv]
                calls.append(rendered)
                return subprocess.CompletedProcess(rendered, 0, "active\n", "")

            with (
                patch.object(installer, "require_linux"),
                patch.object(installer, "_wait_for_ready") as wait_for_ready,
            ):
                code = installer.service_action(
                    "start",
                    layout=layout,
                    runner=fake_runner,
                )

            self.assertEqual(code, 0)
            self.assertEqual(
                calls,
                [["systemctl", "--user", "is-active", "netizen.service"]],
            )
            wait_for_ready.assert_not_called()

    def test_missing_user_unit_is_a_normal_first_install_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._layout(Path(directory))
            calls: list[list[str]] = []

            def fake_runner(
                argv: list[object],
                **_kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                rendered = [os.fspath(value) for value in argv]
                calls.append(rendered)
                if "show-environment" in rendered:
                    return subprocess.CompletedProcess(rendered, 0, "", "")
                return subprocess.CompletedProcess(
                    rendered,
                    1,
                    "",
                    "Unit netizen.service could not be found.\n",
                )

            self.assertEqual(
                installer._user_service_state(layout, fake_runner),
                (False, False),
            )
            self.assertEqual(calls[0][2], "show-environment")

    def test_custom_systemd_unit_search_path_must_include_the_fixed_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            layout = self._layout(root)

            with self.assertRaisesRegex(installer.InstallError, "XDG_CONFIG_HOME"):
                installer._validate_user_unit_search_path(
                    layout,
                    f"XDG_CONFIG_HOME={root / 'custom-config'}\n",
                )

            installer._validate_user_unit_search_path(
                layout,
                "XDG_CONFIG_HOME=/custom\n"
                f"SYSTEMD_UNIT_PATH={layout.unit_dir}\n",
            )

    def test_systemd_manager_environment_escapes_are_decoded_without_a_shell(self) -> None:
        environment = installer._parse_systemd_manager_environment(
            "PLAIN=value\n"
            "EQUAL=a=b=c\n"
            "SPACE=$'hello world'\n"
            "QUOTE=$'say \\'hi\\''\n"
            "SLASH=$'C:\\\\tools'\n"
            "MULTI=$'line1\\nline2'\n"
            "HEX=$'a\\x20b'\n"
        )

        self.assertEqual(environment["PLAIN"], "value")
        self.assertEqual(environment["EQUAL"], "a=b=c")
        self.assertEqual(environment["SPACE"], "hello world")
        self.assertEqual(environment["QUOTE"], "say 'hi'")
        self.assertEqual(environment["SLASH"], "C:\\tools")
        self.assertEqual(environment["MULTI"], "line1\nline2")
        self.assertEqual(environment["HEX"], "a b")

        with self.assertRaisesRegex(installer.InstallError, "invalid escaped"):
            installer._parse_systemd_manager_environment("TOKEN=$'unterminated\n")

    def test_service_action_refuses_an_unrecognized_same_name_unit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._layout(Path(directory))
            layout.unit_file.parent.mkdir(parents=True)
            layout.unit_file.write_text("[Service]\nExecStart=/bin/other\n", encoding="utf-8")

            with patch.object(installer, "require_linux"):
                with self.assertRaisesRegex(installer.InstallError, "unrecognized user unit"):
                    installer.service_action("stop", layout=layout)

    def test_activation_refuses_an_orphaned_active_same_name_user_unit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            layout = self._layout(root)
            installer.prepare_directories(layout)
            candidate = self._release(layout, "c" * 64)

            with patch.object(
                installer,
                "_user_service_state",
                return_value=(True, True),
            ):
                with self.assertRaisesRegex(installer.InstallError, "inspect it"):
                    installer.activate_release(
                        candidate,
                        layout,
                        interactive=False,
                    )

            self.assertFalse(layout.current.exists())

    def test_failed_activation_restores_release_unit_and_skill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            layout = self._layout(root)
            installer.prepare_directories(layout)
            old = self._release(layout, "1" * 64)
            candidate = self._release(layout, "2" * 64)
            installer._set_release_link(layout.current, old.root, layout)
            old_unit_text = installer.UNIT_MARKER + "\n# old unit\n"
            layout.unit_file.write_text(old_unit_text, encoding="utf-8")
            old_skill = layout.codex_home / "skills/netizen-user-guide"
            old_skill.mkdir(parents=True)
            (old_skill / "SKILL.md").write_text("old skill\n", encoding="utf-8")
            database = layout.state_dir / "channel.sqlite3"
            database.write_text("old database", encoding="utf-8")
            calls: list[list[str]] = []
            ready_attempts = 0

            def fake_runner(argv: list[object], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                rendered = [os.fspath(value) for value in argv]
                calls.append(rendered)
                return subprocess.CompletedProcess(rendered, 0, "active\n", "")

            def fail_candidate_once(*_args: object, **_kwargs: object) -> None:
                nonlocal ready_attempts
                ready_attempts += 1
                if ready_attempts == 1:
                    database.write_text("candidate database", encoding="utf-8")
                    raise installer.InstallError("candidate failed")

            with (
                patch.object(installer, "_user_service_state", return_value=(True, True)),
                patch.object(installer, "inspect_legacy_service", return_value=installer.LegacyServiceState()),
                patch.object(
                    installer,
                    "_wait_for_ready",
                    side_effect=fail_candidate_once,
                ),
            ):
                with self.assertRaisesRegex(installer.InstallError, "rolled back"):
                    installer.activate_release(
                        candidate,
                        layout,
                        interactive=False,
                        runner=fake_runner,
                        data_dir=layout.state_dir,
                    )

            self.assertEqual(installer._read_release_link(layout.current, layout), old.root.resolve())
            self.assertEqual(layout.unit_file.read_text(), old_unit_text)
            self.assertEqual((old_skill / "SKILL.md").read_text(), "old skill\n")
            self.assertEqual(database.read_text(), "old database")
            self.assertFalse((layout.state_dir / installer.ACTIVATION_INTENT).exists())
            self.assertGreaterEqual(
                sum(call[:3] == ["systemctl", "--user", "stop"] for call in calls),
                2,
            )
            self.assertIn(["systemctl", "--user", "start", "netizen.service"], calls)

    def test_failed_skill_rollback_preserves_a_recovery_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            layout = self._layout(root)
            installer.prepare_directories(layout)
            old = self._release(layout, "7" * 64)
            candidate = self._release(layout, "8" * 64)
            installer._set_release_link(layout.current, old.root, layout)
            layout.unit_file.write_text(installer.UNIT_MARKER, encoding="utf-8")
            old_skill = layout.codex_home / "skills/netizen-user-guide"
            old_skill.mkdir(parents=True)
            (old_skill / "SKILL.md").write_text("recovery content", encoding="utf-8")

            def fake_runner(argv: list[object], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                rendered = [os.fspath(value) for value in argv]
                return subprocess.CompletedProcess(rendered, 0, "active\n", "")

            with (
                patch.object(installer, "_user_service_state", return_value=(True, True)),
                patch.object(installer, "inspect_legacy_service", return_value=installer.LegacyServiceState()),
                patch.object(
                    installer,
                    "_wait_for_ready",
                    side_effect=installer.InstallError("candidate failed"),
                ),
                patch.object(
                    installer,
                    "_restore_skill",
                    side_effect=installer.InstallError("restore failed"),
                ),
            ):
                with self.assertRaisesRegex(installer.InstallError, "snapshot preserved"):
                    installer.activate_release(
                        candidate,
                        layout,
                        interactive=False,
                        runner=fake_runner,
                    )

            recoveries = list(layout.state_dir.glob("rollback-recovery-*"))
            self.assertEqual(len(recoveries), 1)
            self.assertEqual(
                (recoveries[0] / "netizen-user-guide/SKILL.md").read_text(),
                "recovery content",
            )

    def test_stopped_upgrade_stays_stopped_and_retains_previous_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            layout = self._layout(root)
            installer.prepare_directories(layout)
            old = self._release(layout, "3" * 64)
            candidate = self._release(layout, "4" * 64)
            installer._set_release_link(layout.current, old.root, layout)
            layout.unit_file.write_text(installer.UNIT_MARKER, encoding="utf-8")
            calls: list[list[str]] = []

            def fake_runner(argv: list[object], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                rendered = [os.fspath(value) for value in argv]
                calls.append(rendered)
                return subprocess.CompletedProcess(rendered, 0, "inactive\n", "")

            with (
                patch.object(installer, "_user_service_state", return_value=(False, True)),
                patch.object(installer, "inspect_legacy_service", return_value=installer.LegacyServiceState()),
            ):
                installer.activate_release(
                    candidate,
                    layout,
                    interactive=False,
                    runner=fake_runner,
                )

            self.assertEqual(
                installer._read_release_link(layout.current, layout),
                candidate.root.resolve(),
            )
            self.assertEqual(
                installer._read_release_link(layout.previous, layout),
                old.root.resolve(),
            )
            self.assertNotIn(["systemctl", "--user", "start", "netizen.service"], calls)

    def test_disabled_stopped_upgrade_remains_disabled_and_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            layout = self._layout(root)
            installer.prepare_directories(layout)
            old = self._release(layout, "9" * 64)
            candidate = self._release(layout, "a" * 64)
            installer._set_release_link(layout.current, old.root, layout)
            calls: list[list[str]] = []

            def fake_runner(argv: list[object], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                rendered = [os.fspath(value) for value in argv]
                calls.append(rendered)
                return subprocess.CompletedProcess(rendered, 0, "inactive\n", "")

            with (
                patch.object(installer, "_user_service_state", return_value=(False, False)),
                patch.object(installer, "inspect_legacy_service", return_value=installer.LegacyServiceState()),
            ):
                installer.activate_release(
                    candidate,
                    layout,
                    interactive=False,
                    runner=fake_runner,
                )

            self.assertNotIn(["systemctl", "--user", "enable", "netizen.service"], calls)
            self.assertNotIn(["systemctl", "--user", "start", "netizen.service"], calls)

    def test_first_install_starts_service_and_has_no_previous_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            layout = self._layout(root)
            installer.prepare_directories(layout)
            candidate = self._release(layout, "5" * 64)
            calls: list[list[str]] = []

            def fake_runner(argv: list[object], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                rendered = [os.fspath(value) for value in argv]
                calls.append(rendered)
                return subprocess.CompletedProcess(rendered, 0, "inactive\n", "")

            with (
                patch.object(installer, "_user_service_state", return_value=(False, False)),
                patch.object(installer, "inspect_legacy_service", return_value=installer.LegacyServiceState()),
                patch.object(installer, "_wait_for_ready"),
            ):
                installer.activate_release(
                    candidate,
                    layout,
                    interactive=False,
                    runner=fake_runner,
                )

            self.assertIn(["systemctl", "--user", "start", "netizen.service"], calls)
            self.assertIn(["systemctl", "--user", "enable", "netizen.service"], calls)
            self.assertEqual(
                installer._read_release_link(layout.current, layout),
                candidate.root.resolve(),
            )
            self.assertIsNone(installer._read_release_link(layout.previous, layout))

    def test_interrupted_activation_intent_is_honored_on_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            layout = self._layout(root)
            installer.prepare_directories(layout)
            candidate = self._release(layout, "d" * 64)
            installer._set_release_link(layout.current, candidate.root, layout)
            layout.unit_file.write_text(installer.UNIT_MARKER, encoding="utf-8")
            installer._write_activation_intent(
                layout,
                candidate,
                should_start=True,
                should_enable=True,
            )
            calls: list[list[str]] = []

            def fake_runner(
                argv: list[object],
                **_kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                rendered = [os.fspath(value) for value in argv]
                calls.append(rendered)
                return subprocess.CompletedProcess(rendered, 0, "inactive\n", "")

            with (
                patch.object(installer, "_user_service_state", return_value=(False, False)),
                patch.object(
                    installer,
                    "inspect_legacy_service",
                    return_value=installer.LegacyServiceState(),
                ),
                patch.object(installer, "_wait_for_ready"),
            ):
                installer.activate_release(
                    candidate,
                    layout,
                    interactive=False,
                    runner=fake_runner,
                )

            self.assertIn(
                ["systemctl", "--user", "enable", "netizen.service"],
                calls,
            )
            self.assertIn(
                ["systemctl", "--user", "start", "netizen.service"],
                calls,
            )
            self.assertFalse((layout.state_dir / installer.ACTIVATION_INTENT).exists())

    def test_interrupted_upgrade_keeps_original_rollback_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            layout = self._layout(root)
            installer.prepare_directories(layout)
            old = self._release(layout, "1" * 64)
            interrupted = self._release(layout, "2" * 64)
            newer = self._release(layout, "3" * 64)
            installer._set_release_link(layout.current, interrupted.root, layout)
            installer._set_release_link(layout.previous, old.root, layout)
            layout.unit_file.write_text(installer.UNIT_MARKER, encoding="utf-8")
            installer._write_activation_intent(
                layout,
                interrupted,
                should_start=True,
                should_enable=True,
                prior_release=old.root,
            )

            def fake_runner(
                argv: list[object],
                **_kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                rendered = [os.fspath(value) for value in argv]
                return subprocess.CompletedProcess(rendered, 0, "inactive\n", "")

            with (
                patch.object(installer, "_user_service_state", return_value=(False, False)),
                patch.object(
                    installer,
                    "inspect_legacy_service",
                    return_value=installer.LegacyServiceState(),
                ),
                patch.object(installer, "_wait_for_ready"),
            ):
                installer.activate_release(
                    newer,
                    layout,
                    interactive=False,
                    runner=fake_runner,
                )

            self.assertEqual(
                installer._read_release_link(layout.current, layout),
                newer.root.resolve(),
            )
            self.assertEqual(
                installer._read_release_link(layout.previous, layout),
                old.root.resolve(),
            )
            self.assertFalse(interrupted.root.exists())

    def test_service_environment_scrubs_ambient_secrets_and_python_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._layout(Path(directory))
            with patch.dict(
                os.environ,
                {
                    "FEISHU_APP_SECRET": "must-not-leak",
                    "FEISHU_APP_SECRET_FILE": "/tmp/must-not-leak",
                    "NETIZEN_ADMIN_SECRET": "must-not-leak",
                    "NETIZEN_ADMIN_SECRET_FILE": "/tmp/must-not-leak",
                    "NETIZEN_CONFIG_PATH": "/tmp/wrong-config",
                    "PYTHONPATH": "/tmp/shadow",
                    "PYTHONHOME": "/tmp/wrong",
                    "VIRTUAL_ENV": "/tmp/venv",
                    "PATH": "/tmp/venv/bin:/usr/bin",
                    "XDG_DATA_HOME": "/tmp/data",
                    "XDG_CONFIG_HOME": "/tmp/config",
                    "XDG_STATE_HOME": "/tmp/state",
                    "XDG_CACHE_HOME": "/tmp/cache",
                },
            ):
                environment = installer._service_environment(layout)

            self.assertNotIn("FEISHU_APP_SECRET", environment)
            self.assertNotIn("NETIZEN_ADMIN_SECRET", environment)
            self.assertNotIn("PYTHONPATH", environment)
            self.assertNotIn("PYTHONHOME", environment)
            self.assertNotIn("VIRTUAL_ENV", environment)
            self.assertNotIn("XDG_DATA_HOME", environment)
            self.assertNotIn("XDG_CONFIG_HOME", environment)
            self.assertNotIn("XDG_STATE_HOME", environment)
            self.assertNotIn("XDG_CACHE_HOME", environment)
            self.assertEqual(environment["PATH"], "/usr/bin")
            self.assertEqual(
                environment["FEISHU_APP_SECRET_FILE"],
                str(layout.secret_file),
            )
            self.assertEqual(
                environment["NETIZEN_ADMIN_SECRET_FILE"],
                str(layout.admin_secret_file),
            )
            self.assertEqual(
                environment["NETIZEN_CONFIG_PATH"],
                str(layout.config_file),
            )

    def test_service_bootstrap_path_does_not_snapshot_the_installer_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._layout(Path(directory))
            development_venv = Path(directory) / "checkout/.venv"
            with patch.dict(
                os.environ,
                {
                    "VIRTUAL_ENV": str(development_venv),
                    "PATH": os.pathsep.join(
                        [str(development_venv / "bin"), "/opt/tools/bin", "relative"]
                    ),
                },
            ):
                path_value = installer._service_bootstrap_path(layout)

            entries = path_value.split(os.pathsep)
            self.assertNotIn(str(development_venv / "bin"), entries)
            self.assertNotIn("relative", entries)
            self.assertNotIn("/opt/tools/bin", entries)
            self.assertIn(str(layout.home / ".local/bin"), entries)
            self.assertIn("/usr/bin", entries)

    def test_runtime_validation_uses_the_candidate_bundled_codex_binary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            layout = self._layout(root)
            release = installer.Release(
                digest="6" * 64,
                root=root / "release",
                source=root / "release/source",
                venv=root / "release/venv",
            )
            calls: list[list[str]] = []
            environments: list[dict[str, str]] = []

            def fake_runner(argv: list[object], **kwargs: object) -> subprocess.CompletedProcess[str]:
                rendered = [os.fspath(value) for value in argv]
                calls.append(rendered)
                environments.append(dict(kwargs["env"]))
                output = (
                    json.dumps(
                        {
                            "dataDir": str(layout.state_dir),
                            "adminWeb": {
                                "enabled": True,
                                "host": "0.0.0.0",
                                "port": 8787,
                            },
                        }
                    )
                    + "\n"
                    if len(calls) == 1
                    else ""
                )
                return subprocess.CompletedProcess(rendered, 0, output, "")

            validation = installer.validate_runtime(release, layout, fake_runner)

            self.assertEqual(validation.data_dir, layout.state_dir)
            self.assertEqual(
                validation.admin_bind,
                installer.AdminBind(True, "0.0.0.0", 8787),
            )
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0][0], str(release.venv / "bin/python"))
            self.assertEqual(calls[0][1:4], ["-E", "-B", "-c"])
            compile(calls[0][4], "<configuration-validation>", "exec")
            self.assertIn("configured directories do not exist", calls[0][4])
            self.assertIn("inside an uninstall target", calls[0][4])
            self.assertEqual(calls[0][-2:], [str(layout.releases), str(layout.cache_dir)])
            self.assertEqual(calls[1][0], str(release.venv / "bin/python"))
            self.assertEqual(calls[1][1:4], ["-E", "-B", "-c"])
            self.assertIn("bundled_codex_path", calls[1][4])
            self.assertNotIn(str(release.venv / "bin/codex"), calls[1])
            for environment in environments:
                self.assertEqual(environment["HOME"], str(layout.home))
                self.assertEqual(environment["CODEX_HOME"], str(layout.codex_home))
                self.assertNotIn("PYTHONOPTIMIZE", environment)

    def test_uninstall_preserves_config_state_and_other_codex_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            layout = self._layout(root)
            installer.prepare_directories(layout)
            layout.unit_file.write_text(installer.UNIT_MARKER, encoding="utf-8")
            (layout.cache_dir / "artifact").write_text("cached", encoding="utf-8")
            layout.config_file.write_text("config", encoding="utf-8")
            layout.admin_secret_file.write_text("A" * 43, encoding="ascii")
            layout.admin_secret_file.chmod(0o600)
            (layout.state_dir / "channel.sqlite3").write_text("state", encoding="utf-8")
            release = self._release(layout, "e" * 64)
            installer._set_release_link(layout.current, release.root, layout)
            installer._set_release_link(layout.previous, release.root, layout)
            installer._write_activation_intent(
                layout,
                release,
                should_start=True,
                should_enable=True,
            )
            managed_skill = layout.codex_home / "skills/netizen-user-guide"
            managed_skill.mkdir(parents=True)
            (managed_skill / "SKILL.md").write_text("managed", encoding="utf-8")
            other_skill = layout.codex_home / "skills/other"
            other_skill.mkdir()
            (other_skill / "SKILL.md").write_text("other", encoding="utf-8")

            def fake_runner(argv: list[object], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                rendered = [os.fspath(value) for value in argv]
                return subprocess.CompletedProcess(rendered, 0, "", "")

            with patch.object(installer, "require_linux"):
                installer.uninstall(layout=layout, runner=fake_runner)

            self.assertTrue(layout.product_root.exists())
            self.assertFalse(layout.releases.exists())
            self.assertFalse(layout.cache_dir.exists())
            self.assertFalse(layout.current.exists())
            self.assertFalse(layout.previous.exists())
            self.assertFalse(layout.unit_file.exists())
            self.assertFalse(managed_skill.exists())
            self.assertTrue(layout.config_file.exists())
            self.assertEqual(layout.admin_secret_file.read_text(), "A" * 43)
            self.assertTrue((layout.state_dir / "channel.sqlite3").exists())
            self.assertFalse((layout.state_dir / installer.ACTIVATION_INTENT).exists())
            self.assertTrue((other_skill / "SKILL.md").exists())

    def test_xdg_drift_cannot_redirect_uninstall_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            layout = installer.resolve_layout(
                environ={
                    "XDG_DATA_HOME": str(root / "drift-data"),
                    "XDG_CONFIG_HOME": str(root / "drift-config"),
                    "XDG_STATE_HOME": str(root / "drift-state"),
                    "XDG_CACHE_HOME": str(root / "drift-cache"),
                },
                account_home=home,
                uid=os.geteuid(),
                username="current-user",
            )
            installer.prepare_directories(layout)
            layout.unit_file.write_text(installer.UNIT_MARKER, encoding="utf-8")
            unrelated = root / "drift-data/netizen/keep"
            unrelated.parent.mkdir(parents=True)
            unrelated.write_text("unrelated", encoding="utf-8")

            def fake_runner(argv: list[object], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                rendered = [os.fspath(value) for value in argv]
                return subprocess.CompletedProcess(rendered, 0, "", "")

            with patch.object(installer, "require_linux"):
                installer.uninstall(layout=layout, runner=fake_runner)

            self.assertTrue(unrelated.exists())
            self.assertTrue((home / ".netizen").exists())
            self.assertFalse(layout.releases.exists())
            self.assertFalse(layout.cache_dir.exists())

    def test_nonempty_unmarked_release_root_is_never_claimed_or_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._layout(Path(directory))
            unrelated = layout.releases / "keep"
            unrelated.parent.mkdir(parents=True)
            unrelated.write_text("unrelated", encoding="utf-8")

            with self.assertRaisesRegex(installer.InstallError, "without a Netizen marker"):
                installer.prepare_directories(layout)
            with self.assertRaisesRegex(installer.InstallError, "marker"):
                installer._remove_managed_netizen_directory(layout.releases, layout)

            self.assertEqual(unrelated.read_text(), "unrelated")

    def test_uninstall_refuses_orphaned_active_unit_before_removing_skill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._layout(Path(directory))
            managed_skill = layout.codex_home / "skills/netizen-user-guide"
            managed_skill.mkdir(parents=True)
            (managed_skill / "SKILL.md").write_text("managed", encoding="utf-8")

            with (
                patch.object(installer, "require_linux"),
                patch.object(installer, "_user_service_state", return_value=(True, False)),
            ):
                with self.assertRaisesRegex(installer.InstallError, "active/enabled"):
                    installer.uninstall(layout=layout)

            self.assertTrue((managed_skill / "SKILL.md").exists())

    def test_failed_first_start_is_stopped_before_state_is_rolled_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            layout = self._layout(root)
            installer.prepare_directories(layout)
            candidate = self._release(layout, "b" * 64)
            calls: list[list[str]] = []

            def fake_runner(argv: list[object], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                rendered = [os.fspath(value) for value in argv]
                calls.append(rendered)
                if rendered == ["systemctl", "--user", "start", "netizen.service"]:
                    raise installer.InstallError("start response lost")
                return subprocess.CompletedProcess(rendered, 0, "inactive\n", "")

            with (
                patch.object(installer, "_user_service_state", return_value=(False, False)),
                patch.object(installer, "inspect_legacy_service", return_value=installer.LegacyServiceState()),
            ):
                with self.assertRaisesRegex(installer.InstallError, "rolled back"):
                    installer.activate_release(
                        candidate,
                        layout,
                        interactive=False,
                        runner=fake_runner,
                        data_dir=layout.state_dir,
                    )

            self.assertIn(["systemctl", "--user", "stop", "netizen.service"], calls)
            self.assertIsNone(installer._read_release_link(layout.current, layout))
            self.assertFalse(layout.unit_file.exists())
            self.assertFalse((layout.codex_home / "skills/netizen-user-guide").exists())

    def test_managed_directory_removal_never_follows_a_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            layout = self._layout(root)
            external = root / "external"
            external.mkdir()
            marker = external / "keep"
            marker.write_text("keep", encoding="utf-8")
            layout.product_root.mkdir(exist_ok=True)
            link = layout.cache_dir
            link.symlink_to(external, target_is_directory=True)

            with self.assertRaisesRegex(installer.InstallError, "not a real directory"):
                installer._remove_managed_netizen_directory(link, layout)

            self.assertTrue(marker.exists())

    def test_uninstall_never_follows_a_symlinked_product_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            layout = self._layout(root)
            external = root / "external"
            external.mkdir()
            external_releases = external / "releases"
            external_cache = external / "cache"
            installer._ensure_managed_netizen_directory(external_releases)
            installer._ensure_managed_netizen_directory(external_cache)
            keep = external_releases / "keep"
            keep.write_text("external", encoding="utf-8")
            layout.product_root.symlink_to(external, target_is_directory=True)

            with patch.object(installer, "require_linux"):
                with self.assertRaisesRegex(installer.InstallError, "symlink"):
                    installer.uninstall(layout=layout)

            self.assertEqual(keep.read_text(), "external")
            self.assertTrue(external_cache.exists())

    def test_uninstall_rejects_invalid_release_pointer_before_mutating(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._layout(Path(directory))
            installer.prepare_directories(layout)
            layout.current.write_text("not-a-symlink", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_runner(
                argv: list[object], **_kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                rendered = [os.fspath(value) for value in argv]
                calls.append(rendered)
                return subprocess.CompletedProcess(rendered, 0, "", "")

            with patch.object(installer, "require_linux"):
                with self.assertRaisesRegex(installer.InstallError, "must be a symlink"):
                    installer.uninstall(layout=layout, runner=fake_runner)

            self.assertEqual(calls, [])
            self.assertTrue(layout.releases.exists())
            self.assertTrue(layout.cache_dir.exists())
            self.assertEqual(layout.current.read_text(), "not-a-symlink")

    def test_database_snapshot_restores_files_and_removes_candidate_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "state"
            data_dir.mkdir()
            database = data_dir / "channel.sqlite3"
            wal = data_dir / "channel.sqlite3-wal"
            database.write_text("old database", encoding="utf-8")
            wal.write_text("old wal", encoding="utf-8")
            temporary = root / "rollback"
            temporary.mkdir()

            snapshot = installer._capture_database(data_dir, temporary)
            database.write_text("candidate database", encoding="utf-8")
            wal.unlink()
            (data_dir / "channel.sqlite3-journal").write_text(
                "candidate journal",
                encoding="utf-8",
            )
            installer._restore_database(snapshot)

            self.assertEqual(database.read_text(), "old database")
            self.assertEqual(wal.read_text(), "old wal")
            self.assertFalse((data_dir / "channel.sqlite3-journal").exists())

    def _release(self, layout: installer.Layout, digest: str) -> installer.Release:
        root = layout.releases / digest
        source = root / "source"
        source.mkdir(parents=True)
        shutil.copy2(ROOT / "deploy/netizen.service", source / "deploy.service")
        deploy = source / "deploy"
        deploy.mkdir()
        shutil.move(source / "deploy.service", deploy / "netizen.service")
        skill = source / "skills/netizen-user-guide"
        skill.mkdir(parents=True)
        shutil.copy2(ROOT / "skills/netizen-user-guide/SKILL.md", skill / "SKILL.md")
        references = ROOT / "skills/netizen-user-guide/references"
        if references.exists():
            shutil.copytree(references, skill / "references")
        venv = root / "venv"
        venv.mkdir()
        return installer.Release(digest=digest, root=root, source=source, venv=venv)


if __name__ == "__main__":
    unittest.main()
