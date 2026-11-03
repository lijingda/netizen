"""Fixed-instance attachment management using the shared bounded file executor."""
from __future__ import annotations

from typing import Callable, TypeVar

from .blocking_io import BoundedBlockingIOExecutor
from ..attachments import AttachmentStore
from ..project_files import FileBrowseError, FilePreview

T = TypeVar("T")


class AttachmentFileService:
    def __init__(self, store: AttachmentStore | None, io: BoundedBlockingIOExecutor):
        self._store = store
        self._io = io

    def _require_store(self) -> AttachmentStore:
        if self._store is None:
            raise FileBrowseError("read_unavailable", "文件附件管理暂不可用。")
        return self._store

    async def root(self, *, deadline: float) -> dict:
        return await self._io.submit(self._require_store().root_info, deadline=deadline)

    async def directory(self, *, identity: str, path: str, deadline: float) -> dict:
        return await self._io.submit(self._require_store().directory, identity=identity, path=path, deadline=deadline)

    async def content(self, *, identity: str, path: str, deadline: float,
                      transform: Callable[[FilePreview], T] | None = None) -> FilePreview | T:
        store = self._require_store()

        def read():
            preview = store.content(identity=identity, path=path)
            return preview if transform is None else transform(preview)

        return await self._io.submit(read, deadline=deadline)

    async def metadata(self, *, identity: str, path: str, version: str, deadline: float) -> dict:
        return await self._io.submit(self._require_store().metadata, identity=identity,
                                     path=path, version=version, deadline=deadline)

    async def delete(self, *, identity: str, path: str, version: str, deadline: float) -> dict:
        # A submitted mutation may finish after an HTTP cancellation or timeout;
        # the shared executor keeps the operation tracked until it really ends.
        return await self._io.submit(self._require_store().delete, identity=identity,
                                     path=path, version=version, deadline=deadline)
