"""Public Claude SDK boundary for an ordinary execution."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, AsyncIterator, Callable, Mapping
from dataclasses import asdict, dataclass, field
from enum import Enum, auto
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Any, Protocol

from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ContentBlock, HookContext, HookInput, HookJSONOutput, HookMatcher,
    Message, RateLimitEvent, RateLimitInfo, ResultMessage, StreamEvent, SystemMessage, ToolResultBlock, ToolUseBlock, UserMessage, get_session_info,
)

from .. import claude_installation
from ..claude_installation import verified_claude_cli
from ..ordinary_sessions import OrdinaryInputRejected, PreparedPrompt


@dataclass(slots=True)
class BackgroundTask:
    task_id: str
    tool_id: str
    owned: bool = False
    terminal: str | None = None
    disposition_due: bool = False
    settled: bool = False
    cancel_requested: bool = False


def _id(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _optional(check: Callable[[Any], bool]) -> Callable[[Any], bool]:
    return lambda value: value is None or check(value)


def _rows(*keys: str) -> Callable[[Any], bool]:
    def check(value: Any) -> bool:
        return (isinstance(value, list) and all(isinstance(row, dict) for row in value)
                and all(len({row.get(key) for row in value if _id(row.get(key))}) == len(value) for key in keys))
    return check


def _blocks(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(block, ContentBlock) for block in value)


_LAUNCHERS = {"Bash", "Agent", "Task"}
_NATIVE_TOOLS = _LAUNCHERS | {"TaskStop", "CronCreate", "CronDelete", "CronList"}
_oid = _optional(_id)
_bool = lambda value: type(value) is bool
_object = lambda value: isinstance(value, dict)
_ids = _optional(lambda value: isinstance(value, list) and all(_id(item) for item in value))
_session = {"session_id": _id, "uuid": _oid}
_attributed = _session | {"user_message_uuid": _oid, "user_message_uuids": _ids}
_task = {"task_id": _id, "run_id": _id, "tool_use_id": _oid}

# Paths are relative to each public SDK object. '*' selects inventory rows.
# '?' accepts an absent key while retaining the value's declared shape when present.
CONSUMED_FIELDS: dict[str, dict[str, Callable[[Any], bool]]] = {
    "system": {"subtype": _id, "data": _object},
    "init": _session | {"cwd": _id, "permissionMode": _id, "user_message_uuids": _ids, "model": _oid},
    "session_state_changed": _session | {"state": lambda x: x in ("idle", "running", "requires_action")},
    "background_tasks_changed": _session | {"tasks": _rows("run_id", "task_id"), "tasks.*.run_id": _id,
        "tasks.*.task_id": _id, "tasks.*.task_type": lambda x: x == "local_bash"},
    "task_started": _session | _task | {"tool_use_id": _id, "task_type": _id, "is_backgrounded": _bool},
    "task_updated": _session | _task | {"patch": _object, "patch.status": _oid},
    "task_notification": _session | _task | {"run_id": _oid, "status": _id},
    "assistant": _attributed | {"parent_tool_use_id": _oid, "content": _blocks},
    "stream": _attributed | {"uuid": _id, "parent_tool_use_id": _oid},
    "user": {"uuid": _oid, "parent_tool_use_id": _oid, "origin": _optional(_object), "content": lambda x: isinstance(x, str) or _blocks(x)},
    "tool": {"id": _id, "name": _id}, "launch": {"input": _object, "input.run_in_background?": _bool},
    "taskstop": {"input": _object, "input.task_id": _id},
    "receipt": {"tool_use_id": _id, "is_error": _optional(_bool)},
    "native_result": {"tool_use_result": lambda x: x is None or isinstance(x, (dict, str)),
        "tool_use_result.backgroundTaskId?": _id, "tool_use_result.task_id?": _id, "tool_use_result.task_type?": _id},
    "result": _attributed | {"uuid": _id, "subtype": _id, "terminal_reason": _oid, "is_error": _bool,
        "result": _optional(lambda x: isinstance(x, str)), "errors": _ids, "origin": _optional(_object),
        "origin.kind": _id, "origin.producer?": _id, "origin.runId?": _id,
        "permission_denials": _optional(lambda x: isinstance(x, list)), "api_error_status": _optional(lambda x: type(x) is int),
        "structured_output": lambda x: True},  # SDK Any; only presence is consumed for a bodyless receipt.
    "stop": {"session_id": _id, "agent_id": lambda x: x is None, "prompt_id": _id, "stop_hook_active?": _bool,
        "background_tasks": _rows("id"), "background_tasks.*.id": _id, "background_tasks.*.type": lambda x: x == "shell",
        "background_tasks.*.status": lambda x: x in ("running", "pending", "paused"), "session_crons": _rows("id"),
        "session_crons.*.id": _id, "session_crons.*.recurring": _bool},
    "hook": {"hook_event_name": lambda x: x == "Stop", "agent_id": _oid},
    "callback": {"callback": _id},
    "rate": {"uuid": _id, "session_id": _id, "rate_limit_info": lambda x: isinstance(x, RateLimitInfo)},
}


def _valid_fields(kind: str, data: Mapping[str, Any]) -> bool:
    def matches(obj: Any, path: str, check: Callable[[Any], bool]) -> bool:
        key, _, tail = path.partition(".")
        if key == "*":
            return all(matches(row, tail, check) for row in obj)
        if key.endswith("?") and key[:-1] not in obj:
            return True
        value = obj.get(key.rstrip("?"))
        if tail:
            return not isinstance(value, (dict, list)) or matches(value, tail, check)
        return check(value)
    return all(matches(data, path, check) for path, check in CONSUMED_FIELDS[kind].items())


def _valid_shape(msg: Message, ledger: EvidenceLedger) -> bool:
    if isinstance(msg, SystemMessage):
        return _valid_fields("system", vars(msg)) and (
            msg.subtype not in {"init", "session_state_changed", "background_tasks_changed", "task_started", "task_updated", "task_notification"}
            or _valid_fields(msg.subtype, msg.data))
    if isinstance(msg, ResultMessage):
        return _valid_fields("result", vars(msg))
    if isinstance(msg, RateLimitEvent):
        return _valid_fields("rate", vars(msg))
    if isinstance(msg, StreamEvent):
        return _valid_fields("stream", vars(msg))
    if isinstance(msg, (UserMessage, AssistantMessage)):
        if not _valid_fields("user" if isinstance(msg, UserMessage) else "assistant", vars(msg)):
            return False
        for block in msg.content if isinstance(msg.content, list) else ():
            if isinstance(block, ToolUseBlock):
                if not _valid_fields("tool", vars(block)):
                    return False
                kind = "launch" if block.name in _LAUNCHERS else "taskstop" if block.name == "TaskStop" else None
                if kind and not _valid_fields(kind, vars(block)):
                    return False
            if isinstance(block, ToolResultBlock):
                if not _valid_fields("receipt", vars(block)):
                    return False
                if ("tool", block.tool_use_id) in ledger.claims and not _valid_fields("native_result", vars(msg)):
                    return False
    return True


class Evidence(Enum):
    NEW = auto()
    DUPLICATE = auto()
    CONFLICT = auto()


Subject = tuple[str, str]


@dataclass(slots=True)
class EvidenceLedger:
    # Keys distinguish independent immutable claims about the same native object
    # (e.g. run -> task, run -> tool and run -> terminal status).
    claims: dict[Subject, set[bytes]] = field(default_factory=dict)

    def record(self, kind: str, identity: object, payload: object) -> Evidence:
        if not isinstance(identity, str) or not identity:
            return Evidence.NEW
        variants = self.claims.setdefault((kind, identity), set())
        digest = sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).digest()
        if digest in variants:
            return Evidence.DUPLICATE
        variants.add(digest)
        if len(variants) == 1:
            return Evidence.NEW
        return Evidence.CONFLICT


@dataclass(slots=True)
class ClaudeBackground:
    """Generation-local decoder for 0.2.186 / 2.1.329. Only public SDK observations enter here."""

    session: str
    human: str
    snapshot_changed: Callable[[], None] | None = None
    runs: dict[str, BackgroundTask] = field(default_factory=dict)
    bash_tools: set[str] = field(default_factory=set)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    ledger: EvidenceLedger = field(default_factory=EvidenceLedger)
    before_resumed_input: bool = False
    prior_ids: set[Subject] = field(default_factory=set)
    tasks_seen: set[str] = field(default_factory=set)
    runs_seen: set[str] = field(default_factory=set)
    stop_prompts: set[str] = field(default_factory=set)
    stop_tasks: set[str] = field(default_factory=set)
    crons: set[str] = field(default_factory=set)
    body_results: set[str] = field(default_factory=set)
    current_init: str | None = None
    turn_number: int = 0
    human_open: bool = False
    wake_observed: bool = False
    stop_tools: dict[str, str] = field(default_factory=dict)
    model_stopped: dict[str, str] = field(default_factory=dict)
    turn_runs: set[str] = field(default_factory=set)
    rejected_tools: set[str] = field(default_factory=set)
    requested_tools: set[str] = field(default_factory=set)
    turn_open: bool = False
    members: dict[str, str] | None = None
    background_possible: bool = False
    cron_touched: bool = False
    seen_state: bool = False
    human_context: bool = False
    first_done: bool = False
    wake: bool = False
    idle: bool = False
    degraded: bool = False
    wake_context: bool = False

    async def stop_hook(self, data: HookInput, tool: str | None, context: HookContext) -> HookJSONOutput:
        if not isinstance(data, dict) or not _valid_fields("hook", data):
            self.snapshot(None, tool)
        elif data.get("agent_id") is None:
            self.snapshot(data, tool)
        return {}

    def record(self, kind: str, identity: object, payload: object) -> Evidence:
        verdict = self.ledger.record(kind, identity, payload)
        if verdict is Evidence.CONFLICT:
            self.degraded = True
        return verdict

    def snapshot(self, data: object, callback: str | None = None) -> None:
        if self.degraded:
            return
        try:
            self._snapshot(data, callback)
        except Exception:
            self.degraded = True
        self.changed.set()
        if self.snapshot_changed is not None:
            self.snapshot_changed()

    def _snapshot(self, data: object, callback: str | None) -> None:
        if (not isinstance(data, dict) or not _valid_fields("stop", data) or not _valid_fields("callback", {"callback": callback})
                or data["session_id"] != self.session):
            self.degraded = True
            return
        # Continuations share a prompt; each callback owns its normalized inventory.
        payload = dict(data)
        for key in ("background_tasks", "session_crons"):
            payload[key] = sorted(data[key], key=lambda row: json.dumps(row, sort_keys=True))
        if self.record("callback", callback, payload) is Evidence.NEW:
            self.stop_prompts.add(data["prompt_id"])
            self.stop_tasks.update(row["id"] for row in data["background_tasks"])
            self.crons.update(row["id"] for row in data["session_crons"])

    def _membership(self, run: object, task: object, tool: object = None) -> None:
        if isinstance(task, str) and task:
            self.tasks_seen.add(task)
        if isinstance(run, str) and run:
            self.runs_seen.add(run)
        if run and task:
            self.record("run-task", run, task)
            self.record("task-run", task, run)
        if tool and run:
            self.record("tool-run", tool, run)
            self.record("run-tool", run, tool)
        if tool and task:
            self.record("tool-task", tool, task)
            self.record("task-tool", task, tool)

    def frame(self, msg: Message) -> Evidence:
        data = msg.data if isinstance(msg, SystemMessage) else vars(msg)
        identity = data.get("uuid") if isinstance(data, dict) else None
        if not identity:
            return Evidence.NEW
        payload = asdict(msg)
        if isinstance(msg, SystemMessage) and msg.subtype == "background_tasks_changed" and isinstance(rows := data.get("tasks"), list):
            payload["data"]["tasks"] = sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))
        return self.record("frame", identity, (type(msg).__name__, payload))

    def _prior_replay(self, msg: Message) -> bool:
        if not self.before_resumed_input and not self.prior_ids:
            return False
        identities: list[Subject] = []
        if isinstance(msg, SystemMessage):
            data = msg.data
            if data.get("session_id") != self.session:
                return False
            if msg.subtype == "init" and data.get("user_message_uuids") == [self.human]:
                self.before_resumed_input = False
            if msg.subtype == "background_tasks_changed" and isinstance(rows := data.get("tasks"), list):
                identities = [("run", row["run_id"]) for row in rows if isinstance(row, dict) and isinstance(row.get("run_id"), str)]
            elif msg.subtype in ("task_started", "task_updated", "task_notification"):
                if isinstance(run := data.get("run_id"), str):
                    identities.append(("run", run))
            else:
                return False
        elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
            identities = [("receipt", b.tool_use_id) for b in msg.content if isinstance(b, ToolResultBlock)]
            if not identities:
                return False
        elif isinstance(msg, (AssistantMessage, StreamEvent, ResultMessage)) and msg.session_id == self.session:
            exact = (msg.user_message_uuid == self.human or msg.user_message_uuids == [self.human]) and (
                msg.user_message_uuid in (None, self.human) and msg.user_message_uuids in (None, [], [self.human]))
            if exact and (msg.origin in (None, {"kind": "human"}) if isinstance(msg, ResultMessage)
                          else msg.parent_tool_use_id is None):
                self.before_resumed_input = False
                return False
            if isinstance(msg, StreamEvent):
                return False
            if isinstance(msg, ResultMessage):
                if not (msg.user_message_uuid or msg.user_message_uuids
                        or (isinstance(msg.origin, dict) and msg.origin.get("kind") not in (None, "human"))):
                    return False
                if (run := self.wake_run(msg)) is not None:
                    identities.append(("run", run))
            else:
                identities = [("tool", b.id) for b in msg.content if isinstance(b, ToolUseBlock)]
                if not identities:
                    return False
        else:
            return False
        # State and Stop observations remain live during resume. Only replayed
        # work identities belong to the older generation, even on a later replay.
        if self.before_resumed_input:
            self.prior_ids.update(identities)
            return True
        return bool(identities) and all(key in self.prior_ids for key in identities)

    def observe(self, msg: Message) -> Evidence:
        if self.degraded:
            return Evidence.CONFLICT
        try:
            return self._classify(msg)
        except Exception:
            self.degraded = True
            return Evidence.CONFLICT

    def _classify(self, msg: Message) -> Evidence:
        verdict = self.frame(msg)
        if verdict is not Evidence.NEW:
            return verdict
        if not _valid_shape(msg, self.ledger):
            self.degraded = True
            return Evidence.CONFLICT
        prior = self._prior_replay(msg)
        if isinstance(msg, ResultMessage) and msg.session_id == self.session and msg.uuid:
            run = self.wake_run(msg)
            if run is not None:
                self.record("run-result", run, msg.uuid)
                if not prior and self.first_done:
                    self.runs_seen.add(run)
            elif msg.origin in (None, {"kind": "human"}) and (
                    msg.user_message_uuid == self.human or msg.user_message_uuids == [self.human]):
                self.record("input-result", self.human, msg.uuid)
        self._observe(msg, prior=prior)
        return Evidence.DUPLICATE if prior else verdict

    def _observe(self, msg: Message, *, prior: bool) -> None:
        if isinstance(msg, AssistantMessage):
            if msg.parent_tool_use_id is not None or msg.session_id != self.session:
                return
            blocks = [block for block in msg.content if isinstance(block, ToolUseBlock) and block.name in _NATIVE_TOOLS]
            fresh = []
            for block in blocks:
                attribution = (msg.user_message_uuid, msg.user_message_uuids)
                payload = (block.name, block.input, msg.session_id, attribution,
                           self.current_init if msg.uuid and attribution == (None, None) else None)
                if block.name in _LAUNCHERS and block.input.get("run_in_background") is True:
                    self.requested_tools.add(block.id)
                if self.record("tool", block.id, payload) is not Evidence.DUPLICATE and ("tool", block.id) not in self.prior_ids:
                    fresh.append(block)
            if blocks and not fresh:
                return
            if msg.user_message_uuid or msg.user_message_uuids:
                exact = (msg.user_message_uuid in (None, self.human) and msg.user_message_uuids in (None, [], [self.human]))
                self.human_context = exact and self.turn_open and not self.first_done
                if not exact:
                    self.human_open = False
                if self.first_done:
                    self.wake_context = False
                    self.degraded = True
            for block in fresh:
                if block.name in _LAUNCHERS | {"TaskStop"} and not (self.human_context or self.wake_context):
                    self.degraded = True
                elif block.name == "Bash":
                    self.bash_tools.add(block.id)
                elif block.name == "TaskStop":
                    self.stop_tools[block.id] = block.input["task_id"]
                if block.name in _LAUNCHERS and block.input.get("run_in_background") is True:
                    self.background_possible = True
                if block.name in {"CronCreate", "CronDelete", "CronList"}:
                    self.cron_touched = True
                    if block.name == "CronCreate":
                        self.background_possible = True
            return
        if isinstance(msg, UserMessage) and msg.parent_tool_use_id is None and isinstance(msg.content, list):
            for item in msg.content:
                if not isinstance(item, ToolResultBlock):
                    continue
                receipt = self.record("tool-result", item.tool_use_id, (asdict(item), msg.tool_use_result, msg.origin))
                if receipt is Evidence.DUPLICATE or ("receipt", item.tool_use_id) in self.prior_ids:
                    continue
                if item.tool_use_id in self.stop_tools:
                    target = self.stop_tools.pop(item.tool_use_id)
                    result = msg.tool_use_result
                    # TaskStop success at this pin omits is_error. Its structured
                    # receipt names the exact Bash task. A plain tool ACK cannot settle it.
                    if item.is_error is None or item.is_error is False:
                        if isinstance(result, dict) and result.get("task_id") == target and result.get("task_type") == "local_bash":
                            self.model_stopped[item.tool_use_id] = target
                        else:
                            self.degraded = True
                if item.is_error is True:
                    self.rejected_tools.add(item.tool_use_id)
                if isinstance(msg.tool_use_result, dict) and ("tool", item.tool_use_id) in self.ledger.claims:
                    native_id = msg.tool_use_result.get("backgroundTaskId")
                    if isinstance(native_id, str):
                        self._membership(None, native_id, item.tool_use_id)
            return
        if not isinstance(msg, SystemMessage):
            return
        data = msg.data
        kind = msg.subtype
        if kind not in {"init", "session_state_changed", "task_started", "task_updated", "task_notification",
                        "background_tasks_changed"}:
            return
        if data.get("session_id") != self.session:
            self.degraded = True
            return
        if kind == "init":
            init_id = data.get("uuid")
            self.turn_number += 1
            if self.turn_open and self.turn_runs:
                self.degraded = True
            self.current_init = init_id if isinstance(init_id, str) else None
            self.turn_open = True
            self.human_context = data.get("user_message_uuids") == [self.human]
            self.human_open = self.human_context and not self.first_done
            self.wake_context = self.first_done and data.get("user_message_uuids") in (None, [])
            if self.wake_context:
                for record in self.runs.values():
                    record.disposition_due = False
            self.wake_observed |= self.wake_context
            if self.first_done and data.get("user_message_uuids") not in (None, []):
                self.degraded = True
            self.bash_tools.clear()
            self.stop_tools.clear()
            self.model_stopped.clear()
            self.turn_runs.clear()
            self.wake = self.first_done
            self.idle = False
        elif kind == "session_state_changed":
            state = data.get("state")
            self.seen_state = True
            self.idle = state == "idle"
            if self.idle:
                self.human_open = False
                for record in self.runs.values():
                    if record.terminal and not record.settled:
                        record.disposition_due = True
            if self.first_done and not self.idle and not self.wake:
                self.wake = True
                self.wake_observed = True
        elif kind == "background_tasks_changed":
            rows = data["tasks"]
            for row in rows:
                self._membership(row["run_id"], row["task_id"])
            if prior:
                return
            if self.prior_ids:
                rows = [row for row in rows if ("run", row["run_id"]) not in self.prior_ids]
            members = {row["run_id"]: row["task_id"] for row in rows}
            if self.members != members:
                self.members = members
                self.idle = False
        else:
            self._task(kind, data, prior=prior)

    def _task(self, kind: str, data: dict[str, Any], *, prior: bool) -> None:
        task, run, tool = data["task_id"], data.get("run_id"), data.get("tool_use_id")
        if not run and kind == "task_notification" and not self.first_done and not self.runs:
            self.human_open = self.human_context = False
            return  # A replay from a previous client has no matching current-generation start.
        self._membership(run, task, tool)
        if not run:
            self.degraded = True
            return
        record = self.runs.get(run)
        if kind == "task_started":
            start = self.record("start", run, (task, tool, data.get("task_type"), data.get("is_backgrounded")))
            if start is not Evidence.NEW or prior:
                return
            if (data.get("task_type") != "local_bash" or data.get("is_backgrounded") is not True
                    or not isinstance(tool, str) or tool not in self.bash_tools):
                self.degraded = True
            else:
                self.runs[run] = BackgroundTask(task, tool)
                self.turn_runs.add(run)
            return
        if kind == "task_updated":
            status = data["patch"].get("status")
            if status in (None, "pending", "running", "paused"):
                return
        else:
            status = data.get("status")
        if status == "stopped":
            status = "killed"
        if status not in ("completed", "failed", "killed"):
            self.degraded = True
        elif self.record("terminal", run, status) is Evidence.NEW and not prior:
            if record is None or (kind == "task_notification" and tool != record.tool_id):
                self.degraded = True
            else:
                record.terminal = status
                self.idle = False

    def finish_turn(self, *, attributed: bool) -> None:
        self.human_open = False
        if self.pending_tools:
            self.degraded = True
        if self.wake:
            for record in self.runs.values():
                if record.terminal and not record.settled:
                    record.disposition_due = True
        self.turn_open = False
        if attributed:
            stopped = self.model_stopped.values()
            for record in self.runs.values():
                if record.task_id in stopped:
                    record.cancel_requested = True
            for run in self.turn_runs:
                self.runs[run].owned = True
        elif self.turn_runs:
            self.degraded = True
        if not self.seen_state:
            self.degraded = True
        self.turn_runs.clear()
        self.bash_tools.clear()
        self.human_context = False
        self.wake = False
        self.idle = False

    def credit(self, msg: ResultMessage, identity: str | None) -> None:
        if (identity and (msg.subtype, msg.terminal_reason, msg.is_error) == ("success", "completed", False)
                and msg.is_error is False and isinstance(msg.result, str) and msg.result.strip()):
            self.body_results.add(identity)

    def skip_foreign_result(self) -> None:
        if self.turn_runs or self.pending_tools:
            self.finish_turn(attributed=False)
        self.human_open = False
        self.bash_tools.clear()
        self.stop_tools.clear()
        self.model_stopped.clear()
        self.human_context = False

    def wake_run(self, msg: ResultMessage) -> str | None:
        origin: object = msg.origin
        if (not isinstance(origin, Mapping) or set(origin) != {"kind", "producer", "runId"}
                or origin.get("kind") != "task-notification" or origin.get("producer") != "session-task"
                or not isinstance(run := origin.get("runId"), str)):
            return None
        return run

    def wake_result(self, msg: ResultMessage) -> BackgroundTask | None:
        run = self.wake_run(msg)
        if run is None:
            return None
        self.runs_seen.add(run)
        task = self.runs.get(run)
        if task is None or not task.owned or not task.terminal:
            return None
        return task

    def settle_cancellations(self) -> None:
        if self.degraded or not self.idle or self.wake or self.members is None:
            return
        for run, task in self.runs.items():
            if not task.settled and task.owned and task.cancel_requested and task.terminal == "killed" and run not in self.members:
                task.settled = True

    @property
    def single_task(self) -> BackgroundTask | None:
        if (not self.first_done or not self.idle or self.wake or self.degraded
                or self.members is None or len(self.members) != 1):
            return None
        run, task_id = next(iter(self.members.items()))
        task = self.runs.get(run)
        if (task and task.owned and task.task_id == task_id and task_id in self.stop_tasks
                and not (self.pending_tasks - {task_id}) and not self.reader_behind
                and not task.terminal and not task.settled
                and all(other is task or other.terminal for other in self.runs.values())):
            return task
        return None

    @property
    def outstanding(self) -> bool:
        return any(not task.settled for task in self.runs.values())

    @property
    def reader_behind(self) -> bool:
        return len(self.stop_prompts) > len(self.body_results)

    @property
    def pending_tasks(self) -> set[str]:
        tasks = self.tasks_seen | self.stop_tasks
        return tasks - {t.task_id for t in self.runs.values() if t.settled}

    @property
    def pending_tools(self) -> set[str]:
        return self.requested_tools - self.rejected_tools - {t.tool_id for t in self.runs.values()}

    @property
    def handoff(self) -> bool:
        return self.background_possible or bool(self.tasks_seen or self.stop_tasks or self.crons) or self.wake_observed or self.degraded

    @property
    def unresolved_work(self) -> bool:
        return bool(self.degraded or self.outstanding or self.runs_seen - self.runs.keys()
                    or self.pending_tools or self.members or self.pending_tasks or self.crons
                    or self.cron_touched or self.wake)

    def pending_proofs(self) -> set[tuple[str, str]]:
        gaps = set()
        for run in self.runs_seen:
            record = self.runs.get(run)
            if record is None:
                gaps.add((run, "start"))
            elif record.settled:
                if self.members and run in self.members:
                    gaps.add((run, "removal"))
            elif record.terminal:
                if record.disposition_due:
                    gaps.add((run, "disposition"))
            elif not self.members or self.members.get(run) != record.task_id:
                gaps.add((run, "membership"))
        if not self.idle and not self.wake and not any(r.terminal and not r.settled for r in self.runs.values()):
            gaps.add(("", "idle"))
        if self.reader_behind and not self.wake:
            gaps.add(("", "stop result"))
        if self.wake and self.idle:
            gaps.add(("", "wake result"))
        gaps.update((task, "start") for task in self.pending_tasks - {r.task_id for r in self.runs.values()})
        return gaps

    @property
    def abandonable(self) -> bool:
        if self.degraded or (self.turn_open and self.wake_context and not self.idle):
            return False
        if not self.first_done or not self.idle or self.wake or self.outstanding or self.runs_seen - self.runs.keys():
            return False
        return bool(self.crons) or self.cron_touched

    @property
    def closable(self) -> bool:
        return (self.first_done and self.idle and not self.degraded and not self.reader_behind and not self.unresolved_work
                and (not self.runs or self.members == {}))


class ClaudeClient(Protocol):
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def interrupt(self) -> None: ...
    async def stop_task(self, task_id: str) -> None: ...
    async def query(self, prompt: AsyncIterable[dict[str, Any]]) -> None: ...
    def receive_messages(self) -> AsyncIterator[Message]: ...


class ClaudeClients(Protocol):
    def create(self, session_id: str, cwd: Path, *, resume: bool, background: ClaudeBackground | None = None) -> ClaudeClient: ...
    async def session_cwd(self, session_id: str, candidates: tuple[Path, ...] = ()) -> Path | None: ...


class BundledClaudeClients:
    def __init__(self) -> None:
        self._cli = verified_claude_cli()
        if (claude_installation.SDK_VERSION, claude_installation.CLI_VERSION) != ("0.2.186", "2.1.329"):
            raise RuntimeError("Claude SDK 或 CLI 固定版本已变化，须重新验证后台任务归属适配器。")

    def create(self, session_id: str, cwd: Path, *, resume: bool, background: ClaudeBackground | None = None) -> ClaudeClient:
        state_option = "CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS"
        observe = background is not None
        if background is not None and os.environ.get(state_option, "1") != "1":
            background.degraded = True
        return ClaudeSDKClient(options=ClaudeAgentOptions(
            cli_path=self._cli, cwd=cwd, session_id=None if resume else session_id,
            resume=session_id if resume else None, permission_mode="auto", verbatim_prompts=True,
            env={state_option: "1"} if observe and state_option not in os.environ else {},
            hooks={"Stop": [HookMatcher(hooks=[background.stop_hook])]} if background is not None else None,
            system_prompt={"type": "preset", "preset": "claude_code"},
        ))

    async def session_cwd(self, session_id: str, candidates: tuple[Path, ...] = ()) -> Path | None:
        return await asyncio.to_thread(self._lookup_cwd, session_id, candidates)

    @staticmethod
    def _lookup_cwd(session_id: str, candidates: tuple[Path, ...]) -> Path | None:
        # The public message reader searches sibling worktrees and its messages
        # carry no source cwd. Only a native summary with an exact cwd can resume.
        info = get_session_info(session_id)
        if info is None or info.session_id != session_id or not info.cwd:
            return None
        cwd = Path(info.cwd)
        if not cwd.is_absolute() or not cwd.is_dir():
            return None
        resolved = cwd.resolve(strict=True)
        if candidates and not any(path.is_absolute() and path.resolve() == resolved for path in candidates):
            return None
        return resolved


def user_message(prompt: PreparedPrompt, input_id: str) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = []
    for image in prompt.images:
        header, separator, data = image.data_url.partition(",")
        media = header.removeprefix("data:").removesuffix(";base64")
        if (not separator or not data or header != f"data:{media};base64"
                or media not in {"image/png", "image/jpeg", "image/webp", "image/gif"}):
            raise OrdinaryInputRejected("图片格式无法提交，本条输入未发送。")
        blocks.append({"type": "text", "text": image.label})
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": data}})
    if prompt.text:
        blocks.append({"type": "text", "text": prompt.text})
    return {"type": "user", "uuid": input_id, "session_id": "default", "parent_tool_use_id": None,
            "message": {"role": "user", "content": blocks}}


async def single_message(message: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    yield message
