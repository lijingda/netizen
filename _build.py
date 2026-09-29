"""Setuptools hook: ship canonical repository resources without a second copy."""

from pathlib import Path
import shutil

from setuptools.command.build_py import build_py


class BuildPy(build_py):
    def run(self) -> None:
        super().run()
        source = Path(__file__).resolve().parent
        destination = Path(self.build_lib).resolve() / "netizen_cli" / "resources"
        if destination == source or destination.is_relative_to(source / "skills"):
            raise ValueError("package resource output overlaps source resources")
        # This is generated build output, never an instance root or user data.
        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True)
        for name in ("netizen-lark", "netizen-user-guide"):
            skill = source / "skills" / name
            if not (skill / "SKILL.md").is_file():
                raise ValueError(f"missing built-in Skill: {skill}")
            for item in (skill, *skill.rglob("*")):
                if item.is_symlink() or not (item.is_dir() or item.is_file()):
                    raise ValueError(f"unsupported built-in Skill resource: {item}")
            shutil.copytree(skill, destination / "skills" / name)
        shutil.copyfile(source / "config.example.yaml", destination / "config.example.yaml")
