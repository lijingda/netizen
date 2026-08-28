from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from openai_codex import (
    ImageInput,
    InternalRpcError,
    InvalidRequestError,
    ServerBusyError,
    SkillInput,
    TextInput,
)
from openai_codex.types import (
    Notification,
    ThreadItem,
    ThreadTokenUsageUpdatedNotification,
)

from netizen.bindings import (
    BindingConflict,
    BindingNotFound,
    BindingStore,
    BindingTaskFeedback,
    BindingTurnSettings,
    SideTopicState,
)
from netizen.codex_runtime import (
    ActiveState,
    ExternalGoalActive,
    GoalOutcome,
    GoalOperationState,
    GoalStateUnknown,
    NativeThreadCatalogState,
    NativeThreadMetadata,
    CompactionFailed,
    CompactionOutcome,
    CompactionStateUnknown,
    ContextWindowUsage,
    ContextBoundaryCommitFailed,
    ContextCursorCommit,
    CodexRuntime,
    RuntimeClosed,
    ReleaseDisposition,
    SideCloseFailed,
    SideLifecycleOutcome,
    SideSessionClosing,
    SideSessionState,
    SideStartFailed,
    SideTurnOutcome,
    SkillReferenceError,
    SubmissionAdmission,
    SteerRace,
    StopDisposition,
    SubmitDisposition,
    TerminalCleanupFailed,
    TerminalStateUnknown,
    ThreadCompactStartFailed,
    ThreadCompacting,
    ThreadDeleteUnavailable,
    ThreadDeleteTargetChanged,
    ThreadGoalActive,
    ThreadBackgroundTerminalsActive,
    ThreadCatalogDeadlineExceeded,
    ThreadCatalogLimitExceeded,
    ThreadLifecycleError,
    ThreadLifecycleState,
    ThreadLifecycleStateUnknown,
    ThreadNotMaterialized,
    ThreadReleaseError,
    ThreadRunningConfiguration,
    ThreadSubscriptionState,
    ThreadStopping,
    TurnInterruptFailed,
    TurnStartFailed,
    TurnOutcome,
)
from netizen.domain import (
    FeishuScope,
    MentionContextMode,
    MessageContextAnchor,
    ScopeKind,
)
from netizen.model_settings import ModelCatalogError
from netizen.sdk_gap_adapter import (
    DiscoveredSkill,
    GoalPauseAck,
    GoalSnapshot,
    GoalStatus,
    GoalStreamTerminal,
    SkillCatalogSnapshot,
    ThreadUnsubscribeStateUnknown,
    ThreadUnsubscribeStatus,
)
from netizen.turn_plan_observer import (
    TurnPlanObservation,
    TurnPlanStepSnapshot,
    TurnPlanStepState,
)


@dataclass
class FakeStatus:
    value: str


_STREAM_END = object()


class FakeTurnHandle:
    def __init__(
        self,
        thread_id: str,
        turn_id: str,
        events: list[tuple[str, str]],
        *,
        allow_run: bool = False,
    ) -> None:
        self.thread_id = thread_id
        self.id = turn_id
        self.events = events
        self.steers: list[object] = []
        self.interrupt_count = 0
        self.complete_on_interrupt = True
        self.interrupt_errors: list[BaseException] = []
        self.record = SimpleNamespace(
            id=turn_id,
            status=FakeStatus("inProgress"),
            error=None,
            started_at=1,
            completed_at=None,
            duration_ms=None,
            items=[],
        )
        self.steer_error: BaseException | None = None
        self.steer_gate: asyncio.Event | None = None
        self.steer_started = asyncio.Event()
        self.run_calls = 0
        self.stream_calls = 0
        self.notifications: asyncio.Queue[object] = asyncio.Queue()
        self.allow_run = allow_run

    async def steer(self, input: object) -> object:
        if self.steer_error is not None:
            raise self.steer_error
        self.steers.append(input)
        self.steer_started.set()
        if self.steer_gate is not None:
            await self.steer_gate.wait()
        return object()

    async def interrupt(self) -> object:
        self.events.append(("interrupt", self.thread_id))
        self.interrupt_count += 1
        if self.interrupt_errors:
            raise self.interrupt_errors.pop(0)
        if self.complete_on_interrupt:
            self.record.status.value = "interrupted"
            self.record.completed_at = 2
            self.notifications.put_nowait(_STREAM_END)
        return object()

    async def run(self) -> object:
        self.run_calls += 1
        if not self.allow_run:
            raise AssertionError(
                "ordinary CodexRuntime must not consume completion through handle.run()"
            )
        while True:
            notification = await self.notifications.get()
            if notification is _STREAM_END:
                final_response = None
                if self.record.items:
                    final_response = self.record.items[-1].root.text
                return SimpleNamespace(
                    status=self.record.status,
                    final_response=final_response,
                )
            if isinstance(notification, BaseException):
                raise notification

    async def stream(self):
        self.stream_calls += 1
        while True:
            notification = await self.notifications.get()
            if notification is _STREAM_END:
                return
            if isinstance(notification, BaseException):
                raise notification
            yield notification

    def complete(self, status: str = "completed", response: str | None = None) -> None:
        self.record.status.value = status
        self.record.completed_at = 2
        if status == "completed":
            self.record.items = [
                SimpleNamespace(
                    root=SimpleNamespace(
                        type="agentMessage",
                        text=response or f"done:{self.id}",
                        phase=FakeStatus("final_answer"),
                    )
                )
            ]
        self.notifications.put_nowait(_STREAM_END)

    def fail(self, message: str) -> None:
        self.record.status.value = "failed"
        self.record.error = SimpleNamespace(message=message)
        self.record.completed_at = 2
        self.notifications.put_nowait(_STREAM_END)


def token_usage_notification(
    *,
    thread_id: str = "native-1",
    turn_id: str = "turn-1",
    used_tokens: int = 25_000,
    lifetime_tokens: int = 90_000,
    context_window_tokens: int | None = 100_000,
) -> Notification:
    def breakdown(total_tokens: int) -> dict[str, int]:
        return {
            "cachedInputTokens": 1_000,
            "inputTokens": total_tokens - 2_000,
            "outputTokens": 1_000,
            "reasoningOutputTokens": 1_000,
            "totalTokens": total_tokens,
        }

    payload = ThreadTokenUsageUpdatedNotification.model_validate(
        {
            "threadId": thread_id,
            "turnId": turn_id,
            "tokenUsage": {
                "last": breakdown(used_tokens),
                "total": breakdown(lifetime_tokens),
                "modelContextWindow": context_window_tokens,
            },
        }
    )
    return Notification("thread/tokenUsage/updated", payload)


def turn_diff_notification(
    diff: str,
    *,
    thread_id: str = "native-1",
    turn_id: str = "turn-1",
) -> Notification:
    return Notification(
        "turn/diff/updated",
        SimpleNamespace(
            thread_id=thread_id,
            turn_id=turn_id,
            diff=diff,
        ),
    )


class FakeThread:
    def __init__(
        self,
        thread_id: str,
        codex: "FakeCodex",
        *,
        ephemeral: bool = False,
        forked_from_id: str | None = None,
    ) -> None:
        self.id = thread_id
        self.codex = codex
        self.ephemeral = ephemeral
        self.forked_from_id = forked_from_id

    async def turn(self, input: object, **kwargs: object) -> FakeTurnHandle:
        entered = self.codex.turn_entered.get(self.id)
        if entered is not None:
            entered.set()
        gate = self.codex.turn_gates.get(self.id)
        if gate is not None:
            await gate.wait()
        handle_thread_id = self.codex.handle_thread_id_override or self.id
        handle = FakeTurnHandle(
            handle_thread_id,
            f"turn-{len(self.codex.handles) + 1}",
            self.codex.events,
            allow_run=self.ephemeral,
        )
        self.codex.turn_inputs.append((self.id, input))
        self.codex.turn_calls.append((self.id, input, kwargs))
        self.codex.handles.append(handle)
        if self.codex.turn_errors_after_start:
            raise self.codex.turn_errors_after_start.pop(0)
        if self.codex.complete_immediately:
            handle.complete()
        return handle

    async def compact(self) -> object:
        self.codex.compact_calls.append(self.id)
        record = SimpleNamespace(
            id=f"compact-{len(self.codex.compact_records) + 1}",
            status=FakeStatus("inProgress"),
            error=None,
            started_at=1,
            completed_at=None,
            duration_ms=None,
            items=(
                [
                    SimpleNamespace(
                        root=SimpleNamespace(type="contextCompaction")
                    )
                ]
                if self.codex.compact_item_visible
                else []
            ),
        )
        self.codex.compact_records.append((self.id, record))
        if self.codex.compact_errors_after_start:
            raise self.codex.compact_errors_after_start.pop(0)
        if self.codex.complete_compact_immediately:
            record.status.value = "completed"
            record.completed_at = 2
        return object()

    async def set_name(self, name: str) -> object:
        self.codex.set_name_calls.append((self.id, name))
        if self.codex.set_name_errors:
            raise self.codex.set_name_errors.pop(0)
        return object()

    async def read(self, *, include_turns: bool = False) -> object:
        self.codex.read_calls.append((self.id, include_turns))
        if include_turns and self.codex.full_read_errors:
            raise self.codex.full_read_errors.pop(0)
        if self.codex.read_errors:
            raise self.codex.read_errors.pop(0)
        if self.codex.read_gate is not None:
            await self.codex.read_gate.wait()
        turns = [
            handle.record
            for handle in self.codex.handles
            if handle.thread_id == self.id
        ]
        turns.extend(
            record
            for thread_id, record in self.codex.compact_records
            if thread_id == self.id
        )
        turns.extend(
            record
            for thread_id, record in self.codex.goal_turns
            if thread_id == self.id
        )
        if include_turns and self.codex.omit_agent_items_full_reads > 0:
            self.codex.omit_agent_items_full_reads -= 1
            turns = [
                SimpleNamespace(
                    id=turn.id,
                    status=turn.status,
                    error=turn.error,
                    started_at=turn.started_at,
                    completed_at=turn.completed_at,
                    duration_ms=turn.duration_ms,
                    items=[],
                )
                for turn in turns
            ]
        if self.codex.read_statuses:
            status_type = self.codex.read_statuses.pop(0)
        else:
            status_type = (
                "active"
                if any(turn.status.value == "inProgress" for turn in turns)
                else "idle"
            )
        return SimpleNamespace(
            thread=SimpleNamespace(
                id=self.id,
                status=SimpleNamespace(root=SimpleNamespace(type=status_type)),
                turns=turns if include_turns else [],
                ephemeral=self.ephemeral,
                forked_from_id=self.forked_from_id,
                path=f"/tmp/{self.id}.jsonl",
            )
        )


class FakeCodex:
    def __init__(self) -> None:
        self.next_thread = 1
        self.start_kwargs: list[dict[str, object]] = []
        self.resume_calls: list[tuple[str, dict[str, object]]] = []
        self.turn_inputs: list[tuple[str, object]] = []
        self.turn_calls: list[tuple[str, object, dict[str, object]]] = []
        self.handles: list[FakeTurnHandle] = []
        self.complete_immediately = False
        self.read_calls: list[tuple[str, bool]] = []
        self.read_errors: list[BaseException] = []
        self.full_read_errors: list[BaseException] = []
        self.omit_agent_items_full_reads = 0
        self.read_statuses: list[str] = []
        self.read_gate: asyncio.Event | None = None
        self.events: list[tuple[str, str]] = []
        self.handle_thread_id_override: str | None = None
        self.resume_thread_id_override: str | None = None
        self.start_errors: list[BaseException] = []
        self.resume_errors: list[BaseException] = []
        self.turn_errors_after_start: list[BaseException] = []
        self.compact_calls: list[str] = []
        self.compact_records: list[tuple[str, SimpleNamespace]] = []
        self.compact_errors_after_start: list[BaseException] = []
        self.complete_compact_immediately = False
        self.compact_item_visible = True
        self.goal_turns: list[tuple[str, SimpleNamespace]] = []
        self.model_response: object = SimpleNamespace(data=[])
        self.model_calls = 0
        self.thread_list_calls: list[dict[str, object]] = []
        self.thread_list_pages: list[object] = [
            SimpleNamespace(data=[], next_cursor=None)
        ]
        self.set_name_calls: list[tuple[str, str]] = []
        self.set_name_errors: list[BaseException] = []
        self.archive_calls: list[str] = []
        self.archive_errors: list[BaseException] = []
        self.unarchive_calls: list[str] = []
        self.unarchive_errors: list[BaseException] = []
        self.fork_calls: list[tuple[str, dict[str, object]]] = []
        self.fork_errors: list[BaseException] = []
        self.turn_entered: dict[str, asyncio.Event] = {}
        self.turn_gates: dict[str, asyncio.Event] = {}

    def finish_compaction(
        self,
        *,
        status: str = "completed",
        message: str | None = None,
    ) -> None:
        record = self.compact_records[-1][1]
        record.status.value = status
        record.completed_at = 2
        if status == "failed":
            record.error = SimpleNamespace(message=message or "compact failed")

    async def thread_start(self, **kwargs: object) -> FakeThread:
        self.start_kwargs.append(kwargs)
        if self.start_errors:
            raise self.start_errors.pop(0)
        thread = FakeThread(f"native-{self.next_thread}", self)
        self.next_thread += 1
        return thread

    async def thread_resume(self, thread_id: str, **kwargs: object) -> FakeThread:
        self.resume_calls.append((thread_id, kwargs))
        if self.resume_errors:
            raise self.resume_errors.pop(0)
        return FakeThread(self.resume_thread_id_override or thread_id, self)

    async def thread_list(self, **kwargs: object) -> object:
        self.thread_list_calls.append(kwargs)
        return self.thread_list_pages.pop(0)

    async def thread_archive(self, thread_id: str) -> object:
        self.archive_calls.append(thread_id)
        if self.archive_errors:
            raise self.archive_errors.pop(0)
        return object()

    async def thread_unarchive(self, thread_id: str) -> FakeThread:
        self.unarchive_calls.append(thread_id)
        if self.unarchive_errors:
            raise self.unarchive_errors.pop(0)
        return FakeThread(thread_id, self)

    async def thread_fork(
        self,
        thread_id: str,
        **kwargs: object,
    ) -> FakeThread:
        self.fork_calls.append((thread_id, kwargs))
        if self.fork_errors:
            raise self.fork_errors.pop(0)
        return FakeThread(
            f"side-{len(self.fork_calls)}",
            self,
            ephemeral=True,
            forked_from_id=thread_id,
        )

    async def models(self, *, include_hidden: bool = False) -> object:
        self.model_calls += 1
        self.last_include_hidden = include_hidden
        return self.model_response


class FakeTerminalCleanup:
    def __init__(self, events: list[tuple[str, str]]) -> None:
        self.events = events
        self.calls: list[str] = []
        self.failures: list[BaseException] = []

    async def clean_thread(self, thread_id: str) -> None:
        self.events.append(("cleanup", thread_id))
        self.calls.append(thread_id)
        if self.failures:
            raise self.failures.pop(0)


class FakeSideThreadControl:
    def __init__(self) -> None:
        self.inject_calls: list[str] = []
        self.unsubscribe_calls: list[str] = []
        self.inject_failures: list[BaseException] = []
        self.unsubscribe_failures: list[BaseException] = []
        self.unsubscribe_status = ThreadUnsubscribeStatus.UNSUBSCRIBED

    async def inject_boundary(self, thread_id: str) -> None:
        self.inject_calls.append(thread_id)
        if self.inject_failures:
            raise self.inject_failures.pop(0)

    async def unsubscribe(self, thread_id: str) -> ThreadUnsubscribeStatus:
        self.unsubscribe_calls.append(thread_id)
        if self.unsubscribe_failures:
            raise self.unsubscribe_failures.pop(0)
        return self.unsubscribe_status


class FakeThreadSubscriptionControl:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.statuses: list[ThreadUnsubscribeStatus] = []
        self.errors: list[BaseException] = []
        self.entered = asyncio.Event()
        self.gate: asyncio.Event | None = None

    async def unsubscribe(self, thread_id: str) -> ThreadUnsubscribeStatus:
        self.calls.append(thread_id)
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.errors:
            raise self.errors.pop(0)
        if self.statuses:
            return self.statuses.pop(0)
        return ThreadUnsubscribeStatus.UNSUBSCRIBED


class FakeBackgroundTerminalInspector:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.results: list[bool] = []
        self.errors: list[BaseException] = []

    async def has_running(self, thread_id: str) -> bool:
        self.calls.append(thread_id)
        if self.errors:
            raise self.errors.pop(0)
        if self.results:
            return self.results.pop(0)
        return False


class FakeSkillCatalog:
    def __init__(self, skills: tuple[DiscoveredSkill, ...]) -> None:
        self.skills = skills
        self.errors: tuple[str, ...] = ()
        self.calls: list[tuple[Path, bool]] = []
        self.called = asyncio.Event()
        self.gate: asyncio.Event | None = None

    async def list(
        self,
        cwd: Path,
        *,
        force_reload: bool = True,
    ) -> SkillCatalogSnapshot:
        self.calls.append((cwd, force_reload))
        self.called.set()
        if self.gate is not None:
            await self.gate.wait()
        return SkillCatalogSnapshot(cwd.resolve(), self.skills, self.errors)


def fake_skills() -> tuple[DiscoveredSkill, ...]:
    return (
        DiscoveredSkill(
            "code-review",
            "/tmp/code-review/SKILL.md",
            "Review code",
            "repo",
            True,
        ),
        DiscoveredSkill(
            "test-triage",
            "/tmp/test-triage/SKILL.md",
            "Triage tests",
            "user",
            True,
        ),
    )


def goal_snapshot(
    status: GoalStatus,
    *,
    objective: str = "ship safely",
) -> GoalSnapshot:
    return GoalSnapshot(
        thread_id="native-1",
        objective=objective,
        status=status,
        token_budget=None,
        tokens_used=1,
        time_used_seconds=1,
        created_at=1,
        updated_at=2,
    )


class FakeGoalHandle:
    def __init__(self, control: "FakeGoalControl", turn_id: str) -> None:
        self.control = control
        self.thread_id = "native-1"
        self.id = turn_id
        self.terminal = asyncio.Event()
        self.closed = False
        self.pause_calls = 0
        self.pause_errors: list[BaseException] = []
        self.record = SimpleNamespace(
            id=turn_id,
            status=FakeStatus("inProgress"),
            error=None,
            started_at=1,
            completed_at=None,
            duration_ms=None,
            items=[],
        )
        control.codex.goal_turns.append((self.thread_id, self.record))

    def current_physical_turn_id(self) -> str | None:
        return self.id if self.record.status.value == "inProgress" else None

    async def wait_terminal(self) -> GoalStreamTerminal:
        await self.terminal.wait()
        return GoalStreamTerminal(
            self.id,
            self.id,
            self.record.status.value,
        )

    async def pause(self) -> GoalPauseAck:
        self.pause_calls += 1
        if self.pause_errors:
            raise self.pause_errors.pop(0)
        self.control.persisted = goal_snapshot(
            GoalStatus.PAUSED,
            objective=self.control.persisted.objective,
        )
        self.record.status.value = "interrupted"
        self.record.completed_at = 2
        self.terminal.set()
        return GoalPauseAck(self.control.persisted, self.id, True)

    async def aclose(self) -> None:
        self.closed = True
        self.terminal.set()

    def finish(
        self,
        *,
        goal_status: GoalStatus = GoalStatus.COMPLETE,
        turn_status: str = "completed",
        response: str = "goal done",
    ) -> None:
        self.control.persisted = goal_snapshot(
            goal_status,
            objective=self.control.persisted.objective,
        )
        self.record.status.value = turn_status
        self.record.completed_at = 2
        self.record.items = [
            SimpleNamespace(
                root=SimpleNamespace(
                    type="agentMessage",
                    text=response,
                    phase=FakeStatus("final_answer"),
                )
            )
        ]
        self.terminal.set()


class FakeGoalControl:
    def __init__(self, codex: FakeCodex) -> None:
        self.codex = codex
        self.persisted: GoalSnapshot | None = None
        self.handles: list[FakeGoalHandle] = []
        self.get_calls: list[str] = []
        self.start_calls: list[tuple[str, str]] = []
        self.resume_calls: list[str] = []
        self.clear_calls: list[str] = []
        self.start_errors: list[BaseException] = []

    async def get(self, thread_id: str) -> GoalSnapshot | None:
        self.get_calls.append(thread_id)
        return self.persisted

    async def start(self, thread_id: str, objective: str) -> FakeGoalHandle:
        self.start_calls.append((thread_id, objective))
        if self.start_errors:
            raise self.start_errors.pop(0)
        self.persisted = goal_snapshot(GoalStatus.ACTIVE, objective=objective)
        handle = FakeGoalHandle(self, f"goal-turn-{len(self.handles) + 1}")
        self.handles.append(handle)
        return handle

    async def resume(self, thread_id: str) -> FakeGoalHandle:
        self.resume_calls.append(thread_id)
        assert self.persisted is not None
        self.persisted = goal_snapshot(
            GoalStatus.ACTIVE,
            objective=self.persisted.objective,
        )
        handle = FakeGoalHandle(self, f"goal-turn-{len(self.handles) + 1}")
        self.handles.append(handle)
        return handle

    async def clear(self, thread_id: str) -> bool:
        self.clear_calls.append(thread_id)
        self.persisted = None
        return True


class FakeThreadDeleteControl:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.errors: list[BaseException] = []

    async def delete(self, thread_id: str) -> None:
        self.calls.append(thread_id)
        if self.errors:
            raise self.errors.pop(0)


class FakeTurnPlanObserver:
    def __init__(self) -> None:
        self.events: dict[
            str,
            list[tuple[str, tuple[TurnPlanStepSnapshot, ...]]],
        ] = {}
        self.calls: list[tuple[str, str, int]] = []
        self.error: BaseException | None = None

    def append(
        self,
        *,
        thread_id: str,
        turn_id: str,
        steps: tuple[TurnPlanStepSnapshot, ...],
    ) -> None:
        self.events.setdefault(turn_id, []).append((thread_id, steps))

    def observe(
        self,
        *,
        thread_id: str,
        turn_id: str,
        after_cursor: int,
    ) -> TurnPlanObservation:
        self.calls.append((thread_id, turn_id, after_cursor))
        if self.error is not None:
            raise self.error
        events = self.events.get(turn_id, [])
        latest_steps: tuple[TurnPlanStepSnapshot, ...] = ()
        latest_cursor: int | None = None
        for cursor, (event_thread_id, steps) in enumerate(events, start=1):
            if cursor <= after_cursor or event_thread_id != thread_id:
                continue
            latest_steps = steps
            latest_cursor = cursor
        return TurnPlanObservation(
            next_cursor=len(events),
            plan_updated=latest_cursor is not None,
            plan_cursor=latest_cursor,
            steps=latest_steps,
        )


class SideRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.next_id = 0

        def make_id() -> str:
            self.next_id += 1
            return f"record-{self.next_id}"

        self.store = BindingStore(id_factory=make_id)
        self.codex = FakeCodex()
        self.cleanup = FakeTerminalCleanup(self.codex.events)
        self.side_control = FakeSideThreadControl()
        self.outcomes: list[object] = []

        async def capture(outcome: object) -> None:
            self.outcomes.append(outcome)

        self.runtime = CodexRuntime(
            codex=self.codex,
            bindings=self.store,
            terminal_cleanup=self.cleanup,
            side_boundary_control=self.side_control,
            thread_subscription_control=self.side_control,
            on_completion=capture,
            poll_interval_seconds=0,
            side_idle_seconds=60,
        )
        self.cwd_context = tempfile.TemporaryDirectory()
        self.cwd = Path(self.cwd_context.name)
        self.scope = FeishuScope("cli_test", "oc_side", ScopeKind.DIRECT)

    async def asyncTearDown(self) -> None:
        try:
            await self.runtime.interrupt_all()
        except BaseException:
            pass
        if not await self.runtime.wait_idle(timeout=0.1):
            await self.runtime.cancel_tasks()
        else:
            await self.runtime.cancel_tasks()
        self.store.close()
        self.cwd_context.cleanup()

    def materialized_binding(self):
        binding = self.store.create_binding(
            scope=self.scope,
            project_alias="test",
            creator_id="ou_owner",
        )
        self.store.assign_native_thread_id(binding.id, "native-parent")
        return self.store.get(binding.id)

    async def open_side(self):
        binding = self.materialized_binding()
        return await self.open_side_for_binding(binding)

    async def open_side_for_binding(self, binding, *, source: str | None = None):
        record = self.store.create_side_topic(
            app_id=self.scope.app_id,
            chat_id=self.scope.chat_id,
            source_message_id=source or f"om-source-{self.next_id}",
            parent_binding_id=binding.id,
            creator_id="ou_owner",
            requires_mention=False,
        )
        snapshot = await self.runtime.create_side(
            side_id=record.id,
            binding=binding,
            cwd=self.cwd,
            creator_id="ou_owner",
        )
        record = self.store.set_side_topic_root(record.id, f"om-root-{record.id}")
        topic_id = f"omt-{record.id}"
        snapshot = await self.runtime.attach_side_topic(
            side_id=record.id,
            topic_id=topic_id,
            root_message_id=record.root_message_id or "",
        )
        record = self.store.open_side_topic(record.id, topic_id)
        return binding, record, snapshot

    def install_model_catalog(self) -> object:
        effort = SimpleNamespace(value="dynamic-effort")
        self.codex.model_response = SimpleNamespace(
            data=[
                SimpleNamespace(
                    id="catalog-model",
                    model="wire-model",
                    display_name="Catalog Model",
                    description="dynamic",
                    is_default=True,
                    default_reasoning_effort=effort,
                    default_service_tier=None,
                    supported_reasoning_efforts=[
                        SimpleNamespace(
                            reasoning_effort=effort,
                            description="dynamic effort",
                        )
                    ],
                    service_tiers=[
                        SimpleNamespace(
                            id="priority-v2",
                            name="Fast v2",
                            description="dynamic tier",
                        )
                    ],
                )
            ],
            next_cursor=None,
        )
        return effort

    async def finish_side_turn(
        self,
        submission,
        *,
        response: str = "done",
    ) -> None:
        handle = next(
            handle
            for handle in reversed(self.codex.handles)
            if handle.id == submission.turn_id
        )
        handle.complete(response=response)
        release = submission.release_receipt_attempt
        assert release is not None
        release()
        self.assertTrue(await self.runtime.wait_idle(timeout=0.2))

    async def test_create_uses_exact_ephemeral_fork_and_fixed_boundary(self) -> None:
        binding, record, snapshot = await self.open_side()

        self.assertEqual(
            self.codex.fork_calls,
            [(binding.native_thread_id, {"ephemeral": True})],
        )
        self.assertEqual(self.side_control.inject_calls, [snapshot.thread_id])
        self.assertEqual(snapshot.parent_binding_id, binding.id)
        self.assertEqual(snapshot.topic_id, record.topic_id)
        self.assertEqual(snapshot.state, SideSessionState.OPEN)

    async def test_three_turns_reuse_one_thread_and_running_input_steers(self) -> None:
        _binding, record, snapshot = await self.open_side()

        first = await self.runtime.submit_side(
            side_id=record.id,
            input="first",
            owner_id="ou_one",
            origin=SimpleNamespace(message_id="om-first"),
        )
        steered = await self.runtime.submit_side(
            side_id=record.id,
            input="adjust",
            owner_id="ou_two",
            origin=SimpleNamespace(message_id="om-steer"),
        )
        self.assertEqual(steered.disposition, SubmitDisposition.STEERED)
        self.assertEqual(self.codex.handles[-1].steers, ["adjust"])
        await self.finish_side_turn(first, response="one")

        second = await self.runtime.submit_side(
            side_id=record.id,
            input="second",
            owner_id="ou_one",
            origin=SimpleNamespace(message_id="om-second"),
        )
        await self.finish_side_turn(second, response="two")
        third = await self.runtime.submit_side(
            side_id=record.id,
            input="third",
            owner_id="ou_one",
            origin=SimpleNamespace(message_id="om-third"),
        )
        await self.finish_side_turn(third, response="three")

        self.assertEqual(
            [thread_id for thread_id, _input, _kwargs in self.codex.turn_calls],
            [snapshot.thread_id] * 3,
        )
        side_outcomes = [
            outcome
            for outcome in self.outcomes
            if isinstance(outcome, SideTurnOutcome)
        ]
        self.assertEqual([item.final_response for item in side_outcomes], ["one", "two", "three"])
        self.assertTrue(all(handle.run_calls == 1 for handle in self.codex.handles))
        self.assertTrue(all(handle.stream_calls == 0 for handle in self.codex.handles))

    async def test_side_submission_admission_rejects_idle_running_idle_aba(self) -> None:
        _binding, record, _snapshot = await self.open_side()
        stale = await self.runtime.capture_side_submission_admission(record.id)
        first = await self.runtime.submit_side(
            side_id=record.id,
            input="first",
            owner_id="ou_owner",
            origin=SimpleNamespace(message_id="om-first"),
        )
        await self.finish_side_turn(first)

        with self.assertRaises(SteerRace):
            await self.runtime.submit_side(
                side_id=record.id,
                input="stale",
                owner_id="ou_owner",
                origin=SimpleNamespace(message_id="om-stale"),
                admission=stale,
            )
        self.assertEqual(len(self.codex.turn_calls), 1)

    async def test_side_submission_rechecks_global_admission_inside_lock(self) -> None:
        _binding, record, _snapshot = await self.open_side()
        admission = await self.runtime.capture_side_submission_admission(record.id)
        lock = self.runtime._side_lock(record.id)
        await lock.acquire()
        try:
            pending = asyncio.create_task(
                self.runtime.submit_side(
                    side_id=record.id,
                    input="must not start",
                    owner_id="ou_owner",
                    origin=SimpleNamespace(message_id="om-late"),
                    admission=admission,
                )
            )
            await asyncio.sleep(0)
            self.runtime.close_admission()
        finally:
            lock.release()

        with self.assertRaises(RuntimeClosed):
            await pending
        self.assertEqual(self.codex.turn_calls, [])

    async def test_cancelled_side_steer_closes_service_admission(self) -> None:
        _binding, record, _snapshot = await self.open_side()
        first = await self.runtime.submit_side(
            side_id=record.id,
            input="first",
            owner_id="ou_owner",
            origin=SimpleNamespace(message_id="om-first"),
        )
        assert first.release_receipt_attempt is not None
        first.release_receipt_attempt()
        handle = self.codex.handles[-1]
        handle.steer_gate = asyncio.Event()
        steering = asyncio.create_task(
            self.runtime.submit_side(
                side_id=record.id,
                input="unknown steer",
                owner_id="ou_owner",
                origin=SimpleNamespace(message_id="om-steer"),
            )
        )
        await handle.steer_started.wait()
        steering.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await steering
        self.assertEqual(
            self.runtime.side_snapshot(record.id).state,
            SideSessionState.CLOSING,
        )
        with self.assertRaises(RuntimeClosed):
            await self.runtime.capture_side_submission_admission(record.id)

    async def test_stop_cleans_current_turn_but_side_accepts_another_turn(self) -> None:
        _binding, record, snapshot = await self.open_side()
        first = await self.runtime.submit_side(
            side_id=record.id,
            input="first",
            owner_id="ou_owner",
            origin=SimpleNamespace(message_id="om-first"),
        )
        assert first.release_receipt_attempt is not None
        first.release_receipt_attempt()

        self.assertEqual(
            await self.runtime.stop_side(record.id),
            StopDisposition.REQUESTED,
        )
        self.assertTrue(await self.runtime.wait_idle(timeout=0.2))
        self.assertEqual(self.cleanup.calls, [snapshot.thread_id])
        self.assertIsNone(self.runtime.side_snapshot(record.id).turn_id)

        second = await self.runtime.submit_side(
            side_id=record.id,
            input="after stop",
            owner_id="ou_owner",
            origin=SimpleNamespace(message_id="om-second"),
        )
        await self.finish_side_turn(second)
        self.assertEqual(len(self.codex.turn_calls), 2)

    async def test_stop_cleanup_failure_retry_releases_the_active_slot(self) -> None:
        _binding, record, snapshot = await self.open_side()
        submission = await self.runtime.submit_side(
            side_id=record.id,
            input="first",
            owner_id="ou_owner",
            origin=SimpleNamespace(message_id="om-first"),
        )
        assert submission.release_receipt_attempt is not None
        submission.release_receipt_attempt()
        self.cleanup.failures.append(RuntimeError("cleanup response lost"))

        with self.assertRaises(TerminalCleanupFailed):
            await self.runtime.stop_side(record.id)
        self.assertFalse(await self.runtime.wait_idle(timeout=0.02))
        self.assertEqual(self.runtime.side_snapshot(record.id).turn_id, "turn-1")

        self.assertEqual(
            await self.runtime.stop_side(record.id),
            StopDisposition.REQUESTED,
        )
        self.assertTrue(await self.runtime.wait_idle(timeout=0.2))
        self.assertEqual(
            self.cleanup.calls,
            [snapshot.thread_id, snapshot.thread_id],
        )
        self.assertIsNone(self.runtime.side_snapshot(record.id).turn_id)

    async def test_repeat_interrupt_rejection_after_exact_terminal_still_cleans(
        self,
    ) -> None:
        _binding, record, snapshot = await self.open_side()
        submission = await self.runtime.submit_side(
            side_id=record.id,
            input="first",
            owner_id="ou_owner",
            origin=SimpleNamespace(message_id="om-first"),
        )
        assert submission.release_receipt_attempt is not None
        submission.release_receipt_attempt()
        handle = self.codex.handles[-1]
        handle.interrupt_errors.extend(
            (RuntimeError("response lost"), RuntimeError("already terminal"))
        )

        with self.assertRaises(TurnInterruptFailed):
            await self.runtime.stop_side(record.id)
        handle.complete(status="interrupted")
        async with asyncio.timeout(0.1):
            while not self.runtime._sides[record.id].active.terminal_observed:
                await asyncio.sleep(0)

        self.assertEqual(
            await self.runtime.stop_side(record.id),
            StopDisposition.REQUESTED,
        )
        self.assertTrue(await self.runtime.wait_idle(timeout=0.2))
        self.assertEqual(self.cleanup.calls, [snapshot.thread_id])
        self.assertIsNone(self.runtime.side_snapshot(record.id).turn_id)

    async def test_side_turn_start_response_loss_closes_service_without_release(
        self,
    ) -> None:
        _binding, record, snapshot = await self.open_side()
        self.codex.turn_errors_after_start.append(RuntimeError("response lost"))

        with self.assertRaisesRegex(SideStartFailed, "请重启服务"):
            await self.runtime.submit_side(
                side_id=record.id,
                input="unknown",
                owner_id="ou_owner",
                origin=SimpleNamespace(message_id="om-unknown"),
            )

        self.assertEqual(len(self.codex.handles), 1)
        self.assertEqual(self.codex.handles[0].record.status.value, "inProgress")
        self.assertEqual(
            self.runtime.side_snapshot(record.id).state,
            SideSessionState.CLOSING,
        )
        self.assertEqual(self.store.get_side_topic(record.id).state, SideTopicState.OPEN)
        self.assertEqual(self.cleanup.calls, [])
        self.assertEqual(self.side_control.unsubscribe_calls, [])
        with self.assertRaisesRegex(SideCloseFailed, "请重启服务"):
            await self.runtime.close_side(record.id)
        self.assertEqual(self.cleanup.calls, [])
        self.assertEqual(self.side_control.unsubscribe_calls, [])
        self.assertEqual(self.store.get_side_topic(record.id).state, SideTopicState.OPEN)
        with self.assertRaises(RuntimeClosed):
            await self.runtime.capture_side_submission_admission(record.id)
        self.assertEqual(snapshot.thread_id, self.runtime.side_snapshot(record.id).thread_id)

    async def test_idle_close_cleans_unsubscribes_and_leaves_tombstone(self) -> None:
        _binding, record, snapshot = await self.open_side()

        outcome = await self.runtime.close_side(record.id)

        self.assertEqual(outcome.state, SideTopicState.CLOSED)
        self.assertEqual(self.cleanup.calls, [snapshot.thread_id])
        self.assertEqual(self.side_control.unsubscribe_calls, [snapshot.thread_id])
        self.assertEqual(
            self.store.get_side_topic(record.id).state,
            SideTopicState.CLOSED,
        )
        with self.assertRaisesRegex(LookupError, record.id):
            self.runtime.side_snapshot(record.id)

    async def test_close_drain_timeout_keeps_native_turn_and_route_open(self) -> None:
        _binding, record, _snapshot = await self.open_side()
        submission = await self.runtime.submit_side(
            side_id=record.id,
            input="still running",
            owner_id="ou_owner",
            origin=SimpleNamespace(message_id="om-running"),
        )
        assert submission.release_receipt_attempt is not None
        submission.release_receipt_attempt()
        handle = self.codex.handles[-1]
        handle.complete_on_interrupt = False

        with patch(
            "netizen.codex_runtime._SIDE_CLOSE_DRAIN_TIMEOUT_SECONDS",
            0.01,
        ), self.assertRaisesRegex(SideCloseFailed, "终态未确认"):
            await self.runtime.close_side(record.id)

        self.assertEqual(handle.record.status.value, "inProgress")
        self.assertEqual(self.cleanup.calls, [])
        self.assertEqual(self.side_control.unsubscribe_calls, [])
        self.assertEqual(self.store.get_side_topic(record.id).state, SideTopicState.OPEN)
        self.assertEqual(
            self.runtime.side_snapshot(record.id).state,
            SideSessionState.CLOSING,
        )

        handle.complete(status="interrupted")
        outcome = await self.runtime.close_side(record.id)
        self.assertEqual(outcome.state, SideTopicState.CLOSED)

    async def test_close_takes_over_stop_cleanup_debt(self) -> None:
        _binding, record, snapshot = await self.open_side()
        submission = await self.runtime.submit_side(
            side_id=record.id,
            input="first",
            owner_id="ou_owner",
            origin=SimpleNamespace(message_id="om-first"),
        )
        assert submission.release_receipt_attempt is not None
        submission.release_receipt_attempt()
        self.cleanup.failures.append(RuntimeError("cleanup response lost"))

        with self.assertRaises(TerminalCleanupFailed):
            await self.runtime.stop_side(record.id)
        outcome = await self.runtime.close_side(record.id)

        self.assertEqual(outcome.state, SideTopicState.CLOSED)
        self.assertEqual(
            self.cleanup.calls,
            [snapshot.thread_id, snapshot.thread_id],
        )
        self.assertEqual(self.side_control.unsubscribe_calls, [snapshot.thread_id])

    async def test_cleanup_and_unsubscribe_failures_remain_retryable(self) -> None:
        _binding, record, snapshot = await self.open_side()
        self.cleanup.failures.append(RuntimeError("cleanup lost"))

        with self.assertRaises(SideCloseFailed):
            await self.runtime.close_side(record.id)
        self.assertEqual(
            self.runtime.side_snapshot(record.id).state,
            SideSessionState.CLOSING,
        )
        self.assertEqual(self.store.get_side_topic(record.id).state, SideTopicState.OPEN)
        self.assertEqual(self.side_control.unsubscribe_calls, [])

        self.side_control.unsubscribe_failures.append(RuntimeError("reply lost"))
        with self.assertRaises(SideCloseFailed):
            await self.runtime.close_side(record.id)
        self.assertEqual(self.cleanup.calls, [snapshot.thread_id, snapshot.thread_id])
        self.assertEqual(self.side_control.unsubscribe_calls, [snapshot.thread_id])
        self.assertEqual(self.store.get_side_topic(record.id).state, SideTopicState.OPEN)

        outcome = await self.runtime.close_side(record.id)
        self.assertEqual(outcome.state, SideTopicState.CLOSED)
        self.assertEqual(self.cleanup.calls, [snapshot.thread_id, snapshot.thread_id])
        self.assertEqual(
            self.side_control.unsubscribe_calls,
            [snapshot.thread_id, snapshot.thread_id],
        )

    async def test_run_failure_poison_cannot_be_reported_closed_without_terminal(
        self,
    ) -> None:
        _binding, record, _snapshot = await self.open_side()
        submission = await self.runtime.submit_side(
            side_id=record.id,
            input="fail",
            owner_id="ou_owner",
            origin=SimpleNamespace(message_id="om-fail"),
        )
        self.codex.handles[-1].notifications.put_nowait(RuntimeError("stream lost"))
        assert submission.release_receipt_attempt is not None
        submission.release_receipt_attempt()
        self.assertTrue(await self.runtime.wait_idle(timeout=0.2))

        self.assertEqual(
            self.runtime.side_snapshot(record.id).state,
            SideSessionState.CLOSING,
        )
        with self.assertRaises(RuntimeClosed):
            await self.runtime.submit_side(
                side_id=record.id,
                input="must not start",
                owner_id="ou_owner",
                origin=SimpleNamespace(message_id="om-late"),
            )
        with self.assertRaisesRegex(SideCloseFailed, "请重启服务"):
            await self.runtime.close_side(record.id)
        self.assertEqual(self.store.get_side_topic(record.id).state, SideTopicState.OPEN)
        self.assertEqual(self.side_control.unsubscribe_calls, [])
        with self.assertRaises(RuntimeClosed):
            await self.runtime.capture_submission_admission(
                self.store.get(record.parent_binding_id).id
            )

    async def test_parent_running_is_allowed_but_parent_stopping_is_rejected(self) -> None:
        binding = self.materialized_binding()
        parent = await self.runtime.submit(
            binding=binding,
            cwd=self.cwd,
            input="parent",
            owner_id="ou_owner",
            origin=SimpleNamespace(message_id="om-parent"),
        )
        record = self.store.create_side_topic(
            app_id=self.scope.app_id,
            chat_id=self.scope.chat_id,
            source_message_id="om-side-running",
            parent_binding_id=binding.id,
            creator_id="ou_owner",
            requires_mention=False,
        )

        snapshot = await self.runtime.create_side(
            side_id=record.id,
            binding=binding,
            cwd=self.cwd,
            creator_id="ou_owner",
        )
        self.assertEqual(snapshot.parent_thread_id, parent.thread_id)

        parent_handle = self.codex.handles[0]
        parent_handle.complete_on_interrupt = False
        await self.runtime.stop(binding.id)
        second_record = self.store.create_side_topic(
            app_id=self.scope.app_id,
            chat_id=self.scope.chat_id,
            source_message_id="om-side-stopping",
            parent_binding_id=binding.id,
            creator_id="ou_owner",
            requires_mention=False,
        )
        with self.assertRaises(ThreadStopping):
            await self.runtime.create_side(
                side_id=second_record.id,
                binding=binding,
                cwd=self.cwd,
                creator_id="ou_owner",
            )

        parent_handle.complete(status="interrupted")
        assert parent.release_receipt_attempt is not None
        parent.release_receipt_attempt()
        self.assertTrue(await self.runtime.wait_idle(timeout=0.2))

    async def test_external_active_parent_fails_closed_before_fork(self) -> None:
        binding = self.materialized_binding()
        record = self.store.create_side_topic(
            app_id=self.scope.app_id,
            chat_id=self.scope.chat_id,
            source_message_id="om-side-external",
            parent_binding_id=binding.id,
            creator_id="ou_owner",
            requires_mention=False,
        )
        self.codex.read_statuses.append("active")

        with self.assertRaises(ThreadRunningConfiguration):
            await self.runtime.create_side(
                side_id=record.id,
                binding=binding,
                cwd=self.cwd,
                creator_id="ou_owner",
            )
        self.assertEqual(self.codex.fork_calls, [])
        subscription = self.runtime.thread_subscription_snapshot(binding.id)
        assert subscription is not None
        self.assertEqual(subscription.thread_id, binding.native_thread_id)
        self.assertEqual(subscription.state, ThreadSubscriptionState.SUBSCRIBED)

    async def test_boundary_injection_failure_compensates_without_registering(self) -> None:
        binding = self.materialized_binding()
        record = self.store.create_side_topic(
            app_id=self.scope.app_id,
            chat_id=self.scope.chat_id,
            source_message_id="om-side-inject-fail",
            parent_binding_id=binding.id,
            creator_id="ou_owner",
            requires_mention=False,
        )
        self.side_control.inject_failures.append(RuntimeError("response lost"))

        with self.assertRaises(SideStartFailed):
            await self.runtime.create_side(
                side_id=record.id,
                binding=binding,
                cwd=self.cwd,
                creator_id="ou_owner",
            )
        self.assertEqual(self.side_control.unsubscribe_calls, ["side-1"])
        with self.assertRaisesRegex(LookupError, record.id):
            self.runtime.side_snapshot(record.id)

    async def test_boundary_and_unsubscribe_unknown_retain_retry_slot(self) -> None:
        binding = self.materialized_binding()
        record = self.store.create_side_topic(
            app_id=self.scope.app_id,
            chat_id=self.scope.chat_id,
            source_message_id="om-side-double-unknown",
            parent_binding_id=binding.id,
            creator_id="ou_owner",
            requires_mention=False,
        )
        self.side_control.inject_failures.append(RuntimeError("inject lost"))
        self.side_control.unsubscribe_failures.append(RuntimeError("unsubscribe lost"))

        with self.assertRaisesRegex(SideStartFailed, "请重启服务"):
            await self.runtime.create_side(
                side_id=record.id,
                binding=binding,
                cwd=self.cwd,
                creator_id="ou_owner",
            )

        snapshot = self.runtime.side_snapshot(record.id)
        self.assertEqual(snapshot.state, SideSessionState.CLOSING)
        self.assertEqual(self.store.get_side_topic(record.id).state, SideTopicState.CREATING)
        with self.assertRaises(RuntimeClosed):
            await self.runtime.capture_side_submission_admission(record.id)
        outcome = await self.runtime.close_side(
            record.id,
            state=SideTopicState.FAILED,
        )
        self.assertEqual(outcome.state, SideTopicState.FAILED)
        self.assertEqual(self.side_control.unsubscribe_calls, ["side-1", "side-1"])

    async def test_fork_response_loss_closes_service_admission(self) -> None:
        binding = self.materialized_binding()
        record = self.store.create_side_topic(
            app_id=self.scope.app_id,
            chat_id=self.scope.chat_id,
            source_message_id="om-side-fork-unknown",
            parent_binding_id=binding.id,
            creator_id="ou_owner",
            requires_mention=False,
        )
        self.codex.fork_errors.append(RuntimeError("fork response lost"))

        with self.assertRaisesRegex(SideStartFailed, "请重启服务"):
            await self.runtime.create_side(
                side_id=record.id,
                binding=binding,
                cwd=self.cwd,
                creator_id="ou_owner",
            )
        with self.assertRaises(RuntimeClosed):
            await self.runtime.capture_submission_admission(binding.id)

    async def test_idle_expiry_is_not_part_of_wait_idle(self) -> None:
        await self.runtime.cancel_tasks()

        async def capture(outcome: object) -> None:
            self.outcomes.append(outcome)

        self.runtime = CodexRuntime(
            codex=self.codex,
            bindings=self.store,
            terminal_cleanup=self.cleanup,
            side_boundary_control=self.side_control,
            thread_subscription_control=self.side_control,
            on_completion=capture,
            poll_interval_seconds=0,
            side_idle_seconds=0.01,
        )
        _binding, record, snapshot = await self.open_side()

        self.assertTrue(await self.runtime.wait_idle(timeout=0.001))
        for _ in range(100):
            if self.store.get_side_topic(record.id).state is SideTopicState.EXPIRED:
                break
            await asyncio.sleep(0.002)
        self.assertEqual(
            self.store.get_side_topic(record.id).state,
            SideTopicState.EXPIRED,
        )
        self.assertEqual(self.side_control.unsubscribe_calls, [snapshot.thread_id])

    async def test_shutdown_interrupts_active_side_before_unsubscribe(self) -> None:
        _binding, record, snapshot = await self.open_side()
        unsubscribe = self.side_control.unsubscribe

        async def ordered_unsubscribe(thread_id: str) -> ThreadUnsubscribeStatus:
            self.codex.events.append(("unsubscribe", thread_id))
            return await unsubscribe(thread_id)

        self.side_control.unsubscribe = ordered_unsubscribe  # type: ignore[method-assign]
        submission = await self.runtime.submit_side(
            side_id=record.id,
            input="running",
            owner_id="ou_owner",
            origin=SimpleNamespace(message_id="om-running"),
        )
        assert submission.release_receipt_attempt is not None
        submission.release_receipt_attempt()

        await self.runtime.interrupt_all()

        self.assertEqual(
            self.codex.events,
            [
                ("interrupt", snapshot.thread_id),
                ("cleanup", snapshot.thread_id),
                ("unsubscribe", snapshot.thread_id),
            ],
        )
        self.assertEqual(self.side_control.unsubscribe_calls, [snapshot.thread_id])
        self.assertEqual(
            self.store.get_side_topic(record.id).state,
            SideTopicState.EXPIRED,
        )

    async def test_side_snapshots_parent_turn_settings_once_at_creation(self) -> None:
        effort = self.install_model_catalog()
        binding = self.store.create_binding(
            scope=self.scope,
            project_alias="test",
            creator_id="ou_owner",
            turn_settings=BindingTurnSettings(
                "catalog-model",
                "dynamic-effort",
                "priority-v2",
            ),
        )
        self.store.assign_native_thread_id(binding.id, "native-parent")
        binding = self.store.get(binding.id)
        _binding, record, _snapshot = await self.open_side_for_binding(binding)
        self.store.set_turn_settings(
            binding_id=binding.id,
            expected_revision=1,
            settings=None,
        )

        first = await self.runtime.submit_side(
            side_id=record.id,
            input="first",
            owner_id="ou_owner",
            origin=SimpleNamespace(message_id="om-first"),
        )
        await self.finish_side_turn(first)
        second = await self.runtime.submit_side(
            side_id=record.id,
            input="second",
            owner_id="ou_owner",
            origin=SimpleNamespace(message_id="om-second"),
        )
        await self.finish_side_turn(second)

        expected = {
            "model": "wire-model",
            "effort": effort,
            "service_tier": "priority-v2",
        }
        self.assertEqual([call[2] for call in self.codex.turn_calls], [expected, expected])
        self.assertEqual(self.codex.model_calls, 1)

    async def test_multiple_sides_on_one_parent_run_without_cross_side_lock(self) -> None:
        binding = self.materialized_binding()
        _binding, first_record, first_snapshot = await self.open_side_for_binding(
            binding,
            source="om-first-side",
        )
        _binding, second_record, second_snapshot = await self.open_side_for_binding(
            binding,
            source="om-second-side",
        )
        first_entered = asyncio.Event()
        second_entered = asyncio.Event()
        release_first = asyncio.Event()
        self.codex.turn_entered[first_snapshot.thread_id] = first_entered
        self.codex.turn_entered[second_snapshot.thread_id] = second_entered
        self.codex.turn_gates[first_snapshot.thread_id] = release_first

        first_task = asyncio.create_task(
            self.runtime.submit_side(
                side_id=first_record.id,
                input="one",
                owner_id="ou_one",
                origin=SimpleNamespace(message_id="om-one"),
            )
        )
        await asyncio.wait_for(first_entered.wait(), timeout=0.2)
        second_task = asyncio.create_task(
            self.runtime.submit_side(
                side_id=second_record.id,
                input="two",
                owner_id="ou_two",
                origin=SimpleNamespace(message_id="om-two"),
            )
        )
        observed_second_while_first_blocked = False
        try:
            await asyncio.wait_for(second_entered.wait(), timeout=0.2)
            observed_second_while_first_blocked = True
        except TimeoutError:
            pass
        finally:
            release_first.set()
        first, second = await asyncio.gather(first_task, second_task)

        self.assertTrue(observed_second_while_first_blocked)
        self.assertNotEqual(first_snapshot.thread_id, second_snapshot.thread_id)
        self.assertEqual(
            {first.thread_id, second.thread_id},
            {first_snapshot.thread_id, second_snapshot.thread_id},
        )
        self.codex.handles[-2].complete(response="one")
        self.codex.handles[-1].complete(response="two")
        assert first.release_receipt_attempt is not None
        assert second.release_receipt_attempt is not None
        first.release_receipt_attempt()
        second.release_receipt_attempt()
        self.assertTrue(await self.runtime.wait_idle(timeout=0.2))

    async def test_persistence_failure_retains_closing_session_for_retry(self) -> None:
        _binding, record, snapshot = await self.open_side()
        with patch.object(
            self.store,
            "transition_side_topic",
            side_effect=RuntimeError("sqlite unavailable"),
        ):
            with self.assertRaises(SideCloseFailed):
                await self.runtime.close_side(record.id)

        self.assertEqual(
            self.runtime.side_snapshot(record.id).state,
            SideSessionState.CLOSING,
        )
        self.assertEqual(self.store.get_side_topic(record.id).state, SideTopicState.OPEN)
        self.assertEqual(self.side_control.unsubscribe_calls, [snapshot.thread_id])

        outcome = await self.runtime.close_side(record.id)
        self.assertEqual(outcome.state, SideTopicState.CLOSED)
        self.assertEqual(
            self.side_control.unsubscribe_calls,
            [snapshot.thread_id, snapshot.thread_id],
        )


class ThreadSubscriptionRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.next_id = 0

        def make_id() -> str:
            self.next_id += 1
            return f"release-binding-{self.next_id}"

        self.store = BindingStore(id_factory=make_id)
        self.codex = FakeCodex()
        self.cleanup = FakeTerminalCleanup(self.codex.events)
        self.subscription = FakeThreadSubscriptionControl()
        self.inspector = FakeBackgroundTerminalInspector()
        self.runtime = self._new_runtime(idle_seconds=0.05)
        self.scope = FeishuScope("cli_test", "oc_release", ScopeKind.DIRECT)
        self.cwd_context = tempfile.TemporaryDirectory()
        self.cwd = Path(self.cwd_context.name)

    def _new_runtime(self, *, idle_seconds: float) -> CodexRuntime:
        return CodexRuntime(
            codex=self.codex,
            bindings=self.store,
            terminal_cleanup=self.cleanup,
            thread_subscription_control=self.subscription,
            background_terminal_inspector=self.inspector,
            poll_interval_seconds=0,
            ordinary_thread_idle_seconds=idle_seconds,
        )

    async def asyncTearDown(self) -> None:
        self.runtime.close_admission()
        await self.runtime.cancel_tasks()
        self.store.close()
        self.cwd_context.cleanup()

    def binding(self, *, scope: FeishuScope | None = None):
        return self.store.create_binding(
            scope=scope or self.scope,
            project_alias="test",
            creator_id="ou_user",
        )

    async def _complete_turn(self, binding) -> object:
        submission = await self.runtime.submit(
            binding=binding,
            cwd=self.cwd,
            input="release me",
            owner_id="ou_user",
            origin=object(),
        )
        assert submission.release_receipt_attempt is not None
        submission.release_receipt_attempt()
        self.codex.handles[-1].complete(response="released")
        self.assertTrue(await self.runtime.wait_idle(timeout=0.5))
        return self.store.get(binding.id)

    async def _wait_for_calls(self, expected: int, *, timeout: float = 0.5) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while len(self.subscription.calls) < expected:
            if asyncio.get_running_loop().time() >= deadline:
                self.fail(
                    f"expected {expected} unsubscribe calls, got "
                    f"{self.subscription.calls!r}"
                )
            await asyncio.sleep(0.002)

    async def _wait_for_inspections(
        self,
        expected: int,
        *,
        timeout: float = 0.5,
    ) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while len(self.inspector.calls) < expected:
            if asyncio.get_running_loop().time() >= deadline:
                self.fail(
                    f"expected {expected} inspector calls, got "
                    f"{self.inspector.calls!r}"
                )
            await asyncio.sleep(0.002)

    async def _wait_for_reads(self, expected: int, *, timeout: float = 0.5) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while len(self.codex.read_calls) < expected:
            if asyncio.get_running_loop().time() >= deadline:
                self.fail(
                    f"expected {expected} Thread reads, got "
                    f"{self.codex.read_calls!r}"
                )
            await asyncio.sleep(0.002)

    async def test_active_idle_release_is_transient_and_same_id_resumes(self) -> None:
        binding = await self._complete_turn(self.binding())
        snapshot = self.runtime.thread_subscription_snapshot(binding.id)
        assert snapshot is not None
        self.assertEqual(snapshot.state, ThreadSubscriptionState.RELEASE_PENDING)
        self.assertGreater(snapshot.release_in_seconds or 0, 0)
        self.assertTrue(await self.runtime.wait_idle(timeout=0.001))

        await self._wait_for_calls(1)
        snapshot = self.runtime.thread_subscription_snapshot(binding.id)
        assert snapshot is not None
        self.assertEqual(snapshot.state, ThreadSubscriptionState.RELEASED)
        self.assertEqual(self.inspector.calls, [binding.native_thread_id])
        self.assertEqual(self.cleanup.calls, [])

        second = await self.runtime.submit(
            binding=binding,
            cwd=self.cwd,
            input="same history",
            owner_id="ou_user",
            origin=object(),
        )
        self.assertEqual(self.codex.resume_calls[-1][0], binding.native_thread_id)
        self.assertEqual(second.thread_id, binding.native_thread_id)
        assert second.release_receipt_attempt is not None
        second.release_receipt_attempt()
        self.codex.handles[-1].complete()
        self.assertTrue(await self.runtime.wait_idle(timeout=0.5))

    async def test_running_binding_switched_away_releases_only_at_terminal(self) -> None:
        first = self.binding()
        submission = await self.runtime.submit(
            binding=first,
            cwd=self.cwd,
            input="still running",
            owner_id="ou_user",
            origin=object(),
        )
        assert submission.release_receipt_attempt is not None
        submission.release_receipt_attempt()
        second = self.binding()
        await self.runtime.active_binding_changed(first.id, second.id)
        await asyncio.sleep(0.01)
        self.assertEqual(self.subscription.calls, [])

        self.codex.handles[-1].complete()
        self.assertTrue(await self.runtime.wait_idle(timeout=0.5))
        await self._wait_for_calls(1)
        self.assertEqual(self.subscription.calls, [submission.thread_id])

    async def test_native_active_and_background_terminal_both_defer(self) -> None:
        binding = await self._complete_turn(self.binding())
        self.codex.read_statuses.extend(["active", "idle", "idle"])
        self.inspector.results.extend([True, False])

        await self._wait_for_inspections(1)
        self.assertEqual(self.subscription.calls, [])
        await self._wait_for_calls(1)
        self.assertGreaterEqual(len(self.inspector.calls), 2)
        self.assertEqual(self.cleanup.calls, [])
        self.assertEqual(self.subscription.calls, [binding.native_thread_id])

    async def test_read_not_loaded_defers_without_claiming_release(self) -> None:
        self.runtime.close_admission()
        await self.runtime.cancel_tasks()
        self.runtime = self._new_runtime(idle_seconds=10)
        binding = await self._complete_turn(self.binding())
        self.codex.read_statuses.append("notLoaded")

        with self.assertRaisesRegex(ThreadReleaseError, "notLoaded"):
            await self.runtime.release_binding(binding)

        snapshot = self.runtime.thread_subscription_snapshot(binding.id)
        assert snapshot is not None
        self.assertEqual(snapshot.state, ThreadSubscriptionState.RELEASE_PENDING)
        self.assertGreater(snapshot.release_in_seconds or 0, 0)
        self.assertEqual(self.subscription.calls, [])
        self.assertIsNotNone(self.runtime._subscriptions[binding.id].thread)

    async def test_inspector_error_defers_without_cleanup(self) -> None:
        self.inspector.errors.append(RuntimeError("list unavailable"))
        binding = await self._complete_turn(self.binding())

        await self._wait_for_inspections(1)
        self.assertEqual(self.subscription.calls, [])
        await self._wait_for_calls(1)
        self.assertEqual(self.cleanup.calls, [])
        self.assertEqual(self.subscription.calls, [binding.native_thread_id])

    async def test_unsubscribe_unknown_waits_a_full_window_then_converges(self) -> None:
        self.subscription.errors.append(
            ThreadUnsubscribeStateUnknown("response lost")
        )
        binding = await self._complete_turn(self.binding())

        await self._wait_for_calls(1)
        snapshot = self.runtime.thread_subscription_snapshot(binding.id)
        assert snapshot is not None
        self.assertEqual(snapshot.state, ThreadSubscriptionState.RELEASE_UNKNOWN)
        self.assertGreater(snapshot.release_in_seconds or 0, 0)
        await self._wait_for_calls(2)
        snapshot = self.runtime.thread_subscription_snapshot(binding.id)
        assert snapshot is not None
        self.assertEqual(snapshot.state, ThreadSubscriptionState.RELEASED)

    async def test_unsubscribe_unknown_survives_deferred_preconditions(self) -> None:
        self.subscription.errors.append(
            ThreadUnsubscribeStateUnknown("response lost")
        )
        binding = await self._complete_turn(self.binding())
        await self._wait_for_calls(1)

        reads_before = len(self.codex.read_calls)
        self.codex.read_statuses.append("notLoaded")
        await self._wait_for_reads(reads_before + 1)
        await asyncio.sleep(0)
        snapshot = self.runtime.thread_subscription_snapshot(binding.id)
        assert snapshot is not None
        self.assertEqual(snapshot.state, ThreadSubscriptionState.RELEASE_UNKNOWN)
        self.assertGreater(snapshot.release_in_seconds or 0, 0)

        inspections_before = len(self.inspector.calls)
        self.inspector.results.append(True)
        await self._wait_for_inspections(inspections_before + 1)
        await asyncio.sleep(0)
        snapshot = self.runtime.thread_subscription_snapshot(binding.id)
        assert snapshot is not None
        self.assertEqual(snapshot.state, ThreadSubscriptionState.RELEASE_UNKNOWN)
        self.assertGreater(snapshot.release_in_seconds or 0, 0)

        await self._wait_for_calls(2, timeout=1)
        snapshot = self.runtime.thread_subscription_snapshot(binding.id)
        assert snapshot is not None
        self.assertEqual(snapshot.state, ThreadSubscriptionState.RELEASED)

    async def test_unsubscribe_unknown_survives_local_busy_slot(self) -> None:
        self.subscription.errors.append(
            ThreadUnsubscribeStateUnknown("response lost")
        )
        binding = await self._complete_turn(self.binding())
        await self._wait_for_calls(1)
        self.runtime._lifecycles[binding.id] = SimpleNamespace(
            state=ThreadLifecycleState.RENAMING
        )

        await asyncio.sleep(0.06)

        snapshot = self.runtime.thread_subscription_snapshot(binding.id)
        assert snapshot is not None
        self.assertEqual(snapshot.state, ThreadSubscriptionState.RELEASE_UNKNOWN)
        self.assertIsNone(snapshot.release_in_seconds)
        self.assertEqual(len(self.subscription.calls), 1)

        self.runtime._lifecycles.pop(binding.id)
        async with self.runtime._lock(binding.id):
            self.runtime._schedule_known_subscription_locked(
                binding.id,
                binding.native_thread_id,
            )
        await self._wait_for_calls(2)

    async def test_cancelled_release_read_rearms_full_window(self) -> None:
        self.runtime.close_admission()
        await self.runtime.cancel_tasks()
        self.runtime = self._new_runtime(idle_seconds=10)
        binding = await self._complete_turn(self.binding())
        reads_before = len(self.codex.read_calls)
        self.codex.read_gate = asyncio.Event()
        task = asyncio.create_task(self.runtime.release_binding(binding))
        await self._wait_for_reads(reads_before + 1)

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.codex.read_gate = None

        snapshot = self.runtime.thread_subscription_snapshot(binding.id)
        assert snapshot is not None
        self.assertEqual(snapshot.state, ThreadSubscriptionState.RELEASE_PENDING)
        self.assertGreater(snapshot.release_in_seconds or 0, 0)
        self.assertIsNotNone(self.runtime._subscriptions[binding.id].idle_task)

    async def test_cancelled_unsubscribe_rearms_unknown_window(self) -> None:
        self.runtime.close_admission()
        await self.runtime.cancel_tasks()
        self.runtime = self._new_runtime(idle_seconds=10)
        binding = await self._complete_turn(self.binding())
        self.subscription.gate = asyncio.Event()
        task = asyncio.create_task(self.runtime.release_binding(binding))
        await asyncio.wait_for(self.subscription.entered.wait(), timeout=0.2)

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.subscription.gate = None

        snapshot = self.runtime.thread_subscription_snapshot(binding.id)
        assert snapshot is not None
        self.assertEqual(snapshot.state, ThreadSubscriptionState.RELEASE_UNKNOWN)
        self.assertGreater(snapshot.release_in_seconds or 0, 0)
        self.assertIsNotNone(self.runtime._subscriptions[binding.id].idle_task)

    async def test_explicit_release_refuses_busy_and_registered_terminal(self) -> None:
        lazy = self.binding()
        self.assertEqual(
            await self.runtime.release_binding(lazy),
            ReleaseDisposition.NOT_MATERIALIZED,
        )
        running = await self.runtime.submit(
            binding=lazy,
            cwd=self.cwd,
            input="busy",
            owner_id="ou_user",
            origin=object(),
        )
        with self.assertRaises(ThreadRunningConfiguration):
            await self.runtime.release_binding(self.store.get(lazy.id))
        assert running.release_receipt_attempt is not None
        running.release_receipt_attempt()
        self.codex.handles[-1].complete()
        self.assertTrue(await self.runtime.wait_idle(timeout=0.5))

        self.codex.read_statuses.append("active")
        with self.assertRaises(ThreadReleaseError):
            await self.runtime.release_binding(self.store.get(lazy.id))
        self.assertEqual(self.inspector.calls, [])

        self.inspector.results.append(True)
        with self.assertRaises(ThreadBackgroundTerminalsActive):
            await self.runtime.release_binding(self.store.get(lazy.id))
        self.assertEqual(self.subscription.calls, [])
        self.assertEqual(self.cleanup.calls, [])

        self.inspector.results.append(False)
        self.assertEqual(
            await self.runtime.release_binding(self.store.get(lazy.id)),
            ReleaseDisposition.RELEASED,
        )

    async def test_explicit_release_refuses_every_local_operation_slot(self) -> None:
        self.runtime.close_admission()
        await self.runtime.cancel_tasks()
        self.runtime = self._new_runtime(idle_seconds=10)
        binding = await self._complete_turn(self.binding())

        self.runtime._compacting[binding.id] = object()
        with self.assertRaises(ThreadCompacting):
            await self.runtime.release_binding(binding)
        self.runtime._compacting.pop(binding.id)

        self.runtime._goals[binding.id] = SimpleNamespace(
            state=GoalOperationState.RUNNING
        )
        with self.assertRaises(ThreadGoalActive):
            await self.runtime.release_binding(binding)
        self.runtime._goals.pop(binding.id)

        self.runtime._lifecycles[binding.id] = SimpleNamespace(
            state=ThreadLifecycleState.RENAMING
        )
        with self.assertRaises(ThreadLifecycleError):
            await self.runtime.release_binding(binding)
        self.runtime._lifecycles.pop(binding.id)

        self.assertEqual(
            await self.runtime.release_binding(binding),
            ReleaseDisposition.RELEASED,
        )

    async def test_exact_release_targets_an_inactive_subscription(self) -> None:
        self.runtime.close_admission()
        await self.runtime.cancel_tasks()
        self.runtime = self._new_runtime(idle_seconds=10)
        first = await self._complete_turn(self.binding())
        second = self.binding()

        disposition = await self.runtime.release_exact(first.id)

        self.assertEqual(disposition, ReleaseDisposition.RELEASED)
        self.assertEqual(self.subscription.calls, [first.native_thread_id])
        self.assertEqual(self.inspector.calls, [first.native_thread_id])
        self.assertEqual(self.store.active_binding(self.scope.key).id, second.id)

    async def test_restore_without_activation_immediately_releases_subscription(
        self,
    ) -> None:
        binding = self.binding()
        self.store.assign_native_thread_id(binding.id, "native-archived")
        other = self.binding()
        self.codex.thread_list_pages = [
            SimpleNamespace(
                data=[SimpleNamespace(id="native-archived", name=None, preview="old")],
                next_cursor=None,
            )
        ]

        restored = await self.runtime.restore_exact(binding.id)
        await self._wait_for_calls(1)

        self.assertFalse(restored.active)
        self.assertEqual(self.store.active_binding(self.scope.key).id, other.id)
        self.assertEqual(self.subscription.calls, ["native-archived"])

    async def test_activate_serializes_with_stale_inactive_release_timer(self) -> None:
        self.runtime.close_admission()
        await self.runtime.cancel_tasks()
        self.runtime = self._new_runtime(idle_seconds=10)
        binding = await self._complete_turn(self.binding())
        self.binding()
        async with self.runtime._lock(binding.id):
            self.runtime._schedule_subscription_release_locked(
                self.runtime._subscriptions[binding.id],
                delay=0.01,
            )
        catalog_entered = asyncio.Event()
        release_catalog = asyncio.Event()

        async def blocked_catalog(_thread_id: str):
            catalog_entered.set()
            await release_catalog.wait()
            return NativeThreadCatalogState.ACTIVE

        with patch.object(
            self.runtime,
            "thread_catalog_state",
            side_effect=blocked_catalog,
        ):
            task = asyncio.create_task(self.runtime.activate_exact(binding.id))
            await asyncio.wait_for(catalog_entered.wait(), timeout=0.2)
            await asyncio.sleep(0.02)
            release_catalog.set()
            activated = await asyncio.wait_for(task, timeout=0.2)

        await asyncio.sleep(0.02)
        self.assertTrue(activated.active)
        self.assertEqual(self.subscription.calls, [])
        snapshot = self.runtime.thread_subscription_snapshot(binding.id)
        assert snapshot is not None
        self.assertEqual(snapshot.state, ThreadSubscriptionState.RELEASE_PENDING)
        self.assertGreater(snapshot.release_in_seconds or 0, 1)

    async def test_restart_does_not_rebuild_subscription_or_timer(self) -> None:
        binding = await self._complete_turn(self.binding())
        self.runtime.close_admission()
        await self.runtime.cancel_tasks()
        calls_before = tuple(self.subscription.calls)
        self.runtime = self._new_runtime(idle_seconds=0.01)

        self.assertIsNone(self.runtime.thread_subscription_snapshot(binding.id))
        self.assertEqual(
            await self.runtime.release_binding(binding),
            ReleaseDisposition.NOT_SUBSCRIBED,
        )
        await asyncio.sleep(0.02)
        self.assertEqual(tuple(self.subscription.calls), calls_before)
        self.assertEqual(self.codex.resume_calls, [])

    async def test_fast_a_b_a_switch_cancels_stale_immediate_release(self) -> None:
        first = await self._complete_turn(self.binding())
        second = self.binding()
        await self.runtime.active_binding_changed(first.id, second.id)
        self.store.activate(scope_key=self.scope.key, binding_id=first.id)
        await self.runtime.active_binding_changed(second.id, first.id)

        await asyncio.sleep(0.01)
        self.assertEqual(self.subscription.calls, [])
        snapshot = self.runtime.thread_subscription_snapshot(first.id)
        assert snapshot is not None
        self.assertEqual(snapshot.state, ThreadSubscriptionState.RELEASE_PENDING)
        await self._wait_for_calls(1)

    async def test_pointer_change_protects_current_before_previous_lock(self) -> None:
        self.runtime.close_admission()
        await self.runtime.cancel_tasks()
        self.runtime = self._new_runtime(idle_seconds=10)
        first = await self._complete_turn(self.binding())
        second = await self._complete_turn(self.binding())
        self.store.activate(scope_key=self.scope.key, binding_id=first.id)
        async with self.runtime._lock(second.id):
            self.runtime._schedule_subscription_release_locked(
                self.runtime._subscriptions[second.id],
                delay=0.2,
            )
        previous_lock = self.runtime._lock(first.id)
        await previous_lock.acquire()
        self.store.activate(scope_key=self.scope.key, binding_id=second.id)
        changed = asyncio.create_task(
            self.runtime.active_binding_changed(first.id, second.id)
        )
        try:
            await asyncio.sleep(0.01)
            snapshot = self.runtime.thread_subscription_snapshot(second.id)
            assert snapshot is not None
            self.assertGreater(snapshot.release_in_seconds or 0, 1)
            self.assertFalse(changed.done())
            self.assertEqual(self.subscription.calls, [])
        finally:
            previous_lock.release()
            await changed

    async def test_inactive_timer_rechecks_new_current_pointer(self) -> None:
        self.runtime.close_admission()
        await self.runtime.cancel_tasks()
        self.runtime = self._new_runtime(idle_seconds=10)
        first = await self._complete_turn(self.binding())
        second = await self._complete_turn(self.binding())
        self.store.activate(scope_key=self.scope.key, binding_id=first.id)
        async with self.runtime._lock(second.id):
            self.runtime._schedule_subscription_release_locked(
                self.runtime._subscriptions[second.id],
                delay=0.01,
            )
        self.store.activate(scope_key=self.scope.key, binding_id=second.id)

        await asyncio.sleep(0.03)

        snapshot = self.runtime.thread_subscription_snapshot(second.id)
        assert snapshot is not None
        self.assertEqual(snapshot.state, ThreadSubscriptionState.RELEASE_PENDING)
        self.assertGreater(snapshot.release_in_seconds or 0, 1)
        self.assertEqual(self.subscription.calls, [])

    async def test_no_subscription_count_cap_or_lru_eviction(self) -> None:
        self.runtime.close_admission()
        await self.runtime.cancel_tasks()
        self.runtime = self._new_runtime(idle_seconds=10)
        bindings = []
        for index in range(12):
            scope = FeishuScope(
                "cli_test",
                f"oc_release_{index}",
                ScopeKind.DIRECT,
            )
            bindings.append(await self._complete_turn(self.binding(scope=scope)))

        self.assertEqual(len(self.runtime._subscriptions), len(bindings))
        self.assertEqual(self.subscription.calls, [])

    async def test_shutdown_cancels_release_timers_before_more_rpc(self) -> None:
        await self._complete_turn(self.binding())
        self.assertTrue(self.runtime._subscription_idle_tasks)
        await self.runtime.interrupt_all()
        self.assertFalse(self.runtime._subscription_idle_tasks)
        await asyncio.sleep(0.06)
        self.assertEqual(self.inspector.calls, [])
        self.assertEqual(self.subscription.calls, [])


class CodexRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.ids = iter(["binding-1", "binding-2", "binding-3"])
        self.store = BindingStore(id_factory=lambda: next(self.ids))
        self.codex = FakeCodex()
        self.cleanup = FakeTerminalCleanup(self.codex.events)
        self.delete_control = FakeThreadDeleteControl()
        self.outcomes: list[TurnOutcome | CompactionOutcome] = []

        async def capture(outcome: TurnOutcome | CompactionOutcome) -> None:
            self.outcomes.append(outcome)

        self.runtime = CodexRuntime(
            codex=self.codex,
            bindings=self.store,
            terminal_cleanup=self.cleanup,
            thread_delete_control=self.delete_control,
            on_completion=capture,
            poll_interval_seconds=0,
        )
        self.scope = FeishuScope("cli_test", "oc_chat", ScopeKind.DIRECT)
        self.cwd_context = tempfile.TemporaryDirectory()
        self.cwd = Path(self.cwd_context.name)

    async def asyncTearDown(self) -> None:
        try:
            await self.runtime.interrupt_all()
        except Exception:
            pass
        if not await self.runtime.wait_idle(timeout=0.1):
            await self.runtime.cancel_tasks()
        self.store.close()
        self.cwd_context.cleanup()

    def binding(self, scope: FeishuScope | None = None):
        return self.store.create_binding(
            scope=scope or self.scope,
            project_alias="test",
            creator_id="ou_user",
        )

    def catch_up_binding(self):
        scope = FeishuScope("cli_test", "oc_group", ScopeKind.GROUP)
        return self.store.create_binding(
            scope=scope,
            project_alias="test",
            creator_id="ou_user",
            message_context_mode=MentionContextMode.CATCH_UP,
            context_anchor=MessageContextAnchor("om-lower", 1_000),
        )

    async def test_thread_metadata_uses_paginated_public_history_list(self) -> None:
        self.codex.thread_list_pages = [
            SimpleNamespace(
                data=[
                    SimpleNamespace(
                        id="unrelated",
                        name=None,
                        preview="other",
                    )
                ],
                next_cursor="page-two",
            ),
            SimpleNamespace(
                data=[
                    SimpleNamespace(
                        id="native-2",
                        name=None,
                        preview="second prompt",
                    ),
                    SimpleNamespace(
                        id="native-1",
                        name="First title",
                        preview="first prompt",
                    ),
                ],
                next_cursor="unused-page",
            ),
        ]

        metadata = await self.runtime.thread_metadata(("native-1", "native-2"))

        self.assertEqual(
            metadata,
            {
                "native-1": NativeThreadMetadata(
                    "native-1",
                    "First title",
                    "first prompt",
                ),
                "native-2": NativeThreadMetadata(
                    "native-2",
                    None,
                    "second prompt",
                ),
            },
        )
        self.assertEqual(
            self.codex.thread_list_calls,
            [
                {
                    "archived": False,
                    "cursor": None,
                    "limit": 100,
                    "model_providers": [],
                },
                {
                    "archived": False,
                    "cursor": "page-two",
                    "limit": 100,
                    "model_providers": [],
                },
            ],
        )

    async def test_archived_thread_metadata_uses_native_archived_filter(self) -> None:
        self.codex.thread_list_pages = [
            SimpleNamespace(
                data=[
                    SimpleNamespace(
                        id="native-1",
                        name="Archived",
                        preview="old task",
                    )
                ],
                next_cursor=None,
            )
        ]

        metadata = await self.runtime.thread_metadata(
            ("native-1",),
            archived=True,
        )

        self.assertEqual(metadata["native-1"].name, "Archived")
        self.assertEqual(
            self.codex.thread_list_calls,
            [
                {
                    "archived": True,
                    "cursor": None,
                    "limit": 100,
                    "model_providers": [],
                }
            ],
        )

    async def test_rename_uses_public_thread_handle_without_local_name_state(self) -> None:
        binding = self.binding()
        self.store.assign_native_thread_id(binding.id, "native-1")
        binding = self.store.get(binding.id)

        name = await self.runtime.rename_binding(binding, "  Release   review  ")

        self.assertEqual(name, "Release review")
        self.assertEqual(
            self.codex.set_name_calls,
            [("native-1", "Release review")],
        )
        self.assertEqual(self.store.get(binding.id).native_thread_id, "native-1")
        self.assertIsNone(self.runtime.lifecycle_state(binding.id))

    async def test_exact_rename_and_configure_accept_inactive_binding(self) -> None:
        first = self.binding()
        self.store.assign_native_thread_id(first.id, "native-1")
        second = self.binding()
        settings = BindingTurnSettings("model", "high", "priority")

        name = await self.runtime.rename_exact(first.id, " inactive ")
        configured = await self.runtime.configure_exact(
            binding_id=first.id,
            expected_revision=1,
            settings=settings,
        )

        self.assertEqual(name, "inactive")
        self.assertEqual(configured.turn_settings, settings)
        self.assertFalse(configured.active)
        self.assertEqual(self.store.active_binding(self.scope.key).id, second.id)

    async def test_archive_requires_idle_then_retains_binding_and_clears_active(self) -> None:
        binding = self.binding()
        self.store.assign_native_thread_id(binding.id, "native-1")
        configured = BindingTurnSettings("model", "high", "priority")
        binding = self.store.set_turn_settings(
            binding_id=binding.id,
            expected_revision=1,
            settings=configured,
        )

        archived = await self.runtime.archive_binding(binding)

        self.assertEqual(self.codex.archive_calls, ["native-1"])
        self.assertFalse(archived.active)
        self.assertEqual(archived.turn_settings, configured)
        self.assertIsNone(self.store.active_binding(self.scope.key))
        self.assertEqual(self.store.get(binding.id).native_thread_id, "native-1")

    async def test_exact_archive_of_inactive_binding_preserves_current_pointer(self) -> None:
        first = self.binding()
        self.store.assign_native_thread_id(first.id, "native-1")
        second = self.binding()

        archived = await self.runtime.archive_exact(first.id)

        self.assertFalse(archived.active)
        self.assertEqual(self.codex.archive_calls, ["native-1"])
        self.assertEqual(self.store.active_binding(self.scope.key).id, second.id)

    async def test_archive_and_delete_reject_a_running_turn_without_mutation(self) -> None:
        binding = self.binding()
        submission = await self.submit(binding)
        binding = self.store.get(binding.id)

        with self.assertRaises(ThreadRunningConfiguration):
            await self.runtime.archive_binding(binding)
        with self.assertRaises(ThreadRunningConfiguration):
            await self.runtime.delete_binding(binding)

        self.assertEqual(self.codex.archive_calls, [])
        self.assertEqual(self.delete_control.calls, [])
        await self.finish(self.codex.handles[0], submission)

    async def test_archive_response_loss_keeps_current_and_fails_closed(self) -> None:
        binding = self.binding()
        self.store.assign_native_thread_id(binding.id, "native-1")
        binding = self.store.get(binding.id)
        self.codex.archive_errors.append(RuntimeError("lost response"))

        with self.assertRaises(ThreadLifecycleStateUnknown):
            await self.runtime.archive_binding(binding)

        self.assertTrue(self.store.get(binding.id).active)
        self.assertEqual(
            self.runtime.lifecycle_state(binding.id).state,
            ThreadLifecycleState.UNKNOWN,
        )
        with self.assertRaises(RuntimeClosed):
            await self.runtime.capture_submission_admission(binding.id)

    async def test_delete_lazy_binding_never_calls_native_delete(self) -> None:
        binding = self.binding()

        deleted = await self.runtime.delete_binding(binding)

        self.assertEqual(deleted.id, binding.id)
        self.assertEqual(self.delete_control.calls, [])
        with self.assertRaises(BindingNotFound):
            self.store.get(binding.id)
        self.assertIsNone(self.store.active_binding(self.scope.key))

    async def test_exact_lazy_delete_never_reaches_materialized_adapter(self) -> None:
        binding = self.binding()
        self.store.assign_native_thread_id(binding.id, "native-1")

        with self.assertRaises(ThreadDeleteUnavailable):
            await self.runtime.delete_lazy_exact(binding.id)

        self.assertEqual(self.delete_control.calls, [])
        self.assertEqual(self.store.get(binding.id).native_thread_id, "native-1")

    async def test_stale_lazy_delete_snapshot_cannot_expand_to_native_delete(
        self,
    ) -> None:
        stale = self.binding()
        self.store.assign_native_thread_id(stale.id, "native-1")

        with self.assertRaises(ThreadDeleteTargetChanged):
            await self.runtime.delete_binding(stale)

        self.assertEqual(self.delete_control.calls, [])
        self.assertEqual(self.store.get(stale.id).native_thread_id, "native-1")

    async def test_delete_native_ack_precedes_local_binding_delete(self) -> None:
        binding = self.binding()
        self.store.assign_native_thread_id(binding.id, "native-1")
        binding = self.store.get(binding.id)

        deleted = await self.runtime.delete_binding(binding)

        self.assertEqual(deleted.id, binding.id)
        self.assertEqual(self.delete_control.calls, ["native-1"])
        with self.assertRaises(BindingNotFound):
            self.store.get(binding.id)

    async def test_delete_error_reconciles_four_missing_native_views(self) -> None:
        binding = self.binding()
        self.store.assign_native_thread_id(binding.id, "native-1")
        binding = self.store.get(binding.id)
        self.delete_control.errors.append(RuntimeError("app-server error"))
        self.codex.thread_list_pages = [
            SimpleNamespace(data=[], next_cursor=None),
            SimpleNamespace(data=[], next_cursor=None),
            SimpleNamespace(data=[], next_cursor=None),
            SimpleNamespace(data=[], next_cursor=None),
        ]

        deleted = await self.runtime.delete_binding(binding)

        self.assertEqual(deleted.id, binding.id)
        with self.assertRaises(BindingNotFound):
            self.store.get(binding.id)
        self.assertEqual(
            self.codex.thread_list_calls,
            [
                {
                    "archived": False,
                    "cursor": None,
                    "limit": 100,
                    "model_providers": [],
                    "use_state_db_only": False,
                },
                {
                    "archived": True,
                    "cursor": None,
                    "limit": 100,
                    "model_providers": [],
                    "use_state_db_only": False,
                },
                {
                    "archived": False,
                    "cursor": None,
                    "limit": 100,
                    "model_providers": [],
                    "use_state_db_only": True,
                },
                {
                    "archived": True,
                    "cursor": None,
                    "limit": 100,
                    "model_providers": [],
                    "use_state_db_only": True,
                },
            ],
        )
        self.assertTrue(self.runtime._accepting)

    async def test_delete_error_with_present_native_thread_is_retryable(self) -> None:
        binding = self.binding()
        self.store.assign_native_thread_id(binding.id, "native-1")
        binding = self.store.get(binding.id)
        self.delete_control.errors.append(RuntimeError("app-server error"))
        present = SimpleNamespace(
            id="native-1",
            name=None,
            preview="existing",
        )
        self.codex.thread_list_pages = [
            SimpleNamespace(data=[present], next_cursor=None),
            SimpleNamespace(data=[], next_cursor=None),
            SimpleNamespace(data=[present], next_cursor=None),
            SimpleNamespace(data=[], next_cursor=None),
        ]

        with self.assertRaisesRegex(ThreadLifecycleError, "仍存在"):
            await self.runtime.delete_binding(binding)

        self.assertEqual(self.store.get(binding.id).native_thread_id, "native-1")
        self.assertIsNone(self.runtime.lifecycle_state(binding.id))
        self.assertTrue(self.runtime._accepting)

    async def test_delete_retry_finishes_binding_when_native_is_already_absent(
        self,
    ) -> None:
        binding = self.binding()
        self.store.assign_native_thread_id(binding.id, "native-1")
        binding = self.store.get(binding.id)
        self.codex.resume_errors.append(RuntimeError("no rollout found"))
        self.codex.thread_list_pages = [
            SimpleNamespace(data=[], next_cursor=None),
            SimpleNamespace(data=[], next_cursor=None),
            SimpleNamespace(data=[], next_cursor=None),
            SimpleNamespace(data=[], next_cursor=None),
        ]

        deleted = await self.runtime.delete_binding(binding)

        self.assertEqual(deleted.id, binding.id)
        self.assertEqual(self.delete_control.calls, [])
        with self.assertRaises(BindingNotFound):
            self.store.get(binding.id)
        self.assertTrue(self.runtime._accepting)

    async def test_delete_read_failure_keeps_binding_when_catalog_still_has_thread(
        self,
    ) -> None:
        binding = self.binding()
        self.store.assign_native_thread_id(binding.id, "native-1")
        binding = self.store.get(binding.id)
        self.codex.resume_errors.append(RuntimeError("resume failed"))
        present = SimpleNamespace(
            id="native-1",
            name=None,
            preview="existing",
        )
        self.codex.thread_list_pages = [
            SimpleNamespace(data=[present], next_cursor=None),
            SimpleNamespace(data=[], next_cursor=None),
            SimpleNamespace(data=[present], next_cursor=None),
            SimpleNamespace(data=[], next_cursor=None),
        ]

        with self.assertRaisesRegex(ThreadLifecycleError, "无法读取"):
            await self.runtime.delete_binding(binding)

        self.assertEqual(self.delete_control.calls, [])
        self.assertEqual(self.store.get(binding.id).native_thread_id, "native-1")
        self.assertIsNone(self.runtime.lifecycle_state(binding.id))
        self.assertTrue(self.runtime._accepting)

    async def test_materialized_delete_is_unavailable_without_gap_contract(self) -> None:
        binding = self.binding()
        self.store.assign_native_thread_id(binding.id, "native-1")
        binding = self.store.get(binding.id)
        self.runtime._thread_delete_control = None

        with self.assertRaises(ThreadDeleteUnavailable):
            await self.runtime.delete_binding(binding)

        self.assertEqual(self.store.get(binding.id).native_thread_id, "native-1")
        self.assertIsNone(self.runtime.lifecycle_state(binding.id))

    async def test_delete_reconciliation_failure_retains_binding_and_fails_closed(
        self,
    ) -> None:
        binding = self.binding()
        self.store.assign_native_thread_id(binding.id, "native-1")
        binding = self.store.get(binding.id)
        self.delete_control.errors.append(RuntimeError("lost response"))

        with self.assertRaises(ThreadLifecycleStateUnknown):
            await self.runtime.delete_binding(binding)

        self.assertEqual(self.store.get(binding.id).native_thread_id, "native-1")
        lifecycle = self.runtime.lifecycle_state(binding.id)
        self.assertIsNotNone(lifecycle)
        self.assertEqual(lifecycle.state, ThreadLifecycleState.UNKNOWN)
        with self.assertRaises(RuntimeClosed):
            await self.runtime.capture_submission_admission(binding.id)

    async def test_delete_cancellation_retains_unknown_and_fails_closed(self) -> None:
        binding = self.binding()
        self.store.assign_native_thread_id(binding.id, "native-1")
        binding = self.store.get(binding.id)
        self.delete_control.errors.append(asyncio.CancelledError())

        with self.assertRaises(asyncio.CancelledError):
            await self.runtime.delete_binding(binding)

        self.assertEqual(self.store.get(binding.id).native_thread_id, "native-1")
        self.assertEqual(
            self.runtime.lifecycle_state(binding.id).state,
            ThreadLifecycleState.UNKNOWN,
        )
        with self.assertRaises(RuntimeClosed):
            await self.runtime.capture_submission_admission(binding.id)

    async def test_delete_local_commit_failure_after_native_ack_fails_closed(
        self,
    ) -> None:
        binding = self.binding()
        self.store.assign_native_thread_id(binding.id, "native-1")
        binding = self.store.get(binding.id)

        with patch.object(
            self.store,
            "delete_binding",
            side_effect=RuntimeError("sqlite commit lost"),
        ):
            with self.assertRaises(ThreadLifecycleStateUnknown):
                await self.runtime.delete_binding(binding)

        self.assertEqual(self.delete_control.calls, ["native-1"])
        self.assertEqual(self.store.get(binding.id).native_thread_id, "native-1")
        self.assertEqual(
            self.runtime.lifecycle_state(binding.id).state,
            ThreadLifecycleState.UNKNOWN,
        )
        with self.assertRaises(RuntimeClosed):
            await self.runtime.capture_submission_admission(binding.id)

    async def test_submit_rechecks_current_binding_after_goal_reconcile_await(self) -> None:
        first = self.binding()
        started = await self.submit(first, "materialize")
        await self.finish(self.codex.handles[0], started)
        second = self.binding()
        first = self.store.activate(
            scope_key=self.scope.key,
            binding_id=first.id,
        )
        admission = await self.runtime.capture_submission_admission(first.id)
        reconcile_started = asyncio.Event()
        release_reconcile = asyncio.Event()

        class BlockingGoalControl(FakeGoalControl):
            async def get(self, thread_id: str) -> GoalSnapshot | None:
                self.get_calls.append(thread_id)
                reconcile_started.set()
                await release_reconcile.wait()
                return None

        self.runtime._goal_control = BlockingGoalControl(self.codex)
        task = asyncio.create_task(
            self.submit(first, "must not run", admission=admission)
        )
        await asyncio.wait_for(reconcile_started.wait(), timeout=0.1)
        self.store.activate(scope_key=self.scope.key, binding_id=second.id)
        release_reconcile.set()

        with self.assertRaises(SteerRace):
            await asyncio.wait_for(task, timeout=0.1)
        self.assertEqual(len(self.codex.turn_inputs), 1)
        self.assertEqual(self.store.active_binding(self.scope.key).id, second.id)

    async def test_rename_is_supported_while_current_turn_is_running(self) -> None:
        binding = self.binding()
        submission = await self.submit(binding)
        binding = self.store.get(binding.id)

        name = await self.runtime.rename_binding(binding, "Running review")

        self.assertEqual(name, "Running review")
        self.assertEqual(
            self.codex.set_name_calls,
            [(binding.native_thread_id, "Running review")],
        )
        self.assertIsNotNone(self.runtime.active_turn(binding.id))
        await self.finish(self.codex.handles[0], submission)

    async def test_unarchive_uses_archived_inventory_then_activates_binding(self) -> None:
        binding = self.binding()
        self.store.assign_native_thread_id(binding.id, "native-1")
        self.store.deactivate(scope_key=self.scope.key, binding_id=binding.id)
        binding = self.store.get(binding.id)
        self.codex.thread_list_pages = [
            SimpleNamespace(
                data=[
                    SimpleNamespace(
                        id="native-1",
                        name="Archived",
                        preview="old task",
                    )
                ],
                next_cursor=None,
            )
        ]

        restored = await self.runtime.unarchive_binding(binding)

        self.assertEqual(self.codex.unarchive_calls, ["native-1"])
        self.assertEqual(self.codex.resume_calls, [("native-1", {})])
        self.assertTrue(restored.active)
        self.assertEqual(self.store.active_binding(self.scope.key).id, binding.id)

    async def test_exact_restore_without_activation_preserves_other_pointer(self) -> None:
        binding = self.binding()
        self.store.assign_native_thread_id(binding.id, "native-1")
        other = self.binding()
        self.codex.thread_list_pages = [
            SimpleNamespace(
                data=[SimpleNamespace(id="native-1", name=None, preview="old")],
                next_cursor=None,
            )
        ]

        restored = await self.runtime.restore_exact(binding.id)

        self.assertFalse(restored.active)
        self.assertEqual(self.store.active_binding(self.scope.key).id, other.id)
        self.assertEqual(self.codex.unarchive_calls, ["native-1"])
        self.assertEqual(self.codex.resume_calls, [("native-1", {})])

    async def test_exact_stop_targets_running_inactive_binding(self) -> None:
        binding = self.binding()
        submission = await self.submit(binding, "keep exact target")
        other = self.binding()

        result = await self.runtime.stop_exact(binding.id)

        self.assertEqual(result, StopDisposition.REQUESTED)
        self.assertEqual(self.store.active_binding(self.scope.key).id, other.id)
        self.assertEqual(self.codex.handles[0].interrupt_count, 1)
        await self.finish(self.codex.handles[0], submission)

    async def test_binding_runtime_snapshot_is_one_immutable_activity_view(self) -> None:
        binding = self.binding()
        idle = self.runtime.binding_runtime_snapshot(binding.id)

        submission = await self.submit(binding, "snapshot")
        running = self.runtime.binding_runtime_snapshot(binding.id)

        self.assertEqual(idle.binding_id, binding.id)
        self.assertIsNone(idle.turn)
        self.assertIsNone(idle.goal)
        self.assertFalse(idle.compacting)
        self.assertIsNone(idle.lifecycle)
        self.assertIsNone(idle.subscription)
        self.assertGreater(running.activity_revision, idle.activity_revision)
        self.assertEqual(running.turn.turn_id, submission.turn_id)
        self.assertEqual(running.subscription.thread_id, submission.thread_id)
        self.assertIsNone(running.context_window_usage)
        await self.finish(self.codex.handles[0], submission)

    async def test_unarchive_requires_exact_resume_before_activation(self) -> None:
        binding = self.binding()
        self.store.assign_native_thread_id(binding.id, "native-1")
        self.store.deactivate(scope_key=self.scope.key, binding_id=binding.id)
        binding = self.store.get(binding.id)
        self.codex.thread_list_pages = [
            SimpleNamespace(
                data=[SimpleNamespace(id="native-1", name=None, preview="old task")],
                next_cursor=None,
            )
        ]
        self.codex.resume_errors.append(RuntimeError("resume failed"))

        with self.assertRaises(ThreadLifecycleStateUnknown):
            await self.runtime.unarchive_binding(binding)

        self.assertEqual(self.codex.unarchive_calls, ["native-1"])
        self.assertEqual(self.codex.resume_calls, [("native-1", {})])
        self.assertFalse(self.store.get(binding.id).active)
        self.assertIsNone(self.store.active_binding(self.scope.key))
        self.assertEqual(
            self.runtime.lifecycle_state(binding.id).state,
            ThreadLifecycleState.UNKNOWN,
        )
        with self.assertRaises(RuntimeClosed):
            await self.runtime.capture_submission_admission(binding.id)

    async def test_thread_metadata_rejects_repeated_pagination_cursor(self) -> None:
        self.codex.thread_list_pages = [
            SimpleNamespace(data=[], next_cursor="repeat"),
            SimpleNamespace(data=[], next_cursor="repeat"),
        ]

        with self.assertRaisesRegex(RuntimeError, "pagination cursor"):
            await self.runtime.thread_metadata(("native-missing",))

    async def test_complete_native_catalog_is_bounded_and_paginated(self) -> None:
        self.codex.thread_list_pages = [
            SimpleNamespace(
                data=[SimpleNamespace(id="native-1", name="One", preview="first")],
                next_cursor="page-two",
            ),
            SimpleNamespace(
                data=[SimpleNamespace(id="native-2", name=None, preview="second")],
                next_cursor=None,
            ),
        ]

        catalog = await self.runtime.thread_catalog(
            archived=True,
            deadline=asyncio.get_running_loop().time() + 1,
        )

        self.assertTrue(catalog.archived)
        self.assertEqual(
            tuple(thread.thread_id for thread in catalog.threads),
            ("native-1", "native-2"),
        )
        self.assertEqual(catalog.by_id()["native-1"].name, "One")
        self.assertEqual(len(self.codex.thread_list_calls), 2)

    async def test_complete_native_catalog_fails_whole_read_at_limits(self) -> None:
        with self.assertRaises(ThreadCatalogDeadlineExceeded):
            await self.runtime.thread_catalog(
                archived=False,
                deadline=asyncio.get_running_loop().time(),
            )
        self.codex.thread_list_pages = [
            SimpleNamespace(
                data=[
                    SimpleNamespace(id="native-1", name=None, preview="one"),
                    SimpleNamespace(id="native-2", name=None, preview="two"),
                ],
                next_cursor=None,
            )
        ]
        with self.assertRaises(ThreadCatalogLimitExceeded):
            await self.runtime.thread_catalog(
                archived=False,
                deadline=asyncio.get_running_loop().time() + 1,
                max_items=1,
            )

    async def submit(
        self,
        binding,
        text: object = "hello",
        admission: SubmissionAdmission | None = None,
        context_commit: ContextCursorCommit | None = None,
    ):
        return await self.runtime.submit(
            binding=binding,
            cwd=self.cwd,
            input=text,
            owner_id="ou_user",
            origin=object(),
            admission=admission,
            context_commit=context_commit,
        )

    def install_model_catalog(
        self,
        *,
        model_id: str = "catalog-model",
        model: str = "wire-model",
        effort_id: str = "dynamic-effort",
        service_tier_id: str = "priority-v2",
    ) -> object:
        effort_wire = SimpleNamespace(value=effort_id)
        self.codex.model_response = SimpleNamespace(
            data=[
                SimpleNamespace(
                    id=model_id,
                    model=model,
                    display_name="Catalog Model",
                    description="dynamic",
                    is_default=True,
                    default_reasoning_effort=effort_wire,
                    default_service_tier=None,
                    supported_reasoning_efforts=[
                        SimpleNamespace(
                            reasoning_effort=effort_wire,
                            description="dynamic effort",
                        )
                    ],
                    service_tiers=[
                        SimpleNamespace(
                            id=service_tier_id,
                            name="Fast v2",
                            description="dynamic tier",
                        )
                    ],
                )
            ],
            next_cursor=None,
        )
        return effort_wire

    async def finish(self, handle: FakeTurnHandle, submission) -> None:
        handle.complete()
        submission.release_receipt_attempt()
        await self.runtime.wait_idle()

    async def test_first_prompt_is_lazy_start_with_only_native_cwd(self) -> None:
        binding = self.binding()
        self.assertIsNone(binding.native_thread_id)
        self.assertEqual(self.codex.start_kwargs, [])

        submission = await self.submit(binding)

        self.assertEqual(submission.disposition, SubmitDisposition.STARTED)
        self.assertEqual(self.codex.start_kwargs, [{"cwd": str(self.cwd)}])
        self.assertEqual(self.codex.turn_inputs, [("native-1", "hello")])
        self.assertEqual(
            self.codex.turn_calls,
            [("native-1", "hello", {})],
        )
        self.assertEqual(
            self.store.get(binding.id).native_thread_id,
            "native-1",
        )
        await self.finish(self.codex.handles[0], submission)
        self.assertEqual(self.codex.handles[0].run_calls, 0)
        self.assertEqual(self.outcomes[0].final_response, "done:turn-1")
        self.assertIn(("native-1", False), self.codex.read_calls)
        self.assertIn(("native-1", True), self.codex.read_calls)

    async def test_catch_up_start_commits_boundary_after_native_acceptance(
        self,
    ) -> None:
        binding = self.catch_up_binding()
        admission = await self.runtime.capture_submission_admission(binding.id)
        upper = MessageContextAnchor("om-upper", 2_000)

        submission = await self.submit(
            binding,
            "catch up",
            admission,
            ContextCursorCommit(admission.context_revision, upper),
        )

        updated = self.store.get(binding.id)
        self.assertEqual(updated.context_anchor, upper)
        self.assertEqual(updated.context_revision, 2)
        self.assertEqual(submission.disposition, SubmitDisposition.STARTED)
        await self.finish(self.codex.handles[0], submission)

    async def test_catch_up_steer_commits_next_boundary_exactly_once(self) -> None:
        binding = self.catch_up_binding()
        first_admission = await self.runtime.capture_submission_admission(binding.id)
        first_upper = MessageContextAnchor("om-upper-1", 2_000)
        first = await self.submit(
            binding,
            "first",
            first_admission,
            ContextCursorCommit(first_admission.context_revision, first_upper),
        )
        running_admission = await self.runtime.capture_submission_admission(binding.id)
        second_upper = MessageContextAnchor("om-upper-2", 3_000)

        steered = await self.submit(
            self.store.get(binding.id),
            "adjust",
            running_admission,
            ContextCursorCommit(running_admission.context_revision, second_upper),
        )

        updated = self.store.get(binding.id)
        self.assertEqual(steered.disposition, SubmitDisposition.STEERED)
        self.assertEqual(updated.context_anchor, second_upper)
        self.assertEqual(updated.context_revision, 3)
        self.assertEqual(self.codex.handles[0].steers, ["adjust"])
        await self.finish(self.codex.handles[0], first)

    async def test_failed_catch_up_steer_does_not_advance_boundary(self) -> None:
        binding = self.catch_up_binding()
        first_admission = await self.runtime.capture_submission_admission(binding.id)
        first_upper = MessageContextAnchor("om-upper-1", 2_000)
        first = await self.submit(
            binding,
            "first",
            first_admission,
            ContextCursorCommit(first_admission.context_revision, first_upper),
        )
        admission = await self.runtime.capture_submission_admission(binding.id)
        self.codex.handles[0].steer_error = InvalidRequestError(
            -32600,
            "already done",
        )

        with self.assertRaises(SteerRace):
            await self.submit(
                self.store.get(binding.id),
                "must retry",
                admission,
                ContextCursorCommit(
                    admission.context_revision,
                    MessageContextAnchor("om-upper-2", 3_000),
                ),
            )

        updated = self.store.get(binding.id)
        self.assertEqual(updated.context_anchor, first_upper)
        self.assertEqual(updated.context_revision, 2)
        await self.finish(self.codex.handles[0], first)

    async def test_unconfirmed_catch_up_start_does_not_advance_boundary(self) -> None:
        binding = self.catch_up_binding()
        admission = await self.runtime.capture_submission_admission(binding.id)
        self.codex.turn_errors_after_start.append(RuntimeError("response lost"))

        with self.assertRaisesRegex(TurnStartFailed, "启动结果未确认"):
            await self.submit(
                binding,
                "unconfirmed",
                admission,
                ContextCursorCommit(
                    admission.context_revision,
                    MessageContextAnchor("om-upper", 2_000),
                ),
            )

        updated = self.store.get(binding.id)
        self.assertEqual(updated.context_anchor, binding.context_anchor)
        self.assertEqual(updated.context_revision, 1)

    async def test_cursor_commit_failure_keeps_native_tracking_and_closes_admission(
        self,
    ) -> None:
        binding = self.catch_up_binding()
        admission = await self.runtime.capture_submission_admission(binding.id)
        upper = MessageContextAnchor("om-upper", 2_000)

        with (
            patch.object(
                self.store,
                "commit_context_anchor",
                side_effect=BindingConflict("CAS failed"),
            ),
            self.assertRaisesRegex(
                ContextBoundaryCommitFailed,
                "任务已被 Codex 接受",
            ),
        ):
            await self.submit(
                binding,
                "accepted before CAS",
                admission,
                ContextCursorCommit(admission.context_revision, upper),
            )

        self.assertIsNotNone(self.runtime.active_turn(binding.id))
        self.assertEqual(self.store.get(binding.id).context_anchor, binding.context_anchor)
        with self.assertRaises(RuntimeClosed):
            await self.runtime.capture_submission_admission(binding.id)
        self.codex.handles[0].complete()
        self.assertTrue(await self.runtime.wait_idle(timeout=0.2))

    async def test_public_turn_stream_tracks_completed_context_usage(self) -> None:
        binding = self.binding()
        self.assertIsNone(self.runtime.context_window_usage(binding.id))
        submission = await self.submit(binding)
        handle = self.codex.handles[0]

        async with asyncio.timeout(0.1):
            while not self.runtime._active[binding.id].terminal_stream_safe:
                await asyncio.sleep(0)
        handle.notifications.put_nowait(token_usage_notification())
        self.assertIsNone(self.runtime.context_window_usage(binding.id))
        await self.finish(handle, submission)

        self.assertEqual(
            self.runtime.context_window_usage(binding.id),
            ContextWindowUsage(
                used_tokens=25_000,
                context_window_tokens=100_000,
            ),
        )
        self.assertEqual(handle.stream_calls, 1)
        self.assertEqual(handle.run_calls, 0)
        compact = await self.runtime.compact(
            binding=self.store.get(binding.id),
            owner_id="ou_user",
            origin=object(),
        )
        self.assertIsNone(self.runtime.context_window_usage(binding.id))
        compact.release_receipt_attempt()
        self.codex.finish_compaction()
        await self.runtime.wait_idle()

    async def test_public_turn_stream_carries_only_latest_aggregate_diff(
        self,
    ) -> None:
        binding = self.binding()
        submission = await self.submit(binding)
        handle = self.codex.handles[0]
        async with asyncio.timeout(0.1):
            while not self.runtime._active[binding.id].terminal_stream_safe:
                await asyncio.sleep(0)

        handle.notifications.put_nowait(
            turn_diff_notification("diff --git a/old.txt b/old.txt\n")
        )
        latest = "diff --git a/report.md b/report.md\n"
        handle.notifications.put_nowait(turn_diff_notification(latest))
        await self.finish(handle, submission)

        self.assertEqual(self.outcomes[0].turn_diff, latest)
        self.assertEqual(handle.stream_calls, 1)

    async def test_usage_stream_failure_does_not_override_persisted_terminal(
        self,
    ) -> None:
        binding = self.binding()
        submission = await self.submit(binding)
        handle = self.codex.handles[0]
        async with asyncio.timeout(0.1):
            while not self.runtime._active[binding.id].terminal_stream_safe:
                await asyncio.sleep(0)

        observed = "diff --git a/report.md b/report.md\n"
        handle.notifications.put_nowait(turn_diff_notification(observed))
        handle.notifications.put_nowait(RuntimeError("transport closed"))
        handle.complete()
        submission.release_receipt_attempt()
        with self.assertLogs("netizen.codex_runtime", level="WARNING"):
            await self.runtime.wait_idle()

        self.assertEqual(handle.stream_calls, 1)
        self.assertIsNone(self.runtime.context_window_usage(binding.id))
        self.assertEqual(len(self.outcomes), 1)
        self.assertIsNone(self.outcomes[0].error)
        self.assertEqual(self.outcomes[0].final_response, "done:turn-1")
        self.assertEqual(self.outcomes[0].turn_diff, observed)

    async def test_turn_diff_ignores_wrong_identity_and_invalid_shape(self) -> None:
        binding = self.binding()
        submission = await self.submit(binding)
        handle = self.codex.handles[0]
        async with asyncio.timeout(0.1):
            while not self.runtime._active[binding.id].terminal_stream_safe:
                await asyncio.sleep(0)

        handle.notifications.put_nowait(
            turn_diff_notification(
                "diff --git a/wrong.txt b/wrong.txt\n",
                turn_id="turn-other",
            )
        )
        handle.notifications.put_nowait(
            Notification(
                "turn/diff/updated",
                SimpleNamespace(
                    thread_id="native-1",
                    turn_id="turn-1",
                    diff=object(),
                ),
            )
        )
        with self.assertLogs("netizen.codex_runtime", level="WARNING"):
            await self.finish(handle, submission)

        self.assertIsNone(self.outcomes[0].turn_diff)

    async def test_followup_keeps_previous_usage_until_unobserved_terminal(
        self,
    ) -> None:
        binding = self.binding()
        first = await self.submit(binding)
        first_handle = self.codex.handles[0]
        async with asyncio.timeout(0.1):
            while not self.runtime._active[binding.id].terminal_stream_safe:
                await asyncio.sleep(0)
        first_handle.notifications.put_nowait(token_usage_notification())
        await self.finish(first_handle, first)
        self.assertIsNotNone(self.runtime.context_window_usage(binding.id))

        self.codex.complete_immediately = True
        second = await self.submit(self.store.get(binding.id), "instant")
        self.assertIsNotNone(self.runtime.context_window_usage(binding.id))
        second.release_receipt_attempt()
        await self.runtime.wait_idle()

        self.assertEqual(self.codex.handles[1].stream_calls, 0)
        self.assertIsNone(self.runtime.context_window_usage(binding.id))
        self.assertIsNone(self.outcomes[1].turn_diff)

    async def test_followup_replaces_previous_usage_at_observed_terminal(
        self,
    ) -> None:
        binding = self.binding()
        first = await self.submit(binding)
        first_handle = self.codex.handles[0]
        async with asyncio.timeout(0.1):
            while not self.runtime._active[binding.id].terminal_stream_safe:
                await asyncio.sleep(0)
        first_handle.notifications.put_nowait(token_usage_notification())
        await self.finish(first_handle, first)

        second = await self.submit(self.store.get(binding.id), "follow up")
        self.assertEqual(
            self.runtime.context_window_usage(binding.id),
            ContextWindowUsage(25_000, 100_000),
        )
        second_handle = self.codex.handles[1]
        async with asyncio.timeout(0.1):
            while not self.runtime._active[binding.id].terminal_stream_safe:
                await asyncio.sleep(0)
        second_handle.notifications.put_nowait(
            token_usage_notification(turn_id="turn-2", used_tokens=40_000)
        )
        await self.finish(second_handle, second)

        self.assertEqual(
            self.runtime.context_window_usage(binding.id),
            ContextWindowUsage(40_000, 100_000),
        )

    async def test_turn_start_response_loss_invalidates_previous_usage(self) -> None:
        binding = self.binding()
        first = await self.submit(binding)
        first_handle = self.codex.handles[0]
        async with asyncio.timeout(0.1):
            while not self.runtime._active[binding.id].terminal_stream_safe:
                await asyncio.sleep(0)
        first_handle.notifications.put_nowait(token_usage_notification())
        await self.finish(first_handle, first)
        self.assertIsNotNone(self.runtime.context_window_usage(binding.id))

        self.codex.turn_errors_after_start.append(RuntimeError("response lost"))
        with self.assertRaises(TurnStartFailed):
            await self.submit(self.store.get(binding.id), "unknown")

        self.assertIsNone(self.runtime.context_window_usage(binding.id))

    async def test_multiple_skills_compile_one_typed_new_turn(self) -> None:
        catalog = FakeSkillCatalog(fake_skills())
        self.runtime._skill_catalog = catalog
        binding = self.binding()

        submission = await self.runtime.submit(
            binding=binding,
            cwd=self.cwd,
            input="$code-review $test-triage inspect",
            owner_id="ou_user",
            origin=object(),
            skill_names=("code-review", "test-triage"),
        )

        native_input = self.codex.turn_inputs[0][1]
        self.assertEqual(len(native_input), 3)
        self.assertIsInstance(native_input[0], TextInput)
        self.assertEqual(native_input[0].text, "$code-review $test-triage inspect")
        self.assertEqual(
            [(item.name, item.path) for item in native_input[1:]],
            [
                ("code-review", "/tmp/code-review/SKILL.md"),
                ("test-triage", "/tmp/test-triage/SKILL.md"),
            ],
        )
        self.assertTrue(all(isinstance(item, SkillInput) for item in native_input[1:]))
        self.assertEqual(catalog.calls, [(self.cwd, True)])
        await self.finish(self.codex.handles[0], submission)

    async def test_skill_reference_preserves_native_multimodal_input(self) -> None:
        catalog = FakeSkillCatalog(fake_skills())
        self.runtime._skill_catalog = catalog
        binding = self.binding()
        multimodal_input = [
            TextInput("inspect this image"),
            ImageInput("data:image/png;base64,AA=="),
        ]

        submission = await self.runtime.submit(
            binding=binding,
            cwd=self.cwd,
            input=multimodal_input,
            owner_id="ou_user",
            origin=object(),
            skill_names=("code-review",),
        )

        native_input = self.codex.turn_inputs[0][1]
        self.assertEqual(native_input[:2], multimodal_input)
        self.assertIsInstance(native_input[2], SkillInput)
        self.assertEqual(native_input[2].name, "code-review")
        self.assertEqual(catalog.calls, [(self.cwd, True)])
        await self.finish(self.codex.handles[0], submission)

    async def test_running_skill_message_steers_exact_turn_once(self) -> None:
        catalog = FakeSkillCatalog(fake_skills())
        self.runtime._skill_catalog = catalog
        binding = self.binding()
        first = await self.submit(binding, "first")

        steered = await self.runtime.submit(
            binding=self.store.get(binding.id),
            cwd=self.cwd,
            input="$code-review inspect current turn",
            owner_id="ou_user",
            origin=object(),
            skill_names=("code-review",),
        )

        self.assertEqual(steered.disposition, SubmitDisposition.STEERED)
        self.assertEqual(len(self.codex.handles), 1)
        self.assertEqual(len(self.codex.handles[0].steers), 1)
        native_input = self.codex.handles[0].steers[0]
        self.assertIsInstance(native_input[0], TextInput)
        self.assertIsInstance(native_input[1], SkillInput)
        await self.finish(self.codex.handles[0], first)

    async def test_skill_resolution_failures_do_not_start_or_steer(self) -> None:
        catalog = FakeSkillCatalog(fake_skills())
        self.runtime._skill_catalog = catalog
        binding = self.binding()
        with self.assertRaisesRegex(SkillReferenceError, "找不到"):
            await self.runtime.submit(
                binding=binding,
                cwd=self.cwd,
                input="$missing inspect",
                owner_id="ou_user",
                origin=object(),
                skill_names=("missing",),
            )
        self.assertEqual(self.codex.start_kwargs, [])

        catalog.skills = (
            fake_skills()[0],
            DiscoveredSkill(
                "code-review",
                "/tmp/other/SKILL.md",
                "Other",
                "user",
                True,
            ),
        )
        with self.assertRaisesRegex(SkillReferenceError, "歧义"):
            await self.runtime.submit(
                binding=binding,
                cwd=self.cwd,
                input="$code-review inspect",
                owner_id="ou_user",
                origin=object(),
                skill_names=("code-review",),
            )
        self.assertEqual(self.codex.start_kwargs, [])

    async def test_skill_discovery_uses_admission_and_does_not_hold_binding_lock(
        self,
    ) -> None:
        catalog = FakeSkillCatalog(fake_skills())
        catalog.gate = asyncio.Event()
        self.runtime._skill_catalog = catalog
        binding = self.binding()
        first = await self.submit(binding, "first")
        delayed = asyncio.create_task(
            self.runtime.submit(
                binding=self.store.get(binding.id),
                cwd=self.cwd,
                input="$code-review delayed",
                owner_id="ou_user",
                origin=object(),
                skill_names=("code-review",),
            )
        )
        await catalog.called.wait()

        stop_result = await asyncio.wait_for(
            self.runtime.stop(binding.id),
            timeout=0.1,
        )
        self.assertEqual(stop_result, StopDisposition.REQUESTED)
        catalog.gate.set()
        with self.assertRaises(SteerRace):
            await delayed
        first.release_receipt_attempt()
        await self.runtime.wait_idle()

    async def test_goal_is_one_slot_across_native_completion(self) -> None:
        control = FakeGoalControl(self.codex)
        self.runtime._goal_control = control
        binding = self.binding()

        submission = await self.runtime.start_goal(
            binding=binding,
            cwd=self.cwd,
            objective="ship safely",
            owner_id="ou_user",
            origin=object(),
        )
        submission.release_receipt_attempt()

        active = self.runtime.active_goal(binding.id)
        self.assertEqual(active.state, GoalOperationState.RUNNING)
        self.assertEqual(control.start_calls, [("native-1", "ship safely")])
        with self.assertRaises(ThreadGoalActive):
            await self.submit(self.store.get(binding.id), "ordinary prompt")

        control.handles[0].finish(response="goal final")
        await self.runtime.wait_idle()

        self.assertIsNone(self.runtime.active_goal(binding.id))
        outcome = self.outcomes[-1]
        self.assertIsInstance(outcome, GoalOutcome)
        self.assertEqual(outcome.goal.status, GoalStatus.COMPLETE)
        self.assertEqual(outcome.final_response, "goal final")

    async def test_goal_stop_pauses_interrupts_and_cleans_before_release(self) -> None:
        control = FakeGoalControl(self.codex)
        self.runtime._goal_control = control
        binding = self.binding()
        submission = await self.runtime.start_goal(
            binding=binding,
            cwd=self.cwd,
            objective="pause me",
            owner_id="ou_user",
            origin=object(),
        )
        submission.release_receipt_attempt()

        result = await self.runtime.stop(binding.id)

        self.assertEqual(result, StopDisposition.GOAL_REQUESTED)
        self.assertEqual(control.persisted.status, GoalStatus.PAUSED)
        self.assertEqual(control.handles[0].pause_calls, 1)
        self.assertEqual(self.cleanup.calls, ["native-1"])
        await self.runtime.wait_idle()
        self.assertIsNone(self.runtime.active_goal(binding.id))
        outcome = self.outcomes[-1]
        self.assertIsInstance(outcome, GoalOutcome)
        self.assertTrue(outcome.background_cleanup_requested)

    async def test_goal_cleanup_failure_keeps_slot_and_repeat_stop_retries(self) -> None:
        control = FakeGoalControl(self.codex)
        self.runtime._goal_control = control
        binding = self.binding()
        submission = await self.runtime.start_goal(
            binding=binding,
            cwd=self.cwd,
            objective="pause me",
            owner_id="ou_user",
            origin=object(),
        )
        submission.release_receipt_attempt()
        self.cleanup.failures.append(RuntimeError("cleanup unavailable"))

        with self.assertRaises(TerminalCleanupFailed):
            await self.runtime.stop(binding.id)
        self.assertIsNotNone(self.runtime.active_goal(binding.id))

        result = await self.runtime.stop(binding.id)
        self.assertEqual(result, StopDisposition.GOAL_REQUESTED)
        await self.runtime.wait_idle()
        self.assertIsNone(self.runtime.active_goal(binding.id))
        self.assertEqual(self.cleanup.calls, ["native-1", "native-1"])

    async def test_external_active_goal_is_detected_and_never_reattached(self) -> None:
        binding = self.binding()
        first = await self.submit(binding, "materialize")
        await self.finish(self.codex.handles[0], first)
        control = FakeGoalControl(self.codex)
        control.persisted = goal_snapshot(GoalStatus.ACTIVE)
        self.runtime._goal_control = control

        with self.assertRaises(ExternalGoalActive):
            await self.submit(self.store.get(binding.id), "must not run")

        active = self.runtime.active_goal(binding.id)
        self.assertEqual(active.state, GoalOperationState.EXTERNAL_ACTIVE)
        self.assertEqual(
            await self.runtime.stop(binding.id),
            StopDisposition.EXTERNAL_GOAL,
        )
        self.assertEqual(len(self.codex.handles), 1)

    async def test_goal_start_response_loss_retains_unknown_slot_and_closes_admission(
        self,
    ) -> None:
        control = FakeGoalControl(self.codex)
        control.start_errors.append(RuntimeError("lost response"))
        self.runtime._goal_control = control
        binding = self.binding()

        with self.assertRaises(GoalStateUnknown):
            await self.runtime.start_goal(
                binding=binding,
                cwd=self.cwd,
                objective="unknown",
                owner_id="ou_user",
                origin=object(),
            )

        self.assertEqual(
            self.runtime.active_goal(binding.id).state,
            GoalOperationState.UNKNOWN,
        )
        with self.assertRaises(RuntimeClosed):
            await self.submit(self.store.get(binding.id), "blocked globally")

    async def test_paused_goal_can_resume_and_clear_without_start_path(self) -> None:
        binding = self.binding()
        first = await self.submit(binding, "materialize")
        await self.finish(self.codex.handles[0], first)
        control = FakeGoalControl(self.codex)
        control.persisted = goal_snapshot(GoalStatus.PAUSED)
        self.runtime._goal_control = control

        resumed = await self.runtime.resume_goal(
            binding=self.store.get(binding.id),
            owner_id="ou_user",
            origin=object(),
        )
        resumed.release_receipt_attempt()
        self.assertEqual(control.resume_calls, ["native-1"])
        control.handles[0].finish()
        await self.runtime.wait_idle()

        control.persisted = goal_snapshot(GoalStatus.PAUSED)
        cleared = await self.runtime.clear_goal(self.store.get(binding.id))
        self.assertTrue(cleared)
        self.assertEqual(control.clear_calls, ["native-1"])
        self.assertIsNone(control.persisted)

    async def test_cancelled_goal_resume_keeps_route_owned_without_second_cleanup(
        self,
    ) -> None:
        binding = self.binding()
        first = await self.submit(binding, "materialize")
        await self.finish(self.codex.handles[0], first)

        class CancelledResumeControl(FakeGoalControl):
            async def resume(self, thread_id: str) -> FakeGoalHandle:
                self.resume_calls.append(thread_id)
                handle = FakeGoalHandle(self, "goal-turn-cancelled-resume")
                self.handles.append(handle)
                error = asyncio.CancelledError()
                error.goal_handle = handle
                raise error

        control = CancelledResumeControl(self.codex)
        control.persisted = goal_snapshot(GoalStatus.PAUSED)
        self.runtime._goal_control = control

        with self.assertRaises(asyncio.CancelledError):
            await self.runtime.resume_goal(
                binding=self.store.get(binding.id),
                owner_id="ou_user",
                origin=object(),
            )

        active = self.runtime.active_goal(binding.id)
        self.assertEqual(active.state, GoalOperationState.UNKNOWN)
        self.assertIs(self.runtime._goals[binding.id].handle, control.handles[0])
        with self.assertLogs("netizen.codex_runtime", level="WARNING"):
            await self.runtime.interrupt_all()
        self.assertEqual(control.handles[0].pause_calls, 0)
        self.assertEqual(self.cleanup.calls, [])
        await self.runtime.cancel_tasks()
        self.assertTrue(control.handles[0].closed)

    async def test_goal_resume_cannot_recreate_slot_after_final_teardown(self) -> None:
        binding = self.binding()
        first = await self.submit(binding, "materialize")
        await self.finish(self.codex.handles[0], first)

        class GatedResumeControl(FakeGoalControl):
            def __init__(self, codex: FakeCodex) -> None:
                super().__init__(codex)
                self.entered = asyncio.Event()
                self.gate = asyncio.Event()

            async def resume(self, thread_id: str) -> FakeGoalHandle:
                self.resume_calls.append(thread_id)
                self.entered.set()
                await self.gate.wait()
                assert self.persisted is not None
                self.persisted = goal_snapshot(
                    GoalStatus.ACTIVE,
                    objective=self.persisted.objective,
                )
                handle = FakeGoalHandle(self, "goal-turn-after-teardown")
                self.handles.append(handle)
                return handle

        control = GatedResumeControl(self.codex)
        control.persisted = goal_snapshot(GoalStatus.PAUSED)
        self.runtime._goal_control = control
        resume = asyncio.create_task(
            self.runtime.resume_goal(
                binding=self.store.get(binding.id),
                owner_id="ou_user",
                origin=object(),
            )
        )
        await control.entered.wait()

        await self.runtime.cancel_tasks()
        control.gate.set()
        with self.assertRaisesRegex(GoalStateUnknown, "恢复期间"):
            await resume

        self.assertIsNone(self.runtime.active_goal(binding.id))
        self.assertEqual(self.runtime._tasks, set())
        with self.assertRaises(RuntimeClosed):
            await self.submit(self.store.get(binding.id), "must remain closed")

    async def test_binding_settings_apply_to_every_new_turn(
        self,
    ) -> None:
        effort_wire = self.install_model_catalog()
        settings = BindingTurnSettings(
            "catalog-model",
            "dynamic-effort",
            "priority-v2",
        )
        binding = self.store.create_binding(
            scope=self.scope,
            project_alias="test",
            creator_id="ou_user",
            turn_settings=settings,
        )

        submission = await self.submit(binding, "configured first prompt")

        self.assertEqual(self.codex.model_calls, 1)
        self.assertEqual(self.codex.start_kwargs, [{"cwd": str(self.cwd)}])
        self.assertEqual(
            self.codex.turn_calls,
            [
                (
                    "native-1",
                    "configured first prompt",
                    {
                        "model": "wire-model",
                        "effort": effort_wire,
                        "service_tier": "priority-v2",
                    },
                )
            ],
        )
        persisted = self.store.get(binding.id)
        self.assertEqual(persisted.turn_settings, settings)
        self.assertEqual(persisted.settings_revision, 1)
        await self.finish(self.codex.handles[0], submission)

        followup = await self.submit(persisted, "ordinary followup")

        self.assertEqual(self.codex.model_calls, 2)
        self.assertEqual(self.codex.resume_calls, [("native-1", {})])
        self.assertEqual(
            self.codex.turn_calls[-1],
            (
                "native-1",
                "ordinary followup",
                {
                    "model": "wire-model",
                    "effort": effort_wire,
                    "service_tier": "priority-v2",
                },
            ),
        )
        await self.finish(self.codex.handles[1], followup)

    async def test_config_saves_persistent_settings_for_resumed_turns(self) -> None:
        effort_wire = self.install_model_catalog(service_tier_id="priority-v2")
        binding = self.binding()
        first = await self.submit(binding, "first")
        await self.finish(self.codex.handles[0], first)
        settings = BindingTurnSettings(
            "catalog-model",
            "dynamic-effort",
            "default",
        )

        configured = await self.runtime.configure_turn_settings(
            binding_id=binding.id,
            expected_revision=1,
            settings=settings,
        )

        self.assertEqual(configured.turn_settings, settings)
        self.assertEqual(configured.settings_revision, 2)
        second = await self.submit(configured, "configured next prompt")
        self.assertEqual(self.codex.resume_calls, [("native-1", {})])
        self.assertEqual(
            self.codex.turn_calls[-1],
            (
                "native-1",
                "configured next prompt",
                {
                    "model": "wire-model",
                    "effort": effort_wire,
                    "service_tier": "default",
                },
            ),
        )
        self.assertEqual(self.store.get(binding.id).turn_settings, settings)
        self.assertEqual(self.store.get(binding.id).settings_revision, 2)
        await self.finish(self.codex.handles[1], second)

        third = await self.submit(self.store.get(binding.id), "configured again")
        self.assertEqual(self.codex.model_calls, 2)
        self.assertEqual(
            self.codex.turn_calls[-1],
            (
                "native-1",
                "configured again",
                {
                    "model": "wire-model",
                    "effort": effort_wire,
                    "service_tier": "default",
                },
            ),
        )
        await self.finish(self.codex.handles[2], third)

    async def test_config_rejects_running_turn_without_steering(self) -> None:
        binding = self.binding()
        first = await self.submit(binding, "first")

        with self.assertRaisesRegex(ThreadRunningConfiguration, "不能修改"):
            await self.runtime.configure_turn_settings(
                binding_id=binding.id,
                expected_revision=1,
                settings=BindingTurnSettings(
                    "catalog-model",
                    "dynamic-effort",
                    "priority-v2",
                ),
            )

        self.assertEqual(self.codex.handles[0].steers, [])
        self.assertIsNone(self.store.get(binding.id).turn_settings)
        await self.finish(self.codex.handles[0], first)

    async def test_steer_never_resolves_or_applies_binding_settings(self) -> None:
        binding = self.binding()
        first = await self.submit(binding, "first")
        settings = BindingTurnSettings(
            "catalog-model",
            "dynamic-effort",
            "priority-v2",
        )
        # Simulate a stale/external persistence race that the Runtime's config
        # gate normally prevents while a Turn is active.
        persisted = self.store.set_turn_settings(
            binding_id=binding.id,
            expected_revision=1,
            settings=settings,
        )

        steered = await self.submit(persisted, "steer only")

        self.assertEqual(steered.disposition, SubmitDisposition.STEERED)
        self.assertEqual(self.codex.handles[0].steers, ["steer only"])
        self.assertEqual(self.codex.model_calls, 0)
        self.assertEqual(
            self.store.get(binding.id).turn_settings,
            settings,
        )
        await self.finish(self.codex.handles[0], first)

    async def test_invalid_configured_catalog_preserves_intent_without_starting(self) -> None:
        settings = BindingTurnSettings(
            "removed-model",
            "dynamic-effort",
            "priority-v2",
        )
        binding = self.store.create_binding(
            scope=self.scope,
            project_alias="test",
            creator_id="ou_user",
            turn_settings=settings,
        )

        with self.assertRaises(ModelCatalogError):
            await self.submit(binding)

        self.assertEqual(self.codex.start_kwargs, [])
        self.assertEqual(
            self.store.get(binding.id).turn_settings,
            settings,
        )

    async def test_turn_start_unknown_preserves_settings_and_closes_admission(
        self,
    ) -> None:
        self.install_model_catalog()
        settings = BindingTurnSettings(
            "catalog-model",
            "dynamic-effort",
            "priority-v2",
        )
        binding = self.store.create_binding(
            scope=self.scope,
            project_alias="test",
            creator_id="ou_user",
            turn_settings=settings,
        )
        self.codex.turn_errors_after_start.append(RuntimeError("lost response"))

        with self.assertRaises(TurnStartFailed):
            await self.submit(binding)

        self.assertEqual(
            self.store.get(binding.id).turn_settings,
            settings,
        )
        with self.assertRaises(RuntimeClosed):
            await self.submit(self.store.get(binding.id), "must stay closed")

    async def test_successful_configured_start_does_not_mutate_settings(self) -> None:
        self.install_model_catalog()
        settings = BindingTurnSettings(
            "catalog-model",
            "dynamic-effort",
            "priority-v2",
        )
        binding = self.store.create_binding(
            scope=self.scope,
            project_alias="test",
            creator_id="ou_user",
            turn_settings=settings,
        )

        with patch.object(
            self.store,
            "set_turn_settings",
            wraps=self.store.set_turn_settings,
        ) as set_settings:
            submission = await self.submit(binding)

        self.assertIsNotNone(self.runtime.active_turn(binding.id))
        set_settings.assert_not_called()
        self.assertEqual(
            self.store.get(binding.id).turn_settings,
            settings,
        )
        await self.finish(self.codex.handles[0], submission)

    async def test_config_change_invalidates_captured_submission(self) -> None:
        self.install_model_catalog()
        binding = self.binding()
        admission = await self.runtime.capture_submission_admission(binding.id)

        configured = await self.runtime.configure_turn_settings(
            binding_id=binding.id,
            expected_revision=1,
            settings=BindingTurnSettings(
                "catalog-model",
                "dynamic-effort",
                "priority-v2",
            ),
        )

        with self.assertRaises(SteerRace):
            await self.submit(configured, admission=admission)
        self.assertEqual(self.codex.start_kwargs, [])
        self.assertIsNotNone(
            self.store.get(binding.id).turn_settings
        )

    async def test_model_settings_are_resolved_from_each_live_catalog_read(self) -> None:
        effort_wire = self.install_model_catalog()

        settings = await self.runtime.resolve_model_settings(
            model_id="catalog-model",
            effort_id="dynamic-effort",
            service_tier_id="priority-v2",
        )

        self.assertEqual(self.codex.model_calls, 1)
        self.assertFalse(self.codex.last_include_hidden)
        self.assertEqual(settings.model, "wire-model")
        self.assertIs(settings.effort, effort_wire)
        self.assertEqual(settings.service_tier_name, "Fast v2")

    async def test_compact_reserves_binding_until_exact_native_turn_completes(
        self,
    ) -> None:
        binding = self.binding()
        first = await self.submit(binding, "before compact")
        await self.finish(self.codex.handles[0], first)
        origin = object()

        compact = await self.runtime.compact(
            binding=self.store.get(binding.id),
            owner_id="ou_user",
            origin=origin,
        )

        self.assertEqual(compact.thread_id, "native-1")
        self.assertEqual(self.codex.resume_calls, [("native-1", {})])
        self.assertEqual(self.codex.compact_calls, ["native-1"])
        self.assertTrue(self.runtime.is_compacting(binding.id))
        with self.assertRaisesRegex(ThreadCompacting, "正在压缩"):
            await self.submit(binding, "must not race compact")
        self.assertEqual(
            await self.runtime.stop(binding.id),
            StopDisposition.COMPACTING,
        )

        self.codex.finish_compaction()
        await asyncio.sleep(0)
        self.assertFalse(self.runtime.is_compacting(binding.id))
        self.assertEqual(len(self.outcomes), 1)
        compact.release_receipt_attempt()
        await self.runtime.wait_idle()

        outcome = self.outcomes[-1]
        self.assertIsInstance(outcome, CompactionOutcome)
        assert isinstance(outcome, CompactionOutcome)
        self.assertIs(outcome.origin, origin)
        self.assertEqual(outcome.compact_turn_id, "compact-1")
        self.assertEqual(outcome.status, "completed")
        self.assertIsNone(outcome.error)

    async def test_compact_requires_materialized_idle_binding(self) -> None:
        lazy = self.binding()
        with self.assertRaisesRegex(ThreadNotMaterialized, "尚未创建"):
            await self.runtime.compact(
                binding=lazy,
                owner_id="ou_user",
                origin=object(),
            )
        self.assertEqual(self.codex.start_kwargs, [])
        self.assertEqual(self.codex.compact_calls, [])

        first = await self.submit(lazy, "running")
        with self.assertRaisesRegex(ThreadRunningConfiguration, "不能压缩"):
            await self.runtime.compact(
                binding=self.store.get(lazy.id),
                owner_id="ou_user",
                origin=object(),
            )
        self.assertEqual(self.codex.compact_calls, [])
        await self.finish(self.codex.handles[0], first)

    async def test_compact_terminal_failure_releases_binding(self) -> None:
        binding = self.binding()
        first = await self.submit(binding, "before compact")
        await self.finish(self.codex.handles[0], first)

        compact = await self.runtime.compact(
            binding=self.store.get(binding.id),
            owner_id="ou_user",
            origin=object(),
        )
        compact.release_receipt_attempt()
        self.codex.finish_compaction(status="failed", message="cannot compact")
        await self.runtime.wait_idle()

        outcome = self.outcomes[-1]
        self.assertIsInstance(outcome, CompactionOutcome)
        assert isinstance(outcome, CompactionOutcome)
        self.assertIsInstance(outcome.error, CompactionFailed)
        self.assertIn("cannot compact", str(outcome.error))
        self.assertFalse(self.runtime.is_compacting(binding.id))

        next_turn = await self.submit(self.store.get(binding.id), "after failure")
        await self.finish(self.codex.handles[-1], next_turn)

    async def test_idle_without_new_context_item_is_not_compaction_completion(
        self,
    ) -> None:
        binding = self.binding()
        first = await self.submit(binding, "before compact")
        await self.finish(self.codex.handles[0], first)
        self.codex.compact_item_visible = False
        self.codex.complete_compact_immediately = True

        compact = await self.runtime.compact(
            binding=self.store.get(binding.id),
            owner_id="ou_user",
            origin=object(),
        )
        compact.release_receipt_attempt()
        await asyncio.sleep(0)

        self.assertTrue(self.runtime.is_compacting(binding.id))
        self.assertEqual(len(self.outcomes), 1)
        record = self.codex.compact_records[-1][1]
        record.items.append(
            SimpleNamespace(root=SimpleNamespace(type="contextCompaction"))
        )
        await self.runtime.wait_idle()
        self.assertFalse(self.runtime.is_compacting(binding.id))
        self.assertIsInstance(self.outcomes[-1], CompactionOutcome)

    async def test_compaction_invalidates_prepared_prompt_admission(self) -> None:
        binding = self.binding()
        first = await self.submit(binding, "before compact")
        await self.finish(self.codex.handles[0], first)
        admission = await self.runtime.capture_submission_admission(binding.id)

        compact = await self.runtime.compact(
            binding=self.store.get(binding.id),
            owner_id="ou_user",
            origin=object(),
        )
        compact.release_receipt_attempt()
        self.codex.finish_compaction()
        await self.runtime.wait_idle()

        with self.assertRaisesRegex(SteerRace, "状态已变化"):
            await self.submit(
                self.store.get(binding.id),
                "prepared before compact",
                admission=admission,
            )

    async def test_unknown_compact_start_fails_global_admission_closed(self) -> None:
        binding = self.binding()
        first = await self.submit(binding, "before compact")
        await self.finish(self.codex.handles[0], first)
        self.codex.compact_errors_after_start.append(RuntimeError("lost response"))

        with self.assertRaisesRegex(ThreadCompactStartFailed, "结果未确认"):
            await self.runtime.compact(
                binding=self.store.get(binding.id),
                owner_id="ou_user",
                origin=object(),
            )

        self.assertTrue(self.runtime.is_compacting(binding.id))
        with self.assertRaises(RuntimeClosed):
            await self.submit(self.store.get(binding.id), "must fail closed")
        await self.runtime.cancel_tasks()

    async def test_unknown_compact_terminal_retains_binding_and_closes_admission(
        self,
    ) -> None:
        binding = self.binding()
        first = await self.submit(binding, "before compact")
        await self.finish(self.codex.handles[0], first)

        compact = await self.runtime.compact(
            binding=self.store.get(binding.id),
            owner_id="ou_user",
            origin=object(),
        )
        compact.release_receipt_attempt()
        self.codex.read_errors.append(ValueError("unclassified read failure"))
        with self.assertLogs("netizen.codex_runtime", level="ERROR"):
            await self.runtime.wait_idle()

        outcome = self.outcomes[-1]
        self.assertIsInstance(outcome, CompactionOutcome)
        assert isinstance(outcome, CompactionOutcome)
        self.assertIsInstance(outcome.error, CompactionStateUnknown)
        self.assertTrue(self.runtime.is_compacting(binding.id))
        with self.assertRaises(RuntimeClosed):
            await self.submit(self.store.get(binding.id), "must fail closed")
        await self.runtime.cancel_tasks()

    async def test_multiple_new_compaction_turns_are_ambiguous_and_fail_closed(
        self,
    ) -> None:
        binding = self.binding()
        first = await self.submit(binding, "before compact")
        await self.finish(self.codex.handles[0], first)

        compact = await self.runtime.compact(
            binding=self.store.get(binding.id),
            owner_id="ou_user",
            origin=object(),
        )
        compact.release_receipt_attempt()
        self.codex.finish_compaction(status="failed", message="own failed")
        external = SimpleNamespace(
            id="external-compact",
            status=FakeStatus("completed"),
            error=None,
            items=[
                SimpleNamespace(root=SimpleNamespace(type="contextCompaction"))
            ],
        )
        self.codex.compact_records.append(("native-1", external))

        with self.assertLogs("netizen.codex_runtime", level="ERROR"):
            await self.runtime.wait_idle()

        outcome = self.outcomes[-1]
        self.assertIsInstance(outcome, CompactionOutcome)
        assert isinstance(outcome, CompactionOutcome)
        self.assertIsInstance(outcome.error, CompactionStateUnknown)
        self.assertIsNone(outcome.compact_turn_id)
        self.assertTrue(self.runtime.is_compacting(binding.id))
        with self.assertRaises(RuntimeClosed):
            await self.submit(self.store.get(binding.id), "must fail closed")
        await self.runtime.cancel_tasks()

    async def test_missing_compaction_terminal_times_out_fail_closed(self) -> None:
        binding = self.binding()
        first = await self.submit(binding, "before compact")
        await self.finish(self.codex.handles[0], first)
        self.codex.compact_item_visible = False
        self.codex.complete_compact_immediately = True

        compact = await self.runtime.compact(
            binding=self.store.get(binding.id),
            owner_id="ou_user",
            origin=object(),
        )
        compact.release_receipt_attempt()
        with (
            patch.object(self.runtime, "_compaction_timeout_seconds", 0.001),
            self.assertLogs("netizen.codex_runtime", level="ERROR"),
        ):
            await self.runtime.wait_idle()

        outcome = self.outcomes[-1]
        self.assertIsInstance(outcome, CompactionOutcome)
        assert isinstance(outcome, CompactionOutcome)
        self.assertIsInstance(outcome.error, CompactionStateUnknown)
        self.assertTrue(self.runtime.is_compacting(binding.id))
        await self.runtime.cancel_tasks()

    async def test_binding_write_failure_prevents_first_native_turn(self) -> None:
        binding = self.binding()

        with patch.object(
            self.store,
            "assign_native_thread_id",
            side_effect=RuntimeError("database unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                await self.submit(binding)

        self.assertEqual(self.codex.turn_inputs, [])
        self.assertEqual(self.codex.handles, [])
        self.assertEqual(self.cleanup.calls, [])
        self.assertIsNone(self.runtime.active_turn(binding.id))
        self.assertIsNone(self.store.get(binding.id).native_thread_id)
        with self.assertRaisesRegex(RuntimeError, "服务正在停止"):
            await self.submit(binding, "must restart")

    async def test_binding_conflict_never_cleans_another_binding_thread(self) -> None:
        owner = self.binding()
        self.store.assign_native_thread_id(owner.id, "native-1")
        failed = self.binding()

        with self.assertRaises(BindingConflict):
            await self.submit(failed)

        self.assertEqual(self.codex.turn_inputs, [])
        self.assertEqual(self.codex.handles, [])
        self.assertEqual(self.cleanup.calls, [])
        self.assertIsNone(self.store.get(failed.id).native_thread_id)
        self.assertEqual(
            self.store.get(owner.id).native_thread_id,
            "native-1",
        )

    async def test_mismatched_turn_thread_id_fails_closed_without_cleanup(self) -> None:
        binding = self.binding()
        self.codex.handle_thread_id_override = "native-other"

        with self.assertRaisesRegex(RuntimeError, "different Thread ID"):
            await self.submit(binding)

        self.assertEqual(self.codex.handles[0].interrupt_count, 0)
        self.assertEqual(self.cleanup.calls, [])
        self.assertIsNone(self.runtime.active_turn(binding.id))
        self.assertEqual(self.store.get(binding.id).native_thread_id, "native-1")

    async def test_turn_start_response_loss_closes_admission_without_retry(
        self,
    ) -> None:
        binding = self.binding()
        self.codex.turn_errors_after_start.append(RuntimeError("response lost"))

        with self.assertRaisesRegex(TurnStartFailed, "启动结果未确认"):
            await self.submit(binding, "first")

        self.assertEqual(self.store.get(binding.id).native_thread_id, "native-1")
        self.assertEqual(len(self.codex.handles), 1)
        self.assertEqual(self.codex.handles[0].record.status.value, "inProgress")
        self.assertIsNone(self.runtime.active_turn(binding.id))

        with self.assertRaisesRegex(RuntimeError, "服务正在停止"):
            await self.submit(self.store.get(binding.id), "must not start again")
        self.assertEqual(len(self.codex.handles), 1)

    async def test_cancelled_steer_closes_admission_because_rpc_may_continue(
        self,
    ) -> None:
        binding = self.binding()
        first = await self.submit(binding, "first")
        handle = self.codex.handles[0]
        handle.steer_gate = asyncio.Event()

        steering = asyncio.create_task(self.submit(binding, "steer"))
        await handle.steer_started.wait()
        steering.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await steering

        with self.assertRaisesRegex(RuntimeClosed, "服务正在停止"):
            await self.submit(binding, "must not submit after unknown steer")
        self.assertEqual(handle.steers, ["steer"])

        await self.finish(handle, first)

    async def test_thread_start_response_loss_closes_admission_without_retry(
        self,
    ) -> None:
        binding = self.binding()
        self.codex.start_errors.append(RuntimeError("response lost"))

        with self.assertRaisesRegex(TurnStartFailed, "启动或恢复结果未确认"):
            await self.submit(binding, "first")

        self.assertEqual(self.codex.start_kwargs, [{"cwd": str(self.cwd)}])
        self.assertIsNone(self.store.get(binding.id).native_thread_id)
        self.assertEqual(self.codex.handles, [])
        with self.assertRaisesRegex(RuntimeError, "服务正在停止"):
            await self.submit(binding, "must not retry")
        self.assertEqual(len(self.codex.start_kwargs), 1)

    async def test_thread_resume_response_loss_closes_admission_without_retry(
        self,
    ) -> None:
        binding = self.binding()
        self.store.assign_native_thread_id(binding.id, "native-existing")
        self.codex.resume_errors.append(RuntimeError("response lost"))

        with self.assertRaisesRegex(TurnStartFailed, "启动或恢复结果未确认"):
            await self.submit(binding, "next")

        self.assertEqual(self.codex.resume_calls, [("native-existing", {})])
        self.assertEqual(self.codex.handles, [])
        with self.assertRaisesRegex(RuntimeError, "服务正在停止"):
            await self.submit(binding, "must not retry")
        self.assertEqual(len(self.codex.resume_calls), 1)

    async def test_later_turn_resumes_exact_id_without_overrides(self) -> None:
        binding = self.binding()
        first = await self.submit(binding, "first")
        await self.finish(self.codex.handles[0], first)

        persisted = self.store.get(binding.id)
        second = await self.submit(persisted, "second")

        self.assertEqual(self.codex.resume_calls, [("native-1", {})])
        self.assertEqual(self.codex.turn_inputs[-1], ("native-1", "second"))
        await self.finish(self.codex.handles[1], second)
        self.assertEqual(self.outcomes[-1].turn_id, second.turn_id)
        self.assertEqual(self.outcomes[-1].final_response, "done:turn-2")

    async def test_stale_lazy_binding_snapshot_resumes_persisted_native_id(
        self,
    ) -> None:
        stale_binding = self.binding()
        first = await self.submit(stale_binding, "first")
        await self.finish(self.codex.handles[0], first)

        second = await self.submit(stale_binding, "second")

        self.assertEqual(self.codex.start_kwargs, [{"cwd": str(self.cwd)}])
        self.assertEqual(self.codex.resume_calls, [("native-1", {})])
        self.assertEqual(second.thread_id, "native-1")
        await self.finish(self.codex.handles[1], second)

    async def test_busy_binding_uses_the_same_native_handle_for_steer(self) -> None:
        binding = self.binding()
        first = await self.submit(binding, "first")

        second = await self.submit(binding, "change direction")

        self.assertEqual(second.disposition, SubmitDisposition.STEERED)
        self.assertEqual(second.turn_id, first.turn_id)
        self.assertEqual(self.codex.handles[0].steers, ["change direction"])
        self.assertEqual(len(self.codex.handles), 1)
        await self.finish(self.codex.handles[0], first)

    async def test_native_plan_replaces_in_memory_checklist_and_tracks_steer_freshness(
        self,
    ) -> None:
        observer = FakeTurnPlanObserver()
        self.runtime._turn_plan_observer = observer
        binding = self.binding()
        first = await self.submit(binding, "first")
        observer.append(
            thread_id=first.thread_id,
            turn_id=first.turn_id,
            steps=(
                TurnPlanStepSnapshot(
                    "inspect",
                    TurnPlanStepState.IN_PROGRESS,
                ),
                TurnPlanStepSnapshot("verify", TurnPlanStepState.PENDING),
            ),
        )

        before_steer = self.runtime.turn_progress(binding.id)
        self.assertIsNotNone(before_steer)
        assert before_steer is not None
        self.assertTrue(before_steer.plan_generated)
        self.assertFalse(before_steer.plan_may_be_stale)
        self.assertEqual(before_steer.steer_count, 0)

        steered = await self.submit(binding, "change direction")

        self.assertEqual(steered.disposition, SubmitDisposition.STEERED)
        stale = self.runtime.turn_progress(binding.id)
        assert stale is not None
        self.assertEqual(stale.steer_count, 1)
        self.assertTrue(stale.plan_may_be_stale)
        self.assertEqual([item.step for item in stale.steps], ["inspect", "verify"])

        observer.append(
            thread_id=first.thread_id,
            turn_id=first.turn_id,
            steps=(
                TurnPlanStepSnapshot("ship", TurnPlanStepState.COMPLETED),
            ),
        )
        refreshed = self.runtime.turn_progress(binding.id)
        assert refreshed is not None
        self.assertFalse(refreshed.plan_may_be_stale)
        self.assertEqual(refreshed.steer_count, 1)
        self.assertEqual([item.step for item in refreshed.steps], ["ship"])
        await self.finish(self.codex.handles[0], first)

    async def test_failed_native_steer_does_not_change_progress_confirmation(self) -> None:
        observer = FakeTurnPlanObserver()
        self.runtime._turn_plan_observer = observer
        binding = self.binding()
        first = await self.submit(binding, "first")
        observer.append(
            thread_id=first.thread_id,
            turn_id=first.turn_id,
            steps=(TurnPlanStepSnapshot("work", TurnPlanStepState.IN_PROGRESS),),
        )
        self.assertIsNotNone(self.runtime.turn_progress(binding.id))
        self.codex.handles[0].steer_error = InvalidRequestError(-32600, "done")

        with self.assertRaises(SteerRace):
            await self.submit(binding, "too late")

        progress = self.runtime.turn_progress(binding.id)
        assert progress is not None
        self.assertEqual(progress.steer_count, 0)
        self.assertFalse(progress.plan_may_be_stale)
        await self.finish(self.codex.handles[0], first)

    async def test_plan_observed_while_steer_rpc_is_pending_is_fresh_on_success(
        self,
    ) -> None:
        observer = FakeTurnPlanObserver()
        self.runtime._turn_plan_observer = observer
        binding = self.binding()
        first = await self.submit(binding, "first")
        observer.append(
            thread_id=first.thread_id,
            turn_id=first.turn_id,
            steps=(TurnPlanStepSnapshot("old", TurnPlanStepState.IN_PROGRESS),),
        )
        self.assertIsNotNone(self.runtime.turn_progress(binding.id))
        handle = self.codex.handles[0]
        handle.steer_gate = asyncio.Event()
        steering = asyncio.create_task(self.submit(binding, "new direction"))
        await handle.steer_started.wait()
        observer.append(
            thread_id=first.thread_id,
            turn_id=first.turn_id,
            steps=(TurnPlanStepSnapshot("new", TurnPlanStepState.PENDING),),
        )

        during_rpc = self.runtime.turn_progress(binding.id)
        assert during_rpc is not None
        self.assertEqual([item.step for item in during_rpc.steps], ["new"])
        handle.steer_gate.set()
        await steering

        confirmed = self.runtime.turn_progress(binding.id)
        assert confirmed is not None
        self.assertEqual(confirmed.steer_count, 1)
        self.assertFalse(confirmed.plan_may_be_stale)
        self.assertEqual([item.step for item in confirmed.steps], ["new"])
        await self.finish(handle, first)

    async def test_plan_observation_failure_is_display_only(self) -> None:
        observer = FakeTurnPlanObserver()
        observer.error = RuntimeError("observer unavailable")
        self.runtime._turn_plan_observer = observer
        binding = self.binding()
        first = await self.submit(binding, "first")

        with self.assertLogs("netizen.codex_runtime", level="WARNING"):
            progress = self.runtime.turn_progress(binding.id)

        assert progress is not None
        self.assertFalse(progress.plan_available)
        self.assertIsNotNone(self.runtime.active_turn(binding.id))
        await self.finish(self.codex.handles[0], first)
        self.assertEqual(self.outcomes[-1].status, "completed")

    async def test_plan_projection_is_isolated_between_concurrent_bindings(self) -> None:
        observer = FakeTurnPlanObserver()
        self.runtime._turn_plan_observer = observer
        first_binding = self.binding(
            FeishuScope("cli_test", "oc_first", ScopeKind.DIRECT)
        )
        second_binding = self.binding(
            FeishuScope("cli_test", "oc_second", ScopeKind.DIRECT)
        )
        first = await self.submit(first_binding, "first")
        second = await self.submit(second_binding, "second")
        observer.append(
            thread_id=first.thread_id,
            turn_id=first.turn_id,
            steps=(TurnPlanStepSnapshot("alpha", TurnPlanStepState.PENDING),),
        )
        observer.append(
            thread_id=second.thread_id,
            turn_id=second.turn_id,
            steps=(TurnPlanStepSnapshot("beta", TurnPlanStepState.COMPLETED),),
        )

        first_progress = self.runtime.turn_progress(first_binding.id)
        second_progress = self.runtime.turn_progress(second_binding.id)

        assert first_progress is not None and second_progress is not None
        self.assertEqual([item.step for item in first_progress.steps], ["alpha"])
        self.assertEqual([item.step for item in second_progress.steps], ["beta"])
        self.codex.handles[0].complete()
        self.codex.handles[1].complete()
        first.release_receipt_attempt()
        second.release_receipt_attempt()
        await self.runtime.wait_idle()

    async def test_turn_activity_is_exact_opt_in_and_revisioned_by_visible_change(
        self,
    ) -> None:
        observer = FakeTurnPlanObserver()
        self.runtime._turn_plan_observer = observer
        binding = self.binding()
        first = await self.submit(binding, "first")

        initial = self.runtime.turn_activity(
            binding.id,
            thread_id=first.thread_id,
            turn_id=first.turn_id,
        )

        assert initial is not None
        self.assertEqual(initial.revision, 1)
        self.assertEqual(initial.state, ActiveState.RUNNING)
        self.assertEqual(observer.calls, [])
        self.assertIsNone(
            self.runtime.turn_activity(
                binding.id,
                thread_id="another-thread",
                turn_id=first.turn_id,
                refresh_plan=True,
            )
        )
        self.assertIsNone(
            self.runtime.turn_activity(
                binding.id,
                thread_id=first.thread_id,
                turn_id="another-turn",
                refresh_plan=True,
            )
        )
        self.assertEqual(observer.calls, [])

        observer.append(
            thread_id=first.thread_id,
            turn_id=first.turn_id,
            steps=(TurnPlanStepSnapshot("inspect", TurnPlanStepState.IN_PROGRESS),),
        )
        with_plan = self.runtime.turn_activity(
            binding.id,
            thread_id=first.thread_id,
            turn_id=first.turn_id,
            refresh_plan=True,
        )

        assert with_plan is not None
        self.assertEqual(with_plan.revision, 2)
        self.assertEqual([step.step for step in with_plan.steps], ["inspect"])
        unchanged = self.runtime.turn_activity(
            binding.id,
            thread_id=first.thread_id,
            turn_id=first.turn_id,
            refresh_plan=True,
        )
        assert unchanged is not None
        self.assertEqual(unchanged.revision, with_plan.revision)

        steered = await self.submit(binding, "change direction")
        self.assertEqual(steered.task_feedback, BindingTaskFeedback())
        after_steer = self.runtime.turn_activity(
            binding.id,
            thread_id=first.thread_id,
            turn_id=first.turn_id,
        )
        assert after_steer is not None
        self.assertEqual(after_steer.revision, 3)
        self.assertEqual(after_steer.steer_count, 1)
        self.assertTrue(after_steer.plan_may_be_stale)

        self.codex.handles[0].complete_on_interrupt = False
        self.assertEqual(
            await self.runtime.stop(binding.id),
            StopDisposition.REQUESTED,
        )
        stopping = self.runtime.turn_activity(
            binding.id,
            thread_id=first.thread_id,
            turn_id=first.turn_id,
        )
        assert stopping is not None
        self.assertEqual(stopping.revision, 4)
        self.assertEqual(stopping.state, ActiveState.STOPPING)
        await self.finish(self.codex.handles[0], first)

    async def test_disabled_progress_adds_no_terminal_plan_observation(self) -> None:
        observer = FakeTurnPlanObserver()
        self.runtime._turn_plan_observer = observer
        binding = self.binding()
        submission = await self.submit(binding, "quiet progress")
        observer.append(
            thread_id=submission.thread_id,
            turn_id=submission.turn_id,
            steps=(TurnPlanStepSnapshot("hidden", TurnPlanStepState.COMPLETED),),
        )

        await self.finish(self.codex.handles[0], submission)

        self.assertEqual(observer.calls, [])
        outcome = self.outcomes[-1]
        assert isinstance(outcome, TurnOutcome)
        self.assertEqual(outcome.task_feedback, BindingTaskFeedback())
        self.assertEqual(outcome.feedback_revision, 1)
        assert outcome.activity is not None
        self.assertFalse(outcome.activity.plan_generated)

    async def test_progress_feedback_and_final_activity_are_exact_turn_snapshots(
        self,
    ) -> None:
        observer = FakeTurnPlanObserver()
        self.runtime._turn_plan_observer = observer
        feedback = BindingTaskFeedback(progress_card_enabled=True)
        binding = self.store.create_binding(
            scope=self.scope,
            project_alias="test",
            creator_id="ou_user",
            task_feedback=feedback,
        )
        submission = await self.submit(binding, "visible progress")
        observer.append(
            thread_id=submission.thread_id,
            turn_id=submission.turn_id,
            steps=(TurnPlanStepSnapshot("verify", TurnPlanStepState.COMPLETED),),
        )

        self.assertEqual(submission.task_feedback, feedback)
        self.assertEqual(submission.feedback_revision, 1)
        await self.finish(self.codex.handles[0], submission)

        self.assertEqual(
            observer.calls,
            [(submission.thread_id, submission.turn_id, 0)],
        )
        outcome = self.outcomes[-1]
        assert isinstance(outcome, TurnOutcome)
        self.assertEqual(outcome.task_feedback, feedback)
        self.assertEqual(outcome.feedback_revision, 1)
        assert outcome.activity is not None
        self.assertEqual(outcome.activity.binding_id, binding.id)
        self.assertEqual(outcome.activity.thread_id, submission.thread_id)
        self.assertEqual(outcome.activity.turn_id, submission.turn_id)
        self.assertEqual([step.step for step in outcome.activity.steps], ["verify"])

    async def test_feedback_revision_invalidates_prepared_submission(self) -> None:
        binding = self.binding()
        admission = await self.runtime.capture_submission_admission(binding.id)
        self.store.set_configuration(
            binding_id=binding.id,
            expected_settings_revision=binding.settings_revision,
            expected_context_revision=binding.context_revision,
            expected_feedback_revision=binding.feedback_revision,
            settings=binding.turn_settings,
            task_feedback=BindingTaskFeedback(task_reactions_enabled=True),
            message_context_mode=binding.message_context_mode,
            context_anchor=None,
        )

        with self.assertRaisesRegex(SteerRace, "任务反馈配置已变化"):
            await self.submit(binding, "stale feedback", admission=admission)
        self.assertEqual(self.codex.start_kwargs, [])

    async def test_native_multimodal_lists_pass_through_start_and_steer(self) -> None:
        binding = self.binding()
        first_input = [
            TextInput("inspect"),
            ImageInput("data:image/png;base64,AA=="),
        ]
        steer_input = [
            TextInput("compare"),
            ImageInput("data:image/jpeg;base64,AA=="),
        ]

        first = await self.submit(binding, first_input)
        second = await self.submit(binding, steer_input)

        self.assertEqual(self.codex.turn_inputs, [("native-1", first_input)])
        self.assertEqual(second.disposition, SubmitDisposition.STEERED)
        self.assertEqual(self.codex.handles[0].steers, [steer_input])
        await self.finish(self.codex.handles[0], first)

    async def test_unchanged_idle_admission_starts_one_exact_turn(self) -> None:
        binding = self.binding()
        admission = await self.runtime.capture_submission_admission(binding.id)

        submission = await self.submit(binding, "quoted", admission)

        self.assertEqual(submission.disposition, SubmitDisposition.STARTED)
        self.assertEqual(self.codex.turn_inputs, [("native-1", "quoted")])
        await self.finish(self.codex.handles[0], submission)

    async def test_unchanged_running_admission_steers_the_exact_turn(self) -> None:
        binding = self.binding()
        first = await self.submit(binding, "first")
        admission = await self.runtime.capture_submission_admission(binding.id)

        steered = await self.submit(binding, "quoted steer", admission)

        self.assertEqual(steered.disposition, SubmitDisposition.STEERED)
        self.assertEqual(steered.turn_id, first.turn_id)
        self.assertEqual(self.codex.handles[0].steers, ["quoted steer"])
        await self.finish(self.codex.handles[0], first)

    async def test_only_one_same_revision_admission_can_be_redeemed(self) -> None:
        binding = self.binding()
        first_admission = await self.runtime.capture_submission_admission(binding.id)
        second_admission = await self.runtime.capture_submission_admission(binding.id)

        submission = await self.submit(binding, "first quote", first_admission)
        with self.assertRaisesRegex(SteerRace, "状态已变化"):
            await self.submit(binding, "second quote", second_admission)

        self.assertEqual(self.codex.handles[0].steers, [])
        await self.finish(self.codex.handles[0], submission)

    async def test_normal_steer_invalidates_preparation_admission(self) -> None:
        binding = self.binding()
        first = await self.submit(binding, "first")
        admission = await self.runtime.capture_submission_admission(binding.id)

        steered = await self.submit(binding, "new direction")
        with self.assertRaisesRegex(SteerRace, "状态已变化"):
            await self.submit(binding, "stale quote", admission)

        self.assertEqual(steered.disposition, SubmitDisposition.STEERED)
        self.assertEqual(self.codex.handles[0].steers, ["new direction"])
        await self.finish(self.codex.handles[0], first)

    async def test_idle_running_idle_aba_invalidates_admission(self) -> None:
        binding = self.binding()
        stale_idle = await self.runtime.capture_submission_admission(binding.id)
        first = await self.submit(binding, "intervening")
        await self.finish(self.codex.handles[0], first)

        with self.assertRaisesRegex(SteerRace, "状态已变化"):
            await self.submit(
                self.store.get(binding.id),
                "stale quote",
                stale_idle,
            )

        self.assertEqual(len(self.codex.handles), 1)

    async def test_running_completion_invalidates_admission_without_new_turn(
        self,
    ) -> None:
        binding = self.binding()
        first = await self.submit(binding, "first")
        stale_running = await self.runtime.capture_submission_admission(binding.id)
        await self.finish(self.codex.handles[0], first)

        with self.assertRaisesRegex(SteerRace, "状态已变化"):
            await self.submit(
                self.store.get(binding.id),
                "stale quote",
                stale_running,
            )

        self.assertEqual(len(self.codex.handles), 1)

    async def test_admission_for_turn_a_never_steers_later_turn_b(self) -> None:
        binding = self.binding()
        first = await self.submit(binding, "turn A")
        stale_a = await self.runtime.capture_submission_admission(binding.id)
        await self.finish(self.codex.handles[0], first)
        second = await self.submit(self.store.get(binding.id), "turn B")

        with self.assertRaisesRegex(SteerRace, "状态已变化"):
            await self.submit(
                self.store.get(binding.id),
                "stale A quote",
                stale_a,
            )

        self.assertEqual(self.codex.handles[1].steers, [])
        await self.finish(self.codex.handles[1], second)

    async def test_stop_invalidates_admission_and_rejects_prepared_input(self) -> None:
        binding = self.binding()
        submission = await self.submit(binding, "first")
        handle = self.codex.handles[0]
        handle.complete_on_interrupt = False
        stale_running = await self.runtime.capture_submission_admission(binding.id)

        self.assertEqual(
            await self.runtime.stop(binding.id),
            StopDisposition.REQUESTED,
        )
        with self.assertRaises(ThreadStopping):
            await self.submit(binding, "stale quote", stale_running)

        self.assertEqual(handle.steers, [])
        handle.complete("interrupted")
        submission.release_receipt_attempt()
        await self.runtime.wait_idle()

    async def test_idle_stop_invalidates_prepared_start(self) -> None:
        binding = self.binding()
        stale_idle = await self.runtime.capture_submission_admission(binding.id)

        self.assertEqual(
            await self.runtime.stop(binding.id),
            StopDisposition.NOT_RUNNING,
        )
        with self.assertRaisesRegex(SteerRace, "状态已变化"):
            await self.submit(binding, "stale quote", stale_idle)

        self.assertEqual(self.codex.start_kwargs, [])
        self.assertEqual(self.codex.turn_inputs, [])

    async def test_admission_cannot_cross_bindings(self) -> None:
        first_binding = self.binding()
        second_binding = self.binding()
        first_binding = self.store.activate(
            scope_key=self.scope.key,
            binding_id=first_binding.id,
        )
        admission = await self.runtime.capture_submission_admission(first_binding.id)

        with self.assertRaisesRegex(ValueError, "another Binding"):
            await self.submit(second_binding, "wrong Binding", admission)

        self.assertEqual(self.codex.start_kwargs, [])
        self.assertEqual(self.codex.turn_inputs, [])

    async def test_closed_runtime_rejects_captured_admission(self) -> None:
        binding = self.binding()
        admission = await self.runtime.capture_submission_admission(binding.id)
        self.runtime.close_admission()

        with self.assertRaises(RuntimeClosed):
            await self.submit(binding, "too late", admission)

        self.assertEqual(self.codex.start_kwargs, [])

    async def test_completion_race_never_turns_failed_steer_into_new_turn(self) -> None:
        binding = self.binding()
        first = await self.submit(binding, "first")
        self.codex.handles[0].steer_error = InvalidRequestError(-32600, "already done")

        with self.assertRaises(SteerRace):
            await self.submit(binding, "must resend")

        self.assertEqual(len(self.codex.handles), 1)
        await self.finish(self.codex.handles[0], first)

    async def test_stopping_rejects_prompt_and_stop_uses_interrupt(self) -> None:
        binding = self.binding()
        submission = await self.submit(binding)
        handle = self.codex.handles[0]
        handle.complete_on_interrupt = False

        self.assertEqual(
            await self.runtime.stop(binding.id),
            StopDisposition.REQUESTED,
        )
        self.assertEqual(
            self.runtime.active_turn(binding.id).state,
            ActiveState.STOPPING,
        )
        with self.assertRaises(ThreadStopping):
            await self.submit(binding, "too soon")

        handle.complete("interrupted")
        submission.release_receipt_attempt()
        await self.runtime.wait_idle()
        self.assertEqual(handle.interrupt_count, 1)
        self.assertEqual(
            self.codex.events[-2:],
            [("interrupt", "native-1"), ("cleanup", "native-1")],
        )
        self.assertEqual(self.outcomes[0].status, "interrupted")
        self.assertTrue(self.outcomes[0].background_cleanup_requested)

    async def test_cleanup_failure_keeps_stopping_and_repeat_stop_retries(self) -> None:
        binding = self.binding()
        submission = await self.submit(binding)
        self.cleanup.failures.append(RuntimeError("cleanup unavailable"))

        with self.assertRaises(TerminalCleanupFailed):
            await self.runtime.stop(binding.id)

        await asyncio.sleep(0)
        active = self.runtime.active_turn(binding.id)
        self.assertIsNotNone(active)
        self.assertEqual(active.state, ActiveState.STOPPING)
        self.assertFalse(await self.runtime.wait_idle(timeout=0.01))
        with self.assertRaises(ThreadStopping):
            await self.submit(binding, "must not start or steer")
        self.assertEqual(self.codex.handles[0].interrupt_count, 1)
        self.assertEqual(self.cleanup.calls, ["native-1"])

        self.assertEqual(
            await self.runtime.stop(binding.id),
            StopDisposition.REQUESTED,
        )
        submission.release_receipt_attempt()
        await self.runtime.wait_idle()

        self.assertEqual(self.codex.handles[0].interrupt_count, 1)
        self.assertEqual(self.cleanup.calls, ["native-1", "native-1"])
        self.assertIsNone(self.runtime.active_turn(binding.id))
        self.assertTrue(self.outcomes[0].background_cleanup_requested)

    async def test_interrupt_failure_stays_stopping_and_repeat_stop_retries(self) -> None:
        binding = self.binding()
        submission = await self.submit(binding)
        handle = self.codex.handles[0]
        handle.interrupt_errors.append(RuntimeError("response lost"))

        with self.assertRaises(TurnInterruptFailed):
            await self.runtime.stop(binding.id)

        active = self.runtime.active_turn(binding.id)
        self.assertIsNotNone(active)
        self.assertEqual(active.state, ActiveState.STOPPING)
        self.assertEqual(self.cleanup.calls, [])
        with self.assertRaises(ThreadStopping):
            await self.submit(binding, "must not steer")

        self.assertEqual(
            await self.runtime.stop(binding.id),
            StopDisposition.REQUESTED,
        )
        submission.release_receipt_attempt()
        await self.runtime.wait_idle()

        self.assertEqual(handle.interrupt_count, 2)
        self.assertEqual(self.cleanup.calls, ["native-1"])
        self.assertTrue(self.outcomes[0].background_cleanup_requested)

    async def test_interrupt_unknown_retries_even_after_terminal_is_observed(
        self,
    ) -> None:
        binding = self.binding()
        submission = await self.submit(binding)
        handle = self.codex.handles[0]
        handle.interrupt_errors.extend(
            [RuntimeError("response lost"), RuntimeError("already terminal")]
        )

        with self.assertRaises(TurnInterruptFailed):
            await self.runtime.stop(binding.id)

        handle.complete("interrupted")

        async def wait_for_terminal() -> None:
            while not self.runtime._active[binding.id].terminal_observed:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_terminal(), timeout=0.1)
        with self.assertLogs("netizen.codex_runtime", level="WARNING") as logs:
            self.assertEqual(
                await self.runtime.stop(binding.id),
                StopDisposition.REQUESTED,
            )
        submission.release_receipt_attempt()
        await self.runtime.wait_idle()

        self.assertEqual(handle.interrupt_count, 2)
        self.assertEqual(self.cleanup.calls, ["native-1"])
        self.assertTrue(self.outcomes[0].background_cleanup_requested)
        self.assertTrue(
            any("repeat interrupt failed" in entry for entry in logs.output)
        )

    async def test_acknowledgement_failure_does_not_cancel_native_stop(self) -> None:
        binding = self.binding()
        submission = await self.submit(binding)

        async def fail_acknowledgement() -> None:
            raise RuntimeError("Feishu unavailable")

        with self.assertLogs("netizen.codex_runtime", level="WARNING") as logs:
            result = await self.runtime.stop(
                binding.id,
                acknowledge=fail_acknowledgement,
            )

        self.assertEqual(result, StopDisposition.REQUESTED)
        self.assertTrue(
            any("acknowledgement failed" in message for message in logs.output)
        )
        self.assertEqual(self.codex.handles[0].interrupt_count, 1)
        self.assertEqual(self.cleanup.calls, ["native-1"])
        submission.release_receipt_attempt()
        await self.runtime.wait_idle()

    async def test_acknowledgement_timeout_does_not_cancel_native_stop(self) -> None:
        binding = self.binding()
        submission = await self.submit(binding)
        acknowledgement_entered = asyncio.Event()

        async def blocked_acknowledgement() -> None:
            acknowledgement_entered.set()
            await asyncio.Event().wait()

        with patch(
            "netizen.codex_runtime._STOP_ACK_ATTEMPT_TIMEOUT_SECONDS",
            0.001,
        ):
            with self.assertLogs("netizen.codex_runtime", level="WARNING") as logs:
                result = await self.runtime.stop(
                    binding.id,
                    acknowledge=blocked_acknowledgement,
                )

        self.assertTrue(acknowledgement_entered.is_set())
        self.assertEqual(result, StopDisposition.REQUESTED)
        self.assertTrue(
            any("acknowledgement timed out" in message for message in logs.output)
        )
        self.assertEqual(self.codex.handles[0].interrupt_count, 1)
        self.assertEqual(self.cleanup.calls, ["native-1"])
        submission.release_receipt_attempt()
        await self.runtime.wait_idle()

    async def test_shutdown_interrupts_and_cleans_every_active_thread(self) -> None:
        first_binding = self.binding()
        second_scope = FeishuScope("cli_test", "oc_other", ScopeKind.DIRECT)
        second_binding = self.binding(second_scope)
        await self.submit(first_binding, "one")
        await self.submit(second_binding, "two")

        await self.runtime.interrupt_all()
        await self.runtime.wait_idle()

        self.assertEqual(
            self.codex.events,
            [
                ("interrupt", "native-1"),
                ("cleanup", "native-1"),
                ("interrupt", "native-2"),
                ("cleanup", "native-2"),
            ],
        )
        self.assertIsNone(self.runtime.active_turn(first_binding.id))
        self.assertIsNone(self.runtime.active_turn(second_binding.id))

    async def test_shutdown_cleanup_failure_is_logged_and_cancellable(self) -> None:
        binding = self.binding()
        await self.submit(binding)
        self.cleanup.failures.append(RuntimeError("cleanup unavailable"))

        with self.assertLogs("netizen.codex_runtime", level="ERROR") as logs:
            with self.assertRaisesRegex(
                ExceptionGroup,
                "native Turn cleanups failed",
            ):
                await self.runtime.interrupt_all()

        self.assertTrue(
            any("shutdown cleanup failed" in message for message in logs.output)
        )
        active = self.runtime.active_turn(binding.id)
        self.assertIsNotNone(active)
        self.assertEqual(active.state, ActiveState.STOPPING)
        self.assertFalse(await self.runtime.wait_idle(timeout=0.01))

        await self.runtime.cancel_tasks()

        self.assertTrue(await self.runtime.wait_idle(timeout=0.01))
        self.assertIsNone(self.runtime.active_turn(binding.id))

    async def test_cancel_tasks_breaks_cleanup_barrier_during_native_read(self) -> None:
        self.codex.read_gate = asyncio.Event()
        binding = self.binding()
        await self.submit(binding)
        while not self.codex.read_calls:
            await asyncio.sleep(0)
        self.cleanup.failures.append(RuntimeError("cleanup unavailable"))

        with self.assertRaises(TerminalCleanupFailed):
            await self.runtime.stop(binding.id)

        await asyncio.wait_for(self.runtime.cancel_tasks(), timeout=0.1)
        self.assertTrue(await self.runtime.wait_idle(timeout=0.01))
        self.assertIsNone(self.runtime.active_turn(binding.id))

    async def test_cancel_tasks_does_not_wait_for_stop_acknowledgement_lock(
        self,
    ) -> None:
        binding = self.binding()
        await self.submit(binding)
        acknowledgement_entered = asyncio.Event()
        release_acknowledgement = asyncio.Event()

        async def blocked_acknowledgement() -> None:
            acknowledgement_entered.set()
            await release_acknowledgement.wait()

        stop_task = asyncio.create_task(
            self.runtime.stop(
                binding.id,
                acknowledge=blocked_acknowledgement,
            )
        )
        await acknowledgement_entered.wait()

        await asyncio.wait_for(self.runtime.cancel_tasks(), timeout=0.1)
        self.assertIsNone(self.runtime.active_turn(binding.id))

        release_acknowledgement.set()
        self.assertEqual(await stop_task, StopDisposition.REQUESTED)
        self.assertEqual(self.codex.handles[0].interrupt_count, 1)
        self.assertEqual(self.cleanup.calls, ["native-1"])

    async def test_prompt_during_stop_acknowledgement_fails_fast(self) -> None:
        binding = self.binding()
        submission = await self.submit(binding)
        acknowledgement_entered = asyncio.Event()
        release_acknowledgement = asyncio.Event()

        async def blocked_acknowledgement() -> None:
            acknowledgement_entered.set()
            await release_acknowledgement.wait()

        stop_task = asyncio.create_task(
            self.runtime.stop(binding.id, acknowledge=blocked_acknowledgement)
        )
        await acknowledgement_entered.wait()

        with self.assertRaises(ThreadStopping):
            await asyncio.wait_for(
                self.submit(binding, "must reject immediately"),
                timeout=0.05,
            )

        release_acknowledgement.set()
        self.assertEqual(await stop_task, StopDisposition.REQUESTED)
        submission.release_receipt_attempt()
        await self.runtime.wait_idle()

    async def test_terminal_observation_wins_stop_race_without_cleanup(self) -> None:
        binding = self.binding()
        submission = await self.submit(binding)
        active = self.runtime._active[binding.id]
        active.terminal_observed = True

        self.assertEqual(
            await self.runtime.stop(binding.id),
            StopDisposition.NOT_RUNNING,
        )
        self.assertEqual(self.codex.handles[0].interrupt_count, 0)
        self.assertEqual(self.cleanup.calls, [])

        self.codex.handles[0].complete()
        submission.release_receipt_attempt()
        await self.runtime.wait_idle()

    async def test_stop_acknowledgement_precedes_racing_natural_completion(
        self,
    ) -> None:
        binding = self.binding()
        submission = await self.submit(binding)
        handle = self.codex.handles[0]
        acknowledgement_entered = asyncio.Event()
        release_acknowledgement = asyncio.Event()

        async def acknowledge() -> None:
            acknowledgement_entered.set()
            await release_acknowledgement.wait()

        stop_task = asyncio.create_task(
            self.runtime.stop(binding.id, acknowledge=acknowledge)
        )
        await acknowledgement_entered.wait()
        handle.complete()
        submission.release_receipt_attempt()

        async def wait_until_terminal_is_observed() -> None:
            while not self.runtime._active[binding.id].terminal_observed:
                await asyncio.sleep(0)

        try:
            await asyncio.wait_for(wait_until_terminal_is_observed(), timeout=0.1)
        except TimeoutError:
            active = self.runtime._active[binding.id]
            self.fail(
                "native completion was not observed while acknowledgement was "
                f"blocked; reads={self.codex.read_calls!r}, "
                f"consumer_done={active.task.done() if active.task else None}"
            )
        self.assertEqual(self.outcomes, [])
        self.assertFalse(stop_task.done())

        release_acknowledgement.set()
        stop_result = await stop_task
        self.assertEqual(stop_result, StopDisposition.NOT_RUNNING)
        await self.runtime.wait_idle()

        self.assertEqual(handle.interrupt_count, 0)
        self.assertEqual(self.cleanup.calls, [])
        self.assertEqual(len(self.outcomes), 1)
        self.assertEqual(self.outcomes[0].status, "completed")

    async def test_terminal_releases_binding_before_receipt_or_final_delivery(self) -> None:
        delivery_entered = asyncio.Event()
        release_delivery = asyncio.Event()

        async def blocked_delivery(outcome: TurnOutcome) -> None:
            self.outcomes.append(outcome)
            delivery_entered.set()
            await release_delivery.wait()

        self.runtime.set_completion_handler(blocked_delivery)
        binding = self.binding()
        submission = await self.submit(binding)
        self.codex.handles[0].complete()
        await asyncio.sleep(0)

        self.assertIsNone(self.runtime.active_turn(binding.id))
        self.assertFalse(delivery_entered.is_set())
        submission.release_receipt_attempt()
        await delivery_entered.wait()
        self.assertIsNone(self.runtime.active_turn(binding.id))
        release_delivery.set()
        await self.runtime.wait_idle()

    async def test_immediate_completion_is_held_behind_receipt_barrier(self) -> None:
        self.codex.complete_immediately = True
        binding = self.binding()

        submission = await self.submit(binding)
        await asyncio.sleep(0)

        self.assertEqual(self.outcomes, [])
        self.assertIsNone(self.runtime.active_turn(binding.id))
        submission.release_receipt_attempt()
        await self.runtime.wait_idle()
        self.assertEqual(len(self.outcomes), 1)
        self.assertEqual(self.codex.handles[0].stream_calls, 0)
        self.assertIsNone(self.runtime.context_window_usage(binding.id))

    async def test_transient_empty_rollout_read_is_retried(self) -> None:
        self.codex.complete_immediately = True
        self.codex.read_errors.append(InternalRpcError(-32603, "rollout is empty"))
        binding = self.binding()

        with self.assertLogs("netizen.codex_runtime", level="WARNING") as logs:
            submission = await self.submit(binding)
            submission.release_receipt_attempt()
            await self.runtime.wait_idle()

        self.assertGreaterEqual(len(self.codex.read_calls), 2)
        self.assertIn("native Thread read unavailable", logs.output[0])
        self.assertIsNone(self.outcomes[0].error)
        self.assertEqual(self.outcomes[0].final_response, "done:turn-1")

    async def test_completed_turn_waits_for_final_response_materialization(self) -> None:
        self.codex.complete_immediately = True
        self.codex.omit_agent_items_full_reads = 1
        binding = self.binding()

        with self.assertLogs("netizen.codex_runtime", level="WARNING") as logs:
            submission = await self.submit(binding)
            submission.release_receipt_attempt()
            await self.runtime.wait_idle()

        self.assertEqual(
            self.codex.read_calls.count((submission.thread_id, True)),
            2,
        )
        self.assertIn("response not materialized", logs.output[0])
        self.assertIsNone(self.outcomes[0].error)
        self.assertEqual(self.outcomes[0].final_response, "done:turn-1")

    async def test_completed_turn_without_text_keeps_bounded_fallback(self) -> None:
        binding = self.binding()
        submission = await self.submit(binding)
        handle = self.codex.handles[0]
        handle.record.status.value = "completed"
        handle.record.completed_at = 2
        handle.notifications.put_nowait(_STREAM_END)
        submission.release_receipt_attempt()
        with self.assertLogs("netizen.codex_runtime", level="WARNING"):
            await self.runtime.wait_idle()

        self.assertEqual(
            self.codex.read_calls.count((submission.thread_id, True)),
            5,
        )
        self.assertIsNone(self.outcomes[0].error)
        self.assertIsNone(self.outcomes[0].final_response)

    async def test_not_loaded_thread_waits_before_requesting_turns(self) -> None:
        self.codex.complete_immediately = True
        self.codex.read_statuses.append("notLoaded")
        binding = self.binding()

        submission = await self.submit(binding)
        submission.release_receipt_attempt()
        await self.runtime.wait_idle()

        self.assertEqual(
            self.codex.read_calls[:3],
            [
                (submission.thread_id, False),
                (submission.thread_id, False),
                (submission.thread_id, True),
            ],
        )
        self.assertIsNone(self.outcomes[0].error)
        self.assertEqual(self.outcomes[0].turn_id, submission.turn_id)

    async def test_exact_materialization_error_keeps_active_and_retries(self) -> None:
        self.codex.complete_immediately = True
        self.codex.full_read_errors.append(
            InvalidRequestError(
                -32600,
                "thread native-1 is not materialized yet; "
                "includeTurns is unavailable before first user message",
            )
        )
        binding = self.binding()

        with self.assertLogs("netizen.codex_runtime", level="WARNING"):
            submission = await self.submit(binding)
            submission.release_receipt_attempt()
            await self.runtime.wait_idle()

        self.assertEqual(
            self.codex.read_calls.count((submission.thread_id, True)),
            2,
        )
        self.assertEqual(len(self.codex.handles), 1)
        self.assertIsNone(self.outcomes[0].error)
        self.assertEqual(self.outcomes[0].turn_id, submission.turn_id)

    async def test_other_invalid_request_is_not_retried(self) -> None:
        self.codex.complete_immediately = True
        self.codex.full_read_errors.append(
            InvalidRequestError(-32600, "different permanent error")
        )
        binding = self.binding()

        with self.assertLogs("netizen.codex_runtime", level="ERROR") as logs:
            submission = await self.submit(binding)
            submission.release_receipt_attempt()
            await self.runtime.wait_idle()

        self.assertIsInstance(self.outcomes[0].error, TerminalStateUnknown)
        self.assertTrue(
            any("terminal state is unknown" in entry for entry in logs.output)
        )
        self.assertEqual(
            self.codex.read_calls.count((submission.thread_id, True)),
            1,
        )
        self.assertIsNotNone(self.runtime.active_turn(binding.id))
        with self.assertRaisesRegex(RuntimeError, "服务正在停止"):
            await self.submit(self.store.get(binding.id), "must not start")
        self.assertEqual(len(self.codex.handles), 1)
        await self.runtime.cancel_tasks()
        self.assertIsNone(self.runtime.active_turn(binding.id))

    async def test_retryable_overload_keeps_active_slot_until_exact_terminal(self) -> None:
        self.codex.complete_immediately = True
        self.codex.read_errors.append(ServerBusyError(-32001, "server overloaded"))
        self.codex.read_gate = asyncio.Event()
        binding = self.binding()

        with self.assertLogs("netizen.codex_runtime", level="WARNING"):
            submission = await self.submit(binding)
            try:
                while len(self.codex.read_calls) < 2:
                    await asyncio.sleep(0)
                self.assertIsNotNone(self.runtime.active_turn(binding.id))
                self.assertEqual(len(self.codex.handles), 1)
            finally:
                self.codex.read_gate.set()
            submission.release_receipt_attempt()
            await self.runtime.wait_idle()

        self.assertIsNone(self.outcomes[0].error)
        self.assertEqual(self.outcomes[0].turn_id, submission.turn_id)

    async def test_failed_native_turn_is_delivered_as_an_error(self) -> None:
        binding = self.binding()
        submission = await self.submit(binding)

        self.codex.handles[0].fail("native failure")
        submission.release_receipt_attempt()
        await self.runtime.wait_idle()

        self.assertIsInstance(self.outcomes[0].error, RuntimeError)
        self.assertIn("native failure", str(self.outcomes[0].error))

    async def test_final_answer_wins_over_commentary_items(self) -> None:
        binding = self.binding()
        submission = await self.submit(binding)
        handle = self.codex.handles[0]

        handle.complete(response="FINAL")
        handle.record.items.append(
            SimpleNamespace(
                root=SimpleNamespace(
                    type="agentMessage",
                    text="later commentary",
                    phase=FakeStatus("commentary"),
                )
            )
        )
        submission.release_receipt_attempt()
        await self.runtime.wait_idle()

        self.assertEqual(self.outcomes[0].final_response, "FINAL")

    async def test_distinct_bindings_share_cwd_and_run_without_global_limit(self) -> None:
        first_binding = self.binding()
        second_scope = FeishuScope("cli_test", "oc_other", ScopeKind.DIRECT)
        second_binding = self.binding(second_scope)

        first, second = await asyncio.gather(
            self.submit(first_binding, "one"),
            self.submit(second_binding, "two"),
        )

        self.assertNotEqual(first.thread_id, second.thread_id)
        self.assertEqual(
            self.codex.start_kwargs,
            [{"cwd": str(self.cwd)}, {"cwd": str(self.cwd)}],
        )
        self.assertIsNotNone(self.runtime.active_turn(first_binding.id))
        self.assertIsNotNone(self.runtime.active_turn(second_binding.id))
        self.codex.handles[0].complete()
        self.codex.handles[1].complete()
        first.release_receipt_attempt()
        second.release_receipt_attempt()
        await self.runtime.wait_idle()

    async def test_stop_is_scoped_to_selected_binding(self) -> None:
        first_binding = self.binding()
        second_scope = FeishuScope("cli_test", "oc_other", ScopeKind.DIRECT)
        second_binding = self.binding(second_scope)
        first = await self.submit(first_binding, "one")
        second = await self.submit(second_binding, "two")

        await self.runtime.stop(first_binding.id)

        self.assertEqual(self.codex.handles[0].interrupt_count, 1)
        self.assertEqual(self.codex.handles[1].interrupt_count, 0)
        self.assertEqual(self.cleanup.calls, [first.thread_id])
        first.release_receipt_attempt()
        await self.finish(self.codex.handles[1], second)
