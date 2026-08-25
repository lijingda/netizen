#!/usr/bin/env python3
"""Build the exact, deterministic Netizen Published Release assets.

The source archive is intentionally built from the same explicit managed-source
shape used by the installer.  It is not GitHub's generated source archive and
does not depend on Git metadata, timestamps, owners, or the caller's umask.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import stat
import sys
import tarfile
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RELEASE_MANIFEST = ".netizen-release.json"
BOOTSTRAP_TEMPLATE = "deploy/install-release.sh.in"
QUALIFICATION = "github-release"
OFFICIAL_REPOSITORY = "lijingda/netizen"

# Keep this set in lockstep with scripts/netizen_installer.py.  A focused test
# compares the two declarations so a source-boundary change fails closed.
MANAGED_DIRECTORIES = ("netizen", "scripts", "skills", "deploy", "docs", "tests")
MANAGED_FILES = (
    ".github/workflows/ci.yml",
    ".github/workflows/release.yml",
    ".gitignore",
    "AGENTS.md",
    "CONTEXT.md",
    "LOCAL_ENVIRONMENT.example.md",
    "Makefile",
    "README.md",
    "config.example.yaml",
    "dev-install.sh",
    "install.sh",
    "pyproject.toml",
    "requirements.lock",
    "service.sh",
    "uninstall.sh",
)
IGNORED_SOURCE_NAMES = {
    ".DS_Store",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
}

VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")
REPOSITORY_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?$"
)
PLACEHOLDER_PATTERN = re.compile(r"@@[A-Z_]+@@")


class ReleaseBuildError(RuntimeError):
    """The requested Published Release asset cannot be built safely."""


@dataclass(frozen=True, slots=True)
class SourceFile:
    relative: str
    content: bytes
    executable: bool


@dataclass(frozen=True, slots=True)
class ReleaseArtifacts:
    archive: Path
    bootstrap: Path
    archive_sha256: str
    manifest: Mapping[str, object]


def _require_regular_source_file(path: Path, root: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ReleaseBuildError(f"required release file is unavailable: {path}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ReleaseBuildError(
            f"release source must contain only regular non-symlink files: {path}"
        )
    if not path.resolve().is_relative_to(root):
        raise ReleaseBuildError(f"release file escapes the source root: {path}")


def collect_source_files(source_root: Path) -> tuple[SourceFile, ...]:
    """Read one immutable in-memory snapshot of the managed source set."""

    try:
        root = source_root.resolve(strict=True)
    except OSError as error:
        raise ReleaseBuildError(f"release source is unavailable: {source_root}") from error
    if not root.is_dir():
        raise ReleaseBuildError(f"release source is not a directory: {root}")

    paths: list[Path] = []
    for name in MANAGED_FILES:
        path = root / name
        _require_regular_source_file(path, root)
        paths.append(path)
    for directory_name in MANAGED_DIRECTORIES:
        directory = root / directory_name
        if directory.is_symlink() or not directory.is_dir():
            raise ReleaseBuildError(f"required release directory is invalid: {directory}")
        for path in sorted(directory.rglob("*")):
            relative_parts = path.relative_to(root).parts
            if any(part in IGNORED_SOURCE_NAMES for part in relative_parts):
                continue
            if path.suffix in {".pyc", ".pyo"}:
                continue
            if path.is_symlink():
                raise ReleaseBuildError(f"release source contains a symlink: {path}")
            if path.is_dir():
                continue
            _require_regular_source_file(path, root)
            paths.append(path)

    records: list[SourceFile] = []
    seen: set[str] = set()
    for path in paths:
        relative = path.relative_to(root).as_posix()
        if relative in seen:
            raise ReleaseBuildError(f"duplicate managed release file: {relative}")
        seen.add(relative)
        metadata = path.lstat()
        try:
            content = path.read_bytes()
        except OSError as error:
            raise ReleaseBuildError(f"could not read release file: {path}") from error
        records.append(
            SourceFile(
                relative=relative,
                content=content,
                executable=bool(metadata.st_mode & stat.S_IXUSR),
            )
        )
    if not records:
        raise ReleaseBuildError(f"release source is empty: {root}")
    return tuple(sorted(records, key=lambda record: record.relative))


def source_digest(files: Sequence[SourceFile]) -> str:
    """Return the installer's path/mode/content digest for a source snapshot."""

    digest = hashlib.sha256()
    for source_file in sorted(files, key=lambda record: record.relative):
        digest.update(source_file.relative.encode())
        digest.update(b"\0")
        digest.update(b"x" if source_file.executable else b"-")
        digest.update(hashlib.sha256(source_file.content).digest())
    return digest.hexdigest()


