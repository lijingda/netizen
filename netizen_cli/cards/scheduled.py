"""Self-contained scheduled-plan cards; no persisted card sessions."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from uuid import uuid4
from zoneinfo import ZoneInfo

from lark_channel import OutboundCard

from ..domain import FeishuScope, MentionContextMode, ScopeKind
from ..model_settings import ModelCatalog
from ..management.chat_directory import AvailableChat
from ..projects import Project
from ..session_settings import BindingTaskFeedback, BindingTurnSettings, SessionSettings
from ..schedules.models import AmbiguousLocalTime, ScheduleError, resolve_once_local
from .callbacks import CardActionError, _builder, _notice, _plain, _plain_text
from .chat_target import (
    ChatSearchSnapshot, ChatSearchView, ChatTargetDraft, chat_options, chat_target_elements,
    decode_chat_snapshot, encode_chat_snapshot, initial_chat_target, read_chat_target, resolve_chat_target,
    snapshot_capacity_selections, snapshot_chat_options,
)
from .pagination import decode_page_selection, pagination_controls
from .controls import (
    MAX_SETTING_ID_CHARS,
    _decode_context_mode_reference, _decode_new_model_reference, _decode_task_feedback_reference,
    decode_session_settings_form, session_settings_form_elements, session_settings_summary,
)
from .reply import TURN_FILE_CARD_JSON_LIMIT_BYTES


_VERSION = 4
_FORM_PREFIX = "cron_name_v7__"
_KINDS = {"once": "一次性", "daily": "每天", "weekly": "每周", "interval": "固定间隔"}
_FILTERS = {
    "current": "当前飞书聊天 · 全部任务", "current_enabled": "当前飞书聊天 · 已启用",
    "current_paused": "当前飞书聊天 · 已暂停", "all": "全部飞书聊天 · 全部任务",
    "all_enabled": "全部飞书聊天 · 已启用", "all_paused": "全部飞书聊天 · 已暂停",
}
_ACTIONS = {"list", "new", "view", "edit", "delete", "runs", "enabled", "run_now", "search_chats", "page_chats"}
_FIELDS = {
    "list": (set(), set()),
    "new": (set(), {"target_kind", "target_binding_id"}),
    "view": ({"plan_id"}, set()),
    "edit": ({"plan_id", "expected_revision"}, set()),
    "delete": ({"plan_id", "expected_revision"}, set()),
    "runs": ({"plan_id"}, {"cursor"}),
    "enabled": ({"plan_id", "expected_revision", "enabled"}, set()),
    "run_now": ({"plan_id", "expected_revision"}, set()),
    "search_chats": (set(), set()),
    "page_chats": ({"snapshot"}, set()),
}
_MAX_INSTRUCTIONS = 8000
_MAX_FORM_INSTRUCTIONS = 1000
_MAX_INSTRUCTION_JSON_BYTES = 12_000
SCHEDULE_CARD_JSON_LIMIT_BYTES = TURN_FILE_CARD_JSON_LIMIT_BYTES
_CARD_ELEMENT_LIMIT = 200
_STATES = {
    "enabled": "已启用", "paused": "已暂停", "ended": "计划已结束",
    "blocked_unknown": "执行状态待确认", "project_disabled": "Project 已停用",
    "project_unavailable": "Project 不可用", "completed": "已完成",
    "failed": "执行失败", "interrupted": "已停止", "inProgress": "运行中",
    "running": "运行中", "deleted": "Agent 会话已删除", "unavailable": "状态暂不可读",
    "claimed": "已触发", "publishing_topic": "正在创建话题", "binding_ready": "正在启动",
    "starting": "正在启动", "starting_turn": "正在启动", "handed_off": "已启动", "released": "本次已处理",
    "missed": "已错过", "skipped_busy": "上次仍在运行，本次跳过",
    "scope_conflict": "话题已有 Agent 会话，本次未启动", "publishing_unknown": "话题发布结果待确认",
    "publishing_failed": "话题发布失败", "dispatch_rejected": "本次未启动",
    "initial_start_rejected": "首次启动条件已改变", "initial_start_unknown": "启动结果待确认",
    "observation_unavailable": "执行状态暂不可读", "sent": "已投递", "unknown": "待确认",
    "target_inactive": "目标 Agent 会话已切换或归档，自动暂停", "target_missing": "目标 Agent 会话不可用",
    "target_mismatch": "目标 Agent 会话所在飞书聊天或话题不匹配", "input_started": "已启动新一轮",
    "input_steered": "已追加当前任务", "input_unknown": "输入接收情况待确认",
    "input_rejected": "输入被拒绝", "anchor_failed": "触发消息发送失败",
    "anchor_unknown": "触发消息发送结果待确认",
}


def _display_state(value: Any) -> str:
    return _STATES.get(str(value), "状态待确认")


def _trigger_source(value: Any) -> str:
    return "手动触发" if value == "manual" else "定时触发"


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
        raise CardActionError("Agent 会话配置不完整，请重新发送 /cron。") from error


def _card(builder: Any) -> OutboundCard:
    card = builder.to_dict()
    card["config"]["width_mode"] = "compact"
    card["header"]["icon"] = {"tag": "standard_icon", "token": "calendar_colorful"}
    card["body"].update(padding="12px", vertical_spacing="12px")
    # lark-channel-sdk 1.4.0 OutboundSender serializes cards this exact way,
    # without splitting them. Use the existing, verified Reply Card ceiling.
    if (_element_count(card) > _CARD_ELEMENT_LIMIT
            or len(json.dumps(card, ensure_ascii=False).encode("utf-8")) > SCHEDULE_CARD_JSON_LIMIT_BYTES):
        raise CardActionError("卡片内容较长，请通过 Admin 或自然语言管理这项计划；内容未被截断保存。")
    return OutboundCard(card=card)


def _element_count(value: Any) -> int:
    if isinstance(value, Mapping):
        return int("tag" in value) + sum(_element_count(item) for item in value.values())
    if isinstance(value, list):
        return sum(_element_count(item) for item in value)
    return 0


@dataclass(frozen=True, slots=True)
class ScheduleCardAction:
    action: str
    payload: dict[str, Any]
    request_id: str
    navigation: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ScheduleFormDraft:
    """One renderable form, separate from a validated plan write request."""

    scope: FeishuScope
    meta: dict[str, Any]
    fields: dict[str, Any]
    settings: SessionSettings
    target: ChatTargetDraft
    allow_context_mode: bool
    session_fields: dict[str, str] | None = None


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
        return _decode_form(scope, form, value=value)
    if not isinstance(value, Mapping) or set(value) != {"kind", "v", "scope", "action", "payload", "request_id", "navigation", "nonce"}:
        raise CardActionError("定时任务卡片动作无效，请重新发送 /cron。")
    if value["kind"] != "netizen_cron" or type(value["v"]) is not int or value["v"] != _VERSION:
        raise CardActionError("定时任务卡片已过期，请重新发送 /cron。")
    if value["scope"] != scope.key or value["action"] not in _ACTIONS:
        raise CardActionError("定时任务卡片与当前飞书聊天或话题不一致，请在原位置重新发送 /cron。")
    if not isinstance(value["payload"], dict) or not isinstance(value["request_id"], str) or not value["request_id"]:
        raise CardActionError("定时任务卡片缺少操作凭据。")
    if not isinstance(value["nonce"], str) or not re.fullmatch(r"[0-9a-f]{32}", value["nonce"]):
        raise CardActionError("定时任务卡片缺少交互凭据。")
    required, optional = _FIELDS[value["action"]]
    fields = set(value["payload"])
    if value["action"] == "search_chats" and "cursor" in fields:
        raise CardActionError("群聊分页卡片已更新，请重新查找群聊。")
    if not required <= fields or fields - required - optional:
        raise CardActionError("定时任务卡片动作字段不完整或含未知字段。")
    if value["action"] == "new" and fields and (
            fields != {"target_kind", "target_binding_id"}
            or value["payload"]["target_kind"] != "binding"
            or not isinstance(value["payload"]["target_binding_id"], str)
            or not value["payload"]["target_binding_id"].strip()):
        raise CardActionError("目标 Agent 会话无效，请重新打开定时任务卡片。")
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
                          notice: str | None = None, current_binding_id: str | None = None) -> OutboundCard:
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
        builder.raw(_plain("还没有符合条件的任务。可新建独立话题任务，或在当前 Agent 会话中定时继续。"))
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
    if current_binding_id:
        builder.raw(_button(scope, "在当前 Agent 会话中定时执行", "new",
            {"target_kind": "binding", "target_binding_id": current_binding_id}, navigation=state))
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
        f"Project：{plan.get('project_alias', '')}\n"
        f"目标飞书聊天：{plan.get('chat_id', '')}\n"
        + (f"执行目标：Agent 会话 · {plan.get('target_label') or plan.get('target_binding_id', '')}\n" if plan.get("target_kind") == "binding" else "执行目标：每次新建独立话题\n")
        + f"启停：{'已启用' if plan.get('enabled') else '已暂停'}\n"
        + f"时间：{rule_text} · {rule.get('timezone', '')}\n"
        f"状态：{status}" + (f" · {_display_state(blocked)}" if blocked else "")
        + deadline
        + f"\n下次：{plan.get('next_due_local') or '无'}"
        + (f"\n最近触发：{plan['latest_run']['due_local']} · {_trigger_source(plan['latest_run'].get('trigger_source'))}" if plan.get("latest_run") else "")
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
    if plan.get("target_kind") == "binding":
        builder.raw(_plain("Project、模型、上下文和反馈随目标 Agent 会话当前配置；系统触发不新增 @ 通知。"))
    else:
        builder.raw(_fold("Agent 会话配置", _plain(session_settings_summary(_session_settings(plan.get("session_settings", SessionSettings()))))))
    inflight = result.get("inflight") or plan.get("inflight")
    if inflight:
        builder.raw(_notice("本次已触发，修改、暂停或删除从后续触发生效；本次交接可以继续。"))
    exact = {"plan_id": plan["id"], "expected_revision": plan["revision"]}
    actions = []
    if plan.get("can_run_now") is True:
        builder.raw(_plain("立即运行会将保存的指令提交到目标 Agent 会话，空闲时启动新一轮，忙碌时追加当前任务；原定时安排保持不变。"
            if plan.get("target_kind") == "binding" else "立即运行会按当前保存的内容执行一次，并新建话题；原定时安排保持不变。"))
        actions.append(_button(scope, "立即运行", "run_now", exact, navigation=navigation, primary=True))
    elif plan.get("blocked_reason"):
        builder.raw(_plain("暂不能立即运行：" + _display_state(plan["blocked_reason"]) + "。请处理后刷新任务。"))
    elif inflight:
        builder.raw(_plain("本次输入正在交接，暂不能立即运行；可刷新任务查看最新状态。" if plan.get("target_kind") == "binding"
            else "上次首轮执行尚未确认结束，暂不能立即运行；可刷新任务查看最新状态。"))
    else:
        builder.raw(_plain("暂不能立即运行；请刷新任务查看最新状态。"))
    actions.extend((_button(scope, "编辑", "edit", exact, navigation=navigation),
        _button(scope, "暂停" if plan.get("enabled") else "启用", "enabled", {**exact, "enabled": not plan.get("enabled")}, navigation=navigation)))
    builder.raw(_row(*actions))
    delete = _button(scope, "删除计划", "delete", exact, navigation=navigation, danger=True)
    delete["confirm"] = {"title": _plain_text("删除定时任务？"),
        "text": _plain_text("删除后停止后续触发，保留已有 Agent 会话。已认领的本次交接仍可能继续。删除的计划不能恢复。")}
    builder.raw(_row(_button(scope, "最近执行", "runs", {"plan_id": plan["id"]}, navigation=navigation),
        _button(scope, "刷新任务", "view", {"plan_id": plan["id"]}, navigation=navigation), delete))


def _runs_panel(scope: FeishuScope, result: dict[str, Any], *, plan_id: str, navigation: dict[str, Any]) -> dict[str, Any]:
    items = []
    runs = result.get("runs", [])
    if not runs:
        items.append(_plain("还没有触发记录。"))
    for run in runs:
        delivery = ("反馈：随目标 Agent 会话当前任务交付" if run.get("target_kind") == "binding" else
            f"结果投递：{ {'sent': '已投递', 'failed': '投递失败', 'unknown': '待确认'}.get(run.get('delivery_state'), '尚未投递')}")
        items.append(_plain(
            f"时间：{run.get('due_local') or _utc_time(run.get('due_at'))}\n"
            f"来源：{_trigger_source(run.get('trigger_source'))}\n"
            f"状态：{_display_state(run.get('status') or run.get('error_code') or run.get('phase'))}\n"
            + delivery
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
                       target_binding_id: str | None = None,
                       initial_project: str | None = None, session_settings: SessionSettings | Mapping[str, Any] | None = None,
                       catalog: ModelCatalog | None = None, catalog_error: str | None = None,
                       allow_context_mode: bool = False, navigation: Any = None,
                       chats: Sequence[AvailableChat] = (), chat_avatar_keys: Mapping[str, str] | None = None,
                       chat_snapshot: ChatSearchSnapshot | None = None, chat_page: int = 0,
                       chat_directory_error: str | None = None,
                       target_chat_id: str | None = None, target_chat_label: str | None = None,
                       target_topic_id: str | None = None,
                       target_label: str | None = None) -> OutboundCard:
    navigation = schedule_navigation(navigation)
    builder = _builder("编辑定时任务" if plan else "新建定时任务", "填写内容与时间，保存后按计划执行")
    existing = dict(plan or {})
    target_binding_id = existing.get("target_binding_id") if plan else target_binding_id
    binding_target = existing.get("target_kind") == "binding" if plan else target_binding_id is not None
    settings = SessionSettings() if binding_target else _session_settings(existing.get("session_settings", session_settings if session_settings is not None else SessionSettings()))
    if not allow_context_mode and settings.message_context_mode is MentionContextMode.CATCH_UP:
        raise CardActionError("私聊不能自动读取群聊讨论，请先将消息范围设为仅当前消息。")
    if len(str(existing.get("instructions", ""))) > _MAX_FORM_INSTRUCTIONS:
        if plan:
            return schedule_manager_card(scope, {"plans": [plan]}, selected={"plan": plan}, navigation=navigation,
                notice="这项计划的完整指令超过卡片编辑容量，请通过 Admin 或自然语言修改。")
        raise CardActionError("执行指令超过卡片编辑容量，请通过 Admin 或自然语言维护。")
    if not projects and not binding_target:
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
    meta = {"request_id": uuid4().hex, "navigation": navigation, "scope": scope.key}
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
    fields = {
        "cron_name": str(existing.get("name", "")), "cron_instructions": str(existing.get("instructions", "")),
        "cron_kind": kind, "cron_timezone": timezone,
        "cron_date": once_at.strftime("%Y-%m-%d") if once_at else "",
        "cron_at": once_at.strftime("%H:%M") if once_at else rule.get("at") or "09:00",
        "cron_weekdays": [str(day) for day in rule.get("weekdays", (0, 1, 2, 3, 4))],
        "cron_every_minutes": str(rule.get("every_minutes") or 60),
        "cron_end_date": end_at.strftime("%Y-%m-%d") if end_at else "",
        "cron_end_time": end_at.strftime("%H:%M") if end_at else "",
        "cron_chat_query": chat_snapshot.query if chat_snapshot is not None else "",
    }
    if binding_target:
        raw_label = str(existing.get("target_label") or target_label or target_binding_id)
        target_chat_id = str(existing.get("chat_id") or target_chat_id or scope.chat_id)
        target_topic_id = target_topic_id or existing.get("target_topic_id")
        meta.update(target_kind="binding", target_binding_id=target_binding_id,
            target_chat_id=target_chat_id, target_label=raw_label,
            target_chat_label=target_chat_label or "", target_topic_id=target_topic_id or "")
    else:
        meta["project"] = existing.get("project_alias") or initial_project or projects[0].alias
    target = initial_chat_target(current_chat_id=scope.chat_id,
        target_chat_id=existing.get("chat_id"), options=chat_options(chats))
    draft = ScheduleFormDraft(scope, meta, fields, settings, target, allow_context_mode)
    return schedule_draft_card(draft, projects=projects, catalog=catalog, catalog_error=catalog_error,
        chats=chats, chat_avatar_keys=chat_avatar_keys,
        chat_snapshot=chat_snapshot, chat_page=chat_page, chat_directory_error=chat_directory_error)


def schedule_draft_card(draft: ScheduleFormDraft, *, projects: Sequence[Project],
                        catalog: ModelCatalog | None = None, catalog_error: str | None = None,
                        chats: Sequence[AvailableChat] = (), chat_avatar_keys: Mapping[str, str] | None = None,
                        chat_snapshot: ChatSearchSnapshot | None = None, chat_page: int = 0,
                        chat_directory_error: str | None = None,
                        notice: str | None = None) -> OutboundCard:
    """Render initial, edited, searched and failed forms from the same draft."""
    options = dict(projects=projects, catalog=catalog, catalog_error=catalog_error,
        chats=chats, chat_avatar_keys=chat_avatar_keys,
        chat_snapshot=chat_snapshot, chat_directory_error=chat_directory_error, notice=notice)
    if chat_snapshot is None:
        return _render_schedule_draft_card(draft, chat_page=0, **options)
    if type(chat_page) is not int or not 0 <= chat_page < chat_snapshot.total_pages:
        raise CardActionError("群聊页码无效，请重新查找群聊。")
    result = None
    try:
        # Full callback snapshots and page choices also consume card capacity.
        # Validate every page before showing any result, never a partial set.
        for page in range(chat_snapshot.total_pages):
            rendered = _render_schedule_draft_card(draft, chat_page=page, **options)
            for retained in snapshot_capacity_selections(chat_snapshot, page):
                if retained == draft.target.choice:
                    continue
                capacity_draft = replace(draft, target=replace(draft.target, choice=retained))
                _render_schedule_draft_card(capacity_draft, chat_page=page, **options)
            if page == chat_page:
                result = rendered
    except CardActionError as error:
        raise CardActionError("群聊查找结果超出卡片容量，请细化关键词或填写聊天 ID；结果和任务内容均未截断。") from error
    assert result is not None
    return result


def _render_schedule_draft_card(draft: ScheduleFormDraft, *, projects: Sequence[Project],
                               catalog: ModelCatalog | None, catalog_error: str | None,
                               chats: Sequence[AvailableChat], chat_avatar_keys: Mapping[str, str] | None,
                               chat_snapshot: ChatSearchSnapshot | None,
                               chat_page: int, chat_directory_error: str | None,
                               notice: str | None) -> OutboundCard:
    scope, meta, fields = draft.scope, draft.meta, draft.fields
    editing = "plan_id" in meta
    binding_target = meta.get("target_kind") == "binding"
    builder = _builder("编辑定时任务" if editing else "新建定时任务", "填写内容与时间，保存后按计划执行")
    instructions_name = "cron_instructions" + (f"__{meta['plan_id']}:{meta['expected_revision']}" if editing else "")
    timezone_name = "cron_timezone" + (f"__{meta['original_at']}" if "original_at" in meta else "")
    minutes_name = "cron_every_minutes" + (f"__{meta['anchor']}" if "anchor" in meta else "")
    end_time_name = "cron_end_time" + (f"__{meta['original_end_at']}" if "original_end_at" in meta else "")
    elements = [
        _input(_form_name(scope, "plan", uuid4().hex), "名称", fields["cron_name"]),
        _input(instructions_name, "执行内容", fields["cron_instructions"], multiline=True),
        _row(_select("cron_kind", "执行频率", list(_KINDS.items()), fields["cron_kind"]),
            _input(timezone_name, "时区", fields["cron_timezone"])),
        _row(_labeled("日期 · 仅一次性", _draft_picker("cron_date", "date_picker", fields.get("cron_date", ""))),
            _labeled("时间 · 一次性 / 每天 / 每周", _draft_picker("cron_at", "picker_time", fields.get("cron_at", "")))),
        _row(_labeled("星期 · 仅每周", {
            "tag": "multi_select_static", "name": "cron_weekdays", "required": False, "width": "fill",
            "placeholder": _plain_text("选择星期"),
            "options": [{"text": _plain_text("周" + "一二三四五六日"[day]), "value": str(day)} for day in range(7)],
            "selected_values": fields.get("cron_weekdays", []),
        }), _input(minutes_name, "分钟间隔 · 仅固定间隔", fields.get("cron_every_minutes", ""), required=False)),
        _plain("截止时间 · 仅每天 / 每周 / 固定间隔（可选）"),
        _row(_labeled("截止日期", _draft_picker("cron_end_date", "date_picker", fields.get("cron_end_date", ""))),
            _labeled("截止时间（含该时刻）", _draft_picker(end_time_name, "picker_time", fields.get("cron_end_time", "")))),
        _plain("截止日期与时间使用上方时区；同时留空可取消截止。"),
    ]
    project_meta = {key: value for key, value in meta.items()
        if key not in {"plan_id", "expected_revision", "anchor", "original_at", "original_end_at"}}
    if binding_target:
        target_value = _encoded(project_meta)
        target_chat_id = meta.get("target_chat_id") or scope.chat_id
        target_chat_label, target_topic_id = meta.get("target_chat_label"), meta.get("target_topic_id")
        elements.append(_select("cron_project", "目标 Agent 会话（只读，创建后固定）",
            [(target_value, "Agent 会话 · " + (meta.get("target_label") or meta["target_binding_id"]))], target_value))
        elements.append(_plain("所属飞书聊天：" + (target_chat_label + " · " if target_chat_label else "") + target_chat_id
            + (f"\n所属飞书话题：{target_topic_id}" if target_topic_id else "")))
        elements.append(_plain("沿用目标 Agent 会话的 Project、模型、上下文和反馈。到点提交一条输入，空闲时启动新一轮，忙碌时追加当前任务。切换或归档后自动暂停，恢复后继续；删除目标 Agent 会话也会删除计划。"))
    else:
        elements.extend(_new_topic_form_fields(draft, projects=projects, project_meta=project_meta,
            catalog=catalog, catalog_error=catalog_error))
        elements.extend(_chat_target_form_fields(draft, chats=chats, chat_avatar_keys=chat_avatar_keys,
            chat_snapshot=chat_snapshot, chat_page=chat_page,
            chat_directory_error=chat_directory_error))
    elements.append(_plain("只使用所选频率对应的日期、时间、星期或间隔；日期与时间以所填时区为准。"))
    elements.append({"tag": "button", "name": "cron_save", "text": _plain_text("保存修改" if editing else "创建任务"),
        "type": "primary_filled", "width": "fill", "form_action_type": "submit"})
    if not binding_target:
        # Directory search submits the same form before task details are filled.
        # Final-save validation remains in the business decoder.
        _relax_required_fields(elements)
    if notice:
        builder.raw(_notice(notice))
    builder.raw({"tag": "form", "name": "cron_plan", "elements": elements})
    builder.raw(_button(scope, "取消", "view" if editing else "list",
        {"plan_id": meta["plan_id"]} if editing else {}, navigation=meta["navigation"]))
    return _card(builder)


def _draft_picker(name: str, tag: str, value: str) -> dict[str, Any]:
    date = tag == "date_picker"
    result = {"tag": tag, "name": name, "required": False, "width": "fill",
        "placeholder": _plain_text("选择日期" if date else "选择时间")}
    if value:
        result["initial_date" if date else "initial_time"] = value
    return result


def _new_topic_form_fields(draft: ScheduleFormDraft, *, projects: Sequence[Project],
                           project_meta: dict[str, Any], catalog: ModelCatalog | None,
                           catalog_error: str | None) -> list[dict[str, Any]]:
    elements: list[dict[str, Any]] = [_plain("执行目标：每次新建独立话题。")]
    selected_project = draft.meta["project"]
    project_choices = list(projects)
    if selected_project is not None and selected_project not in {project.alias for project in projects if project.enabled}:
        elements.append(_notice(f"Project「{selected_project}」已停用或不可用，请选择可用的 Project 后保存；仍可先查找群聊，不会更改计划。"))
        if selected_project not in {project.alias for project in projects}:
            # Preserve only the display identity needed for read-only search
            # and retries. Saving still resolves the exact live Project.
            project_choices.append(Project(selected_project, Path("."), False, 0))
    project_options = [(_encoded({**project_meta, "project": p.alias}), p.alias if p.enabled else p.alias + " · 已停用或不可用") for p in project_choices]
    selected_project_value = _encoded({**project_meta, "project": selected_project}) if selected_project is not None else None
    elements.append(_select("cron_project", "执行 Project", project_options, selected_project_value))
    incomplete_model = draft.session_fields is not None and any(
        not draft.session_fields.get("cron_session_" + field, "") for field in ("effort", "speed")
    ) and _decode_new_model_reference(draft.session_fields["cron_session_model"]) is not None
    elements.append(_fold("Agent 会话配置",
        _plain("配置尚未填写完整；查找群聊不会保存任务。" if incomplete_model else
            session_settings_summary(draft.settings, allow_context_mode=draft.allow_context_mode)),
        *_draft_session_fields(draft, catalog=catalog, catalog_error=catalog_error)))
    return elements


def _draft_session_fields(draft: ScheduleFormDraft, *, catalog: ModelCatalog | None,
                          catalog_error: str | None) -> list[dict[str, Any]]:
    # Catalog choices and unsaved selections are separate inputs. In particular,
    # selecting inherit must not discard the user's pending Effort/Speed values.
    settings = replace(draft.settings, turn_settings=None) if draft.session_fields is not None and catalog else draft.settings
    elements = session_settings_form_elements(prefix="cron_session", settings=settings, catalog=catalog,
        catalog_error=catalog_error, allow_context_mode=draft.allow_context_mode)
    remaining = dict(draft.session_fields or {})
    for control in elements:
        if control.get("tag") != "select_static" or control["name"] not in remaining:
            continue
        value = remaining.pop(control["name"])
        control.pop("initial_option", None)
        if value:
            if value not in {option["value"] for option in control["options"]}:
                control["options"].append({"text": _plain_text("保留所选值"), "value": value})
            control["initial_option"] = value
    for name, value in remaining.items():
        # An unavailable catalog has no inherited Effort/Speed controls. Keep
        # their pending choices as selects, not a second text-input fallback.
        elements.append(_select(name, "Effort" if name.endswith("effort") else "Speed",
            [(value, value)] if value else [], value or None))
    return elements


def _chat_target_form_fields(draft: ScheduleFormDraft, *,
                             chats: Sequence[AvailableChat], chat_avatar_keys: Mapping[str, str] | None,
                             chat_snapshot: ChatSearchSnapshot | None,
                             chat_page: int,
                             chat_directory_error: str | None) -> list[dict[str, Any]]:
    scope, target = draft.scope, draft.target
    navigation = draft.meta["navigation"]
    groups = {chat.chat_id: chat for chat in chats}
    if target.choice and target.choice not in groups:
        # Retained selection is only a draft, never an availability assertion.
        groups[target.choice] = AvailableChat(target.choice, "已选群聊 · " + target.choice, None, None)
    if chat_snapshot is None:
        options = chat_options(tuple(groups.values()), avatar_keys=chat_avatar_keys)
    else:
        options = snapshot_chat_options(chat_snapshot, chat_page, target.choice)
        if target.choice and all(option["value"] != target.choice for option in options):
            options.extend(chat_options((groups[target.choice],), avatar_keys=chat_avatar_keys))
    search = _button(scope, "查找群聊", "search_chats", navigation=navigation)
    search.update(name="cron_search_chats", form_action_type="submit")
    paging = None
    if chat_snapshot is not None:
        jump = _button(scope, "跳转", "page_chats", {"snapshot": encode_chat_snapshot(chat_snapshot)}, navigation=navigation)
        jump.update(name="cron_page_chats", form_action_type="submit")
        paging = pagination_controls(page_field="cron_chat_page", page=chat_page,
            total_pages=chat_snapshot.total_pages, button=jump)
    target_fields = chat_target_elements(mode_field="cron_target_mode", choice_field="cron_group_id",
        id_field="cron_chat_id", draft=target, options=options,
        search=ChatSearchView("cron_chat_query", draft.fields.get("cron_chat_query", ""),
            chat_snapshot.query if chat_snapshot is not None else None, search, paging,
            result_count=len(chat_snapshot.chats) if chat_snapshot is not None else None,
            page=chat_page, total_pages=chat_snapshot.total_pages if chat_snapshot is not None else 1))
    target_hint = "目标飞书聊天 · 每次触发在指定聊天中新建话题"
    directory_notice = chat_directory_error or (chat_snapshot.notice if chat_snapshot is not None else None)
    if directory_notice:
        # Directory feedback belongs beside the target, not the task settings.
        target_hint += "\n" + directory_notice
    return [_plain(target_hint), *target_fields]


def _relax_required_fields(node: Any) -> None:
    if isinstance(node, list):
        for child in node:
            _relax_required_fields(child)
    elif isinstance(node, dict):
        if "required" in node:
            node["required"] = False
        for child in node.values():
            _relax_required_fields(child)


def _chat_query(value: Any) -> str:
    if not isinstance(value, str) or len(value.strip()) > 50 or any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value):
        raise CardActionError("群名关键词最多 50 个字符，不能包含控制字符。")
    return value.strip()


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
        raise CardActionError("定时任务表单与原消息所在飞书聊天或话题不一致，请在原位置重新发送 /cron。")
    return names[0], identity


def _plan_form_metadata(form: Mapping[str, Any]) -> dict[str, Any]:
    """Parse write identity only, without validating editable business values."""
    meta = _decoded(_text(form.get("cron_project"), "Project"))
    binding_target = meta.get("target_kind") == "binding"
    target_fields = {"target_kind", "target_binding_id"} if binding_target else {"project"}
    required = target_fields | {"navigation", "scope", "request_id"}
    display_fields = {"target_chat_id", "target_chat_label", "target_topic_id", "target_label"} if binding_target else set()
    if not required <= set(meta) or set(meta) - required - display_fields:
        raise CardActionError("定时任务 Project 选项无效，请重新选择。")
    if any(not isinstance(meta[key], str) or len(meta[key]) > 512 for key in display_fields & set(meta)):
        raise CardActionError("目标 Agent 会话的显示信息无效，请重新打开表单。")
    if not isinstance(meta["request_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", meta["request_id"]):
        raise CardActionError("定时任务表单缺少操作凭据。")
    if binding_target:
        meta["target_binding_id"] = _text(meta["target_binding_id"], "目标 Agent 会话")
    else:
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


def read_schedule_form(scope: FeishuScope, form: Mapping[str, Any]) -> ScheduleFormDraft:
    """Validate native form shape, not whether incomplete business data can save."""
    name, identity = _form_identity(scope, form)
    if identity[1] != "p":
        raise CardActionError("无法恢复缺少身份的定时任务表单。")
    meta = _plan_form_metadata(form)
    if meta["scope"] != scope.key:
        raise CardActionError("定时任务 Project 选项与当前飞书聊天或话题不一致。")
    binding_target = meta.get("target_kind") == "binding"
    target = ChatTargetDraft() if binding_target else read_chat_target(form,
        mode_field="cron_target_mode", choice_field="cron_group_id", id_field="cron_chat_id")
    fields: dict[str, Any] = {}
    session_fields: dict[str, str] = {}
    evidence = {"cron_timezone": "original_at", "cron_every_minutes": "anchor", "cron_end_time": "original_end_at"}
    allowed = {"cron_name", "cron_instructions", "cron_project", "cron_timezone", "cron_kind",
        "cron_every_minutes", "cron_at", "cron_date", "cron_weekdays", "cron_end_date", "cron_end_time"}
    if not binding_target:
        allowed.update({"cron_target_mode", "cron_group_id", "cron_chat_id", "cron_chat_query", "cron_chat_page"})
    for key, value in form.items():
        if not isinstance(key, str):
            raise CardActionError("表单字段格式无效，无法恢复。")
        base, decorated, reference = key.partition("__")
        if key == name:
            base = "cron_name"
        elif key.startswith("cron_session_"):
            value = "" if value is None else value
            if binding_target or not isinstance(value, str):
                raise CardActionError("Agent 会话配置字段格式无效。")
            session_fields[key] = value
            continue
        elif decorated:
            if not reference or base not in {*evidence, "cron_instructions"}:
                raise CardActionError("表单包含未知字段。")
            if base in evidence:
                meta[evidence[base]] = reference
        if base in fields or base not in allowed:
            raise CardActionError("定时任务表单缺少字段或混入其他操作。")
        if base in {"cron_group_id", "cron_chat_id", "cron_target_mode"}:
            # Shared target parsing has already discarded malformed inactive
            # controls; neither their value nor shape may override the mode.
            continue
        if base == "cron_weekdays":
            value = [] if value is None or value == "" else value
            if not isinstance(value, list) or any(day not in [str(i) for i in range(7)] for day in value):
                raise CardActionError("星期字段格式无效，无法恢复。")
        else:
            value = "" if value is None else value
            if not isinstance(value, str):
                raise CardActionError("表单字段格式无效，无法恢复。")
            if value and base in {"cron_date", "cron_end_date"}:
                match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?: [+-]\d{4})?", value)
                try:
                    if match is None:
                        raise ValueError("invalid date")
                    datetime.fromisoformat(match[1])
                except ValueError as error:
                    raise CardActionError("日期字段格式无效，请重新打开卡片。") from error
                value = match[1]
            elif value and base in {"cron_at", "cron_end_time"}:
                value = _picker_clock(value)
        fields[base] = value
    if not {"cron_name", "cron_instructions", "cron_project", "cron_timezone", "cron_kind"} <= fields.keys():
        raise CardActionError("定时任务表单缺少字段。")
    if fields["cron_kind"] not in _KINDS:
        raise CardActionError("未知时间规则。")
    if len(fields["cron_instructions"]) > _MAX_FORM_INSTRUCTIONS or not _instructions_fit(fields["cron_instructions"]):
        raise CardActionError("执行指令超过卡片编辑容量，请通过 Admin 或自然语言维护；尚未保存。")
    if target.choice and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,191}", target.choice) is None:
        if target.mode == "group":
            raise CardActionError("飞书聊天选项无效，请重新选择。")
        target = replace(target, choice="")
    settings = SessionSettings() if binding_target else _read_draft_settings(session_fields)
    return ScheduleFormDraft(scope, meta, fields, settings, target,
        "cron_session_context_mode" in session_fields, None if binding_target else session_fields)


def _read_draft_settings(fields: dict[str, str]) -> SessionSettings:
    core = {"cron_session_" + key for key in ("model", "task_reactions", "progress_card", "completion_mention")}
    context = {"cron_session_context_mode"} if "cron_session_context_mode" in fields else set()
    tuning = {"cron_session_effort", "cron_session_speed"}
    if not core <= fields.keys() or fields.keys() - core - context - tuning:
        raise CardActionError("会话配置表单字段不完整或包含未知字段。")
    model = _decode_new_model_reference(fields["cron_session_model"])
    feedback = BindingTaskFeedback(*(_decode_task_feedback_reference(fields["cron_session_" + field], field)
        for field in ("task_reactions", "progress_card", "completion_mention")))
    mode = _decode_context_mode_reference(fields["cron_session_context_mode"]) if context else MentionContextMode.CURRENT_ONLY
    turn = None
    if model is not None or tuning & fields.keys():
        # Optional native selects may omit an unselected value altogether.
        # Keep it blank in the draft; final save still requires both values.
        for key in tuning:
            fields.setdefault(key, "")
    if tuning <= fields.keys():
        effort, speed = fields["cron_session_effort"], fields["cron_session_speed"]
        if any(len(value) > MAX_SETTING_ID_CHARS or any(ord(char) < 32 for char in value) for value in (effort, speed)):
            raise CardActionError("Effort / Speed 字段格式无效。")
        if model is not None and effort and speed:
            turn = BindingTurnSettings(model, effort, speed)
    return SessionSettings(turn, feedback, mode)


def _decode_form(scope: FeishuScope, form: Any, *, value: Any = None) -> ScheduleCardAction:
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
    if isinstance(value, Mapping) and value.get("action") in {"search_chats", "page_chats"}:
        action = decode_schedule_action(scope=scope, value=value)
        draft = read_schedule_form(scope, form)
        meta = draft.meta
        if meta.get("target_kind") == "binding":
            raise CardActionError("固定 Agent 会话任务不能重新选择飞书聊天。")
        if action.action == "search_chats":
            query = _chat_query(draft.fields.get("cron_chat_query", ""))
            draft = replace(draft, target=replace(draft.target, choice=""))
            payload = {"query": query, "draft": draft}
        else:
            snapshot = decode_chat_snapshot(action.payload["snapshot"])
            payload = {"snapshot": snapshot, "page": decode_page_selection(form.get("cron_chat_page"), snapshot.total_pages), "draft": draft}
        return ScheduleCardAction(action.action, payload, meta["request_id"], meta["navigation"])
    meta = _plan_form_metadata(form)
    if meta["scope"] != scope.key:
        raise CardActionError("定时任务 Project 选项与当前飞书聊天或话题不一致。")
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
    binding_target = meta.get("target_kind") == "binding"
    if binding_target and settings_fields:
        raise CardActionError("固定 Agent 会话任务沿用目标配置，请重新打开表单。")
    settings = None if binding_target else decode_session_settings_form(settings_fields, prefix="cron_session")
    expected = {names[0], "cron_instructions", "cron_project", "cron_timezone", "cron_kind"}
    if not binding_target:
        expected.add("cron_target_mode")
    optional = {"cron_every_minutes", "cron_at", "cron_date", "cron_weekdays", "cron_end_date", "cron_end_time"}
    if not binding_target:
        optional.update({"cron_group_id", "cron_chat_id", "cron_chat_query", "cron_chat_page"})
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
    draft = {"name": _text(form[names[0]], "名称"), "instructions": _text(form["cron_instructions"], "执行指令"), "schedule": rule}
    if binding_target:
        draft.update(target_kind="binding", target_binding_id=meta["target_binding_id"])
    else:
        assert settings is not None
        draft.update(project=meta["project"], session_settings=settings.to_dict())
    if not _instructions_fit(draft["instructions"]) or len(draft["instructions"]) > _MAX_FORM_INSTRUCTIONS:
        raise CardActionError("执行指令超过卡片编辑容量，请通过 Admin 或自然语言维护；尚未保存。")
    if not binding_target:
        target = read_chat_target(form, mode_field="cron_target_mode",
            choice_field="cron_group_id", id_field="cron_chat_id")
        draft["chat_id"] = resolve_chat_target(target, current_chat_id=scope.chat_id)
    if "plan_id" in meta:
        draft.update(plan_id=meta["plan_id"], expected_revision=meta["expected_revision"])
    return ScheduleCardAction("save", draft, meta["request_id"], meta["navigation"])


def schedule_retry_card(*, app_id: str, chat_id: str, value: Any, form: Any,
                        notice: str, projects: Sequence[Project],
                        scope: FeishuScope | None = None,
                        catalog: ModelCatalog | None = None, catalog_error: str | None = None,
                        chats: Sequence[AvailableChat] = (), chat_avatar_keys: Mapping[str, str] | None = None,
                        chat_snapshot: ChatSearchSnapshot | None = None, chat_page: int = 0,
                        chat_directory_error: str | None = None) -> tuple[FeishuScope, OutboundCard]:
    """Restore only UI state; this scope never authorizes a management request.

    Each renewed transport nonce is independent of the original write identity.
    The next callback must fetch and validate the message's real scope again.
    """
    if isinstance(form, Mapping) and form and "cron_project" in form:
        original_scope = _retry_scope(_decoded(form["cron_project"]).get("scope"), app_id=app_id, chat_id=chat_id)
        if scope is not None and scope != original_scope:
            raise CardActionError("卡片位置已改变，请重新发送 /cron。")
        draft = read_schedule_form(original_scope, form)
        return original_scope, schedule_draft_card(draft, projects=projects,
            catalog=catalog, catalog_error=catalog_error,
            chats=chats, chat_avatar_keys=chat_avatar_keys, chat_snapshot=chat_snapshot, chat_page=chat_page,
            chat_directory_error=chat_directory_error, notice=notice)
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
        raise CardActionError("定时任务卡片的飞书聊天或话题身份无效。")
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
        raise CardActionError("定时任务卡片的飞书聊天或话题身份无效。") from error


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
