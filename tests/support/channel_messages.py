"""Feishu messages, transport responses and history fixtures."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from lark_channel import (
    Conversation,
    Identity,
    InboundMessage,
    MediaSource,
    ResourceDescriptor,
    TextContent,
)
from netizen.domain import FeishuScope, MessageContextAnchor
from netizen.message_history import MessageHistoryWindow


PNG = b"\x89PNG\r\n\x1a\nchannel-test"


class FakeMessage:
    def __init__(
        self,
        text: str,
        *,
        message_id: str,
        sender_id: str = "ou_user",
        display_name: str = "Current User",
        union_id: str | None = None,
        user_id: str | None = None,
        sender_type: str = "user",
        is_bot: bool = False,
        chat_id: str = "oc_direct",
        chat_type: str = "p2p",
        thread_id: str | None = None,
        mentioned_bot: bool = True,
        raw_content_type: str = "text",
        resources: list[object] | None = None,
        mentions: list[object] | None = None,
        content: object | None = None,
        reply_id: str | None = None,
        raw: dict[str, object] | None = None,
        create_time: int = 123,
    ) -> None:
        self.id = message_id
        self.create_time = create_time
        self.body_text = text
        self.sender = SimpleNamespace(
            open_id=sender_id,
            display_name=display_name,
            union_id=union_id,
            user_id=user_id,
            sender_type=sender_type,
            is_bot=is_bot,
        )
        self.conversation = SimpleNamespace(
            chat_id=chat_id,
            chat_type=chat_type,
            thread_id=thread_id,
        )
        self.mentioned_bot = mentioned_bot
        self.resources = resources or []
        self.raw_content_type = raw_content_type
        self.mentions = mentions or []
        self.content = content
        self.content_text = text
        self.reply = (
            SimpleNamespace(message_id=reply_id) if reply_id is not None else None
        )
        self.raw = raw or {}


class FakeChannel:
    def __init__(self) -> None:
        self.replies: list[tuple[str, object]] = []
        self.reply_targets: list[object] = []
        self.reply_results: list[object | BaseException] = []
        self.send_calls: list[tuple[str, object, object]] = []
        self.send_results: list[object | BaseException] = []
        self.upload_calls: list[tuple[MediaSource, str]] = []
        self.upload_results: list[str | BaseException] = []
        self.reactions: list[tuple[str, str]] = []
        self.reaction_operations: list[tuple[str, str, str]] = []
        self.reaction_removals: list[tuple[str, str]] = []
        self.reaction_remove_attempted = asyncio.Event()
        self._next_reaction_id = 1
        self.updates: list[tuple[str, dict[str, object]]] = []
        self.fetched_messages: dict[str, dict[str, object]] = {}
        self.inbound_messages: dict[str, object | None | BaseException] = {}
        self.quoted_contexts: dict[str, object | None | BaseException] = {}
        self.fetch_inbound_calls: list[str] = []
        self.fetch_quoted_calls: list[str] = []
        self.chat_types: dict[str, str] = {}
        self.chat_info_calls: list[str] = []
        self.resource_bodies: dict[
            tuple[str, str],
            bytes | None | BaseException | asyncio.Event,
        ] = {}
        self.download_resource_calls: list[tuple[str, str, str | None]] = []
        self.fail_card_updates = False
        self.card_update_success = True
        self.card_update_results: list[object | BaseException] = []
        self.fail_once_reaction_on: str | None = None
        self.fail_once_reaction_remove = False
        self.bot_identity = SimpleNamespace(open_id="ou_bot", name="椰羊")

    async def reply(self, message: FakeMessage, content: object, opts=None) -> object:
        self.reply_targets.append(message)
        self.replies.append((message.id, content))
        if self.reply_results:
            result = self.reply_results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return object()

    async def send(self, to: str, content: object, opts=None) -> object:
        self.send_calls.append((to, content, opts))
        if not self.send_results:
            raise AssertionError("unexpected channel.send call")
        result = self.send_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    async def add_reaction(self, message_id: str, emoji_type: str) -> object:
        self.reactions.append((message_id, emoji_type))
        self.reaction_operations.append(("add", message_id, emoji_type))
        if self.fail_once_reaction_on == emoji_type:
            self.fail_once_reaction_on = None
            raise RuntimeError("reaction failed")
        reaction_id = f"reaction-{self._next_reaction_id}"
        self._next_reaction_id += 1
        return SimpleNamespace(
            success=True,
            raw={"data": {"reaction_id": reaction_id}},
        )

    async def upload_media(self, source: MediaSource, *, kind: str) -> str:
        self.upload_calls.append((source, kind))
        if self.upload_results:
            result = self.upload_results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return f"img_uploaded_{len(self.upload_calls)}"

    async def remove_reaction(
        self,
        message_id: str,
        reaction_id: str,
    ) -> object:
        self.reaction_removals.append((message_id, reaction_id))
        self.reaction_operations.append(("remove", message_id, reaction_id))
        self.reaction_remove_attempted.set()
        if self.fail_once_reaction_remove:
            self.fail_once_reaction_remove = False
            return SimpleNamespace(success=False)
        return SimpleNamespace(success=True)

    async def update_card(self, message_id: str, card: dict[str, object]) -> object:
        self.updates.append((message_id, card))
        if self.card_update_results:
            result = self.card_update_results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        if self.fail_card_updates:
            raise RuntimeError("card update failed")
        return SimpleNamespace(success=self.card_update_success)

    async def fetch_message(self, message_id: str) -> dict[str, object]:
        return self.fetched_messages[message_id]

    async def fetch_inbound_message(self, message_id: str) -> object | None:
        self.fetch_inbound_calls.append(message_id)
        result = self.inbound_messages.get(message_id)
        if isinstance(result, BaseException):
            raise result
        return result

    async def fetch_quoted_context(self, message_id: str) -> object | None:
        self.fetch_quoted_calls.append(message_id)
        result = self.quoted_contexts.get(message_id)
        if isinstance(result, BaseException):
            raise result
        return result

    async def download_resource(
        self,
        file_key: str,
        resource_type: str = "image",
        message_id: str | None = None,
    ) -> bytes | None:
        self.download_resource_calls.append((file_key, resource_type, message_id))
        result = self.resource_bodies.get((str(message_id), file_key))
        if isinstance(result, BaseException):
            raise result
        if isinstance(result, asyncio.Event):
            await result.wait()
            return PNG
        return result

    async def get_chat_info(self, chat_id: str) -> object:
        self.chat_info_calls.append(chat_id)
        return SimpleNamespace(
            chat_type="unknown",
            chat_mode=self.chat_types.get(chat_id, "group"),
        )


class FakeMessageHistory:
    def __init__(self) -> None:
        self.resolve_calls: list[tuple[FeishuScope, str]] = []
        self.read_calls: list[
            tuple[FeishuScope, MessageContextAnchor, str]
        ] = []
        self.anchors: dict[str, MessageContextAnchor] = {}
        self.window: MessageHistoryWindow | None = None

    async def resolve_anchor(
        self,
        scope: FeishuScope,
        message_id: str,
    ) -> MessageContextAnchor:
        self.resolve_calls.append((scope, message_id))
        return self.anchors.get(
            message_id,
            MessageContextAnchor(message_id, 1_000),
        )

    async def read_window(
        self,
        scope: FeishuScope,
        lower: MessageContextAnchor,
        upper_id: str,
    ) -> MessageHistoryWindow:
        self.read_calls.append((scope, lower, upper_id))
        if self.window is None:
            raise AssertionError("unexpected history read")
        return self.window


def quoted_inbound(
    *,
    message_id: str = "om_quoted",
    chat_id: str = "oc_direct",
    content: object | None = None,
    content_text: str = "quoted text",
    raw_content_type: str = "text",
    resources: list[ResourceDescriptor] | None = None,
) -> InboundMessage:
    return InboundMessage(
        id=message_id,
        create_time=123,
        conversation=Conversation(chat_id=chat_id, chat_type="p2p"),
        sender=Identity(open_id="ou_quoted", display_name="Quoted User"),
        content=content or TextContent(text=content_text),
        raw={"message_id": message_id},
        content_text=content_text,
        resources=resources or [],
        body_text=content_text,
        raw_content_type=raw_content_type,
    )


def plain_prompt_projection(native_input: object) -> tuple[str, dict[str, object]]:
    if isinstance(native_input, list):
        prompt_text = native_input[-1].text
    else:
        prompt_text = native_input
    assert isinstance(prompt_text, str)
    request_text, trailer = prompt_text.split(
        "\n\n<feishu_current_message_context>\n",
        1,
    )
    metadata_json, closing = trailer.rsplit(
        "\n</feishu_current_message_context>",
        1,
    )
    assert closing == ""
    return request_text, json.loads(metadata_json)
