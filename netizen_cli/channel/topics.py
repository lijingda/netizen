"""Public Channel sends for fresh topics, with bounded same-UUID reconciliation."""

from __future__ import annotations

import logging

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid5

from lark_channel import OutboundCard, OutboundConfig, OutboundPost, SendOpts, SendResult
from lark_channel.channel.outbound import markdown_to_post_ast, split_with_code_fences

from ..external_calls import external_call
from ..domain import FeishuScope

from .messages import _confirmed_reply_id, _nonempty_field, _object_field, _send_result_error_code
from .ports import ReplyChannel


logger = logging.getLogger(__name__)


class TopicPublishError(RuntimeError):
    def __init__(self, message: str, *, unknown: bool = False) -> None:
        super().__init__(message)
        self.unknown = unknown


def receipt(result: SendResult | Exception | None) -> Literal["confirmed", "rejected", "unknown"]:
    """Classify the evidence from one Feishu write, including a transport exception."""
    if isinstance(result, Exception):
        return "rejected" if isinstance(result, TopicPublishError) and not result.unknown else "unknown"
    code = _send_result_error_code(result)
    if result is not None and result.success and code in (None, 0):
        return "confirmed"
    return "rejected" if code is not None and code > 0 else "unknown"


@dataclass(frozen=True, slots=True)
class TopicMessage:
    message_id: str
    chat_id: str
    thread_id: str | None
    root_id: str | None
    parent_id: str | None
    last_chunk_id: str | None = None

    @property
    def is_main_line(self) -> bool:
        return self.thread_id is None and self.parent_id is None and self.root_id in {None, self.last_chunk_id or self.message_id}

    def matches_location(self, scope: FeishuScope, root: str | None, *, parent: str | None = None,
                         partial: bool = False) -> bool:
        if self.chat_id != scope.chat_id:
            return False
        if parent is None:
            if scope.topic_id is None:
                return self.is_main_line
            return ((self.thread_id == scope.topic_id or (partial and not self.thread_id))
                    and self.root_id in (None, root) and self.parent_id in (None, root))
        # An anchored reply may have only partial location evidence.
        return (self.thread_id in (None, scope.topic_id) and self.parent_id in (None, parent)
                and (root is None or self.root_id in (None, root)))


@dataclass(slots=True)
class BindingCardLocation:
    owners: dict[str, CardReplyLocation]
    root: str | None = None
    unusable: set[str] = field(default_factory=set)
    retired: bool = False
    release_cards: Callable[[set[str]], None] = lambda _: None

    def retire(self) -> None:
        self.retired = True
        for owner in set(self.owners.values()):
            if owner.binding is self:
                owner.retire()
        self.root = None
        self.unusable.clear()


