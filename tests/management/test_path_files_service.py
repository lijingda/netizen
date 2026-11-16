from __future__ import annotations

import asyncio
import errno
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from netizen_cli.management.blocking_io import BlockingIODrainTimeout, BlockingIOExecutorClosed
from netizen_cli.project_files import FileBrowseError
from tests.support.channel_fixtures import channel_fixture


class PathFileServiceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = await self.enterAsyncContext(channel_fixture())
        self.management = self.fixture.management
        self.files = self.management.path_files
        self.root = self.fixture.project.resolve()
        (self.root / "hello.txt").write_text("hello")

    def deadline(self):
        return asyncio.get_running_loop().time() + 5

    async def open(self, root=None):
        return await self.files.root(root=str(root or self.root), deadline=self.deadline())

    async def content(self, opened, *, root=None, path="hello.txt", **kwargs):
        return await self.files.content(root=str(root or opened["cwd"]), identity=opened["identity"],
                                        path=path, deadline=self.deadline(), **kwargs)

    async def test_initial_resolution_is_off_loop_and_later_reads_require_canonical_root(self):
        alias = self.root.parent / "alias"
        alias.symlink_to(self.root.parent, target_is_directory=True)
        spelling = alias / self.root.name
        main_thread = threading.get_ident()
        resolve = Path.resolve

        def checked_resolve(path, **kwargs):
            self.assertNotEqual(threading.get_ident(), main_thread)
            return resolve(path, **kwargs)

        with patch.object(Path, "resolve", checked_resolve):
            opened = await self.open(spelling)
        self.assertEqual(opened["cwd"], str(self.root))
        self.assertEqual(opened["rootId"], "path")
        self.assertEqual(len(opened["identity"]), 64)
        with patch.object(Path, "resolve", side_effect=AssertionError("resolve only on open")):
            listing = await self.files.directory(root=opened["cwd"], identity=opened["identity"],
                                                  path="", deadline=self.deadline())
            self.assertIn("hello.txt", [entry["path"] for entry in listing["entries"]])
            self.assertEqual((await self.content(opened)).text, "hello")
            with self.assertRaises(FileBrowseError) as caught:
                await self.content(opened, root=spelling)
            self.assertEqual(caught.exception.code, "unsafe_path")

    async def test_replacement_root_and_symlink_descendants_fail_closed(self):
        opened = await self.open()
        (self.root / "link").symlink_to(self.root / "hello.txt")
        with self.assertRaises(FileBrowseError) as caught:
            await self.content(opened, path="link")
        self.assertEqual(caught.exception.code, "unsafe_path")
        self.root.rename(self.root.with_name("old-root"))
        self.root.mkdir()
        (self.root / "hello.txt").write_text("replacement")
        with self.assertRaises(FileBrowseError) as caught:
            await self.content(opened)
        self.assertEqual(caught.exception.code, "root_changed")
        self.assertEqual(caught.exception.message, "目录已变化，请从对话记录重新打开链接。")
        with self.assertRaises(FileBrowseError) as caught:
            await self.files.directory(root=opened["cwd"], identity=opened["identity"],
                                       path="", deadline=self.deadline())
        self.assertEqual(caught.exception.code, "root_changed")
        self.assertEqual(caught.exception.message, "目录已变化，请从对话记录重新打开链接。")

    async def test_pseudo_filesystems_and_git_are_rejected_by_shared_readers(self):
        git = self.root / ".git"
        git.mkdir()
        alias = self.root / "git-alias"
        alias.symlink_to(git, target_is_directory=True)
        for path in (git, git / "child", alias, "/dev"):
            with self.subTest(path=str(path)), self.assertRaises(FileBrowseError) as caught:
                await self.open(path)
            self.assertEqual(caught.exception.code, "invalid_path")
        if Path("/proc").is_dir():
            link = self.root / "pseudo-alias"
            link.symlink_to("/proc", target_is_directory=True)
            for path in ("/proc", link):
                with self.subTest(path=str(path)), self.assertRaises(FileBrowseError) as caught:
                    await self.open(path)
                self.assertEqual(caught.exception.code, "invalid_path")
        opened = await self.open("/")
        listing = await self.files.directory(root="/", identity=opened["identity"], path="", deadline=self.deadline())
        self.assertIsInstance(listing["entries"], list)
        for path in ("proc/self/status", "SYS/kernel", "dev/null"):
            with self.subTest(path=path), self.assertRaises(FileBrowseError) as caught:
                await self.content(opened, path=path)
            self.assertEqual(caught.exception.code, "invalid_path")

    async def test_resolution_errors_use_shared_browse_errors(self):
        for error, code in ((OSError(errno.ENOENT, "missing"), "not_found"),
                            (OSError(errno.EACCES, "private"), "permission_denied"),
                            (OSError(errno.ELOOP, "loop"), "unsafe_path"),
                            (RuntimeError("loop"), "unsafe_path"),
                            (OSError(errno.EIO, "io"), "read_failed")):
            with self.subTest(code=code), patch.object(Path, "resolve", side_effect=error):
                with self.assertRaises(FileBrowseError) as caught:
                    await self.open()
                self.assertEqual(caught.exception.code, code)

    async def test_other_file_executor_saturation_does_not_block_path_reads_or_projection(self):
        release, entered = threading.Event(), threading.Event()

        def block():
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test did not release file_reads")

        pending = [asyncio.create_task(self.management.file_reads.submit(block)) for _ in range(4)]
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            self.assertEqual(self.management.file_reads.in_flight, 4)
            main_thread = threading.get_ident()

            def transform(preview):
                self.assertNotEqual(threading.get_ident(), main_thread)
                return {"text": preview.text}

            self.assertEqual(await self.content(await self.open(), transform=transform), {"text": "hello"})
        finally:
            release.set()
            await asyncio.gather(*pending)

    async def test_management_close_joins_both_file_executors(self):
        await self.open()
        await self.management.close(deadline=self.deadline())
        self.assertTrue(self.management.file_reads.joined)
        self.assertTrue(self.management.path_reads.joined)
        with self.assertRaises(BlockingIOExecutorClosed):
            await self.open()

    async def test_path_read_drain_timeout_follows_other_management_shutdown(self):
        entered, release = threading.Event(), threading.Event()

        def block():
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test did not release path_reads")

        pending = asyncio.create_task(self.management.path_reads.submit(block))
        close_paths = self.management.path_reads.aclose

        async def expire_path_drain(*, deadline):
            await close_paths(deadline=asyncio.get_running_loop().time())

        async def close_worktrees(*, deadline):
            self.assertFalse(self.management.path_reads.accepting)

        worktrees = SimpleNamespace(close=AsyncMock(side_effect=close_worktrees))
        labels = SimpleNamespace(aclose=AsyncMock())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            with (
                patch.object(self.management, "worktrees", worktrees),
                patch.object(self.management, "_chat_labels", labels),
                patch.object(self.management._updates, "close", wraps=self.management._updates.close) as updates,
                patch.object(self.management.path_reads, "aclose", side_effect=expire_path_drain),
            ):
                deadline = self.deadline()
                with self.assertRaises(BlockingIODrainTimeout):
                    await self.management.close(deadline=deadline)
                worktrees.close.assert_awaited_once_with(deadline=deadline)
                updates.assert_awaited_once_with(deadline=deadline)
                labels.aclose.assert_awaited_once_with()
                self.assertTrue(self.management.file_reads.joined)
                self.assertTrue(self.management._blocking_io.joined)
                self.assertFalse(self.management.path_reads.joined)
        finally:
            release.set()
            await pending
            await close_paths(deadline=self.deadline())
