from __future__ import annotations

import copy
import base64
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from lark_channel import OutboundSender

from netizen.cards.callbacks import CardActionError
from netizen.cards.scheduled import (
    decode_schedule_action,
    is_schedule_card_action,
    schedule_form_card,
    schedule_manager_card,
    schedule_navigation,
    schedule_query,
    schedule_retry_card,
    SCHEDULE_CARD_JSON_LIMIT_BYTES,
)
from netizen.domain import FeishuScope, ScopeKind
from netizen.model_settings import EffortOption, ModelCatalog, ModelOption, ServiceTierOption
from netizen.projects import Project
from netizen.session_settings import SessionSettings


def elements(value, tag):
    found = []
    if isinstance(value, dict):
        if value.get("tag") == tag:
            found.append(value)
        for child in value.values():
            found.extend(elements(child, tag))
    elif isinstance(value, list):
        for child in value:
            found.extend(elements(child, tag))
    return found


def callback(card, label):
    return next(button["behaviors"][0]["value"] for button in elements(card.card, "button")
                if button.get("text", {}).get("content") == label)


def form_values(card):
    form = elements(card.card, "form")[0]
    result = {}
    for item in sum((elements(form, tag) for tag in ("input", "select_static", "multi_select_static", "date_picker", "picker_time")), []):
        if item["tag"] == "input":
            result[item["name"]] = item.get("default_value", "")
        elif item["tag"] == "select_static":
            result[item["name"]] = item.get("initial_option", "")
        elif item["tag"] == "multi_select_static":
            result[item["name"]] = item.get("selected_values", [])
        elif item["tag"] == "date_picker":
            result[item["name"]] = item.get("initial_date", "")
        elif item["tag"] == "picker_time":
            result[item["name"]] = item.get("initial_time", "")
    return result


def option_value(value):
    return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))


def manager_form(card, form_name, *, option=None, plan_id=None):
    form = next(item for item in elements(card.card, "form") if item["name"] == form_name)
    select = elements(form, "select_static")[0]
    value = select.get("initial_option", "")
    if plan_id is not None:
        value = next(item["value"] for item in select["options"] if option_value(item["value"]).get("plan_id") == plan_id)
    elif option is not None:
        value = next(item["value"] for item in select["options"] if item["value"] == option or option_value(item["value"]).get("filter") == option)
    return {select["name"]: value}


