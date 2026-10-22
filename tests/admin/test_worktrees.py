from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from netizen_cli.worktrees.errors import WorktreeError
from netizen_cli.worktrees.settings import WorktreeSettingsService
from tests.admin import test_web as fixture
from tests.admin import test_project_deletion as project_fixture


class WorktreeServiceFixture:
    def __init__(self) -> None:
        self.calls = []
        self.safe = True
        self.resource = {"id": "worktree-1", "projectAlias": "test", "projectGeneration": 1,
            "root": "/instance/worktrees/one", "cwd": "/instance/worktrees/one", "state": "ready",
            "branch": None, "head": "a" * 40, "orphaned": False, "creationState": "ready",
            "associatedSessionCount": 1}

    async def query(self, **kwargs):
        self.calls.append(("query", kwargs))
        return {"items": [deepcopy(self.resource)], "nextCursor": "page-two"}

    async def creation_options(self, project_alias):
        return {"projectAlias": project_alias, "projectRevision": 7, "isGit": True}

    async def create(self, **kwargs):
        self.calls.append(("create", kwargs))
        return deepcopy(self.resource)

    def settings_view(self):
        return {"root": "", "effectiveRoot": "/instance/worktrees", "revision": "settings-1",
            "fetchBeforeCreate": False, "editable": True}

    async def update_settings(self, **kwargs):
        self.calls.append(("settings", kwargs))
        return self.settings_view()

    async def preview_delete(self, worktree_id, *, delete_sessions=False):
        self.calls.append(("preview", worktree_id, delete_sessions))
        return {"worktreeId": worktree_id, "fingerprint": "private-inventory",
            "deleteSessions": delete_sessions, "resource": deepcopy(self.resource),
            "bindings": [{"bindingId": "binding-1", "shortId": "bind", "nativeThreadId": "native-1"}],
            "safe": self.safe, "reasons": [] if self.safe else ["dirty"]}

    async def delete(self, worktree_id, **kwargs):
        self.calls.append(("delete", worktree_id, kwargs))
        return {"deleted": True, "code": "deleted"}


