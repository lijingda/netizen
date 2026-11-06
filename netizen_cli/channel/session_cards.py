"""Session, project and new-binding cards."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence

from lark_channel import (
    OutboundCard,
)

from ..bindings import (
    BindingTaskFeedback,
    BindingTurnSettings,
    ThreadBinding,
)
from ..cards import (
    ArchivedSessionCardItem,
    CardActionError,
    SessionCardItem,
    SESSIONS_PAGE_SIZE,
    archived_sessions_card,
    sessions_card,
    new_binding_card,
)
from .session_pages import SessionCardPage
from ..runtime.contracts import (
    BindingRuntimeSnapshot,
    NativeThreadMetadata,
    ThreadLifecycleError,
)
from ..domain import (
    CardControlIntent,
    FeishuScope,
    MessageContextAnchor,
    MentionContextMode,
    NativeCapability,
)
from ..worktrees.errors import WorktreeError
from ..model_settings import ModelCatalogError, TurnModelSettings
from ..management import (
    RuntimePrecondition,
    SessionInventoryState,
)

from . import constants
from .base import (
    CHANNEL_LOGGER_NAME,
    _session_title,
    _session_card_cursor,
)
from .questions import QuestionChannel


logger = logging.getLogger(CHANNEL_LOGGER_NAME)


class SessionCardChannel(QuestionChannel):
    """Session, project and new-binding cards."""

    async def _resolve_card_model_settings(
        self,
        intent: CardControlIntent,
    ) -> TurnModelSettings | None:
        if intent.model_id is None:
            if intent.effort_id is not None or intent.service_tier_id is not None:
                raise CardActionError("Model / Effort / Speed 必须成组设置。")
            return None
        assert intent.model_id is not None
        assert intent.effort_id is not None
        assert intent.service_tier_id is not None
        return await self._runtime.resolve_model_settings(
            model_id=intent.model_id,
            effort_id=intent.effort_id,
            service_tier_id=intent.service_tier_id,
        )

    @staticmethod
    def _runtime_precondition(intent: CardControlIntent) -> RuntimePrecondition:
        if intent.expected_activity_revision is None:
            raise CardActionError(
                "会话运行状态前置条件缺失，请重新发送原命令。"
            )
        return RuntimePrecondition(
            activity_revision=intent.expected_activity_revision,
            physical_turn_id=intent.expected_turn_id,
        )

    async def _binding_state(
        self,
        binding: ThreadBinding,
        *,
        snapshot: BindingRuntimeSnapshot | None = None,
    ) -> str:
        projection = await self._management.binding_status_exact(
            binding.id,
            snapshot=snapshot,
        )
        assert projection.primary_status is not None
        return projection.primary_status

    async def _read_thread_metadata(
        self,
        bindings: Sequence[ThreadBinding],
        *,
        archived: bool = False,
        strict: bool = False,
    ) -> dict[str, NativeThreadMetadata]:
        # These controls already identify their exact Binding. Archive state
        # comes from the action's native result, never a title lookup.
        metadata: dict[str, NativeThreadMetadata] = {}
        deadline = asyncio.get_running_loop().time() + constants.SESSION_QUERY_TIMEOUT_SECONDS
        try:
            for binding in bindings:
                if binding.native_thread_id is None or binding.native_thread_id in metadata:
                    continue
                summary = await self._management.binding_metadata_exact(binding.id, deadline=deadline)
                if summary is not None:
                    metadata[binding.native_thread_id] = summary
            return metadata
        except Exception as error:
            if strict:
                raise ThreadLifecycleError(
                    "无法读取指定 Codex 会话的信息，请稍后重试。"
                ) from error
            # Titles are useful display metadata, never an admission or
            # correctness signal. Keep read-only controls available if the
            # native exact summary is temporarily unavailable.
            logger.warning(
                "native Thread metadata unavailable for Channel display",
                extra={"error_type": type(error).__name__},
            )
            return metadata

    async def _sessions_card(
        self,
        *,
        scope: FeishuScope,
        page: int = 0,
        session_cursor: str | None = None,
        notice: str | None = None,
    ) -> OutboundCard:
        if page and session_cursor is None:
            raise CardActionError("会话分页已过期，请重新发送 /sessions。")
        try:
            navigation = SessionCardPage.decode(session_cursor, scope)
        except ValueError as error:
            raise CardActionError(str(error)) from error
        if navigation.state == "archived":
            return await self._archived_sessions_card(
                scope=scope, session_cursor=session_cursor, notice=notice,
            )
        deadline = asyncio.get_running_loop().time() + constants.SESSION_QUERY_TIMEOUT_SECONDS
        inventory = await self._management.query_scope_sessions(
            scope=scope, lazy=navigation.state == "lazy", cursor=navigation.cursor,
            limit=SESSIONS_PAGE_SIZE, deadline=deadline,
        )
        items = inventory.items
        current = inventory.current
        if current is not None and all(
            item.record.binding.id != current.record.binding.id for item in items
        ):
            items = (*items, current)
        else:
            current = None
        projections = await self._management.binding_statuses_exact(
            binding_ids=tuple(item.record.binding.id for item in items),
            catalog_states={
                item.record.binding.id: item.native.state if item.native else None
                for item in items
            },
            deadline=deadline,
        )
        sessions: list[SessionCardItem] = []
        for item, projection in zip(items, projections, strict=True):
            binding = item.record.binding
            snapshot = projection.snapshot
            sessions.append(SessionCardItem(
                binding_id=binding.id,
                short_id=binding.short_id,
                project_alias=binding.project_alias,
                native_thread_id=binding.native_thread_id,
                title=_session_title(binding, item.native.metadata if item.native else None),
                state=projection.primary_status or "暂不可用",
                catalog_unconfirmed=item.inventory_state is SessionInventoryState.UNKNOWN,
                active=binding.active,
                activity_revision=snapshot.activity_revision,
                turn_id=snapshot.turn.turn_id if snapshot.turn is not None else None,
            ))
        current_session = sessions.pop() if current is not None else None
        previous = navigation.previous()
        return sessions_card(
            scope=scope, sessions=tuple(sessions), current=current_session,
            native_delete_available=NativeCapability.DELETE in self._runtime.available_capabilities,
            page=navigation.page, session_cursor=_session_card_cursor(navigation, scope),
            previous_cursor=_session_card_cursor(previous, scope) if previous is not None else None,
            next_cursor=(
                _session_card_cursor(navigation.next(inventory.next_cursor), scope)
                if inventory.next_cursor is not None else None
            ),
            active_binding_id=inventory.active_binding_id,
            lazy=navigation.state == "lazy", has_lazy=inventory.has_lazy, notice=notice,
        )

    async def _archived_sessions_card(
        self,
        *,
        scope: FeishuScope,
        session_cursor: str | None = None,
        notice: str | None = None,
        notice_is_error: bool = False,
    ) -> OutboundCard:
        try:
            navigation = SessionCardPage.decode(session_cursor, scope, state="archived")
            if navigation.state != "archived":
                raise ValueError("归档列表分页已过期，请重新发送 /sessions archived。")
        except ValueError as error:
            raise CardActionError(str(error)) from error
        inventory = await self._management.query_scope_sessions(
            scope=scope, archived=True, cursor=navigation.cursor, limit=SESSIONS_PAGE_SIZE,
            deadline=asyncio.get_running_loop().time() + constants.SESSION_QUERY_TIMEOUT_SECONDS,
        )
        sessions = tuple(
            ArchivedSessionCardItem(
                binding_id=item.record.binding.id,
                short_id=item.record.binding.short_id,
                project_alias=item.record.binding.project_alias,
                native_thread_id=item.record.binding.native_thread_id,
                title=_session_title(item.record.binding, item.native.metadata),
            )
            for item in inventory.items
        )
        previous = navigation.previous()
        return archived_sessions_card(
            scope=scope, sessions=sessions,
            native_delete_available=NativeCapability.DELETE in self._runtime.available_capabilities,
            page=navigation.page, session_cursor=_session_card_cursor(navigation, scope),
            previous_cursor=_session_card_cursor(previous, scope) if previous is not None else None,
            next_cursor=(
                _session_card_cursor(navigation.next(inventory.next_cursor), scope)
                if inventory.next_cursor is not None else None
            ),
            notice=notice, notice_is_error=notice_is_error,
        )

    async def _binding_model_status_lines(
        self,
        binding: ThreadBinding,
    ) -> tuple[str, str, str, str]:
        configured = binding.turn_settings
        if configured is None:
            value = "继承 Codex"
            return (
                f"Model：{value}",
                f"Effort：{value}",
                f"Speed：{value}",
                "配置来源：Codex",
            )

        try:
            catalog = await self._runtime.model_catalog()
        except Exception as error:
            # Status is read-only and must remain useful when the live catalog
            # cannot be read. The persisted IDs remain the exact Binding
            # selection; submission will revalidate them before every new Turn.
            logger.warning(
                "native model catalog unavailable for /status Binding settings",
                extra={"error_type": type(error).__name__},
            )
            return (
                f"Model：{configured.model_id}",
                f"Effort：{configured.effort_id}",
                f"Speed：{configured.service_tier_id}",
                "配置来源：Netizen 会话配置（模型目录暂不可用）",
            )

        try:
            settings = catalog.resolve(
                model_id=configured.model_id,
                effort_id=configured.effort_id,
                service_tier_id=configured.service_tier_id,
            )
        except ModelCatalogError:
            return (
                f"Model：{configured.model_id}",
                f"Effort：{configured.effort_id}",
                f"Speed：{configured.service_tier_id}",
                "配置来源：Netizen 会话配置（已失效，请使用 /config 更新）",
            )

        return (
            f"Model：{settings.model}",
            f"Effort：{settings.effort_id}",
            f"Speed：{settings.service_tier_name}",
            "配置来源：Netizen 会话配置",
        )

    async def _create_binding(
        self,
        *,
        scope: FeishuScope,
        sender_id: str,
        project_alias: str,
        expected_revision: int | None = None,
        turn_settings: BindingTurnSettings | None = None,
        task_feedback: BindingTaskFeedback = BindingTaskFeedback(),
        message_context_mode: MentionContextMode = MentionContextMode.CURRENT_ONLY,
        context_anchor: MessageContextAnchor | None = None,
        worktree_id: str | None = None,
        worktree_create: dict[str, str | None] | None = None,
    ):
        try:
            created = await self._management.create_current_binding(
                scope=scope,
                creator_id=sender_id,
                project_alias=project_alias,
                expected_project_revision=expected_revision,
                turn_settings=turn_settings,
                task_feedback=task_feedback,
                message_context_mode=message_context_mode,
                context_anchor=context_anchor,
                **({"worktree_id": worktree_id, "worktree_create": worktree_create}
                   if worktree_id is not None or worktree_create is not None else {}),
            )
        except WorktreeError as error:
            # WorktreeError is the safe application projection, not raw Git stderr.
            raise CardActionError(str(error)) from None
        assert created.project is not None
        return created.project, created.binding

    async def _new_binding_picker(
        self, *, scope: FeishuScope, initial_project_alias: str | None,
        allow_context_mode: bool, expected_project_revision: int | None = None,
        settings_draft: Mapping[str, str] | None = None,
    ) -> OutboundCard:
        projects = self._projects.list(enabled_only=True)
        catalog = None
        catalog_error = None
        try:
            catalog = await self._runtime.model_catalog()
        except Exception as error:
            logger.warning("native model catalog unavailable for /new settings",
                           extra={"error_type": type(error).__name__})
            catalog_error = "Model / Effort / Speed 暂不可用。"
        worktrees = self._management.worktrees
        workspace_options = None
        workspace_error = None
        if worktrees is not None and initial_project_alias is not None:
            try:
                workspace_options = await worktrees.options(
                    project_alias=initial_project_alias,
                    expected_project_revision=expected_project_revision,
                )
            except WorktreeError as error:
                workspace_error = str(error) + " 可直接在项目目录创建会话，或重新加载 worktree 选项。"
        return new_binding_card(
            scope=scope, projects=projects, initial_project_alias=initial_project_alias,
            catalog=catalog, catalog_error=catalog_error,
            allow_context_mode=allow_context_mode,
            workspace_options=workspace_options, workspace_error=workspace_error,
            allow_workspace_selection=worktrees is not None,
            settings_draft=settings_draft,
        )
