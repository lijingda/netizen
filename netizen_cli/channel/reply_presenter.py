"""One best-effort owner for ordinary, Side, and Goal Reply Card delivery."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from weakref import WeakValueDictionary

from lark_channel import OutboundCard, OutboundPost, SendResult

from ..external_calls import external_call
from ..cards import turn_progress_card
from ..cards.reply import feishu_answer_text
from .reply_events import Activity, ActivityChanged, PartialAnswer, PresentationKey, ReplyTarget, TurnOpened
from .messages import _confirmed_reply_id, _send_result_error_code, _receipt_message_id
from .ports import ReplyChannel
from .topics import BindingCardLocation, CardReplyLocation
from .physical_delivery import PhysicalDelivery, receipt


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


@dataclass(slots=True)
class _PartialAnswerDelivery:
    target: ReplyTarget
    stopped: asyncio.Event
    reply_partial: Callable[[OutboundPost], Awaitable[SendResult]]
    validate_reply: Callable[[SendResult], Awaitable[bool]] | None = None
    attempted_partials: set[tuple[str, str, str]] = field(default_factory=set)
    confirmed_partials: set[tuple[str, str, str]] = field(default_factory=set)
    # A physical terminal inherits the running poll's mode and its last confirmed card.
    initial: tuple[str | None, Activity] | None = None
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
    reply_partial: Callable[[OutboundPost], Awaitable[SendResult]] | None = None
    validate_reply: Callable[[SendResult], Awaitable[bool]] | None = None
    attempted_partials: set[tuple[str, str, str]] = field(default_factory=set)
    confirmed_partials: set[tuple[str, str, str]] = field(default_factory=set)
    failed: bool = False
    task: asyncio.Task[None] | None = None


class _ReplyCardPresenter:
    """One best-effort owner for Turn, Side Turn, and Goal Reply Cards."""

    def __init__(
        self,
        channel: ReplyChannel,
        *,
        confirm_each_write: bool = False,
        poll_seconds: float = _PROGRESS_CARD_POLL_SECONDS,
        operation_timeout_seconds: float = _PROGRESS_CARD_OPERATION_TIMEOUT_SECONDS,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("progress card poll interval must be positive")
        if operation_timeout_seconds <= 0:
            raise ValueError("progress card operation timeout must be positive")
        self._channel = channel
        self._confirm_each_write = confirm_each_write
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
        self._writers: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self._card_owners: dict[str, CardReplyLocation] = {}
        self._binding_locations: dict[str, BindingCardLocation] = {}
        self._closed = False

    def binding_location(self, binding_id: str) -> BindingCardLocation:
        return self._binding_locations.setdefault(binding_id, BindingCardLocation(self._card_owners))

    def retire_binding(self, binding_id: str) -> None:
        if location := self._binding_locations.pop(binding_id, None):
            location.retire()

    @property
    def operation_timeout(self) -> float:
        return self._operation_timeout_seconds

    def physical_delivery(self, binding_id: str, thread_id: str, turn_id: str) -> PhysicalDelivery:
        session = self._sessions.get((binding_id, thread_id, turn_id))
        return session.delivery if session and session.delivery is not None else PhysicalDelivery()

    async def write_physical_card(self, message_id: str, card: OutboundCard, *, binding_id: str, turn_id: str,
                                  location: CardReplyLocation | None, publishable: Callable[[], bool],
                                  begin: Callable[[], None]) -> _CardUpdateAttempt | None:
        async with self._message_writer(message_id):
            if not publishable() or (location and not location.can_write(message_id)):
                return None
            begin()
            return await self._attempt_update_message(message_id, card, binding_id=binding_id,
                                                      operation_id=turn_id, location=location)

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
            initial = event.observe()
        except Exception:
            logger.exception("failed to read initial progress-card activity")
            return False
        if initial is None or initial.target != target or not key.matches(initial.snapshot):
            logger.error("failed to start progress card: exact Turn activity unavailable")
            return False
        snapshot = initial.exact_snapshot()
        message_id = None
        location = event.location
        delivery = PhysicalDelivery() if location else None
        if event.feedback.progress_card_enabled:
            try:
                card = turn_progress_card(snapshot=snapshot)
                async with asyncio.timeout(self._operation_timeout_seconds):
                    items = tuple(delivery.partial(p.item) for p in initial.partials if p.target == target) if delivery else ()
                    if delivery:
                        delivery.begin(items)
                    result = await external_call(event.reply(card), isolate=self._confirm_each_write)
                    if event.validate_reply is None or await external_call(event.validate_reply(result), isolate=self._confirm_each_write):
                        if location:
                            result = location.validate(result, card=True)
                        message_id = _confirmed_reply_id(result) if self._confirm_each_write else _receipt_message_id(result)
                        if delivery:
                            delivery.record(items, receipt(result))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("failed to send initial progress card")
            if message_id is None:
                logger.error("failed to start progress card: reply message ID unavailable")
        if self._closed or not event.retained() or not event.retain_after_send():
            return False
        previous = sessions.pop(key.index, None)
        if previous:
            await self._stop_session(previous)
        if self._closed or not event.retained():
            return False
        session = _TurnProgressCardSession(
            target=target, message_id=message_id, stopped=asyncio.Event(), snapshot=snapshot, observe=event.observe,
            location=location, delivery=delivery, reply_partial=event.reply_partial, validate_reply=event.validate_reply,
            attempted_partials=set() if previous is None else previous.attempted_partials,
            confirmed_partials=set() if previous is None else previous.confirmed_partials,
        )
        sessions[key.index] = session
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
                # The Claude poll bounds its SDK call. Join that write before
                # transferring its receipts and the message to the terminal writer.
                async with asyncio.timeout(None if self._confirm_each_write else self._operation_timeout_seconds):
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
            initial=(session.message_id, session.snapshot) if session else None,
            location=session.location if session else None,
            text_only=bool(session and session.failed),
        )

    async def deliver_partial_answers(
        self,
        delivery: _PartialAnswerDelivery,
        answers: tuple[PartialAnswer, ...],
        *,
        timeout_seconds: float | None = None,
    ) -> bool:
        """Attempt each missing item once within one bounded terminal delivery."""

        budget = self._operation_timeout_seconds if timeout_seconds is None else timeout_seconds
        if budget < 0:
            raise ValueError("partial-answer delivery timeout must be nonnegative")
        try:
            if budget > 0:
                async with asyncio.timeout(budget):
                    await self._deliver_partial_answers(delivery, answers, terminal=True)
        except TimeoutError:
            logger.warning("terminal partial-answer delivery budget exhausted")
        identities = {
            (part.item.thread_id, part.item.turn_id, part.item.item_id)
            for part in answers if part.target.key == delivery.target.key and part.item.text.strip()
        }
        return (not answers or bool(identities)) and identities <= delivery.confirmed_partials

    def has_card(
        self, *, owner_id: str, thread_id: str, turn_id: str, side: bool = False,
    ) -> bool:
        sessions = self._side_sessions if side else self._sessions
        session = sessions.get((owner_id, thread_id, turn_id))
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
        for part in answers:
            answer = part.item
            if session.stopped.is_set() and not terminal:
                return
            identity = (answer.thread_id, answer.turn_id, answer.item_id)
            if (
                part.target.key != session.target.key
                or not answer.text.strip()
                or identity in session.attempted_partials
            ):
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
            ledger = session.delivery if isinstance(session, _TurnProgressCardSession) else None
            if ledger:
                item = ledger.partial(answer)
                if ledger.items.get(item, "unattempted") != "unattempted":
                    continue
                ledger.begin((item,))
            try:
                async with asyncio.timeout(self._operation_timeout_seconds):
                    result = await external_call(reply(post), isolate=self._confirm_each_write)
                    if ledger:
                        ledger.record((item,), receipt(result))
                    if not _CardUpdateAttempt("", result).confirmed:
                        continue
                    if session.validate_reply is not None and not await external_call(session.validate_reply(result), isolate=self._confirm_each_write):
                        continue
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
            operation_id=session.target.key.turn_id, publishable=publishable, location=session.location,
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

        ordinary_keys = tuple(
            key
            for key in self._sessions
            if key[0] == binding_id and key[1] == thread_id
        )
        for key in ordinary_keys:
            session = self._sessions.pop(key, None)
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
            observed = session.observe()
            snapshot = (observed.exact_snapshot() if observed and observed.target == session.target
                        and session.target.key.matches(observed.snapshot) else None)
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
        while not session.stopped.is_set():
            try:
                await asyncio.wait_for(
                    session.stopped.wait(),
                    timeout=self._poll_seconds,
                )
                return
            except TimeoutError:
                pass
            if session.stopped.is_set():
                return
            try:
                change = session.observe()
            except Exception:
                logger.exception("failed to refresh running progress card", extra=log_context)
                session.failed = True
                return
            if change is None or change.target != session.target or not session.target.key.matches(change.snapshot):
                continue
            snapshot = change.exact_snapshot()
            if session.message_id is None or session.failed:
                await self._deliver_partial_answers(session, change.partials)
                if session.message_id is None:
                    session.snapshot = snapshot
                continue
            if snapshot.revision == session.snapshot.revision:
                continue
            try:
                card = turn_progress_card(snapshot=snapshot)
            except Exception:
                logger.exception("failed to render running progress card", extra=log_context)
                session.failed = True
                if session.delivery:
                    await self._deliver_partial_answers(session, change.partials)
                    continue  # Preserve the last rendered card for the terminal freeze.
                return
            if session.delivery:
                items = tuple(session.delivery.partial(p.item) for p in change.partials if p.target == session.target)
                if not await self._update(session, card, items):
                    # Never replay a running replacement with uncertain content.
                    return
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
    ) -> bool:
        if session.message_id is None:
            return False
        if not session.delivery:
            return await self.update_message(session.message_id, card, binding_id=session.target.key.owner_id,
                                             operation_id=session.target.key.turn_id, location=session.location)
        async with self._message_writer(session.message_id):
            if session.location and not session.location.can_write(session.message_id):
                session.message_id = None
                return False
            session.delivery.begin(items)
            attempt = await self._attempt_update_message(session.message_id, card, binding_id=session.target.key.owner_id,
                                                        operation_id=session.target.key.turn_id, location=session.location)
            session.delivery.record(items, receipt(attempt.result))
        if session.location and not session.location.can_write(session.message_id):
            session.message_id = None
        return attempt.confirmed

    async def update_message(
        self,
        message_id: str,
        card: OutboundCard,
        *,
        binding_id: str,
        operation_id: str,
        publishable: Callable[[], bool] = lambda: True,
        location: CardReplyLocation | None = None,
    ) -> bool:
        async with self._message_writer(message_id):
            if not publishable():
                return False
            attempt = await self._attempt_update_message(
                message_id, card, binding_id=binding_id, operation_id=operation_id, location=location,
            )
            return attempt.confirmed if self._confirm_each_write else attempt.updated

    def _message_writer(self, message_id: str) -> asyncio.Lock:
        # A waiter retains the same lock until its complete card replacement settles.
        return self._writers.setdefault(message_id, asyncio.Lock())

    async def _update_terminal_message(
        self,
        message_id: str,
        card: OutboundCard,
        *,
        binding_id: str,
        operation_id: str,
        publishable: Callable[[], bool] = lambda: True,
        location: CardReplyLocation | None = None,
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
                        binding_id=binding_id, operation_id=operation_id, location=location,
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
    ) -> _CardUpdateAttempt:
        if location and not location.can_write(message_id):
            return _CardUpdateAttempt("", None)
        try:
            async with asyncio.timeout(self._operation_timeout_seconds):
                result = await external_call(self._channel.update_card(
                    message_id,
                    card.card,
                ), isolate=self._confirm_each_write)
        except asyncio.CancelledError:
            raise
        except Exception:
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
            await external_call(task, isolate=self._confirm_each_write)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "progress card updater failed while stopping",
                extra={
                    "binding_id": getattr(session, "binding_id", None),
                    "side_id": getattr(session, "side_id", None),
                    "operation_id": getattr(
                        session,
                        "turn_id",
                        getattr(session, "logical_turn_id", "unknown"),
                    ),
                },
            )
