"""Codex Goal snapshots and the runtime-wide completion callback."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..ordinary_sessions import LogicalReply

from ..bindings import BindingTaskFeedback
from ..domain import GoalOperationState
from ..partial_answers import PartialAnswer
from ..sdk_gap_adapter import GoalSnapshot
from ..turn_patch_children import TaskPatchChildren
from .contracts import (
    ActiveTurnSnapshot, CompactionOutcome, ContextWindowUsage, GoalActivitySnapshot,
    GoalFinalizationStatus, SideLifecycleOutcome, SideTurnOutcome, ThreadActivityDiscardedOutcome,
    ThreadLifecycleSnapshot, ThreadSubscriptionSnapshot, TurnObservationUnavailableOutcome, TurnOutcome,
)


@dataclass(frozen=True, slots=True)
class ActiveGoalSnapshot:
    binding_id: str
    thread_id: str
    logical_turn_id: str | None
    owner_id: str
    state: GoalOperationState
    persisted: GoalSnapshot | None = None


@dataclass(frozen=True, slots=True)
class BindingRuntimeSnapshot:
    """One immutable, process-local view of an ordinary Binding's activity."""

    binding_id: str
    activity_revision: int
    turn: ActiveTurnSnapshot | None
    goal: ActiveGoalSnapshot | None
    compacting: bool
    lifecycle: ThreadLifecycleSnapshot | None
    subscription: ThreadSubscriptionSnapshot | None
    context_window_usage: ContextWindowUsage | None

    def __post_init__(self) -> None:
        if not self.binding_id:
            raise ValueError("Binding runtime snapshot identity must not be empty")
        if self.activity_revision < 0:
            raise ValueError("Binding runtime snapshot revision must be non-negative")


@dataclass(frozen=True, slots=True)
class GoalOutcome:
    binding_id: str
    thread_id: str
    logical_turn_id: str | None
    owner_id: str
    origin: object
    goal: GoalSnapshot | None = None
    final_physical_turn_id: str | None = None
    final_turn_status: str | None = None
    final_items: tuple[object, ...] = ()
    final_response: str | None = None
    # Latest aggregate diff captured for the exact final physical Turn by the
    # existing Goal notification consumer. It is never reconstructed from
    # persisted Turn history or stored by Netizen.
    turn_diff: str | None = None
    error: BaseException | None = None
    background_cleanup_requested: bool = False
    task_feedback: BindingTaskFeedback = BindingTaskFeedback()
    feedback_revision: int = 1
    activity: GoalActivitySnapshot | None = None
    finalization: GoalFinalizationStatus = GoalFinalizationStatus.NOT_APPLICABLE
    finalization_error: BaseException | None = None
    patch_children: TaskPatchChildren = TaskPatchChildren()
    partial_answers: tuple[PartialAnswer, ...] = ()
    cwd: Path | None = None
    run_continues: bool = False
    earlier_delivery_incomplete: bool = False
    reply: LogicalReply | None = None

    def __post_init__(self) -> None:
        if self.feedback_revision < 1:
            raise ValueError("feedback revision must be positive")
        if self.activity is not None and (
            self.activity.binding_id != self.binding_id
            or self.activity.thread_id != self.thread_id
            or self.activity.logical_turn_id != self.logical_turn_id
        ):
            raise ValueError("Goal outcome activity belongs to another Goal run")


@dataclass(frozen=True, slots=True)
class GoalRollover:
    previous: GoalOutcome
    opening: GoalActivitySnapshot


RuntimeOutcome = (
    TurnOutcome
    | TurnObservationUnavailableOutcome
    | ThreadActivityDiscardedOutcome
    | CompactionOutcome
    | GoalOutcome
    | SideTurnOutcome
    | SideLifecycleOutcome
)


CompletionHandler = Callable[[RuntimeOutcome], Awaitable[None]]
