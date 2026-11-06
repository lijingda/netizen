"""Instance-owned input originals with complete publication and exact deletion.

No native execution state is recorded here. A retained file is immediately
available for explicit deletion, regardless of whether an input was accepted.
"""
from __future__ import annotations

import logging
import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import threading
from typing import Sequence
import uuid

from .project_files import (
    FileBrowseError, FilePreview, MAX_DIRECTORY_ENTRIES, _check_chain,
    _directories, _file_version, _identity, _modified_at, _open_directory, _read_bounded,
    read_file,
)


logger = logging.getLogger(__name__)

MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_ATTACHMENT_BATCH_BYTES = 50 * 1024 * 1024
MAX_ATTACHMENT_FILES = 20
_MANIFEST = ".manifest.json"
_ID = re.compile(r"[0-9a-f]{32}\Z")
_MANIFEST_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class AttachmentInput:
    name: str
    data: bytes


@dataclass(frozen=True, slots=True)
class StoredAttachment:
    name: str
    path: Path
    size: int


@dataclass(frozen=True, slots=True)
class PreparedAttachments:
    batch_id: str
    files: tuple[StoredAttachment, ...]
    owner: str


def _safe_name(name: str) -> str:
    name = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(char for char in name if char.isprintable() and not 0xD800 <= ord(char) <= 0xDFFF)
    name = name.strip(" .") or "attachment"
    while len(name.encode("utf-8")) > 180:
        name = name[:-1]
    return name


def _valid_file_name(name: str) -> bool:
    return (bool(name) and not name.startswith(".") and "/" not in name and "\\" not in name
            and all(char.isprintable() and not 0xD800 <= ord(char) <= 0xDFFF for char in name)
            and len(name.encode("utf-8")) <= 255)


def _read_json(fd: int, name: str) -> dict:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
            raise FileBrowseError("unsafe_path", "附件归属记录无效。")
        data = _read_bounded(descriptor, _MANIFEST_BYTES)
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, UnicodeError):
        raise FileBrowseError("unsafe_path", "附件归属记录无效。") from None
    finally:
        os.close(descriptor)


def _write_file(fd: int, name: str, data: bytes) -> os.stat_result:
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=fd)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(descriptor)
        return os.fstat(descriptor)
    except BaseException:
        os.unlink(name, dir_fd=fd)
        raise
    finally:
        os.close(descriptor)


async def _finish_worker(task: asyncio.Task):
    """Repeated cancellation cannot abandon a filesystem worker."""
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise


class AttachmentStore:
    """One instance's originals; filesystem work is serialized, never Turns."""

    def __init__(self, root: Path, *, max_file_bytes: int = MAX_ATTACHMENT_BYTES,
                 max_batch_bytes: int = MAX_ATTACHMENT_BATCH_BYTES) -> None:
        self.root = Path(root)
        if not self.root.is_absolute() or ".." in self.root.parts:
            raise ValueError("attachment root must be an absolute instance-owned path")
        self.retained = self.root / "retained"
        self.max_file_bytes = max_file_bytes
        self.max_batch_bytes = max_batch_bytes
        if any(type(value) is not int or value < 1 for value in (max_file_bytes, max_batch_bytes)):
            raise ValueError("attachment limits must be positive integers")
        self._lock = threading.RLock()
        # A batch belongs to this configured path, not a persisted global token
        # or a previous operation's initialization state.
        self._owner = str(self.root)

    @staticmethod
    def _require_private(info: os.stat_result) -> None:
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise FileBrowseError("unsafe_path", "附件目录必须仅属于当前服务账号。")

    @contextmanager
    def _tree(self, area: str, parts: tuple[str, ...] = (), *, retained_identity: str | None = None):
        # Missing directories are ordinary mkdir operations. Only the area used
        # by this operation is needed; no historical batch or owner file is read.
        with _directories(self.root.parent, (), identity=None, context="attachments-parent") as (parents, _):
            try:
                os.mkdir(self.root.name, mode=0o700, dir_fd=parents[-1].fd)
                os.fsync(parents[-1].fd)
            except FileExistsError:
                pass
        with _directories(self.root, (), identity=None, context=self._owner) as (root, info):
            self._require_private(info)
            try:
                os.mkdir(area, mode=0o700, dir_fd=root[-1].fd)
                os.fsync(root[-1].fd)
            except FileExistsError:
                pass
            # Pin the root for this operation only. Admin requests additionally
            # carry their own selected retained-directory identity.
            with _directories(self.root, (area, *parts), identity=_identity(info, self._owner),
                              context=self._owner) as value:
                chain, _ = value
                area_info = os.fstat(chain[-len(parts) - 1].fd)
                self._require_private(area_info)
                if retained_identity is not None and _identity(area_info, self._owner) != retained_identity:
                    raise FileBrowseError("root_changed", "附件目录已变化，请重新打开。")
                yield value

    @staticmethod
    def _assert_batch_location(parent: int, batch: str, fd: int) -> None:
        current = os.stat(batch, dir_fd=parent, follow_symlinks=False)
        opened = os.fstat(fd)
        if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise FileBrowseError("root_changed", "附件批次已变化，请刷新后核对。")

    def _manifest(self, fd: int, batch: str) -> dict:
        if not _ID.fullmatch(batch):
            raise FileBrowseError("not_found", "附件批次不存在。")
        manifest = _read_json(fd, _MANIFEST)
        files = manifest.get("files")
        directory_info = os.fstat(fd)
        if (manifest.get("format") != 1 or manifest.get("owner") != self._owner
                or (manifest.get("device"), manifest.get("inode")) != (directory_info.st_dev, directory_info.st_ino)
                or manifest.get("batch") != batch or not isinstance(manifest.get("savedAt"), str)
                or not isinstance(files, list) or not 1 <= len(files) <= MAX_ATTACHMENT_FILES):
            raise FileBrowseError("unsafe_path", "附件批次归属记录无效。")
        names = set()
        for item in files:
            if (not isinstance(item, dict) or not isinstance(item.get("name"), str)
                    or not isinstance(item.get("originalName"), str)
                    or not _valid_file_name(item["name"])
                    or item["name"] in names
                    or any(type(item.get(key)) is not int or item[key] < 0 for key in ("size", "device", "inode"))):
                raise FileBrowseError("unsafe_path", "附件文件归属记录无效。")
            names.add(item["name"])
        return manifest

    @staticmethod
    def _owned_info(fd: int, item: dict) -> os.stat_result:
        info = os.stat(item["name"], dir_fd=fd, follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                or (info.st_dev, info.st_ino) != (item["device"], item["inode"])):
            raise FileBrowseError("file_changed", "附件已被替换，请刷新后核对。")
        return info

    def prepare(self, files: Sequence[AttachmentInput]) -> PreparedAttachments:
        files = tuple(files)
        if not 1 <= len(files) <= MAX_ATTACHMENT_FILES:
            raise FileBrowseError("attachment_limit", "单条消息最多支持 20 个文件附件。")
        if any(not isinstance(item.data, bytes) or len(item.data) > self.max_file_bytes for item in files):
            raise FileBrowseError("attachment_limit", "文件附件超过单文件大小限制。")
        total = sum(len(item.data) for item in files)
        if total > self.max_batch_bytes:
            raise FileBrowseError("attachment_limit", "文件附件超过单条消息大小限制。")
        with self._lock:
            batch = uuid.uuid4().hex
            saved = datetime.now(timezone.utc).isoformat()
            names: list[str] = []
            with self._tree(".staging") as (chain, _):
                parent = chain[-1].fd
                os.mkdir(batch, mode=0o700, dir_fd=parent)
                fd = _open_directory(batch, parent_fd=parent)
                try:
                    records = []
                    result = []
                    for index, item in enumerate(files, 1):
                        name = f"{index:02}-{_safe_name(item.name)}"
                        info = _write_file(fd, name, item.data)
                        names.append(name)
                        records.append({"name": name, "originalName": _safe_name(item.name), "size": len(item.data),
                                        "device": info.st_dev, "inode": info.st_ino})
                        result.append(StoredAttachment(_safe_name(item.name), self.retained / batch / name, len(item.data)))
                    directory_info = os.fstat(fd)
                    manifest = {"format": 1, "owner": self._owner, "batch": batch, "savedAt": saved, "files": records,
                                "device": directory_info.st_dev, "inode": directory_info.st_ino}
                    _write_file(fd, _MANIFEST, json.dumps(manifest, ensure_ascii=True).encode())
                    names.append(_MANIFEST)
                    _check_chain(chain)
                    os.fsync(fd)
                    os.fsync(parent)
                    return PreparedAttachments(batch, tuple(result), self._owner)
                except BaseException:
                    # Only the files just created through this pinned descriptor.
                    for name in names:
                        try:
                            os.unlink(name, dir_fd=fd)
                        except OSError:
                            pass
                    try:
                        os.rmdir(batch, dir_fd=parent)
                    except OSError:
                        pass
                    raise
                finally:
                    os.close(fd)

    async def prepare_files(self, files: Sequence[AttachmentInput]) -> PreparedAttachments:
        task = asyncio.create_task(asyncio.to_thread(self.prepare, files))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # The worker still owns its write. Do not abandon a late completion.
            try:
                batch = await _finish_worker(task)
            except Exception as diagnostic_error:
                logger.warning(
                    "attachment preparation worker failed during cancellation drain",
                    extra={"error_type": type(diagnostic_error).__name__},
                )
            else:
                await _finish_worker(asyncio.create_task(asyncio.to_thread(self.discard, batch)))
            raise

    def publish(self, prepared: PreparedAttachments) -> None:
        with self._lock:
            self._require_prepared(prepared)
            with self._tree(".staging") as (source, _), self._tree("retained") as (target, _):
                fd = _open_directory(prepared.batch_id, parent_fd=source[-1].fd)
                try:
                    manifest = self._manifest(fd, prepared.batch_id)
                    for item in manifest["files"]:
                        self._owned_info(fd, item)
                    _check_chain(source)
                    _check_chain(target)
                    self._assert_batch_location(source[-1].fd, prepared.batch_id, fd)
                    try:
                        os.stat(prepared.batch_id, dir_fd=target[-1].fd, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise FileBrowseError("file_changed", "附件发布位置已被占用，请重新发送。")
                    os.rename(prepared.batch_id, prepared.batch_id,
                              src_dir_fd=source[-1].fd, dst_dir_fd=target[-1].fd)
                    os.fsync(source[-1].fd)
                    os.fsync(target[-1].fd)
                finally:
                    os.close(fd)

    async def publish_files(self, prepared: PreparedAttachments) -> None:
        task = asyncio.create_task(asyncio.to_thread(self.publish, prepared))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await _finish_worker(task)
            except Exception as diagnostic_error:
                logger.warning(
                    "attachment publication worker failed during cancellation drain",
                    extra={"batch_id": prepared.batch_id, "error_type": type(diagnostic_error).__name__},
                )
            await _finish_worker(asyncio.create_task(asyncio.to_thread(self.discard, prepared)))
            raise

    async def discard_files(self, prepared: PreparedAttachments) -> None:
        task = asyncio.create_task(asyncio.to_thread(self.discard, prepared))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await _finish_worker(task)
            raise

    def _require_prepared(self, prepared: PreparedAttachments) -> None:
        if prepared.owner != self._owner or not _ID.fullmatch(prepared.batch_id):
            raise FileBrowseError("unsafe_path", "附件不属于此实例。")

    def discard(self, prepared: PreparedAttachments) -> None:
        """Only call when the caller knows these paths were never delivered."""
        with self._lock:
            self._require_prepared(prepared)
            for area in (".staging", "retained"):
                try:
                    self._discard_batch(area, prepared.batch_id)
                except FileBrowseError as error:
                    if error.code != "not_found":
                        raise

    def _discard_batch(self, area: str, batch: str) -> None:
        with self._tree(area) as (chain, _):
            parent = chain[-1].fd
            try:
                fd = _open_directory(batch, parent_fd=parent)
            except FileNotFoundError:
                return
            try:
                manifest = self._manifest(fd, batch)
                for item in manifest["files"]:
                    try:
                        self._owned_info(fd, item)
                    except FileNotFoundError:
                        continue
                    _check_chain(chain)
                    self._assert_batch_location(parent, batch, fd)
                    os.unlink(item["name"], dir_fd=fd)
                self._remove_empty_batch(chain, fd, batch)
            finally:
                os.close(fd)

    def _remove_empty_batch(self, chain, fd: int, batch: str) -> bool:
        """Best effort; true means the batch is no longer browsable, even if
        removing its now-empty directory fails. Never change a file's result.
        """
        parent = chain[-1].fd
        try:
            with os.scandir(fd) as entries:
                names = [entry.name for _, entry in zip(range(2), entries)]
            if names != [_MANIFEST]:
                return False
            _check_chain(chain)
            self._assert_batch_location(parent, batch, fd)
            os.unlink(_MANIFEST, dir_fd=fd)
        except (OSError, FileBrowseError):
            return False
        try:
            self._assert_batch_location(parent, batch, fd)
            os.rmdir(batch, dir_fd=parent)
            os.fsync(parent)
        except (OSError, FileBrowseError):
            pass
        return True

    def root_info(self) -> dict:
        with self._lock:
            with self._tree("retained") as (chain, _):
                identity = _identity(os.fstat(chain[-1].fd), self._owner)
            return {"rootId": "attachments", "cwd": str(self.retained), "identity": identity}

    def _require_identity(self, identity: str) -> None:
        if self.root_info()["identity"] != identity:
            raise FileBrowseError("root_changed", "附件目录已变化，请重新打开。")

    def directory(self, *, identity: str, path: str) -> dict:
        with self._lock:
            self._require_identity(identity)
            if path and not _ID.fullmatch(path):
                raise FileBrowseError("invalid_path", "附件目录路径无效。")
            result = []
            truncated = False
            if path:
                with self._tree("retained", (path,), retained_identity=identity) as (chain, _):
                    manifest = self._manifest(chain[-1].fd, path)
                    for item in manifest["files"]:
                        try:
                            info = self._owned_info(chain[-1].fd, item)
                        except FileNotFoundError:
                            continue
                        result.append(self._entry(path, item, info, manifest["savedAt"]))
            else:
                with self._tree("retained", retained_identity=identity) as (chain, _):
                    with os.scandir(chain[-1].fd) as entries:
                        for index, entry in enumerate(entries):
                            if index >= MAX_DIRECTORY_ENTRIES:
                                truncated = True
                                break
                            if not _ID.fullmatch(entry.name) or not entry.is_dir(follow_symlinks=False):
                                continue
                            try:
                                with self._tree("retained", (entry.name,), retained_identity=identity) as (batch_chain, _):
                                    manifest = self._manifest(batch_chain[-1].fd, entry.name)
                                    info = os.fstat(batch_chain[-1].fd)
                            except FileBrowseError:
                                continue
                            result.append({"name": entry.name, "path": entry.name, "kind": "directory",
                                           "displayName": f"{manifest['savedAt']} · {entry.name[:8]}",
                                           "savedAt": manifest["savedAt"], "modifiedAt": _modified_at(info)})
                result.sort(key=lambda entry: (entry["savedAt"], entry["name"]), reverse=True)
            return {"path": path, "entries": result, "truncated": truncated}

    @staticmethod
    def _entry(batch: str, item: dict, info: os.stat_result, saved: str) -> dict:
        return {"name": item["name"], "displayName": item["originalName"], "path": f"{batch}/{item['name']}",
                "kind": "file", "size": info.st_size, "modifiedAt": _modified_at(info), "savedAt": saved,
                "version": _file_version(info), "deletable": True}

    def _target(self, path: str) -> tuple[str, str]:
        parts = path.split("/")
        if (len(parts) != 2 or not _ID.fullmatch(parts[0]) or not _valid_file_name(parts[1])):
            raise FileBrowseError("invalid_path", "附件文件路径无效。")
        return parts[0], parts[1]

    def metadata(self, *, identity: str, path: str, version: str | None = None) -> dict:
        with self._lock:
            self._require_identity(identity)
            batch, name = self._target(path)
            with self._tree("retained", (batch,), retained_identity=identity) as (chain, _):
                manifest = self._manifest(chain[-1].fd, batch)
                item = next((item for item in manifest["files"] if item["name"] == name), None)
                if item is None:
                    raise FileBrowseError("not_found", "该文件不是受管附件。")
                info = self._owned_info(chain[-1].fd, item)
                if version is not None and _file_version(info) != version:
                    raise FileBrowseError("file_changed", "附件已变化，请刷新后核对。")
                return self._entry(batch, item, info, manifest["savedAt"])

    def content(self, *, identity: str, path: str) -> FilePreview:
        with self._lock:
            entry = self.metadata(identity=identity, path=path)
            preview = read_file(self.retained, path, identity=identity, context=self._owner)
            if preview.version != entry["version"]:
                raise FileBrowseError("file_changed", "附件已变化，请刷新后核对。")
            self.metadata(identity=identity, path=path, version=entry["version"])
            return preview

    def delete(self, *, identity: str, path: str, version: str) -> dict:
        with self._lock:
            self.metadata(identity=identity, path=path, version=version)
            batch, name = self._target(path)
            changed = False
            try:
                with self._tree("retained", retained_identity=identity) as (chain, _):
                    parent = chain[-1].fd
                    fd = _open_directory(batch, parent_fd=parent)
                    try:
                        manifest = self._manifest(fd, batch)
                        item = next(item for item in manifest["files"] if item["name"] == name)
                        if _file_version(self._owned_info(fd, item)) != version:
                            raise FileBrowseError("file_changed", "附件已变化，请刷新后核对。")
                        _check_chain(chain)
                        self._assert_batch_location(parent, batch, fd)
                        os.unlink(name, dir_fd=fd)
                        changed = True
                        os.fsync(fd)
                        removed = self._remove_empty_batch(chain, fd, batch)
                        return {"deleted": True, "path": path, "removedBatch": removed}
                    finally:
                        os.close(fd)
            except (OSError, FileBrowseError):
                if changed:
                    raise FileBrowseError("outcome_unknown", "附件删除结果未确认，请刷新目录核对；不会自动重试。") from None
                raise
