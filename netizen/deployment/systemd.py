"""Linux user-service lifecycle for one canonical Netizen instance root."""

from __future__ import annotations

import os
import re
import shlex
import shutil
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
    run_command,
    _capture_file,
    _clean_subprocess_environment,
    _path_exists,
    _require_regular_file,
    _restore_file,
    _write_atomic,
)
from .service_backend import (
    ServiceState,
    SERVICE_READY_TIMEOUT_SECONDS,
    SERVICE_STOP_TIMEOUT_SECONDS,
    _clear_ready_marker,
    _ready_marker_present,
    _service_bootstrap_path,
    _service_environment,
    _wait_for_stop_confirmation,
)


SYSTEMD_SERVICE_MARKER = "# Managed by Netizen install.sh"
ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
RUNNING_UNIT_STATES = {"active", "activating", "reloading", "deactivating"}


def render_systemd_service(release: Release, layout: Layout) -> str:
    template_path = release.source / "deploy" / "netizen.service"
    try:
        template = template_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise InstallError(f"could not read systemd template: {template_path}") from error
    values = {
        "@HOME_ENV@": _systemd_quote(f"HOME={layout.home}"),
        "@ROOT_ENV@": _systemd_quote(f"NETIZEN_ROOT={layout.product_root}"),
        "@CODEX_HOME_ENV@": _systemd_quote(f"CODEX_HOME={layout.codex_home}"),
        "@PATH_ENV@": _systemd_quote(
            f"PATH={_service_bootstrap_path(layout)}"
        ),
        "@CONFIG_ENV@": _systemd_quote(f"NETIZEN_CONFIG_PATH={layout.config_file}"),
        "@LARK_APP_CONFIG_ENV@": _systemd_quote(
            f"NETIZEN_LARK_APP_CONFIG={layout.lark_app_file}"
        ),
        "@ADMIN_SECRET_ENV@": _systemd_quote(
            f"NETIZEN_ADMIN_SECRET_FILE={layout.admin_secret_file}"
        ),
        "@READY_FILE_ENV@": _systemd_quote(
            f"NETIZEN_READY_FILE={layout.ready_file}"
        ),
        "@LIFETIME_LOCK_FILE_ENV@": _systemd_quote(
            f"NETIZEN_LIFETIME_LOCK_FILE={layout.lifetime_lock_file}"
        ),
        "@EXEC_START@": _service_exec_start(layout),
        "@SYSLOG_IDENTIFIER@": layout.service_name.removesuffix(".service"),
    }
    template_tokens = set(re.findall(r"@[A-Z_]+@", template))
    missing = sorted(values.keys() - template_tokens)
    unknown = sorted(template_tokens - values.keys())
    if missing:
        raise InstallError(f"systemd template is missing placeholders: {missing}")
    if unknown:
        raise InstallError(f"systemd template has unknown placeholders: {unknown}")
    return re.sub(r"@[A-Z_]+@", lambda match: values[match.group()], template)


def _systemd_quote(value: str) -> str:
    if any(character in value for character in "\r\n\0"):
        raise InstallError("systemd values must not contain control characters")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{escaped}"'


def _service_exec_start(layout: Layout) -> str:
    # ExecStart expands dollars even inside quotes. A ':' prefix disables
    # environment substitution while preserving systemd's normal argv parsing.
    return ":" + " ".join((
        _systemd_quote(str(layout.current / "venv" / "bin" / "python")),
        "-E", "-B", "-u",
        _systemd_quote(str(layout.current / "source" / "scripts" / "netizen_service_launcher.py")),
    ))


