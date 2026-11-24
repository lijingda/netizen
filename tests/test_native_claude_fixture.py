import asyncio
from contextlib import asynccontextmanager
from importlib.util import find_spec
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts.check_native_claude import fixture_environment, live_environment, live_refusal, validate_worker_fixture
from tests.support.claude_live_smoke import TurnBudget


class ClaudeIsolationTests(unittest.TestCase):
    @unittest.skipUnless(find_spec("claude_agent_sdk"), "requires the Claude fixture")
    def test_scenario_failures_report_location_and_condition_without_content(self):
        from tests.support import claude_channel_e2e as harness

        class Driver:
            def submissions(self):
                return 0

            reported_turns = submissions

            async def example(self):
                assert False, "private scenario detail"

            async def wait_timeout(self):
                await harness.wait_for("held_foreground", lambda: False, seconds=0)

            async def enclosing_timeout(self):
                async with asyncio.timeout(0):
                    await harness.wait_for("held_foreground", lambda: False)

            async def unexpected(self):
                def predicate():
                    raise ValueError("private scenario detail")
                await harness.wait_for("stale_rejection", predicate)

        @asynccontextmanager
        async def driver(*args):
            yield Driver()

        for name, error, condition, file in (
            ("example", AssertionError, "example", Path(__file__).name),
            ("wait_timeout", TimeoutError, "held_foreground", "claude_channel_e2e.py"),
            ("enclosing_timeout", TimeoutError, "held_foreground", "claude_channel_e2e.py"),
            ("unexpected", ValueError, "stale_rejection", Path(__file__).name),
        ):
            rows = []
            with (self.subTest(name=name), patch.object(harness, "CASES", [name]),
                  patch.object(harness, "channel_driver", driver)):
                with self.assertRaises(error):
                    asyncio.run(harness.run_cases(None, report=lambda row, **kw: rows.append(row)))
                report = json.loads(rows[0])
                self.assertEqual((report["error"], report["condition"], report["file"]), (error.__name__, condition, file))
                self.assertGreater(report["line"], 0)
                if name == "example":
                    self.assertEqual(report["line"], Driver.example.__code__.co_firstlineno + 1)
                self.assertNotIn("private scenario detail", rows[0])
                self.assertNotIn(str(Path(__file__).parent), rows[0])

    @unittest.skipUnless(find_spec("claude_agent_sdk"), "requires the Claude fixture")
    def test_exact_stop_helper_waits_for_observed_init_and_fails_on_early_result(self):
        from tests.support import claude_channel_e2e as harness

        async def exercise(cwd):
            runtime = Mock()
            parent = harness.psutil.Process(os.getppid())
            call = SimpleNamespace(inputs=["human"], session="session", processes=[parent],
                                   attributed=False, finished=asyncio.Event())
            driver = harness.Driver(cwd, SimpleNamespace(_claude=runtime), Mock(), None,
                                    SimpleNamespace(calls=[call]), [], None, None)
            observed = asyncio.Event()
            initialized = False
            def activity(binding_id, *, thread_id, turn_id):
                self.assertEqual((thread_id, turn_id), ("session", "human"))
                observed.set()
                return object() if initialized else None
            runtime.turn_activity.side_effect = activity
            pid_file = cwd / "fixture-foreground-b.pid"
            pid_file.write_text(str(os.getpid()))
            waiting = asyncio.create_task(driver.ready_tool("foreground-b", initialized_turn=call))
            try:
                await asyncio.wait_for(observed.wait(), 5)
                self.assertFalse(waiting.done())
                initialized = True
                self.assertEqual((await asyncio.wait_for(waiting, 5)).pid, os.getpid())
            finally:
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
            call.attributed = True
            with self.assertRaises(AssertionError):
                await asyncio.wait_for(driver.ready_tool("foreground-b", initialized_turn=call), 5)

        with tempfile.TemporaryDirectory() as temp:
            asyncio.run(exercise(Path(temp)))

    @unittest.skipUnless(find_spec("claude_agent_sdk") and find_spec("aiohttp"), "requires the Claude loopback fixture")
    def test_intermediate_idle_cannot_split_a_notification_batch_from_its_result(self):
        from claude_agent_sdk import ResultMessage, SystemMessage
        from tests.support.claude_session_facts import QuiescenceProbe
        trace = QuiescenceProbe(None, None, "three")
        def membership(*ids):
            return SystemMessage("background_tasks_changed", {"tasks": [{"task_id": i, "run_id": i} for i in ids]})
        idle = SystemMessage("session_state_changed", {"state": "idle"})
        result = ResultMessage("success", 1, 1, False, 1, "session", result="batch-answer", origin={"runId": "B"})
        trace.frames = [membership("B", "C"), membership("C"), result, idle]
        trace.batches = [(2, {"B"}, "batch-answer")]
        trace.check_removals()
        other = ResultMessage("success", 1, 1, False, 1, "session", result="unrelated")
        trace.frames[2:2] = [other, idle]
        with self.assertRaises(AssertionError):
            trace.check_removals()

    @unittest.skipUnless(find_spec("claude_agent_sdk") and find_spec("aiohttp"), "requires the Claude loopback fixture")
    def test_receipt_cannot_hide_a_later_batch_for_the_same_removed_task(self):
        from claude_agent_sdk import ResultMessage, SystemMessage
        from tests.support.claude_session_facts import QuiescenceProbe
        trace = QuiescenceProbe(None, None, "three")
        def tasks(*ids):
            return SystemMessage("background_tasks_changed", {"tasks": [{"task_id": i, "run_id": i} for i in ids]})
        idle = SystemMessage("session_state_changed", {"state": "idle"})
        receipt = ResultMessage("success", 1, 1, False, 1, "session", origin={"runId": "B"})
        answer = ResultMessage("success", 1, 1, False, 1, "session", result="both")
        trace.frames = [tasks("B", "C"), tasks("C"), receipt, tasks(), answer, idle]
        trace.batches = [(4, {"B", "C"}, "both")]
        trace.check_removals()
        trace.frames.insert(3, idle)
        trace.batches = [(5, {"B", "C"}, "both")]
        with self.assertRaises(AssertionError):
            trace.check_removals()

    def test_live_requires_explicit_local_opt_in_and_only_passes_login_environment(self):
        env = {"NETIZEN_CLAUDE_LIVE": "1", "HOME": "/owner", "PATH": "/usr/bin", "EXTRA": "x",
               "ANTHROPIC_API_KEY": "fixture", "CLAUDE_CONFIG_DIR": "/elsewhere"}
        self.assertIsNone(live_refusal(env))
        self.assertEqual(live_refusal({**env, "CI": ""}), "refused_ci")
        self.assertEqual(live_refusal({}), "refused_missing_opt_in")
        self.assertEqual(live_environment(env), {"NETIZEN_CLAUDE_LIVE": "1", "HOME": "/owner", "PATH": "/usr/bin"})

    def test_live_reserves_native_turn_limit_before_start(self):
        budget = TurnBudget()
        self.assertEqual(budget.reserve(29), 29)
        with self.assertRaises(RuntimeError):
            budget.reserve(2)
        with self.assertRaises(RuntimeError):
            budget.reserve(1)
        self.assertEqual(budget.reserved, 29)
        budget = TurnBudget()
        self.assertEqual(budget.reserve(30), 30)
        with self.assertRaises(RuntimeError):
            budget.reserve(1)
        self.assertEqual(budget.reserved, 30)

    def test_home_credentials_and_traffic_are_fixture_scoped(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "other", "CLAUDE_CODE_OAUTH_TOKEN": "other", "EXTRA_ENV": "other"}):
            config = fixture_environment(Path("/test-home"))
        self.assertEqual(config["HOME"], "/test-home")
        self.assertEqual(config["CLAUDE_CONFIG_DIR"], "/test-home/.claude")
        self.assertEqual(config["ANTHROPIC_API_KEY"], "synthetic-fixture")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", config)
        self.assertNotIn("EXTRA_ENV", config)
        self.assertEqual(config["ANTHROPIC_BASE_URL"], "http://127.0.0.1:9")
        self.assertEqual(config["HTTP_PROXY"], config["HTTPS_PROXY"])
        self.assertEqual(config["NO_PROXY"], "127.0.0.1,localhost")
        self.assertTrue(all(config[name] == "1" for name in (
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "DISABLE_TELEMETRY", "DISABLE_AUTOUPDATER", "DISABLE_ERROR_REPORTING",
        )))

    def test_worker_resolves_home_and_removes_only_darwin_launcher_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root).resolve()
            alias = home / "home-alias"
            alias.symlink_to(home, target_is_directory=True)
            cwd = home / "project"
            cwd.mkdir()
            extras = {"__CF_USER_TEXT_ENCODING": "fixture", "__PYVENV_LAUNCHER__": "fixture"}
            env = fixture_environment(alias) | extras
            validate_worker_fixture(cwd, env, "darwin")
            self.assertEqual(env, fixture_environment(alias))
            with self.assertRaisesRegex(RuntimeError, "unexpected keys="):
                validate_worker_fixture(cwd, env | extras, "linux")
            with self.assertRaisesRegex(RuntimeError, "unexpected keys=EXTRA_ENV"):
                validate_worker_fixture(cwd, env | {"EXTRA_ENV": "fixture"}, "darwin")

    def test_required_paths_values_and_keys_fail_with_names_only(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root).resolve()
            cfg = fixture_environment(home)
            cfg["ANTHROPIC_API_KEY"] = "secret-test-value"
            del cfg["HTTPS_PROXY"]
            with self.assertRaises(RuntimeError) as caught:
                validate_worker_fixture(home, cfg, "darwin")
            message = str(caught.exception)
            for text in ("working directory", "missing keys=HTTPS_PROXY", "mismatched keys=ANTHROPIC_API_KEY",
                         "resolved cwd.parent == resolved HOME: False"):
                self.assertIn(text, message)
            self.assertNotIn("secret-test-value", message)
            self.assertNotIn(str(home), message)
