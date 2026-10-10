"""Stable, completed answer fragments kept only for the active reply lifetime."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from openai_codex.generated.v2_all import AgentMessageThreadItem, ThreadItem


@dataclass(frozen=True, slots=True)
class PartialAnswer:
    thread_id: str
    turn_id: str
    item_id: str
    text: str

    def __post_init__(self) -> None:
        for value in (self.thread_id, self.turn_id, self.item_id):
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError("partial answer requires exact Thread, Turn and item IDs")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("partial answer text must not be empty")


def project_partial_answer(
    item: object, *, thread_id: str, turn_id: str,
) -> PartialAnswer | None:
    """Project only a completed typed assistant answer, without Activity truncation."""

    if type(item) is not AgentMessageThreadItem:
        return None
    if getattr(item.phase, "value", item.phase) != "partial_answer":
        return None
    if not isinstance(item.text, str) or not item.text.strip():
        return None
    return PartialAnswer(thread_id, turn_id, item.id, item.text)


def partial_answers_from_items(
    items: Iterable[object], *, thread_id: str, turn_id: str,
) -> tuple[PartialAnswer, ...]:
    """Read completed exact-Turn items; callers establish terminal authority."""

    result: list[PartialAnswer] = []
    seen: set[str] = set()
    for item in items:
        if type(item) is not ThreadItem:
            continue
        answer = project_partial_answer(item.root, thread_id=thread_id, turn_id=turn_id)
        if answer is not None and answer.item_id not in seen:
            result.append(answer)
            seen.add(answer.item_id)
    return tuple(result)
