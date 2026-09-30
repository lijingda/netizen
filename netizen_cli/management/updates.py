"""Instance-only Admin maintenance; package updates belong to the CLI."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import pwd
import sys
import time
from typing import Any, Callable

from .blocking_io import BoundedBlockingIOExecutor
from ..instance import resolve_instance_root
from ..deployment.update_executor import UpdateExecutor, UpdateDispatchUnknown, UpdateExecutorError
from ..deployment.update_protocol import (
    UpdateProtocolError, acquire_install_lock, advance_operation,
    new_cli_restart_operation, read_operation, terminal_phase, write_operation,
)
from ..deployment.restart_worker import START_HANDOFF_SECONDS


class UpdateError(RuntimeError):
    """Stable application failure; entry points own presentation."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class InstalledPackage:
    version: str
    python: Path
    prefix: Path
    package: Path

    @property
    def identity(self) -> str:
        # Resolving Python's symlink would merge independent virtual environments.
        payload = [self.version, str(self.python), str(self.prefix), str(self.package)]
        return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {"version": self.version, "source": "python",
                "installationId": self.identity, "python": str(self.python)}


def installed_package() -> InstalledPackage:
    try:
        version = importlib.metadata.version("netizen-cli")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
    return InstalledPackage(version, Path(os.path.abspath(sys.executable)),
                            Path(sys.prefix).resolve(), Path(__file__).resolve().parents[1])


class UpdateService:
    """One bounded management worker, with no package-manager authority."""

    def __init__(self, *, root: Path | None = None, home: Path | None = None,
                 current: InstalledPackage | None = None,
                 executor: UpdateExecutor | None = None,
                 binding_matches: Callable[[Path, InstalledPackage], bool] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.home = (home or Path(pwd.getpwuid(os.geteuid()).pw_dir)).resolve()
        self.product_root = resolve_instance_root(root, account_home=self.home)
        self._current = current or installed_package()
        self._executor = executor
        self._binding_matches = binding_matches or self._matches_bound_environment
        self._clock = clock
        self._service_ready = False
        self._startup_restart_id: str | None = None
        try:
            operation = read_operation(self.product_root)
            if (operation is not None and operation.get("schema") == 3
                    and operation["phase"] in {"restarting", "recovery_required"}):
                self._startup_restart_id = operation["operationId"]
        except (UpdateProtocolError, OSError):
            pass  # Maintenance diagnostics must not prevent normal startup.
        self._io = BoundedBlockingIOExecutor(max_workers=1, capacity=2,
                                           thread_name_prefix="netizen-maintenance-io")

    @staticmethod
    def _matches_bound_environment(root: Path, current: InstalledPackage) -> bool:
        from ..cli_services import ServiceError, ServiceManager

        try:
            status = ServiceManager().inspect(root)
            return bool(status is not None
                        and status.binding.prefix == current.prefix
                        and status.binding.python == current.python)
        except (ServiceError, OSError, ValueError):
            return False

    async def close(self, *, deadline: float | None = None) -> None:
        self.set_service_ready(False)
        await self._io.aclose(deadline=deadline)

    def set_service_ready(self, ready: bool) -> None:
        self._service_ready = ready

    async def status(self) -> dict[str, Any]:
        return await self._io.submit(self._status)

    async def restart(self, *, installation_id: str) -> dict[str, Any]:
        return await self._io.submit(self._restart, installation_id=installation_id)

    def _manager(self) -> UpdateExecutor:
        if self._executor is None:
            self._executor = UpdateExecutor(self.home, root=self.product_root)
        return self._executor

    def _restart_supported(self) -> bool:
        return (self._current.version != "unknown"
                and self._binding_matches(self.product_root, self._current))

    def _operation(self) -> dict[str, Any] | None:
        try:
            operation = read_operation(self.product_root)
            if operation is None:
                return None
            try:
                descriptor = acquire_install_lock(self.product_root)
            except BlockingIOError:
                return operation
            try:
                operation = read_operation(self.product_root)
                if operation is None:
                    return None
                if terminal_phase(operation["phase"]):
                    try:
                        self._manager().cleanup(operation["operationId"])
                    except UpdateExecutorError:
                        return operation
                    return self._recover_ready_restart(operation)
                if operation["phase"] == "accepted":
                    if (self._manager().is_active(operation["operationId"])
                            and self._clock() - operation["createdAt"] < START_HANDOFF_SECONDS):
                        return operation
                operation = advance_operation(self.product_root, operation["operationId"],
                                              "recovery_required", "worker_lost")
                try:
                    self._manager().cleanup(operation["operationId"])
                except UpdateExecutorError:
                    pass
                return operation
            finally:
                os.close(descriptor)
        except (UpdateProtocolError, OSError, UpdateExecutorError) as error:
            raise UpdateError("update_state_unavailable") from error

    def _recover_ready_restart(self, operation: dict[str, Any]) -> dict[str, Any]:
        if (operation.get("schema") == 3
                and operation["phase"] == "recovery_required"
                and operation["code"] in {"restart_failed", "worker_lost"}
                and operation["operationId"] == self._startup_restart_id
                and self._service_ready and self._restart_supported()
                and operation["target"]["installationId"] == self._current.identity):
            return advance_operation(self.product_root, operation["operationId"],
                                     "recovered", "service_ready")
        return operation

    def _status(self) -> dict[str, Any]:
        operation = self._operation()
        busy = operation is not None and (not terminal_phase(operation["phase"])
                                         or operation["phase"] == "recovery_required")
        supported = self._restart_supported()
        return {"current": self._current.as_dict(), "supported": False,
                "restartSupported": supported, "restartAvailable": supported and not busy,
                "latest": None, "available": False, "operation": operation,
                "checkingErrorCode": None, "checkedAt": None}

    def _restart(self, *, installation_id: str) -> dict[str, Any]:
        if not self._restart_supported():
            raise UpdateError("restart_unsupported")
        if installation_id != self._current.identity:
            raise UpdateError("update_installation_changed")
        try:
            descriptor = acquire_install_lock(self.product_root)
        except BlockingIOError as error:
            raise UpdateError("update_busy") from error
        except (OSError, UpdateProtocolError) as error:
            raise UpdateError("update_lock_unavailable") from error
        try:
            if not self._restart_supported():
                raise UpdateError("update_installation_changed")
            previous = read_operation(self.product_root)
            if previous is not None:
                if not terminal_phase(previous["phase"]):
                    raise UpdateError("update_already_submitted")
                if previous["phase"] == "recovery_required":
                    raise UpdateError("update_recovery_required")
                try:
                    self._manager().cleanup(previous["operationId"])
                except UpdateExecutorError as error:
                    raise UpdateError("update_cleanup_unavailable") from error
            operation = new_cli_restart_operation(self._current.version, self._current.identity)
            write_operation(self.product_root, operation)
            try:
                self._manager().launch(operation["operationId"], self._current.python)
            except UpdateDispatchUnknown:
                return operation  # May have launched; never submit twice.
            except (OSError, RuntimeError):
                return advance_operation(self.product_root, operation["operationId"],
                                         "failed", "dispatch_failed")
            return operation
        except (OSError, UpdateProtocolError) as error:
            raise UpdateError("update_submission_unknown") from error
        finally:
            os.close(descriptor)
