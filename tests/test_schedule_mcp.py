from __future__ import annotations

import asyncio
import json
import tomllib
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from jsonschema import Draft202012Validator
from mcp.types import CallToolRequestParams

from netizen.schedules.mcp import CREATE_EXAMPLE, MAX_BODY_BYTES, ScheduleMcpRunner, _Arguments, _tool_schema
from netizen.schedules.models import ScheduleRule


class ScheduleMcpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []

        async def manage(request, thread_id):
            self.requests.append((request, thread_id))
            await asyncio.sleep(0)
            return {"ok": True, "thread_id": thread_id, "plans": []}

        self.runner = ScheduleMcpRunner()
        self.runner.attach(manage)
        await self.runner.bind()
        token = next(iter(self.runner.app_server_env.values()))
        self.client = httpx.AsyncClient(
            base_url=self.runner.url,
            headers={
                "authorization": f"Bearer {token}",
                "accept": "application/json, text/event-stream",
                "mcp-protocol-version": "2025-06-18",
            },
            trust_env=False,
            timeout=5,
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.runner.close()

    async def rpc(self, method, params=None, **kwargs):
        return await self.client.post(self.runner.url, json={
            "jsonrpc": "2.0", "id": 1, "method": method,
            "params": params or {},
        }, **kwargs)

    async def call(self, arguments, meta=None):
        params = {"name": "cron_manage", "arguments": arguments}
        if meta is not None:
            params["_meta"] = meta
        response = await self.rpc("tools/call", params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["result"]

    async def test_discovery_before_application_ready_and_management_admission(self):
        initialized = await self.rpc("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "schedule-test", "version": "1"},
        })
        result = initialized.json()["result"]
        self.assertIn("cron_manage", result["instructions"].splitlines()[0])
        tools = (await self.rpc("tools/list")).json()["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], ["cron_manage"])
        self.assertFalse(tools[0]["annotations"]["readOnlyHint"])
        schema = tools[0]["inputSchema"]
        self.assertNotIn("thread_id", schema["properties"])
        self.assertEqual(schema["properties"]["mode"]["enum"], ["options", "list", "view", "create", "update", "delete", "runs"])
        result = await self.call({"mode": "list"})
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"]["code"], "unavailable")
        self.assertEqual(self.requests, [])
        self.runner.open_admission()
        self.assertFalse((await self.call({"mode": "list", "all": True}))["isError"])
        self.runner.close_admission()
        self.assertTrue((await self.call({"mode": "list"}))["isError"])

    async def test_metadata_is_per_call_under_concurrency(self):
        self.runner.open_admission()
        results = await asyncio.gather(*(
            self.call({"mode": "list"}, {
                "threadId": thread_id,
                "x-codex-turn-metadata": json.dumps({"thread_id": thread_id, "turn_id": "turn"}),
            })
            for thread_id in ("thread-a", "thread-b")
        ))
        self.assertEqual({result["structuredContent"]["thread_id"] for result in results}, {"thread-a", "thread-b"})
        self.assertEqual({thread for _, thread in self.requests}, {"thread-a", "thread-b"})

    async def test_explicit_parameters_work_without_binding_metadata(self):
        self.runner.open_admission()
        request = {"mode": "create", "name": "daily", "instructions": "Check the service.",
                   "schedule": {"kind": "daily", "at": "09:00"}, "timezone": "Asia/Shanghai",
                   "chat_id": "oc_new", "project": "demo", "request_id": "request-1"}
        await self.call(request)
        self.assertEqual(self.requests, [(request, None)])

    async def test_conflicting_or_malformed_metadata_never_reaches_service(self):
        self.runner.open_admission()
        for meta in (
            {"threadId": "a", "x-codex-turn-metadata": {"thread_id": "b"}},
            {"threadId": "a", "x-codex-turn-metadata": "{"},
            {"threadId": []},
        ):
            result = await self.call({"mode": "list", "all": True}, meta)
            self.assertEqual(result["structuredContent"]["error"]["code"], "invalid_call_context")
        self.assertEqual(self.requests, [])

    async def test_argument_identity_and_missing_mutation_fields_rejected(self):
        self.runner.open_admission()
        for request in (
            {"mode": "list", "thread_id": "forged"},
            {"mode": "create", "name": "missing fields"},
            {"mode": "update", "plan_id": "p", "request_id": "r"},
            {"mode": "delete", "plan_id": "p", "request_id": "r", "expected_revision": True},
            {"mode": "unknown"},
        ):
            result = await self.call(request)
            self.assertEqual(result["structuredContent"]["error"]["code"], "invalid_request")
        self.assertEqual(self.requests, [])

    async def test_real_misspelled_create_gets_field_corrections_then_succeeds(self):
        self.runner.open_admission()
        wrong = {"action": "create", "request_id": "stable-request", "name": "Verification",
                 "instruction": "Reply verification completed.",
                 "schedule": {"type": "once", "at": "2030-01-02T09:00+08:00", "timezone": "Asia/Shanghai"}}
        result = await self.call(wrong, {"threadId": "private-chat-thread"})
        error = result["structuredContent"]["error"]
        self.assertEqual(error["code"], "invalid_request")
        fields = {item["field"] for item in error["fields"]}
        self.assertTrue({"mode", "action", "instruction", "schedule.kind", "schedule.type"} <= fields)
        self.assertEqual(self.requests, [])
        corrected = {"mode": "create", "request_id": wrong["request_id"], "name": wrong["name"],
                     "instructions": wrong["instruction"],
                     "schedule": {"kind": "once", "at": wrong["schedule"]["at"], "timezone": "Asia/Shanghai"}}
        result = await self.call(corrected, {"threadId": "private-chat-thread"})
        self.assertFalse(result["isError"])
        self.assertEqual(self.requests, [(corrected, "private-chat-thread")])

    async def test_partial_session_settings_preserve_explicit_null_and_options_identity(self):
        self.runner.open_admission()
        for settings in ({}, {"turn_settings": None}, {"progress_card_enabled": False}, {
            "turn_settings": {"model_id": "model-a", "effort_id": "low", "service_tier_id": "default"},
            "reaction_pulse_enabled": True, "message_context_mode": "catch-up",
        }):
            request = {"mode": "update", "plan_id": "plan-one", "expected_revision": 3,
                "request_id": "settings-request", "session_settings": settings}
            result = await self.call(request, {"threadId": "exact-settings-thread"})
            self.assertFalse(result["isError"])
            self.assertEqual(self.requests[-1], (request, "exact-settings-thread"))
        request = {"mode": "options", "chat_id": "explicit-target"}
        result = await self.call(request, {"threadId": "exact-settings-thread"})
        self.assertFalse(result["isError"])
        self.assertEqual(self.requests[-1], (request, "exact-settings-thread"))

    async def test_recurring_cutoff_reaches_service_and_replacement_null_clears_it(self):
        self.runner.open_admission()
        rule = {"kind": "daily", "at": "09:00", "end_at": "2030-01-31T23:59+08:00"}
        create = {**CREATE_EXAMPLE, "schedule": rule}
        self.assertFalse((await self.call(create, {"threadId": "exact-limit-thread"}))["isError"])
        self.assertEqual(self.requests[-1], (create, "exact-limit-thread"))
        update = {"mode": "update", "plan_id": "plan-one", "expected_revision": 3, "request_id": "limit-update"}
        for replacement in (None, {"kind": "daily", "at": "10:00"}, {
            "kind": "daily", "at": "10:00", "end_at": None,
        }):
            request = {**update, **({"schedule": replacement} if replacement is not None else {"enabled": False})}
            self.assertFalse((await self.call(request, {"threadId": "exact-limit-thread"}))["isError"])
            expected = {**request}
            if replacement is not None:
                expected["schedule"] = {key: value for key, value in replacement.items() if value is not None}
            self.assertEqual(self.requests[-1], (expected, "exact-limit-thread"))

    async def test_once_cutoff_gets_safe_field_correction_then_recurring_succeeds(self):
        self.runner.open_admission()
        request = {**CREATE_EXAMPLE, "schedule": {
            "kind": "once", "at": "2030-01-02T09:00+08:00", "end_at": "SECRET_CUTOFF",
        }}
        result = await self.call(request)
        error = result["structuredContent"]["error"]
        self.assertEqual(error["code"], "invalid_request")
        self.assertIn("schedule.end_at", {item["field"] for item in error["fields"]})
        self.assertNotIn("SECRET_CUTOFF", json.dumps(result))
        self.assertEqual(self.requests, [])
        corrected = {**request, "schedule": {
            "kind": "daily", "at": "09:00", "end_at": "2030-01-31T23:59+08:00",
        }}
        self.assertFalse((await self.call(corrected))["isError"])
        self.assertEqual(self.requests, [(corrected, None)])

    async def test_session_setting_errors_are_actionable_without_echoing_values(self):
        self.runner.open_admission()
        result = await self.call({**CREATE_EXAMPLE, "session_settings": {
            "turn_settings": {"model_id": "SECRET_MODEL_VALUE"},
            "progress_card_enabled": "SECRET_BOOLEAN_VALUE",
            "SECRET_UNKNOWN_FIELD": "SECRET_UNKNOWN_VALUE",
        }})
        error = result["structuredContent"]["error"]
        self.assertEqual(error["code"], "invalid_request")
        self.assertNotIn("SECRET_", json.dumps(error))
        fields = {item["field"] for item in error["fields"]}
        self.assertTrue({"session_settings.turn_settings.effort_id", "session_settings.turn_settings.service_tier_id",
                         "session_settings.progress_card_enabled", "session_settings"} <= fields)
        self.assertEqual(self.requests, [])

    async def test_validation_hints_identify_missing_fields_without_echoing_secrets(self):
        self.runner.open_admission()
        for arguments, expected in (
            ({"mode": "create", "name": "SECRET_VALUE"}, {"instructions", "schedule", "request_id"}),
            ({"mode": "create", "name": "SECRET_VALUE", "instructions": "SECRET_BODY", "request_id": "r",
              "schedule": {"kind": "weekly", "at": "09:00"}}, {"schedule.weekdays"}),
            ({"mode": "SECRET_MODE", "SECRET_FIELD": "SECRET_VALUE",
              "schedule": {"kind": "SECRET_KIND", "SECRET_NESTED": "SECRET_NESTED_VALUE"}}, {"mode", "schedule.kind"}),
        ):
            with self.subTest(expected=expected):
                result = await self.call(arguments)
                error = result["structuredContent"]["error"]
                self.assertEqual(error["code"], "invalid_request")
                self.assertTrue(expected <= {item["field"] for item in error["fields"]})
                self.assertNotIn("SECRET", json.dumps(result))
        self.assertEqual(self.requests, [])

    async def test_auth_origin_and_host(self):
        for headers, status in (
            ({"authorization": "Bearer wrong"}, 401),
            ({"origin": "http://127.0.0.1"}, 403),
            ({"host": "evil.example"}, 421),
        ):
            response = await self.rpc("tools/list", headers=headers)
            self.assertEqual(response.status_code, status, response.text)
        response = await self.rpc("tools/list", headers=[
            ("authorization", self.client.headers["authorization"]),
            ("authorization", self.client.headers["authorization"]),
        ])
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.requests, [])

    async def test_body_size_limit_applies_to_declared_and_chunked_bodies(self):
        body = b" " * (MAX_BODY_BYTES + 1)
        response = await self.client.post(self.runner.url, content=body, headers={"content-type": "application/json"})
        self.assertEqual(response.status_code, 413)

        async def chunks():
            yield body[:MAX_BODY_BYTES]
            yield body[MAX_BODY_BYTES:]

        response = await self.client.post(self.runner.url, content=chunks(), headers={"content-type": "application/json"})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.requests, [])

    async def test_slow_request_body_times_out(self):
        async def chunks():
            yield b"{"
            await asyncio.sleep(0.05)
            yield b"}"

        with patch("netizen.schedules.mcp.HTTP_TIMEOUT_SECONDS", 0.01):
            response = await self.client.post(self.runner.url, content=chunks(), headers={"content-type": "application/json"})
        self.assertEqual(response.status_code, 408)

    async def test_callback_timeout_is_stable_and_drain_is_bounded(self):
        started = asyncio.Event()
        finish = asyncio.Event()

        async def manage(request, native_thread_id):
            started.set()
            await finish.wait()
            return {"ok": True}

        self.runner._callback = manage
        self.runner.open_admission()
        with patch("netizen.schedules.mcp.CALL_TIMEOUT_SECONDS", 0.05):
            pending = asyncio.create_task(self.call({"mode": "list"}))
            await started.wait()
            self.runner.close_admission()
            self.assertFalse(await self.runner.drain(asyncio.get_running_loop().time()))
            result = await pending
        self.assertEqual(result["structuredContent"]["error"]["code"], "timeout")
        self.assertTrue(await self.runner.drain(asyncio.get_running_loop().time() + 1))

    async def test_callback_error_never_exposes_input_or_exception(self):
        async def fail(request, native_thread_id):
            raise ValueError("SECRET INSTRUCTIONS")

        self.runner._callback = fail
        self.runner.open_admission()
        with self.assertLogs("netizen.schedules.mcp", level="ERROR") as captured:
            result = await self.call({"mode": "list"})
        self.assertEqual(result["structuredContent"]["error"]["code"], "internal_error")
        self.assertNotIn("SECRET", json.dumps(result) + str(captured.output))

    async def test_process_config_is_one_owned_entry_without_credential_value(self):
        override, = self.runner.config_overrides
        document = tomllib.loads(override)
        entries = document["mcp_servers"]
        self.assertEqual(list(entries), [self.runner.namespace])
        entry = entries[self.runner.namespace]
        self.assertTrue(self.runner.url.startswith("http://127.0.0.1:"))
        self.assertEqual(entry["url"], self.runner.url)
        self.assertEqual(entry["enabled_tools"], ["cron_manage"])
        env_key, = self.runner.app_server_env
        self.assertEqual(entry["bearer_token_env_var"], env_key)
        self.assertNotIn(self.runner.app_server_env[env_key], override)
        other = ScheduleMcpRunner()
        self.assertNotEqual(other.namespace, self.runner.namespace)
        self.assertNotEqual(other.app_server_env, self.runner.app_server_env)

    async def test_close_revokes_endpoint_and_is_idempotent(self):
        await self.runner.close()
        await self.runner.close()
        with self.assertRaises(httpx.TransportError):
            await self.rpc("tools/list")
        with self.assertRaises(RuntimeError):
            self.runner.open_admission()


