"""Bounded execution for blocking management-plane filesystem work.

The executor deliberately counts submitted ``concurrent.futures.Future``
objects rather than asyncio Tasks.  Cancelling an HTTP handler therefore only
stops that handler from waiting; it neither cancels the blocking operation nor
returns its admission slot early.
"""

from __future__ import annotations

import asyncio
import math
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from typing import Callable, TypeVar


ResultT = TypeVar("ResultT")


class BlockingIOExecutorClosed(RuntimeError):
    """Raised when work is submitted after admission has closed."""


class BlockingIOExecutorSaturated(RuntimeError):
    """Raised when all running and queued work slots are occupied."""


class BlockingIOResultUnknown(RuntimeError):
    """A submitted operation did not finish before its response deadline.

    The underlying operation is not cancelled and remains tracked by the
    executor.  Callers must reconcile the authoritative state; they must not
    infer rollback or retry the operation automatically.
    """

    def __init__(self, *, deadline: float) -> None:
        self.deadline = deadline
        super().__init__(
            "blocking I/O deadline expired after submission; result is unknown "
            "and the operation remains tracked"
        )


class BlockingIODrainTimeout(TimeoutError):
    """Raised when tracked work cannot drain within the caller's budget."""

    def __init__(self, *, in_flight: int, deadline: float) -> None:
        self.in_flight = in_flight
        self.deadline = deadline
        super().__init__(
            f"blocking I/O drain deadline expired with {in_flight} operation(s) "
            "still tracked"
        )


class BlockingIOShutdownTimeout(TimeoutError):
    """Raised when the worker-thread join exceeds the caller's budget."""

    def __init__(self, *, deadline: float) -> None:
        self.deadline = deadline
        super().__init__("blocking I/O worker shutdown exceeded its deadline")


