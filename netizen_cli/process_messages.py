"""The three Feishu message_cot writes approved by ADR 0107."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import IntEnum
from typing import Literal

import lark_oapi as lark


_RESOURCE = "/open-apis/im/v1/message_cot"
_EVENT = "ACTIVITY_SNAPSHOT"
CONTENT_LIMIT = 1_800
_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SnapshotContent:
    title: str
    rows: tuple[str, ...]


@dataclass(frozen=True)
class ActivitySnapshot:
    activity_id: str  # Stable identity of the presentation Turn inside the event.
    content: SnapshotContent
    timestamp: int

    def __post_init__(self) -> None:
        if not self.activity_id.strip():
            raise ValueError("a snapshot requires a stable activity identity")
        _check_timestamp(self.timestamp)


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


def _serialize(activity_id: str, content: SnapshotContent) -> str:
    return json.dumps({
        "type": _EVENT, "messageId": activity_id, "activityType": "PLAN",
        "content": {"title": content.title, "rows": content.rows}, "replace": True,
    }, ensure_ascii=False, separators=(",", ":"))


def build_snapshot_content(
    activity_id: str, title: str, rows: tuple[str, ...], *,
    state_line: str | None = None, terminal_lines: tuple[str, ...] = (),
) -> SnapshotContent:
    """Pack already-safe display rows (oldest first), including the full event overhead."""
    state = (state_line,) if state_line is not None else ()
    required = state + terminal_lines
    available = CONTENT_LIMIT - len(_serialize(activity_id, SnapshotContent(title, required)))
    if available < 0:
        # All ordinary rows have been dropped. Keep each required line, sharing
        # the remaining space when even the title/state/terminal text is long.
        def shortened(width: int) -> SnapshotContent:
            return SnapshotContent(_clip(title, width), tuple(_clip(row, width) for row in required))

        width = _largest_width(lambda n: len(_serialize(activity_id, shortened(n))) <= CONTENT_LIMIT)
        if width < 0:
            raise ValueError("Activity identity and required line structure exceed the budget")
        return shortened(width)

    kept: list[str] = []
    for row in reversed(rows):
        separator = int(bool(required or kept))
        cost = len(json.dumps(row, ensure_ascii=False)) + separator
        if cost > available:
            if not kept:
                # Once just the newest row remains, shorten its text in place.
                width = _largest_width(
                    lambda n: len(json.dumps(_clip(row, n), ensure_ascii=False)) + separator <= available
                )
                if width >= 0:
                    kept.append(_clip(row, width))
            break
        kept.append(row)
        available -= cost
    return SnapshotContent(title, (*state, *reversed(kept), *terminal_lines))


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[:width] + "…"


def _largest_width(fits: Callable[[int], bool]) -> int:
    low, high = -1, CONTENT_LIMIT + 1
    while high - low > 1:
        middle = (low + high) // 2
        if fits(middle):
            low = middle
        else:
            high = middle
    return low


def snapshot_revision(
    activity_id: str, content: SnapshotContent, *, now_seconds: int,
    previous: ActivitySnapshot | None = None,
) -> ActivitySnapshot:
    """Allocate a revision from this message's last desired snapshot, including unacknowledged writes."""
    if previous is not None:
        if previous.activity_id != activity_id:
            raise ValueError("previous revision belongs to a different activity")
        if previous.content == content:
            return previous
        now_seconds = max(now_seconds, previous.timestamp + 1)
    return ActivitySnapshot(activity_id, content, now_seconds)


def _check_timestamp(value: int) -> None:
    if type(value) is not int or not 1_000_000_000 <= value <= 9_999_999_999:
        raise ValueError("timestamp must be ten-digit Unix seconds")


async def _receipt(
    pending: Awaitable[lark.BaseResponse],
) -> dict[str, object] | Rejected | Unknown:
    try:
        raw = (await pending).raw
        payload = json.loads(raw.content)
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
    """Borrow the same instance-owned lark-oapi client as the history reader."""

    def __init__(self, client: lark.Client) -> None:
        self._client = client

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
        self, cot_id: str, message_id: str, snapshot: ActivitySnapshot,
    ) -> Ok | Rejected | Unknown:
        inner = _serialize(snapshot.activity_id, snapshot.content)
        if len(inner) > CONTENT_LIMIT:
            raise ValueError("serialized Activity snapshot exceeds its character budget")
        request = (lark.BaseRequest.builder().http_method(lark.HttpMethod.PUT)
                   .uri(_RESOURCE).token_types({lark.AccessTokenType.TENANT})
                   .body({"cot_id": cot_id, "message_id": message_id, "events": [{
                       "event_type": _EVENT, "content": inner, "timestamp": str(snapshot.timestamp),
                   }]}).build())
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
