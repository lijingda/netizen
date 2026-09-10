"""Claim due plans and hand them to ordinary Channel/Runtime execution.

Only dispatch creation is owned here. Native Turns keep their ordinary single
consumer; this timer never waits for them or resumes them after a restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Protocol

from ..bindings import BindingStore, ProjectDeleting
from .models import Claim, Run, ScheduleNotFound


logger = logging.getLogger(__name__)
TICK_INTERVAL_SECONDS = 1.0
RECOVERY_TIMEOUT_SECONDS = 10.0
READ_TIMEOUT_SECONDS = 3.0
DISPATCH_TIMEOUT_SECONDS = 30.0
_TERMINAL = {"completed", "interrupted", "failed"}
_PRE_NATIVE = {"claimed", "publishing_topic", "binding_ready"}


class ScheduledTurnReader(Protocol):
    async def read_scheduled_turn(
        self, binding_id: str, turn_id: str, *, deadline: float | None = None,
    ) -> str: ...


class Scheduler:
    def __init__(
        self,
        bindings: BindingStore,
        runtime: ScheduledTurnReader,
        app_id: str,
        dispatch: Callable[[Claim], Awaitable[None]],
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._bindings = bindings
        self._store = bindings.schedules
        self._runtime = runtime
        self._app_id = app_id
        self._dispatch = dispatch
        self._wall_clock = wall_clock
        self._loop: asyncio.AbstractEventLoop | None = None
        self._timer: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._admission = False
        self._closed = False
        self._recovered = False
        self._ticking = False
        self._dispatches: dict[str, tuple[str, asyncio.Task[None]]] = {}
        # Only exact Turns observed running after restart/explicit refresh.
        # Fresh ordinary consumers are responsible for their own barriers.
        self._recovered_running: dict[str, str] = {}

    def _own_loop(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("Scheduler must run on its owning event loop")
        return loop

    async def recover(self) -> None:
        """Reconcile pending metadata once, then skip all offline due points."""
        loop = self._own_loop()
        if self._admission or self._closed or self._recovered:
            raise RuntimeError("Scheduler recovery must precede admission exactly once")
        self._store.abandon_pending_deliveries()
        deadline = loop.time() + RECOVERY_TIMEOUT_SECONDS
        for run in self._store.pending_runs():
            if run.app_id != self._app_id:
                continue
            if run.phase in _PRE_NATIVE:
                self._store.release(run.id, error_code=(
                    "publishing_unknown" if run.phase == "publishing_topic"
                    else "recovery_no_start"
                ))
            elif run.barrier != "unknown":
                await self._observe(run, deadline=deadline)
            # A persisted unknown is not retried by every service restart.
        seen: set[str] = set()
        while True:
            plans = self._store.due_plans(app_id=self._app_id, now=self._wall_clock())
            fresh = [plan for plan in plans if plan.id not in seen]
            if not fresh:
                break
            for plan in fresh:
                seen.add(plan.id)
                try:
                    self._store.claim_due(plan.id, app_id=self._app_id, now=self._wall_clock(), recover=True)
                except (ProjectDeleting, ScheduleNotFound):
                    continue
            await asyncio.sleep(0)
        self._recovered = True

    def start(self) -> None:
        self._own_loop()
        if self._closed or self._timer is not None or not self._recovered:
            raise RuntimeError("Recover Scheduler before starting it exactly once")
        self._admission = True
        self._timer = asyncio.create_task(self._run_timer(), name="schedule-timer")

    def wake(self) -> None:
        self._wake.set()

    def close_admission(self) -> None:
        self._admission = False
        self.wake()

    async def close(self) -> None:
        """Stop the timer; shutdown dispatch draining remains a separate step."""
        self.close_admission()
        self._closed = True
        if self._timer is not None:
            self._timer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._timer

    async def _run_timer(self) -> None:
        while self._admission:
            self._wake.clear()
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # A Store/controller failure must not keep creating work.
                self.close_admission()
                logger.error("Schedule timer stopped", extra={"error_type": type(error).__name__})
                return
            if self._admission:
                try:
                    await asyncio.wait_for(self._wake.wait(), TICK_INTERVAL_SECONDS)
                except TimeoutError:
                    pass

    async def tick(self) -> int:
        """Claim one bounded batch; return without waiting for dispatch or Turn."""
        loop = self._own_loop()
        if not self._admission or self._ticking:
            return 0
        self._ticking = True
        claimed = 0
        try:
            deadline = loop.time() + RECOVERY_TIMEOUT_SECONDS
            plans = self._store.due_plans(app_id=self._app_id, now=self._wall_clock())
            for plan in plans:
                if not self._admission:
                    break
                pending = self._store.pending_for_plan(plan.id)
                if pending is None:
                    self._recovered_running.pop(plan.id, None)
                elif self._recovered_running.get(plan.id) == pending.id:
                    await self._observe(pending, deadline=deadline)
                if not self._admission:
                    break
                try:
                    claim = self._store.claim_due(plan.id, app_id=self._app_id, now=self._wall_clock())
                except (ProjectDeleting, ScheduleNotFound):
                    continue
                if claim is not None:
                    task = asyncio.create_task(self._dispatch_once(claim), name="schedule-dispatch:" + claim.run.id)
                    self._dispatches[claim.run.id] = (claim.run.project_alias, task)
                    task.add_done_callback(lambda done, run_id=claim.run.id: self._dispatch_done(run_id, done))
                    claimed += 1
            return claimed
        finally:
            self._ticking = False

    async def refresh(self, plan_id: str) -> str | None:
        """Explicit user recheck; ordinary Runtime observation guards still apply."""
        loop = self._own_loop()
        run = self._store.pending_for_plan(plan_id)
        if run is None or run.app_id != self._app_id:
            self._recovered_running.pop(plan_id, None)
            return None
        if run.id in self._dispatches or run.phase in _PRE_NATIVE:
            return None
        return await self._observe(run, deadline=loop.time() + READ_TIMEOUT_SECONDS)

    async def _observe(self, run: Run, *, deadline: float) -> str | None:
        loop = self._own_loop()
        self._recovered_running.pop(run.plan_id, None)
        try:
            current = self._store.get_run(run.id)
        except ScheduleNotFound:
            return None
        if current.barrier == "released":
            return None
        if not current.binding_id or not current.initial_turn_id:
            self._store.set_run(current.id, barrier="unknown", error_code="initial_reference_unknown")
            return None
        if loop.time() >= deadline:
            self._store.set_run(current.id, barrier="unknown", error_code="read_timeout")
            return None
        try:
            async with asyncio.timeout_at(min(deadline, loop.time() + READ_TIMEOUT_SECONDS)):
                status = await self._runtime.read_scheduled_turn(
                    current.binding_id, current.initial_turn_id,
                    deadline=min(deadline, loop.time() + READ_TIMEOUT_SECONDS),
                )
        except asyncio.CancelledError:
            # Shutdown is not native terminal evidence. No later timer is left
            # to repeat the cancelled observation automatically.
            with contextlib.suppress(ScheduleNotFound):
                self._store.set_run(current.id, barrier="unknown", error_code="read_cancelled")
            raise
        except Exception as error:
            code = getattr(error, "code", None)
            if not isinstance(code, str):
                code = "read_timeout" if isinstance(error, TimeoutError) else "read_unavailable"
            with contextlib.suppress(ScheduleNotFound):
                self._store.set_run(current.id, barrier="unknown", error_code=code)
            return None
        # Re-read after await: terminal/lifecycle proof may already have released
        # this barrier. Store never reopens a released occurrence.
        try:
            current = self._store.get_run(current.id)
        except ScheduleNotFound:
            # Completion/lifecycle may have released and pruned it while this
            # read was outstanding. There is no longer a barrier to change.
            return None
        if current.barrier == "released":
            return status if status in _TERMINAL else None
        if status in _TERMINAL:
            self._store.release(current.id)
            return status
        if status == "inProgress":
            current = self._store.set_run(current.id, barrier="held", error_code=None)
            self._recovered_running[current.plan_id] = current.id
            return status
        self._store.set_run(current.id, barrier="unknown", error_code="status_unavailable")
        return None

    async def _dispatch_once(self, claim: Claim) -> None:
        try:
            async with asyncio.timeout(DISPATCH_TIMEOUT_SECONDS):
                await self._dispatch(claim)
        except asyncio.CancelledError:
            self._settle_dispatch(claim.run.id, "dispatch_cancelled")
            raise
        except Exception as error:
            self._settle_dispatch(claim.run.id, "dispatch_timeout" if isinstance(error, TimeoutError) else "dispatch_failed")
            logger.warning("Scheduled dispatch did not finish", extra={"run_id": claim.run.id, "error_type": type(error).__name__})
        else:
            try:
                run = self._store.get_run(claim.run.id)
            except ScheduleNotFound:
                return
            if run.barrier == "held" and (
                run.phase != "handed_off" or not run.binding_id or not run.initial_turn_id
            ):
                self._settle_dispatch(run.id, "dispatch_incomplete")

    def _settle_dispatch(self, run_id: str, reason: str) -> None:
        try:
            run = self._store.get_run(run_id)
        except ScheduleNotFound:
            return
        if run.barrier in {"released", "unknown"}:
            return
        if run.phase in _PRE_NATIVE:
            self._store.release(run_id, error_code="publishing_unknown" if run.phase == "publishing_topic" else reason)
        else:
            self._store.set_run(run_id, barrier="unknown", error_code=reason)

    def _dispatch_done(self, run_id: str, task: asyncio.Task[None]) -> None:
        self._dispatches.pop(run_id, None)
        if not task.cancelled() and task.exception() is not None:
            self.close_admission()
            logger.error("Scheduled dispatch state could not be saved", extra={"run_id": run_id})

    async def drain(self, deadline: float) -> bool:
        """Bound shutdown creation work; never cancel a handed-off native Turn."""
        loop = self._own_loop()
        tasks = {task for _, task in self._dispatches.values() if not task.done()}
        if not tasks:
            return True
        try:
            _, pending = await asyncio.wait(tasks, timeout=max(0, deadline - loop.time()))
        except asyncio.CancelledError:
            await self._cancel_dispatches({task for task in tasks if not task.done()})
            raise
        if not pending:
            return True
        await self._cancel_dispatches(pending)
        return False

    async def _cancel_dispatches(self, pending: set[asyncio.Task[None]]) -> None:
        if not pending:
            return
        for task in pending:
            task.cancel()
        # Save uncertainty before yielding, including when the outer service
        # cleanup budget cancelled drain before its own deadline elapsed.
        for run_id, (_, task) in tuple(self._dispatches.items()):
            if task in pending and not task.done():
                self._settle_dispatch(run_id, "dispatch_cancelled")
        await asyncio.wait(pending, timeout=0.1)

    async def drain_project_creation(self, alias: str, deadline: float) -> bool:
        """Project deletion has already frozen new claims; only observe tasks."""
        loop = self._own_loop()
        tasks = {task for project, task in self._dispatches.values() if project == alias and not task.done()}
        if not tasks:
            return True
        _, pending = await asyncio.wait(tasks, timeout=max(0, deadline - loop.time()))
        return not pending
