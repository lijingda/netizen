from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from netizen.autonomy import AutonomyConflict, AutonomyService, AutonomyStore, create_schema
from netizen.autonomy.config import load_config, parse_config, public_config
from netizen.bindings import BindingTaskFeedback
from netizen.domain import MentionContextMode
from netizen.management import (
    CurrentBindingTarget, InstanceManagementService, ManagementRuntimePort, ScopeCoordinator,
)
from tests.admin import test_web as fixture


class DecisionServiceStub:
    """No provider calls: exercise the HTTP/management boundary and real validation."""

    def __init__(self) -> None:
        self.revision = 0
        self.config = None
        self.calls: list[tuple[str, object]] = []
        self.failure: BaseException | None = None

    def get_status(self) -> dict:
        config = public_config(self.config)
        if config is not None:
            # The HTTP boundary explicitly projects public fields, even if a
            # future application projection accidentally includes extra fields.
            config["api_key"] = self.config.api_key
        return {
            "revision": self.revision, "configured": self.config is not None,
            "config": config, "state": "ready" if config else "unconfigured",
            "error": None, "private_extra": "must-not-leak",
        }

    async def configure(self, payload: dict) -> dict:
        self.calls.append(("configure", payload))
        if payload["expected_revision"] != self.revision:
            raise AutonomyConflict("decision configuration changed")
        if self.failure is not None:
            raise self.failure
        self.config = None if payload.get("clear") else parse_config(payload, self.config)
        self.revision += 1
        return self.get_status()

    async def test_connection(self, *, expected_revision: int) -> dict:
        self.calls.append(("test", expected_revision))
        if expected_revision != self.revision:
            raise AutonomyConflict("decision configuration changed")
        return {"ok": True, "revision": self.revision, "error": None, "api_key": "must-not-leak"}


