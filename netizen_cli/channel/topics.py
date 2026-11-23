"""Public Channel sends for fresh topics, with bounded same-UUID reconciliation."""

from __future__ import annotations

import logging

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from lark_channel import OutboundConfig, OutboundPost, SendOpts, SendResult
from lark_channel.channel.outbound import markdown_to_post_ast, split_with_code_fences

from ..domain import FeishuScope

from .messages import _nonempty_field, _object_field
from .ports import ReplyChannel


logger = logging.getLogger(__name__)


class TopicPublishError(RuntimeError):
    def __init__(self, message: str, *, unknown: bool = False) -> None:
        super().__init__(message)
        self.unknown = unknown


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
                                    markdown: str, delivery_id: str) -> SendResult:
    # The SDK assigns random UUIDs to trailing chunks in a single send. Use its
    # public Markdown splitter and give each Feishu request its own stable UUID.
    config = getattr(channel, "config", None)
    outbound = config.outbound if config is not None else OutboundConfig()
    parts = split_with_code_fences(markdown, outbound.text_chunk_limit)
    message_id = None
    for index, part in enumerate(parts):
        content: str | OutboundPost = part
        if len(parts) > 1 or len(part) > outbound.text_chunk_limit:
            converter = outbound.markdown_converter
            content = OutboundPost(post=markdown_to_post_ast(
                part, table_mode=converter.table_mode if converter.enabled else "off",
                tag_md_mode=converter.tag_md_mode if converter.enabled else "structured"))
        opts = SendOpts(receive_id_type="chat_id", reply_to=root, reply_in_thread=bool(root), reply_target_gone="fail",
                        uuid=str(uuid5(NAMESPACE_URL, f"{scope.key}/{delivery_id}/{index}")))
        sent = await send_topic_message(channel, scope.chat_id, content, opts, allow_chunks=True)
        confirmed = (sent.thread_id == scope.topic_id and sent.root_id in {None, root}) if scope.topic_id else sent.is_main_line
        if not confirmed:
            raise TopicPublishError("Claude 会话消息的飞书目标未确认。", unknown=True)
        message_id = message_id or sent.message_id
    return SendResult.ok(message_id=message_id)


async def send_topic_message(channel: ReplyChannel, chat_id: str, content: Any, opts: SendOpts, *,
                             allow_chunks: bool = False) -> TopicMessage:
    if not opts.uuid:
        raise TopicPublishError("话题消息缺少确定性发送 UUID。")
    uncertain = False
    for attempt in range(2):
        try:
            result = await channel.send(chat_id, content, opts)
        except Exception as error:
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
        if not result.success and result.error is not None and result.error.retryable:
            uncertain = True
            if attempt == 0:
                continue
            raise TopicPublishError("话题发布结果未确认；同 UUID 对账仍返回可重试失败。", unknown=True)
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
