"""Decode real process-adapter calls with a separate synthetic message ID stream."""

import json
from types import SimpleNamespace

from netizen_cli.process_messages import ProcessMessages


def check_pairs(events):
    """Independent wire oracle. Every event series in one replacement starts locally."""
    started, ended, texts = {}, set(), set()
    previous = 0
    for frame in events:
        data = json.loads(frame["content"])
        kind = frame["event_type"]
        assert kind == data.pop("type")
        timestamp = int(frame["timestamp"])
        assert previous <= timestamp
        previous = timestamp
        assert len(frame["content"]) <= 1800
        if kind.startswith("TOOL_CALL_"):
            key = ("tool", data["toolCallId"])
        elif kind.startswith("STEP_"):
            key = ("step", data["stepName"])
        else:
            key = ("text", data["messageId"])
        if kind in {"TOOL_CALL_START", "STEP_STARTED", "TEXT_MESSAGE_START"}:
            assert key not in started
            started[key] = timestamp
        elif kind == "TEXT_MESSAGE_CONTENT":
            assert key in started and key not in texts and key not in ended
            texts.add(key)
        else:
            assert kind in {"STEP_FINISHED", "TOOL_CALL_END", "TEXT_MESSAGE_END"}
            assert key in started and key not in ended
            assert key[0] != "text" or key in texts
            ended.add(key)
    assert len(started) <= 60
    return started, ended


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

    def assert_retained_completion(self, case, reason):
        names = [name for name, _, _ in self.events]
        case.assertEqual(names, ["process.create", "process.update", "process.update", "process.complete"])
        running, ended = check_pairs(self.events[1][1][1])
        case.assertEqual(len(running), 1)
        case.assertFalse(ended)
        final, ended = check_pairs(self.events[2][1][1])
        case.assertEqual(final, running)
        case.assertEqual(ended, set(running))
        case.assertEqual(self.events[-1][1][1], reason)
