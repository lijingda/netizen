"""Bounded deployment-owned state shared by Admin and the one-shot updater.

This module deliberately uses only the standard library: downloaded installers
read the same protocol before a candidate virtual environment exists.
"""

from __future__ import annotations

import fcntl
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import time
from typing import Any
from collections.abc import Iterator


OPERATION_FILE = "update.json"
MAX_OPERATION_BYTES = 4096
OPERATION_ID = re.compile(r"[0-9a-f]{32}")
SHA256 = re.compile(r"[0-9a-f]{64}")
VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
PHASES = frozenset({
    "accepted", "downloading", "preparing", "installing", "restarting",
    "succeeded", "failed", "rolled_back", "requires_action", "recovery_required", "recovered",
})
TERMINAL_PHASES = frozenset({
    "succeeded", "failed", "rolled_back", "requires_action", "recovery_required", "recovered",
})
CODES = frozenset({
    "none", "download_failed", "installer_invalid", "preparation_failed",
    "configuration_required", "permissions_required", "activation_failed",
    "rollback_incomplete", "installer_failed", "worker_interrupted", "lock_busy",
    "operation_invalid", "dispatch_failed", "dispatch_unknown", "worker_lost",
    "previous_release_changed", "profile_failed", "manual_recovery",
})
ENV_OPERATION_ID = "NETIZEN_UPDATE_OPERATION_ID"
ENV_LOCK_FD = "NETIZEN_UPDATE_LOCK_FD"
ENV_VERSION = "NETIZEN_UPDATE_VERSION"
ENV_ARCHIVE_SHA256 = "NETIZEN_UPDATE_ARCHIVE_SHA256"


class UpdateProtocolError(RuntimeError):
    """The deployment state cannot safely be read or changed."""


def _matches(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def validate_target(value: object) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "releaseId", "installerSha256", "archiveSha256"}
        or not _matches(VERSION, value.get("version"))
        or type(value.get("releaseId")) is not int
        or not 0 < value["releaseId"] < 2**63
        or not _matches(SHA256, value.get("installerSha256"))
        or not _matches(SHA256, value.get("archiveSha256"))
    ):
        raise UpdateProtocolError("invalid update target")
    return dict(value)


def validate_operation(value: object) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != {
            "schema", "operationId", "target", "previousRelease", "phase", "code",
            "createdAt", "updatedAt",
        }
        or type(value.get("schema")) is not int or value["schema"] != 1
        or not _matches(OPERATION_ID, value.get("operationId"))
        or not _matches(SHA256, value.get("previousRelease"))
        or not isinstance(value.get("phase"), str) or value["phase"] not in PHASES
        or not isinstance(value.get("code"), str) or value["code"] not in CODES
        or type(value.get("createdAt")) is not int
        or type(value.get("updatedAt")) is not int
        or not 0 <= value["createdAt"] <= value["updatedAt"] < 2**63
    ):
        raise UpdateProtocolError("invalid update operation")
    return {**value, "target": validate_target(value["target"])}


def new_operation(target: object, previous_release: str) -> dict[str, Any]:
    now = int(time.time())
    return validate_operation({
        "schema": 1, "operationId": secrets.token_hex(16),
        "target": validate_target(target), "previousRelease": previous_release,
        "phase": "accepted", "code": "none", "createdAt": now, "updatedAt": now,
    })


def terminal_phase(phase: str) -> bool:
    return phase in TERMINAL_PHASES


def _state_directory(product_root: Path) -> Path:
    for directory in (product_root, product_root / "state"):
        info = directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise UpdateProtocolError("unsafe update state directory")
    return product_root / "state"


def _check_file(info: os.stat_result, *, private: bool = True) -> None:
    if (
        not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or (private and stat.S_IMODE(info.st_mode) != 0o600)
    ):
        raise UpdateProtocolError("unsafe update state file")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate update state field")
        value[key] = item
    return value


def read_operation(product_root: Path) -> dict[str, Any] | None:
    try:
        directory = _state_directory(product_root)
        descriptor = os.open(directory / OPERATION_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise UpdateProtocolError("could not read update operation") from error
    try:
        _check_file(os.fstat(descriptor))
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            payload = source.read(MAX_OPERATION_BYTES + 1)
        if len(payload) > MAX_OPERATION_BYTES:
            raise UpdateProtocolError("update operation exceeds size limit")
        return validate_operation(json.loads(payload, object_pairs_hook=_unique_json_object))
    except (ValueError, UnicodeError, OSError, RecursionError) as error:
        raise UpdateProtocolError("could not read update operation") from error
    finally:
        os.close(descriptor)


def write_operation(product_root: Path, operation: object) -> None:
    value = validate_operation(operation)
    payload = (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode()
    if len(payload) > MAX_OPERATION_BYTES:
        raise UpdateProtocolError("update operation exceeds size limit")
    temporary: str | None = None
    try:
        directory = _state_directory(product_root)
        destination = directory / OPERATION_FILE
        try:
            _check_file(destination.lstat())
        except FileNotFoundError:
            pass
        descriptor, temporary = tempfile.mkstemp(prefix=".update-", dir=directory)
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        temporary = None
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise UpdateProtocolError("could not write update operation") from error
    finally:
        if temporary is not None:
            os.unlink(temporary)


def advance_operation(product_root: Path, operation_id: str, phase: str, code: str = "none") -> dict[str, Any]:
    """Write a phase while the caller owns the shared installation lock."""
    operation = read_operation(product_root)
    if operation is None or operation["operationId"] != operation_id:
        raise UpdateProtocolError("update operation changed")
    operation.update(phase=phase, code=code, updatedAt=max(int(time.time()), operation["updatedAt"]))
    write_operation(product_root, operation)
    return operation


def acquire_install_lock(product_root: Path, *, blocking: bool = False) -> int:
    """Return the shared lock FD; closing it releases this acquisition."""
    directory = _state_directory(product_root)
    descriptor = os.open(directory / ".install.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        _check_file(os.fstat(descriptor), private=False)
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def validate_inherited_lock(product_root: Path, descriptor: int) -> None:
    """Accept only the worker's exact locked open-file description."""
    os.set_inheritable(descriptor, False)
    directory = _state_directory(product_root)
    inherited = os.fstat(descriptor)
    _check_file(inherited)
    expected = (directory / ".install.lock").lstat()
    _check_file(expected)
    if (inherited.st_dev, inherited.st_ino) != (expected.st_dev, expected.st_ino):
        raise UpdateProtocolError("update lock descriptor does not match")
    # A separately opened FD must see a held lock; the inherited open-file
    # description must already own it, rather than some unrelated process.
    probe = os.open(directory / ".install.lock", os.O_RDWR | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise UpdateProtocolError("inherited update lock is not held")
    finally:
        os.close(probe)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


@contextmanager
def install_lock(product_root: Path, *, blocking: bool = False) -> Iterator[int]:
    descriptor = acquire_install_lock(product_root, blocking=blocking)
    try:
        yield descriptor
    finally:
        os.close(descriptor)
