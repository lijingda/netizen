"""Runtime protocols, errors and immutable operation inputs and snapshots.

These types describe the shared boundary. They do not own execution tasks,
locks, subscriptions or persistence.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from ..bindings import BindingTaskFeedback, SideTopicState
from ..domain import ActiveState, GoalOperationState, MessageContextAnchor
from ..sdk_gap_adapter import GoalSnapshot
from ..turn_activity import TurnActivityEntrySnapshot, TurnPlanStepSnapshot
from ..turn_patch_children import TaskPatchChildren


class NativeTurnHandle(Protocol):
    id: str
    thread_id: str

    async def steer(self, input: Any) -> object: ...

    async def interrupt(self) -> object: ...

    async def run(self) -> object: ...

    def stream(self) -> AsyncIterator[object]: ...


class NativeThread(Protocol):
    id: str

    async def turn(self, input: Any, **kwargs: object) -> NativeTurnHandle: ...

    async def read(self, *, include_turns: bool = False) -> object: ...

    async def compact(self) -> object: ...

    async def set_name(self, name: str) -> object: ...


class NativeCodex(Protocol):
    async def thread_start(self, **kwargs: object) -> NativeThread: ...

    async def thread_resume(self, thread_id: str, **kwargs: object) -> NativeThread: ...

    async def thread_list(self, **kwargs: object) -> object: ...

    async def thread_archive(self, thread_id: str) -> object: ...

    async def thread_unarchive(self, thread_id: str) -> NativeThread: ...

    async def thread_fork(
        self,
        thread_id: str,
        **kwargs: object,
    ) -> NativeThread: ...

    async def models(self, *, include_hidden: bool = False) -> object: ...


class RuntimeClosed(RuntimeError):
    pass


class ThreadStopping(RuntimeError):
    pass


class ThreadRunningConfiguration(RuntimeError):
    pass


class ThreadCompacting(RuntimeError):
    pass


class ThreadNotMaterialized(RuntimeError):
    pass


class SteerRace(RuntimeError):
    pass


class TerminalCleanupFailed(RuntimeError):
    pass


class TurnInterruptFailed(RuntimeError):
    pass


class TurnStartFailed(RuntimeError):
    pass


class ContextBoundaryCommitFailed(RuntimeError):
    """The native submission succeeded but its catch-up cursor did not."""

    pass


class ThreadCompactStartFailed(RuntimeError):
    pass


class CompactionFailed(RuntimeError):
    pass


class CompactionStateUnknown(RuntimeError):
    pass


class TerminalStateUnknown(RuntimeError):
    pass


class SkillReferenceError(RuntimeError):
    pass


class ThreadGoalActive(RuntimeError):
    pass


class ExternalGoalActive(ThreadGoalActive):
    pass


class GoalNotFound(RuntimeError):
    pass


class GoalNotMaterialized(RuntimeError):
    pass


class GoalStateUnknown(RuntimeError):
    pass


class ThreadLifecycleError(RuntimeError):
    pass


class TurnObservationUnavailable(ThreadLifecycleError):
    pass


class ScheduledInitialStartConflict(ThreadLifecycleError):
    """An initial scheduled submission can no longer start its exact Binding."""


class ScheduledTurnReadError(ThreadLifecycleError):
    """One bounded read could not establish the scheduled exact Turn state."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ContextAnchorRequired(ThreadLifecycleError):
    pass


class ThreadLifecycleStateUnknown(ThreadLifecycleError):
    pass


class ThreadArchived(ThreadLifecycleError):
    pass


class ThreadNotArchived(ThreadLifecycleError):
    pass


class ThreadDeleteUnavailable(ThreadLifecycleError):
    pass


class ThreadDeleteTargetChanged(ThreadLifecycleError):
    pass


class ThreadActivityChanged(ThreadLifecycleError):
    pass


class ThreadReleaseError(ThreadLifecycleError):
    pass


class ThreadReleaseStateUnknown(ThreadReleaseError):
    pass


class ThreadBackgroundTerminalsActive(ThreadReleaseError):
    pass


class ThreadCatalogError(RuntimeError):
    pass


class ThreadCatalogDeadlineExceeded(ThreadCatalogError):
    pass


class ThreadCatalogLimitExceeded(ThreadCatalogError):
    pass


class ThreadCatalogIdentityMissing(ThreadCatalogError):
    pass


class SideUnavailable(RuntimeError):
    pass


class SideSessionNotFound(LookupError):
    pass


class SideSessionConflict(RuntimeError):
    pass


