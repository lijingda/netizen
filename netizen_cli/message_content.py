"""Shared content projection over the Channel SDK public normalized messages.

This module owns message-type and resource semantics. Message acquisition,
source identity, prompt intent, historical selection, and wire envelopes remain
with their callers.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from lark_channel import (
    MergeForwardContent,
    MergeForwardItem,
    PostContent,
    TextContent,
    flatten_content,
)
from lark_channel.channel.normalize import MentionExtraction, resolve_mentions


_FORWARD_ITEM_LIMIT = 50
_FORWARD_DEPTH_LIMIT = 3
_FORWARD_DEPTH_ERROR = (
    "合并转发嵌套超过最大深度 3，本条消息未执行；请拆分后重试。"
)
_FORWARD_TEXT_LIMIT = 16_000
_FORWARD_MEDIA_NOTICE = (
    "合并转发仅提供可见文字和附件描述；"
    "未读取图片像素、文件正文、音视频或表情内容。"
    "仅限 SDK 返回的展开内容，不代表原话题完整历史。"
)
_FORWARD_TRUNCATION_NOTICE = "合并转发内容已截断，部分消息或文字未提供。"
_CARD_NODE_LIMIT = 4_096
_CARD_DEPTH_LIMIT = 32
_CARD_TEXT_LIMIT = 256_000
_CARD_HIDDEN_FIELDS = frozenset(
    {
        "value", "confirm", "options", "behaviors", "events",
        "disabled_tips", "initial_option", "initial_value",
        "selected_values", "tooltip",
    }
)


TEXT_TYPES = frozenset({"text", "post"})
MATERIAL_MESSAGE_TYPES = frozenset({"interactive", "merge_forward"})
STRUCTURED_TYPES = frozenset(
    {
        "interactive",
        "calendar",
        "general_calendar",
        "share_calendar_event",
        "location",
        "video_chat",
        "todo",
        "vote",
        "hongbao",
    }
)
METADATA_TYPES = frozenset(
    {
        "image",
        "file",
        "folder",
        "audio",
        "media",
        "sticker",
        "share_chat",
        "share_user",
    }
)
SUPPORTED_MESSAGE_TYPES = (
    TEXT_TYPES | STRUCTURED_TYPES | METADATA_TYPES | {"merge_forward"}
)
PLACEHOLDER_TEXT = frozenset(
    {
        "",
        "[interactive]",
        "[unsupported message]",
        "<forwarded_messages/>",
    }
)


class HistoricalMessageError(RuntimeError):
    """A historical-message failure that is safe to show to the user."""


class HistoricalMessageUnavailable(HistoricalMessageError):
    """A selected message could not be projected faithfully."""


class UnsupportedHistoricalMessage(HistoricalMessageError):
    """A selected message has no supported historical representation."""


class HistoricalMessageContractError(HistoricalMessageError):
    """Public normalized fields contradict each other."""


def normalized_content_type(
    message: Any,
    *,
    default: str = "unknown",
    conflict_error: type[Exception] = HistoricalMessageContractError,
) -> str:
    """Resolve the SDK's typed/raw message kind with one shared conflict gate."""

    content = getattr(message, "content", None)
    kind = _nonempty_string(getattr(content, "kind", None))
    raw_kind = _nonempty_string(getattr(message, "raw_content_type", None))
    normalized_raw_kind = "media" if raw_kind == "video" else raw_kind
    if kind == "unknown" and normalized_raw_kind is not None:
        return normalized_raw_kind
    if (
        kind is not None
        and normalized_raw_kind is not None
        and kind != normalized_raw_kind
    ):
        raise conflict_error(
            "Channel SDK 返回了相互冲突的消息类型，"
            "本条消息未执行；请联系维护者检查 SDK 兼容性。"
        )
    return kind or normalized_raw_kind or default


