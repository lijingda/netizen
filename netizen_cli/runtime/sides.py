"""Ephemeral Side sessions: creation, submission, observation and close."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any


from ..bindings import (
    PROJECT_DELETE_LIMIT,
    SideTopicConflict,
    SideTopicNotFound,
    SideTopicState,
    ThreadBinding,
)
from ..domain import (
    ActiveState,
)
from .contracts import (
    RuntimeClosed,
    ThreadStopping,
    ThreadOccupied,
    ThreadRunningConfiguration,
    ThreadCompacting,
    ThreadNotMaterialized,
    SteerRace,
    TerminalCleanupFailed,
    TurnInterruptFailed,
    SideUnavailable,
    SideSessionNotFound,
    SideSessionConflict,
    SideSessionClosing,
    SideStartFailed,
    SideCloseFailed,
    SubmitDisposition,
    StopDisposition,
    SideSessionState,
    SideSubmission,
    SideSubmissionAdmission,
    SideTurnActivitySnapshot,
    SideSessionSnapshot,
    SideTurnOutcome,
    SideLifecycleOutcome,
    StopAcknowledger,
)
from ..turn_activity import (
    SIDE_ACTIVITY_QUEUE_HIGH_WATER,
    project_question,
)
from ..turn_patch_children import TaskPatchChildren

from . import limits
from .native_values import (
    _enum_value,
    _native_cwd,
    _thread_status_type,
)
from .state import (
    RUNTIME_LOGGER_NAME,
    _ActiveSideTurn,
    _SideSession,
)
from .submission import SubmissionRuntime


logger = logging.getLogger(RUNTIME_LOGGER_NAME)


class SideRuntime(SubmissionRuntime):
    """Ephemeral Side sessions: creation, submission, observation and close."""

    def _init_side_state(self, *, side_idle_seconds: float) -> None:
        self._side_idle_seconds = side_idle_seconds
        self._side_locks: dict[str, asyncio.Lock] = {}
        self._side_idle_tasks: set[asyncio.Task[None]] = set()

    async def create_side(
        self,
        *,
        side_id: str,
        binding: ThreadBinding,
        cwd: Path,
        creator_id: str,
    ) -> SideSessionSnapshot:
        control = self._side_boundary_control
        subscription_control = self._thread_subscription_control
        if control is None or subscription_control is None:
            raise SideUnavailable(
                "当前 SDK/App Server 的 Side Thread 兼容契约未通过。"
            )
        if not side_id or not creator_id:
            raise ValueError("Side identity must not be empty")
        if not self._accepting:
            raise RuntimeClosed("服务正在停止，暂不能创建 Side。")
        self._require_side_creation_allowed(binding, side_id)
        if side_id in self._sides:
            return self.side_snapshot(side_id)

        prepared = self._bindings.get(binding.id)
        if not prepared.active:
            raise SteerRace("当前会话已切换，本次 Side 未创建。")
        if prepared.native_thread_id is None:
            raise ThreadNotMaterialized(
                "当前会话尚未在 Codex 中创建；请先发送一条真实任务，"
                "再使用 /side。"
            )
        prepared_revision = prepared.settings_revision
        prepared_feedback_revision = prepared.feedback_revision
        prepared_feedback = self._capture_task_feedback(prepared.task_feedback)
        configured = prepared.turn_settings
        resolved_settings = None
        if configured is not None:
            resolved_settings = await self.resolve_model_settings(
                model_id=configured.model_id,
                effort_id=configured.effort_id,
                service_tier_id=configured.service_tier_id,
            )

        async with self._lock(binding.id):
            if not self._accepting:
                raise RuntimeClosed("服务正在停止，暂不能创建 Side。")
            self._require_side_creation_allowed(binding, side_id)
            self._guard_no_lifecycle_locked(binding.id)
            if binding.id in self._compacting:
                raise ThreadCompacting(
                    "当前会话正在压缩上下文，完成前不能创建 Side。"
                )
            current = self._bindings.get(binding.id)
            if not current.active:
                raise SteerRace("当前会话已切换，本次 Side 未创建。")
            if current.native_thread_id is None:
                raise ThreadNotMaterialized(
                    "当前会话尚未在 Codex 中创建；请先发送一条真实任务，"
                    "再使用 /side。"
                )
            if current.settings_revision != prepared_revision:
                raise SteerRace("创建 Side 期间会话配置已变化，请重新发送 /side。")
            if current.feedback_revision != prepared_feedback_revision:
                raise SteerRace(
                    "创建 Side 期间任务反馈配置已变化，请重新发送 /side。"
                )
            existing = self._sides.get(side_id)
            if existing is not None:
                return self._side_snapshot(existing)
            await self._guard_no_goal_locked(current)
            current = self._bindings.get(binding.id)
            if not current.active or current.native_thread_id is None:
                raise SteerRace("创建 Side 期间当前会话已变化，请重新发送 /side。")
            if current.settings_revision != prepared_revision:
                raise SteerRace("创建 Side 期间会话配置已变化，请重新发送 /side。")
            if current.feedback_revision != prepared_feedback_revision:
                raise SteerRace(
                    "创建 Side 期间任务反馈配置已变化，请重新发送 /side。"
                )
            parent_active = self._active.get(binding.id)
            if (
                parent_active is not None
                and parent_active.state is ActiveState.STOPPING
            ):
                raise ThreadStopping(
                    "当前父 Turn 正在停止或清理，完成前不能创建 Side。"
                )
            if parent_active is None:
                try:
                    parent_thread = await self._resume_thread_for_operation(
                        current.native_thread_id
                    )
                    if parent_thread.id != current.native_thread_id:
                        self.close_admission()
                        raise RuntimeError(
                            "thread_resume returned a different parent Thread ID"
                        )
                    record = self._mark_thread_subscribed_locked(
                        current,
                        parent_thread,
                    )
                    self._schedule_subscription_release_locked(record)
                    parent_view = await parent_thread.read(include_turns=False)
                    native_parent = getattr(parent_view, "thread", None)
                    if getattr(native_parent, "id", None) != current.native_thread_id:
                        raise RuntimeError("parent Thread read identity mismatch")
                    parent_status = _thread_status_type(native_parent)
                    if parent_status != "idle":
                        if parent_status == "active":
                            raise ThreadRunningConfiguration(
                                "父 Codex Thread 正由外部客户端执行未知 Turn，"
                                "不能创建 Side。"
                            )
                        raise RuntimeError(
                            f"unexpected parent Thread status: {parent_status!r}"
                        )
                    if getattr(native_parent, "ephemeral", None) is not False:
                        raise RuntimeError("parent Thread persistence shape changed")
                except (ThreadRunningConfiguration, ThreadOccupied):
                    raise
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self._schedule_known_subscription_locked(
                        current.id,
                        current.native_thread_id,
                    )
                    raise SideStartFailed(
                        "无法确认父 Codex Thread 处于可 fork 状态，本次 Side 未创建。"
                    ) from error
            # Project deletion can reserve admission while the parent is being
            # resumed/read. Recheck immediately before the native fork.
            self._require_side_creation_allowed(current, side_id)
            try:
                thread = await self._codex.thread_fork(
                    current.native_thread_id,
                    ephemeral=True,
                    include_turns=False,
                )
            except asyncio.CancelledError:
                self.close_admission()
                raise
            except Exception as error:
                self.close_admission()
                raise SideStartFailed(
                    "Codex Side Thread 创建结果未确认；服务已停止接收新任务，"
                    "请重启服务。"
                ) from error
            if (
                not isinstance(thread.id, str)
                or not thread.id
                or thread.id == current.native_thread_id
            ):
                self.close_admission()
                raise SideStartFailed(
                    "Codex 返回了无效的 Side Thread 标识；"
                    "服务已停止接收新任务，请重启服务。"
                )
            try:
                fork_view = await thread.read(include_turns=False)
                native_fork = getattr(fork_view, "thread", None)
                if getattr(native_fork, "id", None) != thread.id:
                    raise RuntimeError("forked Side Thread read identity mismatch")
                cwd = _native_cwd(native_fork, thread.id)
                if getattr(native_fork, "ephemeral", None) is not True:
                    raise RuntimeError("forked Side Thread is not ephemeral")
                forked_from_id = getattr(native_fork, "forked_from_id", None)
                if forked_from_id is not None and forked_from_id != current.native_thread_id:
                    raise RuntimeError("forked Side Thread parent identity mismatch")
                await control.inject_boundary(thread.id, parent_thread_id=current.native_thread_id)
            except BaseException as error:
                try:
                    await subscription_control.unsubscribe(thread.id)
                except BaseException as unsubscribe_error:
                    self.close_admission()
                    session = _SideSession(
                        side_id=side_id,
                        parent_binding_id=current.id,
                        parent_thread_id=current.native_thread_id,
                        thread=thread,
                        project_alias=current.project_alias,
                        cwd=cwd,
                        creator_id=creator_id,
                        turn_settings=resolved_settings,
                        task_feedback=prepared_feedback,
                        feedback_revision=prepared_feedback_revision,
                        last_activity=asyncio.get_running_loop().time(),
                        state=SideSessionState.CLOSING,
                    )
                    self._sides[side_id] = session
                    combined = BaseExceptionGroup(
                        "Side boundary and compensation state are unknown",
                        [error, unsubscribe_error],
                    )
                    if isinstance(unsubscribe_error, asyncio.CancelledError):
                        raise unsubscribe_error
                    if isinstance(error, asyncio.CancelledError):
                        raise error
                    raise SideStartFailed(
                        "Side 边界注入与取消订阅结果均未确认；"
                        "服务已停止接收新任务，请重启服务。"
                    ) from combined
                if isinstance(error, asyncio.CancelledError):
                    raise
                raise SideStartFailed(
                    "Side 边界注入结果未确认；已请求取消订阅，飞书话题未创建。"
                ) from error

            session = _SideSession(
                side_id=side_id,
                parent_binding_id=current.id,
                parent_thread_id=current.native_thread_id,
                thread=thread,
                project_alias=current.project_alias,
                cwd=cwd,
                creator_id=creator_id,
                turn_settings=resolved_settings,
                task_feedback=prepared_feedback,
                feedback_revision=prepared_feedback_revision,
                last_activity=asyncio.get_running_loop().time(),
            )
            self._sides[side_id] = session
            return self._side_snapshot(session)

    def _require_side_creation_allowed(
        self,
        binding: ThreadBinding,
        side_id: str,
    ) -> None:
        self._bindings.require_project_not_deleting(binding.project_alias)
        self._bindings.worktrees.require_binding_not_deleting(binding.id)
        try:
            route = self._bindings.get_side_topic(side_id)
        except SideTopicNotFound:
            # Standalone Runtime callers may create before reserving a route.
            return
        if route.parent_binding_id != binding.id or route.state.terminal:
            raise SideSessionConflict("Side 路由已结束或父会话已变化，本次未创建。")

    async def drain_project_side_creation(self, binding_id: str) -> None:
        """Wait for an admitted Side fork to publish its exact local session.

        The caller first fences Project admission and bounds this wait. Taking
        only the parent Binding lock also drains setup without native I/O or
        blocking unrelated Bindings, before an absent Side session is terminal.
        """

        self._invalidate_name_generation(binding_id)
        async with self._lock(binding_id):
            pass

    async def attach_side_topic(
        self,
        *,
        side_id: str,
        topic_id: str,
        root_message_id: str,
    ) -> SideSessionSnapshot:
        if not topic_id or not root_message_id:
            raise ValueError("Side Topic identity must not be empty")
        async with self._side_lock(side_id):
            session = self._require_side(side_id)
            if session.state is SideSessionState.CLOSING:
                raise SideSessionClosing("Side 正在结束。")
            record = self._bindings.get_side_topic(side_id)
            creating_match = (
                record.state is SideTopicState.CREATING
                and record.topic_id in {None, topic_id}
                and record.root_message_id == root_message_id
            )
            open_match = (
                record.state is SideTopicState.OPEN
                and record.topic_id == topic_id
                and record.root_message_id == root_message_id
            )
            if not creating_match and not open_match:
                raise SideSessionConflict("Side Runtime 与飞书话题记录不一致。")
            if session.topic_id is not None and (
                session.topic_id != topic_id
                or session.root_message_id != root_message_id
            ):
                raise SideSessionConflict("Side Topic identity is write-once")
            session.topic_id = topic_id
            session.root_message_id = root_message_id
            session.last_activity = asyncio.get_running_loop().time()
            self._schedule_side_idle(session)
            return self._side_snapshot(session)

    def side_snapshot(self, side_id: str) -> SideSessionSnapshot:
        return self._side_snapshot(self._require_side(side_id))

    def side_snapshot_for_thread(self, thread_id: str) -> SideSessionSnapshot | None:
        """Resolve a native caller without importing ephemeral Threads or their history."""
        return next((self._side_snapshot(session) for session in self._sides.values()
                     if session.thread.id == thread_id), None)

    def side_ids_for_projects(self, project_aliases: tuple[str, ...]) -> tuple[str, ...]:
        """Find live Side IDs by frozen ownership, including deleted Parents."""
        aliases = set(project_aliases)
        return tuple(
            side_id for side_id, session in self._sides.items()
            if session.project_alias in aliases
        )

    def project_side_snapshots(
        self,
        alias: str,
        *,
        limit: int = PROJECT_DELETE_LIMIT,
    ) -> tuple[SideSessionSnapshot, ...]:
        """Project live Sides, including those whose Parent Binding was deleted."""

        if not isinstance(alias, str) or not alias:
            raise ValueError("Project alias must not be empty")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= PROJECT_DELETE_LIMIT
        ):
            raise ValueError("Project Side snapshot limit is invalid")
        snapshots: list[SideSessionSnapshot] = []
        for session in self._sides.values():
            if session.project_alias == alias:
                if len(snapshots) >= limit:
                    raise ValueError("Project Side snapshot exceeds the limit")
                snapshots.append(self._side_snapshot(session))
        return tuple(snapshots)

    def worktree_side_snapshots(
        self, root: Path, *, limit: int = PROJECT_DELETE_LIMIT,
    ) -> tuple[SideSessionSnapshot, ...]:
        """Use each live Side's frozen cwd, independent of its Parent's state."""
        if not root.is_absolute() or type(limit) is not int or not 1 <= limit <= PROJECT_DELETE_LIMIT:
            raise ValueError("Worktree Side snapshot range is invalid")
        snapshots: list[SideSessionSnapshot] = []
        for session in self._sides.values():
            if session.cwd.is_relative_to(root):
                if len(snapshots) >= limit:
                    raise ValueError("Worktree Side snapshot exceeds the limit")
                snapshots.append(self._side_snapshot(session))
        return tuple(snapshots)

    async def capture_side_submission_admission(
        self,
        side_id: str,
    ) -> SideSubmissionAdmission:
        if not self._accepting:
            raise RuntimeClosed("服务正在停止，暂不接受 Side 消息。")
        async with self._side_lock(side_id):
            session = self._require_side(side_id)
            if not self._accepting:
                raise RuntimeClosed("服务正在停止，暂不接受 Side 消息。")
            self._bindings.worktrees.require_cwd_not_deleting(session.cwd)
            if session.state is SideSessionState.CLOSING:
                raise SideSessionClosing("Side 正在结束，暂不接受新消息。")
            if session.topic_id is None:
                raise SideSessionConflict("Side 话题尚未准备完成。")
            active = session.active
            if active is not None and active.state is ActiveState.STOPPING:
                raise SideSessionClosing(
                    "当前 Side Turn 正在停止，暂不接受新消息。"
                )
            return SideSubmissionAdmission(
                side_id=side_id,
                revision=session.revision,
                thread_id=session.thread.id,
                turn_id=active.handle.id if active is not None else None,
            )

    async def submit_side(
        self,
        *,
        side_id: str,
        input: Any,
        owner_id: str,
        origin: object,
        admission: SideSubmissionAdmission | None = None,
        skill_names: tuple[str, ...] = (),
    ) -> SideSubmission:
        if not self._accepting:
            raise RuntimeClosed("服务正在停止，暂不接受 Side 消息。")
        if admission is not None and admission.side_id != side_id:
            raise ValueError("Side submission admission belongs to another Side")
        if admission is None:
            admission = await self.capture_side_submission_admission(side_id)
        session = self._require_side(side_id)
        native_input = input
        if skill_names:
            native_input = await self._compile_skill_input(
                cwd=session.cwd,
                text=input,
                names=skill_names,
            )

        start_error: BaseException | None = None
        started: SideSubmission | None = None
        async with self._side_lock(side_id):
            session = self._require_side(side_id)
            self._bindings.worktrees.require_cwd_not_deleting(session.cwd)
            if not self._accepting:
                raise RuntimeClosed("服务正在停止，暂不接受 Side 消息。")
            if session.state is SideSessionState.CLOSING:
                raise SideSessionClosing("Side 正在结束，暂不接受新消息。")
            if session.topic_id is None or session.root_message_id is None:
                raise SideSessionConflict("Side 话题尚未准备完成。")
            active = session.active
            self._redeem_side_admission(session, active, admission)
            session.revision += 1
            if active is not None:
                if active.state is ActiveState.STOPPING:
                    raise SideSessionClosing(
                        "当前 Side Turn 正在停止，暂不接受新消息。"
                    )
                if active.task_feedback.progress_card_enabled:
                    self._refresh_turn_activity(active)
                stale_after_cursor = active.plan_cursor
                self._notify_input_received(
                    active.handle.thread_id, active.handle.id, active.handle.id,
                )
                try:
                    await self._steer_turn(
                        active.handle.steer(native_input),
                        race_message="当前 Side Turn 恰好已经结束，本条消息未执行，请重新发送。",
                    )
                except asyncio.CancelledError:
                    session.state = SideSessionState.CLOSING
                    session.revision += 1
                    self._cancel_side_idle(session)
                    raise
                active.steer_count += 1
                last_update = active.plan_last_update_cursor
                if last_update is not None and last_update > stale_after_cursor:
                    active.plan_may_be_stale = False
                    active.plan_stale_after_cursor = None
                else:
                    active.plan_may_be_stale = True
                    active.plan_stale_after_cursor = stale_after_cursor
                active.activity_revision += 1
                session.last_activity = asyncio.get_running_loop().time()
                self._touch_side_topic(side_id)
                return SideSubmission(
                    SubmitDisposition.STEERED,
                    side_id,
                    active.handle.thread_id,
                    active.handle.id,
                    task_feedback=active.task_feedback,
                    feedback_revision=active.feedback_revision,
                )

            turn_kwargs: dict[str, object] = {}
            if session.turn_settings is not None:
                turn_kwargs = {
                    "model": session.turn_settings.model,
                    "effort": session.turn_settings.effort,
                    "service_tier": session.turn_settings.service_tier_id,
                }
            try:
                handle = await session.thread.turn(native_input, **turn_kwargs)
            except BaseException as error:
                session.state = SideSessionState.CLOSING
                session.turn_start_state_unknown = True
                session.revision += 1
                self._cancel_side_idle(session)
                self.close_admission()
                start_error = error
            else:
                if handle.thread_id != session.thread.id:
                    session.state = SideSessionState.CLOSING
                    session.turn_start_state_unknown = True
                    session.revision += 1
                    self._cancel_side_idle(session)
                    self.close_admission()
                    start_error = RuntimeError(
                        "Side Turn handle returned a different Thread ID"
                    )
                else:
                    receipt_attempted = asyncio.Event()
                    session.terminal_cleanup_succeeded = False
                    active = _ActiveSideTurn(
                        side_id=side_id,
                        handle=handle,
                        owner_id=owner_id,
                        origin=origin,
                        receipt_attempted=receipt_attempted,
                        task_feedback=session.task_feedback,
                        feedback_revision=session.feedback_revision,
                        plan_available=self._turn_plan_observer is not None,
                    )
                    session.active = active
                    session.last_activity = asyncio.get_running_loop().time()
                    self._cancel_side_idle(session)
                    self._touch_side_topic(side_id)
                    self._track_side(session, active)
                    started = SideSubmission(
                        SubmitDisposition.STARTED,
                        side_id,
                        session.thread.id,
                        handle.id,
                        receipt_attempted.set,
                        task_feedback=active.task_feedback,
                        feedback_revision=active.feedback_revision,
                    )

        if start_error is not None:
            if isinstance(start_error, asyncio.CancelledError):
                raise start_error
            raise SideStartFailed(
                "Codex Side Turn 启动结果未确认；服务已停止接收新任务，"
                "且不会清理或退订这个 Side。请重启服务。"
            ) from start_error
        assert started is not None
        return started

    async def stop_side(
        self,
        side_id: str,
        *,
        acknowledge: StopAcknowledger | None = None,
        expected_revision: int | None = None,
        expected_turn_id: str | None = None,
    ) -> StopDisposition:
        async with self._side_lock(side_id):
            session = self._require_side(side_id)
            if expected_revision is not None and (
                session.revision != expected_revision
                or (session.active.handle.id if session.active is not None else None) != expected_turn_id
            ):
                raise SideSessionConflict("Side 运行状态已变化，请刷新后重试。")
            if session.state is SideSessionState.CLOSING:
                return StopDisposition.STOPPING
            active = session.active
            if active is None or (
                active.terminal_observed and active.state is ActiveState.RUNNING
            ):
                return StopDisposition.NOT_RUNNING
            if active.state is ActiveState.RUNNING:
                active.state = ActiveState.STOPPING
                active.activity_revision += 1
                session.revision += 1
            if acknowledge is not None:
                try:
                    async with asyncio.timeout(limits.STOP_ACK_ATTEMPT_TIMEOUT_SECONDS):
                        await acknowledge()
                except TimeoutError:
                    logger.warning(
                        "Side stop acknowledgement timed out",
                        extra={"side_id": side_id, "turn_id": active.handle.id},
                    )
                except Exception:
                    logger.warning(
                        "Side stop acknowledgement failed",
                        exc_info=True,
                        extra={"side_id": side_id, "turn_id": active.handle.id},
                    )
            return await self._interrupt_side_turn(session, active)

    async def close_side(
        self,
        side_id: str,
        *,
        state: SideTopicState = SideTopicState.CLOSED,
    ) -> SideLifecycleOutcome:
        if state not in {
            SideTopicState.CLOSED,
            SideTopicState.EXPIRED,
            SideTopicState.FAILED,
        }:
            raise ValueError("Side close state must be terminal")
        session = self._require_side(side_id)
        async with session.close_lock:
            session = self._require_side(side_id)
            active: _ActiveSideTurn | None
            async with self._side_lock(side_id):
                session = self._require_side(side_id)
                session.state = SideSessionState.CLOSING
                session.revision += 1
                self._cancel_side_idle(session)
                active = session.active
                if active is not None:
                    active.activity_observation_enabled = False
                    if active.state is ActiveState.RUNNING:
                        active.state = ActiveState.STOPPING
                        active.activity_revision += 1
                    active.receipt_attempted.set()
                    if active.cleanup_required:
                        active.cleanup_ready.set()

            if session.turn_start_state_unknown:
                error = SideCloseFailed(
                    "Side Turn 启动结果未知，不能安全确认中断；"
                    "不会清理、退订或报告结束，请重启服务。"
                )
                await self._deliver_side_lifecycle(
                    SideLifecycleOutcome(
                        side_id=side_id,
                        state=SideTopicState.OPEN,
                        error=error,
                    )
                )
                raise error
            if session.turn_terminal_state_unknown:
                error = SideCloseFailed(
                    "Side Turn 终态未知，不能安全确认中断；"
                    "不会清理、退订或报告结束，请重启服务。"
                )
                await self._deliver_side_lifecycle(
                    SideLifecycleOutcome(
                        side_id=side_id,
                        state=SideTopicState.OPEN,
                        error=error,
                    )
                )
                raise error

            errors: list[BaseException] = []
            if active is not None and not active.terminal_observed:
                try:
                    await self._request_side_interrupt_for_close(session, active)
                except asyncio.CancelledError:
                    raise
                except BaseException as error:
                    errors.append(error)

            active_task = active.task if active is not None else None
            if (
                not errors
                and active_task is not None
                and active_task is not asyncio.current_task()
            ):
                try:
                    await asyncio.wait_for(
                        asyncio.shield(active_task),
                        timeout=limits.SIDE_CLOSE_DRAIN_TIMEOUT_SECONDS,
                    )
                except TimeoutError as error:
                    logger.warning(
                        "Side Turn did not drain before unsubscribe",
                        extra={"side_id": side_id, "thread_id": session.thread.id},
                    )
                    errors.append(error)
                except asyncio.CancelledError:
                    raise
                except BaseException as error:
                    errors.append(error)

            if (
                not errors
                and active is not None
                and not active.terminal_observed
            ):
                errors.append(
                    SideCloseFailed(
                        "Side Turn 终态未确认，不能清理或取消订阅。"
                    )
                )

            if errors:
                grouped = BaseExceptionGroup("Side close failed", errors)
                await self._deliver_side_lifecycle(
                    SideLifecycleOutcome(
                        side_id=side_id,
                        state=SideTopicState.OPEN,
                        error=grouped,
                    )
                )
                raise SideCloseFailed(
                    "Side 正在关闭，但 Codex 中断或终态未确认；"
                    "不会清理或退订，请再次结束 Side。"
                ) from errors[0]

            outcome = await self._finalize_side(
                session,
                state=state,
                error=None,
            )
            if outcome.error is not None:
                raise SideCloseFailed(
                    "Side 已停止接收消息，但 Codex 清理未全部确认。"
                ) from outcome.error
            return outcome

    async def close_side_exact(
        self,
        side_id: str,
        *,
        state: SideTopicState = SideTopicState.CLOSED,
    ) -> SideLifecycleOutcome:
        """Management-port name for the exact Side close primitive."""

        return await self.close_side(side_id, state=state)

    def _side_snapshot(self, session: _SideSession) -> SideSessionSnapshot:
        active = session.active
        return SideSessionSnapshot(
            side_id=session.side_id,
            parent_binding_id=session.parent_binding_id,
            parent_thread_id=session.parent_thread_id,
            thread_id=session.thread.id,
            project_alias=session.project_alias,
            cwd=session.cwd,
            creator_id=session.creator_id,
            state=session.state,
            topic_id=session.topic_id,
            root_message_id=session.root_message_id,
            turn_id=active.handle.id if active is not None else None,
            turn_state=active.state if active is not None else None,
            last_activity=session.last_activity,
            revision=session.revision,
        )

    def _require_side(self, side_id: str) -> _SideSession:
        session = self._sides.get(side_id)
        if session is None:
            raise SideSessionNotFound(side_id)
        return session

    def _side_lock(self, side_id: str) -> asyncio.Lock:
        lock = self._side_locks.get(side_id)
        if lock is None:
            lock = asyncio.Lock()
            self._side_locks[side_id] = lock
        return lock

    def _touch_side_topic(self, side_id: str) -> None:
        try:
            self._bindings.touch_side_topic(side_id)
        except Exception:
            logger.warning(
                "failed to update Side Topic activity timestamp",
                exc_info=True,
                extra={"side_id": side_id},
            )

    def _cancel_side_idle(self, session: _SideSession) -> None:
        task = session.idle_task
        session.idle_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _schedule_side_idle(self, session: _SideSession) -> None:
        self._cancel_side_idle(session)
        if (
            session.state is not SideSessionState.OPEN
            or session.active is not None
            or session.topic_id is None
        ):
            return
        task = asyncio.create_task(
            self._expire_side_when_idle(session),
            name=f"codex-side-idle:{session.side_id}",
        )
        session.idle_task = task
        self._side_idle_tasks.add(task)
        task.add_done_callback(self._side_idle_tasks.discard)

    async def _expire_side_when_idle(self, session: _SideSession) -> None:
        try:
            while True:
                elapsed = asyncio.get_running_loop().time() - session.last_activity
                remaining = self._side_idle_seconds - elapsed
                if remaining > 0:
                    await asyncio.sleep(remaining)
                async with self._side_lock(session.side_id):
                    if self._sides.get(session.side_id) is not session:
                        return
                    if (
                        session.state is not SideSessionState.OPEN
                        or session.active is not None
                    ):
                        return
                    elapsed = (
                        asyncio.get_running_loop().time() - session.last_activity
                    )
                    if elapsed < self._side_idle_seconds:
                        continue
                    session.idle_task = None
                    break
            await self.close_side(
                session.side_id,
                state=SideTopicState.EXPIRED,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Side idle expiry failed",
                extra={"side_id": session.side_id, "thread_id": session.thread.id},
            )

    async def _interrupt_side_turn(
        self,
        session: _SideSession,
        active: _ActiveSideTurn,
    ) -> StopDisposition:
        if active.handle.thread_id != session.thread.id:
            raise SideSessionConflict(
                "Side active Turn 与 Side Thread 标识不一致。"
            )
        active.state = ActiveState.STOPPING
        active.cleanup_required = True
        active.cleanup_ready.clear()
        if not active.interrupt_succeeded:
            retrying_unknown_interrupt = active.interrupt_attempted
            active.interrupt_attempted = True
            try:
                await active.handle.interrupt()
            except Exception as error:
                if active.terminal_observed and retrying_unknown_interrupt:
                    logger.warning(
                        "repeat Side interrupt failed after exact terminal; "
                        "continuing exact Thread cleanup",
                        exc_info=True,
                        extra={
                            "side_id": session.side_id,
                            "thread_id": session.thread.id,
                            "turn_id": active.handle.id,
                        },
                    )
                else:
                    active.cleanup_ready.set()
                    raise TurnInterruptFailed(
                        "Side Turn 中断结果未确认；可再次发送 /stop 或结束 Side。"
                    ) from error
            else:
                active.interrupt_succeeded = True
        if not active.cleanup_succeeded:
            try:
                await self._terminal_cleanup.clean_thread(session.thread.id)
            except Exception as error:
                active.cleanup_ready.set()
                raise TerminalCleanupFailed(
                    "Side Turn 已请求中断，但已登记后台终端的清理请求失败；"
                    "前台工具进程也可能仍在运行。可再次发送 /stop 重试。"
                ) from error
            active.cleanup_succeeded = True
            session.terminal_cleanup_succeeded = True
        active.cleanup_ready.set()
        return StopDisposition.REQUESTED

    async def _request_side_interrupt_for_close(
        self,
        session: _SideSession,
        active: _ActiveSideTurn,
    ) -> None:
        if active.handle.thread_id != session.thread.id:
            raise SideSessionConflict(
                "Side active Turn 与 Side Thread 标识不一致。"
            )
        if active.terminal_observed:
            return
        retrying_unknown_interrupt = active.interrupt_attempted
        active.interrupt_attempted = True
        try:
            await active.handle.interrupt()
        except Exception as error:
            if active.terminal_observed and retrying_unknown_interrupt:
                logger.warning(
                    "repeat Side close interrupt failed after exact terminal",
                    exc_info=True,
                    extra={
                        "side_id": session.side_id,
                        "thread_id": session.thread.id,
                        "turn_id": active.handle.id,
                    },
                )
                return
            raise TurnInterruptFailed(
                "Side Turn 中断结果未确认；不能继续清理或取消订阅。"
            ) from error
        active.interrupt_succeeded = True

    def _track_side(
        self,
        session: _SideSession,
        active: _ActiveSideTurn,
    ) -> None:
        task = asyncio.create_task(
            self._consume_side(session, active),
            name=f"codex-side-turn:{active.handle.id}",
        )
        active.task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _consume_side(
        self,
        session: _SideSession,
        active: _ActiveSideTurn,
    ) -> None:
        result: object | None = None
        error: BaseException | None = None
        activity: SideTurnActivitySnapshot | None = None
        try:
            # Side Threads are ephemeral. Intentionally use the normal SDK
            # handle path and do not apply persisted-thread completion recovery.
            # Activity, answers and questions share one observation loop. Only
            # peek until exact completion is queued; ``handle.run()`` remains
            # the sole consumer and terminal authority.
            while True:
                observation = self._refresh_turn_activity(active)
                if observation is None:
                    break
                if observation.retained_count >= SIDE_ACTIVITY_QUEUE_HIGH_WATER:
                    before = self._turn_activity_visible_state(active)
                    active.plan_available = False
                    active.activity_observation_enabled = False
                    if self._turn_activity_visible_state(active) != before:
                        active.activity_revision += 1
                    logger.warning(
                        "Side Turn activity queue reached the fixed high water; "
                        "falling back to the unique consumer",
                        extra={
                            "thread_id": active.handle.thread_id,
                            "turn_id": active.handle.id,
                        },
                    )
                    break
                if active.completion_notification_seen:
                    break
                await asyncio.sleep(self._poll_interval_seconds)
            # Once drain starts, status/steer refreshes must no longer peek at
            # notifications concurrently consumed by the SDK handle.
            active.activity_observation_enabled = False
            result = await active.handle.run()
            active.terminal_observed = True
            self._supplement_partial_answers(
                active, thread_id=active.handle.thread_id, turn_id=active.handle.id,
                items=tuple(getattr(result, "items", ())),
            )
            # Drain can include questions missed after observation fell back.
            # Reuse delivery dedup and the live Side guard, without restarting
            # observation or retrying earlier presentation attempts.
            if self._on_question is not None:
                for item in getattr(result, "items", ()):
                    try:
                        request = project_question(getattr(item, "root", None))
                        if request is not None:
                            self._deliver_question(active, active.handle.id, request)
                    except Exception as caught:
                        logger.warning(
                            "Side result question presentation unavailable",
                            extra={
                                "side_id": session.side_id,
                                "turn_id": active.handle.id,
                                "error_type": type(caught).__name__,
                            },
                        )
        except asyncio.CancelledError:
            raise
        except BaseException as caught:
            error = caught
        finally:
            if active.state is ActiveState.STOPPING and active.cleanup_required:
                while (
                    not active.cleanup_succeeded
                    and session.state is SideSessionState.OPEN
                ):
                    await active.cleanup_ready.wait()
                    if (
                        not active.cleanup_succeeded
                        and session.state is SideSessionState.OPEN
                    ):
                        active.cleanup_ready.clear()
            async with self._side_lock(session.side_id):
                if (
                    self._sides.get(session.side_id) is session
                    and session.active is active
                ):
                    if active.task_feedback.progress_card_enabled:
                        activity = self._side_turn_activity_snapshot(
                            session.side_id,
                            active,
                        )
                    cleanup_debt = (
                        active.cleanup_required and not active.cleanup_succeeded
                    )
                    if error is not None:
                        session.state = SideSessionState.CLOSING
                        session.turn_terminal_state_unknown = True
                        session.revision += 1
                        self._cancel_side_idle(session)
                        self.close_admission()
                    elif (
                        not cleanup_debt
                        and session.state is SideSessionState.OPEN
                    ):
                        session.active = None
                        session.revision += 1
                        session.last_activity = asyncio.get_running_loop().time()
                        self._touch_side_topic(session.side_id)
                        self._schedule_side_idle(session)

        await active.receipt_attempted.wait()
        outcome = SideTurnOutcome(
            side_id=session.side_id,
            parent_binding_id=session.parent_binding_id,
            thread_id=session.thread.id,
            turn_id=active.handle.id,
            owner_id=active.owner_id,
            origin=active.origin,
            cwd=session.cwd,
            result=result,
            error=error,
            background_cleanup_requested=active.cleanup_succeeded,
            stop_requested=active.state is ActiveState.STOPPING,
            task_feedback=active.task_feedback,
            feedback_revision=active.feedback_revision,
            activity=activity,
            partial_answers=tuple(active.partial_answers.values()),
            patch_children=(
                await self._completion_patch_children(
                    session.thread.id, active.handle.id,
                    tuple(getattr(result, "items", ())),
                )
                if error is None and _enum_value(getattr(result, "status", None)) == "completed"
                else TaskPatchChildren()
            ),
        )
        try:
            await self._on_completion(outcome)
        except Exception:
            logger.exception(
                "Side Turn completion delivery failed",
                extra={"side_id": session.side_id, "turn_id": active.handle.id},
            )

    async def _finalize_side(
        self,
        session: _SideSession,
        *,
        state: SideTopicState,
        error: BaseException | None,
    ) -> SideLifecycleOutcome:
        control = self._thread_subscription_control
        assert control is not None
        cleanup_error: BaseException | None = None
        if not session.terminal_cleanup_succeeded:
            try:
                await self._terminal_cleanup.clean_thread(session.thread.id)
            except asyncio.CancelledError:
                raise
            except Exception as caught:
                cleanup_error = caught
            else:
                session.terminal_cleanup_succeeded = True
        if cleanup_error is not None:
            combined = (
                BaseExceptionGroup(
                    "Side cleanup failed",
                    [error, cleanup_error],
                )
                if error is not None
                else cleanup_error
            )
            outcome = SideLifecycleOutcome(
                side_id=session.side_id,
                state=SideTopicState.OPEN,
                error=combined,
            )
            await self._deliver_side_lifecycle(outcome)
            return outcome

        unsubscribe_error: BaseException | None = None
        try:
            await control.unsubscribe(session.thread.id)
        except asyncio.CancelledError:
            raise
        except Exception as caught:
            unsubscribe_error = caught

        if unsubscribe_error is not None:
            combined = (
                BaseExceptionGroup(
                    "Side cleanup failed",
                    [error, unsubscribe_error],
                )
                if error is not None
                else unsubscribe_error
            )
            outcome = SideLifecycleOutcome(
                side_id=session.side_id,
                state=SideTopicState.OPEN,
                error=combined,
            )
            await self._deliver_side_lifecycle(outcome)
            return outcome

        final_state = state if error is None else SideTopicState.FAILED
        try:
            record = self._bindings.transition_side_topic(
                session.side_id,
                final_state,
            )
            final_state = record.state
        except SideTopicConflict:
            record = self._bindings.get_side_topic(session.side_id)
            final_state = record.state
        except Exception as caught:
            persistence_error = (
                BaseExceptionGroup(
                    "Side persistence cleanup failed",
                    [error, caught],
                )
                if error is not None
                else caught
            )
            outcome = SideLifecycleOutcome(
                side_id=session.side_id,
                state=SideTopicState.OPEN,
                error=persistence_error,
            )
            await self._deliver_side_lifecycle(outcome)
            return outcome

        task_to_cancel: asyncio.Task[None] | None = None
        async with self._side_lock(session.side_id):
            if self._sides.get(session.side_id) is session:
                self._sides.pop(session.side_id, None)
                active = session.active
                if active is not None:
                    active.receipt_attempted.set()
                    task_to_cancel = active.task
                session.active = None
            self._side_locks.pop(session.side_id, None)
        if (
            task_to_cancel is not None
            and task_to_cancel is not asyncio.current_task()
            and not task_to_cancel.done()
        ):
            task_to_cancel.cancel()
            await asyncio.gather(task_to_cancel, return_exceptions=True)

        outcome = SideLifecycleOutcome(
            side_id=session.side_id,
            state=final_state,
            error=error,
        )
        await self._deliver_side_lifecycle(outcome)
        return outcome

    async def _deliver_side_lifecycle(
        self,
        outcome: SideLifecycleOutcome,
    ) -> None:
        try:
            await self._on_completion(outcome)
        except Exception:
            logger.exception(
                "Side lifecycle delivery failed",
                extra={"side_id": outcome.side_id},
            )

    @staticmethod
    def _redeem_side_admission(
        session: _SideSession,
        active: _ActiveSideTurn | None,
        admission: SideSubmissionAdmission,
    ) -> None:
        actual_turn_id = active.handle.id if active is not None else None
        if (
            admission.revision != session.revision
            or admission.thread_id != session.thread.id
            or admission.turn_id != actual_turn_id
        ):
            raise SteerRace(
                "准备本条消息期间 Side Turn 状态已变化，本条消息未执行，请重新发送。"
            )
