from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from lark_channel import OutboundCard

from netizen.cards import (
    ArchivedSessionCardItem,
    CardActionError,
    SessionCardItem,
    SettingsCardActionError,
    TurnFileCardLimitError,
    activity_step_display,
    archive_binding_card,
    archived_sessions_card,
    archived_sessions_delete_binding_card,
    sessions_card,
    sessions_delete_binding_card,
    binding_created_card,
    binding_configured_card,
    config_card,
    decode_button_action,
    decode_card_form,
    decode_settings_form,
    decode_turn_file_action,
    delete_binding_card,
    fetched_card_topic_id,
    goal_card,
    goal_generation,
    new_binding_card,
    rename_binding_card,
    side_topic_card,
    scope_from_fetched_card,
    settings_card,
    turn_files_card,
    turn_files_card_from_manifest,
    turn_progress_card,
    turn_progress_card_from_manifest,
    reply_card,
    reply_card_from_manifest,
)
from netizen.bindings import (
    BindingTaskFeedback,
    BindingTurnSettings,
    SideTopicState,
)
from netizen.domain import (
    CardControlName,
    FeishuScope,
    MentionContextMode,
    ReplyCardActivityModule,
    ReplyCardFileItem,
    ReplyCardFilesModule,
    ReplyCardGoalModule,
    ReplyCardProjection,
    ReplyCardResultModule,
    SettingsSection,
    ScopeKind,
    TurnActivityManifestEntry,
    TurnCommentaryManifestEntry,
    TurnFileActionName,
    TurnFileManifestItem,
    TurnProgressManifest,
    TurnProgressManifestStep,
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
from netizen.turn_activity import (
    TurnActivityEntrySnapshot,
    TurnActivityKind,
    TurnActivityStatus,
)
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
            "v": 4,
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
                "v": 4,
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
            "v": 4,
            "intent": "goal.pause",
            "chat_id": "oc_group",
            "scope_kind": "topic",
            "topic_id": "omt_topic",
            "binding_id": "binding:v1:binding-123",
            "goal_generation": "a" * 43,
            "expected_goal_status": "active",
        }

        intent = self.decode(value)

        self.assertEqual(intent.name, CardControlName.GOAL_PAUSE)
        self.assertEqual(intent.binding_id, "binding-123")
        self.assertEqual(intent.goal_generation, "a" * 43)
        self.assertEqual(intent.expected_goal_status, "active")
        with self.assertRaises(CardActionError):
            self.decode(
                {
                    key: item
                    for key, item in value.items()
                    if key != "goal_generation"
                }
            )
        with self.assertRaises(CardActionError):
            self.decode({**value, "expected_goal_status": "paused"})
        with self.assertRaises(CardActionError):
            self.decode({**value, "native_path": "/tmp/forbidden"})

    def test_goal_end_accepts_complete_snapshot_that_was_not_auto_cleared(self) -> None:
        intent = self.decode(
            {
                "v": 4,
                "intent": "goal.clear",
                "chat_id": "oc_group",
                "scope_kind": "topic",
                "topic_id": "omt_topic",
                "binding_id": "binding:v1:binding-123",
                "goal_generation": "a" * 43,
                "expected_goal_status": "complete",
            }
        )

        self.assertEqual(intent.name, CardControlName.GOAL_CLEAR)
        self.assertEqual(intent.expected_goal_status, "complete")

    def test_side_close_round_trips_exact_side_and_topic(self) -> None:
        value = {
            "v": 4,
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
                        "v": 4,
                        "intent": raw_intent,
                        "chat_id": "oc_group",
                        "scope_kind": "topic",
                        "topic_id": "omt_topic",
                        "binding_id": "binding:v1:binding-123",
                    }
                )
                self.assertEqual(intent.name, expected)
                self.assertEqual(intent.binding_id, "binding-123")

        materialized_delete = self.decode(
            {
                "v": 4,
                "intent": "binding.delete",
                "chat_id": "oc_group",
                "scope_kind": "topic",
                "topic_id": "omt_topic",
                "binding_id": "binding:v1:binding-123",
                "expected_native_thread_id": "native-thread:v1:thread-123",
            }
        )
        self.assertEqual(
            materialized_delete.expected_native_thread_id,
            "thread-123",
        )
        with self.assertRaises(CardActionError):
            self.decode(
                {
                    "v": 4,
                    "intent": "binding.delete",
                    "chat_id": "oc_group",
                    "scope_kind": "topic",
                    "topic_id": "omt_topic",
                    "binding_id": "binding:v1:binding-123",
                    "expected_native_thread_id": "thread-123",
                }
            )

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
            "v": 4,
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

    def test_exact_archive_round_trips_binding_and_page(self) -> None:
        value = {
            "v": 4,
            "intent": "binding.archive.exact",
            "chat_id": "oc_group",
            "scope_kind": "topic",
            "topic_id": "omt_topic",
            "binding_id": "binding:v1:binding-123",
            "page": 2,
        }

        intent = self.decode(value)

        self.assertEqual(intent.name, CardControlName.ARCHIVE_EXACT_BINDING)
        self.assertEqual(intent.binding_id, "binding-123")
        self.assertEqual(intent.page, 2)

        for mutation in (
            {key: item for key, item in value.items() if key != "page"},
            {**value, "extra": "field"},
            {**value, "page": -1},
            {**value, "page": True},
            {**value, "expected_active_binding_id": None},
            {**value, "expected_activity_revision": 7},
            {**value, "expected_turn_id": "turn:v1:turn-123"},
            {**value, "binding_id": "binding-123"},
        ):
            with self.subTest(mutation=mutation), self.assertRaises(CardActionError):
                self.decode(mutation)

    def test_exact_delete_actions_round_trip_exact_thread(self) -> None:
        for raw_intent, expected_name in (
            (
                "binding.delete.exact.prepare",
                CardControlName.PREPARE_EXACT_DELETE_BINDING,
            ),
            ("binding.delete.exact", CardControlName.DELETE_EXACT_BINDING),
        ):
            value = {
                "v": 4,
                "intent": raw_intent,
                "chat_id": "oc_group",
                "scope_kind": "topic",
                "topic_id": "omt_topic",
                "binding_id": "binding:v1:binding-123",
                "expected_native_thread_id": "native-thread:v1:thread-123",
                "page": 2,
            }

            with self.subTest(raw_intent=raw_intent):
                intent = self.decode(value)
                self.assertEqual(intent.name, expected_name)
                self.assertEqual(intent.binding_id, "binding-123")
                self.assertEqual(intent.expected_native_thread_id, "thread-123")
                self.assertEqual(intent.page, 2)
                lazy = self.decode(
                    {
                        **value,
                        "expected_native_thread_id": None,
                    }
                )
                self.assertIsNone(lazy.expected_native_thread_id)

                for mutation in (
                    {key: item for key, item in value.items() if key != "page"},
                    {
                        key: item
                        for key, item in value.items()
                        if key != "expected_native_thread_id"
                    },
                    {**value, "extra": "field"},
                    {**value, "page": True},
                    {**value, "page": -1},
                    {**value, "expected_native_thread_id": "thread-123"},
                    {**value, "expected_activity_revision": 7},
                    {**value, "expected_turn_id": "turn:v1:turn-123"},
                ):
                    with self.assertRaises(CardActionError):
                        self.decode(mutation)

    def test_exact_stop_recheck_and_archived_delete_actions_are_strict(
        self,
    ) -> None:
        runtime_common = {
            "v": 4,
            "chat_id": "oc_group",
            "scope_kind": "topic",
            "topic_id": "omt_topic",
            "binding_id": "binding:v1:binding-123",
            "expected_active_binding_id": "binding:v1:binding-current",
            "expected_activity_revision": 9,
            "expected_turn_id": "turn:v1:turn-123",
            "page": 2,
        }
        stop = self.decode(
            {**runtime_common, "intent": "binding.stop.exact"}
        )
        recheck = self.decode(
            {**runtime_common, "intent": "binding.turn.recheck"}
        )
        self.assertEqual(stop.name, CardControlName.STOP_EXACT_BINDING)
        self.assertEqual(recheck.name, CardControlName.RECHECK_EXACT_TURN)
        self.assertEqual(recheck.expected_activity_revision, 9)
        self.assertEqual(recheck.expected_turn_id, "turn-123")
        with self.assertRaises(CardActionError):
            self.decode(
                {
                    **runtime_common,
                    "intent": "binding.turn.recheck",
                    "expected_turn_id": None,
                }
            )

        archived_common = {
            "v": 4,
            "chat_id": "oc_group",
            "scope_kind": "topic",
            "topic_id": "omt_topic",
            "binding_id": "binding:v1:binding-123",
            "expected_native_thread_id": "native-thread:v1:thread-123",
        }
        for raw_intent, expected in (
            (
                "binding.delete.archived.prepare",
                CardControlName.PREPARE_ARCHIVED_DELETE_BINDING,
            ),
            (
                "binding.delete.archived",
                CardControlName.DELETE_ARCHIVED_BINDING,
            ),
        ):
            with self.subTest(raw_intent=raw_intent):
                intent = self.decode(
                    {**archived_common, "intent": raw_intent}
                )
                self.assertEqual(intent.name, expected)
                self.assertEqual(intent.expected_native_thread_id, "thread-123")
                with self.assertRaises(CardActionError):
                    self.decode(
                        {
                            **archived_common,
                            "intent": raw_intent,
                            "page": 0,
                        }
                    )

    def test_sessions_page_round_trips_page_and_rejects_extras(self) -> None:
        value = {
            "v": 4,
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
            {**self.value, "v": 3},
            {**self.value, "extra": "field"},
            {**self.value, "chat_id": "oc_other"},
            {**self.value, "topic_id": None},
        ):
            with self.subTest(mutation=mutation), self.assertRaises(CardActionError):
                self.decode(mutation)

        with self.assertRaisesRegex(CardActionError, "版本已过期"):
            self.decode({**self.value, "v": 3})

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
            {
                "path": "/srv/work/report.xlsx",
                "label": "report.xlsx",
                "a": 12,
                "d": 3,
            },
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
                "a": 12,
                "d": 3,
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
        self.assertEqual((page.additions, page.deletions), (12, 3))
        self.assertEqual(
            page.files,
            (
                TurnFileManifestItem(
                    "/srv/work/report.xlsx",
                    "report.xlsx",
                    12,
                    3,
                ),
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
            {
                **common,
                "intent": "turn-file.page",
                "page": 0,
                "files": [{"path": "/tmp/a", "label": "a", "a": 1}],
                "answer": "done",
            },
            {
                **common,
                "intent": "turn-file.page",
                "page": 0,
                "files": manifest,
                "answer": "done",
                "a": 1,
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

    def test_binding_settings_forms_decode_explicit_and_context_revisions(
        self,
    ) -> None:
        created = decode_card_form(
            scope=self.scope,
            message_id="om_card",
            sender_id="ou_user",
            tag="button",
            form_value={
                "new_project": "project:v1:test:3",
                "new_context_mode": "context-mode:v1:catch-up",
                "new_model": "new-model:v1:explicit:ZnV0dXJlLW1vZGVs",
                "new_effort": "ultra",
                "new_speed": "priority-v2",
                "new_task_reactions": "task-feedback:v2:on",
                "new_progress_card": "task-feedback:v2:off",
            },
        )
        self.assertEqual(created.name, CardControlName.CREATE_BINDING)
        self.assertEqual(created.project_alias, "test")
        self.assertEqual(created.expected_revision, 3)
        self.assertEqual(created.model_id, "future-model")
        self.assertEqual(created.effort_id, "ultra")
        self.assertEqual(created.service_tier_id, "priority-v2")
        self.assertTrue(created.reaction_pulse_enabled)
        self.assertFalse(created.progress_card_enabled)
        self.assertEqual(
            created.message_context_mode,
            MentionContextMode.CATCH_UP,
        )

        configured = decode_card_form(
            scope=self.scope,
            message_id="om_card",
            sender_id="ou_user",
            tag="button",
            form_value={
                "config_model": (
                    "config-model:v4:"
                    "11111111-0000-0000-0000-000000000001:7:11:13:"
                    "explicit:ZnV0dXJlLW1vZGVs"
                ),
                "config_context_mode": "context-mode:v1:current-only",
                "config_effort": "ultra",
                "config_speed": "default",
                "config_task_reactions": "task-feedback:v2:off",
                "config_progress_card": "task-feedback:v2:on",
            },
        )
        self.assertEqual(configured.name, CardControlName.CONFIGURE_BINDING)
        self.assertEqual(
            configured.binding_id,
            "11111111-0000-0000-0000-000000000001",
        )
        self.assertEqual(configured.expected_settings_revision, 7)
        self.assertEqual(configured.expected_context_revision, 11)
        self.assertEqual(configured.feedback_revision, 13)
        self.assertEqual(configured.model_id, "future-model")
        self.assertEqual(configured.effort_id, "ultra")
        self.assertEqual(configured.service_tier_id, "default")
        self.assertFalse(configured.reaction_pulse_enabled)
        self.assertTrue(configured.progress_card_enabled)
        self.assertEqual(
            configured.message_context_mode,
            MentionContextMode.CURRENT_ONLY,
        )

    def test_binding_settings_forms_decode_minimal_and_catalog_inherit(
        self,
    ) -> None:
        minimal = decode_card_form(
            scope=self.scope,
            message_id="om_card",
            sender_id="ou_user",
            tag="button",
            form_value={
                "new_project": "project:v1:test:3",
                "new_context_mode": "context-mode:v1:current-only",
                "new_model": "new-model:v1:inherit",
                "new_task_reactions": "task-feedback:v2:off",
                "new_progress_card": "task-feedback:v2:off",
            },
        )
        catalog = decode_card_form(
            scope=self.scope,
            message_id="om_card",
            sender_id="ou_user",
            tag="button",
            form_value={
                "new_project": "project:v1:test:3",
                "new_context_mode": "context-mode:v1:catch-up",
                "new_model": "new-model:v1:inherit",
                "new_effort": "rendered-effort",
                "new_speed": "rendered-speed",
                "new_task_reactions": "task-feedback:v2:on",
                "new_progress_card": "task-feedback:v2:on",
            },
        )
        configured = decode_card_form(
            scope=self.scope,
            message_id="om_card",
            sender_id="ou_user",
            tag="button",
            form_value={
                "config_model": (
                    "config-model:v4:"
                    "11111111-0000-0000-0000-000000000001:7:11:13:inherit"
                ),
                "config_context_mode": "context-mode:v1:catch-up",
                "config_task_reactions": "task-feedback:v2:off",
                "config_progress_card": "task-feedback:v2:on",
            },
        )

        for intent in (minimal, catalog, configured):
            self.assertIsNone(intent.model_id)
            self.assertIsNone(intent.effort_id)
            self.assertIsNone(intent.service_tier_id)
        self.assertEqual(
            minimal.message_context_mode,
            MentionContextMode.CURRENT_ONLY,
        )
        self.assertEqual(catalog.message_context_mode, MentionContextMode.CATCH_UP)
        self.assertEqual(configured.expected_settings_revision, 7)
        self.assertEqual(configured.expected_context_revision, 11)
        self.assertEqual(configured.feedback_revision, 13)
        self.assertEqual(configured.message_context_mode, MentionContextMode.CATCH_UP)

    def test_binding_settings_forms_reject_old_mixed_and_unbounded_shapes(
        self,
    ) -> None:
        invalid_forms = (
            {
                "new_project": "project:v1:test:3",
                "new_model": "new-model:v1:inherit",
                "new_effort": "ultra",
                "new_speed": "priority-v2",
                "unexpected": "value",
            },
            {
                "new_project": "project:v1:test:3",
                "new_model": "future-model",
            },
            {
                "new_project": "project:v1:test:3",
                "new_settings_mode": "custom-v2",
                "new_model": "new-model:v1:explicit:ZnV0dXJlLW1vZGVs",
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
                    "config-model:v3:"
                    "11111111-0000-0000-0000-000000000001:1:1:"
                    "explicit:ZnV0dXJlLW1vZGVs"
                ),
                "config_effort": "ultra",
            },
            {
                "new_project": "project:v1:test:3",
                "new_model": "new-model:v1:explicit:ZnV0dXJlLW1vZGVs",
                "new_effort": "ultra",
                "new_speed": "priority-v2",
                "config_model": (
                    "config-model:v3:"
                    "11111111-0000-0000-0000-000000000001:1:1:inherit"
                ),
            },
            {
                "new_project": "project:v1:test:3",
                "new_model": "new-model:v1:inherit",
                "new_effort": "x" * 129,
                "new_speed": "default",
            },
            {
                "new_project": "project:v1:test:3",
                "new_model": "new-model:v1:explicit:ZnV0dXJlLW1vZGVs",
            },
            {
                "new_project": "project:v1:test:3",
                "new_model": "new-model:v1:inherit",
                "new_task_reactions": "task-feedback:v1:off",
                "new_progress_card": "task-feedback:v1:off",
            },
            {
                "new_project": "project:v1:test:3",
                "new_model": "new-model:v1:inherit",
                "new_task_reactions": True,
                "new_progress_card": "task-feedback:v2:off",
            },
            {
                "config_model": (
                    "config-model:v4:"
                    "11111111-0000-0000-0000-000000000001:1:1:1:inherit"
                ),
                "config_task_reactions": "task-feedback:v2:off",
                "config_progress_card": "on",
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
                additions=index + 1,
                deletions=index,
            )
            for index in range(18)
        )

        first = turn_files_card(
            scope=self.scope,
            binding_id="binding-123",
            turn_id="turn-123",
            final_response="**分析完成**：退款率下降。",
            files=files,
            additions=38,
            deletions=14,
        )
        last = turn_files_card(
            scope=self.scope,
            binding_id="binding-123",
            turn_id="turn-123",
            final_response="**分析完成**：退款率下降。",
            files=files,
            page=2,
            additions=38,
            deletions=14,
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
        self.assertNotIn("1.0 KB", serialized)
        self.assertNotIn("发送原图到话题", serialized)
        self.assertNotIn("发送文件到话题", serialized)
        self.assertIn("点击“发送”后", visible)
        self.assertIn("+38", visible)
        self.assertIn("-14", visible)
        self.assertNotIn("+1", visible)
        self.assertIn("+2", visible)
        self.assertIn("-1", visible)
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
        self.assertEqual(page_values[0]["a"], 38)
        self.assertEqual(page_values[0]["d"], 14)
        self.assertEqual(
            set(page_values[0]["files"][0]),
            {"path", "label"},
        )
        self.assertEqual(
            page_values[0]["files"][1]["a"],
            2,
        )
        self.assertEqual(
            page_values[0]["files"][1]["d"],
            1,
        )
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
        self.assertIn("+17", last_visible)
        self.assertIn("src/file-17.txt", last_visible)
        self.assertNotIn("下一页", json.dumps(last.card, ensure_ascii=False))
        self.assertIn("回到第一页", json.dumps(last.card, ensure_ascii=False))

    def test_running_progress_card_is_expanded_bounded_and_projection_only(
        self,
    ) -> None:
        snapshot = SimpleNamespace(
            state=SimpleNamespace(value="running"),
            steer_count=2,
            plan_available=True,
            plan_generated=True,
            plan_may_be_stale=True,
            steps=tuple(
                SimpleNamespace(
                    step=(
                        "检查当前实现 " + "x" * 300
                        if index == 0
                        else (
                            "调用 Authorization: Bearer "
                            "SECRET_PLAN_VALUE_1234567890"
                            if index == 2
                            else (
                                "联系 alice@example.com，ETA 5m，完成 50%，"
                                "耗时 18m"
                                if index == 3
                                else f"步骤 {index}"
                            )
                        )
                    ),
                    status=SimpleNamespace(
                        value=(
                            "completed"
                            if index == 0
                            else "inProgress"
                            if index == 1
                            else "pending"
                        )
                    ),
                )
                for index in range(14)
            ),
            reasoning="SECRET_REASONING",
            tool_arguments="SECRET_ARGUMENTS",
            raw_output="SECRET_RAW_OUTPUT",
        )

        outbound = turn_progress_card(snapshot=snapshot)

        self.assertEqual(outbound.card["schema"], "2.0")
        self.assertEqual(outbound.card["header"]["template"], "blue")
        self.assertEqual(
            outbound.card["body"]["elements"][0]["tag"],
            "collapsible_panel",
        )
        panel = _elements(outbound.card, "collapsible_panel")[0]
        self.assertTrue(panel["expanded"])
        self.assertEqual(panel["element_id"], "turnprogressv1")
        self.assertEqual(panel["padding"], "8px 12px 12px 12px")
        self.assertNotIn("padding", panel["header"])
        visible = "\n".join(
            item["text"]["content"]
            for item in _elements(panel, "div")
            if item.get("text", {}).get("tag") == "plain_text"
        )
        self.assertIn("状态：正在执行", visible)
        self.assertIn("已接收调整：2 次", visible)
        self.assertIn("✓ 检查当前实现", visible)
        self.assertIn("→ 步骤 1", visible)
        self.assertIn("… 另有 2 项未展示", visible)
        self.assertNotIn("步骤 12", visible)
        markdown_visible = tuple(
            item["content"] for item in _elements(panel, "markdown")
        )
        self.assertIn(
            "**任务清单**（可能尚未反映最近一次调整）",
            markdown_visible,
        )
        serialized = json.dumps(outbound.card, ensure_ascii=False)
        for forbidden in (
            "SECRET_REASONING",
            "SECRET_ARGUMENTS",
            "SECRET_RAW_OUTPUT",
            "SECRET_PLAN_VALUE",
            "alice@example.com",
            "耗时",
            "ETA",
            "%",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_activity_card_renders_bounded_commentary_and_generic_operations(
        self,
    ) -> None:
        snapshot = SimpleNamespace(
            state=SimpleNamespace(value="running"),
            steer_count=0,
            plan_available=True,
            plan_generated=False,
            plan_may_be_stale=False,
            steps=(),
            commentary=(
                TurnActivityEntrySnapshot(
                    TurnActivityKind.COMMENTARY,
                    TurnActivityStatus.COMPLETED,
                    1,
                    text="first",
                ),
                TurnActivityEntrySnapshot(
                    TurnActivityKind.COMMENTARY,
                    TurnActivityStatus.COMPLETED,
                    2,
                    text="second",
                ),
                TurnActivityEntrySnapshot(
                    TurnActivityKind.COMMENTARY,
                    TurnActivityStatus.COMPLETED,
                    3,
                    text="third",
                ),
                TurnActivityEntrySnapshot(
                    TurnActivityKind.COMMENTARY,
                    TurnActivityStatus.COMPLETED,
                    4,
                    text="checked `/Users/user/private.py`",
                ),
            ),
            operations=(
                TurnActivityEntrySnapshot(
                    TurnActivityKind.COMMAND,
                    TurnActivityStatus.IN_PROGRESS,
                    5,
                    text="搜索内容",
                ),
                TurnActivityEntrySnapshot(
                    TurnActivityKind.FILE_CHANGE,
                    TurnActivityStatus.COMPLETED,
                    6,
                    count=3,
                ),
                TurnActivityEntrySnapshot(
                    TurnActivityKind.SUBAGENT,
                    TurnActivityStatus.FAILED,
                    7,
                    count=2,
                ),
            ),
        )

        outbound = turn_progress_card(snapshot=snapshot)
        serialized = json.dumps(outbound.card, ensure_ascii=False)

        self.assertNotIn("first", serialized)
        self.assertIn("second", serialized)
        self.assertIn("third", serialized)
        self.assertIn("路径已隐藏", serialized)
        self.assertIn("搜索内容", serialized)
        self.assertIn("修改文件（3 项）", serialized)
        self.assertIn("子任务（2 项）", serialized)
        self.assertNotIn("private.py", serialized)
        self.assertEqual(serialized.count("<local_datetime"), 12)
        self.assertEqual(serialized.count("format_type='date_num'"), 6)
        self.assertEqual(serialized.count("format_type='time'"), 6)
        for timestamp in range(2, 8):
            self.assertEqual(serialized.count(f"millisecond='{timestamp}'"), 2)
        markdown_visible = tuple(
            item["content"]
            for item in _elements(outbound.card, "markdown")
        )
        self.assertIn("**最近进展**", markdown_visible)
        self.assertIn("**最近操作**", markdown_visible)
        self.assertIn("**任务清单**：Codex 尚未生成", markdown_visible)

    def test_activity_steps_filter_chinese_adjacent_sensitive_patterns(self) -> None:
        fully_hidden = (
            "使用password: correct horse battery staple",
            "调用认证：Bearer x",
            "凭据postgresql://user:pass@example.com继续",
            "读取密码：correct horse battery staple",
            "使用API密钥：foo bar baz",
            "session令牌：foo bar",
            "DB_PASSWORD=foo bar",
            "OPENAI_API_KEY=shortsecret",
            "-----BEGIN PRIVATE KEY-----abc",
        )
        for value in fully_hidden:
            with self.subTest(value=value):
                self.assertEqual(
                    activity_step_display(value),
                    "[敏感内容已隐藏]",
                )

    def test_activity_markdown_keeps_only_generated_time_tags_active(self) -> None:
        injected = (
            "**bold** [link](not-a-url) "
            "<local_datetime millisecond='999' format_type='date_num'>"
            "</local_datetime>"
        )
        snapshot = SimpleNamespace(
            state=SimpleNamespace(value="running"),
            steer_count=0,
            plan_available=True,
            plan_generated=False,
            plan_may_be_stale=False,
            steps=(),
            commentary=(
                TurnActivityEntrySnapshot(
                    TurnActivityKind.COMMENTARY,
                    TurnActivityStatus.COMPLETED,
                    100,
                    text=injected,
                ),
            ),
            operations=(
                TurnActivityEntrySnapshot(
                    TurnActivityKind.TOOL,
                    TurnActivityStatus.COMPLETED,
                    200,
                    text=injected,
                ),
            ),
        )

        outbound = turn_progress_card(snapshot=snapshot)
        markdown_elements = _elements(outbound.card, "markdown")
        rows = [
            item
            for item in markdown_elements
            if "<local_datetime" in item["content"]
        ]

        self.assertEqual(len(rows), 2)
        self.assertIn(
            "**最近进展**",
            {item["content"] for item in markdown_elements},
        )
        self.assertIn(
            "**最近操作**",
            {item["content"] for item in markdown_elements},
        )
        for timestamp, row in zip((100, 200), rows, strict=True):
            content = row["content"]
            self.assertTrue(
                content.startswith(
                    f"<local_datetime millisecond='{timestamp}' "
                    "format_type='date_num'></local_datetime> "
                    f"<local_datetime millisecond='{timestamp}' "
                    "format_type='time'></local_datetime> · "
                )
            )
            self.assertEqual(content.count("<local_datetime"), 2)
            self.assertEqual(content.count("format_type='date_num'"), 1)
            self.assertEqual(content.count("format_type='time'"), 1)
            self.assertIn(r"&lt;local\_datetime", content)
            self.assertIn("\\*\\*bold\\*\\*", content)
            self.assertIn("\\[link\\]\\(not-a-url\\)", content)

        partially_hidden = {
            "联系alice@example.com后继续": "alice@example.com",
            "使用sk-proj-abcdefghijklmnopqrstuvwx继续": (
                "sk-proj-abcdefghijklmnopqrstuvwx"
            ),
        }
        for value, forbidden in partially_hidden.items():
            with self.subTest(value=value):
                self.assertNotIn(forbidden, activity_step_display(value))

        for value, forbidden in (
            ("已完成50%", "%"),
            ("已完成 50％", "％"),
            ("当前ETA：5m", "ETA"),
            ("本步耗时 18m", "耗时"),
        ):
            with self.subTest(value=value):
                self.assertNotIn(forbidden, activity_step_display(value))

    def test_activity_markdown_preserves_commentary_layout(self) -> None:
        snapshot = SimpleNamespace(
            state=SimpleNamespace(value="running"),
            steer_count=0,
            plan_available=True,
            plan_generated=False,
            plan_may_be_stale=False,
            steps=(),
            commentary=(
                TurnActivityEntrySnapshot(
                    TurnActivityKind.COMMENTARY,
                    TurnActivityStatus.COMPLETED,
                    100,
                    text="第一行\r\n\r第二行\t缩进 **原样**",
                ),
            ),
            operations=(),
        )

        outbound = turn_progress_card(snapshot=snapshot)
        rows = [
            item["content"]
            for item in _elements(outbound.card, "markdown")
            if "<local_datetime" in item["content"]
        ]

        self.assertEqual(len(rows), 1)
        self.assertIn(
            "• 第一行\n\n第二行    缩进 \\*\\*原样\\*\\*",
            rows[0],
        )
        self.assertNotIn("�", rows[0])

    def test_progress_file_pagination_preserves_sanitized_collapsed_panel(
        self,
    ) -> None:
        secret = "foo bar baz"
        snapshot = SimpleNamespace(
            state=SimpleNamespace(value="running"),
            steer_count=1,
            plan_available=True,
            plan_generated=True,
            plan_may_be_stale=False,
            steps=(
                SimpleNamespace(
                    step=f"读取 secret: {secret} 后生成报告",
                    status=SimpleNamespace(value="completed"),
                ),
            ),
            commentary=(
                TurnActivityEntrySnapshot(
                    TurnActivityKind.COMMENTARY,
                    TurnActivityStatus.COMPLETED,
                    111,
                    text="finished review",
                ),
            ),
            operations=(
                TurnActivityEntrySnapshot(
                    TurnActivityKind.TOOL,
                    TurnActivityStatus.COMPLETED,
                    222,
                    text="github.get_file_contents",
                ),
            ),
        )
        files = tuple(
            TurnFile(
                display_path=f"result-{index:02}.txt",
                resolved_path=Path(f"/tmp/result-{index:02}.txt"),
                size=1,
                media_kind="file",
            )
            for index in range(10)
        )
        first = turn_progress_card(
            snapshot=snapshot,
            final_response="done",
            files=files,
            terminal_status="completed",
            scope=self.scope,
            binding_id="binding-123",
            turn_id="turn-123",
        )
        page_value = next(
            behavior["value"]
            for button in _elements(first.card, "button")
            for behavior in button.get("behaviors", ())
            if behavior["value"]["intent"] == "turn-file.page"
        )
        self.assertIn("progress", page_value)
        self.assertNotIn(secret, json.dumps(page_value, ensure_ascii=False))
        self.assertEqual(
            page_value["progress"]["commentary"],
            [{"text": "finished review", "event_timestamp_ms": 111}],
        )
        self.assertEqual(
            page_value["progress"]["operations"][0]["event_timestamp_ms"],
            222,
        )
        self.assertEqual(
            page_value["progress"]["operations"][0]["text"],
            "github.get_file_contents",
        )

        intent = decode_turn_file_action(
            app_id="cli_test",
            message_id="om_card",
            callback_chat_id=self.scope.chat_id,
            sender_id="ou_user",
            tag="button",
            value=page_value,
        )
        self.assertIsNotNone(intent.progress)
        assert intent.progress is not None
        tampered_value = json.loads(json.dumps(page_value))
        tampered_value["progress"]["steps"][0]["step"] = (
            'password: "correct horse battery staple"'
        )
        tampered_intent = decode_turn_file_action(
            app_id="cli_test",
            message_id="om_card",
            callback_chat_id=self.scope.chat_id,
            sender_id="ou_user",
            tag="button",
            value=tampered_value,
        )
        assert tampered_intent.progress is not None
        self.assertEqual(
            tampered_intent.progress.steps[0].step,
            "[敏感内容已隐藏]",
        )
        rebuilt = turn_progress_card_from_manifest(
            scope=intent.scope,
            binding_id=intent.binding_id,
            turn_id=intent.turn_id,
            final_response=intent.answer or "",
            manifest=intent.files,
            progress=intent.progress,
            page=intent.page or 0,
        )

        panel = _elements(rebuilt.card, "collapsible_panel")[0]
        self.assertFalse(panel["expanded"])
        serialized = json.dumps(rebuilt.card, ensure_ascii=False)
        self.assertIn("敏感内容已隐藏", serialized)
        self.assertNotIn(secret, serialized)
        self.assertIn("result-08.txt", serialized)
        self.assertIn("millisecond='111'", serialized)
        self.assertIn("millisecond='222'", serialized)
        self.assertIn("github.get", serialized)
        self.assertIn("file", serialized)

        legacy_value = json.loads(json.dumps(page_value))
        legacy_value["progress"]["commentary"] = ["finished review"]
        legacy_value["progress"]["operations"][0].pop("event_timestamp_ms")
        legacy_intent = decode_turn_file_action(
            app_id="cli_test",
            message_id="om_card",
            callback_chat_id=self.scope.chat_id,
            sender_id="ou_user",
            tag="button",
            value=legacy_value,
        )
        assert legacy_intent.progress is not None
        self.assertIsNone(
            legacy_intent.progress.commentary[0].event_timestamp_ms
        )
        self.assertIsNone(
            legacy_intent.progress.operations[0].event_timestamp_ms
        )

    def test_v4_progress_file_pages_preserve_existing_step_truncation(self) -> None:
        snapshot = SimpleNamespace(
            state=SimpleNamespace(value="running"),
            steer_count=0,
            plan_available=True,
            plan_generated=True,
            plan_may_be_stale=False,
            steps=tuple(
                SimpleNamespace(
                    step=f"legacy step {index}",
                    status=SimpleNamespace(value="completed"),
                )
                for index in range(14)
            ),
        )
        files = tuple(
            TurnFile(
                display_path=f"legacy-result-{index:02}.txt",
                resolved_path=Path(f"/tmp/legacy-result-{index:02}.txt"),
                size=1,
                media_kind="file",
            )
            for index in range(10)
        )

        first = turn_progress_card(
            snapshot=snapshot,
            final_response="done",
            files=files,
            terminal_status="completed",
            scope=self.scope,
            binding_id="binding-123",
            turn_id="turn-123",
        )
        value = next(
            behavior["value"]
            for button in _elements(first.card, "button")
            for behavior in button.get("behaviors", ())
            if behavior["value"]["intent"] == "turn-file.page"
        )
        intent = decode_turn_file_action(
            app_id="cli_test",
            message_id="om_card",
            callback_chat_id=self.scope.chat_id,
            sender_id="ou_user",
            tag="button",
            value=value,
        )
        assert intent.progress is not None
        rebuilt = turn_progress_card_from_manifest(
            scope=intent.scope,
            binding_id=intent.binding_id,
            turn_id=intent.turn_id,
            final_response=intent.answer or "",
            manifest=intent.files,
            progress=intent.progress,
            page=intent.page or 0,
        )

        self.assertNotIn(
            "项未展示",
            json.dumps(first.card, ensure_ascii=False),
        )
        self.assertNotIn(
            "项未展示",
            json.dumps(rebuilt.card, ensure_ascii=False),
        )

    def test_terminal_progress_card_collapses_and_reuses_answer_and_files(
        self,
    ) -> None:
        snapshot = SimpleNamespace(
            state=SimpleNamespace(value="running"),
            steer_count=0,
            plan_available=True,
            plan_generated=True,
            plan_may_be_stale=False,
            steps=(
                SimpleNamespace(
                    step="生成报告",
                    status=SimpleNamespace(value="completed"),
                ),
            ),
        )
        files = (
            TurnFile(
                display_path="reports/result.txt",
                resolved_path=Path("/server/private/reports/result.txt"),
                size=32,
                media_kind="file",
            ),
        )

        outbound = turn_progress_card(
            snapshot=snapshot,
            final_response="**任务完成**：报告已生成。",
            files=files,
            terminal_status="completed",
            scope=self.scope,
            binding_id="binding-123",
            turn_id="turn-123",
        )

        self.assertEqual(outbound.card["header"]["template"], "green")
        panel = _elements(outbound.card, "collapsible_panel")[0]
        self.assertFalse(panel["expanded"])
        self.assertIn("已完成", panel["header"]["title"]["content"])
        visible = "\n".join(
            item["content"] for item in _elements(outbound.card, "markdown")
        )
        self.assertIn("任务完成", visible)
        self.assertIn("reports/result.txt", visible)
        self.assertNotIn("/server/private", visible)
        element_ids = {
            item.get("element_id")
            for item in _tagged_elements(outbound.card)
            if item.get("element_id")
        }
        self.assertEqual(
            element_ids,
            {"turnprogressv1", "turnanswerv1", "turnfilesv4"},
        )
        button = _elements(outbound.card, "button")[0]
        self.assertEqual(
            button["behaviors"][0]["value"]["intent"],
            "turn-file.send",
        )

    def test_progress_card_terminal_variants_and_invalid_payloads(self) -> None:
        snapshot = SimpleNamespace(
            state=SimpleNamespace(value="stopping"),
            steer_count=0,
            plan_available=False,
            plan_generated=False,
            plan_may_be_stale=False,
            steps=(),
        )
        running = turn_progress_card(snapshot=snapshot)
        self.assertIn(
            "正在停止",
            _elements(running.card, "collapsible_panel")[0]["header"]["title"][
                "content"
            ],
        )
        snapshot.state = SimpleNamespace(value="turn-observation-unavailable")
        unavailable = turn_progress_card(snapshot=snapshot)
        self.assertIn(
            "Turn 观测不可用",
            _elements(unavailable.card, "collapsible_panel")[0]["header"]["title"][
                "content"
            ],
        )
        interrupted = turn_progress_card(
            snapshot=snapshot,
            terminal_status="interrupted",
            final_response="Codex Turn 已中断。",
        )
        self.assertEqual(interrupted.card["header"]["template"], "orange")
        self.assertFalse(
            _elements(interrupted.card, "collapsible_panel")[0]["expanded"]
        )

        with self.assertRaises(ValueError):
            turn_progress_card(snapshot=snapshot, final_response="尚未结束")
        with self.assertRaises(ValueError):
            turn_progress_card(snapshot=snapshot, collapsed=True)
        with self.assertRaises(ValueError):
            turn_progress_card(snapshot=snapshot, terminal_status="unknown")
        with self.assertRaisesRegex(TurnFileCardLimitError, "bytes"):
            turn_progress_card(
                snapshot=snapshot,
                terminal_status="completed",
                final_response="x" * 55_000,
            )
        with self.assertRaises(ValueError):
            turn_progress_card(
                snapshot=snapshot,
                terminal_status="completed",
                final_response="done",
                files=(
                    TurnFile(
                        display_path="result.txt",
                        resolved_path=Path("/tmp/result.txt"),
                        size=1,
                        media_kind="file",
                    ),
                ),
            )

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
                    additions=index + 1,
                    deletions=index,
                )
                for index in range(10)
            )
            original = turn_files_card(
                scope=self.scope,
                binding_id="binding-123",
                turn_id="turn-123",
                final_response="**不可替换的回答**",
                files=files,
                additions=55,
                deletions=45,
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
                additions=decoded.additions,
                deletions=decoded.deletions,
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
        self.assertIn("+55", visible)
        self.assertIn("-45", visible)
        self.assertIn("+9", visible)
        self.assertIn("-8", visible)
        self.assertEqual(len(_elements(updated.card, "button")), 2)

    def test_v4_manifest_capacity_is_complete_through_four_hundred(self) -> None:
        for count in (100, 400):
            with self.subTest(count=count):
                files = tuple(
                    TurnFile(
                        display_path=f"src/file-{index:04}.txt",
                        resolved_path=Path(f"/srv/work/src/file-{index:04}.txt"),
                        size=index,
                        media_kind="file",
                        additions=12,
                        deletions=3,
                    )
                    for index in range(count)
                )
                card = turn_files_card(
                    scope=self.scope,
                    binding_id="binding-123",
                    turn_id="turn-123",
                    final_response="done",
                    files=files,
                    additions=count * 12,
                    deletions=count * 3,
                )
                next_value = next(
                    value
                    for button in _elements(card.card, "button")
                    for behavior in button.get("behaviors", ())
                    if (value := behavior["value"])["intent"]
                    == "turn-file.page"
                )
                self.assertEqual(len(next_value["files"]), count)
                self.assertTrue(
                    all(
                        set(item) == {"path", "label", "a", "d"}
                        for item in next_value["files"]
                    )
                )
                self.assertLess(len(_tagged_elements(card.card)), 200)

        too_many = tuple(
            TurnFile(
                display_path=f"file-{index}.txt",
                resolved_path=Path(f"/srv/work/file-{index}.txt"),
                size=0,
                media_kind="file",
                additions=1,
                deletions=1,
            )
            for index in range(401)
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

    def test_v5_stats_manifest_fits_four_hundred_files(self) -> None:
        files = tuple(
            ReplyCardFileItem(
                path=f"/srv/work/src/file-{index:04}.txt",
                label=f"src/file-{index:04}.txt",
                size=index,
                media_kind="file",
                additions=12,
                deletions=3,
            )
            for index in range(400)
        )

        card = reply_card(
            ReplyCardProjection(
                scope=self.scope,
                result=ReplyCardResultModule("done"),
                files=ReplyCardFilesModule(
                    binding_id="binding-123",
                    turn_id="turn-123",
                    items=files,
                    additions=4_800,
                    deletions=1_200,
                ),
            )
        )

        page_value = next(
            behavior["value"]
            for button in _elements(card.card, "button")
            for behavior in button.get("behaviors", ())
            if behavior["value"]["intent"] == "turn-file.page"
        )
        self.assertEqual(len(page_value["files"]), 400)
        self.assertEqual((page_value["a"], page_value["d"]), (4_800, 1_200))
        self.assertLess(
            len(json.dumps(card.card, ensure_ascii=False).encode("utf-8")),
            55_000,
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
        self.assertTrue(all(value["v"] == 4 for value in values))
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

    def test_new_card_has_one_form_with_inherit_explicit_and_context_mode(
        self,
    ) -> None:
        outbound = new_binding_card(
            scope=self.scope,
            projects=tuple(project for project in self.projects if project.enabled),
            catalog=self.catalog,
            allow_context_mode=True,
        )
        serialized = str(outbound.card)
        self.assertIn("none · /home/user", serialized)
        self.assertIn("test · /home/user/test", serialized)
        self.assertNotIn("off · /home/user/off", serialized)
        self.assertIn("new_binding_v6", serialized)
        self.assertNotIn("快速新建", serialized)
        self.assertNotIn("下一条真实任务", serialized)
        self.assertIn("继承 Codex", serialized)
        self.assertIn("执行中表情闪烁", serialized)
        self.assertIn("任务接收、成功调整和结束时始终显示表情", serialized)
        self.assertNotIn("开启后会用表情反馈接收、运行和终态", serialized)
        self.assertIn("@ 时读取的消息范围", serialized)
        self.assertIn("机器人始终只响应", serialized)
        self.assertIn("未 @ 机器人", serialized)

        form = next(
            item
            for item in _elements(outbound.card, "form")
            if item["name"] == "new_binding_v6"
        )
        self.assertEqual(
            [item["name"] for item in form["elements"] if "name" in item],
            [
                "new_project",
                "new_model",
                "new_effort",
                "new_speed",
                "new_context_mode",
                "new_task_reactions",
                "new_progress_card",
                "new_binding_submit_v6",
            ],
        )
        fields = {
            item["name"]: item
            for item in form["elements"]
            if "name" in item
        }
        self.assertEqual(
            fields["new_model"]["initial_option"],
            "new-model:v1:explicit:ZnV0dXJlLW1vZGVs",
        )
        self.assertEqual(
            [option["value"] for option in fields["new_model"]["options"]],
            [
                "new-model:v1:inherit",
                "new-model:v1:explicit:ZnV0dXJlLW1vZGVs",
            ],
        )
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
        self.assertEqual(
            fields["new_context_mode"]["initial_option"],
            "context-mode:v1:current-only",
        )
        self.assertEqual(
            fields["new_task_reactions"]["initial_option"],
            "task-feedback:v2:off",
        )
        self.assertEqual(
            fields["new_progress_card"]["initial_option"],
            "task-feedback:v2:off",
        )
        self.assertNotIn("new_prompt", fields)
        self.assertEqual(len(_elements(outbound.card, "form")), 1)
        self.assertNotIn("credits", serialized.lower())
        self.assertNotIn("cost", serialized.lower())
        self.assertNotIn("费用", serialized)

    def test_new_card_shows_every_project_without_pagination_or_truncation(
        self,
    ) -> None:
        for count in (0, 1, 12, 13, 80):
            projects = tuple(
                Project(
                    "none" if index == 0 else f"project_{index}",
                    Path(f"/home/user/project_{index}"),
                    True,
                    index + 1,
                )
                for index in range(count)
            )
            outbound = new_binding_card(
                scope=self.scope,
                projects=projects,
                catalog=self.catalog,
                allow_context_mode=True,
            )
            with self.subTest(count=count):
                forms = _elements(outbound.card, "form")
                if count == 0:
                    self.assertEqual(forms, [])
                    continue
                form = next(
                    item for item in forms if item["name"] == "new_binding_v6"
                )
                project = next(
                    item
                    for item in form["elements"]
                    if item.get("name") == "new_project"
                )
                self.assertEqual(
                    [option["value"] for option in project["options"]],
                    [
                        f"project:v1:{item.alias}:{item.revision}"
                        for item in projects
                    ],
                )
                serialized = str(outbound.card)
                self.assertNotIn("仅显示前", serialized)
                self.assertNotIn("/new alias", serialized)
                self.assertNotIn("上一页", serialized)
                self.assertNotIn("下一页", serialized)

    def test_context_mode_field_is_explicitly_suppressible_for_p2p(self) -> None:
        direct = FeishuScope("cli_test", "oc_p2p", ScopeKind.DIRECT)
        group = new_binding_card(
            scope=self.scope,
            projects=self.projects[:2],
            catalog=self.catalog,
            allow_context_mode=True,
        )
        p2p = new_binding_card(
            scope=direct,
            projects=self.projects[:2],
            catalog=self.catalog,
            allow_context_mode=False,
        )
        p2p_config = config_card(
            scope=direct,
            binding_id="11111111-0000-0000-0000-000000000001",
            short_id="11111111",
            project_alias="test",
            settings_revision=7,
            context_revision=11,
            turn_settings=None,
            message_context_mode=MentionContextMode.CURRENT_ONLY,
            allow_context_mode=False,
            catalog=self.catalog,
        )

        self.assertIn("new_context_mode", str(group.card))
        self.assertNotIn("new_context_mode", str(p2p.card))
        self.assertNotIn("未 @ 机器人", str(p2p.card))
        self.assertNotIn("config_context_mode", str(p2p_config.card))
        p2p_form = next(
            item
            for item in _elements(p2p.card, "form")
            if item["name"] == "new_binding_v6"
        )
        p2p_fields = {
            item["name"]: item
            for item in p2p_form["elements"]
            if item.get("tag") == "select_static"
        }
        decoded = decode_card_form(
            scope=direct,
            message_id="om_card",
            sender_id="ou_user",
            tag="button",
            form_value={
                "new_project": p2p_fields["new_project"]["initial_option"],
                "new_model": "new-model:v1:inherit",
                "new_task_reactions": p2p_fields["new_task_reactions"][
                    "initial_option"
                ],
                "new_progress_card": p2p_fields["new_progress_card"][
                    "initial_option"
                ],
            },
        )
        self.assertEqual(
            decoded.message_context_mode,
            MentionContextMode.CURRENT_ONLY,
        )
        self.assertFalse(decoded.reaction_pulse_enabled)
        self.assertFalse(decoded.progress_card_enabled)

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
            context_revision=11,
            feedback_revision=13,
            message_context_mode=MentionContextMode.CATCH_UP,
            task_feedback=BindingTaskFeedback(
                reaction_pulse_enabled=True,
                progress_card_enabled=False,
            ),
            allow_context_mode=True,
        )
        form = next(
            item
            for item in _elements(outbound.card, "form")
            if item["name"] == "binding_config_v6"
        )
        self.assertEqual(
            [item["name"] for item in form["elements"] if "name" in item],
            [
                "config_model",
                "config_effort",
                "config_speed",
                "config_context_mode",
                "config_task_reactions",
                "config_progress_card",
                "binding_config_submit_v6",
            ],
        )
        fields = {
            item["name"]: item
            for item in form["elements"]
            if "name" in item
        }
        self.assertEqual(
            fields["config_model"]["options"][0]["value"],
            "config-model:v4:"
            "11111111-0000-0000-0000-000000000001:7:11:13:inherit",
        )
        self.assertEqual(
            fields["config_model"]["initial_option"],
            "config-model:v4:"
            "11111111-0000-0000-0000-000000000001:7:11:13:"
            "explicit:ZnV0dXJlLW1vZGVs",
        )
        self.assertNotIn("config_binding", fields)
        self.assertEqual(
            fields["config_context_mode"]["initial_option"],
            "context-mode:v1:catch-up",
        )
        self.assertEqual(fields["config_effort"]["initial_option"], "low")
        self.assertEqual(fields["config_speed"]["initial_option"], "default")
        self.assertEqual(
            fields["config_task_reactions"]["initial_option"],
            "task-feedback:v2:on",
        )
        self.assertEqual(
            fields["config_progress_card"]["initial_option"],
            "task-feedback:v2:off",
        )
        self.assertNotIn("config_prompt", fields)
        self.assertIn("不会启动任务", str(outbound.card))
        self.assertNotIn("目标会话", str(outbound.card))
        self.assertIn("继承 Codex", str(outbound.card))

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
                message_context_mode=MentionContextMode.CATCH_UP,
                task_feedback=BindingTaskFeedback(
                    reaction_pulse_enabled=True,
                    progress_card_enabled=True,
                ),
            ).card
        )

        self.assertIn("Standard", rendered)
        self.assertNotIn("Speed=`default`", rendered)
        self.assertNotIn("credits", rendered.lower())
        self.assertNotIn("cost", rendered.lower())
        self.assertIn("自动带上期间的群聊讨论", rendered)
        self.assertIn("执行中表情闪烁：开启", rendered)
        self.assertIn("进度卡：开启", rendered)

        inherited = str(
            binding_configured_card(
                short_id="11111111",
                project_alias="test",
                settings=None,
                message_context_mode=MentionContextMode.CURRENT_ONLY,
            ).card
        )
        self.assertIn("继承 Codex", inherited)
        self.assertIn("仅这条 @ 消息", inherited)
        self.assertIn("执行中表情闪烁：关闭", inherited)
        self.assertIn("进度卡：关闭", inherited)

        created = str(
            binding_created_card(
                short_id="22222222",
                project_alias="none",
                settings=None,
                message_context_mode=MentionContextMode.CATCH_UP,
            ).card
        )
        self.assertIn("none", created)
        self.assertIn("22222222", created)
        self.assertIn("继承 Codex", created)
        self.assertIn("自动带上期间的群聊讨论", created)
        self.assertIn("未 @ 机器人", created)

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
            context_revision=3,
            allow_context_mode=True,
        )

        rendered = str(outbound.card)
        self.assertIn("不再出现在当前模型目录", rendered)
        self.assertIn("重新选择三项配置", rendered)
        self.assertIn("继承 Codex", rendered)

    def test_new_and_config_without_catalog_have_minimal_inherit_forms(self) -> None:
        outbound = new_binding_card(
            scope=self.scope,
            projects=self.projects[:2],
            catalog=None,
            catalog_error="模型目录暂不可用。",
            allow_context_mode=True,
        )
        serialized = str(outbound.card)
        form = next(
            item
            for item in _elements(outbound.card, "form")
            if item["name"] == "new_binding_v6"
        )
        fields = {
            item.get("name")
            for item in form["elements"]
            if item.get("name")
        }
        self.assertEqual(
            fields,
            {
                "new_project",
                "new_context_mode",
                "new_model",
                "new_task_reactions",
                "new_progress_card",
                "new_binding_submit_v6",
            },
        )
        self.assertIn("模型目录暂不可用", serialized)
        self.assertIn("继承 Codex", serialized)
        self.assertNotIn("/new alias", serialized)

        configured = config_card(
            scope=self.scope,
            binding_id="11111111-0000-0000-0000-000000000001",
            short_id="11111111",
            project_alias="test",
            settings_revision=7,
            context_revision=11,
            turn_settings=BindingTurnSettings("future-model", "low", "default"),
            message_context_mode=MentionContextMode.CATCH_UP,
            allow_context_mode=True,
            catalog=None,
            catalog_error="模型目录暂不可用。",
        )
        config_form = next(
            item
            for item in _elements(configured.card, "form")
            if item["name"] == "binding_config_v6"
        )
        config_fields = {
            item.get("name")
            for item in config_form["elements"]
            if item.get("name")
        }
        self.assertEqual(
            config_fields,
            {
                "config_context_mode",
                "config_model",
                "config_task_reactions",
                "config_progress_card",
                "binding_config_submit_v6",
            },
        )
        self.assertIn("清除显式配置", str(configured.card))

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
        self.assertNotIn("**Time**", serialized)
        self.assertNotIn("耗时", serialized)
        pause = next(
            behavior["value"]
            for button in _elements(outbound.card, "button")
            for behavior in button.get("behaviors", ())
            if behavior["value"]["intent"] == "goal.pause"
        )
        self.assertEqual(pause["expected_goal_status"], "active")
        self.assertEqual(pause["goal_generation"], goal_generation(goal))
        self.assertNotIn("thread_id", pause)

        same_creation = GoalSnapshot(
            thread_id="native-one",
            objective="ship safely",
            status=GoalStatus.ACTIVE,
            token_budget=None,
            tokens_used=100,
            time_used_seconds=999,
            created_at=1,
            updated_at=999,
        )
        next_creation = GoalSnapshot(
            thread_id="native-one",
            objective="ship safely",
            status=GoalStatus.ACTIVE,
            token_budget=None,
            tokens_used=0,
            time_used_seconds=0,
            created_at=3,
            updated_at=3,
        )
        self.assertEqual(goal_generation(goal), goal_generation(same_creation))
        self.assertNotEqual(goal_generation(goal), goal_generation(next_creation))
        different_objective = GoalSnapshot(
            thread_id="native-one",
            objective="different objective",
            status=GoalStatus.ACTIVE,
            token_budget=None,
            tokens_used=0,
            time_used_seconds=0,
            created_at=goal.created_at,
            updated_at=goal.updated_at,
        )
        self.assertNotEqual(
            goal_generation(goal),
            goal_generation(different_objective),
        )

    def test_goal_card_truncates_only_the_visible_objective(self) -> None:
        exact_objective = "x" * 200
        exact_goal = GoalSnapshot(
            thread_id="native-one",
            objective=exact_objective,
            status=GoalStatus.ACTIVE,
            token_budget=None,
            tokens_used=10,
            time_used_seconds=2,
            created_at=1,
            updated_at=2,
        )
        exact = goal_card(
            scope=self.scope,
            binding_id="11111111-0000-0000-0000-000000000001",
            short_id="11111111",
            project_alias="test",
            goal=exact_goal,
            runtime_state="goal-running",
        )
        exact_visible = next(
            element["content"]
            for element in _elements(exact.card, "markdown")
            if "**Objective**" in element["content"]
        )
        self.assertIn(f"**Objective**：{exact_objective}\n", exact_visible)
        self.assertNotIn(f"{exact_objective}…", exact_visible)

        full_objective = f"{'y' * 198} \nkeep this whitespace and hidden tail"
        goal_module = ReplyCardGoalModule(
            binding_id="binding-123",
            short_id="binding1",
            project_alias="test",
            goal_generation="g" * 43,
            status="active",
            runtime_state="goal-running",
            objective=full_objective,
            token_budget=None,
            tokens_used=10,
        )
        files = tuple(
            ReplyCardFileItem(
                path=f"/tmp/result-{index:02}.txt",
                label=f"result-{index:02}.txt",
                size=1,
                media_kind="file",
                additions=index + 1,
                deletions=index,
            )
            for index in range(10)
        )
        truncated = reply_card(
            ReplyCardProjection(
                scope=self.scope,
                goal=goal_module,
                result=ReplyCardResultModule("done"),
                files=ReplyCardFilesModule(
                    binding_id="binding-123",
                    turn_id="turn-123",
                    items=files,
                ),
            )
        )
        truncated_visible = next(
            element["content"]
            for element in _elements(truncated.card, "markdown")
            if "**Objective**" in element["content"]
        )
        self.assertIn(f"**Objective**：{'y' * 198} \n…\n", truncated_visible)
        self.assertNotIn("hidden tail", truncated_visible)

        page_value = next(
            behavior["value"]
            for button in _elements(truncated.card, "button")
            for behavior in button.get("behaviors", ())
            if behavior["value"]["intent"] == "turn-file.page"
        )
        self.assertEqual(
            page_value["reply"]["goal"]["objective"],
            full_objective,
        )

    def test_reply_card_closed_modules_render_legal_combinations(self) -> None:
        progress = TurnProgressManifest(
            state="running",
            steer_count=0,
            plan_available=True,
            plan_generated=True,
            plan_may_be_stale=False,
            steps=(TurnProgressManifestStep("检查实现", "completed"),),
        )
        active_goal = ReplyCardGoalModule(
            binding_id="binding-123",
            short_id="binding1",
            project_alias="test",
            goal_generation="g" * 43,
            status="active",
            runtime_state="goal-running",
            objective="deliver safely",
            token_budget=1000,
            tokens_used=20,
        )
        file_module = ReplyCardFilesModule(
            binding_id="binding-123",
            turn_id="turn-123",
            items=(
                ReplyCardFileItem(
                    path="/tmp/report.txt",
                    label="report.txt",
                    size=1,
                    media_kind="file",
                ),
            ),
        )
        combinations = (
            (
                ReplyCardProjection(result=ReplyCardResultModule("done")),
                {"turnanswerv1"},
            ),
            (
                ReplyCardProjection(scope=self.scope, goal=active_goal),
                {"goalmodulev1"},
            ),
            (
                ReplyCardProjection(
                    activity=ReplyCardActivityModule(progress=progress)
                ),
                {"turnprogressv1"},
            ),
            (
                ReplyCardProjection(
                    scope=self.scope,
                    result=ReplyCardResultModule("done"),
                    files=file_module,
                ),
                {"turnanswerv1", "turnfilesv4"},
            ),
            (
                ReplyCardProjection(
                    scope=self.scope,
                    goal=active_goal,
                    activity=ReplyCardActivityModule(
                        progress=progress,
                        terminal_status="completed",
                        collapsed=True,
                    ),
                    result=ReplyCardResultModule("done"),
                    files=file_module,
                ),
                {
                    "goalmodulev1",
                    "turnprogressv1",
                    "turnanswerv1",
                    "turnfilesv4",
                },
            ),
        )
        for projection, expected_ids in combinations:
            with self.subTest(expected_ids=expected_ids):
                card = reply_card(projection)
                element_ids = {
                    item.get("element_id")
                    for item in _tagged_elements(card.card)
                    if item.get("element_id")
                }
                self.assertEqual(element_ids, expected_ids)

        with self.assertRaisesRegex(ValueError, "same binding_id"):
            reply_card(
                ReplyCardProjection(
                    scope=self.scope,
                    goal=active_goal,
                    result=ReplyCardResultModule("done"),
                    files=ReplyCardFilesModule(
                        binding_id="binding-other",
                        turn_id=file_module.turn_id,
                        items=file_module.items,
                    ),
                )
            )

    def test_v5_pagination_rebuilds_goal_activity_result_and_files(self) -> None:
        files = tuple(
            ReplyCardFileItem(
                path=f"/tmp/result-{index:02}.txt",
                label=f"result-{index:02}.txt",
                size=1,
                media_kind="file",
            )
            for index in range(10)
        )
        projection = ReplyCardProjection(
            scope=self.scope,
            goal=ReplyCardGoalModule(
                binding_id="binding-123",
                short_id="binding1",
                project_alias="test",
                goal_generation="z" * 43,
                status="paused",
                runtime_state="goal-paused",
                objective="generate reports",
                token_budget=None,
                tokens_used=50,
            ),
            activity=ReplyCardActivityModule(
                progress=TurnProgressManifest(
                    state="running",
                    steer_count=1,
                    plan_available=True,
                    plan_generated=True,
                    plan_may_be_stale=False,
                    steps=(
                        TurnProgressManifestStep(
                            "password: do-not-leak", "completed"
                        ),
                    ),
                    commentary=(
                        TurnCommentaryManifestEntry(
                            text="goal progress",
                            event_timestamp_ms=333,
                        ),
                    ),
                    operations=(
                        TurnActivityManifestEntry(
                            kind=TurnActivityKind.TOOL.value,
                            status=TurnActivityStatus.COMPLETED.value,
                            event_timestamp_ms=444,
                            text="drive.search",
                        ),
                    ),
                ),
                terminal_status="completed",
                collapsed=True,
            ),
            result=ReplyCardResultModule("**完成**：报告已生成。"),
            files=ReplyCardFilesModule(
                binding_id="binding-123",
                turn_id="turn-123",
                items=files,
                additions=55,
                deletions=45,
            ),
        )
        first = reply_card(projection)
        page_value = next(
            behavior["value"]
            for button in _elements(first.card, "button")
            for behavior in button.get("behaviors", ())
            if behavior["value"]["intent"] == "turn-file.page"
        )
        self.assertEqual(page_value["v"], 5)
        self.assertEqual((page_value["a"], page_value["d"]), (55, 45))
        self.assertEqual(
            set(page_value["reply"]), {"goal", "activity", "result"}
        )
        encoded = json.dumps(page_value, ensure_ascii=False)
        self.assertNotIn("do-not-leak", encoded)
        self.assertIn("敏感内容已隐藏", encoded)
        progress = page_value["reply"]["activity"]["progress"]
        self.assertEqual(progress["commentary"][0]["event_timestamp_ms"], 333)
        self.assertEqual(progress["operations"][0]["event_timestamp_ms"], 444)
        self.assertEqual(progress["operations"][0]["text"], "drive.search")

        intent = decode_turn_file_action(
            app_id="cli_test",
            message_id="om_card",
            callback_chat_id=self.scope.chat_id,
            sender_id="ou_user",
            tag="button",
            value=page_value,
        )
        assert intent.reply is not None
        rebuilt = reply_card_from_manifest(
            scope=intent.scope,
            binding_id=intent.binding_id,
            turn_id=intent.turn_id,
            manifest=intent.files,
            reply=intent.reply,
            page=intent.page or 0,
            additions=intent.additions,
            deletions=intent.deletions,
        )
        rebuilt_text = json.dumps(rebuilt.card, ensure_ascii=False)
        self.assertIn("generate reports", rebuilt_text)
        self.assertIn("敏感内容已隐藏", rebuilt_text)
        self.assertIn("报告已生成", rebuilt_text)
        rebuilt_visible = "\n".join(
            element["content"]
            for element in _elements(rebuilt.card, "markdown")
        )
        self.assertIn("result-08.txt", rebuilt_visible)
        self.assertIn("+55", rebuilt_visible)
        self.assertIn("-45", rebuilt_visible)
        self.assertNotIn("result-00.txt", rebuilt_visible)
        self.assertIn("结束 Goal", rebuilt_text)
        self.assertNotIn("**Time**", rebuilt_text)
        self.assertIn("millisecond='333'", rebuilt_text)
        self.assertIn("millisecond='444'", rebuilt_text)
        self.assertIn("drive.search", rebuilt_text)

        retargeted = json.loads(json.dumps(page_value))
        retargeted["binding_id"] = "binding:v1:binding-other"
        with self.assertRaisesRegex(CardActionError, "Goal .* Files"):
            decode_turn_file_action(
                app_id="cli_test",
                message_id="om_card",
                callback_chat_id=self.scope.chat_id,
                sender_id="ou_user",
                tag="button",
                value=retargeted,
            )

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
        materialized_delete = delete_binding_card(
            scope=self.scope,
            binding_id=binding_id,
            short_id="11111111",
            project_alias="test",
            title="Release review",
            native_thread_id="native-one",
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
            native_delete_available=True,
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
        self.assertIn("原生 Codex Thread", str(materialized_delete.card))
        self.assertIn("spawned descendants", str(materialized_delete.card))
        self.assertIn(
            "native-thread:v1:native-one",
            str(materialized_delete.card),
        )
        self.assertIn("binding.unarchive", str(archived.card))
        self.assertIn("恢复并切换", str(archived.card))
        self.assertIn("binding.delete.archived.prepare", str(archived.card))

    def test_sessions_card_pins_active_and_offers_activate_for_others(self) -> None:
        active = SessionCardItem(
            binding_id="11111111-0000-0000-0000-000000000001",
            short_id="11111111",
            project_alias="test",
            native_thread_id="native-one",
            title="Active work",
            state="running",
            active=True,
            activity_revision=11,
            turn_id="turn-one",
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
            native_delete_available=True,
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
        # A running materialized row keeps lifecycle controls independent of stop.
        self.assertEqual(labels.count("归档"), 2)
        self.assertEqual(labels.count("删除"), 3)
        self.assertEqual(labels.count("停止"), 1)
        archive = next(
            b
            for b in buttons
            if b["text"]["content"] == "归档"
            and b["behaviors"][0]["value"]["binding_id"]
            == "binding:v1:22222222-0000-0000-0000-000000000002"
        )
        archive_value = archive["behaviors"][0]["value"]
        self.assertEqual(archive_value["intent"], "binding.archive.exact")
        self.assertEqual(
            archive_value["binding_id"],
            "binding:v1:22222222-0000-0000-0000-000000000002",
        )
        self.assertNotIn("expected_active_binding_id", archive_value)
        self.assertEqual(archive_value["page"], 0)
        self.assertEqual(
            archive["confirm"]["title"]["content"],
            "确认归档此会话？",
        )
        self.assertIn("历史不会删除", archive["confirm"]["text"]["content"])
        running_archive = next(
            b
            for b in buttons
            if b["text"]["content"] == "归档"
            and b["behaviors"][0]["value"]["binding_id"]
            == "binding:v1:11111111-0000-0000-0000-000000000001"
        )
        self.assertNotIn(
            "expected_activity_revision",
            running_archive["behaviors"][0]["value"],
        )
        self.assertNotIn(
            "expected_turn_id",
            running_archive["behaviors"][0]["value"],
        )

    def test_sessions_lifecycle_actions_depend_only_on_materialization(self) -> None:
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
                activity_revision=5,
                turn_id="turn-two",
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

        card = sessions_card(
            scope=self.scope,
            sessions=sessions,
            native_delete_available=True,
        )
        archive_buttons = [
            button
            for button in _elements(card.card, "button")
            if button["text"]["content"] == "归档"
        ]

        self.assertEqual(len(archive_buttons), 5)
        self.assertEqual(
            {
                button["behaviors"][0]["value"]["binding_id"]
                for button in archive_buttons
            },
            {
                "binding:v1:11111111-0000-0000-0000-000000000001",
                "binding:v1:22222222-0000-0000-0000-000000000002",
                "binding:v1:33333333-0000-0000-0000-000000000003",
                "binding:v1:55555555-0000-0000-0000-000000000005",
                "binding:v1:66666666-0000-0000-0000-000000000006",
            },
        )
        delete_buttons = [
            button
            for button in _elements(card.card, "button")
            if button["text"]["content"] == "删除"
        ]
        self.assertEqual(len(delete_buttons), 6)
        self.assertEqual(
            {
                button["behaviors"][0]["value"]["binding_id"]
                for button in delete_buttons
            },
            {
                "binding:v1:11111111-0000-0000-0000-000000000001",
                "binding:v1:22222222-0000-0000-0000-000000000002",
                "binding:v1:33333333-0000-0000-0000-000000000003",
                "binding:v1:44444444-0000-0000-0000-000000000004",
                "binding:v1:55555555-0000-0000-0000-000000000005",
                "binding:v1:66666666-0000-0000-0000-000000000006",
            },
        )
        stop_buttons = [
            button
            for button in _elements(card.card, "button")
            if button["text"]["content"] == "停止"
        ]
        self.assertEqual(len(stop_buttons), 1)

    def test_sessions_unavailable_row_keeps_lifecycle_and_recheck_controls(
        self,
    ) -> None:
        card = sessions_card(
            scope=self.scope,
            sessions=(
                SessionCardItem(
                    binding_id="11111111-0000-0000-0000-000000000001",
                    short_id="11111111",
                    project_alias="test",
                    native_thread_id="native-one",
                    title="Needs recovery",
                    state="turn-observation-unavailable",
                    active=True,
                    activity_revision=12,
                    turn_id="turn-unavailable",
                ),
            ),
            native_delete_available=True,
        )

        buttons = _elements(card.card, "button")
        labels = {button["text"]["content"] for button in buttons}
        self.assertEqual(labels, {"归档", "删除", "停止", "重新检查"})
        recheck = next(
            button
            for button in buttons
            if button["text"]["content"] == "重新检查"
        )
        value = recheck["behaviors"][0]["value"]
        self.assertEqual(value["expected_activity_revision"], 12)
        self.assertEqual(value["expected_turn_id"], "turn:v1:turn-unavailable")

    def test_sessions_materialized_delete_requires_native_capability(self) -> None:
        sessions = (
            SessionCardItem(
                binding_id="11111111-0000-0000-0000-000000000001",
                short_id="11111111",
                project_alias="test",
                native_thread_id="native-one",
                title="Materialized",
                state="idle",
                active=True,
            ),
            SessionCardItem(
                binding_id="22222222-0000-0000-0000-000000000002",
                short_id="22222222",
                project_alias="test",
                native_thread_id=None,
                title="Lazy",
                state="idle",
                active=False,
            ),
        )

        card = sessions_card(
            scope=self.scope,
            sessions=sessions,
            native_delete_available=False,
        )
        delete_values = [
            button["behaviors"][0]["value"]
            for button in _elements(card.card, "button")
            if button["text"]["content"] == "删除"
        ]

        self.assertEqual(len(delete_values), 1)
        self.assertEqual(
            delete_values[0]["binding_id"],
            "binding:v1:22222222-0000-0000-0000-000000000002",
        )
        self.assertIsNone(delete_values[0]["expected_native_thread_id"])

    def test_sessions_delete_confirmation_is_exact_red_and_has_back_action(
        self,
    ) -> None:
        common = {
            "scope": self.scope,
            "binding_id": "11111111-0000-0000-0000-000000000001",
            "short_id": "11111111",
            "project_alias": "test",
            "title": "Release cleanup",
            "page": 3,
        }
        materialized = sessions_delete_binding_card(
            **common,
            native_thread_id="native-one",
        )
        lazy = sessions_delete_binding_card(
            **common,
            native_thread_id=None,
        )

        self.assertEqual(materialized.card["header"]["template"], "red")
        self.assertIn("spawned descendants", str(materialized.card))
        self.assertIn("Codex App/CLI", str(materialized.card))
        final = next(
            button
            for button in _elements(materialized.card, "button")
            if button["text"]["content"] == "永久删除此会话"
        )
        value = final["behaviors"][0]["value"]
        self.assertEqual(value["intent"], "binding.delete.exact")
        self.assertEqual(
            value["binding_id"],
            "binding:v1:11111111-0000-0000-0000-000000000001",
        )
        self.assertNotIn("expected_active_binding_id", value)
        self.assertEqual(
            value["expected_native_thread_id"],
            "native-thread:v1:native-one",
        )
        self.assertEqual(value["page"], 3)
        self.assertNotIn("expected_activity_revision", value)
        self.assertNotIn("expected_turn_id", value)
        self.assertEqual(final["type"], "danger")
        self.assertIn("无法恢复", final["confirm"]["title"]["content"])
        back = next(
            button
            for button in _elements(materialized.card, "button")
            if button["text"]["content"] == "返回会话列表"
        )
        self.assertEqual(back["behaviors"][0]["value"]["page"], 3)
        self.assertIn("只永久删除本地 Binding", str(lazy.card))
        lazy_final = next(
            button
            for button in _elements(lazy.card, "button")
            if button["text"]["content"] == "永久删除此会话"
        )
        self.assertIsNone(
            lazy_final["behaviors"][0]["value"]["expected_native_thread_id"]
        )

    def test_archived_delete_confirmation_is_exact_and_returns_to_archive(
        self,
    ) -> None:
        card = archived_sessions_delete_binding_card(
            scope=self.scope,
            binding_id="11111111-0000-0000-0000-000000000001",
            short_id="11111111",
            project_alias="test",
            title="Archived work",
            native_thread_id="native-one",
        )

        self.assertEqual(card.card["header"]["template"], "red")
        buttons = _elements(card.card, "button")
        delete = next(
            button
            for button in buttons
            if button["text"]["content"] == "永久删除已归档会话"
        )
        value = delete["behaviors"][0]["value"]
        self.assertEqual(value["intent"], "binding.delete.archived")
        self.assertEqual(
            value["expected_native_thread_id"],
            "native-thread:v1:native-one",
        )
        back = next(
            button
            for button in buttons
            if button["text"]["content"] == "返回归档列表"
        )
        self.assertEqual(
            back["behaviors"][0]["value"]["intent"],
            "sessions.archived.refresh",
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
        card = sessions_card(
            scope=self.scope,
            sessions=sessions,
            native_delete_available=True,
            page=0,
        )
        text = str(card.card)
        self.assertIn("第 1/3 页", text)
        self.assertIn("下一页", text)
        self.assertNotIn("上一页", text)

        card = sessions_card(
            scope=self.scope,
            sessions=sessions,
            native_delete_available=True,
            page=1,
        )
        text = str(card.card)
        self.assertIn("第 2/3 页", text)
        self.assertIn("上一页", text)
        self.assertIn("下一页", text)

        # Out-of-range page is clamped to the last valid page.
        card = sessions_card(
            scope=self.scope,
            sessions=sessions,
            native_delete_available=True,
            page=99,
        )
        text = str(card.card)
        self.assertIn("第 3/3 页", text)
        self.assertIn("上一页", text)
        self.assertNotIn("下一页", text)

    def test_sessions_card_empty_state(self) -> None:
        card = sessions_card(
            scope=self.scope,
            sessions=(),
            native_delete_available=False,
            page=0,
        )
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
