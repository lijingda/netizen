from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from netizen.bindings import BindingStore
from netizen.projects import (
    ProjectDisabled,
    ProjectError,
    ProjectRegistry,
    StaleProject,
    UnknownProject,
)
from netizen.settings import AdminWebSettings, Settings, SettingsError


class SettingsTest(unittest.TestCase):
    def environment(self, directory: Path, **values: str) -> dict[str, str]:
        credential = directory / "admin-web-secret"
        credential.write_text("A" * 43, encoding="ascii")
        credential.chmod(0o600)
        return {
            "NETIZEN_ADMIN_SECRET_FILE": str(credential),
            **values,
        }

    def write_config(self, directory: Path) -> Path:
        default = directory / "default"
        project = directory / "project"
        default.mkdir()
        project.mkdir()
        path = directory / "config.yaml"
        path.write_text(
            "instance:\n"
            "  appId: cli_test\n"
            f"  dataDir: {directory / 'data'}\n"
            f"  defaultCwd: {default}\n"
            "projects:\n"
            f"  test: {project}\n"
            "channel:\n"
            "  securityMode: audit\n",
            encoding="utf-8",
        )
        return path

    def test_loads_yaml_and_direct_development_secret(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            settings = Settings.from_file(
                self.write_config(directory),
                self.environment(directory, FEISHU_APP_SECRET="secret"),
            )

        self.assertEqual(settings.app_id, "cli_test")
        self.assertEqual(settings.app_secret, "secret")
        self.assertEqual(settings.projects["test"].name, "project")
        self.assertEqual(settings.project_root, settings.default_cwd.parent)
        self.assertEqual(settings.admin_web.host, "0.0.0.0")
        self.assertEqual(settings.admin_web.port, 8787)
        self.assertTrue(settings.admin_web.enabled)

    def test_explicit_project_root_is_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            project_root = directory / "managed-projects"
            project_root.mkdir()
            config = self.write_config(directory)
            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    "projects:\n",
                    f"  projectRoot: {project_root}\nprojects:\n",
                ),
                encoding="utf-8",
            )
            settings = Settings.from_file(
                config,
                self.environment(directory, FEISHU_APP_SECRET="secret"),
            )

        self.assertEqual(settings.project_root, project_root)

    def test_secret_file_must_be_private_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            config = self.write_config(directory)
            secret = directory / "secret"
            secret.write_text("protected\n", encoding="utf-8")
            secret.chmod(0o600)

            settings = Settings.from_file(
                config,
                self.environment(directory, FEISHU_APP_SECRET_FILE=str(secret)),
            )
            self.assertEqual(settings.app_secret, "protected")

            secret.chmod(0o644)
            with self.assertRaisesRegex(SettingsError, "0600"):
                Settings.from_file(
                    config,
                    self.environment(
                        directory,
                        FEISHU_APP_SECRET_FILE=str(secret),
                    ),
                )

    def test_legacy_access_section_is_rejected_instead_of_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            config = self.write_config(directory)
            config.write_text(
                config.read_text(encoding="utf-8")
                + "access:\n  allowedUsers:\n    - ou_legacy\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                SettingsError,
                "configure availability in the Feishu app console",
            ):
                Settings.from_file(
                    config,
                    self.environment(directory, FEISHU_APP_SECRET="secret"),
                )

    def test_channel_section_must_be_a_mapping_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            config = self.write_config(directory)
            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    "channel:\n  securityMode: audit\n",
                    "channel: invalid\n",
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(SettingsError, "channel must be a mapping"):
                Settings.from_file(
                    config,
                    self.environment(directory, FEISHU_APP_SECRET="secret"),
                )

    def test_missing_secret_file_is_a_settings_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            config = self.write_config(directory)

            with self.assertRaisesRegex(SettingsError, "cannot be read"):
                Settings.from_file(
                    config,
                    self.environment(
                        directory,
                        FEISHU_APP_SECRET_FILE=str(directory / "missing"),
                    ),
                )

    def test_admin_web_override_disable_and_validation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            config = self.write_config(directory)
            config.write_text(
                config.read_text(encoding="utf-8")
                + "adminWeb:\n  enabled: true\n  host: 127.0.0.1\n  port: 9443\n",
                encoding="utf-8",
            )
            loaded = Settings.from_file(
                config,
                self.environment(directory, FEISHU_APP_SECRET="secret"),
            )
            self.assertEqual(
                loaded.admin_web,
                AdminWebSettings(
                    enabled=True,
                    host="127.0.0.1",
                    port=9443,
                    credential_path=directory / "admin-web-secret",
                ),
            )

            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    "enabled: true", "enabled: false"
                ),
                encoding="utf-8",
            )
            disabled = Settings.from_file(
                config,
                {"FEISHU_APP_SECRET": "secret"},
            )
            self.assertFalse(disabled.admin_web.enabled)
            self.assertIsNone(disabled.admin_web.credential_path)

            invalid_cases = (
                ("enabled: false", "enabled: 1", "boolean"),
                ("host: 127.0.0.1", "host: ''", "non-empty host"),
                ("port: 9443", "port: 0", "1 to 65535"),
                ("port: 9443", "port: true", "1 to 65535"),
            )
            baseline = config.read_text(encoding="utf-8")
            for old, new, message in invalid_cases:
                with self.subTest(new=new):
                    config.write_text(baseline.replace(old, new), encoding="utf-8")
                    with self.assertRaisesRegex(SettingsError, message):
                        Settings.from_file(
                            config,
                            {"FEISHU_APP_SECRET": "secret"},
                        )

    def test_enabled_admin_requires_absolute_path_and_rejects_raw_secret(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            config = self.write_config(directory)
            with self.assertRaisesRegex(SettingsError, "required"):
                Settings.from_file(config, {"FEISHU_APP_SECRET": "secret"})
            with self.assertRaisesRegex(SettingsError, "must be an absolute path"):
                Settings.from_file(
                    config,
                    {
                        "FEISHU_APP_SECRET": "secret",
                        "NETIZEN_ADMIN_SECRET_FILE": "relative",
                    },
                )
            with self.assertRaisesRegex(SettingsError, "not supported"):
                Settings.from_file(
                    config,
                    {
                        "FEISHU_APP_SECRET": "secret",
                        "NETIZEN_ADMIN_SECRET": "raw-secret",
                    },
                )


class ProjectRegistryTest(unittest.TestCase):
    def test_none_and_named_projects_resolve_to_canonical_shared_cwds(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            default = root / "default"
            project = root / "project"
            default.mkdir()
            project.mkdir()
            store = BindingStore()
            try:
                registry = ProjectRegistry(
                    store=store,
                    default_cwd=default,
                    projects={"test": project},
                )

                self.assertEqual(registry.resolve("none").cwd, default.resolve())
                self.assertEqual(registry.resolve("test").cwd, project.resolve())
                self.assertEqual(registry.aliases(), ("none", "test"))
            finally:
                store.close()

    def test_registry_rejects_reserved_unknown_and_relative_projects(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            root.chmod(0o700)
            store = BindingStore()
            try:
                with self.assertRaisesRegex(ValueError, "保留"):
                    ProjectRegistry(
                        store=store,
                        default_cwd=root,
                        projects={"none": root},
                    )
                with self.assertRaisesRegex(ValueError, "绝对路径"):
                    ProjectRegistry(
                        store=store,
                        default_cwd=root,
                        projects={"test": Path("relative")},
                    )
                registry = ProjectRegistry(
                    store=store,
                    default_cwd=root,
                    projects={},
                )
                with self.assertRaises(UnknownProject):
                    registry.resolve("missing")
            finally:
                store.close()

    def test_registry_accepts_home_style_symlink_but_stores_real_path(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            real = root / "real"
            alias = root / "alias"
            real.mkdir()
            alias.symlink_to(real, target_is_directory=True)

            store = BindingStore()
            try:
                registry = ProjectRegistry(
                    store=store,
                    default_cwd=alias,
                    projects={},
                )

                self.assertEqual(registry.resolve("none").cwd, real.resolve())
            finally:
                store.close()

    def test_disabled_project_blocks_new_but_existing_binding_resolution_works(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            default = root / "default"
            project = root / "project"
            default.mkdir()
            project.mkdir()
            store = BindingStore()
            try:
                registry = ProjectRegistry(
                    store=store,
                    default_cwd=default,
                    projects={"test": project},
                )
                original = registry.resolve_for_new("test")
                disabled = registry.set_enabled(
                    alias="test",
                    enabled=False,
                    expected_revision=original.revision,
                )

                with self.assertRaises(ProjectDisabled):
                    registry.resolve_for_new("test")
                self.assertEqual(
                    registry.resolve_for_binding("test").cwd,
                    project.resolve(),
                )
                with self.assertRaises(StaleProject):
                    registry.set_enabled(
                        alias="test",
                        enabled=True,
                        expected_revision=original.revision,
                    )
                enabled = registry.set_enabled(
                    alias="test",
                    enabled=True,
                    expected_revision=disabled.revision,
                )
                self.assertTrue(enabled.enabled)
            finally:
                store.close()

    def test_feishu_managed_projects_survive_restart_and_yaml_does_not_reenable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            default = root / "default"
            seed = root / "seed"
            state = root / "channel.sqlite3"
            default.mkdir()
            seed.mkdir()
            first_store = BindingStore(state)
            first = ProjectRegistry(
                store=first_store,
                default_cwd=default,
                projects={"seed": seed},
            )
            seeded = first.resolve_for_new("seed")
            first.set_enabled(
                alias="seed",
                enabled=False,
                expected_revision=seeded.revision,
            )
            first_store.close()

            second_store = BindingStore(state)
            try:
                second = ProjectRegistry(
                    store=second_store,
                    default_cwd=default,
                    projects={"seed": root / "now-missing"},
                )
                self.assertFalse(
                    next(project for project in second.list() if project.alias == "seed").enabled
                )
            finally:
                second_store.close()

    def test_create_is_confined_to_project_root_and_existing_paths_can_be_registered(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            default = root / "default"
            project_root = root / "projects"
            outside = root / "outside"
            default.mkdir()
            project_root.mkdir()
            outside.mkdir()
            store = BindingStore()
            try:
                registry = ProjectRegistry(
                    store=store,
                    default_cwd=default,
                    project_root=project_root,
                    projects={},
                )
                created = registry.register(
                    alias="created",
                    path=None,
                    create_directory=True,
                )
                self.assertEqual(created.cwd, (project_root / "created").resolve())
                self.assertTrue(created.cwd.is_dir())

                with self.assertRaisesRegex(ProjectError, "projectRoot"):
                    registry.register(
                        alias="escaped",
                        path=str(outside / "escaped"),
                        create_directory=True,
                    )
                self.assertFalse((outside / "escaped").exists())

                registered = registry.register(
                    alias="outside",
                    path=str(outside),
                    create_directory=False,
                )
                self.assertEqual(registered.cwd, outside.resolve())
                registry.set_enabled(
                    alias="outside",
                    enabled=False,
                    expected_revision=registered.revision,
                )
                self.assertTrue(outside.exists())
            finally:
                store.close()
