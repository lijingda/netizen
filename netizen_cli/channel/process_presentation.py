"""One process message's ephemeral list, placement evidence and write lifetime."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Literal

from ..external_calls import external_call
from ..runtime.contracts import TurnActivitySnapshot
from ..process_activity import ObservedProcessItem
from ..process_messages import (
    ITEM_LIMIT, Created, Ok, ProcessItem, ProcessMessages, Rejected, RejectionCode,
    StepItem, TextItem, ToolItem, build_items, finish_items,
)
from .messages import _nonempty_field, _object_field
from .physical_delivery import Outcome
from .ports import ReplyChannel
from .replacement_writer import ReplacementWriter
from .reply_events import Activity, PresentationKey
from .topics import CardReplyLocation


logger = logging.getLogger(__name__)
Reason = Literal["done", "error", "timeout"]
Payload = tuple[ProcessItem, ...]


def _item(row: ObservedProcessItem) -> ProcessItem:
    if row.kind == "commentary":
        assert row.finished_at is not None
        return TextItem(row.identity, row.text, row.started_at, row.finished_at)
    kind = StepItem if row.kind == "step" else ToolItem
    return kind(row.identity, row.text, row.started_at, row.finished_at)


class ProcessList:
    """Reserve a slot for each running START until its paired END is confirmed."""

    def __init__(self) -> None:
        self.pinned: dict[str, ProcessItem] = {}
        self.running_offers: dict[str, ProcessItem] = {}
        self._step_pins: dict[str, ProcessItem] = {}
        self._physical_turn: str | None = None
        self._window: dict[str, ProcessItem] = {}
        self.desired: Payload = ()

    def observe(self, snapshot: Activity, now: int) -> None:
        if not snapshot.process_available:
            return
        physical = (snapshot.physical_turn_id if isinstance(snapshot, TurnActivitySnapshot) else None) or snapshot.turn_id
        if physical != self._physical_turn:
            self._physical_turn = physical
            self._step_pins.clear()
        current = {}
        for row in snapshot.process_items:
            item = _item(row)
            pinned = self._step_pins.get(row.identity) or self.running_offers.get(row.identity) or self.pinned.get(row.identity)
            if pinned is not None:
                item = pinned.finish(row.finished_at) if row.finished_at is not None else pinned
            current[row.identity] = item
        self._window = current
        for key, offered in self.running_offers.items():
            observed = current.get(key)
            self.running_offers[key] = offered.finish(now) if observed is None else observed
            if key in self._step_pins:
                self._step_pins[key] = self.running_offers[key]
        self._select()

    def _select(self) -> None:
        # All obligations fit because only free slots can admit a new START.
        reserved = self.running_offers
        available = [item for key, item in self._window.items() if key not in reserved]
        running = sorted((item for item in available if item.finished_at is None),
                         key=lambda item: (item.started_at, item.item_id))
        finished = sorted((item for item in available if item.finished_at is not None),
                          key=lambda item: (item.finished_at or 0, item.started_at, item.item_id), reverse=True)
        free = ITEM_LIMIT - len(reserved)
        self.desired = build_items((*reserved.values(), *(running + finished)[:free]))

    def offered(self, payload: Payload) -> None:
        self.pinned = {item.item_id: item for item in payload}
        for item in payload:
            if item.finished_at is None:
                self.running_offers.setdefault(item.item_id, item)
            if isinstance(item, StepItem) and item.item_id in self._window:
                self._step_pins[item.item_id] = item

    def confirmed(self, payload: Payload) -> None:
        for item in payload:
            if item.finished_at is not None:
                self.running_offers.pop(item.item_id, None)
        self._select()


async def _verify(channel: ReplyChannel, location: CardReplyLocation, created: Created) -> bool:
    mid = created.message_id
    if mid in (location.parent, location.root) or mid in location.binding.owners:
        location.binding.unusable.add(mid)
        return False
    if location.scope.topic_id:
        response = await external_call(channel.fetch_message(mid))
        code = _object_field(response, "code")
        items = _object_field(_object_field(response, "data"), "items")
        if type(code) is not int or code != 0 or not isinstance(items, list) or len(items) != 1:
            return False
        actual = items[0]
        expected = {"message_id": mid, "chat_id": location.scope.chat_id, "thread_id": location.scope.topic_id,
                    "root_id": location.root, "parent_id": location.parent}
        if any((value := _nonempty_field(actual, key)) and value != required for key, required in expected.items()):
            location.binding.unusable.add(mid)
            return False
        if _object_field(actual, "deleted") is not False or any(
            _nonempty_field(actual, key) != required for key, required in expected.items()
        ):
            return False
    return (not location.retired and not location.binding.retired
            and mid not in location.binding.unusable and mid not in location.binding.owners)


class ProcessPresentation:
    """The presenter's poll supplies revisions. Only this task touches the process transport."""

    def __init__(self, key: PresentationKey, run: PresentationKey | None, port: ProcessMessages,
                 channel: ReplyChannel, location: Callable[[], CardReplyLocation | None],
                 claim: Callable[[ProcessPresentation, Created], bool], clock: Callable[[], float],
                 timeout: float, on_closed: Callable[[ProcessPresentation], None] = lambda _: None) -> None:
        self.key, self.run = key, run
        self.port, self.channel, self.resolve, self.claim = port, channel, location, claim
        self.clock, self.timeout, self.on_closed = clock, timeout, on_closed
        self.terminal = False
        self.items = ProcessList()
        self.writer = ReplacementWriter[Payload](lambda items: items)
        self.location: CardReplyLocation | None = None
        self.created: Created | None = None
        self.ready = asyncio.Event()
        self.task: asyncio.Task[None] | None = None
        self.revision: int | None = None
        self.generation = 0
        self.sampled = -1
        self.wake = asyncio.Event()
        self.closing: Reason | None = None
        self.verdict: Reason | None = None
        self.closed = False

    def sample(self, snapshot: Activity | None, *, terminal: bool = False) -> None:
        if self.terminal and not terminal:
            return
        self.terminal |= terminal
        if self.closed or self.closing or snapshot is None or not snapshot.process_available or not self.key.matches(snapshot):
            return
        if not terminal and self.revision == snapshot.revision:
            return
        self.revision = snapshot.revision
        self.items.observe(snapshot, int(self.clock()))
        if self.task or (not self.terminal and self.items.desired):
            self._trigger()

    def _trigger(self) -> None:
        self.generation += 1
        self.wake.set()
        if self.task is None:
            self.task = asyncio.create_task(self._drive())

    def revoke(self) -> None:
        self.closed = True
        self.ready.set()
        if self.task and not self.task.done():
            self.task.cancel()

    def finish(self, reason: Reason) -> None:
        if self.closed or self.closing:
            return
        self.closing = reason
        if self.task is None:
            self.closed = True
            self.ready.set()
            self.on_closed(self)
            return
        self._trigger()

    async def _create(self) -> bool:
        location = self.location = self.resolve()
        if location is None or location.binding.retired or (location.scope.topic_id and not (location.root and location.parent)):
            logger.warning("process message target unavailable")
            return False
        async with asyncio.timeout(self.timeout):
            created = await external_call(self.port.create(location.scope.chat_id, location.parent, bool(location.scope.topic_id)))
        if not isinstance(created, Created):
            if not isinstance(created, Rejected):
                logger.warning("process message creation unconfirmed")
            return False
        if not location.binding.reserve_process(created.cot_id, created.message_id):
            logger.warning("process message identity collision")
            return False
        async with asyncio.timeout(self.timeout):
            verified = await _verify(self.channel, location, created)
        if not verified or self.closed or not self.claim(self, created):
            logger.warning("process message location unconfirmed")
            return False
        self.created = created
        location.binding.owners[created.message_id] = location
        location.cards.add(created.message_id)
        return True

    def _can_write(self) -> bool:
        return bool(self.created and self.location and not self.closed and self.location.can_write(self.created.message_id))

    async def _write(self, payload: Payload) -> Outcome:
        if not self._can_write():
            return "unknown"
        assert self.created
        self.items.offered(payload)
        try:
            async with asyncio.timeout(self.timeout):
                result = await external_call(self.port.update(self.created.cot_id, self.created.message_id, payload))
        except Exception:
            result = None
        if isinstance(result, Ok) and self._can_write():
            self.items.confirmed(payload)
            return "confirmed"
        if isinstance(result, Rejected):
            return "unknown" if result.code == RejectionCode.CONCURRENT else "rejected"
        logger.warning("process message update unconfirmed")
        return "unknown"

    async def _drive(self) -> None:
        try:
            if not self.ready.is_set():
                try:
                    if self.terminal or self.closing or not self.items.desired or not await self._create():
                        self.closed = True
                        return
                finally:
                    self.ready.set()
            def desired() -> Payload | None:
                self.sampled = self.generation
                return self.items.desired if self._can_write() and not self.closing else None

            while self._can_write():
                await self.wake.wait()
                self.wake.clear()
                if self.closing is not None:
                    final = finish_items(self.items.desired, int(self.clock()))
                    self.writer.known = None
                    await self.writer.reconcile(lambda: final if self._can_write() else None, self._write)
                    if self._can_write():
                        assert self.created
                        async with asyncio.timeout(self.timeout):
                            result = await external_call(self.port.complete(self.created.cot_id, self.created.message_id, self.closing))
                            if not isinstance(result, (Ok, Rejected)):
                                logger.warning("process message completion unconfirmed")
                    self.closed = True
                    return
                if self.generation != self.sampled:
                    await self.writer.reconcile(desired, self._write)
        except Exception:
            self.closed = True
            logger.warning("process message operation unconfirmed")
        finally:
            self.closed = True
            self.ready.set()
            if self.location:
                self.location.retire()
            self.on_closed(self)