def project_message_content(
    message: Any,
    *,
    message_type: str,
    interactive_fallback_text: str | None = None,
    read_image_keys: tuple[str, ...] = (),
    request_text: str | None = None,
) -> dict[str, Any]:
    """Project normalized content independently of source and wire envelope."""

    content = getattr(message, "content", None)
    if message_type == "merge_forward":
        return _project_merge_forward(message)
    read_image_key_set = frozenset(read_image_keys)
    if message_type == "interactive":
        _validate_interactive_content(content)
        read_image_key_set = frozenset()
    resources = _resource_metadata(message, read_image_keys=read_image_key_set)
    if message_type == "post" and read_image_keys:
        public_images = {
            item.get("file_key"): item
            for item in resources
            if item.get("type") == "image"
        }
        visible_images = [
            public_images.get(key)
            or _resource_item("image", file_key=key, content_read=True)
            for key in read_image_keys
        ]
        resources = visible_images + [
            item for item in resources if item.get("type") != "image"
        ]
    truncated = bool(getattr(content, "truncated", False))

    if message_type in TEXT_TYPES:
        if request_text is not None and not isinstance(request_text, str):
            raise TypeError("request_text must be a string")
        text = (
            request_text
            if request_text is not None
            else usable_text(getattr(message, "content_text", ""))
        )
        if text is None:
            raise HistoricalMessageUnavailable(
                "历史消息没有可读取的文本内容，请复制内容后重试。"
            )
        return {
            "text": text,
            "content_fidelity": (
                "full_multimodal"
                if resources and all(item["content_read"] for item in resources)
                else "partial"
                if resources
                else "full_text"
            ),
            "content_read": True,
            "content_metadata": {},
            "resources": resources,
            "truncated": truncated,
        }

    if message_type in STRUCTURED_TYPES:
        text = usable_text(getattr(message, "content_text", ""))
        if message_type == "interactive" and text is None:
            text = usable_text(interactive_fallback_text)
        if text is None:
            raise HistoricalMessageUnavailable(
                "历史应用消息没有可提取的可见内容，"
                "请复制内容后重试。"
            )
        return {
            "text": text,
            "content_fidelity": (
                "visible_text" if message_type == "interactive" else "structured_text"
            ),
            "content_read": True,
            "content_metadata": {},
            "resources": resources,
            "truncated": truncated,
        }

    text, content_metadata, intrinsic_resource = _metadata_projection(
        message_type,
        content,
        read_image_keys=read_image_key_set,
    )
    if intrinsic_resource is not None and _resource_signature(
        intrinsic_resource
    ) not in {_resource_signature(item) for item in resources}:
        resources.append(intrinsic_resource)
    image_read = message_type == "image" and any(
        item.get("type") == "image" and item.get("content_read") is True
        for item in resources
    )
    return {
        "text": text,
        "content_fidelity": "full_multimodal" if image_read else "metadata_only",
        "content_read": image_read,
        "content_metadata": content_metadata,
        "resources": resources,
        "truncated": truncated,
    }


