"""Copied outside the installation before use; intentionally standard-library only.

This file is an update protocol participant, not a second package installer. It
waits for the selected package manager, validates with a new interpreter, and
restores only the recorded running services using the newly installed CLI.
"""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
import shlex
import signal
import subprocess
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


PROTOCOL_VERSION = 1
_interrupted_signal: int | None = None
_CHILD_STOP_GRACE = 5.0


class UpdateInterrupted(KeyboardInterrupt):
    pass


def _request_interruption(signum: int, _frame: Any) -> None:
    # Raising from a handler could abandon Popen during construction or release
    # locks before the package tool exits. Repeated signals remain deferred too.
    global _interrupted_signal
    if _interrupted_signal is None:
        _interrupted_signal = signum


def _check_interrupted() -> None:
    if _interrupted_signal is not None:
        raise UpdateInterrupted(signal.Signals(_interrupted_signal).name)


def _stop_owned_child(child: subprocess.Popen) -> None:
    """Stop only the group whose unreaped leader still belongs to this worker."""
    if child.returncode is not None:
        return
    for signum, grace in ((signal.SIGTERM, _CHILD_STOP_GRACE), (signal.SIGKILL, 0)):
        try:
            os.killpg(child.pid, signum)
        except ProcessLookupError:
            break
        except PermissionError:
            # macOS excludes zombies from group signal recipients and can return
            # EPERM when only our exited leader remains. Reap it, then accept
            # only a vanished group; never send another signal to its old ID.
            if child.poll() is None:
                raise
            try:
                os.killpg(child.pid, 0)
            except ProcessLookupError:
                break
            raise
        if grace:
            # Do not poll/reap during grace: the leader's exit does not prove
            # its descendants stopped. Its reserved PID anchors the final signal.
            time.sleep(grace)
    # Reap our direct child after the last group signal, while holding the locks.
    # Orphan zombies are not waitable here and cannot execute or retain locks.
    child.communicate()


def _run_owned(command: list[str], *, timeout: float | None = None,
               capture_output: bool = False, check: bool = False, **kwargs: Any) -> Any:
    _check_interrupted()
    if capture_output:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = None if timeout is None else time.monotonic() + timeout
    with subprocess.Popen(command, start_new_session=True, **kwargs) as child:
        try:
            while True:
                _check_interrupted()
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    stdout, stderr = child.communicate(timeout=min(0.2, remaining)
                                                       if remaining is not None else 0.2)
                    break
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            _stop_owned_child(child)
            raise
        # Successful communicate ends ownership of this command. A late signal
        # still aborts the update, but must never target its already-reaped PID.
        _check_interrupted()
        if check and child.returncode:
            raise subprocess.CalledProcessError(child.returncode, command, stdout, stderr)
        return subprocess.CompletedProcess(command, child.returncode, stdout, stderr)


