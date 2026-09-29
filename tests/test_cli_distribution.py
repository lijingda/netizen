from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import yaml

from scripts import build_cli_distribution as distribution


ROOT = Path(__file__).resolve().parents[1]
COMMIT = "a" * 40


class DistributionContractTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="netizen-distribution-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source = self.directory / "source"
        self.source.mkdir()
        (self.source / "netizen_cli").mkdir()
        (self.source / "netizen_cli/__init__.py").write_text('__version__ = "1.2.3"\n')
        (self.source / "pyproject.toml").write_text(
            '[build-system]\nrequires=["setuptools==80.9.0"]\nbuild-backend="setuptools.build_meta"\n'
            '[project]\nname="netizen-cli"\nversion="1.2.3"\n'
            '[project.scripts]\nnetizen="netizen_cli.cli:main"\n'
        )
        self.output = self.directory / "dist"

    def backend(self, source: Path, output: Path, operation: str, python: str) -> None:
        if operation == "build_sdist":
            members = {name: b"fixture" for name in (
                "pyproject.toml", "_build.py", "config.example.yaml", "netizen_cli/cli.py",
                "skills/netizen-lark/SKILL.md", "skills/netizen-user-guide/SKILL.md",
            )}
            members["PKG-INFO"] = b"Metadata-Version: 2.1\nName: netizen-cli\nVersion: 1.2.3\n"
            with tarfile.open(output / "netizen_cli-1.2.3.tar.gz", "w:gz") as archive:
                for name, body in members.items():
                    member = tarfile.TarInfo("netizen_cli-1.2.3/" + name)
                    member.size = len(body)
                    archive.addfile(member, io.BytesIO(body))
        else:
            self.assertNotEqual(source, self.source)  # Wheel must come from extracted sdist.
            self.assertEqual((source / "config.example.yaml").read_bytes(), b"fixture")
            with zipfile.ZipFile(output / "netizen_cli-1.2.3-py3-none-any.whl", "w") as archive:
                for name in distribution.REQUIRED_WHEEL_FILES:
                    archive.writestr(name, b"fixture")
                archive.writestr("netizen_cli-1.2.3.dist-info/METADATA", "Metadata-Version: 2.1\nName: netizen-cli\nVersion: 1.2.3\n")
                archive.writestr("netizen_cli-1.2.3.dist-info/entry_points.txt", "[console_scripts]\nnetizen = netizen_cli.cli:main\n")

    def build(self) -> dict:
        with patch.object(distribution, "_backend", side_effect=self.backend):
            return distribution.build_distribution(self.source, self.output, tag="v1.2.3", commit=COMMIT)

    def test_builds_one_sdist_then_its_wheel_and_manifest(self) -> None:
        manifest = self.build()
        self.assertEqual(set(manifest["files"]), {"netizen_cli-1.2.3.tar.gz", "netizen_cli-1.2.3-py3-none-any.whl"})
        verified = distribution.verify_distribution(self.output, tag="v1.2.3", commit=COMMIT)
        self.assertEqual(verified, manifest)

    def test_never_overwrites_existing_release_assets(self) -> None:
        self.build()
        with self.assertRaisesRegex(distribution.DistributionError, "never overwritten"):
            self.build()

    def test_tag_and_both_version_anchors_must_agree(self) -> None:
        with self.assertRaises(distribution.DistributionError):
            distribution.validate_source(self.source, "v9.9.9", COMMIT)
        (self.source / "netizen_cli/__init__.py").write_text('__version__="9.9.9"\n')
        with self.assertRaisesRegex(distribution.DistributionError, "package version"):
            distribution.validate_source(self.source, "v1.2.3", COMMIT)

    def test_hashes_detect_modified_distributions(self) -> None:
        self.build()
        wheel = next(self.output.glob("*.whl"))
        with wheel.open("ab") as output:
            output.write(b"tampered")
        with self.assertRaisesRegex(distribution.DistributionError, "checksum mismatch"):
            distribution.verify_distribution(self.output, tag="v1.2.3", commit=COMMIT)

    def test_commit_and_manifest_hash_cannot_be_substituted(self) -> None:
        self.build()
        with self.assertRaisesRegex(distribution.DistributionError, "identity"):
            distribution.verify_distribution(self.output, tag="v1.2.3", commit="b" * 40)
        with self.assertRaisesRegex(distribution.DistributionError, "SHA-256"):
            distribution.verify_distribution(self.output, tag="v1.2.3", commit=COMMIT, manifest_sha256="0" * 64)

    def test_unexpected_installer_is_not_a_release_asset(self) -> None:
        self.build()
        (self.output / "install.sh").write_text("legacy")
        with self.assertRaisesRegex(distribution.DistributionError, "unexpected"):
            distribution.verify_distribution(self.output, tag="v1.2.3", commit=COMMIT)

    def test_sdist_traversal_and_links_are_rejected_before_extraction(self) -> None:
        for name, kind in (("../outside", tarfile.REGTYPE), ("root/link", tarfile.SYMTYPE)):
            with self.subTest(name=name):
                archive_path = self.directory / "unsafe.tar.gz"
                with tarfile.open(archive_path, "w:gz") as archive:
                    member = tarfile.TarInfo(name)
                    member.type = kind
                    member.linkname = "/outside"
                    archive.addfile(member, io.BytesIO())
                with self.assertRaises(distribution.DistributionError):
                    distribution.verify_sdist(archive_path, "1.2.3", extract_to=self.directory / "extract")

    def test_wheel_without_runtime_resources_is_rejected(self) -> None:
        archive = self.directory / "incomplete.whl"
        with zipfile.ZipFile(archive, "w") as wheel:
            wheel.writestr("netizen_cli/__init__.py", "")
        with self.assertRaisesRegex(distribution.DistributionError, "runtime resources"):
            distribution.verify_wheel(archive, "1.2.3")


class ReleaseWorkflowContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = yaml.load((ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader)

    def test_manual_trigger_defaults_to_verification_without_a_release(self) -> None:
        workflow = self.workflow
        self.assertEqual(set(workflow["on"]), {"workflow_dispatch"})
        self.assertEqual(workflow["on"]["workflow_dispatch"]["inputs"]["publish"]["default"], "false")
        self.assertEqual(workflow["permissions"]["contents"], "read")
        self.assertNotIn("id-token", workflow["permissions"])
        jobs = workflow["jobs"]
        self.assertEqual(set(jobs), {"build-artifact", "verify-artifact", "prepare-release",
                                     "publish-pypi", "publish-release"})
        # The default dispatch reaches no job capable of creating a draft,
        # uploading release assets, or publishing distributions to PyPI.
        for name in ("prepare-release", "publish-pypi", "publish-release"):
            self.assertEqual(jobs[name]["if"], "inputs.publish")
            self.assertEqual(jobs[name]["environment"], "published-release")
        for name in ("build-artifact", "verify-artifact"):
            self.assertNotIn("if", jobs[name])
            self.assertNotIn("environment", jobs[name])
            permissions = jobs[name].get("permissions", workflow["permissions"])
            self.assertEqual(permissions["contents"], "read")
            self.assertNotIn("id-token", permissions)

    def test_publication_waits_for_verification_and_scopes_oidc_to_pypi(self) -> None:
        jobs = self.workflow["jobs"]
        self.assertEqual(jobs["prepare-release"]["needs"], ["build-artifact", "verify-artifact"])
        self.assertEqual(jobs["publish-pypi"]["needs"],
                         ["build-artifact", "verify-artifact", "prepare-release"])
        self.assertEqual(jobs["publish-release"]["needs"],
                         ["build-artifact", "prepare-release", "publish-pypi"])
        self.assertEqual(jobs["publish-pypi"]["permissions"]["id-token"], "write")
        self.assertEqual(jobs["prepare-release"]["permissions"], {"contents": "write"})
        self.assertEqual(jobs["publish-release"]["permissions"], {"contents": "write"})
        actions = [step.get("uses", "") for step in jobs["publish-pypi"]["steps"]]
        self.assertIn("pypa/gh-action-pypi-publish@release/v1", actions)

    def test_builds_once_and_verifies_the_preserved_distribution_bytes(self) -> None:
        jobs = self.workflow["jobs"]
        build = jobs["build-artifact"]
        self.assertEqual(build["steps"][0]["with"]["ref"], "refs/tags/${{ inputs.tag }}")
        build_steps = [step for step in build["steps"]
                       if "build_cli_distribution.py" in step.get("run", "")]
        self.assertEqual(len(build_steps), 1)
        self.assertNotIn("--verify-only", build_steps[0]["run"])
        preserved = next(step for step in build["steps"]
                         if step.get("uses", "").startswith("actions/upload-artifact@"))
        self.assertEqual(set(preserved["with"]["path"].splitlines()),
                         {"dist/*.whl", "dist/*.tar.gz", "dist/netizen-cli-release.json"})
        verify = jobs["verify-artifact"]
        self.assertEqual(verify["needs"], "build-artifact")
        verification = next(step for step in verify["steps"] if "--verify-only" in step.get("run", ""))
        self.assertIn('--manifest-sha256 "$MANIFEST_SHA256"', verification["run"])
        self.assertEqual(verification["env"]["MANIFEST_SHA256"],
                         "${{ needs.build-artifact.outputs.manifest-sha256 }}")
        commands = "\n".join(step.get("run", "") for step in verify["steps"])
        self.assertIn('git rev-parse HEAD', commands)
        self.assertIn('-I -m netizen_cli --help', commands)
        self.assertIn('cd "$RUNNER_TEMP"', commands)
        self.assertNotIn("make check", commands)  # Reuse exact main qualification.
        self.assertNotIn("probe_python_sdk.py", commands)

    @unittest.skipUnless(shutil.which("node"), "Node.js validates the exact main CI gate")
    def test_only_successful_main_push_for_the_exact_commit_qualifies(self) -> None:
        gate = next(step for step in self.workflow["jobs"]["build-artifact"]["steps"]
                    if step.get("id") == "main-gate")
        self.assertEqual(gate["env"]["RELEASE_COMMIT"], "${{ steps.identity.outputs.commit }}")
        harness = """
const assert = require('assert/strict');
const runGate = new (Object.getPrototypeOf(async function(){}).constructor)(
  'github', 'context', 'core', process.argv[1]);
const good = {head_sha: process.env.RELEASE_COMMIT, head_branch: 'main', event: 'push',
  status: 'completed', conclusion: 'success', html_url: 'https://example.test/main-gate'};
async function check(runs, expected) {
  const outputs = {};
  const github = {rest: {actions: {listWorkflowRuns: async (args) => {
    assert.deepEqual(args, {owner: 'owner', repo: 'repo', workflow_id: 'ci.yml',
      branch: 'main', event: 'push', head_sha: process.env.RELEASE_COMMIT, per_page: 100});
    return {data: {workflow_runs: runs}};
  }}}};
  const attempt = runGate(github, {repo: {owner: 'owner', repo: 'repo'}},
    {setOutput: (key, value) => { outputs[key] = value; }});
  if (expected) {
    await attempt;
    assert.equal(outputs.url, good.html_url);
  } else {
    await assert.rejects(attempt, /no successful main CI run/);
    assert.deepEqual(outputs, {});
  }
}
(async () => {
  await check([], false);
  await check([good], true);
  for (const change of [{head_sha: 'wrong'}, {head_branch: 'feature'},
      {event: 'pull_request'}, {status: 'in_progress'}, {conclusion: 'failure'}]) {
    await check([{...good, ...change}], false);
  }
})().catch((error) => { console.error(error); process.exit(1); });
"""
        subprocess.run(["node", "-e", harness, gate["with"]["script"]],
                       env={**os.environ, "RELEASE_COMMIT": COMMIT},
                       check=True, capture_output=True, text=True)

    def test_each_publication_stage_rechecks_tag_and_keeps_verification_evidence(self) -> None:
        jobs = self.workflow["jobs"]
        for name in ("prepare-release", "publish-pypi", "publish-release"):
            scripts = "\n".join(step.get("with", {}).get("script", "") for step in jobs[name]["steps"])
            self.assertIn("github.rest.git.getRef", scripts)
            self.assertIn("github.rest.git.getTag", scripts)
            self.assertIn("object.sha !== process.env.RELEASE_COMMIT", scripts)
        prepare = next(step for step in jobs["prepare-release"]["steps"] if step.get("id") == "draft")
        self.assertEqual(prepare["env"]["RELEASE_NOTES"], "${{ inputs.notes }}")
        self.assertEqual(prepare["env"]["MAIN_GATE_URL"], "${{ needs.build-artifact.outputs.main-gate-url }}")
        self.assertIn("createHash('sha256')", prepare["with"]["script"])
        self.assertIn("assets are never replaced", prepare["with"]["script"])

    @unittest.skipUnless(shutil.which("node"), "Node.js validates GitHub script syntax")
    def test_github_action_scripts_are_valid_async_javascript(self) -> None:
        workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
        for job in workflow["jobs"].values():
            for step in job["steps"]:
                if step.get("uses", "").startswith("actions/github-script@"):
                    with self.subTest(step=step["name"]):
                        subprocess.run(["node", "-e", "new (Object.getPrototypeOf(async function(){}).constructor)(process.argv[1]);",
                                        step["with"]["script"]], check=True, capture_output=True, text=True)


class ActualCliDistributionTest(unittest.TestCase):
    def test_real_sdist_wheel_build_and_verification(self) -> None:
        with tempfile.TemporaryDirectory(prefix="netizen-release-build-test-") as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            for name in ("pyproject.toml", "_build.py", "MANIFEST.in", "config.example.yaml"):
                shutil.copyfile(ROOT / name, source / name)
            for name in ("netizen_cli", "skills"):
                shutil.copytree(ROOT / name, source / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            import tomllib
            version = tomllib.loads((source / "pyproject.toml").read_text())["project"]["version"]
            manifest = distribution.build_distribution(
                source, root / "dist", tag="v" + version, commit=COMMIT,
                python=os.environ.get("NETIZEN_TEST_BUILD_PYTHON", sys.executable),
            )
            verified = distribution.verify_distribution(root / "dist", tag="v" + version, commit=COMMIT)
            self.assertEqual(verified, manifest)


if __name__ == "__main__":
    unittest.main()
