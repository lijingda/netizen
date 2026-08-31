"""Bounded, metadata-only Feishu history discovery for catch-up context.

The reader deliberately stops at exact message references.  Message bodies are
normalized later through the public Channel SDK boundary, so this module never
reads or retains the ``Message.body`` returned by the OpenAPI list endpoint.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

from lark_oapi.api.im.v1.model.get_message_request import GetMessageRequest
from lark_oapi.api.im.v1.model.list_message_request import ListMessageRequest

from .domain import FeishuScope, MessageContextAnchor, ScopeKind


_GET_TIMEOUT_SECONDS = 10.0
_PAGE_TIMEOUT_SECONDS = 10.0
_TOTAL_TIMEOUT_SECONDS = 60.0
_MAX_PAGES = 10
_MAX_RAW_MESSAGES = 500
_PAGE_SIZE = 50
_SORT_DESCENDING = "ByCreateTimeDesc"


class MessageHistoryError(RuntimeError):
    """A history failure with a message that is safe to show to the user."""


class MessageHistoryUnavailable(MessageHistoryError):
    """The exact history window could not be read within the bounded attempt."""


class MessageHistoryContractError(MessageHistoryError):
    """The pinned SDK or platform response violated the expected public shape."""


@dataclass(frozen=True, slots=True)
class MessageHistoryRef:
    """Stable metadata required to fetch and normalize one exact message.

    Display names are deliberately absent: the exact-message normalization
    boundary resolves the name used for prompt attribution.  A mutable name
    returned by the history-list endpoint is not an identity invariant.
    """

    message_id: str
    create_time_ms: int
    sender_id: str
    message_type: str


@dataclass(frozen=True, slots=True)
class MessageHistoryStats:
    pages_scanned: int
    raw_messages_scanned: int
    duplicate_messages: int
    ignored_after_upper: int
    omitted_messages: int
    truncated_before: bool
    scan_limit_hit: bool


@dataclass(frozen=True, slots=True)
class MessageHistoryWindow:
    """The open interval ``(lower, upper)`` in lower-to-upper order."""

    lower: MessageContextAnchor
    upper: MessageContextAnchor
    candidates: tuple[MessageHistoryRef, ...]
    stats: MessageHistoryStats


class MessageHistoryReader(Protocol):
    async def resolve_anchor(
        self,
        scope: FeishuScope,
        message_id: str,
    ) -> MessageContextAnchor: ...

    async def read_window(
        self,
        scope: FeishuScope,
        lower: MessageContextAnchor,
        upper_id: str,
    ) -> MessageHistoryWindow: ...


class FeishuMessageHistoryReader:
    """Read one exact, bounded group-main or topic history window."""

    def __init__(
        self,
        client: Any,
        *,
        get_timeout_seconds: float = _GET_TIMEOUT_SECONDS,
        page_timeout_seconds: float = _PAGE_TIMEOUT_SECONDS,
        total_timeout_seconds: float = _TOTAL_TIMEOUT_SECONDS,
        max_pages: int = _MAX_PAGES,
        max_raw_messages: int = _MAX_RAW_MESSAGES,
        page_size: int = _PAGE_SIZE,
    ) -> None:
        if client is None:
            raise ValueError("message history client is required")
        if min(
            get_timeout_seconds,
            page_timeout_seconds,
            total_timeout_seconds,
        ) <= 0:
            raise ValueError("message history timeouts must be positive")
        if (
            get_timeout_seconds > _GET_TIMEOUT_SECONDS
            or page_timeout_seconds > _PAGE_TIMEOUT_SECONDS
            or total_timeout_seconds > _TOTAL_TIMEOUT_SECONDS
        ):
            raise ValueError("message history timeouts exceed the safe maximum")
        if min(max_pages, max_raw_messages, page_size) <= 0:
            raise ValueError("message history limits must be positive")
        if (
            max_pages > _MAX_PAGES
            or max_raw_messages > _MAX_RAW_MESSAGES
            or page_size > _PAGE_SIZE
        ):
            raise ValueError("message history limits exceed the safe maximum")

        self._client = client
        self._get_timeout_seconds = get_timeout_seconds
        self._page_timeout_seconds = page_timeout_seconds
        self._total_timeout_seconds = total_timeout_seconds
        self._max_pages = max_pages
        self._max_raw_messages = max_raw_messages
        self._page_size = page_size

    async def resolve_anchor(
        self,
        scope: FeishuScope,
        message_id: str,
    ) -> MessageContextAnchor:
        """Resolve and scope-check one exact message through typed ``aget``."""

        _require_history_scope(scope)
        exact_id = _required_string(message_id, "message_id")
        item = await self._get_exact_item(exact_id)
        return _anchor_from_exact_item(scope, item, expected_id=exact_id)

    async def read_window(
        self,
        scope: FeishuScope,
        lower: MessageContextAnchor,
        upper_id: str,
    ) -> MessageHistoryWindow:
        """Return metadata refs strictly between two exact message endpoints."""

        _require_history_scope(scope)
        if not isinstance(lower, MessageContextAnchor):
            raise ValueError("lower must be a MessageContextAnchor")
        exact_upper_id = _required_string(upper_id, "upper_id")
        if lower.message_id == exact_upper_id:
            raise MessageHistoryContractError(
                "上下文起止消息相同，本条消息未执行。"
            )

        try:
            async with asyncio.timeout(self._total_timeout_seconds):
                return await self._read_window(scope, lower, exact_upper_id)
        except MessageHistoryError:
            raise
        except TimeoutError as exc:
            raise MessageHistoryUnavailable(
                "读取群聊上下文超过时间限制，本条消息未执行；请重试。"
            ) from exc

    async def _read_window(
        self,
        scope: FeishuScope,
        lower: MessageContextAnchor,
        upper_id: str,
    ) -> MessageHistoryWindow:
        lower_item = await self._get_exact_item(lower.message_id)
        resolved_lower = _anchor_from_exact_item(
            scope,
            lower_item,
            expected_id=lower.message_id,
        )
        if resolved_lower != lower:
            raise MessageHistoryUnavailable(
                "已保存的群聊上下文边界与飞书当前记录不一致，"
                "本条消息未执行；请重试或重新配置会话。"
            )

        upper_item = await self._get_exact_item(upper_id)
        upper = _anchor_from_exact_item(
            scope,
            upper_item,
            expected_id=upper_id,
        )
        if upper.create_time_ms < lower.create_time_ms:
            raise MessageHistoryContractError(
                "群聊上下文消息顺序无法确认，本条消息未执行。"
            )

        return await self._scan_window(scope, lower, upper)

    async def _get_exact_item(self, message_id: str) -> Any:
        request = (
            GetMessageRequest.builder()
            .message_id(message_id)
            .user_id_type("open_id")
            .with_sender_name(True)
            .build()
        )
        try:
            async with asyncio.timeout(self._get_timeout_seconds):
                response = await self._client.im.v1.message.aget(request)
        except TimeoutError as exc:
            raise MessageHistoryUnavailable(
                "读取群聊上下文消息超时，本条消息未执行；请重试。"
            ) from exc
        except Exception as exc:
            raise MessageHistoryUnavailable(
                "无法读取群聊上下文消息，本条消息未执行；请重试。"
            ) from exc

        _require_success(response, operation="读取群聊上下文消息")
        data = getattr(response, "data", None)
        items = getattr(data, "items", None)
        if not isinstance(items, (list, tuple)) or len(items) != 1:
            raise MessageHistoryContractError(
                "飞书没有返回唯一的群聊上下文消息，本条消息未执行。"
            )
        return items[0]

    async def _scan_window(
        self,
        scope: FeishuScope,
        lower: MessageContextAnchor,
        upper: MessageContextAnchor,
    ) -> MessageHistoryWindow:
        page_token: str | None = None
        seen_page_tokens: set[str] = set()
        seen_message_ids: set[str] = set()
        candidates_descending: list[MessageHistoryRef] = []
        pages_scanned = 0
        raw_messages_scanned = 0
        duplicate_messages = 0
        ignored_after_upper = 0
        omitted_messages = 0
        upper_found = False
        lower_found = False
        scan_limit_hit = False
        crossed_lower_time = False
        upper_visibility_retried = False

        while pages_scanned < self._max_pages:
            request = self._list_request(scope, upper, page_token=page_token)
            response = await self._list_page(request)
            pages_scanned += 1

            data = getattr(response, "data", None)
            items = getattr(data, "items", None)
            if items is None:
                page_items: tuple[Any, ...] = ()
            elif isinstance(items, (list, tuple)):
                page_items = tuple(items)
            else:
                raise MessageHistoryContractError(
                    "飞书群聊历史分页结构无法确认，本条消息未执行。"
                )
            if len(page_items) > self._page_size:
                raise MessageHistoryContractError(
                    "飞书群聊历史分页超过已验证上限，本条消息未执行。"
                )

            for item in page_items:
                if raw_messages_scanned >= self._max_raw_messages:
                    scan_limit_hit = True
                    break
                raw_messages_scanned += 1

                message_id = _required_item_string(item, "message_id")
                if message_id in seen_message_ids:
                    duplicate_messages += 1
                    continue
                seen_message_ids.add(message_id)

                in_scope = _validate_list_item_scope(scope, item)
                if not upper_found:
                    if message_id != upper.message_id:
                        ignored_after_upper += 1
                        continue
                    if not in_scope:
                        raise MessageHistoryContractError(
                            "飞书返回的当前消息不属于目标群聊范围，"
                            "本条消息未执行。"
                        )
                    _validate_list_endpoint(item, upper)
                    upper_found = True
                    continue

                if message_id == lower.message_id:
                    if not in_scope:
                        raise MessageHistoryContractError(
                            "飞书返回的历史边界不属于目标群聊范围，"
                            "本条消息未执行。"
                        )
                    _validate_list_endpoint(item, lower)
                    lower_found = True
                    break

                if not in_scope:
                    omitted_messages += 1
                    continue

                reference = _candidate_reference(item)
                if reference is None:
                    omitted_messages += 1
                    continue
                if reference.create_time_ms > upper.create_time_ms:
                    raise MessageHistoryContractError(
                        "飞书群聊历史没有保持已请求的倒序，"
                        "本条消息未执行。"
                    )
                if reference.create_time_ms < lower.create_time_ms:
                    crossed_lower_time = True
                    break
                candidates_descending.append(reference)

            if lower_found or crossed_lower_time:
                break
            if scan_limit_hit:
                break

            has_more = getattr(data, "has_more", None)
            if has_more is None:
                has_more = False
            if not isinstance(has_more, bool):
                raise MessageHistoryContractError(
                    "飞书群聊历史分页标记无法确认，本条消息未执行。"
                )
            if not has_more:
                if (
                    not upper_found
                    and not upper_visibility_retried
                    and pages_scanned < self._max_pages
                    and raw_messages_scanned < self._max_raw_messages
                ):
                    # Exact ``aget`` already proved that upper exists.  Start
                    # one fresh list snapshot to cover bounded eventual
                    # visibility, while retaining the same total page/raw/time
                    # budgets. Advisory duplicate/after-upper counters also
                    # describe both bounded snapshots rather than one page
                    # chain. Message-ID deduplication restarts with the new
                    # snapshot: the first snapshot may already have exposed
                    # lower or real interval messages before upper became
                    # visible, and those IDs must remain usable after retry.
                    upper_visibility_retried = True
                    page_token = None
                    seen_page_tokens.clear()
                    seen_message_ids.clear()
                    continue
                break
            next_token = _optional_string(getattr(data, "page_token", None))
            if next_token is None or next_token in seen_page_tokens:
                raise MessageHistoryContractError(
                    "飞书群聊历史分页游标无法安全推进，本条消息未执行。"
                )
            seen_page_tokens.add(next_token)
            page_token = next_token

        if not upper_found:
            raise MessageHistoryUnavailable(
                "当前消息暂未出现在飞书群聊历史中，本条消息未执行；请重试。"
            )

        if (
            not lower_found
            and raw_messages_scanned >= self._max_raw_messages
        ):
            scan_limit_hit = True
        if not lower_found and pages_scanned >= self._max_pages:
            scan_limit_hit = True
        truncated_before = not lower_found
        stats = MessageHistoryStats(
            pages_scanned=pages_scanned,
            raw_messages_scanned=raw_messages_scanned,
            duplicate_messages=duplicate_messages,
            ignored_after_upper=ignored_after_upper,
            omitted_messages=omitted_messages,
            truncated_before=truncated_before,
            scan_limit_hit=scan_limit_hit,
        )
        return MessageHistoryWindow(
            lower=lower,
            upper=upper,
            candidates=tuple(reversed(candidates_descending)),
            stats=stats,
        )

    def _list_request(
        self,
        scope: FeishuScope,
        upper: MessageContextAnchor,
        *,
        page_token: str | None,
    ) -> ListMessageRequest:
        if scope.kind is ScopeKind.TOPIC:
            assert scope.topic_id is not None
            builder = (
                ListMessageRequest.builder()
                .container_id_type("thread")
                .container_id(scope.topic_id)
                .sort_type(_SORT_DESCENDING)
                .page_size(self._page_size)
                .with_sender_name(True)
            )
        else:
            # ``end_time`` is seconds.  Move to the following second even
            # when upper is exactly on a second boundary, then seek the exact
            # upper ID locally.  This intentionally over-reads rather than
            # risking exclusion of upper.
            safe_end_second = upper.create_time_ms // 1_000 + 1
            builder = (
                ListMessageRequest.builder()
                .container_id_type("chat")
                .container_id(scope.chat_id)
                .end_time(str(safe_end_second))
                .sort_type(_SORT_DESCENDING)
                .page_size(self._page_size)
                .with_sender_name(True)
            )
        if page_token is not None:
            builder = builder.page_token(page_token)
        return builder.build()

    async def _list_page(self, request: ListMessageRequest) -> Any:
        try:
            async with asyncio.timeout(self._page_timeout_seconds):
                response = await self._client.im.v1.message.alist(request)
        except TimeoutError as exc:
            raise MessageHistoryUnavailable(
                "读取群聊历史分页超时，本条消息未执行；请重试。"
            ) from exc
        except Exception as exc:
            raise MessageHistoryUnavailable(
                "无法读取群聊历史分页，本条消息未执行；请重试。"
            ) from exc
        _require_success(response, operation="读取群聊历史分页")
        if getattr(response, "data", None) is None:
            raise MessageHistoryContractError(
                "飞书没有返回群聊历史分页数据，本条消息未执行。"
            )
        return response


def _require_history_scope(scope: FeishuScope) -> None:
    if not isinstance(scope, FeishuScope):
        raise ValueError("scope must be a FeishuScope")
    if scope.kind not in (ScopeKind.GROUP, ScopeKind.TOPIC):
        raise MessageHistoryContractError(
            "私聊不支持补充群聊上下文，本条消息未执行。"
        )


def _require_success(response: Any, *, operation: str) -> None:
    success = getattr(response, "success", None)
    try:
        succeeded = success() if callable(success) else None
    except Exception as exc:
        raise MessageHistoryContractError(
            f"{operation}的飞书响应无法确认，本条消息未执行。"
        ) from exc
    if succeeded is not True:
        raise MessageHistoryUnavailable(
            f"{operation}失败，本条消息未执行；请重试。"
        )


def _anchor_from_exact_item(
    scope: FeishuScope,
    item: Any,
    *,
    expected_id: str,
) -> MessageContextAnchor:
    message_id = _required_item_string(item, "message_id")
    if message_id != expected_id:
        raise MessageHistoryContractError(
            "飞书返回了错误的群聊上下文消息，本条消息未执行。"
        )
    chat_id = _required_item_string(item, "chat_id")
    if chat_id != scope.chat_id:
        raise MessageHistoryUnavailable(
            "群聊上下文消息不属于当前会话，本条消息未执行。"
        )
    thread_id = _optional_string(getattr(item, "thread_id", None))
    if scope.kind is ScopeKind.GROUP and thread_id is not None:
        raise MessageHistoryUnavailable(
            "群聊上下文消息不属于当前群聊主线，本条消息未执行。"
        )
    if scope.kind is ScopeKind.TOPIC and thread_id != scope.topic_id:
        raise MessageHistoryUnavailable(
            "群聊上下文消息不属于当前话题，本条消息未执行。"
        )
    return MessageContextAnchor(
        message_id=message_id,
        create_time_ms=_positive_timestamp(getattr(item, "create_time", None)),
    )


def _validate_list_item_scope(scope: FeishuScope, item: Any) -> bool:
    chat_id = _required_item_string(item, "chat_id")
    if chat_id != scope.chat_id:
        raise MessageHistoryContractError(
            "飞书群聊历史包含其他会话的消息，本条消息未执行。"
        )
    thread_id = _optional_string(getattr(item, "thread_id", None))
    if scope.kind is ScopeKind.TOPIC:
        if thread_id != scope.topic_id:
            raise MessageHistoryContractError(
                "飞书话题历史包含其他话题的消息，本条消息未执行。"
            )
        return True
    return thread_id is None


def _validate_list_endpoint(item: Any, anchor: MessageContextAnchor) -> None:
    if _positive_timestamp(getattr(item, "create_time", None)) != (
        anchor.create_time_ms
    ):
        raise MessageHistoryContractError(
            "飞书群聊历史端点元数据发生冲突，本条消息未执行。"
        )


def _candidate_reference(item: Any) -> MessageHistoryRef | None:
    if getattr(item, "deleted", None) is True:
        return None
    message_type = _optional_string(getattr(item, "msg_type", None)) or "unknown"
    if message_type == "system":
        return None
    sender = getattr(item, "sender", None)
    if sender is None or _optional_string(getattr(sender, "sender_type", None)) != "user":
        return None
    if _optional_string(getattr(sender, "id_type", None)) != "open_id":
        return None
    sender_id = _optional_string(getattr(sender, "id", None))
    if sender_id is None:
        return None
    return MessageHistoryRef(
        message_id=_required_item_string(item, "message_id"),
        create_time_ms=_positive_timestamp(getattr(item, "create_time", None)),
        sender_id=sender_id,
        message_type=message_type,
    )


def _positive_timestamp(value: Any) -> int:
    if isinstance(value, bool):
        raise MessageHistoryContractError(
            "飞书群聊历史缺少有效时间，本条消息未执行。"
        )
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.isdigit():
        result = int(value)
    else:
        raise MessageHistoryContractError(
            "飞书群聊历史缺少有效时间，本条消息未执行。"
        )
    if result <= 0:
        raise MessageHistoryContractError(
            "飞书群聊历史缺少有效时间，本条消息未执行。"
        )
    return result


def _required_item_string(item: Any, field: str) -> str:
    value = _optional_string(getattr(item, field, None))
    if value is None:
        raise MessageHistoryContractError(
            "飞书群聊历史缺少必要标识，本条消息未执行。"
        )
    return value


def _required_string(value: Any, field: str) -> str:
    result = _optional_string(value)
    if result is None:
        raise ValueError(f"{field} must not be empty")
    return result


def _optional_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
