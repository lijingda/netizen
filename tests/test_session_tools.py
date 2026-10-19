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
from netizen_cli.runtime.contracts import NativeThreadCatalogPage, NativeThreadMetadata
from netizen_cli.sessions.delegation import delegated_prompt
from netizen_cli.sessions.errors import SessionToolError
from netizen_cli.sessions.mcp import SessionMcpRunner
from netizen_cli.sessions.policy import SessionAccessPolicy, SessionAccessRule
from netizen_cli.sessions.presentation import fit_result, result_bytes, turn_summary
from netizen_cli.sessions.schema import TOOL_ARGUMENTS, validate_arguments
from netizen_cli.sessions.service import SessionService
from netizen_cli.worktrees.errors import WorktreeError


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
        self.history_reads = []
        self.catalog_reads = []
        self.read_hook = None

    async def list_turns(self, thread_id, *, cursor=None, limit=1, items_view="full"):
        self.history_reads.append(("turns", thread_id, cursor, limit, items_view))
        turns = list(reversed(copy.deepcopy(self.items[thread_id]["turns"])))
        start = int(cursor.removeprefix("native-page:")) if cursor else 0
        data = turns[start:start + limit]
        if items_view == "summary":
            for item in data:
                item["items"] = []
        return SimpleNamespace(data=data, next_cursor=f"native-page:{start + limit}" if start + limit < len(turns) else None)

    async def list_items(self, thread_id, turn_id, *, limit=20):
        self.history_reads.append(("items", thread_id, turn_id, limit))
        turn = next(turn for turn in self.items[thread_id]["turns"] if turn["id"] == turn_id)
        return SimpleNamespace(data=[SimpleNamespace(item=copy.deepcopy(item)) for item in reversed(turn["items"][-limit:])])

    async def thread_catalog_page(self, *, archived, cursor, limit, sort_key, sort_direction, deadline):
        self.catalog_reads.append((archived, cursor, limit))
        items = sorted((
            NativeThreadMetadata(thread_id, item.get("name"), item["preview"], item["updatedAt"])
            for thread_id, item in self.items.items() if (thread_id in self.archived) == archived
        ), key=lambda item: (item.updated_at, item.thread_id), reverse=True)
        start = int(cursor.removeprefix("catalog:")) if cursor else 0
        return NativeThreadCatalogPage(tuple(items[start:start + limit]), f"catalog:{start + limit}" if start + limit < len(items) else None)

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
                                      policy=self.policy, create_thread=create, send_message=send, history=self.catalog)
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

    async def test_discovery_uses_native_cwd_and_reports_unknown_without_project_fallback(self):
        self.catalog.items["target"]["cwd"] = "/managed/worktree"
        listing = await self.call("list_threads")
        self.assertEqual(next(item for item in listing["threads"] if item["id"] == "target")["cwd"], "/managed/worktree")
        async def unavailable(identifier):
            if identifier == "target":
                raise OSError("native unavailable")
        self.catalog.read_hook = unavailable
        listing = await self.call("list_threads")
        target = next(item for item in listing["threads"] if item["id"] == "target")
        self.assertIsNone(target["cwd"])
        self.assertTrue(target["netizen"]["cwdUnavailable"])

    async def test_lazy_discovery_uses_explicit_working_cwd_without_native_creation(self):
        lazy = self.store.create_channel_binding(scope=FeishuScope("app", "lazy", ScopeKind.GROUP),
                                                project_alias="p", creator_id="person", working_cwd="/selected/worktree")
        listing = await self.call("list_threads")
        item = next(item for item in listing["threads"] if item["netizen"]["bindingId"] == lazy.id)
        self.assertEqual(item["cwd"], "/selected/worktree")
        self.assertIsNone(item["id"])
        self.assertEqual(self.calls, [])

    def worktree_service(self):
        calls = []
        async def query(**kwargs):
            calls.append(("list", kwargs))
            return {"worktrees": [{"id": "managed", "cwd": "/managed/tree"}], "nextCursor": None}
        async def create(*, project_alias, **kwargs):
            calls.append(("create", {"project_alias": project_alias, **kwargs}))
            return {"worktree": {"id": "new-managed", "cwd": "/managed/new", "creationState": "ready"}}
        async def cwd(binding_id, **kwargs):
            self.assertEqual(binding_id, self.source.id)
            return Path("/actual/source")
        self.catalog.resolve_binding_cwd = cwd
        self.service._worktrees = SimpleNamespace(query=query, create=create)
        return calls

    async def test_worktree_tools_use_source_project_and_actual_cwd_without_creating_session(self):
        calls = self.worktree_service()
        listed = await self.call("list_worktrees", {"limit": 4, "cursor": "page"})
        self.assertEqual(listed["worktrees"][0]["id"], "managed")
        result = await self.call("create_worktree", {"ref": "refs/heads/main", "allowAsync": True})
        self.assertEqual(result["worktree"]["id"], "new-managed")
        self.assertEqual(calls, [
            ("list", {"project_alias": "p", "cursor": "page", "limit": 4}),
            ("create", {"project_alias": "p", "expected_project_revision": self.store.get_project("p").revision,
                        "ref": "refs/heads/main", "source_cwd": Path("/actual/source"),
                        "origin_binding_id": self.source.id, "agent_default": True}),
        ])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.catalog.items["source"]["cwd"], "/project")

    async def test_worktree_discovery_and_creation_keep_independent_access(self):
        calls = self.worktree_service()
        self.policy.discovery = set()
        self.policy.read_send = {"chat-a"}
        self.assertEqual((await self.call("list_worktrees"))["error"]["code"], "access_denied")
        self.assertNotIn("error", await self.call("create_worktree"))
        self.policy.discovery = {"chat-a"}
        self.policy.read_send = set()
        self.assertNotIn("error", await self.call("list_worktrees"))
        self.assertEqual((await self.call("create_worktree"))["error"]["code"], "access_denied")
        self.assertEqual([name for name, _ in calls], ["create", "list"])

    async def test_removed_branch_tool_is_rejected_before_backend_dispatch(self):
        calls = self.worktree_service()
        result = await self.call("list_worktree_branches")
        self.assertEqual(result["error"]["code"], "unknown_tool")
        self.assertEqual(calls, [])
        self.assertEqual(self.calls, [])

    async def test_worktree_creation_does_not_fall_back_when_native_cwd_unavailable(self):
        calls = self.worktree_service()
        async def failed(*args, **kwargs):
            raise OSError("unavailable cwd")
        self.catalog.resolve_binding_cwd = failed
        result = await self.call("create_worktree")
        self.assertEqual(result["error"]["code"], "unavailable")
        self.assertEqual(calls, [])

    async def test_worktree_known_partial_failure_preserves_receipt_and_unknown_is_not_retried(self):
        self.worktree_service()
        calls = []
        async def known(*args, **kwargs):
            calls.append(kwargs)
            raise WorktreeError("creation_failed", "Checkout failed; inspect retained registration.",
                                details={"worktree": {"id": "registered", "creationState": "failed"}})
        self.service._worktrees.create = known
        result = await self.call("create_worktree")
        self.assertEqual(result["error"]["code"], "creation_failed")
        self.assertEqual(result["worktree"]["id"], "registered")
        self.assertEqual(len(calls), 1)
        async def unknown(*args, **kwargs):
            calls.append(kwargs)
            raise OSError("sensitive git output")
        self.service._worktrees.create = unknown
        result = await self.call("create_worktree")
        self.assertEqual(result["error"]["code"], "outcome_unknown")
        self.assertNotIn("sensitive", json.dumps(result))
        self.assertEqual(len(calls), 2)

    async def test_worktree_results_never_shorten_usable_paths_or_skip_pages(self):
        self.worktree_service()
        async def huge(**kwargs):
            return {"worktrees": [{"id": "id", "cwd": "/" + "x" * 9000}], "nextCursor": "next"}
        self.service._worktrees.query = huge
        result = await self.call("list_worktrees")
        self.assertEqual(result["error"]["code"], "response_too_large")
        self.assertNotIn("nextCursor", result)
        self.assertNotIn("worktrees", result)
        async def huge_create(*args, **kwargs):
            return {"worktree": {"id": "created", "cwd": "/" + "x" * 9000, "creationState": "ready"}}
        self.service._worktrees.create = huge_create
        result = await self.call("create_worktree")
        self.assertEqual(result["worktree"], {"id": "created", "creationState": "ready"})
        self.assertLessEqual(result_bytes(result), 8000)

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
        original = self.catalog.thread_catalog_page
        async def revoke(**kwargs):
            self.policy.discovery = set()
            self.policy.read_send = set()
            self.policy.revision = "two"
            return await original(**kwargs)
        self.catalog.thread_catalog_page = revoke
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
        original = self.catalog.thread_catalog_page
        async def grant(**kwargs):
            self.policy.read_send = {"chat-b"}
            self.policy.revision = "two"
            return await original(**kwargs)
        self.catalog.thread_catalog_page = grant
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

    async def test_discovery_reads_one_directory_page_and_never_all_candidates(self):
        for index in range(250):
            self.catalog.items[f"external-{index}"] = thread(f"external-{index}", updated=-index)
        with patch.object(self.store, "query_binding_candidates", side_effect=AssertionError("full local inventory")):
            page = await self.call("list_threads", {"limit": 1})
        self.assertEqual(len(page["threads"]), 1)
        self.assertEqual(self.catalog.catalog_reads, [(False, None, 100)])
        self.assertIsNotNone(page["nextCursor"])

    async def test_sparse_discovery_search_scans_following_native_pages(self):
        self.policy.discovery = {"chat-b"}
        self.catalog.items["target"]["name"] = "needle"
        self.catalog.items["target"]["updatedAt"] = 1
        for index in range(105):
            self.catalog.items[f"external-{index}"] = thread(f"external-{index}", updated=100 + index)
        result = await self.call("list_threads", {"searchTerm": "needle"})
        self.assertEqual([item["id"] for item in result["threads"]], ["target"])
        self.assertEqual(self.catalog.catalog_reads, [(False, None, 100), (False, "catalog:100", 100)])

    async def test_discovery_budget_checkpoint_and_lazy_phase_never_skip_targets(self):
        expected = {"source", "target"}
        for index in range(7):
            self.binding("chat-b", f"large-{index}", title="中" * 2000, updated=20 + index)
            expected.add(f"large-{index}")
        lazy = self.binding("chat-lazy")
        found, lazy_ids, cursor = [], [], None
        for _ in range(20):
            result = await self.call("list_threads", {"limit": 50, **({"cursor": cursor} if cursor else {})})
            self.assertNotIn("error", result)
            self.assertLessEqual(result_bytes(result), 8000)
            found.extend(item["id"] for item in result["threads"] if item["id"] is not None)
            lazy_ids.extend(item["netizen"]["bindingId"] for item in result["threads"] if item["id"] is None)
            cursor = result["nextCursor"]
            if cursor is None:
                break
        self.assertIsNone(cursor)
        self.assertEqual(set(found), expected)
        self.assertEqual(len(found), len(expected))
        self.assertEqual(lazy_ids, [lazy.id])

    async def test_missing_native_catalog_entry_is_not_reintroduced_by_discovery(self):
        del self.catalog.items["target"]
        result = await self.call("list_threads")
        self.assertEqual([item["id"] for item in result["threads"]], ["source"])

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
        self.assertEqual(first["page"]["nextCursor"], "native-page:1")
        self.assertNotIn("secret output", json.dumps(first))
        second = await self.call("read_thread", {"threadId": "target", "cursor": first["page"]["nextCursor"], "turnLimit": 2})
        self.assertEqual([item["id"] for item in second["turns"]], ["turn-1", "turn-0"])
        self.assertFalse(second["page"]["hasMore"])
        output = await self.call("read_thread", {"threadId": "target", "includeOutputs": True, "maxOutputCharsPerItem": 3})
        self.assertEqual(output["turns"][0]["items"][0]["output"], {"text": "sec", "truncated": True, "originalChars": 13})
        self.assertTrue(all(not full for _, full in self.catalog.reads))
        self.assertEqual(self.catalog.history_reads[:2], [
            ("turns", "target", None, 1, "full"),
            ("turns", "target", "native-page:1", 2, "full"),
        ])

    async def test_history_budget_retains_page_membership_and_native_cursor(self):
        self.catalog.items["target"]["turns"] = [turn(str(index), "中" * 20000) for index in range(12)]
        result = await self.call("read_thread", {"threadId": "target", "turnLimit": 10})
        self.assertEqual([item["id"] for item in result["turns"]], [str(index) for index in reversed(range(2, 12))])
        self.assertEqual(result["page"]["nextCursor"], "native-page:10")
        self.assertLessEqual(result_bytes(result), 8000)
        last = await self.call("read_thread", {"threadId": "target", "cursor": result["page"]["nextCursor"], "turnLimit": 10})
        self.assertEqual([item["id"] for item in last["turns"]], ["1", "0"])

    async def test_wait_reads_latest_turn_and_bounded_items_for_both_history_modes(self):
        for mode in ("paginated", "legacy"):
            with self.subTest(mode=mode):
                self.catalog.items["target"].update(historyMode=mode, turns=[turn(str(index), f"answer {index}") for index in range(100)])
                self.catalog.history_reads.clear()
                result = await self.call("wait_threads", {"targets": [{"threadId": "target"}], "timeoutMs": 0})
                self.assertEqual(result["wake"]["turnId"], "99")
                self.assertEqual(result["polls"][0]["latestAssistantMessage"]["text"], "answer 99")
                expected = [("turns", "target", None, 1, "full" if mode == "legacy" else "summary")]
                if mode != "legacy":
                    expected.append(("items", "target", "99", 20))
                self.assertEqual(self.catalog.history_reads, expected)
        self.assertTrue(all(not full for _, full in self.catalog.reads))

    async def test_failed_history_page_never_falls_back_to_full_thread(self):
        with patch.object(self.catalog, "list_turns", side_effect=TimeoutError):
            result = await self.call("read_thread", {"threadId": "target"})
            self.assertEqual(result["error"]["code"], "unavailable")
        self.catalog.items["target"]["turns"] = [turn("latest", "answer")]
        with patch.object(self.catalog, "list_items", side_effect=TimeoutError):
            result = await self.call("wait_threads", {"targets": [{"threadId": "target"}], "timeoutMs": 0})
            self.assertIsNone(result["wake"])
            self.assertEqual(result["errors"][0]["code"], "unavailable")
        self.assertTrue(all(not full for _, full in self.catalog.reads))

    async def test_permission_change_during_native_read_applies_to_next_call(self):
        async def revoke(_thread_id):
            self.policy.read_send = set()
        self.catalog.read_hook = revoke
        result = await self.call("read_thread", {"threadId": "target"})
        self.assertEqual(result["thread"]["preview"], "PRIVATE BODY")
        denied = await self.call("read_thread", {"threadId": "target"})
        self.assertEqual(denied["error"]["code"], "access_denied")
        self.assertEqual(self.catalog.reads, [("target", False)])

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
            "id": "delegation", "type": "functionCallOutput", "name": "create_thread", "namespace": "codex_tui",
            "output": delegated_prompt(source_id, "中" * 10000),
        }]}]
        result = await self.call("read_thread", {"threadId": "target"})
        summary = result["turns"][0]["items"][0]["codexDelegation"]
        self.assertEqual(summary["sourceThreadId"], source_id)
        self.assertTrue(summary["input"].endswith("…"))
        self.assertLessEqual(len(summary["input"]), 2000)
        self.assertLessEqual(result_bytes(result), 8000)

    async def test_native_delegation_history_preserves_source_and_escaped_prompt(self):
        envelope = ("<codex_delegation>\n  <source_thread_id>source</source_thread_id>\n"
                    "  <input>核对 &lt;input&gt; &amp;amp;\n&lt;/input&gt;</input>\n</codex_delegation>")
        messages = [
            {"id": namespace + name, "type": "functionCallOutput", "name": name,
             "namespace": namespace, "output": envelope}
            for namespace in ("codex_tui", "codex_app")
            for name in ("create_thread", "send_message_to_thread")
        ]
        unrelated = [
            {**messages[0], "id": "other-namespace", "namespace": "another_server"},
            {**messages[0], "id": "other-tool", "name": "read_thread"},
            {**messages[0], "id": "plain-output", "output": "not an envelope"},
            {**messages[0], "id": "missing-source", "output": "<codex_delegation>\n  <input>x</input>\n</codex_delegation>"},
            {**messages[0], "id": "missing-suffix", "output": envelope[:-1]},
        ]
        self.catalog.items["target"]["turns"] = [{"id": "turn", "status": "completed", "items": messages + unrelated}]
        history = await self.call("read_thread", {"threadId": "target"})
        items = history["turns"][0]["items"]
        for item in items[:4]:
            self.assertEqual(item["codexDelegation"], {"sourceThreadId": "source", "input": "核对 <input> &amp;\n</input>"})
            self.assertEqual(item["type"], "functionCallOutput")
            self.assertNotIn("output", item)
        for item in items[4:]:
            self.assertNotIn("codexDelegation", item)
            self.assertNotIn("output", item)
        expanded = await self.call("read_thread", {"threadId": "target", "includeOutputs": True})
        self.assertEqual(expanded["turns"][0]["items"][0]["output"]["text"], envelope)

    async def test_output_budget_retains_native_identity_answer_and_cursor(self):
        self.catalog.items["target"]["turns"] = [turn("old", "old answer"), turn("new", "中文" * 20000)]
        value = await self.call("read_thread", {"threadId": "target", "turnLimit": 2})
        self.assertLessEqual(result_bytes(value), 8000)
        self.assertEqual(value["thread"]["id"], "target")
        self.assertIsNone(value["page"]["nextCursor"])
        self.assertEqual([item["id"] for item in value["turns"]], ["new", "old"])
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


