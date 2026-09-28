from __future__ import annotations

import concurrent.futures
from dataclasses import replace
import io
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from netizen.deployment.update_protocol import install_lock
from scripts import netizen_installer as installer


ROOT = Path(__file__).resolve().parents[1]


class MultiInstanceInstallerTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.home = self.directory / "home"
        self.home.mkdir()

    def layout(self, root: str | Path | None = None, **kwargs: object) -> installer.Layout:
        return installer.resolve_layout(
            root=root,
            account_home=self.home,
            uid=os.geteuid(),
            username="current-user",
            platform_name="linux",
            **({"environ": {}} | kwargs),
        )

    def test_root_precedence_and_effective_home_expansion(self) -> None:
        environment = {
            "HOME": str(self.directory / "wrong-home"),
            "NETIZEN_ROOT": "~/from environment",
        }
        self.assertEqual(self.layout().product_root, (self.home / ".netizen").resolve())
        self.assertEqual(
            self.layout(environ=environment).product_root,
            (self.home / "from environment").resolve(),
        )
        selected = self.layout("~/共享 team", environ=environment)
        self.assertEqual(selected.product_root, (self.home / "共享 team").resolve())
        self.assertEqual(selected.codex_home, self.home / ".codex")
        self.assertEqual(selected.lark_app_file, selected.product_root / "lark-app/config.json")
        self.assertEqual(selected.config_file, selected.product_root / "config.yaml")
        self.assertEqual(selected.state_dir, selected.product_root / "state")

    def test_default_root_canonicalizes_a_symlinked_account_home(self) -> None:
        alias = self.directory / "home-alias"
        alias.symlink_to(self.home, target_is_directory=True)
        layout = installer.resolve_layout(
            environ={}, account_home=alias, uid=os.geteuid(),
            username="current-user", platform_name="linux",
        )
        canonical = self.layout()
        self.assertEqual(layout.product_root, canonical.product_root)
        self.assertEqual(layout.config_file, canonical.config_file)
        self.assertEqual(layout.state_dir, canonical.state_dir)
        self.assertEqual(layout.service_name, canonical.service_name)

    def test_root_aliases_produce_the_same_platform_targets(self) -> None:
        root = self.home / "实例 one"
        root.mkdir()
        alias = self.home / "alias"
        alias.symlink_to(root, target_is_directory=True)
        canonical = self.layout(root)
        for selection in (str(root) + "/", str(root) + "/../实例 one", alias):
            with self.subTest(selection=selection):
                self.assertEqual(self.layout(selection), canonical)
        with patch.object(Path, "cwd", return_value=self.home):
            self.assertEqual(self.layout("实例 one"), canonical)
        second = self.layout(self.home / "other")
        self.assertNotEqual(canonical.service_name, second.service_name)
        self.assertNotEqual(canonical.service_label, second.service_label)
        self.assertNotEqual(canonical.ready_file, second.ready_file)
        self.assertNotEqual(canonical.lifetime_lock_file, second.lifetime_lock_file)
        self.assertRegex(canonical.root_digest, r"^[0-9a-f]{24}$")

    def test_invalid_roots_are_rejected_without_filesystem_mutation(self) -> None:
        before = set(self.home.iterdir())
        for selection in ("", " ", "/", self.home, "~", "~another/instance", "bad\npath"):
            with self.subTest(selection=selection), self.assertRaises(installer.InstallError):
                self.layout(selection)
        with self.assertRaises(installer.InstallError):
            self.layout(environ={"NETIZEN_ROOT": ""})
        self.assertEqual(set(self.home.iterdir()), before)

    def test_codex_home_cannot_be_the_root_or_a_deletion_subtree(self) -> None:
        root = self.home / "instance"
        for codex_home in (root, root / "releases/codex", root / "cache/codex"):
            with self.subTest(codex_home=codex_home), self.assertRaises(installer.InstallError):
                self.layout(root, environ={"CODEX_HOME": str(codex_home)})
        self.assertFalse(root.exists())

    def test_existing_container_and_unrelated_files_are_preserved(self) -> None:
        layout = self.layout(self.home / "shared folder")
        layout.product_root.mkdir(mode=0o755)
        unrelated = layout.product_root / "my-document.txt"
        unrelated.write_text("owned by user")
        before_mode = stat.S_IMODE(layout.product_root.stat().st_mode)
        installer.prepare_directories(layout)
        installer.prepare_directories(layout)
        self.assertEqual(stat.S_IMODE(layout.product_root.stat().st_mode), before_mode)
        self.assertEqual(unrelated.read_text(), "owned by user")
        marker = layout.product_root / installer.INSTANCE_ROOT_MARKER
        self.assertEqual(marker.read_bytes(), installer.INSTANCE_ROOT_MARKER_CONTENT)
        self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)

    def test_reserved_name_conflict_fails_before_claim_lock_or_chmod(self) -> None:
        for entry in ("state", "releases", "cache", "credentials", "lark-app"):
            with self.subTest(entry=entry):
                layout = self.layout(self.home / entry)
                layout.product_root.mkdir(mode=0o755)
                layout.product_root.chmod(0o755)
                conflict = layout.product_root / entry
                conflict.mkdir(mode=0o755)
                conflict.chmod(0o755)
                user_file = conflict / "user-file"
                user_file.write_bytes(b"do not touch")
                for operation in (installer.prepare_directories, self._lock_once):
                    with self.assertRaises(installer.InstallError):
                        operation(layout)
                self.assertEqual(user_file.read_bytes(), b"do not touch")
                self.assertEqual(stat.S_IMODE(conflict.stat().st_mode), 0o755)
                self.assertEqual(stat.S_IMODE(layout.product_root.stat().st_mode), 0o755)
                self.assertEqual(set(layout.product_root.iterdir()), {conflict})
                self.assertFalse(layout.service_dir.exists())

    def test_existing_root_with_wrong_owner_or_writable_mode_is_not_claimed(self) -> None:
        for case, mode in (("wrong-owner", 0o755), ("group-writable", 0o775), ("world-writable", 0o757)):
            with self.subTest(case=case):
                layout = self.layout(self.home / case)
                layout.product_root.mkdir()
                layout.product_root.chmod(mode)
                unrelated = layout.product_root / "user-file"
                unrelated.write_bytes(b"preserve user file")
                if case == "wrong-owner":
                    # Model a different effective account without changing real ownership.
                    layout = replace(layout, uid=layout.uid + 1)
                for operation in (installer.prepare_directories, self._lock_once):
                    with self.assertRaises(installer.InstallError):
                        operation(layout)
                self.assertEqual(stat.S_IMODE(layout.product_root.stat().st_mode), mode)
                self.assertEqual(unrelated.read_bytes(), b"preserve user file")
                self.assertEqual(set(layout.product_root.iterdir()), {unrelated})
                self.assertFalse(layout.service_dir.exists())

    def test_preconfigured_invalid_admin_secret_fails_before_root_claim(self) -> None:
        for index, content in enumerate((b"invalid", b"A" * 42, b"B" * 43, b"A" * 43 + b"\n")):
            with self.subTest(content=content):
                layout = self.layout(self.home / f"bad-secret-{index}")
                layout.credentials_dir.mkdir(parents=True)
                installer._write_atomic(layout.admin_secret_file, content, mode=0o600)
                before = self._files(layout.product_root)
                before_mode = stat.S_IMODE(layout.credentials_dir.stat().st_mode)
                for operation in (installer.prepare_directories, self._lock_once):
                    with self.assertRaises(installer.InstallError):
                        operation(layout)
                self.assertEqual(self._files(layout.product_root), before)
                self.assertEqual(stat.S_IMODE(layout.credentials_dir.stat().st_mode), before_mode)
                self.assertEqual(set(layout.product_root.iterdir()), {layout.credentials_dir})
                self.assertFalse(layout.service_dir.exists())

    def test_valid_preconfigured_admin_secret_survives_root_claim(self) -> None:
        layout = self.layout(self.home / "preconfigured")
        layout.credentials_dir.mkdir(parents=True)
        installer._write_atomic(layout.admin_secret_file, b"A" * 43, mode=0o600)
        installer.prepare_directories(layout)
        self.assertEqual(layout.admin_secret_file.read_bytes(), b"A" * 43)
        self.assertEqual(stat.S_IMODE(layout.admin_secret_file.stat().st_mode), 0o600)
        self.assertTrue((layout.product_root / installer.INSTANCE_ROOT_MARKER).is_file())

    def test_source_location_allows_checkout_in_unmanaged_root_child(self) -> None:
        layout = self.layout(self.home / "instance")
        checkout = layout.product_root / "my-checkout"
        checkout.mkdir(parents=True)
        document = checkout / "user-file"
        document.write_bytes(b"keep checkout")
        installer._validate_source_location(checkout, layout)
        self.assertEqual(document.read_bytes(), b"keep checkout")
        self.assertFalse((layout.product_root / installer.INSTANCE_ROOT_MARKER).exists())
        for source in (layout.product_root, layout.product_root.parent):
            with self.subTest(source=source), self.assertRaisesRegex(installer.InstallError, "root must not be inside"):
                installer._validate_source_location(source, layout)

    def test_source_location_rejects_all_managed_state_and_deletion_subtrees(self) -> None:
        layout = self.layout(self.home / "source-location")
        for managed in (
            layout.state_dir, layout.cache_dir, layout.releases,
            layout.credentials_dir, layout.lark_app_file.parent, layout.codex_home,
        ):
            nested = managed / "checkout"
            nested.mkdir(parents=True)
            for source in (managed, nested):
                with self.subTest(source=source), self.assertRaisesRegex(installer.InstallError, "overlaps"):
                    installer._validate_source_location(source, layout)
        exact = layout.releases / ("a" * 64) / "source"
        exact.mkdir(parents=True)
        installer._validate_source_location(exact, layout)
        for source in (exact.parent, exact / "nested", layout.releases / "not-a-digest" / "source"):
            source.mkdir(parents=True, exist_ok=True)
            with self.subTest(source=source), self.assertRaisesRegex(installer.InstallError, "overlaps"):
                installer._validate_source_location(source, layout)
        self.assertFalse((layout.product_root / installer.INSTANCE_ROOT_MARKER).exists())

    @staticmethod
    def _lock_once(layout: installer.Layout) -> None:
        with installer.installation_lock(layout):
            pass

    def test_root_marker_supports_concurrent_claim_and_interrupted_retry(self) -> None:
        layout = self.layout(self.home / "concurrent")
        layout.product_root.mkdir()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: installer._claim_instance_root(layout), range(8)))
        self.assertEqual(
            set(layout.product_root.iterdir()),
            {layout.product_root / installer.INSTANCE_ROOT_MARKER},
        )
        installer.prepare_directories(layout)
        self.assertTrue(layout.state_dir.is_dir())
        self.assertTrue(layout.releases.is_dir())

    def test_corrupt_or_nonprivate_root_marker_is_not_repaired(self) -> None:
        for content, mode in ((b"another product\n", 0o600), (installer.INSTANCE_ROOT_MARKER_CONTENT, 0o644)):
            with self.subTest(content=content, mode=mode):
                layout = self.layout(self.home / f"marker-{mode}-{len(content)}")
                layout.product_root.mkdir()
                marker = layout.product_root / installer.INSTANCE_ROOT_MARKER
                marker.write_bytes(content)
                marker.chmod(mode)
                with self.assertRaises(installer.InstallError):
                    installer.prepare_directories(layout)
                self.assertEqual(marker.read_bytes(), content)
                self.assertEqual(stat.S_IMODE(marker.stat().st_mode), mode)
                self.assertFalse(layout.state_dir.exists())

    def test_installation_locks_are_independent_but_aliases_contend(self) -> None:
        first = self.layout(self.home / "first")
        second = self.layout(self.home / "second")
        installer.prepare_directories(first)
        installer.prepare_directories(second)
        alias = self.home / "first-alias"
        alias.symlink_to(first.product_root, target_is_directory=True)
        with installer.installation_lock(first):
            with install_lock(second.product_root, blocking=False):
                pass
            with self.assertRaises(BlockingIOError):
                with install_lock(self.layout(alias).product_root, blocking=False):
                    self.fail("another alias entered the same instance lock")

    def test_uninstall_one_instance_preserves_other_root_and_shared_skills(self) -> None:
        first = self.layout(self.home / "first")
        second = self.layout(self.home / "second")
        for layout in (first, second):
            installer.prepare_directories(layout)
            (layout.releases / "owned-artifact").write_bytes(b"release")
            (layout.cache_dir / "artifact").write_bytes(b"cache")
            (layout.state_dir / "channel.sqlite3").write_bytes(b"persistent state")
            installer._write_atomic(layout.config_file, b"instance: {}\n", mode=0o600)
            (layout.product_root / "user-file").write_bytes(b"user")
        for name in ("netizen-user-guide", "netizen-lark", "user-skill"):
            path = first.codex_home / "skills" / name / "SKILL.md"
            path.parent.mkdir(parents=True)
            path.write_text(name)
        before_second = self._files(second.product_root)
        before_codex = self._files(first.codex_home)
        backend = MagicMock()
        with (
            patch.object(installer, "require_supported_platform"),
            patch.object(installer, "_service_backend", return_value=backend) as factory,
        ):
            installer.uninstall(layout=first)
        self.assertEqual(factory.call_args.args[0], first)
        backend.uninstall_definition.assert_called_once()
        self.assertEqual(self._files(second.product_root), before_second)
        self.assertEqual(self._files(first.codex_home), before_codex)
        self.assertFalse(first.releases.exists())
        self.assertFalse(first.cache_dir.exists())
        self.assertEqual((first.product_root / "user-file").read_bytes(), b"user")
        self.assertEqual((first.state_dir / "channel.sqlite3").read_bytes(), b"persistent state")
        self.assertTrue(first.config_file.is_file())
        self.assertTrue((first.product_root / installer.INSTANCE_ROOT_MARKER).is_file())
        installer.prepare_directories(first)
        self.assertTrue(first.releases.is_dir())

    @staticmethod
    def _files(root: Path) -> dict[Path, bytes]:
        return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}

    def test_unassigned_admin_port_skips_installer_bind_probe(self) -> None:
        with patch.object(installer.socket, "socket") as socket_factory:
            installer.preflight_admin_bind(installer.AdminBind(True, "127.0.0.1", None))
        socket_factory.assert_not_called()

    def test_generated_config_has_instance_state_but_no_allocated_admin_port(self) -> None:
        layout = self.layout(self.home / "configured instance")
        config = yaml.safe_load(installer._default_config(layout))
        self.assertEqual(config["instance"]["dataDir"], str(layout.state_dir))
        self.assertEqual(config["instance"]["projectRoot"], str(self.home / "projects"))
        self.assertTrue(config["adminWeb"]["enabled"])
        self.assertNotIn("port", config["adminWeb"])

    def test_direct_install_rejects_invalid_admin_port_before_side_effects(self) -> None:
        layout = self.layout(self.home / "invalid port")
        for port in (0, 65536, True, "8890"):
            with (
                self.subTest(port=port),
                patch.object(installer, "require_supported_platform") as preflight,
                patch.object(installer, "_service_backend") as backend,
                self.assertRaisesRegex(installer.InstallError, "admin port"),
            ):
                installer.install_source(source_root=ROOT, layout=layout, admin_port=port)
            preflight.assert_not_called()
            backend.assert_not_called()
            self.assertFalse(layout.product_root.exists())

    def test_install_updates_explicit_port_with_candidate_before_validation(self) -> None:
        for port in (None, 8893):
            with self.subTest(port=port):
                layout = self.layout(self.home / f"安装实例 {port}")
                installer.prepare_directories(layout)
                original = (
                    "instance:\n"
                    f"  dataDir: {layout.state_dir}\n"
                    "adminWeb:\n  enabled: true\n  port: 8891\n"
                    "userSection:\n  retained: value\n"
                ).encode()
                installer._write_atomic(layout.config_file, original, mode=0o600)
                release = installer.Release(
                    digest="a" * 64, root=ROOT, source=ROOT, venv=ROOT / ".venv"
                )
                events: list[str] = []
                commands: list[list[str]] = []

                def prepare_candidate(*_args: object, **_kwargs: object) -> installer.Release:
                    events.append("candidate")
                    return release

                def run_candidate(argv: list[object], **kwargs: object) -> subprocess.CompletedProcess[str]:
                    events.append("configure")
                    command = [os.fspath(value) for value in argv]
                    commands.append(command)
                    self.assertEqual(command[0], str(release.venv / "bin/python"))
                    self.assertIn("from netizen.admin.port_config import set_admin_port", command[4])
                    self.assertEqual(command[-2:], [str(layout.config_file), str(port)])
                    self.assertEqual(kwargs["env"]["NETIZEN_ROOT"], str(layout.product_root))
                    return installer.run_command(argv, **kwargs)

                def validate(*_args: object) -> installer.RuntimeValidation:
                    events.append("validate")
                    config = yaml.safe_load(layout.config_file.read_bytes())
                    self.assertEqual(config["adminWeb"]["port"], 8891 if port is None else port)
                    self.assertEqual(config["userSection"], {"retained": "value"})
                    self.assertEqual(stat.S_IMODE(layout.config_file.stat().st_mode), 0o600)
                    return installer.RuntimeValidation(
                        layout.state_dir, installer.AdminBind(True, "127.0.0.1", config["adminWeb"]["port"])
                    )

                with (
                    patch.object(installer, "require_supported_platform"),
                    patch.object(installer, "_service_backend", return_value=MagicMock()),
                    patch.object(installer, "prepare_configuration"),
                    patch.object(installer, "prepare_source_release", side_effect=prepare_candidate),
                    patch.object(installer, "require_codex_login"),
                    patch.object(installer, "require_feishu_permissions"),
                    patch.object(installer, "validate_runtime", side_effect=validate),
                    patch.object(installer, "activate_release", side_effect=lambda *_args, **_kwargs: events.append("activate")),
                ):
                    installed = installer.install_source(
                        source_root=ROOT, layout=layout, runner=run_candidate,
                        interactive=False, admin_port=port,
                    )
                self.assertEqual(installed, release)
                self.assertEqual(
                    events,
                    ["candidate", "validate", "activate"] if port is None
                    else ["candidate", "configure", "validate", "activate"],
                )
                self.assertEqual(len(commands), 0 if port is None else 1)
                if port is None:
                    self.assertEqual(layout.config_file.read_bytes(), original)

    def test_internal_commands_accept_instance_selection(self) -> None:
        selected = str(self.home / "shared 实例")
        cases = (
            ["install-source", "--root", selected, "--admin-port", "8890"],
            ["install-release", "/tmp/candidate", "--root", selected],
            ["service", "--root", selected, "restart"],
            ["uninstall", "--root", selected],
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                args = installer.parse_args(arguments)
                self.assertEqual(os.fspath(args.root), selected)
        args = installer.parse_args(cases[0])
        self.assertEqual(args.admin_port, 8890)
        for value in ("0", "-1", "65536", "abc"):
            with patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
                installer.parse_args(["install-source", "--admin-port", value])

    def test_public_wrappers_preserve_root_and_port_as_argv(self) -> None:
        tools = self.directory / "tools"
        tools.mkdir()
        python = tools / "python3"
        python.write_text(
            '#!/bin/sh\n'
            'for argument in "$@"; do\n'
            '  if [ "$argument" = "-u" ]; then printf "%s\\n" "$@"; exit 0; fi\n'
            'done\n'
        )
        python.chmod(0o700)
        selected = str(self.home / "实例 $name % shared")
        environment = dict(os.environ, PATH=str(tools) + os.pathsep + os.defpath)
        for script, command, arguments in (
            ("dev-install.sh", "install-source", ["--root", selected, "--admin-port", "8890"]),
            ("service.sh", "service", ["--root", selected, "restart"]),
            ("uninstall.sh", "uninstall", ["--root", selected]),
        ):
            with self.subTest(script=script):
                result = subprocess.run(
                    ["/bin/sh", ROOT / script, *arguments],
                    env=environment, check=False, capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                forwarded = result.stdout.splitlines()
                self.assertEqual(forwarded[4:], [command, *arguments])

    def test_latest_and_exact_release_bootstraps_preserve_instance_arguments(self) -> None:
        tools = self.directory / "tools"
        tools.mkdir()
        downloaded = self.directory / "downloaded-installer.sh"
        downloaded.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        curl = tools / "curl"
        curl.write_text(
            '#!/bin/sh\n'
            'while [ "$#" -gt 0 ]; do\n'
            '  case "$1" in -o) output="$2"; shift 2;; *) shift;; esac\n'
            'done\n'
            'cp "$NETIZEN_TEST_ARTIFACT" "$output"\n'
        )
        curl.chmod(0o700)
        python = tools / "python3"
        python.write_text(
            '#!/bin/sh\n'
            'for argument in "$@"; do\n'
            '  if [ "$argument" = "-u" ]; then printf "%s\\n" "$@"; exit 0; fi\n'
            'done\n'
        )
        python.chmod(0o700)
        environment = dict(
            os.environ,
            PATH=str(tools) + os.pathsep + os.defpath,
            TMPDIR=str(self.directory),
            NETIZEN_TEST_ARTIFACT=str(downloaded),
        )
        environment.pop("NETIZEN_UPDATE_OPERATION_ID", None)
        selected = str(self.home / "发布 $name % 实例")
        arguments = ["--root", selected, "--admin-port", "8890"]
        latest = subprocess.run(
            ["/bin/sh", ROOT / "install.sh", *arguments],
            env=environment, stdin=subprocess.DEVNULL,
            check=False, capture_output=True, text=True,
        )
        self.assertEqual(latest.returncode, 0, latest.stderr)
        self.assertEqual(latest.stdout.splitlines(), arguments)
        exact = subprocess.run(
            ["/bin/sh", ROOT / "deploy/install-release.sh.in", *arguments],
            env=environment, stdin=subprocess.DEVNULL,
            check=False, capture_output=True, text=True,
        )
        self.assertEqual(exact.returncode, 0, exact.stderr)
        forwarded = exact.stdout.splitlines()
        self.assertEqual(forwarded[4], "install-release")
        self.assertEqual(forwarded[6:], arguments)

    def test_bootstraps_reject_invalid_arguments_before_download_or_temp_creation(self) -> None:
        tools = self.directory / "tools"
        tools.mkdir()
        marker = self.directory / "unexpected-side-effect"
        for name in ("curl", "python3", "mktemp"):
            command = tools / name
            command.write_text('#!/bin/sh\nprintf invoked > "$NETIZEN_TEST_MARKER"\nexit 97\n')
            command.chmod(0o700)
        environment = dict(
            os.environ, PATH=str(tools), NETIZEN_TEST_MARKER=str(marker),
        )
        cases = (
            ["unexpected"], ["--root"], ["--root", ""], ["--root="],
            ["--admin-port"], ["--admin-port", "0"], ["--admin-port", "65536"],
            ["--admin-port", "-1"], ["--admin-port", "abc"], ["--admin-port="],
            ["--admin-port=1.5"], ["--admin-port=999999999999999999999999"],
        )
        for script in ("install.sh", "deploy/install-release.sh.in"):
            for arguments in cases:
                with self.subTest(script=script, arguments=arguments):
                    result = subprocess.run(
                        ["/bin/sh", ROOT / script, *arguments], env=environment,
                        stdin=subprocess.DEVNULL, check=False, capture_output=True, text=True,
                    )
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("usage:", result.stderr)
                    self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
