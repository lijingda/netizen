"""Feishu product controls over the native Codex runtime.

``ChannelApplication`` is assembled from a linear chain of layers in
``netizen_cli.channel``. Each layer may call only itself and the layers below it,
so a dependency on a later feature is a type error rather than a convention:

    base           shared state, scope, card and scheduled-delivery utilities
    replies        replies, error reports and Goal management cards
    presentation   completion Result, Files, Goal receipts and reminders
    prompts        ordinary prompt admission and automatic session creation
    sides          Side topic creation, routing and cards
    forks          persistent fork cards and creation
    session_tools  session collaboration tool entries
    schedules      Scheduled Run dispatch and the schedule manager card
    questions      native question cards and answers
    session_cards  session, project and new-binding cards
    turn_files     Turn file card actions
    card_controls  card actions, one handler per CardControlName
    commands       slash commands, one handler per ControlName
    entry          message and card-action entry points

Shared mutable state is declared once on ``ChannelState`` and initialized here.
"""

from __future__ import annotations

from pathlib import Path
from collections.abc import Callable


from .bindings import (
    BindingStore,
)
from .chat_avatars import ChatAvatarImages
from .cards import (
    TurnFileCardLimitError,
    turn_progress_card,
)
from .channel.input_preparation import MessageInputPreparer
from .attachments import AttachmentStore
from .channel.ports import ReplyChannel
from .channel.reactions import _ReactionController
from .channel.reply_presenter import (
    _ReplyCardPresenter,
)
from .channel.reply_suppression import ReplySuppression
from .codex_runtime import CodexRuntime
from .domain import (
    TurnFileActionIntent,
    TurnFileActionName,
)
from .management import (
    InstanceManagementService,
)
from .projects import ProjectRegistry
from .group_access import GroupAccessService
from .message_history import (
    MessageHistoryReader,
)
from .turn_files import (
    TurnFileError,
    turn_patch_summary,
)

from .channel.base import (
    logger,
    SideTopicCreateFailed,
    _normalized_thread_preview,
)
from .channel.app_values import (
    _ScheduledCompletion,
    _RenameNameGeneration,
    _side_send_uuid,
    _validate_turn_file_reply,
    _side_initial_question_echo,
    _is_feishu_card_action_lock,
    _body_text,
    _goal_reply_activity,
    _outcome_completion_mention_user_id,
)
from .channel.entry import ChannelEntry


__all__ = [
    "ChannelApplication",
    "SideTopicCreateFailed",
    "_goal_reply_activity",
    "_normalized_thread_preview",
    "_outcome_completion_mention_user_id",
    "TurnFileActionIntent",
    "TurnFileActionName",
    "TurnFileCardLimitError",
    "TurnFileError",
    "_body_text",
    "_is_feishu_card_action_lock",
    "_side_initial_question_echo",
    "_side_send_uuid",
    "_validate_turn_file_reply",
    "logger",
    "turn_patch_summary",
    "turn_progress_card",
]


class ChannelApplication(ChannelEntry):
    """Route Feishu messages and card actions to the runtime and present results."""

    def __init__(
        self,
        *,
        app_id: str,
        channel: ReplyChannel,
        runtime: CodexRuntime,
        bindings: BindingStore,
        projects: ProjectRegistry,
        management: InstanceManagementService,
        message_history: MessageHistoryReader | None = None,
        admin_urls: tuple[str, ...] = (),
        admin_loopback_only: bool | None = None,
        instance_root: Path | None = None,
        attachment_store: AttachmentStore | None = None,
        group_access: GroupAccessService | None = None,
        is_instance_admin: Callable[[str, str], bool] | None = None,
    ) -> None:
        self._group_access = group_access or GroupAccessService()
        self._is_instance_admin = is_instance_admin
        self._app_id = app_id
        self._channel = channel
        self._chat_avatars = ChatAvatarImages(channel)
        self._runtime = runtime
        self._bindings = bindings
        self._projects = projects
        self._message_history = message_history
        self._admin_urls = admin_urls
        self._admin_loopback_only = admin_loopback_only
        self._instance_root = instance_root
        self._input_preparer = MessageInputPreparer(channel=channel, attachment_store=attachment_store)
        self._management = management
        self._management.enable_schedules(app_id=app_id, chat_info=channel)
        self._management.enable_defaults(app_id=app_id, chat_info=channel)
        self._scope_coordinator = management.scope_coordinator
        # Only the final send writes the aggregate Scheduled Run receipt. Keep
        # this completion's receipt through pruning of the persisted Run.
        self._scheduled_completions: dict[str, _ScheduledCompletion] = {}
        self._rename_name_generations: dict[str, _RenameNameGeneration] = {}
        self._reactions = _ReactionController(channel)
        self._reply_suppression = ReplySuppression()
        self._progress_cards = _ReplyCardPresenter(channel, runtime)
        runtime.set_completion_handler(self.handle_completion)
        runtime.set_question_handler(self.handle_questions)
        runtime.set_input_received_handler(self._reply_suppression.discard_physical_turn)
