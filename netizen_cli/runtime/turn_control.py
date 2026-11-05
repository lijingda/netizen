"""Ordinary Turn stop, exact recheck and scheduled Turn reads."""

from __future__ import annotations

import asyncio
import logging


from .state import RUNTIME_LOGGER_NAME
from ..bindings import (
    ThreadBinding,
)
from ..domain import (
    ActiveState,
    GoalOperationState,
)
from .contracts import (
    SteerRace,
    TurnObservationUnavailable,
    ScheduledTurnReadError,
    ThreadActivityChanged,
    StopDisposition,
    ActiveTurnSnapshot,
    StopAcknowledger,
)

from . import limits
from .native_values import (
    _enum_value,
    _thread_status_type,
)
from .goals import GoalRuntime


logger = logging.getLogger(RUNTIME_LOGGER_NAME)


class TurnControlRuntime(GoalRuntime):
    """Ordinary Turn stop, exact recheck and scheduled Turn reads."""

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
                    async with asyncio.timeout(limits.STOP_ACK_ATTEMPT_TIMEOUT_SECONDS):
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

    async def _recheck_before_submission(self, binding_id: str) -> int | None:
        """A new message may join one bounded check, never queue a new Turn."""

        async with self._lock(binding_id):
            self._guard_no_lifecycle_locked(binding_id)
            binding = self._bindings.get(binding_id)
            if not binding.active:
                raise SteerRace("当前会话已切换，本条消息未执行，请重新发送。")
            active = self._active.get(binding_id)
            if active is None or active.state is not ActiveState.OBSERVATION_UNAVAILABLE:
                return None
            if active.task is None or active.task.done():
                # The current request reports failure itself. Do not also send
                # another unavailable notice to the original prompt.
                self._start_turn_consumer(
                    active, recover_first=True, notify_unavailable=False,
                )
            ready = active.recheck_ready
            if ready is None:
                # The first observer is still handing off its unavailable
                # notice. Never start a second consumer during that handoff.
                raise self._unavailable_submission_error(active)

        # Never hold the Binding lock while its consumer needs to reconcile.
        # A cancelled message cancels only this wait, not the shared observer.
        try:
            async with asyncio.timeout(limits.TURN_OBSERVATION_RECOVERY_TIMEOUT_SECONDS):
                await ready.wait()
        except TimeoutError:
            raise TurnObservationUnavailable(
                "上一轮状态仍在检查，本条消息未执行。请稍后重新发送或查看 /sessions。"
            ) from None
        async with self._lock(binding_id):
            current = self._active.get(binding_id)
            if current is not None and current is not active:
                raise SteerRace("检查期间任务已变化，本条消息未执行，请重新发送。")
            if active.recheck_ready is not ready:
                # A delayed waiter belongs to the attempt it joined, never a
                # later recovery that overwrote this Turn's frozen revision.
                raise SteerRace("检查期间任务状态已变化，本条消息未执行，请重新发送。")
            if active.recheck_revision is None:
                raise self._unavailable_submission_error(active)
            return active.recheck_revision

    async def read_scheduled_turn(
        self,
        binding_id: str,
        turn_id: str,
        *,
        deadline: float | None = None,
    ) -> str:
        """Read one exact persisted Turn without loading or resuming a Thread.

        This is a bounded restart/refresh observation, not another consumer.
        Later manual Turns may be active in the same Thread and do not change
        the state of the scheduled initial Turn.
        """
        if not turn_id:
            raise ValueError("scheduled Turn ID must not be empty")
        loop = asyncio.get_running_loop()
        deadline = min(
            deadline if deadline is not None else float("inf"),
            loop.time() + limits.TURN_OBSERVATION_RECOVERY_TIMEOUT_SECONDS,
        )
        try:
            async with asyncio.timeout_at(deadline):
                async with self._lock(binding_id):
                    binding = self._scheduled_read_binding_locked(binding_id)
                    thread_id = binding.native_thread_id
                    if thread_id is None:
                        raise ScheduledTurnReadError(
                            "thread_unavailable", "定时执行尚无原生 Thread 引用。",
                        )
                for attempt in range(2):
                    if attempt == 1:
                        await asyncio.sleep(limits.INTERRUPTION_CONFIRMATION_SECONDS)
                    try:
                        response = await self._thread_handle(thread_id).read(
                            include_turns=True,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        raise ScheduledTurnReadError(
                            "read_unavailable", "暂时无法读取本次定时执行的原生状态。",
                        ) from error
                    async with self._lock(binding_id):
                        current = self._scheduled_read_binding_locked(binding_id)
                        if current.native_thread_id != thread_id:
                            raise ScheduledTurnReadError(
                                "identity_changed", "定时执行的原生 Thread 引用已变化。",
                            )
                        native = getattr(response, "thread", None)
                        if getattr(native, "id", None) != thread_id:
                            raise ScheduledTurnReadError(
                                "identity_mismatch", "原生读取返回了其他 Thread。",
                            )
                        exact = [
                            turn for turn in getattr(native, "turns", ())
                            if getattr(turn, "id", None) == turn_id
                        ]
                        if len(exact) != 1:
                            raise ScheduledTurnReadError(
                                "turn_unavailable", "原生历史无法确认本次 exact Turn。",
                            )
                        status = _enum_value(getattr(exact[0], "status", None))
                        if status not in {"inProgress", "completed", "interrupted", "failed"}:
                            raise ScheduledTurnReadError(
                                "status_unavailable", "原生 Turn 状态无法确认。",
                            )
                        if status == "inProgress" and _thread_status_type(native) != "active":
                            raise ScheduledTurnReadError(
                                "status_conflict", "原生 Thread 与本次 Turn 状态不一致。",
                            )
                        if status != "interrupted" or attempt == 1:
                            return status
                # The second attempt always returns or raises above.
                raise AssertionError("scheduled Turn read loop must return")
        except TimeoutError as error:
            raise ScheduledTurnReadError(
                "read_timeout", "读取本次定时执行状态超时，请稍后刷新。",
            ) from error

    def _scheduled_read_binding_locked(self, binding_id: str) -> ThreadBinding:
        self._guard_no_lifecycle_locked(binding_id)
        active = self._active.get(binding_id)
        if active is not None and active.state is ActiveState.OBSERVATION_UNAVAILABLE:
            raise TurnObservationUnavailable(
                "当前会话的 Turn 观测已停止，请先在 /sessions 中重新检查。"
            )
        return self._bindings.get(binding_id)
