"""Persistent Channel aliases for native Codex working directories."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .bindings import (
    BindingStore,
    ProjectConflict as StoredProjectConflict,
    ProjectNotFound as StoredProjectNotFound,
    ProjectRecord,
    ProjectRevisionConflict as StoredProjectRevisionConflict,
)


_ALIAS = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class ProjectError(ValueError):
    pass


class UnknownProject(ProjectError):
    pass


class ProjectDisabled(ProjectError):
    pass


class ProjectAlreadyExists(ProjectError):
    pass


class StaleProject(ProjectError):
    pass


@dataclass(frozen=True, slots=True)
class Project:
    alias: str
    cwd: Path
    enabled: bool
    revision: int


class ProjectRegistry:
    def __init__(
        self,
        *,
        store: BindingStore,
        default_cwd: Path,
        projects: dict[str, Path],
        project_root: Path | None = None,
    ) -> None:
        self._store = store
        root = project_root if project_root is not None else default_cwd.parent
        self._project_root = _canonical_directory(root, "projectRoot")
        self._bootstrap(alias="none", configured_path=default_cwd, reserved=True)
        for alias, configured_path in projects.items():
            self._bootstrap(
                alias=alias,
                configured_path=configured_path,
                reserved=False,
            )

    @property
    def project_root(self) -> Path:
        return self._project_root

    def list(self, *, enabled_only: bool = False) -> tuple[Project, ...]:
        projects = tuple(_project(record) for record in self._store.list_projects())
        if not enabled_only:
            return projects
        return tuple(project for project in projects if project.enabled)

    def resolve_for_new(
        self,
        alias: str,
        *,
        expected_revision: int | None = None,
    ) -> Project:
        project = self._resolve(alias)
        if expected_revision is not None and project.revision != expected_revision:
            raise StaleProject(
                f"Project {alias} 已被其他操作修改，请刷新卡片后重试。"
            )
        if not project.enabled:
            raise ProjectDisabled(f"Project {alias} 已停用，不能创建新会话。")
        return _usable_project(project)

    def resolve_for_binding(self, alias: str) -> Project:
        return _usable_project(self._resolve(alias))

    def resolve(self, alias: str) -> Project:
        """Compatibility alias for existing bindings; disabled rows still resolve."""
        return self.resolve_for_binding(alias)

    def aliases(self) -> tuple[str, ...]:
        return tuple(project.alias for project in self.list())

    def register(
        self,
        *,
        alias: str,
        path: str | None,
        create_directory: bool,
    ) -> Project:
        alias = alias.strip()
        _validate_alias(alias, reserved=False)
        try:
            self._store.get_project(alias)
        except StoredProjectNotFound:
            pass
        else:
            raise ProjectAlreadyExists(f"Project {alias} 已存在。")

        created_directory: Path | None = None
        if create_directory:
            target = self._creation_target(alias=alias, path=path)
            if target.exists():
                raise ProjectError(
                    f"目录 {target} 已存在；请改用“登记已有目录”。"
                )
            try:
                target.mkdir(mode=0o700)
            except OSError as error:
                raise ProjectError(f"无法创建 Project 目录 {target}：{error}") from error
            created_directory = target
            try:
                cwd = _canonical_directory(target, alias)
            except ProjectError as error:
                raise ProjectError(
                    f"{error} 新建目录 {target} 已保留，未自动删除。"
                ) from error
        else:
            if path is None or not path.strip():
                raise ProjectError("登记已有目录时必须填写绝对路径。")
            cwd = _canonical_directory(Path(path.strip()).expanduser(), alias)

        try:
            record = self._store.register_project(alias=alias, cwd=str(cwd))
        except StoredProjectConflict as error:
            if created_directory is not None:
                raise ProjectAlreadyExists(
                    f"Project {alias} 已被其他操作登记；新建目录 {created_directory} "
                    "已保留，未自动删除。"
                ) from error
            raise ProjectAlreadyExists(f"Project {alias} 已存在。") from error
        except Exception as error:
            if created_directory is not None:
                raise ProjectError(
                    f"Project {alias} 登记失败；新建目录 {created_directory} "
                    "已保留，未自动删除。"
                ) from error
            raise
        return _project(record)

    def set_enabled(
        self,
        *,
        alias: str,
        enabled: bool,
        expected_revision: int,
    ) -> Project:
        if alias == "none" and not enabled:
            raise ProjectError("保留 Project none 不能停用。")
        try:
            record = self._store.set_project_enabled(
                alias=alias,
                enabled=enabled,
                expected_revision=expected_revision,
            )
        except StoredProjectNotFound as error:
            raise UnknownProject(alias) from error
        except StoredProjectRevisionConflict as error:
            raise StaleProject(
                f"Project {alias} 已被其他操作修改，请刷新卡片后重试。"
            ) from error
        return _project(record)

    def _resolve(self, alias: str) -> Project:
        try:
            return _project(self._store.get_project(alias))
        except StoredProjectNotFound as error:
            raise UnknownProject(alias) from error

    def _bootstrap(
        self,
        *,
        alias: str,
        configured_path: Path,
        reserved: bool,
    ) -> None:
        _validate_alias(alias, reserved=reserved)
        try:
            self._store.get_project(alias)
        except StoredProjectNotFound:
            cwd = _canonical_directory(configured_path, alias)
            self._store.bootstrap_project(alias=alias, cwd=str(cwd))

    def _creation_target(self, *, alias: str, path: str | None) -> Path:
        raw = (
            Path(path.strip()).expanduser()
            if path is not None and path.strip()
            else self._project_root / alias
        )
        if not raw.is_absolute():
            raise ProjectError("创建 Project 的路径必须是绝对路径。")
        target = raw.resolve(strict=False)
        if target == self._project_root or not target.is_relative_to(
            self._project_root
        ):
            raise ProjectError(
                f"只能在 projectRoot {self._project_root} 内创建目录。"
            )
        try:
            parent = target.parent.resolve(strict=True)
        except OSError as error:
            raise ProjectError(f"Project 父目录不存在：{target.parent}") from error
        if not parent.is_relative_to(self._project_root):
            raise ProjectError(
                f"只能在 projectRoot {self._project_root} 内创建目录。"
            )
        if not parent.is_dir() or not os.access(parent, os.R_OK | os.W_OK | os.X_OK):
            raise ProjectError(f"Project 父目录不可读写：{parent}")
        return target


def _validate_alias(alias: str, *, reserved: bool) -> None:
    if not _ALIAS.fullmatch(alias):
        raise ProjectError(f"无效 Project alias：{alias}")
    if reserved:
        if alias != "none":
            raise ProjectError(f"无效保留 Project alias：{alias}")
    elif alias == "none":
        raise ProjectError("Project alias none 为系统保留。")


def _project(record: ProjectRecord) -> Project:
    return Project(
        alias=record.alias,
        cwd=Path(record.cwd),
        enabled=record.enabled,
        revision=record.revision,
    )


def _usable_project(project: Project) -> Project:
    cwd = _canonical_directory(project.cwd, project.alias)
    if cwd == project.cwd:
        return project
    return Project(project.alias, cwd, project.enabled, project.revision)


def _canonical_directory(path: Path, alias: str) -> Path:
    if not path.is_absolute():
        raise ProjectError(f"Project {alias} cwd 必须是绝对路径。")
    try:
        cwd = path.expanduser().resolve(strict=True)
    except OSError as error:
        raise ProjectError(f"Project {alias} cwd 不存在：{path}") from error
    if not cwd.is_dir():
        raise ProjectError(f"Project {alias} cwd 不是目录。")
    if not os.access(cwd, os.R_OK | os.W_OK | os.X_OK):
        raise ProjectError(f"Project {alias} cwd 必须可读、可写、可进入。")
    return cwd