def systemctl_user(
    layout: Layout,
    *arguments: str,
    runner: Runner | None = None,
    check: bool = True,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    execute = run_command if runner is None else runner
    return execute(
        ["systemctl", "--user", *arguments],
        check=check,
        capture_output=capture_output,
        env=_service_environment(layout),
    )


def _user_service_state(layout: Layout, runner: Runner) -> tuple[bool, bool]:
    # Prove that the user manager is reachable first. Some systemd releases
    # report a missing unit only on stderr for the two state queries below;
    # that is a normal first-install state, not a bus failure.
    manager_environment = systemctl_user(
        layout,
        "show-environment",
        runner=runner,
        capture_output=True,
    )
    _validate_user_unit_search_path(layout, manager_environment.stdout)
    active_result = systemctl_user(
        layout,
        "is-active",
        layout.service_name,
        runner=runner,
        check=False,
        capture_output=True,
    )
    enabled_result = systemctl_user(
        layout,
        "is-enabled",
        layout.service_name,
        runner=runner,
        check=False,
        capture_output=True,
    )
    active = active_result.stdout.strip() in RUNNING_UNIT_STATES
    enabled = enabled_result.stdout.strip() in {"enabled", "enabled-runtime"}
    return active, enabled


def _validate_user_unit_search_path(layout: Layout, output: str) -> None:
    manager_environment = _parse_systemd_manager_environment(output)

    fixed_unit_dir = layout.service_dir.resolve(strict=False)
    configured_unit_path = manager_environment.get("SYSTEMD_UNIT_PATH", "")
    explicit_fixed_path = False
    defaults_appended = not configured_unit_path or configured_unit_path.endswith(":")
    if configured_unit_path:
        explicit_fixed_path = any(
            Path(entry).is_absolute()
            and Path(entry).resolve(strict=False) == fixed_unit_dir
            for entry in configured_unit_path.split(os.pathsep)
            if entry
        )
    if not explicit_fixed_path and not defaults_appended:
        raise InstallError(
            "the systemd user manager replaces SYSTEMD_UNIT_PATH without Netizen's "
            f"fixed unit directory {layout.service_dir}"
        )

    configured_xdg = manager_environment.get("XDG_CONFIG_HOME", "").strip()
    if configured_xdg and not explicit_fixed_path:
        path = Path(configured_xdg)
        if (
            not path.is_absolute()
            or path.resolve(strict=False) != layout.config_home.resolve(strict=False)
        ):
            raise InstallError(
                "the systemd user manager uses XDG_CONFIG_HOME="
                f"{configured_xdg}, but Netizen requires the fixed user-unit directory "
                f"{layout.service_dir}; remove that manager override or add the fixed "
                "directory to SYSTEMD_UNIT_PATH"
            )


def _decode_systemd_environment_value(value: str) -> str:
    """Decode systemctl's documented shell-compatible $'...' representation."""

    if not value.startswith("$'"):
        return value
    if len(value) < 3 or not value.endswith("'"):
        raise ValueError("unterminated dollar-single-quoted value")

    body = value[2:-1]
    decoded = bytearray()
    simple_escapes = {
        "a": 0x07,
        "b": 0x08,
        "e": 0x1B,
        "E": 0x1B,
        "f": 0x0C,
        "n": 0x0A,
        "r": 0x0D,
        "t": 0x09,
        "v": 0x0B,
        "\\": 0x5C,
        "'": 0x27,
        '"': 0x22,
        "?": 0x3F,
    }
    index = 0
    while index < len(body):
        character = body[index]
        if character == "'":
            raise ValueError("unescaped quote in dollar-single-quoted value")
        if character != "\\":
            decoded.extend(os.fsencode(character))
            index += 1
            continue
        index += 1
        if index >= len(body):
            raise ValueError("trailing escape in dollar-single-quoted value")
        escaped = body[index]
        if escaped in simple_escapes:
            decoded.append(simple_escapes[escaped])
            index += 1
            continue
        if escaped == "x":
            digits = body[index + 1 : index + 3]
            if len(digits) != 2 or not all(
                character in "0123456789abcdefABCDEF" for character in digits
            ):
                raise ValueError("invalid hexadecimal escape")
            decoded.append(int(digits, 16))
            index += 3
            continue
        if escaped in "01234567":
            end = index + 1
            while end < min(index + 3, len(body)) and body[end] in "01234567":
                end += 1
            decoded.append(int(body[index:end], 8))
            index = end
            continue
        if escaped in {"u", "U"}:
            width = 4 if escaped == "u" else 8
            digits = body[index + 1 : index + 1 + width]
            if len(digits) != width or not all(
                character in "0123456789abcdefABCDEF" for character in digits
            ):
                raise ValueError("invalid Unicode escape")
            codepoint = int(digits, 16)
            if codepoint > 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
                raise ValueError("invalid Unicode codepoint")
            decoded.extend(os.fsencode(chr(codepoint)))
            index += 1 + width
            continue
        raise ValueError("unsupported dollar-single-quote escape")
    if b"\0" in decoded:
        raise ValueError("environment value contains NUL")
    return os.fsdecode(bytes(decoded))


def _parse_systemd_manager_environment(output: str) -> dict[str, str]:
    environment: dict[str, str] = {}
    for line_number, line in enumerate(output.splitlines(), start=1):
        name, separator, value = line.partition("=")
        if not separator or ENVIRONMENT_NAME.fullmatch(name) is None:
            raise InstallError(
                "systemd user manager returned an invalid environment entry "
                f"on line {line_number}"
            )
        try:
            environment[name] = _decode_systemd_environment_value(value)
        except ValueError as error:
            raise InstallError(
                "systemd user manager returned an invalid escaped environment "
                f"value for {name}"
            ) from error
    return environment


def ensure_linger(
    layout: Layout,
    *,
    interactive: bool,
    runner: Runner | None = None,
) -> None:
    execute = run_command if runner is None else runner
    result = execute(
        ["loginctl", "show-user", str(layout.uid), "--property=Linger", "--value"],
        capture_output=True,
        env=_clean_subprocess_environment(),
    )
    if result.stdout.strip().lower() == "yes":
        return
    command = ["loginctl", "enable-linger", layout.username]
    if os.geteuid() != 0:
        if not interactive:
            raise InstallError(
                "user lingering is disabled; run "
                f"sudo loginctl enable-linger {layout.username} once, then rerun the installer"
            )
        command.insert(0, "sudo")
    info("enabling systemd user lingering (one-time host authorization may be requested)")
    execute(command, env=_clean_subprocess_environment())


class SystemdServiceBackend:
    def __init__(self, layout: Layout, runner: Runner) -> None:
        self.layout = layout
        self._runner = runner
        self._known_stopped = False

    def preflight(self) -> None:
        _user_service_state(self.layout, self._runner)

    def prepare_host(self, *, interactive: bool) -> None:
        ensure_linger(self.layout, interactive=interactive, runner=self._runner)

    def inspect_state(self) -> ServiceState:
        active, enabled = _user_service_state(self.layout, self._runner)
        return ServiceState(loaded=active, enabled=enabled)

    def capture_definition(self) -> FileSnapshot:
        if _path_exists(self.layout.service_file):
            _require_managed_systemd_service(self.layout.service_file, self.layout)
        return _capture_file(
            self.layout.service_file,
            label="managed systemd service",
        )

    def render_definition(self, release: Release) -> bytes:
        return render_systemd_service(release, self.layout).encode()

    def _is_loaded(self) -> bool:
        result = systemctl_user(
            self.layout,
            "is-active",
            self.layout.service_name,
            runner=self._runner,
            check=False,
            capture_output=True,
        )
        return result.stdout.strip() in RUNNING_UNIT_STATES

    def stop_and_confirm(
        self,
        *,
        timeout: float = SERVICE_STOP_TIMEOUT_SECONDS,
    ) -> None:
        # Issue the idempotent stop even when the last state observation was
        # inactive: a prior start response may have been lost after creating
        # the process.
        try:
            systemctl_user(
                self.layout,
                "stop",
                self.layout.service_name,
                runner=self._runner,
            )
        except InstallError:
            # A first installation can fail before publishing its unit. Do
            # not mistake that absent target for a failed manager transition:
            # require exact, successful state evidence before checking the
            # lifetime lock. Other stop or query failures remain fail closed.
            state = systemctl_user(
                self.layout,
                "show",
                self.layout.service_name,
                "--property=LoadState",
                "--property=ActiveState",
                runner=self._runner,
                check=False,
                capture_output=True,
            )
            if state.returncode != 0 or sorted(state.stdout.splitlines()) != [
                "ActiveState=inactive", "LoadState=not-found",
            ]:
                raise
        # A successful stop is a synchronous manager transition; otherwise the
        # query above proved the target absent and inactive. The lifetime lock
        # independently proves that Python released rollback-protected state.
        _wait_for_stop_confirmation(
            self.layout,
            is_loaded=lambda: False,
            timeout=timeout,
        )
        self._known_stopped = True

    def publish_definition(self, content: bytes, *, should_enable: bool) -> None:
        if _path_exists(self.layout.service_file):
            _require_managed_systemd_service(self.layout.service_file, self.layout)
        _write_atomic(self.layout.service_file, content, mode=0o600)
        _require_managed_systemd_service(self.layout.service_file, self.layout)
        systemd_analyze = shutil.which(
            "systemd-analyze",
            path=_service_bootstrap_path(self.layout),
        )
        if systemd_analyze is not None:
            self._runner(
                [systemd_analyze, "--user", "verify", self.layout.service_file],
                env=_service_environment(self.layout),
            )
        systemctl_user(self.layout, "daemon-reload", runner=self._runner)
        systemctl_user(
            self.layout,
            "enable" if should_enable else "disable",
            self.layout.service_name,
            runner=self._runner,
            check=should_enable,
        )

    def restore_definition(
        self,
        snapshot: FileSnapshot,
        *,
        should_enable: bool,
    ) -> None:
        _restore_file(
            self.layout.service_file,
            snapshot,
            label="managed systemd service",
        )
        if snapshot.existed:
            _require_managed_systemd_service(self.layout.service_file, self.layout)
        systemctl_user(self.layout, "daemon-reload", runner=self._runner)
        systemctl_user(
            self.layout,
            "enable" if should_enable else "disable",
            self.layout.service_name,
            runner=self._runner,
            check=should_enable,
        )

    def start_and_wait(self, *, timeout: float) -> None:
        loaded = False if self._known_stopped else self._is_loaded()
        if loaded and _ready_marker_present(self.layout):
            return
        if not loaded:
            _clear_ready_marker(self.layout)
            self._known_stopped = False
            systemctl_user(
                self.layout,
                "start",
                self.layout.service_name,
                runner=self._runner,
            )
        _wait_for_systemd_ready(self.layout, timeout=timeout, runner=self._runner)

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
        result = systemctl_user(
            self.layout,
            "--no-pager",
            "--full",
            "status",
            self.layout.service_name,
            runner=self._runner,
            check=False,
        )
        return result.returncode

    def uninstall_definition(self) -> None:
        if _path_exists(self.layout.service_file):
            _require_managed_systemd_service(self.layout.service_file, self.layout)
            self.stop_and_confirm()
            systemctl_user(
                self.layout,
                "disable",
                self.layout.service_name,
                runner=self._runner,
                check=False,
            )
            self.layout.service_file.unlink()
            systemctl_user(self.layout, "daemon-reload", runner=self._runner)
            systemctl_user(
                self.layout,
                "reset-failed",
                self.layout.service_name,
                runner=self._runner,
                check=False,
            )
            return
        state = self.inspect_state()
        if state.loaded or state.enabled:
            raise InstallError(
                "the managed user service file is missing but systemd still has an "
                f"active/enabled {self.layout.service_name}; inspect it before uninstalling"
            )
        systemctl_user(self.layout, "daemon-reload", runner=self._runner)

def _wait_for_systemd_ready(
    layout: Layout,
    *,
    timeout: float,
    runner: Runner,
) -> None:
    deadline = time.monotonic() + timeout
    last_journal = ""
    while time.monotonic() < deadline:
        active = systemctl_user(
            layout,
            "is-active",
            layout.service_name,
            runner=runner,
            check=False,
            capture_output=True,
        )
        if active.stdout.strip() == "failed":
            break
        if _ready_marker_present(layout) and active.stdout.strip() == "active":
            return
        journal = runner(
            [
                "journalctl",
                "--user",
                "--unit",
                layout.service_name,
                "--lines=5",
                "--output=cat",
                "--no-pager",
            ],
            check=False,
            capture_output=True,
            env=_service_environment(layout),
        )
        last_journal = journal.stdout
        time.sleep(0.5)
    excerpt = " | ".join(line for line in last_journal.strip().splitlines()[-5:])
    suffix = f"; recent journal: {excerpt}" if excerpt else ""
    raise InstallError(
        f"{layout.service_name} did not become ready within {timeout:g}s{suffix}"
    )


def _require_managed_systemd_service(path: Path, layout: Layout) -> None:
    _require_regular_file(path, "managed systemd service")
    try:
        metadata = path.stat()
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise InstallError(f"could not read managed systemd service {path}: {error}") from error
    expected_environment = {
        "HOME": str(layout.home),
        "NETIZEN_ROOT": str(layout.product_root),
        "NETIZEN_CONFIG_PATH": str(layout.config_file),
        "NETIZEN_LARK_APP_CONFIG": str(layout.lark_app_file),
        "NETIZEN_ADMIN_SECRET_FILE": str(layout.admin_secret_file),
        "NETIZEN_READY_FILE": str(layout.ready_file),
        "NETIZEN_LIFETIME_LOCK_FILE": str(layout.lifetime_lock_file),
    }
    environment: dict[str, str] = {}
    exec_start: list[str] = []
    section = ""
    valid = True
    try:
        for raw in content.splitlines():
            line = raw.strip()
            if not line or line.startswith(("#", ";")):
                continue
            if line.startswith("["):
                section = line
                continue
            if section != "[Service]":
                continue
            key, separator, value = line.partition("=")
            if not separator or line.endswith("\\"):
                valid = False
                break
            if key == "ExecStart":
                exec_start.append(value)
            elif key == "Environment":
                if not value:
                    environment.clear()
                for assignment in shlex.split(value):
                    name, equal, variable = assignment.partition("=")
                    if not equal or (name in expected_environment and name in environment):
                        valid = False
                    environment[name] = variable
            elif key in {"EnvironmentFile", "PassEnvironment"} and value:
                valid = False
            elif key == "UnsetEnvironment":
                if any(entry.split("=", 1)[0] in expected_environment for entry in shlex.split(value)):
                    valid = False
    except ValueError:
        valid = False
    if (
        not valid
        or SYSTEMD_SERVICE_MARKER not in content.splitlines()
        or metadata.st_uid != layout.uid
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or exec_start != [_service_exec_start(layout)]
        or any(environment.get(name) != value.replace("%", "%%")
               for name, value in expected_environment.items())
        or ("NETIZEN_LOG_FILE" in environment
            and environment["NETIZEN_LOG_FILE"] != str(layout.log_file).replace("%", "%%"))
    ):
        raise InstallError(
            f"refusing to operate on an unrecognized systemd user service: {path}"
        )
