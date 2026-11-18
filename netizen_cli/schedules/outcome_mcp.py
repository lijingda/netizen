"""Thread-scoped scheduled topic self-assessment on the shared MCP transport."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from mcp.server import ServerRequestContext
from mcp.types import CallToolRequestParams, CallToolResult, ListToolsResult, PaginatedRequestParams, TextContent, Tool

from ..mcp_transport import EmbeddedMcpRunner, native_subagent_kind, native_thread_id, native_turn_id

logger = logging.getLogger(__name__)
TOOL = "report_run_outcome"
INSTRUCTIONS = (
    "Assess only the scheduled task that created this Feishu topic. Its first input contains <scheduled_plan>. "
    "Use the plan's completion criteria. For example, finding and reporting an anomaly can satisfy a plan "
    "to check for and report anomalies. Report your assessment, and call again if it changes during this "
    "Turn or a follow-up. Skip unrelated requests. Subagents must not call this tool. "
    "Reporting does not end the Turn or change result delivery."
)


def _result(value: dict[str, Any]) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(value, ensure_ascii=False))],
                          structured_content=value, is_error="error" in value)


def _error(code: str, message: str) -> CallToolResult:
    return _result({"error": {"code": code, "message": message + " Do not retry in this session."}})


class OutcomeMcpRunner(EmbeddedMcpRunner):
    def __init__(self) -> None:
        super().__init__(name="Netizen scheduled task outcome", namespace_prefix="netizen_run_outcome",
                         env_prefix="NETIZEN_RUN_OUTCOME_TOKEN", instructions=INSTRUCTIONS, tool_names=(TOOL,))

    def thread_config(self) -> dict[str, object]:
        return {f"mcp_servers.{self.namespace}": {
            "url": self.url, "bearer_token_env_var": self._env_key, "enabled": True,
            "enabled_tools": [TOOL], "default_tools_approval_mode": "approve",
        }}

    async def _list_tools(self, ctx: ServerRequestContext, params: PaginatedRequestParams | None) -> ListToolsResult:
        return ListToolsResult(tools=[Tool(name=TOOL, description=INSTRUCTIONS, input_schema={
            "type": "object", "properties": {"outcome": {"type": "string", "enum": ["succeeded", "failed"]}},
            "required": ["outcome"], "additionalProperties": False,
        })])

    async def _call_tool(self, ctx: ServerRequestContext, params: CallToolRequestParams) -> CallToolResult:
        if not self._admission or self._callback is None:
            return _error("unavailable", "Scheduled task assessment is unavailable.")
        args = params.arguments
        if (params.name != TOOL or not isinstance(args, dict) or set(args) != {"outcome"}
                or args["outcome"] not in ("succeeded", "failed")):
            return _error("invalid_call_context", "Supply only the outcome, succeeded or failed.")
        try:
            if not isinstance(ctx.meta, dict):
                raise ValueError("Missing native call metadata")
            meta = dict(ctx.meta)
            thread_id = native_thread_id(meta, require_turn_thread=True)
            turn_id = native_turn_id(meta)
            if not thread_id or not turn_id or native_subagent_kind(meta) is not None:
                return _error("invalid_call_context", "Only the parent agent's current physical Turn may report.")
        except (ValueError, TypeError):
            return _error("invalid_call_context", "The native caller metadata is malformed or conflicting.")
        task = asyncio.current_task()
        assert task is not None
        self._calls.add(task)
        try:
            async with asyncio.timeout(15):
                result = await self._callback(source_thread_id=thread_id, source_turn_id=turn_id, outcome=args["outcome"])
            return _result(result)
        except Exception:
            logger.warning("report_run_outcome outcome_unknown", extra={"thread_id": thread_id, "turn_id": turn_id})
            return _error("outcome_unknown", "The assessment write could not be confirmed.")
        finally:
            self._calls.discard(task)
