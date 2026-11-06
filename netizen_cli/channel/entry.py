"""Channel message and card-action entry points."""

from __future__ import annotations

import logging
from typing import Any

from lark_channel import SendOpts

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
from ..cards.defaults import is_defaults_card_action
from ..cards.status import (
    is_status_command,
)
from ..cards.fork import (
    is_fork_card_action,
)
from ..runtime.contracts import (
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
    ControlIntent,
    ControlName,
    PromptInput,
)
from ..experience import (
    InvalidInteraction,
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
                await self._side_message(message, side)
                return

            direct = _message_chat_type(message) == "p2p"
            if not direct and not bool(getattr(message, "mentioned_bot", False)):
                return
            pending = self._bindings.schedules.pending_route(
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

    async def handle_card_action(self, event: Any) -> None:
        group_rule, _ = self._group_access.snapshot()
        chat_id = str(getattr(event, "chat_id", "") or "")
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
        action = getattr(event, "action", None)
        if is_status_command(getattr(action, "value", None)):
            await self._handle_status_command(event)
            return
        if is_fork_card_action(
            getattr(action, "value", None), getattr(action, "form_value", None),
        ):
            await self._handle_fork_card_action(event)
            return
        if is_defaults_card_action(
            getattr(action, "value", None), getattr(action, "form_value", None),
        ):
            await self._handle_defaults_card_action(event)
            return
        if is_question_card_action(
            getattr(action, "value", None), getattr(action, "form_value", None),
        ):
            await self._handle_question_card_action(event)
            return
        if is_schedule_card_action(
            getattr(action, "value", None), getattr(action, "form_value", None),
        ):
            await self._handle_schedule_card_action(event)
            return
        if is_turn_file_action(getattr(action, "value", None)):
            await self._handle_turn_file_card_action(event)
            return
        intent: CardControlIntent | None = None
        context = _CardCallbackContext()
        message_id = str(getattr(event, "message_id", "") or "")
        callback_chat_id = str(getattr(event, "chat_id", "") or "")
        try:
            intent = await self._decode_card_event(event, context)
            context.scope = intent.scope
            await self._card_control(intent)
        except (
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
