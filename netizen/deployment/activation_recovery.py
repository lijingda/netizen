"""Durable activation recovery evidence; installation owns locks and policy.

Only the standard library is available to the installer. Recovery snapshots
stay in the selected instance's private state directory. A sealed snapshot is
immutable, and an admission marker records that the candidate may have written
new user data before an interrupted installer could commit its activation.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Any

from .installer_support import InstallError, Layout


RECOVERY_PREFIX = "activation-recovery-"
MANIFEST = "manifest.json"
DATABASE_FILES = frozenset({
    "channel.sqlite3", "channel.sqlite3-journal", "channel.sqlite3-wal",
    "channel.sqlite3-shm",
})
PHASES = frozenset({
    "prepared", "snapshot", "publishing", "published", "starting",
    "committed", "restoring", "restoring_source", "database_restored", "restored", "restoring_service",
})
_ID = re.compile(r"[0-9a-f]{32}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_TEMPORARY = re.compile(r"\.(?:manifest|admission)\.[0-9a-f]{32}\.tmp")
_ADMISSION = b"netizen-candidate-admission-v1\n"
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024


def _matches(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _mode(value: object) -> bool:
    return type(value) is int and 0 <= value <= 0o777


def _validate_payload(value: object) -> dict[str, Any]:
    keys = {
        "phase", "release", "old_current", "old_previous", "old_loaded",
        "old_enabled", "should_start", "should_enable", "source_version",
        "target_version", "definition", "database_files",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise InstallError("activation recovery has an invalid payload shape")
    if (
        not isinstance(value["phase"], str) or value["phase"] not in PHASES
        or not _matches(_DIGEST, value["release"])
        or any(value[key] is not None and not _matches(_DIGEST, value[key])
               for key in ("old_current", "old_previous"))
        or any(type(value[key]) is not bool for key in (
            "old_loaded", "old_enabled", "should_start", "should_enable",
        ))
        or (value["source_version"] is not None and (
            type(value["source_version"]) is not int or value["source_version"] <= 0
        ))
        or type(value["target_version"]) is not int or value["target_version"] <= 0
    ):
        raise InstallError("activation recovery has invalid payload values")
    definition = value["definition"]
    if (
        not isinstance(definition, dict)
        or set(definition) != {"existed", "content", "mode"}
        or type(definition["existed"]) is not bool
        or not isinstance(definition["content"], str)
        or re.fullmatch(r"(?:[0-9a-f]{2})*", definition["content"]) is None
        or not _mode(definition["mode"])
        or (not definition["existed"] and definition["content"] != "")
    ):
        raise InstallError("activation recovery has an invalid service definition")
    files = value["database_files"]
    if files is not None:
        if not isinstance(files, dict) or not set(files) <= DATABASE_FILES:
            raise InstallError("activation recovery has invalid database filenames")
        for evidence in files.values():
            if (
                not isinstance(evidence, dict) or set(evidence) != {"sha256", "mode"}
                or not _matches(_DIGEST, evidence["sha256"])
                or not _mode(evidence["mode"])
            ):
                raise InstallError("activation recovery has invalid database evidence")
    return deepcopy(value)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class ActivationIntent:
    release: str
    prior_release: str | None
    should_start: bool
    should_enable: bool
    recovery: str | None = None


def decode_activation_intent(raw: str | bytes) -> ActivationIntent:
    """Decode the shared installer/runtime contract, including legacy v1 intents."""
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError) as error:
        raise InstallError("activation intent is unreadable") from error
    keys = {"version", "release", "priorRelease", "shouldStart", "shouldEnable"}
    if isinstance(value, dict) and value.get("version") == 2:
        keys.add("recovery")
    if not isinstance(value, dict) or set(value) != keys:
        raise InstallError("activation intent has an invalid shape")
    if (
        type(value["version"]) is not int or value["version"] not in {1, 2}
        or not _matches(_DIGEST, value["release"])
        or (value["priorRelease"] is not None and not _matches(_DIGEST, value["priorRelease"]))
        or type(value["shouldStart"]) is not bool
        or type(value["shouldEnable"]) is not bool
        or (value["version"] == 2 and not _matches(_ID, value["recovery"]))
    ):
        raise InstallError("activation intent has invalid values")
    return ActivationIntent(
        release=value["release"], prior_release=value["priorRelease"],
        should_start=value["shouldStart"], should_enable=value["shouldEnable"],
        recovery=value.get("recovery"),
    )


def _check_directory(metadata: os.stat_result, uid: int, *, private: bool) -> None:
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != uid
        or mode & 0o022 or (private and mode != 0o700)
    ):
        raise InstallError("activation recovery directory has unsafe type, owner or mode")


@contextmanager
def _directory(
    path: str | Path, uid: int, *, parent: int | None = None, private: bool = True,
) -> Iterator[int]:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        _check_directory(os.fstat(descriptor), uid, private=private)
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _state(state: Path, uid: int) -> Iterator[int]:
    with _directory(state.parent, uid, private=False) as product:
        with _directory(state.name, uid, parent=product, private=False) as descriptor:
            yield descriptor


def _check_file(metadata: os.stat_result, uid: int, *, private: bool = False) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != uid
        or metadata.st_nlink != 1 or not _mode(stat.S_IMODE(metadata.st_mode))
        or (private and stat.S_IMODE(metadata.st_mode) != 0o600)
    ):
        raise InstallError("activation recovery file has unsafe type, owner or mode")


@contextmanager
def _file(name: str, directory: int, uid: int, *, private: bool = False) -> Iterator[int]:
    descriptor = os.open(
        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory,
    )
    try:
        _check_file(os.fstat(descriptor), uid, private=private)
        yield descriptor
    finally:
        os.close(descriptor)


def _read(name: str, directory: int, uid: int, maximum: int) -> bytes:
    with _file(name, directory, uid, private=True) as descriptor:
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            content = stream.read(maximum + 1)
        if len(content) > maximum:
            raise InstallError("activation recovery file exceeds its maximum size")
        return content


def _evidence(name: str, directory: int, uid: int, *, sync: bool) -> dict[str, Any]:
    with _file(name, directory, uid) as descriptor:
        metadata = os.fstat(descriptor)
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if sync:
            os.fsync(descriptor)
        return {"sha256": digest.hexdigest(), "mode": stat.S_IMODE(metadata.st_mode)}


def _write_manifest(directory: int, recovery_id: str, payload: dict[str, Any]) -> None:
    content = (json.dumps({"version": 1, "id": recovery_id, "payload": payload},
                          sort_keys=True) + "\n").encode("utf-8")
    if len(content) > _MAX_MANIFEST_BYTES:
        raise InstallError("activation recovery manifest exceeds its maximum size")
    name = f".manifest.{secrets.token_hex(16)}.tmp"
    descriptor = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
        dir_fd=directory,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, MANIFEST, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(name, dir_fd=directory)
        except FileNotFoundError:
            pass


@dataclass(slots=True)
class Recovery:
    _state_dir: Path
    _uid: int
    _id: str
    _payload: dict[str, Any]

    @property
    def id(self) -> str:
        return self._id

    @property
    def root(self) -> Path:
        return self._state_dir / (RECOVERY_PREFIX + self.id)

    @property
    def payload(self) -> dict[str, Any]:
        return deepcopy(self._payload)

    @contextmanager
    def _open(self) -> Iterator[int]:
        if not _matches(_ID, self.id):
            raise InstallError("activation recovery has an invalid identifier")
        try:
            with _state(self._state_dir, self._uid) as parent:
                with _directory(self.root.name, self._uid, parent=parent) as descriptor:
                    yield descriptor
        except OSError as error:
            raise InstallError(f"could not access activation recovery {self.root}: {error}") from error

    def save(self, **changes: Any) -> None:
        payload = _validate_payload(self._payload | changes)
        if (self._payload["database_files"] is not None
                and payload["database_files"] != self._payload["database_files"]):
            raise InstallError("sealed activation recovery database evidence is immutable")
        with self._open() as directory:
            _write_manifest(directory, self.id, payload)
        self._payload = payload

    def seal_database(self, existing_files: tuple[str, ...]) -> None:
        if (not isinstance(existing_files, tuple)
                or any(not isinstance(name, str) for name in existing_files)
                or len(set(existing_files)) != len(existing_files)
                or not set(existing_files) <= DATABASE_FILES):
            raise InstallError("activation recovery has invalid snapshot filenames")
        if self._payload["database_files"] is not None:
            raise InstallError("activation recovery database is already sealed")
        with self._open() as directory:
            try:
                os.mkdir("database", mode=0o700, dir_fd=directory)
            except FileExistsError:
                pass
            with _directory("database", self._uid, parent=directory) as database:
                if set(os.listdir(database)) != set(existing_files):
                    raise InstallError("activation recovery snapshot file set differs")
                evidence = {
                    name: _evidence(name, database, self._uid, sync=True)
                    for name in existing_files
                }
                os.fsync(database)
            os.fsync(directory)
            payload = _validate_payload(self._payload | {"database_files": evidence})
            _write_manifest(directory, self.id, payload)
        self._payload = payload

    def verify_database(self) -> None:
        evidence = self._payload["database_files"]
        if evidence is None:
            raise InstallError("activation recovery database snapshot is incomplete")
        with self._open() as directory:
            with _directory("database", self._uid, parent=directory) as database:
                if set(os.listdir(database)) != set(evidence):
                    raise InstallError("activation recovery snapshot file set differs")
                for name, expected in evidence.items():
                    if _evidence(name, database, self._uid, sync=False) != expected:
                        raise InstallError(f"activation recovery database snapshot differs: {name}")

    def mark_admission(self) -> None:
        with self._open() as directory:
            try:
                existing = _read("admission", directory, self._uid, len(_ADMISSION))
            except FileNotFoundError:
                pass
            else:
                if existing != _ADMISSION:
                    raise InstallError("activation recovery admission marker is invalid")
            name = f".admission.{secrets.token_hex(16)}.tmp"
            descriptor = os.open(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600, dir_fd=directory,
            )
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(_ADMISSION)
                    stream.flush()
                    os.fsync(stream.fileno())
                # The service lifetime lock gives this marker one writer. A
                # rename leaves one complete, single-link file even if that
                # writer is killed immediately after publication.
                os.replace(name, "admission", src_dir_fd=directory, dst_dir_fd=directory)
            finally:
                try:
                    os.unlink(name, dir_fd=directory)
                except FileNotFoundError:
                    pass
            os.fsync(directory)

    def admission_observed(self) -> bool:
        with self._open() as directory:
            try:
                content = _read("admission", directory, self._uid, len(_ADMISSION))
            except FileNotFoundError:
                return False
            if content != _ADMISSION:
                raise InstallError("activation recovery admission marker is invalid")
            return True

    def remove(self) -> None:
        """Remove only this owned directory, after the caller clears its intent."""
        with self._open() as directory:
            names = os.listdir(directory)
            files: list[str] = []
            database_files: list[str] = []
            for name in names:
                if name == "database":
                    with _directory(name, self._uid, parent=directory) as database:
                        database_files = os.listdir(database)
                        if not set(database_files) <= DATABASE_FILES:
                            raise InstallError("activation recovery contains unexpected database files")
                        for entry in database_files:
                            with _file(entry, database, self._uid):
                                pass
                elif name in {MANIFEST, "admission"} or _TEMPORARY.fullmatch(name):
                    with _file(name, directory, self._uid, private=True):
                        pass
                    files.append(name)
                else:
                    raise InstallError("activation recovery contains unexpected files")
            if "database" in names:
                with _directory("database", self._uid, parent=directory) as database:
                    for name in database_files:
                        os.unlink(name, dir_fd=database)
                    os.fsync(database)
                os.rmdir("database", dir_fd=directory)
            for name in files:
                os.unlink(name, dir_fd=directory)
            os.fsync(directory)
            with _state(self._state_dir, self._uid) as parent:
                if os.stat(self.root.name, dir_fd=parent, follow_symlinks=False) != os.fstat(directory):
                    raise InstallError("activation recovery directory changed during cleanup")
                os.rmdir(self.root.name, dir_fd=parent)
                os.fsync(parent)


def create_recovery(layout: Layout, payload: dict[str, Any]) -> Recovery:
    validated = _validate_payload(payload)
    if validated["database_files"] is not None:
        raise InstallError("a new activation recovery must have an unsealed database")
    recovery = Recovery(layout.state_dir, layout.uid, secrets.token_hex(16), validated)
    try:
        with _state(layout.state_dir, layout.uid) as parent:
            os.mkdir(recovery.root.name, mode=0o700, dir_fd=parent)
            with _directory(recovery.root.name, layout.uid, parent=parent) as directory:
                _write_manifest(directory, recovery.id, validated)
            os.fsync(parent)
    except OSError as error:
        raise InstallError(f"could not create activation recovery: {error}") from error
    return recovery


def _load(state: Path, uid: int, recovery_id: str) -> Recovery:
    recovery = Recovery(state, uid, recovery_id, {})
    with recovery._open() as directory:
        try:
            value = json.loads(_read(MANIFEST, directory, uid, _MAX_MANIFEST_BYTES),
                               object_pairs_hook=_unique_object)
        except (UnicodeError, ValueError) as error:
            raise InstallError("activation recovery manifest is unreadable") from error
    if (not isinstance(value, dict) or set(value) != {"version", "id", "payload"}
            or type(value["version"]) is not int or value["version"] != 1
            or value["id"] != recovery_id):
        raise InstallError("activation recovery manifest has an invalid shape or identity")
    recovery._payload = _validate_payload(value["payload"])
    return recovery


def load_recovery(layout: Layout, recovery_id: str) -> Recovery:
    return _load(layout.state_dir, layout.uid, recovery_id)


def mark_candidate_admission(ready_file: Path, release_root: Path) -> None:
    """Persist candidate admission before runtime opens any input boundary."""
    state = ready_file.parent
    uid = os.geteuid()
    try:
        with _state(state, uid) as directory:
            try:
                raw = _read(".activation-intent.json", directory, uid, _MAX_MANIFEST_BYTES)
            except FileNotFoundError:
                return
        intent = decode_activation_intent(raw)
        if intent.recovery is None:
            return
        recovery = _load(state, uid, intent.recovery)
        payload = recovery.payload
        if (payload["release"] != intent.release
                or payload["old_current"] != intent.prior_release
                or payload["should_start"] != intent.should_start
                or payload["should_enable"] != intent.should_enable):
            raise InstallError("activation intent and recovery disagree before admission")
        if (state.name != "state" or not _matches(_DIGEST, release_root.name)
                or release_root.parent != state.parent / "releases"
                or release_root.resolve(strict=True) != release_root):
            raise InstallError("activation admission has an unmanaged release")
        with _directory(release_root, uid, private=False):
            pass
        if (release_root.name == payload["old_current"]
                and payload["phase"] in {"restoring_service", "restored"}):
            # Reinstallation can restore the same digest as its candidate.
            return
        if release_root.name != intent.release:
            raise InstallError("activation admission release differs from the candidate")
        if payload["phase"] not in {"starting", "committed"}:
            raise InstallError("activation candidate is not ready to admit input")
        recovery.mark_admission()
    except OSError as error:
        raise InstallError(f"could not persist activation admission: {error}") from error