class ScheduleCardsTest(unittest.TestCase):
    def setUp(self):
        self.scope = FeishuScope("app", "oc_group", ScopeKind.TOPIC, "omt_existing")
        self.project = Project("work", Path("/tmp"), True, 1)
        self.plan = {"id": "plan-one", "revision": 2, "name": "日报", "instructions": "检查明确资源", "project_alias": "work", "chat_id": "oc_group", "enabled": True,
                     "schedule": {"kind": "daily", "timezone": "Asia/Shanghai", "at": "09:00"}, "session_settings": SessionSettings().to_dict()}
        self.catalog = ModelCatalog((ModelOption("future-model", "gpt-future", "Future Model", "catalog-only-description", True,
            "high", "priority", (EffortOption("low", "", "low"), EffortOption("high", "", "high")),
            (ServiceTierOption("priority", "Fast", ""),)),))

    def test_one_form_selects_four_rules_and_decodes_directly_to_save(self):
        card = schedule_form_card(self.scope, projects=[self.project], default_timezone="Asia/Shanghai")
        original = form_values(card)
        self.assertEqual(original["cron_kind"], "once")
        self.assertEqual(original["cron_at"], "09:00")
        self.assertEqual(original["cron_weekdays"], ["0", "1", "2", "3", "4"])
        self.assertEqual(original["cron_every_minutes"], "60")
        for kind in ("once", "daily", "weekly", "interval"):
            with self.subTest(kind=kind):
                form = dict(original)
                form["cron_kind"] = kind
                name = next(key for key in form if key.startswith("cron_name"))
                form[name] = "日报"
                form["cron_instructions"] = "检查明确资源"
                if kind == "once":
                    form["cron_date"] = "2030-09-10 +0000"
                    form["cron_at"] = "09:00 +0000"
                action = decode_schedule_action(scope=self.scope, value=None, form=form)
                self.assertEqual(action.action, "save")
                self.assertEqual(action.payload["schedule"]["kind"], kind)
                self.assertEqual(action.payload["project"], "work")
                self.assertNotIn("chat_id", action.payload)
                self.assertNotIn("sender_id", action.payload)
                if kind == "once":
                    self.assertEqual(action.payload["schedule"]["at"], "2030-09-10T09:00+08:00")

    def test_forms_use_supported_feishu_controls(self):
        for card in (schedule_manager_card(self.scope, {"plans": [self.plan]}),
                schedule_form_card(self.scope, projects=[self.project], default_timezone="UTC")):
            for tag in ("select_static", "multi_select_static", "date_picker", "picker_time"):
                for control in elements(card.card, tag):
                    self.assertNotIn("label", control)  # Real Feishu error 200621.
                    self.assertNotIn("initial_options", control)
                    self.assertTrue(control["name"])
                    self.assertNotIn("behaviors", control)
            names = [item["name"] for tag in ("input", "select_static", "multi_select_static", "date_picker", "picker_time", "button") for item in elements(card.card, tag) if "name" in item]
            self.assertEqual(len(names), len(set(names)))
            self.assertTrue(all(len(name) <= 100 for name in names))
        for control in sum((elements(card.card, tag) for tag in ("multi_select_static", "date_picker", "picker_time")), []):
            self.assertFalse(control["required"])
        interval = next(item for item in elements(card.card, "input") if item["name"].startswith("cron_every_minutes"))
        self.assertFalse(interval["required"])
        self.assertTrue(any(item["name"].startswith("cron_timezone") for item in elements(card.card, "input")))
        self.assertFalse(any(item["name"].startswith("cron_timezone") for panel in elements(card.card, "collapsible_panel") for item in elements(panel, "input")))
        self.assertEqual([item["text"]["content"] for item in elements(card.card, "button")], ["创建任务", "取消"])

    def test_edit_field_identity_fits_real_topic_and_uuid_plan_limits(self):
        self.scope = FeishuScope("cli_" + "a" * 16, "oc_" + "a" * 32, ScopeKind.TOPIC, "omt_" + "b" * 32)
        self.plan.update(id="721a455d-e1b8-482f-828d-22b69e4230ac", revision=123456789)
        self.plan["schedule"] = {"kind": "interval", "timezone": "UTC", "every_minutes": 45, "anchor": 1900000000.123456}
        navigation = {"filter": "all_enabled", "cursor": "a" * 512, "plan_id": self.plan["id"]}
        card = schedule_form_card(self.scope, projects=[self.project], default_timezone="UTC", plan=self.plan, navigation=navigation)
        form = form_values(card)
        self.assertTrue(all(len(name) <= 100 for name in form))
        action = decode_schedule_action(scope=self.scope, value=None, form=form)
        self.assertEqual(action.payload["plan_id"], self.plan["id"])
        self.assertEqual(action.payload["expected_revision"], self.plan["revision"])
        self.assertEqual(action.payload["schedule"]["anchor"], self.plan["schedule"]["anchor"])
        self.assertEqual(action.navigation, navigation)

    def test_obsolete_form_identity_is_rejected(self):
        meta = {"scope": self.scope.key, "kind": "once", "request_id": "old-form"}
        encoded = base64.urlsafe_b64encode(json.dumps(meta).encode()).decode().rstrip("=")
        for prefix in ("cron_name_v1__", "cron_name_v2__", "cron_name_v3__", "cron_name_v4__", "cron_name_v5__"):
            with self.subTest(prefix=prefix):
                form = {prefix + encoded: "旧卡片", "cron_instructions": "旧指令", "cron_project": "work", "cron_chat_id": "",
                    "cron_timezone": "UTC", "cron_at": "2030-09-10T09:00+08:00"}
                self.assertFalse(is_schedule_card_action(None, form))
                with self.assertRaises(CardActionError):
                    decode_schedule_action(scope=self.scope, value=None, form=form)

    def test_current_form_rejects_duplicate_or_incomplete_plan_revision(self):
        form = form_values(schedule_form_card(self.scope, projects=[self.project], default_timezone="UTC", plan=self.plan))
        instructions = next(name for name in form if name.startswith("cron_instructions__"))
        malformed_forms = [
            {**form, "cron_instructions": "duplicate base field"},
            {**form, "cron_instructions__another-plan:3": "duplicate decorated field"},
        ]
        for reference in ("plan-one", "plan-one:0", "plan-one:true", ":2"):
            malformed = dict(form)
            malformed["cron_instructions__" + reference] = malformed.pop(instructions)
            malformed_forms.append(malformed)
        for malformed in malformed_forms:
            with self.subTest(fields=list(malformed)):
                with self.assertRaises(CardActionError):
                    decode_schedule_action(scope=self.scope, value=None, form=malformed)
                with self.assertRaises(CardActionError):
                    schedule_retry_card(app_id="app", chat_id=self.scope.chat_id, value=None, form=malformed,
                        notice="retry", projects=[self.project])

    def test_current_form_requires_only_selected_frequency_fields(self):
        once = form_values(schedule_form_card(self.scope, projects=[self.project], default_timezone="UTC"))
        once[next(name for name in once if name.startswith("cron_name"))] = "一次性计划"
        once.update(cron_instructions="检查明确资源", cron_date="2030-09-10", cron_at="09:00")
        missing_date = dict(once)
        missing_date.pop("cron_date")
        obsolete_time = {**once, "cron_at": "2030-09-10T09:00+08:00"}
        for malformed in (missing_date, obsolete_time):
            with self.subTest(form=malformed), self.assertRaises(CardActionError):
                decode_schedule_action(scope=self.scope, value=None, form=malformed)
        required_by_kind = {"once": ("cron_date", "cron_at"), "daily": ("cron_at",),
            "weekly": ("cron_at", "cron_weekdays"), "interval": ("cron_every_minutes",)}
        for kind, required in required_by_kind.items():
            form = {**once, "cron_kind": kind}
            for missing in required:
                for value in (None, ""):
                    with self.subTest(kind=kind, missing=missing, value=value), self.assertRaises(CardActionError):
                        decode_schedule_action(scope=self.scope, value=None, form={**form, missing: value})
            for unused in {"cron_date", "cron_at", "cron_weekdays", "cron_every_minutes"} - set(required):
                form.pop(unused, None)
            action = decode_schedule_action(scope=self.scope, value=None, form=form)
            self.assertEqual(action.payload["schedule"]["kind"], kind)
        for invalid in (None, "", "cron", True):
            with self.subTest(kind=invalid), self.assertRaises(CardActionError):
                decode_schedule_action(scope=self.scope, value=None, form={**once, "cron_kind": invalid})

    def test_switching_frequency_ignores_unselected_values_and_prior_rule_evidence(self):
        self.plan["schedule"] = {"kind": "interval", "timezone": "UTC", "every_minutes": 45, "anchor": 120.0}
        interval = form_values(schedule_form_card(self.scope, projects=[self.project], default_timezone="UTC", plan=self.plan))
        minutes = next(key for key in interval if key.startswith("cron_every_minutes"))
        interval[minutes] = "invalid but unused"
        interval.update(cron_kind="daily", cron_date="invalid but unused", cron_weekdays="invalid but unused")
        daily = decode_schedule_action(scope=self.scope, value=None, form=interval)
        self.assertEqual(daily.payload["schedule"], {"kind": "daily", "timezone": "UTC", "at": "09:00", "end_at": None})
        self.plan["schedule"] = {"kind": "once", "timezone": "America/New_York", "at": "2030-11-03T01:30-05:00"}
        once = form_values(schedule_form_card(self.scope, projects=[self.project], default_timezone="UTC", plan=self.plan))
        once.update(cron_kind="interval", cron_date=None, cron_at=None, cron_weekdays=None, cron_every_minutes="7")
        repeated = decode_schedule_action(scope=self.scope, value=None, form=once)
        self.assertEqual(repeated.payload["schedule"], {"kind": "interval", "timezone": "America/New_York", "every_minutes": 7, "end_at": None})

    def test_recurring_deadline_create_roundtrip_and_once_ignores_it(self):
        card = schedule_form_card(self.scope, projects=[self.project], default_timezone="Asia/Shanghai")
        values = form_values(card)
        values[next(key for key in values if key.startswith("cron_name"))] = "有期限的计划"
        values.update(cron_instructions="检查项目", cron_end_date="2030-09-10 +0000", cron_end_time="18:45 +0000")
        for kind in ("daily", "weekly", "interval"):
            with self.subTest(kind=kind):
                action = decode_schedule_action(scope=self.scope, value=None, form={**values, "cron_kind": kind})
                self.assertEqual(action.payload["schedule"]["end_at"], "2030-09-10T18:45+08:00")
                saved = {**self.plan, "schedule": action.payload["schedule"]}
                edit = schedule_form_card(self.scope, projects=[self.project], default_timezone="UTC", plan=saved)
                edited = decode_schedule_action(scope=self.scope, value=None, form=form_values(edit))
                self.assertEqual(edited.payload["schedule"], action.payload["schedule"])
        once = decode_schedule_action(scope=self.scope, value=None, form={**values, "cron_kind": "once",
            "cron_date": "2030-09-09", "cron_end_date": True, "cron_end_time": "bad"})
        self.assertEqual(once.payload["schedule"], {"kind": "once", "timezone": "Asia/Shanghai", "at": "2030-09-09T09:00+08:00"})

    def test_edit_can_clear_deadline_without_resetting_identity_or_navigation(self):
        self.plan["schedule"].update(end_at="2030-09-10T09:00+08:00")
        navigation = {"filter": "all", "plan_id": self.plan["id"], "cursor": "page-two"}
        card = schedule_form_card(self.scope, projects=[self.project], default_timezone="UTC", plan=self.plan, navigation=navigation)
        original = form_values(card)
        end_time = next(key for key in original if key.startswith("cron_end_time"))
        expected = self.plan["schedule"]
        for clear_end in (False, True):
            with self.subTest(clear_end=clear_end):
                values = dict(original)
                if clear_end:
                    values.update(cron_end_date="", **{end_time: ""})
                action = decode_schedule_action(scope=self.scope, value=None, form=values)
                self.assertEqual(action.payload["schedule"], {**expected,
                    "end_at": None if clear_end else expected["end_at"]})
                self.assertEqual(action.payload["expected_revision"], self.plan["revision"])
                self.assertEqual(action.navigation, navigation)
        cancel = decode_schedule_action(scope=self.scope, value=callback(card, "取消"))
        self.assertEqual(cancel.action, "view")
        self.assertEqual(cancel.payload, {"plan_id": self.plan["id"]})
        self.assertEqual(cancel.navigation, navigation)
        self.assertEqual(self.plan["schedule"], expected)

    def test_recurring_deadline_validation_and_correction_keep_submission_identity(self):
        values = form_values(schedule_form_card(self.scope, projects=[self.project], default_timezone="UTC", plan=self.plan))
        initial = decode_schedule_action(scope=self.scope, value=None, form=values)
        self.assertIsNone(initial.payload["schedule"]["end_at"])
        for date, time in (("2030-09-10", ""), ("", "10:00"), (True, "10:00"),
                ("2030-02-30", "10:00"), ("2030-09-10", "25:00"), ("2030-09-10", True)):
            with self.subTest(date=date, time=time), self.assertRaises(CardActionError):
                decode_schedule_action(scope=self.scope, value=None, form={**values, "cron_end_date": date, "cron_end_time": time})
        corrected = decode_schedule_action(scope=self.scope, value=None, form={**values, "cron_end_date": "2030-09-10", "cron_end_time": "10:00"})
        self.assertEqual(corrected.payload["schedule"]["end_at"], "2030-09-10T10:00+08:00")
        self.assertEqual(corrected.request_id, initial.request_id)

    def test_recurring_end_picker_rejects_dst_gap_and_ambiguity_but_keeps_original_overlap(self):
        self.plan["schedule"] = {"kind": "weekly", "timezone": "America/New_York", "at": "09:00", "weekdays": [1],
            "end_at": "2030-11-03T01:30-05:00"}
        form = form_values(schedule_form_card(self.scope, projects=[self.project], default_timezone="UTC", plan=self.plan))
        end_time = next(key for key in form if key.startswith("cron_end_time"))
        self.assertEqual(form["cron_end_date"], "2030-11-03")
        self.assertEqual(form[end_time], "01:30")
        action = decode_schedule_action(scope=self.scope, value=None, form=form)
        self.assertEqual(action.payload["schedule"], self.plan["schedule"])
        gap = {**form, "cron_end_date": "2030-03-10 +0800", end_time: "02:30 +0800"}
        with self.assertRaises(CardActionError):
            decode_schedule_action(scope=self.scope, value=None, form=gap)
        changed = {**form, end_time: "01:45"}
        with self.assertRaisesRegex(CardActionError, "出现两次"):
            decode_schedule_action(scope=self.scope, value=None, form=changed)
        form["cron_end_time"] = form.pop(end_time)
        with self.assertRaisesRegex(CardActionError, "出现两次"):
            decode_schedule_action(scope=self.scope, value=None, form=form)

    def test_recurring_end_evidence_is_ignored_for_once_and_rejects_duplicate_fields(self):
        self.plan["schedule"].update(end_at="2030-09-10T09:00+08:00")
        form = form_values(schedule_form_card(self.scope, projects=[self.project], default_timezone="UTC", plan=self.plan))
        with self.assertRaises(CardActionError):
            decode_schedule_action(scope=self.scope, value=None, form={**form, "cron_end_time": "09:00"})
        end_time = next(key for key in form if key.startswith("cron_end_time"))
        form.update(cron_kind="once", cron_date="2030-09-09", cron_end_date="bad")
        form["cron_end_time__bad"] = form.pop(end_time)
        action = decode_schedule_action(scope=self.scope, value=None, form=form)
        self.assertNotIn("end_at", action.payload["schedule"])

    def test_selected_recurring_plan_shows_deadline_and_generic_ended_status(self):
        self.plan.update(status="ended")
        self.plan["schedule"].update(end_at="2030-09-10T01:00+00:00")
        detail = schedule_manager_card(self.scope, {"plans": [self.plan]}, selected={"plan": self.plan})
        text = str(detail.card)
        self.assertIn("2030-09-10T09:00+08:00", text)
        self.assertNotIn("一次性计划已结束", text)
        self.assertIn("计划已结束", text)

    def test_empty_form_value_on_navigation_still_decodes_button(self):
        value = callback(schedule_manager_card(self.scope, {"plans": []}), "新建定时任务")
        self.assertEqual(decode_schedule_action(scope=self.scope, value=value, form={}).action, "new")

    def test_once_picker_rejects_dst_gap_and_preserves_exact_overlap_on_edit(self):
        self.plan["schedule"] = {"kind": "once", "timezone": "America/New_York", "at": "2030-11-03T01:30-05:00"}
        form = form_values(schedule_form_card(self.scope, projects=[self.project], default_timezone="UTC", plan=self.plan))
        action = decode_schedule_action(scope=self.scope, value=None, form=form)
        self.assertEqual(action.payload["schedule"]["at"], "2030-11-03T01:30-05:00")
        form.update(cron_date="2030-03-10 +0800", cron_at="02:30 +0800")
        with self.assertRaises(CardActionError):
            decode_schedule_action(scope=self.scope, value=None, form=form)

        fresh = form_values(schedule_form_card(self.scope, projects=[self.project], default_timezone="America/New_York"))
        fresh.update(cron_date="2030-11-03", cron_at="01:30")
        with self.assertRaisesRegex(CardActionError, "出现两次"):
            decode_schedule_action(scope=self.scope, value=None, form=fresh)

    def test_client_input_limit_routes_long_existing_instructions_without_truncation(self):
        self.plan["instructions"] = "a" * 1001
        card = schedule_form_card(self.scope, projects=[self.project], default_timezone="UTC", plan=self.plan)
        self.assertNotIn("cron_plan", [item["name"] for item in elements(card.card, "form")])
        self.assertIn(self.plan["instructions"], str(card.card))
        self.assertEqual(callback(card, "暂停")["action"], "enabled")

    def test_edit_keeps_exact_revision_and_interval_anchor(self):
        self.plan["schedule"] = {"kind": "interval", "timezone": "UTC", "every_minutes": 45, "anchor": 120.0}
        card = schedule_form_card(self.scope, projects=[self.project], default_timezone="UTC", plan=self.plan)
        action = decode_schedule_action(scope=self.scope, value=None, form=form_values(card))
        self.assertEqual(action.payload["plan_id"], "plan-one")
        self.assertEqual(action.payload["expected_revision"], 2)
        self.assertEqual(action.payload["schedule"]["anchor"], 120)

    def test_unavailable_project_requires_explicit_replacement_and_preserves_edit(self):
        available = Project("available", Path("/tmp"), True, 2)
        for existing in ({"plan": self.plan}, {"initial_project": "work"}):
            with self.subTest(existing=existing):
                card = schedule_form_card(self.scope, projects=[available], default_timezone="UTC", **existing)
                control = next(item for item in elements(card.card, "select_static") if item["name"] == "cron_project")
                self.assertNotIn("initial_option", control)
                self.assertEqual([option_value(item["value"])["project"] for item in control["options"]], ["available"])
                self.assertIn("已停用或不可用", str(card.card))
                form = form_values(card)
                form["cron_kind"] = "daily"
                name = next(key for key in form if key.startswith("cron_name"))
                form[name] = form[name] or "新计划"
                instructions = next(key for key in form if key.startswith("cron_instructions"))
                form[instructions] = form[instructions] or "新指令"
                with self.assertRaisesRegex(CardActionError, "Project不能为空"):
                    decode_schedule_action(scope=self.scope, value=None, form=form)
                form["cron_project"] = control["options"][0]["value"]
                action = decode_schedule_action(scope=self.scope, value=None, form=form)
                self.assertEqual(action.payload["project"], "available")
                if "plan" in existing:
                    self.assertEqual(action.payload["instructions"], self.plan["instructions"])
                    self.assertEqual(action.payload["plan_id"], self.plan["id"])
                    self.assertEqual(action.payload["expected_revision"], self.plan["revision"])
                    self.assertEqual(action.payload["chat_id"], self.plan["chat_id"])
        self.assertEqual(self.plan["project_alias"], "work")

    def test_no_available_project_keeps_existing_plan_controls_and_instructions(self):
        card = schedule_form_card(self.scope, projects=[], default_timezone="UTC", plan=self.plan)
        self.assertNotIn("cron_plan", [item["name"] for item in elements(card.card, "form")])
        self.assertIn(self.plan["instructions"], str(card.card))
        self.assertIn("没有可用的 Project", str(card.card))
        self.assertEqual(callback(card, "暂停")["action"], "enabled")
        self.assertEqual(callback(card, "删除计划")["action"], "delete")

    def test_form_submission_identity_is_stable_across_replay_and_validation_correction(self):
        card = schedule_form_card(self.scope, projects=[self.project], default_timezone="UTC", plan=self.plan)
        values = form_values(card)
        first = decode_schedule_action(scope=self.scope, value=None, form=values)
        repeated = decode_schedule_action(scope=self.scope, value=None, form=dict(values))
        self.assertEqual(first, repeated)
        invalid = {**values, "cron_kind": "interval", "cron_every_minutes": "zero"}
        with self.assertRaises(CardActionError):
            decode_schedule_action(scope=self.scope, value=None, form=invalid)
        invalid["cron_every_minutes"] = "15"
        corrected = decode_schedule_action(scope=self.scope, value=None, form=invalid)
        self.assertEqual(corrected.request_id, first.request_id)
        self.assertEqual(corrected.payload["schedule"], {"kind": "interval", "timezone": "Asia/Shanghai", "every_minutes": 15, "end_at": None})
        self.assertEqual(corrected.payload["expected_revision"], self.plan["revision"])
        self.assertEqual([item["text"]["content"] for item in elements(card.card, "button")], ["保存修改", "取消"])

    def test_retry_form_renews_transport_identity_but_keeps_all_business_fields(self):
        self.plan["schedule"] = {"kind": "interval", "timezone": "America/New_York", "every_minutes": 45,
            "anchor": 1900000000.123456, "end_at": "2030-11-03T01:30-05:00"}
        self.plan["session_settings"] = SessionSettings.from_dict({"turn_settings": {"model_id": "missing-model", "effort_id": "high", "service_tier_id": "default"},
            "reaction_pulse_enabled": True, "progress_card_enabled": True, "message_context_mode": "catch-up"}).to_dict()
        navigation = {"filter": "all", "plan_id": self.plan["id"], "cursor": "page-two"}
        original = form_values(schedule_form_card(self.scope, projects=[self.project], default_timezone="UTC", plan=self.plan,
            allow_context_mode=True, navigation=navigation))
        first = decode_schedule_action(scope=self.scope, value=None, form=original)
        for _ in range(2):
            restored_scope, retry = schedule_retry_card(app_id=self.scope.app_id, chat_id=self.scope.chat_id,
                value=None, form=original, notice="稍后重试", projects=[])
            values = form_values(retry)
            self.assertEqual(restored_scope, self.scope)
            self.assertNotEqual(set(values), set(original))
            self.assertEqual(decode_schedule_action(scope=self.scope, value=None, form=values), first)
            self.assertEqual(len(elements(retry.card, "date_picker")), 2)
            self.assertEqual(len(elements(retry.card, "picker_time")), 2)
            self.assertEqual(callback(retry, "取消")["payload"], {"plan_id": self.plan["id"]})
            self.assertTrue(all(len(name) <= 100 for name in values))
            original = values

    def test_retry_form_keeps_invalid_text_and_empty_fields_for_correction(self):
        values = form_values(schedule_form_card(self.scope, projects=[self.project], default_timezone="UTC"))
        name = next(key for key in values if key.startswith("cron_name"))
        values.update(cron_instructions="保留完整文本", cron_kind="once", cron_date="2030-09-10", cron_at="09:00", cron_timezone="wrong-zone")
        _, retry = schedule_retry_card(app_id="app", chat_id=self.scope.chat_id, value=None, form=values, notice="检查时间", projects=[self.project])
        restored = form_values(retry)
        self.assertEqual(restored[next(key for key in restored if key.startswith("cron_name"))], values[name])
        for field in ("cron_instructions", "cron_date", "cron_at", "cron_timezone"):
            self.assertEqual(restored[field], values[field])
        self.assertEqual(len(elements(retry.card, "date_picker")), 2)
        self.assertEqual(len(elements(retry.card, "picker_time")), 2)
        with self.assertRaises(CardActionError):
            decode_schedule_action(scope=self.scope, value=None, form=restored)
        restored[next(key for key in restored if key.startswith("cron_name"))] = "修正后"
        restored.update(cron_date="2030-09-10", cron_at="09:00", cron_timezone="UTC")
        action = decode_schedule_action(scope=self.scope, value=None, form=restored)
        self.assertEqual(action.payload["instructions"], "保留完整文本")
        self.assertEqual(action.request_id, option_value(values["cron_project"])["request_id"])

    def test_retry_rejects_damaged_native_control_values_without_repair(self):
        values = form_values(schedule_form_card(self.scope, projects=[self.project], default_timezone="UTC"))
        malformed = (("cron_date", "2030-02-30"), ("cron_at", "wrong-time"),
            ("cron_end_date", "2030-13-01"), ("cron_end_time", "25:00"),
            ("cron_weekdays", ["7"]), ("cron_kind", "unknown"))
        for name, value in malformed:
            with self.subTest(name=name), self.assertRaises(CardActionError):
                schedule_retry_card(app_id="app", chat_id=self.scope.chat_id, value=None,
                    form={**values, name: value}, notice="retry", projects=[self.project])

    def test_retry_keeps_dst_business_error_in_native_pickers(self):
        values = form_values(schedule_form_card(self.scope, projects=[self.project], default_timezone="America/New_York"))
        values[next(key for key in values if key.startswith("cron_name"))] = "夏令时计划"
        values.update(cron_instructions="保留指令", cron_date="2030-03-10", cron_at="02:30")
        with self.assertRaises(CardActionError):
            decode_schedule_action(scope=self.scope, value=None, form=values)
        _, retry = schedule_retry_card(app_id="app", chat_id=self.scope.chat_id, value=None, form=values, notice="夏令时跳时", projects=[self.project])
        restored = form_values(retry)
        self.assertEqual(restored["cron_date"], "2030-03-10")
        self.assertEqual(restored["cron_at"], "02:30")
        self.assertEqual(len(elements(retry.card, "date_picker")), 2)
        self.assertEqual(len(elements(retry.card, "picker_time")), 2)
        restored["cron_date"] = "2030-03-11"
        action = decode_schedule_action(scope=self.scope, value=None, form=restored)
        self.assertEqual(action.payload["schedule"]["at"], "2030-03-11T02:30-04:00")
        self.assertEqual(action.request_id, option_value(values["cron_project"])["request_id"])

    def test_retry_preserves_known_effort_and_speed_fields_with_inherited_model(self):
        values = form_values(schedule_form_card(self.scope, projects=[self.project], default_timezone="UTC", plan=self.plan, catalog=self.catalog))
        self.assertTrue({"cron_session_effort", "cron_session_speed"} <= set(values))
        original = decode_schedule_action(scope=self.scope, value=None, form=values)
        _, retry = schedule_retry_card(app_id="app", chat_id=self.scope.chat_id, value=None, form=values,
            notice="retry", projects=[self.project])
        restored = form_values(retry)
        self.assertEqual(restored["cron_session_effort"], values["cron_session_effort"])
        self.assertEqual(restored["cron_session_speed"], values["cron_session_speed"])
        self.assertEqual(decode_schedule_action(scope=self.scope, value=None, form=restored), original)

    def test_retry_rejects_missing_identity_cross_scope_and_unknown_fields(self):
        values = form_values(schedule_form_card(self.scope, projects=[self.project], default_timezone="UTC"))
        malformed = [{**values, "unknown": "value"}, {key: value for key, value in values.items() if key != "cron_instructions"}]
        for form in malformed:
            with self.subTest(form=form), self.assertRaises(CardActionError):
                schedule_retry_card(app_id="app", chat_id=self.scope.chat_id, value=None, form=form, notice="retry", projects=[self.project])
        with self.assertRaises(CardActionError):
            schedule_retry_card(app_id="app", chat_id="oc_other", value=None, form=values, notice="retry", projects=[self.project])
        with self.assertRaises(CardActionError):
            schedule_retry_card(app_id="app", chat_id=self.scope.chat_id, value=None, form=values, notice="retry", projects=[self.project],
                scope=FeishuScope("app", self.scope.chat_id, ScopeKind.GROUP))

    def test_retry_button_renews_nonce_and_preserves_exact_write_request(self):
        value = callback(schedule_manager_card(self.scope, {"plans": [self.plan]}, selected={"plan": self.plan}), "暂停")
        before = decode_schedule_action(scope=self.scope, value=value)
        _, retry = schedule_retry_card(app_id="app", chat_id=self.scope.chat_id, value=value, form=None, notice="重试", projects=[])
        renewed = callback(retry, "重试刚才的操作")
        self.assertNotEqual(value["nonce"], renewed["nonce"])
        self.assertEqual(decode_schedule_action(scope=self.scope, value=renewed), before)

    def test_retry_navigation_uses_decoded_action_directly(self):
        manager = schedule_manager_card(self.scope, {"plans": [self.plan]})
        for form in (manager_form(manager, "cron_manage", plan_id=self.plan["id"]),
                manager_form(manager, "cron_filter", option="all_paused")):
            with self.subTest(form=form):
                original = decode_schedule_action(scope=self.scope, value=None, form=form)
                _, retry = schedule_retry_card(app_id="app", chat_id=self.scope.chat_id, value=None,
                    form=form, notice="retry", projects=[], scope=self.scope)
                action = decode_schedule_action(scope=self.scope, value=callback(retry, "重试刚才的操作"))
                self.assertEqual(action, original)

    def test_session_settings_copy_current_and_edit_keeps_all_settings_on_frequency_change(self):
        copied = SessionSettings.from_dict({"turn_settings": {"model_id": "future-model", "effort_id": "low", "service_tier_id": "default"},
            "reaction_pulse_enabled": True, "progress_card_enabled": True, "message_context_mode": "catch-up"})
        card = schedule_form_card(self.scope, projects=[self.project], default_timezone="UTC",
            session_settings=copied, catalog=self.catalog, allow_context_mode=True)
        form = form_values(card)
        form["cron_kind"] = "daily"
        form[next(key for key in form if key.startswith("cron_name"))] = "完整配置计划"
        form["cron_instructions"] = "检查明确资源"
        decoded = decode_schedule_action(scope=self.scope, value=None, form=form)
        self.assertEqual(decoded.payload["session_settings"], copied.to_dict())
        self.assertNotIn("catalog-only-description", str(decoded.payload))
        form["cron_kind"] = "weekly"
        repeated = decode_schedule_action(scope=self.scope, value=None, form=form)
        self.assertEqual(repeated.payload["session_settings"], copied.to_dict())
        self.plan["session_settings"] = copied.to_dict()
        edited = schedule_form_card(self.scope, projects=[self.project], default_timezone="UTC",
            plan=self.plan, session_settings=SessionSettings(), catalog=self.catalog, allow_context_mode=True)
        edited_payload = decode_schedule_action(scope=self.scope, value=None, form=form_values(edited)).payload
        self.assertEqual(edited_payload["session_settings"], copied.to_dict())
        self.assertEqual((edited_payload["plan_id"], edited_payload["expected_revision"]), (self.plan["id"], self.plan["revision"]))

    def test_explicit_settings_survive_catalog_failure_or_removal_until_inherit_selected(self):
        original = SessionSettings.from_dict({"turn_settings": {"model_id": "removed-model", "effort_id": "removed-effort", "service_tier_id": "removed-tier"},
            "reaction_pulse_enabled": True, "progress_card_enabled": False, "message_context_mode": "current-only"})
        self.plan["session_settings"] = original.to_dict()
        for catalog in (None, self.catalog):
            with self.subTest(catalog_available=catalog is not None):
                card = schedule_form_card(self.scope, projects=[self.project], default_timezone="UTC",
                    plan=self.plan, catalog=catalog, catalog_error="目录不可用", allow_context_mode=True)
                values = form_values(card)
                self.assertEqual(decode_schedule_action(scope=self.scope, value=None, form=values).payload["session_settings"], original.to_dict())
                model = next(item for item in elements(card.card, "select_static") if item["name"] == "cron_session_model")
                values["cron_session_model"] = next(option["value"] for option in model["options"] if option["text"]["content"] == "继承 Codex")
                inherited = decode_schedule_action(scope=self.scope, value=None, form=values).payload["session_settings"]
                self.assertIsNone(inherited["turn_settings"])
                self.assertTrue(inherited["reaction_pulse_enabled"])
                self.assertFalse(inherited["progress_card_enabled"])
                self.assertIn("removed-model", str(card.card))

    def test_private_form_hides_context_choice_and_explicit_inherit_is_not_catalog_default(self):
        inherited = SessionSettings.from_dict({"turn_settings": None, "reaction_pulse_enabled": False,
            "progress_card_enabled": True, "message_context_mode": "current-only"})
        self.plan["session_settings"] = inherited.to_dict()
        private = FeishuScope("app", "oc_private", ScopeKind.TOPIC, "omt_private")
        card = schedule_form_card(private, projects=[self.project], default_timezone="UTC",
            plan=self.plan, catalog=self.catalog, allow_context_mode=False)
        values = form_values(card)
        self.assertNotIn("cron_session_context_mode", values)
        self.assertEqual(decode_schedule_action(scope=private, value=None, form=values).payload["session_settings"], inherited.to_dict())
        self.assertIn("cron_session_effort", values)

    def test_session_settings_fields_are_required_and_share_ordinary_validation(self):
        card = schedule_form_card(self.scope, projects=[self.project], default_timezone="UTC",
            plan=self.plan, catalog=self.catalog, allow_context_mode=True)
        form = form_values(card)
        obsolete = {key: value for key, value in form.items() if not key.startswith("cron_session_")}
        malformed = [obsolete, {**form, "cron_session_task_reactions": True}, {**form, "cron_session_effort": "x" * 129},
            {**form, "cron_session_context_mode": "catch-up"}]
        partial = dict(form)
        partial.pop("cron_session_speed")
        malformed.append(partial)
        for values in malformed:
            with self.subTest(fields=values), self.assertRaises(CardActionError):
                decode_schedule_action(scope=self.scope, value=None, form=values)

    def test_cross_scope_and_mixed_forms_rejected(self):
        card = schedule_form_card(self.scope, projects=[self.project], default_timezone="UTC", plan=self.plan)
        form = form_values(card)
        other = FeishuScope("app", "oc_group", ScopeKind.TOPIC, "omt_other")
        with self.assertRaises(CardActionError):
            decode_schedule_action(scope=other, value=None, form=form)
        mixed = {**form, "project_mode": "create"}
        with self.assertRaises(CardActionError):
            decode_schedule_action(scope=self.scope, value=None, form=mixed)
        detail = schedule_manager_card(self.scope, {"plans": [self.plan]}, selected={"plan": self.plan})
        value = callback(detail, "暂停")
        with self.assertRaises(CardActionError):
            decode_schedule_action(scope=other, value=value)
        malformed = copy.deepcopy(value)
        malformed["v"] = True
        with self.assertRaises(CardActionError):
            decode_schedule_action(scope=self.scope, value=malformed)

    def test_manager_default_and_filter_choices_reset_selection_and_cursor(self):
        card = schedule_manager_card(self.scope, {"plans": [self.plan]})
        default = decode_schedule_action(scope=self.scope, value=None, form=manager_form(card, "cron_filter"))
        self.assertEqual(default.navigation, {"filter": "current_enabled"})
        self.assertEqual(schedule_query(default.navigation), {"enabled": True, "ended": False})
        self.assertEqual(manager_form(card, "cron_manage").popitem()[1], "")
        for selected, query in (("current", {}), ("current_enabled", {"enabled": True, "ended": False}),
                ("current_paused", {"enabled": False}), ("all", {"all": True}),
                ("all_enabled", {"all": True, "enabled": True, "ended": False}), ("all_paused", {"all": True, "enabled": False})):
            with self.subTest(filter=selected):
                drawn = schedule_manager_card(self.scope, {"plans": [self.plan]},
                    navigation={"filter": "all", "cursor": "old-page", "plan_id": self.plan["id"]}, selected={"plan": self.plan})
                action = decode_schedule_action(scope=self.scope, value=None, form=manager_form(drawn, "cron_filter", option=selected))
                self.assertEqual(action.action, "list")
                self.assertEqual(action.payload, {})
                self.assertEqual(action.navigation, {"filter": selected})
                self.assertEqual(schedule_query(action.navigation), query)

    def test_selection_and_pagination_keep_navigation_out_of_mutation_payload(self):
        state = {"filter": "all_enabled", "cursor": "this-page"}
        card = schedule_manager_card(self.scope, {"plans": [self.plan], "next_cursor": "page-two"}, navigation=state)
        selected = decode_schedule_action(scope=self.scope, value=None, form=manager_form(card, "cron_manage", plan_id=self.plan["id"]))
        self.assertEqual(selected.navigation, {**state, "plan_id": self.plan["id"]})
        self.assertEqual(selected.payload, {})
        following = decode_schedule_action(scope=self.scope, value=callback(card, "下一页任务"))
        self.assertEqual(following.navigation, {"filter": "all_enabled", "cursor": "page-two"})
        first = decode_schedule_action(scope=self.scope, value=callback(card, "回到首页"))
        self.assertEqual(first.navigation, {"filter": "all_enabled"})
        detail = schedule_manager_card(self.scope, {"plans": [self.plan]}, navigation=selected.navigation, selected={"plan": self.plan})
        pause = decode_schedule_action(scope=self.scope, value=callback(detail, "暂停"))
        self.assertEqual(pause.payload, {"plan_id": self.plan["id"], "expected_revision": 2, "enabled": False})
        self.assertEqual(pause.navigation, selected.navigation)

    def test_manager_shows_only_selected_details_and_repeated_selection_has_new_identity(self):
        other = {**self.plan, "id": "plan-two", "name": "其他计划", "instructions": "另一项完整指令"}
        plans = {"plans": [self.plan, other]}
        initial = schedule_manager_card(self.scope, plans)
        self.assertNotIn("刷新任务", [item["text"]["content"] for item in elements(initial.card, "button")])
        self.assertNotIn(self.plan["instructions"], str(initial.card))
        self.assertNotIn(other["instructions"], str(initial.card))
        callbacks = []
        state = None
        for chosen in (self.plan, other, self.plan):
            card = schedule_manager_card(self.scope, plans, navigation=state)
            values = manager_form(card, "cron_manage", plan_id=chosen["id"])
            callbacks.append(values)
            state = decode_schedule_action(scope=self.scope, value=None, form=values).navigation
            detail = schedule_manager_card(self.scope, plans, navigation=state, selected={"plan": chosen})
            self.assertIn(chosen["instructions"], str(detail.card))
            self.assertNotIn((other if chosen is self.plan else self.plan)["instructions"], str(detail.card))
            self.assertEqual(len(elements(detail.card, "select_static")), 2)
            refresh = decode_schedule_action(scope=self.scope, value=callback(detail, "刷新任务"))
            self.assertEqual(refresh.action, "view")
            self.assertEqual(refresh.payload, {"plan_id": chosen["id"]})
            body = detail.card["body"]["elements"]
            self.assertEqual(body[-2]["tag"], "hr")
            self.assertEqual(body[-1]["text"]["content"], "新建定时任务")
            self.assertEqual(body[-1]["type"], "primary_filled")
        self.assertNotEqual(callbacks[0], callbacks[2])
        self.assertEqual([option_value(next(iter(value.values())))["plan_id"] for value in callbacks],
            [self.plan["id"], other["id"], self.plan["id"]])

    def test_navigation_rejects_unknown_malformed_mixed_and_cross_scope_values(self):
        for value in (True, "all", [], {"filter": True}, {"filter": "private"}, {"all": True},
                {"cursor": ""}, {"cursor": "a" * 513}, {"plan_id": ""}, {"plan_id": "a" * 65}, {"plan_id": 1}):
            with self.subTest(value=value), self.assertRaises(CardActionError):
                schedule_navigation(value)
        card = schedule_manager_card(self.scope, {"plans": [self.plan]})
        chosen = manager_form(card, "cron_manage", plan_id=self.plan["id"])
        other_scope = FeishuScope("app", "oc_other", ScopeKind.GROUP)
        with self.assertRaises(CardActionError):
            decode_schedule_action(scope=other_scope, value=None, form=chosen)
        for values in ({**chosen, "other": "mixed"}, {next(iter(chosen)): "bad-value"},
                {**chosen, **manager_form(card, "cron_filter")}):
            with self.subTest(values=values), self.assertRaises(CardActionError):
                decode_schedule_action(scope=self.scope, value=None, form=values)
        value = callback(card, "新建定时任务")
        for malformed in ({key: item for key, item in value.items() if key != "navigation"},
                {**value, "v": 1}, {**value, "v": 2}, {**value, "navigation": {"filter": "bad"}},
                {**value, "action": "list", "payload": {"all": True}},
                {**value, "payload": {"kind": "daily"}}, {**value, "payload": {"draft": {}}},
                {**value, "action": "save", "payload": {"draft": {}, "write_request_id": "old-save"}},
                {**value, "action": "edit", "payload": {"plan_id": self.plan["id"], "expected_revision": 2, "kind": "daily"}}):
            with self.subTest(value=malformed), self.assertRaises(CardActionError):
                decode_schedule_action(scope=self.scope, value=malformed)

    def test_form_navigation_survives_frequency_selection_without_entering_write_payload(self):
        navigation = {"filter": "all_paused", "cursor": "page-two", "plan_id": self.plan["id"]}
        form = schedule_form_card(self.scope, projects=[self.project], default_timezone="UTC",
            plan=self.plan, navigation=navigation)
        decoded = decode_schedule_action(scope=self.scope, value=None, form=form_values(form))
        self.assertEqual(decoded.navigation, navigation)
        self.assertEqual(decoded.payload["project"], self.project.alias)
        self.assertNotIn("navigation", decoded.payload)
        self.assertEqual(callback(form, "取消")["navigation"], navigation)
        self.assertEqual(callback(form, "取消")["payload"], {"plan_id": self.plan["id"]})
        values = form_values(form)
        values["cron_kind"] = "weekly"
        repeated = decode_schedule_action(scope=self.scope, value=None, form=values)
        self.assertEqual(repeated.navigation, navigation)
        self.assertEqual(repeated.payload, {**decoded.payload,
            "schedule": {**decoded.payload["schedule"], "kind": "weekly", "weekdays": [0, 1, 2, 3, 4]}})

    def test_delete_uses_exact_native_confirmation_and_runs_stay_in_manager(self):
        detail = schedule_manager_card(self.scope, {"plans": [self.plan]}, selected={"plan": self.plan, "inflight": True})
        delete = decode_schedule_action(scope=self.scope, value=callback(detail, "删除计划"))
        self.assertEqual(delete.action, "delete")
        self.assertEqual(delete.payload, {"plan_id": "plan-one", "expected_revision": 2})
        button = next(item for item in elements(detail.card, "button") if item.get("text", {}).get("content") == "删除计划")
        self.assertIn("保留已有普通会话", str(button["confirm"]))
        runs = schedule_manager_card(self.scope, {"plans": [self.plan]}, selected={"plan": self.plan},
            runs={"runs": [{"status": "starting"}, {"status": "completed", "feishu_url": "https://applink.feishu.cn/client/chat/open?chatId=oc_group", "delivery_state": "sent"}], "next_cursor": "more"})
        panel = next(item for item in elements(runs.card, "collapsible_panel") if item["header"]["title"]["content"] == "最近执行")
        self.assertTrue(panel["expanded"])
        self.assertIn("正在启动", str(panel))
        self.assertNotIn("open_url", str(panel))
        following = decode_schedule_action(scope=self.scope, value=callback(runs, "更早执行"))
        self.assertEqual(following.payload, {"plan_id": "plan-one", "cursor": "more"})
        self.assertEqual(following.navigation, {"filter": "current_enabled", "plan_id": "plan-one"})
        self.assertEqual(len(elements(runs.card, "select_static")), 2)
        self.assertNotIn("返回计划", str(runs.card))

    def test_large_existing_plan_keeps_controls_and_explicitly_routes_editing(self):
        self.plan["instructions"] = "汉" * 32000
        card = schedule_form_card(self.scope, projects=[self.project], default_timezone="UTC", plan=self.plan)
        self.assertNotIn("cron_plan", [item["name"] for item in elements(card.card, "form")])
        self.assertIn("内容节选", str(card.card))
        self.assertIn("通过 Admin 或自然语言修改", str(card.card))
        self.assertEqual(callback(card, "暂停")["action"], "enabled")
        self.assertEqual(callback(card, "删除计划")["action"], "delete")
        self.assertEqual(self.plan["instructions"], "汉" * 32000)


