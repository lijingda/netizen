"""Exact archive, delete and restore reservations for native Threads."""

from __future__ import annotations

import asyncio
import logging

from openai_codex import (
    InvalidRequestError,
)

from ..bindings import (
    ThreadBinding,
)
from ..domain import (
    ActiveState,
    GoalOperationState,
    MessageContextAnchor,
    MentionContextMode,
)
from .contracts import (
    NativeThread,
    RuntimeClosed,
    ThreadStopping,
    ThreadOccupied,
    ThreadCompacting,
    ThreadNotMaterialized,
    ThreadLifecycleError,
    TurnObservationUnavailable,
    ContextAnchorRequired,
    ThreadLifecycleStateUnknown,
    ThreadRestoreRejected,
    ThreadNotArchived,
    ThreadDeleteUnavailable,
    ThreadDeleteTargetChanged,
    ThreadLifecycleState,
    NativeThreadCatalogState,
    ThreadActivityDiscardedOutcome,
)
from ..sdk_gap_adapter import (
    GoalHandle,
    ThreadDeleteRejected,
)

from . import limits
from .native_values import (
    _resume_rejection,
)
from .state import (
    RUNTIME_LOGGER_NAME,
    _ActiveThreadLifecycle,
)
from .activity import TurnActivityRuntime


logger = logging.getLogger(RUNTIME_LOGGER_NAME)