def _project_merge_forward(message: Any) -> dict[str, Any]:
    """Bound the SDK's typed tree before using its public text converter.

    The SDK's item limit applies separately to every nested container and its
    converter can silently skip a malformed child. Validate retained leaves
    individually, then freeze their rendered text in copied SDK items so the
    final SDK layout cannot discard them. No resource here represents read
    bytes, regardless of an outer caller's image-read set.
    """

    content = getattr(message, "content", None)
    if not isinstance(content, MergeForwardContent):
        raise HistoricalMessageContractError(
            "合并转发缺少可验证的消息结构，本条消息未执行。"
        )
    _validate_forward_state(content)
    if not content.items:
        raise HistoricalMessageUnavailable(
            "合并转发消息没有可读取的内容，请复制内容后重试。"
        )
    visiting: set[int] = set()

    def validate_depth(node: MergeForwardContent, depth: int) -> None:
        # Check the returned tree before applying the output item budget: an
        # omitted branch must not hide an SDK/local depth-limit violation.
        if id(node) in visiting:
            raise HistoricalMessageContractError(
                "合并转发包含循环消息结构，本条消息未执行。"
            )
        if depth > _FORWARD_DEPTH_LIMIT or node.error == "max_depth_exceeded":
            raise HistoricalMessageUnavailable(_FORWARD_DEPTH_ERROR)
        if not isinstance(node.items, list):
            return  # Retained malformed containers are rejected by prepare.
        visiting.add(id(node))
        for item in node.items:
            child = getattr(item, "content", None)
            if isinstance(child, MergeForwardContent):
                validate_depth(child, depth + 1)
        visiting.remove(id(node))

    validate_depth(content, 0)
    remaining = _FORWARD_ITEM_LIMIT
    descriptors: list[Any] = []

    def prepare(node: MergeForwardContent) -> MergeForwardContent:
        nonlocal remaining
        _validate_forward_state(node)
        items: list[MergeForwardItem] = []
        truncated = bool(node.truncated)
        for item in node.items:
            if remaining == 0:
                truncated = True
                break
            remaining -= 1
            if not isinstance(item, MergeForwardItem) or item.content is None:
                raise HistoricalMessageUnavailable(
                    "合并转发包含无法读取的子消息，本条消息未执行。"
                )
            _validate_forward_item(item)
            child = item.content
            if isinstance(child, MergeForwardContent):
                prepared_child = prepare(child)
                if not prepared_child.items and not prepared_child.truncated:
                    raise HistoricalMessageUnavailable(
                        "合并转发包含未展开的子消息，本条消息未执行。"
                    )
                truncated |= prepared_child.truncated
            else:
                kind = getattr(child, "kind", None)
                if kind not in SUPPORTED_MESSAGE_TYPES:
                    raise UnsupportedHistoricalMessage(
                        "合并转发包含不支持的子消息类型，"
                        "请拆分后重试。本条消息未执行。"
                    )
                if kind == "interactive":
                    _validate_interactive_content(child)
                try:
                    if isinstance(child, PostContent):
                        displayed_post, child_resources = _forward_post_content(child)
                        child_text, _ = flatten_content(displayed_post)
                    else:
                        child_text, child_resources = flatten_content(child)
                except Exception as error:
                    raise HistoricalMessageUnavailable(
                        "合并转发子消息无法转换为可见内容，本条消息未执行。"
                    ) from error
                if usable_text(child_text) is None:
                    raise HistoricalMessageUnavailable(
                        "合并转发子消息没有可读取的内容，本条消息未执行。"
                    )
                # Metadata leaves use the existing source-neutral resource
                # description; generated file/image keys stay internal.
                if kind in METADATA_TYPES:
                    child_text, _, intrinsic = _metadata_projection(
                        kind, child, read_image_keys=frozenset()
                    )
                    if intrinsic is not None:
                        descriptors.append(intrinsic)
                descriptors.extend(child_resources)
                if len(child_text) > _FORWARD_TEXT_LIMIT:
                    child_text = child_text[:_FORWARD_TEXT_LIMIT]
                    truncated = True
                prepared_child = TextContent(text=child_text)
            items.append(replace(item, content=prepared_child))
        return replace(node, items=items, truncated=truncated)

    prepared = prepare(content)
    text, _ = flatten_content(prepared)
    text = resolve_mentions(
        text,
        MentionExtraction(mentions={
            mention.key: mention
            for mention in getattr(message, "mentions", ()) or ()
            if isinstance(getattr(mention, "key", None), str)
            and isinstance(getattr(mention, "name", None), str)
        }),
    )
    if usable_text(text) is None:
        raise HistoricalMessageUnavailable(
            "合并转发消息没有可读取的内容，请复制内容后重试。"
        )
    resources = _resource_descriptors_metadata(
        descriptors, read_image_keys=frozenset()
    )
    truncated = prepared.truncated

    suffix = "\n\n" + _FORWARD_MEDIA_NOTICE
    if len(text) + len(suffix) > _FORWARD_TEXT_LIMIT:
        truncated = True
    if truncated:
        suffix += "\n" + _FORWARD_TRUNCATION_NOTICE
    text = text[: _FORWARD_TEXT_LIMIT - len(suffix)] + suffix
    return {
        "text": text,
        "content_fidelity": "bounded_aggregate",
        "content_read": True,
        "content_metadata": {},
        "resources": resources,
        "truncated": bool(truncated),
    }


def _validate_forward_state(content: MergeForwardContent) -> None:
    if content.error == "max_depth_exceeded":
        raise HistoricalMessageUnavailable(_FORWARD_DEPTH_ERROR)
    if content.loading or content.error:
        raise HistoricalMessageUnavailable(
            "合并转发尚未完整读取，或其中消息读取失败，本条消息未执行；请重试。"
        )
    if not isinstance(content.items, list):
        raise HistoricalMessageContractError(
            "合并转发子消息结构无效，本条消息未执行。"
        )


