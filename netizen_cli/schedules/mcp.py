"""The single scheduled-plan MCP tool, on the owning application's loop.

MCP framing, initialization and Streamable HTTP are owned by the official SDK.
This adapter supplies only the management schema, call identity and a bounded
loopback listener. It never reads or writes Codex configuration files.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from mcp.server import ServerRequestContext
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
    ToolAnnotations,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


from ..mcp_transport import EmbeddedMcpRunner, MAX_BODY_BYTES, native_thread_id as _native_thread_id
from ..worktrees.policy import WorkspacePolicy, WorkspacePolicyError


logger = logging.getLogger(__name__)
ManagementCallback = Callable[[dict[str, Any], str | None], Awaitable[dict[str, Any]]]
CALL_TIMEOUT_SECONDS = 15.0
HTTP_TIMEOUT_SECONDS = 20.0
_REQUIRED_FIELDS = {
    "view": ("plan_id",),
    "create": ("name", "instructions", "schedule", "request_id"),
    "update": ("plan_id", "expected_revision", "request_id"),
    "delete": ("plan_id", "expected_revision", "request_id"),
    "run_now": ("plan_id", "expected_revision", "request_id"),
    "runs": ("plan_id",),
}
_SCHEDULE_REQUIRED = {
    "once": ("at",), "daily": ("at",),
    "weekly": ("at", "weekdays"), "interval": ("every_minutes",),
}
CREATE_EXAMPLE = {
    "mode": "create", "name": "Daily check", "instructions": "Report the current project status.",
    "schedule": {"kind": "daily", "at": "09:00"}, "timezone": "Asia/Shanghai",
    "enabled": False, "request_id": "6d2c05a2-5da6-4f57-a433-c1ea5fbb2a6f",
}
RUN_NOW_EXAMPLE = {
    "mode": "run_now", "plan_id": "<exact plan_id from list/view>",
    "expected_revision": 1, "request_id": "6d2c05a2-5da6-4f57-a433-c1ea5fbb2a6f",
}
_RUN_NOW_FIELDS = frozenset({"mode", "plan_id", "expected_revision", "request_id"})

INSTRUCTIONS = """Manage Netizen scheduled tasks with cron_manage: create, list, view, update, pause, enable, delete, or run an existing plan now for recurring work, reminders, monitoring, and follow-ups.

When the user asks to manage scheduled tasks, use cron_manage and follow its schema. If the tool is deferred, find it through the available tool search first.

