"""A card submission's real operator and separate, bot-authored reply anchor."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..prompt_projection import PromptProjectionError, project_identity
from ..user_questions import QuestionTarget
from .ports import ReplyChannel


@dataclass(frozen=True, slots=True)
class CardAnswerConversation:
    chat_id: str
    chat_type: str
    thread_id: str | None = None


@dataclass(frozen=True, slots=True)
class CardAnswerOrigin:
    app_id: str
    chat_id: str
    message_id: str
    conversation: CardAnswerConversation
    source_card_id: str
    target: QuestionTarget

    @property
    def id(self) -> str:
        return self.message_id


async def card_answer_sender(
    channel: ReplyChannel, chat_id: str, operator: Any,
) -> dict[str, Any]:
    """Resolve attribution for the callback operator, never the card's author."""
    sender = project_identity(operator)
    open_id = sender.get("open_id")
    if not open_id:
        raise PromptProjectionError("卡片回调缺少回答者身份，本条回答未执行。")
    if "display_name" not in sender:
        members = await channel.get_chat_members(chat_id, id_type="open_id")
        member = next((item for item in members if item.id == open_id), None)
        name = getattr(member, "name", None)
        if not isinstance(name, str) or not name.strip():
            raise PromptProjectionError(
                "无法获取回答者姓名，本条回答未执行；请检查群成员读取权限后重试。"
            )
        sender["display_name"] = name
    return sender
