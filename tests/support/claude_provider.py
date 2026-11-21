"""Deterministic Messages fixture. The only executable tool inputs are fixed here."""
import asyncio
import json
import re
import uuid

from aiohttp import web

PROJECT_SENTINEL = "NETIZEN-CHANNEL-PROJECT-VERIFIED"

MARK = re.compile(r"\[\[S:([a-z0-9-]+)(?:/([A-Za-z0-9_-]+))?\]\]")


def blocks(message):
    content = message.get("content", [])
    return [{"type": "text", "text": content}] if isinstance(content, str) else content


class Provider:
    def __init__(self, cwd):
        self.cwd = cwd
        self.rows = []
        self.arrived = asyncio.Condition()

    async def start(self):
        app = web.Application(client_max_size=8 * 1024**2)
        app.router.add_route("*", "/{anything:.*}", self.serve)
        self.runner = web.AppRunner(app, access_log=None, handler_cancellation=True, shutdown_timeout=2)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        return self.runner.addresses[0][1]

    async def wait_request(self, label):
        async with self.arrived:
            await asyncio.wait_for(self.arrived.wait_for(lambda: any(row["label"] == label for row in self.rows)), 20)

    async def close(self):
        await self.runner.cleanup()

    def release(self, label):
        (self.cwd / ("release-" + label)).touch()

    async def gate(self, label, phase):
        (self.cwd / (phase + "-" + label)).touch()
        async with asyncio.timeout(120):
            while not (self.cwd / ("release-" + label)).exists():
                await asyncio.sleep(.025)

    async def serve(self, request):
        data = await request.json() if request.can_read_body else {}
        messages = data.get("messages", [])
        tool_error = False
        history, tool_result, scenario, label = [], False, "side", "side"
        for message in messages:
            parts = blocks(message)
            value = "\n".join(p.get("text", "") for p in parts if p["type"] == "text")
            hits = MARK.findall(value)
            history.append({"role": message["role"], "markers": hits,
                            "answers": re.findall(r"fixture-reply-[A-Za-z0-9_-]+", value),
                            "blocks": [p["type"] for p in parts]})
            if message["role"] == "user":
                if hits:
                    scenario, label = hits[-1]
                    tool_result = False
                elif value and not any(p["type"] == "tool_result" for p in parts):
                    scenario, label, tool_result = "side", "side", False
                if any(p["type"] == "tool_result" for p in parts):
                    tool_result = True
                    tool_error = any(p.get("is_error") is True for p in parts if p["type"] == "tool_result")
        # Keep fixture identifiers, never message bodies, arguments, headers or native transcripts.
        row = {"path": request.path, "stream": data.get("stream", False), "scenario": scenario, "label": label,
               "project_sentinel": PROJECT_SENTINEL in json.dumps([data.get("system"), messages]),
               "history": history, "tool_result": tool_result, "tool_error": tool_error, "model": data.get("model")}
        async with self.arrived:
            self.rows.append(row)
            self.arrived.notify_all()
        if request.path == "/v1/models":
            return web.json_response({"data": [{"type": "model", "id": "claude-sonnet-4-6",
                                                "display_name": "Mock", "created_at": "2026-01-01T00:00:00Z"}],
                                      "has_more": False})
        if request.path.endswith("/count_tokens"):
            return web.json_response({"input_tokens": 12})
        if request.path != "/v1/messages":
            return web.Response(status=404)
        if scenario.removesuffix("-hint") in ("http-500", "http-529", "http-401"):
            code = int(scenario.split("-")[1])
            kind = {500: "api_error", 529: "overloaded_error", 401: "authentication_error"}[code]
            return web.json_response({"type": "error", "error": {"type": kind, "message": "fixture-" + kind}},
                                     status=code, headers={"retry-after": "0"} if scenario.endswith("-hint") else {})
        if scenario == "slow-first-byte":
            await self.gate(label, "waiting")
        tool = None
        if not tool_result:
            if scenario == "tool-barrier":
                tool = ("Bash", {"command": "./wait-for-fixture", "timeout": 30000, "description": "Wait for fixture gate"})
            elif scenario in ("foreground", "background"):
                command = "./fixture-" + scenario
                tool = ("Bash", {"command": command, "run_in_background": scenario == "background",
                                 "description": "Run disposable fixture task"})
            elif scenario == "denied-touch":
                tool = ("Bash", {"command": "touch permission-probe", "description": "Check the project deny rule"})
            elif scenario == "tool-error":
                tool = ("Bash", {"command": "exit 17", "description": "Fixture tool error"})
            elif scenario == "edit-file":
                tool = ("Write", {"file_path": str(self.cwd / ("file-" + label)), "content": "fixture data\n"})
            elif scenario == "mcp":
                tool = ("mcp__fixture__identify", {})
        content = ({"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex, "name": tool[0], "input": tool[1]}
                   if tool else {"type": "text", "text": "fixture-reply-" + label})
        if scenario == "inline-image":
            content["text"] += " ![pixel](result.png)"
        stop = "tool_use" if tool else "end_turn"
        msg = {"id": "msg_" + uuid.uuid4().hex, "type": "message", "role": "assistant",
               "model": data["model"], "content": [], "stop_reason": None, "stop_sequence": None,
               "usage": {"input_tokens": 12, "output_tokens": 0}}
        if not data.get("stream"):
            if scenario == "disconnect-hard":
                request.transport.abort()
                return web.Response()
            return web.json_response({**msg, "content": [content], "stop_reason": stop,
                                      "usage": {"input_tokens": 12, "output_tokens": 8}})
        stream = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        await stream.prepare(request)

        async def emit(event, **payload):
            body = json.dumps({"type": event, **payload})
            await stream.write(f"event: {event}\ndata: {body}\n\n".encode())

        await emit("message_start", message=msg)
        initial = {**content, "input": {}} if tool else {"type": "text", "text": ""}
        await emit("content_block_start", index=0, content_block=initial)
        delta = ({"type": "input_json_delta", "partial_json": json.dumps(tool[1])}
                 if tool else {"type": "text_delta", "text": content["text"]})
        await emit("content_block_delta", index=0, delta=delta)
        if scenario in ("disconnect-mid-stream","disconnect-hard"):
            request.transport.abort()
            return stream
        if scenario == "hold-stream":
            await self.gate(label, "streaming")
        await emit("content_block_stop", index=0)
        await emit("message_delta", delta={"stop_reason": stop, "stop_sequence": None}, usage={"output_tokens": 8})
        await emit("message_stop")
        await stream.write_eof()
        return stream
