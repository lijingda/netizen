"""Bounded, read-only target validation and live group discovery.

This is not a historical directory: no group metadata or pagination state is
retained. The shared official client uses only the instance's bot credentials.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from lark_oapi.api.im.v1.model.get_chat_request import GetChatRequest
from lark_oapi.api.im.v1.model.is_in_chat_chat_members_request import IsInChatChatMembersRequest
from lark_oapi.api.im.v1.model.list_chat_request import ListChatRequest
from lark_oapi.api.im.v2.model.chat_search_filter import ChatSearchFilter
from lark_oapi.api.im.v2.model.search_chat_request import SearchChatRequest
from lark_oapi.api.im.v2.model.search_chat_request_body import SearchChatRequestBody

from ..channel.messages import public_chat_kind
from ..chat_avatars import avatar_url
from ..chat_targets import ChatTargetError, ValidatedChatTarget


_PAGE_SIZE = 20
_QUERY_SECONDS = 8.0
_CALL_CONCURRENCY = 4
_MAX_PAGE_TOKEN = 1_024


class ChatDirectoryError(ChatTargetError):
    """Stable, non-sensitive error safe for the management response."""


@dataclass(frozen=True, slots=True)
class AvailableChat:
    chat_id: str
    name: str
    # List v1 does not promise a mode; do not invent group versus topic.
    chat_mode: str | None
    external: bool | None
    avatar_url: str | None = None


@dataclass(frozen=True, slots=True)
class AvailableChatPage:
    items: tuple[AvailableChat, ...]
    next_page_token: str | None
    notice: str | None = None


class ChatDirectory(Protocol):
    async def query(self, *, query: str, page_token: str | None, page_size: int = _PAGE_SIZE) -> AvailableChatPage: ...

    async def validate(self, chat_id: str) -> AvailableChat: ...

    async def validate_target(self, chat_id: str) -> ValidatedChatTarget: ...


class FeishuChatDirectory:
    """Use only the public typed list/search/get/membership SDK operations."""

    def __init__(self, client: Any, *, query_seconds: float = _QUERY_SECONDS) -> None:
        if query_seconds <= 0:
            raise ValueError("chat directory query budget must be positive")
        self._client = client
        self._query_seconds = query_seconds
        self._calls = asyncio.Semaphore(_CALL_CONCURRENCY)

    async def query(self, *, query: str = "", page_token: str | None = None,
                    page_size: int = _PAGE_SIZE) -> AvailableChatPage:
        if not isinstance(query, str) or len(query) > 50 or query.strip() != query:
            raise ChatDirectoryError("invalid_query", "群名关键词最多 50 个字符，不能包含首尾空白。")
        if page_token is not None and not _valid_token(page_token):
            raise ChatDirectoryError("invalid_cursor", "分页游标无效，请重新搜索。")
        if type(page_size) is not int or not 1 <= page_size <= _PAGE_SIZE:
            raise ChatDirectoryError("invalid_page_size", "群聊查询页大小无效。")
        try:
            async with asyncio.timeout(self._query_seconds):
                return await self._query_page(query=query, page_token=page_token, page_size=page_size)
        except TimeoutError:
            raise ChatDirectoryError("chat_query_timeout", "群聊查询超时，请重试。") from None

    async def _query_page(self, *, query: str, page_token: str | None, page_size: int) -> AvailableChatPage:
        if query:
            body = (
                SearchChatRequestBody.builder().query(query)
                .filter(ChatSearchFilter.builder()
                        .search_types(["private", "external", "public_joined"])
                        .disable_search_by_user(True).build())
                .build()
            )
            builder = SearchChatRequest.builder().page_size(page_size).request_body(body)
            if page_token is not None:
                builder.page_token(page_token)
            data = await self._call(self._client.im.v2.chat.asearch, builder.build())
        else:
            list_builder = ListChatRequest.builder().page_size(page_size).sort_type("ByCreateTimeAsc")
            if page_token is not None:
                list_builder.page_token(page_token)
            data = await self._call(self._client.im.v1.chat.alist, list_builder.build())

        raw_items = getattr(data, "items", None)
        has_more = getattr(data, "has_more", None)
        if not isinstance(raw_items, list) or len(raw_items) > page_size or type(has_more) is not bool:
            raise _contract_error()
        next_token = getattr(data, "page_token", None) if has_more else None
        if has_more and (not _valid_token(next_token) or next_token == page_token):
            raise _contract_error()
        notice = getattr(data, "notice", None)
        if notice is not None and (not isinstance(notice, str) or len(notice) > 2_000):
            raise _contract_error()

        candidates: dict[str, AvailableChat] = {}
        for raw in raw_items:
            metadata = getattr(raw, "meta_data", None) if query else raw
            if metadata is None:
                raise _contract_error()
            status = getattr(metadata, "chat_status", None)
            if status in {"dissolved", "dissolved_save"}:
                continue
            if status != "normal":
                raise _contract_error()
            if public_chat_kind(metadata) == "p2p":
                continue
            chat = _chat(metadata, getattr(metadata, "chat_id", None), search_result=bool(query))
            candidates[chat.chat_id] = chat

        # Search visibility is broader than membership, even with filters.
        # List is explicitly the bot's joined groups; validate() rechecks a
        # selected group so a later removal cannot silently select it.
        if query:
            tasks = [asyncio.create_task(self._is_member(chat_id)) for chat_id in candidates]
            try:
                joined = await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            items = tuple(chat for chat, member in zip(candidates.values(), joined) if member)
        else:
            items = tuple(candidates.values())
        return AvailableChatPage(items, next_token, notice or None)

    async def validate(self, chat_id: str) -> AvailableChat:
        if not _valid_chat_id(chat_id):
            raise ChatDirectoryError("invalid_chat_id", "请输入聊天 ID，而不是用户 ID。")
        try:
            async with asyncio.timeout(self._query_seconds):
                if not await self._is_member(chat_id):
                    raise ChatDirectoryError("chat_unavailable", "机器人当前不在该群中，请重新选择。")
                request = GetChatRequest.builder().chat_id(chat_id).build()
                data = await self._call(self._client.im.v1.chat.aget, request)
                if getattr(data, "chat_status", None) in {"dissolved", "dissolved_save"}:
                    raise ChatDirectoryError("chat_unavailable", "该群已解散，请重新选择。")
                if getattr(data, "chat_status", None) != "normal":
                    raise _contract_error()
                kind = public_chat_kind(data)
                if kind == "p2p":
                    raise ChatDirectoryError("not_group_chat", "请选择群聊；单聊请切换到单聊并输入聊天 ID。")
                if kind != "group":
                    raise _contract_error()
                return _chat(data, chat_id)
        except TimeoutError:
            raise ChatDirectoryError("chat_query_timeout", "群聊查询超时，请重试。") from None

    async def _is_member(self, chat_id: str) -> bool:
        request = IsInChatChatMembersRequest.builder().chat_id(chat_id).build()
        data = await self._call(self._client.im.v1.chat_members.ais_in_chat, request)
        member = getattr(data, "is_in_chat", None)
        if type(member) is not bool:
            raise _contract_error()
        return member

    async def validate_target(self, chat_id: str) -> ValidatedChatTarget:
        """Confirm a submitted group or bot P2P without sending a test message.

        Group metadata is visible to nonmembers, so membership is a separate
        proof. Bot-authenticated P2P metadata does not use the group membership
        API. Neither proof promises future availability or sending permission.
        """
        if not _valid_chat_id(chat_id):
            raise ChatDirectoryError("invalid_chat_id", "请输入飞书聊天 ID（oc_ 开头），不是用户 ID。")
        try:
            async with asyncio.timeout(self._query_seconds):
                request = GetChatRequest.builder().chat_id(chat_id).build()
                data = await self._call(self._client.im.v1.chat.aget, request)
                kind = public_chat_kind(data)
                if kind not in {"group", "p2p"}:
                    raise ChatDirectoryError("chat_kind_unknown", "无法确认目标飞书聊天类型，请检查聊天 ID 后重试。")
                status = getattr(data, "chat_status", None)
                if status in {"dissolved", "dissolved_save"}:
                    raise ChatDirectoryError("chat_unavailable", "目标飞书聊天已解散，请重新选择。")
                # P2P does not promise group-specific metadata. An explicitly
                # unknown state still fails closed; absence is not dissolution.
                if status != "normal" and not (kind == "p2p" and status is None):
                    raise ChatDirectoryError("chat_query_failed", "无法确认目标飞书聊天状态，请稍后重试。")
                if kind == "group" and not await self._is_member(chat_id):
                    raise ChatDirectoryError("chat_unavailable", "机器人当前不在目标群中，请先将机器人加入群聊。")
                return ValidatedChatTarget(chat_id, kind)
        except TimeoutError:
            raise ChatDirectoryError("chat_query_timeout", "飞书聊天校验超时，请重试。") from None

    async def _call(self, operation: Callable[[Any], Awaitable[Any]], request: Any) -> Any:
        try:
            async with self._calls:
                response = await operation(request)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ChatDirectoryError("chat_query_failed", "无法读取飞书聊天，请稍后重试。") from None
        if getattr(response, "code", None) != 0:
            code = getattr(response, "code", None)
            status = getattr(getattr(response, "raw", None), "status_code", None)
            if code == 231020:
                raise ChatDirectoryError("invalid_cursor", "分页已失效，请重新搜索。")
            if code == 231022:
                raise ChatDirectoryError("chat_search_limit", "已达到飞书搜索分页上限，请使用更具体的群名。")
            if status == 429:
                raise ChatDirectoryError("chat_query_rate_limited", "飞书聊天查询过于频繁，请稍后重试。")
            if status == 403 or code in {99991672, 232033}:
                raise ChatDirectoryError("chat_permission_denied", "飞书聊天读取权限不足，请检查应用权限后重试。")
            if code == 232006:
                raise ChatDirectoryError("chat_unavailable", "目标飞书聊天不存在或当前不可访问，请重新选择。")
            raise ChatDirectoryError("chat_query_failed", "无法读取飞书聊天，请检查应用状态或稍后重试。")
        data = getattr(response, "data", None)
        if data is None:
            raise _contract_error()
        return data


def _contract_error() -> ChatDirectoryError:
    return ChatDirectoryError("chat_query_failed", "飞书聊天数据不完整，无法确认目标可用，请重试。")


def _valid_token(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= _MAX_PAGE_TOKEN and all(32 < ord(c) < 127 for c in value)


def _valid_chat_id(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("oc_") and 3 < len(value) <= 256 and all(
        c.isascii() and (c.isalnum() or c in "_-") for c in value
    )


def _chat(data: Any, chat_id: Any, *, search_result: bool = False) -> AvailableChat:
    if not _valid_chat_id(chat_id):
        raise _contract_error()
    name = getattr(data, "name", None)
    mode = getattr(data, "chat_mode", None)
    external = getattr(data, "external", None)
    # A normalized public chat_type is authoritative for group versus p2p.
    # It does not establish ordinary-group versus topic mode: retain unknown
    # when the optional mode is absent or conflicts with that explicit kind.
    # The group-only v2 search currently returns DEFAULT in a live tenant,
    # despite its docs describing group/topic. Its subtype is display-only;
    # preserve unknown rather than guessing undocumented enum mappings. A
    # selected result still goes through get + membership in validate().
    if (search_result or getattr(data, "chat_type", None) == "group") and mode not in {"group", "topic"}:
        mode = None
    if (
        not isinstance(name, str) or len(name) > 1_000
        or mode not in {"group", "topic", None}
        or (external is not None and type(external) is not bool)
    ):
        raise _contract_error()
    return AvailableChat(chat_id, name or chat_id, mode, external, avatar_url(getattr(data, "avatar", None)))
