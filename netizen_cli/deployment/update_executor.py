"""Dispatch an instance restart outside the Netizen service's lifetime."""

from __future__ import annotations

import os
import plistlib
import re
import stat
import subprocess
import sys
from pathlib import Path

from ..instance import instance_digest, resolve_instance_root


COMMAND_TIMEOUT_SECONDS = 10.0
_OPERATION_ID = re.compile(r"[0-9a-f]{32}\Z")
_BOOTSTRAP_PATH = "/usr/local/bin:/usr/bin:/bin"
_CLEARED_ENVIRONMENT = (
    "NETIZEN_ROOT",
    "FEISHU_APP_SECRET",
    "NETIZEN_ADMIN_SECRET",
    "FEISHU_APP_SECRET_FILE",
    "NETIZEN_LARK_APP_CONFIG",
    "NETIZEN_ADMIN_SECRET_FILE",
    "NETIZEN_CONFIG_PATH",
    "NETIZEN_LIFETIME_LOCK_FD",
    "NETIZEN_LIFETIME_LOCK_FILE",
    "NETIZEN_READY_FILE",
    "NETIZEN_LOG_FILE",
    "NETIZEN_MANAGED_LAUNCH_AGENT",
    "NETIZEN_CLI_SERVICE",
    "NETIZEN_CLI_PREFIX",
    "NETIZEN_CLI_PYTHON",
    "NETIZEN_UPDATE_OPERATION_ID",
    "NETIZEN_UPDATE_LOCK_FD",
    "NETIZEN_UPDATE_VERSION",
    "NETIZEN_UPDATE_ARCHIVE_SHA256",
    "PYTHONHOME",
    "PYTHONPATH",
    "VIRTUAL_ENV",
    "__PYVENV_LAUNCHER__",
)


class UpdateExecutorError(RuntimeError):
    """A platform operation could not be completed or safely observed."""


class UpdateDispatchUnknown(UpdateExecutorError):
    """The manager may have accepted this operation; do not dispatch it again."""


