from __future__ import annotations

import tempfile
import tomllib
import unittest
from pathlib import Path

import yaml

from scripts.verify_installed_release import (
    InstalledReleaseMismatch,
    verify_installed_release,
)
from tests.documentation_links import local_link_errors


ROOT = Path(__file__).resolve().parents[1]


class DeploymentAssetsTest(unittest.TestCase):
    def test_package_metadata_declares_the_supported_python_range(self) -> None:
        project = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(project["project"]["requires-python"], ">=3.11,<3.15")

    def test_admin_static_assets_are_packaged_with_admin_module(self) -> None:
        project = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )

        self.assertEqual(
            project["tool"]["setuptools"]["package-data"]["netizen.admin"],
            ["static/*.html", "static/*.css", "static/*.js"],
        )

    def test_installed_release_probe_detects_stale_and_shadowed_packages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "release"
            source_package = source_root / "netizen"
            runtime_prefix = root / "candidate-venv"
            installed_package = runtime_prefix / "site-packages" / "netizen"
            source_package.mkdir(parents=True)
            installed_package.mkdir(parents=True)
            (source_package / "__init__.py").write_text(
                "release = 1\n",
                encoding="utf-8",
            )
            (installed_package / "__init__.py").write_text(
                "release = 1\n",
                encoding="utf-8",
            )
            (source_package / "admin/static").mkdir(parents=True)
            (installed_package / "admin/static").mkdir(parents=True)
            (source_package / "admin/static/index.html").write_text(
                "release asset\n", encoding="utf-8"
            )
            (installed_package / "admin/static/index.html").write_text(
                "release asset\n", encoding="utf-8"
            )

            self.assertEqual(
                verify_installed_release(
                    source_root=source_root,
                    installed_package=installed_package,
                    runtime_prefix=runtime_prefix,
                ),
                2,
            )

            (installed_package / "__init__.py").write_text(
                "release = 0\n",
                encoding="utf-8",
            )
            with self.assertRaises(InstalledReleaseMismatch):
                verify_installed_release(
                    source_root=source_root,
                    installed_package=installed_package,
                    runtime_prefix=runtime_prefix,
                )

            with self.assertRaisesRegex(
                InstalledReleaseMismatch,
                "resolved to the source tree",
            ):
                verify_installed_release(
                    source_root=source_root,
                    installed_package=source_package,
                    runtime_prefix=runtime_prefix,
                )

    def test_local_documentation_links_and_anchors_resolve(self) -> None:
        documents = [
            ROOT / "README.md",
            ROOT / "AGENTS.md",
            ROOT / "CONTEXT.md",
            *sorted((ROOT / "docs").rglob("*.md")),
            *sorted((ROOT / "skills").rglob("*.md")),
        ]

        self.assertEqual(local_link_errors(ROOT, documents), [])

    def test_unit_template_is_for_one_per_user_python_service(self) -> None:
        unit = (ROOT / "deploy/netizen.service").read_text(encoding="utf-8")

        self.assertNotIn("User=", unit)
        self.assertNotIn("Group=", unit)
        self.assertIn("WorkingDirectory=%h", unit)
        self.assertNotIn("EnvironmentFile=", unit)
        self.assertNotIn("ExecStartPre=", unit)
        self.assertNotIn("service.env", unit)
        self.assertIn("Environment=@HOME_ENV@", unit)
        self.assertIn("Environment=@CODEX_HOME_ENV@", unit)
        self.assertIn("ExecStart=@EXEC_START@", unit)
        self.assertIn("Environment=@SECRET_ENV@", unit)
        self.assertIn("Environment=@ADMIN_SECRET_ENV@", unit)
        self.assertIn("NETIZEN_ADMIN_SECRET", unit)
        self.assertIn("TimeoutStopSec=75s", unit)
        self.assertNotIn("XDG_DATA_HOME", unit)
        self.assertNotIn("XDG_CONFIG_HOME", unit)
        self.assertIn("WantedBy=default.target", unit)
        self.assertIn("KillMode=control-group", unit)
        self.assertNotIn("approval", unit.lower())
        self.assertNotIn("ProtectHome", unit)
        self.assertNotIn("@openai/codex-sdk", unit)

    def test_example_config_uses_alias_to_one_canonical_cwd_shape(self) -> None:
        config = yaml.safe_load(
            (ROOT / "config.example.yaml").read_text(encoding="utf-8")
        )

        self.assertEqual(set(config), {"instance", "projects", "channel", "adminWeb"})
        self.assertEqual(set(config["instance"]), {"appId", "dataDir", "projectRoot"})
        self.assertEqual(config["instance"]["projectRoot"], "/home/your-user/projects")
        self.assertEqual(config["projects"], {"test": "/home/your-user/projects/test"})
        self.assertEqual(config["channel"], {"securityMode": "audit"})
        self.assertEqual(
            config["adminWeb"], {"enabled": True, "host": "0.0.0.0", "port": 8787}
        )

    def test_every_direct_runtime_dependency_has_an_exact_constraint(self) -> None:
        project = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        constraints = {
            line.lower()
            for raw in (ROOT / "requirements.lock").read_text(
                encoding="utf-8"
            ).splitlines()
            if (line := raw.strip()) and not line.startswith("#")
        }

        for dependency in project["project"]["dependencies"]:
            self.assertIn(dependency.lower(), constraints)
        self.assertIn("openai-codex-cli-bin==0.154.0", constraints)
