from __future__ import annotations

import asyncio
import copy
import json
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jsonschema import Draft202012Validator
from mcp.types import CallToolRequestParams

from netizen_cli.bindings import BindingStore
from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.runtime.contracts import NativeThreadCatalog, NativeThreadMetadata
from netizen_cli.sessions.errors import SessionToolError
from netizen_cli.sessions.mcp import SessionMcpRunner
from netizen_cli.sessions.policy import SessionAccessPolicy, SessionAccessRule
from netizen_cli.sessions.presentation import fit_result, result_bytes
from netizen_cli.sessions.schema import TOOL_ARGUMENTS, validate_arguments
from netizen_cli.sessions.service import SessionService


class Policy:
    revision = "one"
    discovery = None
    read_send = None

    def snapshot(self):
        def rule(chats):
            return SessionAccessRule() if chats is None else SessionAccessRule("only", tuple(chats))
        return SessionAccessPolicy(rule(self.discovery), rule(self.read_send)), self.revision


class Catalog:
    def __init__(self):
        self.items = {}
        self.archived = set()
        self.reads = []
        self.read_hook = None

    async def thread_catalog(self, *, archived, deadline):
        return NativeThreadCatalog(archived, tuple(
            NativeThreadMetadata(thread_id, item.get("name"), item["preview"], item["updatedAt"])
            for thread_id, item in self.items.items() if (thread_id in self.archived) == archived
        ))

    def handle(self, codex, thread_id):
        async def read(*, include_turns):
            self.reads.append((thread_id, include_turns))
            if self.read_hook:
                await self.read_hook(thread_id)
            thread = copy.deepcopy(self.items[thread_id])
            if not include_turns:
                thread["turns"] = []
            return SimpleNamespace(thread=thread)
        return SimpleNamespace(read=read)


def thread(thread_id, title="A task", *, updated=10, status="idle", turns=None):
    return {"id": thread_id, "name": title, "preview": "PRIVATE BODY", "cwd": "/project",
            "projectId": "native-project", "createdAt": 1, "updatedAt": updated,
            "status": {"type": status}, "turns": turns or []}


def turn(turn_id, text, *, status="completed"):
    return {"id": turn_id, "status": status, "items": [
        {"id": "answer-" + turn_id, "type": "agentMessage", "text": text, "phase": "final_answer"},
    ]}


class SessionToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = BindingStore()
        self.store.register_project(alias="p", cwd=self.directory.name)
        self.catalog = Catalog()
        self.policy = Policy()
        self.calls = []
        async def create(**kwargs):
            self.calls.append(("create", kwargs))
            return {"threadId": "created", "netizen": {"accepted": True}}
        async def send(**kwargs):
            self.calls.append(("send", kwargs))
            return {"threadId": kwargs["target_thread_id"], "netizen": {"accepted": True}}
        self.service = SessionService(bindings=self.store, runtime=self.catalog, codex=object(), app_id="app",
                                      policy=self.policy, create_thread=create, send_message=send)
        self.source = self.binding("chat-a", "source")
        self.target = self.binding("chat-b", "target")
        self.patch = patch("netizen_cli.sessions.service.AsyncThread", self.catalog.handle)
        self.patch.start()

    async def asyncTearDown(self):
        self.patch.stop()
        self.store.close()
        self.directory.cleanup()

    def binding(self, chat_id, thread_id=None, *, app_id="app", title="A task", updated=10):
        binding = self.store.create_channel_binding(scope=FeishuScope(app_id, chat_id, ScopeKind.GROUP),
                                                    project_alias="p", creator_id="person")
        if thread_id:
            self.store.assign_native_thread_id(binding.id, thread_id)
            self.catalog.items[thread_id] = thread(thread_id, title, updated=updated)
        return self.store.get(binding.id)

    async def call(self, name, data=None, source="source"):
        return await self.service.call(name, data or {}, source)

    async def test_topic_metadata_reuses_native_feishu_thread_link(self):
        binding = self.store.create_channel_binding(scope=FeishuScope("app", "chat-topic", ScopeKind.TOPIC, "omt_topic"),
                                                    project_alias="p", creator_id="person")
        self.store.assign_native_thread_id(binding.id, "topic-native")
        self.catalog.items["topic-native"] = thread("topic-native")
        result = await self.call("read_thread", {"threadId": "topic-native"})
        self.assertIn("/client/thread/open?", result["netizen"]["topicUrl"])
        self.assertIn("open_thread_id=omt_topic", result["netizen"]["topicUrl"])
        self.assertNotIn("messageId", result["netizen"]["topicUrl"])

    async def test_only_registered_ordinary_instance_sources_and_targets(self):
        external = self.binding("chat-c", "other-app", app_id="another")
        for source in (None, "unregistered", external.native_thread_id):
            result = await self.call("list_threads", source=source)
            self.assertEqual(result["error"]["code"], "invalid_call_context")
        for target in ("unregistered", external.native_thread_id, self.target.id):
            result = await self.call("read_thread", {"threadId": target})
            self.assertEqual(result["error"]["code"], "target_not_registered")
        self.assertEqual(self.catalog.reads, [])

    async def test_discovery_and_read_send_are_independent(self):
        self.policy.discovery = {"chat-b"}
        self.policy.read_send = set()
        listing = await self.call("list_threads")
        self.assertEqual([item["id"] for item in listing["threads"]], ["target"])
        self.assertNotIn("summary", listing["threads"][0])
        self.assertNotIn("PRIVATE BODY", json.dumps(listing))
        denied = await self.call("read_thread", {"threadId": "target"})
        self.assertEqual(denied["error"]["code"], "access_denied")
        self.policy.discovery = set()
        self.policy.read_send = {"chat-b"}
        self.assertEqual((await self.call("list_threads"))["threads"], [])
        self.assertEqual((await self.call("read_thread", {"threadId": "target"}))["thread"]["id"], "target")
        sent = await self.call("send_message_to_thread", {"threadId": "target", "prompt": "continue"})
        self.assertTrue(sent["netizen"]["accepted"])
        self.assertEqual(self.calls[0][1]["source_thread_id"], "source")
        self.assertNotIn("authorize", self.calls[0][1])

    async def test_discovery_keeps_admitted_rules_and_cursor_during_policy_change(self):
        self.policy.discovery = {"chat-b"}
        self.policy.read_send = {"chat-b"}
        self.binding("chat-b", "third", updated=20)
        original = self.catalog.thread_catalog
        async def revoke(**kwargs):
            self.policy.discovery = set()
            self.policy.read_send = set()
            self.policy.revision = "two"
            return await original(**kwargs)
        self.catalog.thread_catalog = revoke
        admitted = await self.call("list_threads", {"limit": 1})
        self.assertEqual([item["id"] for item in admitted["threads"]], ["third"])
        self.assertEqual(admitted["threads"][0]["summary"], "PRIVATE BODY")
        self.assertIsNotNone(admitted["nextCursor"])
        self.assertEqual((await self.call("list_threads"))["threads"], [])
        stale = await self.call("list_threads", {"cursor": admitted["nextCursor"]})
        self.assertEqual(stale["error"]["code"], "invalid_cursor")

    async def test_discovery_does_not_gain_preview_permission_mid_call(self):
        self.policy.discovery = {"chat-b"}
        self.policy.read_send = set()
        original = self.catalog.thread_catalog
        async def grant(**kwargs):
            self.policy.read_send = {"chat-b"}
            self.policy.revision = "two"
            return await original(**kwargs)
        self.catalog.thread_catalog = grant
        admitted = await self.call("list_threads")
        self.assertNotIn("summary", admitted["threads"][0])
        self.assertEqual((await self.call("list_threads"))["threads"][0]["summary"], "PRIVATE BODY")

    async def test_global_search_pagination_filters_before_limiting(self):
        self.policy.discovery = {"selected"}
        expected = set()
        for index in range(62):
            identifier = f"thread-{index}"
            self.binding("selected", identifier, title=f"searchable {index}", updated=100 + index)
            expected.add(identifier)
        self.catalog.items["external"] = thread("external", "searchable external", updated=9999)
        found, cursor = set(), None
        while True:
            args = {"searchTerm": "searchable", "limit": 10, **({"cursor": cursor} if cursor else {})}
            value = await self.call("list_threads", args)
            self.assertNotIn("error", value)
            ids = {item["id"] for item in value["threads"]}
            self.assertFalse(found & ids)
            found.update(ids)
            cursor = value["nextCursor"]
            if not cursor:
                break
        self.assertEqual(found, expected)
        self.assertTrue(all(not include_turns for _, include_turns in self.catalog.reads))
        first = await self.call("list_threads", {"limit": 1})
        cursor = first["nextCursor"]
        self.policy.revision = "changed"
        stale = await self.call("list_threads", {"cursor": cursor})
        self.assertEqual(stale["error"]["code"], "invalid_cursor")

    async def test_archived_pending_and_background_are_not_activated(self):
        pending = self.binding("chat-pending")
        self.catalog.archived.add("target")
        listing = await self.call("list_archived_threads")
        self.assertEqual([item["id"] for item in listing["threads"]], ["target"])
        active = await self.call("list_threads")
        lazy = next(item for item in active["threads"] if item["netizen"]["bindingId"] == pending.id)
        self.assertIsNone(lazy["id"])
        self.assertEqual(lazy["status"], "pending")
        self.assertNotIn("target", [item["id"] for item in active["threads"]])
        replacement = self.binding("chat-b", "replacement")
        read = await self.call("read_thread", {"threadId": "target"})
        self.assertFalse(read["netizen"]["isCurrent"])
        sent = await self.call("send_message_to_thread", {"threadId": "target", "prompt": "continue"})
        self.assertEqual(sent["error"]["code"], "target_not_current")
        self.assertFalse(sent["netizen"]["accepted"])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.active_binding(self.target.scope_key).id, replacement.id)
        self.assertIsNone(self.store.get(pending.id).native_thread_id)

    async def test_read_pages_history_and_limits_tool_outputs(self):
        turns = [turn(f"turn-{index}", f"answer {index}") for index in range(3)]
        turns[-1]["items"].insert(0, {"id": "command", "type": "commandExecution", "command": "pwd", "aggregatedOutput": "secret output", "status": "completed"})
        self.catalog.items["target"]["turns"] = turns
        first = await self.call("read_thread", {"threadId": "target"})
        self.assertEqual(first["turns"][0]["id"], "turn-2")
        self.assertEqual(first["page"]["nextCursor"], "turn-2")
        self.assertNotIn("secret output", json.dumps(first))
        second = await self.call("read_thread", {"threadId": "target", "cursor": first["page"]["nextCursor"], "turnLimit": 2})
        self.assertEqual([item["id"] for item in second["turns"]], ["turn-1", "turn-0"])
        self.assertFalse(second["page"]["hasMore"])
        output = await self.call("read_thread", {"threadId": "target", "includeOutputs": True, "maxOutputCharsPerItem": 3})
        self.assertEqual(output["turns"][0]["items"][0]["output"], {"text": "sec", "truncated": True, "originalChars": 13})

    async def test_permission_change_during_native_read_applies_to_next_call(self):
        async def revoke(_thread_id):
            self.policy.read_send = set()
        self.catalog.read_hook = revoke
        result = await self.call("read_thread", {"threadId": "target"})
        self.assertEqual(result["thread"]["preview"], "PRIVATE BODY")
        denied = await self.call("read_thread", {"threadId": "target"})
        self.assertEqual(denied["error"]["code"], "access_denied")
        self.assertEqual(self.catalog.reads, [("target", True)])

    async def test_read_still_revalidates_removed_target_after_native_read(self):
        async def remove(_thread_id):
            self.store.delete_binding(self.target.id)
        self.catalog.read_hook = remove
        result = await self.call("read_thread", {"threadId": "target"})
        self.assertEqual(result["error"]["code"], "target_not_registered")
        self.assertNotIn("PRIVATE BODY", json.dumps(result))

    async def test_wait_snapshots_changes_and_self_duplicate_validation(self):
        self.catalog.items["target"]["turns"] = [turn("turn-1", "the result")]
        first = await self.call("wait_threads", {"targets": [{"threadId": "target"}], "timeoutMs": 0})
        self.assertEqual(first["wake"]["reason"], "turnCompleted")
        poll = first["polls"][0]
        self.assertEqual(poll["latestAssistantMessage"]["text"], "the result")
        second = await self.call("wait_threads", {"targets": [{"threadId": "target", "afterCursor": poll["cursor"]}], "timeoutMs": 0})
        self.assertTrue(second["timedOut"])
        self.assertFalse(second["polls"][0]["changed"])
        self.assertIsNone(second["polls"][0]["latestAssistantMessage"])
        for targets in ([{"threadId": "source"}], [{"threadId": "target"}, {"threadId": "target"}]):
            self.assertEqual((await self.call("wait_threads", {"targets": targets}))["error"]["code"], "invalid_targets")
        bad = await self.call("wait_threads", {"targets": [{"threadId": "target", "afterCursor": "turn-1"}], "timeoutMs": 0})
        self.assertEqual(bad["errors"][0]["code"], "invalid_cursor")

    async def test_wait_observes_completion_without_delivering_input(self):
        self.catalog.items["target"]["status"] = {"type": "active", "activeFlags": []}
        count = 0
        async def complete(identifier):
            nonlocal count
            count += 1
            if count == 2:
                self.catalog.items[identifier]["status"] = {"type": "idle"}
                self.catalog.items[identifier]["turns"] = [turn("completed-turn", "finished")]
        self.catalog.read_hook = complete
        result = await self.call("wait_threads", {"targets": [{"threadId": "target"}], "timeoutMs": 5000})
        self.assertEqual(result["wake"], {"threadId": "target", "reason": "turnCompleted", "turnId": "completed-turn"})
        self.assertEqual(self.calls, [])
        self.assertEqual(count, 2)

    async def test_wait_keeps_admitted_targets_when_policy_changes_mid_call(self):
        self.catalog.items["target"]["status"] = {"type": "active", "activeFlags": []}
        other = self.binding("chat-c", "third")
        async def revoke(identifier):
            if identifier == other.native_thread_id:
                self.policy.read_send = {"chat-c"}
        self.catalog.read_hook = revoke
        result = await self.call("wait_threads", {"targets": [{"threadId": "target"}, {"threadId": "third"}], "timeoutMs": 0})
        self.assertEqual([poll["thread"]["id"] for poll in result["polls"]], ["target", "third"])
        self.assertNotIn("errors", result)
        denied = await self.call("wait_threads", {"targets": [{"threadId": "target"}], "timeoutMs": 0})
        self.assertEqual(denied["error"]["code"], "access_denied")
        self.assertEqual(self.calls, [])

    async def test_wait_drops_target_removed_while_reading_another_target(self):
        self.catalog.items["target"]["status"] = {"type": "active", "activeFlags": []}
        self.binding("chat-c", "third")
        async def remove(identifier):
            if identifier == "third":
                self.store.delete_binding(self.target.id)
        self.catalog.read_hook = remove
        result = await self.call("wait_threads", {"targets": [{"threadId": "target"}, {"threadId": "third"}], "timeoutMs": 0})
        self.assertEqual([poll["thread"]["id"] for poll in result["polls"]], ["third"])
        self.assertEqual(result["errors"][0]["code"], "target_not_registered")

    async def test_create_permission_and_partial_error_receipt(self):
        self.policy.discovery = set()
        result = await self.call("create_thread", {"prompt": "investigate", "model": "model-a"})
        self.assertEqual(result["threadId"], "created")
        self.assertEqual(self.calls[-1][1]["model"], "model-a")
        self.assertNotIn("authorize", self.calls[-1][1])
        self.policy.read_send = set()
        result = await self.call("create_thread", {"prompt": "investigate"})
        self.assertEqual(result["error"]["code"], "access_denied")
        self.assertEqual(len(self.calls), 1)
        self.policy.read_send = None
        async def failed(**kwargs):
            raise SessionToolError("input_unknown", "Inspect the created topic.", {"threadId": "already-created", "netizen": {"accepted": None}})
        self.service._create = failed
        partial = await self.call("create_thread", {"prompt": "investigate"})
        self.assertEqual(partial["threadId"], "already-created")
        self.assertIsNone(partial["netizen"]["accepted"])

    async def test_mutation_admission_is_not_revoked_while_callback_runs(self):
        for tool, arguments, callback_name in (
            ("create_thread", {"prompt": "investigate"}, "_create"),
            ("send_message_to_thread", {"threadId": "target", "prompt": "continue"}, "_send"),
        ):
            with self.subTest(tool=tool):
                self.policy.read_send = None
                calls = []
                async def admitted(**kwargs):
                    calls.append(kwargs)
                    self.policy.read_send = set()
                    await asyncio.sleep(0)
                    return {"threadId": "created" if tool == "create_thread" else "target", "netizen": {"accepted": True}}
                setattr(self.service, callback_name, admitted)
                self.assertTrue((await self.call(tool, arguments))["netizen"]["accepted"])
                self.assertEqual((await self.call(tool, arguments))["error"]["code"], "access_denied")
                self.assertEqual(len(calls), 1)

    async def test_default_history_retains_only_recognized_netizen_delegation(self):
        envelope = json.dumps({"sourceThreadId": "source", "sourceBindingId": self.source.id,
                               "message": "核对迁移安全性 <input>"}, ensure_ascii=False)
        messages = [
            {"id": name, "type": "functionCallOutput", "name": name,
             "namespace": "netizen_sessions", "output": envelope}
            for name in ("create_thread", "send_message_to_thread")
        ]
        unrelated = [
            {**messages[0], "id": "other-namespace", "namespace": "another_server"},
            {**messages[0], "id": "other-tool", "name": "read_thread"},
            {**messages[0], "id": "plain-output", "output": "not an envelope"},
            {**messages[0], "id": "missing-source", "output": json.dumps({"message": "private output"})},
            {**messages[0], "id": "wrong-shape", "output": json.dumps({"sourceThreadId": ["source"], "sourceBindingId": self.source.id, "message": "private output"})},
        ]
        self.catalog.items["target"]["turns"] = [{"id": "turn", "status": "completed", "items": messages + unrelated}]
        default = await self.call("read_thread", {"threadId": "target"})
        items = default["turns"][0]["items"]
        for item in items[:2]:
            self.assertEqual(item["codexDelegation"], {"sourceThreadId": "source", "input": "核对迁移安全性 <input>"})
            self.assertEqual(item["type"], "functionCallOutput")
            self.assertNotIn("output", item)
        for item in items[2:]:
            self.assertNotIn("codexDelegation", item)
            self.assertNotIn("output", item)
        expanded = await self.call("read_thread", {"threadId": "target", "includeOutputs": True})
        self.assertEqual(expanded["turns"][0]["items"][0]["output"]["text"], envelope)
        self.assertEqual(expanded["turns"][0]["items"][2]["output"]["text"], envelope)

    async def test_delegation_summary_obeys_output_budget_without_shortening_source_id(self):
        source_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        self.catalog.items["target"]["turns"] = [{"id": "turn", "status": "completed", "items": [{
            "id": "delegation", "type": "functionCallOutput", "name": "create_thread", "namespace": "netizen_sessions",
            "output": json.dumps({"sourceThreadId": source_id, "sourceBindingId": self.source.id, "message": "中" * 10000}),
        }]}]
        result = await self.call("read_thread", {"threadId": "target"})
        summary = result["turns"][0]["items"][0]["codexDelegation"]
        self.assertEqual(summary["sourceThreadId"], source_id)
        self.assertTrue(summary["input"].endswith("…"))
        self.assertLessEqual(len(summary["input"]), 2000)
        self.assertLessEqual(result_bytes(result), 8000)

    async def test_output_budget_retains_native_identity_answer_and_cursor(self):
        self.catalog.items["target"]["turns"] = [turn("old", "old answer"), turn("new", "中文" * 20000)]
        value = await self.call("read_thread", {"threadId": "target", "turnLimit": 2})
        self.assertLessEqual(result_bytes(value), 8000)
        self.assertEqual(value["thread"]["id"], "target")
        self.assertEqual(value["page"]["nextCursor"], "new")
        self.assertEqual(value["turns"][0]["items"][0]["id"], "answer-new")
        self.assertTrue(value["truncated"])


    async def test_history_projection_does_not_include_opaque_command_actions(self):
        self.catalog.items["target"]["turns"] = [{"id": "turn", "status": "completed", "items": [
            {"id": "command", "type": "commandExecution", "command": "pwd", "status": "completed",
             "commandActions": [{"type": "unknown", "command": "x"}] * 1000},
        ]}]
        result = await self.call("read_thread", {"threadId": "target"})
        self.assertNotIn("commandActions", json.dumps(result))
        self.assertLessEqual(result_bytes(result), 8000)

    async def test_budget_preserves_latest_answer_before_trailing_tool(self):
        self.catalog.items["target"]["turns"] = [{"id": "turn", "status": "completed", "items": [
            {"id": "answer", "type": "agentMessage", "text": "A" * 10000, "phase": "commentary"},
            {"id": "command", "type": "commandExecution", "command": "pwd", "status": "completed"},
        ]}]
        result = await self.call("read_thread", {"threadId": "target"})
        self.assertEqual(result["turns"][0]["items"][0]["id"], "answer")
        self.assertTrue(result["turns"][0]["items"][0]["text"])
        self.assertLessEqual(result_bytes(result), 8000)

    async def test_oversized_result_preserves_accepted_mutation_receipt(self):
        result = fit_result({"threadId": "exact-thread", "turnId": "exact-turn", "netizen": {"accepted": True, "bindingId": "exact-binding"},
                             "diagnostics": {str(index): True for index in range(10000)}})
        self.assertLessEqual(result_bytes(result), 8000)
        self.assertEqual(result["threadId"], "exact-thread")
        self.assertEqual(result["turnId"], "exact-turn")
        self.assertTrue(result["netizen"]["accepted"])
        self.assertTrue(result["truncated"])

    async def test_budget_truncates_identity_arrays_without_shortening_ids(self):
        sender = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        receiver = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        self.catalog.items["target"]["turns"] = [{"id": "turn", "status": "completed", "items": [
            {"id": "call", "type": "collabAgentToolCall", "senderThreadId": sender,
             "receiverThreadIds": [receiver] * 1500, "tool": "spawn", "status": "completed"},
        ]}]
        result = await self.call("read_thread", {"threadId": "target"})
        item = result["turns"][0]["items"][0]
        self.assertEqual(item["senderThreadId"], sender)
        self.assertEqual(set(item["receiverThreadIds"]), {receiver})
        self.assertTrue(item["receiverThreadIdsTruncated"])
        self.assertLessEqual(result_bytes(result), 8000)


class SessionMcpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runner = SessionMcpRunner()
        self.calls = []
        async def callback(name, arguments, source):
            self.calls.append((name, arguments, source))
            await asyncio.sleep(0)
            return {"threadId": source}
        self.runner.attach(callback)
        self.runner._admission = True

    async def call(self, name, arguments, meta):
        return await self.runner._call_tool(SimpleNamespace(meta=meta), CallToolRequestParams(name=name, arguments=arguments))

    async def test_schemas_limits_names_and_utf8_validation(self):
        listed = await self.runner._list_tools(None, None)
        self.assertEqual({tool.name for tool in listed.tools}, set(TOOL_ARGUMENTS))
        for tool in listed.tools:
            Draft202012Validator.check_schema(tool.input_schema)
            self.assertFalse(tool.input_schema["additionalProperties"])
        for data in ({"prompt": "中" * 334}, {"prompt": " "}, {"prompt": "ok", "sourceThreadId": "spoof"}, {"prompt": "ok", "title": "x" * 121}):
            value = await self.call("create_thread", data, {"threadId": "source"})
            self.assertTrue(value.is_error)
        self.assertEqual(self.calls, [])
        self.assertEqual(validate_arguments("create_thread", {"prompt": "ok", "title": "x" * 120})["title"], "x" * 120)
        self.assertEqual(validate_arguments("create_thread", {"prompt": "ok", "title": "  title\n name "})["title"], "title name")
        with self.assertRaises(SessionToolError):
            validate_arguments("send_message_to_thread", {"prompt": "ok", "threadId": "target", "title": "extra"})

    async def test_trusted_metadata_is_per_call_and_required(self):
        for meta in (None, {}, {"x-codex-turn-metadata": '{"thread_id":"spoof"}'}, {"threadId": "a", "x-codex-turn-metadata": '{"thread_id":"b"}'}):
            self.assertTrue((await self.call("list_threads", {}, meta)).is_error)
        values = await asyncio.gather(*(self.call("list_threads", {}, {"threadId": identifier}) for identifier in ("a", "b")))
        self.assertEqual({value.structured_content["threadId"] for value in values}, {"a", "b"})
        self.assertEqual(self.runner._calls, set())

    async def test_config_preserves_native_approval_and_wait_timeout(self):
        self.runner._url = "http://127.0.0.1:12345/mcp"
        override, = self.runner.config_overrides
        config = tomllib.loads(override)["mcp_servers"][self.runner.namespace]
        self.assertEqual(config["default_tools_approval_mode"], "approve")
        self.assertEqual(config["tools"]["send_message_to_thread"]["approval_mode"], "prompt")
        self.assertEqual(config["tools"]["create_thread"]["approval_mode"], "prompt")
        self.assertGreater(config["tool_timeout_sec"], 120)
        self.assertEqual(set(config["enabled_tools"]), set(TOOL_ARGUMENTS))

    async def test_error_remains_a_tool_failure(self):
        async def fail(*args):
            return {"error": {"code": "target_not_current", "message": "Background"}, "threadId": "exact-target", "netizen": {"accepted": False}}
        self.runner._callback = fail
        result = await self.call("send_message_to_thread", {"threadId": "exact-target", "prompt": "continue"}, {"threadId": "source"})
        self.assertTrue(result.is_error)
        self.assertEqual(json.loads(result.content[0].text), result.structured_content)
        self.assertEqual(result.structured_content["threadId"], "exact-target")
