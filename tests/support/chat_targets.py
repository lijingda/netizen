"""Explicit target-validation fixture for tests without the Feishu read SDK."""

from netizen_cli.channel.messages import public_chat_kind
from netizen_cli.chat_targets import ChatTargetError, ValidatedChatTarget
from netizen_cli.management.chat_directory import ChatDirectoryError


class FakeChatTargetDirectory:
    def __init__(self, chat_info):
        self.chat_info = chat_info
        self.calls = []
        self.errors = {}

    async def validate_target(self, chat_id):
        self.calls.append(chat_id)
        if chat_id in self.errors:
            raise self.errors[chat_id]
        try:
            info = await self.chat_info.get_chat_info(chat_id)
        except Exception as error:
            raise ChatTargetError("chat_unavailable", "聊天不可访问。") from error
        kind = public_chat_kind(info)
        if kind is None:
            raise ChatTargetError("chat_kind_unknown", "无法确认聊天类型。")
        return ValidatedChatTarget(chat_id, kind)

    async def query(self, *, query="", page_token=None, page_size=20):
        raise ChatDirectoryError("chat_query_unavailable", "群聊查询暂不可用。")

    async def validate(self, chat_id):
        raise ChatDirectoryError("chat_query_unavailable", "群聊查询暂不可用。")