def _project_version(files: Sequence[SourceFile]) -> str:
    by_name = {source_file.relative: source_file for source_file in files}
    try:
        project = tomllib.loads(by_name["pyproject.toml"].content.decode("utf-8"))[
            "project"
        ]
        version = project["version"]
    except (KeyError, TypeError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ReleaseBuildError("pyproject.toml has no valid project.version") from error
    if not isinstance(version, str) or VERSION_PATTERN.fullmatch(version) is None:
        raise ReleaseBuildError(
            "Published Release versions must use the numeric X.Y.Z form"
        )
    return version


def _manifest_bytes(manifest: Mapping[str, object]) -> bytes:
    return (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _tar_info(name: str, *, directory: bool, executable: bool = False) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name + ("/" if directory and not name.endswith("/") else ""))
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = 0
    info.mode = 0o755 if directory or executable else 0o644
    if directory:
        info.type = tarfile.DIRTYPE
    return info


def _write_deterministic_archive(
    path: Path,
    *,
    archive_root: str,
    files: Sequence[SourceFile],
    manifest_content: bytes,
) -> None:
    directories = {archive_root}
    for directory_name in MANAGED_DIRECTORIES:
        directories.add(f"{archive_root}/{directory_name}")
    for source_file in files:
        parent = Path(source_file.relative).parent
        while parent != Path("."):
            directories.add(f"{archive_root}/{parent.as_posix()}")
            parent = parent.parent

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o644)
        raw_output = os.fdopen(descriptor, "wb")
        descriptor = -1
        with raw_output:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                fileobj=raw_output,
                compresslevel=9,
                mtime=0,
            ) as compressed:
                with tarfile.open(
                    fileobj=compressed,
                    mode="w",
                    format=tarfile.GNU_FORMAT,
                ) as archive:
                    for directory in sorted(directories):
                        archive.addfile(_tar_info(directory, directory=True))

                    manifest_name = f"{archive_root}/{RELEASE_MANIFEST}"
                    manifest_info = _tar_info(manifest_name, directory=False)
                    manifest_info.size = len(manifest_content)
                    archive.addfile(manifest_info, io.BytesIO(manifest_content))

                    for source_file in files:
                        info = _tar_info(
                            f"{archive_root}/{source_file.relative}",
                            directory=False,
                            executable=source_file.executable,
                        )
                        info.size = len(source_file.content)
                        archive.addfile(info, io.BytesIO(source_file.content))
            raw_output.flush()
            os.fsync(raw_output.fileno())
        os.replace(temporary, path)
    except BaseException:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def render_bootstrap(
    template: str,
    *,
    repository: str,
    tag: str,
    archive_name: str,
    archive_sha256: str,
    version: str,
) -> str:
    replacements = {
        "@@REPOSITORY@@": repository,
        "@@TAG@@": tag,
        "@@ARCHIVE_NAME@@": archive_name,
        "@@ARCHIVE_SHA256@@": archive_sha256,
        "@@VERSION@@": version,
    }
    rendered = template
    for placeholder, value in replacements.items():
        if rendered.count(placeholder) != 1:
            raise ReleaseBuildError(
                f"bootstrap template must contain {placeholder} exactly once"
            )
        rendered = rendered.replace(placeholder, value)
    unresolved = PLACEHOLDER_PATTERN.findall(rendered)
    if unresolved:
        raise ReleaseBuildError(
            "bootstrap template has unresolved placeholders: " + ", ".join(unresolved)
        )
    if not rendered.startswith("#!/bin/sh\n"):
        raise ReleaseBuildError("bootstrap template must be a POSIX shell script")
    return rendered


