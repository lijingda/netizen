"""Managed Worktree metadata on the Channel's single writer and bounded reader."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from .models import (
    WorktreeConflict, WorktreeCursor, WorktreeDeleteSnapshot, WorktreeNotFound,
    WorktreePage, WorktreeRecord,
)

if TYPE_CHECKING:
    from ..bindings import BindingStore, ProjectRecord, ThreadBinding


INVENTORY_LIMIT = 1000
SCHEMA = (
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


def create_schema(connection: sqlite3.Connection) -> None:
    for statement in SCHEMA:
        connection.execute(statement)


def _record(row: sqlite3.Row) -> WorktreeRecord:
    return WorktreeRecord(
        id=row["worktree_id"], project_alias=row["project_alias"],
        project_generation=row["project_generation"], root=row["root"], cwd=row["cwd"],
        common_dir=row["common_dir"], git_dir=row["git_dir"],
        identity_token=row["identity_token"], origin_binding_id=row["origin_binding_id"],
        creation_state=row["creation_state"], revision=row["revision"], created_at=row["created_at"],
    )


class WorktreeStore:
    def __init__(self, owner: BindingStore) -> None:
        self.owner = owner
        self._delete_intents: dict[str, WorktreeDeleteSnapshot] = {}
        self._creating: set[str] = set()

    @property
    def _db(self) -> sqlite3.Connection:
        return self.owner._connection

    def get(self, worktree_id: str) -> WorktreeRecord:
        with self.owner._lock:
            row = self._db.execute(
                "SELECT * FROM managed_worktrees WHERE worktree_id=?", (worktree_id,),
            ).fetchone()
            if row is None:
                raise WorktreeNotFound(worktree_id)
            return _record(row)

    def register(
        self, *, project_alias: str, expected_project_revision: int, root: str,
        cwd: str, common_dir: str, origin_binding_id: str | None = None,
        worktree_id: str | None = None,
    ) -> WorktreeRecord:
        """Reserve exact creation identity before external Git effects start."""
        from ..bindings import ProjectDisabled, ProjectRevisionConflict, _now

        if not all(isinstance(value, str) and Path(value).is_absolute()
                   for value in (root, cwd, common_dir)):
            raise ValueError("Worktree paths must be absolute")
        if not Path(cwd).is_relative_to(Path(root)):
            raise ValueError("Worktree cwd must be inside its checkout root")
        worktree_id = worktree_id or str(uuid.uuid4())
        with self.owner._lock:
            try:
                with self.owner._transaction():
                    self.owner.require_project_not_deleting(project_alias)
                    project = self.owner.get_project(project_alias)
                    if project.revision != expected_project_revision:
                        raise ProjectRevisionConflict(project_alias)
                    if not project.enabled:
                        raise ProjectDisabled(project_alias)
                    if origin_binding_id is not None:
                        origin = self.owner.get(origin_binding_id)
                        if origin.project_alias != project_alias:
                            raise WorktreeConflict("创建来源会话不属于该 Project。")
                    self._db.execute(
                        "INSERT INTO managed_worktrees(worktree_id,project_alias,project_generation,"
                        "root,cwd,common_dir,origin_binding_id,creation_state,created_at) "
                        "VALUES(?,?,?,?,?,?,?,'creating',?)",
                        (worktree_id, project_alias, project.generation, root, cwd,
                         common_dir, origin_binding_id, _now()),
                    )
                self._creating.add(worktree_id)
            except sqlite3.IntegrityError as error:
                raise WorktreeConflict("工作区标识或目录已经登记；本次未覆盖。") from error
            return self.get(worktree_id)

    def discard_unstarted(self, expected: WorktreeRecord) -> None:
        """Drop only this live reservation after proof no checkout was attempted.

        This never touches Git or filesystem contents and cannot clear a ready,
        unknown, restarted, changed or associated resource.
        """
        with self.owner._lock:
            with self.owner._transaction():
                record = self.get(expected.id)
                self.require_not_deleting(record.id)
                if (record != expected or record.id not in self._creating
                        or record.creation_state != "creating"
                        or record.git_dir is not None or record.identity_token is not None
                        or self._db.execute("SELECT 1 FROM bindings WHERE worktree_id=? LIMIT 1",
                                            (record.id,)).fetchone() is not None):
                    raise WorktreeConflict("创建预留已改变或结果未确认，保留登记。")
                self._db.execute("DELETE FROM managed_worktrees WHERE worktree_id=?", (record.id,))
            self._creating.discard(record.id)

    def mark_ready(self, worktree_id: str, *, git_dir: str, identity_token: str) -> WorktreeRecord:
        if not Path(git_dir).is_absolute() or not identity_token:
            raise ValueError("A ready worktree requires its exact Git identity")
        with self.owner._transaction():
            record = self.get(worktree_id)
            self.require_not_deleting(worktree_id)
            if record.creation_state == "ready":
                if (record.git_dir, record.identity_token) != (git_dir, identity_token):
                    raise WorktreeConflict("工作区身份已经登记，本次未覆盖。")
                return record
            self._db.execute(
                "UPDATE managed_worktrees SET git_dir=?, identity_token=?, creation_state='ready', "
                "revision=revision+1 WHERE worktree_id=?", (git_dir, identity_token, worktree_id),
            )
        with self.owner._lock:
            self._creating.discard(worktree_id)
        return self.get(worktree_id)

    def mark_unknown(self, worktree_id: str) -> WorktreeRecord:
        """Retain a creation receipt without guessing whether Git already succeeded."""
        with self.owner._transaction():
            record = self.get(worktree_id)
            self.require_not_deleting(worktree_id)
            if record.creation_state != "ready":
                self._db.execute(
                    "UPDATE managed_worktrees SET creation_state='unknown', revision=revision+1 "
                    "WHERE worktree_id=?", (worktree_id,),
                )
        with self.owner._lock:
            self._creating.discard(worktree_id)
        return self.get(worktree_id)

    async def list_page(
        self, *, project_alias: str | None = None, project_generation: int | None = None,
        cursor: WorktreeCursor | None = None, limit: int = 50,
    ) -> WorktreePage:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Worktree page limit must be between 1 and 100")
        clauses, values = self._filters(project_alias, project_generation)
        if cursor is not None:
            clauses.append("(created_at,worktree_id) > (?,?)")
            values.extend((cursor.created_at, cursor.worktree_id))
        statement = "SELECT * FROM managed_worktrees"
        if clauses:
            statement += " WHERE " + " AND ".join(clauses)
        statement += " ORDER BY created_at,worktree_id LIMIT ?"
        values.append(limit + 1)

        def query(connection: sqlite3.Connection) -> WorktreePage:
            rows = connection.execute(statement, values).fetchall()
            items = tuple(_record(row) for row in rows[:limit])
            next_cursor = (WorktreeCursor(items[-1].created_at, items[-1].id)
                           if len(rows) > limit else None)
            return WorktreePage(items, next_cursor)

        return await self.owner._submit_query(query, deadline_seconds=2)

    @staticmethod
    def _filters(project_alias: str | None, generation: int | None) -> tuple[list[str], list]:
        clauses: list[str] = []
        values: list = []
        if project_alias is not None:
            clauses.append("project_alias=?")
            values.append(project_alias)
        if generation is not None:
            if project_alias is None or type(generation) is not int or generation < 1:
                raise ValueError("Project generation requires a Project alias and positive integer")
            clauses.append("project_generation=?")
            values.append(generation)
        return clauses, values

    def list_for_project(
        self, alias: str, *, generation: int | None = None, limit: int = INVENTORY_LIMIT,
    ) -> tuple[WorktreeRecord, ...]:
        self._require_limit(limit)
        clauses, values = self._filters(alias, generation)
        with self.owner._lock:
            rows = self._db.execute(
                "SELECT * FROM managed_worktrees WHERE " + " AND ".join(clauses)
                + " ORDER BY worktree_id LIMIT ?", (*values, limit + 1),
            ).fetchall()
        if len(rows) > limit:
            raise WorktreeConflict("关联工作区超过单次操作上限，请先分批处理。")
        return tuple(_record(row) for row in rows)

    def find_by_cwd(
        self, cwd: str, *, project_alias: str | None = None,
        project_generation: int | None = None,
    ) -> WorktreeRecord | None:
        clauses, values = self._filters(project_alias, project_generation)
        clauses.extend(("cwd=?", "creation_state='ready'"))
        values.append(cwd)
        with self.owner._lock:
            row = self._db.execute(
                "SELECT * FROM managed_worktrees WHERE " + " AND ".join(clauses), values,
            ).fetchone()
            return _record(row) if row is not None else None

    @staticmethod
    def _require_limit(limit: int) -> None:
        if type(limit) is not int or not 1 <= limit <= INVENTORY_LIMIT:
            raise ValueError(f"Worktree inventory limit must be between 1 and {INVENTORY_LIMIT}")

    def bindings(self, worktree_id: str, *, limit: int = INVENTORY_LIMIT) -> tuple[ThreadBinding, ...]:
        from ..bindings import _BINDING_SELECT, _binding

        self._require_limit(limit)
        with self.owner._lock:
            rows = self._db.execute(
                _BINDING_SELECT + " WHERE b.worktree_id=? ORDER BY b.binding_id LIMIT ?",
                (worktree_id, limit + 1),
            ).fetchall()
        if len(rows) > limit:
            raise WorktreeConflict("关联会话超过单次操作上限，请先分批处理。")
        return tuple(_binding(row) for row in rows)

    def binding_count(self, worktree_id: str) -> int:
        """Count relationships without materializing a deletion-sized inventory."""
        with self.owner._lock:
            return self._db.execute(
                "SELECT COUNT(*) FROM bindings WHERE worktree_id=?", (worktree_id,),
            ).fetchone()[0]

    def usage_bindings(self, worktree_id: str, *, limit: int = INVENTORY_LIMIT) -> tuple[ThreadBinding, ...]:
        """Known cwd users are protection evidence, never cascade associations."""
        from ..bindings import _BINDING_SELECT, _binding

        self._require_limit(limit)
        with self.owner._lock:
            root = self.get(worktree_id).root.rstrip("/")
            # Absolute canonical cwd facts are path-delimited, not textual-prefix
            # matches. The binary range includes root + '/' but not root + '-x'.
            rows = self._db.execute(
                _BINDING_SELECT + " WHERE (b.worktree_id IS NULL OR b.worktree_id != ?) "
                "AND (b.working_cwd=? OR (b.working_cwd>=? AND b.working_cwd<?)) "
                "ORDER BY b.working_cwd,b.binding_id LIMIT ?",
                (worktree_id, root or "/", root + "/", root + "0", limit + 1),
            ).fetchall()
        if len(rows) > limit:
            raise WorktreeConflict("已知目录使用会话超过单次操作上限，请先分批处理。")
        return tuple(_binding(row) for row in rows)

    def require_not_deleting(self, worktree_id: str) -> None:
        with self.owner._lock:
            if worktree_id in self._delete_intents:
                raise WorktreeConflict("工作区正在删除，请稍后刷新。")

    def require_cwd_not_deleting(self, cwd: str | Path) -> None:
        """Protect known native/lazy paths without adopting their Worktree identity."""
        candidate = Path(os.path.normpath(str(cwd)))
        with self.owner._lock:
            if any(candidate.is_relative_to(Path(intent.record.root))
                   for intent in self._delete_intents.values()):
                raise WorktreeConflict("会话工作目录位于正在删除的工作区内，请稍后重试。")

    def require_project_path_not_deleting(self, canonical_cwd: Path) -> None:
        """The caller resolves cwd before locking; this check does no filesystem I/O."""
        from ..bindings import ProjectConflict

        with self.owner._lock:
            if any(canonical_cwd.is_relative_to(Path(intent.record.root))
                   for intent in self._delete_intents.values()):
                raise ProjectConflict("Project 目录位于正在删除的工作区内，请稍后重试。")

    def require_binding_not_deleting(self, binding_id: str) -> None:
        """Short admission check, including a creator known to be using its result."""
        with self.owner._lock:
            binding = self.owner.get(binding_id)
            if binding.worktree_id is not None:
                self.require_not_deleting(binding.worktree_id)
            if binding.working_cwd is not None:
                self.require_cwd_not_deleting(binding.working_cwd)
            if any(intent.record.origin_binding_id == binding_id for intent in self._delete_intents.values()):
                raise WorktreeConflict("该会话创建的工作区正在删除，请稍后重试。")

    def require_bindable(self, worktree_id: str, *, project: ProjectRecord, cwd: str | None) -> WorktreeRecord:
        with self.owner._lock:
            self.require_not_deleting(worktree_id)
            record = self.get(worktree_id)
            if (record.project_alias, record.project_generation) != (project.alias, project.generation):
                raise WorktreeConflict("工作区不属于当前 Project 登记；本次未重新关联。")
            if record.creation_state != "ready" or record.cwd != cwd:
                raise WorktreeConflict("工作区尚未完成创建或选择目录不匹配。")
            return record

    def require_no_creation(self, alias: str, generation: int) -> None:
        with self.owner._lock:
            if any(item.id in self._creating for item in self.list_for_project(alias, generation=generation)):
                raise WorktreeConflict("Project 仍有在途工作区创建，请完成后重试。")

    def preview_delete(self, worktree_id: str) -> WorktreeDeleteSnapshot:
        with self.owner._lock:
            self.require_not_deleting(worktree_id)
            return self._delete_snapshot(worktree_id)

    def _delete_snapshot(self, worktree_id: str) -> WorktreeDeleteSnapshot:
        record = self.get(worktree_id)
        if worktree_id in self._creating:
            raise WorktreeConflict("工作区仍在创建，请完成后重试。")
        bindings = self.bindings(worktree_id)
        identity = {
            "record": asdict(record),
            "bindings": [(item.id, item.scope_key, item.native_thread_id, item.working_cwd)
                         for item in bindings],
        }
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        return WorktreeDeleteSnapshot(record, bindings, fingerprint)

    def reserve_delete(self, worktree_id: str, *, expected_fingerprint: str) -> WorktreeDeleteSnapshot:
        with self.owner._lock:
            self.require_not_deleting(worktree_id)
            snapshot = self._delete_snapshot(worktree_id)
            if snapshot.fingerprint != expected_fingerprint:
                raise WorktreeConflict("工作区或关联会话已改变，请重新预览并确认。")
            self._delete_intents[worktree_id] = snapshot
            return snapshot

    def release_delete(self, worktree_id: str) -> None:
        with self.owner._lock:
            self._delete_intents.pop(worktree_id, None)

    def finish_delete(self, worktree_id: str, *, expected_fingerprint: str) -> None:
        """Forget only after the caller proves exact Git removal/explicit missing cleanup."""
        with self.owner._transaction():
            intent = self._delete_intents.get(worktree_id)
            if intent is None or intent.fingerprint != expected_fingerprint:
                raise WorktreeConflict("工作区删除确认已经失效。")
            if self.get(worktree_id) != intent.record:
                raise WorktreeConflict("工作区身份已改变，本次未删除登记。")
            # Keep the selected cwd even when the directory no longer exists.
            self._db.execute("UPDATE bindings SET worktree_id=NULL WHERE worktree_id=?", (worktree_id,))
            self._db.execute("DELETE FROM managed_worktrees WHERE worktree_id=?", (worktree_id,))
        self.release_delete(worktree_id)
