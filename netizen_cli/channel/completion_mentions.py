"""Best-effort, single-attempt completion reminders with a fixed reply anchor."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging

from lark_channel import Identity, OutboundText, SendOpts

from ..domain import FeishuScope, ScopeKind
from .ports import ReplyChannel
from .topics import TopicPublishError, validate_topic_message

logger = logging.getLogger(__name__)


async def send_completion_mention(
    channel: ReplyChannel,
    *,
    scope: FeishuScope,
    card_message_id: str | None,
    user_id: str,
    operation_id: str,
    source_message_id: str | None = None,
) -> bool:
    """Use the card, or an explicit source when no card exists; never re-anchor."""
    anchor_id = card_message_id if card_message_id is not None else source_message_id
    if not anchor_id:
        return False
    identity = json.dumps(
        [scope.key, operation_id, anchor_id, user_id], ensure_ascii=True,
    )
    opts = SendOpts(
        receive_id_type="chat_id", reply_to=anchor_id,
        reply_in_thread=scope.kind is ScopeKind.TOPIC, reply_target_gone="fail",
        uuid="completion-" + hashlib.sha256(identity.encode()).hexdigest()[:32],
    )
    try:
        async with asyncio.timeout(5):
            result = await channel.send(
                scope.chat_id,
                OutboundText(text="本轮任务已结束。", mentions=[Identity(open_id=user_id)]),
                opts,
            )
        sent = validate_topic_message(result, scope.chat_id)
        if (
            not sent.root_id or not sent.parent_id
            or sent.thread_id != scope.topic_id
            or (scope.kind is not ScopeKind.TOPIC and sent.parent_id != anchor_id)
        ):
            raise TopicPublishError("completion reminder reply was not confirmed", unknown=True)
        return True
    except asyncio.CancelledError:
        raise
    except Exception:
        # The request may already have notified the user. Do not retry or
        # trigger result recovery; notification delivery never changes execution.
        logger.warning(
            "completion reminder reply was not confirmed; not retrying",
            extra={"message_id": anchor_id, "operation_id": operation_id},
        )
        return False
