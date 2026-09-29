"""One-shot, instance-only restart dispatched by the service manager."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import time
from typing import Any

from .update_protocol import (
    UpdateProtocolError, acquire_install_lock, advance_operation, read_operation,
    terminal_phase, validate_inherited_lock,
)

START_HANDOFF_SECONDS = 60


def assert_no_pending_restart(root: Path, *, lock_descriptor: int,
                              now: float | None = None) -> dict[str, Any] | None:
    """Fence delayed Admin work before a CLI mutation, under the exact lock.

    A fresh accepted dispatch keeps its bounded handoff window. Acquiring this
    lock proves no worker is currently in its stop/start critical section. A
    stale record is therefore fenced before returning, and a delayed worker
    will reject it without recreating a service or maintenance record.
    """
    validate_inherited_lock(root, lock_descriptor)
    operation = read_operation(root)
    if operation is None or terminal_phase(operation["phase"]):
        return operation
    if operation.get("schema") != 3:
        raise UpdateProtocolError(
            "Legacy maintenance is unresolved; inspect the old installation before manual conversion"
        )
    if operation["phase"] == "accepted":
        observed_at = time.time() if now is None else now
        if observed_at - operation["createdAt"] < START_HANDOFF_SECONDS:
            raise UpdateProtocolError(
                f"Admin restart {operation['operationId']} is pending dispatch; "
                "wait for maintenance to finish, then retry this command"
            )
        # The worker writes restarting before stop, while retaining this lock.
        # Accepted + lock ownership proves that this request never began stop.
        return advance_operation(root, operation["operationId"], "failed", "dispatch_failed")
    # The only other nonterminal schema-3 phase is restarting. Retain unknown
    # side effects; a succeeding explicit CLI operation may prove recovery.
    return advance_operation(root, operation["operationId"], "recovery_required", "worker_lost")


def finish_manual_restart(root: Path, *, lock_descriptor: int, status: Any,
                          executor: Any = None) -> dict[str, Any] | None:
    """Reconcile a failed Admin restart after an explicit CLI ready result.

    The caller still owns the root's maintenance lock and just obtained the
    exact service manager's private ready proof. This records recovery only;
    it never rewrites the original request as success or changes its target.
    """
    from .update_executor import UpdateExecutor, UpdateExecutorError
    import pwd

    validate_inherited_lock(root, lock_descriptor)
    if (status is None or not status.running or not status.ready
            or status.binding.root != root):
        raise UpdateProtocolError("manual recovery requires the exact ready instance")
    operation = read_operation(root)
    if (operation is None or operation.get("schema") != 3
            or operation["phase"] != "recovery_required"):
        return operation
    manager = executor or UpdateExecutor(Path(pwd.getpwuid(os.geteuid()).pw_dir), root=root)
    try:
        # launchd retains exited jobs as loaded. Cleanup under the held lock
        # can unload that once-only job; querying presence first would block
        # macOS recovery forever. Linux collect needs no explicit cleanup.
        manager.cleanup(operation["operationId"])
        if manager.is_active(operation["operationId"]):
            return operation
    except UpdateExecutorError:
        return operation
    return advance_operation(root, operation["operationId"], "recovered", "manual_recovery")


def run(root: Path, operation_id: str, *, manager: Any = None) -> int:
    from ..cli_services import ServiceError, ServiceManager
    from ..management.updates import installed_package

    descriptor = acquire_install_lock(root, blocking=True)
    try:
        operation = read_operation(root)
        if (operation is None or operation.get("schema") != 3
                or operation["operationId"] != operation_id
                or operation["phase"] != "accepted"):
            return 1  # Never claim a superseded or already executed request.
        if time.time() - operation["createdAt"] >= START_HANDOFF_SECONDS:
            advance_operation(root, operation_id, "failed", "dispatch_failed")
            return 1
        current = installed_package()
        manager = manager or ServiceManager()
        status = manager.inspect(root)
        if (operation["target"]["installationId"] != current.identity
                or status is None or status.binding.python != current.python
                or status.binding.prefix != current.prefix):
            advance_operation(root, operation_id, "failed", "previous_release_changed")
            return 1
        advance_operation(root, operation_id, "restarting")
        try:
            manager.stop(root)
            # start preserves the exact existing service binding and only
            # returns after a held lifetime lock + the private ready proof.
            manager.start(root)
        except (ServiceError, OSError, RuntimeError):
            advance_operation(root, operation_id, "recovery_required", "restart_failed")
            return 1
        advance_operation(root, operation_id, "succeeded")
        return 0
    finally:
        os.close(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--operation-id", required=True)
    options = parser.parse_args()
    try:
        return run(options.root, options.operation_id)
    except (UpdateProtocolError, OSError, RuntimeError):
        # Leave the durable operation for bounded Admin reconciliation. Never
        # print manager output, credentials, or guessed success.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
