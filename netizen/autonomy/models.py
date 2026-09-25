"""Provider-neutral values for the removable autonomous-input experiment."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


class AutonomyError(ValueError):
    """Safe, public error: never include supplied secrets or remote responses."""


class AutonomyConflict(AutonomyError):
    pass


@dataclass(frozen=True)
class Candidate:
    message_id: str
    text: str
    sender: str = "用户"
    message_type: str = "text"


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    revision: int = 0
    received: int = 0
    last_accepted: int | None = None


@dataclass(frozen=True)
class DecisionToken:
    binding_id: str
    binding_revision: int
    config_revision: int
    sequence: int
    last_accepted: int | None
    message_id: str
    explicit: bool = False


@dataclass(frozen=True)
class Decision:
    outcome: Literal["consume", "skip", "unavailable"]
    token: DecisionToken | None = None
    gap_hint: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class Record:
    sequence: int
    kind: str
    reference: str
    text: str


@dataclass(frozen=True)
class Context:
    summary: str = ""
    summary_revision: int = 0
    records: tuple[Record, ...] = ()


@dataclass(frozen=True)
class DecisionConfig:
    provider: str
    base_url: str
    model: str
    api_key: str = field(repr=False)
    timeout_seconds: float = 10
    input_budget: int = 32000
