from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import venv
import zipfile

from netizen_cli import cli_packages as packages


class PackageConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.home = self.directory / "home"
        self.prefix = self.directory / "environment"
        self.env = {
            "XDG_CONFIG_HOME": str(self.directory / "config"),
            "XDG_DATA_HOME": str(self.directory / "user data"),
            "XDG_DATA_DIRS": os.pathsep.join(str(self.directory / name)
                                          for name in ("global data", "extra data")),
        }
        self.enterContext(patch.object(Path, "home", return_value=self.home))
        self.enterContext(patch.object(sys, "platform", "darwin"))

    def assert_config_rejected(self, config: Path) -> None:
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("[global]\nindex-url = https://example.invalid/simple\n")
        try:
            with self.assertRaises(packages.PackageUpdateError) as error:
                packages._check_tool_config("pip", self.prefix, self.env)
            self.assertIn(str(config), str(error.exception))
        finally:
            config.unlink()

    def test_macos_xdg_data_config_is_not_silently_ignored(self) -> None:
        for directory in ("user data", "global data", "extra data"):
            with self.subTest(directory=directory):
                self.assert_config_rejected(self.directory / directory / "pip/pip.conf")

    def test_macos_fallback_config_is_checked_with_custom_xdg_config_home(self) -> None:
        self.assert_config_rejected(self.home / ".config/pip/pip.conf")

    def test_macos_legacy_and_environment_configs_remain_checked(self) -> None:
        for config in (self.home / "Library/Application Support/pip/pip.conf",
                       self.home / ".pip/pip.conf", self.prefix / "pip.conf"):
            with self.subTest(config=config):
                self.assert_config_rejected(config)

    def test_explicit_null_config_still_disables_file_discovery(self) -> None:
        config = self.directory / "user data/pip/pip.conf"
        config.parent.mkdir(parents=True)
        config.write_text("[global]\nindex-url = https://example.invalid/simple\n")
        packages._check_tool_config("pip", self.prefix, {**self.env, "PIP_CONFIG_FILE": os.devnull})


class PackagePreflightTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.prefix = self.directory / "ordinary env"
        self.work = self.directory / "maintenance"
        self.work.mkdir()
        self.tools = self.directory / "tool root"
        self.tools.mkdir()
        self.cache = self.directory / "cache"
        self.bin_dir = self.directory / "tool bin"
        self.bin_dir.mkdir()
        self.uv = self.directory / "uv"
        self.uv.write_text("synthetic uv")
        self.calls: list[list[str]] = []
        self.environments: list[dict[str, str]] = []
        self.uv_version = "0.12.20"
        self.report: object = {"version": "1", "install": []}
        self.pip_exit = 0
        self.check_exit = 0
        self.dry_run_exit = 0
        self._layout()
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"PATH": str(self.directory)}, clear=True).start()
        patch.object(packages, "_run", side_effect=self.run_command).start()
        patch.object(packages, "_uv_executable", return_value=str(self.uv)).start()
        patch.object(packages, "_config_paths", return_value=set()).start()

    def _layout(self) -> None:
        scripts = self.prefix / "bin"
        scripts.mkdir(parents=True, exist_ok=True)
        self.python = scripts / "python"
        self.python.symlink_to(sys.executable)
        (scripts / "netizen").write_text("synthetic console entry")
        site = self.prefix / "lib/python3.11/site-packages"
        package = site / "netizen_cli"
        package.mkdir(parents=True)
        info = site / "netizen_cli-1.0.dist-info"
        info.mkdir()
        (info / "METADATA").write_text("Name: netizen-cli\nVersion: 1.0\n")
        (info / "INSTALLER").write_text("pip\n")
        (info / "entry_points.txt").write_text("[console_scripts]\nnetizen=netizen_cli.cli:main\n")
        (self.prefix / "pyvenv.cfg").write_text("include-system-site-packages = false\n")
        self.info = {
            "environment_python": str(self.python), "prefix": str(self.prefix),
            "base_prefix": "/synthetic/base-python", "package_dir": str(package),
            "dist_info": str(info), "version": "1.0", "installer": "pip",
            "pip_available": True, "scripts_dir": str(scripts), "externally_managed": False,
        }

    def run_command(self, argv: list[str], *, env: dict[str, str], cwd: Path, **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        self.environments.append(dict(env))
        if "-c" in argv:
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.info), "")
        if "self" in argv:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"version": self.uv_version}), "")
        if argv[-2:] == ["tool", "dir"]:
            return subprocess.CompletedProcess(argv, 0, str(self.tools) + "\n", "")
        if argv[-2:] == ["cache", "dir"]:
            return subprocess.CompletedProcess(argv, 0, str(self.cache) + "\n", "")
        if "--report" in argv:
            if self.pip_exit == 0:
                Path(argv[argv.index("--report") + 1]).write_text(json.dumps(self.report))
            return subprocess.CompletedProcess(argv, self.pip_exit, "irrelevant log wording", "diagnostic")
        if "--check" in argv:
            return subprocess.CompletedProcess(argv, self.check_exit, "logs are not a protocol", "diagnostic")
        if "--dry-run" in argv:
            return subprocess.CompletedProcess(argv, self.dry_run_exit, "new unknown wording", "diagnostic")
        raise AssertionError(f"Unexpected or mutating command during preflight: {argv}")

    def prepare(self, **kwargs: object) -> dict:
        return packages.prepare_update(work_dir=self.work, python=str(self.python), **kwargs)

    def tool_layout(self) -> Path:
        self.prefix = self.tools / "netizen-cli"
        self._layout()
        self.info["installer"] = "uv"
        destination = self.bin_dir / "netizen"
        destination.symlink_to(self.prefix / "bin/netizen")
        receipt = self.prefix / "uv-receipt.toml"
        receipt.write_text(
            '[tool]\nrequirements = [{name="netizen-cli", specifier="<2"}]\n'
            'entrypoints = [{name="netizen", from="netizen-cli", install-path='
            + json.dumps(str(destination)) + '}]\n'
            '[tool.options]\nno-index=true\n'
        )
        return receipt

    def test_pip_empty_stable_report_does_not_request_restart(self) -> None:
        plan = self.prepare()
        self.assertEqual(plan["backend"], "pip")
        self.assertFalse(plan["changes_required"])
        self.assertEqual(plan["command"][:4], [str(self.python), "-I", "-m", "pip"])
        self.assertNotEqual(plan["environment_python"], str(self.python.resolve()))
        self.assertEqual(plan["command_env"]["PIP_CONFIG_FILE"], os.devnull)
        self.assertEqual(json.loads(json.dumps(plan)), plan)

    def test_dependency_only_change_still_requires_update(self) -> None:
        self.report = {"version": "1", "install": [{"metadata": {"name": "dependency", "version": "2"}}]}
        self.assertTrue(self.prepare()["changes_required"])

    def test_pip_report_version_and_shape_are_validated(self) -> None:
        for report in ({"version": "2", "install": []}, {"version": "1"},
                       {"version": "1", "install": [{}]}, []):
            with self.subTest(report=report):
                self.report = report
                (self.work / "pip-preflight-report.json").unlink(missing_ok=True)
                with self.assertRaises(packages.PackageUpdateError):
                    self.prepare()

    def test_failed_pip_preflight_never_uses_stale_empty_plan(self) -> None:
        self.pip_exit = 1
        with self.assertRaisesRegex(packages.PackageUpdateError, "pip dependency preflight failed") as error:
            self.prepare()
        self.assertEqual(error.exception.diagnostic, "diagnostic")
        self.assertFalse(any("tool" in call and "upgrade" in call for call in self.calls))

    def test_stale_preflight_file_is_not_overwritten(self) -> None:
        (self.work / "pip-preflight-report.json").write_text('{"version":"1","install":[]}')
        with self.assertRaisesRegex(packages.PackageUpdateError, "already exists"):
            self.prepare()

    def test_uv_pip_no_change_uses_exit_zero_only(self) -> None:
        self.info["installer"] = "uv"
        self.info["pip_available"] = False
        plan = self.prepare()
        self.assertEqual(plan["backend"], "uv-pip")
        self.assertFalse(plan["changes_required"])
        self.assertIn(str(self.python), plan["command"])
        self.assertFalse(any("--dry-run" in call for call in self.calls))

    def test_uv_check_one_requires_a_successful_subsequent_resolution(self) -> None:
        self.info["installer"] = "uv"
        self.check_exit = 1
        self.assertTrue(self.prepare()["changes_required"])
        checks = [call[-1] for call in self.calls if call[-1] in ("--check", "--dry-run")]
        self.assertEqual(checks, ["--check", "--dry-run"])

    def test_uv_resolution_error_is_not_an_update_plan(self) -> None:
        self.info["installer"] = "uv"
        self.check_exit = self.dry_run_exit = 1
        with self.assertRaisesRegex(packages.PackageUpdateError, "dependency preflight failed"):
            self.prepare()

    def test_uv_unknown_check_status_is_not_a_change(self) -> None:
        self.info["installer"] = "uv"
        self.check_exit = 2
        with self.assertRaisesRegex(packages.PackageUpdateError, "no-change preflight failed"):
            self.prepare()
        self.assertFalse(any("--dry-run" in call for call in self.calls))

    def test_uv_tool_preserves_receipt_and_accepts_no_op_restart(self) -> None:
        receipt = self.tool_layout()
        original = receipt.read_bytes()
        plan = self.prepare()
        self.assertEqual(plan["backend"], "uv-tool")
        self.assertTrue(plan["changes_required"])
        self.assertEqual(plan["command"][-3:], ["tool", "upgrade", "netizen-cli"])
        self.assertNotIn("--python", plan["command"])
        self.assertNotIn("--all", plan["command"])
        self.assertEqual(plan["command_env"]["UV_TOOL_DIR"], str(self.tools))
        self.assertEqual(plan["command_env"]["UV_TOOL_BIN_DIR"], str(self.bin_dir))
        self.assertEqual(receipt.read_bytes(), original)
        self.assertIn(str(receipt), plan["revalidation_files"])
        self.assertFalse(any("--check" in call or "--dry-run" in call for call in self.calls))

    def test_tool_root_path_alias_is_canonicalized(self) -> None:
        self.tool_layout()
        actual = self.tools
        self.tools = self.directory / "tool root alias"
        self.tools.symlink_to(actual, target_is_directory=True)
        self.assertEqual(self.prepare()["command_env"]["UV_TOOL_DIR"], str(actual))

    def test_whole_tool_environment_symlink_is_not_a_supported_layout(self) -> None:
        self.tool_layout()
        original = self.prefix
        moved = self.directory / "moved environment"
        original.rename(moved)
        original.symlink_to(moved, target_is_directory=True)
        self.info.update({key: str(Path(value).resolve()) for key, value in self.info.items()
                          if key in ("prefix", "package_dir", "dist_info", "scripts_dir")})
        with self.assertRaisesRegex(packages.PackageUpdateError, "symlinked whole uv tool"):
            self.prepare()

    def test_same_named_other_tool_does_not_hijack_ordinary_uv_env(self) -> None:
        (self.tools / "netizen-cli").mkdir()
        self.info["installer"] = "uv"
        self.assertEqual(self.prepare()["backend"], "uv-pip")

    def test_lost_custom_tool_root_never_falls_back(self) -> None:
        self.tool_layout()
        self.tools = self.directory / "unrelated tools"
        self.tools.mkdir()
        with self.assertRaisesRegex(packages.PackageUpdateError, "UV_TOOL_DIR does not match"):
            self.prepare()

    def test_missing_and_invalid_tool_receipts_fail_before_stop(self) -> None:
        receipt = self.tool_layout()
        for value in (None, "not = valid = toml", '[tool]\nrequirements=[{name="other"}]\n'):
            with self.subTest(value=value):
                if value is None:
                    receipt.unlink()
                else:
                    receipt.write_text(value)
                with self.assertRaisesRegex(packages.PackageUpdateError, "uv tool identity"):
                    self.prepare()

    def test_tool_command_link_must_point_into_this_environment(self) -> None:
        self.tool_layout()
        destination = self.bin_dir / "netizen"
        destination.unlink()
        destination.write_text("another installed command")
        with self.assertRaisesRegex(packages.PackageUpdateError, "uv tool identity"):
            self.prepare()

    def test_uv_pip_compatibility_uses_public_capabilities_not_tool_receipt_version(self) -> None:
        self.info["installer"] = "uv"
        self.uv_version = "99.0.0"
        self.assertEqual(self.prepare()["backend"], "uv-pip")
        self.check_exit = 1
        self.assertTrue(self.prepare()["changes_required"])
        self.check_exit = 2
        with self.assertRaisesRegex(packages.PackageUpdateError, "no-change preflight failed"):
            self.prepare()

    def test_uv_tool_unknown_version_fails_before_using_private_receipt(self) -> None:
        self.tool_layout()
        self.uv_version = "99.0.0"
        with self.assertRaisesRegex(packages.PackageUpdateError, "outside the tested"):
            self.prepare()
        self.assertFalse(any("upgrade" in call for call in self.calls))

    def test_installer_is_a_default_not_ownership_of_an_ordinary_environment(self) -> None:
        self.assertEqual(self.prepare(backend="uv-pip")["backend"], "uv-pip")
        self.info["installer"] = "uv"
        self.assertEqual(self.prepare(backend="pip")["backend"], "pip")

    def test_explicit_backend_cannot_override_unknown_installers(self) -> None:
        self.info["installer"] = "other-manager"
        for backend in (None, "pip", "uv-pip"):
            with self.subTest(backend=backend):
                with self.assertRaisesRegex(packages.PackageUpdateError, "unsupported"):
                    self.prepare(backend=backend)

    def test_uv_cache_root_is_rejected_before_ordinary_backend_choice(self) -> None:
        # No private bucket name is needed when the selected uv identifies it.
        self.prefix = self.cache / "unfamiliar-layout" / "cached-environment"
        self._layout()
        self.info["installer"] = "uv"
        for backend in (None, "pip", "uv-pip", "uv-tool"):
            with self.subTest(backend=backend):
                with self.assertRaisesRegex(packages.PackageUpdateError, "disposable uv cache"):
                    self.prepare(backend=backend)
        self.assertFalse(any("install" in call or "upgrade" in call for call in self.calls))

    def test_cached_archive_is_rejected_even_if_original_cache_root_or_installer_is_lost(self) -> None:
        self.prefix = self.directory / "original-cache" / "archive-v0" / "cached-environment"
        self._layout()
        for installer in ("uv", "pip", ""):
            self.info["installer"] = installer
            for backend in (None, "pip", "uv-pip"):
                with self.subTest(installer=installer, backend=backend):
                    with self.assertRaisesRegex(packages.PackageUpdateError, "disposable uv cache"):
                        self.prepare(backend=backend)
        self.assertFalse(any("install" in call or "upgrade" in call for call in self.calls))

    def test_explicit_backend_cannot_override_tool_ownership(self) -> None:
        self.tool_layout()
        for backend in ("pip", "uv-pip"):
            with self.subTest(backend=backend):
                with self.assertRaisesRegex(packages.PackageUpdateError, "cannot take it over"):
                    self.prepare(backend=backend)

    def test_missing_installer_requires_intent_and_never_installs_pip(self) -> None:
        self.info["installer"] = ""
        with self.assertRaisesRegex(packages.PackageUpdateError, "Installer identity is absent"):
            self.prepare()
        self.assertEqual(self.prepare(backend="pip")["backend"], "pip")
        self.info["pip_available"] = False
        with self.assertRaisesRegex(packages.PackageUpdateError, "has no pip"):
            self.prepare(backend="pip")

    def test_unsupported_managers_shared_sites_and_project_locks_fail(self) -> None:
        for marker in (self.prefix / "pipx_metadata.json", self.prefix / "conda-meta", self.prefix.parent / "uv.lock"):
            with self.subTest(marker=marker.name):
                marker.touch()
                with self.assertRaises(packages.PackageUpdateError):
                    self.prepare()
                marker.unlink()
        (self.prefix / "pyvenv.cfg").write_text("include-system-site-packages = true\n")
        with self.assertRaisesRegex(packages.PackageUpdateError, "Shared system-site-packages"):
            self.prepare()

    def test_target_redirection_and_relative_file_options_are_rejected(self) -> None:
        for key, value in (("PIP_TARGET", "/another/environment"), ("PIP_ROOT", "/elsewhere"),
                           ("PIP_BREAK_SYSTEM_PACKAGES", "1"), ("PIP_CONSTRAINT", "relative.txt")):
            with self.subTest(key=key), patch.dict(os.environ, {key: value}):
                with self.assertRaisesRegex(packages.PackageUpdateError, key):
                    self.prepare()

    def test_nonempty_value_options_are_not_mistaken_for_boolean_false(self) -> None:
        options = {
            "pip": ("PIP_TARGET", "PIP_PREFIX", "PIP_ROOT", "PIP_PYTHON",
                    "PIP_REQUIREMENT", "PIP_EDITABLE", "PIP_REPORT"),
            "uv-pip": ("UV_TARGET", "UV_PREFIX", "UV_PYTHON", "UV_PROJECT_ENVIRONMENT",
                       "UV_WORKING_DIR", "UV_PROJECT", "UV_REINSTALL_PACKAGE"),
        }
        for backend, keys in options.items():
            for key in keys:
                for value in ("false", "0", "no", "False", ""):
                    with self.subTest(backend=backend, key=key, value=value):
                        if value:
                            with self.assertRaisesRegex(packages.PackageUpdateError, key):
                                packages._check_environment_options(backend, {key: value})
                        else:
                            packages._check_environment_options(backend, {key: value})

    def test_false_target_redirect_fails_before_dependency_resolution(self) -> None:
        for value in ("false", "0", "no"):
            with self.subTest(value=value), patch.dict(os.environ, {"PIP_TARGET": value}):
                with self.assertRaisesRegex(packages.PackageUpdateError, "PIP_TARGET"):
                    self.prepare()
        self.assertFalse(any("install" in call for call in self.calls))

    def test_boolean_options_can_be_explicitly_disabled(self) -> None:
        for backend, key in (("pip", "PIP_USER"), ("pip", "PIP_BREAK_SYSTEM_PACKAGES"),
                             ("uv-pip", "UV_SYSTEM_PYTHON"), ("uv-pip", "UV_REINSTALL")):
            for value in ("false", "0", "no", "False", ""):
                with self.subTest(backend=backend, key=key, value=value):
                    packages._check_environment_options(backend, {key: value})

    def test_index_credentials_are_inherited_not_saved_in_plan(self) -> None:
        with patch.dict(os.environ, {"PIP_INDEX_URL": "https://user:secret@example.test/simple"}):
            plan = self.prepare()
        self.assertNotIn("secret", json.dumps(plan))
        self.assertEqual(self.environments[-1]["PIP_INDEX_URL"], "https://user:secret@example.test/simple")

    def test_custom_config_is_not_silently_ignored(self) -> None:
        config = self.directory / "pip.conf"
        config.write_text("[global]\nindex-url = https://private.test/simple\n")
        with patch.object(packages, "_config_paths", return_value={config}):
            with self.assertRaisesRegex(packages.PackageUpdateError, "Custom package configuration"):
                self.prepare()
            with patch.dict(os.environ, {"PIP_CONFIG_FILE": os.devnull}):
                self.assertEqual(self.prepare()["backend"], "pip")

    def test_preflight_target_replacement_is_detected(self) -> None:
        original = self.run_command

        def changing_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            result = original(argv, **kwargs)
            if "--report" in argv:
                (Path(self.info["dist_info"]) / "METADATA").write_text("changed during preflight")
            return result

        with patch.object(packages, "_run", side_effect=changing_run):
            with self.assertRaisesRegex(packages.PackageUpdateError, "changed during preflight"):
                self.prepare()


