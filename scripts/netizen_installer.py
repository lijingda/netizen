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
import pwd
import re
import secrets
import shlex
import shutil
import socket
import stat
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
from netizen.instance import (  # noqa: E402
    INSTANCE_ROOT_MARKER,
    INSTANCE_ROOT_MARKER_CONTENT,
    require_instance_root_marker,
    resolve_instance_root,
    systemd_service_name,
    launch_agent_label,
)
from netizen.lark_app import (  # noqa: E402
    LarkAppConfigError,
    LarkAppCredentials,
    encode_lark_app,
    load_lark_app,
)
from netizen.bindings import (  # noqa: E402
    SCHEMA_VERSION,
)
from netizen.database_migrations import (  # noqa: E402
    plan_channel_database,
    migrate_channel_database,
)
from netizen.deployment.activation_recovery import (  # noqa: E402
    ActivationIntent,
    decode_activation_intent,
    Recovery,
    create_recovery,
    load_recovery,
)
from netizen.deployment.update_protocol import (  # noqa: E402
    ENV_ARCHIVE_SHA256,
    ENV_LOCK_FD,
    ENV_OPERATION_ID,
    ENV_VERSION,
    UpdateProtocolError,
    advance_operation,
    install_lock,
    read_operation,
    terminal_phase,
    validate_inherited_lock,
)
from netizen.deployment.update_executor import (  # noqa: E402
    UpdateExecutor,
    UpdateExecutorError,
)
from netizen.deployment.installer_support import (  # noqa: E402
    InstallError,
    Layout,
    Release,
    FileSnapshot,
    Runner,
    info,
    run_command,
    _ensure_real_directory,
    _require_regular_file,
    _write_atomic,
    _path_exists,
    _clean_subprocess_environment,
)
from netizen.deployment.service_backend import (  # noqa: E402
    ServiceBackend,
    ServiceState,
    SERVICE_READY_TIMEOUT_SECONDS,
    _service_environment,
)
from netizen.deployment.systemd import (  # noqa: E402
    SystemdServiceBackend,
)
from netizen.deployment.launchd import (  # noqa: E402
    LaunchAgentServiceBackend,
)


RELEASE_METADATA = ".release.json"
PUBLISHED_RELEASE_MANIFEST = ".netizen-release.json"
PUBLISHED_RELEASE_QUALIFICATION = "github-release"
OFFICIAL_RELEASE_DOWNLOADS = "https://github.com/lijingda/netizen/releases/download"
CODEX_CLI_INSTALL_URL = "https://developers.openai.com/codex/cli/"
CODEX_APP_INSTALL_URL = "https://developers.openai.com/codex/app/"
ACTIVATION_INTENT = ".activation-intent.json"
MANAGED_DIRECTORY_MARKER = ".netizen-managed"
MANAGED_DIRECTORY_MARKER_CONTENT = b"netizen-installer-managed-directory-v1\n"
CHANNEL_DATABASE_FILES = (
    "channel.sqlite3",
    "channel.sqlite3-journal",
    "channel.sqlite3-shm",
    "channel.sqlite3-wal",
)
SOURCE_DIRECTORIES = ("netizen", "scripts", "skills", "extensions", "deploy", "docs", "tests")
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


class ConfigurationRequired(InstallError):
    """A non-interactive install prepared files that the caller must fill."""


class FeishuPermissionsRequired(InstallError):
    """The exact app needs an explicit tenant-permission repair."""


@dataclass(slots=True)
class InstallerUpdate:
    product_root: Path
    operation_id: str
    lock_fd: int
    phase: str = "downloading"

    def report(self, phase: str, code: str = "none") -> None:
        try:
            advance_operation(self.product_root, self.operation_id, phase, code)
        except UpdateProtocolError as error:
            raise InstallError("could not record Admin upgrade result") from error
        self.phase = phase


def _installer_update(layout: Layout, manifest: PublishedReleaseManifest) -> InstallerUpdate | None:
    keys = (ENV_OPERATION_ID, ENV_LOCK_FD, ENV_VERSION, ENV_ARCHIVE_SHA256)
    if not any(key in os.environ for key in keys):
        return None
    try:
        if not all(os.environ.get(key) for key in keys):
            raise UpdateProtocolError("incomplete Admin upgrade handoff")
        descriptor = int(os.environ[ENV_LOCK_FD])
        if descriptor < 3:
            raise UpdateProtocolError("invalid Admin upgrade descriptor")
        validate_inherited_lock(layout.product_root, descriptor)
        operation = read_operation(layout.product_root)
        if (
            operation is None
            or operation.get("kind") == "restart"
            or operation["operationId"] != os.environ[ENV_OPERATION_ID]
            or operation["phase"] != "downloading"
            or operation["target"]["version"] != manifest.version
            or os.environ[ENV_VERSION] != manifest.version
            or operation["target"]["archiveSha256"] != os.environ[ENV_ARCHIVE_SHA256]
        ):
            raise UpdateProtocolError("Admin upgrade target changed")
        update = InstallerUpdate(layout.product_root, operation["operationId"], descriptor)
        for key in keys:
            os.environ.pop(key, None)
        return update
    except (ValueError, OSError, UpdateProtocolError) as error:
        raise InstallError("invalid Admin upgrade handoff") from error


