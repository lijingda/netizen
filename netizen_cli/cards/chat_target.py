"""Shared current-chat, group-picker and manual-ID target controls."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..pagination import paginate_items
from .callbacks import CardActionError, _plain, _plain_text

if TYPE_CHECKING:
    from ..management.chat_directory import AvailableChat


CHAT_ID_HELP_URL = "https://open.feishu.cn/document/server-docs/group/chat/chat-id-description"
_CHAT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,191}")
CHAT_SEARCH_PAGE_SIZE = 10
CHAT_SEARCH_LIMIT = 200


@dataclass(frozen=True, slots=True)
class ChatSearchSnapshot:
    """One complete search result carried by the card, never an authority/cache."""

    query: str
    chats: tuple[AvailableChat, ...]
    avatar_keys: Mapping[str, str]
    notice: str | None = None

    @property
    def total_pages(self) -> int:
        return max(1, (len(self.chats) + CHAT_SEARCH_PAGE_SIZE - 1) // CHAT_SEARCH_PAGE_SIZE)

    def page_chats(self, page: int) -> tuple[AvailableChat, ...]:
        return paginate_items(self.chats, page, page_size=CHAT_SEARCH_PAGE_SIZE).items


def encode_chat_snapshot(snapshot: ChatSearchSnapshot) -> dict[str, Any]:
    return {
        "v": 1, "query": snapshot.query,
        "chats": [{"id": chat.chat_id, "name": chat.name, "external": chat.external,
                   **({"avatar": snapshot.avatar_keys[chat.chat_id]} if chat.chat_id in snapshot.avatar_keys else {})}
                  for chat in snapshot.chats],
        **({"notice": snapshot.notice} if snapshot.notice else {}),
    }


def decode_chat_snapshot(value: Any) -> ChatSearchSnapshot:
    """Validate the untrusted display payload; saving still validates the live chat."""
    from ..management.chat_directory import AvailableChat

    error = "群聊查找结果无效或已过期，请重新查找。"
    if (not isinstance(value, Mapping) or set(value) - {"v", "query", "chats", "notice"}
            or type(value.get("v")) is not int or value["v"] != 1):
        raise CardActionError(error)
    query, items, notice = value.get("query"), value.get("chats"), value.get("notice")
    if (not isinstance(query, str) or query.strip() != query or len(query) > 50
            or any(ord(char) < 32 or 127 <= ord(char) < 160 for char in query)
            or not isinstance(items, list) or len(items) > CHAT_SEARCH_LIMIT
            or (notice is not None and (not isinstance(notice, str) or len(notice) > 2000))):
        raise CardActionError(error)
    chats, avatars, seen = [], {}, set()
    for item in items:
        if not isinstance(item, Mapping) or set(item) - {"id", "name", "external", "avatar"}:
            raise CardActionError(error)
        chat_id, name, external, avatar = (item.get(key) for key in ("id", "name", "external", "avatar"))
        if (not isinstance(chat_id, str) or _CHAT_ID.fullmatch(chat_id) is None or chat_id in seen
                or not isinstance(name, str) or len(name) > 1000
                or (external is not None and type(external) is not bool)
                or (avatar is not None and (not isinstance(avatar, str)
                    or re.fullmatch(r"img_[A-Za-z0-9_-]{1,256}", avatar) is None))):
            raise CardActionError(error)
        seen.add(chat_id)
        chats.append(AvailableChat(chat_id, name, None, external))
        if avatar is not None:
            avatars[chat_id] = avatar
    return ChatSearchSnapshot(query, tuple(chats), avatars, notice)


@dataclass(frozen=True, slots=True)
class ChatTargetDraft:
    mode: str = "current"
    choice: str = ""
    chat_id: str = ""


@dataclass(frozen=True, slots=True)
class ChatSearchView:
    """A submitted query and the independently editable next-query draft."""

    query_field: str
    query: str
    applied_query: str | None
    search_button: dict[str, Any]
    navigation: dict[str, Any] | None = None
    result_count: int | None = None
    page: int = 0
    total_pages: int = 1


def read_chat_target(form: Mapping[str, Any], *, mode_field: str,
                     choice_field: str, id_field: str) -> ChatTargetDraft:
    """Read the draft without validating the inactive field or creating a target."""
    mode = form.get(mode_field)
    if not isinstance(mode, str) or mode not in {"current", "group", "id"}:
        raise CardActionError("请选择有效的飞书聊天指定方式。")
    # Non-string inactive values are inert, not a reason to reject a valid
    # selected target. Real text drafts survive mode changes and search.
    choice = form.get(choice_field, "")
    chat_id = form.get(id_field, "")
    # Native optional form fields may be absent or null when untouched.
    choice = "" if choice is None else choice
    chat_id = "" if chat_id is None else chat_id
    if (mode == "group" and not isinstance(choice, str)) or (mode == "id" and not isinstance(chat_id, str)):
        raise CardActionError("飞书聊天输入无效，请检查后重试。")
    return ChatTargetDraft(
        mode,
        choice if isinstance(choice, str) else "",
        chat_id if isinstance(chat_id, str) else "",
    )


def resolve_chat_target(draft: ChatTargetDraft, *, current_chat_id: str,
                        choice_error: str = "请选择飞书群聊。") -> str:
    """Only the selected input may determine the target; never cross-fallback."""
    if draft.mode == "current":
        target = current_chat_id
    elif draft.mode == "group":
        target = draft.choice
        if not target:
            raise CardActionError(choice_error)
    elif draft.mode == "id":
        target = draft.chat_id.strip()
        if not target:
            raise CardActionError("请填写飞书聊天 ID，或选择当前聊天。")
    else:
        raise CardActionError("请选择有效的飞书聊天指定方式。")
    if _CHAT_ID.fullmatch(target) is None:
        raise CardActionError("飞书聊天 ID 无效，请检查后重试。")
    return target


def initial_chat_target(*, current_chat_id: str, target_chat_id: str | None,
                        options: Sequence[dict[str, Any]]) -> ChatTargetDraft:
    """Choose an initial input mode without changing an existing destination."""
    if target_chat_id is None or target_chat_id == current_chat_id:
        return ChatTargetDraft()
    if any(option["value"] == target_chat_id for option in options):
        return ChatTargetDraft("group", target_chat_id)
    return ChatTargetDraft("id", chat_id=target_chat_id)


def chat_options(chats: Sequence[AvailableChat], *,
                 avatar_keys: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """Render only group identities, with avatars and duplicate-name hints."""
    groups = {}
    for chat in chats:
        groups.setdefault(chat.chat_id, chat)
    labels = {
        chat_id: " ".join(chat.name.split()) or chat_id
        for chat_id, chat in groups.items()
    }
    counts = Counter(labels.values())
    options = []
    for chat_id, chat in groups.items():
        if _CHAT_ID.fullmatch(chat_id) is None:
            raise CardActionError("飞书聊天 ID 无效，请重新打开表单。")
        label = labels[chat_id]
        if counts[label] > 1:
            label += f" · {chat_id[-8:]}"
        if chat.external:
            label += " · 外部群"
        avatar_key = avatar_keys.get(chat_id) if avatar_keys else None
        icon = {"tag": "standard_icon", "token": "group_outlined"}
        if avatar_key:
            icon = {"tag": "custom_icon", "img_key": avatar_key}
        options.append({
            "text": _plain_text(label), "value": chat_id,
            "icon": icon,
        })
    return options


def snapshot_chat_options(snapshot: ChatSearchSnapshot, page: int,
                          selected: str) -> list[dict[str, Any]]:
    """Keep names stable across pages and append only a retained selection."""
    options = chat_options(snapshot.chats, avatar_keys=snapshot.avatar_keys)
    page_ids = {chat.chat_id for chat in snapshot.page_chats(page)}
    visible = [option for option in options if option["value"] in page_ids]
    if selected not in page_ids:
        visible.extend(option for option in options if option["value"] == selected)
    return visible


def snapshot_capacity_selections(snapshot: ChatSearchSnapshot, page: int) -> tuple[str, ...]:
    """Choose the largest in-page and retained states for actual-card checks.

    The caller renders both selections through ordinary shared target fields.
    In-page selections add a caption and initial value; off-page selections
    also add an option (and more tags). Checking each class covers both limits
    without a separate approximate byte/tag budget.
    """
    page_ids = {chat.chat_id for chat in snapshot.page_chats(page)}
    options = chat_options(snapshot.chats, avatar_keys=snapshot.avatar_keys)
    selections = []
    for retained in (False, True):
        candidates = [option for option in options if (option["value"] not in page_ids) is retained]
        if candidates:
            largest = max(candidates, key=lambda option: len(json.dumps(
                [option if retained else None,
                 _plain(f"已选：{option['text']['content']}（翻页时保留）"), option["value"]],
                ensure_ascii=False,
            ).encode("utf-8")))
            selections.append(largest["value"])
    return tuple(selections)


def chat_target_elements(*, mode_field: str, choice_field: str, id_field: str,
                         draft: ChatTargetDraft, options: Sequence[dict[str, Any]],
                         search: ChatSearchView | None = None) -> list[dict[str, Any]]:
    """Both inputs remain editable; the mode alone determines the saved value."""
    if draft.mode not in {"current", "group", "id"}:
        raise CardActionError("请选择有效的飞书聊天指定方式。")
    choices = list(options)
    if search is not None and search.applied_query is None:
        # Before an explicit search, only a retained destination is
        # meaningful. Never present an arbitrary preloaded page as all groups.
        choices = [option for option in choices if option["value"] == draft.choice]
    choice = {
        "tag": "select_static", "name": choice_field, "required": False,
        "width": "fill", "placeholder": _plain_text("请选择本页群聊" if search else "请选择群聊"),
        "options": choices,
    }
    if draft.choice and any(option["value"] == draft.choice for option in choices):
        choice["initial_option"] = draft.choice
    elements = [
        {"tag": "select_static", "name": mode_field, "required": True,
         "width": "fill", "placeholder": _plain_text("飞书聊天指定方式"),
         "options": [
             {"text": _plain_text("当前聊天"), "value": "current"},
             {"text": _plain_text("选择群聊"), "value": "group"},
             {"text": _plain_text("填写聊天 ID"), "value": "id"},
         ], "initial_option": draft.mode},
    ]
    elements.append(_plain("群聊选择（仅选择群聊时生效）"))
    if search is not None:
        elements.extend([
            {"tag": "input", "name": search.query_field, "required": False,
             "width": "fill", "max_length": 50, "label": _plain_text("查找群聊 · 群名关键词"),
             "default_value": search.query, "placeholder": _plain_text("留空查找全部可选群")},
            search.search_button,
        ])
        if search.applied_query is not None:
            query_label = f"关键词「{search.applied_query}」" if search.applied_query else "全部可选群"
            page_label = (f" · 共 {search.result_count} 个群 · 第 {search.page + 1}/{search.total_pages} 页"
                          if search.result_count is not None else "")
            elements.append(_plain(f"查找结果 · {query_label}{page_label}。修改关键词后请重新点击查找群聊。"))
        elif choices:
            elements.append(_plain("当前选择；如需其他群，请先查找群聊。"))
        else:
            elements.append(_plain("请先查找群聊，再从查找结果中选择。"))
    if choices or search is None:
        if search is not None and draft.choice and "initial_option" in choice:
            selected = next(option for option in choices if option["value"] == draft.choice)
            elements.append(_plain(f"已选：{selected['text']['content']}（翻页时保留）"))
        elements.append(choice)
    elif search is not None and search.applied_query is not None:
        elements.append(_plain("没有找到可选群聊，请修改关键词重新查找。"))
    if search is not None and search.navigation is not None:
        elements.append(search.navigation)
    elements.extend([
        {"tag": "input", "name": id_field, "required": False, "width": "fill",
         "label": _plain_text("聊天 ID（仅填写 ID 时生效）"),
         "default_value": draft.chat_id, "max_length": 192,
         "placeholder": _plain_text("请输入聊天 ID（oc_…），不能为空")},
        _plain("仅指定方式对应的输入生效；填写 ID 时不能为空。聊天 ID 不是用户 ID；单聊请选择与本机器人的聊天。"),
        {"tag": "markdown", "content": f"[如何获取聊天 ID]({CHAT_ID_HELP_URL})"},
    ])
    return elements
