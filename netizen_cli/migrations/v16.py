"""Frozen v15 -> v16 default-session workspace policy migration.

Only frozen schema/format helpers are reused; historical rules keep project cwd.
"""

from __future__ import annotations

import json
import sqlite3

from .schema import require_schema
from .v14 import _require_session_settings
from .v15 import SCHEMA as V15_SCHEMA


WORKSPACE = """workspace_json TEXT NOT NULL DEFAULT '{"mode":"project"}'"""
SCHEMA = tuple(
    statement.replace("session_settings_json TEXT NOT NULL,", f"session_settings_json TEXT NOT NULL, {WORKSPACE},")
    if statement.startswith("CREATE TABLE session_defaults (") else statement
    for statement in V15_SCHEMA
)


def apply(connection: sqlite3.Connection) -> None:
    connection.execute(f"ALTER TABLE session_defaults ADD COLUMN {WORKSPACE}")


def _require_workspace(raw: str) -> None:
    # Frozen persistence shape: never import the live WorkspacePolicy model.
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("invalid workspace policy")
    mode = value.get("mode")
    if mode == "project" and value.keys() == {"mode"}:
        return
    text = None
    if mode == "existing" and value.keys() == {"mode", "worktree_id"}:
        text = value["worktree_id"]
    elif mode == "new":
        if value.keys() == {"mode"}:
            return
        if value.keys() == {"mode", "ref"}:
            text = value["ref"]
    if (not isinstance(text, str) or not text or text != text.strip() or len(text) > 256
            or any(ord(character) < 32 for character in text)):
        raise ValueError("invalid workspace policy")


def validate(connection: sqlite3.Connection) -> None:
    require_schema(connection, SCHEMA)
    if connection.execute(
        "SELECT 1 FROM schedule_runs WHERE (target_kind='binding' AND (binding_id IS NULL OR binding_id='')) "
        "OR (disposition IS NOT NULL AND (target_kind!='binding' "
        "OR initial_turn_id IS NULL OR initial_turn_id='' OR barrier!='released')) LIMIT 1"
    ).fetchone():
        raise RuntimeError("schema v16 scheduling input receipt is invalid")
    if connection.execute(
        "SELECT 1 FROM session_defaults_order WHERE app_id IS NULL OR app_id = '' "
        "OR typeof(revision) != 'integer' OR revision < 1 LIMIT 1"
    ).fetchone():
        raise RuntimeError("schema v16 defaults order revision is invalid")
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
        raise RuntimeError(f"schema v16 persisted configuration is invalid: {error}") from error
