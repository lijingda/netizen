"""Self-contained scheduled-plan cards; no persisted card sessions."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from uuid import uuid4
from zoneinfo import ZoneInfo

from lark_channel import OutboundCard

from ..domain import FeishuScope, MentionContextMode, ScopeKind
from ..model_settings import ModelCatalog
from ..projects import Project
from ..session_settings import SessionSettings
from ..schedules.models import AmbiguousLocalTime, ScheduleError, resolve_once_local
from .callbacks import CardActionError, _builder, _notice, _plain, _plain_text
from .controls import decode_session_settings_form, session_settings_form_elements, session_settings_summary
from .reply import TURN_FILE_CARD_JSON_LIMIT_BYTES


_VERSION = 4
_FORM_PREFIX = "cron_name_v6__"
_KINDS = {"once": "一次性", "daily": "每天", "weekly": "每周", "interval": "固定间隔"}
_FILTERS = {
    "current": "当前会话 · 全部任务", "current_enabled": "当前会话 · 已启用",
    "current_paused": "当前会话 · 已暂停", "all": "全部会话 · 全部任务",
    "all_enabled": "全部会话 · 已启用", "all_paused": "全部会话 · 已暂停",
}
_ACTIONS = {"list", "new", "view", "edit", "delete", "runs", "enabled"}
_FIELDS = {
    "list": (set(), set()),
    "new": (set(), set()),
    "view": ({"plan_id"}, set()),
    "edit": ({"plan_id", "expected_revision"}, set()),
    "delete": ({"plan_id", "expected_revision"}, set()),
    "runs": ({"plan_id"}, {"cursor"}),
    "enabled": ({"plan_id", "expected_revision", "enabled"}, set()),
}
_MAX_INSTRUCTIONS = 8000
_MAX_FORM_INSTRUCTIONS = 1000
_MAX_INSTRUCTION_JSON_BYTES = 12_000
SCHEDULE_CARD_JSON_LIMIT_BYTES = TURN_FILE_CARD_JSON_LIMIT_BYTES
_STATES = {
    "enabled": "已启用", "paused": "已暂停", "ended": "计划已结束",
    "blocked_unknown": "执行状态待确认", "project_disabled": "Project 已停用",
    "project_unavailable": "Project 不可用", "completed": "已完成",
    "failed": "执行失败", "interrupted": "已停止", "inProgress": "运行中",
    "running": "运行中", "deleted": "会话已删除", "unavailable": "状态暂不可读",
    "claimed": "已触发", "publishing_topic": "正在创建话题", "binding_ready": "正在启动",
    "starting": "正在启动", "starting_turn": "正在启动", "handed_off": "已启动", "released": "本次已处理",
    "missed": "已错过", "skipped_busy": "上次仍在运行，本次跳过",
    "scope_conflict": "话题已有会话，本次未启动", "publishing_unknown": "话题发布结果待确认",
    "publishing_failed": "话题发布失败", "dispatch_rejected": "本次未启动",
    "initial_start_rejected": "首次启动条件已改变", "initial_start_unknown": "启动结果待确认",
    "observation_unavailable": "执行状态暂不可读", "sent": "已投递", "unknown": "待确认",
}


def _display_state(value: Any) -> str:
    return _STATES.get(str(value), "状态待确认")


def _utc_time(value: Any) -> str:
    if type(value) not in {int, float}:
        return "未记录"
    return datetime.fromtimestamp(value, UTC).isoformat(timespec="minutes")


def _instructions_fit(value: str) -> bool:
    # Count escaped UTF-8, including control characters, rather than visible
    # glyphs when deciding whether the full instructions fit in the card.
    return len(value) <= _MAX_INSTRUCTIONS and len(json.dumps(value, ensure_ascii=False).encode("utf-8")) <= _MAX_INSTRUCTION_JSON_BYTES


def _session_settings(value: Any) -> SessionSettings:
    if isinstance(value, SessionSettings):
        return value
    try:
        return SessionSettings.from_dict(dict(value) if isinstance(value, Mapping) else value)
    except ValueError as error:
        raise CardActionError("会话配置不完整，请重新发送 /cron。") from error


def _card(builder: Any) -> OutboundCard:
    card = builder.to_dict()
    card["config"]["width_mode"] = "compact"
    card["header"]["icon"] = {"tag": "standard_icon", "token": "calendar_colorful"}
    card["body"].update(padding="12px", vertical_spacing="12px")
    # lark-channel-sdk 1.4.0 OutboundSender serializes cards this exact way,
    # without splitting them. Use the existing, verified Reply Card ceiling.
    if len(json.dumps(card, ensure_ascii=False).encode("utf-8")) > SCHEDULE_CARD_JSON_LIMIT_BYTES:
        raise CardActionError("卡片内容较长，请通过 Admin 或自然语言管理这项计划；内容未被截断保存。")
    return OutboundCard(card=card)


@dataclass(frozen=True, slots=True)
class ScheduleCardAction:
    action: str
    payload: dict[str, Any]
    request_id: str
    navigation: dict[str, Any]


def schedule_navigation(value: Any = None) -> dict[str, Any]:
    """Validate self-contained UI state, separate from management requests."""
    if value is None:
        return {"filter": "current_enabled"}
    if not isinstance(value, Mapping) or set(value) - {"filter", "cursor", "plan_id"}:
        raise CardActionError("定时任务卡片导航无效，请重新发送 /cron。")
    selected_filter = value.get("filter", "current_enabled")
    if not isinstance(selected_filter, str) or selected_filter not in _FILTERS:
        raise CardActionError("定时任务筛选条件无效。")
    result = {"filter": selected_filter}
    for field, limit in (("cursor", 512), ("plan_id", 64)):
        if field in value:
            item = value[field]
            if not isinstance(item, str) or not item.strip() or len(item) > limit:
                raise CardActionError("定时任务卡片导航无效，请重新发送 /cron。")
            result[field] = item
    return result


def schedule_query(navigation: Any = None) -> dict[str, Any]:
    state = schedule_navigation(navigation)
    selected_filter = state["filter"]
    query: dict[str, Any] = {}
    if selected_filter.startswith("all"):
        query["all"] = True
    if selected_filter.endswith("_enabled"):
        query["enabled"] = True
        query["ended"] = False
    elif selected_filter.endswith("_paused"):
        query["enabled"] = False
    if "cursor" in state:
        query["cursor"] = state["cursor"]
    return query


def _encoded(value: Mapping[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).decode().rstrip("=")


def _decoded(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise CardActionError("定时任务选项无效，请重新选择。")
    try:
        result = json.loads(base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True))
    except (ValueError, UnicodeError) as error:
        raise CardActionError("定时任务选项无效，请重新选择。") from error
    if not isinstance(result, dict):
        raise CardActionError("定时任务选项无效，请重新选择。")
    return result


def is_schedule_card_action(value: Any, form: Any = None) -> bool:
    return (
        isinstance(value, Mapping) and value.get("kind") == "netizen_cron"
    ) or (
        isinstance(form, Mapping)
        and any(isinstance(key, str) and key.startswith(_FORM_PREFIX) for key in form)
    )


def decode_schedule_action(*, scope: FeishuScope, value: Any, form: Any = None) -> ScheduleCardAction:
    if form is not None and form != {}:
        return _decode_form(scope, form)
    if not isinstance(value, Mapping) or set(value) != {"kind", "v", "scope", "action", "payload", "request_id", "navigation", "nonce"}:
        raise CardActionError("定时任务卡片动作无效，请重新发送 /cron。")
    if value["kind"] != "netizen_cron" or type(value["v"]) is not int or value["v"] != _VERSION:
        raise CardActionError("定时任务卡片已过期，请重新发送 /cron。")
    if value["scope"] != scope.key or value["action"] not in _ACTIONS:
        raise CardActionError("定时任务卡片与当前会话不一致，请在当前会话重新发送 /cron。")
    if not isinstance(value["payload"], dict) or not isinstance(value["request_id"], str) or not value["request_id"]:
        raise CardActionError("定时任务卡片缺少操作凭据。")
    if not isinstance(value["nonce"], str) or not re.fullmatch(r"[0-9a-f]{32}", value["nonce"]):
        raise CardActionError("定时任务卡片缺少交互凭据。")
    required, optional = _FIELDS[value["action"]]
    fields = set(value["payload"])
    if not required <= fields or fields - required - optional:
        raise CardActionError("定时任务卡片动作字段不完整或含未知字段。")
    return ScheduleCardAction(value["action"], dict(value["payload"]), value["request_id"], schedule_navigation(value["navigation"]))


def _button(scope: FeishuScope, label: str, action: str, payload: dict[str, Any] | None = None, *, navigation: Any = None, danger: bool = False, primary: bool = False) -> dict[str, Any]:
    return {
        "tag": "button", "text": _plain_text(label),
        "type": "danger" if danger else "primary_filled" if primary else "default", "width": "fill",
        "behaviors": [{"type": "callback", "value": {
            "kind": "netizen_cron", "v": _VERSION, "scope": scope.key,
            "action": action, "payload": payload or {}, "request_id": str(uuid4()),
            "nonce": uuid4().hex,
            "navigation": schedule_navigation(navigation),
        }}],
    }


def _row(*items: dict[str, Any]) -> dict[str, Any]:
    return {"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px", "columns": [
        {"tag": "column", "width": "weighted", "weight": 1, "elements": [item]} for item in items
    ]}


def _section(*items: dict[str, Any]) -> dict[str, Any]:
    return {"tag": "column_set", "columns": [{"tag": "column", "width": "weighted", "weight": 1,
        "background_style": "grey-50", "padding": "12px", "vertical_spacing": "8px", "elements": list(items)}]}


def _labeled(label: str, control: dict[str, Any]) -> dict[str, Any]:
    # Unlike input, Feishu select/picker components do not accept `label`.
    return {"tag": "column_set", "columns": [{"tag": "column", "width": "weighted", "weight": 1,
        "vertical_spacing": "4px", "elements": [_plain(label), control]}]}


def _fold(title: str, *items: dict[str, Any], expanded: bool = False) -> dict[str, Any]:
    return {"tag": "collapsible_panel", "expanded": expanded, "header": {"title": _plain_text(title)}, "elements": list(items)}


def _form_name(scope: FeishuScope, kind: str, nonce: str) -> str:
    # Feishu caps each component name at 100 characters. Keep the common
    # identity small; edit revision / rule evidence travels on its own field.
    identity = [hashlib.sha256(scope.key.encode()).hexdigest()[:20], kind[0], nonce]
    return _FORM_PREFIX + base64.urlsafe_b64encode(json.dumps(identity, separators=(",", ":")).encode()).decode().rstrip("=")


def _navigation_form(scope: FeishuScope, *, kind: str, label: str, options: Sequence[tuple[str, str]], selected: str | None, submit: str) -> dict[str, Any]:
    # A fresh field identity prevents SDK event dedup from swallowing A → B → A
    # selections on this same card. A form submit uses the public form_value;
    # the pinned SDK does not forward standalone select_static's option field.
    name = _form_name(scope, kind, uuid4().hex)
    control = {"tag": "select_static", "name": name, "required": True, "width": "fill",
        "placeholder": _plain_text(label), "options": [{"text": _plain_text(text), "value": value} for value, text in options]}
    if selected is not None:
        control["initial_option"] = selected
    row = _row(control,
        {"tag": "button", "name": "cron_" + kind + "_submit", "text": _plain_text(submit),
         "type": "default", "width": "fill", "form_action_type": "submit"})
    row["columns"][0]["weight"] = 3
    return {"tag": "form", "name": "cron_" + kind, "elements": [row]}


def schedule_manager_card(scope: FeishuScope, result: dict[str, Any], *, navigation: Any = None,
                          selected: dict[str, Any] | None = None, runs: dict[str, Any] | None = None,
                          notice: str | None = None) -> OutboundCard:
    state = schedule_navigation(navigation)
    plan = selected.get("plan") if selected else None
    if plan is not None:
        state = schedule_navigation({**state, "plan_id": plan["id"]})
    builder = _builder("定时任务", _FILTERS[state["filter"]])
    if notice:
        builder.raw(_notice(notice))
    builder.raw(_navigation_form(scope, kind="filter", label="筛选任务",
        options=[(_encoded({"filter": key}), label) for key, label in _FILTERS.items()],
        selected=_encoded({"filter": state["filter"]}), submit="确认筛选"))
    plans = list(result.get("plans", []))
    # A successful edit may move the selected record outside this page's ID
    # window. The handler verifies its filter, then supplies the exact result.
    if plan is not None and all(item["id"] != plan["id"] for item in plans):
        plans.append(plan)
    if plans:
        builder.raw(_navigation_form(scope, kind="manage", label="选择定时任务",
            options=[(_encoded({**state, "plan_id": item["id"]}),
                f"{str(item.get('name', ''))[:60]} · {_display_state(item.get('status') or ('enabled' if item.get('enabled') else 'paused'))} · {str(item['id'])[:8]}") for item in plans],
            selected=_encoded(state) if plan is not None else None, submit="查看任务"))
    else:
        builder.raw(_plain("还没有符合条件的任务。创建后，每次执行都会在目标会话开启一个独立话题。"))
    pages = []
    if state.get("cursor"):
        pages.append(_button(scope, "回到首页", "list", navigation={"filter": state["filter"]}))
    if result.get("next_cursor"):
        pages.append(_button(scope, "下一页任务", "list", navigation={"filter": state["filter"], "cursor": result["next_cursor"]}))
    if pages:
        builder.raw(_row(*pages))
    if plan is not None:
        _render_selected_plan(builder, scope, selected, navigation=state)
        if runs is not None:
            builder.raw(_runs_panel(scope, runs, plan_id=plan["id"], navigation=state))
    elif plans:
        builder.raw(_plain("选择一个任务后，在这里查看详情和管理。"))
    builder.divider()
    builder.raw(_button(scope, "新建定时任务", "new", navigation=state, primary=True))
    return _card(builder)


def _plan_summary(plan: Mapping[str, Any]) -> str:
    rule = plan.get("schedule", {})
    status = _display_state(plan.get("status") or ("enabled" if plan.get("enabled") else "paused"))
    blocked = plan.get("blocked_reason")
    rule_text = _KINDS.get(rule.get("kind"), str(rule.get("kind", "")))
    if rule.get("at"):
        rule_text += " " + str(rule["at"])
    if rule.get("kind") == "weekly":
        rule_text += " " + "、".join("周" + "一二三四五六日"[day] for day in rule.get("weekdays", []) if type(day) is int and 0 <= day <= 6)
    if rule.get("kind") == "interval":
        rule_text += f" {rule.get('every_minutes')} 分钟"
    deadline = ""
    if rule.get("kind") in {"daily", "weekly", "interval"}:
        end_at = rule.get("end_at")
        end = datetime.fromisoformat(end_at).astimezone(ZoneInfo(rule["timezone"])).isoformat(timespec="minutes") if end_at else "不限"
        deadline = f"\n截止：{end}" + ("（含该时刻）" if end_at else "")
    return (
        f"{plan.get('name', '')} · {str(plan.get('id', ''))[:8]}\n"
        f"Project：{plan.get('project_alias', plan.get('project', ''))}\n"
        f"目标会话：{plan.get('chat_id', '')}\n"
        f"时间：{rule_text} · {rule.get('timezone', '')}\n"
        f"状态：{status}" + (f" · {_display_state(blocked)}" if blocked else "")
        + deadline
        + f"\n下次：{plan.get('next_due_local') or '无'}"
        + (f"\n最近触发：{plan['latest_run']['due_local']}" if plan.get("latest_run") else "")
    )


def _render_selected_plan(builder: Any, scope: FeishuScope, result: dict[str, Any], *, navigation: dict[str, Any]) -> None:
    plan = result["plan"]
    builder.raw(_section(_plain(_plan_summary(plan))))
    instructions = str(plan.get("instructions", ""))
    if _instructions_fit(instructions):
        builder.raw(_fold("执行指令", _plain(instructions), expanded=len(instructions) <= 200))
    else:
        builder.raw(_plain("执行指令（内容节选）\n" + instructions[:2000] + "…"))
        builder.raw(_notice("完整指令较长，请通过 Admin 或自然语言查看、编辑。计划原文完整保留，仍可在此启停或删除。"))
    builder.raw(_fold("会话配置", _plain(session_settings_summary(_session_settings(plan.get("session_settings", SessionSettings()))))))
    if result.get("inflight"):
        builder.raw(_notice("本次已触发，修改、暂停或删除从后续触发生效；本次交接可以继续。"))
    exact = {"plan_id": plan["id"], "expected_revision": plan["revision"]}
    builder.raw(_row(_button(scope, "编辑", "edit", exact, navigation=navigation, primary=True),
        _button(scope, "暂停" if plan.get("enabled") else "启用", "enabled", {**exact, "enabled": not plan.get("enabled")}, navigation=navigation)))
    delete = _button(scope, "删除计划", "delete", exact, navigation=navigation, danger=True)
    delete["confirm"] = {"title": _plain_text("删除定时任务？"),
        "text": _plain_text("删除后停止后续触发，保留已有普通会话。已认领的本次交接仍可能继续。删除的计划不能恢复。")}
    builder.raw(_row(_button(scope, "最近执行", "runs", {"plan_id": plan["id"]}, navigation=navigation),
        _button(scope, "刷新任务", "view", {"plan_id": plan["id"]}, navigation=navigation), delete))


def _runs_panel(scope: FeishuScope, result: dict[str, Any], *, plan_id: str, navigation: dict[str, Any]) -> dict[str, Any]:
    items = []
    runs = result.get("runs", [])
    if not runs:
        items.append(_plain("还没有触发记录。"))
    for run in runs:
        items.append(_plain(
            f"时间：{run.get('due_local') or _utc_time(run.get('due_at'))}\n"
            f"状态：{_display_state(run.get('status') or run.get('error_code') or run.get('phase'))}\n"
            f"结果投递：{ {'sent': '已投递', 'failed': '投递失败', 'unknown': '待确认'}.get(run.get('delivery_state'), '尚未投递')}"
            + (f"\n合并漏跑：{run['missed_count']} 次" if run.get("missed_count", 0) > 1 else "")
        ))
        items.append({"tag": "hr"})
    if result.get("next_cursor"):
        items.append(_button(scope, "更早执行", "runs", {"plan_id": plan_id, "cursor": result["next_cursor"]}, navigation=navigation))
    return _fold("最近执行", *items, expanded=True)


def _input(name: str, label: str, value: str = "", *, required: bool = True, multiline: bool = False) -> dict[str, Any]:
    return {"tag": "input", "name": name, "label": _plain_text(label), "required": required,
            "default_value": value, "input_type": "multiline_text" if multiline else "text", "width": "fill",
            **({"rows": 3, "max_length": _MAX_FORM_INSTRUCTIONS, "placeholder": _plain_text("例如：检查项目进展，整理今天的更新并发到本话题。")} if multiline else {})}


def _select(name: str, label: str, options: Sequence[tuple[str, str]], selected: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"tag": "select_static", "name": name, "required": True,
        "width": "fill", "placeholder": _plain_text(label), "options": [{"text": _plain_text(text), "value": value} for value, text in options]}
    if selected is not None:
        result["initial_option"] = selected
    return _labeled(label, result)


def schedule_form_card(scope: FeishuScope, *, projects: Sequence[Project], default_timezone: str | None,
                       plan: Mapping[str, Any] | None = None,
                       initial_project: str | None = None, session_settings: SessionSettings | Mapping[str, Any] | None = None,
                       catalog: ModelCatalog | None = None, catalog_error: str | None = None,
                       allow_context_mode: bool = False, navigation: Any = None,
                       _retry_form: Mapping[str, Any] | None = None, _request_id: str | None = None,
                       _notice_text: str | None = None) -> OutboundCard:
    navigation = schedule_navigation(navigation)
    builder = _builder("编辑定时任务" if plan else "新建定时任务", "填写内容与时间，保存后按计划执行")
    existing = dict(plan or {})
    settings = _session_settings(existing.get("session_settings", session_settings if session_settings is not None else SessionSettings()))
    if not allow_context_mode and settings.message_context_mode is MentionContextMode.CATCH_UP:
        raise CardActionError("私聊不能自动读取群聊讨论，请先将消息范围设为仅当前消息。")
    if len(str(existing.get("instructions", ""))) > _MAX_FORM_INSTRUCTIONS:
        if plan:
            return schedule_manager_card(scope, {"plans": [plan]}, selected={"plan": plan}, navigation=navigation,
                notice="这项计划的完整指令超过卡片编辑容量，请通过 Admin 或自然语言修改。")
        raise CardActionError("执行指令超过卡片编辑容量，请通过 Admin 或自然语言维护。")
    if not projects:
        notice = "没有可用的 Project，请先通过 /settings 登记并启用 Project 后再编辑。"
        if plan:
            return schedule_manager_card(scope, {"plans": [plan]}, selected={"plan": plan}, navigation=navigation, notice=notice)
        builder.raw(_plain(notice))
        builder.raw(_button(scope, "取消", "list", navigation=navigation))
        return _card(builder)
    rule = existing.get("schedule", {})
    kind = rule.get("kind", "once")
    if kind not in _KINDS:
        raise CardActionError("未知时间规则。")
    meta = {"request_id": _request_id or uuid4().hex}
    if plan:
        meta.update(plan_id=plan["id"], expected_revision=plan["revision"])
    if kind == "interval" and rule.get("anchor") is not None:
        meta["anchor"] = rule["anchor"]
    timezone = str(rule.get("timezone") or default_timezone or "")
    once_at = None
    if kind == "once" and rule.get("at"):
        once_at = datetime.fromisoformat(rule["at"]).astimezone(ZoneInfo(timezone))
        # An unchanged edit preserves an explicit second DST-overlap occurrence.
        meta["original_at"] = once_at.isoformat(timespec="minutes")
    end_at = None
    if kind != "once" and rule.get("end_at"):
        end_at = datetime.fromisoformat(rule["end_at"]).astimezone(ZoneInfo(timezone))
        meta["original_end_at"] = rule["end_at"]
    instructions_name = "cron_instructions"
    if meta.get("plan_id"):
        instructions_name += f"__{meta['plan_id']}:{meta['expected_revision']}"
    timezone_name = "cron_timezone" + (f"__{meta['original_at']}" if "original_at" in meta else "")
    elements = [
        _input(_form_name(scope, "plan", uuid4().hex), "名称", str(existing.get("name", ""))),
        _input(instructions_name, "执行内容", str(existing.get("instructions", "")), multiline=True),
        _row(_select("cron_kind", "执行频率", list(_KINDS.items()), kind), _input(timezone_name, "时区", timezone)),
    ]
    date = {"tag": "date_picker", "name": "cron_date", "required": False, "width": "fill", "placeholder": _plain_text("选择日期")}
    if once_at:
        date["initial_date"] = once_at.strftime("%Y-%m-%d")
    time = {"tag": "picker_time", "name": "cron_at", "required": False, "width": "fill",
        "placeholder": _plain_text("选择时间"), "initial_time": once_at.strftime("%H:%M") if once_at else rule.get("at") or "09:00"}
    elements.append(_row(_labeled("日期 · 仅一次性", date), _labeled("时间 · 一次性 / 每天 / 每周", time)))
    weekdays = {"tag": "multi_select_static", "name": "cron_weekdays", "required": False,
        "width": "fill", "placeholder": _plain_text("选择星期"),
        "options": [{"text": _plain_text("周" + "一二三四五六日"[day]), "value": str(day)} for day in range(7)],
        "selected_values": [str(day) for day in rule.get("weekdays", (0, 1, 2, 3, 4))]}
    minutes_name = "cron_every_minutes" + (f"__{meta['anchor']}" if "anchor" in meta else "")
    elements.append(_row(_labeled("星期 · 仅每周", weekdays),
        _input(minutes_name, "分钟间隔 · 仅固定间隔", str(rule.get("every_minutes") or 60), required=False)))
    end_time_name = "cron_end_time" + (f"__{meta['original_end_at']}" if "original_end_at" in meta else "")
    end_date = {"tag": "date_picker", "name": "cron_end_date", "required": False, "width": "fill", "placeholder": _plain_text("不限截止日期")}
    end_time = {"tag": "picker_time", "name": end_time_name, "required": False, "width": "fill", "placeholder": _plain_text("选择截止时间")}
    if end_at:
        end_date["initial_date"] = end_at.strftime("%Y-%m-%d")
        end_time["initial_time"] = end_at.strftime("%H:%M")
    elements.append(_plain("截止时间 · 仅每天 / 每周 / 固定间隔（可选）"))
    elements.append(_row(_labeled("截止日期", end_date), _labeled("截止时间（含该时刻）", end_time)))
    elements.append(_plain("截止日期与时间使用上方时区；同时留空可取消截止。"))
    selected_project = existing.get("project_alias") or initial_project
    if selected_project is not None and selected_project not in {project.alias for project in projects}:
        elements.append(_notice(f"Project「{selected_project}」已停用或不可用，请选择可用的 Project；保存前不会更改计划。"))
        selected_project = None
    elif selected_project is None:
        selected_project = projects[0].alias
    project_meta = {"navigation": navigation, "scope": scope.key, "request_id": meta["request_id"]}
    project_options = [(_encoded({"project": p.alias, **project_meta}), p.alias) for p in projects]
    selected_project_value = _encoded({"project": selected_project, **project_meta}) if selected_project is not None else None
    elements.append(_select("cron_project", "执行 Project", project_options, selected_project_value))
    elements.append(_fold("目标会话", _input("cron_chat_id", "目标会话 ID（留空使用当前会话）", str(existing.get("chat_id", "")), required=False)))
    elements.append(_fold("会话配置",
        _plain(session_settings_summary(settings, allow_context_mode=allow_context_mode)),
        *session_settings_form_elements(prefix="cron_session", settings=settings, catalog=catalog,
            catalog_error=catalog_error, allow_context_mode=allow_context_mode)))
    elements.append(_plain("只使用所选频率对应的日期、时间、星期或间隔；日期与时间以所填时区为准。每次执行会开启独立话题。"))
    elements.append({"tag": "button", "name": "cron_save", "text": _plain_text("保存修改" if plan else "创建任务"), "type": "primary_filled", "width": "fill", "form_action_type": "submit"})
    if _retry_form is not None:
        _restore_form_fields(elements, _retry_form)
    if _notice_text:
        builder.raw(_notice(_notice_text))
    builder.raw({"tag": "form", "name": "cron_plan", "elements": elements})
    builder.raw(_button(scope, "取消", "view" if plan else "list", {"plan_id": plan["id"]} if plan else {}, navigation=navigation))
    return _card(builder)


def _form_identity(scope: FeishuScope, form: Any) -> tuple[str, list[str]]:
    if not isinstance(form, Mapping):
        raise CardActionError("定时任务表单无效。")
    names = [name for name in form if isinstance(name, str) and name.startswith(_FORM_PREFIX)]
    if len(names) != 1:
        raise CardActionError("定时任务表单身份无效，请重新打开。")
    encoded = names[0][len(_FORM_PREFIX):]
    try:
        identity = json.loads(base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True))
    except (ValueError, UnicodeError) as error:
        raise CardActionError("定时任务表单身份无效。") from error
    if (not isinstance(identity, list) or len(identity) != 3 or identity[0] != hashlib.sha256(scope.key.encode()).hexdigest()[:20]
            or not isinstance(identity[1], str) or identity[1] not in {"p", "f", "m"}
            or not isinstance(identity[2], str) or not re.fullmatch(r"[0-9a-f]{32}", identity[2])):
        raise CardActionError("定时任务表单与原消息会话不一致，请在当前会话重新发送 /cron。")
    return names[0], identity


def _plan_form_metadata(form: Mapping[str, Any]) -> dict[str, Any]:
    """Parse write identity only, without validating editable business values."""
    meta = _decoded(_text(form.get("cron_project"), "Project"))
    if set(meta) != {"project", "navigation", "scope", "request_id"}:
        raise CardActionError("定时任务 Project 选项无效，请重新选择。")
    if not isinstance(meta["request_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", meta["request_id"]):
        raise CardActionError("定时任务表单缺少操作凭据。")
    meta["project"] = _text(meta["project"], "Project")
    meta["navigation"] = schedule_navigation(meta["navigation"])
    instructions = [name for name in form if isinstance(name, str) and (name == "cron_instructions" or name.startswith("cron_instructions__"))]
    if len(instructions) != 1:
        raise CardActionError("定时任务表单缺少执行指令字段或包含重复字段。")
    if instructions[0] != "cron_instructions":
        match = re.fullmatch(r"cron_instructions__([A-Za-z0-9_-]{1,64}):([1-9][0-9]{0,17})", instructions[0])
        if not match:
            raise CardActionError("定时任务表单的计划版本无效。")
        meta.update(plan_id=match[1], expected_revision=int(match[2]))
    return meta


def _decode_form(scope: FeishuScope, form: Any) -> ScheduleCardAction:
    name, identity = _form_identity(scope, form)
    names = [name]
    form_kind = {"p": "plan", "f": "filter", "m": "manage"}[identity[1]]
    form = dict(form)
    if form_kind in {"filter", "manage"}:
        if set(form) != {names[0]}:
            raise CardActionError("定时任务选择字段无效。")
        choice = _decoded(form[names[0]])
        if (form_kind == "filter" and set(choice) != {"filter"}) or (form_kind == "manage" and "plan_id" not in choice):
            raise CardActionError("定时任务选项无效，请重新选择。")
        return ScheduleCardAction("list", {}, identity[2], schedule_navigation(choice))
    meta = _plan_form_metadata(form)
    if meta["scope"] != scope.key:
        raise CardActionError("定时任务 Project 选项与当前会话不一致。")
    kind = _text(form.get("cron_kind"), "执行频率")
    if kind not in _KINDS:
        raise CardActionError("未知时间规则。")
    for name in tuple(form):
        if not isinstance(name, str) or not name.startswith(("cron_instructions__", "cron_timezone__", "cron_every_minutes__", "cron_end_time__")):
            continue
        field, reference = name.split("__", 1)
        if field in form:
            raise CardActionError("定时任务表单包含重复字段。")
        if field == "cron_timezone":
            if kind == "once":
                meta["original_at"] = reference
        elif field == "cron_end_time":
            if kind != "once":
                meta["original_end_at"] = reference
        elif field == "cron_every_minutes":
            if kind == "interval":
                try:
                    meta["anchor"] = float(reference)
                except ValueError as error:
                    raise CardActionError("定时任务表单的间隔起点无效。") from error
        form[field] = form.pop(name)
    settings_fields = {key: form.pop(key) for key in tuple(form) if isinstance(key, str) and key.startswith("cron_session_")}
    settings = decode_session_settings_form(settings_fields, prefix="cron_session")
    expected = {names[0], "cron_instructions", "cron_project", "cron_chat_id", "cron_timezone", "cron_kind"}
    optional = {"cron_every_minutes", "cron_at", "cron_date", "cron_weekdays", "cron_end_date", "cron_end_time"}
    if not expected <= set(form) or set(form) - expected - optional:
        raise CardActionError("定时任务表单缺少字段或混入其他操作。")
    rule: dict[str, Any] = {"kind": kind, "timezone": _text(form["cron_timezone"], "时区")}
    if kind == "interval":
        minutes = _text(form.get("cron_every_minutes"), "间隔")
        if not minutes.isdecimal() or int(minutes) <= 0:
            raise CardActionError("间隔必须是正整数分钟。")
        rule["every_minutes"] = int(minutes)
        if "anchor" in meta:
            rule["anchor"] = meta["anchor"]
    else:
        rule["at"] = _picker_clock(_text(form.get("cron_at"), "执行时间"))
        if kind == "once":
            rule["at"] = _once_picker(form.get("cron_date"), rule["at"], rule["timezone"], meta.get("original_at"))
    if kind == "weekly":
        days = form.get("cron_weekdays")
        if not isinstance(days, list) or not days or any(day not in [str(i) for i in range(7)] for day in days):
            raise CardActionError("请选择要执行的星期。")
        rule["weekdays"] = [int(day) for day in days]
    if kind != "once":
        end_date = _optional_text(form.get("cron_end_date"), "截止日期")
        end_time = _optional_text(form.get("cron_end_time"), "截止时间")
        if bool(end_date) != bool(end_time):
            raise CardActionError("请同时填写截止日期和时间；如不限制截止，请同时留空。")
        rule["end_at"] = _once_picker(end_date, _picker_clock(end_time, label="截止时间"), rule["timezone"], meta.get("original_end_at"), label="截止日期") if end_date else None
    draft = {"name": _text(form[names[0]], "名称"), "instructions": _text(form["cron_instructions"], "执行指令"),
             "project": meta["project"], "schedule": rule, "session_settings": settings.to_dict()}
    if not _instructions_fit(draft["instructions"]) or len(draft["instructions"]) > _MAX_FORM_INSTRUCTIONS:
        raise CardActionError("执行指令超过卡片编辑容量，请通过 Admin 或自然语言维护；尚未保存。")
    if form["cron_chat_id"]:
        draft["chat_id"] = _text(form["cron_chat_id"], "目标会话")
    if "plan_id" in meta:
        draft.update(plan_id=meta["plan_id"], expected_revision=meta["expected_revision"])
    return ScheduleCardAction("save", draft, meta["request_id"], meta["navigation"])


def schedule_retry_card(*, app_id: str, chat_id: str, value: Any, form: Any,
                        notice: str, projects: Sequence[Project],
                        scope: FeishuScope | None = None) -> tuple[FeishuScope, OutboundCard]:
    """Restore only UI state; this scope never authorizes a management request.

    Each renewed transport nonce is independent of the original write identity.
    The next callback must fetch and validate the message's real scope again.
    """
    if isinstance(form, Mapping) and form and "cron_project" in form:
        meta = _plan_form_metadata(form)
        original_scope = _retry_scope(meta["scope"], app_id=app_id, chat_id=chat_id)
        if scope is not None and scope != original_scope:
            raise CardActionError("卡片位置已改变，请重新发送 /cron。")
        _, identity = _form_identity(original_scope, form)
        if identity[1] != "p":
            raise CardActionError("无法恢复缺少身份的定时任务表单。")
        project = meta["project"]
        settings_fields = {key: item for key, item in form.items() if isinstance(key, str) and key.startswith("cron_session_")}
        settings = decode_session_settings_form(settings_fields, prefix="cron_session")
        plan = {"id": meta["plan_id"], "revision": meta["expected_revision"], "project_alias": project} if "plan_id" in meta else None
        # The retained option is display data, not a Project registration or
        # availability decision. The service revalidates the exact submitted ID.
        options = list(projects)
        if project not in {item.alias for item in options}:
            options.append(Project(project, Path("."), False, 0))
        return original_scope, schedule_form_card(original_scope, projects=options,
            default_timezone="", plan=plan, initial_project=project, session_settings=settings,
            allow_context_mode="cron_session_context_mode" in form,
            navigation=meta["navigation"], _retry_form=form,
            _request_id=meta["request_id"], _notice_text=notice)
    if form:
        if scope is None:
            raise CardActionError("暂时无法确认卡片位置，请稍后重新发送 /cron。")
        original_scope = scope
        decoded = decode_schedule_action(scope=scope, value=value, form=form)
    else:
        if not isinstance(value, Mapping):
            raise CardActionError("无法恢复缺少身份的定时任务操作。")
        original_scope = _retry_scope(value.get("scope"), app_id=app_id, chat_id=chat_id)
        if scope is not None and scope != original_scope:
            raise CardActionError("卡片位置已改变，请重新发送 /cron。")
        decoded = decode_schedule_action(scope=original_scope, value=value)
    retry = _button(original_scope, "重试刚才的操作", decoded.action, decoded.payload, navigation=decoded.navigation, primary=True)
    retry["behaviors"][0]["value"].update(request_id=decoded.request_id)
    builder = _builder("定时任务", "上次操作尚未确认")
    builder.raw(_notice(notice))
    builder.raw(retry)
    builder.raw(_button(original_scope, "查看最新状态", "list", navigation=decoded.navigation))
    return original_scope, _card(builder)


def _retry_scope(value: Any, *, app_id: str, chat_id: str) -> FeishuScope:
    if not isinstance(value, str) or len(value) > 2048:
        raise CardActionError("定时任务卡片会话身份无效。")
    parts = value.split(":")
    try:
        if len(parts) != 6 or parts[:2] != ["scope", "v1"]:
            raise ValueError("invalid scope")
        app, kind, chat, topic = (unquote(part) for part in parts[2:])
        scope = FeishuScope(app, chat, ScopeKind(kind), topic or None)
        if scope.key != value or app != app_id or chat != chat_id:
            raise ValueError("scope mismatch")
        return scope
    except ValueError as error:
        raise CardActionError("定时任务卡片会话身份无效。") from error


def _restore_form_fields(elements: list[dict[str, Any]], submitted: Mapping[str, Any]) -> None:
    fields: dict[str, tuple[str, Any]] = {}
    for name, value in submitted.items():
        if not isinstance(name, str):
            raise CardActionError("表单字段格式无效，无法恢复。")
        base = "cron_name" if name.startswith(_FORM_PREFIX) else name.split("__", 1)[0]
        if base in fields or not (isinstance(value, str) or value is None or (isinstance(value, list) and all(isinstance(item, str) for item in value))):
            raise CardActionError("表单字段格式无效，无法恢复。")
        fields[base] = (name, value)
    restored = set()
    def visit(node: Any) -> None:
        if isinstance(node, list):
            for child in node:
                visit(child)
        elif isinstance(node, dict):
            tag, name = node.get("tag"), node.get("name")
            if tag in {"input", "select_static", "multi_select_static", "date_picker", "picker_time"} and isinstance(name, str):
                base = "cron_name" if name.startswith(_FORM_PREFIX) else name.split("__", 1)[0]
                original_name, value = fields.get(base, (name, ""))
                restored.add(base)
                node["name"] = name if base == "cron_name" else original_name
                if tag == "multi_select_static":
                    if value is None or value == "":
                        value = []
                    if not isinstance(value, list) or any(item not in {option["value"] for option in node["options"]} for item in value):
                        raise CardActionError("星期字段格式无效，无法恢复。")
                    node["selected_values"] = value
                else:
                    if value is None:
                        value = ""
                    if not isinstance(value, str):
                        raise CardActionError("表单字段格式无效，无法恢复。")
                    if tag == "select_static":
                        node.pop("initial_option", None)
                        if value:
                            node["initial_option"] = value
                            if value not in {option["value"] for option in node["options"]}:
                                raise CardActionError("选项字段格式无效，无法恢复。")
                    elif tag == "input":
                        node["default_value"] = value
                    else:
                        initial = "initial_date" if tag == "date_picker" else "initial_time"
                        node.pop(initial, None)
                        try:
                            if value and tag == "date_picker":
                                match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?: [+-]\d{4})?", value)
                                if not match:
                                    raise ValueError("invalid picker date")
                                datetime.fromisoformat(match[1])
                                node[initial] = match[1]
                            elif value:
                                node[initial] = _picker_clock(value)
                        except (ValueError, CardActionError) as error:
                            raise CardActionError("日期或时间字段格式无效，无法恢复，请重新打开卡片。") from error
                return
            for child in tuple(node.values()):
                visit(child)
    visit(elements)
    for base in fields.keys() - restored:
        name, value = fields[base]
        if base not in {"cron_session_effort", "cron_session_speed"} or not isinstance(value, str):
            raise CardActionError("表单包含无法恢复的字段。")
        elements.insert(-1, _input(name, "Effort" if base.endswith("effort") else "Speed", value))


def _picker_clock(value: str, *, label: str = "执行时间") -> str:
    match = re.fullmatch(r"((?:[01]\d|2[0-3]):[0-5]\d)(?: [+-]\d{4})?", value)
    if not match:
        raise CardActionError(f"请选择有效的{label}。")
    return match[1]


def _once_picker(value: Any, clock: str, timezone: str, original_at: Any, *, label: str = "执行日期") -> str:
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?: [+-]\d{4})?", _text(value, label))
    if not match:
        raise CardActionError(f"请选择有效的{label}。")
    try:
        return resolve_once_local(
            f"{match[1]}T{clock}", timezone,
            original_at=original_at if isinstance(original_at, str) else None,
        )
    except AmbiguousLocalTime as error:
        raise CardActionError("所选时间因夏令时回拨会出现两次，请换一个时间，或通过 Admin／自然语言明确指定偏移。") from error
    except ScheduleError as error:
        raise CardActionError(str(error)) from error


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CardActionError(label + "不能为空。")
    return value.strip()


def _optional_text(value: Any, label: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise CardActionError(f"{label}格式无效。")
    return value.strip()
