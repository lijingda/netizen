"""Persistent fork cards and creation."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import replace
from typing import Any

from lark_channel import (
    OutboundCard,
    SendOpts,
)

from .base import CHANNEL_LOGGER_NAME
from ..bindings import (
    ProjectConflict,
    ThreadBinding,
)
from ..chat_targets import ChatTargetError
from ..cards import (
    CardActionError,
    fetched_card_topic_id,
    scope_from_fetched_card,
)
from .messages import public_chat_kind as _public_chat_kind
from .topics import TopicPublishError, complete_fresh_topic, send_topic_message
from ..cards.chat_target import ChatSearchSnapshot
from ..cards.fork import (
    ForkCardAction, ForkCardCapacityError, ForkFormValidationError, ForkSource, decode_fork_action, fork_form_card, fork_status_card,
)
from ..feishu_links import topic_open_url
from ..runtime.contracts import (
    ThreadNotMaterialized,
)
from ..domain import (
    ControlIntent,
    FeishuScope,
    MentionContextMode,
    ScopeKind,
)
from ..error_messages import describe_error
from ..management.chat_directory import AvailableChat

from .app_values import (
    _CardReplyConversation,
    _CardReplyTarget,
)
from .sides import SideChannel


logger = logging.getLogger(CHANNEL_LOGGER_NAME)


class ForkChannel(SideChannel):
    """Persistent fork cards and creation."""

    async def _fork_source_title(self, binding: ThreadBinding) -> str:
        try:
            async with asyncio.timeout(3):
                metadata = await self._runtime.thread_summary(binding.native_thread_id)
                return metadata.name or binding.short_id
        except Exception:
            return binding.short_id

    async def _fork_card(self, message: Any, intent: ControlIntent) -> None:
        binding = self._bindings.active_binding(intent.scope.key)
        if binding is None:
            await self._reply_no_current_binding(message, intent.scope)
            return
        if binding.native_thread_id is None:
            raise ThreadNotMaterialized("当前会话尚无原生上下文；请先发送任务，再使用 /fork。")
        project = self._bindings.get_project(binding.project_alias)
        self._bindings.require_project_not_deleting(binding.project_alias)
        if not project.enabled:
            raise CardActionError("Project 已停用，不能创建分支。")
        source = ForkSource(
            binding.id, binding.native_thread_id, binding.settings_revision,
            binding.context_revision, binding.feedback_revision, project.revision,
        )
        await self._reply(message, await self._fork_form(intent.scope, source, binding))

    async def _fork_form(
        self, scope: FeishuScope, source: ForkSource, binding: ThreadBinding, *,
        name: str | None = None, target_chat_id: str | None = None,
        target_mode: str | None = None, target_choice: str | None = None, target_id: str = "",
        query: str = "", notice: str | None = None,
        query_input: str | None = None, searched: bool = False,
        chat_snapshot: ChatSearchSnapshot | None = None, chat_page: int = 0,
    ) -> OutboundCard:
        title = await self._fork_source_title(binding)
        if searched:
            try:
                chat_snapshot = await self._card_chat_snapshot(query=query)
            except CardActionError as error:
                notice = "\n".join(item for item in (notice, str(error)) if item)
        # A page callback already carries all names and avatar keys. Do not
        # re-read the directory or re-upload images to display that snapshot.
        chats = {chat.chat_id: chat for chat in chat_snapshot.chats} if chat_snapshot is not None else {}
        selected_id = target_choice if target_choice is not None else (
            target_chat_id if target_mode in {None, "group"} and target_chat_id != scope.chat_id else None
        )
        if (chat_snapshot is None and selected_id
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,191}", selected_id)):
            # Keep a selected destination and the name across optional remote
            # search/paging. Membership is still revalidated on creation.
            selected = await self._card_display_group(selected_id)
            chats[selected_id] = selected or AvailableChat(selected_id, selected_id, None, None)
        display = dict(
            source_title=title, project_alias=binding.project_alias,
            avatar_keys=(await self._chat_avatars.prepare(tuple(chats.values()))
                         if chat_snapshot is None else chat_snapshot.avatar_keys),
            name=name, target_chat_id=target_chat_id, query=query, query_input=query_input,
            target_mode=target_mode, target_choice=target_choice, target_id=target_id,
        )
        try:
            return fork_form_card(
                scope, source, **display, chats=tuple(chats.values()),
                chat_snapshot=chat_snapshot, chat_page=chat_page, notice=notice,
            )
        except ForkCardCapacityError:
            # Do not truncate searchable names or silently present a partial
            # directory as complete. Keep the draft while requesting a query.
            selected = chats.get(selected_id) if selected_id else None
            return fork_form_card(
                scope, source, **display, chats=(selected,) if selected else (),
                notice="\n".join(item for item in (notice,
                    "完整群聊结果超过卡片容量，请按更具体的群名查找，或填写聊天 ID；未截断结果。") if item),
            )

    def _require_fork_source(self, scope: FeishuScope, source: ForkSource) -> ThreadBinding:
        binding = self._bindings.get(source.binding_id)
        if binding.scope_key != scope.key or not binding.active or (
            binding.native_thread_id, binding.settings_revision,
            binding.context_revision, binding.feedback_revision,
        ) != (
            source.native_thread_id, source.settings_revision,
            source.context_revision, source.feedback_revision,
        ):
            raise CardActionError("来源会话或配置已变化，请重新发送 /fork。")
        project = self._bindings.get_project(binding.project_alias)
        if not project.enabled or project.revision != source.project_revision:
            raise CardActionError("来源 Project 已变化，请重新发送 /fork。")
        self._bindings.require_project_not_deleting(binding.project_alias)
        return binding

    async def _handle_fork_card_action(self, event: Any) -> None:
        action = getattr(event, "action", None)
        message_id = str(getattr(event, "message_id", "") or "")
        chat_id = str(getattr(event, "chat_id", "") or "")
        scope: FeishuScope | None = None
        try:
            sender_id = getattr(getattr(event, "operator", None), "open_id", None)
            if getattr(action, "tag", None) != "button" or not message_id or not chat_id or not sender_id:
                raise CardActionError("分支卡片回调缺少真实卡片或操作者。")
            fetched = await self._channel.fetch_message(message_id)
            topic_id = fetched_card_topic_id(callback_chat_id=chat_id, fetched_message=fetched)
            chat_kind = None if topic_id is not None else _public_chat_kind(await self._channel.get_chat_info(chat_id))
            scope = scope_from_fetched_card(app_id=self._app_id, callback_chat_id=chat_id,
                fetched_message=fetched, chat_type=chat_kind)
            if self._bindings.side_topic_for_message(
                app_id=self._app_id, chat_id=chat_id, topic_id=topic_id, root_message_id=message_id,
            ) is not None:
                raise CardActionError("Side 话题不支持创建持久分支，请返回普通会话使用 /fork。")
            notice = None
            try:
                request = decode_fork_action(scope, getattr(action, "value", None), getattr(action, "form_value", None))
            except ForkFormValidationError as error:
                request, notice = error.draft, str(error)
            source = self._require_fork_source(scope, request.source)
            if request.action == "results":
                card = await self._fork_form(
                    scope, request.source, source, name=request.name, target_chat_id=request.target_chat_id,
                    target_mode=request.target_mode, target_choice=request.target_choice, target_id=request.target_id,
                    query=request.query or "", notice=notice,
                    query_input=request.query_input, searched=request.search_requested,
                    chat_snapshot=request.chat_snapshot, chat_page=request.chat_page,
                )
            elif request.action == "create":
                await self._create_fork_from_card(
                    intent=request, scope=scope, source=source, source_title=await self._fork_source_title(source),
                    message_id=message_id, sender_id=sender_id,
                )
                return
            else:
                raise CardActionError("未知分支卡片动作。")
            if not await self._safe_update_card(message_id, card):
                raise CardActionError("分支选择卡片更新未确认，请重新发送 /fork。")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("fork card action failed", extra={"error_type": type(error).__name__})
            await self._report_card_action_error(
                None, message_id, chat_id, error, scope=scope,
                unexpected=not isinstance(error, CardActionError), recovery_command="/fork",
            )

    async def _create_fork_from_card(
        self, *, intent: ForkCardAction, scope: FeishuScope, source: ThreadBinding,
        source_title: str, message_id: str, sender_id: str,
    ) -> None:
        assert intent.target_chat_id is not None and intent.name is not None
        target_chat_id = intent.target_chat_id
        try:
            target = await self._management.validate_chat_target(target_chat_id)
        except ChatTargetError as error:
            # This is before any native or topic creation. Only this boundary
            # can restore a submit button; unknown creation results must not.
            card = await self._fork_form(
                scope, intent.source, source, name=intent.name, target_chat_id=target_chat_id,
                target_mode=intent.target_mode, target_choice=intent.target_choice, target_id=intent.target_id,
                query=intent.query or "", notice=str(error),
                query_input=intent.query_input,
            )
            if not await self._safe_update_card(message_id, card):
                raise CardActionError("目标校验未通过，表单恢复失败；本次未创建分支，请重新发送 /fork。") from error
            return
        project = self._bindings.get_project(source.project_alias)
        self._bindings.require_project_not_deleting(project.alias)
        if not project.enabled:
            raise ProjectConflict("Project 已停用，不能创建分支。")
        if project.revision != intent.source.project_revision:
            raise CardActionError("来源 Project 已变化，请重新发送 /fork。")
        common = {"name": intent.name, "source_title": source_title, "project_alias": project.alias}
        thread = root = binding = None
        topic_url = None
        status = "failed"
        detail = None
        async with self._runtime.track_fork_creation(project.alias):
            try:
                await self._safe_update_card(message_id, fork_status_card(status="creating", **common))
                async with self._scope_coordinator.hold(scope.key):
                    source = self._require_fork_source(scope, intent.source)
                    cwd = await self._binding_cwd(source)
                    registered_project = self._bindings.get_project(project.alias)
                    worktree = self._bindings.worktrees.find_by_cwd(
                        str(cwd), project_alias=project.alias,
                        project_generation=registered_project.generation,
                    )
                    source = replace(source, working_cwd=str(cwd),
                                     worktree_id=None if worktree is None else worktree.id)
                    thread = await self._runtime.fork_exact(source, expected_project_revision=project.revision)
                self._runtime.require_fork_creation_open(project.alias)
                root = await send_topic_message(self._channel, target_chat_id,
                    fork_status_card(status="creating", **common),
                    SendOpts(receive_id_type="chat_id", uuid=hashlib.sha256(
                        ("netizen-fork-root:v1\0" + thread.id).encode()).hexdigest()[:32]))
                origin = await complete_fresh_topic(
                    self._channel, root,
                    seed_content="分支创建结果请查看话题顶部卡片。创建成功后，可在本话题发送消息继续。",
                    seed_uuid=hashlib.sha256(("netizen-fork-seed:v1\0" + thread.id).encode()).hexdigest()[:32],
                )
                if origin.thread_id is None or (target_chat_id == scope.chat_id and origin.thread_id == scope.topic_id):
                    raise TopicPublishError("飞书未创建新的普通话题，本次未绑定分支。", unknown=True)
                destination = FeishuScope(self._app_id, target_chat_id, ScopeKind.TOPIC, origin.thread_id)
                topic_url = topic_open_url(target_chat_id, origin.thread_id)
                anchor = await self._resolve_context_anchor(destination, origin.message_id) if (
                    target.chat_kind == "group" and source.message_context_mode is MentionContextMode.CATCH_UP
                ) else None
                async with self._scope_coordinator.hold(destination.key):
                    self._runtime.require_fork_creation_open(project.alias)
                    binding = self._bindings.create_fork_binding(
                        scope=destination, source=source, native_thread_id=thread.id,
                        root_message_id=root.message_id, creator_id=sender_id,
                        expected_project_revision=project.revision, context_anchor=anchor,
                        target_chat_kind=target.chat_kind,
                    )
                    named = await self._runtime.adopt_fork(binding, thread, name=intent.name)
                status = "success"
                if not named:
                    detail = "会话已创建；原生名称更新未确认，可稍后使用 /rename 修改。"
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("persistent fork creation incomplete", extra={"error_type": type(error).__name__})
                detail = describe_error(error)
                if binding is not None:
                    detail = "本地会话已保存，但订阅交接未确认；请先核查会话，不要重新创建。" + detail
            finally:
                if thread is not None and binding is None:
                    await self._runtime.release_unbound_fork(thread)
            result = dict(status=status, detail=detail,
                native_thread_id=None if thread is None else thread.id,
                binding_saved=binding is not None, **common)
            if root is not None:
                await self._safe_update_card(root.message_id, fork_status_card(**result))
            card = fork_status_card(topic_url=topic_url, **result)
            if not await self._safe_update_card(message_id, card):
                target = _CardReplyTarget(message_id, message_id, scope.chat_id, _CardReplyConversation(scope.topic_id))
                try:
                    await self._reply(target, card)
                except Exception:
                    # Delivery cannot undo a known Binding or replace its
                    # success/partial-result notice with a generic error.
                    logger.warning("fork result receipt was not delivered", exc_info=True)
