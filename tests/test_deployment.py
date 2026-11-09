from __future__ import annotations

import tomllib
from importlib.metadata import version
import unittest
from pathlib import Path

import yaml

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
            project["tool"]["setuptools"]["package-data"]["netizen_cli.admin"],
            ["static/*.html", "static/*.css", "static/*.js", "static/*.svg",
             "static/vendor/ace/*.js", "static/vendor/ace/LICENSE",
             "static/vendor/ace/README.md"],
        )

    def test_local_documentation_links_and_anchors_resolve(self) -> None:
        documents = [
            ROOT / "README.md",
            ROOT / "AGENTS.md",
            ROOT / "CONTEXT.md",
            # Local, ignored research snapshots can refer to historical source
            # layouts. They are not shipped contributor/user documentation.
            *sorted(path for path in (ROOT / "docs").rglob("*.md")
                    if not path.is_relative_to(ROOT / "docs/research")),
            *sorted((ROOT / "skills").rglob("*.md")),
        ]

        self.assertEqual(local_link_errors(ROOT, documents), [])

    def test_example_config_uses_alias_to_one_canonical_cwd_shape(self) -> None:
        config = yaml.safe_load(
            (ROOT / "config.example.yaml").read_text(encoding="utf-8")
        )

        self.assertEqual(
            set(config), {"instance", "projects", "channel", "adminWeb", "sessionTools", "worktrees", "questionNotifications", "groupAccess"}
        )
        self.assertEqual(set(config["instance"]), {"dataDir", "projectRoot"})
        self.assertEqual(config["instance"]["projectRoot"], "/home/your-user/projects")
        self.assertEqual(config["projects"], {"test": "/home/your-user/projects/test"})
        self.assertEqual(config["channel"], {"securityMode": "audit"})
        self.assertEqual(config["worktrees"], {"fetchBeforeCreate": False})
        self.assertEqual(config["questionNotifications"], {"mentionInitiator": False})
        self.assertEqual(config["groupAccess"], {"mode": "all", "chatIds": []})
        self.assertEqual(config["sessionTools"], {
            "discovery": {"mode": "all", "chatIds": []},
            "readSend": {"mode": "all", "chatIds": []},
        })
        self.assertEqual(
            config["adminWeb"], {"host": "0.0.0.0"}
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
        from netizen_cli import terminal_cleanup, turn_plan_observer

        sdk = version("openai-codex")
        self.assertIn(f"openai-codex=={sdk}", project["project"]["dependencies"])
        self.assertIn(f"openai-codex=={sdk}", constraints)
        self.assertEqual(terminal_cleanup.SUPPORTED_SDK_VERSION, sdk)
        self.assertEqual(turn_plan_observer.SUPPORTED_SDK_VERSION, sdk)
        self.assertEqual(version("openai-codex-cli-bin"), sdk)
        self.assertIn(f"openai-codex-cli-bin=={sdk}", constraints)

    def test_main_ci_runs_repository_gate_for_supported_python_versions(self) -> None:
        workflow_path = ROOT / ".github" / "workflows" / "ci.yml"
        workflow = yaml.load(
            workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader
        )
        for event in ("pull_request", "push"):
            self.assertEqual(workflow["on"][event]["branches"], ["main"])
        self.assertEqual(workflow["permissions"]["contents"], "read")

        for job_name, runner, versions in (
            ("check", "ubuntu-latest", ["3.11", "3.12", "3.13", "3.14"]),
            ("macos-arm64-check", "macos-15", ["3.14"]),
        ):
            with self.subTest(job=job_name):
                job = workflow["jobs"][job_name]
                self.assertEqual(job["runs-on"], runner)
                self.assertEqual(job["strategy"]["matrix"]["python"], versions)
                actions = {
                    step["uses"].split("@")[0]: step.get("with", {})
                    for step in job["steps"] if "uses" in step
                }
                self.assertEqual(
                    actions["actions/setup-node"]["node-version"], "22"
                )
                self.assertEqual(
                    actions["actions/setup-python"]["python-version"],
                    "${{ matrix.python }}",
                )
                commands = [
                    step["run"] for step in job["steps"] if "run" in step
                ]
                self.assertIn("make check", commands)
                self.assertTrue(any(
                    "--constraint requirements.lock" in command
                    and "setuptools==80.9.0" in command
                    for command in commands
                ))
                trust_checks = [
                    command for command in commands
                    if "_configure_platform_trust" in command
                ]
                if job_name == "macos-arm64-check":
                    self.assertTrue(any(
                        'test "$(uname -m)" = arm64' in command
                        for command in commands
                    ))
                    self.assertEqual(len(trust_checks), 1)
                    self.assertIn("from netizen_cli.main", trust_checks[0])
                else:
                    self.assertEqual(trust_checks, [])
