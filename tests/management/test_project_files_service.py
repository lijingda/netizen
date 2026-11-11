from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import subprocess
import threading
import unittest
from unittest.mock import AsyncMock, patch

from netizen_cli.management.blocking_io import (
    BlockingIODrainTimeout,
    BlockingIOExecutorClosed,
    BlockingIOExecutorSaturated,
    BlockingIOResultUnknown,
)
from netizen_cli.project_files import FileBrowseError, check_root, read_file
from netizen_cli.worktrees.errors import WorktreeError
from netizen_cli.worktrees.settings import WorktreeSettingsService
from netizen_cli.worktrees.viewer import Status
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

    async def reserve_project_delete(self):
        snapshot = await self.store.preview_project_delete("test")
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
        reserved = await self.reserve_project_delete()
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
                reserved = await self.reserve_project_delete()
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
        self.assertEqual((await self.store.preview_project_delete("test")).bindings, ())

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
        reserved = await self.reserve_project_delete()
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

    def git(self, *args):
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(self.project), *args],
            env=env, capture_output=True, check=True, timeout=15).stdout.decode().strip()

    async def git_read(self, operation="changes", path="", **kwargs):
        root = await self.root()
        return await getattr(self.files, operation)(project="test", worktree=None, identity=root["identity"],
                                                   path=path, deadline=self.deadline(), **kwargs)

    @unittest.skipUnless(shutil.which("git"), "Git is required")
    async def test_diff_text_deleted_same_cached_removal_mode_binary_limits_symlink_and_missing_object(self):
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        values = {"same.py": "base\n", "deleted": "deleted\n", "cached": "base\n", "mode": "base\n",
                  "binary": "old\0binary", "large": "x" * (1024 * 1024 + 1),
                  "lines": "\n" * 10001, "long-line": "x" * 20001,
                  "missing": "missing object\n", "hello.txt": "current file"}
        for path, value in values.items():
            (self.project / path).write_text(value)
        (self.project / "link").symlink_to("same.py")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        (self.project / "same.py").write_text("staged\n")
        self.git("add", "same.py")
        (self.project / "same.py").write_text("base\n")
        self.git("rm", "deleted")
        self.git("rm", "--cached", "cached")
        (self.project / "cached").write_text("worktree\n")
        (self.project / "mode").chmod(0o755)
        (self.project / "link").unlink()
        (self.project / "link").symlink_to("hello.txt")
        for path in ("binary", "large", "lines", "long-line", "missing"):
            (self.project / path).write_text("new\n")
        oid = self.git("rev-parse", "HEAD:missing")
        (self.project / ".git/objects" / oid[:2] / oid[2:]).unlink()
        same = await self.git_read("diff", "same.py")
        self.assertTrue(same["same"])
        self.assertEqual(same["language"], "python")
        self.assertEqual(same["old"], "base\n")
        deleted = await self.git_read("diff", "deleted")
        self.assertEqual((deleted["kind"], deleted["old"], deleted["new"]), ("deleted", "deleted\n", ""))
        cached = await self.git_read("diff", "cached")
        self.assertEqual((cached["kind"], cached["old"], cached["new"]), ("modified", "base\n", "worktree\n"))
        for path, reason in (("mode", "mode_only"), ("binary", "unsupported_file"),
            ("large", "preview_too_large"), ("lines", "preview_too_large"),
            ("long-line", "preview_too_large"), ("link", "symlink"), ("missing", "missing_object")):
            with self.subTest(path=path):
                diff = await self.git_read("diff", path)
                self.assertEqual(diff["reason"], reason)
                self.assertIsNone(diff["old"])
                self.assertIsNone(diff["new"])
        self.assertEqual((await self.git_read("diff", "hello.txt"))["kind"], "unchanged")
        (self.project / "new.txt").write_text("new\n")
        new = await self.git_read("diff", "new.txt")
        self.assertEqual((new["old"], new["new"]), ("", "new\n"))
        (self.project / "hello.txt").write_bytes(b"\x00new binary")
        self.assertEqual((await self.git_read("diff", "hello.txt"))["reason"], "unsupported_file")

    @unittest.skipUnless(shutil.which("git"), "Git is required")
    async def test_conflict_only_and_unborn_empty_base(self):
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        unborn = await self.git_read("diff", "hello.txt")
        self.assertIsNone(unborn["head"])
        self.assertEqual(unborn["old"], "")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.git("checkout", "-qb", "side")
        (self.project / "hello.txt").write_text("side\n")
        self.git("commit", "-qam", "side")
        self.git("checkout", "main")
        (self.project / "hello.txt").write_text("main\n")
        self.git("commit", "-qam", "main")
        with self.assertRaises(subprocess.CalledProcessError):
            self.git("merge", "side")
        conflict = await self.git_read("diff", "hello.txt")
        self.assertEqual((conflict["kind"], conflict["reason"]), ("conflict", "conflict"))
        self.assertIsNone(conflict["old"])

    async def test_git_busy_and_hanging_calls_do_not_use_file_read_capacity_and_close_cancels(self):
        entered = asyncio.Event()
        count = 0
        async def blocked(*_):
            nonlocal count
            count += 1
            if count == 2:
                entered.set()
            await asyncio.Future()

        root = await self.root()
        args = dict(project="test", worktree=None, identity=root["identity"], path="", deadline=self.deadline())
        with patch.object(self.files._git, "status", side_effect=blocked):
            tasks = [asyncio.create_task(self.files.changes(**args)) for _ in range(2)]
            try:
                await asyncio.wait_for(entered.wait(), 2)
                with patch.object(self.files._io, "submit", side_effect=AssertionError("Git busy must not occupy file_reads")):
                    busy = await self.files.changes(**args)
                    self.assertEqual(busy["git"], "unavailable")
                    self.assertIn("繁忙", busy["message"])
                self.assertEqual((await self.content(root["identity"])).text, "current file")
                self.assertTrue((await self.files.directory(**args))["entries"])
                await self.files.close(deadline=self.deadline())
                self.assertTrue(all(task.done() for task in tasks))
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def test_git_rechecks_directory_and_registration_and_bounds_timeout(self):
        root = await self.root()
        status = Status("", "", 10000)
        status.record("# branch.oid (initial)")
        status.finish()
        args = dict(project="test", worktree=None, identity=root["identity"], path="", deadline=self.deadline())
        async def replace_root(*_):
            self.project.rename(self.project.with_name("old"))
            self.project.mkdir()
            return status
        with patch.object(self.files._git, "status", side_effect=replace_root):
            with self.assertRaises(FileBrowseError) as error:
                await self.files.changes(**args)
        self.assertEqual(error.exception.code, "root_changed")
        args["identity"] = (await self.root())["identity"]
        async def replace_registration(*_):
            reserved = await self.reserve_project_delete()
            self.finish_project_delete(reserved)
            self.store.register_project(alias="test", cwd=str(self.project))
            return status
        with patch.object(self.files._git, "status", side_effect=replace_registration):
            with self.assertRaises(FileBrowseError) as error:
                await self.files.changes(**args)
        self.assertEqual(error.exception.code, "root_changed")
        args["identity"] = (await self.root())["identity"]
        async def blocked(*_):
            await asyncio.Future()
        args["deadline"] = asyncio.get_running_loop().time() + .05
        with patch.object(self.files._git, "status", side_effect=blocked):
            self.assertEqual((await self.files.changes(**args))["git"], "unavailable")
        with patch.object(self.files._git, "status", new_callable=AsyncMock) as git:
            args["path"] = "../escape"
            with self.assertRaises(FileBrowseError):
                await self.files.changes(**args)
            git.assert_not_called()

    async def test_git_root_checks_leave_the_event_loop_and_other_file_worker_free(self):
        root = await self.root()
        status = Status("", "", 10000)
        status.record("# branch.oid (initial)")
        status.finish()
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        for operation in ("changes", "diff"):
            for blocked_call in (1, 2):
                with self.subTest(operation=operation, blocked_call=blocked_call):
                    entered, release = asyncio.Event(), threading.Event()
                    threads = []
                    def blocked(*args, **kwargs):
                        threads.append(threading.get_ident())
                        if len(threads) == blocked_call:
                            loop.call_soon_threadsafe(entered.set)
                            self.assertNotEqual(threads[-1], loop_thread, "root checks must not block the event loop")
                            if not release.wait(5):
                                raise TimeoutError("test did not release root check")
                        return check_root(*args, **kwargs)
                    with patch("netizen_cli.management.project_files.check_root", side_effect=blocked), \
                            patch.object(self.files._git, "status", return_value=status) as git:
                        task = asyncio.create_task(getattr(self.files, operation)(project="test", worktree=None,
                            identity=root["identity"], path="hello.txt" if operation == "diff" else "", deadline=self.deadline()))
                        try:
                            await asyncio.wait_for(entered.wait(), 2)
                            self.assertFalse(task.done())
                            self.assertEqual(git.await_count, blocked_call - 1)
                            self.assertEqual((await self.content(root["identity"])).text, "current file")
                            self.assertTrue((await self.files.directory(project="test", worktree=None,
                                identity=root["identity"], path="", deadline=self.deadline()))["entries"])
                            release.set()
                            self.assertEqual((await task)["git"], "available")
                            self.assertEqual(len(threads), 2)
                            self.assertNotIn(loop_thread, threads)
                        finally:
                            release.set()
                            await asyncio.gather(task, return_exceptions=True)

    async def test_git_root_check_executor_failures_are_unavailable_but_identity_errors_propagate(self):
        root = await self.root()
        status = Status("", "", 10000)
        status.record("# branch.oid (initial)")
        status.finish()
        for failed_call in (1, 2):
            for error in (BlockingIOExecutorSaturated(), BlockingIOExecutorClosed(),
                          BlockingIOResultUnknown(deadline=self.deadline()),
                          FileBrowseError("root_changed", "changed")):
                with self.subTest(failed_call=failed_call, error=type(error).__name__):
                    deadline = self.deadline()
                    with patch.object(self.files._io, "submit", side_effect=[None] * (failed_call - 1) + [error]) as submit, \
                            patch.object(self.files._git, "status", return_value=status):
                        request = self.files.changes(project="test", worktree=None, identity=root["identity"], deadline=deadline)
                        if isinstance(error, FileBrowseError):
                            with self.assertRaises(FileBrowseError) as caught:
                                await request
                            self.assertEqual(caught.exception.code, "root_changed")
                        else:
                            self.assertEqual((await request)["git"], "unavailable")
                        self.assertEqual(submit.await_count, failed_call)
                        self.assertTrue(all(call.kwargs["deadline"] == deadline for call in submit.call_args_list))

    @unittest.skipUnless(shutil.which("git") and hasattr(os, "mkfifo"), "Git and FIFO support are required")
    async def test_close_kills_an_actual_inflight_git_process(self):
        self.git("init", "-q")
        root = await self.root()
        config = self.project / ".git/config"
        config.unlink()
        os.mkfifo(config)
        entered = asyncio.Event()
        processes = []
        real_spawn = asyncio.create_subprocess_exec
        async def spawn(*args, **kwargs):
            process = await real_spawn(*args, **kwargs)
            processes.append(process)
            entered.set()
            return process
        with patch("netizen_cli.worktrees.git.asyncio.create_subprocess_exec", side_effect=spawn):
            task = asyncio.create_task(self.files.changes(project="test", worktree=None,
                identity=root["identity"], deadline=self.deadline()))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                self.assertEqual((await self.content(root["identity"])).text, "current file")
                await self.files.close(deadline=self.deadline())
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(all(process.returncode is not None for process in processes))
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
