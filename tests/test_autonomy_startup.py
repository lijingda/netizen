from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from netizen.autonomy.config import parse_config, save_config
from netizen.autonomy.summary import CodexSummarizer
from netizen.bindings import BindingStore
from netizen.lark_app import encode_lark_app
from netizen.main import ServiceCore, build_channel
from netizen.projects import ProjectRegistry
from netizen.settings import AdminWebSettings, Settings
from tests.support.channel_messages import FakeChannel, FakeMessage


class AutonomySettingsTests(unittest.TestCase):
    def test_file_settings_derive_private_profile_without_requiring_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            app = root / "lark-app" / "config.json"
            app.parent.mkdir()
            app.write_bytes(encode_lark_app("cli_test", "test-secret"))
            app.chmod(0o600)
            config = root / "config.yaml"
            config.write_text(
                f"instance:\n  dataDir: {root / 'data'}\n  projectRoot: {root}\n"
                "adminWeb:\n  enabled: false\n",
                encoding="utf-8",
            )
            settings = Settings.from_file(config, {})
            expected = root / "credentials" / "decision-model.json"
            self.assertEqual(settings.decision_model_config_path, expected)
            self.assertFalse(expected.exists())
            self.assertNotIn(str(expected), repr(settings))

    def test_channel_admission_defers_to_application_only_when_experiment_is_assembled(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            settings = Settings("cli_test", "secret", root)
            store = BindingStore()
            try:
                self.assertIsNone(settings.decision_model_config_path)
                legacy = build_channel(settings, store)
                self.assertTrue(legacy.config.policy.require_mention)
                assembled = build_channel(replace(
                    settings, decision_model_config_path=root / "credentials" / "decision-model.json",
                ), store)
                self.assertFalse(assembled.config.policy.require_mention)
                self.assertFalse(assembled.config.policy.respond_to_mention_all)
                self.assertFalse(assembled.config.safety.chat_queue.enabled)
                self.assertIs(assembled._deduper._store, store)
            finally:
                store.close()


class AutonomyStartupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.temporary)
        self.events: list[str] = []
        self.store = BindingStore()
        self.addCleanup(self.store.close)
        self.settings = Settings(
            "cli_test", "secret", self.root,
            projects={"test": self.root}, admin_web=AdminWebSettings(enabled=False),
            decision_model_config_path=self.root / "credentials" / "decision-model.json",
        )
        self.channel = FakeChannel()
        self.channel.safety = None
        self.channel.update_policy = Mock()
        self.codex = SimpleNamespace(
            __aenter__=AsyncMock(),
            close=AsyncMock(side_effect=lambda: self.events.append("codex:close")),
            thread_start=AsyncMock(), thread_resume=AsyncMock(), thread_fork=AsyncMock(),
        )
        self.codex.__aenter__.return_value = self.codex
        self.cleanup = SimpleNamespace(clean_thread=AsyncMock(), has_running=AsyncMock(return_value=False))
        self.subscriptions = SimpleNamespace(unsubscribe=AsyncMock(return_value="unsubscribed"))
        self.mcp = SimpleNamespace(
            config_overrides=(), app_server_env={}, bind=AsyncMock(), attach=Mock(),
            open_admission=Mock(), close_admission=Mock(), close=AsyncMock(), drain=AsyncMock(),
        )
        self.scheduler = SimpleNamespace(
            recover=AsyncMock(), start=Mock(), wake=Mock(), refresh=AsyncMock(),
            run_now=Mock(), drain_project_creation=AsyncMock(), close_admission=Mock(),
            close=AsyncMock(), drain=AsyncMock(),
        )
        self.constructor = self.enterContext(patch("netizen.main.AsyncCodex", return_value=self.codex))
        self.enterContext(patch("netizen.main.PinnedExperimentalTerminalCleanup", return_value=self.cleanup))
        self.enterContext(patch("netizen.main.AppServerThreadSubscriptionControl", return_value=self.subscriptions))
        self.enterContext(patch("netizen.main.ScheduleMcpRunner", return_value=self.mcp))
        self.enterContext(patch("netizen.main.Scheduler", return_value=self.scheduler))
        for name in (
            "AppServerSkillCatalog", "AppServerGoalControl", "AppServerSideBoundaryControl",
            "AppServerThreadDeleteControl", "PinnedTurnActivityObserver",
        ):
            self.enterContext(patch("netizen.main." + name, return_value=None))
        self.decision = self.enterContext(patch(
            "netizen.autonomy.provider.SystemOneProvider.decide", new_callable=AsyncMock,
        ))
        self.core: ServiceCore | None = None

    def make_core(self, settings: Settings | None = None) -> ServiceCore:
        self.core = ServiceCore(
            settings=settings or self.settings, channel=self.channel, store=self.store,
            projects=ProjectRegistry(store=self.store, project_root=self.root, projects={"test": self.root}),
        )
        return self.core

    async def asyncTearDown(self) -> None:
        if self.core is not None and self.core._started:
            await self.core.close()

    async def test_unconfigured_optional_feature_starts_without_external_connections(self) -> None:
        core = self.make_core()
        await core.start()
        self.constructor.assert_called_once()
        self.assertIs(core._autonomy.store, self.store.autonomy)
        self.assertIs(core._autonomy.summarizer, core._autonomy_summarizer)
        self.assertIs(core._autonomy_summarizer._codex, self.codex)
        self.assertIs(core._autonomy_summarizer._terminal_cleanup, self.cleanup)
        self.assertIs(core._autonomy_summarizer._subscription_control, self.subscriptions)
        self.assertIs(core.application._autonomy, core._autonomy)
        status = await core._management.autonomy_status()
        self.assertTrue(status["supported"])
        self.assertFalse(status["configured"])
        self.decision.assert_not_awaited()
        self.codex.thread_start.assert_not_awaited()
        self.codex.thread_resume.assert_not_awaited()

    async def test_configured_offline_model_does_not_affect_startup(self) -> None:
        config = parse_config({"provider": "laya", "base_url": "http://127.0.0.1:9999"})
        save_config(self.settings.decision_model_config_path, 1, config)
        self.decision.side_effect = ConnectionError("not running")
        core = self.make_core()
        await core.start()
        status = await core._management.autonomy_status()
        self.assertTrue(status["configured"])
        self.assertEqual(status["config"]["base_url"], "http://127.0.0.1:9999")
        self.decision.assert_not_awaited()
        self.codex.thread_start.assert_not_awaited()

    async def test_bad_optional_profile_reports_unconfigured_without_blocking_startup(self) -> None:
        path = self.settings.decision_model_config_path
        path.parent.mkdir()
        path.write_text("not-json", encoding="utf-8")
        path.chmod(0o600)
        core = self.make_core()
        await core.start()
        status = await core._management.autonomy_status()
        self.assertFalse(status["configured"])
        self.assertIsNotNone(status["error"])
        self.decision.assert_not_awaited()

    async def test_unassembled_instance_does_not_create_experimental_service(self) -> None:
        core = self.make_core(replace(self.settings, decision_model_config_path=None))
        with patch("netizen.main.CodexSummarizer") as summarizer:
            await core.start()
        summarizer.assert_not_called()
        self.assertIsNone(core._autonomy)
        self.assertFalse((await core._management.autonomy_status())["supported"])

    async def test_application_keeps_unmentioned_ordinary_group_messages_silent(self) -> None:
        core = self.make_core()
        await core.start()
        message = FakeMessage("ordinary group conversation", message_id="om_skip", chat_type="group", mentioned_bot=False)
        await core.application.handle_message(message)
        self.assertEqual(self.channel.replies, [])
        self.assertEqual(self.channel.reactions, [])
        self.decision.assert_not_awaited()
        self.codex.thread_start.assert_not_awaited()

    async def test_shutdown_closes_optional_admission_and_summary_before_shared_transport(self) -> None:
        core = self.make_core()
        await core.start()
        original = core._autonomy_summarizer.aclose

        async def close_summary() -> None:
            self.assertFalse(core._autonomy.configured)
            self.events.append("summary:close")
            await original()

        with patch.object(core._autonomy_summarizer, "aclose", side_effect=close_summary):
            await core.close()
        self.assertEqual(self.events, ["summary:close", "codex:close"])
        self.assertTrue(core._autonomy_summarizer._closed)

    async def test_summary_cleanup_error_does_not_skip_transport_close(self) -> None:
        core = self.make_core()
        await core.start()
        with (
            patch.object(core._autonomy_summarizer, "aclose", side_effect=RuntimeError("cleanup failure")),
            self.assertLogs("netizen.main", level="ERROR"),
        ):
            await core.close()
        self.codex.close.assert_awaited_once()
        self.assertTrue(core._autonomy_summarizer._closed)
        self.assertFalse(core._autonomy.configured)

    async def test_partial_start_cleans_summary_before_transport_and_keeps_admission_closed(self) -> None:
        self.scheduler.recover.side_effect = RuntimeError("recovery failed")
        core = self.make_core()
        original = CodexSummarizer.aclose

        async def close_summary(summarizer: CodexSummarizer) -> None:
            self.events.append("summary:close")
            await original(summarizer)

        with patch.object(CodexSummarizer, "aclose", autospec=True, side_effect=close_summary):
            with self.assertRaisesRegex(RuntimeError, "recovery failed"):
                await core.start()
        self.assertFalse(core._started)
        self.assertEqual(self.events, ["summary:close", "codex:close"])
        self.assertTrue(core._autonomy_summarizer._closed)
        self.assertFalse(core._autonomy.configured)
        self.decision.assert_not_awaited()
        self.codex.thread_start.assert_not_awaited()
