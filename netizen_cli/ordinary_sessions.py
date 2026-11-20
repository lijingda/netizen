"""Netizen-owned values for ordinary session inputs and completion."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from .bindings import BindingTaskFeedback
from .file_evidence import FileEvidence
from .partial_answers import PartialAnswer
from .runtime.contracts import TurnActivitySnapshot, TurnObservationUnavailableOutcome


@dataclass(frozen=True, slots=True)
class PromptImage:
    label: str
    data_url: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class PreparedPrompt:
    text: str
    images: tuple[PromptImage, ...] = ()


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
    files: FileEvidence = FileEvidence()

    def __post_init__(self) -> None:
        if self.feedback_revision < 1:
            raise ValueError("feedback revision must be positive")
        if self.activity is not None and (
            self.activity.binding_id != self.binding_id
            or self.activity.thread_id != self.thread_id
            or self.activity.turn_id != self.turn_id
        ):
            raise ValueError("Turn outcome activity belongs to another Turn")


OrdinaryEvent = OrdinaryConclusion | TurnObservationUnavailableOutcome
OrdinaryCompletionHandler = Callable[[OrdinaryEvent], Awaitable[None]]
