#!/usr/bin/env python3
"""Build/verify ordinary Python distributions; never publish or install Netizen.

The release workflow supplies an already main-qualified exact tag. The wheel is
built from the sdist using the project's pinned public PEP 517 backend hooks.
Checksums bind subsequent jobs to these bytes, not to another rebuild.
"""
from __future__ import annotations

import argparse
import ast
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "netizen-cli-release.json"
REQUIRED_WHEEL_FILES = frozenset({
    "netizen_cli/__init__.py", "netizen_cli/__main__.py", "netizen_cli/cli.py",
    "netizen_cli/service_launcher.py", "netizen_cli/cli_update_worker.py",
    "netizen_cli/resources/config.example.yaml",
    "netizen_cli/resources/skills/netizen-lark/SKILL.md",
    "netizen_cli/resources/skills/netizen-user-guide/SKILL.md",
    "netizen_cli/admin/static/index.html",
})


class DistributionError(RuntimeError):
    pass


def release_version(tag: str) -> str:
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise DistributionError("release tag must have the form vX.Y.Z")
    return tag[1:]


def validate_source(source: Path, tag: str, commit: str) -> str:
    version = release_version(tag)
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise DistributionError("release commit must be an exact 40-character Git SHA")
    project = tomllib.loads((source / "pyproject.toml").read_text())
    if project["project"].get("name") != "netizen-cli" or project["project"].get("version") != version:
        raise DistributionError("project distribution/version does not match the release tag")
    if project["build-system"] != {"requires": ["setuptools==80.9.0"], "build-backend": "setuptools.build_meta"}:
        raise DistributionError("release builder only supports the declared pinned setuptools backend")
    if project["project"].get("scripts", {}).get("netizen") != "netizen_cli.cli:main":
        raise DistributionError("project must publish the netizen_cli CLI entry point")
    package = ast.parse((source / "netizen_cli/__init__.py").read_text())
    values = [ast.literal_eval(node.value) for node in package.body if isinstance(node, ast.Assign)
              and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)]
    if values != [version]:
        raise DistributionError("package version does not match the release tag")
    return version


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_member(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if not path.parts or path.is_absolute() or ".." in path.parts or "\\" in name:
        raise DistributionError(f"unsafe distribution member: {name}")
    return path


def _metadata(body: bytes, version: str) -> None:
    metadata = BytesParser().parsebytes(body)
    if metadata.get("Name") != "netizen-cli" or metadata.get("Version") != version:
        raise DistributionError("distribution metadata name/version differs from the release tag")


def verify_wheel(path: Path, version: str) -> None:
    with zipfile.ZipFile(path) as wheel:
        names = wheel.namelist()
        if len(names) != len(set(names)):
            raise DistributionError("wheel contains duplicate members")
        for name in names:
            _safe_member(name)
        if not REQUIRED_WHEEL_FILES.issubset(names):
            raise DistributionError("wheel is missing CLI modules or required runtime resources")
        if any(name.startswith(("netizen/", "scripts/")) for name in names):
            raise DistributionError("wheel contains a legacy package or source installer")
        metadata = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata) != 1:
            raise DistributionError("wheel must contain one distribution metadata record")
        _metadata(wheel.read(metadata[0]), version)
        entry_file = metadata[0].removesuffix("METADATA") + "entry_points.txt"
        import configparser
        entries = configparser.ConfigParser()
        entries.read_string(wheel.read(entry_file).decode())
        if entries.get("console_scripts", "netizen", fallback=None) != "netizen_cli.cli:main":
            raise DistributionError("wheel CLI entry point is invalid")


def verify_sdist(path: Path, version: str, *, extract_to: Path | None = None) -> Path | None:
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        if len(names) != len(set(names)):
            raise DistributionError("sdist contains duplicate members")
        roots: set[str] = set()
        for member in members:
            relative = _safe_member(member.name)
            roots.add(relative.parts[0])
            if not member.isfile() and not member.isdir():
                raise DistributionError("sdist may only contain ordinary files and directories")
        if len(roots) != 1:
            raise DistributionError("sdist must contain one source root")
        root = next(iter(roots))
        required = {f"{root}/{name}" for name in (
            "PKG-INFO", "pyproject.toml", "_build.py", "config.example.yaml",
            "netizen_cli/cli.py", "skills/netizen-lark/SKILL.md", "skills/netizen-user-guide/SKILL.md",
        )}
        if not required.issubset(names):
            raise DistributionError("sdist is missing package or build resources")
        metadata = archive.extractfile(f"{root}/PKG-INFO")
        if metadata is None:
            raise DistributionError("sdist metadata is not a file")
        _metadata(metadata.read(), version)
        if extract_to is not None:
            for member in members:
                destination = extract_to.joinpath(*PurePosixPath(member.name).parts)
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    content = archive.extractfile(member)
                    if content is None:
                        raise DistributionError("sdist member has no file content")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(content.read())
            return extract_to / root
    return None


