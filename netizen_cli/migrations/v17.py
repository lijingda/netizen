"""Frozen v16 -> v17 scheduled workspace intent and minimal Run resource receipts.

Historical new-topic plans retain project-directory behavior; binding targets and
plan tombstones never gain independent workspace intent. Receipts have no FK to
managed resources and remain meaningful after explicit Worktree deletion.
"""

from __future__ import annotations

import sqlite3

from .schema import require_schema
from .v14 import _require_session_settings
from .v16 import SCHEMA as V16_SCHEMA, _require_workspace


WORKSPACE = "workspace_json TEXT"
WORKTREE_ID = "worktree_id TEXT CHECK(worktree_id IS NULL OR (target_kind = 'new_topic' AND length(worktree_id) > 0))"
SCHEMA = tuple(
    statement.replace("session_settings_json TEXT,", f"session_settings_json TEXT, {WORKSPACE},")
    if statement.startswith("CREATE TABLE schedule_plans (") else
    statement.replace("disposition TEXT CHECK(disposition IN ('started','steered')),",
                      f"disposition TEXT CHECK(disposition IN ('started','steered')), {WORKTREE_ID},")
    if statement.startswith("CREATE TABLE schedule_runs (") else statement
    for statement in V16_SCHEMA
)


def apply(connection: sqlite3.Connection) -> None:
    connection.execute(f"ALTER TABLE schedule_plans ADD COLUMN {WORKSPACE}")
    connection.execute(f"ALTER TABLE schedule_runs ADD COLUMN {WORKTREE_ID}")
    connection.execute("UPDATE schedule_plans SET workspace_json = '{\"mode\":\"project\"}' "
                       "WHERE deleted = 0 AND target_kind = 'new_topic'")


def validate(connection: sqlite3.Connection) -> None:
    require_schema(connection, SCHEMA)
    if connection.execute(
        "SELECT 1 FROM schedule_runs WHERE (target_kind='binding' AND (binding_id IS NULL OR binding_id='')) "
        "OR (disposition IS NOT NULL AND (target_kind!='binding' "
        "OR initial_turn_id IS NULL OR initial_turn_id='' OR barrier!='released')) LIMIT 1"
    ).fetchone():
        raise RuntimeError("schema v17 scheduling input receipt is invalid")
    if connection.execute(
        "SELECT 1 FROM session_defaults_order WHERE app_id IS NULL OR app_id = '' "
        "OR typeof(revision) != 'integer' OR revision < 1 LIMIT 1"
    ).fetchone():
        raise RuntimeError("schema v17 defaults order revision is invalid")
    try:
        for row in connection.execute("SELECT deleted, session_settings_json, workspace_json, target_kind, target_binding_id FROM schedule_plans"):
            if (row["target_kind"] == "binding") != bool(row["target_binding_id"]):
                raise ValueError("invalid scheduling target identity")
            if row["target_kind"] == "binding" or row["deleted"]:
                if row["session_settings_json"] is not None or row["workspace_json"] is not None:
                    raise ValueError("unexpected scheduling session settings or workspace policy")
            else:
                _require_session_settings(row["session_settings_json"])
                _require_workspace(row["workspace_json"])
        positions: dict[str, list[int]] = {}
        for row in connection.execute("SELECT * FROM session_defaults ORDER BY app_id, position"):
            if not all(isinstance(row[key], str) and row[key] for key in ("id", "app_id", "project")):
                raise ValueError("missing default identity")
            _require_session_settings(row["session_settings_json"])
            _require_workspace(row["workspace_json"])
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
        raise RuntimeError(f"schema v17 persisted configuration is invalid: {error}") from error
