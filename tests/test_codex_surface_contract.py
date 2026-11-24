"""Wire fixtures captured from the Codex reference at 30a9e8c."""

import asyncio
import base64
import itertools
import json
from types import SimpleNamespace
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from uuid import UUID


from netizen_cli.admin.auth import AdminAuth
from netizen_cli.admin.transport import Request
from netizen_cli.admin.users import AdminUsers
from netizen_cli.admin.web import AdminWebApplication
from netizen_cli.bindings import BindingTaskFeedback, BindingTurnSettings, ThreadBinding
from netizen_cli.codex_runtime import CodexRuntime
from netizen_cli.domain import FeishuScope, NativeCapability, ScopeKind
from netizen_cli.management import SessionQuery, SessionInventoryState, CurrentBindingTarget, InstanceManagementService, ManagementRuntimePort
from netizen_cli.ordinary_sessions import OrdinaryConclusion
from netizen_cli.runtime.codex_ordinary import CodexOrdinarySessions
from netizen_cli.runtime.contracts import NativeThreadMetadata
from tests.support.channel_cards import _card_button_value, _elements, direct_button_event, direct_card_event
from tests.support.channel_fixtures import channel_fixture
from tests.support.presenter_traces import delivery_recorder
from tests.support.channel_messages import FakeMessage
from tests.test_codex_runtime import FakeTerminalCleanup, FakeThreadSubscriptionControl
from tests.test_runtime_thread_naming import NamingCodex, NamingThread, eventually

FIXTURE = Path(__file__).parent / "fixtures" / "codex_surfaces_30a9e8c.json"
DIRECT = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)


def native_access_recorder(runtime, stack):
    calls = []

    def argument(value):
        if isinstance(value, ThreadBinding):
            return {"bindingId": value.id, "threadId": value.native_thread_id}
        if isinstance(value, Path):
            return str(value)
        return value

    def capture(method):
        async def call(*args, **kwargs):
            options = {key: "bounded" if key == "deadline" and value is not None else argument(value)
                       for key, value in kwargs.items()}
            calls.append([method.__name__, [argument(value) for value in args], options])
            return await method(*args, **kwargs)
        return call

    for name in ("model_catalog", "thread_metadata", "thread_summary", "thread_catalog_page", "goal_snapshot",
                 "native_thread_cwd", "resolve_binding_cwd", "rename_exact", "generate_name_exact"):
        stack.enter_context(patch.object(runtime, name, capture(getattr(runtime, name))))
    return calls


async def channel_surfaces(fx, events, calls):
    seen = {}

    def captured(label):
        seen[label] = {"delivery": events[:], "nativeAccess": calls[:]}
        events.clear()
        calls.clear()

    async def command(label, text, **kwargs):
        await fx.app.handle_message(FakeMessage(text, message_id="om-" + label, **kwargs))
        captured(label)
        return fx.channel.replies[-1][1]

    await command("degraded_help", "/help")
    fx.runtime.available_capabilities = frozenset(NativeCapability)
    await command("full_menu", "/")
    await command("new_direct", "/new")
    await command("defaults_group", "/defaults", chat_id="oc_group", chat_type="group")
    await fx.new()
    binding = fx.store.active_binding(DIRECT.key)
    await command("lazy_status", "/status")
    await command("lazy_config", "/config")
    await command("lazy_current", "/sessions")
    fx.runtime.materialize(binding.id, "thread-a", cwd=fx.project)
    await command("unlisted_current", "/sessions")
    fx.runtime.thread_metadata_values["thread-a"] = NativeThreadMetadata("thread-a", "Title", "Preview", 12, 11)
    await command("listed_current", "/sessions")
    fx.runtime.archived_thread_metadata_values["thread-a"] = fx.runtime.thread_metadata_values.pop("thread-a")
    await command("archived", "/sessions archived")
    fx.runtime.thread_metadata_values["thread-a"] = fx.runtime.archived_thread_metadata_values.pop("thread-a")
    await command("named_status", "/status")
    form = await command("rename_form", "/rename")
    prompt = direct_card_event(fx.channel, {}, message_id="om-name-card")
    await fx.app.handle_card_action(direct_button_event(_card_button_value(form, "自动生成"), message_id=prompt.message_id))
    task = fx.app._rename_name_generations[prompt.message_id].task
    await asyncio.wait_for(task, 2)
    captured("candidate_updates")
    await command("manual_receipt", "/rename saved")
    for status, answer in (("completed", "reference answer"), ("failed", None), ("interrupted", None)):
        origin = FakeMessage("request", message_id="om-" + status)
        await fx.app.handle_ordinary_completion(OrdinaryConclusion(
            binding.id, "thread-a", "turn-" + status, "ou_user", origin, status, answer,
            task_feedback=BindingTaskFeedback(progress_card_enabled=False), cwd=fx.project,
        ))
        captured("result_" + status)
    fx.store.create_channel_binding(scope=DIRECT, project_alias="test", creator_id="ou_user",
                                    turn_settings=BindingTurnSettings("future-model", "ultra", "priority-v2"))
    await command("explicit_status", "/status")
    field = _elements(form.card, "input")[0]["name"]
    await fx.app.handle_card_action(direct_card_event(fx.channel, {field: "stale"}, message_id="om-name-card"))
    captured("stale_rename")
    assert "切换" in str(seen["stale_rename"])
    seen["name_effects"] = {"generate": list(fx.runtime.generate_name_calls), "write": list(fx.runtime.rename_binding_calls)}
    return seen


