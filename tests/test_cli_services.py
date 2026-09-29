from __future__ import annotations

import fcntl
import html
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


from netizen_cli.cli_services import ServiceBinding, ServiceError, ServiceManager
from netizen_cli.instance import INSTANCE_ROOT_MARKER_CONTENT


def _private(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    path.chmod(0o600)


class ManagerDouble:
    def __init__(self) -> None:
        self.manager: ServiceManager
        self.calls: list[list[str]] = []
        self.running: dict[str, int | None] = {}
        self.enabled: set[str] = set()
        self.descriptors: dict[str, int] = {}
        self.overrides: dict[str, dict[str, str]] = {}
        self.ready_on_start = True
        self.stop_stuck = False
        self.query_failure = False
        self.malformed_inventory = False

    def binding(self, name: str) -> ServiceBinding:
        suffix = "" if self.manager.platform == "linux" else ".plist"
        return self.manager._binding_from_file(self.manager.service_dir / (name + suffix))

    def start(self, name: str) -> None:
        binding = self.binding(name)
        self.running[name] = 4242
        lock = binding.root / "state/service.lifetime.lock"
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.descriptors[name] = fd
        _private(binding.root / "state/service.identity.json", json.dumps({
            "format": 1, "pid": 4242, "python": str(binding.python),
            "prefix": str(binding.prefix), "root": str(binding.root),
        }).encode())
        if self.ready_on_start:
            _private(binding.root / "state/service.ready", b"netizen service ready\n")

    def stop(self, name: str) -> None:
        if self.stop_stuck:
            return
        self.running.pop(name, None)
        fd = self.descriptors.pop(name, None)
        if fd is not None:
            os.close(fd)

    def __call__(self, args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        out = ""
        code = 0
        if self.query_failure:
            return subprocess.CompletedProcess(args, 1, "", "manager unavailable")
        if args[0] == "systemctl":
            command = args[2]
            if command == "show":
                name = args[3]
                path = self.manager.service_dir / name
                exists = path.exists()
                active = self.running.get(name) is not None
                props = {
                    "LoadState": "loaded" if exists else "not-found",
                    "ActiveState": "active" if active else "inactive",
                    "SubState": "running" if active else "dead",
                    "MainPID": "4242" if active else "0", "ControlPID": "0",
                    "FragmentPath": str(path) if exists else "", "DropInPaths": "",
                    "NeedDaemonReload": "no", "Transient": "no",
                    "UnitFileState": "enabled" if name in self.enabled else "disabled",
                }
                props.update(self.overrides.get(name, {}))
                out = "\n".join(f"{key}={value}" for key, value in props.items())
            elif command in {"list-units", "list-unit-files"}:
                key = "unit" if command == "list-units" else "unit_file"
                out = json.dumps([{key: path.name}
                                  for path in self.manager.service_dir.glob("*.service")])
                if self.malformed_inventory:
                    out = "unexpected human diagnostics"
            elif command == "start":
                self.start(args[3])
            elif command == "stop":
                self.stop(args[3])
            elif command == "enable":
                self.enabled.add(args[3])
            elif command == "disable":
                self.enabled.discard(args[3])
        elif args[0] == "launchctl":
            command = args[1]
            if command == "list":
                out = "PID\tStatus\tLabel\n" + "".join(
                    f"{pid if pid else '-'}\t0\t{name}\n" for name, pid in self.running.items()
                )
                if self.malformed_inventory:
                    out = "new unsupported output"
            elif command == "print":
                if args[2].count("/") == 2:
                    code = 0 if args[2].rsplit("/", 1)[-1] in self.running else 113
                out = "Not parsed diagnostic fields: state = nonsense pid = 999999"
            elif command == "bootstrap":
                self.start(Path(args[3]).stem)
            elif command == "kickstart":
                self.start(args[2].rsplit("/", 1)[-1])
            elif command == "bootout":
                self.stop(args[2].rsplit("/", 1)[-1])
            elif command == "enable":
                self.enabled.add(args[2].rsplit("/", 1)[-1])
            elif command == "disable":
                self.enabled.discard(args[2].rsplit("/", 1)[-1])
        return subprocess.CompletedProcess(args, code, out, "")


class LinuxServicesTest(unittest.TestCase):
    platform = "linux"

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        tmp_path = Path(temporary.name)
        home = tmp_path / "account"
        home.mkdir()
        root = tmp_path / "instance with space $dollar %percent"
        root.mkdir()
        (root / "state").mkdir()
        _private(root / ".netizen-root", INSTANCE_ROOT_MARKER_CONTENT)
        prefix = tmp_path / "env-A"
        (prefix / "bin").mkdir(parents=True)
        python = prefix / "bin/python"
        python.symlink_to(sys.executable)
        binding = ServiceBinding(root, python, prefix)
        native = ManagerDouble()
        manager = ServiceManager(home=home, platform=self.platform, runner=native)
        native.manager = manager
        self.managed = manager, native, binding
        self.addCleanup(lambda: [os.close(fd) for fd in native.descriptors.values()])

    def install_v1_fixture(self) -> bytes:
        """Load persisted v1 evidence without using the current service renderer."""
        manager, _, binding = self.managed
        suffix = ".service" if self.platform == "linux" else ".plist"
        template = (Path(__file__).parent / "fixtures" / ("cli_service_v1" + suffix)).read_text()
        for name, value in {"ROOT": binding.root, "PYTHON": binding.python,
                            "PREFIX": binding.prefix, "HOME": manager.home,
                            "LABEL": manager.service_name(binding.root)}.items():
            text = str(value)
            escaped = (text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
                       if self.platform == "linux" else html.escape(text))
            template = template.replace("@" + name + "@", escaped)
        template = template.replace("@METADATA@", json.dumps({
            "root": str(binding.root), "python": str(binding.python),
            "prefix": str(binding.prefix), "codex_home": None,
        }, sort_keys=True))
        manager.service_dir.mkdir(parents=True, exist_ok=True)
        content = template.encode()
        _private(manager.service_file(binding.root), content)
        return content

    def test_frozen_v1_services_remain_controllable_after_default_renderer_changes(self) -> None:
        manager, native, binding = self.managed
        content = self.install_v1_fixture()
        # A package update may select a new format for new registrations. All
        # controls and inventory must still understand known persisted v1 bytes.
        with patch.object(manager, "_render", return_value=b"future default format\n") as render:
            manager.register(binding)
            self.assertEqual(manager.inspect(binding.root).binding, binding)
            self.assertEqual([status.binding for status in manager.list_instances()], [binding])
            self.assertTrue(manager.start(binding.root, timeout=0).ready)
            self.assertFalse(manager.stop(binding.root, timeout=0).running)
            self.assertEqual(manager.service_file(binding.root).read_bytes(), content)
            manager.remove(binding.root)
            self.assertIsNone(manager.inspect(binding.root))
            render.assert_not_called()

    def test_v1_fixture_rejects_unknown_versions_identity_changes_and_overrides(self) -> None:
        manager, native, binding = self.managed
        content = self.install_v1_fixture()
        if self.platform == "linux":
            mutations = [
                content.replace(b"service v1 ", b"service v2 "),
                content.replace(b'ExecStart=:', b'ExecStart=:+'),
                content.replace(b'"NETIZEN_CLI_SERVICE=1"', b'"NETIZEN_CLI_SERVICE=0"'),
                content.replace(b'"NETIZEN_ROOT=', b'"OTHER_ROOT='),
                content.replace(b'KillMode=control-group', b'KillMode=process'),
                content.replace(b'[Install]', b'Environment="PYTHONPATH=/unsafe"\n[Install]'),
                content.replace(b'"--root"', b'"--other-root"'),
            ]
        else:
            payload = plistlib.loads(content)
            mutations = []
            for key, value in (
                ("Label", "io.github.lijingda.netizen.other"),
                ("ProgramArguments", ["/other/python", *payload["ProgramArguments"][1:]]),
                ("AbandonProcessGroup", True),
                ("AbandonProcessGroup", 0),  # A plist integer is not a boolean.
                ("RunAtLoad", 1),
                ("OtherOverride", True),
            ):
                mutations.append(plistlib.dumps({**payload, key: value}))
            for key, value in (
                ("NETIZEN_MANAGED_LAUNCH_AGENT", "io.github.lijingda.netizen/cli-v2"),
                ("NETIZEN_ROOT", str(binding.root.parent / "other-root")),
                ("NETIZEN_CLI_SERVICE", "0"),
                ("PYTHONPATH", "/unsafe"),
            ):
                mutations.append(plistlib.dumps({**payload, "EnvironmentVariables": {
                    **payload["EnvironmentVariables"], key: value,
                }}))
        for changed in mutations:
            with self.subTest(definition=changed):
                _private(manager.service_file(binding.root), changed)
                native.calls.clear()
                with self.assertRaises(ServiceError):
                    manager.remove(binding.root)
                self.assertEqual(native.calls, [])
                self.assertTrue(manager.service_file(binding.root).exists())

    def test_systemd_empty_list_unit_files_exit_one_is_valid_inventory(self) -> None:
        manager, native, _ = self.managed
        if self.platform != "linux":
            self.skipTest("systemd-specific empty inventory status")

        def runner(args, **kwargs):
            if "list-unit-files" in args:
                return subprocess.CompletedProcess(args, 1, "[]\n", "")
            return native(args, **kwargs)

        manager._runner = runner
        self.assertEqual(manager.list_instances(), [])

    def test_systemd_other_inventory_failures_are_never_empty_inventory(self) -> None:
        manager, native, binding = self.managed
        if self.platform != "linux":
            self.skipTest("systemd-specific inventory statuses")
        for command, code, output, diagnostics in (
            ("list-unit-files", 2, "[]", ""),
            ("list-unit-files", 1, "[]", "manager unavailable"),
            ("list-unit-files", 1, "", ""),
            ("list-unit-files", 1, "{}", ""),
            ("list-unit-files", 1, "human diagnostics", ""),
            ("list-unit-files", 1, json.dumps([{"unit_file": manager.service_name(binding.root)}]), ""),
            ("list-unit-files", 0, "[{}]", ""),
            ("list-unit-files", 0, '["not a row"]', ""),
            ("list-units", 1, "[]", ""),
        ):
            with self.subTest(command=command, code=code, output=output, diagnostics=diagnostics):
                def runner(args, **kwargs):
                    if command in args:
                        return subprocess.CompletedProcess(args, code, output, diagnostics)
                    return native(args, **kwargs)

                manager._runner = runner
                with self.assertRaises(ServiceError):
                    manager.list_instances()

    def test_register_retains_environment_python_and_does_not_start(self) -> None:
        manager, native, binding = self.managed
        manager.register(binding)
        status = manager.inspect(binding.root)
        assert status and status.binding == binding
        assert status.binding.python != binding.python.resolve()
        assert not status.running and not status.ready
        assert not native.descriptors
        assert "current" not in manager.service_file(binding.root).read_text()
        assert manager.list_instances() == [status]


    def test_fresh_root_inspection_is_read_only_and_requires_positive_manager_evidence(self) -> None:
        manager, native, binding = self.managed
        new = binding.root.parent / "not-created"
        assert manager.inspect(new) is None
        assert not new.exists()
        native.query_failure = True
        with self.assertRaises(ServiceError):
            manager.inspect(new)
        assert not new.exists()


    def test_cross_environment_control_does_not_change_existing_binding(self) -> None:
        manager, native, binding = self.managed
        manager.register(binding)
        other = ServiceBinding(binding.root, Path(sys.executable).absolute(), Path(sys.prefix).resolve())
        with self.assertRaisesRegex(ServiceError, "already bound"):
            manager.register(other)
        assert manager.start(binding.root, timeout=0).ready
        assert manager.inspect(binding.root).binding == binding
        assert manager.stop(binding.root, timeout=0).binding == binding
        assert manager.inspect(binding.root).enabled is (True if manager.platform == "linux" else None)
        assert not any("disable" in call for call in native.calls)


    def test_remove_stops_unregisters_but_keeps_instance_data(self) -> None:
        manager, native, binding = self.managed
        _private(binding.root / "state/channel.sqlite3", b"instance data")
        manager.register(binding)
        manager.start(binding.root, timeout=0)
        manager.remove(binding.root)
        assert manager.inspect(binding.root) is None
        assert not native.descriptors
        assert (binding.root / "state/channel.sqlite3").read_bytes() == b"instance data"
        assert (binding.root / ".netizen-root").exists()
        assert (binding.root / "state/service.lifetime.lock").exists()
        manager.remove(binding.root)  # Proven unbound is idempotent.


    def test_stop_failure_does_not_delete_definition_or_report_success(self) -> None:
        manager, native, binding = self.managed
        manager.register(binding)
        manager.start(binding.root, timeout=0)
        native.stop_stuck = True
        with self.assertRaisesRegex(ServiceError, "fully exit"):
            manager.stop(binding.root, timeout=0)
        assert manager.service_file(binding.root).exists()
        assert manager.inspect(binding.root).running


    def test_ready_requires_running_process_held_lock_and_private_marker(self) -> None:
        manager, native, binding = self.managed
        manager.register(binding)
        _private(binding.root / "state/service.ready", b"netizen service ready\n")
        assert not manager.inspect(binding.root).ready
        native.ready_on_start = False
        with self.assertRaisesRegex(ServiceError, "did not become ready"):
            manager.start(binding.root, timeout=0)
        assert manager.inspect(binding.root).running
        assert not (binding.root / "state/service.ready").exists()
        _private(binding.root / "state/service.ready", b"not a ready marker")
        assert not manager.inspect(binding.root).ready
        _private(binding.root / "state/service.ready", b"netizen service ready\n")
        assert manager.inspect(binding.root).ready


    def test_held_lifetime_lock_blocks_treating_unbound_root_as_free(self) -> None:
        manager, native, binding = self.managed
        path = binding.root / "state/service.lifetime.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ServiceError, "lifetime lock"):
                manager.inspect(binding.root)
        finally:
            os.close(fd)


    def test_modified_foreign_or_symlink_definitions_fail_closed(self) -> None:
        manager, native, binding = self.managed
        manager.register(binding)
        path = manager.service_file(binding.root)
        content = path.read_bytes()
        if manager.platform == "linux":
            path.write_bytes(content.replace(b"KillMode=control-group", b"KillMode=process"))
        else:
            payload = plistlib.loads(content)
            payload["ProgramArguments"][0] = "/other/python"
            path.write_bytes(plistlib.dumps(payload))
        native.calls.clear()
        with self.assertRaises(ServiceError):
            manager.remove(binding.root)
        assert path.exists()
        assert not native.calls
        path.write_bytes(content)
        copied = binding.root / "copied.service"
        copied.write_bytes(content)
        path.unlink()
        path.symlink_to(copied)
        with self.assertRaises(ServiceError):
            manager.inspect(binding.root)


    def test_inventory_malformed_response_is_not_empty_inventory(self) -> None:
        manager, native, binding = self.managed
        manager.register(binding)
        native.malformed_inventory = True
        with self.assertRaises(ServiceError):
            manager.list_instances()


    def test_prefix_filter_skips_state_queries_for_proven_other_environment(self) -> None:
        manager, native, binding = self.managed
        manager.register(binding)
        native.calls.clear()
        assert manager.list_instances(prefix=binding.root / "other-env") == []
        assert not any("show" in call for call in native.calls)


    def test_explicit_native_codex_home_survives_service_binding_roundtrip(self) -> None:
        manager, native, binding = self.managed
        selected = binding.root.parent / "shared-codex"
        configured = ServiceBinding(binding.root, binding.python, binding.prefix, selected)
        manager.register(configured)
        assert manager.inspect(binding.root).binding.codex_home == selected
        assert manager._environment(configured)["CODEX_HOME"] == str(selected)


    def test_systemd_effective_overrides_are_not_accepted(self) -> None:
        manager, native, binding = self.managed
        if manager.platform != "linux":
            self.skipTest("systemd-specific machine property validation")
        manager.register(binding)
        name = manager.service_name(binding.root)
        for override in ({"DropInPaths": "/tmp/override.conf"}, {"NeedDaemonReload": "yes"},
                         {"Transient": "yes"}, {"FragmentPath": "/other/unit.service"}):
            native.overrides[name] = override
            with self.assertRaisesRegex(ServiceError, "definition differs"):
                manager.start(binding.root, timeout=0)
        assert not native.descriptors


    def test_launchd_loaded_is_not_running_and_print_text_is_not_state(self) -> None:
        manager, native, binding = self.managed
        if manager.platform != "darwin":
            self.skipTest("launchd-specific native table validation")
        manager.register(binding)
        native.running[manager.service_name(binding.root)] = None
        status = manager.inspect(binding.root)
        assert status.loaded and not status.running and not status.ready
        assert status.enabled is None
        assert manager.start(binding.root, timeout=0).ready
        assert any("kickstart" in call for call in native.calls)

    def test_launchd_loaded_job_without_pid_must_be_booted_out_to_stop(self) -> None:
        manager, native, binding = self.managed
        if self.platform != "darwin":
            self.skipTest("launchd-specific pending restart intent")
        manager.register(binding)
        name = manager.service_name(binding.root)
        native.running[name] = None
        native.stop_stuck = True
        with self.assertRaisesRegex(ServiceError, "remained loaded"):
            manager.stop(binding.root, timeout=0)
        self.assertTrue(manager.inspect(binding.root).loaded)
        native.stop_stuck = False
        stopped = manager.stop(binding.root, timeout=0)
        self.assertFalse(stopped.running)
        self.assertFalse(stopped.loaded)
        self.assertIn(["launchctl", "bootout", f"gui/{manager.uid}/{name}"], native.calls)
        self.assertIn(name, native.enabled)  # Preserve login autostart intent.
        self.assertTrue(manager.service_file(binding.root).exists())


    def test_binding_rejects_control_characters_and_keeps_venv_symlink(self) -> None:
        _, _, binding = self.managed
        with self.assertRaisesRegex(ServiceError, "control characters"):
            ServiceBinding(Path("/tmp/evil\nvalue"), binding.python, binding.prefix)
        assert binding.python.is_symlink()

    def test_inventory_excludes_one_shot_maintenance_jobs(self) -> None:
        manager, native, binding = self.managed
        manager.register(binding)
        name = "netizen-update-abc123.service" if self.platform == "linux" else "io.github.lijingda.netizen.update.abc123.plist"
        (manager.service_dir / name).write_text("not an instance definition")
        assert len(manager.list_instances()) == 1

    def test_launchd_requires_actual_running_identity_not_just_plist(self) -> None:
        manager, native, binding = self.managed
        if self.platform != "darwin":
            self.skipTest("launchd loaded identity evidence")
        manager.register(binding)
        manager.start(binding.root, timeout=0)
        marker = binding.root / "state/service.identity.json"
        identity = json.loads(marker.read_bytes())
        identity["prefix"] = "/another/environment"
        _private(marker, json.dumps(identity).encode())
        with self.assertRaisesRegex(ServiceError, "running service identity differs"):
            manager.inspect(binding.root)
        native.stop(manager.service_name(binding.root))
        assert not manager.inspect(binding.root).running  # Stale identity cannot revive a stopped process.

    def test_launchd_missing_running_identity_is_not_update_evidence(self) -> None:
        manager, native, binding = self.managed
        if self.platform != "darwin":
            self.skipTest("launchd loaded identity evidence")
        manager.register(binding)
        manager.start(binding.root, timeout=0)
        (binding.root / "state/service.identity.json").unlink()
        with self.assertRaisesRegex(ServiceError, "not yet proven"):
            manager.list_instances()
        with self.assertRaisesRegex(ServiceError, "did not become ready"):
            manager.start(binding.root, timeout=0)

    def test_logs_are_diagnostics_even_when_runtime_state_is_unknown(self) -> None:
        manager, native, binding = self.managed
        manager.register(binding)
        native.malformed_inventory = True
        native.calls.clear()
        output = manager.logs(binding.root, lines=20)
        self.assertIn("Runtime log", output)
        self.assertIn("(not created)", output)
        if self.platform == "linux":
            self.assertEqual(native.calls[-1][0], "journalctl")
        else:
            self.assertIn("LaunchAgent startup stderr", output)
            self.assertEqual(native.calls, [])

    def test_logs_read_runtime_and_startup_sinks_with_independent_availability(self) -> None:
        manager, native, binding = self.managed
        manager.register(binding)
        _private(binding.root / "state/netizen.log", b"older\nRuntime failure detail\n")
        _private(binding.root / "state/launchd.stderr.log", b"before logger startup error\n")
        original_runner = manager._runner

        def runner(args, **kwargs):
            if args[0] == "journalctl":
                return subprocess.CompletedProcess(args, 0, "before logger startup error\n", "")
            return original_runner(args, **kwargs)

        manager._runner = runner
        output = manager.logs(binding.root, lines=1)
        self.assertIn("Runtime failure detail", output)
        self.assertNotIn("older", output)
        self.assertIn("before logger startup error", output)
        (binding.root / "state/netizen.log").unlink()
        self.assertIn("before logger startup error", manager.logs(binding.root, lines=1))

    def test_logs_reject_symlink_without_hiding_other_diagnostic_sink(self) -> None:
        manager, native, binding = self.managed
        manager.register(binding)
        foreign = binding.root / "do-not-read"
        foreign.write_text("private-unrelated-content")
        (binding.root / "state/netizen.log").symlink_to(foreign)
        output = manager.logs(binding.root)
        self.assertNotIn("private-unrelated-content", output)
        self.assertIn("unavailable", output)
        self.assertIn("startup", output)

    def test_runtime_log_read_is_bounded_in_bytes_and_lines(self) -> None:
        manager, native, binding = self.managed
        manager.register(binding)
        _private(binding.root / "state/netizen.log", b"large line" * 140000 + b"\nlast line\n")
        output = manager.logs(binding.root, lines=1)
        self.assertIn("last line", output)
        self.assertIn("earlier log bytes omitted", output)
        self.assertNotIn("large line", output)

    def test_register_and_final_unbound_inspection_accept_only_exact_held_descriptor(self) -> None:
        from netizen_cli.cli_data import instance_lifetime_lock

        manager, native, binding = self.managed
        with instance_lifetime_lock(binding.root) as descriptor:
            assert manager.inspect(binding.root, lifetime_descriptor=descriptor) is None
            manager.register(binding, lifetime_descriptor=descriptor)
            assert not manager.inspect(binding.root, lifetime_descriptor=descriptor).running
            with self.assertRaisesRegex(ServiceError, "lifetime lock"):
                manager.inspect(binding.root)
        manager.remove(binding.root)
        with instance_lifetime_lock(binding.root) as descriptor:
            assert manager.inspect(binding.root, lifetime_descriptor=descriptor) is None
            duplicate = os.open(binding.root / "state/service.lifetime.lock", os.O_RDWR)
            try:
                with self.assertRaisesRegex(ServiceError, "does not hold"):
                    manager.inspect(binding.root, lifetime_descriptor=duplicate)
            finally:
                os.close(duplicate)


class DarwinServicesTest(LinuxServicesTest):
    platform = "darwin"
