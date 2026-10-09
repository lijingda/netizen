from __future__ import annotations

import contextlib
import fcntl
import hashlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from netizen_cli import cli_update, cli_update_worker, cli_update_restore
from netizen_cli.cli_services import ServiceBinding, ServiceManager
from netizen_cli.deployment.update_protocol import acquire_install_lock, new_cli_restart_operation, write_operation
from netizen_cli.instance import INSTANCE_ROOT_MARKER_CONTENT


def prepare_root(root: Path) -> None:
    root.mkdir(mode=0o700)
    (root / "state").mkdir(mode=0o700)
    marker = root / ".netizen-root"
    marker.write_bytes(INSTANCE_ROOT_MARKER_CONTENT)
    marker.chmod(0o600)


def human_report(report: dict) -> str:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        cli_update_worker.print_report(report)
    return output.getvalue()


class BrokenProgressStream(io.StringIO):
    def write(self, text: str) -> int:
        raise BrokenPipeError("progress consumer closed")


class FakeManager:
    def __init__(self, roots: dict[str, bool]) -> None:
        self.platform = "linux"
        self.roots = dict(roots)
        self.loaded = dict.fromkeys(roots, True)
        self.actions: list[tuple[str, str]] = []
        self.stop_failure: str | None = None
        self.query_failure: str | None = None

    def service_name(self, root: Path) -> str:
        return f"netizen-{root.name}.service"

    def inspect(self, root: Path) -> SimpleNamespace:
        if str(root) == self.query_failure:
            raise RuntimeError("manager state unavailable")
        return SimpleNamespace(
            binding=SimpleNamespace(root=root, prefix=Path(sys.prefix), python=Path(sys.executable)),
            running=self.roots[str(root)], ready=self.roots[str(root)], enabled=True,
            loaded=self.loaded[str(root)],
        )

    def list_instances(self, *, prefix: Path | None = None) -> list[SimpleNamespace]:
        return [self.inspect(Path(root)) for root in self.roots]

    def stop(self, root: Path) -> SimpleNamespace:
        self.actions.append(("stop", str(root)))
        if str(root) == self.stop_failure:
            raise RuntimeError("process did not exit")
        self.roots[str(root)] = False
        if self.platform == "darwin":
            self.loaded[str(root)] = False
        return self.inspect(root)

    def start(self, root: Path) -> SimpleNamespace:
        self.actions.append(("start", str(root)))
        self.roots[str(root)] = True
        self.loaded[str(root)] = True
        return self.inspect(root)


class UpdateOrchestrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.manager = FakeManager({str(self.home / "a"): True, str(self.home / "b"): True,
                                    str(self.home / "c"): False})
        for root in self.manager.roots:
            prepare_root(Path(root))
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    def package(self, *, work_dir: Path, backend: str | None = None) -> dict:
        return {
            "prefix": sys.prefix, "environment_python": sys.executable,
            "backend": backend or "pip", "before_version": "1.0", "changes_required": True,
            "command": ["package-tool"], "command_cwd": str(work_dir),
            "validation_command": ["verify-new-install"],
        }

    def invoke(self, prepare=None, executor=None) -> tuple[int, dict]:
        with (contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr),
              patch.dict(os.environ, {"NETIZEN_CLI_SERVICE": "0"})):
            code = cli_update.run_update(
                manager=self.manager, prepare=prepare or self.package, home=self.home,
                executor=executor or (lambda *_: (_ for _ in ()).throw(OSError("exec failed"))),
                json_output=True,
            )
        return code, json.loads(self.stdout.getvalue())

    def test_preflight_noop_does_not_stop_or_launch_anything(self) -> None:
        def prepare(**kwargs):
            return {**self.package(**kwargs), "changes_required": False}
        code, report = self.invoke(prepare)
        self.assertEqual(code, 0)
        self.assertEqual(self.manager.actions, [])
        self.assertEqual(report["package"]["state"], "unchanged")
        self.assertFalse(report["package"]["replacement_started"])
        self.assertEqual([row["state"] for row in report["instances"]], ["running", "running", "stopped"])
        output = human_report(report)
        self.assertIn("No package changes required under the current package-manager configuration", output)
        self.assertIn("No instances were stopped or restarted", output)
        self.assertNotIn("Not executed", output)
        self.assertNotIn("latest", output)
        self.assertIn("leave unchanged", self.stderr.getvalue())
        self.assertNotIn("stop and restore", self.stderr.getvalue())

    def test_progress_precedes_preflight_and_each_stop_with_json_stdout(self) -> None:
        def prepare(**kwargs):
            output = self.stderr.getvalue()
            self.assertIn(f"Python: {sys.executable}", output)
            self.assertIn(f"Environment: {Path(sys.prefix).resolve()}", output)
            self.assertIn("Checking installation and package changes...", output)
            return self.package(**kwargs)

        original_stop = self.manager.stop
        def stop(root):
            output = self.stderr.getvalue()
            self.assertIn(f"Stopping {root}...", output)
            for _, previous in self.manager.actions:
                self.assertIn(f"Stopped: {previous}.", output)
            return original_stop(root)

        with patch.object(self.manager, "stop", side_effect=stop):
            _, report = self.invoke(prepare)
        self.assertEqual(report["progress"]["stopped"], 2)
        self.assertEqual(len(self.stdout.getvalue().splitlines()), 1)
        self.assertEqual(json.loads(Path(report["report_path"]).read_text()), report)

    def test_broken_progress_stream_does_not_prevent_stops_or_handoff(self) -> None:
        self.stderr = BrokenProgressStream()
        code, report = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(report["progress"]["stopped"], 2)
        self.assertEqual(report["reason"], "OSError: exec failed")

    def test_unknown_service_aborts_before_any_stop(self) -> None:
        self.manager.query_failure = str(self.home / "b")
        code, report = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(report["phase"], "preflight")
        self.assertEqual(self.manager.actions, [])
        output = human_report(report)
        self.assertIn("Update failed during preflight checks", output)
        self.assertIn("Instance inventory could not be confirmed", output)
        self.assertIn("Package replacement did not start", output)

    def test_partial_stop_failure_leaves_already_stopped_instance_stopped(self) -> None:
        self.manager.stop_failure = str(self.home / "b")
        code, report = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(report["phase"], "stopping")
        self.assertEqual(self.manager.actions, [("stop", str(self.home / "a")), ("stop", str(self.home / "b"))])
        self.assertEqual([row["state"] for row in report["instances"]], ["stopped", "running", "stopped"])
        self.assertFalse(report["package"]["replacement_started"])
        self.assertEqual(report["progress"]["stopped"], 1)
        output = human_report(report)
        self.assertIn("Update failed during instance shutdown", output)
        self.assertIn(f"{self.home / 'a'}: stopped (not restarted)", output)
        self.assertIn(f"{self.home / 'b'}: running", output)
        self.assertNotIn("No instances were stopped", output)
        self.assertIn(f"Stop failed for {self.home / 'b'}", self.stderr.getvalue())

    def test_handoff_uses_external_worker_base_python_and_exact_original_set(self) -> None:
        captured = {}

        def handoff(executable, argv):
            captured.update(executable=executable, argv=argv,
                            plan=json.loads(Path(argv[-1]).read_text()))
            cli_update_worker._validate_root_locks(captured["plan"])
            raise OSError("synthetic no exec")

        code, report = self.invoke(executor=handoff)
        self.assertEqual(code, 1)
        self.assertEqual(captured["executable"], os.path.abspath(sys._base_executable))
        self.assertEqual(captured["argv"][1:3], ["-I", "-B"])
        self.assertFalse(Path(captured["argv"][-2]).resolve().is_relative_to(Path(sys.prefix)))
        self.assertEqual(sum(item["was_running"] for item in captured["plan"]["report"]["instances"]), 2)
        self.assertTrue(all(not value for value in self.manager.roots.values()))
        self.assertEqual(report["package"]["state"], "unchanged")

    def test_loaded_launchagent_is_stopped_but_not_added_to_running_restore_set(self) -> None:
        self.manager.platform = "darwin"
        calls = []
        captured = {}

        def handoff(_executable, argv):
            plan = json.loads(Path(argv[-1]).read_text())
            def runner(command, **kwargs):
                calls.append(command)
                return subprocess.CompletedProcess(command, 0, stdout='{"version":"2.0"}')
            captured["report"] = cli_update_worker.execute(plan, runner=runner)
            raise OSError("synthetic handoff test")

        self.invoke(executor=handoff)
        report = captured["report"]
        self.assertEqual(report["status"], "succeeded")
        self.assertEqual(report["progress"],
                         {"stop_total": 3, "stopped": 3, "start_total": 2, "ready": 2})
        self.assertFalse(report["instances"][2]["was_running"])
        self.assertEqual(report["instances"][2]["state"], "stopped")
        output = human_report(report)
        self.assertIn("2/2 restored and ready", output)
        self.assertIn(f"{self.home / 'c'}: stopped (kept stopped)", output)
        self.assertEqual(self.manager.actions, [("stop", str(self.home / name)) for name in "abc"])
        self.assertEqual([command[-1] for command in calls[2:]],
                         [str(self.home / name) for name in "ab"])
        self.assertIn(f"{self.home / 'c'}: loaded, no running process -> unload; keep stopped",
                      self.stderr.getvalue())

    def test_reloaded_launchagent_aborts_before_worker_handoff(self) -> None:
        self.manager.platform = "darwin"
        original_inspect = self.manager.inspect
        def inspect(root):
            if root == self.home / "a" and len(self.manager.actions) == 3:
                self.manager.loaded[str(root)] = True
            return original_inspect(root)
        self.manager.inspect = inspect
        code, report = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(report["instances"][0]["state"], "loaded")
        self.assertIn("LaunchAgent is still loaded", report["reason"])
        self.assertFalse(report["package"]["replacement_started"])

    def test_native_loaded_no_pid_services_have_platform_specific_stop_scope(self) -> None:
        for platform in ("linux", "darwin"):
            with self.subTest(platform=platform):
                root = self.home / platform
                prepare_root(root)
                calls = []
                loaded = False

                def native(args, **kwargs):
                    nonlocal loaded
                    calls.append(args)
                    name = manager.service_name(root)
                    path = manager.service_file(root)
                    out, code = "", 0
                    if args[0] == "systemctl":
                        command = args[2]
                        if command == "show":
                            exists = path.exists()
                            props = {
                                "LoadState": "loaded" if exists else "not-found",
                                "ActiveState": "inactive", "SubState": "dead",
                                "MainPID": "0", "ControlPID": "0",
                                "FragmentPath": str(path) if exists else "",
                                "DropInPaths": "", "NeedDaemonReload": "no",
                                "Transient": "no", "UnitFileState": "enabled",
                            }
                            out = "\n".join(f"{key}={value}" for key, value in props.items())
                        elif command in {"list-units", "list-unit-files"}:
                            key = "unit" if command == "list-units" else "unit_file"
                            out = json.dumps([{key: name}])
                    elif args[0] == "launchctl":
                        command = args[1]
                        if command == "list":
                            out = "PID\tStatus\tLabel\n" + (f"-\t1\t{name}\n" if loaded else "")
                        elif command == "print" and args[2].count("/") == 2:
                            code = 0 if loaded else 113
                        elif command == "bootout":
                            loaded = False
                    return subprocess.CompletedProcess(args, code, out, "")

                manager = ServiceManager(home=self.home / f"account-{platform}",
                                         platform=platform, runner=native)
                manager.register(ServiceBinding(root, Path(sys.executable), Path(sys.prefix)))
                loaded = True
                calls.clear()
                self.manager = manager
                self.stdout = io.StringIO()
                _, report = self.invoke()
                self.assertEqual(report["progress"]["stop_total"], 1 if platform == "darwin" else 0)
                self.assertEqual(report["progress"]["stopped"], 1 if platform == "darwin" else 0)
                self.assertEqual(report["progress"]["start_total"], 0)
                self.assertFalse(report["instances"][0]["was_running"])
                if platform == "darwin":
                    self.assertTrue(any(args[1] == "bootout" for args in calls))
                    self.assertFalse(loaded)
                else:
                    self.assertFalse(any(args[2] == "stop" for args in calls))
                self.assertFalse(any("disable" in args for args in calls))

    def test_service_context_is_rejected_even_without_discovery(self) -> None:
        with (patch.dict(os.environ, {"NETIZEN_CLI_SERVICE": "1"}),
              contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr)):
            code = cli_update.run_update(manager=self.manager, prepare=self.package, home=self.home,
                                         json_output=True)
        self.assertEqual(code, 1)
        self.assertEqual(self.manager.actions, [])
        self.assertFalse((self.home / ".cache").exists())
        self.assertIn("external terminal", self.stdout.getvalue())

    def test_concurrent_update_lock_fails_without_stopping(self) -> None:
        directory = cli_update.maintenance_directory(Path(sys.prefix), home=self.home)
        descriptor = cli_update._lock(directory)
        try:
            code, report = self.invoke()
        finally:
            os.close(descriptor)
        self.assertEqual(code, 1)
        self.assertIn("already maintaining", report["reason"])
        self.assertEqual(self.manager.actions, [])

    def test_environment_switch_after_lock_is_rejected(self) -> None:
        def prepare(**kwargs):
            return {**self.package(**kwargs), "prefix": str(self.home / "other-env")}
        code, report = self.invoke(prepare)
        self.assertEqual(code, 1)
        self.assertIn("locked current Python", report["reason"])
        self.assertEqual(self.manager.actions, [])

    def test_observed_external_start_prevents_replacement(self) -> None:
        original_inspect = self.manager.inspect

        def inspect(root):
            if root == self.home / "c" and len(self.manager.actions) == 2:
                self.manager.roots[str(root)] = True
            return original_inspect(root)

        self.manager.inspect = inspect
        code, report = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("started during update", report["reason"])
        self.assertFalse(report["package"]["replacement_started"])

    def test_same_base_interpreter_different_prefix_not_affected(self) -> None:
        status = self.manager.inspect(self.home / "a")
        status.binding.prefix = self.home / "different-env"
        self.manager.list_instances = lambda **_: [status]
        rows = cli_update._snapshot(self.manager, Path(sys.prefix), Path(sys.executable))
        self.assertEqual(rows, [])

    def test_symlinked_maintenance_parent_is_rejected(self) -> None:
        destination = self.home / "foreign-cache"
        destination.mkdir()
        (self.home / ".cache").symlink_to(destination)
        code, report = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("Unsafe update maintenance parent", report["reason"])
        self.assertEqual(list(destination.iterdir()), [])

    def test_service_cgroup_is_rejected_by_exact_component(self) -> None:
        with (patch.dict(os.environ, {"NETIZEN_CLI_SERVICE": "0"}),
              patch.object(cli_update.sys, "platform", "linux"),
              patch.object(Path, "read_text", return_value="0::/user.slice/netizen-a.service/child\n")):
            with self.assertRaises(cli_update.UpdateError):
                cli_update._reject_service_context(["netizen-a.service"])
            cli_update._reject_service_context(["netizen-b.service"])

    def test_any_instance_maintenance_busy_aborts_before_first_stop(self) -> None:
        # A stopped instance is protected too: an Admin restart must not enter it
        # while the shared package is being replaced.
        busy = acquire_install_lock(self.home / "c")
        try:
            code, report = self.invoke()
            self.assertEqual(code, 1)
            self.assertEqual(report["phase"], "preflight")
            self.assertEqual(self.manager.actions, [])
            self.assertIn(str(self.home / "c"), report["reason"])
            # Earlier acquisitions were released on the all-locks preflight failure.
            released = acquire_install_lock(self.home / "a")
            os.close(released)
        finally:
            os.close(busy)

    def test_original_running_set_is_refreshed_under_all_root_locks(self) -> None:
        count = 0
        inventory = self.manager.list_instances
        def list_instances(**kwargs):
            nonlocal count
            count += 1
            if count == 2:
                self.manager.roots[str(self.home / "a")] = False
            return inventory(**kwargs)
        self.manager.list_instances = list_instances
        _, report = self.invoke()
        self.assertEqual(self.manager.actions, [("stop", str(self.home / "b"))])
        self.assertEqual(report["progress"]["stop_total"], 1)
        self.assertEqual([row["was_running"] for row in report["instances"]], [False, True, False])

    def test_dispatched_but_not_yet_locked_admin_restart_blocks_update(self) -> None:
        root = self.home / "b"
        operation = new_cli_restart_operation("1.0.0", "a" * 64)
        write_operation(root, operation)
        code, report = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(self.manager.actions, [])
        self.assertEqual(report["phase"], "preflight")
        self.assertIn("pending maintenance (accepted)", report["reason"])
        self.assertIn("netizen start", report["reason"])

    def test_new_admin_or_cli_maintenance_cannot_enter_during_helper_handoff(self) -> None:
        def handoff(_executable, argv):
            plan = json.loads(Path(argv[-1]).read_text())
            for root in plan["root_locks"]:
                with self.assertRaises(BlockingIOError):
                    acquire_install_lock(Path(root))
            raise OSError("synthetic handoff test")
        self.invoke(executor=handoff)


class UpdateWorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        report = cli_update.new_report()
        report.update(environment=sys.prefix, backend="pip", inventory_complete=True)
        report["report_path"] = str(self.directory / "report.json")
        report["package"]["before_version"] = "1.0"
        report["progress"].update(stop_total=2, stopped=2, start_total=2)
        report["instances"] = [
            {"root": str(self.directory / name), "service": name, "was_running": running,
             "state": "stopped", "action": None, "reason": None,
             "prefix": sys.prefix, "python": sys.executable}
            for name, running in [("a", True), ("b", True), ("c", False)]
        ]
        self.plan = {"protocol": 1, "json_output": True, "report": report, "package_plan": {
            "environment_python": sys.executable, "command": ["package-tool"],
            "command_cwd": str(self.directory), "validation_command": ["validate"],
            "command_env": {"UPDATE_TEST_ONLY": "true"},
        }}
        self.plan["root_locks"] = {}
        for row in report["instances"]:
            root = Path(row["root"])
            prepare_root(root)
            descriptor = acquire_install_lock(root)
            self.addCleanup(os.close, descriptor)
            self.plan["root_locks"][str(root)] = descriptor
        self.calls = []

    def runner(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout='{"version": "2.0"}', stderr="")

    def test_update_validate_then_start_only_original_running_set(self) -> None:
        report = cli_update_worker.execute(self.plan, runner=self.runner)
        self.assertEqual(report["status"], "succeeded")
        self.assertEqual([call[0][0] for call in self.calls], ["package-tool", "validate", sys.executable, sys.executable])
        self.assertEqual([call[0][-1] for call in self.calls[2:]],
                         [str(self.directory / "a"), str(self.directory / "b")])
        self.assertEqual([item["state"] for item in report["instances"]], ["ready", "ready", "stopped"])
        self.assertEqual(report["package"]["state"], "verified")
        self.assertEqual(report["unexecuted"], [])
        self.assertEqual(self.calls[0][1]["env"]["UPDATE_TEST_ONLY"], "true")
        self.assertIn("PATH", self.calls[0][1]["env"])
        self.assertNotIn("pass_fds", self.calls[0][1])
        for call, root in zip(self.calls[2:], ("a", "b")):
            self.assertEqual(call[1]["pass_fds"], (self.plan["root_locks"][str(self.directory / root)],))
        output = human_report(report)
        self.assertIn("Update complete.", output)
        self.assertIn("netizen-cli 1.0 -> 2.0; installation verified", output)
        self.assertIn("2/2 restored and ready", output)

    def test_progress_wraps_raw_package_output_and_precedes_validation_and_restore(self) -> None:
        output = io.StringIO()
        def runner(argv, **kwargs):
            if argv == ["package-tool"]:
                self.assertIn("Updating packages with pip...", output.getvalue())
                self.assertIs(kwargs["stdout"], output)
                self.assertIs(kwargs["stderr"], output)
                print("raw package stdout", file=kwargs["stdout"])
                print("raw package stderr", file=kwargs["stderr"])
            elif argv == ["validate"]:
                self.assertTrue(output.getvalue().endswith("Verifying the installed package...\n"))
            else:
                self.assertIn("Verified netizen-cli 2.0.", output.getvalue())
                self.assertTrue(output.getvalue().endswith(f"Restoring {argv[-1]}; waiting for readiness...\n"))
                if argv[-1] == str(self.directory / "b"):
                    self.assertIn(f"Ready: {self.directory / 'a'}.", output.getvalue())
            return self.runner(argv, **kwargs)

        with contextlib.redirect_stderr(output):
            report = cli_update_worker.execute(self.plan, runner=runner)
        text = output.getvalue()
        self.assertIn("raw package stdout\nraw package stderr\nVerifying", text)
        self.assertNotIn("\r", text)
        self.assertNotIn("\x1b", text)
        self.assertIn(f"Ready: {self.directory / 'b'}.", text)
        self.assertEqual(report["status"], "succeeded")

    def test_progress_write_failure_does_not_change_worker_result(self) -> None:
        with contextlib.redirect_stderr(BrokenProgressStream()):
            report = cli_update_worker.execute(self.plan, runner=self.runner)
        self.assertEqual(report["status"], "succeeded")
        self.assertEqual(report["progress"]["ready"], 2)

    def test_progress_is_flushed_for_noninteractive_readers(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stderr(output), patch.object(output, "flush") as flush:
            cli_update_worker.progress("Checking installation...")
        self.assertEqual(output.getvalue(), "Checking installation...\n")
        flush.assert_called_once_with()

    def test_package_failure_is_unknown_not_rollback_and_never_starts(self) -> None:
        def runner(argv, **kwargs):
            self.calls.append(argv)
            return subprocess.CompletedProcess(argv, 1)
        report = cli_update_worker.execute(self.plan, runner=runner)
        self.assertEqual(self.calls, [["package-tool"]])
        self.assertEqual(report["package"]["state"], "unknown")
        self.assertTrue(report["package"]["replacement_started"])
        self.assertEqual(report["phase"], "updating")
        self.assertEqual(report["status"], "failed")
        output = human_report(report)
        self.assertIn("Package state is unknown; no instances were restarted", output)
        self.assertNotIn("Update complete", output)

    def test_invalid_validation_output_never_restores(self) -> None:
        def runner(argv, **kwargs):
            self.calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="not-json")
        report = cli_update_worker.execute(self.plan, runner=runner)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(report["phase"], "validating")
        self.assertEqual(report["package"]["state"], "unknown")
        self.assertIn("Update failed during installation verification", human_report(report))

    def test_own_validation_error_protocol_preserves_actionable_reason(self) -> None:
        def runner(argv, **kwargs):
            self.calls.append(argv)
            return subprocess.CompletedProcess(argv, 1 if argv == ["validate"] else 0,
                                               stdout='{"error":"netizen_cli resources are missing"}')
        report = cli_update_worker.execute(self.plan, runner=runner)
        self.assertEqual(len(self.calls), 2)
        self.assertIn("resources are missing", report["reason"])
        self.assertEqual(report["package"]["state"], "unknown")

    def test_failed_start_does_not_prevent_other_instances_or_roll_back(self) -> None:
        def runner(argv, **kwargs):
            result = self.runner(argv, **kwargs)
            if argv[-1] == str(self.directory / "a"):
                result.returncode = 1
            return result
        report = cli_update_worker.execute(self.plan, runner=runner)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["phase"], "restoring")
        self.assertEqual(report["package"]["state"], "verified")
        self.assertEqual([item["state"] for item in report["instances"]], ["unknown", "ready", "stopped"])
        self.assertEqual(report["progress"]["ready"], 1)
        output = human_report(report)
        self.assertIn("Update incomplete: the installed package was verified", output)
        self.assertIn("1/2 restored and ready", output)
        self.assertIn(f"{self.directory / 'a'}: readiness unconfirmed (may be running)", output)
        self.assertIn(f"{self.directory / 'b'}: ready", output)
        self.assertIn(f"netizen status --root {self.directory / 'a'}", output)
        self.assertIn(f"netizen logs --root {self.directory / 'a'}", output)

    def test_changed_identity_files_abort_before_package_replacement(self) -> None:
        metadata = self.directory / "METADATA"
        metadata.write_text("changed")
        self.plan["package_plan"]["revalidation_files"] = {str(metadata): hashlib.sha256(b"original").hexdigest()}
        report = cli_update_worker.execute(self.plan, runner=self.runner)
        self.assertEqual(self.calls, [])
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["package"]["replacement_started"])
        output = human_report(report)
        self.assertIn("Package replacement did not start", output)
        self.assertIn(f"{self.directory / 'a'}: stopped (not restarted)", output)
        self.assertNotIn("No instances were stopped", output)

    def test_fresh_identity_revalidation_failure_prevents_package_replacement(self) -> None:
        self.plan["package_plan"]["revalidation_command"] = ["revalidate"]
        def runner(argv, **kwargs):
            self.calls.append(argv)
            return subprocess.CompletedProcess(argv, 1)
        report = cli_update_worker.execute(self.plan, runner=runner)
        self.assertEqual(self.calls, [["revalidate"]])
        self.assertFalse(report["package"]["replacement_started"])

    def test_timeout_marks_only_affected_start_unknown_and_continues(self) -> None:
        def runner(argv, **kwargs):
            if argv[-1] == str(self.directory / "a"):
                raise subprocess.TimeoutExpired(argv, 180)
            return self.runner(argv, **kwargs)
        report = cli_update_worker.execute(self.plan, runner=runner)
        self.assertEqual([item["state"] for item in report["instances"]], ["unknown", "ready", "stopped"])

    def test_tool_no_version_change_still_restores_recorded_set(self) -> None:
        self.plan["package_plan"]["backend"] = "uv-tool"
        def runner(argv, **kwargs):
            result = self.runner(argv, **kwargs)
            result.stdout = '{"version":"1.0"}'
            return result
        report = cli_update_worker.execute(self.plan, runner=runner)
        self.assertEqual(report["status"], "succeeded")
        self.assertEqual(report["package"]["after_version"], "1.0")
        self.assertEqual(report["progress"]["ready"], 2)
        output = human_report(report)
        self.assertIn("netizen-cli 1.0 (version unchanged); installation verified", output)
        self.assertIn("2/2 restored and ready", output)
        self.assertNotIn("No package changes required", output)

    def test_interruption_after_all_ready_does_not_claim_recovery_failure(self) -> None:
        with patch.object(cli_update_worker, "_check_interrupted", side_effect=KeyboardInterrupt()):
            report = cli_update_worker.execute(self.plan, runner=self.runner)
        self.assertEqual(report["status"], "failed")
        output = human_report(report)
        self.assertIn("Update incomplete", output)
        self.assertIn("2/2 restored and ready", output)
        self.assertIn("KeyboardInterrupt", output)
        self.assertNotIn("readiness unconfirmed", output)

    def test_rendering_preserves_json_report_and_quotes_recovery_commands(self) -> None:
        report = self.plan["report"]
        report.update(status="failed", phase="restoring")
        report["package"].update(state="verified", replacement_started=True, after_version="2.0")
        report["instances"][0].update(root="/tmp/work netizen's root", action="start", state="unknown")
        original = json.dumps(report)
        output = human_report(report)
        self.assertIn("netizen status --root '/tmp/work netizen'\"'\"'s root'", output)
        self.assertIn(f"Report: {report['report_path']}", output)
        self.assertEqual(json.dumps(report), original)
        machine_output = io.StringIO()
        with contextlib.redirect_stdout(machine_output):
            cli_update_worker.print_report(report, json_output=True)
        self.assertEqual(json.loads(machine_output.getvalue()), json.loads(original))

    def test_interrupt_during_start_marks_unknown_without_database_or_package_rollback(self) -> None:
        def runner(argv, **kwargs):
            if argv[-1] == str(self.directory / "a"):
                raise KeyboardInterrupt()
            return self.runner(argv, **kwargs)
        report = cli_update_worker.execute(self.plan, runner=runner)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["package"]["state"], "verified")
        self.assertEqual([item["state"] for item in report["instances"]], ["unknown", "stopped", "stopped"])
        self.assertEqual(report["unexecuted"], [f"start:{self.directory / 'b'}"])

    def test_worker_refuses_lost_root_lock_before_package_replacement(self) -> None:
        del self.plan["root_locks"][str(self.directory / "b")]
        report = cli_update_worker.execute(self.plan, runner=self.runner)
        self.assertEqual(self.calls, [])
        self.assertFalse(report["package"]["replacement_started"])
        self.assertIn("every instance maintenance lock", report["reason"])

    def test_worker_rejects_an_unowned_descriptor_for_the_same_locked_inode(self) -> None:
        root = self.directory / "b"
        borrowed = os.open(root / "state/.install.lock", os.O_RDWR)
        self.addCleanup(os.close, borrowed)
        self.plan["root_locks"][str(root)] = borrowed
        report = cli_update_worker.execute(self.plan, runner=self.runner)
        self.assertEqual(self.calls, [])
        self.assertFalse(report["package"]["replacement_started"])

    def test_fresh_restore_uses_owned_lock_without_reacquiring_and_sets_cloexec(self) -> None:
        root = self.directory / "a"
        descriptor = self.plan["root_locks"][str(root)]
        os.set_inheritable(descriptor, True)
        manager = FakeManager({str(root): False})
        cli_update_restore.restore(root, descriptor, expected_prefix=Path(sys.prefix),
                                   expected_python=Path(sys.executable), manager=manager)
        self.assertFalse(os.get_inheritable(descriptor))
        self.assertEqual(manager.actions, [("start", str(root))])
        # The caller still owns the lock after a successful child-style restore.
        with self.assertRaises(BlockingIOError):
            acquire_install_lock(root)

    def test_fresh_restore_refuses_binding_change_without_start(self) -> None:
        root = self.directory / "a"
        manager = FakeManager({str(root): False})
        with self.assertRaisesRegex(RuntimeError, "binding changed"):
            cli_update_restore.restore(root, self.plan["root_locks"][str(root)],
                                       expected_prefix=self.directory / "other-python",
                                       expected_python=Path(sys.executable), manager=manager)
        self.assertEqual(manager.actions, [])

    def test_external_standard_library_worker_survives_source_removal(self) -> None:
        source = self.directory / "old-install" / "worker.py"
        source.parent.mkdir()
        source.write_bytes(Path(cli_update_worker.__file__).read_bytes())
        external = self.directory / "external-worker.py"
        shutil.copyfile(source, external)
        package = self.plan["package_plan"]
        package["command"] = [sys.executable, "-I", "-c", "from pathlib import Path; "
                              f"Path({str(source)!r}).unlink()"]
        package["validation_command"] = [sys.executable, "-I", "-c", 'print(\'{"version":"2.0"}\')']
        self.plan["report"]["instances"] = []
        self.plan["root_locks"] = {}
        self.plan["report"]["progress"].update(stop_total=0, stopped=0, start_total=0)
        plan_file = self.directory / "plan.json"
        plan_file.write_text(json.dumps(self.plan))
        result = subprocess.run([sys.executable, "-I", str(external), str(plan_file)],
                                capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(source.exists())
        self.assertFalse(plan_file.exists())
        self.assertEqual(json.loads(result.stdout)["package"]["state"], "verified")
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertIn("Verifying the installed package...", result.stderr)
        self.assertIn("Verified netizen-cli 2.0.", result.stderr)
        self.assertEqual(json.loads(Path(self.plan["report"]["report_path"]).read_text())["status"], "succeeded")

    def test_external_worker_retains_locks_but_does_not_leak_them_to_package_tool(self) -> None:
        for item in self.plan["report"]["instances"]:
            item["was_running"] = False
        self.plan["report"]["progress"].update(stop_total=0, stopped=0, start_total=0)
        root = self.directory / "a"
        descriptor = self.plan["root_locks"][str(root)]
        probe = "\n".join([
            "import fcntl, os, sys", f"path = {str(root / 'state/.install.lock')!r}",
            "expected = os.stat(path)",
            "try:", f"    inherited = os.fstat({descriptor})",
            "except OSError:", "    pass", "else:",
            "    assert (inherited.st_dev,inherited.st_ino) != (expected.st_dev,expected.st_ino)",
            "fd = os.open(path, os.O_RDWR)", "try:",
            "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)",
            "except BlockingIOError:", "    sys.exit(0)",
            "sys.exit(1)",
        ])
        self.plan["package_plan"]["command"] = [sys.executable, "-I", "-c", probe]
        self.plan["package_plan"]["validation_command"] = [
            sys.executable, "-I", "-c", 'print(\'{"version":"2.0"}\')',
        ]
        external = self.directory / "external-worker.py"
        shutil.copyfile(Path(cli_update_worker.__file__), external)
        plan_file = self.directory / "lock-plan.json"
        plan_file.write_text(json.dumps(self.plan))
        result = subprocess.run([sys.executable, "-I", str(external), str(plan_file)],
                                pass_fds=tuple(self.plan["root_locks"].values()),
                                capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "succeeded")


class UpdateWorkerSignalTest(unittest.TestCase):
    def wait_for_file(self, path: Path, process: subprocess.Popen) -> None:
        deadline = time.monotonic() + 10
        while not path.exists():
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                self.fail(f"Worker exited before {path.name}: {stdout}\n{stderr}")
            if time.monotonic() >= deadline:
                self.fail(f"Timed out waiting for {path.name}")
            time.sleep(0.01)

    def check_interruption(self, signum: int, *, ignore_termination: bool = False,
                           descendant: bool = False) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            root = directory / "instance"
            prepare_root(root)
            maintenance = directory / "maintenance"
            maintenance.mkdir(mode=0o700)
            environment_lock = cli_update._lock(maintenance)
            root_lock = acquire_install_lock(root)
            report = cli_update.new_report()
            report["report_path"] = str(directory / "report.json")
            report["instances"] = [{
                "root": str(root), "service": "disposable-instance", "was_running": False,
                "state": "stopped", "action": "stop", "reason": None,
                "prefix": sys.prefix, "python": sys.executable,
            }]
            child_source = "\n".join([
                "import fcntl, os, signal, sys, time", "from pathlib import Path",
                f"directory = Path({str(directory)!r})",
                "(directory / 'group').write_text(str(os.getpgrp()))",
                f"if {descendant!r} and os.fork():",
                "    time.sleep(20)",
                "    sys.exit(0)",
                f"if {descendant!r}:",
                "    with open(os.devnull, 'wb') as output:",
                "        os.dup2(output.fileno(), 1)",
                "        os.dup2(output.fileno(), 2)",
                "def terminate(signum, frame):",
                "    (directory / 'received').write_text(str(signum))",
                f"    if {ignore_termination!r}: return",
                "    while not (directory / 'release').exists(): time.sleep(0.01)",
                "    (directory / 'exited').write_text('graceful')",
                "    sys.exit(0)",
                "signal.signal(signal.SIGTERM, terminate)",
                "activity = (directory / 'activity.lock').open('w')",
                "fcntl.flock(activity, fcntl.LOCK_EX)",
                "heartbeat = (directory / 'heartbeat').open('a', buffering=1)",
                "heartbeat.write(str(time.monotonic_ns()) + '\\n')",
                "(directory / 'started').write_text(str(os.getpid()))",
                "deadline = time.monotonic() + 20",
                "while time.monotonic() < deadline:",
                "    heartbeat.write(str(time.monotonic_ns()) + '\\n')",
                "    time.sleep(0.01)",
            ])
            plan = {
                "protocol": 1, "json_output": True, "report": report,
                "root_locks": {str(root): root_lock}, "package_plan": {
                    "environment_python": sys.executable,
                    "command": [sys.executable, "-I", "-c", child_source],
                    "command_cwd": str(directory),
                    "validation_command": [sys.executable, "-I", "-c",
                                           "raise AssertionError('validation must not run')"],
                },
            }
            worker = directory / "worker.py"
            shutil.copyfile(Path(cli_update_worker.__file__), worker)
            plan_path = directory / "plan.json"
            plan_path.write_text(json.dumps(plan))
            try:
                process = subprocess.Popen(
                    [sys.executable, "-I", "-B", str(worker), str(plan_path)],
                    pass_fds=(environment_lock, root_lock),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
            finally:
                # Only the worker now owns these lock descriptions. Keeping a
                # parent copy would hide an early release by the interrupted worker.
                os.close(environment_lock)
                os.close(root_lock)
            child_pid = None
            try:
                self.wait_for_file(directory / "started", process)
                child_pid = int((directory / "started").read_text())
                if descendant:
                    group = int((directory / "group").read_text())
                    self.assertNotEqual(child_pid, group)
                    self.assertEqual(os.getpgid(child_pid), group)
                process.send_signal(signum)
                self.wait_for_file(directory / "received", process)
                self.assertIsNone(process.poll())
                with self.assertRaises(cli_update.UpdateError):
                    cli_update._lock(maintenance)
                with self.assertRaises(BlockingIOError):
                    acquire_install_lock(root)
                heartbeat = (directory / "heartbeat").read_text()
                process.send_signal(signum)
                if descendant:
                    # Allow slow CI scheduling, but stay within the worker's 5s grace.
                    deadline = time.monotonic() + 2
                    while (directory / "heartbeat").read_text() == heartbeat:
                        self.assertIsNone(process.poll(), "Worker exited before descendant progress")
                        if time.monotonic() >= deadline:
                            self.fail("Descendant heartbeat did not advance during the shutdown grace")
                        time.sleep(0.01)
                else:
                    time.sleep(0.05)
                self.assertIsNone(process.poll())
                if descendant:
                    self.assertNotEqual((directory / "heartbeat").read_text(), heartbeat)
                    with self.assertRaises(cli_update.UpdateError):
                        cli_update._lock(maintenance)
                    with self.assertRaises(BlockingIOError):
                        acquire_install_lock(root)
                os.kill(child_pid, 0)
                (directory / "release").touch()
                stdout, stderr = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 1, stdout + stderr)
                result = json.loads(stdout)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["phase"], "updating")
                self.assertEqual(result["package"]["state"], "unknown")
                self.assertIn(signal.Signals(signum).name, result["reason"])
                self.assertEqual(result["unexecuted"], ["validation", "restore"])
                self.assertEqual(result["instances"][0]["state"], "stopped")
                self.assertEqual(json.loads(Path(report["report_path"]).read_text()), result)
                # The descendant closes its inherited output pipes, so the
                # worker's communicate cannot hide early worker/lock release.
                # This lock is released on exit even if an orphan remains a
                # zombie until init reaps it. kill(pid, 0) alone cannot prove
                # whether a descendant is still capable of writing the package.
                with (directory / "activity.lock").open("r+") as activity:
                    fcntl.flock(activity, fcntl.LOCK_EX | fcntl.LOCK_NB)
                heartbeat = (directory / "heartbeat").read_text()
                time.sleep(0.05)
                self.assertEqual((directory / "heartbeat").read_text(), heartbeat)
                if not descendant:
                    with self.assertRaises(ProcessLookupError):
                        os.kill(child_pid, 0)
                self.assertEqual((directory / "exited").exists(), not ignore_termination)
                os.close(cli_update._lock(maintenance))
                os.close(acquire_install_lock(root))
            finally:
                (directory / "release").touch()
                if descendant and child_pid is not None:
                    # Clean only this fixture if the worker regresses and
                    # exits before terminating its descendant.
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(child_pid, signal.SIGKILL)
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate(timeout=5)
                if child_pid is not None:
                    # Disposable test child only; never a manager/service PID.
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_controlled_signals_reap_child_before_releasing_real_locks(self) -> None:
        for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            with self.subTest(signal=signal.Signals(signum).name):
                self.check_interruption(signum)

    def test_sigterm_escalates_for_owned_child_ignoring_termination(self) -> None:
        self.check_interruption(signal.SIGTERM, ignore_termination=True)

    def test_sigterm_stops_descendant_after_group_leader_exits(self) -> None:
        self.check_interruption(signal.SIGTERM, ignore_termination=True, descendant=True)

    def test_cleanup_never_signals_a_reaped_process_group(self) -> None:
        with patch.object(cli_update_worker.os, "killpg") as killpg:
            cli_update_worker._stop_owned_child(SimpleNamespace(returncode=0))
        killpg.assert_not_called()

    def test_late_interruption_aborts_without_cleaning_a_completed_command(self) -> None:
        with patch.object(cli_update_worker.subprocess, "Popen") as popen:
            child = popen.return_value.__enter__.return_value
            child.communicate.return_value = ("done", "")
            child.returncode = 0
            with patch.object(cli_update_worker, "_check_interrupted", side_effect=[
                None, None, cli_update_worker.UpdateInterrupted("SIGTERM"),
            ]), patch.object(cli_update_worker, "_stop_owned_child") as cleanup:
                with self.assertRaises(cli_update_worker.UpdateInterrupted):
                    cli_update_worker._run_owned(["disposable-command"], capture_output=True)
            cleanup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
