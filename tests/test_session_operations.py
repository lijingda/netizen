"""Exercise the same operation through the published Feishu and Admin entries."""

import json
from dataclasses import replace
from html.parser import HTMLParser
import unittest
from unittest.mock import AsyncMock, Mock, patch

from netizen_cli.admin.auth import AdminAuth
from netizen_cli.admin.transport import Request
from netizen_cli.admin.users import AdminUsers
from netizen_cli.admin.web import AdminWebApplication
from netizen_cli.backends import BackendPolicy, BackendUnsupported, Provider
from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.management import CurrentBindingTarget
from netizen_cli.runtime.contracts import NativeThreadMetadata
from netizen_cli.session_providers import SessionNames
from netizen_cli.session_settings import SessionFormView, SessionSettings
from tests.support.channel_cards import (
    _card_button_value,
    _elements,
    direct_button_event,
    direct_card_event,
    form_values,
)
from tests.support.channel_fixtures import channel_fixture
from tests.support.channel_messages import FakeMessage
from tests.test_claude_channel import owner_channel


class RenameControls(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.in_editor = False
        self.hidden = {}
        self.feed(page.decode())

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "form":
            self.in_editor = attributes.get("id") == "session-rename-editor"
        if self.in_editor:
            key = attributes.get("id")
            if tag == "p" and key is None and "hint" in attributes.get("class", "").split():
                key = "save-hint"
            if key:
                self.hidden[key] = "hidden" in attributes

    def handle_endtag(self, tag):
        if tag == "form":
            self.in_editor = False


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
        self.open_admin(self.fx.project_root, self.fx.management, "ou_admin")

    def open_admin(self, root, management, identity, **options):
        users = AdminUsers(root, "cli_test")
        member = users.add_initial(identity)
        auth = AdminAuth(users)
        self.admin = AdminWebApplication(auth=auth, management=management, **options)
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

    async def test_local_observation_failures_and_absent_fields_use_defaults_across_transports(self):
        async with owner_channel() as (app, fx, _, clock, ordinary):
            clock[0] = 2
            bindings = [(await fx.create_binding(self.scope)).binding for _ in range(4)]
            self.open_admin(fx.project_root, app._management, "ou_user")
            defaults = ordinary.observation_defaults.json()
            ids = ",".join(binding.id for binding in bindings)

            def check_rows(rows):
                self.assertEqual({row["bindingId"] for row in rows}, {binding.id for binding in bindings})
                for row in rows:
                    self.assertEqual(row["observations"], defaults)
                    self.assertEqual(row["localLabel"], "空闲")

            async def lists():
                await app.handle_message(FakeMessage("/sessions", message_id="list"))
                self.assertIn("上次观测的模型：未观测", str(fx.channel.replies[-1][1].card))
                response = await self.request("/api/v1/sessions")
                self.assertEqual(response.status, 200)
                check_rows([item["runtime"] for item in json.loads(response.body)["items"]])

            with patch.object(ordinary.observations, "read", side_effect=TimeoutError) as read, self.assertLogs(
                "netizen_cli.runtime.claude_status", level="WARNING",
            ):
                await lists()
                self.assertEqual(read.call_count, 2 * len(bindings))

            source = app._management.status_source
            def without_observations(binding, snapshot=None):
                return replace(source.capture(binding, snapshot), observations=None)

            with patch.object(app._management, "status_source", Mock(capture=without_observations)):
                await lists()
                await app.handle_message(FakeMessage("/status", message_id="status"))
                self.assertIn("上下文窗口：取不到", str(fx.channel.replies[-1][1].card))
                self.assertIn("订阅：不适用", str(fx.channel.replies[-1][1].card))
                for resolve in ("false", "true"):
                    response = await self.request(f"/api/v1/runtime-snapshots?bindingIds={ids}&resolvePrimary={resolve}")
                    self.assertEqual(response.status, 200)
                    check_rows(json.loads(response.body)["bindings"])

            with (patch.object(app._management, "status_source", Mock(capture=Mock(side_effect=TimeoutError))),
                  patch.object(ordinary.observations, "read", side_effect=AssertionError("retried read")) as read):
                await lists()
                read.assert_not_called()

    async def test_channel_and_admin_inherit_the_bound_policy(self):
        async with owner_channel() as (app, fx, clients, clock, ordinary):
            self.open_admin(fx.project_root, app._management, "ou_user")
            self.assertIs(self.admin._backend_policy, app._management.policy)
            self.assertIs(app._policy, self.admin._backend_policy)
            self.assertIs(app._management.operation_policy(ordinary.backend_policy), app._policy)
            for identity in (BackendPolicy("codex"), BackendPolicy("claude", "other")):
                with self.assertRaisesRegex(ValueError, "identity conflicts"):
                    AdminWebApplication(auth=self.admin._auth, management=app._management, backend_policy=identity)
            page = await self.request("/sessions/ordinary")
            self.assertIn(b'data-owner-only="true"', page.body)
            denied = await self.request("/api/v1/admin-users/selector-config")
            self.assertEqual(denied.status, 403)

    async def test_owner_forms_and_authenticated_post_reject_unsupported_fields(self):
        async with owner_channel() as (app, fx, clients, clock, _):
            clock[0] = 2
            binding = (await fx.create_binding(self.scope)).binding
            self.open_admin(fx.project_root, app._management, "ou_user", backend_policy=app._policy)
            for command, prefix in (("/new", "new"), ("/config", "config"), ("/defaults", "defaults_session")):
                await app.handle_message(FakeMessage(command, message_id=prefix))
                values = form_values(fx.channel.replies[-1][1])
                for field, value, notice in (("effort", "high", "模型"), ("speed", "fast", "模型")):
                    with self.subTest(command=command, field=field):
                        await app.handle_card_action(direct_card_event(fx.channel, {**values, prefix + "_" + field: value}))
                        self.assertIn(notice, fx.channel.replies[-1][1])
            for override in ({"turn_settings": {
                "model_id": "invented", "effort_id": "high", "service_tier_id": "fast",
            }},):
                action = (await self.actions())["configure"]
                denied = await self.request("/api/v1/sessions/configure", self.payload(
                    action, sessionSettings=SessionSettings().to_dict() | override,
                ))
                self.assertEqual((denied.status, json.loads(denied.body)["code"]), (409, "backend_unsupported"))
            action = (await self.actions())["configure"]
            permitted = await self.request("/api/v1/sessions/configure", self.payload(
                action, sessionSettings=SessionSettings().to_dict() | {"progress_card_enabled": True}))
            self.assertEqual(permitted.status, 200)
            self.assertTrue(fx.store.get(binding.id).task_feedback.progress_card_enabled)
            self.assertEqual(clients.calls, [])

    def test_model_and_progress_authority_are_independent(self):
        for model_editable, progress_editable in ((True, False), (False, True)):
            view = SessionFormView(model_editable, progress_editable)
            feedback = view.defaults(None).task_feedback
            self.assertEqual(feedback.progress_card_enabled, progress_editable)
            view.validate(model_editable, feedback)
            if not model_editable:
                with self.assertRaises(BackendUnsupported):
                    view.validate(True, feedback)

    async def test_manual_only_form_and_authenticated_dispatch(self):
        names = self.fx.management.names
        old_card = await self.command("/rename")
        old_generate = _card_button_value(old_card, "自动生成")
        old_actions = await self.actions()
        original_page = await self.request("/sessions/ordinary")
        original = RenameControls(original_page.body).hidden
        self.assertFalse(original["session-rename-generate"])
        self.assertFalse(original["save-hint"])
        with patch.object(
            self.fx.management, "names", SessionNames(writer=names.writer)
        ):
            self.assertIn("/rename", await self.command("/"))
            form = await self.command("/rename")
            self.assertNotIn("自动生成", str(form.card))
            self.assertIn("保存后名称同步显示在 Codex App/CLI。", str(form.card))
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
            controls = RenameControls(page.body).hidden
            self.assertIn("保存后名称同步显示在 Codex App/CLI。".encode(), page.body)
            self.assertTrue(controls["session-rename-generate"])
            self.assertFalse(controls["save-hint"])
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

    async def test_no_generator_with_unassembled_thread_namer(self):
        from netizen_cli.codex_runtime import CodexRuntime
        from netizen_cli.management import ManagementRuntimePort
        from netizen_cli.runtime.codex_ordinary import CodexOrdinarySessions
        from tests.test_codex_runtime import FakeCodex, FakeTerminalCleanup

        codex = FakeCodex()
        runtime = CodexRuntime(codex=codex, bindings=self.fx.store, terminal_cleanup=FakeTerminalCleanup(codex.events))
        self.addAsyncCleanup(runtime.cancel_tasks)
        port = ManagementRuntimePort(runtime, ordinary=CodexOrdinarySessions(runtime))
        self.assertIsNotNone(port.names.writer)
        self.assertIsNone(port.names.generate)

    async def test_protected_api_descendants_and_delete_grant(self):
        policy = BackendPolicy("codex").bind(lambda provider: provider in {Provider.ORDINARY, Provider.HOST})
        with patch.object(self.admin, "_backend_policy", policy):
            for family in ("schedules", "side-topics", "session-tools", "session-tool-access", "progress-card",
                           "question-notifications", "sessions/conversation", "sessions/delete-materialized"):
                with self.subTest(family=family):
                    reply = await self.request("/api/v1/" + family + "/future")
                    self.assertEqual((reply.status, json.loads(reply.body)["code"]), (409, "backend_unsupported"))
            self.assertNotIn("deleteMaterialized", await self.actions())

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
            writer = Mock(sync_hint=None)
            writer.rename_exact = AsyncMock(return_value="allowed")
            writer.eligible.side_effect = lambda binding, state: binding.native_thread_id is not None
            self.open_admin(fx.project_root, app._management, "ou_user", backend_policy=app._policy)
            self.assertNotIn("rename", await self.actions())
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
                grant = (await self.actions())["rename"]
                writer.eligible.side_effect = None
                writer.eligible.return_value = False
                for command in ("/rename", "/rename revoked"):
                    await app.handle_message(FakeMessage(command, message_id="om_revoked"))
                    self.assertIn("不支持重命名", str(fx.channel.replies[-1][1]))
                await app.handle_card_action(direct_card_event(fx.channel, {field: "revoked"}, message_id="om_form"))
                denied = await self.request("/api/v1/sessions/rename", self.payload(grant, name="revoked"))
                self.assertEqual((denied.status, json.loads(denied.body)["code"]), (409, "backend_unsupported"))
                self.assertNotIn("rename", await self.actions())
                writer.rename_exact.assert_awaited_once()
                writer.eligible.side_effect = lambda binding, state: binding.native_thread_id is not None
                await fx.create_binding(self.scope)
                await app.handle_card_action(
                    direct_card_event(fx.channel, {field: "stale"}, message_id="om_old")
                )
                writer.rename_exact.assert_awaited_once()
                rows = json.loads((await self.request("/api/v1/sessions")).body)["items"]
                actual = next(item for item in rows if item["bindingId"] == row.binding.id)["actions"]
                self.assertIn("rename", actual)
                self.assertNotIn("generateName", actual)
                self.assertNotIn("rename", next(item for item in rows if item["catalogState"] == "lazy")["actions"])
                saved = await self.request("/api/v1/sessions/rename", self.payload(actual["rename"], name="admin"))
                self.assertEqual(saved.status, 200)
                writer.rename_exact.assert_awaited_with(row.binding.id, "admin")
                self.assertEqual(clients.calls, [])