class ThreadLifecycleRuntime(TurnActivityRuntime):
    """Exact archive, delete and restore reservations for native Threads."""

    async def archive_exact(self, binding_id: str) -> ThreadBinding:
        if not self._accepting:
            raise RuntimeClosed("服务正在停止，暂不能归档会话。")
        async with self._lock(binding_id):
            if not self._accepting:
                raise RuntimeClosed("服务正在停止，暂不能归档会话。")
            self._guard_no_lifecycle_locked(binding_id)
            binding = self._bindings.get(binding_id)
            if binding.native_thread_id is None:
                raise ThreadNotMaterialized(
                    "Lazy 会话没有 Codex 历史可归档；如不再需要，请使用 /delete。"
                )
            operation = self._begin_lifecycle_locked(
                binding,
                ThreadLifecycleState.ARCHIVING,
            )
            thread_id = binding.native_thread_id

        mutation_error: Exception | None = None
        try:
            await self._codex.thread_archive(thread_id)
        except asyncio.CancelledError:
            await self._mark_lifecycle_unknown(operation)
            raise
        except Exception as error:
            mutation_error = error

        if mutation_error is not None:
            try:
                async with asyncio.timeout(
                    limits.THREAD_ARCHIVE_RECONCILE_TIMEOUT_SECONDS
                ):
                    state = await self.thread_catalog_state(thread_id)
            except asyncio.CancelledError:
                await self._mark_lifecycle_unknown(operation)
                raise
            except Exception as reconcile_error:
                await self._mark_lifecycle_unknown(operation)
                raise ThreadLifecycleStateUnknown(
                    "Codex 会话归档结果与 Codex 目录均未确认；会话保留，"
                    "请稍后重新检查。"
                ) from reconcile_error
            if state is NativeThreadCatalogState.ACTIVE:
                await self._release_lifecycle_reservation(operation)
                raise ThreadLifecycleError(
                    "Codex 会话仍在 active 目录，会话已保留；"
                    "本次归档未完成，请重新确认后重试。"
                ) from mutation_error
            if state is not NativeThreadCatalogState.ARCHIVED:
                await self._mark_lifecycle_unknown(operation)
                raise ThreadLifecycleStateUnknown(
                    "Codex 会话归档结果无法由 Codex 目录确认；会话保留，"
                    "请稍后重新检查。"
                ) from mutation_error

        local_commit_error: Exception | None = None
        async with self._lock(binding_id):
            binding = self._require_reserved_lifecycle_binding_locked(operation)
            discarded = self._discard_local_thread_activity_locked(
                binding.id,
                thread_id,
            )
            try:
                archived = self._bindings.archive_binding(binding.id)
            except Exception as error:
                self._retain_unknown_lifecycle(operation)
                local_commit_error = error

        try:
            await self._deliver_thread_activity_discarded(discarded)
        finally:
            if local_commit_error is None:
                async with self._lock(binding_id):
                    self._finish_lifecycle_locked(operation)
        if local_commit_error is not None:
            raise ThreadLifecycleStateUnknown(
                "Codex 会话已归档，但 Netizen 中的会话记录更新结果未确认。"
            ) from local_commit_error
        return archived

    async def delete_exact(
        self,
        binding_id: str,
        *,
        expected_native_thread_id: str | None,
    ) -> ThreadBinding:
        """Delete one exact Lazy or materialized Binding."""

        return await self._delete_binding_exact(
            binding_id,
            allow_materialized=True,
            expected_native_thread_id=expected_native_thread_id,
        )

    async def delete_archived_exact(
        self,
        binding_id: str,
        *,
        expected_native_thread_id: str,
    ) -> ThreadBinding:
        """Delete one exact persisted Thread through the shared primitive."""

        return await self.delete_exact(
            binding_id,
            expected_native_thread_id=expected_native_thread_id,
        )

    async def delete_lazy_exact(self, binding_id: str) -> ThreadBinding:
        """Delete one exact Lazy Binding without reaching native delete."""

        return await self._delete_binding_exact(
            binding_id,
            allow_materialized=False,
            expected_native_thread_id=None,
        )

    async def _delete_binding_exact(
        self,
        binding_id: str,
        *,
        allow_materialized: bool,
        expected_native_thread_id: str | None,
    ) -> ThreadBinding:
        if not self._accepting:
            raise RuntimeClosed("服务正在停止，暂不能删除会话。")
        async with self._lock(binding_id):
            if not self._accepting:
                raise RuntimeClosed("服务正在停止，暂不能删除会话。")
            self._guard_no_lifecycle_locked(binding_id)
            binding = self._bindings.get(binding_id)
            if binding.native_thread_id != expected_native_thread_id:
                if not allow_materialized and binding.native_thread_id is not None:
                    raise ThreadDeleteUnavailable(
                        "已有 Codex 历史的会话不支持 Lazy 删除；本次未调用 Codex。"
                    )
                raise ThreadDeleteTargetChanged(
                    "会话对应的 Codex 会话已变化，本删除确认未执行；"
                    "请重新发送 /delete。"
                )

            if binding.native_thread_id is None:
                operation = self._begin_lifecycle_locked(
                    binding,
                    ThreadLifecycleState.DELETING,
                )
                try:
                    deleted = self._bindings.delete_binding(binding.id)
                except Exception as error:
                    self._retain_unknown_lifecycle(operation)
                    raise ThreadLifecycleStateUnknown(
                        "Lazy 会话删除结果未确认。"
                    ) from error
                self._finish_lifecycle_locked(operation)
                return deleted

            if not allow_materialized:
                raise ThreadDeleteUnavailable(
                    "已有 Codex 历史的会话不支持删除；本次未调用 Codex。"
                )

            control = self._thread_delete_control
            if control is None:
                raise ThreadDeleteUnavailable(
                    "当前 SDK/App Server 的 Thread Delete 兼容契约未通过；"
                    "本次未调用 Codex，会话与 Codex 历史均未改变。"
                )
            operation = self._begin_lifecycle_locked(
                binding,
                ThreadLifecycleState.DELETING,
            )
            thread_id = binding.native_thread_id

        return await self._delete_materialized_reserved(operation, thread_id)

    async def _delete_materialized_reserved(
        self,
        operation: _ActiveThreadLifecycle,
        thread_id: str,
    ) -> ThreadBinding:
        control = self._thread_delete_control
        if control is None:
            await self._release_lifecycle_reservation(operation)
            raise ThreadDeleteUnavailable(
                "当前 SDK/App Server 的 Thread Delete 兼容契约未通过；"
                "本次未调用 Codex，会话与 Codex 历史均未改变。"
            )

        try:
            await control.delete(thread_id)
        except asyncio.CancelledError:
            await self._mark_lifecycle_unknown(operation)
            raise
        except ThreadDeleteRejected as error:
            # Native delete may shut down the Thread before rejecting its
            # history removal. Keep the Binding, but never revive stale local
            # Turn/Goal/Activity or subscription observations.
            async with self._lock(operation.binding_id):
                binding = self._require_reserved_lifecycle_binding_locked(operation)
                discarded = self._discard_local_thread_activity_locked(
                    binding.id,
                    thread_id,
                )
            try:
                await self._deliver_thread_activity_discarded(discarded)
            finally:
                await self._release_lifecycle_reservation(operation)
            raise ThreadLifecycleError(
                "Codex 未完成本次删除，会话已保留；"
                "原有运行活动可能已停止，请重新确认后再操作。"
            ) from error
        except Exception as error:
            await self._mark_lifecycle_unknown(operation)
            raise ThreadLifecycleStateUnknown(
                "Codex 会话删除结果未确认；会话已保留，"
                "当前会话生命周期状态未知，不能自动重试。"
            ) from error

        local_commit_error: Exception | None = None
        async with self._lock(operation.binding_id):
            binding = self._require_reserved_lifecycle_binding_locked(operation)
            discarded = self._discard_local_thread_activity_locked(
                binding.id,
                thread_id,
            )
            try:
                deleted = self._bindings.delete_binding(binding.id)
            except Exception as error:
                self._retain_unknown_lifecycle(operation)
                local_commit_error = error

        try:
            await self._deliver_thread_activity_discarded(discarded)
        finally:
            if local_commit_error is None:
                async with self._lock(operation.binding_id):
                    self._finish_lifecycle_locked(operation)
        if local_commit_error is not None:
            raise ThreadLifecycleStateUnknown(
                "Codex 会话已删除，但 Netizen 中的会话记录删除结果未确认。"
            ) from local_commit_error
        return deleted

    async def restore_exact(self, binding_id: str) -> ThreadBinding:
        """Restore one archived Binding without changing its Scope pointer."""

        return await self._restore_exact(binding_id, activate=False)

    async def restore_as_current_exact(
        self,
        binding_id: str,
        *,
        context_anchor: MessageContextAnchor | None = None,
    ) -> ThreadBinding:
        """Restore one archived Binding and select it in its Scope."""

        return await self._restore_exact(
            binding_id,
            activate=True,
            context_anchor=context_anchor,
        )

    async def _restore_exact(
        self,
        binding_id: str,
        *,
        activate: bool,
        context_anchor: MessageContextAnchor | None = None,
    ) -> ThreadBinding:
        if not self._accepting:
            raise RuntimeClosed("服务正在停止，暂不能恢复归档会话。")
        async with self._lock(binding_id):
            if not self._accepting:
                raise RuntimeClosed("服务正在停止，暂不能恢复归档会话。")
            self._guard_no_lifecycle_locked(binding_id)
            binding = self._bindings.get(binding_id)
            if activate:
                if (
                    binding.message_context_mode is MentionContextMode.CATCH_UP
                    and not binding.active
                    and context_anchor is None
                ):
                    raise ContextAnchorRequired(
                        "该会话启用了 @ 时补充上下文；请从飞书恢复卡片操作，"
                        "以建立新的消息边界。"
                    )
                if (
                    binding.message_context_mode is MentionContextMode.CURRENT_ONLY
                    and context_anchor is not None
                ):
                    raise ValueError(
                        "current-only restore cannot carry a context anchor"
                    )
            elif context_anchor is not None:
                raise ValueError("non-current restore cannot carry a context anchor")
            if binding.native_thread_id is None:
                raise ThreadNotArchived("Lazy 会话不是归档会话。")
            if binding.id in self._compacting:
                raise ThreadCompacting("当前会话正在压缩上下文，暂不能恢复。")
            running = self._active.get(binding.id)
            if running is not None and running.state is not ActiveState.RUNNING:
                if running.state is ActiveState.STOPPING:
                    raise ThreadStopping("当前 Turn 正在停止，暂不能恢复。")
                raise TurnObservationUnavailable(
                    "当前 Turn 观测不可用，请重新检查或停止后再恢复。"
                )
            goal = self._goals.get(binding.id)
            if goal is not None and (
                goal.state is not GoalOperationState.RUNNING or goal.handle is None
            ):
                raise self._goal_slot_error(goal)
            operation = self._begin_lifecycle_locked(
                binding,
                ThreadLifecycleState.UNARCHIVING,
            )
            unarchived = False
            resumed = False
            try:
                try:
                    result = await self._codex.thread_unarchive(
                        binding.native_thread_id
                    )
                except InvalidRequestError as error:
                    # Both rejections precede storage mutation in the pinned
                    # local store. Neither proves absence or writer ownership;
                    # one exact resume determines whether this process can join.
                    if error.code != -32600 or error.message not in (
                        f"no archived rollout found for thread id {binding.native_thread_id}",
                        f"thread {binding.native_thread_id} already has an active writer",
                    ):
                        raise
                else:
                    if result.id != binding.native_thread_id:
                        raise RuntimeError(
                            "thread_unarchive returned a different native ID"
                        )
                    unarchived = True
                try:
                    thread = await self._codex.thread_resume(
                        binding.native_thread_id, include_turns=False
                    )
                except InvalidRequestError as error:
                    rejection = _resume_rejection(error, binding.native_thread_id)
                    if rejection is None:
                        raise
                    reasons = {
                        "archived": "Codex 会话目前已归档，请重新发起恢复。",
                        "not_found": "未找到该 Codex 会话的可恢复记录。",
                        "closing": "Codex 会话正在关闭，请等待完成后重新操作。",
                        "occupied": (
                            "无法取得该会话，可能由其他 Codex 实例占用。"
                            "请释放占用后重新操作。"
                        ),
                    }
                    outcome = "未切换当前会话。" if activate else "未完成会话恢复。"
                    raise ThreadRestoreRejected(
                        ("本次已解除归档，但" if unarchived else "")
                        + outcome + reasons[rejection] + "本地会话关联已保留。",
                        unarchived=unarchived,
                    ) from error
                if thread.id != binding.native_thread_id:
                    raise RuntimeError(
                        "thread_resume returned a different unarchived native ID"
                    )
                resumed = True
                # Preserve the confirmed subscription even if activate() commits
                # then fails its final read. Rejoin never replaces Turn/Goal handles.
                self._mark_thread_subscribed_locked(binding, thread)
                restored = (
                    self._bindings.activate(
                        scope_key=binding.scope_key,
                        binding_id=binding.id,
                        context_anchor=context_anchor,
                    )
                    if activate and not binding.active
                    else self._bindings.get(binding.id)
                )
                if activate:
                    self._advance_admission_revision(binding.id)
            except ThreadRestoreRejected:
                self._finish_lifecycle_locked(operation)
                self._schedule_known_subscription_locked(
                    binding.id, binding.native_thread_id,
                )
                raise
            except asyncio.CancelledError:
                self._retain_unknown_lifecycle(operation)
                raise
            except Exception as error:
                self._retain_unknown_lifecycle(operation)
                if resumed:
                    message = (
                        "Codex 会话已恢复，但本地切换结果未确认；"
                        if activate else
                        "Codex 会话已恢复，但本地订阅状态未确认；"
                    )
                elif unarchived:
                    message = "本次已解除归档，但后续会话恢复结果未确认；"
                else:
                    message = "Codex 会话恢复结果未确认；"
                raise ThreadLifecycleStateUnknown(
                    message + "当前会话"
                    "的生命周期状态待对账。"
                ) from error
            self._finish_lifecycle_locked(operation)
            self._schedule_known_subscription_locked(
                restored.id,
                restored.native_thread_id,
            )
            return restored

    def _begin_lifecycle_locked(
        self,
        binding: ThreadBinding,
        state: ThreadLifecycleState,
    ) -> _ActiveThreadLifecycle:
        self._guard_no_lifecycle_locked(binding.id)
        self._invalidate_name_generation(binding.id)
        active = _ActiveThreadLifecycle(
            binding_id=binding.id,
            thread_id=binding.native_thread_id,
            state=state,
        )
        self._lifecycles[binding.id] = active
        if state in {ThreadLifecycleState.ARCHIVING, ThreadLifecycleState.DELETING}:
            running = self._active.get(binding.id)
            if running is not None:
                running.activity_observation_enabled = False
            goal = self._goals.get(binding.id)
            if goal is not None:
                goal.activity_observation_enabled = False
        self._advance_admission_revision(binding.id)
        return active

    async def _release_lifecycle_reservation(
        self,
        operation: _ActiveThreadLifecycle,
    ) -> None:
        async with self._lock(operation.binding_id):
            if self._lifecycles.get(operation.binding_id) is operation:
                self._finish_lifecycle_locked(operation)

    async def _mark_lifecycle_unknown(
        self,
        operation: _ActiveThreadLifecycle,
    ) -> None:
        async with self._lock(operation.binding_id):
            self._retain_unknown_lifecycle(operation)

    def _require_reserved_lifecycle_binding_locked(
        self,
        operation: _ActiveThreadLifecycle,
    ) -> ThreadBinding:
        if self._lifecycles.get(operation.binding_id) is not operation:
            raise RuntimeError(
                "native Thread lifecycle reservation changed"
            )
        binding = self._bindings.get(operation.binding_id)
        if binding.native_thread_id != operation.thread_id:
            raise RuntimeError(
                "native Thread lifecycle identity changed"
            )
        return binding

    def _finish_lifecycle_locked(
        self,
        active: _ActiveThreadLifecycle,
    ) -> None:
        if self._lifecycles.get(active.binding_id) is not active:
            raise RuntimeError(
                "native Thread lifecycle ownership changed"
            )
        self._lifecycles.pop(active.binding_id, None)
        running = self._active.get(active.binding_id)
        if running is not None:
            running.activity_observation_enabled = True
        goal = self._goals.get(active.binding_id)
        if goal is not None:
            goal.activity_observation_enabled = True
        self._advance_admission_revision(active.binding_id)

    def _retain_unknown_lifecycle(
        self,
        active: _ActiveThreadLifecycle,
    ) -> None:
        if self._lifecycles.get(active.binding_id) is active:
            if active.state is not ThreadLifecycleState.UNKNOWN:
                active.state = ThreadLifecycleState.UNKNOWN
                self._advance_admission_revision(active.binding_id)

    def _discard_local_thread_activity_locked(
        self,
        binding_id: str,
        thread_id: str,
    ) -> ThreadActivityDiscardedOutcome:
        """Forget observations invalidated by native archive/delete shutdown."""

        changed = False
        turn_id: str | None = None
        active = self._active.pop(binding_id, None)
        if active is not None:
            active.activity_observation_enabled = False
            turn_id = active.handle.id
            active.receipt_attempted.set()
            active.cleanup_ready.set()
            if active.task is not None and not active.task.done():
                active.task.cancel()
            changed = True

        for key, retired in tuple(self._retiring_turns.items()):
            if retired.binding_id != binding_id:
                continue
            self._retiring_turns.pop(key)
            retired.activity_observation_enabled = False
            retired.receipt_attempted.set()
            if retired.task is not None and not retired.task.done():
                retired.task.cancel()
            changed = True

        compaction = self._compacting.pop(binding_id, None)
        if compaction is not None:
            compaction.receipt_attempted.set()
            if compaction.task is not None and not compaction.task.done():
                compaction.task.cancel()
            changed = True

        goal = self._goals.pop(binding_id, None)
        if goal is not None:
            if goal.handle is not None:
                turn_id = goal.handle.id
            goal.activity_observation_enabled = False
            goal.receipt_attempted.set()
            goal.cleanup_ready.set()
            if goal.task is not None and not goal.task.done():
                goal.task.cancel()
            if goal.handle is not None:
                close_task = asyncio.create_task(
                    self._close_discarded_goal_handle(goal.handle),
                    name=f"codex-goal-close:{goal.handle.id}",
                )
                self._tasks.add(close_task)
                close_task.add_done_callback(self._tasks.discard)
            changed = True

        record = self._subscriptions.pop(binding_id, None)
        if record is not None:
            self._cancel_subscription_idle(record)
        self._invalidate_context_window_usage(binding_id)
        if changed:
            self._advance_admission_revision(binding_id)
        return ThreadActivityDiscardedOutcome(
            binding_id,
            thread_id,
            turn_id,
        )

    async def _deliver_thread_activity_discarded(
        self,
        outcome: ThreadActivityDiscardedOutcome,
    ) -> None:
        try:
            await self._on_completion(outcome)
        except Exception:
            logger.exception(
                "Thread activity discard presentation cleanup failed",
                extra={
                    "binding_id": outcome.binding_id,
                    "thread_id": outcome.thread_id,
                },
            )

    @staticmethod
    async def _close_discarded_goal_handle(handle: GoalHandle) -> None:
        try:
            await handle.aclose()
        except Exception:
            logger.debug(
                "failed to close Goal observer after native Thread removal",
                exc_info=True,
            )

    async def _lifecycle_thread_locked(
        self,
        binding: ThreadBinding,
    ) -> NativeThread:
        thread_id = binding.native_thread_id
        if thread_id is None:
            raise ThreadNotMaterialized("当前会话尚未在 Codex 中创建。")
        active_turn = self._active.get(binding.id)
        if active_turn is not None:
            thread = active_turn.thread
        elif (compaction := self._compacting.get(binding.id)) is not None:
            thread = compaction.thread
        elif (
            (goal := self._goals.get(binding.id)) is not None
            and goal.thread is not None
        ):
            thread = goal.thread
        else:
            try:
                thread = await self._resume_thread_for_operation(thread_id)
            except ThreadOccupied:
                raise
            except BaseException:
                self._schedule_known_subscription_locked(binding.id, thread_id)
                raise
        if thread.id != thread_id:
            self.close_admission()
            raise RuntimeError("native lifecycle Thread identity mismatch")
        self._mark_thread_subscribed_locked(binding, thread)
        return thread
