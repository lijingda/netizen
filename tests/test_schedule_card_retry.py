from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from lark_channel.channel.channel import _card_action_identity
from lark_channel.channel.safety.pipeline import SafetyPipeline

from netizen.bindings import BindingQueryBusy
from netizen.cards.scheduled import decode_schedule_action, schedule_form_card, schedule_manager_card
from netizen.domain import FeishuScope, ScopeKind

from tests.support.channel_fixtures import scheduled_channel_fixture
from tests.support.channel_cards import (
    callback,
    form_values,
)


class ScheduleCardRetryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = await self.enterAsyncContext(scheduled_channel_fixture())
        self.gate = SafetyPipeline(loop=asyncio.get_running_loop(), on_message=lambda _: None)
        self.addAsyncCleanup(self.gate.dispose)
        self.scope = FeishuScope("app", "oc_group", ScopeKind.TOPIC, "omt_retry")
        self.fixture.channel.fetched_messages["om_card"] = {"code": 0, "data": {"items": [{
            "message_id": "om_card", "chat_id": self.scope.chat_id, "thread_id": self.scope.topic_id,
        }]}}
        self.handler_calls = 0

    def new_form(self, *, target_binding_id=None):
        card = schedule_form_card(self.scope, projects=self.fixture.projects.list(enabled_only=True),
            default_timezone="UTC", target_binding_id=target_binding_id)
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

    def new_binding(self):
        return self.fixture.store.create_channel_binding(
            scope=self.scope, project_alias="work", creator_id="ou_user",
        )

    async def test_binding_directory_failure_does_not_block_new_topic_form(self):
        manager = schedule_manager_card(self.scope, {"plans": []})
        with patch.object(self.fixture.store, "query_bindings", side_effect=BindingQueryBusy("busy")):
            await self.push(value=callback(manager, "新建定时任务"))
        form = self.restored_form()
        form[next(key for key in form if key.startswith("cron_name"))] = "独立话题计划"
        form.update(cron_kind="daily", cron_instructions="检查项目")
        await self.push(form=form)
        plan, = self.fixture.store.schedules.list(app_id="app")
        self.assertEqual(plan.target_kind, "new_topic")
        self.assertIn("计划已保存", str(self.fixture.channel.updates[-1]))

    async def test_noncurrent_binding_create_form_and_crud_keep_the_selected_target(self):
        original = self.new_binding()
        manager = schedule_manager_card(self.scope, {"plans": []}, current_binding_id=original.id)
        create = callback(manager, "在当前会话定时执行")
        self.new_binding()  # Switching before opening is not a management restriction.
        await self.push(value=create)
        form = self.restored_form()
        form[next(key for key in form if key.startswith("cron_name"))] = "原目标计划"
        form.update(cron_kind="daily", cron_instructions="继续原问题")
        replacement = self.new_binding()  # Nor is switching before saving.
        await self.push(form=form)
        plan, = self.fixture.store.schedules.list(app_id="app")
        self.assertEqual(plan.target_binding_id, original.id)
        self.assertEqual(self.fixture.store.active_binding(self.scope.key).id, replacement.id)
        self.assertIn("计划已保存", str(self.fixture.channel.updates[-1]))
        detail = SimpleNamespace(card=self.fixture.channel.updates[-1][1])
        with self.assertRaises(StopIteration):
            callback(detail, "立即运行")
        await self.push(value=callback(detail, "编辑"))
        edit = self.restored_form()
        instructions_key = next(key for key in edit if key.startswith("cron_instructions"))
        edit[instructions_key] = "更新原目标的检查指令"
        await self.push(form=edit)
        updated = self.fixture.store.schedules.get(plan.id)
        self.assertEqual(updated.instructions, edit[instructions_key])
        self.assertEqual(updated.target_binding_id, original.id)
        detail = SimpleNamespace(card=self.fixture.channel.updates[-1][1])
        await self.push(value=callback(detail, "删除计划"))
        self.assertFalse(self.fixture.store.schedules.list(app_id="app"))
        self.assertEqual(self.fixture.store.active_binding(self.scope.key).id, replacement.id)

    async def test_binding_create_unknown_response_replays_after_switch_without_duplicate(self):
        original = self.new_binding()
        form = self.new_form(target_binding_id=original.id)
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
        plan, = self.fixture.store.schedules.list(app_id="app")
        restored = self.restored_form()
        self.assertEqual(decode_schedule_action(scope=self.scope, value=None, form=restored).request_id,
            decode_schedule_action(scope=self.scope, value=None, form=form).request_id)
        replacement = self.new_binding()
        await self.push(form=restored)
        replayed, = self.fixture.store.schedules.list(app_id="app")
        self.assertEqual((replayed.id, replayed.target_binding_id), (plan.id, original.id))
        self.assertEqual(self.fixture.store.active_binding(self.scope.key).id, replacement.id)
        self.assertIn("计划已保存", str(self.fixture.channel.updates[-1]))

    async def test_old_binding_run_button_is_rejected_after_switch_without_claim(self):
        original = self.new_binding()
        claims = self.fixture.enable_manual_claims()
        result = await self.fixture.management.schedules.manage({
            "mode": "create", "name": "原会话手动检查", "instructions": "检查原问题",
            "target_kind": "binding", "target_binding_id": original.id, "enabled": False,
            "schedule": {"kind": "interval", "every_minutes": 10, "timezone": "UTC"},
            "request_id": "binding-manual-create",
        }, scope_key=self.scope.key, source="card")
        self.assertTrue(result["ok"], result)
        plan = result["plan"]
        self.assertTrue(plan["can_run_now"])
        button = callback(schedule_manager_card(self.scope, {"plans": [plan]}, selected=result), "立即运行")
        replacement = self.new_binding()
        await self.push(value=button)
        self.assertEqual(claims, [])
        self.assertEqual(self.fixture.store.schedules.list_runs(plan["id"]), ())
        self.assertEqual(self.fixture.store.active_binding(self.scope.key).id, replacement.id)
        self.assertIn("目标会话不可用", str(self.fixture.channel.updates[-1]))

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

    async def test_run_now_unknown_response_retries_one_claim_through_real_sdk_gate(self):
        plan_id = self.fixture.manager_plan("手动执行重试")
        claims = self.fixture.enable_manual_claims()
        manage = self.fixture.management.schedules.manage
        result = await manage({"mode": "view", "plan_id": plan_id}, source="card")
        value = callback(schedule_manager_card(self.scope, {"plans": [result["plan"]]}, selected=result), "立即运行")
        lost = False

        async def lose_first_trigger_response(request, **kwargs):
            nonlocal lost
            result = await manage(request, **kwargs)
            if request["mode"] == "run_now" and not lost:
                lost = True
                raise OSError("response lost after manual claim")
            return result

        self.fixture.management.schedules.manage = lose_first_trigger_response
        with self.assertLogs("netizen.channel_app", level="ERROR"):
            first_key = await self.push(value=value)
        self.assertEqual(len(claims), 1)
        retry = callback(SimpleNamespace(card=self.fixture.channel.updates[-1][1]), "重试刚才的操作")
        self.assertNotEqual(retry["nonce"], value["nonce"])
        self.assertEqual(retry["request_id"], value["request_id"])
        self.assertEqual(retry["payload"], value["payload"])
        await self.push(value=value)
        self.assertEqual(self.handler_calls, 1)
        second_key = await self.push(value=retry)
        self.assertNotEqual(first_key, second_key)
        self.assertEqual(self.handler_calls, 2)
        self.assertEqual(len(claims), 1)
        self.assertEqual(len(self.fixture.store.schedules.list_runs(plan_id)), 1)
        self.assertIn("立即运行请求已受理", str(self.fixture.channel.updates[-1]))
        self.assertIn("同一请求未重复触发", str(self.fixture.channel.updates[-1]))


if __name__ == "__main__":
    unittest.main()
