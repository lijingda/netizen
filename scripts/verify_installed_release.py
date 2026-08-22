#!/usr/bin/env python3
"""Fail closed unless the active venv contains the exact release package."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.resources
import json
import sys
from pathlib import Path


class InstalledReleaseMismatch(RuntimeError):
    pass


def verify_installed_release(
    *,
    source_root: Path,
    installed_package: Path,
    runtime_prefix: Path,
) -> int:
    source_package = (source_root / "netizen").resolve()
    installed_package = installed_package.resolve()
    runtime_prefix = runtime_prefix.resolve()
    if installed_package == source_package:
        raise InstalledReleaseMismatch(
            "netizen resolved to the source tree; run this probe outside the release cwd"
        )
    if not installed_package.is_relative_to(runtime_prefix):
        raise InstalledReleaseMismatch(
            f"installed package is outside the candidate venv: {installed_package}"
        )

    source_files = _package_file_hashes(source_package)
    installed_files = _package_file_hashes(installed_package)
    missing = sorted(source_files.keys() - installed_files.keys())
    extra = sorted(installed_files.keys() - source_files.keys())
    changed = sorted(
        path
        for path in source_files.keys() & installed_files.keys()
        if source_files[path] != installed_files[path]
    )
    if missing or extra or changed:
        raise InstalledReleaseMismatch(
            "installed package differs from release source: "
            f"missing={missing}, extra={extra}, changed={changed}"
        )
    if not source_files:
        raise InstalledReleaseMismatch("release source contains no package files")
    return len(source_files)


def _package_file_hashes(root: Path) -> dict[str, str]:
    if not root.is_dir():
        raise InstalledReleaseMismatch(f"package directory does not exist: {root}")
    hashes: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise InstalledReleaseMismatch(f"package contains a symlink: {relative}")
        if not path.is_file():
            continue
        hashes[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def _verify_admin_assets(package: object) -> tuple[str, ...]:
    root = importlib.resources.files(package).joinpath("admin").joinpath("static")
    required = ("index.html", "admin.css", "admin.js")
    for name in required:
        asset = root.joinpath(name)
        try:
            content = asset.read_bytes()
        except (FileNotFoundError, OSError) as error:
            raise InstalledReleaseMismatch(
                f"installed Admin Web asset is unavailable: {name}"
            ) from error
        if not content:
            raise InstalledReleaseMismatch(
                f"installed Admin Web asset is empty: {name}"
            )
    return required


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    args = parser.parse_args()
    package = importlib.import_module("netizen")
    assets = _verify_admin_assets(package)
    package_file = getattr(package, "__file__", None)
    if not package_file:
        raise InstalledReleaseMismatch("imported netizen has no filesystem package")
    installed_package = Path(package_file).resolve().parent
    count = verify_installed_release(
        source_root=args.source_root,
        installed_package=installed_package,
        runtime_prefix=Path(sys.prefix),
    )
    print(
        json.dumps(
            {
                "installed_package": str(installed_package),
                "matched_package_files": count,
                "admin_assets": assets,
            }
        )
    )


if __name__ == "__main__":
    main()
