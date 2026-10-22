"""One best-effort owner for ordinary, Side, and Goal Reply Card delivery."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from lark_channel import OutboundCard, OutboundPost, SendResult

from ..cards import turn_progress_card
from ..codex_runtime import CodexRuntime
from ..partial_answers import PartialAnswer
from ..runtime.contracts import (
    SideTurnActivitySnapshot,
    TurnActivitySnapshot,
)
from .messages import _progress_card_message_id, _send_result_error_code
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


@dataclass(slots=True)
class _PartialAnswerDelivery:
    thread_id: str
    turn_id: str | None
    stopped: asyncio.Event
    reply_partial: Callable[[OutboundPost], Awaitable[SendResult]]
    validate_reply: Callable[[SendResult], Awaitable[bool]] | None = None
    attempted_partials: set[tuple[str, str, str]] = field(default_factory=set)
    confirmed_partials: set[tuple[str, str, str]] = field(default_factory=set)


@dataclass(slots=True)
class _TurnProgressCardSession:
    binding_id: str
    thread_id: str
    turn_id: str
    message_id: str | None
    stopped: asyncio.Event
    snapshot: TurnActivitySnapshot
    snapshot_reader: Callable[[], TurnActivitySnapshot | None]
    partial_turn_id: str | None
    reply_partial: Callable[[OutboundPost], Awaitable[SendResult]] | None = None
    validate_reply: Callable[[SendResult], Awaitable[bool]] | None = None
    attempted_partials: set[tuple[str, str, str]] = field(default_factory=set)
    confirmed_partials: set[tuple[str, str, str]] = field(default_factory=set)
    failed: bool = False
    task: asyncio.Task[None] | None = None


@dataclass(slots=True)
class _SideTurnProgressCardSession:
    side_id: str
    thread_id: str
    turn_id: str
    message_id: str | None
    stopped: asyncio.Event
    snapshot: SideTurnActivitySnapshot
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
        runtime: CodexRuntime,
        *,
        poll_seconds: float = _PROGRESS_CARD_POLL_SECONDS,
        operation_timeout_seconds: float = _PROGRESS_CARD_OPERATION_TIMEOUT_SECONDS,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("progress card poll interval must be positive")
        if operation_timeout_seconds <= 0:
            raise ValueError("progress card operation timeout must be positive")
        self._channel = channel
        self._runtime = runtime
        self._poll_seconds = poll_seconds
        self._operation_timeout_seconds = operation_timeout_seconds
        self._sessions: dict[
            tuple[str, str, str],
            _TurnProgressCardSession,
        ] = {}
        self._side_sessions: dict[
            tuple[str, str, str],
            _SideTurnProgressCardSession,
        ] = {}
        self._closed = False

    async def start(
        self,
        *,
        binding_id: str,
        thread_id: str,
        turn_id: str,
        origin: object,
        reply: Callable[[OutboundCard], Awaitable[SendResult]] | None = None,
        validate_reply: Callable[[SendResult], Awaitable[bool]] | None = None,
        progress_enabled: bool = True,
        reply_partial: Callable[[OutboundPost], Awaitable[SendResult]] | None = None,
        snapshot_reader: Callable[[], TurnActivitySnapshot | None] | None = None,
        logical_turn: bool = False,
    ) -> bool:
        if self._closed:
            return False
        key = (binding_id, thread_id, turn_id)
        if logical_turn and key in self._sessions:
            # Resuming a paused Goal can join the same running logical stream.
            return True
        if snapshot_reader is None:
            snapshot_reader = lambda: self._runtime.turn_activity(
                binding_id, thread_id=thread_id, turn_id=turn_id, refresh_plan=False,
            )
        try:
            snapshot = snapshot_reader()
        except Exception:
            logger.exception(
                "failed to read initial progress-card activity",
                extra={"binding_id": binding_id, "turn_id": turn_id},
            )
            return False
        if snapshot is None:
            logger.error(
                "failed to start progress card: exact Turn activity unavailable",
                extra={
                    "binding_id": binding_id,
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                },
            )
            return False
        message_id = None
        if progress_enabled:
            try:
                card = turn_progress_card(snapshot=snapshot)
                async with asyncio.timeout(self._operation_timeout_seconds):
                    result = (
                        await self._channel.reply(origin, card)
                        if reply is None
                        else await reply(card)
                    )
                    if validate_reply is None or await validate_reply(result):
                        message_id = _progress_card_message_id(result)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "failed to send initial progress card",
                    extra={"binding_id": binding_id, "turn_id": turn_id},
                )
            if message_id is None:
                logger.error(
                    "failed to start progress card: reply message ID unavailable",
                    extra={"binding_id": binding_id, "turn_id": turn_id},
                )
        current = snapshot_reader()
        if (
            current is None
            or self._runtime.lifecycle_state(binding_id) is not None
        ):
            return False
        key = (binding_id, thread_id, turn_id)
        previous = self._sessions.pop(key, None)
        if previous is not None:
            await self._stop_session(previous)
        session = _TurnProgressCardSession(
            binding_id=binding_id,
            thread_id=thread_id,
            turn_id=turn_id,
            message_id=message_id,
            stopped=asyncio.Event(),
            snapshot=snapshot,
            snapshot_reader=snapshot_reader,
            partial_turn_id=None if logical_turn else turn_id,
            reply_partial=reply_partial or (lambda post: self._channel.reply(origin, post)),
            validate_reply=validate_reply,
            attempted_partials=set() if previous is None else previous.attempted_partials,
            confirmed_partials=set() if previous is None else previous.confirmed_partials,
        )
        self._sessions[key] = session
        session.task = asyncio.create_task(
            self._poll(session),
            name=f"netizen-progress-card-{turn_id}",
        )
        return True

    async def prepare_partial_answers(
        self,
        *,
        owner_id: str,
        thread_id: str,
        turn_id: str | None,
        side: bool,
        logical_turn: bool = False,
        reply: Callable[[OutboundPost], Awaitable[SendResult]],
        validate_reply: Callable[[SendResult], Awaitable[bool]] | None = None,
    ) -> _PartialAnswerDelivery:
        """Quiesce running delivery and retain receipts before trying the terminal card."""

        sessions = self._side_sessions if side else self._sessions
        session = None if turn_id is None else sessions.get((owner_id, thread_id, turn_id))
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
            thread_id=thread_id, turn_id=(
                None if logical_turn else turn_id
            ),
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
            (answer.thread_id, answer.turn_id, answer.item_id)
            for answer in answers
            if answer.thread_id == delivery.thread_id
            and (delivery.turn_id is None or answer.turn_id == delivery.turn_id)
            and answer.text.strip()
        }
        return (not answers or bool(identities)) and identities <= delivery.confirmed_partials

    async def _deliver_partial_answers(
        self,
        session: _PartialAnswerDelivery | _TurnProgressCardSession | _SideTurnProgressCardSession,
        answers: tuple[PartialAnswer, ...],
        *,
        terminal: bool = False,
    ) -> None:
        reply = session.reply_partial
        if reply is None:
            return
        partial_turn_id = session.partial_turn_id if isinstance(session, _TurnProgressCardSession) else session.turn_id
        for answer in answers:
            if session.stopped.is_set() and not terminal:
                return
            identity = (answer.thread_id, answer.turn_id, answer.item_id)
            if (
                answer.thread_id != session.thread_id
                or (partial_turn_id is not None and answer.turn_id != partial_turn_id)
                or not answer.text.strip()
                or identity in session.attempted_partials
            ):
                continue
            try:
                post = OutboundPost(markdown=f"**阶段性答案**\n\n{answer.text}")
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
                    result = await reply(post)
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
        binding_id: str,
        thread_id: str,
        turn_id: str,
        activity: TurnActivitySnapshot | None,
        render: Callable[[TurnActivitySnapshot], OutboundCard],
    ) -> _CardUpdateAttempt | None:
        session = self._sessions.pop((binding_id, thread_id, turn_id), None)
        if session is None:
            return None
        await self._stop_session(session)
        if session.message_id is None:
            return None
        snapshot = activity or session.snapshot
        if (
            snapshot.binding_id != session.binding_id
            or snapshot.thread_id != session.thread_id
            or snapshot.turn_id != session.turn_id
        ):
            logger.error(
                "failed to finish progress card: activity identity mismatch",
                extra={
                    "binding_id": session.binding_id,
                    "turn_id": session.turn_id,
                },
            )
            return None
        try:
            card = render(snapshot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "failed to render terminal progress card",
                extra={
                    "binding_id": session.binding_id,
                    "turn_id": session.turn_id,
                },
            )
            return None
        return await self._update_terminal_message(
            session.message_id,
            card,
            binding_id=session.binding_id,
            operation_id=session.turn_id,
        )

    async def abandon(
        self,
        *,
        binding_id: str,
        thread_id: str,
        turn_id: str,
    ) -> None:
        session = self._sessions.pop((binding_id, thread_id, turn_id), None)
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
            snapshot = self._runtime.turn_activity(
                binding_id,
                thread_id=thread_id,
                turn_id=turn_id,
                refresh_plan=False,
            )
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

    async def start_side(
        self,
        *,
        side_id: str,
        thread_id: str,
        turn_id: str,
        origin: object,
        reply: Callable[[OutboundCard], Awaitable[SendResult]] | None = None,
        progress_enabled: bool = True,
        reply_partial: Callable[[OutboundPost], Awaitable[SendResult]] | None = None,
    ) -> bool:
        if self._closed:
            return False
        try:
            snapshot = self._runtime.side_turn_activity(
                side_id,
                thread_id=thread_id,
                turn_id=turn_id,
                refresh_plan=False,
            )
        except Exception:
            logger.exception(
                "failed to read initial Side progress-card activity",
                extra={"side_id": side_id, "turn_id": turn_id},
            )
            return False
        if snapshot is None:
            logger.error(
                "failed to start Side progress card: exact activity unavailable",
                extra={
                    "side_id": side_id,
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                },
            )
            return False
        message_id = None
        if progress_enabled:
            try:
                card = turn_progress_card(snapshot=snapshot)
                async with asyncio.timeout(self._operation_timeout_seconds):
                    result = (
                        await self._channel.reply(origin, card)
                        if reply is None
                        else await reply(card)
                    )
                message_id = _progress_card_message_id(result)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "failed to send initial Side progress card",
                    extra={"side_id": side_id, "turn_id": turn_id},
                )
            if message_id is None:
                logger.error(
                    "failed to start Side progress card: reply message ID unavailable",
                    extra={"side_id": side_id, "turn_id": turn_id},
                )
        key = (side_id, thread_id, turn_id)
        previous = self._side_sessions.pop(key, None)
        if previous is not None:
            await self._stop_session(previous)
        session = _SideTurnProgressCardSession(
            side_id=side_id,
            thread_id=thread_id,
            turn_id=turn_id,
            message_id=message_id,
            stopped=asyncio.Event(),
            snapshot=snapshot,
            reply_partial=reply_partial or (lambda post: self._channel.reply(origin, post)),
            attempted_partials=set() if previous is None else previous.attempted_partials,
            confirmed_partials=set() if previous is None else previous.confirmed_partials,
        )
        self._side_sessions[key] = session
        session.task = asyncio.create_task(
            self._poll_side(session),
            name=f"netizen-side-progress-card-{turn_id}",
        )
        return True

    async def finish_side(
        self,
        *,
        side_id: str,
        thread_id: str,
        turn_id: str,
        activity: SideTurnActivitySnapshot | None,
        render: Callable[[SideTurnActivitySnapshot], OutboundCard],
    ) -> _CardUpdateAttempt | None:
        session = self._side_sessions.pop((side_id, thread_id, turn_id), None)
        if session is None:
            return None
        await self._stop_session(session)
        if session.message_id is None:
            return None
        snapshot = activity or session.snapshot
        if (
            snapshot.side_id != session.side_id
            or snapshot.thread_id != session.thread_id
            or snapshot.turn_id != session.turn_id
        ):
            logger.error(
                "failed to finish Side progress card: activity identity mismatch",
                extra={"side_id": session.side_id, "turn_id": session.turn_id},
            )
            return None
        try:
            card = render(snapshot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "failed to render terminal Side progress card",
                extra={"side_id": session.side_id, "turn_id": session.turn_id},
            )
            return None
        return await self._update_terminal_message(
            session.message_id,
            card,
            binding_id=f"side:{session.side_id}",
            operation_id=session.turn_id,
        )

    async def abandon_side(
        self,
        *,
        side_id: str,
        thread_id: str,
        turn_id: str,
    ) -> None:
        session = self._side_sessions.pop((side_id, thread_id, turn_id), None)
        if session is not None:
            await self._stop_session(session)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
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
        await self._poll_session(
            session,
            snapshot_reader=session.snapshot_reader,
            update=lambda card: self._update(session, card),
            log_context={"binding_id": session.binding_id, "turn_id": session.turn_id},
        )

    async def _poll_side(self, session: _SideTurnProgressCardSession) -> None:
        await self._poll_session(
            session,
            snapshot_reader=lambda: self._runtime.side_turn_activity(
                session.side_id,
                thread_id=session.thread_id,
                turn_id=session.turn_id,
                refresh_plan=False,
            ),
            update=lambda card: self._update_side(session, card),
            log_context={"side_id": session.side_id, "turn_id": session.turn_id},
        )

    async def _poll_session(
        self,
        session: _TurnProgressCardSession | _SideTurnProgressCardSession,
        *,
        snapshot_reader: Callable[[], TurnActivitySnapshot | SideTurnActivitySnapshot | None],
        update: Callable[[OutboundCard], Awaitable[bool]],
        log_context: dict[str, str],
    ) -> None:
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
                snapshot = snapshot_reader()
            except Exception:
                logger.exception("failed to refresh running progress card", extra=log_context)
                session.failed = True
                return
            if snapshot is None:
                continue
            if session.message_id is None:
                await self._deliver_partial_answers(session, snapshot.partial_answers)
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
            if not await update(card):
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
        return await self._update_message(
            session.message_id,
            card,
            binding_id=session.binding_id,
            operation_id=session.turn_id,
        )

    async def _update_side(
        self,
        session: _SideTurnProgressCardSession,
        card: OutboundCard,
    ) -> bool:
        if session.message_id is None:
            return False
        return await self._update_message(
            session.message_id,
            card,
            binding_id=f"side:{session.side_id}",
            operation_id=session.turn_id,
        )

    async def _update_message(
        self,
        message_id: str,
        card: OutboundCard,
        *,
        binding_id: str,
        operation_id: str,
    ) -> bool:
        attempt = await self._attempt_update_message(
            message_id, card, binding_id=binding_id, operation_id=operation_id,
        )
        return attempt.updated

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
            async with asyncio.timeout(self._operation_timeout_seconds):
                for index in range(_TERMINAL_CARD_UPDATE_ATTEMPTS):
                    if index:
                        await asyncio.sleep(_TERMINAL_CARD_RETRY_SECONDS)
                    attempt = await self._attempt_update_message(
                        message_id, card,
                        binding_id=binding_id, operation_id=operation_id,
                    )
                    if attempt.updated:
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

    @staticmethod
    async def _stop_session(
        session: (
            _TurnProgressCardSession
            | _SideTurnProgressCardSession
        ),
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
