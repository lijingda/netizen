from __future__ import annotations

import asyncio
import html
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import unittest
from html.parser import HTMLParser
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

from netizen_cli.admin.auth import AdminAuth
from netizen_cli.admin.feishu import DeviceChallenge, UserProfile, UserTokens
from netizen_cli.admin.login import AdminLogin
from netizen_cli.admin.users import AdminIdentity, AdminUsers
from netizen_cli.admin.errors import AdminWebError
from netizen_cli.admin.presentation import (
    _chat_open_url,
    _release_disposition_message,
    _stop_disposition_message,
    _topic_open_url,
)
from netizen_cli.admin.queries import (
    _created_range_query,
    _current_query,
    _decode_session_cursor,
    _encode_cursor,
    _encode_session_cursor,
    _scope_kinds_query,
    _session_inventory_state,
    _session_page_size,
    _text_set_query,
    _updated_range_query,
)
from netizen_cli.admin.web import (
    AdminWebRunner,
    _batches,
    accepted_authorities,
)
from netizen_cli.bindings import (
    BindingCursor,
    BindingContextRevisionConflict,
    BindingFeedbackRevisionConflict,
    BindingInventoryRecord,
    BindingQueryLimitExceeded,
    BindingTurnSettings,
    BindingSettingsRevisionConflict,
    ProjectAggregate,
    ProjectAggregatePage,
    ProjectRecord,
    ScopeRecord,
    SideTopicInventoryRecord,
    SideTopicCursor,
    SideTopicRecord,
    SideTopicState,
    ThreadBinding,
)
from netizen_cli.codex_runtime import (
    ActiveState,
    ActiveTurnSnapshot,
    BindingRuntimeSnapshot,
    NativeThreadCatalogState,
    NativeThreadMetadata,
    ReleaseDisposition,
    SideSessionSnapshot,
    SideSessionConflict,
    SideSessionState,
    StopDisposition,
    ThreadLifecycleSnapshot,
    ThreadLifecycleState,
    ThreadSubscriptionSnapshot,
    ThreadSubscriptionState,
    ThreadOccupied,
)
from netizen_cli.domain import GoalStatus, MentionContextMode, ScopeKind
from netizen_cli.runtime.contracts import ThreadCatalogError
from netizen_cli.session_settings import BindingTaskFeedback, SessionSettings
from netizen_cli.management import (
    BindingStatusProjection,
    ChatLabel,
    ChatLabelResolver,
    ClosedSide,
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
    SessionNativeCursor,
    SideTopicInventoryItem,
    SideTopicInventoryPage,
    StoppedBinding,
)
from netizen_cli.management.service import _project_binding_status
from netizen_cli.management.chat_directory import AvailableChat, AvailableChatPage, ChatDirectoryError
from netizen_cli.management.updates import UpdateError
from netizen_cli.projects import Project
from netizen_cli.sdk_gap_adapter import GoalSnapshot


class FakeFeishuClient:
    """Synthetic identity endpoint: never contacts the real application."""

    app_id = "cli_test"

    def __init__(self, clock=time.time):
        self.clock = clock
        self.profile = UserProfile("ou_admin", "Admin", "https://example.test/avatar.png")
        self.start_calls = 0
        self.poll_calls = 0

    async def start_login(self):
        self.start_calls += 1
        return DeviceChallenge(
            "synthetic-device-code", "SYNTHETIC", "https://accounts.feishu.cn/synthetic",
            self.clock() + 600, 1,
        )

    async def poll_login(self, _device_code):
        self.poll_calls += 1
        return UserTokens(
            "synthetic-access-token", "synthetic-refresh-token", self.clock() + 600,
            self.clock() + 3_600, ("component:selector", "offline_access"),
        )

    async def user_info(self, _access_token):
        return self.profile

    async def aclose(self):
        pass


class FakeManagement:
    async def resolve_chat_labels(self, chat_ids, *, deadline):
        return {chat_id: ChatLabelResolver.fallback(chat_id) for chat_id in chat_ids}

    def __init__(self, root: Path) -> None:
        self.native_delete_available = True
        self.defaults = None
        self.calls: list[tuple[str, object]] = []
        self.query_session_calls: list[dict[str, object]] = []
        self.query_side_topic_calls: list[dict[str, object]] = []
        self.snapshot_batches: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
        self.session_items_override: tuple[SessionInventoryItem, ...] | None = None
        self.persisted_goals: dict[str, GoalSnapshot] = {}
        self.set_enabled_entered: asyncio.Event | None = None
        self.set_enabled_release: asyncio.Event | None = None
        self.update_data = {
            "current": {
                "version": "1.0.0", "source": "python", "installationId": "a" * 64,
                "python": str(root / "env/bin/python"),
            },
            "restartSupported": False,
            "restartAvailable": False,
            "operation": None,
        }
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

    async def update_status(self):
        self.calls.append(("update_status", None))
        return json.loads(json.dumps(self.update_data))

    async def instance_diagnostics(self):
        self.calls.append(("instance_diagnostics", None))
        return {"root": "/instance", "checkedAt": 1900000000,
                "bound": True, "running": True, "ready": True, "loaded": True, "enabled": None,
                "binding": {"python": "/env/bin/python", "prefix": "/env", "codexHome": None}}

    async def restart_service(self, *, installation_id):
        self.calls.append(("restart_service", installation_id))
        self.update_data["restartAvailable"] = False
        self.update_data["operation"] = {
            "schema": 3, "kind": "restart", "operationId": "d" * 32,
            "target": {
                "version": self.update_data["current"]["version"],
                "installationId": installation_id,
            },
            "phase": "accepted", "code": "none",
            "createdAt": 1900000000, "updatedAt": 1900000000,
        }
        return self.update_data["operation"]

    async def query_project_options(self, **_kwargs):
        aggregate = ProjectAggregate(self.project_record, 3, 1, 2, "2030-01-02")
        return ProjectAggregatePage((aggregate,), None)

    async def query_projects(self, **_kwargs):
        aggregate = ProjectAggregate(self.project_record, 3, 1, 2, "2030-01-02")
        return ProjectInventoryPage((ProjectInventoryItem(aggregate),), None)

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
                    NativeThreadMetadata("native-active", "Active", "preview", created_at=1_730_000_000, updated_at=1_730_831_111),
                ),
                label,
            ),
            SessionInventoryItem(
                BindingInventoryRecord(self.archived, self.scope),
                NativeThreadView(
                    NativeThreadCatalogState.ARCHIVED,
                    NativeThreadMetadata("native-archived", "Archived", "old", created_at=1_720_000_000, updated_at=1_730_750_000),
                ),
                label,
            ),
        )
        state = _kwargs["query"].inventory_state
        return SessionInventoryPage(tuple(item for item in records if item.inventory_state is state), None)

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
            self.native,
            NativeThreadMetadata("native-active", "Source session", "source preview"),
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

    async def delete_exact_binding(self, **values):
        self.calls.append(("delete-materialized", values))
        return self._binding(values["target"].binding_id)

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

    async def stop_side(self, **values):
        self.calls.append(("stop-side", values))
        return StopDisposition.REQUESTED


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

    def test_session_inventory_state_is_single_and_defaults_to_active(self) -> None:
        self.assertIs(_session_inventory_state({}), SessionInventoryState.ACTIVE)
        for state in (SessionInventoryState.ACTIVE, SessionInventoryState.ARCHIVED, SessionInventoryState.LAZY):
            self.assertIs(_session_inventory_state({"inventoryState": [state.value]}), state)
        for values in (["materialized"], ["all"], ["unknown"], ["missing"], ["active", "lazy"], ["active", "active"], [""], []):
            with self.subTest(values=values), self.assertRaises(AdminWebError):
                _session_inventory_state({"inventoryState": values})

    def test_session_cursor_preserves_source_page_and_consumed_offset(self) -> None:
        for cursor in (
            SessionNativeCursor(None, 17),
            SessionNativeCursor("native-page", 99),
            SessionNativeCursor("start", 0),
        ):
            with self.subTest(cursor=cursor):
                encoded = _encode_session_cursor(cursor, "filters")
                decoded = _decode_session_cursor(encoded, "filters", inventory_state=SessionInventoryState.ACTIVE)
                self.assertEqual(decoded, cursor)
        for values in (
            ("start", "not-start", "0"), ("invalid", "start", "0"),
            ("next", "native-page", "-1"), ("next", "native-page", "100"),
            ("next", "native-page", "01"), ("next", "native-page", "1.0"),
        ):
            with self.subTest(values=values), self.assertRaises(AdminWebError):
                _decode_session_cursor(
                    _encode_cursor("session-native", values, "filters"),
                    "filters", inventory_state=SessionInventoryState.ACTIVE,
                )

    def test_session_multiselect_values_are_exact_and_canonical(self) -> None:
        self.assertEqual(
            _text_set_query({"project": ["beta", "alpha", "beta"]}, "project"),
            ("alpha", "beta"),
        )
        self.assertIsNone(_text_set_query({}, "project"))
        self.assertEqual(
            _scope_kinds_query({"scopeKind": ["topic", "direct", "topic"]}),
            (ScopeKind.DIRECT, ScopeKind.TOPIC),
        )
        self.assertIsNone(_scope_kinds_query({
            "scopeKind": [kind.value for kind in ScopeKind]
        }))
        self.assertIsNone(_current_query({"current": ["true", "false", "true"]}))
        self.assertIs(_current_query({"current": ["true", "true"]}), True)
        self.assertIs(_current_query({"current": ["false"]}), False)
        for values in ([""], [" alpha"], []):
            with self.subTest(values=values), self.assertRaises(AdminWebError):
                _text_set_query({"project": values}, "project")
        with self.assertRaises(AdminWebError):
            _scope_kinds_query({"scopeKind": ["direct", "other"]})
        with self.assertRaises(AdminWebError):
            _current_query({"current": ["true", "yes"]})

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

    def test_topic_link_opens_root_with_encoded_desktop_and_mobile_ids(self) -> None:
        chat_id = "oc_group+/?&="
        topic_id = "omt_topic+/?&="
        url = urlsplit(_topic_open_url(chat_id, topic_id))
        self.assertEqual((url.scheme, url.netloc, url.path), (
            "https", "applink.feishu.cn", "/client/thread/open",
        ))
        self.assertEqual(parse_qs(url.query), {
            "open_chat_id": [chat_id],
            "openchatid": [chat_id],
            "open_thread_id": [topic_id],
            "openthreadid": [topic_id],
            "thread_position": ["-1"],
        })

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

    def test_updated_range_uses_the_same_strict_instant_contract(self) -> None:
        self.assertEqual(_updated_range_query({
            "updatedFrom": ["2030-01-02T08:30+08:00"],
            "updatedBefore": ["2030-01-03T00:00Z"],
        }), ("2030-01-02T00:30:00.000000+00:00", "2030-01-03T00:00:00.000000+00:00"))
        for values in (
            {"updatedFrom": ["2030-01-02T00:00"]},
            {"updatedBefore": ["2030-02-30T00:00Z"]},
            {"updatedFrom": ["2030-01-03T00:00Z"], "updatedBefore": ["2030-01-02T00:00Z"]},
            {"updatedFrom": ["2030-01-03T00:00Z"], "updatedBefore": ["2030-01-03T08:00+08:00"]},
        ):
            with self.subTest(values=values), self.assertRaises(AdminWebError):
                _updated_range_query(values)


class AdminWebTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.users = AdminUsers(self.root, "cli_test")
        self.member = self.users.add_initial("ou_admin")
        self.auth_now = 1_000.0
        self.auth = AdminAuth(self.users, clock=lambda: self.auth_now)
        self.feishu = FakeFeishuClient(clock=lambda: self.auth_now)
        self.management = FakeManagement(self.root)

        def loopback_authorities(_host, _port, addresses):
            address = addresses[0]
            assert isinstance(address, tuple)
            return (f"127.0.0.1:{address[1]}",)

        # These cases exercise the HTTP application, while authority discovery
        # has focused coverage below. Keep every case independent of host DNS,
        # which can take about a minute on a macOS CI runner.
        self.authority_builder_patch = patch(
            "netizen_cli.admin.web.accepted_authorities",
            side_effect=loopback_authorities,
        )
        self.authority_builder_patch.start()
        self.addCleanup(self.authority_builder_patch.stop)
        with patch("netizen_cli.admin.web.AdminLogin", side_effect=lambda auth, users, credentials, client: AdminLogin(
            auth, users, credentials, client, clock=lambda: self.auth_now,
        )):
            self.runner = AdminWebRunner(
                host="127.0.0.1",
                port=0,
                users=self.users,
                feishu_client=self.feishu,
                auth=self.auth,
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

    async def login_form(self, target: str = "/login") -> tuple[str, str, bytes]:
        status, headers, body = await self.request("GET", target)
        self.assertEqual(status, 200)
        self.assertEqual(dict(headers)["content-type"], "text/html; charset=utf-8")
        preauth = _cookie_value(headers, "netizen_admin_preauth")
        nonce_match = re.search(rb'id="login-nonce"[^>]*value="([^"]+)"', body)
        assert nonce_match is not None
        return preauth, nonce_match.group(1).decode("ascii"), body

    async def submit_login(
        self, preauth: str, nonce: str,
    ) -> tuple[int, list[tuple[str, str]], bytes]:
        return await self.request(
            "POST",
            "/api/v1/login/start",
            headers=[
                ("Origin", self.origin),
                ("Content-Type", "application/json"),
                ("Cookie", f"netizen_admin_preauth={preauth}"),
            ],
            body=json.dumps({"nonce": nonce}).encode(),
        )

    async def login(self) -> str:
        return self.auth.issue_session(
            AdminIdentity("cli_test", "ou_admin"), source_ip="127.0.0.1",
            expected_add_revision=self.member.add_revision,
        ).token

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

    async def test_chat_picker_queries_and_assets_require_authentication(self) -> None:
        self.runner.open_admission()
        for path in ("/api/v1/chats", "/api/v1/chats/validate?chatId=oc_test", "/static/chat-picker.js", "/static/chat-picker.css"):
            status, _, _ = await self.request("GET", path)
            self.assertEqual(status, 401, path)
        session = await self.login()
        for path, content_type in (("/static/chat-picker.js", "text/javascript; charset=utf-8"), ("/static/chat-picker.css", "text/css; charset=utf-8")):
            status, headers, content = await self.request("GET", path, headers=[("Cookie", f"netizen_admin_session={session}")])
            self.assertEqual(status, 200)
            self.assertEqual(dict(headers)["content-type"], content_type)
            self.assertIn("script-src 'self'", dict(headers)["content-security-policy"])
            self.assertIn("img-src 'self' https://p3-lark-file.byteimg.com", dict(headers)["content-security-policy"])
            self.assertNotIn("img-src *", dict(headers)["content-security-policy"])
            self.assertNotIn("https://*.", dict(headers)["content-security-policy"])
            self.assertTrue(content)

    async def test_chat_picker_pagination_is_bound_to_keyword(self) -> None:
        self.runner.open_admission()
        session = await self.login()
        avatar = "https://p3-lark-file.byteimg.com/img/avatar.jpg"
        group = AvailableChat("oc_group", "<群聊>", None, False, avatar)
        self.management.query_available_chats = AsyncMock(return_value=AvailableChatPage((group,), "page-two", "搜索提示"))
        status, _, payload = await self.json_get("/api/v1/chats?query=", session)
        self.assertEqual(status, 200)
        self.assertEqual(payload["items"], [{"chatId": "oc_group", "name": "<群聊>", "chatMode": None, "external": False, "avatarUrl": avatar}])
        self.assertEqual(payload["notice"], "搜索提示")
        self.management.query_available_chats.assert_awaited_with(query="", page_token=None)
        cursor = payload["nextCursor"]
        self.management.query_available_chats.return_value = AvailableChatPage((), None)
        status, _, payload = await self.json_get("/api/v1/chats?" + urlencode({"cursor": cursor}), session)
        self.assertEqual(status, 200)
        self.assertIsNone(payload["nextCursor"])
        self.management.query_available_chats.assert_awaited_with(query="", page_token="page-two")
        status, _, payload = await self.json_get("/api/v1/chats?" + urlencode({"query": "群", "cursor": cursor}), session)
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "invalid_cursor")
        self.assertEqual(self.management.query_available_chats.await_count, 2)

    async def test_chat_picker_rejects_invalid_query_before_service(self) -> None:
        self.runner.open_admission()
        session = await self.login()
        self.management.query_available_chats = AsyncMock()
        for query in ({"query": "x" * 51}, {"query": " x"}, {"cursor": ""}, {"pageSize": 100}, {"cursor": "malformed"}, {"cursor": "x" * 2_049}):
            status, _, _ = await self.json_get("/api/v1/chats?" + urlencode(query), session)
            self.assertEqual(status, 400, query)
        status, _, _ = await self.json_get("/api/v1/chats?query=a&query=b", session)
        self.assertEqual(status, 400)
        self.management.query_available_chats.assert_not_awaited()

    async def test_chat_picker_validation_and_errors_are_explicit(self) -> None:
        self.runner.open_admission()
        session = await self.login()
        avatar = "https://s1-imfile.feishucdn.com/static-resource/avatar.jpg"
        group = AvailableChat("oc_group", "新名称", "topic", False, avatar)
        self.management.validate_available_chat = AsyncMock(return_value=group)
        status, _, payload = await self.json_get("/api/v1/chats/validate?chatId=oc_group", session)
        self.assertEqual(status, 200)
        self.assertEqual(payload["chat"]["name"], "新名称")
        self.assertEqual(payload["chat"]["avatarUrl"], avatar)
        self.management.validate_available_chat.assert_awaited_once_with("oc_group")
        for query in ("", "?chatId=", "?chatId=oc_a&chatId=oc_b", "?chatId=oc_group&unknown=x"):
            status, _, _ = await self.json_get("/api/v1/chats/validate" + query, session)
            self.assertEqual(status, 400, query)
        self.assertEqual(self.management.validate_available_chat.await_count, 1)
        for code, status in (("chat_permission_denied", 403), ("chat_query_timeout", 503), ("chat_unavailable", 409), ("chat_query_rate_limited", 429)):
            self.management.validate_available_chat.side_effect = ChatDirectoryError(code, "可显示的明确错误")
            actual_status, _, payload = await self.json_get("/api/v1/chats/validate?chatId=oc_group", session)
            self.assertEqual(actual_status, status)
            self.assertEqual(payload["code"], code)
            self.assertNotIn("items", payload)

    async def test_instance_diagnostics_is_authenticated_root_scoped_and_read_only(self) -> None:
        self.runner.open_admission()
        status, _, _ = await self.request("GET", "/api/v1/instance-diagnostics")
        self.assertEqual(status, 401)
        self.assertNotIn(("instance_diagnostics", None), self.management.calls)
        session = await self.login()
        status, _, data = await self.json_get("/api/v1/instance-diagnostics", session)
        self.assertEqual(status, 200)
        self.assertEqual(data["root"], "/instance")
        self.assertTrue(data["ready"])
        self.assertIsNone(data["enabled"])
        self.assertEqual(data["binding"]["python"], "/env/bin/python")
        self.assertNotIn("actions", data)
        self.assertNotIn(("update_status", None), self.management.calls)
        calls = tuple(self.management.calls)
        for suffix in ("?root=/other", "?command=restart", "?all=true"):
            status, _, _ = await self.json_get("/api/v1/instance-diagnostics" + suffix, session)
            self.assertEqual(status, 400)
        status, _, _ = await self.json_post("/api/v1/instance-diagnostics", session, {})
        self.assertEqual(status, 404)
        self.assertEqual(tuple(self.management.calls), calls)

    async def test_instance_diagnostics_failure_is_not_a_stopped_service(self) -> None:
        self.runner.open_admission()
        session = await self.login()
        with patch.object(self.management, "instance_diagnostics", side_effect=UpdateError("diagnostics_unavailable")):
            status, _, data = await self.json_get("/api/v1/instance-diagnostics", session)
        self.assertEqual(status, 503)
        self.assertEqual(data["code"], "diagnostics_unavailable")
        self.assertNotIn("running", data)
        status, _, maintenance = await self.json_get("/api/v1/updates", session)
        self.assertEqual(status, 200)
        self.assertIn("current", maintenance)

    async def test_updates_require_auth_and_admin_has_no_package_update_authority(self) -> None:
        self.runner.open_admission()
        status, _, _ = await self.request("GET", "/api/v1/updates")
        self.assertEqual(status, 401)
        session = await self.login()
        status, _, data = await self.json_get("/api/v1/updates", session)
        self.assertEqual(status, 200)
        self.assertIn("netizen update", data["message"])
        calls = tuple(self.management.calls)
        for action in ("check", "install"):
            status, _, result = await self.json_post(f"/api/v1/updates/{action}", session, {})
            self.assertEqual(status, 409)
            self.assertEqual(result["code"], "update_unsupported")
        self.assertEqual(tuple(self.management.calls), calls)

    async def test_maintenance_rejects_tampering_and_untrusted_requests(self) -> None:
        self.runner.open_admission()
        self.management.update_data.update(restartSupported=True, restartAvailable=True)
        session = await self.login()
        other_session = await self.login()
        targets = {
            "restart": ({"targetId": "d" * 64}, {"resource": "instance-update"},
                        {"scopeKey": "foreign-scope"}, {"command": "sh malicious.sh"}),
        }
        for action, alterations in targets.items():
            with self.subTest(action=action):
                path = f"/api/v1/updates/{action}"
                for alteration in alterations:
                    _, _, data = await self.json_get("/api/v1/updates", session)
                    payload = _action_payload(data["actions"][action])
                    payload["target"] = {**payload["target"], **alteration}
                    status, _, _ = await self.json_post(path, session, payload)
                    self.assertIn(status, (400, 409), alteration)
                _, _, data = await self.json_get("/api/v1/updates", session)
                payload = _action_payload(data["actions"][action])
                status, _, _ = await self.request("POST", path, headers=[
                    ("Cookie", f"netizen_admin_session={session}"),
                    ("Content-Type", "application/json"), ("Origin", "https://untrusted.example"),
                ], body=json.dumps(payload).encode())
                self.assertEqual(status, 403)
                for extra, expected in (({"csrfToken": "invalid"}, 403), ({"command": "restart"}, 400)):
                    status, _, _ = await self.json_post(path, session, {**payload, **extra})
                    self.assertEqual(status, expected)
                status, _, _ = await self.json_post(path + "?command=restart", session, payload)
                self.assertEqual(status, 400)
                status, _, _ = await self.json_post(path, other_session, payload)
                self.assertIn(status, (403, 409))
        self.assertFalse(any(call[0] == "restart_service" for call in self.management.calls))

    async def test_restart_requires_auth_and_binds_exact_current_installation_once(self) -> None:
        self.runner.open_admission()
        status, _, _ = await self.request("POST", "/api/v1/updates/restart")
        self.assertEqual(status, 401)
        self.management.update_data.update(restartSupported=True, restartAvailable=True)
        session = await self.login()
        _, _, data = await self.json_get("/api/v1/updates", session)
        grant = data["actions"]["restart"]
        self.assertEqual(grant["target"], {
            "resource": "instance-restart", "targetId": "a" * 64, "scopeKey": None,
        })
        # Forward the granted identity even if a later read sees another installation.
        # UpdateService must reject that stale identity inside the install lock.
        self.management.update_data["current"]["installationId"] = "f" * 64
        status, _, result = await self.json_post(
            "/api/v1/updates/restart", session, _action_payload(grant)
        )
        self.assertEqual(status, 202)
        self.assertEqual(result["operation"]["kind"], "restart")
        self.assertEqual(result["operation"]["phase"], "accepted")
        self.assertEqual(result["operation"]["target"]["installationId"], "a" * 64)
        self.assertEqual(self.management.calls[-1], ("restart_service", "a" * 64))
        status, _, _ = await self.json_post(
            "/api/v1/updates/restart", session, _action_payload(grant)
        )
        self.assertEqual(status, 409)
        self.assertEqual(self.management.calls.count(("restart_service", "a" * 64)), 1)
        _, _, data = await self.json_get("/api/v1/updates", session)
        self.assertIsNone(data["actions"]["restart"])

    async def test_restart_grants_follow_instance_availability(self) -> None:
        self.runner.open_admission()
        session = await self.login()
        for supported, available in ((False, False), (True, False), (True, True)):
            with self.subTest(supported=supported, available=available):
                self.management.update_data.update(
                    restartSupported=supported, restartAvailable=available,
                )
                _, _, data = await self.json_get("/api/v1/updates", session)
                self.assertEqual(data["actions"]["restart"] is not None, available)

    async def test_maintenance_history_remains_visible_when_restart_is_unavailable(self) -> None:
        self.runner.open_admission()
        session = await self.login()
        previous_release = "b" * 64
        common = {
            "operationId": "e" * 32, "previousRelease": previous_release,
            "createdAt": 1900000000, "updatedAt": 1900000001,
        }
        operations = (
            {**common, "schema": 1, "phase": "rolled_back", "code": "activation_failed",
             "target": {"version": "0.9.0", "releaseId": 123,
                        "installerSha256": "c" * 64, "archiveSha256": "d" * 64}},
            {**common, "schema": 2, "kind": "restart", "phase": "recovery_required",
             "code": "restart_failed", "target": {"version": "0.9.0", "releaseDigest": previous_release}},
        )
        for operation in operations:
            with self.subTest(schema=operation["schema"]):
                self.management.update_data["operation"] = operation
                status, _, data = await self.json_get("/api/v1/updates", session)
                self.assertEqual(status, 200)
                self.assertEqual(data["operation"], operation)
                self.assertIsNone(data["actions"]["restart"])

    async def test_update_errors_keep_stable_http_code_and_message(self) -> None:
        self.runner.open_admission()
        self.management.update_data.update(restartSupported=True, restartAvailable=True)
        session = await self.login()
        cases = (
            ("restart_unsupported", 409, "restart_unsupported", "仅绑定当前 Python 环境的受管实例支持 Admin 重启。"),
            ("update_busy", 409, "update_busy", "另一个安装或维护操作正在执行，请稍后查看结果。"),
            ("update_state_unavailable", 503, "update_state_unavailable", "维护状态无法确认，请检查实例维护记录。"),
            ("update_lock_unavailable", 503, "update_state_unavailable", "无法取得维护锁，请检查实例状态。"),
            ("update_installation_changed", 409, "update_target_changed", "当前安装已变化，请重新连接。"),
            ("update_already_submitted", 409, "update_busy", "维护操作已提交，请查看已有操作结果。"),
            ("update_recovery_required", 409, "update_recovery_required", "上次维护结果未确认，请检查服务状态并使用 netizen restart 恢复。"),
            ("update_cleanup_unavailable", 503, "update_cleanup_unavailable", "暂时无法清理上次维护任务，请稍后重试。"),
            ("update_submission_unknown", 503, "update_state_unavailable", "维护操作提交结果无法确认，请刷新查看；不要重复提交。"),
        )
        for reason, expected_status, code, message in cases:
            with self.subTest(reason=reason):
                _, _, data = await self.json_get("/api/v1/updates", session)
                action, method = "restart", "restart_service"
                with patch.object(self.management, method, side_effect=UpdateError(reason)):
                    status, _, result = await self.json_post(
                        f"/api/v1/updates/{action}", session, _action_payload(data["actions"][action]),
                    )
                self.assertEqual(status, expected_status)
                self.assertEqual(result["code"], code)
                self.assertEqual(result["message"], message)

    async def test_maintenance_status_explains_environment_scope(self) -> None:
        self.runner.open_admission()
        session = await self.login()
        status, _, data = await self.json_get("/api/v1/updates", session)
        self.assertEqual(status, 200)
        self.assertIn("Python 环境", data["message"])

    async def test_unknown_update_failure_does_not_expose_internal_details(self) -> None:
        self.runner.open_admission()
        session = await self.login()
        with patch.object(self.management, "update_status", side_effect=UpdateError("SECRET internal reason")), \
                self.assertLogs("netizen_cli.admin.web", level="ERROR"):
            status, _, result = await self.json_get("/api/v1/updates", session)
        self.assertEqual(status, 500)
        self.assertEqual(result["code"], "internal_error")
        self.assertEqual(result["message"], "服务内部错误。")
        self.assertNotIn("SECRET", json.dumps(result))

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
        self.assertIn(b'href="/static/admin.css"', login_page)
        self.assertIn(b'class="login-body"', login_page)
        self.assertIn(b'class="login-card"', login_page)
        self.assertNotIn(b'name="credential"', login_page)

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

    async def test_old_secret_login_cannot_issue_session_or_echo_credentials(self) -> None:
        self.runner.open_admission()
        preauth, nonce, _ = await self.login_form()
        credential = secrets.token_urlsafe(32)
        status, headers, body = await self.request(
            "POST", "/login", headers=[
                ("Origin", self.origin),
                ("Content-Type", "application/x-www-form-urlencoded"),
                ("Cookie", f"netizen_admin_preauth={preauth}"),
            ], body=urlencode({"nonce": nonce, "credential": credential}).encode(),
        )
        self.assertEqual(status, 401)
        self.assertEqual(self.auth.state_counts().sessions, 0)
        self.assertEqual(self.feishu.start_calls, 0)
        for secret in (preauth, nonce, credential):
            self.assertNotIn(secret, str(headers))
            self.assertNotIn(secret.encode(), body)

    async def test_feishu_login_creates_browser_bound_session_without_exposing_tokens(self) -> None:
        self.runner.open_admission()
        preauth, nonce, _ = await self.login_form()
        status, _, body = await self.submit_login(preauth, nonce)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["state"], "pending")
        self.auth_now += 1
        status, _, body = await self.request("GET", "/api/v1/login/state")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["state"], "idle")
        self.assertEqual(self.feishu.poll_calls, 0)
        status, headers, body = await self.request(
            "GET", "/api/v1/login/state",
            headers=[("Cookie", f"netizen_admin_preauth={preauth}")],
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["state"], "authenticated")
        session = _cookie_value(headers, "netizen_admin_session")
        for secret in ("synthetic-access-token", "synthetic-refresh-token", "synthetic-device-code"):
            self.assertNotIn(secret.encode(), body)
            self.assertNotIn(secret, str(headers))
        status, _, _ = await self.request(
            "GET", "/", headers=[("Cookie", f"netizen_admin_session={session}")],
        )
        self.assertEqual(status, 200)

    async def test_login_rate_limit_recovers_without_page_refresh_extending_it(self) -> None:
        self.runner.open_admission()
        for _ in range(5):
            preauth, nonce, _ = await self.login_form()
            status, _, _ = await self.submit_login(preauth, secrets.token_urlsafe(32))
            self.assertEqual(status, 401)
        counts = self.auth.state_counts()
        self.assertEqual(counts.global_failures, 5)
        self.assertEqual(counts.preauth, 0)
        self.assertEqual(self.feishu.start_calls, 0)
        for elapsed in (0, 150, 299):
            self.auth_now = 1_000.0 + elapsed
            status, headers, page = await self.request("GET", "/login")
            self.assertEqual(status, 401)
            self.assertEqual(dict(headers)["content-type"], "text/html; charset=utf-8")
            self.assertEqual(dict(headers)["cache-control"], "no-store")
            self.assertIn(b"frame-ancestors 'none'", dict(headers)["content-security-policy"].encode())
            self.assertIn("Max-Age=0", dict(headers)["set-cookie"])
            self.assertEqual(self.auth.state_counts(), counts)
        self.auth_now = 1_300.0
        session = await self.login()
        self.assertEqual(self.auth.state_counts().global_failures, 0)
        self.auth.authenticate(session)

    async def test_device_login_does_not_bypass_origin_validation(self) -> None:
        self.runner.open_admission()
        preauth, nonce, _ = await self.login_form()
        status, headers, body = await self.request(
            "POST", "/api/v1/login/start",
            headers=[
                ("Origin", "http://evil.invalid"),
                ("Content-Type", "application/json"),
                ("Cookie", f"netizen_admin_preauth={preauth}"),
            ],
            body=json.dumps({"nonce": nonce}).encode(),
        )
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["code"], "invalid_origin")
        self.assertNotIn("location", dict(headers))
        self.assertNotIn("set-cookie", dict(headers))
        self.assertEqual(self.auth.state_counts().sessions, 0)
        self.assertEqual(self.auth.state_counts().global_failures, 0)

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
        self.assertNotIn(session.encode(), payload)

    async def test_unauthenticated_root_redirects_to_login(self) -> None:
        self.runner.open_admission()
        status, headers, body = await self.request("GET", "/")
        self.assertEqual(status, 303)
        self.assertEqual(dict(headers)["location"], "/login")
        # The redirect must not serve the admin HTML or any state.
        self.assertEqual(body, b"")

    async def test_admin_pages_share_authenticated_document_and_preserve_login_target(self) -> None:
        self.runner.open_admission()
        session = await self.login()
        _, _, original = await self.request(
            "GET", "/", headers=[("Cookie", f"netizen_admin_session={session}")],
        )
        for path in (
            "/projects", "/sessions/ordinary", "/sessions/temporary", "/schedules",
            "/defaults", "/maintenance", "/system", "/worktrees", "/",
        ):
            target = path + "?project=netizen&project=demo&sortBy=updatedAt&sortDirection=asc"
            with self.subTest(path=path):
                status, headers, body = await self.request("GET", target)
                self.assertEqual(status, 303)
                location = urlsplit(dict(headers)["location"])
                self.assertEqual(location.path, "/login")
                self.assertEqual(parse_qs(location.query), {"next": [target]})
                self.assertEqual(body, b"")
                status, headers, body = await self.request(
                    "GET", target, headers=[("Cookie", f"netizen_admin_session={session}")],
                )
                self.assertEqual(status, 200)
                self.assertEqual(dict(headers)["content-type"], "text/html; charset=utf-8")
                self.assertEqual(body, original)

    async def test_login_preserves_legacy_binding_and_encoded_query_targets(self) -> None:
        self.runner.open_admission()
        for target in (
            "/?binding_id=binding-1",
            "/sessions/ordinary?project=R%26D&project=%E7%A0%94%E7%A9%B6&sortDirection=asc",
        ):
            with self.subTest(target=target):
                status, headers, _ = await self.request("GET", target)
                self.assertEqual(status, 303)
                login_target = dict(headers)["location"]
                _, _, body = await self.login_form(login_target)
                found = re.search(rb'id="login-next"[^>]*value="([^"]+)"', body)
                self.assertIsNotNone(found)
                self.assertEqual(html.unescape(found.group(1).decode()), target)
                self.assertNotIn(b"__NETIZEN_LOGIN_NEXT__", body)
                session = await self.login()
                status, headers, body = await self.request(
                    "GET", login_target, headers=[("Cookie", f"netizen_admin_session={session}")],
                )
                self.assertEqual(status, 303)
                self.assertEqual(dict(headers)["location"], target)
                self.assertNotIn("set-cookie", dict(headers))
                self.assertEqual(body, b"")

    async def test_login_rejects_external_unknown_and_ambiguous_return_targets(self) -> None:
        self.runner.open_admission()
        session = await self.login()
        unsafe_targets = (
            "https://evil.example/projects", "//evil.example/projects", "///evil.example",
            "///projects", "///sessions/ordinary?inventoryState=all", "///system", "////projects",
            "javascript:alert(1)", "/\\evil.example", "/projects\\evil.example",
            "/%2f%2fevil.example", "/projects/../login", "/projects%2f..%2flogin",
            "/api/v1/projects", "/static/admin.js", "/does-not-exist", "/login",
            "/projects#external", "/projects#", "/projects?value=%0d%0aLocation:evil",
            "/projects?value=%5cevil", "/projects\n", " /projects", "",
        )
        for target in unsafe_targets:
            with self.subTest(target=target):
                status, headers, _ = await self.request(
                    "GET", "/login?" + urlencode({"next": target}),
                    headers=[("Cookie", f"netizen_admin_session={session}")],
                )
                self.assertEqual(status, 303)
                self.assertEqual(dict(headers)["location"], "/projects")
        for query in ("", "?next=%2Fschedules&next=%2Fprojects"):
            status, headers, _ = await self.request(
                "GET", "/login" + query,
                headers=[("Cookie", f"netizen_admin_session={session}")],
            )
            self.assertEqual(status, 303)
            self.assertEqual(dict(headers)["location"], "/projects")
        for target in ("///projects", "///sessions/ordinary?inventoryState=all", "///system"):
            with self.subTest(anonymous_target=target):
                _, _, body = await self.login_form("/login?" + urlencode({"next": target}))
                self.assertIn(b'id="login-next" type="hidden" value="/projects"', body)
                self.assertNotIn(target.encode(), body)
        _, _, body = await self.login_form("/login?next=%2F%2Fevil.example")
        self.assertIn(b'id="login-next" type="hidden" value="/projects"', body)
        self.assertNotIn(b"evil.example", body)

    async def test_invalid_session_cookie_still_redirects_root_to_login(self) -> None:
        self.runner.open_admission()
        status, headers, _body = await self.request(
            "GET",
            "/",
            headers=[("Cookie", "netizen_admin_session=not-a-real-session")],
        )
        self.assertEqual(status, 303)
        self.assertEqual(dict(headers)["location"], "/login")

    async def test_revoked_session_cookie_redirects_root_to_login(self) -> None:
        self.runner.open_admission()
        session = await self.login()
        self.auth.revoke_identity(AdminIdentity("cli_test", "ou_admin"))
        status, headers, _body = await self.request(
            "GET",
            "/",
            headers=[("Cookie", f"netizen_admin_session={session}")],
        )
        self.assertEqual(status, 303)
        self.assertEqual(dict(headers)["location"], "/login")

    async def test_unauthenticated_non_root_routes_still_return_json_401(self) -> None:
        self.runner.open_admission()
        for target in ("/api/v1/projects", "/static/admin.js", "/does-not-exist", "/sessions/unknown"):
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
        with self.assertLogs("netizen_cli.admin.web", level="ERROR") as captured:
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

    async def test_administrator_removal_invalidates_existing_session(self) -> None:
        self.runner.open_admission()
        session = await self.login()
        self.users.add(
            AdminIdentity("cli_test", "ou_other"), actor=AdminIdentity("cli_test", "ou_admin"),
            expected_revision=self.users.revision,
        )
        self.users.remove(
            AdminIdentity("cli_test", "ou_admin"), actor=AdminIdentity("cli_test", "ou_other"),
            expected_revision=self.users.revision,
        )

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

    async def test_occupied_thread_returns_conflict_and_keeps_management_available(self) -> None:
        self.runner.open_admission()
        session = await self.login()
        status, _headers, page = await self.json_get(
            "/api/v1/sessions?inventoryState=active", session,
        )
        self.assertEqual(status, 200)
        item = next(row for row in page["items"] if row["bindingId"] == "binding-native")
        message = "该会话正被其他 Codex 实例占用，请从占用方归档后回飞书恢复。"
        with patch.object(
            self.management, "rename_exact_binding", side_effect=ThreadOccupied(message),
        ):
            status, _headers, payload = await self.json_post(
                "/api/v1/sessions/rename", session,
                _action_payload(item["actions"]["rename"], name="Renamed"),
            )
        self.assertEqual(status, 409)
        self.assertEqual(payload["code"], "thread_occupied")
        self.assertEqual(payload["message"], message)
        status, _headers, _page = await self.json_get(
            "/api/v1/sessions?inventoryState=active", session,
        )
        self.assertEqual(status, 200)

    async def test_session_configuration_prefills_and_submits_full_settings_with_server_revisions(self):
        self.runner.open_admission()
        self.management.lazy = replace(
            self.management.lazy, settings_revision=3, context_revision=5, feedback_revision=7,
            task_feedback=BindingTaskFeedback(True, False, False),
        )
        session = await self.login()
        status, _, page = await self.json_get("/api/v1/sessions?inventoryState=lazy", session)
        self.assertEqual(status, 200, page)
        item = page["items"][0]
        self.assertEqual(item["sessionSettings"], SessionSettings.from_binding(self.management.lazy).to_dict())
        self.assertNotIn("context_anchor", item["sessionSettings"])
        settings = SessionSettings(BindingTurnSettings("model", "high", "priority"), BindingTaskFeedback(False, True, True))
        payload = _action_payload(item["actions"]["configure"], sessionSettings=settings.to_dict())

        status, _, result = await self.json_post("/api/v1/sessions/configure", session, payload)

        self.assertEqual(status, 200, result)
        values = next(values for name, values in self.management.calls if name == "configure")
        self.assertEqual(values["target"].binding_id, item["bindingId"])
        self.assertEqual(values["target"].scope_key, item["scopeKey"])
        self.assertEqual(values["expected_settings_revision"], 3)
        self.assertEqual(values["expected_context_revision"], 5)
        self.assertEqual(values["expected_feedback_revision"], 7)
        self.assertEqual(values["settings"], settings.turn_settings)
        self.assertEqual(values["task_feedback"], settings.task_feedback)
        self.assertEqual(values["message_context_mode"], MentionContextMode.CURRENT_ONLY)
        status, _, _ = await self.json_post("/api/v1/sessions/configure", session, payload)
        self.assertEqual(status, 409)
        self.assertEqual(len([call for call in self.management.calls if call[0] == "configure"]), 1)

    async def test_session_configuration_rejects_partial_settings_and_client_preconditions(self):
        self.runner.open_admission()
        session = await self.login()
        settings = SessionSettings().to_dict()
        cases = (
            {"turnSettings": None},
            {"sessionSettings": None},
            {"sessionSettings": {"turn_settings": None}},
            {"sessionSettings": {**settings, "reaction_pulse_enabled": 1}},
            {"sessionSettings": {**settings, "turn_settings": {"model_id": "partial"}}},
            {"sessionSettings": {**settings, "context_anchor": "om_forged"}},
            {"sessionSettings": settings, "expectedFeedbackRevision": 1},
        )
        for extra in cases:
            with self.subTest(extra=extra):
                status, _, page = await self.json_get("/api/v1/sessions?inventoryState=lazy", session)
                self.assertEqual(status, 200, page)
                payload = _action_payload(page["items"][0]["actions"]["configure"], **extra)
                status, _, result = await self.json_post("/api/v1/sessions/configure", session, payload)
                self.assertEqual(status, 400, result)
        self.assertEqual(self.management.calls, [])

    async def test_session_configuration_maps_each_revision_conflict_to_409(self):
        self.runner.open_admission()
        session = await self.login()
        for error in (BindingSettingsRevisionConflict, BindingContextRevisionConflict, BindingFeedbackRevisionConflict):
            with self.subTest(error=error.__name__):
                status, _, page = await self.json_get("/api/v1/sessions?inventoryState=lazy", session)
                self.assertEqual(status, 200, page)
                payload = _action_payload(page["items"][0]["actions"]["configure"], sessionSettings=SessionSettings().to_dict())
                with patch.object(self.management, "configure_exact_binding", AsyncMock(side_effect=error("stale"))) as configure:
                    status, _, result = await self.json_post("/api/v1/sessions/configure", session, payload)
                    self.assertEqual(status, 409, result)
                    self.assertEqual(result["code"], "stale_or_conflict")
                    status, _, _ = await self.json_post("/api/v1/sessions/configure", session, payload)
                    self.assertEqual(status, 409)
                    configure.assert_awaited_once()

    async def test_session_options_share_model_catalog_and_require_authentication(self):
        self.runner.open_admission()
        options = {"models": [], "model_catalog_error": "暂不可用"}
        manage = AsyncMock(return_value={**options, "session_settings": SessionSettings().to_dict()})
        self.management.defaults = SimpleNamespace(manage=manage)
        status, _, _ = await self.request("GET", "/api/v1/sessions/options")
        self.assertEqual(status, 401)
        manage.assert_not_awaited()
        session = await self.login()
        status, _, result = await self.json_get("/api/v1/sessions/options", session)
        self.assertEqual(status, 200, result)
        self.assertEqual({key: result[key] for key in options}, options)
        self.assertNotIn("session_settings", result)
        manage.assert_awaited_once_with({"mode": "options"})
        status, _, _ = await self.json_get("/api/v1/sessions/options?chat_id=forged", session)
        self.assertEqual(status, 400)
        self.management.defaults = None
        status, _, _ = await self.json_get("/api/v1/sessions/options", session)
        self.assertEqual(status, 503)

    async def test_all_session_and_side_mutation_routes_are_reachable(self) -> None:
        self.runner.open_admission()
        session = await self.login()

        async def fresh_items():
            items = {}
            for state in ("active", "archived", "lazy"):
                status, _headers, page = await self.json_get(
                    f"/api/v1/sessions?inventoryState={state}", session,
                )
                self.assertEqual(status, 200)
                items.update((item["bindingId"], item) for item in page["items"])
            return items

        cases = [
            ("binding-lazy", "activate", "/api/v1/sessions/activate", {}),
            ("binding-lazy", "configure", "/api/v1/sessions/configure", {"sessionSettings": SessionSettings().to_dict()}),
            ("binding-lazy", "deleteLazy", "/api/v1/sessions/delete-lazy", {}),
            ("binding-native", "rename", "/api/v1/sessions/rename", {"name": "Renamed"}),
            ("binding-native", "archive", "/api/v1/sessions/archive", {}),
            ("binding-native", "deleteMaterialized", "/api/v1/sessions/delete-materialized", {}),
            ("binding-native", "stop", "/api/v1/sessions/stop", {}),
            ("binding-native", "release", "/api/v1/sessions/release", {}),
            ("binding-archived", "unarchive", "/api/v1/sessions/unarchive", {"actionKind": "sessions.unarchive"}),
            ("binding-archived", "unarchiveCurrent", "/api/v1/sessions/unarchive", {"actionKind": "sessions.unarchive-current"}),
            ("binding-archived", "deleteMaterialized", "/api/v1/sessions/delete-materialized", {}),
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
        delete_calls = [
            values
            for name, values in self.management.calls
            if name == "delete-materialized"
        ]
        self.assertEqual(
            [item["expected_native_thread_id"] for item in delete_calls],
            ["native-active", "native-archived"],
        )
        self.assertTrue(
            all(item["target"].expected_active_binding_id is None for item in delete_calls)
        )

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

    async def test_session_creation_is_unavailable_in_admin(self):
        self.runner.open_admission()
        session = await self.login()
        status, _, page = await self.json_get("/api/v1/sessions", session)
        self.assertEqual(status, 200, page)
        for item in page["items"]:
            with self.subTest(binding=item["bindingId"]):
                self.assertNotIn("createLazy", item["actions"])
        self.management.calls.clear()
        status, _, result = await self.json_post(
            "/api/v1/sessions/create-lazy", session,
            {"projectAlias": "test", "projectRevision": 1, "activate": False,
             "turnSettings": None},
        )
        self.assertEqual(status, 404, result)
        self.assertEqual(self.management.calls, [])

    async def test_side_query_defaults_multiselect_cursor_and_readable_projection(self):
        self.runner.open_admission()
        session = await self.login()
        status, _headers, page = await self.json_get("/api/v1/side-topics", session)
        self.assertEqual(status, 200)
        call = self.management.query_side_topic_calls[-1]
        self.assertEqual(call["limit"], 20)
        self.assertEqual((call["query"].sort_by, call["query"].sort_direction), ("updated_at", "desc"))
        self.assertEqual(call["query"].states, (SideTopicState.CREATING, SideTopicState.OPEN))
        row = page["items"][0]
        self.assertEqual(row["parentSession"], {
            "bindingId": self.management.native.id, "title": "Source session", "available": True,
        })
        self.assertEqual(row["sessionState"], "open")
        self.assertIn("ou_partner", row["chatOpenUrl"])
        self.assertIn("omt_topic", row["topicOpenUrl"])
        self.assertNotIn("stop", row["actions"])
        original_query = self.management.query_side_topics

        async def with_cursor(**values):
            result = await original_query(**values)
            return replace(result, next_cursor=SideTopicCursor("2030", "side-1", "2031"))

        with patch.object(self.management, "query_side_topics", side_effect=with_cursor):
            status, _, page = await self.json_get(
                "/api/v1/side-topics?state=open&state=creating&state=open&project=b&project=a&pageSize=100",
                session,
            )
            self.assertEqual(status, 200)
            cursor = page["nextCursor"]
            status, _, _ = await self.json_get(
                "/api/v1/side-topics?state=creating&state=open&project=a&project=b&pageSize=100&cursor=" + cursor,
                session,
            )
            self.assertEqual(status, 200)
            status, _, error = await self.json_get(
                "/api/v1/side-topics?state=open&project=a&project=b&pageSize=100&cursor=" + cursor,
                session,
            )
            self.assertEqual(status, 400)
            self.assertEqual(error["code"], "invalid_cursor")
        status, _, _ = await self.json_get("/api/v1/side-topics?state=all", session)
        self.assertEqual(status, 200)
        self.assertIsNone(self.management.query_side_topic_calls[-1]["query"].states)
        for query in ("pageSize=25", "state=all&state=open", "state=closing"):
            status, _, _ = await self.json_get("/api/v1/side-topics?" + query, session)
            self.assertEqual(status, 400)

    async def test_side_stop_is_exact_one_shot_and_stale_state_is_rejected(self):
        self.runner.open_admission()
        session = await self.login()
        self.management.side_runtime = replace(
            self.management.side_runtime, turn_id="physical-turn", turn_state=ActiveState.RUNNING,
            revision=7,
        )
        _, _, page = await self.json_get("/api/v1/side-topics", session)
        stop = page["items"][0]["actions"]["stop"]
        status, _, payload = await self.json_post(
            "/api/v1/side-topics/stop", session, _action_payload(stop),
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["disposition"], StopDisposition.REQUESTED.value)
        values = self.management.calls[-1][1]
        self.assertEqual(values["target"].side_id, "side-1")
        self.assertEqual(values["target"].topic_id, "omt_topic")
        self.assertEqual(values["runtime_precondition"].activity_revision, 7)
        self.assertEqual(values["runtime_precondition"].physical_turn_id, "physical-turn")
        calls = tuple(self.management.calls)
        status, _, _ = await self.json_post(
            "/api/v1/side-topics/stop", session, _action_payload(stop),
        )
        self.assertEqual(status, 409)
        self.assertEqual(tuple(self.management.calls), calls)
        _, _, page = await self.json_get("/api/v1/side-topics", session)
        with patch.object(self.management, "stop_side", side_effect=SideSessionConflict("changed")):
            status, _, _ = await self.json_post(
                "/api/v1/side-topics/stop", session,
                _action_payload(page["items"][0]["actions"]["stop"]),
            )
            self.assertEqual(status, 409)
        self.management.side_runtime = replace(self.management.side_runtime, state=SideSessionState.CLOSING)
        _, _, page = await self.json_get("/api/v1/side-topics", session)
        self.assertEqual(page["items"][0]["sessionState"], "closing")
        self.assertNotIn("stop", page["items"][0]["actions"])
        self.assertIn("close", page["items"][0]["actions"])

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

        from netizen_cli.admin.queries import _encode_binding_cursor, _fingerprint
        from netizen_cli.bindings import BindingCursor

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

    async def test_session_cursor_uses_normalized_filters_and_all_time_ranges(self) -> None:
        self.runner.open_admission()
        session = await self.login()
        native_cursor = SessionNativeCursor(None, 37)
        with patch.object(self.management, "query_sessions", return_value=SessionInventoryPage((), native_cursor)) as query:
            status, _, page = await self.json_get(
                "/api/v1/sessions?project=beta&project=alpha&project=beta"
                "&scopeKind=topic&scopeKind=direct&current=true&current=false&inventoryState=active",
                session,
            )
            self.assertEqual(status, 200, page)
            filters = query.call_args.kwargs["query"]
            self.assertEqual(filters.local.project_aliases, ("alpha", "beta"))
            self.assertEqual(filters.local.scope_kinds, (ScopeKind.DIRECT, ScopeKind.TOPIC))
            self.assertIsNone(filters.local.current)
            self.assertIs(filters.inventory_state, SessionInventoryState.ACTIVE)
            cursor = page["nextCursor"]
            equivalent = "/api/v1/sessions?project=alpha&project=beta&scopeKind=direct&scopeKind=topic"
            status, _, page = await self.json_get(f"{equivalent}&cursor={cursor}", session)
            self.assertEqual(status, 200, page)
            self.assertEqual(query.call_args.kwargs["cursor"], native_cursor)
            for change in (
                "&project=gamma", "&scopeKind=group", "&current=true",
                "&inventoryState=lazy", "&inventoryState=archived", "&pageSize=50",
                "&sortBy=createdAt", "&sortDirection=asc", "&identity=another",
                "&createdFrom=2030-01-01T00:00Z", "&createdBefore=2030-01-01T00:00Z",
                "&updatedFrom=2030-01-01T00:00Z", "&updatedBefore=2030-01-01T00:00Z",
            ):
                with self.subTest(change=change):
                    status, _, error = await self.json_get(f"{equivalent}{change}&cursor={cursor}", session)
                    self.assertEqual(status, 400, error)
                    self.assertEqual(error["code"], "invalid_cursor")

            # Explicitly selecting all other filter choices is equivalent to omitting them.
            status, _, page = await self.json_get(
                "/api/v1/sessions?scopeKind=direct&scopeKind=group&scopeKind=topic&current=false&current=true", session,
            )
            self.assertEqual(status, 200, page)
            status, _, page = await self.json_get(f"/api/v1/sessions?cursor={page['nextCursor']}", session)
            self.assertEqual(status, 200, page)

    async def test_session_multiselect_query_count_remains_bounded(self) -> None:
        self.runner.open_admission()
        session = await self.login()
        projects = tuple(f"project-{index:03d}" for index in range(40))
        query = urlencode([("project", alias) for alias in projects])
        status, _, page = await self.json_get(f"/api/v1/sessions?{query}", session)
        self.assertEqual(status, 200, page)
        self.assertEqual(
            self.management.query_session_calls[-1]["query"].local.project_aliases,
            projects,
        )
        with patch.object(self.management, "query_sessions") as sessions:
            overflow = urlencode([("project", "x")] * 257)
            status, _, error = await self.json_get(
                f"/api/v1/sessions?{overflow}", session
            )
            self.assertEqual(status, 400, error)
            sessions.assert_not_awaited()

    async def test_session_time_order_parameters_and_cursors_are_bound_to_each_order(self) -> None:
        self.runner.open_admission()
        session = await self.login()
        for route, method in (("sessions", "query_sessions"), ("side-topics", "query_side_topics")):
            for key in ("createdAt", "updatedAt"):
                for direction in ("asc", "desc"):
                    with self.subTest(route=route, key=key, direction=direction):
                        if route == "sessions":
                            native_cursor = SessionNativeCursor("opaque-native-page-token")
                            result = SessionInventoryPage((), native_cursor)
                        else:
                            native_cursor = SideTopicCursor("2030", "side-last", "2031" if key == "updatedAt" else None)
                            result = SideTopicInventoryPage((), native_cursor)
                        with patch.object(self.management, method, return_value=result) as call:
                            url = f"/api/v1/{route}?sortBy={key}&sortDirection={direction}"
                            status, _, page = await self.json_get(url, session)
                            self.assertEqual(status, 200, page)
                            query = call.call_args.kwargs["query"]
                            self.assertEqual(query.sort_by, "created_at" if key == "createdAt" else "updated_at")
                            self.assertEqual(query.sort_direction, direction)
                            cursor = page["nextCursor"]
                            status, _, page = await self.json_get(url + "&cursor=" + cursor, session)
                            self.assertEqual(status, 200, page)
                            self.assertEqual(call.call_args.kwargs["cursor"], native_cursor)
                            changed = "asc" if direction == "desc" else "desc"
                            status, _, error = await self.json_get(f"/api/v1/{route}?sortBy={key}&sortDirection={changed}&cursor={cursor}", session)
                            self.assertEqual(status, 400, error)
                            self.assertEqual(error["code"], "invalid_cursor")
            with patch.object(self.management, method) as call:
                for query in ("sortBy=activatedAt", "sortDirection=DESC", "sortBy=createdAt&sortBy=updatedAt", "sortDirection=asc&sortDirection=desc", "sortBy="):
                    status, _, error = await self.json_get(f"/api/v1/{route}?{query}", session)
                    self.assertEqual(status, 400, error)
                call.assert_not_awaited()

    async def test_session_default_order_is_updated_desc_and_accepts_explicit_equivalent_cursor(self) -> None:
        self.runner.open_admission()
        session = await self.login()
        with patch.object(self.management, "query_sessions", return_value=SessionInventoryPage((), SessionNativeCursor("opaque-native-page-token"))) as call:
            status, _, page = await self.json_get("/api/v1/sessions", session)
            self.assertEqual(status, 200, page)
            query = call.call_args.kwargs["query"]
            self.assertEqual((query.sort_by, query.sort_direction), ("updated_at", "desc"))
            self.assertIs(query.inventory_state, SessionInventoryState.ACTIVE)
            self.assertIsNone(query.created_from)
            self.assertIsNone(query.created_before)
            self.assertIsNone(query.updated_from)
            self.assertIsNone(query.updated_before)
            status, _, error = await self.json_get("/api/v1/sessions?sortBy=updatedAt&sortDirection=desc&cursor=" + page["nextCursor"], session)
            self.assertEqual(status, 200, error)

    async def test_session_state_rejects_multi_state_and_legacy_all_before_query(self) -> None:
        self.runner.open_admission()
        session = await self.login()
        with patch.object(self.management, "query_sessions") as call:
            for query in (
                "inventoryState=all", "inventoryState=unknown", "inventoryState=missing",
                "inventoryState=active&inventoryState=archived",
                "inventoryState=active&inventoryState=active", "inventoryState=",
            ):
                with self.subTest(query=query):
                    status, _, error = await self.json_get(f"/api/v1/sessions?{query}", session)
                    self.assertEqual(status, 400, error)
            call.assert_not_awaited()

    async def test_lazy_cursor_remains_local_and_cannot_be_used_for_native_catalog(self) -> None:
        self.runner.open_admission()
        session = await self.login()
        local_cursor = BindingCursor("2030-01-01T00:00:00+00:00", "binding-last")
        with patch.object(self.management, "query_sessions", return_value=SessionInventoryPage((), local_cursor)) as call:
            status, _, page = await self.json_get("/api/v1/sessions?inventoryState=lazy", session)
            self.assertEqual(status, 200, page)
            cursor = page["nextCursor"]
            status, _, page = await self.json_get(f"/api/v1/sessions?inventoryState=lazy&cursor={cursor}", session)
            self.assertEqual(status, 200, page)
            self.assertEqual(call.call_args.kwargs["cursor"], local_cursor)
            status, _, error = await self.json_get(f"/api/v1/sessions?inventoryState=active&cursor={cursor}", session)
            self.assertEqual(status, 400, error)
            self.assertEqual(error["code"], "invalid_cursor")

    async def test_native_time_ranges_are_normalized_and_bound_to_cursor(self) -> None:
        self.runner.open_admission()
        session = await self.login()
        cursor = SessionNativeCursor("opaque-native-page-token")
        with patch.object(self.management, "query_sessions", return_value=SessionInventoryPage((), cursor)) as call:
            query_values = {
                "createdFrom": "2030-01-01T08:00+08:00",
                "createdBefore": "2030-02-01T08:00+08:00",
                "updatedFrom": "2030-03-01T08:00+08:00",
                "updatedBefore": "2030-04-01T08:00+08:00",
            }
            status, _, page = await self.json_get("/api/v1/sessions?" + urlencode(query_values), session)
            self.assertEqual(status, 200, page)
            query = call.call_args.kwargs["query"]
            for name, field in (("createdFrom", "created_from"), ("createdBefore", "created_before"), ("updatedFrom", "updated_from"), ("updatedBefore", "updated_before")):
                self.assertEqual(getattr(query, field), datetime.fromisoformat(query_values[name]).timestamp())
            self.assertIsNone(query.local.created_from)
            self.assertIsNone(query.local.created_before)
            equivalent = {key: value.replace("T08:00+08:00", "T00:00Z") for key, value in query_values.items()}
            status, _, response = await self.json_get("/api/v1/sessions?" + urlencode({**equivalent, "cursor": page["nextCursor"]}), session)
            self.assertEqual(status, 200, response)
            self.assertEqual(call.call_args.kwargs["cursor"], cursor)
            status, _, error = await self.json_get("/api/v1/sessions?" + urlencode({**equivalent, "updatedBefore": "2030-04-02T00:00Z", "cursor": page["nextCursor"]}), session)
            self.assertEqual(status, 400, error)
            self.assertEqual(error["code"], "invalid_cursor")

    async def test_project_options_are_authenticated_paginated_and_issue_no_actions(self) -> None:
        self.runner.open_admission()
        status, _, _ = await self.request("GET", "/api/v1/projects/options")
        self.assertEqual(status, 401)
        session = await self.login()
        disabled = replace(self.management.project_record, alias="disabled", enabled=False)
        aggregate = ProjectAggregate(disabled, 0, 0, 0, None)
        before = self.runner.application._auth.state_counts().actions
        with patch.object(
            self.management,
            "query_project_options",
            return_value=ProjectAggregatePage((aggregate,), "disabled"),
        ) as query, patch.object(self.management, "query_projects") as inventory:
            status, _, page = await self.json_get(
                "/api/v1/projects/options?pageSize=1", session
            )
            self.assertEqual(status, 200, page)
            self.assertEqual(page["items"], [{"alias": "disabled", "enabled": False}])
            self.assertNotIn("actions", page)
            self.assertEqual(query.call_args.kwargs["limit"], 1)
            cursor = page["nextCursor"]
            status, _, page = await self.json_get(
                f"/api/v1/projects/options?pageSize=1&cursor={cursor}", session
            )
            self.assertEqual(status, 200, page)
            self.assertEqual(query.call_args.kwargs["cursor"], "disabled")
            inventory.assert_not_awaited()
            for target in (
                f"/api/v1/projects/options?pageSize=2&cursor={cursor}",
                f"/api/v1/projects?pageSize=1&cursor={cursor}",
            ):
                status, _, error = await self.json_get(target, session)
                self.assertEqual(status, 400, error)
                self.assertEqual(error["code"], "invalid_cursor")
        self.assertEqual(before, self.runner.application._auth.state_counts().actions)

    async def test_session_topic_link_uses_scope_identity_independent_of_chat_mode(self) -> None:
        self.runner.open_admission()
        session = await self.login()
        for kind, topic_id, mode in (
            (ScopeKind.TOPIC, "omt_topic", "group"),
            (ScopeKind.TOPIC, "omt_topic", "topic"),
            (ScopeKind.TOPIC, "omt_topic", "p2p"),
            (ScopeKind.TOPIC, "omt_topic", None),
            (ScopeKind.GROUP, None, "topic"),
            (ScopeKind.TOPIC, None, "group"),
        ):
            with self.subTest(kind=kind, topic_id=topic_id, mode=mode):
                scope = replace(
                    self.management.scope, kind=kind, topic_id=topic_id,
                    scope_key=f"scope-{kind.value}-{topic_id}",
                )
                binding = replace(self.management.lazy, scope_key=scope.scope_key)
                chat = ChatLabel(
                    scope.chat_id, "Demo Chat", mode, resolved=mode is not None,
                )
                self.management.session_items_override = (
                    SessionInventoryItem(
                        BindingInventoryRecord(binding, scope), None, chat,
                    ),
                )
                status, _, page = await self.json_get("/api/v1/sessions", session)
                self.assertEqual(status, 200, page)
                item, = page["items"]
                self.assertEqual(
                    item["chatOpenUrl"],
                    "https://applink.feishu.cn/client/chat/open?openChatId=oc_chat",
                )
                if topic_id is None:
                    self.assertIsNone(item["topicOpenUrl"])
                else:
                    url = urlsplit(item["topicOpenUrl"])
                    self.assertEqual(url.path, "/client/thread/open")
                    query = parse_qs(url.query)
                    self.assertEqual(query["open_chat_id"], [scope.chat_id])
                    self.assertEqual(query["open_thread_id"], [topic_id])
                    self.assertEqual(query["thread_position"], ["-1"])

    async def test_sessions_inventory_limit_explains_how_to_narrow_query(self) -> None:
        self.runner.open_admission()
        session = await self.login()
        with patch.object(
            self.management, "query_sessions", AsyncMock(side_effect=BindingQueryLimitExceeded("too many")),
        ):
            status, _, result = await self.json_get("/api/v1/sessions", session)
        self.assertEqual(status, 400)
        self.assertEqual(result["code"], "query_limit")
        self.assertIn("缩小筛选范围", result["message"])

    async def test_sessions_catalog_failure_is_an_explicit_read_error(self) -> None:
        self.runner.open_admission()
        session = await self.login()
        with patch.object(self.management, "query_sessions", side_effect=ThreadCatalogError("invalid page")):
            status, _, result = await self.json_get("/api/v1/sessions", session)
        self.assertEqual(status, 503)
        self.assertEqual(result["code"], "catalog_unavailable")
        self.assertIn("目录读取失败", result["message"])
        self.assertNotIn("items", result)

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
        self.assertEqual(
            call["query"].inventory_state,
            SessionInventoryState.ACTIVE,
        )
        self.assertGreaterEqual(call["deadline"], started + 10.0)
        self.assertLessEqual(call["deadline"], finished + 10.0)
        self.assertEqual([item["bindingId"] for item in page["items"]], ["binding-native"])
        by_id = {item["bindingId"]: item for item in page["items"]}
        for inventory_state in ("archived", "lazy"):
            status, _, selected = await self.json_get(f"/api/v1/sessions?inventoryState={inventory_state}", session)
            self.assertEqual(status, 200, selected)
            by_id.update((item["bindingId"], item) for item in selected["items"])
        self.assertEqual(by_id["binding-native"]["sessionType"], "message")
        self.assertEqual(by_id["binding-native"]["chatMode"], "p2p")
        self.assertTrue(by_id["binding-native"]["chatLabelResolved"])
        self.assertEqual(
            by_id["binding-native"]["chatOpenUrl"],
            "https://applink.feishu.cn/client/chat/open?openId=ou_partner",
        )
        self.assertIsNone(by_id["binding-native"]["topicOpenUrl"])
        self.assertEqual(by_id["binding-native"]["pointerState"], "current")
        self.assertEqual(by_id["binding-native"]["catalogState"], "active")
        self.assertEqual(by_id["binding-lazy"]["pointerState"], "inactive")
        self.assertEqual(by_id["binding-lazy"]["catalogState"], "lazy")
        self.assertEqual(by_id["binding-archived"]["pointerState"], "inactive")
        self.assertEqual(by_id["binding-archived"]["catalogState"], "archived")
        self.assertEqual(by_id["binding-native"]["updatedAt"], 1_730_831_111)
        self.assertEqual(by_id["binding-archived"]["updatedAt"], 1_730_750_000)
        self.assertIsNone(by_id["binding-lazy"]["updatedAt"])
        self.assertEqual(by_id["binding-native"]["createdAt"], 1_730_000_000)
        self.assertEqual(by_id["binding-archived"]["createdAt"], 1_720_000_000)
        self.assertIsNone(by_id["binding-lazy"]["createdAt"])
        self.assertEqual(by_id["binding-lazy"]["registeredAt"], self.management.lazy.created_at)
        for item in by_id.values():
            self.assertNotIn("sessionState", item)
            self.assertNotIn("nativeState", item)
            self.assertNotIn("current", item)
        self.assertEqual(by_id["binding-native"]["runtime"]["primaryStatus"], "running")
        self.assertEqual(
            by_id["binding-native"]["runtime"]["subscriptionState"],
            "subscribed",
        )
        self.assertIn("deleteMaterialized", by_id["binding-native"]["actions"])
        self.assertIn("deleteMaterialized", by_id["binding-archived"]["actions"])
        self.assertNotIn("activate", by_id["binding-archived"]["actions"])
        self.assertIn("unarchiveCurrent", by_id["binding-archived"]["actions"])
        self.assertNotIn("deleteMaterialized", by_id["binding-lazy"]["actions"])
        self.assertNotIn("archive", by_id["binding-lazy"]["actions"])
        self.assertNotIn("archive", by_id["binding-archived"]["actions"])

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
        self.assertEqual(
            self.management.query_session_calls[-1]["query"].inventory_state,
            SessionInventoryState.LAZY,
        )
        status, _headers, _page = await self.json_get(
            "/api/v1/sessions?inventoryState=active",
            session,
        )
        self.assertEqual(status, 200)
        self.assertIs(
            self.management.query_session_calls[-1]["query"].inventory_state, SessionInventoryState.ACTIVE,
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

    async def test_unknown_session_state_retains_exact_lifecycle_and_stop_actions(self) -> None:
        self.runner.open_admission()
        session = await self.login()
        unknown = replace(
            self.management.native,
            id="binding-unknown",
            native_thread_id="native-unlisted",
            active=False,
        )
        self.management.runtime_by_id[unknown.id] = self.management._runtime(
            unknown.id, 5, running=True,
        )
        label = ChatLabelResolver.fallback("oc_chat")
        for metadata in (
            None,
            NativeThreadMetadata("native-unlisted", "Read title", "preview"),
            NativeThreadMetadata("native-unlisted", "Read title", "preview", updated_at=1_730_831_111),
        ):
            with self.subTest(metadata=metadata):
                self.management.session_items_override = (
                    SessionInventoryItem(
                        BindingInventoryRecord(unknown, self.management.scope),
                        NativeThreadView(None, metadata),
                        label,
                    ),
                    SessionInventoryItem(
                        BindingInventoryRecord(self.management.lazy, self.management.scope),
                        None,
                        label,
                    ),
                )
                status, _, page = await self.json_get(
                    "/api/v1/sessions?inventoryState=active", session,
                )
                self.assertEqual(status, 200, page)
                self.assertEqual(
                    self.management.query_session_calls[-1]["query"].inventory_state,
                    SessionInventoryState.ACTIVE,
                )
                item = page["items"][0]
                self.assertEqual(item["catalogState"], "unknown")
                self.assertEqual(item["nativeThreadId"], "native-unlisted")
                self.assertEqual(item["pointerState"], "inactive")
                self.assertEqual(item["nativeTitle"], metadata.name if metadata else None)
                self.assertEqual(item["updatedAt"], metadata.updated_at if metadata else None)
                self.assertEqual(item["runtime"]["primaryStatus"], "running")
                self.assertEqual(
                    set(item["actions"]),
                    {"configure", "stop", "activate", "archive", "deleteMaterialized"},
                )
                status, _, result = await self.json_post(
                    "/api/v1/sessions/activate", session,
                    _action_payload(item["actions"]["activate"]),
                )
                self.assertEqual(status, 200, result)
                name, values = self.management.calls[-1]
                self.assertEqual(name, "activate")
                self.assertEqual(values["target"].binding_id, "binding-unknown")
                self.assertEqual(values["target"].scope_key, unknown.scope_key)
                self.assertEqual(page["items"][1]["catalogState"], "lazy")
                self.assertIn("deleteLazy", page["items"][1]["actions"])

    async def test_unlisted_current_fork_can_use_exact_archive_and_delete_actions(self) -> None:
        self.runner.open_admission()
        session = await self.login()
        scope = replace(
            self.management.scope,
            scope_key="cli_test:topic:oc_chat:omt_fork",
            kind=ScopeKind.TOPIC, topic_id="omt_fork", active_binding_id="binding-fork",
        )
        fork = replace(
            self.management.native, id="binding-fork", scope_key=scope.scope_key,
            native_thread_id="native-fork",
        )
        self.management.runtime_by_id[fork.id] = self.management._runtime(fork.id, 1)
        self.management.session_items_override = (
            SessionInventoryItem(
                BindingInventoryRecord(fork, scope),
                NativeThreadView(None, NativeThreadMetadata(
                    "native-fork", "Unread fork", "Inherited history", updated_at=1_792_074_779,
                )),
                ChatLabelResolver.fallback(scope.chat_id),
            ),
        )
        for action, operation in (("archive", "archive"), ("deleteMaterialized", "delete-materialized")):
            with self.subTest(action=action):
                status, _, page = await self.json_get("/api/v1/sessions", session)
                self.assertEqual(status, 200, page)
                item = page["items"][0]
                self.assertEqual(item["catalogState"], "unknown")
                self.assertEqual(item["pointerState"], "current")
                self.assertEqual(item["nativeTitle"], "Unread fork")
                self.assertEqual(item["updatedAt"], 1_792_074_779)
                self.assertNotIn("activate", item["actions"])
                status, _, result = await self.json_post(
                    f"/api/v1/sessions/{operation}", session,
                    _action_payload(item["actions"][action]),
                )
                self.assertEqual(status, 200, result)
                name, values = self.management.calls[-1]
                self.assertEqual(name, operation)
                self.assertEqual(values["target"].binding_id, fork.id)
                self.assertEqual(values["target"].scope_key, scope.scope_key)
                self.assertIsNone(values["target"].expected_active_binding_id)
                if operation == "delete-materialized":
                    self.assertEqual(values["expected_native_thread_id"], fork.native_thread_id)

    async def test_projects_api_has_only_local_counts_and_preserves_actions(self) -> None:
        self.runner.open_admission()
        session = await self.login()
        aggregate = ProjectAggregate(self.management.project_record, 3, 1, 2, "2030-01-02")
        with patch.object(
            self.management, "query_projects",
            return_value=ProjectInventoryPage((ProjectInventoryItem(aggregate),), None),
        ):
            status, _, page = await self.json_get("/api/v1/projects", session)
        self.assertEqual(status, 200, page)
        item = page["items"][0]
        self.assertEqual(item["bindingCount"], 3)
        self.assertEqual(item["lazyBindingCount"], 1)
        self.assertEqual(item["materializedBindingCount"], 2)
        self.assertNotIn("archivedBindingCount", item)
        self.assertNotIn("unconfirmedBindingCount", item)
        self.assertIn("setEnabled", item["actions"])
        self.assertIn("previewDelete", item["actions"])

    async def test_materialized_delete_action_requires_capability_not_catalog_membership(self) -> None:
        self.runner.open_admission()
        session = await self.login()

        self.management.native_delete_available = False
        status, _headers, page = await self.json_get(
            "/api/v1/sessions?inventoryState=active",
            session,
        )

        self.assertEqual(status, 200)
        self.assertTrue(
            all(
                "deleteMaterialized" not in item["actions"]
                for item in page["items"]
            )
        )

        self.management.native_delete_available = True
        missing = replace(
            self.management.native,
            id="missing-binding",
            native_thread_id="native-missing",
            active=False,
        )
        self.management.runtime_by_id[missing.id] = self.management._runtime(
            missing.id,
            11,
        )
        self.management.session_items_override = (
            SessionInventoryItem(
                BindingInventoryRecord(missing, self.management.scope),
                NativeThreadView(NativeThreadCatalogState.MISSING, None),
                ChatLabel("oc_chat", "Demo Chat", "p2p", True, "ou_partner"),
            ),
        )
        status, _headers, page = await self.json_get(
            "/api/v1/sessions?inventoryState=active",
            session,
        )

        self.assertEqual(status, 200)
        self.assertIn("deleteMaterialized", page["items"][0]["actions"])
        self.assertIn("archive", page["items"][0]["actions"])

    async def test_materialized_delete_action_is_one_shot(self) -> None:
        self.runner.open_admission()
        session = await self.login()
        status, _headers, page = await self.json_get(
            "/api/v1/sessions?inventoryState=active",
            session,
        )
        self.assertEqual(status, 200)
        item = next(
            value for value in page["items"] if value["bindingId"] == "binding-native"
        )
        action = item["actions"]["deleteMaterialized"]
        status, _headers, payload = await self.json_post(
            "/api/v1/sessions/delete-materialized",
            session,
            _action_payload(action),
        )

        self.assertEqual(status, 200, payload)
        calls_after_delete = tuple(self.management.calls)
        self.assertEqual(calls_after_delete[-1][0], "delete-materialized")

        status, _headers, replay = await self.json_post(
            "/api/v1/sessions/delete-materialized",
            session,
            _action_payload(action),
        )
        self.assertEqual(status, 409)
        self.assertEqual(replay["code"], "stale_or_consumed")
        self.assertEqual(tuple(self.management.calls), calls_after_delete)

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
        query = self.management.query_session_calls[-1]["query"]
        self.assertEqual(query.created_from, datetime.fromisoformat("2030-01-02T00:30:00+00:00").timestamp())
        self.assertEqual(query.created_before, datetime.fromisoformat("2030-01-03T00:45:00+00:00").timestamp())
        self.assertIsNone(query.local.created_from)
        self.assertIsNone(query.local.created_before)

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
            "/api/v1/sessions?inventoryState=active",
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
            "/api/v1/sessions?inventoryState=active",
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

    async def test_archived_session_runtime_is_not_applicable_but_keeps_local_activity(
        self,
    ) -> None:
        self.runner.open_admission()
        session = await self.login()

        status, _headers, page = await self.json_get(
            "/api/v1/sessions?inventoryState=archived", session,
        )

        self.assertEqual(status, 200)
        archived = next(
            item for item in page["items"] if item["bindingId"] == "binding-archived"
        )
        self.assertEqual(archived["catalogState"], "archived")
        self.assertIsNone(archived["runtime"]["primaryStatus"])
        self.assertEqual(archived["runtime"]["primaryStatusResolution"], "archived")
        self.assertNotIn("stop", archived["actions"])
        self.assertNotIn("release", archived["actions"])
        self.assertIn("unarchive", archived["actions"])
        self.assertIn("deleteMaterialized", archived["actions"])

        self.management.runtime_by_id["binding-archived"] = replace(
            self.management.runtime_by_id["binding-archived"],
            lifecycle=ThreadLifecycleSnapshot(
                "binding-archived", "native-archived", ThreadLifecycleState.UNKNOWN,
            ),
        )
        status, _headers, page = await self.json_get(
            "/api/v1/sessions?inventoryState=archived", session,
        )

        self.assertEqual(status, 200)
        archived = next(
            item for item in page["items"] if item["bindingId"] == "binding-archived"
        )
        self.assertEqual(archived["runtime"]["primaryStatus"], "lifecycle-unknown")
        self.assertEqual(archived["runtime"]["primaryStatusResolution"], "local")

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
            patch("netizen_cli.admin.web.socket.gethostname", return_value="host.test"),
            patch("netizen_cli.admin.web.socket.getfqdn", return_value="host.example"),
            patch("netizen_cli.admin.web.socket.getaddrinfo", return_value=[]),
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
    def test_static_ui_has_management_pages_external_assets_and_safe_text_sinks(self) -> None:
        root = Path(__file__).resolve().parents[2] / "netizen_cli/admin/static"
        html = (root / "index.html").read_text(encoding="utf-8")
        javascript = (root / "admin.js").read_text(encoding="utf-8")
        parser = _AdminAssetParser()
        parser.feed(html)

        self.assertEqual(parser.scripts, ["/static/chat-picker.js", "/static/identity.js",
                                          "/static/session-access.js", "/static/worktrees.js", "/static/admin.js"])
        self.assertEqual(parser.stylesheets, ["/static/admin.css", "/static/chat-picker.css"])
        self.assertFalse(parser.inline_script_text.strip())
        self.assertTrue({"projects", "sessions", "sessions-ordinary", "sessions-temporary", "updates"} <= parser.ids)
        self.assertNotIn("side-topics", parser.ids)
        self.assertIn("service-restart", parser.ids)
        self.assertNotIn("update-install", parser.ids)
        self.assertNotIn("update-check", parser.ids)
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
        self.assertIn("runtime.primaryStatus", javascript)
        self.assertIn("runtime.subscriptionState", javascript)
        self.assertIn("session.pointerState", javascript)
        self.assertIn("session.catalogState", javascript)
        self.assertNotIn("session.nativeState", javascript)
        self.assertIn("result?.message", javascript)
        self.assertNotIn("if (runtime.turn)", javascript)
        self.assertNotIn("if (runtime.goal)", javascript)
        self.assertIn("sessionLocationCell", javascript)
        self.assertIn(
            'confirmMaterializedDelete(session) && mutate("/api/v1/sessions/delete-materialized"',
            javascript,
        )
        self.assertIn(
            'window.confirm("永久删除这个 Lazy Binding？") && mutate("/api/v1/sessions/delete-lazy"',
            javascript,
        )
        self.assertIn("spawned descendants", javascript)
        self.assertNotIn("delete-session-confirmation", html)
        self.assertIn('link.rel = "noopener noreferrer"', javascript)
        self.assertIn("await refresh(state.tab)", javascript)
        self.assertIn('state.tab === "sessions"', javascript)
        self.assertIn('state.sessionTab === "temporary"', javascript)

    def test_time_range_filter_is_shared_accessible_and_uses_utc_bounds(self) -> None:
        root = Path(__file__).resolve().parents[2] / "netizen_cli/admin/static"
        html = (root / "index.html").read_text(encoding="utf-8")
        javascript = (root / "admin.js").read_text(encoding="utf-8")
        stylesheet = (root / "admin.css").read_text(encoding="utf-8")

        self.assertEqual(html.count('class="time-range-filter" data-time-range'), 3)
        self.assertIn('id="time-range-template"', html)
        self.assertIn('name="createdFrom" data-time-range-from', html)
        self.assertIn('name="createdBefore" data-time-range-before', html)
        time_range_template = html.split('<template id="time-range-template">', 1)[1].split("</template>", 1)[0]
        self.assertEqual(time_range_template.count('type="datetime-local"'), 2)
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
            "last-3-months",
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
            Path(__file__).resolve().parents[2] / "netizen_cli/admin/static/admin.js"
        ).read_text(encoding="utf-8")
        start = javascript.index("let timeRangeModalOwner")
        end = javascript.index("function initializeTimeRangeFilter")
        core = javascript[start:end]
        harness = "const document = { body: null };\n" + core + r"""
const now = new Date("2026-09-02T08:20:30.456Z");
const today = presetTimeRange("today", now);
const rolling = presetTimeRange("last-24-hours", now);
const exactMinute = presetTimeRange("last-24-hours", new Date("2026-09-02T08:20:00Z"));
const updated = presetTimeRange("last-3-months", now, true);
const crossYear = presetTimeRange("last-3-months", new Date("2026-01-02T08:20:30Z"));
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
  exactMinute,
  updated,
  crossYear,
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
        self.assertEqual(shanghai["today"]["summary"], "今天 · UTC+08:00\n2026-09-02 00:00 ~ 2026-09-02 23:59")
        self.assertEqual(shanghai["rolling"]["summary"], "最近 24 小时 · UTC+08:00\n2026-09-01 16:20 ~ 2026-09-02 16:20")
        self.assertEqual(shanghai["updated"]["summary"], "最近 3 个月（至今） · UTC+08:00\n2026-06-02 16:20 ~ 2026-09-02 16:20")
        self.assertEqual(shanghai["updated"]["before"], "")
        self.assertEqual(shanghai["rolling"]["endValue"], "2026-09-02T16:20")
        self.assertEqual(shanghai["exactMinute"]["summary"], "最近 24 小时 · UTC+08:00\n2026-09-01 16:20 ~ 2026-09-02 16:19")
        self.assertEqual(shanghai["exactMinute"]["endValue"], "2026-09-02T16:19")
        self.assertEqual(shanghai["crossYear"]["summary"], "最近 3 个月 · UTC+08:00\n2025-10-02 16:20 ~ 2026-01-02 16:20")
        self.assertEqual(shanghai["custom"]["range"]["summary"], "UTC+08:00\n2026-08-27 00:00 ~ 2026-09-02 23:59")
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
        self.assertEqual(new_york["rolling"]["summary"], "最近 24 小时 · UTC-04:00\n2026-09-01 04:20 ~ 2026-09-02 04:20")
        self.assertEqual(new_york["crossYear"]["summary"], "最近 3 个月 · UTC-04:00 → UTC-05:00\n2025-10-02 03:20 ~ 2026-01-02 03:20")
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
