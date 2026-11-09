"""Pure readers for native SDK values and cwd identity shared by runtime layers."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from openai_codex import (
    InternalRpcError,
    InvalidRequestError,
    MethodNotFoundError,
    TransportClosedError,
    is_retryable_error,
)
from openai_codex.generated.v2_all import (
    ModelProviderError,
    ModelProviderMismatchModelProviderError,
    ModelProviderNotConfiguredModelProviderError,
)
from pydantic import ValidationError

from ..bindings import (
    ThreadBinding,
)
from .contracts import (
    ModelProviderRejected,
    SteerRace,
    ThreadCatalogError,
    ContextWindowUsage,
)

from . import limits


def _resume_rejection(
    error: InvalidRequestError,
    thread_id: str,
) -> Literal["archived", "not_found", "closing", "occupied"] | None:
    """Classify only a direct, exact-ID public thread_resume rejection."""

    if error.code != -32600:
        return None
    if error.message == (
        f"session {thread_id} is archived. Run `codex unarchive {thread_id}` "
        "to unarchive it first."
    ):
        return "archived"
    if error.message == f"no rollout found for thread id {thread_id}":
        return "not_found"
    if error.message == (
        f"thread {thread_id} is closing; retry thread/resume after the thread is closed"
    ):
        return "closing"
    if error.message == f"thread {thread_id} already has an active writer":
        return "occupied"
    return None


_PROVIDER_ID_DISPLAY_LIMIT = 80


def _model_provider_rejection(error: BaseException) -> ModelProviderRejected | None:
    """Classify App Server's structured pre-submission provider rejection.

    App Server validates the provider before it submits a resume or input, and
    reports the result as ``-32600`` with a ``ModelProviderError`` in
    ``data`` (ADR 0093). Only that typed shape proves non-delivery: message
    text, untyped configuration-load failures and other errors keep the
    calling operation's unknown-outcome rules.
    """

    if not isinstance(error, InvalidRequestError) or error.code != -32600:
        return None
    data = error.data
    if not isinstance(data, dict):
        return None
    try:
        detail = ModelProviderError.model_validate(data).root
    except ValidationError:
        return None
    saved = _provider_id(detail.model_provider)
    if isinstance(detail, ModelProviderMismatchModelProviderError):
        current = _provider_id(detail.current_model_provider)
        reason = (
            f"该会话使用模型提供方 {saved}，而当前 Codex 配置为 {current}"
        )
        recovery = f"可把 Codex 配置切回 {saved} 后重试，或新建会话"
    elif isinstance(detail, ModelProviderNotConfiguredModelProviderError):
        reason = f"该会话使用的模型提供方 {saved} 在当前 Codex 配置中不存在"
        recovery = "可恢复该提供方配置后重试，或新建会话"
    else:
        required = _provider_id(detail.required_model_provider)
        reason = f"Codex 组织策略要求模型提供方 {required}，与 {saved} 不符"
        recovery = "请调整 Codex 配置后重试"
    return ModelProviderRejected(
        f"{reason}，Codex 拒绝执行本次操作，未发送给模型。"
        f"{recovery}；其他会话不受影响。"
    )


def _provider_id(value: str) -> str:
    """Bound a configured provider ID for user-visible explanations."""

    text = "".join(character for character in value if character.isprintable()).strip()
    if len(text) > _PROVIDER_ID_DISPLAY_LIMIT:
        text = text[: _PROVIDER_ID_DISPLAY_LIMIT - 1] + "…"
    return text or "（未命名）"


def _enum_value(value: object) -> str | None:
    raw = getattr(value, "value", value)
    return raw if isinstance(raw, str) else None


def _context_window_usage_from_native(
    token_usage: object,
) -> ContextWindowUsage | None:
    last = getattr(token_usage, "last", None)
    used_tokens = getattr(last, "total_tokens", None)
    context_window_tokens = getattr(token_usage, "model_context_window", None)
    if (
        isinstance(used_tokens, bool)
        or not isinstance(used_tokens, int)
        or used_tokens < 0
    ):
        return None
    if context_window_tokens is not None and (
        isinstance(context_window_tokens, bool)
        or not isinstance(context_window_tokens, int)
        or context_window_tokens <= 0
    ):
        return None
    return ContextWindowUsage(used_tokens, context_window_tokens)


def _is_transient_thread_read_error(
    error: BaseException,
    *,
    thread_id: str,
    include_turns: bool,
) -> bool:
    if _is_transient_thread_rpc_error(error):
        return True
    return (
        include_turns
        and isinstance(error, InvalidRequestError)
        and error.code == -32600
        and error.message == f"thread {thread_id} {limits.NOT_MATERIALIZED_SUFFIX}"
    )


def _is_recoverable_turn_observation_io_error(error: BaseException) -> bool:
    return isinstance(
        error,
        (InternalRpcError, TransportClosedError, TimeoutError, ConnectionError, OSError),
    ) or is_retryable_error(error)


def _is_paginated_turn_read_unavailable(error: BaseException) -> bool:
    return (
        isinstance(error, MethodNotFoundError)
        and error.code == -32601
        and error.message in {
            "list_turns is not supported yet",
            "list_items is not supported yet",
        }
    )


def _is_transient_thread_rpc_error(error: BaseException) -> bool:
    return isinstance(error, InternalRpcError) or is_retryable_error(error)


def _thread_creation_cwd(binding: ThreadBinding, requested: Path | None) -> Path:
    cwd = Path(binding.working_cwd) if binding.working_cwd is not None else requested
    if not isinstance(cwd, Path) or not cwd.is_absolute():
        raise SteerRace("新建 Codex 会话需要明确的绝对工作目录。")
    return cwd


def _accepted_execution_cwd(cwd: Path | None) -> Path:
    if not isinstance(cwd, Path) or not cwd.is_absolute():
        raise SteerRace("无法确认当前任务的 Codex 工作目录，不能使用调用方或 Project 目录替代。")
    return cwd


def _native_cwd(native_thread: object, thread_id: str) -> Path:
    cwd = getattr(native_thread, "cwd", None)
    # Public 0.167.0 Thread.cwd is AbsolutePathBuf(RootModel[str]).
    cwd = getattr(cwd, "root", cwd)
    if (
        getattr(native_thread, "id", None) != thread_id
        or not isinstance(cwd, str)
        or not cwd
        or not Path(cwd).is_absolute()
    ):
        raise ThreadCatalogError("Codex 会话未返回匹配身份的绝对工作目录，不能回退到 Project 目录。")
    return Path(cwd)


def _thread_status_type(native_thread: object) -> str | None:
    status = getattr(native_thread, "status", None)
    root = getattr(status, "root", status)
    value = getattr(root, "type", None)
    return value if isinstance(value, str) else None


def _thread_updated_at(native_thread: object) -> int | None:
    value = getattr(native_thread, "updated_at", None)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _thread_created_at(native_thread: object) -> int | None:
    value = getattr(native_thread, "created_at", None)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _turn_contains_item(turn: object, item_type: str) -> bool:
    return any(
        getattr(getattr(item, "root", item), "type", None) == item_type
        for item in getattr(turn, "items", ())
    )


def _final_agent_response(items: list[object]) -> str | None:
    fallback: str | None = None
    for item in reversed(items):
        root = getattr(item, "root", item)
        if getattr(root, "type", None) != "agentMessage":
            continue
        text = getattr(root, "text", None)
        if not isinstance(text, str):
            continue
        phase = _enum_value(getattr(root, "phase", None))
        if phase == "final_answer":
            return text
        if phase is None and fallback is None:
            fallback = text
    return fallback
