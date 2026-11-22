import asyncio
import json
from unittest.mock import patch
import unittest

from claude_agent_sdk import RateLimitEvent, RateLimitInfo
from netizen_cli.admin.presentation import _runtime_binding_json
from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.runtime.claude_status import ClaudeObservations
from tests.support.channel_messages import FakeMessage
from tests.test_claude_channel import owner_channel


class ObservationTest(unittest.IsolatedAsyncioTestCase):
    async def test_channel_uses_only_attributed_samples_and_never_opens_a_reader(self):
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2
            created = await fx.create_binding(FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT))
            binding = created.binding.id
            async def status():
                await app.handle_message(FakeMessage("/status", message_id="status"))
                return json.dumps(fx.channel.replies[-1][1].card, ensure_ascii=False)
            cold = await status()
            for label in ("未观测", "取不到", "不适用"):
                self.assertIn(label, cold)
            self.assertEqual(clients.calls, [])
            await app.handle_message(FakeMessage("work", message_id="work"))
            client = clients.calls[-1]
            client.init(model="claude-test")
            client.stream.put_nowait(RateLimitEvent(RateLimitInfo("rejected", raw={"secret": "private"}), "wrong", "other"))
            client.result(user_message_uuid="other", total_cost_usd=999)
            client.stream.put_nowait(RateLimitEvent(RateLimitInfo("allowed", utilization=0.0), "rate", client.session))
            client.result(total_cost_usd=0, usage={"input_tokens": 0, "output_tokens": True, "ignored": "private"})
            self.assertTrue(await ordinary.wait_idle(3))
            before = list(clients.lookups)
            warm = await status()
            self.assertIn("claude-test", warm)
            self.assertIn("allowed", warm)
            self.assertNotIn("private", warm)
            values = ordinary.observations.read(binding, client.session).json()
            self.assertEqual(values["input_tokens"]["value"], 0)
            self.assertEqual(values["totalCostUsd"]["value"], 0)
            self.assertEqual(values["output_tokens"]["availability"], "取不到")
            self.assertIsNotNone(values["model"]["sampledAt"])
            self.assertEqual((len(clients.calls), client.consumers, clients.lookups), (1, 1, before))

    async def test_local_page_includes_off_page_current_and_polling_uses_same_sample(self):
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
            first = (await fx.create_binding(scope)).binding
            second = (await fx.create_binding(scope)).binding
            fx.store.activate(scope_key=scope.key, binding_id=first.id)
            deadline = asyncio.get_running_loop().time() + 5
            page = await app._management.query_scope_sessions(scope=scope, limit=1, deadline=deadline)
            self.assertEqual([row.record.binding.id for row in page.items], [second.id])
            self.assertEqual(page.current.record.binding.id, first.id)
            self.assertIsNotNone(page.next_cursor)
            with patch("netizen_cli.channel.session_cards.SESSIONS_PAGE_SIZE", 1):
                card = (await app._sessions_card(scope=scope)).card
            text = json.dumps(card, ensure_ascii=False)
            for label in ("当前会话", "本页会话", "未观测", "下一页", "登记时间"):
                self.assertIn(label, text)
            self.assertNotIn("归档", text)
            self.assertNotIn("删除", text)
            next_page = await app._management.query_scope_sessions(
                scope=scope, cursor=page.next_cursor, limit=1, deadline=deadline,
            )
            self.assertEqual(next_page.items, (next_page.current,))
            statuses = await app._management.binding_statuses_exact(binding_ids=(first.id,), deadline=deadline)
            self.assertEqual(_runtime_binding_json(statuses[0])["observations"], ordinary.observations.read(first.id, None).json())
            self.assertIsNone(await app._management.binding_metadata_exact(first.id, deadline=deadline))
            self.assertEqual((clients.calls, clients.lookups), ([], []))

    async def test_terminal_result_uses_original_request_and_safe_native_summary(self):
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2
            await fx.create_binding(FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT))
            cases = (
                ({"result": ""}, "Claude 已完成本次执行。", True),
                ({"reason": "api_error", "is_error": True, "api_error_status": 503,
                  "result": "unfiltered native error"}, "Claude 请求失败（HTTP 503）。", True),
                ({"subtype": "error_during_execution", "reason": "aborted_tools", "is_error": True},
                 "Claude 本次执行已停止。", False),
            )
            for index, (native, expected, mention) in enumerate(cases):
                origin = "result-" + str(index)
                await app.handle_message(FakeMessage("work", message_id=origin))
                client = clients.calls[-1]
                client.init()
                client.result(**native)
                self.assertTrue(await ordinary.wait_idle(2))
                message_id, reply = fx.channel.replies[-1]
                self.assertEqual(message_id, origin)
                self.assertEqual(getattr(reply, "markdown", reply), expected)
                self.assertEqual([item.open_id for item in getattr(reply, "mentions", ())],
                                 ["ou_user"] if mention else [])
                self.assertEqual(client.consumers, 1)

    def test_bound_and_execution_identity(self):
        samples = ClaudeObservations(capacity=1, clock=lambda: 1.5)
        samples.init("a", "x", "first", "old", "/project")
        samples.init("a", "y", "second", "new", "/project")
        samples.rate("a", "y", "first", RateLimitInfo("rejected"))
        self.assertFalse(samples.read("a", "x").fields)
        self.assertEqual(samples.read("a", "y").json()["limitStatus"]["availability"], "未观测")
        samples.init("b", "z", "third", {"private": "ignored"}, "/different")
        self.assertFalse(samples.read("a", "y").fields)
        self.assertEqual(samples.read("b", "z").json()["model"]["availability"], "取不到")
