from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from netizen_cli.builtin_skills import (
    BUILTIN_SKILL_NAMES,
    BuiltinSkillError,
    builtin_skill_root,
    validate_builtin_skills,
)
from netizen_cli.experience import COMMAND_SPECS
from tests.documentation_links import local_link_errors


ROOT = Path(__file__).resolve().parents[1]
RELEASE_SKILL = ROOT / "skills" / "netizen-user-guide"


class BuiltinSkillsTest(unittest.TestCase):
    def test_release_skills_are_complete_and_self_contained(self) -> None:
        skills = validate_builtin_skills(ROOT)
        for name in BUILTIN_SKILL_NAMES:
            skill = skills / name
            self.assertEqual(local_link_errors(skill, skill.rglob("*.md")), [])

    def test_development_source_is_derived_from_module_not_cwd(self) -> None:
        with patch("pathlib.Path.cwd", side_effect=AssertionError("do not read cwd")):
            self.assertEqual(builtin_skill_root(), ROOT / "skills")

    def test_installed_runtime_uses_its_own_package_resources(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            for prefix in (root / "arbitrary-venv", root / "global", root / "user-site"):
                module = prefix / "lib" / "netizen_cli" / "builtin_skills.py"
                resources = module.parent / "resources"
                shutil.copytree(ROOT / "skills", resources / "skills")
                module.touch()
                with self.subTest(prefix=prefix), patch("pathlib.Path.cwd", side_effect=AssertionError):
                    self.assertEqual(builtin_skill_root(package_file=module), resources / "skills")

    def test_damaged_package_resources_do_not_fall_back_to_source(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw)
            shutil.copytree(ROOT / "skills", source / "skills")
            (source / "pyproject.toml").touch()
            package = source / "netizen_cli"
            (package / "resources").mkdir(parents=True)
            with self.assertRaises(BuiltinSkillError):
                builtin_skill_root(package_file=package / "builtin_skills.py")

    def test_unrecognized_package_layout_fails_without_global_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with self.assertRaises(BuiltinSkillError):
                builtin_skill_root(package_file=root / "other.py")
            with self.assertRaises(BuiltinSkillError):
                builtin_skill_root(package_file=root / "netizen_cli" / "__init__.py")

    def test_missing_reference_entrypoint_and_symlink_fail_closed(self) -> None:
        for damage in ("reference", "entrypoint", "symlink", "external-reference", "empty"):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as raw:
                source = Path(raw)
                shutil.copytree(ROOT / "skills", source / "skills")
                skill = source / "skills" / "netizen-user-guide"
                guide = skill / "references" / "user-guide.md"
                if damage == "reference":
                    guide.unlink()
                elif damage == "entrypoint":
                    (skill / "SKILL.md").unlink()
                elif damage == "symlink":
                    guide.unlink()
                    guide.symlink_to(RELEASE_SKILL / "references" / "user-guide.md")
                elif damage == "external-reference":
                    guide.write_text("[external](../../netizen-lark/SKILL.md)\n", encoding="utf-8")
                else:
                    guide.write_text("", encoding="utf-8")
                with self.assertRaises(BuiltinSkillError):
                    validate_builtin_skills(source)


class UserGuideSkillContentTest(unittest.TestCase):
    def test_skill_has_discovery_metadata_and_no_scaffold_placeholders(self) -> None:
        for name in BUILTIN_SKILL_NAMES:
            text = (ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
            metadata = yaml.safe_load(text.split("---", 2)[1])
            self.assertEqual(metadata["name"], name)
            self.assertTrue(metadata["description"].strip())
            self.assertNotIn("[TODO:", text)

    def test_guide_covers_the_registered_command_surface(self) -> None:
        guide = (RELEASE_SKILL / "references" / "user-guide.md").read_text(encoding="utf-8")
        for spec in COMMAND_SPECS:
            with self.subTest(command=spec.name):
                self.assertIn(f"/{spec.name}", guide)
        self.assertIn("/threads", guide)
        for command in ("model", "effort", "fast", "skills"):
            self.assertIn(f"/{command}", guide)


if __name__ == "__main__":
    unittest.main()