async def admin_surfaces(fx, calls):
    traces = []
    users = AdminUsers(fx.project_root, "cli_test")
    member = users.add_initial("ou_admin")
    auth = AdminAuth(users, clock=lambda: 1000.0)
    admin = AdminWebApplication(auth=auth, management=fx.management)
    admin.configure_authorities(("localhost",))
    admin.open_admission()
    token = auth.issue_session(member.identity, source_ip="127.0.0.1", expected_add_revision=member.add_revision).token

    async def request(path, payload=None):
        headers = ((b"Host", b"localhost"), (b"Cookie", ("netizen_admin_session=" + token).encode()),
                   (b"Origin", b"http://localhost"), (b"Content-Type", b"application/json"))
        response = await admin.handle(Request(
            b"GET" if payload is None else b"POST", ("/api/v1/" + path).encode(), b"1.1", headers,
            b"" if payload is None else json.dumps(payload).encode(), ("127.0.0.1", 1234), ("127.0.0.1", 80), "r1",
        ))
        traces.append([path, calls[:]])
        calls.clear()
        return response.status, json.loads(response.body)

    try:
        result = {}
        for state in ("active", "lazy"):
            result[state] = await request("sessions?inventoryState=" + state)
        native_id = "thread-a"
        fx.runtime.archived_thread_metadata_values[native_id] = fx.runtime.thread_metadata_values.pop(native_id)
        result["archived_with_row"] = await request("sessions?inventoryState=archived")
        assert result["archived_with_row"][1]["items"][0]["catalogState"] == "archived"
        fx.runtime.thread_metadata_values[native_id] = fx.runtime.archived_thread_metadata_values.pop(native_id)
        binding_id = result["active"][1]["items"][0]["bindingId"]
        result["poll"] = await request("runtime-snapshots?resolvePrimary=true&bindingIds=" + binding_id)
        assert result["poll"][0] == 200
        actions = result["active"][1]["items"][0]["actions"]
        for key, route in (("generateName", "generate-name"), ("rename", "rename")):
            data = actions[key]
            payload = {k: data[k] for k in ("csrfToken", "actionToken", "target")}
            if key == "rename":
                payload["name"] = "from admin"
            result[key] = await request("sessions/" + route, payload)
            result[key + "_replay"] = await request("sessions/" + route, payload)
        result["archived"] = await request("sessions?inventoryState=archived")
        status, empty = result["archived"]
        assert status == 200 and empty["items"] == [] and empty["nextCursor"] is None
        result["nativeAccess"] = traces
        return result
    finally:
        admin.close_auth()


