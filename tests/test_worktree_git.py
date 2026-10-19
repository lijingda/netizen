from __future__ import annotations

import asyncio
from dataclasses import replace
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from netizen_cli.worktrees.git import GitError, NotGitRepository, WorktreeGit


@unittest.skipUnless(shutil.which("git"), "Git is required")
class WorktreeGitTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="netizen-worktree-git-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.repo = self.base / "source"
        self.repo.mkdir()
        self.managed = self.base / "managed"
        self.managed.mkdir()
        self.git(self.repo, "init", "-b", "main")
        self.git(self.repo, "config", "user.email", "test@example.invalid")
        self.git(self.repo, "config", "user.name", "Worktree Test")
        (self.repo / "file.txt").write_text("committed\n")
        (self.repo / ".gitignore").write_text("ignored.txt\n")
        (self.repo / "sub").mkdir()
        (self.repo / "sub" / "file.txt").write_text("subdirectory\n")
        self.git(self.repo, "add", ".")
        self.git(self.repo, "commit", "-m", "initial")
        # Use the production command budget for real filesystem/process work;
        # timeout behavior itself is covered with an explicitly blocked fake.
        self.backend = WorktreeGit()

    def git(self, cwd: Path, *args: str) -> str:
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-C", str(cwd), *args],
            env=env, stdin=subprocess.DEVNULL, capture_output=True, check=True,
        )
        return result.stdout.decode().strip()

    async def create(self, name: str = "one", **kwargs):
        return await self.backend.create(self.repo, self.managed / name, **kwargs)

    async def test_detached_checkout_preserves_source_and_does_not_copy_local_files(self) -> None:
        (self.repo / "file.txt").write_text("unstaged\n")
        (self.repo / "sub" / "file.txt").write_text("staged\n")
        self.git(self.repo, "add", "sub/file.txt")
        (self.repo / "untracked.txt").write_text("untracked")
        (self.repo / "ignored.txt").write_text("ignored")
        before = self.git(self.repo, "status", "--porcelain", "--ignored")
        result = await self.create()
        self.assertIsNone(result.branch)
        self.assertEqual((result.identity.root / "file.txt").read_text(), "committed\n")
        self.assertEqual((result.identity.root / "sub/file.txt").read_text(), "subdirectory\n")
        self.assertFalse((result.identity.root / "untracked.txt").exists())
        self.assertFalse((result.identity.root / "ignored.txt").exists())
        self.assertEqual(self.git(self.repo, "status", "--porcelain", "--ignored"), before)
        self.assertEqual((await self.backend.inspect(result.identity)).status, "ready")

    async def test_non_git_is_distinct_from_missing_unreadable_and_unavailable_repository(self) -> None:
        plain = self.base / "plain"
        plain.mkdir()
        with self.assertRaises(NotGitRepository):
            await self.backend.inspect_repository(plain)
        (plain / ".git").write_text(f"gitdir: {self.base / 'missing-metadata'}\n")
        with self.assertRaises(GitError) as broken:
            await self.backend.inspect_repository(plain)
        self.assertNotIsInstance(broken.exception, NotGitRepository)
        bare = self.base / "bare.git"
        bare.mkdir()
        self.git(bare, "init", "--bare")
        with self.assertRaises(NotGitRepository):
            await self.backend.inspect_repository(bare)
        with self.assertRaises(GitError) as missing:
            await self.backend.inspect_repository(self.base / "absent")
        self.assertNotIsInstance(missing.exception, NotGitRepository)
        for detail in (
            "fatal: detected dubious ownership in repository",
            "fatal: cannot open .git: Permission denied",
            "Git timed out",
        ):
            with patch.object(self.backend, "_run", side_effect=GitError("rev-parse", detail)):
                with self.assertRaises(GitError) as raised:
                    await self.backend.inspect_repository(self.repo)
                self.assertNotIsInstance(raised.exception, NotGitRepository)

    async def test_subdirectory_is_preserved_and_missing_base_subdirectory_fails_before_creation(self) -> None:
        result = await self.backend.create(self.repo / "sub", self.managed / "nested")
        self.assertEqual(result.identity.cwd, result.identity.root / "sub")
        self.git(self.repo, "rm", "-r", "sub")
        self.git(self.repo, "commit", "-m", "remove subdirectory")
        (self.repo / "sub").mkdir()
        with self.assertRaisesRegex(GitError, "does not contain") as raised:
            await self.backend.create(self.repo / "sub", self.managed / "missing")
        self.assertEqual(raised.exception.outcome, "not_started")
        self.assertFalse((self.managed / "missing").exists())

    async def test_branches_report_occupancy_and_existing_checkout_never_steals_branch(self) -> None:
        self.git(self.repo, "branch", "available")
        branches = await self.backend.list_branches(self.repo)
        main = next(item for item in branches if item.name == "main")
        self.assertEqual(main.occupied_paths, (self.repo,))
        with self.assertRaisesRegex(GitError, "already checked out") as raised:
            await self.create(mode="existing", branch="main")
        self.assertEqual(raised.exception.outcome, "not_started")
        result = await self.create(mode="existing", branch="available")
        self.assertEqual(result.branch, "available")
        with self.assertRaisesRegex(GitError, "already checked out"):
            await self.create("two", mode="existing", branch="available")
        self.assertEqual(self.git(self.repo, "branch", "--show-current"), "main")

    async def test_new_branch_and_revision_tag_and_option_safety(self) -> None:
        self.git(self.repo, "tag", "-a", "v1", "-m", "base")
        result = await self.create(mode="new", branch="feature/new", ref="v1")
        self.assertEqual(result.branch, "feature/new")
        self.assertEqual(result.head, self.git(self.repo, "rev-parse", "HEAD"))
        for value in ("--force", "bad\nbranch", "refs/heads/ambiguous"):
            with self.subTest(value=value), self.assertRaises(GitError) as raised:
                await self.create("invalid", mode="new", branch=value)
            self.assertEqual(raised.exception.outcome, "not_started")
        with self.assertRaises(GitError) as raised:
            await self.create("invalid-ref", ref="--help")
        self.assertEqual(raised.exception.outcome, "not_started")
        self.assertFalse((self.managed / "invalid-ref").exists())

    async def test_new_branch_collision_is_rejected_before_worktree_add(self) -> None:
        with self.assertRaisesRegex(GitError, "already exists") as raised:
            await self.create(mode="new", branch="main")
        self.assertEqual(raised.exception.outcome, "not_started")
        self.assertFalse((self.managed / "one").exists())

    async def test_agent_default_uses_cached_default_not_current_head_and_never_fetches(self) -> None:
        base = self.git(self.repo, "rev-parse", "HEAD")
        self.git(self.repo, "checkout", "-b", "feature")
        (self.repo / "file.txt").write_text("feature\n")
        self.git(self.repo, "commit", "-am", "feature")
        model = await self.create("model", agent_default=True)
        human = await self.create("human")
        self.assertEqual(model.head, base)
        self.assertNotEqual(human.head, base)
        self.git(self.repo, "update-ref", "refs/remotes/other/stable", human.head)
        self.git(self.repo, "symbolic-ref", "refs/remotes/other/HEAD", "refs/remotes/other/stable")
        self.assertEqual(await self.backend.default_base(self.repo), "refs/remotes/other/stable")
        self.git(self.repo, "update-ref", "refs/remotes/origin/stable", base)
        self.git(self.repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/stable")
        self.assertEqual(await self.backend.default_base(self.repo), "refs/remotes/origin/stable")

    async def test_agent_default_without_cached_branch_requires_explicit_ref(self) -> None:
        self.git(self.repo, "branch", "-m", "trunk")
        with self.assertRaisesRegex(GitError, "no cached default"):
            await self.create(agent_default=True)
        self.assertFalse((self.managed / "one").exists())

    async def test_fetch_failure_has_no_checkout_and_does_not_fallback(self) -> None:
        self.git(self.repo, "remote", "add", "origin", str(self.base / "absent.git"))
        with self.assertRaises(GitError) as raised:
            await self.create(fetch=True)
        self.assertEqual(raised.exception.outcome, "not_started")
        self.assertFalse((self.managed / "one").exists())

    async def test_hooks_filters_and_inherited_git_selectors_do_not_run_or_redirect(self) -> None:
        marker = self.base / "hook-was-run"
        hook = self.repo / ".git/hooks/post-checkout"
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
        hook.chmod(0o755)
        filter_marker = self.base / "filter-was-run"
        (self.repo / ".gitattributes").write_text("file.txt filter=test\n")
        self.git(self.repo, "add", ".gitattributes")
        self.git(self.repo, "commit", "-m", "attributes")
        self.git(self.repo, "config", "filter.test.smudge", f"touch '{filter_marker}'; cat")
        self.git(self.repo, "config", "filter.test.clean", f"touch '{filter_marker}'; cat")
        self.git(self.repo, "config", "filter.test.required", "true")
        with patch.dict(os.environ, {
            "GIT_DIR": str(self.base / "invalid"), "GIT_INDEX_FILE": str(self.base / "wrong-index"),
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.worktree", "GIT_CONFIG_VALUE_0": str(self.base),
        }):
            result = await self.create()
            self.assertTrue((await self.backend.preflight_delete(result.identity)).safe)
        self.assertFalse(marker.exists())
        self.assertFalse(filter_marker.exists())
        self.assertFalse((self.base / "wrong-index").exists())

    async def test_destination_only_smudge_filters_are_disabled_for_each_checkout_mode(self) -> None:
        (self.repo / ".gitattributes").write_text("file.txt filter=Late.Driver\n")
        self.git(self.repo, "add", ".gitattributes")
        self.git(self.repo, "commit", "-m", "attributes")
        for mode in ("new", "existing", "detached"):
            with self.subTest(mode=mode):
                marker = self.base / f"smudge-{mode}"
                config = self.base / f"filter-{mode}.config"
                self.git(self.repo, "config", "--file", str(config), "filter.Late.Driver.smudge", f"touch '{marker}'; cat")
                self.git(self.repo, "config", "--file", str(config), "filter.Late.Driver.required", "true")
                branch = f"feature-{mode}"
                if mode == "existing":
                    self.git(self.repo, "branch", branch)
                condition = (f"gitdir:{self.repo / '.git/worktrees'}/" if mode == "detached"
                             else f"onbranch:{branch}")
                self.git(self.repo, "config", f"includeIf.{condition}.path", str(config))
                before = {path: path.read_bytes() for path in (self.repo / ".git/config", config)}
                result = await self.create(mode, mode=mode,
                    **({"branch": branch} if mode != "detached" else {}))
                self.assertFalse(marker.exists())
                self.assertEqual((result.identity.root / "file.txt").read_text(), "committed\n")
                self.assertEqual(result.branch, None if mode == "detached" else branch)
                self.assertTrue((await self.backend.preflight_delete(result.identity)).safe)
                for path, content in before.items():
                    self.assertEqual(path.read_bytes(), content)

    async def test_destination_process_filter_is_disabled_without_mutating_configuration(self) -> None:
        marker = self.base / "process-filter"
        config = self.base / "process.config"
        (self.repo / ".gitattributes").write_text("file.txt filter=late\n")
        self.git(self.repo, "add", ".gitattributes")
        self.git(self.repo, "commit", "-m", "attributes")
        self.git(self.repo, "config", "--file", str(config), "filter.late.process", f"touch '{marker}'; exit 1")
        self.git(self.repo, "config", "--file", str(config), "filter.late.required", "true")
        self.git(self.repo, "config", f"includeIf.gitdir:{self.repo / '.git/worktrees'}/.path", str(config))
        before = config.read_bytes()
        result = await self.create()
        self.assertFalse(marker.exists())
        self.assertEqual((result.identity.root / "file.txt").read_text(), "committed\n")
        self.assertEqual(config.read_bytes(), before)

    async def test_final_remove_disables_target_clean_and_process_filters(self) -> None:
        (self.repo / ".gitattributes").write_text("file.txt filter=late\n")
        self.git(self.repo, "add", ".gitattributes")
        self.git(self.repo, "commit", "-m", "attributes")
        for helper in ("clean", "process"):
            with self.subTest(helper=helper):
                result = await self.create(helper)
                marker = self.base / f"delete-{helper}"
                config = self.base / f"delete-{helper}.config"
                command = f"touch '{marker}'; " + ("cat" if helper == "clean" else "exit 1")
                self.git(self.repo, "config", "--file", str(config), f"filter.late.{helper}", command)
                self.git(self.repo, "config", "--file", str(config), "filter.late.required", "true")
                self.git(self.repo, "config", f"includeIf.gitdir:{result.identity.git_dir}.path", str(config))
                self.assertEqual(self.git(result.identity.root, "config", "--get", f"filter.late.{helper}"), command)
                before = {path: path.read_bytes() for path in (self.repo / ".git/config", config)}
                path = result.identity.root / "file.txt"
                path.write_text("committed\n")
                stat = path.stat()
                os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
                self.assertTrue((await self.backend.preflight_delete(result.identity)).safe)
                self.assertFalse(marker.exists())
                await self.backend.remove(result.identity)
                self.assertFalse(marker.exists())
                self.assertFalse(result.identity.root.exists())
                for path, content in before.items():
                    self.assertEqual(path.read_bytes(), content)

    async def test_attribute_overrides_are_command_local_and_do_not_change_checkout_bytes(self) -> None:
        attributes = self.base / "external.attributes"
        attributes.write_text("file.txt eol=crlf\n")
        self.git(self.repo, "config", "core.attributesFile", str(attributes))
        self.git(self.repo, "config", "attr.tree", "HEAD")
        self.git(self.repo, "config", "core.autocrlf", "false")
        before = (self.repo / ".git/config").read_bytes()
        _, effective = await self.backend._run(self.repo, "config", "--get", "attr.tree")
        self.assertEqual(effective.strip(), "")
        result = await self.create()
        self.assertEqual((result.identity.root / "file.txt").read_bytes(), b"committed\n")
        self.assertEqual((self.repo / ".git/config").read_bytes(), before)
        self.assertEqual(attributes.read_text(), "file.txt eol=crlf\n")

    async def test_filter_discovery_failure_after_add_keeps_unknown_creation_outcome(self) -> None:
        original = self.backend._filters
        async def fail_target(cwd):
            if cwd != self.repo:
                raise GitError("config", "cannot inspect destination filters", outcome="not_started")
            return await original(cwd)
        with patch.object(self.backend, "_filters", side_effect=fail_target):
            with self.assertRaises(GitError) as raised:
                await self.create()
        self.assertEqual(raised.exception.outcome, "unknown")
        self.assertTrue((self.managed / "one" / ".git").is_file())
        self.assertFalse((self.managed / "one" / "file.txt").exists())

    async def test_lightweight_inspection_observes_branch_changes_but_does_not_scan_dirty_files(self) -> None:
        result = await self.create()
        self.git(result.identity.root, "checkout", "-b", "external-change")
        (result.identity.root / "ignored.txt").write_text("must not scan")
        with patch.object(self.backend, "_run", wraps=self.backend._run) as run:
            state = await self.backend.inspect(result.identity)
        self.assertEqual(state.branch, "external-change")
        self.assertEqual(state.status, "ready")
        self.assertNotIn("status", [call.args[1] for call in run.await_args_list])

    async def test_missing_and_recreated_path_are_not_silently_readopted(self) -> None:
        result = await self.create()
        self.git(self.repo, "worktree", "remove", str(result.identity.root))
        self.assertEqual((await self.backend.inspect(result.identity)).status, "missing")
        recreated = await self.create()
        state = await self.backend.inspect(result.identity)
        self.assertEqual(state.status, "replaced")
        self.assertFalse((await self.backend.preflight_delete(result.identity)).safe)
        self.assertEqual((await self.backend.inspect(recreated.identity)).status, "ready")

    async def test_regular_directory_and_symlink_replacement_are_not_removable(self) -> None:
        result = await self.create()
        moved = self.base / "saved-checkout"
        result.identity.root.rename(moved)
        result.identity.root.mkdir()
        self.assertEqual((await self.backend.inspect(result.identity)).status, "replaced")
        result.identity.root.rmdir()
        result.identity.root.symlink_to(moved, target_is_directory=True)
        self.assertEqual((await self.backend.inspect(result.identity)).status, "replaced")
        with self.assertRaises(GitError):
            await self.backend.remove(result.identity)
        self.assertTrue((moved / "file.txt").exists())

    async def test_deletion_checks_tracked_untracked_ignored_and_only_detached_commits(self) -> None:
        for name, relative in (("tracked", "file.txt"), ("untracked", "new.txt"), ("ignored", "ignored.txt")):
            result = await self.create(name)
            (result.identity.root / relative).write_text("keep this")
            check = await self.backend.preflight_delete(result.identity)
            self.assertFalse(check.safe)
            with self.assertRaises(GitError):
                await self.backend.remove(result.identity)
            self.assertTrue(result.identity.root.exists())
        result = await self.create("detached")
        (result.identity.root / "file.txt").write_text("new commit")
        self.git(result.identity.root, "commit", "-am", "detached-only")
        check = await self.backend.preflight_delete(result.identity)
        self.assertFalse(check.safe)
        self.assertIn("not retained", " ".join(check.reasons))
        self.git(result.identity.root, "branch", "saved-result")
        self.assertTrue((await self.backend.preflight_delete(result.identity)).safe)
        await self.backend.remove(result.identity)
        self.assertFalse(result.identity.root.exists())
        self.assertEqual(self.git(self.repo, "rev-parse", "saved-result"), check.state.head)

    async def test_locked_worktree_is_preserved_and_unlocked_clean_worktree_is_removed(self) -> None:
        result = await self.create()
        other = await self.create("other")
        self.git(self.repo, "worktree", "lock", str(result.identity.root))
        self.assertFalse((await self.backend.preflight_delete(result.identity)).safe)
        with self.assertRaises(GitError):
            await self.backend.remove(result.identity)
        self.git(self.repo, "worktree", "unlock", str(result.identity.root))
        await self.backend.remove(result.identity)
        self.assertFalse(result.identity.root.exists())
        self.assertTrue(other.identity.root.exists())
        self.assertTrue(self.repo.exists())

    async def test_missing_checkout_cleanup_removes_only_exact_registration(self) -> None:
        result = await self.create()
        other = await self.create("other")
        # Simulate an external filesystem deletion, not a production fallback.
        shutil.rmtree(result.identity.root)
        self.assertEqual((await self.backend.inspect(result.identity)).status, "missing")
        self.assertTrue((await self.backend.preflight_delete(result.identity)).safe)
        await self.backend.remove(result.identity)
        self.assertFalse(result.identity.git_dir.exists())
        self.assertTrue(other.identity.git_dir.exists())
        await self.backend.remove(result.identity)  # exact already-absent identity is harmless

    async def test_permission_and_timeout_are_unknown_not_missing(self) -> None:
        result = await self.create()
        with patch.object(self.backend, "_check_identity", side_effect=PermissionError("denied")):
            self.assertEqual((await self.backend.inspect(result.identity)).status, "unknown")
        with patch.object(self.backend, "_run", side_effect=GitError("list", "timed out")):
            self.assertEqual((await self.backend.inspect(result.identity)).status, "unknown")
        invalid = replace(result.identity, identity_token="invalid")
        self.assertEqual((await self.backend.inspect(invalid)).status, "unknown")

    async def test_mutation_failure_reports_unknown_and_keeps_created_resource(self) -> None:
        original = self.backend._run

        for outcome in ("not_started", "failed", "unknown"):
            async def failing_reset(cwd, *args, **kwargs):
                if args[0] == "reset":
                    raise GitError("reset", "injected failure", outcome=outcome)
                return await original(cwd, *args, **kwargs)

            with self.subTest(outcome=outcome), patch.object(self.backend, "_run", side_effect=failing_reset):
                with self.assertRaises(GitError) as raised:
                    await self.create(outcome)
                self.assertEqual(raised.exception.outcome, "unknown")
                self.assertTrue((self.managed / outcome / ".git").is_file())

    async def test_deletion_rejects_hidden_index_changes(self) -> None:
        for flag in ("--assume-unchanged", "--skip-worktree"):
            result = await self.create(flag.removeprefix("--"))
            self.git(result.identity.root, "update-index", flag, "file.txt")
            (result.identity.root / "file.txt").write_text("hidden local result")
            self.assertEqual(self.git(result.identity.root, "status", "--porcelain"), "")
            check = await self.backend.preflight_delete(result.identity)
            self.assertFalse(check.safe)
            self.assertIn("index hides", " ".join(check.reasons))

    async def test_timeout_and_cancellation_kill_only_the_spawned_group_and_reap(self) -> None:
        class BlockedProcess:
            pid = 12345678
            returncode = None

            def __init__(self):
                self.started = asyncio.Event()
                self.killed = asyncio.Event()
                self.reaped = False
                self.stdout = self
                self.stderr = self

            async def read(self, _size):
                self.started.set()
                await self.killed.wait()
                return b""

            async def wait(self):
                await self.killed.wait()
                self.reaped = True
                self.returncode = -9
                return -9

        process = BlockedProcess()
        with patch("netizen_cli.worktrees.git.asyncio.create_subprocess_exec", return_value=process), patch(
            "netizen_cli.worktrees.git.os.killpg", side_effect=lambda *_: process.killed.set()
        ) as kill:
            with self.assertRaises(GitError) as raised:
                await WorktreeGit(timeout=0.02)._run(self.repo, "worktree", "add", mutating=True)
        self.assertEqual(raised.exception.outcome, "unknown")
        self.assertTrue(process.reaped)
        self.assertEqual(kill.call_args.args[0], process.pid)

        process = BlockedProcess()
        with patch("netizen_cli.worktrees.git.asyncio.create_subprocess_exec", return_value=process), patch(
            "netizen_cli.worktrees.git.os.killpg", side_effect=lambda *_: process.killed.set()
        ):
            task = asyncio.create_task(self.backend._run(self.repo, "status"))
            await asyncio.wait_for(process.started.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(process.reaped)


if __name__ == "__main__":
    unittest.main()
