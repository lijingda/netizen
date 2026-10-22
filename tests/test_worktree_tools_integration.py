from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import AsyncMock

from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.sessions.policy import SessionAccessPolicy, SessionAccessRule, SessionPolicyService
from netizen_cli.sessions.service import SessionService
from netizen_cli.worktrees.errors import WorktreeError
from netizen_cli.worktrees.settings import WorktreeSettingsService
from tests.support.channel_fixtures import channel_fixture


@unittest.skipUnless(shutil.which("git"), "Git is required")
class WorktreeToolIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = await self.enterAsyncContext(channel_fixture())
        self.store = self.fixture.store
        self.repo = self.fixture.project
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Worktree Tool Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "file.txt").write_text("committed\n")
        (self.repo / "sub").mkdir()
        (self.repo / "sub" / "file.txt").write_text("subdirectory\n")
        self.git("add", ".")
        self.git("commit", "-m", "initial")
        self.initial_head = self.git("rev-parse", "HEAD")
        self.worktrees = self.fixture.management.enable_worktrees(settings=WorktreeSettingsService(
            default_root=self.fixture.project_root / "managed",
        ))
        self.source = self.store.create_channel_binding(
            scope=FeishuScope("cli_test", "source", ScopeKind.GROUP),
            project_alias="test", creator_id="user",
        )
        self.store.assign_native_thread_id(self.source.id, "source-native")
        self.fixture.runtime.native_cwds["source-native"] = self.repo
        self.create_thread = AsyncMock()
        self.service = self.session_service()

    def session_service(self, policy: SessionAccessPolicy = SessionAccessPolicy()) -> SessionService:
        return SessionService(
            bindings=self.store, runtime=self.fixture.runtime, codex=None, app_id="cli_test",
            policy=SessionPolicyService(policy), create_thread=self.create_thread, send_message=AsyncMock(),
            worktrees=self.worktrees,
        )

    def git(self, *args, cwd=None):
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-C", str(cwd or self.repo), *args],
            env=env, stdin=subprocess.DEVNULL, capture_output=True, check=True,
        )
        return result.stdout.decode().strip()

    async def call(self, name, arguments=None):
        return await self.service.call(name, arguments or {}, "source-native")

    async def test_create_and_list_traverse_real_application_git_store(self):
        self.git("checkout", "-b", "feature")
        (self.repo / "file.txt").write_text("feature\n")
        self.git("commit", "-am", "feature")
        (self.repo / "file.txt").write_text("uncommitted\n")
        created = await self.call("create_worktree")
        self.assertNotIn("error", created)
        self.assertTrue(created["available"])
        self.assertIsNone(created["branch"])
        self.assertEqual(created["head"], self.initial_head)
        self.assertEqual((Path(created["cwd"]) / "file.txt").read_text(), "committed\n")
        stored = self.store.worktrees.get(created["id"])
        self.assertEqual(stored.origin_binding_id, self.source.id)
        self.assertEqual(stored.creation_state, "ready")
        self.assertEqual(self.store.worktrees.bindings(stored.id), ())
        listing = await self.call("list_worktrees")
        self.assertEqual([item["id"] for item in listing["items"]], [stored.id])
        self.assertTrue(listing["items"][0]["available"])
        self.assertEqual(self.fixture.runtime.native_cwds["source-native"], self.repo)
        self.assertEqual(self.git("branch", "--show-current"), "feature")
        self.assertEqual((self.repo / "file.txt").read_text(), "uncommitted\n")
        self.create_thread.assert_not_awaited()

    async def test_removed_model_branch_tool_preserves_internal_creation_options(self):
        rejected = await self.call("list_worktree_branches")
        self.assertEqual(rejected["error"]["code"], "unknown_tool")
        self.assertEqual(self.store.worktrees.list_for_project("test"), ())
        options = await self.worktrees.options(project_alias="test")
        self.assertTrue(options["isGit"])
        self.assertNotIn("branches", options)

    async def test_source_subdirectory_is_preserved_and_explicit_ref_wins(self):
        self.fixture.runtime.native_cwds["source-native"] = self.repo / "sub"
        self.git("checkout", "-b", "feature")
        (self.repo / "sub" / "file.txt").write_text("feature subdirectory\n")
        self.git("commit", "-am", "feature")
        created = await self.call("create_worktree", {"ref": "feature", "allowAsync": True})
        self.assertNotIn("error", created)
        self.assertEqual(Path(created["cwd"]), Path(created["root"]) / "sub")
        self.assertEqual((Path(created["cwd"]) / "file.txt").read_text(), "feature subdirectory\n")
        self.assertEqual(self.fixture.runtime.native_cwds["source-native"], self.repo / "sub")

    async def test_denied_mutation_does_not_create_registration_or_checkout(self):
        self.service = self.session_service(SessionAccessPolicy(read_send=SessionAccessRule("disabled")))
        denied = await self.call("create_worktree")
        self.assertEqual(denied["error"]["code"], "access_denied")
        self.assertEqual(self.store.worktrees.list_for_project("test"), ())
        self.assertFalse((self.fixture.project_root / "managed").exists())
        self.assertEqual((await self.call("list_worktrees"))["items"], [])

    async def test_external_branch_changes_are_live_but_missing_worktree_stays_registered(self):
        created = await self.call("create_worktree")
        self.assertNotIn("error", created)
        self.git("checkout", "-b", "external-branch", cwd=Path(created["cwd"]))
        listing = await self.call("list_worktrees")
        self.assertEqual(listing["items"][0]["branch"], "external-branch")
        self.git("worktree", "remove", created["root"])
        listing = await self.call("list_worktrees")
        self.assertEqual(listing["items"][0]["state"], "missing")
        self.assertFalse(listing["items"][0]["available"])
        self.assertEqual(self.store.worktrees.get(created["id"]).creation_state, "ready")

    async def test_disabled_project_has_no_available_worktree_and_tool_rejects_discovery(self):
        created = await self.call("create_worktree")
        self.assertNotIn("error", created)
        project = self.store.get_project("test")
        self.store.set_project_enabled(alias="test", enabled=False, expected_revision=project.revision)
        listing = await self.worktrees.query(project_alias="test")
        self.assertEqual(listing["items"][0]["id"], created["id"])
        self.assertEqual(listing["items"][0]["state"], "ready")
        self.assertFalse(listing["items"][0]["available"])
        self.assertEqual((await self.call("list_worktrees"))["error"]["code"], "project_unavailable")
        self.assertEqual((await self.call("create_worktree"))["error"]["code"], "project_unavailable")

    async def test_recreated_project_alias_never_makes_old_worktree_available(self):
        old = await self.call("create_worktree")
        self.assertNotIn("error", old)
        self.store.delete_binding(self.source.id)
        snapshot = self.store.preview_project_delete("test")
        reserved = self.store.begin_project_delete(
            alias="test", expected_revision=snapshot.project.revision,
            expected_inventory_fingerprint=snapshot.fingerprint,
        )
        self.store.finish_project_delete(
            alias="test", expected_revision=reserved.project.revision,
            expected_inventory_fingerprint=reserved.fingerprint,
        )
        project = self.store.register_project(alias="test", cwd=str(self.repo))
        self.assertGreater(project.generation, old["projectGeneration"])
        self.source = self.store.create_channel_binding(
            scope=FeishuScope("cli_test", "source", ScopeKind.GROUP),
            project_alias="test", creator_id="user",
        )
        self.store.assign_native_thread_id(self.source.id, "source-native")
        current = await self.call("create_worktree")
        self.assertNotIn("error", current)
        listing = {item["id"]: item for item in (await self.call("list_worktrees"))["items"]}
        self.assertTrue(listing[old["id"]]["orphaned"])
        self.assertFalse(listing[old["id"]]["available"])
        self.assertTrue(listing[current["id"]]["available"])
        options = await self.worktrees.options(project_alias="test")
        self.assertEqual([item["id"] for item in options["items"]], [current["id"]])
        with self.assertRaises(WorktreeError):
            await self.worktrees.select(project_alias="test", worktree_id=old["id"])