def build_release_artifacts(
    source_root: Path,
    output_dir: Path,
    *,
    tag: str,
    commit: str,
    repository: str,
) -> ReleaseArtifacts:
    files = collect_source_files(source_root)
    version = _project_version(files)
    if tag != f"v{version}":
        raise ReleaseBuildError(
            f"release tag {tag!r} does not match project version v{version}"
        )
    if COMMIT_PATTERN.fullmatch(commit) is None:
        raise ReleaseBuildError("release commit must be one full 40-character Git SHA")
    if REPOSITORY_PATTERN.fullmatch(repository) is None:
        raise ReleaseBuildError("release repository must use the OWNER/REPOSITORY form")
    if repository != OFFICIAL_REPOSITORY:
        raise ReleaseBuildError(
            f"Published Release assets must target {OFFICIAL_REPOSITORY}"
        )
    commit = commit.lower()

    by_name = {source_file.relative: source_file for source_file in files}
    requirements_digest = hashlib.sha256(by_name["requirements.lock"].content).hexdigest()
    manifest: dict[str, object] = {
        "commit": commit,
        "qualification": QUALIFICATION,
        "requirementsDigest": requirements_digest,
        "schema": 1,
        "sourceDigest": source_digest(files),
        "version": version,
    }

    output_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    archive_name = f"netizen-{tag}.tar.gz"
    archive_path = output_dir / archive_name
    bootstrap_path = output_dir / "install.sh"
    for target in (archive_path, bootstrap_path):
        if target.exists() or target.is_symlink():
            raise ReleaseBuildError(f"refusing to overwrite release asset: {target}")

    _write_deterministic_archive(
        archive_path,
        archive_root=f"netizen-{tag}",
        files=files,
        manifest_content=_manifest_bytes(manifest),
    )
    archive_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    try:
        template = by_name[BOOTSTRAP_TEMPLATE].content.decode("utf-8")
    except (KeyError, UnicodeDecodeError) as error:
        archive_path.unlink(missing_ok=True)
        raise ReleaseBuildError("release bootstrap template is unavailable or invalid") from error
    try:
        bootstrap = render_bootstrap(
            template,
            repository=repository,
            tag=tag,
            archive_name=archive_name,
            archive_sha256=archive_sha256,
            version=version,
        )
        descriptor = os.open(
            bootstrap_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o755,
        )
        try:
            os.fchmod(descriptor, 0o755)
            output = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
            descriptor = -1
            with output:
                output.write(bootstrap)
                output.flush()
                os.fsync(output.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    except BaseException:
        archive_path.unlink(missing_ok=True)
        bootstrap_path.unlink(missing_ok=True)
        raise

    return ReleaseArtifacts(
        archive=archive_path,
        bootstrap=bootstrap_path,
        archive_sha256=archive_sha256,
        manifest=manifest,
    )


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build deterministic Netizen GitHub Release assets."
    )
    parser.add_argument("--source-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--tag", required=True, help="Exact release tag, for example v0.3.0")
    parser.add_argument("--commit", required=True, help="Full 40-character release commit")
    parser.add_argument("--repository", required=True, help="Official GitHub OWNER/REPOSITORY")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        artifacts = build_release_artifacts(
            arguments.source_root,
            arguments.output_dir,
            tag=arguments.tag,
            commit=arguments.commit,
            repository=arguments.repository,
        )
    except (OSError, ReleaseBuildError) as error:
        print(f"release build failed: {error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "archive": str(artifacts.archive),
                "archiveSha256": artifacts.archive_sha256,
                "bootstrap": str(artifacts.bootstrap),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
