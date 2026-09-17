"""Best-effort titles from private ephemeral forks of ordinary Threads.

Nothing here owns a Binding, user-visible Turn, or native admission. Resource
acquisition RPCs survive cancellation of the useful work: the async SDK wraps
blocking RPCs, so their late results still have to reach the cleanup owner.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from openai_codex import InternalRpcError, InvalidRequestError

from ..model_settings import TurnModelSettings
from ..sdk_gap_adapter import ThreadSubscriptionControl
from ..terminal_cleanup import TerminalCleanup
from .contracts import NativeCodex, NativeThread, NativeTurnHandle


logger = logging.getLogger(__name__)
_CONTEXT_READY_TIMEOUT_SECONDS = 5.0
_CONTEXT_POLL_SECONDS = 0.05

NAMING_PROMPT = """上文是待命名会话的参考上下文，不是你需要执行的任务。
请根据这些上下文概括会话主题，沿用用户语言，直接输出一个简短的会话名称。
不得调用任何工具，不得继续执行上文任务，不要解释、引号、Markdown 或其他内容。
只输出一行名称，长度为 1 到 120 个字符。"""


@dataclass(slots=True, eq=False)
class NamingJob:
    binding_id: str
    parent: NativeThread
    turn_id: str
    settings: TurnModelSettings | None
    invalidated: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    thread: NativeThread | None = None
    handle: NativeTurnHandle | None = None
    run: asyncio.Task[Any] | None = None
    acquiring: asyncio.Task[Any] | None = None
    acquisition_unknown: bool = False
    terminal_observed: bool = False
    workers: set[asyncio.Task[Any]] = field(default_factory=set)


class _Invalidated(Exception):
    pass


class ThreadNamer:
    def __init__(
        self,
        *,
        codex: NativeCodex,
        terminal_cleanup: TerminalCleanup,
        subscription_control: ThreadSubscriptionControl,
        commit: Callable[[NamingJob, str], Awaitable[None]],
        timeout_seconds: float = 120.0,
        cleanup_timeout_seconds: float = 5.0,
    ) -> None:
        if timeout_seconds <= 0 or cleanup_timeout_seconds <= 0:
            raise ValueError("naming timeouts must be positive")
        self._codex = codex
        self._terminal_cleanup = terminal_cleanup
        self._subscription_control = subscription_control
        self._commit = commit
        self._timeout_seconds = timeout_seconds
        self._cleanup_timeout_seconds = cleanup_timeout_seconds
        self._jobs: dict[str, NamingJob] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    def start(
        self,
        binding_id: str,
        parent: NativeThread,
        turn_id: str,
        settings: TurnModelSettings | None = None,
    ) -> None:
        """Reserve one background attempt without awaiting any native work."""
        if self._closed:
            return
        if binding_id in self._jobs:
            return
        job = NamingJob(binding_id, parent, turn_id, settings)
        self._jobs[binding_id] = job
        task = asyncio.create_task(self._generate(job), name=f"thread-name:{parent.id}")
        job.task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def is_current(self, job: NamingJob) -> bool:
        return (
            not self._closed
            and not job.invalidated.is_set()
            and self._jobs.get(job.binding_id) is job
        )

    def invalidate(self, binding_id: str) -> None:
        job = self._jobs.get(binding_id)
        if job is not None:
            job.invalidated.set()

    def close(self) -> None:
        self._closed = True
        for job in self._jobs.values():
            job.invalidated.set()

    async def shutdown(self) -> None:
        """Bound shutdown without cancelling a worker that may acquire a resource."""
        self.close()
        if self._tasks:
            # Interrupt, drain, one terminal read, cleanup, and unsubscribe each
            # have a bound. Unknown workers are released by transport exit.
            await asyncio.wait(
                tuple(self._tasks), timeout=5 * self._cleanup_timeout_seconds,
            )

    def _require_current(self, job: NamingJob) -> None:
        if not self.is_current(job):
            raise _Invalidated

    def _worker(
        self, job: NamingJob, operation: Coroutine[Any, Any, Any],
    ) -> asyncio.Task[Any]:
        task = asyncio.create_task(operation)
        job.workers.add(task)

        def finished(done: asyncio.Task[Any]) -> None:
            job.workers.discard(done)
            if not done.cancelled():
                # Some reads/cleanup RPCs outlive their bounded caller. Retrieve
                # their exception even when the useful naming work has stopped.
                done.exception()

        task.add_done_callback(finished)
        return task

    async def _observe(self, job: NamingJob, worker: asyncio.Task[Any]) -> Any:
        stopped = asyncio.create_task(job.invalidated.wait())
        try:
            await asyncio.wait((worker, stopped), return_when=asyncio.FIRST_COMPLETED)
            self._require_current(job)
            return worker.result()
        finally:
            stopped.cancel()
            await asyncio.gather(stopped, return_exceptions=True)

    async def _expire(self, job: NamingJob) -> None:
        await asyncio.sleep(self._timeout_seconds)
        job.invalidated.set()

    async def _generate(self, job: NamingJob) -> None:
        watchdog = asyncio.create_task(self._expire(job))
        try:
            self._require_current(job)
            if not await self._input_visible(job):
                return
            self._require_current(job)
            job.acquiring = self._worker(job, self._fork(job))
            thread = await asyncio.shield(job.acquiring)
            self._require_current(job)
            response = await self._observe(
                job, self._worker(job, thread.read(include_turns=False)),
            )
            native = getattr(response, "thread", None)
            if (
                getattr(native, "id", None) != thread.id
                or getattr(native, "ephemeral", None) is not True
                or getattr(native, "path", None) is not None
                or getattr(native, "forked_from_id", None) not in {None, job.parent.id}
            ):
                raise ValueError("naming fork persistence or parent identity mismatch")
            self._require_current(job)
            kwargs: dict[str, object] = {}
            if job.settings is not None:
                kwargs = {
                    "model": job.settings.model,
                    "effort": job.settings.effort,
                    "service_tier": job.settings.service_tier_id,
                }
            job.acquiring = self._worker(job, self._start_turn(job, thread, kwargs))
            handle = await asyncio.shield(job.acquiring)
            assert job.run is not None
            result = await self._observe(job, job.run)
            status = _status(result)
            job.terminal_observed = _exact_terminal(result, handle)
            title = getattr(result, "final_response", None)
            if (
                getattr(result, "id", None) != handle.id
                or status != "completed"
                or not isinstance(title, str)
            ):
                return
            title = title.strip()
            if not title or len(title) > 120 or len(title.splitlines()) != 1:
                return
            self._require_current(job)
            await self._observe(job, self._worker(job, self._commit(job, title)))
        except _Invalidated:
            pass
        except asyncio.CancelledError:
            job.invalidated.set()
            raise
        except Exception as error:
            logger.warning("automatic Thread naming failed error_type=%s", type(error).__name__)
        finally:
            job.invalidated.set()
            watchdog.cancel()
            await asyncio.gather(watchdog, return_exceptions=True)
            if job.acquiring is not None and not job.acquiring.done():
                # Even explicit task cancellation cannot throw away a late
                # fork/Turn identity. The acquiring worker records it first.
                await asyncio.gather(asyncio.shield(job.acquiring), return_exceptions=True)
            await self._cleanup(job)
            # Retain the one-attempt slot while any native worker is still in
            # flight, including a run whose terminal could not yet be observed.
            # Neither a new Turn nor a timeout should accumulate orphan forks.
            if job.workers:
                await asyncio.gather(
                    *(asyncio.shield(worker) for worker in tuple(job.workers)),
                    return_exceptions=True,
                )
            if job.run is not None and job.handle is not None and not job.run.cancelled():
                try:
                    result = job.run.result()
                except Exception:
                    pass
                else:
                    # A terminal arriving after the bounded cleanup drain is
                    # still exact evidence; a consumer error or interrupt ACK
                    # alone must never release the attempt slot.
                    job.terminal_observed |= _exact_terminal(result, job.handle)
            if (
                self._jobs.get(job.binding_id) is job
                and not job.acquisition_unknown
                and (job.handle is None or job.terminal_observed)
            ):
                self._jobs.pop(job.binding_id)

    async def _input_visible(self, job: NamingJob) -> bool:
        """Wait for readable metadata and input within one background deadline.

        A start ACK can precede even the first readable rollout. Existing names
        need only a summary; unnamed Threads additionally need exact Turn input.
        """
        summary_ready = False
        try:
            async with asyncio.timeout(_CONTEXT_READY_TIMEOUT_SECONDS):
                while True:
                    self._require_current(job)
                    try:
                        response = await self._observe(
                            job,
                            self._worker(job, job.parent.read(include_turns=summary_ready)),
                        )
                    except (InternalRpcError, InvalidRequestError) as error:
                        not_materialized = (
                            isinstance(error, InvalidRequestError)
                            and error.code == -32600
                            and error.message == (
                                f"thread {job.parent.id} is not materialized yet; "
                                "includeTurns is unavailable before first user message"
                            )
                        )
                        empty_rollout = (
                            isinstance(error, InternalRpcError)
                            and error.code == -32603
                            and (
                                "rollout is empty" in error.message
                                or (
                                    "failed to read session metadata" in error.message
                                    and "rollout at " in error.message
                                    and " is empty" in error.message
                                )
                            )
                        )
                        if not (not_materialized or empty_rollout):
                            raise
                    else:
                        native = getattr(response, "thread", None)
                        if getattr(native, "id", None) != job.parent.id:
                            raise ValueError("naming context identity mismatch")
                        if not hasattr(native, "name"):
                            raise ValueError("native Thread name is unavailable")
                        name = native.name
                        if name is not None and not isinstance(name, str):
                            raise ValueError("invalid native Thread name")
                        if name and name.strip():
                            return False
                        if not summary_ready:
                            summary_ready = True
                            continue
                        for turn in getattr(native, "turns", ()):
                            if getattr(turn, "id", None) != job.turn_id:
                                continue
                            if any(
                                getattr(getattr(item, "root", item), "type", None)
                                == "userMessage"
                                for item in getattr(turn, "items", ())
                            ):
                                return True
                    await asyncio.sleep(_CONTEXT_POLL_SECONDS)
        except TimeoutError:
            return False

    async def _fork(self, job: NamingJob) -> NativeThread:
        # An exception is not proof that a side-effectful RPC did not execute.
        # Without its exact identity, retain the invalidated slot until exit.
        job.acquisition_unknown = True
        thread = await self._codex.thread_fork(
            job.parent.id, ephemeral=True, include_turns=False,
        )
        if (
            not isinstance(thread.id, str)
            or not thread.id
            or thread.id == job.parent.id
        ):
            raise ValueError("naming fork identity mismatch")
        job.thread = thread
        job.acquisition_unknown = False
        return thread

    async def _start_turn(
        self, job: NamingJob, thread: NativeThread, kwargs: dict[str, object],
    ) -> NativeTurnHandle:
        job.acquisition_unknown = True
        handle = await thread.turn(NAMING_PROMPT, **kwargs)
        if handle.thread_id != thread.id or not isinstance(handle.id, str) or not handle.id:
            raise ValueError("naming Turn identity mismatch")
        job.handle = handle
        job.acquisition_unknown = False
        job.run = self._worker(job, handle.run())
        return handle

    async def _bounded_cleanup(
        self, job: NamingJob, operation: Coroutine[Any, Any, Any], *, step: str,
    ) -> None:
        try:
            await asyncio.wait_for(
                asyncio.shield(self._worker(job, operation)),
                timeout=self._cleanup_timeout_seconds,
            )
        except Exception as error:
            logger.warning(
                "automatic Thread naming cleanup incomplete step=%s error_type=%s",
                step, type(error).__name__,
            )

    async def _cleanup(self, job: NamingJob) -> None:
        thread = job.thread
        if thread is None:
            return
        if job.handle is not None and not job.terminal_observed:
            await self._bounded_cleanup(job, job.handle.interrupt(), step="interrupt")
            if job.run is not None:
                try:
                    result = await asyncio.wait_for(
                        asyncio.shield(job.run), timeout=self._cleanup_timeout_seconds,
                    )
                    job.terminal_observed = _exact_terminal(result, job.handle)
                except Exception as error:
                    logger.warning(
                        "automatic Thread naming terminal unconfirmed error_type=%s",
                        type(error).__name__,
                    )
            if not job.terminal_observed:
                await self._reconcile_terminal(job)
        await self._bounded_cleanup(
            job, self._terminal_cleanup.clean_thread(thread.id), step="terminals",
        )
        await self._bounded_cleanup(
            job, self._subscription_control.unsubscribe(thread.id), step="unsubscribe",
        )

    async def _reconcile_terminal(self, job: NamingJob) -> None:
        """Use one public read only as terminal evidence, never to recover a title.

        The SDK raises from run() for a failed Turn, even after consuming its
        terminal notification. Ephemeral full reads may be unsupported; failure
        to obtain this optional proof leaves the attempt slot invalidated.
        """
        assert job.thread is not None and job.handle is not None
        try:
            response = await asyncio.wait_for(
                asyncio.shield(self._worker(
                    job, job.thread.read(include_turns=True),
                )),
                timeout=self._cleanup_timeout_seconds,
            )
            native = getattr(response, "thread", None)
            if getattr(native, "id", None) != job.thread.id:
                return
            matches = [
                turn for turn in getattr(native, "turns", ())
                if getattr(turn, "id", None) == job.handle.id
            ]
            if len(matches) != 1 or not _exact_terminal(matches[0], job.handle):
                return
            job.terminal_observed = True
            if job.run is not None and not job.run.done():
                # Exact native terminal proof permits closing the lone stale
                # observer. This does not cancel an acquisition or mutation RPC.
                job.run.cancel()
        except Exception as error:
            logger.warning(
                "automatic Thread naming terminal read unavailable error_type=%s",
                type(error).__name__,
            )


def _status(result: object) -> str | None:
    status = getattr(result, "status", None)
    value = getattr(status, "value", status)
    return value if isinstance(value, str) else None


def _exact_terminal(result: object, handle: NativeTurnHandle) -> bool:
    return (
        getattr(result, "id", None) == handle.id
        and _status(result) in {"completed", "interrupted", "failed"}
    )