@contextlib.contextmanager
def _report_install_update(update: InstallerUpdate | None) -> Iterator[None]:
    try:
        if update is not None:
            update.report("preparing")
        yield
    except BaseException as error:
        if update is not None and not terminal_phase(update.phase):
            if isinstance(error, ConfigurationRequired):
                phase, code = "requires_action", "configuration_required"
            elif isinstance(error, FeishuPermissionsRequired):
                phase, code = "requires_action", "permissions_required"
            elif update.phase in {"installing", "restarting"}:
                phase, code = "recovery_required", "worker_interrupted"
            else:
                phase, code = "failed", "preparation_failed"
            try:
                update.report(phase, code)
            except InstallError:
                pass  # Preserve the original failure; an unreadable result is unknown.
        raise
    # The worker owns success: the outer official bootstrap must also exit zero
    # before Admin can present the installation as complete.


def _record_manual_update_recovery(layout: Layout) -> None:
    """A successful CLI activation resolves an abandoned update, not its target."""
    try:
        operation = read_operation(layout.product_root)
        if operation is None or (terminal_phase(operation["phase"]) and operation["phase"] != "recovery_required"):
            return
        if _path_exists(layout.state_dir / ACTIVATION_INTENT):
            return
        advance_operation(layout.product_root, operation["operationId"], "recovered", "manual_recovery")
    except (OSError, UpdateProtocolError):
        info("warning: installation succeeded but the prior Admin update result could not be reconciled")


@dataclass(frozen=True, slots=True)
class PublishedReleaseManifest:
    version: str
    commit: str
    source_digest: str
    requirements_digest: str


@dataclass(frozen=True, slots=True)
class DatabaseSnapshot:
    data_dir: Path
    saved_root: Path
    existing_files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AdminBind:
    enabled: bool
    host: str
    port: int | None


@dataclass(frozen=True, slots=True)
class FeishuAppCredentials:
    app_id: str
    app_secret: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class RuntimeValidation:
    data_dir: Path
    admin_bind: AdminBind


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


def resolve_layout(
    *,
    root: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    account_home: Path | None = None,
    uid: int | None = None,
    username: str | None = None,
    platform_name: str | None = None,
) -> Layout:
    """Resolve one canonical instance for the effective user, never ``$SUDO_USER``."""

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

    # Platform discovery stays in the effective account's standard location.
    config_home = home / ".config"
    configured_codex_home = env.get("CODEX_HOME", "").strip()
    codex_home = (
        Path(configured_codex_home) if configured_codex_home else home / ".codex"
    )
    if not codex_home.is_absolute() or codex_home == Path(codex_home.anchor):
        raise InstallError(f"CODEX_HOME must be an absolute non-root path: {codex_home}")

    try:
        product_root = resolve_instance_root(root, environ=env, account_home=home)
    except (ValueError, OSError, RuntimeError) as error:
        raise InstallError(str(error)) from error
    if product_root == codex_home.resolve():
        raise InstallError("NETIZEN_ROOT must not be CODEX_HOME")
    credentials_dir = product_root / "credentials"
    selected_platform = _supported_platform_name(platform_name)
    if selected_platform == "linux":
        service_dir = config_home / "systemd" / "user"
        service_file = service_dir / systemd_service_name(product_root)
        service_error_log = product_root / "state" / "service.stderr.log"
    else:
        service_dir = home / "Library" / "LaunchAgents"
        service_file = service_dir / f"{launch_agent_label(product_root)}.plist"
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
        layout.lark_app_file.parent,
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
    if source == layout.product_root or layout.product_root.is_relative_to(source):
        raise InstallError(f"install root must not be inside the release source: {source}")
    for managed in (
        layout.releases, layout.cache_dir, layout.state_dir,
        layout.credentials_dir, layout.lark_app_file.parent, layout.codex_home,
    ):
        if not _paths_overlap(source, managed):
            continue
        if managed == layout.releases and managed_release_source:
            continue
        raise InstallError(
            f"release source overlaps a managed install/data path: {source}, {managed}"
        )


def require_supported_platform(
    platform_name: str | None = None,
    *,
    require_definition_validation: bool = False,
) -> str:
    selected = _supported_platform_name(platform_name)
    if sys.version_info[:2] not in {(3, 11), (3, 12), (3, 13), (3, 14)}:
        raise InstallError("Python 3.11, 3.12, 3.13, or 3.14 is required")
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


def require_codex_login(
    release: Release,
    layout: Layout,
    *,
    rerun_instruction: str,
    runner: Runner | None = None,
) -> None:
    """Require a valid login visible to the candidate Codex runtime."""

    execute = run_command if runner is None else runner
    environment = _service_environment(layout)
    identity = (
        f"account {layout.username} with HOME {layout.home} "
        f"and CODEX_HOME {layout.codex_home}"
    )
    try:
        result = execute(
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
            check=False,
            capture_output=True,
            cwd=layout.home,
            env=environment,
            timeout=30.0,
        )
    except InstallError as error:
        raise InstallError(
            f"The candidate Codex runtime could not check the login state for "
            f"{identity}; {rerun_instruction}."
        ) from error
    if result.returncode != 0:
        raise InstallError(
            f"No valid Codex login is available to the Netizen runtime for {identity}. "
            f"Install and sign in with either Codex CLI ({CODEX_CLI_INSTALL_URL}) "
            f"or Codex App ({CODEX_APP_INSTALL_URL}), ensure that login is available "
            f"to this account and CODEX_HOME, then {rerun_instruction}."
        )


def prepare_directories(layout: Layout) -> None:
    _claim_instance_root(layout)
    _ensure_managed_netizen_directory(layout.releases)
    for path in (
        layout.credentials_dir,
        layout.lark_app_file.parent,
        layout.state_dir,
    ):
        _ensure_real_directory(path, mode=0o700)
    _ensure_managed_netizen_directory(layout.cache_dir)
    _ensure_real_directory(layout.service_dir, mode=0o700, enforce_mode=False)


