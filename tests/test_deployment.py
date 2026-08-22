from __future__ import annotations

import re
import tempfile
import tomllib
import unittest
from pathlib import Path

from scripts.verify_installed_release import (
    InstalledReleaseMismatch,
    verify_installed_release,
)


ROOT = Path(__file__).resolve().parents[1]


class DeploymentAssetsTest(unittest.TestCase):
    def test_admin_static_assets_are_packaged_with_admin_module(self) -> None:
        project = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )

        self.assertEqual(
            project["tool"]["setuptools"]["package-data"]["netizen.admin"],
            ["static/*.html", "static/*.css", "static/*.js"],
        )

    def test_deployment_requires_an_explicit_target_and_explains_local_notes(
        self,
    ) -> None:
        deployment = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        template = (ROOT / "LOCAL_ENVIRONMENT.example.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("仓库不定义默认服务器、SSH alias、账号或远端 checkout", deployment)
        self.assertIn("`<deployment-host>`", deployment)
        self.assertIn("defines no default remote host, SSH alias, account", agents)
        self.assertIn("`LOCAL_ENVIRONMENT.md`", agents)
        self.assertIn("not a Netizen runtime configuration file", template)
        self.assertIn("`<ssh-config-alias-or-user-at-host>`", template)

    def test_readme_local_development_names_its_prerequisites(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        local_development = readme.split("## 本地开发", 1)[1].split(
            "## 发布验证状态", 1
        )[0]

        for expected in (
            "Python 3.11+",
            "macOS 和 Linux",
            "只支持 Linux + systemd",
            "`make check`",
            "使用 fake App Server",
            "codex login status",
            "codex exec --skip-git-repo-check",
        ):
            self.assertIn(expected, local_development)

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

    def test_deployment_uses_content_addressed_one_command_install(self) -> None:
        deployment = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")

        self.assertIn("./install.sh", deployment)
        self.assertIn("releases/<sha256>", deployment)
        self.assertIn("current -> releases/<sha256>", deployment)
        self.assertIn("previous -> releases/<sha256>", deployment)
        self.assertIn("不执行 `git pull`", deployment)
        self.assertIn("~/.netizen/", deployment)
        self.assertIn("credentials/", deployment)
        self.assertIn("有意忽略", deployment)
        self.assertIn(".netizen-managed", deployment)
        self.assertIn(".activation-intent.json", deployment)
        self.assertIn("不会被误认成用户主动停止/禁用服务", deployment)
        self.assertNotIn("${XDG_DATA_HOME", deployment)
        self.assertIn("scripts/verify_installed_release.py", deployment)
        self.assertIn("已有有效配置和 Secret 的升级天然非交互", deployment)

    def test_deployment_documents_service_start_shell_environment(self) -> None:
        deployment = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        for document in (deployment, readme):
            self.assertIn("`.bashrc`", document)
            self.assertIn("interactive", document)
            self.assertIn("login", document)
            self.assertIn("每次", document)
            self.assertIn("NVM", document)
        self.assertIn("不会再由 Codex", deployment)
        self.assertIn("不再", readme)
        self.assertIn("无 TTY", deployment)
        self.assertIn("10 秒", deployment)
        self.assertIn("`exec`", deployment)
        self.assertIn("logout", deployment)
        self.assertIn("SHA-256", deployment)
        self.assertIn("45 秒", deployment)
        self.assertIn("`-E -B -u`", deployment)
        self.assertIn("shell_environment_policy", deployment)
        self.assertIn("`allow_login_shell=false`", deployment)
        self.assertIn("不写死 PATH", deployment)
        self.assertIn("代理、CA", readme)

    def test_deployment_fully_replaces_the_global_user_guide_skill(self) -> None:
        deployment = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")

        self.assertIn("${CODEX_HOME:-~/.codex}/skills/netizen-user-guide", deployment)
        self.assertIn("完整替换", deployment)
        self.assertIn("其他 Skill 不会被读取或修改", deployment)
        self.assertIn("按安装前快照恢复“原本不存在”", deployment)
        self.assertIn("受管用户指南 Skill", deployment)
        self.assertIn("$netizen-user-guide 如何切换会话？", deployment)

    def test_release_gate_includes_native_thread_lifecycle_probe(self) -> None:
        deployment = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
        probe = (ROOT / "scripts" / "probe_python_sdk.py").read_text(
            encoding="utf-8"
        )

        self.assertRegex(
            deployment,
            r"for phase in [^\n]*\blifecycle\b",
        )
        self.assertIn("thread_list(archived=False|True)", deployment)
        self.assertIn("它不会调用\n`thread/delete`", deployment)
        lifecycle = probe.split("async def _thread_lifecycle_live", 1)[1].split(
            "async def _steer", 1
        )[0]
        self.assertNotIn("ThreadDelete", lifecycle)
        self.assertNotIn(".delete(", lifecycle)

    def test_release_gate_includes_non_consuming_turn_plan_probes(self) -> None:
        deployment = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
        probe = (ROOT / "scripts" / "probe_python_sdk.py").read_text(
            encoding="utf-8"
        )

        self.assertRegex(deployment, r"for phase in [^\n]*\bplan\b")
        self.assertIn("async def _turn_plan_live", probe)
        self.assertIn("PinnedTurnPlanObserver", probe)
        self.assertIn("async for notification in handle.stream()", probe)

    def test_release_gate_includes_ephemeral_multi_turn_side_probe(self) -> None:
        deployment = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
        probe = (ROOT / "scripts" / "probe_python_sdk.py").read_text(
            encoding="utf-8"
        )

        self.assertRegex(deployment, r"for phase in [^\n]*\bside\b")
        self.assertIn("async def _side_live", probe)
        self.assertIn("thread_fork(parent.id, ephemeral=True)", probe)
        self.assertIn("AppServerSideBoundaryControl", probe)
        self.assertIn("AppServerThreadSubscriptionControl", probe)
        side = probe.split("async def _side_live", 1)[1].split(
            "async def _steer", 1
        )[0]
        self.assertIn('(\"SIDE-ONE\", \"SIDE-TWO\")', side)
        self.assertIn("side_turn_ids", side)
        self.assertIn("await handle.run()", side)
        self.assertIn("_public_final_response(parent, seed.id)", side)
        self.assertIn("_public_final_response(parent, parent_after.id)", side)
        self.assertIn("parent_side_overlap_pids", side)
        self.assertLess(
            side.index("parent_running = await parent.turn"),
            side.index("thread_fork(parent.id, ephemeral=True)"),
        )
        self.assertIn("await boundary.inject_boundary(side.id)", side)
        self.assertIn("await subscription.unsubscribe(side.id)", side)
        self.assertIn("父 Turn 正在运行", deployment)
        self.assertIn("多个 Side", deployment)
        self.assertIn("相同 UUID", deployment)
        self.assertIn("Parent 仍能继续", deployment)

    def test_release_gate_uses_two_app_servers_and_exact_same_id_resume(self) -> None:
        deployment = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
        probe = (ROOT / "scripts" / "probe_python_sdk.py").read_text(
            encoding="utf-8"
        )

        self.assertRegex(deployment, r"--phase release")
        release = probe.split("async def _release_live", 1)[1].split(
            "async def _side_live", 1
        )[0]
        self.assertGreaterEqual(release.count("async with AsyncCodex()"), 2)
        self.assertIn("AppServerThreadSubscriptionControl", release)
        self.assertIn("await inspector.has_running", release)
        self.assertIn("await subscription.unsubscribe", release)
        self.assertIn("await first.thread_resume(thread.id)", release)
        self.assertIn("await second.thread_resume(thread_id)", release)
        self.assertIn("same_id_resumed_by_fresh_app_server", release)
        self.assertIn("30 分钟", deployment)

    def test_current_schema_documents_side_route_tombstones(self) -> None:
        deployment = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
        design = (ROOT / "docs" / "design.md").read_text(encoding="utf-8")

        self.assertIn("当前 schema v5", deployment)
        self.assertIn("`side_topics`", design)
        self.assertIn("不保存\nephemeral native Thread ID", design)

    def test_make_check_covers_safe_local_release_gates(self) -> None:
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

        self.assertIn("check: test", makefile)
        self.assertIn("unittest discover -s tests -v", makefile)
        self.assertIn("compileall -q netizen scripts tests", makefile)
        self.assertIn("pip check", makefile)
        self.assertIn("probe_sdk_turn_plan.py --timeout 5", makefile)
        self.assertIn(
            "probe_sdk_completion_race.py --read-recovery --attempts 20 --timeout 3",
            makefile,
        )
        self.assertIn(
            "probe_sdk_completion_race.py --usage-drain --attempts 40 --timeout 10",
            makefile,
        )
        self.assertNotIn("probe_python_sdk.py", makefile)

    def test_local_markdown_links_resolve(self) -> None:
        documents = [
            ROOT / "README.md",
            ROOT / "AGENTS.md",
            ROOT / "CONTEXT.md",
            *sorted((ROOT / "docs").rglob("*.md")),
            *sorted((ROOT / "skills").rglob("*.md")),
        ]

        for document in documents:
            text = document.read_text(encoding="utf-8")
            for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", text):
                if "://" in target or target.startswith(("#", "mailto:")):
                    continue
                path = target.split("#", 1)[0]
                if not path:
                    continue
                with self.subTest(document=document.relative_to(ROOT), target=target):
                    self.assertTrue((document.parent / path).resolve().exists())

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
        self.assertIn("profile capture", unit)
        self.assertNotIn("XDG_DATA_HOME", unit)
        self.assertNotIn("XDG_CONFIG_HOME", unit)
        self.assertIn("WantedBy=default.target", unit)
        self.assertIn("KillMode=control-group", unit)
        self.assertNotIn("approval", unit.lower())
        self.assertNotIn("ProtectHome", unit)
        self.assertNotIn("@openai/codex-sdk", unit)

    def test_example_config_uses_alias_to_one_canonical_cwd_shape(self) -> None:
        config = (ROOT / "config.example.yaml").read_text(encoding="utf-8")

        self.assertIn("Manual-development example", config)
        self.assertIn("./install.sh does not copy this file", config)
        self.assertIn("`projects: {}`", config)
        self.assertIn("defaultCwd:", config)
        self.assertIn("projectRoot:", config)
        self.assertIn("test: /home/your-user/projects/test", config)
        self.assertIn("adminWeb:", config)
        self.assertIn("host: 0.0.0.0", config)
        self.assertIn("port: 8787", config)
        self.assertNotIn("allowedUsers:", config)
        self.assertNotIn("allowedChats:", config)
        self.assertNotIn("operators:", config)
        self.assertNotIn("groupProfile", config)

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
        self.assertIn("openai-codex-cli-bin==0.147.0", constraints)
