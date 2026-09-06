"""Channel transport operations used by the application and presenters."""

from __future__ import annotations

from typing import Any, Protocol


class ReplyChannel(Protocol):
    @property
    def bot_identity(self) -> Any: ...

    async def reply(self, message: Any, content: Any, opts: Any = None) -> object: ...

    async def send(self, to: str, content: Any, opts: Any = None) -> object: ...

    async def add_reaction(self, message_id: str, emoji_type: str) -> object: ...

    async def remove_reaction(
        self,
        message_id: str,
        reaction_id: str,
    ) -> object: ...

    async def update_card(self, message_id: str, card: dict[str, Any]) -> object: ...

    async def fetch_message(self, message_id: str) -> dict[str, Any]: ...

    async def fetch_inbound_message(self, message_id: str) -> Any: ...

    async def fetch_quoted_context(self, message_id: str) -> Any: ...

    async def download_resource(
        self,
        file_key: str,
        resource_type: str = "image",
        message_id: str | None = None,
    ) -> bytes | None: ...

    async def get_chat_info(self, chat_id: str) -> Any: ...