def persist_report(path: Path, report: dict[str, Any]) -> None:
    """An interrupted update leaves evidence, never an instruction to roll back."""
    descriptor, temporary = tempfile.mkstemp(prefix=".report-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def progress(message: str) -> None:
    """Keep line-oriented progress timely without making it an update decision."""
    try:
        print(message, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def print_report(report: dict[str, Any], *, json_output: bool = False) -> None:
    if json_output:
        print(json.dumps(report, ensure_ascii=False))
        return
    package = report["package"]
    counts = report["progress"]
    succeeded = report["status"] == "succeeded"
    noop = succeeded and not package["replacement_started"]
    if noop:
        print("No package changes required under the current package-manager configuration.")
    elif succeeded:
        print("Update complete.")
    elif package["state"] == "verified":
        print("Update incomplete: the installed package was verified.")
    else:
        stage = {"preflight": "preflight checks", "stopping": "instance shutdown",
                 "updating": "package update", "validating": "installation verification",
                 "restoring": "instance recovery"}.get(report["phase"], report["phase"])
        print(f"Update failed during {stage}.")
    if report.get("environment"):
        print(f"Environment: {report['environment']}")
    if report.get("backend"):
        print(f"Package manager: {report['backend']}")
    if noop:
        print(f"Package: netizen-cli {package['before_version']} (unchanged).")
        print("No instances were stopped or restarted.")
    elif package["state"] == "verified":
        before, after = package["before_version"], package["after_version"]
        version = f"{after} (version unchanged)" if before == after else f"{before} -> {after}"
        print(f"Package: netizen-cli {version}; installation verified.")
        if counts["start_total"]:
            print(f"Instances: {counts['ready']}/{counts['start_total']} restored and ready.")
        else:
            print("No previously running instances needed restoring.")
    else:
        if package["replacement_started"]:
            print("Package state is unknown; no instances were restarted.")
        else:
            print("Package replacement did not start.")
            print("Instance recovery was not attempted.")
    if not report["inventory_complete"]:
        print("Instance inventory could not be confirmed.")
    for item in report["instances"]:
        state = item["state"]
        if item["action"] == "start" and state in {"unknown", "starting"}:
            state = "readiness unconfirmed (may be running)"
        elif state == "loaded":
            state = "loaded, no running process"
        elif state == "unknown":
            state = "state unconfirmed"
        elif state == "stopped" and not noop:
            state = "stopped (not restarted)" if item["was_running"] else "stopped (kept stopped)"
        print(f"  {item['root']}: {state}")
        if item.get("reason"):
            print(f"    {item['reason']}")
        if item["action"] == "start" and item["state"] != "ready":
            root = shlex.quote(item["root"])
            print(f"    Check: netizen status --root {root}")
            print(f"    Logs: netizen logs --root {root}")
    if not succeeded and report.get("reason"):
        print(f"Reason: {report['reason']}")
    if report.get("recommendation"):
        print(f"Next: {report['recommendation']}")
    if report.get("report_path"):
        print(f"Report: {report['report_path']}")


def _error(report: dict[str, Any], reason: str, recommendation: str) -> dict[str, Any]:
    report.update(status="failed", reason=reason, recommendation=recommendation)
    return report


def _validation_error(result: Any, message: str) -> ValueError:
    # Only our own inline Python probe emits this error object; package-manager
    # diagnostics are never interpreted as a decision protocol.
    try:
        response = json.loads(result.stdout)
    except (TypeError, ValueError):
        response = None
    if isinstance(response, dict) and isinstance(response.get("error"), str):
        message += ": " + response["error"][:4096]
    return ValueError(message)


def _validate_root_locks(plan: dict[str, Any]) -> None:
    """Keep parent-acquired root locks through replacement without importing it."""
    locks = plan.get("root_locks", {})
    roots = {item["root"] for item in plan["report"]["instances"]}
    if set(locks) != roots:
        raise ValueError("The updater did not retain every instance maintenance lock")
    for root, descriptor in locks.items():
        if type(descriptor) is not int or descriptor < 3:
            raise ValueError("Invalid inherited instance maintenance lock")
        path = Path(root) / "state/.install.lock"
        held, expected = os.fstat(descriptor), path.lstat()
        for metadata in (held, expected):
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1):
                raise ValueError(f"Unsafe inherited maintenance lock: {root}")
        if (held.st_dev, held.st_ino) != (expected.st_dev, expected.st_ino):
            raise ValueError(f"Inherited maintenance lock does not match: {root}")
        probe = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise ValueError(f"Instance maintenance lock is not held: {root}")
        finally:
            os.close(probe)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Only an explicit pass_fds to the matching fresh restore child may pass
        # this further; package tools and service processes cannot inherit it.
        os.set_inheritable(descriptor, False)


def execute(plan: dict[str, Any], *, runner: Any = _run_owned) -> dict[str, Any]:
    """Run the irreversible half. All instances were stopped by the caller."""
    report = plan["report"]
    report_path = Path(report["report_path"])
    package = plan["package_plan"]
    environment = dict(os.environ)
    environment.update(package.get("command_env", {}))
    report["phase"] = "updating"
    persist_report(report_path, report)
    try:
        progress("Rechecking installation before package replacement...")
        _validate_root_locks(plan)
        for name, expected in package.get("revalidation_files", {}).items():
            actual = hashlib.sha256(Path(name).read_bytes()).hexdigest()
            if actual != expected:
                raise ValueError(f"Installation identity changed during preflight: {name}")
        for name, expected in package.get("revalidation_paths", {}).items():
            metadata = Path(name).stat()
            if [metadata.st_dev, metadata.st_ino] != list(expected):
                raise ValueError(f"Installation target changed during preflight: {name}")
        if package.get("revalidation_command"):
            checked = runner(
                package["revalidation_command"], cwd=package["command_cwd"], env=environment,
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                close_fds=True, check=False, timeout=60,
            )
            if checked.returncode != 0:
                raise _validation_error(checked, "Fresh-process installation identity revalidation failed")
        report["package"].update(state="mutation_started", replacement_started=True)
        report["unexecuted"] = ["validation", "restore"]
        persist_report(report_path, report)
        # Package output is diagnostic only, never parsed to decide success or no-op.
        progress(f"Updating packages with {package.get('backend') or report.get('backend') or 'package manager'}...")
        result = runner(
            package["command"], cwd=package["command_cwd"],
            env=environment, stdin=subprocess.DEVNULL,
            stdout=sys.stderr, stderr=sys.stderr, close_fds=True, check=False,
        )
        if result.returncode != 0:
            report["package"]["state"] = "unknown"
            return _error(
                report, f"Package manager failed (exit {result.returncode}); no instance was restarted.",
                "Repair this Python installation with its package manager, then explicitly start "
                "the stopped instances. No automatic rollback or backend fallback was attempted.",
            )
        report["phase"] = "validating"
        report["package"]["state"] = "unverified"
        persist_report(report_path, report)
        progress("Verifying the installed package...")
        validation = runner(
            package["validation_command"], cwd=package["command_cwd"],
            env=environment, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, close_fds=True, check=False, timeout=60,
        )
        if validation.returncode != 0:
            raise _validation_error(validation, "Fresh-process installation validation failed")
        installed = json.loads(validation.stdout)
        if not isinstance(installed, dict) or not isinstance(installed.get("version"), str):
            raise ValueError("Fresh-process installation validation returned an invalid result")
        report["package"].update(state="verified", after_version=installed["version"])
        report["phase"] = "restoring"
        report["unexecuted"] = [f"start:{item['root']}" for item in report["instances"] if item["was_running"]]
        persist_report(report_path, report)
        progress(f"Verified netizen-cli {installed['version']}.")
        for item in report["instances"]:
            if not item["was_running"]:
                continue
            item.update(state="starting", action="start")
            report["unexecuted"].remove(f"start:{item['root']}")
            persist_report(report_path, report)
            progress(f"Restoring {item['root']}; waiting for readiness...")
            try:
                restored = runner(
                    [package["environment_python"], "-I", "-m", "netizen_cli.cli_update_restore",
                     "--lock-fd", str(plan["root_locks"][item["root"]]),
                     "--expected-prefix", item["prefix"], "--expected-python", item["python"],
                     "--root", item["root"]],
                    cwd=package["command_cwd"], env=environment,
                    stdin=subprocess.DEVNULL, stdout=sys.stderr, stderr=sys.stderr,
                    close_fds=True, pass_fds=(plan["root_locks"][item["root"]],),
                    check=False, timeout=180,
                )
                if restored.returncode == 0:
                    item["state"] = "ready"
                    report["progress"]["ready"] += 1
                else:
                    item.update(state="unknown", reason=f"Start failed (exit {restored.returncode}); "
                                "the service may have started but not reached readiness.")
            except (OSError, subprocess.TimeoutExpired) as error:
                item.update(state="unknown", reason=f"Start result could not be confirmed: {type(error).__name__}")
            persist_report(report_path, report)
            if item["state"] == "ready":
                progress(f"Ready: {item['root']}.")
            else:
                progress(f"Readiness unconfirmed: {item['root']} (may be running).")
        if report["progress"]["ready"] != report["progress"]["start_total"]:
            return _error(
                report, "Program update was verified, but some instances did not confirm readiness.",
                "Inspect each failed instance with status/logs and repair its startup failure. "
                "Successful instances remain running; committed migrations are not rolled back.",
            )
        _check_interrupted()
        report.update(phase="complete", status="succeeded", reason=None, recommendation=None)
        report["unexecuted"] = []
        return report
    except (OSError, ValueError, subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        for item in report["instances"]:
            if item["state"] == "starting":
                item.update(state="unknown", reason="Start observation was interrupted; readiness is unknown.")
        if report["package"]["replacement_started"] and report["package"]["state"] != "verified":
            report["package"]["state"] = "unknown"
        return _error(
            report, f"Update interrupted or could not be verified: {type(error).__name__}: {error}",
            "Inspect the recorded package and instance states. If package replacement began, repair "
            "and verify the installation before explicitly restarting stopped instances. "
            "No rollback or automatic compensation was performed.",
        )
    finally:
        persist_report(report_path, report)


def main() -> int:
    if len(sys.argv) != 2:
        print("This is a private, one-shot Netizen update helper.", file=sys.stderr)
        return 2
    plan_path = Path(sys.argv[1])
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("protocol") != PROTOCOL_VERSION:
        print("Unsupported Netizen update helper protocol.", file=sys.stderr)
        return 2
    # Do not retain the inherited environment (which may include credentials) on disk.
    plan_path.unlink()
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, _request_interruption)
    report = execute(plan)
    print_report(report, json_output=plan.get("json_output", False))
    return 0 if report["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
