"""In-memory receipts for one Claude physical answer and its running presentation."""
from dataclasses import dataclass, field
from typing import Literal

from lark_channel import SendResult

from ..cards.reply import feishu_answer_text
from ..partial_answers import PartialAnswer
from ..ordinary_sessions import LogicalReply
from .messages import _confirmed_reply_id, _send_result_error_code
from .topics import CardReplyLocation

Item = tuple[str, str]
Outcome = Literal["unattempted", "unknown", "rejected", "confirmed"]
BODY: Item = ("body", "")
INCOMPLETE = "部分内容未能确认送达。"


def receipt(result: SendResult | None) -> Outcome:
    code = _send_result_error_code(result)
    if result is not None and result.success and code in (None, 0):
        return "confirmed"
    return "rejected" if code is not None and code > 0 else "unknown"


def record_sent_answer(reply: LogicalReply | None, location: CardReplyLocation, turn_id: str,
                       items: tuple[Item, ...], result: SendResult) -> None:
    # An empty ID denotes a confirmed post with contradictory IDs. It may qualify
    # for the original-input exception, but is never evidence of content on that input.
    mid = _confirmed_reply_id(result, on_conflict="")
    if reply is not None and mid is not None:
        reply.published(mid, turn_id, items, location.binding.unusable)


@dataclass(slots=True)
class PhysicalDelivery:
    reply: LogicalReply | None = None
    items: dict[Item, Outcome] = field(default_factory=dict)
    partial_text: dict[Item, str] = field(default_factory=dict)
    prepared_images: set[Item] = field(default_factory=set)
    settled: bool = False

    def expect(self, item: Item) -> None:
        self.items.setdefault(item, "unattempted")

    def partial(self, answer: PartialAnswer) -> Item:
        item = ("partial", answer.item_id)
        self.expect(item)
        self.partial_text[item] = feishu_answer_text(answer.text)
        return item

    def partial_receipt(self, content: str) -> Outcome:
        states = {self.items[item] for item, text in self.partial_text.items() if text == content}
        for state in ("confirmed", "unknown", "rejected"):
            if state in states:
                return state
        return "unattempted"

    def begin(self, items: tuple[Item, ...]) -> None:
        # Mark before awaiting the Channel. Cancellation leaves an unknown outcome.
        if self.reply and any(kind in ("body", "partial") for kind, _ in items):
            self.reply.attempted = True
        self.record(items, "unknown")

    def record(self, items: tuple[Item, ...], outcome: Outcome) -> None:
        for item in items:
            if self.items.get(item) != "confirmed":
                self.items[item] = outcome

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
