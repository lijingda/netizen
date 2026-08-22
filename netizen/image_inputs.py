"""Bounded Feishu image resources projected as native Codex image inputs.

Only ordinary image messages and images embedded in ``post`` messages enter
this boundary.  Card, merge-forward, file, audio, video, and sticker resources
remain outside the Pilot image scope.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from openai_codex import ImageInput, TextInput


_SUPPORTED_MESSAGE_TYPES = frozenset({"image", "post"})
_IMAGE_MARKER_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
_IMAGE_FETCH_TIMEOUT_SECONDS = 10.0
_IMAGE_TOTAL_FETCH_TIMEOUT_SECONDS = 60.0
_IMAGE_MAX_COUNT = 20
_IMAGE_MAX_BYTES = 20 * 1024 * 1024
_IMAGE_TOTAL_MAX_BYTES = 50 * 1024 * 1024


class ImageInputError(RuntimeError):
    """An image-specific failure that is safe to show to the user."""


class UnsupportedPromptMedia(ImageInputError):
    pass


class ImageInputUnavailable(ImageInputError):
    pass


class ImageInputContractError(ImageInputError):
    pass


ImageSource = Literal["current_message", "quoted_message"]


@dataclass(frozen=True, slots=True)
class ImageReference:
    source: ImageSource
    message_id: str
    file_key: str


@dataclass(frozen=True, slots=True)
class PreparedImage:
    reference: ImageReference
    mime_type: str
    size_bytes: int
    data_url: str


class ImageDownloadChannel(Protocol):
    async def download_resource(
        self,
        file_key: str,
        resource_type: str = "image",
        message_id: str | None = None,
    ) -> bytes | None: ...


def current_message_image_references(message: Any) -> tuple[ImageReference, ...]:
    """Validate current prompt media and return its ordinary image refs.

    Current messages stay strict: text cannot carry resources, and image/post
    prompts cannot silently drop non-image resources.  Quoted messages retain
    their existing per-type projection and call :func:`image_references` only
    to add supported pixels.
    """

    message_type = normalized_message_type(message)
    resources = tuple(getattr(message, "resources", None) or ())
    if message_type == "text":
        if resources:
            raise UnsupportedPromptMedia(
                "当前消息包含暂不支持的附件；目前仅支持普通图片和富文本图片。"
            )
        return ()
    if message_type not in _SUPPORTED_MESSAGE_TYPES:
        raise UnsupportedPromptMedia(
            f"暂不支持这种消息类型（{message_type or 'unknown'}）；"
            "目前仅支持文本、普通图片和富文本图片。"
        )
    if message_type == "post":
        elements = _selected_post_elements(message)
        visible_types = _post_visible_resource_types(elements or ())
        has_unsupported_visible_resource = any(
            resource_type != "image" for resource_type in visible_types
        )
        # A missing typed post AST is a contract gap, so retain descriptors as
        # a conservative fallback for rejecting unsupported attachments.  If
        # the AST exists, it is authoritative for the same document/version
        # that the SDK rendered; descriptors can belong to a hidden locale or
        # the unselected content-v1 variant in SDK 1.2.0.
        has_unsupported_fallback_resource = elements is None and any(
            _resource_type(resource) != "image" for resource in resources
        )
        if has_unsupported_visible_resource or has_unsupported_fallback_resource:
            raise UnsupportedPromptMedia(
                "当前消息还包含暂不支持的附件；目前仅支持普通图片和富文本图片。"
            )
    elif any(_resource_type(resource) != "image" for resource in resources):
        raise UnsupportedPromptMedia(
            "当前消息还包含暂不支持的附件；目前仅支持普通图片和富文本图片。"
        )
    return image_references(message, source="current_message")


def image_references(
    message: Any,
    *,
    source: ImageSource,
) -> tuple[ImageReference, ...]:
    """Return ordered, deduplicated image refs for ``image``/``post`` only."""

    message_type = normalized_message_type(message)
    if message_type not in _SUPPORTED_MESSAGE_TYPES:
        return ()
    message_id = _message_id(message)
    if not message_id:
        raise ImageInputContractError(
            "图片消息缺少可验证的消息 ID，本条消息未执行。"
        )

    resource_keys: list[str] = []
    resource_seen: set[str] = set()
    for resource in getattr(message, "resources", None) or ():
        if _resource_type(resource) != "image":
            continue
        key = _nonempty_string(getattr(resource, "file_key", None))
        if key is not None and key not in resource_seen:
            resource_seen.add(key)
            resource_keys.append(key)

    if message_type == "post":
        # The typed PostContent AST is the only reliable way to distinguish a
        # real image node from a literal Markdown-looking string in a text or
        # code node.  Match SDK 1.2.0's public rendering choice: first locale,
        # non-empty content_v2 before content.  This also avoids descriptors
        # from hidden locales or an unselected content-v1 variant.
        elements = _selected_post_elements(message)
        if elements is None:
            if not resource_keys:
                return ()
            raise ImageInputContractError(
                "Channel SDK 没有提供可验证的富文本结构，"
                "本条消息未执行；请联系维护者检查 SDK 兼容性。"
            )
        keys = _deduplicated(_post_image_keys(elements))
        return tuple(
            ImageReference(
                source=source,
                message_id=message_id,
                file_key=key,
            )
            for key in keys
        )

    if message_type == "image":
        content = getattr(message, "content", None)
        intrinsic_key = _nonempty_string(getattr(content, "image_key", None))
        if intrinsic_key is not None:
            if any(key != intrinsic_key for key in resource_keys):
                raise ImageInputContractError(
                    "Channel SDK 返回了相互冲突的普通图片资源，"
                    "本条消息未执行；请联系维护者检查 SDK 兼容性。"
                )
            keys = [intrinsic_key]
        else:
            keys = list(resource_keys)
            if len(keys) > 1:
                raise ImageInputContractError(
                    "普通图片消息包含多个相互冲突的资源，"
                    "本条消息未执行；请联系维护者检查 SDK 兼容性。"
                )
        if not keys:
            raise ImageInputContractError(
                "普通图片消息没有可验证的图片资源，本条消息未执行。"
            )

    return tuple(
        ImageReference(source=source, message_id=message_id, file_key=key)
        for key in keys
    )


async def prepare_images(
    channel: ImageDownloadChannel,
    references: tuple[ImageReference, ...],
    *,
    max_count: int = _IMAGE_MAX_COUNT,
    max_image_bytes: int = _IMAGE_MAX_BYTES,
    max_total_bytes: int = _IMAGE_TOTAL_MAX_BYTES,
    fetch_timeout_seconds: float = _IMAGE_FETCH_TIMEOUT_SECONDS,
    total_timeout_seconds: float = _IMAGE_TOTAL_FETCH_TIMEOUT_SECONDS,
) -> tuple[PreparedImage, ...]:
    """Download and validate images sequentially before native submission."""

    if not references:
        return ()
    if min(
        max_count,
        max_image_bytes,
        max_total_bytes,
    ) <= 0 or min(fetch_timeout_seconds, total_timeout_seconds) <= 0:
        raise ValueError("image preparation limits must be positive")
    if len(references) > max_count:
        raise ImageInputUnavailable(
            f"本条消息共包含 {len(references)} 张图片，超过当前 {max_count} 张上限；"
            "请拆分后重试。本条消息未执行。"
        )

    async def prepare_all() -> tuple[PreparedImage, ...]:
        prepared: list[PreparedImage] = []
        total_bytes = 0
        for reference in references:
            try:
                async with asyncio.timeout(fetch_timeout_seconds):
                    body = await channel.download_resource(
                        reference.file_key,
                        resource_type="image",
                        message_id=reference.message_id,
                    )
            except TimeoutError as error:
                raise ImageInputUnavailable(
                    "读取图片超时，本条消息未执行；请重新发送。"
                ) from error
            except Exception as error:
                raise ImageInputUnavailable(
                    "无法读取消息中的图片；它可能已被删除、设为保密，"
                    "或应用缺少消息读取权限。本条消息未执行。"
                ) from error
            if not isinstance(body, bytes) or not body:
                raise ImageInputUnavailable(
                    "无法读取消息中的图片；它可能已被删除、设为保密，"
                    "或应用缺少消息读取权限。本条消息未执行。"
                )
            size = len(body)
            if size > max_image_bytes:
                raise ImageInputUnavailable(
                    f"图片大小超过当前 {max_image_bytes // (1024 * 1024)} MB 上限，"
                    "请压缩后重试。本条消息未执行。"
                )
            total_bytes += size
            if total_bytes > max_total_bytes:
                raise ImageInputUnavailable(
                    f"图片总大小超过当前 {max_total_bytes // (1024 * 1024)} MB 上限，"
                    "请拆分后重试。本条消息未执行。"
                )
            mime_type = _sniff_image_mime_type(body)
            if mime_type is None:
                raise ImageInputUnavailable(
                    "图片格式不受支持；目前支持 PNG、JPEG、GIF 和 WebP。"
                    "本条消息未执行。"
                )
            data = base64.b64encode(body).decode("ascii")
            prepared.append(
                PreparedImage(
                    reference=reference,
                    mime_type=mime_type,
                    size_bytes=size,
                    data_url=f"data:{mime_type};base64,{data}",
                )
            )
            del body, data
        return tuple(prepared)

    try:
        async with asyncio.timeout(total_timeout_seconds):
            return await prepare_all()
    except TimeoutError as error:
        raise ImageInputUnavailable(
            "读取本条消息的全部图片超时，本条消息未执行；请拆分后重试。"
        ) from error


def compose_multimodal_input(
    prompt_text: str,
    *,
    images: tuple[PreparedImage, ...],
) -> str | list[Any]:
    """Place labeled native images before one complete final text prompt."""

    if not images:
        return prompt_text
    counts = {
        source: sum(image.reference.source == source for image in images)
        for source in ("quoted_message", "current_message")
    }
    indexes = {"quoted_message": 0, "current_message": 0}
    items: list[Any] = []
    for image in images:
        reference = image.reference
        indexes[reference.source] += 1
        label = {
            "kind": "feishu_image_input",
            "source": reference.source,
            "message_id": reference.message_id,
            "file_key": reference.file_key,
            "index": indexes[reference.source],
            "count": counts[reference.source],
            "mime_type": image.mime_type,
            "size_bytes": image.size_bytes,
        }
        items.append(TextInput(json.dumps(label, ensure_ascii=False)))
        items.append(ImageInput(image.data_url))
    items.append(TextInput(prompt_text))
    return items


def normalized_message_type(message: Any) -> str:
    content = getattr(message, "content", None)
    kind = _nonempty_string(getattr(content, "kind", None))
    raw_kind = _nonempty_string(getattr(message, "raw_content_type", None))
    normalized_raw_kind = "media" if raw_kind == "video" else raw_kind
    if kind == "unknown" and raw_kind is not None:
        return normalized_raw_kind or raw_kind
    if (
        kind is not None
        and normalized_raw_kind is not None
        and kind != normalized_raw_kind
    ):
        raise ImageInputContractError(
            "Channel SDK 返回了相互冲突的消息类型，本条消息未执行；"
            "请联系维护者检查 SDK 兼容性。"
        )
    return kind or normalized_raw_kind or "text"


def _message_id(message: Any) -> str:
    return str(getattr(message, "message_id", None) or getattr(message, "id", ""))


def _resource_type(resource: Any) -> str | None:
    return _nonempty_string(getattr(resource, "type", None))


def _selected_post_elements(message: Any) -> tuple[dict[str, Any], ...] | None:
    content = getattr(message, "content", None)
    post = getattr(content, "post", None)
    if not isinstance(post, dict) or not post:
        return None
    if "content" in post:
        documents = (post,)
    else:
        documents = tuple(value for value in post.values() if isinstance(value, dict))
    if not documents:
        return None
    document = documents[0]
    content_v2 = document.get("content_v2")
    paragraphs = (
        content_v2
        if isinstance(content_v2, list) and content_v2
        else document.get("content")
    )
    if not isinstance(paragraphs, list):
        return ()
    return tuple(
        element
        for paragraph in paragraphs
        if isinstance(paragraph, list)
        for element in paragraph
        if isinstance(element, dict)
    )


def _post_image_keys(elements: tuple[dict[str, Any], ...]) -> tuple[str, ...]:
    keys: list[str] = []
    for element in elements:
        tag = element.get("tag")
        if tag == "img":
            key = _nonempty_string(element.get("image_key"))
            if key is not None:
                keys.append(key)
        elif tag == "md":
            keys.extend(_markdown_image_keys_outside_fences(element.get("text")))
    return tuple(keys)


def _post_visible_resource_types(
    elements: tuple[dict[str, Any], ...],
) -> tuple[str, ...]:
    result: list[str] = []
    for element in elements:
        tag = element.get("tag")
        if tag == "img":
            result.append("image")
        elif tag == "media":
            result.append("video")
        elif tag in {"audio", "file"}:
            result.append(str(tag))
        elif tag == "md" and _markdown_image_keys_outside_fences(element.get("text")):
            result.append("image")
    return tuple(result)


def _markdown_image_keys_outside_fences(value: Any) -> tuple[str, ...]:
    if not isinstance(value, str):
        return ()
    parts = value.split("```")
    keys: list[str] = []
    for index, part in enumerate(parts):
        inside_fence = index % 2 == 1
        if inside_fence:
            continue
        keys.extend(
            key.strip()
            for key in _IMAGE_MARKER_RE.findall(part)
            if key.strip().startswith("img_")
        )
    return tuple(keys)


def _deduplicated(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _sniff_image_mime_type(body: bytes) -> str | None:
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if body.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if body.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(body) >= 12 and body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    return None


def _nonempty_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
