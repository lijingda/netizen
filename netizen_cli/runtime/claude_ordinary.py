"""One owned Claude client per admitted ordinary Binding execution."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import logging
from pathlib import Path
import time
from typing import Any, cast
from uuid import UUID, uuid4

from claude_agent_sdk import AssistantMessage, ResultMessage, SystemInitData, SystemMessage

from ..bindings import BindingNotFound, BindingStore, ProjectDeleting, ProjectNotFound, ScopeNotFound, ThreadBinding
from ..bindings import BindingTaskFeedback, BindingTurnSettings
from ..domain import ActiveState, MessageContextAnchor, MentionContextMode
from ..session_settings import require_local_settings
from ..ordinary_sessions import (
    LocalExecutionSnapshot, OrdinaryCompletionHandler, OrdinaryConclusion, OrdinaryEvent, OrdinaryInputRejected, OrdinarySessionPort,
    PreparedPrompt, SessionShutdownPort, StopPreview,
)
from .claude_client import ClaudeClient, ClaudeClients, single_message, user_message
from .contracts import (
    ActiveTurnSnapshot, ContextCursorCommit, StopAcknowledger, StopDisposition, Submission, SubmissionAdmission,
    SubmitDisposition, TerminalStateUnknown, ThreadActivityChanged, TurnActivitySnapshot,
    TurnObservationUnavailableOutcome,
)

logger = logging.getLogger(__name__)
UNKNOWN = "本次 Claude 提交结果未知，请检查原会话；系统不会自动重发。"
CLOSE_FAILED = "无法确认 Claude 客户端已关闭，本会话暂不可再提交。"
CLOSING_CONCLUDED = "这次 Claude 执行已经结束，结果正在送出。"
CLOSING = "这次 Claude 执行已经结束，正在关闭客户端。"
MODE_UNKNOWN = "无法确认 Claude 的有效权限模式，本次执行已停止。"
DEFAULT_MODE = "Claude 的自动审核不可用，本次使用默认权限；未预先允许的操作会被拒绝。"
START_FAILED = "Claude 会话启动失败，本条输入未发送。"
START_CANCELLED = "已取消启动，本条输入未发送。"
STALE = "准备本条消息期间会话已变化，本条输入未发送。"
GRACE = "服务刚启动，请稍后重发"
# The pinned transport allows 5s each for its write lock, EOF, TERM and KILL waits.
_CLOSE_SECONDS = 22.0
_CONTROL_SECONDS = 5.0
_RECONCILE_SECONDS = 3.0
_START_SECONDS = 60.0
_WRITE_SECONDS = 30.0


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
    started: bool = False
    writing: bool = False
    stopping: bool = False
    closing: bool = False
    close_finished: bool = False
    closed: bool = False
    concluded: bool = False
    detached: bool = False
    stop_receipt: asyncio.Event | None = None


def _terminal(message: ResultMessage) -> tuple[str, str | None, str | None]:
    if type(message.is_error) is not bool:
        raise TerminalStateUnknown(UNKNOWN)
    match (message.subtype, message.terminal_reason, message.is_error):
        case ("success", "completed", False):
            if message.result is not None and not isinstance(message.result, str):
                raise TerminalStateUnknown(UNKNOWN)
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
        self._clients = clients
        self._owner = owner_id
        self._completion = on_completion
        self._notice = notice
        self._clock = clock
        self._opens_at: float | None = None
        self._shutdown_deadline: float | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._revisions: dict[str, int] = {}
        self._active: dict[str, _Execution] = {}
        self._tasks: dict[asyncio.Task[None], _Execution] = {}
        self._closes: set[asyncio.Task[None]] = set()
        self._owned_cwds: dict[str, Path] = {}
        self._controls: set[asyncio.Task[None]] = set()
        self._unavailable: set[str] = set()

    def open_admission(self) -> None:
        if self._opens_at is None and self._shutdown_deadline is None:
            self._opens_at = self._clock() + 2.0

    def require_admission(self) -> None:
        if self._shutdown_deadline is not None:
            raise OrdinaryInputRejected("服务正在关闭，本条输入未发送。")
        if self._opens_at is None or self._clock() < self._opens_at:
            raise OrdinaryInputRejected(GRACE)

    def _lock(self, binding_id: str) -> asyncio.Lock:
        if binding_id not in self._locks:
            self._locks[binding_id] = asyncio.Lock()
        return self._locks[binding_id]

    def _advance(self, binding_id: str) -> int:
        revision = self._revisions.get(binding_id, 0) + 1
        self._revisions[binding_id] = revision
        return revision

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
        if binding_id in self._active:
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
        return SubmissionAdmission(binding.id, self._revisions.get(binding.id, 0), None, None,
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
                                   self._advance(binding.id), asyncio.get_running_loop().create_future())
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
                execution.ready.exception()
            if not execution.writing and not execution.closing:
                self._cancel_opening(execution)
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
        if self._revisions[binding_id] != execution.revision or self._current(binding_id) != execution.binding:
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
        event: OrdinaryEvent | None = None
        failure: OrdinaryInputRejected | TerminalStateUnknown | None = None
        try:
            self._check_opening(execution)
            async with asyncio.timeout(_START_SECONDS):
                actual = await self._cwd(execution, cwd)
                self._check_opening(execution)
                self._bindings.require_actual_cwd_not_deleting(actual)
                execution.client = self._clients.create(execution.session, actual, resume=binding.native_thread_id is not None)
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
                execution.revision = self._advance(binding.id)
            try:
                async with asyncio.timeout(_WRITE_SECONDS):
                    await execution.client.query(single_message(message))
            except Exception:
                # The write may have succeeded. This is still the only consumer.
                async with asyncio.timeout(self._remaining(_RECONCILE_SECONDS)):
                    event = await self._observe(execution, execution.client, actual, commit)
            del message
            if not execution.ready.done():
                execution.ready.set_result(Submission(SubmitDisposition.STARTED, binding.id, execution.session, execution.input_id,
                                               execution.receipt.set, binding.task_feedback, binding.feedback_revision))
            if event is None:
                event = await self._observe(execution, execution.client, actual, commit)
        except OrdinaryInputRejected as exc:
            # Never send a traceback holding the still-running owner's frame to another task.
            failure = OrdinaryInputRejected(str(exc))
        except (Exception, asyncio.CancelledError) as exc:
            if execution.writing:
                failure = TerminalStateUnknown(str(exc) if isinstance(exc, TerminalStateUnknown) else UNKNOWN)
            else:
                failure = OrdinaryInputRejected(START_CANCELLED if execution.stopping else START_FAILED)
        finally:
            await self._finish(execution, event, failure)

    async def _disconnect(self, execution: _Execution, client: ClaudeClient) -> None:
        binding_id = execution.binding.id
        try:
            await client.disconnect()
        except (Exception, asyncio.CancelledError):
            self._unavailable.add(binding_id)
        else:
            execution.closed = True
            self._owned_cwds.pop(binding_id, None)
            if binding_id in self._unavailable:
                self._unavailable.remove(binding_id)
                self._advance(binding_id)

    async def _finish(
        self, execution: _Execution, event: OrdinaryEvent | None,
        failure: OrdinaryInputRejected | TerminalStateUnknown | None,
    ) -> None:
        binding = execution.binding
        execution.closing = True
        execution.concluded = event is not None
        if execution.client is not None:
            close = asyncio.create_task(self._disconnect(execution, execution.client))
            self._closes.add(close)
            close.add_done_callback(self._closes.discard)
            await asyncio.wait([close], timeout=self._remaining(_CLOSE_SECONDS))
            if not close.done():
                self._unavailable.add(binding.id)
        execution.close_finished = True
        close_failed = binding.id in self._unavailable
        if failure is not None:
            # A confirmed close releases the UUID for the next explicit input.
            # Delivery of the old outcome may still be waiting on Feishu.
            self._release(execution)
            if not execution.ready.done():
                if execution.stop_receipt is not None:
                    try:
                        async with asyncio.timeout(self._remaining(_CONTROL_SECONDS + 1)):
                            await execution.stop_receipt.wait()
                    except TimeoutError:
                        pass
                if not execution.ready.done():
                    execution.ready.set_exception(failure)
                # The waiting handler reports this write failure unless it was cancelled.
                await execution.submit_finished.wait()
                report_unknown = execution.detached
            else:
                report_unknown = True
            if report_unknown and isinstance(failure, TerminalStateUnknown):
                event = TurnObservationUnavailableOutcome(binding.id, execution.session, execution.input_id,
                                                          self._owner, execution.origin, failure)
        try:
            if event is not None:
                await execution.receipt.wait()
                if execution.stop_receipt is not None:
                    await execution.stop_receipt.wait()
                async with asyncio.timeout(self._remaining(30.0)):
                    await self._completion(event)
        except Exception:
            logger.warning("Claude completion delivery failed for Binding %s", binding.id)
        finally:
            self._release(execution)
        if close_failed and binding.id in self._unavailable:
            await self._tell(execution, CLOSE_FAILED)

    def _release(self, execution: _Execution) -> None:
        if self._active.get(execution.binding.id) is execution:
            del self._active[execution.binding.id]
            self._advance(execution.binding.id)

    async def _tell(self, execution: _Execution, text: str) -> None:
        try:
            async with asyncio.timeout(self._remaining(_CONTROL_SECONDS)):
                await self._notice(execution.origin, text)
        except Exception:
            logger.warning("Claude notice delivery failed for Binding %s", execution.binding.id)

    async def _invalid_init(self, client: ClaudeClient) -> None:
        try:
            async with asyncio.timeout(self._remaining(_CONTROL_SECONDS)):
                await client.interrupt()
        except Exception:
            pass
        raise TerminalStateUnknown(f"{MODE_UNKNOWN}\n{UNKNOWN}")

    async def _observe(self, execution: _Execution, client: ClaudeClient, cwd: Path,
                       commit: ContextCursorCommit | None) -> OrdinaryConclusion:
        async def commit_intake() -> None:
            nonlocal commit
            if commit is not None:
                cursor, commit = commit, None
                try:
                    self._bindings.commit_context_anchor(binding_id=execution.binding.id,
                                                         expected_context_revision=cursor.expected_context_revision,
                                                         anchor=cursor.anchor)
                except Exception:
                    await self._tell(execution, "已接收，但上下文边界更新失败。")

        initialized = False
        warned = False
        async with asyncio.timeout(None) as reconciliation:
            async for msg in client.receive_messages():
                if not initialized and isinstance(msg, AssistantMessage):
                    await self._invalid_init(client)
                if isinstance(msg, SystemMessage) and msg.subtype == "init":
                    data = cast(SystemInitData, msg.data)
                    init_cwd = data.get("cwd")
                    if (data.get("session_id") != execution.session or not isinstance(init_cwd, str)
                            or not Path(init_cwd).is_absolute() or Path(init_cwd).resolve() != cwd):
                        raise TerminalStateUnknown(UNKNOWN)
                    await commit_intake()
                    mode = data.get("permissionMode")
                    if mode not in ("auto", "default"):
                        await self._invalid_init(client)
                    initialized = True
                    if mode == "default" and not warned:
                        warned = True
                        await self._tell(execution, DEFAULT_MODE)
                if not isinstance(msg, ResultMessage):
                    continue
                if msg.session_id != execution.session:
                    raise TerminalStateUnknown(UNKNOWN)
                ids = [] if msg.user_message_uuids is None else msg.user_message_uuids
                valid_ids = isinstance(ids, list) and all(isinstance(value, str) and value for value in ids)
                single = msg.user_message_uuid
                valid_single = single is None or (isinstance(single, str) and bool(single))
                origin = msg.origin
                # The SDK says any origin kind other than "human" is not a person's
                # input, including kinds newer CLIs add (types.MessageOriginKind).
                injected = isinstance(origin, dict) and origin.get("kind") not in (None, "human")
                if valid_ids and valid_single and (single or ids):
                    if single != execution.input_id and execution.input_id not in ids:
                        continue
                elif injected and valid_ids and valid_single:
                    # For example, resuming after a killed background task replays its
                    # notification as a Result with no input attribution at all.
                    continue
                else:
                    if reconciliation.when() is None:
                        reconciliation.reschedule(asyncio.get_running_loop().time() + _RECONCILE_SECONDS)
                    continue
                await commit_intake()
                if not initialized:
                    await self._invalid_init(client)
                status, answer, detail = _terminal(msg)
                return OrdinaryConclusion(execution.binding.id, execution.session, execution.input_id, self._owner, execution.origin,
                                          status, answer, detail, task_feedback=execution.binding.task_feedback,
                                          feedback_revision=execution.binding.feedback_revision, cwd=cwd)
        raise TerminalStateUnknown(UNKNOWN)

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
            candidates = self._candidate_dirs(binding)
        try:
            async with asyncio.timeout(_CONTROL_SECONDS):
                cwd = await self._clients.session_cwd(binding.native_thread_id, candidates)
        except Exception:
            cwd = None
        async with lock:
            if (self._bindings.get(binding_id) != binding or snapshot != self.local_snapshot(binding_id)
                    or self._candidate_dirs(binding) != candidates):
                return {"cwd": None, "status": "unknown"}
            return {"cwd": str(cwd.resolve()) if cwd else None, "status": "unknown"}

    async def delete_lazy_resource(self, binding_id: str) -> ThreadBinding:
        async with self._lock(binding_id):
            self._idle(binding_id)
            if self._bindings.get(binding_id).native_thread_id is not None:
                raise OrdinaryInputRejected("Claude 试点暂不支持【已建立的会话删除】。")
            removed = self._bindings.delete_binding(binding_id)
            self._advance(binding_id)
            return removed

    def local_snapshot(self, binding_id: str) -> LocalExecutionSnapshot:
        execution = self._active.get(binding_id)
        turn = None
        if execution is not None:
            state = ActiveState.STOPPING if execution.stopping else ActiveState.RUNNING
            turn = ActiveTurnSnapshot(binding_id, execution.session, execution.input_id, self._owner, state)
        return LocalExecutionSnapshot(execution.revision if execution else self._revisions.get(binding_id, 0),
                                      turn, binding_id in self._unavailable)

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
                raise OrdinaryInputRejected(CLOSING_CONCLUDED if execution.concluded else CLOSING)
            return StopPreview("正在请求停止当前 Claude 执行。" if execution.writing else "正在取消 Claude 会话启动。",
                               execution.revision, execution.input_id)

    def _exact(self, binding_id: str, revision: int | None, turn_id: str | None) -> _Execution:
        execution = self._active.get(binding_id)
        if execution is None or execution.closing or (revision, turn_id) != (execution.revision, execution.input_id):
            raise ThreadActivityChanged("目标执行已变化，请刷新后重试。")
        return execution

    async def stop_exact(self, binding_id: str, *, acknowledge: StopAcknowledger | None = None,
                         expected_activity_revision: int | None = None,
                         expected_turn_id: str | None = None) -> StopDisposition:
        async with self._lock(binding_id):
            execution = self._exact(binding_id, expected_activity_revision, expected_turn_id)
            if execution.stopping:
                return StopDisposition.STOPPING
            execution.stopping = True
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
                    await client.interrupt()
            acknowledge_success = True
        except (Exception, asyncio.CancelledError) as exc:
            async with self._lock(binding_id):
                if execution.concluded and execution.closed:
                    return StopDisposition.NOT_RUNNING
                if self._active.get(binding_id) is execution:
                    execution.stopping = False
            if isinstance(exc, asyncio.CancelledError):
                raise
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
            return ActiveTurnSnapshot(binding_id, execution.session, execution.input_id, self._owner,
                                      ActiveState.STOPPING if execution.stopping else ActiveState.RUNNING)

    def _remaining(self, seconds: float) -> float:
        if self._shutdown_deadline is None:
            return seconds
        return max(0.0, min(seconds, self._shutdown_deadline - time.monotonic()))

    def close_admission(self) -> None:
        if self._shutdown_deadline is None:
            self._shutdown_deadline = time.monotonic() + 60.0
        for execution in self._tasks.values():
            execution.receipt.set()

    async def _shutdown_stop(self, execution: _Execution) -> None:
        try:
            await self.stop_exact(execution.binding.id, expected_activity_revision=execution.revision, expected_turn_id=execution.input_id)
        except (ThreadActivityChanged, TerminalStateUnknown):
            pass

    async def interrupt_all(self) -> None:
        self.close_admission()
        for execution in self._active.values():
            if not execution.closing:
                task = asyncio.create_task(self._shutdown_stop(execution))
                self._controls.add(task)
                task.add_done_callback(self._controls.discard)
        if not await self.wait_idle(_CONTROL_SECONDS):
            await self.cancel_tasks()

    async def wait_idle(self, timeout: float) -> bool:
        deadline = time.monotonic() + self._remaining(timeout)
        while tasks := {task for task in (*self._tasks, *self._controls, *self._closes) if not task.done()}:
            _, pending = await asyncio.wait(tasks, timeout=max(0.0, deadline - time.monotonic()))
            if pending:
                return False
        return not self._unavailable

    async def cancel_tasks(self) -> None:
        self.close_admission()
        for task in self._controls:
            task.cancel()
        for execution in self._tasks.values():
            if execution.closing:
                continue
            if not execution.writing:
                self._cancel_opening(execution)
            elif execution.task is not None and not execution.task.cancelling():
                execution.task.cancel()
        if not await self.wait_idle(_CLOSE_SECONDS):
            for task, execution in self._tasks.items():
                if execution.close_finished and execution.submit_finished.is_set():
                    task.cancel()
            if not await self.wait_idle(self._remaining(60.0)):
                raise RuntimeError("未能确认所有 Claude 客户端已关闭。")