def _validate_root_marker(layout: Layout) -> None:
    try:
        require_instance_root_marker(layout.product_root, uid=layout.uid)
    except (OSError, ValueError) as error:
        raise InstallError(f"could not validate instance root marker: {error}") from error


def _preflight_instance_root(layout: Layout) -> bool:
    """Inspect all reserved entries before claiming or chmod'ing any directory."""

    _validate_layout_safety(layout)
    if layout.product_root.exists():
        metadata = layout.product_root.stat()
        if metadata.st_uid != layout.uid or stat.S_IMODE(metadata.st_mode) & 0o022:
            raise InstallError(
                "existing NETIZEN_ROOT must be current-user owned and not writable by group/others; "
                f"its permissions were not changed: {layout.product_root}"
            )
    marker = layout.product_root / INSTANCE_ROOT_MARKER
    claimed = _path_exists(marker)
    if claimed:
        _validate_root_marker(layout)
    for path in (
        layout.state_dir, layout.releases, layout.cache_dir,
        layout.credentials_dir, layout.lark_app_file.parent,
    ):
        if not _path_exists(path):
            continue
        if path.is_symlink() or not path.is_dir():
            raise InstallError(f"managed Netizen path is not a real directory: {path}")
        if claimed:
            if path in (layout.releases, layout.cache_dir) and any(path.iterdir()):
                _require_managed_netizen_directory(path, layout)
            continue
        allowed = (
            {layout.admin_secret_file} if path == layout.credentials_dir else
            {layout.lark_app_file} if path == layout.lark_app_file.parent else set()
        )
        if any(child not in allowed for child in path.iterdir()):
            # Another installer may have finished the atomic claim meanwhile.
            if _path_exists(marker):
                return _preflight_instance_root(layout)
            raise InstallError(f"refusing to claim a non-empty unowned directory: {path}")
    for path in (layout.config_file, layout.admin_secret_file, layout.lark_app_file):
        if _path_exists(path):
            _require_regular_file(path, "instance configuration")
            if not claimed:
                metadata = path.stat()
                if metadata.st_uid != layout.uid or stat.S_IMODE(metadata.st_mode) & 0o077:
                    raise InstallError(f"preconfigured instance file must be private and current-user owned: {path}")
    if not claimed:
        # A concurrent installer may have claimed and populated the root since
        # the first observation. Revalidate its marker instead of adopting it.
        if _path_exists(marker):
            return _preflight_instance_root(layout)
        for link in (layout.current, layout.previous):
            if _path_exists(link):
                raise InstallError(f"refusing to adopt a deployment without an instance root marker: {link}")
        if _path_exists(layout.lark_app_file):
            try:
                load_lark_app(layout.lark_app_file, allow_incomplete=True)
            except LarkAppConfigError as error:
                raise InstallError(str(error)) from error
        if _path_exists(layout.admin_secret_file):
            # This path exists: validation is stdlib-only and must not create
            # or repair a credential while the namespace is still unclaimed.
            _prepare_admin_secret(layout.admin_secret_file)
    return claimed


def _claim_instance_root(layout: Layout) -> None:
    if _preflight_instance_root(layout):
        _sync_instance_root(layout)
        return
    _ensure_real_directory(layout.product_root, mode=0o700, enforce_mode=False)
    marker = layout.product_root / INSTANCE_ROOT_MARKER
    descriptor, name = tempfile.mkstemp(prefix=".netizen-root.", dir=layout.product_root)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(INSTANCE_ROOT_MARKER_CONTENT)
            output.flush()
            os.fsync(output.fileno())
        # Link publishes a complete marker without replacing another claim.
        try:
            os.link(temporary, marker)
        except FileExistsError:
            pass
        _validate_root_marker(layout)
    finally:
        temporary.unlink(missing_ok=True)
    _sync_instance_root(layout)


