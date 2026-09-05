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
    def test_package_metadata_declares_the_supported_python_range(self) -> None:
        project = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        requirements_header = (ROOT / "requirements.lock").read_text(
            encoding="utf-8"
        ).splitlines()[0]

        self.assertEqual(project["project"]["requires-python"], ">=3.11,<3.15")
        self.assertEqual(
            requirements_header,
            "# Exact runtime constraints for the supported standard CPython "
            "3.11-3.14 Pilot.",
        )

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
        after_local_development = readme.split("## 本地开发", 1)[1]
        local_development, _ = after_local_development.split(
            "## 开发与兼容性验证", 1
        )

        for expected in (
            "Python 3.11-3.14",
            "macOS 和 Linux",
            "macOS LaunchAgent",
            "Linux systemd",
            "`make check`",
            "使用 fake App Server",
            "bundled App Server",
            "codex login status",
            "codex exec --skip-git-repo-check",
        ):
            self.assertIn(expected, local_development)

    def test_managed_install_auth_gate_is_client_and_platform_agnostic(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        deployment = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")

        for document in (readme, deployment):
            self.assertIn("Codex CLI 或 Codex App", document)
            self.assertIn("bundled Codex runtime", document)
            self.assertNotIn("同一 Unix 用户安装官方 Codex CLI", document)
            self.assertNotIn("当前 Unix 用户的登录有效", document)
            self.assertNotIn("`PATH` 中已安装的官方 Codex CLI", document)

    def test_macos_launchagent_contract_is_public_and_platform_bounded(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        deployment = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
        adr = (
            ROOT / "docs/adr/0034-support-macos-with-a-user-launchagent.md"
        ).read_text(encoding="utf-8")

        for document in (readme, deployment, adr):
            self.assertIn("LaunchAgent", document)
            self.assertIn("lifetime", document)
            self.assertIn("ready", document)
            self.assertIn("LaunchDaemon", document)
            self.assertIn("Apple Silicon", document)
            self.assertIn("Intel", document)
        self.assertIn("macOS 14+", readme)
        self.assertIn("退出登录", readme)
        self.assertIn("下次登录", deployment)
        self.assertIn("不解析", deployment)
        self.assertIn("launchctl print", deployment)
        self.assertIn("service.lifetime.lock", deployment)
        self.assertIn("gtimeout", deployment)
        self.assertIn("brew install coreutils", deployment)

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
        self.assertIn("Secret 和完整授权的升级仍天然非交互", deployment)
        self.assertIn("im:message.p2p_msg:readonly", deployment)
        self.assertIn("canonical `im:chat:read`", deployment)
        self.assertIn("`im:chat`、`im:chat:read`、`im:chat:readonly` 三者任一", deployment)
        self.assertIn("在切换 release 前退出", deployment)
        self.assertIn("飞书应用绑定重置", deployment)
        self.assertIn("feishu-app-secret", deployment)
        self.assertIn("不会迁移到新应用", deployment)
        self.assertIn("应用重绑定和 release 激活采用两阶段语义", deployment)
        self.assertIn("两种失败都不自动恢复旧应用凭据", deployment)
        self.assertIn("成对恢复", deployment)

    def test_routine_published_upgrade_uses_installer_exit_as_success_signal(
        self,
    ) -> None:
        deployment = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

        self.assertIn("官方 installer 返回 0", deployment)
        self.assertIn("常规 Published Release 升级", readme)
        self.assertIn("zero exit from the downloaded official installer", agents)
        self.assertIn("安装前 active", deployment)
        self.assertIn("主动停止的服务", deployment)
        self.assertIn("无需再次检查数据库完整性", deployment)
        self.assertIn("installer 返回非零", deployment)
        self.assertIn("Service-manager `active` alone", agents)

    def test_deployment_separates_published_and_source_installation(self) -> None:
        deployment = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        for document in (deployment, readme):
            self.assertIn("Published Release", document)
            self.assertIn("dev-install.sh", document)
            self.assertIn("Python 3.11-3.14", document)
        self.assertIn("releases/latest/download/install.sh", deployment)
        self.assertIn("install-release <source-root>", deployment)
        self.assertIn("不重复运行", deployment)
        self.assertIn("同一个", deployment)
        self.assertIn("rollback", deployment)
        self.assertIn("该 tag 的 exact main commit", readme)
        self.assertIn("发布流水线复用该 exact SHA", readme)
        self.assertNotIn("发布前已对这份", readme)
        self.assertIn("required reviewer 或 wait timer", deployment)
        self.assertIn(
            "维护者的发布指令与脚本的 exact-tag 创建、workflow", deployment
        )
        self.assertIn("dispatch 共同构成发布意图边界（ADR 0043、ADR 0050）", deployment)
        self.assertIn("不因 main push 或 tag push 自动发布", deployment)
        self.assertNotIn("environment 要求仓库所有者审批", deployment)

    def test_existing_app_permission_repair_does_not_require_tty(self) -> None:
        deployment = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
        design = (ROOT / "docs" / "design.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        decision = (
            ROOT
            / "docs"
            / "adr"
            / "0044-decouple-existing-app-repair-from-terminal-input.md"
        ).read_text(encoding="utf-8")

        self.assertIn("无论是否有 TTY", deployment)
        self.assertIn("不读取 stdin", deployment)
        self.assertIn("约 660 秒", deployment)
        self.assertIn("不依赖 TTY", design)
        self.assertIn("权限修复是唯一例外", readme)
        self.assertIn("exact-App permission repair is the sole browser exception", agents)
        self.assertIn("does not require a PTY or writable", agents)
        self.assertIn("amends: 0033, 0035", decision)
        self.assertIn("凭据不完整的首次安装继续", decision)
        self.assertIn("不调用 `scope.apply`", decision)

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

    def test_live_suite_includes_native_thread_lifecycle_probe(self) -> None:
        deployment = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
        probe = (ROOT / "scripts" / "probe_python_sdk.py").read_text(
            encoding="utf-8"
        )

        self.assertRegex(
            deployment,
            r"for phase in [^\n]*\blifecycle\b",
        )
        self.assertIn("thread_list(archived=False|True)", deployment)
        self.assertIn("`thread/delete`", deployment)
        self.assertIn("scan/state-db", deployment)
        lifecycle = probe.split("async def _thread_lifecycle_live", 1)[1].split(
            "async def _steer", 1
        )[0]
        self.assertIn("AppServerThreadDeleteControl", lifecycle)
        self.assertIn(".delete(", lifecycle)

    def test_live_suite_includes_non_consuming_turn_plan_probes(self) -> None:
        deployment = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
        probe = (ROOT / "scripts" / "probe_python_sdk.py").read_text(
            encoding="utf-8"
        )

        self.assertRegex(deployment, r"for phase in [^\n]*\bplan\b")
        self.assertIn("async def _turn_plan_live", probe)
        self.assertIn("PinnedTurnActivityObserver", probe)
        self.assertIn("async for notification in handle.stream()", probe)

    def test_installer_and_deployment_gate_root_task_diff_observation(self) -> None:
        deployment = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
        installer = (ROOT / "scripts" / "netizen_installer.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("probe_sdk_task_diff.py", installer)
        self.assertIn("probe_sdk_task_diff.py", deployment)

    def test_live_suite_includes_ephemeral_multi_turn_side_probe(self) -> None:
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

    def test_live_suite_uses_two_app_servers_and_exact_same_id_resume(self) -> None:
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

    def test_current_schema_documents_context_boundary_and_side_tombstones(
        self,
    ) -> None:
        deployment = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
        design = (ROOT / "docs" / "design.md").read_text(encoding="utf-8")

        self.assertIn("当前 schema v7", deployment)
        self.assertIn("Mention Context Mode", deployment)
        self.assertIn("Context Boundary", design)
        self.assertIn("不保存任何补充消息正文", deployment)
        self.assertIn("`side_topics`", design)
        self.assertIn("不保存\nephemeral native Thread ID", design)

    def test_make_check_covers_safe_local_code_gates(self) -> None:
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

        self.assertIn("check: test", makefile)
        self.assertIn("unittest discover -s tests -v", makefile)
        self.assertIn("compileall -q netizen scripts tests", makefile)
        self.assertIn("pip check", makefile)
        self.assertIn("probe_sdk_task_diff.py --timeout 5", makefile)
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
        self.assertIn("managed installers do not copy this file", config)
        self.assertIn("`projects: {}`", config)
        self.assertNotIn("defaultCwd:", config)
        self.assertIn("projectRoot:", config)
        self.assertIn("test: /home/your-user/projects/test", config)
        self.assertIn("adminWeb:", config)
        self.assertIn("host: 0.0.0.0", config)
        self.assertIn("port: 8787", config)
        self.assertIn("Managed Linux and macOS", config)
        self.assertIn("FEISHU_APP_SECRET_FILE", config)
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
