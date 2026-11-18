from __future__ import annotations

import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.schedules.models import ScheduleRule
from netizen_cli.schedules.outcome_mcp import OutcomeMcpRunner, TOOL
from tests.support.channel_fixtures import scheduled_channel_fixture
from tests.support.channel_results import goal_activity_snapshot, sent_result, turn_activity_snapshot


class OutcomeToolTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runner = OutcomeMcpRunner()
        await self.runner.bind()
        self.addAsyncCleanup(self.runner.close)
        self.callback = AsyncMock(return_value={"recorded": True})
        self.runner.attach(self.callback)
        self.runner.open_admission()
        self.meta = {"threadId": "parent", "x-codex-turn-metadata": {"thread_id": "parent", "turn_id": "turn"}}

    async def call(self, meta, **args):
        return await self.runner._call_tool(SimpleNamespace(meta=meta), SimpleNamespace(name=TOOL, arguments=args))

    async def test_exact_mount_tool_schema_and_receipt(self):
        self.assertEqual(self.runner.thread_config(), {"mcp_servers." + self.runner.namespace: {
            "url": self.runner.url, "bearer_token_env_var": next(iter(self.runner.app_server_env)),
            "enabled": True, "enabled_tools": [TOOL], "default_tools_approval_mode": "approve",
        }})
        self.assertFalse(hasattr(self.runner, "config_overrides"))
        tool, = (await self.runner._list_tools(None, None)).tools
        self.assertEqual(tool.name, TOOL)
        self.assertEqual(tool.input_schema, {"type": "object", "properties": {
            "outcome": {"type": "string", "enum": ["succeeded", "failed"]}}, "required": ["outcome"], "additionalProperties": False})
        meta = {**self.meta, "x-codex-turn-metadata": json.dumps(self.meta["x-codex-turn-metadata"])}
        result = await self.call(meta, outcome="succeeded")
        self.assertFalse(result.is_error)
        self.callback.assert_awaited_once_with(source_thread_id="parent", source_turn_id="turn", outcome="succeeded")

    async def test_invalid_arguments_metadata_and_closed_admission(self):
        for args in ({}, {"outcome": None}, {"outcome": []}, {"outcome": "unreported"},
                     {"outcome": "failed", "reason": "reason"}, {"outcome": "failed", "thread_id": "parent"}):
            with self.subTest(args=args):
                result = await self.call(self.meta, **args)
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content["error"]["code"], "invalid_call_context")
        for inner in (None, "{", [], {"thread_id": "parent"}, {"turn_id": "turn"},
                      {"thread_id": "other", "turn_id": "turn"}, {"thread_id": "parent", "turn_id": ["turn"]},
                      {"thread_id": "parent", "turn_id": "turn", "subagent_kind": "thread_spawn"},
                      {"thread_id": "parent", "turn_id": "turn", "subagent_kind": []}):
            with self.subTest(inner=inner):
                result = await self.call({"threadId": "parent", "x-codex-turn-metadata": inner}, outcome="failed")
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content["error"]["code"], "invalid_call_context")
        for outer in (None, [], {"x-codex-turn-metadata": self.meta["x-codex-turn-metadata"]}):
            self.assertTrue((await self.call(outer, outcome="failed")).is_error)
        self.runner.close_admission()
        self.assertEqual((await self.call(self.meta, outcome="failed")).structured_content["error"]["code"], "unavailable")
        self.callback.assert_not_awaited()

    async def test_uncertain_callback_is_not_repeated(self):
        self.callback.side_effect = TimeoutError()
        with self.assertLogs("netizen_cli.schedules.outcome_mcp", "WARNING"):
            result = await self.call(self.meta, outcome="failed")
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["error"]["code"], "outcome_unknown")
        self.callback.assert_awaited_once()


class OutcomeChannelTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        f = self.f = await self.enterAsyncContext(scheduled_channel_fixture())
        plan_id = f.manager_plan("review")
        claim = f.store.schedules.claim_due(plan_id, app_id="app", now=3700)
        f.channel.send_results.append(sent_result("om_root", chat_id="oc_group", thread_id="omt_fresh", root_id="om_root"))
        await f.app.dispatch_scheduled_run(claim)
        request, = f.submissions
        self.binding = request["binding"]
        self.run_id = claim.run.id
        self.thread_id = "native-" + self.run_id

    async def report(self, turn_id, outcome="succeeded", thread=None):
        return await self.f.app.report_run_outcome(
            source_thread_id=thread or self.thread_id, source_turn_id=turn_id, outcome=outcome)

    async def test_initial_followup_goal_and_deleted_plan_receipts(self):
        for turn_id, outcome in (("turn-initial", "failed"), ("followup", "succeeded")):
            self.f.runtime.turn_activity_values[self.binding.id] = turn_activity_snapshot(
                binding_id=self.binding.id, thread_id=self.thread_id, turn_id=turn_id)
            result = await self.report(turn_id, outcome)
            self.assertEqual(result["task"], {"plan_name": "review", "triggered_at": "1970-01-01T01:01:40+00:00"})
            self.assertEqual((result["recorded"], result["outcome"]), (True, outcome))
        self.assertEqual((await self.report("turn-initial"))["error"]["code"], "invalid_call_context")
        run = self.f.store.schedules.get_run(self.run_id)
        self.assertEqual(run.self_reported_outcome, "succeeded")
        self.f.store.schedules.delete(run.plan_id, expected_revision=1, request_id="delete")
        self.f.runtime.turn_activity_values.clear()
        self.f.runtime.goal_activity_values[self.binding.id] = replace(
            goal_activity_snapshot(binding_id=self.binding.id), thread_id=self.thread_id)
        result = await self.report("goal-turn-final", "failed")
        self.assertIsNone(result["task"]["plan_name"])
        self.assertEqual(self.f.store.schedules.get_run(self.run_id).self_reported_outcome, "failed")

    async def test_rejects_noncurrent_sources_missing_or_removed_runs(self):
        ordinary = self.f.store.create_channel_binding(scope=FeishuScope("app", "oc_other", ScopeKind.GROUP),
            project_alias="work", creator_id="owner")
        self.f.store.assign_native_thread_id(ordinary.id, "ordinary")
        existing = self.f.store.schedules.create(
            name="follow up", instructions="continue", app_id="app", chat_id="oc_other", project_alias="work",
            schedule=ScheduleRule("interval", "UTC", every_minutes=1, anchor=100), request_id="existing",
            now=100, target_kind="binding", target_binding_id=ordinary.id, session_settings=None, workspace=None)
        self.assertIsNotNone(self.f.store.schedules.claim_due(existing.plan_id, app_id="app", now=160))
        self.f.runtime.turn_activity_values[ordinary.id] = turn_activity_snapshot(
            binding_id=ordinary.id, thread_id="ordinary", turn_id="turn-initial")
        self.f.runtime.side_snapshots["side"] = SimpleNamespace(
            thread_id="side-thread", parent_binding_id=self.binding.id, turn_id="turn-initial")
        for source in ("side-thread", "fork", "other-client", "ordinary"):
            self.assertEqual((await self.report("turn-initial", thread=source))["error"]["code"], "not_scheduled_topic")
        self.assertEqual((await self.report("turn-initial"))["error"]["code"], "invalid_call_context")
        self.f.runtime.turn_activity_values[self.binding.id] = turn_activity_snapshot(
            binding_id=self.binding.id, thread_id=self.thread_id, turn_id="turn-initial")
        self.f.runtime.lifecycle_states[self.binding.id] = "archiving"
        self.assertEqual((await self.report("turn-initial"))["error"]["code"], "invalid_call_context")
        self.f.runtime.lifecycle_states.clear()
        with patch.object(self.f.store.schedules, "record_self_reported_outcome", return_value=False):
            self.assertEqual((await self.report("turn-initial"))["error"]["code"], "run_unavailable")
        self.f.store.schedules.set_run(self.run_id, binding_removed=True)
        self.assertEqual((await self.report("turn-initial"))["error"]["code"], "run_unavailable")
