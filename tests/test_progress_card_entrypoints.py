from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path

from netizen_cli.cards.callbacks import CardActionError
from netizen_cli.cards.controls import decode_card_form, new_binding_card
from netizen_cli.cards.defaults import defaults_card, decode_defaults_action
from netizen_cli.cards.scheduled import (
    decode_schedule_action, read_schedule_form, schedule_draft_card, schedule_form_card,
)
from netizen_cli.channel_app import _goal_reply_activity
from netizen_cli.domain import FeishuScope, GoalOperationState, ScopeKind
from netizen_cli.progress_card_settings import ProgressCardSettings
from netizen_cli.projects import Project
from netizen_cli.runtime.contracts import GoalActivitySnapshot
from netizen_cli.session_settings import BindingTaskFeedback, SessionSettings
from tests.support.channel_cards import direct_card_event, elements, form_values
from tests.support.channel_fixtures import channel_fixture
from tests.support.channel_messages import FakeMessage


CUSTOM = ProgressCardSettings(
    show_commentary=False, show_plan=False, commentary_limit=8, operation_limit=16,
    commentary_max_chars=320, operation_max_chars=240, organization="time",
)


class ProgressCardFormPreservationTest(unittest.TestCase):
    def setUp(self):
        self.scope = FeishuScope("app", "oc_group", ScopeKind.GROUP)
        self.projects = (Project("work", Path("/tmp/work"), True, 1),)
        self.settings = SessionSettings(task_feedback=BindingTaskFeedback(
            progress_card_enabled=True, progress_card_settings=CUSTOM,
        ))

    @staticmethod
    def toggle(card, field, *, enabled):
        select = next(item for item in elements(card.card, "select_static") if item["name"] == field)
        label = "开启" if enabled else "关闭"
        return next(option["value"] for option in select["options"] if option["text"]["content"].startswith(label))

    def defaults_card(self):
        record = {
            "id": "defaults-one", "kind": "chat", "chat_id": "oc_group", "keyword": None,
            "project": "work", "session_settings": self.settings.to_dict(), "revision": 1,
            "workspace": {"mode": "project"}, "position": None,
        }
        return defaults_card(self.scope,
            {"exact": record, "effective": record, "chat_kind": "group", "match_error": None},
            self.projects, None)

    def test_defaults_form_toggles_keep_admin_presentation_choices(self):
        card = self.defaults_card()
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                fields = form_values(card)
                fields["defaults_session_progress_card"] = self.toggle(card, "defaults_session_progress_card", enabled=enabled)
                result = decode_defaults_action(self.scope, {}, fields)
                actual = SessionSettings.from_dict(result["session_settings"])
                self.assertEqual(actual.task_feedback.progress_card_settings, CUSTOM)
                self.assertEqual(actual.task_feedback.progress_card_enabled, enabled)

    def test_schedule_new_and_edit_drafts_preserve_choices_when_redrawn(self):
        plan = {
            "id": "plan-one", "revision": 2, "name": "日报", "instructions": "检查项目",
            "project_alias": "work", "chat_id": "oc_group", "enabled": True,
            "schedule": {"kind": "daily", "timezone": "UTC", "at": "09:00"},
            "session_settings": self.settings.to_dict(),
        }
        for existing in (None, plan):
            with self.subTest(edit=existing is not None):
                card = schedule_form_card(self.scope, projects=self.projects, default_timezone="UTC",
                    plan=existing, session_settings=self.settings)
                fields = form_values(card)
                fields[next(name for name in fields if name.startswith("cron_name"))] = "新标题"
                fields["cron_kind"] = "daily"
                fields[next(name for name in fields if name.startswith("cron_instructions"))] = "修改指令"
                fields["cron_session_progress_card"] = self.toggle(card, "cron_session_progress_card", enabled=False)
                draft = read_schedule_form(self.scope, fields)
                self.assertEqual(draft.settings.task_feedback.progress_card_settings, CUSTOM)
                redrawn = schedule_draft_card(draft, projects=self.projects)
                result = decode_schedule_action(scope=self.scope, value=None, form=form_values(redrawn))
                actual = SessionSettings.from_dict(result.payload["session_settings"])
                self.assertEqual(actual.task_feedback.progress_card_settings, CUSTOM)
                self.assertFalse(actual.task_feedback.progress_card_enabled)

    def test_new_form_decodes_complete_feedback_without_new_visible_controls(self):
        for settings in (ProgressCardSettings(), CUSTOM):
            with self.subTest(settings=settings):
                card = new_binding_card(scope=self.scope, projects=self.projects, initial_project_alias="work",
                    task_feedback=replace(self.settings.task_feedback, progress_card_settings=settings))
                fields = form_values(card)
                intent = decode_card_form(scope=self.scope, message_id="om_form", sender_id="ou_user",
                    tag="button", form_value=fields)
                self.assertEqual(intent.progress_card_settings, settings)
                self.assertTrue(intent.progress_card_enabled)
                self.assertFalse(any("organization" in name or "limit" in name for name in fields))
                self.assertTrue(fields["new_progress_card"].startswith("progress-card:v1:"))
                self.assertLessEqual(len(fields["new_progress_card"]), 128)
                redrawn = new_binding_card(scope=self.scope, projects=self.projects, initial_project_alias="work",
                    settings_draft={name: value for name, value in fields.items() if name != "new_project"})
                fields = form_values(redrawn)
                fields["new_progress_card"] = self.toggle(redrawn, "new_progress_card", enabled=False)
                self.assertTrue(fields["new_progress_card"].startswith("progress-card:v1:"))
                intent = decode_card_form(scope=self.scope, message_id="om_redrawn", sender_id="ou_user",
                    tag="button", form_value=fields)
                self.assertEqual(intent.progress_card_settings, settings)
                self.assertFalse(intent.progress_card_enabled)

    def test_legacy_feedback_values_keep_default_presentation(self):
        card = new_binding_card(scope=self.scope, projects=self.projects, initial_project_alias="work")
        fields = form_values(card)
        fields["new_progress_card"] = "task-feedback:v2:on"
        intent = decode_card_form(scope=self.scope, message_id="om_form", sender_id="ou_user",
            tag="button", form_value=fields)
        self.assertEqual(intent.progress_card_settings, ProgressCardSettings())

    def test_invalid_or_oversize_carried_settings_are_rejected(self):
        card = self.defaults_card()
        fields = form_values(card)
        invalid = (
            [], [True], [True, False, True, False, 9, 16, 320, 240, "time"],
            [True, False, True, False, 8, 16, 320, 240, "unknown"],
            [True, False, True, False, True, 16, 320, 240, "time"],
            [1, False, True, False, 8, 16, 320, 240, "time"],
            [True, False, True, False, 8, 16, 320, 240, "time", "extra"],
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(CardActionError):
                decode_defaults_action(self.scope, {}, {
                    **fields, "defaults_session_progress_card": "progress-card:v1:" + json.dumps(values),
                })
        with self.assertRaises(CardActionError):
            decode_defaults_action(self.scope, {}, {
                **fields, "defaults_session_progress_card": "progress-card:v1:" + "[" * 129,
            })

    def test_goal_reply_projection_keeps_captured_settings(self):
        snapshot = GoalActivitySnapshot(
            binding_id="binding", thread_id="thread", logical_turn_id="goal", physical_turn_id="turn",
            revision=1, state=GoalOperationState.RUNNING, plan_available=True, plan_generated=False,
            steps=(), progress_card_settings=CUSTOM,
        )
        self.assertEqual(_goal_reply_activity(snapshot).progress_card_settings, CUSTOM)


class ProgressCardConfigurationChannelTest(unittest.IsolatedAsyncioTestCase):
    async def test_new_form_saves_complete_feedback_without_starting_turn(self):
        fixture = await self.enterAsyncContext(channel_fixture())
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        feedback = BindingTaskFeedback(progress_card_enabled=True, progress_card_settings=CUSTOM)
        card = new_binding_card(scope=scope, projects=fixture.projects.list(enabled_only=True),
            initial_project_alias="test", task_feedback=feedback, allow_context_mode=False)
        await fixture.app.handle_card_action(direct_card_event(fixture.channel, form_values(card)))
        self.assertEqual(fixture.store.active_binding(scope.key).task_feedback, feedback)
        self.assertEqual(fixture.runtime.submit_calls, [])

    async def test_current_and_legacy_config_forms_preserve_choices(self):
        fixture = await self.enterAsyncContext(channel_fixture())
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = fixture.store.create_channel_binding(scope=scope, project_alias="test", creator_id="ou_user",
            task_feedback=BindingTaskFeedback(progress_card_enabled=True, progress_card_settings=CUSTOM))
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                await fixture.app.handle_message(FakeMessage("/config", message_id=f"om_config_{legacy}"))
                card = fixture.channel.replies[-1][1]
                fields = form_values(card)
                fields["config_task_reactions"] = "task-feedback:v2:on"
                fields["config_progress_card"] = (
                    "task-feedback:v2:off" if legacy else
                    ProgressCardFormPreservationTest.toggle(card, "config_progress_card", enabled=False)
                )
                await fixture.app.handle_card_action(direct_card_event(
                    fixture.channel, fields, message_id=f"om_submit_{legacy}",
                ))
                updated = fixture.store.get(binding.id)
                self.assertEqual(updated.task_feedback, replace(binding.task_feedback,
                    reaction_pulse_enabled=True, progress_card_enabled=False))
                self.assertEqual(fixture.runtime.submit_calls, [])
