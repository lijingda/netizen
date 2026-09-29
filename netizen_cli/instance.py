"""Canonical deployment location and names, without a second instance registry."""

from __future__ import annotations

import hashlib
import os
import pwd
import stat
from collections.abc import Mapping
from pathlib import Path


INSTANCE_ROOT_MARKER = ".netizen-root"
INSTANCE_ROOT_MARKER_CONTENT = b"netizen-instance-root-v1\n"


def require_instance_root_marker(root: Path, *, uid: int | None = None) -> None:
    """Read-only ownership proof shared by installation and managed startup."""
    marker = root / INSTANCE_ROOT_MARKER
    metadata = marker.lstat()
    expected_uid = os.geteuid() if uid is None else uid
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != expected_uid
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise ValueError(f"instance root marker is not recognized: {marker}")
    descriptor = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        opened = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != expected_uid
            or stat.S_IMODE(opened.st_mode) != 0o600
            or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
            or source.read(len(INSTANCE_ROOT_MARKER_CONTENT) + 1) != INSTANCE_ROOT_MARKER_CONTENT
        ):
            raise ValueError(f"instance root marker is not recognized: {marker}")


def resolve_instance_root(
    root: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    account_home: Path | None = None,
    cwd: Path | None = None,
) -> Path:
    """Resolve explicit argument > environment > effective account default once."""

    env = os.environ if environ is None else environ
    home = (
        Path(pwd.getpwuid(os.geteuid()).pw_dir)
        if account_home is None else account_home
    )
    if not home.is_absolute() or home == Path(home.anchor):
        raise ValueError("account home must be an absolute non-root path")
    selected = root if root is not None else env.get("NETIZEN_ROOT", home / ".netizen")
    raw = os.fspath(selected)
    if not raw.strip() or any(ord(character) < 32 for character in raw):
        raise ValueError("NETIZEN_ROOT must be a non-empty path without control characters")
    if raw == "~" or raw.startswith("~/"):
        path = home / raw[2:] if raw != "~" else home
    elif raw.startswith("~"):
        raise ValueError("NETIZEN_ROOT only supports the effective account's ~/ prefix")
    else:
        path = Path(raw)
    if not path.is_absolute():
        path = (Path.cwd() if cwd is None else cwd) / path
    canonical = path.resolve()
    if canonical == Path(canonical.anchor) or canonical == home.resolve():
        raise ValueError("NETIZEN_ROOT must not be a filesystem root or the account home")
    return canonical


def instance_digest(root: Path) -> str:
    return hashlib.sha256(os.fsencode(root.resolve())).hexdigest()[:24]


def systemd_service_name(root: Path) -> str:
    return f"netizen-{instance_digest(root)}.service"


def launch_agent_label(root: Path) -> str:
    return f"io.github.lijingda.netizen.{instance_digest(root)}"
