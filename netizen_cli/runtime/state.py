"""Live runtime records and the shared state every CodexRuntime layer builds on.

CodexRuntime is split into a linear chain of layer classes (see ``codex_runtime``).
Each layer may call only itself and the layers below it; mypy enforces this,
because a lower class has no attribute that only a higher class defines."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path

from openai_codex import (
    AsyncThread,
    InvalidRequestError,
)

from ..account_rate_limits import (
    AccountRateLimits,
)
from ..bindings import (
    BindingStore,
    BindingTaskFeedback,
    ThreadBinding,
)
from ..domain import (
    ActiveState,
    GoalOperationState,
    NativeCapability,
)
from ..model_settings import TurnModelSettings
from ..partial_answers import PartialAnswer
from ..progress_card_settings import ProgressCardSettings
from .name_writes import ThreadNameWrites
from .thread_naming import ThreadNamer
from .contracts import (
    NativeTurnHandle,
    NativeThread,
    NativeCodex,
    ThreadOccupied,
    SteerRace,
    ContextBoundaryCommitFailed,
    ThreadLifecycleError,
    TurnObservationUnavailable,
    ScheduledInitialStartConflict,
    ThreadLifecycleStateUnknown,
    ThreadArchived,
    ThreadResumeNotFound,
    ThreadActivityChanged,
    ThreadLifecycleState,
    SideSessionState,
    ThreadSubscriptionState,
    ContextWindowUsage,
    ContextCursorCommit,
    RuntimeOutcome,
    CompletionHandler,
)
from ..sdk_gap_adapter import (
    GoalControl,
    GoalControlError,
    GoalHandle,
    GoalSnapshot,
    GoalStreamTerminal,
    SideBoundaryControl,
    SkillCatalog,
    ThreadDeleteControl,
    ThreadSubscriptionControl,
)
from ..terminal_cleanup import BackgroundTerminalInspector, TerminalCleanup
from ..turn_activity import (
    TurnActivityEntrySnapshot,
    TurnPlanStepSnapshot,
)
from ..turn_plan_observer import TurnActivityObserver
from ..user_questions import (
    BindingQuestionTarget,
    QuestionRequest,
    QuestionTarget,
    SideQuestionTarget,
)

from . import limits
from .native_values import (
    _resume_rejection,
)


# Every runtime layer logs under the historical module name, which operators and
# tests already filter on; the split into layers is not an operational change.
RUNTIME_LOGGER_NAME = "netizen_cli.codex_runtime"
logger = logging.getLogger(RUNTIME_LOGGER_NAME)

QuestionHandler = Callable[[QuestionTarget, object, QuestionRequest, str], Awaitable[None]]
InputReceivedHandler = Callable[[str, str, str], None]


class _TurnViewUnverified(RuntimeError):
    """One read cycle could not establish an exact authoritative Turn view."""


class _TurnResumeRequired(_TurnViewUnverified):
    """One bounded recovery attempt must replace the native Thread handle."""


async def _ignore_completion(_outcome: RuntimeOutcome) -> None:
    return None


@dataclass(slots=True)
class _ScheduledInitialSubmission:
    run_id: str
    begun: bool = False


@dataclass(slots=True)
class _ActiveTurn:
    binding_id: str
    thread: NativeThread
    handle: NativeTurnHandle
    owner_id: str
    origin: object
    receipt_attempted: asyncio.Event
    task_feedback: BindingTaskFeedback
    cwd: Path | None = None
    state: ActiveState = ActiveState.RUNNING
    interrupt_attempted: bool = False
    interrupt_succeeded: bool = False
    cleanup_required: bool = False
    cleanup_succeeded: bool = False
    terminal_observed: bool = False
    cleanup_ready: asyncio.Event = field(default_factory=asyncio.Event)
    in_progress_observed: bool = False
    observation_error: str | None = None
    recheck_ready: asyncio.Event | None = None
    recheck_revision: int | None = None
    latest_diff: str | None = None
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
    activity_sequence: int = 0
    activity_segment: int = 0
    activity_commentary: dict[str, TurnActivityEntrySnapshot] = field(default_factory=dict)
    activity_commentary_order: list[str] = field(default_factory=list)
    activity_operations: dict[str, TurnActivityEntrySnapshot] = field(default_factory=dict)
    activity_operation_order: list[str] = field(default_factory=list)
    partial_answers: dict[tuple[str, str, str], PartialAnswer] = field(default_factory=dict)
    question_ids: set[tuple[str, str]] = field(default_factory=set)
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
    task_feedback: BindingTaskFeedback
    cwd: Path | None = None
    persisted: GoalSnapshot | None = None
    goal_cleared: bool = False
    generation_created_at: int | None = None
    generation_token_budget: int | None = None
    stream_terminal: GoalStreamTerminal | None = None
    final_turn_status: str | None = None
    final_items: tuple[object, ...] = ()
    final_response: str | None = None
    feedback_revision: int = 1
    activity_revision: int = 1
    activity_observation_enabled: bool = True
    activity_turn_id: str | None = None
    activity_turn_ids: set[str] = field(default_factory=set)
    plan_cursor: int = 0
    plan_generated: bool = False
    plan_available: bool = True
    plan_steps: tuple[TurnPlanStepSnapshot, ...] = ()
    activity_sequence: int = 0
    activity_segment: int = 0
    activity_commentary: dict[str, TurnActivityEntrySnapshot] = field(default_factory=dict)
    activity_commentary_order: list[str] = field(default_factory=list)
    activity_operations: dict[str, TurnActivityEntrySnapshot] = field(default_factory=dict)
    activity_operation_order: list[str] = field(default_factory=list)
    partial_answers: dict[tuple[str, str, str], PartialAnswer] = field(default_factory=dict)
    question_ids: set[tuple[str, str]] = field(default_factory=set)
    pause_attempted: bool = False
    interrupt_acknowledged: bool = False
    cleanup_required: bool = False
    cleanup_succeeded: bool = False
    terminal_observed: bool = False
    observation_unavailable: bool = False
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
    side_id: str
    handle: NativeTurnHandle
    owner_id: str
    origin: object
    receipt_attempted: asyncio.Event
    task_feedback: BindingTaskFeedback
    state: ActiveState = ActiveState.RUNNING
    interrupt_attempted: bool = False
    interrupt_succeeded: bool = False
    cleanup_required: bool = False
    cleanup_succeeded: bool = False
    terminal_observed: bool = False
    completion_notification_seen: bool = False
    cleanup_ready: asyncio.Event = field(default_factory=asyncio.Event)
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
    activity_sequence: int = 0
    activity_segment: int = 0
    activity_commentary: dict[str, TurnActivityEntrySnapshot] = field(default_factory=dict)
    activity_commentary_order: list[str] = field(default_factory=list)
    activity_operations: dict[str, TurnActivityEntrySnapshot] = field(default_factory=dict)
    activity_operation_order: list[str] = field(default_factory=list)
    partial_answers: dict[tuple[str, str, str], PartialAnswer] = field(default_factory=dict)
    question_ids: set[tuple[str, str]] = field(default_factory=set)
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


class RuntimeState:
    """Shared per-instance state, slot guards and admission revisions."""

    _codex: NativeCodex
    _bindings: BindingStore
    _terminal_cleanup: TerminalCleanup
    _skill_catalog: SkillCatalog | None
    _account_rate_limits: AccountRateLimits | None
    _goal_control: GoalControl | None
    _side_boundary_control: SideBoundaryControl | None
    _thread_subscription_control: ThreadSubscriptionControl | None
    _background_terminal_inspector: BackgroundTerminalInspector | None
    _thread_delete_control: ThreadDeleteControl | None
    _turn_plan_observer: TurnActivityObserver | None
    _progress_card_defaults: Callable[[], ProgressCardSettings]
    _on_completion: CompletionHandler
    _on_question: QuestionHandler | None
    _on_input_received: InputReceivedHandler | None
    _poll_interval_seconds: float
    _compaction_timeout_seconds: float
    _side_idle_seconds: float
    _ordinary_thread_idle_seconds: float
    _active: dict[str, _ActiveTurn]
    _retiring_turns: dict[tuple[str, str], _ActiveTurn]
    _compacting: dict[str, _ActiveCompaction]
    _goals: dict[str, _ActiveGoal]
    _context_window_usage: dict[str, _ContextWindowUsageSnapshot]
    _lifecycles: dict[str, _ActiveThreadLifecycle]
    _subscriptions: dict[str, _ThreadSubscription]
    _sides: dict[str, _SideSession]
    _locks: dict[str, asyncio.Lock]
    _side_locks: dict[str, asyncio.Lock]
    _admission_revisions: dict[str, int]
    _tasks: set[asyncio.Task[None]]
    _fork_creations: dict[asyncio.Task[object], str]
    _side_idle_tasks: set[asyncio.Task[None]]
    _subscription_idle_tasks: set[asyncio.Task[None]]
    _name_writes: ThreadNameWrites
    _name_generation_requests: dict[str, asyncio.Event]
    _automatic_thread_naming: bool
    _accepting: bool
    _shutdown_requested: bool
    _thread_namer: ThreadNamer | None

    def _thread_handle(self, thread_id: str) -> AsyncThread:
        """Open the public SDK handle for one exact native Thread."""
        return AsyncThread(self._codex, thread_id)

    def _capture_task_feedback(self, feedback: BindingTaskFeedback) -> BindingTaskFeedback:
        """Freeze presentation defaults without changing the Binding's intent."""
        if feedback.progress_card_settings is not None:
            return feedback
        return replace(feedback, progress_card_settings=self._progress_card_defaults())

    def set_completion_handler(self, handler: CompletionHandler) -> None:
        self._on_completion = handler

    def set_question_handler(self, handler: QuestionHandler) -> None:
        self._on_question = handler

    def set_input_received_handler(self, handler: InputReceivedHandler) -> None:
        self._on_input_received = handler

    def _notify_input_received(
        self, thread_id: str, reply_turn_id: str, physical_turn_id: str,
    ) -> None:
        """Clear the prior display choice before submitting a targeted new input."""
        if self._on_input_received is None:
            return
        try:
            self._on_input_received(thread_id, reply_turn_id, physical_turn_id)
        except Exception:
            logger.exception(
                "received input presentation notification failed",
                extra={"thread_id": thread_id, "turn_id": physical_turn_id},
            )

    def _deliver_question(
        self,
        active: _ActiveTurn | _ActiveGoal | _ActiveSideTurn,
        turn_id: str,
        request: QuestionRequest,
    ) -> None:
        handler = self._on_question
        identity = (turn_id, request.item_id)
        if handler is None or identity in active.question_ids:
            return
        target: QuestionTarget
        if isinstance(active, _ActiveSideTurn):
            session = self._sides.get(active.side_id)
            if (
                session is None
                or session.state is not SideSessionState.OPEN
                or session.active is not active
            ):
                return
            target = SideQuestionTarget(active.side_id)
            target_log = {"side_id": active.side_id}
        else:
            if not active.activity_observation_enabled:
                return
            target = BindingQuestionTarget(active.binding_id)
            target_log = {"binding_id": active.binding_id}
        active.question_ids.add(identity)
        origin = active.origin
        owner_id = active.owner_id

        async def deliver() -> None:
            try:
                async with asyncio.timeout(limits.QUESTION_DELIVERY_TIMEOUT_SECONDS):
                    await handler(target, origin, request, owner_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "native question presentation failed",
                    exc_info=True,
                    extra={**target_log, "turn_id": turn_id},
                )

        task = asyncio.create_task(deliver(), name=f"codex-question:{request.item_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @property
    def available_capabilities(self) -> frozenset[NativeCapability]:
        capabilities: set[NativeCapability] = set()
        if self._account_rate_limits is not None:
            capabilities.add(NativeCapability.ACCOUNT_RATE_LIMITS)
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

    def _invalidate_context_window_usage(self, binding_id: str) -> None:
        self._context_window_usage.pop(binding_id, None)

    def _invalidate_name_generation(self, binding_id: str) -> None:
        # Cover model preparation and delivery as well as the native job.
        request = self._name_generation_requests.get(binding_id)
        if request is not None:
            request.set()
        if self._thread_namer is not None:
            self._thread_namer.invalidate(binding_id)

    def _guard_no_scheduled_initial(self, binding_id: str) -> None:
        if self._bindings.scheduled_initial_reservation(binding_id) is not None:
            raise ScheduledInitialStartConflict("定时任务正在启动，请稍后重发。")

    async def _resume_thread_for_operation(self, thread_id: str) -> NativeThread:
        """Recognize only verified exact-ID resume rejections.

        A timeout, another invalid request, or the same text from a different
        operation is not proof that the requested operation had no side effects.
        Restore shares the exact classifier, but retains its own phase outcomes
        and Binding-local unknown boundary. Active-Turn recovery is independent.
        """

        try:
            return await self._codex.thread_resume(thread_id, include_turns=False)
        except InvalidRequestError as error:
            rejection = _resume_rejection(error, thread_id)
            if rejection == "archived":
                raise ThreadArchived(
                    "原生会话已归档；请先使用“恢复归档并切换”。"
                ) from error
            if rejection == "not_found":
                raise ThreadResumeNotFound(
                    "未找到该原生会话的可恢复记录；Binding 已保留。"
                ) from error
            if rejection == "closing":
                raise ThreadLifecycleError(
                    "原生会话正在关闭；请等待关闭完成后再恢复。"
                ) from error
            if rejection == "occupied":
                raise ThreadOccupied(
                    "该会话正被其他 Codex 实例占用，本次操作未执行。"
                    "请在占用该会话的 Codex App 中归档，或在占用它的 CLI 会话中执行 /archive；"
                    "然后回到飞书发送 /sessions archived，选择“恢复并切换”后重发消息。"
                    "其他会话和管理功能可继续使用。\n\n"
                    "归档会结束目标会话及其子会话的运行活动；"
                    "App 托管的 worktree 还可能被清理。"
                ) from error
            raise

    async def _steer_turn(
        self,
        request: Awaitable[object],
        *,
        race_message: str,
    ) -> None:
        """Share exact-Turn rejection and cancellation handling across inputs."""
        try:
            await request
        except asyncio.CancelledError:
            # The SDK delegates steer to a blocking worker-thread RPC. Cancelling
            # its waiter does not prove that the native request stopped.
            self.close_admission()
            raise
        except InvalidRequestError as error:
            raise SteerRace(race_message) from error

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

    def _require_goal_control(self) -> GoalControl:
        if self._goal_control is None:
            raise GoalControlError("当前原生 Goal 兼容能力不可用。")
        return self._goal_control

    @staticmethod
    def _unavailable_submission_error(active: _ActiveTurn) -> TurnObservationUnavailable:
        detail = active.observation_error or "尚未取得本轮任务的完整状态。"
        return TurnObservationUnavailable(
            "仍无法确认上一轮是否结束，本条消息未执行。"
            f"最近原因：{detail}\n"
            "可稍后重新发送消息，或在 /sessions 中重新检查、停止、归档或删除会话。"
        )

    def close_admission(self) -> None:
        self._accepting = False
        if self._thread_namer is not None:
            self._thread_namer.close()
        for record in self._subscriptions.values():
            self._cancel_subscription_idle(record)

    def _is_retiring_turn(self, active: _ActiveTurn) -> bool:
        return self._retiring_turns.get((active.binding_id, active.handle.id)) is active

    def _owns_turn_observation(self, active: _ActiveTurn) -> bool:
        return self._active.get(active.binding_id) is active or self._is_retiring_turn(active)

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
