"""Stable, completed answer fragments kept only for the active reply lifetime."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
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


@dataclass(frozen=True, slots=True)
class FinalAgentAnswer:
    item_id: str | None
    text: str
    explicit: bool


def final_agent_answer(items: Sequence[object]) -> FinalAgentAnswer | None:
    fallback = None
    for item in reversed(items):
        root = getattr(item, "root", item)
        text = getattr(root, "text", None)
        if getattr(root, "type", None) != "agentMessage" or not isinstance(text, str):
            continue
        phase = getattr(root, "phase", None)
        if getattr(phase, "value", phase) == "final_answer":
            return FinalAgentAnswer(getattr(root, "id", None), text, True)
        if phase is None and fallback is None:
            fallback = FinalAgentAnswer(getattr(root, "id", None), text, False)
    return fallback


def merge_final_answer(observed: FinalAgentAnswer | None, turn: object) -> FinalAgentAnswer | None:
    history = final_agent_answer(getattr(turn, "items", ()))
    if history is None or observed is None:
        return observed or history
    view = getattr(turn, "items_view", "full")
    if history.explicit != observed.explicit:
        return history if history.explicit else observed
    return history if getattr(view, "value", view) == "full" else observed
