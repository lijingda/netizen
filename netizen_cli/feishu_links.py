"""Pure Feishu location links shared by Channel and Admin presentation."""

from __future__ import annotations

from urllib.parse import urlencode


def chat_open_url(chat_id: str, *, p2p_target_open_id: str | None = None) -> str:
    query = urlencode(
        {"openId": p2p_target_open_id}
        if p2p_target_open_id is not None
        else {"openChatId": chat_id}
    )
    return f"https://applink.feishu.cn/client/chat/open?{query}"


def topic_open_url(chat_id: str, topic_id: str) -> str:
    # Match the official Lark CLI's desktop/mobile ID spellings and root position.
    query = urlencode({
        "open_chat_id": chat_id,
        "open_thread_id": topic_id,
        "openchatid": chat_id,
        "openthreadid": topic_id,
        "thread_position": -1,
    })
    return f"https://applink.feishu.cn/client/thread/open?{query}"