async def sdk_requests(fx):
    codex = NamingCodex()
    calls = []
    runtime = CodexRuntime(codex=codex, bindings=fx.store, terminal_cleanup=FakeTerminalCleanup(codex.events),
                           thread_subscription_control=FakeThreadSubscriptionControl())
    management = InstanceManagementService(bindings=fx.store, projects=fx.projects,
        runtime=ManagementRuntimePort(runtime, ordinary=CodexOrdinarySessions(runtime)),
        scope_coordinator=fx.management.scope_coordinator)
    binding = fx.store.create_channel_binding(scope=DIRECT, project_alias="test", creator_id="ou_user")
    fx.store.assign_native_thread_id(binding.id, "thread-sdk")
    target = CurrentBindingTarget(DIRECT.key, binding.id)

    def trace(method):
        async def recording(receiver, *args, **kwargs):
            calls.append((method.__name__, getattr(receiver, "id", None), args, kwargs))
            return await method(receiver, *args, **kwargs)
        return recording

    try:
        with ExitStack() as stack:
            for cls, names in ((NamingCodex, ("thread_start", "thread_resume", "thread_fork", "thread_list",
                                               "thread_archive", "thread_unarchive")),
                               (NamingThread, ("set_name", "read", "turn"))):
                for name in names:
                    stack.enter_context(patch.object(cls, name, trace(getattr(cls, name))))
            entries = {}

            def recorded(label):
                entries[label] = calls[:]
                calls.clear()

            deadline = asyncio.get_running_loop().time() + 3
            for state in (SessionInventoryState.ACTIVE, SessionInventoryState.ARCHIVED):
                codex.thread_list_pages = [SimpleNamespace(data=[], next_cursor=None)]
                await management.query_sessions(query=SessionQuery(inventory_state=state), limit=10, deadline=deadline)
                recorded("list_" + state.value)
            with patch("netizen_cli.runtime.state.AsyncThread", side_effect=lambda client, tid: NamingThread(tid, client)):
                summary = await management.binding_metadata_exact(binding.id, deadline=deadline)
                assert summary.thread_id == "thread-sdk"
            recorded("status_metadata")
            await management.binding_status_exact(binding.id)
            recorded("poll")
            await management.rename_current_binding(target=target, name=" sdk   name ")
            recorded("rename")
            pending = asyncio.create_task(management.generate_current_binding_name(target=target))
            try:
                await eventually(lambda: bool(codex.handles) or pending.done())
                if codex.handles:
                    codex.handles[-1].complete(response='{"title":"candidate"}')
                result = await asyncio.wait_for(pending, 2)
                assert result.name == "candidate"
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            recorded("generate")
        return entries
    finally:
        await runtime.cancel_tasks()
        await management.close()


async def reference():
    async with channel_fixture() as fx:
        nonces, grants = itertools.count(1), itertools.count(1)
        with (patch("secrets.token_hex", side_effect=lambda n: next(nonces).to_bytes(n, "big").hex()),
              patch("secrets.token_urlsafe", side_effect=lambda n: base64.urlsafe_b64encode(
                  next(grants).to_bytes(n, "big")).decode().rstrip("=")),
              patch("netizen_cli.cards.defaults.uuid4", return_value=UUID(int=1)),
              patch("netizen_cli.bindings._now", return_value="2026-01-01T00:00:00.000000+00:00")):
            with ExitStack() as stack:
                calls = native_access_recorder(fx.runtime, stack)
                channel = await channel_surfaces(fx, delivery_recorder(fx.channel, stack), calls)
                # Both the HTTP route and each SQLite worker have a deadline.
                # Keep the real worker, SQL, and wire comparison on slow CI hosts.
                query_seconds = 60
                submit_query = fx.store._submit_query
                async def fixture_query(operation, *, deadline_seconds):
                    return await submit_query(operation, deadline_seconds=query_seconds)
                with (patch("netizen_cli.admin.web._QUERY_DEADLINE_SECONDS", query_seconds),
                      patch("netizen_cli.admin.web._SESSION_QUERY_DEADLINE_SECONDS", query_seconds),
                      patch.object(fx.store, "_submit_query", fixture_query)):
                    admin = await admin_surfaces(fx, calls)
            sdk = await sdk_requests(fx)
            recorded = json.dumps({"channel": channel, "admin": admin, "sdk": sdk}, ensure_ascii=False)
            # macOS may resolve /var/... to /private/var/... before a path reaches a card.
            return json.loads(recorded.replace(str(fx.project_root.resolve()), "<root>")
                              .replace(str(fx.project_root), "<root>"))


class CodexSurfaceContract(unittest.IsolatedAsyncioTestCase):
    async def test_reference(self):
        expected = json.loads(FIXTURE.read_text())
        actual = await reference()
        for section in expected:
            with self.subTest(section=section):
                self.assertEqual(expected[section], actual[section])
