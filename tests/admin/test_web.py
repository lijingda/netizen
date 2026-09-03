from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import unittest
from html.parser import HTMLParser
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

from netizen.admin.web import (
    AdminWebError,
    AdminWebRunner,
    _batches,
    _chat_open_url,
    _created_range_query,
    _session_inventory_state,
    _session_page_size,
    _release_disposition_message,
    _stop_disposition_message,
    accepted_authorities,
)
from netizen.bindings import (
    BindingInventoryRecord,
    BindingTurnSettings,
    ProjectAggregate,
    ProjectAggregatePage,
    ProjectRecord,
    ScopeRecord,
    SideTopicInventoryRecord,
    SideTopicRecord,
    SideTopicState,
    ThreadBinding,
)
from netizen.codex_runtime import (
    ActiveState,
    ActiveTurnSnapshot,
    BindingRuntimeSnapshot,
    NativeThreadCatalogState,
    NativeThreadMetadata,
    ReleaseDisposition,
    SideSessionSnapshot,
    SideSessionState,
    StopDisposition,
    ThreadSubscriptionSnapshot,
    ThreadSubscriptionState,
)
from netizen.domain import GoalStatus, ScopeKind
from netizen.management import (
    BindingStatusProjection,
    ChatLabel,
    ClosedSide,
    CreatedBinding,
    InstanceManagementService,
    ManagementRuntimePort,
    NativeThreadView,
    ProjectInventoryItem,
    ProjectInventoryPage,
    ReleasedBinding,
    RenamedBinding,
    RuntimeSnapshots,
    SessionInventoryItem,
    SessionInventoryPage,
    SessionInventoryState,
    SideTopicInventoryItem,
    SideTopicInventoryPage,
    StoppedBinding,
)
from netizen.management.service import _project_binding_status
from netizen.projects import Project
from netizen.sdk_gap_adapter import GoalSnapshot


class FakeManagement:
    def __init__(self, root: Path) -> None:
        self.calls: list[tuple[str, object]] = []
        self.query_session_calls: list[dict[str, object]] = []
        self.query_side_topic_calls: list[dict[str, object]] = []
        self.snapshot_batches: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
        self.session_items_override: tuple[SessionInventoryItem, ...] | None = None
        self.persisted_goals: dict[str, GoalSnapshot] = {}
        self.set_enabled_entered: asyncio.Event | None = None
        self.set_enabled_release: asyncio.Event | None = None
        self.project = Project("test", root, True, 1)
        self.project_record = ProjectRecord(
            "test", str(root), True, 1, "2030-01-01", "2030-01-01"
        )
        self.scope = ScopeRecord(
            scope_key="cli_test:direct:oc_chat",
            app_id="cli_test",
            chat_id="oc_chat",
            kind=ScopeKind.DIRECT,
            topic_id=None,
            active_binding_id="binding-native",
            updated_at="2030-01-01",
        )
        self.lazy = ThreadBinding(
            "binding-lazy",
            self.scope.scope_key,
            "test",
            None,
            None,
            1,
            "admin:web",
            False,
            "2030-01-03",
            None,
        )
        self.native = ThreadBinding(
            "binding-native",
            self.scope.scope_key,
            "test",
            "native-active",
            BindingTurnSettings("model", "medium", "priority"),
            2,
            "ou_user",
            True,
            "2030-01-02",
            "2030-01-02",
        )
        self.archived = ThreadBinding(
            "binding-archived",
            self.scope.scope_key,
            "test",
            "native-archived",
            None,
            1,
            "ou_user",
            False,
            "2030-01-01",
            "2030-01-01",
        )
        self.runtime_by_id = {
            self.lazy.id: self._runtime(self.lazy.id, 1),
            self.native.id: self._runtime(self.native.id, 4, running=True),
            self.archived.id: self._runtime(self.archived.id, 2),
        }
        self.side_record = SideTopicRecord(
            id="side-1",
            app_id="cli_test",
            chat_id="oc_chat",
            source_message_id="om_source",
            root_message_id="om_root",
            topic_id="omt_topic",
            parent_binding_id=self.native.id,
            creator_id="ou_user",
            requires_mention=False,
            state=SideTopicState.OPEN,
            created_at="2030-01-01",
            updated_at="2030-01-01",
        )
        self.side_runtime = SideSessionSnapshot(
            side_id="side-1",
            parent_binding_id=self.native.id,
            parent_thread_id="native-active",
            thread_id="native-side",
            project_alias="test",
            cwd=root,
            creator_id="ou_user",
            state=SideSessionState.OPEN,
            topic_id="omt_topic",
            root_message_id="om_root",
            turn_id=None,
            turn_state=None,
            last_activity=1.0,
        )

    def _runtime(
        self,
        binding_id: str,
        revision: int,
        *,
        running: bool = False,
    ) -> BindingRuntimeSnapshot:
        turn = (
            ActiveTurnSnapshot(
                binding_id,
                "native-active",
                "turn-active",
                "ou_user",
                ActiveState.RUNNING,
            )
            if running
            else None
        )
        subscription = (
            ThreadSubscriptionSnapshot(
                binding_id,
                "native-active",
                ThreadSubscriptionState.SUBSCRIBED,
                None,
            )
            if binding_id == "binding-native"
            else None
        )
        return BindingRuntimeSnapshot(
            binding_id,
            revision,
            turn,
            None,
            False,
            None,
            subscription,
            None,
        )

    async def close(self, **_kwargs) -> None:
        pass

    async def query_projects(self, **_kwargs):
        aggregate = ProjectAggregate(self.project_record, 3, 1, 2, "2030-01-02")
        return ProjectInventoryPage((ProjectInventoryItem(aggregate, 1),), None)

    async def query_sessions(self, **_kwargs):
        self.query_session_calls.append(dict(_kwargs))
        if self.session_items_override is not None:
            return SessionInventoryPage(self.session_items_override, None)
        label = ChatLabel(
            "oc_chat",
            "Demo Chat",
            "p2p",
            True,
            "ou_partner",
            "private",
        )
        records = (
            SessionInventoryItem(
                BindingInventoryRecord(self.lazy, self.scope), None, label
            ),
            SessionInventoryItem(
                BindingInventoryRecord(self.native, self.scope),
                NativeThreadView(
                    NativeThreadCatalogState.ACTIVE,
                    NativeThreadMetadata("native-active", "Active", "preview"),
                ),
                label,
            ),
            SessionInventoryItem(
                BindingInventoryRecord(self.archived, self.scope),
                NativeThreadView(
                    NativeThreadCatalogState.ARCHIVED,
                    NativeThreadMetadata("native-archived", "Archived", "old"),
                ),
                label,
            ),
        )
        return SessionInventoryPage(records, None)

    async def query_side_topics(self, **_kwargs):
        self.query_side_topic_calls.append(dict(_kwargs))
        item = SideTopicInventoryItem(
            SideTopicInventoryRecord(self.side_record, "test"),
            ChatLabel(
                "oc_chat",
                "Demo Chat",
                "p2p",
                True,
                "ou_partner",
                "private",
            ),
            self.side_runtime,
        )
        return SideTopicInventoryPage((item,), None)

    def runtime_snapshots(self, *, binding_ids=(), side_ids=()):
        self.snapshot_batches.append((tuple(binding_ids), tuple(side_ids)))
        return RuntimeSnapshots(
            tuple(
                _project_binding_status(
                    binding=self._binding(item),
                    snapshot=self.runtime_by_id[item],
                )
                for item in binding_ids
            ),
            tuple(self.side_runtime for item in side_ids if item == "side-1"),
            tuple(item for item in side_ids if item != "side-1"),
        )

    async def binding_statuses_exact(
        self,
        *,
        binding_ids=(),
        catalog_states=None,
        deadline=None,
    ) -> tuple[BindingStatusProjection, ...]:
        del deadline
        self.snapshot_batches.append((tuple(binding_ids), ()))
        states = catalog_states or {}
        return tuple(
            _project_binding_status(
                binding=self._binding(item),
                snapshot=self.runtime_by_id[item],
                persisted_goal=self.persisted_goals.get(item),
                persisted_goal_resolved=True,
                catalog_state=states.get(item),
            )
            for item in binding_ids
        )

    def _binding(self, binding_id: str) -> ThreadBinding:
        for binding in (self.lazy, self.native, self.archived):
            if binding.id == binding_id:
                return binding
        for item in self.session_items_override or ():
            if item.record.binding.id == binding_id:
                return item.record.binding
        raise AssertionError(f"unknown Binding {binding_id}")

    async def register_project(self, **values):
        self.calls.append(("register", values))
        return self.project

    async def set_project_enabled(self, **values):
        self.calls.append(("set-enabled", values))
        if self.set_enabled_entered is not None:
            self.set_enabled_entered.set()
        if self.set_enabled_release is not None:
            await self.set_enabled_release.wait()
        return Project("test", self.project.cwd, values["enabled"], 2)

    async def create_exact_lazy_binding(self, **values):
        self.calls.append(("create-lazy", values))
        binding = self.lazy
        return CreatedBinding(self.project, binding)

    async def resolve_turn_settings(self, **values):
        self.calls.append(("resolve-settings", values))
        return BindingTurnSettings(
            values["model_id"], values["effort_id"], values["service_tier_id"]
        )

    async def activate_exact_binding(self, **values):
        self.calls.append(("activate", values))
        return self.lazy

    async def configure_exact_binding(self, **values):
        self.calls.append(("configure", values))
        return self.lazy

    async def rename_exact_binding(self, **values):
        self.calls.append(("rename", values))
        return RenamedBinding(self.native, values["name"])

    async def archive_exact_binding(self, **values):
        self.calls.append(("archive", values))
        return self.native

    async def restore_exact_binding(self, **values):
        self.calls.append(("unarchive", values))
        return self.archived

    async def restore_exact_binding_as_current(self, **values):
        self.calls.append(("unarchive-current", values))
        return self.archived

    async def delete_exact_lazy_binding(self, **values):
        self.calls.append(("delete-lazy", values))
        return self.lazy

    async def stop_exact_binding(self, **values):
        self.calls.append(("stop", values))
        return StoppedBinding(self.native, StopDisposition.REQUESTED)

    async def release_exact_binding(self, **values):
        self.calls.append(("release", values))
        return ReleasedBinding(self.native, ReleaseDisposition.RELEASED)

    async def close_side(self, **values):
        self.calls.append(("close-side", values))
        closed = replace(self.side_record, state=SideTopicState.CLOSED)
        return ClosedSide(closed, None)


