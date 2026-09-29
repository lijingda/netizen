from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile

from netizen_cli.package_resources import resource_path


ROOT = Path(__file__).resolve().parents[1]


class PackageResourceTests(unittest.TestCase):
    def test_source_checkout_uses_canonical_config(self) -> None:
        self.assertEqual(resource_path("config.example.yaml"), ROOT / "config.example.yaml")

    def test_parent_traversal_and_absolute_paths_are_rejected(self) -> None:
        for value in ("../pyproject.toml", str(ROOT / "config.example.yaml")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                resource_path(value)


class BuiltArtifactTests(unittest.TestCase):
    """Offline backend build + isolated imports, never install in the active env."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="netizen-wheel-test-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.directory = Path(cls.temporary.name).resolve()
        cls.source = cls.directory / "source"
        cls.source.mkdir()
        for name in ("pyproject.toml", "_build.py", "MANIFEST.in", "config.example.yaml"):
            shutil.copyfile(ROOT / name, cls.source / name)
        for name in ("netizen_cli", "skills"):
            shutil.copytree(ROOT / name, cls.source / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        cls._build(cls.source, "build_wheel")
        cls.wheel = next((cls.source / "dist").glob("*.whl"))

    @classmethod
    def _build(cls, source: Path, operation: str) -> None:
        command = (
            "from setuptools import build_meta; "
            f"build_meta.{operation}('dist')"
        )
        result = subprocess.run(
            [os.environ.get("NETIZEN_TEST_BUILD_PYTHON", sys.executable), "-E", "-c", command], cwd=source,
            capture_output=True, text=True, check=False,
        )
        if result.returncode:
            raise AssertionError(
                f"{operation} failed; install the pyproject build-system requirements in the dev "
                "environment or point NETIZEN_TEST_BUILD_PYTHON at a prepared builder:\n"
                f"{result.stdout}\n{result.stderr}"
            )

    def test_wheel_contains_runtime_resources_and_no_legacy_import_package(self) -> None:
        with zipfile.ZipFile(self.wheel) as wheel:
            names = set(wheel.namelist())
            for path in (ROOT / "skills").rglob("*"):
                if path.is_file():
                    target = "netizen_cli/resources/" + path.relative_to(ROOT).as_posix()
                    self.assertEqual(wheel.read(target), path.read_bytes())
            self.assertEqual(wheel.read("netizen_cli/resources/config.example.yaml"),
                             (ROOT / "config.example.yaml").read_bytes())
            self.assertIn("netizen_cli/service_launcher.py", names)
            self.assertIn("netizen_cli/admin/static/index.html", names)
            self.assertFalse(any(name.startswith("netizen/") for name in names))
            entrypoint = next(name for name in names if name.endswith(".dist-info/entry_points.txt"))
            self.assertIn("netizen = netizen_cli.cli:main", wheel.read(entrypoint).decode())

    def test_extracted_wheel_finds_its_resources_without_source_or_cwd(self) -> None:
        installation = self.directory / "isolated site with spaces"
        with zipfile.ZipFile(self.wheel) as wheel:
            wheel.extractall(installation)
        code = (
            "import json,sys; sys.path.insert(0,sys.argv[1]); "
            "from netizen_cli.builtin_skills import builtin_skill_root; "
            "from netizen_cli.package_resources import resource_path; "
            "print(json.dumps([str(builtin_skill_root()),str(resource_path('config.example.yaml'))]))"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-c", code, str(installation)], cwd=self.directory,
            check=True, capture_output=True, text=True,
        )
        self.assertEqual(json.loads(result.stdout), [
            str(installation / "netizen_cli/resources/skills"),
            str(installation / "netizen_cli/resources/config.example.yaml"),
        ])

    def test_sdist_rebuild_has_the_same_resource_bytes(self) -> None:
        self._build(self.source, "build_sdist")
        archive = next((self.source / "dist").glob("*.tar.gz"))
        extracted = self.directory / "sdist"
        with tarfile.open(archive) as source:
            # This archive was just produced from the private synthetic checkout.
            source.extractall(extracted)
        source_root = next(extracted.iterdir())
        self._build(source_root, "build_wheel")
        rebuilt = next((source_root / "dist").glob("*.whl"))
        with zipfile.ZipFile(self.wheel) as direct, zipfile.ZipFile(rebuilt) as indirect:
            resources = {name for name in direct.namelist() if name.startswith("netizen_cli/resources/")}
            self.assertEqual(resources, {name for name in indirect.namelist() if name.startswith("netizen_cli/resources/")})
            for name in resources:
                self.assertEqual(direct.read(name), indirect.read(name))


if __name__ == "__main__":
    unittest.main()