class SideSessionClosing(RuntimeError):
    pass


class SideStartFailed(RuntimeError):
    pass


class SideCloseFailed(RuntimeError):
    pass


class SubmitDisposition(str, Enum):
    STARTED = "started"
    STEERED = "steered"


class GoalFinalizationStatus(str, Enum):
    NOT_APPLICABLE = "not-applicable"
    CLEARED = "cleared"
    UNKNOWN = "unknown"


class ThreadLifecycleState(str, Enum):
    RENAMING = "renaming"
    ARCHIVING = "archiving"
    UNARCHIVING = "unarchiving"
    DELETING = "deleting"
    UNKNOWN = "lifecycle-unknown"


class StopDisposition(str, Enum):
    NOT_RUNNING = "not-running"
    REQUESTED = "requested"
    STOPPING = "stopping"
    COMPACTING = "compacting"
    GOAL_REQUESTED = "goal-requested"
    GOAL_STOPPING = "goal-stopping"
    EXTERNAL_GOAL = "externally-active-goal"


class SideSessionState(str, Enum):
    OPEN = "open"
    CLOSING = "closing"


class ThreadSubscriptionState(str, Enum):
    SUBSCRIBED = "subscribed"
    RELEASE_PENDING = "release-pending"
    RELEASING = "releasing"
    RELEASED = "released"
    RELEASE_UNKNOWN = "release-unknown"


class ReleaseDisposition(str, Enum):
    NOT_MATERIALIZED = "not-materialized"
    NOT_SUBSCRIBED = "not-subscribed"
    RELEASED = "released"


@dataclass(frozen=True, slots=True)
class Submission:
    disposition: SubmitDisposition
    binding_id: str
    thread_id: str
    turn_id: str
    release_receipt_attempt: Callable[[], None] | None = None
    task_feedback: BindingTaskFeedback = BindingTaskFeedback()
    feedback_revision: int = 1

    def __post_init__(self) -> None:
        if self.feedback_revision < 1:
            raise ValueError("feedback revision must be positive")


@dataclass(frozen=True, slots=True)
class CompactSubmission:
    binding_id: str
    thread_id: str
    release_receipt_attempt: Callable[[], None]


@dataclass(frozen=True, slots=True)
class GoalSubmission:
    binding_id: str
    thread_id: str
    logical_turn_id: str
    release_receipt_attempt: Callable[[], None]
    task_feedback: BindingTaskFeedback = BindingTaskFeedback()
    feedback_revision: int = 1

    def __post_init__(self) -> None:
        if self.feedback_revision < 1:
            raise ValueError("feedback revision must be positive")


@dataclass(frozen=True, slots=True)
class SideSubmission:
    disposition: SubmitDisposition
    side_id: str
    thread_id: str
    turn_id: str
    release_receipt_attempt: Callable[[], None] | None = None
    task_feedback: BindingTaskFeedback = BindingTaskFeedback()
    feedback_revision: int = 1

    def __post_init__(self) -> None:
        if self.feedback_revision < 1:
            raise ValueError("feedback revision must be positive")


@dataclass(frozen=True, slots=True)
class SideSubmissionAdmission:
    side_id: str
    revision: int
    thread_id: str
    turn_id: str | None

    def __post_init__(self) -> None:
        if self.revision < 0:
            raise ValueError("Side admission revision must be non-negative")
        if not self.side_id or not self.thread_id:
            raise ValueError("Side admission identity must not be empty")


@dataclass(frozen=True, slots=True)
class ActiveTurnSnapshot:
    binding_id: str
    thread_id: str
    turn_id: str
    owner_id: str
    state: ActiveState


@dataclass(frozen=True, slots=True)
class TurnProgressSnapshot:
    binding_id: str
    thread_id: str
    turn_id: str
    steer_count: int
    plan_available: bool
    plan_generated: bool
    plan_may_be_stale: bool
    steps: tuple[TurnPlanStepSnapshot, ...]


@dataclass(frozen=True, slots=True)
class TurnActivitySnapshot:
    """Latest bounded display projection for one exact active Turn.

    The projection keeps only bounded, allowlisted activity and the latest full
    plan replacement. It is process-local display data, not a Turn history or
    a second terminal-state authority.
    """

    binding_id: str
    thread_id: str
    turn_id: str
    revision: int
    state: ActiveState
    steer_count: int
    plan_available: bool
    plan_generated: bool
    plan_may_be_stale: bool
    steps: tuple[TurnPlanStepSnapshot, ...]
    commentary: tuple[TurnActivityEntrySnapshot, ...] = ()
    operations: tuple[TurnActivityEntrySnapshot, ...] = ()

    def __post_init__(self) -> None:
        if not self.binding_id or not self.thread_id or not self.turn_id:
            raise ValueError("Turn activity identity must not be empty")
        if self.revision < 1:
            raise ValueError("Turn activity revision must be positive")
        if self.steer_count < 0:
            raise ValueError("Turn activity steer count must be non-negative")


