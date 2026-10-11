"""Private administrator membership, independent of Channel data and credentials.

The service is the sole online writer. Offline setup uses the instance lifetime
lock. The shared reentrant lock also orders local session admission/revocation.
"""
from __future__ import annotations

import contextlib
import json
import math
import os
import re
import stat
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any


class PrivateJSONError(RuntimeError):
    """Never exposes filesystem paths or private JSON content."""

    def __init__(self) -> None:
        super().__init__("private Admin configuration is unavailable or invalid")


class AdminUsersError(RuntimeError):
    code = "admin_users_error"


class AdminUsersUnavailable(AdminUsersError):
    code = "admin_users_unavailable"

    def __init__(self) -> None:
        super().__init__("administrator configuration is unavailable")


class AdminUsersConflict(AdminUsersError):
    code = "admin_users_conflict"

    def __init__(self) -> None:
        super().__init__("administrator list changed; reload before retrying")


class AdminPermissionDenied(AdminUsersError):
    code = "admin_permission_denied"

    def __init__(self) -> None:
        super().__init__("current administrator permission is required")


class LastAdministrator(AdminUsersError):
    code = "last_administrator"

    def __init__(self) -> None:
        super().__init__("the last administrator cannot be removed")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PrivateJSONError()
        result[key] = value
    return result


def _private_metadata(metadata: os.stat_result) -> None:
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) not in (0o400, 0o600)
            or metadata.st_nlink != 1):
        raise PrivateJSONError()


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_uid,
            metadata.st_gid, metadata.st_nlink, metadata.st_size,
            metadata.st_mtime_ns, metadata.st_ctime_ns)


def _private_parent(path: Path, *, create: bool) -> None:
    if not path.is_absolute():
        raise PrivateJSONError()
    # The selected instance root must already exist; never create a new root.
    parent = path.parent
    if create:
        parent.mkdir(mode=0o700, exist_ok=True)
    metadata = parent.lstat()
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077):
        raise PrivateJSONError()


def read_private_json(
    path: Path, *, missing_ok: bool = False, maximum: int = 1_048_576,
) -> dict[str, Any] | None:
    """Read one owned private regular file, without following its final symlink."""
    try:
        if not path.is_absolute():
            raise PrivateJSONError()
        try:
            _private_parent(path, create=False)
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        except FileNotFoundError:
            if missing_ok:
                return None
            raise PrivateJSONError() from None
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            _private_metadata(before)
            if before.st_size > maximum:
                raise PrivateJSONError()
            content = stream.read(maximum + 1)
            after = os.fstat(stream.fileno())
            _private_metadata(after)
            if len(content) > maximum or _file_identity(before) != _file_identity(after):
                raise PrivateJSONError()
        value = json.loads(content, object_pairs_hook=_unique_object,
                           parse_constant=lambda _value: _reject_json_constant())
        if not isinstance(value, dict):
            raise PrivateJSONError()
        return value
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise PrivateJSONError() from None


def _reject_json_constant() -> Any:
    raise PrivateJSONError()


