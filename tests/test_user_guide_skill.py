from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from openai_codex import AsyncCodex, CodexConfig

from netizen.experience import COMMAND_SPECS
from netizen.sdk_gap_adapter import AppServerSkillCatalog
from scripts.install_managed_skill import (
    SKILL_NAMES,
    SkillInstallError,
    install_skill,
    remove_skill,
)
from tests.documentation_links import local_link_errors


ROOT = Path(__file__).resolve().parents[1]
SKILL_NAME = "netizen-user-guide"
RELEASE_SKILL = ROOT / "skills" / SKILL_NAME


class UserGuideSkillInstallTest(unittest.TestCase):
    def test_release_skills_install_as_exact_global_copies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            for skill_name in SKILL_NAMES:
                with self.subTest(skill=skill_name):
                    source = ROOT / "skills" / skill_name
                    result = install_skill(
                        source_skill=source,
                        codex_home=codex_home,
                    )
                    target = codex_home / "skills" / skill_name
                    source_files = sorted(
                        path for path in source.rglob("*") if path.is_file()
                    )
                    self.assertEqual(result.file_count, len(source_files))
                    for source_file in source_files:
                        relative = source_file.relative_to(source)
                        with self.subTest(relative=str(relative)):
                            self.assertEqual(
                                (target / relative).read_bytes(),
                                source_file.read_bytes(),
                            )

    def test_install_and_remove_reject_unmanaged_skill_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "release" / "other-skill"
            _write_skill(source, marker="unmanaged-release")
            codex_home = root / "codex-home"
            existing = codex_home / "skills" / "other-skill"
            _write_skill(existing, marker="user-owned")

            with self.assertRaises(SkillInstallError):
                install_skill(source_skill=source, codex_home=codex_home)
            for skill_name in ("other-skill", "../other-skill", str(existing)):
                with self.subTest(skill=skill_name):
                    with self.assertRaises(SkillInstallError):
                        remove_skill(codex_home=codex_home, skill_name=skill_name)

            self.assertEqual((existing / "SKILL.md").read_text(), "user-owned\n")
            self.assertEqual(
                sorted(path.name for path in (codex_home / "skills").iterdir()),
                ["other-skill"],
            )

    def test_install_fully_replaces_only_the_managed_skill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "release" / "skills" / SKILL_NAME
            _write_skill(source, marker="release-v2")
            codex_home = root / "codex-home"
            target = codex_home / "skills" / SKILL_NAME
            _write_skill(target, marker="user-edited-v1")
            (target / "stale.md").write_text("stale\n", encoding="utf-8")
            other_skill = codex_home / "skills" / "other-skill" / "SKILL.md"
            other_skill.parent.mkdir(parents=True)
            other_skill.write_text("other\n", encoding="utf-8")

            first = install_skill(
                source_skill=source,
                codex_home=codex_home,
            )
            second = install_skill(
                source_skill=source,
                codex_home=codex_home,
            )

            self.assertEqual((target / "SKILL.md").read_text(), "release-v2\n")
            self.assertEqual(
                (target / "references" / "user-guide.md").read_text(),
                "guide release-v2\n",
            )
            self.assertFalse((target / "stale.md").exists())
            self.assertEqual(other_skill.read_text(), "other\n")
            self.assertEqual(first.digest, second.digest)
            self.assertEqual(first.file_count, 2)
            self.assertEqual(Path(first.target), target.resolve())
            self.assertEqual(
                sorted(path.name for path in (codex_home / "skills").iterdir()),
                [SKILL_NAME, "other-skill"],
            )

    def test_publish_failure_restores_the_previous_skill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "release" / "skills" / SKILL_NAME
            _write_skill(source, marker="release-v2")
            codex_home = root / "codex-home"
            target = codex_home / "skills" / SKILL_NAME
            _write_skill(target, marker="installed-v1")
            real_replace = os.replace
            replace_calls = 0

            def fail_publish(source_path: object, target_path: object) -> None:
                nonlocal replace_calls
                replace_calls += 1
                if replace_calls == 2:
                    raise OSError("synthetic publish failure")
                real_replace(source_path, target_path)

            with patch(
                "scripts.install_managed_skill.os.replace",
                side_effect=fail_publish,
            ):
                with self.assertRaisesRegex(
                    SkillInstallError,
                    "previous Skill was restored",
                ):
                    install_skill(
                        source_skill=source,
                        codex_home=codex_home,
                    )

            self.assertEqual((target / "SKILL.md").read_text(), "installed-v1\n")
            self.assertEqual(
                (target / "references" / "user-guide.md").read_text(),
                "guide installed-v1\n",
            )
            self.assertEqual(
                sorted(path.name for path in (codex_home / "skills").iterdir()),
                [SKILL_NAME],
            )

    def test_first_install_publish_failure_leaves_no_managed_skill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "release" / "skills" / SKILL_NAME
            _write_skill(source, marker="release-v1")
            codex_home = root / "codex-home"
            target = codex_home / "skills" / SKILL_NAME

            with patch(
                "scripts.install_managed_skill.os.replace",
                side_effect=OSError("synthetic publish failure"),
            ):
                with self.assertRaisesRegex(
                    SkillInstallError,
                    "no managed Skill was installed",
                ):
                    install_skill(
                        source_skill=source,
                        codex_home=codex_home,
                    )

            self.assertFalse(target.exists())
            self.assertEqual(list((codex_home / "skills").iterdir()), [])

    def test_verification_error_restores_the_previous_skill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "release" / "skills" / SKILL_NAME
            _write_skill(source, marker="release-v2")
            codex_home = root / "codex-home"
            target = codex_home / "skills" / SKILL_NAME
            _write_skill(target, marker="installed-v1")

            with _fail_installed_manifest():
                with self.assertRaisesRegex(
                    SkillInstallError,
                    "previous Skill was restored",
                ):
                    install_skill(
                        source_skill=source,
                        codex_home=codex_home,
                    )

            self.assertEqual((target / "SKILL.md").read_text(), "installed-v1\n")
            self.assertEqual(
                sorted(path.name for path in (codex_home / "skills").iterdir()),
                [SKILL_NAME],
            )

    def test_first_install_verification_error_removes_the_new_skill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "release" / "skills" / SKILL_NAME
            _write_skill(source, marker="release-v1")
            codex_home = root / "codex-home"
            target = codex_home / "skills" / SKILL_NAME

            with _fail_installed_manifest():
                with self.assertRaisesRegex(
                    SkillInstallError,
                    "unverified Skill was removed",
                ):
                    install_skill(
                        source_skill=source,
                        codex_home=codex_home,
                    )

            self.assertFalse(target.exists())
            self.assertEqual(list((codex_home / "skills").iterdir()), [])

    def test_release_source_rejects_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "release" / "skills" / SKILL_NAME
            _write_skill(source, marker="release")
            (source / "linked-guide.md").symlink_to(
                source / "references" / "user-guide.md"
            )

            with self.assertRaisesRegex(SkillInstallError, "contains a symlink"):
                install_skill(
                    source_skill=source,
                    codex_home=root / "codex-home",
                )

    def test_rejects_a_symlinked_global_skills_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "release" / "skills" / SKILL_NAME
            _write_skill(source, marker="release")
            codex_home = root / "codex-home"
            codex_home.mkdir()
            external_skills = root / "external-skills"
            external_skills.mkdir()
            (codex_home / "skills").symlink_to(external_skills)

            with self.assertRaisesRegex(
                SkillInstallError,
                "Skills directory must not be a symlink",
            ):
                install_skill(
                    source_skill=source,
                    codex_home=codex_home,
                )

            self.assertEqual(list(external_skills.iterdir()), [])

    def test_rejects_a_filesystem_root_as_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "release" / "skills" / SKILL_NAME
            _write_skill(source, marker="release")

            with self.assertRaisesRegex(SkillInstallError, "filesystem root"):
                install_skill(
                    source_skill=source,
                    codex_home=Path(Path.cwd().anchor),
                )
            with self.assertRaisesRegex(SkillInstallError, "filesystem root"):
                remove_skill(codex_home=Path(Path.cwd().anchor), skill_name=SKILL_NAME)

    def test_pre_feature_rollback_removes_only_the_managed_skill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex-home"
            target = codex_home / "skills" / SKILL_NAME
            _write_skill(target, marker="managed")
            other_skill = codex_home / "skills" / "other-skill" / "SKILL.md"
            other_skill.parent.mkdir(parents=True)
            other_skill.write_text("other\n", encoding="utf-8")

            first = remove_skill(codex_home=codex_home, skill_name=SKILL_NAME)
            second = remove_skill(codex_home=codex_home, skill_name=SKILL_NAME)

            self.assertTrue(first.removed)
            self.assertFalse(second.removed)
            self.assertFalse(target.exists())
            self.assertEqual(other_skill.read_text(), "other\n")

    def test_remove_rejects_a_symlinked_global_skills_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home = root / "codex-home"
            codex_home.mkdir()
            external_skill = root / "external-skills" / SKILL_NAME / "SKILL.md"
            external_skill.parent.mkdir(parents=True)
            external_skill.write_text("external\n", encoding="utf-8")
            (codex_home / "skills").symlink_to(external_skill.parents[1])

            with self.assertRaisesRegex(
                SkillInstallError,
                "Skills directory must not be a symlink",
            ):
                remove_skill(codex_home=codex_home, skill_name=SKILL_NAME)

            self.assertEqual(external_skill.read_text(), "external\n")

    def test_remove_unlinks_a_managed_target_symlink_without_following_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home = root / "codex-home"
            skills_root = codex_home / "skills"
            skills_root.mkdir(parents=True)
            external_skill = root / "external-skill"
            _write_skill(external_skill, marker="external")
            target = skills_root / SKILL_NAME
            target.symlink_to(external_skill, target_is_directory=True)

            result = remove_skill(codex_home=codex_home, skill_name=SKILL_NAME)

            self.assertTrue(result.removed)
            self.assertFalse(target.is_symlink())
            self.assertEqual(
                (external_skill / "SKILL.md").read_text(),
                "external\n",
            )


