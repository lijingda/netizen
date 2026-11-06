"""Persistent fork creation and topic-creation tracking."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from openai_codex import (
    InvalidRequestError,
)

from .state import RUNTIME_LOGGER_NAME
from ..bindings import (
    ProjectDisabled,
    ThreadBinding,
)
from ..domain import (
    ActiveState,
)
from ..error_messages import describe_error
from .contracts import (
    NativeThread,
    RuntimeClosed,
    ThreadStopping,
    ThreadRunningConfiguration,
    ThreadCompacting,
    ThreadNotMaterialized,
    SteerRace,
    ThreadLifecycleError,
    TurnObservationUnavailable,
)

from . import limits
from .native_values import (
    _thread_status_type,
)
from .occupancy import BindingOccupancyRuntime


logger = logging.getLogger(RUNTIME_LOGGER_NAME)


class ForkRuntime(BindingOccupancyRuntime):
    """Persistent fork creation and topic-creation tracking."""

    @asynccontextmanager
    async def track_topic_creation(self, project_alias: str) -> AsyncIterator[None]:
        """Own an ordinary topic creation through its final Binding handoff."""

        task = asyncio.current_task()
        if task is None or task in self._fork_creations:
            raise RuntimeError("topic creation must have one non-nested owning task")
        self._fork_creations[task] = project_alias
        already_tracked = task in self._tasks
        self._tasks.add(task)
        try:
            self.require_topic_creation_open(project_alias)
            yield
        finally:
            self._fork_creations.pop(task, None)
            if not already_tracked:
                self._tasks.discard(task)

    def project_has_fork_creation(self, project_alias: str) -> bool:
        return project_alias in self._fork_creations.values()

    @asynccontextmanager
    async def track_fork_creation(self, project_alias: str) -> AsyncIterator[None]:
        """Compatibility name for the shared ordinary-topic creation owner."""

        async with self.track_topic_creation(project_alias):
            yield

    def require_fork_creation_open(self, project_alias: str) -> None:
        self.require_topic_creation_open(project_alias)

    def require_topic_creation_open(self, project_alias: str) -> None:
        """Fence native creation and the final synchronous Binding commit."""

        if not self._accepting:
            raise RuntimeClosed("服务正在停止，本次会话创建未完成。")
        if self._fork_creations.get(asyncio.current_task()) != project_alias:
            raise RuntimeError("topic creation handoff is not owned by this task")
        self._bindings.require_project_not_deleting(project_alias)

    def _require_fork_source_locked(
        self, source: ThreadBinding, expected_project_revision: int,
    ) -> ThreadBinding:
        self.require_fork_creation_open(source.project_alias)
        self._bindings.worktrees.require_binding_not_deleting(source.id)
        self._guard_no_lifecycle_locked(source.id)
        current = self._bindings.get(source.id)
        if (
            not current.active
            or current.scope_key != source.scope_key
            or current.project_alias != source.project_alias
            or current.native_thread_id != source.native_thread_id
            or current.settings_revision != source.settings_revision
            or current.context_revision != source.context_revision
            or current.feedback_revision != source.feedback_revision
        ):
            raise SteerRace("来源会话或配置已变化，请重新发送 /fork。")
        if current.native_thread_id is None:
            raise ThreadNotMaterialized(
                "当前会话尚无 Codex 上下文；请先发送一条任务，再使用 /fork。"
            )
        project = self._bindings.get_project(current.project_alias)
        if not project.enabled:
            raise ProjectDisabled("Project 已停用，不能创建分支。")
        if project.revision != expected_project_revision:
            raise SteerRace("Project 已变化，请重新发送 /fork。")
        if current.id in self._compacting:
            raise ThreadCompacting("来源会话正在压缩上下文，完成前不能创建分支。")
        if current.id in self._goals:
            raise self._goal_slot_error(self._goals[current.id])
        active = self._active.get(current.id)
        if active is not None:
            if active.state is ActiveState.STOPPING:
                raise ThreadStopping("来源 Turn 正在停止，完成后才能创建分支。")
            if active.state is ActiveState.OBSERVATION_UNAVAILABLE:
                raise TurnObservationUnavailable("来源 Turn 观测不可用，暂不能创建分支。")
            raise ThreadRunningConfiguration("来源会话正在执行，空闲后才能创建分支。")
        return current

    async def fork_exact(
        self, source: ThreadBinding, *, expected_project_revision: int,
    ) -> NativeThread:
        """Take one persistent native snapshot without loading the source."""

        async with self._lock(source.id):
            current = self._require_fork_source_locked(source, expected_project_revision)
            await self._guard_no_goal_locked(current)
            response = await self._thread_handle(current.native_thread_id).read(
                include_turns=False,
            )
            native = getattr(response, "thread", None)
            if (
                getattr(native, "id", None) != current.native_thread_id
                or getattr(native, "ephemeral", None) is not False
            ):
                raise ThreadLifecycleError("无法确认来源 Codex 会话身份，本次未创建分支。")
            if _thread_status_type(native) not in {"idle", "notLoaded"}:
                raise ThreadRunningConfiguration(
                    "来源 Codex 会话尚未确认空闲，本次未创建分支。"
                )
            # Source settings and Project admission may change during the
            # read-only awaits. No new native request follows a stale snapshot.
            current = self._require_fork_source_locked(source, expected_project_revision)
            try:
                thread = await self._codex.thread_fork(
                    current.native_thread_id, ephemeral=False, include_turns=False,
                )
            except asyncio.CancelledError:
                raise
            except InvalidRequestError as error:
                raise ThreadLifecycleError(
                    f"Codex 拒绝创建分支：{describe_error(error)}"
                ) from error
            except Exception as error:
                raise ThreadLifecycleError(
                    "Codex 分支创建结果未确认；本次不自动重试，可能需人工核查。"
                ) from error
            if (
                not isinstance(getattr(thread, "id", None), str)
                or not thread.id
                or thread.id == current.native_thread_id
            ):
                raise ThreadLifecycleError(
                    "Codex 返回的分支标识无效；创建结果未确认，不能继续绑定。"
                )
            # Preserve the known ID for the caller's partial-failure feedback.
            # It rechecks admission before publishing/committing, and owns
            # releasing this exact handle if that handoff cannot complete.
            return thread

    async def adopt_fork(
        self, binding: ThreadBinding, thread: NativeThread, *, name: str | None = None,
    ) -> bool:
        """Register and optionally name the returned handle, without resuming it."""

        async with self._lock(binding.id):
            self.require_fork_creation_open(binding.project_alias)
            current = self._bindings.get(binding.id)
            if (
                current.native_thread_id != thread.id
                or current.native_thread_id != binding.native_thread_id
                or current.scope_key != binding.scope_key
                or current.project_alias != binding.project_alias
            ):
                raise SteerRace("分支绑定身份已变化，不能登记 Codex 订阅。")
            record = self._mark_thread_subscribed_locked(current, thread)
            try:
                if name is not None:
                    normalized = " ".join(name.split())
                    if not normalized or len(normalized) > 120:
                        raise ValueError("会话名称必须为 1 到 120 个字符。")
                    await thread.set_name(normalized)
                return True
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "persistent fork name write was not confirmed",
                    extra={"binding_id": binding.id, "thread_id": thread.id},
                )
                return False
            finally:
                self._schedule_subscription_release_locked(record)

    async def release_unbound_fork(self, thread: NativeThread) -> bool:
        """Best-effort release of a known orphan handle, never delete or retry."""

        control = self._thread_subscription_control
        if control is None:
            return False
        try:
            async with asyncio.timeout(limits.FORK_RELEASE_TIMEOUT_SECONDS):
                await control.unsubscribe(thread.id)
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "unbound persistent fork subscription release was not confirmed",
                exc_info=True, extra={"thread_id": thread.id},
            )
            return False
