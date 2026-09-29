"""Instance-owned data and startup migration; never a package installer.

Only explicit setup initializes a database. Every actual service start validates
and, when necessary, migrates it while retaining the same lifetime lock until
exit. Migration backups are recovery material, not automatic rollback commands.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing, contextmanager
import fcntl
import os
from pathlib import Path
import pwd
import sqlite3
import stat
import tempfile
import time
import uuid

import yaml

from .bindings import BindingStore
from .database_migrations import (
    MigrationPlan,
    migrate_channel_database,
    plan_channel_database,
)
from .deployment.update_protocol import install_lock
from .instance import (
    INSTANCE_ROOT_MARKER,
    INSTANCE_ROOT_MARKER_CONTENT,
    require_instance_root_marker,
    resolve_instance_root,
)
from .lark_app import load_lark_app


INSTANCE_DATA_MARKER = ".netizen-initialized"
_DATA_STATES = {
    state: f"netizen-instance-data-v1:{state}\n".encode()
    for state in ("initializing", "initialized", "purging", "purged")
}
_BACKUP_MARKER = ".netizen-migration-backup"
_BACKUP_MARKER_CONTENT = b"netizen-channel-migration-backup-v1\n"
_DIRECTORIES = ("state", "credentials", "lark-app")
_PURGE_FILES = (
    "config.yaml", "lark-app/config.json", "credentials/admin-web-secret",
    "state/channel.sqlite3", "state/channel.sqlite3-wal", "state/channel.sqlite3-shm",
    "state/channel.sqlite3-journal", "state/netizen.log", "state/netizen.log.1",
    "state/netizen.log.2", "state/launchd.stderr.log", "state/service.ready",
    "state/service.identity.json",
)


class InstanceDataError(RuntimeError):
    """The instance cannot be safely initialized, started, or purged."""


class StartupRejected(InstanceDataError):
    """Deterministic startup rejection that requires configuration/data repair.

    Managed service entry points exit cleanly without readiness for this error,
    so the manager does not repeatedly retry an unchanged invalid instance.
    """


def _directory(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o022):
        raise InstanceDataError(f"unsafe instance directory: {path}")


def _file(path: Path, *, private: bool = True) -> None:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or (private and stat.S_IMODE(info.st_mode) != 0o600)):
        raise InstanceDataError(f"unsafe instance file: {path}")


def _root(root: Path) -> Path:
    canonical = resolve_instance_root(root)
    if Path(root) != canonical:
        raise InstanceDataError(f"instance operation requires canonical root: {canonical}")
    _directory(canonical)
    try:
        require_instance_root_marker(canonical)
    except (OSError, ValueError) as error:
        raise InstanceDataError(f"instance ownership is not confirmed: {error}") from error
    return canonical


def _sync(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, payload: bytes) -> None:
    try:
        _file(path)
    except FileNotFoundError:
        pass
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _sync(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def ensure_instance_root(root: Path) -> None:
    """Explicit setup's minimal ownership preparation, before any other writes.

    A preconfigured private config/profile/secret may be supplied. Unmarked state
    and unknown files in managed directories are never silently claimed.
    """
    canonical = resolve_instance_root(root)
    if Path(root) != canonical:
        raise InstanceDataError(f"instance operation requires canonical root: {canonical}")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _directory(root)
    marker = root / INSTANCE_ROOT_MARKER
    if marker.exists() or marker.is_symlink():
        _root(root)
    else:
        for name in _DIRECTORIES:
            directory = root / name
            if directory.exists() or directory.is_symlink():
                _directory(directory)
                allowed = {"credentials": {"admin-web-secret"}, "lark-app": {"config.json"}}.get(name, set())
                for child in directory.iterdir():
                    if child.name not in allowed:
                        raise InstanceDataError(f"unowned instance data; refusing to claim {child}")
                    _file(child)
        for name in ("config.yaml", INSTANCE_DATA_MARKER):
            path = root / name
            if path.exists() or path.is_symlink():
                if name == INSTANCE_DATA_MARKER:
                    raise InstanceDataError(f"data marker exists without root ownership: {path}")
                _file(path)
        profile = root / "lark-app" / "config.json"
        if profile.exists():
            load_lark_app(profile, allow_incomplete=True)
        secret = root / "credentials" / "admin-web-secret"
        if secret.exists():
            from .admin.auth import load_credential_snapshot
            load_credential_snapshot(secret)
        descriptor, temporary = tempfile.mkstemp(prefix=".netizen-root-", dir=root)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(INSTANCE_ROOT_MARKER_CONTENT)
                output.flush()
                os.fsync(output.fileno())
            # Publish complete ownership evidence without replacing a concurrent
            # claim; an in-progress marker must never look like a damaged root.
            try:
                os.link(temporary, marker)
            except FileExistsError:
                pass
            _root(root)
        finally:
            Path(temporary).unlink(missing_ok=True)
        _sync(root)
    for name in _DIRECTORIES:
        directory = root / name
        directory.mkdir(mode=0o700, exist_ok=True)
        _directory(directory)


def acquire_lifetime_lock(root: Path) -> int:
    """Acquire the exclusive service lock without creating an instance."""
    _root(root)
    directory = root / "state"
    _directory(directory)
    descriptor = os.open(directory / "service.lifetime.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        _file(directory / "service.lifetime.lock")
        os.set_inheritable(descriptor, False)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def instance_lifetime_lock(root: Path) -> Iterator[int]:
    descriptor = acquire_lifetime_lock(root)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def root_maintenance_lock(root: Path) -> Iterator[int]:
    """Serialize registration/removal/setup; do not hold the service's own lock."""
    _root(root)
    with install_lock(root) as descriptor:
        yield descriptor


