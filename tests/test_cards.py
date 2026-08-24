from __future__ import annotations

import json
import unittest
from pathlib import Path

from lark_channel import OutboundCard

from netizen.cards import (
    ArchivedSessionCardItem,
    CardActionError,
    SessionCardItem,
    SettingsCardActionError,
    TurnFileCardLimitError,
    archive_binding_card,
    archived_sessions_card,
    sessions_card,
    binding_configured_card,
    config_card,
    decode_button_action,
    decode_card_form,
    decode_settings_form,
    decode_turn_file_action,
    delete_binding_card,
    fetched_card_topic_id,
    goal_card,
    new_binding_card,
    rename_binding_card,
    side_topic_card,
    scope_from_fetched_card,
    settings_card,
    turn_files_card,
    turn_files_card_from_manifest,
)
from netizen.bindings import BindingTurnSettings, SideTopicState
from netizen.domain import (
    CardControlName,
    FeishuScope,
    SettingsSection,
    ScopeKind,
    TurnFileActionName,
    TurnFileManifestItem,
)
from netizen.projects import Project
from netizen.model_settings import (
    EffortOption,
    ModelCatalog,
    ModelOption,
    ServiceTierOption,
    TurnModelSettings,
)
from netizen.sdk_gap_adapter import GoalSnapshot, GoalStatus
from netizen.turn_files import TurnFile


class CardCodecTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scope = FeishuScope(
            "cli_test",
            "oc_group",
            ScopeKind.TOPIC,
            "omt_topic",
        )
        self.value = {
            "v": 3,
            "intent": "settings.refresh",
            "chat_id": "oc_group",
            "scope_kind": "topic",
            "topic_id": "omt_topic",
            "settings_section": "projects",
        }

    def decode(self, value=None):
        return decode_button_action(
            app_id="cli_test",
            message_id="om_card",
            callback_chat_id="oc_group",
            sender_id="ou_user",
            tag="button",
            value=self.value if value is None else value,
        )

    def test_topic_button_round_trips_to_typed_intent(self) -> None:
        intent = self.decode()
        self.assertEqual(intent.name, CardControlName.REFRESH_SETTINGS)
        self.assertEqual(intent.scope, self.scope)
        self.assertEqual(intent.settings_section, SettingsSection.PROJECTS)

    def test_direct_and_group_buttons_round_trip_without_topic_field(self) -> None:
        for kind in (ScopeKind.DIRECT, ScopeKind.GROUP):
            value = {
                "v": 3,
                "intent": "settings.refresh",
                "chat_id": "oc_chat",
                "scope_kind": kind.value,
                "settings_section": "projects",
            }
            with self.subTest(kind=kind):
                intent = decode_button_action(
                    app_id="cli_test",
                    message_id="om_card",
                    callback_chat_id="oc_chat",
                    sender_id="ou_user",
                    tag="button",
                    value=value,
                )
                self.assertEqual(intent.scope.kind, kind)
                self.assertIsNone(intent.scope.topic_id)
                self.assertEqual(intent.settings_section, SettingsSection.PROJECTS)

    def test_goal_button_round_trips_exact_binding(self) -> None:
        value = {
            "v": 3,
            "intent": "goal.pause",
            "chat_id": "oc_group",
            "scope_kind": "topic",
            "topic_id": "omt_topic",
            "binding_id": "binding:v1:binding-123",
        }

        intent = self.decode(value)

        self.assertEqual(intent.name, CardControlName.GOAL_PAUSE)
        self.assertEqual(intent.binding_id, "binding-123")
        with self.assertRaises(CardActionError):
            self.decode({**value, "native_path": "/tmp/forbidden"})

    def test_side_close_round_trips_exact_side_and_topic(self) -> None:
        value = {
            "v": 3,
            "intent": "side.close",
            "chat_id": "oc_group",
            "scope_kind": "topic",
            "topic_id": "omt_topic",
            "side_id": "side:v1:side-123",
        }

        intent = self.decode(value)

        self.assertEqual(intent.name, CardControlName.SIDE_CLOSE)
        self.assertEqual(intent.side_id, "side-123")
        self.assertEqual(intent.scope, self.scope)
        for mutation in (
            {**value, "side_id": "side-123"},
            {**value, "native_thread_id": "forbidden"},
            {
                **value,
                "scope_kind": "group",
                "topic_id": None,
            },
        ):
            with self.subTest(mutation=mutation), self.assertRaises(CardActionError):
                self.decode(mutation)

    def test_lifecycle_buttons_and_rename_form_are_strict_typed_controls(self) -> None:
        for raw_intent, expected in (
            ("binding.archive", CardControlName.ARCHIVE_BINDING),
            ("binding.delete", CardControlName.DELETE_BINDING),
            ("binding.unarchive", CardControlName.UNARCHIVE_BINDING),
        ):
            with self.subTest(raw_intent=raw_intent):
                intent = self.decode(
                    {
                        "v": 3,
                        "intent": raw_intent,
                        "chat_id": "oc_group",
                        "scope_kind": "topic",
                        "topic_id": "omt_topic",
                        "binding_id": "binding:v1:binding-123",
                    }
                )
                self.assertEqual(intent.name, expected)
                self.assertEqual(intent.binding_id, "binding-123")

        renamed = decode_card_form(
            scope=self.scope,
            message_id="om_card",
            sender_id="ou_user",
            tag="button",
            form_value={
                "rename_name_v1__binding-123": "  Release   review  ",
            },
        )
        self.assertEqual(renamed.name, CardControlName.RENAME_BINDING)
        self.assertEqual(renamed.binding_id, "binding-123")
        self.assertEqual(renamed.thread_name, "Release review")
        with self.assertRaises(CardActionError):
            decode_card_form(
                scope=self.scope,
                message_id="om_card",
                sender_id="ou_user",
                tag="button",
                form_value={
                    "rename_name_v1__binding-123": "Release",
                    "extra": "forbidden",
                },
            )

    def test_activate_binding_round_trips_exact_binding_id(self) -> None:
        value = {
            "v": 3,
            "intent": "binding.activate",
            "chat_id": "oc_group",
            "scope_kind": "topic",
            "topic_id": "omt_topic",
            "binding_id": "binding:v1:binding-123",
        }
        intent = self.decode(value)
        self.assertEqual(intent.name, CardControlName.ACTIVATE_BINDING)
        self.assertEqual(intent.binding_id, "binding-123")
        self.assertIsNone(intent.page)

        for mutation in (
            {**value, "extra": "field"},
            {**value, "page": 0},
            {**value, "binding_id": "binding-123"},
        ):
            with self.subTest(mutation=mutation), self.assertRaises(CardActionError):
                self.decode(mutation)

    def test_exact_archive_round_trips_binding_pointer_and_page(self) -> None:
        value = {
            "v": 3,
            "intent": "binding.archive.exact",
            "chat_id": "oc_group",
            "scope_kind": "topic",
            "topic_id": "omt_topic",
            "binding_id": "binding:v1:binding-123",
            "expected_active_binding_id": "binding:v1:binding-current",
            "page": 2,
        }

        intent = self.decode(value)

        self.assertEqual(intent.name, CardControlName.ARCHIVE_EXACT_BINDING)
        self.assertEqual(intent.binding_id, "binding-123")
        self.assertEqual(intent.expected_active_binding_id, "binding-current")
        self.assertEqual(intent.page, 2)

        without_current = self.decode(
            {**value, "expected_active_binding_id": None}
        )
        self.assertIsNone(without_current.expected_active_binding_id)

        for mutation in (
            {key: item for key, item in value.items() if key != "page"},
            {**value, "extra": "field"},
            {**value, "page": -1},
            {**value, "page": True},
            {**value, "expected_active_binding_id": "binding-current"},
            {**value, "binding_id": "binding-123"},
        ):
            with self.subTest(mutation=mutation), self.assertRaises(CardActionError):
                self.decode(mutation)

    def test_sessions_page_round_trips_page_and_rejects_extras(self) -> None:
        value = {
            "v": 3,
            "intent": "sessions.page",
            "chat_id": "oc_group",
            "scope_kind": "topic",
            "topic_id": "omt_topic",
            "page": 1,
        }
        intent = self.decode(value)
        self.assertEqual(intent.name, CardControlName.SESSIONS_PAGE)
        self.assertEqual(intent.page, 1)

        for mutation in (
            {**value, "extra": "field"},
            {**value, "page": -1},
            {**value, "page": "1"},
        ):
            with self.subTest(mutation=mutation), self.assertRaises(CardActionError):
                self.decode(mutation)

    def test_button_decoder_rejects_version_extras_chat_and_topic_mismatch(self) -> None:
        for mutation in (
            {**self.value, "v": 2},
            {**self.value, "extra": "field"},
            {**self.value, "chat_id": "oc_other"},
            {**self.value, "topic_id": None},
        ):
            with self.subTest(mutation=mutation), self.assertRaises(CardActionError):
                self.decode(mutation)

        with self.assertRaisesRegex(CardActionError, "版本已过期"):
            self.decode({**self.value, "v": 2})

    def test_v3_turn_file_actions_are_expired(self) -> None:
        common = {
            "v": 3,
            "chat_id": "oc_group",
            "scope_kind": "topic",
            "topic_id": "omt_topic",
            "binding_id": "binding:v1:binding-123",
            "turn_id": "turn:v1:turn-123",
        }
        for value in (
            {**common, "intent": "turn-file.page", "page": 2},
            {
                **common,
                "intent": "turn-file.send",
                "file_ref": "turn-file:v1:" + "a" * 64,
            },
        ):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(CardActionError, "已过期"),
            ):
                decode_turn_file_action(
                    app_id="cli_test",
                    message_id="om_card",
                    callback_chat_id="oc_group",
                    sender_id="ou_user",
                    tag="button",
                    value=value,
                )

    def test_v4_turn_file_actions_carry_absolute_paths_and_full_manifest(
        self,
    ) -> None:
        common = {
            "v": 4,
            "chat_id": "oc_group",
            "scope_kind": "topic",
            "topic_id": "omt_topic",
            "binding_id": "binding:v1:binding-123",
            "turn_id": "turn:v1:turn-123",
        }
        manifest = [
            {"path": "/srv/work/report.xlsx", "label": "report.xlsx"},
            {"path": "/tmp/trend.png", "label": "生成图片/trend.png"},
        ]
        page = decode_turn_file_action(
            app_id="cli_test",
            message_id="om_card",
            callback_chat_id="oc_group",
            sender_id="ou_user",
            tag="button",
            value={
                **common,
                "intent": "turn-file.page",
                "page": 1,
                "files": manifest,
                "answer": "analysis complete",
            },
        )
        sent = decode_turn_file_action(
            app_id="cli_test",
            message_id="om_card",
            callback_chat_id="oc_group",
            sender_id="ou_user",
            tag="button",
            value={
                **common,
                "intent": "turn-file.send",
                "path": "/srv/work/report.xlsx",
            },
        )

        self.assertEqual(page.page, 1)
        self.assertEqual(page.answer, "analysis complete")
        self.assertEqual(
            page.files,
            (
                TurnFileManifestItem("/srv/work/report.xlsx", "report.xlsx"),
                TurnFileManifestItem(
                    "/tmp/trend.png",
                    "生成图片/trend.png",
                ),
            ),
        )
        self.assertEqual(sent.path, "/srv/work/report.xlsx")

        invalid = (
            {**common, "intent": "turn-file.send", "path": "relative.txt"},
            {
                **common,
                "intent": "turn-file.send",
                "path": "/tmp/file.txt",
                "file_ref": "turn-file:v1:" + "a" * 64,
            },
            {
                **common,
                "intent": "turn-file.page",
                "page": 0,
                "files": [],
                "answer": "done",
            },
            {
                **common,
                "intent": "turn-file.page",
                "page": 0,
                "files": [manifest[0], manifest[0]],
                "answer": "done",
            },
            {
                **common,
                "intent": "turn-file.page",
                "page": 0,
                "files": [{"path": "/tmp/a", "label": "a", "extra": 1}],
                "answer": "done",
            },
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(CardActionError):
                decode_turn_file_action(
                    app_id="cli_test",
                    message_id="om_card",
                    callback_chat_id="oc_group",
                    sender_id="ou_user",
                    tag="button",
                    value=value,
                )

    def test_settings_forms_are_strict_and_typed(self) -> None:
        intent = decode_settings_form(
            scope=self.scope,
            message_id="om_card",
            sender_id="ou_user",
            tag="button",
            form_value={
                "project_alias": "demo",
                "project_mode": "create",
                "project_path": "",
            },
        )
        self.assertEqual(intent.name, CardControlName.REGISTER_PROJECT)
        self.assertEqual(intent.settings_section, SettingsSection.PROJECTS)
        self.assertEqual(intent.project_alias, "demo")
        self.assertIsNone(intent.project_path)
        self.assertTrue(intent.create_directory)

        managed = decode_settings_form(
            scope=self.scope,
            message_id="om_card",
            sender_id="ou_user",
            tag="button",
            form_value={
                "project_manage_target": "project:v1:test:3",
                "project_manage_operation": "disable",
            },
        )
        self.assertEqual(managed.name, CardControlName.SET_PROJECT_ENABLED)
        self.assertEqual(managed.settings_section, SettingsSection.PROJECTS)
        self.assertEqual(managed.project_alias, "test")
        self.assertEqual(managed.expected_revision, 3)
        self.assertFalse(managed.enabled)

        with self.assertRaises(SettingsCardActionError):
            decode_settings_form(
                scope=self.scope,
                message_id="om_card",
                sender_id="ou_user",
                tag="button",
                form_value={
                    "project_alias": "demo",
                    "project_mode": "create",
                    "unexpected": "value",
                },
            )

        for target, operation in (
            ("project:v1:none:1", "disable"),
            ("project:v1:test:0", "enable"),
            ("project:v1:test:3", "toggle"),
        ):
            with self.subTest(target=target, operation=operation):
                with self.assertRaises(SettingsCardActionError):
                    decode_settings_form(
                        scope=self.scope,
                        message_id="om_card",
                        sender_id="ou_user",
                        tag="button",
                        form_value={
                            "project_manage_target": target,
                            "project_manage_operation": operation,
                        },
                    )

    def test_binding_settings_forms_are_strict_and_typed(self) -> None:
        created = decode_card_form(
            scope=self.scope,
            message_id="om_card",
            sender_id="ou_user",
            tag="button",
            form_value={
                "new_project": "project:v1:test:3",
                "new_model": "future-model",
                "new_effort": "ultra",
                "new_speed": "priority-v2",
            },
        )
        self.assertEqual(created.name, CardControlName.CREATE_BINDING)
        self.assertEqual(created.project_alias, "test")
        self.assertEqual(created.expected_revision, 3)
        self.assertEqual(created.model_id, "future-model")
        self.assertEqual(created.effort_id, "ultra")
        self.assertEqual(created.service_tier_id, "priority-v2")

        configured = decode_card_form(
            scope=self.scope,
            message_id="om_card",
            sender_id="ou_user",
            tag="button",
            form_value={
                "config_model": (
                    "config-model:v2:"
                    "11111111-0000-0000-0000-000000000001:7:"
                    "ZnV0dXJlLW1vZGVs"
                ),
                "config_effort": "ultra",
                "config_speed": "default",
            },
        )
        self.assertEqual(configured.name, CardControlName.CONFIGURE_BINDING)
        self.assertEqual(
            configured.binding_id,
            "11111111-0000-0000-0000-000000000001",
        )
        self.assertEqual(configured.expected_settings_revision, 7)
        self.assertEqual(configured.model_id, "future-model")
        self.assertEqual(configured.effort_id, "ultra")
        self.assertEqual(configured.service_tier_id, "default")

        invalid_forms = (
            {
                "new_project": "project:v1:test:3",
                "new_model": "future-model",
                "new_effort": "ultra",
                "new_speed": "priority-v2",
                "unexpected": "value",
            },
            {
                "new_project": "project:v1:test:3",
            },
            {
                "new_project": "project:v1:test:3",
                # Old cards with the removed mode field must fail closed.
                "new_settings_mode": "custom-v2",
                "new_model": "future-model",
                "new_effort": "ultra",
                "new_speed": "priority-v2",
            },
            {
                "config_model": "config-model:v2:not_valid!:1:broken",
                "config_effort": "ultra",
                "config_speed": "default",
            },
            {
                "config_model": (
                    "config-model:v2:"
                    "11111111-0000-0000-0000-000000000001:1:"
                    "ZnV0dXJlLW1vZGVs"
                ),
                "config_effort": "ultra",
            },
            {
                "new_project": "project:v1:test:3",
                "new_model": "future-model",
                "new_effort": "ultra",
                "new_speed": "priority-v2",
                "config_model": (
                    "config-model:v2:"
                    "11111111-0000-0000-0000-000000000001:1:"
                    "ZnV0dXJlLW1vZGVs"
                ),
                "config_effort": "ultra",
                "config_speed": "default",
            },
        )
        for form_value in invalid_forms:
            with self.subTest(form_value=form_value), self.assertRaises(
                CardActionError
            ):
                decode_card_form(
                    scope=self.scope,
                    message_id="om_card",
                    sender_id="ou_user",
                    tag="button",
                    form_value=form_value,
                )

    def test_fetched_card_recovers_direct_group_and_topic_scope(self) -> None:
        def payload(thread_id=None):
            return {
                "data": {
                    "items": [
                        {"chat_id": "oc_group", "thread_id": thread_id}
                    ]
                }
            }

        topic = scope_from_fetched_card(
            app_id="cli_test",
            callback_chat_id="oc_group",
            fetched_message=payload("omt_topic"),
            chat_type=None,
        )
        group = scope_from_fetched_card(
            app_id="cli_test",
            callback_chat_id="oc_group",
            fetched_message=payload(),
            chat_type="group",
        )
        direct = scope_from_fetched_card(
            app_id="cli_test",
            callback_chat_id="oc_group",
            fetched_message=payload(),
            chat_type="p2p",
        )
        self.assertEqual(topic.kind, ScopeKind.TOPIC)
        self.assertEqual(group.kind, ScopeKind.GROUP)
        self.assertEqual(direct.kind, ScopeKind.DIRECT)
        self.assertEqual(
            fetched_card_topic_id(
                callback_chat_id="oc_group",
                fetched_message=payload("omt_topic"),
            ),
            "omt_topic",
        )
        self.assertIsNone(
            fetched_card_topic_id(
                callback_chat_id="oc_group",
                fetched_message=payload(),
            )
        )


class CardRendererTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scope = FeishuScope("cli_test", "oc_group", ScopeKind.GROUP)
        self.projects = (
            Project("none", Path("/home/user"), True, 1),
            Project("test", Path("/home/user/test"), True, 2),
            Project("off", Path("/home/user/off"), False, 4),
        )
        self.catalog = ModelCatalog(
            models=(
                ModelOption(
                    id="future-model",
                    model="gpt-future-codex",
                    display_name="GPT Future",
                    description="future model",
                    is_default=True,
                    default_effort_id="ultra",
                    default_service_tier_id="priority-v2",
                    efforts=(
                        EffortOption("low", "low", "low-wire"),
                        EffortOption("ultra", "ultra", "ultra-wire"),
                    ),
                    service_tiers=(
                        ServiceTierOption(
                            "priority-v2",
                            "Fast v2",
                            "future fast tier",
                        ),
                    ),
                ),
            )
        )

    def test_turn_files_card_keeps_answer_and_paged_v4_manifest_together(self) -> None:
        files = tuple(
            TurnFile(
                display_path=f"src/file-{index:02}.txt",
                resolved_path=Path(f"/server/private/src/file-{index:02}.txt"),
                size=1024 + index,
                media_kind="image" if index == 0 else "file",
            )
            for index in range(18)
        )

        first = turn_files_card(
            scope=self.scope,
            binding_id="binding-123",
            turn_id="turn-123",
            final_response="**分析完成**：退款率下降。",
            files=files,
        )
        last = turn_files_card(
            scope=self.scope,
            binding_id="binding-123",
            turn_id="turn-123",
            final_response="**分析完成**：退款率下降。",
            files=files,
            page=2,
        )

        serialized = json.dumps(first.card, ensure_ascii=False)
        visible = "\n".join(
            element["content"] for element in _elements(first.card, "markdown")
        )
        self.assertEqual(first.card["schema"], "2.0")
        self.assertEqual(first.card["header"]["template"], "green")
        self.assertIn("分析完成", visible)
        self.assertIn("src/file-00.txt", visible)
        self.assertIn("src/file-07.txt", visible)
        self.assertNotIn("src/file-08.txt", visible)
        self.assertNotIn("/server/private", visible)
        self.assertNotIn("查看差异", serialized)
        self.assertNotIn("预览", serialized)
        self.assertIn("发送原图到话题", serialized)
        self.assertIn("发送文件到话题", serialized)
        values = [
            behavior["value"]
            for button in _elements(first.card, "button")
            for behavior in button.get("behaviors", ())
        ]
        self.assertEqual(len(values), 9)
        self.assertTrue(all(value["v"] == 4 for value in values))
        send_values = [value for value in values if value["intent"] == "turn-file.send"]
        page_values = [value for value in values if value["intent"] == "turn-file.page"]
        self.assertEqual(len(send_values), 8)
        self.assertTrue(all(value["path"].startswith("/server/private/") for value in send_values))
        self.assertEqual(len(page_values), 1)
        self.assertEqual(len(page_values[0]["files"]), 18)
        self.assertEqual(page_values[0]["answer"], "**分析完成**：退款率下降。")
        self.assertTrue(
            all(
                value["binding_id"] == "binding:v1:binding-123"
                for value in values
            )
        )
        self.assertTrue(
            all(value["turn_id"] == "turn:v1:turn-123" for value in values)
        )
        self.assertLess(len(_tagged_elements(first.card)), 200)
        element_ids = {
            element.get("element_id")
            for element in _elements(first.card, "column_set")
            if "element_id" in element
        }
        self.assertEqual(element_ids, {"turnanswerv1", "turnfilesv4"})
        answer_block = next(
            element
            for element in _elements(first.card, "column_set")
            if element.get("element_id") == "turnanswerv1"
        )
        self.assertEqual(answer_block["background_style"], "default")

        last_visible = "\n".join(
            element["content"] for element in _elements(last.card, "markdown")
        )
        self.assertIn("src/file-16.txt", last_visible)
        self.assertIn("src/file-17.txt", last_visible)
        self.assertNotIn("下一页", json.dumps(last.card, ensure_ascii=False))
        self.assertIn("回到第一页", json.dumps(last.card, ensure_ascii=False))

    def test_v4_page_update_preserves_answer_and_marks_missing_files(self) -> None:
        existing = Path("/tmp/report-existing.txt")
        existing.write_text("ok", encoding="utf-8")
        try:
            files = tuple(
                TurnFile(
                    display_path=f"result-{index:02}.txt",
                    resolved_path=(
                        existing
                        if index == 8
                        else Path(f"/tmp/result-missing-{index:02}.txt")
                    ),
                    size=2,
                    media_kind="file",
                )
                for index in range(10)
            )
            original = turn_files_card(
                scope=self.scope,
                binding_id="binding-123",
                turn_id="turn-123",
                final_response="**不可替换的回答**",
                files=files,
            )
            next_value = next(
                value
                for button in _elements(original.card, "button")
                for behavior in button.get("behaviors", ())
                if (value := behavior["value"])["intent"] == "turn-file.page"
            )
            decoded = decode_turn_file_action(
                app_id="cli_test",
                message_id="om_card",
                callback_chat_id="oc_group",
                sender_id="ou_user",
                tag="button",
                value=next_value,
            )
            updated = turn_files_card_from_manifest(
                scope=decoded.scope,
                binding_id=decoded.binding_id,
                turn_id=decoded.turn_id,
                final_response=decoded.answer or "",
                manifest=decoded.files,
                page=decoded.page or 0,
            )
        finally:
            existing.unlink(missing_ok=True)

        visible = "\n".join(
            element["content"] for element in _elements(updated.card, "markdown")
        )
        self.assertIn("不可替换的回答", visible)
        self.assertIn("result-08.txt", visible)
        self.assertIn("result-09.txt", visible)
        self.assertIn("文件当前不可用", visible)
        self.assertEqual(len(_elements(updated.card, "button")), 2)

    def test_v4_manifest_capacity_is_complete_through_five_hundred(self) -> None:
        for count in (100, 500):
            with self.subTest(count=count):
                files = tuple(
                    TurnFile(
                        display_path=f"src/file-{index:04}.txt",
                        resolved_path=Path(f"/srv/work/src/file-{index:04}.txt"),
                        size=index,
                        media_kind="file",
                    )
                    for index in range(count)
                )
                card = turn_files_card(
                    scope=self.scope,
                    binding_id="binding-123",
                    turn_id="turn-123",
                    final_response="done",
                    files=files,
                )
                next_value = next(
                    value
                    for button in _elements(card.card, "button")
                    for behavior in button.get("behaviors", ())
                    if (value := behavior["value"])["intent"]
                    == "turn-file.page"
                )
                self.assertEqual(len(next_value["files"]), count)
                self.assertLess(len(_tagged_elements(card.card)), 200)

        too_many = tuple(
            TurnFile(
                display_path=f"file-{index}.txt",
                resolved_path=Path(f"/srv/work/file-{index}.txt"),
                size=0,
                media_kind="file",
            )
            for index in range(1000)
        )
        with self.assertRaisesRegex(TurnFileCardLimitError, "未截断"):
            turn_files_card(
                scope=self.scope,
                binding_id="binding-123",
                turn_id="turn-123",
                final_response="done",
                files=too_many,
            )
        with self.assertRaisesRegex(TurnFileCardLimitError, "bytes"):
            turn_files_card(
                scope=self.scope,
                binding_id="binding-123",
                turn_id="turn-123",
                final_response="x" * 55_000,
                files=too_many[:1],
            )

        short = too_many[:8]
        long_later_page = tuple(
            TurnFile(
                display_path="label-" + "x" * 1000 + str(index),
                resolved_path=Path("/" + "p" * 3500 + f"/{index}"),
                size=0,
                media_kind="file",
            )
            for index in range(8)
        )
        with self.assertRaisesRegex(TurnFileCardLimitError, "bytes"):
            turn_files_card(
                scope=self.scope,
                binding_id="binding-123",
                turn_id="turn-123",
                final_response="done",
                files=short + long_later_page,
            )

    def test_settings_card_is_v2_shared_and_uses_callback_behaviors(self) -> None:
        outbound = settings_card(
            scope=self.scope,
            projects=self.projects,
            project_root="/home/user/projects",
        )
        self.assertIsInstance(outbound, OutboundCard)
        self.assertEqual(outbound.card["schema"], "2.0")
        self.assertTrue(outbound.card["config"]["update_multi"])
        serialized = str(outbound.card)
        self.assertIn("当前 Scope 的参与者均可操作", serialized)
        self.assertIn("Project 对整个 Netizen 实例共享", serialized)
        self.assertIn("project_manage_v1", serialized)
        self.assertIn("project_create_v1", serialized)
        self.assertIn("test · 已启用", serialized)
        self.assertIn("off · 已停用", serialized)
        self.assertNotIn(str(self.projects[1].cwd), serialized)
        self.assertNotIn(str(self.projects[2].cwd), serialized)
        buttons = _elements(outbound.card, "button")
        self.assertTrue(buttons)
        callback_buttons = [button for button in buttons if "behaviors" in button]
        submit_buttons = [
            button for button in buttons if button.get("form_action_type") == "submit"
        ]
        self.assertEqual(len(callback_buttons), 2)
        self.assertEqual(len(submit_buttons), 2)
        self.assertTrue(all("value" not in button for button in callback_buttons))
        values = [button["behaviors"][0]["value"] for button in callback_buttons]
        self.assertTrue(all("topic_id" not in value for value in values))
        self.assertTrue(all(value["v"] == 3 for value in values))
        self.assertTrue(all(value["settings_section"] == "projects" for value in values))
        manage = next(
            form
            for form in _elements(outbound.card, "form")
            if form["name"] == "project_manage_v1"
        )
        target = next(
            element
            for element in manage["elements"]
            if element.get("name") == "project_manage_target"
        )
        self.assertEqual(
            [option["value"] for option in target["options"]],
            ["project:v1:test:2", "project:v1:off:4"],
        )
        manage_submit = next(
            button
            for button in submit_buttons
            if button["name"] == "project_manage_submit_v1"
        )
        self.assertIn("confirm", manage_submit)
        self.assertIn(
            "状态会立即更新",
            manage_submit["confirm"]["text"]["content"],
        )

    def test_project_forms_share_settings_card_and_use_native_form_contract(self) -> None:
        outbound = settings_card(
            scope=self.scope,
            projects=self.projects,
            project_root="/home/user/projects",
        )
        forms = _elements(outbound.card, "form")
        self.assertEqual(
            {form["name"] for form in forms},
            {"project_manage_v1", "project_create_v1"},
        )
        create = next(form for form in forms if form["name"] == "project_create_v1")
        submit = next(
            element
            for element in create["elements"]
            if element.get("form_action_type") == "submit"
        )
        self.assertNotIn("behaviors", submit)
        self.assertEqual(submit["name"], "project_submit_v1")

    def test_project_count_does_not_grow_settings_card_body(self) -> None:
        many = (self.projects[0],) + tuple(
            Project(
                f"project_{index}",
                Path(f"/home/user/project_{index}"),
                index % 2 == 0,
                index + 1,
            )
            for index in range(50)
        )
        small = settings_card(
            scope=self.scope,
            projects=self.projects,
            project_root="/home/user/projects",
        ).card
        large = settings_card(
            scope=self.scope,
            projects=many,
            project_root="/home/user/projects",
        ).card

        self.assertEqual(
            len(small["body"]["elements"]),
            len(large["body"]["elements"]),
        )
        manage = next(
            form
            for form in _elements(large, "form")
            if form["name"] == "project_manage_v1"
        )
        target = next(
            element
            for element in manage["elements"]
            if element.get("name") == "project_manage_target"
        )
        self.assertEqual(len(target["options"]), 50)

    def test_new_card_has_one_model_settings_form_and_no_mode_or_task(self) -> None:
        outbound = new_binding_card(
            scope=self.scope,
            projects=tuple(project for project in self.projects if project.enabled),
            catalog=self.catalog,
        )
        serialized = str(outbound.card)
        self.assertIn("none · /home/user", serialized)
        self.assertIn("test · /home/user/test", serialized)
        self.assertNotIn("off · /home/user/off", serialized)
        self.assertIn("new_binding_v4", serialized)
        self.assertNotIn("快速新建", serialized)
        self.assertNotIn("下一条真实任务", serialized)
        self.assertNotIn("配置方式", serialized)
        self.assertNotIn("仅自定义时生效", serialized)

        form = next(
            item
            for item in _elements(outbound.card, "form")
            if item["name"] == "new_binding_v4"
        )
        fields = {
            item["name"]: item
            for item in form["elements"]
            if "name" in item
        }
        self.assertNotIn("new_settings_mode", fields)
        self.assertEqual(fields["new_model"]["initial_option"], "future-model")
        self.assertEqual(
            [option["value"] for option in fields["new_effort"]["options"]],
            ["low", "ultra"],
        )
        self.assertEqual(
            [option["value"] for option in fields["new_speed"]["options"]],
            ["default", "priority-v2"],
        )
        self.assertEqual(fields["new_effort"]["initial_option"], "ultra")
        self.assertEqual(
            fields["new_speed"]["initial_option"],
            "priority-v2",
        )
        self.assertNotIn("new_prompt", fields)
        self.assertEqual(len(_elements(outbound.card, "form")), 1)
        self.assertNotIn("credits", serialized.lower())
        self.assertNotIn("cost", serialized.lower())
        self.assertNotIn("费用", serialized)

    def test_config_card_targets_exact_binding_and_uses_live_catalog_options(
        self,
    ) -> None:
        outbound = config_card(
            scope=self.scope,
            binding_id="11111111-0000-0000-0000-000000000001",
            short_id="11111111",
            project_alias="test",
            settings_revision=7,
            turn_settings=BindingTurnSettings(
                "future-model",
                "low",
                "default",
            ),
            catalog=self.catalog,
        )
        form = next(
            item
            for item in _elements(outbound.card, "form")
            if item["name"] == "binding_config_v4"
        )
        fields = {
            item["name"]: item
            for item in form["elements"]
            if "name" in item
        }
        self.assertEqual(
            fields["config_model"]["options"][0]["value"],
            "config-model:v2:"
            "11111111-0000-0000-0000-000000000001:7:"
            "ZnV0dXJlLW1vZGVs",
        )
        self.assertEqual(
            fields["config_model"]["initial_option"],
            "config-model:v2:"
            "11111111-0000-0000-0000-000000000001:7:"
            "ZnV0dXJlLW1vZGVs",
        )
        self.assertNotIn("config_binding", fields)
        self.assertNotIn("config_settings_mode", fields)
        self.assertEqual(fields["config_effort"]["initial_option"], "low")
        self.assertEqual(fields["config_speed"]["initial_option"], "default")
        self.assertNotIn("config_prompt", fields)
        self.assertIn("不会启动任务", str(outbound.card))
        self.assertNotIn("目标会话", str(outbound.card))
        self.assertNotIn("配置方式", str(outbound.card))
        self.assertNotIn("继承 Codex", str(outbound.card))

    def test_configured_success_card_shows_speed_name_not_protocol_value(self) -> None:
        settings = TurnModelSettings(
            model_id="future-model",
            model="gpt-future-codex",
            effort_id="ultra",
            effort="ultra-wire",
            service_tier_id="default",
            service_tier_name="Standard",
        )

        rendered = str(
            binding_configured_card(
                short_id="11111111",
                project_alias="test",
                settings=settings,
            ).card
        )

        self.assertIn("Standard", rendered)
        self.assertNotIn("Speed=`default`", rendered)
        self.assertNotIn("credits", rendered.lower())
        self.assertNotIn("cost", rendered.lower())

    def test_config_card_marks_stale_persistent_selection(self) -> None:
        outbound = config_card(
            scope=self.scope,
            binding_id="11111111-0000-0000-0000-000000000001",
            short_id="11111111",
            project_alias="test",
            settings_revision=3,
            turn_settings=BindingTurnSettings(
                "removed-model",
                "removed-effort",
                "removed-tier",
            ),
            catalog=self.catalog,
        )

        rendered = str(outbound.card)
        self.assertIn("不再出现在当前模型目录", rendered)
        self.assertIn("重新选择三项配置", rendered)
        self.assertNotIn("继承 Codex", rendered)

    def test_new_card_without_catalog_has_no_submittable_form(self) -> None:
        outbound = new_binding_card(
            scope=self.scope,
            projects=self.projects[:2],
            catalog=None,
            catalog_error="模型目录暂不可用。",
        )
        serialized = str(outbound.card)
        self.assertEqual(_elements(outbound.card, "form"), [])
        self.assertIn("模型目录暂不可用", serialized)
        self.assertIn("/new alias", serialized)
        self.assertNotIn("配置方式", serialized)

    def test_goal_card_uses_typed_controls_without_native_paths(self) -> None:
        goal = GoalSnapshot(
            thread_id="native-one",
            objective="ship safely",
            status=GoalStatus.ACTIVE,
            token_budget=None,
            tokens_used=10,
            time_used_seconds=2,
            created_at=1,
            updated_at=2,
        )

        outbound = goal_card(
            scope=self.scope,
            binding_id="11111111-0000-0000-0000-000000000001",
            short_id="11111111",
            project_alias="test",
            goal=goal,
            runtime_state="goal-running",
        )
        serialized = str(outbound.card)

        self.assertIn("ship safely", serialized)
        self.assertIn("goal.pause", serialized)
        self.assertIn(
            "binding:v1:11111111-0000-0000-0000-000000000001",
            serialized,
        )
        self.assertNotIn("native-one", serialized)
        self.assertNotIn("SKILL.md", serialized)

    def test_side_card_exposes_close_for_open_and_routable_creating_states(self) -> None:
        topic = FeishuScope(
            "cli_test",
            "oc_group",
            ScopeKind.TOPIC,
            "omt_side",
        )
        open_card = side_topic_card(
            scope=topic,
            side_id="side-123",
            parent_short_id="parent12",
            creator_id="ou_creator",
            created_at="2026-08-15T12:00:00Z",
            state=SideTopicState.OPEN,
        )
        serialized = str(open_card.card)
        self.assertIn("side.close", serialized)
        self.assertIn("side:v1:side-123", serialized)
        self.assertIn("omt_side", serialized)
        self.assertIn("共享 Project cwd", serialized)
        self.assertIn("服务重启后", serialized)
        self.assertIn("confirm", serialized)

        creating = side_topic_card(
            scope=topic,
            side_id="side-123",
            parent_short_id="parent12",
            creator_id="ou_creator",
            created_at="2026-08-15T12:00:00Z",
            state=SideTopicState.CREATING,
        )
        self.assertIn("side.close", str(creating.card))
        self.assertIn("取消 Side", str(creating.card))
        unrouted_creating = side_topic_card(
            scope=self.scope,
            side_id="side-123",
            parent_short_id="parent12",
            creator_id="ou_creator",
            created_at="2026-08-15T12:00:00Z",
            state=SideTopicState.CREATING,
        )
        self.assertNotIn("side.close", str(unrouted_creating.card))

        for state in (
            SideTopicState.CLOSED,
            SideTopicState.EXPIRED,
            SideTopicState.FAILED,
        ):
            with self.subTest(state=state):
                card = side_topic_card(
                    scope=topic,
                    side_id="side-123",
                    parent_short_id="parent12",
                    creator_id="ou_creator",
                    created_at="2026-08-15T12:00:00Z",
                    state=state,
                )
                self.assertNotIn("side.close", str(card.card))

    def test_lifecycle_cards_encode_exact_binding_and_destructive_confirmation(self) -> None:
        binding_id = "11111111-0000-0000-0000-000000000001"
        rename = rename_binding_card(
            scope=self.scope,
            binding_id=binding_id,
            short_id="11111111",
            project_alias="test",
            current_title="Release review",
        )
        archive = archive_binding_card(
            scope=self.scope,
            binding_id=binding_id,
            short_id="11111111",
            project_alias="test",
            title="Release review",
        )
        delete = delete_binding_card(
            scope=self.scope,
            binding_id=binding_id,
            short_id="11111111",
            project_alias="test",
            title="Release review",
        )
        archived = archived_sessions_card(
            scope=self.scope,
            sessions=(
                ArchivedSessionCardItem(
                    binding_id=binding_id,
                    short_id="11111111",
                    project_alias="test",
                    native_thread_id="native-one",
                    title="Release review",
                ),
            ),
        )

        rename_fields = [
            item["name"]
            for item in _elements(rename.card, "input")
        ]
        self.assertEqual(rename_fields, [f"rename_name_v1__{binding_id}"])
        self.assertIn("binding.archive", str(archive.card))
        self.assertIn("确认归档当前会话", str(archive.card))
        self.assertIn("binding.delete", str(delete.card))
        self.assertIn("永久删除且无法恢复", str(delete.card))
        self.assertIn("只删除本地 Binding", str(delete.card))
        self.assertNotIn("原生 Thread", str(delete.card))
        self.assertIn("binding.unarchive", str(archived.card))
        self.assertIn("恢复并切换", str(archived.card))

    def test_sessions_card_pins_active_and_offers_activate_for_others(self) -> None:
        active = SessionCardItem(
            binding_id="11111111-0000-0000-0000-000000000001",
            short_id="11111111",
            project_alias="test",
            native_thread_id="native-one",
            title="Active work",
            state="running",
            active=True,
        )
        other = SessionCardItem(
            binding_id="22222222-0000-0000-0000-000000000002",
            short_id="22222222",
            project_alias="test",
            native_thread_id="native-two",
            title="Other work",
            state="idle",
            active=False,
        )
        lazy = SessionCardItem(
            binding_id="33333333-0000-0000-0000-000000000003",
            short_id="33333333",
            project_alias="none",
            native_thread_id=None,
            title="新会话",
            state="idle",
            active=False,
        )
        card = sessions_card(
            scope=self.scope,
            sessions=(other, lazy, active),
            page=0,
        )
        text = str(card.card)
        # Active is pinned to the top.
        self.assertLess(text.index("Active work"), text.index("Other work"))
        self.assertLess(text.index("Active work"), text.index("新会话"))
        self.assertIn("● 当前", text)
        self.assertIn("Native：native-o", text)
        self.assertIn("Native：pending", text)
        self.assertIn("切换不会停止其他会话正在运行的任务", text)
        buttons = _elements(card.card, "button")
        labels = [b["text"]["content"] for b in buttons]
        self.assertIn("设为当前", labels)
        # Active row has no activate button; only two non-active rows have it.
        self.assertEqual(labels.count("设为当前"), 2)
        # Only the idle materialized row can be archived.
        self.assertEqual(labels.count("归档"), 1)
        archive = next(b for b in buttons if b["text"]["content"] == "归档")
        archive_value = archive["behaviors"][0]["value"]
        self.assertEqual(archive_value["intent"], "binding.archive.exact")
        self.assertEqual(
            archive_value["binding_id"],
            "binding:v1:22222222-0000-0000-0000-000000000002",
        )
        self.assertEqual(
            archive_value["expected_active_binding_id"],
            "binding:v1:11111111-0000-0000-0000-000000000001",
        )
        self.assertEqual(archive_value["page"], 0)
        self.assertEqual(
            archive["confirm"]["title"]["content"],
            "确认归档此会话？",
        )
        self.assertIn("历史不会删除", archive["confirm"]["text"]["content"])

    def test_sessions_archive_is_hidden_for_non_idle_or_lazy_rows(self) -> None:
        sessions = (
            SessionCardItem(
                binding_id="11111111-0000-0000-0000-000000000001",
                short_id="11111111",
                project_alias="test",
                native_thread_id="native-one",
                title="Idle current",
                state="idle",
                active=True,
            ),
            SessionCardItem(
                binding_id="22222222-0000-0000-0000-000000000002",
                short_id="22222222",
                project_alias="test",
                native_thread_id="native-two",
                title="Running",
                state="running",
                active=False,
            ),
            SessionCardItem(
                binding_id="33333333-0000-0000-0000-000000000003",
                short_id="33333333",
                project_alias="test",
                native_thread_id="native-three",
                title="Goal",
                state="goal-active",
                active=False,
            ),
            SessionCardItem(
                binding_id="44444444-0000-0000-0000-000000000004",
                short_id="44444444",
                project_alias="test",
                native_thread_id=None,
                title="Lazy",
                state="idle",
                active=False,
            ),
            SessionCardItem(
                binding_id="55555555-0000-0000-0000-000000000005",
                short_id="55555555",
                project_alias="test",
                native_thread_id="native-five",
                title="Compacting",
                state="compacting",
                active=False,
            ),
            SessionCardItem(
                binding_id="66666666-0000-0000-0000-000000000006",
                short_id="66666666",
                project_alias="test",
                native_thread_id="native-six",
                title="Archiving",
                state="archiving",
                active=False,
            ),
        )

        card = sessions_card(scope=self.scope, sessions=sessions)
        archive_buttons = [
            button
            for button in _elements(card.card, "button")
            if button["text"]["content"] == "归档"
        ]

        self.assertEqual(len(archive_buttons), 1)
        self.assertEqual(
            archive_buttons[0]["behaviors"][0]["value"]["binding_id"],
            "binding:v1:11111111-0000-0000-0000-000000000001",
        )

    def test_sessions_card_paginates_and_clamps_page(self) -> None:
        sessions = tuple(
            SessionCardItem(
                binding_id=f"{i:08d}-0000-0000-0000-000000000000",
                short_id=f"{i:08d}",
                project_alias="test",
                native_thread_id=f"native-{i}",
                title=f"Session {i}",
                state="idle",
                active=(i == 0),
            )
            for i in range(25)
        )
        card = sessions_card(scope=self.scope, sessions=sessions, page=0)
        text = str(card.card)
        self.assertIn("第 1/3 页", text)
        self.assertIn("下一页", text)
        self.assertNotIn("上一页", text)

        card = sessions_card(scope=self.scope, sessions=sessions, page=1)
        text = str(card.card)
        self.assertIn("第 2/3 页", text)
        self.assertIn("上一页", text)
        self.assertIn("下一页", text)

        # Out-of-range page is clamped to the last valid page.
        card = sessions_card(scope=self.scope, sessions=sessions, page=99)
        text = str(card.card)
        self.assertIn("第 3/3 页", text)
        self.assertIn("上一页", text)
        self.assertNotIn("下一页", text)

    def test_sessions_card_empty_state(self) -> None:
        card = sessions_card(scope=self.scope, sessions=(), page=0)
        text = str(card.card)
        self.assertIn("没有普通会话", text)
        self.assertIn("/sessions archived", text)


def _elements(value, tag: str):
    found = []
    if isinstance(value, dict):
        if value.get("tag") == tag:
            found.append(value)
        for child in value.values():
            found.extend(_elements(child, tag))
    elif isinstance(value, list):
        for child in value:
            found.extend(_elements(child, tag))
    return found


def _tagged_elements(value):
    found = []
    if isinstance(value, dict):
        if "tag" in value:
            found.append(value)
        for child in value.values():
            found.extend(_tagged_elements(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_tagged_elements(child))
    return found


if __name__ == "__main__":
    unittest.main()
