"""Read current Project files through the existing management boundary."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, TypeVar

from .blocking_io import BoundedBlockingIOExecutor
from ..bindings import ProjectConflict, ProjectNotFound
from ..project_files import FileBrowseError, FilePreview, list_directory, read_file, root_identity
from ..worktrees.models import WorktreeConflict, WorktreeNotFound, WorktreeRecord

if TYPE_CHECKING:
    from .service import InstanceManagementService

T = TypeVar("T")


@dataclass(frozen=True)
class _Root:
    alias: str
    cwd: Path
    context: str
    worktree: WorktreeRecord | None


class ProjectFileService:
    """No index or content cache; cancelled reads keep their executor slot."""

    def __init__(self, management: InstanceManagementService, *,
                 io: BoundedBlockingIOExecutor | None = None) -> None:
        self._management = management
        self._store = management._bindings
        self._io = io or BoundedBlockingIOExecutor(
            max_workers=2, capacity=4, thread_name_prefix="netizen-file-preview",
        )

    async def close(self, *, deadline: float | None = None) -> None:
        await self._io.aclose(deadline=deadline)

    def _project(self, alias: str):
        try:
            project = self._store.get_project(alias)
            self._store.require_project_not_deleting(alias)
            return project
        except (ProjectNotFound, ProjectConflict) as error:
            raise FileBrowseError("root_changed", "Project 已删除或正在删除，请关闭后重新打开文件查看。") from error

    @staticmethod
    def _context(project, worktree: WorktreeRecord | None) -> str:
        return json.dumps([
            project.alias, project.generation, project.cwd,
            None if worktree is None else [worktree.id, worktree.project_generation,
                worktree.cwd, worktree.revision, worktree.identity_token],
        ], ensure_ascii=True, separators=(",", ":"))

    async def _resolve(self, alias: str, worktree_id: str | None) -> _Root:
        project = self._project(alias)
        record = None
        if worktree_id is not None:
            service = self._management.worktrees
            if service is None:
                raise FileBrowseError("root_changed", "受管 worktree 暂不可用，请重新选择目录。")
            record = await service.resolve_for_browse(project_alias=alias, worktree_id=worktree_id)
        root = _Root(alias, Path(record.cwd if record else project.cwd), self._context(project, record), record)
        self._assert_current(root)
        return root

    def _assert_current(self, root: _Root) -> None:
        project = self._project(root.alias)
        record = None
        if root.worktree is not None:
            try:
                record = self._store.worktrees.get(root.worktree.id)
                self._store.worktrees.require_not_deleting(record.id)
            except (WorktreeNotFound, WorktreeConflict) as error:
                raise FileBrowseError("root_changed", "worktree 已删除或正在删除，请重新选择目录。") from error
            if (record != root.worktree or record.project_alias != project.alias
                    or record.project_generation != project.generation or record.creation_state != "ready"):
                raise FileBrowseError("root_changed", "worktree 登记已变化，请重新选择目录。")
        if self._context(project, record) != root.context:
            raise FileBrowseError("root_changed", "Project 登记已变化，请关闭后重新打开文件查看。")

    async def root(self, *, project: str, worktree: str | None, deadline: float) -> dict:
        async with asyncio.timeout_at(deadline):
            root = await self._resolve(project, worktree)
            identity = await self._io.submit(root_identity, root.cwd, context=root.context, deadline=deadline)
            self._assert_current(root)
            return {"projectAlias": project, "rootId": worktree or "project", "cwd": str(root.cwd), "identity": identity}

    async def directory(self, *, project: str, worktree: str | None, identity: str,
                        path: str, deadline: float) -> dict:
        return await self._read(list_directory, project=project, worktree=worktree,
                                identity=identity, path=path, deadline=deadline)

    async def content(self, *, project: str, worktree: str | None, identity: str,
                      path: str, deadline: float,
                      transform: Callable[[FilePreview], T] | None = None) -> FilePreview | T:
        def read_and_transform(*args, **kwargs):
            preview = read_file(*args, **kwargs)
            return preview if transform is None else transform(preview)

        # Projection can involve CPU work (for example Markdown parsing). Keep
        # it in the same bounded task and recheck the original root registration
        # after all work finishes, before returning any content to the caller.
        return await self._read(read_and_transform, project=project, worktree=worktree,
                                identity=identity, path=path, deadline=deadline)

    async def _read(self, operation: Callable[..., T], *, project: str, worktree: str | None,
                    identity: str, path: str, deadline: float) -> T:
        async with asyncio.timeout_at(deadline):
            root = await self._resolve(project, worktree)
            result = await self._io.submit(operation, root.cwd, path, identity=identity,
                                           context=root.context, deadline=deadline)
            self._assert_current(root)
            return result
