from __future__ import annotations

from collections.abc import Sequence
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import Mock

from netizen_cli.admin.auth import AdminAuth, IssuedActionGrant
from netizen_cli.admin.web import AdminActionTarget, _ActionGrantPlan, _empty_preconditions


# Component-only harnesses explicitly choose their dependencies and skip bootstrap.
CONFIG_SCRIPTS = ("config-editor.js",)


def action_grant_contract() -> dict[str, list[str]]:
    """Exercise the Web serializer with inert tokens; never issue real credentials."""
    auth = Mock(spec=AdminAuth)
    auth.issue_actions.return_value = (
        IssuedActionGrant(csrf_token="fixture-csrf", action_token="fixture-action", expires_at=0),
    )
    plan = _ActionGrantPlan(auth, "fixture-session")
    envelope = plan.add(
        "sessions.stop", AdminActionTarget("binding", "fixture-binding", "fixture-scope"),
        _empty_preconditions(),
    )
    plan.issue()
    return {"grant": sorted(envelope), "target": sorted(envelope["target"])}


class _HtmlTree(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.root: dict = {"tag": "document", "attrs": {}, "children": []}
        self.stack = [self.root]
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = {"tag": tag, "attrs": dict(attrs), "children": []}
        if tag == "script" and (src := node["attrs"].get("src")):
            if not src.startswith("/static/") or ".." in src.split("/"):
                raise ValueError(f"Expected a shipped static script, got {src!r}")
            self.scripts.append(src.removeprefix("/static/"))
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
    scripts: Sequence[str] | None,
    before: str,
    after: str,
) -> str:
    """Use scripts=None for the page's script order; tuples isolate components."""
    static = Path(__file__).resolve().parents[2] / "netizen_cli/admin/static"
    if isinstance(page, str):
        tree = _HtmlTree()
        tree.feed((static / page).read_text(encoding="utf-8"))
        html_tree = tree.root
        if scripts is None:
            scripts = tree.scripts
    else:
        html_tree = page
        if scripts is None:
            raise ValueError("Page scripts require an HTML filename")
    dom = Path(__file__).with_name("dom_harness.js").read_text(encoding="utf-8")
    grants = Path(__file__).with_name("action_grant_fixture.js").read_text(encoding="utf-8")
    contract = "const actionGrantContract = " + json.dumps(action_grant_contract()) + ";\n"
    if page == "index.html" and "admin-ui.js" not in scripts:
        scripts = ("admin-ui.js", *scripts)
    shipped = "\n".join((static / name).read_text(encoding="utf-8") for name in scripts)
    return "const htmlTree = " + json.dumps(html_tree) + ";\n" + dom + contract + grants + before + shipped + after


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
        [node, "--max-old-space-size=256", *args], input=source, capture_output=True, text=True,
        timeout=timeout, env=env,
    )
