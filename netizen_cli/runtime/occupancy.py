"""Per-Binding Goal slot guards and the exact subscription release that depends on them."""

from __future__ import annotations

import asyncio


from ..bindings import (
    ThreadBinding,
)
from ..domain import (
    ActiveState,
    GoalOperationState,
    GoalStatus,
)
from .contracts import (
    NativeThread,
    RuntimeClosed,
    ThreadStopping,
    ThreadRunningConfiguration,
    ThreadCompacting,
    ThreadGoalActive,
    ExternalGoalActive,
    GoalStateUnknown,
    TurnObservationUnavailable,
    ThreadReleaseError,
    ThreadReleaseStateUnknown,
    ThreadSubscriptionState,
    ReleaseDisposition,
)
from ..sdk_gap_adapter import (
    GoalControlError,
    GoalSnapshot,
)

from . import limits
from .native_values import (
    _enum_value,
    _thread_status_type,
)
from .state import (
    _ActiveGoal,
)
from .subscriptions import ThreadSubscriptionRuntime


class BindingOccupancyRuntime(ThreadSubscriptionRuntime):
    """Per-Binding Goal slot guards and the exact subscription release that depends on them."""

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

    async def _guard_no_goal_locked(self, binding: ThreadBinding) -> None:
        await self._recheck_goal_observation_locked(binding)
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

    def _mark_goal_observation_unavailable(self, active: _ActiveGoal) -> None:
        # Never downgrade an uncertain mutation to a recoverable read failure.
        if active.state is GoalOperationState.RUNNING or (
            active.state is GoalOperationState.PAUSING and active.cleanup_succeeded
        ):
            active.observation_unavailable = True
        active.state = GoalOperationState.UNKNOWN
        self._advance_admission_revision(active.binding_id)

    async def _recheck_goal_observation_locked(self, binding: ThreadBinding) -> None:
        """Release only a failed observer after an on-demand, read-only idle check."""

        active = self._goals.get(binding.id)
        if active is None or not active.observation_unavailable:
            return
        # Preserve the old outcome's delivery before admitting another Turn.
        if (
            active.task is None or not active.task.done()
            or (active.cleanup_required and not active.cleanup_succeeded)
        ):
            raise self._goal_slot_error(active)
        try:
            async with asyncio.timeout(limits.TURN_OBSERVATION_RECOVERY_TIMEOUT_SECONDS):
                if active.thread is None or binding.native_thread_id != active.thread_id:
                    raise RuntimeError("Goal observation Thread identity changed")
                goal = await self._require_goal_control().get(active.thread_id)
                if goal is not None and (
                    goal.thread_id != active.thread_id or goal.status is GoalStatus.ACTIVE
                ):
                    raise RuntimeError("native Goal is active or its identity is unverified")
                response = await active.thread.read(include_turns=True)
                native = getattr(response, "thread", None)
                if (
                    getattr(native, "id", None) != active.thread_id
                    or _thread_status_type(native) != "idle"
                    or any(
                        _enum_value(getattr(turn, "status", None)) == "inProgress"
                        for turn in getattr(native, "turns", ())
                    )
                ):
                    raise RuntimeError("native Goal Thread is not confirmed idle")
        except Exception as error:
            raise GoalStateUnknown(
                "本次 Goal 观察已停止，尚未确认原生会话空闲；"
                "本次操作未执行，请稍后重试。其他会话可继续使用。"
            ) from error
        if self._goals.get(binding.id) is not active:
            raise GoalStateUnknown("检查期间 Goal 占用已变化，请重新操作。")
        self._goals.pop(binding.id)
        self._advance_admission_revision(binding.id)
        self._schedule_known_subscription_locked(binding.id, active.thread_id)

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
            task_feedback=self._capture_task_feedback(binding.task_feedback),
            persisted=persisted,
            generation_created_at=persisted.created_at,
            generation_token_budget=persisted.token_budget,
        )
        self._advance_admission_revision(binding.id)
        self._invalidate_context_window_usage(binding.id)

    def _goal_slot_error(self, active: _ActiveGoal) -> RuntimeError:
        if active.observation_unavailable:
            return GoalStateUnknown(
                "本次 Goal 观察已停止，该会话尚未确认空闲；"
                "请稍后发送消息或查看 /goal 重试。其他会话可继续使用。"
            )
        if active.state is GoalOperationState.EXTERNAL_ACTIVE:
            return ExternalGoalActive(
                "当前 Thread 有重启前或外部客户端启动的 active Goal；"
                "本服务不会猜测重挂，请先在原生 Codex 中暂停，再查看或清除。"
            )
        if active.state in {GoalOperationState.STARTING, GoalOperationState.RUNNING}:
            return ThreadGoalActive(
                "当前 Goal 正在启动或执行；/compact、/config 和第二个 Goal 暂不可用。"
            )
        if active.state is GoalOperationState.PAUSING:
            return ThreadGoalActive("当前 Goal 正在暂停，完成前暂不接受新任务。")
        return GoalStateUnknown(
            "当前 Goal 状态未确认；该会话保持占用，不能开始新的原生操作。"
        )