@dataclass(slots=True, eq=False)
class CardReplyLocation:
    """Receipt evidence and exclusive card writes for one Feishu reply anchor."""

    scope: FeishuScope
    parent: str | None
    root: str | None
    binding: BindingCardLocation
    retired: bool = False
    cards: set[str] = field(default_factory=set)

    def retire(self) -> None:
        self.retired = True
        owned = {mid for mid in self.cards if self.binding.owners.get(mid) is self}
        self.binding.release_cards(owned)
        for mid in owned:
            del self.binding.owners[mid]
        self.cards.clear()

    def usable(self, message_id: str) -> bool:
        return not self.retired and not self.binding.retired and self.accepts_message_id(message_id)

    def can_write(self, message_id: str) -> bool:
        return self.usable(message_id) and self.binding.owners.get(message_id) is self

    def _receipt_location(self, result: SendResult, updated_id: str | None) -> tuple[TopicMessage, set[str]]:
        data = _object_field(result.raw, "data")
        nested = _nonempty_field(data, "message_id")
        mid = updated_id or result.message_id or nested or ""
        sent = TopicMessage(mid, _nonempty_field(data, "chat_id") or self.scope.chat_id,
                            _nonempty_field(data, "thread_id"), _nonempty_field(data, "root_id"),
                            _nonempty_field(data, "parent_id"))
        ids = {value for value in (updated_id, result.message_id, nested, *(result.chunk_ids or ())) if value}
        return sent, ids

    def _disqualify(self, ids: set[str]) -> SendResult:
        self.binding.unusable.update(ids)
        self.binding.release_cards({mid for mid in ids
                                    if (owner := self.binding.owners.get(mid)) and owner.binding is self.binding})
        return SendResult(success=False)

    def accepts_message_id(self, mid: str) -> bool:
        owner = self.binding.owners.get(mid)
        return mid not in self.binding.unusable and (owner is None or owner is self)

    def validate_page_receipt(self, result: SendResult, updated_id: str) -> SendResult:
        """Verify an issued replacement using only the destination evidence we have."""
        sent, ids = self._receipt_location(result, updated_id)
        root = self.binding.root or self.root
        if (len(ids) != 1 or sent.chat_id != self.scope.chat_id or sent.thread_id not in (None, self.scope.topic_id)
                or (root is not None and sent.root_id not in (None, root))
                or (self.parent is not None and sent.parent_id not in (None, self.parent))):
            if self.binding.retired:
                return SendResult(success=False)
            return self._disqualify(ids)
        if not self.accepts_message_id(updated_id) or (result.success and receipt(result) != "confirmed"):
            return SendResult(success=False)
        return result

    def validate(self, result: SendResult, *, card: bool = False, updated_id: str | None = None,
                 reply_to: str | None = None) -> SendResult:
        if self.retired or self.binding.retired:
            return SendResult(success=False)
        sent, ids = self._receipt_location(result, updated_id)
        root = self.binding.root or self.root
        if (not sent.matches_location(self.scope, root, parent=reply_to or self.parent, partial=True)
                or (card and self.scope.topic_id and sent.root_id and not root)
                or (card and len(ids) > 1)):
            return self._disqualify(ids)
        if any(not self.usable(value) for value in ids):
            return SendResult(success=False)
        if card and self.parent and (confirmed := _confirmed_reply_id(result)):
            self.binding.owners[confirmed] = self
            self.cards.add(confirmed)
        return result


async def complete_fresh_topic(
    channel: ReplyChannel,
    root: TopicMessage,
    *,
    seed_content: Any,
    seed_uuid: str,
    before_seed: Callable[[], None] | None = None,
) -> TopicMessage:
    """Validate a fresh root and promote it once when Feishu needs a seed.

    The caller retains the root even if promotion fails, so its existing
    ownership and partial-publication handling remain operation-specific.
    """
    if root.parent_id is not None or root.root_id not in {None, root.message_id}:
        raise TopicPublishError("飞书未返回全新的话题根消息。", unknown=True)
    if root.thread_id is not None:
        return root
    if before_seed is not None:
        before_seed()
    origin = await send_topic_message(
        channel, root.chat_id, seed_content,
        SendOpts(receive_id_type="chat_id", reply_to=root.message_id,
                 reply_in_thread=True, reply_target_gone="fail", uuid=seed_uuid),
    )
    if (
        origin.thread_id is None or origin.root_id != root.message_id
        or origin.parent_id != root.message_id
    ):
        raise TopicPublishError("飞书未确认种子消息与新话题的关系。", unknown=True)
    return origin


async def send_conversation_message(channel: ReplyChannel, scope: FeishuScope, root: str | None,
                                    message: str | OutboundCard, delivery_id: str,
                                    publishable: Callable[[], bool] = lambda: True, *, single_attempt: bool = False,
                                    location: CardReplyLocation | None = None) -> SendResult:
    # The SDK assigns random UUIDs to trailing chunks in a single send. Use its
    # public Markdown splitter and give each Feishu request its own stable UUID.
    config = getattr(channel, "config", None)
    outbound = config.outbound if config is not None else OutboundConfig()
    parts: list[str] | list[OutboundCard] = (split_with_code_fences(message, outbound.text_chunk_limit)
                                              if isinstance(message, str) else [message])
    message_id = None
    for index, part in enumerate(parts):
        content: str | OutboundCard | OutboundPost = part
        if isinstance(part, str) and (len(parts) > 1 or len(part) > outbound.text_chunk_limit):
            converter = outbound.markdown_converter
            content = OutboundPost(post=markdown_to_post_ast(
                part, table_mode=converter.table_mode if converter.enabled else "off",
                tag_md_mode=converter.tag_md_mode if converter.enabled else "structured"))
        opts = SendOpts(receive_id_type="chat_id", reply_to=root, reply_in_thread=bool(root), reply_target_gone="fail",
                        uuid=str(uuid5(NAMESPACE_URL, f"{scope.key}/{delivery_id}/{index}")))
        sent = await send_topic_message(channel, scope.chat_id, content, opts, allow_chunks=True, publishable=publishable,
                                        isolate_child_cancellation=True, single_attempt=single_attempt, location=location)
        if not sent.matches_location(scope, root):
            raise TopicPublishError("Claude 会话消息的飞书目标未确认。", unknown=True)
        message_id = message_id or sent.message_id
    return SendResult.ok(message_id=message_id)


