"""Record calls at the public Feishu transport boundary for presenter differentials."""

import base64
import itertools
import json
from contextlib import ExitStack
from dataclasses import asdict, is_dataclass
from pathlib import Path
from unittest.mock import patch

from tests.support.process_transport import ProcessTransport


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "codex_presenter_a1b9eee.json"


def delivery_value(value):
    if hasattr(value, "id") and hasattr(value, "conversation"):
        return {"messageId": value.id, "chatId": getattr(value, "chat_id", None) or getattr(value.conversation, "chat_id", None),
                "threadId": getattr(value.conversation, "thread_id", None)}
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def delivery_recorder(channel, stack, *, include_reads=False):
    events = []

    def capture(name, method):
        async def call(*args, **kwargs):
            events.append([name, [delivery_value(v) for v in args],
                           {k: delivery_value(v) for k, v in kwargs.items()}])
            return await method(*args, **kwargs)
        return call

    methods = ("reply", "send", "update_card", "upload_media", "add_reaction", "remove_reaction")
    if include_reads:
        methods += ("fetch_message", "get_chat_info", "download_resource")
        if hasattr(channel, "get_chat_members"):
            methods += ("get_chat_members",)
    for name in methods:
        stack.enter_context(patch.object(channel, name, capture(name, getattr(channel, name))))
    return events


def _encode_media(value):
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode("ascii")}
    raise TypeError(f"Unexpected transport value {type(value).__name__}")


def expect_presenter_trace(case, fixture, name, *, reference=FIXTURE, expected_edits=None, stack=None, process_mode=False):
    """Compare the complete ordered trace after the scenario finishes."""
    if stack is None:
        stack = case.enterContext(ExitStack())
    nonces = itertools.count(1)
    stack.enter_context(patch("secrets.token_hex", side_effect=lambda n: next(nonces).to_bytes(n, "big").hex()))
    events = delivery_recorder(fixture.channel, stack, include_reads=True)

    process = ProcessTransport(events)
    stack.enter_context(patch.object(fixture.app._progress_cards, "_process_port", process.port))

    def compare():
        serialized = json.dumps(process.content_trace() if process_mode else events, default=_encode_media, ensure_ascii=False)
        root = fixture.project_root
        actual = json.loads(serialized.replace(str(root.resolve()), "<root>").replace(str(root), "<root>"))
        expected = json.loads(reference.read_text(encoding="utf-8"))[name]
        if expected_edits:
            expected_edits(expected)
        case.assertEqual(expected, actual, name)

    stack.callback(compare)
    return events