def validate_lifetime_lock(root: Path, descriptor: int) -> None:
    """Require the exact already-held open-file description, not just a busy file."""
    _root(root)
    path = root / "state" / "service.lifetime.lock"
    _directory(path.parent)
    _file(path)
    actual, expected = os.fstat(descriptor), path.lstat()
    if (not stat.S_ISREG(actual.st_mode)
            or (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino)):
        raise InstanceDataError("lifetime lock descriptor does not match this instance")
    os.set_inheritable(descriptor, False)
    probe = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise InstanceDataError("instance lifetime lock is not held")
    finally:
        os.close(probe)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        raise InstanceDataError("lifetime lock belongs to another process") from error


def _data_state(root: Path) -> str | None:
    marker = root / INSTANCE_DATA_MARKER
    try:
        _file(marker)
    except FileNotFoundError:
        return None
    payload = marker.read_bytes()
    for state, expected in _DATA_STATES.items():
        if payload == expected:
            return state
    raise InstanceDataError(f"unrecognized instance data marker: {marker}")


def _database(root: Path) -> Path:
    _directory(root / "state")
    path = root / "state" / "channel.sqlite3"
    try:
        _file(path)
    except FileNotFoundError as error:
        raise InstanceDataError("prepared instance database is missing; restore its data, do not recreate an empty database") from error
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            _file(Path(str(path) + suffix))
        except FileNotFoundError:
            pass
    return path


def _check_recovery(root: Path) -> None:
    intent = root / "state" / ".activation-intent.json"
    if intent.exists() or intent.is_symlink():
        raise InstanceDataError("unfinished legacy activation recovery; resolve it manually before CLI startup")


def begin_instance_setup(root: Path, *, lifetime_descriptor: int) -> MigrationPlan | None:
    """Record explicit setup before configuration or authorization can write data.

    Return a validated existing database plan, or None for a proven new setup.
    This never creates a database or reclassifies a lost one as a new instance.
    """
    validate_lifetime_lock(root, lifetime_descriptor)
    _check_recovery(root)
    state = _data_state(root)
    database = root / "state" / "channel.sqlite3"
    has_data = any(
        candidate.exists() or candidate.is_symlink()
        for suffix in ("", "-wal", "-shm", "-journal")
        for candidate in (Path(str(database) + suffix),)
    )
    if state == "initialized" or (state == "initializing" and has_data):
        return plan_channel_database(_database(root))
    if state == "purging":
        raise InstanceDataError("instance purge is incomplete; retry remove --purge before setup")
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = Path(str(database) + suffix)
        if candidate.exists() or candidate.is_symlink():
            raise InstanceDataError(f"uninitialized instance has existing database data: {candidate}; explicit manual repair is required")
    if state != "initializing":
        _atomic_write(root / INSTANCE_DATA_MARKER, _DATA_STATES["initializing"])
    return None


