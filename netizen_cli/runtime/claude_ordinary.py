"""One owned Claude client per admitted ordinary Binding execution."""
from __future__ import annotations

import asyncio
from contextlib import aclosing
from collections.abc import Awaitable, Callable
from weakref import WeakValueDictionary
from dataclasses import dataclass, field, replace
import logging
import os
from pathlib import Path
import time
from typing import Any
from uuid import UUID, uuid4

from claude_agent_sdk import AssistantMessage, RateLimitEvent, ResultMessage, StreamEvent, SystemMessage

from ..backends import BackendPolicy
from ..bindings import BindingContextRevisionConflict, BindingNotFound, BindingStore, ProjectDeleting, ProjectNotFound, ScopeNotFound, ThreadBinding
from ..bindings import BindingTaskFeedback, BindingTurnSettings
from ..partial_answers import PartialAnswer
from ..progress_card_settings import ProgressCardSettings
from ..completion_mention import valid_completion_mention_user_id
from ..domain import ActiveState, FeishuScope, MessageContextAnchor, MentionContextMode
from ..session_settings import require_local_settings
from ..external_calls import ExternalCallCancelled, external_call
from ..ordinary_sessions import (
    ClaudeCliState, CliStateChanged, ClaudeInputAnchor, ConversationTarget, DegradedTurn, LiveSubmissionUnknown, LogicalReply, LogicalTerminal, PhysicalAnswer, PhysicalStarted,
    LocalExecutionSnapshot, LocalStatusSnapshot, OrdinaryCompletionHandler, OrdinaryEvent, OrdinaryInputRejected, OrdinarySessionPort,
    PreparedPrompt, SessionShutdownPort, StopPreview,
)
from .claude_delivery import AnchorDelivery
from .claude_status import ClaudeObservations, DEFAULT_OBSERVATIONS, record_observation
from .claude_client import ClaudeClient, ClaudeClients, LiveSession, live_messages, native_human, single_message, user_message
from .contracts import (
    ActiveTurnSnapshot, ContextCursorCommit, StopAcknowledger, StopDisposition, Submission, SubmissionAdmission,
    SubmitDisposition, TerminalStateUnknown, ThreadActivityChanged, TurnActivitySnapshot,
)

logger = logging.getLogger(__name__)
UNKNOWN = "本次 Claude 提交结果未知，请检查原会话；系统不会自动重发。"
CLOSE_FAILED = "无法确认 Claude 客户端已关闭，本会话暂不可再提交。"
CLOSING_CONCLUDED = "这次 Claude 执行已经结束，结果正在送出。"
CLOSING = "正在关闭 Claude 客户端，执行结果尚未确认。"
MODE_UNKNOWN = "无法确认 Claude 的有效权限模式，已尝试停止，结果未知。"
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
_TRANSITION_SECONDS = 300.0
DEGRADED = "无法确认本轮 Claude 会话状态，正在断开客户端。其他后台任务的结果可能无法送达；关闭后可重新发送消息。"


@dataclass(slots=True)
class _BindingActivity:
    revision: int = 0
    last_execution_id: str | None = None


@dataclass(slots=True)
class _Human:
    binding: ThreadBinding
    origin: object
    input_id: str
    ready: asyncio.Future[Submission]
    commit: ContextCursorCommit | None
    receipt: asyncio.Event = field(default_factory=asyncio.Event)
    proof: asyncio.Event = field(default_factory=asyncio.Event)
    reply: LogicalReply = field(default_factory=LogicalReply)
    last_display: tuple[ClaudeCliState, bool] | None = None
    written: bool = False
    terminal_sent: bool = False
    stop_requested: bool = False
    status: str | None = None
    permission_notice: str | None = None

    @property
    def source(self) -> object:
        return self.origin.source if isinstance(self.origin, ClaudeInputAnchor) else self.origin

    def take_cursor(self) -> ContextCursorCommit | None:
        cursor, self.commit = self.commit, None
        return cursor

    def settle(self, session: str, error: Exception | asyncio.CancelledError | None = None) -> None:
        if isinstance(error, asyncio.CancelledError):
            self.receipt.set()
            if not self.ready.done():
                self.ready.cancel()
            elif not self.ready.cancelled():
                self.ready.exception()
            return
        if self.ready.done():
            return
        if error is None:
            self.ready.set_result(Submission(SubmitDisposition.STARTED, self.binding.id, session, self.input_id,
                                             self.receipt.set, self.binding.task_feedback, self.binding.feedback_revision))
        else:
            self.ready.set_exception(error)
            self.receipt.set()  # No Channel receipt will follow a rejected or uncertain submission.


@dataclass(slots=True)
class _Publication:
    allowed: bool = True

    def valid(self) -> bool:
        return self.allowed


