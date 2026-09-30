"""Atomic persistence of the Admin port allocated during first startup."""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml


class AdminPortConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ConfigFileSnapshot:
    path: Path
    content: bytes = field(repr=False)
    identity: tuple[int, ...]

    @classmethod
    def read(cls, path: Path) -> "ConfigFileSnapshot":
        if not stat.S_ISREG(path.lstat().st_mode):
            raise AdminPortConfigurationError(f"configuration must be a regular file: {path}")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise AdminPortConfigurationError(f"configuration must be a regular file: {path}")
            content = source.read()
            after = os.fstat(source.fileno())
        identity = _identity(before)
        if identity != _identity(after) or identity != _identity(path.lstat()):
            raise AdminPortConfigurationError(f"configuration changed while reading: {path}")
        return cls(path, content, identity)

    def require_unchanged(self) -> None:
        if self != self.read(self.path):
            raise AdminPortConfigurationError(
                f"configuration changed since startup; retry with the current configuration: {self.path}"
            )


def _identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_size,
        metadata.st_mtime_ns, metadata.st_ctime_ns,
    )


def persist_admin_port(snapshot: ConfigFileSnapshot, port: int) -> None:
    """Preserve all YAML values and refuse a detected concurrent human edit.

    The caller retains its listening socket and instance lifetime lock. This
    deliberately does not acquire the CLI maintenance lock (the CLI may still
    be waiting for this startup). Only the previously absent port is added.
    """

    _validate_port(port)
    loaded = yaml.safe_load(snapshot.content)
    if not isinstance(loaded, dict):
        raise AdminPortConfigurationError("configuration root must be a mapping")
    values = loaded.setdefault("adminWeb", {})
    if not isinstance(values, dict) or "port" in values or values.get("enabled", True) is not True:
        raise AdminPortConfigurationError("Admin port allocation requires enabled Admin and absent port")
    # YAML aliases may share this mapping with another key. Updating Admin
    # must not mutate those unrelated values through Python object identity.
    values = dict(values)
    loaded["adminWeb"] = values
    values["port"] = port
    _write_configuration(snapshot, loaded)


def _validate_port(port: int) -> None:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise AdminPortConfigurationError("Admin port must be from 1 to 65535")


def _write_configuration(snapshot: ConfigFileSnapshot, loaded: dict) -> None:
    content = yaml.safe_dump(loaded, allow_unicode=True, sort_keys=False).encode("utf-8")
    snapshot.require_unchanged()
    descriptor, temporary_name = tempfile.mkstemp(prefix=".admin-port-", dir=snapshot.path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        snapshot.require_unchanged()
        os.replace(temporary, snapshot.path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
