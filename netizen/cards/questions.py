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
    raise CardActionError("问题卡片的会话目标无效。")


def _decoded(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise CardActionError("问题卡片字段不完整或含未知字段。")
    payload = dict(payload)
    payload.pop("nonce", None)  # Transport dedup only, never a business precondition.
    version = payload.get("v")
    if payload.get("kind") != _KIND or type(version) is not int or version not in {1, _VERSION}:
        raise CardActionError("问题卡片版本无效，请在会话中直接回答。")
    fields = {"kind", "v", "item_id", "question_index", "title", "options"}
    fields.add("binding_id" if version == 1 else "target")
    if set(payload) != fields:
        raise CardActionError("问题卡片字段不完整或含未知字段。")
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
        raise CardActionError("问题卡片的会话、题目或选项无效。")
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
        raise CardActionError("问题表单字段不匹配，请重新打开卡片。")
    choice = form_value.get(_CHOICE, "free")
    if not isinstance(choice, str) or choice not in {"free", *(str(index) for index in range(len(payload["options"])))}:
        raise CardActionError("问题选项无效，请重新选择。")
    if choice == "free":
        answer = form_value.get(_TEXT) or ""
        if not isinstance(answer, str) or len(answer) > _MAX_ANSWER_CHARS:
            raise CardActionError("填写的回答无效或过长，请在会话中直接发送。")
        if not answer.strip():
            raise CardActionError("请选择一个建议，或选择自行填写并输入回答，然后点击提交。")
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
    builder = _builder("Codex 提问", "选择建议或自行填写，再提交回答")
    if notice:
        builder.raw(_notice(notice))
    builder.raw(_plain(context.question.title))
    for index, option in enumerate(context.question.options):
        builder.raw(_plain(f"{index + 1}. {option}"))
    options = [{"text": _plain_text("自行填写"), "value": "free"}]
    options.extend(
        {"text": _plain_text(f"选项 {index + 1}"), "value": str(index)}
        for index in range(len(context.question.options))
    )
    initial_choice = context.choice if isinstance(context, QuestionAnswer) else "free"
    initial_text = context.answer if isinstance(context, QuestionAnswer) and context.choice == "free" else ""
    builder.raw({
        "tag": "form",
        "name": "netizen_question",
        "elements": [
            {"tag": "select_static", "name": _CHOICE, "required": True,
             "width": "fill", "options": options, "initial_option": initial_choice,
             "placeholder": _plain_text("选择建议或自行填写")},
            {"tag": "input", "name": _TEXT, "required": False,
             "input_type": "multiline_text", "max_length": _MAX_ANSWER_CHARS,
             "width": "fill", "placeholder": _plain_text("仅选择“自行填写”时提交此处内容"),
             "default_value": initial_text},
            {"tag": "button", "name": "netizen_question_submit", "text": _plain_text("提交回答"),
             "type": "primary_filled", "width": "fill", "form_action_type": "submit",
             "behaviors": [{"type": "callback", "value": payload}]},
        ],
    })
    builder.raw(_plain("提交后请查看聊天中的处理反馈；如需重发，请在原会话直接发送回答。"))
    card = builder.to_dict()
    card["config"]["width_mode"] = "compact"
    if len(json.dumps(card, ensure_ascii=False).encode("utf-8")) > QUESTION_CARD_JSON_LIMIT_BYTES:
        raise CardActionError("问题内容较长，无法展示完整选择卡片；请在会话中直接回答。")
    return OutboundCard(card=card)
