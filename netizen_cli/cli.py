"""Lightweight public CLI. Query/help paths do not import the Channel runtime."""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from . import __version__
from .instance import resolve_instance_root


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="netizen", description="Manage Netizen instances and this Python installation.")
    result.add_argument("--version", action="version", version=f"netizen {__version__}")
    result.add_argument("--root", dest="global_root", help="instance data root (not valid for update)")
    result.add_argument("--json", dest="global_json", action="store_true", help="machine-readable result")
    commands = result.add_subparsers(dest="command")
    for name, description in (
        ("setup", "Prepare instance configuration and register its service; do not start"),
        ("start", "Start the bound service, or register retained instance data in this environment"),
        ("stop", "Stop and confirm exit; retain the service binding"),
        ("restart", "Restart using the existing service's Python environment"),
        ("status", "Show the selected instance's binding and runtime state"),
        ("logs", "Read the selected instance's logs"),
        ("doctor", "Read-only diagnostics; list all user instances unless --root is given"),
        ("remove", "Remove an instance service; preserve data unless --purge"),
        ("_serve", "Internal service-manager entry; not an interactive command"),
    ):
        command = commands.add_parser(name, help=description)
        command.add_argument("--root", help=(
            "diagnose only this instance; omitted: list all user instances, ignoring NETIZEN_ROOT"
            if name == "doctor" else "instance data root"))
        command.add_argument("--json", action="store_true", help="machine-readable result")
        if name == "remove":
            command.add_argument("--purge", action="store_true", help="also delete the displayed owned instance data")
            command.add_argument("-y", "--yes", action="store_true", help="skip confirmation, not safety checks")
        if name == "setup":
            command.add_argument("--admin-port", type=_port)
        if name == "logs":
            command.add_argument("--lines", type=_lines, default=100)
    update = commands.add_parser(
        "update", help="Update this installation and restore its previously running services",
        description=(
            "Update this Python environment and restore its previously running services.\n"
            "Run from an external terminal. --root is not accepted; NETIZEN_ROOT does\n"
            "not limit the update scope."),
        epilog=(
            "Agent/script example:\n"
            "  netizen update --json >result.json 2>progress.log\n\n"
            "Use the final JSON report and exit code to determine the outcome.\n"
            "Package verification and instance readiness are reported separately."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    update.add_argument("--json", action="store_true", help=(
        "write one final JSON object to stdout; progress and package-manager output go to stderr"))
    update.add_argument("--via", choices=("pip", "uv-pip", "uv-tool"),
                        help="resolve an ambiguous supported maintenance method; target checks still apply")
    return result


def _port(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return number


def _lines(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 10000:
        raise argparse.ArgumentTypeError("lines must be between 1 and 10000")
    return number


def _emit(value: dict, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(value, ensure_ascii=False, default=str))
    else:
        for key, item in value.items():
            print(f"{key}: {json.dumps(item, ensure_ascii=False, default=str) if isinstance(item, (dict, list)) else item}")


def _status_value(status: object | None, root: Path) -> dict:
    if status is None:
        return {"root": str(root), "bound": False, "running": False, "ready": False}
    value = asdict(status)
    value["bound"] = True
    value["root"] = str(root)
    return value


def _current_binding(root: Path):
    from .cli_services import ServiceBinding

    codex_home = os.environ.get("CODEX_HOME")
    return ServiceBinding(root=root, python=Path(os.path.abspath(sys.executable)),
                          prefix=Path(sys.prefix).resolve(),
                          codex_home=Path(codex_home).expanduser().absolute() if codex_home else None)


def _doctor_command(root: Path | None, *, json_output: bool, report: dict) -> int:
    from .cli_services import ServiceManager

    manager = ServiceManager()
    if root is None:
        prefix = Path(sys.prefix).resolve()
        instances = [
            {**_status_value(status, status.binding.root),
             "current_environment": status.binding.prefix == prefix}
            for status in manager.list_instances()
        ]
        value = {"count": len(instances), "instances": instances}
    else:
        report.update(root=str(root), total=1, instance="unknown")
        value = _status_value(manager.inspect(root), root)
    value["cli"] = {"python": os.path.abspath(sys.executable),
                    "prefix": sys.prefix, "version": __version__}
    value["note"] = "Read-only service diagnostics; does not migrate or open a running instance's database."
    _emit(value, json_output=json_output)
    return 0


def _confirm_removal(root: Path, status: object | None, inventory: Sequence[Path],
                     *, purge: bool, yes: bool) -> None:
    """Always disclose the exact scope, even when confirmation is pre-authorized."""
    print(f"Instance: {root}", file=sys.stderr)
    print(f"Service: {_status_value(status, root)}", file=sys.stderr)
    print("Remove: this instance's verified service definition and autostart registration", file=sys.stderr)
    for path in inventory:
        print(f"Delete: {path}", file=sys.stderr)
    print("Preserve: instance data" if not purge else
          "Preserve: root, ownership marker, unknown files, Projects and shared Codex state", file=sys.stderr)
    if purge:
        print("Deleted data is not automatically recoverable. Back up anything you need first.", file=sys.stderr)
    if yes:
        return
    if not sys.stdin.isatty():
        raise RuntimeError("confirmation required; review the scope and rerun with -y, or use an interactive terminal")
    print("Proceed? [y/N] ", end="", file=sys.stderr, flush=True)
    if sys.stdin.readline().strip().lower() not in {"y", "yes"}:
        raise RuntimeError("cancelled; no service or data changes were performed")


def _instance_command(args: argparse.Namespace, root: Path, *, json_output: bool,
                      report: dict) -> int:
    from .cli_services import ServiceManager

    manager = ServiceManager()
    command = args.command
    report.update(root=str(root), phase="preflight", completed=0,
                  total={"setup": 3, "start": 2, "stop": 1, "restart": 2,
                         "remove": 3}.get(command, 1), instance="unknown")
    if command in {"status", "logs"}:
        if command == "logs":
            output = manager.logs(root, lines=args.lines)
            if output is not None:
                _emit({"root": str(root), "logs": output}, json_output=json_output)
            return 0
        status = manager.inspect(root)
        value = _status_value(status, root)
        _emit(value, json_output=json_output)
        return 0

    from .cli_data import (begin_instance_setup, ensure_instance_root, initialize_instance_data,
                           instance_lifetime_lock, purge_instance_data, purge_inventory,
                           root_maintenance_lock, validate_prepared_instance)
    from .deployment.restart_worker import assert_no_pending_restart

    if command == "setup":
        from .cli_setup import prepare_configuration, require_codex_login

        existing = manager.inspect(root)
        if existing is not None:
            _emit({"phase": "setup", "status": "already_registered",
                   "instance": _status_value(existing, root),
                   "recommendation": (
                       "Existing binding and autostart setting retained; use start for this root to run it. "
                       "To repair interrupted registration, use remove (without --purge), then setup "
                       "for the same root from the intended Python environment. To switch environments, "
                       "use remove (without --purge), then start from the new environment."
                   )},
                  json_output=json_output)
            return 0
        require_codex_login()
        ensure_instance_root(root)
        with root_maintenance_lock(root) as maintenance_descriptor:
            assert_no_pending_restart(root, lock_descriptor=maintenance_descriptor)
            if manager.inspect(root) is not None:
                raise RuntimeError("instance was registered concurrently; inspect its status before retrying")
            with instance_lifetime_lock(root) as descriptor:
                report.update(phase="prepare_instance")
                begin_instance_setup(root, lifetime_descriptor=descriptor)
                report.update(phase="prepare_configuration")
                prepare_configuration(root, admin_port=args.admin_port)
                report.update(phase="initialize", completed=1)
                initialize_instance_data(root, lifetime_descriptor=descriptor)
                report.update(phase="register", completed=2)
                manager.register(_current_binding(root), lifetime_descriptor=descriptor)
        _emit({"phase": "setup", "status": "prepared", "root": str(root),
               "recommendation": f"Run netizen start --root {root}"}, json_output=json_output)
        return 0

    with root_maintenance_lock(root) as maintenance_descriptor:
        assert_no_pending_restart(root, lock_descriptor=maintenance_descriptor)
        status = manager.inspect(root)
        report["instance"] = _status_value(status, root)
        if command == "start":
            if status is None:
                report["phase"] = "register"
                with instance_lifetime_lock(root) as descriptor:
                    validate_prepared_instance(root)
                    binding = _current_binding(root)
                    manager.register(binding, lifetime_descriptor=descriptor)
                    # Registration and process readiness are separate facts.
                    # Do not lose the known binding if the following start fails.
                    report["registered_binding"] = asdict(binding)
            elif status is not None:
                report["registered_binding"] = asdict(status.binding)
            report.update(phase="start", completed=1, instance="unknown until ready is verified")
            result = manager.start(root)
        elif command == "stop":
            report.update(phase="stop", instance="unknown until exit is verified")
            if status is None:
                with instance_lifetime_lock(root):
                    pass
                result = None
            else:
                result = manager.stop(root)
        elif command == "restart":
            if status is None:
                raise RuntimeError("instance has no service binding; use start explicitly to register it")
            report.update(phase="stop", instance="unknown until exit is verified")
            stopped = manager.stop(root)
            report.update(phase="start", completed=1,
                          last_confirmed=_status_value(stopped, root),
                          instance="unknown until ready is verified")
            result = manager.start(root)
        elif command == "remove":
            protected_codex_home = status.binding.codex_home if status is not None else None
            inventory = purge_inventory(root, protected_codex_home=protected_codex_home) if args.purge else ()
            _confirm_removal(root, status, inventory, purge=args.purge, yes=args.yes)
            if status is not None:
                report.update(phase="stop", instance="unknown until exit is verified")
                stopped = manager.stop(root)
                report.update(phase="unregister", completed=1,
                              last_confirmed=_status_value(stopped, root),
                              instance="unknown until unregistration is verified")
                manager.remove(root)
            report.update(phase="purge" if args.purge else "verify_removed", completed=2,
                          instance="service binding removed; verifying no running owner")
            with instance_lifetime_lock(root) as descriptor:
                if manager.inspect(root, lifetime_descriptor=descriptor) is not None:
                    raise RuntimeError("service binding still exists; no data was deleted")
                if args.purge:
                    inventory = purge_instance_data(root, expected_inventory=inventory,
                                                    lifetime_descriptor=descriptor,
                                                    protected_codex_home=protected_codex_home)
            _emit({"phase": "remove", "status": "removed", "root": str(root),
                   "deleted": [str(path) for path in inventory],
                   "data": "purged owned files" if args.purge else "preserved",
                   "recommendation": "Use setup for a new instance." if args.purge else
                   "Use start from the intended Python environment to register this retained instance."},
                  json_output=json_output)
            return 0
        else:
            raise RuntimeError(f"unknown instance operation: {command}")
        if command in {"start", "restart"}:
            from .deployment.restart_worker import finish_manual_restart

            try:
                operation = finish_manual_restart(root, lock_descriptor=maintenance_descriptor, status=result)
                if operation is not None and operation.get("phase") not in {"recovered", "succeeded", "failed"}:
                    report["maintenance_warning"] = "Service is ready, but a prior Admin operation still needs reconciliation."
            except (OSError, ValueError, RuntimeError) as error:
                report["maintenance_warning"] = f"Service is ready; Admin operation reconciliation failed: {error}"
    _emit({**report, "phase": command, "status": "succeeded", "completed": report["total"],
           "instance": _status_value(result, root)},
          json_output=json_output)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    argument_parser = parser()
    args = argument_parser.parse_args(argv)
    if args.command is None:
        argument_parser.print_help()
        return 0
    json_output = args.global_json or args.json
    if args.command == "update" and args.global_root is not None:
        argument_parser.error("update is environment-wide and does not accept --root")
    report = {"command": args.command, "phase": "preflight", "completed": 0,
              "status": "pending"}
    try:
        if args.command == "update":
            from .cli_update import run_update

            return run_update(json_output=json_output, backend=args.via)
        selected_root = args.root if args.root is not None else args.global_root
        if args.command == "doctor":
            root = resolve_instance_root(selected_root) if selected_root is not None else None
            return _doctor_command(root, json_output=json_output, report=report)
        root = resolve_instance_root(selected_root)
        if args.command == "_serve":
            from .service_launcher import launch

            launch(root)
            return 0
        return _instance_command(args, root, json_output=json_output, report=report)
    except KeyboardInterrupt:
        _emit({**report, "status": "interrupted",
               "reason": "operation interrupted; completed steps are not automatically rolled back",
               "recommendation": "Inspect service and data state before retrying."}, json_output=json_output)
        return 130
    except (OSError, ValueError, RuntimeError) as error:
        _emit({**report, "status": "failed", "reason": str(error),
               "deleted": [str(path) for path in getattr(error, "deleted", ())],
               "recommendation": "Inspect status and logs; fix the reported problem before retrying. "
               "No automatic rollback is performed."}, json_output=json_output)
        return 1
