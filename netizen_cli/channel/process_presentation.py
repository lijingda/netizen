"""One process message's ephemeral list, placement evidence and write lifetime."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Literal

from ..domain import FeishuScope
from ..external_calls import external_call
from ..turn_activity import ACTIVITY_PLAN_LIMIT, TurnPlanStepState, sanitize_activity_text
from ..process_activity import ObservedProcessItem, process_plan_steps
from ..process_messages import (
    ITEM_LIMIT, Created, Ok, ProcessItem, ProcessMessages, Rejected, RejectionCode,
    StepItem, TextItem, ToolItem, build_items, finish_items,
)
from .messages import _nonempty_field, _object_field
from .ports import ReplyChannel
from .reply_events import Activity, PresentationKey


logger = logging.getLogger(__name__)
Reason = Literal["done", "error", "timeout"]
Payload = tuple[ProcessItem, ...]


class ProcessList:
    """Reserve a slot for each running START until its paired END is confirmed."""

    def __init__(self, key: PresentationKey) -> None:
        self.key = key
        self.pinned: dict[str, ProcessItem] = {}
        self.running_offers: dict[str, ProcessItem] = {}
        self._step_pins: dict[str, ProcessItem] = {}
        self._physical_turn_id: str | None = None
        self._window: dict[str, ProcessItem] = {}
        self.desired: Payload = ()

    def _item(self, row: ObservedProcessItem) -> ProcessItem:
        if row.kind == "step":
            text = sanitize_activity_text(row.text) or "未命名步骤"
            name = json.dumps((self.key.side, *self.key.index, text), ensure_ascii=True)
            identity = "step:" + sha256(name.encode("ascii")).hexdigest()
            return StepItem(identity, text, row.started_at, row.finished_at)
        if row.kind == "commentary":
            assert row.finished_at is not None
            return TextItem(row.identity, row.text, row.started_at, row.finished_at)
        return ToolItem(row.identity, row.text, row.started_at, row.finished_at)

    def observe(self, snapshot: Activity, now: int) -> None:
        physical = snapshot.physical_turn_id
        same_window = physical is not None and physical == self._physical_turn_id and snapshot.process_items is not None
        rollover = physical is not None and self._physical_turn_id is not None and physical != self._physical_turn_id
        if physical is not None:
            self._physical_turn_id = physical
        rows = snapshot.process_items
        if rows is None and not rollover:
            return
        # A new physical Turn may supply operations before its first checklist.
        # Only a known plan replaces the logical run's previous checklist.
        has_plan = rows is not None and snapshot.plan_available and snapshot.plan_generated
        outside = {}
        if has_plan and len(snapshot.steps) > ACTIVITY_PLAN_LIMIT:
            outside = {name: status for name, status, admit in process_plan_steps(snapshot.steps) if not admit}
        current: dict[str, ProcessItem] = {
            key: item for key, item in self._window.items() if isinstance(item, StepItem) and not has_plan
        }
        for row in rows or ():
            item = self._item(row)
            key = item.item_id
            if row.kind == "step" and not has_plan:
                # An unavailable plan cannot add or remove steps. A retained END
                # still proves completion even when both observations share a poll.
                retained = current.get(key) or self.running_offers.get(key)
                if retained is not None and row.finished_at is not None:
                    current[key] = retained.finish(row.finished_at)
                continue
            if key in current or (isinstance(item, StepItem) and item.text in outside and key not in self._step_pins):
                continue
            pinned = self._step_pins.get(key) or self.running_offers.get(key) or self.pinned.get(key)
            if pinned is not None:
                item = pinned.finish(row.finished_at) if row.finished_at is not None else pinned
            current[key] = item
        if outside:
            # A new physical window has never admitted the logical run's displaced step.
            for key, pinned_step in self._step_pins.items():
                status = outside.get(pinned_step.text)
                if status is not None and key not in current:
                    current[key] = pinned_step if status is TurnPlanStepState.IN_PROGRESS else pinned_step.finish(now)
        self._window = current
        for key, offered in self.running_offers.items():
            observed = current.get(key)
            absence_proven = has_plan if isinstance(offered, StepItem) else (same_window or rollover)
            if observed is not None:
                self.running_offers[key] = observed
            elif absence_proven:
                self.running_offers[key] = offered.finish(now)
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


@dataclass(frozen=True, slots=True)
class ProcessLocation:
    scope: FeishuScope
    parent: str | None
    root: str | None

    def with_source_root(self, source: object, *, explicit_inbound_root: bool = False) -> ProcessLocation | None:
        """Resolve the root from the exact triggering message or card."""
        expected = {"message_id": self.parent, "chat_id": self.scope.chat_id, "thread_id": self.scope.topic_id}
        for key, required in expected.items():
            actual = _object_field(source, key)
            if explicit_inbound_root and actual is None:
                continue  # The normalized inbound message already identifies this reply.
            if not required or actual != required:
                return None
        root = _object_field(source, "root_id")
        parent = _object_field(source, "parent_id")
        if root in (None, "", self.parent):
            return replace(self, root=self.parent) if parent in (None, "") else None
        if not isinstance(root, str) or not root.strip():
            return None
        if parent is not None and (not isinstance(parent, str) or parent == self.parent):
            return None
        return replace(self, root=root)


async def _read_source_location(channel: ReplyChannel, location: ProcessLocation) -> ProcessLocation | None:
    assert location.parent is not None
    response = await external_call(channel.fetch_message(location.parent))
    code = _object_field(response, "code")
    items = _object_field(_object_field(response, "data"), "items")
    if type(code) is not int or code != 0 or not isinstance(items, list) or len(items) != 1:
        return None
    source, = items
    return location.with_source_root(source) if _object_field(source, "deleted") is False else None


async def _verify(channel: ReplyChannel, location: ProcessLocation, created: Created) -> bool:
    mid = created.message_id
    if mid in (location.parent, location.root):
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
        if _object_field(actual, "deleted") is not False or any(
            _nonempty_field(actual, key) != required for key, required in expected.items()
        ):
            return False
    return True


class ProcessPresentation:
    """The presenter's poll supplies revisions. Only this task touches the process transport."""

    def __init__(self, key: PresentationKey, port: ProcessMessages,
                 channel: ReplyChannel, location: Callable[[], ProcessLocation | None],
                 clock: Callable[[], float],
                 timeout: float, on_closed: Callable[[ProcessPresentation], None] = lambda _: None) -> None:
        self.key = key
        self.port, self.channel, self.resolve = port, channel, location
        self.clock, self.timeout, self.on_closed = clock, timeout, on_closed
        self.terminal = False
        self.items = ProcessList(key)
        self.confirmed: Payload | None = None
        self.created: Created | None = None
        self.ready = asyncio.Event()
        self.task: asyncio.Task[None] | None = None
        self.revision: int | None = None
        self.generation = 0
        self.sampled = -1
        self.wake = asyncio.Event()
        self.closing: Reason | None = None
        self.closed = False

    def sample(self, snapshot: Activity | None, *, terminal: bool = False) -> None:
        if self.terminal and not terminal:
            return
        self.terminal |= terminal
        if self.closed or self.closing or snapshot is None or not self.key.matches(snapshot):
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
        location = self.resolve()
        if location is not None and location.scope.topic_id and location.parent and location.root is None:
            async with asyncio.timeout(self.timeout):
                location = await _read_source_location(self.channel, location)
        if location is None or (location.scope.topic_id and not (location.root and location.parent)):
            logger.warning("process message target unavailable")
            return False
        async with asyncio.timeout(self.timeout):
            if self.terminal or self.closing or self.closed:
                return False
            created = await external_call(self.port.create(location.scope.chat_id, location.parent, bool(location.scope.topic_id)))
        if not isinstance(created, Created):
            if not isinstance(created, Rejected):
                logger.warning("process message creation unconfirmed")
            return False
        async with asyncio.timeout(self.timeout):
            verified = await _verify(self.channel, location, created)
        if not verified or self.closed:
            logger.warning("process message location unconfirmed")
            return False
        self.created = created
        return True

    def _can_write(self) -> bool:
        return self.created is not None and not self.closed

    async def _write(self, payload: Payload) -> Literal["confirmed", "unknown", "rejected"]:
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
            if self.closed or self.terminal or self.closing or not self.items.desired or not await self._create():
                return
            self.ready.set()
            while self._can_write():
                await self.wake.wait()
                self.wake.clear()
                if self.closing is not None:
                    final = finish_items(self.items.desired, int(self.clock()))
                    await self._write(final)
                    if self._can_write():
                        assert self.created
                        async with asyncio.timeout(self.timeout):
                            result = await external_call(self.port.complete(self.created.cot_id, self.created.message_id, self.closing))
                            if not isinstance(result, (Ok, Rejected)):
                                logger.warning("process message completion unconfirmed")
                    return
                if self.generation == self.sampled:
                    continue
                while self._can_write() and not self.closing:
                    self.sampled = self.generation
                    payload = self.items.desired
                    if payload == self.confirmed:
                        break
                    outcome = await self._write(payload)
                    if outcome == "confirmed":
                        self.confirmed = payload
                    else:
                        if outcome == "unknown":
                            self.confirmed = None
                        break
        except Exception:
            logger.warning("process message operation unconfirmed")
        finally:
            self.closed = True
            self.ready.set()
            self.on_closed(self)
