"""Bounded, self-contained navigation for Scope session cards."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from ..bindings import BindingCursor
from ..domain import FeishuScope
from ..session_inventory import NativeSessionCursor as SessionNativeCursor


_MAX_HISTORY = 8
_MAX_TOKEN_CHARS = 1024
SessionCursor = BindingCursor | SessionNativeCursor | None


@dataclass(frozen=True, slots=True)
class SessionCardPage:
    state: str = "active"
    page: int = 0
    positions: tuple[SessionCursor, ...] = (None,)

    @property
    def cursor(self) -> SessionCursor:
        return self.positions[-1]

    def next(self, cursor: SessionCursor) -> SessionCardPage:
        if cursor is None:
            raise ValueError("Next page requires a cursor")
        return SessionCardPage(
            self.state, self.page + 1,
            (*self.positions[-_MAX_HISTORY:], cursor),
        )

    def previous(self) -> SessionCardPage | None:
        if len(self.positions) < 2:
            return None
        return SessionCardPage(self.state, self.page - 1, self.positions[:-1])

    def encode(self, scope: FeishuScope) -> str:
        positions = [_encode_position(position) for position in self.positions]
        while True:
            token = json.dumps(
                [1, _scope_key(scope), self.state, self.page, positions],
                separators=(",", ":"), ensure_ascii=True,
            )
            if len(token) <= _MAX_TOKEN_CHARS:
                return token
            if len(positions) == 1:
                raise ValueError("Session cursor exceeds the card navigation limit")
            positions.pop(0)

    @classmethod
    def decode(cls, token: str | None, scope: FeishuScope, *, state: str = "active") -> SessionCardPage:
        if token is None:
            return cls(state=state)
        try:
            if not isinstance(token, str) or len(token) > _MAX_TOKEN_CHARS:
                raise ValueError
            version, scope_key, state, page, raw_positions = json.loads(token)
            if (
                type(version) is not int or version != 1
                or scope_key != _scope_key(scope)
                or state not in {"active", "archived", "lazy"}
                or type(page) is not int or page < 0
                or not isinstance(raw_positions, list)
                or not 1 <= len(raw_positions) <= _MAX_HISTORY + 1
                or len(raw_positions) > page + 1
            ):
                raise ValueError
            positions = tuple(_decode_position(raw, state) for raw in raw_positions)
            if any(position is None for position in positions[1:]):
                raise ValueError
            return cls(state, page, positions)
        except (ValueError, TypeError, KeyError) as error:
            raise ValueError("会话分页已过期或无效，请重新发送 /sessions。") from error


def _scope_key(scope: FeishuScope) -> str:
    return hashlib.sha256(scope.key.encode()).hexdigest()[:24]


def _encode_position(cursor: SessionCursor) -> list[object] | None:
    if cursor is None:
        return None
    if isinstance(cursor, BindingCursor):
        return [cursor.created_at, cursor.binding_id]
    return [cursor.cursor, cursor.offset]


def _decode_position(raw: object, state: str) -> SessionCursor:
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) != 2:
        raise ValueError
    if state == "lazy":
        if not all(isinstance(value, str) and value for value in raw):
            raise ValueError
        return BindingCursor(*raw)
    return SessionNativeCursor(*raw)