@dataclass(frozen=True, slots=True)
class SideTurnActivitySnapshot:
    """Latest bounded display projection for one exact active Side Turn."""

    side_id: str
    thread_id: str
    turn_id: str
    revision: int
    state: ActiveState
    steer_count: int
    plan_available: bool
    plan_generated: bool
    plan_may_be_stale: bool
    steps: tuple[TurnPlanStepSnapshot, ...]
    commentary: tuple[TurnActivityEntrySnapshot, ...] = ()
    operations: tuple[TurnActivityEntrySnapshot, ...] = ()

    def __post_init__(self) -> None:
        if not self.side_id or not self.thread_id or not self.turn_id:
            raise ValueError("Side Turn activity identity must not be empty")
        if self.revision < 1:
            raise ValueError("Side Turn activity revision must be positive")
        if self.steer_count < 0:
            raise ValueError(
                "Side Turn activity steer count must be non-negative"
            )


@dataclass(frozen=True, slots=True)
class GoalActivitySnapshot:
    """Latest bounded display projection for one exact native Goal run."""

    binding_id: str
    thread_id: str
    logical_turn_id: str
    physical_turn_id: str | None
    revision: int
    state: GoalOperationState
    plan_available: bool
    plan_generated: bool
    steps: tuple[TurnPlanStepSnapshot, ...]
    commentary: tuple[TurnActivityEntrySnapshot, ...] = ()
    operations: tuple[TurnActivityEntrySnapshot, ...] = ()

    def __post_init__(self) -> None:
        if not self.binding_id or not self.thread_id or not self.logical_turn_id:
            raise ValueError("Goal activity identity must not be empty")
        if self.revision < 1:
            raise ValueError("Goal activity revision must be positive")


@dataclass(frozen=True, slots=True)
class NativeThreadMetadata:
    thread_id: str
    name: str | None
    preview: str


