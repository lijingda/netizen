"""The single scheduled-plan MCP tool, on the owning application's loop.

MCP framing, initialization and Streamable HTTP are owned by the official SDK.
This adapter supplies only the management schema, call identity and a bounded
loopback listener. It never reads or writes Codex configuration files.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import socket
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

import uvicorn
from mcp.server import Server, ServerRequestContext
from mcp.server.transport_security import TransportSecuritySettings
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


logger = logging.getLogger(__name__)
ManagementCallback = Callable[[dict[str, Any], str | None], Awaitable[dict[str, Any]]]
MAX_BODY_BYTES = 65536
CALL_TIMEOUT_SECONDS = 15.0
HTTP_TIMEOUT_SECONDS = 20.0
_REQUIRED_FIELDS = {
    "view": ("plan_id",),
    "create": ("name", "instructions", "schedule", "request_id"),
    "update": ("plan_id", "expected_revision", "request_id"),
    "delete": ("plan_id", "expected_revision", "request_id"),
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

INSTRUCTIONS = """Manage Netizen scheduled tasks with cron_manage: create, list, view, update, pause, enable, or delete plans for recurring work, reminders, monitoring, and follow-ups.

When the user asks to manage scheduled tasks, use cron_manage and follow its schema. If the tool is deferred, find it through the available tool search first.

Each scheduled occurrence starts an independent ordinary persistent Codex Thread in a new Feishu topic. Pausing or deleting a plan prevents new claims; an already claimed occurrence may continue. Stop or continue an existing occurrence through ordinary conversation controls. Write clear, reusable task instructions in the saved plan.
"""

TOOL_DESCRIPTION = """Manage Netizen scheduled tasks with cron_manage: options, list, view, create, update (including pause/enable), delete, and recent runs. Follow the schema and report success only from the returned result.
Find candidates by name before using an exact plan_id; clarify ambiguous matches. Resolve relative times into a structured schedule and IANA timezone, and save reusable instructions with explicit resource references. New occurrences use independent ordinary Threads in new Feishu topics. Pausing/deleting prevents future claims; already claimed work may continue and uses ordinary stop/continue controls.
For create/update/delete supply a UUID request_id; retry the same request with the same ID after a timeout. For update/delete copy the exact current revision from the latest list/view/create/update result into expected_revision; never increment it yourself. An existing current result does not require another view call. Omitted chat_id/project on create use the calling native Thread's Binding: the current Feishu conversation may be a group or a private chat; a topic uses its containing conversation. list defaults to that current conversation; all=true lists this instance's plans. update keeps omitted fields unchanged. No native Thread identity belongs in tool arguments.
Create copies the calling Binding's session settings once; later Binding changes do not change the plan. session_settings is a partial override: turn_settings=null explicitly inherits native Codex settings; a model override supplies all three IDs. Other fields control reaction pulse, progress card and message context. Use options to discover current model/effort/service-tier IDs and defaults when choosing settings; ordinary creation needs no preliminary options call. A private target supports only current-only; an inherited catch-up default is normalized, but explicitly requesting catch-up is rejected. Updating unrelated fields retains the plan's settings even if its model is no longer available.
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
    message_context_mode: Literal["current-only", "catch-up"] = "current-only"


class _Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    mode: Literal["options", "list", "view", "create", "update", "delete", "runs"]
    plan_id: str | None = Field(default=None, min_length=1)
    name: str | None = Field(default=None, min_length=1)
    instructions: str | None = Field(default=None, min_length=1)
    project: str | None = Field(default=None, min_length=1)
    chat_id: str | None = Field(default=None, min_length=1)
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
    expected_revision: int | None = Field(default=None, ge=1, description="Copy the exact current revision from the latest list/view/create/update result. Do not increment it; no extra view call is needed if already known.")
    request_id: str | None = Field(default=None, min_length=1, description="Stable UUID for mutation retries.")
    cursor: str | None = None
    limit: int | None = Field(default=None, ge=1, le=50, description="Page size for list/runs: 1 to 50; defaults to 20.")
    all: bool | None = None

    @model_validator(mode="after")
    def required_for_mode(self) -> _Arguments:
        required = _REQUIRED_FIELDS.get(self.mode, ())
        if any(getattr(self, key) is None for key in required):
            raise ValueError("Required fields are missing for this mode.")
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
    return schema


