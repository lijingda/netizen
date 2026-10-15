"""Public Channel sends for fresh topics, with bounded same-UUID reconciliation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from lark_channel import SendOpts

from .messages import _nonempty_field, _object_field
from .ports import ReplyChannel


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


async def send_topic_message(channel: ReplyChannel, chat_id: str, content: Any, opts: SendOpts) -> TopicMessage:
    if not opts.uuid:
        raise TopicPublishError("话题消息缺少确定性发送 UUID。")
    uncertain = False
    for attempt in range(2):
        try:
            result = await channel.send(chat_id, content, opts)
        except Exception as error:
            uncertain = True
            if attempt == 0:
                continue
            raise TopicPublishError("话题发布结果未确认；同 UUID 有界对账也未成功。", unknown=True) from error
        if getattr(result, "success", None) is not True and getattr(getattr(result, "error", None), "retryable", None) is True:
            uncertain = True
            if attempt == 0:
                continue
            raise TopicPublishError("话题发布结果未确认；同 UUID 对账仍返回可重试失败。", unknown=True)
        try:
            return validate_topic_message(result, chat_id)
        except TopicPublishError as error:
            if uncertain and not error.unknown:
                raise TopicPublishError(str(error), unknown=True) from error
            raise
    raise AssertionError("unreachable send budget")


def validate_topic_message(result: object, chat_id: str) -> TopicMessage:
    raw = getattr(result, "raw", None)
    code = _object_field(raw, "code")
    if code == 230071:
        raise TopicPublishError("当前飞书会话不支持创建话题（230071）。")
    if getattr(result, "success", None) is not True:
        raise TopicPublishError(f"飞书未确认消息发送成功（code={code!r}）。")
    data = _object_field(raw, "data")
    message_id = _nonempty_field(result, "message_id")
    if (
        code != 0 or getattr(result, "chunk_ids", None) or data is None
        or message_id is None or _nonempty_field(data, "message_id") != message_id
        or _nonempty_field(data, "chat_id") != chat_id
    ):
        raise TopicPublishError("飞书消息响应缺少一致的 exact 消息和聊天标识。", unknown=True)
    return TopicMessage(message_id, chat_id, _nonempty_field(data, "thread_id"), _nonempty_field(data, "root_id"), _nonempty_field(data, "parent_id"))