class SessionProjectionTests(unittest.TestCase):
    def test_item_budget_boundary_preserves_unicode_answer_and_only_removes_needed_items(self):
        for flag in (None, False, True):
            for excess in (0, 1):
                with self.subTest(items_truncated=flag, excess=excess):
                    answer = {"id": "answer", "type": "agentMessage", "text": '中文😀\\\n"'}
                    trailing = {"id": "trailing", "type": "commandExecution", "command": "pwd"}
                    expected = {
                        "thread": {"id": "thread"}, "page": {"nextCursor": "opaque:游标", "hasMore": True},
                        "turns": [{"id": "turn", "items": [answer, trailing], "itemsTruncated": True}],
                        "truncated": True,
                    }
                    answer["text"] += "x" * (8000 + excess - result_bytes(expected))
                    original = copy.deepcopy(expected)
                    original.pop("truncated")
                    original_turn = original["turns"][0]
                    if flag is None:
                        original_turn.pop("itemsTruncated")
                    else:
                        original_turn["itemsTruncated"] = flag
                    original_turn["items"].insert(0, {
                        "id": "old-tool", "type": "commandExecution", "output": "中" * 100,
                    })
                    unchanged = copy.deepcopy(original)
                    result = fit_result(original)
                    if excess:
                        expected["turns"][0]["items"].pop()
                    self.assertEqual(result, expected)
                    self.assertLessEqual(result_bytes(result), 8000)
                    self.assertEqual(original, unchanged)

    def test_large_history_page_has_bounded_serialization_work(self):
        items = [{
            "id": f"command-{index}", "type": "commandExecution", "command": "run",
            "status": "completed", "aggregatedOutput": "中" * 20000,
        } for index in range(19)] + [{
            "id": "answer", "type": "agentMessage", "text": "答" * 20000, "phase": "final_answer",
        }]
        value = {
            "thread": {"id": "thread"}, "page": {"nextCursor": "opaque-next", "hasMore": True},
            "turns": [turn_summary({"id": f"turn-{index}", "status": "completed", "items": items}, True, 20000)
                      for index in range(10)],
        }
        input_bytes = result_bytes(value)
        serialized_bytes = 0

        def measured_size(node):
            nonlocal serialized_bytes
            size = result_bytes(node)
            serialized_bytes += size
            return size

        with patch("netizen_cli.sessions.presentation.result_bytes", side_effect=measured_size):
            result = fit_result(value)
        # Bound actual encoding work rather than wall-clock time: this must not
        # repeatedly encode every remaining large payload after each removal.
        self.assertLess(serialized_bytes, input_bytes * 6)
        self.assertLessEqual(result_bytes(result), 8000)
        self.assertEqual([turn["id"] for turn in result["turns"]], [f"turn-{index}" for index in range(10)])
        self.assertEqual(result["page"], value["page"])
        self.assertTrue(all([item["id"] for item in turn["items"]] == ["answer"] for turn in result["turns"]))


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
        self.assertEqual(config["tools"]["create_worktree"]["approval_mode"], "prompt")
        self.assertGreater(config["tool_timeout_sec"], 120)
        self.assertEqual(set(config["enabled_tools"]), set(TOOL_ARGUMENTS))
        self.assertNotIn("list_worktree_branches", config["enabled_tools"])

    async def test_removed_branch_tool_is_not_advertised_and_calls_fail_explicitly(self):
        listed = await self.runner._list_tools(None, None)
        self.assertNotIn("list_worktree_branches", {tool.name for tool in listed.tools})
        result = await self.call("list_worktree_branches", {}, {"threadId": "source"})
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["error"]["code"], "unknown_tool")
        self.assertEqual(self.calls, [])

    async def test_worktree_schemas_cannot_override_caller_path_project_or_delete(self):
        self.assertEqual(set(TOOL_ARGUMENTS["create_thread"].model_fields), {"prompt", "title", "model"})
        self.assertNotIn("delete_worktree", TOOL_ARGUMENTS)
        for tool, data in (("create_thread", {"prompt": "p", "cwd": "/other"}),
                           ("create_worktree", {"cwd": "/other"}),
                           ("create_worktree", {"projectAlias": "other"}),
                           ("create_worktree", {"branch": "new"}),
                           ("create_worktree", {"allowAsync": False}),
                           ("create_worktree", {"allowAsync": 1}),
                           ("list_worktrees", {"sourceThreadId": "other"})):
            with self.subTest(tool=tool, data=data), self.assertRaises(SessionToolError):
                validate_arguments(tool, data)
        self.assertEqual(validate_arguments("create_worktree", {}), {"allowAsync": True})
        listed = {tool.name: tool for tool in (await self.runner._list_tools(None, None)).tools}
        self.assertFalse(listed["create_worktree"].annotations.read_only_hint)
        self.assertTrue(listed["list_worktrees"].annotations.read_only_hint)

    async def test_error_remains_a_tool_failure(self):
        async def fail(*args):
            return {"error": {"code": "target_not_current", "message": "Background"}, "threadId": "exact-target", "netizen": {"accepted": False}}
        self.runner._callback = fail
        result = await self.call("send_message_to_thread", {"threadId": "exact-target", "prompt": "continue"}, {"threadId": "source"})
        self.assertTrue(result.is_error)
        self.assertEqual(json.loads(result.content[0].text), result.structured_content)
        self.assertEqual(result.structured_content["threadId"], "exact-target")
