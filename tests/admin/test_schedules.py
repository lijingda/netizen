from __future__ import annotations

import asyncio
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

from tests.admin import test_web as fixture
from netizen.management import ProjectDeletionResult
from netizen.management import InstanceManagementService, ScopeCoordinator
from netizen.bindings import BindingStore
from netizen.projects import ProjectRegistry
from netizen.schedules.service import ScheduleService
from netizen.model_settings import EffortOption, ModelCatalog, ModelOption, ServiceTierOption
from tests.management.test_chat_labels import FakeChatInfo, FakeChatLabelProvider, FakeChatMember
from tests.management.test_service import FakeManagementRuntime


class FakeSchedules:
    def close_admission(self) -> None:
        pass

    def __init__(self) -> None:
        self.calls: list[tuple[dict, str]] = []
        self.previews: list[dict] = []
        self.plan = {
            "id": "plan-exact", "revision": 7, "name": "Daily report",
            "instructions": "Read the project.\nSummarize the changes.",
            "project_alias": "test", "app_id": "cli_test", "chat_id": "oc_target",
            "enabled": True,
            "schedule": {"kind": "daily", "at": "09:00", "timezone": "Asia/Shanghai"},
            "next_due_at": 1893488400,
        }
        self.error: dict | None = None
        self.plans: list[dict] | None = None
        self.entered: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    async def preview(self, request: dict) -> dict:
        self.previews.append(copy.deepcopy(request))
        return {
            "ok": True, "schedule": request["schedule"],
            "preview": [{"utc": "2030-01-01T01:00:00Z", "local": "2030-01-01T09:00:00+08:00"}],
        }

    async def manage(self, request: dict, *, source: str) -> dict:
        self.calls.append((copy.deepcopy(request), source))
        if self.entered is not None:
            self.entered.set()
            assert self.release is not None
            await self.release.wait()
        if self.error is not None:
            return {"ok": False, "error": self.error}
        mode = request["mode"]
        if mode == "options":
            return {"ok": True, "models": [], "session_settings": {
                "turn_settings": None, "reaction_pulse_enabled": False,
                "progress_card_enabled": False, "message_context_mode": "current-only",
            }, "context_mode_available": False, "model_catalog_error": {
                "code": "model_catalog_unavailable", "message": "模型目录暂不可用。",
            }}
        if mode == "list":
            return {"ok": True, "plans": copy.deepcopy(self.plans) if self.plans is not None else [copy.deepcopy(self.plan)],
                    "next_cursor": "next-page", "default_timezone": "Asia/Shanghai"}
        if mode == "runs":
            return {"ok": True, "runs": [{"id": "run-id", "binding_id": "binding-exact",
                    "due_at": 1893488400, "status": "released",
                    "feishu_url": "https://applink.feishu.cn/client/message/link?token=om_root"}],
                    "next_cursor": None}
        if mode == "view" and self.plans is not None:
            plan = next(item for item in self.plans if item["id"] == request["plan_id"])
            return {"ok": True, "plan": copy.deepcopy(plan), "preview": [], "inflight": False}
        if mode in {"create", "update", "delete"}:
            if mode != "create" and request["expected_revision"] != self.plan["revision"]:
                return {"ok": False, "error": {"code": "revision_conflict", "message": "计划已变化，请刷新。"}}
            if mode == "delete":
                return {"ok": True, "id": request["plan_id"], "revision": self.plan["revision"] + 1}
            for key in ("name", "instructions", "chat_id", "schedule", "enabled", "session_settings"):
                if key in request:
                    self.plan[key] = request[key]
            if "project" in request:
                self.plan["project_alias"] = request["project"]
            self.plan["revision"] += 1
        return {"ok": True, "plan": copy.deepcopy(self.plan), "preview": [], "inflight": True}


