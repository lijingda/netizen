"""One owned Claude client per admitted ordinary Binding execution."""
from __future__ import annotations

import asyncio
from contextlib import aclosing
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import time
from typing import Any, cast
from uuid import UUID, uuid4

from claude_agent_sdk import AssistantMessage, Message, RateLimitEvent, ResultMessage, StreamEvent, SystemInitData, SystemMessage

from ..backends import BackendPolicy
from ..bindings import BindingNotFound, BindingStore, ProjectDeleting, ProjectNotFound, ScopeNotFound, ThreadBinding
from ..bindings import BindingTaskFeedback, BindingTurnSettings
from ..domain import ActiveState, MessageContextAnchor, MentionContextMode
from ..session_settings import require_local_settings
from ..ordinary_sessions import (
    LocalExecutionSnapshot, LocalStatusSnapshot, OrdinaryCompletionHandler, OrdinaryConclusion, OrdinaryEvent, OrdinaryInputRejected, OrdinarySessionPort, OrdinaryTerminalQueued,
    PreparedPrompt, SessionShutdownPort, StopPreview,
)
from .claude_status import ClaudeObservations, DEFAULT_OBSERVATIONS, record_observation
from .claude_client import BackgroundTask, ClaudeBackground, ClaudeClient, ClaudeClients, Evidence, single_message, user_message
from .contracts import (
    ActiveTurnSnapshot, ContextCursorCommit, StopAcknowledger, StopDisposition, Submission, SubmissionAdmission,
    SubmitDisposition, TerminalStateUnknown, ThreadActivityChanged, TurnActivitySnapshot,
    TurnObservationUnavailableOutcome,
)

logger = logging.getLogger(__name__)
UNKNOWN = "本次 Claude 提交结果未知，请检查原会话；系统不会自动重发。"
BACKGROUND_PENDING = "首答已发，后台任务仍在运行"
WAKE_STOP = "Claude 正在处理后台完成轮，无法精确停止这一轮。"
MULTI_STOP = "有多个后台任务，无法确定要停止哪一个。"
DEGRADED = "本轮结果无法确认，后续回答不再发送。后台任务可能仍在运行，Netizen 无法确认或终止它。客户端关闭后可发送新消息继续。"
ABANDON_HELP = "所有者可发送以下命令，关闭当前客户端并终止其后台工作。\n/stop abandon-background"
CLOSE_FAILED = "无法确认 Claude 客户端已关闭，本会话暂不可再提交。"
CLOSING_CONCLUDED = "这次 Claude 执行已经结束，结果正在送出。"
CLOSING = "正在关闭 Claude 客户端，执行结果尚未确认。"
DEFAULT_MODE = "Claude 的自动审核不可用，本次使用默认权限；未预先允许的操作会被拒绝。"
START_FAILED = "Claude 会话启动失败，本条输入未发送。"
START_CANCELLED = "已取消启动，本条输入未发送。"
STALE = "准备本条消息期间会话已变化，本条输入未发送。"
GRACE = "服务刚启动，请稍后重发"
CURSOR_FAILURE = "上下文边界未能持久化，服务已停止接收新任务，请重启后对账。"
# The pinned transport allows 5s each for its write lock, EOF, TERM and KILL waits.
_CLOSE_SECONDS = 22.0
_CONTROL_SECONDS = 5.0
_RECONCILE_SECONDS = 3.0
_START_SECONDS = 60.0
_WRITE_SECONDS = 30.0


@dataclass(slots=True)
class _BindingActivity:
    revision: int = 0
    last_execution_id: str | None = None


@dataclass(slots=True)
class _Execution:
    binding: ThreadBinding
    origin: object
    session: str
    input_id: str
    revision: int
    ready: asyncio.Future[Submission]
    receipt: asyncio.Event = field(default_factory=asyncio.Event)
    submit_finished: asyncio.Event = field(default_factory=asyncio.Event)
    client: ClaudeClient | None = None
    task: asyncio.Task[None] | None = None
    background: ClaudeBackground = field(init=False)
    delivery: asyncio.Task[None] | None = None
    visible: tuple[object, ...] | None = None
    close_task: asyncio.Task[bool] | None = None
    hold_notice: bool = False
    abandoned: bool = False
    started: bool = False
    writing: bool = False
    stopping: bool = False
    closing: bool = False
    close_finished: bool = False
    closed: bool = False
    concluded: bool = False
    delivery_finished: bool = False
    detached: bool = False
    stop_receipt: asyncio.Event | None = None
    permission_notice: str | None = None


    def __post_init__(self) -> None:
        self.background = ClaudeBackground(self.session, self.input_id, before_resumed_input=self.binding.native_thread_id is not None)


class _ReaderEnded(Exception):
    """The SDK pull ended independently of cancellation of its owning execution."""


async def _messages(client: ClaudeClient, changed: asyncio.Event) -> AsyncGenerator[Message | None]:
    read: asyncio.Future[Message] | None = None
    signal = asyncio.create_task(changed.wait())
    try:
        stream = client.receive_messages()
        while True:
            if read is None:
                read = asyncio.ensure_future(anext(stream))
            await asyncio.wait((read, signal), return_when=asyncio.FIRST_COMPLETED)
            if signal.done():
                changed.clear()
                signal = asyncio.create_task(changed.wait())
                yield None
            if read.done():
                msg = read.result()
                read = None
                yield msg
    except (Exception, asyncio.CancelledError) as error:
        owner_task = asyncio.current_task()
        if isinstance(error, asyncio.CancelledError) and owner_task is not None and owner_task.cancelling():
            raise
        raise _ReaderEnded from None
    finally:
        pending: list[asyncio.Future[Any]] = [signal] if read is None else [read, signal]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


# Only fixed public labels may reach Feishu. Custom tools and MCP names can contain private identifiers.
_DENIAL_TOOL_LABELS = frozenset({
    "Agent", "Bash", "Edit", "Glob", "Grep", "NotebookEdit", "Read", "Skill", "WebFetch", "WebSearch", "Write",
})


def _denial_notice(denials: object) -> str | None:
    if not isinstance(denials, list) or not denials:
        return None
    names: list[str] = []
    for entry in denials[:32]:
        name = entry.get("tool_name") if isinstance(entry, dict) else entry
        if isinstance(name, str) and name in _DENIAL_TOOL_LABELS and name not in names:
            names.append(name)
            if len(names) == 6:
                break
    tools = "（" + "、".join(names) + "）" if names else ""
    return f"本次有 {len(denials)} 个操作被 Claude Code 的权限设置拒绝{tools}。如需允许，请在 Claude Code 设置中调整权限规则。"