def initialize_instance_data(root: Path, *, lifetime_descriptor: int) -> MigrationPlan:
    """Explicit setup only; a missing DB in an existing instance is never new."""
    plan = begin_instance_setup(root, lifetime_descriptor=lifetime_descriptor)
    if plan is not None:
        if _data_state(root) == "initializing":
            _atomic_write(root / INSTANCE_DATA_MARKER, _DATA_STATES["initialized"])
        return plan
    database = root / "state" / "channel.sqlite3"
    # The durable initializing state proves this is unfinished explicit setup,
    # unlike an initialized instance that lost its DB. An interruption before
    # file creation may retry; any partial existing DB still needs validation.
    descriptor = os.open(database, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(descriptor)
    try:
        store = BindingStore(database)
        store.close()
    except sqlite3.Error as error:
        raise InstanceDataError(
            "database initialization failed; partial data was preserved; inspect and repair the instance before retrying setup"
        ) from error
    plan = plan_channel_database(database)
    _atomic_write(root / INSTANCE_DATA_MARKER, _DATA_STATES["initialized"])
    return plan


def validate_prepared_instance(root: Path) -> MigrationPlan:
    """Read-only validation; old supported schemas remain eligible for startup."""
    _root(root)
    _check_recovery(root)
    if _data_state(root) != "initialized":
        raise InstanceDataError("instance is not initialized; run netizen setup explicitly (or repair interrupted setup/purge)")
    _file(root / "config.yaml")
    return plan_channel_database(_database(root))


def _backup(root: Path, database: Path) -> Path:
    directory = root / "state" / "migration-backups"
    directory.mkdir(mode=0o700, exist_ok=True)
    _directory(directory)
    backup_dir = directory / uuid.uuid4().hex
    backup_dir.mkdir(mode=0o700)
    destination = backup_dir / "channel.sqlite3"
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(descriptor)
    source = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    deadline = time.monotonic() + 30
    def progress(_status: int, _remaining: int, _total: int) -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError("database backup timed out; check for an external database writer")
    try:
        target = sqlite3.connect(destination)
        try:
            source.backup(target, pages=256, progress=progress)
        finally:
            target.close()
    finally:
        source.close()
    descriptor = os.open(destination, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _atomic_write(backup_dir / _BACKUP_MARKER, _BACKUP_MARKER_CONTENT)
    _sync(directory)
    return destination


def prepare_instance(root: Path, *, lifetime_descriptor: int) -> MigrationPlan:
    """Actual-start admission, with caller retaining its lock until service exit."""
    try:
        return _prepare_instance(root, lifetime_descriptor=lifetime_descriptor)
    except (RuntimeError, ValueError, FileNotFoundError, PermissionError) as error:
        # SQLite's public error codes distinguish a persistent invalid schema
        # from transient contention/storage failures; never parse its messages.
        cause: BaseException | None = error
        while cause is not None:
            if isinstance(cause, sqlite3.Error) and (
                getattr(cause, "sqlite_errorcode", 0) & 0xff
            ) in {
                sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_IOERR,
                sqlite3.SQLITE_FULL, sqlite3.SQLITE_NOMEM, sqlite3.SQLITE_INTERRUPT,
                sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_READONLY,
            }:
                raise
            cause = cause.__cause__
        raise StartupRejected(str(error)) from error


def _prepare_instance(root: Path, *, lifetime_descriptor: int) -> MigrationPlan:
    validate_lifetime_lock(root, lifetime_descriptor)
    plan = validate_prepared_instance(root)
    if plan["steps"]:
        database = _database(root)
        _backup(root, database)
        migrate_channel_database(database, expected_source_version=plan["source_version"])
    else:
        return plan
    # Validate the committed target. Failure never restores an older snapshot.
    return plan_channel_database(_database(root))


def _protected_paths(root: Path, *, protected_codex_home: Path | None = None) -> tuple[Path, ...]:
    home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
    paths = [Path(os.environ.get("CODEX_HOME", str(home / ".codex"))).expanduser().resolve()]
    if protected_codex_home is not None:
        if not protected_codex_home.is_absolute():
            raise InstanceDataError("bound service CODEX_HOME must be an absolute path")
        paths.append(protected_codex_home.resolve())
    config = root / "config.yaml"
    if config.exists() or config.is_symlink():
        _file(config)
        try:
            value = yaml.safe_load(config.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("configuration root must be a mapping")
            instance = value.get("instance", {})
            projects = value.get("projects", {})
            if not isinstance(instance, dict) or not isinstance(projects, dict):
                raise ValueError("invalid Project configuration")
            for raw in [instance.get("projectRoot"), *projects.values()]:
                if raw is None:
                    continue
                if not isinstance(raw, str) or not Path(raw).expanduser().is_absolute():
                    raise ValueError("Project paths must be absolute")
                paths.append(Path(raw).expanduser().resolve())
        except (ValueError, OSError, yaml.YAMLError) as error:
            raise InstanceDataError(f"cannot verify protected Project paths before purge: {error}") from error
    elif _data_state(root) not in {"initializing", "purged"}:
        raise InstanceDataError(
            "cannot verify Project paths without instance configuration; preserve remaining files. "
            "Review the previous deletion report and backups for manual recovery or restore the missing configuration. "
            "Do not delete the whole root or alter ownership markers to bypass validation."
        )
    database = root / "state" / "channel.sqlite3"
    if database.exists() or database.is_symlink():
        try:
            # Admin-registered Projects need not appear among YAML seed aliases.
            # Read the supported persisted format without creating a Store,
            # migrating it, or opening a writer in the controlling environment.
            plan_channel_database(_database(root))
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0.25)) as connection:
                connection.execute("PRAGMA query_only=ON")
                for (raw,) in connection.execute("SELECT cwd FROM projects"):
                    if not isinstance(raw, str) or not Path(raw).is_absolute():
                        raise ValueError("persisted Project path is not absolute")
                    paths.append(Path(raw).resolve())
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
            raise InstanceDataError("cannot verify persisted Project paths before purge; preserve data and inspect or repair the database") from error
    elif _data_state(root) not in {"initializing", "purged"}:
        # Once deletion has removed that evidence, a partial retry cannot infer
        # the original Project inventory. Fail closed instead of claiming that
        # missing data means the instance never had a registered Project.
        raise InstanceDataError(
            "cannot verify persisted Project paths without the database; preserve remaining files. "
            "Review the previous deletion report and backups for manual recovery or restore the missing database. "
            "Do not delete the whole root or alter ownership markers to bypass validation."
        )
    return tuple(paths)


def purge_inventory(root: Path, *, protected_codex_home: Path | None = None) -> tuple[Path, ...]:
    """An explicit finite file inventory, never recursive ownership by location."""
    _root(root)
    if _data_state(root) not in {"initialized", "initializing", "purging", "purged"}:
        raise InstanceDataError("instance data ownership is not established; refusing purge")
    for name in _DIRECTORIES:
        directory = root / name
        if directory.exists() or directory.is_symlink():
            _directory(directory)
    protected = _protected_paths(root, protected_codex_home=protected_codex_home)
    paths = []
    for name in _PURGE_FILES:
        candidate = root / name
        try:
            _file(candidate)
        except FileNotFoundError:
            continue
        paths.append(candidate)
    backups = root / "state" / "migration-backups"
    if backups.exists() or backups.is_symlink():
        _directory(backups)
        for child in sorted(backups.iterdir()):
            # Other files/directories remain user-owned, even in this namespace.
            if len(child.name) != 32 or any(c not in "0123456789abcdef" for c in child.name):
                continue
            _directory(child)
            marker = child / _BACKUP_MARKER
            if not marker.exists() and not marker.is_symlink():
                continue
            _file(marker)
            if marker.read_bytes() != _BACKUP_MARKER_CONTENT:
                raise InstanceDataError(f"unrecognized migration backup marker: {marker}")
            database = child / "channel.sqlite3"
            if database.exists() or database.is_symlink():
                _file(database)
                paths.append(database)
            paths.append(marker)
    for candidate in paths:
        if any(candidate == path or candidate.is_relative_to(path) or path.is_relative_to(candidate) for path in protected):
            raise InstanceDataError(f"purge target overlaps Project or shared Codex state: {candidate}")
    # Delete configuration last so a partial purge retry can still check Projects.
    return tuple(sorted(paths, key=lambda path: (
        path == root / "config.yaml", path.name == _BACKUP_MARKER, str(path),
    )))


class InstancePurgeError(InstanceDataError):
    def __init__(self, message: str, deleted: tuple[Path, ...]) -> None:
        super().__init__(message)
        self.deleted = deleted


def purge_instance_data(
    root: Path, *, expected_inventory: tuple[Path, ...], lifetime_descriptor: int,
    protected_codex_home: Path | None = None,
) -> tuple[Path, ...]:
    """Caller has confirmed service removal; preserve root, locks and unknowns."""
    validate_lifetime_lock(root, lifetime_descriptor)
    inventory = purge_inventory(root, protected_codex_home=protected_codex_home)
    # Clean shutdown may remove WAL/SHM or readiness files shown at confirmation.
    # A narrower inventory is safe; newly discovered targets need confirmation.
    if not set(inventory).issubset(expected_inventory):
        raise InstanceDataError("purge inventory changed since confirmation; inspect and confirm again")
    _atomic_write(root / INSTANCE_DATA_MARKER, _DATA_STATES["purging"])
    deleted: list[Path] = []
    try:
        for path in inventory:
            _file(path)
            path.unlink()
            deleted.append(path)
            _sync(path.parent)
        _atomic_write(root / INSTANCE_DATA_MARKER, _DATA_STATES["purged"])
    except (OSError, InstanceDataError) as error:
        raise InstancePurgeError(f"instance purge stopped after {len(deleted)} files: {error}", tuple(deleted)) from error
    return tuple(deleted)
