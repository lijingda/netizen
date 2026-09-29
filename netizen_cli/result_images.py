"""Best-effort previews of the local images explicitly referenced in a Result."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import stat
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import unquote, urlsplit

from lark_channel import MediaSource

from .markdown_images import image_tokens, parse_markdown, render_markdown, replace_image_with_text
from .turn_files import is_supported_image_header


_MAX_IMAGES = 20
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_TOTAL_BYTES = 50 * 1024 * 1024
_PREVIEW_TIMEOUT_SECONDS = 8.0
_UPLOAD_TIMEOUT_SECONDS = 4.0
_IMAGE_KEY = re.compile(r"img_[A-Za-z0-9_-]{1,256}\Z")
logger = logging.getLogger(__name__)


class ImageUploadChannel(Protocol):
    async def upload_media(self, source: MediaSource, *, kind: Literal["image"]) -> str: ...


async def prepare_result_images(
    channel: ImageUploadChannel,
    content: str,
    *,
    cwd: Path | None,
) -> str:
    """Read the referenced images independently of the Files module.

    Uploaded keys live only in the returned Result string and its existing
    self-contained page callbacks. Neither rendering nor pagination uploads.
    """
    if "![" not in content:
        return content
    tokens, env = parse_markdown(content)
    uploaded: dict[Path, str | None] = {}
    total_bytes = 0
    deadline = asyncio.get_running_loop().time() + _PREVIEW_TIMEOUT_SECONDS
    changed = False
    for token in image_tokens(tokens):
        target = token.attrGet("src") or ""
        if _IMAGE_KEY.fullmatch(target):
            continue
        parts = urlsplit(target)
        if parts.scheme not in {"", "file"} or parts.netloc:
            continue  # Remote/data images are not fetched or rewritten.
        changed = True
        path = _local_path(target, cwd)
        if path is not None:
            if path not in uploaded and len(uploaded) < _MAX_IMAGES:
                uploaded[path] = None
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining > 0 and total_bytes < _MAX_TOTAL_BYTES:
                    read_limit = min(_MAX_IMAGE_BYTES, _MAX_TOTAL_BYTES - total_bytes)
                    # Reserve before starting the worker: failed or timed-out reads
                    # may have consumed bytes even when no body reaches this task.
                    total_bytes += read_limit
                    try:
                        async with asyncio.timeout(min(remaining, _UPLOAD_TIMEOUT_SECONDS)):
                            body = await asyncio.to_thread(
                                _read_image, path, read_limit,
                            )
                            total_bytes -= read_limit - len(body)
                            key = await channel.upload_media(
                                MediaSource(kind="buffer", buffer=body), kind="image",
                            )
                            if not isinstance(key, str) or not _IMAGE_KEY.fullmatch(key):
                                raise ValueError("invalid uploaded image key")
                            uploaded[path] = key
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # Do not expose local paths or transport/credential details.
                        logger.warning("inline result image preview unavailable")
            key = uploaded.get(path)
            if key is not None:
                token.attrSet("src", key)
                continue
        replace_image_with_text(token, "预览暂不可用：图片不存在、无法读取或上传失败")
    return render_markdown(tokens, env) if changed else content


def _local_path(target: str, cwd: Path | None) -> Path | None:
    try:
        parsed = urlsplit(target)
        if parsed.scheme == "file" and not parsed.netloc and not parsed.query and not parsed.fragment:
            target = parsed.path
        elif parsed.scheme or parsed.netloc:
            return None
        path = Path(unquote(target, errors="strict")).expanduser()
        if not path.is_absolute():
            if cwd is None:
                return None
            path = cwd / path
        return path.resolve()
    except (OSError, RuntimeError, ValueError, UnicodeError):
        return None


def _read_image(path: Path, limit: int) -> bytes:
    # Nonblocking open rejects a file replaced by a FIFO without hanging a worker.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb", buffering=0) as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= limit:
            raise ValueError("image is unavailable or exceeds preview limits")
        body = stream.read(limit)
        if len(body) != metadata.st_size or os.fstat(stream.fileno()).st_size != metadata.st_size:
            raise ValueError("image changed while being read")
    if not is_supported_image_header(body[:12]):
        raise ValueError("unsupported or oversized image")
    return body
