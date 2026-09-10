from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from netizen.bindings import BindingStore, ScopeNotFound, BindingTurnSettings, BindingTaskFeedback
from netizen.domain import FeishuScope, ScopeKind, MentionContextMode, MessageContextAnchor
from netizen.model_settings import ModelCatalog, ModelOption, EffortOption, ServiceTierOption
from netizen.session_settings import SessionSettings
from netizen.schedules.service import ScheduleService


class ChatInfoFixture:
    def __init__(self):
        self.calls = []
        self.types = {}

    async def get_chat_info(self, chat_id):
        self.calls.append(chat_id)
        value = self.types.get(chat_id, ("private", "group"))
        if isinstance(value, Exception):
            raise value
        if value is None:
            return None
        return SimpleNamespace(chat_id=chat_id, chat_type=value[0], chat_mode=value[1])


class RunReaderFixture:
    def __init__(self):
        self.calls = []
        self.states = {}
        self.catalog = None
        self.catalog_calls = 0

    async def model_catalog(self):
        self.catalog_calls += 1
        if self.catalog is None:
            raise RuntimeError("catalog unavailable")
        return self.catalog

    async def read_scheduled_turn(self, binding_id, turn_id, *, deadline):
        self.calls.append((binding_id, turn_id))
        if deadline <= asyncio.get_running_loop().time():
            raise TimeoutError
        value = self.states.get((binding_id, turn_id), "inProgress")
        if isinstance(value, Exception):
            raise value
        return value


class ScheduleServiceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.cwd = Path(self.directory.name)
        self.store = BindingStore()
        self.store.register_project(alias="p", cwd=str(self.cwd))
        self.now = datetime.fromisoformat("2026-09-08T08:00:00+00:00").timestamp()
        self.chats = ChatInfoFixture()
        self.runtime = RunReaderFixture()
        self.service = self.new_service()
        self.first = self.binding("group-a", "native-a")
        self.second = self.binding("group-b", "native-b")
        self.request_sequence = 0

    async def asyncTearDown(self):
        self.store.close()
        self.directory.cleanup()

    def new_service(self, app_id="app"):
        return ScheduleService(
            bindings=self.store, runtime=self.runtime, app_id=app_id,
            chat_info=self.chats, wall_clock=lambda: self.now,
            default_timezone="Asia/Shanghai",
        )

    def binding(self, chat_id, thread_id, *, app_id="app", direct=False):
        binding = self.store.create_channel_binding(
            scope=FeishuScope(app_id, chat_id, ScopeKind.DIRECT if direct else ScopeKind.GROUP),
            project_alias="p", creator_id="person",
        )
        self.store.assign_native_thread_id(binding.id, thread_id)
        return self.store.get(binding.id)

    def create_request(self, **fields):
        self.request_sequence += 1
        return {
            "mode": "create", "name": "Daily review",
            "instructions": "Review the project and report findings.",
            "schedule": {"kind": "interval", "every_minutes": 15},
            "request_id": f"request-{self.request_sequence}", **fields,
        }

    async def create(self, *, service=None, native_thread_id="native-a", **fields):
        response = await (service or self.service).manage(
            self.create_request(**fields), native_thread_id=native_thread_id,
        )
        self.assertTrue(response["ok"], response)
        return response["plan"]

    async def test_same_cwd_native_threads_resolve_their_own_groups(self):
        first, second = await asyncio.gather(
            self.create(native_thread_id="native-a"),
            self.create(native_thread_id="native-b"),
        )
        self.assertEqual((first["chat_id"], second["chat_id"]), ("group-a", "group-b"))
        self.assertEqual((first["project_alias"], second["project_alias"]), ("p", "p"))
        for thread_id, expected in (("native-a", first), ("native-b", second)):
            response = await self.service.manage({"mode": "list"}, native_thread_id=thread_id)
            self.assertEqual([item["id"] for item in response["plans"]], [expected["id"]])

    async def test_recurring_cutoff_shares_preview_claims_and_ended_filters(self):
        request = self.create_request(schedule={"kind": "interval", "every_minutes": 1,
            "timezone": "UTC", "end_at": "2026-09-08T08:02+00:00"})
        preview = await self.service.preview(request, native_thread_id="native-a")
        self.assertTrue(preview["ok"], preview)
        self.assertEqual(len(preview["preview"]), 2)
        response = await self.service.manage(request, native_thread_id="native-a")
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["preview"], preview["preview"])
        plan = response["plan"]
        for count in (1, 2):
            self.now += 60
            claim = self.store.schedules.claim_due(plan["id"], now=self.now, app_id="app")
            self.assertIsNotNone(claim)
            view = await self.service.manage({"mode": "view", "plan_id": plan["id"]})
            self.assertEqual(len(view["preview"]), 2 - count)
            self.assertFalse(view["plan"]["lifecycle"]["ended"])
            if count == 2:
                self.assertTrue(view["plan"]["execution"]["is_last"])
                active = await self.service.manage({"mode": "list", "all": True, "ended": False})
                self.assertEqual([item["id"] for item in active["plans"]], [plan["id"]])
            self.store.schedules.release(claim.run.id)
        ended = await self.service.manage({"mode": "list", "all": True, "ended": True})
        self.assertEqual([item["id"] for item in ended["plans"]], [plan["id"]])
        self.assertIsNone(ended["plans"][0]["next_due_at"])
        self.now += 60
        self.assertIsNone(self.store.schedules.claim_due(plan["id"], now=self.now, app_id="app"))

    async def test_extending_shortening_and_clearing_cutoff(self):
        plan = await self.create(schedule={"kind": "interval", "every_minutes": 1,
            "end_at": "2026-09-08T08:01+00:00"})
        self.now += 60
        claim = self.store.schedules.claim_due(plan["id"], now=self.now, app_id="app")
        self.store.schedules.release(claim.run.id)
        rule = dict(plan["schedule"], end_at="2026-09-08T08:03+00:00")
        raised = await self.service.manage({"mode": "update", "plan_id": plan["id"], "expected_revision": 1,
            "request_id": "extend-cutoff", "schedule": rule})
        self.assertTrue(raised["ok"], raised)
        self.assertEqual(len(raised["preview"]), 2)
        cutoff = dict(rule, end_at="2026-09-08T08:00+00:00")
        ended = await self.service.manage({"mode": "update", "plan_id": plan["id"], "expected_revision": 2,
            "request_id": "end-now", "schedule": cutoff})
        self.assertTrue(ended["ok"], ended)
        self.assertTrue(ended["plan"]["lifecycle"]["ended"])
        self.assertEqual(ended["preview"], [])
        preview = await self.service.preview({"plan_id": plan["id"], "schedule": cutoff})
        self.assertTrue(preview["ok"], preview)
        self.assertEqual(preview["preview"], [])
        edited = await self.service.manage({"mode": "update", "plan_id": plan["id"], "expected_revision": 3,
            "request_id": "edit-ended", "schedule": cutoff, "enabled": True, "instructions": "Changed instructions"})
        self.assertTrue(edited["ok"], edited)
        cleared = await self.service.manage({"mode": "update", "plan_id": plan["id"], "expected_revision": 4,
            "request_id": "clear-cutoff", "schedule": dict(rule, end_at=None)})
        self.assertTrue(cleared["ok"], cleared)
        self.assertNotIn("end_at", cleared["plan"]["schedule"])
        self.assertEqual(len(cleared["preview"]), 3)
        self.assertFalse(cleared["plan"]["lifecycle"]["ended"])

    async def test_new_recurring_cutoff_requires_a_future_opportunity(self):
        request = self.create_request(schedule={"kind": "daily", "at": "09:00", "timezone": "UTC",
            "end_at": "2026-09-08T08:30+00:00"})
        preview = await self.service.preview(request)
        created = await self.service.manage(request, native_thread_id="native-a")
        for result in (preview, created):
            self.assertFalse(result["ok"], result)
            self.assertEqual(result["error"]["code"], "invalid_schedule")

    def session_catalog(self):
        self.runtime.catalog = ModelCatalog((ModelOption(
            id="model-a", model="model-a", display_name="Model A", description="Available model", is_default=True,
            default_effort_id="high", default_service_tier_id="priority",
            efforts=(EffortOption("low", "Low", "wire-low"), EffortOption("high", "High", "wire-high")),
            service_tiers=(ServiceTierOption("priority", "Fast", "Fast processing"),),
        ),))
        return self.runtime.catalog

    def configured_binding(self, thread_id="configured-native", chat_id="configured-chat"):
        binding = self.store.create_channel_binding(
            scope=FeishuScope("app", chat_id, ScopeKind.GROUP), project_alias="p", creator_id="person",
            turn_settings=BindingTurnSettings("model-a", "low", "default"),
            task_feedback=BindingTaskFeedback(True, True), message_context_mode=MentionContextMode.CATCH_UP,
            context_anchor=MessageContextAnchor("source-anchor", 1000),
        )
        self.store.assign_native_thread_id(binding.id, thread_id)
        return self.store.get(binding.id)

    async def test_create_snapshots_exact_native_binding_even_when_inactive_and_target_overridden(self):
        self.session_catalog()
        source = self.configured_binding()
        active = self.binding("configured-chat", "active-other-native")
        other_cwd = self.cwd / "other"
        other_cwd.mkdir()
        self.store.register_project(alias="other", cwd=str(other_cwd))
        plan = await self.create(native_thread_id=source.native_thread_id, chat_id="explicit-chat", project="other")
        expected = SessionSettings.from_binding(source).to_dict()
        self.assertEqual(plan["session_settings"], expected)
        self.assertEqual((plan["chat_id"], plan["project_alias"]), ("explicit-chat", "other"))
        self.assertEqual(self.store.active_binding(source.scope_key).id, active.id)
        self.assertEqual(self.runtime.catalog_calls, 1)
        self.store.set_turn_settings(binding_id=source.id, expected_revision=source.settings_revision, settings=None)
        self.assertEqual(self.store.schedules.get(plan["id"]).session_settings.to_dict(), expected)

    async def test_new_session_defaults_match_catalog_without_overriding_native_inheritance(self):
        catalog = self.session_catalog()
        inherited = await self.create()
        self.assertEqual(inherited["session_settings"], SessionSettings().to_dict())
        self.assertEqual(self.runtime.catalog_calls, 0)
        anonymous = await self.create(native_thread_id=None, chat_id="explicit-chat", project="p")
        self.assertEqual(anonymous["session_settings"], SessionSettings.new_defaults(catalog).to_dict())
        self.assertEqual(self.runtime.catalog_calls, 1)
        native = await self.create(native_thread_id=None, chat_id="explicit-chat", project="p", session_settings={"turn_settings": None})
        self.assertEqual(native["session_settings"], SessionSettings().to_dict())
        self.assertEqual(self.runtime.catalog_calls, 1)
        self.runtime.catalog = None
        fallback = await self.create(native_thread_id=None, chat_id="explicit-chat", project="p")
        self.assertEqual(fallback["session_settings"], SessionSettings().to_dict())
        self.assertEqual(self.runtime.catalog_calls, 2)

    async def test_partial_settings_update_preserves_plan_and_null_explicitly_resets_model(self):
        self.session_catalog()
        source = self.configured_binding()
        plan = await self.create(native_thread_id=source.native_thread_id)
        self.runtime.catalog = None
        calls = self.runtime.catalog_calls
        update = {"mode": "update", "plan_id": plan["id"], "expected_revision": 1,
                  "request_id": "partial-settings", "session_settings": {"progress_card_enabled": False}}
        changed = await self.service.manage(update, native_thread_id="native-b")
        self.assertTrue(changed["ok"], changed)
        expected = {**plan["session_settings"], "progress_card_enabled": False}
        self.assertEqual(changed["plan"]["session_settings"], expected)
        preview = await self.service.preview({"plan_id": plan["id"], "session_settings": expected}, native_thread_id="native-b")
        self.assertTrue(preview["ok"], preview)
        self.assertEqual(preview["session_settings"], expected)
        full = await self.service.manage({**update, "expected_revision": 2, "request_id": "full-unchanged-settings", "name": "Only metadata changed", "session_settings": expected})
        self.assertTrue(full["ok"], full)
        reset = await self.service.manage({**update, "expected_revision": 3, "request_id": "reset-settings", "session_settings": {"turn_settings": None}})
        self.assertTrue(reset["ok"], reset)
        self.assertEqual(reset["plan"]["session_settings"], {**expected, "turn_settings": None})
        self.assertEqual(self.runtime.catalog_calls, calls)

    async def test_private_target_normalizes_copied_context_but_rejects_explicit_catch_up(self):
        self.session_catalog()
        source = self.configured_binding()
        self.chats.types["private-target"] = ("private", "p2p")
        plan = await self.create(native_thread_id=source.native_thread_id, chat_id="private-target")
        self.assertEqual(plan["session_settings"]["message_context_mode"], "current-only")
        self.assertTrue(plan["session_settings"]["progress_card_enabled"])
        rejected = await self.service.manage(self.create_request(chat_id="private-target", session_settings={"message_context_mode": "catch-up"}), native_thread_id=source.native_thread_id)
        self.assertEqual(rejected["error"]["code"], "invalid_session_settings")
        options = await self.service.options(native_thread_id=source.native_thread_id, chat_id="private-target")
        self.assertFalse(options["context_mode_available"])
        self.assertEqual(options["session_settings"]["message_context_mode"], "current-only")
        group = await self.create(native_thread_id=source.native_thread_id)
        moved = await self.service.manage({"mode": "update", "plan_id": group["id"], "expected_revision": 1,
            "request_id": "move-to-private", "chat_id": "private-target"})
        self.assertTrue(moved["ok"], moved)
        self.assertEqual(moved["plan"]["session_settings"]["message_context_mode"], "current-only")

    async def test_options_share_catalog_and_preserve_source_settings_during_catalog_failure(self):
        catalog = self.session_catalog()
        source = self.configured_binding()
        options, raw = await self.service.form_options(native_thread_id=source.native_thread_id)
        self.assertIs(raw, catalog)
        self.assertTrue(options["ok"])
        self.assertEqual(options["session_settings"], SessionSettings.from_binding(source).to_dict())
        model = options["models"][0]
        self.assertEqual(model["id"], "model-a")
        self.assertEqual([tier["id"] for tier in model["service_tiers"]], ["default", "priority"])
        self.assertNotIn("wire_value", model["efforts"][0])
        self.assertEqual(self.runtime.catalog_calls, 1)
        self.runtime.catalog = None
        unavailable = await self.service.manage({"mode": "options"}, native_thread_id=source.native_thread_id)
        self.assertTrue(unavailable["ok"])
        self.assertEqual(unavailable["models"], [])
        self.assertEqual(unavailable["model_catalog_error"]["code"], "model_catalog_unavailable")
        self.assertEqual(unavailable["session_settings"], options["session_settings"])

    async def test_explicit_model_changes_are_validated_but_pause_and_delete_do_not_read_catalog(self):
        self.session_catalog()
        source = self.configured_binding()
        plan = await self.create(native_thread_id=source.native_thread_id)
        invalid = await self.service.manage({"mode": "update", "plan_id": plan["id"], "expected_revision": 1,
            "request_id": "invalid-model", "session_settings": {"turn_settings": {"model_id": "model-a", "effort_id": "SECRET_UNSUPPORTED_EFFORT", "service_tier_id": "default"}}})
        self.assertEqual(invalid["error"]["code"], "invalid_model_settings")
        self.assertNotIn("SECRET_UNSUPPORTED_EFFORT", str(invalid))
        self.assertEqual(self.store.schedules.get(plan["id"]).revision, 1)
        self.runtime.catalog = None
        calls = self.runtime.catalog_calls
        paused = await self.service.manage({"mode": "update", "plan_id": plan["id"], "expected_revision": 1, "request_id": "pause-without-model", "enabled": False})
        self.assertTrue(paused["ok"], paused)
        deleted = await self.service.manage({"mode": "delete", "plan_id": plan["id"], "expected_revision": 2, "request_id": "delete-without-model"})
        self.assertTrue(deleted["ok"], deleted)
        self.assertEqual(self.runtime.catalog_calls, calls)

    async def test_explicit_unregistered_group_does_not_need_a_scope(self):
        scope = FeishuScope("app", "brand-new-group", ScopeKind.GROUP)
        with self.assertRaises(ScopeNotFound):
            self.store.get_scope(scope.key)
        plan = await self.create(chat_id="brand-new-group")
        self.assertEqual(plan["chat_id"], "brand-new-group")
        self.assertEqual(plan["project_alias"], "p")
        with self.assertRaises(ScopeNotFound):
            self.store.get_scope(scope.key)

    async def test_native_topic_binding_defaults_to_topic_group_and_can_update(self):
        self.chats.types["topic-group"] = ("private", "topic")
        scope = FeishuScope("app", "topic-group", ScopeKind.TOPIC, "omt_source")
        binding = self.store.create_channel_binding(scope=scope, project_alias="p", creator_id="person")
        self.store.assign_native_thread_id(binding.id, "native-topic")
        plan = await self.create(native_thread_id="native-topic")
        self.assertEqual((plan["chat_id"], plan["project_alias"]), ("topic-group", "p"))
        listed = await self.service.manage({"mode": "list"}, native_thread_id="native-topic")
        self.assertEqual([item["id"] for item in listed["plans"]], [plan["id"]])
        updated = await self.service.manage({"mode": "update", "plan_id": plan["id"], "expected_revision": 1,
            "request_id": "topic-update", "name": "更新话题群计划"}, native_thread_id="native-topic")
        self.assertTrue(updated["ok"], updated)
        self.assertEqual(updated["plan"]["chat_id"], "topic-group")
        self.assertEqual(self.store.active_binding(scope.key).id, binding.id)

    async def test_private_topic_defaults_to_its_own_chat(self):
        scope = FeishuScope("app", "private-topic", ScopeKind.TOPIC, "omt_private")
        binding = self.store.create_channel_binding(scope=scope, project_alias="p", creator_id="person")
        self.store.assign_native_thread_id(binding.id, "native-private-topic")
        self.chats.types[scope.chat_id] = ("private", "p2p")
        plan = await self.create(native_thread_id="native-private-topic")
        self.assertEqual(plan["chat_id"], scope.chat_id)
        self.assertEqual(self.store.active_binding(scope.key).id, binding.id)

    async def test_unmapped_and_absent_context_require_explicit_defaults(self):
        for native_thread_id in (None, "side-native-id"):
            with self.subTest(native_thread_id=native_thread_id):
                missing = await self.service.manage(
                    self.create_request(), native_thread_id=native_thread_id,
                )
                self.assertFalse(missing["ok"])
                listed = await self.service.manage(
                    {"mode": "list"}, native_thread_id=native_thread_id,
                )
                self.assertEqual(listed["error"]["code"], "context_required")
                plan = await self.create(
                    native_thread_id=native_thread_id, chat_id="explicit-group", project="p",
                )
                self.assertEqual(plan["chat_id"], "explicit-group")
        all_plans = await self.service.manage({"mode": "list", "all": True})
        self.assertEqual(len(all_plans["plans"]), 2)

    async def test_direct_context_supplies_chat_and_project_and_scopes_the_list(self):
        source = self.binding("direct-chat", "direct-native", direct=True)
        self.chats.types["direct-chat"] = ("private", "p2p")
        private = await self.create(native_thread_id="direct-native")
        group = await self.create(native_thread_id="direct-native", chat_id="group-a")
        self.assertEqual((private["chat_id"], private["project_alias"]), ("direct-chat", "p"))
        self.assertEqual((group["chat_id"], group["project_alias"]), ("group-a", "p"))
        for context in ({"native_thread_id": "direct-native"}, {"scope_key": source.scope_key}):
            listed = await self.service.manage({"mode": "list"}, **context)
            self.assertEqual([plan["id"] for plan in listed["plans"]], [private["id"]])
        self.assertEqual(self.store.active_binding(source.scope_key).id, source.id)

    async def test_empty_explicit_target_is_rejected_instead_of_using_context(self):
        for field in ("chat_id", "project"):
            for value in ("", False, 0):
                with self.subTest(field=field, value=value):
                    response = await self.service.manage(
                        self.create_request(**{field: value}), native_thread_id="native-a",
                    )
                    self.assertFalse(response["ok"], response)
        response = await self.service.manage(
            {"mode": "list", "chat_id": ""}, native_thread_id="native-a",
        )
        self.assertFalse(response["ok"], response)
        self.assertEqual(self.store.schedules.list(app_id="app"), ())

    async def test_default_timezone_is_visible_and_persisted(self):
        plan = await self.create(schedule={"kind": "daily", "at": "09:00"})
        self.assertEqual(plan["schedule"]["timezone"], "Asia/Shanghai")
        self.assertEqual(plan["next_due_local"], "2026-09-09T09:00+08:00")
        self.service.default_timezone = "UTC"
        viewed = await self.service.manage({"mode": "view", "plan_id": plan["id"]})
        self.assertEqual(viewed["plan"]["schedule"]["timezone"], "Asia/Shanghai")

    async def test_unknown_local_timezone_requires_an_explicit_zone(self):
        with patch("netizen.schedules.service.local_timezone", return_value=None):
            service = ScheduleService(
                bindings=self.store, runtime=self.runtime, app_id="app",
                chat_info=self.chats, wall_clock=lambda: self.now,
            )
        missing = await service.manage(self.create_request(), native_thread_id="native-a")
        self.assertEqual(missing["error"]["code"], "timezone_required")
        explicit = await self.create(service=service, timezone="UTC")
        self.assertEqual(explicit["schedule"]["timezone"], "UTC")

    async def test_all_four_rules_use_the_same_preview_and_next_due(self):
        examples = (
            ({"kind": "once", "at": "2026-09-08T10:00+00:00"}, ["2026-09-08T10:00+00:00"]),
            ({"kind": "daily", "at": "09:00"}, ["2026-09-08T09:00+00:00", "2026-09-09T09:00+00:00", "2026-09-10T09:00+00:00"]),
            ({"kind": "weekly", "at": "09:00", "weekdays": [1, 3]}, ["2026-09-08T09:00+00:00", "2026-09-10T09:00+00:00", "2026-09-15T09:00+00:00"]),
            ({"kind": "interval", "every_minutes": 15}, ["2026-09-08T08:15+00:00", "2026-09-08T08:30+00:00", "2026-09-08T08:45+00:00"]),
        )
        for rule, expected in examples:
            with self.subTest(kind=rule["kind"]):
                preview = await self.service.preview({"schedule": rule, "timezone": "UTC"})
                self.assertTrue(preview["ok"], preview)
                self.assertEqual([item["utc"] for item in preview["preview"]], expected)
                created = await self.service.manage(
                    self.create_request(schedule=rule, timezone="UTC"), native_thread_id="native-a",
                )
                self.assertTrue(created["ok"], created)
                self.assertEqual(created["preview"], preview["preview"])
                self.assertEqual(created["plan"]["next_due_local"], expected[0])

    async def test_crud_pause_enable_and_revision_share_one_plan(self):
        plan = await self.create()
        self.now += 61
        paused = await self.service.manage({
            "mode": "update", "plan_id": plan["id"], "expected_revision": 1,
            "request_id": "pause", "enabled": False,
        }, native_thread_id="native-b")
        self.assertTrue(paused["ok"], paused)
        self.assertEqual(paused["plan"]["status"], "paused")
        self.assertIsNone(paused["plan"]["next_due_at"])
        self.assertEqual(paused["plan"]["chat_id"], "group-a")
        stale = await self.service.manage({
            "mode": "update", "plan_id": plan["id"], "expected_revision": 1,
            "request_id": "stale", "name": "Wrong stale edit",
        })
        self.assertEqual(stale["error"]["code"], "revision_conflict")
        enabled = await self.service.manage({
            "mode": "update", "plan_id": plan["id"], "expected_revision": 2,
            "request_id": "enable", "enabled": True, "name": "New name",
            "instructions": "New reusable instructions",
        })
        self.assertTrue(enabled["ok"], enabled)
        self.assertEqual(enabled["plan"]["revision"], 3)
        self.assertEqual(enabled["plan"]["name"], "New name")
        self.assertGreater(enabled["plan"]["next_due_at"], self.now)
        self.assertEqual(enabled["plan"]["schedule"]["anchor"], plan["schedule"]["anchor"])
        deleted = await self.service.manage({
            "mode": "delete", "plan_id": plan["id"], "expected_revision": 3,
            "request_id": "delete",
        })
        self.assertTrue(deleted["ok"], deleted)
        viewed = await self.service.manage({"mode": "view", "plan_id": plan["id"]})
        self.assertEqual(viewed["error"]["code"], "not_found")

    async def test_same_name_list_paginates_without_guessing_a_target(self):
        plans = [await self.create(name="Shared name") for _ in range(5)]
        await self.create(name="Unrelated")
        seen, cursor = [], None
        while True:
            response = await self.service.manage({
                "mode": "list", "name": "Shared name", "limit": 2, "cursor": cursor,
            }, native_thread_id="native-a")
            self.assertTrue(response["ok"], response)
            seen.extend(item["id"] for item in response["plans"])
            cursor = response["next_cursor"]
            if not cursor:
                break
            changed = await self.service.manage({
                "mode": "list", "name": "Unrelated", "limit": 2, "cursor": cursor,
            }, native_thread_id="native-a")
            self.assertFalse(changed["ok"])
        self.assertEqual(set(seen), {plan["id"] for plan in plans})
        self.assertEqual(len(seen), 5)

    async def test_unfinished_filter_applies_before_pagination_without_hiding_paused_plans(self):
        plans = sorted([await self.create(schedule={"kind": "once", "at": "2026-09-08T08:01+00:00"}) for _ in range(6)], key=lambda plan: plan["id"])
        paused = await self.create(enabled=False)
        ended_ids = {plan["id"] for plan in plans[::2]}
        for plan in plans[::2]:
            claim = self.store.schedules.claim_due(plan["id"], app_id="app", now=self.now + 60)
            self.assertIsNotNone(claim)
            self.store.schedules.release(claim.run.id)
        query = {"mode": "list", "enabled": True, "ended": False, "limit": 2}
        first = await self.service.manage(query, native_thread_id="native-a")
        self.assertTrue(first["ok"], first)
        self.assertEqual(len(first["plans"]), 2)
        self.assertIsNotNone(first["next_cursor"])
        second = await self.service.manage({**query, "cursor": first["next_cursor"]}, native_thread_id="native-a")
        self.assertTrue(second["ok"], second)
        self.assertEqual(len(second["plans"]), 1)
        self.assertIsNone(second["next_cursor"])
        self.assertEqual([item["id"] for item in first["plans"] + second["plans"]], [plan["id"] for plan in plans[1::2]])
        for changed in ({"mode": "list", "enabled": True}, {"mode": "list", "enabled": True, "ended": True}):
            mismatch = await self.service.manage({**changed, "cursor": first["next_cursor"]}, native_thread_id="native-a")
            self.assertFalse(mismatch["ok"])
        enabled_only = await self.service.manage({"mode": "list", "enabled": True}, native_thread_id="native-a")
        self.assertEqual({item["id"] for item in enabled_only["plans"]}, {plan["id"] for plan in plans})
        self.assertEqual({item["id"] for item in enabled_only["plans"] if item["status"] == "ended"}, ended_ids)
        all_plans = await self.service.manage({"mode": "list", "all": True}, native_thread_id="native-a")
        self.assertEqual(len(all_plans["plans"]), 7)
        remaining = await self.service.manage({"mode": "list", "ended": False}, native_thread_id="native-a")
        self.assertEqual({item["id"] for item in remaining["plans"]}, {plan["id"] for plan in plans[1::2]} | {paused["id"]})
        paused_only = await self.service.manage({"mode": "list", "enabled": False, "ended": False}, native_thread_id="native-a")
        self.assertEqual([item["id"] for item in paused_only["plans"]], [paused["id"]])

    async def test_ended_filter_is_independent_of_enabled_and_bound_to_cursor(self):
        future = await self.create(enabled=False)
        expired = [await self.create(enabled=enabled, schedule={"kind": "once", "at": "2026-09-08T08:01+00:00"})
                   for enabled in (False, True)]
        self.now += 121
        query = {"mode": "list", "ended": True, "limit": 1}
        first = await self.service.manage(query, native_thread_id="native-a")
        second = await self.service.manage({**query, "cursor": first["next_cursor"]}, native_thread_id="native-a")
        self.assertEqual({item["id"] for item in first["plans"] + second["plans"]}, {item["id"] for item in expired})
        for item in first["plans"] + second["plans"]:
            self.assertEqual(item["lifecycle"], {"ended": True, "has_future": False, "has_trigger": False})
            self.assertEqual(item["execution"]["status"], "expired")
            self.assertEqual(item["status"], "ended")
        remaining = await self.service.manage({"mode": "list", "ended": False, "enabled": False}, native_thread_id="native-a")
        self.assertEqual([item["id"] for item in remaining["plans"]], [future["id"]])
        self.assertTrue(remaining["plans"][0]["lifecycle"]["has_trigger"])
        self.assertEqual(remaining["plans"][0]["status"], "paused")
        self.assertEqual(remaining["plans"][0]["execution"]["status"], "not_started")
        for invalid in ({"ended": False, "cursor": first["next_cursor"]}, {"ended": "true"},
                        {"ended": 1}, {"ended": []}):
            result = await self.service.manage({"mode": "list", **invalid}, native_thread_id="native-a")
            self.assertFalse(result["ok"], result)

    async def test_interval_retry_uses_original_intent_across_clock_changes(self):
        request = self.create_request()
        first = await self.service.manage(request, native_thread_id="native-a")
        self.now += 3600
        retry = await self.service.manage(request, native_thread_id="native-a")
        self.assertTrue(retry["ok"], retry)
        self.assertTrue(retry["replayed"])
        self.assertEqual(retry["plan"]["id"], first["plan"]["id"])
        self.assertEqual(retry["plan"]["schedule"], first["plan"]["schedule"])
        self.assertEqual(len(self.store.schedules.list(app_id="app")), 1)
        self.assertEqual(self.chats.calls, ["group-a"])
        changed = await self.service.manage({**request, "instructions": "Different"}, native_thread_id="native-a")
        self.assertEqual(changed["error"]["code"], "request_conflict")

    async def test_once_retry_after_due_and_delete_retries_do_not_recreate(self):
        request = self.create_request(schedule={"kind": "once", "at": "2026-09-08T09:00+00:00"})
        first = await self.service.manage(request, native_thread_id="native-a")
        self.assertTrue(first["ok"], first)
        self.now += 7200
        retry = await self.service.manage(request, native_thread_id="native-a")
        self.assertTrue(retry["ok"], retry)
        self.assertTrue(retry["replayed"])
        delete = {"mode": "delete", "plan_id": first["plan"]["id"], "expected_revision": 1, "request_id": "delete-once"}
        self.assertTrue((await self.service.manage(delete))["ok"])
        delete_retry = await self.service.manage(delete)
        self.assertTrue(delete_retry["ok"], delete_retry)
        self.assertTrue(delete_retry["replayed"])
        after_delete = await self.service.manage(request, native_thread_id="native-a")
        self.assertTrue(after_delete["ok"], after_delete)
        self.assertTrue(after_delete["deleted"])
        self.assertEqual(self.store.schedules.list(app_id="app"), ())

    async def test_app_namespaces_do_not_expose_or_mutate_each_others_plans(self):
        plan = await self.create()
        other = self.new_service("other-app")
        listed = await other.manage({"mode": "list", "all": True})
        self.assertEqual(listed["plans"], [])
        for mode in ("view", "runs", "update", "delete"):
            request = {"mode": mode, "plan_id": plan["id"]}
            if mode in {"update", "delete"}:
                request.update(expected_revision=1, request_id="other-" + mode)
            if mode == "update":
                request["enabled"] = False
            response = await other.manage(request)
            self.assertEqual(response["error"]["code"], "not_found")
        missing_defaults = await other.manage(self.create_request(), native_thread_id="native-a")
        self.assertFalse(missing_defaults["ok"])
        explicit = await self.create(service=other, native_thread_id="native-a", project="p", chat_id="group-a")
        self.assertEqual(explicit["app_id"], "other-app")
        self.assertEqual(self.store.schedules.get(plan["id"]).revision, 1)

    async def test_disabled_project_can_still_pause_its_plan(self):
        plan = await self.create()
        project = self.store.get_project("p")
        self.store.set_project_enabled(alias="p", enabled=False, expected_revision=project.revision)
        response = await self.service.manage({
            "mode": "update", "plan_id": plan["id"], "expected_revision": 1,
            "request_id": "disabled-pause", "enabled": False,
        })
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["plan"]["status"], "paused")
        self.assertEqual(response["plan"]["blocked_reason"], "project_disabled")
        enabled = await self.service.manage({
            "mode": "update", "plan_id": plan["id"], "expected_revision": 2,
            "request_id": "disabled-enable", "enabled": True,
        })
        self.assertFalse(enabled["ok"])
        self.assertEqual(enabled["error"]["code"], "project_disabled")

    async def test_project_failures_have_safe_actionable_results_for_all_adapters(self):
        cases = (("missing", "not_found"), ("disabled", "project_disabled"), ("deleting", "project_deleting"))
        for state, expected_code in cases:
            alias = "audit-project-value-" + state
            if state != "missing":
                cwd = self.cwd / state
                cwd.mkdir()
                project = self.store.register_project(alias=alias, cwd=str(cwd))
                if state == "disabled":
                    self.store.set_project_enabled(alias=alias, enabled=False, expected_revision=project.revision)
                else:
                    snapshot = self.store.preview_project_delete(alias)
                    self.store.begin_project_delete(alias=alias, expected_revision=project.revision,
                        expected_inventory_fingerprint=snapshot.fingerprint)
            messages = []
            for source in ("mcp", "card", "admin"):
                with self.subTest(state=state, source=source):
                    response = await self.service.manage(self.create_request(project=alias, enabled=False),
                        native_thread_id="native-a", source=source)
                    self.assertFalse(response["ok"])
                    self.assertEqual(response["error"]["code"], expected_code)
                    self.assertNotIn(alias, str(response))
                    self.assertIn("Project", response["error"]["message"])
                    self.assertIn("选择", response["error"]["message"])
                    messages.append(response["error"]["message"])
            self.assertEqual(len(set(messages)), 1)
        self.assertEqual(self.store.schedules.list(app_id="app"), ())

    async def test_invalid_and_unreachable_chat_preserve_zero_mutation(self):
        for chat, value in (("unknown", ("private", "unknown")), ("gone", None), ("error", OSError("no access"))):
            self.chats.types[chat] = value
            response = await self.service.manage(
                self.create_request(chat_id=chat), native_thread_id="native-a",
            )
            self.assertFalse(response["ok"], response)
        self.assertEqual(self.store.schedules.list(app_id="app"), ())

    async def run_fixture(self, *, schedule=None):
        plan = await self.create(schedule=schedule or {"kind": "interval", "every_minutes": 1})
        self.now += 60
        claim = self.store.schedules.claim_due(plan["id"], app_id="app", now=self.now)
        assert claim is not None
        self.store.schedules.set_run(
            claim.run.id, phase="publishing_topic", root_message_id="root-" + claim.run.id,
            topic_id="topic-" + claim.run.id, origin_message_id="seed-" + claim.run.id,
        )
        binding = self.store.create_scheduled_binding(
            run_id=claim.run.id, scope=FeishuScope("app", "group-a", ScopeKind.TOPIC, "topic-" + claim.run.id),
        )
        self.store.begin_scheduled_initial(claim.run.id, binding.id)
        self.store.assign_native_thread_id(binding.id, "scheduled-native-" + claim.run.id)
        self.store.mark_scheduled_turn_started(claim.run.id, binding.id, "initial-turn")
        return plan, claim.run.id, self.store.get(binding.id)

    async def test_pending_execution_precedes_newer_skipped_result_and_list_is_read_only(self):
        plan, run_id, binding = await self.run_fixture()
        changed = await self.service.manage({"mode": "update", "plan_id": plan["id"], "expected_revision": 1,
            "request_id": "last-opportunity", "schedule": {"kind": "once", "at": "2026-09-08T08:02+00:00"}})
        self.assertTrue(changed["ok"], changed)
        self.now += 60
        self.assertIsNone(self.store.schedules.claim_due(plan["id"], app_id="app", now=self.now))
        skipped = self.store.schedules.list_runs(plan["id"], limit=1)[0]
        self.assertEqual(skipped.error_code, "skipped_busy")
        refreshed = []

        async def forbidden_refresh(plan_id):
            refreshed.append(plan_id)
            raise AssertionError("list must not refresh")

        self.service.set_refresh_handler(forbidden_refresh)
        for native_status, expected in (("inProgress", "inProgress"), ("completed", "completed"),
                                        ("failed", "failed"), ("interrupted", "interrupted"), ("missing", "unknown"),
                                        (OSError("unavailable"), "unavailable")):
            self.runtime.states[binding.id, "initial-turn"] = native_status
            before = self.store.schedules.get_run(run_id)
            self.runtime.calls.clear()
            response = await self.service.manage({"mode": "list", "ended": False}, native_thread_id="native-a")
            current, = response["plans"]
            self.assertEqual(current["lifecycle"], {"ended": False, "has_future": False, "has_trigger": False})
            self.assertEqual(current["latest_run"]["id"], skipped.id)
            self.assertEqual((current["execution"]["kind"], current["execution"]["run_id"], current["execution"]["status"]),
                             ("current", run_id, expected))
            self.assertTrue(current["execution"]["is_last"])
            self.assertEqual(self.runtime.calls, [(binding.id, "initial-turn")])
            self.assertEqual(self.store.schedules.get_run(run_id), before)
        self.assertEqual(refreshed, [])
        self.assertEqual((await self.service.manage({"mode": "list", "ended": True}, native_thread_id="native-a"))["plans"], [])

    async def test_last_pending_stages_remain_unfinished_until_explicit_terminal_refresh(self):
        plan = await self.create(schedule={"kind": "once", "at": "2026-09-08T08:01+00:00"})
        self.now += 60
        claim = self.store.schedules.claim_due(plan["id"], app_id="app", now=self.now)
        assert claim is not None
        for phase, barrier in (("claimed", "held"), ("publishing_topic", "held"), ("publishing_topic", "unknown")):
            self.store.schedules.set_run(claim.run.id, phase=phase, barrier=barrier)
            result = await self.service.manage({"mode": "list", "ended": False}, native_thread_id="native-a")
            current, = result["plans"]
            self.assertFalse(current["lifecycle"]["ended"])
            self.assertFalse(current["lifecycle"]["has_trigger"])
            self.assertEqual(current["execution"]["status"], "unknown" if barrier == "unknown" else "starting")
        # An explicit view retains the existing recovery entry, then snapshots
        # the release. It reuses that exact observation instead of reading twice.
        scheduled, run_id, binding = await self.run_fixture(schedule={"kind": "once", "at": "2026-09-08T08:02+00:00"})
        self.runtime.states[binding.id, "initial-turn"] = "failed"
        self.runtime.calls.clear()
        detail = await self.service.manage({"mode": "view", "plan_id": scheduled["id"]})
        self.assertTrue(detail["plan"]["lifecycle"]["ended"])
        self.assertEqual(detail["plan"]["execution"]["status"], "failed")
        self.assertEqual(detail["plan"]["execution"]["kind"], "latest")
        self.assertTrue(detail["plan"]["execution"]["is_last"])
        self.assertEqual(self.store.schedules.get_run(run_id).barrier, "released")
        self.assertEqual(self.runtime.calls, [(binding.id, "initial-turn")])
        history = await self.service.manage({"mode": "runs", "plan_id": scheduled["id"]})
        self.assertEqual(history["runs"][0]["status"], "failed")
        self.assertEqual(history["runs"][0]["due_local"], detail["plan"]["execution"]["due_local"])
        self.assertTrue(history["runs"][0]["is_last"])

    async def test_list_keeps_filter_snapshot_when_normal_completion_arrives_during_read(self):
        plan, run_id, binding = await self.run_fixture(schedule={"kind": "once", "at": "2026-09-08T08:01+00:00"})

        async def completing_reader(binding_id, turn_id, *, deadline):
            self.assertEqual((binding_id, turn_id), (binding.id, "initial-turn"))
            self.store.release_scheduled_initial_turn(binding_id, turn_id)
            self.now += 1
            await asyncio.sleep(0)
            return "completed"

        self.runtime.read_scheduled_turn = completing_reader
        result = await self.service.manage({"mode": "list", "ended": False}, native_thread_id="native-a")
        current, = result["plans"]
        self.assertFalse(current["lifecycle"]["ended"])
        self.assertEqual(current["execution"]["status"], "completed")
        self.assertEqual(result["snapshot_at"], self.now - 1)
        self.assertEqual(self.store.schedules.get_run(run_id).barrier, "released")
        self.assertEqual((await self.service.manage({"mode": "list", "ended": False}, native_thread_id="native-a"))["plans"], [])

    async def test_list_native_reads_only_current_page_with_bounded_concurrency_and_deadline(self):
        fixtures = [await self.run_fixture() for _ in range(6)]
        expected = {binding.id for _plan, _run, binding in sorted(fixtures, key=lambda item: item[0]["id"])[:2]}
        self.runtime.calls.clear()
        first = await self.service.manage({"mode": "list", "limit": 2}, native_thread_id="native-a")
        self.assertIsNotNone(first["next_cursor"])
        self.assertEqual({binding_id for binding_id, _turn_id in self.runtime.calls}, expected)
        active = maximum = 0

        async def yielding_reader(binding_id, turn_id, *, deadline):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            try:
                await asyncio.sleep(0.001)
                return "inProgress"
            finally:
                active -= 1

        self.runtime.read_scheduled_turn = yielding_reader
        result = await self.service.manage({"mode": "list"}, native_thread_id="native-a")
        self.assertEqual(len(result["plans"]), 6)
        self.assertGreater(maximum, 1)
        self.assertLessEqual(maximum, 4)

        async def blocked_reader(binding_id, turn_id, *, deadline):
            await asyncio.Event().wait()

        self.runtime.read_scheduled_turn = blocked_reader
        with patch("netizen.schedules.service._READ_TIMEOUT_SECONDS", 0.01):
            timed_out = await asyncio.wait_for(self.service.manage({"mode": "list"}, native_thread_id="native-a"), 1)
        self.assertEqual({item["execution"]["status"] for item in timed_out["plans"]}, {"unavailable"})
        self.assertTrue(all(self.store.schedules.get_run(run_id).barrier == "held" for _plan, run_id, _binding in fixtures))

    async def test_runs_read_exact_initial_turn_and_return_committed_release(self):
        plan, run_id, binding = await self.run_fixture()
        self.runtime.states[binding.id, "initial-turn"] = "completed"
        self.runtime.states[binding.id, "later-manual-turn"] = "inProgress"
        response = await self.service.manage({"mode": "runs", "plan_id": plan["id"]})
        self.assertTrue(response["ok"], response)
        self.assertEqual(self.runtime.calls, [(binding.id, "initial-turn")])
        self.assertEqual(response["runs"][0]["status"], "completed")
        self.assertEqual(self.store.schedules.get_run(run_id).barrier, "released")
        self.assertEqual(response["runs"][0]["barrier"], "released")
        self.assertEqual(response["runs"][0]["phase"], "released")
        self.assertEqual(response["runs"][0]["native_thread_id"], binding.native_thread_id)
        self.assertIn("messageId=root", response["runs"][0]["feishu_url"])

    async def test_released_run_read_failure_does_not_reoccupy_the_barrier(self):
        plan, run_id, binding = await self.run_fixture()
        self.store.release_scheduled_initial_turn(binding.id, "initial-turn")
        self.runtime.states[binding.id, "initial-turn"] = OSError("history unavailable")
        response = await self.service.manage({"mode": "runs", "plan_id": plan["id"]})
        self.assertEqual(response["runs"][0]["status"], "unavailable")
        self.assertEqual(self.store.schedules.get_run(run_id).barrier, "released")
        self.store.delete_binding(binding.id)
        self.runtime.calls.clear()
        removed = await self.service.manage({"mode": "runs", "plan_id": plan["id"]})
        self.assertEqual(removed["runs"][0]["status"], "deleted")
        self.assertEqual(self.runtime.calls, [])

    async def test_pending_read_failure_becomes_unknown(self):
        plan, run_id, binding = await self.run_fixture()
        self.runtime.states[binding.id, "initial-turn"] = OSError("history unavailable")
        response = await self.service.manage({"mode": "runs", "plan_id": plan["id"]})
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["runs"][0]["status"], "unavailable")
        self.assertEqual(response["runs"][0]["barrier"], "unknown")
        self.assertEqual(self.store.schedules.get_run(run_id).barrier, "unknown")

    async def test_scheduler_refresh_result_is_reused_without_a_second_native_read(self):
        plan, run_id, _binding = await self.run_fixture()
        refreshed = []

        async def refresh(plan_id):
            refreshed.append(plan_id)
            self.store.schedules.release(run_id)
            return "completed"

        self.service.set_refresh_handler(refresh)
        invalid = await self.service.manage({"mode": "runs", "plan_id": plan["id"], "cursor": "invalid"})
        self.assertFalse(invalid["ok"])
        self.assertEqual(refreshed, [])
        self.assertEqual(self.store.schedules.get_run(run_id).barrier, "held")
        response = await self.service.manage({"mode": "runs", "plan_id": plan["id"]})
        self.assertTrue(response["ok"], response)
        self.assertEqual(refreshed, [plan["id"]])
        self.assertEqual(self.runtime.calls, [])
        self.assertEqual(response["runs"][0]["status"], "completed")
        self.assertEqual(response["runs"][0]["barrier"], "released")


if __name__ == "__main__":
    unittest.main()
