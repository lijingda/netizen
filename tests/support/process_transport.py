"""Decode real process-adapter calls with a separate synthetic message ID stream."""

import json
from types import SimpleNamespace

from netizen_cli.process_messages import ProcessMessages


class ProcessTransport:
    def __init__(self, events):
        self.events = events
        self.created = {}
        self.completed = set()
        self.port = ProcessMessages(self)

    async def arequest(self, request):
        data = {}
        if request.http_method.name == "PUT":
            mid = request.body["message_id"]
            assert self.created[mid][0] == request.body["cot_id"] and mid not in self.completed
            self.events.append(["process.update", [mid, request.body["events"]], {}])
        elif request.uri.endswith("/complete/:cot_id"):
            values = dict(request.queries)
            mid = values["message_id"]
            assert self.created[mid][0] == request.paths["cot_id"] and mid not in self.completed
            self.completed.add(mid)
            self.events.append(["process.complete", [mid, values["reason"]], {}])
        else:
            assert request.uri == "/open-apis/im/v1/message_cot"
            n = len(self.created) + 1
            mid, cot = f"synthetic-process-{n}", f"synthetic-cot-{n}"
            self.created[mid] = (cot, request.body)
            data = {"message_id": mid, "cot_id": cot}
            self.events.append(["process.create", [mid, request.body], {}])
        return SimpleNamespace(raw=SimpleNamespace(status_code=200, content=json.dumps({"code": 0, "data": data}).encode()))

    def content_trace(self):
        reads = set()
        content = []
        for event in self.events:
            name, args = event[:2]
            if name in ("process.create", "process.update", "process.complete"):
                assert args[0] in self.created
            elif name == "fetch_message" and len(args) == 1 and args[0] in self.created:
                mid = args[0]
                assert self.created[mid][1]["reply_in_thread"] and mid not in reads
                reads.add(mid)
            else:
                content.append(event)
        assert reads == {mid for mid, (_, body) in self.created.items() if body["reply_in_thread"]}
        return content
