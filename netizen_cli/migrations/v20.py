"""Frozen v19 -> v20 scheduled new-conversation auto-archive policy migration.

Existing plans retain their execution behavior with the policy disabled. The
policy belongs to the plan; Run rows retain only their existing receipts.
"""

from __future__ import annotations

import json
import sqlite3

from .schema import require_schema
from .v16 import _require_workspace
from .v19 import SCHEMA as V19_SCHEMA, _require_progress_card_intent, _require_session_settings


AUTO_ARCHIVE = (
    "auto_archive INTEGER NOT NULL DEFAULT 0 CHECK("
    "typeof(auto_archive) = 'integer' AND auto_archive IN (0,1) "
    "AND (auto_archive = 0 OR (target_kind = 'new_topic' AND deleted = 0)))"
)
SCHEMA = tuple(
    statement.replace("target_binding_id TEXT,", f"target_binding_id TEXT, {AUTO_ARCHIVE},")
    if statement.startswith("CREATE TABLE schedule_plans (") else statement
    for statement in V19_SCHEMA
)


def apply(connection: sqlite3.Connection) -> None:
    connection.execute(f"ALTER TABLE schedule_plans ADD COLUMN {AUTO_ARCHIVE}")


def validate(connection: sqlite3.Connection) -> None:
    require_schema(connection, SCHEMA)
    if connection.execute(
        "SELECT 1 FROM schedule_plans WHERE typeof(auto_archive) != 'integer' OR auto_archive NOT IN (0,1) "
        "OR (auto_archive != 0 AND (target_kind != 'new_topic' OR deleted != 0)) LIMIT 1"
    ).fetchone():
        raise RuntimeError("schema v20 scheduling auto archive is invalid")
    if connection.execute(
        "SELECT 1 FROM schedule_runs WHERE (target_kind='binding' AND (binding_id IS NULL OR binding_id='')) "
        "OR (disposition IS NOT NULL AND (target_kind!='binding' "
        "OR initial_turn_id IS NULL OR initial_turn_id='' OR barrier!='released')) LIMIT 1"
    ).fetchone():
        raise RuntimeError("schema v20 scheduling input receipt is invalid")
    if connection.execute(
        "SELECT 1 FROM session_defaults_order WHERE app_id IS NULL OR app_id = '' "
        "OR typeof(revision) != 'integer' OR revision < 1 LIMIT 1"
    ).fetchone():
        raise RuntimeError("schema v20 defaults order revision is invalid")
    try:
        for row in connection.execute("SELECT progress_card_settings_json FROM bindings"):
            _require_progress_card_intent(json.loads(row["progress_card_settings_json"]))
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
        raise RuntimeError(f"schema v20 persisted configuration is invalid: {error}") from error