class ScheduleMcpSchemaTests(unittest.TestCase):
    def test_recurring_cutoff_accepts_set_omitted_and_clear_shapes(self):
        validator = Draft202012Validator(_tool_schema())
        for rule in (
            {"kind": "daily", "at": "09:00"},
            {"kind": "weekly", "at": "09:00", "weekdays": [0, 6]},
            {"kind": "interval", "every_minutes": 15},
        ):
            for cutoff in ({}, {"end_at": None}, {"end_at": "2030-01-31T23:59+08:00"}):
                with self.subTest(kind=rule["kind"], cutoff=cutoff):
                    request = {**CREATE_EXAMPLE, "schedule": {**rule, **cutoff}}
                    validator.validate(request)
                    self.assertEqual(_Arguments.model_validate(request).model_dump(exclude_unset=True), request)

    def test_recurring_cutoff_rejects_invalid_types_and_once_usage(self):
        validator = Draft202012Validator(_tool_schema())
        invalid = [{"kind": "daily", "at": "09:00", "end_at": value} for value in ("", 123, True)]
        invalid.append({"kind": "once", "at": "2030-01-02T09:00+08:00", "end_at": "2030-01-31T23:59+08:00"})
        for rule in invalid:
            with self.subTest(rule=rule):
                request = {**CREATE_EXAMPLE, "schedule": rule}
                self.assertTrue(list(validator.iter_errors(request)))
                with self.assertRaises(ValueError):
                    _Arguments.model_validate(request)
        request = {**CREATE_EXAMPLE, "schedule": {
            "kind": "once", "at": "2030-01-02T09:00+08:00", "end_at": None,
        }}
        validator.validate(request)
        _Arguments.model_validate(request)

    def test_session_settings_schema_requires_strict_values_and_complete_model_triple(self):
        validator = Draft202012Validator(_tool_schema())
        for settings in ({}, {"turn_settings": None}, {"progress_card_enabled": False}, {
            "turn_settings": {"model_id": "model-a", "effort_id": "low", "service_tier_id": "default"},
            "message_context_mode": "catch-up",
        }):
            validator.validate({**CREATE_EXAMPLE, "session_settings": settings})
        for settings in (None, {"progress_card_enabled": None}, {"reaction_pulse_enabled": 1},
                         {"message_context_mode": "all-history"}, {"turn_settings": {"model_id": "model-a"}},
                         {"native_thread_id": "untrusted"}):
            request = {**CREATE_EXAMPLE, "session_settings": settings}
            with self.subTest(settings=settings):
                self.assertTrue(list(validator.iter_errors(request)))
                with self.assertRaises(ValueError):
                    _Arguments.model_validate(request)

    def test_paging_limit_accepts_service_bounds_and_rejects_invalid_sizes(self):
        validator = Draft202012Validator(_tool_schema())
        for request in ({"mode": "list"}, {"mode": "runs", "plan_id": "plan-one"}):
            for limit in (1, 20, 50):
                with self.subTest(request=request, limit=limit):
                    arguments = {**request, "limit": limit}
                    validator.validate(arguments)
                    self.assertEqual(_Arguments.model_validate(arguments).model_dump(exclude_none=True), arguments)
            for limit in (0, 51, True, "20", 1.5):
                with self.subTest(request=request, limit=limit):
                    arguments = {**request, "limit": limit}
                    self.assertTrue(list(validator.iter_errors(arguments)))
                    with self.assertRaises(ValueError):
                        _Arguments.model_validate(arguments)
            self.assertEqual(_Arguments.model_validate({**request, "limit": None}).model_dump(exclude_none=True), request)

    def test_schedule_schema_and_parser_accept_shared_rule_shapes_and_valid_example(self):
        validator = Draft202012Validator(_tool_schema())
        validator.validate(CREATE_EXAMPLE)
        for schedule in (
            {"kind": "once", "at": "2030-01-02T09:00+08:00"},
            {"kind": "daily", "at": "09:00"},
            {"kind": "weekly", "at": "09:00", "weekdays": [0, 6]},
            {"kind": "interval", "every_minutes": 15},
        ):
            request = {**CREATE_EXAMPLE, "schedule": schedule}
            validator.validate(request)
            self.assertEqual(_Arguments.model_validate(request).model_dump(exclude_none=True)["schedule"], schedule)
            ScheduleRule.from_dict({**schedule, "timezone": "UTC", **({"anchor": 0} if schedule["kind"] == "interval" else {})})

    def test_schedule_schema_rejects_misspelled_and_missing_or_mistyped_rule_fields(self):
        validator = Draft202012Validator(_tool_schema())
        for schedule in (
            {"type": "once", "at": "2030-01-02T09:00+08:00"},
            {"kind": "once"},
            {"kind": "daily", "at": None},
            {"kind": "weekly", "at": "09:00", "weekdays": []},
            {"kind": "weekly", "at": "09:00", "weekdays": [7]},
            {"kind": "weekly", "at": "09:00", "weekdays": [True]},
            {"kind": "interval", "every_minutes": 0},
            {"kind": "daily", "at": "09:00", "max_runs": 5},
        ):
            with self.subTest(schedule=schedule):
                request = {**CREATE_EXAMPLE, "schedule": schedule}
                self.assertTrue(list(validator.iter_errors(request)))
                with self.assertRaises(ValueError):
                    _Arguments.model_validate(request)


class ScheduleMcpCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_outer_close_deadline_cancels_active_management_before_returning(self):
        started = asyncio.Event()
        settled = asyncio.Event()

        async def manage(request, thread_id):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                settled.set()

        runner = ScheduleMcpRunner()
        runner.attach(manage)
        runner._loop = asyncio.get_running_loop()
        runner._server_task = asyncio.create_task(asyncio.Event().wait())
        runner.open_admission()
        call = asyncio.create_task(runner._call_tool(
            SimpleNamespace(meta=None),
            CallToolRequestParams(name="cron_manage", arguments={"mode": "list"}),
        ))
        await started.wait()
        closing = asyncio.create_task(runner.close())
        await asyncio.sleep(0)
        closing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await closing
        self.assertTrue(settled.is_set())
        self.assertTrue(call.cancelled())
        self.assertTrue(runner._server_task.cancelled())
        self.assertEqual(runner._calls, set())
        await runner.close()


if __name__ == "__main__":
    unittest.main()