class AdminWorktreeTest(unittest.IsolatedAsyncioTestCase):
    request = fixture.AdminWebTest.request
    login = fixture.AdminWebTest.login
    json_get = fixture.AdminWebTest.json_get
    json_post = fixture.AdminWebTest.json_post

    async def asyncSetUp(self):
        await fixture.AdminWebTest.asyncSetUp(self)
        self.worktrees = WorktreeServiceFixture()
        self.management.worktrees = self.worktrees
        self.runner.open_admission()

    async def asyncTearDown(self):
        await fixture.AdminWebTest.asyncTearDown(self)

    async def test_queries_are_authenticated_and_forward_page_budget(self):
        status, _, _ = await self.json_get("/api/v1/worktrees", "invalid")
        self.assertEqual(status, 401)
        self.assertEqual(self.worktrees.calls, [])
        session = await self.login()
        status, _, result = await self.json_get("/api/v1/worktrees?project=test&pageSize=10&cursor=page-one", session)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["nextCursor"], "page-two")
        self.assertEqual(result["items"][0]["actions"]["previewDelete"]["target"],
            {"resource": "worktree", "targetId": "worktree-1", "scopeKey": None})
        _, kwargs = self.worktrees.calls[0]
        self.assertEqual({key: value for key, value in kwargs.items() if key != "deadline"},
            {"project_alias": "test", "cursor": "page-one", "limit": 10})
        self.assertGreater(kwargs["deadline"], 0)

    async def test_create_grant_binds_project_revision_and_rejects_replay_or_target_change(self):
        session = await self.login()
        status, _, result = await self.json_get("/api/v1/worktrees/create-options?project=test", session)
        self.assertEqual(status, 200, result)
        body = {**fixture._action_payload(result["actions"]["create"]), "mode": "new", "ref": "HEAD", "branch": "feature"}
        bad = {**body, "target": {"resource": "project", "targetId": "other"}}
        status, _, _ = await self.json_post("/api/v1/worktrees/create", session, bad)
        self.assertEqual(status, 409)
        self.assertEqual(self.worktrees.calls, [])
        status, _, result = await self.json_post("/api/v1/worktrees/create", session, body)
        self.assertEqual(status, 200, result)
        self.assertEqual(self.worktrees.calls, [("create", {"project_alias": "test", "expected_project_revision": 7,
            "ref": "HEAD", "mode": "new", "branch": "feature"})])
        status, _, _ = await self.json_post("/api/v1/worktrees/create", session, body)
        self.assertEqual(status, 409)
        self.assertEqual(len(self.worktrees.calls), 1)

    async def test_settings_use_revision_grant_not_client_revision_and_allow_default_root(self):
        session = await self.login()
        _, _, settings = await self.json_get("/api/v1/worktrees/settings", session)
        body = {**fixture._action_payload(settings["actions"]["save"]), "root": "", "fetchBeforeCreate": True}
        status, _, _ = await self.json_post("/api/v1/worktrees/settings", session, {**body, "revision": "invented"})
        self.assertEqual(status, 400)
        status, _, result = await self.json_post("/api/v1/worktrees/settings", session, body)
        self.assertEqual(status, 200, result)
        self.assertEqual(self.worktrees.calls, [("settings", {"root": "", "fetch_before_create": True,
            "expected_revision": "settings-1"})])

    async def preview(self, session, delete_sessions=False):
        _, _, page = await self.json_get("/api/v1/worktrees", session)
        return await self.json_post("/api/v1/worktrees/delete-preview", session,
            {**fixture._action_payload(page["items"][0]["actions"]["previewDelete"]), "deleteSessions": delete_sessions})

    async def test_delete_cascade_and_inventory_are_sealed_in_one_shot_grant(self):
        session = await self.login()
        for choice in (False, True):
            status, _, preview = await self.preview(session, choice)
            self.assertEqual(status, 200, preview)
            self.assertNotIn("fingerprint", preview)
            self.assertEqual(preview["deleteSessions"], choice)
            payload = fixture._action_payload(preview["actions"]["delete"])
            status, _, _ = await self.json_post("/api/v1/worktrees/delete", session,
                {**payload, "deleteSessions": not choice})
            self.assertEqual(status, 400)
            status, _, _ = await self.json_post("/api/v1/worktrees/delete", session, {**payload, "csrfToken": "invalid"})
            self.assertEqual(status, 403)
            status, _, result = await self.json_post("/api/v1/worktrees/delete", session, payload)
            self.assertEqual(status, 200, result)
            self.assertEqual(self.worktrees.calls[-1], ("delete", "worktree-1", {
                "delete_sessions": choice, "expected_fingerprint": "private-inventory"}))
            status, _, _ = await self.json_post("/api/v1/worktrees/delete", session, payload)
            self.assertEqual(status, 409)

    async def test_unsafe_preview_issues_no_mutation_authority(self):
        self.worktrees.safe = False
        session = await self.login()
        status, _, preview = await self.preview(session, True)
        self.assertEqual(status, 200, preview)
        self.assertEqual(preview["actions"], {})
        self.assertEqual(preview["reasons"], ["dirty"])
        self.assertFalse(any(call[0] == "delete" for call in self.worktrees.calls))

    async def test_non_git_and_failed_query_never_offer_creation(self):
        session = await self.login()
        self.worktrees.creation_options = AsyncMock(return_value={"projectAlias": "test", "projectRevision": 7,
            "isGit": False})
        status, _, result = await self.json_get("/api/v1/worktrees/create-options?project=test", session)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["actions"], {})
        self.worktrees.creation_options.side_effect = WorktreeError("unavailable", "状态未取得。")
        status, _, result = await self.json_get("/api/v1/worktrees/create-options?project=test", session)
        self.assertEqual(status, 503, result)
        self.assertEqual(result["code"], "unavailable")
        self.assertNotIn("actions", result)

    async def test_settings_read_only_and_missing_service_are_explicit(self):
        session = await self.login()
        original = self.worktrees.settings_view()
        self.worktrees.settings_view = lambda: {**original, "editable": False}
        status, _, result = await self.json_get("/api/v1/worktrees/settings", session)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["actions"], {})
        self.management.worktrees = None
        status, _, result = await self.json_get("/api/v1/worktrees", session)
        self.assertEqual(status, 503, result)

    async def test_partial_native_deletion_evidence_is_preserved_without_exposing_other_details(self):
        session = await self.login()
        _, _, preview = await self.preview(session, True)
        self.worktrees.delete = AsyncMock(side_effect=WorktreeError("unsafe_delete", "目录已改变，未删除。",
            details={"deletedSessionCount": 1, "remainingSessionCount": 2, "private": "not browser data"}))
        status, _, result = await self.json_post("/api/v1/worktrees/delete", session,
            fixture._action_payload(preview["actions"]["delete"]))
        self.assertEqual(status, 200, result)
        self.assertFalse(result["deleted"])
        self.assertEqual(result["deletedSessionCount"], 1)
        self.assertEqual(result["remainingSessionCount"], 2)
        self.assertNotIn("private", result)

    async def test_project_cascade_choice_is_not_mutable_after_preview(self):
        snapshot = SimpleNamespace(project=SimpleNamespace(alias="test", cwd="/source", revision=7),
            bindings=(), sides=(), scheduled_plans=(), scheduled_runs=(),
            worktrees=(SimpleNamespace(id="worktree-1", root="/worktrees/one", cwd="/worktrees/one"),),
            fingerprint="project-inventory")
        self.management.preview_project_delete = AsyncMock(return_value=snapshot)
        self.management.delete_project = AsyncMock(return_value=SimpleNamespace(deleted=True, project_alias="test",
            deleted_session_count=0, remaining_sessions=(), remaining_side_count=0, deleted_plan_count=0,
            remaining_scheduled_run_count=0, failed_binding_id=None, failed_side_id=None, code="deleted",
            deleted_worktree_count=1, remaining_worktree_count=0))
        session = await self.login()
        _, _, page = await self.json_get("/api/v1/projects", session)
        action = page["items"][0]["actions"]["previewDelete"]
        status, _, preview = await self.json_post("/api/v1/projects/delete-preview", session,
            {**fixture._action_payload(action), "deleteWorktrees": True})
        self.assertEqual(status, 200, preview)
        self.assertEqual(preview["worktreeCount"], 1)
        self.assertTrue(preview["deleteWorktrees"])
        payload = fixture._action_payload(preview["actions"]["delete"])
        status, _, _ = await self.json_post("/api/v1/projects/delete", session, {**payload, "deleteWorktrees": False})
        self.assertEqual(status, 400)
        status, _, result = await self.json_post("/api/v1/projects/delete", session, payload)
        self.assertEqual(status, 200, result)
        self.assertTrue(self.management.delete_project.call_args.kwargs["delete_worktrees"])
        self.assertEqual(self.management.delete_project.call_args.kwargs["expected_inventory_fingerprint"], "project-inventory")
        self.assertEqual(result["deletedWorktreeCount"], 1)