def _forward_post_content(content: PostContent) -> tuple[PostContent, list[Any]]:
    """Mask only SDK-generated resource targets in a copied visible post AST.

    Match the pinned SDK's first-locale/content_v2 selection and top-level
    files zone. Markdown targets use the existing image-marker boundary;
    text, fenced code, titles, and other user-authored fields remain literal.
    Text and resource extraction share the same visible locale/body. The SDK
    reads resources only from ``content``, so expose the selected paragraphs
    there on a copy before collecting their original keys. Placeholder targets
    are display-only and can never be downloaded.
    """

    post = content.post
    if not isinstance(post, dict) or not post:
        return content, flatten_content(content)[1]
    # image_inputs depends on this module's shared type gate. Import its
    # existing marker helper only during preparation to avoid an import cycle
    # while preserving the same fenced-code semantics as native image input.
    from .image_inputs import localize_image_markers

    document = post if "content" in post else next(
        (value for value in post.values() if isinstance(value, dict)), None,
    )
    visible_post = dict(document) if document is not None else {}
    if document is not None:
        content_v2 = document.get("content_v2")
        visible_post["content"] = (
            content_v2
            if isinstance(content_v2, list) and content_v2
            else document.get("content") or []
        )
        visible_post.pop("content_v2", None)
    # Only the top-level attachment zone is visible, not a locale's files field.
    visible_post.pop("files", None)
    if "files" in post:
        visible_post["files"] = post["files"]
    visible_content = replace(content, post=visible_post)
    _, resources = flatten_content(visible_content)
    marker_refs = {
        ("current_message", "forward-post", resource.file_key): "unread-image"
        for resource in resources
        if getattr(resource, "type", None) == "image"
        and _nonempty_string(getattr(resource, "file_key", None)) is not None
    }
    replacement = dict(visible_post)
    changed = False
    if document is not None:
        paragraphs = visible_post.get("content")
        if isinstance(paragraphs, list):
            copied_paragraphs = []
            body_changed = False
            for paragraph in paragraphs:
                if not isinstance(paragraph, list):
                    copied_paragraphs.append(paragraph)
                    continue
                copied_nodes = []
                for node in paragraph:
                    copied = node
                    if isinstance(node, dict):
                        tag = node.get("tag")
                        if tag == "md" and isinstance(node.get("text"), str):
                            rendered = localize_image_markers(
                                node["text"],
                                source="current_message",
                                message_id="forward-post",
                                image_prompt_refs=marker_refs,
                            )
                            if rendered != node["text"]:
                                copied = {**node, "text": rendered}
                                body_changed = True
                        fields = (
                            ("image_key",)
                            if tag == "img"
                            else ("file_key", "image_key")
                            if tag == "media"
                            else ("file_key",)
                            if tag in ("audio", "file")
                            else ()
                        )
                        for field in fields:
                            if _nonempty_string(node.get(field)) is not None:
                                copied = dict(copied)
                                copied[field] = (
                                    "unread-image" if field == "image_key"
                                    else f"unread-{tag}"
                                )
                                body_changed = True
                    copied_nodes.append(copied)
                copied_paragraphs.append(copied_nodes)
            if body_changed:
                replacement["content"] = copied_paragraphs
                changed = True
    files = post.get("files")
    if isinstance(files, list):
        copied_files = []
        for item in files:
            if isinstance(item, dict) and _nonempty_string(item.get("file_key")):
                copied_files.append({
                    **item,
                    "file_key": (
                        "unread-folder" if item.get("is_folder") is True
                        else "unread-file"
                    ),
                })
                changed = True
            else:
                copied_files.append(item)
        if changed:
            replacement["files"] = copied_files
    return (
        replace(visible_content, post=replacement) if changed else visible_content,
        resources,
    )


def _validate_forward_item(item: MergeForwardItem) -> None:
    # The final SDK renderer isolates exceptions by silently dropping items.
    # Give it only the public dataclass's scalar shapes, including a bounded
    # timestamp; leaf conversion alone cannot prove its header will render.
    valid_sender = all(
        value is None or isinstance(value, str)
        for value in (item.sender_name, item.sender_open_id)
    )
    valid_time = item.create_time is None or (
        type(item.create_time) is int
        and 0 <= item.create_time <= 253_402_300_799_999
    )
    if not isinstance(item.message_id, str) or not valid_sender or not valid_time:
        raise HistoricalMessageContractError(
            "合并转发子消息的来源或时间格式无效，本条消息未执行。"
        )