class ScheduleCardCapacityTest(unittest.IsolatedAsyncioTestCase):
    def test_full_form_with_deadline_stays_within_component_budget(self):
        scope = FeishuScope("app", "oc_group", ScopeKind.TOPIC, "omt_topic")
        projects = [Project(f"project-{index}", Path("/tmp"), True, index) for index in range(16)]
        catalog = ModelCatalog(tuple(ModelOption(f"model-{index}", f"model-{index}", f"Model {index}", "", index == 0,
            "high", "priority", (EffortOption("low", "", "low"), EffortOption("high", "", "high")),
            (ServiceTierOption("priority", "Fast", ""),)) for index in range(16)))
        card = schedule_form_card(scope, projects=projects, default_timezone="Asia/Shanghai", catalog=catalog, allow_context_mode=True)
        def tagged_count(value):
            if isinstance(value, dict):
                return int("tag" in value) + sum(tagged_count(child) for child in value.values())
            return sum(tagged_count(child) for child in value) if isinstance(value, list) else 0
        # Count even option/label plain_text nodes, a conservative upper bound
        # on the platform's 200-component ceiling.
        self.assertLessEqual(tagged_count(card.card), 200)
        self.assertEqual(len(elements(card.card, "form")), 1)
        form = form_values(card)
        self.assertTrue({"cron_end_date", "cron_end_time"} <= set(form))
        self.assertEqual((form["cron_end_date"], form["cron_end_time"]), ("", ""))
        for panel in elements(card.card, "collapsible_panel"):
            self.assertNotIn("cron_end_date", str(panel))

    def test_fifty_plan_options_remain_compact_with_one_full_detail_and_runs(self):
        scope = FeishuScope("app", "oc_group", ScopeKind.TOPIC, "omt_topic")
        plans = [{"id": f"00000000-0000-0000-0000-{index:012d}", "revision": 1,
            "name": "名称" * 100, "instructions": "\x01" * 1999, "project_alias": "p" * 64,
            "chat_id": "oc_" + "a" * 32, "enabled": True,
            "schedule": {"kind": "daily", "timezone": "Asia/Shanghai", "at": "09:00"},
            "session_settings": SessionSettings().to_dict()} for index in range(50)]
        # This is the longest normal cursor emitted by the service: exact UUID
        # plus its filter fingerprint, rather than an arbitrary oversized token.
        cursor = base64.urlsafe_b64encode(json.dumps([plans[0]["id"], "f" * 24]).encode()).decode().rstrip("=")
        navigation = {"filter": "all", "cursor": cursor}
        result = {"plans": plans, "next_cursor": cursor}
        initial = schedule_manager_card(scope, result, navigation=navigation)
        detailed = schedule_manager_card(scope, result, navigation=navigation, selected={"plan": plans[-1]},
            runs={"runs": [{"status": "completed", "delivery_state": "sent", "due_at": 1900000000,
                "feishu_url": "https://applink.feishu.cn/client/chat/open?chatId=" + "a" * 64} for _ in range(5)]})
        for card in (initial, detailed):
            with self.subTest(selected=card is detailed):
                forms = elements(card.card, "form")
                selector = next(item for item in forms if item["name"] == "cron_manage")
                self.assertEqual(len(elements(selector, "select_static")[0]["options"]), 50)
                self.assertLess(len(card.card["body"]["elements"]), 15)
                self.assertLessEqual(len(json.dumps(card.card, ensure_ascii=False).encode()), SCHEDULE_CARD_JSON_LIMIT_BYTES)
        self.assertNotIn(plans[-1]["instructions"], str(initial.card))
        self.assertEqual(len([item for item in elements(detailed.card, "collapsible_panel")
            if item["header"]["title"]["content"] == "执行指令"]), 1)

    async def test_sdk_serialization_at_ascii_utf8_and_json_escape_boundaries(self):
        captured = []
        async def capture(**request):
            captured.append(request)
            return {"code": 0, "data": {"message_id": "om_wire_fixture"}}
        sender = OutboundSender(SimpleNamespace(create_message=capture, reply_message=capture))
        scope = FeishuScope("app", "oc_group", ScopeKind.TOPIC, "omt_topic")
        project = Project("work", Path("/tmp"), True, 1)
        settings = SessionSettings.from_dict({"turn_settings": {"model_id": "m" * 128, "effort_id": "e" * 128, "service_tier_id": "s" * 128},
            "reaction_pulse_enabled": True, "progress_card_enabled": True, "message_context_mode": "catch-up"}).to_dict()
        for instructions in ("a" * 1000, "汉" * 1000, "😀" * 1000, "\x01" * 1000):
            with self.subTest(utf8_bytes=len(instructions.encode("utf-8"))):
                plan = {"id": "plan-one", "revision": 1, "name": "名称" * 100,
                    "instructions": instructions, "project_alias": "work", "chat_id": "oc_group", "enabled": True,
                    "schedule": {"kind": "weekly", "timezone": "Asia/Shanghai", "at": "09:00", "weekdays": list(range(7)),
                        "end_at": "2030-09-10T09:00+08:00"}, "session_settings": settings}
                form_card = schedule_form_card(scope, projects=[project], default_timezone="UTC", plan=plan, allow_context_mode=True)
                action = decode_schedule_action(scope=scope, value=None, form=form_values(form_card))
                self.assertTrue(all(len(name) <= 100 for name in form_values(form_card)))
                for card in (form_card, schedule_manager_card(scope, {"plans": [plan]}, selected={"plan": plan})):
                    result = await sender.send(card, receive_id="oc_group", receive_id_type="chat_id", uuid_="wire-fixture")
                    self.assertTrue(result.success)
                    encoded = captured[-1]["content"]
                    self.assertEqual(encoded, json.dumps(card.card, ensure_ascii=False))
                    self.assertLessEqual(len(encoded.encode("utf-8")), SCHEDULE_CARD_JSON_LIMIT_BYTES)
                # Save retains every byte; no truncation is used to meet capacity.
                self.assertEqual(action.payload["instructions"], instructions)
                self.assertEqual(action.payload["session_settings"], settings)
                self.assertEqual(action.payload["schedule"], plan["schedule"])

    def test_over_capacity_input_is_rejected_before_a_save_action_exists(self):
        scope = FeishuScope("app", "oc_group", ScopeKind.GROUP)
        card = schedule_form_card(scope, projects=[Project("work", Path("/tmp"), True, 1)], default_timezone="UTC")
        form = form_values(card)
        form["cron_kind"] = "daily"
        form[next(key for key in form if key.startswith("cron_name"))] = "名称"
        for instructions in ("汉" * 4000, "😀" * 3000, "\x01" * 2000):
            form["cron_instructions"] = instructions
            with self.subTest(text_bytes=len(instructions.encode("utf-8"))), self.assertRaises(CardActionError):
                decode_schedule_action(scope=scope, value=None, form=form)


if __name__ == "__main__":
    unittest.main()
