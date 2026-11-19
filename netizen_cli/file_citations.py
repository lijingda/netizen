"""Recognize Codex client file citations in answer prose, preserving Markdown source."""

import re
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from unicodedata import category

from markdown_it.rules_core import StateCore
from markdown_it.rules_inline import StateInline
from markdown_it.token import Token

from .markdown_images import answer_markdown_parser


_NAME = "codex-file-citation"
_PREFIX = re.compile(r"(?<!:):{1,3}codex-file-citation\{")
_END = re.compile(r"[ \t]*+}")
_MAX_DIRECTIVE = 4096
_MAX_LABEL = 200


def _attribute_pattern(double: str, single: str) -> re.Pattern[str]:
    # A value starting with a quote must close it, even if an unquoted match would fit.
    unquoted = r"""(?!["'])[^ \t}\r\n]++"""
    return re.compile(
        rf"[ \t]*+([A-Za-z0-9_-]++)[ \t]*+=[ \t]*+({double}|{single}|{unquoted})"
    )


_LITERAL = _attribute_pattern(r'"[^"\r\n]*+"', r"'[^'\r\n]*+'")
_BACKSLASH = _attribute_pattern(r'"(?:\\"|[^"\r\n])*+"', r"'(?:\\'|[^'\r\n])*+'")


@dataclass(frozen=True, slots=True)
class FileCitation:
    raw: str
    path: str
    attributes: Mapping[str, str]


def _parse(text: str, prefix: re.Match[str], limit: int) -> FileCitation | None:
    start = prefix.start()
    before = start
    while before and text[before - 1] == "\\":
        before -= 1
    if (start - before) % 2:
        return None

    limit = min(limit, start + _MAX_DIRECTIVE)
    for pattern in (_LITERAL, _BACKSLASH):
        pos = prefix.end()
        attributes: dict[str, str] = {}
        while match := pattern.match(text, pos, limit):
            key, value = match.groups()
            if key in attributes:
                break
            if value[0] in "\"'":
                quote = value[0]
                value = value[1:-1]
                if pattern is _BACKSLASH:
                    value = value.replace("\\" + quote, quote)
            attributes[key] = value
            pos = match.end()
        else:
            if end := _END.match(text, pos, limit):
                if path := attributes.get("path"):
                    return FileCitation(text[start:end.end()], path, MappingProxyType(attributes))
                return None
    return None


def _candidates(text: str) -> Iterator[tuple[int, FileCitation]]:
    for prefix in _PREFIX.finditer(text):
        citation = _parse(text, prefix, len(text))
        if citation is not None:
            yield prefix.start(), citation


def _inline_citation(state: StateInline, silent: bool) -> bool:
    if state.src[state.pos] != ":":
        return False
    prefix = _PREFIX.match(state.src, state.pos, state.posMax)
    if prefix is None or (citation := _parse(state.src, prefix, state.posMax)) is None:
        return False
    if not silent:
        token_type = "text" if state.linkLevel else "file_citation"
        state.push(token_type, "", 0).content = citation.raw
    state.pos += len(citation.raw)
    return True


_MARKDOWN = answer_markdown_parser()
_MARKDOWN.inline.ruler.before("linkify", "file_citation", _inline_citation)


def rewrite_file_citations(
    text: str, replace: Callable[[FileCitation], str], *, block_tokens: Callable[[], list[Token]] = list,
) -> str:
    """Replace a spelling only when every source occurrence is recognized in prose."""
    if _NAME not in text or not (candidates := list(_candidates(text))):
        return text

    # Block rules give inline tokens their prose context, but not source offsets.
    # Keep the caller's token allocation budget during this parse, too.
    state = StateCore(text, _MARKDOWN, {})
    state.tokens = block_tokens()
    _MARKDOWN.core.process(state)
    # Each pipe within a GFM cell lost exactly one backslash during block
    # parsing. Recover the source spelling before comparing occurrence counts.
    prose_counts: Counter[str] = Counter()
    for index, token in enumerate(state.tokens):
        if token.type == "inline":
            in_cell = index > 0 and state.tokens[index - 1].type in {"th_open", "td_open"}
            for child in token.children or ():
                if child.type == "file_citation":
                    raw = child.content.replace("|", "\\|") if in_cell else child.content
                    prose_counts[raw] += 1

    # CommonMark normalizes NUL before tokenization.
    source_counts = Counter(citation.raw.replace("\x00", "\ufffd") for _, citation in candidates)

    parts: list[str] = []
    offset = 0
    for start, citation in candidates:
        if start < offset:
            # An earlier replacement already consumed this nested candidate.
            continue
        key = citation.raw.replace("\x00", "\ufffd")
        if prose_counts[key] == source_counts[key]:
            replacement = replace(citation)
            if replacement != citation.raw:
                parts.extend((text[offset:start], replacement))
                offset = start + len(citation.raw)
    if not parts:
        return text
    parts.append(text[offset:])
    return "".join(parts)


def _label(name: str, attributes: Mapping[str, str]) -> str:
    parts = [name]
    if page := attributes.get("page_number"):
        parts.append(f"第 {page} 页")
    if slide := attributes.get("slide_number"):
        parts.append(f"第 {slide} 张幻灯片")
    if cell := "!".join(filter(None, (attributes.get("sheet"), attributes.get("range")))):
        parts.append(cell)
    if label := attributes.get("label"):
        parts.append(label)
    visible = "".join(c for c in " · ".join(parts) if category(c) not in {"Cc", "Cf", "Cs", "Zl", "Zp"}) or "文件"
    return visible[:_MAX_LABEL - 1] + "…" if len(visible) > _MAX_LABEL else visible


def citation_label(citation: FileCitation) -> str:
    return _label(citation.path.rsplit("/", 1)[-1] or citation.path, citation.attributes)


def citation_code(citation: FileCitation) -> str:
    if citation.path.endswith("/"):
        # Shared labels retain a trailing-slash path. A static Feishu reference
        # may display the directory name, but never its absolute host path.
        directory = citation.path.rstrip("/").rsplit("/", 1)[-1]
        label = _label(directory + "/" if directory else "目录", citation.attributes)
    else:
        label = citation_label(citation)
    return "`" + label.replace("`", "'") + "`"