def validate_interactive_version(content: Any) -> None:
    """Reject legacy cards, including v1 headers misclassified by SDK parsing."""

    card = getattr(content, "card", None)
    legacy_shape = (
        isinstance(card, dict)
        and "schema" not in card
        and "body" not in card
        and any(key in card for key in ("elements", "i18n_elements", "config", "card"))
    )
    if getattr(content, "card_version", None) == "v1" or legacy_shape:
        raise UnsupportedHistoricalMessage(
            "不支持飞书 1.0 卡片，请提供 2.0 卡片或复制可见正文。本条消息未执行。"
        )


def _validate_interactive_content(content: Any) -> None:
    """Reject the SDK's proven non-visible text leak without parsing cards.

    SDK 1.4.0 visits arbitrary dictionary values beneath header/body. A
    markdown/plain_text node inside an interaction payload therefore leaks
    into its public text. Normal scalar values and visible button labels are
    fine. This gate can be removed once the SDK excludes those payloads.
    """

    validate_interactive_version(content)
    card = getattr(content, "card", None)
    if not isinstance(card, dict):
        return
    header = card.get("header")
    roots = [card.get("body")]
    if isinstance(header, dict):
        roots.extend((header.get("title"), header.get("subtitle")))
    stack = [(node, 0, False) for node in roots]
    scheduled = len(stack)
    text_size = 0
    while stack:
        node, depth, hidden = stack.pop()
        if depth > _CARD_DEPTH_LIMIT:
            raise HistoricalMessageUnavailable(
                "应用卡片结构超过可验证范围，本条消息未执行。"
            )
        if isinstance(node, str):
            text_size += len(node)
        elif isinstance(node, dict):
            tag = node.get("tag")
            if tag == "plain_text" or tag == "markdown":
                text = node.get("content")
                if hidden and text:
                    raise HistoricalMessageUnavailable(
                        "应用卡片的 SDK 转换混入了非可见交互内容，"
                        "请复制可见正文后重试。本条消息未执行。"
                    )
                # The SDK stops traversal at a recognized text node too.
                if isinstance(text, str):
                    text_size += len(text)
            else:
                for key, value in node.items():
                    scheduled += 1
                    if scheduled > _CARD_NODE_LIMIT:
                        raise HistoricalMessageUnavailable(
                            "应用卡片结构超过可验证范围，本条消息未执行。"
                        )
                    stack.append(
                        (value, depth + 1, hidden or key in _CARD_HIDDEN_FIELDS)
                    )
        elif isinstance(node, list):
            scheduled += len(node)
            if scheduled > _CARD_NODE_LIMIT:
                raise HistoricalMessageUnavailable(
                    "应用卡片结构超过可验证范围，本条消息未执行。"
                )
            stack.extend((value, depth + 1, hidden) for value in node)
        if text_size > _CARD_TEXT_LIMIT:
            raise HistoricalMessageUnavailable(
                "应用卡片内容超过可验证范围，本条消息未执行。"
            )


