"""Bounded, inert Markdown previews for authenticated Admin readers."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
from markdown_it.rules_core import StateCore
from markdown_it.token import Token


MAX_MARKDOWN_BYTES = 256 * 1024
MAX_PREVIEW_BYTES = 1024 * 1024
MAX_MARKDOWN_TOKENS = 20_000
MAX_MARKDOWN_IMAGES = 8

_MARKDOWN = MarkdownIt("commonmark", {
    "html": False,
    "linkify": False,
    "maxNesting": 20,
}).enable("table")


class PreviewUnavailable(ValueError):
    """The source remains viewable, but its rendered preview exceeds a budget."""


class _BoundedBlockTokens(list[Token]):
    def append(self, token: Token) -> None:
        # Tables pad missing cells while parsing. Stop before that expansion can
        # allocate an unbounded token graph, not merely after parse() returns.
        if len(self) >= MAX_MARKDOWN_TOKENS:
            raise PreviewUnavailable("Markdown 内容过于复杂，请查看源码。")
        super().append(token)


def _limit_block_tokens(state: StateCore) -> None:
    state.tokens = _BoundedBlockTokens(state.tokens)


_MARKDOWN.core.ruler.before("block", "preview_token_budget", _limit_block_tokens)


def _unsafe_characters(value: str) -> bool:
    return "\\" in value or any(
        ord(char) < 32 or 127 <= ord(char) <= 159 for char in value
    )


def _local_path(value: str, directory: list[str]) -> str | None:
    if value.startswith("/") or _unsafe_characters(value):
        return None
    parts = directory.copy()
    for part in value.split("/"):
        if part.casefold() == ".git":
            return None
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)


def _target(value: str, directory: list[str]) -> tuple[str, str] | None:
    """Classify decoded URLs without allowing another decoding pass to change them."""
    if _unsafe_characters(value):
        return None
    try:
        decoded = unquote(value, errors="strict")
        if _unsafe_characters(decoded) or unquote(decoded, errors="strict") != decoded:
            return None
        parts = urlsplit(value)
        decoded_parts = urlsplit(decoded)
        # Decoding may reveal an unsafe scheme or authority, but encoded # and
        # ? in a local filename must not become URL separators.
        if decoded_parts.scheme != parts.scheme or decoded_parts.netloc != parts.netloc:
            return None
    except (UnicodeError, ValueError):
        return None
    if parts.scheme:
        if parts.scheme not in {"http", "https", "mailto"}:
            return None
        if parts.scheme in {"http", "https"} and not parts.netloc:
            return None
        if parts.scheme == "mailto" and not parts.path:
            return None
        return "external", value
    if parts.netloc or parts.query or not parts.path:
        # Headings deliberately have no ids. Pure fragments cannot navigate the
        # containing Admin document; file fragments open that file from the top.
        return None
    local_path = unquote(parts.path, errors="strict")
    if (relative := _local_path(local_path, directory)) is None:
        return None
    directory_hint = local_path.endswith("/") or local_path.rsplit("/", 1)[-1] in {".", ".."}
    return "directory" if directory_hint else "local", relative


def _tokens(tokens: Sequence[Token]) -> Iterator[Token]:
    for token in tokens:
        yield token
        if token.children:
            yield from _tokens(token.children)


def _replace_image(token: Token, reason: str) -> None:
    label = " ".join(_image_alt(token.children or []).split())[:120] or "图片"
    token.type, token.tag = "text", ""
    token.content = f"〔图片：{label}；{reason}〕"
    token.attrs, token.children = {}, None


def _image_alt(tokens: Sequence[Token]) -> str:
    # markdown-it-py's default image alt renderer omits text_special/code_inline.
    # Flatten those parsed tokens to plain text so entities and code stay visible,
    # and nested Markdown images never become independent resource requests.
    parts: list[str] = []
    for token in tokens:
        if token.type in {"text", "text_special", "code_inline"}:
            parts.append(token.content)
        elif token.type in {"softbreak", "hardbreak"}:
            parts.append("\n")
        elif token.children:
            parts.append(_image_alt(token.children))
    return "".join(parts)


def _escaped_bytes(value: str) -> int:
    return (
        len(value.encode("utf-8"))
        + value.count("&") * 4
        + value.count('"') * 5
        + (value.count("<") + value.count(">")) * 3
    )


def _check_render_budget(tokens: Sequence[Token]) -> None:
    # A conservative upper bound before rendering also limits reference-link
    # amplification: many small [x] uses can share a very long URL/title. Count
    # inline source and image children too rather than underestimate HTML output.
    size = 0
    for token in _tokens(tokens):
        size += 64 + 2 * len(token.tag) + _escaped_bytes(token.content)
        size += _escaped_bytes(token.info)
        for key, value in token.attrs.items():
            size += len(key) + 4 + _escaped_bytes(str(value))
        if size > MAX_PREVIEW_BYTES:
            raise PreviewUnavailable("Markdown 预览内容过大，请查看源码。")


def render_markdown(
    text: str,
    *,
    path: str | None = None,
    image_url: Callable[[str], str] | None = None,
) -> str:
    """Render safe HTML; image_url maps a root-relative file to its Admin endpoint.

    Local links carry ``data-file-browser-path`` for the file browser to intercept;
    the empty path identifies its root. Explicit directory targets additionally
    carry ``data-file-browser-directory="true"``.
    Remote images and fragment-only links remain inert. No filesystem or network
    access occurs here; the file endpoint separately validates image contents.
    Without a file context, local links and all images remain inert.
    """
    try:
        length = len(text.encode("utf-8"))
    except UnicodeError as exc:
        raise PreviewUnavailable("文件包含无法预览的文本编码，请查看源码。") from exc
    if length > MAX_MARKDOWN_BYTES:
        raise PreviewUnavailable("Markdown 文件超过 256 KiB，请查看源码。")
    if path is not None and (not path or _local_path(path, []) != path):
        raise PreviewUnavailable("Markdown 文件路径无效，请查看源码。")
    directory = path.split("/")[:-1] if path is not None else []
    env: dict[str, object] = {}
    tokens = _MARKDOWN.parse(text, env)
    if sum(1 for _ in _tokens(tokens)) > MAX_MARKDOWN_TOKENS:
        raise PreviewUnavailable("Markdown 内容过于复杂，请查看源码。")
    _check_render_budget(tokens)

    images = 0
    for token in _tokens(tokens):
        if token.type in {"th_open", "td_open"}:
            # The Admin CSP forbids inline style attributes; table alignment
            # uses fixed classes from the shared preview stylesheet instead.
            alignment = token.attrs.pop("style", None)
            if alignment in {"text-align:left", "text-align:center", "text-align:right"}:
                token.attrSet("class", "markdown-align-" + alignment.split(":")[1])
        elif token.type == "link_open":
            target = _target(token.attrGet("href") or "", directory)
            token.attrs.pop("href", None)
            if target is None:
                continue
            kind, value = target
            if kind == "external":
                token.attrSet("href", value)
                token.attrSet("rel", "noopener noreferrer")
                token.attrSet("target", "_blank")
            elif path is not None:
                token.attrSet("href", "#")
                token.attrSet("data-file-browser-path", value)
                if kind == "directory":
                    token.attrSet("data-file-browser-directory", "true")
        elif token.type == "image":
            target = _target(token.attrGet("src") or "", directory)
            if path is None or image_url is None:
                _replace_image(token, "此处不加载图片")
            elif target is None or target[0] != "local":
                _replace_image(token, "仅预览当前根目录内的相对路径图片")
            elif images >= MAX_MARKDOWN_IMAGES:
                _replace_image(token, "本页最多预览 8 张图片")
            else:
                token.children = [Token(
                    "text", "", 0, content=_image_alt(token.children or []),
                )]
                token.attrSet("src", image_url(target[1]))
                token.attrSet("loading", "lazy")
                token.attrSet("decoding", "async")
                token.attrSet("referrerpolicy", "no-referrer")
                images += 1

    _check_render_budget(tokens)
    rendered = _MARKDOWN.renderer.render(tokens, _MARKDOWN.options, env)
    if len(rendered.encode("utf-8")) > MAX_PREVIEW_BYTES:
        raise PreviewUnavailable("Markdown 预览内容过大，请查看源码。")
    return rendered
