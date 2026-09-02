"""Bounded, read-only Git status for Project display."""

from __future__ import annotations

import asyncio
from pathlib import Path


_GIT_STATUS_TIMEOUT_SECONDS = 2.0
_BRANCH_HEADER_PREFIX = "## "


async def git_branch_status(cwd: Path) -> str | None:
    """Return Git's porcelain branch header content for ``cwd`` when available."""

    process: asyncio.subprocess.Process | None = None
    try:
        async with asyncio.timeout(_GIT_STATUS_TIMEOUT_SECONDS):
            process = await asyncio.create_subprocess_exec(
                "git",
                "--no-optional-locks",
                "-C",
                str(cwd),
                "-c",
                "core.fsmonitor=false",
                "status",
                "--porcelain=v1",
                "--branch",
                "--untracked-files=no",
                "--ignore-submodules=all",
                "--",
                ".git",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _stderr = await process.communicate()
    except asyncio.CancelledError:
        if process is not None:
            await _kill_and_reap(process)
        raise
    except (OSError, TimeoutError):
        if process is not None:
            await _kill_and_reap(process)
        return None
    if process.returncode != 0:
        return None
    header, _, _remainder = stdout.decode(
        "utf-8", errors="replace"
    ).partition("\n")
    if not header.startswith(_BRANCH_HEADER_PREFIX):
        return None
    status = header.removeprefix(_BRANCH_HEADER_PREFIX).strip()
    return status or None


async def _kill_and_reap(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await process.wait()
