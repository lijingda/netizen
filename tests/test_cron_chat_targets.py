"""Chat target forms through the Channel callback and real schedule service."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

from netizen_cli.cards.scheduled import decode_schedule_action
from netizen_cli.chat_targets import ChatTargetError
from netizen_cli.domain import FeishuScope, MentionContextMode, MessageContextAnchor, ScopeKind
from netizen_cli.management.chat_directory import AvailableChat, AvailableChatPage
from netizen_cli.schedules.models import ScheduleRule
from netizen_cli.session_settings import BindingTurnSettings, SessionSettings
from tests.support.channel_cards import callback, elements, form_values, option_value
from tests.support.channel_fixtures import scheduled_channel_fixture


class CronChatTargetsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = await self.enterAsyncContext(scheduled_channel_fixture())
        self.app = self.fixture.app
        self.channel = self.fixture.channel
        self.store = self.fixture.store
        self.management = self.fixture.management
        self.scope = FeishuScope("app", "oc_group", ScopeKind.GROUP)
        self.current = AvailableChat("oc_group", "当前研发群", "group", False)
        self.other = AvailableChat("oc_other", "另一研发群", "group", False)
        self.management.query_available_chats = AsyncMock(
            return_value=AvailableChatPage((self.current, self.other), None)
        )
        self.management.validate_available_chat = AsyncMock(
            side_effect=lambda chat_id: {"oc_group": self.current, "oc_other": self.other}.get(chat_id)
        )
        self.avatars = AsyncMock(return_value={"oc_group": "img_current", "oc_other": "img_other"})
        self.app._chat_avatars.prepare = self.avatars
        self.targets = self.management._chat_directory

    async def action(self, *, value=None, form=None, scope=None):
        scope = scope or self.scope
        self.channel.fetched_messages["om_cron_target"] = {"data": {"items": [{
            "message_id": "om_cron_target", "chat_id": scope.chat_id,
            **({"thread_id": scope.topic_id} if scope.topic_id else {}),
        }]}}
        event = SimpleNamespace(
            message_id="om_cron_target", chat_id=scope.chat_id,
            operator=SimpleNamespace(open_id="ou_user"),
            action=SimpleNamespace(tag="button", value=value, form_value=form),
        )
        previous = len(self.channel.updates)
        await self.app.handle_card_action(event)
        self.assertEqual(len(self.channel.updates), previous + 1)
        return SimpleNamespace(card=self.channel.updates[-1][1])

    async def editor(self, *, plan_id=None, scope=None, label="新建定时任务"):
        scope = scope or self.scope
        navigation = {"filter": "all", "plan_id": plan_id} if plan_id else None
        manager = await self.app._schedule_manager_card(scope, navigation=navigation)
        return await self.action(value=callback(manager, "编辑" if plan_id else label), scope=scope)

    @staticmethod
    def field(card, name):
        return next(item for item in elements(card.card, "select_static") if item["name"] == name)

    async def jump(self, card, page_index, *, values=None, scope=None):
        page_value = self.field(card, "cron_chat_page")["options"][page_index]["value"]
        return await self.action(value=callback(card, "跳转"), form={
            **form_values(card), **(values or {}), "cron_chat_page": page_value,
        }, scope=scope)

    @staticmethod
    def complete(card, **overrides):
        values = form_values(card)
        values[next(key for key in values if key.startswith("cron_name"))] = "目标选择测试"
        values[next(key for key in values if key.startswith("cron_instructions"))] = "检查明确资源"
        values.update(cron_kind="daily", cron_timezone="UTC", **overrides)
        return values

    @staticmethod
    def stable_form(values):
        # Transport nonces renew and a failed submission may gain a directory
        # search field. An empty group picker can be omitted entirely; neither
        # display choice changes business fields or UTC/version references.
        values = {"cron_group_id": "", **values}
        return {"cron_name" if key.startswith("cron_name") else key: value
                for key, value in values.items() if key not in {"cron_chat_query", "cron_chat_page"}}

    async def test_new_defaults_current_and_edit_loads_saved_group_avatar(self):
        card = await self.editor()
        values = form_values(card)
        self.assertEqual(values["cron_target_mode"], "current")
        self.assertNotIn("cron_group_id", values)
        self.management.query_available_chats.assert_not_awaited()
        self.management.validate_available_chat.assert_not_awaited()
        self.avatars.assert_not_awaited()
        card = await self.action(value=callback(card, "查找群聊"), form=values)
        self.management.query_available_chats.assert_awaited_once_with(query="", page_token=None, page_size=20)
        self.assertEqual(form_values(card)["cron_group_id"], "")
        self.assertEqual({item["value"]: item["icon"]["img_key"] for item in self.field(card, "cron_group_id")["options"]},
                         {"oc_group": "img_current", "oc_other": "img_other"})
        await self.action(form=self.complete(card,
            cron_target_mode="group", cron_group_id="oc_other", cron_chat_id="oc_residual"))
        saved, = self.store.schedules.list(app_id="app")
        self.assertEqual(saved.chat_id, "oc_other")
        self.assertEqual(self.targets.calls, ["oc_other"])
        # An edited target not in the current directory batch is resolved for
        # display, not silently replaced with the management card's chat.
        self.management.query_available_chats.reset_mock()
        self.management.validate_available_chat = AsyncMock(return_value=self.other)
        edited = await self.editor(plan_id=saved.id)
        self.management.query_available_chats.assert_not_awaited()
        self.management.validate_available_chat.assert_awaited_once_with("oc_other")
        self.assertEqual(form_values(edited)["cron_group_id"], "oc_other")
        self.assertEqual(self.field(edited, "cron_group_id")["options"][-1]["icon"]["img_key"], "img_other")

    async def test_current_target_uses_fetched_chat_and_ignores_inactive_inputs(self):
        self.channel.chat_types["oc_private"] = "p2p"
        scopes = (
            self.scope,
            FeishuScope("app", "oc_private", ScopeKind.DIRECT),
            FeishuScope("app", "oc_group", ScopeKind.TOPIC, "omt_origin"),
        )
        for scope in scopes:
            with self.subTest(scope=scope):
                card = await self.editor(scope=scope)
                self.assertEqual(form_values(card)["cron_target_mode"], "current")
                previous = {plan.id for plan in self.store.schedules.list(app_id="app")}
                await self.action(scope=scope, form=self.complete(card,
                    cron_group_id={"malformed": "inactive"}, cron_chat_id=["oc_other"]))
                saved, = (plan for plan in self.store.schedules.list(app_id="app") if plan.id not in previous)
                self.assertEqual(saved.chat_id, scope.chat_id)
        self.assertEqual(self.targets.calls, [scope.chat_id for scope in scopes])
        self.management.query_available_chats.assert_not_awaited()
        self.management.validate_available_chat.assert_not_awaited()

    async def test_empty_explicit_id_does_not_fall_back_to_current_or_group(self):
        card = await self.editor()
        values = self.complete(card, cron_target_mode="id", cron_chat_id="  ", cron_group_id="oc_other")
        retry = await self.action(form=values)
        self.assertEqual(self.store.schedules.list(app_id="app"), ())
        self.assertEqual(self.targets.calls, [])
        self.assertEqual(self.stable_form(form_values(retry)), self.stable_form(values))
        restored = form_values(retry)
        restored["cron_chat_id"] = "oc_other"
        await self.action(form=restored)
        saved, = self.store.schedules.list(app_id="app")
        self.assertEqual(saved.chat_id, "oc_other")

    async def test_id_target_accepts_p2p_ignores_group_and_reports_saved_outside_filter(self):
        self.channel.chat_types["oc_private"] = "p2p"
        card = await self.editor()
        rendered = await self.action(form=self.complete(card,
            cron_target_mode="id", cron_chat_id="oc_private", cron_group_id="oc_other"))
        saved, = self.store.schedules.list(app_id="app")
        self.assertEqual(saved.chat_id, "oc_private")
        self.assertEqual(saved.session_settings.message_context_mode.value, "current-only")
        self.assertEqual(self.targets.calls, ["oc_private"])
        self.assertIn("计划已保存", str(rendered.card))
        self.assertIn("oc_private", str(rendered.card))
        self.assertIn("不符合当前筛选", str(rendered.card))
        self.assertNotIn("plan_id", callback(rendered, "新建定时任务")["navigation"])
        # Editing that private destination never asks the group-only display
        # validator to recognize P2P, and preserves the saved ID.
        self.management.validate_available_chat = AsyncMock(side_effect=AssertionError("P2P is not a group"))
        edited = await self.editor(plan_id=saved.id)
        self.management.validate_available_chat.assert_not_awaited()
        values = form_values(edited)
        self.assertEqual((values["cron_target_mode"], values["cron_chat_id"]), ("id", "oc_private"))

    async def test_empty_group_does_not_fall_back_to_id_or_save_and_retry_keeps_draft(self):
        card = await self.editor()
        values = self.complete(card, cron_target_mode="group", cron_group_id="", cron_chat_id="oc_other")
        retry = await self.action(form=values)
        self.assertEqual(self.store.schedules.list(app_id="app"), ())
        self.assertEqual(self.targets.calls, [])
        self.assertIn("请选择飞书群聊", str(retry.card))
        restored = form_values(retry)
        self.assertEqual(self.stable_form(restored), self.stable_form(values))
        restored["cron_group_id"] = "oc_other"
        await self.action(form=restored)
        saved, = self.store.schedules.list(app_id="app")
        self.assertEqual(saved.chat_id, "oc_other")

    async def test_server_rejects_unreachable_target_without_saving_and_preserves_retry_identity(self):
        self.targets.errors["oc_denied"] = ChatTargetError("not_accessible", "机器人无法访问此聊天。")
        card = await self.editor()
        values = self.complete(card, cron_target_mode="id", cron_chat_id="oc_denied", cron_group_id="oc_other")
        retry = await self.action(form=values)
        self.assertEqual(self.store.schedules.list(app_id="app"), ())
        self.assertIn("机器人无法访问", str(retry.card))
        self.assertEqual(self.targets.calls, ["oc_denied"])
        restored = form_values(retry)
        self.assertEqual(self.stable_form(restored), self.stable_form(values))
        restored["cron_chat_id"] = "oc_other"
        await self.action(form=restored)
        saved, = self.store.schedules.list(app_id="app")
        self.assertEqual(saved.chat_id, "oc_other")
        await self.action(form=restored)
        self.assertEqual(len(self.store.schedules.list(app_id="app")), 1)

    async def test_directory_failure_keeps_id_flow_and_current_private_default(self):
        self.management.query_available_chats.side_effect = TimeoutError("directory unavailable")
        self.management.validate_available_chat = AsyncMock(side_effect=AssertionError("private target"))
        self.channel.chat_types["oc_private"] = "p2p"
        scope = FeishuScope("app", "oc_private", ScopeKind.DIRECT)
        card = await self.editor(scope=scope)
        self.management.query_available_chats.assert_not_awaited()
        card = await self.action(value=callback(card, "查找群聊"), form=form_values(card), scope=scope)
        self.assertIn("填写聊天 ID", str(card.card))
        self.assertIn("群聊查找超时", str(card.card))
        self.management.query_available_chats.assert_awaited_once_with(query="", page_token=None, page_size=20)
        values = self.complete(card, cron_target_mode="current", cron_chat_id="oc_other", cron_group_id="oc_other")
        await self.action(form=values, scope=scope)
        saved, = self.store.schedules.list(app_id="app")
        self.assertEqual(saved.chat_id, "oc_private")
        self.assertEqual(self.targets.calls, ["oc_private"])
        self.management.validate_available_chat.assert_not_awaited()

    async def test_search_and_paging_allow_incomplete_task_and_preserve_write_metadata(self):
        groups = tuple(AvailableChat(f"oc_page_{i}", f"研发 {i}", "group", False) for i in range(23))
        self.management.query_available_chats.side_effect = [
            AvailableChatPage(groups[:20], "tail"), AvailableChatPage(groups[20:], None),
        ]
        self.management.validate_available_chat = AsyncMock(
            side_effect=lambda chat_id: self.current if chat_id == self.current.chat_id else None
        )
        created = self.store.schedules.create(
            name="保留原时刻", instructions="原始指令", project_alias="work", app_id="app", chat_id="oc_group",
            schedule=ScheduleRule("once", "America/New_York", at="2030-11-03T01:30:00-05:00"),
            request_id="search-fixture", now=160,
        )
        card = await self.editor(plan_id=created.plan_id)
        self.management.query_available_chats.assert_not_awaited()
        values = form_values(card)
        values[next(key for key in values if key.startswith("cron_name"))] = ""
        values[next(key for key in values if key.startswith("cron_instructions"))] = ""
        values.update(cron_target_mode="group", cron_group_id="oc_group", cron_chat_id="oc_private", cron_chat_query="研发")
        before = self.store.schedules.get(created.plan_id)
        changes = self.store._connection.total_changes
        self.management.query_available_chats.reset_mock()
        searched = await self.action(value=callback(card, "查找群聊"), form=values)
        self.assertEqual(self.store._connection.total_changes, changes)
        self.assertEqual(self.store.schedules.get(created.plan_id), before)
        expected_search = {**values, "cron_group_id": ""}
        self.assertEqual(self.stable_form(form_values(searched)), self.stable_form(expected_search))
        self.assertEqual(form_values(searched)["cron_chat_query"], "研发")
        self.assertNotEqual(next(key for key in form_values(searched) if key.startswith("cron_name")),
                            next(key for key in values if key.startswith("cron_name")))
        self.assertEqual(self.management.query_available_chats.await_args_list, [
            call(query="研发", page_token=None, page_size=20), call(query="研发", page_token="tail", page_size=20),
        ])
        for field in elements(searched.card, "input") + elements(searched.card, "select_static"):
            self.assertFalse(field.get("required", False))
        next_action = callback(searched, "跳转")
        self.assertEqual(next_action["action"], "page_chats")
        self.assertIn("snapshot", next_action["payload"])
        self.assertEqual(len(self.field(searched, "cron_chat_page")["options"]), 3)
        self.management.query_available_chats.reset_mock()
        self.management.validate_available_chat.reset_mock()
        self.avatars.reset_mock()
        pending = {**form_values(searched), "cron_group_id": "oc_page_0", "cron_chat_query": "尚未查找的产品群"}
        paged = searched
        for page_index in (2, 1, 0):
            paged = await self.jump(paged, page_index, values={
                "cron_group_id": "oc_page_0", "cron_chat_query": "尚未查找的产品群",
            })
            self.assertEqual(callback(paged, "跳转")["payload"]["snapshot"], next_action["payload"]["snapshot"])
            options = {option["value"] for option in self.field(paged, "cron_group_id")["options"]}
            expected_page = {group.chat_id for group in groups[page_index * 10:(page_index + 1) * 10]}
            self.assertEqual(options, expected_page | {"oc_page_0"})
            self.assertEqual(self.stable_form(form_values(paged)), self.stable_form(pending))
        self.management.query_available_chats.assert_not_awaited()
        self.management.validate_available_chat.assert_not_awaited()
        self.avatars.assert_not_awaited()
        restored = form_values(paged)
        self.assertEqual(self.stable_form(restored), self.stable_form(pending))
        self.assertEqual(restored["cron_chat_query"], "尚未查找的产品群")
        self.assertEqual(restored["cron_group_id"], "oc_page_0")
        self.assertEqual(self.store._connection.total_changes, changes)
        self.assertEqual(self.targets.calls, [])
        self.assertEqual(option_value(restored["cron_project"])["request_id"],
                         option_value(values["cron_project"])["request_id"])
        restored[next(key for key in restored if key.startswith("cron_name"))] = "补全草稿"
        restored[next(key for key in restored if key.startswith("cron_instructions"))] = "搜索后保存"
        await self.action(form=restored)
        saved = self.store.schedules.get(created.plan_id)
        self.assertEqual(saved.schedule, before.schedule)
        self.assertEqual(saved.revision, before.revision + 1)
        self.assertEqual(saved.instructions, "搜索后保存")

    async def test_complete_snapshot_consumes_empty_platform_page_and_does_not_drop_tail(self):
        groups = tuple(AvailableChat(f"oc_search_{i}", f"检索群 {i}", "group", False) for i in range(12))
        self.management.query_available_chats.side_effect = [
            AvailableChatPage((), "first"),
            AvailableChatPage(groups, None),
        ]
        card = await self.editor()
        before = self.store._connection.total_changes
        card = await self.action(value=callback(card, "查找群聊"), form=form_values(card))
        self.assertEqual(self.management.query_available_chats.await_args_list, [
            call(query="", page_token=None, page_size=20), call(query="", page_token="first", page_size=20),
        ])
        self.assertEqual([item["value"] for item in self.field(card, "cron_group_id")["options"]],
                         [group.chat_id for group in groups[:10]])
        self.management.query_available_chats.reset_mock()
        self.management.validate_available_chat.reset_mock()
        self.avatars.reset_mock()
        card = await self.jump(card, 1)
        self.assertEqual([item["value"] for item in self.field(card, "cron_group_id")["options"]],
                         [group.chat_id for group in groups[10:]])
        self.management.query_available_chats.assert_not_awaited()
        self.management.validate_available_chat.assert_not_awaited()
        self.avatars.assert_not_awaited()
        self.assertFalse(any(button["text"]["content"] == "下一页" for button in elements(card.card, "button")))
        self.assertEqual(self.store._connection.total_changes, before)
        self.assertEqual(self.targets.calls, [])

    async def test_failed_or_bounded_snapshot_never_exposes_a_partial_directory(self):
        groups = tuple(AvailableChat(f"oc_partial_{i}", f"不可展示的部分结果 {i}", "group", False) for i in range(201))
        scenarios = {
            "midstream_failure": [AvailableChatPage(groups[:20], "next"), TimeoutError("directory failed")],
            "too_many_results": [AvailableChatPage(groups[index:index + 20], str(index + 20) if index < 200 else None)
                                 for index in range(0, 201, 20)],
            "too_many_requests": [AvailableChatPage((), str(index + 1)) for index in range(20)],
        }
        for label, replies in scenarios.items():
            with self.subTest(label=label):
                card = await self.editor()
                self.management.query_available_chats.reset_mock()
                self.management.query_available_chats.side_effect = replies
                self.avatars.reset_mock()
                values = self.complete(card, cron_chat_query="需要完整结果", cron_chat_id="oc_private")
                result = await self.action(value=callback(card, "查找群聊"), form=values)
                self.assertLessEqual(self.management.query_available_chats.await_count, 20)
                self.assertNotIn("oc_partial_", str(result.card))
                self.assertFalse(any(button["text"]["content"] == "跳转" for button in elements(result.card, "button")))
                self.assertEqual(self.stable_form(form_values(result)), self.stable_form({**values, "cron_group_id": ""}))
                self.assertFalse(any(call.args[0] for call in self.avatars.await_args_list))
                self.assertEqual(self.store.schedules.list(app_id="app"), ())
        self.assertEqual(self.targets.calls, [])

    async def test_search_and_business_error_preserve_complete_model_catalog(self):
        # A verified topic does not need mainline chat-kind resolution during
        # callback admission, isolating optional model reload from target reads.
        scope = FeishuScope("app", "oc_group", ScopeKind.TOPIC, "omt_model_draft")
        card = await self.editor(scope=scope)
        config_names = ("cron_session_model", "cron_session_effort", "cron_session_speed")
        original = {name: self.field(card, name)["options"] for name in config_names}
        self.assertGreater(len(original["cron_session_model"]), 1)
        self.management.schedules.form_options = AsyncMock(wraps=self.management.schedules.form_options)
        self.channel.get_chat_info = AsyncMock(wraps=self.channel.get_chat_info)
        self.store.query_bindings = AsyncMock(wraps=self.store.query_bindings)
        self.management.query_available_chats.return_value = AvailableChatPage(tuple(
            AvailableChat(f"oc_model_{index}", f"配置检索群 {index}", "group", False) for index in range(12)
        ), None)
        values = self.complete(card, cron_chat_query="研发", cron_chat_id="oc_denied")
        searched = await self.action(value=callback(card, "查找群聊"), form=values, scope=scope)
        self.assertEqual({name: self.field(searched, name)["options"] for name in config_names}, original)
        self.assertNotIn("模型目录暂不可用", str(searched.card))
        searched = await self.jump(searched, 1, scope=scope)
        self.assertEqual({name: self.field(searched, name)["options"] for name in config_names}, original)
        self.targets.errors["oc_denied"] = ChatTargetError("not_accessible", "机器人无法访问此聊天。")
        submitted = {**form_values(searched), "cron_target_mode": "id"}
        retry = await self.action(form=submitted, scope=scope)
        self.assertEqual({name: self.field(retry, name)["options"] for name in config_names}, original)
        self.assertNotIn("模型目录暂不可用", str(retry.card))
        self.assertEqual(self.store.schedules.list(app_id="app"), ())
        self.management.schedules.form_options.assert_not_awaited()
        self.channel.get_chat_info.assert_not_awaited()
        self.store.query_bindings.assert_not_awaited()

    async def test_unreadable_saved_target_opens_id_form_preserving_settings_and_can_be_corrected(self):
        settings = SessionSettings(
            turn_settings=BindingTurnSettings("removed-model", "high", "default"),
            message_context_mode=MentionContextMode.CATCH_UP,
        )
        created = self.store.schedules.create(
            name="保留配置", instructions="原始指令", project_alias="work", app_id="app", chat_id="oc_old",
            schedule=ScheduleRule("interval", "UTC", every_minutes=60, anchor=100),
            session_settings=settings, request_id="unreadable-target", now=160,
        )
        before = self.store.schedules.get(created.plan_id)
        self.channel.chat_types["oc_old"] = "unknown"
        card = await self.editor(plan_id=created.plan_id)
        values = form_values(card)
        self.assertEqual((values["cron_target_mode"], values["cron_chat_id"]), ("id", "oc_old"))
        decoded = decode_schedule_action(scope=self.scope, value=None, form=values)
        self.assertEqual(decoded.payload["session_settings"], settings.to_dict())
        self.assertEqual(decoded.payload["expected_revision"], before.revision)
        self.management.query_available_chats.assert_not_awaited()
        self.assertEqual(self.store.schedules.get(created.plan_id), before)
        retry = await self.action(form=values)
        self.assertEqual(self.store.schedules.get(created.plan_id), before)
        self.assertEqual(self.targets.calls, ["oc_old"])
        restored = form_values(retry)
        restored["cron_chat_id"] = "oc_other"
        await self.action(form=restored)
        saved = self.store.schedules.get(created.plan_id)
        self.assertEqual(saved.chat_id, "oc_other")
        self.assertEqual(saved.session_settings, settings)
        self.assertEqual(saved.revision, before.revision + 1)

    async def test_verified_topic_with_unreadable_chat_preserves_source_settings_without_guessing_kind(self):
        scope = FeishuScope("app", "oc_unknown", ScopeKind.TOPIC, "omt_known")
        source = self.store.create_channel_binding(
            scope=scope, project_alias="work", creator_id="ou_user",
            message_context_mode=MentionContextMode.CATCH_UP,
            context_anchor=MessageContextAnchor("om_anchor", 1000),
        )
        self.channel.chat_types[scope.chat_id] = "unknown"
        card = await self.editor(scope=scope)
        values = self.complete(card)
        self.assertEqual(values["cron_target_mode"], "current")
        self.assertEqual(values["cron_chat_id"], "")
        decoded = decode_schedule_action(scope=scope, value=None, form=values)
        self.assertEqual(decoded.payload["session_settings"], SessionSettings.from_binding(source).to_dict())
        self.management.query_available_chats.assert_not_awaited()
        values.update(cron_target_mode="id", cron_chat_id="oc_other")
        await self.action(form=values, scope=scope)
        saved, = self.store.schedules.list(app_id="app")
        self.assertEqual(saved.chat_id, "oc_other")
        self.assertEqual(saved.session_settings.message_context_mode, MentionContextMode.CATCH_UP)
        self.assertEqual(self.store.get(source.id), source)

    async def test_unreadable_chat_does_not_bypass_mainline_identity_or_fixed_target(self):
        scope = FeishuScope("app", "oc_unknown", ScopeKind.GROUP)
        self.channel.chat_types[scope.chat_id] = "unknown"
        card = await self.editor(scope=scope)
        self.assertEqual(elements(card.card, "form"), [])
        topic = FeishuScope("app", scope.chat_id, ScopeKind.TOPIC, "omt_known")
        self.store.create_channel_binding(scope=topic, project_alias="work", creator_id="ou_user")
        fixed = await self.editor(scope=topic, label="在当前 Agent 会话中定时执行")
        self.assertEqual(elements(fixed.card, "input"), [])
        self.assertEqual(self.store.schedules.list(app_id="app"), ())
        self.management.query_available_chats.assert_not_awaited()

    async def test_fixed_agent_target_stays_exact_after_switch_and_edit_from_other_chat(self):
        self.channel.chat_types["oc_private"] = "p2p"
        origin = FeishuScope("app", "oc_private", ScopeKind.TOPIC, "omt_original")
        original = self.store.create_channel_binding(scope=origin, project_alias="work", creator_id="ou_user")
        manager = await self.app._schedule_manager_card(origin)
        create = callback(manager, "在当前 Agent 会话中定时执行")
        replacement = self.store.create_channel_binding(scope=origin, project_alias="work", creator_id="ou_user")
        card = await self.action(value=create, scope=origin)
        self.management.query_available_chats.assert_not_awaited()
        self.avatars.assert_not_awaited()
        values = self.complete(card)
        self.assertFalse({"cron_target_mode", "cron_group_id", "cron_chat_id"} & values.keys())
        self.assertEqual(option_value(values["cron_project"])["target_binding_id"], original.id)
        self.assertIn("oc_private", str(card.card))
        self.assertIn("omt_original", str(card.card))
        await self.action(form=values, scope=origin)
        saved, = self.store.schedules.list(app_id="app")
        self.assertEqual(saved.target_binding_id, original.id)
        self.assertEqual(self.store.active_binding(origin.key).id, replacement.id)
        edited = await self.editor(plan_id=saved.id)
        self.management.query_available_chats.assert_not_awaited()
        self.assertIn("oc_private", str(edited.card))
        self.assertIn("omt_original", str(edited.card))
        self.assertEqual(option_value(form_values(edited)["cron_project"])["target_binding_id"], original.id)
        self.assertFalse({"cron_target_mode", "cron_group_id", "cron_chat_id"} & form_values(edited).keys())
