"""Exercise the same operation through the published Feishu and Admin entries."""

import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from netizen_cli.admin.auth import AdminAuth
from netizen_cli.admin.transport import Request
from netizen_cli.admin.users import AdminUsers
from netizen_cli.admin.web import AdminWebApplication
from netizen_cli.backends import BackendUnsupported
from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.management import CurrentBindingTarget
from netizen_cli.runtime.contracts import NativeThreadMetadata
from netizen_cli.session_providers import SessionNames
from tests.support.channel_cards import (
    _card_button_value,
    _elements,
    direct_button_event,
    direct_card_event,
)
from tests.support.channel_fixtures import channel_fixture
from tests.support.channel_messages import FakeMessage
from tests.test_claude_channel import owner_channel


class SessionOperationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fx = await self.enterAsyncContext(channel_fixture())
        self.scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        await self.fx.new()
        self.binding = self.fx.store.active_binding(self.scope.key)
        self.fx.runtime.materialize(self.binding.id, "native", cwd=self.fx.project)
        self.fx.runtime.thread_metadata_values["native"] = NativeThreadMetadata(
            "native", "Before", ""
        )
        users = AdminUsers(self.fx.project_root, "cli_test")
        member = users.add_initial("ou_admin")
        auth = AdminAuth(users)
        self.admin = AdminWebApplication(auth=auth, management=self.fx.management)
        self.admin.configure_authorities(("localhost",))
        self.admin.open_admission()
        self.addCleanup(self.admin.close_auth)
        token = auth.issue_session(
            member.identity,
            source_ip="127.0.0.1",
            expected_add_revision=member.add_revision,
        ).token
        self.headers = (
            (b"Host", b"localhost"),
            (b"Origin", b"http://localhost"),
            (b"Cookie", ("netizen_admin_session=" + token).encode()),
            (b"Content-Type", b"application/json"),
        )

    async def request(self, path, data=None):
        return await self.admin.handle(
            Request(
                b"GET" if data is None else b"POST",
                path.encode(),
                b"1.1",
                self.headers,
                b"" if data is None else json.dumps(data).encode(),
                ("127.0.0.1", 1234),
                ("127.0.0.1", 80),
                "request",
            )
        )

    async def actions(self):
        response = await self.request("/api/v1/sessions?inventoryState=active")
        self.assertEqual(response.status, 200)
        return json.loads(response.body)["items"][0]["actions"]

    def payload(self, grant, **extra):
        return {
            key: grant[key] for key in ("csrfToken", "actionToken", "target")
        } | extra

    async def command(self, text):
        await self.fx.app.handle_message(FakeMessage(text, message_id="om_command"))
        return self.fx.channel.replies[-1][1]

    async def test_manual_only_form_and_authenticated_dispatch(self):
        names = self.fx.management.names
        old_card = await self.command("/rename")
        old_generate = _card_button_value(old_card, "自动生成")
        old_actions = await self.actions()
        original_page = await self.request("/sessions/ordinary")
        self.assertIn(b'id="session-rename-generate" class=', original_page.body)
        self.assertIn('<p class="hint">自动生成'.encode(), original_page.body)
        with patch.object(
            self.fx.management, "names", SessionNames(writer=names.writer)
        ):
            self.assertIn("/rename", await self.command("/"))
            form = await self.command("/rename")
            self.assertNotIn("自动生成", str(form.card))
            field = _elements(form.card, "input")[0]["name"]
            await self.fx.app.handle_card_action(
                direct_card_event(
                    self.fx.channel, {field: "saved"}, message_id="om_form"
                )
            )
            self.assertEqual(
                self.fx.runtime.rename_binding_calls, [(self.binding.id, "saved")]
            )
            await self.fx.app.handle_card_action(
                direct_button_event(old_generate, message_id="om_form")
            )
            self.assertFalse(self.fx.app._rename_name_generations)
            actions = await self.actions()
            self.assertIn("rename", actions)
            self.assertNotIn("generateName", actions)
            page = await self.request("/sessions/ordinary")
            self.assertIn(b'id="session-rename-generate" hidden', page.body)
            self.assertIn('<p class="hint" hidden>自动生成'.encode(), page.body)
            denied = await self.request(
                "/api/v1/sessions/generate-name",
                self.payload(old_actions["generateName"]),
            )
            self.assertEqual(
                (denied.status, json.loads(denied.body)["code"]),
                (409, "backend_unsupported"),
            )
            saved = await self.request(
                "/api/v1/sessions/rename",
                self.payload(actions["rename"], name="admin saved"),
            )
            self.assertEqual(saved.status, 200)
            self.assertEqual(
                self.fx.runtime.rename_binding_calls[-1],
                (self.binding.id, "admin saved"),
            )
            self.assertEqual(self.fx.runtime.generate_name_calls, [])

    async def test_candidate_never_implies_write_even_with_a_valid_prior_grant_or_card(
        self,
    ):
        old_action = (await self.actions())["rename"]
        old_card = await self.command("/rename")
        field = _elements(old_card.card, "input")[0]["name"]
        with patch.object(
            self.fx.management,
            "names",
            SessionNames(generate=self.fx.management.names.generate),
        ):
            self.assertNotIn("/rename", await self.command("/help"))
            self.assertIn("不支持", await self.command("/rename write"))
            await self.fx.app.handle_card_action(
                direct_card_event(
                    self.fx.channel, {field: "old card"}, message_id="om_old"
                )
            )
            self.assertNotIn("rename", await self.actions())
            response = await self.request(
                "/api/v1/sessions/rename", self.payload(old_action, name="old grant")
            )
            self.assertEqual(
                (response.status, json.loads(response.body)["code"]),
                (409, "backend_unsupported"),
            )
            with self.assertRaises(BackendUnsupported):
                await self.fx.management.rename_current_binding(
                    target=CurrentBindingTarget(self.scope.key, self.binding.id),
                    name="direct",
                )
        self.assertEqual(self.fx.runtime.rename_binding_calls, [])
        self.assertEqual(self.fx.runtime.generate_name_calls, [])

    async def test_unknown_entries_and_absent_schedule_service(self):
        card = await self.command("/rename")
        action = dict(
            _card_button_value(card, "自动生成"), intent="binding.name.unknown"
        )
        direct_card_event(self.fx.channel, {}, message_id="om_unknown")
        await self.fx.app.handle_card_action(
            direct_button_event(action, message_id="om_unknown")
        )
        self.assertFalse(self.fx.app._rename_name_generations)
        self.assertEqual(self.fx.runtime.rename_binding_calls, [])
        self.assertEqual(self.fx.runtime.generate_name_calls, [])
        result = await self.request("/api/v1/sessions/rename-extra", {})
        self.assertEqual(result.status, 404)
        with patch.object(self.fx.management, "schedules", None):
            with self.assertRaises(BackendUnsupported):
                await self.fx.app.dispatch_scheduled_run(None)
            result = await self.request("/api/v1/schedules/run-now", {})
            self.assertEqual(
                (result.status, json.loads(result.body)["code"]),
                (409, "backend_unsupported"),
            )

    async def test_owner_and_exact_target_still_apply_to_a_synthetic_ordinary_writer(
        self,
    ):
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            clock[0] = 2
            row = await fx.create_binding(self.scope)
            fx.store.assign_native_thread_id(row.binding.id, "sample")
            writer = Mock()
            writer.rename_exact = AsyncMock(return_value="allowed")
            with patch.object(app._management, "names", SessionNames(writer=writer)):
                await app.handle_message(
                    FakeMessage(
                        "/rename denied", message_id="om_actor", sender_id="other"
                    )
                )
                self.assertIn("仅限所有者", fx.channel.replies[-1][1])
                writer.rename_exact.assert_not_called()
                await app.handle_message(FakeMessage("/rename", message_id="om_form"))
                form = fx.channel.replies[-1][1]
                self.assertNotIn("自动生成", str(form.card))
                await app.handle_message(
                    FakeMessage("/rename allowed", message_id="om_actor")
                )
                writer.rename_exact.assert_awaited_once_with(row.binding.id, "allowed")
                field = _elements(form.card, "input")[0]["name"]
                await fx.create_binding(self.scope)
                await app.handle_card_action(
                    direct_card_event(fx.channel, {field: "stale"}, message_id="om_old")
                )
                writer.rename_exact.assert_awaited_once()
                self.assertEqual(clients.calls, [])
