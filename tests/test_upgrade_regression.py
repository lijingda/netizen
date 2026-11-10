from __future__ import annotations

from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
import asyncio
import io
import itertools
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import tomllib
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import run_upgrade_regression as runner


def no_group(_pid, sig):
    if sig == 0:
        raise ProcessLookupError


class UpgradeRegressionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.script = self.root / "fake.py"
        self.script.write_text('print(\'{"passed": true}\')\n')
        self.step = runner.Step("fake", "isolated", str(self.script))

    def run_steps(self, steps=None, **kwargs):
        with redirect_stdout(io.StringIO()):
            return runner.run_suite(steps or (self.step,), output_dir=self.root, cwd=self.root,
                                    sdk_version="test-version", **kwargs)

    def test_inventory_accounts_for_every_probe_and_has_no_ambiguous_names(self) -> None:
        probes = {path.name for path in (runner.ROOT / "scripts").glob("probe_*.py")}
        included = {step.script for step in runner.REGISTRY}
        excluded = set(runner.EXCLUDED_PROBES)
        self.assertEqual(probes, included | excluded)
        self.assertFalse(included & excluded)
        self.assertTrue(all(reason.strip() for reason in runner.EXCLUDED_PROBES.values()))
        self.assertEqual(len(runner.REGISTRY), len({step.name for step in runner.REGISTRY}))
        for step in runner.REGISTRY:
            self.assertGreater(step.deadline, 0)
            self.assertGreater(step.grace, 0)
            self.assertTrue(step.evidence)
            if step.evidence == "exit-only":
                self.assertTrue(step.weakness)
            if step.group == "native-phases":
                self.assertTrue(step.live)

    def test_isolated_configs_disable_plugin_sync_without_starting_sdk(self) -> None:
        with patch.object(sys, "path", [str(runner.ROOT / "scripts"), *sys.path]):
            from scripts import probe_sdk_partial_answers, probe_skill_roots, probe_account_rate_limits
        with self.subTest(probe="shared"):
            _, env = probe_sdk_partial_answers.isolated_config(self.root, SimpleNamespace(server_port=1234))
            config = tomllib.loads((Path(env["CODEX_HOME"]) / "config.toml").read_text())
            self.assertIs(config.get("features", {}).get("plugins"), False)
            self.assertEqual(config["model_provider"], "native_probe")
        class Captured(Exception):
            pass
        def capture(config):
            payload = tomllib.loads((Path(config.env["CODEX_HOME"]) / "config.toml").read_text())
            self.assertIs(payload.get("features", {}).get("plugins"), False)
            raise Captured
        for module, constructor, kwargs in ((probe_skill_roots, "_codex", {"source": runner.ROOT}),
                                             (probe_account_rate_limits, "AsyncCodex", {"live": False})):
            with self.subTest(probe=module.__name__), patch.object(module, constructor, side_effect=capture):
                with self.assertRaises(Captured):
                    asyncio.run(module.probe(**kwargs))

    def test_real_fake_command_success_and_json_saved(self) -> None:
        rows, _ = self.run_steps()
        self.assertEqual(rows[0]["status"], "passed")
        self.assertEqual(runner.exit_code(rows), 0)
        self.assertEqual(json.loads((self.root / "fake.json").read_text()), {"passed": True})

    def test_exit_and_evidence_failures_continue_to_independent_step(self) -> None:
        good = self.root / "good.py"
        good.write_text('print(\'{"passed": true}\')\n')
        for source in ('raise SystemExit(3)', 'print("not JSON")', 'print("{}")',
                       'print(\'{"passed": false}\')', 'print(\'{"passed": 1}\')'):
            with self.subTest(source=source):
                self.script.write_text(source)
                rows, _ = self.run_steps((self.step, replace(self.step, name="next", script=str(good))))
                self.assertEqual([row["status"] for row in rows], ["failed", "passed"])
                self.assertEqual(runner.exit_code(rows), 1)

    def test_output_file_missing_or_false_is_not_replaced_by_stdout(self) -> None:
        step = replace(self.step, evidence_source="json", name="file-proof")
        rows, _ = self.run_steps((step,))
        self.assertEqual(rows[0]["status"], "failed")
        (self.root / "file-proof.json").write_text('{"passed":false}')
        rows, _ = self.run_steps((step,))
        self.assertEqual(rows[0]["status"], "failed")

    def test_nonzero_exit_with_true_evidence_fails(self) -> None:
        self.script.write_text('print(\'{"passed": true}\'); raise SystemExit(2)')
        rows, _ = self.run_steps()
        self.assertEqual(rows[0]["exit_code"], 2)
        self.assertEqual(rows[0]["status"], "failed")

    def test_skip_and_not_selected_never_launch_a_probe(self) -> None:
        with patch.object(runner, "run_step", side_effect=AssertionError("must not run")):
            rows, _ = self.run_steps(skips={"fake": "covered separately"})
            self.assertEqual(rows[0]["status"], "skipped")
            self.assertEqual(rows[0]["reason"], "covered separately")
            self.assertEqual(json.loads((self.root / "fake.result.json").read_text()), rows[0])
            self.assertEqual(runner.exit_code(rows), 1)
            for kwargs in ({"groups": {"account"}}, {"only": {"another"}}):
                rows, _ = self.run_steps(**kwargs)
                self.assertEqual(rows[0]["status"], "not_selected")
            rows, _ = self.run_steps((replace(self.step, live=True),))
            self.assertEqual(rows[0]["status"], "not_selected")
            self.assertEqual(runner.exit_code(rows), 1)

    def test_explicit_live_selection_without_authorization_is_argument_error(self) -> None:
        for args in (["--only", "sdk-smoke"], ["--group", "native-phases"],
                     ["--only", "sdk-smoke,worktree-cwd"]):
            with (self.subTest(args=args), redirect_stderr(io.StringIO()) as stderr,
                  patch.object(runner, "run_suite", return_value=([], None)),
                  redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as error):
                runner.main(args + ["--output-dir", str(self.root / str(len(args)))])
            self.assertEqual(error.exception.code, 2)
            self.assertIn("sdk-smoke", stderr.getvalue())
            self.assertIn("--live", stderr.getvalue())

    def test_live_skip_reason_survives_without_authorization(self) -> None:
        with patch.object(runner, "run_step", side_effect=AssertionError("must not run")):
            rows, _ = self.run_steps((replace(self.step, live=True),),
                                    only={"fake"}, skips={"fake": "real account deliberately excluded"})
        self.assertEqual(rows[0]["status"], "skipped")
        self.assertEqual(rows[0]["reason"], "real account deliberately excluded")
        self.assertEqual(json.loads((self.root / "fake.result.json").read_text())["reason"], rows[0]["reason"])

    def test_empty_execution_is_nonzero_and_explained_in_summary(self) -> None:
        rows, _ = self.run_steps((replace(self.step, live=True),))
        report = {"steps": rows, "source_dirty": False, "source_after": {"source_dirty": False},
                  "source_commit": "fixture", "sdk_version": "x", "cli_version": "x", "model": None}
        with redirect_stdout(io.StringIO()):
            runner.write_summary(self.root, report)
        self.assertNotEqual(report["exit_code"], 0)
        self.assertIn("没有执行任何步骤", (self.root / "SUMMARY.md").read_text())
        self.assertNotEqual(runner.exit_code([]), 0)

    def test_model_dependency_fails_without_launch_and_nested_default_is_used(self) -> None:
        dependent = replace(self.step, name="dependent", needs_model=True)
        with patch.object(runner, "run_command", side_effect=AssertionError("must not run")):
            rows, model = self.run_steps((dependent,))
        self.assertIsNone(model)
        self.assertEqual(rows[0]["status"], "failed")
        self.assertIn("前置条件失败", rows[0]["reason"])
        models = self.root / "models.py"
        models.write_text('import json, sys\nprint(json.dumps({"openai_codex_version":"test-version", '
                          '"sdk_gap_facade_migrations":[], "models":{"default_model_id":"catalog-id",'
                          '"models":[{"id":"catalog-id", "model":"fixture-model", "is_default":True}]}}))\n'
                          'print("[probe] models: passed", file=sys.stderr)\n')
        native = runner.Step("sdk-models", "native-phases", str(models), ("--phase", "models"), evidence="native")
        self.script.write_text('import sys\nassert sys.argv[1] == "fixture-model"\nprint(\'{"passed": true}\')')
        rows, model = self.run_steps((native, replace(dependent, arguments=("{model}",))))
        self.assertEqual([row["status"] for row in rows], ["passed", "passed"])
        self.assertEqual(model, "fixture-model")
        self.assertEqual(rows[0]["default_model_id"], "catalog-id")
        self.assertEqual(rows[0]["default_model"], "fixture-model")
        # The old, wrong top-level path cannot satisfy the prerequisite.
        models.write_text(models.read_text().replace('"models":{"default_model_id":"catalog-id",'
                           '"models":[{"id":"catalog-id", "model":"fixture-model", "is_default":True}]}',
                           '"default_model_id":"fixture-model"'))
        rows, _ = self.run_steps((native, dependent))
        self.assertEqual([row["status"] for row in rows], ["failed", "failed"])

    def test_model_catalog_requires_unique_default_and_valid_id_and_slug(self) -> None:
        valid = {"id": "catalog-id", "model": "model-slug", "is_default": True}
        for entries in ([], [valid, valid], [{**valid, "is_default": False}], [{**valid, "is_default": 1}],
                        [{**valid, "model": ""}], [{**valid, "model": 1}], [{**valid, "id": None}],
                        [{**valid, "id": " "}], ["malformed"]):
            with self.subTest(entries=entries):
                payload = {"openai_codex_version": "test-version", "sdk_gap_facade_migrations": [],
                           "models": {"default_model_id": "catalog-id", "models": entries}}
                self.script.write_text(f'import sys\nprint({json.dumps(payload)!r})\n'
                                       'print("[probe] models: passed", file=sys.stderr)\n')
                rows, _ = self.run_steps((replace(self.step, name="sdk-models", evidence="native",
                                                  arguments=("--phase", "models")),))
                self.assertEqual(rows[0]["status"], "failed")

    def test_native_evidence_requires_version_migration_and_exact_phase_marker(self) -> None:
        step = replace(self.step, evidence="native", arguments=("--phase", "models"))
        good = {"openai_codex_version": "test-version", "sdk_gap_facade_migrations": []}
        runner.evidence_check(step, good, "[probe] models: passed\n", "test-version")
        for payload, stderr in ((good, "[probe] smoke: passed"),
                                ({**good, "openai_codex_version": "other"}, "[probe] models: passed"),
                                ({**good, "sdk_gap_facade_migrations": ["candidate"]}, "[probe] models: passed")):
            with self.assertRaises(ValueError):
                runner.evidence_check(step, payload, stderr, "test-version")

    def test_cases_require_every_expected_case_and_boolean_proof(self) -> None:
        step = replace(self.step, evidence="cases", case_count=2)
        for cases in ([], [{"passed": True}], [{"passed": True}, {}], [{"passed": True}, {"passed": False}]):
            with self.assertRaises(ValueError):
                runner.evidence_check(step, {"cases": cases}, "", "test-version")
        runner.evidence_check(step, {"cases": [{"passed": True}, {"passed": True}]}, "", "test-version")

    def test_auto_archive_requires_five_scenarios_and_overall_pass(self) -> None:
        step = next(step for step in runner.REGISTRY if step.name == "scheduled-auto-archive")
        self.assertEqual(step.group, "isolated")
        self.assertFalse(step.live)
        cases = [{"passed": True} for _ in range(5)]
        runner.evidence_check(step, {"passed": True, "scenarios": cases}, "", "test-version")
        for payload in ({"scenarios": cases}, {"passed": False, "scenarios": cases},
                        {"passed": True, "scenarios": cases[:-1]},
                        {"passed": True, "scenarios": cases[:-1] + [{"passed": False}]}):
            with self.assertRaises(ValueError):
                runner.evidence_check(step, payload, "", "test-version")

    def test_delete_rejection_requires_all_four_native_cases(self) -> None:
        step = next(step for step in runner.REGISTRY if step.name == "thread-delete-rejection")
        self.assertEqual(step.group, "isolated")
        self.assertFalse(step.live)
        cases = [{"passed": True} for _ in range(4)]
        runner.evidence_check(step, {"cases": cases}, "", "test-version")
        for incomplete in (cases[:-1], cases[:-1] + [{"passed": False}]):
            with self.assertRaises(ValueError):
                runner.evidence_check(step, {"cases": incomplete}, "", "test-version")

    def test_timeout_signals_only_owned_group_with_bounded_grace_and_fails_even_if_zero(self) -> None:
        with (patch.object(runner.subprocess, "Popen") as popen,
              patch.object(runner.os, "killpg", side_effect=no_group) as killpg):
            process = popen.return_value
            process.pid = 12345
            process.returncode = 0
            process.wait.side_effect = [subprocess.TimeoutExpired("fake", 1),
                                        subprocess.TimeoutExpired("fake", 2), 0]
            rows, _ = self.run_steps((replace(self.step, deadline=1, grace=2),))
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            self.assertEqual([call.args for call in killpg.call_args_list if call.args[1]],
                             [(12345, signal.SIGINT), (12345, signal.SIGKILL)])
            self.assertEqual([call.kwargs["timeout"] for call in process.wait.call_args_list], [1, 2, 5])
        self.assertTrue(rows[0]["timed_out"])
        self.assertEqual(rows[0]["status"], "failed")
        self.assertEqual(runner.exit_code(rows), 1)

    def test_interrupt_allows_cleanup_stops_later_steps_and_writes_summary(self) -> None:
        for waits, expected in (([KeyboardInterrupt(), 0], [signal.SIGINT]),
                                ([KeyboardInterrupt(), subprocess.TimeoutExpired("fake", 2), -9],
                                 [signal.SIGINT, signal.SIGKILL])):
            with (self.subTest(expected=expected), patch.object(runner.subprocess, "Popen") as popen,
                  patch.object(runner.os, "killpg", side_effect=no_group) as killpg):
                popen.return_value.pid = 12345
                popen.return_value.wait.side_effect = waits
                rows, _ = self.run_steps((replace(self.step, grace=2), replace(self.step, name="later")))
                self.assertEqual([call.args[1] for call in killpg.call_args_list if call.args[1]], expected)
                self.assertEqual(popen.call_count, 1)
                self.assertEqual(popen.return_value.wait.call_args_list[1].kwargs["timeout"], 2)
            self.assertEqual([row["status"] for row in rows], ["interrupted", "not_run"])
            report = {"steps": rows, "source_dirty": False, "source_after": {"source_dirty": False},
                      "source_commit": "fixture", "sdk_version": "x", "cli_version": "x", "model": None}
            with redirect_stdout(io.StringIO()):
                runner.write_summary(self.root, report)
            self.assertEqual(report["exit_code"], 1)
            self.assertEqual(json.loads((self.root / "SUMMARY.json").read_text())["counts"]["interrupted"], 1)
            self.assertEqual(json.loads((self.root / "later.result.json").read_text())["status"], "not_run")

    def test_termination_signals_use_interrupt_path_and_restore_handlers(self) -> None:
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signum=signum):
                original = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
                def wait(*, timeout):
                    if timeout == self.step.deadline:
                        handler = signal.getsignal(signum)
                        self.assertTrue(callable(handler))
                        handler(signum, None)
                    return 0
                with (patch.object(runner.subprocess, "Popen") as popen,
                      patch.object(runner.os, "killpg", side_effect=no_group) as killpg):
                    popen.return_value.pid = 12345
                    popen.return_value.wait.side_effect = wait
                    rows, _ = self.run_steps((self.step, replace(self.step, name="later")))
                    self.assertEqual([row["status"] for row in rows], ["interrupted", "not_run"])
                    self.assertIn(unittest.mock.call(12345, signal.SIGINT), killpg.call_args_list)
                self.assertEqual({sig: signal.getsignal(sig) for sig in original}, original)

    def test_signal_during_timeout_cleanup_does_not_abandon_owned_group(self) -> None:
        waits = 0
        def wait(*, timeout):
            nonlocal waits
            waits += 1
            if waits == 1:
                raise subprocess.TimeoutExpired("fake", timeout)
            if waits == 2:
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            if waits <= 3:
                raise subprocess.TimeoutExpired("fake", timeout)
            return 0
        with (patch.object(runner.subprocess, "Popen") as popen,
              patch.object(runner.os, "killpg", side_effect=no_group) as killpg):
            popen.return_value.pid = 12345
            popen.return_value.returncode = -9
            popen.return_value.wait.side_effect = wait
            rows, _ = self.run_steps((self.step, replace(self.step, name="later")))
        self.assertEqual([row["status"] for row in rows], ["interrupted", "not_run"])
        self.assertIn(unittest.mock.call(12345, signal.SIGKILL), killpg.call_args_list)

    def test_main_keeps_signal_handlers_until_summary_and_restores_them(self) -> None:
        original = signal.getsignal(signal.SIGTERM)
        write_summary = runner.write_summary
        def summary(output_dir, report):
            handler = signal.getsignal(signal.SIGTERM)
            self.assertTrue(callable(handler))
            handler(signal.SIGTERM, None)
            write_summary(output_dir, report)
        with (patch.object(runner, "REGISTRY", (self.step,)),
              patch.object(runner, "write_summary", side_effect=summary), redirect_stdout(io.StringIO())):
            code = runner.main(["--output-dir", str(self.root / "summary")])
        self.assertNotEqual(code, 0)
        self.assertEqual(signal.getsignal(signal.SIGTERM), original)
        self.assertTrue((self.root / "summary/SUMMARY.json").exists())

    def test_leftover_process_group_is_cleaned_and_warned_before_next_step(self) -> None:
        for stubborn in (False, True):
            with self.subTest(stubborn=stubborn):
                signals = []
                def killpg(pid, sig):
                    if sig:
                        signals.append(sig)
                    if sig == 0 and (signal.SIGKILL in signals or (not stubborn and signal.SIGINT in signals)):
                        raise ProcessLookupError
                with (patch.object(runner.subprocess, "Popen") as popen,
                      patch.object(runner.os, "killpg", side_effect=killpg),
                      patch.object(runner.subprocess, "run", return_value=subprocess.CompletedProcess(
                          [], 0, "12345 S git /private/repo/git fetch https://private.invalid/repo --token SECRET\n")),
                      patch.object(runner.time, "monotonic", side_effect=itertools.count()),
                      patch("threading.Event.wait", return_value=False)):
                    popen.return_value.pid = 12345
                    popen.return_value.wait.return_value = 0
                    steps = (replace(self.step, grace=2, evidence="exit-only"),
                             replace(self.step, name="later", evidence="exit-only"))
                    rows, _ = self.run_steps(steps)
                self.assertEqual([row["status"] for row in rows], ["passed", "passed"])
                self.assertEqual(rows[0]["warnings"][0]["processes"][0]["comm"], "git")
                warning = json.dumps(rows[0]["warnings"])
                for private in ("/private/repo", "private.invalid", "SECRET"):
                    self.assertNotIn(private, warning)
                report = {"steps": rows, "source_dirty": False, "source_after": {"source_dirty": False},
                          "source_commit": "fixture", "sdk_version": "x", "cli_version": "x", "model": None}
                with redirect_stdout(io.StringIO()):
                    runner.write_summary(self.root, report)
                self.assertIn("残留进程警告", (self.root / "SUMMARY.md").read_text())
                self.assertIn("git", (self.root / "SUMMARY.md").read_text())
                self.assertEqual(report["exit_code"], 0)
                self.assertEqual(signals, [signal.SIGINT, signal.SIGKILL] if stubborn else [signal.SIGINT])

    def test_residue_after_sigkill_fails_but_zombie_only_groups_pass(self) -> None:
        for states, expected in (("Z", "passed"), ("Z+", "passed"), ("S", "failed"), ("Z\nS", "failed")):
            with self.subTest(states=states):
                stdout = "".join(f"12345 {state} git git fetch <redacted>\n" for state in states.splitlines())
                with (patch.object(runner.subprocess, "Popen") as popen,
                      patch.object(runner.os, "killpg") as killpg,
                      patch.object(runner.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout)),
                      patch.object(runner.time, "monotonic", side_effect=itertools.count()),
                      patch("threading.Event.wait", return_value=False)):
                    popen.return_value.pid = 12345
                    popen.return_value.wait.return_value = 0
                    rows, _ = self.run_steps((replace(self.step, grace=2, evidence="exit-only"),))
                self.assertEqual(rows[0]["status"], expected)
                sent = [call.args[1] for call in killpg.call_args_list if call.args[1]]
                if expected == "passed":
                    self.assertEqual(sent, [])
                    self.assertFalse(rows[0].get("warnings"))
                else:
                    self.assertEqual(sent, [signal.SIGINT, signal.SIGKILL])
                    self.assertIn("SIGKILL", rows[0]["reason"])
                    self.assertTrue(rows[0]["warnings"])

    def test_exited_group_can_drain_during_grace_without_signals(self) -> None:
        with (patch.object(runner.subprocess, "Popen") as popen,
              patch.object(runner.os, "killpg", side_effect=[None, ProcessLookupError()]) as killpg,
              patch.object(runner.subprocess, "run", return_value=subprocess.CompletedProcess(
                  [], 0, "12345 S python python fixture.py\n")),
              patch.object(runner.time, "monotonic", side_effect=itertools.count()),
              patch("threading.Event.wait", return_value=False)):
            popen.return_value.pid = 12345
            popen.return_value.wait.return_value = 0
            rows, _ = self.run_steps((replace(self.step, grace=5, evidence="exit-only"),))
        self.assertEqual(rows[0]["status"], "passed")
        self.assertEqual([call.args[1] for call in killpg.call_args_list], [0, 0])

    def test_exit_only_is_visible_and_bad_skip_or_unknown_status_cannot_pass(self) -> None:
        step = replace(self.step, evidence="exit-only", weakness="仅退出码")
        self.script.write_text('print("no JSON marker")')
        rows, _ = self.run_steps((step,))
        self.assertEqual(rows[0]["status"], "passed")
        self.assertEqual(rows[0]["weakness"], "仅退出码")
        for row in ({"status": "skipped"}, {"status": "skipped", "reason": "  "}, {"status": "running"}):
            self.assertEqual(runner.exit_code([row]), 1)

    def test_invalid_cli_selection_and_empty_skip_reason_fail_before_execution(self) -> None:
        for args in (["--only", "unknown"], ["--skip", "thread-history="],
                     ["--skip", "thread-history"], ["--only", "thread-history", "--skip", "sdk-models=reason"]):
            with self.subTest(args=args), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                runner.main(args)
            self.assertEqual(error.exception.code, 2)

    def test_summary_separates_counts_and_marks_dirty(self) -> None:
        rows = [{"name": str(i), "status": status, "reason": "fixture"} for i, status in enumerate(
            ("passed", "failed", "skipped", "not_selected"))]
        report = {"steps": rows, "source_dirty": True, "source_after": {"source_dirty": True},
                  "source_commit": "fixture", "sdk_version": "x", "cli_version": "x", "model": None}
        with redirect_stdout(io.StringIO()):
            runner.write_summary(self.root, report)
        self.assertEqual(report["counts"], {"passed": 1, "failed": 1, "skipped": 1, "not_selected": 1,
                                           "interrupted": 0, "not_run": 0})
        self.assertIn("不能作为 exact commit 证据", (self.root / "SUMMARY.md").read_text())
        self.assertTrue((self.root / "SUMMARY.md").read_text().rstrip().endswith(runner.FOOTER))

    def test_summary_warns_on_commit_or_diff_change_even_when_clean(self) -> None:
        for change in ({"source_commit": "new"}, {"source_diff_sha256": "new"}, {}):
            with self.subTest(change=change):
                source = {"source_commit": "old", "source_diff_sha256": "old", "source_dirty": False}
                report = {**source, "source_after": {**source, **change},
                          "steps": [{"name": "test", "status": "passed"}],
                          "sdk_version": "x", "cli_version": "x", "model": None}
                with redirect_stdout(io.StringIO()):
                    runner.write_summary(self.root, report)
                saved = json.loads((self.root / "SUMMARY.json").read_text())
                self.assertEqual(saved["source_changed_during_run"], bool(change))
                self.assertEqual("运行期间源码变化，不能作为 exact commit 证据" in
                                 (self.root / "SUMMARY.md").read_text(), bool(change))

    def test_shared_source_evidence_preserves_probe_fields_and_runner_stat(self) -> None:
        from scripts import source_evidence as shared
        with patch.object(sys, "path", [str(runner.ROOT / "scripts"), *sys.path]):
            from scripts import probe_scheduled_tasks, probe_input_attachments, probe_worktree_cwd
        def git(command, **kwargs):
            self.assertTrue(kwargs["check"])
            self.assertEqual(kwargs["timeout"], 10)
            self.assertTrue(kwargs["capture_output"])
            output = {("diff", "--binary", "HEAD", "--"): b"",
                      ("rev-parse", "HEAD"): b"fixture-commit\n",
                      ("status", "--porcelain"): b" M fixture.py\n",
                      ("diff", "--stat", "HEAD", "--"): b" fixture.py | 1 +\n"}[tuple(command[3:])]
            return subprocess.CompletedProcess(command, 0, output)
        expected = {"source_commit": "fixture-commit", "source_dirty": True,
                    "source_diff_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"}
        with patch.object(shared.subprocess, "run", side_effect=git):
            for consumer in (probe_scheduled_tasks, probe_input_attachments, probe_worktree_cwd, runner):
                self.assertEqual(consumer.source_evidence(), expected)
            self.assertEqual(runner.regression_source_evidence(),
                             {**expected, "tracked_diff_stat": " fixture.py | 1 +\n"})


if __name__ == "__main__":
    unittest.main()
