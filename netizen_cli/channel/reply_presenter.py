"""One best-effort owner for ordinary, Side, and Goal Reply Card delivery."""

from __future__ import annotations

import asyncio
import logging
from time import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from weakref import WeakKeyDictionary, WeakValueDictionary

from lark_channel import OutboundCard, OutboundPost, SendResult

from ..process_messages import Created, ProcessMessages
from ..external_calls import external_call
from ..cards import reply_card, turn_progress_card
from ..domain import ReplyCardProjection
from ..ordinary_sessions import LogicalReply
from ..cards.reply import feishu_answer_text, turn_progress_projection
from ..partial_answers import PartialAnswer
from .process_presentation import ProcessPresentation, Reason
from .replacement_writer import ReplacementWriter
from .reply_events import Activity, ActivityChanged, DeliveryPolicy, PresentationKey, ReplyTarget, RunDisplay, TerminalStatus, TurnOpened, TurnResult
from .messages import _confirmed_reply_id, _send_result_error_code, _receipt_message_id
from .ports import ReplyChannel
from .topics import BindingCardLocation, CardReplyLocation
from .physical_delivery import BODY, Item, Outcome, PhysicalDelivery, record_sent_answer


logger = logging.getLogger(__name__)

_PROGRESS_CARD_POLL_SECONDS = 1.0
_PROGRESS_CARD_OPERATION_TIMEOUT_SECONDS = 5.0
_PROGRESS_CARD_MAX_CONSECUTIVE_FAILURES = 3
_TERMINAL_CARD_UPDATE_ATTEMPTS = 3
_TERMINAL_CARD_RETRY_SECONDS = 0.5


@dataclass(frozen=True, slots=True)
class _CardUpdateAttempt:
    """One public update call, retaining its response for terminal handoff."""

    message_id: str
    result: SendResult | None

    @property
    def updated(self) -> bool:
        # This is the existing presentation success projection, not proof of
        # an exact destination or of a failed request having no side effects.
        return self.result is not None and self.result.success

    @property
    def confirmed(self) -> bool:
        if self.result is None:
            return False
        code = self.result.raw.get("code") if self.result.raw is not None else None
        return self.result.success and (code is None or code == 0)


class _CardWriter(ReplacementWriter[OutboundCard]):
    def __init__(self) -> None:
        super().__init__(lambda card: card.card)


@dataclass(slots=True)
class _PublishedCard:
    key: PresentationKey
    location: CardReplyLocation
    content: ReplyCardProjection
    writer: _CardWriter
    tracks_run: bool
    delivery: PhysicalDelivery
    items: tuple[Item, ...]


def render_run_card(content: ReplyCardProjection, line: str | None) -> OutboundCard:
    activity = content.activity
    assert activity is not None
    return reply_card(replace(content, activity=replace(activity, state_line=line)))


def _exact(change: ActivityChanged | None, target: ReplyTarget) -> ActivityChanged | None:
    if change and change.target == target and target.key.matches(change.snapshot):
        return change
    return None


@dataclass(slots=True)
class _PartialAnswerDelivery:
    target: ReplyTarget
    stopped: asyncio.Event
    reply_partial: Callable[[OutboundPost], Awaitable[SendResult]]
    validate_reply: Callable[[SendResult], Awaitable[bool]] | None = None
    policy: DeliveryPolicy = DeliveryPolicy.BOUNDED_FALLBACK
    attempted_partials: set[tuple[str, str, str]] = field(default_factory=set)
    confirmed_partials: set[tuple[str, str, str]] = field(default_factory=set)
    message_id: str | None = None
    freeze_snapshot: Activity | None = None
    text_only: bool = False
    location: CardReplyLocation | None = None


