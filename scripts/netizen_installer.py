#!/usr/bin/env python3
"""Per-user installer and service lifecycle for Netizen.

The public interface is deliberately the repository shell scripts.  This
module keeps filesystem and service-manager behavior testable without teaching
the shell wrappers about releases, configuration, or rollback.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import getpass
import hashlib
import json
import os
import plistlib
import pwd
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Protocol


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.feishu_app_onboarding import REQUIRED_TENANT_SCOPES  # noqa: E402
from scripts.install_user_guide_skill import (  # noqa: E402
    SKILL_NAME,
    SkillInstallError,
    install_user_guide_skill,
    remove_user_guide_skill,
)
from netizen.bindings import (  # noqa: E402
    migrate_channel_database_v5_to_v6,
)


SYSTEMD_SERVICE_NAME = "netizen.service"
SYSTEMD_SERVICE_MARKER = "# Managed by Netizen install.sh"
LAUNCH_AGENT_LABEL = "io.github.lijingda.netizen"
LAUNCH_AGENT_SENTINEL_NAME = "NETIZEN_MANAGED_LAUNCH_AGENT"
LAUNCH_AGENT_SENTINEL_VALUE = "io.github.lijingda.netizen/v1"
READY_MARKER_CONTENT = b"netizen service ready\n"
LEGACY_SYSTEMD_READY_LOG = "netizen service ready"
SYSTEMD_READY_ENVIRONMENT_TOKEN = b"NETIZEN_READY_FILE="
SERVICE_READY_TIMEOUT_SECONDS = 45.0
SERVICE_STOP_TIMEOUT_SECONDS = 90.0
RELEASE_METADATA = ".release.json"
PUBLISHED_RELEASE_MANIFEST = ".netizen-release.json"
PUBLISHED_RELEASE_QUALIFICATION = "github-release"
OFFICIAL_RELEASE_DOWNLOADS = "https://github.com/lijingda/netizen/releases/download"
ACTIVATION_INTENT = ".activation-intent.json"
MANAGED_DIRECTORY_MARKER = ".netizen-managed"
MANAGED_DIRECTORY_MARKER_CONTENT = b"netizen-installer-managed-directory-v1\n"
CHANNEL_DATABASE_FILES = (
    "channel.sqlite3",
    "channel.sqlite3-journal",
    "channel.sqlite3-shm",
    "channel.sqlite3-wal",
)
SQLITE_DATABASE_HEADER = b"SQLite format 3\x00"
SOURCE_DIRECTORIES = ("netizen", "scripts", "skills", "deploy", "docs", "tests")
SOURCE_FILES = (
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
RELEASE_NAME = re.compile(r"^[0-9a-f]{64}$")
ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
RUNNING_UNIT_STATES = {"active", "activating", "reloading", "deactivating"}
CONFIGURED_APP_ID = re.compile(
    r"(?m)^[ \t]*appId:[ \t]*(?:\"(?P<double>cli_[A-Za-z0-9_-]+)\"|"
    r"'(?P<single>cli_[A-Za-z0-9_-]+)'|(?P<plain>cli_[A-Za-z0-9_-]+))"
    r"[ \t]*(?:#.*)?$"
)


class InstallError(RuntimeError):
    """The requested lifecycle operation cannot be completed safely."""


class ConfigurationRequired(InstallError):
    """A non-interactive install prepared files that the caller must fill."""


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
class PublishedReleaseManifest:
    version: str
    commit: str
    source_digest: str
    requirements_digest: str


@dataclass(frozen=True, slots=True)
class LegacyServiceState:
    present: bool = False
    recognized: bool = False
    active: bool = False
    enabled: bool = False


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    existed: bool
    content: bytes = b""
    mode: int = 0o600


@dataclass(frozen=True, slots=True)
class SkillSnapshot:
    kind: str
    saved_path: Path | None = None
    link_target: str | None = None


@dataclass(frozen=True, slots=True)
class DatabaseSnapshot:
    data_dir: Path
    saved_root: Path
    existing_files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ActivationIntent:
    release: str
    prior_release: str | None
    should_start: bool
    should_enable: bool


@dataclass(frozen=True, slots=True)
class AdminBind:
    enabled: bool
    host: str
    port: int


@dataclass(frozen=True, slots=True)
class FeishuAppCredentials:
    app_id: str
    app_secret: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class RuntimeValidation:
    data_dir: Path
    admin_bind: AdminBind


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


Runner = Callable[..., subprocess.CompletedProcess[str]]
AppRegistrar = Callable[[str | None], FeishuAppCredentials]


class ReleaseChecks(Protocol):
    def __call__(
        self,
        release: Release,
        runner: Runner,
        *,
        environment: Mapping[str, str],
    ) -> None: ...


class CandidatePreparer(Protocol):
    def __call__(
        self,
        layout: Layout,
        *,
        source_root: Path,
        runner: Runner,
    ) -> Release: ...


def info(message: str) -> None:
    print(f"[netizen] {message}", flush=True)


def resolve_layout(
    *,
    environ: Mapping[str, str] | None = None,
    account_home: Path | None = None,
    uid: int | None = None,
    username: str | None = None,
    platform_name: str | None = None,
) -> Layout:
    """Resolve fixed deployment paths for the effective user, never ``$SUDO_USER``."""

    env = os.environ if environ is None else environ
    effective_uid = os.geteuid() if uid is None else uid
    try:
        account = pwd.getpwuid(effective_uid)
    except KeyError as error:
        raise InstallError(
            f"effective uid {effective_uid} has no account database entry"
        ) from error
    home = Path(account.pw_dir) if account_home is None else account_home
    user = account.pw_name if username is None else username
    if not home.is_absolute() or home == Path(home.anchor):
        raise InstallError(f"current user's home must be an absolute non-root path: {home}")

    # Netizen has one product root, one user unit, and one managed global Skill
    # per Unix user. Keep their deployment identity stable across shells,
    # agents, and sudo environments instead of allowing XDG overrides to
    # select another root.
    config_home = home / ".config"
    configured_codex_home = env.get("CODEX_HOME", "").strip()
    codex_home = (
        Path(configured_codex_home) if configured_codex_home else home / ".codex"
    )
    if not codex_home.is_absolute() or codex_home == Path(codex_home.anchor):
        raise InstallError(f"CODEX_HOME must be an absolute non-root path: {codex_home}")

    product_root = home / ".netizen"
    credentials_dir = product_root / "credentials"
    selected_platform = _supported_platform_name(platform_name)
    if selected_platform == "linux":
        service_dir = config_home / "systemd" / "user"
        service_file = service_dir / SYSTEMD_SERVICE_NAME
        service_error_log = product_root / "state" / "service.stderr.log"
    else:
        service_dir = home / "Library" / "LaunchAgents"
        service_file = service_dir / f"{LAUNCH_AGENT_LABEL}.plist"
        service_error_log = product_root / "state" / "launchd.stderr.log"
    layout = Layout(
        platform=selected_platform,
        uid=effective_uid,
        username=user,
        home=home,
        config_home=config_home,
        codex_home=codex_home,
        product_root=product_root,
        releases=product_root / "releases",
        current=product_root / "current",
        previous=product_root / "previous",
        config_file=product_root / "config.yaml",
        credentials_dir=credentials_dir,
        secret_file=credentials_dir / "feishu-app-secret",
        admin_secret_file=credentials_dir / "admin-web-secret",
        state_dir=product_root / "state",
        cache_dir=product_root / "cache",
        service_dir=service_dir,
        service_file=service_file,
        ready_file=product_root / "state" / "service.ready",
        lifetime_lock_file=product_root / "state" / "service.lifetime.lock",
        log_file=product_root / "state" / "netizen.log",
        service_error_log=service_error_log,
    )
    _validate_layout_safety(layout)
    return layout


def _supported_platform_name(platform_name: str | None = None) -> str:
    selected = sys.platform if platform_name is None else platform_name
    if selected.startswith("linux"):
        return "linux"
    if selected == "darwin":
        return "darwin"
    raise InstallError(
        "this release supports Linux + systemd and macOS + LaunchAgent only"
    )


def _validate_layout_safety(layout: Layout) -> None:
    if _path_exists(layout.product_root) and (
        layout.product_root.is_symlink() or not layout.product_root.is_dir()
    ):
        raise InstallError(
            f"Netizen product root is not a real directory: {layout.product_root}"
        )
    product_root = layout.product_root.resolve(strict=False)
    deletion_roots = (layout.releases, layout.cache_dir)
    preserved_roots = (
        layout.config_file,
        layout.credentials_dir,
        layout.state_dir,
        layout.codex_home,
    )
    for deletion_root in deletion_roots:
        resolved = deletion_root.resolve(strict=False)
        if resolved.parent != product_root:
            raise InstallError(
                f"uninstall target must be a direct child of {layout.product_root}: "
                f"{deletion_root}"
            )
    for index, first in enumerate(deletion_roots):
        for second in deletion_roots[index + 1 :]:
            if _paths_overlap(first, second):
                raise InstallError(
                    "managed deployment paths overlap: "
                    f"{first}, {second}"
                )
        for preserved in preserved_roots:
            if _paths_overlap(first, preserved):
                raise InstallError(
                    "deployment/CODEX_HOME paths overlap a preserved directory with an "
                    f"uninstall target: {first}, {preserved}"
                )


def _paths_overlap(first: Path, second: Path) -> bool:
    resolved_first = first.resolve(strict=False)
    resolved_second = second.resolve(strict=False)
    return (
        resolved_first == resolved_second
        or resolved_first.is_relative_to(resolved_second)
        or resolved_second.is_relative_to(resolved_first)
    )


def _validate_source_location(source_root: Path, layout: Layout) -> None:
    source = source_root.resolve(strict=True)
    managed_release_source = (
        source.parent.parent == layout.releases.resolve(strict=False)
        and RELEASE_NAME.fullmatch(source.parent.name) is not None
        and source.name == "source"
    )
    for managed in (
        layout.product_root,
        layout.codex_home,
    ):
        if not _paths_overlap(source, managed):
            continue
        if managed == layout.product_root and managed_release_source:
            continue
        raise InstallError(
            f"release source overlaps a managed install/data path: {source}, {managed}"
        )


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


def require_supported_platform(
    platform_name: str | None = None,
    *,
    require_definition_validation: bool = False,
) -> str:
    selected = _supported_platform_name(platform_name)
    if sys.version_info[:2] not in {(3, 11), (3, 12)}:
        raise InstallError("Python 3.11 or 3.12 is required")
    if selected == "linux":
        if shutil.which("systemctl") is None:
            raise InstallError("systemctl is required for the Linux installer")
        if (
            require_definition_validation
            and shutil.which("systemd-analyze") is None
        ):
            raise InstallError("systemd-analyze is required for user-unit validation")
    else:
        if shutil.which("launchctl") is None:
            raise InstallError("launchctl is required for the macOS installer")
        if require_definition_validation and shutil.which("plutil") is None:
            raise InstallError("plutil is required for LaunchAgent validation")
    return selected


def prepare_directories(layout: Layout) -> None:
    _ensure_real_directory(layout.product_root, mode=0o700)
    _ensure_managed_netizen_directory(layout.releases)
    for path in (
        layout.credentials_dir,
        layout.state_dir,
    ):
        _ensure_real_directory(path, mode=0o700)
    _ensure_managed_netizen_directory(layout.cache_dir)
    _ensure_real_directory(layout.service_dir, mode=0o700, enforce_mode=False)


def _ensure_managed_netizen_directory(path: Path) -> None:
    existed = _path_exists(path)
    if existed and (path.is_symlink() or not path.is_dir()):
        raise InstallError(f"managed Netizen path is not a real directory: {path}")
    if not existed:
        _ensure_real_directory(path, mode=0o700)
    else:
        path.chmod(0o700)

    marker = path / MANAGED_DIRECTORY_MARKER
    if not _path_exists(marker):
        try:
            nonempty = next(path.iterdir(), None) is not None
        except OSError as error:
            raise InstallError(f"could not inspect managed directory {path}: {error}") from error
        if nonempty:
            raise InstallError(
                f"refusing to claim a non-empty directory without a Netizen marker: {path}"
            )
        _write_atomic(marker, MANAGED_DIRECTORY_MARKER_CONTENT, mode=0o600)
        return

    _require_regular_file(marker, "managed directory marker")
    try:
        content = marker.read_bytes()
        marker.chmod(0o600)
    except OSError as error:
        raise InstallError(f"could not read managed directory marker {marker}: {error}") from error
    if content != MANAGED_DIRECTORY_MARKER_CONTENT:
        raise InstallError(f"managed directory marker is not recognized: {marker}")


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


@contextlib.contextmanager
def installation_lock(layout: Layout) -> Iterator[None]:
    # The lock must outlive uninstall. Deleting a locked file would let a new
    # process create a second inode and enter concurrently with an old waiter.
    _ensure_real_directory(layout.product_root, mode=0o700)
    _ensure_real_directory(layout.state_dir, mode=0o700)
    lock_path = layout.state_dir / ".install.lock"
    try:
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            yield
    except OSError as error:
        raise InstallError(f"could not lock Netizen installation: {error}") from error


def prepare_configuration(
    layout: Layout,
    *,
    interactive: bool,
    rerun_instruction: str = "rerun ./dev-install.sh",
    input_stream: IO[str] | None = None,
    secret_prompt: Callable[[str], str] = getpass.getpass,
    app_registrar: AppRegistrar | None = None,
) -> None:
    """Create or complete config without ever waiting on a non-TTY caller."""

    source = sys.stdin if input_stream is None else input_stream
    config_missing = not _path_exists(layout.config_file)
    secret_missing = not _path_exists(layout.secret_file)
    if config_missing:
        _write_atomic(
            layout.config_file,
            _default_config(layout, app_id="cli_REPLACE_ME").encode(),
            mode=0o600,
        )
        _ensure_project_directory(layout.home / "projects")
    else:
        _require_regular_file(layout.config_file, "configuration")
        layout.config_file.chmod(0o600)

    _prepare_admin_secret(layout.admin_secret_file)

    try:
        config_text = layout.config_file.read_text(encoding="utf-8")
    except UnicodeError as error:
        raise InstallError(f"configuration is not valid UTF-8: {layout.config_file}") from error
    needs_app_id = "cli_REPLACE_ME" in config_text
    configured_app_id = None if needs_app_id else _configured_app_id(config_text)
    rebind_requested = secret_missing and configured_app_id is not None

    if secret_missing and not rebind_requested:
        _write_atomic(layout.secret_file, b"", mode=0o600)
    elif not secret_missing:
        _require_regular_file(layout.secret_file, "Feishu secret")
        layout.secret_file.chmod(0o600)
    if rebind_requested:
        needs_secret = True
    else:
        try:
            needs_secret = not layout.secret_file.read_bytes().strip()
        except OSError as error:
            raise InstallError(
                f"could not read Feishu secret file {layout.secret_file}: {error}"
            ) from error

    if interactive and (needs_app_id or needs_secret):
        if rebind_requested:
            assert configured_app_id is not None
            info(
                "Feishu/Lark app binding reset requested because the App Secret "
                f"file is missing; selecting a different app replaces {configured_app_id} "
                "and does not migrate its Feishu sessions"
            )
        registration_app_id = (
            None if needs_app_id or rebind_requested else configured_app_id
        )
        can_register = app_registrar is not None and (
            needs_app_id or rebind_requested or configured_app_id is not None
        )
        use_browser = can_register and _prompt_feishu_setup_method(
            source,
            app_id=registration_app_id,
        )
        if use_browser:
            info(
                "starting official Feishu/Lark browser setup; "
                "the App Secret will not be displayed"
            )
            try:
                credentials = app_registrar(registration_app_id)
                config_text = _store_registered_feishu_credentials(
                    layout,
                    config_text=config_text,
                    expected_app_id=registration_app_id,
                    replace_existing_app_id=(
                        configured_app_id if rebind_requested else None
                    ),
                    credentials=credentials,
                )
            except InstallError:
                print(
                    "Browser setup did not complete. Enter an existing App ID and "
                    "App Secret manually instead.",
                    file=sys.stderr,
                )
            else:
                needs_app_id = False
                needs_secret = False
                rebind_requested = False
                configured_app_id = credentials.app_id
                info(
                    f"Feishu/Lark app {credentials.app_id} configured; "
                    f"credential saved to {layout.secret_file}"
                )
                info(
                    "finish any tenant-admin approval/application publication, "
                    "set availability, and add the bot to target chats"
                )

        if needs_app_id or rebind_requested:
            app_id = _prompt_app_id(source)
            if rebind_requested:
                assert configured_app_id is not None
                config_text = _replace_configured_app_id(
                    config_text,
                    expected_app_id=configured_app_id,
                    replacement_app_id=app_id,
                )
            else:
                config_text = config_text.replace("cli_REPLACE_ME", app_id, 1)
            _write_atomic(layout.config_file, config_text.encode(), mode=0o600)
            needs_app_id = False
            rebind_requested = False
            configured_app_id = app_id

        if needs_secret:
            try:
                secret = secret_prompt("Feishu App Secret: ").strip()
            except EOFError as error:
                raise InstallError(
                    "Feishu App Secret input ended before a value was provided"
                ) from error
            if not secret:
                raise InstallError("Feishu App Secret must not be empty")
            _write_atomic(layout.secret_file, secret.encode(), mode=0o600)
            needs_secret = False

    if needs_app_id or needs_secret:
        missing: list[str] = []
        if rebind_requested:
            missing.append(
                f"{rerun_instruction} in an interactive terminal to create or select "
                "a Feishu/Lark app, or update instance.appId in "
                f"{layout.config_file} and write the raw App Secret to "
                f"{layout.secret_file}"
            )
        elif needs_app_id:
            missing.append(f"replace cli_REPLACE_ME in {layout.config_file}")
        if needs_secret and not rebind_requested:
            missing.append(f"write the raw App Secret to {layout.secret_file}")
        raise ConfigurationRequired(
            "non-interactive install will not prompt for credentials; "
            + "; ".join(missing)
            + f"; then {rerun_instruction}"
        )


def _configured_app_id(config_text: str) -> str | None:
    match = CONFIGURED_APP_ID.search(config_text)
    if match is None:
        return None
    return next(value for value in match.groupdict().values() if value is not None)


def _replace_configured_app_id(
    config_text: str,
    *,
    expected_app_id: str,
    replacement_app_id: str,
) -> str:
    matches = list(CONFIGURED_APP_ID.finditer(config_text))
    if len(matches) != 1:
        raise InstallError("configuration does not contain exactly one Feishu/Lark App ID")
    match = matches[0]
    matched_group = next(
        name for name, value in match.groupdict().items() if value is not None
    )
    if match.group(matched_group) != expected_app_id:
        raise InstallError("configuration App ID changed during Feishu/Lark setup")
    start, end = match.span(matched_group)
    return config_text[:start] + replacement_app_id + config_text[end:]


def _prompt_feishu_setup_method(source: IO[str], *, app_id: str | None) -> bool:
    if app_id is None:
        browser_action = "Create or select and configure a Feishu/Lark app in the browser"
    else:
        browser_action = f"Configure existing app {app_id} in the browser"
    print("Feishu/Lark application setup:")
    print(f"  1) {browser_action} (recommended)")
    print("  2) Enter App ID and App Secret manually")
    while True:
        print("Choose [1]: ", end="", flush=True)
        raw_choice = source.readline()
        if raw_choice == "":
            raise InstallError("Feishu/Lark setup choice ended before a value was provided")
        choice = raw_choice.strip()
        if choice in {"", "1"}:
            return True
        if choice == "2":
            return False
        print("Choose 1 or 2.", file=sys.stderr)


def _prompt_app_id(source: IO[str]) -> str:
    while True:
        print("Feishu App ID (cli_...): ", end="", flush=True)
        raw_app_id = source.readline()
        if raw_app_id == "":
            raise InstallError("Feishu App ID input ended before a value was provided")
        app_id = raw_app_id.strip()
        if app_id.startswith("cli_"):
            return app_id
        print("App ID must start with cli_.", file=sys.stderr)


def _store_registered_feishu_credentials(
    layout: Layout,
    *,
    config_text: str,
    expected_app_id: str | None,
    replace_existing_app_id: str | None = None,
    credentials: FeishuAppCredentials,
) -> str:
    app_id = credentials.app_id
    app_secret = credentials.app_secret
    if (
        not app_id.startswith("cli_")
        or app_id.strip() != app_id
        or any(ord(character) < 0x20 for character in app_id)
    ):
        raise InstallError("browser setup returned an invalid App ID")
    if (
        not app_secret
        or app_secret.strip() != app_secret
        or any(ord(character) < 0x20 for character in app_secret)
    ):
        raise InstallError("browser setup returned an invalid App Secret")
    if expected_app_id is not None and replace_existing_app_id is not None:
        raise InstallError("Feishu/Lark setup has conflicting App ID expectations")
    if expected_app_id is not None and app_id != expected_app_id:
        raise InstallError("browser setup returned a different App ID")
    if replace_existing_app_id is not None:
        updated_config = _replace_configured_app_id(
            config_text,
            expected_app_id=replace_existing_app_id,
            replacement_app_id=app_id,
        )
    elif expected_app_id is None:
        if "cli_REPLACE_ME" not in config_text:
            raise InstallError("configuration has no App ID placeholder to update")
        updated_config = config_text.replace("cli_REPLACE_ME", app_id, 1)
    else:
        updated_config = config_text

    config_snapshot = _capture_file(layout.config_file, label="configuration")
    secret_snapshot = _capture_file(layout.secret_file, label="Feishu secret")
    try:
        if updated_config != config_text:
            _write_atomic(layout.config_file, updated_config.encode(), mode=0o600)
        _write_atomic(layout.secret_file, app_secret.encode(), mode=0o600)
    except BaseException as error:
        rollback_failed = False
        for path, snapshot, label in (
            (layout.config_file, config_snapshot, "configuration"),
            (layout.secret_file, secret_snapshot, "Feishu secret"),
        ):
            try:
                _restore_file(path, snapshot, label=label)
            except (OSError, InstallError):
                rollback_failed = True
        if rollback_failed:
            raise InstallError(
                "could not roll back Feishu credential files; inspect "
                f"{layout.config_file} and {layout.secret_file} before rerunning"
            ) from error
        raise
    return updated_config


def _default_config(layout: Layout, *, app_id: str) -> str:
    project_root = layout.home / "projects"
    return (
        "# Generated by Netizen. Edit this file before rerunning the installer.\n"
        "instance:\n"
        f"  appId: {json.dumps(app_id)}\n"
        f"  dataDir: {json.dumps(str(layout.state_dir))}\n"
        "  # `none` maps to this one native cwd; it is not a workspace copy.\n"
        f"  defaultCwd: {json.dumps(str(layout.home))}\n"
        f"  projectRoot: {json.dumps(str(project_root))}\n"
        "\n"
        "projects: {}\n"
        "\n"
        "channel:\n"
        "  securityMode: audit\n"
        "\n"
        "adminWeb:\n"
        "  enabled: true\n"
        "  host: 0.0.0.0\n"
        "  port: 8787\n"
    )


def _prepare_admin_secret(path: Path) -> None:
    """Create the independent Admin credential once, then only validate it."""

    if not _path_exists(path):
        _write_atomic(path, secrets.token_urlsafe(32).encode("ascii"), mode=0o600)
        return
    _require_regular_file(path, "Admin Web secret")
    try:
        metadata = path.stat()
        content = path.read_bytes()
    except OSError as error:
        raise InstallError(f"could not read Admin Web secret file {path}: {error}") from error
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise InstallError("Admin Web secret file permissions must be exactly 0600")
    try:
        from netizen.admin.auth import CredentialFileError, load_credential_snapshot

        load_credential_snapshot(path)
    except CredentialFileError as error:
        raise InstallError(
            "Admin Web secret must be one canonical 32-byte base64url credential"
        ) from error
    if content.endswith(b"\n"):
        raise InstallError("Admin Web secret must not contain a trailing newline")


def _ensure_project_directory(path: Path) -> None:
    if path.exists():
        if not path.is_dir():
            raise InstallError(f"default projectRoot is not a directory: {path}")
        return
    if path.is_symlink():
        raise InstallError(f"default projectRoot is a broken symlink: {path}")
    try:
        path.mkdir(mode=0o700, parents=True)
    except OSError as error:
        raise InstallError(f"could not create default projectRoot {path}: {error}") from error


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


def source_manifest(source_root: Path) -> dict[str, tuple[str, bool]]:
    root = source_root.resolve(strict=True)
    manifest: dict[str, tuple[str, bool]] = {}
    for path in _source_files(root):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        executable = bool(metadata.st_mode & stat.S_IXUSR)
        manifest[relative] = (hashlib.sha256(path.read_bytes()).hexdigest(), executable)
    if not manifest:
        raise InstallError(f"release source is empty: {root}")
    return manifest


def source_digest(manifest: Mapping[str, tuple[str, bool]]) -> str:
    digest = hashlib.sha256()
    for relative, (file_digest, executable) in sorted(manifest.items()):
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(b"x" if executable else b"-")
        digest.update(bytes.fromhex(file_digest))
    return digest.hexdigest()


def _release_identity(
    *,
    qualification: str,
    source_digest_value: str,
    published_manifest: PublishedReleaseManifest | None,
) -> str:
    identity = hashlib.sha256()
    identity.update(b"netizen-local-release-v2\0")
    identity.update(qualification.encode("ascii"))
    identity.update(b"\0")
    identity.update(source_digest_value.encode("ascii"))
    if published_manifest is not None:
        identity.update(b"\0")
        identity.update(published_manifest.version.encode("utf-8"))
        identity.update(b"\0")
        identity.update(published_manifest.commit.encode("ascii"))
        identity.update(b"\0")
        identity.update(published_manifest.requirements_digest.encode("ascii"))
    return identity.hexdigest()


def read_published_release_manifest(source_root: Path) -> PublishedReleaseManifest:
    root = source_root.resolve(strict=True)
    path = root / PUBLISHED_RELEASE_MANIFEST
    try:
        _require_source_file(path, root)
    except InstallError as error:
        raise InstallError(f"published Release manifest is unavailable: {path}") from error
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InstallError(f"published Release manifest is invalid: {path}") from error
    required = {
        "schema",
        "version",
        "commit",
        "sourceDigest",
        "requirementsDigest",
        "qualification",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise InstallError("published Release manifest has an unsupported shape")
    version = payload.get("version")
    commit = payload.get("commit")
    recorded_source_digest = payload.get("sourceDigest")
    recorded_requirements_digest = payload.get("requirementsDigest")
    if (
        payload.get("schema") != 1
        or payload.get("qualification") != PUBLISHED_RELEASE_QUALIFICATION
        or not isinstance(version, str)
        or not version
        or not isinstance(commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", commit) is None
        or not isinstance(recorded_source_digest, str)
        or RELEASE_NAME.fullmatch(recorded_source_digest) is None
        or not isinstance(recorded_requirements_digest, str)
        or RELEASE_NAME.fullmatch(recorded_requirements_digest) is None
    ):
        raise InstallError("published Release manifest contains invalid values")
    try:
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        project_version = project["project"]["version"]
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as error:
        raise InstallError("could not read the published Release project version") from error
    if not isinstance(project_version, str) or version != project_version:
        raise InstallError(
            "published Release version does not match pyproject.toml: "
            f"manifest={version}, project={project_version}"
        )
    actual_source_digest = source_digest(source_manifest(root))
    if recorded_source_digest != actual_source_digest:
        raise InstallError(
            "published Release source digest does not match its managed files"
        )
    try:
        actual_requirements_digest = hashlib.sha256(
            (root / "requirements.lock").read_bytes()
        ).hexdigest()
    except OSError as error:
        raise InstallError("could not read the published Release dependency lock") from error
    if recorded_requirements_digest != actual_requirements_digest:
        raise InstallError(
            "published Release dependency digest does not match requirements.lock"
        )
    return PublishedReleaseManifest(
        version=version,
        commit=commit,
        source_digest=recorded_source_digest,
        requirements_digest=recorded_requirements_digest,
    )


def _source_files(root: Path) -> Iterator[Path]:
    for name in SOURCE_FILES:
        path = root / name
        _require_source_file(path, root)
        yield path
    for directory_name in SOURCE_DIRECTORIES:
        directory = root / directory_name
        if directory.is_symlink() or not directory.is_dir():
            raise InstallError(f"required release directory is invalid: {directory}")
        for path in sorted(directory.rglob("*")):
            if any(part in IGNORED_SOURCE_NAMES for part in path.relative_to(root).parts):
                continue
            if path.suffix in {".pyc", ".pyo"}:
                continue
            if path.is_symlink():
                raise InstallError(f"release source contains a symlink: {path}")
            if path.is_dir():
                continue
            _require_source_file(path, root)
            yield path


def _require_source_file(path: Path, root: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise InstallError(f"required release file is unavailable: {path}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise InstallError(f"release source must contain only regular files: {path}")
    if not path.resolve().is_relative_to(root):
        raise InstallError(f"release file escapes the source root: {path}")


def snapshot_source(source_root: Path, destination: Path) -> tuple[str, int]:
    root = source_root.resolve(strict=True)
    manifest = source_manifest(root)
    digest = source_digest(manifest)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    try:
        for relative in sorted(manifest):
            source = root / relative
            target = destination / relative
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            shutil.copy2(source, target, follow_symlinks=False)
        installed = source_manifest(destination)
        if installed != manifest:
            raise InstallError("release source snapshot differs from the development tree")
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return digest, len(manifest)


def prepare_source_release(
    layout: Layout,
    *,
    source_root: Path,
    runner: Runner | None = None,
) -> Release:
    return _prepare_release(
        layout,
        source_root=source_root,
        qualification="source",
        published_manifest=None,
        candidate_checks=_run_source_release_checks,
        runner=runner,
    )


def prepare_published_release(
    layout: Layout,
    *,
    source_root: Path,
    manifest: PublishedReleaseManifest | None = None,
    runner: Runner | None = None,
) -> Release:
    selected_manifest = (
        read_published_release_manifest(source_root) if manifest is None else manifest
    )
    return _prepare_release(
        layout,
        source_root=source_root,
        qualification="published",
        published_manifest=selected_manifest,
        candidate_checks=_run_host_release_checks,
        runner=runner,
    )


def _prepare_release(
    layout: Layout,
    *,
    source_root: Path,
    qualification: str,
    published_manifest: PublishedReleaseManifest | None,
    candidate_checks: ReleaseChecks,
    runner: Runner | None = None,
) -> Release:
    execute = run_command if runner is None else runner
    environment = _clean_subprocess_environment()
    environment["HOME"] = str(layout.home)
    environment["CODEX_HOME"] = str(layout.codex_home)
    environment["PIP_CACHE_DIR"] = str(layout.cache_dir / "pip")
    manifest = source_manifest(source_root)
    content_digest = source_digest(manifest)
    digest = _release_identity(
        qualification=qualification,
        source_digest_value=content_digest,
        published_manifest=published_manifest,
    )
    release_root = layout.releases / digest
    release = Release(
        digest=digest,
        root=release_root,
        source=release_root / "source",
        venv=release_root / "venv",
    )
    if _release_is_ready(
        release,
        qualification=qualification,
        source_digest_value=content_digest,
        published_manifest=published_manifest,
    ):
        info(f"reusing verified release {digest[:12]}")
        _verify_installed_package(release, execute, environment=environment)
        candidate_checks(release, execute, environment=environment)
        if published_manifest is not None:
            _record_published_release_provenance(
                release,
                source_root=source_root,
                manifest=published_manifest,
            )
        return release
    if _path_exists(release_root):
        _remove_managed_release(release_root, layout)

    release_root.mkdir(mode=0o700, parents=False)
    try:
        copied_digest, file_count = snapshot_source(source_root, release.source)
        if copied_digest != content_digest:
            raise InstallError("development tree changed while its release was copied; rerun")
        if published_manifest is not None:
            shutil.copy2(
                source_root / PUBLISHED_RELEASE_MANIFEST,
                release.source / PUBLISHED_RELEASE_MANIFEST,
                follow_symlinks=False,
            )
            copied_manifest = read_published_release_manifest(release.source)
            if copied_manifest != published_manifest:
                raise InstallError(
                    "published Release manifest changed while its source was copied"
                )
        info(f"building release {digest[:12]} from {file_count} files")
        execute([sys.executable, "-m", "venv", release.venv], env=environment)
        python = release.venv / "bin" / "python"
        execute(
            [
                python,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "--constraint",
                release.source / "requirements.lock",
                release.source,
            ],
            env=environment,
        )
        _verify_installed_package(release, execute, environment=environment)
        candidate_checks(release, execute, environment=environment)
        _write_release_metadata(
            release,
            source_digest_value=content_digest,
            source_files=file_count,
            qualification=qualification,
            published_manifest=published_manifest,
        )
    except BaseException:
        shutil.rmtree(release_root, ignore_errors=True)
        raise
    return release


def _record_published_release_provenance(
    release: Release,
    *,
    source_root: Path,
    manifest: PublishedReleaseManifest,
) -> None:
    source = source_root.resolve(strict=True) / PUBLISHED_RELEASE_MANIFEST
    try:
        content = source.read_bytes()
    except OSError as error:
        raise InstallError(f"could not read published Release provenance: {source}") from error
    _write_atomic(
        release.source / PUBLISHED_RELEASE_MANIFEST,
        content,
        mode=0o644,
    )
    if read_published_release_manifest(release.source) != manifest:
        raise InstallError("published Release provenance changed while it was recorded")
    metadata_path = release.root / RELEASE_METADATA
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        source_files = metadata["sourceFiles"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise InstallError("verified release metadata became invalid") from error
    if not isinstance(source_files, int) or source_files < 1:
        raise InstallError("verified release metadata has an invalid source file count")
    _write_release_metadata(
        release,
        source_digest_value=manifest.source_digest,
        source_files=source_files,
        qualification="published",
        published_manifest=manifest,
    )


def _write_release_metadata(
    release: Release,
    *,
    source_digest_value: str,
    source_files: int,
    qualification: str,
    published_manifest: PublishedReleaseManifest | None,
) -> None:
    metadata: dict[str, object] = {
        "gateSchema": 1,
        "digest": release.digest,
        "sourceDigest": source_digest_value,
        "sourceFiles": source_files,
        "createdAt": int(time.time()),
        "python": str(release.venv / "bin" / "python"),
        "qualification": qualification,
    }
    if published_manifest is not None:
        metadata["publishedRelease"] = {
            "version": published_manifest.version,
            "commit": published_manifest.commit,
            "requirementsDigest": published_manifest.requirements_digest,
        }
    _write_atomic(
        release.root / RELEASE_METADATA,
        (json.dumps(metadata, sort_keys=True) + "\n").encode(),
        mode=0o600,
    )


def _run_source_release_checks(
    release: Release,
    runner: Runner,
    *,
    environment: Mapping[str, str],
) -> None:
    python = release.venv / "bin" / "python"
    source = release.source
    runner(
        [python, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=source,
        env=environment,
    )
    runner(
        [python, "-m", "compileall", "-q", "tests"],
        cwd=source,
        env=environment,
    )
    _run_host_release_checks(release, runner, environment=environment)


def _run_host_release_checks(
    release: Release,
    runner: Runner,
    *,
    environment: Mapping[str, str],
) -> None:
    python = release.venv / "bin" / "python"
    source = release.source
    runner(
        [python, "-m", "compileall", "-q", "netizen", "scripts"],
        cwd=source,
        env=environment,
    )
    runner([python, "-m", "pip", "check"], env=environment)
    runner(
        [python, source / "scripts" / "probe_sdk_turn_plan.py", "--timeout", "5"],
        env=environment,
    )
    runner(
        [
            python,
            source / "scripts" / "probe_sdk_completion_race.py",
            "--read-recovery",
            "--attempts",
            "20",
            "--timeout",
            "3",
        ],
        env=environment,
    )
    runner(
        [
            python,
            source / "scripts" / "probe_sdk_completion_race.py",
            "--usage-drain",
            "--attempts",
            "40",
            "--timeout",
            "10",
        ],
        env=environment,
    )


def _verify_installed_package(
    release: Release,
    runner: Runner,
    *,
    environment: Mapping[str, str],
) -> None:
    runner(
        [
            release.venv / "bin" / "python",
            release.source / "scripts" / "verify_installed_release.py",
            "--source-root",
            release.source,
        ],
        cwd=release.root,
        env=environment,
        capture_output=True,
    )


def _register_feishu_app_from_release(
    release: Release,
    app_id: str | None,
    *,
    runner: Runner,
) -> FeishuAppCredentials:
    command: list[str | os.PathLike[str]] = [
        release.venv / "bin" / "python",
        "-E",
        "-B",
        "-u",
        release.source / "scripts" / "feishu_app_onboarding.py",
    ]
    if app_id is not None:
        command.extend(("--app-id", app_id))
    result = runner(
        command,
        check=False,
        capture_stdout=True,
        env=_clean_subprocess_environment(),
        timeout=660.0,
    )
    if result.returncode == 130:
        raise KeyboardInterrupt
    if result.returncode != 0:
        raise InstallError("official Feishu/Lark browser setup did not complete")
    try:
        payload = json.loads(result.stdout or "")
    except (TypeError, json.JSONDecodeError) as error:
        raise InstallError(
            "official Feishu/Lark browser setup returned an invalid result"
        ) from error
    if (
        not isinstance(payload, dict)
        or set(payload) != {"version", "appId", "appSecret"}
        or payload.get("version") != 1
        or not isinstance(payload.get("appId"), str)
        or not isinstance(payload.get("appSecret"), str)
    ):
        raise InstallError(
            "official Feishu/Lark browser setup returned an invalid result"
        )
    return FeishuAppCredentials(
        app_id=payload["appId"],
        app_secret=payload["appSecret"],
    )


def _configured_app_id_from_file(layout: Layout) -> tuple[str, str]:
    try:
        config_text = layout.config_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise InstallError(
            f"could not read configuration {layout.config_file}: {error}"
        ) from error
    app_id = _configured_app_id(config_text)
    if app_id is None:
        raise InstallError("configuration does not contain one valid Feishu/Lark App ID")
    return app_id, config_text


def _query_missing_feishu_permissions_from_release(
    release: Release,
    layout: Layout,
    *,
    runner: Runner,
    rerun_instruction: str = "rerun ./dev-install.sh",
) -> tuple[str, ...]:
    app_id, _ = _configured_app_id_from_file(layout)
    command: list[str | os.PathLike[str]] = [
        release.venv / "bin" / "python",
        "-E",
        "-B",
        release.source / "scripts" / "feishu_app_permissions.py",
        "--app-id",
        app_id,
        "--secret-file",
        layout.secret_file,
    ]
    result = runner(
        command,
        check=False,
        capture_output=True,
        env=_clean_subprocess_environment(),
        timeout=90.0,
    )
    if result.returncode == 130:
        raise KeyboardInterrupt
    if result.returncode != 0:
        raise InstallError(
            "could not verify Feishu/Lark tenant permissions; ensure the app is "
            "installed in the tenant and its current version is published, then "
            f"{rerun_instruction}"
        )
    try:
        payload = json.loads(result.stdout or "")
    except (TypeError, json.JSONDecodeError) as error:
        raise InstallError(
            "Feishu/Lark tenant permission verification returned an invalid result"
        ) from error
    missing = payload.get("missingScopes") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or set(payload) != {"version", "missingScopes"}
        or payload.get("version") != 1
        or not isinstance(missing, list)
        or any(not isinstance(scope, str) for scope in missing)
        or len(set(missing)) != len(missing)
        or missing
        != [scope for scope in REQUIRED_TENANT_SCOPES if scope in set(missing)]
    ):
        raise InstallError(
            "Feishu/Lark tenant permission verification returned an invalid result"
        )
    return tuple(missing)


def _missing_feishu_permissions_message(
    missing: Sequence[str],
    *,
    rerun_instruction: str,
) -> str:
    return (
        "Feishu/Lark tenant permissions are not fully authorized: "
        + ", ".join(missing)
        + "; finish tenant-admin approval and application publication, ensure the "
        f"app is installed in the tenant, then {rerun_instruction}"
    )


def require_feishu_permissions(
    release: Release,
    layout: Layout,
    *,
    interactive: bool,
    repair_existing_app: bool,
    rerun_instruction: str = "rerun ./dev-install.sh",
    runner: Runner | None = None,
) -> None:
    """Gate activation on the effective tenant grant contract."""

    execute = run_command if runner is None else runner
    missing = _query_missing_feishu_permissions_from_release(
        release,
        layout,
        runner=execute,
        rerun_instruction=rerun_instruction,
    )
    if missing and interactive and repair_existing_app:
        app_id, config_text = _configured_app_id_from_file(layout)
        info(
            "Feishu/Lark app is missing required tenant permissions; opening the "
            f"official browser flow to update exact app {app_id}: "
            + ", ".join(missing)
        )
        try:
            credentials = _register_feishu_app_from_release(
                release,
                app_id,
                runner=execute,
            )
            _store_registered_feishu_credentials(
                layout,
                config_text=config_text,
                expected_app_id=app_id,
                credentials=credentials,
            )
        except InstallError as error:
            raise InstallError(
                "official Feishu/Lark browser repair did not complete; "
                + _missing_feishu_permissions_message(
                    missing,
                    rerun_instruction=rerun_instruction,
                )
            ) from error
        missing = _query_missing_feishu_permissions_from_release(
            release,
            layout,
            runner=execute,
            rerun_instruction=rerun_instruction,
        )
    if missing:
        raise InstallError(
            _missing_feishu_permissions_message(
                missing,
                rerun_instruction=rerun_instruction,
            )
        )
    info("Feishu/Lark tenant permissions verified")


def _release_is_ready(
    release: Release,
    *,
    qualification: str,
    source_digest_value: str,
    published_manifest: PublishedReleaseManifest | None,
) -> bool:
    metadata_path = release.root / RELEASE_METADATA
    if release.root.is_symlink() or not release.root.is_dir():
        return False
    if not metadata_path.is_file() or metadata_path.is_symlink():
        return False
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict):
            return False
        if (
            metadata.get("gateSchema") != 1
            or metadata.get("digest") != release.digest
            or metadata.get("sourceDigest") != source_digest_value
            or metadata.get("qualification") != qualification
        ):
            return False
        if published_manifest is None:
            if "publishedRelease" in metadata:
                return False
        else:
            expected_published = {
                "version": published_manifest.version,
                "commit": published_manifest.commit,
                "requirementsDigest": published_manifest.requirements_digest,
            }
            if metadata.get("publishedRelease") != expected_published:
                return False
        return (
            source_digest(source_manifest(release.source)) == source_digest_value
            and (release.venv / "bin" / "python").is_file()
        )
    except (InstallError, OSError, UnicodeError, ValueError, TypeError):
        return False


def _remove_managed_release(path: Path, layout: Layout) -> None:
    if path.is_symlink() or not path.is_dir():
        raise InstallError(f"managed release is not a real directory: {path}")
    resolved = path.resolve()
    if resolved.parent != layout.releases.resolve() or not RELEASE_NAME.fullmatch(path.name):
        raise InstallError(f"refusing to remove an unmanaged release path: {path}")
    active_targets = {
        target
        for link in (layout.current, layout.previous)
        if (target := _read_release_link(link, layout))
    }
    if resolved in active_targets:
        raise InstallError(f"refusing to remove an active release: {path}")
    shutil.rmtree(path)


def validate_runtime(
    release: Release,
    layout: Layout,
    runner: Runner | None = None,
) -> RuntimeValidation:
    execute = run_command if runner is None else runner
    runtime_environment = _service_environment(layout)
    configuration = execute(
        [
            release.venv / "bin" / "python",
            "-E",
            "-B",
            "-c",
            (
                "from netizen.settings import Settings; "
                "from pathlib import Path; import json, sys; "
                "settings = Settings.from_file(sys.argv[1]); "
                "resolved_data_dir = settings.data_dir.resolve(); "
                "resolved_data_dir == Path(resolved_data_dir.anchor) and "
                "sys.exit('instance.dataDir must not be a filesystem root'); "
                "paths = {'instance.defaultCwd': settings.default_cwd, "
                "'instance.projectRoot': settings.project_root, "
                "**{f'projects.{name}': path for name, path in settings.projects.items()}}; "
                "missing = [f'{name}={path}' for name, path in paths.items() "
                "if not path.is_dir()]; "
                "missing and sys.exit('configured directories do not exist: ' "
                "+ ', '.join(missing)); "
                "deletion_roots = [Path(value).resolve() for value in sys.argv[2:]]; "
                "persistent = {'instance.dataDir': settings.data_dir, **paths}; "
                "unsafe = [f'{name}={path}' for name, path in persistent.items() "
                "if any(path.resolve() == root or path.resolve().is_relative_to(root) "
                "for root in deletion_roots)]; "
                "unsafe and sys.exit('configured persistent path is inside an uninstall target: ' "
                "+ ', '.join(unsafe)); "
                "print(json.dumps({'dataDir': str(resolved_data_dir), "
                "'adminWeb': {'enabled': settings.admin_web.enabled, "
                "'host': settings.admin_web.host, 'port': settings.admin_web.port}}))"
            ),
            layout.config_file,
            layout.releases,
            layout.cache_dir,
        ],
        cwd=release.root,
        env=runtime_environment,
        capture_output=True,
    )
    try:
        payload = json.loads(configuration.stdout.strip().splitlines()[-1])
        data_dir = Path(payload["dataDir"])
        admin_payload = payload["adminWeb"]
        admin_bind = AdminBind(
            enabled=admin_payload["enabled"],
            host=admin_payload["host"],
            port=admin_payload["port"],
        )
    except (IndexError, KeyError, TypeError, ValueError) as error:
        raise InstallError(
            "candidate configuration validator returned invalid runtime settings"
        ) from error
    if not data_dir.is_absolute():
        raise InstallError(f"candidate returned a non-absolute dataDir: {data_dir}")
    if (
        not isinstance(admin_bind.enabled, bool)
        or not isinstance(admin_bind.host, str)
        or not admin_bind.host
        or isinstance(admin_bind.port, bool)
        or not isinstance(admin_bind.port, int)
        or not 1 <= admin_bind.port <= 65535
    ):
        raise InstallError("candidate returned an invalid Admin Web bind")
    execute(
        [
            release.venv / "bin" / "python",
            "-E",
            "-B",
            "-c",
            (
                "import os; from codex_cli_bin import bundled_codex_path; "
                "path = os.fspath(bundled_codex_path()); "
                "os.execv(path, [path, 'login', 'status'])"
            ),
        ],
        cwd=layout.home,
        env=runtime_environment,
    )
    return RuntimeValidation(data_dir=data_dir, admin_bind=admin_bind)


def preflight_admin_bind(binding: AdminBind) -> None:
    """Best-effort collision check while holding every successful address."""

    if not binding.enabled:
        return
    try:
        addresses = socket.getaddrinfo(
            binding.host,
            binding.port,
            type=socket.SOCK_STREAM,
            flags=socket.AI_PASSIVE,
        )
    except OSError as error:
        raise InstallError(
            f"could not resolve Admin Web bind {binding.host}:{binding.port}: {error}"
        ) from error
    held: list[socket.socket] = []
    unavailable: list[OSError] = []
    seen: set[tuple[int, tuple[object, ...]]] = set()
    try:
        for family, socktype, protocol, _canonical, sockaddr in addresses:
            normalized = tuple(sockaddr)
            key = (family, normalized)
            if key in seen:
                continue
            seen.add(key)
            candidate = socket.socket(family, socktype, protocol)
            try:
                candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if family == socket.AF_INET6:
                    candidate.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                candidate.bind(sockaddr)
            except OSError as error:
                candidate.close()
                if error.errno == errno.EADDRNOTAVAIL:
                    unavailable.append(error)
                    continue
                if error.errno == errno.EADDRINUSE:
                    raise InstallError(
                        "Admin Web address is already in use: "
                        f"{binding.host}:{binding.port}"
                    ) from error
                raise InstallError(
                    "could not preflight Admin Web bind "
                    f"{binding.host}:{binding.port}: {error}"
                ) from error
            held.append(candidate)
        if not held:
            detail = unavailable[-1] if unavailable else "no bindable addresses"
            raise InstallError(
                "could not preflight Admin Web bind "
                f"{binding.host}:{binding.port}: {detail}"
            )
    finally:
        for candidate in held:
            candidate.close()


def render_systemd_service(release: Release, layout: Layout) -> str:
    template_path = release.source / "deploy" / "netizen.service"
    try:
        template = template_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise InstallError(f"could not read systemd template: {template_path}") from error
    values = {
        "@HOME_ENV@": _systemd_quote(f"HOME={layout.home}"),
        "@CODEX_HOME_ENV@": _systemd_quote(f"CODEX_HOME={layout.codex_home}"),
        "@PATH_ENV@": _systemd_quote(
            f"PATH={_service_bootstrap_path(layout)}"
        ),
        "@CONFIG_ENV@": _systemd_quote(f"NETIZEN_CONFIG_PATH={layout.config_file}"),
        "@SECRET_ENV@": _systemd_quote(
            f"FEISHU_APP_SECRET_FILE={layout.secret_file}"
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
        "@EXEC_START@": " ".join(
            (
                _systemd_quote(str(layout.current / "venv" / "bin" / "python")),
                "-E",
                "-B",
                "-u",
                _systemd_quote(
                    str(
                        layout.current
                        / "source"
                        / "scripts"
                        / "netizen_service_launcher.py"
                    )
                ),
            )
        ),
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
        SYSTEMD_SERVICE_NAME,
        runner=runner,
        check=False,
        capture_output=True,
    )
    enabled_result = systemctl_user(
        layout,
        "is-enabled",
        SYSTEMD_SERVICE_NAME,
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


def inspect_legacy_service(runner: Runner | None = None) -> LegacyServiceState:
    execute = run_command if runner is None else runner
    legacy_path = Path("/etc/systemd/system") / SYSTEMD_SERVICE_NAME
    if not _path_exists(legacy_path):
        return LegacyServiceState()
    recognized = False
    if legacy_path.is_file() and not legacy_path.is_symlink():
        with contextlib.suppress(OSError, UnicodeError):
            recognized = "Netizen Feishu Codex channel" in legacy_path.read_text(
                encoding="utf-8"
            )
    active_result = execute(
        ["systemctl", "is-active", SYSTEMD_SERVICE_NAME],
        check=False,
        capture_output=True,
        env=_clean_subprocess_environment(),
    )
    enabled_result = execute(
        ["systemctl", "is-enabled", SYSTEMD_SERVICE_NAME],
        check=False,
        capture_output=True,
        env=_clean_subprocess_environment(),
    )
    for label, result in (
        ("active state", active_result),
        ("enable state", enabled_result),
    ):
        if result.returncode != 0 and not result.stdout.strip() and result.stderr.strip():
            raise InstallError(
                f"could not query legacy system service {label}: {result.stderr.strip()}"
            )
    return LegacyServiceState(
        present=True,
        recognized=recognized,
        active=active_result.stdout.strip() in RUNNING_UNIT_STATES,
        enabled=enabled_result.stdout.strip() in {"enabled", "enabled-runtime"},
    )


def disable_legacy_service(
    state: LegacyServiceState,
    *,
    layout: Layout,
    interactive: bool,
    runner: Runner,
) -> None:
    if not state.present or not (state.active or state.enabled):
        return
    if not state.recognized:
        raise InstallError(
            f"an unrecognized system-level {SYSTEMD_SERVICE_NAME} is active or enabled; disable it manually"
        )
    command = ["systemctl", "disable", "--now", SYSTEMD_SERVICE_NAME]
    if layout.uid != 0:
        if not interactive:
            raise InstallError(
                "legacy system service migration needs one-time authorization; run "
                f"sudo systemctl disable --now {SYSTEMD_SERVICE_NAME}, then rerun the installer"
            )
        command.insert(0, "sudo")
    info("disabling the recognized legacy system-level Netizen service")
    runner(command, env=_clean_subprocess_environment())


def restore_legacy_service(
    state: LegacyServiceState,
    *,
    layout: Layout,
    interactive: bool,
    runner: Runner,
) -> None:
    if not state.present or not (state.active or state.enabled):
        return
    commands: list[list[str]] = []
    if state.enabled:
        commands.append(["systemctl", "enable", SYSTEMD_SERVICE_NAME])
    if state.active:
        commands.append(["systemctl", "start", SYSTEMD_SERVICE_NAME])
    for command in commands:
        if layout.uid != 0:
            if not interactive:
                raise InstallError(
                    "automatic rollback needs authorization to restore the legacy service"
                )
            command.insert(0, "sudo")
        runner(command, env=_clean_subprocess_environment())


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


@contextlib.contextmanager
def _hold_service_lifetime_lock(layout: Layout) -> Iterator[None]:
    """Exclude service startup while rollback-protected state is migrated."""

    path = layout.lifetime_lock_file
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise InstallError(
            f"could not open service lifetime lock {path}: {error}"
        ) from error
    locked = False
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != layout.uid:
            raise InstallError(
                f"service lifetime lock is not a current-user regular file: {path}"
            )
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise InstallError(
                "service lifetime lock is still held; refusing to migrate "
                "the Channel database"
            ) from error
        locked = True
        yield
    except OSError as error:
        raise InstallError(
            f"could not hold service lifetime lock {path}: {error}"
        ) from error
    finally:
        if locked:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
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


def _log_excerpt(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return " | ".join(lines[-5:])


class SystemdServiceBackend:
    def __init__(self, layout: Layout, runner: Runner) -> None:
        self.layout = layout
        self._runner = runner
        self._known_stopped = False
        self._ready_marker_required = True

    def preflight(self) -> None:
        _user_service_state(self.layout, self._runner)

    def prepare_host(self, *, interactive: bool) -> None:
        ensure_linger(self.layout, interactive=interactive, runner=self._runner)

    def inspect_state(self) -> ServiceState:
        active, enabled = _user_service_state(self.layout, self._runner)
        return ServiceState(loaded=active, enabled=enabled)

    def capture_definition(self) -> FileSnapshot:
        if _path_exists(self.layout.service_file):
            _require_managed_systemd_service(self.layout.service_file)
        snapshot = _capture_file(
            self.layout.service_file,
            label="managed systemd service",
        )
        if snapshot.existed:
            # Preserve the readiness contract of the definition being captured
            # so a failed upgrade can restart a pre-marker release safely.
            self._ready_marker_required = (
                SYSTEMD_READY_ENVIRONMENT_TOKEN in snapshot.content
            )
        return snapshot

    def render_definition(self, release: Release) -> bytes:
        return render_systemd_service(release, self.layout).encode()

    def _is_loaded(self) -> bool:
        result = systemctl_user(
            self.layout,
            "is-active",
            SYSTEMD_SERVICE_NAME,
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
        systemctl_user(
            self.layout,
            "stop",
            SYSTEMD_SERVICE_NAME,
            runner=self._runner,
        )
        # systemctl stop is itself a synchronous manager transition.  The
        # lifetime lock independently proves that the Python process released
        # rollback-protected state; unlike launchd, no second manager poll is
        # needed here.
        _wait_for_stop_confirmation(
            self.layout,
            is_loaded=lambda: False,
            timeout=timeout,
        )
        self._known_stopped = True

    def publish_definition(self, content: bytes, *, should_enable: bool) -> None:
        _write_atomic(self.layout.service_file, content, mode=0o600)
        self._ready_marker_required = True
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
            SYSTEMD_SERVICE_NAME,
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
        self._ready_marker_required = (
            not snapshot.existed
            or SYSTEMD_READY_ENVIRONMENT_TOKEN in snapshot.content
        )
        systemctl_user(self.layout, "daemon-reload", runner=self._runner)
        systemctl_user(
            self.layout,
            "enable" if should_enable else "disable",
            SYSTEMD_SERVICE_NAME,
            runner=self._runner,
            check=should_enable,
        )

    def start_and_wait(self, *, timeout: float) -> None:
        loaded = False if self._known_stopped else self._is_loaded()
        if loaded:
            if not self._ready_marker_required or _ready_marker_present(self.layout):
                return
        started_at = time.time()
        if not loaded:
            _clear_ready_marker(self.layout)
            self._known_stopped = False
            systemctl_user(
                self.layout,
                "start",
                SYSTEMD_SERVICE_NAME,
                runner=self._runner,
            )
        if self._ready_marker_required:
            _wait_for_systemd_ready(self.layout, timeout=timeout, runner=self._runner)
        else:
            _wait_for_legacy_systemd_ready(
                self.layout,
                since=started_at,
                timeout=timeout,
                runner=self._runner,
            )

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
            SYSTEMD_SERVICE_NAME,
            runner=self._runner,
            check=False,
        )
        return result.returncode

    def uninstall_definition(self) -> None:
        if _path_exists(self.layout.service_file):
            _require_managed_systemd_service(self.layout.service_file)
            self.stop_and_confirm()
            systemctl_user(
                self.layout,
                "disable",
                SYSTEMD_SERVICE_NAME,
                runner=self._runner,
                check=False,
            )
            self.layout.service_file.unlink()
            systemctl_user(self.layout, "daemon-reload", runner=self._runner)
            systemctl_user(
                self.layout,
                "reset-failed",
                SYSTEMD_SERVICE_NAME,
                runner=self._runner,
                check=False,
            )
            return
        state = self.inspect_state()
        if state.loaded or state.enabled:
            raise InstallError(
                "the managed user service file is missing but systemd still has an "
                "active/enabled netizen.service; inspect it before uninstalling"
            )
        systemctl_user(self.layout, "daemon-reload", runner=self._runner)

    def inspect_legacy(self) -> LegacyServiceState:
        return inspect_legacy_service(self._runner)

    def disable_legacy(
        self,
        state: LegacyServiceState,
        *,
        interactive: bool,
    ) -> None:
        disable_legacy_service(
            state,
            layout=self.layout,
            interactive=interactive,
            runner=self._runner,
        )

    def restore_legacy(
        self,
        state: LegacyServiceState,
        *,
        interactive: bool,
    ) -> None:
        restore_legacy_service(
            state,
            layout=self.layout,
            interactive=interactive,
            runner=self._runner,
        )


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


def _service_backend(
    layout: Layout,
    runner: Runner | None = None,
) -> ServiceBackend:
    execute = run_command if runner is None else runner
    if layout.platform == "linux":
        return SystemdServiceBackend(layout, execute)
    if layout.platform == "darwin":
        return LaunchAgentServiceBackend(layout, execute)
    raise InstallError(f"unsupported service backend: {layout.platform}")


def _read_activation_intent(layout: Layout) -> ActivationIntent | None:
    path = layout.state_dir / ACTIVATION_INTENT
    if not _path_exists(path):
        return None
    _require_regular_file(path, "activation intent")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InstallError(f"activation intent is unreadable: {path}: {error}") from error
    if not isinstance(payload, dict) or set(payload) != {
        "version",
        "release",
        "priorRelease",
        "shouldStart",
        "shouldEnable",
    }:
        raise InstallError(f"activation intent has an invalid shape: {path}")
    release = payload["release"]
    prior_release = payload["priorRelease"]
    should_start = payload["shouldStart"]
    should_enable = payload["shouldEnable"]
    if (
        not isinstance(payload["version"], int)
        or isinstance(payload["version"], bool)
        or payload["version"] != 1
        or not isinstance(release, str)
        or RELEASE_NAME.fullmatch(release) is None
        or (
            prior_release is not None
            and (
                not isinstance(prior_release, str)
                or RELEASE_NAME.fullmatch(prior_release) is None
            )
        )
        or not isinstance(should_start, bool)
        or not isinstance(should_enable, bool)
    ):
        raise InstallError(f"activation intent has invalid values: {path}")
    return ActivationIntent(
        release=release,
        prior_release=prior_release,
        should_start=should_start,
        should_enable=should_enable,
    )


def _write_activation_intent(
    layout: Layout,
    release: Release,
    *,
    should_start: bool,
    should_enable: bool,
    prior_release: Path | None = None,
) -> None:
    if RELEASE_NAME.fullmatch(release.digest) is None:
        raise InstallError(f"activation intent has an invalid release digest: {release.digest}")
    prior_digest: str | None = None
    if prior_release is not None:
        resolved_prior = prior_release.resolve(strict=True)
        if (
            prior_release.is_symlink()
            or resolved_prior.parent != layout.releases.resolve()
            or RELEASE_NAME.fullmatch(resolved_prior.name) is None
        ):
            raise InstallError(
                f"activation intent has an unmanaged prior release: {prior_release}"
            )
        prior_digest = resolved_prior.name
    payload = {
        "version": 1,
        "release": release.digest,
        "priorRelease": prior_digest,
        "shouldStart": should_start,
        "shouldEnable": should_enable,
    }
    _write_atomic(
        layout.state_dir / ACTIVATION_INTENT,
        (json.dumps(payload, sort_keys=True) + "\n").encode(),
        mode=0o600,
    )


def _clear_activation_intent(layout: Layout) -> None:
    path = layout.state_dir / ACTIVATION_INTENT
    if not _path_exists(path):
        return
    _require_regular_file(path, "activation intent")
    try:
        path.unlink()
    except OSError as error:
        raise InstallError(f"could not clear activation intent {path}: {error}") from error


def _intent_prior_release(
    layout: Layout,
    intent: ActivationIntent,
) -> Path | None:
    if intent.prior_release is None:
        return None
    path = layout.releases / intent.prior_release
    if path.is_symlink() or not path.is_dir():
        raise InstallError(
            "activation intent's prior release is unavailable: "
            f"{intent.prior_release}"
        )
    resolved = path.resolve()
    if resolved.parent != layout.releases.resolve():
        raise InstallError(
            f"activation intent's prior release is unmanaged: {resolved}"
        )
    return resolved


def activate_release(
    release: Release,
    layout: Layout,
    *,
    interactive: bool,
    runner: Runner | None = None,
    ready_timeout: float = SERVICE_READY_TIMEOUT_SECONDS,
    data_dir: Path | None = None,
    admin_bind: AdminBind | None = None,
) -> None:
    execute = run_command if runner is None else runner
    backend = _service_backend(layout, execute)
    old_current = _read_release_link(layout.current, layout)
    old_previous = _read_release_link(layout.previous, layout)
    old_definition = backend.capture_definition()
    old_state = backend.inspect_state()
    if not old_definition.existed and (old_state.loaded or old_state.enabled):
        raise InstallError(
            "the managed service definition is missing but its service-manager "
            "target is still loaded/enabled; inspect it before installing"
        )
    legacy = backend.inspect_legacy()
    pending_intent = _read_activation_intent(layout)
    if pending_intent is None:
        intended_prior_release = old_current
        should_start = old_current is None or old_state.loaded or legacy.active
        should_enable = old_current is None or old_state.enabled or legacy.enabled
    else:
        intended_prior_release = _intent_prior_release(layout, pending_intent)
        should_start = pending_intent.should_start
        should_enable = pending_intent.should_enable
        info(
            "recovering interrupted activation intent from release "
            f"{pending_intent.release[:12]}"
        )
    definition = backend.render_definition(release)

    with tempfile.TemporaryDirectory(prefix=".rollback-", dir=layout.state_dir) as temp:
        skill_snapshot = _capture_skill(layout, Path(temp))
        database_snapshot: DatabaseSnapshot | None = None
        changed_service = False
        definition_publish_attempted = False
        legacy_disabled = bool(
            legacy.present
            and legacy.recognized
            and (legacy.active or legacy.enabled)
            and (layout.uid == 0 or interactive)
        )
        _write_activation_intent(
            layout,
            release,
            should_start=should_start,
            should_enable=should_enable,
            prior_release=intended_prior_release,
        )
        try:
            backend.disable_legacy(
                legacy,
                interactive=interactive,
            )
            if old_state.loaded:
                changed_service = True
                backend.stop_and_confirm()

            if admin_bind is not None:
                preflight_admin_bind(admin_bind)

            channel_data_dir = layout.state_dir if data_dir is None else data_dir
            channel_database = channel_data_dir / "channel.sqlite3"
            if _path_exists(channel_database):
                # The old service is already confirmed stopped above. Holding
                # its stable lifetime lock closes the race with an external
                # service start while the rollback snapshot and one-step
                # schema migration are in progress.
                with _hold_service_lifetime_lock(layout):
                    database_snapshot = _capture_database(
                        channel_data_dir,
                        Path(temp),
                    )
                    _set_release_link(layout.current, release.root, layout)
                    if (
                        _has_sqlite_database_header(channel_database)
                        and migrate_channel_database_v5_to_v6(channel_database)
                    ):
                        info(
                            "migrated Channel database from schema v5 to v6 "
                            "with existing Bindings and Side Topic tombstones"
                        )
            else:
                if should_start:
                    database_snapshot = _capture_database(
                        channel_data_dir,
                        Path(temp),
                    )
                _set_release_link(layout.current, release.root, layout)
            install_user_guide_skill(
                source_skill=release.source / "skills" / SKILL_NAME,
                codex_home=layout.codex_home,
            )
            definition_publish_attempted = True
            backend.publish_definition(
                definition,
                should_enable=should_enable,
            )
            if should_start:
                # A failed start request can still have created a process.
                changed_service = True
                backend.start_and_wait(timeout=ready_timeout)

            if pending_intent is not None:
                if intended_prior_release is None:
                    _set_release_link(layout.previous, None, layout)
                elif intended_prior_release != release.root.resolve():
                    _set_release_link(
                        layout.previous,
                        intended_prior_release,
                        layout,
                    )
            elif old_current is not None and old_current != release.root.resolve():
                _set_release_link(layout.previous, old_current, layout)
            elif old_current is None and old_previous is None:
                _set_release_link(layout.previous, None, layout)
            _clear_activation_intent(layout)
        except BaseException as error:
            rollback_errors: list[str] = []
            preserve_snapshot = False
            candidate_stopped = True
            if changed_service:
                try:
                    backend.stop_and_confirm()
                except BaseException as rollback_error:
                    candidate_stopped = False
                    preserve_snapshot = True
                    rollback_errors.append(f"stop candidate: {rollback_error}")
            rollback_safe_to_start = candidate_stopped
            rollback_actions: list[tuple[str, Callable[[], None]]] = [
                (
                    "current release",
                    lambda: _set_release_link(layout.current, old_current, layout),
                ),
            ]
            if definition_publish_attempted:
                rollback_actions.append(
                    (
                        "service definition",
                        lambda: backend.restore_definition(
                            old_definition,
                            should_enable=old_state.enabled,
                        ),
                    )
                )
            rollback_actions.extend(
                (
                    (
                        "database",
                        lambda: _restore_database(database_snapshot),
                    ),
                    ("Skill", lambda: _restore_skill(layout, skill_snapshot)),
                )
            )
            for label, action in rollback_actions:
                if label in {"database", "Skill"} and not candidate_stopped:
                    preserve_snapshot = True
                    rollback_safe_to_start = False
                    rollback_errors.append(
                        f"restore {label}: skipped because candidate stop was not confirmed"
                    )
                    continue
                try:
                    action()
                except BaseException as rollback_error:
                    rollback_safe_to_start = False
                    rollback_errors.append(f"restore {label}: {rollback_error}")
                    preserve_snapshot = preserve_snapshot or label in {
                        "database",
                        "Skill",
                    }
            if preserve_snapshot:
                recovery = layout.state_dir / f"rollback-recovery-{uuid.uuid4().hex}"
                try:
                    os.replace(temp, recovery)
                    rollback_errors.append(
                        f"rollback recovery snapshot preserved at {recovery}"
                    )
                except OSError as preserve_error:
                    rollback_errors.append(
                        "rollback recovery snapshot could not be preserved: "
                        f"{preserve_error}"
                    )
            try:
                if old_state.loaded and rollback_safe_to_start:
                    backend.start_and_wait(timeout=ready_timeout)
                elif old_state.loaded:
                    rollback_errors.append(
                        "restore user service: skipped because rollback state is incomplete"
                    )
            except BaseException as rollback_error:
                rollback_errors.append(f"restore user service: {rollback_error}")
            if legacy_disabled and rollback_safe_to_start:
                try:
                    backend.restore_legacy(
                        legacy,
                        interactive=interactive,
                    )
                except BaseException as rollback_error:
                    rollback_errors.append(f"restore legacy service: {rollback_error}")
            elif legacy_disabled:
                rollback_errors.append(
                    "restore legacy service: skipped because rollback state is incomplete"
                )
            if pending_intent is None and not rollback_errors:
                try:
                    _clear_activation_intent(layout)
                except BaseException as rollback_error:
                    rollback_errors.append(
                        f"clear activation intent: {rollback_error}"
                    )
            detail = f"; rollback issues: {'; '.join(rollback_errors)}" if rollback_errors else ""
            raise InstallError(
                f"activation failed and was rolled back: {error}{detail}"
            ) from error
    try:
        _prune_releases(layout)
    except (InstallError, OSError) as error:
        info(f"warning: release activated but obsolete release cleanup failed: {error}")


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
            SYSTEMD_SERVICE_NAME,
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
                SYSTEMD_SERVICE_NAME,
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
        f"{SYSTEMD_SERVICE_NAME} did not become ready within {timeout:g}s{suffix}"
    )


def _wait_for_legacy_systemd_ready(
    layout: Layout,
    *,
    since: float,
    timeout: float,
    runner: Runner,
) -> None:
    """Wait for a pre-ready-marker release during failed-upgrade rollback."""

    deadline = time.monotonic() + timeout
    last_journal = ""
    while time.monotonic() < deadline:
        active = systemctl_user(
            layout,
            "is-active",
            SYSTEMD_SERVICE_NAME,
            runner=runner,
            check=False,
            capture_output=True,
        )
        if active.stdout.strip() == "failed":
            break
        journal = runner(
            [
                "journalctl",
                "--user",
                "--unit",
                SYSTEMD_SERVICE_NAME,
                "--since",
                f"@{since:.6f}",
                "--output=cat",
                "--no-pager",
            ],
            check=False,
            capture_output=True,
            env=_service_environment(layout),
        )
        last_journal = journal.stdout
        if (
            LEGACY_SYSTEMD_READY_LOG in last_journal
            and active.stdout.strip() == "active"
        ):
            return
        time.sleep(0.5)
    excerpt = " | ".join(line for line in last_journal.strip().splitlines()[-5:])
    suffix = f"; recent journal: {excerpt}" if excerpt else ""
    raise InstallError(
        f"legacy {SYSTEMD_SERVICE_NAME} did not become ready within {timeout:g}s{suffix}"
    )


def _capture_database(data_dir: Path, temporary_root: Path) -> DatabaseSnapshot:
    if data_dir.is_symlink() and not data_dir.exists():
        raise InstallError(f"configured dataDir is a broken symlink: {data_dir}")
    if data_dir.exists() and not data_dir.is_dir():
        raise InstallError(f"configured dataDir is not a directory: {data_dir}")
    saved_root = temporary_root / "database"
    existing: list[str] = []
    for name in CHANNEL_DATABASE_FILES:
        source = data_dir / name
        if not _path_exists(source):
            continue
        _require_regular_file(source, "Channel database")
        saved_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        saved = saved_root / name
        shutil.copy2(source, saved, follow_symlinks=False)
        if _stream_digest(source) != _stream_digest(saved):
            raise InstallError(f"Channel database rollback copy differs: {source}")
        existing.append(name)
    if existing:
        info("captured Channel database rollback snapshot")
    return DatabaseSnapshot(
        data_dir=data_dir,
        saved_root=saved_root,
        existing_files=tuple(existing),
    )


def _has_sqlite_database_header(path: Path) -> bool:
    _require_regular_file(path, "Channel database")
    try:
        with path.open("rb") as database:
            return database.read(len(SQLITE_DATABASE_HEADER)) == SQLITE_DATABASE_HEADER
    except OSError as error:
        raise InstallError(f"could not inspect Channel database {path}: {error}") from error


def _restore_database(snapshot: DatabaseSnapshot | None) -> None:
    if snapshot is None:
        return
    data_dir = snapshot.data_dir
    if data_dir.is_symlink() and not data_dir.exists():
        raise InstallError(f"configured dataDir became a broken symlink: {data_dir}")
    if data_dir.exists() and not data_dir.is_dir():
        raise InstallError(f"configured dataDir is no longer a directory: {data_dir}")
    if not data_dir.exists():
        data_dir.mkdir(mode=0o700, parents=True)
    targets = [data_dir / name for name in CHANNEL_DATABASE_FILES]
    for target in targets:
        if target.is_dir() and not target.is_symlink():
            raise InstallError(f"database rollback target became a directory: {target}")
    for target in targets:
        if _path_exists(target):
            target.unlink()
    for name in snapshot.existing_files:
        saved = snapshot.saved_root / name
        _require_regular_file(saved, "saved Channel database")
        restored = data_dir / name
        shutil.copy2(saved, restored, follow_symlinks=False)
        if _stream_digest(saved) != _stream_digest(restored):
            raise InstallError(f"restored Channel database differs: {restored}")


def _stream_digest(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.digest()


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


def _capture_skill(layout: Layout, temporary_root: Path) -> SkillSnapshot:
    target = layout.codex_home / "skills" / SKILL_NAME
    if not _path_exists(target):
        return SkillSnapshot(kind="absent")
    if target.is_symlink():
        return SkillSnapshot(kind="symlink", link_target=os.readlink(target))
    saved = temporary_root / SKILL_NAME
    if target.is_dir():
        shutil.copytree(target, saved, symlinks=True)
        return SkillSnapshot(kind="directory", saved_path=saved)
    if target.is_file():
        shutil.copy2(target, saved, follow_symlinks=False)
        return SkillSnapshot(kind="file", saved_path=saved)
    raise InstallError(f"managed Skill has an unsupported filesystem type: {target}")


def _restore_skill(layout: Layout, snapshot: SkillSnapshot) -> None:
    skills_root = layout.codex_home / "skills"
    target = skills_root / SKILL_NAME
    if _path_exists(target):
        _remove_path(target)
    if snapshot.kind == "absent":
        return
    _ensure_real_directory(skills_root, mode=0o700, enforce_mode=False)
    if snapshot.kind == "symlink":
        assert snapshot.link_target is not None
        os.symlink(snapshot.link_target, target)
    elif snapshot.kind == "directory":
        assert snapshot.saved_path is not None
        shutil.copytree(snapshot.saved_path, target, symlinks=True)
    elif snapshot.kind == "file":
        assert snapshot.saved_path is not None
        shutil.copy2(snapshot.saved_path, target, follow_symlinks=False)
    else:  # pragma: no cover - internal invariant
        raise InstallError(f"unknown Skill snapshot kind: {snapshot.kind}")


def _read_release_link(link: Path, layout: Layout) -> Path | None:
    if not _path_exists(link):
        return None
    if not link.is_symlink():
        raise InstallError(f"managed release pointer must be a symlink: {link}")
    raw_target = Path(os.readlink(link))
    entry = raw_target if raw_target.is_absolute() else link.parent / raw_target
    releases = layout.releases.resolve()
    if entry.parent.resolve() != releases or not RELEASE_NAME.fullmatch(entry.name):
        raise InstallError(f"managed release pointer escapes the release directory: {link}")
    if entry.is_symlink():
        raise InstallError(f"managed release pointer targets a symlinked release: {link}")
    if not entry.is_dir():
        raise InstallError(f"managed release pointer is broken: {link}")
    return entry.resolve()


def _set_release_link(link: Path, target: Path | None, layout: Layout) -> None:
    if target is None:
        if _path_exists(link):
            if not link.is_symlink():
                raise InstallError(f"managed release pointer is not a symlink: {link}")
            link.unlink()
        return
    resolved_target = target.resolve(strict=True)
    if (
        target.is_symlink()
        or resolved_target.parent != layout.releases.resolve()
        or not RELEASE_NAME.fullmatch(resolved_target.name)
    ):
        raise InstallError(f"refusing to point at an unmanaged release: {target}")
    temporary = link.parent / f".{link.name}.{uuid.uuid4().hex}"
    try:
        os.symlink(os.path.relpath(resolved_target, link.parent.resolve()), temporary)
        os.replace(temporary, link)
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink()


def _prune_releases(layout: Layout) -> None:
    keep = {
        target
        for link in (layout.current, layout.previous)
        if (target := _read_release_link(link, layout))
    }
    for path in layout.releases.iterdir():
        if path.resolve() in keep or not RELEASE_NAME.fullmatch(path.name):
            continue
        _remove_managed_release(path, layout)


def install_source(
    *,
    source_root: Path = REPOSITORY_ROOT,
    layout: Layout | None = None,
    runner: Runner | None = None,
    interactive: bool | None = None,
) -> Release:
    return _install(
        source_root=source_root,
        prepare_candidate=prepare_source_release,
        rerun_instruction="rerun ./dev-install.sh",
        layout=layout,
        runner=runner,
        interactive=interactive,
    )


def install_published(
    *,
    source_root: Path,
    layout: Layout | None = None,
    runner: Runner | None = None,
    interactive: bool | None = None,
) -> Release:
    manifest = read_published_release_manifest(source_root)

    def prepare_candidate(
        candidate_layout: Layout,
        *,
        source_root: Path,
        runner: Runner,
    ) -> Release:
        return prepare_published_release(
            candidate_layout,
            source_root=source_root,
            manifest=manifest,
            runner=runner,
        )

    return _install(
        source_root=source_root,
        prepare_candidate=prepare_candidate,
        rerun_instruction=(
            f"rerun the official Netizen v{manifest.version} installer from "
            f"{OFFICIAL_RELEASE_DOWNLOADS}/v{manifest.version}/install.sh"
        ),
        layout=layout,
        runner=runner,
        interactive=interactive,
    )


def _install(
    *,
    source_root: Path,
    prepare_candidate: CandidatePreparer,
    rerun_instruction: str,
    layout: Layout | None,
    runner: Runner | None,
    interactive: bool | None,
) -> Release:
    selected_layout = resolve_layout() if layout is None else layout
    require_supported_platform(
        selected_layout.platform,
        require_definition_validation=True,
    )
    execute = run_command if runner is None else runner
    backend = _service_backend(selected_layout, execute)
    is_interactive = sys.stdin.isatty() if interactive is None else interactive
    _validate_source_location(source_root, selected_layout)
    backend.preflight()
    with installation_lock(selected_layout):
        prepare_directories(selected_layout)
        configuration_ready = True
        try:
            prepare_configuration(
                selected_layout,
                interactive=False,
                rerun_instruction=rerun_instruction,
            )
        except ConfigurationRequired:
            configuration_ready = False
            if not is_interactive:
                raise
        if not configuration_ready:
            info(
                "Feishu/Lark credentials are incomplete; interactive setup follows "
                "release preparation. Agent/CI callers should cancel now and rerun "
                "the selected installer with </dev/null."
            )
        release = prepare_candidate(
            selected_layout,
            source_root=source_root,
            runner=execute,
        )
        if not configuration_ready:
            prepare_configuration(
                selected_layout,
                interactive=True,
                rerun_instruction=rerun_instruction,
                app_registrar=lambda app_id: _register_feishu_app_from_release(
                    release,
                    app_id,
                    runner=execute,
                ),
            )
        validation = validate_runtime(release, selected_layout, execute)
        require_feishu_permissions(
            release,
            selected_layout,
            interactive=is_interactive,
            repair_existing_app=configuration_ready,
            rerun_instruction=rerun_instruction,
            runner=execute,
        )
        backend.prepare_host(interactive=is_interactive)
        activate_release(
            release,
            selected_layout,
            interactive=is_interactive,
            runner=execute,
            data_dir=validation.data_dir,
            admin_bind=validation.admin_bind,
        )
    info(f"installed release {release.digest[:12]} at {release.root}")
    info(f"configuration: {selected_layout.config_file}")
    info("service environment: account shell profile (reloaded on every start)")
    info(f"service control: {release.source / 'service.sh'}")
    return release


def service_action(
    action: str,
    *,
    layout: Layout | None = None,
    runner: Runner | None = None,
) -> int:
    if action not in {"start", "stop", "restart", "status"}:
        raise InstallError("service action must be start, stop, restart, or status")
    selected_layout = resolve_layout() if layout is None else layout
    require_supported_platform(selected_layout.platform)
    execute = run_command if runner is None else runner
    backend = _service_backend(selected_layout, execute)
    if (
        not selected_layout.service_file.is_file()
        or selected_layout.service_file.is_symlink()
    ):
        raise InstallError(
            f"Netizen is not installed for this user: {selected_layout.service_file}"
        )
    backend.capture_definition()
    backend.preflight()
    return backend.service_action(action)


def uninstall(
    *,
    layout: Layout | None = None,
    runner: Runner | None = None,
) -> None:
    selected_layout = resolve_layout() if layout is None else layout
    require_supported_platform(selected_layout.platform)
    _validate_layout_safety(selected_layout)
    execute = run_command if runner is None else runner
    backend = _service_backend(selected_layout, execute)
    for path in (selected_layout.releases, selected_layout.cache_dir):
        if _path_exists(path):
            _require_managed_netizen_directory(path, selected_layout)
    for link in (selected_layout.current, selected_layout.previous):
        _read_release_link(link, selected_layout)
    activation_intent = selected_layout.state_dir / ACTIVATION_INTENT
    if _path_exists(activation_intent):
        _read_activation_intent(selected_layout)
    if _path_exists(selected_layout.service_file):
        backend.capture_definition()
    backend.preflight()
    managed_skill = selected_layout.codex_home / "skills" / SKILL_NAME
    with installation_lock(selected_layout):
        if not any(
            _path_exists(path)
            for path in (
                selected_layout.releases,
                selected_layout.cache_dir,
                selected_layout.current,
                selected_layout.previous,
                selected_layout.service_file,
                managed_skill,
                activation_intent,
            )
        ):
            info("Netizen is already uninstalled for this user")
            return
        try:
            if Path.cwd().resolve().is_relative_to(selected_layout.product_root.resolve()):
                os.chdir(selected_layout.home)
        except OSError:
            pass
        for path in (selected_layout.releases, selected_layout.cache_dir):
            if _path_exists(path):
                _require_managed_netizen_directory(path, selected_layout)
        for link in (selected_layout.current, selected_layout.previous):
            _read_release_link(link, selected_layout)
        if _path_exists(activation_intent):
            _read_activation_intent(selected_layout)
        backend.uninstall_definition()
        if selected_layout.codex_home.exists():
            try:
                remove_user_guide_skill(codex_home=selected_layout.codex_home)
            except SkillInstallError as error:
                raise InstallError(str(error)) from error
        _clear_activation_intent(selected_layout)
        for link in (selected_layout.current, selected_layout.previous):
            _set_release_link(link, None, selected_layout)
        _remove_managed_netizen_directory(selected_layout.cache_dir, selected_layout)
        _remove_managed_netizen_directory(selected_layout.releases, selected_layout)
    info("uninstalled Netizen program, user service, and managed user-guide Skill")
    info(
        "preserved configuration and credentials: "
        f"{selected_layout.config_file}, {selected_layout.credentials_dir}"
    )
    info(
        "preserved state and native Codex history: "
        f"{selected_layout.state_dir}, {selected_layout.codex_home}"
    )


def _remove_managed_netizen_directory(path: Path, layout: Layout) -> None:
    if not _path_exists(path):
        return
    _require_managed_netizen_directory(path, layout)
    shutil.rmtree(path)


def _require_managed_netizen_directory(path: Path, layout: Layout) -> None:
    if path.is_symlink() or not path.is_dir():
        raise InstallError(f"managed Netizen path is not a real directory: {path}")
    if path not in (layout.releases, layout.cache_dir):
        raise InstallError(f"refusing to remove an unmanaged directory: {path}")
    marker = path / MANAGED_DIRECTORY_MARKER
    _require_regular_file(marker, "managed directory marker")
    try:
        content = marker.read_bytes()
    except OSError as error:
        raise InstallError(f"could not read managed directory marker {marker}: {error}") from error
    if content != MANAGED_DIRECTORY_MARKER_CONTENT:
        raise InstallError(f"managed directory marker is not recognized: {marker}")


def _require_managed_systemd_service(path: Path) -> None:
    _require_regular_file(path, "managed systemd service")
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise InstallError(f"could not read managed systemd service {path}: {error}") from error
    if SYSTEMD_SERVICE_MARKER not in content:
        raise InstallError(
            f"refusing to operate on an unrecognized systemd user service: {path}"
        )


def _path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Netizen installer internals")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("install-source")
    release = subparsers.add_parser("install-release")
    release.add_argument("source_root", type=Path)
    service = subparsers.add_parser("service")
    service.add_argument("action", choices=("start", "stop", "restart", "status"))
    subparsers.add_parser("uninstall")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "install-source":
            install_source()
            return 0
        if args.command == "install-release":
            install_published(source_root=args.source_root)
            return 0
        if args.command == "service":
            return service_action(args.action)
        uninstall()
        return 0
    except (InstallError, SkillInstallError, OSError) as error:
        print(f"netizen: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("netizen: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
