"""Read directory roots selected by answer links through shared file readers."""
from __future__ import annotations

import errno
import json
from pathlib import Path
from typing import Callable, TypeVar

from .blocking_io import BoundedBlockingIOExecutor
from ..project_files import (
    FileBrowseError, FilePreview, absolute_root_parts,
    list_directory, read_file, root_identity,
)

T = TypeVar("T")


class PathFileService:
    """Directory roots for answer links; no index, cache, or persistence."""

    def __init__(self, io: BoundedBlockingIOExecutor) -> None:
        self._io = io

    @staticmethod
    def _context(root: str) -> str:
        return json.dumps(["path", root])

    async def root(self, *, root: str, deadline: float) -> dict:
        def resolve() -> dict:
            absolute_root_parts(root)
            try:
                cwd = Path(root).resolve(strict=True)
            except OSError as error:
                if error.errno in {errno.ENOENT, errno.ENOTDIR}:
                    raise FileBrowseError("not_found", "文件或目录不存在。") from None
                if error.errno in {errno.EACCES, errno.EPERM}:
                    raise FileBrowseError("permission_denied", "没有读取此文件或目录的权限。") from None
                if error.errno == errno.ELOOP:
                    raise FileBrowseError("unsafe_path", "暂不支持访问循环符号链接。") from None
                raise FileBrowseError("read_failed", "无法读取文件或目录，请刷新后重试。") from None
            except RuntimeError:
                # Python 3.11/3.12 report symlink loops as RuntimeError.
                raise FileBrowseError("unsafe_path", "暂不支持访问循环符号链接。") from None
            canonical = str(cwd)
            absolute_root_parts(canonical)
            identity = root_identity(cwd, context=self._context(canonical))
            return {"rootId": "path", "cwd": canonical, "identity": identity}

        return await self._io.submit(resolve, deadline=deadline)

    async def directory(self, *, root: str, identity: str, path: str, deadline: float) -> dict:
        return await self._read(list_directory, root=root, identity=identity, path=path,
                                deadline=deadline)

    async def content(self, *, root: str, identity: str, path: str, deadline: float,
                      transform: Callable[[FilePreview], T] | None = None) -> FilePreview | T:
        def read_and_transform(cwd: Path, path: str, *, identity: str, context: str) -> FilePreview | T:
            preview = read_file(cwd, path, identity=identity, context=context)
            return preview if transform is None else transform(preview)

        return await self._read(read_and_transform, root=root, identity=identity, path=path,
                                deadline=deadline)

    async def _read(self, operation: Callable[..., T], *, root: str, identity: str,
                    path: str, deadline: float) -> T:
        absolute_root_parts(root)
        # Subsequent reads never resolve links. The descriptor reader verifies
        # every component and the selected root's device/inode identity.
        try:
            return await self._io.submit(operation, Path(root), path, identity=identity,
                                          context=self._context(root), deadline=deadline)
        except FileBrowseError as error:
            if error.code == "root_changed":
                raise FileBrowseError("root_changed", "目录已变化，请从对话记录重新打开链接。") from None
            raise
