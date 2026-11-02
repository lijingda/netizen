"""Bounded, read-only access to one explicitly selected project directory.

Paths are opened relative to pinned directory descriptors. No component,
including an ancestor of the selected root, may be a symbolic link.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Iterator, Literal


MAX_DIRECTORY_ENTRIES = 2000
MAX_TEXT_BYTES = 1024 * 1024
MAX_TEXT_LINES = 10000
MAX_LINE_CHARACTERS = 20000
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
MAX_IMAGE_DIMENSION = 8192


class FileBrowseError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class FilePreview:
    path: str
    kind: Literal["text", "image"]
    text: str | None
    data: bytes | None
    mime: str
    size: int
    modified_at: str
    version: str


@dataclass(frozen=True, slots=True)
class _Directory:
    fd: int
    name: str | None
    device: int
    inode: int


def _path_parts(path: str, *, allow_empty: bool) -> tuple[str, ...]:
    if not isinstance(path, str) or len(path) > 4096:
        raise FileBrowseError("invalid_path", "文件路径无效。")
    if path == "" and allow_empty:
        return ()
    parts = tuple(path.split("/"))
    if (
        not path
        or "\\" in path
        or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in path)
        or any(0xD800 <= ord(char) <= 0xDFFF for char in path)
        or len(parts) > 128
        or any(part in ("", ".", "..") or part.casefold() == ".git" for part in parts)
    ):
        raise FileBrowseError("invalid_path", "文件路径无效，不能访问项目外部或 .git。")
    return parts


def _identity(info: os.stat_result, context: str) -> str:
    value = json.dumps([context, info.st_dev, info.st_ino], ensure_ascii=True)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _file_version(info: os.stat_result) -> str:
    value = f"{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}"
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _modified_at(info: os.stat_result) -> str:
    try:
        return datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _open_directory(name: str, *, parent_fd: int | None = None) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except OSError as error:
        # On some systems O_DIRECTORY | O_NOFOLLOW reports ENOTDIR for links.
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            try:
                info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except OSError:
                raise error
            if stat.S_ISLNK(info.st_mode):
                raise FileBrowseError("unsafe_path", "暂不支持访问符号链接。") from None
        raise


def _check_chain(chain: list[_Directory]) -> None:
    for parent, child in zip(chain, chain[1:]):
        try:
            info = os.stat(child.name, dir_fd=parent.fd, follow_symlinks=False)
        except OSError:
            raise FileBrowseError("root_changed", "目录位置已变化，请刷新后重试。") from None
        if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != (
            child.device, child.inode,
        ):
            raise FileBrowseError("root_changed", "目录位置已变化，请刷新后重试。")


@contextmanager
def _directories(
    root: Path, parts: tuple[str, ...], *, identity: str | None, context: str,
) -> Iterator[tuple[list[_Directory], os.stat_result]]:
    root = Path(root)
    if not root.is_absolute() or ".." in root.parts or len(root.parts) + len(parts) > 128:
        raise FileBrowseError("invalid_path", "项目目录无效。")
    chain: list[_Directory] = []
    try:
        for component in (root.anchor, *root.parts[1:]):
            fd = _open_directory(component, parent_fd=chain[-1].fd if chain else None)
            info = os.fstat(fd)
            chain.append(_Directory(fd, component if chain else None, info.st_dev, info.st_ino))
        root_info = os.fstat(chain[-1].fd)
        if identity is not None and _identity(root_info, context) != identity:
            raise FileBrowseError("root_changed", "项目目录已变化，请重新选择工作区。")
        for component in parts:
            fd = _open_directory(component, parent_fd=chain[-1].fd)
            info = os.fstat(fd)
            chain.append(_Directory(fd, component, info.st_dev, info.st_ino))
        _check_chain(chain)
        yield chain, root_info
        _check_chain(chain)
    except OSError as error:
        if error.errno in (errno.ELOOP,):
            raise FileBrowseError("unsafe_path", "暂不支持访问符号链接。") from None
        if error.errno in (errno.ENOENT, errno.ENOTDIR):
            raise FileBrowseError("not_found", "文件或目录不存在。") from None
        if error.errno in (errno.EACCES, errno.EPERM):
            raise FileBrowseError("permission_denied", "没有读取此文件或目录的权限。") from None
        raise FileBrowseError("read_failed", "无法读取文件或目录，请刷新后重试。") from None
    finally:
        for item in reversed(chain):
            os.close(item.fd)


def root_identity(root: Path, *, context: str) -> str:
    with _directories(root, (), identity=None, context=context) as (_, info):
        return _identity(info, context)


def list_directory(root: Path, path: str, *, identity: str, context: str) -> dict:
    parts = _path_parts(path, allow_empty=True)
    with _directories(root, parts, identity=identity, context=context) as (chain, _):
        fd = chain[-1].fd
        before = os.fstat(fd)
        entries = []
        truncated = False
        with os.scandir(fd) as iterator:
            for index, entry in enumerate(iterator):
                if index >= MAX_DIRECTORY_ENTRIES:
                    truncated = True
                    break
                try:
                    _path_parts(entry.name, allow_empty=False)
                except FileBrowseError:
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    raise FileBrowseError("file_changed", "目录内容已变化，请刷新后重试。") from None
                if stat.S_ISLNK(info.st_mode):
                    kind = "symlink"
                elif stat.S_ISDIR(info.st_mode):
                    kind = "directory"
                elif stat.S_ISREG(info.st_mode):
                    kind = "file"
                else:
                    kind = "unsupported"
                entries.append({
                    "name": entry.name,
                    "path": "/".join((*parts, entry.name)),
                    "kind": kind,
                })
        after = os.fstat(fd)
        if (before.st_mtime_ns, before.st_ctime_ns) != (after.st_mtime_ns, after.st_ctime_ns):
            raise FileBrowseError("file_changed", "目录内容已变化，请刷新后重试。")
        entries.sort(key=lambda entry: (entry["kind"] != "directory", entry["name"].casefold(), entry["name"]))
        return {"path": path, "entries": entries, "truncated": truncated}


def _read_bounded(fd: int, limit: int) -> bytes:
    chunks = []
    remaining = limit + 1
    while remaining:
        chunk = os.read(fd, min(65536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) > limit:
        raise FileBrowseError("preview_too_large", "文件过大，暂不支持预览。")
    return data


def _image_mime(data: bytes, path: str) -> str | None:
    signatures = (
        (data.startswith(b"\x89PNG\r\n\x1a\n"), "image/png"),
        (data.startswith(b"\xff\xd8"), "image/jpeg"),
        (data.startswith((b"GIF87a", b"GIF89a")), "image/gif"),
        (data[:4] == b"RIFF" and data[8:12] == b"WEBP", "image/webp"),
    )
    for matches, mime in signatures:
        if matches:
            return mime
    extension = Path(path).suffix.lower()
    if extension in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        raise FileBrowseError("invalid_image", "图片格式无效，暂不支持预览。")
    return None


def _image_dimensions(data: bytes, mime: str) -> tuple[int, int]:
    if mime == "image/png" and len(data) >= 33 and data[8:16] == b"\x00\x00\x00\rIHDR":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if mime == "image/gif" and len(data) >= 13:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if mime == "image/jpeg":
        offset = 2
        while offset < len(data):
            if data[offset] != 0xFF:
                break
            while offset < len(data) and data[offset] == 0xFF:
                offset += 1
            if offset >= len(data):
                break
            marker = data[offset]
            offset += 1
            if marker in (0xD9, 0xDA, 0):
                break
            if marker in (0x01, *range(0xD0, 0xD9)):
                continue
            length = int.from_bytes(data[offset:offset + 2], "big")
            if length < 2 or offset + length > len(data):
                break
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                if length < 8:
                    break
                return int.from_bytes(data[offset + 5:offset + 7], "big"), int.from_bytes(data[offset + 3:offset + 5], "big")
            offset += length
    if mime == "image/webp" and len(data) >= 20 and int.from_bytes(data[4:8], "little") + 8 == len(data):
        length = int.from_bytes(data[16:20], "little")
        payload = data[20:20 + length]
        if len(payload) == length:
            kind = data[12:16]
            if kind == b"VP8X" and length == 10:
                return int.from_bytes(payload[4:7], "little") + 1, int.from_bytes(payload[7:10], "little") + 1
            if kind == b"VP8L" and length >= 5 and payload[0] == 0x2F:
                bits = int.from_bytes(payload[1:5], "little")
                if bits >> 29 == 0:
                    return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            if kind == b"VP8 " and length >= 10 and payload[3:6] == b"\x9d\x01\x2a":
                return int.from_bytes(payload[6:8], "little") & 0x3FFF, int.from_bytes(payload[8:10], "little") & 0x3FFF
    raise FileBrowseError("invalid_image", "无法读取图片尺寸，暂不支持预览。")


def _preview(data: bytes, path: str, info: os.stat_result) -> FilePreview:
    mime = _image_mime(data, path)
    if mime:
        width, height = _image_dimensions(data, mime)
        if width <= 0 or height <= 0:
            raise FileBrowseError("invalid_image", "图片尺寸无效，暂不支持预览。")
        if max(width, height) > MAX_IMAGE_DIMENSION or width * height > MAX_IMAGE_PIXELS:
            raise FileBrowseError("preview_too_large", "图片尺寸过大，暂不支持预览。")
        return FilePreview(path, "image", None, data, mime, len(data), _modified_at(info), _file_version(info))
    if len(data) > MAX_TEXT_BYTES:
        raise FileBrowseError("preview_too_large", "文本超过 1 MiB，暂不支持预览。")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise FileBrowseError("invalid_encoding", "暂不支持此文件编码，仅支持 UTF-8 文本。") from None
    if any((ord(char) < 32 and char not in "\t\r\n") or 127 <= ord(char) <= 159 for char in text):
        raise FileBrowseError("unsupported_file", "此文件不是可预览的文本或图片。")
    lines = re.split(r"\r\n|[\r\n\u2028\u2029]", text, maxsplit=MAX_TEXT_LINES)
    if len(lines) > MAX_TEXT_LINES:
        raise FileBrowseError("preview_too_large", "文本超过 10000 行，暂不支持预览。")
    if any(len(line) > MAX_LINE_CHARACTERS for line in lines):
        raise FileBrowseError("preview_too_large", "文本单行超过 20000 字符，暂不支持预览。")
    return FilePreview(path, "text", text, None, "text/plain; charset=utf-8", len(data), _modified_at(info), _file_version(info))


def read_file(root: Path, path: str, *, identity: str, context: str) -> FilePreview:
    parts = _path_parts(path, allow_empty=False)
    with _directories(root, parts[:-1], identity=identity, context=context) as (chain, _):
        parent_fd = chain[-1].fd
        entry = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(entry.st_mode):
            raise FileBrowseError("unsafe_path", "暂不支持访问符号链接。")
        if not stat.S_ISREG(entry.st_mode):
            raise FileBrowseError("unsupported_file", "仅支持查看普通文件。")
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent_fd)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise FileBrowseError("unsupported_file", "仅支持查看普通文件。")
            _check_chain(chain)
            if before.st_size > MAX_IMAGE_BYTES:
                raise FileBrowseError("preview_too_large", "文件过大，暂不支持预览。")
            prefix = os.read(fd, 64)
            limit = MAX_IMAGE_BYTES if _image_mime(prefix, path) else MAX_TEXT_BYTES
            if before.st_size > limit:
                raise FileBrowseError("preview_too_large", "文件过大，暂不支持预览。")
            data = prefix + _read_bounded(fd, limit - len(prefix))
            after = os.fstat(fd)
            try:
                current = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
            except OSError:
                raise FileBrowseError("file_changed", "文件已变化，请刷新后重试。") from None
            if _file_version(before) != _file_version(after) or _file_version(after) != _file_version(current):
                raise FileBrowseError("file_changed", "文件已变化，请刷新后重试。")
            _check_chain(chain)
            return _preview(data, path, after)
        finally:
            os.close(fd)
