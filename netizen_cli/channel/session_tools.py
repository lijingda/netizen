"""Session collaboration tool entries and reply suppression."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from dataclasses import replace
from typing import Any

from lark_channel import (
    OutboundCard,
    SendOpts,
)
from openai_codex import ExternalMessage, InvalidRequestError

from ..bindings import (
    BindingNotFound,
    BindingTurnSettings,
    ProjectConflict,
    ProjectNotFound,
    ThreadBinding,
)
from .topics import TopicMessage, TopicPublishError, complete_fresh_topic, send_topic_message
from ..feishu_links import topic_open_url
from ..runtime.contracts import (
    GoalStateUnknown,
    RuntimeClosed,
    SkillReferenceError,
    SteerRace,
    Submission,
    SubmitDisposition,
    ThreadCompacting,
    ThreadGoalActive,
    ThreadArchived,
    ThreadLifecycleError,
    ThreadStopping,
    ThreadOccupied,
)
from ..domain import (
    ActiveState,
    FeishuScope,
    GoalOperationState,
    MentionContextMode,
    SessionAgentConversation,
    SessionAgentOrigin,
    ScopeKind,
)
from ..error_messages import describe_error
from ..worktrees.errors import WorktreeError
from ..worktrees.models import WorktreeConflict
from ..model_settings import ModelCatalogError
from ..message_history import (
    MessageHistoryUnavailable,
)
from ..sdk_gap_adapter import SkillCatalogError
from ..sdk_gap_adapter import GoalControlError
from ..sessions.delegation import SIDE_SOURCE_NOTICE, delegated_prompt
from ..sessions.errors import SessionToolError
from ..sessions.sources import SessionSource, resolve_session_source

from . import constants
from .base import (
    CHANNEL_LOGGER_NAME,
    _normalized_thread_text,
)
from .forks import ForkChannel


logger = logging.getLogger(CHANNEL_LOGGER_NAME)


class SessionToolChannel(ForkChannel):
    """Session collaboration tool entries and reply suppression."""

    async def suppress_reply(self, *, source_thread_id: str, source_turn_id: str) -> dict[str, Any]:
        """Record a caller's exact physical Turn choice without changing execution."""
        source = resolve_session_source(
            self._bindings, self._runtime, self._app_id, source_thread_id,
        )
        reply_turn_id = None
        if isinstance(source, ThreadBinding):
            if self._runtime.lifecycle_state(source.id) is None:
                ordinary = self._runtime.turn_activity(
                    source.id, thread_id=source_thread_id, turn_id=source_turn_id,
                )
                if ordinary is not None and ordinary.state is ActiveState.RUNNING:
                    reply_turn_id = ordinary.turn_id
                else:
                    goal = self._runtime.goal_activity(source.id, thread_id=source_thread_id)
                    if (goal is not None and goal.physical_turn_id == source_turn_id
                            and goal.state is GoalOperationState.RUNNING):
                        reply_turn_id = goal.logical_turn_id
        else:
            side = self._runtime.side_turn_activity(
                source.side_id, thread_id=source_thread_id, turn_id=source_turn_id,
            )
            if side is not None and side.state is ActiveState.RUNNING:
                reply_turn_id = side.turn_id
        if reply_turn_id is None:
            raise SessionToolError(
                "invalid_call_context", "Only the caller's exact active Netizen Turn can suppress its reply; subagents must not call this tool.",
            )
        self._reply_suppression.suppress(source_thread_id, reply_turn_id, source_turn_id)
        return {
            "suppressed": True,
            "scope": "current_physical_turn",
            "instruction": (
                "Suppression is recorded for this physical Turn. "
                "Finish naturally with an empty final reply. Suppression applies only on normal "
                "completion with an empty final reply; a non-empty final reply is delivered normally. "
                "New input for this Turn clears prior suppression before delivery to the model. "
                "This tool has not ended the Turn. "
                "Existing error handling and completion notifications remain unchanged. "
                "This does not stop a Goal or suppress any later Turn."
            ),
        }

    def _session_tool_binding(self, thread_id: str) -> tuple[ThreadBinding, FeishuScope]:
        binding = self._bindings.find_by_native_thread_id(thread_id)
        if binding is None:
            raise SessionToolError("thread_not_registered", "Codex 会话未登记在当前 Netizen 实例。")
        record = self._bindings.get_scope(binding.scope_key)
        if record.app_id != self._app_id:
            raise SessionToolError("thread_not_registered", "Codex 会话不属于当前飞书应用。")
        return binding, FeishuScope(record.app_id, record.chat_id, record.kind, record.topic_id)

    def _require_session_target(self, thread_id: str) -> tuple[ThreadBinding, FeishuScope]:
        binding, scope = self._session_tool_binding(thread_id)
        if not binding.active:
            raise SessionToolError("target_not_current", "目标会话不是该聊天或话题的当前会话；未自动切换或恢复。", {
                "threadId": thread_id,
                "netizen": {"bindingId": binding.id, "chatId": scope.chat_id,
                            "topicId": scope.topic_id, "isCurrent": False, "accepted": False},
            })
        return binding, scope

    async def _session_agent_card(self, source: SessionSource, prompt: str, *, title: str | None = None) -> OutboundCard:
        # The source is this tool's authenticated caller. Its title is display
        # attribution; neither it nor the prompt can manufacture Feishu @s.
        if isinstance(source, ThreadBinding):
            source_title = source.short_id
            try:
                metadata = await self._runtime.thread_summary(source.native_thread_id)
                source_title = _normalized_thread_text(metadata.name, max_chars=120) or source_title
            except Exception:
                pass
            content = (
                f"来自会话：{source_title}\nCodex 会话 ID：{source.native_thread_id}"
                f"\nNetizen 会话编号：{source.short_id}\n\n{prompt}"
            )
        else:
            content = (
                f"来自临时 Side：{source.side_id[:8]}\nCodex 会话 ID：{source.thread_id}"
                f"\nSide 不支持作为跨会话读取、等待或回信的目标。\n\n{prompt}"
            )
        return OutboundCard(card={
            "schema": "2.0", "config": {"width_mode": "default"},
            "header": {"title": {"tag": "plain_text", "content": title or "会话协作来信"}},
            "body": {"elements": [{"tag": "div", "text": {"tag": "plain_text", "content": content}}]},
        })

    @staticmethod
    def _session_message(source: SessionSource, prompt: str, *, tool_name: str) -> ExternalMessage:
        if isinstance(source, ThreadBinding):
            thread_id = source.native_thread_id
        else:
            thread_id = source.thread_id
            prompt = SIDE_SOURCE_NOTICE + prompt
        return ExternalMessage(
            namespace="codex_tui", tool_name=tool_name,
            content=delegated_prompt(thread_id, prompt),
        )

    def _session_receipt(self, binding: ThreadBinding, scope: FeishuScope, submission: Submission) -> dict[str, Any]:
        result: dict[str, Any] = {"threadId": submission.thread_id, "turnId": submission.turn_id, "netizen": {
            "bindingId": binding.id, "chatId": scope.chat_id, "topicId": scope.topic_id,
            "topicUrl": topic_open_url(scope.chat_id, scope.topic_id) if scope.topic_id else None,
            "isCurrent": binding.active, "accepted": True,
            "disposition": submission.disposition.value,
        }}
        # The native acceptance is already known. A concurrent deletion or a
        # failed local status refresh cannot turn it into an unknown delivery.
        try:
            result["netizen"]["isCurrent"] = self._bindings.get(binding.id).active
        except BindingNotFound:
            result["netizen"].update(isCurrent=False, targetDeleted=True)
        except Exception:
            result["netizen"].update(isCurrent=None, statusWarning="投递已接受；当前会话状态未能刷新。")
        return result

    async def _present_session_submission(self, submission: Submission, origin: SessionAgentOrigin) -> None:
        # Joined input keeps the original Turn's owner, origin and consumer.
        # Its visible anchor already confirms delivery without another card.
        if submission.disposition is SubmitDisposition.STARTED:
            try:
                async with asyncio.timeout(constants.SESSION_RECEIPT_TIMEOUT_SECONDS):
                    await self._present_ordinary_receipt(submission, origin)
            except asyncio.CancelledError:
                # The native input is already accepted. A tool deadline during
                # display must still return its known Thread/Turn receipt.
                # The ordinary presenter releases the consumer in its finally.
                logger.warning("session collaboration receipt display cancelled after acceptance")
            except Exception:
                logger.warning("session collaboration receipt display failed", exc_info=True)

    async def _present_session_failure(
        self, published: TopicMessage | None, card: OutboundCard | None,
        error: SessionToolError,
    ) -> None:
        """Mark the existing request anchor without changing delivery facts."""
        if published is None or card is None:
            return
        accepted = error.details.get("netizen", {}).get("accepted")
        notice = (
            "该协作请求未执行：" + str(error)
            if accepted is False else
            "该协作请求的投递结果未确认，可能已被接收。请先通过 /status 或会话历史核查，不要重复提交。"
        )
        updated = {**card.card, "body": {**card.card["body"], "elements": [
            *card.card["body"]["elements"],
            {"tag": "div", "text": {"tag": "plain_text", "content": notice}},
        ]}}
        try:
            async with asyncio.timeout(constants.SESSION_RECEIPT_TIMEOUT_SECONDS):
                await self._safe_update_card(published.message_id, OutboundCard(card=updated))
        except asyncio.CancelledError:
            # Display cancellation must not replace the original error or its
            # already classified delivery outcome with a generic unknown.
            logger.warning("session collaboration rejection display cancelled")
        except Exception:
            logger.warning("session collaboration rejection display failed", exc_info=True)

    def _session_failure(self, error: Exception, *, binding: ThreadBinding | None,
                         scope: FeishuScope | None, root: TopicMessage | None,
                         submission_attempted: bool) -> SessionToolError:
        known_rejection = isinstance(error, (
            SessionToolError, RuntimeClosed, SteerRace, ThreadStopping, ThreadCompacting,
            ThreadOccupied, ThreadGoalActive, ThreadLifecycleError, ModelCatalogError,
            SkillCatalogError, SkillReferenceError, InvalidRequestError,
            ProjectConflict, ProjectNotFound, BindingNotFound, GoalStateUnknown,
            WorktreeError, WorktreeConflict,
        )) or type(error) is GoalControlError
        # The external entry only reads Goal state before delivery. Preserve
        # unknown outcomes for subclasses such as GoalMutationStateUnknown.
        details: dict[str, Any] = {"netizen": {"accepted": (
            None if submission_attempted and not known_rejection else False
        )}}
        if binding is not None:
            current: bool | None = None
            try:
                binding = self._bindings.get(binding.id)
                current = binding.active
            except BindingNotFound:
                current = False
                details["netizen"]["targetDeleted"] = True
            except Exception:
                pass
            details["threadId"] = binding.native_thread_id
            details["netizen"].update(bindingId=binding.id, isCurrent=current)
        if scope is not None:
            details["netizen"].update(chatId=scope.chat_id, topicId=scope.topic_id)
        if root is not None:
            details["netizen"]["messageId"] = root.message_id
            if scope is None:
                details["netizen"].update(chatId=root.chat_id, topicId=root.thread_id)
        if isinstance(error, SessionToolError):
            details["netizen"].update(error.details.get("netizen", {}))
            details.update({key: value for key, value in error.details.items() if key != "netizen"})
            return SessionToolError(error.code, str(error), details)
        code = "submission_unknown" if details["netizen"]["accepted"] is None else "input_rejected"
        if isinstance(error, ThreadArchived):
            code = "target_archived"
        if isinstance(error, TopicPublishError):
            code = "publishing_unknown" if error.unknown else "publishing_failed"
        return SessionToolError(code, describe_error(error), details)

    async def create_session_thread(
        self, source_thread_id: str, prompt: str, *, title: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Create an ordinary sibling topic after the session tool's admission."""
        binding = root = destination = card = None
        submission_attempted = False
        try:
            source, source_scope = self._session_tool_binding(source_thread_id)
            if title is not None:
                title = " ".join(title.split())
                if not title or len(title) > 120:
                    raise SessionToolError("invalid_title", "会话名称必须为 1 到 120 个字符。")
            selected_model = model or await self._runtime.native_thread_model(source.id)
            catalog = await self._runtime.model_catalog()
            option = next((item for item in catalog.models if selected_model in {item.id, item.model}), None)
            if option is None:
                raise SessionToolError("invalid_model", "指定或继承的模型当前不可用。")
            settings = source.turn_settings
            if settings is None or settings.model_id != option.id:
                settings = BindingTurnSettings(option.id, option.default_effort_id, option.default_service_tier_id)
            catalog.resolve(model_id=settings.model_id, effort_id=settings.effort_id, service_tier_id=settings.service_tier_id)
            project = self._bindings.get_project(source.project_alias)
            self._bindings.require_project_not_deleting(project.alias)
            if not project.enabled:
                raise ProjectConflict("Project 已停用，不能创建会话。")
            cwd = await self._binding_cwd(source)
            registered_project = self._bindings.get_project(project.alias)
            worktree = self._bindings.worktrees.find_by_cwd(
                str(cwd), project_alias=project.alias,
                project_generation=registered_project.generation,
            )
            target = await self._management.validate_chat_target(source_scope.chat_id)
            card = await self._session_agent_card(source, prompt, title=title)
            call_id = uuid.uuid4().hex
            async with self._runtime.track_topic_creation(project.alias):
                self._session_tool_binding(source_thread_id)
                self._bindings.worktrees.require_binding_not_deleting(source.id)
                self._runtime.require_topic_creation_open(project.alias)
                root = await send_topic_message(self._channel, source_scope.chat_id, card,
                    SendOpts(receive_id_type="chat_id", uuid=call_id))
                def require_seed_creation() -> None:
                    self._runtime.require_topic_creation_open(project.alias)
                    if not self._bindings.get_project(project.alias).enabled:
                        raise ProjectConflict("Project 已停用，本次未启动。")
                published = await complete_fresh_topic(
                    self._channel, root,
                    seed_content="协作任务见话题顶部消息；创建完成后可在本话题继续交流或使用 /stop。",
                    seed_uuid=hashlib.sha256((call_id + ":seed").encode()).hexdigest()[:32],
                    before_seed=require_seed_creation,
                )
                if published.thread_id == source_scope.topic_id:
                    raise TopicPublishError("飞书未创建新的普通话题。", unknown=True)
                destination = FeishuScope(self._app_id, source_scope.chat_id, ScopeKind.TOPIC, published.thread_id)
                context_mode = source.message_context_mode if target.chat_kind == "group" else MentionContextMode.CURRENT_ONLY
                anchor = await self._resolve_context_anchor(destination, published.message_id) if (
                    context_mode is MentionContextMode.CATCH_UP
                ) else None
                async with self._scope_coordinator.hold(destination.key):
                    self._runtime.require_topic_creation_open(project.alias)
                    binding = self._bindings.create_channel_binding(
                        scope=destination, project_alias=project.alias,
                        creator_id="session_agent:" + source.id,
                        expected_project_revision=project.revision,
                        turn_settings=settings,
                        task_feedback=replace(source.task_feedback,
                            reaction_pulse_enabled=False, completion_mention_enabled=False),
                        message_context_mode=context_mode, context_anchor=anchor,
                        empty_topic_root_message_id=root.message_id,
                        working_cwd=str(cwd),
                        worktree_id=None if worktree is None else worktree.id,
                    )
                origin = SessionAgentOrigin(self._app_id, destination.chat_id, published.message_id,
                    SessionAgentConversation(destination.chat_id, target.chat_kind, destination.topic_id),
                    source.id, source_thread_id, binding.id, call_id)
                self._runtime.require_topic_creation_open(project.alias)
                submission_attempted = True
                submission = await self._runtime.submit_external(
                    binding=binding, cwd=cwd,
                    message=self._session_message(source, prompt, tool_name="create_thread"),
                    owner_id="session_agent:" + source.id, origin=origin, title=title,
                )
                await self._present_session_submission(submission, origin)
                return self._session_receipt(binding, destination, submission)
        except Exception as error:
            failure = self._session_failure(error, binding=binding, scope=destination,
                                            root=root, submission_attempted=submission_attempted)
            await self._present_session_failure(root, card, failure)
            raise failure from error

    async def send_session_message(
        self, source_thread_id: str, target_thread_id: str, prompt: str, *,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Deliver an admitted session tool call to its still-current exact Thread."""
        source = resolve_session_source(self._bindings, self._runtime, self._app_id, source_thread_id)
        if isinstance(source, ThreadBinding):
            source_binding_id, source_side_id = source.id, None
            owner_id = "session_agent:" + source.id
        else:
            source_binding_id, source_side_id = None, source.side_id
            owner_id = "session_agent:side:" + source.side_id
        if source_thread_id == target_thread_id:
            raise SessionToolError("invalid_target", "不能向当前调用会话自身发送跨会话来信。")
        binding, scope = self._require_session_target(target_thread_id)
        sent = card = None
        submission_attempted = False
        try:
            if model is not None:
                catalog = await self._runtime.model_catalog()
                if not any(model in {item.id, item.model} for item in catalog.models):
                    raise SessionToolError("invalid_model", "指定模型当前不可用。")
            cwd = self._binding_creation_cwd(binding)
            target = await self._management.validate_chat_target(scope.chat_id)
            reply_to = None
            if scope.kind is ScopeKind.TOPIC:
                if self._message_history is None:
                    raise MessageHistoryUnavailable("话题消息读取能力不可用，本次未发送。")
                reply_to = (await self._message_history.resolve_topic_reply_anchor(scope)).message_id
            card = await self._session_agent_card(source, prompt)
            # Keep the existing ordinary-source recheck. Side tool cancellation
            # and closure use the general Side lifecycle, without a send protocol.
            if source_binding_id is not None:
                self._session_tool_binding(source_thread_id)
            self._require_session_target(target_thread_id)
            call_id = uuid.uuid4().hex
            sent = await send_topic_message(self._channel, scope.chat_id, card, SendOpts(
                receive_id_type="chat_id", reply_to=reply_to,
                reply_in_thread=scope.kind is ScopeKind.TOPIC,
                reply_target_gone="fail", uuid=call_id,
            ))
            if sent.thread_id != scope.topic_id or (
                reply_to is not None and (not sent.root_id or sent.parent_id != reply_to)
            ):
                raise TopicPublishError("来信消息不属于指定会话，本次未投递。", unknown=True)
            origin = SessionAgentOrigin(self._app_id, scope.chat_id, sent.message_id,
                SessionAgentConversation(scope.chat_id, target.chat_kind, scope.topic_id),
                source_binding_id, source_thread_id, binding.id, call_id, source_side_id)
            binding, scope = self._require_session_target(target_thread_id)
            submission_attempted = True
            submission = await self._runtime.submit_external(
                binding=binding, cwd=cwd,
                message=self._session_message(source, prompt, tool_name="send_message_to_thread"),
                owner_id=owner_id, origin=origin, model=model,
            )
            await self._present_session_submission(submission, origin)
            return self._session_receipt(binding, scope, submission)
        except Exception as error:
            failure = self._session_failure(error, binding=binding, scope=scope, root=sent,
                                            submission_attempted=submission_attempted)
            await self._present_session_failure(sent, card, failure)
            raise failure from error
