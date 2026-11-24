"""In-memory receipts for one physical answer and its running presentation."""
import asyncio
import logging
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Literal

from lark_channel import SendResult

from ..cards.reply import feishu_answer_text
from ..partial_answers import PartialAnswer
from ..ordinary_sessions import LogicalReply
from .messages import _confirmed_reply_id
from .topics import CardReplyLocation, receipt

Item = tuple[str, str]
Outcome = Literal["unattempted", "unknown", "rejected", "confirmed"]
BODY: Item = ("body", "")
INCOMPLETE = "部分内容未能确认送达。"
logger = logging.getLogger(__name__)


def record_sent_answer(reply: LogicalReply | None, location: CardReplyLocation, turn_id: str,
                       items: tuple[Item, ...], result: SendResult) -> None:
    # An empty ID denotes a confirmed post with contradictory IDs. It may qualify
    # for the original-input exception, but is never evidence of content on that input.
    mid = _confirmed_reply_id(result, on_conflict="")
    if reply is not None and mid is not None:
        reply.published(mid, turn_id, items, location.usable)


@dataclass(slots=True)
class PhysicalDelivery:
    items: dict[Item, Outcome] = field(default_factory=dict)
    partial_text: dict[Item, str] = field(default_factory=dict)
    prepared_images: set[Item] = field(default_factory=set)
    posted: set[Item] = field(default_factory=set)
    settled: bool = False
    bounded_fallback: bool = False

    def expect(self, item: Item) -> None:
        self.items.setdefault(item, "unattempted")

    def partial(self, answer: PartialAnswer) -> Item:
        item = ("partial", answer.item_id)
        self.expect(item)
        self.partial_text[item] = feishu_answer_text(answer.text)
        return item

    def partial_receipt(self, items: tuple[Item, ...]) -> Outcome:
        states = {self.items[item] for item in items}
        for state in ("confirmed", "unknown", "rejected"):
            if state in states:
                return state
        return "unattempted"

    def can_post(self, item: Item) -> bool:
        state = self.items.get(item, "unattempted")
        return item not in self.posted and (state in ("unattempted", "rejected") or (state == "unknown" and self.bounded_fallback))

    def begin(self, items: tuple[Item, ...], reply: LogicalReply | None, *, post: bool = False) -> tuple[Item, ...]:
        # Mark before awaiting the Channel. Cancellation leaves an unknown outcome.
        undisputed = tuple(item for item in items if self.items.get(item) != "unknown")
        if post:
            self.posted.update(items)
        if reply and any(kind in ("body", "partial") for kind, _ in items):
            reply.attempted = True
        self.record(items, "unknown")
        return undisputed

    def record(self, items: tuple[Item, ...], outcome: Outcome) -> None:
        for item in items:
            if self.items.get(item) != "confirmed":
                self.items[item] = outcome

    async def send(self, items: tuple[Item, ...], reply: LogicalReply | None, operation: Awaitable[SendResult | None],
                   *, post: bool = False, timeout: float | None = None) -> tuple[Outcome, SendResult | None]:
        undisputed = self.begin(items, reply, post=post)
        result = None
        try:
            async with asyncio.timeout(timeout):
                result = await operation
            outcome = receipt(result)
        except Exception as error:
            outcome = receipt(error)
            logger.warning("content write %s (%s)", outcome, type(error).__name__)
        # Rejecting this request cannot disprove an earlier unacknowledged write.
        self.record(undisputed if outcome == "rejected" else items, outcome)
        return outcome, result

    def image(self, source: str, state: Literal["unattempted", "unknown", "rejected", "ready"]) -> None:
        item = ("image", source)
        self.expect(item)
        if state == "ready":
            self.prepared_images.add(item)
        else:
            self.record((item,), state)

    def settle(self) -> bool:
        if self.settled:
            return False
        self.settled = True
        return any(state != "confirmed" for state in self.items.values())