def _metadata_projection(
    message_type: str,
    content: Any,
    *,
    read_image_keys: frozenset[str],
) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
    if message_type == "image":
        key = _nonempty_string(getattr(content, "image_key", None))
        image_read = key is not None and key in read_image_keys
        resource = _resource_item("image", file_key=key, content_read=image_read)
        text = (
            "引用了一张图片；图片像素已作为原生视觉输入提供。"
            if image_read
            else "引用了一张图片；当前版本未读取图片像素内容。"
        )
        return text, {}, resource
    if message_type == "file":
        name = _nonempty_string(getattr(content, "file_name", None))
        key = _nonempty_string(getattr(content, "file_key", None))
        suffix = f"，文件名：{name}" if name else ""
        return (
            f"引用了一个文件{suffix}；当前版本未读取文件正文。",
            {},
            _resource_item("file", file_key=key, file_name=name),
        )
    if message_type == "folder":
        name = _nonempty_string(getattr(content, "file_name", None))
        key = _nonempty_string(getattr(content, "file_key", None))
        suffix = f"，文件夹名：{name}" if name else ""
        return (
            f"引用了一个文件夹{suffix}；当前版本未读取其中内容。",
            {},
            _resource_item("folder", file_key=key, file_name=name),
        )
    if message_type == "audio":
        key = _nonempty_string(getattr(content, "file_key", None))
        duration = _positive_int(getattr(content, "duration_ms", None))
        suffix = f"，时长 {duration}ms" if duration is not None else ""
        return (
            f"引用了一段语音{suffix}；当前版本未转写音频。",
            {},
            _resource_item("audio", file_key=key, duration_ms=duration),
        )
    if message_type == "media":
        key = _nonempty_string(getattr(content, "file_key", None))
        cover = _nonempty_string(getattr(content, "image_key", None))
        name = _nonempty_string(getattr(content, "file_name", None))
        duration = _positive_int(getattr(content, "duration_ms", None))
        details = []
        if name:
            details.append(f"文件名：{name}")
        if duration is not None:
            details.append(f"时长 {duration}ms")
        suffix = f"，{'，'.join(details)}" if details else ""
        return (
            f"引用了一个视频{suffix}；当前版本未读取视频内容。",
            {},
            _resource_item(
                "video",
                file_key=key,
                file_name=name,
                duration_ms=duration,
                cover_image_key=cover,
            ),
        )
    if message_type == "sticker":
        key = _nonempty_string(getattr(content, "file_key", None))
        return (
            "引用了一个表情包；当前版本未读取表情内容。",
            {},
            _resource_item("sticker", file_key=key),
        )
    if message_type == "share_chat":
        chat_id = _nonempty_string(getattr(content, "chat_id", None))
        return (
            "引用了一个群名片；未额外读取群信息。",
            {"chat_id": chat_id},
            None,
        )
    if message_type == "share_user":
        user_id = _nonempty_string(getattr(content, "user_id", None))
        return (
            "引用了一个个人名片；未额外读取联系人信息。",
            {"user_id": user_id},
            None,
        )
    raise UnsupportedHistoricalMessage(
        f"暂不支持这种历史消息类型（{message_type}），"
        "本条消息未执行。"
    )


def _resource_metadata(
    message: Any,
    *,
    read_image_keys: frozenset[str],
) -> list[dict[str, Any]]:
    return _resource_descriptors_metadata(
        getattr(message, "resources", None) or (),
        read_image_keys=read_image_keys,
    )


def _resource_descriptors_metadata(
    descriptors: Any,
    *,
    read_image_keys: frozenset[str],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for resource in descriptors:
        if isinstance(resource, dict):
            item = dict(resource)
            signature = _resource_signature(item)
            if signature not in seen:
                seen.add(signature)
                result.append(item)
            continue
        resource_type = _nonempty_string(getattr(resource, "type", None))
        if resource_type is None:
            continue
        item = _resource_item(
            resource_type,
            file_key=_nonempty_string(getattr(resource, "file_key", None)),
            file_name=_nonempty_string(getattr(resource, "file_name", None)),
            duration_ms=_positive_int(getattr(resource, "duration_ms", None)),
            cover_image_key=_nonempty_string(
                getattr(resource, "cover_image_key", None)
            ),
            content_read=(
                resource_type == "image"
                and _nonempty_string(getattr(resource, "file_key", None))
                in read_image_keys
            ),
        )
        signature = _resource_signature(item)
        if signature in seen:
            continue
        seen.add(signature)
        result.append(item)
    return result


def _resource_item(
    resource_type: str,
    *,
    file_key: str | None = None,
    file_name: str | None = None,
    duration_ms: int | None = None,
    cover_image_key: str | None = None,
    content_read: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {"type": resource_type, "content_read": content_read}
    if file_key is not None:
        result["file_key"] = file_key
    if file_name is not None:
        result["file_name"] = file_name
    if duration_ms is not None:
        result["duration_ms"] = duration_ms
    if cover_image_key is not None:
        result["cover_image_key"] = cover_image_key
    return result


def _resource_signature(resource: dict[str, Any]) -> tuple[Any, ...]:
    return (
        resource.get("type"),
        resource.get("file_key"),
        resource.get("file_name"),
        resource.get("duration_ms"),
        resource.get("cover_image_key"),
    )


def usable_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return None if text in PLACEHOLDER_TEXT else text


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None


def _nonempty_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
