#!/usr/bin/env python3
"""Live gate for update-job isolation, using disposable current-user jobs only.

Run with the checkout Python on Linux with a systemd user manager, or in a macOS
GUI login session. This does not run the installer, capture a login profile, or
access Netizen credentials, databases, or Codex state. The parent fixture job
launches the actual UpdateExecutor; its helper then stops that same parent job.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT))

from netizen.deployment.update_executor import UpdateExecutor  # noqa: E402


_ID = re.compile(r"[0-9a-f]{32}\Z")
_RELEASE_ID = re.compile(r"[0-9a-f]{64}\Z")
_FIXTURE_PREFIX = "netizen-update-executor-probe-"
_FIXTURE_ACCOUNT = "account ${NETIZEN_PROBE_EXPANSION} %h space ' ;"


class ProbeError(RuntimeError):
    pass


class _ParentExecutor(UpdateExecutor):
    """Use the real dispatcher with an unmistakably disposable parent label."""

    def _label(self, operation_id: str) -> str:
        self._validate_operation_id(operation_id)
        return f"netizen-update-probe-parent-{operation_id}"


def _write(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ProbeError("invalid probe record")
    return value


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProbeError("probe deadline expired")
    return remaining


def _wait(predicate: Any, deadline: float, label: str) -> None:
    while not predicate():
        try:
            remaining = _remaining(deadline)
        except ProbeError as error:
            raise ProbeError(f"timed out waiting for {label}") from error
        time.sleep(min(0.1, remaining))


def _lock_available(path: Path) -> bool:
    with path.open("a+b") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        return True


def _cgroup() -> str | None:
    return Path("/proc/self/cgroup").read_text(encoding="utf-8") if sys.platform == "linux" else None


def _fixture(home: Path) -> dict[str, Any]:
    if (
        home.name != _FIXTURE_ACCOUNT
        or not home.parent.name.startswith(_FIXTURE_PREFIX)
        or home != home.resolve()
    ):
        raise ProbeError("refusing a path outside the disposable probe fixture")
    config = _read(home / ".netizen" / "state" / "probe.json")
    for name in ("parent_id", "helper_id"):
        if not isinstance(config.get(name), str) or _ID.fullmatch(config[name]) is None:
            raise ProbeError("invalid disposable job identity")
    if config["parent_id"] == config["helper_id"]:
        raise ProbeError("parent and helper identities must differ")
    if not isinstance(config.get("release_id"), str) or _RELEASE_ID.fullmatch(config["release_id"]) is None:
        raise ProbeError("invalid disposable release identity")
    if not isinstance(config.get("deadline"), (float, int)):
        raise ProbeError("invalid probe deadline")
    return config


def _job(home: Path, config: dict[str, Any], role: str) -> tuple[UpdateExecutor, str]:
    # There is deliberately no general target-name, command, or script option.
    if role == "parent":
        return _ParentExecutor(home), config["parent_id"]
    if role == "helper":
        return UpdateExecutor(home), config["helper_id"]
    raise ProbeError("unknown probe job role")


def _stop_job(home: Path, config: dict[str, Any], role: str, deadline: float) -> None:
    executor, operation_id = _job(home, config, role)
    if not executor.is_active(operation_id):
        return
    label = executor._label(operation_id)
    arguments = (
        ["systemctl", "--user", "stop", "--no-block", "--", f"{label}.service"]
        if sys.platform == "linux"
        else ["launchctl", "bootout", f"gui/{os.geteuid()}/{label}"]
    )
    environment = {"HOME": str(home), "PATH": "/usr/local/bin:/usr/bin:/bin"}
    if sys.platform == "linux":
        environment["XDG_RUNTIME_DIR"] = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.geteuid()}")
        environment["DBUS_SESSION_BUS_ADDRESS"] = os.environ.get(
            "DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.geteuid()}/bus"
        )
    result = subprocess.run(
        arguments, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        close_fds=True, timeout=min(10, _remaining(deadline)), check=False,
    )
    if result.returncode != 0 and executor.is_active(operation_id):
        raise ProbeError(f"could not stop disposable {role} job: {result.stderr[-1000:]}")
    _wait(lambda: not executor.is_active(operation_id), deadline, f"disposable {role} job to stop")


def _parent(home: Path, config: dict[str, Any]) -> None:
    state = home / ".netizen" / "state"
    with (state / "parent.lifetime.lock").open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Deliberately exercise the strongest FD leak case. A helper inheriting
        # this FD would prevent the post-stop lock proof from succeeding.
        os.set_inheritable(lock.fileno(), True)
        os.environ["NETIZEN_LIFETIME_LOCK_FD"] = str(lock.fileno())
        os.environ["NETIZEN_LIFETIME_LOCK_FILE"] = str(state / "parent.lifetime.lock")
        _write(state / "parent-started.json", {"cgroup": _cgroup()})
        UpdateExecutor(home).launch(config["helper_id"], home / ".netizen" / "current")
        _write(state / "parent-submitted.json", {"helper_id": config["helper_id"]})
        while time.monotonic() < config["deadline"]:
            time.sleep(0.1)
        raise ProbeError("the helper did not stop its launching parent job")


def _helper(home: Path, config: dict[str, Any]) -> None:
    state = home / ".netizen" / "state"
    deadline = config["deadline"]
    with (state / "helper.execution.lock").open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _write(state / "helper-started.json", {"cgroup": _cgroup()})
        _wait(lambda: (state / "parent-submitted.json").exists(), deadline, "parent dispatch confirmation")
        if _lock_available(state / "parent.lifetime.lock"):
            raise ProbeError("the launching parent had already exited before the stop test")
        if "NETIZEN_LIFETIME_LOCK_FD" in os.environ:
            raise ProbeError("the helper inherited the parent lifetime-lock environment")
        _stop_job(home, config, "parent", deadline)
        if not _lock_available(state / "parent.lifetime.lock"):
            raise ProbeError("parent lifetime lock remained held after its manager job stopped")
        # This write happens only after the exact launching parent was stopped.
        _write(
            state / "completed.json",
            {
                "operation_id": config["helper_id"],
                "python": sys.executable,
                "script": str(Path(__file__).resolve()),
                "cwd": str(Path.cwd()),
                "home": os.environ.get("HOME"),
                "parent_stopped": True,
                "parent_lock_released": True,
            },
        )


def _actor(operation_id: str) -> int:
    home = Path(__file__).resolve().parents[5]
    config = _fixture(home)
    if Path(__file__).resolve() != home / ".netizen" / "releases" / config["release_id"] / "source" / "scripts" / "netizen_updater.py":
        raise ProbeError("the probe actor did not start from its physical fixture release")
    role = "parent" if operation_id == config["parent_id"] else "helper"
    if operation_id != config[f"{role}_id"]:
        raise ProbeError("operation does not belong to this probe fixture")
    try:
        (_parent if role == "parent" else _helper)(home, config)
        return 0
    except Exception as error:
        _write(
            home / ".netizen" / "state" / f"{role}-error.json",
            {"type": type(error).__name__, "error": str(error)[:2000]},
        )
        return 1


def _prepare(root: Path, deadline: float) -> tuple[Path, dict[str, Any]]:
    home = root / _FIXTURE_ACCOUNT
    config = {"parent_id": uuid.uuid4().hex, "helper_id": uuid.uuid4().hex,
              "release_id": uuid.uuid4().hex + uuid.uuid4().hex, "deadline": deadline}
    release = home / ".netizen" / "releases" / config["release_id"]
    (release / "venv" / "bin").mkdir(parents=True, mode=0o700)
    (release / "venv" / "bin" / "python").symlink_to(Path(sys.executable).resolve())
    scripts = release / "source" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copyfile(__file__, scripts / "netizen_updater.py")
    package = release / "source" / "netizen"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    deployment = package / "deployment"
    deployment.mkdir()
    (deployment / "__init__.py").write_text("", encoding="utf-8")
    shutil.copyfile(SOURCE_ROOT / "netizen" / "deployment" / "update_executor.py", deployment / "update_executor.py")
    state = home / ".netizen" / "state"
    state.mkdir(mode=0o700)
    _write(state / "probe.json", config)
    (home / ".netizen" / "current").symlink_to(release)
    return home, config


def _evidence(home: Path, config: dict[str, Any]) -> dict[str, bool]:
    state = home / ".netizen" / "state"
    completed = _read(state / "completed.json")
    release = home / ".netizen" / "releases" / config["release_id"]
    checks = {
        "launched_from_managed_parent": (state / "parent-submitted.json").is_file(),
        "helper_completed_after_stopping_parent": completed.get("parent_stopped") is True,
        "parent_lifetime_fd_not_inherited": completed.get("parent_lock_released") is True,
        "physical_release_python": completed.get("python") == str(release / "venv" / "bin" / "python"),
        "physical_release_script": completed.get("script") == str(release / "source" / "scripts" / "netizen_updater.py"),
        "literal_dollar_percent_space_home": completed.get("home") == str(home) and completed.get("cwd") == str(home),
        "exact_operation": completed.get("operation_id") == config["helper_id"],
    }
    if sys.platform == "linux":
        parent = _read(state / "parent-started.json")["cgroup"]
        helper = _read(state / "helper-started.json")["cgroup"]
        checks["distinct_service_cgroups"] = (
            parent != helper
            and f"netizen-update-probe-parent-{config['parent_id']}.service" in parent
            and f"netizen-update-{config['helper_id']}.service" in helper
        )
    return checks


def _run_probe(timeout: float) -> dict[str, Any]:
    root = Path(tempfile.mkdtemp(prefix=_FIXTURE_PREFIX)).resolve()
    home: Path | None = None
    config: dict[str, Any] | None = None
    report: dict[str, Any] = {"platform": sys.platform, "ok": False, "scope": "executor-isolation"}
    try:
        deadline = time.monotonic() + timeout
        home, config = _prepare(root, deadline)
        _ParentExecutor(home).launch(config["parent_id"], home / ".netizen" / "current")
        state = home / ".netizen" / "state"

        def finished() -> bool:
            for role in ("parent", "helper"):
                path = state / f"{role}-error.json"
                if path.exists():
                    error = _read(path)
                    raise ProbeError(f"{role}: {error.get('type')}: {error.get('error')}")
            return (state / "completed.json").exists()

        _wait(finished, deadline, "helper completion after parent stop")
        _wait(lambda: _lock_available(state / "helper.execution.lock"), deadline, "helper result write to finish")
        checks = _evidence(home, config)
        checks["parent_manager_stopped"] = not _ParentExecutor(home).is_active(config["parent_id"])
        report.update({"ok": all(checks.values()), "checks": checks})
        if not report["ok"]:
            report["error"] = "one or more executor isolation proofs failed"
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        if home is not None:
            report["markers"] = sorted(path.name for path in (home / ".netizen" / "state").glob("*.json"))
    finally:
        cleanup_errors: list[str] = []
        if home is not None and config is not None:
            cleanup_deadline = time.monotonic() + 20
            for role in ("helper", "parent"):
                try:
                    _stop_job(home, config, role, cleanup_deadline)
                    executor, operation_id = _job(home, config, role)
                    executor.cleanup(operation_id)
                except Exception as error:
                    cleanup_errors.append(f"{role}: {type(error).__name__}: {error}")
        if cleanup_errors:
            report.update({"ok": False, "cleanup_errors": cleanup_errors, "retained_fixture": str(root)})
        else:
            try:
                shutil.rmtree(root)
                report["cleanup_complete"] = True
            except OSError as error:
                report.update({"ok": False, "cleanup_errors": [f"fixture: {error}"], "retained_fixture": str(root)})
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=60, help="probe deadline in seconds; cleanup has an additional 20-second budget")
    parser.add_argument("--operation-id", help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    if arguments.operation_id is not None:
        return _actor(arguments.operation_id)
    if not 5 <= arguments.timeout <= 300:
        parser.error("--timeout must be between 5 and 300 seconds")
    if sys.platform not in {"linux", "darwin"}:
        parser.error("this probe requires Linux or macOS")
    result = _run_probe(arguments.timeout)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
