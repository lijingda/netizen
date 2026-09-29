"""Image-token editing through the standard Markdown parser and renderer."""

from collections.abc import Iterator, Sequence
from typing import Any
from urllib.parse import urlsplit

from markdown_it import MarkdownIt
from markdown_it.token import Token
from mdformat.plugins import PARSER_EXTENSIONS
from mdformat.renderer import MDRenderer


_GFM = PARSER_EXTENSIONS["gfm"]
_MARKDOWN = MarkdownIt("commonmark", {
    "mdformat": {"wrap": "keep", "number": True},
    "parser_extension": [_GFM],
}, renderer_cls=MDRenderer)
_GFM.update_mdit(_MARKDOWN)
_DEFAULT_VALIDATE_LINK = _MARKDOWN.validateLink


def _validate_local_file_link(target: str) -> bool:
    # The default rejects file: links. Permit local file URLs for image reads,
    # but never network shares or the other schemes rejected by CommonMark.
    try:
        parts = urlsplit(target)
    except ValueError:
        return False
    if parts.scheme == "file":
        return not (parts.netloc or parts.query or parts.fragment)
    return _DEFAULT_VALIDATE_LINK(target)


_MARKDOWN.validateLink = _validate_local_file_link


def parse_markdown(content: str) -> tuple[list[Token], dict[str, Any]]:
    env: dict[str, Any] = {}
    return _MARKDOWN.parse(content, env), env


def _walk_tokens(tokens: Sequence[Token]) -> Iterator[Token]:
    for token in tokens:
        yield token
        # Alt text is not a separate image or link to be rewritten.
        if token.type != "image" and token.children:
            yield from _walk_tokens(token.children)


def image_tokens(tokens: Sequence[Token]) -> Iterator[Token]:
    return (token for token in _walk_tokens(tokens) if token.type == "image")


def render_markdown(tokens: Sequence[Token], env: dict[str, Any]) -> str:
    for token in _walk_tokens(tokens):
        if token.type not in {"image", "link_open"}:
            continue
        if token.type == "image":
            # mdformat 1.0 otherwise drops escaped/entity text in image alt.
            token.children = [Token("text", "", 0, content=token.content)]
        # Attributes are already decoded. Escape literal entities/backslashes
        # for Markdown output; autolinks do not interpret character references.
        uri_attr = "src" if token.type == "image" else "href"
        if token.info != "auto" and (uri := token.attrGet(uri_attr)) is not None:
            token.attrSet(uri_attr, uri.replace("&", "&amp;"))
        if (title := token.attrGet("title")) is not None:
            title = title.replace("&", "&amp;").replace("\\", "\\\\")
            if token.type == "image":  # Link rendering already escapes quotes.
                title = title.replace('"', '\\"')
            token.attrSet("title", title)
    return _MARKDOWN.renderer.render(tokens, _MARKDOWN.options, env).rstrip("\n")


def replace_image_with_text(token: Token, detail: str) -> None:
    label = " ".join(token.content.split())[:120] or "图片"
    token.type, token.tag = "text", ""
    token.content = f"〔图片：{label}；{detail}〕"
    token.attrs, token.children = {}, None
