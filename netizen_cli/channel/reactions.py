"""Best-effort lifecycle for exact Turn reactions."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from .ports import ReplyChannel


logger = logging.getLogger(__name__)

_TYPING_REACTION = "Typing"
_THINKING_REACTION = "THINKING"
_THINKING_VISIBLE_SECONDS = 2.0
_THINKING_HIDDEN_SECONDS = 13.0
_REACTION_OPERATION_TIMEOUT_SECONDS = 3.0


@dataclass(slots=True)
class _TurnReactionPulse:
    turn_id: str
    message_id: str
    stopped: asyncio.Event
    typing_reaction_id: str | None = None
    thinking_reaction_id: str | None = None
    task: asyncio.Task[None] | None = None


class _ReactionController:
    """Best-effort, in-memory lifecycle for one exact Turn's reactions."""

    def __init__(
        self,
        channel: ReplyChannel,
        *,
        visible_seconds: float = _THINKING_VISIBLE_SECONDS,
        hidden_seconds: float = _THINKING_HIDDEN_SECONDS,
        operation_timeout_seconds: float = _REACTION_OPERATION_TIMEOUT_SECONDS,
    ) -> None:
        if visible_seconds <= 0 or hidden_seconds <= 0:
            raise ValueError("thinking reaction pulse intervals must be positive")
        if operation_timeout_seconds <= 0:
            raise ValueError("reaction operation timeout must be positive")
        self._channel = channel
        self._visible_seconds = visible_seconds
        self._hidden_seconds = hidden_seconds
        self._operation_timeout_seconds = operation_timeout_seconds
        self._pulses: dict[str, _TurnReactionPulse] = {}
        self._closed = False

    async def start(
        self,
        turn_id: str,
        message_id: str,
        *,
        pulse_enabled: bool,
    ) -> bool:
        if type(pulse_enabled) is not bool:
            raise ValueError("reaction pulse setting must be a boolean")
        if not turn_id or not message_id:
            logger.error(
                "failed to start turn reactions: missing identity",
                extra={"turn_id": turn_id, "message_id": message_id},
            )
            return False
        if self._closed:
            logger.warning(
                "turn reaction controller is closed",
                extra={"turn_id": turn_id, "message_id": message_id},
            )
            return False
        if turn_id in self._pulses:
            await self.stop(turn_id)
        pulse = _TurnReactionPulse(
            turn_id=turn_id,
            message_id=message_id,
            stopped=asyncio.Event(),
        )
        self._pulses[turn_id] = pulse
        reaction_id = await self._add_reaction(pulse, _TYPING_REACTION)
        if self._pulses.get(turn_id) is not pulse:
            if reaction_id is not None:
                await self._remove_reaction(
                    pulse,
                    reaction_id,
                    _TYPING_REACTION,
                )
            return False
        if reaction_id is None:
            self._pulses.pop(turn_id, None)
            return False
        pulse.typing_reaction_id = reaction_id
        if not pulse_enabled:
            return True

        reaction_id = await self._add_reaction(pulse, _THINKING_REACTION)
        if self._pulses.get(turn_id) is not pulse:
            if reaction_id is not None:
                await self._remove_reaction(
                    pulse,
                    reaction_id,
                    _THINKING_REACTION,
                )
            return False
        if reaction_id is None:
            # Keep the stable Typing placeholder even when the optional pulse
            # cannot start. Terminal/shutdown cleanup still owns its exact ID.
            return True
        pulse.thinking_reaction_id = reaction_id
        pulse.task = asyncio.create_task(
            self._pulse(pulse),
            name=f"netizen-thinking-{turn_id}",
        )
        return True

    async def freeze(self, turn_id: str) -> None:
        """Stop future pulse operations without removing visible reactions."""

        pulse = self._pulses.get(turn_id)
        if pulse is None:
            return
        pulse.stopped.set()
        await self._wait_for_task(pulse)

    async def stop(self, turn_id: str) -> None:
        pulse = self._pulses.pop(turn_id, None)
        if pulse is None:
            return
        pulse.stopped.set()
        await self._wait_for_task(pulse)
        reaction_id = pulse.thinking_reaction_id
        if reaction_id is not None:
            if await self._remove_reaction(
                pulse,
                reaction_id,
                _THINKING_REACTION,
            ):
                pulse.thinking_reaction_id = None
        reaction_id = pulse.typing_reaction_id
        if reaction_id is not None:
            if await self._remove_reaction(
                pulse,
                reaction_id,
                _TYPING_REACTION,
            ):
                pulse.typing_reaction_id = None

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await asyncio.gather(
            *(self.stop(turn_id) for turn_id in tuple(self._pulses)),
            return_exceptions=False,
        )

    async def _wait_for_task(self, pulse: _TurnReactionPulse) -> None:
        task = pulse.task
        if task is None or task is asyncio.current_task():
            return
        try:
            await task
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "thinking reaction pulse failed while stopping",
                extra={
                    "turn_id": pulse.turn_id,
                    "message_id": pulse.message_id,
                },
            )

    async def _pulse(self, pulse: _TurnReactionPulse) -> None:
        while not pulse.stopped.is_set():
            if await self._wait(pulse.stopped, self._visible_seconds):
                return
            reaction_id = pulse.thinking_reaction_id
            if reaction_id is None:
                return
            if not await self._remove_reaction(
                pulse,
                reaction_id,
                _THINKING_REACTION,
            ):
                # Preserve the exact ID so terminal/shutdown cleanup gets one
                # final best-effort removal attempt without a retry storm.
                return
            pulse.thinking_reaction_id = None
            if await self._wait(pulse.stopped, self._hidden_seconds):
                return
            reaction_id = await self._add_reaction(pulse, _THINKING_REACTION)
            if reaction_id is None:
                return
            pulse.thinking_reaction_id = reaction_id

    @staticmethod
    async def _wait(stopped: asyncio.Event, seconds: float) -> bool:
        try:
            await asyncio.wait_for(stopped.wait(), timeout=seconds)
        except TimeoutError:
            return False
        return True

    async def _add_reaction(
        self,
        pulse: _TurnReactionPulse,
        emoji_type: str,
    ) -> str | None:
        try:
            async with asyncio.timeout(self._operation_timeout_seconds):
                result = await self._channel.add_reaction(
                    pulse.message_id,
                    emoji_type,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "failed to add turn reaction",
                extra={
                    "turn_id": pulse.turn_id,
                    "message_id": pulse.message_id,
                    "emoji_type": emoji_type,
                },
            )
            return None
        if getattr(result, "success", False) is not True:
            logger.error(
                "failed to add turn reaction: unsuccessful result",
                extra={
                    "turn_id": pulse.turn_id,
                    "message_id": pulse.message_id,
                    "emoji_type": emoji_type,
                },
            )
            return None
        raw = getattr(result, "raw", None)
        if raw is None and isinstance(result, dict):
            raw = result
        data = raw.get("data") if isinstance(raw, dict) else None
        reaction_id = data.get("reaction_id") if isinstance(data, dict) else None
        if not isinstance(reaction_id, str) or not reaction_id:
            logger.error(
                "failed to add turn reaction: missing reaction ID",
                extra={
                    "turn_id": pulse.turn_id,
                    "message_id": pulse.message_id,
                    "emoji_type": emoji_type,
                },
            )
            return None
        return reaction_id

    async def _remove_reaction(
        self,
        pulse: _TurnReactionPulse,
        reaction_id: str,
        emoji_type: str,
    ) -> bool:
        try:
            async with asyncio.timeout(self._operation_timeout_seconds):
                result = await self._channel.remove_reaction(
                    pulse.message_id,
                    reaction_id,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "failed to remove turn reaction",
                extra={
                    "turn_id": pulse.turn_id,
                    "message_id": pulse.message_id,
                    "reaction_id": reaction_id,
                    "emoji_type": emoji_type,
                },
            )
            return False
        if getattr(result, "success", False) is not True:
            logger.error(
                "failed to remove turn reaction: unsuccessful result",
                extra={
                    "turn_id": pulse.turn_id,
                    "message_id": pulse.message_id,
                    "reaction_id": reaction_id,
                    "emoji_type": emoji_type,
                },
            )
            return False
        return True
