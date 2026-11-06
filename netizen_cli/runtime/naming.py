"""Manual and automatic native Thread naming."""

from __future__ import annotations

import asyncio
import logging

from openai_codex import (
    ExternalMessage,
)

from .state import RUNTIME_LOGGER_NAME
from ..bindings import (
    ProjectDisabled,
    ThreadBinding,
)
from ..model_settings import ModelCatalogError, TurnModelSettings
from .name_writes import ThreadNameWrites
from .thread_naming import NamingJob, ThreadNameGenerationError
from .contracts import (
    NativeThread,
    RuntimeClosed,
    ThreadOccupied,
    ThreadNotMaterialized,
    ThreadLifecycleError,
    ThreadLifecycleStateUnknown,
    ThreadLifecycleState,
)

from . import limits
from .lifecycle import ThreadLifecycleRuntime


logger = logging.getLogger(RUNTIME_LOGGER_NAME)


class ThreadNamingRuntime(ThreadLifecycleRuntime):
    """Manual and automatic native Thread naming."""

    def _init_naming_state(self, *, automatic_thread_naming: bool) -> None:
        self._name_writes = ThreadNameWrites()
        self._automatic_thread_naming = automatic_thread_naming

    async def generate_name_exact(self, binding_id: str) -> str:
        """Generate a candidate from the exact Thread without resuming or renaming it."""
        if self._thread_namer is None:
            raise ThreadLifecycleError("当前实例的名称生成能力不可用。")
        async with self._lock(binding_id):
            binding = self._require_name_generation_binding(binding_id)
            if binding_id in self._name_generation_requests or self._thread_namer.has_pending(binding_id):
                raise ThreadLifecycleError("上一次名称生成仍在执行或收尾，请稍后重试。")
            invalidated = asyncio.Event()
            self._name_generation_requests[binding_id] = invalidated
        try:
            settings = None
            if binding.turn_settings is not None:
                try:
                    async with asyncio.timeout(limits.NAMING_PREPARATION_TIMEOUT_SECONDS):
                        settings = await self.resolve_model_settings(
                            model_id=binding.turn_settings.model_id,
                            effort_id=binding.turn_settings.effort_id,
                            service_tier_id=binding.turn_settings.service_tier_id,
                        )
                except TimeoutError as error:
                    raise ThreadLifecycleError("读取名称生成的模型配置超时，请重试。") from error
                except ModelCatalogError as error:
                    raise ThreadLifecycleError(str(error)) from error
                except Exception as error:
                    raise ThreadLifecycleError("无法读取名称生成的模型配置，请重试。") from error
            async with self._lock(binding_id):
                current = self._require_name_generation_binding(binding_id)
                if (
                    current.native_thread_id != binding.native_thread_id
                    or current.settings_revision != binding.settings_revision
                    or invalidated.is_set()
                ):
                    raise ThreadLifecycleError("会话身份或模型配置已变化，请重新生成名称。")
            # Constructing this public handle does no I/O and does not resume
            # or subscribe the parent. generate() reserves its native owner
            # before yielding; every RPC runs outside the Binding lock.
            assert binding.native_thread_id is not None
            parent = self._thread_handle(binding.native_thread_id)
            try:
                name = await self._thread_namer.generate(binding_id, parent, settings)
            except ThreadNameGenerationError as error:
                raise ThreadLifecycleError(str(error)) from error
            async with self._lock(binding_id):
                current = self._require_name_generation_binding(binding_id)
                if current.native_thread_id != binding.native_thread_id or invalidated.is_set():
                    raise ThreadLifecycleError("会话状态已变化，本次生成结果已失效。")
            return name
        finally:
            if self._name_generation_requests.get(binding_id) is invalidated:
                self._name_generation_requests.pop(binding_id)

    def _require_name_generation_binding(self, binding_id: str) -> ThreadBinding:
        if not self._accepting:
            raise RuntimeClosed("服务正在停止，暂不能生成名称。")
        self._guard_no_lifecycle_locked(binding_id)
        binding = self._bindings.get(binding_id)
        self._bindings.require_project_not_deleting(binding.project_alias)
        if binding.native_thread_id is None:
            raise ThreadNotMaterialized("当前会话尚未在 Codex 中创建，请先发送一条真实任务。")
        return binding

    async def rename_exact(
        self,
        binding_id: str,
        name: str,
    ) -> str:
        normalized = " ".join(name.split())
        if not normalized or len(normalized) > 120:
            raise ValueError("会话名称必须为 1 到 120 个字符。")
        if not self._accepting:
            raise RuntimeClosed("服务正在停止，暂不能重命名会话。")

        result = await self._name_writes.write(
            binding_id,
            lambda: self._rename_exact_serialized(binding_id, normalized),
            wait=True,
        )
        assert result is not None
        return result

    async def _rename_exact_serialized(
        self, binding_id: str, normalized: str,
    ) -> str:
        async with self._lock(binding_id):
            if not self._accepting:
                raise RuntimeClosed("服务正在停止，暂不能重命名会话。")
            self._guard_no_lifecycle_locked(binding_id)
            binding = self._bindings.get(binding_id)
            if binding.native_thread_id is None:
                raise ThreadNotMaterialized(
                    "当前会话尚未在 Codex 中创建；"
                    "请先发送一条真实任务，再使用 /rename。"
                )
            operation = self._begin_lifecycle_locked(
                binding,
                ThreadLifecycleState.RENAMING,
            )
            mutation_attempted = False
            try:
                thread = await self._lifecycle_thread_locked(binding)
                mutation_attempted = True
                await thread.set_name(normalized)
            except ThreadOccupied:
                self._finish_lifecycle_locked(operation)
                raise
            except asyncio.CancelledError:
                if mutation_attempted:
                    self._retain_unknown_lifecycle(operation)
                else:
                    self._finish_lifecycle_locked(operation)
                raise
            except Exception as error:
                if mutation_attempted:
                    self._retain_unknown_lifecycle(operation)
                    raise ThreadLifecycleStateUnknown(
                        "Codex 会话重命名结果未确认；当前会话"
                        "的生命周期状态待对账。"
                    ) from error
                self._finish_lifecycle_locked(operation)
                self._schedule_known_subscription_locked(
                    binding.id,
                    binding.native_thread_id,
                )
                raise ThreadLifecycleError(
                    "无法打开当前 Codex 会话，未执行重命名。"
                ) from error
            self._finish_lifecycle_locked(operation)
            self._schedule_known_subscription_locked(
                binding.id,
                binding.native_thread_id,
            )
            return normalized

    def _start_automatic_name(
        self,
        binding_id: str,
        thread: NativeThread,
        turn_id: str,
        settings: TurnModelSettings | None = None,
        *,
        preferred_title: str | None = None,
        external_input: ExternalMessage | None = None,
    ) -> None:
        if self._thread_namer is None or not self._automatic_thread_naming or not self._accepting:
            return
        if binding_id in self._name_generation_requests:
            return
        try:
            self._thread_namer.start(binding_id, thread, turn_id, settings,
                                     preferred_title=preferred_title,
                                     external_input=external_input)
        except Exception as diagnostic_error:
            # Auxiliary setup must never turn an accepted user Turn into an
            # apparent submission failure. Do not log user content or titles.
            logger.warning(
                "automatic naming setup failed",
                extra={
                    "binding_id": binding_id,
                    "thread_id": thread.id,
                    "turn_id": turn_id,
                    "error_type": type(diagnostic_error).__name__,
                },
            )

    def _automatic_name_is_current(self, job: NamingJob) -> bool:
        if (
            not self._accepting
            or self._thread_namer is None
            or not self._thread_namer.is_current(job)
            or job.binding_id in self._lifecycles
        ):
            return False
        try:
            binding = self._bindings.get(job.binding_id)
            self._bindings.require_project_not_deleting(binding.project_alias)
            project = self._bindings.get_project(binding.project_alias)
        except (LookupError, ProjectDisabled):
            return False
        # A Scope pointer change is deliberately irrelevant: the result belongs
        # to the original exact Thread, even when it is no longer current.
        return project.enabled and binding.native_thread_id == job.parent.id

    async def _commit_automatic_thread_name(self, job: NamingJob, name: str) -> None:
        await self._name_writes.write(
            job.binding_id,
            lambda: self._write_automatic_thread_name(job, name),
            wait=False,
        )

    async def _write_automatic_thread_name(self, job: NamingJob, name: str) -> None:
        if not self._automatic_name_is_current(job):
            return
        # Read/check/write all run under the shared name lock. The background
        # writer never waits behind an existing manual or automatic writer.
        response = await job.parent.read(include_turns=False)
        native = getattr(response, "thread", None)
        current_name = getattr(native, "name", None)
        if (
            getattr(native, "id", None) != job.parent.id
            or not hasattr(native, "name")
            or (current_name is not None and not isinstance(current_name, str))
            or (isinstance(current_name, str) and current_name.strip())
        ):
            return
        try:
            async with self._lock(job.binding_id):
                if not self._automatic_name_is_current(job):
                    return
            # No Binding/Scope lock, lifecycle reservation, admission
            # revision, ordinary task or user-visible result is involved.
            await job.parent.set_name(name)
        except Exception as diagnostic_error:
            logger.warning(
                "automatic name write unconfirmed",
                extra={
                    "binding_id": job.binding_id,
                    "thread_id": job.parent.id,
                    "turn_id": job.turn_id,
                    "error_type": type(diagnostic_error).__name__,
                },
            )
