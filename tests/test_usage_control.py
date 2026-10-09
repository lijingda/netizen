from __future__ import annotations

import asyncio
import json
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from openai_codex import AsyncCodex
from lark_channel import OutboundCard

from netizen_cli.account_rate_limits import (
    AccountRateLimitBucket,
    AccountRateLimitsSnapshot,
    AccountRateLimitsUnavailable,
    AccountRateLimitWindow,
)
from netizen_cli.bindings import BindingStore, SideTopicState
from netizen_cli.codex_runtime import CodexRuntime, RuntimeClosed
from netizen_cli.domain import ControlName, FeishuScope, NativeCapability, ScopeKind
from netizen_cli.experience import InvalidInteraction, command_help, parse_message, side_command_help
from netizen_cli.sdk_gap_adapter import facade_migration_requirements
from tests.support.channel_fixtures import channel_fixture, side_channel_fixture
from tests.support.channel_messages import FakeMessage


SNAPSHOT = AccountRateLimitsSnapshot((
    AccountRateLimitBucket(
        "codex", "Codex", AccountRateLimitWindow(25, 300, 0),
        AccountRateLimitWindow(105, 10080, None),
    ),
    AccountRateLimitBucket("other", "Other", None, AccountRateLimitWindow(0, None, None)),
))


class UsagePresentationTest(unittest.TestCase):
    def test_command_is_argument_free_and_capability_gated(self) -> None:
        def parse(text, capabilities=()):
            return parse_message(
                scope=FeishuScope("app", "chat", ScopeKind.DIRECT),
                message_id="message", sender_id="sender", text=text,
                available_capabilities=capabilities,
            )
        with self.assertRaises(InvalidInteraction):
            parse("/usage")
        capability = {NativeCapability.ACCOUNT_RATE_LIMITS}
        self.assertEqual(parse("/usage", capability).name, ControlName.USAGE)
        with self.assertRaises(InvalidInteraction):
            parse("/usage reset", capability)
        self.assertNotIn("/usage", command_help())
        self.assertIn("/usage", command_help(capability))
        self.assertNotIn("/usage", side_command_help(requires_mention=False))
        self.assertIn("/usage", side_command_help(
            requires_mention=False, available_capabilities=capability,
        ))

    def test_account_facade_is_in_repository_migration_gate(self) -> None:
        with patch.object(AsyncCodex, "account_rate_limits", object(), create=True):
            self.assertIn(
                "migration-required:account-rate-limits:AsyncCodex.account_rate_limits",
                facade_migration_requirements(),
            )


class UsageRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_no_binding_no_cache_or_native_execution(self) -> None:
        with closing(BindingStore()) as store:
            reader = SimpleNamespace(read=AsyncMock(return_value=SNAPSHOT))
            runtime = CodexRuntime(
                codex=object(), bindings=store, terminal_cleanup=object(),
                account_rate_limits=reader,
            )
            self.assertIn(NativeCapability.ACCOUNT_RATE_LIMITS, runtime.available_capabilities)
            changes = store._connection.total_changes
            self.assertIs(await runtime.account_rate_limits(), SNAPSHOT)
            self.assertIs(await runtime.account_rate_limits(), SNAPSHOT)
            self.assertEqual(reader.read.await_count, 2)
            self.assertEqual(store._connection.total_changes, changes)
            self.assertEqual(runtime._tasks, set())
            runtime._accepting = False
            with self.assertRaises(RuntimeClosed):
                await runtime.account_rate_limits()
            self.assertEqual(reader.read.await_count, 2)

    async def test_unavailable_and_failed_reads_do_not_close_admission(self) -> None:
        with closing(BindingStore()) as store:
            runtime = CodexRuntime(codex=object(), bindings=store, terminal_cleanup=object())
            self.assertNotIn(NativeCapability.ACCOUNT_RATE_LIMITS, runtime.available_capabilities)
            with self.assertRaises(AccountRateLimitsUnavailable):
                await runtime.account_rate_limits()
            reader = SimpleNamespace(read=AsyncMock(side_effect=TimeoutError))
            runtime._account_rate_limits = reader
            with self.assertRaises(TimeoutError):
                await runtime.account_rate_limits()
            self.assertTrue(runtime._accepting)
            reader.read.side_effect = None
            reader.read.return_value = SNAPSHOT
            self.assertIs(await runtime.account_rate_limits(), SNAPSHOT)


