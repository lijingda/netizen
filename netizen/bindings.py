"""Persistence for Feishu bindings and the Channel SDK dedup extension."""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import TypeVar

from .domain import (
    FeishuScope,
    MentionContextMode,
    MessageContextAnchor,
    ScopeKind,
)


SCHEMA_VERSION = 7
PREVIOUS_SCHEMA_VERSION = 6


_CONTEXT_INTEGRITY_TRIGGERS = (
    """
    CREATE TRIGGER IF NOT EXISTS bindings_context_shape_insert
    BEFORE INSERT ON bindings
    WHEN NOT (
        (
            NEW.message_context_mode = 'current-only'
            AND NEW.context_anchor_message_id IS NULL
            AND NEW.context_anchor_create_time_ms IS NULL
        ) OR (
            NEW.message_context_mode = 'catch-up'
            AND NEW.context_anchor_message_id IS NOT NULL
            AND length(NEW.context_anchor_message_id) > 0
            AND NEW.context_anchor_create_time_ms IS NOT NULL
            AND typeof(NEW.context_anchor_create_time_ms) = 'integer'
            AND NEW.context_anchor_create_time_ms > 0
        )
    )
    BEGIN
        SELECT RAISE(
            ABORT,
            'Binding context must match its mode'
        );
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS bindings_context_shape_update
    BEFORE UPDATE OF message_context_mode,
        context_anchor_message_id, context_anchor_create_time_ms
    ON bindings
    WHEN NOT (
        (
            NEW.message_context_mode = 'current-only'
            AND NEW.context_anchor_message_id IS NULL
            AND NEW.context_anchor_create_time_ms IS NULL
        ) OR (
            NEW.message_context_mode = 'catch-up'
            AND NEW.context_anchor_message_id IS NOT NULL
            AND length(NEW.context_anchor_message_id) > 0
            AND NEW.context_anchor_create_time_ms IS NOT NULL
            AND typeof(NEW.context_anchor_create_time_ms) = 'integer'
            AND NEW.context_anchor_create_time_ms > 0
        )
    )
    BEGIN
        SELECT RAISE(
            ABORT,
            'Binding context must match its mode'
        );
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS bindings_context_scope_insert
    BEFORE INSERT ON bindings
    WHEN NEW.message_context_mode = 'catch-up'
         AND EXISTS (
             SELECT 1
             FROM scopes
             WHERE scope_key = NEW.scope_key
               AND kind = 'direct'
         )
    BEGIN
        SELECT RAISE(
            ABORT,
            'direct Binding cannot use catch-up context'
        );
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS bindings_context_scope_update
    BEFORE UPDATE OF scope_key, message_context_mode,
        context_anchor_message_id, context_anchor_create_time_ms
    ON bindings
    WHEN NEW.message_context_mode = 'catch-up'
         AND EXISTS (
             SELECT 1
             FROM scopes
             WHERE scope_key = NEW.scope_key
               AND kind = 'direct'
         )
    BEGIN
        SELECT RAISE(
            ABORT,
            'direct Binding cannot use catch-up context'
        );
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS scopes_context_kind_update
    BEFORE UPDATE OF kind ON scopes
    WHEN NEW.kind = 'direct'
         AND EXISTS (
             SELECT 1
             FROM bindings
             WHERE scope_key = NEW.scope_key
               AND message_context_mode = 'catch-up'
         )
    BEGIN
        SELECT RAISE(
            ABORT,
            'direct Scope cannot contain catch-up Binding'
        );
    END
    """,
)


class BindingConflict(RuntimeError):
    pass


class BindingNotFound(LookupError):
    pass


class BindingSettingsRevisionConflict(RuntimeError):
    pass


class BindingContextRevisionConflict(RuntimeError):
    pass


class BindingFeedbackRevisionConflict(RuntimeError):
    pass


class AmbiguousBinding(LookupError):
    pass


class ProjectConflict(RuntimeError):
    pass


class ProjectNotFound(LookupError):
    pass


class ProjectRevisionConflict(RuntimeError):
    pass


class ProjectDisabled(ProjectConflict):
    pass


class SideTopicConflict(RuntimeError):
    pass


class SideTopicNotFound(LookupError):
    pass


class BindingQueryBusy(RuntimeError):
    """The bounded Admin query worker already owns its one admission slot."""


class BindingQueryClosed(RuntimeError):
    """The Store is draining or closed and accepts no new Admin query."""


class BindingQueryTimeout(TimeoutError):
    """SQLite interrupted an Admin query at its statement deadline."""


class ScopeConflict(RuntimeError):
    pass


class ScopeNotFound(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class BindingTurnSettings:
    """Catalog selection to apply to every new Turn started by Netizen."""

    model_id: str
    effort_id: str
    service_tier_id: str

    def __post_init__(self) -> None:
        values = (self.model_id, self.effort_id, self.service_tier_id)
        if not all(isinstance(value, str) and value for value in values):
            raise ValueError("Binding Turn settings IDs must not be empty")


@dataclass(frozen=True, slots=True)
class BindingTaskFeedback:
    """Binding-scoped, opt-in pulse/card feedback for Turns."""

    reaction_pulse_enabled: bool = False
    progress_card_enabled: bool = False

    def __post_init__(self) -> None:
        values = (
            self.reaction_pulse_enabled,
            self.progress_card_enabled,
        )
        if not all(type(value) is bool for value in values):
            raise ValueError("Binding task feedback values must be booleans")


@dataclass(frozen=True, slots=True)
class ThreadBinding:
    id: str
    scope_key: str
    project_alias: str
    native_thread_id: str | None
    turn_settings: BindingTurnSettings | None
    settings_revision: int
    creator_id: str
    active: bool
    created_at: str
    activated_at: str | None
    message_context_mode: MentionContextMode = MentionContextMode.CURRENT_ONLY
    context_anchor: MessageContextAnchor | None = None
    context_revision: int = 1
    task_feedback: BindingTaskFeedback = BindingTaskFeedback()
    feedback_revision: int = 1

    @property
    def short_id(self) -> str:
        return self.id[:8]


@dataclass(frozen=True, slots=True)
class ProjectRecord:
    alias: str
    cwd: str
    enabled: bool
    revision: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class ScopeRecord:
    scope_key: str
    app_id: str
    chat_id: str
    kind: ScopeKind
    topic_id: str | None
    active_binding_id: str | None
    updated_at: str

    @property
    def scope(self) -> FeishuScope:
        return FeishuScope(
            app_id=self.app_id,
            chat_id=self.chat_id,
            kind=self.kind,
            topic_id=self.topic_id,
        )


@dataclass(frozen=True, slots=True)
class BindingCursor:
    created_at: str
    binding_id: str

    def __post_init__(self) -> None:
        if not self.created_at or not self.binding_id:
            raise ValueError("Binding cursor fields must not be empty")


@dataclass(frozen=True, slots=True)
class BindingQuery:
    project_alias: str | None = None
    scope_kind: ScopeKind | None = None
    chat_id: str | None = None
    topic_id: str | None = None
    identity: str | None = None
    materialized: bool | None = None
    current: bool | None = None
    created_from: str | None = None
    created_before: str | None = None


@dataclass(frozen=True, slots=True)
class BindingInventoryRecord:
    binding: ThreadBinding
    scope: ScopeRecord


@dataclass(frozen=True, slots=True)
class BindingPage:
    items: tuple[BindingInventoryRecord, ...]
    next_cursor: BindingCursor | None


@dataclass(frozen=True, slots=True)
class SideTopicCursor:
    created_at: str
    side_id: str

    def __post_init__(self) -> None:
        if not self.created_at or not self.side_id:
            raise ValueError("Side Topic cursor fields must not be empty")


@dataclass(frozen=True, slots=True)
class SideTopicQuery:
    project_alias: str | None = None
    parent_binding_id: str | None = None
    app_id: str | None = None
    chat_id: str | None = None
    topic_id: str | None = None
    root_message_id: str | None = None
    state: SideTopicState | None = None
    created_from: str | None = None
    created_before: str | None = None


@dataclass(frozen=True, slots=True)
class SideTopicInventoryRecord:
    side_topic: SideTopicRecord
    project_alias: str | None


@dataclass(frozen=True, slots=True)
class SideTopicPage:
    items: tuple[SideTopicInventoryRecord, ...]
    next_cursor: SideTopicCursor | None


@dataclass(frozen=True, slots=True)
class ProjectAggregate:
    project: ProjectRecord
    binding_count: int
    lazy_binding_count: int
    materialized_binding_count: int
    last_activated_at: str | None


@dataclass(frozen=True, slots=True)
class ProjectAggregatePage:
    items: tuple[ProjectAggregate, ...]
    next_cursor: str | None


class SideTopicState(str, Enum):
    CREATING = "creating"
    OPEN = "open"
    CLOSED = "closed"
    EXPIRED = "expired"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in {
            SideTopicState.CLOSED,
            SideTopicState.EXPIRED,
            SideTopicState.FAILED,
        }


@dataclass(frozen=True, slots=True)
class SideTopicRecord:
    id: str
    app_id: str
    chat_id: str
    topic_id: str | None
    root_message_id: str | None
    source_message_id: str
    parent_binding_id: str
    creator_id: str
    requires_mention: bool
    state: SideTopicState
    created_at: str
    updated_at: str

    @property
    def short_id(self) -> str:
        return self.id[:8]


def migrate_channel_database_v6_to_v7(path: str | Path) -> bool:
    """Upgrade a stopped v6 database inside the installer transaction.

    ``BindingStore`` deliberately remains current-schema-only. The installer
    calls this one-step migration only after the service target is unloaded,
    the stable lifetime lock is held, and rollback files have been captured.
    Existing Bindings receive both task-feedback options disabled.
    """

    database = Path(path)
    if not database.exists():
        return False
    if database.is_symlink() or not database.is_file():
        raise RuntimeError(
            "Channel database migration target must be a regular file"
        )

    connection = sqlite3.connect(database, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 0")
        version_rows = connection.execute(
            "SELECT version FROM schema_version"
        ).fetchall()
        if len(version_rows) != 1:
            raise RuntimeError("Channel database must contain one schema version")
        version = version_rows[0]["version"]
        if version == SCHEMA_VERSION:
            _require_v7_feedback_columns(connection)
            return False
        if version != PREVIOUS_SCHEMA_VERSION:
            raise RuntimeError(
                "unsupported Channel database migration source version: "
                f"{version!r}"
            )

        required_tables = {
            "schema_version",
            "scopes",
            "bindings",
            "projects",
            "dedup_keys",
            "side_topics",
        }
        actual_tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        missing_tables = required_tables - actual_tables
        if missing_tables:
            raise RuntimeError(
                "v6 Channel database is missing required tables: "
                + ", ".join(sorted(missing_tables))
            )
        legacy_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(bindings)"
            ).fetchall()
        }
        required_legacy_columns = {
            "binding_id",
            "scope_key",
            "project_alias",
            "native_thread_id",
            "model_id",
            "effort_id",
            "service_tier_id",
            "settings_revision",
            "creator_id",
            "created_at",
            "activated_at",
            "ever_activated",
            "message_context_mode",
            "context_anchor_message_id",
            "context_anchor_create_time_ms",
            "context_revision",
        }
        missing_columns = required_legacy_columns - legacy_columns
        feedback_columns = {
            "task_reactions_enabled",
            "progress_card_enabled",
            "feedback_revision",
        }
        if missing_columns or legacy_columns & feedback_columns:
            details: list[str] = []
            if missing_columns:
                details.append("missing " + ", ".join(sorted(missing_columns)))
            if legacy_columns & feedback_columns:
                details.append(
                    "unexpected "
                    + ", ".join(sorted(legacy_columns & feedback_columns))
                )
            raise RuntimeError(
                "unexpected v6 bindings schema: " + "; ".join(details)
            )
        _require_database_integrity(connection)
        binding_count = connection.execute(
            "SELECT COUNT(*) FROM bindings"
        ).fetchone()[0]
        side_count = connection.execute(
            "SELECT COUNT(*) FROM side_topics"
        ).fetchone()[0]

        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """
                ALTER TABLE bindings
                ADD COLUMN task_reactions_enabled INTEGER NOT NULL DEFAULT 0
                    CHECK(
                        typeof(task_reactions_enabled) = 'integer'
                        AND task_reactions_enabled IN (0, 1)
                    )
                """
            )
            connection.execute(
                """
                ALTER TABLE bindings
                ADD COLUMN progress_card_enabled INTEGER NOT NULL DEFAULT 0
                    CHECK(
                        typeof(progress_card_enabled) = 'integer'
                        AND progress_card_enabled IN (0, 1)
                    )
                """
            )
            connection.execute(
                """
                ALTER TABLE bindings
                ADD COLUMN feedback_revision INTEGER NOT NULL DEFAULT 1
                    CHECK(
                        typeof(feedback_revision) = 'integer'
                        AND feedback_revision >= 1
                    )
                """
            )
            updated = connection.execute(
                "UPDATE schema_version SET version = ? WHERE version = ?",
                (SCHEMA_VERSION, PREVIOUS_SCHEMA_VERSION),
            )
            if updated.rowcount != 1:
                raise RuntimeError(
                    "Channel database schema version changed during migration"
                )
            _require_database_integrity(connection)
            migrated_binding_count = connection.execute(
                "SELECT COUNT(*) FROM bindings"
            ).fetchone()[0]
            migrated_side_count = connection.execute(
                "SELECT COUNT(*) FROM side_topics"
            ).fetchone()[0]
            if migrated_binding_count != binding_count:
                raise RuntimeError(
                    "Binding count changed during Channel database migration"
                )
            if migrated_side_count != side_count:
                raise RuntimeError(
                    "Side Topic count changed during Channel database migration"
                )
            invalid_feedback_rows = connection.execute(
                """
                SELECT COUNT(*)
                FROM bindings
                WHERE task_reactions_enabled != 0
                   OR progress_card_enabled != 0
                   OR feedback_revision != 1
                """
            ).fetchone()[0]
            if invalid_feedback_rows:
                raise RuntimeError(
                    "legacy Bindings did not receive disabled feedback defaults"
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        _require_v7_feedback_columns(connection)
        _require_database_integrity(connection)
        return True
    except sqlite3.Error as error:
        raise RuntimeError(
            f"Channel database migration failed: {error}"
        ) from error
    finally:
        connection.close()


def _require_v6_context_columns(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(bindings)").fetchall()
    }
    required = {
        "message_context_mode",
        "context_anchor_message_id",
        "context_anchor_create_time_ms",
        "context_revision",
    }
    missing = required - columns
    if missing:
        raise RuntimeError(
            "schema v6 Channel database is missing context columns: "
            + ", ".join(sorted(missing))
        )


def _require_v7_feedback_columns(connection: sqlite3.Connection) -> None:
    _require_v6_context_columns(connection)
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(bindings)").fetchall()
    }
    required = {
        "task_reactions_enabled",
        "progress_card_enabled",
        "feedback_revision",
    }
    missing = required - columns
    if missing:
        raise RuntimeError(
            "schema v7 Channel database is missing feedback columns: "
            + ", ".join(sorted(missing))
        )