@dataclass(slots=True)
class _Execution:
    binding: ThreadBinding
    scope: FeishuScope
    session: str
    revision: int
    human: _Human
    conversation_source: object = field(init=False)
    live: LiveSession = field(init=False)
    humans: dict[str, _Human] = field(default_factory=dict)
    unwritten: set[str] = field(default_factory=set)
    pending: _Human | None = None
    client: ClaudeClient | None = None
    task: asyncio.Task[None] | None = None
    writer: asyncio.Task[None] | None = None
    delivery: AnchorDelivery = field(default_factory=AnchorDelivery)
    close: asyncio.Task[None] | None = None
    timer: asyncio.TimerHandle | None = None
    signature: tuple[object, ...] | None = None
    ending: bool = False
    publication: _Publication = field(default_factory=_Publication)
    started: bool = False
    stop_target: str | None = None
    awaiting_task_stop: bool = False
    stopping: bool = False
    closing: bool = False
    close_finished: bool = False
    concluded: bool = False
    stop_receipt: asyncio.Event | None = None
    broken: bool = False

    def __post_init__(self) -> None:
        self.live = LiveSession(self.session, self.fail)
        self.conversation_source = self.human.origin
        self.reserve(self.human)

    def reserve(self, human: _Human) -> None:
        anchor = human.origin
        previous = self.conversation_source
        if isinstance(anchor, ClaudeInputAnchor):
            if anchor.scope != self.scope:
                raise OrdinaryInputRejected(STALE)
            if isinstance(previous, ClaudeInputAnchor) and previous.topic_root_id:
                if anchor.topic_root_id and anchor.topic_root_id != previous.topic_root_id:
                    raise OrdinaryInputRejected("本条消息的话题根与当前会话不一致，本条输入未发送。")
            elif anchor.topic_root_id:
                self.conversation_source = anchor
        self.humans[human.input_id] = self.pending = human

    def submitted(self, input_id: str | None) -> _Human | None:
        human = self.pending
        return human if human and human.written and input_id == human.input_id else None

    def routable(self, owner: str | None) -> _Human | None:
        human = self.humans.get(owner or "")
        if owner in self.unwritten or (human and not human.written):
            raise ValueError("Claude attributed a Result to an unwritten input")
        return human

    def cli_state(self) -> ClaudeCliState:
        return ClaudeCliState(self.live.ordered_idle and self.pending is None and self.live.physical is None,
                             len(self.live.tasks), self.live.crons)

    def activity(self, turn_id: str, feedback: BindingTaskFeedback, *, final: str | None = None) -> TurnActivitySnapshot:
        partials = tuple(PartialAnswer(self.session, turn_id, key, text) for key, text in self.live.partials.items()
                         if text != final)
        return TurnActivitySnapshot(self.binding.id, self.session, turn_id, self.revision,
                                   ActiveState.RUNNING, 0, False, False, False, (), partial_answers=partials,
                                   progress_card_settings=feedback.progress_card_settings or ProgressCardSettings(),
                                   state_line=self.cli_state().line)

    @property
    def finishing(self) -> bool:
        return self.ending or self.closing

    @property
    def writing(self) -> bool:
        return self.human.written

    @property
    def stopping_current(self) -> bool:
        return self.stopping and self.stop_target == self.live.physical

    @property
    def stoppable(self) -> bool:
        return self.live.physical is not None or (self.pending is not None and (self.writer is None or self.pending.written))

    @property
    def admissible(self) -> bool:
        return (self.pending is None and self.writer is None and self.live.ordered_idle and self.live.physical is None
                and not self.finishing and not self.broken)

    def fail(self) -> None:
        self.broken = True
        self.publication.allowed = False
        self.live.changed.set()

    @property
    def input_id(self) -> str:
        return self.live.init_human or self.live.physical or (self.pending or self.human).input_id


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
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self.on_binding_deleted: Callable[[str], None] | None = None
        self._activity: dict[str, _BindingActivity] = {}
        self._active: dict[str, _Execution] = {}
        self._tasks: dict[asyncio.Task[None], _Execution] = {}
        self._closes: set[asyncio.Task[None]] = set()
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
        return self._locks.setdefault(binding_id, asyncio.Lock())

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
                    try:
                        self._bindings.get(binding_id)
                    except BindingNotFound:
                        continue
                    self._invalidate(binding_id)

    def _invalidate(self, binding_id: str) -> None:
        if execution := self._active.get(binding_id):
            self._reconcile(execution, invalidate=True)
            execution.live.changed.set()
        else:
            self._advance(binding_id)

    def _idle(self, binding_id: str, *, input: bool = False) -> None:
        self.require_admission()
        if binding_id in self._unavailable:
            raise OrdinaryInputRejected(CLOSE_FAILED)
        if binding_id in self._active and self._active[binding_id].closing:
            raise OrdinaryInputRejected("上一轮正在收尾，本条消息未发送。")
        active = self._active.get(binding_id)
        if active is not None and not (input and active.admissible):
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
            self._idle(binding_id, input=True)
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
            self._idle(binding.id, input=True)
            current = self._current(binding.id)
            if (current != binding or self._admission(current) != admission or
                    (context_commit and context_commit.expected_context_revision != binding.context_revision)):
                raise OrdinaryInputRejected(STALE)
            input_id = str(uuid4())
            message = user_message(input, input_id)
            human = _Human(current, origin, input_id, asyncio.get_running_loop().create_future(), context_commit)
            active = self._active.get(binding.id)
            if active is None:
                scope = self._bindings.get_scope(current.scope_key).scope
                execution = _Execution(current, scope, current.native_thread_id or str(uuid4()), admission.revision, human)
                self._active[binding.id] = execution
                self._reconcile(execution)
                execution.task = asyncio.create_task(self._run(execution, cwd, message, execution.revision))
                self._tasks[execution.task] = execution
                execution.task.add_done_callback(self._tasks.pop)
            else:
                execution = active
                execution.reserve(human)
                self._reconcile(execution)
                execution.writer = asyncio.create_task(self._write_next(execution, human, message, execution.revision))
                execution.live.changed.set()
        try:
            return await asyncio.shield(human.ready)
        except asyncio.CancelledError as exc:
            human.settle(execution.session, exc)
            if not execution.writing:
                self._cancel_opening(execution)
            raise

    @staticmethod
    def _cancel_opening(execution: _Execution) -> None:
        if execution.finishing:
            return
        execution.stopping = True
        if execution.started and execution.task is not None and not execution.task.cancelling():
            execution.task.cancel()

    def _check_opening(self, execution: _Execution, revision: int) -> None:
        self.require_admission()
        if execution.stopping:
            raise OrdinaryInputRejected(START_CANCELLED)
        binding_id = execution.binding.id
        if self._activity[binding_id].revision != revision or self._current(binding_id) != execution.binding:
            raise OrdinaryInputRejected(STALE)

    def _candidate_dirs(self, binding: ThreadBinding) -> tuple[Path, ...]:
        project = Path(self._bindings.get_project(binding.project_alias).cwd)
        return (Path(binding.working_cwd), project) if binding.working_cwd else (project,)

    async def _cwd(self, execution: _Execution, requested: Path | None) -> Path:
        binding = execution.binding
        if binding.native_thread_id is not None:
            try:
                UUID(execution.session)
                cwd = await external_call(self._clients.session_cwd(execution.session, self._candidate_dirs(binding)))
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

    async def _run(self, execution: _Execution, cwd: Path | None, message: dict[str, Any], revision: int) -> None:
        execution.started = True
        binding = execution.binding
        failure: OrdinaryInputRejected | TerminalStateUnknown | None = None
        try:
            self._check_opening(execution, revision)
            async with asyncio.timeout(_START_SECONDS):
                actual = await self._cwd(execution, cwd)
                self._check_opening(execution, revision)
                self._bindings.require_actual_cwd_not_deleting(actual)
                execution.client = self._clients.create(execution.session, actual, resume=binding.native_thread_id is not None, live=execution.live)
                self._owned_cwds[binding.id] = actual
                await external_call(execution.client.connect())
            async with self._lock(binding.id):
                self._check_opening(execution, revision)
                self._bindings.worktrees.require_cwd_not_deleting(actual)
                if binding.native_thread_id is None:
                    if self._creation_cwd(binding, cwd) != actual:
                        raise OrdinaryInputRejected(STALE)
                    self._bindings.assign_native_thread_id(binding.id, execution.session)
            confirmed_write = await self._write(execution, execution.human, message)
            del message
            await self._observe(execution, execution.client, actual, confirmed_write)
        except OrdinaryInputRejected as exc:
            failure = OrdinaryInputRejected(str(exc))
        except TerminalStateUnknown as exc:
            failure = LiveSubmissionUnknown(str(exc))
        except (Exception, asyncio.CancelledError):
            failure = LiveSubmissionUnknown(UNKNOWN) if execution.writing else OrdinaryInputRejected(
                START_CANCELLED if execution.stopping else START_FAILED)
        finally:
            await self._finish(execution, failure)

    async def _write(self, execution: _Execution, human: _Human, message: dict[str, Any]) -> bool:
        assert execution.client is not None
        execution.human = human
        human.written = True
        if execution.writer:
            execution.concluded = execution.stopping = False
            execution.stop_receipt = None
            execution.publication = _Publication()
        execution.live.changed.set()
        try:
            async with asyncio.timeout(_WRITE_SECONDS):
                await external_call(execution.client.query(single_message(message)))
        except Exception:
            return False  # Reconcile the possible write on the existing reader.
        human.settle(execution.session)
        return True

    async def _write_next(self, execution: _Execution, human: _Human,
                          message: dict[str, Any], revision: int) -> None:
        try:
            try:
                self.require_admission()
                if (human.ready.done() or not execution.live.ordered_idle or execution.live.physical is not None or execution.finishing
                        or execution.broken or self._activity[human.binding.id].revision != revision
                        or self._current(human.binding.id) != human.binding):
                    raise OrdinaryInputRejected(STALE)
            except OrdinaryInputRejected as exc:
                execution.unwritten.add(human.input_id)
                human.settle(execution.session, exc)
                self._retire(execution, human)
                execution.pending = None
                return
            if not await self._write(execution, human, message):
                async with asyncio.timeout(self._remaining(_RECONCILE_SECONDS)):
                    await human.proof.wait()
        except (Exception, asyncio.CancelledError):
            if not execution.finishing:
                execution.fail()
        finally:
            execution.writer = None
            execution.live.changed.set()

    async def _disconnect(self, execution: _Execution) -> None:
        # The final level check and SDK invocation share a synchronous hand-off.
        self._reconcile(execution, handoff=True)
        if not execution.closing:
            execution.close = None
            return
        binding_id = execution.binding.id
        try:
            if execution.client is not None:
                await external_call(execution.client.disconnect())
        except (Exception, asyncio.CancelledError):
            self._unavailable.add(binding_id)
        else:
            self._owned_cwds.pop(binding_id, None)
            if binding_id in self._unavailable:
                self._unavailable.remove(binding_id)
                self._invalidate(binding_id)

    async def _finish(self, execution: _Execution, failure: OrdinaryInputRejected | TerminalStateUnknown | None) -> None:
        execution.ending = True
        human = execution.human
        unknown = isinstance(failure, TerminalStateUnknown) or execution.broken
        if unknown:
            execution.fail()
        self._reconcile(execution)
        if execution.writer:
            execution.writer.cancel()
            await asyncio.gather(execution.writer, return_exceptions=True)
        if unknown and self._shutdown_deadline is None:
            detail = f"{MODE_UNKNOWN}\n{DEGRADED}" if MODE_UNKNOWN in str(failure) else DEGRADED
            affected = tuple(h.input_id for h in execution.humans.values() if h.written)
            event = DegradedTurn(execution.binding.id, execution.session, human.input_id, self._owner,
                                 execution.scope, TerminalStateUnknown(detail), affected_inputs=affected)
            self._queue(execution, human, event, conversation_id="degraded-" + human.input_id)
        assert execution.close is not None
        await asyncio.wait([execution.close], timeout=self._remaining(_CLOSE_SECONDS))
        if not execution.close.done():
            self._unavailable.add(execution.binding.id)
        for owned in tuple(execution.humans.values()):
            error = (failure or LiveSubmissionUnknown(UNKNOWN)) if owned.written else (
                failure if isinstance(failure, OrdinaryInputRejected) else OrdinaryInputRejected(START_CANCELLED))
            owned.settle(execution.session, error)
            if unknown:
                self._retire(execution, owned)
        execution.pending = None
        execution.close_finished = True
        self._reconcile(execution)
        try:
            await execution.delivery.drain()
        finally:
            for owned in tuple(execution.humans.values()):
                self._retire(execution, owned)
            await execution.delivery.join_cleanups()
            self._reconcile(execution)
        if execution.binding.id in self._unavailable:
            await execution.delivery.notice(lambda: self._tell(human, CLOSE_FAILED))

    def _retire(self, execution: _Execution, human: _Human) -> None:
        if execution.humans.pop(human.input_id, None) is None:
            return
        human.take_cursor()  # Discard a cursor for which intake was never proven.
        anchor = human.origin
        if isinstance(anchor, ClaudeInputAnchor):
            anchor.retired = True
            anchor.release_cards()
            if human.written:
                execution.delivery.retire(anchor, human.input_id, human.binding.id)

    def _queue(self, execution: _Execution, human: _Human, event: OrdinaryEvent, *,
               conversation_id: str | None = None) -> None:
        key = human.input_id if conversation_id is None else None
        stop_receipt = execution.stop_receipt
        notice = None
        receipts = []
        if isinstance(event, DegradedTurn):
            receipts = [execution.humans[input_id].receipt for input_id in event.affected_inputs]
        elif key is not None:
            receipts = [human.receipt]
            notice, human.permission_notice = human.permission_notice, None
        async def present() -> None:
            if isinstance(event, (PhysicalAnswer, LogicalTerminal)) and not event.publishable():
                return
            if notice and not isinstance(event, DegradedTurn):
                await self._tell(human, notice)
            outcome = event
            if conversation_id is not None:
                target = ConversationTarget(execution.scope, execution.conversation_source, conversation_id,
                                            event.publishable if isinstance(event, PhysicalAnswer) else lambda: True)
                outcome = replace(event, origin=target)
            budget = self._remaining(30.0)
            if isinstance(outcome, PhysicalAnswer):
                # The presenter settles incomplete content once after its content deadline.
                outcome = replace(outcome, delivery_deadline=asyncio.get_running_loop().time() + budget)
                budget = self._remaining(budget + _CONTROL_SECONDS)
            async with asyncio.timeout(budget):
                await external_call(self._completion(outcome))

        execution.delivery.enqueue(key, receipts, stop_receipt, present, binding_id=execution.binding.id,
                                   changed=execution.live.changed,
                                   settled=(lambda: self._retire(execution, human)) if isinstance(event, LogicalTerminal) else None)

    async def _tell(self, execution: _Human, text: str) -> None:
        try:
            async with asyncio.timeout(self._remaining(_CONTROL_SECONDS)):
                await external_call(self._notice(execution.source, text))
        except Exception:
            logger.warning("Claude notice delivery failed for Binding %s", execution.binding.id)

    async def _invalid_init(self, client: ClaudeClient) -> None:
        try:
            async with asyncio.timeout(self._remaining(_CONTROL_SECONDS)):
                await external_call(client.interrupt())
        except Exception:
            pass
        raise TerminalStateUnknown(f"{MODE_UNKNOWN}\n{UNKNOWN}")

    async def _intake(self, execution: _Execution, human: _Human) -> None:
        human.settle(execution.session)
        human.proof.set()
        cursor = human.take_cursor()
        if cursor is not None:
            try:
                self._bindings.commit_context_anchor(binding_id=human.binding.id,
                    expected_context_revision=cursor.expected_context_revision, anchor=cursor.anchor)
            except BindingContextRevisionConflict:
                pass  # A newer context configuration owns this Binding's cursor.
            except Exception:
                self.close_admission(reason=CURSOR_FAILURE)
                await self._tell(human, f"Claude 已接收本次任务，但{CURSOR_FAILURE}")

    def _route_result(self, execution: _Execution, frame: ResultMessage) -> _Human | None:
        live = execution.live
        assert frame.uuid is not None
        input_id = native_human(frame)
        explicit = execution.routable(input_id)
        if frame.uuid in live.seen:
            return None
        live.seen.add(frame.uuid)
        if live.init_human and not input_id:
            raise ValueError("missing active input attribution")
        physical = live.physical
        owner, wake = live.route(frame)
        human = explicit if input_id else execution.routable(owner)
        if physical is not None and execution.stop_target == physical:
            if human:
                human.stop_requested = True
            execution.stop_target = None
            execution.stopping = False
        submitted = execution.submitted(input_id)
        bodyless = (frame.subtype, frame.terminal_reason, frame.is_error) == ("success", None, False) and not frame.result
        status, body, detail = ("completed", None, None) if bodyless else _terminal(frame)
        source = human or execution.human
        if submitted:
            submitted.status = status
            if denial := _denial_notice(frame.permission_denials):
                submitted.permission_notice = "\n".join(filter(None, [submitted.permission_notice, denial]))
            with record_observation():
                self.observations.result(submitted.binding.id, execution.session, submitted.input_id, frame)
        if wake and human and status in ("failed", "interrupted"):
            human.status = status
        direct = submitted is not None and submitted is human
        # Native error bodies only use the existing safe failure projection.
        # Even without a Result body, finish the proven human's running presenter
        # and transfer this physical turn's validated partials before clearing them.
        finish_presentation = direct and isinstance(source.origin, ClaudeInputAnchor)
        if frame.result or detail or live.partials or (status == "completed" and finish_presentation):
            turn_id = source.input_id if direct else frame.uuid
            if human:
                human.reply.latest_answer_turn_id = turn_id
            snapshot = execution.activity(turn_id, source.binding.task_feedback, final=body)
            event = PhysicalAnswer(source.binding.id, execution.session,
                                   turn_id, self._owner,
                                   human.source if human else execution.scope, status, body, detail,
                                   task_feedback=source.binding.task_feedback, feedback_revision=source.binding.feedback_revision,
                                   cwd=self._owned_cwds[source.binding.id], logical_id=human.input_id if human else None,
                                   state=execution.cli_state, owned_tasks=(lambda: live.task_count(human.input_id) if human else 0),
                                   activity=snapshot, partial_answers=snapshot.partial_answers,
                                   wake=wake, reply=human.reply if human else None,
                                   publishable=execution.publication.valid)
            self._queue(execution, source, event, conversation_id=None if human else frame.uuid)
        live.partials.clear()
        live.candidate = None
        if submitted:
            execution.pending = None
        execution.concluded = True
        return submitted

    def _reconcile(self, execution: _Execution, result: ResultMessage | None = None, *,
                   handoff: bool = False, invalidate: bool = False) -> _Human | None:
        """Apply the same readiness, revision, deadline and close rules at every cut."""
        binding_id = execution.binding.id
        if self._active.get(binding_id) is not execution:
            return None
        if execution.close_finished and (execution.broken or not execution.delivery.queues):
            del self._active[binding_id]
            self._advance(binding_id)
            return None
        submitted = self._route_result(execution, result) if result else None
        live = execution.live
        idle = live.ordered_idle and live.physical is None
        force = execution.ending or execution.broken
        close = force or (idle and not live.tasks and not live.crons and execution.pending is None
                          and not execution.awaiting_task_stop)
        if handoff and close:
            execution.closing = True
            live.changed.set()
        signature = (execution.input_id, execution.human.input_id, execution.admissible, execution.closing,
                     live.physical, live.state, live.crons, frozenset(live.tasks.items()), tuple(live.partials))
        if signature != execution.signature or invalidate:
            execution.signature = signature
            execution.revision = self._advance(execution.binding.id, execution_id=execution.human.input_id)
        waiting = not close and (
            (live.ordered_idle and (execution.pending is not None or live.physical is not None)) or
            (live.awaiting_boundary and not live.tasks and live.physical is None and execution.pending is None))
        if waiting and execution.timer is None:
            execution.timer = asyncio.get_running_loop().call_later(_TRANSITION_SECONDS, execution.fail)
        elif not waiting and execution.timer is not None:
            execution.timer.cancel()
            execution.timer = None
        display = execution.cli_state()
        for human in execution.humans.values():
            if force or human.terminal_sent or human.status is None or not isinstance(human.origin, ClaudeInputAnchor):
                continue
            background = live.has_work(human.input_id)
            if (display, background) != human.last_display:
                human.last_display = (display, background)
                self._queue(execution, human, CliStateChanged(binding_id, execution.session, human.source, human.input_id, background,
                            human.reply, execution.cli_state, execution.publication.valid))
        if idle and not force and not execution.closing:
            self._complete_humans(execution)
        if close and execution.close is None:
            execution.close = asyncio.create_task(self._disconnect(execution))
            self._closes.add(execution.close)
            execution.close.add_done_callback(self._closes.discard)
        return submitted

    def _complete_humans(self, execution: _Execution) -> None:
        live = execution.live
        for human in tuple(execution.humans.values()):
            if human.status is None or human.terminal_sent or live.has_work(human.input_id):
                continue
            human.terminal_sent = True
            human.reply.verified_status = human.status
            mention = None
            if human.binding.task_feedback.completion_mention_enabled and not human.stop_requested and human.status != "interrupted":
                mention = valid_completion_mention_user_id(self._owner)
            self._queue(execution, human, LogicalTerminal(human.binding.id, execution.session, human.input_id, self._owner,
                        human.source, human.status, human.reply, mention, lambda: not execution.broken, execution.cli_state))
        live.prune({key for key, human in execution.humans.items() if not human.terminal_sent})

    async def _observe(self, execution: _Execution, client: ClaudeClient, cwd: Path, confirmed_write: bool) -> None:
        live = execution.live
        async with (aclosing(live_messages(client, live)) as stream,
                    asyncio.timeout(None if confirmed_write else self._remaining(_RECONCILE_SECONDS)) as timer):
            try:
                async for msg in stream:
                    if execution.finishing:
                        return
                    if execution.broken:
                        raise ValueError("unusable Claude session")
                    human = execution.pending
                    pending = human.input_id if human and human.written else None
                    if isinstance(msg, SystemMessage) and msg.subtype == "init":
                        data = msg.data
                        if Path(data["cwd"]).resolve() != cwd or not Path(data["cwd"]).is_absolute():
                            raise ValueError("unexpected Claude cwd")
                        if data.get("permissionMode") not in ("auto", "default"):
                            execution.fail()
                            await self._invalid_init(client)
                        if not live.initialized and data.get("user_message_uuids") != [pending]:
                            self._reconcile(execution)
                            continue  # Cold notification init before the submitted input.
                        live.initialized = True
                        live.init(msg)
                        execution.routable(live.init_human)
                        if not live.init_human:
                            execution.stopping = False
                            execution.stop_receipt = None
                        execution.publication = _Publication()
                        if human := execution.submitted(live.init_human):
                            if isinstance(human.origin, ClaudeInputAnchor):
                                physical = live.physical
                                def active(physical=physical, anchor=human.origin) -> bool:
                                    return live.physical == physical and not execution.broken and not anchor.retired
                                self._queue(execution, human, PhysicalStarted(execution.binding.id, execution.session,
                                    human.input_id, human.source, human.binding.task_feedback, active,
                                    human.origin.retained, human.reply))
                            if confirmed_write and human.ready.done() and not human.ready.cancelled():
                                await self._intake(execution, human)
                            if data["permissionMode"] == "default":
                                human.permission_notice = DEFAULT_MODE
                            with record_observation():
                                self.observations.init(execution.binding.id, execution.session, human.input_id, data.get("model"), str(cwd))
                    elif not live.initialized:
                        if isinstance(msg, SystemMessage) and msg.subtype == "session_state_changed":
                            live.system(msg)
                        if isinstance(msg, (AssistantMessage, ResultMessage)) and native_human(msg) == pending and pending:
                            await self._invalid_init(client)
                        self._reconcile(execution)
                        continue
                    elif isinstance(msg, SystemMessage):
                        live.system(msg)
                    elif isinstance(msg, AssistantMessage):
                        if human := execution.submitted(live.assistant(msg)):
                            await self._intake(execution, human)
                    elif isinstance(msg, StreamEvent) and msg.parent_tool_use_id is None:
                        if human := execution.submitted(native_human(msg)):
                            await self._intake(execution, human)
                    elif isinstance(msg, RateLimitEvent) and msg.session_id == execution.session and human and human.written:
                        with record_observation():
                            self.observations.rate(execution.binding.id, execution.session, human.input_id, msg.rate_limit_info)
                    submitted = self._reconcile(execution, msg if isinstance(msg, ResultMessage) else None)
                    if submitted:
                        await self._intake(execution, submitted)
                    if not confirmed_write and execution.human.proof.is_set():
                        confirmed_write = True
                        timer.reschedule(None)
                if execution.closing:
                    return
                raise TerminalStateUnknown(UNKNOWN)
            except (Exception, asyncio.CancelledError):
                # Revoke publication at the point of observation, before
                # joining SDK children or awaiting the confirmed disconnect.
                execution.fail()
                raise

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
            self._activity.pop(binding_id, None)
            self.observations.discard(binding_id)
            if self.on_binding_deleted:
                self.on_binding_deleted(binding_id)
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

    def local_snapshot(self, binding_id: str) -> LocalExecutionSnapshot:
        execution = self._active.get(binding_id)
        turn = None
        if execution is not None and (execution.closing or execution.pending or execution.live.physical
                                      or not execution.live.ordered_idle):
            state = (ActiveState.CLOSING if execution.closing else
                     ActiveState.STOPPING if execution.stopping_current else ActiveState.RUNNING)
            turn = ActiveTurnSnapshot(binding_id, execution.session, execution.input_id, self._owner, state)
        return LocalExecutionSnapshot(execution.revision if execution else self._activity.get(binding_id, _BindingActivity()).revision,
                                      turn, binding_id in self._unavailable, execution.cli_state() if execution else None,
                                      execution.input_id if execution else None,
                                      bool(execution and execution.stoppable and not execution.finishing),
                                      bool(execution and execution.live.tasks and not execution.finishing))

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
            self.require_admission()
            require_local_settings(settings is not None, task_feedback)
            binding = self._bindings.set_configuration(
                binding_id=binding_id, expected_settings_revision=expected_settings_revision,
                expected_context_revision=expected_context_revision, expected_feedback_revision=expected_feedback_revision,
                settings=settings, task_feedback=task_feedback, message_context_mode=message_context_mode,
                context_anchor=context_anchor,
            )
            self._invalidate(binding_id)
            return binding

    def turn_activity(self, binding_id: str, *, thread_id: str, turn_id: str) -> TurnActivitySnapshot | None:
        execution = self._active.get(binding_id)
        if execution and execution.session == thread_id and not execution.broken:
            human = execution.humans.get(turn_id)
            if human and human.written and execution.live.init_human == turn_id:
                return execution.activity(turn_id, human.binding.task_feedback)
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
            if execution.finishing:
                # The client close can take several seconds (longer with background
                # tasks); saying "nothing is running" here reads as a lost answer.
                raise OrdinaryInputRejected(self._closing_notice(execution))
            if not execution.stoppable:
                return None
            return StopPreview("正在请求停止当前 Claude 执行。" if execution.writing else "正在取消 Claude 会话启动。",
                               execution.revision, execution.input_id)

    @staticmethod
    def _closing_notice(execution: _Execution) -> str:
        if execution.concluded and not execution.delivery.queues:
            return "这次 Claude 执行已经结束，客户端正在收尾。"
        return CLOSING_CONCLUDED if execution.concluded else CLOSING

    def _exact(self, binding_id: str, revision: int | None, turn_id: str | None) -> _Execution:
        execution = self._active.get(binding_id)
        if execution is None or (revision, turn_id) != (execution.revision, execution.input_id):
            raise ThreadActivityChanged("目标执行已变化，请刷新后重试。")
        if execution.finishing:
            raise OrdinaryInputRejected(self._closing_notice(execution))
        if not execution.stoppable:
            raise ThreadActivityChanged("目标执行已结束。")
        return execution

    async def stop_control(self, binding_id: str, *, sender_id: str, revision: int | None, turn_id: str | None,
                           all_tasks: bool, acknowledge: Callable[[str], Awaitable[None]]) -> StopDisposition:
        if sender_id != self._owner:
            raise OrdinaryInputRejected("这个 Claude 实例仅限所有者使用。")
        return await self._stop(binding_id, revision, turn_id, all_tasks=all_tasks, receipt=acknowledge)

    async def stop_exact(self, binding_id: str, *, acknowledge: StopAcknowledger | None = None,
                         expected_activity_revision: int | None = None,
                         expected_turn_id: str | None = None) -> StopDisposition:
        return await self._stop(binding_id, expected_activity_revision, expected_turn_id, all_tasks=False,
                                receipt=(lambda _: acknowledge()) if acknowledge else None)

    async def _stop(self, binding_id: str, revision: int | None, turn_id: str | None, *, all_tasks: bool,
                    receipt: Callable[[str], Awaitable[None]] | None) -> StopDisposition:
        barrier = asyncio.Event() if receipt else None
        opening: asyncio.Task[None] | None = None
        try:
            async with self._lock(binding_id):
                execution = self._active.get(binding_id)
                if binding_id in self._unavailable:
                    raise OrdinaryInputRejected(CLOSE_FAILED)
                if (revision, turn_id) != (None, None) and (
                        not execution or (revision, turn_id) != (execution.revision, execution.input_id)):
                    raise ThreadActivityChanged("目标执行已变化，请刷新后重试。")
                if execution is None:
                    raise OrdinaryInputRejected("当前没有正在执行的任务。")
                if execution.finishing:
                    raise OrdinaryInputRejected(self._closing_notice(execution))
                live = execution.live
                tasks = dict(live.tasks) if all_tasks else {}
                if not tasks and not execution.stoppable:
                    if live.tasks:
                        raise OrdinaryInputRejected(f"当前没有可中断的前台执行，仍有 {len(live.tasks)} 个后台任务。可发送 /stop all。")
                    raise OrdinaryInputRejected("当前没有正在执行的任务。")
                if not tasks and execution.stopping_current:
                    return StopDisposition.STOPPING
                if barrier:
                    execution.stop_receipt = barrier
                client = execution.client
                unknown = False
                if execution.stoppable and not execution.stopping_current:
                    target = live.physical
                    pending = execution.pending
                    execution.stop_target = target
                    execution.stopping = True
                    live.stop_requested.update(live.tasks)
                    if not execution.writing:
                        self._cancel_opening(execution)
                        opening = execution.task
                    elif client is not None:
                        try:
                            async with asyncio.timeout(self._remaining(_CONTROL_SECONDS)):
                                await external_call(client.interrupt())
                        except Exception as error:
                            still_running = not execution.finishing and (
                                live.physical == target if target is not None else execution.pending is pending
                            )
                            if still_running:
                                execution.stopping = False
                            if not all_tasks:
                                if not still_running and not isinstance(error, ExternalCallCancelled):
                                    return StopDisposition.NOT_RUNNING
                                raise TerminalStateUnknown("Claude 停止结果未知，请刷新 /status 核对。") from None
                            unknown = True
                requested = 0
                for task, run in tasks.items():
                    if execution.finishing or execution.broken or self._active.get(binding_id) is not execution:
                        break
                    if live.tasks.get(task) != run:
                        continue
                    assert client is not None
                    live.stop_requested.add(task)
                    owner = live.tool_owner.get(live.run_tool.get(run, ""))
                    if owner and owner.human and owner.human in execution.humans:
                        execution.humans[owner.human].stop_requested = True
                    # Membership can empty before the SDK acknowledges this control.
                    execution.awaiting_task_stop = True
                    try:
                        async with asyncio.timeout(self._remaining(_CONTROL_SECONDS)):
                            await external_call(client.stop_task(task))
                        requested += 1
                    except Exception:
                        unknown = True
                    finally:
                        execution.awaiting_task_stop = False
                        self._reconcile(execution)
                count = len(live.tasks)
                text = (f"已请求停止当前工作，已向 {requested} 个后台任务发送停止请求。当前观测到 {count} 个后台任务。"
                        if all_tasks else f"已请求中断；当前观测到 {count} 个后台任务。/stop all 可停止剩余任务。")
                if unknown:
                    text += "部分停止结果未知，请刷新 /status 核对。"
            if receipt:
                try:
                    async with asyncio.timeout(self._remaining(_CONTROL_SECONDS)):
                        await external_call(receipt(text))
                except Exception:
                    logger.warning("Claude stop receipt failed for Binding %s", binding_id)
        finally:
            if barrier:
                barrier.set()
        if opening:
            await asyncio.wait([opening], timeout=self._remaining(_CLOSE_SECONDS))
        return StopDisposition.REQUESTED

    async def recheck_turn_exact(self, binding_id: str, *, expected_activity_revision: int,
                                 expected_turn_id: str) -> ActiveTurnSnapshot:
        async with self._lock(binding_id):
            execution = self._exact(binding_id, expected_activity_revision, expected_turn_id)
            return ActiveTurnSnapshot(binding_id, execution.session, execution.input_id, self._owner,
                                      ActiveState.STOPPING if execution.stopping_current else ActiveState.RUNNING)

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
            execution.human.receipt.set()
            execution.delivery.release_receipts()
            for human in execution.humans.values():
                human.receipt.set()

    async def _shutdown_stop(self, execution: _Execution) -> None:
        try:
            await self.stop_exact(execution.binding.id, expected_activity_revision=execution.revision, expected_turn_id=execution.input_id)
        except (OrdinaryInputRejected, ThreadActivityChanged, TerminalStateUnknown):
            pass

    async def interrupt_all(self, *, deadline: float | None = None) -> None:
        self.close_admission()
        if deadline is not None:
            self._shutdown_deadline = deadline
        for execution in self._active.values():
            if execution.finishing:
                continue
            if execution.live.tasks or execution.live.crons or (execution.pending is None and execution.live.physical is None):
                execution.ending = True
                self._reconcile(execution)
                execution.live.changed.set()
            else:
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
            await self._cancel_presentation()
            raise

    async def wait_idle(self, timeout: float) -> bool:
        deadline = time.monotonic() + self._remaining(timeout)
        while tasks := {task for task in (*self._tasks, *self._controls, *self._closes) if not task.done()}:
            _, pending = await asyncio.wait(tasks, timeout=max(0.0, deadline - time.monotonic()))
            if pending:
                return False
        return not self._active and not self._unavailable

    def _begin_client_cleanup(self) -> None:
        for task in self._controls:
            if not task.cancelling():
                task.cancel()
        for execution in self._tasks.values():
            if execution.finishing:
                continue
            if not execution.writing:
                self._cancel_opening(execution)
            elif execution.task is not None and not execution.task.cancelling():
                execution.task.cancel()

    async def _cancel_presentation(self) -> None:
        await asyncio.gather(*(execution.delivery.cancel() for execution in self._tasks.values()))

    async def cancel_tasks(self) -> None:
        self.close_admission()
        self._begin_client_cleanup()
        if await self.wait_idle(_CLOSE_SECONDS):
            return
        # The execution owns settlement. Only its remaining presentation work
        # can be abandoned after the bounded drain.
        await self._cancel_presentation()
        if not await self.wait_idle(self._remaining(60.0)):
            raise RuntimeError("未能确认所有 Claude 客户端已关闭。")
