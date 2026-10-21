from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from netizen_cli.admin.port_config import ConfigFileSnapshot
from netizen_cli.bindings import BindingNotFound, BindingStore, ProjectNotFound
from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.management import InstanceManagementService, ScopeCoordinator
from netizen_cli.projects import ProjectRegistry
from netizen_cli.worktrees.errors import WorktreeError
from netizen_cli.worktrees.git import GitError
from netizen_cli.worktrees.models import WorktreeConflict, WorktreeNotFound
from netizen_cli.worktrees.settings import WorktreeSettings, WorktreeSettingsService


class WorktreeRuntime:
    native_delete_available = True

    def __init__(self, store):
        self.store = store
        self.busy = set()
        self.delete_calls = []
        self.fail_delete = None
        self.live_sides = []

    async def binding_pointer_changed(self, previous, current):
        pass

    async def worktree_usage(self, binding_id):
        binding = self.store.get(binding_id)
        return {"cwd": binding.working_cwd or self.store.get_project(binding.project_alias).cwd,
                "status": "active" if binding_id in self.busy else "idle"}

    def project_side_snapshots(self, alias):
        return ()

    def worktree_side_snapshots(self, root):
        return tuple(side for side in self.live_sides if side.cwd.is_relative_to(root))

    def side_snapshot_exact(self, side_id):
        return None

    def project_has_fork_creation(self, alias):
        return False

    async def delete_exact(self, binding_id, *, expected_native_thread_id):
        self.delete_calls.append(binding_id)
        if binding_id == self.fail_delete:
            raise RuntimeError("synthetic native failure")
        binding = self.store.get(binding_id)
        if binding.native_thread_id != expected_native_thread_id:
            raise RuntimeError("native identity changed")
        return self.store.delete_binding(binding_id)


class WorktreeServiceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="netizen-worktree-service-")
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Worktree Test")
        (self.repo / "tracked.txt").write_text("committed")
        self.git("add", ".")
        self.git("commit", "-m", "initial")
        self.store = BindingStore(self.root / "channel.sqlite3")
        self.projects = ProjectRegistry(store=self.store, project_root=self.root, projects={"repo": self.repo})
        self.runtime = WorktreeRuntime(self.store)
        self.management = InstanceManagementService(bindings=self.store, projects=self.projects,
            runtime=self.runtime, scope_coordinator=ScopeCoordinator())
        self.service = self.management.enable_worktrees(settings=WorktreeSettingsService(default_root=self.root / "managed"))
        self.scope = FeishuScope("app", "chat", ScopeKind.DIRECT)

    async def asyncTearDown(self):
        await self.management.close()
        await self.store.aclose()
        self.temp.cleanup()

    def git(self, *args):
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True, env=env).stdout

    async def resource(self, **kwargs):
        return await self.service.create(project_alias="repo", **kwargs)

    async def binding(self, resource, native=None):
        result = await self.management.create_current_binding(scope=self.scope, creator_id="user",
            project_alias="repo", worktree_id=resource["id"])
        if native:
            self.store.assign_native_thread_id(result.binding.id, native)
        return self.store.get(result.binding.id)

    async def test_create_query_select_and_shared_lazy_directory(self):
        resource = await self.resource()
        first = await self.binding(resource)
        second = await self.binding(resource)
        self.assertNotEqual(first.id, second.id)
        self.assertIsNone(first.native_thread_id)
        self.assertEqual(first.working_cwd, resource["cwd"])
        self.assertEqual(first.worktree_id, second.worktree_id)
        page = await self.service.query(project_alias="repo")
        self.assertEqual(page["items"][0]["associatedSessionCount"], 2)
        self.assertTrue(page["items"][0]["available"])
        self.assertIsNone(page["items"][0]["branch"])

    async def test_create_keeps_root_and_fetch_from_one_settings_snapshot(self):
        self.git("remote", "add", "origin", str(self.repo))
        config = self.root / "config.yaml"
        config.write_text("{}\n")
        settings = WorktreeSettingsService(default_root=self.root / "managed",
            config_snapshot=ConfigFileSnapshot.read(config))
        self.service._settings = settings
        newer = WorktreeSettings(self.root / "updated", True)
        snapshot = settings.snapshot

        def capture_then_update():
            captured = snapshot()
            settings.update(newer, expected_revision=captured[1])
            return captured

        with patch.object(self.service._git, "create", wraps=self.service._git.create) as create:
            with patch.object(settings, "snapshot", side_effect=capture_then_update):
                first = await self.resource()
            second = await self.resource()

        self.assertEqual(Path(first["root"]).parent, self.root / "managed")
        self.assertEqual(Path(second["root"]).parent, newer.root)
        self.assertEqual([call.kwargs["fetch"] for call in create.await_args_list], [False, True])

    async def test_new_binding_failure_keeps_ready_resource_with_receipt(self):
        with patch.object(self.store, "create_channel_binding", side_effect=RuntimeError("binding failed")):
            with self.assertRaises(WorktreeError) as caught:
                await self.management.create_current_binding(scope=self.scope, creator_id="user",
                    project_alias="repo", worktree_create={"mode": "detached", "ref": "HEAD"})
        self.assertEqual(caught.exception.code, "session_creation_failed")
        resource = caught.exception.details["worktree"]
        self.assertTrue(Path(resource["cwd"]).is_dir())
        self.assertEqual(self.store.worktrees.get(resource["id"]).creation_state, "ready")
        self.assertEqual((await self.binding(resource)).working_cwd, resource["cwd"])

    async def test_git_unknown_retains_visible_registration_without_adoption(self):
        original = self.service._git.create
        async def uncertain(*args, **kwargs):
            await original(*args, **kwargs)
            raise GitError("create", "sensitive remote stderr", outcome="unknown")
        with patch.object(self.service._git, "create", uncertain):
            with self.assertRaises(WorktreeError) as caught:
                await self.resource()
        self.assertEqual(caught.exception.code, "outcome_unknown")
        self.assertNotIn("sensitive", str(caught.exception))
        resource = caught.exception.details["worktree"]
        self.assertTrue(Path(resource["root"]).is_dir())
        self.assertEqual((await self.service.query())["items"][0]["state"], "unknown")
        self.assertFalse((await self.service.preview_delete(resource["id"]))["safe"])

    async def test_preparation_failures_do_not_leave_managed_resource_receipts(self):
        for options in ({"ref": "missing-ref"}, {"mode": "new", "branch": "bad..name"},
                        {"mode": "existing", "branch": "main"}, {"mode": "new", "branch": "main"}):
            with self.subTest(options=options), self.assertRaises(WorktreeError) as raised:
                await self.resource(**options)
            self.assertEqual(raised.exception.code, "unavailable")
            self.assertNotIn("worktree", raised.exception.details)
            self.assertEqual((await self.service.query())["items"], [])
            self.assertEqual(tuple((self.root / "managed").iterdir()), ())
            self.assertEqual(self.store.preview_project_delete("repo").worktrees, ())

    async def test_reset_not_started_does_not_discard_successful_add_receipt(self):
        original = self.service._git._run
        async def fail_reset(cwd, *args, **kwargs):
            if args[0] == "reset":
                raise GitError("reset", "process could not start", outcome="not_started")
            return await original(cwd, *args, **kwargs)
        with patch.object(self.service._git, "_run", side_effect=fail_reset):
            with self.assertRaises(WorktreeError) as raised:
                await self.resource()
        self.assertEqual(raised.exception.code, "outcome_unknown")
        resource = raised.exception.details["worktree"]
        self.assertTrue((Path(resource["root"]) / ".git").is_file())
        self.assertEqual(self.store.worktrees.get(resource["id"]).creation_state, "unknown")

    async def test_unstarted_checkout_discards_receipt_without_rolling_back_fetch(self):
        self.git("remote", "add", "origin", str(self.repo))
        self.service._settings = WorktreeSettingsService(WorktreeSettings(fetch_before_create=True),
            default_root=self.root / "managed")
        with self.assertRaises(WorktreeError) as raised:
            await self.resource(ref="missing-ref-after-fetch")
        self.assertEqual(raised.exception.code, "unavailable")
        self.assertEqual((await self.service.query())["items"], [])
        self.assertEqual(self.git("rev-parse", "refs/remotes/origin/main"), self.git("rev-parse", "HEAD"))

    async def test_changed_reservation_is_retained_when_unstarted_cleanup_is_rejected(self):
        with patch.object(self.store.worktrees, "discard_unstarted", side_effect=WorktreeConflict("changed")):
            with self.assertRaises(WorktreeError) as raised:
                await self.resource(ref="missing-ref")
        self.assertEqual(raised.exception.code, "outcome_unknown")
        resource = raised.exception.details["worktree"]
        self.assertEqual(self.store.worktrees.get(resource["id"]).creation_state, "unknown")

    async def test_cancelled_or_timed_out_creation_retains_unconfirmed_receipt(self):
        started = asyncio.Event()
        async def blocked(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        with patch.object(self.service._git, "create", side_effect=blocked):
            task = asyncio.create_task(self.resource())
            await asyncio.wait_for(started.wait(), timeout=2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        with patch.object(self.service._git, "create", side_effect=TimeoutError):
            with self.assertRaises(WorktreeError) as raised:
                await self.resource()
        self.assertEqual(raised.exception.code, "outcome_unknown")
        rows = (await self.service.query())["items"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["creationState"] == "unknown" for row in rows))

    async def test_non_git_hides_options_but_missing_directory_is_not_non_git(self):
        plain = self.root / "plain"
        plain.mkdir()
        self.projects.register(alias="plain", path=str(plain), create_directory=False)
        self.assertFalse((await self.service.options(project_alias="plain"))["isGit"])
        plain.rmdir()
        with self.assertRaises(WorktreeError) as caught:
            await self.service.options(project_alias="plain")
        self.assertNotEqual(caught.exception.code, "not_git")

    async def test_human_branch_options_do_not_resolve_agent_default_base(self):
        self.git("branch", "feature")
        with patch.object(self.service._git, "default_base", side_effect=AssertionError("agent-only default")):
            branches = await self.service.branches(project_alias="repo")
            options = await self.service.options(project_alias="repo")
        self.assertEqual(branches["defaultRef"], "HEAD")
        self.assertEqual(options["branches"], branches["items"])
        self.assertEqual({item["ref"] for item in options["branches"]},
                         {"refs/heads/main", "refs/heads/feature"})

    async def test_keep_sessions_delete_retains_cwd_and_history_identity(self):
        resource = await self.resource()
        binding = await self.binding(resource, "native-kept")
        preview = await self.service.preview_delete(resource["id"])
        self.assertTrue(preview["safe"], preview)
        result = await self.service.delete(resource["id"], expected_fingerprint=preview["fingerprint"])
        self.assertTrue(result["deleted"])
        self.assertFalse(Path(resource["root"]).exists())
        retained = self.store.get(binding.id)
        self.assertEqual(retained.native_thread_id, "native-kept")
        self.assertEqual(retained.working_cwd, resource["cwd"])
        self.assertIsNone(retained.worktree_id)
        self.assertEqual(self.runtime.delete_calls, [])

    async def test_cascade_deletes_exact_shared_sessions_not_creator(self):
        origin = self.store.create_channel_binding(scope=self.scope, creator_id="user", project_alias="repo")
        resource = await self.resource(origin_binding_id=origin.id)
        first = await self.binding(resource, "native-one")
        second = await self.binding(resource)
        preview = await self.service.preview_delete(resource["id"], delete_sessions=True)
        result = await self.service.delete(resource["id"], delete_sessions=True,
                                          expected_fingerprint=preview["fingerprint"])
        self.assertTrue(result["deleted"])
        self.assertCountEqual(self.runtime.delete_calls, [first.id, second.id])
        self.assertEqual(self.store.get(origin.id).project_alias, origin.project_alias)
        self.assertIsNone(self.store.get(origin.id).worktree_id)

    async def test_dirty_or_active_worktree_refuses_delete(self):
        resource = await self.resource()
        binding = await self.binding(resource)
        preview = await self.service.preview_delete(resource["id"])
        self.runtime.busy.add(binding.id)
        with self.assertRaises(WorktreeError):
            await self.service.delete(resource["id"], expected_fingerprint=preview["fingerprint"])
        self.assertTrue(Path(resource["root"]).is_dir())
        self.runtime.busy.clear()
        (Path(resource["root"]) / "local.txt").write_text("keep")
        self.assertFalse((await self.service.preview_delete(resource["id"], delete_sessions=True))["safe"])

    async def test_delete_cascade_choice_and_inventory_are_sealed(self):
        resource = await self.resource()
        preview = await self.service.preview_delete(resource["id"])
        with self.assertRaises(WorktreeError):
            await self.service.delete(resource["id"], delete_sessions=True,
                                      expected_fingerprint=preview["fingerprint"])
        await self.binding(resource)
        with self.assertRaises(WorktreeError):
            await self.service.delete(resource["id"], expected_fingerprint=preview["fingerprint"])
        self.assertTrue(Path(resource["root"]).is_dir())

    async def test_project_delete_can_keep_or_cascade_worktrees(self):
        resource = await self.resource()
        await self.binding(resource)
        preview = await self.management.preview_project_delete(alias="repo", expected_revision=1,
            deadline=asyncio.get_running_loop().time() + 10)
        result = await self.management.delete_project(alias="repo", expected_revision=1,
            expected_inventory_fingerprint=preview.fingerprint)
        self.assertTrue(result.deleted)
        self.assertTrue(Path(resource["root"]).is_dir())
        self.assertTrue((await self.service.query())["items"][0]["orphaned"])
        self.projects.register(alias="repo", path=str(self.repo), create_directory=False)
        self.assertEqual((await self.service.options(project_alias="repo"))["items"], [])
        second = await self.resource()
        project = self.store.get_project("repo")
        preview = await self.management.preview_project_delete(alias="repo", expected_revision=project.revision,
            delete_worktrees=True, deadline=asyncio.get_running_loop().time() + 10)
        result = await self.management.delete_project(alias="repo", expected_revision=project.revision,
            expected_inventory_fingerprint=preview.fingerprint, delete_worktrees=True)
        self.assertTrue(result.deleted, result)
        self.assertEqual(result.deleted_worktree_count, 1)
        self.assertFalse(Path(second["root"]).exists())
        self.assertTrue(Path(resource["root"]).is_dir())

    async def test_lazy_admission_is_checked_under_delete_reservation(self):
        resource = await self.resource()
        binding = await self.binding(resource)
        preview = await self.service.preview_delete(resource["id"])
        original = self.runtime.worktree_usage
        async def in_flight(binding_id):
            with self.assertRaises(WorktreeConflict):
                self.store.worktrees.require_binding_not_deleting(binding_id)
            return {"cwd": resource["cwd"], "status": "active"}
        with patch.object(self.runtime, "worktree_usage", in_flight):
            with self.assertRaises(WorktreeError):
                await self.service.delete(resource["id"], expected_fingerprint=preview["fingerprint"])
        self.assertTrue(Path(resource["root"]).is_dir())
        self.store.worktrees.require_binding_not_deleting(binding.id)
        self.assertEqual((await original(binding.id))["status"], "idle")

    async def test_list_timeout_is_unknown_not_clean_or_missing(self):
        resource = await self.resource()
        async def slow(record):
            await asyncio.Event().wait()
        with patch.object(self.service, "_state", slow):
            result = await self.service.query(deadline=asyncio.get_running_loop().time() + 0.1)
        self.assertEqual(result["items"][0]["state"], "unknown")
        self.assertFalse(result["items"][0]["available"])
        self.assertEqual(result["items"][0]["id"], resource["id"])

    async def test_partial_native_delete_keeps_directory_and_exact_remaining_sessions(self):
        resource = await self.resource()
        await self.binding(resource, "native-one")
        await self.binding(resource, "native-two")
        first, second = self.store.worktrees.bindings(resource["id"])
        self.runtime.fail_delete = second.id
        preview = await self.service.preview_delete(resource["id"], delete_sessions=True)
        result = await self.service.delete(resource["id"], delete_sessions=True,
                                          expected_fingerprint=preview["fingerprint"])
        self.assertFalse(result["deleted"])
        self.assertEqual(result["deletedSessionCount"], 1)
        self.assertEqual(result["remainingSessionCount"], 1)
        self.assertTrue(Path(resource["root"]).is_dir())
        with self.assertRaises(BindingNotFound):
            self.store.get(first.id)
        self.assertEqual(self.store.get(second.id).native_thread_id, second.native_thread_id)
        self.runtime.fail_delete = None
        preview = await self.service.preview_delete(resource["id"], delete_sessions=True)
        result = await self.service.delete(resource["id"], delete_sessions=True,
                                          expected_fingerprint=preview["fingerprint"])
        self.assertTrue(result["deleted"])

    async def test_git_remove_failure_does_not_restore_successful_session_deletion(self):
        resource = await self.resource()
        binding = await self.binding(resource, "native-one")
        preview = await self.service.preview_delete(resource["id"], delete_sessions=True)
        with patch.object(self.service._git, "remove", side_effect=GitError("remove", "sensitive", outcome="unknown")):
            result = await self.service.delete(resource["id"], delete_sessions=True,
                                              expected_fingerprint=preview["fingerprint"])
        self.assertFalse(result["deleted"])
        self.assertEqual(result["code"], "outcome_unknown")
        self.assertEqual(result["deletedSessionCount"], 1)
        self.assertTrue(Path(resource["root"]).is_dir())
        self.assertEqual(self.store.worktrees.binding_count(resource["id"]), 0)
        with self.assertRaises(BindingNotFound):
            self.store.get(binding.id)

    async def test_disabled_project_resource_is_visible_but_unavailable(self):
        resource = await self.resource()
        project = self.store.get_project("repo")
        self.store.set_project_enabled(alias="repo", enabled=False, expected_revision=project.revision)
        item = (await self.service.query())["items"][0]
        self.assertEqual(item["id"], resource["id"])
        self.assertFalse(item["orphaned"])
        self.assertFalse(item["available"])

    async def test_branch_timeout_is_explicitly_unavailable_not_non_git(self):
        with patch.object(self.service._git, "list_branches", side_effect=TimeoutError):
            with self.assertRaises(WorktreeError) as caught:
                await self.service.options(project_alias="repo")
        self.assertEqual(caught.exception.code, "unavailable")

    async def test_close_waits_for_cancelled_mutation_finalizer(self):
        started, finalized = asyncio.Event(), asyncio.Event()
        async def mutation():
            task = asyncio.current_task()
            self.service._mutations.add(task)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                self.service._mutations.discard(task)
                finalized.set()
        task = asyncio.create_task(mutation())
        await started.wait()
        await self.service.close(deadline=asyncio.get_running_loop().time() + 0.2)
        self.assertTrue(finalized.is_set())
        self.assertTrue(task.cancelled())

    async def test_settings_errors_preserve_safe_configuration_feedback(self):
        from netizen_cli.admin.port_config import AdminPortConfigurationError
        before = self.service.settings_view()
        for error, code in ((AdminPortConfigurationError("private path"), "configuration_changed"),
                            (OSError("private disk"), "configuration_unavailable")):
            with patch.object(self.service._settings, "update", side_effect=error):
                with self.assertRaises(WorktreeError) as caught:
                    await self.service.update_settings(root="", fetch_before_create=True,
                                                        expected_revision=before["revision"])
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn("private", str(caught.exception))
            self.assertEqual(self.service.settings_view(), before)

    async def test_project_cascade_can_shutdown_active_creator_in_its_exact_inventory(self):
        origin = self.store.create_channel_binding(scope=self.scope, creator_id="user", project_alias="repo")
        self.store.assign_native_thread_id(origin.id, "native-origin")
        resource = await self.resource(origin_binding_id=origin.id)
        self.runtime.busy.add(origin.id)
        self.assertFalse((await self.service.preview_delete(resource["id"], delete_sessions=True))["safe"])
        preview = await self.management.preview_project_delete(alias="repo", expected_revision=1,
            delete_worktrees=True, deadline=asyncio.get_running_loop().time() + 10)
        result = await self.management.delete_project(alias="repo", expected_revision=1,
            expected_inventory_fingerprint=preview.fingerprint, delete_worktrees=True)
        self.assertTrue(result.deleted, result)
        self.assertEqual(self.runtime.delete_calls, [origin.id])
        self.assertFalse(Path(resource["root"]).exists())

    async def test_partial_project_worktree_delete_retains_disabled_project_and_known_remaining(self):
        await self.resource()
        await self.resource()
        first, second = self.store.worktrees.list_for_project("repo")
        preview = await self.management.preview_project_delete(alias="repo", expected_revision=1,
            delete_worktrees=True, deadline=asyncio.get_running_loop().time() + 10)
        original = self.service._git.remove
        async def remove(identity):
            if str(identity.root) == second.root:
                raise GitError("remove", "private diagnostics", outcome="unknown")
            await original(identity)
        with patch.object(self.service._git, "remove", remove):
            result = await self.management.delete_project(alias="repo", expected_revision=1,
                expected_inventory_fingerprint=preview.fingerprint, delete_worktrees=True)
        self.assertFalse(result.deleted)
        self.assertEqual(result.deleted_worktree_count, 1)
        self.assertEqual(result.remaining_worktree_count, 1)
        self.assertFalse(self.store.get_project("repo").enabled)
        self.assertFalse(Path(first.root).exists())
        self.assertTrue(Path(second.root).is_dir())
        self.assertTrue(self.repo.is_dir())

    async def test_known_subdirectory_usage_is_protected_but_not_cascaded(self):
        resource = await self.resource()
        nested = Path(resource["root"]) / "nested"
        nested.mkdir()
        binding = self.store.create_channel_binding(scope=self.scope, creator_id="user",
            project_alias="repo", working_cwd=str(nested))
        self.assertIsNone(binding.worktree_id)
        self.runtime.busy.add(binding.id)
        preview = await self.service.preview_delete(resource["id"], delete_sessions=True)
        self.assertFalse(preview["safe"])
        self.assertEqual(preview["bindings"], [])
        self.assertEqual(preview["protectedSessionCount"], 1)
        self.runtime.busy.clear()
        preview = await self.service.preview_delete(resource["id"], delete_sessions=True)
        self.assertTrue(preview["safe"], preview)
        result = await self.service.delete(resource["id"], delete_sessions=True,
                                          expected_fingerprint=preview["fingerprint"])
        self.assertTrue(result["deleted"])
        self.assertEqual(self.runtime.delete_calls, [])
        self.assertEqual(self.store.get(binding.id).working_cwd, str(nested))

    async def test_new_known_non_cascade_user_invalidates_delete_confirmation(self):
        resource = await self.resource()
        preview = await self.service.preview_delete(resource["id"])
        self.store.create_channel_binding(scope=self.scope, creator_id="user",
            project_alias="repo", working_cwd=str(Path(resource["root"]) / "nested"))
        with self.assertRaises(WorktreeError) as caught:
            await self.service.delete(resource["id"], expected_fingerprint=preview["fingerprint"])
        self.assertEqual(caught.exception.code, "inventory_changed")
        self.assertTrue(Path(resource["root"]).is_dir())

    async def test_cross_project_side_retains_protection_after_parent_moves_or_is_deleted(self):
        resource = await self.resource()
        other = self.root / "other-project"
        other.mkdir()
        self.projects.register(alias="other", path=str(other), create_directory=False)
        parent = self.store.create_channel_binding(scope=self.scope, creator_id="user",
            project_alias="other", working_cwd=str(other))
        route = self.store.create_side_topic(app_id="app", chat_id="chat", source_message_id="side-source",
            parent_binding_id=parent.id, creator_id="user", requires_mention=False)
        self.store.set_side_topic_root(route.id, "side-root")
        self.store.open_side_topic(route.id, "side-topic")
        self.runtime.live_sides = [SimpleNamespace(side_id=route.id, cwd=Path(resource["root"]), project_alias="other")]
        self.assertEqual(self.store.worktrees.usage_bindings(resource["id"]), ())
        for remove_parent in (False, True):
            if remove_parent:
                self.store.delete_binding(parent.id)
            preview = await self.service.preview_delete(resource["id"], delete_sessions=True)
            self.assertFalse(preview["safe"])
            self.assertEqual(preview["sideCount"], 1)
            self.assertTrue(any("非级联" in reason for reason in preview["reasons"]))
            self.assertEqual(preview["bindings"], [])
        self.assertTrue(Path(resource["root"]).is_dir())