def _backend(source: Path, output: Path, operation: str, python: str) -> None:
    code = (
        "import importlib.metadata as m; "
        "assert m.version('setuptools') == '80.9.0', 'install setuptools==80.9.0 in the builder'; "
        "from setuptools import build_meta; "
        f"build_meta.{operation}({str(output)!r})"
    )
    completed = subprocess.run([python, "-E", "-c", code], cwd=source, text=True,
                               capture_output=True, check=False, timeout=180)
    if completed.returncode:
        raise DistributionError(f"{operation} failed:\n{completed.stdout}\n{completed.stderr}")


def build_distribution(source: Path, output: Path, *, tag: str, commit: str,
                       python: str = sys.executable) -> dict:
    source, output = source.resolve(), output.resolve()
    version = validate_source(source, tag, commit)
    if output.exists() and any(output.iterdir()):
        raise DistributionError("release output directory must be empty; existing artifacts are never overwritten")
    output.mkdir(parents=True, exist_ok=True)
    _backend(source, output, "build_sdist", python)
    archives = list(output.glob("*.tar.gz"))
    if len(archives) != 1:
        raise DistributionError("builder did not produce exactly one sdist")
    with tempfile.TemporaryDirectory(prefix="netizen-release-build-") as temporary:
        extracted = verify_sdist(archives[0], version, extract_to=Path(temporary))
        assert extracted is not None
        _backend(extracted, output, "build_wheel", python)
    wheels = list(output.glob("*.whl"))
    if len(wheels) != 1:
        raise DistributionError("builder did not produce exactly one wheel")
    verify_wheel(wheels[0], version)
    manifest = {
        "schema": 1, "distribution": "netizen-cli", "version": version, "tag": tag,
        "commit": commit, "files": {path.name: _digest(path) for path in (wheels[0], archives[0])},
    }
    (output / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def verify_distribution(directory: Path, *, tag: str, commit: str,
                        manifest_sha256: str | None = None) -> dict:
    path = directory / MANIFEST
    if manifest_sha256 is not None and _digest(path) != manifest_sha256:
        raise DistributionError("release manifest SHA-256 differs from the built candidate")
    manifest = json.loads(path.read_text())
    if (not isinstance(manifest, dict) or manifest.get("schema") != 1 or manifest.get("distribution") != "netizen-cli"
            or manifest.get("tag") != tag or manifest.get("version") != release_version(tag)
            or manifest.get("commit") != commit):
        raise DistributionError("release manifest identity does not match the exact tag/commit")
    files = manifest.get("files")
    if not isinstance(files, dict) or len(files) != 2:
        raise DistributionError("release must contain exactly one wheel and one sdist")
    if {path.name for path in directory.iterdir()} != {*files, MANIFEST}:
        raise DistributionError("release directory contains unexpected or missing artifacts")
    kinds: set[str] = set()
    for name, digest in files.items():
        if Path(name).name != name or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise DistributionError("invalid artifact identity")
        artifact = directory / name
        if artifact.is_symlink() or not artifact.is_file() or _digest(artifact) != digest:
            raise DistributionError(f"artifact checksum mismatch: {name}")
        if name.endswith(".whl"):
            kinds.add("wheel")
            verify_wheel(artifact, manifest["version"])
        elif name.endswith(".tar.gz"):
            kinds.add("sdist")
            verify_sdist(artifact, manifest["version"])
        else:
            raise DistributionError(f"not a Python distribution: {name}")
    if kinds != {"wheel", "sdist"}:
        raise DistributionError("release must contain one wheel and one sdist")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--manifest-sha256")
    args = parser.parse_args(argv)
    try:
        if args.verify_only:
            manifest = verify_distribution(args.output_dir, tag=args.tag, commit=args.commit,
                                           manifest_sha256=args.manifest_sha256)
        else:
            manifest = build_distribution(args.source, args.output_dir, tag=args.tag, commit=args.commit)
            verify_distribution(args.output_dir, tag=args.tag, commit=args.commit)
        if os.environ.get("GITHUB_OUTPUT"):
            with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
                for name in manifest["files"]:
                    kind = "wheel" if name.endswith(".whl") else "sdist"
                    output.write(f"{kind}-name={name}\n")
                output.write(f"manifest-sha256={_digest(args.output_dir / MANIFEST)}\n")
        print(json.dumps(manifest, sort_keys=True))
        return 0
    except (DistributionError, OSError, ValueError, KeyError, zipfile.BadZipFile, tarfile.TarError) as error:
        print(f"CLI distribution failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