class InstalledIdentityProbeTest(unittest.TestCase):
    def test_fresh_isolated_probe_ignores_cwd_and_retains_venv_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prefix = root / "environment with spaces"
            venv.EnvBuilder(with_pip=False).create(prefix)
            python = prefix / "bin/python"
            result = subprocess.run(
                [str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                text=True, capture_output=True, check=True,
            )
            site = Path(result.stdout.strip())
            package = site / "netizen_cli"
            package.mkdir()
            (package / "__init__.py").write_text("")
            for module in ("cli", "cli_services", "package_resources"):
                (package / f"{module}.py").write_text("def main(): pass\n")
            info = site / "netizen_cli-1.0.dist-info"
            info.mkdir()
            (info / "METADATA").write_text("Metadata-Version: 2.1\nName: netizen-cli\nVersion: 1.0\n")
            (info / "entry_points.txt").write_text("[console_scripts]\nnetizen = netizen_cli.cli:main\n")
            (info / "RECORD").write_text("netizen_cli-1.0.dist-info/METADATA,,\n")
            (root / "netizen_cli.py").write_text("raise RuntimeError('cwd shadow')\n")
            completed = subprocess.run(
                [str(python), "-I", "-c", packages._PROBE, "{}", "import"], cwd=root,
                text=True, capture_output=True, check=True,
            )
            identity = json.loads(completed.stdout)
            self.assertEqual(identity["environment_python"], str(python))
            self.assertEqual(identity["prefix"], str(prefix))
            self.assertEqual(identity["version"], "1.0")
            self.assertFalse(identity["pip_available"])
            (info / "direct_url.json").write_text('{"dir_info":{"editable":true},"url":"file:///source"}')
            rejected = subprocess.run(
                [str(python), "-I", "-c", packages._PROBE], cwd=root,
                text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("require manual maintenance", json.loads(rejected.stdout)["error"])


@unittest.skipUnless(os.environ.get("NETIZEN_TEST_UV"), "set NETIZEN_TEST_UV to a tested uv binary for isolated native probes")
class NativeUvPreflightTest(unittest.TestCase):
    """No network, real application installs or system service operations."""

    def setUp(self) -> None:
        self.uv = Path(os.environ["NETIZEN_TEST_UV"]).absolute()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.wheels = self.directory / "wheels"
        self.wheels.mkdir()
        self._wheel("1.0")
        environment = {
            "HOME": str(self.directory), "PATH": str(self.uv.parent) + os.pathsep + os.defpath,
            "UV_TOOL_DIR": str(self.directory / "tools"),
            "UV_TOOL_BIN_DIR": str(self.directory / "bin"),
            "UV_CACHE_DIR": str(self.directory / "cache"), "UV_PYTHON_DOWNLOADS": "never",
            "UV_FIND_LINKS": str(self.wheels), "UV_OFFLINE": "true",
        }
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, environment, clear=True).start()

    def _wheel(self, version: str) -> None:
        info = f"netizen_cli-{version}.dist-info"
        entries = {
            "netizen_cli/__init__.py": "",
            "netizen_cli/cli.py": "def main(): pass\n",
            "netizen_cli/cli_services.py": "",
            "netizen_cli/package_resources.py": "",
            f"{info}/METADATA": f"Metadata-Version: 2.1\nName: netizen-cli\nVersion: {version}\n",
            f"{info}/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            f"{info}/entry_points.txt": "[console_scripts]\nnetizen = netizen_cli.cli:main\n",
        }
        entries[f"{info}/RECORD"] = "".join(f"{path},,\n" for path in (*entries, f"{info}/RECORD"))
        with zipfile.ZipFile(self.wheels / f"netizen_cli-{version}-py3-none-any.whl", "w") as wheel:
            for path, body in entries.items():
                wheel.writestr(path, body)

    def command(self, *args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [str(self.uv), "--no-config", *args, "--no-index"], cwd=self.directory,
            text=True, capture_output=True, check=False, timeout=45,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def plan(self, python: Path, *, backend: str | None = None) -> dict:
        work = Path(tempfile.mkdtemp(prefix="work-", dir=self.directory))
        return packages.prepare_update(work_dir=work, python=str(python), backend=backend)

    def test_native_ordinary_environment_accepts_explicit_maintenance_tool(self) -> None:
        prefix = self.directory / "ordinary with pip"
        venv.EnvBuilder(with_pip=True).create(prefix)
        python = prefix / "bin/python"
        with patch.dict(os.environ, {"PIP_CONFIG_FILE": os.devnull, "PIP_NO_INDEX": "1",
                                    "PIP_FIND_LINKS": str(self.wheels)}):
            installed = subprocess.run(
                [str(python), "-I", "-m", "pip", "install", "netizen-cli==1.0"],
                cwd=self.directory, text=True, capture_output=True, check=False, timeout=45,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            uv_plan = self.plan(python, backend="uv-pip")
            self.assertEqual(uv_plan["backend"], "uv-pip")
            self.assertFalse(uv_plan["changes_required"])
            self.command("pip", "install", "--python", str(python), "--reinstall", "netizen-cli==1.0")
            pip_plan = self.plan(python, backend="pip")
            self.assertEqual(pip_plan["backend"], "pip")
            self.assertFalse(pip_plan["changes_required"])

    def test_native_uv_pip_no_change_change_and_resolution_failure(self) -> None:
        prefix = self.directory / "ordinary"
        venv.EnvBuilder(with_pip=False).create(prefix)
        python = prefix / "bin/python"
        self.command("pip", "install", "--python", str(python), "netizen-cli==1.0")
        self.assertFalse(self.plan(python)["changes_required"])
        self._wheel("2.0")
        plan = self.plan(python)
        self.assertTrue(plan["changes_required"])
        self.assertEqual(plan["before_version"], "1.0")
        # Preflight did not mutate the environment even when a newer wheel exists.
        validation = subprocess.run(plan["validation_command"], text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(validation.stdout)["version"], "1.0")
        constraint = self.directory / "constraints.txt"
        constraint.write_text("netizen-cli==999.0\n")
        with patch.dict(os.environ, {"UV_CONSTRAINT": str(constraint)}):
            with self.assertRaisesRegex(packages.PackageUpdateError, "dependency preflight failed"):
                self.plan(python)

    def test_native_uv_tool_receipt_target_and_fresh_validation(self) -> None:
        self.command("tool", "install", "--python", sys.executable, "netizen-cli<3")
        python = Path(os.environ["UV_TOOL_DIR"]) / "netizen-cli/bin/python"
        plan = self.plan(python)
        self.assertEqual(plan["backend"], "uv-tool")
        self.assertTrue(plan["changes_required"])
        self._wheel("2.0")
        updated = subprocess.run(
            plan["command"], env=dict(os.environ) | plan["command_env"], cwd=plan["command_cwd"],
            text=True, capture_output=True, check=False, timeout=45,
        )
        self.assertEqual(updated.returncode, 0, updated.stderr)
        validation = subprocess.run(plan["validation_command"], text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(validation.stdout)["version"], "2.0")
        self.assertTrue(self.plan(python)["changes_required"])  # Tool no-op still restores originally running set.

    def test_native_pinned_tool_run_cache_survives_attempted_update(self) -> None:
        command = [
            str(self.uv), "--no-config", "tool", "run", "--no-index",
            "--python", sys.executable, "--from", "netizen-cli==1.0", "python", "-I", "-c",
            "import sys,json,importlib.metadata as m; "
            "print(json.dumps({'python':sys.executable,'version':m.version('netizen-cli')}))",
        ]

        def pinned_tool() -> dict:
            result = subprocess.run(command, cwd=self.directory, text=True, capture_output=True,
                                    check=False, timeout=45)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        before = pinned_tool()
        self.assertEqual(before["version"], "1.0")
        self._wheel("2.0")
        with self.assertRaisesRegex(packages.PackageUpdateError, "disposable uv cache"):
            self.plan(Path(before["python"]))
        # uv caches this exact requirement. Accidentally upgrading the cached
        # environment would make the next pinned invocation incorrectly run 2.0.
        self.assertEqual(pinned_tool(), before)


@unittest.skipUnless(os.environ.get("NETIZEN_TEST_PIP"), "set NETIZEN_TEST_PIP=1 for an isolated ensurepip/native report probe")
class NativePipPreflightTest(unittest.TestCase):
    def test_native_pip_report_no_change_and_real_update(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.wheels = root / "wheels"
            self.wheels.mkdir()
            NativeUvPreflightTest._wheel(self, "1.0")
            prefix = root / "ordinary"
            venv.EnvBuilder(with_pip=True).create(prefix)
            python = prefix / "bin/python"
            env = {"PATH": os.defpath, "HOME": str(root), "PIP_CONFIG_FILE": os.devnull,
                   "PIP_NO_INDEX": "1", "PIP_FIND_LINKS": str(self.wheels)}
            with patch.dict(os.environ, env, clear=True):
                subprocess.run([str(python), "-I", "-m", "pip", "install", "netizen-cli==1.0"],
                               cwd=root, text=True, capture_output=True, check=True, timeout=45)
                work = root / "no-op"
                work.mkdir()
                no_op = packages.prepare_update(work_dir=work, python=str(python))
                self.assertFalse(no_op["changes_required"])
                NativeUvPreflightTest._wheel(self, "2.0")
                work = root / "update"
                work.mkdir()
                plan = packages.prepare_update(work_dir=work, python=str(python))
                self.assertTrue(plan["changes_required"])
                result = subprocess.run(plan["command"], env=env | plan["command_env"], cwd=work,
                                        text=True, capture_output=True, check=False, timeout=45)
                self.assertEqual(result.returncode, 0, result.stderr)
                validation = subprocess.run(plan["validation_command"], text=True, capture_output=True, check=True)
                self.assertEqual(json.loads(validation.stdout)["version"], "2.0")


if __name__ == "__main__":
    unittest.main()
