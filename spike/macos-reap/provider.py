"""Track A's loopback Messages fixture, limited to the single fixed Bash barrier."""
import json
import time
import uuid

from aiohttp import web


class Provider:
    def __init__(self, log, *, command=None):
        self.command = command
        self.log = log
        self.rows = []

    async def start(self):
        app = web.Application(client_max_size=8 * 1024**2)
        app.router.add_route("*", "/{anything:.*}", self.serve)
        self.runner = web.AppRunner(app, access_log=None, handler_cancellation=True, shutdown_timeout=2)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        return self.runner.addresses[0][1]

    async def serve(self, request):
        received = time.monotonic()
        data = await request.json() if request.can_read_body else {}
        users = [m["content"] for m in data.get("messages", []) if m["role"] == "user"]
        last = users[-1] if users else []
        blocks = [{"type": "text", "text": last}] if isinstance(last, str) else last
        marker = any("[[S:tool-barrier/reap]]" in b.get("text", "") for b in blocks)
        tool_result = any(b["type"] == "tool_result" for b in blocks)
        row = dict(t=received, path=request.path, method=request.method, marker=marker, tool_result=tool_result)
        self.rows.append(row)
        with self.log.open("a") as output:
            print(json.dumps(row), file=output)
        if request.path == "/v1/models":
            return web.json_response({"data": [{"type": "model", "id": "claude-sonnet-4-6",
                                      "display_name": "Mock", "created_at": "2026-01-01T00:00:00Z"}],
                                      "has_more": False})
        if request.path.endswith("/count_tokens"):
            return web.json_response({"input_tokens": 12})
        if request.path != "/v1/messages":
            return web.Response(status=404)

        tool = marker and not tool_result
        command = self.command or ("SECONDS=0; echo $$ >> starts-reap; for ((i=0;i<1200;i++)); do "
                   "if test -e release-reap; then echo released; exit 0; fi; "
                   "if (( SECONDS >= 119 )); then exit 33; fi; sleep .1; done; exit 33")
        content = ({"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex, "name": "Bash",
                    "input": {"command": command, "timeout": 120000, "description": "Wait for fixture gate"}}
                   if tool else {"type": "text", "text": "fixture-reply-reap"})
        stop = "tool_use" if tool else "end_turn"
        msg = {"id": "msg_" + uuid.uuid4().hex, "type": "message", "role": "assistant",
               "model": data["model"], "content": [], "stop_reason": None, "stop_sequence": None,
               "usage": {"input_tokens": 12, "output_tokens": 0}}
        if not data.get("stream"):
            return web.json_response({**msg, "content": [content], "stop_reason": stop,
                                      "usage": {"input_tokens": 12, "output_tokens": 8}})
        stream = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        await stream.prepare(request)

        async def emit(event, **payload):
            body = json.dumps({"type": event, **payload})
            await stream.write(f"event: {event}\ndata: {body}\n\n".encode())

        await emit("message_start", message=msg)
        await emit("content_block_start", index=0, content_block={**content, "input": {}} if tool
                   else {"type": "text", "text": ""})
        delta = ({"type": "input_json_delta", "partial_json": json.dumps(content["input"])}
                 if tool else {"type": "text_delta", "text": content["text"]})
        await emit("content_block_delta", index=0, delta=delta)
        await emit("content_block_stop", index=0)
        await emit("message_delta", delta={"stop_reason": stop, "stop_sequence": None}, usage={"output_tokens": 8})
        await emit("message_stop")
        await stream.write_eof()
        return stream
