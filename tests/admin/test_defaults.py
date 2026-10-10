from __future__ import annotations

import asyncio
import copy
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from netizen_cli.bindings import BindingStore
from netizen_cli.defaults import DefaultConfigurationError
from netizen_cli.defaults.service import SessionDefaultsService
from netizen_cli.management import InstanceManagementService, ScopeCoordinator
from netizen_cli.projects import ProjectRegistry
from tests.admin import test_web as fixture
from tests.management.test_chat_labels import FakeChatInfo, FakeChatLabelProvider, FakeChatMember
from tests.support.chat_targets import FakeChatTargetDirectory


class FakeDefaults:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.order_revision = 4
        settings = {"turn_settings": None, "reaction_pulse_enabled": False,
                    "progress_card_enabled": True, "completion_mention_enabled": True,
                    "message_context_mode": "current-only"}
        self.records = [
            {"id": "exact-one", "kind": "chat", "chat_id": "oc_one", "keyword": None,
             "project": "test", "session_settings": settings, "revision": 3, "position": None},
            *[{"id": f"rule-{index}", "kind": "group_name", "chat_id": None, "keyword": keyword,
               "project": "test", "session_settings": settings, "revision": 2, "position": index}
              for index, keyword in enumerate(("Oncall", "Payments"))],
        ]

    async def manage(self, request: dict) -> dict:
        self.calls.append(copy.deepcopy(request))
        mode = request["mode"]
        if mode == "options":
            return {"models": [], "session_settings": self.records[0]["session_settings"],
                    "context_mode_available": False, "model_catalog_error": "暂不可用"}
        if mode == "list":
            return {"items": copy.deepcopy([item for item in self.records if item["kind"] == request.get("kind", "chat")]),
                    "offset": request.get("offset", 0), "has_more": False, "order_revision": self.order_revision}
        if mode == "view":
            exact = next((item for item in self.records if item["chat_id"] == request["chat_id"]), None)
            return {"exact": copy.deepcopy(exact), "effective": copy.deepcopy(exact or self.records[1]),
                    "chat_kind": "group", "match_error": None}
        if mode == "reorder":
            if request["order_revision"] != self.order_revision:
                raise DefaultConfigurationError("群名规则顺序已变化。", code="revision_conflict")
            ids = [item["id"] for item in self.records if item["kind"] == "group_name"]
            if sorted(request["rule_ids"]) != sorted(ids):
                raise DefaultConfigurationError("排序必须包含全部规则。")
            self.order_revision += 1
            return {"order_revision": self.order_revision}
        if "id" in request:
            record = next(item for item in self.records if item["id"] == request["id"])
            if request["expected_revision"] != record["revision"]:
                raise DefaultConfigurationError("默认配置已变化。", code="revision_conflict")
            if mode == "delete":
                self.records.remove(record)
                return {"deleted": record["id"]}
            return {"rule": {**record, **request, "revision": record["revision"] + 1}}
        return {"rule": {**request, "id": "new-default", "revision": 1}}


