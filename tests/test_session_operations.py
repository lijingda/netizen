"""Exercise the same operation through the published Feishu and Admin entries."""

import json
from html.parser import HTMLParser
import unittest
from unittest.mock import patch

from netizen_cli.admin.auth import AdminAuth
from netizen_cli.admin.transport import Request
from netizen_cli.admin.users import AdminUsers
from netizen_cli.admin.web import AdminWebApplication
from netizen_cli.backends import BackendPolicy, BackendUnsupported, Provider
from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.management import CurrentBindingTarget
from netizen_cli.runtime.contracts import NativeThreadMetadata
from netizen_cli.schedules.models import ScheduleRule
from netizen_cli.session_providers import SessionNames
from netizen_cli.session_settings import SessionFormView
from netizen_cli.worktrees.policy import WorkspacePolicy
from tests.support.channel_cards import (
    _card_button_value,
    _elements,
    direct_button_event,
    direct_card_event,
)
from tests.support.channel_fixtures import channel_fixture
from tests.support.channel_messages import FakeMessage


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


    async def test_codex_admin_rename_accepts_up_to_120_utf8_bytes(self):
        for count, status in ((30, 200), (31, 400), (61, 400)):
            with self.subTest(count=count):
                grant = (await self.actions())["rename"]
                result = await self.request("/api/v1/sessions/rename", self.payload(grant, name="🦊" * count))
                self.assertEqual(result.status, status, result.body)
        self.assertEqual(self.fx.runtime.rename_binding_calls, [(self.binding.id, "🦊" * 30)])


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
        store = self.fx.store.schedules
        plan = store.create(
            name="reserved topic", instructions="check", project_alias=self.binding.project_alias,
            app_id=self.scope.app_id, chat_id=self.scope.chat_id, workspace=WorkspacePolicy(),
            schedule=ScheduleRule("interval", "UTC", every_minutes=1, anchor=100), request_id="create", now=100,
        )
        claim = store.claim_due(plan.plan_id, app_id=self.scope.app_id, now=160)
        store.set_run(claim.run.id, phase="publishing_topic", topic_id="reserved")
        with patch.object(self.fx.management, "schedules", None):
            message = FakeMessage("/stop", message_id="om_stop", thread_id="reserved")
            with patch.object(self.fx.app, "_control") as control:
                await self.fx.app.handle_message(message)
                self.assertEqual(self.fx.channel.replies[-1], (message.id, "定时任务正在启动，请稍后重发。"))
                control.assert_not_awaited()
            with self.assertRaises(BackendUnsupported):
                await self.fx.app.dispatch_scheduled_run(None)
            result = await self.request("/api/v1/schedules/run-now", {})
            self.assertEqual(
                (result.status, json.loads(result.body)["code"]),
                (409, "backend_unsupported"),
            )
