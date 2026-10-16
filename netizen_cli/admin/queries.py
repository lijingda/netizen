"""Admin query values and filter-bound pagination cursors."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from .errors import AdminWebError
from ..bindings import BindingCursor, SideTopicCursor
from ..domain import ScopeKind
from ..management import SessionInventoryState, SessionNativeCursor


_MAX_TEXT_BYTES = 4_096
_SESSION_PAGE_SIZES = frozenset((10, 20, 50, 100))
_ISO_INSTANT_PATTERN = re.compile(
    r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}"
    r"(?::[0-9]{2}(?:\.[0-9]{1,6})?)?(?:Z|[+-][0-9]{2}:[0-9]{2})\Z"
)


def _optional_one(values: Mapping[str, list[str]], name: str) -> str | None:
    items = values.get(name)
    if items is None:
        return None
    if len(items) != 1 or not items[0]:
        raise AdminWebError(400, "invalid_query", f"查询参数 {name} 无效。")
    return items[0]


def _require_query_keys(
    values: Mapping[str, list[str]],
    allowed: set[str],
) -> None:
    unknown = values.keys() - allowed
    if unknown:
        raise AdminWebError(400, "invalid_query", "包含未知查询参数。")


def _page_size(values: Mapping[str, list[str]]) -> int:
    raw = _optional_one(values, "pageSize")
    if raw is None:
        return 25
    try:
        value = int(raw, 10)
    except ValueError:
        raise AdminWebError(400, "invalid_page_size", "分页大小无效。") from None
    if not 1 <= value <= 50:
        raise AdminWebError(400, "invalid_page_size", "分页大小必须为 1 到 50。")
    return value


def _session_page_size(values: Mapping[str, list[str]]) -> int:
    raw = _optional_one(values, "pageSize")
    if raw is None:
        return 20
    try:
        value = int(raw, 10)
    except ValueError:
        raise AdminWebError(400, "invalid_page_size", "分页大小无效。") from None
    if value not in _SESSION_PAGE_SIZES:
        raise AdminWebError(
            400,
            "invalid_page_size",
            "Sessions 分页大小必须为 10、20、50 或 100。",
        )
    return value


def _session_order_query(values: Mapping[str, list[str]]) -> tuple[str, str]:
    sort_by = _optional_one(values, "sortBy") or "updatedAt"
    direction = _optional_one(values, "sortDirection") or "desc"
    if sort_by not in {"createdAt", "updatedAt"} or direction not in {"asc", "desc"}:
        raise AdminWebError(400, "invalid_sort", "排序字段或方向无效。")
    return ("created_at" if sort_by == "createdAt" else "updated_at"), direction


def _optional_text_query(
    values: Mapping[str, list[str]],
    name: str,
    *,
    maximum: int = _MAX_TEXT_BYTES,
) -> str | None:
    value = _optional_one(values, name)
    if value is None:
        return None
    if len(value.encode("utf-8")) > maximum or value.strip() != value:
        raise AdminWebError(400, "invalid_query", f"查询参数 {name} 无效。")
    return value


def _text_set_query(
    values: Mapping[str, list[str]],
    name: str,
) -> tuple[str, ...] | None:
    items = values.get(name)
    if items is None:
        return None
    if not items or any(
        not item
        or item.strip() != item
        or len(item.encode("utf-8")) > _MAX_TEXT_BYTES
        for item in items
    ):
        raise AdminWebError(400, "invalid_query", f"查询参数 {name} 无效。")
    return tuple(sorted(set(items)))


def _created_range_query(
    values: Mapping[str, list[str]],
) -> tuple[str | None, str | None]:
    return _time_range_query(values, "created", "创建")


def _updated_range_query(
    values: Mapping[str, list[str]],
) -> tuple[str | None, str | None]:
    return _time_range_query(values, "updated", "更新")


def _time_range_query(
    values: Mapping[str, list[str]], prefix: str, label: str,
) -> tuple[str | None, str | None]:
    lower = _optional_time_query(values, f"{prefix}From")
    upper = _optional_time_query(values, f"{prefix}Before")
    if (
        lower is not None
        and upper is not None
        and lower >= upper
    ):
        raise AdminWebError(
            400,
            "invalid_time_range",
            f"{label}时间的开始时间必须早于结束时间。",
        )
    return lower, upper


def _optional_time_query(
    values: Mapping[str, list[str]],
    name: str,
) -> str | None:
    raw = _optional_text_query(values, name, maximum=64)
    if raw is None:
        return None
    if _ISO_INSTANT_PATTERN.fullmatch(raw) is None:
        raise AdminWebError(
            400,
            "invalid_time",
            f"查询参数 {name} 必须是带时区的 ISO-8601 时间。",
        )
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError("timezone is required")
        return parsed.astimezone(UTC).isoformat(timespec="microseconds")
    except (OverflowError, ValueError):
        raise AdminWebError(
            400,
            "invalid_time",
            f"查询参数 {name} 必须是带时区的 ISO-8601 时间。",
        ) from None


def _optional_bool_query(
    values: Mapping[str, list[str]],
    name: str,
) -> bool | None:
    value = _optional_one(values, name)
    if value is None:
        return None
    if value == "true":
        return True
    if value == "false":
        return False
    raise AdminWebError(400, "invalid_query", f"查询参数 {name} 必须是布尔值。")


def _scope_kinds_query(
    values: Mapping[str, list[str]],
) -> tuple[ScopeKind, ...] | None:
    raw = _text_set_query(values, "scopeKind")
    if raw is None:
        return None
    try:
        kinds = tuple(ScopeKind(value) for value in raw)
    except ValueError:
        raise AdminWebError(400, "invalid_scope_kind", "Scope 类型无效。") from None
    return None if set(kinds) == set(ScopeKind) else kinds


def _current_query(values: Mapping[str, list[str]]) -> bool | None:
    raw = _text_set_query(values, "current")
    if raw is None:
        return None
    if any(value not in {"true", "false"} for value in raw):
        raise AdminWebError(400, "invalid_query", "查询参数 current 必须是布尔值。")
    return raw[0] == "true" if len(raw) == 1 else None


def _session_inventory_state(
    values: Mapping[str, list[str]],
) -> SessionInventoryState:
    raw = _optional_one(values, "inventoryState") or "active"
    if raw not in {"active", "archived", "lazy"}:
        raise AdminWebError(
            400,
            "invalid_inventory_state",
            "会话状态无效。",
        )
    return SessionInventoryState(raw)


def _id_query(values: Mapping[str, list[str]], name: str) -> tuple[str, ...]:
    raw_values = values.get(name, [])
    result: list[str] = []
    for raw in raw_values:
        for value in raw.split(","):
            if not value or value.strip() != value or len(value.encode("utf-8")) > 256:
                raise AdminWebError(400, "invalid_ids", f"{name} 包含无效 ID。")
            result.append(value)
    return tuple(result)


def _fingerprint(route: str, filters: object) -> str:
    canonical = json.dumps(
        {"route": route, "filters": filters},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:32]


def _encode_cursor(kind: str, values: Sequence[str], fingerprint: str) -> str:
    payload = json.dumps(
        {"v": 1, "t": kind, "k": list(values), "f": fingerprint},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _decode_cursor(
    encoded: str | None,
    *,
    kind: str,
    length: int,
    fingerprint: str,
) -> tuple[str, ...] | None:
    if encoded is None:
        return None
    if len(encoded) > 2_048:
        raise AdminWebError(400, "invalid_cursor", "分页游标无效。")
    try:
        padding = "=" * (-len(encoded) % 4)
        raw = base64.b64decode(
            encoded + padding,
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        raise AdminWebError(400, "invalid_cursor", "分页游标无效。") from None
    if (
        not isinstance(payload, dict)
        or set(payload) != {"v", "t", "k", "f"}
        or payload["v"] != 1
        or payload["t"] != kind
        or payload["f"] != fingerprint
        or not isinstance(payload["k"], list)
        or len(payload["k"]) != length
        or any(not isinstance(value, str) or not value for value in payload["k"])
    ):
        raise AdminWebError(400, "invalid_cursor", "分页游标与当前筛选不匹配。")
    canonical = _encode_cursor(kind, payload["k"], fingerprint)
    if canonical != encoded:
        raise AdminWebError(400, "invalid_cursor", "分页游标无效。")
    return tuple(payload["k"])


def _encode_binding_cursor(
    cursor: BindingCursor | None,
    fingerprint: str,
) -> str | None:
    if cursor is None:
        return None
    return _encode_cursor(
        "binding",
        (cursor.created_at, cursor.binding_id),
        fingerprint,
    )


def _decode_binding_cursor(
    encoded: str | None,
    fingerprint: str,
) -> BindingCursor | None:
    values = _decode_cursor(
        encoded,
        kind="binding",
        length=2,
        fingerprint=fingerprint,
    )
    return BindingCursor(*values) if values is not None else None


def _encode_session_cursor(
    cursor: BindingCursor | SessionNativeCursor | None,
    fingerprint: str,
) -> str | None:
    if not isinstance(cursor, SessionNativeCursor):
        return _encode_binding_cursor(cursor, fingerprint)
    marker = "start" if cursor.cursor is None else "next"
    return _encode_cursor(
        "session-native", (marker, cursor.cursor if cursor.cursor is not None else "start", str(cursor.offset)), fingerprint,
    )


def _decode_session_cursor(
    encoded: str | None,
    fingerprint: str,
    *,
    inventory_state: SessionInventoryState,
) -> BindingCursor | SessionNativeCursor | None:
    if inventory_state is SessionInventoryState.LAZY:
        return _decode_binding_cursor(encoded, fingerprint)
    values = _decode_cursor(encoded, kind="session-native", length=3, fingerprint=fingerprint)
    if values is None:
        return None
    marker, raw_cursor, raw_offset = values
    if (
        marker not in {"start", "next"}
        or marker == "start" and raw_cursor != "start"
        or re.fullmatch(r"0|[1-9][0-9]?", raw_offset) is None
    ):
        raise AdminWebError(400, "invalid_cursor", "分页游标无效。")
    return SessionNativeCursor(None if marker == "start" else raw_cursor, int(raw_offset))


def _encode_side_cursor(
    cursor: SideTopicCursor | None,
    fingerprint: str,
) -> str | None:
    if cursor is None:
        return None
    if cursor.updated_at is not None:
        return _encode_cursor("side-updated", (cursor.created_at, cursor.side_id, cursor.updated_at), fingerprint)
    return _encode_cursor("side", (cursor.created_at, cursor.side_id), fingerprint)


def _decode_side_cursor(
    encoded: str | None,
    fingerprint: str,
    *,
    sort_by: str = "created_at",
) -> SideTopicCursor | None:
    values = _decode_cursor(
        encoded,
        kind="side-updated" if sort_by == "updated_at" else "side",
        length=3 if sort_by == "updated_at" else 2,
        fingerprint=fingerprint,
    )
    return SideTopicCursor(*values) if values is not None else None


def _encode_project_cursor(cursor: str | None, fingerprint: str) -> str | None:
    if cursor is None:
        return None
    return _encode_cursor("project", (cursor,), fingerprint)


def _decode_project_cursor(encoded: str | None, fingerprint: str) -> str | None:
    values = _decode_cursor(
        encoded,
        kind="project",
        length=1,
        fingerprint=fingerprint,
    )
    return values[0] if values is not None else None
