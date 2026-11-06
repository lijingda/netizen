"""Side topic creation, routing, messages and cards."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from lark_channel import (
    SendOpts,
)

from ..admin.port_config import AdminPortConfigurationError
from ..group_access import GroupAccessLimitExceeded
from ..bindings import (
    SideTopicConflict,
    SideTopicNotFound,
    SideTopicRecord,
    SideTopicState,
    ThreadBinding,
)
from ..cards import (
    side_topic_card,
)
from .messages import _nonempty_field, _object_field
from .question_inputs import CardAnswerOrigin
from .topics import TopicPublishError, send_topic_message
from ..cards.status import (
    side_status_card,
)
from ..runtime.contracts import (
    RuntimeClosed,
    SideCloseFailed,
    SideSessionClosing,
    SideSessionNotFound,
    SideSessionState,
    SideStartFailed,
    SideSubmissionAdmission,
    StopDisposition,
    SubmitDisposition,
)
from ..domain import (
    ControlIntent,
    ControlName,
    FeishuScope,
    NativeCapability,
    PromptInput,
    ScopeKind,
)
from ..experience import (
    InvalidInteraction,
    side_command_help,
)
from ..management import (
    CurrentSideTarget,
    SideIdentityMismatch,
)
from ..image_inputs import (
    ImageReference,
    normalized_message_type,
)
from ..prompt_projection import (
    PromptProjectionError,
    MessageInputProjection,
    project_current_message,
)
from ..quoted_context import (
    quoted_message_id,
)
from ..skill_references import InvalidSkillReference, parse_skill_references

from . import constants
from .app_values import (
    _CardReplyTarget,
    _SentMessage,
    _side_send_uuid,
    _side_initial_question_echo,
    _side_reply_target,
    _message_chat_type,
    _message_id,
)
from .base import (
    CHANNEL_LOGGER_NAME,
    SideTopicCreateFailed,
    _git_branch_status_lines,
)
from .prompts import PromptChannel


logger = logging.getLogger(CHANNEL_LOGGER_NAME)


class SideChannel(PromptChannel):
    """Side topic creation, routing, messages and cards."""

    async def refresh_expired_side_cards(
        self,
        records: Sequence[SideTopicRecord],
    ) -> None:
        await asyncio.gather(
            *(
                self._update_side_card(
                    record,
                    notice="服务已重启，本临时对话已过期；历史消息仍可查看。",
                )
                for record in records
                if record.root_message_id is not None
            ),
            return_exceptions=True,
        )

    async def _side_message(
        self,
        message: Any,
        record: SideTopicRecord,
    ) -> None:
        if record.state is SideTopicState.CREATING:
            interaction = self._parse_current_interaction(message, self._scope(message))
            if (
                isinstance(interaction, ControlIntent)
                and interaction.name is ControlName.SIDE
                and interaction.arguments
                and interaction.arguments[0].strip().lower() == "close"
            ):
                if self._current_image_references(message) or self._has_current_files(message):
                    raise InvalidInteraction(
                        "控制命令不能携带图片或文件附件，请拆分后重试。"
                    )
                await self._close_creating_side_from_message(message, record)
                return
            await self._reply(
                message,
                "这个 Side 仍在创建，或创建结果尚未确认；"
                "可发送 /side close 重试清理。",
            )
            return
        if not await self._side_session_available(message, record):
            return

        current_images = self._current_image_references(message)
        interaction = self._parse_current_interaction(message, self._side_scope(record))
        if isinstance(interaction, PromptInput):
            target_id = quoted_message_id(message)
            await self._side_prompt(
                source_message=message,
                prompt=interaction,
                side_id=record.id,
                reply_origin=message,
                quoted_target_id=target_id,
                current_images=current_images,
            )
            return
        if current_images or self._has_current_files(message):
            raise InvalidInteraction("控制命令不能携带图片或文件附件，请拆分后重试。")
        await self._side_control(message, interaction, record)

    async def _side_session_available(self, message: Any, record: SideTopicRecord) -> bool:
        """Shared lifecycle admission for messages and status command aliases."""
        if record.state.terminal:
            labels = {
                SideTopicState.CLOSED: "已结束",
                SideTopicState.EXPIRED: "已过期（服务重启或空闲超时）",
                SideTopicState.FAILED: "创建失败",
            }
            await self._reply(
                message,
                f"这个 Side {labels[record.state]}，不会转成普通会话；"
                "请回到 Parent 会话重新发送 /side。",
            )
            return False
        if record.topic_id is None or record.root_message_id is None:
            raise SideTopicConflict("open Side 缺少话题或根消息标识")
        try:
            self._runtime.side_snapshot(record.id)
        except SideSessionNotFound:
            await self._report_missing_side_session(message, record)
            return False
        return True

    async def _side_prompt(
        self,
        source_message: Any,
        prompt: PromptInput,
        *,
        side_id: str,
        reply_origin: Any,
        quoted_target_id: str | None,
        current_images: tuple[ImageReference, ...] = (),
        source_command: str | None = None,
    ) -> None:
        admission = await self._runtime.capture_side_submission_admission(side_id)
        current = project_current_message(
            source_message,
            expected_message_id=prompt.source_id,
            expected_sender_id=prompt.sender_id,
            message_type=normalized_message_type(source_message),
            content_fidelity=(
                "full_multimodal" if current_images else "full_text"
            ),
            request_text=self._model_task_text(
                source_message, prompt.text, command=source_command,
            ),
        )
        await self._consume_side_prompt(
            side_id=side_id, source_message=source_message, reply_origin=reply_origin,
            current=current, owner_id=prompt.sender_id, skill_names=prompt.skill_names,
            admission=admission, quoted_target_id=quoted_target_id,
            current_images=current_images,
        )

    async def _consume_side_prompt(
        self, *, side_id: str, source_message: Any, reply_origin: Any,
        current: MessageInputProjection, owner_id: str, skill_names: tuple[str, ...],
        admission: SideSubmissionAdmission, quoted_target_id: str | None = None,
        current_images: tuple[ImageReference, ...] = (),
        on_accepted: Callable[[], None] | None = None,
    ) -> None:
        """Use the same Side preparation, exact admission and feedback for all inputs."""
        prepared = await self._input_preparer.prepare(
            source_message=source_message,
            quoted_target_id=quoted_target_id,
            current=current,
            current_images=current_images,
        )
        submit_kwargs: dict[str, Any] = dict(
            side_id=side_id,
            input=prepared.native_input,
            owner_id=owner_id,
            origin=reply_origin,
            admission=admission,
            skill_names=skill_names,
        )
        accepted = False
        try:
            submission = await self._runtime.submit_side(**submit_kwargs)
            accepted = True
            if on_accepted is not None:
                on_accepted()
        except BaseException as error:
            if not accepted:
                await self._discard_unsubmitted_attachments(prepared, error)
            raise
        finally:
            submit_kwargs.pop("input", None)
            prepared = None
        if submission.disposition is SubmitDisposition.STEERED:
            if not await self._safe_add_reaction(reply_origin, constants.STEER_REACTION):
                try:
                    await self._reply(reply_origin, "已接收 Side 调整。")
                except Exception as error:
                    logger.warning(
                        "accepted Side steer presentation failed",
                        extra={
                            "side_id": submission.side_id,
                            "thread_id": submission.thread_id,
                            "turn_id": submission.turn_id,
                            "error_type": type(error).__name__,
                        },
                    )
            return
        release = submission.release_receipt_attempt
        assert release is not None
        try:
            presenters: list[Awaitable[bool]] = [
                self._reactions.start(
                    submission.turn_id,
                    _message_id(reply_origin),
                    pulse_enabled=(
                        submission.task_feedback.reaction_pulse_enabled
                    ),
                )
            ]
            delivery_options = {}
            if isinstance(reply_origin, CardAnswerOrigin):
                delivery_options["reply"] = lambda card: self._reply_to_origin(reply_origin, card)
            presenters.append(
                self._progress_cards.start_side(
                    side_id=submission.side_id,
                    thread_id=submission.thread_id,
                    turn_id=submission.turn_id,
                    origin=reply_origin,
                    progress_enabled=submission.task_feedback.progress_card_enabled,
                    reply_partial=lambda post: self._reply_to_origin(reply_origin, post),
                    **delivery_options,
                )
            )
            if presenters:
                await asyncio.gather(*presenters)
        except Exception as error:
            logger.warning(
                "accepted Side prompt presentation failed",
                extra={
                    "side_id": submission.side_id,
                    "thread_id": submission.thread_id,
                    "turn_id": submission.turn_id,
                    "error_type": type(error).__name__,
                },
            )
        finally:
            release()

    async def _command_enable(self, message: Any, intent: ControlIntent) -> None:
        if _message_chat_type(message) == "p2p":
            await self._reply(message, "单聊无需启用。")
            return
        try:
            if self._is_instance_admin is None:
                raise ValueError("administrator check is unavailable")
            is_admin = self._is_instance_admin(
                self._app_id, getattr(getattr(message, "sender", None), "open_id", None),
            )
        except Exception as error:
            logger.warning("group enable administrator check failed", extra={"error_type": type(error).__name__})
            await self._reply(message, "暂时无法确认管理员身份，本群未启用。")
            return
        if not is_admin:
            await self._reply(message, "只有实例管理员可以启用本群。")
            return
        try:
            rule, added = await self._management.enable_group_access(
                self._group_access, intent.scope.chat_id,
            )
        except Exception as error:
            if isinstance(error, AdminPortConfigurationError):
                reason = "实例配置文件已变化"
            elif isinstance(error, GroupAccessLimitExceeded):
                reason = "已达到 1000 个群聊的上限"
            else:
                reason = "暂时无法保存群聊准入配置"
            logger.warning("group enable failed", extra={"error_type": type(error).__name__, "chat_id": intent.scope.chat_id})
            await self._reply(message, reason + "，请稍后重试或在 Admin 中启用。")
            return
        if rule.mode == "all":
            result = "已将本群加入名单" if added else "本群已在名单中"
            await self._reply(message, result + "；当前为全部群聊模式，切换为限制模式后生效。")
        else:
            await self._reply(message, "已启用本群，之后的消息会正常执行。" if added else "本群已启用。")

    async def _side_control(
        self,
        message: Any,
        intent: ControlIntent,
        record: SideTopicRecord,
    ) -> None:
        if intent.name is ControlName.ENABLE:
            await self._command_enable(message, intent)
            return
        if intent.name is ControlName.USAGE:
            await self._usage(message)
            return
        if intent.name is ControlName.ADMIN:
            await self._reply(message, self._admin_entry())
            return
        if intent.name in {ControlName.MENU, ControlName.HELP}:
            await self._reply(
                message,
                side_command_help(
                    requires_mention=record.requires_mention,
                    available_capabilities=self._runtime.available_capabilities,
                ),
            )
            return
        if intent.name is ControlName.STATUS:
            snapshot = self._runtime.side_snapshot(record.id)
            turn_state = (
                snapshot.turn_state.value
                if snapshot.turn_state is not None
                else "idle"
            )
            git_branch_lines = await _git_branch_status_lines(snapshot.cwd)
            await self._reply(
                message,
                side_status_card(
                    scope=self._side_scope(record),
                    side_id=record.id,
                    short_id=record.short_id,
                    parent_short_id=record.parent_binding_id[:8],
                    state=snapshot.state.value,
                    turn_state=turn_state,
                    git_branch_lines=git_branch_lines,
                ),
            )
            return
        if intent.name is ControlName.STOP:
            snapshot = self._runtime.side_snapshot(record.id)
            if snapshot.turn_id is None:
                await self._reply(message, "当前 Side 没有正在执行的 Turn。")
                return
            acknowledged = False

            async def acknowledge() -> None:
                nonlocal acknowledged
                await self._reply(
                    message,
                    "正在中断当前 Side Turn；Side 话题仍会保留。",
                )
                acknowledged = True

            disposition = await self._management.stop_side(
                target=CurrentSideTarget(
                    side_id=record.id, app_id=record.app_id, chat_id=record.chat_id,
                    topic_id=record.topic_id, root_message_id=record.root_message_id,
                ),
                acknowledge=acknowledge,
            )
            if disposition is StopDisposition.NOT_RUNNING and not acknowledged:
                await self._reply(message, "当前 Side Turn 恰好已经结束。")
            elif disposition is StopDisposition.STOPPING and not acknowledged:
                await self._reply(
                    message,
                    "当前 Side 正在结束；如清理曾失败，可再次发送 /side close。",
                )
            return
        if intent.name is ControlName.SIDE:
            argument = intent.arguments[0].strip().lower() if intent.arguments else ""
            if argument != "close":
                raise InvalidInteraction(
                    "Side 中不能再创建嵌套 Side；使用 /side close 结束当前 Side。"
                )
            await self._close_side_from_message(message, record)
            return
        if intent.name is ControlName.RELEASE:
            raise InvalidInteraction(
                "Side 中不开放 /release；请使用 /side close 结束并取消 Side 订阅。"
            )
        raise InvalidInteraction(
            "该命令在 Side 中不可用。发送 /help 查看 Side 支持的操作。"
        )

    async def _close_side_from_message(
        self,
        message: Any,
        record: SideTopicRecord,
    ) -> None:
        try:
            closed = await self._management.close_side(
                target=CurrentSideTarget(
                    side_id=record.id,
                    app_id=record.app_id,
                    chat_id=record.chat_id,
                    topic_id=record.topic_id,
                    root_message_id=record.root_message_id,
                )
            )
        except SideIdentityMismatch as error:
            raise InvalidInteraction("Side 身份已变化，本次未执行。") from error
        except SideCloseFailed as error:
            current = self._bindings.get_side_topic(record.id)
            await self._update_side_card(
                current,
                notice=str(error),
                notice_is_error=True,
            )
            raise
        if closed.missing_runtime_session:
            await self._report_missing_side_session(message, closed.record)
            return
        outcome = closed.outcome
        if outcome is None:
            await self._reply(message, "Side 已经结束。")
            return
        await self._reply(
            message,
            (
                "Side 已结束，并已请求清理已登记的后台终端。"
                if outcome.state is SideTopicState.CLOSED
                else f"Side 已进入 {outcome.state.value}。"
            ),
        )

    async def _close_creating_side_from_message(
        self,
        message: Any,
        record: SideTopicRecord,
    ) -> None:
        try:
            closed = await self._management.close_side(
                target=CurrentSideTarget(
                    side_id=record.id,
                    app_id=record.app_id,
                    chat_id=record.chat_id,
                    topic_id=record.topic_id,
                    root_message_id=record.root_message_id,
                )
            )
        except SideIdentityMismatch as error:
            raise InvalidInteraction("Side 身份已变化，本次未执行。") from error
        except SideCloseFailed as error:
            current = self._bindings.get_side_topic(record.id)
            await self._update_side_card(
                current,
                notice=str(error),
                notice_is_error=True,
            )
            raise
        if closed.missing_runtime_session:
            await self._update_side_card(
                closed.record,
                notice="Side 创建未完成，且没有可重试的 Codex 会话。",
                notice_is_error=True,
            )
            await self._reply(
                message,
                f"Side 当前状态为 {closed.record.state.value}，不会转成普通会话。",
            )
            return
        await self._reply(
            message,
            "未完成的 Side 已清理并保留失败墓碑，不会转成普通会话。",
        )

    async def _report_missing_side_session(
        self,
        message: Any,
        record: SideTopicRecord,
    ) -> None:
        current = self._bindings.get_side_topic(record.id)
        if current.state is SideTopicState.OPEN:
            try:
                current = self._bindings.transition_side_topic(
                    current.id,
                    SideTopicState.EXPIRED,
                )
            except SideTopicConflict:
                current = self._bindings.get_side_topic(current.id)
        if current.state is SideTopicState.EXPIRED:
            notice = "服务内已没有对应 Side Session；该话题已过期。"
            reply = (
                "这个 Side 已随服务重启失效；"
                "请回到 Parent 会话重新发送 /side。"
            )
        else:
            notice = f"Side 已进入 {current.state.value}。"
            reply = (
                f"这个 Side 已进入 {current.state.value}，不会转成普通会话。"
            )
        await self._update_side_card(current, notice=notice)
        await self._reply(message, reply)

    async def _create_side(
        self,
        message: Any,
        intent: ControlIntent,
    ) -> None:
        if intent.arguments and intent.arguments[0].strip().lower() == "close":
            await self._reply(
                message,
                "当前不在 Side 话题中；请在要结束的 Side 话题里发送 /side close。",
            )
            return
        initial_text = intent.arguments[0].strip() if intent.arguments else None
        initial_skill_names: tuple[str, ...] = ()
        if initial_text is not None:
            try:
                initial_skill_names = parse_skill_references(initial_text)
            except InvalidSkillReference as error:
                raise InvalidInteraction(str(error)) from error
            if (
                initial_skill_names
                and NativeCapability.SKILLS
                not in self._runtime.available_capabilities
            ):
                raise InvalidInteraction(
                    "当前 Codex Skills 发现功能不可用，$skill 引用未执行。"
                )
        source_message_id = _message_id(message)
        requires_mention = _message_chat_type(message) != "p2p"
        existing: SideTopicRecord | None = None
        binding: ThreadBinding | None = None
        async with self._scope_coordinator.hold(intent.scope.key):
            existing = self._bindings.side_topic_for_source(
                app_id=self._app_id,
                source_message_id=source_message_id,
            )
            if existing is not None and (
                existing.chat_id != intent.scope.chat_id
                or existing.creator_id != intent.sender_id
                or existing.requires_mention is not requires_mention
            ):
                raise SideTopicConflict(
                    "Side source message is already reserved with different identity"
                )
            if existing is None:
                binding = self._bindings.active_binding(intent.scope.key)
                if binding is not None:
                    existing = self._bindings.create_side_topic(
                        app_id=self._app_id,
                        chat_id=intent.scope.chat_id,
                        source_message_id=source_message_id,
                        parent_binding_id=binding.id,
                        creator_id=intent.sender_id,
                        requires_mention=requires_mention,
                    )
        if existing is None or binding is None:
            if existing is not None:
                await self._reply_existing_side(message, existing)
                return
            await self._reply_no_current_binding(message, intent.scope)
            return
        record = existing
        root: _SentMessage | None = None
        topic_id: str | None = None
        origin: _CardReplyTarget | None = None
        try:
            cwd = await self._binding_cwd(binding)
            await self._runtime.create_side(
                side_id=record.id,
                binding=binding,
                cwd=cwd,
                creator_id=intent.sender_id,
            )
            root_scope = FeishuScope(
                self._app_id,
                record.chat_id,
                ScopeKind.GROUP if record.requires_mention else ScopeKind.DIRECT,
            )
            card_context = await self._side_card_context(
                record,
                creator_name=_nonempty_field(_object_field(message, "sender"), "display_name"),
            )
            root_card = side_topic_card(
                scope=root_scope,
                side_id=record.id,
                parent_short_id=binding.short_id,
                creator_id=record.creator_id,
                created_at=record.created_at,
                state=SideTopicState.CREATING,
                context=card_context,
                requires_mention=record.requires_mention,
                # The fresh root establishes the topic. Optional profile
                # rendering is only attempted once the route is confirmed.
                show_creator_profile=False,
            )
            root = await self._send_side_message(
                record.chat_id,
                root_card,
                SendOpts(
                    receive_id_type="chat_id",
                    uuid=_side_send_uuid(constants.SIDE_ROOT_UUID_PREFIX, record.id),
                ),
            )
            record = self._bindings.set_side_topic_root(record.id, root.message_id)
            if (
                root.parent_id is not None
                or root.root_id not in {None, root.message_id}
            ):
                raise SideTopicCreateFailed(
                    "飞书返回的 Side 根消息不是新话题根消息，本次创建已停止。"
                )
            if root.thread_id is not None:
                topic_id = root.thread_id
                origin = _side_reply_target(root, topic_id)
            if root.thread_id is None or initial_text is not None:
                seed_content = (
                    _side_initial_question_echo(initial_text)
                    if initial_text is not None
                    else constants.SIDE_EMPTY_TOPIC_PROMPT
                )
                seed = await self._send_side_message(
                    record.chat_id,
                    seed_content,
                    SendOpts(
                        receive_id_type="chat_id",
                        reply_to=root.message_id,
                        reply_in_thread=True,
                        reply_target_gone="fail",
                        uuid=_side_send_uuid(constants.SIDE_SEED_UUID_PREFIX, record.id),
                    ),
                )
                if (
                    seed.thread_id is None
                    or seed.root_id != root.message_id
                    or seed.parent_id != root.message_id
                    or (
                        root.thread_id is not None
                        and seed.thread_id != root.thread_id
                    )
                ):
                    raise SideTopicCreateFailed(
                        "飞书没有确认 Side 首轮消息与新话题的关系，本次创建已停止。"
                    )
                topic_id = seed.thread_id
                origin = _side_reply_target(seed, topic_id)
            if intent.scope.topic_id == topic_id:
                raise SideTopicCreateFailed(
                    "飞书把 Side 留在了原话题中，未创建同级话题。"
                )
            record = self._bindings.set_side_topic_topic(record.id, topic_id)
            await self._runtime.attach_side_topic(
                side_id=record.id,
                topic_id=topic_id,
                root_message_id=root.message_id,
            )
            record = self._bindings.open_side_topic(record.id, topic_id)
        except BaseException as error:
            await self._compensate_side_creation(record, error)
            raise

        assert root is not None
        await self._update_side_card(
            record,
            context=card_context,
        )
        if initial_text is not None:
            assert origin is not None
            await self._start_initial_side_prompt(
                record=record,
                source_message=message,
                reply_origin=origin,
                text=initial_text,
                sender_id=intent.sender_id,
                skill_names=initial_skill_names,
            )

    async def _reply_existing_side(
        self,
        message: Any,
        record: SideTopicRecord,
    ) -> None:
        if record.state is SideTopicState.OPEN:
            return
        elif record.state is SideTopicState.CREATING:
            await self._reply(
                message,
                "这条 /side 已在创建中，或创建结果尚未确认；未重复 fork。",
            )
        else:
            await self._reply(
                message,
                f"这条 /side 已处理过，当前状态为 {record.state.value}；"
                "为避免重复副作用，不会再次创建。",
            )

    async def _start_initial_side_prompt(
        self,
        *,
        record: SideTopicRecord,
        source_message: Any,
        reply_origin: _CardReplyTarget,
        text: str,
        sender_id: str,
        skill_names: tuple[str, ...],
    ) -> None:
        try:
            prompt = PromptInput(
                scope=self._side_scope(record),
                source_id=_message_id(source_message),
                sender_id=sender_id,
                text=text,
                skill_names=skill_names,
            )
            await self._side_prompt(
                source_message=source_message,
                prompt=prompt,
                side_id=record.id,
                reply_origin=reply_origin,
                quoted_target_id=None,
                source_command="side",
            )
        except InvalidInteraction as error:
            await self._reply(reply_origin, str(error))
        except PromptProjectionError as error:
            await self._update_side_card(
                self._bindings.get_side_topic(record.id),
                notice=str(error),
                notice_is_error=True,
            )
            await self._reply(reply_origin, str(error))
        except (
            RuntimeClosed,
            SideSessionClosing,
            SideStartFailed,
        ) as error:
            await self._update_side_card(
                self._bindings.get_side_topic(record.id),
                notice=str(error),
                notice_is_error=True,
            )
            await self._reply(reply_origin, str(error))
        except Exception as error:
            logger.exception(
                "initial Side prompt failed",
                extra={"side_id": record.id, "error_type": type(error).__name__},
            )
            await self._reply(
                reply_origin,
                "Side 已创建，但首轮问题未执行；请在本话题重新发送。",
            )

    async def _compensate_side_creation(
        self,
        record: SideTopicRecord,
        cause: BaseException,
    ) -> None:
        try:
            snapshot = self._runtime.side_snapshot(record.id)
        except SideSessionNotFound:
            try:
                self._bindings.transition_side_topic(
                    record.id,
                    SideTopicState.FAILED,
                )
            except (SideTopicConflict, SideTopicNotFound):
                pass
        else:
            if snapshot.state is SideSessionState.CLOSING:
                logger.error(
                    "Side creation compensation remains closing",
                    extra={"side_id": record.id},
                )
            else:
                try:
                    await self._runtime.close_side(
                        record.id,
                        state=SideTopicState.FAILED,
                    )
                except Exception:
                    logger.exception(
                        "Side creation compensation remains unconfirmed",
                        extra={"side_id": record.id},
                    )
        try:
            current = self._bindings.get_side_topic(record.id)
        except SideTopicNotFound:
            return
        if current.root_message_id is not None:
            detail = str(cause).strip() or type(cause).__name__
            await self._update_side_card(
                current,
                notice=f"Side 未创建完成：{detail[:300]}",
                notice_is_error=True,
            )

    async def _send_side_message(
        self,
        chat_id: str,
        content: Any,
        opts: SendOpts,
    ) -> _SentMessage:
        try:
            sent = await send_topic_message(self._channel, chat_id, content, opts)
        except TopicPublishError as error:
            raise SideTopicCreateFailed("Side " + str(error)) from error
        return _SentMessage(sent.message_id, sent.chat_id, sent.thread_id, sent.root_id, sent.parent_id)
