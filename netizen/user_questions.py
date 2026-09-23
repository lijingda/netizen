"""Native question display data and Codex 0.156.1 answer formatting.

These are presentation values, not pending requests or a question lifecycle.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class UserQuestion:
    title: str
    options: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class QuestionRequest:
    item_id: str
    questions: tuple[UserQuestion, ...]


def question_item_id(item_id: str, question_index: int) -> str:
    """Match the official client's identity for one question in an item."""

    return json.dumps(
        ["request_user_input_async", item_id, question_index],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def format_question_answer(
    item_id: str, question_index: int, title: str, answer: str
) -> str:
    """Render the native 0.156.1 answered-question fragment.

    The title's UTF-8 byte bound and overlong-identity fallback layout match
    context-fragments/src/answered_question.rs. Model-authored dollar signs use
    Unicode escapes so native raw-text scanning cannot turn them into Skills.
    In the plain fallback these remain readable literal escape sequences.
    """

    question_id = question_item_id(item_id, question_index)
    bounded_title = title.encode("utf-8")[:512].decode("utf-8", errors="ignore")
    bounded_title = bounded_title.replace("\n", " ").replace("\r", " ")
    if len(question_id.encode("utf-8")) > 512:
        inert_title = bounded_title.replace("$", "\\u0024")
        return f"> {inert_title}\n\n{answer}"

    def model_text(value: str) -> str:
        return json.dumps(value, ensure_ascii=False).replace("$", "\\u0024")

    body = (
        '[{"answer":'
        + json.dumps(answer, ensure_ascii=False)
        + ',"question":'
        + model_text(bounded_title)
        + ',"questionItemId":'
        + model_text(question_id)
        + "}]"
    )
    return (
        "<send_user_message_question_reply>\n"
        + body
        + "\n</send_user_message_question_reply>"
    )
