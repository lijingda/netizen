#!/usr/bin/env python3
"""Execute the full Netizen release chain after a maintainer instruction.

The maintainer decides when to release.  This script then performs the whole
mechanical chain without further human steps: it derives the version from
conventional commits since the previous tag, renders deterministic release
notes, opens the version-bump pull request, waits for its required checks,
merges it, waits for the exact merge commit's main CI, creates and pushes the
protected annotated tag, dispatches the release workflow with the notes, and
follows the workflow to the published release.

Anything needing human judgement fails closed and escalates: a breaking
change in the range, an explicitly chosen version, or any failed gate.  The
script never weakens the release workflow's own integrity checks; a bad run
can at worst publish an extra, fully qualified release.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

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

# Each file must contain its anchor exactly once; the release bump rewrites
# the previous tag's anchor to the new one.  tests/test_release_script.py
# guards these anchors against drift.
VERSION_FILES: tuple[tuple[str, str], ...] = (
    ("pyproject.toml", 'version = "{version}"'),
    ("netizen/__init__.py", '__version__ = "{version}"'),
    ("README.md", "releases/download/{tag}/install.sh"),
    ("docs/deployment.md", "releases/download/{tag}/install.sh"),
)

SECTION_ORDER = ("Features", "Bug Fixes", "Documentation", "Other Changes")
TYPE_HEADINGS = {"feat": "Features", "fix": "Bug Fixes", "docs": "Documentation"}


class ReleaseError(Exception):
    """A gate failed or an invariant broke; stop without further side effects."""


class ReleaseEscalation(ReleaseError):
    """A maintainer decision is required before the release can proceed."""


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
    repository: str, previous_tag: str, tag: str, entries: Sequence[CommitEntry]
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
    return "\n".join(lines) + "\n"


def bump_text(text: str, old: str, new: str) -> str:
    count = text.count(old)
    if count != 1:
        raise ReleaseError(f"expected exactly one {old!r}, found {count}")
    return text.replace(old, new)


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
    result = subprocess.run(
        ["git", "-C", str(REPOSITORY_ROOT), *arguments],
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


def release(explicit_version: str | None) -> str:
    require_clean_release_base()
    previous_tag = latest_tag()
    entries = collect_entries(previous_tag)
    if explicit_version is not None:
        tag = validate_explicit_version(previous_tag, explicit_version)
        derived = False
    else:
        tag = next_version(previous_tag, entries)
        derived = True
    if git("tag", "-l", tag).strip():
        raise ReleaseError(f"tag {tag} already exists")
    notes = render_notes(repository(), previous_tag, tag, entries)
    print(f"→ releasing {tag} (previous {previous_tag})")

    number = create_bump_pull_request(previous_tag, tag, derived, notes)
    print(f"→ opened pull request #{number}")
    wait_for_pull_request_checks(number)
    merge_commit = merge_pull_request(number)
    print(f"→ merged into {merge_commit}")
    gate = wait_for_main_ci(merge_commit)
    print(f"→ main CI passed: {gate}")

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
    arguments = parser.parse_args(argv)
    try:
        url = release(arguments.version)
    except ReleaseEscalation as error:
        print(f"escalation: {error}", file=sys.stderr)
        return 2
    except ReleaseError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"published: {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
