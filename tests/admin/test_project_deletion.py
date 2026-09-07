from __future__ import annotations

import asyncio
import itertools
import json
import unittest
from unittest.mock import AsyncMock, patch

from netizen.bindings import BindingStore, ProjectNotFound, SideTopicState
from netizen.domain import FeishuScope, ScopeKind
from netizen.management import InstanceManagementService, ScopeCoordinator
from netizen.projects import ProjectRegistry
from netizen.runtime.contracts import (
    NativeThreadMetadata,
    SideCloseFailed,
    ThreadLifecycleStateUnknown,
)
from tests.admin import test_web as web_fixture
from tests.management import test_service as management_fixture


class AdminProjectDeletionTest(unittest.IsolatedAsyncioTestCase):
    # Reuse only HTTP fixture helpers, without inheriting the unrelated test cases.
    request = web_fixture.AdminWebTest.request
    login = web_fixture.AdminWebTest.login
    json_get = web_fixture.AdminWebTest.json_get
    json_post = web_fixture.AdminWebTest.json_post

    async def asyncSetUp(self) -> None:
        await web_fixture.AdminWebTest.asyncSetUp(self)
        self.project_path = self.root / "project"
        self.project_path.mkdir()
        numbers = itertools.count()
        self.store = BindingStore(
            self.root / "channel.sqlite3",
            id_factory=lambda: f"binding-{next(numbers):03d}",
        )
        self.registry = ProjectRegistry(
            store=self.store,
            project_root=self.root,
            projects={"test": self.project_path},
        )
        self.runtime = management_fixture.FakeManagementRuntime(self.store)
        self.runtime.drain_project_side_creation = AsyncMock(return_value=None)
        self.management = InstanceManagementService(
            bindings=self.store,
            projects=self.registry,
            runtime=self.runtime,  # type: ignore[arg-type]
            scope_coordinator=ScopeCoordinator(),
        )
        self.runner.application._management = self.management
        self.scope = FeishuScope("cli_test", "oc_project", ScopeKind.DIRECT)
        self.runner.open_admission()

    async def asyncTearDown(self) -> None:
        try:
            await self.runner.drain(asyncio.get_running_loop().time() + 1)
        finally:
            self.runner.close_auth()
            await self.management.close()
            await self.store.aclose()
            self.temp.cleanup()

    def new_binding(self, native_thread_id: str | None = None):
        binding = self.store.create_channel_binding(
            scope=self.scope, project_alias="test", creator_id="ou_user",
        )
        if native_thread_id is not None:
            self.store.assign_native_thread_id(binding.id, native_thread_id)
            self.runtime.active_metadata[native_thread_id] = NativeThreadMetadata(
                native_thread_id, "Title", "Private preview",
            )
        return self.store.get(binding.id)

    async def preview_action(self, session: str):
        status, _, page = await self.json_get("/api/v1/projects", session)
        self.assertEqual(status, 200, page)
        project = next(item for item in page["items"] if item["alias"] == "test")
        return project["actions"]["previewDelete"]

    async def delete_action(self, session: str):
        action = await self.preview_action(session)
        status, _, preview = await self.json_post(
            "/api/v1/projects/delete-preview", session,
            web_fixture._action_payload(action),
        )
        self.assertEqual(status, 200, preview)
        return preview["actions"]["delete"]

    async def test_preview_requires_auth_and_counts_whole_inventory_without_mutation(self) -> None:
        for route in ("delete-preview", "delete"):
            status, _, _ = await self.json_post(f"/api/v1/projects/{route}", "invalid", {})
            self.assertEqual(status, 401)
        session = await self.login()
        self.new_binding()
        self.new_binding("native-active")
        archived = self.new_binding("native-archived")
        self.runtime.archived_metadata["native-archived"] = (
            self.runtime.active_metadata.pop("native-archived")
        )
        self.store.create_side_topic(
            app_id=self.scope.app_id, chat_id=self.scope.chat_id,
            source_message_id="om_side", parent_binding_id=archived.id,
            creator_id="ou_user", requires_mention=False,
        )
        action = await self.preview_action(session)
        self.runtime.calls.clear()
        before = self.store.preview_project_delete("test")
        payload = web_fixture._action_payload(action)
        status, _, preview = await self.json_post(
            "/api/v1/projects/delete-preview", session, payload,
        )
        self.assertEqual(status, 200, preview)
        self.assertEqual(preview["project"], {
            "alias": "test", "cwd": str(self.project_path.resolve()), "revision": 1,
        })
        self.assertEqual(
            [preview[key] for key in (
                "sessionCount", "lazySessionCount", "materializedSessionCount", "sideCount",
            )],
            [3, 1, 2, 1],
        )
        self.assertEqual(self.store.preview_project_delete("test"), before)
        self.assertEqual(self.runtime.calls, [("project-side-snapshots", "test")])
        serialized = json.dumps(preview)
        for private in (before.fingerprint, "native-active", "native-archived", "Private preview"):
            self.assertNotIn(private, serialized)
        status, _, error = await self.json_post(
            "/api/v1/projects/delete-preview", session, payload,
        )
        self.assertEqual(status, 409, error)
        self.assertEqual(error["code"], "stale_or_consumed")

    async def test_final_grant_is_exact_and_single_use_and_preserves_directory(self) -> None:
        self.new_binding("native-delete")
        session = await self.login()
        action = await self.delete_action(session)
        payload = web_fixture._action_payload(action)
        bad_csrf = {**payload, "csrfToken": "invalid"}
        status, _, _ = await self.json_post("/api/v1/projects/delete", session, bad_csrf)
        self.assertEqual(status, 403)
        self.assertTrue(self.store.get_project("test").enabled)
        tampered = {**payload, "target": {**payload["target"], "targetId": "other"}}
        status, _, _ = await self.json_post("/api/v1/projects/delete", session, tampered)
        self.assertEqual(status, 409)
        self.assertTrue(self.store.get_project("test").enabled)
        status, _, _ = await self.json_post(
            "/api/v1/projects/delete", session,
            {**payload, "expected_inventory_fingerprint": "invented"},
        )
        self.assertEqual(status, 400)
        self.assertTrue(self.store.get_project("test").enabled)
        status, _, result = await self.json_post("/api/v1/projects/delete", session, payload)
        self.assertEqual(status, 200, result)
        self.assertTrue(result["deleted"])
        self.assertEqual(result["deletedSessionCount"], 1)
        self.assertEqual(result["remainingSessions"], [])
        self.assertEqual(result["remainingSessionCount"], 0)
        self.assertEqual(result["code"], "deleted")
        self.assertTrue(self.project_path.is_dir())
        with self.assertRaises(ProjectNotFound):
            self.store.get_project("test")
        delete_calls = [call for call in self.runtime.calls if call[0] == "delete"]
        status, _, replay = await self.json_post("/api/v1/projects/delete", session, payload)
        self.assertEqual(status, 409, replay)
        self.assertEqual(replay["code"], "stale_or_consumed")
        self.assertEqual([call for call in self.runtime.calls if call[0] == "delete"], delete_calls)

    async def test_stale_same_count_inventory_rejected_before_project_disable(self) -> None:
        session = await self.login()
        native = self.new_binding("native-original")
        lazy = self.new_binding()
        for change in ("native-swap", "lazy-materialized", "binding-cohort"):
            with self.subTest(change=change):
                action = await self.delete_action(session)
                before_project = self.store.get_project("test")
                before_count = len(self.store.preview_project_delete("test").bindings)
                if change == "native-swap":
                    self.store._connection.execute(
                        "UPDATE bindings SET native_thread_id = ? WHERE binding_id = ?",
                        ("native-replaced", native.id),
                    )
                elif change == "lazy-materialized":
                    self.store.assign_native_thread_id(lazy.id, "native-new")
                else:
                    self.store.delete_binding(lazy.id)
                    self.new_binding("native-replacement")
                self.assertEqual(len(self.store.preview_project_delete("test").bindings), before_count)
                self.runtime.calls.clear()
                status, _, error = await self.json_post(
                    "/api/v1/projects/delete", session, web_fixture._action_payload(action),
                )
                self.assertEqual(status, 409, error)
                self.assertEqual(self.store.get_project("test"), before_project)
                self.assertFalse(self.store.project_delete_in_progress("test"))
                self.assertEqual(self.runtime.calls, [("project-side-snapshots", "test")])

    async def test_preview_and_final_grants_reject_stale_project_revision(self) -> None:
        self.new_binding("native-original")
        session = await self.login()
        preview_action = await self.preview_action(session)
        self.registry.set_enabled(alias="test", enabled=False, expected_revision=1)
        self.runtime.calls.clear()
        status, _, result = await self.json_post(
            "/api/v1/projects/delete-preview", session,
            web_fixture._action_payload(preview_action),
        )
        self.assertEqual(status, 409, result)
        self.assertNotIn("actions", result)
        self.assertEqual(self.runtime.calls, [("project-side-snapshots", "test")])
        delete_action = await self.delete_action(session)
        self.registry.set_enabled(alias="test", enabled=True, expected_revision=2)
        self.runtime.calls.clear()
        status, _, result = await self.json_post(
            "/api/v1/projects/delete", session,
            web_fixture._action_payload(delete_action),
        )
        self.assertEqual(status, 409, result)
        self.assertTrue(self.store.get_project("test").enabled)
        self.assertEqual(self.store.get_project("test").revision, 3)
        self.assertEqual(self.runtime.calls, [("project-side-snapshots", "test")])

    async def assert_incomplete(self, error: Exception, expected_code: str) -> None:
        first = self.new_binding("native-first")
        second = self.new_binding("native-second")
        session = await self.login()
        action = await self.delete_action(session)
        original = self.runtime.delete_exact

        async def fail_second(binding_id, *, expected_native_thread_id):
            if binding_id == second.id:
                raise error
            return await original(binding_id, expected_native_thread_id=expected_native_thread_id)

        with patch.object(self.runtime, "delete_exact", side_effect=fail_second) as deletion:
            status, _, result = await self.json_post(
                "/api/v1/projects/delete", session, web_fixture._action_payload(action),
            )
            self.assertEqual(status, 200, result)
            self.assertFalse(result["deleted"])
            self.assertEqual(result["code"], expected_code)
            self.assertEqual(result["deletedSessionCount"], 1)
            self.assertEqual(result["remainingSessionCount"], 1)
            self.assertEqual(result["remainingSessions"], [{
                "bindingId": second.id, "shortId": second.short_id, "scopeKey": second.scope_key,
            }])
            self.assertEqual(result["failedBindingId"], second.id)
            self.assertFalse(self.store.get_project("test").enabled)
            self.assertNotIn(str(error), json.dumps(result))
            self.assertNotIn("native-second", json.dumps(result))
            self.assertTrue(self.project_path.is_dir())
            self.assertEqual([call.args[0] for call in deletion.await_args_list], [first.id, second.id])
            status, _, _ = await self.json_post(
                "/api/v1/projects/delete", session, web_fixture._action_payload(action),
            )
            self.assertEqual(status, 409)
            self.assertEqual(deletion.await_count, 2)

    async def test_partial_failure_reports_remaining_sessions(self) -> None:
        await self.assert_incomplete(RuntimeError("SECRET raw tool arguments"), "delete_failed")

    async def test_unknown_outcome_is_not_reported_as_deleted(self) -> None:
        await self.assert_incomplete(
            ThreadLifecycleStateUnknown("SECRET raw native response"), "outcome_unknown",
        )

    async def test_side_failure_reports_exact_side_and_preserves_project(self) -> None:
        binding = self.new_binding("native-with-side")
        side = self.store.create_side_topic(
            app_id=self.scope.app_id, chat_id=self.scope.chat_id,
            source_message_id="om_side", parent_binding_id=binding.id,
            creator_id="ou_user", requires_mention=False,
        )
        self.store.set_side_topic_root(side.id, "om_side_root")
        side = self.store.open_side_topic(side.id, "omt_side")
        session = await self.login()
        action = await self.delete_action(session)
        with patch.object(
            self.runtime, "close_side_exact", side_effect=SideCloseFailed("SECRET raw side output"),
        ), patch.object(self.runtime, "delete_exact") as deletion:
            status, _, result = await self.json_post(
                "/api/v1/projects/delete", session, web_fixture._action_payload(action),
            )
            self.assertEqual(status, 200, result)
            self.assertFalse(result["deleted"])
            self.assertEqual(result["code"], "side_close_failed")
            self.assertEqual(result["failedSideId"], side.id)
            self.assertEqual(result["remainingSideCount"], 1)
            self.assertEqual(result["remainingSessionCount"], 1)
            self.assertFalse(self.store.get_project("test").enabled)
            self.assertNotIn("SECRET", json.dumps(result))
            deletion.assert_not_awaited()

    async def test_creating_side_preserves_route_and_reports_pending_publication(self) -> None:
        binding = self.new_binding("native-with-creating-side")
        side = self.store.create_side_topic(
            app_id=self.scope.app_id, chat_id=self.scope.chat_id,
            source_message_id="om_creating_side", parent_binding_id=binding.id,
            creator_id="ou_user", requires_mention=False,
        )
        session = await self.login()
        action = await self.delete_action(session)
        with patch.object(self.runtime, "close_side_exact") as close, \
                patch.object(self.runtime, "delete_exact") as deletion:
            status, _, result = await self.json_post(
                "/api/v1/projects/delete", session, web_fixture._action_payload(action),
            )
            self.assertEqual(status, 200, result)
            self.assertFalse(result["deleted"])
            self.assertEqual(result["code"], "side_creation_in_progress")
            self.assertEqual(result["deletedSessionCount"], 0)
            self.assertEqual(result["remainingSideCount"], 1)
            self.assertEqual(result["remainingSessionCount"], 1)
            self.assertEqual(result["failedSideId"], side.id)
            self.assertEqual(self.store.get_side_topic(side.id).state, SideTopicState.CREATING)
            self.assertFalse(self.store.get_project("test").enabled)
            self.assertTrue(self.project_path.is_dir())
            close.assert_not_awaited()
            deletion.assert_not_awaited()

    async def test_disconnected_delete_continues_once_and_hides_project_actions(self) -> None:
        self.new_binding("native-gated")
        session = await self.login()
        action = await self.delete_action(session)
        payload = web_fixture._action_payload(action)
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.runtime.delete_exact

        async def gated(binding_id, *, expected_native_thread_id):
            entered.set()
            await release.wait()
            return await original(binding_id, expected_native_thread_id=expected_native_thread_id)

        with patch.object(self.runtime, "delete_exact", side_effect=gated) as deletion:
            reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
            del reader
            body = json.dumps(payload).encode()
            raw = (
                "POST /api/v1/projects/delete HTTP/1.1\r\n"
                f"Host: {self.authority}\r\nOrigin: {self.origin}\r\n"
                f"Cookie: netizen_admin_session={session}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n\r\n"
            ).encode() + body
            try:
                writer.write(raw)
                await writer.drain()
                await asyncio.wait_for(entered.wait(), 1)
                writer.close()
                await writer.wait_closed()
                status, _, page = await self.json_get("/api/v1/projects", session)
                self.assertEqual(status, 200, page)
                project = next(item for item in page["items"] if item["alias"] == "test")
                self.assertTrue(project["deleting"])
                self.assertEqual(project["actions"], {})
                status, _, _ = await self.json_post("/api/v1/projects/delete", session, payload)
                self.assertEqual(status, 409)
                self.assertEqual(deletion.await_count, 1)
            finally:
                release.set()
                writer.close()
                await writer.wait_closed()
            async with asyncio.timeout(1):
                while self.runner.application.mutation_task_count:
                    await asyncio.sleep(0.002)
            self.assertEqual(deletion.await_count, 1)
        with self.assertRaises(ProjectNotFound):
            self.store.get_project("test")
        self.assertTrue(self.project_path.is_dir())
