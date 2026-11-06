"""Native Goal physical Turns and logical terminal handling."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from openai_codex import (
    InvalidRequestError,
)

from ..bindings import (
    ThreadBinding,
)
from ..domain import (
    GoalOperationState,
    GoalStatus,
)
from .contracts import (
    NativeThread,
    RuntimeClosed,
    ThreadOccupied,
    ThreadRunningConfiguration,
    ThreadCompacting,
    SteerRace,
    TerminalCleanupFailed,
    ThreadGoalActive,
    ExternalGoalActive,
    GoalNotFound,
    GoalNotMaterialized,
    GoalStateUnknown,
    SubmitDisposition,
    GoalFinalizationStatus,
    StopDisposition,
    Submission,
    GoalSubmission,
    ContextCursorCommit,
    GoalOutcome,
    StopAcknowledger,
)
from ..sdk_gap_adapter import (
    GoalControlError,
    GoalMutationStateUnknown,
    GoalMutationRejected,
    GoalSnapshot,
)
from ..turn_patch_children import TaskPatchChildren

from . import limits
from .native_values import (
    _enum_value,
    _thread_creation_cwd,
    _native_cwd,
    _thread_status_type,
    _final_agent_response,
)
from .state import (
    RUNTIME_LOGGER_NAME,
    _ActiveGoal,
    _same_goal_generation,
)
from .observation import TurnObservationRuntime


logger = logging.getLogger(RUNTIME_LOGGER_NAME)


class GoalRuntime(TurnObservationRuntime):
    """Native Goal physical Turns and logical terminal handling."""

    async def goal_snapshot(self, binding: ThreadBinding) -> GoalSnapshot | None:
        control = self._goal_control
        if control is None:
            return None
        async with self._lock(binding.id):
            self._guard_no_lifecycle_locked(binding.id)
            binding = self._bindings.get(binding.id)
            if binding.native_thread_id is None:
                return None
            await self._recheck_goal_observation_locked(binding)
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
                            and not active.goal_cleared
                        )
                        or (
                            snapshot is not None
                            and (
                                active.goal_cleared
                                or not _same_goal_generation(active, snapshot)
                            )
                        )
                    )
                ):
                    self._mark_goal_observation_unavailable(active)
                    raise GoalStateUnknown(
                        "当前 Codex Goal 已变化，本次观察无法继续；"
                        "稍后发送消息或查看 /goal 时会重新检查该会话是否空闲。"
                    )
                if active.terminal_observed and snapshot is None and not active.goal_cleared:
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
        cwd: Path | None = None,
        objective: str,
        owner_id: str,
        origin: object,
    ) -> GoalSubmission:
        """Start a Goal; cwd is only a lazy Thread's creation-directory intent."""
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
            self._bindings.worktrees.require_binding_not_deleting(binding.id)
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
            creation_cwd = (
                _thread_creation_cwd(binding, cwd) if binding.native_thread_id is None else None
            )
            thread = await self._open_goal_thread(binding=binding, creation_cwd=creation_cwd)
            execution_cwd = self._adopt_native_cwd(
                binding_id=binding.id, thread_id=thread.id,
                native_cwd=await self._validate_goal_thread_ready(thread),
            )
            persisted = await control.get(thread.id)
            if persisted is not None and persisted.status is GoalStatus.ACTIVE:
                self._track_external_goal(binding, persisted, thread=thread)
                raise ExternalGoalActive(
                    "当前 Codex 会话已有外部 active Goal；不能启动第二个 Goal。"
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
                cwd=execution_cwd,
                persisted=persisted,
                task_feedback=self._capture_task_feedback(binding.task_feedback),
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
            raise GoalStateUnknown("Goal handle 与 Codex 会话不一致。")
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
            self._bindings.worktrees.require_binding_not_deleting(binding.id)
            if binding.id in self._compacting or self._active.get(binding.id) is not None:
                raise ThreadRunningConfiguration(
                    "当前会话有其他 Codex 操作，不能恢复 Goal。"
                )
            existing = self._goals.get(binding.id)
            if existing is not None:
                if (
                    existing.state is not GoalOperationState.RUNNING
                    or existing.terminal_observed
                    or existing.goal_cleared
                    or existing.handle is None
                    or existing.handle.is_finished()
                    or existing.handle.current_physical_turn_id() is None
                ):
                    raise self._goal_slot_error(existing)
                persisted = await control.get(existing.thread_id)
                if persisted is None or not _same_goal_generation(existing, persisted):
                    raise GoalNotFound("Goal 已变化，本次恢复未执行；请重新发送 /goal。")
                if expected_created_at is not None and persisted.created_at != expected_created_at:
                    raise GoalNotFound("Goal 已变化，本次恢复未执行；请重新发送 /goal。")
                if persisted.status is not GoalStatus.PAUSED:
                    raise ThreadGoalActive("只有 paused Goal 可恢复。")
                try:
                    resumed = await control.activate(existing.thread_id)
                    if (
                        resumed.status is not GoalStatus.ACTIVE
                        or not _same_goal_generation(existing, resumed)
                    ):
                        raise RuntimeError("Goal generation changed while resuming")
                    if existing.handle.is_finished():
                        raise RuntimeError("Goal observer ended while resuming")
                except GoalMutationRejected as error:
                    raise GoalNotFound(str(error)) from error
                except BaseException as error:
                    existing.state = GoalOperationState.UNKNOWN
                    existing.observation_unavailable = False
                    self.close_admission()
                    if isinstance(error, asyncio.CancelledError):
                        raise
                    raise GoalStateUnknown("Goal 恢复结果或观察归属未确认；不能自动重试。") from error
                existing.persisted = resumed
                self._advance_admission_revision(binding.id)
                assert existing.handle.id is not None
                return GoalSubmission(
                    binding_id=binding.id,
                    thread_id=existing.thread_id,
                    logical_turn_id=existing.handle.id,
                    release_receipt_attempt=existing.receipt_attempted.set,
                    task_feedback=existing.task_feedback,
                    feedback_revision=existing.feedback_revision,
                    continued=True,
                )
            binding = self._bindings.get(binding.id)
            if binding.native_thread_id is None:
                raise GoalNotMaterialized("当前会话尚未在 Codex 中创建。")
            thread = await self._open_goal_thread(binding=binding, creation_cwd=None)
            cwd = await self._validate_goal_thread_ready(thread)
            self._bindings.sync_native_workspace(
                binding.id, expected_native_thread_id=thread.id, working_cwd=str(cwd),
            )
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
                cwd=cwd,
                persisted=persisted,
                generation_created_at=persisted.created_at,
                generation_token_budget=persisted.token_budget,
                task_feedback=self._capture_task_feedback(binding.task_feedback),
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
            raise GoalStateUnknown("恢复后的 Goal handle 与 Codex 会话不一致。")
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

    async def pause_goal(
        self,
        binding: ThreadBinding,
        *,
        expected_created_at: int | None = None,
    ) -> GoalSnapshot:
        """Pause continuation while the current Turn and its observer continue."""
        control = self._require_goal_control()
        async with self._lock(binding.id):
            self._guard_no_lifecycle_locked(binding.id)
            binding = self._bindings.get(binding.id)
            if binding.native_thread_id is None:
                raise GoalNotFound("当前会话没有 Codex Goal。")
            active = self._goals.get(binding.id)
            if active is not None and (
                active.state is not GoalOperationState.RUNNING
                or active.terminal_observed or active.goal_cleared
            ):
                raise self._goal_slot_error(active)
            persisted = await control.get(binding.native_thread_id)
            if persisted is None or (
                expected_created_at is not None
                and persisted.created_at != expected_created_at
            ):
                raise GoalNotFound("Goal 已变化，本次暂停未执行；请重新发送 /goal。")
            if active is not None and not _same_goal_generation(active, persisted):
                raise GoalNotFound("Goal 已变化，本次暂停未执行；请重新发送 /goal。")
            if persisted.status is GoalStatus.PAUSED:
                return persisted
            if active is None:
                if persisted.status is GoalStatus.ACTIVE:
                    self._track_external_goal(binding, persisted)
                    raise self._goal_slot_error(self._goals[binding.id])
                raise ThreadGoalActive("当前 Goal 未在执行，无需暂停。")
            try:
                paused = await control.pause(active.thread_id)
                if (
                    paused.status is not GoalStatus.PAUSED
                    or not _same_goal_generation(active, paused)
                ):
                    raise RuntimeError("Goal generation changed while pausing")
            except GoalMutationRejected as error:
                raise GoalNotFound(str(error)) from error
            except BaseException as error:
                active.state = GoalOperationState.UNKNOWN
                active.observation_unavailable = False
                self.close_admission()
                if isinstance(error, asyncio.CancelledError):
                    raise
                raise GoalStateUnknown("Goal 暂停结果未确认；不能自动重试。") from error
            active.persisted = paused
            self._advance_admission_revision(binding.id)
            return paused

    async def clear_goal(
        self,
        binding: ThreadBinding,
        *,
        expected_created_at: int | None = None,
    ) -> bool:
        control = self._require_goal_control()
        async with self._lock(binding.id):
            self._guard_no_lifecycle_locked(binding.id)
            if binding.id in self._compacting:
                raise ThreadRunningConfiguration(
                    "当前会话有其他 Codex 操作，不能清除 Goal。"
                )
            binding = self._bindings.get(binding.id)
            if binding.native_thread_id is None:
                return False
            active = self._goals.get(binding.id)
            if (
                active is not None
                and (
                    active.state not in {
                        GoalOperationState.RUNNING, GoalOperationState.EXTERNAL_ACTIVE,
                    }
                    or active.terminal_observed
                )
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
            if active is not None and active.handle is not None:
                if not _same_goal_generation(active, persisted):
                    raise GoalNotFound("Goal 已变化，本次结束未执行；请重新发送 /goal。")
            elif persisted.status is GoalStatus.ACTIVE:
                if binding.id not in self._goals:
                    self._track_external_goal(binding, persisted)
                raise self._goal_slot_error(self._goals[binding.id])
            if active is not None and active.handle is not None:
                sentinel = active
                thread = active.thread
                assert thread is not None
            else:
                ordinary = self._active.get(binding.id)
                if ordinary is not None:
                    thread = ordinary.thread
                else:
                    thread = await self._open_goal_thread(binding=binding, creation_cwd=None)
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
                    task_feedback=self._capture_task_feedback(binding.task_feedback),
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
            if sentinel.handle is not None:
                # Keep the same observer until the running physical Turn ends.
                # Only this acknowledged mutation permits absent persisted Goal.
                sentinel.goal_cleared = True
                sentinel.persisted = None
            else:
                self._goals.pop(binding.id, None)
            self._advance_admission_revision(binding.id)
            self._schedule_known_subscription_locked(binding.id, thread.id)
            return True

    async def _open_goal_thread(
        self,
        *,
        binding: ThreadBinding,
        creation_cwd: Path | None,
    ) -> NativeThread:
        try:
            if binding.native_thread_id is None:
                if creation_cwd is None:
                    raise GoalNotMaterialized(
                        "当前会话尚未在 Codex 中创建。"
                    )
                thread = await self._codex.thread_start(cwd=str(creation_cwd))
                self._bindings.assign_native_thread_id(binding.id, thread.id)
            else:
                thread = await self._resume_thread_for_operation(
                    binding.native_thread_id
                )
                if thread.id != binding.native_thread_id:
                    raise RuntimeError("thread_resume returned a different native ID")
            current = self._bindings.get(binding.id)
            record = self._mark_thread_subscribed_locked(current, thread)
            self._schedule_subscription_release_locked(record)
            return thread
        except (GoalNotMaterialized, ThreadOccupied):
            raise
        except asyncio.CancelledError:
            self.close_admission()
            raise
        except Exception as error:
            self.close_admission()
            raise GoalStateUnknown(
                "Codex Thread 启动或恢复结果未确认；服务已关闭 admission。"
            ) from error

    async def _validate_goal_thread_ready(self, thread: NativeThread) -> Path:
        try:
            response = await thread.read(include_turns=False)
        except InvalidRequestError as error:
            if error.code == -32600 and limits.NOT_MATERIALIZED_SUFFIX in error.message:
                raise GoalNotMaterialized(
                    "当前 SDK 尚不能在零 Turn 的新 Thread 上启动 Goal；"
                    "该能力未通过 live persistence 门禁。"
                ) from error
            raise GoalControlError("无法读取 Goal 对应的 Codex 会话。") from error
        except Exception as error:
            raise GoalControlError("无法读取 Goal 对应的 Codex 会话。") from error
        native = getattr(response, "thread", None)
        status = _thread_status_type(native)
        if status != "idle":
            raise ThreadRunningConfiguration(
                f"Codex 会话状态为 {status!r}，不能修改 Goal。"
            )
        if getattr(native, "ephemeral", None) is not False:
            raise GoalNotMaterialized("Goal 只允许使用已持久化的 Codex 会话。")
        path = getattr(native, "path", None)
        if not isinstance(path, str) or not path:
            raise GoalNotMaterialized(
                "Codex 会话尚未持久化 path，当前不能启动或恢复 Goal。"
            )
        return _native_cwd(native, thread.id)

    async def _prompt_goal_locked(self, binding: ThreadBinding) -> _ActiveGoal | None:
        await self._recheck_goal_observation_locked(binding)
        active = self._goals.get(binding.id)
        if active is None:
            await self._guard_no_goal_locked(binding)
            return None
        return active

    def _goal_steer_turn_id(self, active: _ActiveGoal) -> str:
        if active.state is not GoalOperationState.RUNNING:
            raise self._goal_slot_error(active)
        if active.stream_terminal is not None or active.terminal_observed:
            raise ThreadGoalActive("当前 Goal 正在收尾，本条消息未执行，请稍后重新发送。")
        handle = active.handle
        if handle is None or handle.id is None:
            raise GoalStateUnknown("当前 Goal 缺少安全 route，本条消息未执行。")
        turn_id = handle.current_physical_turn_id()
        if turn_id is None:
            raise SteerRace("Goal 当前物理 Turn 已结束或正在换轮，本条消息未执行，请重新发送。")
        return turn_id

    async def _steer_goal_locked(
        self,
        *,
        binding: ThreadBinding,
        active: _ActiveGoal,
        expected_turn_id: str,
        input: Any,
        context_commit: ContextCursorCommit | None,
    ) -> Submission:
        handle = active.handle
        assert handle is not None
        self._notify_input_received(active.thread_id, handle.id, expected_turn_id)
        try:
            await self._steer_turn(
                handle.steer(input, expected_turn_id=expected_turn_id),
                race_message="Goal 当前物理 Turn 已变化或不再接受输入，本条消息未执行，请重新发送。",
            )
        except asyncio.CancelledError:
            # As with ordinary steer, cancelling the waiter cannot cancel the
            # SDK's worker-thread mutation or prove non-delivery.
            active.state = GoalOperationState.UNKNOWN
            raise
        except GoalMutationStateUnknown as error:
            active.state = GoalOperationState.UNKNOWN
            self.close_admission()
            raise GoalStateUnknown(
                "Goal 追加消息结果未确认；服务已停止接收新任务，不能自动重试。"
            ) from error
        self._commit_context_cursor_locked(binding=binding, commit=context_commit)
        return Submission(
            disposition=SubmitDisposition.STEERED,
            binding_id=binding.id,
            thread_id=active.thread_id,
            turn_id=expected_turn_id,
            task_feedback=active.task_feedback,
            feedback_revision=active.feedback_revision,
        )

    async def _stop_goal_locked(
        self,
        active: _ActiveGoal,
        *,
        acknowledge: StopAcknowledger | None,
    ) -> StopDisposition:
        if active.state is GoalOperationState.UNKNOWN:
            raise GoalStateUnknown(
                "Goal 状态未确认；/stop 不会再次发起 Codex 状态修改。"
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
                async with asyncio.timeout(limits.STOP_ACK_ATTEMPT_TIMEOUT_SECONDS):
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
            if active.goal_cleared:
                # Clearing the Goal does not stop its still-running physical Turn.
                # /stop must not attempt to pause a Goal that no longer exists.
                await handle.interrupt()
                active.interrupt_acknowledged = True
            else:
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
            active.stream_terminal = await handle.wait_terminal(
                lambda projection: self._apply_goal_activity_projection(active, projection),
                progress_card_settings=active.task_feedback.progress_card_settings,
            )
            if active.stream_terminal.logical_turn_id != handle.id:
                raise RuntimeError("Goal stream terminal identity mismatch")
            async with asyncio.timeout(limits.COMPACTION_TERMINAL_TIMEOUT_SECONDS):
                persisted = await self._read_goal_terminal(active)
            async with self._lock(active.binding_id):
                if active.goal_cleared:
                    persisted = None
                    finalization = GoalFinalizationStatus.CLEARED
                active.persisted = persisted
                active.terminal_observed = True
                # The stream can finish before an in-flight steer RPC returns.
                # Wait for its receipt before deciding whether this slot may
                # be cleared/released; native terminal does not prove delivery.
                if active.state is GoalOperationState.UNKNOWN:
                    retain_active = True
                    error = (
                        self._goal_slot_error(active)
                        if active.observation_unavailable else GoalStateUnknown(
                            "Goal 消息或操作结果未确认；最终结果仍会展示，"
                            "但会话保持占用，请重启后对账。"
                        )
                    )
            while active.cleanup_required and not active.cleanup_succeeded:
                await active.cleanup_ready.wait()
                if not active.cleanup_succeeded:
                    active.cleanup_ready.clear()
            if (
                not retain_active
                and persisted is not None
                and persisted.status is GoalStatus.COMPLETE
                and active.final_turn_status == "completed"
            ):
                clear_attempted = False
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
                            clear_attempted = True
                            await self._clear_completed_goal(active)
                            finalization = GoalFinalizationStatus.CLEARED
                except BaseException as caught:
                    finalization = GoalFinalizationStatus.UNKNOWN
                    finalization_error = GoalStateUnknown(
                        "已完成 Goal 的自动清理结果未确认；最终结果仍会展示，"
                        "但会话保持占用且服务已关闭 admission。"
                        if clear_attempted else
                        "Goal 自动清理前的状态无法确认；最终结果仍会展示。"
                        "稍后发送消息或查看 /goal 时会重新检查该会话是否空闲。"
                    )
                    finalization_error.__cause__ = caught
                    retain_active = True
                    async with self._lock(active.binding_id):
                        if clear_attempted:
                            active.state = GoalOperationState.UNKNOWN
                            active.observation_unavailable = False
                            self.close_admission()
                        else:
                            self._mark_goal_observation_unavailable(active)
                    logger.error(
                        "completed Goal finalization is unknown",
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
                async with self._lock(active.binding_id):
                    self._mark_goal_observation_unavailable(active)
                error = self._goal_slot_error(active)
                error.__cause__ = caught
                logger.error(
                    "native Goal terminal observation failed; retaining this Goal slot",
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
                cwd=active.cwd,
                goal=persisted,
                final_physical_turn_id=(
                    active.stream_terminal.final_physical_turn_id
                    if active.stream_terminal is not None
                    else None
                ),
                final_turn_status=active.final_turn_status,
                final_items=active.final_items,
                final_response=active.final_response,
                turn_diff=(
                    active.stream_terminal.turn_diff
                    if active.stream_terminal is not None
                    else None
                ),
                error=error,
                background_cleanup_requested=active.cleanup_succeeded,
                task_feedback=active.task_feedback,
                feedback_revision=active.feedback_revision,
                activity=(
                    self._goal_activity_snapshot(active)
                    if active.task_feedback.progress_card_enabled
                    else None
                ),
                partial_answers=tuple(active.partial_answers.values()),
                finalization=finalization,
                finalization_error=finalization_error,
                patch_children=(
                    await self._completion_patch_children(
                        active.thread_id,
                        active.stream_terminal.final_physical_turn_id,
                        active.final_items,
                    )
                    if active.final_turn_status == "completed"
                    and active.stream_terminal is not None
                    else TaskPatchChildren()
                ),
            )
            try:
                async with asyncio.timeout(
                    limits.GOAL_COMPLETION_DELIVERY_TIMEOUT_SECONDS
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

    async def _read_goal_terminal(self, active: _ActiveGoal) -> GoalSnapshot | None:
        control = self._require_goal_control()
        thread = active.thread
        terminal = active.stream_terminal
        response_materialization_retries = (
            limits.TERMINAL_RESPONSE_MATERIALIZATION_RETRIES
        )
        if thread is None or terminal is None:
            raise RuntimeError("Goal terminal cross-check is missing native handles")
        while True:
            # A clear acknowledgement and its absence proof are committed under
            # the same lock. Do not mistake an in-flight requested clear for an
            # external disappearance; uncertain clear still fails closed.
            async with self._lock(active.binding_id):
                if self._goals.get(active.binding_id) is not active:
                    raise RuntimeError("Goal observer no longer owns this operation")
                persisted = await control.get(active.thread_id)
                if persisted is None:
                    if not active.goal_cleared:
                        raise RuntimeError("persisted Goal disappeared before terminal check")
                elif active.goal_cleared:
                    raise RuntimeError("a cleared Goal was replaced before terminal check")
                elif persisted.thread_id != active.thread_id:
                    raise RuntimeError("persisted Goal identity mismatch")
                elif not _same_goal_generation(active, persisted):
                    raise RuntimeError("persisted Goal generation mismatch")
            response = await thread.read(include_turns=False)
            native = getattr(response, "thread", None)
            if getattr(native, "id", None) != active.thread_id:
                raise RuntimeError("Goal Thread identity mismatch")
            status = _thread_status_type(native)
            if status not in {"idle", "active", "notLoaded"}:
                raise RuntimeError(f"unexpected Goal Thread status: {status!r}")
            if status == "notLoaded":
                await asyncio.sleep(self._poll_interval_seconds)
                continue
            response = await thread.read(include_turns=True)
            native = getattr(response, "thread", None)
            if getattr(native, "id", None) != active.thread_id:
                raise RuntimeError("Goal history Thread identity mismatch")
            history_status = _thread_status_type(native)
            if history_status not in {"idle", "active", "notLoaded"}:
                raise RuntimeError(
                    f"unexpected Goal history Thread status: {history_status!r}"
                )
            turns = tuple(getattr(native, "turns", ()))
            exact = next(
                (
                    turn
                    for turn in turns
                    if getattr(turn, "id", None) == terminal.final_physical_turn_id
                ),
                None,
            )
            # An interrupted physical Turn can end the SDK logical stream while
            # native Goal continuation keeps running. A later history Turn proves
            # that the stream's final identity is stale; never adopt the later Turn.
            if exact is not None and exact is not turns[-1]:
                raise RuntimeError("Goal stream final Turn is not the latest history Turn")
            if (
                (persisted is not None and persisted.status is GoalStatus.ACTIVE)
                or status != "idle"
                or history_status != "idle"
                or any(_enum_value(getattr(turn, "status", None)) == "inProgress" for turn in turns)
            ):
                await asyncio.sleep(self._poll_interval_seconds)
                continue
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
                    == limits.TERMINAL_RESPONSE_MATERIALIZATION_RETRIES
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
            self._supplement_partial_answers(
                active, thread_id=active.thread_id,
                turn_id=terminal.final_physical_turn_id, items=items,
            )
            return persisted

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
