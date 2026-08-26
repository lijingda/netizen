from __future__ import annotations

import fcntl
import hashlib
import inspect
import io
import json
import os
import plistlib
import shutil
import socket
import sqlite3
import stat
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

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
            platform_name="linux",
        )

    def _published_source(
        self,
        destination: Path,
        *,
        commit: str = "a" * 40,
    ) -> installer.PublishedReleaseManifest:
        source_digest_value, _ = installer.snapshot_source(ROOT, destination)
        project = tomllib.loads(
            (destination / "pyproject.toml").read_text(encoding="utf-8")
        )
        version = project["project"]["version"]
        requirements_digest = hashlib.sha256(
            (destination / "requirements.lock").read_bytes()
        ).hexdigest()
        payload = {
            "schema": 1,
            "version": version,
            "commit": commit,
            "sourceDigest": source_digest_value,
            "requirementsDigest": requirements_digest,
            "qualification": installer.PUBLISHED_RELEASE_QUALIFICATION,
        }
        (destination / installer.PUBLISHED_RELEASE_MANIFEST).write_text(
            json.dumps(payload, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return installer.PublishedReleaseManifest(
            version=version,
            commit=commit,
            source_digest=source_digest_value,
            requirements_digest=requirements_digest,
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
                platform_name="linux",
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
                layout.service_file,
                home / ".config/systemd/user/netizen.service",
            )
            self.assertEqual(layout.codex_home, root / "codex")
            self.assertEqual(layout.username, "chosen-user")

    def test_internal_install_cli_requires_an_explicit_candidate_origin(self) -> None:
        source_args = installer.parse_args(["install-source"])
        release_args = installer.parse_args(["install-release", "/tmp/candidate"])

        self.assertEqual(source_args.command, "install-source")
        self.assertEqual(release_args.command, "install-release")
        self.assertEqual(release_args.source_root, Path("/tmp/candidate"))
        with (
            patch("sys.stderr", new=io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            installer.parse_args(["install"])

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
                    platform_name="linux",
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
                platform_name="linux",
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

    def test_deleted_secret_rebinds_through_unbound_browser_setup(self) -> None:
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
            layout.secret_file.write_text("old-secret", encoding="utf-8")
            layout.secret_file.unlink()
            requested_app_ids: list[str | None] = []

            def register_app(app_id: str | None) -> installer.FeishuAppCredentials:
                requested_app_ids.append(app_id)
                return installer.FeishuAppCredentials(
                    app_id="cli_replacement",
                    app_secret="replacement-secret",
                )

            installer.prepare_configuration(
                layout,
                interactive=True,
                input_stream=io.StringIO("\n"),
                secret_prompt=lambda _prompt: self.fail("manual secret prompt was used"),
                app_registrar=register_app,
            )

            self.assertEqual(requested_app_ids, [None])
            self.assertIn("cli_replacement", layout.config_file.read_text())
            self.assertNotIn("cli_existing", layout.config_file.read_text())
            self.assertEqual(layout.secret_file.read_text(), "replacement-secret")
            self.assertEqual(stat.S_IMODE(layout.secret_file.stat().st_mode), 0o600)

    def test_noninteractive_deleted_secret_preserves_rebind_request(self) -> None:
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
            layout.secret_file.unlink()

            with self.assertRaisesRegex(
                installer.ConfigurationRequired,
                r"interactive terminal.*update instance\.appId",
            ):
                installer.prepare_configuration(layout, interactive=False)

            self.assertFalse(layout.secret_file.exists())
            self.assertIn("cli_existing", layout.config_file.read_text())

    def test_deleted_secret_manual_rebind_replaces_app_id(self) -> None:
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
            layout.secret_file.unlink()

            installer.prepare_configuration(
                layout,
                interactive=True,
                input_stream=io.StringIO("2\ncli_manual_replacement\n"),
                secret_prompt=lambda _prompt: "manual-replacement-secret",
                app_registrar=lambda _app_id: self.fail("browser setup was used"),
            )

            self.assertIn("cli_manual_replacement", layout.config_file.read_text())
            self.assertNotIn("cli_existing", layout.config_file.read_text())
            self.assertEqual(
                layout.secret_file.read_text(),
                "manual-replacement-secret",
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

    def test_rebind_credentials_roll_back_config_and_missing_secret(self) -> None:
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
            layout.secret_file.unlink()
            original_config = layout.config_file.read_bytes()
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
                    replace_existing_app_id="cli_existing",
                    credentials=installer.FeishuAppCredentials(
                        app_id="cli_replacement",
                        app_secret="new-secret",
                    ),
                )

            self.assertEqual(layout.config_file.read_bytes(), original_config)
            self.assertFalse(layout.secret_file.exists())

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

    def test_release_permission_query_uses_secret_file_and_contract_order(self) -> None:
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
            layout.secret_file.write_text("private-secret", encoding="utf-8")
            release = installer.Release(
                digest="c" * 64,
                root=ROOT,
                source=ROOT,
                venv=ROOT / ".venv",
            )
            calls: list[tuple[list[str], dict[str, object]]] = []

            def fake_runner(
                argv: list[object], **kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                rendered = [os.fspath(value) for value in argv]
                calls.append((rendered, kwargs))
                return subprocess.CompletedProcess(
                    rendered,
                    0,
                    json.dumps(
                        {
                            "version": 1,
                            "missingScopes": [
                                "im:message.p2p_msg:readonly",
                                "im:chat:readonly",
                            ],
                        }
                    ),
                    "",
                )

            missing = installer._query_missing_feishu_permissions_from_release(
                release,
                layout,
                runner=fake_runner,
            )

            self.assertEqual(
                missing,
                ("im:message.p2p_msg:readonly", "im:chat:readonly"),
            )
            command, kwargs = calls[0]
            self.assertEqual(
                command[-4:],
                [
                    "--app-id",
                    "cli_existing",
                    "--secret-file",
                    str(layout.secret_file),
                ],
            )
            self.assertNotIn("private-secret", command)
            self.assertIs(kwargs["capture_output"], True)
            self.assertEqual(kwargs["timeout"], 90.0)

    def test_noninteractive_permission_failure_prevents_activation(self) -> None:
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
            layout.secret_file.write_text("existing-secret", encoding="utf-8")
            release = installer.Release(
                digest="d" * 64,
                root=ROOT,
                source=ROOT,
                venv=ROOT / ".venv",
            )

            with (
                patch.object(installer, "require_supported_platform"),
                patch.object(installer.SystemdServiceBackend, "preflight"),
                patch.object(
                    installer.SystemdServiceBackend, "prepare_host"
                ) as prepare_host,
                patch.object(installer, "prepare_source_release", return_value=release),
                patch.object(
                    installer,
                    "validate_runtime",
                    return_value=installer.RuntimeValidation(
                        data_dir=layout.state_dir,
                        admin_bind=installer.AdminBind(False, "127.0.0.1", 8787),
                    ),
                ),
                patch.object(
                    installer,
                    "_query_missing_feishu_permissions_from_release",
                    return_value=("im:chat:readonly",),
                ),
                patch.object(
                    installer, "_register_feishu_app_from_release"
                ) as register,
                patch.object(installer, "activate_release") as activate,
                self.assertRaisesRegex(
                    installer.InstallError,
                    "im:chat:readonly.*rerun ./dev-install.sh",
                ),
            ):
                installer.install_source(
                    source_root=ROOT,
                    layout=layout,
                    interactive=False,
                )

            register.assert_not_called()
            prepare_host.assert_not_called()
            activate.assert_not_called()

    def test_interactive_rebind_permission_failure_prevents_activation(self) -> None:
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
            layout.secret_file.write_text("old-secret", encoding="utf-8")
            layout.secret_file.unlink()
            release = installer.Release(
                digest="9" * 64,
                root=ROOT,
                source=ROOT,
                venv=ROOT / ".venv",
            )

            with (
                patch.object(installer, "require_supported_platform"),
                patch.object(installer.SystemdServiceBackend, "preflight"),
                patch.object(
                    installer.SystemdServiceBackend, "prepare_host"
                ) as prepare_host,
                patch.object(installer, "prepare_source_release", return_value=release),
                patch.object(
                    installer,
                    "validate_runtime",
                    return_value=installer.RuntimeValidation(
                        data_dir=layout.state_dir,
                        admin_bind=installer.AdminBind(False, "127.0.0.1", 8787),
                    ),
                ),
                patch.object(
                    installer,
                    "_register_feishu_app_from_release",
                    return_value=installer.FeishuAppCredentials(
                        app_id="cli_replacement",
                        app_secret="replacement-secret",
                    ),
                ) as register,
                patch.object(
                    installer,
                    "_query_missing_feishu_permissions_from_release",
                    return_value=("im:chat:readonly",),
                ),
                patch.object(installer, "activate_release") as activate,
                patch("sys.stdin", new=io.StringIO("\n")),
                self.assertRaisesRegex(
                    installer.InstallError,
                    "im:chat:readonly.*rerun ./dev-install.sh",
                ),
            ):
                installer.install_source(
                    source_root=ROOT,
                    layout=layout,
                    interactive=True,
                )

            register.assert_called_once_with(release, None, runner=ANY)
            self.assertIn("cli_replacement", layout.config_file.read_text())
            self.assertEqual(layout.secret_file.read_text(), "replacement-secret")
            prepare_host.assert_not_called()
            activate.assert_not_called()

    def test_interactive_existing_app_repairs_once_before_activation(self) -> None:
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
            layout.secret_file.write_text("old-secret", encoding="utf-8")
            release = installer.Release(
                digest="e" * 64,
                root=ROOT,
                source=ROOT,
                venv=ROOT / ".venv",
            )

            with (
                patch.object(installer, "require_supported_platform"),
                patch.object(installer.SystemdServiceBackend, "preflight"),
                patch.object(
                    installer.SystemdServiceBackend, "prepare_host"
                ) as prepare_host,
                patch.object(installer, "prepare_source_release", return_value=release),
                patch.object(
                    installer,
                    "validate_runtime",
                    return_value=installer.RuntimeValidation(
                        data_dir=layout.state_dir,
                        admin_bind=installer.AdminBind(False, "127.0.0.1", 8787),
                    ),
                ),
                patch.object(
                    installer,
                    "_query_missing_feishu_permissions_from_release",
                    side_effect=[("im:chat:readonly",), ()],
                ) as query,
                patch.object(
                    installer,
                    "_register_feishu_app_from_release",
                    return_value=installer.FeishuAppCredentials(
                        app_id="cli_existing",
                        app_secret="updated-secret",
                    ),
                ) as register,
                patch.object(installer, "activate_release") as activate,
            ):
                installed = installer.install_source(
                    source_root=ROOT,
                    layout=layout,
                    interactive=True,
                )

            self.assertEqual(installed, release)
            register.assert_called_once_with(
                release,
                "cli_existing",
                runner=ANY,
            )
            self.assertEqual(query.call_count, 2)
            self.assertEqual(layout.secret_file.read_text(), "updated-secret")
            prepare_host.assert_called_once_with(interactive=True)
            activate.assert_called_once()

    def test_newly_configured_app_is_not_sent_through_a_second_browser_flow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._layout(Path(directory))
            installer.prepare_directories(layout)
            with self.assertRaises(installer.ConfigurationRequired):
                installer.prepare_configuration(layout, interactive=False)
            layout.config_file.write_text(
                layout.config_file.read_text().replace(
                    "cli_REPLACE_ME",
                    "cli_new",
                ),
                encoding="utf-8",
            )
            layout.secret_file.write_text("new-secret", encoding="utf-8")
            release = installer.Release(
                digest="f" * 64,
                root=ROOT,
                source=ROOT,
                venv=ROOT / ".venv",
            )

            with (
                patch.object(
                    installer,
                    "_query_missing_feishu_permissions_from_release",
                    return_value=("im:chat:readonly",),
                ),
                patch.object(
                    installer, "_register_feishu_app_from_release"
                ) as register,
                self.assertRaisesRegex(installer.InstallError, "im:chat:readonly"),
            ):
                installer.require_feishu_permissions(
                    release,
                    layout,
                    interactive=True,
                    repair_existing_app=False,
                )

            register.assert_not_called()

    def test_noninteractive_install_does_not_build_before_credentials_are_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._layout(Path(directory))
            with (
                patch.object(installer, "require_supported_platform"),
                patch.object(installer.SystemdServiceBackend, "preflight"),
                patch.object(installer, "prepare_source_release") as prepare_release,
                patch.object(installer, "info") as installer_info,
                self.assertRaises(installer.ConfigurationRequired),
            ):
                installer.install_source(
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
                "the selected installer with </dev/null."
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
                patch.object(installer, "require_supported_platform"),
                patch.object(installer.SystemdServiceBackend, "preflight"),
                patch.object(installer.SystemdServiceBackend, "prepare_host"),
                patch.object(
                    installer,
                    "prepare_source_release",
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
                patch.object(
                    installer,
                    "_query_missing_feishu_permissions_from_release",
                    return_value=(),
                ),
                patch.object(installer, "activate_release"),
                patch.object(installer, "info", side_effect=messages.append),
                patch("sys.stdin", new=io.StringIO("\n")),
            ):
                installed = installer.install_source(
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
            self.assertTrue((first / "dev-install.sh").is_file())
            self.assertTrue((first / ".gitignore").is_file())
            self.assertTrue((first / "LOCAL_ENVIRONMENT.example.md").is_file())
            self.assertFalse((first / "LOCAL_ENVIRONMENT.md").exists())
            self.assertTrue((first / "scripts/netizen_installer.py").is_file())
            self.assertTrue((first / "scripts/netizen_service_launcher.py").is_file())
            self.assertFalse(any(path.name == "__pycache__" for path in first.rglob("*")))

    def test_published_manifest_binds_version_source_and_dependency_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "published"
            expected = self._published_source(source)

            self.assertEqual(
                installer.read_published_release_manifest(source),
                expected,
            )

            readme = source / "README.md"
            readme.write_text(readme.read_text(encoding="utf-8") + "tampered\n")
            with self.assertRaisesRegex(installer.InstallError, "source digest"):
                installer.read_published_release_manifest(source)

    def test_published_manifest_rejects_unknown_fields_and_invalid_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "published"
            self._published_source(source)
            path = source / installer.PUBLISHED_RELEASE_MANIFEST
            payload = json.loads(path.read_text(encoding="utf-8"))

            path.write_text(
                json.dumps({**payload, "unexpected": True}) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(installer.InstallError, "unsupported shape"):
                installer.read_published_release_manifest(source)

            payload["commit"] = "not-a-full-commit"
            path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(installer.InstallError, "invalid values"):
                installer.read_published_release_manifest(source)

    def test_published_install_rejects_unqualified_source_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            layout = self._layout(root)
            source = root / "unqualified"
            installer.snapshot_source(ROOT, source)

            with self.assertRaisesRegex(installer.InstallError, "manifest"):
                installer.install_published(
                    source_root=source,
                    layout=layout,
                    interactive=False,
                )

            self.assertFalse(layout.product_root.exists())

    def test_source_and_published_candidates_use_isolated_qualification_gates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            layout = self._layout(root)
            installer.prepare_directories(layout)
            published_source = root / "published"
            published_manifest = self._published_source(published_source)
            calls: list[list[str]] = []

            def fake_runner(
                argv: list[object],
                **_kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                rendered = [os.fspath(value) for value in argv]
                calls.append(rendered)
                if len(rendered) >= 4 and rendered[1:3] == ["-m", "venv"]:
                    candidate_bin = Path(rendered[3]) / "bin"
                    candidate_bin.mkdir(parents=True)
                    (candidate_bin / "python").write_text("candidate", encoding="utf-8")
                return subprocess.CompletedProcess(rendered, 0, "", "")

            source_release = installer.prepare_source_release(
                layout,
                source_root=ROOT,
                runner=fake_runner,
            )
            source_commands = list(calls)
            calls.clear()
            published_release = installer.prepare_published_release(
                layout,
                source_root=published_source,
                manifest=published_manifest,
                runner=fake_runner,
            )

            self.assertNotEqual(source_release.digest, published_release.digest)
            self.assertTrue(
                any("unittest" in command for command in source_commands)
            )
            self.assertFalse(any("unittest" in command for command in calls))
            self.assertTrue(any(command[-2:] == ["pip", "check"] for command in calls))
            self.assertEqual(
                installer.read_published_release_manifest(published_release.source),
                published_manifest,
            )

            calls.clear()
            reused_published_release = installer.prepare_published_release(
                layout,
                source_root=published_source,
                manifest=published_manifest,
                runner=fake_runner,
            )
            self.assertEqual(reused_published_release, published_release)
            self.assertFalse(any(command[1:3] == ["-m", "venv"] for command in calls))
            self.assertFalse(any("unittest" in command for command in calls))
            self.assertTrue(any(command[-2:] == ["pip", "check"] for command in calls))
            self.assertEqual(
                installer.read_published_release_manifest(published_release.source),
                published_manifest,
            )

    def test_published_install_uses_the_shared_activation_orchestration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            layout = self._layout(root)
            installer.prepare_directories(layout)
            with self.assertRaises(installer.ConfigurationRequired):
                installer.prepare_configuration(layout, interactive=False)
            layout.config_file.write_text(
                layout.config_file.read_text(encoding="utf-8").replace(
                    "cli_REPLACE_ME",
                    "cli_existing",
                ),
                encoding="utf-8",
            )
            layout.secret_file.write_text("existing-secret", encoding="utf-8")
            published_source = root / "published"
            self._published_source(published_source)
            release = installer.Release(
                digest="8" * 64,
                root=ROOT,
                source=ROOT,
                venv=ROOT / ".venv",
            )
            validation = installer.RuntimeValidation(
                data_dir=layout.state_dir,
                admin_bind=installer.AdminBind(False, "127.0.0.1", 8787),
            )

            with (
                patch.object(installer, "require_supported_platform"),
                patch.object(installer.SystemdServiceBackend, "preflight"),
                patch.object(
                    installer.SystemdServiceBackend,
                    "prepare_host",
                ) as prepare_host,
                patch.object(
                    installer,
                    "prepare_published_release",
                    return_value=release,
                ) as prepare_candidate,
                patch.object(
                    installer,
                    "validate_runtime",
                    return_value=validation,
                ),
                patch.object(installer, "require_feishu_permissions") as permissions,
                patch.object(installer, "activate_release") as activate,
            ):
                installed = installer.install_published(
                    source_root=published_source,
                    layout=layout,
                    interactive=False,
                )

            self.assertEqual(installed, release)
            prepare_candidate.assert_called_once()
            permissions.assert_called_once()
            prepare_host.assert_called_once_with(interactive=False)
            activate.assert_called_once()

    def test_source_and_published_installs_switch_both_directions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            layout = self._layout(root)
            installer.prepare_directories(layout)
            with self.assertRaises(installer.ConfigurationRequired):
                installer.prepare_configuration(layout, interactive=False)
            layout.config_file.write_text(
                layout.config_file.read_text(encoding="utf-8").replace(
                    "cli_REPLACE_ME",
                    "cli_existing",
                ),
                encoding="utf-8",
            )
            layout.secret_file.write_text("existing-secret", encoding="utf-8")
            published_source = root / "published"
            published_manifest = self._published_source(published_source)
            backend = _stopped_backend()
            validation = installer.RuntimeValidation(
                data_dir=layout.state_dir,
                admin_bind=installer.AdminBind(False, "127.0.0.1", 8787),
            )

            def fake_runner(
                argv: list[object],
                **_kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                rendered = [os.fspath(value) for value in argv]
                if len(rendered) >= 4 and rendered[1:3] == ["-m", "venv"]:
                    candidate_bin = Path(rendered[3]) / "bin"
                    candidate_bin.mkdir(parents=True)
                    (candidate_bin / "python").write_text(
                        "candidate",
                        encoding="utf-8",
                    )
                return subprocess.CompletedProcess(rendered, 0, "", "")

            with (
                patch.object(installer, "require_supported_platform"),
                patch.object(installer, "_service_backend", return_value=backend),
                patch.object(
                    installer,
                    "validate_runtime",
                    return_value=validation,
                ),
                patch.object(installer, "require_feishu_permissions"),
            ):
                source_release = installer.install_source(
                    source_root=ROOT,
                    layout=layout,
                    runner=fake_runner,
                    interactive=False,
                )
                self.assertEqual(
                    installer._read_release_link(layout.current, layout),
                    source_release.root.resolve(),
                )
                self.assertIsNone(
                    installer._read_release_link(layout.previous, layout)
                )

                published_release = installer.install_published(
                    source_root=published_source,
                    layout=layout,
                    runner=fake_runner,
                    interactive=False,
                )
                self.assertEqual(
                    installer._read_release_link(layout.current, layout),
                    published_release.root.resolve(),
                )
                self.assertEqual(
                    installer._read_release_link(layout.previous, layout),
                    source_release.root.resolve(),
                )

                source_again = installer.install_source(
                    source_root=ROOT,
                    layout=layout,
                    runner=fake_runner,
                    interactive=False,
                )

            self.assertEqual(source_again, source_release)
            self.assertNotEqual(source_release.digest, published_release.digest)
            self.assertEqual(
                installer._read_release_link(layout.current, layout),
                source_release.root.resolve(),
            )
            self.assertEqual(
                installer._read_release_link(layout.previous, layout),
                published_release.root.resolve(),
            )
            source_metadata = json.loads(
                (source_release.root / installer.RELEASE_METADATA).read_text(
                    encoding="utf-8"
                )
            )
            published_metadata = json.loads(
                (published_release.root / installer.RELEASE_METADATA).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(source_metadata["qualification"], "source")
            self.assertNotIn("publishedRelease", source_metadata)
            self.assertEqual(published_metadata["qualification"], "published")
            self.assertEqual(
                published_metadata["publishedRelease"]["commit"],
                published_manifest.commit,
            )

    def test_app_rebind_composes_with_both_origins_and_failure_retries(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            layout = self._layout(root)
            installer.prepare_directories(layout)
            with self.assertRaises(installer.ConfigurationRequired):
                installer.prepare_configuration(layout, interactive=False)
            layout.config_file.write_text(
                layout.config_file.read_text(encoding="utf-8").replace(
                    "cli_REPLACE_ME",
                    "cli_original",
                ),
                encoding="utf-8",
            )
            layout.secret_file.write_text("original-secret", encoding="utf-8")
            published_source = root / "published"
            self._published_source(published_source)
            backend = _stopped_backend()
            validation = installer.RuntimeValidation(
                data_dir=layout.state_dir,
                admin_bind=installer.AdminBind(False, "127.0.0.1", 8787),
            )

            def fake_runner(
                argv: list[object],
                **_kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                rendered = [os.fspath(value) for value in argv]
                if len(rendered) >= 4 and rendered[1:3] == ["-m", "venv"]:
                    candidate_bin = Path(rendered[3]) / "bin"
                    candidate_bin.mkdir(parents=True)
                    (candidate_bin / "python").write_text(
                        "candidate",
                        encoding="utf-8",
                    )
                return subprocess.CompletedProcess(rendered, 0, "", "")

            with (
                patch.object(installer, "require_supported_platform"),
                patch.object(installer, "_service_backend", return_value=backend),
                patch.object(
                    installer,
                    "validate_runtime",
                    return_value=validation,
                ),
                patch.object(installer, "require_feishu_permissions"),
            ):
                source_release = installer.install_source(
                    source_root=ROOT,
                    layout=layout,
                    runner=fake_runner,
                    interactive=False,
                )

            layout.secret_file.unlink()
            backend.reset_mock()
            register = MagicMock(
                return_value=installer.FeishuAppCredentials(
                    app_id="cli_published",
                    app_secret="published-secret",
                )
            )
            query_permissions = MagicMock(return_value=("im:chat:readonly",))
            with (
                patch.object(installer, "require_supported_platform"),
                patch.object(installer, "_service_backend", return_value=backend),
                patch.object(
                    installer,
                    "validate_runtime",
                    return_value=validation,
                ),
                patch.object(
                    installer,
                    "_register_feishu_app_from_release",
                    new=register,
                ),
                patch.object(
                    installer,
                    "_query_missing_feishu_permissions_from_release",
                    new=query_permissions,
                ),
            ):
                with (
                    patch("sys.stdin", new=io.StringIO("\n")),
                    self.assertRaisesRegex(
                        installer.InstallError,
                        "im:chat:readonly",
                    ),
                ):
                    installer.install_published(
                        source_root=published_source,
                        layout=layout,
                        runner=fake_runner,
                        interactive=True,
                    )

                self.assertEqual(
                    installer._read_release_link(layout.current, layout),
                    source_release.root.resolve(),
                )
                self.assertIsNone(
                    installer._read_release_link(layout.previous, layout)
                )
                self.assertIn("cli_published", layout.config_file.read_text())
                self.assertEqual(
                    layout.secret_file.read_text(encoding="utf-8"),
                    "published-secret",
                )
                self.assertEqual(register.call_count, 1)
                backend.prepare_host.assert_not_called()
                backend.publish_definition.assert_not_called()

                query_permissions.return_value = ()
                backend.capture_definition.return_value = installer.FileSnapshot(
                    True,
                    b"old service definition",
                )
                backend.inspect_state.return_value = installer.ServiceState(True, True)
                start_attempts = 0

                def fail_candidate_start_once(
                    *_args: object,
                    **_kwargs: object,
                ) -> None:
                    nonlocal start_attempts
                    start_attempts += 1
                    if start_attempts == 1:
                        raise installer.InstallError("candidate failed to become ready")

                backend.start_and_wait.side_effect = fail_candidate_start_once
                with self.assertRaisesRegex(installer.InstallError, "rolled back"):
                    installer.install_published(
                        source_root=published_source,
                        layout=layout,
                        runner=fake_runner,
                        interactive=False,
                    )
                self.assertEqual(start_attempts, 2)
                self.assertEqual(register.call_count, 1)
                self.assertEqual(
                    installer._read_release_link(layout.current, layout),
                    source_release.root.resolve(),
                )
                self.assertIsNone(
                    installer._read_release_link(layout.previous, layout)
                )
                self.assertIn("cli_published", layout.config_file.read_text())
                self.assertEqual(
                    layout.secret_file.read_text(encoding="utf-8"),
                    "published-secret",
                )

                published_release = installer.install_published(
                    source_root=published_source,
                    layout=layout,
                    runner=fake_runner,
                    interactive=False,
                )
                self.assertEqual(register.call_count, 1)
                self.assertEqual(
                    installer._read_release_link(layout.current, layout),
                    published_release.root.resolve(),
                )
                self.assertEqual(
                    installer._read_release_link(layout.previous, layout),
                    source_release.root.resolve(),
                )

                layout.secret_file.unlink()
                register.return_value = installer.FeishuAppCredentials(
                    app_id="cli_source",
                    app_secret="source-secret",
                )
                backend.reset_mock()
                with patch("sys.stdin", new=io.StringIO("\n")):
                    source_again = installer.install_source(
                        source_root=ROOT,
                        layout=layout,
                        runner=fake_runner,
                        interactive=True,
                    )

            self.assertEqual(source_again, source_release)
            self.assertEqual(register.call_count, 2)
            self.assertEqual(
                [call.args[1] for call in register.call_args_list],
                [None, None],
            )
            self.assertEqual(
                installer._read_release_link(layout.current, layout),
                source_release.root.resolve(),
            )
            self.assertEqual(
                installer._read_release_link(layout.previous, layout),
                published_release.root.resolve(),
            )
            config_text = layout.config_file.read_text(encoding="utf-8")
            self.assertIn("cli_source", config_text)
            self.assertNotIn("cli_published", config_text)
            self.assertEqual(
                layout.secret_file.read_text(encoding="utf-8"),
                "source-secret",
            )

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

            first = installer.prepare_source_release(
                layout,
                source_root=ROOT,
                runner=fake_runner,
            )

            self.assertEqual(first.root.name, first.digest)
            self.assertTrue((first.root / installer.RELEASE_METADATA).is_file())
            self.assertTrue((first.source / "netizen/main.py").is_file())
            self.assertTrue(any(call[1:3] == ["-m", "venv"] for call in calls))

            calls.clear()
            second = installer.prepare_source_release(
                layout,
                source_root=ROOT,
                runner=fake_runner,
            )

            self.assertEqual(second, first)
            self.assertFalse(any(call[1:3] == ["-m", "venv"] for call in calls))
            self.assertTrue(any("unittest" in call for call in calls))
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

            unit = installer.render_systemd_service(release, layout)

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
            ("dev-install.sh", ["unexpected"]),
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
        for script in ("install.sh", "dev-install.sh", "service.sh", "uninstall.sh"):
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
            installer.prepare_directories(layout)
            layout.service_file.write_text(
                installer.SYSTEMD_SERVICE_MARKER
                + "\nEnvironment=NETIZEN_READY_FILE=/managed/service.ready\n",
                encoding="utf-8",
            )
            calls: list[list[str]] = []

            def fake_runner(argv: list[object], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                rendered = [os.fspath(value) for value in argv]
                calls.append(rendered)
                return subprocess.CompletedProcess(rendered, 0, "", "")

            with (
                patch.object(installer, "require_supported_platform"),
                patch.object(installer.SystemdServiceBackend, "preflight"),
                patch.object(installer, "_wait_for_systemd_ready") as wait_for_ready,
            ):
                code = installer.service_action(
                    "restart",
                    layout=layout,
                    runner=fake_runner,
                )

            self.assertEqual(code, 0)
            self.assertEqual(
                calls,
                [
                    ["systemctl", "--user", "stop", "netizen.service"],
                    ["systemctl", "--user", "start", "netizen.service"],
                ],
            )
            self.assertTrue(all("sudo" not in call for call in calls))
            wait_for_ready.assert_called_once()
            self.assertEqual(
                wait_for_ready.call_args.kwargs["timeout"],
                installer.SERVICE_READY_TIMEOUT_SECONDS,
            )

    def test_service_restart_surfaces_a_post_systemctl_ready_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._layout(Path(directory))
            installer.prepare_directories(layout)
            layout.service_file.write_text(
                installer.SYSTEMD_SERVICE_MARKER
                + "\nEnvironment=NETIZEN_READY_FILE=/managed/service.ready\n",
                encoding="utf-8",
            )
            calls: list[list[str]] = []

            def fake_runner(
                argv: list[object],
                **_kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                rendered = [os.fspath(value) for value in argv]
                calls.append(rendered)
                return subprocess.CompletedProcess(rendered, 0, "", "")

            with (
                patch.object(installer, "require_supported_platform"),
                patch.object(installer.SystemdServiceBackend, "preflight"),
                patch.object(
                    installer,
                    "_wait_for_systemd_ready",
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
                [
                    ["systemctl", "--user", "stop", "netizen.service"],
                    ["systemctl", "--user", "start", "netizen.service"],
                ],
            )

    def test_service_start_is_idempotent_when_unit_is_already_active(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._layout(Path(directory))
            installer.prepare_directories(layout)
            layout.service_file.write_text(
                installer.SYSTEMD_SERVICE_MARKER,
                encoding="utf-8",
            )
            layout.ready_file.write_bytes(installer.READY_MARKER_CONTENT)
            layout.ready_file.chmod(0o600)
            calls: list[list[str]] = []

            def fake_runner(
                argv: list[object],
                **_kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                rendered = [os.fspath(value) for value in argv]
                calls.append(rendered)
                return subprocess.CompletedProcess(rendered, 0, "active\n", "")

            with (
                patch.object(installer, "require_supported_platform"),
                patch.object(installer.SystemdServiceBackend, "preflight"),
                patch.object(installer, "_wait_for_systemd_ready") as wait_for_ready,
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
                f"SYSTEMD_UNIT_PATH={layout.service_dir}\n",
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
            layout.service_file.parent.mkdir(parents=True)
            layout.service_file.write_text(
                "[Service]\nExecStart=/bin/other\n",
                encoding="utf-8",
            )

            with patch.object(installer, "require_supported_platform"):
                with self.assertRaisesRegex(
                    installer.InstallError,
                    "unrecognized systemd user service",
                ):
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
            old_unit_text = installer.SYSTEMD_SERVICE_MARKER + "\n# old unit\n"
            layout.service_file.write_text(old_unit_text, encoding="utf-8")
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
                if rendered[0] == "journalctl":
                    return subprocess.CompletedProcess(
                        rendered,
                        0,
                        installer.LEGACY_SYSTEMD_READY_LOG + "\n",
                        "",
                    )
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
                    "_wait_for_systemd_ready",
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
            self.assertEqual(layout.service_file.read_text(), old_unit_text)
            self.assertEqual((old_skill / "SKILL.md").read_text(), "old skill\n")
            self.assertEqual(database.read_text(), "old database")
            self.assertFalse((layout.state_dir / installer.ACTIVATION_INTENT).exists())
            self.assertGreaterEqual(
                sum(call[:3] == ["systemctl", "--user", "stop"] for call in calls),
                2,
            )
            self.assertIn(
                ["systemctl", "--user", "start", "netizen.service"],
                calls,
            )
            self.assertTrue(any(call[0] == "journalctl" for call in calls))

    def test_stopped_upgrade_migrates_v5_database_without_losing_side_routes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            layout = self._layout(root)
            installer.prepare_directories(layout)
            old = self._release(layout, "1" * 64)
            candidate = self._release(layout, "2" * 64)
            installer._set_release_link(layout.current, old.root, layout)
            database = layout.state_dir / "channel.sqlite3"
            _write_v5_channel_database(database)
            backend = _stopped_backend()

            with patch.object(
                installer,
                "_service_backend",
                return_value=backend,
            ):
                installer.activate_release(
                    candidate,
                    layout,
                    interactive=False,
                    data_dir=layout.state_dir,
                )

            connection = sqlite3.connect(database)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT version FROM schema_version"
                    ).fetchone()[0],
                    6,
                )
                self.assertEqual(
                    connection.execute(
                        """
                        SELECT message_context_mode,
                               context_anchor_message_id,
                               context_anchor_create_time_ms,
                               context_revision
                        FROM bindings
                        """
                    ).fetchall(),
                    [("current-only", None, None, 1)],
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT side_id, state FROM side_topics"
                    ).fetchall(),
                    [("side-legacy", "closed")],
                )
            finally:
                connection.close()
            self.assertEqual(
                installer._read_release_link(layout.current, layout),
                candidate.root.resolve(),
            )
            backend.start_and_wait.assert_not_called()

    def test_failed_publish_rolls_back_v5_database_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            layout = self._layout(root)
            installer.prepare_directories(layout)
            old = self._release(layout, "3" * 64)
            candidate = self._release(layout, "4" * 64)
            installer._set_release_link(layout.current, old.root, layout)
            database = layout.state_dir / "channel.sqlite3"
            _write_v5_channel_database(database)
            before = database.read_bytes()
            backend = _stopped_backend()
            backend.publish_definition.side_effect = installer.InstallError(
                "publish failed"
            )

            with (
                patch.object(
                    installer,
                    "_service_backend",
                    return_value=backend,
                ),
                self.assertRaisesRegex(installer.InstallError, "rolled back"),
            ):
                installer.activate_release(
                    candidate,
                    layout,
                    interactive=False,
                    data_dir=layout.state_dir,
                )

            self.assertEqual(database.read_bytes(), before)
            connection = sqlite3.connect(database)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT version FROM schema_version"
                    ).fetchone()[0],
                    5,
                )
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(bindings)"
                    ).fetchall()
                }
                self.assertNotIn("message_context_mode", columns)
            finally:
                connection.close()
            self.assertEqual(
                installer._read_release_link(layout.current, layout),
                old.root.resolve(),
            )

    def test_failed_skill_rollback_preserves_a_recovery_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            layout = self._layout(root)
            installer.prepare_directories(layout)
            old = self._release(layout, "7" * 64)
            candidate = self._release(layout, "8" * 64)
            installer._set_release_link(layout.current, old.root, layout)
            layout.service_file.write_text(
                installer.SYSTEMD_SERVICE_MARKER,
                encoding="utf-8",
            )
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
                    "_wait_for_systemd_ready",
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
            layout.service_file.write_text(
                installer.SYSTEMD_SERVICE_MARKER,
                encoding="utf-8",
            )
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
                patch.object(installer, "_wait_for_systemd_ready"),
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
            layout.service_file.write_text(
                installer.SYSTEMD_SERVICE_MARKER,
                encoding="utf-8",
            )
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
                patch.object(installer, "_wait_for_systemd_ready"),
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
            layout.service_file.write_text(
                installer.SYSTEMD_SERVICE_MARKER,
                encoding="utf-8",
            )
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
                patch.object(installer, "_wait_for_systemd_ready"),
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
            layout.service_file.write_text(
                installer.SYSTEMD_SERVICE_MARKER,
                encoding="utf-8",
            )
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

            with patch.object(installer, "require_supported_platform"):
                installer.uninstall(layout=layout, runner=fake_runner)

            self.assertTrue(layout.product_root.exists())
            self.assertFalse(layout.releases.exists())
            self.assertFalse(layout.cache_dir.exists())
            self.assertFalse(layout.current.exists())
            self.assertFalse(layout.previous.exists())
            self.assertFalse(layout.service_file.exists())
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
                platform_name="linux",
            )
            installer.prepare_directories(layout)
            layout.service_file.write_text(
                installer.SYSTEMD_SERVICE_MARKER,
                encoding="utf-8",
            )
            unrelated = root / "drift-data/netizen/keep"
            unrelated.parent.mkdir(parents=True)
            unrelated.write_text("unrelated", encoding="utf-8")

            def fake_runner(argv: list[object], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                rendered = [os.fspath(value) for value in argv]
                return subprocess.CompletedProcess(rendered, 0, "", "")

            with patch.object(installer, "require_supported_platform"):
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
                patch.object(installer, "require_supported_platform"),
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
            self.assertFalse(layout.service_file.exists())
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

            with patch.object(installer, "require_supported_platform"):
                with self.assertRaisesRegex(
                    installer.InstallError,
                    "not a real directory",
                ):
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

            with patch.object(installer, "require_supported_platform"):
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

    def test_platform_selection_accepts_linux_and_darwin_only(self) -> None:
        self.assertEqual(installer._supported_platform_name("linux"), "linux")
        self.assertEqual(installer._supported_platform_name("linux2"), "linux")
        self.assertEqual(installer._supported_platform_name("darwin"), "darwin")
        with self.assertRaisesRegex(installer.InstallError, "supports Linux"):
            installer._supported_platform_name("win32")

    def test_common_activation_transaction_has_no_manager_commands(self) -> None:
        source = inspect.getsource(installer.activate_release)

        self.assertNotIn("systemctl", source)
        self.assertNotIn("launchctl", source)
        self.assertIn("backend.stop_and_confirm", source)
        self.assertIn("backend.publish_definition", source)
        self.assertIn("backend.start_and_wait", source)

    def test_macos_layout_and_launch_agent_definition_are_user_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._darwin_layout(Path(directory))
            installer.prepare_directories(layout)
            release = self._release(layout, "a" * 64)
            layout.secret_file.write_text("must-not-enter-plist", encoding="utf-8")

            content = installer.render_launch_agent(release, layout)
            payload = plistlib.loads(content)
            installer._write_atomic(layout.service_file, content, mode=0o600)
            installer._require_managed_launch_agent(layout.service_file, layout)

            self.assertEqual(layout.platform, "darwin")
            self.assertEqual(
                layout.service_file,
                layout.home
                / "Library/LaunchAgents/io.github.lijingda.netizen.plist",
            )
            self.assertEqual(payload["Label"], installer.LAUNCH_AGENT_LABEL)
            self.assertEqual(
                payload["ProgramArguments"],
                installer._launch_agent_program_arguments(layout),
            )
            self.assertEqual(payload["WorkingDirectory"], str(layout.home))
            self.assertIs(payload["RunAtLoad"], True)
            self.assertEqual(payload["KeepAlive"], {"SuccessfulExit": False})
            self.assertEqual(payload["ExitTimeOut"], 75)
            self.assertEqual(payload["Umask"], 0o077)
            self.assertEqual(payload["StandardOutPath"], "/dev/null")
            self.assertEqual(
                payload["StandardErrorPath"],
                str(layout.service_error_log),
            )
            environment = payload["EnvironmentVariables"]
            self.assertEqual(
                environment[installer.LAUNCH_AGENT_SENTINEL_NAME],
                installer.LAUNCH_AGENT_SENTINEL_VALUE,
            )
            self.assertIn("/opt/homebrew/bin", environment["PATH"].split(":"))
            self.assertNotIn("must-not-enter-plist", content.decode())
            self.assertEqual(stat.S_IMODE(layout.service_file.stat().st_mode), 0o600)
            with patch.dict(
                os.environ,
                {
                    "PATH": "/usr/bin",
                    "XDG_RUNTIME_DIR": "/run/user/wrong",
                    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/wrong/bus",
                },
                clear=True,
            ):
                subprocess_environment = installer._service_environment(layout)
            self.assertNotIn("XDG_RUNTIME_DIR", subprocess_environment)
            self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", subprocess_environment)

    def test_macos_launch_agent_permissions_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._darwin_layout(Path(directory))
            installer.prepare_directories(layout)
            release = self._release(layout, "8" * 64)
            installer._write_atomic(
                layout.service_file,
                installer.render_launch_agent(release, layout),
                mode=0o600,
            )
            layout.service_file.chmod(0o620)

            with self.assertRaisesRegex(
                installer.InstallError,
                "group/world writable",
            ):
                installer._require_managed_launch_agent(
                    layout.service_file,
                    layout,
                )

    def test_macos_fresh_activation_publishes_enables_and_starts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._darwin_layout(Path(directory))
            installer.prepare_directories(layout)
            candidate = self._release(layout, "9" * 64)
            runner, state, calls = self._launchd_runner(
                layout,
                ready_on_bootstrap=True,
            )

            installer.activate_release(
                candidate,
                layout,
                interactive=False,
                runner=runner,
            )

            target = f"gui/{layout.uid}/{installer.LAUNCH_AGENT_LABEL}"
            self.assertIn(["launchctl", "enable", target], calls)
            self.assertIn(
                [
                    "launchctl",
                    "bootstrap",
                    f"gui/{layout.uid}",
                    str(layout.service_file),
                ],
                calls,
            )
            self.assertIs(state["loaded"], True)
            self.assertTrue(layout.service_file.is_file())
            self.assertEqual(
                installer._read_release_link(layout.current, layout),
                candidate.root.resolve(),
            )
            self.assertIsNone(
                installer._read_release_link(layout.previous, layout)
            )

    def test_macos_gui_domain_preflight_fails_before_any_install_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._darwin_layout(Path(directory))
            calls: list[list[str]] = []

            def fake_runner(
                argv: list[object], **_kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                rendered = [os.fspath(value) for value in argv]
                calls.append(rendered)
                return subprocess.CompletedProcess(rendered, 1, "", "no gui domain")

            with (
                patch.object(installer, "require_supported_platform"),
                self.assertRaisesRegex(installer.InstallError, "GUI launchd domain"),
            ):
                installer.install_source(
                    source_root=ROOT,
                    layout=layout,
                    runner=fake_runner,
                    interactive=False,
                )

            self.assertEqual(
                calls,
                [["launchctl", "print", f"gui/{layout.uid}"]],
            )
            self.assertFalse(layout.product_root.exists())
            self.assertFalse(layout.service_file.exists())

    def test_macos_start_clears_sticky_disable_before_exact_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._darwin_layout(Path(directory))
            installer.prepare_directories(layout)
            release = self._release(layout, "b" * 64)
            installer._write_atomic(
                layout.service_file,
                installer.render_launch_agent(release, layout),
                mode=0o600,
            )
            runner, _state, calls = self._launchd_runner(
                layout,
                ready_on_bootstrap=True,
            )
            backend = installer.LaunchAgentServiceBackend(layout, runner)

            backend.preflight()
            backend.start_and_wait(timeout=0.1)

            target = f"gui/{layout.uid}/{installer.LAUNCH_AGENT_LABEL}"
            enable = ["launchctl", "enable", target]
            bootstrap = [
                "launchctl",
                "bootstrap",
                f"gui/{layout.uid}",
                str(layout.service_file),
            ]
            self.assertIn(enable, calls)
            self.assertIn(bootstrap, calls)
            self.assertLess(calls.index(enable), calls.index(bootstrap))
            self.assertNotIn("kickstart", [argument for call in calls for argument in call])

    def test_macos_restart_boots_out_before_enable_and_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._darwin_layout(Path(directory))
            installer.prepare_directories(layout)
            release = self._release(layout, "a" * 64)
            installer._write_atomic(
                layout.service_file,
                installer.render_launch_agent(release, layout),
                mode=0o600,
            )
            runner, _state, calls = self._launchd_runner(
                layout,
                initially_loaded=True,
                initially_ready=True,
                ready_on_bootstrap=True,
            )
            backend = installer.LaunchAgentServiceBackend(layout, runner)

            self.assertEqual(backend.service_action("restart"), 0)

            target = f"gui/{layout.uid}/{installer.LAUNCH_AGENT_LABEL}"
            bootout = ["launchctl", "bootout", target]
            enable = ["launchctl", "enable", target]
            bootstrap = [
                "launchctl",
                "bootstrap",
                f"gui/{layout.uid}",
                str(layout.service_file),
            ]
            self.assertLess(calls.index(bootout), calls.index(enable))
            self.assertLess(calls.index(enable), calls.index(bootstrap))

    def test_macos_loaded_without_ready_waits_without_rebootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._darwin_layout(Path(directory))
            installer.prepare_directories(layout)
            release = self._release(layout, "c" * 64)
            installer._write_atomic(
                layout.service_file,
                installer.render_launch_agent(release, layout),
                mode=0o600,
            )
            runner, _state, calls = self._launchd_runner(
                layout,
                initially_loaded=True,
            )
            backend = installer.LaunchAgentServiceBackend(layout, runner)

            with self.assertRaisesRegex(installer.InstallError, "did not become ready"):
                backend.start_and_wait(timeout=0.01)

            flattened = [argument for call in calls for argument in call]
            self.assertNotIn("bootstrap", flattened)
            self.assertNotIn("kickstart", flattened)

    def test_macos_bootstrap_response_loss_reconciles_by_loaded_and_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._darwin_layout(Path(directory))
            installer.prepare_directories(layout)
            release = self._release(layout, "d" * 64)
            installer._write_atomic(
                layout.service_file,
                installer.render_launch_agent(release, layout),
                mode=0o600,
            )
            runner, state, _calls = self._launchd_runner(
                layout,
                ready_on_bootstrap=True,
                lose_bootstrap_response=True,
            )
            backend = installer.LaunchAgentServiceBackend(layout, runner)

            backend.start_and_wait(timeout=0.1)

            self.assertIs(state["loaded"], True)
            self.assertTrue(installer._ready_marker_present(layout))

    def test_macos_refuses_unmanaged_plist_and_orphan_loaded_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._darwin_layout(Path(directory))
            installer.prepare_directories(layout)
            release = self._release(layout, "e" * 64)
            layout.service_file.write_bytes(
                plistlib.dumps(
                    {
                        "Label": installer.LAUNCH_AGENT_LABEL,
                        "ProgramArguments": ["/bin/other"],
                        "EnvironmentVariables": {
                            installer.LAUNCH_AGENT_SENTINEL_NAME:
                                installer.LAUNCH_AGENT_SENTINEL_VALUE,
                        },
                    }
                )
            )
            runner, _state, _calls = self._launchd_runner(layout)
            backend = installer.LaunchAgentServiceBackend(layout, runner)
            with self.assertRaisesRegex(installer.InstallError, "unrecognized LaunchAgent"):
                backend.capture_definition()

            layout.service_file.unlink()
            runner, _state, _calls = self._launchd_runner(
                layout,
                initially_loaded=True,
            )
            with self.assertRaisesRegex(installer.InstallError, "inspect it"):
                installer.activate_release(
                    release,
                    layout,
                    interactive=False,
                    runner=runner,
                )
            self.assertFalse(layout.current.exists())

    def test_macos_active_upgrade_stops_then_publishes_and_restarts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._darwin_layout(Path(directory))
            installer.prepare_directories(layout)
            old = self._release(layout, "1" * 64)
            candidate = self._release(layout, "2" * 64)
            installer._set_release_link(layout.current, old.root, layout)
            installer._write_atomic(
                layout.service_file,
                installer.render_launch_agent(old, layout),
                mode=0o600,
            )
            runner, _state, calls = self._launchd_runner(
                layout,
                initially_loaded=True,
                initially_ready=True,
                ready_on_bootstrap=True,
            )

            installer.activate_release(
                candidate,
                layout,
                interactive=False,
                runner=runner,
                data_dir=layout.state_dir,
            )

            target = f"gui/{layout.uid}/{installer.LAUNCH_AGENT_LABEL}"
            bootout = ["launchctl", "bootout", target]
            bootstrap = [
                "launchctl",
                "bootstrap",
                f"gui/{layout.uid}",
                str(layout.service_file),
            ]
            self.assertIn(bootout, calls)
            self.assertIn(bootstrap, calls)
            self.assertLess(calls.index(bootout), calls.index(bootstrap))
            self.assertEqual(
                installer._read_release_link(layout.current, layout),
                candidate.root.resolve(),
            )
            self.assertEqual(
                installer._read_release_link(layout.previous, layout),
                old.root.resolve(),
            )

    def test_macos_stopped_upgrade_stays_unloaded_but_enabled_for_next_login(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._darwin_layout(Path(directory))
            installer.prepare_directories(layout)
            old = self._release(layout, "3" * 64)
            candidate = self._release(layout, "4" * 64)
            installer._set_release_link(layout.current, old.root, layout)
            installer._write_atomic(
                layout.service_file,
                installer.render_launch_agent(old, layout),
                mode=0o600,
            )
            runner, state, calls = self._launchd_runner(layout)

            installer.activate_release(
                candidate,
                layout,
                interactive=False,
                runner=runner,
            )

            target = f"gui/{layout.uid}/{installer.LAUNCH_AGENT_LABEL}"
            self.assertIn(["launchctl", "enable", target], calls)
            self.assertNotIn("bootstrap", [argument for call in calls for argument in call])
            self.assertIs(state["loaded"], False)
            self.assertEqual(
                installer._read_release_link(layout.previous, layout),
                old.root.resolve(),
            )

    def test_macos_stop_requires_both_unloaded_target_and_released_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._darwin_layout(Path(directory))
            installer.prepare_directories(layout)
            release = self._release(layout, "5" * 64)
            installer._write_atomic(
                layout.service_file,
                installer.render_launch_agent(release, layout),
                mode=0o600,
            )
            descriptor = os.open(
                layout.lifetime_lock_file,
                os.O_RDWR | os.O_CREAT,
                0o600,
            )
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            runner, state, _calls = self._launchd_runner(
                layout,
                initially_loaded=True,
            )
            backend = installer.LaunchAgentServiceBackend(layout, runner)
            try:
                with self.assertRaisesRegex(
                    installer.InstallError,
                    "refusing to mutate rollback-protected state",
                ):
                    backend.stop_and_confirm(timeout=0.01)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

            self.assertIs(state["loaded"], False)

    def test_macos_failed_stop_skips_database_and_skill_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._darwin_layout(Path(directory))
            installer.prepare_directories(layout)
            candidate = self._release(layout, "6" * 64)
            database = layout.state_dir / "channel.sqlite3"
            database.write_text("old database", encoding="utf-8")
            runner, _state, _calls = self._launchd_runner(
                layout,
                ready_on_bootstrap=False,
            )

            def fail_ready(
                _backend: installer.LaunchAgentServiceBackend,
                *,
                timeout: float,
            ) -> None:
                del timeout
                database.write_text("candidate database", encoding="utf-8")
                raise installer.InstallError("candidate failed")

            with (
                patch.object(
                    installer.LaunchAgentServiceBackend,
                    "_wait_for_ready",
                    autospec=True,
                    side_effect=fail_ready,
                ),
                patch.object(
                    installer.LaunchAgentServiceBackend,
                    "stop_and_confirm",
                    side_effect=installer.InstallError("candidate still alive"),
                ),
                self.assertRaisesRegex(
                    installer.InstallError,
                    "restore database: skipped",
                ),
            ):
                installer.activate_release(
                    candidate,
                    layout,
                    interactive=False,
                    runner=runner,
                    data_dir=layout.state_dir,
                )

            self.assertEqual(database.read_text(), "candidate database")
            self.assertTrue(
                (layout.codex_home / "skills/netizen-user-guide/SKILL.md").is_file()
            )
            self.assertTrue(list(layout.state_dir.glob("rollback-recovery-*")))

    def test_macos_uninstall_removes_only_managed_artifacts_and_preserves_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = self._darwin_layout(Path(directory))
            installer.prepare_directories(layout)
            release = self._release(layout, "7" * 64)
            installer._set_release_link(layout.current, release.root, layout)
            installer._write_atomic(
                layout.service_file,
                installer.render_launch_agent(release, layout),
                mode=0o600,
            )
            preserved = layout.state_dir / "channel.sqlite3"
            preserved.write_text("state", encoding="utf-8")
            runner, _state, calls = self._launchd_runner(
                layout,
                initially_loaded=True,
            )

            with patch.object(installer, "require_supported_platform"):
                installer.uninstall(layout=layout, runner=runner)

            target = f"gui/{layout.uid}/{installer.LAUNCH_AGENT_LABEL}"
            self.assertIn(["launchctl", "bootout", target], calls)
            self.assertIn(["launchctl", "disable", target], calls)
            self.assertFalse(layout.service_file.exists())
            self.assertFalse(layout.current.exists())
            self.assertFalse(layout.releases.exists())
            self.assertFalse(layout.cache_dir.exists())
            self.assertEqual(preserved.read_text(), "state")

    def _darwin_layout(self, root: Path) -> installer.Layout:
        home = root / "home"
        home.mkdir(parents=True, exist_ok=True)
        return installer.resolve_layout(
            environ={},
            account_home=home,
            uid=os.geteuid(),
            username="current-user",
            platform_name="darwin",
        )

    def _launchd_runner(
        self,
        layout: installer.Layout,
        *,
        initially_loaded: bool = False,
        initially_ready: bool = False,
        ready_on_bootstrap: bool = False,
        lose_bootstrap_response: bool = False,
        domain_available: bool = True,
    ) -> tuple[
        object,
        dict[str, bool],
        list[list[str]],
    ]:
        state = {"loaded": initially_loaded}
        calls: list[list[str]] = []
        domain = f"gui/{layout.uid}"
        target = f"{domain}/{installer.LAUNCH_AGENT_LABEL}"
        if initially_ready:
            layout.ready_file.write_bytes(installer.READY_MARKER_CONTENT)
            layout.ready_file.chmod(0o600)

        def mark_ready() -> None:
            layout.ready_file.write_bytes(installer.READY_MARKER_CONTENT)
            layout.ready_file.chmod(0o600)

        def fake_runner(
            argv: list[object],
            **_kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            rendered = [os.fspath(value) for value in argv]
            calls.append(rendered)
            if rendered == ["launchctl", "print", domain]:
                return subprocess.CompletedProcess(
                    rendered,
                    0 if domain_available else 1,
                    "",
                    "",
                )
            if rendered == ["launchctl", "print", target]:
                return subprocess.CompletedProcess(
                    rendered,
                    0 if state["loaded"] else 1,
                    "",
                    "",
                )
            if rendered == ["launchctl", "enable", target]:
                return subprocess.CompletedProcess(rendered, 0, "", "")
            if rendered == ["launchctl", "disable", target]:
                return subprocess.CompletedProcess(rendered, 0, "", "")
            if rendered == ["launchctl", "bootout", target]:
                state["loaded"] = False
                return subprocess.CompletedProcess(rendered, 0, "", "")
            if rendered == [
                "launchctl",
                "bootstrap",
                domain,
                str(layout.service_file),
            ]:
                state["loaded"] = True
                if ready_on_bootstrap:
                    mark_ready()
                if lose_bootstrap_response:
                    raise installer.InstallError("bootstrap response lost")
                return subprocess.CompletedProcess(rendered, 0, "", "")
            if rendered == ["plutil", "-lint", str(layout.service_file)]:
                return subprocess.CompletedProcess(rendered, 0, "", "")
            raise AssertionError(f"unexpected command: {rendered}")

        return fake_runner, state, calls

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


def _stopped_backend() -> MagicMock:
    backend = MagicMock()
    backend.capture_definition.return_value = installer.FileSnapshot(False)
    backend.inspect_state.return_value = installer.ServiceState(False, False)
    backend.inspect_legacy.return_value = installer.LegacyServiceState()
    backend.render_definition.return_value = b"candidate service definition"
    return backend


def _write_v5_channel_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version(version) VALUES (5);
            CREATE TABLE scopes (
                scope_key TEXT PRIMARY KEY,
                app_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                topic_id TEXT,
                active_binding_id TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE bindings (
                binding_id TEXT PRIMARY KEY,
                scope_key TEXT NOT NULL REFERENCES scopes(scope_key),
                project_alias TEXT NOT NULL,
                native_thread_id TEXT UNIQUE,
                model_id TEXT,
                effort_id TEXT,
                service_tier_id TEXT,
                settings_revision INTEGER NOT NULL DEFAULT 1,
                creator_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                activated_at TEXT NOT NULL,
                ever_activated INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE projects (
                alias TEXT PRIMARY KEY,
                cwd TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                revision INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE dedup_keys (
                dedup_key TEXT PRIMARY KEY,
                expires_at REAL NOT NULL
            );
            CREATE TABLE side_topics (
                side_id TEXT PRIMARY KEY,
                app_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                topic_id TEXT,
                root_message_id TEXT,
                source_message_id TEXT NOT NULL,
                parent_binding_id TEXT NOT NULL,
                creator_id TEXT NOT NULL,
                requires_mention INTEGER NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(app_id, source_message_id)
            );
            INSERT INTO projects(
                alias, cwd, enabled, revision, created_at, updated_at
            ) VALUES (
                'legacy', '/tmp/legacy', 1, 1,
                '2029-01-01T00:00:00+00:00',
                '2029-01-01T00:00:00+00:00'
            );
            INSERT INTO scopes(
                scope_key, app_id, chat_id, kind, topic_id,
                active_binding_id, updated_at
            ) VALUES (
                'cli_test:direct:oc_legacy', 'cli_test', 'oc_legacy',
                'direct', NULL, 'binding-legacy',
                '2029-01-01T00:00:00+00:00'
            );
            INSERT INTO bindings(
                binding_id, scope_key, project_alias, native_thread_id,
                model_id, effort_id, service_tier_id, settings_revision,
                creator_id, created_at, activated_at, ever_activated
            ) VALUES (
                'binding-legacy', 'cli_test:direct:oc_legacy', 'legacy',
                'thread-legacy', NULL, NULL, NULL, 1, 'ou_legacy',
                '2029-01-01T00:00:00+00:00',
                '2029-01-01T00:00:00+00:00', 1
            );
            INSERT INTO side_topics(
                side_id, app_id, chat_id, topic_id, root_message_id,
                source_message_id, parent_binding_id, creator_id,
                requires_mention, state, created_at, updated_at
            ) VALUES (
                'side-legacy', 'cli_test', 'oc_legacy', 'omt_side',
                'om_root', 'om_source', 'binding-legacy', 'ou_legacy', 1,
                'closed', '2029-01-02T00:00:00+00:00',
                '2029-01-02T00:00:00+00:00'
            );
            """
        )
        connection.commit()
    finally:
        connection.close()


if __name__ == "__main__":
    unittest.main()