async def send_topic_message(channel: ReplyChannel, chat_id: str, content: Any, opts: SendOpts, *,
                             allow_chunks: bool = False, isolate_child_cancellation: bool = False,
                             single_attempt: bool = False, location: CardReplyLocation | None = None,
                             publishable: Callable[[], bool] = lambda: True) -> TopicMessage:
    if not opts.uuid:
        raise TopicPublishError("话题消息缺少确定性发送 UUID。")
    uncertain = False
    for attempt in range(2):
        if not publishable():
            raise TopicPublishError("本轮投递已取消。")
        try:
            result = await external_call(channel.send(chat_id, content, opts), isolate=isolate_child_cancellation)
        except Exception as error:
            if single_attempt:
                raise TopicPublishError("飞书发送结果未确认。", unknown=receipt(error) == "unknown") from error
            logger.warning(
                "topic message send failed",
                extra={
                    "chat_id": chat_id,
                    "message_uuid": opts.uuid,
                    "attempt": attempt + 1,
                    "error_type": type(error).__name__,
                },
            )
            uncertain = True
            if attempt == 0:
                continue
            raise TopicPublishError("话题发布结果未确认；同 UUID 有界对账也未成功。", unknown=True) from error
        if location is not None:
            result = location.validate(result, card=isinstance(content, OutboundCard))
        if not single_attempt and not result.success and result.error is not None and result.error.retryable:
            uncertain = True
            if attempt == 0:
                continue
            raise TopicPublishError("话题发布结果未确认；同 UUID 对账仍返回可重试失败。", unknown=True)
        if single_attempt and (outcome := receipt(result)) != "confirmed":
            raise TopicPublishError("飞书未确认内容送达。", unknown=outcome == "unknown")
        try:
            return validate_topic_message(result, chat_id, allow_chunks=allow_chunks)
        except TopicPublishError as error:
            if uncertain and not error.unknown:
                raise TopicPublishError(str(error), unknown=True) from error
            raise
    raise AssertionError("unreachable send budget")


def validate_topic_message(result: SendResult, chat_id: str, *, allow_chunks: bool = False) -> TopicMessage:
    raw = result.raw
    code = _object_field(raw, "code")
    if code == 230071:
        raise TopicPublishError("当前飞书会话不支持创建话题（230071）。")
    if not result.success:
        raise TopicPublishError(f"飞书未确认消息发送成功（code={code!r}）。")
    data = _object_field(raw, "data")
    message_id = result.message_id or None
    # A split SDK send reports the first ID on the logical result and the last ID in raw.
    chunks = result.chunk_ids
    if chunks and (not allow_chunks or chunks[0] != message_id or not all(chunks) or len(set(chunks)) != len(chunks)):
        raise TopicPublishError("飞书消息分段标识不一致。", unknown=True)
    if (
        code != 0 or data is None or message_id is None
        or _nonempty_field(data, "message_id") != (chunks[-1] if chunks else message_id)
        or _nonempty_field(data, "chat_id") != chat_id
    ):
        raise TopicPublishError("飞书消息响应缺少一致的消息和聊天标识。", unknown=True)
    root = _nonempty_field(data, "root_id")
    thread, parent = _nonempty_field(data, "thread_id"), _nonempty_field(data, "parent_id")
    return TopicMessage(message_id, chat_id, thread, root, parent, chunks[-1] if chunks else None)
