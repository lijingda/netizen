"""Existing service-manager contract and shared stop/readiness proofs."""

from __future__ import annotations

import fcntl
import os
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .installer_support import (
    FileSnapshot,
    InstallError,
    Layout,
    Release,
    _clean_subprocess_environment,
    _path_exists,
)


READY_MARKER_CONTENT = b"netizen service ready\n"
SERVICE_READY_TIMEOUT_SECONDS = 45.0
SERVICE_STOP_TIMEOUT_SECONDS = 90.0


@dataclass(frozen=True, slots=True)
class LegacyServiceState:
    present: bool = False
    recognized: bool = False
    active: bool = False
    enabled: bool = False


@dataclass(frozen=True, slots=True)
class ServiceState:
    loaded: bool
    enabled: bool


class ServiceBackend(Protocol):
    """Transition-level boundary around one per-user service manager."""

    layout: Layout

    def preflight(self) -> None: ...

    def prepare_host(self, *, interactive: bool) -> None: ...

    def inspect_state(self) -> ServiceState: ...

    def capture_definition(self) -> FileSnapshot: ...

    def render_definition(self, release: Release) -> bytes: ...

    def stop_and_confirm(
        self,
        *,
        timeout: float = SERVICE_STOP_TIMEOUT_SECONDS,
    ) -> None: ...

    def publish_definition(self, content: bytes, *, should_enable: bool) -> None: ...

    def restore_definition(
        self,
        snapshot: FileSnapshot,
        *,
        should_enable: bool,
    ) -> None: ...

    def start_and_wait(self, *, timeout: float) -> None: ...

    def service_action(self, action: str) -> int: ...

    def uninstall_definition(self) -> None: ...

    def inspect_legacy(self) -> LegacyServiceState: ...

    def disable_legacy(
        self,
        state: LegacyServiceState,
        *,
        interactive: bool,
    ) -> None: ...

    def restore_legacy(
        self,
        state: LegacyServiceState,
        *,
        interactive: bool,
    ) -> None: ...


def _service_environment(layout: Layout) -> dict[str, str]:
    environment = _clean_subprocess_environment()
    environment["HOME"] = str(layout.home)
    environment["CODEX_HOME"] = str(layout.codex_home)
    environment["NETIZEN_CONFIG_PATH"] = str(layout.config_file)
    environment["FEISHU_APP_SECRET_FILE"] = str(layout.secret_file)
    environment["NETIZEN_ADMIN_SECRET_FILE"] = str(layout.admin_secret_file)
    if layout.platform == "linux":
        environment.setdefault("XDG_RUNTIME_DIR", f"/run/user/{layout.uid}")
        environment.setdefault(
            "DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{layout.uid}/bus"
        )
    else:
        environment.pop("XDG_RUNTIME_DIR", None)
        environment.pop("DBUS_SESSION_BUS_ADDRESS", None)
    return environment


def _service_bootstrap_path(layout: Layout) -> str:
    """Provide only enough PATH to load the account profile and launcher."""

    entries = [str(layout.home / ".local" / "bin")]
    if layout.platform == "darwin":
        entries.extend(("/opt/homebrew/sbin", "/opt/homebrew/bin"))
    entries.extend(
        (
            "/usr/local/sbin",
            "/usr/local/bin",
            "/usr/sbin",
            "/usr/bin",
            "/sbin",
            "/bin",
        )
    )
    return os.pathsep.join(entries)


def _clear_ready_marker(layout: Layout) -> None:
    path = layout.ready_file
    if not _path_exists(path):
        return
    if path.is_dir() and not path.is_symlink():
        raise InstallError(f"service ready marker is a directory: {path}")
    try:
        path.unlink()
    except OSError as error:
        raise InstallError(f"could not clear service ready marker {path}: {error}") from error


def _ready_marker_present(layout: Layout) -> bool:
    path = layout.ready_file
    if path.is_symlink() or not path.is_file():
        return False
    try:
        metadata = path.stat()
        return (
            metadata.st_uid == layout.uid
            and stat.S_IMODE(metadata.st_mode) == 0o600
            and path.read_bytes() == READY_MARKER_CONTENT
        )
    except OSError:
        return False


def _lifetime_lock_available(layout: Layout) -> bool:
    """Probe the stable service-lifetime lock without replacing its inode."""

    path = layout.lifetime_lock_file
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise InstallError(f"could not open service lifetime lock {path}: {error}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != layout.uid:
            raise InstallError(
                f"service lifetime lock is not a current-user regular file: {path}"
            )
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return True
    except OSError as error:
        raise InstallError(f"could not inspect service lifetime lock {path}: {error}") from error
    finally:
        os.close(descriptor)


def _wait_for_stop_confirmation(
    layout: Layout,
    *,
    is_loaded: Callable[[], bool],
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_loaded() and _lifetime_lock_available(layout):
            _clear_ready_marker(layout)
            return
        time.sleep(0.25)
    raise InstallError(
        "service did not fully exit within "
        f"{timeout:g}s; refusing to mutate rollback-protected state"
    )
