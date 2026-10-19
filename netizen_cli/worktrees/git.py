"""Bounded Git operations for explicitly registered, instance-owned worktrees.

This module owns no registry and does not infer ownership from a directory name.
Only a recorded identity may be inspected or removed. It never runs setup scripts,
copies source files, forces checkout/removal, or prunes unrelated registrations.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
from typing import Literal


@dataclass(frozen=True)
class RepositoryInfo:
    root: Path
    common_dir: Path
    relative_cwd: Path


@dataclass(frozen=True)
class BranchInfo:
    name: str
    ref: str
    commit: str
    remote: bool
    occupied_paths: tuple[Path, ...] = ()


@dataclass(frozen=True)
class WorktreeIdentity:
    root: Path
    cwd: Path
    common_dir: Path
    git_dir: Path
    identity_token: str


@dataclass(frozen=True)
class CreatedWorktree:
    identity: WorktreeIdentity
    head: str
    branch: str | None


@dataclass(frozen=True)
class WorktreeState:
    status: Literal["ready", "missing", "unknown", "replaced"]
    head: str | None = None
    branch: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class DeleteCheck:
    state: WorktreeState
    safe: bool
    reasons: tuple[str, ...] = ()


class GitError(RuntimeError):
    """A failed operation; ``unknown`` mutations may have left a resource behind."""

    def __init__(
        self,
        operation: str,
        detail: str,
        *,
        outcome: Literal["not_started", "failed", "unknown"] = "failed",
    ) -> None:
        self.operation = operation
        self.detail = detail
        self.outcome = outcome
        super().__init__(f"{operation}: {detail}")


class NotGitRepository(GitError):
    """An accessible directory has no usable Git checkout (not an I/O error)."""


_MAX_OUTPUT = 2 * 1024 * 1024
_ISOLATED_OPTIONS = (
    "--no-optional-locks",
    "--literal-pathspecs",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "attr.tree=",
    "-c", "core.attributesFile=",
    "-c", "core.untrackedCache=false",
    "-c", "submodule.recurse=false",
)
_GIT_SELECTORS = {
    "GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
    "GIT_PREFIX", "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_EXEC_PATH", "GIT_SHALLOW_FILE", "GIT_REPLACE_REF_BASE",
}


def _environment() -> dict[str, str]:
    # Keep SSH/proxy/credential environment for the explicitly enabled fetch.
    env = {
        key: value for key, value in os.environ.items()
        if key not in _GIT_SELECTORS and not key.startswith("GIT_CONFIG")
    }
    env.update({
        "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_NO_REPLACE_OBJECTS": "1", "LC_ALL": "C",
    })
    return env


def _validate_text(value: str, label: str) -> None:
    if not value or any(char in value for char in "\x00\r\n"):
        raise GitError(label, "invalid empty or control-character value", outcome="not_started")


def _stat_key(path: Path) -> tuple[int, int]:
    value = path.stat()
    return value.st_dev, value.st_ino


def _file_key(path: Path) -> tuple[int, int, int]:
    value = path.stat()
    return value.st_dev, value.st_ino, value.st_ctime_ns


def _read_link_file(path: Path) -> str:
    with path.open("rb") as stream:
        value = stream.read(65537)
    if len(value) > 65536:
        raise ValueError("Git backlink is unexpectedly large")
    return value.decode("utf-8", "surrogateescape").rstrip("\n")


def _identity_token(root: Path, git_dir: Path) -> str:
    # Inodes can be reused immediately after delete. The two stable Git backlink
    # files retain their ctime through ordinary branch/HEAD changes, unlike the
    # checkout and metadata directories themselves.
    return json.dumps([
        _stat_key(root), _stat_key(git_dir), _file_key(git_dir / "gitdir"),
        _file_key(root / ".git"),
    ], separators=(",", ":"))


def _token_parts(token: str) -> list[tuple[int, ...]]:
    try:
        values = json.loads(token)
        if not isinstance(values, list) or len(values) != 4 or any(
            not isinstance(pair, list) or len(pair) != length
            or any(type(item) is not int for item in pair)
            for pair, length in zip(values, (2, 2, 3, 3), strict=True)
        ):
            raise ValueError("invalid identity")
        return [tuple(pair) for pair in values]
    except (ValueError, TypeError) as error:
        raise GitError("identity", "recorded identity is unavailable") from error


class WorktreeGit:
    def __init__(self, *, timeout: float = 15.0) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.timeout = timeout

    async def _run(
        self,
        cwd: Path,
        *args: str,
        mutating: bool = False,
        allowed: tuple[int, ...] = (0,),
        options: tuple[str, ...] = (),
    ) -> tuple[int, str]:
        process: asyncio.subprocess.Process | None = None
        spawning: asyncio.Task[asyncio.subprocess.Process] | None = None

        async def read(stream: asyncio.StreamReader | None) -> bytes:
            assert stream is not None
            chunks: list[bytes] = []
            length = 0
            while chunk := await stream.read(65536):
                length += len(chunk)
                if length > _MAX_OUTPUT:
                    raise GitError(args[0], "Git output exceeds the bounded response size")
                chunks.append(chunk)
            return b"".join(chunks)

        async def stop() -> None:
            nonlocal process
            if process is None and spawning is not None:
                # Cancellation must not lose the handle between OS spawn and
                # asyncio's subprocess transport completing initialization.
                try:
                    async with asyncio.timeout(2):
                        process = await asyncio.shield(spawning)
                except (OSError, TimeoutError):
                    spawning.cancel()
            if process is None:
                return
            # The group was created by this call; never signal unrelated processes.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                async with asyncio.timeout(2):
                    await process.wait()
            except TimeoutError:
                pass

        try:
            async with asyncio.timeout(self.timeout):
                spawning = asyncio.create_task(asyncio.create_subprocess_exec(
                    "git", *_ISOLATED_OPTIONS, *options, "-C", str(cwd), *args,
                    env=_environment(), stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                ))
                process = await asyncio.shield(spawning)
                stdout, stderr, _ = await asyncio.gather(
                    read(process.stdout), read(process.stderr), process.wait()
                )
        except asyncio.CancelledError:
            await stop()
            raise
        except (OSError, TimeoutError, GitError) as error:
            await stop()
            raise GitError(
                args[0], "Git timed out" if isinstance(error, TimeoutError) else str(error),
                outcome="unknown" if mutating and process is not None else "not_started",
            ) from error
        assert process.returncode is not None
        if process.returncode not in allowed:
            raise GitError(
                args[0], (
                    # Transport diagnostics can echo credential-bearing remote URLs.
                    "fetch failed; inspect the configured remote and authentication"
                    if args[0] == "fetch"
                    else stderr.decode("utf-8", "replace").strip()[:2000] or "Git failed"
                ),
                # Even a nonzero worktree-add can leave a new branch/registration.
                outcome="unknown" if mutating else "failed",
            )
        return process.returncode, stdout.decode("utf-8", "surrogateescape")

    async def _filters(self, cwd: Path) -> tuple[str, ...]:
        """Disable every helper of each currently configured filter driver."""
        _, output = await self._run(
            cwd, "config", "--null", "--name-only", "--get-regexp",
            r"^filter\..*\.(clean|smudge|process|required)$", allowed=(0, 1),
        )
        options: list[str] = []
        drivers = {key[len("filter."):].rsplit(".", 1)[0] for key in output.split("\0") if key}
        for driver in sorted(drivers):
            if not driver:
                continue
            for attribute in ("clean", "smudge", "process", "required"):
                options.extend(("-c", f"filter.{driver}.{attribute}={'false' if attribute == 'required' else ''}"))
        return tuple(options)

    async def inspect_repository(self, cwd: Path) -> RepositoryInfo:
        _validate_text(str(cwd), "source directory")
        try:
            resolved = cwd.resolve(strict=True)
            if not resolved.is_dir():
                raise OSError("source is not a directory")
        except OSError as error:
            raise GitError("repository", str(error), outcome="not_started") from error
        try:
            _, root = await self._run(resolved, "rev-parse", "--show-toplevel")
        except GitError as error:
            # LC_ALL=C is fixed for this narrow Git boundary. Do not confuse
            # permission, timeout, unsafe ownership or malformed metadata with a
            # normal non-Git Project; those states must remain unavailable.
            if error.outcome == "failed" and (
                error.detail == "fatal: not a git repository (or any of the parent directories): .git"
                or error.detail.startswith("fatal: not a git repository (or any parent up to mount point ")
                or error.detail == "fatal: this operation must be run in a work tree"
            ):
                raise NotGitRepository("repository", "directory has no Git checkout", outcome="not_started") from error
            raise
        _, common = await self._run(resolved, "rev-parse", "--path-format=absolute", "--git-common-dir")
        root_path = Path(root.rstrip("\n")).resolve(strict=True)
        common_path = Path(common.rstrip("\n")).resolve(strict=True)
        try:
            relative = resolved.relative_to(root_path)
        except ValueError as error:
            raise GitError("repository", "source directory is outside the checkout") from error
        return RepositoryInfo(root_path, common_path, relative)

    async def _registrations(self, cwd: Path, *, common: bool = False) -> list[dict[str, str]]:
        options = (f"--git-dir={cwd}",) if common else ()
        _, output = await self._run(cwd, "worktree", "list", "--porcelain", "-z", options=options)
        rows: list[dict[str, str]] = []
        row: dict[str, str] = {}
        for entry in output.split("\0"):
            if not entry:
                if row:
                    rows.append(row)
                    row = {}
                continue
            key, _, value = entry.partition(" ")
            row[key] = value
        if row:
            rows.append(row)
        return rows

    async def list_branches(self, cwd: Path) -> tuple[BranchInfo, ...]:
        info = await self.inspect_repository(cwd)
        rows = await self._registrations(info.root)
        _, output = await self._run(
            info.root, "for-each-ref", "--format=%(refname)%00%(objectname)%00%(symref)",
            "refs/heads", "refs/remotes",
        )
        result: list[BranchInfo] = []
        for line in output.splitlines():
            ref, commit, symbolic = line.split("\0", 2)
            if symbolic:
                continue
            remote = ref.startswith("refs/remotes/")
            prefix = "refs/remotes/" if remote else "refs/heads/"
            result.append(BranchInfo(
                ref.removeprefix(prefix), ref, commit, remote,
                tuple(Path(row["worktree"]) for row in rows if row.get("branch") == ref),
            ))
        return tuple(result)

    async def default_base(self, cwd: Path) -> str:
        _, output = await self._run(cwd, "for-each-ref", "--format=%(refname)%00%(symref)", "refs/remotes")
        symbolic = dict(line.split("\0", 1) for line in output.splitlines())
        candidates = sorted(
            (key for key, value in symbolic.items() if key.endswith("/HEAD") and value),
            key=lambda key: (key != "refs/remotes/origin/HEAD", key),
        )
        candidates = [symbolic[key] for key in candidates] + [
            "refs/remotes/origin/main", "refs/remotes/origin/master",
            "refs/heads/main", "refs/heads/master",
        ]
        for ref in candidates:
            code, _ = await self._run(cwd, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}", allowed=(0, 1))
            if code == 0:
                return ref
        raise GitError("default base", "no cached default branch; specify a ref", outcome="not_started")

    async def create(
        self,
        source_cwd: Path,
        target: Path,
        *,
        ref: str | None = None,
        mode: Literal["detached", "existing", "new"] = "detached",
        branch: str | None = None,
        fetch: bool = False,
        agent_default: bool = False,
    ) -> CreatedWorktree:
        # This is operation-wide evidence, not the outcome of the last Git
        # subprocess: reset may fail to start after add already created files.
        add_started = False
        try:
            info = await self.inspect_repository(source_cwd)
            _validate_text(str(target), "target directory")
            target = target.absolute()
            if target.exists() or target.is_symlink():
                raise GitError("create", "target already exists", outcome="not_started")
            if not target.parent.is_dir() or target.parent.resolve() != target.parent:
                raise GitError("create", "target parent must be an existing canonical directory", outcome="not_started")
            if mode not in {"detached", "existing", "new"}:
                raise GitError("create", "unknown checkout mode", outcome="not_started")
            if mode == "detached" and branch is not None:
                raise GitError("create", "detached checkout does not accept a branch name", outcome="not_started")
            if mode != "detached":
                _validate_text(branch or "", "branch")
                if branch is None or branch.startswith("-") or branch.startswith("refs/"):
                    raise GitError("branch", "use a local branch name", outcome="not_started")
                await self._run(info.root, "check-ref-format", f"refs/heads/{branch}")
            if mode == "new":
                code, _ = await self._run(info.root, "show-ref", "--verify", "--quiet",
                                         f"refs/heads/{branch}", allowed=(0, 1))
                if code == 0:
                    raise GitError("create", "new branch already exists", outcome="not_started")
            if fetch:
                await self._run(info.root, "fetch", "--all", "--no-recurse-submodules", mutating=True)
            if mode == "existing":
                if ref is not None:
                    raise GitError("create", "existing checkout uses the branch tip, not a separate base", outcome="not_started")
                base = f"refs/heads/{branch}"
                branches = await self.list_branches(info.root)
                match = next((value for value in branches if value.ref == base), None)
                if match is None or match.occupied_paths:
                    raise GitError("create", "local branch is missing or already checked out", outcome="not_started")
            else:
                base = ref or (await self.default_base(info.root) if agent_default else "HEAD")
            _validate_text(base, "ref")
            _, commit_output = await self._run(info.root, "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}")
            commit = commit_output.strip()
            if info.relative_cwd != Path("."):
                _, entry = await self._run(info.root, "ls-tree", "-z", commit, "--", str(info.relative_cwd))
                if not entry.startswith("040000 tree "):
                    raise GitError("create", "selected commit does not contain the source subdirectory", outcome="not_started")
            filters = await self._filters(info.root)
            args = ["worktree", "add", "--no-checkout"]
            if mode == "detached":
                args.append("--detach")
            elif mode == "new":
                args.extend(("-b", str(branch)))
            args.extend(("--", str(target), str(branch) if mode == "existing" else commit))
            add_started = True
            await self._run(info.root, *args, mutating=True, options=filters)
            # Never reset an existing branch to the previously-read tip: another Git
            # client may have advanced it before worktree add acquired the branch.
            # Conditional configuration can enable filters only in this checkout.
            filters = await self._filters(target)
            await self._run(target, "reset", "--hard", "--no-recurse-submodules", "HEAD", mutating=True, options=filters)
            _, git_output = await self._run(target, "rev-parse", "--absolute-git-dir")
            git_dir = Path(git_output.strip()).resolve(strict=True)
            actual_cwd = target / info.relative_cwd
            if not actual_cwd.is_dir() or actual_cwd.resolve() != actual_cwd:
                raise GitError("create", "created checkout has no safe source subdirectory", outcome="unknown")
            identity = WorktreeIdentity(target, actual_cwd, info.common_dir, git_dir, _identity_token(target, git_dir))
            state = await self.inspect(identity)
            if state.status != "ready" or state.head is None:
                raise GitError("create", state.detail or "created identity could not be verified", outcome="unknown")
            return CreatedWorktree(identity, state.head, state.branch)
        except GitError as error:
            # Fetch can have updated refs; not_started only proves this call has
            # not attempted worktree add, never that all Git state is unchanged.
            raise GitError(error.operation, error.detail,
                           outcome="unknown" if add_started else "not_started") from error

    def _check_identity(self, identity: WorktreeIdentity) -> bool:
        """Return whether the root is missing; reject replacements, never adopt."""
        root_key, git_key, backlink_key, marker_key = _token_parts(identity.identity_token)
        for path in (identity.root, identity.cwd, identity.common_dir, identity.git_dir):
            if not path.is_absolute() or path.is_symlink() or path.resolve() != path:
                raise ValueError("recorded path is noncanonical or replaced by a symlink")
        try:
            identity.cwd.relative_to(identity.root)
            relative_git = identity.git_dir.relative_to(identity.common_dir / "worktrees")
            if len(relative_git.parts) != 1:
                raise ValueError("invalid linked-worktree metadata path")
        except ValueError as error:
            raise ValueError("recorded worktree paths do not belong together") from error
        try:
            identity.root.stat()
            missing = False
        except FileNotFoundError:
            missing = True
        if not missing and _stat_key(identity.root) != root_key:
            raise ValueError("checkout directory was replaced")
        try:
            current_git_key = _stat_key(identity.git_dir)
        except FileNotFoundError:
            if missing:
                return True
            raise ValueError("worktree registration is missing") from None
        if current_git_key != git_key:
            raise ValueError("worktree registration was replaced")
        if (identity.git_dir / "gitdir").is_symlink():
            raise ValueError("worktree registration backlink is a symlink")
        if _file_key(identity.git_dir / "gitdir") != backlink_key:
            raise ValueError("worktree registration backlink was replaced")
        backlink = _read_link_file(identity.git_dir / "gitdir")
        if backlink != str(identity.root / ".git"):
            raise ValueError("worktree registration points to another directory")
        if not missing:
            if identity.root.resolve() != identity.root or identity.cwd.resolve() != identity.cwd:
                raise ValueError("worktree directory is no longer canonical")
            marker = identity.root / ".git"
            if marker.is_symlink() or not marker.is_file():
                raise ValueError("checkout Git identity was replaced")
            if _file_key(marker) != marker_key:
                raise ValueError("checkout Git identity was replaced")
            if _read_link_file(marker) != f"gitdir: {identity.git_dir}":
                raise ValueError("checkout points to another Git registration")
        return missing

    async def inspect(self, identity: WorktreeIdentity) -> WorktreeState:
        try:
            missing = self._check_identity(identity)
            if missing and not identity.git_dir.exists():
                return WorktreeState("missing", detail="checkout and Git registration are absent")
            rows = await self._registrations(identity.common_dir, common=True)
            row = next((item for item in rows if item.get("worktree") == str(identity.root)), None)
            if row is None:
                return WorktreeState("replaced", detail="Git does not register this checkout")
            if missing:
                return WorktreeState("missing", row.get("HEAD"), row.get("branch", "").removeprefix("refs/heads/") or None, "checkout directory is absent")
            if not identity.cwd.is_dir():
                return WorktreeState("missing", detail="session subdirectory is absent; checkout still exists")
            info = await self.inspect_repository(identity.root)
            if info.root != identity.root or info.common_dir != identity.common_dir:
                return WorktreeState("replaced", detail="checkout belongs to another repository")
            return WorktreeState("ready", row.get("HEAD"), row.get("branch", "").removeprefix("refs/heads/") or None)
        except ValueError as error:
            return WorktreeState("replaced", detail=str(error))
        except (OSError, GitError) as error:
            return WorktreeState("unknown", detail=str(error))

    async def preflight_delete(self, identity: WorktreeIdentity) -> DeleteCheck:
        state = await self.inspect(identity)
        if state.status not in {"ready", "missing"}:
            return DeleteCheck(state, False, (state.detail or "identity is unavailable",))
        try:
            if not identity.root.exists() and not identity.git_dir.exists():
                return DeleteCheck(state, True)
            rows = await self._registrations(identity.common_dir, common=True)
            row = next((item for item in rows if item.get("worktree") == str(identity.root)), None)
            if row is None:
                return DeleteCheck(state, False, ("registration changed",))
            reasons: list[str] = []
            if "locked" in row:
                reasons.append("worktree is locked")
            # A missing directory is naturally marked prunable; remove only this
            # verified registration. Other prune reasons must not be guessed away.
            if "prunable" in row and identity.root.exists():
                reasons.append("worktree registration is prunable or invalid")
            head = row.get("HEAD")
            if not head:
                reasons.append("HEAD is unavailable")
            else:
                _, reachable = await self._run(
                    identity.common_dir, "for-each-ref", f"--contains={head}",
                    "--format=%(refname)", "refs/heads", "refs/remotes", "refs/tags",
                    options=(f"--git-dir={identity.common_dir}",),
                )
                if not reachable.strip():
                    reasons.append("HEAD contains commits not retained by a named branch or tag")
            if identity.root.exists():
                filters = await self._filters(identity.root)
                _, index = await self._run(identity.root, "ls-files", "-v", "-z", options=filters)
                if any(entry and (entry[0].islower() or entry[0] == "S") for entry in index.split("\0")):
                    reasons.append("index hides file changes with assume-unchanged or skip-worktree flags")
                _, status = await self._run(
                    identity.root, "status", "--porcelain=v1", "-z",
                    "--untracked-files=all", "--ignored=matching", "--ignore-submodules=none",
                    options=filters,
                )
                if status:
                    reasons.append("checkout contains tracked changes, untracked or ignored files")
            return DeleteCheck(state, not reasons, tuple(reasons))
        except (OSError, GitError) as error:
            return DeleteCheck(WorktreeState("unknown", detail=str(error)), False, (str(error),))

    async def remove(self, identity: WorktreeIdentity) -> None:
        check = await self.preflight_delete(identity)
        if not check.safe:
            raise GitError("remove", "; ".join(check.reasons), outcome="not_started")
        try:
            self._check_identity(identity)
        except (OSError, ValueError) as error:
            raise GitError("remove", str(error), outcome="not_started") from error
        if not identity.root.exists() and not identity.git_dir.exists():
            return
        filters = await self._filters(identity.common_dir)
        if identity.root.exists():
            # Git remove performs its own status check in the target checkout.
            # Source-only configuration does not cover target includeIf drivers.
            filters += await self._filters(identity.root)
        await self._run(
            identity.common_dir, "worktree", "remove", "--", str(identity.root),
            mutating=True, options=(f"--git-dir={identity.common_dir}", *filters),
        )
        if identity.root.exists() or identity.git_dir.exists():
            raise GitError("remove", "Git returned success but resource remains", outcome="unknown")
