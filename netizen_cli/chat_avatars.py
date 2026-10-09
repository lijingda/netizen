"""Optional group-avatar presentation using public Channel media uploads.

Only image keys are retained, in bounded process-local memory. Group discovery,
membership and destination identity always come from the live chat directory.
"""

from __future__ import annotations

import asyncio
import re
from collections import OrderedDict
from collections.abc import Sequence
from time import monotonic
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from lark_channel import MediaSource

from .channel.ports import ReplyChannel

if TYPE_CHECKING:
    from .management.chat_directory import AvailableChat


# Exact CDN hosts from the official chat list/get and chooseChat examples, not
# an exhaustive platform catalog. Unknown hosts degrade to the group icon.
# Match the public Channel upload allowlist, URL filter and browser CSP; do not
# rely on SDK versions accepting hosts absent from their configured allowlist.
CHAT_AVATAR_HOSTS = (
    "p3-lark-file.byteimg.com",
    "s1-imfile.feishucdn.com",
    "s3-imfile.feishucdn.com",
)
CHAT_AVATAR_IMAGE_SOURCES = " ".join(f"https://{host}" for host in CHAT_AVATAR_HOSTS)
_IMAGE_KEY = re.compile(r"img_[A-Za-z0-9_-]{1,256}\Z")
_MAX_KEYS = 128
_KEY_SECONDS = 600.0
_PREPARE_SECONDS = 4.0


def avatar_url(value: object) -> str | None:
    """Discard malformed/unsupported optional images without losing the chat."""
    if not isinstance(value, str) or not value or len(value) > 4_096:
        return None
    if any(ord(char) <= 32 or ord(char) == 127 for char in value) or "\\" in value:
        return None
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        if (
            parts.scheme != "https" or parts.username is not None or parts.password is not None
            or parts.port not in (None, 443) or parts.fragment
            or host not in CHAT_AVATAR_HOSTS
        ):
            return None
    except ValueError:
        return None
    return value


class ChatAvatarImages:
    """Prepare at most one directory page; timeout/failure only loses icons."""

    def __init__(self, channel: ReplyChannel, *, prepare_seconds: float = _PREPARE_SECONDS) -> None:
        self._channel = channel
        self._prepare_seconds = prepare_seconds
        self._calls = asyncio.Semaphore(4)
        self._keys: OrderedDict[str, tuple[float, str]] = OrderedDict()

    def _cached(self, url: str) -> str | None:
        cached = self._keys.get(url)
        if cached is None:
            return None
        if cached[0] <= monotonic():
            del self._keys[url]
            return None
        self._keys.move_to_end(url)
        return cached[1]

    async def _prepare_url(self, url: str) -> None:
        async with self._calls:
            if self._cached(url) is not None:
                return
            try:
                key = await self._channel.upload_media(MediaSource(kind="url", url=url), kind="image")
                if not isinstance(key, str) or not _IMAGE_KEY.fullmatch(key):
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                return
            self._keys[url] = (monotonic() + _KEY_SECONDS, key)
            self._keys.move_to_end(url)
            while len(self._keys) > _MAX_KEYS:
                self._keys.popitem(last=False)

    async def prepare(self, chats: Sequence[AvailableChat]) -> dict[str, str]:
        urls = {chat.chat_id: url for chat in chats[:20] if (url := avatar_url(chat.avatar_url))}
        missing = dict.fromkeys(url for url in urls.values() if self._cached(url) is None)
        if missing:
            # All work belongs to this request, including cancellation. No
            # retained background jobs or card/session state survive a redraw.
            try:
                async with asyncio.timeout(self._prepare_seconds):
                    await asyncio.gather(*(self._prepare_url(url) for url in missing))
            except TimeoutError:
                pass
        return {chat_id: key for chat_id, url in urls.items() if (key := self._cached(url))}
