import json
import unittest

from claude_agent_sdk import RateLimitEvent, RateLimitInfo
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