def _native_uuid(value: object) -> str | None:
    if not isinstance(value, str) or len(value) != 36:
        return None
    try:
        return value if str(UUID(value)) == value else None
    except ValueError:
        return None


def _terminal(message: ResultMessage) -> tuple[str, str | None, str | None]:
    match (message.subtype, message.terminal_reason, message.is_error):
        case ("success", "completed", False):
            return "completed", message.result, None
        case ("error_during_execution", "aborted_streaming" | "aborted_tools", True):
            return "interrupted", None, None
        case ("error_max_turns", "max_turns", True):
            return "failed", None, "Claude 已达到本次执行的轮数上限。"
        case ("success", "api_error", True):
            status = message.api_error_status
            detail = f"（HTTP {status}）" if type(status) is int and 100 <= status <= 599 else ""
            return "failed", None, f"Claude 请求失败{detail}。"
        case ("error_max_budget_usd", _, True):
            return "failed", None, "Claude 已达到本次执行的预算上限。"
        case (subtype, reason, True) if subtype in {
            "error_during_execution", "error_max_turns", "error_max_structured_output_retries",
        }:
            safe_reason = reason if reason in {
                "max_turns", "completed", "api_error", "max_budget_usd", "model_error",
                "aborted_streaming", "aborted_tools", "max_structured_output_retries",
            } else "unknown"
            return "failed", None, f"Claude 执行失败（{subtype}/{safe_reason}）。"
        case _:
            raise TerminalStateUnknown(UNKNOWN)


