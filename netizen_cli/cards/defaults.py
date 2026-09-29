"""Current-chat session defaults form; state travels on the real card."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import uuid4

from lark_channel import OutboundCard

from ..domain import FeishuScope, ScopeKind
from ..model_settings import ModelCatalog
from ..projects import Project
from ..session_settings import SessionSettings
from .callbacks import CardActionError, _builder, _notice, _plain, _plain_text
from .controls import decode_session_settings_form, session_settings_form_elements


_VERSION = 1
_FORM_PREFIX = "defaults_project_v1__"
_SESSION_PREFIX = "defaults_session"
_STATE_FIELDS = {"v", "scope", "chat_kind", "id", "expected_revision"}
_PROJECT_ALIAS = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_RECORD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_NONCE = re.compile(r"[0-9a-f]{32}")


def _encoded(value: Mapping[str, Any]) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    ).decode().rstrip("=")


def _decoded(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise CardActionError("请选择默认会话使用的 Project。")
    try:
        result = json.loads(base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True,
        ))
    except (ValueError, UnicodeError) as error:
        raise CardActionError("默认会话配置选项无效，请重新发送 /defaults。") from error
    if not isinstance(result, dict):
        raise CardActionError("默认会话配置选项无效，请重新发送 /defaults。")
    return result


def _validate_state(scope: FeishuScope, state: Mapping[str, Any]) -> None:
    if type(state.get("v")) is not int or state["v"] != _VERSION:
        raise CardActionError("默认会话配置卡片已过期，请重新发送 /defaults。")
    if state.get("scope") != scope.key:
        raise CardActionError("默认会话配置卡片与原消息位置不一致。")
    if not isinstance(state.get("chat_kind"), str) or state["chat_kind"] not in {"group", "p2p"}:
        raise CardActionError("默认会话配置缺少聊天类型，请重新发送 /defaults。")
    if scope.kind is ScopeKind.DIRECT and state["chat_kind"] != "p2p":
        raise CardActionError("默认会话配置卡片与聊天类型不一致。")
    if scope.kind is ScopeKind.GROUP and state["chat_kind"] != "group":
        raise CardActionError("默认会话配置卡片与聊天类型不一致。")
    record_id, revision = state.get("id"), state.get("expected_revision")
    if record_id is None and revision is None:
        return
    if (not isinstance(record_id, str) or _RECORD_ID.fullmatch(record_id) is None
            or type(revision) is not int or revision < 1):
        raise CardActionError("默认会话配置版本无效，请重新发送 /defaults。")


def defaults_card(
    scope: FeishuScope,
    view: Mapping[str, Any],
    projects: Sequence[Project],
    catalog: ModelCatalog | None,
    notice: str | None = None,
    notice_is_error: bool = False,
) -> OutboundCard:
    """One editable form for the enclosing chat, including from topic scopes."""
    exact = view.get("exact")
    effective = exact if exact is not None else view.get("effective")
    state = {
        "v": _VERSION, "scope": scope.key, "chat_kind": view.get("chat_kind"),
        "id": exact["id"] if exact is not None else None,
        "expected_revision": exact["revision"] if exact is not None else None,
    }
    _validate_state(scope, state)
    if effective is None:
        settings = SessionSettings.new_defaults(catalog)
        selected_project = None
    else:
        raw_settings = effective["session_settings"]
        settings = raw_settings if isinstance(raw_settings, SessionSettings) else SessionSettings.from_dict(raw_settings)
        selected_project = effective["project"]

    builder = _builder("默认会话配置", "当前聊天")
    kind_label = "单聊" if state["chat_kind"] == "p2p" else "群聊"
    builder.raw(_plain(f"作用于当前{kind_label}：{scope.chat_id}（包含其中的话题）。"))
    if exact is not None:
        builder.raw(_plain("当前使用本聊天的精确配置；修改后保存，仅影响之后自动创建的会话。"))
    elif effective is not None:
        builder.raw(_plain(f"当前使用群名包含「{effective['keyword']}」的规则；保存后将成为本聊天的精确配置。"))
    else:
        builder.raw(_plain("尚未配置默认会话，请选择 Project 后保存。"))
    if view.get("match_error"):
        builder.raw(_notice(str(view["match_error"]), error=True))
    if notice:
        builder.raw(_notice(notice, error=notice_is_error))

    enabled_projects = [project for project in projects if project.enabled]
    project_options: list[dict[str, Any]] = []
    selected_value = None
    for project in enabled_projects:
        option = _encoded({**state, "project": project.alias, "project_revision": project.revision})
        project_options.append({"text": _plain_text(f"{project.alias} · {project.cwd}"), "value": option})
        if project.alias == selected_project:
            selected_value = option
    if selected_project is not None and selected_value is None:
        selected_value = _encoded({**state, "project": selected_project, "project_revision": None})
        project_options.append({"text": _plain_text(f"{selected_project} · 不可用（保留原配置）"), "value": selected_value})
        builder.raw(_notice(f"Project「{selected_project}」已停用或不可用；请选择可用项目后保存，或删除本聊天的默认配置。", error=True))
    if not enabled_projects:
        builder.raw(_notice("没有可用 Project，请先通过 /settings 登记或启用项目。"))
    if not project_options:
        # Feishu cannot render an empty static select. There is no saved exact
        # record to delete in this branch; opening again after /settings gives
        # the ordinary single form without inventing a default Project.
        return OutboundCard(card=builder.to_dict())

    # Each redraw gives form submissions a fresh SDK dedup identity. This is
    # transport state, not a persisted card session or a management revision.
    project_select: dict[str, Any] = {
        "tag": "select_static", "name": _FORM_PREFIX + uuid4().hex,
        "required": True, "placeholder": _plain_text("选择 Project"),
        "options": project_options,
    }
    if selected_value is not None:
        project_select["initial_option"] = selected_value
    builder.raw({"tag": "form", "name": "defaults_v1", "elements": [
        _plain("Project"), project_select,
        *session_settings_form_elements(
            prefix=_SESSION_PREFIX, settings=settings, catalog=catalog,
            allow_context_mode=state["chat_kind"] == "group",
        ),
        {"tag": "button", "name": "defaults_save", "text": _plain_text("保存默认配置"),
         "type": "primary_filled", "width": "fill", "form_action_type": "submit"},
    ]})
    if exact is not None:
        builder.raw({
            "tag": "button", "text": _plain_text("删除默认配置"), "type": "danger", "width": "fill",
            "behaviors": [{"type": "callback", "value": {
                "kind": "netizen_defaults", "action": "delete", "nonce": uuid4().hex, **state,
            }}],
        })
        builder.raw(_plain("删除后仍可能使用匹配的群名规则；已有会话不受影响。"))
    return OutboundCard(card=builder.to_dict())


def is_defaults_card_action(value: Any, form: Any = None) -> bool:
    return (isinstance(value, Mapping) and value.get("kind") == "netizen_defaults") or (
        isinstance(form, Mapping)
        and any(isinstance(name, str) and name.startswith(_FORM_PREFIX) for name in form)
    )


def _request(scope: FeishuScope, state: Mapping[str, Any], mode: str) -> dict[str, Any]:
    result = {
        "mode": mode, "kind": "chat", "chat_id": scope.chat_id,
        "expected_revision": state["expected_revision"],
    }
    if state["id"] is not None:
        result["id"] = state["id"]
    return result


def decode_defaults_action(scope: FeishuScope, value: Any, form: Any = None) -> dict[str, Any]:
    """Decode strict form values; the caller verifies the real card location."""
    if form is not None and form != {}:
        if value is not None and value != {}:
            raise CardActionError("默认会话配置表单混入其他操作。")
        if not isinstance(form, Mapping):
            raise CardActionError("默认会话配置表单无效。")
        names = [name for name in form if isinstance(name, str) and name.startswith(_FORM_PREFIX)]
        if len(names) != 1 or _NONCE.fullmatch(names[0][len(_FORM_PREFIX):]) is None:
            raise CardActionError("默认会话配置表单身份无效，请重新发送 /defaults。")
        state = _decoded(form[names[0]])
        if set(state) != _STATE_FIELDS | {"project", "project_revision"}:
            raise CardActionError("默认会话配置选项字段无效。")
        _validate_state(scope, state)
        project = state["project"]
        if not isinstance(project, str) or _PROJECT_ALIAS.fullmatch(project) is None:
            raise CardActionError("默认会话配置 Project 无效。")
        revision = state["project_revision"]
        if revision is not None and (type(revision) is not int or revision < 1):
            raise CardActionError("默认会话配置 Project 版本无效。")
        fields = {key: item for key, item in form.items() if key != names[0]}
        if state["chat_kind"] == "p2p" and f"{_SESSION_PREFIX}_context_mode" in fields:
            raise CardActionError("单聊不支持补充历史上下文。")
        if state["chat_kind"] == "group" and f"{_SESSION_PREFIX}_context_mode" not in fields:
            raise CardActionError("默认会话配置表单缺少上下文设置。")
        settings = decode_session_settings_form(fields, prefix=_SESSION_PREFIX)
        return {
            **_request(scope, state, "save"), "project": project,
            "expected_project_revision": revision, "session_settings": settings.to_dict(),
        }
    if not isinstance(value, Mapping) or set(value) != _STATE_FIELDS | {"kind", "action", "nonce"}:
        raise CardActionError("默认会话配置动作无效，请重新发送 /defaults。")
    if (value["kind"] != "netizen_defaults" or value["action"] != "delete"
            or not isinstance(value["nonce"], str) or _NONCE.fullmatch(value["nonce"]) is None):
        raise CardActionError("默认会话配置动作无效，请重新发送 /defaults。")
    _validate_state(scope, value)
    if value["id"] is None:
        raise CardActionError("当前聊天没有可删除的精确配置。")
    return _request(scope, value, "delete")
