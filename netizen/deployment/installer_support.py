"""Shared installation values and filesystem/command operations.

Only the standard library is available while a candidate environment is being
prepared. This module owns no installation or activation transaction.
"""

from __future__ import annotations

import contextlib
import os
import stat
import subprocess
import tempfile
from collections.abc import (
    Callable,
    Mapping,
    Sequence,
)
from dataclasses import dataclass
from pathlib import Path

from .update_protocol import (
    ENV_ARCHIVE_SHA256,
    ENV_LOCK_FD,
    ENV_OPERATION_ID,
    ENV_VERSION,
)


class InstallError(RuntimeError):
    """The requested lifecycle operation cannot be completed safely."""


@dataclass(frozen=True, slots=True)
class Layout:
    platform: str
    uid: int
    username: str
    home: Path
    config_home: Path
    codex_home: Path
    product_root: Path
    releases: Path
    current: Path
    previous: Path
    config_file: Path
    credentials_dir: Path
    secret_file: Path
    admin_secret_file: Path
    state_dir: Path
    cache_dir: Path
    service_dir: Path
    service_file: Path
    ready_file: Path
    lifetime_lock_file: Path
    log_file: Path
    service_error_log: Path


@dataclass(frozen=True, slots=True)
class Release:
    digest: str
    root: Path
    source: Path
    venv: Path


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    existed: bool
    content: bytes = b""
    mode: int = 0o600


Runner = Callable[..., subprocess.CompletedProcess[str]]


def info(message: str) -> None:
    print(f"[netizen] {message}", flush=True)


def run_command(
    argv: Sequence[str | os.PathLike[str]],
    *,
    check: bool = True,
    capture_output: bool = False,
    capture_stdout: bool = False,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    rendered = [os.fspath(value) for value in argv]
    if capture_output and capture_stdout:
        raise InstallError("capture_output and capture_stdout are mutually exclusive")
    try:
        return subprocess.run(
            rendered,
            check=check,
            capture_output=capture_output,
            stdout=subprocess.PIPE if capture_stdout else None,
            cwd=cwd,
            env=None if env is None else dict(env),
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as error:
        raise InstallError(f"required command was not found: {rendered[0]}") from error
    except subprocess.CalledProcessError as error:
        command = " ".join(rendered)
        detail = (error.stderr or error.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise InstallError(f"command failed ({error.returncode}): {command}{suffix}") from error
    except subprocess.TimeoutExpired as error:
        duration = f"{error.timeout:g}" if error.timeout is not None else "configured"
        raise InstallError(
            f"command timed out after {duration} seconds: {rendered[0]}"
        ) from error


def _ensure_real_directory(
    path: Path,
    *,
    mode: int,
    enforce_mode: bool = True,
) -> None:
    existed = path.exists()
    if path.is_symlink():
        raise InstallError(f"managed directory must not be a symlink: {path}")
    try:
        path.mkdir(mode=mode, parents=True, exist_ok=True)
    except OSError as error:
        raise InstallError(f"could not create directory {path}: {error}") from error
    if not path.is_dir():
        raise InstallError(f"managed path is not a directory: {path}")
    if enforce_mode or not existed:
        try:
            path.chmod(mode)
        except OSError as error:
            raise InstallError(f"could not protect directory {path}: {error}") from error


def _require_regular_file(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise InstallError(f"could not inspect {label} file {path}: {error}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise InstallError(f"{label} must be a regular non-symlink file: {path}")


def _write_atomic(path: Path, content: bytes, *, mode: int) -> None:
    _ensure_real_directory(path.parent, mode=0o700, enforce_mode=False)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


def _clean_subprocess_environment() -> dict[str, str]:
    environment = dict(os.environ)
    virtual_environment = environment.get("VIRTUAL_ENV", "").strip()
    if virtual_environment:
        virtual_bin = (Path(virtual_environment) / "bin").resolve(strict=False)
        environment["PATH"] = os.pathsep.join(
            entry
            for entry in environment.get("PATH", "").split(os.pathsep)
            if entry
            and (
                not Path(entry).is_absolute()
                or Path(entry).resolve(strict=False) != virtual_bin
            )
        )
    for name in (
        "FEISHU_APP_SECRET",
        "FEISHU_APP_SECRET_FILE",
        "NETIZEN_ADMIN_SECRET",
        "NETIZEN_ADMIN_SECRET_FILE",
        "NETIZEN_CONFIG_PATH",
        "NETIZEN_LIFETIME_LOCK_FD",
        "NETIZEN_LIFETIME_LOCK_FILE",
        "NETIZEN_READY_FILE",
        "NETIZEN_LOG_FILE",
        "NETIZEN_MANAGED_LAUNCH_AGENT",
        ENV_OPERATION_ID,
        ENV_LOCK_FD,
        ENV_VERSION,
        ENV_ARCHIVE_SHA256,
        "PYTHONHOME",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "__PYVENV_LAUNCHER__",
        "XDG_DATA_HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
    ):
        environment.pop(name, None)
    return environment


def _capture_file(path: Path, *, label: str = "managed unit") -> FileSnapshot:
    if not _path_exists(path):
        return FileSnapshot(existed=False)
    _require_regular_file(path, label)
    metadata = path.stat()
    return FileSnapshot(
        existed=True,
        content=path.read_bytes(),
        mode=stat.S_IMODE(metadata.st_mode),
    )


def _restore_file(
    path: Path,
    snapshot: FileSnapshot,
    *,
    label: str = "managed unit",
) -> None:
    if snapshot.existed:
        _write_atomic(path, snapshot.content, mode=snapshot.mode)
    elif _path_exists(path):
        if path.is_symlink() or path.is_file():
            path.unlink()
        else:
            raise InstallError(f"refusing to remove unexpected {label} path: {path}")


def _path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()