class ClaudeOrdinarySessions(OrdinarySessionPort, SessionShutdownPort):
    def __init__(
        self, *, bindings: BindingStore, clients: ClaudeClients, owner_id: str,
        on_completion: OrdinaryCompletionHandler, notice: Callable[[object, str], Awaitable[None]],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not owner_id:
            raise ValueError("Claude 实例需要一名所有者。")
        self._bindings = bindings
        self.observations = ClaudeObservations()
        self._clients = clients
        self._owner = owner_id
        self.backend_policy = BackendPolicy("claude", owner_id)
        self._completion = on_completion
        self._notice = notice
        self._clock = clock
        self._opens_at: float | None = None
        self._admission_error: str | None = None
        self._shutdown_deadline: float | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._activity: dict[str, _BindingActivity] = {}
        self._active: dict[str, _Execution] = {}
        self._tasks: dict[asyncio.Task[None], _Execution] = {}
        self._deliveries: set[asyncio.Task[None]] = set()
        self._closes: set[asyncio.Task[bool]] = set()
        self._owned_cwds: dict[str, Path] = {}
        self._controls: set[asyncio.Task[None]] = set()
        self._unavailable: set[str] = set()

    def open_admission(self) -> None:
        if self._opens_at is None and self._admission_error is None:
            started = self._clock()
            self._opens_at = started + 2.0
            logger.info("Claude admission grace started pid=%d monotonic=%r deadline=%r",
                        os.getpid(), started, self._opens_at)

    def require_admission(self) -> None:
        if self._admission_error is not None:
            raise OrdinaryInputRejected(self._admission_error)
        if self._opens_at is None or self._clock() < self._opens_at:
            raise OrdinaryInputRejected(GRACE)

    def _lock(self, binding_id: str) -> asyncio.Lock:
        if binding_id not in self._locks:
            self._locks[binding_id] = asyncio.Lock()
        return self._locks[binding_id]

    def _advance(self, binding_id: str, *, execution_id: str | None = None) -> int:
        activity = self._activity.setdefault(binding_id, _BindingActivity())
        activity.revision += 1
        if execution_id is not None:
            activity.last_execution_id = execution_id
        return activity.revision

    async def binding_pointer_changed(self, previous_binding_id: str | None, current_binding_id: str | None) -> None:
        if previous_binding_id == current_binding_id:
            return
        for binding_id in (previous_binding_id, current_binding_id):
            if binding_id is not None:
                async with self._lock(binding_id):
                    self._advance(binding_id)

    def _idle(self, binding_id: str) -> None:
        self.require_admission()
        if binding_id in self._unavailable:
            raise OrdinaryInputRejected(CLOSE_FAILED)
        if binding_id in self._active and self._active[binding_id].closing:
            raise OrdinaryInputRejected("上一轮正在收尾，本条消息未发送。")
        if active := self._active.get(binding_id):
            if active.hold_notice and active.background.abandonable:
                raise OrdinaryInputRejected(self._abandon_notice(active))
            if active.background.first_done:
                if active.background.handoff:
                    raise OrdinaryInputRejected(BACKGROUND_PENDING)
                raise OrdinaryInputRejected("上一轮正在收尾，本条消息未发送。")
            raise OrdinaryInputRejected("当前会话正在执行，本条消息未发送。")

    def _current(self, binding_id: str) -> ThreadBinding:
        try:
            binding = self._bindings.get(binding_id)
            if self._bindings.get_scope(binding.scope_key).active_binding_id != binding_id:
                raise OrdinaryInputRejected(STALE)
            self._bindings.require_project_not_deleting(binding.project_alias)
            self._bindings.get_project(binding.project_alias)
            self._bindings.worktrees.require_binding_not_deleting(binding_id)
        except (BindingNotFound, ScopeNotFound, ProjectDeleting, ProjectNotFound):
            raise OrdinaryInputRejected(STALE) from None
        return binding

    def _admission(self, binding: ThreadBinding) -> SubmissionAdmission:
        return SubmissionAdmission(binding.id, self._activity.get(binding.id, _BindingActivity()).revision, None, None,
                                   binding.settings_revision, binding.context_revision, binding.feedback_revision)

    async def capture_submission_admission(self, binding_id: str) -> SubmissionAdmission:
        async with self._lock(binding_id):
            self._idle(binding_id)
            return self._admission(self._current(binding_id))

    async def submit(
        self, *, binding: ThreadBinding, cwd: Path | None, input: PreparedPrompt, owner_id: str,
        origin: object, admission: SubmissionAdmission, context_commit: ContextCursorCommit | None,
        skill_names: tuple[str, ...],
    ) -> Submission:
        if owner_id != self._owner:
            raise OrdinaryInputRejected("这个 Claude 实例仅限所有者使用。")
        if skill_names:
            raise OrdinaryInputRejected("Claude 试点暂不支持【显式 Skill 调用】。")
        async with self._lock(binding.id):
            self._idle(binding.id)
            current = self._current(binding.id)
            if (current != binding or self._admission(current) != admission or
                    (context_commit and context_commit.expected_context_revision != binding.context_revision)):
                raise OrdinaryInputRejected(STALE)
            input_id = str(uuid4())
            message = user_message(input, input_id)
            execution = _Execution(binding, origin, binding.native_thread_id or str(uuid4()), input_id,
                                   self._advance(binding.id, execution_id=input_id), asyncio.get_running_loop().create_future())
            self._active[binding.id] = execution
            execution.task = asyncio.create_task(self._run(execution, cwd, message, context_commit))
            self._tasks[execution.task] = execution
            execution.task.add_done_callback(self._tasks.pop)
        try:
            return await asyncio.shield(execution.ready)
        except asyncio.CancelledError:
            execution.detached = True
            execution.receipt.set()
            # A disconnected submitter can abandon preparation but cannot revoke a possible write.
            if not execution.ready.done():
                execution.ready.cancel()
            elif not execution.ready.cancelled():
                execution.ready.exception()  # Consume an already reconciled submission failure.
            if not execution.writing and not execution.closing:
                self._cancel_opening(execution)
            raise
        except Exception:
            # A failed submit returns no Submission to release the receipt gate.
            execution.receipt.set()
            raise
        finally:
            execution.submit_finished.set()

    @staticmethod
    def _cancel_opening(execution: _Execution) -> None:
        execution.stopping = True
        if execution.started and execution.task is not None and not execution.task.cancelling():
            execution.task.cancel()

    def _check_opening(self, execution: _Execution) -> None:
        self.require_admission()
        if execution.stopping:
            raise OrdinaryInputRejected(START_CANCELLED)
        binding_id = execution.binding.id
        if self._activity[binding_id].revision != execution.revision or self._current(binding_id) != execution.binding:
            raise OrdinaryInputRejected(STALE)

    def _candidate_dirs(self, binding: ThreadBinding) -> tuple[Path, ...]:
        project = Path(self._bindings.get_project(binding.project_alias).cwd)
        return (Path(binding.working_cwd), project) if binding.working_cwd else (project,)

    async def _cwd(self, execution: _Execution, requested: Path | None) -> Path:
        binding = execution.binding
        if binding.native_thread_id is not None:
            try:
                UUID(execution.session)
                cwd = await self._clients.session_cwd(execution.session, self._candidate_dirs(binding))
            except Exception:
                cwd = None
            if cwd is None or not cwd.is_absolute() or not cwd.is_dir():
                raise OrdinaryInputRejected("无法恢复原 Claude 会话的实际目录，本条输入未发送。请检查原会话或使用 /new 新建会话。")
            return cwd.resolve()
        return self._creation_cwd(binding, requested)

    def _creation_cwd(self, binding: ThreadBinding, requested: Path | None) -> Path:
        project = self._bindings.get_project(binding.project_alias)
        expected = Path(binding.working_cwd or project.cwd)
        if requested is None or not requested.is_absolute() or not requested.is_dir() or requested.resolve() != expected.resolve():
            raise OrdinaryInputRejected("会话工作目录已变化，本条输入未发送。")
        return requested.resolve()

    async def _run(self, execution: _Execution, cwd: Path | None, message: dict[str, Any],
                   commit: ContextCursorCommit | None) -> None:
        execution.started = True
        binding = execution.binding
        failure: OrdinaryInputRejected | TerminalStateUnknown | None = None
        try:
            self._check_opening(execution)
            async with asyncio.timeout(_START_SECONDS):
                actual = await self._cwd(execution, cwd)
                self._check_opening(execution)
                self._bindings.require_actual_cwd_not_deleting(actual)
                execution.background.snapshot_changed = lambda: self._refresh(execution)
                execution.client = self._clients.create(execution.session, actual, resume=binding.native_thread_id is not None,
                                                       background=execution.background)
                self._owned_cwds[binding.id] = actual
                await execution.client.connect()
            async with self._lock(binding.id):
                self._check_opening(execution)
                self._bindings.worktrees.require_cwd_not_deleting(actual)
                if binding.native_thread_id is None:
                    if self._creation_cwd(binding, cwd) != actual:
                        raise OrdinaryInputRejected(STALE)
                    self._bindings.assign_native_thread_id(binding.id, execution.session)
                execution.writing = True
                self._refresh(execution)
            confirmed_write = True
            try:
                async with asyncio.timeout(_WRITE_SECONDS):
                    await execution.client.query(single_message(message))
            except Exception:
                # A lost receipt may already have written H1. Its one reader reconciles it.
                confirmed_write = False
            del message
            if confirmed_write:
                self._accepted(execution)
            bg = execution.background
            async with aclosing(_messages(execution.client, bg.changed)) as native:
                try:
                    async with aclosing(self._read(execution, native)) as stream:
                        event = await self._observe(execution, stream, actual, commit, confirmed_write=confirmed_write)
                        self._accepted(execution)
                        execution.concluded = True
                        self._queue_delivery(execution, event)
                        await self._follow(execution, stream, actual)
                except _ReaderEnded:
                    if bg.unresolved_work:
                        bg.degraded = True
                    elif not execution.concluded:
                        failure = TerminalStateUnknown(UNKNOWN)
                except Exception:
                    bg.degraded = True
                if bg.degraded:
                    if not execution.ready.done():
                        execution.ready.set_exception(OrdinaryTerminalQueued(DEGRADED))
                    self._queue_delivery(execution, TurnObservationUnavailableOutcome(
                        binding.id, execution.session, execution.input_id, self._owner, execution.origin, TerminalStateUnknown(DEGRADED)))
                    async def drain() -> None:
                        async for _ in native:
                            pass
                    reader = asyncio.create_task(drain())
                    try:
                        await self._close_client(execution)
                        if execution.close_task is not None and not execution.close_task.done():
                            # A timed-out disconnect still owns the transport until it
                            # returns or the SDK stream ends. Admission is quarantined.
                            await asyncio.wait((execution.close_task, reader), return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        reader.cancel()
                        await asyncio.gather(reader, return_exceptions=True)
        except OrdinaryInputRejected as exc:
            failure = OrdinaryInputRejected(str(exc))
        except (Exception, asyncio.CancelledError):
            if not execution.writing:
                failure = OrdinaryInputRejected(START_CANCELLED if execution.stopping else START_FAILED)
            elif not execution.abandoned and not execution.concluded and not execution.background.degraded:
                failure = TerminalStateUnknown(UNKNOWN)
        finally:
            await self._finish(execution, failure)

    @staticmethod
    def _accepted(execution: _Execution) -> None:
        if not execution.ready.done():
            binding = execution.binding
            execution.ready.set_result(Submission(SubmitDisposition.STARTED, binding.id, execution.session,
                                                  execution.input_id, execution.receipt.set,
                                                  binding.task_feedback, binding.feedback_revision))

    def _close_quarantine(self, execution: _Execution, *, unavailable: bool) -> None:
        key = execution.binding.id
        if (key in self._unavailable) != unavailable:
            if unavailable:
                self._unavailable.add(key)
            else:
                self._unavailable.discard(key)
            execution.revision = self._advance(key)

    async def _disconnect(self, execution: _Execution, client: ClaudeClient, may_close: Callable[[], bool] | None) -> bool:
        binding_id = execution.binding.id
        if may_close is not None and not may_close():
            return False
        execution.closing = True
        self._refresh(execution)
        try:
            await client.disconnect()
        except (Exception, asyncio.CancelledError):
            self._close_quarantine(execution, unavailable=True)
        else:
            execution.closed = True
            self._owned_cwds.pop(binding_id, None)
            self._close_quarantine(execution, unavailable=False)
            if execution.background.degraded:
                self._release(execution)
        return True

    async def _finish(
        self, execution: _Execution, failure: OrdinaryInputRejected | TerminalStateUnknown | None,
    ) -> None:
        binding = execution.binding
        execution.closing = True
        self._refresh(execution)
        if execution.concluded:
            try:
                assert execution.delivery is not None
                tail = execution.delivery
                await asyncio.gather(self._close_client(execution), tail, return_exceptions=True)
                while execution.delivery is not tail:
                    tail = execution.delivery
                    await asyncio.gather(tail, return_exceptions=True)
            finally:
                if not execution.close_finished and not execution.closed:
                    self._close_quarantine(execution, unavailable=True)
                self._release(execution)
        else:
            await self._close_client(execution)
            self._release(execution)
        if failure is not None:
            # A confirmed close releases the UUID for the next explicit input.
            # Delivery of the old outcome may still be waiting on Feishu.
            await self._report_failure(execution, failure)
        execution.background.snapshot_changed = None
        if binding.id in self._unavailable and not execution.background.degraded:
            await self._tell(execution, CLOSE_FAILED)

    async def _report_failure(self, e: _Execution, failure: OrdinaryInputRejected | TerminalStateUnknown) -> None:
        if e.stop_receipt is not None:
            await e.stop_receipt.wait()
        if e.permission_notice:
            notice, e.permission_notice = e.permission_notice, None
            await self._tell(e, notice)
        if not e.ready.done():
            e.ready.set_exception(failure)
            await e.submit_finished.wait()
            if not e.detached:
                return
        if isinstance(failure, TerminalStateUnknown):
            await self._queue_delivery(e, TurnObservationUnavailableOutcome(
                e.binding.id, e.session, e.input_id, self._owner, e.origin, failure))

    async def _close_client(self, execution: _Execution, may_close: Callable[[], bool] | None = None) -> bool:
        if execution.close_finished:
            return True
        if execution.client is not None:
            close = execution.close_task
            if close is None:
                close = asyncio.create_task(self._disconnect(execution, execution.client, may_close))
                execution.close_task = close
                self._closes.add(close)
                close.add_done_callback(self._closes.discard)
            await asyncio.wait([close], timeout=self._remaining(_CLOSE_SECONDS))
            if close.done() and not close.result():
                execution.close_task = None
                if may_close is None:
                    return await self._close_client(execution)
                return False
            if not close.done():
                self._close_quarantine(execution, unavailable=True)
        execution.close_finished = True
        return True

    def _queue_delivery(self, execution: _Execution, event: OrdinaryEvent) -> asyncio.Task[None]:
        previous, notice = execution.delivery, execution.permission_notice
        execution.permission_notice = None
        execution.delivery_finished = False

        async def deliver() -> None:
            if previous is not None:
                await asyncio.shield(asyncio.gather(previous, return_exceptions=True))
            await self._deliver(execution, event, notice)
            if execution.delivery is asyncio.current_task():
                execution.delivery_finished = True
                self._refresh(execution)

        execution.delivery = asyncio.create_task(deliver())
        self._deliveries.add(execution.delivery)
        execution.delivery.add_done_callback(self._deliveries.discard)
        return execution.delivery

    async def _deliver(self, execution: _Execution, event: OrdinaryEvent, notice: str | None) -> None:
        try:
            await execution.receipt.wait()
            if execution.stop_receipt is not None:
                await execution.stop_receipt.wait()
            if notice and not execution.background.degraded:
                await self._tell(execution, notice)
            async with asyncio.timeout(self._remaining(30.0)):
                if isinstance(event, OrdinaryConclusion) and execution.background.degraded:
                    return
                await self._completion(event)
        except (Exception, asyncio.CancelledError):
            logger.warning("Claude completion delivery failed for Binding %s", execution.binding.id)

    def _release(self, execution: _Execution) -> None:
        if self._active.get(execution.binding.id) is execution:
            del self._active[execution.binding.id]
            self._advance(execution.binding.id)

    def _schedule(self, operation: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(operation)
        self._controls.add(task)
        task.add_done_callback(self._controls.discard)

    async def _tell(self, execution: _Execution, text: str) -> None:
        try:
            async with asyncio.timeout(self._remaining(_CONTROL_SECONDS)):
                await self._notice(execution.origin, text)
        except Exception:
            logger.warning("Claude notice delivery failed for Binding %s", execution.binding.id)

    async def _invalid_init(self, execution: _Execution) -> None:
        bg = execution.background
        if bg.handoff or bg.crons:
            bg.degraded = True
            raise TerminalStateUnknown(UNKNOWN)
        if not bg.human_open:
            raise TerminalStateUnknown(UNKNOWN)
        client = execution.client
        assert client is not None
        try:
            async with asyncio.timeout(self._remaining(_CONTROL_SECONDS)):
                await client.interrupt()
        except Exception:
            pass
        raise TerminalStateUnknown(UNKNOWN)

    async def _observe(self, execution: _Execution, stream: AsyncIterator[Message | None], cwd: Path,
                       commit: ContextCursorCommit | None, *, confirmed_write: bool = True) -> OrdinaryEvent:
        def commit_intake() -> None:
            nonlocal commit
            if commit is not None:
                cursor, commit = commit, None
                try:
                    self._bindings.commit_context_anchor(binding_id=execution.binding.id,
                                                         expected_context_revision=cursor.expected_context_revision,
                                                         anchor=cursor.anchor)
                except Exception:
                    self.close_admission(reason=CURSOR_FAILURE)
                    self._schedule(self._tell(execution, f"Claude 已接收本次任务，但{CURSOR_FAILURE}"))

        loop = asyncio.get_running_loop()
        bg = execution.background
        reconciliation = None if confirmed_write else loop.call_later(self._remaining(_RECONCILE_SECONDS), bg.changed.set)

        def accepted() -> None:
            nonlocal confirmed_write, reconciliation
            if not confirmed_write:
                self._accepted(execution)
                confirmed_write = True
                if reconciliation is not None:
                    reconciliation.cancel()
                    reconciliation = None
            commit_intake()

        initialized = False
        human_init = False
        # Wake the reader on expiry without cancelling its in-flight SDK pull.
        try:
            async for msg in stream:
                assert execution.client is not None
                verdict = bg.observe(msg) if msg is not None else None
                self._refresh(execution)
                if reconciliation is not None and reconciliation.when() <= loop.time():
                    raise TerminalStateUnknown(UNKNOWN)
                if msg is None or verdict is not Evidence.NEW or bg.degraded:
                    continue
                if (isinstance(msg, (AssistantMessage, StreamEvent)) and msg.parent_tool_use_id is None
                        and msg.session_id == execution.session
                        and (msg.user_message_uuid == execution.input_id or msg.user_message_uuids == [execution.input_id])
                        and msg.user_message_uuid in (None, execution.input_id)
                        and msg.user_message_uuids in (None, [], [execution.input_id])):
                    accepted()
                if not initialized and isinstance(msg, AssistantMessage):
                    await self._invalid_init(execution)
                if isinstance(msg, SystemMessage) and msg.subtype == "init":
                    data = cast(SystemInitData, msg.data)
                    init_cwd = data.get("cwd")
                    if (data.get("session_id") != execution.session or not isinstance(init_cwd, str)
                            or not Path(init_cwd).is_absolute() or Path(init_cwd).resolve() != cwd):
                        raise TerminalStateUnknown(UNKNOWN)
                    human_init = msg.data.get("user_message_uuids") == [execution.input_id]
                    if human_init:
                        accepted()
                    mode = data.get("permissionMode")
                    if mode not in ("auto", "default"):
                        await self._invalid_init(execution)
                    if human_init:
                        with record_observation():
                            self.observations.init(execution.binding.id, execution.session, execution.input_id, data.get("model"), str(cwd))
                    initialized = True
                    if mode == "default":
                        execution.permission_notice = DEFAULT_MODE
                if human_init and isinstance(msg, RateLimitEvent) and msg.session_id == execution.session:
                    with record_observation():
                        self.observations.rate(execution.binding.id, execution.session, execution.input_id, msg.rate_limit_info)
                if not isinstance(msg, ResultMessage):
                    continue
                if msg.session_id != execution.session:
                    raise TerminalStateUnknown(UNKNOWN)
                ids = [] if msg.user_message_uuids is None else msg.user_message_uuids
                single = msg.user_message_uuid
                origin = msg.origin
                # The SDK says any origin kind other than "human" is not a person's
                # input, including kinds newer CLIs add (types.MessageOriginKind).
                injected = isinstance(origin, dict) and origin.get("kind") not in (None, "human")
                if single or ids:
                    if single != execution.input_id and execution.input_id not in ids:
                        bg.skip_foreign_result()
                        self._refresh(execution)
                        continue
                elif injected:
                    # For example, resuming after a killed background task replays its
                    # notification as a Result with no input attribution at all.
                    bg.skip_foreign_result()
                    self._refresh(execution)
                    continue
                else:
                    if reconciliation is None:
                        reconciliation = loop.call_later(self._remaining(_RECONCILE_SECONDS), bg.changed.set)
                    continue
                if (origin not in (None, {"kind": "human"}) or any(value != execution.input_id for value in ids)
                        or single not in (None, execution.input_id)):
                    bg.degraded = True
                    raise TerminalStateUnknown(UNKNOWN)
                accepted()
                bg.human_open = False
                if not initialized:
                    await self._invalid_init(execution)
                if denial := _denial_notice(msg.permission_denials):
                    execution.permission_notice = (f"{DEFAULT_MODE}\n{denial}" if execution.permission_notice else denial)
                status, answer, detail = _terminal(msg)
                with record_observation():
                    self.observations.result(execution.binding.id, execution.session, execution.input_id, msg)
                result_id = _native_uuid(msg.uuid)
                if result_id is None:
                    bg.degraded = True

                bg.first_done = True
                bg.finish_turn(attributed=True)
                self._refresh(execution)
                bg.credit(msg, result_id)
                return OrdinaryConclusion(execution.binding.id, execution.session, execution.input_id, self._owner, execution.origin,
                                          status, answer, detail, task_feedback=execution.binding.task_feedback,
                                          feedback_revision=execution.binding.feedback_revision, cwd=cwd)
        finally:
            if reconciliation is not None:
                reconciliation.cancel()
        raise TerminalStateUnknown(UNKNOWN)

    @staticmethod
    def _abandon_notice(execution: _Execution) -> str:
        return f"Claude 有会话定时唤醒。{ABANDON_HELP} {execution.input_id}"

    def _hold(self, execution: _Execution) -> None:
        if not execution.hold_notice:
            execution.hold_notice = True
            self._refresh(execution)
            self._schedule(self._tell(execution, self._abandon_notice(execution)))

    async def abandon_background(self, binding_id: str | None, generation: str | None, owner_id: str) -> str:
        stale = "后台交接目标已变化或不可放弃，本次未关闭。"
        if owner_id != self._owner:
            raise OrdinaryInputRejected(stale)
        if binding_id is None:
            binding_id = next((key for key, e in self._active.items() if e.input_id == generation), None)
            if binding_id is None:
                raise OrdinaryInputRejected(stale)
        async with self._lock(binding_id):
            e = self._active.get(binding_id)
            if generation is None:
                if e is None:
                    raise OrdinaryInputRejected("当前会话没有可放弃的后台执行。")
                current = self._bindings.active_binding(e.binding.scope_key)
                if current is None or current.id != binding_id:
                    raise OrdinaryInputRejected(stale)
                generation = e.input_id
            if (self._shutdown_deadline is not None or e is None or e.input_id != generation
                    or not e.hold_notice or not e.background.abandonable or e.closing):
                raise OrdinaryInputRejected(stale)
            label = getattr(e.origin, "id", None) or e.input_id
            tasks = e.background.pending_tasks | set((e.background.members or {}).values())
            e.abandoned = e.closing = True
            self._refresh(e)
            assert e.task is not None
            e.task.cancel()
            return f"已请求关闭原消息 {label} 的客户端并终止其后台工作。任务：{len(tasks)}，会话定时唤醒：{len(e.background.crons)}。"

    async def _read(self, e: _Execution, stream: AsyncIterator[Message | None]) -> AsyncGenerator[Message | None]:
        bg = e.background
        deadlines: dict[tuple[str, str], float] = {}
        loop = asyncio.get_running_loop()
        while not bg.degraded:
            if bg.closable and await self._close_client(e, lambda: bg.closable):
                return
            now = loop.time()
            gaps = bg.pending_proofs() if bg.first_done else set()
            deadlines = {gap: deadlines.get(gap, now + self._remaining(_RECONCILE_SECONDS)) for gap in gaps}
            if any(end <= now for end in deadlines.values()):
                bg.degraded = True
                return
            if bg.abandonable:
                self._hold(e)
            else:
                e.hold_notice = False
            self._refresh(e)
            alarm = loop.call_at(min(deadlines.values()), bg.changed.set) if deadlines else None
            try:
                yield await anext(stream)
            finally:
                if alarm is not None:
                    alarm.cancel()

    async def _follow(self, e: _Execution, stream: AsyncIterator[Message | None], cwd: Path) -> None:
        bg = e.background
        wake_id: str | None = None
        activity = self._activity[e.binding.id]
        async for msg in stream:
            if msg is None:
                continue
            was_wake = bg.wake
            previous_turn = bg.turn_number
            verdict = bg.observe(msg)
            if verdict is not Evidence.NEW or bg.degraded:
                continue
            if bg.wake and not was_wake:
                activity.last_execution_id = wake_id = None
            if isinstance(msg, SystemMessage) and msg.subtype == "init" and bg.turn_number != previous_turn:
                directory = msg.data.get("cwd")
                mode = msg.data.get("permissionMode")
                if (not isinstance(directory, str) or not Path(directory).is_absolute()
                        or Path(directory).resolve() != cwd or mode not in ("auto", "default")):
                    bg.degraded = True
                    bg.wake_context = False
                activity.last_execution_id = wake_id = None
                raw_id = _native_uuid(msg.data.get("uuid"))
                if (not bg.degraded and msg.data.get("user_message_uuids") in (None, [])
                        and raw_id and raw_id != e.input_id):
                    activity.last_execution_id = wake_id = raw_id
                    with record_observation():
                        self.observations.init(e.binding.id, e.session, wake_id, msg.data.get("model"), str(cwd))
                e.permission_notice = DEFAULT_MODE if mode == "default" else None
            if (wake_id and bg.turn_open and not bg.degraded and isinstance(msg, RateLimitEvent)
                    and msg.session_id == e.session):
                with record_observation():
                    self.observations.rate(e.binding.id, e.session, wake_id, msg.rate_limit_info)
            if isinstance(msg, ResultMessage):
                self._wake_terminal(e, msg, cwd, wake_id)
            bg.settle_cancellations()
            self._refresh(e)

    def _wake_terminal(self, e: _Execution, msg: ResultMessage, cwd: Path, wake_id: str | None) -> None:
        bg = e.background
        if msg.session_id != e.session:
            bg.degraded = True
            return
        result_id = _native_uuid(msg.uuid)
        task = bg.wake_result(msg)
        if not task or msg.user_message_uuid is not None or msg.user_message_uuids not in (None, []):
            # A second terminal for H1 cannot be delivered a second time. A foreign
            # native wake never inherits H1's Feishu destination.
            if msg.user_message_uuid == e.input_id and not msg.origin and not bg.wake:
                return
            bg.degraded = True
            bg.finish_turn(attributed=False)
            return
        if task.settled:
            if bg.turn_open:
                bg.degraded = True
                bg.finish_turn(attributed=False)
            return
        if not bg.wake_context or not bg.turn_open:
            bg.degraded = True
            task.settled = True
            bg.finish_turn(attributed=False)
            return
        empty_receipt = (msg.subtype == "success" and msg.terminal_reason is None and msg.is_error is False
                         and msg.result in (None, "") and not msg.errors and msg.structured_output is None)
        event: OrdinaryEvent | None = None
        if not empty_receipt or result_id is None:
            try:
                if result_id is None:
                    raise TerminalStateUnknown(UNKNOWN)
                status, answer, detail = _terminal(msg)
                if status == "completed" and not (answer and answer.strip()):
                    raise TerminalStateUnknown(UNKNOWN)
            except TerminalStateUnknown:
                bg.degraded = True
            else:
                if denial := _denial_notice(msg.permission_denials):
                    e.permission_notice = (f"{DEFAULT_MODE}\n{denial}" if e.permission_notice else denial)
                event = OrdinaryConclusion(e.binding.id, e.session, result_id, self._owner, e.origin, status, answer,
                    detail, task_feedback=e.binding.task_feedback, feedback_revision=e.binding.feedback_revision, cwd=cwd)
        bg.credit(msg, result_id)
        if event is not None:
            self._queue_delivery(e, event)
        task.settled = True
        bg.finish_turn(attributed=True)
        if wake_id and not bg.degraded:
            with record_observation():
                self.observations.result(e.binding.id, e.session, wake_id, msg)

    def occupying_bindings(self, directory: Path) -> frozenset[str]:
        target = directory.resolve()
        return frozenset(key for key, cwd in self._owned_cwds.items() if cwd.is_relative_to(target))

    async def resource_usage(self, binding_id: str) -> dict[str, str | None]:
        lock = self._lock(binding_id)
        async with lock:
            binding = self._bindings.get(binding_id)
            snapshot = self.local_snapshot(binding_id)
            owned = self._owned_cwds.get(binding_id)
            if owned is not None:
                return {"cwd": str(owned), "status": "active"}
            if binding.native_thread_id is None:
                return {"cwd": binding.working_cwd, "status": "active" if snapshot.turn else "idle"}
            return {"cwd": None, "status": "unknown", "reason": "Claude 试点不支持已建立会话的原生删除。"}

    async def delete_lazy_resource(self, binding_id: str) -> ThreadBinding:
        async with self._lock(binding_id):
            self._idle(binding_id)
            if self._bindings.get(binding_id).native_thread_id is not None:
                raise OrdinaryInputRejected("Claude 试点暂不支持【已建立的会话删除】。")
            removed = self._bindings.delete_binding(binding_id)
            self._advance(binding_id)
            return removed

    observation_defaults = DEFAULT_OBSERVATIONS

    def capture_status(self, binding_id: str) -> LocalStatusSnapshot:
        local = self.local_snapshot(binding_id)
        session = self._bindings.get(binding_id).native_thread_id
        execution_id = self._activity.get(binding_id, _BindingActivity()).last_execution_id
        samples = self.observation_defaults
        with record_observation():
            samples = self.observations.read(binding_id, session, execution_id)
        return local.capture(binding_id, session, samples)

    def _state(self, e: _Execution) -> ActiveState:
        bg = e.background
        if e.closing or bg.degraded:
            return ActiveState.CLOSING
        if e.hold_notice:
            return ActiveState.QUARANTINED
        if bg.wake:
            return ActiveState.WAKE
        if task := bg.single_task:
            return ActiveState.STOPPING if task.cancel_requested else ActiveState.BACKGROUND_TASK
        if bg.first_done:
            return ActiveState.PENDING if bg.handoff else ActiveState.CLOSING
        if bg.handoff:
            return ActiveState.HANDOFF
        if e.stopping:
            return ActiveState.STOPPING
        if e.writing and not bg.human_open:
            return ActiveState.AWAITING_INPUT
        return ActiveState.RUNNING

    def _refresh(self, e: _Execution) -> None:
        if self._active.get(e.binding.id) is not e:
            return
        bg = e.background
        task = bg.single_task
        signature = (self._state(e), task.task_id if task else None, bg.turn_number, bg.human_open,
                     e.hold_notice and bg.abandonable, e.closing and e.delivery_finished)
        if e.visible != signature:
            e.revision = self._advance(e.binding.id)
            e.visible = signature

    def local_snapshot(self, binding_id: str) -> LocalExecutionSnapshot:
        execution = self._active.get(binding_id)
        turn = None
        stop_acknowledgement = None
        if execution is not None:
            task = execution.background.single_task
            state = self._state(execution)
            turn = ActiveTurnSnapshot(binding_id, execution.session, execution.input_id, self._owner, state)
            if state in (ActiveState.RUNNING, ActiveState.BACKGROUND_TASK):
                stop_acknowledgement = self._stop_acknowledgement(execution, task)
        return LocalExecutionSnapshot(execution.revision if execution else self._activity.get(binding_id, _BindingActivity()).revision,
                                      turn, binding_id in self._unavailable, stop_acknowledgement)

    async def activate_exact(self, binding_id: str, *, context_anchor: MessageContextAnchor | None = None) -> ThreadBinding:
        self.require_admission()
        async with self._lock(binding_id):
            self.require_admission()
            binding = self._bindings.get(binding_id)
            self._bindings.require_project_not_deleting(binding.project_alias)
            if binding.message_context_mode is MentionContextMode.CATCH_UP and context_anchor is None:
                raise OrdinaryInputRejected("请从飞书 /resume 切换，以建立新的消息边界。")
            return self._bindings.activate(scope_key=binding.scope_key, binding_id=binding_id, context_anchor=context_anchor)

    async def configure_context_exact(
        self, *, binding_id: str, expected_settings_revision: int, expected_context_revision: int,
        expected_feedback_revision: int, settings: BindingTurnSettings | None, task_feedback: BindingTaskFeedback,
        message_context_mode: MentionContextMode, context_anchor: MessageContextAnchor | None,
    ) -> ThreadBinding:
        self.require_admission()
        async with self._lock(binding_id):
            self._idle(binding_id)
            require_local_settings(settings is not None, task_feedback)
            binding = self._bindings.set_configuration(
                binding_id=binding_id, expected_settings_revision=expected_settings_revision,
                expected_context_revision=expected_context_revision, expected_feedback_revision=expected_feedback_revision,
                settings=settings, task_feedback=task_feedback, message_context_mode=message_context_mode,
                context_anchor=context_anchor,
            )
            self._advance(binding_id)
            return binding

    def turn_activity(self, binding_id: str, *, thread_id: str, turn_id: str) -> TurnActivitySnapshot | None:
        return None

    def reply_blocked_by_lifecycle(self, binding_id: str) -> bool:
        return False

    async def preview_stop(self, binding: ThreadBinding) -> StopPreview | None:
        async with self._lock(binding.id):
            if binding.id in self._unavailable:
                raise OrdinaryInputRejected(CLOSE_FAILED)
            execution = self._active.get(binding.id)
            if execution is None:
                return None
            if execution.closing:
                # The client close can take several seconds (longer with background
                # tasks); saying "nothing is running" here reads as a lost answer.
                raise OrdinaryInputRejected(self._closing_notice(execution))
            task = self._stop_target(execution)
            return StopPreview(self._stop_acknowledgement(execution, task), execution.revision, execution.input_id)

    @staticmethod
    def _stop_acknowledgement(execution: _Execution, task: BackgroundTask | None) -> str:
        if task is not None:
            return "正在请求停止这个 Claude 后台任务。"
        return "正在请求停止当前 Claude 执行。" if execution.writing else "正在取消 Claude 会话启动。"

    def _stop_target(self, e: _Execution) -> BackgroundTask | None:
        bg = e.background
        if e.hold_notice and bg.abandonable:
            raise OrdinaryInputRejected(self._abandon_notice(e))
        if bg.degraded:
            raise OrdinaryInputRejected(CLOSING)
        elif task := bg.single_task:
            return task
        elif bg.first_done and not bg.wake and bg.members and len(bg.members) > 1:
            reason = MULTI_STOP
        elif bg.first_done and not bg.handoff:
            raise OrdinaryInputRejected(self._closing_notice(e))
        elif bg.handoff:
            reason = WAKE_STOP if bg.wake else "Claude 已进入后台交接，无法精确中断当前执行。"
        elif e.writing and not bg.human_open:
            raise OrdinaryInputRejected("尚未确认当前人工输入已开始执行，无法精确停止。")
        else:
            return None
        raise OrdinaryInputRejected(f"{reason}若稍后提示可放弃后台工作，{ABANDON_HELP} {e.input_id}")

    @staticmethod
    def _closing_notice(execution: _Execution) -> str:
        if execution.delivery_finished:
            return "这次 Claude 执行已经结束，客户端正在收尾。"
        return CLOSING_CONCLUDED if execution.concluded else CLOSING

    def _exact(self, binding_id: str, revision: int | None, turn_id: str | None) -> _Execution:
        execution = self._active.get(binding_id)
        if execution is None or (revision, turn_id) != (execution.revision, execution.input_id):
            raise ThreadActivityChanged("目标执行已变化，请刷新后重试。")
        if execution.closing:
            raise OrdinaryInputRejected(self._closing_notice(execution))
        return execution

    async def stop_exact(self, binding_id: str, *, acknowledge: StopAcknowledger | None = None,
                         expected_activity_revision: int | None = None,
                         expected_turn_id: str | None = None) -> StopDisposition:
        async with self._lock(binding_id):
            execution = self._exact(binding_id, expected_activity_revision, expected_turn_id)
            task = self._stop_target(execution)
            if (task.cancel_requested if task else execution.stopping):
                return StopDisposition.STOPPING
            if task:
                task.cancel_requested = True
            else:
                execution.stopping = True
            self._refresh(execution)
            if acknowledge is not None:
                execution.stop_receipt = asyncio.Event()
            client = execution.client if execution.writing else None
            if not execution.writing:
                self._cancel_opening(execution)
        stop_receipt = execution.stop_receipt
        acknowledge_success = False
        # Keep the captured client pinned. Completion waits for this stop's receipt attempt.
        try:
            if client is not None:
                async with asyncio.timeout(self._remaining(_CONTROL_SECONDS)):
                    if task:
                        await client.stop_task(task.task_id)
                    else:
                        await client.interrupt()
            acknowledge_success = True
        except (Exception, asyncio.CancelledError) as exc:
            async with self._lock(binding_id):
                if self._active.get(binding_id) is execution:
                    if task and not task.settled and not task.terminal:
                        execution.background.degraded = True
                        execution.background.changed.set()
                    elif task is None:
                        execution.stopping = False
                    self._refresh(execution)
            if isinstance(exc, asyncio.CancelledError):
                raise
            if (task and task.terminal) or (task is None and execution.concluded):
                return StopDisposition.NOT_RUNNING
            raise TerminalStateUnknown("无法确认 Claude 停止请求的结果。") from None
        finally:
            if acknowledge is not None and stop_receipt is not None:
                try:
                    async with asyncio.timeout(self._remaining(_CONTROL_SECONDS)):
                        if acknowledge_success:
                            await acknowledge()
                except Exception:
                    logger.warning("Claude stop receipt failed for Binding %s", binding_id)
                finally:
                    stop_receipt.set()
        if not execution.writing and execution.task is not None:
            await asyncio.wait([execution.task], timeout=self._remaining(_CLOSE_SECONDS))
        return StopDisposition.REQUESTED

    async def recheck_turn_exact(self, binding_id: str, *, expected_activity_revision: int,
                                 expected_turn_id: str) -> ActiveTurnSnapshot:
        async with self._lock(binding_id):
            execution = self._exact(binding_id, expected_activity_revision, expected_turn_id)
            self._stop_target(execution)
            snapshot = self.local_snapshot(binding_id)
            assert snapshot.turn is not None
            return snapshot.turn

    def _remaining(self, seconds: float) -> float:
        if self._shutdown_deadline is None:
            return seconds
        return max(0.0, min(seconds, self._shutdown_deadline - time.monotonic()))

    def close_admission(self, *, reason: str | None = None) -> None:
        if reason is not None:
            # Persistence failure closes intake while accepted executions keep their delivery budget.
            self._admission_error = reason
            return
        self._admission_error = "服务正在关闭，本条输入未发送。"
        if self._shutdown_deadline is None:
            self._shutdown_deadline = time.monotonic() + 60.0
        for execution in self._tasks.values():
            execution.receipt.set()

    async def _shutdown_stop(self, execution: _Execution) -> None:
        try:
            await self.stop_exact(execution.binding.id, expected_activity_revision=execution.revision, expected_turn_id=execution.input_id)
        except (OrdinaryInputRejected, ThreadActivityChanged, TerminalStateUnknown):
            pass

    async def interrupt_all(self) -> None:
        self.close_admission()
        for execution in self._active.values():
            if not execution.closing:
                task = asyncio.create_task(self._shutdown_stop(execution))
                self._controls.add(task)
                task.add_done_callback(self._controls.discard)
        try:
            if not await self.wait_idle(_CONTROL_SECONDS):
                await self.cancel_tasks()
        except asyncio.CancelledError:
            # The service's remaining drain budget can expire during interrupt.
            # Trigger each execution's owned finally even when there is no time to join.
            self._begin_client_cleanup()
            raise

    async def wait_idle(self, timeout: float) -> bool:
        deadline = time.monotonic() + self._remaining(timeout)
        while tasks := {task for task in (*self._tasks, *self._controls, *self._closes, *self._deliveries) if not task.done()}:
            _, pending = await asyncio.wait(tasks, timeout=max(0.0, deadline - time.monotonic()))
            if pending:
                return False
        return not self._unavailable

    def _begin_client_cleanup(self) -> None:
        for task in (*self._controls, *self._deliveries):
            if not task.cancelling():
                task.cancel()
        for execution in self._tasks.values():
            if execution.closing:
                continue
            if not execution.writing:
                self._cancel_opening(execution)
            elif execution.task is not None and not execution.task.cancelling():
                execution.task.cancel()

    async def cancel_tasks(self) -> None:
        self.close_admission()
        self._begin_client_cleanup()
        if not await self.wait_idle(_CLOSE_SECONDS):
            for task, execution in self._tasks.items():
                if execution.close_finished and execution.submit_finished.is_set():
                    task.cancel()
            if not await self.wait_idle(self._remaining(60.0)):
                raise RuntimeError("未能确认所有 Claude 客户端已关闭。")
