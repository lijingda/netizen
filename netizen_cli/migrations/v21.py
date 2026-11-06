"""Frozen v20 -> v21 bounded-cleanup and exact-target query indexes.

No row data or interpretation changes; all additions are non-unique indexes.
"""

from __future__ import annotations

import sqlite3

from .schema import _tokens
from .v20 import SCHEMA as V20_SCHEMA, validate as validate_v20


INDEXES = (
    "CREATE INDEX dedup_keys_expiry ON dedup_keys(expires_at)",
    "CREATE INDEX schedule_requests_expiry ON schedule_requests(expires_at)",
    "CREATE INDEX schedule_runs_binding_target ON schedule_runs(binding_id) WHERE target_kind = 'binding'",
    "CREATE INDEX schedule_plans_binding_target ON schedule_plans(target_binding_id) WHERE target_kind = 'binding' AND deleted = 0",
    "CREATE INDEX schedule_runs_project_pending ON schedule_runs(project_alias, run_id) WHERE barrier != 'released' OR error_code = 'publishing_unknown'",
)
SCHEMA = (*V20_SCHEMA, *INDEXES)


def apply(connection: sqlite3.Connection) -> None:
    for statement in INDEXES:
        connection.execute(statement)


def validate(connection: sqlite3.Connection) -> None:
    validate_v20(connection)
    # Earlier validators intentionally ignore non-unique performance indexes.
    # These five are required for v21's bounded synchronous query contract.
    for statement in INDEXES:
        name = statement.split()[2]
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,),
        ).fetchone()
        if row is None or _tokens(row[0]) != _tokens(statement):
            raise RuntimeError(f"schema v21 query index is invalid: {name}")
