"""One best-effort owner for ordinary, Side, and Goal Reply Card delivery."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from enum import Enum

from lark_channel import OutboundCard

from ..cards import CardActionError, reply_card, turn_progress_card
from ..codex_runtime import CodexRuntime
from ..runtime.contracts import (
    SideTurnActivitySnapshot,
    TurnActivitySnapshot,
)
from ..domain import FeishuScope, GoalStatus, ReplyCardGoalModule, ReplyCardProjection
from .messages import _progress_card_message_id
from .ports import ReplyChannel


logger = logging.getLogger(__name__)

_PROGRESS_CARD_POLL_SECONDS = 1.0
_PROGRESS_CARD_OPERATION_TIMEOUT_SECONDS = 5.0
_PROGRESS_CARD_MAX_CONSECUTIVE_FAILURES = 3
_GOAL_REPLY_CARD_CACHE_LIMIT = 256


@dataclass(slots=True)
class GoalCardOrigin:
    message_id: str | None
    scope: FeishuScope
    binding_id: str
    short_id: str
    project_alias: str
    fallback_origin: object | None = None
    goal_generation: str | None = None


@dataclass(slots=True)
class _TurnProgressCardSession:
    binding_id: str
    thread_id: str
    turn_id: str
    message_id: str
    stopped: asyncio.Event
    snapshot: TurnActivitySnapshot
    failed: bool = False
    task: asyncio.Task[None] | None = None


@dataclass(slots=True)
class _SideTurnProgressCardSession:
    side_id: str
    thread_id: str
    turn_id: str
    message_id: str
    stopped: asyncio.Event
    snapshot: SideTurnActivitySnapshot
    failed: bool = False
    task: asyncio.Task[None] | None = None


@dataclass(slots=True)
class _GoalReplyCardSession:
    binding_id: str
    thread_id: str
    goal_generation: str
    logical_turn_id: str
    message_id: str
    stopped: asyncio.Event
    projection: ReplyCardProjection
    revision: object
    refresh: Callable[
        [],
        Awaitable[tuple[object, ReplyCardProjection] | None],
    ] | None = None
    failed: bool = False
    task: asyncio.Task[None] | None = None


class _GoalCardDelivery(Enum):
    DELIVERED = "delivered"
    SUPERSEDED = "superseded"
    FAILED = "failed"


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
        self._goal_sessions: dict[
            tuple[str, str, str],
            _GoalReplyCardSession,
        ] = {}
        self._goal_cards: dict[
            tuple[str, str],
            ReplyCardProjection,
        ] = {}
        self._goal_lock = asyncio.Lock()
        self._goal_card_lock = asyncio.Lock()
        self._retired_goal_runs: set[tuple[str, str, str]] = set()
        self._goal_latest_runs: dict[tuple[str, str, str], str] = {}
        self._closed = False

    async def start(
        self,
        *,
        binding_id: str,
        thread_id: str,
        turn_id: str,
        origin: object,
    ) -> bool:
        if self._closed:
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
        try:
            card = turn_progress_card(snapshot=snapshot)
            async with asyncio.timeout(self._operation_timeout_seconds):
                result = await self._channel.reply(origin, card)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "failed to send initial progress card",
                extra={"binding_id": binding_id, "turn_id": turn_id},
            )
            return False
        message_id = _progress_card_message_id(result)
        if message_id is None:
            logger.error(
                "failed to start progress card: reply message ID unavailable",
                extra={"binding_id": binding_id, "turn_id": turn_id},
            )
            return False
        current = self._runtime.turn_activity(
            binding_id,
            thread_id=thread_id,
            turn_id=turn_id,
            refresh_plan=False,
        )
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
        )
        self._sessions[key] = session
        session.task = asyncio.create_task(
            self._poll(session),
            name=f"netizen-progress-card-{turn_id}",
        )
        return True

    async def finish(
        self,
        *,
        binding_id: str,
        thread_id: str,
        turn_id: str,
        activity: TurnActivitySnapshot | None,
        render: Callable[[TurnActivitySnapshot], OutboundCard],
    ) -> bool:
        session = self._sessions.pop((binding_id, thread_id, turn_id), None)
        if session is None:
            return False
        await self._stop_session(session)
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
            return False
        try:
            card = render(snapshot)
            return await self._update(session, card)
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
            return False

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

        async with self._goal_lock:
            goal_keys = tuple(
                key
                for key in self._goal_sessions
                if key[0] == binding_id and key[1] == thread_id
            )
            self._goal_latest_runs = {
                key: run_id
                for key, run_id in self._goal_latest_runs.items()
                if key[:2] != (binding_id, thread_id)
            }
            removed: list[_GoalReplyCardSession] = []
            for key in goal_keys:
                session = self._goal_sessions.pop(key, None)
                if session is not None:
                    removed.append(session)
                    await self._stop_session(session)
            if removed:
                async with self._goal_card_lock:
                    for session in removed:
                        self._goal_cards.pop(
                            (session.message_id, session.goal_generation),
                            None,
                        )

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
        try:
            card = turn_progress_card(snapshot=snapshot)
            async with asyncio.timeout(self._operation_timeout_seconds):
                result = await self._channel.reply(origin, card)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "failed to send initial Side progress card",
                extra={"side_id": side_id, "turn_id": turn_id},
            )
            return False
        message_id = _progress_card_message_id(result)
        if message_id is None:
            logger.error(
                "failed to start Side progress card: reply message ID unavailable",
                extra={"side_id": side_id, "turn_id": turn_id},
            )
            return False
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
    ) -> bool:
        session = self._side_sessions.pop((side_id, thread_id, turn_id), None)
        if session is None:
            return False
        await self._stop_session(session)
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
            return False
        try:
            return await self._update_side(session, render(snapshot))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "failed to render terminal Side progress card",
                extra={"side_id": session.side_id, "turn_id": session.turn_id},
            )
            return False

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

    async def start_goal(
        self,
        *,
        binding_id: str,
        thread_id: str,
        logical_turn_id: str,
        generation: str,
        origin: GoalCardOrigin,
        projection: ReplyCardProjection,
        revision: object,
        refresh: Callable[
            [],
            Awaitable[tuple[object, ReplyCardProjection] | None],
        ]
        | None,
    ) -> bool:
        async with self._goal_lock:
            return await self._start_goal_locked(
                binding_id=binding_id,
                thread_id=thread_id,
                logical_turn_id=logical_turn_id,
                generation=generation,
                origin=origin,
                projection=projection,
                revision=revision,
                refresh=refresh,
            )

    async def _start_goal_locked(
        self,
        *,
        binding_id: str,
        thread_id: str,
        logical_turn_id: str,
        generation: str,
        origin: GoalCardOrigin,
        projection: ReplyCardProjection,
        revision: object,
        refresh: Callable[
            [],
            Awaitable[tuple[object, ReplyCardProjection] | None],
        ]
        | None,
    ) -> bool:
        if self._closed:
            return False
        key = (binding_id, thread_id, generation)
        self._goal_latest_runs = {
            existing: run_id
            for existing, run_id in self._goal_latest_runs.items()
            if existing[:2] != (binding_id, thread_id) or existing == key
        }
        self._goal_latest_runs[key] = logical_turn_id
        previous = self._goal_sessions.pop(key, None)
        message_id = origin.message_id
        if previous is not None:
            await self._stop_session(previous)
            if not previous.failed:
                message_id = previous.message_id
        try:
            card = reply_card(projection)
            if message_id is None:
                fallback = origin.fallback_origin
                if fallback is None:
                    return False
                async with asyncio.timeout(self._operation_timeout_seconds):
                    result = await self._channel.reply(fallback, card)
                message_id = _progress_card_message_id(result)
            else:
                async with self._goal_card_lock:
                    if not await self._update_message(
                        message_id,
                        card,
                        binding_id=binding_id,
                        operation_id=logical_turn_id,
                    ):
                        return False
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "failed to establish Goal Reply Card",
                extra={
                    "binding_id": binding_id,
                    "logical_turn_id": logical_turn_id,
                },
            )
            return False
        if message_id is None:
            logger.error(
                "failed to establish Goal Reply Card: reply message ID unavailable",
                extra={
                    "binding_id": binding_id,
                    "logical_turn_id": logical_turn_id,
                },
            )
            return False
        if self._runtime.lifecycle_state(binding_id) is not None:
            return False
        origin.message_id = message_id
        origin.goal_generation = generation
        self._retired_goal_runs = {
            item
            for item in self._retired_goal_runs
            if item[:2] != (message_id, generation)
        }
        session = _GoalReplyCardSession(
            binding_id=binding_id,
            thread_id=thread_id,
            goal_generation=generation,
            logical_turn_id=logical_turn_id,
            message_id=message_id,
            stopped=asyncio.Event(),
            projection=projection,
            revision=revision,
            refresh=refresh,
        )
        self._goal_sessions[key] = session
        self._remember_goal_projection(message_id, generation, projection)
        if refresh is not None:
            session.task = asyncio.create_task(
                self._poll_goal(session),
                name=f"netizen-goal-reply-card-{logical_turn_id}",
            )
        return True

    async def finish_goal(
        self,
        *,
        binding_id: str,
        thread_id: str,
        logical_turn_id: str | None,
        generation: str,
        origin: GoalCardOrigin,
        projection: ReplyCardProjection,
        retain_session: bool,
    ) -> _GoalCardDelivery:
        async with self._goal_lock:
            return await self._finish_goal_locked(
                binding_id=binding_id,
                thread_id=thread_id,
                logical_turn_id=logical_turn_id,
                generation=generation,
                origin=origin,
                projection=projection,
                retain_session=retain_session,
            )

    async def reply_goal_fallback(
        self,
        *,
        binding_id: str,
        thread_id: str,
        logical_turn_id: str | None,
        generation: str,
        target: object,
        card: OutboundCard,
        origin: GoalCardOrigin,
        projection: ReplyCardProjection,
        retain_session: bool,
    ) -> _GoalCardDelivery:
        """CAS, reply, and adopt one fallback while the Goal route is stable."""

        async with self._goal_lock:
            if self._closed:
                return _GoalCardDelivery.FAILED
            key = (binding_id, thread_id, generation)
            latest_run = self._goal_latest_runs.get(key)
            if latest_run is not None and latest_run != logical_turn_id:
                return _GoalCardDelivery.SUPERSEDED
            current = self._goal_sessions.get(key)
            if current is not None and current.logical_turn_id != logical_turn_id:
                return _GoalCardDelivery.SUPERSEDED
            if current is not None:
                self._goal_sessions.pop(key, None)
                await self._stop_session(current)
            try:
                async with asyncio.timeout(self._operation_timeout_seconds):
                    result = await self._channel.reply(target, card)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "terminal Goal card fallback failed",
                    extra={"binding_id": binding_id},
                )
                return _GoalCardDelivery.FAILED
            if getattr(result, "success", True) is False:
                logger.error(
                    "terminal Goal card fallback was not confirmed",
                    extra={"binding_id": binding_id},
                )
                return _GoalCardDelivery.FAILED
            message_id = _progress_card_message_id(result)
            if message_id is None:
                logger.warning(
                    "terminal Goal fallback lacks a reusable message identity",
                    extra={"binding_id": binding_id},
                )
                return _GoalCardDelivery.DELIVERED
            origin.message_id = message_id
            origin.goal_generation = generation
            if logical_turn_id is not None:
                self._goal_latest_runs[key] = logical_turn_id
            async with self._goal_card_lock:
                if not retain_session:
                    return _GoalCardDelivery.DELIVERED
                self._remember_goal_projection(
                    message_id,
                    generation,
                    projection,
                )
            if retain_session:
                self._goal_sessions[key] = _GoalReplyCardSession(
                    binding_id=binding_id,
                    thread_id=thread_id,
                    goal_generation=generation,
                    logical_turn_id=logical_turn_id or generation,
                    message_id=message_id,
                    stopped=asyncio.Event(),
                    projection=projection,
                    revision=("terminal-fallback",),
                )
            return _GoalCardDelivery.DELIVERED

    async def _finish_goal_locked(
        self,
        *,
        binding_id: str,
        thread_id: str,
        logical_turn_id: str | None,
        generation: str,
        origin: GoalCardOrigin,
        projection: ReplyCardProjection,
        retain_session: bool,
    ) -> _GoalCardDelivery:
        key = (binding_id, thread_id, generation)
        latest_run = self._goal_latest_runs.get(key)
        if latest_run is not None and latest_run != logical_turn_id:
            # The exact Goal generation has already advanced to a newer
            # logical run, even if that newer run has also reached terminal.
            return _GoalCardDelivery.SUPERSEDED
        session = self._goal_sessions.get(key)
        if (
            session is not None
            and session.logical_turn_id != logical_turn_id
        ):
            # A resumed run already owns this Goal generation and card.  The
            # previous run's delayed terminal projection must not overwrite it.
            return _GoalCardDelivery.SUPERSEDED
        retired_key = (
            origin.message_id or "",
            generation,
            logical_turn_id or "",
        )
        if session is None and retired_key in self._retired_goal_runs:
            self._retired_goal_runs.discard(retired_key)
            return _GoalCardDelivery.SUPERSEDED
        if session is not None:
            self._goal_sessions.pop(key, None)
        if session is not None:
            await self._stop_session(session)
            message_id = session.message_id
        else:
            message_id = origin.message_id
        if message_id is None:
            return _GoalCardDelivery.FAILED
        try:
            card = reply_card(projection)
        except Exception:
            logger.exception(
                "failed to render terminal Goal Reply Card",
                extra={"binding_id": binding_id},
            )
            return _GoalCardDelivery.FAILED
        async with self._goal_card_lock:
            delivered = await self._update_message(
                message_id,
                card,
                binding_id=binding_id,
                operation_id=origin.goal_generation or generation,
            )
            if delivered and retain_session:
                self._remember_goal_projection(
                    message_id,
                    generation,
                    projection,
                )
            elif delivered:
                self._goal_cards.pop((message_id, generation), None)
        if delivered and retain_session:
            self._goal_sessions[key] = _GoalReplyCardSession(
                binding_id=binding_id,
                thread_id=thread_id,
                goal_generation=generation,
                logical_turn_id=(
                    session.logical_turn_id
                    if session is not None
                    else (logical_turn_id or generation)
                ),
                message_id=message_id,
                stopped=asyncio.Event(),
                projection=projection,
                revision=("terminal",),
            )
        elif delivered:
            self._retired_goal_runs.discard(retired_key)
        return (
            _GoalCardDelivery.DELIVERED
            if delivered
            else _GoalCardDelivery.FAILED
        )

    async def update_goal(
        self,
        *,
        source_id: str,
        generation: str,
        projection: ReplyCardProjection,
        retain_session: bool = True,
    ) -> bool:
        async with self._goal_lock:
            return await self._update_goal_locked(
                source_id=source_id,
                generation=generation,
                projection=projection,
                retain_session=retain_session,
            )

    async def refresh_goal_snapshot(
        self,
        *,
        source_id: str,
        generation: str,
        logical_turn_id: str | None,
        projection: ReplyCardProjection,
    ) -> bool:
        """Merge `/goal` status into the canonical card without regressing it."""

        async with self._goal_lock:
            matched = next(
                (
                    session
                    for session in self._goal_sessions.values()
                    if session.message_id == source_id
                    and session.goal_generation == generation
                ),
                None,
            )
            current = self._goal_projection(source_id, generation)
            if matched is None:
                # A terminal update won the race after the read-only native
                # snapshot.  Its newer projection already answers the status
                # request and must not be overwritten by the stale read.
                return current is not None
            incoming_goal = projection.goal
            assert incoming_goal is not None
            if matched.refresh is not None and (
                (
                    logical_turn_id is not None
                    and logical_turn_id != matched.logical_turn_id
                )
                or incoming_goal.status != GoalStatus.ACTIVE.value
            ):
                return True
            merged = projection
            if current is not None:
                merged_goal = incoming_goal
                if (
                    current.goal is not None
                    and current.goal.goal_generation == generation
                    and current.goal.status == incoming_goal.status
                ):
                    merged_goal = replace(
                        incoming_goal,
                        notice=current.goal.notice,
                        notice_is_error=current.goal.notice_is_error,
                    )
                merged = replace(
                    current,
                    scope=projection.scope or current.scope,
                    goal=merged_goal,
                    activity=(
                        projection.activity
                        if matched.refresh is not None
                        else current.activity
                    ),
                )
            return await self._update_goal_locked(
                source_id=source_id,
                generation=generation,
                projection=merged,
                retain_session=True,
            )

    async def update_goal_module(
        self,
        *,
        source_id: str,
        generation: str,
        scope: FeishuScope,
        goal: ReplyCardGoalModule,
        retain_session: bool,
    ) -> bool:
        """Atomically replace only Goal while preserving other card modules."""

        async with self._goal_lock:
            current = self._goal_projection(source_id, generation)
            projection = (
                ReplyCardProjection(scope=scope, goal=goal)
                if current is None
                else replace(current, scope=scope, goal=goal)
            )
            return await self._update_goal_locked(
                source_id=source_id,
                generation=generation,
                projection=projection,
                retain_session=retain_session,
            )

    async def _update_goal_locked(
        self,
        *,
        source_id: str,
        generation: str,
        projection: ReplyCardProjection,
        retain_session: bool,
    ) -> bool:
        matched_key: tuple[str, str, str] | None = None
        matched: _GoalReplyCardSession | None = None
        for key, session in self._goal_sessions.items():
            if (
                session.message_id == source_id
                and session.goal_generation == generation
            ):
                matched_key = key
                matched = session
                break
        if matched_key is not None and matched is not None:
            self._goal_sessions.pop(matched_key, None)
            await self._stop_session(matched)
            if not retain_session and matched.refresh is not None:
                self._retired_goal_runs.add(
                    (source_id, generation, matched.logical_turn_id)
                )
        try:
            card = reply_card(projection)
        except Exception:
            logger.exception("failed to render Goal Reply Card update")
            return False
        async with self._goal_card_lock:
            delivered = await self._update_message(
                source_id,
                card,
                binding_id=(
                    matched.binding_id if matched is not None else "unknown"
                ),
                operation_id=generation,
            )
            if delivered and retain_session:
                self._remember_goal_projection(
                    source_id,
                    generation,
                    projection,
                )
            elif delivered:
                self._goal_cards.pop((source_id, generation), None)
        if delivered and retain_session and matched_key is not None and matched is not None:
            refreshed_session = _GoalReplyCardSession(
                binding_id=matched.binding_id,
                thread_id=matched.thread_id,
                goal_generation=matched.goal_generation,
                logical_turn_id=matched.logical_turn_id,
                message_id=source_id,
                stopped=asyncio.Event(),
                projection=projection,
                revision=matched.revision,
                refresh=matched.refresh,
            )
            self._goal_sessions[matched_key] = refreshed_session
            if refreshed_session.refresh is not None:
                refreshed_session.task = asyncio.create_task(
                    self._poll_goal(refreshed_session),
                    name=(
                        "netizen-goal-reply-card-"
                        f"{refreshed_session.logical_turn_id}"
                    ),
                )
        return delivered

    async def abandon_goal(
        self,
        *,
        binding_id: str,
        thread_id: str,
        logical_turn_id: str | None,
        generation: str,
    ) -> None:
        async with self._goal_lock:
            key = (binding_id, thread_id, generation)
            session = self._goal_sessions.get(key)
            if (
                session is None
                or session.logical_turn_id != logical_turn_id
            ):
                return
            self._goal_sessions.pop(key, None)
            await self._stop_session(session)

    def goal_projection(
        self,
        *,
        source_id: str,
        generation: str,
    ) -> ReplyCardProjection | None:
        return self._goal_projection(source_id, generation)

    def _goal_projection(
        self,
        source_id: str,
        generation: str,
    ) -> ReplyCardProjection | None:
        current = self._goal_cards.get((source_id, generation))
        if current is not None:
            return current
        for session in self._goal_sessions.values():
            if (
                session.message_id == source_id
                and session.goal_generation == generation
            ):
                return session.projection
        return None

    async def update_goal_page(
        self,
        *,
        source_id: str,
        binding_id: str,
        generation: str,
        page: int,
        render: Callable[[ReplyCardProjection | None], OutboundCard],
    ) -> bool:
        """Serialize one Goal file-page rebuild with every card mutation."""

        async with self._goal_lock:
            async with self._goal_card_lock:
                current = self._goal_projection(source_id, generation)
                if (
                    current is not None
                    and current.goal is not None
                    and current.goal.binding_id != binding_id
                ):
                    raise CardActionError("Goal 文件卡片的会话身份不一致。")
                card = render(current)
                delivered = await self._update_message(
                    source_id,
                    card,
                    binding_id=binding_id,
                    operation_id=generation,
                )
                if delivered and current is not None and current.files is not None:
                    current = replace(
                        current,
                        files=replace(current.files, page=page),
                    )
                    self._remember_goal_projection(
                        source_id,
                        generation,
                        current,
                    )
                    for session in self._goal_sessions.values():
                        if (
                            session.message_id == source_id
                            and session.goal_generation == generation
                        ):
                            session.projection = current
                            break
                return delivered

    def _remember_goal_projection(
        self,
        source_id: str,
        generation: str,
        projection: ReplyCardProjection,
    ) -> None:
        key = (source_id, generation)
        if key not in self._goal_cards and (
            len(self._goal_cards) >= _GOAL_REPLY_CARD_CACHE_LIMIT
        ):
            self._goal_cards.pop(next(iter(self._goal_cards)))
        self._goal_cards[key] = projection

    def goal_message_id(
        self,
        *,
        binding_id: str,
        thread_id: str,
        generation: str,
    ) -> str | None:
        session = self._goal_sessions.get((binding_id, thread_id, generation))
        return None if session is None else session.message_id

    def owns_goal_card(
        self,
        *,
        source_id: str,
        binding_id: str,
        thread_id: str,
        generation: str,
    ) -> bool:
        """Return whether one live exact Goal route owns this control card."""

        session = self._goal_sessions.get((binding_id, thread_id, generation))
        return session is not None and session.message_id == source_id

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        sessions = tuple(self._sessions.values())
        self._sessions.clear()
        side_sessions = tuple(self._side_sessions.values())
        self._side_sessions.clear()
        async with self._goal_lock:
            goal_sessions = tuple(self._goal_sessions.values())
            self._goal_sessions.clear()
            self._retired_goal_runs.clear()
            self._goal_latest_runs.clear()
            self._goal_cards.clear()
        await asyncio.gather(
            *(
                self._stop_session(session)
                for session in (*sessions, *side_sessions, *goal_sessions)
            ),
            return_exceptions=False,
        )

    async def _poll(self, session: _TurnProgressCardSession) -> None:
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
                snapshot = self._runtime.turn_activity(
                    session.binding_id,
                    thread_id=session.thread_id,
                    turn_id=session.turn_id,
                    refresh_plan=False,
                )
            except Exception:
                logger.exception(
                    "failed to refresh running progress card",
                    extra={
                        "binding_id": session.binding_id,
                        "turn_id": session.turn_id,
                    },
                )
                session.failed = True
                return
            if snapshot is None:
                continue
            if snapshot.revision == session.snapshot.revision:
                continue
            try:
                card = turn_progress_card(snapshot=snapshot)
            except Exception:
                logger.exception(
                    "failed to render running progress card",
                    extra={
                        "binding_id": session.binding_id,
                        "turn_id": session.turn_id,
                    },
                )
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

    async def _poll_side(self, session: _SideTurnProgressCardSession) -> None:
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
                snapshot = self._runtime.side_turn_activity(
                    session.side_id,
                    thread_id=session.thread_id,
                    turn_id=session.turn_id,
                    refresh_plan=False,
                )
            except Exception:
                logger.exception(
                    "failed to refresh running Side progress card",
                    extra={
                        "side_id": session.side_id,
                        "turn_id": session.turn_id,
                    },
                )
                session.failed = True
                return
            if snapshot is None:
                continue
            if snapshot.revision == session.snapshot.revision:
                continue
            try:
                card = turn_progress_card(snapshot=snapshot)
            except Exception:
                logger.exception(
                    "failed to render running Side progress card",
                    extra={
                        "side_id": session.side_id,
                        "turn_id": session.turn_id,
                    },
                )
                session.failed = True
                return
            if not await self._update_side(session, card):
                failures += 1
                if failures >= _PROGRESS_CARD_MAX_CONSECUTIVE_FAILURES:
                    session.failed = True
                    return
                continue
            session.snapshot = snapshot
            failures = 0

    async def _poll_goal(self, session: _GoalReplyCardSession) -> None:
        refresh = session.refresh
        if refresh is None:
            return
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
                async with asyncio.timeout(self._operation_timeout_seconds):
                    refreshed = await refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "failed to refresh running Goal Reply Card",
                    extra={
                        "binding_id": session.binding_id,
                        "logical_turn_id": session.logical_turn_id,
                    },
                )
                session.failed = True
                return
            if refreshed is None:
                continue
            revision, projection = refreshed
            if session.stopped.is_set():
                return
            async with self._goal_card_lock:
                key = (
                    session.binding_id,
                    session.thread_id,
                    session.goal_generation,
                )
                if (
                    session.stopped.is_set()
                    or self._goal_sessions.get(key) is not session
                ):
                    return
                if revision == session.revision:
                    continue
                try:
                    card = reply_card(projection)
                except Exception:
                    logger.exception(
                        "failed to render running Goal Reply Card",
                        extra={"binding_id": session.binding_id},
                    )
                    session.failed = True
                    return
                if not await self._update_message(
                    session.message_id,
                    card,
                    binding_id=session.binding_id,
                    operation_id=session.logical_turn_id,
                ):
                    failures += 1
                    if failures >= _PROGRESS_CARD_MAX_CONSECUTIVE_FAILURES:
                        session.failed = True
                        return
                    continue
                session.revision = revision
                session.projection = projection
                failures = 0
                self._remember_goal_projection(
                    session.message_id,
                    session.goal_generation,
                    projection,
                )

    async def _update(
        self,
        session: _TurnProgressCardSession,
        card: OutboundCard,
    ) -> bool:
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
            return False
        if getattr(result, "success", True) is False:
            logger.error(
                "failed to update progress card: unsuccessful result",
                extra={
                    "binding_id": binding_id,
                    "operation_id": operation_id,
                    "message_id": message_id,
                },
            )
            return False
        return True

    @staticmethod
    async def _stop_session(
        session: (
            _TurnProgressCardSession
            | _SideTurnProgressCardSession
            | _GoalReplyCardSession
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
