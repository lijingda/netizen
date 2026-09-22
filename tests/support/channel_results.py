"""Native result and Activity snapshots used by Channel tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from openai_codex.types import ThreadItem
from netizen.codex_runtime import (
    ActiveState,
    GoalActivitySnapshot,
    GoalOperationState,
    SideTurnActivitySnapshot,
    TurnActivitySnapshot,
)
from netizen.sdk_gap_adapter import GoalSnapshot, GoalStatus
from netizen.turn_activity import TurnActivityEntrySnapshot
from netizen.turn_plan_observer import TurnPlanStepSnapshot


def native_goal(
    status: GoalStatus = GoalStatus.ACTIVE,
    *,
    created_at: int = 1,
) -> GoalSnapshot:
    return GoalSnapshot(
        thread_id="native-one",
        objective="ship safely",
        status=status,
        token_budget=None,
        tokens_used=10,
        time_used_seconds=2,
        created_at=created_at,
        updated_at=2,
    )


def sent_result(
    message_id: str,
    *,
    chat_id: str,
    thread_id: str | None = None,
    root_id: str | None = None,
    parent_id: str | None = None,
    success: bool = True,
    code: int = 0,
) -> object:
    return SimpleNamespace(
        success=success,
        message_id=message_id,
        chunk_ids=(),
        raw={
            "code": code,
            "data": {
                "message_id": message_id,
                "chat_id": chat_id,
                "thread_id": thread_id,
                "root_id": root_id,
                "parent_id": parent_id,
            },
        },
    )


def retryable_sent_result(*, code: int = 999_999) -> object:
    return SimpleNamespace(
        success=False,
        message_id=None,
        chunk_ids=(),
        error=SimpleNamespace(retryable=True),
        raw={"code": code, "data": None},
    )


def file_change_item(*paths: str) -> ThreadItem:
    return ThreadItem.model_validate(
        {
            "type": "fileChange",
            "id": "file-change",
            "status": "completed",
            "changes": [
                {"path": path, "diff": "", "kind": {"type": "add"}}
                for path in paths
            ],
        }
    )


def image_generation_item(path: Path) -> ThreadItem:
    return ThreadItem.model_validate(
        {
            "type": "imageGeneration",
            "id": "image-generation",
            "status": "completed",
            "result": "generated",
            "savedPath": str(path),
        }
    )


def completed_turn_result(
    *items: ThreadItem,
    final_response: str | None = "done",
) -> object:
    return SimpleNamespace(
        final_response=final_response,
        status=SimpleNamespace(value="completed"),
        items=list(items),
    )


def turn_activity_snapshot(
    *,
    binding_id: str,
    revision: int = 1,
    thread_id: str = "native-one",
    turn_id: str = "turn-one",
    state: ActiveState = ActiveState.RUNNING,
    steps: tuple[TurnPlanStepSnapshot, ...] = (),
) -> TurnActivitySnapshot:
    return TurnActivitySnapshot(
        binding_id=binding_id,
        thread_id=thread_id,
        turn_id=turn_id,
        revision=revision,
        state=state,
        steer_count=0,
        plan_available=True,
        plan_generated=bool(steps),
        plan_may_be_stale=False,
        steps=steps,
    )


def side_turn_activity_snapshot(
    *,
    side_id: str,
    revision: int = 1,
    thread_id: str = "native-side-1",
    turn_id: str = "side-turn-1",
    state: ActiveState = ActiveState.RUNNING,
    steps: tuple[TurnPlanStepSnapshot, ...] = (),
) -> SideTurnActivitySnapshot:
    return SideTurnActivitySnapshot(
        side_id=side_id,
        thread_id=thread_id,
        turn_id=turn_id,
        revision=revision,
        state=state,
        steer_count=0,
        plan_available=True,
        plan_generated=bool(steps),
        plan_may_be_stale=False,
        steps=steps,
    )


def goal_activity_snapshot(
    *,
    binding_id: str,
    revision: int = 1,
    steps: tuple[TurnPlanStepSnapshot, ...] = (),
    commentary: tuple[TurnActivityEntrySnapshot, ...] = (),
    operations: tuple[TurnActivityEntrySnapshot, ...] = (),
) -> GoalActivitySnapshot:
    return GoalActivitySnapshot(
        binding_id=binding_id,
        thread_id="native-one",
        logical_turn_id="goal-one",
        physical_turn_id="goal-turn-final",
        revision=revision,
        state=GoalOperationState.RUNNING,
        plan_available=True,
        plan_generated=bool(steps),
        steps=steps,
        commentary=commentary,
        operations=operations,
    )
