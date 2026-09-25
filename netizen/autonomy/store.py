"""Feature-owned tables on the existing Channel writer and transaction boundary."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Callable, ContextManager

from .models import Candidate, Context, DecisionToken, Record, Settings


SCHEMA = (
    """CREATE TABLE autonomy_bindings (
        binding_id TEXT PRIMARY KEY REFERENCES bindings(binding_id) ON DELETE CASCADE,
        enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
        revision INTEGER NOT NULL CHECK(revision >= 1),
        received INTEGER NOT NULL DEFAULT 0 CHECK(received >= 0),
        last_accepted INTEGER,
        summary TEXT NOT NULL DEFAULT '',
        summary_revision INTEGER NOT NULL DEFAULT 0,
        CHECK(last_accepted IS NULL OR (last_accepted >= 1 AND last_accepted <= received))
    )""",
    """CREATE TABLE autonomy_records (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        binding_id TEXT NOT NULL REFERENCES autonomy_bindings(binding_id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK(kind IN ('user','final')),
        reference TEXT NOT NULL,
        text TEXT NOT NULL,
        UNIQUE(binding_id, kind, reference)
    )""",
    """CREATE TABLE autonomy_pending_turns (
        binding_id TEXT NOT NULL REFERENCES autonomy_bindings(binding_id) ON DELETE CASCADE,
        turn_id TEXT NOT NULL,
        PRIMARY KEY(binding_id,turn_id)
    )""",
)
TABLE_COLUMNS = {
    "autonomy_bindings": {"binding_id", "enabled", "revision", "received", "last_accepted", "summary", "summary_revision"},
    "autonomy_records": {"sequence", "binding_id", "kind", "reference", "text"},
    "autonomy_pending_turns": {"binding_id", "turn_id"},
}


def create_schema(connection: sqlite3.Connection) -> None:
    for statement in SCHEMA:
        connection.execute(statement)


def require_schema(connection: sqlite3.Connection) -> None:
    for table, expected in TABLE_COLUMNS.items():
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if actual != expected:
            raise RuntimeError("current Channel database has invalid autonomy schema")
        parent = "bindings" if table == "autonomy_bindings" else "autonomy_bindings"
        foreign = list(connection.execute(f"PRAGMA foreign_key_list({table})"))
        if not any(row[2] == parent and row[3] == "binding_id" and row[4] == "binding_id" and row[6] == "CASCADE" for row in foreign):
            raise RuntimeError("current Channel database has invalid autonomy ownership")
    if connection.execute(
        "SELECT 1 FROM autonomy_bindings WHERE enabled NOT IN (0,1) OR revision < 1 "
        "OR received < 0 OR (last_accepted IS NOT NULL AND (last_accepted < 1 OR last_accepted > received)) LIMIT 1"
    ).fetchone():
        raise RuntimeError("current Channel database has invalid autonomy state")
    if connection.execute(
        "SELECT 1 FROM autonomy_bindings a JOIN bindings b ON b.binding_id=a.binding_id "
        "JOIN scopes s ON s.scope_key=b.scope_key WHERE a.enabled=1 "
        "AND (s.kind NOT IN ('group','topic') OR b.message_context_mode != 'current-only') LIMIT 1"
    ).fetchone():
        raise RuntimeError("autonomy requires a group/topic Binding with current-only context")


class AutonomyStore:
    """No independent connection, thread, commit policy or native history model."""

    def __init__(
        self, connection: sqlite3.Connection, *,
        transaction: Callable[[], ContextManager[object]] | None = None,
        lock: object | None = None,
    ) -> None:
        self._connection = connection
        self._lock = lock or threading.RLock()
        self.transaction = transaction or self._transaction

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._connection.execute("SAVEPOINT autonomy_write")
            try:
                yield
            except BaseException:
                self._connection.execute("ROLLBACK TO autonomy_write")
                self._connection.execute("RELEASE autonomy_write")
                raise
            else:
                self._connection.execute("RELEASE autonomy_write")

    def settings(self, binding_id: str) -> Settings:
        with self._lock:
            row = self._connection.execute(
                "SELECT enabled,revision,received,last_accepted FROM autonomy_bindings WHERE binding_id=?", (binding_id,),
            ).fetchone()
        return Settings(bool(row[0]), row[1], row[2], row[3]) if row else Settings()

    def is_enabled(self, binding_id: str) -> bool:
        return self.settings(binding_id).enabled

    def set_enabled(self, binding_id: str, enabled: bool) -> Settings:
        # Called inside BindingStore's existing settings/create transaction.
        with self._lock:
            changed = self._connection.execute(
                "INSERT INTO autonomy_bindings(binding_id,enabled,revision) VALUES(?,?,1) "
                "ON CONFLICT(binding_id) DO UPDATE SET enabled=excluded.enabled, "
                "revision=revision+1, received=0, last_accepted=NULL "
                "WHERE enabled != excluded.enabled", (binding_id, int(enabled)),
            ).rowcount
            if changed:
                self._connection.execute("DELETE FROM autonomy_pending_turns WHERE binding_id=?", (binding_id,))
        return self.settings(binding_id)

    def forget(self, binding_id: str) -> None:
        with self._lock:
            self._connection.execute("DELETE FROM autonomy_bindings WHERE binding_id=?", (binding_id,))

    def invalidate_gaps(self) -> None:
        # Process restarts may miss Feishu deliveries; do not claim exact old gaps.
        with self.transaction():
            self._connection.execute("UPDATE autonomy_bindings SET received=0,last_accepted=NULL,revision=revision+1")

    def receive(self, binding_id: str) -> Settings:
        with self.transaction():
            self._connection.execute(
                "UPDATE autonomy_bindings SET received=received+1 WHERE binding_id=? AND enabled=1", (binding_id,),
            )
            return self.settings(binding_id)

    def invalidate_gap(self, binding_id: str) -> None:
        with self.transaction():
            self._connection.execute("UPDATE autonomy_bindings SET last_accepted=NULL WHERE binding_id=?", (binding_id,))

    def accepted(self, token: DecisionToken, candidate: Candidate, turn_id: str) -> bool:
        with self.transaction():
            settings = self.settings(token.binding_id)
            if not settings.enabled or settings.revision != token.binding_revision:
                return False
            inserted = self._connection.execute(
                "INSERT OR IGNORE INTO autonomy_records(binding_id,kind,reference,text) VALUES(?,'user',?,?)",
                (token.binding_id, candidate.message_id, _candidate_text(candidate)),
            ).rowcount
            if not inserted:
                return False
            self._connection.execute(
                "INSERT OR IGNORE INTO autonomy_pending_turns(binding_id,turn_id) VALUES(?,?)",
                (token.binding_id, turn_id),
            )
            # Older asynchronous admissions cannot move the anchor backwards.
            self._connection.execute(
                "UPDATE autonomy_bindings SET last_accepted=MAX(COALESCE(last_accepted,0),?) WHERE binding_id=?",
                (token.sequence, token.binding_id),
            )
            return True

    def final(self, binding_id: str, turn_id: str, text: str, *, accepted_turn_id: str | None = None) -> bool:
        with self.transaction():
            if not self.is_enabled(binding_id) or not text:
                return False
            pending = self._connection.execute(
                "DELETE FROM autonomy_pending_turns WHERE binding_id=? AND turn_id=?",
                (binding_id, accepted_turn_id or turn_id),
            ).rowcount
            if not pending:
                return False
            self._connection.execute(
                "INSERT OR IGNORE INTO autonomy_records(binding_id,kind,reference,text) VALUES(?,'final',?,?)",
                (binding_id, turn_id, text),
            )
            return True

    def forget_turn(self, binding_id: str, turn_id: str) -> None:
        with self.transaction():
            self._connection.execute("DELETE FROM autonomy_pending_turns WHERE binding_id=? AND turn_id=?", (binding_id, turn_id))

    def context(self, binding_id: str) -> Context:
        with self._lock:
            row = self._connection.execute(
                "SELECT summary,summary_revision FROM autonomy_bindings WHERE binding_id=?", (binding_id,),
            ).fetchone()
            if row is None:
                return Context()
            records = tuple(Record(*item) for item in self._connection.execute(
                "SELECT sequence,kind,reference,text FROM autonomy_records WHERE binding_id=? ORDER BY sequence", (binding_id,),
            ))
        return Context(row[0], row[1], records)

    def replace_prefix(self, binding_id: str, *, binding_revision: int, previous_revision: int, through: int, summary: str) -> bool:
        with self.transaction():
            cursor = self._connection.execute(
                "UPDATE autonomy_bindings SET summary=?,summary_revision=summary_revision+1 "
                "WHERE binding_id=? AND enabled=1 AND revision=? AND summary_revision=?",
                (summary, binding_id, binding_revision, previous_revision),
            )
            if cursor.rowcount != 1:
                return False
            self._connection.execute(
                "DELETE FROM autonomy_records WHERE binding_id=? AND sequence<=?", (binding_id, through),
            )
            return True


def _candidate_text(candidate: Candidate) -> str:
    return f"{candidate.sender} [{candidate.message_type}]: {candidate.text}"
