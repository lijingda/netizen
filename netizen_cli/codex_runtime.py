"""Thin orchestration over public AsyncCodex Thread and Turn handles.

``CodexRuntime`` is assembled from a linear chain of lifecycle layers in
``netizen_cli.runtime``. Each layer may call only itself and the layers below it,
so a dependency on a later lifecycle is a type error rather than a convention:

    state          shared records, per-Binding locks, admission revisions, guards
    subscriptions  process-local native Thread subscriptions and idle release
    occupancy      Goal slot guards and exact subscription release
    forks          persistent forks and topic-creation tracking
    queries        read-only native Thread, catalog, model and account reads
    activity       Activity, progress and Partial Answer snapshots
    lifecycle      exact archive, delete and restore reservations
    naming         manual and automatic Thread naming
    observation    ordinary Turn consumption and terminal reads
    goals          native Goal physical Turns and logical terminal
    turn_control   ordinary stop, exact recheck and scheduled Turn reads
    compaction     native context compaction
    submission     ordinary input admission and start/steer
    sides          ephemeral Side sessions
    shutdown       service shutdown, interrupt and drain

Shared mutable state is declared once on ``RuntimeState`` and initialized here.
This module remains the import location for the runtime and its contracts.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable


from .account_rate_limits import (
    AccountRateLimits,
)
from .bindings import (
    BindingStore,
)
from .domain import (
    ActiveState,
    GoalOperationState,
)
from .progress_card_settings import ProgressCardSettings
from .runtime.name_writes import ThreadNameWrites
from .runtime.thread_naming import ThreadNamer
# Preserve existing public imports as aliases of the shared contract types.
from .runtime.contracts import (
    NativeCodex,
    RuntimeClosed,
    ThreadStopping,
    ThreadOccupied,
    ThreadRunningConfiguration,
    ThreadCompacting,
    ThreadNotMaterialized,
    SteerRace,
    TerminalCleanupFailed,
    TurnInterruptFailed,
    TurnStartFailed,
    ContextBoundaryCommitFailed,
    ThreadCompactStartFailed,
    CompactionFailed,
    CompactionStateUnknown,
    TerminalStateUnknown,
    SkillReferenceError,
    ThreadGoalActive,
    ExternalGoalActive,
    GoalNotFound,
    GoalStateUnknown,
    ThreadLifecycleError,
    TurnObservationUnavailable,
    ScheduledInitialStartConflict,
    ScheduledTurnReadError,
    ContextAnchorRequired,
    ThreadLifecycleStateUnknown,
    ThreadArchived,
    ThreadResumeNotFound,
    ThreadResumeFailed,
    ThreadRestoreRejected,
    ThreadNotArchived,
    ThreadDeleteUnavailable,
    ThreadDeleteTargetChanged,
    ThreadActivityChanged,
    ThreadReleaseError,
    ThreadReleaseStateUnknown,
    ThreadBackgroundTerminalsActive,
    ThreadCatalogError,
    ThreadCatalogDeadlineExceeded,
    ThreadCatalogLimitExceeded,
    SideSessionNotFound,
    SideSessionConflict,
    SideSessionClosing,
    SideStartFailed,
    SideCloseFailed,
    SubmitDisposition,
    GoalFinalizationStatus,
    ThreadLifecycleState,
    StopDisposition,
    SideSessionState,
    ThreadSubscriptionState,
    ReleaseDisposition,
    Submission,
    CompactSubmission,
    GoalSubmission,
    SideSubmission,
    SideSubmissionAdmission,
    ActiveTurnSnapshot,
    TurnProgressSnapshot,
    TurnActivitySnapshot,
    SideTurnActivitySnapshot,
    GoalActivitySnapshot,
    NativeThreadMetadata,
    NativeThreadCatalogState,
    NativeThreadCatalog,
    NativeThreadCatalogPage,
    ContextWindowUsage,
    ActiveGoalSnapshot,
    ThreadLifecycleSnapshot,
    ThreadSubscriptionSnapshot,
    BindingRuntimeSnapshot,
    SideSessionSnapshot,
    SubmissionAdmission,
    ContextCursorCommit,
    TurnOutcome,
    TurnObservationUnavailableOutcome,
    ThreadActivityDiscardedOutcome,
    CompactionOutcome,
    GoalOutcome,
    SideTurnOutcome,
    SideLifecycleOutcome,
    CompletionHandler,
)
from .sdk_gap_adapter import (
    GoalControl,
    SideBoundaryControl,
    SkillCatalog,
    ThreadDeleteControl,
    ThreadSubscriptionControl,
)
from .terminal_cleanup import BackgroundTerminalInspector, TerminalCleanup
from .turn_plan_observer import TurnActivityObserver

from .runtime import limits
from .runtime.state import (
    QuestionHandler,
    InputReceivedHandler,
    _ignore_completion,
    _ActiveTurn,
    _ActiveCompaction,
    _ActiveGoal,
    _ContextWindowUsageSnapshot,
    _ActiveThreadLifecycle,
    _ThreadSubscription,
    _SideSession,
)
from .runtime.native_values import (
    _is_paginated_turn_read_unavailable,
    _native_cwd,
    _final_agent_response,
)
from .runtime.shutdown import ShutdownRuntime


__all__ = [
    "ActiveGoalSnapshot",
    "ActiveState",
    "ActiveTurnSnapshot",
    "BindingRuntimeSnapshot",
    "CodexRuntime",
    "CompactSubmission",
    "CompactionFailed",
    "CompactionOutcome",
    "CompactionStateUnknown",
    "ContextAnchorRequired",
    "ContextBoundaryCommitFailed",
    "ContextCursorCommit",
    "ContextWindowUsage",
    "ExternalGoalActive",
    "GoalActivitySnapshot",
    "GoalFinalizationStatus",
    "GoalNotFound",
    "GoalOperationState",
    "GoalOutcome",
    "GoalStateUnknown",
    "GoalSubmission",
    "NativeThreadCatalog",
    "NativeThreadCatalogPage",
    "NativeThreadCatalogState",
    "NativeThreadMetadata",
    "ReleaseDisposition",
    "RuntimeClosed",
    "ScheduledInitialStartConflict",
    "ScheduledTurnReadError",
    "SideCloseFailed",
    "SideLifecycleOutcome",
    "SideSessionClosing",
    "SideSessionConflict",
    "SideSessionNotFound",
    "SideSessionSnapshot",
    "SideSessionState",
    "SideStartFailed",
    "SideSubmission",
    "SideSubmissionAdmission",
    "SideTurnActivitySnapshot",
    "SideTurnOutcome",
    "SkillReferenceError",
    "SteerRace",
    "StopDisposition",
    "Submission",
    "SubmissionAdmission",
    "SubmitDisposition",
    "TerminalCleanupFailed",
    "TerminalStateUnknown",
    "ThreadActivityChanged",
    "ThreadActivityDiscardedOutcome",
    "ThreadArchived",
    "ThreadBackgroundTerminalsActive",
    "ThreadCatalogDeadlineExceeded",
    "ThreadCatalogError",
    "ThreadCatalogLimitExceeded",
    "ThreadCompactStartFailed",
    "ThreadCompacting",
    "ThreadDeleteTargetChanged",
    "ThreadDeleteUnavailable",
    "ThreadGoalActive",
    "ThreadLifecycleError",
    "ThreadLifecycleSnapshot",
    "ThreadLifecycleState",
    "ThreadLifecycleStateUnknown",
    "ThreadNotArchived",
    "ThreadNotMaterialized",
    "ThreadOccupied",
    "ThreadReleaseError",
    "ThreadReleaseStateUnknown",
    "ThreadRestoreRejected",
    "ThreadResumeFailed",
    "ThreadResumeNotFound",
    "ThreadRunningConfiguration",
    "ThreadStopping",
    "ThreadSubscriptionSnapshot",
    "ThreadSubscriptionState",
    "TurnActivitySnapshot",
    "TurnInterruptFailed",
    "TurnObservationUnavailable",
    "TurnObservationUnavailableOutcome",
    "TurnOutcome",
    "TurnProgressSnapshot",
    "TurnStartFailed",
    "_final_agent_response",
    "_is_paginated_turn_read_unavailable",
    "_native_cwd",
]


class CodexRuntime(ShutdownRuntime):
    """Own live native operations and apply Binding-scoped Turn intent."""

    def __init__(
        self,
        *,
        codex: NativeCodex,
        bindings: BindingStore,
        terminal_cleanup: TerminalCleanup,
        skill_catalog: SkillCatalog | None = None,
        account_rate_limits: AccountRateLimits | None = None,
        goal_control: GoalControl | None = None,
        side_boundary_control: SideBoundaryControl | None = None,
        thread_subscription_control: ThreadSubscriptionControl | None = None,
        background_terminal_inspector: BackgroundTerminalInspector | None = None,
        thread_delete_control: ThreadDeleteControl | None = None,
        turn_plan_observer: TurnActivityObserver | None = None,
        progress_card_defaults: Callable[[], ProgressCardSettings] = ProgressCardSettings,
        on_completion: CompletionHandler = _ignore_completion,
        poll_interval_seconds: float = 0.5,
        compaction_timeout_seconds: float = limits.COMPACTION_TERMINAL_TIMEOUT_SECONDS,
        side_idle_seconds: float = limits.SIDE_IDLE_SECONDS,
        ordinary_thread_idle_seconds: float = limits.ORDINARY_THREAD_IDLE_SECONDS,
        automatic_thread_naming: bool = True,
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
        self._account_rate_limits = account_rate_limits
        self._goal_control = goal_control
        self._side_boundary_control = side_boundary_control
        self._thread_subscription_control = thread_subscription_control
        self._background_terminal_inspector = background_terminal_inspector
        self._thread_delete_control = thread_delete_control
        self._turn_plan_observer = turn_plan_observer
        self._progress_card_defaults = progress_card_defaults
        self._on_completion = on_completion
        self._on_question: QuestionHandler | None = None
        self._on_input_received: InputReceivedHandler | None = None
        self._poll_interval_seconds = poll_interval_seconds
        self._compaction_timeout_seconds = compaction_timeout_seconds
        self._side_idle_seconds = side_idle_seconds
        self._ordinary_thread_idle_seconds = ordinary_thread_idle_seconds
        self._active: dict[str, _ActiveTurn] = {}
        # ExternalMessage may start the next native Turn while the previous
        # Turn is still materializing its Result/Files. Its existing consumer
        # retains exact ownership until that independent handoff completes.
        self._retiring_turns: dict[tuple[str, str], _ActiveTurn] = {}
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
        self._fork_creations: dict[asyncio.Task[object], str] = {}
        self._side_idle_tasks: set[asyncio.Task[None]] = set()
        self._subscription_idle_tasks: set[asyncio.Task[None]] = set()
        self._name_writes = ThreadNameWrites()
        self._name_generation_requests: dict[str, asyncio.Event] = {}
        self._automatic_thread_naming = automatic_thread_naming
        self._accepting = True
        self._shutdown_requested = False
        self._thread_namer = (
            ThreadNamer(
                codex=codex,
                terminal_cleanup=terminal_cleanup,
                subscription_control=thread_subscription_control,
                commit=self._commit_automatic_thread_name,
            )
            if thread_subscription_control is not None
            else None
        )
