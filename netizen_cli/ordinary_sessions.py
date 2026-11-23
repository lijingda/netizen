"""Netizen-owned values for ordinary session inputs and completion."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .backends import BackendPolicy
from .bindings import BindingTaskFeedback, BindingTurnSettings, ThreadBinding
from .domain import ActiveState, FeishuScope, MessageContextAnchor, MentionContextMode
from .runtime.codex_contracts import BindingRuntimeSnapshot
from .session_views import SessionObservations
from .file_evidence import FileEvidence
from .partial_answers import PartialAnswer
from .prompt_inputs import PreparedPrompt as PreparedPrompt, PromptImage as PromptImage
from .runtime.contracts import (
    ActiveTurnSnapshot, ContextCursorCommit, StopAcknowledger, StopDisposition, Submission, SubmissionAdmission,
    TerminalStateUnknown, TurnActivitySnapshot, TurnObservationUnavailableOutcome,
)


@dataclass(frozen=True, slots=True)
class OrdinaryConclusion:
    binding_id: str
    thread_id: str
    turn_id: str
    owner_id: str
    origin: object
    status: str | None
    final_response: str | None
    error_detail: str | None = None
    native_failure_detail: str | None = None
    background_cleanup_requested: bool = False
    task_feedback: BindingTaskFeedback = BindingTaskFeedback()
    feedback_revision: int = 1
    activity: TurnActivitySnapshot | None = None
    partial_answers: tuple[PartialAnswer, ...] = ()
    cwd: Path | None = None
    file_evidence: Callable[[], FileEvidence] = FileEvidence

    def __post_init__(self) -> None:
        if self.feedback_revision < 1:
            raise ValueError("feedback revision must be positive")
        if self.activity is not None and (
            self.activity.binding_id != self.binding_id
            or self.activity.thread_id != self.thread_id
            or self.activity.turn_id != self.turn_id
        ):
            raise ValueError("Turn outcome activity belongs to another Turn")


@dataclass(slots=True)
class ClaudeInputAnchor:
    scope: FeishuScope
    source: object
    topic_root_id: str | None
    stop_reactions: Callable[[str], Awaitable[None]]
    retired: bool = False


@dataclass(frozen=True, slots=True)
class ConversationTarget:
    scope: FeishuScope
    source: object
    delivery_id: str


@dataclass(slots=True)
class LogicalReply:
    attempted: bool = False
    message_id: str | None = None

    def confirmed(self, message_id: str) -> None:
        self.message_id = message_id


@dataclass(frozen=True, slots=True)
class PhysicalAnswer(OrdinaryConclusion):
    logical_id: str | None = None
    wake: bool = False
    reply: LogicalReply | None = None
    publishable: Callable[[], bool] = lambda: True


@dataclass(frozen=True, slots=True)
class LogicalTerminal:
    binding_id: str
    thread_id: str
    turn_id: str
    owner_id: str
    origin: object
    status: str
    reply: LogicalReply
    mention_user_id: str | None
    publishable: Callable[[], bool]


@dataclass(frozen=True, slots=True)
class DegradedTurn(TurnObservationUnavailableOutcome):
    affected_inputs: tuple[str, ...] = ()


class LiveSubmissionUnknown(TerminalStateUnknown):
    """The runtime sends the uncertainty notice to the Binding conversation."""


OrdinaryEvent = OrdinaryConclusion | LogicalTerminal | TurnObservationUnavailableOutcome
OrdinaryCompletionHandler = Callable[[OrdinaryEvent], Awaitable[None]]


class OrdinaryInputRejected(RuntimeError):
    """The backend verified that this input was never submitted."""


@dataclass(frozen=True, slots=True)
class StopPreview:
    """Receipt text captured before the stop operation selects its target."""

    acknowledgement: str
    activity_revision: int | None = None
    turn_id: str | None = None

    def __post_init__(self) -> None:
        if (self.activity_revision is None) != (self.turn_id is None):
            raise ValueError("exact stop preview requires both revision and execution ID")


class OrdinarySessionPort(Protocol):
    """Admission, submission and exact activity for an ordinary Binding."""

    async def capture_submission_admission(self, binding_id: str) -> SubmissionAdmission: ...

    async def submit(
        self, *, binding: ThreadBinding, cwd: Path | None, input: PreparedPrompt, owner_id: str,
        origin: object, admission: SubmissionAdmission, context_commit: ContextCursorCommit | None,
        skill_names: tuple[str, ...],
    ) -> Submission: ...

    def turn_activity(
        self, binding_id: str, *, thread_id: str, turn_id: str,
    ) -> TurnActivitySnapshot | None: ...

    def reply_blocked_by_lifecycle(self, binding_id: str) -> bool: ...

    async def preview_stop(self, binding: ThreadBinding) -> StopPreview | None: ...

    async def stop_exact(
        self, binding_id: str, *, acknowledge: StopAcknowledger | None = None,
        expected_activity_revision: int | None = None, expected_turn_id: str | None = None,
    ) -> StopDisposition: ...

    async def recheck_turn_exact(
        self, binding_id: str, *, expected_activity_revision: int, expected_turn_id: str,
    ) -> ActiveTurnSnapshot: ...


@dataclass(frozen=True, slots=True)
class LocalExecutionSnapshot:
    revision: int
    turn: ActiveTurnSnapshot | None
    unavailable: bool

    def capture(self, binding_id: str, session_id: str | None, observations: SessionObservations) -> "LocalStatusSnapshot":
        return LocalStatusSnapshot(
            binding_id=binding_id, activity_revision=self.revision, turn=self.turn, goal=None,
            compacting=False, lifecycle=None, subscription=None, context_window_usage=None,
            local=self, session_id=session_id, observations=observations,
        )

    @property
    def label(self) -> str:
        if self.unavailable:
            return "无法确认 Claude 客户端已关闭"
        if self.turn is None:
            return "空闲"
        return {ActiveState.CLOSING: "正在收尾", ActiveState.RUNNING: "执行中", ActiveState.STOPPING: "正在停止",
                ActiveState.OBSERVATION_UNAVAILABLE: "状态待确认"}[self.turn.state]


@dataclass(frozen=True, slots=True)
class LocalStatusSnapshot(BindingRuntimeSnapshot):
    local: LocalExecutionSnapshot
    session_id: str | None
    observations: SessionObservations


class LocalSessionManagement(Protocol):
    @property
    def backend_policy(self) -> BackendPolicy: ...
    @property
    def observation_defaults(self) -> SessionObservations: ...
    def capture_status(self, binding_id: str) -> LocalStatusSnapshot: ...
    def occupying_bindings(self, directory: Path) -> frozenset[str]: ...
    async def resource_usage(self, binding_id: str) -> dict[str, str | None]: ...
    async def delete_lazy_resource(self, binding_id: str) -> ThreadBinding: ...

    def require_admission(self) -> None: ...
    def local_snapshot(self, binding_id: str) -> LocalExecutionSnapshot: ...
    async def binding_pointer_changed(self, previous_binding_id: str | None, current_binding_id: str | None) -> None: ...
    async def activate_exact(
        self, binding_id: str, *, context_anchor: MessageContextAnchor | None = None,
    ) -> ThreadBinding: ...
    async def configure_context_exact(
        self, *, binding_id: str, expected_settings_revision: int, expected_context_revision: int,
        expected_feedback_revision: int, settings: BindingTurnSettings | None, task_feedback: BindingTaskFeedback,
        message_context_mode: MentionContextMode, context_anchor: MessageContextAnchor | None,
    ) -> ThreadBinding: ...


class SessionShutdownPort(Protocol):
    def close_admission(self) -> None: ...

    async def interrupt_all(self, *, deadline: float | None = None) -> None: ...

    async def wait_idle(self, timeout: float) -> bool: ...

    async def cancel_tasks(self) -> None: ...
