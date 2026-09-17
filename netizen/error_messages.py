"""Bounded, credential-filtered explanations for user-visible failures.

Only explicit exception causes and public native error fields are projected.
RPC data, native additional details, traceback and arbitrary object reprs are
not user-facing error messages.
"""

from __future__ import annotations

import re
from enum import Enum

from openai_codex.errors import JsonRpcError, TransportClosedError
from openai_codex.types import TurnError
from pydantic import ValidationError

from .turn_activity import sanitize_activity_operation_text


_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_.:/-]{0,95}\Z")
_NATIVE_VARIANTS = (
    "http_connection_failed",
    "response_stream_connection_failed",
    "response_stream_disconnected",
    "response_too_many_failed_attempts",
    "active_turn_not_steerable",
)


def _safe_text(value: str, *, limit: int) -> str:
    # A serialized payload or traceback is not a public explanation. Avoid
    # accidentally exposing one through Exception.__str__ or TurnError.message.
    if value != "[敏感内容已隐藏]" and value.lstrip().startswith(("{", "[", "Traceback (")):
        return "错误详情不是可显示的文字说明"
    return sanitize_activity_operation_text(value, limit=limit) or "未提供错误说明"


def _describe_one(error: BaseException, *, limit: int) -> str:
    if isinstance(error, ValidationError):
        # Pydantic's text and even error locations/types may contain input data
        # or custom-validator details; only the validation count is safe here.
        return (
            f"ValidationError：数据格式不符合预期（{error.error_count()} 处校验错误）"
        )
    if isinstance(error, JsonRpcError):
        detail = _safe_text(error.message, limit=limit)
        return f"{type(error).__name__}（code={error.code}）：{detail}"
    if isinstance(error, TimeoutError):
        fallback = "请求超时，未收到确认结果"
    elif isinstance(error, (ConnectionError, TransportClosedError)):
        fallback = "与后端的连接中断，未收到确认结果"
    else:
        fallback = "未提供错误说明"
    if isinstance(error, OSError) and isinstance(error.strerror, str):
        message = error.strerror
    elif not error.args or (len(error.args) == 1 and isinstance(error.args[0], str)):
        message = str(error).strip()
    else:
        message = ""
    detail = _safe_text(message, limit=limit) if message else fallback
    # Domain exceptions already carry actionable wording. RuntimeError is also
    # used to carry the native Turn's public explanation.
    if message and (
        type(error) is RuntimeError or type(error).__module__.startswith("netizen.")
    ):
        return detail
    return f"{type(error).__name__}：{detail}"


def describe_error(error: BaseException, *, limit: int = 500) -> str:
    """Keep operation context and its explicit underlying cause within a bound."""

    if limit < 1:
        return ""
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen and len(chain) < 8:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__
    outer = _describe_one(chain[0], limit=limit)
    if len(chain) == 1:
        return _bounded(outer, limit)
    underlying = _describe_one(chain[-1], limit=limit)
    if underlying == outer:
        return _bounded(underlying, limit)
    # Reserve space for the underlying reason, even when a wrapper is verbose.
    context = _bounded(outer, min(180, max(1, limit // 3)))
    return _bounded(f"{context}；原因：{underlying}", limit)


def _bounded(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _native_code(info: object) -> str | None:
    root = getattr(info, "root", info)
    value = root.value if isinstance(root, Enum) else getattr(root, "type", root)
    if isinstance(value, str) and _CODE.fullmatch(value):
        return value
    for name in _NATIVE_VARIANTS:
        variant = getattr(root, name, None)
        if variant is not None:
            code = name.split("_")[0] + "".join(
                part.title() for part in name.split("_")[1:]
            )
            status = getattr(variant, "http_status_code", None)
            if type(status) is int and 100 <= status <= 599:
                return f"{code}, HTTP {status}"
            return code
    return None


def native_turn_failure(error: object) -> RuntimeError:
    """Project only the SDK TurnError's public explanation and typed error code."""

    if not isinstance(error, TurnError):
        return RuntimeError("Codex 本轮执行失败，未提供错误说明")
    message = _safe_text(error.message, limit=400)
    code = _native_code(error.codex_error_info)
    if code is not None:
        return RuntimeError(f"Codex 错误码 {code}：{message}")
    return RuntimeError(message)
