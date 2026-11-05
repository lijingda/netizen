"""Native question cards and answers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any

from lark_channel import (
    SendOpts,
)

from .base import CHANNEL_LOGGER_NAME
from ..bindings import (
    BindingNotFound,
    SideTopicNotFound,
    SideTopicRecord,
    SideTopicState,
    ThreadBinding,
)
from ..cards import (
    CardActionError,
    error_card,
    scope_from_fetched_card,
)
from .messages import _nonempty_field, _object_field, public_chat_kind as _public_chat_kind
from .question_inputs import CardAnswerConversation, CardAnswerOrigin, card_answer_sender
from .topics import TopicPublishError, send_topic_message
from ..cards.questions import (
    QuestionAnswer,
    decode_question_answer,
    decode_question_context,
    render_question_card,
    render_question_context_card,
    render_question_receipt_card,
    render_question_submission_card,
)
from ..user_questions import (
    BindingQuestionTarget,
    QuestionRequest,
    QuestionTarget,
    SideQuestionTarget,
    question_target_payload,
)
from ..completion_mention import valid_completion_mention_user_id
from ..runtime.contracts import (
    ContextBoundaryCommitFailed,
    SideSessionNotFound,
    SideSubmissionAdmission,
    SubmissionAdmission,
)
from ..domain import (
    FeishuScope,
    ScopeKind,
)
from ..error_messages import describe_error
from ..prompt_projection import (
    CardAnswerProjection,
)
from ..skill_references import parse_skill_references

from . import constants
from .app_values import (
    _CardReplyConversation,
    _CardReplyTarget,
    _message_id,
)
from .schedules import ScheduleChannel


logger = logging.getLogger(CHANNEL_LOGGER_NAME)


class QuestionChannel(ScheduleChannel):
    """Native question cards and answers."""

    def _question_recipient(
        self, target: QuestionTarget,
    ) -> tuple[FeishuScope, ThreadBinding | SideTopicRecord]:
        """Resolve an existing target without following a Parent or current pointer."""
        if isinstance(target, BindingQuestionTarget):
            binding = self._bindings.get(target.binding_id)
            record = self._bindings.get_scope(binding.scope_key)
            return FeishuScope(record.app_id, record.chat_id, record.kind, record.topic_id), binding
        if isinstance(target, SideQuestionTarget):
            side = self._bindings.get_side_topic(target.side_id)
            if side.state is not SideTopicState.OPEN:
                raise CardActionError(
                    "问题所属 Side 尚未就绪或已结束、过期，本条回答未执行；不会转投主会话。"
                    "请在主会话查看 /sessions；旧 Side 已结束或过期时，需重新发起 /side。"
                )
            return self._side_scope(side), side
        raise CardActionError("问题的回答目标无效。")

    async def handle_questions(
        self, target: QuestionTarget, origin: object, request: QuestionRequest,
        owner_id: str = "",
    ) -> None:
        """Project questions for either target without changing the producing Turn."""
        scope, _ = self._question_recipient(target)
        if scope.app_id != self._app_id:
            return
        reply_to = _message_id(origin)
        if not reply_to:
            return
        for index in range(len(request.questions)):
            identity = json.dumps([scope.key, question_target_payload(target), request.item_id, index])
            async with asyncio.timeout(15):
                sent = await send_topic_message(
                    self._channel, scope.chat_id,
                    render_question_card(
                        target, request, index,
                        mention_user_id=(
                            valid_completion_mention_user_id(owner_id)
                            if self._management.question_mention_enabled else None
                        ),
                    ),
                    SendOpts(
                        receive_id_type="chat_id", reply_to=reply_to,
                        reply_in_thread=scope.kind is ScopeKind.TOPIC,
                        reply_target_gone="fail",
                        uuid="question-" + hashlib.sha256(identity.encode()).hexdigest()[:32],
                    ),
                )
            if sent.thread_id != scope.topic_id:
                raise TopicPublishError("问题卡片的飞书位置未确认。", unknown=True)

    async def _handle_question_card_action(self, event: Any) -> None:
        """Share answer handling; only admission and submission depend on the target."""
        action = getattr(event, "action", None)
        value, form = getattr(action, "value", None), getattr(action, "form_value", None)
        message_id = str(getattr(event, "message_id", "") or "")
        chat_id = str(getattr(event, "chat_id", "") or "")
        context = None
        scope = None
        try:
            context = decode_question_context(value)
            answer = decode_question_answer(value, form)
            context = answer
            operator = getattr(event, "operator", None)
            if not message_id or not chat_id or not getattr(operator, "open_id", None):
                raise CardActionError(
                    "卡片回调缺少原消息或回答者身份，本次回答尚未交给 Codex。无需修改答案；"
                    "请在原会话直接发送回答，或联系管理员检查卡片回调配置。"
                )
            async with asyncio.timeout(30):
                fetched = await self._channel.fetch_message(message_id)
                data = _object_field(fetched, "data")
                items = _object_field(data, "items")
                if (
                    _object_field(fetched, "code") != 0 or not isinstance(items, list)
                    or len(items) != 1 or _nonempty_field(items[0], "message_id") != message_id
                ):
                    raise CardActionError(
                        "无法核验原问题卡片，本次回答尚未交给 Codex。无需修改答案；"
                        "请稍后重试，若仍失败，请在原会话直接发送回答并联系管理员检查消息读取权限。"
                    )
                chat_kind = _public_chat_kind(await self._channel.get_chat_info(chat_id))
                try:
                    scope = scope_from_fetched_card(
                        app_id=self._app_id, callback_chat_id=chat_id,
                        fetched_message=fetched, chat_type=chat_kind,
                    )
                except CardActionError as error:
                    raise CardActionError(
                        "无法核验问题卡片所在会话，本次回答尚未交给 Codex。无需修改答案；"
                        "请稍后重试，若仍失败，请在原会话直接发送回答并联系管理员检查消息读取权限。"
                    ) from error
                target_scope, recipient = self._question_recipient(context.target)
                if target_scope != scope:
                    raise CardActionError(
                        "问题卡片与原会话的位置不一致，本条回答未执行。无需修改答案；"
                        "请回到原提问所在聊天或话题提交，或在原会话直接发送回答。"
                    )
                if isinstance(recipient, ThreadBinding):
                    current = self._bindings.active_binding(scope.key)
                    if current is None or current.id != recipient.id:
                        raise CardActionError(
                            f"请先通过 /sessions 切回问题所属会话 {recipient.short_id}，再提交回答；无需修改答案。"
                        )
                    admission = await self._runtime.capture_submission_admission(recipient.id)
                else:
                    admission = await self._runtime.capture_side_submission_admission(recipient.id)
                sender = await card_answer_sender(self._channel, chat_id, operator)
                if chat_kind not in {"p2p", "group"}:
                    raise CardActionError(
                        "无法确认原会话类型，本次回答尚未交给 Codex。无需修改答案；"
                        "请稍后重试，若仍失败，请在原会话直接发送回答并联系管理员检查聊天信息读取权限。"
                    )
                # The old question card is not a current history upper. Publish
                # a real answer anchor after capture, with actual operator
                # attribution retained separately from this bot-authored card.
                identity = json.dumps([message_id, sender["open_id"], value, form], sort_keys=True)
                receipt = render_question_receipt_card(answer, sender_name=sender["display_name"])
                sent = await send_topic_message(self._channel, chat_id, receipt, SendOpts(
                    receive_id_type="chat_id", reply_to=message_id,
                    reply_in_thread=scope.kind is ScopeKind.TOPIC, reply_target_gone="fail",
                    uuid="answer-" + hashlib.sha256(identity.encode()).hexdigest()[:32],
                ))
                if (
                    sent.thread_id != scope.topic_id or not sent.root_id or not sent.parent_id
                    or (scope.kind is not ScopeKind.TOPIC and sent.parent_id != message_id)
                ):
                    raise TopicPublishError("回答消息不属于原会话，本条回答未执行。", unknown=True)
                origin = CardAnswerOrigin(
                    self._app_id, chat_id, sent.message_id,
                    CardAnswerConversation(chat_id, chat_kind, scope.topic_id),
                    message_id, context.target,
                )
            projection = CardAnswerProjection(
                message_id=sent.message_id, source_card_id=message_id,
                target=context.target, sender=sender, request_text=answer.prompt,
            )
        except Exception as error:
            # No input has been handed off yet. Refresh the form's
            # transport nonce so an explicit retry can pass SDK dedup.
            if isinstance(error, TopicPublishError):
                explanation = (
                    "未能确认回答记录已正确发送到原会话，本次回答尚未交给 Codex。"
                    "无需修改答案；请稍后重试，或在原会话直接发送回答。"
                    f"\n原因：{describe_error(error)}"
                )
            elif isinstance(error, BindingNotFound):
                explanation = "问题所属会话已不存在，请通过 /sessions 选择可用会话，再直接发送问题和回答。"
            elif isinstance(error, (SideTopicNotFound, SideSessionNotFound)):
                explanation = "问题所属 Side 已不可用，旧卡片不能恢复它；请在主会话重新发起 /side，再发送问题和回答。"
            else:
                explanation = describe_error(error)
                if isinstance(context, QuestionAnswer) and not isinstance(error, CardActionError):
                    explanation += (
                        "\n本次回答尚未交给 Codex，无需修改答案。"
                        "请按上述原因处理，必要时检查或恢复服务；确认原会话仍可用后，可重新提交或直接发送回答。"
                    )
            notice = f"回答未提交：{explanation}"
            try:
                card = (
                    render_question_context_card(context, notice=notice)
                    if context is not None else error_card(notice)
                )
                updated = await self._safe_update_card(message_id, card)
            except Exception:
                updated = False
                logger.warning("question answer card could not be rendered")
            if not updated and scope is not None:
                target = _CardReplyTarget(message_id, message_id, chat_id, _CardReplyConversation(scope.topic_id))
                try:
                    async with asyncio.timeout(5):
                        await self._channel.reply(target, notice, SendOpts(
                            receive_id_type="chat_id", reply_to=message_id,
                            reply_in_thread=scope.kind is ScopeKind.TOPIC,
                            reply_target_gone="fail",
                        ))
                except Exception:
                    logger.warning("question answer feedback unavailable")
            return

        accepted = False
        submission_notice = None

        def mark_accepted() -> None:
            nonlocal accepted
            accepted = True

        try:
            skill_names = parse_skill_references(answer.answer)
            if isinstance(recipient, ThreadBinding):
                assert isinstance(admission, SubmissionAdmission)
                await self._consume_prompt(
                    binding=recipient, scope=scope, message=origin, current=projection,
                    owner_id=sender["open_id"], skill_names=skill_names, admission=admission,
                    on_accepted=mark_accepted,
                )
            else:
                assert isinstance(admission, SideSubmissionAdmission)
                await self._consume_side_prompt(
                    side_id=recipient.id, source_message=origin, reply_origin=origin,
                    current=projection, owner_id=sender["open_id"],
                    skill_names=skill_names, admission=admission,
                    on_accepted=mark_accepted,
                )
        except Exception as error:
            accepted = accepted or isinstance(error, ContextBoundaryCommitFailed)
            submission_notice = describe_error(error)
            await self._report_input_error(origin, error)
        finally:
            if accepted or submission_notice is not None:
                try:
                    async with asyncio.timeout(constants.QUESTION_SUBMISSION_CARD_TIMEOUT_SECONDS):
                        card = render_question_submission_card(
                            answer, sender_name=sender["display_name"],
                            accepted=accepted, notice=submission_notice,
                        )
                        await self._safe_update_card(message_id, card)
                except Exception:
                    logger.warning("question submission feedback unavailable", exc_info=True)
