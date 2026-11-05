"""Process-local native Thread subscription tracking and idle release."""

from __future__ import annotations

import asyncio
import logging


from ..bindings import (
    ThreadBinding,
)
from ..domain import (
    MessageContextAnchor,
    MentionContextMode,
)
from .contracts import (
    NativeThread,
    RuntimeClosed,
    ThreadOccupied,
    ThreadLifecycleError,
    ContextAnchorRequired,
    ThreadResumeFailed,
    ThreadReleaseError,
    ThreadReleaseStateUnknown,
    ThreadBackgroundTerminalsActive,
    ThreadSubscriptionState,
    ReleaseDisposition,
    ThreadSubscriptionSnapshot,
)
from ..sdk_gap_adapter import (
    ThreadUnsubscribeStatus,
    ThreadUnsubscribeStateUnknown,
)

from .native_values import (
    _thread_status_type,
)
from .state import (
    RUNTIME_LOGGER_NAME,
    _ThreadSubscription,
)
from .state import RuntimeState


logger = logging.getLogger(RUNTIME_LOGGER_NAME)


class ThreadSubscriptionRuntime(RuntimeState):
    """Process-local native Thread subscription tracking and idle release."""

    def thread_subscription_snapshot(
        self,
        binding_id: str,
    ) -> ThreadSubscriptionSnapshot | None:
        """Return this process's transient subscription state for one Binding."""

        record = self._subscriptions.get(binding_id)
        if record is None:
            return None
        release_in_seconds = None
        if record.release_deadline is not None:
            release_in_seconds = max(
                0.0,
                record.release_deadline - asyncio.get_running_loop().time(),
            )
        return ThreadSubscriptionSnapshot(
            binding_id=record.binding_id,
            thread_id=record.thread_id,
            state=record.state,
            release_in_seconds=release_in_seconds,
        )

    async def active_binding_changed(
        self,
        previous_binding_id: str | None,
        current_binding_id: str | None,
    ) -> None:
        """Re-arm transient release timers after a persisted pointer change."""

        if previous_binding_id == current_binding_id:
            return
        # The persisted pointer has already changed. Protect the new current
        # Binding first so an older inactive timer cannot release it while this
        # callback waits for an unrelated previous-Binding lock.
        if current_binding_id is not None:
            async with self._lock(current_binding_id):
                record = self._subscriptions.get(current_binding_id)
                if record is not None and record.state is not ThreadSubscriptionState.RELEASED:
                    self._schedule_subscription_release_locked(
                        record,
                        delay=self._ordinary_thread_idle_seconds,
                    )
        if previous_binding_id is not None:
            async with self._lock(previous_binding_id):
                record = self._subscriptions.get(previous_binding_id)
                if record is not None and record.state is not ThreadSubscriptionState.RELEASED:
                    self._schedule_subscription_release_locked(
                        record,
                        delay=0.0,
                    )

    async def binding_pointer_changed(
        self,
        previous_binding_id: str | None,
        current_binding_id: str | None,
    ) -> None:
        """Management-port name for subscription pointer reconciliation."""

        await self.active_binding_changed(previous_binding_id, current_binding_id)

    async def activate_exact(
        self,
        binding_id: str,
        *,
        context_anchor: MessageContextAnchor | None = None,
    ) -> ThreadBinding:
        """Select an exact Binding while serializing transient state.

        The caller owns the Scope lock. Taking the target Binding lock here
        prevents an already-scheduled inactive-subscription release from
        racing the persisted pointer commit.
        """

        if not self._accepting:
            raise RuntimeClosed("服务正在停止，暂不能切换会话。")
        async with self._lock(binding_id):
            if not self._accepting:
                raise RuntimeClosed("服务正在停止，暂不能切换会话。")
            self._guard_no_lifecycle_locked(binding_id)
            binding = self._bindings.get(binding_id)
            if (
                binding.message_context_mode is MentionContextMode.CATCH_UP
                and context_anchor is None
            ):
                raise ContextAnchorRequired(
                    "该会话启用了 @ 时补充上下文；请从飞书 /resume 切换，"
                    "以建立新的消息边界。"
                )
            if (
                binding.message_context_mode is MentionContextMode.CURRENT_ONLY
                and context_anchor is not None
            ):
                raise ValueError("current-only activation cannot carry an anchor")
            if binding.native_thread_id is not None:
                try:
                    thread = await self._resume_thread_for_operation(
                        binding.native_thread_id,
                    )
                    self._mark_thread_subscribed_locked(binding, thread)
                except (ThreadLifecycleError, ThreadOccupied):
                    raise
                except asyncio.CancelledError:
                    self.close_admission()
                    raise
                except Exception as error:
                    self.close_admission()
                    raise ThreadResumeFailed(
                        "Codex 会话恢复结果未确认；本次未切换当前会话。"
                        "服务已停止接收新任务，请重启服务。"
                    ) from error
            try:
                activated = self._bindings.activate(
                    scope_key=binding.scope_key,
                    binding_id=binding.id,
                    context_anchor=context_anchor,
                )
            except Exception as error:
                # activate() commits before returning its final read. Do not
                # claim rollback merely because that read failed.
                self._advance_admission_revision(binding.id)
                if binding.native_thread_id is None:
                    raise
                try:
                    self._schedule_known_subscription_locked(
                        binding.id,
                        binding.native_thread_id,
                    )
                except Exception:
                    self.close_admission()
                    logger.exception("could not schedule resumed Thread subscription")
                raise ThreadResumeFailed(
                    "原生 Codex 会话已恢复，但本地切换结果未确认；"
                    + (
                        "请检查当前会话后再操作。"
                        if self._accepting
                        else "服务已停止接收新任务，请重启服务。"
                    )
                ) from error
            self._advance_admission_revision(binding.id)
            record = self._subscriptions.get(binding.id)
            if (
                record is not None
                and record.state is not ThreadSubscriptionState.RELEASED
            ):
                self._schedule_subscription_release_locked(
                    record,
                    delay=self._ordinary_thread_idle_seconds,
                )
            return activated

    def _mark_thread_subscribed_locked(
        self,
        binding: ThreadBinding,
        thread: NativeThread,
    ) -> _ThreadSubscription:
        thread_id = binding.native_thread_id or thread.id
        if thread.id != thread_id:
            self.close_admission()
            raise RuntimeError(
                "native Thread subscription identity mismatch; admission closed"
            )
        record = self._subscriptions.get(binding.id)
        if record is not None and record.thread_id != thread_id:
            self.close_admission()
            raise RuntimeError(
                "Binding subscription changed native Thread identity; admission closed"
            )
        now = asyncio.get_running_loop().time()
        if record is None:
            record = _ThreadSubscription(
                binding_id=binding.id,
                thread_id=thread_id,
                thread=thread,
                last_activity=now,
            )
            self._subscriptions[binding.id] = record
        else:
            self._cancel_subscription_idle(record)
            record.thread = thread
            record.last_activity = now
            record.generation += 1
            record.state = ThreadSubscriptionState.SUBSCRIBED
            record.release_deadline = None
        return record

    def _subscription_busy_locked(self, binding_id: str) -> bool:
        return (
            binding_id in self._active
            or binding_id in self._compacting
            or binding_id in self._goals
            or binding_id in self._lifecycles
        )

    def _schedule_known_subscription_locked(
        self,
        binding_id: str,
        thread_id: str,
        *,
        delay: float | None = None,
    ) -> None:
        record = self._subscriptions.get(binding_id)
        if record is None:
            return
        if record.thread_id != thread_id:
            self.close_admission()
            raise RuntimeError(
                "native Thread subscription identity changed; admission closed"
            )
        record.last_activity = asyncio.get_running_loop().time()
        self._schedule_subscription_release_locked(record, delay=delay)

    def _schedule_subscription_release_locked(
        self,
        record: _ThreadSubscription,
        *,
        delay: float | None = None,
        state: ThreadSubscriptionState | None = None,
    ) -> None:
        if (
            not self._accepting
            or self._thread_subscription_control is None
            or self._background_terminal_inspector is None
            or record.thread is None
            or record.state is ThreadSubscriptionState.RELEASED
            or self._subscription_busy_locked(record.binding_id)
        ):
            return
        binding = self._bindings.get(record.binding_id)
        if delay is None:
            delay = self._ordinary_thread_idle_seconds if binding.active else 0.0
        if state is None:
            state = (
                ThreadSubscriptionState.RELEASE_UNKNOWN
                if record.state
                in {
                    ThreadSubscriptionState.RELEASING,
                    ThreadSubscriptionState.RELEASE_UNKNOWN,
                }
                else ThreadSubscriptionState.RELEASE_PENDING
            )
        self._cancel_subscription_idle(record)
        record.generation += 1
        generation = record.generation
        record.state = state
        record.release_deadline = asyncio.get_running_loop().time() + delay
        task = asyncio.create_task(
            self._release_subscription_after_delay(
                record,
                generation,
                delay,
                binding.active,
            ),
            name=f"codex-thread-release:{record.thread_id}",
        )
        record.idle_task = task
        self._subscription_idle_tasks.add(task)
        task.add_done_callback(self._subscription_idle_tasks.discard)

    async def _release_subscription_after_delay(
        self,
        record: _ThreadSubscription,
        generation: int,
        delay: float,
        scheduled_active: bool,
    ) -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            async with self._lock(record.binding_id):
                if (
                    not self._accepting
                    or self._subscriptions.get(record.binding_id) is not record
                    or record.generation != generation
                    or record.state
                    not in {
                        ThreadSubscriptionState.RELEASE_PENDING,
                        ThreadSubscriptionState.RELEASE_UNKNOWN,
                    }
                ):
                    return
                record.idle_task = None
                record.release_deadline = None
                if self._subscription_busy_locked(record.binding_id):
                    if record.state is not ThreadSubscriptionState.RELEASE_UNKNOWN:
                        record.state = ThreadSubscriptionState.SUBSCRIBED
                    return
                binding = self._bindings.get(record.binding_id)
                if binding.active and not scheduled_active:
                    self._schedule_subscription_release_locked(
                        record,
                        delay=self._ordinary_thread_idle_seconds,
                    )
                    return
                await self._attempt_subscription_release_locked(
                    binding,
                    record,
                    explicit=False,
                )
        except asyncio.CancelledError:
            return
        except Exception:
            logger.warning(
                "idle Thread subscription release failed; deferred",
                exc_info=True,
                extra={
                    "binding_id": record.binding_id,
                    "thread_id": record.thread_id,
                },
            )
            async with self._lock(record.binding_id):
                if (
                    self._accepting
                    and self._subscriptions.get(record.binding_id) is record
                    and record.generation == generation
                ):
                    self._schedule_subscription_release_locked(
                        record,
                        delay=self._ordinary_thread_idle_seconds,
                    )

    async def _attempt_subscription_release_locked(
        self,
        binding: ThreadBinding,
        record: _ThreadSubscription,
        *,
        explicit: bool,
    ) -> ReleaseDisposition:
        control = self._thread_subscription_control
        inspector = self._background_terminal_inspector
        thread = record.thread
        assert control is not None and inspector is not None and thread is not None
        if binding.native_thread_id != record.thread_id:
            self.close_admission()
            raise ThreadReleaseStateUnknown(
                "Thread 订阅身份无法确认；服务已停止接收新任务。"
            )
        try:
            response = await thread.read(include_turns=False)
            native = getattr(response, "thread", None)
            if getattr(native, "id", None) != record.thread_id:
                raise RuntimeError("Thread read identity mismatch before release")
            status = _thread_status_type(native)
        except asyncio.CancelledError:
            self._schedule_subscription_release_locked(
                record,
                delay=self._ordinary_thread_idle_seconds,
            )
            raise
        except Exception as error:
            if explicit:
                self._schedule_subscription_release_locked(
                    record,
                    delay=self._ordinary_thread_idle_seconds,
                )
                raise ThreadReleaseError(
                    "无法确认原生 Thread 已空闲，本次未释放订阅。"
                ) from error
            self._schedule_subscription_release_locked(
                record,
                delay=self._ordinary_thread_idle_seconds,
            )
            return ReleaseDisposition.NOT_SUBSCRIBED

        if status != "idle" or getattr(native, "ephemeral", None) is not False:
            self._schedule_subscription_release_locked(
                record,
                delay=self._ordinary_thread_idle_seconds,
            )
            if explicit:
                raise ThreadReleaseError(
                    f"原生 Thread 状态为 {status!r}，本次未释放订阅。"
                )
            return ReleaseDisposition.NOT_SUBSCRIBED

        try:
            has_running = await inspector.has_running(record.thread_id)
        except asyncio.CancelledError:
            self._schedule_subscription_release_locked(
                record,
                delay=self._ordinary_thread_idle_seconds,
            )
            raise
        except Exception as error:
            self._schedule_subscription_release_locked(
                record,
                delay=self._ordinary_thread_idle_seconds,
            )
            if explicit:
                raise ThreadReleaseError(
                    "无法确认后台终端状态，本次未释放订阅。"
                ) from error
            return ReleaseDisposition.NOT_SUBSCRIBED
        if has_running:
            self._schedule_subscription_release_locked(
                record,
                delay=self._ordinary_thread_idle_seconds,
            )
            if explicit:
                raise ThreadBackgroundTerminalsActive(
                    "当前 Thread 仍有已登记后台终端，本次未释放订阅。"
                )
            return ReleaseDisposition.NOT_SUBSCRIBED

        record.state = ThreadSubscriptionState.RELEASING
        try:
            status = await control.unsubscribe(record.thread_id)
        except asyncio.CancelledError:
            record.state = ThreadSubscriptionState.RELEASE_UNKNOWN
            self._schedule_subscription_release_locked(
                record,
                delay=self._ordinary_thread_idle_seconds,
                state=ThreadSubscriptionState.RELEASE_UNKNOWN,
            )
            raise
        except ThreadUnsubscribeStateUnknown as error:
            self._schedule_subscription_release_locked(
                record,
                delay=self._ordinary_thread_idle_seconds,
                state=ThreadSubscriptionState.RELEASE_UNKNOWN,
            )
            if explicit:
                raise ThreadReleaseStateUnknown(
                    "Thread 取消订阅结果未确认；不会立即重试。"
                ) from error
            return ReleaseDisposition.NOT_SUBSCRIBED
        self._mark_subscription_released_locked(record)
        if status is ThreadUnsubscribeStatus.UNSUBSCRIBED:
            return ReleaseDisposition.RELEASED
        return ReleaseDisposition.NOT_SUBSCRIBED

    def _mark_subscription_released_locked(
        self,
        record: _ThreadSubscription,
    ) -> None:
        self._cancel_subscription_idle(record)
        record.generation += 1
        record.thread = None
        record.last_activity = asyncio.get_running_loop().time()
        record.state = ThreadSubscriptionState.RELEASED