_FIELD_HINTS = {
    "mode": "Set mode to options, list, view, create, update, delete or runs.",
    "plan_id": "Supply the exact nonempty plan_id returned by list or create.",
    "name": "Supply a nonempty plan name.",
    "instructions": "Supply nonempty reusable task instructions in instructions.",
    "project": "Supply a nonempty Project alias. Omit on create to use the current Binding's Project; omit on update to keep the plan's Project.",
    "chat_id": "Supply a nonempty Feishu conversation ID. Omit on create to use the current conversation (group or private chat); omit on update to keep the plan's target.",
    "schedule": "Supply a structured schedule with kind and its required fields: once/daily use at; weekly uses at and weekdays; interval uses every_minutes. Remove unsupported schedule fields.",
    "timezone": "Supply an IANA timezone string, such as Asia/Shanghai.",
    "enabled": "Use the boolean true or false.",
    "expected_revision": "Copy the exact current integer revision (at least 1) from the latest list/view/create/update result. Do not increment it or call view again if already known.",
    "request_id": "Supply a nonempty stable UUID request_id for this mutation.",
    "cursor": "Use the cursor string returned by the previous page, or omit it for the first page.",
    "limit": "Use an integer page size from 1 to 50 for list/runs, or omit it to use 20.",
    "all": "Use the boolean true or false; true lists plans across this instance.",
    "session_settings": "Supply a partial object containing only turn_settings, reaction_pulse_enabled, progress_card_enabled and message_context_mode. Omitted fields keep their defaults/current values.",
    "session_settings.turn_settings": "Use null to inherit native Codex, or supply all three nonempty model_id, effort_id and service_tier_id from options.",
    "session_settings.turn_settings.model_id": "Supply the exact nonempty model_id from options and all three model-setting IDs.",
    "session_settings.turn_settings.effort_id": "Supply a supported nonempty effort_id from options and all three model-setting IDs.",
    "session_settings.turn_settings.service_tier_id": "Supply a supported nonempty service_tier_id from options and all three model-setting IDs.",
    "session_settings.reaction_pulse_enabled": "Use the boolean true or false for reaction pulse.",
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
    return _result({"ok": False, "error": {
        "code": "invalid_request",
        "message": "Correct the fields below and retry cron_manage, keeping the requested plan details and request_id. Do not use shell commands or list_mcp_resources to discover argument names.",
        "fields": [{"field": field, "message": hint} for field, hint in fields.items()],
        "create_example": CREATE_EXAMPLE,
    }})


def _result(value: dict[str, Any]) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(value, ensure_ascii=False))],
        structured_content=value,
        is_error=value.get("ok") is False or bool(value.get("error")),
    )


def _error(code: str, message: str) -> CallToolResult:
    return _result({"ok": False, "error": {"code": code, "message": message}})


def _native_thread_id(meta: dict[str, Any] | None) -> str | None:
    """Read only native per-call metadata, never arguments or connection state."""
    if meta is None:
        return None
    thread_id = meta.get("threadId")
    turn_metadata = meta.get("x-codex-turn-metadata")
    if isinstance(turn_metadata, str):
        turn_metadata = json.loads(turn_metadata)
    if turn_metadata is not None and not isinstance(turn_metadata, dict):
        raise ValueError("Invalid native turn metadata")
    other_id = (turn_metadata or {}).get("thread_id")
    for value in (thread_id, other_id):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError("Invalid native Thread ID")
    if thread_id is not None and other_id is not None and thread_id != other_id:
        raise ValueError("Conflicting native Thread IDs")
    # The second metadata form is a cross-check, not an alternative source.
    return thread_id


class _EmbeddedServer(uvicorn.Server):
    def capture_signals(self):
        # The Netizen process already owns signal handling and shutdown.
        return contextlib.nullcontext()