class NativeThreadCatalogState(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class NativeThreadCatalog:
    archived: bool
    threads: tuple[NativeThreadMetadata, ...]

    def by_id(self) -> dict[str, NativeThreadMetadata]:
        return {thread.thread_id: thread for thread in self.threads}


@dataclass(frozen=True, slots=True)
class ContextWindowUsage:
    used_tokens: int
    context_window_tokens: int | None

    def __post_init__(self) -> None:
        if (
            isinstance(self.used_tokens, bool)
            or not isinstance(self.used_tokens, int)
            or self.used_tokens < 0
        ):
            raise ValueError("used tokens must be a non-negative integer")
        if self.context_window_tokens is not None and (
            isinstance(self.context_window_tokens, bool)
            or not isinstance(self.context_window_tokens, int)
            or self.context_window_tokens <= 0
        ):
            raise ValueError("context window tokens must be a positive integer")


@dataclass(frozen=True, slots=True)
class ActiveGoalSnapshot:
    binding_id: str
    thread_id: str
    logical_turn_id: str | None
    owner_id: str
    state: GoalOperationState
    persisted: GoalSnapshot | None = None


@dataclass(frozen=True, slots=True)
class ThreadLifecycleSnapshot:
    binding_id: str
    thread_id: str | None
    state: ThreadLifecycleState


@dataclass(frozen=True, slots=True)
class ThreadSubscriptionSnapshot:
    binding_id: str
    thread_id: str
    state: ThreadSubscriptionState
    release_in_seconds: float | None


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
class SideSessionSnapshot:
    side_id: str
    parent_binding_id: str
    parent_thread_id: str
    thread_id: str
    project_alias: str
    cwd: Path
    creator_id: str
    state: SideSessionState
    topic_id: str | None
    root_message_id: str | None
    turn_id: str | None
    turn_state: ActiveState | None
    last_activity: float


@dataclass(frozen=True, slots=True)
class SubmissionAdmission:
    """Condition captured before asynchronous prompt preparation.

    The monotonic revision prevents an idle -> running -> idle ABA from
    turning a delayed prompt into a different native submission.
    """

    binding_id: str
    revision: int
    thread_id: str | None
    turn_id: str | None
    settings_revision: int
    context_revision: int = 1
    feedback_revision: int = 1

    def __post_init__(self) -> None:
        if self.revision < 0:
            raise ValueError("submission admission revision must be non-negative")
        if self.settings_revision < 1:
            raise ValueError("settings revision must be positive")
        if self.context_revision < 1:
            raise ValueError("context revision must be positive")
        if self.feedback_revision < 1:
            raise ValueError("feedback revision must be positive")
        if (self.thread_id is None) != (self.turn_id is None):
            raise ValueError(
                "submission admission thread_id and turn_id must both be set or unset"
            )


@dataclass(frozen=True, slots=True)
class ContextCursorCommit:
    """Catch-up boundary to commit after one exact native submission."""

    expected_context_revision: int
    anchor: MessageContextAnchor

    def __post_init__(self) -> None:
        if self.expected_context_revision < 1:
            raise ValueError("expected context revision must be positive")


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    binding_id: str
    thread_id: str
    turn_id: str
    owner_id: str
    origin: object
    result: object | None = None
    error: BaseException | None = None
    # True only means the exact backgroundTerminals/clean RPC returned
    # successfully. It does not attest that a foreground tool process exited.
    background_cleanup_requested: bool = False
    # Latest aggregate snapshot observed from the public
    # ``turn/diff/updated`` notification for this exact Turn.  It is carried
    # only through completion delivery and is never persisted by Netizen.
    turn_diff: str | None = None
    task_feedback: BindingTaskFeedback = BindingTaskFeedback()
    feedback_revision: int = 1
    activity: TurnActivitySnapshot | None = None
    patch_children: TaskPatchChildren = TaskPatchChildren()

    def __post_init__(self) -> None:
        if self.feedback_revision < 1:
            raise ValueError("feedback revision must be positive")
        if self.activity is not None and (
            self.activity.binding_id != self.binding_id
            or self.activity.thread_id != self.thread_id
            or self.activity.turn_id != self.turn_id
        ):
            raise ValueError("Turn outcome activity belongs to another Turn")

    @property
    def final_response(self) -> str | None:
        value = getattr(self.result, "final_response", None)
        return value if isinstance(value, str) else None

    @property
    def status(self) -> str | None:
        status = getattr(self.result, "status", None)
        value = getattr(status, "value", status)
        return value if isinstance(value, str) else None


@dataclass(frozen=True, slots=True)
class TurnObservationUnavailableOutcome:
    """One non-terminal notice that bounded exact Turn observation failed."""

    binding_id: str
    thread_id: str
    turn_id: str
    owner_id: str
    origin: object
    error: TerminalStateUnknown
    state: ActiveState = ActiveState.OBSERVATION_UNAVAILABLE


@dataclass(frozen=True, slots=True)
class ThreadActivityDiscardedOutcome:
    """Presentation-only notice after native Thread removal is committed."""

    binding_id: str
    thread_id: str
    turn_id: str | None


@dataclass(frozen=True, slots=True)
class CompactionOutcome:
    binding_id: str
    thread_id: str
    owner_id: str
    origin: object
    compact_turn_id: str | None = None
    status: str | None = None
    error: BaseException | None = None


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
class SideTurnOutcome:
    side_id: str
    parent_binding_id: str
    thread_id: str
    turn_id: str
    owner_id: str
    origin: object
    cwd: Path
    result: object | None = None
    error: BaseException | None = None
    background_cleanup_requested: bool = False
    task_feedback: BindingTaskFeedback = BindingTaskFeedback()
    feedback_revision: int = 1
    activity: SideTurnActivitySnapshot | None = None
    patch_children: TaskPatchChildren = TaskPatchChildren()

    def __post_init__(self) -> None:
        if self.feedback_revision < 1:
            raise ValueError("feedback revision must be positive")
        if self.activity is not None and (
            self.activity.side_id != self.side_id
            or self.activity.thread_id != self.thread_id
            or self.activity.turn_id != self.turn_id
        ):
            raise ValueError("Side outcome activity belongs to another Side Turn")

    @property
    def final_response(self) -> str | None:
        value = getattr(self.result, "final_response", None)
        return value if isinstance(value, str) else None

    @property
    def status(self) -> str | None:
        status = getattr(self.result, "status", None)
        value = getattr(status, "value", status)
        return value if isinstance(value, str) else None


@dataclass(frozen=True, slots=True)
class SideLifecycleOutcome:
    side_id: str
    state: SideTopicState
    error: BaseException | None = None


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


StopAcknowledger = Callable[[], Awaitable[None]]
