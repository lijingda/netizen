"""Project current task mentions without changing interaction parsing.

Only public source placeholders and post at-nodes identify mentions. Rendered
names, handwritten tags, quotes and historical messages are not reinterpreted.
"""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass, replace
from html import escape
from typing import Any
from uuid import uuid4

from lark_channel import PostContent, TextContent, flatten_content


_PLACEHOLDER = re.compile(r"@_user_\d+|@_all(?![A-Za-z0-9_])")
_LITERAL_AT = re.compile(r"<at\b[^>]*>.*?</at>", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class _Token:
    text: str
    is_self: bool = False
    is_mention: bool = False
    is_literal: bool = False


class _Mentions:
    def __init__(self, message: Any, bot_open_id: str | None, bot_name: str | None) -> None:
        mentions = getattr(message, "mentions", None) or ()
        self.by_key = {m.key: m for m in mentions if getattr(m, "key", None)}
        self.by_id = {
            m.open_id: m for m in mentions if getattr(m, "open_id", None)
        }
        self.bot_open_id = bot_open_id
        self.bot_name = bot_name
        self.tokens: dict[str, _Token] = {}
        self.prefix = f"\x00netizen-at-{uuid4().hex}-"

    def token(
        self, text: str, *, is_self: bool = False, is_mention: bool = False,
        is_literal: bool = False,
    ) -> str:
        key = f"{self.prefix}{len(self.tokens)}\x00"
        self.tokens[key] = _Token(text, is_self, is_mention, is_literal)
        return key

    def mention(self, uid: str, name: str | None = None) -> str | None:
        if uid in {"@_all", "all", "all_members"}:
            return self.token('<at target="all">所有人</at>', is_mention=True)
        mention = self.by_key.get(uid) or self.by_id.get(uid)
        open_id = getattr(mention, "open_id", None)
        if not open_id and uid.startswith("ou_"):
            open_id = uid
        name = name or getattr(mention, "name", None)
        if not open_id:
            # Keep the SDK's display/placeholder fallback; never guess an ID.
            return self.token(f"@{name}") if name else None
        is_self = open_id == self.bot_open_id
        name = name or ((self.bot_name or "机器人") if is_self else uid)
        # Generated identity text is not user-authored task text. In particular,
        # a display name containing $skill must not become a Skill reference.
        label = escape(name, quote=False).replace("$", "&#36;")
        attribute = (
            'target="self"' if is_self
            else f'user_id="{escape(open_id, quote=True)}"'
        )
        return self.token(
            f"<at {attribute}>{label}</at>", is_self=is_self, is_mention=True,
        )

    def placeholders(self, text: str) -> str:
        return _PLACEHOLDER.sub(
            lambda match: self.mention(match[0]) or match[0], text,
        )

    def post(self, value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                self.post(item)
        elif isinstance(value, dict):
            if value.get("tag") == "at":
                uid = value.get("user_id")
                if isinstance(uid, str):
                    marker = self.mention(uid, value.get("user_name"))
                    if marker is not None:
                        value.clear()
                        value.update(tag="text", text=marker)
            elif value.get("tag") == "md" and isinstance(value.get("text"), str):
                # The SDK flattener normally rewrites inline at-like text.
                # Shield it while rendering real AST at-nodes, then restore it
                # unchanged; this does not escape or authenticate user input.
                value["text"] = _LITERAL_AT.sub(
                    lambda match: self.token(match[0], is_literal=True), value["text"],
                )
            else:
                for item in value.values():
                    self.post(item)

    def token_pattern(self) -> re.Pattern[str]:
        return re.compile(re.escape(self.prefix) + r"\d+\x00")

    def consume_head(self, text: str, pattern: str) -> str | None:
        # The existing SDK may remove own mentions even *inside* a command.
        # Match its zero-width view, but delete only mapped command characters
        # from the source; all mention tokens retain their relative positions.
        for omit_literals in (False, True):
            # Post normalization can also omit a handwritten own-ID tag inside
            # the already parsed head. A second mapping view preserves that
            # literal unchanged; it does not turn it into a real self mention.
            positions: list[int] = []
            start = 0
            for match in self.token_pattern().finditer(text):
                token = self.tokens[match[0]]
                if token.is_self or (omit_literals and token.is_literal):
                    positions.extend(range(start, match.start()))
                    start = match.end()
            positions.extend(range(start, len(text)))
            view = "".join(text[index] for index in positions)
            head = re.match(r"\s*(" + pattern + ")", view, re.IGNORECASE)
            if head is not None:
                consumed = set(positions[head.start(1):head.end(1)])
                return "".join(char for index, char in enumerate(text) if index not in consumed)
        return None

    def expand(self, text: str) -> str:
        # One pass: neither names nor user-written tags are scanned again.
        return self.token_pattern().sub(lambda match: self.tokens[match[0]].text, text)


def project_task_text(
    message: Any,
    request_text: str,
    *,
    bot_open_id: str | None,
    bot_name: str | None = None,
    command: str | None = None,
    literal_slash: bool = False,
) -> str:
    """Render mentions in an already classified current task body.

    ``command`` names an already consumed Goal/Side head, not a new parser.
    Missing source evidence retains the caller's existing task text.
    """
    content = getattr(message, "content", None)
    mentions = _Mentions(message, bot_open_id, bot_name)
    if isinstance(content, TextContent):
        source = content.raw.get("text") if isinstance(content.raw, dict) else None
        if not isinstance(source, str):
            return request_text
        text = mentions.placeholders(source).strip()
    elif isinstance(content, PostContent) and content.post:
        post = deepcopy(content.post)
        mentions.post(post)
        text, _ = flatten_content(replace(content, post=post))
        text = mentions.placeholders(text).strip()
    else:
        return request_text
    if not any(t.is_mention and key in text for key, t in mentions.tokens.items()):
        return request_text
    if command is not None:
        projected = mentions.consume_head(text, r"/\s*" + re.escape(command) + r"(?:\s+|$)")
        if projected is None:
            return request_text
        text = projected
    elif literal_slash:
        projected = mentions.consume_head(text, r"/(?=/)")
        if projected is None:
            return request_text
        text = projected
    return mentions.expand(text).strip()
