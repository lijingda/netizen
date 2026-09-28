from __future__ import annotations

import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from netizen.deployment import launchd, service_backend, systemd
from netizen.deployment.installer_support import InstallError, Layout, Release
from scripts.netizen_installer import resolve_layout


REPOSITORY = Path(__file__).resolve().parents[1]


class _InstanceManager:
    """A fake manager with separate state for every registered instance target."""

    def __init__(self, layouts: tuple[Layout, ...]) -> None:
        self.layouts = layouts
        self.calls: list[tuple[str, ...]] = []
        self.active: set[str] = set()
        self.enabled: set[str] = set()

    @staticmethod
    def target(layout: Layout) -> str:
        if layout.platform == "linux":
            return layout.service_name
        return f"gui/{layout.uid}/{layout.service_label}"

    def __call__(self, arguments, **kwargs):
        args = tuple(str(item) for item in arguments)
        self.calls.append(args)
        stdout, returncode = "", 0
        if args[0] == "systemctl":
            assert args[1] == "--user", args
            action = args[2]
            target = args[-1]
            if action == "show-environment":
                return subprocess.CompletedProcess(args, 0, "", "")
            if action == "is-active":
                stdout = "active\n" if target in self.active else "inactive\n"
                returncode = 0 if target in self.active else 3
            elif action == "is-enabled":
                stdout = "enabled\n" if target in self.enabled else "disabled\n"
                returncode = 0 if target in self.enabled else 1
            elif action == "start":
                self._start(target)
            elif action == "stop":
                self.active.discard(target)
            elif action == "enable":
                self.enabled.add(target)
            elif action == "disable":
                self.enabled.discard(target)
        elif args[0] == "launchctl":
            action, target = args[1], args[2]
            if action == "print" and target.count("/") == 2:
                returncode = 0 if target in self.active else 113
            elif action == "bootstrap":
                layout = next(layout for layout in self.layouts if str(layout.service_file) == args[3])
                self._start(self.target(layout))
            elif action == "bootout":
                self.active.discard(target)
            elif action == "enable":
                self.enabled.add(target)
            elif action == "disable":
                self.enabled.discard(target)
        return subprocess.CompletedProcess(args, returncode, stdout, "")

    def _start(self, target: str) -> None:
        layout = next(layout for layout in self.layouts if self.target(layout) == target)
        self.active.add(target)
        layout.ready_file.write_bytes(service_backend.READY_MARKER_CONTENT)
        layout.ready_file.chmod(0o600)


class InstanceServiceBackendsTest(unittest.TestCase):
    def _layouts(self, directory: str, platform: str) -> tuple[Layout, Layout]:
        home = Path(directory).resolve() / "account"
        home.mkdir()
        layouts = tuple(resolve_layout(
            root=home / name,
            environ={"CODEX_HOME": str(home / ".codex")},
            account_home=home,
            platform_name=platform,
        ) for name in ("share ${NETIZEN_ROOT} %h 团队", "private"))
        for layout in layouts:
            layout.state_dir.mkdir(parents=True)
            layout.service_dir.mkdir(parents=True, exist_ok=True)
        return layouts

    @staticmethod
    def _backend(layout: Layout, manager: _InstanceManager):
        backend_type = systemd.SystemdServiceBackend if layout.platform == "linux" else launchd.LaunchAgentServiceBackend
        return backend_type(layout, manager)

    @staticmethod
    def _release(layout: Layout) -> Release:
        root = layout.releases / ("a" * 64)
        return Release(digest=root.name, root=root, source=REPOSITORY, venv=root / "venv")

    def test_two_instances_keep_start_stop_and_uninstall_targets_separate(self) -> None:
        for platform in ("linux", "darwin"):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                first, second = self._layouts(directory, platform)
                manager = _InstanceManager((first, second))
                backends = [self._backend(layout, manager) for layout in (first, second)]
                with patch.dict(os.environ, {"NETIZEN_ROOT": "/wrong/from-operator-shell"}):
                    for layout, backend in zip((first, second), backends):
                        backend.preflight()
                        backend.publish_definition(backend.render_definition(self._release(layout)), should_enable=True)
                        backend.start_and_wait(timeout=0.1)
                        self.assertEqual(service_backend._service_environment(layout)["NETIZEN_ROOT"], str(layout.product_root))
                    backends[0].service_action("restart")
                    backends[0].uninstall_definition()
                self.assertNotIn(manager.target(first), manager.active)
                self.assertIn(manager.target(second), manager.active)
                self.assertFalse(first.service_file.exists())
                self.assertTrue(second.service_file.exists())
                self.assertFalse(first.ready_file.exists())
                self.assertEqual(second.ready_file.read_bytes(), service_backend.READY_MARKER_CONTENT)
                self.assertEqual(first.codex_home, second.codex_home)
                self.assertNotEqual(first.service_file, second.service_file)
                self.assertFalse(any("netizen.service" in call or "sudo" in call for call in manager.calls))

    def test_definition_for_another_root_is_rejected_before_manager_mutation(self) -> None:
        for platform in ("linux", "darwin"):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                first, second = self._layouts(directory, platform)
                manager = _InstanceManager((first, second))
                first_backend = self._backend(first, manager)
                second_backend = self._backend(second, manager)
                first.service_file.write_bytes(second_backend.render_definition(self._release(second)))
                first.service_file.chmod(0o600)
                for operation in (first_backend.capture_definition, first_backend.uninstall_definition):
                    with self.assertRaisesRegex(InstallError, "unrecognized"):
                        operation()
                self.assertEqual(manager.calls, [])
                self.assertTrue(first.service_file.exists())

    def test_root_environment_mismatch_cannot_hide_behind_a_matching_launcher(self) -> None:
        for platform in ("linux", "darwin"):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                first, second = self._layouts(directory, platform)
                manager = _InstanceManager((first, second))
                backend = self._backend(first, manager)
                definition = backend.render_definition(self._release(first))
                if platform == "linux":
                    content = definition.decode().replace(
                        'Environment=' + systemd._systemd_quote(f"NETIZEN_ROOT={first.product_root}"),
                        'Environment=' + systemd._systemd_quote(f"NETIZEN_ROOT={second.product_root}"),
                    ).encode()
                else:
                    content = plistlib.loads(definition)
                    content["EnvironmentVariables"]["NETIZEN_ROOT"] = str(second.product_root)
                    content = plistlib.dumps(content)
                first.service_file.write_bytes(content)
                first.service_file.chmod(0o600)
                with self.assertRaisesRegex(InstallError, "unrecognized"):
                    backend.capture_definition()
                self.assertEqual(manager.calls, [])

    def test_systemd_active_without_ready_marker_never_counts_as_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first, second = self._layouts(directory, "linux")
            manager = _InstanceManager((first, second))
            manager.active.update((first.service_name, second.service_name))
            manager._start(second.service_name)
            with self.assertRaisesRegex(InstallError, first.service_name):
                self._backend(first, manager).start_and_wait(timeout=0)
            self.assertTrue(second.ready_file.exists())

    def test_systemd_duplicate_environment_override_is_not_owned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first, second = self._layouts(directory, "linux")
            manager = _InstanceManager((first, second))
            backend = self._backend(first, manager)
            content = backend.render_definition(self._release(first)).decode().replace(
                "[Install]", f'Environment="NETIZEN_ROOT={second.product_root}"\n\n[Install]',
            )
            first.service_file.write_text(content, encoding="utf-8")
            first.service_file.chmod(0o600)
            with self.assertRaisesRegex(InstallError, "unrecognized"):
                backend.capture_definition()


if __name__ == "__main__":
    unittest.main()