class ScheduleMcpRunner:
    """Bind before AsyncCodex creation; open management only after app readiness.

    ``drain`` accepts an absolute event-loop deadline. ``close`` is idempotent
    and also closes the listener with bounded ASGI lifespan shutdown.
    """

    def __init__(self) -> None:
        nonce = secrets.token_hex(12)
        self.namespace = f"netizen_scheduler_{nonce}"
        self._env_key = f"NETIZEN_SCHEDULE_MCP_TOKEN_{nonce.upper()}"
        self._token = secrets.token_urlsafe(32)
        self._callback: ManagementCallback | None = None
        self._admission = False
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._http: _EmbeddedServer | None = None
        self._server_task: asyncio.Task[None] | None = None
        self._calls: set[asyncio.Task[Any]] = set()
        self._url: str | None = None
        self._mcp = Server(
            "Netizen scheduled tasks",
            instructions=INSTRUCTIONS,
            on_list_tools=self._list_tools,
            on_call_tool=self._call_tool,
        )

    @property
    def url(self) -> str:
        if self._url is None:
            raise RuntimeError("Schedule MCP listener has not been bound")
        return self._url

    @property
    def config_overrides(self) -> tuple[str, ...]:
        # A single owned table entry cannot replace the user's mcp_servers table.
        entry = (
            f'{{url={json.dumps(self.url)}, '
            f'bearer_token_env_var={json.dumps(self._env_key)}, '
            'enabled=true, enabled_tools=["cron_manage"]}'
        )
        return (f"mcp_servers.{self.namespace}={entry}",)

    @property
    def app_server_env(self) -> dict[str, str]:
        return {self._env_key: self._token}

    def attach(self, callback: ManagementCallback) -> None:
        if self._closed or self._callback is not None:
            raise RuntimeError("Schedule MCP callback is already attached or closed")
        if not callable(callback):
            raise TypeError("Schedule management callback must be callable")
        self._callback = callback

    def open_admission(self) -> None:
        self._assert_loop()
        if self._closed or self._callback is None or self._server_task is None or self._server_task.done():
            raise RuntimeError("Schedule MCP is not ready")
        self._admission = True

    def close_admission(self) -> None:
        self._admission = False

    async def bind(self) -> None:
        if self._closed or self._server_task is not None:
            raise RuntimeError("Schedule MCP listener cannot be bound twice")
        self._loop = asyncio.get_running_loop()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setblocking(False)
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            self._url = f"http://127.0.0.1:{port}/mcp"
            app = self._mcp.streamable_http_app(
                json_response=True,
                stateless_http=True,
                max_request_body_size=MAX_BODY_BYTES,
                transport_security=TransportSecuritySettings(
                    allowed_hosts=[f"127.0.0.1:{port}"],
                    allowed_origins=[],
                ),
            )
            self._http = _EmbeddedServer(uvicorn.Config(
                self._guard(app), host="127.0.0.1", port=port,
                loop="asyncio", http="h11", ws="none", lifespan="on",
                log_config=None, access_log=False, proxy_headers=False,
                server_header=False, limit_concurrency=32,
                h11_max_incomplete_event_size=32768,
                timeout_keep_alive=5, timeout_graceful_shutdown=5,
            ))
            self._server_task = asyncio.create_task(self._serve(listener), name="schedule-mcp")
            async with asyncio.timeout(10):
                while not self._http.started:
                    if self._server_task.done():
                        await self._server_task
                        raise RuntimeError("Schedule MCP listener did not start")
                    await asyncio.sleep(0.01)
        except BaseException:
            listener.close()
            await self.close()
            raise

    async def _serve(self, listener: socket.socket) -> None:
        assert self._http is not None
        try:
            await self._http.serve(sockets=[listener])
        except SystemExit as exc:
            raise RuntimeError("Schedule MCP listener failed to start") from exc
        finally:
            listener.close()

    async def drain(self, deadline: float) -> bool:
        self._assert_loop()
        if not self._calls:
            return True
        _, pending = await asyncio.wait(self._calls, timeout=max(0, deadline - self._loop.time()))
        return not pending

    async def close(self) -> None:
        self.close_admission()
        self._closed = True
        if self._http is not None:
            self._http.should_exit = True
            for listener in getattr(self._http, "servers", ()):
                listener.close()
        task = self._server_task
        if task is not None and not task.done():
            try:
                async with asyncio.timeout(7):
                    await asyncio.shield(task)
            except (TimeoutError, asyncio.CancelledError) as exc:
                # An outer shutdown budget may expire before Uvicorn's own
                # graceful deadline. Shielding the server must not leave
                # management callbacks accessing a subsequently closed Store.
                pending = {task, *self._calls}
                for active in pending:
                    active.cancel()
                await asyncio.wait(pending, timeout=0.1)
                if isinstance(exc, asyncio.CancelledError):
                    raise

    def _assert_loop(self) -> None:
        if self._loop is None or asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("Schedule MCP must run on its owning event loop")

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

    def _guard(self, app):
        async def guarded(scope, receive, send):
            if scope["type"] != "http":
                await app(scope, receive, send)
                return
            headers = scope.get("headers", [])
            authorization = [value for key, value in headers if key.lower() == b"authorization"]
            expected = f"Bearer {self._token}".encode("ascii")
            if len(authorization) != 1 or not secrets.compare_digest(authorization[0], expected):
                await self._http_error(send, 401, "unauthorized")
                return
            # No browser client is part of this private process endpoint.
            if any(key.lower() == b"origin" for key, _ in headers):
                await self._http_error(send, 403, "origin_not_allowed")
                return
            response_started = False

            async def tracked_send(message):
                nonlocal response_started
                if message["type"] == "http.response.start":
                    response_started = True
                await send(message)

            try:
                async with asyncio.timeout(HTTP_TIMEOUT_SECONDS):
                    await app(scope, receive, tracked_send)
            except TimeoutError:
                if not response_started:
                    await self._http_error(send, 408, "request_timeout")

        return guarded

    @staticmethod
    async def _http_error(send, status: int, code: str) -> None:
        body = json.dumps({"error": code}).encode("ascii")
        await send({"type": "http.response.start", "status": status, "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]})
        await send({"type": "http.response.body", "body": body})
