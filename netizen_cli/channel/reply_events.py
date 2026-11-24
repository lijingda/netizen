"""Exact, in-memory inputs to Feishu reply presentation."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import InitVar, dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Literal

from lark_channel import OutboundCard, OutboundPost, SendResult

from ..bindings import BindingTaskFeedback
from ..file_evidence import FileEvidence
from ..ordinary_sessions import LogicalReply
from ..partial_answers import PartialAnswer as NativePartial
from ..runtime.contracts import SideTurnActivitySnapshot, TurnActivitySnapshot
from .topics import CardReplyLocation
from .physical_delivery import PhysicalDelivery

Activity = TurnActivitySnapshot | SideTurnActivitySnapshot
TerminalStatus = Literal["completed", "interrupted", "failed"]


def terminal_view(
    status: str | None, has_error: bool, *, failure: Callable[[], str],
    interruption: Callable[[], str], answer: str | None, empty: str,
) -> tuple[TerminalStatus, str]:
    if has_error or status not in {"completed", "interrupted"}:
        return "failed", failure()
    if status == "interrupted":
        return "interrupted", interruption()
    return "completed", answer or empty


@dataclass(frozen=True, slots=True)
class PresentationKey:
    owner_id: str
    thread_id: str
    turn_id: str
    side: bool = False

    @property
    def index(self) -> tuple[str, str, str]:
        return self.owner_id, self.thread_id, self.turn_id

    @property
    def log_context(self) -> dict[str, str]:
        return {
            "side_id" if self.side else "binding_id": self.owner_id,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "operation_id": self.turn_id,
        }

    def matches(self, snapshot: Activity) -> bool:
        if isinstance(snapshot, SideTurnActivitySnapshot):
            return self.side and self.index == (snapshot.side_id, snapshot.thread_id, snapshot.turn_id)
        return not self.side and self.index == (snapshot.binding_id, snapshot.thread_id, snapshot.turn_id)


@dataclass(frozen=True, slots=True)
class ReplyTarget:
    key: PresentationKey
    origin: object
    run: PresentationKey | None

    def partial(self, item: NativePartial) -> PartialAnswer:
        key = replace(self.key, thread_id=item.thread_id, turn_id=item.turn_id)
        return PartialAnswer(replace(self, key=key), item)

    def exact_answers(self, partials: tuple[PartialAnswer, ...]) -> tuple[NativePartial, ...]:
        return tuple(part.item for part in partials if part.target == self)


@dataclass(frozen=True, slots=True)
class PartialAnswer:
    target: ReplyTarget
    item: NativePartial


@dataclass(frozen=True, slots=True)
class ActivityChanged:
    target: ReplyTarget
    snapshot: Activity
    partials: InitVar[tuple[PartialAnswer, ...]]

    def __post_init__(self, partials: tuple[PartialAnswer, ...]) -> None:
        items = self.target.exact_answers(partials)
        if items != self.snapshot.partial_answers:
            object.__setattr__(self, "snapshot", replace(self.snapshot, partial_answers=items))

    @classmethod
    def from_snapshot(cls, target: ReplyTarget, snapshot: Activity | None) -> ActivityChanged | None:
        if snapshot is None:
            return None
        return cls(target, snapshot, tuple(target.partial(p)
                                           for p in snapshot.partial_answers))


class DeliveryPolicy(Enum):
    BOUNDED_FALLBACK = "bounded_fallback"
    ITEM_RECEIPTS = "item_receipts"

    @property
    def confirms_items(self) -> bool:
        return self is DeliveryPolicy.ITEM_RECEIPTS


@dataclass(frozen=True, slots=True)
class TurnOpened:
    target: ReplyTarget
    policy: DeliveryPolicy
    feedback: BindingTaskFeedback
    observe: Callable[[], ActivityChanged | None]
    retain_after_send: Callable[[], bool]
    reply: Callable[[OutboundCard], Awaitable[SendResult]]
    reply_partial: Callable[[OutboundPost], Awaitable[SendResult]]
    validate_reply: Callable[[SendResult], Awaitable[bool]] | None = None
    retained: Callable[[], bool] = lambda: True
    location: CardReplyLocation | None = None
    reuse_existing: bool = False
    run_reply: LogicalReply | None = None


@dataclass(frozen=True, slots=True)
class RunDisplay:
    reply: LogicalReply | None
    publishable: Callable[[], bool]
    current: Callable[[], str]


@dataclass(frozen=True, slots=True)
class ItemDelivery:
    """Receipt policy for content which can only be attempted once."""
    display: RunDisplay
    location: CardReplyLocation
    send: Callable[[str | OutboundPost | OutboundCard, str | None, CardReplyLocation], Awaitable[SendResult]]
    plain_suffix: Callable[[], str]
    deadline: float | None


@dataclass(frozen=True, slots=True)
class RunEnded:
    target: ReplyTarget
    status: TerminalStatus
    mention_user_id: str | None
    display: RunDisplay | None = None
    location: CardReplyLocation | None = None


@dataclass(frozen=True, slots=True)
class RunState:
    target: ReplyTarget
    notice: str | None = None
    display: RunDisplay | None = None
    background: bool = False
    send_notice: Callable[[str], Awaitable[SendResult]] | None = None


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
    run_continues: bool
    end: RunEnded | None
    receipt: DeliveryReceipt | None = None
    items: ItemDelivery | None = None
    run_reply: LogicalReply | None = None
    partial_answers: tuple[NativePartial, ...] = field(init=False)
    bounded_delivery: PhysicalDelivery | None = None

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

    def __post_init__(self) -> None:
        object.__setattr__(self, "partial_answers", self.target.exact_answers(self.partials))
