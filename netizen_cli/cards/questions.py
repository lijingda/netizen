"""Self-contained question forms; callback answers use ordinary input admission."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from lark_channel import OutboundCard

from ..user_questions import (
    BindingQuestionTarget,
    QuestionRequest,
    QuestionTarget,
    SideQuestionTarget,
    UserQuestion,
    format_question_answer,
    question_target_payload,
)
from .callbacks import CardActionError, _builder, _notice, _plain, _plain_text
from .reply import TURN_FILE_CARD_JSON_LIMIT_BYTES


_KIND = "netizen_question"
_VERSION = 2
_CHOICE = "netizen_question_choice"
_TEXT = "netizen_question_text"
_MAX_ANSWER_CHARS = 1000
QUESTION_CARD_JSON_LIMIT_BYTES = TURN_FILE_CARD_JSON_LIMIT_BYTES


@dataclass(frozen=True, slots=True)
class QuestionCardContext:
    target: QuestionTarget
    item_id: str
    question_index: int
    question: UserQuestion


@dataclass(frozen=True, slots=True)
class QuestionAnswer(QuestionCardContext):
    answer: str
    choice: str

    @property
    def prompt(self) -> str:
        return format_question_answer(
            self.item_id, self.question_index, self.question.title, self.answer
        )


def is_question_card_action(value: Any, form_value: Any = None) -> bool:
    return (
        isinstance(value, Mapping) and value.get("kind") == _KIND
    ) or (
        isinstance(form_value, Mapping)
        and bool({_CHOICE, _TEXT} & form_value.keys())
    )


def _payload(context: QuestionCardContext) -> dict[str, Any]:
    return {
        "kind": _KIND,
        "v": _VERSION,
        "target": question_target_payload(context.target),
        "item_id": context.item_id,
        "question_index": context.question_index,
        "title": context.question.title,
        "options": list(context.question.options),
        "nonce": uuid4().hex,
    }


def _target(payload: Mapping[str, Any]) -> QuestionTarget:
    try:
        if payload["v"] == 1:
            return BindingQuestionTarget(payload["binding_id"])
        target = payload["target"]
        if isinstance(target, Mapping) and set(target) == {"kind", "id"}:
            if target["kind"] == "binding":
                return BindingQuestionTarget(target["id"])
            if target["kind"] == "side":
                return SideQuestionTarget(target["id"])
    except (TypeError, ValueError):
        pass
    raise CardActionError("无法确认问题卡片所属的会话，请在原会话直接发送回答。")


def _decoded(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise CardActionError("问题卡片内容不完整，无法提交；请在原会话直接发送回答。")
    payload = dict(payload)
    payload.pop("nonce", None)  # Transport dedup only, never a business precondition.
    version = payload.get("v")
    if payload.get("kind") != _KIND or type(version) is not int or version not in {1, _VERSION}:
        raise CardActionError("问题卡片版本无效，请在会话中直接回答。")
    fields = {"kind", "v", "item_id", "question_index", "title", "options"}
    fields.add("binding_id" if version == 1 else "target")
    if set(payload) != fields:
        raise CardActionError("问题卡片内容不完整，无法提交；请在原会话直接发送回答。")
    _target(payload)
    item_id = payload["item_id"]
    index = payload["question_index"]
    title = payload["title"]
    options = payload["options"]
    if (
        not isinstance(item_id, str)
        or not item_id.strip()
        or len(item_id) > 2048
        or type(index) is not int
        or not 0 <= index < 1000
        or not isinstance(title, str)
        or not title.strip()
        or not isinstance(options, list)
        or len(options) > 99
        or any(not isinstance(option, str) or not option.strip() for option in options)
    ):
        raise CardActionError("无法读取问题卡片中的题目或选项，请在原会话直接发送回答。")
    result = dict(payload)
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > QUESTION_CARD_JSON_LIMIT_BYTES:
        raise CardActionError("问题内容较长，无法展示完整选择卡片；请在会话中直接回答。")
    return result


def _context(payload: Mapping[str, Any]) -> QuestionCardContext:
    return QuestionCardContext(
        target=_target(payload),
        item_id=payload["item_id"],
        question_index=payload["question_index"],
        question=UserQuestion(payload["title"], tuple(payload["options"])),
    )


def decode_question_context(value: Any) -> QuestionCardContext:
    """Recover retry context even when the submitted answer is empty."""

    return _context(_decoded(value))


def decode_question_answer(value: Any, form_value: Any = None) -> QuestionAnswer:
    payload = _decoded(value)
    if not isinstance(form_value, Mapping) or set(form_value) - {_CHOICE, _TEXT}:
        raise CardActionError("无法读取回答表单，请在原会话直接发送回答。")
    choice = form_value.get(_CHOICE, "free")
    if not isinstance(choice, str) or choice not in {"free", *(str(index) for index in range(len(payload["options"])))}:
        raise CardActionError("所选选项无效，请重新选择一个建议，或选择“自行填写”并输入回答。")
    if choice == "free":
        answer = form_value.get(_TEXT) or ""
        if not isinstance(answer, str):
            raise CardActionError("无法读取填写的回答，请在原会话直接发送回答。")
        if len(answer) > _MAX_ANSWER_CHARS:
            raise CardActionError("自行填写最多 1,000 字符，请缩短回答，或在原会话直接发送。")
        if not answer.strip():
            raise CardActionError("你选择了“自行填写”，但尚未输入内容。请填写回答，或改选一个建议后提交。")
    else:
        answer = payload["options"][int(choice)]
    context = _context(payload)
    return QuestionAnswer(
        context.target, context.item_id, context.question_index,
        context.question, answer, choice,
    )


def render_question_card(
    target: QuestionTarget,
    request: QuestionRequest,
    question_index: int,
    *,
    notice: str | None = None,
) -> OutboundCard:
    if type(question_index) is not int or not 0 <= question_index < len(request.questions):
        raise CardActionError("问题序号无效。")
    return render_question_context_card(
        QuestionCardContext(target, request.item_id, question_index, request.questions[question_index]),
        notice=notice,
    )


def render_question_context_card(
    context: QuestionCardContext,
    *,
    notice: str | None = None,
) -> OutboundCard:
    payload = _payload(context)
    _decoded(payload)
    builder = _builder(
        "回答提交失败" if notice else "Codex 提问",
        "回答尚未交给 Codex，请查看下方原因与处理办法" if notice else "选择建议或自行填写，再提交回答",
        template="red" if notice else "blue",
    )
    if notice:
        builder.raw(_notice(notice, error=True))
    builder.raw(_plain(context.question.title))
    options = [
        {"text": _plain_text(f"选项 {index + 1}"), "value": str(index)}
        for index in range(len(context.question.options))
    ]
    options.append({
        "text": _plain_text(f"选项 {len(options) + 1}（自行填写）"), "value": "free",
    })
    initial_choice = context.choice if isinstance(context, QuestionAnswer) else "free"
    initial_text = context.answer if isinstance(context, QuestionAnswer) and context.choice == "free" else ""
    builder.raw({
        "tag": "form",
        "name": "netizen_question",
        "elements": [
            *(_plain(f"{index + 1}. {option}") for index, option in enumerate(context.question.options)),
            {"tag": "input", "name": _TEXT, "required": False,
             "input_type": "text", "max_length": _MAX_ANSWER_CHARS,
             "label": _plain_text(f"{len(context.question.options) + 1}. 自行填写"),
             "width": "fill", "placeholder": _plain_text("输入自己的回答"),
             "default_value": initial_text},
            {"tag": "select_static", "name": _CHOICE, "required": True,
             "width": "fill", "options": options, "initial_option": initial_choice,
             "placeholder": _plain_text("选择建议或自行填写")},
            {"tag": "button", "name": "netizen_question_submit", "text": _plain_text("提交回答"),
             "type": "primary_filled", "width": "fill", "form_action_type": "submit",
             "behaviors": [{"type": "callback", "value": payload}]},
        ],
    })
    builder.raw(_plain("只有选择“自行填写”时，才会提交输入框中的内容。"))
    card = builder.to_dict()
    card["config"]["width_mode"] = "compact"
    return _bounded_question_card(card)


def render_question_receipt_card(
    answer: QuestionAnswer, *, sender_name: str,
) -> OutboundCard:
    builder = _builder("问题回答", f"{sender_name} 通过问题卡提交的回答")
    _question_answer_content(builder, answer, sender_name=sender_name, color="blue")
    builder.raw(_plain("此消息用于记录回答；提交结果请查看原问题卡和聊天反馈。"))
    return _bounded_question_card(builder.to_dict())


def render_question_submission_card(
    answer: QuestionAnswer, *, sender_name: str, accepted: bool, notice: str | None = None,
) -> OutboundCard:
    """Display this submission's result without creating another submit action."""
    subtitle = (
        "回答已送达原会话，后续进展请查看聊天"
        if accepted else "请查看聊天中的处理反馈，确认后再决定是否重发"
    )
    if accepted and notice:
        subtitle = "回答已被 Codex 接收，后续处理出现异常"
    color = "green" if accepted and not notice else "orange"
    builder = _builder(
        "回答已提交" if accepted else "回答提交异常", subtitle, template=color,
    )
    _question_answer_content(builder, answer, sender_name=sender_name, color=color)
    if notice:
        builder.raw(_notice(notice, error=True))
    builder.raw(_plain(
        "如需补充或修改回答，请在原会话直接发送。"
        if accepted else
        "本次卡片提交已结束。需要补充或重发时，请在原会话直接发送；"
        "接收结果未确认时，请先按聊天中的提示检查或恢复服务，不要重复提交。"
    ))
    card = builder.to_dict()
    card["config"]["width_mode"] = "compact"
    return _bounded_question_card(card)


def _question_answer_content(
    builder: Any, answer: QuestionAnswer, *, sender_name: str, color: str,
) -> None:
    builder.raw(_plain("问题\n" + answer.question.title))
    block = _notice(f"{sender_name} 的回答\n{answer.answer}")
    block["background_style"] = f"{color}-50"
    builder.raw(block)


def _bounded_question_card(card: dict[str, Any]) -> OutboundCard:
    if len(json.dumps(card, ensure_ascii=False).encode("utf-8")) > QUESTION_CARD_JSON_LIMIT_BYTES:
        raise CardActionError("问题内容较长，无法展示完整选择卡片；请在会话中直接回答。")
    return OutboundCard(card=card)