class UpdateExecutor:
    def __init__(self, home: Path, platform_name: str | None = None,
                 *, root: Path | None = None) -> None:
        if not home.is_absolute() or home == Path(home.anchor):
            raise UpdateExecutorError("update home must be an absolute non-root path")
        self.home = home.resolve()
        try:
            self.product_root = resolve_instance_root(root, account_home=self.home)
        except (OSError, ValueError, RuntimeError) as error:
            raise UpdateExecutorError("invalid update instance root") from error
        self.platform_name = sys.platform if platform_name is None else platform_name
        if self.platform_name not in {"linux", "darwin"}:
            raise UpdateExecutorError("updates require Linux or macOS")
        self._uid = os.geteuid()

    @staticmethod
    def _validate_operation_id(operation_id: str) -> None:
        if not isinstance(operation_id, str) or _OPERATION_ID.fullmatch(operation_id) is None:
            raise UpdateExecutorError("invalid update operation ID")

    def _label(self, operation_id: str) -> str:
        self._validate_operation_id(operation_id)
        return f"netizen-update-{instance_digest(self.product_root)}-{operation_id}"

    def _plist_path(self, operation_id: str) -> Path:
        return self.product_root / "state" / f"{self._label(operation_id)}.plist"

    def _worker_arguments(self, operation_id: str, python: Path) -> list[str]:
        self._validate_operation_id(operation_id)
        if (not python.is_absolute() or not python.is_file()
                or not os.access(python, os.X_OK)
                or any(character in str(python) for character in "\r\n\0")):
            raise UpdateExecutorError("the bound Python executable is unavailable")
        return [str(python), "-E", "-P", "-B", "-u", "-m", "netizen_cli.deployment.restart_worker",
                "--root", str(self.product_root), "--operation-id", operation_id]

    def _command(
        self, arguments: list[str], *, dispatch: bool = False, capture: bool = False
    ) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        for key in tuple(environment):
            if key.startswith("NETIZEN_") or key in _CLEARED_ENVIRONMENT:
                environment.pop(key, None)
        environment["HOME"] = str(self.home)
        if self.platform_name == "linux":
            environment.setdefault("XDG_RUNTIME_DIR", f"/run/user/{self._uid}")
            environment.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{self._uid}/bus")
        try:
            return subprocess.run(
                arguments,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
                close_fds=True,
                cwd=self.home,
                env=environment,
                timeout=COMMAND_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            exception = UpdateDispatchUnknown if dispatch else UpdateExecutorError
            raise exception("update service manager command timed out") from error
        except (FileNotFoundError, PermissionError) as error:
            # Popen did not start the manager command. Its output and inherited
            # environment are deliberately never included in user-visible errors.
            raise UpdateExecutorError("could not execute the update service manager") from error
        except OSError as error:
            exception = UpdateDispatchUnknown if dispatch else UpdateExecutorError
            raise exception("could not complete the update service manager command") from error

    def launch(self, operation_id: str, python: Path) -> None:
        """Submit once, without waiting for the instance lock or its result.

        A dispatch error after submission is ambiguous even when the manager
        command returns nonzero. The caller must inspect the operation result
        and execution lock; it must never retry this operation automatically.
        """

        label = self._label(operation_id)
        arguments = self._worker_arguments(operation_id, python)
        if self.platform_name == "linux":
            # systemd-run expands $ in argv, including argv[0]. A fixed env
            # executable lets every environment path be escaped as an argument,
            # then execs the real Python in place. Doubling argv[0] itself would
            # make systemd-run look for a nonexistent executable. Unlike unit
            # file syntax, transient D-Bus properties preserve literal %.
            worker_arguments = [value.replace("$", "$$") for value in arguments]
            result = self._command(
                [
                    "systemd-run", "--user", "--no-ask-password", f"--unit={label}.service", "--collect",
                    "--service-type=exec", "--property=Restart=no",
                    "--property=KillMode=control-group", "--property=UMask=0077",
                    "--property=StandardInput=null", "--property=StandardOutput=null",
                    "--property=StandardError=null", f"--working-directory={self.home}",
                    f"--setenv=HOME={self.home}", f"--setenv=PATH={_BOOTSTRAP_PATH}",
                    "--property=UnsetEnvironment=" + " ".join(_CLEARED_ENVIRONMENT),
                    "--", "/usr/bin/env", "--", *worker_arguments,
                ],
                dispatch=True,
            )
        else:
            domain = f"gui/{self._uid}"
            if self._command(["launchctl", "print", domain]).returncode != 0:
                raise UpdateExecutorError("the current macOS GUI launchd domain is unavailable")
            path = self._plist_path(operation_id)
            self._write_plist(path, label, arguments)
            result = self._command(["launchctl", "bootstrap", domain, str(path)], dispatch=True)
        if result.returncode != 0:
            raise UpdateDispatchUnknown("the update service manager did not confirm dispatch")

    def _write_plist(self, path: Path, label: str, arguments: list[str]) -> None:
        # A state-directory plist is submitted explicitly. It is not installed
        # in Library/LaunchAgents and cannot replay an update at the next login.
        try:
            metadata = path.parent.lstat()
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != self._uid
                or stat.S_IMODE(metadata.st_mode) & 0o022
            ):
                raise UpdateExecutorError("the update state directory is not private to this user")
            payload = plistlib.dumps(
                {
                    "Label": label,
                    "ProgramArguments": arguments,
                    "WorkingDirectory": str(self.home),
                    "RunAtLoad": True,
                    "KeepAlive": False,
                    "Umask": 0o077,
                    "EnvironmentVariables": {"HOME": str(self.home), "PATH": _BOOTSTRAP_PATH},
                    "StandardOutPath": "/dev/null",
                    "StandardErrorPath": "/dev/null",
                },
                sort_keys=True,
            )
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError as error:
            raise UpdateDispatchUnknown("this update already has a manager submission file") from error
        except OSError as error:
            raise UpdateExecutorError("could not prepare the update LaunchAgent") from error

    def is_active(self, operation_id: str) -> bool:
        """Return conservative manager presence, never infer absence on timeout.

        launchd's print exit status proves only that a job is loaded. The caller
        also uses the updater's execution lock to identify an exited worker.
        No launchctl print text or process identity is parsed here.
        """

        label = self._label(operation_id)
        if self.platform_name == "darwin":
            domain = f"gui/{self._uid}"
            if self._command(["launchctl", "print", domain]).returncode != 0:
                raise UpdateExecutorError("the current macOS GUI launchd domain is unavailable")
            result = self._command(["launchctl", "print", f"{domain}/{label}"])
            if result.returncode not in {0, 113}:
                raise UpdateExecutorError("could not determine the update LaunchAgent state")
            return result.returncode == 0
        result = self._command(
            [
                "systemctl", "--user", "show", "--property=LoadState",
                "--property=ActiveState", "--", f"{label}.service",
            ],
            capture=True,
        )
        if result.returncode != 0:
            raise UpdateExecutorError("could not determine the update service state")
        fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if fields.get("LoadState") == "not-found" and fields.get("ActiveState") == "inactive":
            return False
        if fields.get("LoadState") != "loaded":
            raise UpdateExecutorError("the update service state is unrecognized")
        state = fields.get("ActiveState")
        if state in {"active", "activating", "reloading", "deactivating", "refreshing"}:
            return True
        if state in {"inactive", "failed"}:
            return False
        raise UpdateExecutorError("the update service state is unrecognized")

    def cleanup(self, operation_id: str) -> None:
        """Remove a completed job from the service manager.

        The Admin process or explicit CLI must own the instance lock and have
        read a durable terminal result; acquiring that lock proves the worker
        has released execution. The updater must not bootout itself: launchd
        could kill it before it finishes writing its result.
        Linux's --collect performs automatic transient-unit removal.
        """

        label = self._label(operation_id)
        if self.platform_name == "linux":
            return
        if self.is_active(operation_id):
            result = self._command(["launchctl", "bootout", f"gui/{self._uid}/{label}"])
            if result.returncode != 0 and self.is_active(operation_id):
                raise UpdateExecutorError("could not remove the completed update LaunchAgent")
        try:
            self._plist_path(operation_id).unlink(missing_ok=True)
        except OSError as error:
            raise UpdateExecutorError("could not remove the completed update submission file") from error
