from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from scripts.release import (
    VERSION_FILES,
    CommitEntry,
    ReleaseEscalation,
    ReleaseError,
    bump_text,
    dispatch_release_workflow,
    merge_pull_request,
    next_version,
    needs_version_bump,
    parse_log,
    release,
    render_notes,
    summarize_rollup,
    validate_explicit_version,
    wait_for_main_ci,
)


ROOT = Path(__file__).resolve().parents[1]

LOG_RAW = (
    "merge1\x1fMerge pull request #14 from lijingda/codex/one\x1f\n\x1e\n"
    "sha1\x1ffeat: integrate side turn reply cards\x1f\n\x1e\n"
    "merge2\x1fMerge pull request #13 from lijingda/codex/two\x1f\n\x1e\n"
    "sha2\x1ffix: report audit failures\x1f\n\x1e\n"
    "sha3\x1fdocs: document scope checks\x1f\n\x1e\n"
    "sha4\x1fReconcile compatibility contracts\x1f\n\x1e\n"
)


class ParseLogTest(unittest.TestCase):
    def test_attributes_commits_to_their_merge_pull_requests(self) -> None:
        entries = parse_log(LOG_RAW)

        self.assertEqual(
            [(entry.subject, entry.pr) for entry in entries],
            [
                ("feat: integrate side turn reply cards", 14),
                ("fix: report audit failures", 13),
                ("docs: document scope checks", 13),
                ("Reconcile compatibility contracts", 13),
            ],
        )

    def test_preserves_commit_bodies(self) -> None:
        raw = (
            "sha1\x1ffeat: something\x1f\nBREAKING CHANGE: behavior\n\x1e\n"
        )

        self.assertIn(
            "BREAKING CHANGE: behavior", parse_log(raw)[0].body
        )


class NextVersionTest(unittest.TestCase):
    def test_feature_bumps_the_minor_version(self) -> None:
        entries = [entry("feat: add cards"), entry("fix: tighten gate")]

        self.assertEqual(next_version("v0.3.3", entries), "v0.4.0")

    def test_without_features_bumps_the_patch_version(self) -> None:
        entries = [entry("fix: tighten gate"), entry("docs: clarify")]

        self.assertEqual(next_version("v0.3.3", entries), "v0.3.4")

    def test_non_conventional_subject_counts_as_patch(self) -> None:
        entries = [entry("Reconcile compatibility contracts")]

        self.assertEqual(next_version("v1.2.3", entries), "v1.2.4")

    def test_breaking_subject_marker_escalates(self) -> None:
        with self.assertRaises(ReleaseEscalation):
            next_version("v0.3.3", [entry("feat!: change everything")])

    def test_breaking_footer_escalates(self) -> None:
        breaking = CommitEntry(
            sha="sha", subject="fix: tighten gate",
            body="BREAKING CHANGE: behavior", pr=None,
        )

        with self.assertRaises(ReleaseEscalation):
            next_version("v0.3.3", [breaking])

    def test_scoped_features_and_fixes(self) -> None:
        entries = [entry("feat(admin): add panel"), entry("fix(core): patch")]

        self.assertEqual(next_version("v0.3.3", entries), "v0.4.0")

    def test_rejects_malformed_previous_tag(self) -> None:
        with self.assertRaises(ReleaseError):
            next_version("0.3.3", [])


