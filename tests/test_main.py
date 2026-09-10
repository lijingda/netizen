from __future__ import annotations

import asyncio
import fcntl
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from lark_channel import DedupStore
from openai_codex import CodexConfig

from netizen.bindings import BindingStore, SideTopicState
from netizen.domain import FeishuScope, ScopeKind
from netizen.main import (
    ServiceCore,
    _adopt_lifetime_lock,
    _clear_ready_marker,
    _cleanup_step,
    _configure_platform_trust,
    _publish_ready_marker,
    _register_channel_handlers,
    _scrub_channel_environment,
    build_channel,
    main,
)
from netizen.projects import ProjectRegistry
from netizen.settings import AdminWebSettings, Settings


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
            patch("netizen.main.sys.platform", "darwin"),
            patch.dict(sys.modules, {"truststore": fake_truststore}),
        ):
            _configure_platform_trust()
        self.assertEqual(calls, ["inject"])

    def test_platform_trust_does_not_import_truststore_on_linux(self) -> None:
        with (
            patch("netizen.main.sys.platform", "linux"),
            patch.dict(sys.modules, {"truststore": None}),
        ):
            _configure_platform_trust()

    def test_main_configures_platform_trust_before_starting_runtime(self) -> None:
        events: list[str] = []
        runtime = object()

        with (
            patch.dict(os.environ, {"NETIZEN_CONFIG_PATH": "/tmp/config"}, clear=True),
            patch("netizen.main._adopt_lifetime_lock", return_value=None),
            patch(
                "netizen.main._configure_platform_trust",
                side_effect=lambda: events.append("trust"),
            ),
            patch("netizen.main._configure_logging"),
            patch("netizen.main.Settings.from_file", return_value=object()),
            patch("netizen.main._scrub_channel_environment"),
            patch(
                "netizen.main.run",
                new=lambda *_args, **_kwargs: runtime,
            ),
            patch(
                "netizen.main.asyncio.run",
                side_effect=lambda candidate: events.append(
                    "runtime" if candidate is runtime else "unexpected"
                ),
            ),
        ):
            main()

        self.assertEqual(events, ["trust", "runtime"])

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
                "NETIZEN_ADMIN_SECRET": "admin-secret",
                "NETIZEN_ADMIN_SECRET_FILE": "/admin-secret-file",
                "NETIZEN_CONFIG_PATH": "/managed/config.yaml",
                "NETIZEN_LOG_FILE": "/managed/netizen.log",
                "NETIZEN_MANAGED_LAUNCH_AGENT": "sentinel",
                "NETIZEN_READY_FILE": "/managed/service.ready",
                "CODEX_HOME": "/home/user/.codex",
                "HOME": "/home/user",
            },
            clear=True,
        ):
            _scrub_channel_environment()

            self.assertNotIn("FEISHU_APP_SECRET", os.environ)
            self.assertNotIn("FEISHU_APP_SECRET_FILE", os.environ)
            self.assertNotIn("NETIZEN_ADMIN_SECRET", os.environ)
            self.assertNotIn("NETIZEN_ADMIN_SECRET_FILE", os.environ)
            self.assertNotIn("NETIZEN_CONFIG_PATH", os.environ)
            self.assertNotIn("NETIZEN_LOG_FILE", os.environ)
            self.assertNotIn("NETIZEN_MANAGED_LAUNCH_AGENT", os.environ)
            self.assertNotIn("NETIZEN_READY_FILE", os.environ)
            self.assertEqual(os.environ["CODEX_HOME"], "/home/user/.codex")
            self.assertEqual(os.environ["HOME"], "/home/user")

    def test_adopted_lifetime_lock_is_cloexec_for_tool_subprocesses(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "service.lifetime.lock"
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.set_inheritable(descriptor, True)
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

                    probe = subprocess.run(
                        [
                            sys.executable,
                            "-c",
                            (
                                "import os,sys; fd=int(sys.argv[1]); "
                                "\ntry: os.fstat(fd)"
                                "\nexcept OSError: raise SystemExit(0)"
                                "\nraise SystemExit(1)"
                            ),
                            str(descriptor),
                        ],
                        check=False,
                        close_fds=False,
                    )
                self.assertEqual(probe.returncode, 0)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
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

        self.enterContext(patch("netizen.main.ScheduleMcpRunner", make_mcp))
        self.enterContext(patch("netizen.main.Scheduler", make_scheduler))

    def _make_core(self, root: Path) -> ServiceCore:
        configured = settings(root)
        store = BindingStore()
        self.addCleanup(store.close)
        return ServiceCore(
            settings=configured,
            channel=SimpleNamespace(  # type: ignore[arg-type]
                safety=None, update_policy=lambda **_kwargs: None,
            ),
            store=store,
            projects=ProjectRegistry(
                store=store, projects=configured.projects,
                project_root=configured.project_root,
            ),
        )

    async def test_schedule_mcp_bind_failure_stops_before_codex(self) -> None:
        self.mcp_bind_error = OSError("schedule listener failed")
        with tempfile.TemporaryDirectory() as raw:
            core = self._make_core(Path(raw))
            with patch("netizen.main.AsyncCodex") as codex_constructor:
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
                patch("netizen.main.AsyncCodex", return_value=codex) as constructor,
                patch("netizen.main.PinnedExperimentalTerminalCleanup", return_value=cleanup),
                patch("netizen.main.AppServerThreadSubscriptionControl", return_value=cleanup),
                patch("netizen.main.ChannelApplication", FakeApplication),
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

        class FakeAdminRunner:
            def __init__(self, **_kwargs: object) -> None:
                events.append("admin:init")

            async def bind(self) -> None:
                events.append("admin:bind")

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
                    enabled=True,
                    host="0.0.0.0",
                    port=8787,
                    credential_path=root / "admin-secret",
                ),
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
            )
            with (
                patch("netizen.main.AdminWebRunner", FakeAdminRunner),
                patch("netizen.main.AsyncCodex", FakeAsyncCodex),
                patch("netizen.main.PinnedExperimentalTerminalCleanup", FakeCleanup),
                patch("netizen.main.AppServerThreadSubscriptionControl", FakeCleanup),
            ):
                await core.start()
                self.assertLess(events.index("admin:bind"), events.index("codex:init"))
                self.assertLess(events.index("mcp:bind"), events.index("codex:init"))
                self.assertLess(events.index("codex:enter"), events.index("admin:attach"))
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
                    patch("netizen.main.AdminWebRunner", FailingAdminRunner),
                    patch(
                        "netizen.main.AsyncCodex",
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
        from netizen.main import _SHUTDOWN_BUDGET_SECONDS

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
                )
                core._management = SimpleNamespace(close=close_management)  # type: ignore[assignment]
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

                with patch("netizen.main._cleanup_step", bounded_step):
                    if failure == "cancel":
                        closing = asyncio.create_task(core.close())
                        await asyncio.wait_for(started.wait(), timeout=1)
                        closing.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await asyncio.wait_for(closing, timeout=1)
                    else:
                        with self.assertLogs("netizen.main", level="WARNING"):
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

        management = SimpleNamespace(close=AsyncMock(side_effect=pending_close))
        core = ServiceCore(
            settings=SimpleNamespace(),  # type: ignore[arg-type]
            channel=SimpleNamespace(update_policy=lambda **_kwargs: None),  # type: ignore[arg-type]
            store=SimpleNamespace(aclose=AsyncMock()),  # type: ignore[arg-type]
            projects=SimpleNamespace(),  # type: ignore[arg-type]
        )
        core._management = management  # type: ignore[assignment]
        with (
            patch("netizen.main._SHUTDOWN_BUDGET_SECONDS", 0.01),
            self.assertLogs("netizen.main", level="WARNING") as logs,
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
            binding = store.create_binding(
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
                    patch("netizen.main.AsyncCodex", FakeAsyncCodex),
                    patch(
                        "netizen.main.PinnedExperimentalTerminalCleanup",
                        FakeCleanup,
                    ),
                    patch(
                        "netizen.main.AppServerSideBoundaryControl",
                        FakeBoundaryControl,
                    ),
                    patch(
                        "netizen.main.AppServerThreadSubscriptionControl",
                        FakeSubscriptionControl,
                    ),
                    patch(
                        "netizen.main.AppServerThreadDeleteControl",
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
            captured_environment | FakeScheduleMcpRunner.app_server_env,
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
                    patch("netizen.main.AsyncCodex", FakeAsyncCodex),
                    patch("netizen.main.CodexRuntime", FakeRuntime),
                    patch(
                        "netizen.main.PinnedExperimentalTerminalCleanup",
                        FakeCleanup,
                    ),
                    patch(
                        "netizen.main.AppServerThreadSubscriptionControl",
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
