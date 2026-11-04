"""Bounded native image content for a single latest-answer response.

Paths only match final Markdown references to the same Turn's generated images.
No filesystem, network, history cache or additional native request is involved.
"""
from __future__ import annotations

import base64
import json
from urllib.parse import unquote, urlsplit

from ..project_files import FileBrowseError, validate_image
from ..runtime.contracts import NativeLatestAnswer
from .file_previews import render_markdown


MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 20 * 1024 * 1024
MAX_ANSWER_IMAGES = 16
MAX_TOTAL_IMAGE_PIXELS = 32_000_000
MAX_IMAGE_DIMENSION = 16_384
MAX_IMAGE_RESPONSE_BYTES = 32 * 1024 * 1024


def _absolute_path(value: str) -> bool:
    return (
        0 < len(value) <= 4096
        and value.startswith("/") and not value.startswith("//")
        and "\\" not in value
        and not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value)
        and all(part not in {"", ".", ".."} for part in value.split("/")[1:])
    )


def _reference_path(value: str) -> str | None:
    try:
        parts = urlsplit(value)
        if parts.scheme or parts.netloc or parts.query or parts.fragment:
            return None
        path = unquote(parts.path, errors="strict")
        if unquote(path, errors="strict") != path or not _absolute_path(path):
            return None
        return path
    except (ValueError, UnicodeError):
        return None


def render_answer_preview(answer: NativeLatestAnswer) -> dict:
    """Validate only referenced images; failures preserve the complete text."""
    candidates: dict[str, str | None] = {}
    if answer.status == "completed":
        for image in answer.images:
            if _absolute_path(image.saved_path):
                # Multiple native items for one path are ambiguous, even when
                # their current bytes happen to agree. Never choose the last.
                candidates[image.saved_path] = None if image.saved_path in candidates else image.data
    selected: dict[str, str | None] = {}
    pixels: dict[str, int] = {}
    images: list[dict[str, str]] = []
    remaining = MAX_TOTAL_IMAGE_BYTES
    remaining_pixels = MAX_TOTAL_IMAGE_PIXELS

    def select(target: str) -> str | None:
        nonlocal remaining, remaining_pixels
        path = _reference_path(target)
        if path is None:
            return None
        if path in selected:
            if selected[path] is not None:
                if pixels[path] > remaining_pixels:
                    return None
                remaining_pixels -= pixels[path]
            return selected[path]
        selected[path] = None
        data = candidates.get(path)
        if not data or len(images) >= MAX_ANSWER_IMAGES:
            return None
        if len(data) % 4 or len(data) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
            return None
        size = len(data) // 4 * 3 - (2 if data.endswith("==") else 1 if data.endswith("=") else 0)
        if size > MAX_IMAGE_BYTES or size > remaining:
            return None
        # Charge attempted decoding, including invalid images, once per path.
        remaining -= size
        try:
            decoded = base64.b64decode(data, validate=True)
            if not decoded.startswith(b"\x89PNG\r\n\x1a\n") or not decoded.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82"):
                return None
            width, height = validate_image(
                decoded, "image/png", max_bytes=MAX_IMAGE_BYTES,
                max_pixels=MAX_TOTAL_IMAGE_PIXELS, max_dimension=MAX_IMAGE_DIMENSION,
            )
        except (ValueError, FileBrowseError):
            return None
        if width * height > remaining_pixels:
            return None
        # Count every rendered occurrence, even when Blob bytes are shared.
        pixels[path] = width * height
        remaining_pixels -= pixels[path]
        slot = f"image-{len(images)}"
        images.append({"id": slot, "mime": "image/png", "data": data})
        selected[path] = slot
        return slot

    html = render_markdown(answer.answer or "", answer_image=select, max_images=MAX_ANSWER_IMAGES)
    preview = {"markdownHtml": html, "images": images}
    # Include text plus HTML and leave room for the fixed identity/time envelope.
    # This budget does not truncate the pre-existing text-only response.
    # Validated base64 is ASCII without JSON escapes. Count it directly instead
    # of serializing/copying all image strings a second time just to measure.
    envelope = {"answer": answer.answer, "markdownHtml": html,
                "images": [{**image, "data": ""} for image in images]}
    response_size = len(json.dumps(envelope, ensure_ascii=True)) + sum(len(image["data"]) for image in images)
    if images and response_size + 4096 > MAX_IMAGE_RESPONSE_BYTES:
        return {
            "markdownHtml": render_markdown(answer.answer or "", answer_image=lambda _: None),
            "images": [],
            "previewMessage": "图片超过预览总量限制，已保留正文。",
        }
    return preview