class AdminSchedulesTest(unittest.IsolatedAsyncioTestCase):
    request = fixture.AdminWebTest.request
    login = fixture.AdminWebTest.login
    json_get = fixture.AdminWebTest.json_get
    json_post = fixture.AdminWebTest.json_post
    asyncTearDown = fixture.AdminWebTest.asyncTearDown

    async def asyncSetUp(self) -> None:
        await fixture.AdminWebTest.asyncSetUp(self)
        self.schedules = FakeSchedules()
        self.management.schedules = self.schedules
        self.runner.open_admission()

    async def page(self, session: str) -> dict:
        status, _, page = await self.json_get("/api/v1/schedules", session)
        self.assertEqual(status, 200, page)
        return page

    async def test_all_routes_use_existing_auth_origin_csrf_and_exact_target(self) -> None:
        for path in ("/api/v1/schedules", "/api/v1/schedules?mode=view&plan_id=plan-exact"):
            status, _, _ = await self.request("GET", path)
            self.assertEqual(status, 401)
        for mode in ("create", "update", "delete"):
            status, _, _ = await self.json_post(f"/api/v1/schedules/{mode}", "invalid", {})
            self.assertEqual(status, 401)
        self.assertEqual(self.schedules.calls, [])
        session = await self.login()
        page = await self.page(session)
        action = page["plans"][0]["actions"]["update"]
        payload = {**fixture._action_payload(action), "definition": {"enabled": False}}
        for rejected in (
            {**payload, "csrfToken": "invalid"},
            {**payload, "target": {**payload["target"], "targetId": "other-plan"}},
            {**payload, "definition": {"expected_revision": 99, "enabled": False}},
        ):
            status, _, _ = await self.json_post("/api/v1/schedules/update", session, rejected)
            self.assertIn(status, (400, 403, 409))
        self.assertEqual(len(self.schedules.calls), 1)
        page = await self.page(session)
        action = page["plans"][0]["actions"]["update"]
        payload = {**fixture._action_payload(action), "definition": {"enabled": False}}
        status, _, _ = await self.request("POST", "/api/v1/schedules/update", headers=[
            ("Cookie", f"netizen_admin_session={session}"),
            ("Origin", "https://foreign.example"), ("Content-Type", "application/json"),
        ], body=json.dumps(payload).encode())
        self.assertEqual(status, 403)
        status, _, _ = await self.json_post("/api/v1/schedules/update", await self.login(), payload)
        self.assertEqual(status, 409)
        status, _, result = await self.json_post("/api/v1/schedules/update", session, payload)
        self.assertEqual(status, 200, result)
        request, source = self.schedules.calls[-1]
        self.assertEqual(source, "admin")
        self.assertEqual({key: request[key] for key in ("mode", "plan_id", "expected_revision", "enabled")},
                         {"mode": "update", "plan_id": "plan-exact", "expected_revision": 7, "enabled": False})
        self.assertNotIn(action["actionToken"], request["request_id"])
        self.assertNotIn("native_thread_id", request)
        status, _, replay = await self.json_post("/api/v1/schedules/update", session, payload)
        self.assertEqual(status, 409, replay)
        self.assertEqual(len(self.schedules.calls), 3)

    async def test_queries_are_bounded_shared_service_calls_without_default_context(self) -> None:
        session = await self.login()
        status, _, page = await self.json_get(
            "/api/v1/schedules?chat_id=oc_other&project=test&enabled=false&ended=false&cursor=next-page", session,
        )
        self.assertEqual(status, 200, page)
        self.assertEqual(self.schedules.calls[-1], ({"mode": "list", "all": True,
            "chat_id": "oc_other", "project": "test", "enabled": False, "ended": False, "cursor": "next-page"}, "admin"))
        self.assertEqual(page["next_cursor"], "next-page")
        status, _, view = await self.json_get("/api/v1/schedules?mode=view&plan_id=plan-exact", session)
        self.assertEqual(status, 200)
        self.assertEqual(view["plan"]["instructions"], self.schedules.plan["instructions"])
        self.assertTrue(view["inflight"])
        status, _, runs = await self.json_get("/api/v1/schedules?mode=runs&plan_id=plan-exact&cursor=runs-page", session)
        self.assertEqual(status, 200)
        self.assertEqual(runs["runs"][0]["binding_id"], "binding-exact")
        before = len(self.schedules.calls)
        for query in ("mode=create", "all=false", "enabled=yes", "ended=yes", "ended=false&ended=true",
                      "mode=view&plan_id=x&enabled=true", "mode=view&plan_id=x&ended=false"):
            status, _, _ = await self.json_get(f"/api/v1/schedules?{query}", session)
            self.assertEqual(status, 400)
        self.assertEqual(len(self.schedules.calls), before)
        rule = self.schedules.plan["schedule"]
        query = urlencode({"mode": "preview", "schedule": json.dumps(rule)})
        status, _, preview = await self.json_get(f"/api/v1/schedules?{query}", session)
        self.assertEqual(status, 200, preview)
        self.assertEqual(self.schedules.previews, [{"schedule": rule}])
        self.assertEqual(len(self.schedules.calls), before)

    async def test_list_and_view_preserve_independent_lifecycle_and_execution_projection(self) -> None:
        session = await self.login()
        self.schedules.plan.update(
            enabled=False,
            lifecycle={"ended": False, "has_future": False, "has_trigger": False},
            execution={"kind": "current", "status": "inProgress", "is_last": True,
                       "run_id": "last-run", "due_at": 1893488400, "due_local": "2030-01-01T09:00+08:00"},
        )
        status, _, page = await self.json_get("/api/v1/schedules?ended=false", session)
        self.assertEqual(status, 200, page)
        status, _, detail = await self.json_get("/api/v1/schedules?mode=view&plan_id=plan-exact", session)
        self.assertEqual(status, 200, detail)
        for plan in (page["plans"][0], detail["plan"]):
            self.assertFalse(plan["enabled"])
            self.assertEqual(plan["lifecycle"], self.schedules.plan["lifecycle"])
            self.assertEqual(plan["execution"], self.schedules.plan["execution"])
            self.assertEqual(plan["actions"]["delete"]["target"]["targetId"], "plan-exact")

    async def test_create_preserves_multiline_instructions_and_delete_checks_revision(self) -> None:
        session = await self.login()
        action = (await self.page(session))["actions"]["create"]
        definition = {"name": "Report", "instructions": "First line.\nSecond line.",
                      "project": "test", "chat_id": "oc_target", "enabled": True,
                      "schedule": self.schedules.plan["schedule"]}
        status, _, result = await self.json_post("/api/v1/schedules/create", session,
            {**fixture._action_payload(action), "definition": definition})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["plan"]["instructions"], definition["instructions"])
        page = await self.page(session)
        action = page["plans"][0]["actions"]["delete"]
        self.schedules.plan["revision"] += 1
        status, _, error = await self.json_post("/api/v1/schedules/delete", session, fixture._action_payload(action))
        self.assertEqual(status, 409, error)
        self.assertEqual(error["code"], "revision_conflict")
        action = (await self.page(session))["plans"][0]["actions"]["delete"]
        status, _, result = await self.json_post("/api/v1/schedules/delete", session, fixture._action_payload(action))
        self.assertEqual(status, 200, result)
        self.assertEqual(result["id"], "plan-exact")

    async def test_management_errors_are_stable_and_do_not_log_instructions(self) -> None:
        session = await self.login()
        for code, status in (("not_found", 404), ("request_conflict", 409), ("unavailable", 503), ("invalid_schedule", 400)):
            self.schedules.error = {"code": code, "message": "管理请求无法完成。"}
            actual, _, result = await self.json_get("/api/v1/schedules?mode=view&plan_id=plan-exact", session)
            self.assertEqual(actual, status, result)
            self.assertEqual(result["code"], code)
        self.schedules.error = None
        action = (await self.page(session))["actions"]["create"]
        payload = {**fixture._action_payload(action), "definition": {"instructions": "private-secret-instruction"}}
        self.schedules.error = {"code": "invalid_input", "message": "任务参数无效。"}
        with self.assertLogs("netizen.admin.web", level="INFO") as captured:
            status, _, _ = await self.json_post("/api/v1/schedules/create", session, payload)
        self.assertEqual(status, 400)
        self.assertNotIn("private-secret-instruction", "".join(captured.output))
        self.assertIn('"terminal":"error"', "".join(captured.output))
        self.assertNotIn(action["actionToken"], "".join(captured.output))

    async def test_session_settings_options_preview_and_partial_updates_share_management(self) -> None:
        status, _, _ = await self.request("GET", "/api/v1/schedules?mode=options")
        self.assertEqual(status, 401)
        session = await self.login()
        status, _, options = await self.json_get("/api/v1/schedules?mode=options&chat_id=oc_private", session)
        self.assertEqual(status, 200, options)
        self.assertEqual(self.schedules.calls[-1], ({"mode": "options", "chat_id": "oc_private"}, "admin"))
        self.assertFalse(options["context_mode_available"])
        self.assertEqual(options["session_settings"]["message_context_mode"], "current-only")
        self.assertNotIn("actions", options)
        settings = {"turn_settings": {"model_id": "native-model", "effort_id": "high", "service_tier_id": "default"},
                    "reaction_pulse_enabled": True, "progress_card_enabled": True,
                    "message_context_mode": "catch-up"}
        rule = self.schedules.plan["schedule"]
        query = urlencode({"mode": "preview", "plan_id": "plan-exact", "chat_id": "oc_target",
                           "schedule": json.dumps(rule), "session_settings": json.dumps(settings)})
        status, _, preview = await self.json_get(f"/api/v1/schedules?{query}", session)
        self.assertEqual(status, 200, preview)
        self.assertEqual(self.schedules.previews[-1], {
            "plan_id": "plan-exact", "chat_id": "oc_target", "schedule": rule, "session_settings": settings,
        })
        action = (await self.page(session))["plans"][0]["actions"]["update"]
        status, _, result = await self.json_post("/api/v1/schedules/update", session, {
            **fixture._action_payload(action), "definition": {"session_settings": settings},
        })
        self.assertEqual(status, 200, result)
        self.assertEqual(self.schedules.calls[-1][0]["session_settings"], settings)
        self.assertEqual(self.schedules.calls[-1][0]["expected_revision"], 7)
        action = (await self.page(session))["plans"][0]["actions"]["update"]
        status, _, result = await self.json_post("/api/v1/schedules/update", session, {
            **fixture._action_payload(action), "definition": {"session_settings": {"progress_card_enabled": False}},
        })
        self.assertEqual(status, 200, result)
        self.assertEqual(self.schedules.calls[-1][0]["session_settings"], {"progress_card_enabled": False})
        self.assertNotIn("native_thread_id", self.schedules.calls[-1][0])

    async def test_project_delete_exposes_plans_and_unresolved_dispatch_counts(self) -> None:
        self.management.preview_project_delete = AsyncMock(return_value=SimpleNamespace(
            project=self.management.project_record, bindings=(), sides=(),
            scheduled_plans=(("plan-a", 2), ("plan-b", 5)), scheduled_runs=(object(),),
            fingerprint="exact-schedule-inventory",
        ))
        self.management.delete_project = AsyncMock(return_value=ProjectDeletionResult(
            project_alias="test", deleted=False, deleted_session_count=0,
            remaining_sessions=(), remaining_side_count=0,
            code="schedule_creation_in_progress", deleted_plan_count=2,
            remaining_scheduled_run_count=1,
        ))
        session = await self.login()
        status, _, projects = await self.json_get("/api/v1/projects", session)
        self.assertEqual(status, 200)
        action = projects["items"][0]["actions"]["previewDelete"]
        status, _, preview = await self.json_post("/api/v1/projects/delete-preview", session,
                                                 fixture._action_payload(action))
        self.assertEqual(status, 200, preview)
        self.assertEqual(preview["scheduledPlanCount"], 2)
        self.assertEqual(preview["scheduledRunCount"], 1)
        status, _, result = await self.json_post("/api/v1/projects/delete", session,
            fixture._action_payload(preview["actions"]["delete"]))
        self.assertEqual(status, 200, result)
        self.assertFalse(result["deleted"])
        self.assertEqual(result["deletedPlanCount"], 2)
        self.assertEqual(result["remainingScheduledRunCount"], 1)
        self.assertIn("不会恢复计划", result["message"])

    async def test_schedule_target_labels_share_session_cache_and_keep_exact_plan_actions(self) -> None:
        store = BindingStore(self.root / "chat-labels.sqlite3")
        self.addAsyncCleanup(store.aclose)
        provider = FakeChatLabelProvider()
        provider.info = {
            "oc_group": FakeChatInfo("Engineering", "group", "private"),
            "oc_direct": FakeChatInfo("", "p2p", "private"),
            "oc_unshown": FakeChatInfo("Outside this page", "group"),
        }
        provider.members["oc_direct"] = [FakeChatMember("ou_alice", "Alice")]
        management = InstanceManagementService(
            bindings=store, projects=ProjectRegistry(store=store, project_root=self.root, projects={"test": self.root}),
            runtime=FakeManagementRuntime(store), scope_coordinator=ScopeCoordinator(), chat_labels=provider,
        )
        self.addAsyncCleanup(management.close)
        management.schedules = self.schedules
        self.runner.application._management = management
        self.schedules.plans = [
            {**self.schedules.plan, "id": f"plan-{number}", "chat_id": chat_id}
            for number, chat_id in enumerate(("oc_group", "oc_group", "oc_direct", "oc_missing"))
        ]
        # Sessions already use this public boundary and the same cache; the
        # schedule adapter must not construct another resolver per request.
        labels = await management.resolve_chat_labels(("oc_group",), deadline=asyncio.get_running_loop().time() + 1)
        self.assertEqual(labels["oc_group"].display_name, "Engineering")
        session = await self.login()
        page = await self.page(session)
        first = page["plans"][0]
        self.assertEqual(first["chat"], {
            "chatId": "oc_group", "chatLabel": "Engineering", "chatLabelResolved": True,
            "chatMode": "group", "chatType": "private",
            "chatOpenUrl": "https://applink.feishu.cn/client/chat/open?openChatId=oc_group",
        })
        self.assertEqual(first["chat_id"], "oc_group")
        self.assertEqual(first["actions"]["update"]["target"]["targetId"], "plan-0")
        self.assertEqual(first["revision"], self.schedules.plan["revision"])
        direct = page["plans"][2]["chat"]
        self.assertEqual(direct["chatLabel"], "Alice")
        self.assertEqual(direct["chatOpenUrl"], "https://applink.feishu.cn/client/chat/open?openId=ou_alice")
        missing = page["plans"][3]["chat"]
        self.assertEqual(missing["chatLabel"], "oc_missing")
        self.assertFalse(missing["chatLabelResolved"])
        self.assertEqual(missing["chatOpenUrl"], "https://applink.feishu.cn/client/chat/open?openChatId=oc_missing")
        status, _, detail = await self.json_get("/api/v1/schedules?mode=view&plan_id=plan-2", session)
        self.assertEqual(status, 200, detail)
        self.assertEqual(detail["plan"]["chat"], direct)
        await self.page(session)
        self.assertCountEqual(provider.info_calls, ["oc_group", "oc_direct", "oc_missing"])
        self.assertEqual(provider.member_calls, ["oc_direct"])

    async def test_recurring_cutoff_picker_resolves_timezone_dst_and_existing_offset(self) -> None:
        session = await self.login()

        async def preview(local_end_at, *, timezone="America/New_York", **extra):
            query = urlencode({"mode": "preview", "schedule": json.dumps({"kind": "daily", "at": "09:00",
                "timezone": timezone}), "local_end_at": local_end_at, **extra})
            return await self.json_get(f"/api/v1/schedules?{query}", session)

        status, _, result = await preview("2030-01-01T09:00", timezone="Asia/Shanghai")
        self.assertEqual(status, 200, result)
        rule = self.schedules.previews[-1]["schedule"]
        self.assertEqual(rule["end_at"], "2030-01-01T09:00+08:00")
        self.assertNotIn("local_end_at", self.schedules.previews[-1])
        status, _, result = await preview("2030-03-10T02:30")
        self.assertEqual(status, 400, result)
        status, _, result = await preview("2030-11-03T01:30")
        self.assertEqual(status, 400, result)
        self.assertEqual(result["code"], "ambiguous_end_time")
        self.assertEqual([choice["utc_offset"] for choice in result["choices"]], ["-04:00", "-05:00"])
        status, _, result = await preview("2030-11-03T01:30", end_utc_offset="-05:00")
        self.assertEqual(status, 200, result)
        self.assertEqual(self.schedules.previews[-1]["schedule"]["end_at"], "2030-11-03T01:30-05:00")
        self.schedules.plan["schedule"] = {"kind": "daily", "at": "09:00", "timezone": "America/New_York",
            "end_at": "2030-11-03T01:30-05:00"}
        status, _, result = await preview("2030-11-03T01:30", plan_id="plan-exact")
        self.assertEqual(status, 200, result)
        self.assertEqual(self.schedules.previews[-1]["schedule"]["end_at"], "2030-11-03T01:30-05:00")
        detail_status, _, detail = await self.json_get("/api/v1/schedules?mode=view&plan_id=plan-exact", session)
        self.assertEqual(detail_status, 200, detail)
        self.assertEqual(detail["plan"]["end_local_at"], "2030-11-03T01:30")
        # Neither conflicting canonical/local values nor cutoff on a once rule is accepted.
        for schedule in ({"kind": "once", "timezone": "UTC", "at": "2030-01-01T09:00+00:00"},
                         dict(self.schedules.plan["schedule"])):
            query = urlencode({"mode": "preview", "schedule": json.dumps(schedule), "local_end_at": "2030-01-01T09:00"})
            status, _, result = await self.json_get(f"/api/v1/schedules?{query}", session)
            self.assertEqual(status, 400, result)

    async def test_local_once_preview_returns_dst_choices_and_preserves_original_second_occurrence(self) -> None:
        session = await self.login()

        async def preview(local_at, *, timezone="America/New_York", **extra):
            query = urlencode({"mode": "preview", "schedule": json.dumps({"kind": "once", "timezone": timezone}),
                               "local_at": local_at, **extra})
            return await self.json_get(f"/api/v1/schedules?{query}", session)

        status, _, result = await preview("2030-01-01T09:00", timezone="Asia/Shanghai")
        self.assertEqual(status, 200, result)
        self.assertEqual(self.schedules.previews[-1]["schedule"]["at"], "2030-01-01T09:00+08:00")
        self.assertNotIn("local_at", self.schedules.previews[-1])
        status, _, result = await preview("2030-03-10T02:30")
        self.assertEqual(status, 400, result)
        self.assertIn("没有所选时间", result["message"])
        status, _, result = await preview("2030-11-03T01:30")
        self.assertEqual(status, 400, result)
        self.assertEqual(result["code"], "ambiguous_local_time")
        self.assertEqual(result["choices"], [
            {"utc_offset": "-04:00", "utc": "2030-11-03T05:30+00:00"},
            {"utc_offset": "-05:00", "utc": "2030-11-03T06:30+00:00"},
        ])
        status, _, result = await preview("2030-11-03T01:30", utc_offset="-05:00")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["schedule"]["at"], "2030-11-03T01:30-05:00")
        status, _, result = await preview("2030-11-03T01:30", utc_offset="+08:00")
        self.assertEqual(status, 400, result)
        self.schedules.plan["schedule"] = {"kind": "once", "timezone": "America/New_York", "at": "2030-11-03T06:30+00:00"}
        status, _, detail = await self.json_get("/api/v1/schedules?mode=view&plan_id=plan-exact", session)
        self.assertEqual(status, 200, detail)
        self.assertEqual(detail["plan"]["once_local_at"], "2030-11-03T01:30")
        status, _, result = await preview("2030-11-03T01:30", plan_id="plan-exact")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["schedule"]["at"], "2030-11-03T06:30+00:00")
        status, _, result = await preview("2030-11-03T01:30", plan_id="plan-exact", utc_offset="-04:00")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["schedule"]["at"], "2030-11-03T01:30-04:00")
        status, _, result = await preview("2030-11-03T01:31", plan_id="plan-exact")
        self.assertEqual(status, 400, result)
        self.assertEqual(result["code"], "ambiguous_local_time")

    async def test_real_schedule_service_round_trip_from_preview_to_revisioned_delete(self) -> None:
        store = BindingStore(self.root / "schedules.sqlite3")
        self.addAsyncCleanup(store.aclose)
        store.register_project(alias="test", cwd=str(self.root))
        runtime = SimpleNamespace(model_catalog=AsyncMock(return_value=ModelCatalog((ModelOption(
            "native-model", "native-model", "Native model", "", True, "medium", "default",
            (EffortOption("medium", "Medium", "medium"), EffortOption("high", "High", "high")),
            (ServiceTierOption("priority", "Fast", ""),),
        ),))))
        self.management.schedules = ScheduleService(
            bindings=store, runtime=runtime, app_id="cli_test",
            chat_info=SimpleNamespace(get_chat_info=AsyncMock(return_value=SimpleNamespace(chat_type="group"))),
            wall_clock=lambda: 1893456000, default_timezone="Asia/Shanghai",
        )
        session = await self.login()
        page = await self.page(session)
        self.assertEqual(page["plans"], [])
        self.assertEqual(page["default_timezone"], "Asia/Shanghai")
        rule = {"kind": "interval", "timezone": "Asia/Shanghai", "every_minutes": 90}
        query = urlencode({"mode": "preview", "schedule": json.dumps(rule)})
        status, _, preview = await self.json_get(f"/api/v1/schedules?{query}", session)
        self.assertEqual(status, 200, preview)
        self.assertEqual(len(preview["preview"]), 3)
        self.assertEqual(preview["schedule"]["anchor"], 1893456000)
        status, _, created = await self.json_post("/api/v1/schedules/create", session, {
            **fixture._action_payload(page["actions"]["create"]), "definition": {
                "name": "Report", "instructions": "One.\nTwo.", "project": "test",
                "chat_id": "oc_explicit", "schedule": preview["schedule"], "enabled": False,
            },
        })
        self.assertEqual(status, 200, created)
        self.assertEqual(created["plan"]["instructions"], "One.\nTwo.")
        self.assertEqual(created["plan"]["schedule"], preview["schedule"])
        defaults = created["plan"]["session_settings"]
        self.assertEqual(defaults, {
            "turn_settings": {"model_id": "native-model", "effort_id": "medium", "service_tier_id": "default"},
            "reaction_pulse_enabled": False, "progress_card_enabled": True, "message_context_mode": "current-only",
        })
        plan = (await self.page(session))["plans"][0]
        status, _, updated = await self.json_post("/api/v1/schedules/update", session, {
            **fixture._action_payload(plan["actions"]["update"]), "definition": {"enabled": True},
        })
        self.assertEqual(status, 200, updated)
        self.assertEqual(updated["plan"]["revision"], plan["revision"] + 1)
        self.assertEqual(updated["plan"]["session_settings"], defaults)
        status, _, stale = await self.json_post("/api/v1/schedules/delete", session,
                                              fixture._action_payload(plan["actions"]["delete"]))
        self.assertEqual(status, 409, stale)
        # A catalog outage must not prevent changing unrelated feedback or
        # cause the adapter to silently replace an existing explicit model.
        runtime.model_catalog.side_effect = RuntimeError("catalog temporarily unavailable")
        plan = (await self.page(session))["plans"][0]
        status, _, configured = await self.json_post("/api/v1/schedules/update", session, {
            **fixture._action_payload(plan["actions"]["update"]),
            "definition": {"session_settings": {"progress_card_enabled": False}},
        })
        self.assertEqual(status, 200, configured)
        self.assertEqual(configured["plan"]["session_settings"], {**defaults, "progress_card_enabled": False})
        plan = (await self.page(session))["plans"][0]
        status, _, deleted = await self.json_post("/api/v1/schedules/delete", session,
                                                fixture._action_payload(plan["actions"]["delete"]))
        self.assertEqual(status, 200, deleted)
        self.assertEqual(deleted["plan_id"], plan["id"])
        self.assertEqual((await self.page(session))["plans"], [])