class BoundedBlockingIOExecutor:
    """Run synchronous callables without an unbounded executor queue.

    ``capacity`` counts both running and queued operations.  Admission is
    rejected synchronously within :meth:`submit` before
    :class:`ThreadPoolExecutor` sees the callable.  The instance belongs to one
    asyncio event loop, while its completion accounting is protected for
    callbacks arriving from worker threads.

    ``deadline`` values are absolute values from the owning loop's monotonic
    clock (``loop.time()``), which is suitable for sharing one shutdown or
    request budget across several awaits.
    """

    def __init__(
        self,
        *,
        max_workers: int = 1,
        capacity: int = 2,
        thread_name_prefix: str = "netizen-project-io",
    ) -> None:
        if (
            isinstance(max_workers, bool)
            or not isinstance(max_workers, int)
            or max_workers < 1
        ):
            raise ValueError("max_workers must be a positive integer")
        if (
            isinstance(capacity, bool)
            or not isinstance(capacity, int)
            or capacity < max_workers
        ):
            raise ValueError("capacity must be an integer at least max_workers")
        if not thread_name_prefix:
            raise ValueError("thread_name_prefix must not be empty")

        self._capacity = capacity
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=thread_name_prefix,
        )
        self._state_lock = threading.Lock()
        self._tracked: set[Future[object]] = set()
        self._accepting = True
        self._shutdown_started = False

        self._loop: asyncio.AbstractEventLoop | None = None
        self._completion_event: asyncio.Event | None = None
        self._join_task: asyncio.Task[None] | None = None
        self._join_error: BaseException | None = None
        self._joined = False

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def accepting(self) -> bool:
        with self._state_lock:
            return self._accepting

    @property
    def in_flight(self) -> int:
        """Return the number of running or queued underlying operations."""

        with self._state_lock:
            return len(self._tracked)

    @property
    def joined(self) -> bool:
        return self._joined

    async def submit(
        self,
        function: Callable[..., ResultT],
        /,
        *args: object,
        deadline: float | None = None,
        **kwargs: object,
    ) -> ResultT:
        """Submit a synchronous callable and asynchronously await its result.

        A deadline applies only to waiting for the already-submitted operation.
        Expiry raises :class:`BlockingIOResultUnknown`; it never cancels or
        retries the underlying future.
        """

        loop, _ = self._bind_to_running_loop()
        checked_deadline = _validate_deadline(deadline)
        operation = partial(function, *args, **kwargs)

        with self._state_lock:
            if not self._accepting:
                raise BlockingIOExecutorClosed(
                    "blocking I/O executor is closed to new work"
                )
            if len(self._tracked) >= self._capacity:
                raise BlockingIOExecutorSaturated(
                    "blocking I/O executor has no free submission slots"
                )
            # This is the only call into ThreadPoolExecutor.submit.  Keeping it
            # in the same critical section as the checks guarantees that a
            # rejected operation never reaches its unbounded internal queue.
            future = self._executor.submit(operation)
            self._tracked.add(future)

        future.add_done_callback(self._operation_completed)
        wrapped = asyncio.wrap_future(future, loop=loop)
        # If the request is cancelled or times out, nobody may await ``wrapped``
        # again.  Retrieve a later exception to prevent an asyncio warning;
        # this does not change the result seen by a normal waiter.
        wrapped.add_done_callback(_consume_asyncio_future_exception)

        if checked_deadline is None:
            return await asyncio.shield(wrapped)

        remaining = checked_deadline - loop.time()
        if remaining > 0:
            done, _ = await asyncio.wait((wrapped,), timeout=remaining)
            if done:
                return wrapped.result()

        # Cover completion racing the timeout without claiming uncertainty
        # when the concurrent result is already synchronously available.
        if future.done():
            return future.result()
        raise BlockingIOResultUnknown(deadline=checked_deadline)

    def close_admission(self) -> None:
        """Idempotently reject all future submissions without cancelling work."""

        with self._state_lock:
            self._accepting = False

    async def drain(self, *, deadline: float | None = None) -> None:
        """Wait for all tracked operations within an optional absolute deadline."""

        loop, completion_event = self._bind_to_running_loop()
        checked_deadline = _validate_deadline(deadline)

        while True:
            # Clear before checking while the worker callback cannot acquire
            # the state lock.  A completion after the check schedules a fresh
            # set(), so no wakeup can be lost.
            completion_event.clear()
            with self._state_lock:
                remaining_work = len(self._tracked)
            if remaining_work == 0:
                return

            if checked_deadline is None:
                await completion_event.wait()
                continue

            remaining_time = checked_deadline - loop.time()
            if remaining_time <= 0:
                raise BlockingIODrainTimeout(
                    in_flight=remaining_work,
                    deadline=checked_deadline,
                )
            try:
                await asyncio.wait_for(
                    completion_event.wait(),
                    timeout=remaining_time,
                )
            except TimeoutError:
                with self._state_lock:
                    remaining_work = len(self._tracked)
                if remaining_work == 0:
                    return
                raise BlockingIODrainTimeout(
                    in_flight=remaining_work,
                    deadline=checked_deadline,
                ) from None

    async def aclose(self, *, deadline: float | None = None) -> None:
        """Close admission, drain tracked work, and join worker threads.

        The operation is idempotent.  Cancellation or deadline expiry leaves
        admission closed and the submitted work running to completion; a later
        call can finish draining and joining it.
        """

        loop, _ = self._bind_to_running_loop()
        checked_deadline = _validate_deadline(deadline)
        self.close_admission()
        self._start_executor_shutdown()

        await self.drain(deadline=checked_deadline)
        if self._joined:
            if self._join_error is not None:
                raise self._join_error
            return

        join_task = self._join_task
        if join_task is None:
            join_task = asyncio.create_task(
                asyncio.to_thread(
                    self._executor.shutdown,
                    wait=True,
                    cancel_futures=False,
                ),
                name="netizen-blocking-io-shutdown",
            )
            join_task.add_done_callback(self._executor_joined)
            self._join_task = join_task

        if checked_deadline is None:
            await asyncio.shield(join_task)
        else:
            remaining = checked_deadline - loop.time()
            if remaining <= 0:
                if not join_task.done():
                    raise BlockingIOShutdownTimeout(deadline=checked_deadline)
            else:
                done, _ = await asyncio.wait((join_task,), timeout=remaining)
                if not done:
                    raise BlockingIOShutdownTimeout(deadline=checked_deadline)

        # The callback consumes and records exceptions for deadline/cancelled
        # callers; result() here preserves normal close failure propagation.
        join_task.result()

    def _bind_to_running_loop(
        self,
    ) -> tuple[asyncio.AbstractEventLoop, asyncio.Event]:
        loop = asyncio.get_running_loop()
        with self._state_lock:
            if self._loop is None:
                self._loop = loop
                self._completion_event = asyncio.Event()
            elif self._loop is not loop:
                raise RuntimeError(
                    "BoundedBlockingIOExecutor cannot be used across event loops"
                )
            completion_event = self._completion_event
        assert completion_event is not None
        return loop, completion_event

    def _operation_completed(self, future: Future[object]) -> None:
        with self._state_lock:
            self._tracked.discard(future)
            loop = self._loop
            completion_event = self._completion_event
        if loop is None or completion_event is None:
            return
        try:
            loop.call_soon_threadsafe(completion_event.set)
        except RuntimeError:
            # The service is expected to drain before closing its event loop.
            # Accounting is already correct even if a broken shutdown cannot
            # be woken after the loop itself has disappeared.
            pass

    def _start_executor_shutdown(self) -> None:
        with self._state_lock:
            if self._shutdown_started:
                return
            self._shutdown_started = True
        # wait=False never cancels queued work and returns immediately.  A
        # later off-loop wait=True call performs the clean thread join.
        self._executor.shutdown(wait=False, cancel_futures=False)

    def _executor_joined(self, task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except BaseException as error:
            self._join_error = error
        else:
            self._joined = True


def _validate_deadline(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        raise TypeError("deadline must be a finite monotonic timestamp")
    checked = float(deadline)
    if not math.isfinite(checked):
        raise ValueError("deadline must be a finite monotonic timestamp")
    return checked


def _consume_asyncio_future_exception(future: asyncio.Future[object]) -> None:
    if future.cancelled():
        return
    future.exception()
