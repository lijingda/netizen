"""Locate and validate release-local Skills without touching user Codex state."""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
import yaml


BUILTIN_SKILL_NAMES = ("netizen-user-guide", "netizen-lark")


class BuiltinSkillError(RuntimeError):
    """The physical running source cannot provide its complete built-in Skills."""


def builtin_skill_root(
    *,
    runtime_prefix: Path | None = None,
    package_file: Path | None = None,
) -> Path:
    """Resolve from the imported package, never cwd or the mutable current link."""
    prefix = (runtime_prefix or Path(sys.prefix)).resolve()
    module = (package_file or Path(__file__)).resolve()
    if module.is_relative_to(prefix):
        if prefix.name != "venv":
            raise BuiltinSkillError("installed Netizen is outside a managed release venv")
        source = prefix.parent / "source"
    else:
        # An explicit source-tree import is the only supported development form.
        source = module.parent.parent
        if module.parent.name != "netizen" or not (source / "pyproject.toml").is_file():
            raise BuiltinSkillError("cannot identify the running Netizen source tree")
    return validate_builtin_skills(source)


def validate_builtin_skills(source_root: Path) -> Path:
    """Require both self-contained Skill trees, including referenced resources."""
    raw_root = source_root.resolve() / "skills"
    if raw_root.is_symlink() or not raw_root.is_dir():
        raise BuiltinSkillError(f"release Skills directory is unavailable: {raw_root}")
    for name in BUILTIN_SKILL_NAMES:
        skill = raw_root / name
        if skill.is_symlink() or not skill.is_dir():
            raise BuiltinSkillError(f"release Skill is unavailable: {skill}")
        entry = skill / "SKILL.md"
        if entry.is_symlink():
            raise BuiltinSkillError(f"release Skill contains a symlink: {entry}")
        try:
            text = entry.read_text(encoding="utf-8")
            sections = text.split("---", 2)
            metadata = yaml.safe_load(sections[1]) if text.startswith("---\n") else None
        except (OSError, IndexError, UnicodeError, yaml.YAMLError) as error:
            raise BuiltinSkillError(f"invalid release Skill: {entry}") from error
        if (
            not isinstance(metadata, dict)
            or metadata.get("name") != name
            or not isinstance(metadata.get("description"), str)
            or not metadata["description"].strip()
        ):
            raise BuiltinSkillError(f"invalid release Skill metadata: {entry}")
        for path in skill.rglob("*"):
            if path.is_symlink():
                raise BuiltinSkillError(f"release Skill contains a symlink: {path}")
            if path.is_file():
                try:
                    if not path.read_bytes():
                        raise BuiltinSkillError(f"release Skill contains an empty file: {path}")
                    if path.suffix.lower() == ".md":
                        _validate_resources(skill, path)
                except (OSError, UnicodeError) as error:
                    raise BuiltinSkillError(f"release Skill resource is unreadable: {path}") from error
    return raw_root


def _validate_resources(skill: Path, document: Path) -> None:
    for block in MarkdownIt().parse(document.read_text(encoding="utf-8")):
        for token in block.children or ():
            target = token.attrGet("href") or token.attrGet("src")
            if not target:
                continue
            url = urlsplit(target)
            if url.scheme or url.netloc or not url.path:
                continue
            resource = (document.parent / unquote(url.path)).resolve()
            if not resource.is_relative_to(skill) or not resource.exists():
                raise BuiltinSkillError(
                    f"release Skill reference is missing or outside its tree: {document} -> {target}"
                )
