"""Manual, exact-release updates over the existing per-user installer."""

from __future__ import annotations

import copy
from dataclasses import dataclass
import importlib.metadata
import json
import os
from pathlib import Path
import pwd
import re
import stat
import sys
import time
from typing import Callable, Any
import urllib.request

from .blocking_io import BoundedBlockingIOExecutor
from ..deployment.update_executor import UpdateExecutor, UpdateDispatchUnknown, UpdateExecutorError
from ..deployment.update_protocol import (
    UpdateProtocolError, acquire_install_lock, advance_operation, new_operation,
    read_operation, terminal_phase, validate_target, write_operation,
)


RELEASES_URL = "https://github.com/lijingda/netizen/releases"
LATEST_API = "https://api.github.com/repos/lijingda/netizen/releases/latest"
MAX_RELEASE_BYTES = 256 * 1024
CHECK_CACHE_SECONDS = 60
START_HANDOFF_SECONDS = 60
_VERSION = re.compile(r"(?:0|[1-9][0-9]{0,8})\.(?:0|[1-9][0-9]{0,8})\.(?:0|[1-9][0-9]{0,8})")
_DIGEST = re.compile(r"[0-9a-f]{64}")


class UpdateError(RuntimeError):
    """Stable application failure; each entry point owns its presentation."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class InstalledRelease:
    version: str
    source: str
    root: Path | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"version": self.version, "source": self.source,
                "releaseDigest": self.root.name if self.root else None}


def _read_metadata(path: Path) -> dict[str, Any]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o022 or info.st_size > 8192):
            raise ValueError("invalid release metadata")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            value = json.loads(source.read(8193))
        if not isinstance(value, dict):
            raise ValueError("invalid release metadata")
        return value
    finally:
        os.close(descriptor)


def installed_release(home: Path) -> InstalledRelease:
    """Identify the running interpreter, never the mutable current symlink."""
    try:
        version = importlib.metadata.version("netizen")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
    prefix = Path(sys.prefix).resolve()
    root = prefix.parent
    releases = (home / ".netizen" / "releases").resolve()
    if (prefix.name != "venv" or root.parent != releases
            or _DIGEST.fullmatch(root.name) is None
            or not Path(__file__).resolve().is_relative_to(prefix)):
        return InstalledRelease(version, "unmanaged")
    try:
        meta = _read_metadata(root / ".release.json")
        if meta.get("digest") != root.name or meta.get("gateSchema") != 1:
            raise ValueError("invalid installed release")
        if meta.get("qualification") == "source":
            return InstalledRelease(version, "source", root)
        manifest = _read_metadata(root / "source" / ".netizen-release.json")
        published = meta.get("publishedRelease")
        if (meta.get("qualification") != "published"
                or manifest.get("schema") != 1
                or manifest.get("qualification") != "github-release"
                or manifest.get("version") != version
                or not isinstance(published, dict)
                or published.get("version") != version
                or published.get("commit") != manifest.get("commit")
                or published.get("requirementsDigest") != manifest.get("requirementsDigest")
                or meta.get("sourceDigest") != manifest.get("sourceDigest")
                or _VERSION.fullmatch(version) is None):
            raise ValueError("invalid installed release")
        return InstalledRelease(version, "published", root)
    except (OSError, ValueError, RecursionError):
        return InstalledRelease(version, "unmanaged")


def fetch_latest_release() -> dict[str, Any]:
    request = urllib.request.Request(LATEST_API, headers={
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Netizen-Admin-Updater",
    })
    with urllib.request.urlopen(request, timeout=10) as response:
        if response.geturl() != LATEST_API:
            raise ValueError("unexpected release endpoint")
        payload = response.read(MAX_RELEASE_BYTES + 1)
    if len(payload) > MAX_RELEASE_BYTES:
        raise ValueError("release response exceeds limit")
    return parse_release(json.loads(payload))


def parse_release(value: object) -> dict[str, Any]:
    """Accept only immutable official stable releases with both asset digests."""
    if (not isinstance(value, dict) or value.get("draft") is not False
            or value.get("prerelease") is not False or value.get("immutable") is not True):
        raise ValueError("release is not an immutable stable release")
    tag = value.get("tag_name")
    if not isinstance(tag, str) or not tag.startswith("v") or not _VERSION.fullmatch(tag[1:]):
        raise ValueError("invalid release version")
    version = tag[1:]
    assets = value.get("assets")
    if not isinstance(assets, list) or len(assets) > 100:
        raise ValueError("invalid release assets")
    target: dict[str, Any] = {"version": version, "releaseId": value.get("id")}
    for name, key in (("install.sh", "installerSha256"),
                      (f"netizen-{tag}.tar.gz", "archiveSha256")):
        matches = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == name]
        if len(matches) != 1:
            raise ValueError("release asset is missing or duplicated")
        asset = matches[0]
        digest = asset.get("digest")
        if (asset.get("state") != "uploaded"
                or asset.get("browser_download_url") != f"{RELEASES_URL}/download/{tag}/{name}"
                or not isinstance(digest, str) or not digest.startswith("sha256:")):
            raise ValueError("release asset identity is invalid")
        target[key] = digest.removeprefix("sha256:")
    target = validate_target(target)
    notes = value.get("body") or ""
    if not isinstance(notes, str) or len(notes) > 64_000:
        raise ValueError("invalid release notes")
    return {**target, "notes": notes, "url": f"{RELEASES_URL}/tag/{tag}"}


def _target(release: dict[str, Any]) -> dict[str, Any]:
    return validate_target({key: release[key] for key in
                           ("version", "releaseId", "installerSha256", "archiveSha256")})


def _newer(candidate: str, current: str) -> bool:
    return bool(_VERSION.fullmatch(candidate) and _VERSION.fullmatch(current)
                and tuple(map(int, candidate.split("."))) > tuple(map(int, current.split("."))))


class UpdateService:
    """One bounded management I/O worker; no scheduler or Runtime dependency."""

    def __init__(self, *, home: Path | None = None,
                 current: InstalledRelease | None = None,
                 executor: UpdateExecutor | None = None,
                 fetch: Callable[[], dict[str, Any]] = fetch_latest_release,
                 clock: Callable[[], float] = time.time) -> None:
        self.home = home or Path(pwd.getpwuid(os.geteuid()).pw_dir)
        self.product_root = self.home / ".netizen"
        self._current = current
        self._executor = executor
        self._fetch, self._clock = fetch, clock
        self._latest: dict[str, Any] | None = None
        self._checked_at: float | None = None
        self._checking_error_code: str | None = None
        self._io = BoundedBlockingIOExecutor(max_workers=1, capacity=2,
                                           thread_name_prefix="netizen-update-io")

    async def close(self, *, deadline: float | None = None) -> None:
        await self._io.aclose(deadline=deadline)

    async def status(self) -> dict[str, Any]:
        return await self._io.submit(self._status)

    async def check(self) -> dict[str, Any]:
        return await self._io.submit(self._check)

    async def start(self, *, target: dict[str, Any]) -> dict[str, Any]:
        return await self._io.submit(self._start, target=copy.deepcopy(target))

    def _installation(self) -> InstalledRelease:
        if self._current is None:
            self._current = installed_release(self.home)
        return self._current

    def _manager(self) -> UpdateExecutor:
        if self._executor is None:
            self._executor = UpdateExecutor(self.home)
        return self._executor

    def _supported(self, current: InstalledRelease) -> bool:
        if current.source != "published" or current.root is None:
            return False
        try:
            return (self.product_root / "current").is_symlink() and (
                self.product_root / "current").resolve(strict=True) == current.root
        except OSError:
            return False

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
                    return operation
                if terminal_phase(operation["phase"]):
                    # A terminal write can precede the worker's exit. Only
                    # remove its manager job once it has released this lock.
                    try:
                        self._manager().cleanup(operation["operationId"])
                    except UpdateExecutorError:
                        pass  # Cleanup cannot erase the installer's result.
                    return operation
                if operation["phase"] == "accepted":
                    active = self._manager().is_active(operation["operationId"])
                    if active and self._clock() - operation["createdAt"] < START_HANDOFF_SECONDS:
                        return operation
                # The executing worker owns this exact lock until its terminal write.
                # A never-claimed dispatch gets a bounded handoff period. It must
                # reread accepted under this lock, so a late start cannot activate.
                operation = advance_operation(self.product_root, operation["operationId"],
                                              "recovery_required", "worker_lost")
                try:
                    self._manager().cleanup(operation["operationId"])
                except UpdateExecutorError:
                    pass  # A late worker must reject the non-accepted record.
                return operation
            finally:
                os.close(descriptor)
        except (UpdateProtocolError, OSError, UpdateExecutorError) as error:
            raise UpdateError("update_state_unavailable") from error

    def _status(self) -> dict[str, Any]:
        current = self._installation()
        supported = self._supported(current)
        operation = self._operation()
        available = bool(supported and self._latest and not self._checking_error_code
                         and _newer(self._latest["version"], current.version))
        if operation is not None and (not terminal_phase(operation["phase"])
                                      or operation["phase"] == "recovery_required"):
            available = False
        return {"current": current.as_dict(), "supported": supported,
                "latest": copy.deepcopy(self._latest), "available": available,
                "operation": operation, "checkingErrorCode": self._checking_error_code,
                "checkedAt": self._checked_at}

    def _check(self) -> dict[str, Any]:
        if not self._supported(self._installation()):
            return self._status()
        now = self._clock()
        if self._checked_at is None or now - self._checked_at >= CHECK_CACHE_SECONDS:
            self._checked_at = now
            try:
                self._latest = self._fetch()
                _target(self._latest)
                self._checking_error_code = None
            except (OSError, ValueError, UpdateProtocolError):
                self._latest = None
                self._checking_error_code = "release_check_failed"
        return self._status()

    def _start(self, *, target: dict[str, Any]) -> dict[str, Any]:
        try:
            target = validate_target(target)
        except UpdateProtocolError as error:
            raise UpdateError("invalid_update_target") from error
        current = self._installation()
        if not self._supported(current):
            raise UpdateError("update_unsupported")
        if (self._latest is None or self._checking_error_code
                or target != _target(self._latest)
                or not _newer(target["version"], current.version)):
            raise UpdateError("update_target_changed")
        assert current.root is not None
        try:
            descriptor = acquire_install_lock(self.product_root)
        except BlockingIOError as error:
            raise UpdateError("update_busy") from error
        except (OSError, UpdateProtocolError) as error:
            raise UpdateError("update_lock_unavailable") from error
        try:
            if not self._supported(current):
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
            operation = new_operation(target, current.root.name)
            write_operation(self.product_root, operation)
            try:
                self._manager().launch(operation["operationId"], current.root)
            except UpdateDispatchUnknown:
                # The command may already have run. Retain accepted for the
                # worker's exact claim; never dispatch a replacement implicitly.
                return operation
            except (OSError, RuntimeError):
                return advance_operation(self.product_root, operation["operationId"],
                                         "failed", "dispatch_failed")
            return operation
        except (OSError, UpdateProtocolError) as error:
            raise UpdateError("update_submission_unknown") from error
        finally:
            os.close(descriptor)
