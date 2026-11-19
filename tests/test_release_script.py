from __future__ import annotations

import json
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import call, patch

from scripts.release import (
    VERSION_FILES,
    CommitEntry,
    ReleaseEscalation,
    ReleaseError,
    bump_text,
    classify,
    dispatch_release_workflow,
    main,
    merge_pull_request,
    next_version,
    needs_version_bump,
    parse_log,
    range_areas,
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


class VerificationAreasTest(unittest.TestCase):
    def test_path_boundaries_and_automated_fallback(self) -> None:
        for path, areas in (
            ("netizen_cli/cli_update_restore.py", {"platform-service"}),
            ("netizen_cli/deployment/restart_worker.py", {"platform-service"}),
            ("config.example.yaml", {"other"}),
            ("netizen_cli/builtin_skills.py", {"skills-loading"}),
            ("scripts/feishu_app_permissions.py", {"feishu-app-setup"}),
            ("netizen_cli/cli_setup.py", {"feishu-app-setup"}),
            ("netizen_cli/lark_app.py", {"feishu-app-setup"}),
            ("netizen_cli/feishu_app_onboarding.py", {"feishu-app-setup"}),
            ("netizen_cli/card_content.py", {"runtime"}),
            ("netizen_cli/message_history.py", {"runtime"}),
            ("netizen_cli/admin/credentials.py", {"admin-login"}),
            ("netizen_cli/admin/login.py", {"admin-login"}),
            ("netizen_cli/admin/static/login.js", {"admin-login"}),
            ("netizen_cli/admin/static/login.html", {"admin"}),
            ("netizen_cli/admin/auth.py", {"admin"}),
            ("netizen_cli/admin/users.py", {"admin"}),
            ("netizen_cli/admin/port_config.py", {"admin"}),
            ("netizen_cli/database_migrations.py", {"migrations"}),
            ("netizen_cli/migrations/v22.py", {"migrations"}),
            (".github/workflows/release.yml", {"release-publisher"}),
            ("netizen_cli/runtime/goals.py", {"runtime"}),
            ("netizen_cli/admin/static/content-preview.css", {"admin"}),
            ("netizen_cli/cards/reply.py", {"cards"}),
            ("tests/fixtures/channel_v21.sql", {"migrations"}),
            ("skills/netizen-lark/SKILL.md", {"docs"}),
            ("Makefile", {"other"}),
        ):
            with self.subTest(path=path):
                self.assertEqual(set(classify([path], {})), areas)

    def test_dependency_changes_by_pin_in_either_manifest(self) -> None:
        for name, area in (
            ("openai-codex", "codex-sdk"), ("openai-codex-cli-bin", "codex-sdk"),
            ("lark-channel-sdk", "feishu-sdk"), ("lark-oapi", "feishu-sdk"),
        ):
            for path, template in (
                ("requirements.lock", "# locked\n{}==1\nother==1"),
                ("pyproject.toml", '[project]\ndependencies = ["{}==1", "other==1"]'),
            ):
                with self.subTest(name=name, path=path):
                    old = template.format(name)
                    new = old.replace(name + "==1", name + "==2")
                    self.assertEqual(classify([path], {path: (old, new)}), {area: [path]})
                    unrelated = old.replace("other==1", "other==2") + "\n# " + name + "==3"
                    self.assertEqual(classify([path], {path: (old, unrelated)}), {"other": [path]})

    def test_python_matrix_and_packaging_fields(self) -> None:
        path = "pyproject.toml"
        for line, area in (
            ('[project]\nrequires-python = "3.13"', "python-matrix"),
            ('[project]\nclassifiers = ["Programming Language :: Python :: 3.13"]', "python-matrix"),
            ('[build-system]\nrequires = ["setuptools==1"]', "platform-service"),
            ('[project.scripts]\nnetizen = "cli:main"', "platform-service"),
            ('[project.gui-scripts]\nnetizen = "cli:main"', "platform-service"),
            ('[project.entry-points.plugins]\nnetizen = "cli:plugin"', "platform-service"),
            ('[project]\ndynamic = ["version"]', "platform-service"),
            ('[tool.setuptools.dynamic]\nversion = {attr = "netizen_cli.__version__"}', "platform-service"),
            ('[tool.setuptools.cmdclass]\nbuild_py = "_build.BuildPy"', "platform-service"),
            ('[tool.setuptools.packages.find]\ninclude = ["netizen*"]', "other"),
            ('[tool.setuptools]\npackages = ["netizen_cli"]', "other"),
            ('[project]\nversion = "1.0.0"', "other"),
            ('[tool.mypy]\npython_version = "3.13"', "other"),
        ):
            with self.subTest(line=line):
                self.assertEqual(classify([path], {path: ("", line)}), {area: [path]})

        original = '[project]\nrequires-python=">=3.11,<3.15"\nclassifiers=["Programming Language :: Python :: 3.11"]'
        edited = original.replace(">=3.11,<3.15", "<3.15, >=3.11").replace(
            '3.11"]', '3.11", "Development Status :: 4 - Beta"]')
        self.assertEqual(classify([path], {path: (original, edited)}), {"other": [path]})

    def test_package_data_and_discovery_edits_are_routine_but_entry_point_edits_are_manual(self) -> None:
        path = "pyproject.toml"
        old = ('[project.scripts]\nnetizen = "cli:main"\n'
               '[tool.setuptools.packages.find]\ninclude = ["netizen_cli"]\n'
               '[tool.setuptools.package-data]\nnetizen_cli = ["admin/static/*.js"]')
        inventory = old.replace('"admin/static/*.js"', '"admin/static/*.js", "resources/skills/**"')
        self.assertEqual(classify([path], {path: (old, inventory)}), {"other": [path]})
        discovery = old.replace('["netizen_cli"]', '["netizen_cli*"]')
        self.assertEqual(classify([path], {path: (old, discovery)}), {"other": [path]})
        for new in (old, inventory):
            with self.subTest(new=new):
                changed_entry = new.replace("cli:main", "cli:replacement")
                self.assertEqual(classify([path], {path: (old, changed_entry)}), {"platform-service": [path]})

    def test_builder_required_file_inventory_edits_are_routine(self) -> None:
        path = "scripts/build_cli_distribution.py"
        for source in (
            'REQUIRED_WHEEL_FILES = frozenset({\n    "index.html",\n})\n',
            'REQUIRED_SDIST_FILES: list[str] = ["index.html"]\n',
            'def verify_sdist(root):\n    required = {f"{root}/{n}" for n in ("index.html",)}\n',
        ):
            with self.subTest(source=source):
                edited = source.replace('"index.html"', '"index.html", "menu.js"')
                self.assertEqual(classify([path], {path: (source, edited)}), {"other": [path]})

    def test_builder_edits_outside_required_file_lists_are_manual(self) -> None:
        path = "scripts/build_cli_distribution.py"
        old = ('REQUIRED_WHEEL_FILES = frozenset({"index.html"})\n'
               'def verify_sdist(root):\n'
               '    required = {f"{root}/{n}" for n in ("index.html",)}\n'
               '    return required.issubset(files)\n')
        for new in (
            old.replace("issubset", "isdisjoint"),
            old.replace('"index.html"', '"menu.js"').replace("issubset", "isdisjoint"),
            old.replace('"index.html"', 'collect_files()'),
            old.replace('f"{root}/{n}"', 'f"{root}/static/{n}"'),
            old + "# revised build procedure\n",
        ):
            with self.subTest(new=new):
                self.assertEqual(classify([path], {path: (old, new)}), {"platform-service": [path]})

    def test_python_versions_across_ci_workflows(self) -> None:
        path = ".github/workflows/ci.yml"
        old = "jobs:\n  check:\n    strategy:\n      matrix:\n        python:\n          - '3.13'\n"
        self.assertEqual(classify([path], {path: (old, old.replace("3.13", "3.14"))}), {"python-matrix": [path]})
        self.assertEqual(classify([path], {path: (old, old + "    timeout-minutes: 30\n")}), {"other": [path]})

        repeated = old + "  smoke:\n    strategy:\n      matrix:\n        python: ['3.13']\n"
        self.assertEqual(classify([path], {path: (old, repeated)}), {"other": [path]})
        same = repeated + "  python:\n    runs-on: ubuntu-24.04\n    steps:\n      - run: |\n          python-version: 99\n"
        self.assertEqual(classify([path], {path: (old, same)}), {"other": [path]})
        inline = "jobs:\n  smoke:\n    strategy: {matrix: {python: ['3.13', '3.15']}}\n"
        self.assertEqual(classify([path], {path: (old, inline)}), {"python-matrix": [path]})
        sibling = ".github/workflows/browser.yaml"
        step = "jobs:\n  smoke:\n    steps:\n      - uses: actions/setup-python@v7\n        with:\n          python-version: '3.13'\n"
        for endpoints in (("", step), (step, "")):
            self.assertEqual(classify([sibling], {path: (old, old), sibling: endpoints}), {"other": [sibling]})
        self.assertEqual(classify([sibling], {path: (old, old), sibling: ("", step.replace("3.13", "3.15"))}),
                         {"python-matrix": [sibling]})

    def test_range_includes_unchanged_workflows_in_version_union(self) -> None:
        with patch("scripts.release.git") as git_mock:
            git_mock.side_effect = [
                "A\0.github/workflows/extra.yaml\0", "jobs:\n  b:\n    strategy:\n      matrix:\n        python: ['3.13']",
                ".github/workflows/ci.yml\0", "jobs:\n  a:\n    strategy:\n      matrix:\n        python: ['3.13']",
            ]
            self.assertEqual(range_areas("v1.0.0", "sha"), {"other": [".github/workflows/extra.yaml"]})
            self.assertIn(call("show", "v1.0.0:.github/workflows/ci.yml"), git_mock.call_args_list)

    def test_packaging_checks_only_skill_inclusion_rules(self) -> None:
        for path, old, edit, areas in (
            ("MANIFEST.in", "graft skills", "prune skills", {"platform-service", "skills-loading"}),
            ("MANIFEST.in", "graft skills", "graft skills\ninclude config.example.yaml", {"platform-service"}),
            ("_build.py", 'for name in ("guide",):\n copytree(src / "skills" / name, dst)',
             'for name in ("guide", "extra"):\n copytree(src / "skills" / name, dst)', {"platform-service", "skills-loading"}),
            ("_build.py", 'copyfile("config.yaml", dst)', 'copyfile("other.yaml", dst)', {"platform-service"}),
            ("pyproject.toml", "", '[tool.setuptools.exclude-package-data]\nnetizen_cli=["resources/skills/**"]',
             {"platform-service", "skills-loading"}),
        ):
            with self.subTest(path=path, edit=edit):
                self.assertEqual(set(classify([path], {path: (old, edit)})), areas)

    def test_ready_and_skill_registration_in_mixed_modules(self) -> None:
        for path, old, new, areas in (
            ("netizen_cli/main.py", "def _publish_ready_marker(p): pass", "", {"platform-service"}),
            ("netizen_cli/main.py", "class ServiceCore:\n async def start(self): await start_scheduler()",
             "class ServiceCore:\n async def start(self): await start_outcome_mcp()", {"runtime"}),
            ("netizen_cli/main.py", "def show(): pass", "def show(): return 0", {"runtime"}),
            ("netizen_cli/sdk_gap_adapter.py", "class AppServerSkillRoots: pass", "", {"skills-loading"}),
            ("netizen_cli/sdk_gap_adapter.py", "class AppServerGoalControl: pass", "", {"runtime"}),
            ("netizen_cli/main.py", "if _EARLY_LIFETIME_DESCRIPTOR:\n os.set_inheritable(fd, False)", "", {"platform-service"}),
            ("netizen_cli/main.py", "def build_channel(): pass", "def build_channel(): return channel", {"runtime"}),
            ("netizen_cli/admin/web.py", '_SELECTOR_CSP = "a"', '_SELECTOR_CSP = "b"', {"admin"}),
            ("netizen_cli/sdk_gap_adapter.py", "def _initialized_client(): pass", "", {"runtime"}),
        ):
            with self.subTest(path=path, old=old):
                self.assertEqual(set(classify([path], {path: (old, new)})), areas)

    def test_moves_and_deleted_pins_use_both_committed_endpoints(self) -> None:
        with patch("scripts.release.git") as git_mock:
            git_mock.side_effect = [
                "D\0requirements.lock\0A\0locks/new name\0A\0pyproject.toml\0",
                "openai-codex-cli-bin==1\n", '[project]\ndependencies = ["lark-oapi==2"]',
            ]
            self.assertEqual(range_areas("v1.0.0", "sha"), {
                "codex-sdk": ["requirements.lock"], "feishu-sdk": ["pyproject.toml"],
                "other": ["locks/new name"],
            })
            self.assertEqual(git_mock.call_args_list, [
                call("diff", "--no-ext-diff", "--no-renames", "--name-status", "-z", "v1.0.0..sha", "--"),
                call("show", "v1.0.0:requirements.lock"), call("show", "sha:pyproject.toml"),
            ])


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
    def test_plan_allows_missing_evidence_on_a_work_branch_and_only_reads_git(self) -> None:
        # Exercise the CLI down to its git/gh ports without allowing even a fetch.
        responses = {
            ("tag", "-l", "v*"): "v1.0.0\n",
            ("rev-parse", "HEAD"): "local-sha\n",
            ("diff", "--no-ext-diff", "--no-renames", "--name-status", "-z", "v1.0.0..local-sha", "--"):
                "M\0netizen_cli/builtin_skills.py\0M\0README.md\0",
        }
        with (
            patch("scripts.release.git", side_effect=lambda *args: responses[args]),
            patch("scripts.release.gh") as gh_mock,
            redirect_stdout(StringIO()) as out,
        ):
            self.assertEqual(main(["--plan"]), 0)
        for fragment in ("v1.0.0..HEAD", "local-sha", "[manual] skills-loading",
                         "netizen_cli/builtin_skills.py", "[automated] docs", "README.md",
                         '--verified skills-loading="', '--accept-risk skills-loading="'):
            self.assertIn(fragment, out.getvalue())
        gh_mock.assert_not_called()

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
            "require_clean_release_base", "latest_tag", "range_areas", "collect_entries",
            "repository", "needs_version_bump", "create_bump_pull_request",
            "wait_for_pull_request_checks", "merge_pull_request",
            "wait_for_main_ci", "dispatch_release_workflow",
            "wait_for_release_run", "gh", "git",
        ):
            self.mocks[name] = self.enterContext(patch(f"scripts.release.{name}"))
        self.mocks["latest_tag"].return_value = "v0.9.1"
        self.mocks["range_areas"].return_value = {"docs": ["README.md"]}
        self.mocks["collect_entries"].return_value = [entry("feat: CLI")]
        self.mocks["repository"].return_value = "lijingda/netizen"
        self.mocks["create_bump_pull_request"].return_value = 42
        self.mocks["merge_pull_request"].return_value = "merge-oid"
        self.mocks["git"].side_effect = lambda *args: (
            "prepared-main\n" if args == ("rev-parse", "HEAD") else ""
        )
        self.mocks["gh"].return_value = json.dumps({"url": "release-url"})
        self.mocks["wait_for_main_ci"].return_value = "https://example.invalid/main-ci"

    def test_missing_evidence_exits_two_before_fetch_or_release_writes(self) -> None:
        self.mocks["range_areas"].return_value = {"platform-service": ["netizen_cli/cli.py"],
                                                   "codex-sdk": ["requirements.lock"]}
        with redirect_stderr(StringIO()) as err:
            self.assertEqual(main([]), 2)
        for area in ("platform-service", "codex-sdk"):
            self.assertIn(f"--verified {area}=", err.getvalue())
            self.assertIn(f"--accept-risk {area}=", err.getvalue())
        self.mocks["require_clean_release_base"].assert_not_called()
        self.mocks["git"].assert_called_once_with("rev-parse", "HEAD")
        for name in ("gh", "create_bump_pull_request", "dispatch_release_workflow"):
            self.mocks[name].assert_not_called()

    def test_rejects_stale_unknown_duplicate_and_invalid_records(self) -> None:
        self.mocks["range_areas"].return_value = {"codex-sdk": ["requirements.lock"], "docs": ["README.md"]}
        for args in (
            ["--verified", "codx-sdk=checked"], ["--accept-risk", "feishu-sdk=stale"],
            ["--verified", "docs=automated"], ["--verified", "codex-sdk= "],
            ["--accept-risk", "codex-sdk"], ["--verified", "codex-sdk=x\n"],
            ["--verified", "codex-sdk=x\ry"], ["--accept-risk", "codex-sdk=" + "x" * 501],
            ["--verified", "codex-sdk=a", "--verified", "codex-sdk=b"],
            ["--accept-risk", "codex-sdk=a", "--accept-risk", "codex-sdk=b"],
            ["--verified", "codex-sdk=a", "--accept-risk", "codex-sdk=b"],
        ):
            with self.subTest(args=args), redirect_stderr(StringIO()):
                self.assertEqual(main(args), 2)
        self.mocks["require_clean_release_base"].assert_not_called()
        self.mocks["create_bump_pull_request"].assert_not_called()

    def test_plan_with_partial_evidence_lists_only_the_remaining_flags(self) -> None:
        self.mocks["range_areas"].return_value = {
            "codex-sdk": ["requirements.lock"], "platform-service": ["netizen_cli/cli.py"],
        }
        with redirect_stdout(StringIO()) as out:
            self.assertEqual(main(["--plan", "--verified", "codex-sdk=review and checks in PR 42"]), 0)
        self.assertIn("verified — review and checks in PR 42", out.getvalue())
        self.assertNotIn("--verified codex-sdk=", out.getvalue())
        self.assertIn("--accept-risk platform-service=", out.getvalue())
        self.mocks["require_clean_release_base"].assert_not_called()

    def test_dispatch_notes_record_ci_and_each_disposition_in_table_order(self) -> None:
        self.mocks["range_areas"].return_value = {"codex-sdk": ["requirements.lock"],
                                                 "feishu-sdk": ["requirements.lock"]}
        self.mocks["needs_version_bump"].return_value = False
        notes = []
        self.mocks["dispatch_release_workflow"].side_effect = lambda tag, path: notes.append(path.read_text())
        release(None, verified=["feishu-sdk=desktop callback in test tenant"],
                accept_risk=["codex-sdk=provider unavailable, repeat when enabled"])
        self.assertIn("## Verification\n", notes[0])
        self.assertIn("[Exact main CI](https://example.invalid/main-ci) passed", notes[0])
        for gate in ("make check (", "make check-native", "make check-browser"):
            self.assertIn(gate, notes[0])
        self.assertIn("codex-sdk: risk accepted — provider unavailable, repeat when enabled\n"
                      "- feishu-sdk: verified — desktop callback in test tenant", notes[0])

    def test_refreshed_tag_or_concurrent_merge_requires_new_plan(self) -> None:
        self.mocks["latest_tag"].side_effect = ["v0.9.1", "v0.9.2"]
        with self.assertRaises(ReleaseEscalation):
            release(None)
        self.mocks["create_bump_pull_request"].assert_not_called()
        self.mocks["latest_tag"].side_effect = None
        self.mocks["needs_version_bump"].return_value = True
        self.mocks["git"].side_effect = lambda *args: "other-tree" if args[-1] == "merge-oid^{tree}" else ""
        with self.assertRaises(ReleaseEscalation):
            release(None)
        self.mocks["wait_for_main_ci"].assert_not_called()
        self.mocks["dispatch_release_workflow"].assert_not_called()

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
        self.assertEqual(self.mocks["git"].call_args_list, [
            call("rev-parse", "HEAD"), call("rev-parse", "HEAD"), call("tag", "-l", "v0.10.0"),
        ])
        self.mocks["dispatch_release_workflow"].assert_not_called()

    def test_prepared_candidate_failed_ci_never_pushes_tag_or_publishes(self) -> None:
        self.mocks["needs_version_bump"].return_value = False
        self.mocks["wait_for_main_ci"].side_effect = ReleaseError("CI failed")
        with self.assertRaises(ReleaseError):
            release("v0.10.0")
        self.assertEqual(
            self.mocks["git"].call_args_list,
            [call("rev-parse", "HEAD"), call("rev-parse", "HEAD"), call("tag", "-l", "v0.10.0")],
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
