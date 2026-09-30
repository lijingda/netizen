from __future__ import annotations

import asyncio
import fcntl
import os
import select
import stat
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from lark_channel import DedupStore
from openai_codex import CodexConfig

from netizen_cli.admin.port_config import ConfigFileSnapshot
from netizen_cli.bindings import BindingStore, SideTopicState
from netizen_cli.cli_data import (
    StartupRejected,
    acquire_lifetime_lock,
    ensure_instance_root,
    initialize_instance_data,
    instance_lifetime_lock,
)
from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.lark_app import encode_lark_app
from netizen_cli.main import (
    ServiceCore,
    _adopt_lifetime_lock,
    _clear_ready_marker,
    _cleanup_step,
    _configure_platform_trust,
    _open_core_admission,
    _publish_ready_marker,
    _register_channel_handlers,
    _scrub_channel_environment,
    _start_channel_input,
    build_channel,
    main,
)
from netizen_cli.projects import ProjectRegistry
from netizen_cli.settings import AdminWebSettings, Settings


def settings(root: Path) -> Settings:
    project = root / "project"
    project.mkdir(exist_ok=True)
    return Settings(
        app_id="cli_test",
        app_secret="secret",
        data_dir=root / "data",
        project_root=root,
        projects={"test": project},
        security_mode="audit",
        admin_web=AdminWebSettings(enabled=False),
    )


