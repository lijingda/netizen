import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class GitEnvironmentTest(unittest.TestCase):
    def test_git_fixtures_leave_the_callers_repository_untouched(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            caller = Path(directory).resolve()
            # Even a broken suite bootstrap must only be able to damage this fixture.
            env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
            env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")

            def git(*args):
                return subprocess.run(["git", "-C", str(caller), *args], env=env,
                                      capture_output=True, check=True, timeout=10).stdout

            git("init", "-q", "-b", "caller")
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "commit", "--allow-empty", "-qm", "caller")

            def snapshot():
                return {
                    "head": git("rev-parse", "HEAD"),
                    "objects": git("cat-file", "--batch-all-objects", "--batch-check=%(objectname)"),
                    "config": (caller / ".git" / "config").read_bytes(),
                    "worktrees": git("worktree", "list", "--porcelain"),
                }

            name = "test_each_run_creates_detached_checkout_from_current_project_head"
            for args in (
                ["tests.test_scheduled_worktrees.ScheduledWorktreeTests." + name],
                ["discover", "-s", "tests", "-p", "test_scheduled_worktrees.py", "-k", name],
                # Standalone discovery of a module that does not import tests.* itself.
                ["discover", "-s", "tests", "-p", "test_repository_hygiene.py",
                 "-k", "test_local_operator_profile_is_ignored_and_public_template_is_shipped"],
            ):
                with self.subTest(args=args):
                    before = snapshot()
                    result = subprocess.run(
                        [sys.executable, "-m", "unittest", *args], cwd=root,
                        env={**env, "GIT_DIR": str(caller / ".git"), "GIT_WORK_TREE": str(caller)},
                        capture_output=True, text=True, timeout=60,
                    )
                    self.assertEqual(snapshot(), before)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("Ran 1 test in ", result.stderr)
                    self.assertIn("\nOK\n", result.stderr)