class UsageChannelTest(unittest.IsolatedAsyncioTestCase):
    async def test_read_without_current_binding_in_main_and_topic_scopes(self) -> None:
        async with channel_fixture() as fixture:
            fixture.runtime.available_capabilities = {NativeCapability.ACCOUNT_RATE_LIMITS}
            fixture.runtime.account_rate_limits = AsyncMock(return_value=SNAPSHOT)
            changes = fixture.store._connection.total_changes
            for index, (chat_type, topic) in enumerate((
                ("p2p", None), ("group", None), ("p2p", "omt-direct"), ("group", "omt-group"),
            )):
                message = FakeMessage(
                    "/usage", message_id=f"om-usage-{index}", chat_type=chat_type,
                    thread_id=topic, mentioned_bot=chat_type == "group",
                )
                await fixture.app.handle_message(message)
                self.assertIsInstance(fixture.channel.replies[-1][1], OutboundCard)
                self.assertIn("75%", json.dumps(fixture.channel.replies[-1][1].card, ensure_ascii=False))
                self.assertEqual(fixture.store.list_bindings(fixture.app._scope(message).key), [])
            self.assertEqual(fixture.runtime.account_rate_limits.await_count, 4)
            self.assertEqual(fixture.store._connection.total_changes, changes)
            self.assertEqual(fixture.runtime.submit_calls, [])
            self.assertEqual(fixture.channel.reactions, [])
            await fixture.app.handle_message(FakeMessage(
                "/usage", message_id="om-unmentioned", chat_type="group", mentioned_bot=False,
            ))
            self.assertEqual(fixture.runtime.account_rate_limits.await_count, 4)

    async def test_failure_is_not_quota_and_does_not_expose_backend_details(self) -> None:
        async with channel_fixture() as fixture:
            fixture.runtime.available_capabilities = {NativeCapability.ACCOUNT_RATE_LIMITS}
            fixture.runtime.account_rate_limits = AsyncMock(side_effect=RuntimeError(
                "private@example.com credit balance=50 token=secret",
            ))
            await fixture.app.handle_message(FakeMessage("/usage", message_id="om-failed"))
            text = fixture.channel.replies[-1][1]
            self.assertIn("账号额度暂不可用", text)
            for value in ("@example", "balance", "secret", "%"):
                self.assertNotIn(value, text)
            fixture.runtime.account_rate_limits.side_effect = asyncio.CancelledError
            with self.assertRaises(asyncio.CancelledError):
                await fixture.app.handle_message(FakeMessage("/usage", message_id="om-cancel"))

    async def test_valid_side_reads_same_account_but_closed_side_still_rejects(self) -> None:
        async with side_channel_fixture() as fixture:
            fixture.runtime.available_capabilities = {
                NativeCapability.SIDE, NativeCapability.ACCOUNT_RATE_LIMITS,
            }
            fixture.runtime.account_rate_limits = AsyncMock(return_value=SNAPSHOT)
            source = FakeMessage("/side", message_id="om-side", chat_id="oc-direct", mentioned_bot=False)
            fixture.binding_for(source)
            fixture.queue_promoted_topic(
                chat_id="oc-direct", root_id="om-root", seed_id="om-seed", topic_id="omt-side",
            )
            await fixture.app.handle_message(source)
            record = fixture.store.side_topic_for_source(app_id="cli_test", source_message_id=source.id)
            self.assertIsNotNone(record)
            message = FakeMessage("/usage", message_id="om-usage", chat_id="oc-direct", thread_id="omt-side", mentioned_bot=False)
            await fixture.app.handle_message(message)
            self.assertIsInstance(fixture.channel.replies[-1][1], OutboundCard)
            self.assertIn("75%", json.dumps(fixture.channel.replies[-1][1].card, ensure_ascii=False))
            fixture.runtime.account_rate_limits.assert_awaited_once_with()
            fixture.store.transition_side_topic(record.id, SideTopicState.CLOSED)
            await fixture.app.handle_message(message)
            self.assertIn("不会转成普通会话", fixture.channel.replies[-1][1])
            fixture.runtime.account_rate_limits.assert_awaited_once_with()
            self.assertEqual(fixture.runtime.submit_calls, [])
            self.assertEqual(fixture.runtime.submit_side_calls, [])
