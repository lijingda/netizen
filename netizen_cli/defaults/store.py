"""Defaults metadata on the existing Channel database and transaction boundary."""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import TYPE_CHECKING, Any

from ..session_settings import SessionSettings
from .models import DefaultConfigurationError, DefaultRule

if TYPE_CHECKING:
    from ..bindings import BindingStore


MAX_GROUP_RULES = 200
SCHEMA = (
    """CREATE TABLE session_defaults (
        id TEXT PRIMARY KEY,
        app_id TEXT NOT NULL CHECK(length(app_id) > 0),
        kind TEXT NOT NULL CHECK(kind IN ('chat', 'group_name')),
        chat_id TEXT, keyword TEXT,
        project TEXT NOT NULL CHECK(length(project) > 0),
        session_settings_json TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(typeof(revision) = 'integer' AND revision >= 1),
        position INTEGER,
        CHECK((kind = 'chat' AND chat_id IS NOT NULL AND length(chat_id) > 0
            AND keyword IS NULL AND position IS NULL) OR
            (kind = 'group_name' AND chat_id IS NULL AND keyword IS NOT NULL
            AND length(trim(keyword)) > 0 AND typeof(position) = 'integer' AND position >= 0))
    )""",
    "CREATE UNIQUE INDEX session_defaults_chat ON session_defaults(app_id, chat_id) WHERE kind = 'chat'",
    "CREATE UNIQUE INDEX session_defaults_position ON session_defaults(app_id, position) WHERE kind = 'group_name'",
    """CREATE TABLE session_defaults_order (
        app_id TEXT PRIMARY KEY CHECK(length(app_id) > 0),
        revision INTEGER NOT NULL CHECK(typeof(revision) = 'integer' AND revision >= 1)
    )""",
)


def create_schema(connection: sqlite3.Connection) -> None:
    for statement in SCHEMA:
        connection.execute(statement)


def require_schema(connection: sqlite3.Connection) -> None:
    expected = {
        "session_defaults": {
            "id": ("TEXT", 0, 1), "app_id": ("TEXT", 1, 0),
            "kind": ("TEXT", 1, 0), "chat_id": ("TEXT", 0, 0),
            "keyword": ("TEXT", 0, 0), "project": ("TEXT", 1, 0),
            "session_settings_json": ("TEXT", 1, 0),
            "revision": ("INTEGER", 1, 0), "position": ("INTEGER", 0, 0),
        },
        "session_defaults_order": {"app_id": ("TEXT", 0, 1), "revision": ("INTEGER", 1, 0)},
    }
    for table, columns in expected.items():
        actual = {row["name"]: (row["type"].upper(), row["notnull"], row["pk"])
                  for row in connection.execute(f"PRAGMA table_info({table})")}
        if any(actual.get(name) != shape for name, shape in columns.items()):
            raise RuntimeError(f"current Channel database has invalid {table} columns")
    indexes = {row["name"]: row for row in connection.execute("PRAGMA index_list(session_defaults)")}
    for name, columns, predicate in (
        ("session_defaults_chat", ("app_id", "chat_id"), "kind = 'chat'"),
        ("session_defaults_position", ("app_id", "position"), "kind = 'group_name'"),
    ):
        index = indexes.get(name)
        if index is None or not index["unique"] or not index["partial"]:
            raise RuntimeError(f"current defaults identity index is missing or invalid: {name}")
        actual_columns = tuple(row["name"] for row in connection.execute(f"PRAGMA index_info({name})"))
        sql = connection.execute("SELECT sql FROM sqlite_master WHERE name = ?", (name,)).fetchone()[0]
        if actual_columns != columns or " ".join(sql.lower().split()).partition(" where ")[2] != predicate:
            raise RuntimeError(f"current defaults identity index has invalid shape: {name}")
    for table in expected:
        if connection.execute(f"PRAGMA foreign_key_list({table})").fetchone():
            raise RuntimeError("default configuration must not link Project lifecycle")
    if connection.execute(
        "SELECT 1 FROM session_defaults_order WHERE app_id IS NULL OR app_id = '' "
        "OR typeof(revision) != 'integer' OR revision < 1 LIMIT 1"
    ).fetchone():
        raise RuntimeError("current defaults order revision is invalid")
    positions: dict[str, list[int]] = {}
    for row in connection.execute("SELECT * FROM session_defaults ORDER BY app_id, position"):
        try:
            rule = _rule(row)
            if not all(isinstance(value, str) and value for value in (rule.id, rule.app_id, rule.project)):
                raise ValueError("missing identity")
            if type(rule.revision) is not int or rule.revision < 1:
                raise ValueError("invalid revision")
            if rule.kind == "chat":
                if not rule.chat_id or rule.keyword is not None or rule.position is not None:
                    raise ValueError("invalid chat rule")
            elif rule.kind == "group_name":
                if (rule.chat_id is not None or not isinstance(rule.keyword, str)
                        or not rule.keyword.strip() or rule.keyword != rule.keyword.strip()
                        or type(rule.position) is not int or rule.position < 0):
                    raise ValueError("invalid group rule")
                positions.setdefault(rule.app_id, []).append(rule.position)
            else:
                raise ValueError("invalid rule kind")
        except (TypeError, ValueError) as error:
            raise RuntimeError("current default configuration is invalid") from error
    for app_id, values in positions.items():
        if len(values) > MAX_GROUP_RULES or values != list(range(len(values))):
            raise RuntimeError("current defaults group order is invalid")
        if connection.execute("SELECT 1 FROM session_defaults_order WHERE app_id = ?", (app_id,)).fetchone() is None:
            raise RuntimeError("current defaults group order revision is missing")


