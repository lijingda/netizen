"""Native context compaction and its terminal observation."""

from __future__ import annotations

import asyncio
import logging


from ..bindings import (
    ThreadBinding,
)
from ..error_messages import describe_error, native_turn_failure
from .contracts import (
    NativeThread,
    RuntimeClosed,
    ThreadOccupied,
    ThreadRunningConfiguration,
    ThreadCompacting,
    ThreadNotMaterialized,
    ThreadCompactStartFailed,
    CompactionFailed,
    CompactionStateUnknown,
    CompactSubmission,
    CompactionOutcome,
)

from . import limits
from .native_values import (
    _enum_value,
    _is_transient_thread_read_error,
    _thread_status_type,
    _turn_contains_item,
)
from .state import (
    RUNTIME_LOGGER_NAME,
    _ActiveCompaction,
)
from .turn_control import TurnControlRuntime


logger = logging.getLogger(RUNTIME_LOGGER_NAME)


class CompactionRuntime(TurnControlRuntime):
    """Native context compaction and its terminal observation."""

    async def compact(
        self,
        *,
        binding: ThreadBinding,
        owner_id: str,
        origin: object,
    ) -> CompactSubmission:
        """Start native compaction and observe its exact terminal Turn.

        The pinned public ``compact()`` method returns immediately.  Keep the
        Binding reserved until public ``thread.read(include_turns=True)``
        exposes a new terminal Turn containing a ``contextCompaction`` item.
        """

        if not self._accepting:
            raise RuntimeClosed("服务正在停止，暂不能压缩会话。")
        async with self._lock(binding.id):
            if not self._accepting:
                raise RuntimeClosed("服务正在停止，暂不能压缩会话。")
            self._guard_no_lifecycle_locked(binding.id)
            if binding.id in self._compacting:
                raise ThreadCompacting("当前会话已经在压缩上下文。")
            binding = self._bindings.get(binding.id)
            await self._guard_no_goal_locked(binding)
            if self._active.get(binding.id) is not None:
                raise ThreadRunningConfiguration(
                    "当前 Turn 正在执行，不能压缩上下文；"
                    "请等待完成或先发送 /stop。"
                )

            binding = self._bindings.get(binding.id)
            if binding.native_thread_id is None:
                raise ThreadNotMaterialized(
                    "当前会话尚未在 Codex 中创建；"
                    "请先发送一条真实任务，再使用 /compact。"
                )
            try:
                thread = await self._resume_thread_for_operation(
                    binding.native_thread_id
                )
                if thread.id != binding.native_thread_id:
                    raise RuntimeError(
                        "thread_resume returned a different native ID"
                    )
                record = self._mark_thread_subscribed_locked(binding, thread)
                self._schedule_subscription_release_locked(record)
            except ThreadOccupied:
                raise
            except asyncio.CancelledError:
                self.close_admission()
                raise
            except Exception as error:
                self.close_admission()
                raise ThreadCompactStartFailed(
                    "Codex Thread 恢复结果未确认；"
                    "服务已停止接收新任务，请重启服务。"
                ) from error

            try:
                baseline = await self._read_compaction_baseline(thread)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                raise ThreadCompactStartFailed(
                    "无法读取 Codex 会话，未开始压缩；请重试。"
                ) from error
            status = _thread_status_type(baseline)
            if status != "idle":
                if status == "active":
                    raise ThreadRunningConfiguration(
                        "Codex 会话当前正在执行其他任务，不能压缩；"
                        "请等待其完成。"
                    )
                raise ThreadCompactStartFailed(
                    f"Codex 会话状态异常：{status!r}，未开始压缩。"
                )
            before_turn_ids = frozenset(
                str(turn_id)
                for turn in getattr(baseline, "turns", ())
                if (turn_id := getattr(turn, "id", None))
            )
            if not before_turn_ids:
                raise ThreadNotMaterialized("当前会话还没有可压缩的 Codex 任务。")

            receipt_attempted = asyncio.Event()
            active = _ActiveCompaction(
                binding_id=binding.id,
                thread=thread,
                owner_id=owner_id,
                origin=origin,
                before_turn_ids=before_turn_ids,
                receipt_attempted=receipt_attempted,
            )
            self._advance_admission_revision(binding.id)
            self._compacting[binding.id] = active
            self._invalidate_context_window_usage(binding.id)
            try:
                await thread.compact()
            except asyncio.CancelledError:
                receipt_attempted.set()
                self.close_admission()
                raise
            except Exception as error:
                # The request is side-effectful and its response is only an
                # acknowledgement.  A lost response cannot prove compaction
                # did not start, so retain the Binding reservation and fail
                # the process-wide admission boundary closed.
                receipt_attempted.set()
                self.close_admission()
                raise ThreadCompactStartFailed(
                    "Codex 压缩请求结果未确认；服务已停止接收新任务，"
                    "请重启服务。"
                ) from error
            self._track_compaction(active)
            return CompactSubmission(
                binding_id=binding.id,
                thread_id=thread.id,
                release_receipt_attempt=receipt_attempted.set,
            )

    async def _read_compaction_baseline(self, thread: NativeThread) -> object:
        # No compact mutation has occurred. Bound this read-only preparation
        # so an unavailable baseline cannot retain the Binding lock forever.
        async with asyncio.timeout(limits.COMPACTION_BASELINE_TIMEOUT_SECONDS):
            for attempt in range(limits.COMPACTION_BASELINE_MAX_READS):
                try:
                    response = await thread.read(include_turns=True)
                    native_thread = getattr(response, "thread", None)
                except Exception as error:
                    if not _is_transient_thread_read_error(
                        error,
                        thread_id=thread.id,
                        include_turns=True,
                    ):
                        raise
                    logger.warning(
                        "native Thread unavailable before compaction",
                        extra={
                            "thread_id": thread.id,
                            "attempt": attempt + 1,
                            "error_type": type(error).__name__,
                        },
                    )
                else:
                    if _thread_status_type(native_thread) != "notLoaded":
                        return native_thread
                if attempt + 1 < limits.COMPACTION_BASELINE_MAX_READS:
                    await asyncio.sleep(self._poll_interval_seconds)
        raise TimeoutError("native Thread baseline remained unavailable before compaction")

    async def _consume_compaction(self, active: _ActiveCompaction) -> None:
        error: BaseException | None = None
        retain_active = False
        try:
            async with asyncio.timeout(self._compaction_timeout_seconds):
                await self._read_compaction_terminal(active)
        except asyncio.CancelledError:
            raise
        except BaseException as caught:
            if active.terminal_observed:
                error = caught
            else:
                self.close_admission()
                retain_active = True
                error = CompactionStateUnknown(
                    "Codex 压缩终态未确认；服务已停止接收新任务，"
                    "请重启服务后再继续。"
                )
                logger.error(
                    "native compaction terminal state is unknown; admission closed",
                    exc_info=(type(caught), caught, caught.__traceback__),
                    extra={"thread_id": active.thread.id},
                )
        finally:
            if not retain_active:
                async with self._lock(active.binding_id):
                    if self._compacting.get(active.binding_id) is active:
                        self._compacting.pop(active.binding_id, None)
                        self._advance_admission_revision(active.binding_id)
                        self._schedule_known_subscription_locked(
                            active.binding_id,
                            active.thread.id,
                        )

        await active.receipt_attempted.wait()
        outcome = CompactionOutcome(
            binding_id=active.binding_id,
            thread_id=active.thread.id,
            owner_id=active.owner_id,
            origin=active.origin,
            compact_turn_id=active.compact_turn_id,
            status=active.status,
            error=error,
        )
        try:
            await self._on_completion(outcome)
        except Exception:
            logger.exception(
                "compaction completion delivery failed",
                extra={"thread_id": active.thread.id},
            )

    async def _read_compaction_terminal(
        self,
        active: _ActiveCompaction,
    ) -> None:
        def candidate(native_thread: object) -> object | None:
            if getattr(native_thread, "id", None) != active.thread.id:
                raise RuntimeError("compaction read returned a different Thread")
            new_turns = [
                turn
                for turn in getattr(native_thread, "turns", ())
                if getattr(turn, "id", None) not in active.before_turn_ids
            ]
            candidates = [
                turn
                for turn in new_turns
                if _turn_contains_item(turn, "contextCompaction")
            ]
            if len(new_turns) > 1 or len(candidates) > 1:
                # compact() returns no Turn ID. Concurrent writers on the same
                # native Thread therefore make request attribution impossible
                # through the pinned public facade; never select an arbitrary
                # candidate and report it as this request's success.
                raise RuntimeError(
                    "multiple native Turns appeared after compaction baseline"
                )
            return candidates[0] if candidates else None

        failures = 0
        while True:
            native_thread, failures = await self._read_compaction_thread(
                active,
                include_turns=False,
                failures=failures,
            )
            thread_status = _thread_status_type(native_thread)
            if thread_status == "systemError":
                raise RuntimeError(
                    "native Thread entered systemError during compaction"
                )
            if thread_status not in {"notLoaded", "active", "idle"}:
                raise RuntimeError(
                    f"unexpected native Thread status: {thread_status!r}"
                )
            if thread_status != "idle":
                await asyncio.sleep(self._poll_interval_seconds)
                continue

            native_thread, failures = await self._read_compaction_thread(
                active,
                include_turns=True,
                failures=failures,
            )
            thread_status = _thread_status_type(native_thread)
            if thread_status in {"notLoaded", "active"}:
                await asyncio.sleep(self._poll_interval_seconds)
                continue
            if thread_status == "systemError":
                raise RuntimeError(
                    "native Thread entered systemError during compaction"
                )
            if thread_status != "idle":
                raise RuntimeError(
                    f"unexpected native Thread status: {thread_status!r}"
                )

            compact_turn = candidate(native_thread)
            if compact_turn is None:
                # The start acknowledgement can race ahead of native status:
                # the pinned live probe observes idle -> active -> idle.  An
                # idle read without a new contextCompaction Turn is therefore
                # not completion evidence.
                await asyncio.sleep(self._poll_interval_seconds * 4)
                continue
            compact_turn_id = getattr(compact_turn, "id", None)
            active.compact_turn_id = (
                compact_turn_id if isinstance(compact_turn_id, str) else None
            )
            status = _enum_value(getattr(compact_turn, "status", None))
            if status == "interrupted":
                await asyncio.sleep(limits.INTERRUPTION_CONFIRMATION_SECONDS)
                native_thread, failures = await self._read_compaction_thread(
                    active, include_turns=True, failures=failures,
                )
                confirmed = candidate(native_thread)
                if confirmed is None or getattr(confirmed, "id", None) != compact_turn_id:
                    raise RuntimeError("compaction interruption confirmation lost the exact Turn")
                thread_status = _thread_status_type(native_thread)
                if thread_status in {"notLoaded", "active"}:
                    continue
                if thread_status != "idle":
                    raise RuntimeError(f"unexpected compaction confirmation status: {thread_status!r}")
                compact_turn = confirmed
                status = _enum_value(getattr(compact_turn, "status", None))
            active.status = status
            if status == "inProgress":
                await asyncio.sleep(self._poll_interval_seconds * 4)
                continue
            if status not in {"completed", "interrupted", "failed"}:
                raise RuntimeError(
                    f"unexpected native compaction status: {status!r}"
                )
            active.terminal_observed = True
            if status == "failed":
                native_error = getattr(compact_turn, "error", None)
                message = getattr(native_error, "message", None)
                raise CompactionFailed(
                    message
                    if isinstance(message, str) and message
                    else "Codex 上下文压缩失败。"
                )
            if status == "interrupted":
                message = "Codex 上下文压缩被中断。"
                native_error = getattr(compact_turn, "error", None)
                if native_error is not None:
                    message += f"中断原因：{describe_error(native_turn_failure(native_error))}"
                raise CompactionFailed(message)
            return

    async def _read_compaction_thread(
        self,
        active: _ActiveCompaction,
        *,
        include_turns: bool,
        failures: int,
    ) -> tuple[object, int]:
        while True:
            try:
                response = await active.thread.read(include_turns=include_turns)
                return getattr(response, "thread", None), failures
            except Exception as error:
                if not _is_transient_thread_read_error(
                    error,
                    thread_id=active.thread.id,
                    include_turns=include_turns,
                ):
                    raise
                failures += 1
                if failures == 1 or failures % 120 == 0:
                    logger.warning(
                        "native compaction read unavailable; retrying",
                        extra={
                            "thread_id": active.thread.id,
                            "failures": failures,
                            "error_type": type(error).__name__,
                        },
                    )
                await asyncio.sleep(self._poll_interval_seconds)

    def _track_compaction(self, active: _ActiveCompaction) -> None:
        task = asyncio.create_task(
            self._consume_compaction(active),
            name=f"codex-compact:{active.thread.id}",
        )
        active.task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
