"""Display existing session identities without changing control references."""

from __future__ import annotations


def session_identity_lines(
    *, short_id: str, native_thread_id: str | None,
) -> tuple[str, str]:
    native = (
        native_thread_id
        if native_thread_id is not None
        else "尚未生成，首条消息后创建"
    )
    return (
        f"Netizen 会话编号（/resume）：{short_id}",
        f"Codex 会话 ID（跨会话工具）：{native}",
    )
