from __future__ import annotations

import ast
import hashlib
import io
import json
import os
from pathlib import Path
import pty
import select
import shutil
import subprocess
import tarfile
import tempfile
import textwrap
import time
import unittest

from scripts.build_release_artifact import (
    MANAGED_DIRECTORIES,
    MANAGED_FILES,
    ReleaseBuildError,
    build_release_artifacts,
    collect_source_files,
    render_bootstrap,
    source_digest,
)
from scripts.netizen_installer import read_published_release_manifest


ROOT = Path(__file__).resolve().parents[1]
COMMIT = "0123456789abcdef0123456789abcdef01234567"
REPOSITORY = "lijingda/netizen"
TAG = "v0.3.0"


def _constant_from_module(path: Path, name: str) -> object:
    module = ast.parse(path.read_text(encoding="utf-8"))
    for statement in module.body:
        if isinstance(statement, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == name for target in statement.targets):
                return ast.literal_eval(statement.value)
    raise AssertionError(f"{name} is not a simple module constant in {path}")


def _extract_trusted_test_archive(archive_path: Path, destination: Path) -> None:
    with tarfile.open(archive_path, mode="r:gz") as archive:
        for member in archive.getmembers():
            target = destination.joinpath(*Path(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise AssertionError(f"test archive payload is missing: {member.name}")
            target.write_bytes(source.read())
            target.chmod(0o755 if member.mode & 0o111 else 0o644)


class ReleaseArtifactTests(unittest.TestCase):
    def _source_fixture(self, root: Path) -> None:
        for directory in MANAGED_DIRECTORIES:
            (root / directory).mkdir(parents=True, exist_ok=True)
        for name in MANAGED_FILES:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"fixture for {name}\n", encoding="utf-8")
        (root / "pyproject.toml").write_text(
            textwrap.dedent(
                """\
                [build-system]
                requires = ["setuptools==80.9.0"]
                build-backend = "setuptools.build_meta"

                [project]
                name = "netizen"
                version = "0.3.0"
                """
            ),
            encoding="utf-8",
        )
        (root / "requirements.lock").write_bytes(b"example==1.0\n")
        shutil.copy2(
            ROOT / "deploy" / "install-release.sh.in",
            root / "deploy" / "install-release.sh.in",
        )
        installer = root / "scripts" / "netizen_installer.py"
        installer.write_text(
            textwrap.dedent(
                """\
                import json
                import os
                from pathlib import Path
                import sys

                Path(os.environ["RESULT_PATH"]).write_text(
                    json.dumps({"argv": sys.argv[1:], "stdinIsTty": sys.stdin.isatty()}),
                    encoding="utf-8",
                )
                """
            ),
            encoding="utf-8",
        )
        for executable in ("dev-install.sh", "install.sh", "service.sh", "uninstall.sh"):
            (root / executable).chmod(0o755)

    def _fake_curl(self, directory: Path) -> Path:
        fake_bin = directory / "bin"
        fake_bin.mkdir()
        curl = fake_bin / "curl"
        curl.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json
                import os
                from pathlib import Path
                import shutil
                import sys

                arguments = sys.argv[1:]
                destination = Path(arguments[arguments.index("-o") + 1])
                shutil.copyfile(os.environ["FAKE_ARCHIVE"], destination)
                Path(os.environ["CURL_RESULT_PATH"]).write_text(
                    json.dumps(arguments), encoding="utf-8"
                )
                """
            ),
            encoding="utf-8",
        )
        curl.chmod(0o755)
        return fake_bin

    def _run_pipe_with_controlling_terminal(
        self,
        script: bytes,
        *,
        cwd: Path,
        environment: dict[str, str],
    ) -> tuple[int, bytes]:
        read_descriptor, write_descriptor = os.pipe()
        process_id, terminal = pty.fork()
        if process_id == 0:  # pragma: no cover - assertions run in the parent
            try:
                os.close(write_descriptor)
                os.dup2(read_descriptor, 0)
                if read_descriptor != 0:
                    os.close(read_descriptor)
                os.chdir(cwd)
                os.execve("/bin/sh", ["/bin/sh"], environment)
            except BaseException:
                os._exit(127)

        os.close(read_descriptor)
        with os.fdopen(write_descriptor, "wb") as pipe:
            pipe.write(script)
        return self._wait_for_pty_process(process_id, terminal)

    def _run_file_with_controlling_terminal(
        self,
        script: Path,
        *,
        cwd: Path,
        environment: dict[str, str],
    ) -> tuple[int, bytes]:
        process_id, terminal = pty.fork()
        if process_id == 0:  # pragma: no cover - assertions run in the parent
            try:
                os.chdir(cwd)
                os.execve("/bin/sh", ["/bin/sh", str(script)], environment)
            except BaseException:
                os._exit(127)
        return self._wait_for_pty_process(process_id, terminal)

    def _wait_for_pty_process(self, process_id: int, terminal: int) -> tuple[int, bytes]:
        output = bytearray()
        status: int | None = None
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                finished, candidate_status = os.waitpid(process_id, os.WNOHANG)
                if finished:
                    status = candidate_status
                    break
                readable, _, _ = select.select([terminal], [], [], 0.1)
                if readable:
                    try:
                        output.extend(os.read(terminal, 65536))
                    except OSError:
                        pass
            if status is None:
                os.kill(process_id, 9)
                _, status = os.waitpid(process_id, 0)
                self.fail("bootstrap did not finish while connected to /dev/tty")
        finally:
            os.close(terminal)
        return os.waitstatus_to_exitcode(status), bytes(output)

    def test_builder_managed_source_boundary_matches_installer(self) -> None:
        installer = ROOT / "scripts" / "netizen_installer.py"
        self.assertEqual(
            tuple(_constant_from_module(installer, "SOURCE_DIRECTORIES")),
            MANAGED_DIRECTORIES,
        )
        self.assertEqual(
            tuple(_constant_from_module(installer, "SOURCE_FILES")),
            MANAGED_FILES,
        )

    def test_build_is_deterministic_and_manifest_binds_exact_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            source = temporary_path / "source"
            source.mkdir()
            self._source_fixture(source)
            first = build_release_artifacts(
                source,
                temporary_path / "first",
                tag=TAG,
                commit=COMMIT,
                repository=REPOSITORY,
            )
            second = build_release_artifacts(
                source,
                temporary_path / "second",
                tag=TAG,
                commit=COMMIT,
                repository=REPOSITORY,
            )

            self.assertEqual(first.archive.read_bytes(), second.archive.read_bytes())
            self.assertEqual(first.bootstrap.read_bytes(), second.bootstrap.read_bytes())
            self.assertEqual(
                first.archive_sha256,
                hashlib.sha256(first.archive.read_bytes()).hexdigest(),
            )
            self.assertTrue(first.bootstrap.stat().st_mode & 0o100)

            with tarfile.open(first.archive, mode="r:gz") as archive:
                members = archive.getmembers()
                names = {member.name.rstrip("/") for member in members}
                manifest = json.load(
                    archive.extractfile("netizen-v0.3.0/.netizen-release.json")
                )
            self.assertIn("netizen-v0.3.0/tests", names)
            self.assertIn("netizen-v0.3.0/.github/workflows/ci.yml", names)
            self.assertIn("netizen-v0.3.0/scripts/netizen_installer.py", names)
            self.assertIn("netizen-v0.3.0/deploy/install-release.sh.in", names)
            self.assertTrue(all(member.mtime == 0 for member in members))
            self.assertTrue(all(member.uid == 0 and member.gid == 0 for member in members))
            self.assertEqual(
                manifest,
                {
                    "commit": COMMIT,
                    "qualification": "github-release",
                    "requirementsDigest": hashlib.sha256(b"example==1.0\n").hexdigest(),
                    "schema": 1,
                    "sourceDigest": source_digest(collect_source_files(source)),
                    "version": "0.3.0",
                },
            )
            bootstrap = first.bootstrap.read_text(encoding="utf-8")
            self.assertIn("REPOSITORY='lijingda/netizen'", bootstrap)
            self.assertIn("TAG='v0.3.0'", bootstrap)
            self.assertIn("ARCHIVE_NAME='netizen-v0.3.0.tar.gz'", bootstrap)
            self.assertIn(f"ARCHIVE_SHA256='{first.archive_sha256}'", bootstrap)
            self.assertIn(
                "sys.version_info[:2] not in {(3, 11), (3, 12)}", bootstrap
            )

            extracted = temporary_path / "extracted"
            _extract_trusted_test_archive(first.archive, extracted)
            parsed = read_published_release_manifest(extracted / "netizen-v0.3.0")
            self.assertEqual(parsed.version, "0.3.0")
            self.assertEqual(parsed.commit, COMMIT)
            self.assertEqual(parsed.source_digest, manifest["sourceDigest"])

    def test_current_managed_snapshot_contains_release_build_resources(self) -> None:
        names = {source_file.relative for source_file in collect_source_files(ROOT)}
        self.assertIn(".github/workflows/release.yml", names)
        self.assertIn("scripts/build_release_artifact.py", names)
        self.assertIn("deploy/install-release.sh.in", names)
        self.assertIn("tests/test_release_artifact.py", names)

    def test_bootstrap_downloads_exact_asset_and_invokes_release_installer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            source = temporary_path / "source"
            source.mkdir()
            self._source_fixture(source)
            artifacts = build_release_artifacts(
                source,
                temporary_path / "dist",
                tag=TAG,
                commit=COMMIT,
                repository=REPOSITORY,
            )
            fake_bin = self._fake_curl(temporary_path)
            install_result = temporary_path / "install-result.json"
            curl_result = temporary_path / "curl-result.json"
            temp_root = temporary_path / "tmp"
            temp_root.mkdir()
            environment = os.environ.copy()
            environment.update(
                {
                    "CURL_RESULT_PATH": str(curl_result),
                    "FAKE_ARCHIVE": str(artifacts.archive),
                    "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
                    "RESULT_PATH": str(install_result),
                    "TMPDIR": str(temp_root),
                }
            )
            completed = subprocess.run(
                ["/bin/sh", str(artifacts.bootstrap)],
                cwd=temporary_path,
                env=environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            invocation = json.loads(install_result.read_text(encoding="utf-8"))
            self.assertEqual(invocation["argv"][0], "install-release")
            self.assertEqual(Path(invocation["argv"][1]).name, "netizen-v0.3.0")
            self.assertFalse(invocation["stdinIsTty"])
            curl_arguments = json.loads(curl_result.read_text(encoding="utf-8"))
            self.assertEqual(
                curl_arguments[-1],
                "https://github.com/lijingda/netizen/releases/download/"
                "v0.3.0/netizen-v0.3.0.tar.gz",
            )
            self.assertEqual(list(temp_root.iterdir()), [])

    def test_repository_installer_resolves_latest_to_an_exact_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            exact_installer = temporary_path / "exact-install.sh"
            result_path = temporary_path / "latest-result"
            exact_installer.write_text(
                "#!/bin/sh\nprintf installed >\"$LATEST_RESULT_PATH\"\n",
                encoding="utf-8",
            )
            fake_bin = self._fake_curl(temporary_path)
            curl_result = temporary_path / "curl-result.json"
            temp_root = temporary_path / "tmp"
            temp_root.mkdir()
            environment = os.environ.copy()
            environment.update(
                {
                    "CURL_RESULT_PATH": str(curl_result),
                    "FAKE_ARCHIVE": str(exact_installer),
                    "LATEST_RESULT_PATH": str(result_path),
                    "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
                    "TMPDIR": str(temp_root),
                }
            )

            completed = subprocess.run(
                ["/bin/sh", str(ROOT / "install.sh")],
                cwd=temporary_path,
                env=environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(result_path.read_text(encoding="utf-8"), "installed")
            curl_arguments = json.loads(curl_result.read_text(encoding="utf-8"))
            self.assertEqual(
                curl_arguments[-1],
                "https://github.com/lijingda/netizen/releases/latest/download/install.sh",
            )
            self.assertEqual(list(temp_root.iterdir()), [])

    def test_curl_pipe_uses_real_controlling_terminal_for_human_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            source = temporary_path / "source"
            source.mkdir()
            self._source_fixture(source)
            artifacts = build_release_artifacts(
                source,
                temporary_path / "dist",
                tag=TAG,
                commit=COMMIT,
                repository=REPOSITORY,
            )
            fake_bin = self._fake_curl(temporary_path)
            install_result = temporary_path / "install-result.json"
            environment = os.environ.copy()
            environment.update(
                {
                    "CURL_RESULT_PATH": str(temporary_path / "curl-result.json"),
                    "FAKE_ARCHIVE": str(artifacts.archive),
                    "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
                    "RESULT_PATH": str(install_result),
                    "TMPDIR": str(temporary_path),
                }
            )
            return_code, output = self._run_pipe_with_controlling_terminal(
                artifacts.bootstrap.read_bytes(),
                cwd=temporary_path,
                environment=environment,
            )
            self.assertEqual(return_code, 0, output.decode(errors="replace"))
            invocation = json.loads(install_result.read_text(encoding="utf-8"))
            self.assertTrue(invocation["stdinIsTty"])

    def test_downloaded_script_preserves_tty_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            source = temporary_path / "source"
            source.mkdir()
            self._source_fixture(source)
            artifacts = build_release_artifacts(
                source,
                temporary_path / "dist",
                tag=TAG,
                commit=COMMIT,
                repository=REPOSITORY,
            )
            fake_bin = self._fake_curl(temporary_path)
            install_result = temporary_path / "install-result.json"
            environment = os.environ.copy()
            environment.update(
                {
                    "CURL_RESULT_PATH": str(temporary_path / "curl-result.json"),
                    "FAKE_ARCHIVE": str(artifacts.archive),
                    "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
                    "RESULT_PATH": str(install_result),
                    "TMPDIR": str(temporary_path),
                }
            )
            return_code, output = self._run_file_with_controlling_terminal(
                artifacts.bootstrap,
                cwd=temporary_path,
                environment=environment,
            )
            self.assertEqual(return_code, 0, output.decode(errors="replace"))
            invocation = json.loads(install_result.read_text(encoding="utf-8"))
            self.assertTrue(invocation["stdinIsTty"])

    def test_truncated_curl_pipe_has_no_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            fake_bin = self._fake_curl(temporary_path)
            rendered = render_bootstrap(
                (ROOT / "deploy" / "install-release.sh.in").read_text(encoding="utf-8"),
                repository=REPOSITORY,
                tag=TAG,
                archive_name="netizen-v0.3.0.tar.gz",
                archive_sha256="0" * 64,
                version="0.3.0",
            )
            truncated = rendered.rsplit("\n}\n", 1)[0]
            environment = os.environ.copy()
            curl_result = temporary_path / "curl-result.json"
            environment.update(
                {
                    "CURL_RESULT_PATH": str(curl_result),
                    "FAKE_ARCHIVE": str(temporary_path / "unused.tar.gz"),
                    "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
                    "RESULT_PATH": str(temporary_path / "install-result.json"),
                    "TMPDIR": str(temporary_path),
                }
            )
            completed = subprocess.run(
                ["/bin/sh"],
                input=truncated,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertFalse(curl_result.exists())

    def test_bootstrap_rejects_digest_mismatch_before_installer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            source = temporary_path / "source"
            source.mkdir()
            self._source_fixture(source)
            artifacts = build_release_artifacts(
                source,
                temporary_path / "dist",
                tag=TAG,
                commit=COMMIT,
                repository=REPOSITORY,
            )
            corrupt = temporary_path / "corrupt.tar.gz"
            corrupt.write_bytes(artifacts.archive.read_bytes() + b"corrupt")
            fake_bin = self._fake_curl(temporary_path)
            install_result = temporary_path / "install-result.json"
            environment = os.environ.copy()
            environment.update(
                {
                    "CURL_RESULT_PATH": str(temporary_path / "curl-result.json"),
                    "FAKE_ARCHIVE": str(corrupt),
                    "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
                    "RESULT_PATH": str(install_result),
                    "TMPDIR": str(temporary_path),
                }
            )
            completed = subprocess.run(
                ["/bin/sh", str(artifacts.bootstrap)],
                env=environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("SHA-256", completed.stderr)
            self.assertFalse(install_result.exists())

    def test_bootstrap_rejects_archive_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            malicious = temporary_path / "netizen-v0.3.0.tar.gz"
            with tarfile.open(malicious, mode="w:gz") as archive:
                root = tarfile.TarInfo("netizen-v0.3.0/")
                root.type = tarfile.DIRTYPE
                archive.addfile(root)
                payload = b"escape\n"
                member = tarfile.TarInfo("netizen-v0.3.0/../escape")
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
            bootstrap = temporary_path / "install.sh"
            bootstrap.write_text(
                render_bootstrap(
                    (ROOT / "deploy" / "install-release.sh.in").read_text(
                        encoding="utf-8"
                    ),
                    repository=REPOSITORY,
                    tag=TAG,
                    archive_name=malicious.name,
                    archive_sha256=hashlib.sha256(malicious.read_bytes()).hexdigest(),
                    version="0.3.0",
                ),
                encoding="utf-8",
            )
            fake_bin = self._fake_curl(temporary_path)
            environment = os.environ.copy()
            environment.update(
                {
                    "CURL_RESULT_PATH": str(temporary_path / "curl-result.json"),
                    "FAKE_ARCHIVE": str(malicious),
                    "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
                    "RESULT_PATH": str(temporary_path / "install-result.json"),
                    "TMPDIR": str(temporary_path),
                }
            )
            completed = subprocess.run(
                ["/bin/sh", str(bootstrap)],
                env=environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("malformed or unsafe", completed.stderr)
            self.assertFalse((temporary_path / "escape").exists())

    def test_builder_rejects_tag_that_does_not_match_project_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            self._source_fixture(source)
            with self.assertRaisesRegex(ReleaseBuildError, "does not match"):
                build_release_artifacts(
                    source,
                    root / "dist",
                    tag="v0.3.1",
                    commit=COMMIT,
                    repository=REPOSITORY,
                )

    def test_builder_rejects_a_noncanonical_release_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            self._source_fixture(source)
            with self.assertRaisesRegex(ReleaseBuildError, "lijingda/netizen"):
                build_release_artifacts(
                    source,
                    root / "dist",
                    tag=TAG,
                    commit=COMMIT,
                    repository="someone/netizen",
                )

    def test_release_workflow_reuses_exact_main_gate_before_publish(self) -> None:
        workflow_path = ROOT / ".github" / "workflows" / "release.yml"
        self.assertTrue(workflow_path.is_file())
        workflow = workflow_path.read_text(encoding="utf-8")
        self.assertIn("ref: refs/tags/${{ inputs.tag }}", workflow)
        self.assertIn('git rev-parse "refs/tags/${RELEASE_TAG}^{commit}"', workflow)
        self.assertIn("actions: read", workflow)
        self.assertIn("github.rest.actions.listWorkflowRuns", workflow)
        self.assertIn("workflow_id: 'ci.yml'", workflow)
        self.assertIn("head_sha: expectedCommit", workflow)
        self.assertIn("run.head_branch === 'main'", workflow)
        self.assertIn("run.event === 'push'", workflow)
        self.assertIn("run.conclusion === 'success'", workflow)
        self.assertIn("Exact release commit ${expectedCommit}", workflow)
        self.assertNotIn("make check", workflow)
        self.assertNotIn("pip install", workflow)
        self.assertNotIn("probe_python_sdk.py", workflow)
        self.assertNotIn("live_probe_evidence", workflow)
        self.assertNotIn("confirm_publish", workflow)
        self.assertNotIn("qualify-artifact", workflow)
        self.assertNotIn("python: [\"3.11\", \"3.12\"]", workflow)
        self.assertIn("verify-artifact:\n", workflow)
        self.assertIn("needs: [build-artifact, verify-artifact]", workflow)
        self.assertEqual(workflow.count("python scripts/build_release_artifact.py"), 1)
        self.assertIn('hashlib.sha256(archive.read_bytes()).hexdigest()', workflow)
        self.assertIn('manifest["commit"]', workflow)
        self.assertIn("publish-release:\n", workflow)
        self.assertIn("    permissions:\n      contents: write", workflow)
        self.assertIn("environment: published-release", workflow)
        self.assertIn("MAIN_GATE_URL", workflow)
        self.assertIn("createHash('sha256')", workflow)
        self.assertEqual(workflow.count("await requireExactTag();"), 2)
        self.assertIn("github.rest.git.getTag", workflow)
        self.assertIn("Tag ${tag} moved", workflow)

    def test_main_ci_runs_repository_gate_for_supported_python_versions(self) -> None:
        workflow_path = ROOT / ".github" / "workflows" / "ci.yml"
        self.assertTrue(workflow_path.is_file())
        workflow = workflow_path.read_text(encoding="utf-8")
        self.assertIn("pull_request:\n    branches: [main]", workflow)
        self.assertIn("push:\n    branches: [main]", workflow)
        self.assertIn('python: ["3.11", "3.12"]', workflow)
        self.assertIn("permissions:\n  contents: read", workflow)
        self.assertIn("--constraint requirements.lock", workflow)
        self.assertIn("run: make check", workflow)


if __name__ == "__main__":
    unittest.main()
