"""Frozen v14 -> v15 managed Worktree metadata migration.

Only frozen v14 SQL/validation helpers are reused. Do not import live stores or
schema creators: later changes must add a new adjacent migration.
"""

from __future__ import annotations

import sqlite3

from .schema import require_schema
from .v14 import SCHEMA as V14_SCHEMA, _require_session_settings


GENERATION = "generation INTEGER NOT NULL DEFAULT 1 CHECK(typeof(generation) = 'integer' AND generation >= 1)"
WORKING_CWD = "working_cwd TEXT CHECK(working_cwd IS NULL OR length(working_cwd) > 0)"
WORKTREE_ID = "worktree_id TEXT CHECK(worktree_id IS NULL OR (length(worktree_id) > 0 AND working_cwd IS NOT NULL))"
WORKTREE_SCHEMA = (
    """CREATE TABLE managed_worktrees (
        worktree_id TEXT PRIMARY KEY CHECK(length(worktree_id) > 0),
        project_alias TEXT NOT NULL CHECK(length(project_alias) > 0),
        project_generation INTEGER NOT NULL
            CHECK(typeof(project_generation) = 'integer' AND project_generation >= 1),
        root TEXT NOT NULL UNIQUE CHECK(length(root) > 0),
        cwd TEXT NOT NULL UNIQUE CHECK(length(cwd) > 0),
        common_dir TEXT NOT NULL CHECK(length(common_dir) > 0),
        git_dir TEXT, identity_token TEXT, origin_binding_id TEXT,
        creation_state TEXT NOT NULL CHECK(creation_state IN ('creating', 'ready', 'unknown')),
        revision INTEGER NOT NULL DEFAULT 1 CHECK(typeof(revision) = 'integer' AND revision >= 1),
        created_at TEXT NOT NULL,
        CHECK(creation_state != 'ready' OR
            (git_dir IS NOT NULL AND length(git_dir) > 0
             AND identity_token IS NOT NULL AND length(identity_token) > 0))
    )""",
    "CREATE INDEX worktrees_project ON managed_worktrees(project_alias, project_generation, created_at, worktree_id)",
    "CREATE INDEX worktrees_created ON managed_worktrees(created_at, worktree_id)",
    "CREATE INDEX bindings_worktree ON bindings(worktree_id)",
)
SCHEMA = tuple(
    statement.replace("native_thread_id TEXT UNIQUE,", f"native_thread_id TEXT UNIQUE, {WORKING_CWD}, {WORKTREE_ID},")
    if statement.startswith("CREATE TABLE bindings ") else
    statement.replace("alias TEXT PRIMARY KEY,", f"alias TEXT PRIMARY KEY, {GENERATION},")
    if statement.startswith("CREATE TABLE projects ") else statement
    for statement in V14_SCHEMA
) + WORKTREE_SCHEMA


def apply(connection: sqlite3.Connection) -> None:
    connection.execute(f"ALTER TABLE projects ADD COLUMN {GENERATION}")
    connection.execute(f"ALTER TABLE bindings ADD COLUMN {WORKING_CWD}")
    connection.execute(f"ALTER TABLE bindings ADD COLUMN {WORKTREE_ID}")
    for statement in WORKTREE_SCHEMA:
        connection.execute(statement)


def validate(connection: sqlite3.Connection) -> None:
    require_schema(connection, SCHEMA)
    if connection.execute(
        "SELECT 1 FROM schedule_runs WHERE (target_kind='binding' AND (binding_id IS NULL OR binding_id='')) "
        "OR (disposition IS NOT NULL AND (target_kind!='binding' "
        "OR initial_turn_id IS NULL OR initial_turn_id='' OR barrier!='released')) LIMIT 1"
    ).fetchone():
        raise RuntimeError("schema v15 scheduling input receipt is invalid")
    if connection.execute(
        "SELECT 1 FROM session_defaults_order WHERE app_id IS NULL OR app_id = '' "
        "OR typeof(revision) != 'integer' OR revision < 1 LIMIT 1"
    ).fetchone():
        raise RuntimeError("schema v15 defaults order revision is invalid")
    try:
        for row in connection.execute("SELECT deleted, session_settings_json, target_kind, target_binding_id FROM schedule_plans"):
            if (row["target_kind"] == "binding") != bool(row["target_binding_id"]):
                raise ValueError("invalid scheduling target identity")
            if row["target_kind"] == "binding" or row["deleted"]:
                if row["session_settings_json"] is not None:
                    raise ValueError("unexpected scheduling session settings")
            else:
                _require_session_settings(row["session_settings_json"])
        positions: dict[str, list[int]] = {}
        for row in connection.execute("SELECT * FROM session_defaults ORDER BY app_id, position"):
            if not all(isinstance(row[key], str) and row[key] for key in ("id", "app_id", "project")):
                raise ValueError("missing default identity")
            _require_session_settings(row["session_settings_json"])
            if row["kind"] == "group_name":
                if row["keyword"] != row["keyword"].strip():
                    raise ValueError("invalid group keyword")
                positions.setdefault(row["app_id"], []).append(row["position"])
        for app_id, values in positions.items():
            if len(values) > 200 or values != list(range(len(values))):
                raise ValueError("invalid defaults group order")
            if connection.execute("SELECT 1 FROM session_defaults_order WHERE app_id = ?", (app_id,)).fetchone() is None:
                raise ValueError("missing defaults group order revision")
        for row in connection.execute("SELECT * FROM managed_worktrees"):
            if any(not isinstance(row[key], str) or not row[key].startswith("/")
                   for key in ("root", "cwd", "common_dir")):
                raise ValueError("invalid worktree paths")
            if row["git_dir"] is not None and not row["git_dir"].startswith("/"):
                raise ValueError("invalid worktree Git directory")
        for row in connection.execute(
            "SELECT b.worktree_id,b.working_cwd,w.cwd,w.project_alias,b.project_alias AS binding_project "
            "FROM bindings b LEFT JOIN managed_worktrees w ON w.worktree_id=b.worktree_id "
            "WHERE b.worktree_id IS NOT NULL"
        ):
            if row["cwd"] is None or row["working_cwd"] != row["cwd"] or row["project_alias"] != row["binding_project"]:
                raise ValueError("invalid worktree Binding association")
        if connection.execute(
            "SELECT 1 FROM bindings WHERE working_cwd IS NOT NULL AND substr(working_cwd,1,1) != '/' LIMIT 1"
        ).fetchone():
            raise ValueError("invalid Binding working directory")
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"schema v15 persisted configuration is invalid: {error}") from error
