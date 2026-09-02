from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

from netizen.git_status import git_branch_status


class FakeGitProcess:
    def __init__(
        self,
        *,
        stdout: bytes = b"",
        returncode: int = 0,
        block: bool = False,
    ) -> None:
        self.stdout = stdout
        self.final_returncode = returncode
        self.block = block
        self.returncode: int | None = None
        self.killed = False
        self.waited = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self.block:
            await asyncio.Event().wait()
        self.returncode = self.final_returncode
        return self.stdout, b""

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        self.waited = True
        assert self.returncode is not None
        return self.returncode


class GitBranchStatusTest(unittest.IsolatedAsyncioTestCase):
    async def test_returns_git_porcelain_branch_header_content(self) -> None:
        process = FakeGitProcess(stdout=b"## main...origin/main [ahead 2]\n")
        with patch(
            "netizen.git_status.asyncio.create_subprocess_exec",
            return_value=process,
        ) as create:
            status = await git_branch_status(Path("/srv/project"))

        self.assertEqual(status, "main...origin/main [ahead 2]")
        self.assertEqual(
            create.await_args.args,
            (
                "git",
                "--no-optional-locks",
                "-C",
                "/srv/project",
                "-c",
                "core.fsmonitor=false",
                "status",
                "--porcelain=v1",
                "--branch",
                "--untracked-files=no",
                "--ignore-submodules=all",
                "--",
                ".git",
            ),
        )

    async def test_preserves_git_special_branch_states(self) -> None:
        for header, expected in (
            (b"## HEAD (no branch)\n", "HEAD (no branch)"),
            (b"## No commits yet on main\n", "No commits yet on main"),
        ):
            with self.subTest(header=header):
                process = FakeGitProcess(stdout=header)
                with patch(
                    "netizen.git_status.asyncio.create_subprocess_exec",
                    return_value=process,
                ):
                    status = await git_branch_status(Path("/srv/project"))

                self.assertEqual(status, expected)

    async def test_non_git_and_malformed_output_are_unavailable(self) -> None:
        for process in (
            FakeGitProcess(returncode=128),
            FakeGitProcess(stdout=b"unexpected\n"),
        ):
            with self.subTest(process=process):
                with patch(
                    "netizen.git_status.asyncio.create_subprocess_exec",
                    return_value=process,
                ):
                    status = await git_branch_status(Path("/srv/project"))

                self.assertIsNone(status)

    async def test_missing_git_is_unavailable(self) -> None:
        with patch(
            "netizen.git_status.asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("git"),
        ):
            status = await git_branch_status(Path("/srv/project"))

        self.assertIsNone(status)

    async def test_timeout_kills_and_reaps_git(self) -> None:
        process = FakeGitProcess(block=True)
        with (
            patch(
                "netizen.git_status.asyncio.create_subprocess_exec",
                return_value=process,
            ),
            patch("netizen.git_status._GIT_STATUS_TIMEOUT_SECONDS", 0.001),
        ):
            status = await git_branch_status(Path("/srv/project"))

        self.assertIsNone(status)
        self.assertTrue(process.killed)
        self.assertTrue(process.waited)