def _require_database_integrity(connection: sqlite3.Connection) -> None:
    integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
    if [row[0] for row in integrity_rows] != ["ok"]:
        raise RuntimeError("Channel database integrity check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RuntimeError("Channel database foreign-key check failed")


class BindingStore:
    """Keep only state that native Codex cannot own.

    This object also implements the Channel SDK 1.x ``DedupStore`` protocol:
    ``seen(key)`` and ``mark(key, ttl_seconds)``.

    A file-backed Store owns a WAL writer plus one query-only Admin reader.
    ``:memory:`` is the explicit compatibility/test mode: Admin reads run on
    the same connection under the writer lock, never against a second empty
    in-memory database.
    """

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        id_factory: Callable[[], str] | None = None,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = path
        self._path_string = str(path)
        self._is_memory = self._path_string == ":memory:"
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self._wall_clock = wall_clock
        self._lock = threading.RLock()
        self._query_state_lock = threading.Lock()
        self._query_admission = threading.Lock()
        self._query_futures: set[concurrent.futures.Future[object]] = set()
        self._query_closing = False
        self._closed = False
        self._query_connection: sqlite3.Connection | None = None
        self._query_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="netizen-binding-query",
        )
        if not self._is_memory:
            db_path = Path(path)
            db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self._path_string,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        try:
            self._initialize()
            if not self._is_memory:
                self._query_connection = self._query_executor.submit(
                    self._open_query_connection
                ).result()
        except BaseException:
            self._connection.close()
            self._query_executor.shutdown(wait=True, cancel_futures=True)
            raise
        if not self._is_memory:
            os.chmod(Path(path), 0o600)

    def _initialize(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER NOT NULL
                );
                """
            )
            rows = self._connection.execute(
                "SELECT version FROM schema_version"
            ).fetchall()
            if len(rows) > 1 or (
                rows and rows[0]["version"] != SCHEMA_VERSION
            ):
                raise RuntimeError(
                    "unsupported channel database schema version; "
                    "recreate the Channel database"
                )
            if not self._is_memory:
                journal_mode = self._connection.execute(
                    "PRAGMA journal_mode = WAL"
                ).fetchone()[0]
                if str(journal_mode).lower() != "wal":
                    raise RuntimeError("could not enable WAL for Channel database")
                self._connection.execute("PRAGMA synchronous = FULL")
                self._connection.execute("PRAGMA busy_timeout = 250")
            self._connection.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS scopes (
                    scope_key TEXT PRIMARY KEY,
                    app_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    topic_id TEXT,
                    active_binding_id TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bindings (
                    binding_id TEXT PRIMARY KEY,
                    scope_key TEXT NOT NULL REFERENCES scopes(scope_key),
                    project_alias TEXT NOT NULL,
                    native_thread_id TEXT UNIQUE,
                    model_id TEXT,
                    effort_id TEXT,
                    service_tier_id TEXT,
                    settings_revision INTEGER NOT NULL DEFAULT 1
                        CHECK(settings_revision >= 1),
                    message_context_mode TEXT NOT NULL DEFAULT 'current-only'
                        CHECK(
                            message_context_mode IN ('current-only', 'catch-up')
                        ),
                    context_anchor_message_id TEXT,
                    context_anchor_create_time_ms INTEGER,
                    context_revision INTEGER NOT NULL DEFAULT 1
                        CHECK(
                            typeof(context_revision) = 'integer'
                            AND context_revision >= 1
                        ),
                    task_reactions_enabled INTEGER NOT NULL DEFAULT 0
                        CHECK(
                            typeof(task_reactions_enabled) = 'integer'
                            AND task_reactions_enabled IN (0, 1)
                        ),
                    progress_card_enabled INTEGER NOT NULL DEFAULT 0
                        CHECK(
                            typeof(progress_card_enabled) = 'integer'
                            AND progress_card_enabled IN (0, 1)
                        ),
                    feedback_revision INTEGER NOT NULL DEFAULT 1
                        CHECK(
                            typeof(feedback_revision) = 'integer'
                            AND feedback_revision >= 1
                        ),
                    creator_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    activated_at TEXT NOT NULL,
                    ever_activated INTEGER NOT NULL DEFAULT 1
                        CHECK(ever_activated IN (0, 1)),
                    CHECK(
                        (
                            model_id IS NULL
                            AND effort_id IS NULL
                            AND service_tier_id IS NULL
                        ) OR (
                            model_id IS NOT NULL
                            AND effort_id IS NOT NULL
                            AND service_tier_id IS NOT NULL
                        )
                    ),
                    CHECK(
                        (
                            message_context_mode = 'current-only'
                            AND context_anchor_message_id IS NULL
                            AND context_anchor_create_time_ms IS NULL
                        ) OR (
                            message_context_mode = 'catch-up'
                            AND context_anchor_message_id IS NOT NULL
                            AND length(context_anchor_message_id) > 0
                            AND context_anchor_create_time_ms IS NOT NULL
                            AND typeof(context_anchor_create_time_ms) = 'integer'
                            AND context_anchor_create_time_ms > 0
                        )
                    )
                );
                CREATE INDEX IF NOT EXISTS bindings_by_scope
                    ON bindings(scope_key, activated_at DESC, created_at DESC);
                CREATE TABLE IF NOT EXISTS dedup_keys (
                    dedup_key TEXT PRIMARY KEY,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS side_topics (
                    side_id TEXT PRIMARY KEY,
                    app_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    topic_id TEXT,
                    root_message_id TEXT,
                    source_message_id TEXT NOT NULL,
                    parent_binding_id TEXT NOT NULL,
                    creator_id TEXT NOT NULL,
                    requires_mention INTEGER NOT NULL
                        CHECK(requires_mention IN (0, 1)),
                    state TEXT NOT NULL
                        CHECK(state IN ('creating', 'open', 'closed', 'expired', 'failed')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(app_id, source_message_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS side_topics_by_topic
                    ON side_topics(app_id, chat_id, topic_id)
                    WHERE topic_id IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS side_topics_by_root
                    ON side_topics(app_id, chat_id, root_message_id)
                    WHERE root_message_id IS NOT NULL;
                """
            )
            with self._transaction():
                self._connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS projects (
                        alias TEXT PRIMARY KEY,
                        cwd TEXT NOT NULL,
                        enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
                        revision INTEGER NOT NULL CHECK(revision >= 1),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                binding_columns = {
                    row["name"]
                    for row in self._connection.execute(
                        "PRAGMA table_info(bindings)"
                    ).fetchall()
                }
                if "ever_activated" not in binding_columns:
                    self._connection.execute(
                        """
                        ALTER TABLE bindings
                        ADD COLUMN ever_activated INTEGER NOT NULL DEFAULT 1
                        """
                    )
                self._connection.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS bindings_turn_settings_insert
                    BEFORE INSERT ON bindings
                    WHEN NOT (
                        (
                            NEW.model_id IS NULL
                            AND NEW.effort_id IS NULL
                            AND NEW.service_tier_id IS NULL
                        ) OR (
                            NEW.model_id IS NOT NULL
                            AND NEW.effort_id IS NOT NULL
                            AND NEW.service_tier_id IS NOT NULL
                            AND length(NEW.model_id) > 0
                            AND length(NEW.effort_id) > 0
                            AND length(NEW.service_tier_id) > 0
                        )
                    )
                    BEGIN
                        SELECT RAISE(
                            ABORT,
                            'Binding Turn settings must be all NULL or all set'
                        );
                    END
                    """
                )
                self._connection.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS bindings_turn_settings_update
                    BEFORE UPDATE OF model_id, effort_id, service_tier_id ON bindings
                    WHEN NOT (
                        (
                            NEW.model_id IS NULL
                            AND NEW.effort_id IS NULL
                            AND NEW.service_tier_id IS NULL
                        ) OR (
                            NEW.model_id IS NOT NULL
                            AND NEW.effort_id IS NOT NULL
                            AND NEW.service_tier_id IS NOT NULL
                            AND length(NEW.model_id) > 0
                            AND length(NEW.effort_id) > 0
                            AND length(NEW.service_tier_id) > 0
                        )
                    )
                    BEGIN
                        SELECT RAISE(
                            ABORT,
                            'Binding Turn settings must be all NULL or all set'
                        );
                    END
                    """
                )
                for statement in _CONTEXT_INTEGRITY_TRIGGERS:
                    self._connection.execute(statement)
                self._connection.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS scopes_activate_binding
                    AFTER UPDATE OF active_binding_id ON scopes
                    WHEN NEW.active_binding_id IS NOT NULL
                    BEGIN
                        UPDATE bindings
                        SET ever_activated = 1
                        WHERE binding_id = NEW.active_binding_id
                          AND scope_key = NEW.scope_key;
                    END
                    """
                )
                for statement in _COMPATIBLE_INDEXES:
                    self._connection.execute(statement)
                if not rows:
                    self._connection.execute(
                        "INSERT INTO schema_version(version) VALUES (?)",
                        (SCHEMA_VERSION,),
                    )

    def _open_query_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._path_string,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 0")
            connection.execute("PRAGMA query_only = ON")
        except BaseException:
            connection.close()
            raise
        return connection

    def expire_live_side_topics(self) -> tuple[SideTopicRecord, ...]:
        """Expire non-terminal Side routes at the explicit service boundary.

        Side Sessions and their ephemeral native Thread IDs are intentionally
        not persisted, so a freshly started service cannot resume either a
        ``creating`` or ``open`` route. Merely opening the Store is read-only
        with respect to this lifecycle decision; ``ServiceCore.start`` owns it.
        """

        with self._transaction():
            rows = self._connection.execute(
                _SIDE_TOPIC_SELECT
                + " WHERE state IN ('creating', 'open') ORDER BY created_at, side_id"
            ).fetchall()
            if rows:
                self._connection.execute(
                    """
                    UPDATE side_topics
                    SET state = 'expired', updated_at = ?
                    WHERE state IN ('creating', 'open')
                    """,
                    (_now(),),
                )
        return tuple(self.get_side_topic(row["side_id"]) for row in rows)

    def create_binding(
        self,
        *,
        scope: FeishuScope,
        project_alias: str,
        creator_id: str,
        turn_settings: BindingTurnSettings | None = None,
        task_feedback: BindingTaskFeedback = BindingTaskFeedback(),
        message_context_mode: MentionContextMode = MentionContextMode.CURRENT_ONLY,
        context_anchor: MessageContextAnchor | None = None,
    ) -> ThreadBinding:
        """Compatibility entry point for older callers.

        Production ingress should use :meth:`create_channel_binding`, whose
        Project check is unconditional.  Existing test and migration callers
        that have an entirely empty Project registry retain the historical
        construction seam; once any Project exists, this method applies the
        same enabled-Project requirement as production.
        """

        return self._create_binding(
            scope=scope,
            project_alias=project_alias,
            creator_id=creator_id,
            turn_settings=turn_settings,
            task_feedback=task_feedback,
            message_context_mode=message_context_mode,
            context_anchor=context_anchor,
            expected_project_revision=None,
            activate=True,
            allow_scope_insert=True,
            allow_empty_project_registry=True,
        )

    def create_channel_binding(
        self,
        *,
        scope: FeishuScope,
        project_alias: str,
        creator_id: str,
        expected_project_revision: int | None = None,
        turn_settings: BindingTurnSettings | None = None,
        task_feedback: BindingTaskFeedback = BindingTaskFeedback(),
        message_context_mode: MentionContextMode = MentionContextMode.CURRENT_ONLY,
        context_anchor: MessageContextAnchor | None = None,
    ) -> ThreadBinding:
        """Atomically validate Project state, upsert exact Scope, and activate."""

        if expected_project_revision is not None and expected_project_revision < 1:
            raise ValueError("expected Project revision must be positive")
        return self._create_binding(
            scope=scope,
            project_alias=project_alias,
            creator_id=creator_id,
            turn_settings=turn_settings,
            task_feedback=task_feedback,
            message_context_mode=message_context_mode,
            context_anchor=context_anchor,
            expected_project_revision=expected_project_revision,
            activate=True,
            allow_scope_insert=True,
            allow_empty_project_registry=False,
        )

    def create_admin_binding(
        self,
        *,
        scope: FeishuScope,
        project_alias: str,
        expected_project_revision: int,
        activate: bool = False,
        creator_id: str = "admin:web",
        turn_settings: BindingTurnSettings | None = None,
        task_feedback: BindingTaskFeedback = BindingTaskFeedback(),
    ) -> ThreadBinding:
        """Atomically create a Lazy Binding in an exact existing Scope."""

        if expected_project_revision < 1:
            raise ValueError("expected Project revision must be positive")
        return self._create_binding(
            scope=scope,
            project_alias=project_alias,
            creator_id=creator_id,
            turn_settings=turn_settings,
            task_feedback=task_feedback,
            message_context_mode=MentionContextMode.CURRENT_ONLY,
            context_anchor=None,
            expected_project_revision=expected_project_revision,
            activate=activate,
            allow_scope_insert=False,
            allow_empty_project_registry=False,
        )

    def _create_binding(
        self,
        *,
        scope: FeishuScope,
        project_alias: str,
        creator_id: str,
        turn_settings: BindingTurnSettings | None,
        task_feedback: BindingTaskFeedback,
        message_context_mode: MentionContextMode,
        context_anchor: MessageContextAnchor | None,
        expected_project_revision: int | None,
        activate: bool,
        allow_scope_insert: bool,
        allow_empty_project_registry: bool,
    ) -> ThreadBinding:
        if not project_alias or not creator_id:
            raise ValueError("Binding Project and creator must not be empty")
        _validate_context_state(
            scope_kind=scope.kind,
            mode=message_context_mode,
            anchor=context_anchor,
        )
        binding_id = self._id_factory()
        now = _now()
        settings_values = _settings_values(turn_settings)
        feedback_values = _feedback_values(task_feedback)
        context_values = _context_values(context_anchor)
        with self._transaction():
            scope_row = self._connection.execute(
                _SCOPE_SELECT + " WHERE scope_key = ?",
                (scope.key,),
            ).fetchone()
            if scope_row is None:
                if not allow_scope_insert:
                    raise ScopeNotFound(scope.key)
                self._connection.execute(
                    """
                    INSERT INTO scopes(
                        scope_key, app_id, chat_id, kind, topic_id,
                        active_binding_id, updated_at
                    ) VALUES (?, ?, ?, ?, ?, NULL, ?)
                    """,
                    (
                        scope.key,
                        scope.app_id,
                        scope.chat_id,
                        scope.kind.value,
                        scope.topic_id,
                        now,
                    ),
                )
            else:
                _require_exact_scope(scope_row, scope)
                if allow_scope_insert:
                    self._connection.execute(
                        "UPDATE scopes SET updated_at = ? WHERE scope_key = ?",
                        (now, scope.key),
                    )

            project_row = self._connection.execute(
                _PROJECT_SELECT + " WHERE alias = ?",
                (project_alias,),
            ).fetchone()
            if project_row is None and allow_empty_project_registry:
                any_project = self._connection.execute(
                    "SELECT 1 FROM projects LIMIT 1"
                ).fetchone()
                if any_project is not None:
                    raise ProjectNotFound(project_alias)
            elif project_row is None:
                raise ProjectNotFound(project_alias)
            if project_row is not None:
                if not bool(project_row["enabled"]):
                    raise ProjectDisabled(project_alias)
                if (
                    expected_project_revision is not None
                    and project_row["revision"] != expected_project_revision
                ):
                    raise ProjectRevisionConflict(project_alias)

            self._connection.execute(
                """
                INSERT INTO bindings(
                    binding_id, scope_key, project_alias, native_thread_id,
                    model_id, effort_id, service_tier_id, settings_revision,
                    message_context_mode, context_anchor_message_id,
                    context_anchor_create_time_ms, context_revision,
                    task_reactions_enabled, progress_card_enabled,
                    feedback_revision,
                    creator_id, created_at, activated_at, ever_activated
                ) VALUES (
                    ?, ?, ?, NULL, ?, ?, ?, 1, ?, ?, ?, 1, ?, ?, 1, ?, ?, ?, ?
                )
                """,
                (
                    binding_id,
                    scope.key,
                    project_alias,
                    *settings_values,
                    message_context_mode.value,
                    *context_values,
                    *feedback_values,
                    creator_id,
                    now,
                    now,
                    int(activate),
                ),
            )
            if activate:
                self._connection.execute(
                    """
                    UPDATE scopes
                    SET active_binding_id = ?, updated_at = ?
                    WHERE scope_key = ?
                    """,
                    (binding_id, now, scope.key),
                )
        return self.get(binding_id)

    def set_turn_settings(
        self,
        *,
        binding_id: str,
        expected_revision: int,
        settings: BindingTurnSettings | None,
    ) -> ThreadBinding:
        if expected_revision < 1:
            raise ValueError("expected settings revision must be positive")
        values = _settings_values(settings)
        with self._transaction():
            row = self._connection.execute(
                """
                SELECT model_id, effort_id, service_tier_id, settings_revision
                FROM bindings
                WHERE binding_id = ?
                """,
                (binding_id,),
            ).fetchone()
            if row is None:
                raise BindingNotFound(binding_id)
            if row["settings_revision"] != expected_revision:
                raise BindingSettingsRevisionConflict(binding_id)
            current = (
                row["model_id"],
                row["effort_id"],
                row["service_tier_id"],
            )
            if current == values:
                return self.get(binding_id)
            cursor = self._connection.execute(
                """
                UPDATE bindings
                SET model_id = ?, effort_id = ?, service_tier_id = ?,
                    settings_revision = settings_revision + 1
                WHERE binding_id = ? AND settings_revision = ?
                """,
                (*values, binding_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise BindingSettingsRevisionConflict(binding_id)
        return self.get(binding_id)

    def set_configuration(
        self,
        *,
        binding_id: str,
        expected_settings_revision: int,
        expected_context_revision: int,
        expected_feedback_revision: int,
        settings: BindingTurnSettings | None,
        task_feedback: BindingTaskFeedback,
        message_context_mode: MentionContextMode,
        context_anchor: MessageContextAnchor | None,
    ) -> ThreadBinding:
        """Atomically replace Turn settings, context, and task feedback.

        Supplying an anchor is only meaningful when changing from
        ``current-only`` to ``catch-up``.  A model-only update on an existing
        catch-up Binding retains its exact boundary.
        """

        if expected_settings_revision < 1:
            raise ValueError("expected settings revision must be positive")
        if expected_context_revision < 1:
            raise ValueError("expected context revision must be positive")
        if expected_feedback_revision < 1:
            raise ValueError("expected feedback revision must be positive")
        if not isinstance(message_context_mode, MentionContextMode):
            raise ValueError("message context mode must be a MentionContextMode")
        settings_values = _settings_values(settings)
        feedback_values = _feedback_values(task_feedback)
        with self._transaction():
            row = self._connection.execute(
                """
                SELECT
                    b.model_id, b.effort_id, b.service_tier_id,
                    b.settings_revision, b.message_context_mode,
                    b.context_anchor_message_id,
                    b.context_anchor_create_time_ms, b.context_revision,
                    b.task_reactions_enabled, b.progress_card_enabled,
                    b.feedback_revision,
                    s.kind AS scope_kind
                FROM bindings b
                JOIN scopes s ON s.scope_key = b.scope_key
                WHERE b.binding_id = ?
                """,
                (binding_id,),
            ).fetchone()
            if row is None:
                raise BindingNotFound(binding_id)
            if row["settings_revision"] != expected_settings_revision:
                raise BindingSettingsRevisionConflict(binding_id)
            if row["context_revision"] != expected_context_revision:
                raise BindingContextRevisionConflict(binding_id)
            if row["feedback_revision"] != expected_feedback_revision:
                raise BindingFeedbackRevisionConflict(binding_id)

            current_settings = (
                row["model_id"],
                row["effort_id"],
                row["service_tier_id"],
            )
            current_mode = _mention_context_mode(row["message_context_mode"])
            current_anchor = _message_context_anchor(
                row["context_anchor_message_id"],
                row["context_anchor_create_time_ms"],
            )
            scope_kind = ScopeKind(row["scope_kind"])
            if message_context_mode is current_mode:
                if context_anchor is not None:
                    raise ValueError(
                        "context anchor may only reset when context mode changes"
                    )
                next_anchor = current_anchor
                context_changed = False
            else:
                next_anchor = context_anchor
                context_changed = True
            _validate_context_state(
                scope_kind=scope_kind,
                mode=message_context_mode,
                anchor=next_anchor,
            )

            settings_changed = current_settings != settings_values
            current_feedback = (
                row["task_reactions_enabled"],
                row["progress_card_enabled"],
            )
            feedback_changed = current_feedback != tuple(
                int(value) for value in feedback_values
            )
            if (
                not settings_changed
                and not context_changed
                and not feedback_changed
            ):
                return self.get(binding_id)
            anchor_values = _context_values(next_anchor)
            cursor = self._connection.execute(
                """
                UPDATE bindings
                SET model_id = ?, effort_id = ?, service_tier_id = ?,
                    settings_revision = settings_revision + ?,
                    message_context_mode = ?,
                    context_anchor_message_id = ?,
                    context_anchor_create_time_ms = ?,
                    context_revision = context_revision + ?,
                    task_reactions_enabled = ?,
                    progress_card_enabled = ?,
                    feedback_revision = feedback_revision + ?
                WHERE binding_id = ?
                  AND settings_revision = ?
                  AND context_revision = ?
                  AND feedback_revision = ?
                """,
                (
                    *settings_values,
                    int(settings_changed),
                    message_context_mode.value,
                    *anchor_values,
                    int(context_changed),
                    *(int(value) for value in feedback_values),
                    int(feedback_changed),
                    binding_id,
                    expected_settings_revision,
                    expected_context_revision,
                    expected_feedback_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise BindingConflict("Binding configuration changed concurrently")
        return self.get(binding_id)

    def commit_context_anchor(
        self,
        *,
        binding_id: str,
        expected_context_revision: int,
        anchor: MessageContextAnchor,
    ) -> ThreadBinding:
        """Advance one catch-up boundary after native submission is accepted."""

        if expected_context_revision < 1:
            raise ValueError("expected context revision must be positive")
        anchor_values = _context_values(anchor)
        with self._transaction():
            row = self._connection.execute(
                """
                SELECT b.message_context_mode, b.context_revision,
                       s.kind AS scope_kind
                FROM bindings b
                JOIN scopes s ON s.scope_key = b.scope_key
                WHERE b.binding_id = ?
                """,
                (binding_id,),
            ).fetchone()
            if row is None:
                raise BindingNotFound(binding_id)
            if row["context_revision"] != expected_context_revision:
                raise BindingContextRevisionConflict(binding_id)
            mode = _mention_context_mode(row["message_context_mode"])
            if mode is not MentionContextMode.CATCH_UP:
                raise BindingConflict(
                    "current-only Binding has no context anchor to commit"
                )
            _validate_context_state(
                scope_kind=ScopeKind(row["scope_kind"]),
                mode=mode,
                anchor=anchor,
            )
            cursor = self._connection.execute(
                """
                UPDATE bindings
                SET context_anchor_message_id = ?,
                    context_anchor_create_time_ms = ?,
                    context_revision = context_revision + 1
                WHERE binding_id = ? AND context_revision = ?
                """,
                (*anchor_values, binding_id, expected_context_revision),
            )
            if cursor.rowcount != 1:
                raise BindingContextRevisionConflict(binding_id)
        return self.get(binding_id)

    def activate(
        self,
        *,
        scope_key: str,
        binding_id: str,
        context_anchor: MessageContextAnchor | None = None,
    ) -> ThreadBinding:
        now = _now()
        with self._transaction():
            owner = self._connection.execute(
                """
                SELECT b.scope_key, b.message_context_mode,
                       s.kind AS scope_kind
                FROM bindings b
                JOIN scopes s ON s.scope_key = b.scope_key
                WHERE b.binding_id = ?
                """,
                (binding_id,),
            ).fetchone()
            if owner is None or owner["scope_key"] != scope_key:
                raise BindingNotFound(binding_id)
            mode = _mention_context_mode(owner["message_context_mode"])
            _validate_context_state(
                scope_kind=ScopeKind(owner["scope_kind"]),
                mode=mode,
                anchor=context_anchor,
            )
            if mode is MentionContextMode.CATCH_UP:
                anchor_values = _context_values(context_anchor)
                self._connection.execute(
                    """
                    UPDATE bindings
                    SET activated_at = ?, ever_activated = 1,
                        context_anchor_message_id = ?,
                        context_anchor_create_time_ms = ?,
                        context_revision = context_revision + 1
                    WHERE binding_id = ?
                    """,
                    (now, *anchor_values, binding_id),
                )
            else:
                self._connection.execute(
                    """
                    UPDATE bindings
                    SET activated_at = ?, ever_activated = 1
                    WHERE binding_id = ?
                    """,
                    (now, binding_id),
                )
            self._connection.execute(
                """
                UPDATE scopes
                SET active_binding_id = ?, updated_at = ?
                WHERE scope_key = ?
                """,
                (binding_id, now, scope_key),
            )
        return self.get(binding_id)

    def deactivate(self, *, scope_key: str, binding_id: str) -> ThreadBinding:
        """Clear the active pointer only when it still targets this Binding."""

        with self._transaction():
            row = self._connection.execute(
                "SELECT active_binding_id FROM scopes WHERE scope_key = ?",
                (scope_key,),
            ).fetchone()
            if row is None or row["active_binding_id"] != binding_id:
                raise BindingConflict("Binding is no longer active in this Scope")
            owner = self._connection.execute(
                "SELECT scope_key FROM bindings WHERE binding_id = ?",
                (binding_id,),
            ).fetchone()
            if owner is None or owner["scope_key"] != scope_key:
                raise BindingNotFound(binding_id)
            self._connection.execute(
                """
                UPDATE scopes
                SET active_binding_id = NULL, updated_at = ?
                WHERE scope_key = ? AND active_binding_id = ?
                """,
                (_now(), scope_key, binding_id),
            )
        return self.get(binding_id)

    def deactivate_if_active(
        self,
        *,
        scope_key: str,
        binding_id: str,
    ) -> ThreadBinding:
        """Clear this Binding's Scope pointer when it is still current.

        Exact management operations may archive an inactive Binding.  They
        must still validate ownership, but must not disturb the Scope's actual
        current pointer merely because another Binding was the mutation target.
        """

        with self._transaction():
            owner = self._connection.execute(
                "SELECT scope_key FROM bindings WHERE binding_id = ?",
                (binding_id,),
            ).fetchone()
            if owner is None or owner["scope_key"] != scope_key:
                raise BindingNotFound(binding_id)
            self._connection.execute(
                """
                UPDATE scopes
                SET active_binding_id = NULL, updated_at = ?
                WHERE scope_key = ? AND active_binding_id = ?
                """,
                (_now(), scope_key, binding_id),
            )
        return self.get(binding_id)

    def delete_binding(self, binding_id: str) -> ThreadBinding:
        """Delete one Binding and clear its Scope pointer in one transaction."""

        with self._transaction():
            row = self._connection.execute(
                _BINDING_SELECT + " WHERE b.binding_id = ?",
                (binding_id,),
            ).fetchone()
            if row is None:
                raise BindingNotFound(binding_id)
            binding = _binding(row)
            self._connection.execute(
                """
                UPDATE scopes
                SET active_binding_id = NULL, updated_at = ?
                WHERE scope_key = ? AND active_binding_id = ?
                """,
                (_now(), binding.scope_key, binding.id),
            )
            cursor = self._connection.execute(
                "DELETE FROM bindings WHERE binding_id = ?",
                (binding.id,),
            )
            if cursor.rowcount != 1:
                raise BindingNotFound(binding.id)
        return binding

    def assign_native_thread_id(self, binding_id: str, native_thread_id: str) -> None:
        if not native_thread_id:
            raise ValueError("native_thread_id must not be empty")
        with self._transaction():
            row = self._connection.execute(
                "SELECT native_thread_id FROM bindings WHERE binding_id = ?",
                (binding_id,),
            ).fetchone()
            if row is None:
                raise BindingNotFound(binding_id)
            existing = row["native_thread_id"]
            if existing == native_thread_id:
                return
            if existing is not None:
                raise BindingConflict("native Thread ID is write-once")
            try:
                self._connection.execute(
                    """
                    UPDATE bindings SET native_thread_id = ?
                    WHERE binding_id = ? AND native_thread_id IS NULL
                    """,
                    (native_thread_id, binding_id),
                )
            except sqlite3.IntegrityError as error:
                raise BindingConflict(
                    "native Thread is already bound to another Feishu session"
                ) from error

    def active_binding(self, scope_key: str) -> ThreadBinding | None:
        with self._lock:
            row = self._connection.execute(
                _BINDING_SELECT
                + " WHERE b.binding_id = s.active_binding_id AND s.scope_key = ?",
                (scope_key,),
            ).fetchone()
        return _binding(row) if row is not None else None

    def get_scope(self, scope_key: str) -> ScopeRecord:
        with self._lock:
            row = self._connection.execute(
                _SCOPE_SELECT + " WHERE scope_key = ?",
                (scope_key,),
            ).fetchone()
        if row is None:
            raise ScopeNotFound(scope_key)
        return _scope_record(row)

    def get(self, binding_id: str) -> ThreadBinding:
        with self._lock:
            row = self._connection.execute(
                _BINDING_SELECT + " WHERE b.binding_id = ?",
                (binding_id,),
            ).fetchone()
        if row is None:
            raise BindingNotFound(binding_id)
        return _binding(row)

    def list_bindings(self, scope_key: str) -> list[ThreadBinding]:
        with self._lock:
            rows = self._connection.execute(
                _BINDING_SELECT
                + " WHERE b.scope_key = ? "
                "ORDER BY b.ever_activated DESC, b.activated_at DESC, b.created_at DESC",
                (scope_key,),
            ).fetchall()
        return [_binding(row) for row in rows]

    def resolve_reference(self, *, scope_key: str, reference: str) -> ThreadBinding:
        candidates = [
            binding
            for binding in self.list_bindings(scope_key)
            if binding.id == reference or binding.id.startswith(reference)
        ]
        if not candidates:
            raise BindingNotFound(reference)
        if len(candidates) != 1:
            raise AmbiguousBinding(reference)
        return candidates[0]

    def bootstrap_project(self, *, alias: str, cwd: str) -> ProjectRecord:
        now = _now()
        with self._transaction():
            self._connection.execute(
                """
                INSERT OR IGNORE INTO projects(
                    alias, cwd, enabled, revision, created_at, updated_at
                ) VALUES (?, ?, 1, 1, ?, ?)
                """,
                (alias, cwd, now, now),
            )
        return self.get_project(alias)

    def register_project(self, *, alias: str, cwd: str) -> ProjectRecord:
        now = _now()
        try:
            with self._transaction():
                self._connection.execute(
                    """
                    INSERT INTO projects(
                        alias, cwd, enabled, revision, created_at, updated_at
                    ) VALUES (?, ?, 1, 1, ?, ?)
                    """,
                    (alias, cwd, now, now),
                )
        except sqlite3.IntegrityError as error:
            raise ProjectConflict(alias) from error
        return self.get_project(alias)

    def get_project(self, alias: str) -> ProjectRecord:
        with self._lock:
            row = self._connection.execute(
                _PROJECT_SELECT + " WHERE alias = ?",
                (alias,),
            ).fetchone()
        if row is None:
            raise ProjectNotFound(alias)
        return _project_record(row)

    def list_projects(self) -> list[ProjectRecord]:
        with self._lock:
            rows = self._connection.execute(
                _PROJECT_SELECT + " ORDER BY alias"
            ).fetchall()
        return [_project_record(row) for row in rows]

    def set_project_enabled(
        self,
        *,
        alias: str,
        enabled: bool,
        expected_revision: int,
    ) -> ProjectRecord:
        with self._transaction():
            row = self._connection.execute(
                _PROJECT_SELECT + " WHERE alias = ?",
                (alias,),
            ).fetchone()
            if row is None:
                raise ProjectNotFound(alias)
            if row["revision"] != expected_revision:
                raise ProjectRevisionConflict(alias)
            if bool(row["enabled"]) == enabled:
                return _project_record(row)
            self._connection.execute(
                """
                UPDATE projects
                SET enabled = ?, revision = revision + 1, updated_at = ?
                WHERE alias = ? AND revision = ?
                """,
                (int(enabled), _now(), alias, expected_revision),
            )
        return self.get_project(alias)

    def create_side_topic(
        self,
        *,
        app_id: str,
        chat_id: str,
        source_message_id: str,
        parent_binding_id: str,
        creator_id: str,
        requires_mention: bool,
    ) -> SideTopicRecord:
        values = (
            app_id,
            chat_id,
            source_message_id,
            parent_binding_id,
            creator_id,
        )
        if not all(isinstance(value, str) and value for value in values):
            raise ValueError("Side Topic identity fields must not be empty")
        if not isinstance(requires_mention, bool):
            raise ValueError("requires_mention must be boolean")
        existing = self.side_topic_for_source(
            app_id=app_id,
            source_message_id=source_message_id,
        )
        if existing is not None:
            _require_same_side_reservation(
                existing,
                chat_id=chat_id,
                parent_binding_id=parent_binding_id,
                creator_id=creator_id,
                requires_mention=requires_mention,
            )
            return existing
        side_id = self._id_factory()
        now = _now()
        try:
            with self._transaction():
                self._connection.execute(
                    """
                    INSERT INTO side_topics(
                        side_id, app_id, chat_id, topic_id, root_message_id,
                        source_message_id, parent_binding_id, creator_id,
                        requires_mention, state, created_at, updated_at
                    ) VALUES (?, ?, ?, NULL, NULL, ?, ?, ?, ?, 'creating', ?, ?)
                    """,
                    (
                        side_id,
                        app_id,
                        chat_id,
                        source_message_id,
                        parent_binding_id,
                        creator_id,
                        int(requires_mention),
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as error:
            existing = self.side_topic_for_source(
                app_id=app_id,
                source_message_id=source_message_id,
            )
            if existing is not None:
                _require_same_side_reservation(
                    existing,
                    chat_id=chat_id,
                    parent_binding_id=parent_binding_id,
                    creator_id=creator_id,
                    requires_mention=requires_mention,
                )
                return existing
            raise SideTopicConflict("Side Topic identity is already reserved") from error
        return self.get_side_topic(side_id)

    def set_side_topic_root(
        self,
        side_id: str,
        root_message_id: str,
    ) -> SideTopicRecord:
        if not root_message_id:
            raise ValueError("root_message_id must not be empty")
        with self._transaction():
            row = self._connection.execute(
                "SELECT root_message_id, state FROM side_topics WHERE side_id = ?",
                (side_id,),
            ).fetchone()
            if row is None:
                raise SideTopicNotFound(side_id)
            if row["root_message_id"] == root_message_id:
                return self.get_side_topic(side_id)
            if row["root_message_id"] is not None or row["state"] != "creating":
                raise SideTopicConflict("Side Topic root is write-once while creating")
            try:
                self._connection.execute(
                    """
                    UPDATE side_topics
                    SET root_message_id = ?, updated_at = ?
                    WHERE side_id = ?
                    """,
                    (root_message_id, _now(), side_id),
                )
            except sqlite3.IntegrityError as error:
                raise SideTopicConflict("Side Topic root is already reserved") from error
        return self.get_side_topic(side_id)

    def open_side_topic(self, side_id: str, topic_id: str) -> SideTopicRecord:
        if not topic_id:
            raise ValueError("topic_id must not be empty")
        with self._transaction():
            row = self._connection.execute(
                """
                SELECT topic_id, root_message_id, state
                FROM side_topics WHERE side_id = ?
                """,
                (side_id,),
            ).fetchone()
            if row is None:
                raise SideTopicNotFound(side_id)
            if row["state"] == "open" and row["topic_id"] == topic_id:
                return self.get_side_topic(side_id)
            if (
                row["state"] != "creating"
                or row["root_message_id"] is None
                or row["topic_id"] not in {None, topic_id}
            ):
                raise SideTopicConflict("Side Topic cannot transition to open")
            try:
                self._connection.execute(
                    """
                    UPDATE side_topics
                    SET topic_id = ?, state = 'open', updated_at = ?
                    WHERE side_id = ?
                    """,
                    (topic_id, _now(), side_id),
                )
            except sqlite3.IntegrityError as error:
                raise SideTopicConflict("Side Topic is already reserved") from error
        return self.get_side_topic(side_id)

    def set_side_topic_topic(
        self,
        side_id: str,
        topic_id: str,
    ) -> SideTopicRecord:
        if not topic_id:
            raise ValueError("topic_id must not be empty")
        with self._transaction():
            row = self._connection.execute(
                """
                SELECT topic_id, root_message_id, state
                FROM side_topics WHERE side_id = ?
                """,
                (side_id,),
            ).fetchone()
            if row is None:
                raise SideTopicNotFound(side_id)
            if row["topic_id"] == topic_id:
                return self.get_side_topic(side_id)
            if (
                row["state"] != "creating"
                or row["root_message_id"] is None
                or row["topic_id"] is not None
            ):
                raise SideTopicConflict(
                    "Side Topic ID is write-once after its root is known"
                )
            try:
                self._connection.execute(
                    """
                    UPDATE side_topics
                    SET topic_id = ?, updated_at = ?
                    WHERE side_id = ?
                    """,
                    (topic_id, _now(), side_id),
                )
            except sqlite3.IntegrityError as error:
                raise SideTopicConflict("Side Topic ID is already reserved") from error
        return self.get_side_topic(side_id)

    def transition_side_topic(
        self,
        side_id: str,
        state: SideTopicState,
    ) -> SideTopicRecord:
        if not state.terminal:
            raise ValueError("Side Topic transition target must be terminal")
        with self._transaction():
            row = self._connection.execute(
                "SELECT state FROM side_topics WHERE side_id = ?",
                (side_id,),
            ).fetchone()
            if row is None:
                raise SideTopicNotFound(side_id)
            current = SideTopicState(row["state"])
            if current is state:
                return self.get_side_topic(side_id)
            if current.terminal:
                raise SideTopicConflict(
                    f"Side Topic is already terminal: {current.value}"
                )
            self._connection.execute(
                """
                UPDATE side_topics SET state = ?, updated_at = ?
                WHERE side_id = ?
                """,
                (state.value, _now(), side_id),
            )
        return self.get_side_topic(side_id)

    def touch_side_topic(self, side_id: str) -> SideTopicRecord:
        with self._transaction():
            cursor = self._connection.execute(
                """
                UPDATE side_topics SET updated_at = ?
                WHERE side_id = ? AND state = 'open'
                """,
                (_now(), side_id),
            )
            if cursor.rowcount != 1:
                row = self._connection.execute(
                    "SELECT 1 FROM side_topics WHERE side_id = ?",
                    (side_id,),
                ).fetchone()
                if row is None:
                    raise SideTopicNotFound(side_id)
                raise SideTopicConflict("Side Topic is not open")
        return self.get_side_topic(side_id)

    def get_side_topic(self, side_id: str) -> SideTopicRecord:
        with self._lock:
            row = self._connection.execute(
                _SIDE_TOPIC_SELECT + " WHERE side_id = ?",
                (side_id,),
            ).fetchone()
        if row is None:
            raise SideTopicNotFound(side_id)
        return _side_topic(row)

    def side_topic_for_source(
        self,
        *,
        app_id: str,
        source_message_id: str,
    ) -> SideTopicRecord | None:
        with self._lock:
            row = self._connection.execute(
                _SIDE_TOPIC_SELECT
                + " WHERE app_id = ? AND source_message_id = ?",
                (app_id, source_message_id),
            ).fetchone()
        return _side_topic(row) if row is not None else None

    def side_topic_for_message(
        self,
        *,
        app_id: str,
        chat_id: str,
        topic_id: str | None,
        root_message_id: str | None = None,
    ) -> SideTopicRecord | None:
        topic_record: SideTopicRecord | None = None
        root_record: SideTopicRecord | None = None
        with self._lock:
            if topic_id:
                row = self._connection.execute(
                    _SIDE_TOPIC_SELECT
                    + " WHERE app_id = ? AND chat_id = ? AND topic_id = ?",
                    (app_id, chat_id, topic_id),
                ).fetchone()
                if row is not None:
                    topic_record = _side_topic(row)
            if root_message_id:
                row = self._connection.execute(
                    _SIDE_TOPIC_SELECT
                    + " WHERE app_id = ? AND chat_id = ? AND root_message_id = ?",
                    (app_id, chat_id, root_message_id),
                ).fetchone()
                if row is not None:
                    root_record = _side_topic(row)

        if topic_record is not None:
            if root_message_id is None:
                return topic_record
            if root_record is None or root_record.id != topic_record.id:
                raise SideTopicConflict(
                    "Side topic and root message identities do not match"
                )
            return topic_record
        if root_record is not None:
            if topic_id is not None and root_record.topic_id is not None:
                raise SideTopicConflict(
                    "Side root message belongs to a different known topic"
                )
            return root_record
        return None

    def list_side_topics(self) -> list[SideTopicRecord]:
        with self._lock:
            rows = self._connection.execute(
                _SIDE_TOPIC_SELECT + " ORDER BY created_at, side_id"
            ).fetchall()
        return [_side_topic(row) for row in rows]

    async def query_bindings(
        self,
        *,
        query: BindingQuery = BindingQuery(),
        cursor: BindingCursor | None = None,
        limit: int = 25,
        deadline_seconds: float = 0.5,
    ) -> BindingPage:
        """Read one global Binding inventory page off the shared event loop."""

        page_limit = _validate_page_limit(limit, maximum=100)
        statement, parameters = _binding_inventory_statement(
            query=query,
            cursor=cursor,
            limit=page_limit + 1,
        )
        rows = await self._read_rows(
            statement,
            parameters,
            deadline_seconds=deadline_seconds,
        )
        has_more = len(rows) > page_limit
        selected = rows[:page_limit]
        items = tuple(_binding_inventory(row) for row in selected)
        next_cursor = None
        if has_more and selected:
            last = selected[-1]
            next_cursor = BindingCursor(last["created_at"], last["binding_id"])
        return BindingPage(items=items, next_cursor=next_cursor)

    async def query_side_topics(
        self,
        *,
        query: SideTopicQuery = SideTopicQuery(),
        cursor: SideTopicCursor | None = None,
        limit: int = 25,
        deadline_seconds: float = 0.5,
    ) -> SideTopicPage:
        """Read one global Side Topic inventory page off the event loop."""

        page_limit = _validate_page_limit(limit)
        statement, parameters = _side_inventory_statement(
            query=query,
            cursor=cursor,
            limit=page_limit + 1,
        )
        rows = await self._read_rows(
            statement,
            parameters,
            deadline_seconds=deadline_seconds,
        )
        has_more = len(rows) > page_limit
        selected = rows[:page_limit]
        items = tuple(_side_inventory(row) for row in selected)
        next_cursor = None
        if has_more and selected:
            last = selected[-1]
            next_cursor = SideTopicCursor(last["created_at"], last["side_id"])
        return SideTopicPage(items=items, next_cursor=next_cursor)

    async def query_project_aggregates(
        self,
        *,
        cursor: str | None = None,
        limit: int = 25,
        deadline_seconds: float = 0.5,
    ) -> ProjectAggregatePage:
        """Read Project rows with Channel-owned Binding aggregates."""

        page_limit = _validate_page_limit(limit)
        where = ""
        parameters: list[object] = []
        if cursor is not None:
            if not cursor:
                raise ValueError("Project cursor must not be empty")
            where = " WHERE p.alias > ?"
            parameters.append(cursor)
        parameters.append(page_limit + 1)
        rows = await self._read_rows(
            _PROJECT_AGGREGATE_SELECT
            + where
            + " GROUP BY p.alias ORDER BY p.alias LIMIT ?",
            parameters,
            deadline_seconds=deadline_seconds,
        )
        has_more = len(rows) > page_limit
        selected = rows[:page_limit]
        items = tuple(_project_aggregate(row) for row in selected)
        next_cursor = selected[-1]["alias"] if has_more and selected else None
        return ProjectAggregatePage(items=items, next_cursor=next_cursor)

    async def project_aliases_for_native_threads(
        self,
        thread_ids: Sequence[str],
        *,
        deadline_seconds: float = 0.5,
    ) -> dict[str, str]:
        """Map a bounded native catalog snapshot back to Channel Projects."""

        unique = tuple(dict.fromkeys(thread_ids))
        if len(unique) > 100_000:
            raise ValueError("native Thread lookup exceeds the bounded catalog limit")
        if any(not isinstance(thread_id, str) or not thread_id for thread_id in unique):
            raise ValueError("native Thread IDs must be non-empty strings")
        if not unique:
            return {}

        def operation(connection: sqlite3.Connection) -> dict[str, str]:
            result: dict[str, str] = {}
            for offset in range(0, len(unique), 500):
                chunk = unique[offset : offset + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    "SELECT native_thread_id, project_alias FROM bindings "
                    f"WHERE native_thread_id IN ({placeholders})",
                    chunk,
                ).fetchall()
                result.update(
                    (row["native_thread_id"], row["project_alias"]) for row in rows
                )
            return result

        return await self._submit_query(
            operation,
            deadline_seconds=deadline_seconds,
        )

    async def _read_rows(
        self,
        statement: str,
        parameters: Sequence[object] = (),
        *,
        deadline_seconds: float,
    ) -> list[sqlite3.Row]:
        if deadline_seconds <= 0:
            raise ValueError("query deadline must be positive")

        def operation(connection: sqlite3.Connection) -> list[sqlite3.Row]:
            return connection.execute(statement, tuple(parameters)).fetchall()

        result = await self._submit_query(
            operation,
            deadline_seconds=deadline_seconds,
        )
        return result

    async def _submit_query(
        self,
        operation: Callable[[sqlite3.Connection], "_QueryResult"],
        *,
        deadline_seconds: float,
    ) -> "_QueryResult":
        if not self._query_admission.acquire(blocking=False):
            raise BindingQueryBusy("Admin query reader is busy")
        with self._query_state_lock:
            if self._query_closing:
                self._query_admission.release()
                raise BindingQueryClosed("Binding Store query reader is closed")
            try:
                future = self._query_executor.submit(
                    self._execute_query,
                    operation,
                    deadline_seconds,
                )
            except BaseException:
                self._query_admission.release()
                raise
            self._query_futures.add(future)
        future.add_done_callback(self._query_finished)
        return await asyncio.shield(asyncio.wrap_future(future))

    def _execute_query(
        self,
        operation: Callable[[sqlite3.Connection], "_QueryResult"],
        deadline_seconds: float,
    ) -> "_QueryResult":
        deadline = time.monotonic() + deadline_seconds
        expired = False

        def progress() -> int:
            nonlocal expired
            expired = time.monotonic() >= deadline
            return int(expired)

        connection = self._query_connection or self._connection
        lock = self._lock if self._query_connection is None else nullcontext()
        with lock:
            connection.set_progress_handler(progress, 100)
            try:
                return operation(connection)
            except sqlite3.OperationalError as error:
                message = str(error).lower()
                if expired or "interrupted" in message:
                    raise BindingQueryTimeout(
                        "Binding Store query exceeded its deadline"
                    ) from error
                if "locked" in message or "busy" in message:
                    raise BindingQueryBusy("Binding Store query reader is busy") from error
                raise
            finally:
                connection.set_progress_handler(None, 0)

    def _query_finished(
        self,
        future: concurrent.futures.Future[object],
    ) -> None:
        with self._query_state_lock:
            self._query_futures.discard(future)
        self._query_admission.release()

    async def drain_queries(self) -> None:
        """Wait for every already-submitted Admin read, including cancelled callers."""

        while True:
            with self._query_state_lock:
                futures = tuple(self._query_futures)
            if not futures:
                return
            await asyncio.gather(
                *(asyncio.wrap_future(future) for future in futures),
                return_exceptions=True,
            )

    async def aclose(self) -> None:
        """Close query admission, drain it, then close both owned connections."""

        with self._query_state_lock:
            if self._closed:
                return
            self._query_closing = True
        await self.drain_queries()
        if self._query_connection is not None:
            await asyncio.wrap_future(
                self._query_executor.submit(self._query_connection.close)
            )
            self._query_connection = None
        await asyncio.to_thread(
            self._query_executor.shutdown,
            wait=True,
            cancel_futures=False,
        )
        with self._lock:
            self._connection.close()
        with self._query_state_lock:
            self._closed = True

    def seen(self, key: str) -> bool:
        now = self._wall_clock()
        with self._lock:
            row = self._connection.execute(
                "SELECT expires_at FROM dedup_keys WHERE dedup_key = ?",
                (key,),
            ).fetchone()
            if row is None:
                return False
            if row["expires_at"] <= now:
                self._connection.execute(
                    "DELETE FROM dedup_keys WHERE dedup_key = ?",
                    (key,),
                )
                return False
            return True

    def mark(self, key: str, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        now = self._wall_clock()
        with self._lock:
            self._connection.execute(
                "DELETE FROM dedup_keys WHERE expires_at <= ?",
                (now,),
            )
            self._connection.execute(
                """
                INSERT INTO dedup_keys(dedup_key, expires_at) VALUES (?, ?)
                ON CONFLICT(dedup_key) DO UPDATE SET expires_at = excluded.expires_at
                """,
                (key, now + ttl_seconds),
            )

    def close(self) -> None:
        """Synchronous compatibility close; async services should await ``aclose``."""

        with self._query_state_lock:
            if self._closed:
                return
            self._query_closing = True
            futures = tuple(self._query_futures)
        for future in futures:
            try:
                future.result()
            except BaseException:
                pass
        if self._query_connection is not None:
            self._query_executor.submit(self._query_connection.close).result()
            self._query_connection = None
        self._query_executor.shutdown(wait=True, cancel_futures=False)
        with self._lock:
            self._connection.close()
        with self._query_state_lock:
            self._closed = True

    def _transaction(self):
        return _Transaction(self._connection, self._lock)


class _Transaction:
    def __init__(self, connection: sqlite3.Connection, lock: threading.RLock) -> None:
        self._connection = connection
        self._lock = lock

    def __enter__(self) -> None:
        self._lock.acquire()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
        except BaseException:
            self._lock.release()
            raise

    def __exit__(self, exc_type, _exc, _tb) -> None:
        try:
            self._connection.execute("COMMIT" if exc_type is None else "ROLLBACK")
        finally:
            self._lock.release()


_QueryResult = TypeVar("_QueryResult")


_COMPATIBLE_INDEXES = (
    """
    CREATE INDEX IF NOT EXISTS bindings_global_created
    ON bindings(created_at DESC, binding_id DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS bindings_project_created
    ON bindings(project_alias, created_at DESC, binding_id DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS bindings_scope_created
    ON bindings(scope_key, created_at DESC, binding_id DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS scopes_kind_chat_topic
    ON scopes(kind, chat_id, topic_id, scope_key)
    """,
    """
    CREATE INDEX IF NOT EXISTS scopes_chat_topic_kind
    ON scopes(chat_id, topic_id, kind, scope_key)
    """,
    """
    CREATE INDEX IF NOT EXISTS side_topics_global_created
    ON side_topics(created_at DESC, side_id DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS side_topics_state_created
    ON side_topics(state, created_at DESC, side_id DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS side_topics_chat_topic_created
    ON side_topics(chat_id, topic_id, created_at DESC, side_id DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS side_topics_parent_created
    ON side_topics(parent_binding_id, created_at DESC, side_id DESC)
    """,
)


_BINDING_SELECT = """
    SELECT
        b.binding_id,
        b.scope_key,
        b.project_alias,
        b.native_thread_id,
        b.model_id,
        b.effort_id,
        b.service_tier_id,
        b.settings_revision,
        b.message_context_mode,
        b.context_anchor_message_id,
        b.context_anchor_create_time_ms,
        b.context_revision,
        b.task_reactions_enabled,
        b.progress_card_enabled,
        b.feedback_revision,
        b.creator_id,
        b.created_at,
        b.activated_at,
        b.ever_activated,
        CASE WHEN s.active_binding_id = b.binding_id THEN 1 ELSE 0 END AS active
    FROM bindings b
    JOIN scopes s ON s.scope_key = b.scope_key
"""


_SCOPE_SELECT = """
    SELECT
        scope_key, app_id, chat_id, kind, topic_id,
        active_binding_id, updated_at
    FROM scopes
"""


_PROJECT_SELECT = """
    SELECT alias, cwd, enabled, revision, created_at, updated_at
    FROM projects
"""


_BINDING_INVENTORY_SELECT = """
    SELECT
        b.binding_id,
        b.scope_key,
        b.project_alias,
        b.native_thread_id,
        b.model_id,
        b.effort_id,
        b.service_tier_id,
        b.settings_revision,
        b.message_context_mode,
        b.context_anchor_message_id,
        b.context_anchor_create_time_ms,
        b.context_revision,
        b.task_reactions_enabled,
        b.progress_card_enabled,
        b.feedback_revision,
        b.creator_id,
        b.created_at,
        b.activated_at,
        b.ever_activated,
        CASE WHEN s.active_binding_id = b.binding_id THEN 1 ELSE 0 END AS active,
        s.scope_key AS scope_scope_key,
        s.app_id AS scope_app_id,
        s.chat_id AS scope_chat_id,
        s.kind AS scope_kind,
        s.topic_id AS scope_topic_id,
        s.active_binding_id AS scope_active_binding_id,
        s.updated_at AS scope_updated_at
    FROM bindings b
    JOIN scopes s ON s.scope_key = b.scope_key
"""


_SIDE_INVENTORY_SELECT = """
    SELECT
        st.side_id, st.app_id, st.chat_id, st.topic_id, st.root_message_id,
        st.source_message_id, st.parent_binding_id, st.creator_id,
        st.requires_mention, st.state, st.created_at, st.updated_at,
        b.project_alias AS parent_project_alias
    FROM side_topics st
    LEFT JOIN bindings b ON b.binding_id = st.parent_binding_id
"""


_PROJECT_AGGREGATE_SELECT = """
    SELECT
        p.alias, p.cwd, p.enabled, p.revision, p.created_at, p.updated_at,
        COUNT(b.binding_id) AS binding_count,
        COALESCE(SUM(CASE WHEN b.native_thread_id IS NULL THEN 1 ELSE 0 END), 0)
            AS lazy_binding_count,
        COALESCE(SUM(CASE WHEN b.native_thread_id IS NOT NULL THEN 1 ELSE 0 END), 0)
            AS materialized_binding_count,
        MAX(CASE WHEN b.ever_activated = 1 THEN b.activated_at END)
            AS last_activated_at
    FROM projects p
    LEFT JOIN bindings b ON b.project_alias = p.alias
"""


_SIDE_TOPIC_SELECT = """
    SELECT
        side_id, app_id, chat_id, topic_id, root_message_id,
        source_message_id, parent_binding_id, creator_id,
        requires_mention, state, created_at, updated_at
    FROM side_topics
"""


def _binding(row: sqlite3.Row) -> ThreadBinding:
    settings_values = (
        row["model_id"],
        row["effort_id"],
        row["service_tier_id"],
    )
    if all(value is None for value in settings_values):
        settings = None
    elif all(isinstance(value, str) and value for value in settings_values):
        settings = BindingTurnSettings(*settings_values)
    else:
        raise RuntimeError("Binding contains partial Turn settings")
    settings_revision = row["settings_revision"]
    if not isinstance(settings_revision, int) or settings_revision < 1:
        raise RuntimeError("Binding contains an invalid settings revision")
    message_context_mode = _mention_context_mode(row["message_context_mode"])
    context_anchor = _message_context_anchor(
        row["context_anchor_message_id"],
        row["context_anchor_create_time_ms"],
    )
    if (
        message_context_mode is MentionContextMode.CURRENT_ONLY
        and context_anchor is not None
    ) or (
        message_context_mode is MentionContextMode.CATCH_UP
        and context_anchor is None
    ):
        raise RuntimeError("Binding contains inconsistent mention context")
    context_revision = row["context_revision"]
    if not isinstance(context_revision, int) or context_revision < 1:
        raise RuntimeError("Binding contains an invalid context revision")
    # Schema v7 retains the historical task_reactions_enabled storage name;
    # ADR 0051 narrows that value to the optional THINKING reaction pulse.
    feedback_values = (
        row["task_reactions_enabled"],
        row["progress_card_enabled"],
    )
    if not all(
        isinstance(value, int) and value in {0, 1}
        for value in feedback_values
    ):
        raise RuntimeError("Binding contains invalid task feedback")
    task_feedback = BindingTaskFeedback(
        reaction_pulse_enabled=bool(feedback_values[0]),
        progress_card_enabled=bool(feedback_values[1]),
    )
    feedback_revision = row["feedback_revision"]
    if not isinstance(feedback_revision, int) or feedback_revision < 1:
        raise RuntimeError("Binding contains an invalid feedback revision")
    ever_activated = row["ever_activated"]
    if not isinstance(ever_activated, int) or ever_activated not in {0, 1}:
        raise RuntimeError("Binding contains an invalid activation flag")
    return ThreadBinding(
        id=row["binding_id"],
        scope_key=row["scope_key"],
        project_alias=row["project_alias"],
        native_thread_id=row["native_thread_id"],
        turn_settings=settings,
        settings_revision=settings_revision,
        creator_id=row["creator_id"],
        active=bool(row["active"]),
        created_at=row["created_at"],
        activated_at=(row["activated_at"] if ever_activated else None),
        message_context_mode=message_context_mode,
        context_anchor=context_anchor,
        context_revision=context_revision,
        task_feedback=task_feedback,
        feedback_revision=feedback_revision,
    )


def _scope_record(row: sqlite3.Row, *, prefix: str = "") -> ScopeRecord:
    return ScopeRecord(
        scope_key=row[prefix + "scope_key"],
        app_id=row[prefix + "app_id"],
        chat_id=row[prefix + "chat_id"],
        kind=ScopeKind(row[prefix + "kind"]),
        topic_id=row[prefix + "topic_id"],
        active_binding_id=row[prefix + "active_binding_id"],
        updated_at=row[prefix + "updated_at"],
    )


def _binding_inventory(row: sqlite3.Row) -> BindingInventoryRecord:
    return BindingInventoryRecord(
        binding=_binding(row),
        scope=_scope_record(row, prefix="scope_"),
    )


def _settings_values(
    settings: BindingTurnSettings | None,
) -> tuple[str | None, str | None, str | None]:
    if settings is None:
        return (None, None, None)
    return (settings.model_id, settings.effort_id, settings.service_tier_id)


def _feedback_values(
    feedback: BindingTaskFeedback,
) -> tuple[bool, bool]:
    if not isinstance(feedback, BindingTaskFeedback):
        raise ValueError("task feedback must be a BindingTaskFeedback")
    return (
        feedback.reaction_pulse_enabled,
        feedback.progress_card_enabled,
    )


def _mention_context_mode(value: object) -> MentionContextMode:
    try:
        return MentionContextMode(value)
    except (TypeError, ValueError) as error:
        raise RuntimeError(
            "Binding contains an invalid mention context mode"
        ) from error


def _message_context_anchor(
    message_id: object,
    create_time_ms: object,
) -> MessageContextAnchor | None:
    if message_id is None and create_time_ms is None:
        return None
    try:
        return MessageContextAnchor(
            message_id=message_id,  # type: ignore[arg-type]
            create_time_ms=create_time_ms,  # type: ignore[arg-type]
        )
    except (TypeError, ValueError) as error:
        raise RuntimeError("Binding contains an invalid context anchor") from error


def _context_values(
    anchor: MessageContextAnchor | None,
) -> tuple[str | None, int | None]:
    if anchor is None:
        return (None, None)
    if not isinstance(anchor, MessageContextAnchor):
        raise ValueError("context anchor must be a MessageContextAnchor")
    return (anchor.message_id, anchor.create_time_ms)


def _validate_context_state(
    *,
    scope_kind: ScopeKind,
    mode: MentionContextMode,
    anchor: MessageContextAnchor | None,
) -> None:
    if not isinstance(mode, MentionContextMode):
        raise ValueError("message context mode must be a MentionContextMode")
    _context_values(anchor)
    if mode is MentionContextMode.CURRENT_ONLY:
        if anchor is not None:
            raise ValueError("current-only context must not have an anchor")
        return
    if scope_kind is ScopeKind.DIRECT:
        raise ValueError("direct Binding cannot use catch-up context")
    if anchor is None:
        raise ValueError("catch-up context requires an exact message anchor")


def _project_record(row: sqlite3.Row) -> ProjectRecord:
    return ProjectRecord(
        alias=row["alias"],
        cwd=row["cwd"],
        enabled=bool(row["enabled"]),
        revision=row["revision"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _project_aggregate(row: sqlite3.Row) -> ProjectAggregate:
    return ProjectAggregate(
        project=_project_record(row),
        binding_count=row["binding_count"],
        lazy_binding_count=row["lazy_binding_count"],
        materialized_binding_count=row["materialized_binding_count"],
        last_activated_at=row["last_activated_at"],
    )


def _side_topic(row: sqlite3.Row) -> SideTopicRecord:
    return SideTopicRecord(
        id=row["side_id"],
        app_id=row["app_id"],
        chat_id=row["chat_id"],
        topic_id=row["topic_id"],
        root_message_id=row["root_message_id"],
        source_message_id=row["source_message_id"],
        parent_binding_id=row["parent_binding_id"],
        creator_id=row["creator_id"],
        requires_mention=bool(row["requires_mention"]),
        state=SideTopicState(row["state"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _side_inventory(row: sqlite3.Row) -> SideTopicInventoryRecord:
    return SideTopicInventoryRecord(
        side_topic=_side_topic(row),
        project_alias=row["parent_project_alias"],
    )


def _binding_inventory_statement(
    *,
    query: BindingQuery,
    cursor: BindingCursor | None,
    limit: int,
) -> tuple[str, list[object]]:
    clauses: list[str] = []
    parameters: list[object] = []
    if query.project_alias is not None:
        _require_query_value("Project alias", query.project_alias)
        clauses.append("b.project_alias = ?")
        parameters.append(query.project_alias)
    if query.scope_kind is not None:
        if not isinstance(query.scope_kind, ScopeKind):
            raise ValueError("Scope kind is invalid")
        clauses.append("s.kind = ?")
        parameters.append(query.scope_kind.value)
    if query.chat_id is not None:
        _require_query_value("chat ID", query.chat_id)
        clauses.append("s.chat_id = ?")
        parameters.append(query.chat_id)
    if query.topic_id is not None:
        _require_query_value("topic ID", query.topic_id)
        clauses.append("s.topic_id = ?")
        parameters.append(query.topic_id)
    if query.identity is not None:
        _require_query_value("Binding identity", query.identity)
        clauses.append("(b.binding_id = ? OR b.native_thread_id = ?)")
        parameters.extend((query.identity, query.identity))
    if query.materialized is not None:
        if not isinstance(query.materialized, bool):
            raise ValueError("materialized filter must be boolean")
        clauses.append(
            "b.native_thread_id IS NOT NULL"
            if query.materialized
            else "b.native_thread_id IS NULL"
        )
    if query.current is not None:
        if not isinstance(query.current, bool):
            raise ValueError("current filter must be boolean")
        clauses.append(
            "s.active_binding_id = b.binding_id"
            if query.current
            else "(s.active_binding_id IS NULL OR s.active_binding_id != b.binding_id)"
        )
    if query.created_from is not None:
        _require_query_value("created-from time", query.created_from)
        clauses.append("b.created_at >= ?")
        parameters.append(query.created_from)
    if query.created_before is not None:
        _require_query_value("created-before time", query.created_before)
        clauses.append("b.created_at < ?")
        parameters.append(query.created_before)
    if cursor is not None:
        clauses.append(
            "(b.created_at < ? OR (b.created_at = ? AND b.binding_id < ?))"
        )
        parameters.extend((cursor.created_at, cursor.created_at, cursor.binding_id))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    parameters.append(limit)
    return (
        _BINDING_INVENTORY_SELECT
        + where
        + " ORDER BY b.created_at DESC, b.binding_id DESC LIMIT ?",
        parameters,
    )


def _side_inventory_statement(
    *,
    query: SideTopicQuery,
    cursor: SideTopicCursor | None,
    limit: int,
) -> tuple[str, list[object]]:
    clauses: list[str] = []
    parameters: list[object] = []
    for label, column, value in (
        ("Project alias", "b.project_alias", query.project_alias),
        ("parent Binding ID", "st.parent_binding_id", query.parent_binding_id),
        ("app ID", "st.app_id", query.app_id),
        ("chat ID", "st.chat_id", query.chat_id),
        ("topic ID", "st.topic_id", query.topic_id),
        ("root message ID", "st.root_message_id", query.root_message_id),
    ):
        if value is not None:
            _require_query_value(label, value)
            clauses.append(column + " = ?")
            parameters.append(value)
    if query.state is not None:
        if not isinstance(query.state, SideTopicState):
            raise ValueError("Side Topic state is invalid")
        clauses.append("st.state = ?")
        parameters.append(query.state.value)
    if query.created_from is not None:
        _require_query_value("created-from time", query.created_from)
        clauses.append("st.created_at >= ?")
        parameters.append(query.created_from)
    if query.created_before is not None:
        _require_query_value("created-before time", query.created_before)
        clauses.append("st.created_at < ?")
        parameters.append(query.created_before)
    if cursor is not None:
        clauses.append(
            "(st.created_at < ? OR (st.created_at = ? AND st.side_id < ?))"
        )
        parameters.extend((cursor.created_at, cursor.created_at, cursor.side_id))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    parameters.append(limit)
    return (
        _SIDE_INVENTORY_SELECT
        + where
        + " ORDER BY st.created_at DESC, st.side_id DESC LIMIT ?",
        parameters,
    )


def _validate_page_limit(limit: int, *, maximum: int = 50) -> int:
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= maximum
    ):
        raise ValueError(f"page size must be between 1 and {maximum}")
    return limit


def _require_query_value(label: str, value: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must not be empty")


def _require_exact_scope(row: sqlite3.Row, scope: FeishuScope) -> None:
    actual = (
        row["scope_key"],
        row["app_id"],
        row["chat_id"],
        row["kind"],
        row["topic_id"],
    )
    expected = (
        scope.key,
        scope.app_id,
        scope.chat_id,
        scope.kind.value,
        scope.topic_id,
    )
    if actual != expected:
        raise ScopeConflict("Scope key is already bound to different identity")


def _require_same_side_reservation(
    record: SideTopicRecord,
    *,
    chat_id: str,
    parent_binding_id: str,
    creator_id: str,
    requires_mention: bool,
) -> None:
    if (
        record.chat_id != chat_id
        or record.parent_binding_id != parent_binding_id
        or record.creator_id != creator_id
        or record.requires_mention is not requires_mention
    ):
        raise SideTopicConflict(
            "Side source message is already reserved with different identity"
        )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")
