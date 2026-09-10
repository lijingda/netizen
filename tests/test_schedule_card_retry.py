from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from lark_channel.channel.channel import _card_action_identity
from lark_channel.channel.safety.pipeline import SafetyPipeline

from netizen.cards.scheduled import decode_schedule_action, schedule_form_card, schedule_manager_card
from netizen.domain import FeishuScope, ScopeKind

import test_scheduled_channel as fixtures
from test_schedule_cards import callback, form_values


class ScheduleCardRetryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.ScheduledChannelTest()
        await self.fixture.asyncSetUp()
        self.gate = SafetyPipeline(loop=asyncio.get_running_loop(), on_message=lambda _: None)
        self.scope = FeishuScope("app", "oc_group", ScopeKind.TOPIC, "omt_retry")
        self.fixture.channel.fetched_messages["om_card"] = {"code": 0, "data": {"items": [{
            "message_id": "om_card", "chat_id": self.scope.chat_id, "thread_id": self.scope.topic_id,
        }]}}
        self.handler_calls = 0

    async def asyncTearDown(self):
        await self.gate.dispose()
        await self.fixture.asyncTearDown()

    def new_form(self):
        card = schedule_form_card(self.scope, projects=self.fixture.projects.list(enabled_only=True), default_timezone="UTC")
        values = form_values(card)
        values[next(key for key in values if key.startswith("cron_name"))] = "原样重试计划"
        values.update(cron_kind="daily", cron_instructions="保留完整指令")
        return values

    async def push(self, *, form=None, value=None):
        action = SimpleNamespace(tag="button", value=value, form_value=form)
        event = SimpleNamespace(message_id="om_card", chat_id=self.scope.chat_id,
            operator=SimpleNamespace(open_id="ou_user"), action=action)
        key = "card:om_card:ou_user:" + _card_action_identity(action)
        async def handler():
            self.handler_calls += 1
            await self.fixture.app.handle_card_action(event)
        # The real pinned gate marks a failed handler's identity seen as well.
        # Every assertion below includes this gate, not a direct handler call.
        await self.gate.push_action(key, self.scope.chat_id, handler)
        async with asyncio.timeout(5):
            while not await self.gate._seen.has(key):
                await asyncio.sleep(0)
        return key

    def restored_form(self):
        message_id, card = self.fixture.channel.updates[-1]
        self.assertEqual(message_id, "om_card")
        return form_values(SimpleNamespace(card=card))

    async def test_transient_target_lookup_then_unchanged_retry_passes_real_sdk_gate(self):
        form = self.new_form()
        original = decode_schedule_action(scope=self.scope, value=None, form=form)
        lookup = self.fixture.channel.get_chat_info
        self.fixture.channel.get_chat_info = AsyncMock(side_effect=OSError("temporary target lookup failure"))
        first_key = await self.push(form=form)
        self.assertEqual(self.handler_calls, 1)
        self.assertFalse(self.fixture.store.schedules.list(app_id="app"))
        restored = self.restored_form()
        self.assertEqual(decode_schedule_action(scope=self.scope, value=None, form=restored), original)
        self.fixture.channel.get_chat_info = lookup
        await self.push(form=form)
        self.assertEqual(self.handler_calls, 1)  # Genuine redelivery still dedups.
        second_key = await self.push(form=restored)
        self.assertNotEqual(first_key, second_key)
        self.assertEqual(self.handler_calls, 2)
        self.assertEqual(len(self.fixture.store.schedules.list(app_id="app")), 1)

    async def test_unknown_write_response_retries_same_business_request_without_duplicate_create(self):
        form = self.new_form()
        manage = self.fixture.management.schedules.manage
        lost = False
        async def lose_first_write_response(request, **kwargs):
            nonlocal lost
            result = await manage(request, **kwargs)
            if request["mode"] == "create" and not lost:
                lost = True
                raise OSError("response lost after commit")
            return result
        self.fixture.management.schedules.manage = lose_first_write_response
        with self.assertLogs("netizen.channel_app", level="ERROR"):
            await self.push(form=form)
        self.assertEqual(len(self.fixture.store.schedules.list(app_id="app")), 1)
        restored = self.restored_form()
        self.assertEqual(decode_schedule_action(scope=self.scope, value=None, form=restored).request_id,
            decode_schedule_action(scope=self.scope, value=None, form=form).request_id)
        await self.push(form={**restored, "cron_instructions": "不能把未知结果变成另一项创建"})
        self.assertEqual(len(self.fixture.store.schedules.list(app_id="app")), 1)
        self.assertEqual(self.fixture.store.schedules.list(app_id="app")[0].instructions, form["cron_instructions"])
        await self.push(form=restored)
        self.assertEqual(self.handler_calls, 3)
        self.assertEqual(len(self.fixture.store.schedules.list(app_id="app")), 1)
        self.assertIn("计划已保存", str(self.fixture.channel.updates[-1]))

    async def test_scope_lookup_failure_only_restores_ui_then_revalidates_before_write(self):
        form = self.new_form()
        fetch = self.fixture.channel.fetch_message
        self.fixture.channel.fetch_message = AsyncMock(side_effect=OSError("temporary message lookup failure"))
        with self.assertLogs("netizen.channel_app", level="ERROR"):
            await self.push(form=form)
        self.assertFalse(self.fixture.store.schedules.list(app_id="app"))
        restored = self.restored_form()
        self.fixture.channel.fetch_message = fetch
        await self.push(form=restored)
        self.assertEqual(self.handler_calls, 2)
        self.assertEqual(len(self.fixture.store.schedules.list(app_id="app")), 1)

    async def test_validation_failure_keeps_inputs_and_allows_correction_through_sdk(self):
        form = self.new_form()
        form["cron_timezone"] = "invalid/timezone"
        await self.push(form=form)
        restored = self.restored_form()
        self.assertEqual(restored["cron_timezone"], "invalid/timezone")
        self.assertEqual(restored["cron_instructions"], form["cron_instructions"])
        restored["cron_timezone"] = "UTC"
        await self.push(form=restored)
        self.assertEqual(self.handler_calls, 2)
        plans = self.fixture.store.schedules.list(app_id="app")
        self.assertEqual([(plan.name, plan.instructions) for plan in plans], [("原样重试计划", "保留完整指令")])

    async def test_retry_card_update_failure_has_one_bounded_feedback_and_no_write(self):
        form = self.new_form()
        self.fixture.channel.get_chat_info = AsyncMock(side_effect=OSError("temporary failure"))
        self.fixture.channel.fail_card_updates = True
        with self.assertLogs("netizen.channel_app", level="ERROR"):
            await self.push(form=form)
        self.assertEqual(len(self.fixture.channel.updates), 1)
        self.assertEqual(len(self.fixture.channel.replies), 1)
        self.assertIn("未能恢复", str(self.fixture.channel.replies[-1]))
        self.assertFalse(self.fixture.store.schedules.list(app_id="app"))

    async def test_button_unknown_response_retries_same_exact_mutation(self):
        plan_id = self.fixture.manager_plan("可暂停")
        manage = self.fixture.management.schedules.manage
        result = await manage({"mode": "view", "plan_id": plan_id}, source="card")
        value = callback(schedule_manager_card(self.scope, {"plans": [result["plan"]]}, selected=result), "暂停")
        lost = False
        async def lose_first_update_response(request, **kwargs):
            nonlocal lost
            result = await manage(request, **kwargs)
            if request["mode"] == "update" and not lost:
                lost = True
                raise OSError("response lost after commit")
            return result
        self.fixture.management.schedules.manage = lose_first_update_response
        with self.assertLogs("netizen.channel_app", level="ERROR"):
            await self.push(value=value)
        retry = callback(SimpleNamespace(card=self.fixture.channel.updates[-1][1]), "重试刚才的操作")
        self.assertNotEqual(retry["nonce"], value["nonce"])
        self.assertEqual(retry["request_id"], value["request_id"])
        await self.push(value=retry)
        self.assertEqual(self.handler_calls, 2)
        self.assertFalse(self.fixture.store.schedules.get(plan_id).enabled)
        self.assertEqual(self.fixture.store.schedules.get(plan_id).revision, 2)


if __name__ == "__main__":
    unittest.main()
