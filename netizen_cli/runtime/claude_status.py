"""Bounded scalars copied at the existing Claude receiver's attribution boundary."""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
import math
import re
import time

from claude_agent_sdk import RateLimitInfo, ResultMessage

from ..session_views import Availability, SessionField, SessionObservations


@dataclass(slots=True)
class _LastSession:
    session: str
    execution: str
    fields: dict[str, SessionField] = field(default_factory=dict)


class ClaudeObservations:
    def __init__(self, *, capacity: int = 512, clock: Callable[[], float] = time.time) -> None:
        self._sessions: OrderedDict[str, _LastSession] = OrderedDict()
        self._capacity = capacity
        self._clock = clock

    def init(self, binding: str, session: str, execution: str, model: object, cwd: str) -> None:
        sample = self._sessions.get(binding)
        if sample is None or sample.session != session:
            sample = self._sessions[binding] = _LastSession(session, execution)
        sample.execution = execution
        self._sessions.move_to_end(binding)
        if len(self._sessions) > self._capacity:
            self._sessions.popitem(last=False)
        now = self._clock()
        sample.fields["cwd"] = SessionField(Availability.OBSERVED, "Claude init", cwd, now)
        self._set(sample, "model", model if isinstance(model, str) and re.fullmatch(r"[\w./:-]{1,160}", model) else None, "Claude init", now)

    @staticmethod
    def _set(sample: _LastSession, name: str, value: str | int | float | None, source: str, now: float) -> None:
        sample.fields[name] = SessionField(Availability.UNAVAILABLE if value is None else Availability.OBSERVED, source, value, now)

    def _exact(self, binding: str, session: str, execution: str) -> _LastSession | None:
        sample = self._sessions.get(binding)
        return sample if sample and (sample.session, sample.execution) == (session, execution) else None

    def rate(self, binding: str, session: str, execution: str, info: RateLimitInfo) -> None:
        sample = self._exact(binding, session, execution)
        if sample is None or not isinstance(info, RateLimitInfo):
            return
        now = self._clock()
        status = info.status if info.status in ("allowed", "allowed_warning", "rejected") else None
        window = info.rate_limit_type if info.rate_limit_type in ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet", "overage") else None
        fraction = info.utilization
        self._set(sample, "limitStatus", status, "Claude rate_limit", now)
        self._set(sample, "limitWindow", window, "Claude rate_limit", now)
        self._set(sample, "limitReset", _count(info.resets_at), "Claude rate_limit", now)
        self._set(sample, "limitUtilization", fraction if type(fraction) in (float, int) and fraction is not None and 0 <= fraction <= 1 else None, "Claude rate_limit", now)

    def result(self, binding: str, session: str, execution: str, msg: ResultMessage) -> None:
        sample = self._exact(binding, session, execution)
        if sample is None:
            return
        usage = msg.usage if isinstance(msg.usage, dict) else {}
        now = self._clock()
        for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            self._set(sample, key, _count(usage.get(key)), "Claude Result", now)
        cost = msg.total_cost_usd
        valid = type(cost) in (int, float) and cost is not None and 0 <= cost <= 1e100 and math.isfinite(cost)
        self._set(sample, "totalCostUsd", cost if valid else None, "Claude Result", now)

    def read(self, binding: str, session: str | None) -> SessionObservations:
        sample = self._sessions.get(binding)
        return SessionObservations(tuple(sample.fields.items())) if sample and session == sample.session else SessionObservations()


def _count(value: object) -> int | None:
    return value if type(value) is int and 0 <= value < 2**63 else None