class AdminDefaultsTest(unittest.IsolatedAsyncioTestCase):
    request = fixture.AdminWebTest.request
    login = fixture.AdminWebTest.login
    json_get = fixture.AdminWebTest.json_get
    json_post = fixture.AdminWebTest.json_post
    asyncTearDown = fixture.AdminWebTest.asyncTearDown

    async def asyncSetUp(self) -> None:
        await fixture.AdminWebTest.asyncSetUp(self)
        self.defaults = FakeDefaults()
        self.management.defaults = self.defaults
        self.runner.open_admission()

    async def page(self, session: str, kind: str = "chat") -> dict:
        status, _, result = await self.json_get(f"/api/v1/defaults?kind={kind}", session)
        self.assertEqual(status, 200, result)
        return result

    async def test_auth_origin_csrf_target_session_and_replay_are_enforced(self) -> None:
        for method, path in (("GET", "/api/v1/defaults"), ("POST", "/api/v1/defaults/save"),
                             ("POST", "/api/v1/defaults/delete"), ("POST", "/api/v1/defaults/reorder")):
            status, _, _ = await self.request(method, path)
            self.assertEqual(status, 401)
        self.assertEqual(self.defaults.calls, [])
        session = await self.login()
        action = (await self.page(session))["items"][0]["actions"]["save"]
        payload = {**fixture._action_payload(action), "definition": {"project": "changed"}}
        status, _, _ = await self.json_post("/api/v1/defaults/save", session, {**payload, "csrfToken": "invalid"})
        self.assertEqual(status, 403)
        status, _, _ = await self.json_post("/api/v1/defaults/save", session,
                                          {**payload, "target": {**payload["target"], "targetId": "other"}})
        self.assertEqual(status, 409)
        status, _, _ = await self.request("POST", "/api/v1/defaults/save", headers=[
            ("Cookie", f"netizen_admin_session={session}"), ("Origin", "https://foreign.example"),
            ("Content-Type", "application/json")], body=json.dumps(payload).encode())
        self.assertEqual(status, 403)
        status, _, _ = await self.json_post("/api/v1/defaults/save", await self.login(), payload)
        self.assertEqual(status, 409)
        self.assertEqual(len(self.defaults.calls), 1)
        status, _, result = await self.json_post("/api/v1/defaults/save", session, payload)
        self.assertEqual(status, 200, result)
        self.assertEqual(self.defaults.calls[-1], {"mode": "save", "id": "exact-one", "expected_revision": 3, "project": "changed"})
        status, _, _ = await self.json_post("/api/v1/defaults/save", session, payload)
        self.assertEqual(status, 409)
        self.assertEqual(len(self.defaults.calls), 2)

    async def test_new_records_and_updates_use_distinct_revision_preconditions(self) -> None:
        session = await self.login()
        page = await self.page(session)
        action = page["actions"]["create"]
        definition = {"kind": "chat", "chat_id": "oc_new", "project": "test",
                      "session_settings": self.defaults.records[0]["session_settings"]}
        status, _, result = await self.json_post("/api/v1/defaults/save", session,
                                               {**fixture._action_payload(action), "definition": definition})
        self.assertEqual(status, 200, result)
        self.assertEqual(self.defaults.calls[-1], {**definition, "mode": "save", "expected_revision": None})
        action = page["items"][0]["actions"]["save"]
        self.defaults.records[0]["revision"] += 1
        status, _, result = await self.json_post("/api/v1/defaults/save", session,
            {**fixture._action_payload(action), "definition": definition})
        self.assertEqual(status, 409, result)
        self.assertEqual(result["code"], "revision_conflict")
        action = (await self.page(session))["items"][0]["actions"]["delete"]
        status, _, result = await self.json_post("/api/v1/defaults/delete", session, fixture._action_payload(action))
        self.assertEqual(status, 200, result)
        self.assertEqual(result["deleted"], "exact-one")

    async def test_reorder_uses_server_order_revision_and_rejects_stale_order(self) -> None:
        session = await self.login()
        page = await self.page(session, "group_name")
        action = page["actions"]["reorder"]
        payload = {**fixture._action_payload(action), "rule_ids": ["rule-1", "rule-0"]}
        status, _, result = await self.json_post("/api/v1/defaults/reorder", session, payload)
        self.assertEqual(status, 200, result)
        self.assertEqual(self.defaults.calls[-1], {"mode": "reorder", "rule_ids": ["rule-1", "rule-0"], "order_revision": 4})
        page = await self.page(session, "group_name")
        self.defaults.order_revision += 1
        status, _, result = await self.json_post("/api/v1/defaults/reorder", session,
            {**fixture._action_payload(page["actions"]["reorder"]), "rule_ids": ["rule-1", "rule-0"]})
        self.assertEqual(status, 409, result)

    async def test_queries_support_paging_effective_values_and_unavailable_models(self) -> None:
        session = await self.login()
        status, _, result = await self.json_get("/api/v1/defaults", session)
        self.assertEqual(status, 200, result)
        self.assertEqual(self.defaults.calls[-1], {"mode": "list", "kind": "chat"})
        status, _, result = await self.json_get("/api/v1/defaults?kind=chat&offset=50&limit=50", session)
        self.assertEqual(status, 200, result)
        self.assertEqual(self.defaults.calls[-1], {"mode": "list", "kind": "chat", "offset": 50, "limit": 50})
        status, _, result = await self.json_get("/api/v1/defaults?mode=view&chat_id=oc_new", session)
        self.assertEqual(status, 200, result)
        self.assertIsNone(result["exact"])
        self.assertEqual(result["effective"]["id"], "rule-0")
        self.assertIn("create", result["actions"])
        status, _, result = await self.json_get("/api/v1/defaults?mode=options&chat_id=oc_one", session)
        self.assertEqual(status, 200, result)
        self.assertFalse(result["context_mode_available"])
        self.assertEqual(result["model_catalog_error"], "暂不可用")

    async def test_chat_labels_share_management_cache_and_preserve_exact_actions(self) -> None:
        store = BindingStore(self.root / "default-chat-labels.sqlite3")
        self.addAsyncCleanup(store.aclose)
        provider = FakeChatLabelProvider()
        provider.info = {
            "oc_group": FakeChatInfo("Engineering", "group"),
            "oc_direct": FakeChatInfo("", "p2p"),
            "oc_unshown": FakeChatInfo("Outside this page", "group"),
        }
        provider.members["oc_direct"] = [FakeChatMember("ou_alice", "Alice")]
        management = InstanceManagementService(
            bindings=store, projects=ProjectRegistry(store=store, project_root=self.root, projects={"test": self.root}),
            runtime=SimpleNamespace(), scope_coordinator=ScopeCoordinator(), chat_labels=provider,
        )
        self.addAsyncCleanup(management.close)
        management.defaults = self.defaults
        self.runner.application._management = management
        template = self.defaults.records[0]
        self.defaults.records = [
            *[{**template, "id": f"exact-{index}", "chat_id": chat_id}
              for index, chat_id in enumerate(("oc_group", "oc_direct", "oc_missing"))],
            *self.defaults.records[1:],
        ]
        await management.resolve_chat_labels(("oc_group",), deadline=asyncio.get_running_loop().time() + 1)
        session = await self.login()
        page = await self.page(session)
        group, direct, missing = page["items"]
        self.assertEqual(group["chat"], {
            "chatId": "oc_group", "chatLabel": "Engineering", "chatLabelResolved": True,
            "chatMode": "group", "chatType": "private",
            "chatOpenUrl": "https://applink.feishu.cn/client/chat/open?openChatId=oc_group",
        })
        self.assertEqual(group["chat_id"], "oc_group")
        self.assertEqual(group["revision"], template["revision"])
        for mode in ("save", "delete"):
            self.assertEqual(group["actions"][mode]["target"]["targetId"], "exact-0")
        self.assertEqual(direct["chat"]["chatLabel"], "Alice")
        self.assertEqual(direct["chat"]["chatOpenUrl"], "https://applink.feishu.cn/client/chat/open?openId=ou_alice")
        self.assertEqual(missing["chat"]["chatLabel"], "oc_missing")
        self.assertFalse(missing["chat"]["chatLabelResolved"])
        self.assertEqual(missing["chat"]["chatOpenUrl"], "https://applink.feishu.cn/client/chat/open?openChatId=oc_missing")
        await self.page(session)
        rules = await self.page(session, "group_name")
        self.assertTrue(all("chat" not in rule for rule in rules["items"]))
        self.assertCountEqual(provider.info_calls, ["oc_group", "oc_direct", "oc_missing"])
        self.assertEqual(provider.member_calls, ["oc_direct"])

    async def test_default_page_queries_share_one_reader_without_rejecting_concurrency(self) -> None:
        store = BindingStore(self.root / "concurrent-defaults.sqlite3")
        projects = ProjectRegistry(
            store=store, project_root=self.root, projects={"database-project": self.root},
        )
        management = InstanceManagementService(
            bindings=store, projects=projects, runtime=SimpleNamespace(),
            scope_coordinator=ScopeCoordinator(), root=self.root,
        )
        management.enable_defaults(app_id="cli_test")
        self.runner.application._management = management
        session = await self.login()
        loop = asyncio.get_running_loop()
        reader_entered = asyncio.Event()
        all_queries_submitted = asyncio.Event()
        release_reader = threading.Event()
        submit_query = store._submit_query
        submitted = 0

        async def submit_with_barrier(operation, *, deadline_seconds):
            nonlocal submitted
            submitted += 1
            if submitted == 3:
                all_queries_submitted.set()
            if submitted == 1:
                def held_read(connection):
                    loop.call_soon_threadsafe(reader_entered.set)
                    if not release_reader.wait(5):
                        raise AssertionError("concurrent HTTP queries never reached the reader")
                    return operation(connection)

                operation_to_submit = held_read
            else:
                operation_to_submit = operation
            return await submit_query(operation_to_submit, deadline_seconds=deadline_seconds)

        requests = []
        try:
            with patch.object(store, "_submit_query", submit_with_barrier):
                requests.append(asyncio.create_task(self.json_get(
                    "/api/v1/defaults?kind=chat&offset=0&limit=50", session,
                )))
                await asyncio.wait_for(reader_entered.wait(), timeout=5)
                requests.extend(asyncio.create_task(self.json_get(path, session)) for path in (
                    "/api/v1/defaults?kind=group_name&limit=200",
                    "/api/v1/projects/options?pageSize=50",
                ))
                # Both later handlers reach the real Store while the first SQL
                # worker is held; success cannot depend on fast query timing.
                await asyncio.wait_for(all_queries_submitted.wait(), timeout=5)
                release_reader.set()
                responses = await asyncio.wait_for(asyncio.gather(*requests), timeout=5)
            self.assertEqual([status for status, _, _ in responses], [200, 200, 200], responses)
            self.assertEqual(responses[0][2]["items"], [])
            self.assertEqual(responses[1][2]["items"], [])
            self.assertEqual(responses[2][2]["items"], [{"alias": "database-project", "enabled": True}])
        finally:
            release_reader.set()
            await asyncio.gather(*requests, return_exceptions=True)
            await management.close()
            await store.aclose()

    async def test_prompt_revision_and_bulk_mutations_are_not_accepted(self) -> None:
        session = await self.login()
        for extra in ({"instructions": "run prompt"}, {"expected_revision": 99}, {"id": "other"}, {"chat_ids": ["oc_one"]}):
            page = await self.page(session)
            action = page["items"][0]["actions"]["save"]
            before = len(self.defaults.calls)
            status, _, _ = await self.json_post("/api/v1/defaults/save", session,
                {**fixture._action_payload(action), "definition": {"project": "test", **extra}})
            self.assertEqual(status, 400)
            self.assertEqual(len(self.defaults.calls), before)
        for query in ("mode=reorder", "offset=-1", "limit=1.5", "kind=chat&instructions=run"):
            status, _, _ = await self.json_get(f"/api/v1/defaults?{query}", session)
            self.assertEqual(status, 400)

    async def test_http_edits_share_real_resolution_order_and_exact_deletion_fallback(self) -> None:
        store = BindingStore(self.root / "defaults.sqlite3")
        self.addAsyncCleanup(store.aclose)
        projects = ProjectRegistry(store=store, project_root=self.root, projects={"test": self.root})
        chat_info = FakeChatLabelProvider()
        chat_info.info = {"oc_one": FakeChatInfo("Payments Oncall", "group"),
                          "oc_direct": FakeChatInfo("", "p2p")}
        self.management.defaults = SessionDefaultsService(
            bindings=store, projects=projects, runtime=SimpleNamespace(), app_id="cli_test", chat_info=chat_info,
            chat_target_validator=FakeChatTargetDirectory(chat_info).validate_target,
        )
        session = await self.login()
        settings = self.defaults.records[0]["session_settings"]
        created = []
        for keyword in ("oncall", "payments"):
            page = await self.page(session, "group_name")
            status, _, result = await self.json_post("/api/v1/defaults/save", session, {
                **fixture._action_payload(page["actions"]["create"]),
                "definition": {"kind": "group_name", "keyword": keyword, "project": "test", "session_settings": settings},
            })
            self.assertEqual(status, 200, result)
            created.append(result["rule"]["id"])
        status, _, view = await self.json_get("/api/v1/defaults?mode=view&chat_id=oc_one", session)
        self.assertEqual(status, 200, view)
        self.assertEqual(view["effective"]["id"], created[0])
        page = await self.page(session, "group_name")
        status, _, result = await self.json_post("/api/v1/defaults/reorder", session, {
            **fixture._action_payload(page["actions"]["reorder"]), "rule_ids": list(reversed(created)),
        })
        self.assertEqual(status, 200, result)
        status, _, view = await self.json_get("/api/v1/defaults?mode=view&chat_id=oc_one", session)
        self.assertEqual(view["effective"]["id"], created[1])
        status, _, result = await self.json_post("/api/v1/defaults/save", session, {
            **fixture._action_payload(view["actions"]["create"]),
            "definition": {"kind": "chat", "chat_id": "oc_one", "project": "test", "session_settings": settings},
        })
        self.assertEqual(status, 200, result)
        exact_id = result["rule"]["id"]
        status, _, view = await self.json_get("/api/v1/defaults?mode=view&chat_id=oc_one", session)
        self.assertEqual(view["exact"]["id"], exact_id)
        self.assertEqual(view["effective"]["id"], exact_id)
        status, _, result = await self.json_post("/api/v1/defaults/delete", session,
                                                fixture._action_payload(view["exact"]["actions"]["delete"]))
        self.assertEqual(status, 200, result)
        status, _, view = await self.json_get("/api/v1/defaults?mode=view&chat_id=oc_one", session)
        self.assertIsNone(view["exact"])
        self.assertEqual(view["effective"]["id"], created[1])
        status, _, view = await self.json_get("/api/v1/defaults?mode=view&chat_id=oc_direct", session)
        self.assertEqual(status, 200, view)
        status, _, result = await self.json_post("/api/v1/defaults/save", session, {
            **fixture._action_payload(view["actions"]["create"]),
            "definition": {"kind": "chat", "chat_id": "oc_direct", "project": "test",
                           "session_settings": {**settings, "message_context_mode": "catch-up"}},
        })
        self.assertEqual(status, 400, result)
        self.assertIsNone(store.defaults.exact("cli_test", "oc_direct"))
