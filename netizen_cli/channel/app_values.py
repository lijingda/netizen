"""Pure readers for inbound messages, card replies and outcomes shared by Channel layers."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from lark_channel import (
    SendResult,
)

from ..cards import (
    TURN_FILE_ACTION_VERSION,
)
from .messages import _nonempty_field, _object_field, _send_result_error_code
from .ports import ReplyChannel
from ..completion_mention import valid_completion_mention_user_id
from ..runtime.contracts import (
    GoalActivitySnapshot,
    SideTurnOutcome,
    TurnActivitySnapshot,
    TurnOutcome,
)
from ..domain import (
    ActiveState,
    FeishuScope,
    GoalOperationState,
    ScheduledBindingOrigin,
    ScheduledOrigin,
    SessionAgentOrigin,
    ScopeKind,
    TurnFileActionIntent,
)
from ..error_messages import describe_error, native_turn_failure
from ..turn_files import (
    TurnFileError,
)

from . import constants


@dataclass(frozen=True, slots=True)
class _GoalReplyResult:
    """Presentation-only view of the verified final physical Turn."""

    status: str | None
    final_response: str | None
    items: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class _CardReplyConversation:
    thread_id: str | None
    chat_type: str = "unknown"


@dataclass(frozen=True, slots=True)
class _CardReplyTarget:
    id: str
    message_id: str
    chat_id: str
    conversation: _CardReplyConversation


@dataclass(frozen=True, slots=True)
class _CardCommandOrigin(_CardReplyTarget):
    """A real card callback reply anchor, never a fabricated prompt message."""


@dataclass(slots=True)
class _CardCallbackContext:
    """Keep the verified location even when later form decoding fails."""

    scope: FeishuScope | None = None


@dataclass(frozen=True, slots=True)
class _SentMessage:
    message_id: str
    chat_id: str
    thread_id: str | None
    root_id: str | None
    parent_id: str | None


@dataclass(slots=True)
class _ScheduledCompletion:
    partial_answers_confirmed: bool = True
    delivery_state: str | None = None


@dataclass(slots=True)
class _RenameNameGeneration:
    """Only an in-flight presentation; no saved draft or card session."""

    task: asyncio.Task[None] | None = None
    invalidated: bool = False
    update_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _admin_url_is_loopback(url: str) -> bool:
    host = urlsplit(url).hostname
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def _side_send_uuid(prefix: str, side_id: str) -> str:
    digest = hashlib.sha256(side_id.encode("utf-8")).hexdigest()[:32]
    return prefix + digest


def _turn_file_action_uuid(
    prefix: str,
    intent: TurnFileActionIntent,
) -> str:
    identity = "\0".join(
        (
            intent.scope.key,
            intent.source_id,
            intent.sender_id,
            intent.name.value,
            intent.binding_id,
            intent.turn_id,
            str(TURN_FILE_ACTION_VERSION),
            intent.path or "",
            "" if intent.page is None else str(intent.page),
        )
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    return prefix + digest


def _validate_turn_file_reply(
    result: SendResult,
    *,
    intent: TurnFileActionIntent,
) -> _SentMessage:
    raw = result.raw
    code = _object_field(raw, "code")
    if code == 230071:
        raise TurnFileError(
            "当前飞书会话不支持将本卡片转为话题（230071），文件未发送。"
        )
    if not result.success:
        raise TurnFileError(
            f"飞书未确认本轮文件发送成功（code={code!r}）。"
        )
    if result.chunk_ids:
        raise TurnFileError("本轮文件消息被意外拆分，无法确认话题关系。")
    if code != 0:
        raise TurnFileError(f"飞书本轮文件响应 code 异常：{code!r}。")
    data = _object_field(raw, "data")
    if data is None:
        raise TurnFileError("飞书本轮文件响应缺少 data。")
    message_id = _nonempty_field(result, "message_id")
    if message_id is None or _nonempty_field(data, "message_id") != message_id:
        raise TurnFileError("飞书本轮文件消息标识不一致。")
    chat_id = _nonempty_field(data, "chat_id")
    if chat_id != intent.scope.chat_id:
        raise TurnFileError("飞书把本轮文件发送到了其他聊天。")
    sent = _SentMessage(
        message_id=message_id,
        chat_id=chat_id,
        thread_id=_nonempty_field(data, "thread_id"),
        root_id=_nonempty_field(data, "root_id"),
        parent_id=_nonempty_field(data, "parent_id"),
    )
    if intent.scope.kind is ScopeKind.TOPIC:
        if (
            sent.thread_id != intent.scope.topic_id
            or sent.root_id is None
            or sent.parent_id is None
        ):
            raise TurnFileError("飞书未确认文件消息留在原话题中。")
    elif (
        sent.thread_id is None
        or sent.root_id is None
        or sent.parent_id != intent.source_id
    ):
        raise TurnFileError("飞书未确认本轮文件卡片已转为话题。")
    return sent


def _side_initial_question_echo(text: str) -> str:
    safe_text = constants.FEISHU_AT_TAG_START.sub("‹", text)
    if len(safe_text) <= constants.SIDE_INITIAL_QUESTION_MAX_CHARS:
        label = "首轮问题（来自 /side 发起消息）"
        visible = safe_text
    else:
        label = "首轮问题（来自 /side 发起消息，内容节选）"
        visible = (
            safe_text[: constants.SIDE_INITIAL_QUESTION_MAX_CHARS - 1].rstrip() + "…"
        )
    return f"{label}\n\n{visible}"


def _side_reply_target(
    message: _SentMessage,
    topic_id: str,
) -> _CardReplyTarget:
    return _CardReplyTarget(
        id=message.message_id,
        message_id=message.message_id,
        chat_id=message.chat_id,
        conversation=_CardReplyConversation(thread_id=topic_id),
    )


def _is_feishu_card_action_lock(result: SendResult) -> bool:
    if result.success:
        return False
    error = result.error
    if getattr(error, "raw_code", None) != constants.FEISHU_CARD_ACTION_LOCK_OUTER_CODE:
        return False
    hint = getattr(error, "hint", None)
    if not isinstance(hint, str):
        return False
    return (
        re.search(
            rf"\bErrCode:\s*{constants.FEISHU_CARD_ACTION_LOCK_INNER_CODE};"
            r"\s*ErrMsg:\s*card action is lock(?:;|$)",
            hint,
        )
        is not None
    )


def _reply_failure_notice(result: SendResult) -> str | None:
    if result.success:
        return None
    code = _send_result_error_code(result)
    if code != constants.FEISHU_CONTENT_AUDIT_REJECTION_CODE:
        return None
    raw = result.raw
    message = _object_field(raw, "msg")
    if not isinstance(message, str):
        error = result.error
        message = _object_field(error, "hint")
    normalized = message.upper() if isinstance(message, str) else ""
    for reason, label in constants.FEISHU_AUDIT_REASON_LABELS.items():
        if reason in normalized:
            return (
                "消息发送失败：飞书内容审核认为回复中包含"
                f"{label}。（错误码 {code}）"
            )
    return f"消息发送失败：回复内容未通过飞书审核。（错误码 {code}）"


def _message_chat_type(message: Any) -> str:
    conversation = getattr(message, "conversation", None)
    return str(
        getattr(message, "chat_type", None)
        or getattr(conversation, "chat_type", "unknown")
    )


def _inbound_root_message_id(message: Any) -> str | None:
    raw = getattr(message, "raw", None)
    if not isinstance(raw, Mapping):
        return None
    value = raw.get("root_id")
    return value if isinstance(value, str) and value else None


def _message_id(message: Any) -> str:
    return str(getattr(message, "message_id", None) or getattr(message, "id", ""))


def _chat_id(message: Any) -> str:
    conversation = getattr(message, "conversation", None)
    return str(
        getattr(message, "chat_id", None)
        or getattr(conversation, "chat_id", "")
    )


def _sender_id(message: Any) -> str:
    sender = getattr(message, "sender", None)
    return str(
        getattr(message, "sender_id", None)
        or getattr(sender, "open_id", "")
    )


def _body_text(message: Any, *, bot_open_id: str | None = None) -> str:
    # Channel SDK's ``body_text`` has already removed the bot mention.  Its
    # empty string is meaningful for a bare @bot and must not fall back to the
    # rendered safe text (which would send "@bot" to Codex).
    body = getattr(message, "body_text", None)
    if body is not None:
        text = str(body)
    else:
        text = str(
            getattr(message, "safe_content_text", None)
            or getattr(message, "content_text", "")
        )
    return _repair_leading_post_bot_mention(
        message,
        text,
        bot_open_id=bot_open_id,
    )


def _channel_bot_open_id(channel: ReplyChannel) -> str | None:
    identity = getattr(channel, "bot_identity", None)
    value = getattr(identity, "open_id", None)
    return str(value) if value else None


def _repair_leading_post_bot_mention(
    message: Any,
    text: str,
    *,
    bot_open_id: str | None,
) -> str:
    """Repair lark-channel-sdk 1.4.0's post mention placeholder mismatch.

    Feishu topic-root posts encode the leading at-node's ``user_id`` as a
    placeholder such as ``@_user_1``.  The pinned SDK tries to remove it by
    comparing that value with the bot open_id, leaving a rendered ``@Bot`` in
    ``body_text``.  Match the public bot identity, normalized mention key and
    first public PostContent AST node before removing only that exact prefix.
    """
    if (
        _raw_content_type(message) != "post"
        or not bool(getattr(message, "mentioned_bot", False))
        or not bot_open_id
    ):
        return text

    bot_mention = next(
        (
            mention
            for mention in (getattr(message, "mentions", None) or [])
            if getattr(mention, "open_id", None) == bot_open_id
        ),
        None,
    )
    mention_key = getattr(bot_mention, "key", None)
    if bot_mention is None or not mention_key:
        return text

    first_node = _first_post_node(message)
    if (
        first_node is None
        or first_node.get("tag") != "at"
        or first_node.get("user_id") not in {mention_key, bot_open_id}
    ):
        return text

    display_name = (
        first_node.get("user_name")
        or getattr(bot_mention, "name", None)
        or first_node.get("user_id")
    )
    prefix = f"@{display_name}"
    if not text.startswith(prefix):
        return text
    return text[len(prefix):].lstrip()


def _first_post_node(message: Any) -> dict[str, Any] | None:
    content = getattr(message, "content", None)
    post = getattr(content, "post", None)
    if not isinstance(post, dict) or not post:
        return None
    documents = (
        [post]
        if "content" in post or "content_v2" in post
        else [value for value in post.values() if isinstance(value, dict)]
    )
    if not documents:
        return None
    document = documents[0]
    paragraphs = document.get("content_v2")
    if not isinstance(paragraphs, list) or not paragraphs:
        paragraphs = document.get("content")
    if not isinstance(paragraphs, list):
        return None
    for paragraph in paragraphs:
        if not isinstance(paragraph, list):
            continue
        for node in paragraph:
            if isinstance(node, dict):
                return node
    return None


def _task_interruption_text(outcome: TurnOutcome | SideTurnOutcome) -> str:
    if outcome.background_cleanup_requested:
        message = (
            "Codex Turn 已中断；已请求清理该 Thread 中已登记的后台终端。"
            "前台工具进程不受此接口保证，可能仍在运行。"
        )
    else:
        message = (
            "Codex Turn 已中断；本服务未请求清理已登记的后台终端。"
            "前台工具进程可能仍在运行。"
        )
    native_error = getattr(outcome.result, "error", None)
    if native_error is not None:
        message += f"\n中断原因：{describe_error(native_turn_failure(native_error))}"
    return message


def _goal_reply_activity(snapshot: GoalActivitySnapshot) -> TurnActivitySnapshot:
    return TurnActivitySnapshot(
        binding_id=snapshot.binding_id, thread_id=snapshot.thread_id,
        turn_id=snapshot.logical_turn_id, revision=snapshot.revision,
        state=(
            ActiveState.OBSERVATION_UNAVAILABLE if snapshot.state is GoalOperationState.UNKNOWN
            else ActiveState.STOPPING if snapshot.state is GoalOperationState.PAUSING
            else ActiveState.RUNNING
        ),
        steer_count=0, plan_available=snapshot.plan_available,
        plan_generated=snapshot.plan_generated, plan_may_be_stale=False,
        steps=snapshot.steps, commentary=snapshot.commentary,
        operations=snapshot.operations, partial_answers=snapshot.partial_answers,
        progress_card_settings=snapshot.progress_card_settings,
    )


def _task_failure_text(outcome: TurnOutcome | SideTurnOutcome) -> str:
    error = outcome.error
    if error is None:
        native_error = getattr(outcome.result, "error", None)
        error = (
            native_turn_failure(native_error)
            if native_error is not None
            else RuntimeError(
                outcome.final_response or f"Codex Turn 状态为 {outcome.status!r}。"
            )
        )
    message = f"任务未完成：{describe_error(error)}"
    if isinstance(outcome, TurnOutcome) and outcome.status == "failed":
        message += (
            "\n本轮已结束，原会话及上下文仍保留；"
            "可发送“继续”或调整 /config 后重试。"
        )
    return message


def _outcome_completion_mention_user_id(
    outcome: TurnOutcome | SideTurnOutcome,
) -> str | None:
    """Mention the captured task initiator once, only on a known task ending.

    Completion origins may be bot-authored Side seeds or Goal cards. The native
    operation's owner is captured at admission and is unchanged by steer.
    Scheduled initial Turns have an automatic owner, not a human addressee.
    """
    if (
        not outcome.task_feedback.completion_mention_enabled
        or isinstance(outcome.origin, (ScheduledOrigin, ScheduledBindingOrigin, SessionAgentOrigin))
        or outcome.background_cleanup_requested
    ):
        return None
    if isinstance(outcome, SideTurnOutcome) and (
        outcome.error is not None or outcome.stop_requested
    ):
        # Side's public handle.run() failure has no persistent-history proof
        # of a terminal; Runtime retains an unknown Side slot in this case.
        return None
    elif outcome.status == "interrupted" or (
        outcome.error is None and outcome.status not in {"completed", "failed"}
    ):
        return None
    return valid_completion_mention_user_id(outcome.owner_id)


def _raw_content_type(message: Any) -> str:
    value = getattr(message, "raw_content_type", None)
    return str(value or "text")
