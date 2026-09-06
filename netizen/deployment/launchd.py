"""Existing macOS current-user LaunchAgent backend."""

from __future__ import annotations

import os
import plistlib
import stat
import subprocess
import time
from pathlib import Path

from .installer_support import (
    FileSnapshot,
    InstallError,
    Layout,
    Release,
    Runner,
    info,
    _capture_file,
    _path_exists,
    _restore_file,
    _write_atomic,
)
from .service_backend import (
    LegacyServiceState,
    ServiceState,
    SERVICE_READY_TIMEOUT_SECONDS,
    SERVICE_STOP_TIMEOUT_SECONDS,
    _clear_ready_marker,
    _ready_marker_present,
    _service_bootstrap_path,
    _service_environment,
    _wait_for_stop_confirmation,
)


LAUNCH_AGENT_LABEL = "io.github.lijingda.netizen"
LAUNCH_AGENT_SENTINEL_NAME = "NETIZEN_MANAGED_LAUNCH_AGENT"
LAUNCH_AGENT_SENTINEL_VALUE = "io.github.lijingda.netizen/v1"


def _log_excerpt(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return " | ".join(lines[-5:])


def _launchctl(
    layout: Layout,
    *arguments: str | os.PathLike[str],
    runner: Runner,
    check: bool = True,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    return runner(
        ["launchctl", *arguments],
        check=check,
        capture_output=capture_output,
        env=_service_environment(layout),
    )


def _launch_agent_program_arguments(layout: Layout) -> list[str]:
    return [
        str(layout.current / "venv" / "bin" / "python"),
        "-E",
        "-B",
        "-u",
        str(
            layout.current
            / "source"
            / "scripts"
            / "netizen_service_launcher.py"
        ),
    ]


def render_launch_agent(release: Release, layout: Layout) -> bytes:
    del release  # The stable current pointer is the LaunchAgent activation boundary.
    payload = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": _launch_agent_program_arguments(layout),
        "WorkingDirectory": str(layout.home),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ExitTimeOut": 75,
        "ThrottleInterval": 3,
        "Umask": 0o077,
        "EnvironmentVariables": {
            "HOME": str(layout.home),
            "CODEX_HOME": str(layout.codex_home),
            "PATH": _service_bootstrap_path(layout),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "NETIZEN_CONFIG_PATH": str(layout.config_file),
            "FEISHU_APP_SECRET_FILE": str(layout.secret_file),
            "NETIZEN_ADMIN_SECRET_FILE": str(layout.admin_secret_file),
            "NETIZEN_READY_FILE": str(layout.ready_file),
            "NETIZEN_LIFETIME_LOCK_FILE": str(layout.lifetime_lock_file),
            "NETIZEN_LOG_FILE": str(layout.log_file),
            LAUNCH_AGENT_SENTINEL_NAME: LAUNCH_AGENT_SENTINEL_VALUE,
        },
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": str(layout.service_error_log),
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


def _require_managed_launch_agent(path: Path, layout: Layout) -> None:
    if path.is_symlink():
        raise InstallError(f"managed LaunchAgent must not be a symlink: {path}")
    try:
        metadata = path.stat()
    except OSError as error:
        raise InstallError(f"could not inspect managed LaunchAgent {path}: {error}") from error
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != layout.uid:
        raise InstallError(
            f"managed LaunchAgent is not a current-user regular file: {path}"
        )
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise InstallError(f"managed LaunchAgent is group/world writable: {path}")
    try:
        payload = plistlib.loads(path.read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError, TypeError) as error:
        raise InstallError(f"managed LaunchAgent is unreadable: {path}: {error}") from error
    environment = payload.get("EnvironmentVariables") if isinstance(payload, dict) else None
    if (
        not isinstance(environment, dict)
        or payload.get("Label") != LAUNCH_AGENT_LABEL
        or payload.get("ProgramArguments") != _launch_agent_program_arguments(layout)
        or environment.get(LAUNCH_AGENT_SENTINEL_NAME)
        != LAUNCH_AGENT_SENTINEL_VALUE
    ):
        raise InstallError(f"refusing to operate on an unrecognized LaunchAgent: {path}")


class LaunchAgentServiceBackend:
    def __init__(self, layout: Layout, runner: Runner) -> None:
        self.layout = layout
        self._runner = runner
        self._domain = f"gui/{layout.uid}"
        self._target = f"{self._domain}/{LAUNCH_AGENT_LABEL}"

    def preflight(self) -> None:
        domain = _launchctl(
            self.layout,
            "print",
            self._domain,
            runner=self._runner,
            check=False,
            capture_output=True,
        )
        if domain.returncode != 0:
            raise InstallError(
                "the current macOS GUI launchd domain is unavailable; log in to a "
                "graphical user session before installing or controlling Netizen"
            )

    def prepare_host(self, *, interactive: bool) -> None:
        del interactive

    def _is_loaded(self) -> bool:
        result = _launchctl(
            self.layout,
            "print",
            self._target,
            runner=self._runner,
            check=False,
            capture_output=True,
        )
        return result.returncode == 0

    def inspect_state(self) -> ServiceState:
        return ServiceState(
            loaded=self._is_loaded(),
            enabled=_path_exists(self.layout.service_file),
        )

    def capture_definition(self) -> FileSnapshot:
        if _path_exists(self.layout.service_file):
            _require_managed_launch_agent(self.layout.service_file, self.layout)
        return _capture_file(self.layout.service_file, label="managed LaunchAgent")

    def render_definition(self, release: Release) -> bytes:
        return render_launch_agent(release, self.layout)

    def stop_and_confirm(
        self,
        *,
        timeout: float = SERVICE_STOP_TIMEOUT_SECONDS,
    ) -> None:
        if self._is_loaded():
            try:
                _launchctl(
                    self.layout,
                    "bootout",
                    self._target,
                    runner=self._runner,
                )
            except InstallError:
                if self._is_loaded():
                    raise
        _wait_for_stop_confirmation(
            self.layout,
            is_loaded=self._is_loaded,
            timeout=timeout,
        )

    def _set_enabled(self, enabled: bool) -> None:
        _launchctl(
            self.layout,
            "enable" if enabled else "disable",
            self._target,
            runner=self._runner,
            check=enabled,
        )

    def _validate_definition(self) -> None:
        _require_managed_launch_agent(self.layout.service_file, self.layout)
        self._runner(
            ["plutil", "-lint", self.layout.service_file],
            env=_service_environment(self.layout),
        )

    def publish_definition(self, content: bytes, *, should_enable: bool) -> None:
        _write_atomic(self.layout.service_file, content, mode=0o600)
        self._validate_definition()
        self._set_enabled(should_enable)

    def restore_definition(
        self,
        snapshot: FileSnapshot,
        *,
        should_enable: bool,
    ) -> None:
        _restore_file(
            self.layout.service_file,
            snapshot,
            label="managed LaunchAgent",
        )
        if snapshot.existed:
            self._validate_definition()
        self._set_enabled(should_enable)

    def _wait_for_ready(self, *, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._is_loaded():
                break
            if _ready_marker_present(self.layout):
                return
            time.sleep(0.25)
        excerpt = _log_excerpt(self.layout.service_error_log)
        suffix = f"; recent launchd stderr: {excerpt}" if excerpt else ""
        raise InstallError(
            f"{LAUNCH_AGENT_LABEL} did not become ready within {timeout:g}s{suffix}"
        )

    def start_and_wait(self, *, timeout: float) -> None:
        loaded = self._is_loaded()
        if loaded and _ready_marker_present(self.layout):
            return
        if not loaded:
            _clear_ready_marker(self.layout)
            self._set_enabled(True)
            try:
                _launchctl(
                    self.layout,
                    "bootstrap",
                    self._domain,
                    self.layout.service_file,
                    runner=self._runner,
                )
            except InstallError:
                if not self._is_loaded():
                    raise
        self._wait_for_ready(timeout=timeout)

    def service_action(self, action: str) -> int:
        if action == "start":
            self.start_and_wait(timeout=SERVICE_READY_TIMEOUT_SECONDS)
            return 0
        if action == "stop":
            self.stop_and_confirm()
            return 0
        if action == "restart":
            self.stop_and_confirm()
            self.start_and_wait(timeout=SERVICE_READY_TIMEOUT_SECONDS)
            return 0
        loaded = self._is_loaded()
        ready = loaded and _ready_marker_present(self.layout)
        info("LaunchAgent status:")
        info(f"  installed: {'yes' if _path_exists(self.layout.service_file) else 'no'}")
        info(f"  loaded: {'yes' if loaded else 'no'}")
        info(f"  ready: {'yes' if ready else 'no'}")
        info(f"  log: {self.layout.log_file}")
        info(f"  launchd stderr: {self.layout.service_error_log}")
        return 0 if ready else 3

    def uninstall_definition(self) -> None:
        if _path_exists(self.layout.service_file):
            _require_managed_launch_agent(self.layout.service_file, self.layout)
            self.stop_and_confirm()
            self._set_enabled(False)
            self.layout.service_file.unlink()
            return
        if self._is_loaded():
            raise InstallError(
                "the managed LaunchAgent plist is missing but the launchd target is "
                "still loaded; inspect it before uninstalling"
            )

    def inspect_legacy(self) -> LegacyServiceState:
        return LegacyServiceState()

    def disable_legacy(
        self,
        state: LegacyServiceState,
        *,
        interactive: bool,
    ) -> None:
        del state, interactive

    def restore_legacy(
        self,
        state: LegacyServiceState,
        *,
        interactive: bool,
    ) -> None:
        del state, interactive