class AdminSessionPresentationTest(unittest.TestCase):
    def test_stop_and_release_render_every_shared_disposition(self) -> None:
        self.assertEqual(
            len(set(map(_stop_disposition_message, StopDisposition))),
            len(StopDisposition),
        )
        self.assertTrue(
            all(_stop_disposition_message(item) for item in StopDisposition)
        )
        self.assertTrue(
            all(_release_disposition_message(item) for item in ReleaseDisposition)
        )

    def test_session_inventory_state_defaults_to_active_and_accepts_all(self) -> None:
        self.assertIs(
            _session_inventory_state({}),
            SessionInventoryState.ACTIVE,
        )
        for state in SessionInventoryState:
            self.assertIs(
                _session_inventory_state({"inventoryState": [state.value]}),
                state,
            )
        self.assertIsNone(_session_inventory_state({"inventoryState": ["all"]}))
        with self.assertRaises(AdminWebError):
            _session_inventory_state({"inventoryState": ["materialized"]})

    def test_sessions_page_sizes_are_exact_and_default_to_twenty(self) -> None:
        self.assertEqual(_session_page_size({}), 20)
        for value in (10, 20, 50, 100):
            self.assertEqual(_session_page_size({"pageSize": [str(value)]}), value)
        for value in (1, 25, 101):
            with self.subTest(value=value), self.assertRaises(AdminWebError):
                _session_page_size({"pageSize": [str(value)]})

    def test_runtime_batches_preserve_order_and_never_exceed_fifty(self) -> None:
        ids = tuple(f"binding-{index}" for index in range(100))
        batches = _batches(ids, 50)
        self.assertEqual(tuple(map(len, batches)), (50, 50))
        self.assertEqual(tuple(item for batch in batches for item in batch), ids)

    def test_chat_link_is_derived_from_shared_facts(self) -> None:
        self.assertEqual(
            _chat_open_url(
                ChatLabel("oc_direct", "Alice", "p2p", True, "ou_alice")
            ),
            "https://applink.feishu.cn/client/chat/open?openId=ou_alice",
        )
        self.assertEqual(
            _chat_open_url(ChatLabel("oc_group", "Engineering", "group")),
            "https://applink.feishu.cn/client/chat/open?openChatId=oc_group",
        )

    def test_created_range_is_strict_and_normalized_to_canonical_utc(self) -> None:
        self.assertEqual(
            _created_range_query(
                {
                    "createdFrom": ["2030-01-02T08:30+08:00"],
                    "createdBefore": ["2030-01-03T00:00:00Z"],
                }
            ),
            (
                "2030-01-02T00:30:00.000000+00:00",
                "2030-01-03T00:00:00.000000+00:00",
            ),
        )
        self.assertEqual(
            _created_range_query(
                {"createdFrom": ["2030-01-02T00:00:00.1234+00:00"]}
            ),
            ("2030-01-02T00:00:00.123400+00:00", None),
        )
        for values, code in (
            ({"createdFrom": ["2030-01-02T00:00"]}, "invalid_time"),
            ({"createdBefore": ["2030-02-30T00:00Z"]}, "invalid_time"),
            (
                {
                    "createdFrom": ["2030-01-03T00:00Z"],
                    "createdBefore": ["2030-01-03T00:00+00:00"],
                },
                "invalid_time_range",
            ),
            (
                {
                    "createdFrom": ["2030-01-04T00:00Z"],
                    "createdBefore": ["2030-01-03T00:00Z"],
                },
                "invalid_time_range",
            ),
        ):
            with self.subTest(values=values), self.assertRaises(AdminWebError) as caught:
                _created_range_query(values)
            self.assertEqual(caught.exception.code, code)


class AdminWebTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.secret_path = self.root / "admin-secret"
        self.credential = secrets.token_urlsafe(32)
        self.secret_path.write_text(self.credential, encoding="ascii")
        self.secret_path.chmod(0o600)
        self.management = FakeManagement(self.root)

        def loopback_authorities(_host, _port, addresses):
            address = addresses[0]
            assert isinstance(address, tuple)
            return (f"127.0.0.1:{address[1]}",)

        # These cases exercise the HTTP application, while authority discovery
        # has focused coverage below. Keep every case independent of host DNS,
        # which can take about a minute on a macOS CI runner.
        self.authority_builder_patch = patch(
            "netizen.admin.web.accepted_authorities",
            side_effect=loopback_authorities,
        )
        self.authority_builder_patch.start()
        self.addCleanup(self.authority_builder_patch.stop)
        self.runner = AdminWebRunner(
            host="127.0.0.1",
            port=0,
            credential_path=self.secret_path,
            management=self.management,  # type: ignore[arg-type]
        )
        await self.runner.bind()
        address = self.runner.addresses[0]
        assert isinstance(address, tuple)
        self.port = int(address[1])
        self.authority = f"127.0.0.1:{self.port}"
        self.origin = f"http://{self.authority}"

    async def asyncTearDown(self) -> None:
        deadline = asyncio.get_running_loop().time() + 1
        try:
            await self.runner.drain(deadline)
        finally:
            self.runner.close_auth()
            self.temp.cleanup()

    async def request(
        self,
        method: str,
        target: str,
        *,
        headers: list[tuple[str, str]] | None = None,
        body: bytes = b"",
    ) -> tuple[int, list[tuple[str, str]], bytes]:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        request_headers = [("Host", self.authority), *(headers or [])]
        if body:
            request_headers.append(("Content-Length", str(len(body))))
        raw = (
            f"{method} {target} HTTP/1.1\r\n"
            + "".join(f"{name}: {value}\r\n" for name, value in request_headers)
            + "\r\n"
        ).encode("ascii") + body
        writer.write(raw)
        await writer.drain()
        try:
            status_line = await reader.readline()
            status = int(status_line.split()[1])
            response_headers: list[tuple[str, str]] = []
            while True:
                line = await reader.readline()
                if line == b"\r\n":
                    break
                name, value = line.decode("latin1").split(":", 1)
                response_headers.append((name.lower(), value.strip()))
            length = int(dict(response_headers).get("content-length", "0"))
            content = await reader.readexactly(length)
            return status, response_headers, content
        finally:
            writer.close()
            await writer.wait_closed()

    async def login(self) -> str:
        status, headers, body = await self.request("GET", "/login")
        self.assertEqual(status, 200)
        preauth = _cookie_value(headers, "netizen_admin_preauth")
        nonce_match = re.search(rb"name='nonce' value='([^']+)'", body)
        assert nonce_match is not None
        form = urlencode(
            {
                "nonce": nonce_match.group(1).decode("ascii"),
                "credential": self.credential,
            }
        ).encode("ascii")
        status, headers, _body = await self.request(
            "POST",
            "/login",
            headers=[
                ("Origin", self.origin),
                ("Content-Type", "application/x-www-form-urlencoded"),
                ("Cookie", f"netizen_admin_preauth={preauth}"),
            ],
            body=form,
        )
        self.assertEqual(status, 303)
        return _cookie_value(headers, "netizen_admin_session")

    async def json_get(self, target: str, session: str):
        status, headers, body = await self.request(
            "GET",
            target,
            headers=[("Cookie", f"netizen_admin_session={session}")],
        )
        return status, headers, json.loads(body) if body else None

    async def json_post(self, target: str, session: str, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        status, headers, content = await self.request(
            "POST",
            target,
            headers=[
                ("Cookie", f"netizen_admin_session={session}"),
                ("Origin", self.origin),
                ("Content-Type", "application/json"),
            ],
            body=body,
        )
        return status, headers, json.loads(content) if content else None

    async def test_bound_closed_readiness_then_login_security_headers(self) -> None:
        status, _headers, _body = await self.request("GET", "/health/ready")
        self.assertEqual(status, 503)
        self.runner.open_admission()
        status, _headers, _body = await self.request("GET", "/health/ready")
        self.assertEqual(status, 204)

        status, headers, stylesheet = await self.request("GET", "/static/admin.css")
        self.assertEqual(status, 200)
        self.assertEqual(dict(headers)["content-type"], "text/css; charset=utf-8")
        self.assertIn(b".login-card", stylesheet)

        status, login_headers, login_page = await self.request("GET", "/login")
        self.assertEqual(status, 200)
        self.assertEqual(dict(login_headers)["referrer-policy"], "same-origin")
        self.assertIn(b"href='/static/admin.css'", login_page)
        self.assertIn(b"class='login-body'", login_page)
        self.assertIn(b"class='login-card'", login_page)

        session = await self.login()
        status, headers, body = await self.request(
            "GET",
            "/",
            headers=[("Cookie", f"netizen_admin_session={session}")],
        )
        self.assertEqual(status, 200)
        self.assertIn(b"Netizen Admin", body)
        mapped = dict(headers)
        self.assertEqual(mapped["cache-control"], "no-store")
        self.assertIn("frame-ancestors 'none'", mapped["content-security-policy"])

    async def test_unknown_route_does_not_leak_before_auth_and_origin_is_exact(self) -> None:
        self.runner.open_admission()
        status, _headers, payload = await self.request("GET", "/does-not-exist")
        self.assertEqual(status, 401)
        self.assertNotIn(b"does-not-exist", payload)
        session = await self.login()
        status, _headers, payload = await self.request(
            "POST",
            "/logout",
            headers=[
                ("Cookie", f"netizen_admin_session={session}"),
                ("Origin", "http://evil.invalid"),
            ],
        )
        self.assertEqual(status, 403)
        self.assertNotIn(self.credential.encode(), payload)

    async def test_unauthenticated_root_redirects_to_login(self) -> None:
        self.runner.open_admission()
        status, headers, body = await self.request("GET", "/")
        self.assertEqual(status, 303)
        self.assertEqual(dict(headers)["location"], "/login")
        # The redirect must not serve the admin HTML or any state.
        self.assertEqual(body, b"")

    async def test_invalid_session_cookie_still_redirects_root_to_login(self) -> None:
        self.runner.open_admission()
        status, headers, _body = await self.request(
            "GET",
            "/",
            headers=[("Cookie", "netizen_admin_session=not-a-real-session")],
        )
        self.assertEqual(status, 303)
        self.assertEqual(dict(headers)["location"], "/login")

    async def test_rotated_session_cookie_redirects_root_to_login(self) -> None:
        self.runner.open_admission()
        session = await self.login()
        # Rotate the credential so the existing session is no longer valid.
        self.secret_path.write_text(secrets.token_urlsafe(32), encoding="ascii")
        self.secret_path.chmod(0o600)
        status, headers, _body = await self.request(
            "GET",
            "/",
            headers=[("Cookie", f"netizen_admin_session={session}")],
        )
        self.assertEqual(status, 303)
        self.assertEqual(dict(headers)["location"], "/login")

    async def test_unauthenticated_non_root_routes_still_return_json_401(self) -> None:
        self.runner.open_admission()
        for target in ("/api/v1/projects", "/static/admin.js", "/does-not-exist"):
            with self.subTest(target=target):
                status, headers, body = await self.request("GET", target)
                self.assertEqual(status, 401)
                self.assertEqual(
                    dict(headers)["content-type"],
                    "application/json; charset=utf-8",
                )
                payload = json.loads(body)
                self.assertEqual(payload["code"], "not_authenticated")

    async def test_unknown_internal_error_logs_only_the_error_type(self) -> None:
        self.runner.open_admission()
        session = await self.login()
        sensitive = "sensitive-cwd-title-preview-request-body"

        async def fail_query(**_kwargs):
            raise RuntimeError(sensitive)

        self.management.query_projects = fail_query
        with self.assertLogs("netizen.admin.web", level="ERROR") as captured:
            status, _headers, payload = await self.request(
                "GET",
                "/api/v1/projects",
                headers=[("Cookie", f"netizen_admin_session={session}")],
            )
        self.assertEqual(status, 500)
        self.assertNotIn(sensitive.encode(), payload)
        rendered = "\n".join(captured.output)
        self.assertNotIn(sensitive, rendered)
        self.assertIn("error_type=RuntimeError", rendered)
        self.assertNotIn("Traceback", rendered)

    async def test_rotation_invalidates_existing_session(self) -> None:
        self.runner.open_admission()
        session = await self.login()
        self.secret_path.write_text(secrets.token_urlsafe(32), encoding="ascii")
        self.secret_path.chmod(0o600)

        status, _headers, _payload = await self.json_get("/api/v1/projects", session)

        self.assertEqual(status, 401)

    async def test_projects_action_is_one_shot(self) -> None:
        self.runner.open_admission()
        session = await self.login()
        status, _headers, page = await self.json_get("/api/v1/projects", session)
        self.assertEqual(status, 200)
        envelope = page["items"][0]["actions"]["setEnabled"]
        payload = _action_payload(envelope, enabled=False)

        status, _headers, _result = await self.json_post(
            "/api/v1/projects/set-enabled", session, payload
        )
        repeated, _headers, error = await self.json_post(
            "/api/v1/projects/set-enabled", session, payload
        )

        self.assertEqual(status, 200)
        self.assertEqual(repeated, 409)
        self.assertEqual(error["code"], "stale_or_consumed")
        call = next(value for name, value in self.management.calls if name == "set-enabled")
        self.assertEqual(call["expected_revision"], 1)

    async def test_disconnect_does_not_cancel_redeemed_mutation(self) -> None:
        self.runner.open_admission()
        session = await self.login()
        _status, _headers, page = await self.json_get("/api/v1/projects", session)
        envelope = page["items"][0]["actions"]["setEnabled"]
        body = json.dumps(_action_payload(envelope, enabled=False)).encode("utf-8")
        self.management.set_enabled_entered = asyncio.Event()
        self.management.set_enabled_release = asyncio.Event()
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        del reader
        request = (
            f"POST /api/v1/projects/set-enabled HTTP/1.1\r\n"
            f"Host: {self.authority}\r\n"
            f"Origin: {self.origin}\r\n"
            f"Cookie: netizen_admin_session={session}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n\r\n"
        ).encode("ascii") + body
        writer.write(request)
        await writer.drain()
        await asyncio.wait_for(self.management.set_enabled_entered.wait(), 0.5)
        writer.close()
        await writer.wait_closed()
        self.management.set_enabled_release.set()

        deadline = asyncio.get_running_loop().time() + 0.5
        while self.runner.application.mutation_task_count:
            if asyncio.get_running_loop().time() >= deadline:
                self.fail("redeemed mutation did not finish after disconnect")
            await asyncio.sleep(0.002)

        self.assertTrue(
            any(name == "set-enabled" for name, _values in self.management.calls)
        )

    async def test_all_session_and_side_mutation_routes_are_reachable(self) -> None:
        self.runner.open_admission()
        session = await self.login()

        async def fresh_items():
            status, _headers, page = await self.json_get(
                "/api/v1/sessions?inventoryState=all",
                session,
            )
            self.assertEqual(status, 200)
            return {item["bindingId"]: item for item in page["items"]}

        cases = [
            ("binding-lazy", "activate", "/api/v1/sessions/activate", {}),
            ("binding-lazy", "configure", "/api/v1/sessions/configure", {"turnSettings": None}),
            ("binding-lazy", "deleteLazy", "/api/v1/sessions/delete-lazy", {}),
            ("binding-native", "rename", "/api/v1/sessions/rename", {"name": "Renamed"}),
            ("binding-native", "archive", "/api/v1/sessions/archive", {}),
            ("binding-native", "stop", "/api/v1/sessions/stop", {}),
            ("binding-native", "release", "/api/v1/sessions/release", {}),
            ("binding-archived", "unarchive", "/api/v1/sessions/unarchive", {"actionKind": "sessions.unarchive"}),
            ("binding-archived", "unarchiveCurrent", "/api/v1/sessions/unarchive", {"actionKind": "sessions.unarchive-current"}),
        ]
        for binding_id, key, route, extra in cases:
            if key == "release":
                self.management.runtime_by_id[binding_id] = self.management._runtime(
                    binding_id,
                    5,
                )
            item = (await fresh_items())[binding_id]
            status, _headers, payload = await self.json_post(
                route,
                session,
                _action_payload(item["actions"][key], **extra),
            )
            self.assertEqual(status, 200, payload)
            if key in {"stop", "release"}:
                self.assertIn("message", payload)

        archive_call = next(
            values for name, values in self.management.calls if name == "archive"
        )
        self.assertIsNone(
            archive_call["target"].expected_active_binding_id
        )

        items = await fresh_items()
        create = items["binding-native"]["actions"]["createLazy"]
        status, _headers, payload = await self.json_post(
            "/api/v1/sessions/create-lazy",
            session,
            _action_payload(
                create,
                projectAlias="test",
                projectRevision=1,
                activate=False,
                turnSettings=None,
            ),
        )
        self.assertEqual(status, 200, payload)

        status, _headers, side_page = await self.json_get(
            "/api/v1/side-topics", session
        )
        self.assertEqual(status, 200)
        close = side_page["items"][0]["actions"]["close"]
        status, _headers, payload = await self.json_post(
            "/api/v1/side-topics/close",
            session,
            _action_payload(close),
        )
        self.assertEqual(status, 200, payload)

        self.management.side_record = replace(
            self.management.side_record,
            state=SideTopicState.CREATING,
        )
        status, _headers, side_page = await self.json_get(
            "/api/v1/side-topics", session
        )
        self.assertEqual(status, 200)
        self.assertNotIn("close", side_page["items"][0]["actions"])

    async def test_cursor_is_bound_to_filter_and_snapshot_get_issues_no_actions(self) -> None:
        self.runner.open_admission()
        session = await self.login()
        before = self.runner.application._auth.state_counts().actions
        status, _headers, payload = await self.json_get(
            "/api/v1/runtime-snapshots?bindingIds=binding-native&sideIds=side-1",
            session,
        )
        after = self.runner.application._auth.state_counts().actions
        self.assertEqual(status, 200)
        self.assertEqual(payload["bindings"][0]["bindingId"], "binding-native")
        self.assertEqual(payload["bindings"][0]["primaryStatus"], "running")
        self.assertEqual(
            payload["bindings"][0]["primaryStatusResolution"],
            "local",
        )
        self.assertEqual(before, after)

        self.management.runtime_by_id["binding-native"] = self.management._runtime(
            "binding-native",
            5,
        )
        status, _headers, payload = await self.json_get(
            "/api/v1/runtime-snapshots?bindingIds=binding-native",
            session,
        )
        self.assertEqual(status, 200)
        self.assertIsNone(payload["bindings"][0]["primaryStatus"])
        self.assertEqual(
            payload["bindings"][0]["primaryStatusResolution"],
            "deferred",
        )

        status, _headers, payload = await self.json_get(
            "/api/v1/runtime-snapshots?bindingIds=binding-native&resolvePrimary=true",
            session,
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["bindings"][0]["primaryStatus"], "idle")
        self.assertEqual(
            payload["bindings"][0]["primaryStatusResolution"],
            "resolved",
        )

        from netizen.admin.web import _encode_binding_cursor, _fingerprint
        from netizen.bindings import BindingCursor

        cursor = _encode_binding_cursor(
            BindingCursor("2030", "binding"),
            _fingerprint(
                "sessions",
                {
                    "pageSize": 20,
                    "local": {
                        "project_alias": "first",
                        "scope_kind": None,
                        "chat_id": None,
                        "topic_id": None,
                        "identity": None,
                        "materialized": None,
                        "current": None,
                        "created_from": None,
                        "created_before": None,
                    },
                    "inventoryState": "active",
                },
            ),
        )
        status, _headers, error = await self.json_get(
            f"/api/v1/sessions?project=second&cursor={cursor}",
            session,
        )
        self.assertEqual(status, 400)
        self.assertEqual(error["code"], "invalid_cursor")
        status, _headers, error = await self.json_get(
            f"/api/v1/sessions?project=first&pageSize=50&cursor={cursor}",
            session,
        )
        self.assertEqual(status, 400)
        self.assertEqual(error["code"], "invalid_cursor")

    async def test_sessions_api_exposes_page_size_presentation_and_state(self) -> None:
        self.runner.open_admission()
        session = await self.login()
        started = asyncio.get_running_loop().time()

        status, _headers, page = await self.json_get("/api/v1/sessions", session)
        finished = asyncio.get_running_loop().time()

        self.assertEqual(status, 200)
        self.assertEqual(page["pageSize"], 20)
        call = self.management.query_session_calls[-1]
        self.assertEqual(call["limit"], 20)
        self.assertIs(
            call["query"].inventory_state,
            SessionInventoryState.ACTIVE,
        )
        self.assertGreaterEqual(call["deadline"], started + 10.0)
        self.assertLessEqual(call["deadline"], finished + 10.0)
        by_id = {item["bindingId"]: item for item in page["items"]}
        self.assertEqual(by_id["binding-native"]["sessionType"], "message")
        self.assertEqual(by_id["binding-native"]["chatMode"], "p2p")
        self.assertTrue(by_id["binding-native"]["chatLabelResolved"])
        self.assertEqual(
            by_id["binding-native"]["chatOpenUrl"],
            "https://applink.feishu.cn/client/chat/open?openId=ou_partner",
        )
        self.assertEqual(by_id["binding-native"]["pointerState"], "current")
        self.assertEqual(by_id["binding-native"]["catalogState"], "active")
        self.assertEqual(by_id["binding-lazy"]["pointerState"], "inactive")
        self.assertEqual(by_id["binding-lazy"]["catalogState"], "lazy")
        self.assertEqual(by_id["binding-archived"]["pointerState"], "inactive")
        self.assertEqual(by_id["binding-archived"]["catalogState"], "archived")
        for item in by_id.values():
            self.assertNotIn("sessionState", item)
            self.assertNotIn("nativeState", item)
            self.assertNotIn("current", item)
        self.assertEqual(by_id["binding-native"]["runtime"]["primaryStatus"], "running")
        self.assertEqual(
            by_id["binding-native"]["runtime"]["subscriptionState"],
            "subscribed",
        )

        status, _headers, page = await self.json_get(
            "/api/v1/sessions?pageSize=100",
            session,
        )
        self.assertEqual(status, 200)
        self.assertEqual(page["pageSize"], 100)
        self.assertEqual(self.management.query_session_calls[-1]["limit"], 100)

        status, _headers, error = await self.json_get(
            "/api/v1/sessions?pageSize=25",
            session,
        )
        self.assertEqual(status, 400)
        self.assertEqual(error["code"], "invalid_page_size")

        status, _headers, _page = await self.json_get(
            "/api/v1/sessions?inventoryState=lazy",
            session,
        )
        self.assertEqual(status, 200)
        self.assertIs(
            self.management.query_session_calls[-1]["query"].inventory_state,
            SessionInventoryState.LAZY,
        )
        status, _headers, _page = await self.json_get(
            "/api/v1/sessions?inventoryState=all",
            session,
        )
        self.assertEqual(status, 200)
        self.assertIsNone(
            self.management.query_session_calls[-1]["query"].inventory_state
        )
        status, _headers, error = await self.json_get(
            "/api/v1/sessions?inventoryState=materialized",
            session,
        )
        self.assertEqual(status, 400)
        self.assertEqual(error["code"], "invalid_inventory_state")
        status, _headers, error = await self.json_get(
            "/api/v1/sessions?materialized=false",
            session,
        )
        self.assertEqual(status, 400)
        self.assertEqual(error["code"], "invalid_query")

    async def test_time_ranges_are_normalized_for_session_and_side_queries(self) -> None:
        self.runner.open_admission()
        session = await self.login()
        encoded = urlencode(
            {
                "createdFrom": "2030-01-02T08:30+08:00",
                "createdBefore": "2030-01-03T08:45+08:00",
            }
        )

        status, _headers, payload = await self.json_get(
            f"/api/v1/sessions?{encoded}", session
        )
        self.assertEqual(status, 200, payload)
        local = self.management.query_session_calls[-1]["query"].local
        self.assertEqual(local.created_from, "2030-01-02T00:30:00.000000+00:00")
        self.assertEqual(local.created_before, "2030-01-03T00:45:00.000000+00:00")

        status, _headers, payload = await self.json_get(
            f"/api/v1/side-topics?{encoded}", session
        )
        self.assertEqual(status, 200, payload)
        side_query = self.management.query_side_topic_calls[-1]["query"]
        self.assertEqual(
            side_query.created_from,
            "2030-01-02T00:30:00.000000+00:00",
        )
        self.assertEqual(
            side_query.created_before,
            "2030-01-03T00:45:00.000000+00:00",
        )

        status, _headers, error = await self.json_get(
            "/api/v1/sessions?createdFrom=2030-01-03T00%3A00Z"
            "&createdBefore=2030-01-02T00%3A00Z",
            session,
        )
        self.assertEqual(status, 400)
        self.assertEqual(error["code"], "invalid_time_range")

    async def test_subscription_state_does_not_replace_idle_primary_status(
        self,
    ) -> None:
        self.runner.open_admission()
        session = await self.login()
        self.management.runtime_by_id[self.management.native.id] = (
            BindingRuntimeSnapshot(
                self.management.native.id,
                9,
                None,
                None,
                False,
                None,
                ThreadSubscriptionSnapshot(
                    self.management.native.id,
                    "native-active",
                    ThreadSubscriptionState.RELEASE_PENDING,
                    30.0,
                ),
                None,
            )
        )

        status, _headers, page = await self.json_get(
            "/api/v1/sessions?inventoryState=all",
            session,
        )

        self.assertEqual(status, 200)
        native = next(
            item for item in page["items"] if item["bindingId"] == "binding-native"
        )
        self.assertEqual(native["runtime"]["primaryStatus"], "idle")
        self.assertEqual(
            native["runtime"]["subscriptionState"],
            "release-pending",
        )
        self.assertNotIn("stop", native["actions"])
        self.assertIn("release", native["actions"])

    async def test_persisted_paused_goal_is_not_reported_as_idle(self) -> None:
        self.runner.open_admission()
        session = await self.login()
        self.management.runtime_by_id[self.management.native.id] = (
            self.management._runtime(self.management.native.id, 10)
        )
        self.management.persisted_goals[self.management.native.id] = GoalSnapshot(
            "native-active",
            "paused work",
            GoalStatus.PAUSED,
            None,
            10,
            2,
            1,
            2,
        )

        status, _headers, page = await self.json_get(
            "/api/v1/sessions?inventoryState=all",
            session,
        )

        self.assertEqual(status, 200)
        native = next(
            item for item in page["items"] if item["bindingId"] == "binding-native"
        )
        self.assertEqual(native["runtime"]["primaryStatus"], "goal-paused")
        self.assertEqual(
            native["runtime"]["primaryStatusResolution"],
            "resolved",
        )
        self.assertNotIn("stop", native["actions"])

    async def test_hundred_session_page_chunks_initial_runtime_snapshots(self) -> None:
        self.runner.open_admission()
        session = await self.login()
        label = ChatLabel("oc_chat", "Demo Chat", "group")
        items = []
        for index in range(100):
            binding = replace(
                self.management.native,
                id=f"binding-{index:03d}",
                native_thread_id=f"native-{index:03d}",
                active=False,
            )
            items.append(
                SessionInventoryItem(
                    BindingInventoryRecord(binding, self.management.scope),
                    NativeThreadView(
                        NativeThreadCatalogState.ACTIVE,
                        NativeThreadMetadata(
                            binding.native_thread_id,
                            f"Session {index}",
                            f"Preview {index}",
                        ),
                    ),
                    label,
                )
            )
            self.management.runtime_by_id[binding.id] = self.management._runtime(
                binding.id,
                index,
            )
        self.management.session_items_override = tuple(items)
        self.management.snapshot_batches.clear()

        status, _headers, page = await self.json_get(
            "/api/v1/sessions?pageSize=100",
            session,
        )

        self.assertEqual(status, 200, page)
        self.assertEqual(len(page["items"]), 100)
        binding_batches = [
            binding_ids
            for binding_ids, side_ids in self.management.snapshot_batches
            if binding_ids and not side_ids
        ]
        self.assertEqual(tuple(map(len, binding_batches)), (50, 50))
        self.assertEqual(
            tuple(item for batch in binding_batches for item in batch),
            tuple(f"binding-{index:03d}" for index in range(100)),
        )

    async def test_forbidden_capabilities_have_no_http_or_management_surface(self) -> None:
        self.runner.open_admission()
        session = await self.login()
        forbidden_routes = (
            "/api/v1/prompt",
            "/api/v1/turns/start",
            "/api/v1/sessions/history",
            "/api/v1/sessions/compact",
            "/api/v1/sessions/goal",
            "/api/v1/sessions/delete-materialized",
            "/api/v1/side-topics/resume",
            "/api/v1/batch",
        )
        for route in forbidden_routes:
            with self.subTest(route=route):
                status, _headers, payload = await self.json_post(
                    route,
                    session,
                    {},
                )
                self.assertEqual(status, 404)
                self.assertEqual(payload["code"], "not_found")

        forbidden_methods = (
            "prompt",
            "turn",
            "history",
            "compact",
            "start_goal",
            "resume_goal",
            "clear_goal",
            "delete_materialized",
            "resume_side",
            "batch",
        )
        for owner in (ManagementRuntimePort, InstanceManagementService):
            for name in forbidden_methods:
                self.assertFalse(hasattr(owner, name), f"{owner.__name__}.{name}")

    def test_authority_builder_uses_canonical_bracketed_ipv6(self) -> None:
        with (
            patch("netizen.admin.web.socket.gethostname", return_value="host.test"),
            patch("netizen.admin.web.socket.getfqdn", return_value="host.example"),
            patch("netizen.admin.web.socket.getaddrinfo", return_value=[]),
        ):
            authorities = accepted_authorities("::", 8787, (("::1", 8787, 0, 0),))
        self.assertIn("[::1]:8787", authorities)
        self.assertIn("127.0.0.1:8787", authorities)
        self.assertIn("host.test:8787", authorities)
        self.assertIn("host.example:8787", authorities)


class _AdminAssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[str | None] = []
        self.stylesheets: list[str | None] = []
        self.ids: set[str] = set()
        self.inline_script_text = ""
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(values["id"] or "")
        if tag == "script":
            self.scripts.append(values.get("src"))
            self._in_script = True
        if tag == "link" and values.get("rel") == "stylesheet":
            self.stylesheets.append(values.get("href"))

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self.inline_script_text += data


class AdminStaticAssetsTest(unittest.TestCase):
    def test_static_ui_has_three_pages_external_assets_and_safe_text_sinks(self) -> None:
        root = Path(__file__).resolve().parents[2] / "netizen/admin/static"
        html = (root / "index.html").read_text(encoding="utf-8")
        javascript = (root / "admin.js").read_text(encoding="utf-8")
        parser = _AdminAssetParser()
        parser.feed(html)

        self.assertEqual(parser.scripts, ["/static/admin.js"])
        self.assertEqual(parser.stylesheets, ["/static/admin.css"])
        self.assertFalse(parser.inline_script_text.strip())
        self.assertTrue({"projects", "sessions", "side-topics"} <= parser.ids)
        self.assertTrue(
            {
                "session-page-size",
                "sessions-previous",
                "sessions-page",
                "sessions-next",
            }
            <= parser.ids
        )
        for option in ('value="10"', 'value="20" selected', 'value="50"', 'value="100"'):
            self.assertIn(option, html)
        self.assertIn('name="inventoryState"', html)
        self.assertIn('<option value="active" selected>Active</option>', html)
        self.assertIn('<option value="lazy">Lazy</option>', html)
        self.assertIn('<option value="archived">Archived</option>', html)
        self.assertIn('<option value="missing">Missing</option>', html)
        self.assertIn('<option value="all">全部</option>', html)
        self.assertNotIn('name="materialized"', html)
        self.assertNotIn('name="nativeState"', html)
        for unsafe in (
            "innerHTML",
            "outerHTML",
            "insertAdjacentHTML",
            "document.write",
            "eval(",
        ):
            self.assertNotIn(unsafe, javascript)
        self.assertIn("textContent", javascript)
        self.assertIn("applyRuntimeSnapshots", javascript)
        self.assertIn("fetchRuntimeSnapshots", javascript)
        self.assertIn("chunkValues(bindingIds)", javascript)
        self.assertIn("runtime.primaryStatus", javascript)
        self.assertIn("runtime.subscriptionState", javascript)
        self.assertIn("mergeDeferredBindingRuntime", javascript)
        self.assertIn(
            "activityRevision: previous?.activityRevision ?? incoming.activityRevision",
            javascript,
        )
        self.assertIn('primaryStatusResolution: "deferred"', javascript)
        self.assertIn(
            'previous.primaryStatusResolution === "unavailable"',
            javascript,
        )
        self.assertIn("session.pointerState", javascript)
        self.assertIn("session.catalogState", javascript)
        self.assertNotIn("session.nativeState", javascript)
        self.assertIn('query.set("resolvePrimary", "true")', javascript)
        self.assertIn("result?.message", javascript)
        self.assertNotIn("if (runtime.turn)", javascript)
        self.assertNotIn("if (runtime.goal)", javascript)
        self.assertIn("resetSessionPagination", javascript)
        self.assertIn("sessionLocationCell", javascript)
        self.assertIn('link.rel = "noopener noreferrer"', javascript)
        self.assertIn("await refresh(state.tab)", javascript)
        self.assertIn('state.tab === "sessions"', javascript)
        self.assertIn('state.tab === "side-topics"', javascript)
        self.assertRegex(javascript, r"setInterval\([\s\S]+?,\s*5000\s*\)")

    def test_time_range_filter_is_shared_accessible_and_uses_utc_bounds(self) -> None:
        root = Path(__file__).resolve().parents[2] / "netizen/admin/static"
        html = (root / "index.html").read_text(encoding="utf-8")
        javascript = (root / "admin.js").read_text(encoding="utf-8")
        stylesheet = (root / "admin.css").read_text(encoding="utf-8")

        self.assertEqual(html.count('class="time-range-filter" data-time-range'), 2)
        self.assertIn('id="time-range-template"', html)
        self.assertIn('name="createdFrom" data-time-range-from', html)
        self.assertIn('name="createdBefore" data-time-range-before', html)
        self.assertEqual(html.count('type="datetime-local"'), 2)
        self.assertIn('role="dialog"', html)
        self.assertIn('role="alert" aria-live="polite"', html)
        self.assertNotIn("ISO-8601", html)
        for preset in (
            "all",
            "today",
            "yesterday",
            "last-24-hours",
            "last-7-days",
            "last-30-days",
        ):
            self.assertIn(f'data-time-range-preset="{preset}"', html)
        for label in ("清除", "取消", "完成"):
            self.assertIn(f">{label}</button>", html)

        self.assertIn('document.querySelectorAll("[data-time-range]")', javascript)
        self.assertIn("initializeTimeRangeFilter(root)", javascript)
        self.assertIn("Intl.DateTimeFormat().resolvedOptions().timeZone", javascript)
        self.assertIn("function canonicalUtc(value)", javascript)
        self.assertIn('000+00:00`', javascript)
        self.assertIn("function customTimeRange(startValue, endValue)", javascript)
        self.assertIn("parsedEnd.date.getTime() + 60 * 1000", javascript)
        self.assertIn('popover.setAttribute("aria-modal", "true")', javascript)
        self.assertIn("function setTimeRangeModalIsolation", javascript)
        self.assertIn('sibling.setAttribute("inert", "")', javascript)
        self.assertIn("timeRangeModalOwner !== controller", javascript)
        self.assertIn('event.key === "Escape"', javascript)
        self.assertIn('trigger.setAttribute("aria-expanded", "false")', javascript)
        self.assertIn('zone.id = `${identity}-zone`', javascript)
        self.assertIn(
            'start.setAttribute("aria-describedby", `${help.id} ${zone.id} ${error.id}`)',
            javascript,
        )
        self.assertIn(
            'end.setAttribute("aria-describedby", `${help.id} ${zone.id} ${error.id}`)',
            javascript,
        )
        self.assertIn("cursor === null", javascript)
        self.assertIn("root.getBoundingClientRect()", javascript)
        self.assertIn("window.innerWidth - margin - width", javascript)

        self.assertIn(".time-range-popover", stylesheet)
        self.assertIn(".time-range-backdrop", stylesheet)
        self.assertIn("@media (max-width: 650px)", stylesheet)
        self.assertIn("position: fixed", stylesheet)

    def test_time_range_javascript_behavior_across_timezones(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is unavailable for the Admin JavaScript behavior test")
        javascript = (
            Path(__file__).resolve().parents[2] / "netizen/admin/static/admin.js"
        ).read_text(encoding="utf-8")
        start = javascript.index("let timeRangeModalOwner")
        end = javascript.index("function initializeTimeRangeFilter")
        core = javascript[start:end]
        harness = "const document = { body: null };\n" + core + r"""
const now = new Date("2026-09-02T08:20:30.456Z");
const today = presetTimeRange("today", now);
const rolling = presetTimeRange("last-24-hours", now);
const custom = customTimeRange("2026-08-27T00:00", "2026-09-02T23:59");
const reversed = customTimeRange("2026-09-02T10:00", "2026-09-02T09:59");
const gap = customTimeRange("2026-03-08T02:30", "2026-03-08T03:30");
const fold = customTimeRange("2026-11-01T01:30", "2026-11-01T02:30");

function fakeNode(name, isBackdrop = false) {
  const attributes = new Set();
  return {
    name,
    isBackdrop,
    attributes,
    children: [],
    parentElement: null,
    matches(selector) {
      return selector === "[data-time-range-backdrop]" && this.isBackdrop;
    },
    hasAttribute(attribute) { return attributes.has(attribute); },
    setAttribute(attribute) { attributes.add(attribute); },
    removeAttribute(attribute) { attributes.delete(attribute); },
  };
}
function append(parent, ...children) {
  parent.children.push(...children);
  for (const child of children) child.parentElement = parent;
}
const bodyClasses = new Set();
const body = fakeNode("body");
body.classList = {
  toggle(name, enabled) {
    if (enabled) bodyClasses.add(name);
    else bodyClasses.delete(name);
  },
};
document.body = body;
const preexisting = fakeNode("preexisting");
preexisting.setAttribute("inert", "");
const header = fakeNode("header");
const main = fakeNode("main");
const root = fakeNode("root");
const otherRoot = fakeNode("other-root");
const trigger = fakeNode("trigger");
const backdrop = fakeNode("backdrop", true);
const popover = fakeNode("popover");
append(body, preexisting, header, main);
append(main, root, otherRoot);
append(root, trigger, backdrop, popover);
const firstController = {};
const secondController = {};
setTimeRangeModalIsolation(firstController, popover, true);
const modalAfterOpen = bodyClasses.has("time-range-modal-open");
const isolatedAfterOpen = [header, otherRoot, trigger]
  .every((node) => node.hasAttribute("inert"));
const dialogPathAvailable = !main.hasAttribute("inert")
  && !root.hasAttribute("inert")
  && !popover.hasAttribute("inert")
  && !backdrop.hasAttribute("inert");
setTimeRangeModalIsolation(secondController, popover, false);
const unaffectedByClosedPeer = bodyClasses.has("time-range-modal-open")
  && trigger.hasAttribute("inert");
setTimeRangeModalIsolation(firstController, popover, false);
const restoredAfterClose = !bodyClasses.has("time-range-modal-open")
  && [header, otherRoot, trigger].every((node) => !node.hasAttribute("inert"))
  && preexisting.hasAttribute("inert");

process.stdout.write(JSON.stringify({
  today,
  rolling,
  custom,
  reversed,
  gap,
  fold,
  modalAfterOpen,
  isolatedAfterOpen,
  dialogPathAvailable,
  unaffectedByClosedPeer,
  restoredAfterClose,
}));
"""

        def run(timezone: str) -> dict:
            completed = subprocess.run(
                [node, "-e", harness],
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, "TZ": timezone},
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            return json.loads(completed.stdout)

        shanghai = run("Asia/Shanghai")
        self.assertEqual(
            (shanghai["today"]["from"], shanghai["today"]["before"]),
            (
                "2026-09-01T16:00:00.000000+00:00",
                "2026-09-02T16:00:00.000000+00:00",
            ),
        )
        self.assertEqual(
            (shanghai["rolling"]["from"], shanghai["rolling"]["before"]),
            (
                "2026-09-01T08:20:30.456000+00:00",
                "2026-09-02T08:20:30.456000+00:00",
            ),
        )
        self.assertEqual(
            (shanghai["custom"]["range"]["from"], shanghai["custom"]["range"]["before"]),
            (
                "2026-08-26T16:00:00.000000+00:00",
                "2026-09-02T16:00:00.000000+00:00",
            ),
        )
        self.assertEqual(shanghai["reversed"]["invalid"], "end")
        for key in (
            "modalAfterOpen",
            "isolatedAfterOpen",
            "dialogPathAvailable",
            "unaffectedByClosedPeer",
            "restoredAfterClose",
        ):
            self.assertTrue(shanghai[key], key)

        new_york = run("America/New_York")
        self.assertIn("不存在", new_york["gap"]["error"])
        self.assertIn("重复", new_york["fold"]["error"])


def _cookie_value(headers: list[tuple[str, str]], name: str) -> str:
    for key, value in headers:
        if key == "set-cookie" and value.startswith(name + "="):
            return value.split(";", 1)[0].split("=", 1)[1]
    raise AssertionError(f"missing cookie {name}")


def _action_payload(envelope: dict, **extra) -> dict:
    return {
        "csrfToken": envelope["csrfToken"],
        "actionToken": envelope["actionToken"],
        "target": envelope["target"],
        **extra,
    }


if __name__ == "__main__":
    unittest.main()