def _sync_instance_root(layout: Layout) -> None:
    # Publish ownership durably before creating state/lock entries. Also sync
    # an existing marker, which may just have been linked by another installer.
    descriptor = os.open(layout.product_root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_managed_netizen_directory(path: Path) -> None:
    existed = _path_exists(path)
    if existed and (path.is_symlink() or not path.is_dir()):
        raise InstallError(f"managed Netizen path is not a real directory: {path}")
    if not existed:
        _ensure_real_directory(path, mode=0o700)

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
        path.chmod(0o700)
        return

    _require_regular_file(marker, "managed directory marker")
    try:
        content = marker.read_bytes()
    except OSError as error:
        raise InstallError(f"could not read managed directory marker {marker}: {error}") from error
    if content != MANAGED_DIRECTORY_MARKER_CONTENT:
        raise InstallError(f"managed directory marker is not recognized: {marker}")
    path.chmod(0o700)
    marker.chmod(0o600)


@contextlib.contextmanager
def installation_lock(layout: Layout) -> Iterator[None]:
    # The lock must outlive uninstall. Deleting a locked file would let a new
    # process create a second inode and enter concurrently with an old waiter.
    _claim_instance_root(layout)
    _ensure_real_directory(layout.state_dir, mode=0o700)
    try:
        with install_lock(layout.product_root, blocking=True):
            yield
    except (OSError, UpdateProtocolError) as error:
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
    """Prepare configuration and the single application credential profile."""

    source = sys.stdin if input_stream is None else input_stream
    if not _path_exists(layout.config_file):
        _write_atomic(layout.config_file, _default_config(layout).encode(), mode=0o600)
        _ensure_project_directory(layout.home / "projects")
    else:
        _require_regular_file(layout.config_file, "configuration")
        layout.config_file.chmod(0o600)
    _prepare_admin_secret(layout.admin_secret_file)
    if not _path_exists(layout.lark_app_file):
        _write_atomic(layout.lark_app_file, encode_lark_app("", ""), mode=0o600)
    credentials = _read_lark_app(layout, allow_incomplete=True)
    configured_app_id = credentials.app_id or None
    if credentials.app_id and credentials.app_secret:
        return

    if interactive or app_registrar is not None:
        use_browser = app_registrar is not None and (
            not interactive
            or _prompt_feishu_setup_method(source, app_id=configured_app_id)
        )
        if use_browser:
            info(
                "starting official Feishu/Lark browser setup; "
                "the App Secret will not be displayed"
            )
            try:
                credentials = app_registrar(configured_app_id)
            except InstallError:
                if not interactive:
                    raise InstallError(
                        "official Feishu/Lark browser setup did not complete; "
                        f"{rerun_instruction} for a new verification link, or run it "
                        "in an interactive terminal to choose manual setup. "
                        "Do not send the App Secret in chat."
                    ) from None
                print(
                    "Browser setup did not complete. Enter an existing App ID and "
                    "App Secret manually instead.",
                    file=sys.stderr,
                )
            else:
                _store_registered_feishu_credentials(
                    layout,
                    expected_app_id=configured_app_id,
                    credentials=credentials,
                )
                info(
                    f"Feishu/Lark app {credentials.app_id} configured; "
                    f"credential saved to {layout.lark_app_file}"
                )
                info(
                    "finish any tenant-admin approval/application publication, "
                    "set availability, and add the bot to target chats"
                )
                return
        if interactive:
            app_id = configured_app_id or _prompt_app_id(source)
            try:
                secret = secret_prompt("Feishu App Secret: ").strip()
            except EOFError as error:
                raise InstallError(
                    "Feishu App Secret input ended before a value was provided"
                ) from error
            if not secret:
                raise InstallError("Feishu App Secret must not be empty")
            _store_registered_feishu_credentials(
                layout,
                expected_app_id=configured_app_id,
                credentials=FeishuAppCredentials(app_id=app_id, app_secret=secret),
            )
            return

    raise ConfigurationRequired(
        "non-interactive install will not prompt for credentials; "
        f"complete the netizen profile appId/appSecret in {layout.lark_app_file}; "
        f"then {rerun_instruction}"
    )


def _read_lark_app(
    layout: Layout, *, allow_incomplete: bool = False
) -> LarkAppCredentials:
    try:
        return load_lark_app(layout.lark_app_file, allow_incomplete=allow_incomplete)
    except LarkAppConfigError as error:
        raise InstallError(str(error)) from error


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
    expected_app_id: str | None,
    credentials: FeishuAppCredentials,
) -> None:
    if expected_app_id is not None and credentials.app_id != expected_app_id:
        raise InstallError("browser setup returned a different App ID")
    try:
        encoded = encode_lark_app(credentials.app_id, credentials.app_secret)
    except LarkAppConfigError as error:
        raise InstallError("browser setup returned invalid application credentials") from error
    if not credentials.app_id or not credentials.app_secret:
        raise InstallError("browser setup returned incomplete application credentials")
    # One atomic replacement commits the complete pair, preserving previous
    # repair/rebind intent if the write fails.
    _write_atomic(layout.lark_app_file, encoded, mode=0o600)


