from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import subprocess
import threading
import unittest
from unittest.mock import patch

from netizen_cli.management.blocking_io import (
    BlockingIODrainTimeout,
    BlockingIOExecutorClosed,
    BlockingIOExecutorSaturated,
)
from netizen_cli.project_files import FileBrowseError, read_file
from netizen_cli.worktrees.errors import WorktreeError
from netizen_cli.worktrees.settings import WorktreeSettingsService
from tests.support.channel_fixtures import channel_fixture


class ProjectFileServiceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = await self.enterAsyncContext(channel_fixture())
        self.store = self.fixture.store
        self.management = self.fixture.management
        self.files = self.management.files
        self.project = self.fixture.project.resolve()
        (self.project / "hello.txt").write_text("current file", encoding="utf-8")

    def deadline(self):
        return asyncio.get_running_loop().time() + 10

    async def root(self, *, project="test", worktree=None):
        return await self.files.root(project=project, worktree=worktree, deadline=self.deadline())

    async def content(self, identity, *, project="test", worktree=None, path="hello.txt"):
        return await self.files.content(project=project, worktree=worktree,
            identity=identity, path=path, deadline=self.deadline())

    def reserve_project_delete(self):
        snapshot = self.store.preview_project_delete("test")
        return self.store.begin_project_delete(alias="test", expected_revision=snapshot.project.revision,
            expected_inventory_fingerprint=snapshot.fingerprint)

    def finish_project_delete(self, reserved):
        self.store.finish_project_delete(alias="test", expected_revision=reserved.project.revision,
            expected_inventory_fingerprint=reserved.fingerprint)

    async def test_disabled_project_is_readable_and_file_content_is_not_cached(self):
        root = await self.root()
        project = self.store.get_project("test")
        self.store.set_project_enabled(alias="test", enabled=False, expected_revision=project.revision)
        self.assertEqual((await self.root())["identity"], root["identity"])
        first = await self.content(root["identity"])
        self.assertEqual(first.text, "current file")
        (self.project / "hello.txt").write_text("changed on disk", encoding="utf-8")
        second = await self.content(root["identity"])
        self.assertEqual(second.text, "changed on disk")
        self.assertNotEqual(first.version, second.version)

    async def test_project_deletion_and_same_alias_reregistration_invalidate_open_view(self):
        old = await self.root()
        reserved = self.reserve_project_delete()
        for operation in (self.root, lambda: self.content(old["identity"])):
            with self.assertRaises(FileBrowseError) as raised:
                await operation()
            self.assertEqual(raised.exception.code, "root_changed")
        self.finish_project_delete(reserved)
        with self.assertRaises(FileBrowseError) as raised:
            await self.root()
        self.assertEqual(raised.exception.code, "root_changed")
        self.store.register_project(alias="test", cwd=str(self.project))
        current = await self.root()
        self.assertNotEqual(current["identity"], old["identity"])
        with self.assertRaises(FileBrowseError) as raised:
            await self.content(old["identity"])
        self.assertEqual(raised.exception.code, "root_changed")
        self.assertEqual((await self.content(current["identity"])).text, "current file")

    async def test_replaced_directory_invalidates_root_even_when_path_is_unchanged(self):
        old = await self.root()
        self.project.rename(self.project.with_name("retained-project"))
        self.project.mkdir()
        (self.project / "hello.txt").write_text("replacement directory", encoding="utf-8")
        with self.assertRaises(FileBrowseError) as raised:
            await self.content(old["identity"])
        self.assertEqual(raised.exception.code, "root_changed")
        current = await self.root()
        self.assertNotEqual(current["identity"], old["identity"])
        self.assertEqual((await self.content(current["identity"])).text, "replacement directory")

    async def test_registration_is_rechecked_after_a_read_finishes(self):
        old = await self.root()
        entered, release = threading.Event(), threading.Event()

        def blocked(*args, **kwargs):
            result = read_file(*args, **kwargs)
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test did not release the read")
            return result

        with patch("netizen_cli.management.project_files.read_file", blocked):
            pending = asyncio.create_task(self.content(old["identity"]))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                reserved = self.reserve_project_delete()
                self.finish_project_delete(reserved)
                self.store.register_project(alias="test", cwd=str(self.project))
            finally:
                release.set()
            with self.assertRaises(FileBrowseError) as raised:
                await pending
        self.assertEqual(raised.exception.code, "root_changed")

    async def enable_git_worktrees(self):
        git_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        git_env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        for arguments in (("init", "-q", "-b", "main"),
            ("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
             "commit", "-q", "--allow-empty", "-m", "initial")):
            subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *arguments],
                cwd=self.project, env=git_env, capture_output=True, check=True, timeout=10)
        return self.management.enable_worktrees(settings=WorktreeSettingsService(
            default_root=self.fixture.project_root / "managed"))

    @unittest.skipUnless(shutil.which("git"), "Git is required")
    async def test_worktree_browse_resolves_current_cwd_without_binding_or_enabled_gate(self):
        worktrees = await self.enable_git_worktrees()
        resource = await worktrees.create(project_alias="test")
        (Path(resource["cwd"]) / "hello.txt").write_text("worktree file", encoding="utf-8")
        project = self.store.get_project("test")
        self.store.set_project_enabled(alias="test", enabled=False, expected_revision=project.revision)
        with patch.object(worktrees, "select", side_effect=AssertionError("must not select a Binding cwd")), \
                patch.object(worktrees, "resolve_for_browse", wraps=worktrees.resolve_for_browse) as resolve:
            root = await self.root(worktree=resource["id"])
            preview = await self.content(root["identity"], worktree=resource["id"])
        self.assertEqual(root["cwd"], resource["cwd"])
        self.assertEqual(root["rootId"], resource["id"])
        self.assertEqual(preview.text, "worktree file")
        self.assertEqual(resolve.await_count, 2)
        self.assertEqual(self.store.preview_project_delete("test").bindings, ())

    @unittest.skipUnless(shutil.which("git"), "Git is required")
    async def test_worktree_requires_current_project_ownership_ready_state_and_live_identity(self):
        worktrees = await self.enable_git_worktrees()
        resource = await worktrees.create(project_alias="test")
        root = await self.root(worktree=resource["id"])
        other = self.fixture.project_root / "other"
        other.mkdir()
        self.store.register_project(alias="other", cwd=str(other))
        with self.assertRaises(WorktreeError):
            await self.root(project="other", worktree=resource["id"])
        creating = self.store.worktrees.register(project_alias="test",
            expected_project_revision=self.store.get_project("test").revision,
            root=str(other / "pending"), cwd=str(other / "pending"), common_dir=str(self.project / ".git"))
        with self.assertRaises(WorktreeError):
            await self.root(worktree=creating.id)
        self.store.worktrees.discard_unstarted(creating)
        snapshot = self.store.worktrees.preview_delete(resource["id"])
        self.store.worktrees.reserve_delete(resource["id"], expected_fingerprint=snapshot.fingerprint)
        try:
            with self.assertRaises(WorktreeError):
                await self.root(worktree=resource["id"])
        finally:
            self.store.worktrees.release_delete(resource["id"])
        checkout = Path(resource["cwd"])
        checkout.rename(checkout.with_name(checkout.name + "-retained"))
        checkout.mkdir()
        with self.assertRaises(WorktreeError):
            await self.content(root["identity"], worktree=resource["id"])

    @unittest.skipUnless(shutil.which("git"), "Git is required")
    async def test_worktree_from_an_old_project_generation_is_not_adopted(self):
        worktrees = await self.enable_git_worktrees()
        resource = await worktrees.create(project_alias="test")
        reserved = self.reserve_project_delete()
        self.finish_project_delete(reserved)
        self.store.register_project(alias="test", cwd=str(self.project))
        with self.assertRaises(WorktreeError):
            await self.root(worktree=resource["id"])

    async def test_cancelled_waiters_keep_capacity_until_reads_finish_and_close_can_resume(self):
        release = threading.Event()
        submitted = asyncio.Event()
        submitted_count = 0

        def blocked(*args, **kwargs):
            if not release.wait(5):
                raise TimeoutError("test did not release blocking work")
            return "finished"

        async def invoke():
            nonlocal submitted_count
            submitted_count += 1
            if submitted_count == 4:
                submitted.set()
            return await self.root()

        root_patch = patch("netizen_cli.management.project_files.root_identity", blocked)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        waiters = [asyncio.create_task(invoke()) for _ in range(4)]
        try:
            await asyncio.wait_for(submitted.wait(), 2)
            for waiter in waiters:
                waiter.cancel()
            results = await asyncio.gather(*waiters, return_exceptions=True)
            self.assertTrue(all(isinstance(result, asyncio.CancelledError) for result in results))
            with self.assertRaises(BlockingIOExecutorSaturated):
                await self.root()
            # File previews have a separate budget and cannot consume the
            # existing Project-management filesystem worker's capacity.
            project = await self.management.resolve_new_project("test", deadline=self.deadline())
            self.assertEqual(project.cwd, self.project)
            with self.assertRaises(BlockingIODrainTimeout):
                await self.files.close(deadline=asyncio.get_running_loop().time())
            with self.assertRaises(BlockingIOExecutorClosed):
                await self.root()
        finally:
            release.set()
            await asyncio.gather(*waiters, return_exceptions=True)
            await self.files.close(deadline=self.deadline())
        with self.assertRaises(BlockingIOExecutorClosed):
            await self.root()
