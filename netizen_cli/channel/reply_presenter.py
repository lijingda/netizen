"""One best-effort owner for ordinary, Side, and Goal Reply Card delivery."""

from __future__ import annotations

import asyncio
import logging
from time import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from weakref import WeakValueDictionary

from lark_channel import OutboundCard, OutboundPost, SendResult

from ..process_messages import PROCESS_OPERATION_TIMEOUT_SECONDS, ProcessMessages
from ..cards import turn_progress_card
from ..cards.reply import feishu_answer_text
from ..partial_answers import PartialAnswer
from .process_presentation import ProcessLocation, ProcessPresentation, Reason
from .reply_events import Activity, ActivityChanged, PresentationKey, ReplyTarget, TurnOpened, TurnResult
from .messages import _send_result_error_code, _receipt_message_id
from .ports import ReplyChannel


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
    attempted_partials: set[tuple[str, str, str]] = field(default_factory=set)
    confirmed_partials: set[tuple[str, str, str]] = field(default_factory=set)


@dataclass(slots=True)
class _TurnProgressCardSession:
    target: ReplyTarget
    message_id: str | None
    stopped: asyncio.Event
    snapshot: Activity
    observe: Callable[[], ActivityChanged | None]
    reply_partial: Callable[[OutboundPost], Awaitable[SendResult]] | None = None
    validate_reply: Callable[[SendResult], Awaitable[bool]] | None = None
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
        process_location: Callable[[object, str | None], ProcessLocation | None] | None = None,
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
        self._claimed_cot_ids: set[str] = set()
        self._process_tombstones: set[PresentationKey] = set()
        self._poll_seconds = poll_seconds
        self._operation_timeout_seconds = operation_timeout_seconds
        self._sessions: dict[PresentationKey, _TurnProgressCardSession] = {}
        self._writers: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self._closed = False

    def _process_for(self, target: ReplyTarget, root_id: str | None = None) -> ProcessPresentation | None:
        if (not target.key.side and not target.key.owner_id) or target.key in self._process_tombstones or self._closed or self._process_port is None or self._process_location is None:
            return None
        if state := self._process.get(target.key):
            return state
        resolve = self._process_location
        state = ProcessPresentation(target.key, self._process_port, self._channel,
            lambda: resolve(target.origin, root_id), self._clock, PROCESS_OPERATION_TIMEOUT_SECONDS,
            self._forget_process, claimed_cot_ids=self._claimed_cot_ids)
        self._process[target.key] = state
        return state


    def _forget_process(self, state: ProcessPresentation) -> None:
        if self._process.get(state.key) is state:
            self._process.pop(state.key)
            if not state.terminal:
                self._process_tombstones.add(state.key)


    def process_result(self, key: PresentationKey, reason: Reason, activity: Activity | None) -> None:
        self._process_tombstones.discard(key)
        state = self._process.get(key)
        if state is None or state.closing in ("done", "error"):
            return  # A queued publication may follow an already-proven physical terminal.
        try:
            state.sample(activity, terminal=True)
        except Exception:
            logger.warning("process message result observation unavailable")
        state.finish(reason)

    def end_process_run(self, run: PresentationKey, reason: Reason) -> None:
        confirmed = reason != "timeout"
        if confirmed:
            self._process_tombstones.discard(run)
        if state := self._process.get(run):
            state.terminal |= confirmed
            state.finish(reason)

    def tombstone_unknown_process(self, key: PresentationKey) -> None:
        self._process_tombstones.add(key)
        if state := self._process.get(key):
            state.finish("timeout")


    @property
    def operation_timeout(self) -> float:
        return self._operation_timeout_seconds


    async def open(self, event: TurnOpened) -> bool:
        target = event.target
        key = target.key
        if self._closed:
            return False
        if event.reuse_existing and key in self._sessions:
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
        if event.feedback.progress_card_enabled:
            try:
                card = turn_progress_card(snapshot=snapshot)

                async def send_card() -> SendResult | None:
                    sent = await event.reply(card)
                    if event.validate_reply and not await event.validate_reply(sent):
                        return None
                    return sent

                async with asyncio.timeout(self._operation_timeout_seconds):
                    result = await send_card()
                    message_id = _receipt_message_id(result) if result is not None else None
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("failed to send initial progress card", extra=key.log_context)
            if message_id is None:
                logger.error("failed to start progress card: reply message ID unavailable", extra=key.log_context)
        if self._closed or not event.retain_after_send():
            return False
        previous = self._sessions.pop(key, None)
        if previous:
            await self._stop_session(previous)
        if self._closed:
            return False
        session = _TurnProgressCardSession(
            target=target, message_id=message_id, stopped=asyncio.Event(), snapshot=snapshot, observe=event.observe,
            reply_partial=event.reply_partial, validate_reply=event.validate_reply,
            attempted_partials=set() if previous is None else previous.attempted_partials,
            confirmed_partials=set() if previous is None else previous.confirmed_partials,
        )
        self._sessions[key] = session
        if not event.feedback.progress_card_enabled:
            try:
                state = self._process_for(target, event.process_root_id)
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

        session = self._sessions.get(target.key)
        if session is not None:
            try:
                async with asyncio.timeout(self._operation_timeout_seconds):
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
        session = self._sessions.get(key)
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
            try:
                async with asyncio.timeout(self._operation_timeout_seconds):
                    operation = reply(post)
                    result = await operation
                    if not _CardUpdateAttempt("", result).confirmed:
                        continue
                    if session.validate_reply is not None and not await session.validate_reply(result):
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
    ) -> _CardUpdateAttempt | None:
        session = self._sessions.pop(key, None)
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
            operation_id=session.target.key.turn_id,
        )

    async def abandon(
        self,
        *,
        key: PresentationKey,
    ) -> None:
        session = self._sessions.pop(key, None)
        if session is not None:
            await self._stop_session(session)

    async def abandon_thread(
        self,
        *,
        binding_id: str,
        thread_id: str,
    ) -> None:
        """Stop every ordinary/Goal presenter owned by one removed Thread."""

        keys = {key for key in self._process.keys() | self._process_tombstones
                if (key.owner_id, key.thread_id) == (binding_id, thread_id)}
        processes = [self._process.pop(key) for key in keys if key in self._process]
        for key in keys:
            self._process_tombstones.discard(key)
        await self._cancel_processes(processes)
        ordinary_keys = tuple(
            key
            for key in self._sessions
            if not key.side and key.owner_id == binding_id and key.thread_id == thread_id
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

        key = PresentationKey(binding_id, thread_id, turn_id)
        session = self._sessions.pop(key, None)
        if session is None:
            return False
        await self._stop_session(session)
        if session.message_id is None:
            # Keep exact item receipts for a later authoritative completion.
            # No running task remains, so unavailable observation does no I/O.
            self._sessions[key] = session
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

    async def _cancel_processes(self, processes: list[ProcessPresentation]) -> None:
        for process in processes:
            process.revoke()
        tasks = {process.task for process in processes if process.task and not process.task.done()}
        if tasks:
            await asyncio.wait(tasks, timeout=self.operation_timeout)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        processes = list(self._process.values())
        self._process.clear()
        self._process_tombstones.clear()
        await self._cancel_processes(processes)
        sessions = tuple(self._sessions.values())
        self._sessions.clear()
        await asyncio.gather(*(self._stop_session(session) for session in sessions))

    async def _poll(self, session: _TurnProgressCardSession) -> None:
        log_context = {"owner_id": session.target.key.owner_id, "turn_id": session.target.key.turn_id}
        failures = 0

        while not session.stopped.is_set():
            try:
                await asyncio.wait_for(session.stopped.wait(), timeout=self._poll_seconds)
                return
            except TimeoutError:
                pass
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
            if session.message_id is None or session.failed:
                await self._deliver_partial_answers(session, snapshot.partial_answers)
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
                return
            if not await self._update(session, card):
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
    ) -> bool:
        if session.message_id is None:
            return False
        return await self.update_message(session.message_id, card, binding_id=session.target.key.owner_id,
                                         operation_id=session.target.key.turn_id)
    async def update_message(
        self,
        message_id: str,
        card: OutboundCard,
        *,
        binding_id: str,
        operation_id: str,
    ) -> bool:
        async with self._message_writer(message_id):
            attempt = await self._attempt_update_message(
                message_id, card, binding_id=binding_id, operation_id=operation_id,
            )
            return attempt.updated

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
    ) -> _CardUpdateAttempt:
        # Retry only replacement of the same frozen card, never a new reply.
        # All attempts and delays share the existing terminal update budget.
        uncertain = False
        try:
            async with asyncio.timeout(self._operation_timeout_seconds), self._message_writer(message_id):
                for index in range(_TERMINAL_CARD_UPDATE_ATTEMPTS):
                    if index:
                        await asyncio.sleep(_TERMINAL_CARD_RETRY_SECONDS)
                    attempt = await self._attempt_update_message(
                        message_id, card,
                        binding_id=binding_id, operation_id=operation_id,
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
    ) -> _CardUpdateAttempt:
        try:
            async with asyncio.timeout(self._operation_timeout_seconds):
                result = await self._channel.update_card(
                    message_id,
                    card.card,
                )
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
            await task
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "progress card updater failed while stopping",
                extra=session.target.key.log_context,
            )
