"""Scheduled metadata using the BindingStore's writer, lock and transactions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from ..session_settings import SessionSettings, SessionSettingsError

from .models import (
    Claim, MutationResult, Plan, Run, ScheduleConflict, ScheduleError,
    ScheduleNotFound, ScheduleRequestConflict, ScheduleRevisionConflict,
    ScheduleRule, has_trigger_opportunity,
)

if TYPE_CHECKING:
    from ..bindings import BindingStore


MAX_INSTRUCTIONS_JSON_BYTES = 48 * 1024


SCHEMA = (
    """CREATE TABLE schedule_plans (
        plan_id TEXT PRIMARY KEY, revision INTEGER NOT NULL CHECK(revision >= 1),
        name TEXT NOT NULL, instructions TEXT NOT NULL, project_alias TEXT NOT NULL,
        app_id TEXT NOT NULL, chat_id TEXT NOT NULL, schedule_json TEXT,
        enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), next_due_at REAL,
        processed_through REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
        source TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0 CHECK(deleted IN (0,1)),
        session_settings_json TEXT,
        CHECK(deleted = 0 OR (enabled = 0 AND instructions = '' AND schedule_json IS NULL))
    )""",
    "CREATE INDEX schedule_plans_due ON schedule_plans(app_id, enabled, next_due_at) WHERE deleted = 0",
    "CREATE INDEX schedule_plans_project ON schedule_plans(project_alias, deleted)",
    """CREATE TABLE schedule_runs (
        run_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES schedule_plans(plan_id),
        plan_revision INTEGER NOT NULL, due_at REAL NOT NULL,
        project_alias TEXT NOT NULL, app_id TEXT NOT NULL, chat_id TEXT NOT NULL,
        phase TEXT NOT NULL CHECK(phase IN ('claimed','publishing_topic','binding_ready','starting_turn','handed_off','released')),
        barrier TEXT NOT NULL CHECK(barrier IN ('held','unknown','released')),
        root_uuid TEXT NOT NULL UNIQUE, seed_uuid TEXT NOT NULL UNIQUE,
        root_message_id TEXT, topic_id TEXT, origin_message_id TEXT,
        binding_id TEXT, initial_turn_id TEXT, error_code TEXT, delivery_state TEXT,
        created_at REAL NOT NULL, updated_at REAL NOT NULL,
        missed_from REAL, missed_count INTEGER NOT NULL DEFAULT 0,
        binding_removed INTEGER NOT NULL DEFAULT 0 CHECK(binding_removed IN (0,1)),
        UNIQUE(plan_id, due_at)
    )""",
    "CREATE INDEX schedule_runs_plan ON schedule_runs(plan_id, due_at DESC, run_id DESC)",
    "CREATE UNIQUE INDEX schedule_runs_binding ON schedule_runs(binding_id) WHERE binding_id IS NOT NULL",
    "CREATE UNIQUE INDEX schedule_runs_root ON schedule_runs(app_id, chat_id, root_message_id) WHERE root_message_id IS NOT NULL",
    "CREATE UNIQUE INDEX schedule_runs_topic ON schedule_runs(app_id, chat_id, topic_id) WHERE topic_id IS NOT NULL",
    "CREATE UNIQUE INDEX schedule_runs_barrier ON schedule_runs(plan_id) WHERE barrier != 'released'",
    """CREATE TABLE schedule_requests (
        request_id TEXT PRIMARY KEY, operation TEXT NOT NULL, payload_digest TEXT NOT NULL,
        plan_id TEXT NOT NULL, revision INTEGER NOT NULL, expires_at REAL NOT NULL
    )""",
)
TABLE_COLUMNS = {
    "schedule_plans": {
        "plan_id", "revision", "name", "instructions", "project_alias", "app_id", "chat_id",
        "schedule_json", "enabled", "next_due_at", "processed_through", "created_at",
        "updated_at", "source", "deleted", "session_settings_json",
    },
    "schedule_runs": {
        "run_id", "plan_id", "plan_revision", "due_at", "project_alias", "app_id", "chat_id",
        "phase", "barrier", "root_uuid", "seed_uuid", "root_message_id", "topic_id",
        "origin_message_id", "binding_id", "initial_turn_id", "error_code", "delivery_state",
        "created_at", "updated_at", "missed_from", "missed_count", "binding_removed",
    },
    "schedule_requests": {"request_id", "operation", "payload_digest", "plan_id", "revision", "expires_at"},
}
_RUN_UNIQUE_INDEXES = {
    "schedule_runs_binding": (("binding_id",), "binding_id is not null"),
    "schedule_runs_root": (("app_id", "chat_id", "root_message_id"), "root_message_id is not null"),
    "schedule_runs_topic": (("app_id", "chat_id", "topic_id"), "topic_id is not null"),
    "schedule_runs_barrier": (("plan_id",), "barrier != 'released'"),
}
PHASES = {name: index for index, name in enumerate(("claimed", "publishing_topic", "binding_ready", "starting_turn", "handed_off", "released"))}


def create_schema(connection: sqlite3.Connection) -> None:
    for statement in SCHEMA:
        connection.execute(statement)


def require_schema(connection: sqlite3.Connection) -> None:
    _require_table_schema(connection)
    columns = {row["name"]: row for row in connection.execute("PRAGMA table_info(schedule_plans)")}
    if "session_settings_json" not in columns or columns["session_settings_json"]["type"].upper() != "TEXT":
        raise RuntimeError("current Channel database is missing scheduled session settings")
    for row in connection.execute("SELECT deleted, session_settings_json FROM schedule_plans"):
        raw = row["session_settings_json"]
        if row["deleted"]:
            if raw is not None:
                raise RuntimeError("current deleted plan retains session settings")
            continue
        try:
            SessionSettings.from_dict(json.loads(raw))
        except (TypeError, ValueError) as error:
            raise RuntimeError("current scheduled session settings are invalid") from error


def _require_table_schema(connection: sqlite3.Connection) -> None:
    primary_keys = {"schedule_plans": "plan_id", "schedule_runs": "run_id", "schedule_requests": "request_id"}
    for table, required in TABLE_COLUMNS.items():
        schema = connection.execute(f"PRAGMA table_info({table})").fetchall()
        columns = {row["name"] for row in schema}
        if not required <= columns:
            raise RuntimeError(f"current Channel database is missing {table} columns")
        if {row["name"] for row in schema if row["pk"]} != {primary_keys[table]}:
            raise RuntimeError(f"current scheduling primary key has invalid shape: {table}")
    indexes = {
        row["name"]: row for row in connection.execute("PRAGMA index_list(schedule_runs)")
    }
    full_unique_keys = set()
    for name, index in indexes.items():
        # Names come from SQLite, but are quoted as identifiers nonetheless.
        quoted = '"' + name.replace('"', '""') + '"'
        columns = tuple(row["name"] for row in connection.execute(f"PRAGMA index_info({quoted})"))
        if index["unique"] and not index["partial"]:
            full_unique_keys.add(columns)
        expected = _RUN_UNIQUE_INDEXES.get(name)
        if expected is not None:
            sql = connection.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone()[0]
            normalized = " ".join((sql or "").lower().split())
            predicate = normalized.partition(" where ")[2]
            if not index["unique"] or not index["partial"] or columns != expected[0] or predicate != expected[1]:
                raise RuntimeError(f"current scheduling identity index has invalid shape: {name}")
    missing = _RUN_UNIQUE_INDEXES.keys() - indexes.keys()
    if missing:
        raise RuntimeError("current scheduling identity indexes are missing: " + ", ".join(sorted(missing)))
    if not {("run_id",), ("root_uuid",), ("seed_uuid",), ("plan_id", "due_at")} <= full_unique_keys:
        raise RuntimeError("current scheduling occurrence or publication UUID uniqueness is missing")


def _json(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError) as error:
        raise ScheduleError("管理参数必须是有效 JSON，数值必须有限。") from error


def _plan(row: sqlite3.Row) -> Plan:
    return Plan(
        id=row["plan_id"], revision=row["revision"], name=row["name"],
        instructions=row["instructions"], project_alias=row["project_alias"],
        app_id=row["app_id"], chat_id=row["chat_id"],
        schedule=ScheduleRule.from_dict(json.loads(row["schedule_json"])) if row["schedule_json"] else None,
        enabled=bool(row["enabled"]), next_due_at=row["next_due_at"],
        processed_through=row["processed_through"], created_at=row["created_at"],
        updated_at=row["updated_at"], source=row["source"], deleted=bool(row["deleted"]),
        session_settings=SessionSettings.from_dict(json.loads(row["session_settings_json"])) if not row["deleted"] else SessionSettings(),
    )


def _run(row: sqlite3.Row) -> Run:
    fields = dict(row)
    fields["id"] = fields.pop("run_id")
    fields["binding_removed"] = bool(fields["binding_removed"])
    return Run(**fields)


def _has_trigger(
    schedule_json: str | None, enabled: int, next_due_at: float | None,
    processed_through: float | None, now: float,
) -> bool:
    rule = ScheduleRule.from_dict(json.loads(schedule_json)) if schedule_json is not None else None
    return has_trigger_opportunity(
        rule, enabled=bool(enabled), next_due_at=next_due_at,
        processed_through=processed_through, now=now,
    )


class ScheduleStore:
    """A component of one Store; never opens or owns a database connection."""

    def __init__(self, owner: BindingStore) -> None:
        self.owner = owner
        self._db.create_function("netizen_schedule_has_trigger", 5, _has_trigger, deterministic=True)

    @property
    def _db(self) -> sqlite3.Connection:
        return self.owner._connection

    def _now(self, now: float | None) -> float:
        return self.owner._wall_clock() if now is None else now

    def _project(self, alias: str) -> None:
        from ..bindings import ProjectDisabled
        self.owner.require_project_not_deleting(alias)
        if not self.owner.get_project(alias).enabled:
            raise ProjectDisabled(alias)

    def _request(self, request_id: str, operation: str, payload: object, now: float) -> MutationResult | None:
        if not isinstance(request_id, str) or not request_id or len(request_id) > 200:
            raise ScheduleError("写操作需要稳定的 request_id（最多 200 字符）。")
        self._db.execute("DELETE FROM schedule_requests WHERE expires_at <= ?", (now,))
        digest = hashlib.sha256(_json(payload).encode()).hexdigest()
        row = self._db.execute("SELECT * FROM schedule_requests WHERE request_id = ?", (request_id,)).fetchone()
        if row is not None:
            if row["operation"] != operation or row["payload_digest"] != digest:
                raise ScheduleRequestConflict("request_id 已用于不同的操作，请核查原操作。")
            return MutationResult(row["plan_id"], row["revision"], True)
        return None

    def _remember(self, request_id: str, operation: str, payload: object, result: MutationResult, now: float) -> None:
        self._db.execute(
            "INSERT INTO schedule_requests VALUES (?, ?, ?, ?, ?, ?)",
            (request_id, operation, hashlib.sha256(_json(payload).encode()).hexdigest(), result.plan_id, result.revision, now + 7 * 86400),
        )

    def lookup_request(self, request_id: str, operation: str, payload: object, *, now: float | None = None) -> MutationResult | None:
        with self.owner._transaction():
            return self._request(request_id, operation, payload, self._now(now))

    @staticmethod
    def _validate(plan: Plan, now: float, *, require_future: bool = True) -> None:
        for name, value, maximum in (("名称", plan.name, 200), ("执行指令", plan.instructions, 32000), ("Project", plan.project_alias, 200), ("App ID", plan.app_id, 200), ("群 ID", plan.chat_id, 200)):
            if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                raise ScheduleError(f"{name}不能为空且不能超过 {maximum} 字符。")
        if len(_json(plan.instructions).encode("utf-8")) > MAX_INSTRUCTIONS_JSON_BYTES:
            raise ScheduleError("执行指令过长，请缩短或引用文件/文档。")
        if type(plan.enabled) is not bool or not isinstance(plan.schedule, ScheduleRule):
            raise ScheduleError("计划需要有效规则和布尔 enabled。")
        if not isinstance(plan.session_settings, SessionSettings):
            raise ScheduleError("计划需要有效的会话设置。")
        if not isinstance(plan.source, str) or len(plan.source) > 1000:
            raise ScheduleError("来源说明最多 1000 字符。")
        boundary = max(now, plan.processed_through) if plan.processed_through is not None else now
        if require_future and plan.schedule.next_after(boundary) is None:
            raise ScheduleError("计划没有可用的未来触发时间；请调整时间后启用。")
        # Saving a definition must not succeed and then fail when the response
        # renders its common three-occurrence preview.
        plan.schedule.preview(boundary)

    def create(self, *, name: str, instructions: str, project_alias: str, app_id: str, chat_id: str, schedule: ScheduleRule, request_id: str, enabled: bool = True, source: str = "", now: float | None = None, request_payload: object | None = None, session_settings: SessionSettings = SessionSettings()) -> MutationResult:
        now = self._now(now)
        if not isinstance(session_settings, SessionSettings):
            raise ScheduleError("计划需要有效的会话设置。")
        payload = dict(name=name, instructions=instructions, project_alias=project_alias, app_id=app_id, chat_id=chat_id, schedule=schedule.to_dict(), enabled=enabled, source=source)
        payload["session_settings"] = session_settings.to_dict()
        if request_payload is not None:
            payload = request_payload
        with self.owner._transaction():
            if result := self._request(request_id, "create", payload, now):
                return result
            plan = Plan(str(uuid.uuid4()), 1, name, instructions, project_alias, app_id, chat_id, schedule, enabled, schedule.next_after(now) if enabled else None, None, now, now, source, session_settings=session_settings)
            self._validate(plan, now)
            self._project(project_alias)
            self._db.execute(
                "INSERT INTO schedule_plans(plan_id,revision,name,instructions,project_alias,app_id,chat_id,schedule_json,enabled,next_due_at,processed_through,created_at,updated_at,source,deleted,session_settings_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
                (plan.id, 1, name, instructions, project_alias, app_id, chat_id, _json(schedule.to_dict()), int(enabled), plan.next_due_at, None, now, now, source, _json(session_settings.to_dict())),
            )
            result = MutationResult(plan.id, 1)
            self._remember(request_id, "create", payload, result, now)
            return result

    def update(self, plan_id: str, *, expected_revision: int, request_id: str, changes: dict[str, Any], now: float | None = None, request_payload: object | None = None) -> MutationResult:
        allowed = {"name", "instructions", "project_alias", "chat_id", "schedule", "enabled", "session_settings"}
        if not isinstance(changes, dict) or not changes or set(changes) - allowed:
            raise ScheduleError("更新字段为空或包含不可修改字段。")
        changes = dict(changes)
        if isinstance(changes.get("schedule"), dict):
            changes["schedule"] = ScheduleRule.from_dict(changes["schedule"])
        payload_changes = {key: value.to_dict() if isinstance(value, (ScheduleRule, SessionSettings)) else value for key, value in changes.items()}
        payload = dict(plan_id=plan_id, expected_revision=expected_revision, changes=payload_changes)
        if request_payload is not None:
            payload = request_payload
        now = self._now(now)
        with self.owner._transaction():
            if result := self._request(request_id, "update", payload, now):
                return result
            current = self.get(plan_id)
            if type(expected_revision) is not int or current.revision != expected_revision:
                raise ScheduleRevisionConflict("计划已经修改，请刷新后重试。")
            if "session_settings" in changes and isinstance(changes["session_settings"], dict):
                try:
                    changes["session_settings"] = current.session_settings.merge(changes["session_settings"])
                except SessionSettingsError as error:
                    raise ScheduleError(str(error)) from error
            changed = replace(current, **changes, revision=current.revision + 1, updated_at=now)
            schedule_changed = changed.schedule != current.schedule
            enabled_changed = changed.enabled != current.enabled
            enabling = enabled_changed and changed.enabled
            # A full form may carry unchanged fields. Only a real enable or a
            # new once time requires the future; editing a recurring cutoff may
            # intentionally end future triggering.
            self._validate(
                changed, now,
                require_future=enabling or (schedule_changed and isinstance(changed.schedule, ScheduleRule) and changed.schedule.kind == "once"),
            )
            if changed.project_alias != current.project_alias or enabling:
                self._project(changed.project_alias)
            else:
                self.owner.require_project_not_deleting(changed.project_alias)
                self.owner.get_project(changed.project_alias)
            assert changed.schedule is not None
            boundary = max(now, current.processed_through if current.processed_through is not None else now)
            if schedule_changed or enabled_changed:
                due = changed.schedule.next_after(boundary) if changed.enabled else None
            else:
                # Metadata-only changes must not erase an unclaimed occurrence
                # that is still within the normal scheduler grace period.
                due = current.next_due_at
            self._db.execute(
                "UPDATE schedule_plans SET revision=?, name=?, instructions=?, project_alias=?, chat_id=?, schedule_json=?, enabled=?, next_due_at=?, updated_at=?, session_settings_json=? WHERE plan_id=?",
                (changed.revision, changed.name, changed.instructions, changed.project_alias, changed.chat_id, _json(changed.schedule.to_dict()), int(changed.enabled), due, now, _json(changed.session_settings.to_dict()), plan_id),
            )
            result = MutationResult(plan_id, changed.revision)
            self._remember(request_id, "update", payload, result, now)
            return result

    def delete(self, plan_id: str, *, expected_revision: int, request_id: str, now: float | None = None, request_payload: object | None = None) -> MutationResult:
        now = self._now(now)
        payload = dict(plan_id=plan_id, expected_revision=expected_revision)
        if request_payload is not None:
            payload = request_payload
        with self.owner._transaction():
            if result := self._request(request_id, "delete", payload, now):
                return result
            current = self.get(plan_id)
            self.owner.require_project_not_deleting(current.project_alias)
            if type(expected_revision) is not int or current.revision != expected_revision:
                raise ScheduleRevisionConflict("计划已经修改，请刷新后重试。")
            self._tombstone("plan_id = ?", (plan_id,), now)
            result = MutationResult(plan_id, current.revision + 1)
            self._remember(request_id, "delete", payload, result, now)
            return result

    def _tombstone(self, where: str, parameters: tuple[object, ...], now: float) -> None:
        self._db.execute(
            "UPDATE schedule_plans SET deleted=1, enabled=0, name='', instructions='', schedule_json=NULL, session_settings_json=NULL, source='', next_due_at=NULL, revision=revision+1, updated_at=? WHERE deleted=0 AND " + where,
            (now, *parameters),
        )

    def get(self, plan_id: str, *, include_deleted: bool = False) -> Plan:
        with self.owner._lock:
            row = self._db.execute("SELECT * FROM schedule_plans WHERE plan_id=?" + ("" if include_deleted else " AND deleted=0"), (plan_id,)).fetchone()
            if row is None:
                raise ScheduleNotFound("定时计划不存在或已删除。")
            return _plan(row)

    def list(self, *, app_id: str, chat_id: str | None = None, project_alias: str | None = None, enabled: bool | None = None, ended: bool | None = None, now: float | None = None, name: str | None = None, after: str | None = None, limit: int = 20) -> tuple[Plan, ...]:
        if type(limit) is not int or not 1 <= limit <= 1001:
            raise ScheduleError("列表数量必须在 1 到 1001 之间。")
        if ended is not None and type(ended) is not bool:
            raise ScheduleError("ended 必须为 true、false 或 null。")
        where, values = ["app_id=?", "deleted=0"], [app_id]
        for key, value in (("chat_id", chat_id), ("project_alias", project_alias), ("enabled", enabled)):
            if value is not None:
                where.append(f"{key}=?")
                values.append(value)
        if ended is not None:
            # The same pure time calculation serves list filtering and the
            # management projection. Any pending Run keeps the plan unfinished,
            # even when a more recent occurrence was skipped. Filter before LIMIT.
            where.append(
                "(netizen_schedule_has_trigger(schedule_json, enabled, next_due_at, processed_through, ?) = 0 "
                "AND NOT EXISTS (SELECT 1 FROM schedule_runs WHERE schedule_runs.plan_id=schedule_plans.plan_id "
                "AND barrier != 'released')) = ?"
            )
            values.extend((self._now(now), ended))
        if after:
            where.append("plan_id > ?")
            values.append(after)
        if name is not None:
            where.append("instr(name, ?) > 0")
            values.append(name)
        with self.owner._lock:
            rows = self._db.execute("SELECT * FROM schedule_plans WHERE " + " AND ".join(where) + " ORDER BY plan_id LIMIT ?", (*values, limit)).fetchall()
            return tuple(_plan(row) for row in rows)

    def due_plans(self, *, app_id: str, now: float | None = None, limit: int = 100) -> tuple[Plan, ...]:
        with self.owner._lock:
            rows = self._db.execute("SELECT * FROM schedule_plans WHERE app_id=? AND deleted=0 AND enabled=1 AND next_due_at <= ? ORDER BY next_due_at, plan_id LIMIT ?", (app_id, self._now(now), limit)).fetchall()
            return tuple(_plan(row) for row in rows)

    def get_run(self, run_id: str) -> Run:
        with self.owner._lock:
            row = self._db.execute("SELECT * FROM schedule_runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise ScheduleNotFound("定时执行不存在。")
            return _run(row)

    def list_runs(self, plan_id: str, *, after: str | None = None, limit: int = 20) -> tuple[Run, ...]:
        if type(limit) is not int or not 1 <= limit <= 1001:
            raise ScheduleError("列表数量必须在 1 到 1001 之间。")
        where, values = "plan_id=?", [plan_id]
        with self.owner._lock:
            if after:
                cursor = self.get_run(after)
                if cursor.plan_id != plan_id:
                    raise ScheduleError("执行游标不属于该计划。")
                where += " AND (due_at < ? OR (due_at = ? AND run_id < ?))"
                values.extend((cursor.due_at, cursor.due_at, cursor.id))
            rows = self._db.execute("SELECT * FROM schedule_runs WHERE " + where + " ORDER BY due_at DESC, run_id DESC LIMIT ?", (*values, limit)).fetchall()
            return tuple(_run(row) for row in rows)

    def pending_for_plan(self, plan_id: str) -> Run | None:
        with self.owner._lock:
            row = self._db.execute("SELECT * FROM schedule_runs WHERE plan_id=? AND barrier != 'released' LIMIT 1", (plan_id,)).fetchone()
            return _run(row) if row else None

    def project_pending_runs(self, alias: str) -> tuple[Run, ...]:
        with self.owner._lock:
            rows = self._db.execute(
                "SELECT * FROM schedule_runs WHERE project_alias=? "
                "AND (barrier != 'released' OR error_code='publishing_unknown') "
                "ORDER BY run_id", (alias,),
            ).fetchall()
            return tuple(_run(row) for row in rows)

    def pending_runs(self, *, project_alias: str | None = None, limit: int | None = None) -> tuple[Run, ...]:
        with self.owner._lock:
            where = "barrier != 'released'" + (" AND project_alias=?" if project_alias else "")
            parameters: tuple[Any, ...] = (project_alias,) if project_alias else ()
            if limit is not None:
                if type(limit) is not int or not 1 <= limit <= 1001:
                    raise ScheduleError("列表数量必须在 1 到 1001 之间。")
                parameters += (limit,)
            rows = self._db.execute("SELECT * FROM schedule_runs WHERE " + where + " ORDER BY due_at, run_id" + (" LIMIT ?" if limit is not None else ""), parameters).fetchall()
            return tuple(_run(row) for row in rows)

    def pending_route(self, *, app_id: str, chat_id: str, topic_id: str | None = None, root_message_id: str | None = None) -> Run | None:
        if not topic_id and not root_message_id:
            return None
        with self.owner._lock:
            row = self._db.execute(
                "SELECT * FROM schedule_runs WHERE app_id=? AND chat_id=? AND barrier != 'released' AND phase IN ('claimed','publishing_topic','binding_ready','starting_turn') AND (topic_id=? OR root_message_id=?) LIMIT 1",
                (app_id, chat_id, topic_id, root_message_id),
            ).fetchone()
            return _run(row) if row else None

    def _insert_run(self, plan: Plan, due_at: float, now: float, *, reason: str | None = None, missed_from: float | None = None, missed_count: int = 0) -> Run:
        run_id = str(uuid.uuid4())
        self._db.execute(
            "INSERT INTO schedule_runs(run_id,plan_id,plan_revision,due_at,project_alias,app_id,chat_id,phase,barrier,root_uuid,seed_uuid,error_code,created_at,updated_at,missed_from,missed_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, plan.id, plan.revision, due_at, plan.project_alias, plan.app_id, plan.chat_id, "released" if reason else "claimed", "released" if reason else "held", str(uuid.uuid4()), str(uuid.uuid4()), reason, now, now, missed_from, missed_count),
        )
        return self.get_run(run_id)

    def claim_due(self, plan_id: str, *, app_id: str, now: float | None = None, recover: bool = False) -> Claim | None:
        now = self._now(now)
        with self.owner._transaction():
            plan = self.get(plan_id, include_deleted=True)
            due = plan.next_due_at
            if plan.deleted or not plan.enabled or plan.app_id != app_id or due is None or due > now:
                return None
            assert plan.schedule is not None
            self.owner.require_project_not_deleting(plan.project_alias)
            high = plan.processed_through
            if high is not None and due <= high:
                self._db.execute("UPDATE schedule_plans SET next_due_at=? WHERE plan_id=?", (plan.schedule.next_after(max(now, high)), plan.id))
                return None
            last, count = plan.schedule.through(due, now)
            if count == 0:
                self._db.execute("UPDATE schedule_plans SET next_due_at=NULL WHERE plan_id=?", (plan.id,))
                return None
            reason: str | None = None
            if recover or now - last > 60:
                reason = "missed"
            else:
                # Persist one compact range for older points, claim only the
                # latest eligible occurrence within the one-minute grace.
                if count > 1:
                    previous, older_count = plan.schedule.through(due, last - 0.000001)
                    self._insert_run(plan, previous, now, reason="missed", missed_from=due, missed_count=older_count)
                project = self.owner.get_project(plan.project_alias, include_deleted=True)
                pending = self._db.execute("SELECT barrier FROM schedule_runs WHERE plan_id=? AND barrier != 'released' LIMIT 1", (plan.id,)).fetchone()
                if not project.enabled or project.deleted:
                    reason = "project_disabled"
                elif pending:
                    reason = "blocked_unknown" if pending["barrier"] == "unknown" else "skipped_busy"
            run = self._insert_run(plan, last, now, reason=reason, missed_from=due if reason == "missed" else None, missed_count=count if reason == "missed" else 0)
            self._db.execute(
                "UPDATE schedule_plans SET next_due_at=?, processed_through=? WHERE plan_id=?",
                (plan.schedule.next_after(max(now, last)), last, plan.id),
            )
            self._prune(plan.id)
            return Claim(run, plan) if reason is None else None

    def _set_run(self, run_id: str, **changes: Any) -> Run:
        allowed = {"phase", "barrier", "root_message_id", "topic_id", "origin_message_id", "binding_id", "initial_turn_id", "error_code", "delivery_state", "binding_removed"}
        if set(changes) - allowed:
            raise ScheduleError("不支持的执行阶段字段。")
        current = self.get_run(run_id)
        if "phase" in changes:
            phase = changes["phase"]
            if phase not in PHASES:
                raise ScheduleError("无效交接阶段。")
            if PHASES[phase] < PHASES[current.phase]:
                changes.pop("phase")
        if "barrier" in changes and changes["barrier"] not in {"held", "unknown", "released"}:
            raise ScheduleError("无效调度 barrier。")
        if current.barrier == "released":
            changes.pop("barrier", None)
        for field in ("root_message_id", "topic_id", "origin_message_id", "binding_id", "initial_turn_id"):
            if field in changes:
                value, previous = changes[field], getattr(current, field)
                if not isinstance(value, str) or not value or (previous is not None and previous != value):
                    raise ScheduleConflict(f"{field} 是不可替换的 exact identity。")
        if "binding_removed" in changes and (type(changes["binding_removed"]) is not bool or (current.binding_removed and not changes["binding_removed"])):
            raise ScheduleConflict("会话移除事实不可撤销。")
        if changes.get("barrier") == "released":
            changes["phase"] = "released"
        if changes:
            changes["updated_at"] = self._now(None)
            self._db.execute("UPDATE schedule_runs SET " + ",".join(f"{key}=?" for key in changes) + " WHERE run_id=?", (*changes.values(), run_id))
        return self.get_run(run_id)

    def set_run(self, run_id: str, **changes: Any) -> Run:
        try:
            with self.owner._transaction():
                run = self._set_run(run_id, **changes)
                if "delivery_state" in changes:
                    self._prune(run.plan_id)
                return run
        except sqlite3.IntegrityError as error:
            raise ScheduleConflict("交接引用已属于其他定时执行或阶段不合法。") from error

    def begin_publication(self, run_id: str) -> Run:
        """Reserve the only publisher before its first external side effect."""
        with self.owner._transaction():
            run = self.get_run(run_id)
            if (
                run.phase != "claimed" or run.barrier != "held"
                or any((run.root_message_id, run.topic_id, run.origin_message_id,
                        run.binding_id, run.initial_turn_id))
            ):
                raise ScheduleConflict("该次定时执行已经进入发布交接。")
            return self._set_run(run_id, phase="publishing_topic")

    def abandon_pending_deliveries(self) -> None:
        """Startup only: a previous process's final receipt cannot be resumed."""
        with self.owner._transaction():
            rows = self._db.execute(
                "SELECT DISTINCT plan_id FROM schedule_runs "
                "WHERE initial_turn_id IS NOT NULL AND delivery_state IS NULL"
            ).fetchall()
            self._db.execute(
                "UPDATE schedule_runs SET delivery_state='unknown', updated_at=? "
                "WHERE initial_turn_id IS NOT NULL AND delivery_state IS NULL",
                (self._now(None),),
            )
            for row in rows:
                self._prune(row["plan_id"])

    def release(self, run_id: str, *, error_code: str | None = None) -> Run:
        """Caller must possess no-start, exact terminal, or lifecycle proof."""
        with self.owner._transaction():
            changes: dict[str, Any] = {"barrier": "released"}
            if error_code is not None:
                changes["error_code"] = error_code
            run = self._set_run(run_id, **changes)
            self._prune(run.plan_id)
            return run

    def _release_binding(self, binding_id: str, *, binding_removed: bool) -> None:
        rows = self._db.execute("SELECT run_id,plan_id FROM schedule_runs WHERE binding_id=?", (binding_id,)).fetchall()
        for row in rows:
            self._set_run(row["run_id"], barrier="released", binding_removed=binding_removed)
            self._prune(row["plan_id"])

    def release_binding(self, binding_id: str, *, binding_removed: bool = False) -> None:
        with self.owner._transaction():
            self._release_binding(binding_id, binding_removed=binding_removed)

    def _prune(self, plan_id: str) -> None:
        self._db.execute(
            "DELETE FROM schedule_runs WHERE plan_id=? AND barrier='released' "
            "AND COALESCE(error_code, '') != 'publishing_unknown' "
            "AND (initial_turn_id IS NULL OR delivery_state IS NOT NULL) "
            "AND run_id NOT IN (SELECT run_id FROM schedule_runs WHERE plan_id=? "
            "AND barrier='released' AND COALESCE(error_code, '') != 'publishing_unknown' "
            "AND (initial_turn_id IS NULL OR delivery_state IS NOT NULL) "
            "ORDER BY due_at DESC, run_id DESC LIMIT 100)",
            (plan_id, plan_id),
        )
