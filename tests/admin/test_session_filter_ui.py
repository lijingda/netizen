from __future__ import annotations

from html.parser import HTMLParser
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest


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


class SessionFilterUiTest(unittest.TestCase):
    def test_multiselect_queries_search_pagination_accessibility_and_reset(self) -> None:
        node = shutil.which("node")
        if node is None:
            reason = "Node.js 22 is required for Admin JavaScript behavior tests"
            if os.environ.get("CI") == "true":
                self.fail(reason)
            self.skipTest(reason)
        root = Path(__file__).resolve().parents[2]
        static = root / "netizen/admin/static"
        tree = _HtmlTree()
        tree.feed((static / "index.html").read_text(encoding="utf-8"))
        source = (static / "admin.js").read_text(encoding="utf-8")
        filters = source[
            source.index("const timeRangeControllers ="):
            source.index("function runtimeLabel(")
        ]
        pagination = source[
            source.index("function renderSessionPagination("):
            source.index("function wireSessionActions(")
        ] + source[
            source.index("async function moveSessionPage("):
            source.index("function sideRuntimeLabel(")
        ]
        harness = Path(__file__).with_name("session_filter_ui_harness.js").read_text(
            encoding="utf-8"
        )
        before, after = harness.split("// SHIPPED_FILTER_CONTROLLERS\n")
        dom = Path(__file__).with_name("dom_harness.js").read_text(encoding="utf-8")
        result = subprocess.run(
            [node, "-e", "const htmlTree = " + json.dumps(tree.root) + ";\n"
             + dom + before + filters + pagination + after],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