def write_private_json(
    path: Path, value: dict[str, Any], *, maximum: int = 1_048_576,
) -> None:
    """Atomically replace private JSON; callers fail closed on uncertain commits."""
    temporary: Path | None = None
    descriptor: int | None = None
    try:
        content = (json.dumps(value, ensure_ascii=False, allow_nan=False,
                              separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
        if len(content) > maximum:
            raise PrivateJSONError()
        _private_parent(path, create=True)
        try:
            _private_metadata(path.lstat())
        except FileNotFoundError:
            pass
        descriptor, filename = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(filename)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = None
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except (OSError, TypeError, ValueError, UnicodeError):
        raise PrivateJSONError() from None
    finally:
        if descriptor is not None:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink()


@dataclass(frozen=True, slots=True)
class AdminIdentity:
    app_id: str
    open_id: str

    def __post_init__(self) -> None:
        if (not isinstance(self.app_id, str)
                or not re.fullmatch(r"cli_[A-Za-z0-9_-]{1,128}", self.app_id)
                or not isinstance(self.open_id, str)
                or not re.fullmatch(r"ou_[A-Za-z0-9_-]{1,256}", self.open_id)):
            raise ValueError("invalid administrator identity")


@dataclass(frozen=True, slots=True)
class AdminMember:
    identity: AdminIdentity
    add_revision: int
    name: str = ""
    avatar_url: str | None = None
    profile_source: str | None = None
    profile_updated_at: float | None = None


def _profile_text(value: object, maximum: int, *, optional: bool = False) -> bool:
    return (optional and value is None) or (
        isinstance(value, str) and len(value.encode("utf-8")) <= maximum
        and not any(ord(character) < 0x20 for character in value)
    )


class AdminUsers:
    def __init__(self, root: Path, app_id: str) -> None:
        AdminIdentity(app_id, "ou_validation")
        self.path = root / "state/admin-users.json"
        self.app_id = app_id
        self.lock = threading.RLock()
        self._available = True
        self._revision = 0
        self._members: dict[AdminIdentity, AdminMember] = {}
        self._revocations: list[Callable[[AdminIdentity], None]] = []
        self._load()

    def _require_available(self) -> None:
        if not self._available:
            raise AdminUsersUnavailable()

    @property
    def has_admins(self) -> bool:
        with self.lock:
            self._require_available()
            return any(identity.app_id == self.app_id for identity in self._members)

    @property
    def revision(self) -> int:
        with self.lock:
            self._require_available()
            return self._revision

    def list(self) -> tuple[AdminMember, ...]:
        with self.lock:
            self._require_available()
            return tuple(member for member in self._members.values() if member.identity.app_id == self.app_id)

    def get_membership(self, identity: AdminIdentity) -> AdminMember | None:
        with self.lock:
            self._require_available()
            return self._members.get(identity) if identity.app_id == self.app_id else None

    def subscribe_revocation(self, callback: Callable[[AdminIdentity], None]) -> None:
        """Listeners run synchronously under the shared lock; no network or awaits."""
        with self.lock:
            self._revocations.append(callback)

    def _revoke(self, identity: AdminIdentity) -> None:
        failed = False
        for callback in self._revocations:
            try:
                callback(identity)
            except Exception:
                # A failed credential-file cleanup cannot prevent local session
                # revocation, regardless of listener registration order.
                failed = True
        if failed:
            raise AdminUsersUnavailable() from None

    def _unavailable(self) -> None:
        self._available = False
        for identity in self._members:
            with contextlib.suppress(AdminUsersUnavailable):
                self._revoke(identity)

    def _load(self) -> None:
        try:
            value = read_private_json(self.path, missing_ok=True)
            if value is None:
                return
            if (set(value) != {"version", "revision", "members"}
                    or type(value["version"]) is not int or value["version"] != 1
                    or type(value["revision"]) is not int or value["revision"] < 0
                    or not isinstance(value["members"], list) or len(value["members"]) > 1_024):
                raise PrivateJSONError()
            members: dict[AdminIdentity, AdminMember] = {}
            for row in value["members"]:
                if (not isinstance(row, dict)
                        or set(row) != {"appId", "openId", "addRevision", "name", "avatarUrl", "profileSource", "profileUpdatedAt"}
                        or type(row["addRevision"]) is not int
                        or not 0 < row["addRevision"] <= value["revision"]
                        or not _profile_text(row["name"], 1_024)
                        or not _profile_text(row["avatarUrl"], 4_096, optional=True)
                        or not _profile_text(row["profileSource"], 128, optional=True)
                        or (row["profileUpdatedAt"] is not None and (
                            type(row["profileUpdatedAt"]) not in (int, float)
                            or not math.isfinite(row["profileUpdatedAt"])
                            or row["profileUpdatedAt"] < 0))):
                    raise PrivateJSONError()
                identity = AdminIdentity(row["appId"], row["openId"])
                if identity in members:
                    raise PrivateJSONError()
                members[identity] = AdminMember(identity, row["addRevision"], row["name"],
                                                row["avatarUrl"], row["profileSource"], row["profileUpdatedAt"])
            self._members, self._revision = members, value["revision"]
        except (PrivateJSONError, ValueError, TypeError):
            self._unavailable()
            raise AdminUsersUnavailable() from None

    def _save(self, members: dict[AdminIdentity, AdminMember], revision: int) -> None:
        value = {"version": 1, "revision": revision, "members": [
            {"appId": member.identity.app_id, "openId": member.identity.open_id, "addRevision": member.add_revision,
             "name": member.name, "avatarUrl": member.avatar_url,
             "profileSource": member.profile_source, "profileUpdatedAt": member.profile_updated_at}
            for member in members.values()
        ]}
        try:
            write_private_json(self.path, value)
        except PrivateJSONError:
            # A failure after replace may already have committed the new list.
            # Never grant access from the old in-memory list in this process.
            self._unavailable()
            raise AdminUsersUnavailable() from None
        self._members, self._revision = members, revision

    def _check_mutation(self, actor: AdminIdentity, expected_revision: int) -> None:
        self._require_available()
        if actor.app_id != self.app_id or actor not in self._members:
            raise AdminPermissionDenied()
        if type(expected_revision) is not int or expected_revision != self._revision:
            raise AdminUsersConflict()

    def _new_member(self, identity: AdminIdentity, *, name: str, avatar_url: str | None) -> AdminMember:
        if (not isinstance(identity, AdminIdentity) or identity.app_id != self.app_id
                or not _profile_text(name, 1_024) or not _profile_text(avatar_url, 4_096, optional=True)):
            raise ValueError("invalid administrator or profile")
        if identity in self._members or len(self._members) >= 1_024:
            raise AdminUsersConflict()
        return AdminMember(identity, self._revision + 1, name, avatar_url)

    def add_initial(self, open_id: str, *, name: str = "", avatar_url: str | None = None) -> AdminMember:
        """Offline setup only; the caller must hold the instance lifetime lock."""
        with self.lock:
            self._require_available()
            if self.has_admins:
                raise AdminUsersConflict()
            member = self._new_member(AdminIdentity(self.app_id, open_id), name=name, avatar_url=avatar_url)
            self._save({**self._members, member.identity: member}, self._revision + 1)
            return member

    def add(self, identity: AdminIdentity, *, actor: AdminIdentity, expected_revision: int,
            name: str = "", avatar_url: str | None = None) -> AdminMember:
        with self.lock:
            self._check_mutation(actor, expected_revision)
            member = self._new_member(identity, name=name, avatar_url=avatar_url)
            self._save({**self._members, identity: member}, self._revision + 1)
            return member

    def remove(self, identity: AdminIdentity, *, actor: AdminIdentity,
               expected_revision: int) -> AdminMember:
        with self.lock:
            self._check_mutation(actor, expected_revision)
            member = self._members.get(identity)
            if member is None or identity.app_id != self.app_id:
                raise AdminUsersConflict()
            if len(self.list()) <= 1:
                raise LastAdministrator()
            self._save({key: value for key, value in self._members.items() if key != identity},
                       self._revision + 1)
            self._revoke(identity)
            return member

    def update_profile(self, identity: AdminIdentity, *, name: str, avatar_url: str | None,
                       source: str = "user_info", expected_add_revision: int | None = None) -> AdminMember | None:
        with self.lock:
            self._require_available()
            member = self._members.get(identity)
            if (member is None or identity.app_id != self.app_id
                    or (expected_add_revision is not None and member.add_revision != expected_add_revision)):
                return None
            if (not _profile_text(name, 1_024) or not _profile_text(avatar_url, 4_096, optional=True)
                    or not _profile_text(source, 128)):
                raise ValueError("invalid administrator profile")
            member = replace(member, name=name, avatar_url=avatar_url,
                             profile_source=source, profile_updated_at=time.time())
            self._save({**self._members, identity: member}, self._revision)
            return member
