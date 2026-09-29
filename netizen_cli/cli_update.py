"""Environment-scoped, stop-first package update orchestration.

System service definitions remain the inventory. The small copied worker is the
only process allowed to execute after package replacement; it uses fresh CLI
processes for restoration and never imports the replaced package.
"""

from __future__ import annotations

import fcntl
from contextlib import ExitStack
import hashlib
import json
import os
import pwd
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

from . import cli_update_worker


class UpdateError(RuntimeError):
    pass


def new_report() -> dict[str, Any]:
    return {
        "protocol": 1, "status": "running", "phase": "preflight",
        "environment": None, "backend": None, "inventory_complete": False,
        "package": {"state": "unchanged", "replacement_started": False,
                    "before_version": None, "after_version": None},
        "progress": {"stop_total": 0, "stopped": 0, "start_total": 0, "ready": 0},
        "instances": [], "reason": None, "recommendation": None,
        "unexecuted": ["stop", "package-update", "validation", "restore"],
    }


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise UpdateError(f"Update maintenance directory must be owned by this user and private: {path}")


def maintenance_directory(prefix: Path, *, home: Path | None = None) -> Path:
    account_home = Path(pwd.getpwuid(os.geteuid()).pw_dir) if home is None else home
    digest = hashlib.sha256(os.fsencode(prefix.resolve())).hexdigest()[:24]
    parent = account_home.resolve()
    for component in (".cache", "netizen-cli", "updates"):
        parent = parent / component
        parent.mkdir(mode=0o700, exist_ok=True)
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o022):
            raise UpdateError(f"Unsafe update maintenance parent: {parent}")
    path = parent / digest
    _private_directory(path)
    if path.resolve().is_relative_to(prefix.resolve()):
        raise UpdateError("Update maintenance files would be inside the installation being replaced")
    return path


