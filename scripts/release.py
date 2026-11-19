#!/usr/bin/env python3
"""Execute the full Netizen release chain after a maintainer instruction.

The maintainer decides when to release and records evidence or accepted risks
for the changed areas. This script derives the version from conventional
commits since the previous tag, renders deterministic release
notes, prepares the version through a checked pull request when needed,
waits for the exact main commit's CI, creates and pushes the
protected annotated tag, dispatches the release workflow with the notes, and
follows the workflow to the published release.

The PyPI netizen-cli Trusted Publisher must already authorize this repository,
release.yml and the published-release environment. The workflow's manual UI
defaults to build verification; this maintainer-requested full chain publishes
the wheel/sdist to PyPI and only then finalizes the GitHub Release (ADR 0076).

Use --plan to inspect the local committed range without starting a release.
Anything needing human judgement fails closed and escalates: a breaking
change in the range, an explicitly chosen version, or any failed gate.  The
script never weakens the release workflow's own integrity checks; a bad run
can at worst publish an extra, fully qualified release.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Literal

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RELEASE_WORKFLOW = "release.yml"
CI_WORKFLOW = "ci.yml"
POLL_INTERVAL_SECONDS = 20
CHECK_TIMEOUT_SECONDS = 25 * 60
CI_TIMEOUT_SECONDS = 30 * 60
RELEASE_RUN_TIMEOUT_SECONDS = 20 * 60
COMMAND_TIMEOUT_SECONDS = 120

TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
MERGE_SUBJECT_RE = re.compile(r"^Merge pull request #(\d+) from ")
CONVENTIONAL_RE = re.compile(
    r"^(?P<type>feat|fix|docs|style|refactor|perf|test|build|ci|chore)"
    r"(?:\([^)]*\))?(?P<breaking>!)?:\s*(?P<subject>.+)$"
)
LOG_FORMAT = "%H%x1f%s%x1f%B%x1e"
BUMP_COMMIT_TEMPLATE = "Prepare {tag} release"
TAG_MESSAGE_TEMPLATE = "Netizen {tag}"

# Every file must contain exactly one version anchor. All versions must agree
# on the previous release or the prepared target before any release mutation.
VERSION_FILES: tuple[tuple[str, str], ...] = (
    ("pyproject.toml", 'version = "{version}"'),
    ("netizen_cli/__init__.py", '__version__ = "{version}"'),
)

SECTION_ORDER = ("Features", "Bug Fixes", "Documentation", "Other Changes")
TYPE_HEADINGS = {"feat": "Features", "fix": "Bug Fixes", "docs": "Documentation"}


class ReleaseError(Exception):
    """A gate failed or an invariant broke; stop without further side effects."""


class ReleaseEscalation(ReleaseError):
    """A maintainer decision is required before the release can proceed."""


Projection = Callable[[str, str], object]
CI_COVERAGE = (
    "make check (Linux CPython matrix and macOS arm64), make check-native "
    "(Linux; macOS arm64 reported, not yet required), and make check-browser (Linux Admin smoke)"
)


def sdk_pins(*names: str) -> Projection:
    def pins(path: str, source: str) -> list[str]:
        if path == "pyproject.toml":
            project = tomllib.loads(source).get("project", {})
            requirements = list(project.get("dependencies", []))
            for group in project.get("optional-dependencies", {}).values():
                requirements.extend(group)
        else:
            requirements = source.splitlines()
        values = []
        for requirement in requirements:
            spec = requirement.partition("#")[0].strip()
            match = re.match(r"([\w.-]+)\s*(.*)", spec)
            if match:
                name = re.sub(r"[-_.]+", "-", match[1].lower())
                if name in names:
                    values.append(name + match[2])
        return sorted(values)
    return pins


def toml_fields(*keys: str) -> Projection:
    def fields(path: str, source: str) -> list[object]:
        data = tomllib.loads(source)
        result = []
        for key in keys:
            value = data
            for part in key.split("."):
                value = value.get(part, {})
            result.append(value)
        return result
    return fields


def packaging_metadata(path: str, source: str) -> object:
    data = tomllib.loads(source)
    project = data.get("project", {})
    setuptools = data.get("tool", {}).get("setuptools", {})
    # CI builds and inspects both distributions, including declarative resource
    # and package lists. Keep backend settings, hooks and entry points visible.
    setuptools.pop("package-data", None)
    setuptools.pop("py-modules", None)
    packages = setuptools.pop("packages", {})
    discovery_settings = {}
    if isinstance(packages, dict):
        discovery_settings = {key: value for key, value in packages.get("find", {}).items()
                              if key not in {"where", "include", "exclude"}}
    metadata = {key: project[key] for key in ("name", "scripts", "gui-scripts", "entry-points", "dynamic")
                if key in project}
    return data.get("build-system", {}), metadata, setuptools, discovery_settings


def python_support(path: str, source: str) -> object:
    project = tomllib.loads(source).get("project", {})
    requires = project.get("requires-python", "").replace(" ", "")
    versions = set()
    for classifier in project.get("classifiers", []):
        match = re.fullmatch(r"Programming Language :: Python :: (\d+(?:\.\d+)*)", classifier)
        if match:
            versions.add(match[1])
    return frozenset(requires.split(",")), versions


WORKFLOW_FILES = (".github/workflows/*.yml", ".github/workflows/*.yaml")


def workflow_file(path: str) -> bool:
    return any(fnmatchcase(path, pattern) for pattern in WORKFLOW_FILES)


def workflow_versions(path: str, source: str) -> set[str]:
    # The workflows declare literal setup-python versions and matrix axes.
    # Read both YAML list forms, ignoring job names, repetitions and matrix references.
    versions = set()
    matrix_depth = script_depth = -1
    lines = [line.partition("#")[0].rstrip() for line in source.splitlines()]
    for index, line in enumerate(lines):
        indent = len(line) - len(line.lstrip())
        if not line or 0 <= script_depth < indent:
            continue
        script_depth = -1
        if re.match(r"^\s*(?:- )?(?:run|script):\s*[>|]", line):
            script_depth = indent
            continue
        if indent <= matrix_depth:
            matrix_depth = -1
        if re.search(r"""(?:^\s*|[{,]\s*)['"]?matrix['"]?:\s*""", line):
            matrix_depth = indent
        field = re.search(r"""(?:^\s*(?:- )?|[{,]\s*)['"]?(python-version|python)['"]?:\s*(.*)""", line)
        if not field or (field[1] == "python" and matrix_depth < 0):
            continue
        value = field[2]
        if not value or value in {"|", ">"} or ("[" in value and "]" not in value):
            for nested in lines[index + 1:]:
                if not nested:
                    continue
                depth = len(nested) - len(nested.lstrip())
                if depth < indent or (depth == indent and not nested.strip().startswith(("- ", "]"))):
                    break
                value += " " + nested.strip().removeprefix("- ")
                if "]" in nested:
                    break
        value = re.sub(r"\$\{\{.*?\}\}", "", value)
        if "[" in value:
            value = value.partition("[")[2].partition("]")[0]
        else:
            value = value.partition(",")[0].partition("}")[0]
        versions.update(re.findall(r"(?<![\w.])(?:pypy-?)?\d[\w.*+-]*", value, re.IGNORECASE))
    return versions


def changed_workflow_versions(contents: Mapping[str, tuple[str, str]]) -> set[str]:
    old_union: set[str] = set()
    new_union: set[str] = set()
    differences = {}
    for path, (old, new) in contents.items():
        if workflow_file(path):
            before = workflow_versions(path, old)
            after = workflow_versions(path, new)
            old_union.update(before)
            new_union.update(after)
            differences[path] = before ^ after
    changed = old_union ^ new_union
    return {path for path, versions in differences.items() if versions & changed}


def python_symbols(*names: str) -> Projection:
    """Compare named top-level definitions and module-level setup using those names."""
    def project(path: str, source: str) -> list[str]:
        values = []
        for node in ast.parse(source, filename=path).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                selected = node.name in names
            else:
                selected = any(isinstance(ref, ast.Name) and ref.id in names for ref in ast.walk(node))
            if selected:
                values.append(ast.dump(node))
        return values
    return project


def distribution_builder(path: str, source: str) -> bytes:
    """Mask only literal file inventories; compare all surrounding source verbatim."""
    inventories = []
    for node in ast.parse(source, filename=path).body:
        match node:
            case (ast.Assign(targets=[ast.Name(id=name)], value=value)
                  | ast.AnnAssign(target=ast.Name(id=name), value=ast.expr() as value)
                  ) if name in {"REQUIRED_WHEEL_FILES", "REQUIRED_SDIST_FILES"}:
                match value:
                    case ast.Call(func=ast.Name(id="frozenset"), args=[literal], keywords=[]):
                        value = literal
                inventories.append(value)
            case ast.FunctionDef(name="verify_sdist"):
                # The sdist inventory is the iterable of the root-prefixed set comprehension.
                for statement in ast.walk(node):
                    match statement:
                        case ast.Assign(targets=[ast.Name(id="required")],
                                        value=ast.SetComp(generators=[ast.comprehension(iter=names)])):
                            inventories.append(names)

    # AST columns are UTF-8 byte offsets. Replace from the end to retain earlier positions.
    lines = source.encode().splitlines(keepends=True)
    for value in sorted(inventories, key=lambda item: (item.lineno, item.col_offset), reverse=True):
        if isinstance(value, (ast.Set, ast.List, ast.Tuple)) and all(
            isinstance(item, ast.Constant) and isinstance(item.value, str) for item in value.elts
        ):
            first, last = value.lineno - 1, value.end_lineno - 1
            lines[first:last + 1] = [lines[first][:value.col_offset] + b"\0" + type(value).__name__.encode()
                                     + lines[last][value.end_col_offset:]]
    return b"".join(lines)


def skill_package_rules(path: str, source: str) -> object:
    if path == "_build.py":
        # Include the loop over the shipped Skill names, its filters and any
        # direct copies of the skills directory. Ignore unrelated resource copies.
        rules = []
        for node in ast.walk(ast.parse(source, filename=path)):
            if isinstance(node, (ast.For, ast.Call)) and any(
                isinstance(term, ast.Constant) and isinstance(term.value, str)
                and "skills" in Path(term.value).parts for term in ast.walk(node)
            ):
                rules.append(ast.dump(node))
        return rules
    if path == "pyproject.toml":
        settings = tomllib.loads(source).get("tool", {}).get("setuptools", {})
        return {(package, pattern) for package, patterns in settings.get("exclude-package-data", {}).items()
                for pattern in patterns
                if "skills" in package or (package in {"*", "netizen_cli"} and
                    ("skills" in pattern or fnmatchcase("resources/skills/netizen-lark/SKILL.md", pattern)))}
    rules = [line.partition("#")[0].split() for line in source.splitlines()]
    return [rule for rule in rules if any("skills" in part for part in rule)
            or (rule[:1] in (["global-include"], ["global-exclude"])
                and any(fnmatchcase("SKILL.md", pattern) for pattern in rule[1:]))]


@dataclass(frozen=True)
class Area:
    name: str
    kind: Literal["manual", "automated"]
    requirement: str
    paths: tuple[str, ...]
    content: tuple[tuple[str, Projection], ...] = ()

    def matches(self, path: str, contents: Mapping[str, tuple[str, str]], changed_workflows: set[str]) -> bool:
        if any(fnmatchcase(path, pattern) for pattern in self.paths):
            return True
        for filename, project in self.content:
            if fnmatchcase(path, filename):
                if project is workflow_versions:
                    return path in changed_workflows
                old, new = contents[path]
                return project(path, old) != project(path, new)
        return False


def dependency_files(*names: str) -> tuple[tuple[str, Projection], ...]:
    pins = sdk_pins(*names)
    return (("pyproject.toml", pins), ("requirements.lock", pins))


# The release inventory. Manual boundaries may overlap. The first remaining
# automated group owns a path, with "other" last. ADR 0103 explains the gates.
AREAS = (
    Area("codex-sdk", "manual", "Complete the SDK/App Server upgrade review. Resolve each "
         "发布前补验 item (including Feishu client display and Linux/macOS ready) or record "
         "accepted residual risks and re-evaluation triggers.", (),
         dependency_files("openai-codex", "openai-codex-cli-bin")),
    Area("feishu-sdk", "manual", "Requalify real Feishu delivery, callbacks, quotes, resources "
         "and tenant history with the pinned SDKs (ADR 0039/0055/0099).", (),
         dependency_files("lark-channel", "lark-channel-sdk", "lark-oapi")),
    # The CLI owns install, data, update and remove. The deployment package and
    # Admin restart coordinator drive the manager; builders ship its entry points/resources.
    Area("platform-service", "manual", "Verify Linux and macOS install/start/ready/stop and the "
         "multi-instance gates in deployment.md, including affected update/remove/restart paths. "
         "Report platform and live validation separately.", (
             "netizen_cli/cli.py", "netizen_cli/__main__.py", "netizen_cli/cli_services.py",
             "netizen_cli/cli_update*.py", "netizen_cli/cli_packages.py", "netizen_cli/cli_data.py",
             "netizen_cli/instance.py", "netizen_cli/service_launcher.py", "netizen_cli/deployment/**",
             "netizen_cli/management/updates.py", "netizen_cli/package_resources.py",
             "_build.py", "MANIFEST.in",
         ), (
             ("scripts/build_cli_distribution.py", distribution_builder),
             ("pyproject.toml", packaging_metadata),
             ("netizen_cli/main.py", python_symbols(
                 "_scrub_channel_environment", "_validate_instance_environment", "_managed_absolute_path",
                 "_validate_instance_path", "_adopt_lifetime_lock", "_EARLY_LIFETIME_DESCRIPTOR",
                 "_READY_MARKER_CONTENT", "_publish_ready_marker", "_clear_ready_marker", "_run_service")),
         )),
    Area("python-matrix", "manual", "Verify macOS arm64 current-user LaunchAgent install, start "
         "and ready for the supported Python versions.", (), (
             ("pyproject.toml", python_support), *((pattern, workflow_versions) for pattern in WORKFLOW_FILES),
         )),
    # lark_app.py owns the credential profile used by setup, repair and startup.
    Area("feishu-app-setup", "manual", "Verify the real browser install/repair path and the Feishu "
         "permission contract: tenant/user grants, callbacks, approvals, app publication, installation "
         "and group membership.", (
             "netizen_cli/cli_setup.py", "netizen_cli/lark_app.py",
             "netizen_cli/feishu_app_onboarding.py", "netizen_cli/feishu_app_permissions.py",
             "scripts/feishu_app_onboarding.py", "scripts/feishu_app_permissions.py",
         )),
    Area("admin-login", "manual", "Verify real Feishu OAuth login, credential refresh and "
         "login return in the browser and Feishu client.", (
             "netizen_cli/admin/login.py", "netizen_cli/admin/feishu.py", "netizen_cli/admin/credentials.py",
             # login.html/css markup is covered by the browser smoke; login.js owns the flow.
             "netizen_cli/admin/static/login.js",
         )),
    Area("skills-loading", "manual", "Verify actual Skill execution in the affected new/cold/"
         "Side/fork/Goal/child/isolation phases, including real resource reads and failed "
         "registration preventing ready.", ("netizen_cli/builtin_skills.py",), (
             ("netizen_cli/sdk_gap_adapter.py", python_symbols(
                 "AppServerSkillRoots", "AppServerSkillCatalog", "_SKILLS_EXTRA_ROOTS_SET_METHOD",
                 "_SKILLS_LIST_METHOD", "_SKILL_ROOTS_TIMEOUT_SECONDS")),
             ("_build.py", skill_package_rules), ("MANIFEST.in", skill_package_rules),
             ("pyproject.toml", skill_package_rules),
         )),
    # The workflow identity and distribution name must match external publisher settings.
    Area("release-publisher", "manual", "Confirm PyPI project ownership and Trusted Publisher "
         "identity, GitHub Immutable Releases, tag protection and environment permissions.", (
             ".github/workflows/release.yml",
         ), (("pyproject.toml", toml_fields("project.name")),)),
    Area("admin", "automated", "Admin behavior and browser smoke.", ("netizen_cli/admin/**", "tests/admin/**")),
    Area("cards", "automated", "Card and presentation behavior.", ("netizen_cli/cards/**", "tests/*card*.py")),
    Area("migrations", "automated", "Migration behavior and frozen fixtures.",
         ("netizen_cli/database_migrations.py", "netizen_cli/migrations/**",
          "tests/*migration*.py", "tests/fixtures/channel_*.sql")),
    Area("runtime", "automated", "Runtime behavior and native isolated regression.", ("netizen_cli/**",)),
    Area("docs", "automated", "Documentation and packaged Skill content.", ("docs/**", "*.md", "skills/**")),
    Area("other", "automated", "Repository CI.", ("*",)),
)


def classify(paths: Sequence[str], contents: Mapping[str, tuple[str, str]]) -> dict[str, list[str]]:
    """Classify committed changes using only the supplied paths and endpoint texts."""
    triggered: dict[str, list[str]] = {}
    changed_workflows = changed_workflow_versions(contents)
    for path in sorted(paths):
        manual = False
        for area in AREAS:
            if manual and area.kind == "automated":
                break
            if area.matches(path, contents, changed_workflows):
                triggered.setdefault(area.name, []).append(path)
                if area.kind == "automated":
                    break
                manual = True
    return triggered


@dataclass(frozen=True)
class CommitEntry:
    sha: str
    subject: str
    body: str
    pr: int | None


def parse_log(raw: str) -> list[CommitEntry]:
    """Parse ``LOG_FORMAT`` records and attribute commits to merge PRs.

    ``git log`` yields newest first, so a merge commit precedes the branch
    commits it merged; commits pushed directly to main carry no PR number.
    """
    entries: list[CommitEntry] = []
    current_pr: int | None = None
    for record in raw.split("\x1e"):
        if not record.strip():
            continue
        sha, subject, body = record.strip("\n").split("\x1f", 2)
        merge = MERGE_SUBJECT_RE.match(subject)
        if merge:
            current_pr = int(merge.group(1))
            continue
        entries.append(
            CommitEntry(sha=sha, subject=subject, body=body, pr=current_pr)
        )
    return entries


def next_version(previous_tag: str, entries: Sequence[CommitEntry]) -> str:
    """Derive the next tag: feat → minor, anything else → patch.

    A breaking change marker never resolves automatically; it escalates so
    the maintainer chooses the version.
    """
    previous = TAG_RE.fullmatch(previous_tag)
    if previous is None:
        raise ReleaseError(f"previous tag {previous_tag!r} is not vX.Y.Z")
    breaking: list[str] = []
    has_feature = False
    for entry in entries:
        conventional = CONVENTIONAL_RE.match(entry.subject)
        if conventional is not None:
            if conventional.group("breaking"):
                breaking.append(entry.subject)
            if conventional.group("type") == "feat":
                has_feature = True
        if "BREAKING CHANGE:" in entry.body or "BREAKING CHANGES:" in entry.body:
            breaking.append(entry.subject)
    if breaking:
        raise ReleaseEscalation(
            "breaking change(s) in "
            f"{previous_tag}..HEAD require a maintainer-chosen version; "
            f"rerun with --version: {'; '.join(breaking)}"
        )
    major, minor, patch = (int(part) for part in previous.groups())
    if has_feature:
        return f"v{major}.{minor + 1}.0"
    return f"v{major}.{minor}.{patch + 1}"


def render_notes(
    repository: str, previous_tag: str, tag: str, entries: Sequence[CommitEntry],
    records: Mapping[str, str] | None = None, *, ci_run: str | None = None,
) -> str:
    groups: dict[str, list[str]] = {}
    for entry in entries:
        conventional = CONVENTIONAL_RE.match(entry.subject)
        heading = TYPE_HEADINGS.get(
            conventional.group("type") if conventional else "", "Other Changes"
        )
        suffix = f" (#{entry.pr})" if entry.pr else ""
        groups.setdefault(heading, []).append(f"- {entry.subject}{suffix}")
    lines = ["## What's Changed", ""]
    for heading in SECTION_ORDER:
        items = groups.get(heading)
        if not items:
            continue
        lines.append(f"### {heading}")
        lines.extend(items)
        lines.append("")
    lines.append(
        f"**Full Changelog**: https://github.com/{repository}/compare/"
        f"{previous_tag}...{tag}"
    )
    lines.extend(["", "## Verification", ""])
    ci = f"[Exact main CI]({ci_run}) passed" if ci_run else "Exact main CI required"
    lines.append(f"- {ci}: {CI_COVERAGE}.")
    for area in AREAS:
        if records and area.name in records:
            lines.append(f"- {area.name}: {records[area.name]}")
    return "\n".join(lines) + "\n"


def bump_text(text: str, old: str, new: str) -> str:
    count = text.count(old)
    if count != 1:
        raise ReleaseError(f"expected exactly one {old!r}, found {count}")
    return text.replace(old, new)


def needs_version_bump(previous_tag: str, tag: str) -> bool:
    """Accept only a consistent previous version or fully prepared target."""
    versions: set[str] = set()
    for relative_path, template in VERSION_FILES:
        content = (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")
        pattern = re.escape(template).replace(
            re.escape("{version}"), r"(\d+\.\d+\.\d+)"
        )
        matches = re.findall(f"^{pattern}$", content, re.MULTILINE)
        if len(matches) != 1:
            raise ReleaseError(
                f"{relative_path}: expected exactly one version anchor, "
                f"found {len(matches)}"
            )
        # Retain the bump's unique textual-anchor gate before creating a branch.
        anchor = template.format(version=matches[0])
        bump_text(content, anchor, anchor)
        versions.add(matches[0])
    if versions == {previous_tag[1:]}:
        return True
    if versions == {tag[1:]}:
        return False
    raise ReleaseError(
        f"version files must all contain {previous_tag[1:]} or {tag[1:]}; "
        f"found {sorted(versions)}"
    )


def summarize_rollup(rollup: Sequence[Mapping[str, object]]) -> str:
    """Reduce a PR's statusCheckRollup to passed/pending/failed.

    Entries mix check runs (``status``/``conclusion``) and commit statuses
    (``state``); case is normalised because gh emits GraphQL upper case.
    """
    if not rollup:
        return "pending"
    outcomes: list[str] = []
    for check in rollup:
        conclusion = str(check.get("conclusion") or "").upper()
        state = str(check.get("state") or "").upper()
        if conclusion:
            if conclusion in {"SUCCESS", "NEUTRAL", "SKIPPED"}:
                outcomes.append("passed")
            else:
                outcomes.append("failed")
        elif state in {"SUCCESS", "NEUTRAL", "SKIPPED"}:
            outcomes.append("passed")
        elif state in {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED"}:
            outcomes.append("failed")
        else:
            outcomes.append("pending")
    if "failed" in outcomes:
        return "failed"
    if "pending" in outcomes:
        return "pending"
    return "passed"


def version_key(tag: str) -> tuple[int, int, int]:
    match = TAG_RE.fullmatch(tag)
    if match is None:
        raise ReleaseError(f"tag {tag!r} is not vX.Y.Z")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def validate_explicit_version(previous_tag: str, candidate: str) -> str:
    if TAG_RE.fullmatch(candidate) is None:
        raise ReleaseError(f"--version {candidate!r} is not vX.Y.Z")
    if version_key(candidate) <= version_key(previous_tag):
        raise ReleaseError(
            f"--version {candidate} must be newer than {previous_tag}"
        )
    return candidate


def git(*arguments: str) -> str:
    # Range inspection must stay offline even in a partial clone. Missing
    # objects require the maintainer to refresh the checkout before planning.
    offline = arguments[0] in {"rev-parse", "diff", "show", "ls-tree"} or arguments[:2] == ("tag", "-l")
    result = subprocess.run(
        ["git", "-C", str(REPOSITORY_ROOT), *arguments],
        env={**os.environ, "GIT_ALLOW_PROTOCOL": "", "GIT_NO_LAZY_FETCH": "1"} if offline else None,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ReleaseError(
            f"git {' '.join(arguments)} failed: {result.stderr.strip()}"
        )
    return result.stdout


def gh(*arguments: str) -> str:
    environment = {**os.environ, "GH_PROMPT_DISABLED": "1"}
    result = subprocess.run(
        ["gh", *arguments],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
        cwd=REPOSITORY_ROOT,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise ReleaseError(
            f"gh {' '.join(arguments)} failed: {result.stderr.strip()}"
        )
    return result.stdout


def require_clean_release_base() -> None:
    if git("status", "--porcelain", "--untracked-files=all").strip():
        raise ReleaseError("working tree is not clean")
    branch = git("rev-parse", "--abbrev-ref", "HEAD").strip()
    if branch != "main":
        raise ReleaseError(f"expected branch main, found {branch}")
    git("fetch", "--quiet", "origin", "main")
    git("fetch", "--quiet", "--tags", "origin")
    head = git("rev-parse", "HEAD").strip()
    remote = git("rev-parse", "origin/main").strip()
    if head != remote:
        raise ReleaseError("local main does not match origin/main; pull first")


def latest_tag() -> str:
    tags = [
        tag
        for tag in git("tag", "-l", "v*").split()
        if TAG_RE.fullmatch(tag)
    ]
    if not tags:
        raise ReleaseError("no previous vX.Y.Z tag found")
    return sorted(tags, key=version_key)[-1]


def range_areas(previous_tag: str, head: str) -> dict[str, list[str]]:
    # --no-renames reports both paths across a move. Read blobs only for
    # content-sensitive rules; an absent endpoint represents an add/delete.
    raw = git("diff", "--no-ext-diff", "--no-renames", "--name-status", "-z",
              f"{previous_tag}..{head}", "--")
    fields = iter(raw.rstrip("\0").split("\0") if raw else ())
    paths = []
    contents = {}
    selected = {path for area in AREAS for path, _ in area.content}
    for status, path in zip(fields, fields, strict=True):
        paths.append(path)
        if any(fnmatchcase(path, pattern) for pattern in selected):
            old = "" if status == "A" else git("show", f"{previous_tag}:{path}")
            new = "" if status == "D" else git("show", f"{head}:{path}")
            contents[path] = (old, new)
    if any(workflow_file(path) for path in paths):
        # A changed workflow can reuse a version from an unchanged sibling.
        existing = git("ls-tree", "-r", "--name-only", "-z", previous_tag, "--", ".github/workflows")
        for path in existing.split("\0"):
            if workflow_file(path) and path not in contents:
                source = git("show", f"{previous_tag}:{path}")
                contents[path] = (source, source)
    try:
        return classify(paths, contents)
    except (tomllib.TOMLDecodeError, SyntaxError) as error:
        raise ReleaseError(f"cannot classify {previous_tag}..{head}: {error}") from error


def verification_records(
    triggered: Mapping[str, list[str]], verified: Sequence[str], accept_risk: Sequence[str],
) -> dict[str, str]:
    records = {}
    areas = {area.name: area for area in AREAS}
    for flag, label, arguments in (
        ("--verified", "verified", verified), ("--accept-risk", "risk accepted", accept_risk)
    ):
        for argument in arguments:
            name, _, note = argument.partition("=")
            if name not in areas:
                raise ReleaseEscalation(f"{flag}: unknown area {name!r}")
            if name not in triggered or areas[name].kind != "manual":
                raise ReleaseEscalation(f"{flag}: {name} is not a triggered manual area")
            if name in records:
                raise ReleaseEscalation(f"duplicate disposition for {name}; give exactly one flag")
            if not note.strip() or not note.isprintable() or len(note) > 500:
                raise ReleaseEscalation(f"{flag} {name} requires 1–500 printable single-line characters")
            records[name] = f"{label} — {note.strip()}"
    return records


def flag_choices(area: Area) -> str:
    return (f'--verified {area.name}="what was checked, where" or '
            f'--accept-risk {area.name}="reason and re-evaluation trigger"')


def format_plan(previous_tag: str, head: str, triggered: Mapping[str, list[str]],
                records: Mapping[str, str], missing: Sequence[Area]) -> str:
    lines = [f"Release verification plan for {previous_tag}..HEAD ({head})",
             f"Required for release: exact main CI ({CI_COVERAGE}) and isolated wheel/sdist validation."]
    for area in AREAS:
        if area.name in triggered:
            lines.append(f"[{area.kind}] {area.name}: {area.requirement}")
            lines.extend(f"  {path}" for path in triggered[area.name])
            if area.name in records:
                lines.append(f"  {records[area.name]}")
    lines.append("Still required:" if missing else "No verification flags outstanding.")
    lines.extend(f"  {flag_choices(area)}" for area in missing)
    return "\n".join(lines)


def collect_entries(previous_tag: str) -> list[CommitEntry]:
    raw = git("log", f"{previous_tag}..HEAD", f"--pretty=format:{LOG_FORMAT}")
    return parse_log(raw)


def repository() -> str:
    name = json.loads(gh("repo", "view", "--json", "nameWithOwner"))
    return name["nameWithOwner"]


def render_pr_body(
    previous_tag: str, tag: str, derived: bool, notes: str
) -> str:
    origin = (
        f"Version derived from conventional commits in `{previous_tag}..HEAD` "
        "(feat → minor, otherwise patch)."
        if derived
        else "Version specified explicitly by the maintainer."
    )
    return (
        f"{origin}\n\n"
        f"Automated release chain per ADR 0050. The dispatch notes for "
        f"`{tag}` will be:\n\n```\n{notes.strip()}\n```\n"
    )


def create_bump_pull_request(
    previous_tag: str, tag: str, derived: bool, notes: str
) -> int:
    branch = f"codex/release-{tag}"
    git("checkout", "-b", branch)
    for relative_path, template in VERSION_FILES:
        path = REPOSITORY_ROOT / relative_path
        path.write_text(
            bump_text(
                path.read_text(encoding="utf-8"),
                template.format(version=previous_tag[1:], tag=previous_tag),
                template.format(version=tag[1:], tag=tag),
            ),
            encoding="utf-8",
        )
    git("add", *(path for path, _ in VERSION_FILES))
    git("commit", "-m", BUMP_COMMIT_TEMPLATE.format(tag=tag))
    git("push", "--quiet", "-u", "origin", branch)
    url = gh(
        "pr",
        "create",
        "--title",
        BUMP_COMMIT_TEMPLATE.format(tag=tag),
        "--body",
        render_pr_body(previous_tag, tag, derived, notes),
    ).strip()
    match = re.search(r"/pull/(\d+)$", url)
    if match is None:
        raise ReleaseError(f"could not parse pull request URL {url!r}")
    return int(match.group(1))


def wait_for_pull_request_checks(number: int) -> None:
    deadline = time.monotonic() + CHECK_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        state = json.loads(
            gh(
                "pr",
                "view",
                str(number),
                "--json",
                "state,statusCheckRollup",
            )
        )
        if state["state"] != "OPEN":
            raise ReleaseError(
                f"pull request #{number} left OPEN state ({state['state']}) "
                "before its checks finished"
            )
        outcome = summarize_rollup(state["statusCheckRollup"])
        if outcome == "passed":
            return
        if outcome == "failed":
            raise ReleaseError(f"pull request #{number} checks failed")
        time.sleep(POLL_INTERVAL_SECONDS)
    raise ReleaseError(
        f"timed out waiting for pull request #{number} checks"
    )


def merge_pull_request(number: int) -> str:
    gh("pr", "merge", str(number), "--merge", "--delete-branch")
    state = json.loads(
        gh("pr", "view", str(number), "--json", "state,mergeCommit")
    )
    if state["state"] != "MERGED":
        raise ReleaseError(f"pull request #{number} did not merge")
    merge_commit = state.get("mergeCommit")
    if not isinstance(merge_commit, Mapping):
        raise ReleaseError(f"pull request #{number} has no merge commit")
    merge_oid = merge_commit.get("oid")
    if not isinstance(merge_oid, str) or not merge_oid:
        raise ReleaseError(f"pull request #{number} has no merge commit OID")
    if git("rev-parse", "--abbrev-ref", "HEAD").strip() != "main":
        git("checkout", "main")
    git("pull", "--quiet", "--ff-only")
    return merge_oid


def wait_for_main_ci(commit: str) -> str:
    deadline = time.monotonic() + CI_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        runs = json.loads(
            gh(
                "run",
                "list",
                "--workflow",
                CI_WORKFLOW,
                "--branch",
                "main",
                "--commit",
                commit,
                "--event",
                "push",
                "--json",
                "status,conclusion,url",
            )
        )
        successful = next(
            (
                run
                for run in runs
                if run["status"] == "completed"
                and run["conclusion"] == "success"
            ),
            None,
        )
        if successful is not None:
            return str(successful["url"])
        if runs and all(run["status"] == "completed" for run in runs):
            run = runs[0]
            raise ReleaseError(
                f"main CI failed for {commit}: {run['conclusion']} "
                f"({run['url']})"
            )
        time.sleep(POLL_INTERVAL_SECONDS)
    raise ReleaseError(f"timed out waiting for main CI on {commit}")


def dispatch_release_workflow(tag: str, notes_path: Path) -> None:
    gh(
        "workflow",
        "run",
        RELEASE_WORKFLOW,
        "--ref",
        tag,
        "-f",
        f"tag={tag}",
        "-f",
        "publish=true",
        "-F",
        f"notes=@{notes_path}",
    )


def wait_for_release_run(tag: str) -> None:
    deadline = time.monotonic() + RELEASE_RUN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        runs = json.loads(
            gh(
                "run",
                "list",
                "--workflow",
                RELEASE_WORKFLOW,
                "--limit",
                "10",
                "--json",
                "databaseId,status,conclusion,headBranch,url",
            )
        )
        completed = next(
            (
                run
                for run in runs
                if run["headBranch"] == tag and run["status"] == "completed"
            ),
            None,
        )
        if completed is not None:
            if completed["conclusion"] != "success":
                raise ReleaseError(
                    f"release workflow failed: {completed['url']}"
                )
            return
        time.sleep(POLL_INTERVAL_SECONDS)
    raise ReleaseError(f"timed out waiting for the {tag} release run")


def release(explicit_version: str | None, *, plan: bool = False,
            verified: Sequence[str] = (), accept_risk: Sequence[str] = ()) -> str:
    previous_tag = latest_tag()
    head = git("rev-parse", "HEAD").strip()
    triggered = range_areas(previous_tag, head)
    records = verification_records(triggered, verified, accept_risk)
    missing = [area for area in AREAS
               if area.kind == "manual" and area.name in triggered and area.name not in records]
    if plan:
        return format_plan(previous_tag, head, triggered, records, missing)
    if missing:
        details = "\n".join(f"{area.name}: {area.requirement}\n  {flag_choices(area)}" for area in missing)
        raise ReleaseEscalation(f"missing verification for {previous_tag}..HEAD:\n{details}")

    # Even a fetch changes local refs. Check dispositions first, then confirm
    # that the synchronized release base is the one the maintainer reviewed.
    require_clean_release_base()
    if latest_tag() != previous_tag or git("rev-parse", "HEAD").strip() != head:
        raise ReleaseEscalation("release range changed; rerun --plan for the current range")
    entries = collect_entries(previous_tag)
    if explicit_version is not None:
        tag = validate_explicit_version(previous_tag, explicit_version)
        derived = False
    else:
        tag = next_version(previous_tag, entries)
        derived = True
    if git("tag", "-l", tag).strip():
        raise ReleaseError(f"tag {tag} already exists")
    bump_required = needs_version_bump(previous_tag, tag)
    repo = repository()
    print(f"→ releasing {tag} (previous {previous_tag})")

    if bump_required:
        notes = render_notes(repo, previous_tag, tag, entries, records)
        number = create_bump_pull_request(previous_tag, tag, derived, notes)
        prepared_tree = git("rev-parse", "HEAD^{tree}").strip()
        print(f"→ opened pull request #{number}")
        wait_for_pull_request_checks(number)
        merge_commit = merge_pull_request(number)
        if git("rev-parse", f"{merge_commit}^{{tree}}").strip() != prepared_tree:
            raise ReleaseEscalation("merged tree differs from the verified candidate; rerun --plan")
        print(f"→ merged into {merge_commit}")
    else:
        merge_commit = head
        print(f"→ version already prepared on main at {merge_commit}")
    gate = wait_for_main_ci(merge_commit)
    print(f"→ main CI passed: {gate}")
    notes = render_notes(repo, previous_tag, tag, entries, records, ci_run=gate)

    git("tag", "-a", tag, "-m", TAG_MESSAGE_TEMPLATE.format(tag=tag), merge_commit)
    git("push", "--quiet", "origin", tag)
    print(f"→ pushed protected tag {tag}")
    with tempfile.TemporaryDirectory() as temporary:
        notes_path = Path(temporary) / "notes.md"
        notes_path.write_text(notes, encoding="utf-8")
        dispatch_release_workflow(tag, notes_path)
    wait_for_release_run(tag)
    published = json.loads(gh("release", "view", tag, "--json", "url"))
    return str(published["url"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Execute the full release chain after a maintainer "
        "instruction; see ADR 0050."
    )
    parser.add_argument(
        "--version",
        help="explicit vX.Y.Z tag; required when the derivation is ambiguous",
    )
    parser.add_argument("--plan", action="store_true",
                        help="show verification for the committed local range without starting a release")
    parser.add_argument("--verified", action="append", default=[], metavar="AREA=NOTE",
                        help="record what was verified and where for a triggered manual area")
    parser.add_argument("--accept-risk", action="append", default=[], metavar="AREA=NOTE",
                        help="record the residual risk, reason and re-evaluation trigger for a manual area")
    arguments = parser.parse_args(argv)
    try:
        result = release(arguments.version, plan=arguments.plan, verified=arguments.verified,
                         accept_risk=arguments.accept_risk)
    except ReleaseEscalation as error:
        print(f"escalation: {error}", file=sys.stderr)
        return 2
    except ReleaseError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(result if arguments.plan else f"published: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