def _rule(row: sqlite3.Row) -> DefaultRule:
    return DefaultRule(
        id=row["id"], app_id=row["app_id"], kind=row["kind"],
        chat_id=row["chat_id"], keyword=row["keyword"], project=row["project"],
        session_settings=SessionSettings.from_dict(json.loads(row["session_settings_json"])),
        revision=row["revision"], position=row["position"],
    )


class DefaultsStore:
    def __init__(self, owner: BindingStore) -> None:
        self.owner = owner

    @property
    def _db(self) -> sqlite3.Connection:
        return self.owner._connection

    def get(self, app_id: str, rule_id: str) -> DefaultRule:
        with self.owner._lock:
            row = self._db.execute("SELECT * FROM session_defaults WHERE app_id = ? AND id = ?", (app_id, rule_id)).fetchone()
            if row is None:
                raise DefaultConfigurationError("默认会话配置不存在，请刷新后重试。", code="not_found")
            return _rule(row)

    def exact(self, app_id: str, chat_id: str) -> DefaultRule | None:
        with self.owner._lock:
            row = self._db.execute(
                "SELECT * FROM session_defaults WHERE app_id = ? AND kind = 'chat' AND chat_id = ?", (app_id, chat_id),
            ).fetchone()
            return _rule(row) if row else None

    def group_rules(self, app_id: str) -> tuple[DefaultRule, ...]:
        with self.owner._lock:
            return tuple(_rule(row) for row in self._db.execute(
                "SELECT * FROM session_defaults WHERE app_id = ? AND kind = 'group_name' ORDER BY position LIMIT ?",
                (app_id, MAX_GROUP_RULES),
            ))

    async def list(self, app_id: str, kind: str, *, offset: int, limit: int) -> dict[str, Any]:
        def query(connection: sqlite3.Connection) -> dict[str, Any]:
            connection.execute("BEGIN")
            try:
                rows = connection.execute(
                    "SELECT * FROM session_defaults WHERE app_id = ? AND kind = ? "
                    "ORDER BY position, id LIMIT ? OFFSET ?", (app_id, kind, limit + 1, offset),
                ).fetchall()
                return {"items": [_rule(row).to_dict() for row in rows[:limit]],
                        "has_more": len(rows) > limit, "offset": offset,
                        "order_revision": self._order_revision(connection, app_id)}
            finally:
                connection.rollback()
        return await self.owner._submit_query(query, deadline_seconds=2)

    @staticmethod
    def _order_revision(connection: sqlite3.Connection, app_id: str) -> int:
        row = connection.execute("SELECT revision FROM session_defaults_order WHERE app_id = ?", (app_id,)).fetchone()
        return row[0] if row else 1

    def _bump_order(self, app_id: str) -> int:
        self._db.execute(
            "INSERT INTO session_defaults_order(app_id, revision) VALUES (?, 2) "
            "ON CONFLICT(app_id) DO UPDATE SET revision = revision + 1", (app_id,),
        )
        return self._order_revision(self._db, app_id)

    def save(
        self, *, app_id: str, kind: str, chat_id: str | None, keyword: str | None,
        project: str, session_settings: SessionSettings, rule_id: str | None,
        expected_revision: int | None, checked_project_revision: int | None = None,
    ) -> DefaultRule:
        with self.owner._transaction():
            if checked_project_revision is not None:
                self._require_checked_project(project, checked_project_revision)
            if expected_revision is None:
                if rule_id is not None:
                    raise DefaultConfigurationError("新增默认配置不能指定已有 ID。")
                if kind == "chat" and self.exact(app_id, chat_id or "") is not None:
                    raise DefaultConfigurationError("该聊天已有默认配置，请刷新后修改。", code="revision_conflict")
                position = None
                if kind == "group_name":
                    position = self._db.execute(
                        "SELECT count(*) FROM session_defaults WHERE app_id = ? AND kind = 'group_name'", (app_id,),
                    ).fetchone()[0]
                    if position >= MAX_GROUP_RULES:
                        raise DefaultConfigurationError(f"群名规则最多支持 {MAX_GROUP_RULES} 条。", code="capacity")
                    self._bump_order(app_id)
                rule_id = str(uuid.uuid4())
                self._db.execute(
                    "INSERT INTO session_defaults VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
                    (rule_id, app_id, kind, chat_id, keyword, project,
                     json.dumps(session_settings.to_dict(), ensure_ascii=False), position),
                )
            else:
                if rule_id is None:
                    raise DefaultConfigurationError("修改默认配置需要 ID。")
                previous = self.get(app_id, rule_id)
                self._check_revision(previous, expected_revision)
                if kind != previous.kind or chat_id != previous.chat_id:
                    raise DefaultConfigurationError("默认配置的类型和聊天不可修改；请新建配置。")
                self._db.execute(
                    "UPDATE session_defaults SET keyword = ?, project = ?, session_settings_json = ?, "
                    "revision = revision + 1 WHERE id = ? AND app_id = ?",
                    (keyword, project, json.dumps(session_settings.to_dict(), ensure_ascii=False), rule_id, app_id),
                )
            return self.get(app_id, rule_id)

    def _require_checked_project(self, alias: str, checked_revision: int) -> None:
        from ..bindings import ProjectConflict, ProjectNotFound

        try:
            self.owner.require_project_not_deleting(alias)
            project = self.owner.get_project(alias)
        except (ProjectConflict, ProjectNotFound) as error:
            raise DefaultConfigurationError("Project 已不可用于新建会话，请刷新后重试。", code="project_unavailable") from error
        if project.revision != checked_revision or not project.enabled:
            raise DefaultConfigurationError("Project 已被其他操作修改，请刷新后重试。", code="project_unavailable")

    @staticmethod
    def _check_revision(rule: DefaultRule, revision: int) -> None:
        if rule.revision != revision:
            raise DefaultConfigurationError("默认配置已被其他操作修改，请刷新后重试。", code="revision_conflict")

    def delete(self, app_id: str, rule_id: str, expected_revision: int) -> None:
        with self.owner._transaction():
            rule = self.get(app_id, rule_id)
            self._check_revision(rule, expected_revision)
            self._db.execute("DELETE FROM session_defaults WHERE app_id = ? AND id = ?", (app_id, rule_id))
            if rule.kind == "group_name":
                self._write_order(app_id, [item.id for item in self.group_rules(app_id)])
                self._bump_order(app_id)

    def reorder(self, app_id: str, rule_ids: list[str], order_revision: int) -> int:
        with self.owner._transaction():
            if self._order_revision(self._db, app_id) != order_revision:
                raise DefaultConfigurationError("群名规则顺序已变化，请刷新后重试。", code="revision_conflict")
            current = [rule.id for rule in self.group_rules(app_id)]
            if len(rule_ids) != len(set(rule_ids)) or set(rule_ids) != set(current):
                raise DefaultConfigurationError("排序必须包含当前应用下全部群名规则，且不能重复。")
            self._write_order(app_id, rule_ids)
            return self._bump_order(app_id)

    def _write_order(self, app_id: str, rule_ids: list[str]) -> None:
        # Move out of the dense final range before assigning the new positions;
        # the unique index remains valid throughout the transaction.
        self._db.execute(
            "UPDATE session_defaults SET position = position + ? WHERE app_id = ? AND kind = 'group_name'",
            (MAX_GROUP_RULES + 1, app_id),
        )
        for position, rule_id in enumerate(rule_ids):
            self._db.execute("UPDATE session_defaults SET position = ? WHERE app_id = ? AND id = ?", (position, app_id, rule_id))
