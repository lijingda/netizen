"""The three Feishu message_cot writes approved by ADR 0107."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass, field, replace
from enum import IntEnum
from hashlib import sha256
from typing import Literal, Self, TypedDict

import lark_oapi as lark

from .turn_activity import normalize_activity_text_layout


_RESOURCE = "/open-apis/im/v1/message_cot"
# Create has no deduplication key. Give slow acknowledgements the full writer
# budget so the returned IDs can still be used to update and complete the message.
PROCESS_OPERATION_TIMEOUT_SECONDS = 30.0
CONTENT_LIMIT = 1_800
ITEM_TEXT_LIMIT = 300
ITEM_LIMIT = 60
_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Item:
    """A presentation-scoped identity and immutable text from the safe Activity projection."""

    item_id: str
    text: str
    started_at: int  # Ten-digit second of the first observation.
    finished_at: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.item_id, str) or not self.item_id.strip():
            raise ValueError("an Activity item requires a stable identity")
        if not isinstance(self.text, str):
            raise ValueError("Activity item text must be projected text")
        object.__setattr__(self, "text", normalize_activity_text_layout(self.text))
        _check_timestamp(self.started_at)
        if self.finished_at is not None:
            _check_timestamp(self.finished_at)
            if self.finished_at < self.started_at:
                raise ValueError("END precedes START")

    @property
    def wire_id(self) -> str:
        return sha256(self.item_id.encode("utf-8", errors="surrogatepass")).hexdigest()[:32]

    @property
    def state(self) -> Literal["running", "finished"]:
        return "running" if self.finished_at is None else "finished"

    def finish(self, observed_at: int) -> Self:
        """Freeze the first completion observation, also when the wall clock has moved back."""
        if self.finished_at is not None:
            return self
        _check_timestamp(observed_at)
        return replace(self, finished_at=max(self.started_at, observed_at))


class ToolItem(_Item):
    """One safe operation label."""


class StepItem(_Item):
    """A checklist step first seen in progress or completed."""


@dataclass(frozen=True)
class TextItem(_Item):
    """Completed commentary. CONTENT defaults to the completion second."""

    finished_at: int = field()
    content_at: int | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        _check_timestamp(self.finished_at)
        if self.content_at is not None:
            _check_timestamp(self.content_at)
            if not self.started_at <= self.content_at <= self.finished_at:
                raise ValueError("CONTENT must be between START and END")


ProcessItem = ToolItem | StepItem | TextItem


class _Event(TypedDict):
    event_type: str
    content: str
    timestamp: str


@dataclass(frozen=True)
class Created:
    cot_id: str
    message_id: str


@dataclass(frozen=True)
class Ok:
    pass


class RejectionCode(IntEnum):
    TERMINAL_OR_INVALID = 10001
    CONCURRENT = 10020


@dataclass(frozen=True)
class Rejected:
    code: int


@dataclass(frozen=True)
class Unknown:
    pass


def _inner(event_type: str, **fields: str) -> str:
    return json.dumps({"type": event_type, **fields}, ensure_ascii=False, separators=(",", ":"))


def _bounded_item(item: ProcessItem) -> ProcessItem:
    if not isinstance(item, (ToolItem, StepItem, TextItem)):
        raise TypeError("unsupported Activity item")
    if len(item.text) <= ITEM_TEXT_LIMIT:
        return item
    return replace(item, text=item.text[:ITEM_TEXT_LIMIT - 1] + "…")


def build_items(items: Sequence[ProcessItem]) -> tuple[ProcessItem, ...]:
    """Keep the oldest running items and fill remaining slots with the latest completions."""
    if len({item.item_id for item in items}) != len(items):
        raise ValueError("duplicate Activity item identity")
    running = sorted((item for item in items if item.finished_at is None),
                     key=lambda item: (item.started_at, item.item_id))[:ITEM_LIMIT]
    slots = ITEM_LIMIT - len(running)
    finished = sorted((item for item in items if item.finished_at is not None),
                      key=lambda item: (item.finished_at or 0, item.started_at, item.item_id), reverse=True)
    kept = sorted(running + finished[:slots], key=lambda item: (item.started_at, item.item_id))
    return tuple(_bounded_item(item) for item in kept)


def finish_items(items: Sequence[ProcessItem], observed_at: int) -> tuple[ProcessItem, ...]:
    """Close the retained items for the final bounded write. Reuse this list on resend."""
    return tuple(item.finish(observed_at) for item in build_items(items))


def _event(name: str, seconds: int, **fields: str) -> _Event:
    inner = _inner(name, **fields)
    if len(inner) > CONTENT_LIMIT:
        raise ValueError("serialized Activity event exceeds its character budget")
    return {"event_type": name, "timestamp": str(seconds), "content": inner}


def _events_for(item: ProcessItem) -> list[_Event]:
    if isinstance(item, TextItem):
        message_id = item.wire_id
        return [
            _event("TEXT_MESSAGE_START", item.started_at, messageId=message_id, role="assistant"),
            _event("TEXT_MESSAGE_CONTENT", item.content_at or item.finished_at, messageId=message_id, delta=item.text),
            _event("TEXT_MESSAGE_END", item.finished_at, messageId=message_id),
        ]
    if isinstance(item, StepItem):
        events = [_event("STEP_STARTED", item.started_at, stepName=item.text)]
        if item.finished_at is not None:
            events.append(_event("STEP_FINISHED", item.finished_at, stepName=item.text))
    else:
        tool_call_id = item.wire_id
        events = [_event("TOOL_CALL_START", item.started_at, toolCallId=tool_call_id, toolCallName=item.text)]
        if item.finished_at is not None:
            events.append(_event("TOOL_CALL_END", item.finished_at, toolCallId=tool_call_id))
    return events


def item_events(items: Sequence[ProcessItem]) -> list[_Event]:
    # Ten-digit seconds sort chronologically. Equal timestamps use stable
    # identities and the causal START/CONTENT/END order within each item.
    ordered = [(event["timestamp"], item.item_id, index, event)
               for item in build_items(items) for index, event in enumerate(_events_for(item))]
    ordered.sort(key=lambda entry: entry[:3])
    return [event for _, _, _, event in ordered]


def _check_timestamp(value: int) -> None:
    if type(value) is not int or not 1_000_000_000 <= value <= 9_999_999_999:
        raise ValueError("timestamp must be ten-digit Unix seconds")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate JSON field in process message receipt")
    return result


async def _receipt(
    pending: Awaitable[lark.BaseResponse],
) -> dict[str, object] | Rejected | Unknown:
    try:
        raw = (await pending).raw
        payload = json.loads(raw.content, object_pairs_hook=_unique_object)
        status = raw.status_code
        if not isinstance(payload, dict) or type(status) is not int:
            return Unknown()
        code = payload.get("code")
        if type(code) is not int:
            return Unknown()
        if code != 0 and (200 <= status < 300 or 400 <= status < 500):
            _logger.warning("Feishu process message rejected (code %d)", code)
            return Rejected(code)
        if code == 0 and 200 <= status < 300:
            return payload
    except Exception:
        # The mutation may have reached Feishu. Caller cancellation propagates.
        pass
    return Unknown()


class ProcessMessages:
    """Write process messages using the instance's app credentials."""

    def __init__(self, client: lark.Client) -> None:
        self._client = client

    @classmethod
    def for_app(cls, app_id: str, app_secret: str, *, log_level: lark.LogLevel = lark.LogLevel.WARNING) -> Self:
        client = (lark.Client.builder().app_id(app_id).app_secret(app_secret)
                  .timeout(PROCESS_OPERATION_TIMEOUT_SECONDS).log_level(log_level).build())
        return cls(client)

    async def create(
        self, receive_chat_id: str, origin_message_id: str | None, reply_in_thread: bool,
    ) -> Created | Rejected | Unknown:
        body: dict[str, str | bool] = {
            "receive_id": receive_chat_id, "reply_in_thread": reply_in_thread,
        }
        if origin_message_id is not None:
            body["origin_message_id"] = origin_message_id
        request = (lark.BaseRequest.builder().http_method(lark.HttpMethod.POST)
                   .uri(_RESOURCE).token_types({lark.AccessTokenType.TENANT})
                   .queries([("receive_id_type", "chat_id")]).body(body).build())
        result = await _receipt(self._client.arequest(request))
        if not isinstance(result, dict):
            return result
        data = result.get("data")
        if isinstance(data, dict):
            cot_id, message_id = data.get("cot_id"), data.get("message_id")
            if (isinstance(cot_id, str) and cot_id.strip() and cot_id not in {".", ".."}
                    and isinstance(message_id, str) and message_id.strip()):
                return Created(cot_id, message_id)
        return Unknown()

    async def update(
        self, cot_id: str, message_id: str, items: Sequence[ProcessItem],
    ) -> Ok | Rejected | Unknown:
        request = (lark.BaseRequest.builder().http_method(lark.HttpMethod.PUT)
                   .uri(_RESOURCE).token_types({lark.AccessTokenType.TENANT})
                   .body({"cot_id": cot_id, "message_id": message_id, "events": item_events(items)}).build())
        result = await _receipt(self._client.arequest(request))
        return Ok() if isinstance(result, dict) else result

    async def complete(
        self, cot_id: str, message_id: str, reason: Literal["done", "error", "timeout"],
    ) -> Ok | Rejected | Unknown:
        if reason not in ("done", "error", "timeout") or cot_id in ("", ".", ".."):
            raise ValueError("invalid process message completion")
        request = (lark.BaseRequest.builder().http_method(lark.HttpMethod.POST)
                   .uri(_RESOURCE + "/complete/:cot_id").paths({"cot_id": cot_id})
                   .token_types({lark.AccessTokenType.TENANT})
                   .queries([("message_id", message_id), ("reason", reason)]).build())
        result = await _receipt(self._client.arequest(request))
        return Ok() if isinstance(result, dict) else result
