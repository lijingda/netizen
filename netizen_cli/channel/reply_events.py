"""Exact, in-memory inputs to Feishu reply presentation."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from lark_channel import OutboundCard, OutboundPost, SendResult

from ..bindings import BindingTaskFeedback
from ..file_evidence import FileEvidence
from ..partial_answers import PartialAnswer as NativePartial
from ..runtime.contracts import SideTurnActivitySnapshot, TurnActivitySnapshot
from .topics import CardReplyLocation

Activity = TurnActivitySnapshot | SideTurnActivitySnapshot
TerminalStatus = Literal["completed", "interrupted", "failed"]


@dataclass(frozen=True, slots=True)
class PresentationKey:
    owner_id: str
    thread_id: str
    turn_id: str
    side: bool = False

    @property
    def index(self) -> tuple[str, str, str]:
        return self.owner_id, self.thread_id, self.turn_id

    def matches(self, snapshot: Activity) -> bool:
        if isinstance(snapshot, SideTurnActivitySnapshot):
            return self.side and self.index == (snapshot.side_id, snapshot.thread_id, snapshot.turn_id)
        return not self.side and self.index == (snapshot.binding_id, snapshot.thread_id, snapshot.turn_id)


@dataclass(frozen=True, slots=True)
class ReplyTarget:
    key: PresentationKey
    origin: object
    run: PresentationKey | None

    def partial(self, item: NativePartial, *, presentation_id: str | None = None) -> PartialAnswer:
        key = replace(self.key, thread_id=item.thread_id, turn_id=presentation_id or item.turn_id)
        return PartialAnswer(replace(self, key=key), item)


@dataclass(frozen=True, slots=True)
class PartialAnswer:
    target: ReplyTarget
    item: NativePartial


@dataclass(frozen=True, slots=True)
class ActivityChanged:
    target: ReplyTarget
    snapshot: Activity
    partials: tuple[PartialAnswer, ...]

    def exact_snapshot(self) -> Activity:
        items = tuple(part.item for part in self.partials if part.target == self.target)
        if items == self.snapshot.partial_answers:
            return self.snapshot
        return replace(self.snapshot, partial_answers=items)


@dataclass(frozen=True, slots=True)
class TurnOpened:
    target: ReplyTarget
    feedback: BindingTaskFeedback
    observe: Callable[[], ActivityChanged | None]
    retain_after_send: Callable[[], bool]
    reply: Callable[[OutboundCard], Awaitable[SendResult]]
    reply_partial: Callable[[OutboundPost], Awaitable[SendResult]]
    validate_reply: Callable[[SendResult], Awaitable[bool]] | None = None
    retained: Callable[[], bool] = lambda: True
    location: CardReplyLocation | None = None
    reuse_existing: bool = False


@dataclass(frozen=True, slots=True)
class RunEnded:
    target: ReplyTarget
    status: TerminalStatus
    mention_user_id: str | None


@dataclass(frozen=True, slots=True)
class RunState:
    target: ReplyTarget
    notice: str


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    """Persist the initial reply once its existing destination policy accepts it."""
    validate: Callable[[SendResult], Awaitable[bool]]
    partials: Callable[[bool], None]
    suppressed: Callable[[], None]
    card: Callable[[str, SendResult | None], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class TurnResult:
    target: ReplyTarget
    status: TerminalStatus
    content: str
    final_response: str | None
    task_feedback: BindingTaskFeedback
    cwd: Path | None
    activity: Activity | None
    partials: tuple[PartialAnswer, ...]
    file_evidence: Callable[[], FileEvidence]
    file_binding_id: str
    physical_turn_id: str
    allow_suppression: bool
    end: RunEnded | None
    receipt: DeliveryReceipt | None = None

    @property
    def origin(self) -> object:
        return self.target.origin

    @property
    def thread_id(self) -> str:
        return self.target.key.thread_id

    @property
    def turn_id(self) -> str:
        return self.target.key.turn_id

    @property
    def mention_user_id(self) -> str | None:
        return self.end.mention_user_id if self.end else None

    @property
    def partial_answers(self) -> tuple[NativePartial, ...]:
        return tuple(part.item for part in self.partials if part.target.key == self.target.key)