Plans default to target_kind=new_topic: each occurrence starts an independent persistent Codex Thread in a new Feishu topic. Its workspace policy chooses the Project directory (default), an exact existing managed Worktree, or one new detached Worktree per accepted occurrence. Use cron_manage options with project for plan workspace choices; list_worktrees remains the ordinary caller-Project discovery tool. For follow-ups in the same conversation, choose target_kind=binding: each occurrence submits an input to the exact original Binding, starting a Turn or steering the running Turn through ordinary message handling and inheriting its actual working directory. Omit target_binding_id to use the calling ordinary Thread's Binding. Switching away or archiving suspends automatic execution; restoring the target resumes future times without catch-up. Deleting the Binding deletes its plans. Pausing prevents automatic claims; deleting prevents all new claims. An already claimed occurrence may continue. Stop or continue work through ordinary conversation controls. Save clear task instructions in the plan.
When asked to run an existing plan once now, use run_now. Enabling a plan uses update and waits for its next scheduled time; changing its recurring time also uses update. Ordinary work without an existing-plan context remains an ordinary task.
"""

TOOL_DESCRIPTION = """Manage Netizen scheduled tasks with cron_manage: options, list, view, create, update (including pause/enable), delete, run_now, and recent runs. Follow the schema and report success only from the returned result.
Find candidates by name before using an exact plan_id; clarify ambiguous matches. Resolve relative times into a structured schedule and IANA timezone, and save reusable instructions with explicit resource references. target_kind=new_topic (default) starts an independent ordinary Thread in a new Feishu topic. target_kind=binding submits to the exact original Binding and shares its context, current settings and ordinary start/steer behavior. For requests such as 'continue here in ten minutes', use binding. Pausing prevents automatic claims; deleting prevents all new claims. Already claimed work may continue and uses ordinary stop/continue controls.
For create/update/delete/run_now supply a UUID request_id; retry the same request with the same ID after a timeout. For update/delete/run_now copy the exact current revision from the latest list/view/create/update result into expected_revision; never increment it yourself. An existing current result does not require another view call. Omitted chat_id/project on create use the calling native Thread's Binding: the current Feishu conversation may be a group or a private chat; a topic uses its containing conversation. list defaults to that current conversation; all=true lists this instance's plans. update keeps omitted fields unchanged. No native Thread identity belongs in tool arguments.
run_now executes the saved plan once, including manually paused or ended plans, without changing its schedule, next due time or enabled state. Supply only mode, plan_id, expected_revision and request_id; instructions and destination come from that saved version. The result is an acceptance receipt with run_id and current run information, not proof of completion. Results go to the saved target; share the returned feishu_url when available. For new_topic an unfinished or uncertain previous initial Turn blocks another run. For binding, started/steered records prove input acceptance only: subsequent scheduled inputs remain independent even after an unknown outcome, while ordinary Runtime admission still applies. Inactive targets cannot run_now; this never switches or restores a conversation. Inspect view/runs when blocked; do not rewrite or duplicate the plan or execute its instructions yourself as a workaround. After a management timeout, reuse the original request_id; never generate a new one merely to retry an uncertain trigger.
For binding, omitted target_binding_id uses the calling ordinary Thread's exact Binding; Side or unmapped callers must supply an exact Binding ID returned by options. Native Thread IDs are never tool arguments. The target identity is immutable after creation. Do not supply project, chat_id, session_settings or workspace for binding: these follow the target conversation and its actual working directory at execution time. enabled remains the user's pause preference; suspended_reason separately reports automatic suspension, and can_run_now reports target eligibility. options supplies source_binding_id and binding_targets; list may filter target_kind/target_binding_id.
For new_topic, create copies the calling Binding's session settings once; later Binding changes do not change the plan. session_settings is a partial override: turn_settings=null explicitly inherits native Codex settings; a model override supplies all three IDs. Other fields control reaction pulse, progress card and message context. Use options to discover current model/effort/service-tier IDs and defaults when choosing settings; ordinary creation needs no preliminary options call. A private target supports only current-only; an inherited catch-up default is normalized, but explicitly requesting catch-up is rejected. Updating unrelated fields retains the plan's settings even if its model is no longer available.
For new_topic, workspace is a complete policy object: {"mode":"project"}, {"mode":"existing","worktree_id":"exact managed ID"}, {"mode":"new"}, or {"mode":"new","ref":"refs/heads/main"}. Omitted workspace on create uses the Project directory; omitted workspace on update preserves the saved policy. It never reads chat defaults or inherits the caller's Worktree. existing shares that resource's current files and branch; new creates one detached Worktree per accepted run, using that execution's Project HEAD when ref is omitted. Saving/options never creates directories. Use options with project and optional expected_project_revision to discover available choices; an exact known ID can be used directly without another lookup. Missing, replaced or unreadable resources block execution without fallback; inspect workspace_error and can_run_now. enabled stays unchanged. Unrelated edits may preserve an unavailable policy. run_now cannot override workspace. Do not create Worktrees yourself to simulate the new policy.
Recurring daily/weekly/interval schedules have no cutoff by default. Optional schedule.end_at is an inclusive cutoff timestamp with an explicit UTC offset and minute precision; turn a requested cutoff date into a specific time and timezone. Once schedules cannot use end_at.
Providing schedule on update replaces the whole rule: include unchanged kind/time/timezone and end_at to keep them. To remove end_at, omit it or set it to null in the replacement schedule. Omitting schedule entirely keeps the existing rule and cutoff.
Use the exact fields mode, instructions and schedule.kind. After invalid_request, correct the indicated fields and call cron_manage again; no shell commands or resource listing are needed to repair arguments.
Correct paused-create example (replace the request_id for a new request):
""" + json.dumps(CREATE_EXAMPLE)


class _ScheduleArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, json_schema_extra={
        "allOf": [
            {"if": {"properties": {"kind": {"const": kind}}}, "then": {
                "required": list(fields),
                "properties": {field: {"not": {"type": "null"}, **({"minItems": 1} if field == "weekdays" else {})} for field in fields},
            }}
            for kind, fields in _SCHEDULE_REQUIRED.items()
        ] + [{"if": {"properties": {"kind": {"const": "once"}}}, "then": {
            "properties": {"end_at": {"type": "null"}},
        }}],
    })

    kind: Literal["once", "daily", "weekly", "interval"]
    timezone: str | None = Field(default=None, min_length=1, description="IANA timezone; may instead be supplied in the outer timezone field. If both exist they must match.")
    at: str | None = Field(default=None, min_length=1, description="Required for once/daily/weekly. once: ISO timestamp with explicit UTC offset, to minute precision, e.g. 2030-01-02T09:00+08:00; daily/weekly: HH:mm. Omit for interval.")
    weekdays: list[Annotated[int, Field(ge=0, le=6)]] | None = Field(default=None, description="Required nonempty list for weekly: Monday=0 through Sunday=6. Omit for other kinds.")
    every_minutes: int | None = Field(default=None, ge=1, description="Required positive integer for interval. Omit for other kinds.")
    anchor: int | float | None = Field(default=None, allow_inf_nan=False, description="Interval UTC epoch anchor. The service generates this; omit it when creating a plan.")
    end_at: str | None = Field(default=None, min_length=1, description="Recurring kinds only. Inclusive cutoff as an ISO timestamp with explicit UTC offset and minute precision, e.g. 2030-01-31T23:59+08:00. Resolve date-only requests to a specific time. Omit/null means no cutoff in a replacement schedule.")

    @model_validator(mode="after")
    def required_for_kind(self) -> _ScheduleArguments:
        if any(getattr(self, key) is None for key in _SCHEDULE_REQUIRED[self.kind]):
            raise ValueError("Required schedule fields are missing.")
        if self.kind == "weekly" and not self.weekdays:
            raise ValueError("Weekly schedule requires weekdays.")
        if self.kind == "once" and self.end_at is not None:
            raise ValueError("Only recurring schedules support end_at.")
        return self


class _TurnSettingsArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    model_id: str = Field(min_length=1)
    effort_id: str = Field(min_length=1)
    service_tier_id: str = Field(min_length=1)


class _SessionSettingsArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    turn_settings: _TurnSettingsArguments | None = Field(default=None, description="Omit to keep copied/current model settings; null explicitly inherits native Codex; an object must provide all three IDs returned by options.")
    reaction_pulse_enabled: bool = False
    progress_card_enabled: bool = True
    completion_mention_enabled: bool = True
    message_context_mode: Literal["current-only", "catch-up"] = "current-only"


class _ProjectWorkspaceArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["project"]


class _ExistingWorkspaceArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["existing"]
    worktree_id: str = Field(min_length=1, max_length=256)


class _NewWorkspaceArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["new"]
    ref: str = Field(default=None, min_length=1, max_length=256)


class _Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    mode: Literal["options", "list", "view", "create", "update", "delete", "run_now", "runs"]
    plan_id: str | None = Field(default=None, min_length=1)
    name: str | None = Field(default=None, min_length=1)
    instructions: str | None = Field(default=None, min_length=1)
    project: str | None = Field(default=None, min_length=1, description="new_topic create/update Project alias, or options target Project for workspace choices. Omitted create/options uses the source Binding's Project.")
    expected_project_revision: int | None = Field(default=None, ge=1, description="Optional Project revision from workspace options; use on options/create/update to reject stale Project selections. Not accepted for binding targets.")
    chat_id: str | None = Field(default=None, min_length=1)
    target_kind: Literal["new_topic", "binding"] | None = Field(default=None, description="Create/list: new_topic (default) starts an independent topic and Thread; binding submits an input to the original conversation, starting or steering through ordinary message handling. Immutable after create.")
    target_binding_id: str | None = Field(default=None, min_length=1, description="Exact Binding ID from options for binding targets or list filtering. Omit on binding create to use the calling ordinary Thread's Binding. Never supply a native Thread ID. Immutable after create.")
    binding_query: str | None = Field(default=None, max_length=200, description="options only: filter available original conversations by partial Binding ID, Project alias, chat ID or topic ID. Use a narrower query when bindings_truncated is true; empty text clears the filter.")
    schedule: _ScheduleArguments | None = Field(
        default=None,
        description=(
            "Object with kind: once/daily/weekly/interval. once uses at as an ISO "
            "timestamp with offset, to minute precision; daily/weekly use at='HH:mm'. "
            "weekly also requires weekdays (Monday=0 through Sunday=6). interval uses "
            "every_minutes (positive integer); the service generates anchor, omit it. "
            "timezone is an IANA name, provided here or in the outer timezone field; "
            "if both are present they must match. Recurring kinds optionally accept "
            "end_at (inclusive offset ISO cutoff). On update this replaces the "
            "whole rule: include fields/cutoff to retain; omit/null end_at "
            "to clear the cutoff. Omit schedule entirely to preserve it. No cron/RRULE expressions."
        ),
    )
    timezone: str | None = Field(default=None, min_length=1, description="IANA timezone, e.g. Asia/Shanghai.")
    enabled: bool | None = None
    session_settings: _SessionSettingsArguments = Field(default=None, description="Optional partial session settings. Omitted fields copy the source Binding at creation or keep the plan's existing values on update. Explicit turn_settings=null resets model settings to native inheritance.")
    workspace: Annotated[
        _ProjectWorkspaceArguments | _ExistingWorkspaceArguments | _NewWorkspaceArguments,
        Field(discriminator="mode"),
    ] = Field(default=None, description="new_topic only: complete workspace policy. project uses Project cwd; existing needs exact worktree_id; new creates one detached Worktree per accepted run, optionally from ref (otherwise execution-time Project HEAD). Omit on create for project, on update to preserve. No path/cwd or branch creation fields.")
    expected_revision: int | None = Field(default=None, ge=1, description="Copy the exact current revision from the latest list/view/create/update result. Do not increment it; no extra view call is needed if already known.")
    request_id: str | None = Field(default=None, min_length=1, description="Stable UUID for mutation retries.")
    cursor: str | None = None
    limit: int | None = Field(default=None, ge=1, le=50, description="Page size for list/runs: 1 to 50; defaults to 20.")
    all: bool | None = None
    ended: bool | None = None

    @model_validator(mode="after")
    def required_for_mode(self) -> _Arguments:
        required = _REQUIRED_FIELDS.get(self.mode, ())
        if any(getattr(self, key) is None for key in required):
            raise ValueError("Required fields are missing for this mode.")
        if self.mode == "run_now" and self.model_fields_set - _RUN_NOW_FIELDS:
            raise ValueError("run_now only accepts exact plan identity, revision and request_id.")
        if self.target_kind == "binding" and {"workspace", "expected_project_revision"} & self.model_fields_set:
            raise ValueError("binding targets inherit their actual workspace and do not accept overrides.")
        if self.workspace is not None:
            WorkspacePolicy.from_dict(self.workspace.model_dump(exclude_unset=True))
        return self


def _tool_schema() -> dict[str, Any]:
    schema = _Arguments.model_json_schema()
    # Keep the advertised conditional requirements identical to validation.
    schema["allOf"] = [
        {"if": {"properties": {"mode": {"const": mode}}}, "then": {
            "required": list(fields), "properties": {field: {"not": {"type": "null"}} for field in fields},
        }}
        for mode, fields in _REQUIRED_FIELDS.items()
    ]
    schema["allOf"].append({
        "if": {"properties": {"mode": {"const": "run_now"}}},
        "then": {"properties": {
            field: False for field in schema["properties"] if field not in _RUN_NOW_FIELDS
        }},
    })
    schema["allOf"].append({
        "if": {"required": ["target_kind"], "properties": {"target_kind": {"const": "binding"}}},
        "then": {"properties": {"workspace": False, "expected_project_revision": False}},
    })
    return schema


_FIELD_HINTS = {
    "mode": "Set mode to options, list, view, create, update, delete, run_now or runs.",
    "plan_id": "Supply the exact nonempty plan_id returned by list or create.",
    "name": "Supply a nonempty plan name.",
    "instructions": "Supply nonempty reusable task instructions in instructions.",
    "project": "Supply a nonempty Project alias. Omit on create to use the current Binding's Project; omit on update to keep the plan's Project.",
    "expected_project_revision": "Copy the positive Project revision returned with workspace options. Use only for options or new_topic create/update.",
    "workspace": "For new_topic supply exactly {mode:project}, {mode:existing,worktree_id:exact-id}, or {mode:new} with optional nonempty ref. Do not supply workspace for binding or run_now; omit on update to preserve the policy.",
    "chat_id": "Supply a nonempty Feishu conversation ID. Omit on create to use the current conversation (group or private chat); omit on update to keep the plan's target.",
    "target_kind": "Use new_topic for independent topic executions or binding for inputs to the original conversation. The target type is immutable after create.",
    "target_binding_id": "Supply an exact Binding ID returned by options. Omit on binding create to use the current ordinary Binding; Side/unmapped callers must specify it. Never supply a native Thread ID.",
    "binding_query": "For options, supply at most 200 characters matching a Binding ID, Project alias, chat ID or topic ID; empty text clears the filter.",
    "schedule": "Supply a structured schedule with kind and its required fields: once/daily use at; weekly uses at and weekdays; interval uses every_minutes. Remove unsupported schedule fields.",
    "timezone": "Supply an IANA timezone string, such as Asia/Shanghai.",
    "enabled": "Use the boolean true or false.",
    "expected_revision": "Copy the exact current integer revision (at least 1) from the latest list/view/create/update result. Do not increment it or call view again if already known.",
    "request_id": "Supply a nonempty stable UUID request_id for this mutation.",
    "cursor": "Use the cursor string returned by the previous page, or omit it for the first page.",
    "limit": "Use an integer page size from 1 to 50 for list/runs, or omit it to use 20.",
    "all": "Use the boolean true or false; true lists plans across this instance.",
    "ended": "Use the boolean true or false to filter ended plans in list.",
    "session_settings": "Supply a partial object containing only turn_settings, reaction_pulse_enabled, progress_card_enabled, completion_mention_enabled and message_context_mode. Omitted fields keep their defaults/current values.",
    "session_settings.turn_settings": "Use null to inherit native Codex, or supply all three nonempty model_id, effort_id and service_tier_id from options.",
    "session_settings.turn_settings.model_id": "Supply the exact nonempty model_id from options and all three model-setting IDs.",
    "session_settings.turn_settings.effort_id": "Supply a supported nonempty effort_id from options and all three model-setting IDs.",
    "session_settings.turn_settings.service_tier_id": "Supply a supported nonempty service_tier_id from options and all three model-setting IDs.",
    "session_settings.reaction_pulse_enabled": "Use the boolean true or false for reaction pulse.",
    "session_settings.completion_mention_enabled": "Use the boolean true or false to mention the human task initiator at completion. Automatic scheduled first turns have no human initiator and do not mention anyone.",
    "session_settings.progress_card_enabled": "Use the boolean true or false for progress cards.",
    "session_settings.message_context_mode": "Use current-only or catch-up. Private chat targets support only current-only.",
    "schedule.kind": "Set schedule.kind to once, daily, weekly or interval.",
    "schedule.timezone": "Supply an IANA timezone string matching the outer timezone, or omit this field.",
    "schedule.at": "Supply at: once needs an ISO timestamp with explicit offset and minute precision; daily/weekly need HH:mm.",
    "schedule.weekdays": "Supply a nonempty list of integers from 0 (Monday) to 6 (Sunday) for weekly.",
    "schedule.every_minutes": "Supply a positive integer every_minutes for interval.",
    "schedule.anchor": "Omit anchor so the service generates it, or supply a finite UTC epoch number for interval.",
    "schedule.end_at": "For daily/weekly/interval, supply an inclusive ISO cutoff timestamp with explicit offset and minute precision. Remove it for once; omit/null it in a replacement schedule to clear the cutoff.",
    "action": "Remove action; use mode instead.", "op": "Remove op; use mode instead.",
    "instruction": "Remove instruction; use instructions instead.", "prompt": "Remove prompt; use instructions instead.",
    "schedule.type": "Remove schedule.type; use schedule.kind instead.",
    "schedule.run_at": "Remove schedule.run_at; use schedule.at instead.",
    "schedule_type": "Remove schedule_type; use schedule.kind instead.",
    "run_at": "Remove run_at; use schedule.at instead.",
    "arguments": "Remove unsupported fields and use only the fields in the cron_manage schema.",
}


def _invalid_arguments(arguments: Any, error: ValidationError) -> CallToolResult:
    # Never serialize Pydantic's input, message or context: unknown field names
    # and literal/type errors can themselves contain credentials or task text.
    fields: dict[str, str] = {}
    for issue in error.errors(include_input=False, include_context=False, include_url=False):
        location = issue["loc"]
        path = ".".join(str(part) for part in location[:3]) if location[:1] == ("session_settings",) else ".".join(str(part) for part in location[:2]) if location[:1] == ("schedule",) else str(location[0]) if location else "arguments"
        if path not in _FIELD_HINTS:
            path = location[0] if location[:1] in (("schedule",), ("session_settings",)) else "arguments"
        fields[path] = _FIELD_HINTS[path]
    if isinstance(arguments, dict):
        mode = arguments.get("mode")
        for field in _REQUIRED_FIELDS.get(mode, ()) if isinstance(mode, str) else ():
            if arguments.get(field) is None:
                fields[field] = _FIELD_HINTS[field]
        schedule = arguments.get("schedule")
        kind = schedule.get("kind") if isinstance(schedule, dict) else None
        for field in _SCHEDULE_REQUIRED.get(kind, ()) if isinstance(kind, str) else ():
            if schedule.get(field) is None or (field == "weekdays" and not schedule.get(field)):
                fields["schedule." + field] = _FIELD_HINTS["schedule." + field]
        if kind == "once" and schedule.get("end_at") is not None:
            fields["schedule.end_at"] = _FIELD_HINTS["schedule.end_at"]
        if "workspace" in arguments:
            try:
                WorkspacePolicy.from_dict(arguments["workspace"])
            except WorkspacePolicyError:
                fields["workspace"] = _FIELD_HINTS["workspace"]
            if arguments.get("target_kind") == "binding":
                fields["workspace"] = _FIELD_HINTS["workspace"]
    manual = isinstance(arguments, dict) and arguments.get("mode") == "run_now"
    if manual and set(arguments) - _RUN_NOW_FIELDS:
        fields["arguments"] = "run_now only accepts mode, plan_id, expected_revision and request_id. Remove all other fields; it uses the saved plan without overrides."
    return _result({"ok": False, "error": {
        "code": "invalid_request",
        "message": "Correct the fields below and retry cron_manage, keeping the requested plan details and request_id. Do not use shell commands or list_mcp_resources to discover argument names.",
        "fields": [{"field": field, "message": hint} for field, hint in fields.items()],
        **({"run_now_example": RUN_NOW_EXAMPLE} if manual else {"create_example": CREATE_EXAMPLE}),
    }})


def _result(value: dict[str, Any]) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(value, ensure_ascii=False))],
        structured_content=value,
        is_error=value.get("ok") is False or bool(value.get("error")),
    )


def _error(code: str, message: str) -> CallToolResult:
    return _result({"ok": False, "error": {"code": code, "message": message}})


class ScheduleMcpRunner(EmbeddedMcpRunner):
    """The scheduled-plan adapter over the shared process MCP transport."""

    def __init__(self) -> None:
        super().__init__(name="Netizen scheduled tasks", namespace_prefix="netizen_scheduler",
                         env_prefix="NETIZEN_SCHEDULE_MCP_TOKEN", instructions=INSTRUCTIONS,
                         tool_names=("cron_manage",))

    @property
    def _http_timeout(self) -> float:
        return HTTP_TIMEOUT_SECONDS

    async def _list_tools(self, ctx: ServerRequestContext, params: PaginatedRequestParams | None) -> ListToolsResult:
        return ListToolsResult(tools=[Tool(
            name="cron_manage", description=TOOL_DESCRIPTION,
            input_schema=_tool_schema(), annotations=ToolAnnotations(read_only_hint=False),
        )])

    async def _call_tool(self, ctx: ServerRequestContext, params: CallToolRequestParams) -> CallToolResult:
        if params.name != "cron_manage":
            return _error("unknown_tool", "Only cron_manage is available.")
        if not self._admission or self._callback is None:
            return _error("unavailable", "Scheduled task management is not ready. Retry later.")
        try:
            request = _Arguments.model_validate(params.arguments or {}).model_dump(exclude_unset=True)
            request = {key: value for key, value in request.items() if value is not None}
            if "schedule" in request:
                request["schedule"] = {key: value for key, value in request["schedule"].items() if value is not None}
        except ValidationError as error:
            return _invalid_arguments(params.arguments, error)
        try:
            native_thread_id = _native_thread_id(ctx.meta)
        except (ValueError, TypeError):
            return _error("invalid_call_context", "Native Thread metadata is malformed or conflicting.")
        task = asyncio.current_task()
        assert task is not None
        self._calls.add(task)
        try:
            async with asyncio.timeout(CALL_TIMEOUT_SECONDS):
                value = await self._callback(request, native_thread_id)
            return _result(value)
        except TimeoutError:
            return _error("timeout", "Management timed out. Retry the same mutation with the same request_id.")
        except Exception:
            # Exception text and tracebacks may contain task instructions or credentials.
            logger.error("Scheduled task management callback failed")
            return _error("internal_error", "Scheduled task management failed. Check the current plan before retrying.")
        finally:
            self._calls.discard(task)
