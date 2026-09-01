"""Thin orchestration over public AsyncCodex Thread and Turn handles."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from openai_codex import (
    ImageInput,
    InternalRpcError,
    InvalidRequestError,
    SkillInput,
    TextInput,
    TransportClosedError,
    TurnResult,
    is_retryable_error,
)
from openai_codex.types import ThreadTokenUsageUpdatedNotification

from .bindings import (
    BindingStore,
    BindingTaskFeedback,
    BindingTurnSettings,
    SideTopicConflict,
    SideTopicState,
    ThreadBinding,
)
from .domain import (
    ActiveState,
    GoalOperationState,
    GoalStatus,
    MessageContextAnchor,
    MentionContextMode,
    NativeCapability,
)
from .model_settings import ModelCatalog, TurnModelSettings
from .sdk_gap_adapter import (
    GoalControl,
    GoalControlError,
    GoalHandle,
    GoalMutationStateUnknown,
    GoalSnapshot,
    GoalStreamTerminal,
    SideBoundaryControl,
    SkillCatalog,
    ThreadDeleteControl,
    ThreadSubscriptionControl,
    ThreadUnsubscribeStatus,
    ThreadUnsubscribeStateUnknown,
)
from .terminal_cleanup import BackgroundTerminalInspector, TerminalCleanup
from .turn_activity import (
    ACTIVITY_COMMENTARY_LIMIT,
    ACTIVITY_OPERATION_LIMIT,
    SIDE_ACTIVITY_QUEUE_HIGH_WATER,
    TurnActivityEntrySnapshot,
    TurnActivityEvent,
    TurnActivityKind,
    TurnActivityNotificationProjection,
    TurnPlanStepSnapshot,
)
from .turn_plan_observer import TurnActivityObservation, TurnActivityObserver


logger = logging.getLogger(__name__)

_NOT_MATERIALIZED_SUFFIX = (
    "is not materialized yet; includeTurns is unavailable before first user message"
)
_STOP_ACK_ATTEMPT_TIMEOUT_SECONDS = 5.0
_COMPACTION_TERMINAL_TIMEOUT_SECONDS = 600.0
_GOAL_COMPLETION_DELIVERY_TIMEOUT_SECONDS = 20.0
_THREAD_LIST_PAGE_LIMIT = 100
_THREAD_CATALOG_MAX_PAGES = 1_000
_THREAD_CATALOG_MAX_ITEMS = 100_000
_THREAD_DELETE_RECONCILE_TIMEOUT_SECONDS = 20.0
_TURN_OBSERVATION_RECOVERY_TIMEOUT_SECONDS = 5.0
_TURN_OBSERVATION_RECOVERY_MAX_IO = 3
_TERMINAL_RESPONSE_MATERIALIZATION_RETRIES = 4
_SIDE_CLOSE_DRAIN_TIMEOUT_SECONDS = 5.0
_SIDE_IDLE_SECONDS = 2 * 60 * 60
_ORDINARY_THREAD_IDLE_SECONDS = 15 * 60


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


class _TurnViewUnverified(RuntimeError):
    """One read cycle could not establish an exact authoritative Turn view."""


class _TurnResumeRequired(_TurnViewUnverified):
    """One bounded recovery attempt must replace the native Thread handle."""


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
    commentary: tuple[str, ...] = ()
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
    commentary: tuple[str, ...] = ()
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
    commentary: tuple[str, ...] = ()
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
    error: BaseException | None = None
    background_cleanup_requested: bool = False
    task_feedback: BindingTaskFeedback = BindingTaskFeedback()
    feedback_revision: int = 1
    activity: GoalActivitySnapshot | None = None
    finalization: GoalFinalizationStatus = GoalFinalizationStatus.NOT_APPLICABLE
    finalization_error: BaseException | None = None

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


async def _ignore_completion(_outcome: RuntimeOutcome) -> None:
    return None


@dataclass(slots=True)
class _ActiveTurn:
    binding_id: str
    thread: NativeThread
    handle: NativeTurnHandle
    owner_id: str
    origin: object
    receipt_attempted: asyncio.Event
    state: ActiveState = ActiveState.RUNNING
    interrupt_attempted: bool = False
    interrupt_succeeded: bool = False
    cleanup_required: bool = False
    cleanup_succeeded: bool = False
    terminal_observed: bool = False
    cleanup_ready: asyncio.Event = field(default_factory=asyncio.Event)
    terminal_stream_safe: bool = False
    latest_diff: str | None = None
    task_feedback: BindingTaskFeedback = BindingTaskFeedback()
    feedback_revision: int = 1
    activity_revision: int = 1
    activity_observation_enabled: bool = True
    steer_count: int = 0
    plan_cursor: int = 0
    plan_generated: bool = False
    plan_available: bool = True
    plan_may_be_stale: bool = False
    plan_stale_after_cursor: int | None = None
    plan_last_update_cursor: int | None = None
    plan_steps: tuple[TurnPlanStepSnapshot, ...] = ()
    activity_commentary: dict[str, str] = field(default_factory=dict)
    activity_commentary_order: list[str] = field(default_factory=list)
    activity_operations: dict[str, TurnActivityEvent] = field(default_factory=dict)
    activity_operation_order: list[str] = field(default_factory=list)
    task: asyncio.Task[None] | None = None


class _TurnObservation(Enum):
    ACTIVE = "active"
    EXACT_IN_PROGRESS = "exact-in-progress"


@dataclass(slots=True)
class _ActiveCompaction:
    binding_id: str
    thread: NativeThread
    owner_id: str
    origin: object
    before_turn_ids: frozenset[str]
    receipt_attempted: asyncio.Event
    compact_turn_id: str | None = None
    status: str | None = None
    terminal_observed: bool = False
    task: asyncio.Task[None] | None = None


@dataclass(slots=True)
class _ActiveGoal:
    binding_id: str
    thread_id: str
    thread: NativeThread | None
    handle: GoalHandle | None
    owner_id: str
    origin: object
    objective: str
    receipt_attempted: asyncio.Event
    state: GoalOperationState
    persisted: GoalSnapshot | None = None
    generation_created_at: int | None = None
    generation_token_budget: int | None = None
    stream_terminal: GoalStreamTerminal | None = None
    final_turn_status: str | None = None
    final_items: tuple[object, ...] = ()
    final_response: str | None = None
    task_feedback: BindingTaskFeedback = BindingTaskFeedback()
    feedback_revision: int = 1
    activity_revision: int = 1
    activity_observation_enabled: bool = True
    activity_turn_id: str | None = None
    plan_cursor: int = 0
    plan_generated: bool = False
    plan_available: bool = True
    plan_steps: tuple[TurnPlanStepSnapshot, ...] = ()
    activity_commentary: dict[str, str] = field(default_factory=dict)
    activity_commentary_order: list[str] = field(default_factory=list)
    activity_operations: dict[str, TurnActivityEvent] = field(default_factory=dict)
    activity_operation_order: list[str] = field(default_factory=list)
    pause_attempted: bool = False
    interrupt_acknowledged: bool = False
    cleanup_required: bool = False
    cleanup_succeeded: bool = False
    terminal_observed: bool = False
    cleanup_ready: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None


def _same_goal_generation(
    active: _ActiveGoal,
    snapshot: GoalSnapshot,
) -> bool:
    """Compare every immutable Goal identity field exposed by the SDK."""

    return (
        active.generation_created_at is not None
        and snapshot.thread_id == active.thread_id
        and snapshot.created_at == active.generation_created_at
        and snapshot.objective == active.objective
        and snapshot.token_budget == active.generation_token_budget
    )


@dataclass(frozen=True, slots=True)
class _ContextWindowUsageSnapshot:
    thread_id: str
    usage: ContextWindowUsage


@dataclass(slots=True)
class _ActiveThreadLifecycle:
    binding_id: str
    thread_id: str | None
    state: ThreadLifecycleState


@dataclass(slots=True)
class _ThreadSubscription:
    binding_id: str
    thread_id: str
    thread: NativeThread | None
    last_activity: float
    generation: int = 0
    state: ThreadSubscriptionState = ThreadSubscriptionState.SUBSCRIBED
    release_deadline: float | None = None
    idle_task: asyncio.Task[None] | None = None


@dataclass(slots=True)
class _ActiveSideTurn:
    handle: NativeTurnHandle
    owner_id: str
    origin: object
    receipt_attempted: asyncio.Event
    state: ActiveState = ActiveState.RUNNING
    interrupt_attempted: bool = False
    interrupt_succeeded: bool = False
    cleanup_required: bool = False
    cleanup_succeeded: bool = False
    terminal_observed: bool = False
    cleanup_ready: asyncio.Event = field(default_factory=asyncio.Event)
    task_feedback: BindingTaskFeedback = BindingTaskFeedback()
    feedback_revision: int = 1
    activity_revision: int = 1
    activity_observation_enabled: bool = True
    steer_count: int = 0
    plan_cursor: int = 0
    plan_generated: bool = False
    plan_available: bool = True
    plan_may_be_stale: bool = False
    plan_stale_after_cursor: int | None = None
    plan_last_update_cursor: int | None = None
    plan_steps: tuple[TurnPlanStepSnapshot, ...] = ()
    activity_commentary: dict[str, str] = field(default_factory=dict)
    activity_commentary_order: list[str] = field(default_factory=list)
    activity_operations: dict[str, TurnActivityEvent] = field(default_factory=dict)
    activity_operation_order: list[str] = field(default_factory=list)
    task: asyncio.Task[None] | None = None


@dataclass(slots=True)
class _SideSession:
    side_id: str
    parent_binding_id: str
    parent_thread_id: str
    thread: NativeThread
    project_alias: str
    cwd: Path
    creator_id: str
    turn_settings: TurnModelSettings | None
    task_feedback: BindingTaskFeedback
    feedback_revision: int
    last_activity: float
    state: SideSessionState = SideSessionState.OPEN
    topic_id: str | None = None
    root_message_id: str | None = None
    active: _ActiveSideTurn | None = None
    idle_task: asyncio.Task[None] | None = None
    revision: int = 0
    terminal_cleanup_succeeded: bool = False
    turn_start_state_unknown: bool = False
    turn_terminal_state_unknown: bool = False
    close_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class CodexRuntime:
    """Own live native operations and apply Binding-scoped Turn intent."""

    def __init__(
        self,
        *,
        codex: NativeCodex,
        bindings: BindingStore,
        terminal_cleanup: TerminalCleanup,
        skill_catalog: SkillCatalog | None = None,
        goal_control: GoalControl | None = None,
        side_boundary_control: SideBoundaryControl | None = None,
        thread_subscription_control: ThreadSubscriptionControl | None = None,
        background_terminal_inspector: BackgroundTerminalInspector | None = None,
        thread_delete_control: ThreadDeleteControl | None = None,
        turn_plan_observer: TurnActivityObserver | None = None,
        on_completion: CompletionHandler = _ignore_completion,
        poll_interval_seconds: float = 0.5,
        compaction_timeout_seconds: float = _COMPACTION_TERMINAL_TIMEOUT_SECONDS,
        side_idle_seconds: float = _SIDE_IDLE_SECONDS,
        ordinary_thread_idle_seconds: float = _ORDINARY_THREAD_IDLE_SECONDS,
    ) -> None:
        if compaction_timeout_seconds <= 0:
            raise ValueError("compaction timeout must be positive")
        if side_idle_seconds <= 0:
            raise ValueError("Side idle timeout must be positive")
        if ordinary_thread_idle_seconds <= 0:
            raise ValueError("ordinary Thread idle timeout must be positive")
        self._codex = codex
        self._bindings = bindings
        self._terminal_cleanup = terminal_cleanup
        self._skill_catalog = skill_catalog
        self._goal_control = goal_control
        self._side_boundary_control = side_boundary_control
        self._thread_subscription_control = thread_subscription_control
        self._background_terminal_inspector = background_terminal_inspector
        self._thread_delete_control = thread_delete_control
        self._turn_plan_observer = turn_plan_observer
        self._on_completion = on_completion
        self._poll_interval_seconds = poll_interval_seconds
        self._compaction_timeout_seconds = compaction_timeout_seconds
        self._side_idle_seconds = side_idle_seconds
        self._ordinary_thread_idle_seconds = ordinary_thread_idle_seconds
        self._active: dict[str, _ActiveTurn] = {}
        self._compacting: dict[str, _ActiveCompaction] = {}
        self._goals: dict[str, _ActiveGoal] = {}
        self._context_window_usage: dict[str, _ContextWindowUsageSnapshot] = {}
        self._lifecycles: dict[str, _ActiveThreadLifecycle] = {}
        self._subscriptions: dict[str, _ThreadSubscription] = {}
        self._sides: dict[str, _SideSession] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._side_locks: dict[str, asyncio.Lock] = {}
        self._admission_revisions: dict[str, int] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._side_idle_tasks: set[asyncio.Task[None]] = set()
        self._subscription_idle_tasks: set[asyncio.Task[None]] = set()
        self._accepting = True

    def set_completion_handler(self, handler: CompletionHandler) -> None:
        self._on_completion = handler

    @property
    def available_capabilities(self) -> frozenset[NativeCapability]:
        capabilities: set[NativeCapability] = set()
        if self._skill_catalog is not None:
            capabilities.add(NativeCapability.SKILLS)
        if self._goal_control is not None:
            capabilities.add(NativeCapability.GOAL)
        if (
            self._side_boundary_control is not None
            and self._thread_subscription_control is not None
        ):
            capabilities.add(NativeCapability.SIDE)
        if (
            self._thread_subscription_control is not None
            and self._background_terminal_inspector is not None
        ):
            capabilities.add(NativeCapability.RELEASE)
        if self._thread_delete_control is not None:
            capabilities.add(NativeCapability.DELETE)
        return frozenset(capabilities)

    def thread_subscription_snapshot(
        self,
        binding_id: str,
    ) -> ThreadSubscriptionSnapshot | None:
        """Return this process's transient subscription state for one Binding."""

        record = self._subscriptions.get(binding_id)
        if record is None:
            return None
        release_in_seconds = None
        if record.release_deadline is not None:
            release_in_seconds = max(
                0.0,
                record.release_deadline - asyncio.get_running_loop().time(),
            )
        return ThreadSubscriptionSnapshot(
            binding_id=record.binding_id,
            thread_id=record.thread_id,
            state=record.state,
            release_in_seconds=release_in_seconds,
        )

    async def active_binding_changed(
        self,
        previous_binding_id: str | None,
        current_binding_id: str | None,
    ) -> None:
        """Re-arm transient release timers after a persisted pointer change."""

        if previous_binding_id == current_binding_id:
            return
        # The persisted pointer has already changed. Protect the new current
        # Binding first so an older inactive timer cannot release it while this
        # callback waits for an unrelated previous-Binding lock.
        if current_binding_id is not None:
            async with self._lock(current_binding_id):
                record = self._subscriptions.get(current_binding_id)
                if record is not None and record.state is not ThreadSubscriptionState.RELEASED:
                    self._schedule_subscription_release_locked(
                        record,
                        delay=self._ordinary_thread_idle_seconds,
                    )
        if previous_binding_id is not None:
            async with self._lock(previous_binding_id):
                record = self._subscriptions.get(previous_binding_id)
                if record is not None and record.state is not ThreadSubscriptionState.RELEASED:
                    self._schedule_subscription_release_locked(
                        record,
                        delay=0.0,
                    )

    async def binding_pointer_changed(
        self,
        previous_binding_id: str | None,
        current_binding_id: str | None,
    ) -> None:
        """Management-port name for subscription pointer reconciliation."""

        await self.active_binding_changed(previous_binding_id, current_binding_id)

    async def activate_exact(
        self,
        binding_id: str,
        *,
        context_anchor: MessageContextAnchor | None = None,
    ) -> ThreadBinding:
        """Select an exact Binding while serializing transient state.

        The caller owns the Scope lock. Taking the target Binding lock here
        prevents an already-scheduled inactive-subscription release from
        racing the persisted pointer commit.
        """

        if not self._accepting:
            raise RuntimeClosed("服务正在停止，暂不能切换会话。")
        async with self._lock(binding_id):
            if not self._accepting:
                raise RuntimeClosed("服务正在停止，暂不能切换会话。")
            self._guard_no_lifecycle_locked(binding_id)
            binding = self._bindings.get(binding_id)
            if (
                binding.message_context_mode is MentionContextMode.CATCH_UP
                and context_anchor is None
            ):
                raise ContextAnchorRequired(
                    "该会话启用了 @ 时补充上下文；请从飞书 /resume 切换，"
                    "以建立新的消息边界。"
                )
            if (
                binding.message_context_mode is MentionContextMode.CURRENT_ONLY
                and context_anchor is not None
            ):
                raise ValueError("current-only activation cannot carry an anchor")
            if binding.native_thread_id is not None:
                state = await self.thread_catalog_state(binding.native_thread_id)
                if state is NativeThreadCatalogState.ARCHIVED:
                    raise ThreadArchived("归档会话必须先恢复，不能直接设为当前。")
                if state is NativeThreadCatalogState.MISSING:
                    raise ThreadCatalogIdentityMissing(
                        "native Thread is absent from active and archived catalogs"
                    )
            activated = self._bindings.activate(
                scope_key=binding.scope_key,
                binding_id=binding.id,
                context_anchor=context_anchor,
            )
            self._advance_admission_revision(binding.id)
            record = self._subscriptions.get(binding.id)
            if (
                record is not None
                and record.state is not ThreadSubscriptionState.RELEASED
            ):
                self._schedule_subscription_release_locked(
                    record,
                    delay=self._ordinary_thread_idle_seconds,
                )
            return activated

    async def release_binding(
        self,
        binding: ThreadBinding,
    ) -> ReleaseDisposition:
        """Compatibility wrapper for the exact Binding release primitive."""

        return await self.release_exact(binding.id)

    async def release_exact(
        self,
        binding_id: str,
    ) -> ReleaseDisposition:
        """Release only this process's subscription to one exact Binding."""

        if not self._accepting:
            raise RuntimeClosed("服务正在停止，暂不能释放会话订阅。")
        if (
            self._thread_subscription_control is None
            or self._background_terminal_inspector is None
        ):
            raise ThreadReleaseError("当前 Thread 订阅释放能力不可用。")
        async with self._lock(binding_id):
            if not self._accepting:
                raise RuntimeClosed("服务正在停止，暂不能释放会话订阅。")
            self._guard_no_lifecycle_locked(binding_id)
            binding = self._bindings.get(binding_id)
            if binding.native_thread_id is None:
                return ReleaseDisposition.NOT_MATERIALIZED
            if binding.id in self._compacting:
                raise ThreadCompacting("当前会话正在压缩上下文，不能释放订阅。")
            if binding.id in self._goals:
                raise self._goal_slot_error(self._goals[binding.id])
            active = self._active.get(binding.id)
            if active is not None:
                if active.state is ActiveState.STOPPING:
                    raise ThreadStopping("当前 Turn 正在停止，不能释放订阅。")
                if active.state is ActiveState.OBSERVATION_UNAVAILABLE:
                    raise TurnObservationUnavailable(
                        "当前 Turn 观测不可用，不能释放订阅；请重新检查或停止。"
                    )
                raise ThreadRunningConfiguration(
                    "当前 Turn 正在执行，不能释放订阅。"
                )
            await self._guard_no_goal_locked(binding)
            if binding.id in self._goals:
                raise self._goal_slot_error(self._goals[binding.id])
            record = self._subscriptions.get(binding.id)
            if record is None or record.state is ThreadSubscriptionState.RELEASED:
                return ReleaseDisposition.NOT_SUBSCRIBED
            if record.thread_id != binding.native_thread_id:
                self.close_admission()
                raise ThreadReleaseStateUnknown(
                    "本进程记录的 Thread 订阅与当前 Binding 不一致；"
                    "服务已停止接收新任务。"
                )
            self._cancel_subscription_idle(record)
            record.generation += 1
            return await self._attempt_subscription_release_locked(
                binding,
                record,
                explicit=True,
            )

    def _mark_thread_subscribed_locked(
        self,
        binding: ThreadBinding,
        thread: NativeThread,
    ) -> _ThreadSubscription:
        thread_id = binding.native_thread_id or thread.id
        if thread.id != thread_id:
            self.close_admission()
            raise RuntimeError(
                "native Thread subscription identity mismatch; admission closed"
            )
        record = self._subscriptions.get(binding.id)
        if record is not None and record.thread_id != thread_id:
            self.close_admission()
            raise RuntimeError(
                "Binding subscription changed native Thread identity; admission closed"
            )
        now = asyncio.get_running_loop().time()
        if record is None:
            record = _ThreadSubscription(
                binding_id=binding.id,
                thread_id=thread_id,
                thread=thread,
                last_activity=now,
            )
            self._subscriptions[binding.id] = record
        else:
            self._cancel_subscription_idle(record)
            record.thread = thread
            record.last_activity = now
            record.generation += 1
            record.state = ThreadSubscriptionState.SUBSCRIBED
            record.release_deadline = None
        return record

    def _cancel_subscription_idle(self, record: _ThreadSubscription) -> None:
        task = record.idle_task
        record.idle_task = None
        record.release_deadline = None
        if (
            task is not None
            and task is not asyncio.current_task()
            and not task.done()
        ):
            task.cancel()

    def _subscription_busy_locked(self, binding_id: str) -> bool:
        return (
            binding_id in self._active
            or binding_id in self._compacting
            or binding_id in self._goals
            or binding_id in self._lifecycles
        )

    def _schedule_known_subscription_locked(
        self,
        binding_id: str,
        thread_id: str,
        *,
        delay: float | None = None,
    ) -> None:
        record = self._subscriptions.get(binding_id)
        if record is None:
            return
        if record.thread_id != thread_id:
            self.close_admission()
            raise RuntimeError(
                "native Thread subscription identity changed; admission closed"
            )
        record.last_activity = asyncio.get_running_loop().time()
        self._schedule_subscription_release_locked(record, delay=delay)

    def _schedule_subscription_release_locked(
        self,
        record: _ThreadSubscription,
        *,
        delay: float | None = None,
        state: ThreadSubscriptionState | None = None,
    ) -> None:
        if (
            not self._accepting
            or self._thread_subscription_control is None
            or self._background_terminal_inspector is None
            or record.thread is None
            or record.state is ThreadSubscriptionState.RELEASED
            or self._subscription_busy_locked(record.binding_id)
        ):
            return
        binding = self._bindings.get(record.binding_id)
        if delay is None:
            delay = self._ordinary_thread_idle_seconds if binding.active else 0.0
        if state is None:
            state = (
                ThreadSubscriptionState.RELEASE_UNKNOWN
                if record.state
                in {
                    ThreadSubscriptionState.RELEASING,
                    ThreadSubscriptionState.RELEASE_UNKNOWN,
                }
                else ThreadSubscriptionState.RELEASE_PENDING
            )
        self._cancel_subscription_idle(record)
        record.generation += 1
        generation = record.generation
        record.state = state
        record.release_deadline = asyncio.get_running_loop().time() + delay
        task = asyncio.create_task(
            self._release_subscription_after_delay(
                record,
                generation,
                delay,
                binding.active,
            ),
            name=f"codex-thread-release:{record.thread_id}",
        )
        record.idle_task = task
        self._subscription_idle_tasks.add(task)
        task.add_done_callback(self._subscription_idle_tasks.discard)

    async def _release_subscription_after_delay(
        self,
        record: _ThreadSubscription,
        generation: int,
        delay: float,
        scheduled_active: bool,
    ) -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            async with self._lock(record.binding_id):
                if (
                    not self._accepting
                    or self._subscriptions.get(record.binding_id) is not record
                    or record.generation != generation
                    or record.state
                    not in {
                        ThreadSubscriptionState.RELEASE_PENDING,
                        ThreadSubscriptionState.RELEASE_UNKNOWN,
                    }
                ):
                    return
                record.idle_task = None
                record.release_deadline = None
                if self._subscription_busy_locked(record.binding_id):
                    if record.state is not ThreadSubscriptionState.RELEASE_UNKNOWN:
                        record.state = ThreadSubscriptionState.SUBSCRIBED
                    return
                binding = self._bindings.get(record.binding_id)
                if binding.active and not scheduled_active:
                    self._schedule_subscription_release_locked(
                        record,
                        delay=self._ordinary_thread_idle_seconds,
                    )
                    return
                await self._attempt_subscription_release_locked(
                    binding,
                    record,
                    explicit=False,
                )
        except asyncio.CancelledError:
            return
        except Exception:
            logger.warning(
                "idle Thread subscription release failed; deferred",
                exc_info=True,
                extra={
                    "binding_id": record.binding_id,
                    "thread_id": record.thread_id,
                },
            )
            async with self._lock(record.binding_id):
                if (
                    self._accepting
                    and self._subscriptions.get(record.binding_id) is record
                    and record.generation == generation
                ):
                    self._schedule_subscription_release_locked(
                        record,
                        delay=self._ordinary_thread_idle_seconds,
                    )

    async def _attempt_subscription_release_locked(
        self,
        binding: ThreadBinding,
        record: _ThreadSubscription,
        *,
        explicit: bool,
    ) -> ReleaseDisposition:
        control = self._thread_subscription_control
        inspector = self._background_terminal_inspector
        thread = record.thread
        assert control is not None and inspector is not None and thread is not None
        if binding.native_thread_id != record.thread_id:
            self.close_admission()
            raise ThreadReleaseStateUnknown(
                "Thread 订阅身份无法确认；服务已停止接收新任务。"
            )
        try:
            response = await thread.read(include_turns=False)
            native = getattr(response, "thread", None)
            if getattr(native, "id", None) != record.thread_id:
                raise RuntimeError("Thread read identity mismatch before release")
            status = _thread_status_type(native)
        except asyncio.CancelledError:
            self._schedule_subscription_release_locked(
                record,
                delay=self._ordinary_thread_idle_seconds,
            )
            raise
        except Exception as error:
            if explicit:
                self._schedule_subscription_release_locked(
                    record,
                    delay=self._ordinary_thread_idle_seconds,
                )
                raise ThreadReleaseError(
                    "无法确认原生 Thread 已空闲，本次未释放订阅。"
                ) from error
            self._schedule_subscription_release_locked(
                record,
                delay=self._ordinary_thread_idle_seconds,
            )
            return ReleaseDisposition.NOT_SUBSCRIBED

        if status != "idle" or getattr(native, "ephemeral", None) is not False:
            self._schedule_subscription_release_locked(
                record,
                delay=self._ordinary_thread_idle_seconds,
            )
            if explicit:
                raise ThreadReleaseError(
                    f"原生 Thread 状态为 {status!r}，本次未释放订阅。"
                )
            return ReleaseDisposition.NOT_SUBSCRIBED

        try:
            has_running = await inspector.has_running(record.thread_id)
        except asyncio.CancelledError:
            self._schedule_subscription_release_locked(
                record,
                delay=self._ordinary_thread_idle_seconds,
            )
            raise
        except Exception as error:
            self._schedule_subscription_release_locked(
                record,
                delay=self._ordinary_thread_idle_seconds,
            )
            if explicit:
                raise ThreadReleaseError(
                    "无法确认后台终端状态，本次未释放订阅。"
                ) from error
            return ReleaseDisposition.NOT_SUBSCRIBED
        if has_running:
            self._schedule_subscription_release_locked(
                record,
                delay=self._ordinary_thread_idle_seconds,
            )
            if explicit:
                raise ThreadBackgroundTerminalsActive(
                    "当前 Thread 仍有已登记后台终端，本次未释放订阅。"
                )
            return ReleaseDisposition.NOT_SUBSCRIBED

        record.state = ThreadSubscriptionState.RELEASING
        try:
            status = await control.unsubscribe(record.thread_id)
        except asyncio.CancelledError:
            record.state = ThreadSubscriptionState.RELEASE_UNKNOWN
            self._schedule_subscription_release_locked(
                record,
                delay=self._ordinary_thread_idle_seconds,
                state=ThreadSubscriptionState.RELEASE_UNKNOWN,
            )
            raise
        except ThreadUnsubscribeStateUnknown as error:
            self._schedule_subscription_release_locked(
                record,
                delay=self._ordinary_thread_idle_seconds,
                state=ThreadSubscriptionState.RELEASE_UNKNOWN,
            )
            if explicit:
                raise ThreadReleaseStateUnknown(
                    "Thread 取消订阅结果未确认；不会立即重试。"
                ) from error
            return ReleaseDisposition.NOT_SUBSCRIBED
        self._mark_subscription_released_locked(record)
        if status is ThreadUnsubscribeStatus.UNSUBSCRIBED:
            return ReleaseDisposition.RELEASED
        return ReleaseDisposition.NOT_SUBSCRIBED

    def _mark_subscription_released_locked(
        self,
        record: _ThreadSubscription,
    ) -> None:
        self._cancel_subscription_idle(record)
        record.generation += 1
        record.thread = None
        record.last_activity = asyncio.get_running_loop().time()
        record.state = ThreadSubscriptionState.RELEASED

    async def create_side(
        self,
        *,
        side_id: str,
        binding: ThreadBinding,
        cwd: Path,
        creator_id: str,
    ) -> SideSessionSnapshot:
        control = self._side_boundary_control
        subscription_control = self._thread_subscription_control
        if control is None or subscription_control is None:
            raise SideUnavailable(
                "当前 SDK/App Server 的 Side Thread 兼容契约未通过。"
            )
        if not side_id or not creator_id:
            raise ValueError("Side identity must not be empty")
        if not self._accepting:
            raise RuntimeClosed("服务正在停止，暂不能创建 Side。")
        if side_id in self._sides:
            return self.side_snapshot(side_id)

        prepared = self._bindings.get(binding.id)
        if not prepared.active:
            raise SteerRace("当前会话已切换，本次 Side 未创建。")
        if prepared.native_thread_id is None:
            raise ThreadNotMaterialized(
                "当前会话尚未创建原生 Codex Thread；请先发送一条真实任务，"
                "再使用 /side。"
            )
        prepared_revision = prepared.settings_revision
        prepared_feedback_revision = prepared.feedback_revision
        prepared_feedback = prepared.task_feedback
        configured = prepared.turn_settings
        resolved_settings = None
        if configured is not None:
            resolved_settings = await self.resolve_model_settings(
                model_id=configured.model_id,
                effort_id=configured.effort_id,
                service_tier_id=configured.service_tier_id,
            )

        async with self._lock(binding.id):
            if not self._accepting:
                raise RuntimeClosed("服务正在停止，暂不能创建 Side。")
            self._guard_no_lifecycle_locked(binding.id)
            if binding.id in self._compacting:
                raise ThreadCompacting(
                    "当前会话正在压缩上下文，完成前不能创建 Side。"
                )
            current = self._bindings.get(binding.id)
            if not current.active:
                raise SteerRace("当前会话已切换，本次 Side 未创建。")
            if current.native_thread_id is None:
                raise ThreadNotMaterialized(
                    "当前会话尚未创建原生 Codex Thread；请先发送一条真实任务，"
                    "再使用 /side。"
                )
            if current.settings_revision != prepared_revision:
                raise SteerRace("创建 Side 期间会话配置已变化，请重新发送 /side。")
            if current.feedback_revision != prepared_feedback_revision:
                raise SteerRace(
                    "创建 Side 期间任务反馈配置已变化，请重新发送 /side。"
                )
            existing = self._sides.get(side_id)
            if existing is not None:
                return self._side_snapshot(existing)
            await self._guard_no_goal_locked(current)
            current = self._bindings.get(binding.id)
            if not current.active or current.native_thread_id is None:
                raise SteerRace("创建 Side 期间当前会话已变化，请重新发送 /side。")
            if current.settings_revision != prepared_revision:
                raise SteerRace("创建 Side 期间会话配置已变化，请重新发送 /side。")
            if current.feedback_revision != prepared_feedback_revision:
                raise SteerRace(
                    "创建 Side 期间任务反馈配置已变化，请重新发送 /side。"
                )
            parent_active = self._active.get(binding.id)
            if (
                parent_active is not None
                and parent_active.state is ActiveState.STOPPING
            ):
                raise ThreadStopping(
                    "当前父 Turn 正在停止或清理，完成前不能创建 Side。"
                )
            if parent_active is None:
                try:
                    parent_thread = await self._codex.thread_resume(
                        current.native_thread_id
                    )
                    if parent_thread.id != current.native_thread_id:
                        self.close_admission()
                        raise RuntimeError(
                            "thread_resume returned a different parent Thread ID"
                        )
                    record = self._mark_thread_subscribed_locked(
                        current,
                        parent_thread,
                    )
                    self._schedule_subscription_release_locked(record)
                    parent_view = await parent_thread.read(include_turns=False)
                    native_parent = getattr(parent_view, "thread", None)
                    if getattr(native_parent, "id", None) != current.native_thread_id:
                        raise RuntimeError("parent Thread read identity mismatch")
                    parent_status = _thread_status_type(native_parent)
                    if parent_status != "idle":
                        if parent_status == "active":
                            raise ThreadRunningConfiguration(
                                "父 Codex Thread 正由外部客户端执行未知 Turn，"
                                "不能创建 Side。"
                            )
                        raise RuntimeError(
                            f"unexpected parent Thread status: {parent_status!r}"
                        )
                    if getattr(native_parent, "ephemeral", None) is not False:
                        raise RuntimeError("parent Thread persistence shape changed")
                except ThreadRunningConfiguration:
                    raise
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self._schedule_known_subscription_locked(
                        current.id,
                        current.native_thread_id,
                    )
                    raise SideStartFailed(
                        "无法确认父 Codex Thread 处于可 fork 状态，本次 Side 未创建。"
                    ) from error
            try:
                thread = await self._codex.thread_fork(
                    current.native_thread_id,
                    ephemeral=True,
                )
            except asyncio.CancelledError:
                self.close_admission()
                raise
            except Exception as error:
                self.close_admission()
                raise SideStartFailed(
                    "Codex Side Thread 创建结果未确认；服务已停止接收新任务，"
                    "请重启服务。"
                ) from error
            if (
                not isinstance(thread.id, str)
                or not thread.id
                or thread.id == current.native_thread_id
            ):
                self.close_admission()
                raise SideStartFailed(
                    "Codex 返回了无效的 Side Thread 标识；"
                    "服务已停止接收新任务，请重启服务。"
                )
            try:
                fork_view = await thread.read(include_turns=False)
                native_fork = getattr(fork_view, "thread", None)
                if getattr(native_fork, "id", None) != thread.id:
                    raise RuntimeError("forked Side Thread read identity mismatch")
                if getattr(native_fork, "ephemeral", None) is not True:
                    raise RuntimeError("forked Side Thread is not ephemeral")
                forked_from_id = getattr(native_fork, "forked_from_id", None)
                if forked_from_id is not None and forked_from_id != current.native_thread_id:
                    raise RuntimeError("forked Side Thread parent identity mismatch")
                await control.inject_boundary(thread.id)
            except BaseException as error:
                try:
                    await subscription_control.unsubscribe(thread.id)
                except BaseException as unsubscribe_error:
                    self.close_admission()
                    session = _SideSession(
                        side_id=side_id,
                        parent_binding_id=current.id,
                        parent_thread_id=current.native_thread_id,
                        thread=thread,
                        project_alias=current.project_alias,
                        cwd=cwd.resolve(),
                        creator_id=creator_id,
                        turn_settings=resolved_settings,
                        task_feedback=prepared_feedback,
                        feedback_revision=prepared_feedback_revision,
                        last_activity=asyncio.get_running_loop().time(),
                        state=SideSessionState.CLOSING,
                    )
                    self._sides[side_id] = session
                    combined = BaseExceptionGroup(
                        "Side boundary and compensation state are unknown",
                        [error, unsubscribe_error],
                    )
                    if isinstance(unsubscribe_error, asyncio.CancelledError):
                        raise unsubscribe_error
                    if isinstance(error, asyncio.CancelledError):
                        raise error
                    raise SideStartFailed(
                        "Side 边界注入与取消订阅结果均未确认；"
                        "服务已停止接收新任务，请重启服务。"
                    ) from combined
                if isinstance(error, asyncio.CancelledError):
                    raise
                raise SideStartFailed(
                    "Side 边界注入结果未确认；已请求取消订阅，飞书话题未创建。"
                ) from error

            session = _SideSession(
                side_id=side_id,
                parent_binding_id=current.id,
                parent_thread_id=current.native_thread_id,
                thread=thread,
                project_alias=current.project_alias,
                cwd=cwd.resolve(),
                creator_id=creator_id,
                turn_settings=resolved_settings,
                task_feedback=prepared_feedback,
                feedback_revision=prepared_feedback_revision,
                last_activity=asyncio.get_running_loop().time(),
            )
            self._sides[side_id] = session
            return self._side_snapshot(session)

    async def attach_side_topic(
        self,
        *,
        side_id: str,
        topic_id: str,
        root_message_id: str,
    ) -> SideSessionSnapshot:
        if not topic_id or not root_message_id:
            raise ValueError("Side Topic identity must not be empty")
        async with self._side_lock(side_id):
            session = self._require_side(side_id)
            if session.state is SideSessionState.CLOSING:
                raise SideSessionClosing("Side 正在结束。")
            record = self._bindings.get_side_topic(side_id)
            creating_match = (
                record.state is SideTopicState.CREATING
                and record.topic_id in {None, topic_id}
                and record.root_message_id == root_message_id
            )
            open_match = (
                record.state is SideTopicState.OPEN
                and record.topic_id == topic_id
                and record.root_message_id == root_message_id
            )
            if not creating_match and not open_match:
                raise SideSessionConflict("Side Runtime 与飞书话题记录不一致。")
            if session.topic_id is not None and (
                session.topic_id != topic_id
                or session.root_message_id != root_message_id
            ):
                raise SideSessionConflict("Side Topic identity is write-once")
            session.topic_id = topic_id
            session.root_message_id = root_message_id
            session.last_activity = asyncio.get_running_loop().time()
            self._schedule_side_idle(session)
            return self._side_snapshot(session)

    def side_snapshot(self, side_id: str) -> SideSessionSnapshot:
        return self._side_snapshot(self._require_side(side_id))

    async def capture_side_submission_admission(
        self,
        side_id: str,
    ) -> SideSubmissionAdmission:
        if not self._accepting:
            raise RuntimeClosed("服务正在停止，暂不接受 Side 消息。")
        async with self._side_lock(side_id):
            session = self._require_side(side_id)
            if not self._accepting:
                raise RuntimeClosed("服务正在停止，暂不接受 Side 消息。")
            if session.state is SideSessionState.CLOSING:
                raise SideSessionClosing("Side 正在结束，暂不接受新消息。")
            if session.topic_id is None:
                raise SideSessionConflict("Side 话题尚未准备完成。")
            active = session.active
            if active is not None and active.state is ActiveState.STOPPING:
                raise SideSessionClosing(
                    "当前 Side Turn 正在停止，暂不接受新消息。"
                )
            return SideSubmissionAdmission(
                side_id=side_id,
                revision=session.revision,
                thread_id=session.thread.id,
                turn_id=active.handle.id if active is not None else None,
            )

    async def submit_side(
        self,
        *,
        side_id: str,
        input: Any,
        owner_id: str,
        origin: object,
        admission: SideSubmissionAdmission | None = None,
        skill_names: tuple[str, ...] = (),
    ) -> SideSubmission:
        if not self._accepting:
            raise RuntimeClosed("服务正在停止，暂不接受 Side 消息。")
        if admission is not None and admission.side_id != side_id:
            raise ValueError("Side submission admission belongs to another Side")
        if admission is None:
            admission = await self.capture_side_submission_admission(side_id)
        session = self._require_side(side_id)
        native_input = input
        if skill_names:
            native_input = await self._compile_skill_input(
                cwd=session.cwd,
                text=input,
                names=skill_names,
            )

        start_error: BaseException | None = None
        started: SideSubmission | None = None
        async with self._side_lock(side_id):
            session = self._require_side(side_id)
            if not self._accepting:
                raise RuntimeClosed("服务正在停止，暂不接受 Side 消息。")
            if session.state is SideSessionState.CLOSING:
                raise SideSessionClosing("Side 正在结束，暂不接受新消息。")
            if session.topic_id is None or session.root_message_id is None:
                raise SideSessionConflict("Side 话题尚未准备完成。")
            active = session.active
            self._redeem_side_admission(session, active, admission)
            session.revision += 1
            if active is not None:
                if active.state is ActiveState.STOPPING:
                    raise SideSessionClosing(
                        "当前 Side Turn 正在停止，暂不接受新消息。"
                    )
                if active.task_feedback.progress_card_enabled:
                    self._refresh_turn_activity(active)
                stale_after_cursor = active.plan_cursor
                try:
                    await active.handle.steer(native_input)
                except asyncio.CancelledError:
                    session.state = SideSessionState.CLOSING
                    session.revision += 1
                    self._cancel_side_idle(session)
                    self.close_admission()
                    raise
                except InvalidRequestError as error:
                    raise SteerRace(
                        "当前 Side Turn 恰好已经结束，本条消息未执行，请重新发送。"
                    ) from error
                except Exception as error:
                    session.state = SideSessionState.CLOSING
                    session.revision += 1
                    self._cancel_side_idle(session)
                    self.close_admission()
                    raise SideStartFailed(
                        "Codex Side steer 结果未确认；服务已停止接收新任务，"
                        "请重启服务。"
                    ) from error
                active.steer_count += 1
                last_update = active.plan_last_update_cursor
                if last_update is not None and last_update > stale_after_cursor:
                    active.plan_may_be_stale = False
                    active.plan_stale_after_cursor = None
                else:
                    active.plan_may_be_stale = True
                    active.plan_stale_after_cursor = stale_after_cursor
                active.activity_revision += 1
                session.last_activity = asyncio.get_running_loop().time()
                self._touch_side_topic(side_id)
                return SideSubmission(
                    SubmitDisposition.STEERED,
                    side_id,
                    active.handle.thread_id,
                    active.handle.id,
                    task_feedback=active.task_feedback,
                    feedback_revision=active.feedback_revision,
                )

            turn_kwargs: dict[str, object] = {}
            if session.turn_settings is not None:
                turn_kwargs = {
                    "model": session.turn_settings.model,
                    "effort": session.turn_settings.effort,
                    "service_tier": session.turn_settings.service_tier_id,
                }
            try:
                handle = await session.thread.turn(native_input, **turn_kwargs)
            except BaseException as error:
                session.state = SideSessionState.CLOSING
                session.turn_start_state_unknown = True
                session.revision += 1
                self._cancel_side_idle(session)
                self.close_admission()
                start_error = error
            else:
                if handle.thread_id != session.thread.id:
                    session.state = SideSessionState.CLOSING
                    session.turn_start_state_unknown = True
                    session.revision += 1
                    self._cancel_side_idle(session)
                    self.close_admission()
                    start_error = RuntimeError(
                        "Side Turn handle returned a different Thread ID"
                    )
                else:
                    receipt_attempted = asyncio.Event()
                    session.terminal_cleanup_succeeded = False
                    active = _ActiveSideTurn(
                        handle=handle,
                        owner_id=owner_id,
                        origin=origin,
                        receipt_attempted=receipt_attempted,
                        task_feedback=session.task_feedback,
                        feedback_revision=session.feedback_revision,
                        plan_available=self._turn_plan_observer is not None,
                    )
                    session.active = active
                    session.last_activity = asyncio.get_running_loop().time()
                    self._cancel_side_idle(session)
                    self._touch_side_topic(side_id)
                    self._track_side(session, active)
                    started = SideSubmission(
                        SubmitDisposition.STARTED,
                        side_id,
                        session.thread.id,
                        handle.id,
                        receipt_attempted.set,
                        task_feedback=active.task_feedback,
                        feedback_revision=active.feedback_revision,
                    )

        if start_error is not None:
            if isinstance(start_error, asyncio.CancelledError):
                raise start_error
            raise SideStartFailed(
                "Codex Side Turn 启动结果未确认；服务已停止接收新任务，"
                "且不会清理或退订这个 Side。请重启服务。"
            ) from start_error
        assert started is not None
        return started

    async def stop_side(
        self,
        side_id: str,
        *,
        acknowledge: StopAcknowledger | None = None,
    ) -> StopDisposition:
        async with self._side_lock(side_id):
            session = self._require_side(side_id)
            if session.state is SideSessionState.CLOSING:
                return StopDisposition.STOPPING
            active = session.active
            if active is None or (
                active.terminal_observed and active.state is ActiveState.RUNNING
            ):
                return StopDisposition.NOT_RUNNING
            if active.state is ActiveState.RUNNING:
                active.state = ActiveState.STOPPING
                active.activity_revision += 1
                session.revision += 1
            if acknowledge is not None:
                try:
                    async with asyncio.timeout(_STOP_ACK_ATTEMPT_TIMEOUT_SECONDS):
                        await acknowledge()
                except TimeoutError:
                    logger.warning(
                        "Side stop acknowledgement timed out",
                        extra={"side_id": side_id, "turn_id": active.handle.id},
                    )
                except Exception:
                    logger.warning(
                        "Side stop acknowledgement failed",
                        exc_info=True,
                        extra={"side_id": side_id, "turn_id": active.handle.id},
                    )
            return await self._interrupt_side_turn(session, active)

    async def close_side(
        self,
        side_id: str,
        *,
        state: SideTopicState = SideTopicState.CLOSED,
    ) -> SideLifecycleOutcome:
        if state not in {
            SideTopicState.CLOSED,
            SideTopicState.EXPIRED,
            SideTopicState.FAILED,
        }:
            raise ValueError("Side close state must be terminal")
        session = self._require_side(side_id)
        async with session.close_lock:
            session = self._require_side(side_id)
            active: _ActiveSideTurn | None
            async with self._side_lock(side_id):
                session = self._require_side(side_id)
                session.state = SideSessionState.CLOSING
                session.revision += 1
                self._cancel_side_idle(session)
                active = session.active
                if active is not None:
                    if active.state is ActiveState.RUNNING:
                        active.state = ActiveState.STOPPING
                        active.activity_revision += 1
                    active.receipt_attempted.set()
                    if active.cleanup_required:
                        active.cleanup_ready.set()

            if session.turn_start_state_unknown:
                error = SideCloseFailed(
                    "Side Turn 启动结果未知，不能安全确认中断；"
                    "不会清理、退订或报告结束，请重启服务。"
                )
                await self._deliver_side_lifecycle(
                    SideLifecycleOutcome(
                        side_id=side_id,
                        state=SideTopicState.OPEN,
                        error=error,
                    )
                )
                raise error
            if session.turn_terminal_state_unknown:
                error = SideCloseFailed(
                    "Side Turn 终态未知，不能安全确认中断；"
                    "不会清理、退订或报告结束，请重启服务。"
                )
                await self._deliver_side_lifecycle(
                    SideLifecycleOutcome(
                        side_id=side_id,
                        state=SideTopicState.OPEN,
                        error=error,
                    )
                )
                raise error

            errors: list[BaseException] = []
            if active is not None and not active.terminal_observed:
                try:
                    await self._request_side_interrupt_for_close(session, active)
                except asyncio.CancelledError:
                    raise
                except BaseException as error:
                    errors.append(error)

            active_task = active.task if active is not None else None
            if (
                not errors
                and active_task is not None
                and active_task is not asyncio.current_task()
            ):
                try:
                    await asyncio.wait_for(
                        asyncio.shield(active_task),
                        timeout=_SIDE_CLOSE_DRAIN_TIMEOUT_SECONDS,
                    )
                except TimeoutError as error:
                    logger.warning(
                        "Side Turn did not drain before unsubscribe",
                        extra={"side_id": side_id, "thread_id": session.thread.id},
                    )
                    errors.append(error)
                except asyncio.CancelledError:
                    raise
                except BaseException as error:
                    errors.append(error)

            if (
                not errors
                and active is not None
                and not active.terminal_observed
            ):
                errors.append(
                    SideCloseFailed(
                        "Side Turn 终态未确认，不能清理或取消订阅。"
                    )
                )

            if errors:
                grouped = BaseExceptionGroup("Side close failed", errors)
                await self._deliver_side_lifecycle(
                    SideLifecycleOutcome(
                        side_id=side_id,
                        state=SideTopicState.OPEN,
                        error=grouped,
                    )
                )
                raise SideCloseFailed(
                    "Side 正在关闭，但原生中断或终态未确认；"
                    "不会清理或退订，请再次结束 Side。"
                ) from errors[0]

            outcome = await self._finalize_side(
                session,
                state=state,
                error=None,
            )
            if outcome.error is not None:
                raise SideCloseFailed(
                    "Side 已停止接收消息，但原生清理未全部确认。"
                ) from outcome.error
            return outcome

    async def close_side_exact(
        self,
        side_id: str,
        *,
        state: SideTopicState = SideTopicState.CLOSED,
    ) -> SideLifecycleOutcome:
        """Management-port name for the exact Side close primitive."""

        return await self.close_side(side_id, state=state)

    async def model_catalog(self) -> ModelCatalog:
        """Read the live native catalog; Netizen never caches model options."""

        if not self._accepting:
            raise RuntimeClosed("服务正在停止，暂不能读取 Codex 模型目录。")
        response = await self._codex.models()
        return ModelCatalog.from_response(response)

    async def thread_metadata(
        self,
        thread_ids: tuple[str, ...],
        *,
        archived: bool = False,
        deadline: float | None = None,
        max_pages: int | None = None,
        max_items: int | None = None,
        use_state_db_only: bool | None = None,
    ) -> dict[str, NativeThreadMetadata]:
        """Read native titles without loading, resuming, or mutating Threads."""

        remaining = set(thread_ids)
        if any(
            not isinstance(thread_id, str) or not thread_id
            for thread_id in remaining
        ):
            raise ValueError("native Thread IDs must be non-empty strings")
        if not remaining:
            return {}
        if max_pages is not None and max_pages < 1:
            raise ValueError("native metadata page limit must be positive")
        if max_items is not None and max_items < 1:
            raise ValueError("native metadata item limit must be positive")
        if use_state_db_only is not None and not isinstance(
            use_state_db_only,
            bool,
        ):
            raise ValueError("use_state_db_only must be boolean or None")
        if deadline is not None and deadline <= asyncio.get_running_loop().time():
            raise ThreadCatalogDeadlineExceeded(
                "native Thread metadata deadline elapsed"
            )

        found: dict[str, NativeThreadMetadata] = {}
        cursor: str | None = None
        seen_cursors: set[str] = set()
        page_count = 0
        item_count = 0
        try:
            async with asyncio.timeout_at(deadline):
                while remaining:
                    if max_pages is not None and page_count >= max_pages:
                        raise ThreadCatalogLimitExceeded(
                            "native Thread metadata exceeded the page limit"
                        )
                    list_kwargs: dict[str, object] = {
                        "archived": archived,
                        "cursor": cursor,
                        "limit": _THREAD_LIST_PAGE_LIMIT,
                        # Bindings remain valid across native model-provider changes.
                        # Use the SDK's explicit all-provider form so older Threads
                        # stay visible in both active and archived catalogs.
                        "model_providers": [],
                    }
                    if use_state_db_only is not None:
                        list_kwargs["use_state_db_only"] = use_state_db_only
                    response = await self._codex.thread_list(**list_kwargs)
                    page_count += 1
                    data = getattr(response, "data", None)
                    if not isinstance(data, list):
                        raise RuntimeError("thread_list returned an invalid data page")

                    for thread in data:
                        if max_items is not None and item_count >= max_items:
                            raise ThreadCatalogLimitExceeded(
                                "native Thread metadata exceeded the item limit"
                            )
                        item_count += 1
                        thread_id = getattr(thread, "id", None)
                        if thread_id not in remaining:
                            continue
                        name = getattr(thread, "name", None)
                        preview = getattr(thread, "preview", None)
                        if name is not None and not isinstance(name, str):
                            raise RuntimeError(
                                "thread_list returned an invalid Thread name"
                            )
                        if not isinstance(preview, str):
                            raise RuntimeError(
                                "thread_list returned an invalid Thread preview"
                            )
                        found[thread_id] = NativeThreadMetadata(
                            thread_id=thread_id,
                            name=name,
                            preview=preview,
                        )
                        remaining.remove(thread_id)

                    if not remaining:
                        break
                    next_cursor = getattr(response, "next_cursor", None)
                    if next_cursor is None:
                        break
                    if (
                        not isinstance(next_cursor, str)
                        or not next_cursor
                        or next_cursor in seen_cursors
                    ):
                        raise RuntimeError(
                            "thread_list returned an invalid pagination cursor"
                        )
                    seen_cursors.add(next_cursor)
                    cursor = next_cursor
        except TimeoutError as error:
            raise ThreadCatalogDeadlineExceeded(
                "native Thread metadata deadline elapsed"
            ) from error

        return found

    async def thread_catalog(
        self,
        *,
        archived: bool,
        deadline: float,
        max_pages: int = _THREAD_CATALOG_MAX_PAGES,
        max_items: int = _THREAD_CATALOG_MAX_ITEMS,
    ) -> NativeThreadCatalog:
        """Read one complete bounded native catalog for a request-scoped view."""

        if not isinstance(archived, bool):
            raise ValueError("archived must be boolean")
        if max_pages < 1 or max_items < 1:
            raise ValueError("native catalog limits must be positive")
        loop = asyncio.get_running_loop()
        if deadline <= loop.time():
            raise ThreadCatalogDeadlineExceeded("native Thread catalog deadline elapsed")

        cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_ids: set[str] = set()
        threads: list[NativeThreadMetadata] = []
        page_count = 0
        try:
            async with asyncio.timeout_at(deadline):
                while True:
                    if page_count >= max_pages:
                        raise ThreadCatalogLimitExceeded(
                            "native Thread catalog exceeded the page limit"
                        )
                    response = await self._codex.thread_list(
                        archived=archived,
                        cursor=cursor,
                        limit=_THREAD_LIST_PAGE_LIMIT,
                        model_providers=[],
                    )
                    page_count += 1
                    data = getattr(response, "data", None)
                    if not isinstance(data, list):
                        raise ThreadCatalogError(
                            "thread_list returned an invalid data page"
                        )
                    for thread in data:
                        thread_id = getattr(thread, "id", None)
                        name = getattr(thread, "name", None)
                        preview = getattr(thread, "preview", None)
                        if not isinstance(thread_id, str) or not thread_id:
                            raise ThreadCatalogError(
                                "thread_list returned an invalid Thread ID"
                            )
                        if thread_id in seen_ids:
                            raise ThreadCatalogError(
                                "thread_list repeated a native Thread ID"
                            )
                        if name is not None and not isinstance(name, str):
                            raise ThreadCatalogError(
                                "thread_list returned an invalid Thread name"
                            )
                        if not isinstance(preview, str):
                            raise ThreadCatalogError(
                                "thread_list returned an invalid Thread preview"
                            )
                        if len(threads) >= max_items:
                            raise ThreadCatalogLimitExceeded(
                                "native Thread catalog exceeded the item limit"
                            )
                        seen_ids.add(thread_id)
                        threads.append(
                            NativeThreadMetadata(
                                thread_id=thread_id,
                                name=name,
                                preview=preview,
                            )
                        )

                    next_cursor = getattr(response, "next_cursor", None)
                    if next_cursor is None:
                        break
                    if (
                        not isinstance(next_cursor, str)
                        or not next_cursor
                        or next_cursor in seen_cursors
                    ):
                        raise ThreadCatalogError(
                            "thread_list returned an invalid pagination cursor"
                        )
                    seen_cursors.add(next_cursor)
                    cursor = next_cursor
        except TimeoutError as error:
            raise ThreadCatalogDeadlineExceeded(
                "native Thread catalog deadline elapsed"
            ) from error
        return NativeThreadCatalog(archived=archived, threads=tuple(threads))

    async def thread_catalog_state(
        self,
        thread_id: str,
    ) -> NativeThreadCatalogState:
        """Classify one exact native ID without treating absence as active."""

        active = await self.thread_metadata((thread_id,), archived=False)
        archived = await self.thread_metadata((thread_id,), archived=True)
        if thread_id in active and thread_id in archived:
            raise ThreadCatalogError(
                "native Thread appeared in both active and archived catalogs"
            )
        if thread_id in active:
            return NativeThreadCatalogState.ACTIVE
        if thread_id in archived:
            return NativeThreadCatalogState.ARCHIVED
        return NativeThreadCatalogState.MISSING

    async def _thread_delete_catalog_state(
        self,
        thread_id: str,
    ) -> NativeThreadCatalogState:
        """Reconcile one delete against both rollout scan and state DB views."""

        deadline = (
            asyncio.get_running_loop().time()
            + _THREAD_DELETE_RECONCILE_TIMEOUT_SECONDS
        )
        active_present = False
        archived_present = False
        for use_state_db_only in (False, True):
            active = await self.thread_metadata(
                (thread_id,),
                archived=False,
                deadline=deadline,
                max_pages=_THREAD_CATALOG_MAX_PAGES,
                max_items=_THREAD_CATALOG_MAX_ITEMS,
                use_state_db_only=use_state_db_only,
            )
            archived = await self.thread_metadata(
                (thread_id,),
                archived=True,
                deadline=deadline,
                max_pages=_THREAD_CATALOG_MAX_PAGES,
                max_items=_THREAD_CATALOG_MAX_ITEMS,
                use_state_db_only=use_state_db_only,
            )
            active_present = active_present or thread_id in active
            archived_present = archived_present or thread_id in archived
        if active_present and archived_present:
            raise ThreadCatalogError(
                "native Thread appeared in active and archived delete views"
            )
        if active_present:
            return NativeThreadCatalogState.ACTIVE
        if archived_present:
            return NativeThreadCatalogState.ARCHIVED
        return NativeThreadCatalogState.MISSING

    def context_window_usage(
        self,
        binding_id: str,
    ) -> ContextWindowUsage | None:
        """Return the latest public usage update observed by this process."""

        snapshot = self._context_window_usage.get(binding_id)
        if snapshot is None:
            return None
        binding = self._bindings.get(binding_id)
        if binding.native_thread_id != snapshot.thread_id:
            return None
        return snapshot.usage

    def _invalidate_context_window_usage(self, binding_id: str) -> None:
        self._context_window_usage.pop(binding_id, None)

    def lifecycle_state(
        self,
        binding_id: str,
    ) -> ThreadLifecycleSnapshot | None:
        active = self._lifecycles.get(binding_id)
        if active is None:
            return None
        return ThreadLifecycleSnapshot(
            binding_id=active.binding_id,
            thread_id=active.thread_id,
            state=active.state,
        )

    async def thread_is_archived(self, thread_id: str) -> bool:
        return thread_id in await self.thread_metadata(
            (thread_id,),
            archived=True,
        )

    async def rename_binding(
        self,
        binding: ThreadBinding,
        name: str,
    ) -> str:
        """Compatibility wrapper for the exact Binding rename primitive."""

        return await self.rename_exact(binding.id, name)

    async def rename_exact(
        self,
        binding_id: str,
        name: str,
    ) -> str:
        normalized = " ".join(name.split())
        if not normalized or len(normalized) > 120:
            raise ValueError("会话名称必须为 1 到 120 个字符。")
        if not self._accepting:
            raise RuntimeClosed("服务正在停止，暂不能重命名会话。")

        async with self._lock(binding_id):
            if not self._accepting:
                raise RuntimeClosed("服务正在停止，暂不能重命名会话。")
            self._guard_no_lifecycle_locked(binding_id)
            binding = self._bindings.get(binding_id)
            if binding.native_thread_id is None:
                raise ThreadNotMaterialized(
                    "当前会话尚未创建原生 Codex Thread；"
                    "请先发送一条真实任务，再使用 /rename。"
                )
            operation = self._begin_lifecycle_locked(
                binding,
                ThreadLifecycleState.RENAMING,
            )
            mutation_attempted = False
            try:
                thread = await self._lifecycle_thread_locked(binding)
                mutation_attempted = True
                await thread.set_name(normalized)
            except asyncio.CancelledError:
                if mutation_attempted:
                    self._retain_unknown_lifecycle(operation)
                else:
                    self._finish_lifecycle_locked(operation)
                raise
            except Exception as error:
                if mutation_attempted:
                    self._retain_unknown_lifecycle(operation)
                    raise ThreadLifecycleStateUnknown(
                        "Codex 会话重命名结果未确认；当前 Binding "
                        "的生命周期状态待对账。"
                    ) from error
                self._finish_lifecycle_locked(operation)
                self._schedule_known_subscription_locked(
                    binding.id,
                    binding.native_thread_id,
                )
                raise ThreadLifecycleError(
                    "无法打开当前原生 Codex Thread，未执行重命名。"
                ) from error
            self._finish_lifecycle_locked(operation)
            self._schedule_known_subscription_locked(
                binding.id,
                binding.native_thread_id,
            )
            return normalized

    async def archive_binding(self, binding: ThreadBinding) -> ThreadBinding:
        """Compatibility wrapper for the exact Binding archive primitive."""

        return await self.archive_exact(binding.id)

    async def archive_exact(self, binding_id: str) -> ThreadBinding:
        if not self._accepting:
            raise RuntimeClosed("服务正在停止，暂不能归档会话。")
        async with self._lock(binding_id):
            if not self._accepting:
                raise RuntimeClosed("服务正在停止，暂不能归档会话。")
            self._guard_no_lifecycle_locked(binding_id)
            binding = self._bindings.get(binding_id)
            if binding.native_thread_id is None:
                raise ThreadNotMaterialized(
                    "Lazy 会话没有原生历史可归档；如不再需要，请使用 /delete。"
                )
            operation = self._begin_lifecycle_locked(
                binding,
                ThreadLifecycleState.ARCHIVING,
            )
            thread_id = binding.native_thread_id

        mutation_error: Exception | None = None
        try:
            await self._codex.thread_archive(thread_id)
        except asyncio.CancelledError:
            await self._mark_lifecycle_unknown(operation)
            raise
        except Exception as error:
            mutation_error = error

        if mutation_error is not None:
            try:
                async with asyncio.timeout(
                    _THREAD_DELETE_RECONCILE_TIMEOUT_SECONDS
                ):
                    state = await self.thread_catalog_state(thread_id)
            except asyncio.CancelledError:
                await self._mark_lifecycle_unknown(operation)
                raise
            except Exception as reconcile_error:
                await self._mark_lifecycle_unknown(operation)
                raise ThreadLifecycleStateUnknown(
                    "Codex 会话归档结果与原生目录均未确认；Binding 保留，"
                    "请稍后重新检查。"
                ) from reconcile_error
            if state is NativeThreadCatalogState.ACTIVE:
                await self._release_lifecycle_reservation(operation)
                raise ThreadLifecycleError(
                    "Codex 会话仍在 active 目录，Binding 已保留；"
                    "本次归档未完成，请重新确认后重试。"
                ) from mutation_error
            if state is not NativeThreadCatalogState.ARCHIVED:
                await self._mark_lifecycle_unknown(operation)
                raise ThreadLifecycleStateUnknown(
                    "Codex 会话归档结果无法由原生目录确认；Binding 保留，"
                    "请稍后重新检查。"
                ) from mutation_error

        local_commit_error: Exception | None = None
        async with self._lock(binding_id):
            binding = self._require_reserved_lifecycle_binding_locked(operation)
            discarded = self._discard_local_thread_activity_locked(
                binding.id,
                thread_id,
            )
            try:
                archived = self._bindings.deactivate_if_active(
                    scope_key=binding.scope_key,
                    binding_id=binding.id,
                )
            except Exception as error:
                self._retain_unknown_lifecycle(operation)
                local_commit_error = error

        try:
            await self._deliver_thread_activity_discarded(discarded)
        finally:
            if local_commit_error is None:
                async with self._lock(binding_id):
                    self._finish_lifecycle_locked(operation)
        if local_commit_error is not None:
            raise ThreadLifecycleStateUnknown(
                "原生 Codex 会话已归档，但本地 Binding 更新结果未确认。"
            ) from local_commit_error
        return archived

    async def delete_binding(self, binding: ThreadBinding) -> ThreadBinding:
        """Compatibility wrapper for exact Binding delete."""

        return await self.delete_exact(
            binding.id,
            expected_native_thread_id=binding.native_thread_id,
        )

    async def delete_exact(
        self,
        binding_id: str,
        *,
        expected_native_thread_id: str | None,
    ) -> ThreadBinding:
        """Delete one exact Lazy or materialized Binding."""

        return await self._delete_binding_exact(
            binding_id,
            allow_materialized=True,
            expected_native_thread_id=expected_native_thread_id,
        )

    async def delete_archived_exact(
        self,
        binding_id: str,
        *,
        expected_native_thread_id: str,
    ) -> ThreadBinding:
        """Delete one exact persisted Thread through the shared primitive."""

        return await self.delete_exact(
            binding_id,
            expected_native_thread_id=expected_native_thread_id,
        )

    async def delete_lazy_exact(self, binding_id: str) -> ThreadBinding:
        """Delete one exact Lazy Binding without reaching native delete."""

        return await self._delete_binding_exact(
            binding_id,
            allow_materialized=False,
            expected_native_thread_id=None,
        )

    async def _delete_binding_exact(
        self,
        binding_id: str,
        *,
        allow_materialized: bool,
        expected_native_thread_id: str | None,
    ) -> ThreadBinding:
        if not self._accepting:
            raise RuntimeClosed("服务正在停止，暂不能删除会话。")
        async with self._lock(binding_id):
            if not self._accepting:
                raise RuntimeClosed("服务正在停止，暂不能删除会话。")
            self._guard_no_lifecycle_locked(binding_id)
            binding = self._bindings.get(binding_id)
            if binding.native_thread_id != expected_native_thread_id:
                if not allow_materialized and binding.native_thread_id is not None:
                    raise ThreadDeleteUnavailable(
                        "已有原生历史的会话不支持 Lazy 删除；本次未调用 Codex。"
                    )
                raise ThreadDeleteTargetChanged(
                    "会话的原生 Thread 已变化，本删除确认未执行；"
                    "请重新发送 /delete。"
                )

            if binding.native_thread_id is None:
                operation = self._begin_lifecycle_locked(
                    binding,
                    ThreadLifecycleState.DELETING,
                )
                try:
                    deleted = self._bindings.delete_binding(binding.id)
                except Exception as error:
                    self._retain_unknown_lifecycle(operation)
                    raise ThreadLifecycleStateUnknown(
                        "Lazy 会话删除结果未确认。"
                    ) from error
                self._finish_lifecycle_locked(operation)
                return deleted

            if not allow_materialized:
                raise ThreadDeleteUnavailable(
                    "已有原生历史的会话不支持删除；本次未调用 Codex。"
                )

            control = self._thread_delete_control
            if control is None:
                raise ThreadDeleteUnavailable(
                    "当前 SDK/App Server 的 Thread Delete 兼容契约未通过；"
                    "本次未调用 Codex，Binding 与原生历史均未改变。"
                )
            operation = self._begin_lifecycle_locked(
                binding,
                ThreadLifecycleState.DELETING,
            )
            thread_id = binding.native_thread_id

        return await self._delete_materialized_reserved(operation, thread_id)

    async def _delete_materialized_reserved(
        self,
        operation: _ActiveThreadLifecycle,
        thread_id: str,
    ) -> ThreadBinding:
        control = self._thread_delete_control
        if control is None:
            await self._release_lifecycle_reservation(operation)
            raise ThreadDeleteUnavailable(
                "当前 SDK/App Server 的 Thread Delete 兼容契约未通过；"
                "本次未调用 Codex，Binding 与原生历史均未改变。"
            )

        delete_error: Exception | None = None
        try:
            await control.delete(thread_id)
        except asyncio.CancelledError:
            await self._mark_lifecycle_unknown(operation)
            raise
        except Exception as error:
            delete_error = error

        if delete_error is not None:
            try:
                state = await self._thread_delete_catalog_state(thread_id)
            except asyncio.CancelledError:
                await self._mark_lifecycle_unknown(operation)
                raise
            except Exception as reconcile_error:
                await self._mark_lifecycle_unknown(operation)
                raise ThreadLifecycleStateUnknown(
                    "Codex 会话删除结果与原生目录均未确认；Binding 保留且"
                    "当前 Binding 生命周期状态未知，请稍后重新检查。"
                ) from reconcile_error
            if state is not NativeThreadCatalogState.MISSING:
                await self._release_lifecycle_reservation(operation)
                raise ThreadLifecycleError(
                    "Codex 会话仍存在于原生目录，Binding 已保留；"
                    "本次删除未完成，请重新确认后重试。"
                ) from delete_error

        local_commit_error: Exception | None = None
        async with self._lock(operation.binding_id):
            binding = self._require_reserved_lifecycle_binding_locked(operation)
            discarded = self._discard_local_thread_activity_locked(
                binding.id,
                thread_id,
            )
            try:
                deleted = self._bindings.delete_binding(binding.id)
            except Exception as error:
                self._retain_unknown_lifecycle(operation)
                local_commit_error = error

        try:
            await self._deliver_thread_activity_discarded(discarded)
        finally:
            if local_commit_error is None:
                async with self._lock(operation.binding_id):
                    self._finish_lifecycle_locked(operation)
        if local_commit_error is not None:
            raise ThreadLifecycleStateUnknown(
                "原生 Codex 会话已删除或确认不存在，但 Binding 删除结果未确认。"
            ) from local_commit_error
        return deleted

    async def unarchive_binding(self, binding: ThreadBinding) -> ThreadBinding:
        """Compatibility wrapper that restores and selects the Binding."""

        return await self.restore_as_current_exact(binding.id)

    async def restore_exact(self, binding_id: str) -> ThreadBinding:
        """Restore one archived Binding without changing its Scope pointer."""

        return await self._restore_exact(binding_id, activate=False)

    async def restore_as_current_exact(
        self,
        binding_id: str,
        *,
        context_anchor: MessageContextAnchor | None = None,
    ) -> ThreadBinding:
        """Restore one archived Binding and select it in its Scope."""

        return await self._restore_exact(
            binding_id,
            activate=True,
            context_anchor=context_anchor,
        )

    async def _restore_exact(
        self,
        binding_id: str,
        *,
        activate: bool,
        context_anchor: MessageContextAnchor | None = None,
    ) -> ThreadBinding:
        if not self._accepting:
            raise RuntimeClosed("服务正在停止，暂不能恢复归档会话。")
        async with self._lock(binding_id):
            if not self._accepting:
                raise RuntimeClosed("服务正在停止，暂不能恢复归档会话。")
            self._guard_no_lifecycle_locked(binding_id)
            binding = self._bindings.get(binding_id)
            if activate:
                if (
                    binding.message_context_mode is MentionContextMode.CATCH_UP
                    and context_anchor is None
                ):
                    raise ContextAnchorRequired(
                        "该会话启用了 @ 时补充上下文；请从飞书恢复卡片操作，"
                        "以建立新的消息边界。"
                    )
                if (
                    binding.message_context_mode is MentionContextMode.CURRENT_ONLY
                    and context_anchor is not None
                ):
                    raise ValueError(
                        "current-only restore cannot carry a context anchor"
                    )
            elif context_anchor is not None:
                raise ValueError("non-current restore cannot carry a context anchor")
            if binding.native_thread_id is None:
                raise ThreadNotArchived("Lazy 会话不是归档会话。")
            if binding.id in self._active or binding.id in self._compacting:
                raise ThreadLifecycleError(
                    "该 Binding 仍有本服务原生活动，不能作为归档会话恢复。"
                )
            if binding.id in self._goals:
                raise self._goal_slot_error(self._goals[binding.id])
            if not await self.thread_is_archived(binding.native_thread_id):
                raise ThreadNotArchived(
                    "该会话不在 Codex 归档列表中；请刷新 /sessions archived。"
                )
            operation = self._begin_lifecycle_locked(
                binding,
                ThreadLifecycleState.UNARCHIVING,
            )
            try:
                unarchived = await self._codex.thread_unarchive(
                    binding.native_thread_id
                )
                if unarchived.id != binding.native_thread_id:
                    raise RuntimeError(
                        "thread_unarchive returned a different native ID"
                    )
                thread = await self._codex.thread_resume(binding.native_thread_id)
                if thread.id != binding.native_thread_id:
                    raise RuntimeError(
                        "thread_resume returned a different unarchived native ID"
                    )
                restored = (
                    self._bindings.activate(
                        scope_key=binding.scope_key,
                        binding_id=binding.id,
                        context_anchor=context_anchor,
                    )
                    if activate
                    else self._bindings.get(binding.id)
                )
                self._mark_thread_subscribed_locked(restored, thread)
                if activate:
                    self._advance_admission_revision(binding.id)
            except asyncio.CancelledError:
                self._retain_unknown_lifecycle(operation)
                raise
            except Exception as error:
                self._retain_unknown_lifecycle(operation)
                raise ThreadLifecycleStateUnknown(
                    "Codex 会话恢复结果未确认；当前 Binding "
                    "的生命周期状态待对账。"
                ) from error
            self._finish_lifecycle_locked(operation)
            self._schedule_known_subscription_locked(
                restored.id,
                restored.native_thread_id,
            )
            return restored

    async def resolve_model_settings(
        self,
        *,
        model_id: str,
        effort_id: str,
        service_tier_id: str,
    ) -> TurnModelSettings:
        catalog = await self.model_catalog()
        return catalog.resolve(
            model_id=model_id,
            effort_id=effort_id,
            service_tier_id=service_tier_id,
        )

    async def configure_turn_settings(
        self,
        *,
        binding_id: str,
        expected_revision: int,
        settings: BindingTurnSettings | None,
    ) -> ThreadBinding:
        """Compatibility wrapper for the exact configuration primitive."""

        return await self.configure_exact(
            binding_id=binding_id,
            expected_revision=expected_revision,
            settings=settings,
        )

    async def configure_exact(
        self,
        *,
        binding_id: str,
        expected_revision: int,
        settings: BindingTurnSettings | None,
    ) -> ThreadBinding:
        """Store a validated selection for one exact Binding."""

        if not self._accepting:
            raise RuntimeClosed("服务正在停止，暂不能修改会话配置。")
        if binding_id in self._compacting:
            raise ThreadCompacting(
                "当前会话正在压缩上下文，完成前不能修改 Model / Effort / Speed。"
            )
        if binding_id in self._goals:
            raise self._goal_slot_error(self._goals[binding_id])
        active = self._active.get(binding_id)
        if active is not None:
            raise ThreadRunningConfiguration(
                "当前 Turn 正在执行，不能修改 Model / Effort / Speed；"
                "请等待完成或先发送 /stop。"
            )

        async with self._lock(binding_id):
            if not self._accepting:
                raise RuntimeClosed("服务正在停止，暂不能修改会话配置。")
            self._guard_no_lifecycle_locked(binding_id)
            if binding_id in self._compacting:
                raise ThreadCompacting(
                    "当前会话正在压缩上下文，完成前不能修改 "
                    "Model / Effort / Speed。"
                )
            binding = self._bindings.get(binding_id)
            await self._guard_no_goal_locked(binding)
            binding = self._bindings.get(binding_id)
            active = self._active.get(binding_id)
            if active is not None:
                if active.state is ActiveState.STOPPING:
                    raise ThreadStopping(
                        "当前 Turn 正在停止，不能修改 Model / Effort / Speed；"
                        "若 /stop 曾提示清理失败，请再次发送 /stop 重试。"
                    )
                raise ThreadRunningConfiguration(
                    "当前 Turn 正在执行，不能修改 Model / Effort / Speed；"
                    "请等待完成或先发送 /stop。"
                )
            updated = self._bindings.set_turn_settings(
                binding_id=binding_id,
                expected_revision=expected_revision,
                settings=settings,
            )
            if updated.settings_revision != binding.settings_revision:
                self._advance_admission_revision(binding_id)
            return updated

    async def configure_context_exact(
        self,
        *,
        binding_id: str,
        expected_settings_revision: int,
        expected_context_revision: int,
        expected_feedback_revision: int,
        settings: BindingTurnSettings | None,
        task_feedback: BindingTaskFeedback,
        message_context_mode: MentionContextMode,
        context_anchor: MessageContextAnchor | None,
    ) -> ThreadBinding:
        """Atomically update Turn settings, context, and task feedback."""

        if not self._accepting:
            raise RuntimeClosed("服务正在停止，暂不能修改会话配置。")
        if binding_id in self._compacting:
            raise ThreadCompacting("当前会话正在压缩上下文，完成前不能修改会话配置。")
        if binding_id in self._goals:
            raise self._goal_slot_error(self._goals[binding_id])
        active = self._active.get(binding_id)
        if active is not None:
            raise ThreadRunningConfiguration(
                "当前 Turn 正在执行，不能修改会话配置；请等待完成或先发送 /stop。"
            )

        async with self._lock(binding_id):
            if not self._accepting:
                raise RuntimeClosed("服务正在停止，暂不能修改会话配置。")
            self._guard_no_lifecycle_locked(binding_id)
            if binding_id in self._compacting:
                raise ThreadCompacting(
                    "当前会话正在压缩上下文，完成前不能修改会话配置。"
                )
            binding = self._bindings.get(binding_id)
            await self._guard_no_goal_locked(binding)
            binding = self._bindings.get(binding_id)
            active = self._active.get(binding_id)
            if active is not None:
                if active.state is ActiveState.STOPPING:
                    raise ThreadStopping(
                        "当前 Turn 正在停止，不能修改会话配置；"
                        "若 /stop 曾提示清理失败，请再次发送 /stop 重试。"
                    )
                raise ThreadRunningConfiguration(
                    "当前 Turn 正在执行，不能修改会话配置；"
                    "请等待完成或先发送 /stop。"
                )
            updated = self._bindings.set_configuration(
                binding_id=binding_id,
                expected_settings_revision=expected_settings_revision,
                expected_context_revision=expected_context_revision,
                expected_feedback_revision=expected_feedback_revision,
                settings=settings,
                task_feedback=task_feedback,
                message_context_mode=message_context_mode,
                context_anchor=context_anchor,
            )
            if (
                updated.settings_revision != binding.settings_revision
                or updated.context_revision != binding.context_revision
                or updated.feedback_revision != binding.feedback_revision
            ):
                self._advance_admission_revision(binding_id)
            return updated

    async def capture_submission_admission(
        self,
        binding_id: str,
    ) -> SubmissionAdmission:
        """Snapshot the exact start/steer target without reserving it.

        Callers may perform bounded network I/O after this method returns, then
        pass the token back to :meth:`submit`. Any intervening prompt, stop, or
        completion invalidates the token instead of changing its disposition.
        """

        if not self._accepting:
            raise RuntimeClosed("服务正在停止，暂不接受新任务。")
        async with self._lock(binding_id):
            if not self._accepting:
                raise RuntimeClosed("服务正在停止，暂不接受新任务。")
            self._guard_no_lifecycle_locked(binding_id)
            if binding_id in self._compacting:
                raise ThreadCompacting(
                    "当前会话正在压缩上下文，完成前暂不接受新消息。"
                )
            binding = self._bindings.get(binding_id)
            if not binding.active:
                raise SteerRace(
                    "准备本条消息期间 active 会话已切换，本条消息未执行，请重新发送。"
                )
            await self._guard_no_goal_locked(binding)
            binding = self._bindings.get(binding_id)
            if not binding.active:
                raise SteerRace(
                    "准备本条消息期间 active 会话已切换，本条消息未执行，请重新发送。"
                )
            active = self._active.get(binding_id)
            if active is not None and active.state is ActiveState.STOPPING:
                raise ThreadStopping(
                    "当前任务正在停止，暂不接受新消息；"
                    "若 /stop 曾提示清理失败，请再次发送 /stop 重试。"
                )
            if (
                active is not None
                and active.state is ActiveState.OBSERVATION_UNAVAILABLE
            ):
                raise TurnObservationUnavailable(
                    "当前 Turn 状态无法确认，暂不能接收新消息；"
                    "请在 /sessions 中重新检查，或直接归档、删除会话。"
                )
            return SubmissionAdmission(
                binding_id=binding_id,
                revision=self._admission_revision(binding_id),
                thread_id=active.handle.thread_id if active is not None else None,
                turn_id=active.handle.id if active is not None else None,
                settings_revision=binding.settings_revision,
                context_revision=binding.context_revision,
                feedback_revision=binding.feedback_revision,
            )

    async def submit(
        self,
        *,
        binding: ThreadBinding,
        cwd: Path,
        input: Any,
        owner_id: str,
        origin: object,
        admission: SubmissionAdmission | None = None,
        context_commit: ContextCursorCommit | None = None,
        skill_names: tuple[str, ...] = (),
    ) -> Submission:
        if admission is not None and admission.binding_id != binding.id:
            raise ValueError("submission admission belongs to another Binding")
        prepared_binding = self._bindings.get(binding.id)
        if not prepared_binding.active:
            raise SteerRace(
                "准备本条消息期间 active 会话已切换，本条消息未执行，请重新发送。"
            )
        prepared_settings_revision = prepared_binding.settings_revision
        prepared_context_revision = prepared_binding.context_revision
        prepared_feedback_revision = prepared_binding.feedback_revision
        configured_settings = prepared_binding.turn_settings
        if admission is not None and (
            admission.settings_revision != prepared_settings_revision
        ):
            raise SteerRace(
                "准备本条消息期间会话配置已变化，本条消息未执行，请重新发送。"
            )
        if admission is not None and (
            admission.context_revision != prepared_context_revision
        ):
            raise SteerRace(
                "准备本条消息期间上下文边界已变化，本条消息未执行，请重新发送。"
            )
        if admission is not None and (
            admission.feedback_revision != prepared_feedback_revision
        ):
            raise SteerRace(
                "准备本条消息期间任务反馈配置已变化，本条消息未执行，请重新发送。"
            )
        if prepared_binding.message_context_mode is MentionContextMode.CATCH_UP:
            if admission is None or context_commit is None:
                raise ValueError("catch-up submission requires admission and cursor commit")
            if (
                context_commit.expected_context_revision
                != admission.context_revision
            ):
                raise ValueError("context cursor commit does not match admission")
        elif context_commit is not None:
            raise ValueError("current-only submission cannot commit a context cursor")
        if configured_settings is not None and admission is None:
            admission = await self.capture_submission_admission(binding.id)
            prepared_binding = self._bindings.get(binding.id)
            if admission.settings_revision != prepared_binding.settings_revision:
                raise SteerRace(
                    "准备本条消息期间会话配置已变化，本条消息未执行，请重新发送。"
                )
            if admission.context_revision != prepared_binding.context_revision:
                raise SteerRace(
                    "准备本条消息期间上下文边界已变化，本条消息未执行，请重新发送。"
                )
            if admission.feedback_revision != prepared_binding.feedback_revision:
                raise SteerRace(
                    "准备本条消息期间任务反馈配置已变化，本条消息未执行，请重新发送。"
                )
            prepared_settings_revision = prepared_binding.settings_revision
            prepared_context_revision = prepared_binding.context_revision
            prepared_feedback_revision = prepared_binding.feedback_revision
            configured_settings = prepared_binding.turn_settings

        resolved_settings = None
        settings_for_new_turn = configured_settings
        if admission is not None and admission.thread_id is not None:
            # A running Turn can only be steered. Binding settings apply only
            # when Netizen starts a new Turn and are never injected into steer.
            settings_for_new_turn = None
        if settings_for_new_turn is not None:
            # models() reaches App Server, so keep it outside the Binding lock.
            # The admission and lock-time settings revision checks below make
            # any intervening state change abort instead of reinterpreting the
            # prepared input.
            resolved_settings = await self.resolve_model_settings(
                model_id=settings_for_new_turn.model_id,
                effort_id=settings_for_new_turn.effort_id,
                service_tier_id=settings_for_new_turn.service_tier_id,
            )
        native_input = input
        if skill_names:
            if admission is None:
                admission = await self.capture_submission_admission(binding.id)
            native_input = await self._compile_skill_input(
                cwd=cwd,
                text=input,
                names=skill_names,
            )
        if not self._accepting:
            raise RuntimeClosed("服务正在停止，暂不接受新任务。")
        if binding.id in self._compacting:
            raise ThreadCompacting(
                "当前会话正在压缩上下文，完成前暂不接受新消息。"
            )
        if binding.id in self._goals:
            raise self._goal_slot_error(self._goals[binding.id])
        active = self._active.get(binding.id)
        if active is not None and active.state is ActiveState.STOPPING:
            raise ThreadStopping(
                "当前任务正在停止，暂不接受新消息；"
                "若 /stop 曾提示清理失败，请再次发送 /stop 重试。"
            )
        if (
            active is not None
            and active.state is ActiveState.OBSERVATION_UNAVAILABLE
        ):
            raise TurnObservationUnavailable(
                "当前 Turn 状态无法确认，暂不能接收新消息；"
                "请在 /sessions 中重新检查，或直接归档、删除会话。"
            )

        async with self._lock(binding.id):
            if not self._accepting:
                raise RuntimeClosed("服务正在停止，暂不接受新任务。")
            self._guard_no_lifecycle_locked(binding.id)
            if binding.id in self._compacting:
                raise ThreadCompacting(
                    "当前会话正在压缩上下文，完成前暂不接受新消息。"
                )
            binding = self._bindings.get(binding.id)
            if not binding.active:
                raise SteerRace(
                    "准备本条消息期间 active 会话已切换，本条消息未执行，请重新发送。"
                )
            if binding.settings_revision != prepared_settings_revision:
                raise SteerRace(
                    "准备本条消息期间会话配置已变化，本条消息未执行，请重新发送。"
                )
            if binding.context_revision != prepared_context_revision:
                raise SteerRace(
                    "准备本条消息期间上下文边界已变化，本条消息未执行，请重新发送。"
                )
            if binding.feedback_revision != prepared_feedback_revision:
                raise SteerRace(
                    "准备本条消息期间任务反馈配置已变化，本条消息未执行，请重新发送。"
                )
            await self._guard_no_goal_locked(binding)
            binding = self._bindings.get(binding.id)
            if not binding.active:
                raise SteerRace(
                    "准备本条消息期间 active 会话已切换，本条消息未执行，请重新发送。"
                )
            active = self._active.get(binding.id)
            if active is not None:
                if active.state is ActiveState.STOPPING:
                    raise ThreadStopping(
                        "当前任务正在停止，暂不接受新消息；"
                        "若 /stop 曾提示清理失败，请再次发送 /stop 重试。"
                    )
                if active.state is ActiveState.OBSERVATION_UNAVAILABLE:
                    raise TurnObservationUnavailable(
                        "当前 Turn 状态无法确认，暂不能接收新消息；"
                        "请在 /sessions 中重新检查，或直接归档、删除会话。"
                    )
            self._redeem_submission_admission(
                binding_id=binding.id,
                active=active,
                admission=admission,
            )
            self._advance_admission_revision(binding.id)
            if active is not None:
                self._mark_thread_subscribed_locked(binding, active.thread)
                self._refresh_turn_activity(active)
                stale_after_cursor = active.plan_cursor
                try:
                    await active.handle.steer(native_input)
                except asyncio.CancelledError:
                    # The official async SDK delegates steer to a blocking
                    # worker-thread RPC. Cancellation does not prove that the
                    # native request stopped, so fail closed service-wide.
                    self.close_admission()
                    raise
                except InvalidRequestError as error:
                    raise SteerRace(
                        "当前任务恰好已经结束，本条消息未执行，请重新发送。"
                    ) from error
                active.steer_count += 1
                last_update = active.plan_last_update_cursor
                if last_update is not None and last_update > stale_after_cursor:
                    # A concurrent /status may already have observed a plan
                    # emitted after steer began but before its RPC returned.
                    active.plan_may_be_stale = False
                    active.plan_stale_after_cursor = None
                else:
                    active.plan_may_be_stale = True
                    active.plan_stale_after_cursor = stale_after_cursor
                active.activity_revision += 1
                self._commit_context_cursor_locked(
                    binding=binding,
                    commit=context_commit,
                )
                return Submission(
                    disposition=SubmitDisposition.STEERED,
                    binding_id=binding.id,
                    thread_id=active.handle.thread_id,
                    turn_id=active.handle.id,
                    task_feedback=active.task_feedback,
                    feedback_revision=active.feedback_revision,
                )

            # A caller may hold the lazy Binding snapshot returned by /new even
            # after an earlier Turn persisted its native Thread ID.
            binding = self._bindings.get(binding.id)
            try:
                if binding.native_thread_id is None:
                    # The public API has no "inherit Ask/Custom" sentinel;
                    # omitting approval_mode deliberately selects its
                    # auto_review default.
                    thread = await self._codex.thread_start(cwd=str(cwd))
                else:
                    # Do not override cwd, sandbox, model, approval, config, or
                    # env while resuming an existing native Thread.
                    thread = await self._codex.thread_resume(
                        binding.native_thread_id
                    )
                    if thread.id != binding.native_thread_id:
                        raise RuntimeError(
                            "thread_resume returned a different native ID"
                        )
            except asyncio.CancelledError:
                self.close_admission()
                raise
            except Exception as error:
                # Starting/resuming is side-effectful and a lost response does
                # not prove that App Server left native state untouched.
                self.close_admission()
                raise TurnStartFailed(
                    "Codex Thread 启动或恢复结果未确认；"
                    "服务已停止接收新任务，请重启服务。"
                ) from error

            if binding.native_thread_id is None:
                # Reserve the App Server-issued ID before sending the first
                # prompt.  This is still lazy (/new never reaches here), but
                # prevents a conflicting Binding from receiving any Turn.
                try:
                    self._bindings.assign_native_thread_id(binding.id, thread.id)
                except Exception:
                    self.close_admission()
                    raise

            self._mark_thread_subscribed_locked(
                self._bindings.get(binding.id),
                thread,
            )

            try:
                turn_kwargs: dict[str, object] = {}
                if resolved_settings is not None:
                    turn_kwargs = {
                        "model": resolved_settings.model,
                        "effort": resolved_settings.effort,
                        "service_tier": resolved_settings.service_tier_id,
                    }
                handle = await thread.turn(native_input, **turn_kwargs)
            except asyncio.CancelledError:
                self._invalidate_context_window_usage(binding.id)
                self.close_admission()
                raise
            except Exception as error:
                # turn/start is side-effectful: a lost response cannot prove
                # that App Server did not start the Turn. Never admit another
                # Turn on this or any other Binding until the service restarts.
                self._invalidate_context_window_usage(binding.id)
                self.close_admission()
                raise TurnStartFailed(
                    "Codex Turn 启动结果未确认；服务已停止接收新任务，请重启服务。"
                ) from error
            if handle.thread_id != thread.id:
                self._invalidate_context_window_usage(binding.id)
                self.close_admission()
                raise RuntimeError(
                    "native Turn handle returned a different Thread ID; "
                    "service admission is closed"
                )

            receipt_attempted = asyncio.Event()
            active = _ActiveTurn(
                binding_id=binding.id,
                thread=thread,
                handle=handle,
                owner_id=owner_id,
                origin=origin,
                receipt_attempted=receipt_attempted,
                task_feedback=binding.task_feedback,
                feedback_revision=binding.feedback_revision,
                plan_available=self._turn_plan_observer is not None,
            )
            self._track(active)
            try:
                self._commit_context_cursor_locked(
                    binding=binding,
                    commit=context_commit,
                )
            except BaseException:
                # The native Turn is already accepted and tracked. Let its
                # consumer proceed even though the caller receives the exact
                # persistence warning instead of a normal receipt callback.
                receipt_attempted.set()
                raise
            return Submission(
                disposition=SubmitDisposition.STARTED,
                binding_id=binding.id,
                thread_id=thread.id,
                turn_id=handle.id,
                release_receipt_attempt=receipt_attempted.set,
                task_feedback=active.task_feedback,
                feedback_revision=active.feedback_revision,
            )

    async def _compile_skill_input(
        self,
        *,
        cwd: Path,
        text: Any,
        names: tuple[str, ...],
    ) -> list[TextInput | ImageInput | SkillInput]:
        if isinstance(text, str):
            native_input: list[TextInput | ImageInput | SkillInput] = [
                TextInput(text)
            ]
        elif (
            isinstance(text, list)
            and text
            and all(isinstance(item, (TextInput, ImageInput)) for item in text)
        ):
            native_input = list(text)
        else:
            raise SkillReferenceError(
                "带 $skill 的消息必须是文本或图片输入，本条消息未执行。"
            )
        if len(set(names)) != len(names):
            raise SkillReferenceError("同一条消息不能重复引用一个 Skill。")
        catalog = self._skill_catalog
        if catalog is None:
            raise SkillReferenceError("当前原生 Skills discovery 不可用。")
        snapshot = await catalog.list(cwd, force_reload=True)
        if snapshot.errors:
            raise SkillReferenceError(
                "Codex 扫描当前 Project 的 Skills 时报告错误："
                + "；".join(snapshot.errors)[:1_000]
            )
        resolved: list[SkillInput] = []
        for name in names:
            matches = tuple(skill for skill in snapshot.skills if skill.name == name)
            if not matches:
                raise SkillReferenceError(
                    f"当前 Project 中找不到 Skill ${name}；本条消息未执行。"
                )
            if len(matches) != 1:
                raise SkillReferenceError(
                    f"Skill ${name} 在当前目录中存在歧义；本条消息未执行。"
                )
            skill = matches[0]
            if not skill.enabled:
                raise SkillReferenceError(
                    f"Skill ${name} 当前未启用；本条消息未执行。"
                )
            resolved.append(SkillInput(name=skill.name, path=skill.path))
        return [*native_input, *resolved]

    def active_turn(self, binding_id: str) -> ActiveTurnSnapshot | None:
        active = self._active.get(binding_id)
        if active is None:
            return None
        return ActiveTurnSnapshot(
            binding_id=binding_id,
            thread_id=active.handle.thread_id,
            turn_id=active.handle.id,
            owner_id=active.owner_id,
            state=active.state,
        )

    def turn_progress(self, binding_id: str) -> TurnProgressSnapshot | None:
        """Project the latest native plan for the current active Turn only."""

        activity = self.turn_activity(binding_id, refresh_plan=True)
        if activity is None:
            return None
        return TurnProgressSnapshot(
            binding_id=activity.binding_id,
            thread_id=activity.thread_id,
            turn_id=activity.turn_id,
            steer_count=activity.steer_count,
            plan_available=activity.plan_available,
            plan_generated=activity.plan_generated,
            plan_may_be_stale=activity.plan_may_be_stale,
            steps=activity.steps,
        )

    def turn_activity(
        self,
        binding_id: str,
        *,
        thread_id: str | None = None,
        turn_id: str | None = None,
        refresh_plan: bool = False,
    ) -> TurnActivitySnapshot | None:
        """Return one exact Turn's latest process-local display projection.

        Callers opt into the pinned plan observation explicitly. A disabled
        progress presenter therefore adds no polling merely because a Turn is
        active. Exact IDs fail closed to ``None`` so a delayed card updater
        cannot render a replacement Turn into an older card.
        """

        active = self._active.get(binding_id)
        if active is None or active.terminal_observed:
            return None
        if thread_id is not None and active.handle.thread_id != thread_id:
            return None
        if turn_id is not None and active.handle.id != turn_id:
            return None
        if refresh_plan:
            self._refresh_turn_activity(active)
        return self._turn_activity_snapshot(active)

    def _turn_activity_snapshot(self, active: _ActiveTurn) -> TurnActivitySnapshot:
        return TurnActivitySnapshot(
            binding_id=active.binding_id,
            thread_id=active.handle.thread_id,
            turn_id=active.handle.id,
            revision=active.activity_revision,
            state=active.state,
            steer_count=active.steer_count,
            plan_available=active.plan_available,
            plan_generated=active.plan_generated,
            plan_may_be_stale=active.plan_may_be_stale,
            steps=active.plan_steps,
            commentary=self._activity_commentary(active),
            operations=self._activity_operations(active),
        )

    def side_turn_activity(
        self,
        side_id: str,
        *,
        thread_id: str | None = None,
        turn_id: str | None = None,
        refresh_plan: bool = False,
    ) -> SideTurnActivitySnapshot | None:
        """Return one exact Side Turn's process-local display projection."""

        session = self._sides.get(side_id)
        if session is None or session.state is SideSessionState.CLOSING:
            return None
        active = session.active
        if active is None or active.terminal_observed:
            return None
        if thread_id is not None and active.handle.thread_id != thread_id:
            return None
        if turn_id is not None and active.handle.id != turn_id:
            return None
        if refresh_plan:
            self._refresh_turn_activity(active)
        return self._side_turn_activity_snapshot(side_id, active)

    def _side_turn_activity_snapshot(
        self,
        side_id: str,
        active: _ActiveSideTurn,
    ) -> SideTurnActivitySnapshot:
        return SideTurnActivitySnapshot(
            side_id=side_id,
            thread_id=active.handle.thread_id,
            turn_id=active.handle.id,
            revision=active.activity_revision,
            state=active.state,
            steer_count=active.steer_count,
            plan_available=active.plan_available,
            plan_generated=active.plan_generated,
            plan_may_be_stale=active.plan_may_be_stale,
            steps=active.plan_steps,
            commentary=self._activity_commentary(active),
            operations=self._activity_operations(active),
        )

    def _turn_activity_visible_state(
        self,
        active: _ActiveTurn | _ActiveSideTurn,
    ) -> tuple[object, ...]:
        return (
            active.state,
            active.steer_count,
            active.plan_available,
            active.plan_generated,
            active.plan_may_be_stale,
            active.plan_steps,
            self._activity_commentary(active),
            self._activity_operations(active),
        )

    def _refresh_turn_activity(
        self,
        active: _ActiveTurn | _ActiveSideTurn,
    ) -> TurnActivityObservation | None:
        before = self._turn_activity_visible_state(active)
        if not active.activity_observation_enabled:
            return None
        observer = self._turn_plan_observer
        if observer is None:
            active.plan_available = False
            if self._turn_activity_visible_state(active) != before:
                active.activity_revision += 1
            return None
        try:
            observation = observer.observe(
                thread_id=active.handle.thread_id,
                turn_id=active.handle.id,
                after_cursor=active.plan_cursor,
            )
        except Exception as error:
            active.plan_available = False
            if self._turn_activity_visible_state(active) != before:
                active.activity_revision += 1
            logger.warning(
                "native Turn activity observation unavailable",
                extra={
                    "thread_id": active.handle.thread_id,
                    "turn_id": active.handle.id,
                    "error_type": type(error).__name__,
                },
            )
            return None
        active.plan_available = True
        active.plan_cursor = observation.next_cursor
        self._apply_activity_events(active, observation.events)
        if observation.plan_updated:
            active.plan_steps = observation.steps
            active.plan_generated = True
            active.plan_last_update_cursor = observation.plan_cursor
            stale_after = active.plan_stale_after_cursor
            if (
                active.plan_may_be_stale
                and stale_after is not None
                and observation.plan_cursor is not None
                and observation.plan_cursor > stale_after
            ):
                active.plan_may_be_stale = False
                active.plan_stale_after_cursor = None
        if self._turn_activity_visible_state(active) != before:
            active.activity_revision += 1
        return observation

    @staticmethod
    def _activity_commentary(
        active: _ActiveTurn | _ActiveSideTurn | _ActiveGoal,
    ) -> tuple[str, ...]:
        return tuple(
            active.activity_commentary[item_id]
            for item_id in active.activity_commentary_order
            if item_id in active.activity_commentary
        )

    @staticmethod
    def _activity_operations(
        active: _ActiveTurn | _ActiveSideTurn | _ActiveGoal,
    ) -> tuple[TurnActivityEntrySnapshot, ...]:
        ordered: list[tuple[int, TurnActivityEntrySnapshot]] = []
        subagents: dict[TurnActivityStatus, tuple[int, int]] = {}
        for position, item_id in enumerate(active.activity_operation_order):
            event = active.activity_operations.get(item_id)
            if event is None:
                continue
            if event.kind is TurnActivityKind.SUBAGENT:
                _previous_position, previous_count = subagents.get(
                    event.status,
                    (position, 0),
                )
                subagents[event.status] = (
                    position,
                    previous_count + event.count,
                )
                continue
            ordered.append(
                (
                    position,
                    TurnActivityEntrySnapshot(
                        kind=event.kind,
                        status=event.status,
                        text=event.text,
                        count=event.count,
                    ),
                )
            )
        ordered.extend(
            (
                position,
                TurnActivityEntrySnapshot(
                    kind=TurnActivityKind.SUBAGENT,
                    status=status,
                    count=count,
                ),
            )
            for status, (position, count) in subagents.items()
        )
        ordered.sort(key=lambda item: item[0])
        return tuple(item for _position, item in ordered)

    @staticmethod
    def _apply_activity_events(
        active: _ActiveTurn | _ActiveSideTurn | _ActiveGoal,
        events: tuple[TurnActivityEvent, ...],
    ) -> None:
        for event in events:
            item_id = event.item_id
            if event.kind is TurnActivityKind.COMMENTARY:
                if event.text is None:
                    continue
                active.activity_commentary[item_id] = event.text
                if item_id in active.activity_commentary_order:
                    active.activity_commentary_order.remove(item_id)
                active.activity_commentary_order.append(item_id)
                while len(active.activity_commentary_order) > ACTIVITY_COMMENTARY_LIMIT:
                    discarded = active.activity_commentary_order.pop(0)
                    active.activity_commentary.pop(discarded, None)
                continue
            active.activity_operations[item_id] = event
            if item_id in active.activity_operation_order:
                active.activity_operation_order.remove(item_id)
            active.activity_operation_order.append(item_id)
            while len(active.activity_operation_order) > ACTIVITY_OPERATION_LIMIT:
                discarded = active.activity_operation_order.pop(0)
                active.activity_operations.pop(discarded, None)

    @staticmethod
    def _reset_activity(active: _ActiveGoal) -> None:
        active.plan_cursor = 0
        active.plan_generated = False
        active.plan_steps = ()
        active.activity_commentary.clear()
        active.activity_commentary_order.clear()
        active.activity_operations.clear()
        active.activity_operation_order.clear()

    def is_compacting(self, binding_id: str) -> bool:
        return binding_id in self._compacting

    def active_goal(self, binding_id: str) -> ActiveGoalSnapshot | None:
        active = self._goals.get(binding_id)
        if active is None:
            return None
        return ActiveGoalSnapshot(
            binding_id=active.binding_id,
            thread_id=active.thread_id,
            logical_turn_id=(active.handle.id if active.handle is not None else None),
            owner_id=active.owner_id,
            state=active.state,
            persisted=active.persisted,
        )

    def goal_activity(
        self,
        binding_id: str,
        *,
        thread_id: str | None = None,
        logical_turn_id: str | None = None,
    ) -> GoalActivitySnapshot | None:
        """Return Goal Activity maintained by its unique stream consumer."""

        active = self._goals.get(binding_id)
        handle = active.handle if active is not None else None
        if active is None or handle is None or handle.id is None:
            return None
        if active.terminal_observed:
            return None
        if thread_id is not None and active.thread_id != thread_id:
            return None
        if logical_turn_id is not None and handle.id != logical_turn_id:
            return None
        return self._goal_activity_snapshot(active)

    def _goal_activity_snapshot(self, active: _ActiveGoal) -> GoalActivitySnapshot:
        handle = active.handle
        assert handle is not None and handle.id is not None
        return GoalActivitySnapshot(
            binding_id=active.binding_id,
            thread_id=active.thread_id,
            logical_turn_id=handle.id,
            physical_turn_id=active.activity_turn_id,
            revision=active.activity_revision,
            state=active.state,
            plan_available=active.plan_available,
            plan_generated=active.plan_generated,
            steps=active.plan_steps,
            commentary=self._activity_commentary(active),
            operations=self._activity_operations(active),
        )

    def _goal_activity_visible_state(self, active: _ActiveGoal) -> tuple[object, ...]:
        return (
            active.state,
            active.activity_turn_id,
            active.plan_available,
            active.plan_generated,
            active.plan_steps,
            self._activity_commentary(active),
            self._activity_operations(active),
        )

    def _apply_goal_activity_projection(
        self,
        active: _ActiveGoal,
        projection: TurnActivityNotificationProjection | None,
    ) -> None:
        if not active.activity_observation_enabled:
            return
        before = self._goal_activity_visible_state(active)
        if projection is None:
            active.plan_available = False
            if self._goal_activity_visible_state(active) != before:
                active.activity_revision += 1
            return
        turn_id = projection.turn_id
        if turn_id is None:
            return
        active.plan_available = True
        if projection.turn_started and turn_id != active.activity_turn_id:
            active.activity_turn_id = turn_id
            self._reset_activity(active)
        if turn_id != active.activity_turn_id:
            # A late event from an earlier physical Turn can never repopulate
            # the new Turn's Activity Module.
            return
        if projection.plan_updated:
            active.plan_steps = projection.steps
            active.plan_generated = True
        if projection.event is not None:
            self._apply_activity_events(active, (projection.event,))
        if self._goal_activity_visible_state(active) != before:
            active.activity_revision += 1

    def binding_runtime_snapshot(self, binding_id: str) -> BindingRuntimeSnapshot:
        """Return all process-local management state without consuming native data."""

        return BindingRuntimeSnapshot(
            binding_id=binding_id,
            activity_revision=self._admission_revision(binding_id),
            turn=self.active_turn(binding_id),
            goal=self.active_goal(binding_id),
            compacting=self.is_compacting(binding_id),
            lifecycle=self.lifecycle_state(binding_id),
            subscription=self.thread_subscription_snapshot(binding_id),
            context_window_usage=self.context_window_usage(binding_id),
        )

    async def goal_snapshot(self, binding: ThreadBinding) -> GoalSnapshot | None:
        control = self._goal_control
        if control is None:
            return None
        async with self._lock(binding.id):
            self._guard_no_lifecycle_locked(binding.id)
            binding = self._bindings.get(binding.id)
            if binding.native_thread_id is None:
                return None
            active = self._goals.get(binding.id)
            if (
                active is not None
                and active.state is GoalOperationState.UNKNOWN
                and active.persisted is not None
            ):
                return active.persisted
            try:
                snapshot = await control.get(binding.native_thread_id)
            except Exception as error:
                raise GoalControlError(
                    "无法读取当前 Codex Goal；本次操作未执行。"
                ) from error
            if active is not None:
                if active.state is GoalOperationState.UNKNOWN:
                    # A response-lost mutation remains unknown even if a later
                    # read is absent.  Preserve the last frozen evidence so the
                    # control surface cannot claim that no Goal exists.
                    if active.persisted is None and snapshot is not None:
                        active.persisted = snapshot
                    return active.persisted
                if (
                    active.handle is not None
                    and (
                        (
                            snapshot is None
                            and not active.terminal_observed
                        )
                        or (
                            snapshot is not None
                            and not _same_goal_generation(active, snapshot)
                        )
                    )
                ):
                    active.state = GoalOperationState.UNKNOWN
                    self.close_admission()
                    raise GoalStateUnknown(
                        "当前原生 Goal generation 已变化；服务已关闭 admission。"
                    )
                if active.terminal_observed and snapshot is None:
                    return active.persisted
                active.persisted = snapshot
            if snapshot is not None and snapshot.status is GoalStatus.ACTIVE:
                if active is None:
                    self._track_external_goal(binding, snapshot)
                elif active.state is GoalOperationState.EXTERNAL_ACTIVE:
                    active.persisted = snapshot
            return snapshot

    async def start_goal(
        self,
        *,
        binding: ThreadBinding,
        cwd: Path,
        objective: str,
        owner_id: str,
        origin: object,
    ) -> GoalSubmission:
        control = self._require_goal_control()
        objective = objective.strip()
        if not objective or len(objective) > 4_000:
            raise ValueError("Goal objective 必须为 1 到 4000 个字符。")
        if not self._accepting:
            raise RuntimeClosed("服务正在停止，暂不接受新 Goal。")

        async with self._lock(binding.id):
            if not self._accepting:
                raise RuntimeClosed("服务正在停止，暂不接受新 Goal。")
            self._guard_no_lifecycle_locked(binding.id)
            if binding.id in self._compacting:
                raise ThreadCompacting("当前会话正在压缩上下文，不能启动 Goal。")
            if self._active.get(binding.id) is not None:
                raise ThreadRunningConfiguration(
                    "当前 Turn 正在执行，不能启动 Goal；请等待完成或先发送 /stop。"
                )
            existing = self._goals.get(binding.id)
            if existing is not None:
                raise self._goal_slot_error(existing)
            binding = self._bindings.get(binding.id)
            thread = await self._open_goal_thread(binding=binding, cwd=cwd)
            await self._validate_goal_thread_ready(thread)
            persisted = await control.get(thread.id)
            if persisted is not None and persisted.status is GoalStatus.ACTIVE:
                self._track_external_goal(binding, persisted, thread=thread)
                raise ExternalGoalActive(
                    "当前原生 Thread 已有外部 active Goal；不能启动第二个 Goal。"
                )
            receipt_attempted = asyncio.Event()
            active = _ActiveGoal(
                binding_id=binding.id,
                thread_id=thread.id,
                thread=thread,
                handle=None,
                owner_id=owner_id,
                origin=origin,
                objective=objective,
                receipt_attempted=receipt_attempted,
                state=GoalOperationState.STARTING,
                persisted=persisted,
                task_feedback=binding.task_feedback,
                feedback_revision=binding.feedback_revision,
                plan_available=True,
            )
            self._goals[binding.id] = active
            self._advance_admission_revision(binding.id)
            self._invalidate_context_window_usage(binding.id)

        try:
            handle = await control.start(thread.id, objective)
        except asyncio.CancelledError as error:
            receipt_attempted.set()
            self.close_admission()
            active.handle = getattr(error, "goal_handle", None)
            active.state = GoalOperationState.UNKNOWN
            raise
        except Exception as error:
            receipt_attempted.set()
            self.close_admission()
            active.state = GoalOperationState.UNKNOWN
            raise GoalStateUnknown(
                "Codex Goal 启动结果未确认；该会话已保留占用，"
                "服务已停止接收新任务，请重启后对账。"
            ) from error
        if handle.thread_id != thread.id or not handle.id:
            receipt_attempted.set()
            self.close_admission()
            active.state = GoalOperationState.UNKNOWN
            raise GoalStateUnknown("Goal handle 与原生 Thread 不一致。")
        active.handle = handle
        try:
            started = await control.get(thread.id)
        except asyncio.CancelledError:
            receipt_attempted.set()
            self.close_admission()
            active.state = GoalOperationState.UNKNOWN
            raise
        except Exception as error:
            receipt_attempted.set()
            self.close_admission()
            active.state = GoalOperationState.UNKNOWN
            raise GoalStateUnknown(
                "Codex Goal 已启动，但 generation 无法确认；服务已关闭 admission。"
            ) from error
        if (
            started is None
            or started.thread_id != thread.id
            or started.status is not GoalStatus.ACTIVE
            or started.objective != objective
        ):
            receipt_attempted.set()
            self.close_admission()
            active.state = GoalOperationState.UNKNOWN
            raise GoalStateUnknown("Goal 启动后的 generation 无法确认。")
        active.persisted = started
        active.generation_created_at = started.created_at
        active.generation_token_budget = started.token_budget
        async with self._lock(binding.id):
            if self._goals.get(binding.id) is not active:
                self.close_admission()
                raise GoalStateUnknown("Goal 槽位在启动期间发生未知变化。")
            active.handle = handle
            active.state = GoalOperationState.RUNNING
            self._track_goal(active)
        return GoalSubmission(
            binding_id=binding.id,
            thread_id=thread.id,
            logical_turn_id=handle.id,
            release_receipt_attempt=receipt_attempted.set,
            task_feedback=active.task_feedback,
            feedback_revision=active.feedback_revision,
        )

    async def resume_goal(
        self,
        *,
        binding: ThreadBinding,
        owner_id: str,
        origin: object,
        expected_created_at: int | None = None,
    ) -> GoalSubmission:
        control = self._require_goal_control()
        if not self._accepting:
            raise RuntimeClosed("服务正在停止，暂不能恢复 Goal。")
        async with self._lock(binding.id):
            self._guard_no_lifecycle_locked(binding.id)
            if binding.id in self._compacting or self._active.get(binding.id) is not None:
                raise ThreadRunningConfiguration(
                    "当前会话有其他原生操作，不能恢复 Goal。"
                )
            existing = self._goals.get(binding.id)
            if existing is not None:
                raise self._goal_slot_error(existing)
            binding = self._bindings.get(binding.id)
            if binding.native_thread_id is None:
                raise GoalNotMaterialized("当前会话还没有原生 Codex Thread。")
            thread = await self._open_goal_thread(binding=binding, cwd=None)
            await self._validate_goal_thread_ready(thread)
            persisted = await control.get(thread.id)
            if persisted is None:
                raise GoalNotFound("当前会话没有可恢复的 Codex Goal。")
            if (
                expected_created_at is not None
                and persisted.created_at != expected_created_at
            ):
                raise GoalNotFound(
                    "Goal 已变化，本次恢复未执行；请重新发送 /goal。"
                )
            if persisted.status is not GoalStatus.PAUSED:
                raise ThreadGoalActive(
                    f"当前 Goal 状态为 {persisted.status.value}，只有 paused 可恢复。"
                )
            receipt_attempted = asyncio.Event()
            active = _ActiveGoal(
                binding_id=binding.id,
                thread_id=thread.id,
                thread=thread,
                handle=None,
                owner_id=owner_id,
                origin=origin,
                objective=persisted.objective,
                receipt_attempted=receipt_attempted,
                state=GoalOperationState.STARTING,
                persisted=persisted,
                generation_created_at=persisted.created_at,
                generation_token_budget=persisted.token_budget,
                task_feedback=binding.task_feedback,
                feedback_revision=binding.feedback_revision,
                plan_available=True,
            )
            self._goals[binding.id] = active
            self._advance_admission_revision(binding.id)
            self._invalidate_context_window_usage(binding.id)
        try:
            handle = await control.resume(thread.id)
        except asyncio.CancelledError as error:
            receipt_attempted.set()
            self.close_admission()
            active.handle = getattr(error, "goal_handle", None)
            active.state = GoalOperationState.UNKNOWN
            raise
        except GoalMutationStateUnknown as error:
            receipt_attempted.set()
            self.close_admission()
            active.handle = error.handle
            active.state = GoalOperationState.UNKNOWN
            raise GoalStateUnknown(
                "Codex Goal 恢复结果未确认；不能自动重试，服务已关闭 admission。"
            ) from error
        except Exception as error:
            receipt_attempted.set()
            self.close_admission()
            active.state = GoalOperationState.UNKNOWN
            raise GoalStateUnknown("Codex Goal 恢复结果未确认。") from error
        if handle.thread_id != thread.id or not handle.id:
            receipt_attempted.set()
            self.close_admission()
            active.state = GoalOperationState.UNKNOWN
            raise GoalStateUnknown("恢复后的 Goal handle 与原生 Thread 不一致。")
        active.handle = handle
        try:
            resumed = await control.get(thread.id)
        except asyncio.CancelledError:
            receipt_attempted.set()
            self.close_admission()
            active.state = GoalOperationState.UNKNOWN
            raise
        except Exception as error:
            receipt_attempted.set()
            self.close_admission()
            active.state = GoalOperationState.UNKNOWN
            raise GoalStateUnknown(
                "Codex Goal 已恢复，但 generation 无法确认；服务已关闭 admission。"
            ) from error
        if (
            resumed is None
            or resumed.thread_id != thread.id
            or resumed.status is not GoalStatus.ACTIVE
            or not _same_goal_generation(active, resumed)
        ):
            receipt_attempted.set()
            self.close_admission()
            active.state = GoalOperationState.UNKNOWN
            raise GoalStateUnknown("Goal 恢复后的 generation 无法确认。")
        active.persisted = resumed
        async with self._lock(binding.id):
            if self._goals.get(binding.id) is not active:
                self.close_admission()
                raise GoalStateUnknown("Goal 槽位在恢复期间发生未知变化。")
            active.handle = handle
            active.state = GoalOperationState.RUNNING
            self._track_goal(active)
        return GoalSubmission(
            binding_id=binding.id,
            thread_id=thread.id,
            logical_turn_id=handle.id,
            release_receipt_attempt=receipt_attempted.set,
            task_feedback=active.task_feedback,
            feedback_revision=active.feedback_revision,
        )

    async def clear_goal(
        self,
        binding: ThreadBinding,
        *,
        expected_created_at: int | None = None,
    ) -> bool:
        control = self._require_goal_control()
        async with self._lock(binding.id):
            self._guard_no_lifecycle_locked(binding.id)
            if binding.id in self._compacting or self._active.get(binding.id) is not None:
                raise ThreadRunningConfiguration(
                    "当前会话有其他原生操作，不能清除 Goal。"
                )
            binding = self._bindings.get(binding.id)
            if binding.native_thread_id is None:
                return False
            active = self._goals.get(binding.id)
            if (
                active is not None
                and active.state is not GoalOperationState.EXTERNAL_ACTIVE
            ):
                raise self._goal_slot_error(active)
            persisted = await control.get(binding.native_thread_id)
            if persisted is None:
                external = active
                if external is not None and external.state is GoalOperationState.EXTERNAL_ACTIVE:
                    self._goals.pop(binding.id, None)
                    self._advance_admission_revision(binding.id)
                    self._schedule_known_subscription_locked(
                        binding.id,
                        binding.native_thread_id,
                    )
                return False
            if (
                expected_created_at is not None
                and persisted.created_at != expected_created_at
            ):
                raise GoalNotFound(
                    "Goal 已变化，本次结束未执行；请重新发送 /goal。"
                )
            if persisted.status is GoalStatus.ACTIVE:
                if binding.id not in self._goals:
                    self._track_external_goal(binding, persisted)
                raise ThreadGoalActive("Goal 仍在 active；请先使用 /goal pause 或 /stop。")
            active = self._goals.get(binding.id)
            if active is not None and active.state is not GoalOperationState.EXTERNAL_ACTIVE:
                raise self._goal_slot_error(active)
            thread = await self._open_goal_thread(binding=binding, cwd=None)
            await self._validate_goal_thread_ready(thread)
            sentinel = _ActiveGoal(
                binding_id=binding.id,
                thread_id=thread.id,
                thread=thread,
                handle=None,
                owner_id="",
                origin=object(),
                objective=persisted.objective,
                receipt_attempted=asyncio.Event(),
                state=GoalOperationState.STARTING,
                persisted=persisted,
                generation_created_at=persisted.created_at,
                generation_token_budget=persisted.token_budget,
            )
            self._goals[binding.id] = sentinel
            self._advance_admission_revision(binding.id)
            try:
                cleared = await control.clear(thread.id)
                confirmed = await control.get(thread.id)
            except asyncio.CancelledError:
                sentinel.state = GoalOperationState.UNKNOWN
                self.close_admission()
                raise
            except Exception as error:
                sentinel.state = GoalOperationState.UNKNOWN
                self.close_admission()
                raise GoalStateUnknown(
                    "Codex Goal 清除结果未确认；服务已关闭 admission。"
                ) from error
            if not cleared or confirmed is not None:
                sentinel.state = GoalOperationState.UNKNOWN
                self.close_admission()
                raise GoalStateUnknown("Codex Goal 清除后状态无法确认。")
            self._goals.pop(binding.id, None)
            self._advance_admission_revision(binding.id)
            self._schedule_known_subscription_locked(binding.id, thread.id)
            return True

    async def _open_goal_thread(
        self,
        *,
        binding: ThreadBinding,
        cwd: Path | None,
    ) -> NativeThread:
        try:
            if binding.native_thread_id is None:
                if cwd is None:
                    raise GoalNotMaterialized(
                        "当前会话还没有原生 Codex Thread。"
                    )
                thread = await self._codex.thread_start(cwd=str(cwd))
                self._bindings.assign_native_thread_id(binding.id, thread.id)
            else:
                thread = await self._codex.thread_resume(binding.native_thread_id)
                if thread.id != binding.native_thread_id:
                    raise RuntimeError("thread_resume returned a different native ID")
            current = self._bindings.get(binding.id)
            record = self._mark_thread_subscribed_locked(current, thread)
            self._schedule_subscription_release_locked(record)
            return thread
        except GoalNotMaterialized:
            raise
        except asyncio.CancelledError:
            self.close_admission()
            raise
        except Exception as error:
            self.close_admission()
            raise GoalStateUnknown(
                "Codex Thread 启动或恢复结果未确认；服务已关闭 admission。"
            ) from error

    async def _validate_goal_thread_ready(self, thread: NativeThread) -> None:
        try:
            response = await thread.read(include_turns=False)
        except InvalidRequestError as error:
            if error.code == -32600 and _NOT_MATERIALIZED_SUFFIX in error.message:
                raise GoalNotMaterialized(
                    "当前 SDK 尚不能在零 Turn 的新 Thread 上启动 Goal；"
                    "该能力未通过 live persistence 门禁。"
                ) from error
            raise GoalControlError("无法读取 Goal 对应的原生 Thread。") from error
        except Exception as error:
            raise GoalControlError("无法读取 Goal 对应的原生 Thread。") from error
        native = getattr(response, "thread", None)
        status = _thread_status_type(native)
        if status != "idle":
            raise ThreadRunningConfiguration(
                f"原生 Thread 状态为 {status!r}，不能修改 Goal。"
            )
        if getattr(native, "ephemeral", None) is not False:
            raise GoalNotMaterialized("Goal 只允许使用已持久化的原生 Thread。")
        path = getattr(native, "path", None)
        if not isinstance(path, str) or not path:
            raise GoalNotMaterialized(
                "原生 Thread 尚未持久化 path，当前不能启动或恢复 Goal。"
            )

    async def _guard_no_goal_locked(self, binding: ThreadBinding) -> None:
        active = self._goals.get(binding.id)
        if active is not None:
            raise self._goal_slot_error(active)
        control = self._goal_control
        if control is None or binding.native_thread_id is None:
            return
        try:
            persisted = await control.get(binding.native_thread_id)
        except Exception as error:
            raise GoalControlError(
                "无法确认当前 Thread 是否存在 active Goal；本条消息未执行。"
            ) from error
        if persisted is not None and persisted.status is GoalStatus.ACTIVE:
            self._track_external_goal(binding, persisted)
            raise ExternalGoalActive(
                "当前 Thread 有一个由其他 Codex 客户端继续执行的 active Goal；"
                "本条普通消息未执行。"
            )

    def _track_external_goal(
        self,
        binding: ThreadBinding,
        persisted: GoalSnapshot,
        *,
        thread: NativeThread | None = None,
    ) -> None:
        if binding.id in self._goals:
            return
        receipt_attempted = asyncio.Event()
        receipt_attempted.set()
        self._goals[binding.id] = _ActiveGoal(
            binding_id=binding.id,
            thread_id=persisted.thread_id,
            thread=thread,
            handle=None,
            owner_id="",
            origin=object(),
            objective=persisted.objective,
            receipt_attempted=receipt_attempted,
            state=GoalOperationState.EXTERNAL_ACTIVE,
            persisted=persisted,
            generation_created_at=persisted.created_at,
            generation_token_budget=persisted.token_budget,
        )
        self._advance_admission_revision(binding.id)
        self._invalidate_context_window_usage(binding.id)

    def _goal_slot_error(self, active: _ActiveGoal) -> RuntimeError:
        if active.state is GoalOperationState.EXTERNAL_ACTIVE:
            return ExternalGoalActive(
                "当前 Thread 有重启前或外部客户端启动的 active Goal；"
                "本服务不会猜测重挂，请先在原生 Codex 中暂停，再查看或清除。"
            )
        if active.state in {GoalOperationState.STARTING, GoalOperationState.RUNNING}:
            return ThreadGoalActive(
                "当前 Goal 正在执行；普通 Prompt、/compact 和 /config 暂不可用。"
            )
        if active.state is GoalOperationState.PAUSING:
            return ThreadGoalActive("当前 Goal 正在暂停，完成前暂不接受新任务。")
        return GoalStateUnknown(
            "当前 Goal 状态未确认；该会话保持占用，不能开始新的原生操作。"
        )

    def _guard_no_lifecycle_locked(self, binding_id: str) -> None:
        active = self._lifecycles.get(binding_id)
        if active is None:
            return
        if active.state is ThreadLifecycleState.UNKNOWN:
            raise ThreadLifecycleStateUnknown(
                "当前会话生命周期状态未确认；请重启服务后对账。"
            )
        labels = {
            ThreadLifecycleState.RENAMING: "重命名",
            ThreadLifecycleState.ARCHIVING: "归档",
            ThreadLifecycleState.UNARCHIVING: "恢复",
            ThreadLifecycleState.DELETING: "删除",
        }
        raise ThreadLifecycleError(
            f"当前会话正在{labels[active.state]}，完成前不能执行其他操作。"
        )

    def _begin_lifecycle_locked(
        self,
        binding: ThreadBinding,
        state: ThreadLifecycleState,
    ) -> _ActiveThreadLifecycle:
        self._guard_no_lifecycle_locked(binding.id)
        active = _ActiveThreadLifecycle(
            binding_id=binding.id,
            thread_id=binding.native_thread_id,
            state=state,
        )
        self._lifecycles[binding.id] = active
        if state in {ThreadLifecycleState.ARCHIVING, ThreadLifecycleState.DELETING}:
            running = self._active.get(binding.id)
            if running is not None:
                running.activity_observation_enabled = False
            goal = self._goals.get(binding.id)
            if goal is not None:
                goal.activity_observation_enabled = False
        self._advance_admission_revision(binding.id)
        return active

    async def _release_lifecycle_reservation(
        self,
        operation: _ActiveThreadLifecycle,
    ) -> None:
        async with self._lock(operation.binding_id):
            if self._lifecycles.get(operation.binding_id) is operation:
                self._finish_lifecycle_locked(operation)

    async def _mark_lifecycle_unknown(
        self,
        operation: _ActiveThreadLifecycle,
    ) -> None:
        async with self._lock(operation.binding_id):
            self._retain_unknown_lifecycle(operation)

    def _require_reserved_lifecycle_binding_locked(
        self,
        operation: _ActiveThreadLifecycle,
    ) -> ThreadBinding:
        if self._lifecycles.get(operation.binding_id) is not operation:
            raise RuntimeError(
                "native Thread lifecycle reservation changed"
            )
        binding = self._bindings.get(operation.binding_id)
        if binding.native_thread_id != operation.thread_id:
            raise RuntimeError(
                "native Thread lifecycle identity changed"
            )
        return binding

    def _finish_lifecycle_locked(
        self,
        active: _ActiveThreadLifecycle,
    ) -> None:
        if self._lifecycles.get(active.binding_id) is not active:
            raise RuntimeError(
                "native Thread lifecycle ownership changed"
            )
        self._lifecycles.pop(active.binding_id, None)
        running = self._active.get(active.binding_id)
        if running is not None:
            running.activity_observation_enabled = True
        goal = self._goals.get(active.binding_id)
        if goal is not None:
            goal.activity_observation_enabled = True
        self._advance_admission_revision(active.binding_id)

    def _retain_unknown_lifecycle(
        self,
        active: _ActiveThreadLifecycle,
    ) -> None:
        if self._lifecycles.get(active.binding_id) is active:
            if active.state is not ThreadLifecycleState.UNKNOWN:
                active.state = ThreadLifecycleState.UNKNOWN
                self._advance_admission_revision(active.binding_id)

    def _discard_local_thread_activity_locked(
        self,
        binding_id: str,
        thread_id: str,
    ) -> ThreadActivityDiscardedOutcome:
        """Forget local execution state after native archive/delete is confirmed."""

        changed = False
        turn_id: str | None = None
        active = self._active.pop(binding_id, None)
        if active is not None:
            turn_id = active.handle.id
            active.receipt_attempted.set()
            active.cleanup_ready.set()
            if active.task is not None and not active.task.done():
                active.task.cancel()
            changed = True

        compaction = self._compacting.pop(binding_id, None)
        if compaction is not None:
            compaction.receipt_attempted.set()
            if compaction.task is not None and not compaction.task.done():
                compaction.task.cancel()
            changed = True

        goal = self._goals.pop(binding_id, None)
        if goal is not None:
            goal.receipt_attempted.set()
            goal.cleanup_ready.set()
            if goal.task is not None and not goal.task.done():
                goal.task.cancel()
            if goal.handle is not None:
                close_task = asyncio.create_task(
                    self._close_discarded_goal_handle(goal.handle),
                    name=f"codex-goal-close:{goal.handle.id}",
                )
                self._tasks.add(close_task)
                close_task.add_done_callback(self._tasks.discard)
            changed = True

        record = self._subscriptions.pop(binding_id, None)
        if record is not None:
            self._cancel_subscription_idle(record)
        self._invalidate_context_window_usage(binding_id)
        if changed:
            self._advance_admission_revision(binding_id)
        return ThreadActivityDiscardedOutcome(
            binding_id,
            thread_id,
            turn_id,
        )

    async def _deliver_thread_activity_discarded(
        self,
        outcome: ThreadActivityDiscardedOutcome,
    ) -> None:
        try:
            await self._on_completion(outcome)
        except Exception:
            logger.exception(
                "Thread activity discard presentation cleanup failed",
                extra={
                    "binding_id": outcome.binding_id,
                    "thread_id": outcome.thread_id,
                },
            )

    @staticmethod
    async def _close_discarded_goal_handle(handle: GoalHandle) -> None:
        try:
            await handle.aclose()
        except Exception:
            logger.debug(
                "failed to close Goal observer after native Thread removal",
                exc_info=True,
            )

    async def _lifecycle_thread_locked(
        self,
        binding: ThreadBinding,
    ) -> NativeThread:
        thread_id = binding.native_thread_id
        if thread_id is None:
            raise ThreadNotMaterialized("当前会话尚未创建原生 Codex Thread。")
        active_turn = self._active.get(binding.id)
        if active_turn is not None:
            thread = active_turn.thread
        elif (compaction := self._compacting.get(binding.id)) is not None:
            thread = compaction.thread
        elif (
            (goal := self._goals.get(binding.id)) is not None
            and goal.thread is not None
        ):
            thread = goal.thread
        else:
            try:
                thread = await self._codex.thread_resume(thread_id)
            except BaseException:
                self._schedule_known_subscription_locked(binding.id, thread_id)
                raise
        if thread.id != thread_id:
            self.close_admission()
            raise RuntimeError("native lifecycle Thread identity mismatch")
        self._mark_thread_subscribed_locked(binding, thread)
        return thread

    def _require_goal_control(self) -> GoalControl:
        if self._goal_control is None:
            raise GoalControlError("当前原生 Goal 兼容能力不可用。")
        return self._goal_control

    async def compact(
        self,
        *,
        binding: ThreadBinding,
        owner_id: str,
        origin: object,
    ) -> CompactSubmission:
        """Start native compaction and observe its exact terminal Turn.

        The pinned public ``compact()`` method returns immediately.  Keep the
        Binding reserved until public ``thread.read(include_turns=True)``
        exposes a new terminal Turn containing a ``contextCompaction`` item.
        """

        if not self._accepting:
            raise RuntimeClosed("服务正在停止，暂不能压缩会话。")
        async with self._lock(binding.id):
            if not self._accepting:
                raise RuntimeClosed("服务正在停止，暂不能压缩会话。")
            self._guard_no_lifecycle_locked(binding.id)
            if binding.id in self._compacting:
                raise ThreadCompacting("当前会话已经在压缩上下文。")
            binding = self._bindings.get(binding.id)
            await self._guard_no_goal_locked(binding)
            if self._active.get(binding.id) is not None:
                raise ThreadRunningConfiguration(
                    "当前 Turn 正在执行，不能压缩上下文；"
                    "请等待完成或先发送 /stop。"
                )

            binding = self._bindings.get(binding.id)
            if binding.native_thread_id is None:
                raise ThreadNotMaterialized(
                    "当前会话尚未创建原生 Codex Thread；"
                    "请先发送一条真实任务，再使用 /compact。"
                )
            try:
                thread = await self._codex.thread_resume(binding.native_thread_id)
                if thread.id != binding.native_thread_id:
                    raise RuntimeError(
                        "thread_resume returned a different native ID"
                    )
                record = self._mark_thread_subscribed_locked(binding, thread)
                self._schedule_subscription_release_locked(record)
            except asyncio.CancelledError:
                self.close_admission()
                raise
            except Exception as error:
                self.close_admission()
                raise ThreadCompactStartFailed(
                    "Codex Thread 恢复结果未确认；"
                    "服务已停止接收新任务，请重启服务。"
                ) from error

            try:
                baseline = await self._read_compaction_baseline(thread)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                raise ThreadCompactStartFailed(
                    "无法读取原生 Codex Thread，未开始压缩；请重试。"
                ) from error
            status = _thread_status_type(baseline)
            if status != "idle":
                if status == "active":
                    raise ThreadRunningConfiguration(
                        "原生 Codex Thread 当前正在执行其他任务，不能压缩；"
                        "请等待其完成。"
                    )
                raise ThreadCompactStartFailed(
                    f"原生 Codex Thread 状态异常：{status!r}，未开始压缩。"
                )
            before_turn_ids = frozenset(
                str(turn_id)
                for turn in getattr(baseline, "turns", ())
                if (turn_id := getattr(turn, "id", None))
            )
            if not before_turn_ids:
                raise ThreadNotMaterialized("当前会话还没有可压缩的原生 Turn。")

            receipt_attempted = asyncio.Event()
            active = _ActiveCompaction(
                binding_id=binding.id,
                thread=thread,
                owner_id=owner_id,
                origin=origin,
                before_turn_ids=before_turn_ids,
                receipt_attempted=receipt_attempted,
            )
            self._advance_admission_revision(binding.id)
            self._compacting[binding.id] = active
            self._invalidate_context_window_usage(binding.id)
            try:
                await thread.compact()
            except asyncio.CancelledError:
                receipt_attempted.set()
                self.close_admission()
                raise
            except Exception as error:
                # The request is side-effectful and its response is only an
                # acknowledgement.  A lost response cannot prove compaction
                # did not start, so retain the Binding reservation and fail
                # the process-wide admission boundary closed.
                receipt_attempted.set()
                self.close_admission()
                raise ThreadCompactStartFailed(
                    "Codex 压缩请求结果未确认；服务已停止接收新任务，"
                    "请重启服务。"
                ) from error
            self._track_compaction(active)
            return CompactSubmission(
                binding_id=binding.id,
                thread_id=thread.id,
                release_receipt_attempt=receipt_attempted.set,
            )

    async def stop(
        self,
        binding_id: str,
        *,
        acknowledge: StopAcknowledger | None = None,
        expected_activity_revision: int | None = None,
        expected_turn_id: str | None = None,
    ) -> StopDisposition:
        async with self._lock(binding_id):
            self._guard_no_lifecycle_locked(binding_id)
            if expected_activity_revision is not None:
                self._require_turn_activity_precondition_locked(
                    binding_id,
                    expected_activity_revision=expected_activity_revision,
                    expected_turn_id=expected_turn_id,
                )
            # A stop attempt also cancels the validity of prompts still doing
            # bounded preparation outside this lock. Otherwise an idle /stop
            # could report NOT_RUNNING and a delayed quoted prompt could start
            # immediately afterwards.
            self._advance_admission_revision(binding_id)
            if binding_id in self._compacting:
                return StopDisposition.COMPACTING
            goal = self._goals.get(binding_id)
            if goal is not None:
                if goal.state is GoalOperationState.EXTERNAL_ACTIVE:
                    return StopDisposition.EXTERNAL_GOAL
                return await self._stop_goal_locked(goal, acknowledge=acknowledge)
            active = self._active.get(binding_id)
            if active is None:
                return StopDisposition.NOT_RUNNING
            if active.terminal_observed and active.state is ActiveState.RUNNING:
                return StopDisposition.NOT_RUNNING
            if active.state is ActiveState.RUNNING:
                self._mark_stopping(active)
            if acknowledge is not None:
                try:
                    async with asyncio.timeout(_STOP_ACK_ATTEMPT_TIMEOUT_SECONDS):
                        await acknowledge()
                except TimeoutError:
                    logger.warning(
                        "stop acknowledgement timed out; continuing native stop",
                        extra={"turn_id": active.handle.id},
                    )
                except Exception:
                    logger.warning(
                        "stop acknowledgement failed; continuing native stop",
                        exc_info=True,
                        extra={"turn_id": active.handle.id},
                    )
            if active.terminal_observed and not active.interrupt_attempted:
                active.state = ActiveState.RUNNING
                active.activity_revision += 1
                return StopDisposition.NOT_RUNNING
            try:
                return await self._interrupt_and_clean(active)
            finally:
                if (
                    self._active.get(binding_id) is active
                    and not active.terminal_observed
                    and (active.task is None or active.task.done())
                ):
                    self._start_turn_consumer(active, recover_first=True)

    async def stop_exact(
        self,
        binding_id: str,
        *,
        acknowledge: StopAcknowledger | None = None,
        expected_activity_revision: int | None = None,
        expected_turn_id: str | None = None,
    ) -> StopDisposition:
        """Management-port name for the exact ordinary Binding stop primitive."""

        return await self.stop(
            binding_id,
            acknowledge=acknowledge,
            expected_activity_revision=expected_activity_revision,
            expected_turn_id=expected_turn_id,
        )

    async def recheck_turn_exact(
        self,
        binding_id: str,
        *,
        expected_activity_revision: int,
        expected_turn_id: str,
    ) -> ActiveTurnSnapshot:
        """Run one new bounded observation attempt for one exact Turn."""

        async with self._lock(binding_id):
            self._guard_no_lifecycle_locked(binding_id)
            self._require_turn_activity_precondition_locked(
                binding_id,
                expected_activity_revision=expected_activity_revision,
                expected_turn_id=expected_turn_id,
            )
            active = self._active.get(binding_id)
            if (
                active is None
                or active.state is not ActiveState.OBSERVATION_UNAVAILABLE
            ):
                raise ThreadActivityChanged(
                    "当前 Turn 已不再处于观测不可用状态；请刷新 /sessions。"
                )
            if active.task is not None and not active.task.done():
                raise ThreadActivityChanged(
                    "当前 Turn 正在重新检查；请稍后刷新 /sessions。"
                )
            self._start_turn_consumer(active, recover_first=True)
            return ActiveTurnSnapshot(
                binding_id=binding_id,
                thread_id=active.handle.thread_id,
                turn_id=active.handle.id,
                owner_id=active.owner_id,
                state=active.state,
            )

    async def _stop_goal_locked(
        self,
        active: _ActiveGoal,
        *,
        acknowledge: StopAcknowledger | None,
    ) -> StopDisposition:
        if active.state is GoalOperationState.UNKNOWN:
            raise GoalStateUnknown(
                "Goal 状态未确认；/stop 不会再次发起原生 mutation。"
            )
        handle = active.handle
        if handle is None:
            return StopDisposition.GOAL_STOPPING
        if active.terminal_observed:
            return StopDisposition.NOT_RUNNING
        if active.state is GoalOperationState.RUNNING:
            active.state = GoalOperationState.PAUSING
            self._advance_admission_revision(active.binding_id)
        if acknowledge is not None:
            try:
                async with asyncio.timeout(_STOP_ACK_ATTEMPT_TIMEOUT_SECONDS):
                    await acknowledge()
            except TimeoutError:
                logger.warning(
                    "Goal stop acknowledgement timed out; continuing native pause",
                    extra={"logical_turn_id": handle.id},
                )
            except Exception:
                logger.warning(
                    "Goal stop acknowledgement failed; continuing native pause",
                    exc_info=True,
                    extra={"logical_turn_id": handle.id},
                )
        if active.cleanup_succeeded:
            return StopDisposition.GOAL_STOPPING
        return await self._pause_and_clean_goal(active)

    async def _pause_and_clean_goal(
        self,
        active: _ActiveGoal,
    ) -> StopDisposition:
        handle = active.handle
        if handle is None:
            active.state = GoalOperationState.UNKNOWN
            raise GoalStateUnknown("Goal handle 缺失，无法暂停。")
        active.state = GoalOperationState.PAUSING
        active.pause_attempted = True
        active.cleanup_required = True
        active.cleanup_ready.clear()
        try:
            ack = await handle.pause()
            active.persisted = ack.goal
            active.interrupt_acknowledged = (
                ack.interrupt_acknowledged or ack.physical_turn_id is None
            )
        except asyncio.CancelledError:
            active.state = GoalOperationState.UNKNOWN
            active.cleanup_ready.set()
            self.close_admission()
            raise
        except GoalMutationStateUnknown as error:
            active.handle = error.handle or handle
            active.state = GoalOperationState.UNKNOWN
            active.cleanup_ready.set()
            self.close_admission()
            raise GoalStateUnknown(
                "Goal 暂停或物理 Turn 中断结果未确认；"
                "服务已关闭 admission，不能自动重试。"
            ) from error
        except Exception as error:
            active.state = GoalOperationState.UNKNOWN
            active.cleanup_ready.set()
            self.close_admission()
            raise GoalStateUnknown("Goal 暂停结果未确认。") from error
        try:
            await self._terminal_cleanup.clean_thread(active.thread_id)
        except Exception as error:
            active.cleanup_ready.set()
            raise TerminalCleanupFailed(
                "Goal 已暂停，但已登记后台终端的清理请求失败；"
                "前台工具进程也可能仍在运行。请再次发送 /stop 重试。"
            ) from error
        active.cleanup_succeeded = True
        active.cleanup_ready.set()
        return StopDisposition.GOAL_REQUESTED

    def close_admission(self) -> None:
        self._accepting = False
        for record in self._subscriptions.values():
            self._cancel_subscription_idle(record)

    async def interrupt_all(self) -> None:
        self.close_admission()
        await self._drain_subscription_idle_tasks()
        snapshot = tuple(self._active.items())
        goal_snapshot = tuple(self._goals.items())
        side_snapshot = tuple(self._sides.values())
        for active in self._compacting.values():
            active.receipt_attempted.set()
        for _binding_id, active in snapshot:
            active.receipt_attempted.set()
        for _binding_id, active in goal_snapshot:
            active.receipt_attempted.set()
        for session in side_snapshot:
            if session.active is not None:
                session.active.receipt_attempted.set()
        main_operations = [
            self._interrupt(binding_id, active)
            for binding_id, active in snapshot
        ] + [
            self._interrupt_goal(binding_id, active)
            for binding_id, active in goal_snapshot
        ]
        side_operations = [
                self.close_side(
                    session.side_id,
                    state=SideTopicState.EXPIRED,
                )
                for session in side_snapshot
        ]
        combined_results = await asyncio.gather(
            *main_operations,
            *side_operations,
            return_exceptions=True,
        )
        results = combined_results[: len(main_operations)]
        side_results = combined_results[len(main_operations) :]
        labels: list[tuple[str, str | None, str | None]] = [
            (active.handle.thread_id, active.handle.id, None)
            for _binding_id, active in snapshot
        ] + [
            (
                active.thread_id,
                None,
                active.handle.id if active.handle is not None else None,
            )
            for _binding_id, active in goal_snapshot
        ]
        for (thread_id, turn_id, logical_turn_id), result in zip(
            labels,
            results,
            strict=True,
        ):
            if isinstance(result, BaseException):
                logger.error(
                    "native operation shutdown cleanup failed",
                    exc_info=(type(result), result, result.__traceback__),
                    extra={
                        "thread_id": thread_id,
                        "turn_id": turn_id,
                        "logical_turn_id": logical_turn_id,
                    },
                )
        for session, result in zip(side_snapshot, side_results, strict=True):
            if isinstance(result, BaseException):
                logger.error(
                    "Side shutdown cleanup failed",
                    exc_info=(type(result), result, result.__traceback__),
                    extra={
                        "side_id": session.side_id,
                        "thread_id": session.thread.id,
                    },
                )
        all_results = [*results, *side_results]
        errors = [result for result in all_results if isinstance(result, Exception)]
        if errors:
            raise ExceptionGroup("one or more native Turn cleanups failed", errors)
        cancellation = next(
            (
                result
                for result in all_results
                if isinstance(result, BaseException)
            ),
            None,
        )
        if cancellation is not None:
            raise cancellation

    async def wait_idle(self, timeout: float | None = None) -> bool:
        deadline = (
            asyncio.get_running_loop().time() + timeout
            if timeout is not None
            else None
        )
        while self._tasks:
            tasks = tuple(self._tasks)
            if deadline is None:
                await asyncio.gather(*tasks, return_exceptions=True)
                self._tasks.difference_update(
                    task for task in tasks if task.done()
                )
                continue
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            done, pending = await asyncio.wait(tasks, timeout=remaining)
            self._tasks.difference_update(done)
            if pending:
                return False
        return True

    async def cancel_tasks(self) -> None:
        tasks = tuple(self._tasks)
        side_idle_tasks = tuple(self._side_idle_tasks)
        subscription_idle_tasks = tuple(self._subscription_idle_tasks)
        # Final service teardown owns every remaining live or poisoned slot,
        # including a consumer that already ended after an unknown terminal.
        for binding_id in tuple(self._active):
            self._advance_admission_revision(binding_id)
        for binding_id in tuple(self._compacting):
            self._advance_admission_revision(binding_id)
        for binding_id in tuple(self._goals):
            self._advance_admission_revision(binding_id)
        for binding_id in tuple(self._lifecycles):
            self._advance_admission_revision(binding_id)
        self._active.clear()
        self._compacting.clear()
        self._lifecycles.clear()
        for record in self._subscriptions.values():
            self._cancel_subscription_idle(record)
        self._subscriptions.clear()
        for session in self._sides.values():
            self._cancel_side_idle(session)
            if session.active is not None:
                session.active.receipt_attempted.set()
        self._sides.clear()
        goal_handles = tuple(
            active.handle
            for active in self._goals.values()
            if active.handle is not None
        )
        self._goals.clear()
        for handle in goal_handles:
            try:
                await handle.aclose()
            except Exception:
                logger.debug("failed to close Goal stream during final teardown")
        for task in tasks:
            task.cancel()
        for task in side_idle_tasks:
            task.cancel()
        for task in subscription_idle_tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if side_idle_tasks:
            await asyncio.gather(*side_idle_tasks, return_exceptions=True)
        if subscription_idle_tasks:
            await asyncio.gather(*subscription_idle_tasks, return_exceptions=True)
        self._side_idle_tasks.difference_update(side_idle_tasks)
        self._subscription_idle_tasks.difference_update(subscription_idle_tasks)

    async def _drain_subscription_idle_tasks(self) -> None:
        tasks = tuple(self._subscription_idle_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._subscription_idle_tasks.difference_update(tasks)

    def _side_snapshot(self, session: _SideSession) -> SideSessionSnapshot:
        active = session.active
        return SideSessionSnapshot(
            side_id=session.side_id,
            parent_binding_id=session.parent_binding_id,
            parent_thread_id=session.parent_thread_id,
            thread_id=session.thread.id,
            project_alias=session.project_alias,
            cwd=session.cwd,
            creator_id=session.creator_id,
            state=session.state,
            topic_id=session.topic_id,
            root_message_id=session.root_message_id,
            turn_id=active.handle.id if active is not None else None,
            turn_state=active.state if active is not None else None,
            last_activity=session.last_activity,
        )

    def _require_side(self, side_id: str) -> _SideSession:
        session = self._sides.get(side_id)
        if session is None:
            raise SideSessionNotFound(side_id)
        return session

    def _side_lock(self, side_id: str) -> asyncio.Lock:
        lock = self._side_locks.get(side_id)
        if lock is None:
            lock = asyncio.Lock()
            self._side_locks[side_id] = lock
        return lock

    def _touch_side_topic(self, side_id: str) -> None:
        try:
            self._bindings.touch_side_topic(side_id)
        except Exception:
            logger.warning(
                "failed to update Side Topic activity timestamp",
                exc_info=True,
                extra={"side_id": side_id},
            )

    def _cancel_side_idle(self, session: _SideSession) -> None:
        task = session.idle_task
        session.idle_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _schedule_side_idle(self, session: _SideSession) -> None:
        self._cancel_side_idle(session)
        if (
            session.state is not SideSessionState.OPEN
            or session.active is not None
            or session.topic_id is None
        ):
            return
        task = asyncio.create_task(
            self._expire_side_when_idle(session),
            name=f"codex-side-idle:{session.side_id}",
        )
        session.idle_task = task
        self._side_idle_tasks.add(task)
        task.add_done_callback(self._side_idle_tasks.discard)

    async def _expire_side_when_idle(self, session: _SideSession) -> None:
        try:
            while True:
                elapsed = asyncio.get_running_loop().time() - session.last_activity
                remaining = self._side_idle_seconds - elapsed
                if remaining > 0:
                    await asyncio.sleep(remaining)
                async with self._side_lock(session.side_id):
                    if self._sides.get(session.side_id) is not session:
                        return
                    if (
                        session.state is not SideSessionState.OPEN
                        or session.active is not None
                    ):
                        return
                    elapsed = (
                        asyncio.get_running_loop().time() - session.last_activity
                    )
                    if elapsed < self._side_idle_seconds:
                        continue
                    session.idle_task = None
                    break
            await self.close_side(
                session.side_id,
                state=SideTopicState.EXPIRED,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Side idle expiry failed",
                extra={"side_id": session.side_id, "thread_id": session.thread.id},
            )

    async def _interrupt_side_turn(
        self,
        session: _SideSession,
        active: _ActiveSideTurn,
    ) -> StopDisposition:
        if active.handle.thread_id != session.thread.id:
            raise SideSessionConflict(
                "Side active Turn 与 Side Thread 标识不一致。"
            )
        active.state = ActiveState.STOPPING
        active.cleanup_required = True
        active.cleanup_ready.clear()
        if not active.interrupt_succeeded:
            retrying_unknown_interrupt = active.interrupt_attempted
            active.interrupt_attempted = True
            try:
                await active.handle.interrupt()
            except Exception as error:
                if active.terminal_observed and retrying_unknown_interrupt:
                    logger.warning(
                        "repeat Side interrupt failed after exact terminal; "
                        "continuing exact Thread cleanup",
                        exc_info=True,
                        extra={
                            "side_id": session.side_id,
                            "thread_id": session.thread.id,
                            "turn_id": active.handle.id,
                        },
                    )
                else:
                    active.cleanup_ready.set()
                    raise TurnInterruptFailed(
                        "Side Turn 中断结果未确认；可再次发送 /stop 或结束 Side。"
                    ) from error
            else:
                active.interrupt_succeeded = True
        if not active.cleanup_succeeded:
            try:
                await self._terminal_cleanup.clean_thread(session.thread.id)
            except Exception as error:
                active.cleanup_ready.set()
                raise TerminalCleanupFailed(
                    "Side Turn 已请求中断，但已登记后台终端的清理请求失败；"
                    "前台工具进程也可能仍在运行。可再次发送 /stop 重试。"
                ) from error
            active.cleanup_succeeded = True
            session.terminal_cleanup_succeeded = True
        active.cleanup_ready.set()
        return StopDisposition.REQUESTED

    async def _request_side_interrupt_for_close(
        self,
        session: _SideSession,
        active: _ActiveSideTurn,
    ) -> None:
        if active.handle.thread_id != session.thread.id:
            raise SideSessionConflict(
                "Side active Turn 与 Side Thread 标识不一致。"
            )
        if active.terminal_observed:
            return
        retrying_unknown_interrupt = active.interrupt_attempted
        active.interrupt_attempted = True
        try:
            await active.handle.interrupt()
        except Exception as error:
            if active.terminal_observed and retrying_unknown_interrupt:
                logger.warning(
                    "repeat Side close interrupt failed after exact terminal",
                    exc_info=True,
                    extra={
                        "side_id": session.side_id,
                        "thread_id": session.thread.id,
                        "turn_id": active.handle.id,
                    },
                )
                return
            raise TurnInterruptFailed(
                "Side Turn 中断结果未确认；不能继续清理或取消订阅。"
            ) from error
        active.interrupt_succeeded = True

    def _track_side(
        self,
        session: _SideSession,
        active: _ActiveSideTurn,
    ) -> None:
        task = asyncio.create_task(
            self._consume_side(session, active),
            name=f"codex-side-turn:{active.handle.id}",
        )
        active.task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _consume_side(
        self,
        session: _SideSession,
        active: _ActiveSideTurn,
    ) -> None:
        result: object | None = None
        error: BaseException | None = None
        activity: SideTurnActivitySnapshot | None = None
        try:
            # Side Threads are ephemeral. Intentionally use the normal SDK
            # handle path and do not apply persisted-thread completion recovery.
            # When Activity is enabled, only peek until exact completion is
            # queued; ``handle.run()`` remains the sole consumer and terminal
            # authority.
            if active.task_feedback.progress_card_enabled:
                while True:
                    observation = self._refresh_turn_activity(active)
                    if observation is None:
                        break
                    if observation.next_cursor >= SIDE_ACTIVITY_QUEUE_HIGH_WATER:
                        before = self._turn_activity_visible_state(active)
                        active.plan_available = False
                        active.activity_observation_enabled = False
                        if self._turn_activity_visible_state(active) != before:
                            active.activity_revision += 1
                        logger.warning(
                            "Side Turn activity queue reached the fixed high water; "
                            "falling back to the unique consumer",
                            extra={
                                "thread_id": active.handle.thread_id,
                                "turn_id": active.handle.id,
                            },
                        )
                        break
                    if observation.turn_completed:
                        break
                    await asyncio.sleep(self._poll_interval_seconds)
            result = await active.handle.run()
            active.terminal_observed = True
        except asyncio.CancelledError:
            raise
        except BaseException as caught:
            error = caught
        finally:
            if active.state is ActiveState.STOPPING and active.cleanup_required:
                while (
                    not active.cleanup_succeeded
                    and session.state is SideSessionState.OPEN
                ):
                    await active.cleanup_ready.wait()
                    if (
                        not active.cleanup_succeeded
                        and session.state is SideSessionState.OPEN
                    ):
                        active.cleanup_ready.clear()
            async with self._side_lock(session.side_id):
                if (
                    self._sides.get(session.side_id) is session
                    and session.active is active
                ):
                    if active.task_feedback.progress_card_enabled:
                        activity = self._side_turn_activity_snapshot(
                            session.side_id,
                            active,
                        )
                    cleanup_debt = (
                        active.cleanup_required and not active.cleanup_succeeded
                    )
                    if error is not None:
                        session.state = SideSessionState.CLOSING
                        session.turn_terminal_state_unknown = True
                        session.revision += 1
                        self._cancel_side_idle(session)
                        self.close_admission()
                    elif (
                        not cleanup_debt
                        and session.state is SideSessionState.OPEN
                    ):
                        session.active = None
                        session.revision += 1
                        session.last_activity = asyncio.get_running_loop().time()
                        self._touch_side_topic(session.side_id)
                        self._schedule_side_idle(session)

        await active.receipt_attempted.wait()
        outcome = SideTurnOutcome(
            side_id=session.side_id,
            parent_binding_id=session.parent_binding_id,
            thread_id=session.thread.id,
            turn_id=active.handle.id,
            owner_id=active.owner_id,
            origin=active.origin,
            cwd=session.cwd,
            result=result,
            error=error,
            background_cleanup_requested=active.cleanup_succeeded,
            task_feedback=active.task_feedback,
            feedback_revision=active.feedback_revision,
            activity=activity,
        )
        try:
            await self._on_completion(outcome)
        except Exception:
            logger.exception(
                "Side Turn completion delivery failed",
                extra={"side_id": session.side_id, "turn_id": active.handle.id},
            )

    async def _finalize_side(
        self,
        session: _SideSession,
        *,
        state: SideTopicState,
        error: BaseException | None,
    ) -> SideLifecycleOutcome:
        control = self._thread_subscription_control
        assert control is not None
        cleanup_error: BaseException | None = None
        if not session.terminal_cleanup_succeeded:
            try:
                await self._terminal_cleanup.clean_thread(session.thread.id)
            except asyncio.CancelledError:
                raise
            except Exception as caught:
                cleanup_error = caught
            else:
                session.terminal_cleanup_succeeded = True
        if cleanup_error is not None:
            combined = (
                BaseExceptionGroup(
                    "Side cleanup failed",
                    [error, cleanup_error],
                )
                if error is not None
                else cleanup_error
            )
            outcome = SideLifecycleOutcome(
                side_id=session.side_id,
                state=SideTopicState.OPEN,
                error=combined,
            )
            await self._deliver_side_lifecycle(outcome)
            return outcome

        unsubscribe_error: BaseException | None = None
        try:
            await control.unsubscribe(session.thread.id)
        except asyncio.CancelledError:
            raise
        except Exception as caught:
            unsubscribe_error = caught

        if unsubscribe_error is not None:
            combined = (
                BaseExceptionGroup(
                    "Side cleanup failed",
                    [error, unsubscribe_error],
                )
                if error is not None
                else unsubscribe_error
            )
            outcome = SideLifecycleOutcome(
                side_id=session.side_id,
                state=SideTopicState.OPEN,
                error=combined,
            )
            await self._deliver_side_lifecycle(outcome)
            return outcome

        final_state = state if error is None else SideTopicState.FAILED
        try:
            record = self._bindings.transition_side_topic(
                session.side_id,
                final_state,
            )
            final_state = record.state
        except SideTopicConflict:
            record = self._bindings.get_side_topic(session.side_id)
            final_state = record.state
        except Exception as caught:
            persistence_error = (
                BaseExceptionGroup(
                    "Side persistence cleanup failed",
                    [error, caught],
                )
                if error is not None
                else caught
            )
            outcome = SideLifecycleOutcome(
                side_id=session.side_id,
                state=SideTopicState.OPEN,
                error=persistence_error,
            )
            await self._deliver_side_lifecycle(outcome)
            return outcome

        task_to_cancel: asyncio.Task[None] | None = None
        async with self._side_lock(session.side_id):
            if self._sides.get(session.side_id) is session:
                self._sides.pop(session.side_id, None)
                active = session.active
                if active is not None:
                    active.receipt_attempted.set()
                    task_to_cancel = active.task
                session.active = None
            self._side_locks.pop(session.side_id, None)
        if (
            task_to_cancel is not None
            and task_to_cancel is not asyncio.current_task()
            and not task_to_cancel.done()
        ):
            task_to_cancel.cancel()
            await asyncio.gather(task_to_cancel, return_exceptions=True)

        outcome = SideLifecycleOutcome(
            side_id=session.side_id,
            state=final_state,
            error=error,
        )
        await self._deliver_side_lifecycle(outcome)
        return outcome

    async def _deliver_side_lifecycle(
        self,
        outcome: SideLifecycleOutcome,
    ) -> None:
        try:
            await self._on_completion(outcome)
        except Exception:
            logger.exception(
                "Side lifecycle delivery failed",
                extra={"side_id": outcome.side_id},
            )

    @staticmethod
    def _redeem_side_admission(
        session: _SideSession,
        active: _ActiveSideTurn | None,
        admission: SideSubmissionAdmission,
    ) -> None:
        actual_turn_id = active.handle.id if active is not None else None
        if (
            admission.revision != session.revision
            or admission.thread_id != session.thread.id
            or admission.turn_id != actual_turn_id
        ):
            raise SteerRace(
                "准备本条消息期间 Side Turn 状态已变化，本条消息未执行，请重新发送。"
            )

    async def _interrupt_goal(
        self,
        binding_id: str,
        active: _ActiveGoal,
    ) -> None:
        async with self._lock(binding_id):
            if self._goals.get(binding_id) is not active:
                return
            if active.terminal_observed:
                # The terminal evidence is already frozen and this slot is
                # retained only until the Channel handoff completes.  Shutdown
                # must not issue another pause or terminal-cleanup mutation.
                return
            if active.state in {
                GoalOperationState.STARTING,
                GoalOperationState.UNKNOWN,
                GoalOperationState.EXTERNAL_ACTIVE,
            }:
                logger.warning(
                    "cannot safely start a second Goal cleanup during shutdown",
                    extra={
                        "thread_id": active.thread_id,
                        "goal_state": active.state.value,
                    },
                )
                return
            if active.handle is None:
                logger.warning(
                    "cannot safely stop Goal without a routed handle during shutdown",
                    extra={"thread_id": active.thread_id},
                )
                return
            if active.cleanup_succeeded:
                return
            await self._pause_and_clean_goal(active)

    async def _interrupt(self, binding_id: str, active: _ActiveTurn) -> None:
        async with self._lock(binding_id):
            if self._active.get(binding_id) is not active:
                return
            if (
                active.terminal_observed
                and not active.interrupt_attempted
                and not active.cleanup_required
            ):
                return
            await self._interrupt_and_clean(active)

    async def _interrupt_and_clean(self, active: _ActiveTurn) -> StopDisposition:
        self._mark_stopping(active)
        active.cleanup_required = True
        if active.interrupt_succeeded:
            if active.cleanup_succeeded:
                return StopDisposition.STOPPING
            return await self._clean_stopping(active)
        retrying_unknown_interrupt = active.interrupt_attempted
        active.interrupt_attempted = True
        try:
            await active.handle.interrupt()
        except Exception as error:
            if active.terminal_observed and retrying_unknown_interrupt:
                # We retried the unknown interrupt as promised. Some native
                # implementations reject interrupt once the exact Turn is
                # already terminal; exact-Thread cleanup is still required to
                # remove any terminal child left behind by that Turn.
                logger.warning(
                    "repeat interrupt failed after exact terminal; "
                    "continuing exact Thread cleanup",
                    exc_info=True,
                    extra={
                        "thread_id": active.handle.thread_id,
                        "turn_id": active.handle.id,
                    },
                )
                return await self._clean_stopping(active)
            raise TurnInterruptFailed(
                "Codex Turn 中断结果未确认；当前会话保持 stopping。"
                "请再次发送 /stop 重试中断。"
            ) from error
        active.interrupt_succeeded = True
        return await self._clean_stopping(active)

    async def _clean_stopping(self, active: _ActiveTurn) -> StopDisposition:
        try:
            if active.handle.thread_id != active.thread.id:
                raise RuntimeError("native Turn/Thread identity mismatch")
            binding = self._bindings.get(active.binding_id)
            if binding.native_thread_id != active.thread.id:
                raise RuntimeError("native Thread is not owned by this Binding")
            await self._terminal_cleanup.clean_thread(active.thread.id)
        except Exception as error:
            raise TerminalCleanupFailed(
                "Codex Turn 已请求中断，但已登记后台终端的清理请求失败；"
                "当前会话保持 stopping，不能假定后台终端已经停止。"
                "请再次发送 /stop 重试清理。"
            ) from error
        active.cleanup_succeeded = True
        active.cleanup_ready.set()
        return StopDisposition.REQUESTED

    async def _read_compaction_baseline(self, thread: NativeThread) -> object:
        failures = 0
        while True:
            try:
                response = await thread.read(include_turns=True)
                native_thread = getattr(response, "thread", None)
            except Exception as error:
                if not _is_transient_thread_read_error(
                    error,
                    thread_id=thread.id,
                    include_turns=True,
                ):
                    raise
                failures += 1
                if failures == 1 or failures % 120 == 0:
                    logger.warning(
                        "native Thread unavailable before compaction; retrying",
                        extra={
                            "thread_id": thread.id,
                            "failures": failures,
                            "error_type": type(error).__name__,
                        },
                    )
                await asyncio.sleep(self._poll_interval_seconds)
                continue
            if _thread_status_type(native_thread) == "notLoaded":
                await asyncio.sleep(self._poll_interval_seconds)
                continue
            return native_thread

    async def _consume_compaction(self, active: _ActiveCompaction) -> None:
        error: BaseException | None = None
        retain_active = False
        try:
            async with asyncio.timeout(self._compaction_timeout_seconds):
                await self._read_compaction_terminal(active)
        except asyncio.CancelledError:
            raise
        except BaseException as caught:
            if active.terminal_observed:
                error = caught
            else:
                self.close_admission()
                retain_active = True
                error = CompactionStateUnknown(
                    "Codex 压缩终态未确认；服务已停止接收新任务，"
                    "请重启服务后再继续。"
                )
                logger.error(
                    "native compaction terminal state is unknown; admission closed",
                    exc_info=(type(caught), caught, caught.__traceback__),
                    extra={"thread_id": active.thread.id},
                )
        finally:
            if not retain_active:
                async with self._lock(active.binding_id):
                    if self._compacting.get(active.binding_id) is active:
                        self._compacting.pop(active.binding_id, None)
                        self._advance_admission_revision(active.binding_id)
                        self._schedule_known_subscription_locked(
                            active.binding_id,
                            active.thread.id,
                        )

        await active.receipt_attempted.wait()
        outcome = CompactionOutcome(
            binding_id=active.binding_id,
            thread_id=active.thread.id,
            owner_id=active.owner_id,
            origin=active.origin,
            compact_turn_id=active.compact_turn_id,
            status=active.status,
            error=error,
        )
        try:
            await self._on_completion(outcome)
        except Exception:
            logger.exception(
                "compaction completion delivery failed",
                extra={"thread_id": active.thread.id},
            )

    async def _read_compaction_terminal(
        self,
        active: _ActiveCompaction,
    ) -> None:
        failures = 0
        while True:
            native_thread, failures = await self._read_compaction_thread(
                active,
                include_turns=False,
                failures=failures,
            )
            thread_status = _thread_status_type(native_thread)
            if thread_status == "systemError":
                raise RuntimeError(
                    "native Thread entered systemError during compaction"
                )
            if thread_status not in {"notLoaded", "active", "idle"}:
                raise RuntimeError(
                    f"unexpected native Thread status: {thread_status!r}"
                )
            if thread_status != "idle":
                await asyncio.sleep(self._poll_interval_seconds)
                continue

            native_thread, failures = await self._read_compaction_thread(
                active,
                include_turns=True,
                failures=failures,
            )
            thread_status = _thread_status_type(native_thread)
            if thread_status in {"notLoaded", "active"}:
                await asyncio.sleep(self._poll_interval_seconds)
                continue
            if thread_status == "systemError":
                raise RuntimeError(
                    "native Thread entered systemError during compaction"
                )
            if thread_status != "idle":
                raise RuntimeError(
                    f"unexpected native Thread status: {thread_status!r}"
                )

            new_turns = [
                turn
                for turn in getattr(native_thread, "turns", ())
                if getattr(turn, "id", None) not in active.before_turn_ids
            ]
            candidates = [
                turn
                for turn in new_turns
                if _turn_contains_item(turn, "contextCompaction")
            ]
            if len(new_turns) > 1 or len(candidates) > 1:
                # compact() returns no Turn ID. Concurrent writers on the same
                # native Thread therefore make request attribution impossible
                # through the pinned public facade; never select an arbitrary
                # candidate and report it as this request's success.
                raise RuntimeError(
                    "multiple native Turns appeared after compaction baseline"
                )
            if not candidates:
                # The start acknowledgement can race ahead of native status:
                # the pinned live probe observes idle -> active -> idle.  An
                # idle read without a new contextCompaction Turn is therefore
                # not completion evidence.
                await asyncio.sleep(self._poll_interval_seconds * 4)
                continue
            compact_turn = candidates[0]

            compact_turn_id = getattr(compact_turn, "id", None)
            active.compact_turn_id = (
                compact_turn_id if isinstance(compact_turn_id, str) else None
            )
            status = _enum_value(getattr(compact_turn, "status", None))
            active.status = status
            if status == "inProgress":
                await asyncio.sleep(self._poll_interval_seconds * 4)
                continue
            if status not in {"completed", "interrupted", "failed"}:
                raise RuntimeError(
                    f"unexpected native compaction status: {status!r}"
                )
            active.terminal_observed = True
            if status == "failed":
                native_error = getattr(compact_turn, "error", None)
                message = getattr(native_error, "message", None)
                raise CompactionFailed(
                    message
                    if isinstance(message, str) and message
                    else "原生 Codex 上下文压缩失败。"
                )
            if status == "interrupted":
                raise CompactionFailed("原生 Codex 上下文压缩被中断。")
            return

    async def _read_compaction_thread(
        self,
        active: _ActiveCompaction,
        *,
        include_turns: bool,
        failures: int,
    ) -> tuple[object, int]:
        while True:
            try:
                response = await active.thread.read(include_turns=include_turns)
                return getattr(response, "thread", None), failures
            except Exception as error:
                if not _is_transient_thread_read_error(
                    error,
                    thread_id=active.thread.id,
                    include_turns=include_turns,
                ):
                    raise
                failures += 1
                if failures == 1 or failures % 120 == 0:
                    logger.warning(
                        "native compaction read unavailable; retrying",
                        extra={
                            "thread_id": active.thread.id,
                            "failures": failures,
                            "error_type": type(error).__name__,
                        },
                    )
                await asyncio.sleep(self._poll_interval_seconds)

    async def _drain_terminal_turn_stream(self, active: _ActiveTurn) -> bool:
        observed_usage = False
        try:
            # turn_start registers the exact Turn queue before returning the
            # handle, so a Turn observed active afterwards can be drained only
            # after its persisted terminal state is known. Never enter this
            # stream for an immediate Turn that may have completed before the
            # queue was registered by the pinned SDK.
            async for notification in active.handle.stream():
                payload = getattr(notification, "payload", None)
                if getattr(notification, "method", None) == "turn/diff/updated":
                    diff_thread_id = getattr(payload, "thread_id", None)
                    diff_turn_id = getattr(payload, "turn_id", None)
                    diff = getattr(payload, "diff", None)
                    if (
                        diff_thread_id != active.handle.thread_id
                        or diff_turn_id != active.handle.id
                    ):
                        logger.warning(
                            "native Turn diff notification identity mismatch",
                            extra={
                                "thread_id": active.handle.thread_id,
                                "turn_id": active.handle.id,
                                "diff_thread_id": diff_thread_id,
                                "diff_turn_id": diff_turn_id,
                            },
                        )
                    elif not isinstance(diff, str):
                        logger.warning(
                            "native Turn diff notification has an invalid shape",
                            extra={
                                "thread_id": active.handle.thread_id,
                                "turn_id": active.handle.id,
                            },
                        )
                    else:
                        # App Server documents this event as the latest
                        # aggregate snapshot, not an incremental delta.
                        active.latest_diff = diff
                    continue
                if not isinstance(payload, ThreadTokenUsageUpdatedNotification):
                    continue
                if (
                    payload.thread_id != active.handle.thread_id
                    or payload.turn_id != active.handle.id
                ):
                    logger.warning(
                        "native token usage notification identity mismatch",
                        extra={
                            "thread_id": active.handle.thread_id,
                            "turn_id": active.handle.id,
                            "usage_thread_id": payload.thread_id,
                            "usage_turn_id": payload.turn_id,
                        },
                    )
                    continue
                usage = _context_window_usage_from_native(payload.token_usage)
                if usage is None:
                    logger.warning(
                        "native token usage notification has an invalid shape",
                        extra={
                            "thread_id": active.handle.thread_id,
                            "turn_id": active.handle.id,
                        },
                    )
                    continue
                self._context_window_usage[active.binding_id] = (
                    _ContextWindowUsageSnapshot(
                        thread_id=active.handle.thread_id,
                        usage=usage,
                    )
                )
                observed_usage = True
        except asyncio.CancelledError:
            raise
        except Exception:
            # Usage and aggregate diff are completion metadata. Persisted
            # thread.read remains the exact terminal-state source of truth
            # even when this public stream is unavailable.
            logger.warning(
                "native terminal Turn stream unavailable",
                exc_info=True,
                extra={
                    "thread_id": active.handle.thread_id,
                    "turn_id": active.handle.id,
                },
            )
        return observed_usage

    async def _consume(
        self,
        active: _ActiveTurn,
        *,
        recover_first: bool = False,
    ) -> None:
        result: object | None = None
        error: BaseException | None = None
        unavailable_error: BaseException | None = None
        activity: TurnActivitySnapshot | None = None
        retain_active = False
        try:
            observation: TurnResult | _TurnObservation | None = None
            if recover_first:
                try:
                    observation = await self._observe_with_bounded_recovery(
                        active,
                        _TurnViewUnverified("manual exact Turn recheck"),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as caught:
                    if active.terminal_observed:
                        error = caught
                    else:
                        unavailable_error = caught
                        retain_active = await self._mark_turn_observation_unavailable(
                            active
                        )

            while error is None and unavailable_error is None:
                if self._active.get(active.binding_id) is not active:
                    return
                if active.task_feedback.progress_card_enabled:
                    self._refresh_turn_activity(active)
                if observation is None:
                    try:
                        observation = await self._read_terminal_result(active)
                    except asyncio.CancelledError:
                        raise
                    except _TurnViewUnverified as caught:
                        if active.terminal_observed:
                            error = caught
                            break
                        try:
                            observation = await self._observe_with_bounded_recovery(
                                active,
                                caught,
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as recovery_error:
                            if active.terminal_observed:
                                error = recovery_error
                            else:
                                unavailable_error = recovery_error
                                retain_active = (
                                    await self._mark_turn_observation_unavailable(
                                        active
                                    )
                                )
                            break
                    except Exception as caught:
                        if active.terminal_observed:
                            error = caught
                        else:
                            unavailable_error = caught
                            retain_active = (
                                await self._mark_turn_observation_unavailable(
                                    active
                                )
                            )
                        break

                if isinstance(observation, TurnResult):
                    result = observation
                    break
                if observation is _TurnObservation.EXACT_IN_PROGRESS:
                    await self._mark_turn_observation_running(active)
                observation = None
                await asyncio.sleep(self._poll_interval_seconds)
        finally:
            try:
                if active.task_feedback.progress_card_enabled:
                    self._refresh_turn_activity(active)
                activity = self._turn_activity_snapshot(active)
                if active.terminal_observed:
                    observed_usage = False
                    try:
                        if active.terminal_stream_safe:
                            observed_usage = await self._drain_terminal_turn_stream(
                                active
                            )
                    finally:
                        if not observed_usage:
                            self._invalidate_context_window_usage(active.binding_id)
            finally:
                while not retain_active:
                    if self._active.get(active.binding_id) is not active:
                        break
                    async with self._lock(active.binding_id):
                        if self._active.get(active.binding_id) is not active:
                            break
                        if active.cleanup_required and not active.cleanup_succeeded:
                            wait_for_cleanup = True
                        else:
                            self._active.pop(active.binding_id, None)
                            self._advance_admission_revision(active.binding_id)
                            self._schedule_known_subscription_locked(
                                active.binding_id,
                                active.handle.thread_id,
                            )
                            wait_for_cleanup = False
                    if not wait_for_cleanup:
                        break
                    await active.cleanup_ready.wait()

        await active.receipt_attempted.wait()
        if unavailable_error is not None:
            if not await self._turn_unavailable_notice_is_current(active):
                return
            notice = TurnObservationUnavailableOutcome(
                binding_id=active.binding_id,
                thread_id=active.handle.thread_id,
                turn_id=active.handle.id,
                owner_id=active.owner_id,
                origin=active.origin,
                error=TerminalStateUnknown(
                    "Codex Turn 状态在短暂恢复后仍无法确认；当前 Turn 已停止自动读取。"
                    "Thread 仍可在 /sessions 中重新检查、归档或删除。"
                ),
            )
            try:
                await self._on_completion(notice)
            except Exception:
                logger.exception(
                    "turn observation unavailable notice delivery failed",
                    extra={"turn_id": active.handle.id},
                )
            return

        outcome = TurnOutcome(
            binding_id=active.binding_id,
            thread_id=active.handle.thread_id,
            turn_id=active.handle.id,
            owner_id=active.owner_id,
            origin=active.origin,
            result=result,
            error=error,
            background_cleanup_requested=active.cleanup_succeeded,
            turn_diff=active.latest_diff,
            task_feedback=active.task_feedback,
            feedback_revision=active.feedback_revision,
            activity=activity,
        )
        try:
            await self._on_completion(outcome)
        except Exception:
            logger.exception(
                "turn completion delivery failed",
                extra={"turn_id": active.handle.id},
            )

    async def _observe_with_bounded_recovery(
        self,
        active: _ActiveTurn,
        initial_error: BaseException,
    ) -> TurnResult | _TurnObservation:
        """Try at most three native I/O operations within one five-second window."""

        last_error = initial_error
        resume_required = isinstance(initial_error, _TurnResumeRequired)
        resume_attempted = False
        io_count = 0
        terminal_turn: object | None = None
        try:
            async with asyncio.timeout(_TURN_OBSERVATION_RECOVERY_TIMEOUT_SECONDS):
                while io_count < _TURN_OBSERVATION_RECOVERY_MAX_IO:
                    if self._active.get(active.binding_id) is not active:
                        raise asyncio.CancelledError
                    if resume_required and not resume_attempted:
                        io_count += 1
                        try:
                            thread = await self._codex.thread_resume(
                                active.handle.thread_id
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as caught:
                            last_error = caught
                            break
                        if thread.id != active.handle.thread_id:
                            raise RuntimeError(
                                "thread_resume returned a different native Thread identity"
                            )
                        async with self._lock(active.binding_id):
                            if self._active.get(active.binding_id) is not active:
                                raise asyncio.CancelledError
                            binding = self._bindings.get(active.binding_id)
                            if binding.native_thread_id != active.handle.thread_id:
                                raise RuntimeError(
                                    "Binding changed native Thread during Turn observation"
                                )
                            active.thread = thread
                            self._mark_thread_subscribed_locked(binding, thread)
                        resume_attempted = True
                        resume_required = False
                        if io_count >= _TURN_OBSERVATION_RECOVERY_MAX_IO:
                            break

                    io_count += 1
                    try:
                        native_thread = await self._read_native_thread(
                            active,
                            include_turns=True,
                        )
                        classified = self._classify_full_turn_view(
                            active,
                            native_thread,
                        )
                    except asyncio.CancelledError:
                        raise
                    except _TurnResumeRequired as caught:
                        last_error = caught
                        resume_required = not resume_attempted
                    except _TurnViewUnverified as caught:
                        last_error = caught
                    else:
                        if isinstance(classified, _TurnObservation):
                            return classified
                        terminal_turn = classified
                        break
                    await asyncio.sleep(self._poll_interval_seconds)
        except TimeoutError as caught:
            last_error = caught

        if terminal_turn is not None:
            return await self._materialize_terminal_turn(active, terminal_turn)
        raise TerminalStateUnknown(
            "exact Turn observation remained unavailable after bounded recovery"
        ) from last_error

    async def _mark_turn_observation_unavailable(
        self,
        active: _ActiveTurn,
    ) -> bool:
        async with self._lock(active.binding_id):
            if self._active.get(active.binding_id) is not active:
                return False
            if active.state is not ActiveState.OBSERVATION_UNAVAILABLE:
                active.state = ActiveState.OBSERVATION_UNAVAILABLE
                active.activity_revision += 1
                self._advance_admission_revision(active.binding_id)
            logger.warning(
                "native Turn observation unavailable after bounded recovery",
                extra={
                    "thread_id": active.handle.thread_id,
                    "turn_id": active.handle.id,
                },
            )
            return True

    async def _mark_turn_observation_running(self, active: _ActiveTurn) -> None:
        async with self._lock(active.binding_id):
            if self._active.get(active.binding_id) is not active:
                return
            if active.state is ActiveState.OBSERVATION_UNAVAILABLE:
                active.state = ActiveState.RUNNING
                active.activity_revision += 1
                self._advance_admission_revision(active.binding_id)

    async def _turn_unavailable_notice_is_current(
        self,
        active: _ActiveTurn,
    ) -> bool:
        async with self._lock(active.binding_id):
            return (
                self._active.get(active.binding_id) is active
                and active.state is ActiveState.OBSERVATION_UNAVAILABLE
            )

    async def _consume_goal(self, active: _ActiveGoal) -> None:
        error: BaseException | None = None
        persisted: GoalSnapshot | None = None
        retain_active = False
        defer_slot_release_until_delivery = False
        finalization = GoalFinalizationStatus.NOT_APPLICABLE
        finalization_error: BaseException | None = None
        handle = active.handle
        assert handle is not None
        try:
            if active.task_feedback.progress_card_enabled:
                active.stream_terminal = await handle.wait_terminal(
                    lambda projection: self._apply_goal_activity_projection(
                        active,
                        projection,
                    )
                )
            else:
                active.stream_terminal = await handle.wait_terminal()
            if active.stream_terminal.logical_turn_id != handle.id:
                raise RuntimeError("Goal stream terminal identity mismatch")
            async with asyncio.timeout(_COMPACTION_TERMINAL_TIMEOUT_SECONDS):
                persisted = await self._read_goal_terminal(active)
            active.persisted = persisted
            active.terminal_observed = True
            while active.cleanup_required and not active.cleanup_succeeded:
                await active.cleanup_ready.wait()
                if not active.cleanup_succeeded:
                    active.cleanup_ready.clear()
            if (
                persisted.status is GoalStatus.COMPLETE
                and active.final_turn_status == "completed"
            ):
                try:
                    async with self._lock(active.binding_id):
                        if self._goals.get(active.binding_id) is not active:
                            raise RuntimeError(
                                "Goal slot changed before completed finalization"
                            )
                        current = await self._require_goal_control().get(
                            active.thread_id
                        )
                        if current is None:
                            raise RuntimeError(
                                "completed Goal disappeared before finalization"
                            )
                        if current.thread_id != active.thread_id:
                            raise RuntimeError(
                                "completed Goal identity changed before finalization"
                            )
                        if not _same_goal_generation(active, current):
                            raise RuntimeError(
                                "completed Goal generation changed before finalization"
                            )
                        persisted = current
                        active.persisted = current
                        if current.status is GoalStatus.ACTIVE:
                            raise RuntimeError(
                                "completed Goal became active before finalization"
                            )
                        if current.status is GoalStatus.COMPLETE:
                            await self._clear_completed_goal(active)
                            finalization = GoalFinalizationStatus.CLEARED
                except BaseException as caught:
                    finalization = GoalFinalizationStatus.UNKNOWN
                    finalization_error = GoalStateUnknown(
                        "已完成 Goal 的自动清理结果未确认；最终结果仍会展示，"
                        "但会话保持占用且服务已关闭 admission。"
                    )
                    finalization_error.__cause__ = caught
                    retain_active = True
                    active.state = GoalOperationState.UNKNOWN
                    self.close_admission()
                    logger.error(
                        "completed Goal finalization is unknown; admission closed",
                        exc_info=(type(caught), caught, caught.__traceback__),
                        extra={
                            "thread_id": active.thread_id,
                            "logical_turn_id": handle.id,
                        },
                    )
            # Keep the exact Goal slot reserved until its terminal outcome has
            # been handed to the Channel.  Otherwise a manual clear or a new
            # same-second Goal can overtake the final Reply Card projection and
            # cause the confirmed Result/Files to be discarded as stale.
            defer_slot_release_until_delivery = not retain_active
        except asyncio.CancelledError:
            raise
        except BaseException as caught:
            error = caught
            retain_active = True
            if not (
                active.terminal_observed
                and isinstance(caught, TerminalCleanupFailed)
            ):
                self.close_admission()
                active.state = GoalOperationState.UNKNOWN
                error = GoalStateUnknown(
                    "Codex Goal 终态未确认；服务已关闭 admission，请重启后对账。"
                )
                logger.error(
                    "native Goal terminal state is unknown; admission closed",
                    exc_info=(type(caught), caught, caught.__traceback__),
                    extra={
                        "thread_id": active.thread_id,
                        "logical_turn_id": handle.id,
                    },
                )
        finally:
            if not retain_active and not defer_slot_release_until_delivery:
                async with self._lock(active.binding_id):
                    if self._goals.get(active.binding_id) is active:
                        self._goals.pop(active.binding_id, None)
                        self._advance_admission_revision(active.binding_id)
                        self._schedule_known_subscription_locked(
                            active.binding_id,
                            active.thread_id,
                        )

        try:
            await active.receipt_attempted.wait()
            outcome = GoalOutcome(
                binding_id=active.binding_id,
                thread_id=active.thread_id,
                logical_turn_id=handle.id,
                owner_id=active.owner_id,
                origin=active.origin,
                goal=persisted,
                final_physical_turn_id=(
                    active.stream_terminal.final_physical_turn_id
                    if active.stream_terminal is not None
                    else None
                ),
                final_turn_status=active.final_turn_status,
                final_items=active.final_items,
                final_response=active.final_response,
                error=error,
                background_cleanup_requested=active.cleanup_succeeded,
                task_feedback=active.task_feedback,
                feedback_revision=active.feedback_revision,
                activity=(
                    self._goal_activity_snapshot(active)
                    if active.task_feedback.progress_card_enabled
                    else None
                ),
                finalization=finalization,
                finalization_error=finalization_error,
            )
            try:
                async with asyncio.timeout(
                    _GOAL_COMPLETION_DELIVERY_TIMEOUT_SECONDS
                ):
                    await self._on_completion(outcome)
            except Exception:
                logger.exception(
                    "goal completion delivery failed",
                    extra={"logical_turn_id": handle.id},
                )
        finally:
            if defer_slot_release_until_delivery:
                async with self._lock(active.binding_id):
                    if self._goals.get(active.binding_id) is active:
                        self._goals.pop(active.binding_id, None)
                        self._advance_admission_revision(active.binding_id)
                        self._schedule_known_subscription_locked(
                            active.binding_id,
                            active.thread_id,
                        )

    async def _clear_completed_goal(self, active: _ActiveGoal) -> None:
        """Clear one four-proof completed Goal exactly once and confirm absence."""

        control = self._require_goal_control()
        cleared = await control.clear(active.thread_id)
        confirmed = await control.get(active.thread_id)
        if not cleared or confirmed is not None:
            raise RuntimeError("completed Goal clear could not be confirmed")

    async def _read_goal_terminal(self, active: _ActiveGoal) -> GoalSnapshot:
        control = self._require_goal_control()
        thread = active.thread
        terminal = active.stream_terminal
        response_materialization_retries = (
            _TERMINAL_RESPONSE_MATERIALIZATION_RETRIES
        )
        if thread is None or terminal is None:
            raise RuntimeError("Goal terminal cross-check is missing native handles")
        while True:
            persisted = await control.get(active.thread_id)
            if persisted is None:
                raise RuntimeError("persisted Goal disappeared before terminal check")
            if persisted.thread_id != active.thread_id:
                raise RuntimeError("persisted Goal identity mismatch")
            if not _same_goal_generation(active, persisted):
                raise RuntimeError("persisted Goal generation mismatch")
            if persisted.status is GoalStatus.ACTIVE:
                await asyncio.sleep(self._poll_interval_seconds)
                continue
            response = await thread.read(include_turns=False)
            native = getattr(response, "thread", None)
            if getattr(native, "id", None) != active.thread_id:
                raise RuntimeError("Goal Thread identity mismatch")
            status = _thread_status_type(native)
            if status in {"active", "notLoaded"}:
                await asyncio.sleep(self._poll_interval_seconds)
                continue
            if status != "idle":
                raise RuntimeError(f"unexpected Goal Thread status: {status!r}")
            response = await thread.read(include_turns=True)
            native = getattr(response, "thread", None)
            if getattr(native, "id", None) != active.thread_id:
                raise RuntimeError("Goal history Thread identity mismatch")
            turns = tuple(getattr(native, "turns", ()))
            if any(_enum_value(getattr(turn, "status", None)) == "inProgress" for turn in turns):
                await asyncio.sleep(self._poll_interval_seconds)
                continue
            exact = next(
                (
                    turn
                    for turn in turns
                    if getattr(turn, "id", None) == terminal.final_physical_turn_id
                ),
                None,
            )
            if exact is None:
                await asyncio.sleep(self._poll_interval_seconds * 4)
                continue
            turn_status = _enum_value(getattr(exact, "status", None))
            if turn_status not in {"completed", "interrupted", "failed"}:
                raise RuntimeError(f"unexpected Goal Turn status: {turn_status!r}")
            if turn_status != terminal.turn_status:
                raise RuntimeError("Goal stream and persisted Turn status disagree")
            items = tuple(getattr(exact, "items", ()))
            final_response = _final_agent_response(list(items))
            if (
                turn_status == "completed"
                and final_response is None
                and response_materialization_retries > 0
            ):
                if (
                    response_materialization_retries
                    == _TERMINAL_RESPONSE_MATERIALIZATION_RETRIES
                ):
                    logger.warning(
                        "native completed Goal Turn response not materialized; retrying",
                        extra={
                            "thread_id": active.thread_id,
                            "logical_turn_id": terminal.logical_turn_id,
                            "turn_id": terminal.final_physical_turn_id,
                        },
                    )
                response_materialization_retries -= 1
                await asyncio.sleep(self._poll_interval_seconds)
                continue
            active.final_turn_status = turn_status
            active.final_items = items
            active.final_response = final_response
            return persisted

    async def _read_terminal_result(
        self,
        active: _ActiveTurn,
    ) -> TurnResult | _TurnObservation:
        """Read one authoritative persisted Thread observation.

        ``openai-codex==0.147.0`` can lose an immediate ``turn/completed``
        before ``AsyncTurnHandle.run()`` registers its notification queue.
        ``AsyncThread.read()`` is public, reads the same native App Server
        state, and remains usable for steer/interrupt Turns.
        """

        native_thread = await self._read_native_thread(
            active,
            include_turns=False,
        )
        self._require_exact_observation_thread(active, native_thread)
        thread_status = _thread_status_type(native_thread)
        if thread_status == "active" and active.terminal_stream_safe:
            return _TurnObservation.ACTIVE
        if thread_status in {"notLoaded", "systemError"}:
            raise _TurnResumeRequired(
                f"native Thread is {thread_status}"
            )
        if thread_status not in {"active", "idle"}:
            raise RuntimeError(
                f"unexpected native Thread status: {thread_status!r}"
            )

        full_view = await self._read_native_thread(active, include_turns=True)
        classified = self._classify_full_turn_view(active, full_view)
        if isinstance(classified, _TurnObservation):
            if thread_status == "idle":
                raise _TurnViewUnverified(
                    "native Thread changed from idle to an in-progress exact Turn"
                )
            return classified
        return await self._materialize_terminal_turn(active, classified)

    def _classify_full_turn_view(
        self,
        active: _ActiveTurn,
        native_thread: object,
    ) -> object | _TurnObservation:
        self._require_exact_observation_thread(active, native_thread)
        thread_status = _thread_status_type(native_thread)
        if thread_status in {"notLoaded", "systemError"}:
            raise _TurnResumeRequired(
                f"full native Thread view is {thread_status}"
            )
        if thread_status not in {"active", "idle"}:
            raise RuntimeError(
                f"unexpected full native Thread status: {thread_status!r}"
            )
        turn = next(
            (
                item
                for item in getattr(native_thread, "turns", ())
                if getattr(item, "id", None) == active.handle.id
            ),
            None,
        )
        if turn is None:
            raise _TurnViewUnverified(
                "full native Thread view did not contain the exact Turn"
            )
        turn_status = _enum_value(getattr(turn, "status", None))
        if turn_status == "inProgress":
            if thread_status != "active":
                raise _TurnViewUnverified(
                    "idle native Thread still reported the exact Turn inProgress"
                )
            active.terminal_stream_safe = True
            return _TurnObservation.EXACT_IN_PROGRESS
        if turn_status not in {"completed", "interrupted", "failed"}:
            raise RuntimeError(
                f"unexpected native Turn status: {turn_status!r}"
            )
        active.terminal_observed = True
        return turn

    async def _materialize_terminal_turn(
        self,
        active: _ActiveTurn,
        turn: object,
    ) -> TurnResult:
        """Deliver a proven terminal Turn without re-entering recovery."""

        status = _enum_value(getattr(turn, "status", None))
        if status == "failed":
            native_error = getattr(turn, "error", None)
            message = getattr(native_error, "message", None)
            raise RuntimeError(
                message
                if isinstance(message, str) and message
                else "native Turn failed"
            )

        current = turn
        retries = _TERMINAL_RESPONSE_MATERIALIZATION_RETRIES
        while status == "completed":
            items = list(getattr(current, "items", ()))
            if _final_agent_response(items) is not None or retries == 0:
                break
            if retries == _TERMINAL_RESPONSE_MATERIALIZATION_RETRIES:
                logger.warning(
                    "native completed Turn response not materialized; retrying",
                    extra={
                        "thread_id": active.handle.thread_id,
                        "turn_id": active.handle.id,
                    },
                )
            retries -= 1
            await asyncio.sleep(self._poll_interval_seconds)
            try:
                native_thread = await self._read_native_thread(
                    active,
                    include_turns=True,
                )
                refreshed = self._classify_full_turn_view(active, native_thread)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "terminal Turn response refresh failed; delivering without text",
                    exc_info=True,
                    extra={
                        "thread_id": active.handle.thread_id,
                        "turn_id": active.handle.id,
                    },
                )
                break
            if isinstance(refreshed, _TurnObservation):
                logger.warning(
                    "terminal Turn regressed to inProgress; delivering proven terminal",
                    extra={
                        "thread_id": active.handle.thread_id,
                        "turn_id": active.handle.id,
                    },
                )
                break
            refreshed_status = _enum_value(getattr(refreshed, "status", None))
            if refreshed_status != status:
                logger.warning(
                    "terminal Turn status changed during response materialization",
                    extra={
                        "thread_id": active.handle.thread_id,
                        "turn_id": active.handle.id,
                        "status": status,
                        "refreshed_status": refreshed_status,
                    },
                )
                break
            current = refreshed

        items = list(getattr(current, "items", ()))
        return TurnResult(
            id=active.handle.id,
            status=getattr(current, "status"),
            error=getattr(current, "error", None),
            started_at=getattr(current, "started_at", None),
            completed_at=getattr(current, "completed_at", None),
            duration_ms=getattr(current, "duration_ms", None),
            final_response=_final_agent_response(items),
            items=items,
            usage=None,
        )

    @staticmethod
    def _require_exact_observation_thread(
        active: _ActiveTurn,
        native_thread: object,
    ) -> None:
        if getattr(native_thread, "id", None) != active.handle.thread_id:
            raise RuntimeError(
                "native Thread read returned a different identity"
            )

    async def _read_native_thread(
        self,
        active: _ActiveTurn,
        *,
        include_turns: bool,
    ) -> object:
        try:
            response = await active.thread.read(include_turns=include_turns)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "native Thread read unavailable",
                extra={
                    "thread_id": active.handle.thread_id,
                    "turn_id": active.handle.id,
                    "error_type": type(error).__name__,
                },
            )
            if _is_recoverable_turn_observation_io_error(error):
                raise _TurnResumeRequired("native Thread read failed") from error
            if (
                include_turns
                and isinstance(error, InvalidRequestError)
                and error.code == -32600
                and error.message
                == f"thread {active.handle.thread_id} {_NOT_MATERIALIZED_SUFFIX}"
            ):
                raise _TurnViewUnverified(
                    "native Thread turns are not materialized yet"
                ) from error
            raise
        return getattr(response, "thread", None)

    def _track(self, active: _ActiveTurn) -> None:
        self._active[active.binding_id] = active
        self._start_turn_consumer(active)

    def _start_turn_consumer(
        self,
        active: _ActiveTurn,
        *,
        recover_first: bool = False,
    ) -> None:
        task = asyncio.create_task(
            self._consume(active, recover_first=recover_first),
            name=f"codex-turn:{active.handle.id}",
        )
        active.task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _track_compaction(self, active: _ActiveCompaction) -> None:
        task = asyncio.create_task(
            self._consume_compaction(active),
            name=f"codex-compact:{active.thread.id}",
        )
        active.task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _track_goal(self, active: _ActiveGoal) -> None:
        handle = active.handle
        assert handle is not None and handle.id is not None
        task = asyncio.create_task(
            self._consume_goal(active),
            name=f"codex-goal:{handle.id}",
        )
        active.task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _redeem_submission_admission(
        self,
        *,
        binding_id: str,
        active: _ActiveTurn | None,
        admission: SubmissionAdmission | None,
    ) -> None:
        if admission is None:
            return
        if admission.revision != self._admission_revision(binding_id):
            raise SteerRace(
                "准备本条消息期间当前任务状态已变化，本条消息未执行，请重新发送。"
            )
        binding = self._bindings.get(binding_id)
        if admission.settings_revision != binding.settings_revision:
            raise SteerRace(
                "准备本条消息期间会话配置已变化，本条消息未执行，请重新发送。"
            )
        if admission.context_revision != binding.context_revision:
            raise SteerRace(
                "准备本条消息期间上下文边界已变化，本条消息未执行，请重新发送。"
            )
        actual_thread_id = active.handle.thread_id if active is not None else None
        actual_turn_id = active.handle.id if active is not None else None
        if (
            admission.thread_id != actual_thread_id
            or admission.turn_id != actual_turn_id
        ):
            self.close_admission()
            raise RuntimeError(
                "submission admission state changed without a revision; "
                "service admission is closed"
            )

    def _commit_context_cursor_locked(
        self,
        *,
        binding: ThreadBinding,
        commit: ContextCursorCommit | None,
    ) -> None:
        if commit is None:
            return
        try:
            self._bindings.commit_context_anchor(
                binding_id=binding.id,
                expected_context_revision=commit.expected_context_revision,
                anchor=commit.anchor,
            )
        except BaseException as error:
            self.close_admission()
            raise ContextBoundaryCommitFailed(
                "任务已被 Codex 接受，但 @ 上下文边界未能持久化；"
                "服务已停止接收新任务，请重启后对账。"
            ) from error

    def _admission_revision(self, binding_id: str) -> int:
        return self._admission_revisions.get(binding_id, 0)

    def _advance_admission_revision(self, binding_id: str) -> None:
        self._admission_revisions[binding_id] = (
            self._admission_revision(binding_id) + 1
        )

    def _require_turn_activity_precondition_locked(
        self,
        binding_id: str,
        *,
        expected_activity_revision: int,
        expected_turn_id: str | None,
    ) -> None:
        if expected_activity_revision != self._admission_revision(binding_id):
            raise ThreadActivityChanged(
                "会话运行状态已经变化，本次操作未执行；请刷新 /sessions。"
            )
        active = self._active.get(binding_id)
        actual_turn_id = active.handle.id if active is not None else None
        if actual_turn_id != expected_turn_id:
            raise ThreadActivityChanged(
                "会话的 exact Turn 已经变化，本次操作未执行；"
                "请刷新 /sessions。"
            )

    def _mark_stopping(self, active: _ActiveTurn) -> None:
        if active.state is not ActiveState.STOPPING:
            active.state = ActiveState.STOPPING
            active.activity_revision += 1
            self._advance_admission_revision(active.binding_id)

    def _lock(self, binding_id: str) -> asyncio.Lock:
        lock = self._locks.get(binding_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[binding_id] = lock
        return lock


def _enum_value(value: object) -> str | None:
    raw = getattr(value, "value", value)
    return raw if isinstance(raw, str) else None


def _context_window_usage_from_native(
    token_usage: object,
) -> ContextWindowUsage | None:
    last = getattr(token_usage, "last", None)
    used_tokens = getattr(last, "total_tokens", None)
    context_window_tokens = getattr(token_usage, "model_context_window", None)
    if (
        isinstance(used_tokens, bool)
        or not isinstance(used_tokens, int)
        or used_tokens < 0
    ):
        return None
    if context_window_tokens is not None and (
        isinstance(context_window_tokens, bool)
        or not isinstance(context_window_tokens, int)
        or context_window_tokens <= 0
    ):
        return None
    return ContextWindowUsage(used_tokens, context_window_tokens)


def _is_transient_thread_read_error(
    error: BaseException,
    *,
    thread_id: str,
    include_turns: bool,
) -> bool:
    if _is_transient_thread_rpc_error(error):
        return True
    return (
        include_turns
        and isinstance(error, InvalidRequestError)
        and error.code == -32600
        and error.message == f"thread {thread_id} {_NOT_MATERIALIZED_SUFFIX}"
    )


def _is_recoverable_turn_observation_io_error(error: BaseException) -> bool:
    return isinstance(
        error,
        (InternalRpcError, TransportClosedError, TimeoutError, ConnectionError, OSError),
    ) or is_retryable_error(error)


def _is_transient_thread_rpc_error(error: BaseException) -> bool:
    return isinstance(error, InternalRpcError) or is_retryable_error(error)


def _thread_status_type(native_thread: object) -> str | None:
    status = getattr(native_thread, "status", None)
    root = getattr(status, "root", status)
    value = getattr(root, "type", None)
    return value if isinstance(value, str) else None


def _turn_contains_item(turn: object, item_type: str) -> bool:
    return any(
        getattr(getattr(item, "root", item), "type", None) == item_type
        for item in getattr(turn, "items", ())
    )


def _final_agent_response(items: list[object]) -> str | None:
    fallback: str | None = None
    for item in reversed(items):
        root = getattr(item, "root", item)
        if getattr(root, "type", None) != "agentMessage":
            continue
        text = getattr(root, "text", None)
        if not isinstance(text, str):
            continue
        phase = _enum_value(getattr(root, "phase", None))
        if phase == "final_answer":
            return text
        if phase is None and fallback is None:
            fallback = text
    return fallback