class AdminAutonomyTest(unittest.IsolatedAsyncioTestCase):
    request = fixture.AdminWebTest.request
    login = fixture.AdminWebTest.login
    json_get = fixture.AdminWebTest.json_get
    json_post = fixture.AdminWebTest.json_post
    asyncTearDown = fixture.AdminWebTest.asyncTearDown

    async def asyncSetUp(self) -> None:
        await fixture.AdminWebTest.asyncSetUp(self)
        self.autonomy = DecisionServiceStub()
        self.management._autonomy = self.autonomy
        for method in ("autonomy_status", "configure_autonomy", "test_autonomy_connection"):
            setattr(self.management, method, getattr(InstanceManagementService, method).__get__(self.management))
        self.runner.open_admission()

    async def page(self, session: str) -> dict:
        status, _, data = await self.json_get("/api/v1/autonomy", session)
        self.assertEqual(status, 200, data)
        return data

    async def save(self, session: str, config: dict):
        action = (await self.page(session))["actions"]["configure"]
        return await self.json_post(
            "/api/v1/autonomy/configure", session,
            fixture._action_payload(action, config=config),
        )

    async def test_configuration_and_script_require_auth_and_existing_action_security(self) -> None:
        for route in ("/api/v1/autonomy", "/static/autonomy.js"):
            status, _, _ = await self.request("GET", route)
            self.assertEqual(status, 401)
        for mode in ("configure", "test"):
            status, _, _ = await self.json_post(f"/api/v1/autonomy/{mode}", "invalid", {})
            self.assertEqual(status, 401)
        session = await self.login()
        action = (await self.page(session))["actions"]["configure"]
        payload = fixture._action_payload(action, config={"provider": "laya"})
        status, _, _ = await self.request("POST", "/api/v1/autonomy/configure", headers=[
            ("Cookie", f"netizen_admin_session={session}"),
            ("Origin", "https://other.example"), ("Content-Type", "application/json"),
        ], body=json.dumps(payload).encode())
        self.assertEqual(status, 403)
        status, _, _ = await self.json_post(
            "/api/v1/autonomy/configure", session, {**payload, "csrfToken": "invalid"},
        )
        self.assertEqual(status, 403)
        status, _, _ = await self.json_post("/api/v1/autonomy/configure", session, {
            **payload, "target": {**payload["target"], "targetId": "99"},
        })
        self.assertEqual(status, 409)
        self.assertEqual(self.autonomy.calls, [])
        status, _, body = await self.request("GET", "/static/autonomy.js", headers=[
            ("Cookie", f"netizen_admin_session={session}"),
        ])
        self.assertEqual(status, 200)
        self.assertIn(b"loadAutonomy", body)

    async def test_secret_is_write_only_blank_preserves_and_clear_is_explicit(self) -> None:
        session = await self.login()
        secret = "fixture-private-api-key"
        status, _, saved = await self.save(session, {"provider": "laya", "api_key": secret})
        self.assertEqual(status, 200, saved)
        self.assertTrue(saved["config"]["has_api_key"])
        self.assertNotIn(secret, json.dumps(saved))
        self.assertNotIn("private_extra", saved)
        self.assertNotIn("api_key", saved["config"])
        status, _, saved = await self.save(session, {"api_key": "", "timeout_seconds": 3})
        self.assertEqual(status, 200, saved)
        self.assertEqual(self.autonomy.config.api_key, secret)
        self.assertNotIn(secret, json.dumps(await self.page(session)))
        status, _, saved = await self.save(session, {"clear_api_key": True})
        self.assertEqual(status, 200, saved)
        self.assertFalse(saved["config"]["has_api_key"])
        self.assertTrue(saved["configured"])
        status, _, saved = await self.save(session, {"clear": True})
        self.assertEqual(status, 200, saved)
        self.assertIsNone(saved["config"])
        self.assertFalse(saved["configured"])
        self.assertIsNone(saved["actions"]["test"])

    async def test_action_replay_and_stale_revision_do_not_overwrite_configuration(self) -> None:
        session = await self.login()
        page = await self.page(session)
        stale = (await self.page(session))["actions"]["configure"]
        payload = fixture._action_payload(page["actions"]["configure"], config={"provider": "laya"})
        status, _, _ = await self.json_post("/api/v1/autonomy/configure", session, payload)
        self.assertEqual(status, 200)
        status, _, _ = await self.json_post("/api/v1/autonomy/configure", session, payload)
        self.assertEqual(status, 409)
        status, _, result = await self.json_post(
            "/api/v1/autonomy/configure", session,
            fixture._action_payload(stale, config={"clear": True}),
        )
        self.assertEqual(status, 409)
        self.assertEqual(result["code"], "autonomy_revision_conflict")
        self.assertIsNotNone(self.autonomy.config)
        self.assertEqual(self.autonomy.revision, 1)

    async def test_connection_test_uses_saved_revision_and_accepts_no_chat_content(self) -> None:
        session = await self.login()
        _, _, saved = await self.save(session, {"provider": "laya"})
        action = saved["actions"]["test"]
        status, _, _ = await self.json_post(
            "/api/v1/autonomy/test", session,
            fixture._action_payload(action, messages=["private group message"]),
        )
        self.assertEqual(status, 400)
        self.assertEqual([call for call in self.autonomy.calls if call[0] == "test"], [])
        status, _, result = await self.json_post(
            "/api/v1/autonomy/test", session, fixture._action_payload(action),
        )
        self.assertEqual(status, 200, result)
        self.assertEqual(self.autonomy.calls[-1], ("test", 1))
        self.assertTrue(result["ok"])
        self.assertNotIn("api_key", result)
        stale = (await self.page(session))["actions"]["test"]
        await self.save(session, {"clear": True})
        status, _, _ = await self.json_post(
            "/api/v1/autonomy/test", session, fixture._action_payload(stale),
        )
        self.assertEqual(status, 409)

    async def test_save_failure_does_not_claim_applied_or_expose_exception_text(self) -> None:
        session = await self.login()
        await self.save(session, {"provider": "laya"})
        self.autonomy.failure = OSError("private-key-from-failed-filesystem")
        with self.assertLogs("netizen.admin.web", level="INFO") as logs:
            status, _, result = await self.save(session, {"timeout_seconds": 2})
        self.assertEqual(status, 500)
        self.assertEqual(result["code"], "internal_error")
        self.assertNotIn("private-key", json.dumps(result) + "".join(logs.output))
        page = await self.page(session)
        self.assertEqual(page["revision"], 1)
        self.assertEqual(page["config"]["timeout_seconds"], 10)

    async def test_invalid_config_and_client_revision_override_are_rejected(self) -> None:
        session = await self.login()
        for config in ({"provider": "bad"}, {"provider": "laya", "input_budget": 99000},
                       {"expected_revision": 0, "provider": "laya"},
                       {"provider": "laya", "clear_api_key": "false"},
                       {"clear": True, "provider": "laya"}):
            status, _, _ = await self.save(session, config)
            self.assertEqual(status, 400)
        self.assertEqual(self.autonomy.revision, 0)

    async def test_page_without_injected_experiment_does_not_break_ordinary_admin(self) -> None:
        self.management._autonomy = None
        session = await self.login()
        page = await self.page(session)
        self.assertFalse(page["supported"])
        self.assertFalse(page["configured"])
        self.assertEqual(page["actions"], {"configure": None, "test": None})
        status, _, _ = await self.json_get("/api/v1/projects", session)
        self.assertEqual(status, 200)

    async def test_real_service_persists_applies_and_clears_without_changing_session_choice(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE bindings(binding_id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO bindings VALUES('binding-autonomous')")
        create_schema(connection)
        store = AutonomyStore(connection)
        store.set_enabled("binding-autonomous", True)
        path = self.root / "credentials" / "decision-model.json"
        provider = SimpleNamespace(decide=AsyncMock(return_value="consume"))
        service = AutonomyService(store, path, provider=provider)
        self.management._autonomy = service
        session = await self.login()
        status, _, saved = await self.save(session, {
            "provider": "laya", "api_key": "private-fixture-key",
        })
        self.assertEqual(status, 200, saved)
        self.assertTrue(service.configured)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        revision, config = load_config(path)
        self.assertEqual(revision, saved["revision"])
        self.assertEqual(config.api_key, "private-fixture-key")
        provider.decide.assert_not_awaited()
        status, _, tested = await self.json_post(
            "/api/v1/autonomy/test", session,
            fixture._action_payload(saved["actions"]["test"]),
        )
        self.assertEqual(status, 200, tested)
        self.assertTrue(tested["ok"])
        self.assertEqual(provider.decide.await_count, 1)
        test_config, sample = provider.decide.await_args.args
        self.assertEqual(test_config, config)
        self.assertIn("No private conversation", sample)
        self.assertNotIn("private-fixture-key", sample)
        with (
            patch("netizen.autonomy.service.save_config", side_effect=OSError("fixture-write-failure")),
            self.assertLogs("netizen.admin.web", level="ERROR"),
        ):
            status, _, failed = await self.save(session, {"timeout_seconds": 2})
        self.assertEqual(status, 500, failed)
        self.assertEqual(service.get_status()["revision"], revision)
        self.assertEqual(load_config(path), (revision, config))
        status, _, cleared = await self.save(session, {"clear": True})
        self.assertEqual(status, 200, cleared)
        self.assertFalse(service.configured)
        self.assertEqual(load_config(path), (revision + 1, None))
        self.assertTrue(store.is_enabled("binding-autonomous"))
        self.assertNotIn("private-fixture-key", path.read_text())


class AutonomyManagementAdmissionTest(unittest.IsolatedAsyncioTestCase):
    async def test_new_binding_mode_is_committed_through_existing_creation_boundary(self) -> None:
        binding = SimpleNamespace(id="binding-new")
        project = SimpleNamespace(alias="project-exact")
        bindings = SimpleNamespace(create_channel_binding=Mock(return_value=binding))
        application = SimpleNamespace(
            _scope_coordinator=ScopeCoordinator(), _bindings=bindings,
            _runtime=SimpleNamespace(binding_pointer_changed=AsyncMock()),
            _blocking_io=SimpleNamespace(submit=AsyncMock(return_value=project)),
            _projects=SimpleNamespace(resolve_for_new=Mock()), _active_id=lambda _scope: "binding-old",
        )
        for mode in (False, True):
            with self.subTest(mode=mode):
                created = await InstanceManagementService.create_current_binding(
                    application, scope=SimpleNamespace(key="scope-exact"), creator_id="user",
                    project_alias="project-exact", autonomy_enabled=mode,
                )
                self.assertIs(created.binding, binding)
                self.assertIs(bindings.create_channel_binding.call_args.kwargs["autonomy_enabled"], mode)
                application._runtime.binding_pointer_changed.assert_awaited_with("binding-old", "binding-new")

    async def test_optional_mode_reaches_exact_runtime_and_omission_preserves_legacy_port(self) -> None:
        binding = SimpleNamespace(id="binding-exact")
        native = SimpleNamespace(configure_context_exact=AsyncMock(return_value=binding))
        application = SimpleNamespace(
            _scope_coordinator=ScopeCoordinator(), _runtime=ManagementRuntimePort(native),
            _require_current=lambda _target: binding,
        )
        request = {
            "target": CurrentBindingTarget("scope-exact", "binding-exact"),
            "expected_settings_revision": 2, "expected_context_revision": 3,
            "expected_feedback_revision": 4, "settings": None,
            "task_feedback": BindingTaskFeedback(),
            "message_context_mode": MentionContextMode.CURRENT_ONLY, "context_anchor": None,
        }
        for mode in (None, True, False):
            with self.subTest(mode=mode):
                result = await InstanceManagementService.configure_current_binding(
                    application, **request, autonomy_enabled=mode,
                )
                self.assertIs(result, binding)
                received = native.configure_context_exact.await_args.kwargs
                self.assertEqual(received["binding_id"], "binding-exact")
                self.assertEqual(received["expected_context_revision"], 3)
                if mode is None:
                    self.assertNotIn("autonomy_enabled", received)
                else:
                    self.assertIs(received["autonomy_enabled"], mode)


class AdminAutonomyUiTest(unittest.TestCase):
    def test_save_clear_test_and_failure_behaviors(self) -> None:
        node = shutil.which("node")
        if node is None:
            if os.environ.get("CI") == "true":
                self.fail("Node.js is required for Admin JavaScript behavior tests")
            self.skipTest("Node.js is required for Admin JavaScript behavior tests")
        source = (Path(__file__).resolve().parents[1] / "netizen/admin/static/autonomy.js").read_text()
        before = r'''
const assert = require("node:assert/strict");
const nodes = new Map();
const document = { querySelector(id) {
  if (!nodes.has(id)) nodes.set(id, { value: "", checked: false, disabled: false, textContent: "",
    listeners: {}, addEventListener(event, callback) { this.listeners[event] = callback; } });
  return nodes.get(id);
} };
const window = { confirm() { return true; } };
let status = "";
let isError = false;
function setStatus(value, error = false) { status = value; isError = error; }
const calls = [];
const grant = { csrfToken: "csrf", actionToken: "action", target: { resource: "autonomy-config", targetId: "1" } };
const saved = () => ({ supported: true, revision: 1, configured: true, state: "ready", error: null,
  config: { provider: "laya", base_url: "http://localhost:8000", model: "multilingual",
    has_api_key: true, input_budget: 1024, timeout_seconds: 10 },
  actions: { configure: grant, test: grant } });
let answer = async () => saved();
async function api(path, options = {}) { calls.push({path, options}); return answer(path, options); }
'''
        after = r'''
(async () => {
  await loadAutonomy();
  assert.equal(nodes.get("#autonomy-key").value, "");
  assert.equal(nodes.get("#autonomy-fields").disabled, false);
  nodes.get("#autonomy-key").value = "write-only-secret";
  nodes.get("#autonomy-clear-key").checked = false;
  await nodes.get("#autonomy-form").listeners.submit({ preventDefault() {} });
  const savedRequest = calls.find((call) => call.options.method === "POST");
  assert.equal(JSON.parse(savedRequest.options.body).config.api_key, "write-only-secret");
  assert.equal(nodes.get("#autonomy-key").value, "");
  assert.equal(isError, false);
  assert.match(status, /已保存并应用/);
  answer = async (path, options) => {
    if (options.method === "POST") throw new Error("save failed");
    return saved();
  };
  nodes.get("#autonomy-key").value = "second-secret";
  await nodes.get("#autonomy-form").listeners.submit({ preventDefault() {} });
  assert.equal(isError, true);
  assert.match(status, /save failed/);
  assert.equal(nodes.get("#autonomy-key").value, "");
  assert.equal(nodes.get("#autonomy-fields").disabled, false);
  answer = async (_path, options) => options.method === "POST"
    ? { ...saved(), configured: false, config: null, state: "unconfigured" } : saved();
  await nodes.get("#autonomy-clear").listeners.click();
  assert.deepEqual(JSON.parse(calls.at(-1).options.body).config, { clear: true });
  assert.match(status, /已清除/);
  await loadAutonomy();
  answer = async (_path, options) => options.method === "POST"
    ? { ok: false, revision: 1, error: "provider unavailable" } : saved();
  await nodes.get("#autonomy-test").listeners.click();
  const tested = calls.filter((call) => call.path.endsWith("/test")).at(-1);
  assert.deepEqual(Object.keys(JSON.parse(tested.options.body)).sort(), ["actionToken", "csrfToken", "target"]);
  assert.equal(isError, true);
  assert.equal(status, "provider unavailable");
  let release;
  answer = () => new Promise((resolve) => { release = resolve; });
  const pending = mutateAutonomy("configure", { provider: "laya" });
  const count = calls.length;
  await mutateAutonomy("configure", { clear: true });
  assert.equal(calls.length, count);
  assert.equal(nodes.get("#autonomy-fields").disabled, true);
  release(saved());
  await pending;
  assert.equal(nodes.get("#autonomy-fields").disabled, false);
})().catch((error) => { console.error(error); process.exitCode = 1; });
'''
        result = subprocess.run([node, "-e", before + source + after], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
