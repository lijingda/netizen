"""Bounded text-field projection of original Feishu cards, without rendering.

Only documented text/containment positions are visited. Interaction payloads
and unknown components are never flattened as arbitrary JSON. Replace this
narrow adapter when the public Channel SDK supplies the same safe projection.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


_RAW_LIMIT = 256_000
_NODE_LIMIT = 4_096
_DEPTH_LIMIT = 32
_TEXT_LIMIT = 64_000
_LOCALE = re.compile(r"(?:^|\s)LarkLocale/([a-z]{2,3}[_-][a-z]{2,4})(?=\s|$)", re.I)
_FALLBACK_LOCALES = ("zh_cn", "en_us", "ja_jp")
_TEXT_NOTICE = (
    "卡片仅提供支持组件的文本字段（富文本保留源码）；未渲染，未读取图片、音视频或交互状态，"
    "不保证与客户端显示一致。可按需通过飞书工具核对来源。"
)


class CardContentError(RuntimeError):
    """The original card cannot be projected within the supported boundaries."""


@dataclass(frozen=True, slots=True)
class PreparedCardContent:
    """Text availability and known omissions, never rendering completeness."""

    text: str
    truncated: bool = False
    content_read: bool = True


def _bounded(value: Any) -> None:
    stack = [(value, 0)]
    scheduled = 1
    size = 0
    while stack:
        node, depth = stack.pop()
        if depth > _DEPTH_LIMIT:
            raise CardContentError("应用卡片结构超过可验证范围，本条消息未执行。")
        if isinstance(node, str):
            size += len(node)
        elif isinstance(node, (dict, list)):
            scheduled += len(node)
            if scheduled > _NODE_LIMIT:
                raise CardContentError("应用卡片结构超过可验证范围，本条消息未执行。")
            if isinstance(node, dict):
                size += sum(len(key) for key in node if isinstance(key, str))
            children = node.values() if isinstance(node, dict) else node
            stack.extend((child, depth + 1) for child in children)
        if size > _RAW_LIMIT:
            raise CardContentError("应用卡片内容超过可验证范围，本条消息未执行。")


def _decode(value: Any) -> Any:
    if isinstance(value, str):
        if len(value) > _RAW_LIMIT:
            raise CardContentError("应用卡片内容超过可验证范围，本条消息未执行。")
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            return None
    return value


def original_card(value: Any) -> dict[str, Any] | None:
    """Recognize original structure; full-tree budgets apply at extraction."""
    value = _decode(value)
    if not isinstance(value, dict):
        return None
    if "user_dsl" in value:
        value = _decode(value["user_dsl"])
        if not isinstance(value, dict):
            return None
    schema = value.get("schema")
    if schema == "2.0":
        body = value.get("body", {})
        return value if isinstance(body, dict) and isinstance(body.get("elements", []), list) else None
    if schema not in (None, "1.0") or "body" in value or isinstance(value.get("title"), str):
        return None
    elements = value.get("elements")
    if "elements" in value:
        if isinstance(elements, list) and len(elements) > _NODE_LIMIT:
            raise CardContentError("应用卡片结构超过可验证范围，本条消息未执行。")
        return value if isinstance(elements, list) and all(
            isinstance(element, dict) and isinstance(element.get("tag"), str) for element in elements
        ) else None
    if "i18n_elements" in value:
        variants = value["i18n_elements"]
        if not isinstance(variants, dict):
            return None
        count = len(variants)
        if count > _NODE_LIMIT:
            raise CardContentError("应用卡片结构超过可验证范围，本条消息未执行。")
        for items in variants.values():
            if not isinstance(items, list):
                return None
            count += len(items)
            if count > _NODE_LIMIT:
                raise CardContentError("应用卡片结构超过可验证范围，本条消息未执行。")
            if not all(isinstance(item, dict) for item in items):
                return None
        return value
    header = value.get("header")
    return value if isinstance(header, dict) and isinstance(header.get("title"), dict) else None


def original_message_card(message: Any) -> dict[str, Any] | None:
    content = getattr(message, "content", None)
    raw = getattr(message, "raw", None)
    if isinstance(raw, dict) and isinstance(raw.get("message"), dict):
        raw = raw["message"]
    raw_body = raw.get("body") if isinstance(raw, dict) else None
    for candidate in (
        getattr(content, "card", None), getattr(content, "raw", None),
        raw.get("content") if isinstance(raw, dict) else None,
        raw_body.get("content") if isinstance(raw_body, dict) else None,
    ):
        card = original_card(candidate)
        if card is not None:
            return card
    return None


def request_locale(message: Any) -> str | None:
    raw = getattr(message, "raw", None)
    if isinstance(raw, dict) and isinstance(raw.get("message"), dict):
        raw = raw["message"]
    ua = raw.get("user_agent") if isinstance(raw, dict) else None
    match = _LOCALE.search(ua) if isinstance(ua, str) else None
    return match[1].lower().replace("-", "_") if match else None


def _nonempty(value: Any) -> str:
    return value if isinstance(value, str) and value.strip() else ""


def _variants(value: Any) -> dict[str, Any]:
    return {key.lower().replace("-", "_"): item for key, item in value.items() if isinstance(key, str)} if isinstance(value, dict) else {}


def _choose(variants: dict[str, Any], locale: str | None, *, allow_empty_list: bool = False) -> Any:
    for key in (locale, *_FALLBACK_LOCALES, *sorted(variants)):
        candidate = variants.get(key) if key else None
        if (allow_empty_list and isinstance(candidate, list)) or (candidate and (not isinstance(candidate, str) or candidate.strip())):
            return candidate
    return None


class _TextProjection:
    def __init__(self, locale: str | None) -> None:
        self.locale = locale.lower().replace("-", "_") if locale else None
        self.parts: list[str] = []
        self.size = 0
        self.readable = 0
        self.unread = False

    def localized(self, value: Any) -> str:
        if isinstance(value, str):
            return _nonempty(value)
        if not isinstance(value, dict):
            return ""
        translations = {
            key: text for key, item in (_variants(value.get("i18n")) | _variants(value.get("i18n_content"))).items()
            if (text := _nonempty(item))
        }
        matched = _nonempty(translations.get(self.locale)) if self.locale else ""
        return matched or _nonempty(value.get("content")) or _nonempty(_choose(translations, None))

    def add(self, value: Any, *, unread: bool = False) -> None:
        text = _nonempty(value)
        if not text:
            return
        self.size += len(text) + 1
        if self.size > _TEXT_LIMIT:
            raise CardContentError("应用卡片可见文字超过可验证范围，本条消息未执行。")
        self.parts.append(text)
        self.unread |= unread
        self.readable += not unread

    def text(self, value: Any) -> None:
        self.add(self.localized(value))

    def elements(self, values: Any) -> None:
        if isinstance(values, list):
            for value in values:
                self.element(value)
        elif values is not None:
            self.add("[未提取的卡片内容]", unread=True)

    def element(self, node: Any) -> None:
        if not isinstance(node, dict):
            self.add("[未提取的卡片组件]", unread=True)
            return
        tag = node.get("tag")
        if not isinstance(tag, str):
            self.add("[未提取的卡片组件]", unread=True)
            return
        if tag in {"plain_text", "lark_md", "markdown"}:
            text = self.localized(node)
            self.add(text or "[未提取到文本]", unread=not bool(text))
        elif tag == "div":
            self.text(node.get("text"))
            fields = node.get("fields", [])
            if isinstance(fields, list):
                for field in fields:
                    if isinstance(field, dict):
                        self.text(field.get("text"))
                    else:
                        self.add("[未提取的文本字段]", unread=True)
            else:
                self.add("[未提取的文本字段]", unread=True)
            if node.get("extra") is not None:
                self.element(node["extra"])
        elif tag == "column_set":
            self.elements(node.get("columns"))
        elif tag == "collapsible_panel":
            header = node.get("header")
            if isinstance(header, dict):
                self.text(header.get("title"))
            self.elements(node.get("elements"))
        elif tag in {"action", "actions"}:
            self.elements(node.get("actions"))
        elif tag in {"column", "note", "form", "interactive_container"}:
            self.elements(node.get("elements"))
        elif tag in {"button", "text_tag"}:
            self.text(node.get("text"))
        elif tag == "checker":
            self.text(node.get("text"))
            self.add("[勾选组件：仅提供标签，未读取勾选状态或附加内容]", unread=True)
        elif tag in {"input", "select_static", "select_person", "multi_select_static", "multi_select_person", "overflow", "date_picker", "picker_date", "picker_time", "picker_datetime"}:
            self.text(node.get("label"))
            self.text(node.get("placeholder"))
            self.add("[交互组件：仅提供标签，未读取选项或输入值]", unread=True)
        elif tag == "table":
            self.table(node)
        elif tag in {"img", "image", "img_combination", "audio", "video", "chart", "person", "person_list", "avatar"}:
            self.text(node.get("alt"))
            self.text(node.get("title"))
            label = {"img": "图片", "image": "图片", "img_combination": "图片", "audio": "音频", "video": "视频", "chart": "图表"}.get(tag, "人员")
            self.add(f"[{label}：未读取内容]", unread=True)
        elif tag == "hr":
            self.add("---")
        elif tag not in {"br", "custom_icon", "standard_icon"}:
            self.add("[未提取的卡片组件]", unread=True)

    def table(self, node: dict[str, Any]) -> None:
        columns, rows = node.get("columns"), node.get("rows")
        if not isinstance(columns, list) or not isinstance(rows, list) or not columns:
            self.add("[表格：未提取内容]", unread=True)
            return
        # The card schema defines columns beyond 50 as undisplayed. Pagination
        # and other client layout settings do not restrict the readable rows.
        if len(columns) > 50:
            self.add("[表格：已省略第 50 列之后不展示的内容]", unread=True)
        columns = [column for column in columns[:50] if isinstance(column, dict) and isinstance(column.get("name"), str)]
        if not columns:
            self.add("[表格：未提取内容]", unread=True)
            return
        labels = [self.localized(column.get("display_name")).strip() for column in columns]
        if any(labels):
            self.add(" | ".join(labels))
        for row in rows:
            if not isinstance(row, dict):
                self.add("[表格行：未提取内容]", unread=True)
                continue
            cells: list[str] = []
            incomplete = False
            for column in columns:
                value = row.get(column["name"])
                data_type = column.get("data_type", "text")
                if value is None:
                    cells.append("")
                elif isinstance(data_type, str) and data_type in {"text", "markdown", "lark_md", "number"} and isinstance(value, (str, int, float, bool)):
                    cells.append(str(value))
                else:
                    cells.append("[未提取的单元格]")
                    incomplete = True
            self.add(" | ".join(cells), unread=incomplete)


def extract_card_content(
    card: dict[str, Any], *, message_id: str | None = None,
    parent_message_id: str | None = None, preferred_locale: str | None = None,
) -> PreparedCardContent:
    original = original_card(card)
    if original is None:
        raise CardContentError("无法确认应用卡片的原始结构，本条消息未执行。")
    _bounded(original)
    legacy = original.get("schema") != "2.0"
    projection = _TextProjection(preferred_locale)
    header = original.get("header")
    if isinstance(header, dict):
        projection.text(header.get("title"))
        projection.text(header.get("subtitle"))
        tag_lists = _variants(header.get("i18n_text_tag_list"))
        tags = _choose(tag_lists, projection.locale, allow_empty_list=True) if tag_lists else header.get("text_tag_list")
        projection.elements(tags)
    before_body = projection.readable
    if legacy:
        variants = _variants(original.get("i18n_elements"))
        elements = _choose(variants, projection.locale) if variants else original.get("elements")
    else:
        elements = original.get("body", {}).get("elements")
    projection.elements(elements)
    if projection.readable == before_body:
        projection.add("[未提取到卡片正文]", unread=True)
    prefixes: list[str] = []
    if legacy:
        prefixes.append("此为飞书 1.0 卡片，内容为尽力提取，可能缺少标题或部分正文；如任务需要，可通过飞书工具读取原始卡片核对。")
    elif projection.unread:
        prefixes.append("卡片内容为部分读取，包含未提取组件或未读取内容。")
    prefixes.append(_TEXT_NOTICE)
    source = {key: value for key, value in (("message_id", message_id), ("parent_message_id", parent_message_id)) if isinstance(value, str) and 0 < len(value) <= 256}
    if source:
        prefixes.append("来源：" + json.dumps(source, ensure_ascii=False))
    text = "\n".join([*prefixes, *projection.parts])
    if len(text) > _TEXT_LIMIT:
        raise CardContentError("应用卡片可见文字超过可验证范围，本条消息未执行。")
    return PreparedCardContent(text, truncated=projection.unread, content_read=bool(projection.readable))
