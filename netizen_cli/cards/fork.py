"""Ordinary persistent fork cards; navigation travels on the real card."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Literal

from lark_channel import OutboundCard

from ..domain import FeishuScope
from .callbacks import (
    CardActionError,
    _builder,
    _new_callback_nonce,
    _notice,
    _plain,
    _plain_text,
    _valid_callback_nonce,
)
from .controls import MAX_THREAD_NAME_CHARS

if TYPE_CHECKING:
    from ..management.chat_directory import AvailableChat, AvailableChatPage


_VERSION = 1
_FORM_PREFIX = "fork_choice_v1__"
_NAME_FIELD = "fork_name_v1"
_QUERY_FIELD = "fork_query_v1"
_SOURCE_FIELDS = {
    "binding_id", "native_thread_id", "settings_revision", "context_revision",
    "feedback_revision", "project_revision",
}
_STATE_FIELDS = {"kind", "v", "scope", "source", "action"}
_ACTION_FIELDS = {
    "destination": set(), "search": set(), "query": set(),
    "results": {"query", "page_token"},
    "select": {"target_chat_id"}, "create": {"target_chat_id"},
}
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,191}")
_BINDING_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,127}")


@dataclass(frozen=True, slots=True)
class ForkSource:
    """Exact source preconditions, checked against live state by the caller."""

    binding_id: str
    native_thread_id: str
    settings_revision: int
    context_revision: int
    feedback_revision: int
    project_revision: int


@dataclass(frozen=True, slots=True)
class ForkCardAction:
    action: str
    source: ForkSource
    target_chat_id: str | None = None
    name: str | None = None
    query: str | None = None
    page_token: str | None = None


def _encoded(state: Mapping[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(
        state, ensure_ascii=False, separators=(",", ":"),
    ).encode()).decode().rstrip("=")


def _decoded(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise CardActionError("分支选项无效，请重新发送 /fork。")
    try:
        state = json.loads(base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True,
        ))
    except (ValueError, UnicodeError) as error:
        raise CardActionError("分支选项无效，请重新发送 /fork。") from error
    if not isinstance(state, dict):
        raise CardActionError("分支选项无效，请重新发送 /fork。")
    return state


def _source(value: Any) -> ForkSource:
    if not isinstance(value, Mapping) or set(value) != _SOURCE_FIELDS:
        raise CardActionError("分支来源字段不完整或包含未知字段。")
    for field, pattern in (("binding_id", _BINDING_ID), ("native_thread_id", _ID)):
        if not isinstance(value[field], str) or pattern.fullmatch(value[field]) is None:
            raise CardActionError("分支来源会话身份无效。")
    if any(type(value[field]) is not int or not 1 <= value[field] <= 2**63 - 1
           for field in _SOURCE_FIELDS - {"binding_id", "native_thread_id"}):
        raise CardActionError("分支来源版本无效，请重新发送 /fork。")
    return ForkSource(**value)


def _state(scope: FeishuScope, source: ForkSource, action: str, **extra: Any) -> dict[str, Any]:
    state = {
        "kind": "netizen_fork", "v": _VERSION, "scope": scope.key,
        "source": asdict(source), "action": action, **extra,
    }
    _validate_state(scope, state)
    return state


def _validate_state(scope: FeishuScope, state: Mapping[str, Any]) -> ForkCardAction:
    action = state.get("action")
    if not isinstance(action, str) or action not in _ACTION_FIELDS:
        raise CardActionError("未知分支卡片动作。")
    if set(state) != _STATE_FIELDS | _ACTION_FIELDS[action]:
        raise CardActionError("分支卡片动作字段不完整或包含未知字段。")
    if state["kind"] != "netizen_fork" or type(state["v"]) is not int or state["v"] != _VERSION:
        raise CardActionError("分支卡片已过期，请重新发送 /fork。")
    if state["scope"] != scope.key:
        raise CardActionError("分支卡片与来源会话位置不一致。")
    source = _source(state["source"])
    target = state.get("target_chat_id")
    if action in {"select", "create"} and (
        not isinstance(target, str) or _ID.fullmatch(target) is None
    ):
        raise CardActionError("分支目标聊天无效，请重新选择。")
    query, token = state.get("query"), state.get("page_token")
    if action == "results":
        if _query(query) != query:
            raise CardActionError("群名关键词不能包含首尾空白。")
        if token is not None and (
            not isinstance(token, str) or not token.strip() or len(token) > 1024
            or any(ord(char) < 32 for char in token)
        ):
            raise CardActionError("群聊分页游标无效，请重新搜索。")
    return ForkCardAction(action, source, target_chat_id=target, query=query, page_token=token)


def _query(value: Any) -> str:
    if not isinstance(value, str):
        raise CardActionError("请输入群名关键词。")
    query = value.strip()
    if not query or len(query) > 50 or any(ord(char) < 32 for char in query):
        raise CardActionError("群名关键词需要 1 至 50 个字符。")
    return query


def is_fork_card_action(value: Any, form: Any = None) -> bool:
    return (isinstance(value, Mapping) and value.get("kind") == "netizen_fork") or (
        isinstance(form, Mapping)
        and any(isinstance(name, str) and name.startswith(_FORM_PREFIX) for name in form)
    )


def decode_fork_action(scope: FeishuScope, value: Any, form: Any = None) -> ForkCardAction:
    """Decode public form_value only; callers verify the fetched card Scope."""
    if form is not None and form != {}:
        if value is not None and value != {}:
            raise CardActionError("分支表单混入其他操作。")
        if not isinstance(form, Mapping):
            raise CardActionError("分支表单无效。")
        fields = [name for name in form if isinstance(name, str) and name.startswith(_FORM_PREFIX)]
        if len(fields) != 1:
            raise CardActionError("分支表单身份无效，请重新发送 /fork。")
        suffix = fields[0][len(_FORM_PREFIX):]
        if suffix != "create" and not _valid_callback_nonce(suffix):
            raise CardActionError("分支表单身份无效，请重新发送 /fork。")
        action = _validate_state(scope, _decoded(form[fields[0]]))
        expected = {fields[0]}
        if suffix == "create":
            if action.action != "create":
                raise CardActionError("分支创建表单与动作不一致。")
            expected.add(_NAME_FIELD)
        elif action.action == "create":
            raise CardActionError("分支创建表单身份无效。")
        if action.action == "query":
            expected.add(_QUERY_FIELD)
        if set(form) != expected:
            raise CardActionError("分支表单缺少字段或混入其他操作。")
        if action.action == "query":
            return ForkCardAction("results", action.source, query=_query(form[_QUERY_FIELD]))
        if action.action == "create":
            raw_name = form[_NAME_FIELD]
            if not isinstance(raw_name, str) or "\x00" in raw_name:
                raise CardActionError("会话名称无效。")
            name = " ".join(raw_name.split())
            if not name or len(name) > MAX_THREAD_NAME_CHARS:
                raise CardActionError(f"会话名称需要 1 至 {MAX_THREAD_NAME_CHARS} 个字符。")
            return ForkCardAction("create", action.source, action.target_chat_id, name)
        return action
    if not isinstance(value, Mapping) or not _valid_callback_nonce(value.get("nonce")):
        raise CardActionError("分支卡片动作无效，请重新发送 /fork。")
    state = {key: item for key, item in value.items() if key != "nonce"}
    action = _validate_state(scope, state)
    if action.action not in {"destination", "search", "results"}:
        raise CardActionError("请选择目标并通过分支表单提交。")
    return action


def _button(scope: FeishuScope, source: ForkSource, label: str, action: str, **extra: Any) -> dict[str, Any]:
    return {
        "tag": "button", "text": _plain_text(label), "type": "default",
        "behaviors": [{"type": "callback", "value": {
            **_state(scope, source, action, **extra), "nonce": _new_callback_nonce(),
        }}],
    }


def _form(*, options: list[tuple[str, str]], label: str, submit: str,
          inputs: tuple[dict[str, Any], ...] = (), selected: str | None = None,
          create: bool = False) -> dict[str, Any]:
    # Navigation redraws use the shared transport nonce convention. Creation
    # stays stable like other one-shot controls, without a separate claim key.
    select = {
        "tag": "select_static", "name": _FORM_PREFIX + ("create" if create else _new_callback_nonce()),
        "required": True, "width": "fill", "placeholder": _plain_text(label),
        "options": [{"text": _plain_text(text), "value": value} for value, text in options],
    }
    if selected is not None:
        select["initial_option"] = selected
    return {"tag": "form", "name": "fork_form_v1", "elements": [
        _plain(label), select, *inputs,
        {"tag": "button", "name": "fork_submit_v1", "text": _plain_text(submit),
         "type": "primary_filled", "width": "fill", "form_action_type": "submit"},
    ]}


def _header(source_title: str, project_alias: str):
    builder = _builder("创建会话分支", project_alias)
    builder.raw(_plain(f"来源：{source_title}"))
    builder.raw(_plain("新会话继承当前原生上下文，在一个新话题中独立继续。两个会话共享项目文件，修改会相互可见。"))
    return builder


def fork_destination_card(scope: FeishuScope, source: ForkSource, *, source_title: str,
                          project_alias: str) -> OutboundCard:
    builder = _header(source_title, project_alias)
    current = _encoded(_state(scope, source, "select", target_chat_id=scope.chat_id))
    builder.raw(_form(options=[(current, "当前聊天的新话题"),
        (_encoded(_state(scope, source, "search")), "选择其他群")],
        selected=current, label="创建位置", submit="继续"))
    return OutboundCard(card=builder.to_dict())


def fork_chat_search_card(scope: FeishuScope, source: ForkSource, *, source_title: str,
                          project_alias: str, notice: str | None = None) -> OutboundCard:
    builder = _header(source_title, project_alias)
    builder.raw(_plain("按群名查找机器人已加入的群；结果不按操作者的群成员身份筛选，当前卡片的参与者均可见。"))
    if notice:
        builder.raw(_notice(notice))
    reference = _encoded(_state(scope, source, "query"))
    builder.raw(_form(options=[(reference, source_title)], selected=reference,
        label="来源会话", submit="搜索群聊", inputs=({
            "tag": "input", "name": _QUERY_FIELD, "required": True, "max_length": 50,
            "label": _plain_text("群名关键词"), "placeholder": _plain_text("输入群名关键词"),
        },)))
    builder.raw(_button(scope, source, "返回创建位置", "destination"))
    return OutboundCard(card=builder.to_dict())


def fork_chat_results_card(scope: FeishuScope, source: ForkSource, page: AvailableChatPage, *,
                           query: str, source_title: str, project_alias: str) -> OutboundCard:
    query = _query(query)
    builder = _header(source_title, project_alias)
    builder.raw(_plain(f"群名包含「{query}」的机器人可用群。"))
    if page.notice:
        builder.raw(_notice(page.notice))
    if page.items:
        builder.raw(_form(options=[(
            _encoded(_state(scope, source, "select", target_chat_id=chat.chat_id)),
            f"{chat.name} · {chat.chat_id[-8:]}" + (" · 外部群" if chat.external else ""),
        ) for chat in page.items], label="目标群聊", submit="选择此群"))
    else:
        builder.raw(_plain("本页没有符合条件的可用群。"))
    if page.next_page_token:
        builder.raw(_button(scope, source, "下一页", "results", query=query, page_token=page.next_page_token))
    builder.raw(_button(scope, source, "重新搜索", "search"))
    builder.raw(_button(scope, source, "返回创建位置", "destination"))
    return OutboundCard(card=builder.to_dict())


def fork_confirm_card(scope: FeishuScope, source: ForkSource, *, source_title: str,
                      project_alias: str, target_chat: AvailableChat | None = None) -> OutboundCard:
    """Only this final page accepts a name, so navigation has no name draft."""
    target_id = target_chat.chat_id if target_chat is not None else scope.chat_id
    target_label = target_chat.name if target_chat is not None else "当前聊天"
    builder = _header(source_title, project_alias)
    builder.raw(_plain(f"创建位置：{target_label}的新普通话题。来源会话保持不变。"))
    if target_id != scope.chat_id:
        builder.raw(_notice("目标群的参与者可通过新会话继续使用继承的上下文；后续回答可能引用来源会话的内容。"))
    reference = _encoded(_state(scope, source, "create", target_chat_id=target_id))
    default_name = " ".join(source_title.split())[:MAX_THREAD_NAME_CHARS - len(" · 分支")] + " · 分支"
    builder.raw(_form(options=[(reference, target_label)], selected=reference,
        label="已确认的目的地", submit="确认创建", create=True, inputs=({
            "tag": "input", "name": _NAME_FIELD, "required": True,
            "label": _plain_text("新会话名称"), "default_value": default_name,
            "max_length": MAX_THREAD_NAME_CHARS,
        },)))
    return OutboundCard(card=builder.to_dict())


def fork_status_card(*, name: str, source_title: str, project_alias: str,
                     status: Literal["creating", "success", "failed"],
                     topic_url: str | None = None, detail: str | None = None,
                     native_thread_id: str | None = None,
                     binding_saved: bool = False) -> OutboundCard:
    """One target root card is updated in place; status pages cannot resubmit."""
    titles = {"creating": "会话分支创建中", "success": "会话分支已创建", "failed": "会话分支创建未完成"}
    builder = _builder(titles[status], name, template="red" if status == "failed" else "blue")
    builder.raw(_plain(f"来源：{source_title}\nProject：{project_alias}"))
    if status == "creating":
        builder.raw(_notice("请等待创建完成后再发送消息。"))
    elif status == "success":
        builder.raw(_plain("在新话题中发送消息即可继续。两个会话独立管理，共享项目文件；来源会话保持不变。"))
        if topic_url:
            builder.raw({"tag": "button", "text": _plain_text("进入新话题"), "type": "primary_filled",
                "behaviors": [{"type": "open_url", "default_url": topic_url}]})
    elif native_thread_id is not None:
        builder.raw(_notice(
            "原生分支和本地会话已保存，但创建交接未完成。请先核查，不要重新创建。"
            if binding_saved else "原生分支已创建，但新话题会话绑定未完成或结果未确认。请先人工核查，不要直接重试。",
            error=True,
        ))
        builder.raw(_plain(f"原生会话：{native_thread_id}"))
    if detail:
        builder.raw(_notice(detail, error=status == "failed"))
    return OutboundCard(card=builder.to_dict())
