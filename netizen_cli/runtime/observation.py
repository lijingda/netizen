"""Ordinary Turn consumption, bounded observation recovery and terminal reads."""

from __future__ import annotations

import asyncio
import logging

from openai_codex import (
    InternalRpcError,
    InvalidRequestError,
    TurnResult,
)
from openai_codex.types import ThreadTokenUsageUpdatedNotification

from ..domain import (
    ActiveState,
)
from ..error_messages import describe_error, native_turn_failure
from .contracts import (
    TerminalCleanupFailed,
    TurnInterruptFailed,
    TerminalStateUnknown,
    StopDisposition,
    TurnActivitySnapshot,
    TurnOutcome,
    TurnObservationUnavailableOutcome,
)
from ..turn_patch_children import TaskPatchChildren, collect_turn_patch_children

from . import limits
from .native_values import (
    _enum_value,
    _context_window_usage_from_native,
    _is_recoverable_turn_observation_io_error,
    _is_paginated_turn_read_unavailable,
    _thread_status_type,
    _final_agent_response,
)
from .state import (
    RUNTIME_LOGGER_NAME,
    _TurnViewUnverified,
    _TurnResumeRequired,
    _ActiveTurn,
    _TurnObservation,
    _ContextWindowUsageSnapshot,
)
from .naming import ThreadNamingRuntime


logger = logging.getLogger(RUNTIME_LOGGER_NAME)