class RenderNotesTest(unittest.TestCase):
    def test_groups_by_type_skips_empty_sections_and_links_prs(self) -> None:
        entries = [
            CommitEntry("sha1", "feat: integrate cards", "", 13),
            CommitEntry("sha2", "fix: report audit failures", "", 11),
            CommitEntry("sha3", "docs: document checks", "", 11),
            CommitEntry("sha4", "Reconcile contracts", "", 11),
            CommitEntry("sha5", "pushed directly", "", None),
        ]

        notes = render_notes("lijingda/netizen", "v0.3.3", "v0.4.0", entries)

        self.assertIn("### Features\n- feat: integrate cards (#13)", notes)
        self.assertIn("### Bug Fixes\n- fix: report audit failures (#11)", notes)
        self.assertIn("### Documentation\n- docs: document checks (#11)", notes)
        self.assertIn("### Other Changes\n- Reconcile contracts (#11)", notes)
        self.assertIn("- pushed directly\n", notes)
        self.assertEqual(notes.count("### "), 4)
        self.assertIn(
            "**Full Changelog**: https://github.com/lijingda/netizen/"
            "compare/v0.3.3...v0.4.0",
            notes,
        )
        self.assertTrue(notes.startswith("## What's Changed"))

    def test_section_order_is_stable(self) -> None:
        entries = [
            CommitEntry("sha", "docs: document checks", "", None),
            CommitEntry("sha", "fix: patch", "", None),
            CommitEntry("sha", "feat: feature", "", None),
        ]

        notes = render_notes("o/r", "v0.1.0", "v0.2.0", entries)

        self.assertLess(notes.index("### Features"), notes.index("### Bug Fixes"))
        self.assertLess(notes.index("### Bug Fixes"), notes.index("### Documentation"))
        self.assertNotIn("### Other Changes", notes)


class BumpTextTest(unittest.TestCase):
    def test_replaces_the_single_anchor(self) -> None:
        self.assertEqual(
            bump_text('version = "0.3.3"\n', 'version = "0.3.3"', 'version = "0.4.0"'),
            'version = "0.4.0"\n',
        )

    def test_missing_anchor_fails_closed(self) -> None:
        with self.assertRaises(ReleaseError):
            bump_text('version = "0.4.0"\n', 'version = "0.3.3"', 'version = "0.4.0"')

    def test_ambiguous_anchor_fails_closed(self) -> None:
        with self.assertRaises(ReleaseError):
            bump_text("x = 1\nx = 1\n", "x = 1", "x = 2")


class PreparedVersionTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temporary)
        self.enterContext(patch("scripts.release.REPOSITORY_ROOT", self.root))

    def write_versions(self, versions: tuple[str, str]) -> None:
        for (relative_path, template), version in zip(VERSION_FILES, versions):
            path = self.root / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(template.format(version=version) + "\n")

    def test_previous_version_requires_a_bump(self) -> None:
        self.write_versions(("0.9.1", "0.9.1"))
        self.assertTrue(needs_version_bump("v0.9.1", "v0.10.0"))

    def test_prepared_version_reuses_main(self) -> None:
        self.write_versions(("0.10.0", "0.10.0"))
        self.assertFalse(needs_version_bump("v0.9.1", "v0.10.0"))

    def test_mixed_and_unexpected_versions_fail_closed(self) -> None:
        for versions in [("0.9.1", "0.10.0"), ("0.9.2", "0.9.2")]:
            with self.subTest(versions=versions):
                self.write_versions(versions)
                with self.assertRaises(ReleaseError):
                    needs_version_bump("v0.9.1", "v0.10.0")

    def test_missing_or_duplicate_version_anchors_fail_closed(self) -> None:
        self.write_versions(("0.10.0", "0.10.0"))
        path = self.root / VERSION_FILES[0][0]
        for content in ["", 'version = "0.10.0"\nversion = "0.9.2"\n']:
            with self.subTest(content=content):
                path.write_text(content)
                with self.assertRaises(ReleaseError):
                    needs_version_bump("v0.9.1", "v0.10.0")


class SummarizeRollupTest(unittest.TestCase):
    def test_empty_rollup_is_pending(self) -> None:
        self.assertEqual(summarize_rollup([]), "pending")

    def test_check_run_shapes(self) -> None:
        self.assertEqual(
            summarize_rollup(
                [{"status": "COMPLETED", "conclusion": "SUCCESS"},
                 {"status": "COMPLETED", "conclusion": "NEUTRAL"}]
            ),
            "passed",
        )
        self.assertEqual(
            summarize_rollup([{"status": "IN_PROGRESS"}]), "pending"
        )
        self.assertEqual(
            summarize_rollup(
                [{"status": "COMPLETED", "conclusion": "SUCCESS"},
                 {"status": "COMPLETED", "conclusion": "FAILURE"}]
            ),
            "failed",
        )

    def test_commit_status_shapes(self) -> None:
        self.assertEqual(summarize_rollup([{"state": "SUCCESS"}]), "passed")
        self.assertEqual(summarize_rollup([{"state": "PENDING"}]), "pending")
        self.assertEqual(summarize_rollup([{"state": "ERROR"}]), "failed")


