"""Self-contained forms for ordinary persistent forks."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
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
from .chat_target import (
    ChatSearchSnapshot,
    ChatSearchView,
    ChatTargetDraft,
    chat_options,
    chat_target_elements,
    decode_chat_snapshot,
    encode_chat_snapshot,
    initial_chat_target,
    read_chat_target,
    resolve_chat_target,
    snapshot_capacity_selections,
    snapshot_chat_options,
)
from .controls import MAX_THREAD_NAME_CHARS
from .pagination import decode_page_selection, pagination_controls
from .reply import TURN_FILE_CARD_JSON_LIMIT_BYTES

if TYPE_CHECKING:
    from ..management.chat_directory import AvailableChat


_VERSION = 5
_MODE_FIELD = "fork_mode_v5"
_TARGET_FIELD = "fork_target_v5"
_ID_FIELD = "fork_chat_id_v5"
_NAME_FIELD = "fork_name_v5"
_QUERY_FIELD = "fork_query_v5"
_PAGE_FIELD = "fork_page_v5"
_SOURCE_FIELDS = {
    "binding_id", "native_thread_id", "settings_revision", "context_revision",
    "feedback_revision", "project_revision",
}
_STATE_FIELDS = {"kind", "v", "scope", "source", "action"}
_ACTION_FIELDS = {
    "create": {"nonce"}, "search": {"nonce"},
    "page": {"nonce", "snapshot"},
}
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,191}")
_BINDING_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,127}")
_CARD_ELEMENT_LIMIT = 200


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
    target_mode: str = "current"
    target_choice: str = ""
    target_id: str = ""
    query_input: str | None = None
    search_requested: bool = False
    chat_snapshot: ChatSearchSnapshot | None = None
    chat_page: int = 0


class ForkFormValidationError(CardActionError):
    """A valid draft with a recoverable input error, not a creation request."""

    def __init__(self, message: str, draft: ForkCardAction) -> None:
        super().__init__(message)
        self.draft = draft


class ForkCardCapacityError(CardActionError):
    """The candidates need a narrower query to fit the platform card."""


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


def _query(value: Any) -> str:
    if not isinstance(value, str) or any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value):
        raise CardActionError("群名关键词无效。")
    query = value.strip()
    if len(query) > 50:
        raise CardActionError("群名关键词最多 50 个字符。")
    return query


def _name(value: Any, *, create: bool) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise CardActionError("会话名称无效。")
    if len(value) > MAX_THREAD_NAME_CHARS:
        raise CardActionError(f"会话名称最多 {MAX_THREAD_NAME_CHARS} 个字符。")
    if not create:
        return value
    name = " ".join(value.split())
    if not name:
        raise CardActionError("会话名称不能为空。")
    return name


def _validate_state(scope: FeishuScope, state: Any) -> tuple[ForkSource, ChatSearchSnapshot | None]:
    if not isinstance(state, Mapping):
        raise CardActionError("分支卡片动作无效，请重新发送 /fork。")
    if state.get("kind") != "netizen_fork" or type(state.get("v")) is not int or state["v"] != _VERSION:
        raise CardActionError("分支卡片已过期，请重新发送 /fork。")
    action = state.get("action")
    if not isinstance(action, str) or action not in _ACTION_FIELDS:
        raise CardActionError("未知分支卡片动作。")
    if set(state) != _STATE_FIELDS | _ACTION_FIELDS[action]:
        raise CardActionError("分支卡片动作字段不完整或包含未知字段。")
    if state["scope"] != scope.key:
        raise CardActionError("分支卡片与来源会话位置不一致。")
    if not _valid_callback_nonce(state["nonce"]):
        raise CardActionError("分支卡片动作无效，请重新发送 /fork。")
    snapshot = decode_chat_snapshot(state["snapshot"]) if action == "page" else None
    return _source(state["source"]), snapshot


def is_fork_card_action(value: Any, form: Any = None) -> bool:
    return (isinstance(value, Mapping) and value.get("kind") == "netizen_fork") or (
        isinstance(form, Mapping) and any(
            name in {_MODE_FIELD, _TARGET_FIELD, _ID_FIELD, _NAME_FIELD, _QUERY_FIELD, _PAGE_FIELD}
            for name in form
        )
    )


def decode_fork_action(scope: FeishuScope, value: Any, form: Any = None) -> ForkCardAction:
    """Read public callback value and form_value; callers verify card Scope."""
    source, snapshot = _validate_state(scope, value)
    if not isinstance(form, Mapping) or _MODE_FIELD not in form or (
        set(form) - {_MODE_FIELD, _TARGET_FIELD, _ID_FIELD, _NAME_FIELD, _QUERY_FIELD, _PAGE_FIELD}
    ):
        raise CardActionError("分支表单缺少字段或混入其他操作。")
    target_draft = read_chat_target(form, mode_field=_MODE_FIELD,
                                   choice_field=_TARGET_FIELD, id_field=_ID_FIELD)
    action = value["action"]
    name_input = form.get(_NAME_FIELD, "")
    name = _name("" if name_input is None else name_input, create=False)
    query_input = form.get(_QUERY_FIELD, "")
    query = _query("" if query_input is None else query_input)
    # Search must work with an empty selected target and preserve the name/ID
    # draft. Only the create action resolves and validates a destination.
    draft = ForkCardAction(
        "results", source, name=name, query=query,
        target_mode=target_draft.mode, target_choice=target_draft.choice,
        target_id=target_draft.chat_id, query_input=query,
    )
    if action == "create":
        try:
            target = resolve_chat_target(target_draft, current_chat_id=scope.chat_id,
                                         choice_error="请选择创建位置。")
            normalized_name = _name(name, create=True)
        except CardActionError as error:
            raise ForkFormValidationError(str(error), draft) from None
        return ForkCardAction(
            "create", source, target_chat_id=target, name=normalized_name, query=query,
            target_mode=target_draft.mode, target_choice=target_draft.choice,
            target_id=target_draft.chat_id, query_input=query,
        )
    # Only Search applies edited text. Paging always follows its rendered
    # result snapshot and retains the independent draft for a later search.
    page = decode_page_selection(form.get(_PAGE_FIELD), snapshot.total_pages) if snapshot else 0
    applied_query = snapshot.query if snapshot else query
    selected = target_draft.choice if action == "page" else ""
    return ForkCardAction(
        "results", source, name=name, query=applied_query, target_mode=target_draft.mode,
        target_choice=selected, target_id=target_draft.chat_id, query_input=query,
        search_requested=action == "search", chat_snapshot=snapshot, chat_page=page,
    )


def _button(scope: FeishuScope, source: ForkSource, label: str, action: str, **extra: Any) -> dict[str, Any]:
    value = {
        "kind": "netizen_fork", "v": _VERSION, "scope": scope.key,
        "source": asdict(source), "action": action, **extra,
    }
    value["nonce"] = _new_callback_nonce()
    _validate_state(scope, value)
    return {
        "tag": "button", "name": f"fork_{action}_v5", "text": _plain_text(label),
        "type": "primary_filled" if action == "create" else "default", "width": "fill",
        "form_action_type": "submit", "behaviors": [{"type": "callback", "value": value}],
    }


def _label(value: str, limit: int = 80) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _element_count(value: Any) -> int:
    if isinstance(value, dict):
        return int("tag" in value) + sum(_element_count(item) for item in value.values())
    if isinstance(value, list):
        return sum(_element_count(item) for item in value)
    return 0


def fork_form_card(scope: FeishuScope, source: ForkSource, *, source_title: str,
                   project_alias: str, chats: Sequence[AvailableChat] = (),
                   avatar_keys: Mapping[str, str] | None = None,
                   name: str | None = None, target_chat_id: str | None = None,
                   target_mode: str | None = None, target_choice: str | None = None,
                   target_id: str = "",
                   query: str = "",
                   query_input: str | None = None,
                   chat_snapshot: ChatSearchSnapshot | None = None, chat_page: int = 0,
                   notice: str | None = None) -> OutboundCard:
    """One form carries the name and target through search and final creation."""
    query = _query(chat_snapshot.query if chat_snapshot else query)
    if chat_snapshot is not None:
        # Validate the requested page even for a single-page result; bad page
        # state must not silently show a different slice.
        chat_snapshot.page_chats(chat_page)
        options = chat_options(chat_snapshot.chats, avatar_keys=chat_snapshot.avatar_keys)
    else:
        options = chat_options(chats, avatar_keys=avatar_keys)
    target = (initial_chat_target(current_chat_id=scope.chat_id, target_chat_id=target_chat_id, options=options)
              if target_mode is None else ChatTargetDraft(target_mode, target_choice or "", target_id))
    default_name = " ".join(source_title.split())[:MAX_THREAD_NAME_CHARS - len(" · 分支")] + " · 分支"
    draft_name = _name(default_name if name is None else name, create=False)
    target_notice = (
        "指定其他聊天时，目标聊天的参与者可通过新会话继续使用继承的上下文；后续回答可能引用来源会话的内容。\n"
        "可选群为机器人已加入的群；不按操作者的群成员身份筛选，当前卡片的参与者均可见。"
    )
    if notice:
        target_notice += "\n" + _label(notice, 512)
    if chat_snapshot is not None and chat_snapshot.notice:
        target_notice += "\n" + _label(chat_snapshot.notice, 512)

    def render(page: int, selection: str) -> OutboundCard:
        page_options = options
        navigation = None
        if chat_snapshot is not None:
            page_options = snapshot_chat_options(chat_snapshot, page, selection)
            if chat_snapshot.total_pages > 1:
                navigation = pagination_controls(
                    page_field=_PAGE_FIELD, page=page, total_pages=chat_snapshot.total_pages,
                    button=_button(scope, source, "跳转", "page", snapshot=encode_chat_snapshot(chat_snapshot)),
                )
        search = ChatSearchView(
            query_field=_QUERY_FIELD, query=query if query_input is None else _query(query_input),
            applied_query=query if chat_snapshot is not None else None,
            search_button=_button(scope, source, "查找群聊", "search"), navigation=navigation,
            result_count=len(chat_snapshot.chats) if chat_snapshot is not None else None,
            page=page, total_pages=chat_snapshot.total_pages if chat_snapshot is not None else 1,
        )
        builder = _builder("创建会话分支", _label(project_alias, 120))
        builder.raw(_plain(f"来源：{_label(source_title, 120)}\n"
                           "新会话继承当前原生上下文。两个会话共享项目文件，修改会相互可见。"))
        builder.raw(_notice(target_notice))
        fields = [
            {"tag": "input", "name": _NAME_FIELD, "width": "fill",
             "label": _plain_text("分支名称"), "default_value": draft_name,
             "max_length": MAX_THREAD_NAME_CHARS},
            _plain("目标飞书聊天 · 在所选聊天中新建分支话题"),
            *chat_target_elements(
                mode_field=_MODE_FIELD, choice_field=_TARGET_FIELD, id_field=_ID_FIELD,
                draft=ChatTargetDraft(target.mode, selection, target.chat_id), options=page_options,
                search=search,
            ),
            _button(scope, source, "创建分支", "create"),
        ]
        builder.raw({"tag": "form", "name": "fork_form_v5", "elements": fields})
        card = builder.to_dict()
        # Check the actual SDK serialization, including the single snapshot
        # callback and avatar keys; never truncate a complete search result.
        if len(json.dumps(card, ensure_ascii=False).encode("utf-8")) > TURN_FILE_CARD_JSON_LIMIT_BYTES or (
            _element_count(card) > _CARD_ELEMENT_LIMIT
        ):
            raise ForkCardCapacityError("完整群聊结果超过分支卡片容量，请缩小群名关键词范围后重新查找。")
        return OutboundCard(card=card)

    card = render(chat_page, target.choice)
    if chat_snapshot is not None:
        # A later page may have longer labels, or keep a selection from another
        # page. Global name disambiguation keeps every option's size stable;
        # the largest in-page and retained selections cover bytes and tags.
        for page in range(chat_snapshot.total_pages):
            for capacity_selection in snapshot_capacity_selections(chat_snapshot, page):
                render(page, capacity_selection)
    return card


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