def _lock(directory: Path) -> int:
    descriptor = os.open(directory / "update.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise UpdateError("Unrecognized update lock ownership or permissions")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise UpdateError("Another update is already maintaining this Python environment") from error
        os.set_inheritable(descriptor, True)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _reject_service_context(service_names: list[str]) -> None:
    if os.environ.get("NETIZEN_CLI_SERVICE") == "1":
        raise UpdateError("Run netizen update from an external terminal, not inside a Netizen service")
    if os.environ.get("XPC_SERVICE_NAME") in service_names:
        raise UpdateError("The update process belongs to a service it would stop; use an external terminal")
    if sys.platform == "linux":
        try:
            entries = Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines()
        except OSError as error:
            raise UpdateError("Cannot verify that the updater is outside the affected service cgroups") from error
        for entry in entries:
            components = entry.split(":", 2)[-1].split("/")
            if any(name in components for name in service_names):
                raise UpdateError("The update process belongs to a service it would stop; use an external terminal")


def _snapshot(manager: Any, prefix: Path, python: Path) -> list[dict[str, Any]]:
    instances = []
    for status in manager.list_instances(prefix=prefix):
        binding = status.binding
        if binding.prefix.resolve() != prefix.resolve():
            continue
        # Prefix proves the environment, while the lexical launcher is retained.
        # Different venvs can have the same base-interpreter realpath.
        if binding.python.parent.resolve() != python.parent.resolve():
            raise UpdateError(f"Ambiguous interpreter binding for instance {binding.root}")
        if not isinstance(status.running, bool):
            raise UpdateError(f"Cannot determine whether instance is running: {binding.root}")
        needs_stop = _needs_stop(manager, status)
        instances.append({
            "root": str(binding.root), "service": manager.service_name(binding.root),
            "python": str(binding.python), "prefix": str(binding.prefix),
            "was_running": status.running,
            "needs_stop": needs_stop,
            "state": _observed_state(manager, status), "action": None,
            "reason": None,
        })
    return instances


def _needs_stop(manager: Any, status: Any) -> bool:
    # launchd may restart a loaded KeepAlive job even while its PID is absent.
    # A loaded inactive systemd unit, by contrast, needs no stop operation.
    if manager.platform == "darwin":
        if not isinstance(status.loaded, bool):
            raise UpdateError(f"Cannot determine whether LaunchAgent is loaded: {status.binding.root}")
        return status.running or status.loaded
    return status.running


def _observed_state(manager: Any, status: Any) -> str:
    if status.running:
        return "running"
    return "loaded" if _needs_stop(manager, status) else "stopped"


def _refresh_observed(manager: Any, instances: list[dict[str, Any]]) -> None:
    for item in instances:
        try:
            current = manager.inspect(Path(item["root"]))
            if current is None:
                item["state"] = "unregistered"
            else:
                item["state"] = _observed_state(manager, current)
        except Exception:
            item["state"] = "unknown"


def stop_recorded(manager: Any, report: dict[str, Any]) -> None:
    """A partial stop has no compensation: preserve and report actual states."""
    report["phase"] = "stopping"
    for item in report["instances"]:
        if not item["needs_stop"]:
            continue
        item["action"] = "stop"
        try:
            stopped = manager.stop(Path(item["root"]))
            if _needs_stop(manager, stopped):
                raise UpdateError("Stop returned without confirming process exit and LaunchAgent unload")
            item["state"] = "stopped"
            report["progress"]["stopped"] += 1
        except (Exception, KeyboardInterrupt) as error:
            item["reason"] = str(error)
            _refresh_observed(manager, report["instances"])
            raise UpdateError(f"Stopping {item['root']} failed: {error}") from error
        if report.get("report_path"):
            cli_update_worker.persist_report(Path(report["report_path"]), report)
    # Detect already-observable external starts before the package replacement.
    for item in report["instances"]:
        try:
            current = manager.inspect(Path(item["root"]))
        except Exception:
            item["state"] = "unknown"
            raise
        if (current is None
                or current.binding.prefix.resolve() != Path(item["prefix"]).resolve()
                or str(current.binding.python) != item["python"]):
            item["state"] = "unknown"
            raise UpdateError(f"Service binding changed during update: {item['root']}")
        if _needs_stop(manager, current):
            item["state"] = _observed_state(manager, current)
            raise UpdateError(f"Instance started during update or its LaunchAgent is still loaded; "
                              f"package replacement aborted: {item['root']}")
    report["unexecuted"] = ["package-update", "validation", "restore"]


def _stage_worker(directory: Path, report: dict[str, Any], package: dict[str, Any],
                  *, json_output: bool, root_locks: dict[str, int]) -> tuple[Path, Path]:
    worker = directory / "worker.py"
    worker.write_bytes(Path(cli_update_worker.__file__).read_bytes())
    worker.chmod(0o600)
    plan_file = directory / "plan.json"
    with plan_file.open("x", encoding="utf-8") as stream:
        json.dump({"protocol": 1, "report": report, "package_plan": package,
                   "json_output": json_output, "root_locks": root_locks}, stream)
    plan_file.chmod(0o600)
    return worker, plan_file


def run_update(*, json_output: bool = False, backend: str | None = None,
               manager: Any = None, prepare: Any = None, home: Path | None = None,
               executor: Any = os.execv) -> int:
    """Report early failures, or replace this process with an external worker."""
    report = new_report()
    report["environment"] = str(Path(sys.prefix).resolve())
    report["backend"] = backend
    descriptor = None
    root_locks: dict[str, int] = {}
    maintenance = ExitStack()
    try:
        if os.environ.get("NETIZEN_CLI_SERVICE") == "1":
            raise UpdateError("Run netizen update from an external terminal, not inside a Netizen service")
        directory = maintenance_directory(Path(sys.prefix), home=home)
        descriptor = _lock(directory)
        operation = Path(tempfile.mkdtemp(prefix="operation-", dir=directory))
        report["report_path"] = str(operation / "report.json")
        if prepare is None:
            from .cli_packages import prepare_update
            prepare = prepare_update
        package = prepare(work_dir=operation, backend=backend)
        if Path(package["prefix"]).resolve() != Path(sys.prefix).resolve():
            raise UpdateError("Package update target does not match the locked current Python environment")
        report.update(environment=package["prefix"], backend=package["backend"])
        report["package"]["before_version"] = package["before_version"]
        if manager is None:
            from .cli_services import ServiceManager
            manager = ServiceManager()
        report["instances"] = _snapshot(
            manager, Path(package["prefix"]), Path(package["environment_python"]),
        )
        report["inventory_complete"] = True
        _reject_service_context([item["service"] for item in report["instances"]])
        if package["changes_required"]:
            from .deployment.update_protocol import install_lock, read_operation, terminal_phase
            from .instance import require_instance_root_marker

            # Use the very same locks as CLI mutations and Admin restarts. Take
            # every known root (including stopped instances) before stopping any.
            for item in sorted(report["instances"], key=lambda row: row["root"]):
                try:
                    root = Path(item["root"])
                    require_instance_root_marker(root)
                    lock_fd = maintenance.enter_context(install_lock(root))
                except (OSError, RuntimeError) as error:
                    raise UpdateError(f"Instance maintenance is busy or unsafe: {item['root']}: {error}") from error
                os.set_inheritable(lock_fd, True)
                root_locks[item["root"]] = lock_fd
                pending = read_operation(root)
                if pending is not None and not terminal_phase(pending["phase"]):
                    raise UpdateError(
                        f"Instance has pending maintenance ({pending['phase']}): {root}. "
                        "Let its Admin restart finish; inspect status/logs and recover an interrupted "
                        "restart with netizen start before retrying update."
                    )
            current = _snapshot(manager, Path(package["prefix"]), Path(package["environment_python"]))
            identity = lambda rows: {(row["root"], row["python"], row["prefix"]) for row in rows}
            if identity(current) != identity(report["instances"]):
                raise UpdateError("Instance inventory changed while acquiring maintenance locks; retry")
            report["instances"] = current
        count = sum(item["was_running"] for item in report["instances"])
        report["progress"].update(stop_total=sum(item["needs_stop"] for item in report["instances"]),
                                  start_total=count)
        print(f"Updating {package['prefix']} using {package['backend']}; "
              f"{count} running instance(s) affected.", file=sys.stderr)
        for item in report["instances"]:
            print(f"  {item['root']}: {item['state']}", file=sys.stderr)
        if package.get("notice"):
            print(package["notice"], file=sys.stderr)
        if not package["changes_required"]:
            report.update(status="succeeded", phase="complete", reason="No package changes are required.")
            report["package"]["after_version"] = package["before_version"]
            report["progress"].update(stop_total=0, start_total=0)
            report["unexecuted"] = ["stop (not needed)", "package-update (not needed)", "restore (not needed)"]
        else:
            print("Running tasks may be interrupted. Only previously running instances will be restored.",
                  file=sys.stderr)
            cli_update_worker.persist_report(Path(report["report_path"]), report)
            stop_recorded(manager, report)
            worker, plan_file = _stage_worker(operation, report, package,
                                             json_output=json_output, root_locks=root_locks)
            # A venv/tool may be recreated by its manager. Use the user's base
            # interpreter to keep the standard-library-only coordinator outside it.
            base_python = os.path.abspath(getattr(sys, "_base_executable", sys.executable))
            executor(base_python, [base_python, "-I", "-B", str(worker), str(plan_file)])
            raise UpdateError("Update helper handoff unexpectedly returned")
    except (Exception, KeyboardInterrupt) as error:
        report.update(status="failed", reason=f"{type(error).__name__}: {error}", recommendation=(
            "Resolve the reported problem and retry. Package replacement has not started. "
            "Any instances already stopped remain stopped; start them explicitly if needed. "
            "Unsupported or ambiguous installations can be maintained manually with their package manager."
        ))
    finally:
        maintenance.close()
        if descriptor is not None:
            os.close(descriptor)
    if report.get("report_path"):
        cli_update_worker.persist_report(Path(report["report_path"]), report)
    cli_update_worker.print_report(report, json_output=json_output)
    return 0 if report["status"] == "succeeded" else 1
