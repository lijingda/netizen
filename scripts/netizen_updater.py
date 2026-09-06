#!/usr/bin/env python3
"""One-shot Admin update worker, dispatched outside the main service lifetime."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from netizen.deployment.update_protocol import (  # noqa: E402
    ENV_ARCHIVE_SHA256, ENV_LOCK_FD, ENV_OPERATION_ID, ENV_VERSION,
    OPERATION_ID, SHA256, UpdateProtocolError, acquire_install_lock,
    advance_operation, read_operation, terminal_phase,
)
from scripts.netizen_service_launcher import (  # noqa: E402
    ServiceLaunchError, capture_profile_environment,
)


OFFICIAL_DOWNLOADS = "https://github.com/lijingda/netizen/releases/download"
LOCK_HANDOFF_TIMEOUT_SECONDS = 15.0
MAX_INSTALLER_BYTES = 1024 * 1024


def _worker_environment() -> dict[str, str]:
    account = pwd.getpwuid(os.geteuid())
    environment = capture_profile_environment(
        shell=Path(account.pw_shell), home=Path(account.pw_dir),
        username=account.pw_name, python_executable=Path(sys.executable),
    )
    # These describe the main service process, never an installer. The account
    # shell remains the source for native CODEX_HOME, PATH and proxy settings.
    for name in tuple(environment):
        if name.startswith("NETIZEN_") or name in {
            "FEISHU_APP_SECRET", "FEISHU_APP_SECRET_FILE", "PYTHONHOME", "PYTHONPATH",
            "VIRTUAL_ENV", "__PYVENV_LAUNCHER__",
        }:
            environment.pop(name, None)
    environment.update(HOME=account.pw_dir, USER=account.pw_name, LOGNAME=account.pw_name)
    return environment


def _lock_for_handoff(product_root: Path, *, timeout: float) -> int:
    deadline = time.monotonic() + timeout
    while True:
        try:
            return acquire_install_lock(product_root)
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))


def _matches_previous_release(product_root: Path, previous: str) -> bool:
    current = product_root / "current"
    if not current.is_symlink():
        return False
    try:
        resolved = current.resolve(strict=True)
        releases = (product_root / "releases").resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    return (
        SHA256.fullmatch(resolved.name) is not None
        and resolved.parent == releases
        and resolved.name == previous
    )


def run_update(
    operation_id: str,
    *,
    product_root: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    environment_loader: Callable[[], Mapping[str, str]] = _worker_environment,
    lock_timeout: float = LOCK_HANDOFF_TIMEOUT_SECONDS,
) -> int:
    if OPERATION_ID.fullmatch(operation_id) is None:
        return 1
    root = Path(pwd.getpwuid(os.geteuid()).pw_dir) / ".netizen" if product_root is None else product_root
    try:
        descriptor = _lock_for_handoff(root, timeout=lock_timeout)
    except (OSError, UpdateProtocolError):
        # The controller owns accepted/dispatch reconciliation. Never write
        # deployment state without the shared lock, including a lock timeout.
        return 1
    try:
        operation = read_operation(root)
        if operation is None or operation["operationId"] != operation_id or operation["phase"] != "accepted":
            return 1
        if not _matches_previous_release(root, operation["previousRelease"]):
            advance_operation(root, operation_id, "failed", "previous_release_changed")
            return 1
        try:
            environment = dict(environment_loader())
        except (ServiceLaunchError, OSError, KeyError):
            advance_operation(root, operation_id, "failed", "profile_failed")
            return 1
        target = operation["target"]
        environment.update({
            ENV_OPERATION_ID: operation_id, ENV_LOCK_FD: str(descriptor),
            ENV_VERSION: target["version"], ENV_ARCHIVE_SHA256: target["archiveSha256"],
        })
        advance_operation(root, operation_id, "downloading")
        with tempfile.TemporaryDirectory(prefix="netizen-update-") as temporary:
            bootstrap = Path(temporary) / "install.sh"
            command = [
                "curl", "-fL", "--proto", "=https", "--proto-redir", "=https",
                "--tlsv1.2", "--connect-timeout", "15", "--max-time", "120",
                "--max-filesize", str(MAX_INSTALLER_BYTES), "-o", str(bootstrap),
                f"{OFFICIAL_DOWNLOADS}/v{target['version']}/install.sh",
            ]
            try:
                downloaded = runner(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, env=environment, check=False, timeout=130)
                if downloaded.returncode != 0:
                    raise OSError("download failed")
            except (OSError, subprocess.TimeoutExpired):
                advance_operation(root, operation_id, "failed", "download_failed")
                return 1
            if (
                bootstrap.is_symlink() or not bootstrap.is_file()
                or bootstrap.stat().st_size > MAX_INSTALLER_BYTES
                or hashlib.sha256(bootstrap.read_bytes()).hexdigest() != target["installerSha256"]
            ):
                advance_operation(root, operation_id, "failed", "installer_invalid")
                return 1
            # The official, exact-version, zero-argument entry point owns the
            # complete prepare/activate/rollback transaction. Inherit only this
            # lock FD; the installer validates it and sets CLOEXEC immediately.
            result = runner(["/bin/sh", str(bootstrap)], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env=environment, pass_fds=(descriptor,), check=False)
        current = read_operation(root)
        if current is None or current["operationId"] != operation_id:
            return 1
        if result.returncode == 0:
            if not terminal_phase(current["phase"]):
                current = advance_operation(root, operation_id, "succeeded")
            return 0 if current["phase"] == "succeeded" else 1
        if current["phase"] == "succeeded":
            # A historical or malformed installer may publish success before
            # its outer bootstrap exits. A nonzero exit contradicts that claim.
            advance_operation(root, operation_id, "recovery_required", "installer_failed")
        elif not terminal_phase(current["phase"]):
            interrupted = current["phase"] in {"installing", "restarting"}
            advance_operation(root, operation_id, "recovery_required" if interrupted else "failed",
                              "worker_interrupted" if interrupted else "installer_failed")
        return 1
    except (OSError, UpdateProtocolError, KeyboardInterrupt):
        # No inferred rollback on an unexpected exit. A surviving controller
        # reconciles the abandoned operation after the shared lock is released.
        return 1
    finally:
        os.close(descriptor)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation-id", required=True)
    args = parser.parse_args(argv)
    return run_update(args.operation_id)


if __name__ == "__main__":
    raise SystemExit(main())
