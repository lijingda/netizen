"""One management boundary for the MCP tool, Feishu cards and Admin."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..bindings import BindingNotFound, BindingStore, ProjectConflict, ProjectDeleting, ProjectDisabled, ProjectNotFound, ScopeNotFound
from ..channel.messages import public_chat_kind
from ..model_settings import ModelCatalog, ModelCatalogError, STANDARD_SERVICE_TIER_ID
from ..session_settings import SessionSettings, SessionSettingsError
from .models import Plan, Run, ScheduleError, ScheduleNotFound, ScheduleRule, plan_lifecycle


_NATIVE_STATUSES = frozenset({"inProgress", "completed", "interrupted", "failed"})
_TERMINAL = _NATIVE_STATUSES - {"inProgress"}
_READ_CONCURRENCY = 4
_READ_TIMEOUT_SECONDS = 5.0
_FIELDS = {
    "options": {"chat_id"},
    "list": {"chat_id", "project", "enabled", "ended", "all", "cursor", "limit", "name"},
    "view": {"plan_id"},
    "create": {"name", "instructions", "project", "chat_id", "schedule", "timezone", "enabled", "request_id", "session_settings"},
    "update": {"plan_id", "expected_revision", "request_id", "name", "instructions", "project", "chat_id", "schedule", "timezone", "enabled", "session_settings"},
    "delete": {"plan_id", "expected_revision", "request_id"},
    "runs": {"plan_id", "cursor", "limit"},
}


class _InputError(ScheduleError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def local_timezone() -> str | None:
    """Resolve an IANA name once; never guess from an ambiguous abbreviation."""
    candidates = [os.environ.get("TZ", "").lstrip(":")]
    try:
        target = str(Path("/etc/localtime").resolve(strict=True))
        if "/zoneinfo/" in target:
            candidates.append(target.split("/zoneinfo/", 1)[1])
    except OSError:
        pass
    try:
        candidates.append(Path("/etc/timezone").read_text().strip())
    except OSError:
        pass
    for candidate in candidates:
        try:
            if candidate:
                ZoneInfo(candidate)
                return candidate
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return None


class ScheduleService:
    def __init__(
        self, *, bindings: BindingStore, runtime: Any, app_id: str,
        chat_info: Any = None, wall_clock: Callable[[], float] = time.time,
        default_timezone: str | None = None,
    ) -> None:
        self.app_id = app_id
        self._bindings = bindings
        self._store = bindings.schedules
        self._runtime = runtime
        self._chat_info = chat_info
        self._clock = wall_clock
        self.default_timezone = default_timezone or local_timezone()
        self._wake: Callable[[], None] = lambda: None
        self._refresh: Callable[[str], Awaitable[str | None]] | None = None
        self._accepting = True

    def set_wake_handler(self, callback: Callable[[], None]) -> None:
        self._wake = callback

    def set_refresh_handler(self, callback: Callable[[str], Awaitable[str | None]]) -> None:
        self._refresh = callback

    def close_admission(self) -> None:
        self._accepting = False

    def _source(self, native_thread_id: str | None, scope_key: str | None) -> tuple[Any, Any]:
        binding = None
        scope = None
        if native_thread_id:
            binding = self._bindings.find_by_native_thread_id(native_thread_id)
            if binding is not None:
                scope = self._bindings.get_scope(binding.scope_key)
        elif scope_key:
            try:
                scope = self._bindings.get_scope(scope_key)
                binding = self._bindings.active_binding(scope_key)
            except ScopeNotFound:
                pass
        if scope is None or scope.app_id != self.app_id:
            return None, None
        return scope, binding

    async def manage(
        self, request: Mapping[str, Any], *, native_thread_id: str | None = None,
        scope_key: str | None = None, source: str = "mcp",
    ) -> dict[str, Any]:
        try:
            if not self._accepting:
                raise _InputError("unavailable", "服务正在停止，暂不接受定时任务管理。")
            if not isinstance(request, Mapping):
                raise ScheduleError("管理请求必须是对象。")
            mode = request.get("mode")
            if not isinstance(mode, str) or mode not in _FIELDS:
                raise ScheduleError("请选择 options/list/view/create/update/delete/runs 操作。")
            # Optional arguments treat null as omission, except the settings
            # object. False remains a real pause/filter value.
            data = {key: value for key, value in request.items() if value is not None or key == "session_settings"}
            if set(data) - (_FIELDS[mode] | {"mode"}):
                raise ScheduleError("该操作包含不支持的字段。")
            now = self._clock()
            # Hash the original structured intent, before clock-dependent
            # anchors or context defaults are materialized. Replays must also
            # work after the plan was edited/deleted or its once time passed.
            request_payload = {
                "app_id": self.app_id, "source": source,
                "context": [native_thread_id, scope_key],
                "request": {key: value for key, value in data.items() if key != "request_id"},
            }
            if mode in {"create", "update", "delete"}:
                request_id = _text(data.get("request_id"), "request_id")
                replay = self._store.lookup_request(request_id, mode, request_payload, now=now)
                if replay is not None:
                    return await self._mutation(replay, now)
            scope, binding = self._source(native_thread_id, scope_key)
            chat_default = scope.chat_id if scope else None
            project_default = binding.project_alias if binding else None
            if mode == "options":
                return await self.options(native_thread_id=native_thread_id, scope_key=scope_key, **{key: data[key] for key in ("chat_id",) if key in data})
            if mode == "list":
                all_plans = data.get("all", False)
                _boolean(all_plans, "all")
                chat = data["chat_id"] if "chat_id" in data else (None if all_plans else chat_default)
                if not all_plans and not chat:
                    raise _InputError("context_required", "无法确定当前会话；请提供 chat_id 或 all=true。")
                filters = {"app_id": self.app_id, "chat_id": chat,
                           "project_alias": data.get("project"), "enabled": data.get("enabled"),
                           "ended": data.get("ended")}
                for field in ("chat_id", "project_alias"):
                    if filters[field] is not None:
                        filters[field] = _text(filters[field], field)
                for field in ("enabled", "ended"):
                    if filters[field] is not None:
                        _boolean(filters[field], field)
                name = data.get("name")
                if name is not None:
                    _text(name, "name")
                size = _page_size(data)
                identity = {**filters, "name": name}
                after = _decode_cursor(data.get("cursor"), identity)
                plans = self._store.list(**filters, now=now, name=name, after=after, limit=size + 1)
                return {"ok": True, "plans": await self._plans(plans[:size], now), "snapshot_at": now,
                        "next_cursor": _encode_cursor(plans[size - 1].id, identity) if len(plans) > size else None,
                        "default_timezone": self.default_timezone}
            if mode == "create":
                chat = _text(data["chat_id"] if "chat_id" in data else chat_default, "chat_id")
                project = _text(data["project"] if "project" in data else project_default, "project")
                chat_kind = await self._validate_chat(chat)
                settings = await self._resolve_session_settings(data, binding=binding, chat_kind=chat_kind)
                rule = self._rule(data, now=now)
                enabled = data.get("enabled", True)
                _boolean(enabled, "enabled")
                result = self._store.create(
                    name=_text(data.get("name"), "name"),
                    instructions=_text(data.get("instructions"), "instructions"),
                    project_alias=project, app_id=self.app_id, chat_id=chat,
                    schedule=rule, enabled=enabled, source=source,
                    session_settings=settings,
                    request_id=_text(data.get("request_id"), "request_id"), now=now,
                    request_payload=request_payload,
                )
                self._wake()
                return await self._mutation(result, now)
            plan = self._store.get(
                _text(data.get("plan_id"), "plan_id"), include_deleted=mode == "runs",
            )
            if plan.app_id != self.app_id:
                raise ScheduleNotFound("当前应用下没有这个定时计划。")
            if mode == "runs":
                size = _page_size(data)
                identity = {"plan_id": plan.id}
                after = _decode_cursor(data.get("cursor"), identity)
            observed: dict[str, str] = {}
            if mode in {"view", "runs"}:
                observed = await self._refresh_pending(plan)
                plan = self._store.get(plan.id, include_deleted=mode == "runs")
                now = self._clock()
            if mode == "view":
                return await self._detail(plan, now, observed=observed)
            if mode == "runs":
                runs = self._store.list_runs(plan.id, after=after, limit=size + 1)
                pending = self._store.pending_for_plan(plan.id)
                lifecycle = plan_lifecycle(plan, now=now, has_pending=pending is not None)
                selected = pending or (runs[0] if runs and not data.get("cursor") else None)
                timezone = plan.schedule.timezone if plan.schedule else "UTC"
                projections = await self._observe_runs([(run, timezone) for run in runs[:size]], observed=observed)
                for value in projections:
                    value["is_last"] = not lifecycle.has_trigger and selected is not None and value["id"] == selected.id
                return {"ok": True, "runs": projections, "snapshot_at": now,
                        "next_cursor": _encode_cursor(runs[size - 1].id, identity) if len(runs) > size else None}
            revision = data.get("expected_revision")
            if type(revision) is not int or revision < 1:
                raise ScheduleError("修改/删除必须提供当前 expected_revision。")
            request_id = _text(data.get("request_id"), "request_id")
            if mode == "delete":
                inflight = self._store.pending_for_plan(plan.id) is not None
                result = self._store.delete(plan.id, expected_revision=revision, request_id=request_id, now=now, request_payload=request_payload)
                self._wake()
                return {"ok": True, "plan_id": result.plan_id, "revision": result.revision,
                        "inflight": inflight, "replayed": result.replayed}
            changes = {key: data[key] for key in ("name", "instructions", "chat_id", "enabled") if key in data}
            if "enabled" in changes:
                _boolean(changes["enabled"], "enabled")
            if "project" in data:
                changes["project_alias"] = _text(data["project"], "project")
            if "session_settings" in data or ("chat_id" in changes and changes["chat_id"] != plan.chat_id):
                chat_kind = await self._validate_chat(_text(changes.get("chat_id", plan.chat_id), "chat_id"))
                changes["session_settings"] = await self._resolve_session_settings(data, previous=plan.session_settings, chat_kind=chat_kind)
            if "schedule" in data or "timezone" in data:
                changes["schedule"] = self._rule(data, now=now, previous=plan.schedule)
            result = self._store.update(plan.id, expected_revision=revision, request_id=request_id, changes=changes, now=now, request_payload=request_payload)
            self._wake()
            return await self._mutation(result, now)
        except (ScheduleError, SessionSettingsError, BindingNotFound, ScopeNotFound, ProjectNotFound, ProjectConflict) as error:
            return _failure(error)

    async def preview(self, request: Mapping[str, Any], *, native_thread_id: str | None = None, scope_key: str | None = None) -> dict[str, Any]:
        try:
            if not isinstance(request, Mapping):
                raise ScheduleError("预览请求必须是对象。")
            now = self._clock()
            scope, binding = self._source(native_thread_id, scope_key)
            plan = self._store.get(_text(request["plan_id"], "plan_id")) if request.get("plan_id") else None
            if plan is not None and plan.app_id != self.app_id:
                raise ScheduleNotFound("当前应用下没有这个定时计划。")
            chat = request["chat_id"] if request.get("chat_id") is not None else (plan.chat_id if plan else scope.chat_id if scope else None)
            kind = await self._validate_chat(_text(chat, "chat_id")) if chat is not None else None
            settings = await self._resolve_session_settings(request, binding=binding, previous=plan.session_settings if plan else None, chat_kind=kind)
            rule = self._rule(request, now=now, previous=plan.schedule if plan else None)
            boundary = max(now, plan.processed_through or now) if plan else now
            return {"ok": True, "schedule": rule.to_dict(),
                    "preview": _preview(rule, boundary),
                    "session_settings": settings.to_dict(), "default_timezone": self.default_timezone}
        except (ScheduleError, SessionSettingsError, BindingNotFound, ScopeNotFound, ProjectNotFound, ProjectConflict) as error:
            return _failure(error)

    async def _catalog(self) -> tuple[ModelCatalog | None, dict[str, str] | None]:
        try:
            async with asyncio.timeout(5):
                return await self._runtime.model_catalog(), None
        except Exception:
            return None, {"code": "model_catalog_unavailable", "message": "Codex 模型目录暂不可用；可保留已有模型设置或选择继承 Codex，稍后再选择新模型。"}

    async def _resolve_session_settings(self, request: Mapping[str, Any], *, binding: Any = None,
                                        previous: SessionSettings | None = None, chat_kind: str | None = None) -> SessionSettings:
        patch = request.get("session_settings", {})
        SessionSettings().merge(patch)  # Reject malformed input before native I/O.
        catalog = None
        catalog_error = None
        if previous is not None:
            base = previous
        elif binding is not None:
            base = SessionSettings.from_binding(binding)
        elif "turn_settings" not in patch:
            catalog, catalog_error = await self._catalog()
            base = SessionSettings.new_defaults(catalog)
        else:
            base = SessionSettings()
        settings = base.merge(patch)
        if chat_kind == "p2p" and settings.message_context_mode.value == "catch-up":
            if "message_context_mode" in patch:
                raise SessionSettingsError("私聊目标只支持 current-only；请将 message_context_mode 设为 current-only。")
            settings = settings.merge({"message_context_mode": "current-only"})
        if settings.turn_settings is not None and (previous is None or settings.turn_settings != previous.turn_settings):
            if catalog is None and catalog_error is None:
                catalog, catalog_error = await self._catalog()
            if catalog is None:
                assert catalog_error is not None
                raise _InputError(catalog_error["code"], catalog_error["message"])
            try:
                settings.validate_catalog(catalog)
            except ModelCatalogError as error:
                raise _InputError("invalid_model_settings", "所选模型、思考强度或速度组合已不可用。请通过 options 读取当前可选项后重新选择；已有计划的其他设置可以单独修改。") from error
        return settings

    async def options(self, *, native_thread_id: str | None = None, scope_key: str | None = None, chat_id: str | None = None) -> dict[str, Any]:
        result, _ = await self.form_options(native_thread_id=native_thread_id, scope_key=scope_key, chat_id=chat_id)
        return result

    async def form_options(self, *, native_thread_id: str | None = None, scope_key: str | None = None, chat_id: str | None = None) -> tuple[dict[str, Any], ModelCatalog | None]:
        try:
            scope, binding = self._source(native_thread_id, scope_key)
            chat = _text(chat_id, "chat_id") if chat_id is not None else scope.chat_id if scope else None
            kind = await self._validate_chat(chat) if chat is not None else None
            catalog, error = await self._catalog()
            settings = SessionSettings.from_binding(binding) if binding else SessionSettings.new_defaults(catalog)
            if kind == "p2p":
                settings = settings.merge({"message_context_mode": "current-only"})
            return {"ok": True, "session_settings": settings.to_dict(), "models": _models(catalog),
                    "context_mode_available": kind != "p2p" if kind is not None else None,
                    "model_catalog_error": error}, catalog
        except (ScheduleError, SessionSettingsError, BindingNotFound, ScopeNotFound) as error:
            return _failure(error), None

    def _rule(self, data: Mapping[str, Any], *, now: float, previous: ScheduleRule | None = None) -> ScheduleRule:
        raw = data.get("schedule")
        if raw is None and previous is not None:
            raw = previous.to_dict()
            if data.get("timezone"):
                raw.pop("timezone", None)
        if not isinstance(raw, dict):
            raise ScheduleError("请提供结构化 schedule。")
        raw = dict(raw)
        timezone = data.get("timezone")
        if timezone is not None and not isinstance(timezone, str):
            raise ScheduleError("timezone 必须是 IANA 时区名称。")
        if raw.get("timezone") is not None and not isinstance(raw["timezone"], str):
            raise ScheduleError("schedule.timezone 必须是 IANA 时区名称。")
        if timezone:
            if raw.get("timezone") and raw["timezone"] != timezone:
                raise ScheduleError("schedule 与外层 timezone 不一致。")
            raw["timezone"] = timezone
        if not raw.get("timezone"):
            raw["timezone"] = previous.timezone if previous else self.default_timezone
        if not raw["timezone"]:
            raise _InputError("timezone_required", "无法确定服务的本地时区，请显式选择 IANA 时区。")
        if raw.get("kind") == "interval" and raw.get("anchor") is None:
            raw["anchor"] = previous.anchor if previous and previous.kind == "interval" else now
        rule = ScheduleRule.from_dict(raw)
        # An unchanged ended definition remains editable. A recurring rule may
        # also be deliberately shortened to end now; only a new definition or
        # a changed one-shot time must provide a future opportunity.
        if (previous is None or (rule.kind == "once" and rule != previous)) and rule.next_after(now) is None:
            raise ScheduleError("时间规则没有未来触发，请检查执行时间和截止条件。")
        return rule

    async def _validate_chat(self, chat_id: str) -> str:
        if self._chat_info is None:
            raise _InputError("unavailable", "会话信息服务暂不可用。")
        try:
            async with asyncio.timeout(5):
                info = await self._chat_info.get_chat_info(chat_id)
        except Exception as error:
            raise _InputError("chat_unavailable", "无法访问目标会话，请检查 chat_id 和机器人是否可访问该会话。") from error
        kind = public_chat_kind(info)
        if kind is None:
            raise _InputError("chat_kind_unknown", "无法确认目标会话类型，请稍后重试或检查 chat_id；这不代表当前会话是私聊。")
        return kind

    async def _mutation(self, result: Any, now: float) -> dict[str, Any]:
        plan = self._store.get(result.plan_id, include_deleted=True)
        if plan.deleted:
            return {"ok": True, "plan_id": result.plan_id, "revision": result.revision,
                    "replayed": result.replayed, "deleted": True,
                    "inflight": self._store.pending_for_plan(plan.id) is not None}
        return {**await self._detail(plan, now), "replayed": result.replayed,
                "operation_revision": result.revision}

    async def _detail(self, plan: Plan, now: float, *, observed: Mapping[str, str] | None = None) -> dict[str, Any]:
        value, = await self._plans((plan,), now, observed=observed)
        return {"ok": True, "plan": value, "snapshot_at": now,
                "preview": _preview(plan.schedule, max(now, plan.processed_through or now)) if plan.schedule else [],
                "inflight": value["inflight"]}

    async def _plans(self, plans: Sequence[Plan], now: float, *, observed: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
        # Snapshot all Channel-owned lifecycle inputs before the first await.
        # Native observation enriches execution only; it never changes barriers
        # or the ended predicate used to select this page.
        snapshots = [self._plan(plan, now) for plan in plans]
        projections = await self._observe_runs([
            (run, plan.schedule.timezone if plan.schedule else "UTC")
            for plan, (_value, run) in zip(plans, snapshots) if run is not None
        ], observed=observed)
        by_id = {run["id"]: run for run in projections}
        for value, run in snapshots:
            if run is not None:
                projection = by_id[run.id]
                value["execution"].update({
                    key: projection[key] for key in (
                        "status", "native_thread_id", "scope_key", "feishu_url",
                    )
                })
        return [value for value, _run in snapshots]

    def _plan(self, plan: Plan, now: float) -> tuple[dict[str, Any], Run | None]:
        value = asdict(plan)
        value["session_settings"] = plan.session_settings.to_dict()
        value["schedule"] = plan.schedule.to_dict() if plan.schedule else None
        timezone = plan.schedule.timezone if plan.schedule else "UTC"
        value["next_due_local"] = _iso(plan.next_due_at, timezone) if plan.next_due_at is not None else None
        pending = self._store.pending_for_plan(plan.id)
        lifecycle = plan_lifecycle(plan, now=now, has_pending=pending is not None)
        value["lifecycle"] = asdict(lifecycle)
        value["status"] = "ended" if lifecycle.ended else "enabled" if plan.enabled else "paused"
        value["inflight"] = pending is not None
        value["blocked_reason"] = "blocked_unknown" if pending and pending.barrier == "unknown" else None
        latest = self._store.list_runs(plan.id, limit=1)
        value["latest_run"] = None
        if latest:
            run = latest[0]
            value["latest_run"] = {
                "id": run.id, "due_at": run.due_at, "phase": run.phase,
                "error_code": run.error_code, "binding_id": run.binding_id,
                "due_local": _iso(run.due_at, timezone),
            }
        selected = pending or (latest[0] if latest else None)
        value["execution"] = {
            "kind": "current" if pending else "latest" if selected else "none",
            "status": "expired" if lifecycle.ended else "not_started",
            "is_last": selected is not None and not lifecycle.has_trigger,
            "run_id": selected.id if selected else None,
            "due_at": selected.due_at if selected else None,
            "due_local": _iso(selected.due_at, timezone) if selected else None,
            "native_thread_id": None, "scope_key": None, "feishu_url": None,
        }
        try:
            if not self._bindings.get_project(plan.project_alias).enabled:
                value["blocked_reason"] = "project_disabled"
        except ProjectNotFound:
            value["blocked_reason"] = "project_unavailable"
        return value, selected

    async def _refresh_pending(self, plan: Plan) -> dict[str, str]:
        """Keep explicit detail/history refresh as the existing recovery entry."""
        pending = self._store.pending_for_plan(plan.id)
        if pending is None:
            return {}
        if self._refresh is not None:
            return {pending.id: await self._refresh(plan.id) or "unavailable"}
        if pending.initial_turn_id is None:
            return {}
        timezone = plan.schedule.timezone if plan.schedule else "UTC"
        value, = await self._observe_runs([(pending, timezone)])
        status = value["status"]
        try:
            if status in _TERMINAL:
                self._store.release(pending.id)
                self._wake()
            elif status == "unavailable":
                self._store.set_run(pending.id, barrier="unknown", error_code="observation_unavailable")
        except ScheduleNotFound:
            pass
        return {pending.id: status}

    async def _observe_runs(self, runs: Sequence[tuple[Run, str]], *, observed: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
        deadline = asyncio.get_running_loop().time() + _READ_TIMEOUT_SECONDS
        slots = asyncio.Semaphore(_READ_CONCURRENCY)
        return list(await asyncio.gather(*(
            self._run(run, timezone=timezone, deadline=deadline, slots=slots, observed=(observed or {}).get(run.id))
            for run, timezone in runs
        )))

    async def _run(self, run: Run, *, timezone: str, deadline: float, slots: asyncio.Semaphore, observed: str | None = None) -> dict[str, Any]:
        value = asdict(run)
        status = run.error_code or ("starting" if run.barrier == "held" else "unknown")
        if run.barrier == "unknown" or run.error_code == "publishing_unknown":
            status = "unknown"
        value.update(status=status, due_local=_iso(run.due_at, timezone), native_thread_id=None, scope_key=None, feishu_url=None)
        if run.root_message_id:
            value["feishu_url"] = "https://applink.feishu.cn/client/chat/open?" + urlencode({"openChatId": run.chat_id, "messageId": run.root_message_id})
        if run.binding_removed:
            value["status"] = "deleted"
            return value
        if run.binding_id:
            try:
                binding = self._bindings.get(run.binding_id)
            except BindingNotFound:
                value["status"] = "unavailable"
                return value
            value.update(native_thread_id=binding.native_thread_id, scope_key=binding.scope_key)
            if run.initial_turn_id:
                if observed is not None:
                    value["status"] = observed
                    return value
                try:
                    async with asyncio.timeout_at(deadline):
                        async with slots:
                            status = await self._runtime.read_scheduled_turn(binding.id, run.initial_turn_id, deadline=deadline)
                    value["status"] = status if status in _NATIVE_STATUSES else "unknown"
                except Exception:
                    value["status"] = "unavailable"
        return value


def _models(catalog: ModelCatalog | None) -> list[dict[str, Any]]:
    if catalog is None:
        return []
    return [{
        "id": model.id, "model": model.model, "display_name": model.display_name,
        "description": model.description, "is_default": model.is_default,
        "default_effort_id": model.default_effort_id, "default_service_tier_id": model.default_service_tier_id,
        "efforts": [{"id": effort.id, "description": effort.description} for effort in model.efforts],
        "service_tiers": [{"id": STANDARD_SERVICE_TIER_ID, "name": "Standard", "description": "Codex 标准服务层"},
                          *[{"id": tier.id, "name": tier.name, "description": tier.description}
                            for tier in model.service_tiers if tier.id != STANDARD_SERVICE_TIER_ID]],
    } for model in catalog.models]


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScheduleError(f"请提供 {field}。")
    return value.strip()


def _boolean(value: Any, field: str) -> None:
    if type(value) is not bool:
        raise ScheduleError(f"{field} 必须为 true 或 false。")


def _page_size(data: Mapping[str, Any]) -> int:
    size = data.get("limit", 20)
    if type(size) is not int or not 1 <= size <= 50:
        raise ScheduleError("每页数量须为 1 到 50。")
    return size


def _fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def _encode_cursor(after: str, identity: object) -> str:
    return base64.urlsafe_b64encode(json.dumps([after, _fingerprint(identity)]).encode()).decode().rstrip("=")


def _decode_cursor(value: Any, identity: object) -> str | None:
    if value is None:
        return None
    try:
        if not isinstance(value, str) or len(value) > 512:
            raise ValueError
        decoded = json.loads(base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True))
        if not isinstance(decoded, list) or len(decoded) != 2 or not isinstance(decoded[0], str) or decoded[1] != _fingerprint(identity):
            raise ValueError
        return decoded[0]
    except (ValueError, binascii.Error, UnicodeDecodeError) as error:
        raise ScheduleError("分页游标无效或筛选条件已改变，请从第一页重新查询。") from error


def _iso(stamp: float, timezone: str = "UTC") -> str:
    return datetime.fromtimestamp(stamp, ZoneInfo(timezone)).isoformat(timespec="minutes")


def _preview(rule: ScheduleRule, now: float) -> list[dict[str, str]]:
    return [{"utc": _iso(stamp), "local": _iso(stamp, rule.timezone)}
            for stamp in rule.preview(now)]


def _failure(error: Exception) -> dict[str, Any]:
    if isinstance(error, ProjectDeleting):
        return {"ok": False, "error": {"code": "project_deleting", "message": "该 Project 正在删除，请等待操作完成后刷新，或选择其他可用的 Project。"}}
    if isinstance(error, ProjectDisabled):
        return {"ok": False, "error": {"code": "project_disabled", "message": "该 Project 已停用，请选择其他可用的 Project，或通过 /settings 重新启用后重试。"}}
    if isinstance(error, ProjectNotFound):
        return {"ok": False, "error": {"code": "not_found", "message": "找不到该 Project，请选择已登记的 Project，或先通过 /settings 登记并启用。"}}
    code = getattr(error, "code", "invalid_schedule")
    if isinstance(error, (ScheduleNotFound, ProjectNotFound, BindingNotFound, ScopeNotFound)):
        code = "not_found"
    return {"ok": False, "error": {"code": code, "message": str(error)}}