class TurnObservationRuntime(ThreadNamingRuntime):
    """Ordinary Turn consumption, bounded observation recovery and terminal reads."""

    def _signal_turn_rechecked(
        self, active: _ActiveTurn, *, previous_revision: int | None = None,
    ) -> None:
        ready = active.recheck_ready
        if ready is not None and not ready.is_set():
            # Only the observer's own locked transition may advance this token.
            # A stop/configuration/lifecycle change leaves it stale, even if
            # observation subsequently succeeds. Never adopt the latest state.
            if (
                previous_revision is not None
                and active.recheck_revision == previous_revision
            ):
                active.recheck_revision = self._admission_revision(active.binding_id)
            ready.set()

    async def _interrupt(self, binding_id: str, active: _ActiveTurn) -> None:
        async with self._lock(binding_id):
            if self._active.get(binding_id) is not active:
                return
            if (
                active.terminal_observed
                and not active.interrupt_attempted
                and not active.cleanup_required
            ):
                return
            await self._interrupt_and_clean(active)

    async def _interrupt_and_clean(self, active: _ActiveTurn) -> StopDisposition:
        self._mark_stopping(active)
        active.cleanup_required = True
        if active.interrupt_succeeded:
            if active.cleanup_succeeded:
                return StopDisposition.STOPPING
            return await self._clean_stopping(active)
        retrying_unknown_interrupt = active.interrupt_attempted
        active.interrupt_attempted = True
        try:
            await active.handle.interrupt()
        except Exception as error:
            if active.terminal_observed and retrying_unknown_interrupt:
                # We retried the unknown interrupt as promised. Some native
                # implementations reject interrupt once the exact Turn is
                # already terminal; exact-Thread cleanup is still required to
                # remove any terminal child left behind by that Turn.
                logger.warning(
                    "repeat interrupt failed after exact terminal; "
                    "continuing exact Thread cleanup",
                    exc_info=True,
                    extra={
                        "thread_id": active.handle.thread_id,
                        "turn_id": active.handle.id,
                    },
                )
                return await self._clean_stopping(active)
            raise TurnInterruptFailed(
                "Codex Turn 中断结果未确认；当前会话保持 stopping。"
                "请再次发送 /stop 重试中断。"
            ) from error
        active.interrupt_succeeded = True
        return await self._clean_stopping(active)

    async def _clean_stopping(self, active: _ActiveTurn) -> StopDisposition:
        try:
            if active.handle.thread_id != active.thread.id:
                raise RuntimeError("native Turn/Thread identity mismatch")
            binding = self._bindings.get(active.binding_id)
            if binding.native_thread_id != active.thread.id:
                raise RuntimeError("native Thread is not owned by this Binding")
            await self._terminal_cleanup.clean_thread(active.thread.id)
        except Exception as error:
            raise TerminalCleanupFailed(
                "Codex Turn 已请求中断，但已登记后台终端的清理请求失败；"
                "当前会话保持 stopping，不能假定后台终端已经停止。"
                "请再次发送 /stop 重试清理。"
            ) from error
        active.cleanup_succeeded = True
        active.cleanup_ready.set()
        return StopDisposition.REQUESTED

    async def _completion_patch_children(
        self, thread_id: str, turn_id: str, items: tuple[object, ...],
    ) -> TaskPatchChildren:
        # Completion-only, bounded, public reads. Display evidence never changes
        # native terminal authority, admission, or the notification consumer.
        try:
            return await collect_turn_patch_children(
                self._codex, thread_id=thread_id, turn_id=turn_id, items=items,
            )
        except Exception:
            logger.warning("child patch evidence unavailable", exc_info=True)
            return TaskPatchChildren(complete=False)

    async def _drain_terminal_turn_stream(self, active: _ActiveTurn) -> bool:
        observed_usage = False
        try:
            # This handle is the sole consumer of its retained notifications.
            # The SDK retains notifications from the start request, including
            # immediate completions. Drain only after persisted terminal proof;
            # this metadata stream never supplies native terminal authority.
            async for notification in active.handle.stream():
                payload = getattr(notification, "payload", None)
                if getattr(notification, "method", None) == "turn/diff/updated":
                    diff_thread_id = getattr(payload, "thread_id", None)
                    diff_turn_id = getattr(payload, "turn_id", None)
                    diff = getattr(payload, "diff", None)
                    if (
                        diff_thread_id != active.handle.thread_id
                        or diff_turn_id != active.handle.id
                    ):
                        logger.warning(
                            "native Turn diff notification identity mismatch",
                            extra={
                                "thread_id": active.handle.thread_id,
                                "turn_id": active.handle.id,
                                "diff_thread_id": diff_thread_id,
                                "diff_turn_id": diff_turn_id,
                            },
                        )
                    elif not isinstance(diff, str):
                        logger.warning(
                            "native Turn diff notification has an invalid shape",
                            extra={
                                "thread_id": active.handle.thread_id,
                                "turn_id": active.handle.id,
                            },
                        )
                    else:
                        # App Server documents this event as the latest
                        # aggregate snapshot, not an incremental delta.
                        active.latest_diff = diff
                    continue
                if not isinstance(payload, ThreadTokenUsageUpdatedNotification):
                    continue
                if (
                    payload.thread_id != active.handle.thread_id
                    or payload.turn_id != active.handle.id
                ):
                    logger.warning(
                        "native token usage notification identity mismatch",
                        extra={
                            "thread_id": active.handle.thread_id,
                            "turn_id": active.handle.id,
                            "usage_thread_id": payload.thread_id,
                            "usage_turn_id": payload.turn_id,
                        },
                    )
                    continue
                usage = _context_window_usage_from_native(payload.token_usage)
                if usage is None:
                    logger.warning(
                        "native token usage notification has an invalid shape",
                        extra={
                            "thread_id": active.handle.thread_id,
                            "turn_id": active.handle.id,
                        },
                    )
                    continue
                if self._active.get(active.binding_id) is active:
                    self._context_window_usage[active.binding_id] = (
                        _ContextWindowUsageSnapshot(
                            thread_id=active.handle.thread_id,
                            usage=usage,
                        )
                    )
                observed_usage = True
        except asyncio.CancelledError:
            raise
        except Exception:
            # Usage and aggregate diff are completion metadata. Persisted
            # thread.read remains the exact terminal-state source of truth
            # even when this public stream is unavailable.
            logger.warning(
                "native terminal Turn stream unavailable",
                exc_info=True,
                extra={
                    "thread_id": active.handle.thread_id,
                    "turn_id": active.handle.id,
                },
            )
        return observed_usage

    async def _consume(
        self,
        active: _ActiveTurn,
        *,
        recover_first: bool = False,
        notify_unavailable: bool = True,
    ) -> None:
        result: object | None = None
        error: BaseException | None = None
        unavailable_error: BaseException | None = None
        activity: TurnActivitySnapshot | None = None
        retain_active = False
        retired_turn = False
        try:
            observation: TurnResult | _TurnObservation | None = None
            if recover_first:
                try:
                    observation = await self._observe_with_bounded_recovery(
                        active,
                        _TurnViewUnverified("尚未取得上一轮任务状态"),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as caught:
                    if active.terminal_observed:
                        error = caught
                    else:
                        unavailable_error = caught
                        retain_active = await self._mark_turn_observation_unavailable(
                            active, caught,
                        )

            while error is None and unavailable_error is None:
                if not self._owns_turn_observation(active):
                    return
                self._refresh_turn_activity(active)
                if observation is None:
                    try:
                        observation = await self._read_terminal_result(active)
                    except asyncio.CancelledError:
                        raise
                    except _TurnViewUnverified as caught:
                        if active.terminal_observed:
                            error = caught
                            break
                        try:
                            observation = await self._observe_with_bounded_recovery(
                                active,
                                caught,
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as recovery_error:
                            if active.terminal_observed:
                                error = recovery_error
                            else:
                                unavailable_error = recovery_error
                                retain_active = (
                                    await self._mark_turn_observation_unavailable(
                                        active, recovery_error,
                                    )
                                )
                            break
                    except Exception as caught:
                        if active.terminal_observed:
                            error = caught
                        else:
                            unavailable_error = caught
                            retain_active = (
                                await self._mark_turn_observation_unavailable(
                                    active, caught,
                                )
                            )
                        break

                if isinstance(observation, TurnResult):
                    result = observation
                    if _enum_value(result.status) == "failed":
                        error = native_turn_failure(result.error)
                    break
                if observation is _TurnObservation.EXACT_IN_PROGRESS:
                    await self._mark_turn_observation_running(active)
                observation = None
                await asyncio.sleep(self._poll_interval_seconds)
        finally:
            try:
                self._refresh_turn_activity(active)
                if result is not None:
                    self._supplement_partial_answers(
                        active, thread_id=active.handle.thread_id, turn_id=active.handle.id,
                        items=tuple(getattr(result, "items", ())),
                    )
                activity = self._turn_activity_snapshot(active)
                if active.terminal_observed:
                    observed_usage = False
                    try:
                        async with asyncio.timeout(limits.TERMINAL_STREAM_DRAIN_TIMEOUT_SECONDS):
                            observed_usage = await self._drain_terminal_turn_stream(active)
                    except TimeoutError:
                        logger.warning(
                            "terminal Turn metadata stream timed out; delivering proven terminal: "
                            "thread_id=%s turn_id=%s",
                            active.handle.thread_id, active.handle.id,
                        )
                    finally:
                        if not observed_usage and self._active.get(active.binding_id) is active:
                            self._invalidate_context_window_usage(active.binding_id)
            finally:
                while not retain_active:
                    if self._active.get(active.binding_id) is not active:
                        break
                    async with self._lock(active.binding_id):
                        if self._active.get(active.binding_id) is not active:
                            break
                        if active.cleanup_required and not active.cleanup_succeeded:
                            wait_for_cleanup = True
                        else:
                            previous_revision = self._admission_revision(active.binding_id)
                            self._active.pop(active.binding_id, None)
                            self._advance_admission_revision(active.binding_id)
                            self._schedule_known_subscription_locked(
                                active.binding_id,
                                active.handle.thread_id,
                            )
                            self._signal_turn_rechecked(
                                active, previous_revision=previous_revision,
                            )
                            wait_for_cleanup = False
                    if not wait_for_cleanup:
                        break
                    await active.cleanup_ready.wait()
                self._signal_turn_rechecked(active)
                retired_turn = self._retiring_turns.pop(
                    (active.binding_id, active.handle.id), None,
                ) is active

        await active.receipt_attempted.wait()
        if unavailable_error is not None:
            if not notify_unavailable:
                return
            if not retired_turn and not await self._turn_unavailable_notice_is_current(active):
                return
            notice = TurnObservationUnavailableOutcome(
                binding_id=active.binding_id,
                thread_id=active.handle.thread_id,
                turn_id=active.handle.id,
                owner_id=active.owner_id,
                origin=active.origin,
                error=TerminalStateUnknown(
                    active.observation_error or describe_error(unavailable_error)
                ),
            )
            try:
                await self._on_completion(notice)
            except Exception:
                logger.exception(
                    "turn observation unavailable notice delivery failed",
                    extra={"turn_id": active.handle.id},
                )
            return

        outcome = TurnOutcome(
            binding_id=active.binding_id,
            thread_id=active.handle.thread_id,
            turn_id=active.handle.id,
            owner_id=active.owner_id,
            origin=active.origin,
            cwd=active.cwd,
            result=result,
            error=error,
            background_cleanup_requested=active.cleanup_succeeded,
            turn_diff=active.latest_diff,
            task_feedback=active.task_feedback,
            feedback_revision=active.feedback_revision,
            activity=activity,
            partial_answers=tuple(active.partial_answers.values()),
            patch_children=(
                await self._completion_patch_children(
                    active.handle.thread_id, active.handle.id,
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
                "turn completion delivery failed",
                extra={"turn_id": active.handle.id},
            )

    async def _observe_with_bounded_recovery(
        self,
        active: _ActiveTurn,
        initial_error: BaseException,
        *,
        interruption_pending: bool = False,
    ) -> TurnResult | _TurnObservation:
        """Try at most three native I/O operations within one five-second window."""

        last_error = initial_error
        resume_required = isinstance(initial_error, _TurnResumeRequired)
        resume_attempted = False
        io_count = 0
        terminal_turn: object | None = None
        try:
            async with asyncio.timeout(limits.TURN_OBSERVATION_RECOVERY_TIMEOUT_SECONDS):
                while io_count < limits.TURN_OBSERVATION_RECOVERY_MAX_IO:
                    if not self._owns_turn_observation(active):
                        raise asyncio.CancelledError
                    if resume_required and not resume_attempted:
                        io_count += 1
                        try:
                            thread = await self._codex.thread_resume(
                                active.handle.thread_id, include_turns=False
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as caught:
                            last_error = caught
                            break
                        if thread.id != active.handle.thread_id:
                            raise RuntimeError(
                                "thread_resume returned a different native Thread identity"
                            )
                        async with self._lock(active.binding_id):
                            if not self._owns_turn_observation(active):
                                raise asyncio.CancelledError
                            binding = self._bindings.get(active.binding_id)
                            if binding.native_thread_id != active.handle.thread_id:
                                raise RuntimeError(
                                    "Binding changed native Thread during Turn observation"
                                )
                            active.thread = thread
                            if self._active.get(active.binding_id) is active:
                                self._mark_thread_subscribed_locked(binding, thread)
                        resume_attempted = True
                        resume_required = False
                        if io_count >= limits.TURN_OBSERVATION_RECOVERY_MAX_IO:
                            break

                    io_count += 1
                    try:
                        native_thread = await self._read_native_thread(
                            active,
                            include_turns=True,
                        )
                        classified = self._classify_full_turn_view(
                            active,
                            native_thread,
                        )
                    except asyncio.CancelledError:
                        raise
                    except _TurnResumeRequired as caught:
                        last_error = caught
                        resume_required = not resume_attempted
                    except _TurnViewUnverified as caught:
                        last_error = caught
                    else:
                        if isinstance(classified, _TurnObservation):
                            if self._is_retiring_turn(active):
                                last_error = _TurnViewUnverified(
                                    "previous Turn is not terminal after native start/join advanced"
                                )
                                await asyncio.sleep(self._poll_interval_seconds)
                                continue
                            return classified
                        if (
                            _enum_value(getattr(classified, "status", None)) == "interrupted"
                            and not interruption_pending
                        ):
                            interruption_pending = True
                            if io_count >= limits.TURN_OBSERVATION_RECOVERY_MAX_IO:
                                break
                            await asyncio.sleep(limits.INTERRUPTION_CONFIRMATION_SECONDS)
                            continue
                        terminal_turn = classified
                        break
                    await asyncio.sleep(self._poll_interval_seconds)
        except TimeoutError as caught:
            # Keep the read failure that led to the timeout, not just an empty
            # TimeoutError that hides the only useful diagnostic information.
            caught.__cause__ = last_error
            last_error = caught

        if terminal_turn is not None:
            return await self._materialize_terminal_turn(active, terminal_turn)
        raise TerminalStateUnknown(
            "读取上一轮任务状态超时，仍无法确认是否结束"
            if isinstance(last_error, TimeoutError)
            else "exact Turn observation remained unavailable after bounded recovery"
        ) from last_error

    async def _mark_turn_observation_unavailable(
        self,
        active: _ActiveTurn,
        error: BaseException,
    ) -> bool:
        async with self._lock(active.binding_id):
            if self._active.get(active.binding_id) is not active:
                return False
            active.observation_error = describe_error(error)
            if active.state is not ActiveState.OBSERVATION_UNAVAILABLE:
                active.state = ActiveState.OBSERVATION_UNAVAILABLE
                active.activity_revision += 1
                self._advance_admission_revision(active.binding_id)
            logger.warning(
                "native Turn observation unavailable after bounded recovery: "
                "thread_id=%s turn_id=%s error=%s",
                active.handle.thread_id, active.handle.id, active.observation_error,
            )
            return True

    async def _mark_turn_observation_running(self, active: _ActiveTurn) -> None:
        async with self._lock(active.binding_id):
            if self._active.get(active.binding_id) is not active:
                return
            previous_revision = self._admission_revision(active.binding_id)
            if active.state is ActiveState.OBSERVATION_UNAVAILABLE:
                # Seeing inProgress restores observation, not permission to
                # undo an accepted stop. Its existing cleanup intent survives
                # read failures and remains in force until exact terminal.
                active.state = (
                    ActiveState.STOPPING if active.cleanup_required else ActiveState.RUNNING
                )
                active.observation_error = None
                active.activity_revision += 1
                self._advance_admission_revision(active.binding_id)
            self._signal_turn_rechecked(active, previous_revision=previous_revision)

    async def _turn_unavailable_notice_is_current(
        self,
        active: _ActiveTurn,
    ) -> bool:
        async with self._lock(active.binding_id):
            return (
                self._active.get(active.binding_id) is active
                and active.state is ActiveState.OBSERVATION_UNAVAILABLE
            )

    async def _read_terminal_result(
        self,
        active: _ActiveTurn,
    ) -> TurnResult | _TurnObservation:
        """Read one authoritative persisted Thread observation.

        The 0.147.0 immediate-completion race originally required this path.
        Although 0.154.0 retains those notifications, public persisted history
        remains the exact Turn's terminal authority, including steer/interrupt
        and resumed observation after a transport interruption.
        """

        native_thread = await self._read_native_thread(
            active,
            include_turns=False,
        )
        self._require_exact_observation_thread(active, native_thread)
        thread_status = _thread_status_type(native_thread)
        if (
            thread_status == "active"
            and active.in_progress_observed
            and not self._is_retiring_turn(active)
        ):
            return _TurnObservation.ACTIVE
        if thread_status not in {"active", "idle", "notLoaded", "systemError"}:
            raise RuntimeError(
                f"unexpected native Thread status: {thread_status!r}"
            )

        full_view = await self._read_native_thread(active, include_turns=True)
        classified = self._classify_full_turn_view(active, full_view)
        if _enum_value(getattr(classified, "status", None)) == "interrupted":
            await asyncio.sleep(limits.INTERRUPTION_CONFIRMATION_SECONDS)
            try:
                full_view = await self._read_native_thread(active, include_turns=True)
                classified = self._classify_full_turn_view(active, full_view)
            except _TurnViewUnverified as error:
                return await self._observe_with_bounded_recovery(
                    active, error, interruption_pending=True,
                )
            if isinstance(classified, _TurnObservation):
                return classified
        if isinstance(classified, _TurnObservation):
            if self._is_retiring_turn(active):
                raise _TurnViewUnverified(
                    "previous Turn is not terminal after native start/join advanced"
                )
            if thread_status == "idle":
                raise _TurnViewUnverified(
                    "native Thread changed from idle to an in-progress exact Turn"
                )
            return classified
        return await self._materialize_terminal_turn(active, classified)

    def _classify_full_turn_view(
        self,
        active: _ActiveTurn,
        native_thread: object,
    ) -> object | _TurnObservation:
        self._require_exact_observation_thread(active, native_thread)
        thread_status = _thread_status_type(native_thread)
        if thread_status not in {"active", "idle", "notLoaded", "systemError"}:
            raise RuntimeError(
                f"unexpected full native Thread status: {thread_status!r}"
            )
        turns = [
            item for item in getattr(native_thread, "turns", ())
            if getattr(item, "id", None) == active.handle.id
        ]
        if len(turns) > 1:
            raise RuntimeError("native history contained duplicate exact Turn identities")
        if not turns:
            if thread_status == "notLoaded":
                raise _TurnResumeRequired("native Thread is notLoaded and exact Turn is missing")
            raise _TurnViewUnverified(
                "full native Thread view did not contain the exact Turn"
            )
        turn = turns[0]
        turn_status = _enum_value(getattr(turn, "status", None))
        if turn_status == "inProgress":
            if thread_status == "notLoaded":
                raise _TurnResumeRequired("native Thread is notLoaded with an unfinished Turn")
            if thread_status != "active":
                raise _TurnViewUnverified(
                    f"{thread_status} native Thread still reported the exact Turn inProgress"
                )
            active.in_progress_observed = True
            return _TurnObservation.EXACT_IN_PROGRESS
        if turn_status not in {"completed", "interrupted", "failed"}:
            raise RuntimeError(
                f"unexpected native Turn status: {turn_status!r}"
            )
        return turn

    async def _materialize_terminal_turn(
        self,
        active: _ActiveTurn,
        turn: object,
    ) -> TurnResult:
        """Deliver a proven terminal Turn without re-entering recovery."""

        active.terminal_observed = True
        self._bindings.release_scheduled_initial_turn(active.binding_id, active.handle.id)
        status = _enum_value(getattr(turn, "status", None))
        current = turn
        retries = limits.TERMINAL_RESPONSE_MATERIALIZATION_RETRIES
        while status == "completed":
            items = list(getattr(current, "items", ()))
            if _final_agent_response(items) is not None or retries == 0:
                break
            if retries == limits.TERMINAL_RESPONSE_MATERIALIZATION_RETRIES:
                logger.warning(
                    "native completed Turn response not materialized; retrying",
                    extra={
                        "thread_id": active.handle.thread_id,
                        "turn_id": active.handle.id,
                    },
                )
            retries -= 1
            await asyncio.sleep(self._poll_interval_seconds)
            try:
                native_thread = await self._read_native_thread(
                    active,
                    include_turns=True,
                )
                refreshed = self._classify_full_turn_view(active, native_thread)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "terminal Turn response refresh failed; delivering without text",
                    exc_info=True,
                    extra={
                        "thread_id": active.handle.thread_id,
                        "turn_id": active.handle.id,
                    },
                )
                break
            if isinstance(refreshed, _TurnObservation):
                logger.warning(
                    "terminal Turn regressed to inProgress; delivering proven terminal",
                    extra={
                        "thread_id": active.handle.thread_id,
                        "turn_id": active.handle.id,
                    },
                )
                break
            refreshed_status = _enum_value(getattr(refreshed, "status", None))
            if refreshed_status != status:
                logger.warning(
                    "terminal Turn status changed during response materialization",
                    extra={
                        "thread_id": active.handle.thread_id,
                        "turn_id": active.handle.id,
                        "status": status,
                        "refreshed_status": refreshed_status,
                    },
                )
                break
            current = refreshed

        items = list(getattr(current, "items", ()))
        return TurnResult(
            id=active.handle.id,
            status=getattr(current, "status"),
            error=getattr(current, "error", None),
            started_at=getattr(current, "started_at", None),
            completed_at=getattr(current, "completed_at", None),
            duration_ms=getattr(current, "duration_ms", None),
            final_response=_final_agent_response(items),
            items=items,
            usage=None,
        )

    @staticmethod
    def _require_exact_observation_thread(
        active: _ActiveTurn,
        native_thread: object,
    ) -> None:
        if getattr(native_thread, "id", None) != active.handle.thread_id:
            raise RuntimeError(
                "native Thread read returned a different identity"
            )

    async def _read_native_thread(
        self,
        active: _ActiveTurn,
        *,
        include_turns: bool,
    ) -> object:
        try:
            response = await active.thread.read(include_turns=include_turns)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "native Thread read unavailable: thread_id=%s turn_id=%s error=%s",
                active.handle.thread_id, active.handle.id, describe_error(error),
            )
            if isinstance(error, InternalRpcError):
                # A server-side read error does not imply a lost attachment.
                # Retry the exact persisted view before requesting resume;
                # new Thread metadata may still be materializing. Both paths
                # share the same bounded recovery budget.
                raise _TurnViewUnverified("native Thread read failed") from error
            if include_turns and _is_paginated_turn_read_unavailable(error):
                # In 0.154.0 the rollout can advertise paginated history before
                # its SQLite metadata is visible to the internal list readers.
                # Retry this exact public read within the existing budget;
                # other missing methods remain contract failures.
                raise _TurnViewUnverified(
                    "native Thread paginated history is unavailable"
                ) from error
            if _is_recoverable_turn_observation_io_error(error):
                raise _TurnResumeRequired("native Thread read failed") from error
            if (
                include_turns
                and isinstance(error, InvalidRequestError)
                and error.code == -32600
                and error.message
                == f"thread {active.handle.thread_id} {limits.NOT_MATERIALIZED_SUFFIX}"
            ):
                raise _TurnViewUnverified(
                    "native Thread turns are not materialized yet"
                ) from error
            raise
        # Current Thread metadata cannot prove the accepted Turn's directory:
        # another client may already have changed cwd after its completion.
        return getattr(response, "thread", None)

    def _track(self, active: _ActiveTurn) -> None:
        self._active[active.binding_id] = active
        self._start_turn_consumer(active)

    def _track_late_shutdown_turn(self, active: _ActiveTurn) -> None:
        active.receipt_attempted.set()

        async def stop() -> None:
            try:
                await self._interrupt(active.binding_id, active)
            except Exception:
                logger.exception(
                    "late accepted external Turn shutdown cleanup failed",
                    extra={"thread_id": active.handle.thread_id, "turn_id": active.handle.id},
                )

        task = asyncio.create_task(stop(), name=f"codex-shutdown-turn:{active.handle.id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _start_turn_consumer(
        self,
        active: _ActiveTurn,
        *,
        recover_first: bool = False,
        notify_unavailable: bool = True,
    ) -> None:
        ready = asyncio.Event() if recover_first else None
        active.recheck_ready = ready
        active.recheck_revision = (
            self._admission_revision(active.binding_id) if recover_first else None
        )
        task = asyncio.create_task(
            self._consume(
                active, recover_first=recover_first, notify_unavailable=notify_unavailable,
            ),
            name=f"codex-turn:{active.handle.id}",
        )
        active.task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        if ready is not None:
            task.add_done_callback(lambda _task: ready.set())
