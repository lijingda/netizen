from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import yaml

from netizen_cli.admin.port_config import ConfigConflict, AdminPortConfigurationError, ConfigFileEditor, ConfigFileSnapshot
from netizen_cli.admin.users import AdminIdentity, AdminUsers
from netizen_cli.group_access import GroupAccessRule, GroupAccessService, GroupAccessError, GROUP_ACCESS_NOTICE
from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.lark_app import encode_lark_app
from netizen_cli.settings import Settings, SettingsError
from tests.support.channel_fixtures import channel_fixture, side_channel_fixture
from tests.support.channel_messages import FakeMessage


class GroupAccessRuleTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "config.yaml"
        self.path.write_text("instance: {projectRoot: /project, dataDir: /state}\n")
        self.editor = ConfigFileEditor(ConfigFileSnapshot.read(self.path))
        self.service = GroupAccessService(config_editor=self.editor)

    def test_parsing_modes_retains_sorted_exact_ids_and_bounds(self):
        for mode in ("all", "only"):
            rule = GroupAccessRule.from_mapping({"mode": mode, "chatIds": ["z", "a", "z"]})
            self.assertEqual(rule.chat_ids, ("a", "z"))
            self.assertTrue(rule.allows("unlisted", "p2p"))
            self.assertTrue(rule.allows("a", "group"))
            self.assertEqual(rule.allows("unlisted", "group"), mode == "all")
            for kind in ("topic", "unknown", None):
                self.assertTrue(rule.allows("a", kind))
                self.assertEqual(rule.allows("unlisted", kind), mode == "all")
        self.assertEqual(len(GroupAccessRule("only", tuple(f"oc_{i}" for i in range(1000))).chat_ids), 1000)
        self.assertEqual(GroupAccessRule("only", ("a",) * 1001).chat_ids, ("a",))
        for value in (None, [], {"extra": True}, {"mode": False}, {"mode": "exclude"},
                      {"chatIds": "oc_a"}, {"chatIds": [None]}, {"chatIds": [""]},
                      {"chatIds": [" oc_a"]}, {"chatIds": ["oc_a\n"]},
                      {"chatIds": ["a\x00b"]}, {"chatIds": ["x" * 257]},
                      {"chatIds": [f"oc_{i}" for i in range(1001)]}):
            with self.subTest(value=str(value)[:100]), self.assertRaises(GroupAccessError):
                GroupAccessRule.from_mapping(value)

    def test_chat_rule_validation_preserves_field_prefixes(self):
        for value, message in (
            ({"extra": 1}, "groupAccess must contain only mode and chatIds"),
            ({"mode": False}, "groupAccess.mode must be all or only"),
            ({"chatIds": "x"}, "groupAccess.chatIds must be a list"),
            ({"chatIds": ["x\x00"]}, "groupAccess.chatIds must contain non-empty exact chat IDs"),
            ({"chatIds": [str(i) for i in range(1001)]}, "groupAccess.chatIds must contain at most 1000 chat IDs"),
        ):
            with self.subTest(message=message), self.assertRaises(GroupAccessError) as raised:
                GroupAccessRule.from_mapping(value)
            self.assertEqual(str(raised.exception), message)

    def test_snapshot_update_conflict_and_write_failures(self):
        old, revision = self.service.snapshot()
        rule = GroupAccessRule("only", ("oc_group",))
        self.service.update(rule, expected_revision=revision)
        self.assertTrue(old.allows("other", "group"))
        self.assertEqual(yaml.safe_load(self.path.read_text())["groupAccess"], rule.to_dict())
        with self.assertRaises(ConfigConflict):
            self.service.update(GroupAccessRule(), expected_revision=revision)
        before = self.service.snapshot()
        with patch("netizen_cli.admin.port_config.os.replace", side_effect=OSError("read only")):
            with self.assertRaises(OSError):
                self.service.update(GroupAccessRule(), expected_revision=before[1])
        self.assertEqual(before, self.service.snapshot())
        self.path.write_text(self.path.read_text() + "# external edit\n")
        with self.assertRaises(AdminPortConfigurationError):
            self.service.update(GroupAccessRule(), expected_revision=before[1])
        self.assertEqual(before, self.service.snapshot())

    def test_settings_default_loaded_and_invalid_rules(self):
        app_path = self.root / "lark-app/config.json"
        app_path.parent.mkdir()
        app_path.write_bytes(encode_lark_app("cli_test", "synthetic-secret"))
        app_path.chmod(0o600)
        self.assertEqual(Settings.from_file(self.path, environment={}).group_access, GroupAccessRule())
        rule = GroupAccessRule("all", ("oc_retained",))
        self.service.update(rule, expected_revision=self.service.snapshot()[1])
        self.assertEqual(Settings.from_file(self.path, environment={}).group_access, rule)
        base = self.path.read_text().split("groupAccess:")[0]
        for value in ("null", "{mode: wrong}", "{chatIds: invalid}"):
            self.path.write_text(base + "groupAccess: " + value + "\n")
            with self.assertRaises(SettingsError):
                Settings.from_file(self.path, environment={})


class GroupAccessChannelTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = await self.enterAsyncContext(channel_fixture())
        self.app = self.fixture.app
        self.channel = self.fixture.channel

    async def test_inbound_matrix_without_chat_info_io(self):
        for mode in ("all", "only"):
            for kind, topic in (("p2p", None), ("p2p", "omt_direct"), ("group", None), ("group", "omt_group"),
                                ("topic", None), ("topic", "omt_topic"), ("unknown", None), ("unknown", "omt_unknown")):
                for mentioned in (False, True):
                    for enabled in (False, True):
                        with self.subTest(mode=mode, kind=kind, topic=topic, mentioned=mentioned, enabled=enabled):
                            self.app._group_access = GroupAccessService(GroupAccessRule(mode, ("oc_target",) if enabled else ()))
                            self.channel.replies.clear()
                            message = FakeMessage("work", message_id="om_input", chat_id="oc_target", chat_type=kind,
                                                  thread_id=topic, mentioned_bot=mentioned, reply_id="om_quote")
                            admitted = (kind == "p2p" or mentioned) and (mode == "all" or kind == "p2p" or enabled)
                            with patch.object(self.app, "_prompt", new_callable=AsyncMock) as prompt:
                                await self.app.handle_message(message)
                                self.assertEqual(prompt.await_count, int(admitted))
                            expected_notice = mode == "only" and kind != "p2p" and not enabled and mentioned
                            self.assertEqual(len(self.channel.replies), int(expected_notice))
                            if expected_notice:
                                self.assertIn(GROUP_ACCESS_NOTICE, str(self.channel.replies))
        self.assertEqual(self.channel.chat_info_calls, [])
        self.assertEqual(self.channel.fetch_quoted_calls, [])
        self.assertEqual(self.channel.download_resource_calls, [])

    async def test_blocked_input_never_prepares_or_routes_commands_sides_schedules(self):
        self.app._group_access = GroupAccessService(GroupAccessRule("only"))
        with patch.object(self.app, "_parse_current_interaction") as parse, \
             patch.object(self.app, "_side_message", new_callable=AsyncMock) as side, \
             patch.object(self.app, "_prompt", new_callable=AsyncMock) as prompt, \
             patch.object(self.app, "_control", new_callable=AsyncMock) as control, \
             patch.object(self.app._bindings.schedules, "pending_route") as pending:
            for text in ("/new", "/stop", "prompt"):
                await self.app.handle_message(FakeMessage(text, message_id="om_" + text, chat_id="oc_group",
                                                         chat_type="group", thread_id="omt_topic", reply_id="om_quote"))
            for mock in (parse, side, prompt, control, pending):
                mock.assert_not_called()
        self.assertEqual(self.fixture.runtime.submit_calls, [])
        self.assertEqual(self.channel.fetch_inbound_calls, [])
        self.assertEqual(self.channel.fetch_quoted_calls, [])
        self.assertEqual(self.channel.download_resource_calls, [])
        self.assertEqual(len(self.channel.replies), 3)

    async def test_one_notice_attempt_per_delivered_message_or_card(self):
        self.app._group_access = GroupAccessService(GroupAccessRule("only"))
        self.channel.chat_types["oc_group"] = "group"
        message = FakeMessage("work", message_id="om_message", chat_id="oc_group", chat_type="group")
        card = SimpleNamespace(chat_id="oc_group", message_id="om_card", action=object())
        for outcome in (True, False, OSError("send failed")):
            with self.subTest(outcome=outcome), ExitStack() as stack:
                reply = stack.enter_context(patch.object(self.channel, "reply", AsyncMock(
                    return_value=SimpleNamespace(success=outcome),
                    side_effect=outcome if isinstance(outcome, Exception) else None,
                )))
                if outcome is not True:
                    stack.enter_context(self.assertLogs("netizen_cli.channel_app", level="WARNING"))
                for index, (handler, trigger) in enumerate(((self.app.handle_message, message),
                        (self.app.handle_card_action, card)) * 2, start=1):
                    await handler(trigger)
                    self.assertEqual(reply.await_count, index)
                    self.assertEqual(reply.await_args.args[1], GROUP_ACCESS_NOTICE)
        self.assertEqual(self.channel.updates, [])

    async def test_message_notice_keeps_sdk_topic_inference_and_fails_if_target_gone(self):
        self.app._group_access = GroupAccessService(GroupAccessRule("only"))
        for topic in (None, "omt_topic"):
            message = FakeMessage("work", message_id="om_message", chat_id="oc_group", chat_type="group", thread_id=topic)
            with patch.object(self.channel, "reply", AsyncMock(wraps=self.channel.reply)) as reply:
                await self.app.handle_message(message)
                reply.assert_awaited_once()
            target, content, options = reply.await_args.args
            self.assertIs(target, message)
            self.assertEqual(content, GROUP_ACCESS_NOTICE)
            self.assertEqual(options.reply_target_gone, "fail")
            self.assertIsNone(options.reply_to)
            self.assertIsNone(options.reply_in_thread)
            self.assertIsNone(options.receive_id_type)

    async def test_card_guard_matrix_and_unknown_kind_fail_closed(self):
        event = SimpleNamespace(chat_id="oc_target", message_id="om_card", action=SimpleNamespace(value={}))
        for mode, kind, enabled, expected in (
            ("all", "group", False, True), ("only", "group", False, False),
            ("only", "group", True, True), ("only", "p2p", False, True),
            ("only", "unknown", True, True), ("only", "error", True, True),
            ("only", "unknown", False, False), ("only", "error", False, False),
        ):
            with self.subTest(mode=mode, kind=kind):
                self.app._group_access = GroupAccessService(GroupAccessRule(mode, ("oc_target",) if enabled else ()))
                self.channel.replies.clear()
                lookup = AsyncMock(return_value=SimpleNamespace(chat_mode=kind))
                if kind == "error":
                    lookup.side_effect = OSError("lookup failed")
                with patch.object(self.channel, "get_chat_info", lookup), \
                     patch("netizen_cli.channel.entry.is_status_command", return_value=True), \
                     patch.object(self.app, "_handle_status_command", new_callable=AsyncMock) as handler:
                    await self.app.handle_card_action(event)
                    self.assertEqual(handler.await_count, int(expected))
                    self.assertEqual(lookup.await_count, int(mode == "only" and not enabled))
                    self.assertEqual(self.channel.updates, [])
                    self.assertEqual(len(self.channel.replies), int(not expected))
                    if not expected:
                        self.assertEqual(self.channel.replies[0], ("om_card", GROUP_ACCESS_NOTICE))

    async def test_blocked_cards_never_run_any_handler_or_decode_input(self):
        self.app._group_access = GroupAccessService(GroupAccessRule("only"))
        self.channel.chat_types["oc_group"] = "group"
        event = SimpleNamespace(chat_id="oc_group", message_id="om_card", action=SimpleNamespace(value={}))
        handlers = ("_handle_status_command", "_handle_fork_card_action", "_handle_defaults_card_action",
                    "_handle_question_card_action", "_handle_schedule_card_action", "_handle_turn_file_card_action",
                    "_decode_card_event", "_card_control")
        with ExitStack() as stack:
            mocks = [stack.enter_context(patch.object(self.app, name, new_callable=AsyncMock)) for name in handlers]
            await self.app.handle_card_action(event)
            for handler in mocks:
                handler.assert_not_called()
        self.assertEqual(self.channel.fetch_inbound_calls, [])
        self.assertEqual(self.channel.fetch_quoted_calls, [])
        self.assertEqual(self.fixture.runtime.submit_calls, [])
        self.assertEqual(self.channel.updates, [])
        self.assertEqual(self.channel.replies, [("om_card", GROUP_ACCESS_NOTICE)])

    async def test_blocked_unmentioned_side_message_is_silent_without_side_lookup(self):
        self.app._group_access = GroupAccessService(GroupAccessRule("only"))
        message = FakeMessage("work", message_id="om_side", chat_id="oc_group", chat_type="group",
                              thread_id="omt_side", mentioned_bot=False)
        with patch.object(self.app, "_side_route") as route, \
             patch.object(self.app, "_side_message", new_callable=AsyncMock) as side:
            await self.app.handle_message(message)
            route.assert_not_called()
            side.assert_not_called()
            self.assertEqual(self.channel.replies, [])

    async def test_blocked_card_notice_preserves_card_and_uses_only_available_destination(self):
        self.app._group_access = GroupAccessService(GroupAccessRule("only"))
        for chat_mode, in_thread in (("group", False), ("topic", True)):
            with self.subTest(chat_mode=chat_mode):
                event = SimpleNamespace(chat_id="oc_group", message_id="om_card", action=object())
                with ExitStack() as stack:
                    reply = stack.enter_context(patch.object(self.channel, "reply", AsyncMock(wraps=self.channel.reply)))
                    lookup = stack.enter_context(patch.object(self.channel, "get_chat_info", AsyncMock(
                        return_value=SimpleNamespace(chat_mode=chat_mode))))
                    forbidden = [stack.enter_context(patch.object(self.channel, name, new_callable=AsyncMock))
                                 for name in ("update_card", "fetch_message", "fetch_inbound_message",
                                              "fetch_quoted_context", "download_resource")]
                    forbidden.append(stack.enter_context(patch.object(self.app._bindings, "side_topic_for_message")))
                    await self.app.handle_card_action(event)
                    lookup.assert_awaited_once_with("oc_group")
                    reply.assert_awaited_once()
                    for operation in forbidden:
                        operation.assert_not_called()
                target, content, options = reply.call_args.args
                self.assertEqual((target.chat_id, target.message_id, target.conversation.thread_id),
                                 ("oc_group", "om_card", None))
                self.assertEqual(content, GROUP_ACCESS_NOTICE)
                self.assertEqual(options.reply_in_thread, in_thread)
                self.assertEqual(options.reply_target_gone, "fail")
        self.assertEqual(self.channel.updates, [])

    async def test_card_uses_entry_snapshot_even_if_rule_changes_during_lookup(self):
        self.app._group_access = GroupAccessService(GroupAccessRule("only"))
        async def lookup(chat_id):
            self.app._group_access = GroupAccessService()
            return SimpleNamespace(chat_type="group")
        event = SimpleNamespace(chat_id="oc_target", message_id="om_card", action=SimpleNamespace(value={}))
        with patch.object(self.channel, "get_chat_info", side_effect=lookup), \
             patch("netizen_cli.channel.entry.is_status_command", return_value=True), \
             patch.object(self.app, "_handle_status_command", new_callable=AsyncMock) as handler:
            await self.app.handle_card_action(event)
            handler.assert_not_called()
        self.assertEqual(self.channel.replies, [("om_card", GROUP_ACCESS_NOTICE)])

    async def test_card_direct_scope_fast_path_is_readonly_and_exact_app_chat(self):
        self.app._group_access = GroupAccessService(GroupAccessRule("only"))
        for app_id, chat_id, kind in (("cli_test", "oc_direct", ScopeKind.DIRECT),
                                    ("other_app", "oc_other", ScopeKind.DIRECT),
                                    ("cli_test", "oc_topic", ScopeKind.TOPIC)):
            self.fixture.store.create_channel_binding(
                scope=FeishuScope(app_id, chat_id, kind, "omt_a" if kind is ScopeKind.TOPIC else None),
                project_alias="test", creator_id="ou_user",
            )
        before = self.fixture.store._connection.total_changes
        for chat_id, expected in (("oc_direct", True), ("oc_other", False), ("oc_topic", False), ("oc_missing", False)):
            with self.subTest(chat_id=chat_id), \
                 patch.object(self.channel, "get_chat_info", AsyncMock(side_effect=OSError("unavailable"))) as lookup, \
                 patch("netizen_cli.channel.entry.is_status_command", return_value=True), \
                 patch.object(self.app, "_handle_status_command", new_callable=AsyncMock) as handler:
                await self.app.handle_card_action(SimpleNamespace(chat_id=chat_id, message_id="om_card", action=object()))
                self.assertEqual(lookup.await_count, int(not expected))
                self.assertEqual(handler.await_count, int(expected))
        self.assertEqual(self.fixture.store._connection.total_changes, before)

    async def test_direct_scope_read_failure_uses_chat_lookup_and_fails_closed(self):
        self.app._group_access = GroupAccessService(GroupAccessRule("only"))
        for kind in ("p2p", "group", "unknown", "error"):
            with self.subTest(kind=kind), ExitStack() as stack:
                direct = stack.enter_context(patch.object(
                    self.app._bindings, "has_direct_scope", side_effect=OSError("private details")))
                lookup = stack.enter_context(patch.object(self.channel, "get_chat_info", AsyncMock(
                    return_value=SimpleNamespace(chat_mode=kind),
                    side_effect=OSError("lookup failed") if kind == "error" else None)))
                stack.enter_context(patch("netizen_cli.channel.entry.is_status_command", return_value=True))
                handler = stack.enter_context(patch.object(self.app, "_handle_status_command", new_callable=AsyncMock))
                logs = stack.enter_context(self.assertLogs("netizen_cli.channel_app", level="WARNING"))
                self.channel.replies.clear()
                await self.app.handle_card_action(SimpleNamespace(
                    chat_id="oc_target", message_id="om_card", action=object()))
                direct.assert_called_once_with("cli_test", "oc_target")
                lookup.assert_awaited_once_with("oc_target")
                self.assertEqual(handler.await_count, int(kind == "p2p"))
                self.assertEqual(len(self.channel.replies), int(kind != "p2p"))
                self.assertEqual(logs.records[0].chat_id, "oc_target")
                self.assertEqual(logs.records[0].error_type, "OSError")
                self.assertNotIn("private details", str(logs.output))
                if kind != "p2p":
                    self.assertIn(GROUP_ACCESS_NOTICE, self.channel.replies[-1][1])

    async def test_all_and_listed_card_admission_skip_database_and_chat_lookup(self):
        for rule in (GroupAccessRule(), GroupAccessRule("only", ("oc_target",))):
            self.app._group_access = GroupAccessService(rule)
            with patch.object(self.app._bindings, "has_direct_scope") as direct, \
                 patch.object(self.channel, "get_chat_info", new_callable=AsyncMock) as lookup, \
                 patch("netizen_cli.channel.entry.is_status_command", return_value=True), \
                 patch.object(self.app, "_handle_status_command", new_callable=AsyncMock) as handler:
                await self.app.handle_card_action(SimpleNamespace(chat_id="oc_target", message_id="om_card", action=object()))
                handler.assert_awaited_once()
                direct.assert_not_called()
                lookup.assert_not_called()


class EnableGroupChannelTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = await self.enterAsyncContext(channel_fixture())
        self.app = self.fixture.app
        self.channel = self.fixture.channel
        self.path = self.fixture.project_root / "config.yaml"
        self.users = AdminUsers(self.fixture.project_root, "cli_test")
        self.users.add_initial("ou_admin")
        self.app._is_instance_admin = lambda app_id, open_id: self.users.get_membership(
            AdminIdentity(app_id, open_id)) is not None
        self.configure()

    def configure(self, mode="only", ids=()):
        rule = GroupAccessRule(mode, tuple(ids))
        self.path.write_text(yaml.safe_dump({"groupAccess": rule.to_dict(), "projects": {}}))
        self.editor = ConfigFileEditor(ConfigFileSnapshot.read(self.path))
        self.app._group_access = GroupAccessService(rule, config_editor=self.editor)
        return self.app._group_access

    def message(self, text="/enable", **kwargs):
        return FakeMessage(text, **{"message_id": "om_enable", "sender_id": "ou_admin",
                                   "chat_id": "oc_group", "chat_type": "group", **kwargs})

    async def test_admin_enables_group_in_both_modes_and_duplicate_does_not_write(self):
        for mode in ("all", "only"):
            with self.subTest(mode=mode):
                service = self.configure(mode, ("oc_saved",))
                await self.app.handle_message(self.message())
                self.assertEqual(service.snapshot()[0], GroupAccessRule(mode, ("oc_group", "oc_saved")))
                self.assertEqual(yaml.safe_load(self.path.read_text())["groupAccess"], service.snapshot()[0].to_dict())
                self.assertIn("已将本群加入名单" if mode == "all" else "已启用本群", self.channel.replies[-1][1])
                snapshot = service.snapshot()
                with patch.object(self.editor, "update_section") as write:
                    await self.app.handle_message(self.message())
                    write.assert_not_called()
                self.assertEqual(snapshot, service.snapshot())
                self.assertIn("本群已在名单中" if mode == "all" else "本群已启用", self.channel.replies[-1][1])
                if mode == "all":
                    self.assertIn("切换为限制模式后生效", self.channel.replies[-1][1])
        self.assertEqual(self.fixture.runtime.submit_calls, [])
        self.assertEqual(self.channel.chat_info_calls, [])

    async def test_non_admin_never_enables_even_when_already_listed_or_sender_alias_matches(self):
        for mode in ("all", "only"):
            for listed in (False, True):
                with self.subTest(mode=mode, listed=listed):
                    service = self.configure(mode, ("oc_group",) if listed else ())
                    before = service.snapshot()
                    message = self.message(sender_id="ou_other")
                    message.sender_id = "ou_admin"  # Only the actual sender open_id grants membership.
                    await self.app.handle_message(message)
                    self.assertEqual(before, service.snapshot())
                    self.assertEqual(self.channel.replies[-1][1], "只有实例管理员可以启用本群。")

    async def test_membership_is_exact_app_and_invalid_identity_or_unavailable_store_fails_closed(self):
        before = self.app._group_access.snapshot()
        self.app._app_id = "cli_other"
        await self.app.handle_message(self.message())
        self.assertEqual(self.channel.replies[-1][1], "只有实例管理员可以启用本群。")
        self.app._app_id = "cli_test"
        for sender in ("", "user_id", None):
            with self.subTest(sender=sender), self.assertLogs("netizen_cli.channel_app", level="WARNING"):
                await self.app.handle_message(self.message(sender_id=sender))
                self.assertIn("暂时无法确认管理员身份", self.channel.replies[-1][1])
        self.users._unavailable()
        with self.assertLogs("netizen_cli.channel_app", level="WARNING"):
            await self.app.handle_message(self.message())
        self.assertIn("暂时无法确认管理员身份", self.channel.replies[-1][1])
        self.app._is_instance_admin = None
        with self.assertLogs("netizen_cli.channel_app", level="WARNING"):
            await self.app.handle_message(self.message())
        self.assertEqual(before, self.app._group_access.snapshot())
        self.assertIn("暂时无法确认管理员身份", self.channel.replies[-1][1])

    async def test_direct_and_direct_topic_need_no_membership_or_write(self):
        for mode in ("all", "only"):
            self.configure(mode)
            for topic in (None, "omt_direct"):
                with patch.object(self.users, "get_membership") as check, patch.object(self.editor, "update_section") as write:
                    await self.app.handle_message(self.message(chat_type="p2p", thread_id=topic, sender_id="ou_other"))
                    check.assert_not_called()
                    write.assert_not_called()
                self.assertEqual(self.channel.replies[-1][1], "单聊无需启用。")

    async def test_blocked_topic_enable_skips_preparation_routes_and_runtime_then_next_input_is_admitted(self):
        before = self.fixture.store._connection.total_changes
        message = self.message(thread_id="omt_topic", reply_id="om_quote")
        with ExitStack() as stack:
            forbidden = [stack.enter_context(patch.object(self.app, method, new_callable=AsyncMock))
                         for method in ("_prompt", "_side_message")]
            forbidden.extend(stack.enter_context(patch.object(self.app._input_preparer, method, new_callable=AsyncMock))
                             for method in ("prepare", "prepare_catch_up"))
            forbidden.append(stack.enter_context(patch.object(self.app, "_side_route")))
            forbidden.append(stack.enter_context(patch.object(self.fixture.store.schedules, "pending_route")))
            await self.app.handle_message(message)
            for call in forbidden:
                call.assert_not_called()
        self.assertEqual(self.fixture.store._connection.total_changes, before)
        self.assertEqual(self.fixture.runtime.submit_calls, [])
        self.assertEqual(self.channel.fetch_inbound_calls, [])
        self.assertEqual(self.channel.fetch_quoted_calls, [])
        self.assertEqual(self.channel.download_resource_calls, [])
        self.assertIs(self.channel.reply_targets[-1], message)
        self.assertEqual(self.app._group_access.snapshot()[0].chat_ids, ("oc_group",))
        # Both the topic and the main line share the newly admitted chat ID.
        with patch.object(self.app, "_prompt", new_callable=AsyncMock) as prompt:
            for topic in ("omt_topic", None):
                await self.app.handle_message(self.message("work", thread_id=topic))
            self.assertEqual(prompt.await_count, 2)

    async def test_blocked_unmentioned_enable_is_silent_and_other_commands_get_notice(self):
        before = self.app._group_access.snapshot()
        with patch.object(self.users, "get_membership") as check:
            await self.app.handle_message(self.message(mentioned_bot=False))
            self.assertEqual(self.channel.replies, [])
            for text in ("/new", "/stop", "/unknown", "//enable", "/enabled", "please /enable"):
                await self.app.handle_message(self.message(text))
                self.assertEqual(self.channel.replies[-1][1], GROUP_ACCESS_NOTICE)
            check.assert_not_called()
        self.assertEqual(before, self.app._group_access.snapshot())

    async def test_arguments_are_rejected_in_both_modes_without_membership_or_write(self):
        for mode in ("all", "only"):
            self.configure(mode)
            with patch.object(self.users, "get_membership") as check, patch.object(self.editor, "update_section") as write:
                await self.app.handle_message(self.message("/enable other"))
                check.assert_not_called()
                write.assert_not_called()
            self.assertIn("不接受参数", self.channel.replies[-1][1])

    async def test_external_edit_limit_and_write_failure_do_not_change_rule_or_config(self):
        for failure in ("external", "limit", "write"):
            with self.subTest(failure=failure), ExitStack() as stack:
                service = self.configure(ids=tuple(f"oc_{i}" for i in range(1000)) if failure == "limit" else ())
                if failure == "external":
                    self.path.write_text(self.path.read_text() + "# external change\n")
                elif failure == "write":
                    stack.enter_context(patch("netizen_cli.admin.port_config.os.replace", side_effect=OSError("read only")))
                before, content = service.snapshot(), self.path.read_bytes()
                with self.assertLogs("netizen_cli.channel_app", level="WARNING"):
                    await self.app.handle_message(self.message())
                self.assertEqual(before, service.snapshot())
                self.assertEqual(content, self.path.read_bytes())
                self.assertTrue(self.channel.replies[-1][1].endswith("，请稍后重试或在 Admin 中启用。"))
                if failure == "limit":
                    self.assertIn("1000", self.channel.replies[-1][1])

    async def test_admin_save_still_checks_revision_and_enable_write_runs_off_event_loop(self):
        service = self.app._group_access
        before = service.snapshot()[1]
        event_loop_thread = threading.get_ident()
        write_threads = []
        update = self.editor.update_section
        def recording_update(*args):
            write_threads.append(threading.get_ident())
            return update(*args)
        with patch.object(self.editor, "update_section", side_effect=recording_update):
            await self.app.handle_message(self.message())
        self.assertEqual(len(write_threads), 1)
        self.assertNotEqual(write_threads[0], event_loop_thread)
        with self.assertRaises(ConfigConflict):
            service.update(GroupAccessRule(), expected_revision=before)
        old = service.snapshot()[1]
        service.update(GroupAccessRule("all", ("oc_admin_edit",)), expected_revision=old)
        rule, added = service.enable_chat("oc_new")
        self.assertTrue(added)
        self.assertEqual(rule, GroupAccessRule("all", ("oc_admin_edit", "oc_new")))


    async def test_enables_from_same_entry_snapshot_preserve_both_chats(self):
        service = self.app._group_access
        submit = self.app._management._blocking_io.submit
        arrived = asyncio.Event()
        release = asyncio.Event()

        async def delayed_submit(*args, **kwargs):
            arrived.set()
            await release.wait()
            return await submit(*args, **kwargs)

        with patch.object(self.app._management._blocking_io, "submit", side_effect=delayed_submit):
            second = asyncio.create_task(self.app.handle_message(self.message(chat_id="oc_second")))
            try:
                await asyncio.wait_for(arrived.wait(), 2)
                # Both command entries have seen the original admission snapshot.
                with patch.object(self.app._management._blocking_io, "submit", side_effect=submit):
                    await self.app.handle_message(self.message())
                release.set()
                await asyncio.wait_for(second, 2)
            finally:
                release.set()
                await second
        self.assertEqual(service.snapshot()[0], GroupAccessRule("only", ("oc_group", "oc_second")))
        self.assertEqual([text for _, text in self.channel.replies], ["已启用本群，之后的消息会正常执行。"] * 2)

    async def test_admin_save_between_enable_entry_and_write_preserves_latest_rule(self):
        service = self.app._group_access
        submit = self.app._management._blocking_io.submit

        async def admin_save_then_submit(*args, **kwargs):
            service.update(GroupAccessRule("all", ("oc_admin_edit",)),
                           expected_revision=service.snapshot()[1])
            return await submit(*args, **kwargs)

        with patch.object(self.app._management._blocking_io, "submit", side_effect=admin_save_then_submit):
            await self.app.handle_message(self.message())
        self.assertEqual(service.snapshot()[0], GroupAccessRule("all", ("oc_admin_edit", "oc_group")))
        self.assertEqual(yaml.safe_load(self.path.read_text())["groupAccess"], service.snapshot()[0].to_dict())
        self.assertIn("已将本群加入名单", self.channel.replies[-1][1])

    async def test_enable_timeout_reports_unknown_without_retrying_or_cancelling_write(self):
        service = self.app._group_access
        executor = self.app._management._blocking_io
        submit = executor.submit
        enable = service.enable_chat
        release = threading.Event()
        loop = asyncio.get_running_loop()

        def blocked_enable(chat_id):
            if not release.wait(5):
                raise TimeoutError("test did not release write")
            return enable(chat_id)

        async def expire_submit(*args, deadline, **kwargs):
            self.assertAlmostEqual(deadline - loop.time(), 10, delta=1)
            return await submit(*args, deadline=loop.time(), **kwargs)

        with patch.object(service, "enable_chat", side_effect=blocked_enable) as write, \
             patch.object(executor, "submit", side_effect=expire_submit) as submission, \
             self.assertLogs("netizen_cli.channel_app", level="WARNING") as logs:
            try:
                await asyncio.wait_for(self.app.handle_message(self.message()), 2)
                self.assertEqual(self.channel.replies[-1][1],
                                 "暂时无法保存群聊准入配置，请稍后重试或在 Admin 中启用。")
                self.assertEqual(logs.records[-1].error_type, "BlockingIOResultUnknown")
                self.assertEqual(executor.in_flight, 1)
                self.assertEqual(service.snapshot()[0].chat_ids, ())
                submission.assert_awaited_once()
            finally:
                release.set()
                await executor.drain(deadline=loop.time() + 5)
            write.assert_called_once_with("oc_group")
        self.assertEqual(service.snapshot()[0].chat_ids, ("oc_group",))

    async def test_enable_in_allowed_side_topic_uses_whole_chat_in_both_modes(self):
        async with side_channel_fixture() as fixture:
            fixture.app._is_instance_admin = self.app._is_instance_admin
            source = self.message("/side", message_id="om_side")
            fixture.binding_for(source)
            fixture.queue_promoted_topic(chat_id="oc_group", root_id="om_root",
                                         seed_id="om_seed", topic_id="omt_side")
            await fixture.app.handle_message(source)
            for mode in ("all", "only"):
                with self.subTest(mode=mode):
                    service = self.configure(mode, ("oc_group",) if mode == "only" else ())
                    fixture.app._group_access = service
                    with patch.object(fixture.app, "_prompt", new_callable=AsyncMock) as ordinary, \
                         patch.object(fixture.app, "_side_prompt", new_callable=AsyncMock) as side:
                        await fixture.app.handle_message(self.message(thread_id="omt_side"))
                        ordinary.assert_not_called()
                        side.assert_not_called()
                    self.assertEqual(service.snapshot()[0], GroupAccessRule(mode, ("oc_group",)))
                    self.assertIn("已将本群加入名单" if mode == "all" else "本群已启用", fixture.channel.replies[-1][1])
