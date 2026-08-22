from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from openai_codex import CodexConfig

from netizen.bindings import BindingStore, SideTopicState
from netizen.domain import FeishuScope, ScopeKind
from netizen.main import ServiceCore, _scrub_channel_environment, build_channel
from netizen.main import _register_channel_handlers
from netizen.projects import ProjectRegistry
from netizen.settings import AdminWebSettings, Settings


def settings(root: Path) -> Settings:
    default = root / "default"
    project = root / "project"
    default.mkdir(exist_ok=True)
    project.mkdir(exist_ok=True)
    return Settings(
        app_id="cli_test",
        app_secret="secret",
        data_dir=root / "data",
        default_cwd=default,
        project_root=root,
        projects={"test": project},
        security_mode="audit",
        admin_web=AdminWebSettings(enabled=False),
    )


class MainConfigurationTest(unittest.TestCase):
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

    def test_channel_environment_scrub_is_hygiene_not_custom_codex_env(self) -> None:
        with patch.dict(
            os.environ,
            {
                "FEISHU_APP_SECRET": "secret",
                "FEISHU_APP_SECRET_FILE": "/secret-file",
                "NETIZEN_ADMIN_SECRET": "admin-secret",
                "NETIZEN_ADMIN_SECRET_FILE": "/admin-secret-file",
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
            self.assertEqual(os.environ["CODEX_HOME"], "/home/user/.codex")
            self.assertEqual(os.environ["HOME"], "/home/user")


class ServiceCoreTest(unittest.IsolatedAsyncioTestCase):
    async def test_admin_listener_binds_before_codex_and_opens_explicitly(self) -> None:
        events: list[str] = []

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
                default_cwd=configured.default_cwd,
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
                    default_cwd=configured.default_cwd,
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
                self.assertLess(events.index("codex:enter"), events.index("admin:attach"))
                self.assertNotIn("admin:open", events)
                core.open_admission()
                self.assertEqual(events[-1], "admin:open")
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
                default_cwd=configured.default_cwd,
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
                    default_cwd=configured.default_cwd,
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
        core._runtime = FakeRuntime()  # type: ignore[assignment]
        core._management = FakeManagement()  # type: ignore[assignment]
        core.application = FakeApplication()  # type: ignore[assignment]
        core._codex = FakeCodex()  # type: ignore[assignment]

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
        self.assertEqual(tuple(events), expected)

    async def test_one_asynccodex_uses_the_captured_service_environment(self) -> None:
        constructed: list[tuple[tuple[object, ...], dict[str, object]]] = []
        cleanup_codex: list[object] = []
        boundary_codex: list[object] = []
        subscription_codex: list[object] = []
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
                    default_cwd=configured.default_cwd,
                    projects=configured.projects,
                    project_root=configured.project_root,
                ),
            )
            try:
                with (
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
                ):
                    await core.start()
                    assert core._runtime is not None
                    self.assertIsNone(core._runtime._thread_delete_control)
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
            ("allow_login_shell=false",),
        )
        self.assertIsNone(config.codex_bin)
        self.assertIsNone(config.env)
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
                    default_cwd=configured.default_cwd,
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
