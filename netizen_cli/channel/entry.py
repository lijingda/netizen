"""Channel message and card-action entry points."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from lark_channel import SendOpts

from ..image_inputs import normalized_message_type
from ..quoted_context import quoted_message_id
from ..ordinary_sessions import OrdinaryInputRejected
from .base import CHANNEL_LOGGER_NAME
from ..bindings import (
    BindingContextRevisionConflict,
    BindingNotFound,
)
from ..cards import (
    CardActionError,
    is_turn_file_action,
)
from ..cards.scheduled import (
    is_schedule_card_action,
)
from ..cards.questions import (
    is_question_card_action,
)
from ..backends import BackendUnsupported, OperationId
from ..cards.controls import permitted_form_fields
from ..cards.defaults import is_defaults_card_action
from ..cards.status import (
    is_status_command,
)
from ..cards.fork import (
    is_fork_card_action,
)
from ..runtime.contracts import (
    TerminalStateUnknown,
    ContextAnchorRequired,
    GoalNotFound,
    GoalNotMaterialized,
    GoalStateUnknown,
    RuntimeClosed,
    SideCloseFailed,
    ThreadCompacting,
    ThreadGoalActive,
    ThreadLifecycleError,
    ThreadRunningConfiguration,
    ThreadStopping,
    ThreadOccupied,
    TurnStartFailed,
)
from ..domain import (
    CardControlIntent,
    CardControlName,
    ControlIntent,
    ControlName,
    PromptInput,
)
from ..experience import (
    InvalidInteraction,
    require_command,
)
from ..model_settings import ModelCatalogError
from ..management import (
    NativeThreadMissing,
)
from ..projects import ProjectError
from ..message_history import (
    MessageHistoryError,
)
from ..sdk_gap_adapter import GoalControlError

from .app_values import (
    _CardCallbackContext,
    _CardReplyConversation,
    _CardReplyTarget,
    _message_chat_type,
    _inbound_root_message_id,
    _body_text,
    authenticated_sender,
    _channel_bot_open_id,
)
from .commands import CommandChannel
from .messages import public_chat_kind
from ..group_access import GROUP_ACCESS_NOTICE


logger = logging.getLogger(CHANNEL_LOGGER_NAME)


class ChannelEntry(CommandChannel):
    """Channel message and card-action entry points."""

    async def handle_message(self, message: Any) -> None:
        group_rule, _ = self._group_access.snapshot()
        scope = self._scope(message)
        try:
            local = self._local_sessions
            if self._policy.owner_only:
                if _message_chat_type(message) != "p2p" and not getattr(message, "mentioned_bot", False):
                    return
                sender = authenticated_sender(message, self._app_id)
                if sender != self._policy.owner_id:
                    bare_help = (
                        sender is not None and group_rule.allows(scope.chat_id, _message_chat_type(message))
                        and normalized_message_type(message) in {"text", "post"}
                        and _body_text(message, bot_open_id=_channel_bot_open_id(self._channel)).strip() == "/help"
                        and not quoted_message_id(message)
                        and not getattr(getattr(message, "reply", None), "message_id", None)
                        and not getattr(message, "resources", None)
                        and not self._has_current_files(message) and not self._current_image_references(message)
                    )
                    await self._reply(message, self._policy.sender_refusal(sender, bare_help=bare_help) or self._help())
                    return
            if not group_rule.allows(scope.chat_id, _message_chat_type(message)):
                if bool(getattr(message, "mentioned_bot", False)):
                    # Inspect only current message text. Parsing is local and
                    # never prepares quotes, history, images or attachments.
                    text = _body_text(message, bot_open_id=_channel_bot_open_id(self._channel)).strip()
                    head = text[1:].lstrip().split(maxsplit=1) if text.startswith("/") else []
                    if head and head[0].lower() == "enable":
                        interaction = self._parse_current_interaction(message, scope)
                        if isinstance(interaction, ControlIntent) and interaction.name is ControlName.ENABLE:
                            if self._current_image_references(message) or self._has_current_files(message):
                                raise InvalidInteraction("控制命令不能携带图片或文件附件，请拆分后重试。")
                            await self._command_enable(message, interaction)
                            return
                    await self._group_access_notice(message, scope.chat_id)
                return
            side = self._side_route(message, scope)
            if side is not None:
                if side.requires_mention and not bool(
                    getattr(message, "mentioned_bot", False)
                ):
                    return
                self._policy.require(OperationId.SIDE)
                await self._side_message(message, side)
                return

            direct = _message_chat_type(message) == "p2p"
            if not direct and not bool(getattr(message, "mentioned_bot", False)):
                return
            pending = None if not self._policy.allows(OperationId.SCHEDULE) else self._bindings.schedules.pending_route(
                app_id=self._app_id,
                chat_id=scope.chat_id,
                topic_id=scope.topic_id,
                root_message_id=_inbound_root_message_id(message),
            )
            if pending is not None:
                await self._reply(message, "定时任务正在启动，请稍后重发。")
                return
            current_images = self._current_image_references(message)
            interaction = self._parse_current_interaction(message, scope)
            if isinstance(interaction, PromptInput):
                if local is not None:
                    local.require_admission()
                await self._prompt(
                    message,
                    interaction,
                    current_images=current_images,
                )
            else:
                if current_images or self._has_current_files(message):
                    raise InvalidInteraction("控制命令不能携带图片或文件附件，请拆分后重试。")
                await self._control(message, interaction)
        except Exception as error:
            await self._report_input_error(message, error)

    def _check_local_card(self, value: Any, form: Any) -> None:
        if isinstance(value, Mapping):
            if is_status_command(value):
                if value.get("target_kind") == "side":
                    raise CardActionError("Claude 试点暂不支持【Side】。")
                require_command(self._policy, str(value.get("command", "")))
            elif value.get("intent"):
                try:
                    name = CardControlName(value["intent"])
                except (TypeError, ValueError):
                    raise CardActionError("未知卡片动作。") from None
                self._policy.require_card(name.value)
        if isinstance(form, Mapping):
            if any(isinstance(key, str) and key.startswith("rename_name_v1__") for key in form):
                self._policy.require_card(CardControlName.RENAME_BINDING.value)
            for prefix in ("new", "config", "defaults_session"):
                if any(isinstance(key, str) and key.startswith(prefix + "_") for key in form):
                    permitted_form_fields(form, prefix, self._session_form)

    async def handle_card_action(self, event: Any) -> None:
        group_rule, _ = self._group_access.snapshot()
        chat_id = str(getattr(event, "chat_id", "") or "")
        local = self._local_sessions
        refusal = self._policy.sender_refusal(authenticated_sender(event, self._app_id, card=True)) if self._policy.owner_only else None
        action = getattr(event, "action", None)
        value = getattr(action, "value", None)
        form = getattr(action, "form_value", None)
        if refusal:
            message_id = getattr(event, "message_id", "")
            if message_id and chat_id:
                await self._reply(_CardReplyTarget(message_id, message_id, chat_id, _CardReplyConversation(None)), refusal)
            return
        known_direct = False
        if not group_rule.allows(chat_id, None):
            try:
                known_direct = self._bindings.has_direct_scope(self._app_id, chat_id)
            except Exception as error:
                logger.warning(
                    "group admission could not read direct scope",
                    extra={"chat_id": chat_id, "error_type": type(error).__name__},
                )
        if not group_rule.allows(chat_id, None) and not known_direct:
            chat_info = None
            chat_kind = None
            try:
                chat_info = await self._channel.get_chat_info(chat_id)
                chat_kind = public_chat_kind(chat_info)
            except Exception as diagnostic_error:
                logger.warning(
                    "group admission could not read card chat kind",
                    extra={"chat_id": chat_id, "error_type": type(diagnostic_error).__name__},
                )
            if chat_kind is None:
                logger.warning("group admission rejected unknown card chat kind", extra={"chat_id": chat_id})
            if not group_rule.allows(chat_id, chat_kind):
                message_id = str(getattr(event, "message_id", "") or "")
                if chat_id and message_id:
                    # The public callback has no topic metadata. Do not decode
                    # the card or fetch its message just to send this notice.
                    target = _CardReplyTarget(
                        id=message_id, message_id=message_id, chat_id=chat_id,
                        conversation=_CardReplyConversation(thread_id=None),
                    )
                    await self._group_access_notice(
                        target, chat_id,
                        reply_in_thread=getattr(chat_info, "chat_mode", None) == "topic",
                    )
                return
        handler = None
        operation = None
        if is_status_command(value):
            handler = self._handle_status_command
        elif is_fork_card_action(value, form):
            handler, operation = self._handle_fork_card_action, OperationId.FORK
        elif is_defaults_card_action(value, form):
            handler = self._handle_defaults_card_action
        elif is_question_card_action(value, form):
            handler, operation = self._handle_question_card_action, OperationId.QUESTIONS
        elif is_schedule_card_action(value, form):
            handler, operation = self._handle_schedule_card_action, OperationId.SCHEDULE
        elif is_turn_file_action(value):
            handler, operation = self._handle_turn_file_card_action, OperationId.FILES
        try:
            if operation is not None:
                if operation is OperationId.FORK:
                    self._policy.require(operation, "card")
                    if local is not None:
                        local.require_admission()
                else:
                    self._policy.require(operation)
            elif local is not None:
                self._check_local_card(value, form)
                local.require_admission()
        except (BackendUnsupported, CardActionError, InvalidInteraction, OrdinaryInputRejected) as error:
            message_id = getattr(event, "message_id", "")
            if message_id and chat_id:
                await self._reply(_CardReplyTarget(message_id, message_id, chat_id, _CardReplyConversation(None)), str(error))
            return
        if handler is not None:
            await handler(event)
            return
        intent: CardControlIntent | None = None
        context = _CardCallbackContext()
        message_id = str(getattr(event, "message_id", "") or "")
        callback_chat_id = str(getattr(event, "chat_id", "") or "")
        try:
            intent = await self._decode_card_event(event, context)
            context.scope = intent.scope
            if not self._policy.allows(OperationId.SIDE) and self._bindings.side_topic_for_message(
                app_id=self._app_id, chat_id=intent.scope.chat_id, topic_id=intent.scope.topic_id, root_message_id=message_id,
            ) is not None:
                raise CardActionError("Claude 试点暂不支持【Side】。")
            await self._card_control(intent)
        except (
            BackendUnsupported,
            CardActionError,
            BindingContextRevisionConflict,
            BindingNotFound,
            ContextAnchorRequired,
            GoalControlError,
            GoalNotFound,
            GoalNotMaterialized,
            GoalStateUnknown,
            ModelCatalogError,
            MessageHistoryError,
            NativeThreadMissing,
            OrdinaryInputRejected,
            TerminalStateUnknown,
            ProjectError,
            RuntimeClosed,
            SideCloseFailed,
            ThreadCompacting,
            ThreadGoalActive,
            ThreadLifecycleError,
            ThreadRunningConfiguration,
            ThreadStopping,
            ThreadOccupied,
            TurnStartFailed,
        ) as error:
            await self._report_card_action_error(
                intent, message_id, callback_chat_id, error, scope=context.scope,
            )
        except Exception as error:
            logger.exception(
                "card interaction failed",
                extra={"error_type": type(error).__name__},
            )
            await self._report_card_action_error(
                intent, message_id, callback_chat_id, error, scope=context.scope, unexpected=True,
            )

    async def _group_access_notice(
        self, message: Any, chat_id: str, *, reply_in_thread: bool | None = None,
    ) -> None:
        try:
            # reply() defaults reply_to to the target message and infers the thread for
            # inbound messages; cards pass reply_in_thread explicitly.
            result = await self._channel.reply(message, GROUP_ACCESS_NOTICE, SendOpts(
                reply_target_gone="fail", reply_in_thread=reply_in_thread,
            ))
            if not result.success:
                logger.warning("group admission notice was rejected", extra={"chat_id": chat_id})
        except Exception as diagnostic_error:
            logger.warning(
                "group admission notice was not delivered",
                extra={"chat_id": chat_id, "error_type": type(diagnostic_error).__name__},
            )