def _default_config(layout: Layout) -> str:
    project_root = layout.home / "projects"
    return (
        "# Generated by Netizen. Edit this file before rerunning the installer.\n"
        "instance:\n"
        f"  dataDir: {json.dumps(str(layout.state_dir))}\n"
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
    # The layout already owns the selected instance. Candidate checks must not
    # inherit it as the default root for their disposable test environments.
    environment.pop("NETIZEN_ROOT", None)
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
                "--no-compile",
                "--constraint",
                release.source / "requirements.lock",
                release.source,
            ],
            env=environment,
        )
        info("precompiling installed packages with 4 workers")
        execute(
            [
                python, "-E", "-B", "-m", "compileall", "-q", "-j", "4",
                "-e", release.venv, release.venv,
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
        [python, source / "scripts" / "check_sdk.py"],
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


def _query_missing_feishu_permissions_from_release(
    release: Release,
    layout: Layout,
    *,
    runner: Runner,
    rerun_instruction: str = "rerun ./dev-install.sh",
) -> tuple[str, ...]:
    command: list[str | os.PathLike[str]] = [
        release.venv / "bin" / "python",
        "-E",
        "-B",
        release.source / "scripts" / "feishu_app_permissions.py",
        "--lark-app-config",
        layout.lark_app_file,
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
    if missing and repair_existing_app:
        app_id = _read_lark_app(layout).app_id
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
        raise FeishuPermissionsRequired(
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
                "paths = {'instance.projectRoot': settings.project_root, "
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
        or (admin_bind.port is not None and (
            isinstance(admin_bind.port, bool)
            or not isinstance(admin_bind.port, int)
            or not 1 <= admin_bind.port <= 65535
        ))
    ):
        raise InstallError("candidate returned an invalid Admin Web bind")
    if data_dir.resolve() != layout.state_dir.resolve():
        raise InstallError("managed instance.dataDir must be NETIZEN_ROOT/state")
    return RuntimeValidation(data_dir=data_dir, admin_bind=admin_bind)


def preflight_admin_bind(binding: AdminBind) -> None:
    """Best-effort collision check while holding every successful address."""

    if not binding.enabled or binding.port is None:
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


@contextlib.contextmanager
def _hold_service_lifetime_lock(layout: Layout) -> Iterator[None]:
    """Exclude service startup while rollback-protected state is inspected."""

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
                "service lifetime lock is still held; refusing to inspect "
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
        return decode_activation_intent(path.read_bytes())
    except (OSError, InstallError) as error:
        raise InstallError(f"could not read activation intent {path}: {error}") from error


def _write_activation_intent(
    layout: Layout,
    release: Release,
    *,
    should_start: bool,
    should_enable: bool,
    prior_release: Path | None = None,
    recovery: str | None = None,
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
        "version": 1 if recovery is None else 2,
        "release": release.digest,
        "priorRelease": prior_digest,
        "shouldStart": should_start,
        "shouldEnable": should_enable,
    }
    if recovery is not None:
        if re.fullmatch(r"[0-9a-f]{32}", recovery) is None:
            raise InstallError("invalid activation recovery identity")
        payload["recovery"] = recovery
    _write_atomic(
        layout.state_dir / ACTIVATION_INTENT,
        (json.dumps(payload, sort_keys=True) + "\n").encode(),
        mode=0o600,
    )
    _sync_directory(layout.state_dir)


def _clear_activation_intent(layout: Layout) -> None:
    path = layout.state_dir / ACTIVATION_INTENT
    if not _path_exists(path):
        return
    _require_regular_file(path, "activation intent")
    try:
        path.unlink()
        _sync_directory(layout.state_dir)
    except OSError as error:
        raise InstallError(f"could not clear activation intent {path}: {error}") from error


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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


def _recovery_release(layout: Layout, digest: str | None) -> Path | None:
    if digest is None:
        return None
    path = layout.releases / digest
    if path.is_symlink() or not path.is_dir() or path.resolve().parent != layout.releases.resolve():
        raise InstallError(f"activation recovery release is unavailable: {digest}")
    return path


def _recovery_snapshot(layout: Layout, recovery: Recovery) -> DatabaseSnapshot | None:
    files = recovery.payload["database_files"]
    if files is None:
        return None
    recovery.verify_database()
    return DatabaseSnapshot(layout.state_dir, recovery.root / "database", tuple(files))


def _clear_recovery(layout: Layout, recovery: Recovery) -> None:
    # Unlink the durable transaction pointer first. A crash during cleanup may
    # leave unused recovery material, never an intent pointing to missing data.
    _clear_activation_intent(layout)
    try:
        recovery.remove()
    except (OSError, InstallError) as error:
        info(f"warning: completed recovery material retained at {recovery.root}: {error}")


def _commit_activation(layout: Layout, release: Release, recovery: Recovery) -> None:
    old = _recovery_release(layout, recovery.payload["old_current"])
    if old is None:
        _set_release_link(layout.previous, None, layout)
    elif old.resolve() != release.root.resolve():
        _set_release_link(layout.previous, old, layout)
    recovery.save(phase="committed")
    _clear_recovery(layout, recovery)


def _rollback_activation(
    layout: Layout,
    backend: ServiceBackend,
    recovery: Recovery,
    *,
    ready_timeout: float,
    preserve_source_database: bool = False,
) -> None:
    payload = recovery.payload
    old = _recovery_release(layout, payload["old_current"])
    previous = _recovery_release(layout, payload["old_previous"])
    # Once restoration completed, an old service may already have accepted new
    # input. Retrying its start must not restore the snapshot a second time.
    if payload["phase"] not in {"restoring_service", "restored"}:
        backend.stop_and_confirm()
        with _hold_service_lifetime_lock(layout):
            if recovery.admission_observed():
                raise InstallError(
                    "candidate may have accepted input; retained its database and "
                    f"original recovery snapshot at {recovery.root}; rerun the exact candidate installer"
                )
            snapshot = _recovery_snapshot(layout, recovery)
            keep_source = payload["phase"] == "restoring_source"
            if (
                preserve_source_database and payload["source_version"] is not None
                and payload["phase"] not in {"restoring", "database_restored"}
                and _read_release_link(layout.current, layout) == old
            ):
                # The first supported old release has no admission hook. After
                # installer death it can restart and accept input if SQLite
                # rolled the migration back. A valid source DB is already the
                # state we need; restoring its earlier snapshot would lose work.
                try:
                    current_plan = plan_channel_database(layout.state_dir / "channel.sqlite3")
                except (RuntimeError, OSError):
                    pass
                else:
                    keep_source = current_plan["source_version"] == payload["source_version"]
            if keep_source:
                recovery.save(phase="restoring_source")
            elif payload["phase"] != "database_restored":
                # During a multi-file DB/WAL restore, the main file alone can
                # be a valid but incomplete source database. Keep the stable
                # service entry on the guarded candidate until every snapshot
                # file is restored and this completion is durable. Old v14
                # binaries do not understand recovery admission markers.
                candidate = _recovery_release(layout, payload["release"])
                _set_release_link(layout.current, candidate, layout)
                recovery.save(phase="restoring")
                _restore_database(snapshot)
                recovery.save(phase="database_restored")
            _set_release_link(layout.current, old, layout)
            _set_release_link(layout.previous, previous, layout)
            definition = payload["definition"]
            backend.restore_definition(
                FileSnapshot(
                    existed=definition["existed"],
                    content=bytes.fromhex(definition["content"]),
                    mode=definition["mode"],
                ),
                should_enable=payload["old_enabled"],
            )
            recovery.save(phase="restoring_service")
    else:
        if _read_release_link(layout.current, layout) != old:
            raise InstallError("restored release changed; refusing to repeat database recovery")
    if payload["old_loaded"]:
        backend.start_and_wait(timeout=ready_timeout)
    recovery.save(phase="restored")
    _clear_recovery(layout, recovery)


def _recover_activation(
    layout: Layout,
    release: Release,
    backend: ServiceBackend,
    intent: ActivationIntent,
    *,
    ready_timeout: float,
) -> bool:
    """Resolve one durable activation before planning another database change."""
    assert intent.recovery is not None
    recovery = load_recovery(layout, intent.recovery)
    payload = recovery.payload
    if (
        payload["release"] != intent.release
        or payload["old_current"] != intent.prior_release
        or payload["should_start"] != intent.should_start
        or payload["should_enable"] != intent.should_enable
    ):
        raise InstallError("activation intent and recovery record disagree")
    if release.digest != payload["release"] and (
        recovery.admission_observed() or payload["phase"] == "committed"
    ):
        raise InstallError(
            "interrupted candidate may have accepted input; recover its exact release "
            f"{payload['release']} before installing another version; recovery: {recovery.root}"
        )
    if payload["phase"] in {
        "restoring", "restoring_source", "database_restored", "restoring_service", "restored",
    }:
        _rollback_activation(
            layout, backend, recovery, ready_timeout=ready_timeout,
            preserve_source_database=True,
        )
        return False
    # Stop under the normal manager proof before consulting the marker. This
    # closes the race with a candidate publishing admission while we recover.
    if payload["phase"] != "committed":
        backend.stop_and_confirm()
    with _hold_service_lifetime_lock(layout) if payload["phase"] != "committed" else contextlib.nullcontext():
        admitted = recovery.admission_observed()
    if not admitted and payload["phase"] != "committed":
        _rollback_activation(
            layout, backend, recovery, ready_timeout=ready_timeout,
            preserve_source_database=True,
        )
        return False
    if release.digest != payload["release"]:
        raise InstallError(
            "interrupted candidate may have accepted input; recover its exact release "
            f"{payload['release']} before installing another version; recovery: {recovery.root}"
        )
    if _read_release_link(layout.current, layout) != release.root.resolve():
        raise InstallError("interrupted candidate release changed; database was retained")
    database = layout.state_dir / "channel.sqlite3"
    if _path_exists(database):
        plan = plan_channel_database(database)
        if plan["source_version"] != payload["target_version"] or plan["steps"]:
            raise InstallError("interrupted candidate database differs from its migration target")
    elif admitted:
        raise InstallError("interrupted candidate database is missing; recovery required")
    # Forward recovery deliberately never restores the old snapshot, including
    # when a repeated start fails. A subsequent exact installer can retry it.
    backend.publish_definition(
        backend.render_definition(release), should_enable=payload["should_enable"],
    )
    if payload["should_start"]:
        recovery.save(phase="starting")
        backend.start_and_wait(timeout=ready_timeout)
    _commit_activation(layout, release, recovery)
    return True


def activate_release(
    release: Release,
    layout: Layout,
    *,
    interactive: bool,
    runner: Runner | None = None,
    ready_timeout: float = SERVICE_READY_TIMEOUT_SECONDS,
    data_dir: Path | None = None,
    admin_bind: AdminBind | None = None,
    update: InstallerUpdate | None = None,
) -> None:
    execute = run_command if runner is None else runner
    backend = _service_backend(layout, execute)
    if data_dir is not None and data_dir.resolve() != layout.state_dir.resolve():
        raise InstallError("Channel database must belong to the selected instance root")
    pending_intent = _read_activation_intent(layout)
    if pending_intent is not None and pending_intent.recovery is not None:
        if update is not None:
            update.report("installing")
        try:
            completed = _recover_activation(
                layout, release, backend, pending_intent, ready_timeout=ready_timeout,
            )
        except BaseException as error:
            if update is not None:
                update.report("recovery_required", "rollback_incomplete")
            raise InstallError(f"interrupted activation requires recovery: {error}") from error
        if completed:
            _prune_after_activation(layout)
            return
        pending_intent = None

    old_current = _read_release_link(layout.current, layout)
    old_previous = _read_release_link(layout.previous, layout)
    old_definition = backend.capture_definition()
    old_state = backend.inspect_state()
    if not old_definition.existed and (old_state.loaded or old_state.enabled):
        raise InstallError(
            "the managed service definition is missing but its service-manager "
            "target is still loaded/enabled; inspect it before installing"
        )
    if pending_intent is None:
        intended_prior_release = old_current
        should_start = old_current is None or old_state.loaded
        should_enable = old_current is None or old_state.enabled
    else:
        # Version 1 intents predate automatic migration. Keep their existing
        # active/enabled recovery semantics without claiming a historical DB snapshot.
        intended_prior_release = _intent_prior_release(layout, pending_intent)
        should_start = pending_intent.should_start
        should_enable = pending_intent.should_enable
        info(f"recovering interrupted activation intent from release {pending_intent.release[:12]}")
    database = layout.state_dir / "channel.sqlite3"
    try:
        plan = plan_channel_database(database) if _path_exists(database) else None
    except (RuntimeError, OSError) as error:
        raise InstallError(f"Channel database upgrade preflight failed: {error}") from error
    definition = backend.render_definition(release)
    recovery = create_recovery(layout, {
        "phase": "prepared",
        "release": release.digest,
        "old_current": None if intended_prior_release is None else intended_prior_release.name,
        "old_previous": None if old_previous is None else old_previous.name,
        "old_loaded": old_state.loaded,
        "old_enabled": old_state.enabled,
        "should_start": should_start,
        "should_enable": should_enable,
        "source_version": None if plan is None else plan["source_version"],
        "target_version": SCHEMA_VERSION if plan is None else plan["target_version"],
        "definition": {
            "existed": old_definition.existed,
            "content": old_definition.content.hex(),
            "mode": old_definition.mode,
        },
        "database_files": None,
    })
    if update is not None:
        update.report("installing")
    _write_activation_intent(
        layout, release, should_start=should_start, should_enable=should_enable,
        prior_release=intended_prior_release, recovery=recovery.id,
    )
    try:
        if old_state.loaded:
            backend.stop_and_confirm()
        if admin_bind is not None:
            preflight_admin_bind(admin_bind)
        with _hold_service_lifetime_lock(layout):
            # Re-plan after exclusion: preflight was read-only while the old
            # service could still write. Never infer a schema from release tags.
            if plan is not None:
                locked_plan = plan_channel_database(database)
                if locked_plan != plan:
                    raise InstallError("Channel database migration plan changed before activation")
            elif _path_exists(database):
                raise InstallError("Channel database appeared after upgrade preflight")
            snapshot = _capture_database(layout.state_dir, recovery.root)
            recovery.seal_database(snapshot.existing_files)
            recovery.save(phase="snapshot")
            if plan is not None:
                info(f"Channel database schema {plan['source_version']} -> {plan['target_version']}")
                migrate_channel_database(database, expected_source_version=plan["source_version"])
            recovery.save(phase="publishing")
            _set_release_link(layout.current, release.root, layout)
            backend.publish_definition(definition, should_enable=should_enable)
            recovery.save(phase="starting" if should_start else "published")
        if should_start:
            if update is not None:
                update.report("restarting")
            backend.start_and_wait(timeout=ready_timeout)
        _commit_activation(layout, release, recovery)
    except BaseException as error:
        try:
            _rollback_activation(layout, backend, recovery, ready_timeout=ready_timeout)
        except BaseException as rollback_error:
            if update is not None:
                with contextlib.suppress(InstallError):
                    update.report("recovery_required", "rollback_incomplete")
            raise InstallError(
                f"activation failed: {error}; rollback incomplete: {rollback_error}; "
                f"recovery snapshot preserved at {recovery.root}"
            ) from error
        if update is not None:
            with contextlib.suppress(InstallError):
                update.report("rolled_back", "activation_failed")
        raise InstallError(f"activation failed and was rolled back: {error}") from error
    _prune_after_activation(layout)


def _prune_after_activation(layout: Layout) -> None:
    try:
        _prune_releases(layout)
    except (InstallError, OSError) as error:
        info(f"warning: release activated but obsolete release cleanup failed: {error}")


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
        with restored.open("rb") as handle:
            os.fsync(handle.fileno())
    _sync_directory(data_dir)


def _stream_digest(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.digest()


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
            _sync_directory(link.parent)
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
        _sync_directory(link.parent)
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
    admin_port: int | None = None,
) -> Release:
    return _install(
        source_root=source_root,
        prepare_candidate=prepare_source_release,
        rerun_instruction="rerun ./dev-install.sh",
        layout=layout,
        runner=runner,
        interactive=interactive,
        admin_port=admin_port,
    )


def install_published(
    *,
    source_root: Path,
    layout: Layout | None = None,
    runner: Runner | None = None,
    interactive: bool | None = None,
    admin_port: int | None = None,
) -> Release:
    manifest = read_published_release_manifest(source_root)
    selected_layout = resolve_layout() if layout is None else layout
    update = _installer_update(selected_layout, manifest)

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

    with _report_install_update(update):
        return _install(
            source_root=source_root,
            prepare_candidate=prepare_candidate,
            rerun_instruction=(
                f"rerun the official Netizen v{manifest.version} installer from "
                f"{OFFICIAL_RELEASE_DOWNLOADS}/v{manifest.version}/install.sh"
            ),
            layout=selected_layout,
            runner=runner,
            interactive=False if update is not None else interactive,
            update=update,
            admin_port=admin_port,
        )


def _install(
    *,
    source_root: Path,
    prepare_candidate: CandidatePreparer,
    rerun_instruction: str,
    layout: Layout | None,
    runner: Runner | None,
    interactive: bool | None,
    update: InstallerUpdate | None = None,
    admin_port: int | None = None,
) -> Release:
    selected_layout = resolve_layout() if layout is None else layout
    if admin_port is not None and (isinstance(admin_port, bool) or not isinstance(admin_port, int) or not 1 <= admin_port <= 65535):
        raise InstallError("admin port must be an integer from 1 to 65535")
    rerun_instruction += f" --root {shlex.quote(str(selected_layout.product_root))}"
    if admin_port is not None:
        rerun_instruction += f" --admin-port {admin_port}"
    require_supported_platform(
        selected_layout.platform,
        require_definition_validation=True,
    )
    execute = run_command if runner is None else runner
    is_interactive = sys.stdin.isatty() if interactive is None else interactive
    _validate_source_location(source_root, selected_layout)
    backend = _service_backend(selected_layout, execute)
    backend.preflight()
    lock_context = installation_lock(selected_layout) if update is None else contextlib.nullcontext()
    with lock_context:
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
            if update is not None:
                raise
        if not configuration_ready:
            info(
                "Feishu/Lark credentials are incomplete; preparing the release "
                "and verifying Codex login before browser setup. Keep this installer "
                "running and read its output for the verification link."
            )
        release = prepare_candidate(
            selected_layout,
            source_root=source_root,
            runner=execute,
        )
        if admin_port is not None:
            _configure_admin_port(release, selected_layout, admin_port, execute)
        require_codex_login(
            release,
            selected_layout,
            rerun_instruction=rerun_instruction,
            runner=execute,
        )
        if not configuration_ready:
            prepare_configuration(
                selected_layout,
                interactive=is_interactive,
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
            repair_existing_app=configuration_ready and update is None,
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
            **({"update": update} if update is not None else {}),
        )
        if update is None:
            _record_manual_update_recovery(selected_layout)
    info(f"installed release {release.digest[:12]} at {release.root}")
    info(f"configuration: {selected_layout.config_file}")
    info("service environment: account shell profile (reloaded on every start)")
    info(f"instance root: {selected_layout.product_root}")
    info(f"service control: {shlex.join([str(release.source / 'service.sh'), '--root', str(selected_layout.product_root), 'status'])}")
    info("send /admin to this Feishu bot to find its running Admin URL")
    return release


def _configure_admin_port(release: Release, layout: Layout, port: int, execute: Runner) -> None:
    # Bootstrap is stdlib-only. Use the validated candidate's YAML dependency
    # to update the one requested field without dropping unrelated mappings.
    execute(
        [
            release.venv / "bin" / "python", "-E", "-B", "-c",
            (
                "from pathlib import Path; import sys; "
                "from netizen.admin.port_config import set_admin_port; "
                "set_admin_port(Path(sys.argv[1]), int(sys.argv[2]))"
            ),
            layout.config_file, str(port),
        ],
        cwd=release.root,
        env=_service_environment(layout),
    )


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
    _validate_root_marker(selected_layout)
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


def _cleanup_terminal_maintenance(layout: Layout) -> None:
    """Clean only a durable terminal job while the installer owns its lock.

    The held install lock proves the worker no longer owns execution. Pending
    records are not a completion proof and are left for normal reconciliation.
    Never discover jobs by name prefix or change the recorded outcome here.
    """
    try:
        operation = read_operation(layout.product_root)
        if operation is None or not terminal_phase(operation["phase"]):
            return
        UpdateExecutor(layout.home, layout.platform, root=layout.product_root).cleanup(
            operation["operationId"]
        )
    except (OSError, UpdateProtocolError, UpdateExecutorError) as error:
        raise InstallError(
            "could not safely clean up this instance's completed maintenance job; "
            "its program and deployment state were retained"
        ) from error


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
    if not _path_exists(selected_layout.product_root) and not _path_exists(selected_layout.service_file):
        backend.preflight()
        state = backend.inspect_state()
        if state.loaded or state.enabled:
            raise InstallError("refusing to uninstall an orphaned service without its instance root and definition")
        info(f"Netizen is already uninstalled at {selected_layout.product_root}")
        return
    _validate_root_marker(selected_layout)
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
    with installation_lock(selected_layout):
        if not any(
            _path_exists(path)
            for path in (
                selected_layout.releases,
                selected_layout.cache_dir,
                selected_layout.current,
                selected_layout.previous,
                selected_layout.service_file,
                activation_intent,
            )
        ):
            state = backend.inspect_state()
            if state.loaded or state.enabled:
                raise InstallError("refusing to uninstall an orphaned service without its definition")
            _cleanup_terminal_maintenance(selected_layout)
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
        if _path_exists(selected_layout.service_file):
            backend.capture_definition()
        _cleanup_terminal_maintenance(selected_layout)
        backend.uninstall_definition()
        _clear_activation_intent(selected_layout)
        for link in (selected_layout.current, selected_layout.previous):
            _set_release_link(link, None, selected_layout)
        _remove_managed_netizen_directory(selected_layout.cache_dir, selected_layout)
        _remove_managed_netizen_directory(selected_layout.releases, selected_layout)
    info(f"uninstalled Netizen program and user service at {selected_layout.product_root}")
    info(
        "preserved configuration and credentials: "
        f"{selected_layout.config_file}, {selected_layout.lark_app_file}, {selected_layout.credentials_dir}"
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


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Netizen installer internals")
    subparsers = parser.add_subparsers(dest="command", required=True)
    source = subparsers.add_parser("install-source")
    release = subparsers.add_parser("install-release")
    release.add_argument("source_root", type=Path)
    service = subparsers.add_parser("service")
    service.add_argument("action", choices=("start", "stop", "restart", "status"))
    remove = subparsers.add_parser("uninstall")
    for command in (source, release, service, remove):
        command.add_argument("--root", help="instance directory (defaults to NETIZEN_ROOT or ~/.netizen)")
    for command in (source, release):
        command.add_argument("--admin-port", type=_port_argument)
    return parser.parse_args(argv)


def _port_argument(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("admin port must be an integer from 1 to 65535") from error
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("admin port must be an integer from 1 to 65535")
    return port


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        layout = resolve_layout(root=args.root)
        if args.command == "install-source":
            install_source(layout=layout, admin_port=args.admin_port)
            return 0
        if args.command == "install-release":
            install_published(source_root=args.source_root, layout=layout, admin_port=args.admin_port)
            return 0
        if args.command == "service":
            return service_action(args.action, layout=layout)
        uninstall(layout=layout)
        return 0
    except (InstallError, OSError) as error:
        print(f"netizen: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("netizen: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
