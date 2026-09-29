#!/usr/bin/env python3
"""One-shot macOS GitHub-runner acceptance; BUSINESS FIXTURE, not full Runtime.

Workflow prerequisites (no release/tag workflow):
* Check out cd00eca3c7467873e366564bc75caff61d5ad88c, build its unchanged 0.9.1
  sdist/wheel, retain hashes/source identity. Complete local dependency wheelhouse.
* Create fresh pip venv A under RUNNER_TEMP; install by NAME using --no-index
  --find-links WHEELHOUSE netizen-cli==0.9.1, not a direct wheel path. Keep pip.
* Supply uv 0.12.20, supported Python 3.11-3.14, real runner HOME/GUI domain.
* Run outside checkout. Directory must not exist, all supplied paths are under
  RUNNER_TEMP (Python/console lexical paths preserve their venv identity).
* Upload only DIRECTORY/evidence. Roots contain synthetic app credentials AND
  generated Admin secrets; never upload roots, environment, whole HOME or cache.
* Allow at least 15 minutes plus cleanup; run once, retain first failure.

Usage: python isolated_cli_acceptance.py --directory "$RUNNER_TEMP/acceptance" \
  --python-a "$RUNNER_TEMP/A/bin/python" --console-a "$RUNNER_TEMP/A/bin/netizen" \
  --uv "$RUNNER_TEMP/tools/bin/uv" --wheelhouse "$RUNNER_TEMP/wheels" \
  --wheel-sha256 EXACT_CANDIDATE_WHEEL_SHA256

No inventory/package/worker mocks. Only setup auth helpers and asyncio.run's
exact business run coroutine are substituted. Does not alter installed package
files, initialize SDK, contact Feishu, or read/write global Codex configuration.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import pwd
import signal
import sqlite3
import subprocess
import sys
import time
import zipfile

CANDIDATE = "cd00eca3c7467873e366564bc75caff61d5ad88c"
WORKER_SHA256 = "33f122ba3e4ba179059e9a8ddd063d7ca8a3519adba8f0b97c889fe26e005d45"
VERSION = "0.9.1"
HOOK = "netizen_ci_acceptance_fixture"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n")


def runner_guard():
    require(sys.platform == "darwin" and os.environ.get("GITHUB_ACTIONS") == "true",
            "Only an explicitly selected GitHub-hosted macOS runner is allowed")
    require(os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted", "Refusing self-hosted runner")
    require(os.environ.get("RUNNER_OS") == "macOS", "RUNNER_OS must be macOS")
    require(os.geteuid() != 0, "Do not run as root")
    require(os.environ.get("RUNNER_TEMP"), "RUNNER_TEMP is required")
    temporary = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
    require(Path.home().resolve() == Path(pwd.getpwuid(os.geteuid()).pw_dir).resolve(),
            "HOME must be the real account home")
    require(not os.environ.get("CODEX_HOME"), "Do not supply shared/custom Codex configuration")
    return temporary


def internal(mode, directory, root=None):
    temporary = runner_guard()
    base = Path(directory).resolve(strict=True)
    require(base.is_relative_to(temporary) and base != temporary, "Unsafe acceptance directory")
    allowed = {base / "roots" / name for name in ("a-active", "a-stopped", "b-active", "b-stopped")}
    if mode == "inventory":
        from netizen_cli.cli_services import ServiceManager
        # The production, complete inventory is never narrowed or replaced.
        print(json.dumps([dataclasses.asdict(s) for s in ServiceManager().list_instances()], default=str))
    elif mode == "identity":
        from netizen_cli.cli_packages import _PROBE
        exec(_PROBE, {"__name__": "__main__"})
    elif mode == "setup":
        from unittest.mock import patch
        from netizen_cli import cli
        selected = Path(root).resolve()
        require(selected in allowed, "Setup refuses non-acceptance root")

        def helper(module, args, **kwargs):
            if module == "netizen_cli.feishu_app_onboarding":
                return {"version": 1, "appId": "ci_business_fixture",
                        "appSecret": "synthetic-not-a-real-secret"}
            if module == "netizen_cli.feishu_app_permissions":
                return {"version": 1, "missingScopes": []}
            raise RuntimeError("Unexpected auth helper: " + module)

        with patch("netizen_cli.cli_setup.require_codex_login"), patch(
                "netizen_cli.cli_setup._helper", side_effect=helper):
            raise SystemExit(cli.main(["setup", "--root", str(selected), "--json"]))


def hook_source(roots):
    return '''"""CI business fixture; never start SDK/Channel/Admin HTTP."""
import asyncio, json, os, signal, sys, time
from pathlib import Path
ALLOWED = ''' + repr([str(p) for p in roots]) + '''
LOCK_FD = int(os.environ.get("NETIZEN_LIFETIME_LOCK_FD", "-1"))
ORIGINAL_RUN = asyncio.run
async def fixture(settings, *, instance_root, ready_file):
    import __main__ as runtime
    root = Path(instance_root).resolve()
    if str(root) not in ALLOWED or LOCK_FD < 0 or os.get_inheritable(LOCK_FD):
        raise RuntimeError("Fixture root/lifetime descriptor boundary rejected")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    row = dict(fixture=True, pid=os.getpid(), python=sys.executable, prefix=sys.prefix,
               root=str(root), time=time.time(), lock_inheritable=False)
    with (root / "fixture-runs.jsonl").open("a") as out:
        out.write(json.dumps(row) + "\\n")
    runtime._publish_ready_marker(ready_file)
    try:
        await stop.wait()
    finally:
        runtime._clear_ready_marker(ready_file)
def run(coro, *args, **kwargs):
    code = getattr(coro, "cr_code", None)
    expected = Path(__file__).parent / "netizen_cli/main.py"
    if code and code.co_name == "run" and Path(code.co_filename).resolve() == expected.resolve():
        values = dict(coro.cr_frame.f_locals)
        coro.close()
        coro = fixture(values["settings"], instance_root=values["instance_root"],
                       ready_file=values["ready_file"])
    return ORIGINAL_RUN(coro, *args, **kwargs)
asyncio.run = run
'''


class Acceptance:
    def __init__(self, args):
        temporary = runner_guard()
        self.base = Path(args.directory).absolute()
        require(self.base.resolve() == self.base and self.base.is_relative_to(temporary)
                and self.base != temporary and not self.base.exists(), "Directory must be fresh/canonical under RUNNER_TEMP")
        for value in (args.python_a, args.console_a, args.uv, args.wheelhouse):
            lexical = Path(value).absolute()
            require(lexical.is_relative_to(temporary) and lexical.exists(), "Input must exist under RUNNER_TEMP: " + value)
        self.base.mkdir(mode=0o700)
        self.evidence = self.base / "evidence"
        self.evidence.mkdir(mode=0o700)
        self.script = str(Path(__file__).resolve())
        self.py_a, self.cli_a = Path(args.python_a).absolute(), Path(args.console_a).absolute()
        self.uv, self.wheels = Path(args.uv).resolve(), Path(args.wheelhouse).resolve()
        require(self.wheels.is_relative_to(temporary) and self.uv.is_relative_to(temporary), "Resolved input outside RUNNER_TEMP")
        self.roots = [self.base / "roots" / name for name in ("a-active", "a-stopped", "b-active", "b-stopped")]
        self.owned = []
        self.pending = None
        self.count = 0
        self.summary = {"candidate_sha": CANDIDATE, "fixture": "business_run_and_setup_auth_only",
                        "full_runtime": "NOT_VERIFIED", "actual_version_upgrade": "NOT_VERIFIED",
                        "pip_native_noop": "NOT_RUN", "uv_tool_native_same_version_update": "NOT_RUN",
                        "cleanup": "NOT_RUN", "steps": []}
        # Explicit offline source settings apply equally to prepare and update.
        # Do not hide unknown settings: refuse inherited package controls first.
        allowed = {"PIP_CONFIG_FILE", "PIP_DISABLE_PIP_VERSION_CHECK"}
        unknown = sorted(k for k, v in os.environ.items() if v and k.startswith(("PIP_", "UV_")) and k not in allowed)
        require(not unknown, "Unset inherited package controls before this isolated run: " + ", ".join(unknown))
        self.env = dict(os.environ, PIP_CONFIG_FILE="/dev/null", PIP_NO_INDEX="1",
                        PIP_FIND_LINKS=str(self.wheels), PIP_DISABLE_PIP_VERSION_CHECK="1",
                        UV_NO_INDEX="1", UV_FIND_LINKS=str(self.wheels),
                        UV_TOOL_DIR=str(self.base / "uv-tools"), UV_TOOL_BIN_DIR=str(self.base / "uv-bin"),
                        UV_CACHE_DIR=str(self.base / "uv-cache"), PATH=str(self.uv.parent) + os.pathsep + os.environ["PATH"])
        for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "NETIZEN_ROOT"):
            require(not self.env.get(name), "Unset inherited " + name)
        wheels = list(self.wheels.glob("netizen_cli-*.whl"))
        require(len(wheels) == 1, "Wheelhouse must contain exactly one netizen-cli wheel")
        self.wheel = wheels[0]
        require(digest(self.wheel.read_bytes()) == args.wheel_sha256, "Candidate wheel hash mismatch")
        with zipfile.ZipFile(self.wheel) as archive:
            self.expected = {n: digest(archive.read(n)) for n in archive.namelist()
                             if n.startswith("netizen_cli/") and not n.endswith("/")}
        require(self.expected.get("netizen_cli/cli_update_worker.py") == WORKER_SHA256,
                "Wheel does not contain the exact cd00eca worker fix")
        self.summary["wheel_sha256"] = args.wheel_sha256

    def run(self, name, command, *, timeout=60, parse=True, discard_output=False):
        self.count += 1
        prefix = self.evidence / f"{self.count:02d}-{name}"
        started = time.monotonic()
        row = {"name": name, "command": [str(x) for x in command]}
        with prefix.with_suffix(".stdout.log").open("w") as out, prefix.with_suffix(".stderr.log").open("w") as err:
            process = subprocess.Popen(row["command"], cwd=self.base, env=self.env,
                                       stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL if discard_output else out,
                                       stderr=subprocess.DEVNULL if discard_output else err)
            self.pending = process
            try:
                process.wait(timeout=timeout)
            except BaseException:
                # Signal only this exact child. Worker owns cleanup of its children.
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=35)
                    except subprocess.TimeoutExpired:
                        row["cleanup_blocked_by_live_child"] = process.pid
                raise
            finally:
                row.update(returncode=process.poll(), elapsed_seconds=round(time.monotonic() - started, 3))
                write_json(prefix.with_suffix(".json"), row)
                self.summary["steps"].append(row)
                if process.poll() is not None:
                    self.pending = None
        text = prefix.with_suffix(".stdout.log").read_text()
        value = None
        if parse and text.strip():
            try:
                value = json.loads(text.strip().splitlines()[-1])
                write_json(prefix.with_suffix(".result.json"), value)
            except json.JSONDecodeError:
                require(process.returncode != 0, name + " returned no valid JSON result")
        require(not parse or process.returncode != 0 or value is not None,
                name + " returned an empty JSON result")
        require(process.returncode == 0, name + " failed; see its saved stdout/stderr")
        return value

    def helper(self, name, python, mode, root=None):
        command = [python, "-I", self.script, "_" + mode, str(self.base)]
        if root is not None:
            command.append(root)
        return self.run(name, command, timeout=90)

    def inventory(self, name, expected):
        rows = self.helper(name, self.py_a, "inventory")
        actual = {Path(row["binding"]["root"]).resolve() for row in rows}
        require(actual == set(expected), "Unexpected full native inventory: " + str(sorted(map(str, actual))))
        return rows

    def installation(self, name, python, console, installer):
        info = self.helper(name, python, "identity")
        prefix, package = Path(info["prefix"]), Path(info["package_dir"])
        require(prefix.is_relative_to(Path(os.environ["RUNNER_TEMP"]).resolve()), "Installation outside runner temp")
        require(info["version"] == VERSION and info["installer"] == installer, "Unexpected version/installer")
        require(Path(info["environment_python"]).parent == Path(python).parent,
                "Python lexical launcher differs")
        require(console.resolve() == (Path(info["scripts_dir"]) / "netizen").resolve(), "Console belongs to another installation")
        for path, expected in self.expected.items():
            require(digest((package.parent / path).read_bytes()) == expected, "Installed package differs from wheel: " + path)
        return info

    def install_hook(self, info, roots):
        site = Path(info["package_dir"]).parent
        content = hook_source(roots)
        for path, value in ((site / (HOOK + ".py"), content),
                            (site / "netizen_ci_acceptance.pth",
                             "import os; exec(" + repr("try:\n import " + HOOK + "\nexcept BaseException:\n os._exit(78)") + ")\n")):
            require(not path.exists(), "Refusing existing fixture hook")
            with path.open("x") as output:
                output.write(value)
        (self.evidence / (roots[0].name + "-hook.py")).write_text(content)

    def prepare(self, python, console, roots):
        for root in roots:
            require(not root.exists(), "Test root already exists")
            # Record intent before setup so partial registration is also cleaned.
            self.owned.append(root)
            self.helper("setup-" + root.name, python, "setup", root)
        self.run("start-" + roots[0].name, [console, "start", "--root", roots[0], "--json"], timeout=180)

    def snapshot(self, name, expected):
        rows = self.inventory(name, expected)
        result = {}
        for row in rows:
            root = Path(row["binding"]["root"])
            identity = root / "state/service.identity.json"
            fixture = root / "fixture-runs.jsonl"
            entries = [json.loads(s) for s in fixture.read_text().splitlines()] if fixture.exists() else []
            row["identity"] = json.loads(identity.read_text()) if identity.exists() else None
            row["fixture_runs"] = entries
            require(all(x.get("fixture") is True and x.get("lock_inheritable") is False for x in entries), "Invalid fixture evidence")
            db = root / "state/channel.sqlite3"
            require(db.is_file(), "Database absent")
            with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as connection:
                require(connection.execute("PRAGMA integrity_check").fetchone() == ("ok",), "Database integrity failed")
                row["database_logical_sha256"] = digest("\n".join(connection.iterdump()).encode())
            active = root.name.endswith("-active")
            require(row["running"] == active and row["ready"] == active and row["loaded"] == active,
                    "Unexpected active/stopped state for " + root.name)
            require(bool(entries) == active, "Unexpected business start count")
            if active:
                require(row["identity"]["pid"] == entries[-1]["pid"], "Identity/fixture PID mismatch")
            result[str(root)] = row
        write_json(self.evidence / (name + "-snapshot.json"), result)
        return result

    def execute(self):
        # Never capture full launchctl output: it can include inherited environment.
        self.run("gui-domain", ["launchctl", "print", f"gui/{os.geteuid()}"], parse=False, discard_output=True)
        write_json(self.evidence / "runner.json", {"uid": os.geteuid(), "home": str(Path.home()),
                   "image_os": os.environ.get("ImageOS"), "image_version": os.environ.get("ImageVersion"),
                   "run_id": os.environ.get("GITHUB_RUN_ID"), "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
                   "gui_domain_visible": True})
        self.inventory("initial-native-inventory", [])
        uv = self.run("uv-version", [self.uv, "--no-config", "self", "version", "--output-format", "json"])
        require(uv["version"] == "0.12.20", "Only uv 0.12.20 is within the receipt contract")
        a_info = self.installation("a-installation-before", self.py_a, self.cli_a, "pip")
        self.summary["pip_native_noop"] = "RUNNING"
        self.install_hook(a_info, self.roots[:2])
        self.prepare(self.py_a, self.cli_a, self.roots[:2])
        before = self.snapshot("a-before-update", self.roots[:2])
        report = self.run("a-native-update", [self.cli_a, "update", "--json"], timeout=360)
        require(report["status"] == "succeeded" and report["inventory_complete"] is True and report["backend"] == "pip", "Native pip update failed")
        require({x["root"] for x in report["instances"]} == set(before), "Pip affected inventory differs")
        require(report["package"]["replacement_started"] is False and report["package"]["before_version"] == report["package"]["after_version"] == VERSION,
                "Pip scenario was not a no-op")
        require(report["progress"] == {"stop_total": 0, "stopped": 0, "start_total": 0, "ready": 0}, "Pip no-op stopped/restored services")
        after = self.snapshot("a-after-update", self.roots[:2])
        require(before == after, "A changed during pip no-op")
        self.summary["pip_native_noop"] = "PASS"
        self.summary["uv_tool_native_same_version_update"] = "RUNNING"
        self.run("b-uv-tool-install", [self.uv, "--no-config", "--no-python-downloads", "tool", "install",
                                      "--python", self.py_a, "netizen-cli==" + VERSION], timeout=300, parse=False)
        py_b = self.base / "uv-tools/netizen-cli/bin/python"
        cli_b = self.base / "uv-bin/netizen"
        b_info = self.installation("b-installation-before", py_b, cli_b, "uv")
        require(b_info["prefix"] != a_info["prefix"], "UV target did not isolate A")
        self.install_hook(b_info, self.roots[2:])
        self.prepare(py_b, cli_b, self.roots[2:])
        before = self.snapshot("all-before-b-update", self.roots)
        report = self.run("b-native-update", [cli_b, "update", "--json"], timeout=360)
        require(report["status"] == "succeeded" and report["inventory_complete"] is True and report["backend"] == "uv-tool", "Native uv-tool update failed")
        require({x["root"] for x in report["instances"]} == set(map(str, self.roots[2:])), "UV affected inventory differs")
        require(report["package"]["replacement_started"] is True and report["package"]["state"] == "verified"
                and report["package"]["before_version"] == report["package"]["after_version"] == VERSION,
                "UV scenario is not verified same-version update")
        require(report["progress"] == {"stop_total": 1, "stopped": 1, "start_total": 1, "ready": 1}, "UV running set was not restored exactly")
        after = self.snapshot("all-after-b-update", self.roots)
        for root in self.roots:
            old, new = before[str(root)], after[str(root)]
            require(old["database_logical_sha256"] == new["database_logical_sha256"], "Database logical content changed")
            if root == self.roots[2]:
                require(old["identity"]["pid"] != new["identity"]["pid"] and len(new["fixture_runs"]) == len(old["fixture_runs"]) + 1,
                        "UV active fixture did not restart exactly once")
            else:
                require(old == new, "Unselected/stopped instance changed: " + root.name)
        self.installation("a-installation-after", self.py_a, self.cli_a, "pip")
        self.installation("b-installation-after", py_b, cli_b, "uv")
        self.summary["uv_tool_native_same_version_update"] = "PASS"

    def cleanup(self):
        errors = []
        if self.pending is not None and self.pending.poll() is None:
            self.summary["cleanup"] = "BLOCKED_BY_LIVE_OWNED_CHILD"
            self.summary["live_child_pid"] = self.pending.pid
            return
        for root in reversed(self.owned):
            for command in ("stop", "remove"):
                try:
                    # A is unchanged; its native CLI manages the exact binding
                    # even if B's package tool left B's console unavailable.
                    argv = [self.cli_a, command, "--root", root, "--json"]
                    if command == "remove":
                        argv.append("-y")
                    self.run("cleanup-" + root.name + "-" + command, argv, timeout=150)
                except Exception as error:
                    errors.append(str(error))
                    if self.pending is not None:
                        break
            if self.pending is not None:
                break
        for root in self.owned:
            # Only fixture/runtime logs; never config, credentials or environment.
            for relative in ("fixture-runs.jsonl", "state/netizen.log", "state/launchd.stderr.log"):
                path = root / relative
                if path.is_file() and not path.is_symlink():
                    (self.evidence / (root.name + "-" + path.name)).write_bytes(path.read_bytes())
        try:
            self.inventory("final-native-inventory", [])
        except Exception as error:
            errors.append(str(error))
        self.summary["cleanup"] = "PASS" if not errors else "FAIL"
        self.summary["cleanup_errors"] = errors


def main():
    if len(sys.argv) > 1 and sys.argv[1].startswith("_"):
        mode, directory, *roots = sys.argv[1:]
        # Public package identity probe consumes argv[1:]; supply empty expected.
        if mode == "_identity":
            sys.argv = [sys.argv[0], "{}", "import"]
        internal(mode[1:], directory, roots[0] if roots else None)
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("directory", "python-a", "console-a", "uv", "wheelhouse", "wheel-sha256"):
        parser.add_argument("--" + flag, required=True)
    args = parser.parse_args()
    try:
        acceptance = Acceptance(args)
    except Exception as error:
        failure = {"status": "REJECTED_BEFORE_SERVICE_OPERATIONS", "reason": str(error)}
        print(json.dumps(failure))
        return 1

    def terminate(signum, frame):
        raise KeyboardInterrupt("CI termination")

    signal.signal(signal.SIGTERM, terminate)
    failed = False
    try:
        acceptance.execute()
    except BaseException as error:
        failed = True
        acceptance.summary["failure"] = f"{type(error).__name__}: {error}"
        for key in ("pip_native_noop", "uv_tool_native_same_version_update"):
            if acceptance.summary[key] == "RUNNING":
                acceptance.summary[key] = "FAIL"
    finally:
        acceptance.cleanup()
        acceptance.summary["status"] = "FAIL" if failed or acceptance.summary["cleanup"] != "PASS" else "PASS"
        write_json(acceptance.evidence / "summary.json", acceptance.summary)
    print(json.dumps(acceptance.summary, indent=2))
    return 1 if acceptance.summary["status"] != "PASS" else 0


if __name__ == "__main__":
    raise SystemExit(main())