@dataclass(slots=True)
class _TurnProgressCardSession:
    target: ReplyTarget
    message_id: str | None
    stopped: asyncio.Event
    snapshot: Activity
    observe: Callable[[], ActivityChanged | None]
    location: CardReplyLocation | None = None
    delivery: PhysicalDelivery | None = None
    run_reply: LogicalReply | None = None
    writer: _CardWriter | None = None
    reply_partial: Callable[[OutboundPost], Awaitable[SendResult]] | None = None
    validate_reply: Callable[[SendResult], Awaitable[bool]] | None = None
    policy: DeliveryPolicy = DeliveryPolicy.BOUNDED_FALLBACK
    attempted_partials: set[tuple[str, str, str]] = field(default_factory=set)
    confirmed_partials: set[tuple[str, str, str]] = field(default_factory=set)
    failed: bool = False
    task: asyncio.Task[None] | None = None


class _ReplyCardPresenter:
    """In-memory progress cards and delivery receipts for exact presentation turns."""

    def __init__(
        self,
        channel: ReplyChannel,
        *,
        process_messages: ProcessMessages | None = None,
        process_location: Callable[[str, object], CardReplyLocation | None] | None = None,
        clock: Callable[[], float] = time,
        poll_seconds: float = _PROGRESS_CARD_POLL_SECONDS,
        operation_timeout_seconds: float = _PROGRESS_CARD_OPERATION_TIMEOUT_SECONDS,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("progress card poll interval must be positive")
        if operation_timeout_seconds <= 0:
            raise ValueError("progress card operation timeout must be positive")
        self._channel = channel
        self._process_port, self._process_location, self._clock = process_messages, process_location, clock
        self._process: dict[PresentationKey, ProcessPresentation] = {}
        self._poll_seconds = poll_seconds
        self._operation_timeout_seconds = operation_timeout_seconds
        self._sessions: dict[
            tuple[str, str, str],
            _TurnProgressCardSession,
        ] = {}
        self._side_sessions: dict[
            tuple[str, str, str],
            _TurnProgressCardSession,
        ] = {}
        self._writers: WeakValueDictionary[str, _CardWriter] = WeakValueDictionary()
        self._card_owners: dict[str, CardReplyLocation] = {}
        self._binding_locations: dict[str, BindingCardLocation] = {}
        self._published: WeakKeyDictionary[LogicalReply, dict[str, _PublishedCard]] = WeakKeyDictionary()
        self._closed = False

    def _process_for(self, target: ReplyTarget, binding_id: str) -> ProcessPresentation | None:
        if self._closed or self._process_port is None or self._process_location is None:
            return None
        if state := self._process.get(target.key):
            return state
        resolve = self._process_location
        state = ProcessPresentation(target.key, target.run, self._process_port, self._channel,
            lambda: resolve(binding_id, target.origin), self._claim_process, self._clock, self.operation_timeout)
        self._process[target.key] = state
        return state

    def _claim_process(self, state: ProcessPresentation, created: Created) -> bool:
        for other in self._process.values():
            if other is state or not other.created:
                continue
            if created.cot_id == other.created.cot_id or created.message_id == other.created.message_id:
                for owner, mid in ((state, created.message_id), (other, other.created.message_id)):
                    if owner.location:
                        owner.location.binding.unusable.add(mid)
                return False
        for other in self._process.values():
            if other is not state and state.run is not None and other.run == state.run and other.created:
                other.finish(other.verdict)
        return True

    async def process_result(self, result: TurnResult) -> None:
        if result.task_feedback.progress_card_enabled or not result.process_enabled:
            return
        if result.target.run is None and result.status == "completed" and not (result.content or result.partial_answers):
            return
        state = self._process_for(result.target, result.file_binding_id)
        if state is None:
            return
        try:
            state.verdict = result.process_reason
            state.sample(result.activity, terminal=True)
            if not result.run_continues:
                state.finish(state.verdict)
            await state.before_content()
        except Exception:
            state.revoke()
            logger.warning("process message result unavailable")

    async def release_process(self, key: PresentationKey) -> None:
        state = self._process.get(key)
        if state and (state.closing or state.closed):
            if state.task:
                await asyncio.gather(state.task, return_exceptions=True)
            if self._process.get(key) is state:
                self._process.pop(key)

    async def end_process_run(self, run: PresentationKey, reason: Reason) -> None:
        matches = [state for state in self._process.values() if state.run == run]
        for state in matches:
            state.finish(reason if reason == "timeout" or state.revision is None else state.verdict)
        await asyncio.gather(*(self.release_process(state.key) for state in matches))

    def unknown_process(self, key: PresentationKey) -> None:
        # Keep the spent create on this parked Turn until exact recovery or removal.
        state = self._process.get(key)
        if state is None:
            state = self._process_for(ReplyTarget(key, None, key), key.owner_id)
        if state:
            state.finish("timeout")

    async def publish_card(
        self, display: RunDisplay, message_id: str, key: PresentationKey, location: CardReplyLocation,
        content: ReplyCardProjection, *, delivery: PhysicalDelivery, items: tuple[Item, ...],
        confirmed: OutboundCard | None = None, tracks_run: bool = True,
    ) -> bool:
        if not location.can_write(message_id) or (display.reply and display.reply.messages is None):
            return False
        writer = self._message_writer(message_id)
        if confirmed is not None:
            writer.known = confirmed
        cards = self._published.setdefault(display.reply, {}) if display.reply else {}
        cards[message_id] = _PublishedCard(key, location, content, writer, tracks_run, delivery, items)
        return (await self._reconcile_cards(display, cards))[message_id]

    async def reconcile(self, display: RunDisplay) -> dict[str, bool]:
        cards = self._published.get(display.reply, {}) if display.reply else {}
        return await self._reconcile_cards(display, cards)

    def retire_run(self, reply: LogicalReply) -> None:
        cards = self._published.pop(reply, {})
        for view in cards.values():
            view.writer.known = None
        cards.clear()

    def _release_cards(self, ids: set[str]) -> None:
        if not ids:
            return
        for mid in ids:
            if writer := self._writers.get(mid):
                writer.known = None
        for cards in self._published.values():
            for mid in ids.intersection(cards):
                del cards[mid]

    async def finish_run(self, display: RunDisplay, status: TerminalStatus) -> None:
        """Stop this input's polls, including turns that never finished Result preparation."""
        sessions = [self._sessions.pop(index) for index, session in tuple(self._sessions.items())
                    if session.run_reply is display.reply]
        for session in sessions:
            session.stopped.set()

        async def close_card(session: _TurnProgressCardSession) -> None:
            await self._stop_session(session)
            if not (display.publishable() and session.message_id and session.location and session.writer
                    and session.location.can_write(session.message_id) and session.delivery and display.reply
                    and display.reply.messages is not None):
                return
            content = turn_progress_projection(snapshot=session.snapshot, terminal_status=status,
                collapsed=True, include_result=status != "completed")
            items = tuple(("partial", part.item_id) for part in session.snapshot.partial_answers)
            cards = self._published.setdefault(display.reply, {})
            cards[session.message_id] = _PublishedCard(session.target.key, session.location, content,
                session.writer, True, session.delivery, (*items, BODY) if content.result else items)

        if sessions:
            await asyncio.gather(*(close_card(session) for session in sessions))
        if display.publishable():
            await self.reconcile(display)

    async def _write_card(self, mid: str, card: OutboundCard, key: PresentationKey, location: CardReplyLocation | None,
                          delivery: PhysicalDelivery, items: tuple[Item, ...], reply: LogicalReply | None) -> Outcome:
        if location and not location.can_write(mid):
            return "unknown"

        async def update() -> SendResult | None:
            attempt = await self._attempt_update_message(mid, card, binding_id=key.owner_id,
                operation_id=key.turn_id, location=location, policy=DeliveryPolicy.ITEM_RECEIPTS)
            return attempt.result

        outcome, _ = await delivery.send(items, reply, update())
        if (location and not location.can_write(mid)) or (reply and reply.messages is None):
            return "unknown"
        if outcome == "confirmed" and reply and location:
            reply.published(mid, key.turn_id, items, location.usable)
        return outcome

    async def _reconcile_cards(self, display: RunDisplay, cards: dict[str, _PublishedCard]) -> dict[str, bool]:
        reply = display.reply

        def newest() -> str | None:
            order = reply.messages if reply and reply.messages is not None else cards
            return next((mid for mid in reversed(order) if mid in cards and cards[mid].tracks_run), None)

        async def reconcile_message(mid: str, view: _PublishedCard) -> bool:
            def desired() -> OutboundCard | None:
                if ((reply and self._published.get(reply) is not cards) or cards.get(mid) is not view
                        or not display.publishable() or not view.location.can_write(mid)):
                    return None
                return render_run_card(view.content, display.current() if mid == newest() else None)

            def write(card: OutboundCard) -> Awaitable[Outcome]:
                return self._write_card(mid, card, view.key, view.location, view.delivery, view.items, reply)

            try:
                current = await external_call(view.writer.reconcile(desired, write))
                if not view.location.can_write(mid):
                    return False
                if current and view.writer.known is not None and mid != newest() and cards.get(mid) is view:
                    cards.pop(mid)  # A confirmed, immutable card no longer needs a writer.
                    view.writer.known = None
                return current
            except Exception:
                logger.warning("optional reply update failed", extra=view.key.log_context)
                return False

        pending = tuple(cards.items())
        results = await asyncio.gather(*(reconcile_message(mid, view) for mid, view in pending))
        return {mid: result for (mid, _), result in zip(pending, results, strict=True)}

    def binding_location(self, binding_id: str) -> BindingCardLocation:
        return self._binding_locations.setdefault(binding_id, BindingCardLocation(self._card_owners, release_cards=self._release_cards))

    def retire_binding(self, binding_id: str) -> None:
        location = self._binding_locations.pop(binding_id, None)
        for key, process in tuple(self._process.items()):
            if key.owner_id == binding_id or (location and process.location and process.location.binding is location):
                process.revoke()
                self._process.pop(key)
        if location:
            location.retire()

    @property
    def operation_timeout(self) -> float:
        return self._operation_timeout_seconds

    def physical_delivery(self, key: PresentationKey) -> PhysicalDelivery:
        session = self._sessions_for(key).get(key.index)
        return session.delivery if session and session.delivery is not None else PhysicalDelivery()

    def _sessions_for(self, key: PresentationKey) -> dict[tuple[str, str, str], _TurnProgressCardSession]:
        return self._side_sessions if key.side else self._sessions

    async def open(self, event: TurnOpened) -> bool:
        target = event.target
        key = target.key
        sessions = self._sessions_for(key)
        if self._closed or not event.retained():
            return False
        if event.reuse_existing and key.index in sessions:
            return True
        try:
            initial = _exact(event.observe(), target)
        except Exception:
            logger.exception("failed to read initial progress-card activity", extra=key.log_context)
            return False
        if initial is None:
            logger.error("failed to start progress card: exact Turn activity unavailable", extra=key.log_context)
            return False
        snapshot = initial.snapshot
        message_id = None
        writer = None
        location = event.location
        delivery = PhysicalDelivery() if event.policy.confirms_items else None
        if event.feedback.progress_card_enabled:
            try:
                card = turn_progress_card(snapshot=snapshot)

                async def send_card() -> SendResult | None:
                    sent = await external_call(event.reply(card), isolate=event.policy.confirms_items)
                    if event.validate_reply and not await external_call(event.validate_reply(sent), isolate=event.policy.confirms_items):
                        return None
                    return location.validate(sent, card=True) if location else sent

                async with asyncio.timeout(self._operation_timeout_seconds):
                    if delivery:
                        items = tuple(delivery.partial(item) for item in snapshot.partial_answers)
                        _, result = await delivery.send(items, event.run_reply, send_card())
                        message_id = _confirmed_reply_id(result) if result is not None else None
                        if message_id:
                            writer = self._message_writer(message_id)
                            writer.known = card
                            if event.run_reply and location:
                                event.run_reply.published(message_id, key.turn_id, items, location.usable)
                    else:
                        result = await send_card()
                        message_id = _receipt_message_id(result) if result is not None else None
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("failed to send initial progress card", extra=key.log_context)
            if message_id is None:
                logger.error("failed to start progress card: reply message ID unavailable", extra=key.log_context)
        if self._closed or not event.retained() or not event.retain_after_send():
            return False
        previous = sessions.pop(key.index, None)
        if previous:
            await self._stop_session(previous)
        if self._closed or not event.retained():
            return False
        session = _TurnProgressCardSession(
            target=target, message_id=message_id, stopped=asyncio.Event(), snapshot=snapshot, observe=event.observe,
            location=location, delivery=delivery, run_reply=event.run_reply, writer=writer,
            policy=event.policy, reply_partial=event.reply_partial, validate_reply=event.validate_reply,
            attempted_partials=set() if previous is None else previous.attempted_partials,
            confirmed_partials=set() if previous is None else previous.confirmed_partials,
        )
        sessions[key.index] = session
        if not event.feedback.progress_card_enabled and event.process_enabled:
            try:
                state = self._process_for(target, event.process_binding_id or key.owner_id)
                if state:
                    state.sample(snapshot)
            except Exception:
                logger.warning("process message observation unavailable")
        session.task = asyncio.create_task(self._poll(session), name=f"netizen-progress-card-{key.turn_id}")
        return True

    async def prepare_partial_answers(
        self,
        *,
        target: ReplyTarget,
        reply: Callable[[OutboundPost], Awaitable[SendResult]],
        validate_reply: Callable[[SendResult], Awaitable[bool]] | None = None,
    ) -> _PartialAnswerDelivery:
        """Quiesce running delivery and retain receipts before trying the terminal card."""

        session = self._sessions_for(target.key).get(target.key.index)
        if session is not None:
            try:
                # An item-receipted poll bounds its SDK call. Join that write before
                # transferring its receipts and the message to the terminal writer.
                async with asyncio.timeout(None if session.policy.confirms_items else self._operation_timeout_seconds):
                    await self._stop_session(session)
            except TimeoutError:
                # Cancellation affects only this local presentation task. Its
                # attempted item remains uncertain and must never be replayed.
                session.task = None
                logger.warning("running partial-answer delivery stop budget exhausted")
        return _PartialAnswerDelivery(
            target=target,
            stopped=asyncio.Event(), reply_partial=reply,
            validate_reply=validate_reply if validate_reply is not None else (
                None if session is None else session.validate_reply
            ),
            attempted_partials=set() if session is None else session.attempted_partials,
            confirmed_partials=set() if session is None else session.confirmed_partials,
            message_id=session.message_id if session else None,
            freeze_snapshot=session.snapshot if session and session.writer else None,
            location=session.location if session else None,
            policy=session.policy if session else DeliveryPolicy.BOUNDED_FALLBACK,
            text_only=bool(session and (session.failed or session.message_id is None)),
        )

    async def deliver_partial_answers(
        self,
        delivery: _PartialAnswerDelivery,
        result: TurnResult,
        *,
        timeout_seconds: float | None = None,
    ) -> bool:
        """Attempt each missing item once within one bounded terminal delivery."""

        if result.target != delivery.target:
            return False
        budget = self._operation_timeout_seconds if timeout_seconds is None else timeout_seconds
        if budget < 0:
            raise ValueError("partial-answer delivery timeout must be nonnegative")
        try:
            if budget > 0:
                async with asyncio.timeout(budget):
                    await self._deliver_partial_answers(delivery, result.partial_answers, terminal=True)
        except TimeoutError:
            logger.warning("terminal partial-answer delivery budget exhausted")
        identities = {(item.thread_id, item.turn_id, item.item_id) for item in result.partial_answers}
        return (not result.partials or bool(identities)) and identities <= delivery.confirmed_partials

    def has_card(self, key: PresentationKey) -> bool:
        session = self._sessions_for(key).get(key.index)
        return session is not None and session.message_id is not None

    async def _deliver_partial_answers(
        self,
        session: _PartialAnswerDelivery | _TurnProgressCardSession,
        answers: tuple[PartialAnswer, ...],
        *,
        terminal: bool = False,
    ) -> None:
        reply = session.reply_partial
        if reply is None:
            return
        ledger, run_reply = (session.delivery, session.run_reply) if isinstance(session, _TurnProgressCardSession) else (None, None)
        for answer in answers:
            if session.stopped.is_set() and not terminal:
                return
            identity = (answer.thread_id, answer.turn_id, answer.item_id)
            if identity in session.attempted_partials:
                continue
            try:
                post = OutboundPost(markdown=f"**阶段性答案**\n\n{feishu_answer_text(answer.text)}")
            except Exception:
                # No transport call has happened: final delivery may retry
                # this known unpublished item after rendering recovers.
                logger.exception("could not format partial answer")
                continue
            # Mark before I/O: a timeout or cancellation may follow publication.
            # Even an SDK failure may hide a partially delivered long post.
            session.attempted_partials.add(identity)
            if ledger:
                item = ledger.partial(answer)
                if ledger.items.get(item, "unattempted") != "unattempted":
                    continue
            try:
                async with asyncio.timeout(self._operation_timeout_seconds):
                    operation = external_call(reply(post), isolate=session.policy.confirms_items)
                    if ledger:
                        outcome, result = await ledger.send((item,), run_reply, operation, post=True)
                        if outcome != "confirmed" or result is None:
                            continue
                    else:
                        result = await operation
                        if not _CardUpdateAttempt("", result).confirmed:
                            continue
                    if session.validate_reply is not None and not await external_call(session.validate_reply(result), isolate=session.policy.confirms_items):
                        continue
                    if ledger and session.location:
                        record_sent_answer(run_reply, session.location, answer.turn_id, (item,), result)
                    session.confirmed_partials.add(identity)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("partial-answer delivery was not confirmed")
                continue

    async def finish(
        self,
        *,
        key: PresentationKey,
        activity: Activity | None,
        render: Callable[[Activity], OutboundCard],
        publishable: Callable[[], bool] = lambda: True,
    ) -> _CardUpdateAttempt | None:
        session = self._sessions_for(key).pop(key.index, None)
        if session is None:
            return None
        await self._stop_session(session)
        if session.message_id is None:
            return None
        snapshot = activity or session.snapshot
        if not key.matches(snapshot):
            logger.error("failed to finish progress card: activity identity mismatch")
            return None
        try:
            card = render(snapshot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "failed to render terminal progress card",
                extra={
                    "binding_id": session.target.key.owner_id,
                    "turn_id": session.target.key.turn_id,
                },
            )
            return None
        return await self._update_terminal_message(
            session.message_id,
            card,
            binding_id=session.target.key.owner_id,
            operation_id=session.target.key.turn_id, publishable=publishable, location=session.location, policy=session.policy,
        )

    async def abandon(
        self,
        *,
        key: PresentationKey,
    ) -> None:
        session = self._sessions_for(key).pop(key.index, None)
        if session is not None:
            await self._stop_session(session)

    async def abandon_thread(
        self,
        *,
        binding_id: str,
        thread_id: str,
    ) -> None:
        """Stop every ordinary/Goal presenter owned by one removed Thread."""

        for key in tuple(self._process):
            if (key.owner_id, key.thread_id) == (binding_id, thread_id):
                state = self._process.pop(key)
                state.revoke()
                if state.task:
                    await asyncio.gather(state.task, return_exceptions=True)
        ordinary_keys = tuple(
            key
            for key in self._sessions
            if key[0] == binding_id and key[1] == thread_id
        )
        for index in ordinary_keys:
            session = self._sessions.pop(index, None)
            if session is not None:
                await self._stop_session(session)

    async def park_unavailable(
        self,
        *,
        binding_id: str,
        thread_id: str,
        turn_id: str,
    ) -> bool:
        """Render one unavailable snapshot and stop all future card polling."""

        session = self._sessions.pop((binding_id, thread_id, turn_id), None)
        if session is None:
            return False
        await self._stop_session(session)
        if session.message_id is None:
            # Keep exact item receipts for a later authoritative completion.
            # No running task remains, so unavailable observation does no I/O.
            self._sessions[(binding_id, thread_id, turn_id)] = session
            return False
        if session.failed:
            return False
        try:
            observed = _exact(session.observe(), session.target)
            snapshot = observed.snapshot if observed else None
        except Exception:
            logger.exception(
                "failed to read unavailable progress-card activity",
                extra={"binding_id": binding_id, "turn_id": turn_id},
            )
            return False
        if snapshot is None:
            return False
        try:
            return await self._update(
                session,
                turn_progress_card(snapshot=snapshot),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "failed to render unavailable progress card",
                extra={"binding_id": binding_id, "turn_id": turn_id},
            )
            return False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        processes = tuple(self._process.values())
        self._process.clear()
        for process in processes:
            process.revoke()
        await asyncio.gather(*(state.task for state in processes if state.task), return_exceptions=True)
        for binding_id in tuple(self._binding_locations):
            self.retire_binding(binding_id)
        sessions = tuple(self._sessions.values())
        self._sessions.clear()
        side_sessions = tuple(self._side_sessions.values())
        self._side_sessions.clear()
        await asyncio.gather(
            *(
                self._stop_session(session)
                for session in (*sessions, *side_sessions)
            ),
            return_exceptions=False,
        )

    async def _poll(self, session: _TurnProgressCardSession) -> None:
        log_context = {"owner_id": session.target.key.owner_id, "turn_id": session.target.key.turn_id}
        failures = 0
        observed_revision = session.snapshot.revision
        read_now = False
        while not session.stopped.is_set():
            if not read_now:
                try:
                    await asyncio.wait_for(session.stopped.wait(), timeout=self._poll_seconds)
                    return
                except TimeoutError:
                    pass
            read_now = False
            if session.stopped.is_set():
                return
            try:
                change = _exact(session.observe(), session.target)
            except Exception:
                logger.exception("failed to refresh running progress card", extra=log_context)
                session.failed = True
                return
            if change is None:
                continue
            snapshot = change.snapshot
            if process := self._process.get(session.target.key):
                try:
                    process.sample(snapshot)
                except Exception:
                    logger.warning("process message observation unavailable")
                    process.revoke()
            if session.message_id is None or session.failed:
                await self._deliver_partial_answers(session, snapshot.partial_answers)
                if session.message_id is None:
                    session.snapshot = snapshot
                continue
            if snapshot.revision == (observed_revision if session.delivery else session.snapshot.revision):
                continue
            observed_revision = snapshot.revision
            try:
                card = turn_progress_card(snapshot=snapshot)
            except Exception:
                logger.exception("failed to render running progress card", extra=log_context)
                session.failed = True
                if session.delivery:
                    await self._deliver_partial_answers(session, snapshot.partial_answers)
                    continue  # Preserve the last rendered card for the terminal freeze.
                return
            if session.delivery:
                items = tuple(session.delivery.partial(item) for item in snapshot.partial_answers)
                # A write may span several native changes. Sample again immediately;
                # the same observed revision does not trigger a retry.
                read_now = True
                if not await self._update(session, card, items, snapshot=snapshot):
                    continue
            elif not await self._update(session, card):
                failures += 1
                if failures >= _PROGRESS_CARD_MAX_CONSECUTIVE_FAILURES:
                    session.failed = True
                    return
                continue
            session.snapshot = snapshot
            failures = 0

    async def _update(
        self,
        session: _TurnProgressCardSession,
        card: OutboundCard,
        items: tuple[tuple[str, str], ...] = (),
        *,
        snapshot: Activity | None = None,
    ) -> bool:
        if session.message_id is None:
            return False
        if not session.delivery:
            return await self.update_message(session.message_id, card, binding_id=session.target.key.owner_id,
                                             operation_id=session.target.key.turn_id, location=session.location, policy=session.policy)
        writer, delivery, mid = session.writer, session.delivery, session.message_id
        assert writer is not None

        async def write(desired: OutboundCard) -> Outcome:
            outcome = await self._write_card(mid, desired, session.target.key, session.location, delivery, items, session.run_reply)
            if snapshot is not None and outcome != "rejected":
                session.snapshot = snapshot  # These partials may now be visible, even if the receipt was lost.
            return outcome

        confirmed = await writer.reconcile(lambda: None if session.stopped.is_set() else card, write)
        if confirmed:
            delivery.record(items, "confirmed")
        if session.location and not session.location.can_write(mid):
            session.message_id = None
        return confirmed

    async def update_message(
        self,
        message_id: str,
        card: OutboundCard,
        *,
        binding_id: str,
        operation_id: str,
        publishable: Callable[[], bool] = lambda: True,
        location: CardReplyLocation | None = None,
        policy: DeliveryPolicy,
    ) -> bool:
        async with self._message_writer(message_id):
            if not publishable():
                return False
            attempt = await self._attempt_update_message(
                message_id, card, binding_id=binding_id, operation_id=operation_id, location=location, policy=policy,
            )
            return attempt.confirmed if policy.confirms_items else attempt.updated

    def _message_writer(self, message_id: str) -> _CardWriter:
        # A waiter retains the same lock until its complete card replacement settles.
        return self._writers.setdefault(message_id, _CardWriter())

    async def _update_terminal_message(
        self,
        message_id: str,
        card: OutboundCard,
        *,
        binding_id: str,
        operation_id: str,
        publishable: Callable[[], bool] = lambda: True,
        location: CardReplyLocation | None = None,
        policy: DeliveryPolicy,
    ) -> _CardUpdateAttempt:
        # Retry only replacement of the same frozen card, never a new reply.
        # All attempts and delays share the existing terminal update budget.
        uncertain = False
        try:
            async with asyncio.timeout(self._operation_timeout_seconds), self._message_writer(message_id):
                for index in range(_TERMINAL_CARD_UPDATE_ATTEMPTS):
                    if index:
                        await asyncio.sleep(_TERMINAL_CARD_RETRY_SECONDS)
                    if not publishable():
                        return _CardUpdateAttempt(message_id, None)
                    attempt = await self._attempt_update_message(
                        message_id, card,
                        binding_id=binding_id, operation_id=operation_id, location=location, policy=policy,
                    )
                    if attempt.updated or not attempt.message_id:
                        return attempt
                    code = _send_result_error_code(attempt.result)
                    uncertain |= code is None or code <= 0
                # A later rejection cannot disprove an earlier lost response.
                return _CardUpdateAttempt(message_id, None) if uncertain else attempt
        except TimeoutError:
            logger.warning(
                "terminal progress-card update budget exhausted",
                extra={
                    "binding_id": binding_id,
                    "operation_id": operation_id,
                    "message_id": message_id,
                },
            )
            # The last request may have reached Feishu before cancellation.
            return _CardUpdateAttempt(message_id, None)

    async def _attempt_update_message(
        self,
        message_id: str,
        card: OutboundCard,
        *,
        binding_id: str,
        operation_id: str,
        location: CardReplyLocation | None = None,
        policy: DeliveryPolicy,
    ) -> _CardUpdateAttempt:
        if location and not location.can_write(message_id):
            return _CardUpdateAttempt("", None)
        try:
            async with asyncio.timeout(self._operation_timeout_seconds):
                result = await external_call(self._channel.update_card(
                    message_id,
                    card.card,
                ), isolate=policy.confirms_items)
        except asyncio.CancelledError:
            raise
        except Exception:
            if policy.confirms_items:
                raise  # The physical delivery ledger classifies this transport exception.
            logger.exception(
                "failed to update progress card",
                extra={
                    "binding_id": binding_id,
                    "operation_id": operation_id,
                    "message_id": message_id,
                },
            )
            return _CardUpdateAttempt(message_id, None)
        if location:
            result = location.validate(result, card=True, updated_id=message_id)
        attempt = _CardUpdateAttempt(message_id, result)
        if not attempt.updated:
            logger.error(
                "failed to update progress card: unsuccessful result",
                extra={
                    "binding_id": binding_id,
                    "operation_id": operation_id,
                    "message_id": message_id,
                },
            )
        return attempt

    async def _stop_session(
        self,
        session: _TurnProgressCardSession,
    ) -> None:
        session.stopped.set()
        task = session.task
        if task is None or task is asyncio.current_task():
            return
        try:
            await external_call(task, isolate=session.policy.confirms_items)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "progress card updater failed while stopping",
                extra=session.target.key.log_context,
            )
