"""Forward Channel database migrations from the v14 baseline.

The actual-start preparation boundary owns the lifetime lock and optional backup;
the business Runtime remains current-schema-only. Published steps and validators
are immutable and must not call the current schema creator or manage transactions.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from .bindings import SCHEMA_VERSION
from .migrations.v14 import validate as validate_v14


MIN_SUPPORTED_SCHEMA_VERSION = 14
Validator = Callable[[sqlite3.Connection], None]


@dataclass(frozen=True)
class Migration:
    from_version: int
    to_version: int
    apply: Callable[[sqlite3.Connection], None]
    validate_target: Validator


# Add an adjacent step only when the persisted schema actually changes. The
# baseline release deliberately has no artificial v15 migration.
MIGRATIONS: tuple[Migration, ...] = ()


class MigrationStep(TypedDict):
    from_version: int
    to_version: int


class MigrationPlan(TypedDict):
    source_version: int
    target_version: int
    steps: list[MigrationStep]


def _registered() -> tuple[dict[int, Migration], dict[int, Validator]]:
    steps: dict[int, Migration] = {}
    validators = {MIN_SUPPORTED_SCHEMA_VERSION: validate_v14}
    for step in MIGRATIONS:
        if (
            type(step.from_version) is not int or type(step.to_version) is not int
            or step.from_version < MIN_SUPPORTED_SCHEMA_VERSION
            or step.to_version != step.from_version + 1
            or step.to_version > SCHEMA_VERSION
            or step.from_version in steps
            or not all(callable(callback) for callback in (step.apply, step.validate_target))
        ):
            raise RuntimeError("invalid or ambiguous Channel database migration registry")
        steps[step.from_version] = step
        validators[step.to_version] = step.validate_target
    return steps, validators


def _path(source: int) -> tuple[list[Migration], Validator]:
    if source < MIN_SUPPORTED_SCHEMA_VERSION:
        raise RuntimeError(
            f"unsupported Channel database schema v{source}; automatic upgrades start at "
            f"v{MIN_SUPPORTED_SCHEMA_VERSION}; existing data was not changed"
        )
    if source > SCHEMA_VERSION:
        raise RuntimeError(
            f"Channel database schema v{source} is newer than this release (v{SCHEMA_VERSION}); "
            "downgrades are not supported; existing data was not changed"
        )
    steps, validators = _registered()
    if source not in validators:
        raise RuntimeError(f"unknown Channel database schema v{source}; existing data was not changed")
    path = []
    version = source
    while version < SCHEMA_VERSION:
        step = steps.get(version)
        if step is None:
            raise RuntimeError(f"missing Channel database migration from v{version}; existing data was not changed")
        path.append(step)
        version = step.to_version
    return path, validators[source]


def _database_uri(path: str | Path, mode: str) -> str:
    database = Path(path)
    if database.is_symlink() or not database.is_file():
        raise RuntimeError("Channel database must be an existing regular file")
    return database.resolve().as_uri() + f"?mode={mode}"


def _version(connection: sqlite3.Connection) -> int:
    rows = connection.execute("SELECT version FROM schema_version").fetchall()
    if len(rows) != 1 or type(rows[0][0]) is not int:
        raise RuntimeError("Channel database must contain exactly one integer schema version")
    return rows[0][0]


def _integrity(connection: sqlite3.Connection) -> None:
    if [row[0] for row in connection.execute("PRAGMA integrity_check")] != ["ok"]:
        raise RuntimeError("Channel database integrity check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RuntimeError("Channel database foreign-key check failed")


def _plan(connection: sqlite3.Connection) -> tuple[MigrationPlan, list[Migration]]:
    source = _version(connection)
    steps, validate = _path(source)
    _validate(connection, validate)
    _integrity(connection)
    return {
        "source_version": source,
        "target_version": SCHEMA_VERSION,
        "steps": [{"from_version": step.from_version, "to_version": step.to_version} for step in steps],
    }, steps


def plan_channel_database(path: str | Path) -> MigrationPlan:
    """Read-only validation and complete-path preflight; never create or repair a DB."""
    try:
        with closing(sqlite3.connect(_database_uri(path, "ro"), uri=True, isolation_level=None, timeout=0.25)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN")
            plan, _ = _plan(connection)
            return plan
    except sqlite3.Error as error:
        raise RuntimeError(f"Channel database migration preflight failed: {error}") from error


def _migration_authorizer(action: int, first: str | None, _second: str | None,
                          _database: str | None, _trigger: str | None) -> int:
    # executescript() implicitly COMMITs an open transaction, even with no COMMIT
    # in the script. SQLite's authorizer blocks it before it can escape rollback.
    if action in {
        sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT,
        sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH,
    }:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA and first not in {
        "quick_check", "integrity_check", "foreign_key_check", "table_info", "table_xinfo",
        "index_list", "index_info", "index_xinfo", "foreign_key_list",
    }:
        # SQLite itself uses quick_check when ALTER TABLE adds a CHECK constraint.
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _validation_authorizer(action: int, first: str | None, second: str | None,
                          database: str | None, trigger: str | None) -> int:
    if action not in {
        sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_RECURSIVE, sqlite3.SQLITE_PRAGMA,
    }:
        return sqlite3.SQLITE_DENY
    return _migration_authorizer(action, first, second, database, trigger)


def _validate(connection: sqlite3.Connection, validate: Validator) -> None:
    connection.set_authorizer(_validation_authorizer)
    try:
        validate(connection)
    finally:
        connection.set_authorizer(None)


def migrate_channel_database(path: str | Path, *, expected_source_version: int) -> MigrationPlan:
    """Apply the entire pending path atomically under the caller's lifetime lock.

    A source-version mismatch fails before opening a writer. The version is also
    checked under BEGIN IMMEDIATE, so a preflight is never treated as a lock.
    Migration callbacks are trusted release code; they must not replace the
    connection authorizer or open independent connections.
    """
    plan = plan_channel_database(path)
    if type(expected_source_version) is not int or plan["source_version"] != expected_source_version:
        raise RuntimeError("Channel database source version changed since migration preflight")
    if not plan["steps"]:
        return plan
    try:
        with closing(sqlite3.connect(_database_uri(path, "rw"), uri=True, isolation_level=None, timeout=0.25)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA busy_timeout = 250")
            connection.execute("BEGIN IMMEDIATE")
            try:
                locked_plan, steps = _plan(connection)
                if locked_plan != plan:
                    raise RuntimeError("Channel database source version changed since migration preflight")
                for step in steps:
                    # The initial source was validated by _plan under this
                    # transaction; each later source is the preceding target.
                    connection.set_authorizer(_migration_authorizer)
                    try:
                        step.apply(connection)
                    finally:
                        connection.set_authorizer(None)
                    if not connection.in_transaction or _version(connection) != step.from_version:
                        raise RuntimeError("migration changed its transaction or schema version")
                    connection.execute("UPDATE schema_version SET version = ?", (step.to_version,))
                    _validate(connection, step.validate_target)
                    _integrity(connection)
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
    except sqlite3.Error as error:
        raise RuntimeError(f"Channel database migration failed: {error}") from error
    return plan
