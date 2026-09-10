"""Check local navigation in repository Markdown, without a runtime dependency.

This is a link checker for the documentation's inline/reference links, ATX and
setext headings, and HTML links/images/anchors, not a Markdown renderer. Fenced
examples, inline code and comments are inert. Heading IDs follow GitHub's rules:
https://docs.github.com/en/get-started/writing-on-github/getting-started-with-writing-and-formatting-on-github/basic-writing-and-formatting-syntax#section-links
"""

from __future__ import annotations

import re
import unicodedata
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urlsplit


_INLINE_LINK = re.compile(
    r"!?\[(?:[^\[\]]|\[[^\]]*\])*\]\(\s*"
    r'(?P<target><[^>\n]*>|(?:\\.|[^()\s]|\([^()\s]*\))*)'
    r'''(?:\s+(?:"[^"\n]*"|'[^'\n]*'|\([^\n]*\)))?\s*\)'''
)
_REFERENCE = re.compile(r"^ {0,3}\[[^\]]+\]:\s*(<[^>\n]+>|\S+)", re.MULTILINE)
_CODE_SPAN = re.compile(r"(`+)(.+?)\1(?!`)", re.DOTALL)


class _HTMLReferences(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.targets: list[str] = []
        self.anchors: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        for attribute in (("id", "name") if tag == "a" else ("id",)):
            if value := values.get(attribute):
                self.anchors.add(value)
        attributes = {"a": ("href",), "img": ("src", "srcset"), "source": ("srcset",)}
        for attribute in attributes.get(tag, ()):
            if not (value := values.get(attribute)):
                continue
            if attribute != "srcset":
                self.targets.append(value)
            elif not value.startswith("data:"):
                self.targets.extend(
                    item.strip().split()[0] for item in value.split(",") if item.strip()
                )


def _prose(text: str) -> str:
    text = re.sub(r"\A---\n.*?\n---(?:\n|$)", "", text, flags=re.DOTALL)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    lines: list[str] = []
    fence = ""
    for line in text.splitlines():
        match = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence:
            closing = rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}\s*"
            if re.fullmatch(closing, line):
                fence = ""
            lines.append("")
        elif match:
            fence = match[1]
            lines.append("")
        else:
            lines.append(line)
    return "\n".join(lines)


def _heading_slug(heading: str) -> str:
    code: list[str] = []

    def protect_code(match: re.Match[str]) -> str:
        code.append(match[2])
        return f"\x00{len(code) - 1}\x00"

    heading = _CODE_SPAN.sub(protect_code, heading)
    heading = re.sub(r"!?\[([^\]]+)\]\([^)]*\)", r"\1", heading)
    heading = re.sub(r"<[^>]+>", "", heading)
    heading = re.sub(r"(\*{1,3}|_{1,3}|~~)(\S(?:.*?\S)?)\1", r"\2", heading)
    heading = re.sub(r"\x00(\d+)\x00", lambda match: code[int(match[1])], heading)
    return "".join(
        "-" if character == " " else character
        for character in unescape(heading).lower()
        if character in " -_" or unicodedata.category(character)[0] in "LNM"
    )


def _index(document: Path) -> tuple[list[str], set[str]]:
    prose = _prose(document.read_text(encoding="utf-8"))
    rendered_links = _CODE_SPAN.sub("", prose)
    html = _HTMLReferences()
    html.feed(rendered_links)
    targets = [match["target"].strip("<>") for match in _INLINE_LINK.finditer(rendered_links)]
    targets.extend(match[1].strip("<>") for match in _REFERENCE.finditer(rendered_links))
    targets.extend(html.targets)

    anchors: set[str] = set()
    lines = prose.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        if match:
            heading = match[1]
        elif index and re.fullmatch(r" {0,3}(?:=+|-+)\s*", line) and lines[index - 1].strip():
            heading = lines[index - 1].strip()
        else:
            continue
        slug = _heading_slug(heading)
        anchor = slug
        suffix = 0
        while anchor in anchors:
            suffix += 1
            anchor = f"{slug}-{suffix}"
        anchors.add(anchor)
    return targets, anchors | html.anchors


def local_link_errors(root: Path, documents: Iterable[Path]) -> list[str]:
    """Return broken paths/Markdown anchors, requiring local targets inside root."""
    root = root.resolve()
    indexes: dict[Path, tuple[list[str], set[str]]] = {}

    def index(document: Path) -> tuple[list[str], set[str]]:
        if document not in indexes:
            indexes[document] = _index(document)
        return indexes[document]

    errors: list[str] = []
    for document in documents:
        document = document.resolve()
        for target in index(document)[0]:
            url = urlsplit(unescape(target))
            if url.scheme or url.netloc:
                continue
            path = (document.parent / unquote(url.path)).resolve() if url.path else document
            context = f"{document.relative_to(root)} -> {target}"
            if not path.is_relative_to(root):
                errors.append(f"{context}: escapes documentation root")
            elif not path.exists():
                errors.append(f"{context}: missing file")
            elif url.fragment and path.suffix.lower() == ".md":
                if unquote(url.fragment) not in index(path)[1]:
                    errors.append(f"{context}: missing heading or HTML anchor")
    return errors
