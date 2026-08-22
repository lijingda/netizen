#!/usr/bin/env python3
"""Replace the release-managed global Netizen user-guide Skill."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


SKILL_NAME = "netizen-user-guide"


class SkillInstallError(RuntimeError):
    """The managed Skill could not be installed without risking stale state."""


@dataclass(frozen=True, slots=True)
class InstalledSkill:
    source: str
    target: str
    file_count: int
    digest: str


@dataclass(frozen=True, slots=True)
class RemovedSkill:
    target: str
    removed: bool


def release_skill_path() -> Path:
    return Path(__file__).resolve().parents[1] / "skills" / SKILL_NAME


def default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured) if configured else Path.home() / ".codex"


def install_user_guide_skill(
    *,
    source_skill: Path,
    codex_home: Path,
) -> InstalledSkill:
    """Fully replace only ``$CODEX_HOME/skills/netizen-user-guide``.

    The source is copied and verified in a sibling staging directory before the
    old target is moved aside. If publishing the staged directory fails, the
    previous target is restored before the error is returned.
    """

    raw_source = source_skill.expanduser()
    if raw_source.is_symlink():
        raise SkillInstallError("release Skill root must not be a symlink")
    try:
        source = raw_source.resolve(strict=True)
    except OSError as error:
        raise SkillInstallError(f"release Skill is unavailable: {raw_source}") from error
    if source.name != SKILL_NAME:
        raise SkillInstallError(
            f"release Skill directory must be named {SKILL_NAME!r}: {source}"
        )
    try:
        source_manifest = _skill_manifest(source)
    except OSError as error:
        raise SkillInstallError(f"release Skill could not be read: {source}") from error

    raw_codex_home = codex_home.expanduser()
    try:
        raw_codex_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        resolved_codex_home = raw_codex_home.resolve(strict=True)
    except OSError as error:
        raise SkillInstallError(
            f"Codex home could not be prepared: {raw_codex_home}"
        ) from error
    if not resolved_codex_home.is_dir():
        raise SkillInstallError(f"Codex home is not a directory: {resolved_codex_home}")
    if resolved_codex_home == Path(resolved_codex_home.anchor):
        raise SkillInstallError("Codex home must not be a filesystem root")

    skills_root = resolved_codex_home / "skills"
    if skills_root.is_symlink():
        raise SkillInstallError(f"Skills directory must not be a symlink: {skills_root}")
    try:
        skills_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as error:
        raise SkillInstallError(
            f"Skills directory could not be prepared: {skills_root}"
        ) from error
    if not skills_root.is_dir():
        raise SkillInstallError(f"Skills path is not a directory: {skills_root}")

    target = skills_root / SKILL_NAME
    if _same_or_nested_path(source, target.resolve(strict=False)):
        raise SkillInstallError("release Skill source and managed target must be separate")

    staging = Path(
        tempfile.mkdtemp(prefix=f".{SKILL_NAME}.staging-", dir=skills_root)
    )
    backup_parent: Path | None = None
    backup: Path | None = None
    original_moved = False
    try:
        shutil.copytree(
            source,
            staging,
            dirs_exist_ok=True,
            copy_function=shutil.copy2,
            symlinks=True,
        )
        staged_manifest = _skill_manifest(staging)
        if staged_manifest != source_manifest:
            raise SkillInstallError("staged Skill differs from the release source")

        if _path_exists(target):
            backup_parent = Path(
                tempfile.mkdtemp(prefix=f".{SKILL_NAME}.backup-", dir=skills_root)
            )
            backup = backup_parent / SKILL_NAME
            os.replace(target, backup)
            original_moved = True

        try:
            os.replace(staging, target)
        except BaseException as publish_error:
            if original_moved and backup is not None:
                try:
                    os.replace(backup, target)
                    original_moved = False
                except BaseException as rollback_error:
                    raise SkillInstallError(
                        "publishing the new Skill failed and the previous Skill "
                        f"could not be restored; recovery copy remains at {backup}"
                    ) from rollback_error
                raise SkillInstallError(
                    "publishing the new Skill failed; the previous Skill was restored"
                ) from publish_error
            raise SkillInstallError(
                "publishing the new Skill failed; no managed Skill was installed"
            ) from publish_error

        verification_error: BaseException | None = None
        try:
            installed_manifest = _skill_manifest(target)
        except (OSError, SkillInstallError) as error:
            verification_error = error
            installed_manifest = None
        if installed_manifest != source_manifest:
            if original_moved and backup is not None:
                failed = backup.parent / f"{SKILL_NAME}.failed"
                try:
                    os.replace(target, failed)
                    os.replace(backup, target)
                    original_moved = False
                except BaseException as rollback_error:
                    raise SkillInstallError(
                        "installed Skill verification failed and the previous Skill "
                        f"could not be restored; inspect {backup.parent}"
                    ) from rollback_error
                raise SkillInstallError(
                    "installed Skill verification failed; the previous Skill was "
                    "restored"
                ) from verification_error
            try:
                _remove_path(target)
            except OSError as cleanup_error:
                raise SkillInstallError(
                    "installed Skill verification failed and the unverified Skill "
                    f"could not be removed; inspect {target}"
                ) from cleanup_error
            raise SkillInstallError(
                "installed Skill verification failed; the unverified Skill was removed"
            ) from verification_error
        original_moved = False
        return InstalledSkill(
            source=str(source),
            target=str(target),
            file_count=len(source_manifest),
            digest=_manifest_digest(source_manifest),
        )
    except SkillInstallError:
        raise
    except OSError as error:
        raise SkillInstallError(f"failed to install {SKILL_NAME}: {error}") from error
    finally:
        if _path_exists(staging):
            _remove_path(staging)
        if (
            backup_parent is not None
            and not original_moved
            and _path_exists(backup_parent)
        ):
            _remove_path(backup_parent)


def remove_user_guide_skill(*, codex_home: Path) -> RemovedSkill:
    """Remove only the release-managed Skill for a pre-feature rollback."""

    raw_codex_home = codex_home.expanduser()
    try:
        resolved_codex_home = raw_codex_home.resolve(strict=True)
    except OSError as error:
        raise SkillInstallError(f"Codex home is unavailable: {raw_codex_home}") from error
    if not resolved_codex_home.is_dir():
        raise SkillInstallError(f"Codex home is not a directory: {resolved_codex_home}")
    if resolved_codex_home == Path(resolved_codex_home.anchor):
        raise SkillInstallError("Codex home must not be a filesystem root")

    skills_root = resolved_codex_home / "skills"
    if skills_root.is_symlink():
        raise SkillInstallError(f"Skills directory must not be a symlink: {skills_root}")
    if not skills_root.exists():
        return RemovedSkill(
            target=str(skills_root / SKILL_NAME),
            removed=False,
        )
    if not skills_root.is_dir():
        raise SkillInstallError(f"Skills path is not a directory: {skills_root}")

    target = skills_root / SKILL_NAME
    removed = _path_exists(target)
    if removed:
        try:
            _remove_path(target)
        except OSError as error:
            raise SkillInstallError(f"managed Skill could not be removed: {target}") from error
    return RemovedSkill(target=str(target), removed=removed)


def _skill_manifest(root: Path) -> dict[str, str]:
    if root.is_symlink() or not root.is_dir():
        raise SkillInstallError(f"Skill root must be a real directory: {root}")
    manifest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise SkillInstallError(f"Skill source contains a symlink: {relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise SkillInstallError(f"Skill source contains a special file: {relative}")
        manifest[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    if "SKILL.md" not in manifest:
        raise SkillInstallError("Skill source is missing SKILL.md")
    return manifest


def _manifest_digest(manifest: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for relative, file_digest in sorted(manifest.items()):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(file_digest))
    return digest.hexdigest()


def _same_or_nested_path(first: Path, second: Path) -> bool:
    return (
        first == second
        or first.is_relative_to(second)
        or second.is_relative_to(first)
    )


def _path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fully replace $CODEX_HOME/skills/netizen-user-guide with the "
            "Skill shipped by this Netizen release."
        )
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=default_codex_home(),
        help="Codex home to update (default: CODEX_HOME or ~/.codex)",
    )
    parser.add_argument(
        "--remove",
        action="store_true",
        help=(
            "remove only the managed Skill instead of installing it; use only "
            "when rolling back to a release from before the Skill existed"
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.remove:
        result = remove_user_guide_skill(codex_home=args.codex_home)
    else:
        result = install_user_guide_skill(
            source_skill=release_skill_path(),
            codex_home=args.codex_home,
        )
    print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