class ValidateExplicitVersionTest(unittest.TestCase):
    def test_accepts_newer_valid_tag(self) -> None:
        self.assertEqual(
            validate_explicit_version("v0.3.3", "v0.4.0"), "v0.4.0"
        )

    def test_rejects_malformed_and_stale_tags(self) -> None:
        with self.assertRaises(ReleaseError):
            validate_explicit_version("v0.3.3", "0.4.0")
        with self.assertRaises(ReleaseError):
            validate_explicit_version("v0.3.3", "v0.3.3")
        with self.assertRaises(ReleaseError):
            validate_explicit_version("v0.3.3", "v0.3.2")


class ReleaseOrchestrationTest(unittest.TestCase):
    def test_maintainer_release_requests_full_publication_explicitly(self) -> None:
        with patch("scripts.release.gh") as gh_mock:
            dispatch_release_workflow("v1.2.3", Path("/tmp/netizen-notes.md"))
        self.assertEqual(gh_mock.call_args, call(
            "workflow", "run", "release.yml", "--ref", "v1.2.3",
            "-f", "tag=v1.2.3", "-f", "publish=true", "-F", "notes=@/tmp/netizen-notes.md",
        ))

    def test_merge_returns_the_pull_requests_exact_merge_commit(self) -> None:
        with (
            patch("scripts.release.gh") as gh_mock,
            patch("scripts.release.git") as git_mock,
        ):
            gh_mock.side_effect = [
                "",
                json.dumps(
                    {"state": "MERGED", "mergeCommit": {"oid": "merge-oid"}}
                ),
            ]
            git_mock.side_effect = ["release-branch\n", "", ""]

            commit = merge_pull_request(42)

        self.assertEqual(commit, "merge-oid")
        self.assertEqual(
            gh_mock.call_args_list,
            [
                call("pr", "merge", "42", "--merge", "--delete-branch"),
                call("pr", "view", "42", "--json", "state,mergeCommit"),
            ],
        )
        self.assertNotIn(call("rev-parse", "HEAD"), git_mock.call_args_list)

    def test_main_ci_requires_a_push_run_and_accepts_any_success(self) -> None:
        runs = [
            {
                "status": "completed",
                "conclusion": "failure",
                "url": "https://example.invalid/failed",
            },
            {
                "status": "completed",
                "conclusion": "success",
                "url": "https://example.invalid/passed",
            },
        ]
        with patch("scripts.release.gh", return_value=json.dumps(runs)) as gh_mock:
            url = wait_for_main_ci("merge-oid")

        self.assertEqual(url, "https://example.invalid/passed")
        self.assertEqual(
            gh_mock.call_args,
            call(
                "run",
                "list",
                "--workflow",
                "ci.yml",
                "--branch",
                "main",
                "--commit",
                "merge-oid",
                "--event",
                "push",
                "--json",
                "status,conclusion,url",
            ),
        )


class ReleaseCandidateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.mocks = {}
        for name in (
            "require_clean_release_base", "latest_tag", "collect_entries",
            "repository", "needs_version_bump", "create_bump_pull_request",
            "wait_for_pull_request_checks", "merge_pull_request",
            "wait_for_main_ci", "dispatch_release_workflow",
            "wait_for_release_run", "gh", "git",
        ):
            self.mocks[name] = self.enterContext(patch(f"scripts.release.{name}"))
        self.mocks["latest_tag"].return_value = "v0.9.1"
        self.mocks["collect_entries"].return_value = [entry("feat: CLI")]
        self.mocks["repository"].return_value = "lijingda/netizen"
        self.mocks["create_bump_pull_request"].return_value = 42
        self.mocks["merge_pull_request"].return_value = "merge-oid"
        self.mocks["git"].side_effect = lambda *args: (
            "prepared-main\n" if args == ("rev-parse", "HEAD") else ""
        )
        self.mocks["gh"].return_value = json.dumps({"url": "release-url"})

    def test_prepared_candidate_requires_exact_main_ci_before_tagging(self) -> None:
        self.mocks["needs_version_bump"].return_value = False
        self.assertEqual(release("v0.10.0"), "release-url")
        self.mocks["create_bump_pull_request"].assert_not_called()
        self.mocks["wait_for_pull_request_checks"].assert_not_called()
        self.mocks["merge_pull_request"].assert_not_called()
        self.mocks["wait_for_main_ci"].assert_called_once_with("prepared-main")
        self.assertIn(
            call("tag", "-a", "v0.10.0", "-m", "Netizen v0.10.0", "prepared-main"),
            self.mocks["git"].call_args_list,
        )
        self.mocks["dispatch_release_workflow"].assert_called_once()

    def test_previous_version_retains_checked_bump_pull_request(self) -> None:
        self.mocks["needs_version_bump"].return_value = True
        release("v0.10.0")
        self.mocks["create_bump_pull_request"].assert_called_once()
        self.mocks["wait_for_pull_request_checks"].assert_called_once_with(42)
        self.mocks["merge_pull_request"].assert_called_once_with(42)
        self.mocks["wait_for_main_ci"].assert_called_once_with("merge-oid")
        self.assertIn(
            call("tag", "-a", "v0.10.0", "-m", "Netizen v0.10.0", "merge-oid"),
            self.mocks["git"].call_args_list,
        )

    def test_invalid_version_files_stop_before_release_mutations(self) -> None:
        self.mocks["needs_version_bump"].side_effect = ReleaseError("mixed versions")
        with self.assertRaises(ReleaseError):
            release("v0.10.0")
        self.mocks["create_bump_pull_request"].assert_not_called()
        self.mocks["wait_for_main_ci"].assert_not_called()
        self.mocks["git"].assert_called_once_with("tag", "-l", "v0.10.0")
        self.mocks["dispatch_release_workflow"].assert_not_called()

    def test_prepared_candidate_failed_ci_never_pushes_tag_or_publishes(self) -> None:
        self.mocks["needs_version_bump"].return_value = False
        self.mocks["wait_for_main_ci"].side_effect = ReleaseError("CI failed")
        with self.assertRaises(ReleaseError):
            release("v0.10.0")
        self.assertEqual(
            self.mocks["git"].call_args_list,
            [call("tag", "-l", "v0.10.0"), call("rev-parse", "HEAD")],
        )
        self.mocks["dispatch_release_workflow"].assert_not_called()


class ReleaseAnchorTest(unittest.TestCase):
    """Guard the bump anchors and the workflow notes input against drift."""

    def test_version_files_contain_each_anchor_exactly_once(self) -> None:
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        package = (ROOT / "netizen_cli" / "__init__.py").read_text(encoding="utf-8")

        self.assertEqual(len(re.findall(r'^version = "\d+\.\d+\.\d+"$', pyproject, re.M)), 1)
        self.assertEqual(len(re.findall(r'^__version__ = "\d+\.\d+\.\d+"$', package, re.M)), 1)
        self.assertEqual({name for name, _ in VERSION_FILES}, {"pyproject.toml", "netizen_cli/__init__.py"})

    def test_release_workflow_accepts_notes_input(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("notes:", workflow)
        self.assertIn("RELEASE_NOTES: ${{ inputs.notes }}", workflow)


def entry(subject: str) -> CommitEntry:
    return CommitEntry(sha="sha", subject=subject, body="", pr=None)


if __name__ == "__main__":
    unittest.main()
