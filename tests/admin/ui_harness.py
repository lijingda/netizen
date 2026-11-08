from __future__ import annotations

from collections.abc import Sequence
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest


# Shared editor dependencies; page-specific controllers stay visible in each test.
CONFIG_SCRIPTS = ("config-editor.js",)
CHAT_CONFIG_SCRIPTS = ("chat-picker.js", *CONFIG_SCRIPTS)


class _HtmlTree(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.root: dict = {"tag": "document", "attrs": {}, "children": []}
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = {"tag": tag, "attrs": dict(attrs), "children": []}
        self.stack[-1]["children"].append(node)
        if tag not in {"meta", "link", "input", "br", "hr", "img"}:
            self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        if self.stack[-1]["tag"] == tag:
            self.stack.pop()

    def handle_data(self, data: str) -> None:
        self.stack[-1]["children"].append(data)


def read_fixture(name: str, marker: str | None = None) -> tuple[str, str]:
    """Split a harness around shipped scripts, or use an unsplit file as its tail."""
    source = Path(__file__).with_name(name).read_text(encoding="utf-8")
    if marker is None:
        return "", source
    before, after = source.split(marker)
    return before, after


def harness_source(
    page: str | dict,
    scripts: Sequence[str],
    before: str,
    after: str,
) -> str:
    static = Path(__file__).resolve().parents[2] / "netizen_cli/admin/static"
    if isinstance(page, str):
        tree = _HtmlTree()
        tree.feed((static / page).read_text(encoding="utf-8"))
        html_tree = tree.root
    else:
        html_tree = page
    dom = Path(__file__).with_name("dom_harness.js").read_text(encoding="utf-8")
    shipped = "\n".join((static / name).read_text(encoding="utf-8") for name in scripts)
    return "const htmlTree = " + json.dumps(html_tree) + ";\n" + dom + before + shipped + after


def run_harness(
    test: unittest.TestCase,
    source: str,
    *,
    timeout: int,
    args: Sequence[str] = (),
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    node = shutil.which("node")
    if node is None:
        reason = "Node.js 22 is required for Admin JavaScript behavior tests"
        if os.environ.get("CI") == "true":
            test.fail(reason)
        test.skipTest(reason)
    return subprocess.run(
        [node, *args], input=source, capture_output=True, text=True,
        timeout=timeout, env=env,
    )
