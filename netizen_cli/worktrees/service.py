"""One application boundary for owned Git resources, never a second Runtime."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..admin.port_config import AdminPortConfigurationError
from ..bindings import BindingNotFound, ProjectNotFound, SideTopicQuery, SideTopicState
from ..projects import ProjectError
from .errors import WorktreeError
from .git import GitError, NotGitRepository, WorktreeGit, WorktreeIdentity, WorktreeState
from .models import WorktreeConflict, WorktreeCursor, WorktreeNotFound, WorktreeRecord
from .policy import WorkspacePolicy
from .settings import WorktreeSettings, WorktreeSettingsError, WorktreeSettingsService

if TYPE_CHECKING:
    from ..management.service import InstanceManagementService

logger = logging.getLogger(__name__)
_READ_SECONDS = 15.0
_MUTATION_SECONDS = 120.0
_SIDE_INVENTORY_LIMIT = 1000


class WorktreeService:
    def __init__(self, management: InstanceManagementService, *, settings: WorktreeSettingsService,
                 git: WorktreeGit | None = None) -> None:
        self._management = management
        self._bindings = management._bindings
        self._store = self._bindings.worktrees
        self._runtime = management._runtime
        self._io = management._blocking_io
        self._settings = settings
        self._git = git or WorktreeGit()
        self._reads = asyncio.Semaphore(4)
        self._closed = False
        self._mutations: set[asyncio.Task] = set()

    def settings_view(self) -> dict[str, Any]:
        return self._settings.view()

    async def update_settings(self, *, root: str, fetch_before_create: bool,
                              expected_revision: str) -> dict[str, Any]:
        try:
            settings = WorktreeSettings.from_mapping({"root": root, "fetchBeforeCreate": fetch_before_create})
            await self._io.submit(self._settings.update, settings, expected_revision=expected_revision,
                                  deadline=asyncio.get_running_loop().time() + 10)
        except WorktreeSettingsError as error:
            raise WorktreeError("conflict", str(error)) from error
        except AdminPortConfigurationError as error:
            raise WorktreeError("configuration_changed", "实例配置文件已变化；请重启实例加载配置后再保存。") from error
        except OSError as error:
            raise WorktreeError("configuration_unavailable", "无法保存 worktree 配置，当前配置未更新。") from error
        return self.settings_view()

    def _project(self, alias: str, revision: int | None = None):
        try:
            project = self._bindings.get_project(alias)
            self._bindings.require_project_not_deleting(alias)
        except (ProjectNotFound, RuntimeError) as error:
            raise WorktreeError("project_unavailable", "Project 不存在或正在删除，请刷新。") from error
        if not project.enabled or revision is not None and project.revision != revision:
            raise WorktreeError("project_unavailable", "Project 已停用或改变，请刷新后重试。")
        return project

    def _record(self, worktree_id: str) -> WorktreeRecord:
        try:
            return self._store.get(worktree_id)
        except WorktreeNotFound as error:
            raise WorktreeError("not_found", "受管 worktree 登记不存在，请刷新。") from error

    def _orphaned(self, record: WorktreeRecord) -> bool:
        try:
            project = self._bindings.get_project(record.project_alias)
        except ProjectNotFound:
            return True
        return project.generation != record.project_generation

    @staticmethod
    def _identity(record: WorktreeRecord) -> WorktreeIdentity:
        if not record.git_dir or not record.identity_token:
            raise WorktreeError("outcome_unknown", "创建身份尚未确认，请先检查实际目录；不会自动认领或重试。")
        return WorktreeIdentity(Path(record.root), Path(record.cwd), Path(record.common_dir),
                                Path(record.git_dir), record.identity_token)

    async def _state(self, record: WorktreeRecord) -> WorktreeState:
        async with self._reads:
            try:
                async with asyncio.timeout(_READ_SECONDS):
                    if record.git_dir and record.identity_token:
                        return await self._git.inspect(self._identity(record))
                    def missing() -> bool:
                        try:
                            os.lstat(record.root)
                        except FileNotFoundError:
                            return True
                        return False
                    absent = await self._io.submit(missing, deadline=asyncio.get_running_loop().time() + 2)
                    return WorktreeState("missing" if absent else "unknown")
            except asyncio.CancelledError:
                raise
            except Exception:
                return WorktreeState("unknown")

    def _view(self, record: WorktreeRecord, state: WorktreeState) -> dict[str, Any]:
        orphaned = self._orphaned(record)
        available = not orphaned and record.creation_state == "ready" and state.status == "ready"
        if available:
            try:
                self._project(record.project_alias)
                self._store.require_not_deleting(record.id)
            except (WorktreeError, WorktreeConflict):
                available = False
        return {"id": record.id, "projectAlias": record.project_alias,
                "projectGeneration": record.project_generation, "root": record.root, "cwd": record.cwd,
                "state": state.status, "head": state.head, "branch": state.branch,
                "orphaned": orphaned, "creationState": record.creation_state,
                "available": available,
                "associatedSessionCount": self._store.binding_count(record.id),
                "createdAt": record.created_at}

    async def query(self, *, project_alias: str | None = None, cursor: str | None = None,
                    limit: int = 20, deadline: float | None = None) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise WorktreeError("invalid_arguments", "每页数量必须为 1–100。")
        position = None
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 2048:
                    raise ValueError()
                value = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                if not isinstance(value, list) or len(value) != 3 or value[0] != project_alias:
                    raise ValueError()
                if any(not isinstance(part, str) or not part or len(part) > 256 for part in value[1:]):
                    raise ValueError()
                position = WorktreeCursor(value[1], value[2])
            except (ValueError, TypeError, UnicodeError) as error:
                raise WorktreeError("invalid_arguments", "worktree 分页已失效，请从第一页重新查询。") from error
        deadline = deadline or asyncio.get_running_loop().time() + _READ_SECONDS + 2
        async with asyncio.timeout_at(deadline):
            page = await self._store.list_page(project_alias=project_alias, cursor=position, limit=limit)
        states = await self._states(page.items, deadline=deadline)
        next_cursor = None
        if page.next_cursor is not None:
            next_cursor = base64.urlsafe_b64encode(json.dumps(
                [project_alias, page.next_cursor.created_at, page.next_cursor.worktree_id],
                separators=(",", ":"),
            ).encode()).decode()
        return {"items": [self._view(record, state) for record, state in zip(page.items, states, strict=True)],
                "nextCursor": next_cursor}

    async def _states(self, records, *, deadline: float) -> list[WorktreeState]:
        tasks = [asyncio.create_task(self._state(record)) for record in records]
        if not tasks:
            return []
        try:
            await asyncio.wait(tasks, timeout=max(0, deadline - asyncio.get_running_loop().time()))
            return [task.result() if task.done() and not task.cancelled() and task.exception() is None
                    else WorktreeState("unknown") for task in tasks]
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def creation_options(
        self, *, project_alias: str, expected_project_revision: int | None = None,
    ) -> dict[str, Any]:
        """Check creation eligibility without enumerating branch candidates."""
        project = self._project(project_alias, expected_project_revision)
        try:
            async with asyncio.timeout(_READ_SECONDS):
                await self._git.inspect_repository(Path(project.cwd))
        except NotGitRepository:
            return {"projectAlias": project_alias, "projectRevision": project.revision, "isGit": False}
        except GitError as error:
            raise WorktreeError("unavailable", "无法读取 Project 的 Git 仓库；未修改目录。") from error
        except TimeoutError as error:
            raise WorktreeError("unavailable", "Git 仓库状态未在时限内取得；未修改目录，请稍后刷新。") from error
        return {"projectAlias": project_alias, "projectRevision": project.revision, "isGit": True}

    async def options(self, *, project_alias: str, expected_project_revision: int | None = None) -> dict[str, Any]:
        eligibility = await self.creation_options(
            project_alias=project_alias, expected_project_revision=expected_project_revision,
        )
        if not eligibility["isGit"]:
            return {**eligibility, "items": []}
        project = self._project(project_alias, eligibility["projectRevision"])
        records = self._store.list_for_project(project_alias, generation=project.generation)
        states = await self._states(records, deadline=asyncio.get_running_loop().time() + _READ_SECONDS + 2)
        items = [self._view(record, state) for record, state in zip(records, states, strict=True)
                 if record.creation_state == "ready" and state.status == "ready"]
        return {**eligibility, "items": items}

    async def select(self, *, project_alias: str, worktree_id: str,
                     expected_project_revision: int | None = None) -> WorktreeRecord:
        project = self._project(project_alias, expected_project_revision)
        record = self._record(worktree_id)
        try:
            self._store.require_bindable(record.id, project=project, cwd=record.cwd)
        except WorktreeConflict as error:
            raise WorktreeError("conflict", str(error)) from error
        state = await self._state(record)
        if state.status != "ready":
            reason = {"missing": "worktree 目录缺失", "replaced": "worktree 身份已被替换",
                      "unknown": "worktree 状态暂时无法确认"}.get(state.status, "worktree 不可用")
            raise WorktreeError("unavailable", reason + "，请刷新；不会切回项目目录。")
        return record

    async def validate_policy(
        self, policy: WorkspacePolicy, *, project_alias: str,
        expected_project_revision: int | None = None,
    ) -> WorktreeRecord | None:
        """Read creation eligibility without creating a checkout or freezing HEAD."""
        if not isinstance(policy, WorkspacePolicy):
            raise WorktreeError("invalid_arguments", "worktree 使用方式无效。")
        if policy.mode == "project":
            return None
        try:
            project = await self._management.resolve_new_project(
                project_alias, deadline=asyncio.get_running_loop().time() + 10,
            )
        except ProjectError as error:
            raise WorktreeError("project_unavailable", str(error)) from error
        revision = expected_project_revision if expected_project_revision is not None else project.revision
        self._project(project_alias, revision)
        if policy.mode == "existing":
            return await self.select(project_alias=project_alias, worktree_id=policy.worktree_id,
                                     expected_project_revision=revision)
        try:
            async with asyncio.timeout(_READ_SECONDS):
                await self._git.inspect_repository(Path(project.cwd))
        except GitError as error:
            raise WorktreeError("not_git" if isinstance(error, NotGitRepository) else "unavailable",
                                "无法确认 Project 的 Git 仓库；未创建 worktree。") from error
        except TimeoutError as error:
            raise WorktreeError("unavailable", "Git 仓库状态未在时限内取得，请稍后刷新。") from error
        self._project(project_alias, revision)
        return None

    async def create(self, *, project_alias: str, expected_project_revision: int | None = None,
                     ref: str | None = None, mode: str = "detached", branch: str | None = None,
                     source_cwd: str | Path | None = None, origin_binding_id: str | None = None,
                     agent_default: bool = False, scheduled_run_id: str | None = None) -> dict[str, Any]:
        if self._closed:
            raise WorktreeError("unavailable", "服务正在关闭，未开始创建 worktree。")
        if mode not in {"detached", "existing", "new"} or any(
            value is not None and (not isinstance(value, str) or not value or len(value) > 256
                                  or any(ord(character) < 32 for character in value))
            for value in (ref, branch)
        ):
            raise WorktreeError("invalid_arguments", "无效的基线或分支选择。")
        if mode == "existing":
            if branch is None and ref and ref.startswith("refs/heads/"):
                branch = ref[len("refs/heads/"):]
            if not branch or ref not in (None, branch, "refs/heads/" + branch):
                raise WorktreeError("invalid_arguments", "检出现有分支时只能选择该本地分支本身。")
            ref = None
        if mode == "detached" and branch is not None or mode == "new" and not branch:
            raise WorktreeError("invalid_arguments", "新分支名称与检出方式不匹配。")
        project = self._project(project_alias, expected_project_revision)
        source = Path(source_cwd) if source_cwd is not None else Path(project.cwd)
        task = asyncio.current_task()
        assert task is not None
        self._mutations.add(task)
        record = None
        try:
            async with asyncio.timeout(_MUTATION_SECONDS):
                repository = await self._git.inspect_repository(source)
                if source_cwd is not None:
                    original = await self._git.inspect_repository(Path(project.cwd))
                    if repository.common_dir != original.common_dir:
                        raise WorktreeError("conflict", "来源目录不属于当前 Project 的仓库。")
                settings, _ = self._settings.snapshot()
                pool = self._settings.resolve_root(settings)
                def prepare_root() -> Path:
                    resolved = pool.resolve()
                    if resolved == Path(resolved.anchor) or resolved.is_relative_to(repository.common_dir):
                        raise WorktreeError("invalid_arguments", "托管根不能是文件系统根或 Git 元数据目录。")
                    resolved.mkdir(parents=True, exist_ok=True, mode=0o700)
                    return resolved.resolve(strict=True)
                pool = await self._io.submit(prepare_root, deadline=asyncio.get_running_loop().time() + 10)
                identifier = str(uuid.uuid4())
                target = pool / identifier
                self._project(project_alias, project.revision)
                record = self._store.register(project_alias=project_alias, expected_project_revision=project.revision,
                    root=str(target), cwd=str(target / repository.relative_cwd), common_dir=str(repository.common_dir),
                    origin_binding_id=origin_binding_id, worktree_id=identifier, scheduled_run_id=scheduled_run_id)
                created = await self._git.create(source, target, ref=ref, mode=mode, branch=branch,
                    fetch=settings.fetch_before_create, agent_default=agent_default)
                record = self._store.mark_ready(record.id, git_dir=str(created.identity.git_dir),
                                                identity_token=created.identity.identity_token)
                return self._view(record, WorktreeState("ready", created.head, created.branch))
        except asyncio.CancelledError:
            if record is not None:
                self._store.mark_unknown(record.id)
            raise
        except WorktreeError:
            if record is not None:
                self._store.mark_unknown(record.id)
            raise
        except Exception as error:
            details = {}
            if record is not None and isinstance(error, GitError) and error.outcome == "not_started":
                try:
                    self._store.discard_unstarted(record)
                except Exception as cleanup_error:
                    logger.warning("worktree_reservation_retained type=%s", type(cleanup_error).__name__)
                else:
                    record = None
            if record is not None:
                record = self._store.mark_unknown(record.id)
                details["worktree"] = {"id": record.id, "root": record.root, "cwd": record.cwd,
                                       "creationState": record.creation_state}
            logger.warning("worktree_create_failed type=%s", type(error).__name__)
            raise WorktreeError("outcome_unknown" if record is not None else "unavailable",
                "worktree 创建未确认完成；已知登记和目录保留，可在 Admin 检查，勿自动重试。" if record else
                "无法准备 worktree；本次未开始创建 Git worktree。若已执行 fetch，更新的引用不会回滚。",
                details=details) from error
        finally:
            self._mutations.discard(task)

    async def _sides(self, record: WorktreeRecord, bindings, usage_bindings=()) -> tuple:
        parents = {binding.id for binding in bindings}
        runtime_sides = self._runtime.worktree_side_snapshots(Path(record.root))
        aliases = {record.project_alias, *(binding.project_alias for binding in usage_bindings)}
        extra = tuple(side.side_id for side in runtime_sides)
        selected, cursor = [], None
        while True:
            page = await self._bindings.query_side_topics(query=SideTopicQuery(
                project_aliases=tuple(sorted(aliases)), states=(SideTopicState.CREATING, SideTopicState.OPEN)),
                extra_side_ids=extra, cursor=cursor, limit=100, deadline_seconds=1)
            selected.extend(item.side_topic for item in page.items
                            if item.side_topic.parent_binding_id in parents or item.side_topic.id in extra)
            if len(selected) > _SIDE_INVENTORY_LIMIT:
                raise WorktreeError("unavailable", "关联 Side 超过单次清理上限。")
            cursor = page.next_cursor
            if cursor is None:
                break
        if not set(extra) <= {side.id for side in selected}:
            raise WorktreeError("unavailable", "已知 Side 使用目录但路由状态尚未确认，请先处理该 Side。")
        return tuple(selected)

    @staticmethod
    def _fingerprint(snapshot, sides, delete_sessions: bool, usage_bindings=()) -> str:
        value = [snapshot.fingerprint, delete_sessions,
                 [(side.id, side.parent_binding_id, side.app_id, side.chat_id, side.topic_id, side.root_message_id)
                  for side in sides],
                 [(binding.id, binding.native_thread_id, binding.working_cwd) for binding in usage_bindings]]
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    async def _usage_reasons(self, snapshot, sides, *, delete_sessions: bool,
                             project_deletion_bindings: frozenset[str] = frozenset(), usage_bindings=()) -> list[str]:
        record, reasons = snapshot.record, []
        associated = {binding.id for binding in snapshot.bindings}
        for binding in snapshot.bindings:
            usage = await self._runtime.worktree_usage(binding.id)
            current = self._bindings.get(binding.id)
            if current.native_thread_id != binding.native_thread_id:
                reasons.append("关联会话已物化或身份发生改变，请重新预览。")
            if usage.get("cwd") != record.cwd:
                reasons.append("关联会话的实际目录已改变或无法确认，请先处理该会话。")
            if not delete_sessions and usage.get("status") != "idle":
                reasons.append("保留的关联会话正在使用目录或状态未知。")
        for binding in usage_bindings:
            if binding.id in project_deletion_bindings:
                continue
            usage = await self._runtime.worktree_usage(binding.id)
            if usage.get("status") != "idle":
                reasons.append("本实例另有已知会话使用此目录且仍活动或状态未知；它不属于级联删除清单。")
        if (record.origin_binding_id is not None and record.origin_binding_id not in associated
                and record.origin_binding_id not in project_deletion_bindings):
            try:
                self._bindings.get(record.origin_binding_id)
            except BindingNotFound:
                pass
            else:
                usage = await self._runtime.worktree_usage(record.origin_binding_id)
                if usage.get("status") != "idle":
                    reasons.append("创建来源会话仍活动或状态未知；它不会被级联删除。")
        if any(side.state is SideTopicState.CREATING for side in sides):
            reasons.append("关联 Side 仍在创建，请完成后重新确认。")
        if sides and not delete_sessions:
            reasons.append("该 worktree 仍有关联 Side，请先结束 Side 或选择同时删除关联会话。")
        if any(side.parent_binding_id not in associated | project_deletion_bindings for side in sides):
            reasons.append("仍有非级联会话的 Side 使用此目录，请先结束该 Side。")
        if delete_sessions and any(binding.native_thread_id for binding in snapshot.bindings) and not self._management.native_delete_available:
            reasons.append("原生会话删除能力不可用。")
        return reasons

    async def _delete_check(self, record: WorktreeRecord) -> tuple[WorktreeState, list[str]]:
        # A code directory explicitly registered as another Project is never
        # silently removed, even if the same path was originally allocated here.
        if any(Path(project.cwd).is_relative_to(Path(record.root)) for project in self._bindings.list_projects()):
            return await self._state(record), ["该目录同时是一个 Project 的代码目录，不能作为 worktree 删除。"]
        if not record.git_dir or not record.identity_token:
            state = await self._state(record)
            return state, [] if state.status == "missing" else ["创建身份未确认；请先人工处理目录后清理登记。"]
        check = await self._git.preflight_delete(self._identity(record))
        # Never expose raw stderr, repository configuration or remote URLs.
        reasons = [] if check.safe else ["目录身份、未保存文件或未保留提交未通过安全检查；请先在 Git 中处理。"]
        return check.state, reasons

    async def preview_delete(self, worktree_id: str, *, delete_sessions: bool = False,
                             _project_deletion_bindings: frozenset[str] = frozenset()) -> dict[str, Any]:
        if type(delete_sessions) is not bool:
            raise WorktreeError("invalid_arguments", "关联会话删除选项必须为布尔值。")
        try:
            async with asyncio.timeout(_READ_SECONDS):
                snapshot = self._store.preview_delete(worktree_id)
                usage_bindings = self._store.usage_bindings(worktree_id)
                sides = await self._sides(snapshot.record, snapshot.bindings, usage_bindings)
                state, reasons = await self._delete_check(snapshot.record)
                reasons.extend(await self._usage_reasons(snapshot, sides, delete_sessions=delete_sessions,
                    project_deletion_bindings=_project_deletion_bindings, usage_bindings=usage_bindings))
        except WorktreeNotFound as error:
            raise WorktreeError("not_found", "worktree 登记不存在。") from error
        except WorktreeConflict as error:
            raise WorktreeError("conflict", str(error)) from error
        except TimeoutError as error:
            raise WorktreeError("unavailable", "删除检查未在时限内完成；未删除任何资源。") from error
        return {"worktreeId": worktree_id, "fingerprint": self._fingerprint(snapshot, sides, delete_sessions, usage_bindings),
                "deleteSessions": delete_sessions, "resource": self._view(snapshot.record, state),
                "bindings": [{"bindingId": binding.id, "shortId": binding.short_id,
                              "nativeThreadId": binding.native_thread_id,
                              "scopeKey": binding.scope_key} for binding in snapshot.bindings],
                "sideCount": len(sides), "protectedSessionCount": len(usage_bindings),
                "safe": not reasons, "reasons": list(dict.fromkeys(reasons)),
                "externalUseWarning": "仅检查本实例已知关联；请确认没有其他客户端正在使用目录。"}

    async def delete(self, worktree_id: str, *, delete_sessions: bool = False,
                     expected_fingerprint: str, deadline: float | None = None) -> dict[str, Any]:
        from ..management.service import CurrentSideTarget, ExactBindingTarget

        if self._closed:
            raise WorktreeError("unavailable", "服务正在关闭。")
        if type(delete_sessions) is not bool:
            raise WorktreeError("invalid_arguments", "关联会话删除选项必须为布尔值。")
        deadline = min(deadline or float("inf"), asyncio.get_running_loop().time() + _MUTATION_SECONDS)
        snapshot = None
        reserved = False
        deleted_bindings = 0
        task = asyncio.current_task()
        assert task is not None
        self._mutations.add(task)
        try:
            async with asyncio.timeout_at(deadline):
                snapshot = self._store.preview_delete(worktree_id)
                usage_bindings = self._store.usage_bindings(worktree_id)
                sides = await self._sides(snapshot.record, snapshot.bindings, usage_bindings)
                if self._fingerprint(snapshot, sides, delete_sessions, usage_bindings) != expected_fingerprint:
                    raise WorktreeError("inventory_changed", "worktree、关联会话或删除选项已经改变，请重新确认。")
                self._store.reserve_delete(worktree_id, expected_fingerprint=snapshot.fingerprint)
                reserved = True
                # Admission is now closed for both users and the creating source.
                # Runtime's exact Binding locks hand off already in-flight starts.
                reasons = await self._usage_reasons(snapshot, sides, delete_sessions=delete_sessions,
                                                     usage_bindings=usage_bindings)
                latest_usage = self._store.usage_bindings(worktree_id)
                latest_sides = await self._sides(snapshot.record, snapshot.bindings, latest_usage)
                if self._fingerprint(snapshot, latest_sides, delete_sessions, latest_usage) != expected_fingerprint:
                    raise WorktreeError("inventory_changed", "在途 Side 创建已改变关联清单，请重新确认。")
                _, safe_reasons = await self._delete_check(snapshot.record)
                reasons.extend(safe_reasons)
                if reasons:
                    raise WorktreeError("unsafe_delete", "；".join(dict.fromkeys(reasons)))
                if delete_sessions:
                    for side in sides:
                        await self._management.close_side(target=CurrentSideTarget(
                            side_id=side.id, app_id=side.app_id, chat_id=side.chat_id,
                            topic_id=side.topic_id, root_message_id=side.root_message_id))
                        if not self._bindings.get_side_topic(side.id).state.terminal or self._runtime.side_snapshot_exact(side.id):
                            raise WorktreeError("in_use", "Side 尚未确认结束，目录保留。")
                    for binding in snapshot.bindings:
                        try:
                            await self._management.delete_exact_binding(
                                target=ExactBindingTarget(binding.scope_key, binding.id, None),
                                expected_native_thread_id=binding.native_thread_id)
                        except BindingNotFound:
                            try:
                                self._bindings.get(binding.id)
                            except BindingNotFound:
                                pass
                            else:
                                raise
                        deleted_bindings += 1
                # Fresh Git safety validation includes any writes during native shutdown.
                if snapshot.record.git_dir and snapshot.record.identity_token:
                    await self._git.remove(self._identity(snapshot.record))
                elif (await self._state(snapshot.record)).status != "missing":
                    raise WorktreeError("inventory_changed", "目录重新出现，未清理登记。")
                self._store.finish_delete(worktree_id, expected_fingerprint=snapshot.fingerprint)
                return {"worktreeId": worktree_id, "deleted": True, "code": "deleted",
                        "deletedSessionCount": deleted_bindings,
                        "retainedSessionCount": 0 if delete_sessions else len(snapshot.bindings)}
        except asyncio.CancelledError:
            raise
        except (WorktreeNotFound, WorktreeConflict) as error:
            raise WorktreeError("inventory_changed", "登记或关联已经改变，请重新预览。") from error
        except WorktreeError as error:
            if deleted_bindings:
                error.details.update({"worktreeId": worktree_id,
                                      "deletedSessionCount": deleted_bindings,
                                      "remainingSessionCount": len(self._store.bindings(worktree_id))})
            raise
        except Exception as error:
            logger.warning("worktree_delete_incomplete type=%s", type(error).__name__)
            return {"worktreeId": worktree_id, "deleted": False,
                    "code": "outcome_unknown" if isinstance(error, (TimeoutError, GitError)) else "delete_failed",
                    "deletedSessionCount": deleted_bindings,
                    "remainingSessionCount": len(self._store.bindings(worktree_id)),
                    "message": "操作未全部确认完成，已成功项不回滚；请重新查看实际状态后再确认。"}
        finally:
            if reserved:
                self._store.release_delete(worktree_id)
            self._mutations.discard(task)

    async def close(self, *, deadline: float | None = None) -> None:
        self._closed = True
        tasks = tuple(task for task in self._mutations if task is not asyncio.current_task())
        if tasks:
            loop = asyncio.get_running_loop()
            deadline = deadline if deadline is not None else loop.time() + 10
            remaining = max(0, deadline - loop.time())
            # Leave bounded time for Git subprocess termination and exact
            # creation/deletion evidence to settle before closing IO/SQLite.
            _, pending = await asyncio.wait(tasks, timeout=max(0, remaining - min(4, remaining)))
            for task in pending:
                task.cancel()
            if pending:
                _, pending = await asyncio.wait(pending, timeout=max(0, deadline - loop.time()))
                if pending:
                    raise TimeoutError("worktree mutations did not finish shutdown handoff")
