"""Safe projection of the pinned Claude client's observed tool calls and results."""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from claude_agent_sdk import AssistantMessage, ToolResultBlock, ToolUseBlock, UserMessage

from ..file_evidence import FileEvidence, OutputPath
from ..turn_activity import (
    TurnActivityEvent, TurnActivityKind, TurnActivityStatus, TurnPlanStepSnapshot,
    TurnPlanStepState, operation_summary, sanitize_activity_text,
)


_DETAILS = {
    "Bash": (TurnActivityKind.COMMAND, "执行命令", ("command",)),
    "Read": (TurnActivityKind.COMMAND, "读取文件", ("file_path",)),
    "Glob": (TurnActivityKind.COMMAND, "搜索文件", ("pattern",)),
    "Grep": (TurnActivityKind.COMMAND, "搜索内容", ("pattern", "path")),
    "Write": (TurnActivityKind.FILE_CHANGE, "写入文件", ("file_path",)),
    "Edit": (TurnActivityKind.FILE_CHANGE, "编辑文件", ("file_path",)),
    "NotebookEdit": (TurnActivityKind.FILE_CHANGE, "编辑笔记本", ("notebook_path",)),
    "WebFetch": (TurnActivityKind.WEB_SEARCH, "打开网页", ("url",)),
    "WebSearch": (TurnActivityKind.WEB_SEARCH, "搜索网页", ("query",)),
}
_PUBLIC_TOOLS = frozenset({
    "Skill", "ToolSearch", "TaskStop", "CronCreate", "CronDelete", "CronList", "ScheduleWakeup",
    "EnterPlanMode", "ExitPlanMode", "AskUserQuestion", "EnterWorktree", "ExitWorktree",
    "ListAgents", "SendMessage", "ReportFindings", "Monitor", "Workflow",
})
_CHECKLIST = frozenset({"TodoWrite", "TaskCreate", "TaskUpdate", "TaskGet", "TaskList"})
_STATUSES = {"pending": TurnPlanStepState.PENDING, "in_progress": TurnPlanStepState.IN_PROGRESS,
             "completed": TurnPlanStepState.COMPLETED}