@unittest.skipUnless(shutil.which("git"), "Git is required")
class AdminWorktreeGitIntegrationTest(unittest.IsolatedAsyncioTestCase):
    request = fixture.AdminWebTest.request
    login = fixture.AdminWebTest.login
    json_get = fixture.AdminWebTest.json_get
    json_post = fixture.AdminWebTest.json_post
    asyncTearDown = project_fixture.AdminProjectDeletionTest.asyncTearDown

    async def asyncSetUp(self):
        await project_fixture.AdminProjectDeletionTest.asyncSetUp(self)
        git_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        git_env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        for args in (("init", "-b", "main"), ("-c", "user.name=Admin Test", "-c", "user.email=test@example.invalid",
            "commit", "--allow-empty", "-m", "initial")):
            subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(self.project_path), *args],
                check=True, capture_output=True, env=git_env)
        self.management.enable_worktrees(settings=WorktreeSettingsService(default_root=self.root / "worktrees"))
        self.runtime.worktree_usage = AsyncMock(side_effect=lambda binding_id: {
            "status": "idle", "cwd": self.store.get(binding_id).working_cwd,
        })

    async def create(self, session):
        status, _, options = await self.json_get("/api/v1/worktrees/create-options?project=test", session)
        self.assertEqual(status, 200, options)
        status, _, result = await self.json_post("/api/v1/worktrees/create", session,
            {**fixture._action_payload(options["actions"]["create"]), "mode": "detached", "ref": "HEAD"})
        self.assertEqual(status, 200, result)
        return result["resource"]

    async def preview(self, session, worktree_id, *, cascade=False):
        status, _, page = await self.json_get("/api/v1/worktrees", session)
        self.assertEqual(status, 200, page)
        row = next(item for item in page["items"] if item["id"] == worktree_id)
        status, _, preview = await self.json_post("/api/v1/worktrees/delete-preview", session,
            {**fixture._action_payload(row["actions"]["previewDelete"]), "deleteSessions": cascade})
        self.assertEqual(status, 200, preview)
        return preview

    def bind(self, resource):
        return self.store.create_channel_binding(scope=self.scope, project_alias="test", creator_id="ou_user",
            working_cwd=resource["cwd"], worktree_id=resource["id"])

    async def test_creation_grant_and_detached_checkout_do_not_query_branch_candidates(self):
        session = await self.login()
        with patch.object(self.management.worktrees._git, "list_branches", side_effect=TimeoutError) as branches:
            resource = await self.create(session)
        branches.assert_not_awaited()
        self.assertEqual(resource["state"], "ready")
        self.assertIsNone(resource["branch"])

    async def test_real_checkout_delete_preserves_unselected_lazy_session_and_cwd(self):
        session = await self.login()
        resource = await self.create(session)
        self.assertTrue(Path(resource["root"]).is_dir())
        self.assertEqual(resource["state"], "ready")
        self.assertIsNone(resource["branch"])
        binding = self.bind(resource)
        preview = await self.preview(session, resource["id"])
        self.assertTrue(preview["safe"], preview)
        status, _, result = await self.json_post("/api/v1/worktrees/delete", session,
            fixture._action_payload(preview["actions"]["delete"]))
        self.assertEqual(status, 200, result)
        self.assertTrue(result["deleted"], result)
        self.assertFalse(Path(resource["root"]).exists())
        retained = self.store.get(binding.id)
        self.assertEqual(retained.working_cwd, resource["cwd"])
        self.assertIsNone(retained.worktree_id)
        self.assertTrue(self.project_path.exists())

    async def test_dirty_checkout_denies_final_grant_and_exact_inventory_rejects_late_binding(self):
        session = await self.login()
        resource = await self.create(session)
        local_file = Path(resource["cwd"]) / "local.txt"
        local_file.write_text("untracked data", encoding="utf-8")
        preview = await self.preview(session, resource["id"], cascade=True)
        self.assertFalse(preview["safe"])
        self.assertEqual(preview["actions"], {})
        self.assertEqual(local_file.read_text(encoding="utf-8"), "untracked data")
        local_file.unlink()
        preview = await self.preview(session, resource["id"], cascade=True)
        self.bind(resource)
        status, _, result = await self.json_post("/api/v1/worktrees/delete", session,
            fixture._action_payload(preview["actions"]["delete"]))
        self.assertEqual(status, 409, result)
        self.assertEqual(result["code"], "inventory_changed")
        self.assertTrue(Path(resource["root"]).is_dir())

    async def test_project_delete_checkbox_preserves_or_deletes_real_owned_checkout(self):
        for cascade in (False, True):
            with self.subTest(cascade=cascade):
                if cascade:
                    self.store.register_project(alias="test", cwd=str(self.project_path))
                session = await self.login()
                resource = await self.create(session)
                self.bind(resource)
                _, _, page = await self.json_get("/api/v1/projects", session)
                action = next(item for item in page["items"] if item["alias"] == "test")["actions"]["previewDelete"]
                status, _, preview = await self.json_post("/api/v1/projects/delete-preview", session,
                    {**fixture._action_payload(action), "deleteWorktrees": cascade})
                self.assertEqual(status, 200, preview)
                status, _, result = await self.json_post("/api/v1/projects/delete", session,
                    fixture._action_payload(preview["actions"]["delete"]))
                self.assertEqual(status, 200, result)
                self.assertTrue(result["deleted"], result)
                self.assertEqual(Path(resource["root"]).exists(), not cascade)
                self.assertTrue(self.project_path.is_dir())
                if not cascade:
                    _, _, page = await self.json_get("/api/v1/worktrees", session)
                    self.assertTrue(next(item for item in page["items"] if item["id"] == resource["id"])["orphaned"])
