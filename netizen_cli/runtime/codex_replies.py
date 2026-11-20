"""Route the existing Codex consumer into the ordinary and feature-specific sinks."""

from collections.abc import Awaitable, Callable

from ..error_messages import describe_error, native_turn_failure
from ..file_evidence import FileEvidence
from ..turn_files import completed_file_evidence
from ..ordinary_sessions import OrdinaryCompletionHandler, OrdinaryConclusion
from .codex_contracts import CompletionHandler, GoalOutcome, RuntimeOutcome
from .contracts import (
    CompactionOutcome, SideLifecycleOutcome, SideTurnOutcome, ThreadActivityDiscardedOutcome,
    TurnObservationUnavailableOutcome, TurnOutcome,
)

CodexFeatureEvent = (
    ThreadActivityDiscardedOutcome | CompactionOutcome | GoalOutcome | SideTurnOutcome | SideLifecycleOutcome
)


def native_failure_detail(result: object | None) -> str | None:
    error = getattr(result, "error", None)
    return describe_error(native_turn_failure(error)) if error is not None else None


def outcome_file_evidence(outcome: TurnOutcome | SideTurnOutcome | GoalOutcome) -> FileEvidence:
    if isinstance(outcome, GoalOutcome):
        if outcome.final_physical_turn_id is None or outcome.final_turn_status != "completed":
            return FileEvidence()
        return completed_file_evidence(
            final_items=outcome.final_items, children=outcome.patch_children, turn_diff=outcome.turn_diff,
        )
    if outcome.error is not None or outcome.status != "completed":
        return FileEvidence()
    return completed_file_evidence(
        outcome.result, children=outcome.patch_children,
        turn_diff=outcome.turn_diff if isinstance(outcome, TurnOutcome) else None,
    )


def ordinary_conclusion(turn: TurnOutcome) -> OrdinaryConclusion:
    return OrdinaryConclusion(
        binding_id=turn.binding_id, thread_id=turn.thread_id, turn_id=turn.turn_id,
        owner_id=turn.owner_id, origin=turn.origin, status=turn.status,
        final_response=turn.final_response, files=outcome_file_evidence(turn), cwd=turn.cwd,
        error_detail=describe_error(turn.error) if turn.error is not None else None,
        native_failure_detail=native_failure_detail(turn.result),
        background_cleanup_requested=turn.background_cleanup_requested,
        task_feedback=turn.task_feedback, feedback_revision=turn.feedback_revision,
        activity=turn.activity, partial_answers=turn.partial_answers,
    )


def codex_completion_handler(
    ordinary: OrdinaryCompletionHandler,
    feature: Callable[[CodexFeatureEvent], Awaitable[None]],
    projection_failed: Callable[[TurnOutcome], Awaitable[None]],
) -> CompletionHandler:
    async def dispatch(outcome: RuntimeOutcome) -> None:
        if isinstance(outcome, TurnOutcome):
            try:
                conclusion = ordinary_conclusion(outcome)
            except BaseException:
                await projection_failed(outcome)
                raise
            await ordinary(conclusion)
        elif isinstance(outcome, TurnObservationUnavailableOutcome):
            await ordinary(outcome)
        else:
            await feature(outcome)

    return dispatch