def _string(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing Claude display field")
    return value


def _step(label: object, status: object) -> TurnPlanStepSnapshot:
    text = sanitize_activity_text(_string(label))
    if text is None or not isinstance(status, str) or status not in _STATUSES:
        raise ValueError("unavailable Claude checklist step")
    return TurnPlanStepSnapshot(text, _STATUSES[status])


class ClaudeChecklist:
    """Ordered steps observed on this connection, or unknown until a full baseline."""

    def __init__(self) -> None:
        self.rows: dict[str, TurnPlanStepSnapshot] | None = {}

    def observe_child(self, frame: AssistantMessage) -> None:
        if frame.parent_tool_use_id is not None and any(
            isinstance(b, ToolUseBlock) and isinstance(b.name, str) and b.name in _CHECKLIST for b in frame.content
        ):
            self.rows = None

    def apply(self, tool: str, payload: object) -> bool:
        """Return whether this result contributed a checklist observation."""
        try:
            if not isinstance(payload, dict):
                raise ValueError("missing Claude checklist result")
            if tool == "TaskGet":
                if "task" not in payload or not isinstance(payload["task"], (dict, type(None))):
                    raise ValueError("invalid Claude task lookup")
                return False
            if tool == "TaskUpdate":
                return self._update(payload)
            if tool == "TaskCreate":
                task = payload.get("task")
                if not isinstance(task, dict):
                    raise ValueError("invalid Claude task creation")
                key = _string(task.get("id"))
                step = _step(task.get("subject"), "pending")
                if self.rows is not None:
                    if key in self.rows:
                        raise ValueError("duplicate Claude checklist ID")
                    self.rows[key] = step
            else:
                todo = tool == "TodoWrite"
                items = payload.get("newTodos" if todo else "tasks")
                if not isinstance(items, list):
                    raise ValueError("missing Claude checklist baseline")
                rows: dict[str, TurnPlanStepSnapshot] = {}
                for index, item in enumerate(items):
                    if not isinstance(item, dict):
                        raise ValueError("invalid Claude checklist entry")
                    key = f"todo:{index}" if todo else _string(item.get("id"))
                    if key in rows:
                        raise ValueError("duplicate Claude checklist ID")
                    rows[key] = _step(item.get("content" if todo else "subject"), item.get("status"))
                self.rows = rows
        except ValueError:
            self.rows = None
        return True

    def _update(self, payload: dict[str, Any]) -> bool:
        if payload.get("success") is False:
            return False
        key = _string(payload.get("taskId"))
        fields = payload.get("updatedFields")
        if payload.get("success") is not True or not isinstance(fields, list) or not all(isinstance(k, str) for k in fields):
            raise ValueError("invalid Claude task update")
        change = payload.get("statusChange")
        status = None
        if "statusChange" in payload:
            if not isinstance(change, dict):
                raise ValueError("invalid Claude status change")
            status = _string(change.get("to"))
            if status != "deleted" and status not in _STATUSES:
                raise ValueError("unknown Claude checklist status")
        if self.rows is None or key not in self.rows:
            return False
        if "subject" in fields:
            # An update can rename a step without returning its effective subject.
            self.rows = None
        elif status == "deleted":
            del self.rows[key]
        elif status is not None:
            self.rows[key] = replace(self.rows[key], status=_STATUSES[status])
        return True


def _start(block: ToolUseBlock, name: str, now: int, limit: int) -> TurnActivityEvent:
    kind, text = TurnActivityKind.TOOL, None
    if detail := _DETAILS.get(name):
        kind, label, fields = detail
        args = block.input if isinstance(block.input, dict) else {}
        values = tuple(args.get(key) for key in fields)
        text = operation_summary(label, *(v for v in values if isinstance(v, str)), limit=limit)
    elif name in ("Agent", "Task"):
        kind = TurnActivityKind.SUBAGENT
    elif name in _PUBLIC_TOOLS:
        text = name
    return TurnActivityEvent(block.id, kind, TurnActivityStatus.IN_PROGRESS, now, text)


def _output(tool: str, payload: object) -> OutputPath | None:
    if not isinstance(payload, dict):
        return None
    if tool in ("Write", "Edit"):
        path = payload.get("filePath")
    elif tool == "NotebookEdit" and payload.get("error") == "":
        path = payload.get("notebook_path")
    else:
        return None
    return OutputPath(path) if isinstance(path, str) and path.strip() and "\x00" not in path else None


class ClaudeTurnTools:
    """One physical init's calls. Retain only identities, safe rows and proven output paths."""

    def __init__(self, checklist: ClaudeChecklist, operation_max_chars: int) -> None:
        self.checklist = checklist
        self.limit = operation_max_chars
        self.plan_generated = False
        self._ids: set[str] = set()
        self._pending: dict[str, str] = {}
        self._open: dict[str, TurnActivityEvent] = {}
        self._outputs: dict[str, OutputPath] = {}

    def assistant(self, frame: AssistantMessage, now: int) -> tuple[TurnActivityEvent, ...]:
        parent = frame.parent_tool_use_id
        if parent is not None:
            return ()
        events = []
        for block in frame.content:
            if not isinstance(block, ToolUseBlock) or block.id in self._ids:
                continue
            self._ids.add(block.id)
            name = block.name if isinstance(block.name, str) else ""
            # Never retain private tool names or arbitrary input bodies.
            self._pending[block.id] = name if name in _CHECKLIST or name in _DETAILS else ""
            if name not in _CHECKLIST:
                event = _start(block, name, now, self.limit)
                events.append(event)
                self._open[block.id] = event
        return tuple(events)

    def user(self, frame: UserMessage, now: int) -> tuple[TurnActivityEvent, ...]:
        if frame.parent_tool_use_id is not None or isinstance(frame.content, str):
            return ()
        results = [b for b in frame.content if isinstance(b, ToolResultBlock)]
        # The envelope has one result payload. Multiple blocks cannot prove which call owns it.
        payload = frame.tool_use_result if len(results) == 1 else None
        events = []
        for result in results:
            key = result.tool_use_id
            name = self._pending.pop(key, None)
            if name is None:
                continue
            if row := self._open.pop(key, None):
                status = TurnActivityStatus.FAILED if result.is_error is True else TurnActivityStatus.COMPLETED
                events.append(replace(row, status=status, event_timestamp_ms=now))
            if result.is_error is True:
                continue
            if name in _CHECKLIST:
                if self.checklist.apply(name, payload):
                    self.plan_generated = True
            elif path := _output(name, payload):
                self._outputs[key] = path
        return tuple(events)

    def finish(self, status: TurnActivityStatus, now: int) -> tuple[tuple[TurnActivityEvent, ...], FileEvidence]:
        closed = tuple(replace(e, status=status, event_timestamp_ms=now) for e in self._open.values())
        outputs = tuple(self._outputs.values()) if status is TurnActivityStatus.COMPLETED else ()
        self._open.clear()
        self._pending.clear()
        return closed, FileEvidence(outputs=outputs)