class UserGuideSkillContentTest(unittest.TestCase):
    def test_installed_guide_navigation_is_self_contained(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for skill_name in SKILL_NAMES:
                with self.subTest(skill=skill_name):
                    installed = install_skill(
                        source_skill=ROOT / "skills" / skill_name,
                        codex_home=Path(directory) / "codex-home",
                    )
                    skill = Path(installed.target)

                    self.assertEqual(local_link_errors(skill, skill.rglob("*.md")), [])

    def test_skill_has_discovery_metadata_and_no_scaffold_placeholders(self) -> None:
        skill = (RELEASE_SKILL / "SKILL.md").read_text(encoding="utf-8")
        guide = (RELEASE_SKILL / "references" / "user-guide.md").read_text(
            encoding="utf-8"
        )
        frontmatter = yaml.safe_load(skill.split("---", 2)[1])

        self.assertEqual(frontmatter["name"], SKILL_NAME)
        description = frontmatter["description"]
        self.assertIsInstance(description, str)
        self.assertTrue(description.strip())
        self.assertNotIn("[TODO:", skill)
        self.assertNotIn("[TODO:", guide)

    def test_guide_covers_the_registered_command_surface(self) -> None:
        guide = (RELEASE_SKILL / "references" / "user-guide.md").read_text(
            encoding="utf-8"
        )

        for spec in COMMAND_SPECS:
            with self.subTest(command=spec.name):
                self.assertIn(f"/{spec.name}", guide)
        self.assertIn("/threads", guide)
        for command in ("model", "effort", "fast", "skills"):
            with self.subTest(unregistered_command=command):
                self.assertIn(f"/{command}", guide)


class UserGuideSkillDiscoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_pinned_codex_discovers_both_managed_global_skills(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home = root / "codex-home"
            user_home = root / "home"
            project = root / "project"
            user_home.mkdir()
            project.mkdir()
            installed = {
                skill_name: install_skill(
                    source_skill=ROOT / "skills" / skill_name,
                    codex_home=codex_home,
                )
                for skill_name in SKILL_NAMES
            }
            env = dict(os.environ)
            env.update(
                {
                    "CODEX_HOME": str(codex_home),
                    "HOME": str(user_home),
                }
            )

            process: object | None = None
            try:
                async with AsyncCodex(CodexConfig(env=env)) as codex:
                    process = codex._client._sync._proc
                    snapshot = await AppServerSkillCatalog(codex).list(
                        project,
                        force_reload=True,
                    )
            finally:
                if process is not None:
                    _close_process_pipes(process)

            self.assertEqual(snapshot.errors, ())
            for skill_name, result in installed.items():
                with self.subTest(skill=skill_name):
                    matches = [
                        skill for skill in snapshot.skills
                        if skill.name == skill_name and skill.enabled
                    ]
                    self.assertEqual(len(matches), 1)
                    self.assertEqual(
                        Path(matches[0].path),
                        Path(result.target) / "SKILL.md",
                    )


def _write_skill(root: Path, *, marker: str) -> None:
    references = root / "references"
    references.mkdir(parents=True)
    (root / "SKILL.md").write_text(f"{marker}\n", encoding="utf-8")
    (references / "user-guide.md").write_text(
        f"guide {marker}\n",
        encoding="utf-8",
    )


def _fail_installed_manifest():
    from scripts import install_managed_skill as installer

    real_manifest = installer._skill_manifest
    calls = 0

    def fail_third_call(root: Path) -> dict[str, str]:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("synthetic installed manifest failure")
        return real_manifest(root)

    return patch(
        "scripts.install_managed_skill._skill_manifest",
        side_effect=fail_third_call,
    )


def _close_process_pipes(process: object) -> None:
    poll = getattr(process, "poll", None)
    wait = getattr(process, "wait", None)
    if callable(poll) and callable(wait) and poll() is None:
        try:
            wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.terminate()
            wait(timeout=1)
    for name in ("stdout", "stderr"):
        stream = getattr(process, name, None)
        if stream is not None:
            stream.close()


if __name__ == "__main__":
    unittest.main()