class MainConfigurationTest(unittest.TestCase):
    def test_platform_trust_uses_keychain_on_macos(self) -> None:
        calls: list[str] = []
        fake_truststore = SimpleNamespace(
            inject_into_ssl=lambda: calls.append("inject")
        )

        with (
            patch("netizen_cli.main.sys.platform", "darwin"),
            patch.dict(sys.modules, {"truststore": fake_truststore}),
        ):
            _configure_platform_trust()
        self.assertEqual(calls, ["inject"])

    def test_platform_trust_does_not_import_truststore_on_linux(self) -> None:
        with (
            patch("netizen_cli.main.sys.platform", "linux"),
            patch.dict(sys.modules, {"truststore": None}),
        ):
            _configure_platform_trust()

    def test_main_configures_platform_trust_before_starting_runtime(self) -> None:
        events: list[str] = []
        runtime = object()

        with (
            patch.dict(os.environ, {"NETIZEN_ROOT": "/tmp/instance"}, clear=True),
            patch("netizen_cli.main._adopt_lifetime_lock", return_value=None),
            patch("netizen_cli.main.acquire_lifetime_lock", return_value=12345),
            patch("netizen_cli.main.prepare_instance", side_effect=lambda *_args, **_kwargs: events.append("data")),
            patch(
                "netizen_cli.main._configure_platform_trust",
                side_effect=lambda: events.append("trust"),
            ),
            patch("netizen_cli.main._configure_logging"),
            patch("netizen_cli.main.Settings.from_file", return_value=SimpleNamespace(
                data_dir=Path("/tmp/instance/state").resolve(),
                admin_web=AdminWebSettings(enabled=False),
            )),
            patch("netizen_cli.main._scrub_channel_environment"),
            patch(
                "netizen_cli.main.run",
                new=lambda *_args, **_kwargs: runtime,
            ),
            patch(
                "netizen_cli.main.asyncio.run",
                side_effect=lambda candidate: events.append(
                    "runtime" if candidate is runtime else "unexpected"
                ),
            ),
        ):
            main()

        self.assertEqual(events, ["trust", "data", "runtime"])

    def test_message_and_card_action_handlers_are_registered(self) -> None:
        registered: dict[str, object] = {}

        class FakeChannel:
            def on(self, event: str, handler: object) -> None:
                registered[event] = handler

        application = SimpleNamespace(
            handle_message=object(),
            handle_card_action=object(),
        )
        _register_channel_handlers(
            FakeChannel(),  # type: ignore[arg-type]
            application,  # type: ignore[arg-type]
        )

        self.assertIs(registered["message"], application.handle_message)
        self.assertIs(
            registered["cardAction"],
            application.handle_card_action,
        )
        self.assertIn("error", registered)
        self.assertNotIn("meetingInvited", registered)

    def test_channel_preserves_steer_delivery_policy_and_persistent_dedup(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = BindingStore()
            try:
                channel = build_channel(settings(root), store)
            finally:
                store.close()

        self.assertEqual(channel.config.safety.text_batch.delay_ms, 0)
        self.assertEqual(channel.config.safety.text_batch.long_delay_ms, 0)
        self.assertFalse(channel.config.safety.chat_queue.enabled)
        self.assertFalse(channel.config.safety.chat_queue.merge_while_busy)
        self.assertTrue(channel.config.policy.require_mention)
        self.assertEqual(channel.config.policy.dm_policy, "open")
        self.assertEqual(channel.config.policy.group_policy, "open")
        self.assertTrue(channel.config.inbound.include_raw)
        self.assertTrue(channel.config.resolve_sender_names)
        self.assertIsNone(channel.config.policy.allow_from)
        self.assertIsNone(channel.config.policy.group_allowlist)
        self.assertEqual(channel.config.security.mode, "audit")
        self.assertIs(channel._deduper._store, store)
        self.assertIsInstance(store, DedupStore)

    def test_channel_environment_scrub_is_hygiene_not_custom_codex_env(self) -> None:
        with patch.dict(
            os.environ,
            {
                "FEISHU_APP_SECRET": "secret",
                "FEISHU_APP_SECRET_FILE": "/secret-file",
                "NETIZEN_LARK_APP_CONFIG": "/managed/lark-app/config.json",
                "NETIZEN_ADMIN_SECRET": "admin-secret",
                "NETIZEN_ADMIN_SECRET_FILE": "/admin-secret-file",
                "NETIZEN_CONFIG_PATH": "/managed/config.yaml",
                "NETIZEN_LOG_FILE": "/managed/netizen.log",
                "NETIZEN_MANAGED_LAUNCH_AGENT": "sentinel",
                "NETIZEN_READY_FILE": "/managed/service.ready",
                "NETIZEN_ROOT": "/managed",
                "CODEX_HOME": "/home/user/.codex",
                "HOME": "/home/user",
            },
            clear=True,
        ):
            _scrub_channel_environment()

            self.assertNotIn("FEISHU_APP_SECRET", os.environ)
            self.assertNotIn("FEISHU_APP_SECRET_FILE", os.environ)
            self.assertNotIn("NETIZEN_LARK_APP_CONFIG", os.environ)
            self.assertNotIn("NETIZEN_ADMIN_SECRET", os.environ)
            self.assertNotIn("NETIZEN_ADMIN_SECRET_FILE", os.environ)
            self.assertNotIn("NETIZEN_CONFIG_PATH", os.environ)
            self.assertNotIn("NETIZEN_LOG_FILE", os.environ)
            self.assertNotIn("NETIZEN_MANAGED_LAUNCH_AGENT", os.environ)
            self.assertNotIn("NETIZEN_READY_FILE", os.environ)
            self.assertEqual(os.environ["CODEX_HOME"], "/home/user/.codex")
            self.assertEqual(os.environ["HOME"], "/home/user")
            self.assertEqual(os.environ["NETIZEN_ROOT"], "/managed")

    def test_main_uses_canonical_root_config_and_preserves_shared_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            alias = root / "alias"
            target = root / "instance"
            target.mkdir()
            alias.symlink_to(target, target_is_directory=True)
            configured = replace(settings(root), data_dir=target / "state")
            environment = {
                "NETIZEN_ROOT": str(alias),
                "CODEX_HOME": str(root / "shared-codex"),
            }
            runtime = object()
            configuration_environments: list[dict[str, str]] = []

            def load_settings(_path: Path) -> Settings:
                configuration_environments.append(dict(os.environ))
                return configured

            with (
                patch.dict(os.environ, environment, clear=True),
                patch("netizen_cli.main._configure_platform_trust"),
                patch("netizen_cli.main._configure_logging"),
                patch("netizen_cli.main.acquire_lifetime_lock", return_value=12345),
                patch("netizen_cli.main.prepare_instance"),
                patch("netizen_cli.main.Settings.from_file", side_effect=load_settings) as load,
                patch("netizen_cli.main.run", new=Mock(return_value=runtime)) as run,
                patch("netizen_cli.main.asyncio.run") as execute,
            ):
                main()
                load.assert_called_once_with(target / "config.yaml")
                run.assert_called_once_with(configured, instance_root=target, ready_file=None)
                execute.assert_called_once_with(runtime)
                self.assertEqual(os.environ["NETIZEN_ROOT"], str(target))
                self.assertEqual(os.environ["CODEX_HOME"], environment["CODEX_HOME"])
                self.assertEqual(
                    configuration_environments[0]["NETIZEN_LARK_APP_CONFIG"],
                    str(target / "lark-app" / "config.json"),
                )
                self.assertEqual(
                    configuration_environments[0]["NETIZEN_ADMIN_SECRET_FILE"],
                    str(target / "credentials" / "admin-web-secret"),
                )
                self.assertNotIn("NETIZEN_LARK_APP_CONFIG", os.environ)
                self.assertNotIn("NETIZEN_ADMIN_SECRET_FILE", os.environ)

    def test_main_without_root_loads_default_instance_and_exports_its_context(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw).resolve()
            root = home / ".netizen"
            ensure_instance_root(root)
            profile = root / "lark-app" / "config.json"
            profile.write_bytes(encode_lark_app("cli_default", "fake-secret"))
            profile.chmod(0o600)
            config = root / "config.yaml"
            config.write_text(
                f"instance:\n  dataDir: {root / 'state'}\n  projectRoot: {home / 'projects'}\n"
                "adminWeb:\n  enabled: false\n", encoding="utf-8",
            )
            config.chmod(0o600)
            with instance_lifetime_lock(root) as descriptor:
                initialize_instance_data(root, lifetime_descriptor=descriptor)
            with (
                patch.dict(os.environ, {"HOME": str(home / "unrelated")}, clear=True),
                patch("netizen_cli.instance.pwd.getpwuid", return_value=SimpleNamespace(pw_dir=str(home))),
                patch("netizen_cli.main._configure_platform_trust"),
                patch("netizen_cli.main._configure_logging"),
                patch("netizen_cli.main.run", new=Mock()) as run,
                patch("netizen_cli.main.asyncio.run"),
            ):
                main()
                configured = run.call_args.args[0]
                self.assertEqual(configured.config_path, config)
                self.assertEqual(configured.app_id, "cli_default")
                self.assertEqual(configured.data_dir, root / "state")
                self.assertEqual(run.call_args.kwargs["instance_root"], root)
                self.assertEqual(os.environ["NETIZEN_ROOT"], str(root))
                self.assertNotIn("NETIZEN_LARK_APP_CONFIG", os.environ)
                self.assertNotIn("NETIZEN_ADMIN_SECRET_FILE", os.environ)

    def test_main_rejects_cross_instance_paths_before_logging_or_runtime(self) -> None:
        names = (
            "NETIZEN_CONFIG_PATH", "NETIZEN_LARK_APP_CONFIG",
            "NETIZEN_ADMIN_SECRET_FILE", "NETIZEN_LOG_FILE",
            "NETIZEN_READY_FILE", "NETIZEN_LIFETIME_LOCK_FILE",
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            cases = ((name, explicit) for name in names for explicit in (False, True))
            for name, explicit_root in cases:
                with (
                    self.subTest(name=name, explicit_root=explicit_root),
                    patch.dict(os.environ, {
                        **({"NETIZEN_ROOT": str(root / "a")} if explicit_root else {}),
                        name: str(root / "b" / "wrong-path"),
                    }, clear=True),
                    patch("netizen_cli.instance.pwd.getpwuid", return_value=SimpleNamespace(pw_dir=str(root))),
                    patch("netizen_cli.main._adopt_lifetime_lock", return_value=None),
                    patch("netizen_cli.main.Settings.from_file") as load,
                    patch("netizen_cli.main._configure_logging") as logging,
                    patch("netizen_cli.main.run", new=Mock()) as run,
                ):
                    with self.assertRaisesRegex(RuntimeError, name):
                        main()
                    load.assert_not_called()
                    logging.assert_not_called()
                    run.assert_not_called()

    def test_managed_and_manual_start_retain_lock_and_propagate_runtime_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            ensure_instance_root(root)
            config = root / "config.yaml"
            config.write_text("instance: {}\n")
            config.chmod(0o600)
            with instance_lifetime_lock(root) as descriptor:
                initialize_instance_data(root, lifetime_descriptor=descriptor)
            configured = replace(settings(root), data_dir=root / "state")
            def fail_runtime(*_args, **_kwargs):
                with self.assertRaises(BlockingIOError):
                    acquire_lifetime_lock(root)
                raise RuntimeError("runtime failed")
            for managed in (False, True):
                with (
                    self.subTest(managed=managed),
                    patch.dict(os.environ, {"NETIZEN_ROOT": str(root), "NETIZEN_CLI_SERVICE": "1" if managed else "0"}, clear=True),
                    patch("netizen_cli.main.Settings.from_file", return_value=configured),
                    patch("netizen_cli.main._configure_logging"),
                    patch("netizen_cli.main.run", side_effect=fail_runtime),
                ):
                    with self.assertRaisesRegex(RuntimeError, "runtime failed"):
                        main()
            # Exit, including failure, releases ownership; no orphan descriptor.
            with instance_lifetime_lock(root):
                pass

    def test_actual_start_with_lost_database_fails_before_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            ensure_instance_root(root)
            config = root / "config.yaml"
            config.write_text("instance: {}\n")
            config.chmod(0o600)
            with instance_lifetime_lock(root) as descriptor:
                initialize_instance_data(root, lifetime_descriptor=descriptor)
            database = root / "state" / "channel.sqlite3"
            database.unlink()
            with (
                patch.dict(os.environ, {"NETIZEN_ROOT": str(root)}, clear=True),
                patch("netizen_cli.main.Settings.from_file", return_value=replace(settings(root), data_dir=root / "state")),
                patch("netizen_cli.main._configure_logging") as logging,
                patch("netizen_cli.main.run") as run,
            ):
                with self.assertRaisesRegex(StartupRejected, "database is missing"):
                    main()
            self.assertFalse(database.exists())
            logging.assert_not_called()
            run.assert_not_called()

    def test_managed_invalid_config_or_data_exits_cleanly_without_ready_or_retry_loop(self) -> None:
        for kind in ("config", "missing", "future"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve()
                ensure_instance_root(root)
                config = root / "config.yaml"
                config.write_text("instance: {}\n")
                config.chmod(0o600)
                with instance_lifetime_lock(root) as descriptor:
                    initialize_instance_data(root, lifetime_descriptor=descriptor)
                database = root / "state" / "channel.sqlite3"
                if kind == "missing":
                    database.unlink()
                elif kind == "future":
                    connection = sqlite3.connect(database)
                    connection.execute("UPDATE schema_version SET version=999")
                    connection.commit()
                    connection.close()
                descriptor = acquire_lifetime_lock(root)
                ready = root / "state" / "service.ready"
                with (
                    patch.dict(os.environ, {
                        "NETIZEN_ROOT": str(root), "NETIZEN_CLI_SERVICE": "1",
                        "NETIZEN_READY_FILE": str(ready),
                        "NETIZEN_LIFETIME_LOCK_FD": str(descriptor),
                        "NETIZEN_LIFETIME_LOCK_FILE": str(root / "state" / "service.lifetime.lock"),
                    }, clear=True),
                    patch("netizen_cli.main.Settings.from_file", return_value=replace(settings(root), data_dir=root / "state"),
                          side_effect=ValueError("invalid config") if kind == "config" else None),
                    patch("netizen_cli.main.run") as run,
                    self.assertLogs("netizen_cli.main", level="ERROR") as logs,
                ):
                    self.assertIsNone(main())
                run.assert_not_called()
                self.assertFalse(ready.exists())
                self.assertIn("startup rejected", logs.output[0])
                with instance_lifetime_lock(root):
                    pass

    def test_managed_transient_failures_still_exit_unsuccessfully(self) -> None:
        for error in (OSError("temporary network failure"), RuntimeError("unexpected runtime failure")):
            with (
                self.subTest(error=type(error).__name__),
                patch.dict(os.environ, {"NETIZEN_CLI_SERVICE": "1"}, clear=True),
                patch("netizen_cli.main._run_service", side_effect=error),
            ):
                with self.assertRaises(type(error)) as failure:
                    main()
                self.assertIs(failure.exception, error)

    def test_main_rejects_cross_instance_configured_state_before_log_or_database(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            configured = settings(root)
            for label, candidate in (
                ("instance.dataDir", configured),
                ("Admin credential path", replace(
                    configured, data_dir=root / "state",
                    admin_web=AdminWebSettings(credential_path=root / "other-secret"),
                )),
            ):
                with (
                    self.subTest(label=label),
                    patch.dict(os.environ, {"NETIZEN_ROOT": str(root)}, clear=True),
                    patch("netizen_cli.main._configure_platform_trust"),
                    patch("netizen_cli.main.Settings.from_file", return_value=candidate),
                    patch("netizen_cli.main._configure_logging") as logging,
                    patch("netizen_cli.main.run", new=Mock()) as run,
                ):
                    with self.assertRaisesRegex(RuntimeError, label):
                        main()
                    logging.assert_not_called()
                    run.assert_not_called()

    def test_managed_main_requires_root_and_closes_adopted_descriptor(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("netizen_cli.main._adopt_lifetime_lock", return_value=12345),
            patch("netizen_cli.main.os.close") as close,
            patch("netizen_cli.main._configure_logging") as logging,
        ):
            with self.assertRaisesRegex(RuntimeError, "missing NETIZEN_ROOT"):
                main()
            close.assert_called_once_with(12345)
            logging.assert_not_called()

    def test_live_tool_subprocess_does_not_retain_adopted_lifetime_lock(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "service.lifetime.lock"
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.set_inheritable(descriptor, True)
            lock_probe = [
                sys.executable,
                "-c",
                "import fcntl,os,sys; fd=os.open(sys.argv[1],os.O_RDWR); "
                "\ntry: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)"
                "\nexcept BlockingIOError: raise SystemExit(1)"
                "\nos.close(fd)",
                str(path),
            ]
            try:
                with patch.dict(
                    os.environ,
                    {
                        "NETIZEN_LIFETIME_LOCK_FD": str(descriptor),
                        "NETIZEN_LIFETIME_LOCK_FILE": str(path),
                    },
                    clear=True,
                ):
                    adopted = _adopt_lifetime_lock()
                    self.assertEqual(adopted, descriptor)
                    self.assertFalse(os.get_inheritable(descriptor))
                    self.assertNotIn("NETIZEN_LIFETIME_LOCK_FD", os.environ)
                    self.assertNotIn("NETIZEN_LIFETIME_LOCK_FILE", os.environ)

                    with subprocess.Popen(
                        [
                            sys.executable,
                            "-c",
                            (
                                "import os,select,sys; fd=int(sys.argv[1]); "
                                "\ntry: os.fstat(fd)"
                                "\nexcept OSError: pass"
                                "\nelse: raise SystemExit(1)"
                                "\nprint('no-lock-fd',flush=True)"
                                "\nready,_,_=select.select([sys.stdin],[],[],15)"
                                "\nraise SystemExit(0 if ready and sys.stdin.readline() == 'done\\n' else 2)"
                            ),
                            str(descriptor),
                        ],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        text=True,
                        close_fds=False,
                    ) as child:
                        assert child.stdin is not None and child.stdout is not None
                        try:
                            ready, _, _ = select.select([child.stdout], [], [], 10)
                            self.assertTrue(ready, "child did not report its lock descriptors")
                            self.assertEqual(child.stdout.readline().strip(), "no-lock-fd")
                            self.assertIsNone(child.poll())
                            held = subprocess.run(lock_probe, check=False, timeout=5)
                            self.assertEqual(held.returncode, 1)

                            # Close only: LOCK_UN could hide an inherited open-file description.
                            os.close(descriptor)
                            descriptor = -1
                            released = subprocess.run(lock_probe, check=False, timeout=5)
                            self.assertEqual(released.returncode, 0)
                            self.assertIsNone(child.poll(), "child exited before the lock check")
                            child.stdin.write("done\n")
                            child.stdin.flush()
                            self.assertEqual(child.wait(timeout=5), 0)
                        finally:
                            if child.poll() is None:
                                child.kill()
                            child.wait(timeout=5)
            finally:
                if descriptor >= 0:
                    os.close(descriptor)

    def test_ready_marker_is_atomic_private_and_removable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "service.ready"
            path.write_text("stale", encoding="utf-8")

            _publish_ready_marker(path)

            self.assertEqual(path.read_bytes(), b"netizen service ready\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*")), [])
            _clear_ready_marker(path)
            self.assertFalse(path.exists())


class FakeScheduleMcpRunner:
    config_overrides = (
        'mcp_servers.netizen_scheduler_test={url="http://127.0.0.1:32123/mcp", '
        'bearer_token_env_var="NETIZEN_SCHEDULE_MCP_TOKEN_TEST", enabled=true}',
    )
    app_server_env = {"NETIZEN_SCHEDULE_MCP_TOKEN_TEST": "temporary-test-token"}

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.bind_error: BaseException | None = None
        self.callback = None
        events.append("mcp:init")

    async def bind(self) -> None:
        self.events.append("mcp:bind")
        if self.bind_error is not None:
            raise self.bind_error

    def attach(self, callback: object) -> None:
        self.callback = callback
        self.events.append("mcp:attach")

    def open_admission(self) -> None:
        if self.callback is None:
            raise AssertionError("MCP admission opened without management")
        self.events.append("mcp:open")

    def close_admission(self) -> None:
        self.events.append("mcp:admission")

    async def drain(self, deadline: float) -> None:
        self.deadline = deadline
        self.events.append("mcp:drain")

    async def close(self) -> None:
        self.events.append("mcp:close")


class FakeScheduler:
    def __init__(self, events: list[str], **options: object) -> None:
        self.events = events
        self.options = options
        self.recovery_error: BaseException | None = None
        events.append("scheduler:init")

    async def recover(self) -> None:
        self.events.append("scheduler:recover")
        if self.recovery_error is not None:
            raise self.recovery_error

    def start(self) -> None:
        self.events.append("scheduler:start")

    def wake(self) -> None:
        self.events.append("scheduler:wake")

    async def refresh(self, plan_id: str) -> None:
        self.events.append("scheduler:refresh")

    def run_now(self, plan_id, expected_revision, request_id, request_payload):
        self.events.append("scheduler:run-now")

    def close_admission(self) -> None:
        self.events.append("scheduler:admission")

    async def close(self) -> None:
        self.events.append("scheduler:close")

    async def drain(self, deadline: float) -> None:
        self.deadline = deadline
        self.events.append("scheduler:drain")

    async def drain_project_creation(self, alias: str, deadline: float) -> bool:
        return True


class ServiceCoreTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.schedule_events: list[str] = []
        self.mcp_runners: list[FakeScheduleMcpRunner] = []
        self.schedulers: list[FakeScheduler] = []
        self.mcp_bind_error: BaseException | None = None
        self.schedule_recovery_error: BaseException | None = None
        self.skill_roots_error: BaseException | None = None
        self.skill_root_calls: list[tuple[object, tuple[Path, ...]]] = []
        self.builtin_root = Path("/physical-release/source/skills")

        def make_skill_roots(codex: object) -> object:
            async def set_roots(roots: tuple[Path, ...]) -> None:
                self.schedule_events.append("skills:set")
                self.skill_root_calls.append((codex, roots))
                if self.skill_roots_error is not None:
                    raise self.skill_roots_error
            return SimpleNamespace(set_roots=set_roots)

        def make_mcp() -> FakeScheduleMcpRunner:
            runner = FakeScheduleMcpRunner(self.schedule_events)
            runner.bind_error = self.mcp_bind_error
            self.mcp_runners.append(runner)
            return runner

        def make_scheduler(**kwargs: object) -> FakeScheduler:
            scheduler = FakeScheduler(self.schedule_events, **kwargs)
            scheduler.recovery_error = self.schedule_recovery_error
            self.schedulers.append(scheduler)
            return scheduler

        self.enterContext(patch("netizen_cli.main.ScheduleMcpRunner", make_mcp))
        self.enterContext(patch("netizen_cli.main.Scheduler", make_scheduler))
        self.enterContext(patch("netizen_cli.main.AppServerSkillRoots", make_skill_roots))
        self.enterContext(patch("netizen_cli.main.builtin_skill_root", return_value=self.builtin_root))

    def _make_core(self, root: Path) -> ServiceCore:
        configured = settings(root)
        store = BindingStore()
        self.addCleanup(store.close)
        return ServiceCore(
            settings=configured,
            instance_root=root,
            channel=SimpleNamespace(  # type: ignore[arg-type]
                safety=None, update_policy=lambda **_kwargs: None,
            ),
            store=store,
            projects=ProjectRegistry(
                store=store, projects=configured.projects,
                project_root=configured.project_root,
            ),
        )

    async def test_restart_recovery_proof_requires_admission_and_managed_ready_publication(self) -> None:
        for outcome in ("ready", "unmanaged", "admission_failed", "marker_failed"):
            with self.subTest(outcome=outcome):
                events = []

                def open_admission():
                    events.append("admission")
                    if outcome == "admission_failed":
                        raise RuntimeError("admission failed")

                def publish(_path):
                    events.append("marker")
                    if outcome == "marker_failed":
                        raise RuntimeError("marker failed")

                core = SimpleNamespace(
                    open_admission=open_admission,
                    _management=SimpleNamespace(set_service_ready=lambda ready: events.append(ready)),
                )
                with patch("netizen_cli.main._publish_ready_marker", side_effect=publish):
                    ready_file = None if outcome == "unmanaged" else Path("/unused/service.ready")
                    if outcome.endswith("failed"):
                        with self.assertRaises(RuntimeError):
                            await _open_core_admission(core, ready_file=ready_file)
                        self.assertNotIn(True, events)
                    else:
                        await _open_core_admission(core, ready_file=ready_file)
                        self.assertEqual(events, ["admission"] if outcome == "unmanaged" else ["admission", "marker", True])

    async def test_channel_input_registers_handlers_before_transport_without_release_rollback(self) -> None:
        events = []
        channel = SimpleNamespace(start_background=AsyncMock(side_effect=lambda: events.append("transport")))
        with patch("netizen_cli.main._register_channel_handlers", side_effect=lambda *_args: events.append("handlers")):
            await _start_channel_input(channel, object(), ready_file=Path("/unused/service.ready"))
        self.assertEqual(events, ["handlers", "transport"])

    async def test_schedule_mcp_bind_failure_stops_before_codex(self) -> None:
        self.mcp_bind_error = OSError("schedule listener failed")
        with tempfile.TemporaryDirectory() as raw:
            core = self._make_core(Path(raw))
            with patch("netizen_cli.main.AsyncCodex") as codex_constructor:
                with self.assertRaisesRegex(OSError, "schedule listener failed"):
                    await core.start()
                codex_constructor.assert_not_called()
            with self.assertRaisesRegex(RuntimeError, "not ready"):
                core.open_admission()

        self.assertEqual(len(self.mcp_runners), 1)
        self.assertEqual(self.schedulers, [])
        self.assertEqual(self.schedule_events.count("mcp:close"), 1)
        self.assertNotIn("mcp:open", self.schedule_events)

    async def test_recovery_failure_closes_schedule_components_and_native_transport(self) -> None:
        self.schedule_recovery_error = RuntimeError("recovery failed")
        events = self.schedule_events
        codex = SimpleNamespace(
            close=AsyncMock(side_effect=lambda: events.append("codex:close")),
        )
        codex.__aenter__ = AsyncMock(return_value=codex)
        cleanup = SimpleNamespace(
            clean_thread=AsyncMock(), has_running=AsyncMock(return_value=False),
            unsubscribe=AsyncMock(return_value="unsubscribed"),
        )

        class FakeApplication:
            def __init__(self, **_kwargs: object) -> None:
                events.append("application:init")

            async def dispatch_scheduled_run(self, claim: object) -> None:
                raise AssertionError("recovery cannot dispatch a new occurrence")

        with tempfile.TemporaryDirectory() as raw:
            core = self._make_core(Path(raw))
            with (
                patch("netizen_cli.main.AsyncCodex", return_value=codex) as constructor,
                patch("netizen_cli.main.PinnedExperimentalTerminalCleanup", return_value=cleanup),
                patch("netizen_cli.main.AppServerThreadSubscriptionControl", return_value=cleanup),
                patch("netizen_cli.main.ChannelApplication", FakeApplication),
            ):
                with self.assertRaisesRegex(RuntimeError, "recovery failed"):
                    await core.start()
                constructor.assert_called_once()
                codex.close.assert_awaited_once()
            with self.assertRaisesRegex(RuntimeError, "not ready"):
                core.open_admission()

        self.assertEqual(events.count("mcp:close"), 1)
        self.assertEqual(events.count("scheduler:close"), 1)
        self.assertEqual(events.count("scheduler:drain"), 1)
        self.assertLess(events.index("scheduler:admission"), events.index("scheduler:close"))
        self.assertLess(events.index("scheduler:drain"), events.index("codex:close"))
        self.assertLess(events.index("mcp:close"), events.index("codex:close"))
        self.assertNotIn("mcp:open", events)
        self.assertNotIn("scheduler:start", events)

    async def test_admin_listener_binds_before_codex_and_opens_explicitly(self) -> None:
        events: list[str] = []
        self.schedule_events = events
        admin_options: dict[str, object] = {}

        class FakeAdminRunner:
            def __init__(self, **kwargs: object) -> None:
                events.append("admin:init")
                admin_options.update(kwargs)
                self.urls = ()
                self.loopback_only = True

            async def bind(self) -> None:
                events.append("admin:bind")
                self.urls = ("http://admin.example.test:8788/",)

            def attach_management(self, management: object) -> None:
                self.management = management
                events.append("admin:attach")

            def open_admission(self) -> None:
                events.append("admin:open")

            def close_admission(self) -> None:
                events.append("admin:close-admission")

            async def close_listener(self) -> None:
                events.append("admin:close-listener")

            async def drain(self, _deadline: float) -> None:
                events.append("admin:drain")

            def close_auth(self) -> None:
                events.append("admin:close-auth")

        class FakeAsyncCodex:
            def __init__(self, _config: CodexConfig) -> None:
                events.append("codex:init")

            async def __aenter__(self):
                events.append("codex:enter")
                return self

            async def close(self) -> None:
                events.append("codex:close")

        class FakeCleanup:
            def __init__(self, _codex: object) -> None:
                return None

            async def clean_thread(self, _thread_id: str) -> None:
                return None

            async def has_running(self, _thread_id: str) -> bool:
                return False

            async def unsubscribe(self, _thread_id: str) -> str:
                return "unsubscribed"

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            instance_root = root / "explicit-instance"
            config_path = root / "development-config.yaml"
            snapshot = ConfigFileSnapshot(config_path, b"adminWeb: {}", ())
            configured = settings(root)
            configured = Settings(
                app_id=configured.app_id,
                app_secret=configured.app_secret,
                data_dir=configured.data_dir,
                project_root=configured.project_root,
                projects=configured.projects,
                security_mode=configured.security_mode,
                admin_web=AdminWebSettings(
                    enabled=True,
                    host="0.0.0.0",
                    port=None,
                    access_host="admin.example.test",
                    credential_path=root / "admin-secret",
                ),
                config_path=config_path,
                config_snapshot=snapshot,
            )
            store = BindingStore()
            channel = SimpleNamespace(
                safety=None,
                update_policy=lambda **_kwargs: events.append("feishu:closed"),
            )
            core = ServiceCore(
                settings=configured,
                channel=channel,  # type: ignore[arg-type]
                store=store,
                projects=ProjectRegistry(
                    store=store,
                    projects=configured.projects,
                    project_root=configured.project_root,
                ),
                instance_root=instance_root,
            )
            with (
                patch("netizen_cli.main.AdminWebRunner", FakeAdminRunner),
                patch("netizen_cli.main.AsyncCodex", FakeAsyncCodex),
                patch("netizen_cli.main.PinnedExperimentalTerminalCleanup", FakeCleanup),
                patch("netizen_cli.main.AppServerThreadSubscriptionControl", FakeCleanup),
                patch("netizen_cli.main.AppServerSkillCatalog", side_effect=lambda _: events.append("catalog:init")),
            ):
                await core.start()
                self.assertLess(events.index("admin:bind"), events.index("codex:init"))
                self.assertLess(events.index("mcp:bind"), events.index("codex:init"))
                self.assertLess(events.index("codex:enter"), events.index("admin:attach"))
                self.assertLess(events.index("codex:enter"), events.index("skills:set"))
                self.assertLess(events.index("skills:set"), events.index("admin:attach"))
                self.assertLess(events.index("skills:set"), events.index("catalog:init"))
                self.assertEqual(self.skill_root_calls, [(core._codex, (self.builtin_root,))])
                self.assertEqual(admin_options, {
                    "host": "0.0.0.0", "port": None,
                    "credential_path": root / "admin-secret",
                    "instance_root": instance_root,
                    "config_path": config_path, "config_snapshot": snapshot,
                    "access_host": "admin.example.test",
                })
                self.assertEqual(core.application._admin_urls, ("http://admin.example.test:8788/",))
                self.assertEqual(core.application._instance_root, instance_root)
                self.assertTrue(core.application._admin_loopback_only)
                self.assertEqual(core._management._updates.product_root, instance_root)
                self.assertNotIn("admin:open", events)
                self.assertNotIn("mcp:open", events)
                self.assertNotIn("scheduler:start", events)
                self.assertIn("scheduler:recover", events)
                self.assertIs(self.schedulers[0].options["bindings"], store)
                self.assertIs(self.schedulers[0].options["runtime"], core._runtime)
                self.assertEqual(self.schedulers[0].options["app_id"], configured.app_id)
                self.assertEqual(
                    self.schedulers[0].options["dispatch"],
                    core.application.dispatch_scheduled_run,
                )
                with patch.object(
                    core._management.schedules, "manage",
                    new=AsyncMock(return_value={"ok": True}),
                ) as manage:
                    result = await self.mcp_runners[0].callback(
                        {"mode": "list"}, "exact-calling-thread",
                    )
                    self.assertEqual(result, {"ok": True})
                    manage.assert_awaited_once_with(
                        {"mode": "list"}, native_thread_id="exact-calling-thread",
                    )
                core.open_admission()
                self.assertIn("admin:open", events)
                self.assertIn("mcp:open", events)
                self.assertIn("scheduler:start", events)
                self.assertLess(events.index("scheduler:recover"), events.index("mcp:open"))
                self.assertLess(events.index("scheduler:recover"), events.index("scheduler:start"))
                await core.close()

        self.assertIn("admin:close-listener", events)
        self.assertIn("admin:drain", events)
        self.assertIn("codex:close", events)

    async def test_builtin_skill_registration_failure_closes_startup_before_runtime(self) -> None:
        for phase in ("adapter", "registration"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as raw:
                core = self._make_core(Path(raw))
                core._settings = replace(
                    core._settings,
                    admin_web=AdminWebSettings(credential_path=Path(raw) / "admin-secret"),
                )
                codex = SimpleNamespace(__aenter__=AsyncMock(), close=AsyncMock())
                admin = SimpleNamespace(
                    bind=AsyncMock(), close_listener=AsyncMock(), drain=AsyncMock(),
                    close_auth=Mock(), urls=(), loopback_only=False,
                )
                error = RuntimeError("builtin Skills unavailable")
                roots = SimpleNamespace(set_roots=AsyncMock(side_effect=error))
                with (
                    patch("netizen_cli.main.AdminWebRunner", return_value=admin),
                    patch("netizen_cli.main.AsyncCodex", return_value=codex),
                    patch("netizen_cli.main.AppServerSkillRoots", return_value=roots,
                          side_effect=error if phase == "adapter" else None),
                    patch("netizen_cli.main.AppServerSkillCatalog") as catalog,
                    patch("netizen_cli.main.CodexRuntime") as runtime,
                    patch("netizen_cli.main._publish_ready_marker") as ready,
                ):
                    with self.assertRaisesRegex(RuntimeError, "builtin Skills unavailable"):
                        await core.start()
                    with self.assertRaisesRegex(RuntimeError, "not ready"):
                        core.open_admission()
                    admin.bind.assert_awaited_once()
                    codex.__aenter__.assert_awaited_once()
                    codex.close.assert_awaited_once()
                    admin.close_listener.assert_awaited_once()
                    admin.drain.assert_awaited_once()
                    admin.close_auth.assert_called_once()
                    catalog.assert_not_called()
                    runtime.assert_not_called()
                    ready.assert_not_called()
                    self.assertIsNone(core.application)
                    self.assertIsNone(core._scheduler)

    async def test_core_uses_explicit_context_not_config_or_data_parent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            core = self._make_core(root)
            self.assertEqual(core._instance_root, root)
            configured = replace(core._settings, config_path=root / "config-root" / "config.yaml")
            configured_core = ServiceCore(
                settings=configured, channel=core._channel,
                store=core._store, projects=core._projects,
                instance_root=root,
            )
            self.assertEqual(configured_core._instance_root, root)

    async def test_admin_bind_failure_cleans_partial_state_without_starting_codex(
        self,
    ) -> None:
        events: list[str] = []

        class FailingAdminRunner:
            def __init__(self, **_kwargs: object) -> None:
                events.append("admin:init")

            async def bind(self) -> None:
                events.append("admin:bind")
                raise OSError("occupied")

            async def close_listener(self) -> None:
                events.append("admin:close-listener")

            async def drain(self, _deadline: float) -> None:
                events.append("admin:drain")

            def close_auth(self) -> None:
                events.append("admin:close-auth")

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            configured = settings(root)
            configured = Settings(
                app_id=configured.app_id,
                app_secret=configured.app_secret,
                data_dir=configured.data_dir,
                project_root=configured.project_root,
                projects=configured.projects,
                security_mode=configured.security_mode,
                admin_web=AdminWebSettings(
                    credential_path=root / "admin-secret"
                ),
            )
            store = BindingStore()
            core = ServiceCore(
                settings=configured,
                instance_root=root,
                channel=SimpleNamespace(safety=None),  # type: ignore[arg-type]
                store=store,
                projects=ProjectRegistry(
                    store=store,
                    projects=configured.projects,
                    project_root=configured.project_root,
                ),
            )
            try:
                with (
                    patch("netizen_cli.main.AdminWebRunner", FailingAdminRunner),
                    patch(
                        "netizen_cli.main.AsyncCodex",
                        side_effect=AssertionError("Codex must not start"),
                    ),
                ):
                    with self.assertRaisesRegex(OSError, "occupied"):
                        await core.start()
            finally:
                store.close()

        self.assertEqual(
            events,
            [
                "admin:init",
                "admin:bind",
                "admin:close-listener",
                "admin:drain",
                "admin:close-auth",
            ],
        )

    async def test_shutdown_closes_ingress_then_drains_and_cleans_in_order(self) -> None:
        from netizen_cli.main import _SHUTDOWN_BUDGET_SECONDS

        events: list[str] = []

        class FakeAdmin:
            def close_admission(self) -> None:
                events.append("admin:admission")

            async def close_listener(self) -> None:
                events.append("admin:listener")

            async def drain(self, deadline: float) -> None:
                self.deadline = deadline
                events.append("admin:drain")

            def close_auth(self) -> None:
                events.append("admin:auth")

        class FakeRuntime:
            def close_admission(self) -> None:
                events.append("runtime:admission")

            async def interrupt_all(self) -> None:
                events.append("runtime:interrupt")

            async def wait_idle(self, timeout: float | None = None) -> bool:
                self.timeout = timeout
                events.append("runtime:idle")
                return True

            async def cancel_tasks(self) -> None:
                events.append("runtime:tasks")

        class FakeManagement:
            def set_service_ready(self, ready: bool) -> None:
                events.append(f"management:ready={ready}")

            async def close(self, *, deadline: float | None = None) -> None:
                self.deadline = deadline
                events.append("management:close")

        class FakeApplication:
            async def close(self) -> None:
                events.append("application:close")

        class FakeCodex:
            async def close(self) -> None:
                events.append("codex:close")

        class FakeStore:
            async def aclose(self) -> None:
                events.append("store:close")

        class FakeSafety:
            async def dispose(self) -> None:
                events.append("feishu:drain")

        channel = SimpleNamespace(
            safety=FakeSafety(),
            update_policy=lambda **_kwargs: events.append("feishu:admission"),
        )
        core = ServiceCore(
            settings=SimpleNamespace(),  # type: ignore[arg-type]
            channel=channel,  # type: ignore[arg-type]
            store=FakeStore(),  # type: ignore[arg-type]
            projects=SimpleNamespace(),  # type: ignore[arg-type]
            instance_root=Path("/unused/instance"),
        )
        core._admin = FakeAdmin()  # type: ignore[assignment]
        core._schedule_mcp = FakeScheduleMcpRunner(events)  # type: ignore[assignment]
        core._scheduler = FakeScheduler(events)  # type: ignore[assignment]
        core._runtime = FakeRuntime()  # type: ignore[assignment]
        core._management = FakeManagement()  # type: ignore[assignment]
        core.application = FakeApplication()  # type: ignore[assignment]
        core._codex = FakeCodex()  # type: ignore[assignment]
        events.clear()

        await core.close()
        await core.close()

        self.assertEqual(_SHUTDOWN_BUDGET_SECONDS, 60.0)
        expected = (
            "management:ready=False",
            "admin:admission",
            "feishu:admission",
            "runtime:admission",
            "admin:listener",
            "feishu:drain",
            "admin:drain",
            "management:close",
            "runtime:interrupt",
            "runtime:idle",
            "application:close",
            "codex:close",
            "runtime:tasks",
            "admin:auth",
            "store:close",
        )
        self.assertEqual(
            tuple(event for event in events if not event.startswith(("mcp:", "scheduler:"))),
            expected,
        )
        for first, second in (
            ("mcp:admission", "mcp:drain"),
            ("scheduler:admission", "scheduler:close"),
            ("scheduler:close", "scheduler:drain"),
            ("mcp:drain", "runtime:interrupt"),
            ("mcp:close", "store:close"),
            ("scheduler:drain", "runtime:interrupt"),
        ):
            self.assertLess(events.index(first), events.index(second))
        for event in ("mcp:close", "scheduler:close", "scheduler:drain"):
            self.assertEqual(events.count(event), 1)
        self.assertEqual(core._schedule_mcp.deadline, core._scheduler.deadline)
        self.assertEqual(core._scheduler.deadline, core._management.deadline)

    async def test_shutdown_native_failure_still_closes_sdk_and_local_resources(self) -> None:
        for failure in ("cleanup_error", "turns_not_idle"):
            with self.subTest(failure=failure):
                runtime = SimpleNamespace(
                    close_admission=Mock(),
                    interrupt_all=AsyncMock(),
                    wait_idle=AsyncMock(return_value=False),
                    cancel_tasks=AsyncMock(),
                )
                if failure == "cleanup_error":
                    runtime.interrupt_all.side_effect = ExceptionGroup(
                        "native stop failed", [RuntimeError("cleanup failed")]
                    )
                store = SimpleNamespace(aclose=AsyncMock())
                codex = SimpleNamespace(close=AsyncMock())
                core = ServiceCore(
                    settings=SimpleNamespace(),  # type: ignore[arg-type]
                    channel=SimpleNamespace(  # type: ignore[arg-type]
                        update_policy=lambda **_kwargs: None,
                    ),
                    store=store,  # type: ignore[arg-type]
                    projects=SimpleNamespace(),  # type: ignore[arg-type]
                    instance_root=Path("/unused/instance"),
                )
                core._runtime = runtime  # type: ignore[assignment]
                core._codex = codex  # type: ignore[assignment]

                with self.assertLogs("netizen_cli.main", level="WARNING"):
                    await asyncio.wait_for(core.close(), timeout=1)
                await core.close()

                runtime.close_admission.assert_called_once()
                runtime.interrupt_all.assert_awaited_once()
                if failure == "cleanup_error":
                    runtime.wait_idle.assert_not_awaited()
                else:
                    runtime.wait_idle.assert_awaited_once()
                codex.close.assert_awaited_once()
                runtime.cancel_tasks.assert_awaited_once()
                store.aclose.assert_awaited_once()

    async def test_shutdown_retries_unfinished_management_close_with_same_deadline(self) -> None:
        for failure in ("timeout", "cancel", "error"):
            with self.subTest(failure=failure):
                started = asyncio.Event()
                deadlines: list[float | None] = []
                completed = False

                async def close_management(*, deadline: float | None = None) -> None:
                    nonlocal completed
                    deadlines.append(deadline)
                    if len(deadlines) == 1:
                        started.set()
                        if failure == "error":
                            raise OSError("management close failed")
                        await asyncio.Future()
                    completed = True

                async def bounded_step(label, operation, *, timeout):
                    if label == "management I/O drain" and failure == "timeout":
                        timeout = min(timeout, 0.01)
                    return await _cleanup_step(label, operation, timeout=timeout)

                store = SimpleNamespace(aclose=AsyncMock())
                core = ServiceCore(
                    settings=SimpleNamespace(),  # type: ignore[arg-type]
                    channel=SimpleNamespace(update_policy=lambda **_kwargs: None),  # type: ignore[arg-type]
                    store=store,  # type: ignore[arg-type]
                    projects=SimpleNamespace(),  # type: ignore[arg-type]
                    instance_root=Path("/unused/instance"),
                )
                core._management = SimpleNamespace(close=close_management, set_service_ready=lambda _ready: None)  # type: ignore[assignment]
                core.application = SimpleNamespace(close=AsyncMock())  # type: ignore[assignment]
                core._codex = SimpleNamespace(close=AsyncMock())  # type: ignore[assignment]
                core._runtime = SimpleNamespace(  # type: ignore[assignment]
                    close_admission=lambda: None,
                    interrupt_all=AsyncMock(),
                    wait_idle=AsyncMock(return_value=True),
                    cancel_tasks=AsyncMock(),
                )
                if failure == "error":
                    core.application.close.side_effect = RuntimeError("presentation close failed")

                with patch("netizen_cli.main._cleanup_step", bounded_step):
                    if failure == "cancel":
                        closing = asyncio.create_task(core.close())
                        await asyncio.wait_for(started.wait(), timeout=1)
                        closing.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await asyncio.wait_for(closing, timeout=1)
                    else:
                        with self.assertLogs("netizen_cli.main", level="WARNING"):
                            await asyncio.wait_for(core.close(), timeout=1)

                self.assertTrue(completed)
                self.assertEqual(len(deadlines), 2)
                self.assertIsNotNone(deadlines[0])
                self.assertEqual(deadlines[0], deadlines[1])
                core.application.close.assert_awaited_once()
                core._codex.close.assert_awaited_once()
                core._runtime.cancel_tasks.assert_awaited_once()
                store.aclose.assert_awaited_once()

    async def test_shutdown_does_not_retry_management_after_total_budget_expires(self) -> None:
        async def pending_close(*, deadline: float | None = None) -> None:
            await asyncio.Future()

        management = SimpleNamespace(close=AsyncMock(side_effect=pending_close), set_service_ready=lambda _ready: None)
        core = ServiceCore(
            settings=SimpleNamespace(),  # type: ignore[arg-type]
            channel=SimpleNamespace(update_policy=lambda **_kwargs: None),  # type: ignore[arg-type]
            store=SimpleNamespace(aclose=AsyncMock()),  # type: ignore[arg-type]
            projects=SimpleNamespace(),  # type: ignore[arg-type]
            instance_root=Path("/unused/instance"),
        )
        core._management = management  # type: ignore[assignment]
        with (
            patch("netizen_cli.main._SHUTDOWN_BUDGET_SECONDS", 0.01),
            self.assertLogs("netizen_cli.main", level="WARNING") as logs,
        ):
            await asyncio.wait_for(core.close(), timeout=1)

        management.close.assert_awaited_once()
        self.assertTrue(any(
            "management I/O final cleanup skipped because the shutdown budget was exhausted"
            in message
            for message in logs.output
        ))

    async def test_one_asynccodex_uses_the_captured_service_environment(self) -> None:
        constructed: list[tuple[tuple[object, ...], dict[str, object]]] = []
        cleanup_codex: list[object] = []
        boundary_codex: list[object] = []
        subscription_codex: list[object] = []
        delete_codex: list[object] = []
        side_card_updates: list[tuple[str, object]] = []
        closed = False

        class FakeAsyncCodex:
            def __init__(self, *args: object, **kwargs: object) -> None:
                constructed.append((args, kwargs))

            async def __aenter__(self):
                return self

            async def close(self) -> None:
                nonlocal closed
                closed = True

            async def thread_fork(self, _thread_id: str, **_kwargs: object):
                raise AssertionError("wiring test must not fork")

        class FakeCleanup:
            def __init__(self, codex: object) -> None:
                cleanup_codex.append(codex)

            async def clean_thread(self, _thread_id: str) -> None:
                return None

            async def has_running(self, _thread_id: str) -> bool:
                return False

        class FakeBoundaryControl:
            def __init__(self, codex: object) -> None:
                boundary_codex.append(codex)
                self.codex = codex

            async def inject_boundary(self, _thread_id: str) -> None:
                return None

        class FakeSubscriptionControl:
            def __init__(self, codex: object) -> None:
                subscription_codex.append(codex)
                self.codex = codex

            async def unsubscribe(self, _thread_id: str):
                return "unsubscribed"

        class FakeDeleteControl:
            def __init__(self, codex: object) -> None:
                delete_codex.append(codex)
                self.codex = codex

            async def delete(self, _thread_id: str) -> None:
                return None

        async def update_card(message_id: str, card: object) -> object:
            side_card_updates.append((message_id, card))
            return SimpleNamespace(success=True)

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            configured = settings(root)
            store = BindingStore()
            store.bootstrap_project(
                alias="test", cwd=str(configured.projects["test"].resolve()),
            )
            binding = store.create_channel_binding(
                scope=FeishuScope("cli_test", "oc_chat", ScopeKind.DIRECT),
                project_alias="test",
                creator_id="ou_owner",
            )
            side = store.create_side_topic(
                app_id="cli_test",
                chat_id="oc_chat",
                source_message_id="om_side",
                parent_binding_id=binding.id,
                creator_id="ou_owner",
                requires_mention=False,
            )
            store.set_side_topic_root(side.id, "om_root")
            store.open_side_topic(side.id, "omt_side")
            channel = SimpleNamespace(
                safety=None,
                update_card=update_card,
                update_policy=lambda **_kwargs: None,
            )
            core = ServiceCore(
                settings=configured,
                instance_root=root,
                channel=channel,  # type: ignore[arg-type]
                store=store,
                projects=ProjectRegistry(
                    store=store,
                    projects=configured.projects,
                    project_root=configured.project_root,
                ),
            )
            try:
                with (
                    patch.dict(
                        os.environ,
                        {
                            "PATH": "/inherited/tools:/usr/bin",
                            "CODEX_HOME": "/inherited/codex",
                            "NETIZEN_TEST_EXPORTED": "keep this exact value",
                        },
                    ),
                    patch("netizen_cli.main.AsyncCodex", FakeAsyncCodex),
                    patch(
                        "netizen_cli.main.PinnedExperimentalTerminalCleanup",
                        FakeCleanup,
                    ),
                    patch(
                        "netizen_cli.main.AppServerSideBoundaryControl",
                        FakeBoundaryControl,
                    ),
                    patch(
                        "netizen_cli.main.AppServerThreadSubscriptionControl",
                        FakeSubscriptionControl,
                    ),
                    patch(
                        "netizen_cli.main.AppServerThreadDeleteControl",
                        FakeDeleteControl,
                    ),
                ):
                    captured_environment = dict(os.environ)
                    await core.start()
                    self.assertEqual(os.environ, captured_environment)
                    assert core._runtime is not None
                    self.assertIs(
                        core._runtime._thread_delete_control.codex,
                        delete_codex[0],
                    )
                    self.assertIs(
                        core._runtime._side_boundary_control.codex,
                        boundary_codex[0],
                    )
                    self.assertIs(
                        core._runtime._thread_subscription_control.codex,
                        subscription_codex[0],
                    )
                    self.assertEqual(
                        store.get_side_topic(side.id).state,
                        SideTopicState.EXPIRED,
                    )
                    await core.close()
            finally:
                store.close()

        self.assertEqual(len(constructed), 1)
        args, kwargs = constructed[0]
        self.assertEqual(kwargs, {})
        self.assertEqual(len(args), 1)
        config = args[0]
        self.assertIsInstance(config, CodexConfig)
        self.assertEqual(
            config.config_overrides,
            ("allow_login_shell=false",) + FakeScheduleMcpRunner.config_overrides,
        )
        self.assertIsNone(config.codex_bin)
        self.assertEqual(
            config.env,
            captured_environment | FakeScheduleMcpRunner.app_server_env
            | {"NETIZEN_ROOT": str(root.resolve())},
        )
        self.assertEqual(len(cleanup_codex), 1)
        self.assertEqual(boundary_codex, [cleanup_codex[0]])
        self.assertEqual(subscription_codex, [cleanup_codex[0]])
        self.assertEqual(side_card_updates[0][0], "om_root")
        self.assertIn("expired", str(side_card_updates[0][1]))
        self.assertTrue(closed)

    async def test_close_always_closes_transport_and_tasks_after_cancellation(self) -> None:
        transport_closed = False
        tasks_cancelled = False

        class FakeAsyncCodex:
            def __init__(self, _config: CodexConfig) -> None:
                return None

            async def __aenter__(self):
                return self

            async def close(self) -> None:
                nonlocal transport_closed
                transport_closed = True

        class FakeCleanup:
            def __init__(self, _codex: object) -> None:
                return None

        class FakeRuntime:
            def __init__(self, **_kwargs: object) -> None:
                return None

            def set_completion_handler(self, _handler: object) -> None:
                return None

            def set_question_handler(self, _handler: object) -> None:
                return None

            def close_admission(self) -> None:
                return None

            async def interrupt_all(self) -> None:
                raise asyncio.CancelledError

            async def wait_idle(self, timeout: float | None = None) -> bool:
                raise AssertionError(f"wait_idle must not run after cancellation: {timeout}")

            async def cancel_tasks(self) -> None:
                nonlocal tasks_cancelled
                tasks_cancelled = True

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            configured = settings(root)
            store = BindingStore()
            core = ServiceCore(
                settings=configured,
                instance_root=root,
                channel=SimpleNamespace(  # type: ignore[arg-type]
                    safety=None,
                    update_policy=lambda **_kwargs: None,
                ),
                store=store,
                projects=ProjectRegistry(
                    store=store,
                    projects=configured.projects,
                    project_root=configured.project_root,
                ),
            )
            try:
                with (
                    patch("netizen_cli.main.AsyncCodex", FakeAsyncCodex),
                    patch("netizen_cli.main.CodexRuntime", FakeRuntime),
                    patch(
                        "netizen_cli.main.PinnedExperimentalTerminalCleanup",
                        FakeCleanup,
                    ),
                    patch(
                        "netizen_cli.main.AppServerThreadSubscriptionControl",
                        FakeCleanup,
                    ),
                ):
                    await core.start()
                    with self.assertRaises(asyncio.CancelledError):
                        await core.close()
            finally:
                store.close()

        self.assertTrue(transport_closed)
        self.assertTrue(tasks_cancelled)
        self.assertEqual(self.schedule_events.count("mcp:close"), 1)
        self.assertEqual(self.schedule_events.count("scheduler:close"), 1)
        self.assertEqual(self.schedule_events.count("scheduler:drain"), 1)
