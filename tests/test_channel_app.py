from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lark_channel import (
    Conversation,
    FeishuChannelErrorCode,
    Identity,
    ImageContent,
    InboundMessage,
    InteractiveContent,
    MediaSource,
    OutboundCard,
    OutboundFile,
    OutboundImage,
    PostContent,
    QuotedContext,
    ResourceDescriptor,
    SendError,
    SendResult,
    TextContent,
)
from openai_codex import ImageInput, TextInput
from openai_codex.types import ThreadItem

from netizen import channel_app
from netizen.turn_patch_children import TaskPatchChildren, TurnPatchBatch
from netizen.bindings import (
    BindingNotFound,
    BindingStore,
    BindingTaskFeedback,
    BindingTurnSettings,
    SideTopicState,
)
from netizen.cards import (
    CardActionError,
    decode_turn_file_action,
    goal_card,
    goal_generation,
    reply_card,
)
from netizen.channel_app import ChannelApplication, SideTopicCreateFailed
from netizen.codex_runtime import (
    ActiveGoalSnapshot,
    ActiveState,
    ActiveTurnSnapshot,
    BindingRuntimeSnapshot,
    CompactSubmission,
    ContextWindowUsage,
    GoalActivitySnapshot,
    GoalFinalizationStatus,
    GoalOperationState,
    GoalOutcome,
    GoalSubmission,
    GoalStateUnknown,
    NativeThreadMetadata,
    ReleaseDisposition,
    SideCloseFailed,
    SideLifecycleOutcome,
    SideSessionNotFound,
    SideSessionSnapshot,
    SideSessionState,
    SideSubmission,
    SideSubmissionAdmission,
    SideTurnActivitySnapshot,
    SideTurnOutcome,
    SubmissionAdmission,
    StopDisposition,
    Submission,
    SubmitDisposition,
    SteerRace,
    TerminalCleanupFailed,
    TerminalStateUnknown,
    ThreadDeleteUnavailable,
    ThreadArchived,
    ThreadBackgroundTerminalsActive,
    ThreadActivityDiscardedOutcome,
    ThreadLifecycleError,
    ThreadReleaseStateUnknown,
    ThreadRunningConfiguration,
    ThreadSubscriptionSnapshot,
    ThreadSubscriptionState,
    TurnProgressSnapshot,
    TurnActivitySnapshot,
    TurnOutcome,
    TurnObservationUnavailable,
    TurnObservationUnavailableOutcome,
)
from netizen.domain import (
    FeishuScope,
    MentionContextMode,
    MessageContextAnchor,
    NativeCapability,
    ReplyCardFileItem,
    ReplyCardFilesModule,
    ReplyCardProjection,
    ReplyCardResultModule,
    ScopeKind,
)
from netizen.model_settings import (
    EffortOption,
    ModelCatalog,
    ModelOption,
    ServiceTierOption,
    TurnModelSettings,
)
from netizen.message_history import (
    MessageHistoryRef,
    MessageHistoryStats,
    MessageHistoryUnavailable,
    MessageHistoryWindow,
)
from netizen.projects import ProjectRegistry
from netizen.sdk_gap_adapter import GoalSnapshot, GoalStatus
from netizen.turn_activity import (
    TurnActivityEntrySnapshot,
    TurnActivityKind,
    TurnActivityStatus,
)
from netizen.turn_plan_observer import (
    TurnPlanStepSnapshot,
    TurnPlanStepState,
)
PNG = b"\x89PNG\r\n\x1a\nchannel-test"
PULSE_ON = BindingTaskFeedback(reaction_pulse_enabled=True)


class FakeMessage:
    def __init__(
        self,
        text: str,
        *,
        message_id: str,
        sender_id: str = "ou_user",
        display_name: str = "Current User",
        union_id: str | None = None,
        user_id: str | None = None,
        sender_type: str = "user",
        is_bot: bool = False,
        chat_id: str = "oc_direct",
        chat_type: str = "p2p",
        thread_id: str | None = None,
        mentioned_bot: bool = True,
        raw_content_type: str = "text",
        resources: list[object] | None = None,
        mentions: list[object] | None = None,
        content: object | None = None,
        reply_id: str | None = None,
        raw: dict[str, object] | None = None,
        create_time: int = 123,
    ) -> None:
        self.id = message_id
        self.create_time = create_time
        self.body_text = text
        self.sender = SimpleNamespace(
            open_id=sender_id,
            display_name=display_name,
            union_id=union_id,
            user_id=user_id,
            sender_type=sender_type,
            is_bot=is_bot,
        )
        self.conversation = SimpleNamespace(
            chat_id=chat_id,
            chat_type=chat_type,
            thread_id=thread_id,
        )
        self.mentioned_bot = mentioned_bot
        self.resources = resources or []
        self.raw_content_type = raw_content_type
        self.mentions = mentions or []
        self.content = content
        self.content_text = text
        self.reply = (
            SimpleNamespace(message_id=reply_id) if reply_id is not None else None
        )
        self.raw = raw or {}


class FakeChannel:
    def __init__(self) -> None:
        self.replies: list[tuple[str, object]] = []
        self.reply_targets: list[object] = []
        self.reply_results: list[object | BaseException] = []
        self.send_calls: list[tuple[str, object, object]] = []
        self.send_results: list[object | BaseException] = []
        self.reactions: list[tuple[str, str]] = []
        self.reaction_operations: list[tuple[str, str, str]] = []
        self.reaction_removals: list[tuple[str, str]] = []
        self.reaction_remove_attempted = asyncio.Event()
        self._next_reaction_id = 1
        self.updates: list[tuple[str, dict[str, object]]] = []
        self.fetched_messages: dict[str, dict[str, object]] = {}
        self.inbound_messages: dict[str, object | None | BaseException] = {}
        self.quoted_contexts: dict[str, object | None | BaseException] = {}
        self.fetch_inbound_calls: list[str] = []
        self.fetch_quoted_calls: list[str] = []
        self.chat_types: dict[str, str] = {}
        self.chat_info_calls: list[str] = []
        self.resource_bodies: dict[
            tuple[str, str],
            bytes | None | BaseException | asyncio.Event,
        ] = {}
        self.download_resource_calls: list[tuple[str, str, str | None]] = []
        self.fail_card_updates = False
        self.card_update_success = True
        self.card_update_results: list[object | BaseException] = []
        self.fail_once_reaction_on: str | None = None
        self.fail_once_reaction_remove = False
        self.bot_identity = SimpleNamespace(open_id="ou_bot", name="椰羊")

    async def reply(self, message: FakeMessage, content: object, opts=None) -> object:
        self.reply_targets.append(message)
        self.replies.append((message.id, content))
        if self.reply_results:
            result = self.reply_results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return object()

    async def send(self, to: str, content: object, opts=None) -> object:
        self.send_calls.append((to, content, opts))
        if not self.send_results:
            raise AssertionError("unexpected channel.send call")
        result = self.send_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    async def add_reaction(self, message_id: str, emoji_type: str) -> object:
        self.reactions.append((message_id, emoji_type))
        self.reaction_operations.append(("add", message_id, emoji_type))
        if self.fail_once_reaction_on == emoji_type:
            self.fail_once_reaction_on = None
            raise RuntimeError("reaction failed")
        reaction_id = f"reaction-{self._next_reaction_id}"
        self._next_reaction_id += 1
        return SimpleNamespace(
            success=True,
            raw={"data": {"reaction_id": reaction_id}},
        )

    async def remove_reaction(
        self,
        message_id: str,
        reaction_id: str,
    ) -> object:
        self.reaction_removals.append((message_id, reaction_id))
        self.reaction_operations.append(("remove", message_id, reaction_id))
        self.reaction_remove_attempted.set()
        if self.fail_once_reaction_remove:
            self.fail_once_reaction_remove = False
            return SimpleNamespace(success=False)
        return SimpleNamespace(success=True)

    async def update_card(self, message_id: str, card: dict[str, object]) -> object:
        self.updates.append((message_id, card))
        if self.card_update_results:
            result = self.card_update_results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        if self.fail_card_updates:
            raise RuntimeError("card update failed")
        return SimpleNamespace(success=self.card_update_success)

    async def fetch_message(self, message_id: str) -> dict[str, object]:
        return self.fetched_messages[message_id]

    async def fetch_inbound_message(self, message_id: str) -> object | None:
        self.fetch_inbound_calls.append(message_id)
        result = self.inbound_messages.get(message_id)
        if isinstance(result, BaseException):
            raise result
        return result

    async def fetch_quoted_context(self, message_id: str) -> object | None:
        self.fetch_quoted_calls.append(message_id)
        result = self.quoted_contexts.get(message_id)
        if isinstance(result, BaseException):
            raise result
        return result

    async def download_resource(
        self,
        file_key: str,
        resource_type: str = "image",
        message_id: str | None = None,
    ) -> bytes | None:
        self.download_resource_calls.append((file_key, resource_type, message_id))
        result = self.resource_bodies.get((str(message_id), file_key))
        if isinstance(result, BaseException):
            raise result
        if isinstance(result, asyncio.Event):
            await result.wait()
            return PNG
        return result

    async def get_chat_info(self, chat_id: str) -> object:
        self.chat_info_calls.append(chat_id)
        return SimpleNamespace(
            chat_type="unknown",
            chat_mode=self.chat_types.get(chat_id, "group"),
        )


class FakeMessageHistory:
    def __init__(self) -> None:
        self.resolve_calls: list[tuple[FeishuScope, str]] = []
        self.read_calls: list[
            tuple[FeishuScope, MessageContextAnchor, str]
        ] = []
        self.anchors: dict[str, MessageContextAnchor] = {}
        self.window: MessageHistoryWindow | None = None

    async def resolve_anchor(
        self,
        scope: FeishuScope,
        message_id: str,
    ) -> MessageContextAnchor:
        self.resolve_calls.append((scope, message_id))
        return self.anchors.get(
            message_id,
            MessageContextAnchor(message_id, 1_000),
        )

    async def read_window(
        self,
        scope: FeishuScope,
        lower: MessageContextAnchor,
        upper_id: str,
    ) -> MessageHistoryWindow:
        self.read_calls.append((scope, lower, upper_id))
        if self.window is None:
            raise AssertionError("unexpected history read")
        return self.window


def quoted_inbound(
    *,
    message_id: str = "om_quoted",
    chat_id: str = "oc_direct",
    content: object | None = None,
    content_text: str = "quoted text",
    raw_content_type: str = "text",
    resources: list[ResourceDescriptor] | None = None,
) -> InboundMessage:
    return InboundMessage(
        id=message_id,
        create_time=123,
        conversation=Conversation(chat_id=chat_id, chat_type="p2p"),
        sender=Identity(open_id="ou_quoted", display_name="Quoted User"),
        content=content or TextContent(text=content_text),
        raw={"message_id": message_id},
        content_text=content_text,
        resources=resources or [],
        body_text=content_text,
        raw_content_type=raw_content_type,
    )


def plain_prompt_projection(native_input: object) -> tuple[str, dict[str, object]]:
    if isinstance(native_input, list):
        prompt_text = native_input[-1].text
    else:
        prompt_text = native_input
    assert isinstance(prompt_text, str)
    request_text, trailer = prompt_text.split(
        "\n\n<feishu_current_message_context>\n",
        1,
    )
    metadata_json, closing = trailer.rsplit(
        "\n</feishu_current_message_context>",
        1,
    )
    assert closing == ""
    return request_text, json.loads(metadata_json)


def native_goal(
    status: GoalStatus = GoalStatus.ACTIVE,
    *,
    created_at: int = 1,
) -> GoalSnapshot:
    return GoalSnapshot(
        thread_id="native-one",
        objective="ship safely",
        status=status,
        token_budget=None,
        tokens_used=10,
        time_used_seconds=2,
        created_at=created_at,
        updated_at=2,
    )


def sent_result(
    message_id: str,
    *,
    chat_id: str,
    thread_id: str | None = None,
    root_id: str | None = None,
    parent_id: str | None = None,
    success: bool = True,
    code: int = 0,
) -> object:
    return SimpleNamespace(
        success=success,
        message_id=message_id,
        chunk_ids=(),
        raw={
            "code": code,
            "data": {
                "message_id": message_id,
                "chat_id": chat_id,
                "thread_id": thread_id,
                "root_id": root_id,
                "parent_id": parent_id,
            },
        },
    )


def retryable_sent_result(*, code: int = 999_999) -> object:
    return SimpleNamespace(
        success=False,
        message_id=None,
        chunk_ids=(),
        error=SimpleNamespace(retryable=True),
        raw={"code": code, "data": None},
    )


def failed_reply_result(
    *,
    code: int,
    message: str,
    retryable: bool = False,
) -> object:
    return SimpleNamespace(
        success=False,
        message_id=None,
        chunk_ids=(),
        error=SimpleNamespace(
            retryable=retryable,
            raw_code=code,
            hint=message,
        ),
        raw={"code": code, "msg": message, "data": None},
    )


def card_action_lock_result(
    *,
    outer_code: int = 230099,
    inner_code: int = 11310,
) -> object:
    message = (
        "Failed to create card content, "
        f"ext=ErrCode: {inner_code}; ErrMsg: 可变平台文案;"
    )
    return SendResult.fail(
        SendError(
            code=FeishuChannelErrorCode.FORMAT_ERROR,
            retryable=False,
            raw_code=outer_code,
            hint=message,
        ),
        raw={"code": outer_code, "msg": message, "data": None},
    )


def file_change_item(*paths: str) -> ThreadItem:
    return ThreadItem.model_validate(
        {
            "type": "fileChange",
            "id": "file-change",
            "status": "completed",
            "changes": [
                {"path": path, "diff": "", "kind": {"type": "add"}}
                for path in paths
            ],
        }
    )


def image_generation_item(path: Path) -> ThreadItem:
    return ThreadItem.model_validate(
        {
            "type": "imageGeneration",
            "id": "image-generation",
            "status": "completed",
            "result": "generated",
            "savedPath": str(path),
        }
    )


def completed_turn_result(
    *items: ThreadItem,
    final_response: str | None = "done",
) -> object:
    return SimpleNamespace(
        final_response=final_response,
        status=SimpleNamespace(value="completed"),
        items=list(items),
    )


def turn_activity_snapshot(
    *,
    binding_id: str,
    revision: int = 1,
    thread_id: str = "native-one",
    turn_id: str = "turn-one",
    state: ActiveState = ActiveState.RUNNING,
    steps: tuple[TurnPlanStepSnapshot, ...] = (),
) -> TurnActivitySnapshot:
    return TurnActivitySnapshot(
        binding_id=binding_id,
        thread_id=thread_id,
        turn_id=turn_id,
        revision=revision,
        state=state,
        steer_count=0,
        plan_available=True,
        plan_generated=bool(steps),
        plan_may_be_stale=False,
        steps=steps,
    )


def side_turn_activity_snapshot(
    *,
    side_id: str,
    revision: int = 1,
    thread_id: str = "native-side-1",
    turn_id: str = "side-turn-1",
    state: ActiveState = ActiveState.RUNNING,
    steps: tuple[TurnPlanStepSnapshot, ...] = (),
) -> SideTurnActivitySnapshot:
    return SideTurnActivitySnapshot(
        side_id=side_id,
        thread_id=thread_id,
        turn_id=turn_id,
        revision=revision,
        state=state,
        steer_count=0,
        plan_available=True,
        plan_generated=bool(steps),
        plan_may_be_stale=False,
        steps=steps,
    )


def goal_activity_snapshot(
    *,
    binding_id: str,
    revision: int = 1,
    steps: tuple[TurnPlanStepSnapshot, ...] = (),
    commentary: tuple[TurnActivityEntrySnapshot, ...] = (),
    operations: tuple[TurnActivityEntrySnapshot, ...] = (),
) -> GoalActivitySnapshot:
    return GoalActivitySnapshot(
        binding_id=binding_id,
        thread_id="native-one",
        logical_turn_id="goal-one",
        physical_turn_id="goal-turn-final",
        revision=revision,
        state=GoalOperationState.RUNNING,
        plan_available=True,
        plan_generated=bool(steps),
        steps=steps,
        commentary=commentary,
        operations=operations,
    )


class StubRuntime:
    def __init__(self) -> None:
        self.available_capabilities = frozenset()
        self.completion = None
        self.submit_calls: list[dict[str, object]] = []
        self.submission: Submission | None = None
        self.active: dict[str, ActiveTurnSnapshot] = {}
        self.activity_revisions: dict[str, int] = {}
        self.stop_result = StopDisposition.REQUESTED
        self.compacting: set[str] = set()
        self.compact_calls: list[dict[str, object]] = []
        self.compact_submission: CompactSubmission | None = None
        self.capture_calls: list[str] = []
        self.capture_error: BaseException | None = None
        self.admission: SubmissionAdmission | None = None
        self.catalog = ModelCatalog(
            models=(
                ModelOption(
                    id="future-model",
                    model="gpt-future-codex",
                    display_name="GPT Future",
                    description="future model",
                    is_default=True,
                    default_effort_id="ultra",
                    default_service_tier_id="priority-v2",
                    efforts=(
                        EffortOption("low", "low", "low-wire"),
                        EffortOption("ultra", "ultra", "ultra-wire"),
                    ),
                    service_tiers=(
                        ServiceTierOption(
                            "priority-v2",
                            "Fast v2",
                            "future fast tier",
                        ),
                    ),
                ),
            )
        )
        self.model_catalog_calls = 0
        self.resolve_model_settings_calls: list[dict[str, str]] = []
        self.configure_settings_calls: list[dict[str, object]] = []
        self.binding_store: BindingStore | None = None
        self.model_catalog_error: Exception | None = None
        self.goal_snapshot_value: GoalSnapshot | None = None
        self.goal_snapshot_calls: list[str] = []
        self.active_goals: dict[str, object] = {}
        self.goal_submission: GoalSubmission | None = None
        self.start_goal_calls: list[dict[str, object]] = []
        self.resume_goal_calls: list[dict[str, object]] = []
        self.clear_goal_calls: list[object] = []
        self.clear_goal_result = True
        self.clear_goal_error: BaseException | None = None
        self.goal_snapshot_after_stop: GoalSnapshot | None = None
        self.thread_metadata_values: dict[str, NativeThreadMetadata] = {}
        self.archived_thread_metadata_values: dict[str, NativeThreadMetadata] = {}
        self.thread_metadata_calls: list[tuple[str, ...]] = []
        self.archived_thread_metadata_calls: list[tuple[str, ...]] = []
        self.thread_metadata_error: Exception | None = None
        self.context_window_usage_values: dict[str, ContextWindowUsage] = {}
        self.context_window_usage_calls: list[str] = []
        self.turn_progress_values: dict[str, TurnProgressSnapshot] = {}
        self.turn_activity_values: dict[str, TurnActivitySnapshot] = {}
        self.turn_activity_calls: list[tuple[str, str | None, str | None, bool]] = []
        self.side_turn_activity_values: dict[str, SideTurnActivitySnapshot] = {}
        self.side_turn_activity_calls: list[
            tuple[str, str | None, str | None, bool]
        ] = []
        self.goal_activity_values: dict[str, GoalActivitySnapshot] = {}
        self.goal_activity_calls: list[
            tuple[str, str | None, str | None, bool]
        ] = []
        self.stop_calls: list[str] = []
        self.recheck_calls: list[tuple[str, int, str]] = []
        self.lifecycle_states: dict[str, object] = {}
        self.rename_binding_calls: list[tuple[str, str]] = []
        self.archive_binding_calls: list[str] = []
        self.archive_binding_error: BaseException | None = None
        self.delete_binding_calls: list[str] = []
        self.delete_binding_error: BaseException | None = None
        self.unarchive_binding_calls: list[str] = []
        self.enforce_active_submission = False
        self.create_side_calls: list[dict[str, object]] = []
        self.attach_side_calls: list[dict[str, str]] = []
        self.capture_side_calls: list[str] = []
        self.submit_side_calls: list[dict[str, object]] = []
        self.close_side_calls: list[tuple[str, SideTopicState]] = []
        self.stop_side_calls: list[str] = []
        self.side_snapshots: dict[str, SideSessionSnapshot] = {}
        self.side_submission: SideSubmission | None = None
        self.side_feedback: dict[
            str,
            tuple[BindingTaskFeedback, int],
        ] = {}
        self.side_stop_result = StopDisposition.REQUESTED
        self.side_close_error: BaseException | None = None
        self.active_binding_change_calls: list[tuple[str | None, str | None]] = []
        self.subscription_snapshots: dict[str, ThreadSubscriptionSnapshot] = {}
        self.release_disposition = ReleaseDisposition.RELEASED
        self.release_binding_calls: list[str] = []
        self.release_error: BaseException | None = None

    def set_completion_handler(self, handler) -> None:
        self.completion = handler

    async def active_binding_changed(
        self,
        previous_binding_id: str | None,
        current_binding_id: str | None,
    ) -> None:
        self.active_binding_change_calls.append(
            (previous_binding_id, current_binding_id)
        )

    async def binding_pointer_changed(
        self,
        previous_binding_id: str | None,
        current_binding_id: str | None,
    ) -> None:
        await self.active_binding_changed(previous_binding_id, current_binding_id)

    def thread_subscription_snapshot(
        self,
        binding_id: str,
    ) -> ThreadSubscriptionSnapshot | None:
        return self.subscription_snapshots.get(binding_id)

    async def release_binding(self, binding) -> ReleaseDisposition:
        self.release_binding_calls.append(binding.id)
        if self.release_error is not None:
            raise self.release_error
        return self.release_disposition

    async def release_exact(self, binding_id: str) -> ReleaseDisposition:
        assert self.binding_store is not None
        return await self.release_binding(self.binding_store.get(binding_id))

    async def submit(self, **kwargs) -> Submission:
        if self.enforce_active_submission:
            assert self.binding_store is not None
            binding = self.binding_store.get(kwargs["binding"].id)
            if not binding.active:
                raise SteerRace(
                    "准备本条消息期间 active 会话已切换，本条消息未执行，请重新发送。"
                )
        self.submit_calls.append(kwargs)
        assert self.submission is not None
        return self.submission

    async def capture_submission_admission(
        self,
        binding_id: str,
    ) -> SubmissionAdmission:
        self.capture_calls.append(binding_id)
        if self.capture_error is not None:
            raise self.capture_error
        if self.admission is not None:
            return self.admission
        assert self.binding_store is not None
        binding = self.binding_store.get(binding_id)
        return SubmissionAdmission(
            binding_id,
            0,
            None,
            None,
            binding.settings_revision,
            binding.context_revision,
            binding.feedback_revision,
        )

    async def model_catalog(self) -> ModelCatalog:
        self.model_catalog_calls += 1
        if self.model_catalog_error is not None:
            raise self.model_catalog_error
        return self.catalog

    async def thread_metadata(
        self,
        thread_ids: tuple[str, ...],
        *,
        archived: bool = False,
    ) -> dict[str, NativeThreadMetadata]:
        calls = (
            self.archived_thread_metadata_calls
            if archived
            else self.thread_metadata_calls
        )
        calls.append(thread_ids)
        if self.thread_metadata_error is not None:
            raise self.thread_metadata_error
        values = (
            self.archived_thread_metadata_values
            if archived
            else self.thread_metadata_values
        )
        return {
            thread_id: values[thread_id]
            for thread_id in thread_ids
            if thread_id in values
        }

    def context_window_usage(self, binding_id: str) -> ContextWindowUsage | None:
        self.context_window_usage_calls.append(binding_id)
        return self.context_window_usage_values.get(binding_id)

    def turn_progress(self, binding_id: str) -> TurnProgressSnapshot | None:
        return self.turn_progress_values.get(binding_id)

    def turn_activity(
        self,
        binding_id: str,
        *,
        thread_id: str | None = None,
        turn_id: str | None = None,
        refresh_plan: bool = False,
    ) -> TurnActivitySnapshot | None:
        self.turn_activity_calls.append(
            (binding_id, thread_id, turn_id, refresh_plan)
        )
        snapshot = self.turn_activity_values.get(binding_id)
        if snapshot is None:
            return None
        if thread_id is not None and snapshot.thread_id != thread_id:
            return None
        if turn_id is not None and snapshot.turn_id != turn_id:
            return None
        return snapshot

    def goal_activity(
        self,
        binding_id: str,
        *,
        thread_id: str | None = None,
        logical_turn_id: str | None = None,
        refresh_plan: bool = False,
    ) -> GoalActivitySnapshot | None:
        self.goal_activity_calls.append(
            (binding_id, thread_id, logical_turn_id, refresh_plan)
        )
        snapshot = self.goal_activity_values.get(binding_id)
        if snapshot is None:
            return None
        if thread_id is not None and snapshot.thread_id != thread_id:
            return None
        if (
            logical_turn_id is not None
            and snapshot.logical_turn_id != logical_turn_id
        ):
            return None
        return snapshot

    def side_turn_activity(
        self,
        side_id: str,
        *,
        thread_id: str | None = None,
        turn_id: str | None = None,
        refresh_plan: bool = False,
    ) -> SideTurnActivitySnapshot | None:
        self.side_turn_activity_calls.append(
            (side_id, thread_id, turn_id, refresh_plan)
        )
        snapshot = self.side_turn_activity_values.get(side_id)
        if snapshot is None:
            return None
        if thread_id is not None and snapshot.thread_id != thread_id:
            return None
        if turn_id is not None and snapshot.turn_id != turn_id:
            return None
        return snapshot

    async def thread_is_archived(self, thread_id: str) -> bool:
        return thread_id in self.archived_thread_metadata_values

    async def activate_exact(
        self,
        binding_id: str,
        *,
        context_anchor: MessageContextAnchor | None = None,
    ):
        assert self.binding_store is not None
        binding = self.binding_store.get(binding_id)
        if binding.native_thread_id in self.archived_thread_metadata_values:
            raise ThreadArchived("该会话已归档，请先恢复后再切换。")
        return self.binding_store.activate(
            scope_key=binding.scope_key,
            binding_id=binding.id,
            context_anchor=context_anchor,
        )

    def lifecycle_state(self, binding_id: str):
        return self.lifecycle_states.get(binding_id)

    def binding_runtime_snapshot(self, binding_id: str) -> BindingRuntimeSnapshot:
        return BindingRuntimeSnapshot(
            binding_id=binding_id,
            activity_revision=self.activity_revisions.get(binding_id, 0),
            turn=self.active.get(binding_id),
            goal=self.active_goals.get(binding_id),
            compacting=binding_id in self.compacting,
            lifecycle=self.lifecycle_states.get(binding_id),
            subscription=self.subscription_snapshots.get(binding_id),
            context_window_usage=self.context_window_usage_values.get(binding_id),
        )

    def _require_activity(
        self,
        binding_id: str,
        *,
        expected_activity_revision: int,
        expected_turn_id: str | None,
    ) -> None:
        active = self.active.get(binding_id)
        actual_turn_id = active.turn_id if active is not None else None
        if (
            self.activity_revisions.get(binding_id, 0)
            != expected_activity_revision
            or actual_turn_id != expected_turn_id
        ):
            raise ThreadLifecycleError("会话运行状态已经变化。")

    async def rename_binding(self, binding, name: str) -> str:
        normalized = " ".join(name.split())
        self.rename_binding_calls.append((binding.id, normalized))
        return normalized

    async def rename_exact(self, binding_id: str, name: str) -> str:
        assert self.binding_store is not None
        return await self.rename_binding(self.binding_store.get(binding_id), name)

    async def archive_binding(self, binding):
        if self.archive_binding_error is not None:
            raise self.archive_binding_error
        self.archive_binding_calls.append(binding.id)
        self.active.pop(binding.id, None)
        assert self.binding_store is not None
        if binding.native_thread_id is not None:
            metadata = self.thread_metadata_values.pop(
                binding.native_thread_id,
                NativeThreadMetadata(
                    binding.native_thread_id,
                    None,
                    "",
                ),
            )
            self.archived_thread_metadata_values[binding.native_thread_id] = (
                metadata
            )
        return self.binding_store.deactivate_if_active(
            scope_key=binding.scope_key,
            binding_id=binding.id,
        )

    async def archive_exact(self, binding_id: str):
        assert self.binding_store is not None
        return await self.archive_binding(self.binding_store.get(binding_id))

    async def delete_binding(self, binding):
        if self.delete_binding_error is not None:
            raise self.delete_binding_error
        assert self.binding_store is not None
        current = self.binding_store.get(binding.id)
        self.delete_binding_calls.append(binding.id)
        self.active.pop(binding.id, None)
        return self.binding_store.delete_binding(binding.id)

    async def delete_exact(
        self,
        binding_id: str,
        *,
        expected_native_thread_id: str | None,
    ):
        assert self.binding_store is not None
        binding = self.binding_store.get(binding_id)
        if binding.native_thread_id != expected_native_thread_id:
            raise ThreadLifecycleError("会话的原生 Thread 已变化。")
        return await self.delete_binding(binding)

    async def delete_archived_exact(
        self,
        binding_id: str,
        *,
        expected_native_thread_id: str,
    ):
        assert self.binding_store is not None
        binding = self.binding_store.get(binding_id)
        if (
            binding.native_thread_id != expected_native_thread_id
            or expected_native_thread_id
            not in self.archived_thread_metadata_values
        ):
            raise ThreadLifecycleError("归档会话已变化。")
        self.archived_thread_metadata_values.pop(expected_native_thread_id)
        return await self.delete_binding(binding)

    async def delete_lazy_exact(self, binding_id: str):
        assert self.binding_store is not None
        binding = self.binding_store.get(binding_id)
        if binding.native_thread_id is not None:
            raise ThreadDeleteUnavailable(
                "已有原生历史的会话不能走 Lazy 删除。"
            )
        return await self.delete_binding(binding)

    async def unarchive_binding(self, binding):
        self.unarchive_binding_calls.append(binding.id)
        assert self.binding_store is not None
        return self.binding_store.activate(
            scope_key=binding.scope_key,
            binding_id=binding.id,
        )

    async def restore_exact(self, binding_id: str):
        self.unarchive_binding_calls.append(binding_id)
        assert self.binding_store is not None
        return self.binding_store.get(binding_id)

    async def restore_as_current_exact(
        self,
        binding_id: str,
        *,
        context_anchor: MessageContextAnchor | None = None,
    ):
        assert self.binding_store is not None
        binding = self.binding_store.get(binding_id)
        self.unarchive_binding_calls.append(binding.id)
        return self.binding_store.activate(
            scope_key=binding.scope_key,
            binding_id=binding.id,
            context_anchor=context_anchor,
        )

    async def resolve_model_settings(
        self,
        *,
        model_id: str,
        effort_id: str,
        service_tier_id: str,
    ) -> TurnModelSettings:
        values = {
            "model_id": model_id,
            "effort_id": effort_id,
            "service_tier_id": service_tier_id,
        }
        self.resolve_model_settings_calls.append(values)
        if self.model_catalog_error is not None:
            raise self.model_catalog_error
        return self.catalog.resolve(**values)

    async def configure_turn_settings(
        self,
        *,
        binding_id: str,
        expected_revision: int,
        settings: BindingTurnSettings | None,
    ):
        values = {
            "binding_id": binding_id,
            "expected_revision": expected_revision,
            "settings": settings,
        }
        self.configure_settings_calls.append(values)
        assert self.binding_store is not None
        return self.binding_store.set_turn_settings(**values)

    async def configure_exact(
        self,
        *,
        binding_id: str,
        expected_revision: int,
        settings: BindingTurnSettings | None,
    ):
        return await self.configure_turn_settings(
            binding_id=binding_id,
            expected_revision=expected_revision,
            settings=settings,
        )

    async def configure_context_exact(
        self,
        *,
        binding_id: str,
        expected_settings_revision: int,
        expected_context_revision: int,
        expected_feedback_revision: int,
        settings: BindingTurnSettings | None,
        task_feedback: BindingTaskFeedback,
        message_context_mode: MentionContextMode,
        context_anchor: MessageContextAnchor | None,
    ):
        self.configure_settings_calls.append(
            {
                "binding_id": binding_id,
                "expected_revision": expected_settings_revision,
                "expected_context_revision": expected_context_revision,
                "expected_feedback_revision": expected_feedback_revision,
                "settings": settings,
                "task_feedback": task_feedback,
                "message_context_mode": message_context_mode,
                "context_anchor": context_anchor,
            }
        )
        assert self.binding_store is not None
        return self.binding_store.set_configuration(
            binding_id=binding_id,
            expected_settings_revision=expected_settings_revision,
            expected_context_revision=expected_context_revision,
            expected_feedback_revision=expected_feedback_revision,
            settings=settings,
            task_feedback=task_feedback,
            message_context_mode=message_context_mode,
            context_anchor=context_anchor,
        )

    def active_turn(self, binding_id: str) -> ActiveTurnSnapshot | None:
        return self.active.get(binding_id)

    def active_goal(self, binding_id: str):
        return self.active_goals.get(binding_id)

    async def goal_snapshot(self, binding):
        self.goal_snapshot_calls.append(binding.id)
        return self.goal_snapshot_value

    async def start_goal(self, **kwargs):
        self.start_goal_calls.append(kwargs)
        assert self.goal_submission is not None
        return self.goal_submission

    async def resume_goal(self, **kwargs):
        self.resume_goal_calls.append(kwargs)
        assert self.goal_submission is not None
        return self.goal_submission

    async def clear_goal(self, binding, **kwargs):
        self.clear_goal_calls.append(binding)
        if self.clear_goal_error is not None:
            raise self.clear_goal_error
        return self.clear_goal_result

    def is_compacting(self, binding_id: str) -> bool:
        return binding_id in self.compacting

    async def compact(self, **kwargs) -> CompactSubmission:
        self.compact_calls.append(kwargs)
        assert self.compact_submission is not None
        return self.compact_submission

    async def stop(
        self,
        binding_id: str,
        *,
        acknowledge=None,
    ) -> StopDisposition:
        self.stop_calls.append(binding_id)
        if acknowledge is not None and self.stop_result is not StopDisposition.COMPACTING:
            await acknowledge()
        if self.goal_snapshot_after_stop is not None:
            self.goal_snapshot_value = self.goal_snapshot_after_stop
        return self.stop_result

    async def stop_exact(
        self,
        binding_id: str,
        *,
        acknowledge=None,
        expected_activity_revision: int | None = None,
        expected_turn_id: str | None = None,
    ) -> StopDisposition:
        if expected_activity_revision is not None:
            self._require_activity(
                binding_id,
                expected_activity_revision=expected_activity_revision,
                expected_turn_id=expected_turn_id,
            )
        return await self.stop(binding_id, acknowledge=acknowledge)

    async def recheck_turn_exact(
        self,
        binding_id: str,
        *,
        expected_activity_revision: int,
        expected_turn_id: str,
    ) -> ActiveTurnSnapshot:
        self.recheck_calls.append(
            (binding_id, expected_activity_revision, expected_turn_id)
        )
        self._require_activity(
            binding_id,
            expected_activity_revision=expected_activity_revision,
            expected_turn_id=expected_turn_id,
        )
        return self.active[binding_id]

    async def create_side(self, **kwargs) -> SideSessionSnapshot:
        self.create_side_calls.append(kwargs)
        binding = kwargs["binding"]
        snapshot = SideSessionSnapshot(
            side_id=kwargs["side_id"],
            parent_binding_id=binding.id,
            parent_thread_id=binding.native_thread_id,
            thread_id=f"native-side-{len(self.create_side_calls)}",
            project_alias=binding.project_alias,
            cwd=Path(kwargs["cwd"]),
            creator_id=kwargs["creator_id"],
            state=SideSessionState.OPEN,
            topic_id=None,
            root_message_id=None,
            turn_id=None,
            turn_state=None,
            last_activity=1.0,
        )
        self.side_snapshots[snapshot.side_id] = snapshot
        self.side_feedback[snapshot.side_id] = (
            binding.task_feedback,
            binding.feedback_revision,
        )
        return snapshot

    async def attach_side_topic(
        self,
        *,
        side_id: str,
        topic_id: str,
        root_message_id: str,
    ) -> SideSessionSnapshot:
        self.attach_side_calls.append(
            {
                "side_id": side_id,
                "topic_id": topic_id,
                "root_message_id": root_message_id,
            }
        )
        before = self.side_snapshot(side_id)
        snapshot = SideSessionSnapshot(
            side_id=before.side_id,
            parent_binding_id=before.parent_binding_id,
            parent_thread_id=before.parent_thread_id,
            thread_id=before.thread_id,
            project_alias=before.project_alias,
            cwd=before.cwd,
            creator_id=before.creator_id,
            state=before.state,
            topic_id=topic_id,
            root_message_id=root_message_id,
            turn_id=before.turn_id,
            turn_state=before.turn_state,
            last_activity=before.last_activity,
        )
        self.side_snapshots[side_id] = snapshot
        return snapshot

    def side_snapshot(self, side_id: str) -> SideSessionSnapshot:
        try:
            return self.side_snapshots[side_id]
        except KeyError as error:
            raise SideSessionNotFound(side_id) from error

    async def capture_side_submission_admission(
        self,
        side_id: str,
    ) -> SideSubmissionAdmission:
        self.capture_side_calls.append(side_id)
        snapshot = self.side_snapshot(side_id)
        return SideSubmissionAdmission(
            side_id=side_id,
            revision=0,
            thread_id=snapshot.thread_id,
            turn_id=snapshot.turn_id,
        )

    async def submit_side(self, **kwargs) -> SideSubmission:
        self.submit_side_calls.append(kwargs)
        if self.side_submission is not None:
            return self.side_submission
        snapshot = self.side_snapshot(kwargs["side_id"])
        task_feedback, feedback_revision = self.side_feedback.get(
            snapshot.side_id,
            (BindingTaskFeedback(), 1),
        )
        return SideSubmission(
            SubmitDisposition.STARTED,
            snapshot.side_id,
            snapshot.thread_id,
            f"side-turn-{len(self.submit_side_calls)}",
            lambda: None,
            task_feedback=task_feedback,
            feedback_revision=feedback_revision,
        )

    async def stop_side(self, side_id: str, *, acknowledge=None) -> StopDisposition:
        self.stop_side_calls.append(side_id)
        if acknowledge is not None:
            await acknowledge()
        return self.side_stop_result

    async def close_side(
        self,
        side_id: str,
        *,
        state: SideTopicState = SideTopicState.CLOSED,
    ) -> SideLifecycleOutcome:
        self.close_side_calls.append((side_id, state))
        if self.side_close_error is not None:
            raise self.side_close_error
        self.side_snapshots.pop(side_id, None)
        assert self.binding_store is not None
        record = self.binding_store.transition_side_topic(side_id, state)
        outcome = SideLifecycleOutcome(side_id, record.state)
        if self.completion is not None:
            await self.completion(outcome)
        return outcome

    async def close_side_exact(
        self,
        side_id: str,
        *,
        state: SideTopicState = SideTopicState.CLOSED,
    ) -> SideLifecycleOutcome:
        return await self.close_side(side_id, state=state)


class ChannelApplicationTest(unittest.IsolatedAsyncioTestCase):
    def test_quote_fetch_timeout_is_ten_seconds_per_sdk_request(self) -> None:
        self.assertEqual(channel_app._QUOTE_FETCH_TIMEOUT_SECONDS, 10.0)

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.project_root = root
        self.project = root / "project"
        self.project.mkdir()
        ids = iter(
            [
                "11111111-0000-0000-0000-000000000001",
                "22222222-0000-0000-0000-000000000002",
                "33333333-0000-0000-0000-000000000003",
            ]
        )
        self.store = BindingStore(id_factory=lambda: next(ids))
        self.channel = FakeChannel()
        self.message_history = FakeMessageHistory()
        self.runtime = StubRuntime()
        self.runtime.binding_store = self.store
        self.projects = ProjectRegistry(
            store=self.store,
            project_root=root,
            projects={"test": self.project},
        )
        self.app = ChannelApplication(
            app_id="cli_test",
            channel=self.channel,
            runtime=self.runtime,  # type: ignore[arg-type]
            bindings=self.store,
            projects=self.projects,
            message_history=self.message_history,
        )

    async def asyncTearDown(self) -> None:
        await self.app.close()
        self.store.close()
        self.tmp.cleanup()

    async def new(self, *, message_id: str = "om_new") -> FakeMessage:
        message = FakeMessage("/new", message_id=message_id)
        await self.create_binding(
            FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        )
        return message

    async def create_binding(self, scope: FeishuScope):
        return await self.app._management.create_current_binding(
            scope=scope,
            creator_id="ou_user",
            project_alias="test",
        )

    async def register_goal_card(
        self,
        *,
        scope: FeishuScope,
        binding,
        goal: GoalSnapshot,
        message_id: str,
        runtime_state: str,
        logical_turn_id: str = "goal-one",
    ) -> OutboundCard:
        if binding.native_thread_id is None:
            self.store.assign_native_thread_id(binding.id, goal.thread_id)
            binding = self.store.get(binding.id)
        projection = ReplyCardProjection(
            scope=scope,
            goal=channel_app._reply_goal_module(
                binding=binding,
                goal=goal,
                runtime_state=runtime_state,
            ),
        )
        generation = goal_generation(goal)
        self.assertTrue(
            await self.app._progress_cards.start_goal(
                binding_id=binding.id,
                thread_id=goal.thread_id,
                logical_turn_id=logical_turn_id,
                generation=generation,
                origin=channel_app.GoalCardOrigin(
                    message_id=message_id,
                    scope=scope,
                    binding_id=binding.id,
                    short_id=binding.short_id,
                    project_alias=binding.project_alias,
                ),
                projection=projection,
                revision=("test",),
                refresh=None,
            )
        )
        self.channel.updates.clear()
        return reply_card(projection)

    def form_card_event(
        self,
        form_value: dict[str, object],
        *,
        message_id: str,
        chat_id: str,
        thread_id: str | None,
        sender_id: str = "ou_user",
        chat_type: str = "group",
    ) -> object:
        self.channel.fetched_messages[message_id] = {
            "data": {"items": [{"chat_id": chat_id, "thread_id": thread_id}]}
        }
        self.channel.chat_types[chat_id] = chat_type
        return SimpleNamespace(
            message_id=message_id,
            chat_id=chat_id,
            operator=SimpleNamespace(open_id=sender_id),
            action=SimpleNamespace(
                tag="button",
                value={},
                form_value=form_value,
            ),
        )

    def direct_card_event(
        self,
        form_value: dict[str, object],
        *,
        message_id: str = "om_card",
    ) -> object:
        self.channel.fetched_messages[message_id] = {
            "data": {"items": [{"chat_id": "oc_direct", "thread_id": None}]}
        }
        self.channel.chat_types["oc_direct"] = "p2p"
        return SimpleNamespace(
            message_id=message_id,
            chat_id="oc_direct",
            operator=SimpleNamespace(open_id="ou_user"),
            action=SimpleNamespace(
                tag="button",
                value={},
                form_value=form_value,
            ),
        )

    def direct_button_event(
        self,
        value: dict[str, object],
        *,
        message_id: str = "om_card",
    ) -> object:
        return SimpleNamespace(
            message_id=message_id,
            chat_id="oc_direct",
            operator=SimpleNamespace(open_id="ou_user"),
            action=SimpleNamespace(
                tag="button",
                value=value,
                form_value=None,
            ),
        )

    def group_button_event(
        self,
        value: dict[str, object],
        *,
        message_id: str = "om_card",
        chat_id: str = "oc_group",
    ) -> object:
        return SimpleNamespace(
            message_id=message_id,
            chat_id=chat_id,
            operator=SimpleNamespace(open_id="ou_user"),
            action=SimpleNamespace(
                tag="button",
                value=value,
                form_value=None,
            ),
        )

    def new_form_values(
        self,
        card: OutboundCard,
        *,
        project_alias: str = "test",
    ) -> dict[str, object]:
        form = next(
            item
            for item in _elements(card.card, "form")
            if item["name"] == "new_binding_v6"
        )
        fields = {
            item["name"]: item
            for item in form["elements"]
            if "name" in item
        }
        project_reference = next(
            option["value"]
            for option in fields["new_project"]["options"]
            if option["text"]["content"].startswith(f"{project_alias} ·")
        )
        values: dict[str, object] = {"new_project": project_reference}
        for name in (
            "new_context_mode",
            "new_model",
            "new_effort",
            "new_speed",
            "new_task_reactions",
            "new_progress_card",
        ):
            if name in fields:
                values[name] = fields[name]["initial_option"]
        return values

    def config_form_values(
        self,
        card: OutboundCard,
        *,
        effort_id: str | None = None,
        speed_id: str | None = None,
        inherit: bool = False,
        reaction_pulse_enabled: bool | None = None,
        progress_card_enabled: bool | None = None,
    ) -> dict[str, object]:
        form = next(
            item
            for item in _elements(card.card, "form")
            if item["name"] == "binding_config_v6"
        )
        fields = {
            item["name"]: item
            for item in form["elements"]
            if "name" in item
        }
        model_field = fields["config_model"]
        model_value = model_field["initial_option"]
        if not inherit:
            model_value = next(
                (
                    option["value"]
                    for option in model_field["options"]
                    if ":explicit:" in option["value"]
                ),
                model_value,
            )
        values = {"config_model": model_value}
        values["config_task_reactions"] = fields["config_task_reactions"][
            "initial_option"
        ]
        values["config_progress_card"] = fields["config_progress_card"][
            "initial_option"
        ]
        for name, enabled in (
            ("config_task_reactions", reaction_pulse_enabled),
            ("config_progress_card", progress_card_enabled),
        ):
            if enabled is not None:
                suffix = ":on" if enabled else ":off"
                values[name] = next(
                    option["value"]
                    for option in fields[name]["options"]
                    if option["value"].endswith(suffix)
                )
        if "config_context_mode" in fields:
            values["config_context_mode"] = fields["config_context_mode"][
                "initial_option"
            ]
        if "config_effort" in fields:
            values["config_effort"] = (
                effort_id or fields["config_effort"]["initial_option"]
            )
        if "config_speed" in fields:
            values["config_speed"] = (
                speed_id or fields["config_speed"]["initial_option"]
            )
        return values

    async def test_new_is_lazy_and_first_prompt_uses_bound_project(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.assertIsNone(binding.native_thread_id)
        self.assertEqual(self.runtime.submit_calls, [])

        released = False

        def release() -> None:
            nonlocal released
            self.assertIn(("om_prompt", "Typing"), self.channel.reactions)
            self.assertIn(("om_prompt", "THINKING"), self.channel.reactions)
            released = True

        self.runtime.submission = Submission(
            SubmitDisposition.STARTED,
            binding.id,
            "native-one",
            "turn-one",
            release,
            task_feedback=PULSE_ON,
        )
        prompt = FakeMessage(
            "hello",
            message_id="om_prompt",
            sender_id="ou_alice",
            display_name="Alice",
            union_id="on_alice",
            user_id="user_alice",
        )
        await self.app.handle_message(prompt)

        self.assertTrue(released)
        self.assertEqual(self.runtime.submit_calls[0]["binding"].id, binding.id)
        self.assertEqual(self.runtime.submit_calls[0]["cwd"], self.project.resolve())
        self.assertEqual(self.runtime.submit_calls[0]["owner_id"], "ou_alice")
        request_text, current_context = plain_prompt_projection(
            self.runtime.submit_calls[0]["input"]
        )
        self.assertEqual(request_text, "hello")
        self.assertEqual(
            current_context["sender"],
            {
                "display_name": "Alice",
                "is_bot": False,
                "open_id": "ou_alice",
                "sender_type": "user",
            },
        )
        self.assertEqual(
            self.channel.reactions,
            [("om_prompt", "Typing"), ("om_prompt", "THINKING")],
        )
        self.assertNotIn(("om_prompt", "已接收，开始处理。"), self.channel.replies)
        self.assertEqual(self.message_history.read_calls, [])

        await self.app.handle_completion(
            TurnOutcome(
                binding_id=binding.id,
                thread_id="native-one",
                turn_id="turn-one",
                owner_id="ou_alice",
                origin=prompt,
                result=SimpleNamespace(
                    final_response="done",
                    status=SimpleNamespace(value="completed"),
                ),
                task_feedback=PULSE_ON,
            )
        )

        self.assertEqual(
            self.channel.reactions,
            [
                ("om_prompt", "Typing"),
                ("om_prompt", "THINKING"),
                ("om_prompt", "DONE"),
            ],
        )
        self.assertEqual(
            self.channel.reaction_removals,
            [
                ("om_prompt", "reaction-2"),
                ("om_prompt", "reaction-1"),
            ],
        )
        self.assertEqual(
            self.channel.reaction_operations,
            [
                ("add", "om_prompt", "Typing"),
                ("add", "om_prompt", "THINKING"),
                ("add", "om_prompt", "DONE"),
                ("remove", "om_prompt", "reaction-2"),
                ("remove", "om_prompt", "reaction-1"),
            ],
        )
        self.assertIn(("om_prompt", "done"), self.channel.replies)

    async def test_default_feedback_keeps_lifecycle_reactions_without_pulse_or_card(
        self,
    ) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        released = False

        def release() -> None:
            nonlocal released
            released = True

        self.runtime.submission = Submission(
            SubmitDisposition.STARTED,
            binding.id,
            "native-one",
            "turn-one",
            release,
        )
        prompt = FakeMessage("hello", message_id="om_silent")

        await self.app.handle_message(prompt)

        self.assertTrue(released)
        self.assertEqual(self.channel.reactions, [("om_silent", "Typing")])
        self.assertEqual(self.channel.replies, [])
        self.assertEqual(self.runtime.turn_activity_calls, [])

        await self.app.handle_completion(
            TurnOutcome(
                binding_id=binding.id,
                thread_id="native-one",
                turn_id="turn-one",
                owner_id="ou_user",
                origin=prompt,
                result=completed_turn_result(final_response="done"),
            )
        )

        self.assertEqual(
            self.channel.reactions,
            [("om_silent", "Typing"), ("om_silent", "DONE")],
        )
        self.assertEqual(
            self.channel.reaction_removals,
            [("om_silent", "reaction-1")],
        )
        self.assertEqual(self.channel.replies, [("om_silent", "done")])
        self.assertEqual(self.channel.updates, [])

    async def test_progress_card_updates_same_message_and_collapses_at_terminal(
        self,
    ) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        feedback = BindingTaskFeedback(progress_card_enabled=True)
        initial = turn_activity_snapshot(binding_id=binding.id)
        self.runtime.turn_activity_values[binding.id] = initial
        self.app._progress_cards = channel_app._ProgressCardController(
            self.channel,
            self.runtime,  # type: ignore[arg-type]
            poll_seconds=0.01,
        )
        released = False

        def release() -> None:
            nonlocal released
            released = True

        self.runtime.submission = Submission(
            SubmitDisposition.STARTED,
            binding.id,
            "native-one",
            "turn-one",
            release,
            task_feedback=feedback,
        )
        self.channel.reply_results.append(
            sent_result("om_progress", chat_id="oc_direct")
        )
        prompt = FakeMessage("hello", message_id="om_progress_origin")

        await self.app.handle_message(prompt)

        self.assertTrue(released)
        self.assertEqual(
            self.channel.reactions,
            [("om_progress_origin", "Typing")],
        )
        self.assertEqual(len(self.channel.replies), 1)
        running = self.channel.replies[0][1]
        self.assertIsInstance(running, OutboundCard)
        assert isinstance(running, OutboundCard)
        running_panel = next(iter(_elements(running.card, "collapsible_panel")))
        self.assertTrue(running_panel["expanded"])

        updated = turn_activity_snapshot(
            binding_id=binding.id,
            revision=2,
            steps=(
                TurnPlanStepSnapshot(
                    "inspect repository",
                    TurnPlanStepState.IN_PROGRESS,
                ),
            ),
        )
        self.runtime.turn_activity_values[binding.id] = updated
        async with asyncio.timeout(1):
            while not self.channel.updates:
                await asyncio.sleep(0.01)
        self.assertEqual(self.channel.updates[-1][0], "om_progress")
        self.assertIn("inspect repository", str(self.channel.updates[-1][1]))

        await self.app.handle_completion(
            TurnOutcome(
                binding_id=binding.id,
                thread_id="native-one",
                turn_id="turn-one",
                owner_id="ou_user",
                origin=prompt,
                result=completed_turn_result(final_response="done"),
                task_feedback=feedback,
                activity=updated,
            )
        )

        self.assertEqual(len(self.channel.replies), 1)
        self.assertEqual(self.channel.updates[-1][0], "om_progress")
        terminal_panel = next(
            iter(_elements(self.channel.updates[-1][1], "collapsible_panel"))
        )
        self.assertFalse(terminal_panel["expanded"])
        self.assertIn("done", str(self.channel.updates[-1][1]))
        self.assertEqual(
            self.channel.reactions,
            [
                ("om_progress_origin", "Typing"),
                ("om_progress_origin", "DONE"),
            ],
        )

    async def test_initial_progress_card_failure_falls_back_at_terminal(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        feedback = BindingTaskFeedback(progress_card_enabled=True)
        activity = turn_activity_snapshot(binding_id=binding.id)
        self.runtime.turn_activity_values[binding.id] = activity
        released = False

        def release() -> None:
            nonlocal released
            released = True

        self.runtime.submission = Submission(
            SubmitDisposition.STARTED,
            binding.id,
            "native-one",
            "turn-one",
            release,
            task_feedback=feedback,
        )
        self.channel.reply_results.append(RuntimeError("progress send failed"))
        prompt = FakeMessage("hello", message_id="om_progress_failed")

        with self.assertLogs("netizen.channel_app", level="ERROR"):
            await self.app.handle_message(prompt)
        self.assertTrue(released)

        await self.app.handle_completion(
            TurnOutcome(
                binding_id=binding.id,
                thread_id="native-one",
                turn_id="turn-one",
                owner_id="ou_user",
                origin=prompt,
                result=completed_turn_result(final_response="answer survives"),
                task_feedback=feedback,
                activity=activity,
            )
        )

        self.assertEqual(self.channel.replies[-1], (prompt.id, "answer survives"))
        self.assertEqual(self.channel.updates, [])

    async def test_progress_sessions_are_isolated_by_exact_turn_identity(self) -> None:
        controller = self.app._progress_cards
        first = turn_activity_snapshot(
            binding_id="binding-one",
            thread_id="thread-one",
            turn_id="shared-turn",
        )
        second = turn_activity_snapshot(
            binding_id="binding-two",
            thread_id="thread-two",
            turn_id="shared-turn",
        )
        self.runtime.turn_activity_values.update(
            {"binding-one": first, "binding-two": second}
        )
        self.channel.reply_results.extend(
            (
                sent_result("om_progress_one", chat_id="oc_direct"),
                sent_result("om_progress_two", chat_id="oc_direct"),
            )
        )

        self.assertTrue(
            await controller.start(
                binding_id=first.binding_id,
                thread_id=first.thread_id,
                turn_id=first.turn_id,
                origin=FakeMessage("one", message_id="om_one"),
            )
        )
        self.assertTrue(
            await controller.start(
                binding_id=second.binding_id,
                thread_id=second.thread_id,
                turn_id=second.turn_id,
                origin=FakeMessage("two", message_id="om_two"),
            )
        )
        self.assertEqual(len(controller._sessions), 2)

        delivered = await controller.finish(
            binding_id=first.binding_id,
            thread_id=first.thread_id,
            turn_id=first.turn_id,
            activity=first,
            render=lambda snapshot: channel_app.turn_progress_card(
                snapshot=snapshot,
                terminal_status="completed",
                final_response="done",
            ),
        )

        self.assertTrue(delivered)
        self.assertEqual(self.channel.updates[-1][0], "om_progress_one")
        self.assertEqual(len(controller._sessions), 1)
        self.assertIn(
            (second.binding_id, second.thread_id, second.turn_id),
            controller._sessions,
        )
        await controller.abandon(
            binding_id=second.binding_id,
            thread_id=second.thread_id,
            turn_id=second.turn_id,
        )
        self.assertEqual(controller._sessions, {})

    async def test_progress_file_page_keeps_collapsed_process_panel(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-one")
        feedback = BindingTaskFeedback(progress_card_enabled=True)
        activity = turn_activity_snapshot(
            binding_id=binding.id,
            steps=(
                TurnPlanStepSnapshot("generate files", TurnPlanStepState.COMPLETED),
            ),
        )
        self.runtime.turn_activity_values[binding.id] = activity
        self.runtime.submission = Submission(
            SubmitDisposition.STARTED,
            binding.id,
            "native-one",
            "turn-one",
            lambda: None,
            task_feedback=feedback,
        )
        self.channel.reply_results.append(
            sent_result("om_progress", chat_id="oc_direct")
        )
        prompt = FakeMessage("hello", message_id="om_progress_origin")
        await self.app.handle_message(prompt)
        paths = tuple(f"result-{index:02}.txt" for index in range(10))
        for path in paths:
            (self.project / path).write_text(path, encoding="utf-8")

        await self.app.handle_completion(
            TurnOutcome(
                binding_id=binding.id,
                thread_id="native-one",
                turn_id="turn-one",
                owner_id="ou_user",
                origin=prompt,
                result=completed_turn_result(
                    file_change_item(*paths),
                    final_response="files ready",
                ),
                task_feedback=feedback,
                activity=activity,
            )
        )

        terminal = self.channel.updates[-1][1]
        page_value = next(
            behavior["value"]
            for button in _elements(terminal, "button")
            for behavior in button.get("behaviors", ())
            if behavior["value"]["intent"] == "turn-file.page"
        )
        await self.app.handle_card_action(
            self.direct_button_event(page_value, message_id="om_progress")
        )

        paged = self.channel.updates[-1][1]
        panel = next(iter(_elements(paged, "collapsible_panel")))
        self.assertFalse(panel["expanded"])
        self.assertIn("generate files", str(paged))
        self.assertIn("result-08.txt", str(paged))

    async def test_terminal_progress_update_failure_falls_back_to_plain_answer(
        self,
    ) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        feedback = BindingTaskFeedback(progress_card_enabled=True)
        activity = turn_activity_snapshot(binding_id=binding.id)
        self.runtime.turn_activity_values[binding.id] = activity
        self.runtime.submission = Submission(
            SubmitDisposition.STARTED,
            binding.id,
            "native-one",
            "turn-one",
            lambda: None,
            task_feedback=feedback,
        )
        self.channel.reply_results.append(
            sent_result("om_progress", chat_id="oc_direct")
        )
        prompt = FakeMessage("hello", message_id="om_progress_origin")
        await self.app.handle_message(prompt)
        self.channel.fail_card_updates = True

        with (
            self.assertLogs("netizen.channel_app", level="ERROR"),
            patch.object(
                channel_app,
                "turn_patch_summary",
                wraps=channel_app.turn_patch_summary,
            ) as parse_diff,
        ):
            await self.app.handle_completion(
                TurnOutcome(
                    binding_id=binding.id,
                    thread_id="native-one",
                    turn_id="turn-one",
                    owner_id="ou_user",
                    origin=prompt,
                    result=completed_turn_result(
                        final_response="answer survives"
                    ),
                    task_feedback=feedback,
                    activity=activity,
                )
            )

        self.assertEqual(parse_diff.call_count, 1)
        self.assertEqual(self.channel.replies[-1], (prompt.id, "answer survives"))
        self.assertEqual(self.channel.updates[-1][0], "om_progress")

    async def test_intermediate_progress_failure_stops_updates_and_falls_back(
        self,
    ) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        feedback = BindingTaskFeedback(progress_card_enabled=True)
        initial = turn_activity_snapshot(binding_id=binding.id)
        self.runtime.turn_activity_values[binding.id] = initial
        self.app._progress_cards = channel_app._ProgressCardController(
            self.channel,
            self.runtime,  # type: ignore[arg-type]
            poll_seconds=0.01,
        )
        self.runtime.submission = Submission(
            SubmitDisposition.STARTED,
            binding.id,
            "native-one",
            "turn-one",
            lambda: None,
            task_feedback=feedback,
        )
        self.channel.reply_results.append(
            sent_result("om_progress", chat_id="oc_direct")
        )
        prompt = FakeMessage("hello", message_id="om_progress_origin")
        await self.app.handle_message(prompt)
        self.channel.fail_card_updates = True
        updated = turn_activity_snapshot(
            binding_id=binding.id,
            revision=2,
            steps=(
                TurnPlanStepSnapshot("verify", TurnPlanStepState.IN_PROGRESS),
            ),
        )
        self.runtime.turn_activity_values[binding.id] = updated
        key = (binding.id, "native-one", "turn-one")

        with self.assertLogs("netizen.channel_app", level="ERROR"):
            async with asyncio.timeout(1):
                while not self.app._progress_cards._sessions[key].failed:
                    await asyncio.sleep(0.01)
        attempts = len(self.channel.updates)
        await asyncio.sleep(0.03)
        self.assertEqual(len(self.channel.updates), attempts)

        await self.app.handle_completion(
            TurnOutcome(
                binding_id=binding.id,
                thread_id="native-one",
                turn_id="turn-one",
                owner_id="ou_user",
                origin=prompt,
                result=completed_turn_result(final_response="answer survives"),
                task_feedback=feedback,
                activity=updated,
            )
        )

        self.assertEqual(self.channel.replies[-1], (prompt.id, "answer survives"))

    async def test_pulse_off_steer_keeps_lifecycle_confirmation(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.runtime.submission = Submission(
            SubmitDisposition.STEERED,
            binding.id,
            "native-one",
            "turn-one",
        )

        await self.app.handle_message(
            FakeMessage("new direction", message_id="om_silent_steer")
        )

        self.assertEqual(
            self.channel.reactions,
            [("om_silent_steer", "OnIt")],
        )
        self.assertEqual(self.channel.replies, [])

    async def test_reactions_and_progress_card_can_run_together(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        feedback = BindingTaskFeedback(
            reaction_pulse_enabled=True,
            progress_card_enabled=True,
        )
        activity = turn_activity_snapshot(binding_id=binding.id)
        self.runtime.turn_activity_values[binding.id] = activity
        self.runtime.submission = Submission(
            SubmitDisposition.STARTED,
            binding.id,
            "native-one",
            "turn-one",
            lambda: None,
            task_feedback=feedback,
        )
        self.channel.reply_results.append(
            sent_result("om_progress", chat_id="oc_direct")
        )
        prompt = FakeMessage("hello", message_id="om_both")

        await self.app.handle_message(prompt)

        self.assertIn((prompt.id, "Typing"), self.channel.reactions)
        self.assertIn((prompt.id, "THINKING"), self.channel.reactions)
        self.assertIsInstance(self.channel.replies[-1][1], OutboundCard)

        await self.app.handle_completion(
            TurnOutcome(
                binding_id=binding.id,
                thread_id="native-one",
                turn_id="turn-one",
                owner_id="ou_user",
                origin=prompt,
                result=completed_turn_result(final_response="done"),
                task_feedback=feedback,
                activity=activity,
            )
        )

        self.assertIn((prompt.id, "DONE"), self.channel.reactions)
        self.assertEqual(self.channel.updates[-1][0], "om_progress")
        self.assertNotIn((prompt.id, "done"), self.channel.replies)

    async def test_completed_turn_email_audit_rejection_gets_safe_notice(
        self,
    ) -> None:
        origin = await self.new(message_id="om_audit")
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        sensitive_response = "已将邮箱改为 alice@example.com"
        audit_message = (
            "The messages do NOT pass the audit, "
            "ext=contain sensitive data: EMAIL_ADDRESS"
        )
        self.channel.reply_results.extend(
            (
                failed_reply_result(code=230028, message=audit_message),
                sent_result("om_notice", chat_id="oc_direct"),
            )
        )

        await self.app.handle_completion(
            TurnOutcome(
                binding_id=binding.id,
                thread_id="native-audit",
                turn_id="turn-audit",
                owner_id="ou_user",
                origin=origin,
                result=completed_turn_result(final_response=sensitive_response),
            )
        )

        self.assertEqual(
            self.channel.replies,
            [
                (origin.id, sensitive_response),
                (
                    origin.id,
                    "消息发送失败：飞书内容审核认为回复中包含邮箱地址。"
                    "（错误码 230028）",
                ),
            ],
        )
        notice = str(self.channel.replies[-1][1])
        self.assertNotIn("alice@example.com", notice)
        self.assertNotIn("请让我", notice)

    async def test_content_audit_unknown_reason_gets_generic_notice(self) -> None:
        origin = FakeMessage("hello", message_id="om_unknown_audit")
        self.channel.reply_results.extend(
            (
                failed_reply_result(
                    code=230028,
                    message="audit rejected: PHONE_NUMBER",
                ),
                sent_result("om_notice", chat_id="oc_direct"),
            )
        )

        await self.app._reply(origin, "sensitive source response")

        self.assertEqual(
            self.channel.replies[-1],
            (
                origin.id,
                "消息发送失败：回复内容未通过飞书审核。（错误码 230028）",
            ),
        )
        self.assertNotIn("PHONE_NUMBER", str(self.channel.replies[-1][1]))

    async def test_content_audit_notice_failure_does_not_recurse(self) -> None:
        origin = FakeMessage("hello", message_id="om_rejected_notice")
        rejected = failed_reply_result(
            code=230028,
            message="contain sensitive data: EMAIL_ADDRESS",
        )
        self.channel.reply_results.extend((rejected, rejected))

        with self.assertLogs("netizen.channel_app", level="ERROR") as logs:
            await self.app._reply(origin, "alice@example.com")

        self.assertEqual(len(self.channel.replies), 2)
        self.assertIn(
            "failed to send safe reply failure notice",
            "\n".join(logs.output),
        )

    async def test_non_audit_reply_failure_does_not_auto_send(self) -> None:
        origin = FakeMessage("hello", message_id="om_unknown_failure")
        self.channel.reply_results.append(
            failed_reply_result(
                code=50_001,
                message="upstream state unknown",
                retryable=True,
            )
        )

        await self.app._reply(origin, "answer")

        self.assertEqual(self.channel.replies, [(origin.id, "answer")])

    async def test_group_new_card_creates_catch_up_binding_from_exact_anchor(
        self,
    ) -> None:
        scope = FeishuScope("cli_test", "oc_group", ScopeKind.GROUP)
        await self.app.handle_message(
            FakeMessage(
                "/new",
                message_id="om_new_group",
                chat_id="oc_group",
                chat_type="group",
            )
        )
        picker = self.channel.replies[-1][1]
        values = self.new_form_values(picker)
        form = _elements(picker.card, "form")[0]
        context_field = next(
            item
            for item in form["elements"]
            if item.get("name") == "new_context_mode"
        )
        values["new_context_mode"] = next(
            option["value"]
            for option in context_field["options"]
            if option["value"].endswith(":catch-up")
        )
        anchor = MessageContextAnchor("om_new_group_card", 4_000)
        self.message_history.anchors[anchor.message_id] = anchor

        await self.app.handle_card_action(
            self.form_card_event(
                values,
                message_id=anchor.message_id,
                chat_id="oc_group",
                thread_id=None,
            )
        )

        binding = self.store.active_binding(scope.key)
        self.assertIsNotNone(binding)
        self.assertIs(binding.message_context_mode, MentionContextMode.CATCH_UP)
        self.assertEqual(binding.context_anchor, anchor)
        self.assertEqual(
            self.message_history.resolve_calls,
            [(scope, anchor.message_id)],
        )
        rendered = str(self.channel.updates[-1][1])
        self.assertIn("自动带上期间的群聊讨论", rendered)
        self.assertIn("未 @ 机器人", str(picker.card))

    async def test_p2p_topic_cannot_forge_catch_up_card_submission(self) -> None:
        await self.app.handle_message(
            FakeMessage(
                "/new",
                message_id="om_new_topic_picker",
                chat_id="oc_group",
                chat_type="group",
                thread_id="omt_topic",
            )
        )
        picker = self.channel.replies[-1][1]
        values = self.new_form_values(picker)
        form = _elements(picker.card, "form")[0]
        context_field = next(
            item
            for item in form["elements"]
            if item.get("name") == "new_context_mode"
        )
        values["new_context_mode"] = next(
            option["value"]
            for option in context_field["options"]
            if option["value"].endswith(":catch-up")
        )
        event = self.form_card_event(
            values,
            message_id="om_p2p_topic_card",
            chat_id="oc_group",
            thread_id="omt_topic",
            chat_type="p2p",
        )

        await self.app.handle_card_action(event)

        scope = FeishuScope(
            "cli_test", "oc_group", ScopeKind.TOPIC, "omt_topic"
        )
        self.assertIsNone(self.store.active_binding(scope.key))
        self.assertEqual(self.message_history.resolve_calls, [])
        self.assertIn("私聊话题不支持", str(self.channel.updates[-1][1]))

    async def test_config_switch_to_catch_up_is_one_atomic_card_operation(
        self,
    ) -> None:
        scope = FeishuScope("cli_test", "oc_group", ScopeKind.GROUP)
        created = await self.create_binding(scope)
        await self.app.handle_message(
            FakeMessage(
                "/config",
                message_id="om_config_group",
                chat_id="oc_group",
                chat_type="group",
            )
        )
        card = self.channel.replies[-1][1]
        values = self.config_form_values(card)
        form = _elements(card.card, "form")[0]
        context_field = next(
            item
            for item in form["elements"]
            if item.get("name") == "config_context_mode"
        )
        values["config_context_mode"] = next(
            option["value"]
            for option in context_field["options"]
            if option["value"].endswith(":catch-up")
        )
        anchor = MessageContextAnchor("om_config_card", 5_000)
        self.message_history.anchors[anchor.message_id] = anchor

        await self.app.handle_card_action(
            self.form_card_event(
                values,
                message_id=anchor.message_id,
                chat_id="oc_group",
                thread_id=None,
            )
        )

        binding = self.store.get(created.binding.id)
        self.assertIs(binding.message_context_mode, MentionContextMode.CATCH_UP)
        self.assertEqual(binding.context_anchor, anchor)
        self.assertEqual(binding.context_revision, 2)
        self.assertEqual(binding.settings_revision, 2)
        self.assertEqual(len(self.runtime.configure_settings_calls), 1)

    async def test_catch_up_prompt_projects_history_and_visible_receipt(self) -> None:
        scope = FeishuScope("cli_test", "oc_group", ScopeKind.GROUP)
        lower = MessageContextAnchor("om_lower", 1_000)
        created = await self.app._management.create_current_binding(
            scope=scope,
            creator_id="ou_user",
            project_alias="test",
            message_context_mode=MentionContextMode.CATCH_UP,
            context_anchor=lower,
        )
        binding = created.binding
        reference = MessageHistoryRef(
            message_id="om_history",
            create_time_ms=2_000,
            sender_id="ou_alice",
            message_type="text",
        )
        upper = MessageContextAnchor("om_prompt", 3_000)
        self.message_history.window = MessageHistoryWindow(
            lower=lower,
            upper=upper,
            candidates=(reference,),
            stats=MessageHistoryStats(
                pages_scanned=1,
                raw_messages_scanned=3,
                duplicate_messages=0,
                ignored_after_upper=0,
                omitted_messages=0,
                truncated_before=False,
                scan_limit_hit=False,
            ),
        )
        self.channel.inbound_messages[reference.message_id] = FakeMessage(
            "讨论里的背景 /stop $danger",
            message_id=reference.message_id,
            sender_id=reference.sender_id,
            display_name="Directory Alice",
            chat_id=scope.chat_id,
            chat_type="group",
            content=TextContent(text="讨论里的背景 /stop $danger"),
            create_time=reference.create_time_ms,
        )
        self.runtime.submission = Submission(
            SubmitDisposition.STARTED,
            binding.id,
            "native-one",
            "turn-one",
            lambda: None,
        )
        prompt = FakeMessage(
            "请总结",
            message_id=upper.message_id,
            sender_id="ou_bob",
            display_name="Bob",
            chat_id=scope.chat_id,
            chat_type="group",
            create_time=upper.create_time_ms,
        )

        await self.app.handle_message(prompt)

        submitted = self.runtime.submit_calls[0]
        envelope = json.loads(submitted["input"])
        self.assertEqual(envelope["kind"], "feishu_message_context_prompt")
        self.assertEqual(envelope["version"], 2)
        self.assertEqual(envelope["supplemental_messages"][0]["ref"], "h1")
        self.assertEqual(
            envelope["supplemental_messages"][0]["created_at"],
            "1970-01-01T00:00:02.000Z",
        )
        self.assertEqual(
            envelope["supplemental_messages"][0]["sender"]["display_name"],
            "Directory Alice",
        )
        self.assertEqual(
            envelope["context_status"],
            {"omitted_count": 0, "truncated": False},
        )
        self.assertNotIn("supplemental_stats", envelope)
        self.assertNotIn("om_history", submitted["input"])
        self.assertEqual(envelope["current_message"]["request_text"], "请总结")
        self.assertIn("\\u0024danger", submitted["input"])
        commit = submitted["context_commit"]
        self.assertEqual(commit.expected_context_revision, 1)
        self.assertEqual(commit.anchor, upper)
        self.assertEqual(
            self.message_history.read_calls,
            [(scope, lower, upper.message_id)],
        )
        self.assertTrue(
            any(
                message_id == upper.message_id and "带入 1 条" in str(content)
                for message_id, content in self.channel.replies
            )
        )

    async def test_catch_up_prompt_uses_list_sender_name_for_attribution(self) -> None:
        scope = FeishuScope("cli_test", "oc_group", ScopeKind.GROUP)
        lower = MessageContextAnchor("om_lower", 1_000)
        created = await self.app._management.create_current_binding(
            scope=scope,
            creator_id="ou_user",
            project_alias="test",
            message_context_mode=MentionContextMode.CATCH_UP,
            context_anchor=lower,
        )
        binding = created.binding
        reference = MessageHistoryRef(
            message_id="om_history",
            create_time_ms=2_000,
            sender_id="ou_alice",
            message_type="text",
            sender_name="List Alice",
        )
        upper = MessageContextAnchor("om_prompt", 3_000)
        self.message_history.window = MessageHistoryWindow(
            lower=lower,
            upper=upper,
            candidates=(reference,),
            stats=MessageHistoryStats(
                pages_scanned=1,
                raw_messages_scanned=2,
                duplicate_messages=0,
                ignored_after_upper=0,
                omitted_messages=0,
                truncated_before=False,
                scan_limit_hit=False,
            ),
        )
        self.channel.inbound_messages[reference.message_id] = FakeMessage(
            "背景",
            message_id=reference.message_id,
            sender_id=reference.sender_id,
            display_name="",
            chat_id=scope.chat_id,
            chat_type="group",
            content=TextContent(text="背景"),
            create_time=reference.create_time_ms,
        )
        self.runtime.submission = Submission(
            SubmitDisposition.STARTED,
            binding.id,
            "native-one",
            "turn-one",
            lambda: None,
        )
        prompt = FakeMessage(
            "请总结",
            message_id=upper.message_id,
            sender_id="ou_bob",
            display_name="Bob",
            chat_id=scope.chat_id,
            chat_type="group",
            create_time=upper.create_time_ms,
        )

        await self.app.handle_message(prompt)

        submitted = self.runtime.submit_calls[0]
        envelope = json.loads(submitted["input"])
        self.assertEqual(
            envelope["supplemental_messages"][0]["sender"]["display_name"],
            "List Alice",
        )

    async def test_catch_up_uses_one_local_ref_space_for_all_image_sources(
        self,
    ) -> None:
        scope = FeishuScope("cli_test", "oc_group", ScopeKind.GROUP)
        lower = MessageContextAnchor("om_lower", 1_000)
        created = await self.app._management.create_current_binding(
            scope=scope,
            creator_id="ou_user",
            project_alias="test",
            message_context_mode=MentionContextMode.CATCH_UP,
            context_anchor=lower,
        )
        binding = created.binding
        history_ref = MessageHistoryRef(
            message_id="om_history_image",
            create_time_ms=2_000,
            sender_id="ou_alice",
            message_type="post",
            sender_name="Alice",
        )
        upper = MessageContextAnchor("om_prompt_image", 3_000)
        self.message_history.window = MessageHistoryWindow(
            lower=lower,
            upper=upper,
            candidates=(history_ref,),
            stats=MessageHistoryStats(
                pages_scanned=1,
                raw_messages_scanned=2,
                duplicate_messages=0,
                ignored_after_upper=0,
                omitted_messages=0,
                truncated_before=False,
                scan_limit_hit=False,
            ),
        )
        self.channel.inbound_messages[history_ref.message_id] = FakeMessage(
            "history ![image](img_history)",
            message_id=history_ref.message_id,
            sender_id=history_ref.sender_id,
            display_name="Alice",
            chat_id=scope.chat_id,
            chat_type="group",
            raw_content_type="post",
            content=PostContent(
                post={
                    "zh_cn": {
                        "content_v2": [[
                            {"tag": "text", "text": "history "},
                            {"tag": "img", "image_key": "img_history"},
                        ]]
                    }
                }
            ),
            resources=[
                ResourceDescriptor(type="image", file_key="img_history")
            ],
            create_time=history_ref.create_time_ms,
        )
        self.channel.inbound_messages["om_quote_image"] = FakeMessage(
            "![image](img_quote)",
            message_id="om_quote_image",
            sender_id="ou_carol",
            display_name="Carol",
            chat_id=scope.chat_id,
            chat_type="group",
            raw_content_type="image",
            content=ImageContent(image_key="img_quote"),
            resources=[ResourceDescriptor(type="image", file_key="img_quote")],
            create_time=2_500,
        )
        for message_id, file_key in (
            ("om_history_image", "img_history"),
            ("om_quote_image", "img_quote"),
            ("om_prompt_image", "img_current"),
        ):
            self.channel.resource_bodies[(message_id, file_key)] = PNG
        self.runtime.submission = Submission(
            SubmitDisposition.STARTED,
            binding.id,
            "native-one",
            "turn-one",
            lambda: None,
        )
        prompt = FakeMessage(
            "compare ![image](img_current)",
            message_id=upper.message_id,
            sender_id="ou_bob",
            display_name="Bob",
            chat_id=scope.chat_id,
            chat_type="group",
            raw_content_type="post",
            content=PostContent(
                post={
                    "zh_cn": {
                        "content_v2": [[
                            {"tag": "text", "text": "compare "},
                            {"tag": "img", "image_key": "img_current"},
                        ]]
                    }
                }
            ),
            resources=[
                ResourceDescriptor(type="image", file_key="img_current")
            ],
            reply_id="om_quote_image",
            raw={"parent_id": "om_quote_image", "root_id": "om_root"},
            create_time=upper.create_time_ms,
        )

        await self.app.handle_message(prompt)

        native_input = self.runtime.submit_calls[0]["input"]
        self.assertIsInstance(native_input, list)
        labels = [json.loads(native_input[index].text) for index in (0, 2, 4)]
        self.assertEqual(
            [(label["source"], label["ref"]) for label in labels],
            [
                ("supplemental_message", "img1"),
                ("quoted_message", "img2"),
                ("current_message", "img3"),
            ],
        )
        self.assertTrue(
            all(
                "file_key" not in label and "message_id" not in label
                for label in labels
            )
        )
        envelope = json.loads(native_input[-1].text)
        self.assertEqual(envelope["version"], 2)
        self.assertEqual(
            envelope["supplemental_messages"][0]["attachments"],
            [{"type": "image", "ref": "img1"}],
        )
        self.assertEqual(
            envelope["supplemental_messages"][0]["text"],
            "history ![image](img1)",
        )
        self.assertEqual(envelope["quoted_message"]["ref"], "h2")
        self.assertEqual(
            envelope["quoted_message"]["attachments"],
            [{"type": "image", "ref": "img2"}],
        )
        self.assertEqual(
            envelope["current_message"]["request_text"],
            "compare ![image](img3)",
        )
        for raw_identifier in (
            "om_history_image",
            "om_quote_image",
            "img_history",
            "img_quote",
            "img_current",
        ):
            self.assertNotIn(raw_identifier, native_input[-1].text)

    def test_history_candidate_keeps_stable_sender_identity_gate(self) -> None:
        scope = FeishuScope("cli_test", "oc_group", ScopeKind.GROUP)
        reference = MessageHistoryRef(
            message_id="om_history",
            create_time_ms=2_000,
            sender_id="ou_alice",
            message_type="text",
        )
        mismatches = (
            {"sender_id": "ou_other"},
            {"is_bot": True},
            {"sender_type": "bot"},
        )

        for mismatch in mismatches:
            with self.subTest(mismatch=mismatch):
                message = FakeMessage(
                    "背景",
                    message_id=reference.message_id,
                    sender_id=str(mismatch.get("sender_id", reference.sender_id)),
                    display_name="Directory Alice",
                    sender_type=str(mismatch.get("sender_type", "user")),
                    is_bot=bool(mismatch.get("is_bot", False)),
                    chat_id=scope.chat_id,
                    chat_type="group",
                    create_time=reference.create_time_ms,
                )

                with self.assertRaisesRegex(
                    MessageHistoryUnavailable,
                    "发送者与历史索引不一致",
                ):
                    self.app._validate_history_candidate(scope, reference, message)

    async def test_resume_catch_up_binding_resets_boundary_to_control_message(
        self,
    ) -> None:
        scope = FeishuScope("cli_test", "oc_group", ScopeKind.GROUP)
        first = await self.app._management.create_current_binding(
            scope=scope,
            creator_id="ou_user",
            project_alias="test",
            message_context_mode=MentionContextMode.CATCH_UP,
            context_anchor=MessageContextAnchor("om_old", 1_000),
        )
        await self.create_binding(scope)
        reset = MessageContextAnchor("om_resume", 6_000)
        self.message_history.anchors[reset.message_id] = reset

        await self.app.handle_message(
            FakeMessage(
                f"/resume {first.binding.short_id}",
                message_id=reset.message_id,
                chat_id=scope.chat_id,
                chat_type="group",
            )
        )

        resumed = self.store.active_binding(scope.key)
        self.assertEqual(resumed.id, first.binding.id)
        self.assertEqual(resumed.context_anchor, reset)
        self.assertEqual(
            self.message_history.resolve_calls,
            [(scope, reset.message_id)],
        )

    async def test_completed_turn_with_files_is_one_answer_and_file_card(
        self,
    ) -> None:
        origin = await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-files")
        report = self.project / "reports" / "sales.xlsx"
        report.parent.mkdir()
        report.write_bytes(b"spreadsheet")
        before = len(self.channel.replies)

        await self.app.handle_completion(
            TurnOutcome(
                binding_id=binding.id,
                thread_id="native-files",
                turn_id="turn-files",
                owner_id="ou_user",
                origin=origin,
                result=completed_turn_result(
                    file_change_item("reports/sales.xlsx"),
                    final_response="analysis complete",
                ),
            )
        )

        delivered = self.channel.replies[before:]
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0][0], origin.id)
        self.assertIsInstance(delivered[0][1], OutboundCard)
        rendered = json.dumps(delivered[0][1].card, ensure_ascii=False)
        visible = "\n".join(
            element["content"]
            for element in _elements(delivered[0][1].card, "markdown")
        )
        self.assertIn("analysis complete", rendered)
        self.assertIn("reports/sales.xlsx", rendered)
        self.assertIn("点击“发送”后", rendered)
        self.assertNotIn(str(self.project.resolve()), visible)
        self.assertEqual(self.channel.send_calls, [])

    async def test_completed_turn_diff_alone_produces_the_file_card(self) -> None:
        origin = await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-files")
        report = self.project / "research.md"
        report.write_text("research", encoding="utf-8")

        with patch.object(
            channel_app,
            "turn_patch_summary",
            wraps=channel_app.turn_patch_summary,
        ) as parse_diff:
            await self.app.handle_completion(
                TurnOutcome(
                    binding_id=binding.id,
                    thread_id="native-files",
                    turn_id="turn-diff-only",
                    owner_id="ou_user",
                    origin=origin,
                    result=completed_turn_result(
                        final_response="research complete"
                    ),
                    turn_diff=(
                        "diff --git a/research.md b/research.md\n"
                        "new file mode 100644\n"
                        "--- /dev/null\n"
                        "+++ b/research.md\n"
                        "@@ -0,0 +1,2 @@\n"
                        "+research\n"
                        "+notes\n"
                    ),
                )
            )

        self.assertEqual(parse_diff.call_count, 1)

        card = self.channel.replies[-1][1]
        self.assertIsInstance(card, OutboundCard)
        assert isinstance(card, OutboundCard)
        rendered = json.dumps(card.card, ensure_ascii=False)
        self.assertIn("research complete", rendered)
        self.assertIn("research.md", rendered)
        self.assertNotIn("+2", rendered)
        self.assertNotIn("累计修改", rendered)
        self.assertNotIn("8 B", rendered)
        self.assertEqual(_card_button_value(card, "发送")["v"], 4)

    async def test_completed_patch_card_accumulates_parent_and_child_and_preserves_partial_rows(self) -> None:
        origin = await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-files")
        (self.project / "shared.txt").write_text("child\nparent\n")
        own = ThreadItem.model_validate({
            "type": "fileChange", "id": "patch", "status": "completed",
            "changes": [{"path": "shared.txt", "kind": {"type": "update"},
                         "diff": "@@ -1 +1,2 @@\n child\n+parent\n"}],
        })
        child = ThreadItem.model_validate({
            "type": "fileChange", "id": "patch", "status": "completed",
            "changes": [{"path": "shared.txt", "kind": {"type": "add"},
                         "diff": "child\n"}],
        })
        batch = TurnPatchBatch("child", "child-turn", self.project, (child,))
        for complete in (True, False):
            with self.subTest(complete=complete):
                await self.app.handle_completion(TurnOutcome(
                    binding_id=binding.id, thread_id="native-files", turn_id="root-turn",
                    owner_id="ou_user", origin=origin,
                    result=completed_turn_result(own, final_response="done"),
                    patch_children=TaskPatchChildren((batch,), complete),
                ))
                card = self.channel.replies[-1][1]
                self.assertIsInstance(card, OutboundCard)
                visible = "\n".join(e["content"] for e in _elements(card.card, "markdown"))
                self.assertIn("<font color='green'>+2", visible)
                self.assertEqual("累计修改" in visible, complete)
                self.assertNotIn(str(self.project), visible)

    async def test_generated_image_outside_project_gets_artifact_aware_card(
        self,
    ) -> None:
        origin = await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-files")
        image = (
            self.project.parent
            / "codex-home"
            / "generated_images"
            / "native-files"
            / "generated.png"
        )
        image.parent.mkdir(parents=True)
        image.write_bytes(PNG)
        before = len(self.channel.replies)

        await self.app.handle_completion(
            TurnOutcome(
                binding_id=binding.id,
                thread_id="native-files",
                turn_id="turn-files",
                owner_id="ou_user",
                origin=origin,
                result=completed_turn_result(
                    image_generation_item(image),
                    final_response=None,
                ),
            )
        )

        delivered = self.channel.replies[before:]
        self.assertEqual(len(delivered), 1)
        self.assertIsInstance(delivered[0][1], OutboundCard)
        rendered = json.dumps(delivered[0][1].card, ensure_ascii=False)
        visible = "\n".join(
            element["content"]
            for element in _elements(delivered[0][1].card, "markdown")
        )
        self.assertIn("任务已完成，已生成以下文件", rendered)
        self.assertIn("生成图片/generated.png", rendered)
        self.assertIn("点击“发送”后", rendered)
        self.assertNotIn(str(image.parent), visible)

    async def test_file_card_delivery_failure_falls_back_to_plain_answer(
        self,
    ) -> None:
        origin = await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-files")
        report = self.project / "report.txt"
        report.write_text("report", encoding="utf-8")
        self.channel.reply_results.extend((RuntimeError("card failed"), object()))

        with self.assertLogs("netizen.channel_app", level="ERROR"):
            await self.app.handle_completion(
                TurnOutcome(
                    binding_id=binding.id,
                    thread_id="native-files",
                    turn_id="turn-files",
                    owner_id="ou_user",
                    origin=origin,
                    result=completed_turn_result(
                        file_change_item("report.txt"),
                        final_response="answer survives",
                    ),
                )
            )

        self.assertIsInstance(self.channel.replies[-2][1], OutboundCard)
        self.assertEqual(self.channel.replies[-1], (origin.id, "answer survives"))

    async def test_file_card_limit_is_explicit_and_never_truncates(self) -> None:
        origin = await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-files")
        report = self.project / "report.txt"
        report.write_text("report", encoding="utf-8")

        with (
            patch.object(
                channel_app,
                "turn_files_card",
                side_effect=channel_app.TurnFileCardLimitError(
                    "本轮文件共 501 个，超过上限；未截断文件清单。"
                ),
            ),
            self.assertLogs("netizen.channel_app", level="WARNING"),
        ):
            await self.app.handle_completion(
                TurnOutcome(
                    binding_id=binding.id,
                    thread_id="native-files",
                    turn_id="turn-files",
                    owner_id="ou_user",
                    origin=origin,
                    result=completed_turn_result(
                        file_change_item("report.txt"),
                        final_response="answer survives",
                    ),
                )
            )

        self.assertEqual(len(self.channel.replies), 1)
        fallback = self.channel.replies[-1][1]
        self.assertIsInstance(fallback, str)
        self.assertIn("answer survives", fallback)
        self.assertIn("未截断文件清单", fallback)
        self.assertNotIsInstance(fallback, OutboundCard)

    async def test_v4_file_card_pages_survive_restart_without_turn_read(self) -> None:
        origin = await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-files")
        paths = tuple(f"result-{index:02}.txt" for index in range(10))
        for path in paths:
            (self.project / path).write_text(path, encoding="utf-8")
        result = completed_turn_result(
            file_change_item(*paths),
            final_response="ten files",
        )
        await self.app.handle_completion(
            TurnOutcome(
                binding_id=binding.id,
                thread_id="native-files",
                turn_id="turn-files",
                owner_id="ou_user",
                origin=origin,
                result=result,
            )
        )
        card = self.channel.replies[-1][1]
        assert isinstance(card, OutboundCard)
        next_page = _card_button_value(card, "下一页")

        await self.new(message_id="om_switched")
        self.assertNotEqual(self.store.active_binding(scope.key).id, binding.id)
        restarted_runtime = StubRuntime()
        restarted_runtime.binding_store = self.store
        restarted_app = ChannelApplication(
            app_id="cli_test",
            channel=self.channel,
            runtime=restarted_runtime,  # type: ignore[arg-type]
            bindings=self.store,
            projects=self.projects,
        )
        changes_before_callback = self.store._connection.total_changes
        await restarted_app.handle_card_action(
            self.direct_button_event(next_page, message_id="om_file_card")
        )

        self.assertEqual(len(self.channel.updates), 1)
        updated = json.dumps(self.channel.updates[0][1], ensure_ascii=False)
        updated_visible = "\n".join(
            element["content"]
            for element in _elements(self.channel.updates[0][1], "markdown")
        )
        self.assertIn("result-08.txt", updated)
        self.assertIn("result-09.txt", updated)
        self.assertNotIn("result-00.txt", updated_visible)
        self.assertEqual(
            self.store._connection.total_changes,
            changes_before_callback,
        )

    async def test_file_buttons_send_file_and_original_image_to_card_topic(
        self,
    ) -> None:
        origin = await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-files")
        report = self.project / "report.pdf"
        image = self.project / "trend.png"
        report.write_bytes(b"pdf")
        image.write_bytes(PNG)
        result = completed_turn_result(
            file_change_item("report.pdf"),
            image_generation_item(image),
            final_response="two files",
        )
        await self.app.handle_completion(
            TurnOutcome(
                binding_id=binding.id,
                thread_id="native-files",
                turn_id="turn-files",
                owner_id="ou_user",
                origin=origin,
                result=result,
            )
        )
        card = self.channel.replies[-1][1]
        assert isinstance(card, OutboundCard)
        send_values = _card_button_values(card, "发送")
        send_file = next(
            value for value in send_values if str(value.get("path", "")).endswith("report.pdf")
        )
        send_image = next(
            value for value in send_values if str(value.get("path", "")).endswith("trend.png")
        )
        self.channel.send_results.extend(
            (
                sent_result(
                    "om_report_one",
                    chat_id="oc_direct",
                    thread_id="omt_files",
                    root_id="om_file_card",
                    parent_id="om_file_card",
                ),
                sent_result(
                    "om_report_two",
                    chat_id="oc_direct",
                    thread_id="omt_files",
                    root_id="om_file_card",
                    parent_id="om_file_card",
                ),
                sent_result(
                    "om_image",
                    chat_id="oc_direct",
                    thread_id="omt_files",
                    root_id="om_file_card",
                    parent_id="om_file_card",
                ),
            )
        )

        file_event = self.direct_button_event(
            send_file,
            message_id="om_file_card",
        )
        changes_before_callbacks = self.store._connection.total_changes
        replies_before_callbacks = len(self.channel.replies)
        await self.app.handle_card_action(file_event)
        await self.app.handle_card_action(file_event)
        await self.app.handle_card_action(
            self.direct_button_event(send_image, message_id="om_file_card")
        )

        first = self.channel.send_calls[0]
        second = self.channel.send_calls[1]
        third = self.channel.send_calls[2]
        self.assertEqual(first[0], "oc_direct")
        self.assertIsInstance(first[1], OutboundFile)
        self.assertIsInstance(first[1].source, MediaSource)
        self.assertEqual(Path(first[1].source.path), report.resolve())
        self.assertEqual(first[1].file_name, "report.pdf")
        self.assertEqual(first[2].reply_to, "om_file_card")
        self.assertTrue(first[2].reply_in_thread)
        self.assertEqual(first[2].reply_target_gone, "fail")
        self.assertEqual(first[2].uuid, second[2].uuid)
        self.assertLessEqual(len(first[2].uuid), 50)
        self.assertIsInstance(third[1], OutboundImage)
        self.assertEqual(Path(third[1].source.path), image.resolve())
        self.assertEqual(self.channel.updates, [])
        self.assertEqual(
            self.store._connection.total_changes,
            changes_before_callbacks,
        )
        self.assertEqual(len(self.channel.replies), replies_before_callbacks)

    async def test_missing_file_reports_in_card_topic_without_mutating_card(
        self,
    ) -> None:
        origin = await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-files")
        report = self.project / "ephemeral.txt"
        report.write_text("gone soon", encoding="utf-8")
        result = completed_turn_result(file_change_item("ephemeral.txt"))
        await self.app.handle_completion(
            TurnOutcome(
                binding_id=binding.id,
                thread_id="native-files",
                turn_id="turn-files",
                owner_id="ou_user",
                origin=origin,
                result=result,
            )
        )
        card = self.channel.replies[-1][1]
        assert isinstance(card, OutboundCard)
        send_file = _card_button_value(card, "发送")
        report.unlink()
        self.channel.send_results.append(
            sent_result(
                "om_feedback",
                chat_id="oc_direct",
                thread_id="omt_files",
                root_id="om_file_card",
                parent_id="om_file_card",
            )
        )

        await self.app.handle_card_action(
            self.direct_button_event(send_file, message_id="om_file_card")
        )

        self.assertEqual(len(self.channel.send_calls), 1)
        self.assertIsInstance(self.channel.send_calls[0][1], str)
        self.assertIn("已不可用", self.channel.send_calls[0][1])
        self.assertEqual(self.channel.updates, [])

    async def test_file_send_failure_is_reported_without_replacing_card(
        self,
    ) -> None:
        origin = await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-files")
        report = self.project / "report.txt"
        report.write_text("report", encoding="utf-8")
        result = completed_turn_result(file_change_item("report.txt"))
        await self.app.handle_completion(
            TurnOutcome(
                binding_id=binding.id,
                thread_id="native-files",
                turn_id="turn-files",
                owner_id="ou_user",
                origin=origin,
                result=result,
            )
        )
        card = self.channel.replies[-1][1]
        assert isinstance(card, OutboundCard)
        send_file = _card_button_value(card, "发送")
        self.channel.send_results.extend(
            (
                sent_result(
                    "om_failed",
                    chat_id="oc_direct",
                    success=False,
                    code=500,
                ),
                sent_result(
                    "om_feedback",
                    chat_id="oc_direct",
                    thread_id="omt_files",
                    root_id="om_file_card",
                    parent_id="om_file_card",
                ),
            )
        )
        replies_before = len(self.channel.replies)

        await self.app.handle_card_action(
            self.direct_button_event(send_file, message_id="om_file_card")
        )

        self.assertEqual(len(self.channel.send_calls), 2)
        self.assertIsInstance(self.channel.send_calls[0][1], OutboundFile)
        self.assertIsInstance(self.channel.send_calls[1][1], str)
        self.assertIn("code=500", self.channel.send_calls[1][1])
        self.assertEqual(self.channel.updates, [])
        self.assertEqual(len(self.channel.replies), replies_before)

    async def test_v3_file_button_reports_expired_without_sending_file(
        self,
    ) -> None:
        self.channel.send_results.append(
            sent_result(
                "om_expired_feedback",
                chat_id="oc_direct",
                thread_id="omt_files",
                root_id="om_legacy_card",
                parent_id="om_legacy_card",
            )
        )
        value = {
            "v": 3,
            "intent": "turn-file.send",
            "chat_id": "oc_direct",
            "scope_kind": "direct",
            "binding_id": "binding:v1:legacy-binding",
            "turn_id": "turn:v1:turn-legacy",
            "file_ref": "turn-file:v1:" + "a" * 64,
        }

        await self.app.handle_card_action(
            self.direct_button_event(value, message_id="om_legacy_card")
        )

        self.assertEqual(len(self.channel.send_calls), 1)
        self.assertIsInstance(self.channel.send_calls[0][1], str)
        self.assertIn("已过期", self.channel.send_calls[0][1])

    async def test_turn_file_reply_validator_checks_exact_topic_relationship(
        self,
    ) -> None:
        direct = channel_app.TurnFileActionIntent(
            scope=FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT),
            source_id="om_card",
            sender_id="ou_user",
            name=channel_app.TurnFileActionName.SEND,
            binding_id="binding-one",
            turn_id="turn-one",
            path="/tmp/report.pdf",
        )
        valid = sent_result(
            "om_file",
            chat_id="oc_direct",
            thread_id="omt_files",
            root_id="om_card",
            parent_id="om_card",
        )
        parsed = channel_app._validate_turn_file_reply(valid, intent=direct)
        self.assertEqual(parsed.message_id, "om_file")

        topic = channel_app.TurnFileActionIntent(
            scope=FeishuScope(
                "cli_test",
                "oc_group",
                ScopeKind.TOPIC,
                "omt_existing",
            ),
            source_id="om_card",
            sender_id="ou_user",
            name=channel_app.TurnFileActionName.SEND,
            binding_id="binding-one",
            turn_id="turn-one",
            path="/tmp/topic.pdf",
        )
        parsed_topic = channel_app._validate_turn_file_reply(
            sent_result(
                "om_topic_file",
                chat_id="oc_group",
                thread_id="omt_existing",
                root_id="om_existing_root",
                parent_id="om_existing_root",
            ),
            intent=topic,
        )
        self.assertEqual(parsed_topic.thread_id, "omt_existing")
        with self.assertRaises(channel_app.TurnFileError):
            channel_app._validate_turn_file_reply(
                sent_result(
                    "om_topic_file",
                    chat_id="oc_group",
                    thread_id="omt_other",
                    root_id="om_existing_root",
                    parent_id="om_card",
                ),
                intent=topic,
            )

        invalid = (
            sent_result(
                "om_file",
                chat_id="oc_other",
                thread_id="omt_files",
                root_id="om_card",
                parent_id="om_card",
            ),
            sent_result(
                "om_file",
                chat_id="oc_direct",
                thread_id=None,
                root_id="om_card",
                parent_id="om_card",
            ),
            sent_result(
                "om_file",
                chat_id="oc_direct",
                thread_id="omt_files",
                root_id=None,
                parent_id="om_card",
            ),
            sent_result(
                "om_file",
                chat_id="oc_direct",
                thread_id="omt_files",
                root_id="om_card",
                parent_id="om_other",
            ),
            sent_result(
                "om_file",
                chat_id="oc_direct",
                success=False,
                code=230071,
            ),
        )
        for result in invalid:
            with self.subTest(result=result), self.assertRaises(
                channel_app.TurnFileError
            ):
                channel_app._validate_turn_file_reply(result, intent=direct)

    async def test_thinking_reaction_pulses_while_typing_stays_visible(self) -> None:
        controller = channel_app._ReactionController(
            self.channel,
            visible_seconds=0.001,
            hidden_seconds=0.001,
        )
        try:
            self.assertTrue(
                await controller.start(
                    "turn-one",
                    "om_prompt",
                    pulse_enabled=True,
                )
            )
            async with asyncio.timeout(1):
                while self.channel.reactions.count(("om_prompt", "THINKING")) < 2:
                    await asyncio.sleep(0)
            await controller.stop("turn-one")
        finally:
            await controller.close()

        self.assertEqual(
            self.channel.reactions,
            [
                ("om_prompt", "Typing"),
                ("om_prompt", "THINKING"),
                ("om_prompt", "THINKING"),
            ],
        )
        self.assertEqual(
            self.channel.reaction_removals,
            [
                ("om_prompt", "reaction-2"),
                ("om_prompt", "reaction-3"),
                ("om_prompt", "reaction-1"),
            ],
        )

    async def test_disabled_thinking_pulse_keeps_typing_and_exact_cleanup(
        self,
    ) -> None:
        controller = channel_app._ReactionController(self.channel)
        try:
            self.assertTrue(
                await controller.start(
                    "turn-one",
                    "om_prompt",
                    pulse_enabled=False,
                )
            )
            self.assertIsNone(controller._pulses["turn-one"].task)
            await controller.stop("turn-one")
        finally:
            await controller.close()

        self.assertEqual(self.channel.reactions, [("om_prompt", "Typing")])
        self.assertEqual(
            self.channel.reaction_operations,
            [
                ("add", "om_prompt", "Typing"),
                ("remove", "om_prompt", "reaction-1"),
            ],
        )

    async def test_thinking_remove_failure_gets_one_terminal_retry(self) -> None:
        controller = channel_app._ReactionController(
            self.channel,
            visible_seconds=0.001,
            hidden_seconds=10,
        )
        self.channel.fail_once_reaction_remove = True
        try:
            with self.assertLogs("netizen.channel_app", level="ERROR"):
                self.assertTrue(
                    await controller.start(
                        "turn-one",
                        "om_prompt",
                        pulse_enabled=True,
                    )
                )
                await asyncio.wait_for(
                    self.channel.reaction_remove_attempted.wait(),
                    timeout=1,
                )
                await controller.stop("turn-one")
        finally:
            await controller.close()

        self.assertEqual(
            self.channel.reaction_removals,
            [
                ("om_prompt", "reaction-2"),
                ("om_prompt", "reaction-2"),
                ("om_prompt", "reaction-1"),
            ],
        )

    async def test_application_close_removes_both_visible_turn_reactions(self) -> None:
        controller = channel_app._ReactionController(
            self.channel,
            visible_seconds=10,
            hidden_seconds=10,
        )
        self.app._reactions = controller

        self.assertTrue(
            await controller.start(
                "turn-one",
                "om_prompt",
                pulse_enabled=True,
            )
        )
        await self.app.close()

        self.assertEqual(
            self.channel.reaction_removals,
            [
                ("om_prompt", "reaction-2"),
                ("om_prompt", "reaction-1"),
            ],
        )

    async def test_thinking_add_failure_keeps_typing_placeholder(self) -> None:
        controller = channel_app._ReactionController(
            self.channel,
            visible_seconds=10,
            hidden_seconds=10,
        )
        self.channel.fail_once_reaction_on = "THINKING"
        try:
            with self.assertLogs("netizen.channel_app", level="ERROR"):
                self.assertTrue(
                    await controller.start(
                        "turn-one",
                        "om_prompt",
                        pulse_enabled=True,
                    )
                )
            self.assertIn("turn-one", controller._pulses)
            self.assertIsNone(controller._pulses["turn-one"].task)
            await controller.stop("turn-one")
        finally:
            await controller.close()

        self.assertEqual(
            self.channel.reaction_operations,
            [
                ("add", "om_prompt", "Typing"),
                ("add", "om_prompt", "THINKING"),
                ("remove", "om_prompt", "reaction-1"),
            ],
        )

    async def test_terminal_reaction_failure_still_clears_running_state(self) -> None:
        controller = channel_app._ReactionController(
            self.channel,
            visible_seconds=10,
            hidden_seconds=10,
        )
        self.app._reactions = controller
        self.assertTrue(
            await controller.start(
                "turn-one",
                "om_prompt",
                pulse_enabled=True,
            )
        )
        self.channel.fail_once_reaction_on = "DONE"

        with self.assertLogs("netizen.channel_app", level="ERROR"):
            await self.app.handle_completion(
                TurnOutcome(
                    binding_id="binding-one",
                    thread_id="native-one",
                    turn_id="turn-one",
                    owner_id="ou_user",
                    origin=FakeMessage("hello", message_id="om_prompt"),
                    result=SimpleNamespace(
                        final_response="done",
                        status=SimpleNamespace(value="completed"),
                    ),
                    task_feedback=PULSE_ON,
                )
            )

        self.assertEqual(
            self.channel.reaction_operations,
            [
                ("add", "om_prompt", "Typing"),
                ("add", "om_prompt", "THINKING"),
                ("add", "om_prompt", "DONE"),
                ("remove", "om_prompt", "reaction-2"),
                ("remove", "om_prompt", "reaction-1"),
            ],
        )
        self.assertNotIn("turn-one", controller._pulses)

    async def test_status_is_multiline_and_shows_lazy_codex_defaults(self) -> None:
        await self.new()

        await self.app.handle_message(
            FakeMessage("/status", message_id="om_status")
        )

        self.assertEqual(
            self.channel.replies[-1][1].splitlines(),
            [
                "当前会话",
                "会话：11111111",
                "名称：新会话",
                "会话预览：暂无（首条消息后生成）",
                "Project：test",
                "Native Thread：pending（首条消息后创建）",
                "状态：idle",
                "Netizen 订阅：未建立（会话尚未物化）",
                "上下文窗口：暂无（首条消息后生成）",
                "Model：继承 Codex",
                "Effort：继承 Codex",
                "Speed：继承 Codex",
                "配置来源：Codex",
            ],
        )

    async def test_status_shows_git_branch_header_for_current_project(self) -> None:
        await self.new()

        with patch(
            "netizen.channel_app.git_branch_status",
            return_value="main...origin/main [ahead 2]",
        ) as read_branch:
            await self.app.handle_message(
                FakeMessage("/status", message_id="om_git_status")
            )

        lines = self.channel.replies[-1][1].splitlines()
        project_index = lines.index("Project：test")
        self.assertEqual(
            lines[project_index : project_index + 3],
            [
                "Project：test",
                "Git Branch：main...origin/main [ahead 2]",
                "Native Thread：pending（首条消息后创建）",
            ],
        )
        read_branch.assert_awaited_once_with(self.project.resolve())

    async def test_status_shows_transient_subscription_without_writer_claim(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        assert binding is not None
        self.store.assign_native_thread_id(binding.id, "native-one")
        self.runtime.subscription_snapshots[binding.id] = ThreadSubscriptionSnapshot(
            binding_id=binding.id,
            thread_id="native-one",
            state=ThreadSubscriptionState.RELEASED,
            release_in_seconds=None,
        )

        await self.app.handle_message(
            FakeMessage("/status", message_id="om_subscription_status")
        )

        reply = str(self.channel.replies[-1][1])
        self.assertIn("Netizen 订阅：本进程已取消", reply)
        self.assertIn("不表示 App Server writer 已立即释放", reply)

    async def test_release_command_keeps_binding_and_native_history(self) -> None:
        self.runtime.available_capabilities = frozenset({NativeCapability.RELEASE})
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        assert binding is not None
        self.store.assign_native_thread_id(binding.id, "native-one")

        await self.app.handle_message(
            FakeMessage("/release", message_id="om_release")
        )

        self.assertEqual(self.runtime.release_binding_calls, [binding.id])
        self.assertEqual(self.store.active_binding(scope.key).id, binding.id)
        self.assertEqual(self.store.get(binding.id).native_thread_id, "native-one")
        reply = str(self.channel.replies[-1][1])
        self.assertIn("已取消本进程", reply)
        self.assertIn("不表示 App Server writer 已立即释放", reply)

    async def test_release_reports_lazy_and_already_unsubscribed_states(self) -> None:
        self.runtime.available_capabilities = frozenset({NativeCapability.RELEASE})
        await self.new()
        self.runtime.release_disposition = ReleaseDisposition.NOT_MATERIALIZED
        await self.app.handle_message(
            FakeMessage("/release", message_id="om_release_lazy")
        )
        self.assertIn("尚未物化", str(self.channel.replies[-1][1]))

        binding = self.store.active_binding(
            FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT).key
        )
        assert binding is not None
        self.store.assign_native_thread_id(binding.id, "native-one")
        self.runtime.release_disposition = ReleaseDisposition.NOT_SUBSCRIBED
        await self.app.handle_message(
            FakeMessage("/release", message_id="om_release_absent")
        )
        self.assertIn("本进程当前没有", str(self.channel.replies[-1][1]))

    async def test_release_surfaces_busy_terminal_and_unknown_refusals(self) -> None:
        self.runtime.available_capabilities = frozenset({NativeCapability.RELEASE})
        await self.new()
        cases = (
            (
                ThreadRunningConfiguration("当前 Turn 正在执行，不能释放订阅。"),
                "正在执行",
            ),
            (
                ThreadBackgroundTerminalsActive(
                    "当前 Thread 仍有已登记后台终端，本次未释放订阅。"
                ),
                "后台终端",
            ),
            (
                ThreadReleaseStateUnknown("Thread 取消订阅结果未确认。"),
                "结果未确认",
            ),
        )
        for index, (error, expected) in enumerate(cases):
            with self.subTest(expected=expected):
                self.runtime.release_error = error
                await self.app.handle_message(
                    FakeMessage(
                        "/release",
                        message_id=f"om_release_error_{index}",
                    )
                )
                self.assertIn(expected, str(self.channel.replies[-1][1]))

    async def test_status_renders_bounded_native_checklist_and_stale_label(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-one")
        self.runtime.active[binding.id] = ActiveTurnSnapshot(
            binding.id,
            "native-one",
            "turn-one",
            "ou_user",
            ActiveState.RUNNING,
        )
        secret = "correct horse battery staple"
        steps = (
            TurnPlanStepSnapshot(
                f'done password: "{secret}"',
                TurnPlanStepState.COMPLETED,
            ),
            TurnPlanStepSnapshot("working", TurnPlanStepState.IN_PROGRESS),
            TurnPlanStepSnapshot("later", TurnPlanStepState.PENDING),
            *tuple(
                TurnPlanStepSnapshot(
                    ("x " * 200) if index == 0 else f"extra-{index}",
                    TurnPlanStepState.PENDING,
                )
                for index in range(11)
            ),
        )
        self.runtime.turn_progress_values[binding.id] = TurnProgressSnapshot(
            binding_id=binding.id,
            thread_id="native-one",
            turn_id="turn-one",
            steer_count=2,
            plan_available=True,
            plan_generated=True,
            plan_may_be_stale=True,
            steps=steps,
        )

        await self.app.handle_message(FakeMessage("/status", message_id="om_status"))

        reply = str(self.channel.replies[-1][1])
        self.assertIn("任务进展", reply)
        self.assertIn("已接收调整：2 次", reply)
        self.assertIn("任务清单（可能尚未反映最近一次调整）：", reply)
        self.assertIn("✓ [敏感内容已隐藏]", reply)
        self.assertNotIn(secret, reply)
        self.assertIn("→ working", reply)
        self.assertIn("○ later", reply)
        self.assertIn("… 另有 2 项未展示", reply)
        checklist_lines = reply.splitlines()
        long_line = next(line for line in checklist_lines if line.startswith("○ x x"))
        self.assertLessEqual(
            len(long_line),
            162,
        )

    async def test_status_reports_plan_gate_failure_without_affecting_turn(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.runtime.active[binding.id] = ActiveTurnSnapshot(
            binding.id,
            "native-one",
            "turn-one",
            "ou_user",
            ActiveState.RUNNING,
        )
        self.runtime.turn_progress_values[binding.id] = TurnProgressSnapshot(
            binding_id=binding.id,
            thread_id="native-one",
            turn_id="turn-one",
            steer_count=1,
            plan_available=False,
            plan_generated=False,
            plan_may_be_stale=True,
            steps=(),
        )

        await self.app.handle_message(FakeMessage("/status", message_id="om_status"))

        reply = str(self.channel.replies[-1][1])
        self.assertIn("状态：running", reply)
        self.assertIn("已接收调整：1 次", reply)
        self.assertIn("任务清单：暂不可用", reply)
        self.assertEqual(self.runtime.resolve_model_settings_calls, [])
        self.assertEqual(self.runtime.thread_metadata_calls, [])

    async def test_sessions_prefer_name_then_preview_and_show_lazy_binding(
        self,
    ) -> None:
        await self.new(message_id="om_new_one")
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        first = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(first.id, "native-one")
        self.runtime.thread_metadata_values["native-one"] = NativeThreadMetadata(
            thread_id="native-one",
            name="  Named\nThread  ",
            preview="name must win",
        )

        await self.new(message_id="om_new_two")
        second = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(second.id, "native-two")
        self.runtime.thread_metadata_values["native-two"] = NativeThreadMetadata(
            thread_id="native-two",
            name=None,
            preview="Preview\n" + "x" * 80,
        )

        await self.new(message_id="om_new_three")
        await self.app.handle_message(
            FakeMessage("/sessions", message_id="om_sessions")
        )

        card = self.channel.replies[-1][1]
        self.assertIsInstance(card, OutboundCard)
        text = str(card.card)
        self.assertIn("Named Thread", text)
        self.assertNotIn("name must win", text)
        self.assertIn("Preview " + "x" * 39 + "…", text)
        self.assertIn("新会话", text)
        self.assertIn("会话：11111111 · Project：test", text)
        self.assertIn("会话：22222222 · Project：test", text)
        self.assertIn("会话：33333333 · Project：test", text)
        self.assertIn("● 当前", text)
        self.assertIn("设为当前", text)
        self.assertEqual(len(self.runtime.thread_metadata_calls), 1)
        self.assertEqual(
            set(self.runtime.thread_metadata_calls[0]),
            {"native-one", "native-two"},
        )

    async def test_sessions_without_bindings_returns_empty_card(self) -> None:
        await self.app.handle_message(
            FakeMessage("/sessions", message_id="om_sessions_empty")
        )

        card = self.channel.replies[-1][1]
        self.assertIsInstance(card, OutboundCard)
        self.assertIn("0 个普通会话", str(card.card))
        self.assertIn("没有普通会话", str(card.card))
        self.assertEqual(self.runtime.thread_metadata_calls, [])

    async def test_status_and_sessions_share_persisted_goal_projection(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-one")
        self.runtime.goal_snapshot_value = native_goal(GoalStatus.PAUSED)

        await self.app.handle_message(
            FakeMessage("/status", message_id="om_goal_status")
        )
        status = str(self.channel.replies[-1][1])
        await self.app.handle_message(
            FakeMessage("/sessions", message_id="om_goal_sessions")
        )
        sessions = str(self.channel.replies[-1][1].card)

        self.assertIn("状态：goal-paused", status)
        self.assertIn("状态：goal-paused", sessions)

    async def test_rename_supports_direct_name_and_current_binding_form(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-one")
        self.runtime.thread_metadata_values["native-one"] = NativeThreadMetadata(
            "native-one",
            "Old name",
            "first task",
        )

        await self.app.handle_message(
            FakeMessage('/rename "Release review"', message_id="om_rename_direct")
        )
        self.assertEqual(
            self.runtime.rename_binding_calls,
            [(binding.id, "Release review")],
        )

        await self.app.handle_message(
            FakeMessage("/rename", message_id="om_rename_card")
        )
        card = self.channel.replies[-1][1]
        self.assertIsInstance(card, OutboundCard)
        field = _elements(card.card, "input")[0]["name"]
        await self.app.handle_card_action(
            self.direct_card_event(
                {field: "  Final   title  "},
                message_id="om_rename_result",
            )
        )

        self.assertEqual(
            self.runtime.rename_binding_calls[-1],
            (binding.id, "Final title"),
        )
        self.assertIn("会话已重命名", str(self.channel.updates[-1][1]))

    async def test_archive_confirmation_retains_binding_and_clears_current(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-one")
        self.runtime.thread_metadata_values["native-one"] = NativeThreadMetadata(
            "native-one",
            "Release review",
            "first task",
        )

        await self.app.handle_message(
            FakeMessage("/archive", message_id="om_archive")
        )
        card = self.channel.replies[-1][1]
        button = next(
            item
            for item in _elements(card.card, "button")
            if item["text"]["content"] == "确认归档当前会话"
        )
        await self.app.handle_card_action(
            self.direct_button_event(
                button["behaviors"][0]["value"],
                message_id="om_archive_card",
            )
        )

        self.assertEqual(self.runtime.archive_binding_calls, [binding.id])
        self.assertIsNone(self.store.active_binding(scope.key))
        self.assertEqual(self.store.get(binding.id).native_thread_id, "native-one")
        self.assertIn("会话已归档", str(self.channel.updates[-1][1]))

    async def test_stale_archive_confirmation_cannot_touch_new_current_binding(self) -> None:
        await self.new(message_id="om_new_first")
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        first = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(first.id, "native-one")
        await self.app.handle_message(
            FakeMessage("/archive", message_id="om_archive")
        )
        card = self.channel.replies[-1][1]
        button = next(
            item
            for item in _elements(card.card, "button")
            if item["text"]["content"] == "确认归档当前会话"
        )
        await self.new(message_id="om_new_second")

        await self.app.handle_card_action(
            self.direct_button_event(
                button["behaviors"][0]["value"],
                message_id="om_stale_archive",
            )
        )

        self.assertEqual(self.runtime.archive_binding_calls, [])
        self.assertNotEqual(self.store.active_binding(scope.key).id, first.id)
        self.assertIn("active 会话已切换", str(self.channel.updates[-1][1]))

    async def test_delete_lazy_binding_requires_confirmation_then_removes_it(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)

        await self.app.handle_message(
            FakeMessage("/delete", message_id="om_delete")
        )
        card = self.channel.replies[-1][1]
        self.assertIn("只删除本地 Binding", str(card.card))
        button = next(
            item
            for item in _elements(card.card, "button")
            if item["text"]["content"] == "永久删除当前会话"
        )
        await self.app.handle_card_action(
            self.direct_button_event(
                button["behaviors"][0]["value"],
                message_id="om_delete_card",
            )
        )

        self.assertEqual(self.runtime.delete_binding_calls, [binding.id])
        self.assertIsNone(self.store.active_binding(scope.key))
        with self.assertRaises(BindingNotFound):
            self.store.get(binding.id)
        self.assertIn("会话已永久删除", str(self.channel.updates[-1][1]))

    async def test_delete_materialized_binding_requires_exact_confirmation(
        self,
    ) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-one")
        self.runtime.available_capabilities = frozenset({NativeCapability.DELETE})
        self.runtime.thread_metadata_values["native-one"] = NativeThreadMetadata(
            "native-one",
            "Materialized",
            "first prompt",
        )

        await self.app.handle_message(
            FakeMessage("/delete", message_id="om_delete_materialized")
        )

        card = self.channel.replies[-1][1]
        serialized = str(card.card)
        self.assertIn("原生 Codex Thread", serialized)
        self.assertIn("native-thread:v1:native-one", serialized)
        self.assertEqual(self.runtime.thread_metadata_calls, [("native-one",)])
        button = next(
            item
            for item in _elements(card.card, "button")
            if item["text"]["content"] == "永久删除当前会话"
        )

        await self.app.handle_card_action(
            self.direct_button_event(
                button["behaviors"][0]["value"],
                message_id="om_delete_materialized_card",
            )
        )

        self.assertEqual(self.runtime.delete_binding_calls, [binding.id])
        with self.assertRaises(BindingNotFound):
            self.store.get(binding.id)
        self.assertIn("原生 Codex 会话", str(self.channel.updates[-1][1]))

    async def test_delete_materialized_binding_is_unavailable_without_capability(
        self,
    ) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-one")

        await self.app.handle_message(
            FakeMessage("/delete", message_id="om_delete_unavailable")
        )

        reply = str(self.channel.replies[-1][1])
        self.assertIn("Thread Delete 兼容契约未通过", reply)
        self.assertIn("本次未调用 Codex", reply)
        self.assertEqual(self.runtime.thread_metadata_calls, [])
        self.assertEqual(self.runtime.delete_binding_calls, [])
        self.assertEqual(self.store.get(binding.id).native_thread_id, "native-one")

    async def test_lazy_delete_card_cannot_delete_binding_materialized_before_click(
        self,
    ) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        await self.app.handle_message(
            FakeMessage("/delete", message_id="om_delete_lazy_race")
        )
        card = self.channel.replies[-1][1]
        button = next(
            item
            for item in _elements(card.card, "button")
            if item["text"]["content"] == "永久删除当前会话"
        )
        self.store.assign_native_thread_id(binding.id, "native-raced")

        await self.app.handle_card_action(
            self.direct_button_event(
                button["behaviors"][0]["value"],
                message_id="om_delete_lazy_race_card",
            )
        )

        self.assertEqual(self.runtime.delete_binding_calls, [])
        self.assertEqual(
            self.store.get(binding.id).native_thread_id,
            "native-raced",
        )
        self.assertIn("active 会话已切换", str(self.channel.updates[-1][1]))

    async def test_stale_delete_confirmation_cannot_delete_new_current_binding(self) -> None:
        await self.new(message_id="om_new_first")
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        first = self.store.active_binding(scope.key)
        await self.app.handle_message(
            FakeMessage("/delete", message_id="om_delete")
        )
        card = self.channel.replies[-1][1]
        button = next(
            item
            for item in _elements(card.card, "button")
            if item["text"]["content"] == "永久删除当前会话"
        )
        await self.new(message_id="om_new_second")
        second = self.store.active_binding(scope.key)

        await self.app.handle_card_action(
            self.direct_button_event(
                button["behaviors"][0]["value"],
                message_id="om_stale_delete",
            )
        )

        self.assertEqual(self.runtime.delete_binding_calls, [])
        self.assertEqual(self.store.active_binding(scope.key).id, second.id)
        self.assertEqual(self.store.get(first.id).id, first.id)
        self.assertIn("active 会话已切换", str(self.channel.updates[-1][1]))

    async def test_archived_sessions_are_separate_and_restore_switches_current(self) -> None:
        await self.new(message_id="om_new_archived")
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        archived_binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(archived_binding.id, "native-one")
        self.store.deactivate(
            scope_key=scope.key,
            binding_id=archived_binding.id,
        )
        self.runtime.archived_thread_metadata_values["native-one"] = (
            NativeThreadMetadata("native-one", "Archived work", "old task")
        )
        await self.new(message_id="om_new_current")
        current = self.store.active_binding(scope.key)

        await self.app.handle_message(
            FakeMessage("/sessions", message_id="om_sessions")
        )
        ordinary = self.channel.replies[-1][1]
        self.assertIsInstance(ordinary, OutboundCard)
        ordinary_text = str(ordinary.card)
        self.assertIn(current.short_id, ordinary_text)
        self.assertNotIn(archived_binding.short_id, ordinary_text)

        await self.app.handle_message(
            FakeMessage("/sessions archived", message_id="om_archived")
        )
        card = self.channel.replies[-1][1]
        self.assertIsInstance(card, OutboundCard)
        self.assertIn("Archived work", str(card.card))
        button = next(
            item
            for item in _elements(card.card, "button")
            if item["text"]["content"] == "恢复并切换"
        )

        await self.app.handle_message(
            FakeMessage(
                f"/resume {archived_binding.short_id}",
                message_id="om_resume_archived",
            )
        )
        self.assertIn("已归档", str(self.channel.replies[-1][1]))
        self.assertEqual(self.store.active_binding(scope.key).id, current.id)

        await self.app.handle_card_action(
            self.direct_button_event(
                button["behaviors"][0]["value"],
                message_id="om_unarchive_card",
            )
        )
        self.assertEqual(
            self.runtime.unarchive_binding_calls,
            [archived_binding.id],
        )
        self.assertEqual(
            self.store.active_binding(scope.key).id,
            archived_binding.id,
        )
        self.assertEqual(
            self.runtime.active_binding_change_calls[-1],
            (current.id, archived_binding.id),
        )
        self.assertIn("会话已恢复并切换", str(self.channel.updates[-1][1]))

    async def test_archived_sessions_support_two_stage_permanent_delete(
        self,
    ) -> None:
        await self.new(message_id="om_new_archived_delete")
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        archived_binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(archived_binding.id, "native-one")
        self.store.deactivate(
            scope_key=scope.key,
            binding_id=archived_binding.id,
        )
        self.runtime.archived_thread_metadata_values["native-one"] = (
            NativeThreadMetadata("native-one", "Archived work", "old task")
        )
        self.runtime.available_capabilities = frozenset({NativeCapability.DELETE})
        await self.new(message_id="om_new_current_after_archive")
        current = self.store.active_binding(scope.key)

        await self.app.handle_message(
            FakeMessage(
                "/sessions archived",
                message_id="om_archived_delete",
            )
        )
        card = self.channel.replies[-1][1]
        prepare = next(
            button
            for button in _elements(card.card, "button")
            if button["text"]["content"] == "删除"
        )
        await self.app.handle_card_action(
            self.direct_button_event(
                prepare["behaviors"][0]["value"],
                message_id="om_archived_delete",
            )
        )
        confirmation = self.channel.updates[-1][1]
        self.assertIn("spawned descendants", str(confirmation))
        delete = next(
            button
            for button in _elements(confirmation, "button")
            if button["text"]["content"] == "永久删除已归档会话"
        )
        with (
            patch.object(
                self.app,
                "_archived_sessions_card",
                side_effect=RuntimeError("catalog refresh failed"),
            ),
            self.assertLogs("netizen.channel_app", level="ERROR"),
        ):
            await self.app.handle_card_action(
                self.direct_button_event(
                    delete["behaviors"][0]["value"],
                    message_id="om_archived_delete",
                )
            )

        with self.assertRaises(BindingNotFound):
            self.store.get(archived_binding.id)
        self.assertEqual(self.store.active_binding(scope.key).id, current.id)
        self.assertNotIn(
            "native-one",
            self.runtime.archived_thread_metadata_values,
        )
        self.assertIn("已永久删除归档会话", str(self.channel.replies[-1][1]))

    async def test_sessions_activate_button_switches_and_refreshes_in_place(self) -> None:
        await self.new(message_id="om_new_one")
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        first = self.store.active_binding(scope.key)
        first_context_revision = first.context_revision
        self.store.assign_native_thread_id(first.id, "native-one")
        self.runtime.thread_metadata_values["native-one"] = NativeThreadMetadata(
            "native-one", "First", "task one",
        )
        await self.new(message_id="om_new_two")
        second = self.store.active_binding(scope.key)

        await self.app.handle_message(
            FakeMessage("/sessions", message_id="om_sessions")
        )
        card = self.channel.replies[-1][1]
        self.assertIsInstance(card, OutboundCard)
        buttons = _elements(card.card, "button")
        activate = next(
            b for b in buttons if b["text"]["content"] == "设为当前"
        )

        await self.app.handle_card_action(
            self.direct_button_event(
                activate["behaviors"][0]["value"],
                message_id="om_sessions",
            )
        )

        self.assertEqual(self.store.active_binding(scope.key).id, first.id)
        self.assertEqual(
            self.runtime.active_binding_change_calls[-1],
            (second.id, first.id),
        )
        activated = self.store.get(first.id)
        self.assertIsNone(activated.context_anchor)
        self.assertEqual(activated.context_revision, first_context_revision)
        self.assertEqual(self.message_history.resolve_calls, [])
        updated = self.channel.updates[-1][1]
        self.assertIn("已切换到会话", str(updated))
        self.assertIn("● 当前", str(updated))

    async def test_sessions_activate_catch_up_resets_boundary_to_exact_card(
        self,
    ) -> None:
        scope = FeishuScope("cli_test", "oc_group", ScopeKind.GROUP)
        old_anchor = MessageContextAnchor("om_old_boundary", 1_000)
        target = await self.app._management.create_current_binding(
            scope=scope,
            creator_id="ou_user",
            project_alias="test",
            message_context_mode=MentionContextMode.CATCH_UP,
            context_anchor=old_anchor,
        )
        current = await self.create_binding(scope)
        await self.app.handle_message(
            FakeMessage(
                "/sessions",
                message_id="om_sessions_request",
                chat_id=scope.chat_id,
                chat_type="group",
            )
        )
        card = self.channel.replies[-1][1]
        activate = next(
            button
            for button in _elements(card.card, "button")
            if button["text"]["content"] == "设为当前"
            and button["behaviors"][0]["value"]["binding_id"]
            == f"binding:v1:{target.binding.id}"
        )
        exact_card_anchor = MessageContextAnchor("om_sessions_card", 7_000)
        self.message_history.anchors[exact_card_anchor.message_id] = exact_card_anchor

        await self.app.handle_card_action(
            self.group_button_event(
                activate["behaviors"][0]["value"],
                message_id=exact_card_anchor.message_id,
                chat_id=scope.chat_id,
            )
        )

        activated = self.store.active_binding(scope.key)
        self.assertEqual(activated.id, target.binding.id)
        self.assertEqual(activated.context_anchor, exact_card_anchor)
        self.assertEqual(
            activated.context_revision,
            target.binding.context_revision + 1,
        )
        self.assertEqual(
            self.message_history.resolve_calls,
            [(scope, exact_card_anchor.message_id)],
        )
        self.assertEqual(
            self.runtime.active_binding_change_calls[-1],
            (current.binding.id, target.binding.id),
        )
        self.assertIn("已切换到会话", str(self.channel.updates[-1][1]))

    async def test_sessions_activate_catch_up_fails_before_pointer_change_when_anchor_is_unavailable(
        self,
    ) -> None:
        scope = FeishuScope("cli_test", "oc_group", ScopeKind.GROUP)
        old_anchor = MessageContextAnchor("om_old_boundary", 1_000)
        target = await self.app._management.create_current_binding(
            scope=scope,
            creator_id="ou_user",
            project_alias="test",
            message_context_mode=MentionContextMode.CATCH_UP,
            context_anchor=old_anchor,
        )
        current = await self.create_binding(scope)
        await self.app.handle_message(
            FakeMessage(
                "/sessions",
                message_id="om_sessions_request",
                chat_id=scope.chat_id,
                chat_type="group",
            )
        )
        card = self.channel.replies[-1][1]
        activate = next(
            button
            for button in _elements(card.card, "button")
            if button["text"]["content"] == "设为当前"
            and button["behaviors"][0]["value"]["binding_id"]
            == f"binding:v1:{target.binding.id}"
        )
        pointer_changes = list(self.runtime.active_binding_change_calls)

        with patch.object(self.app, "_message_history", None):
            await self.app.handle_card_action(
                self.group_button_event(
                    activate["behaviors"][0]["value"],
                    message_id="om_sessions_card",
                    chat_id=scope.chat_id,
                )
            )

        self.assertEqual(self.store.active_binding(scope.key).id, current.binding.id)
        unchanged = self.store.get(target.binding.id)
        self.assertEqual(unchanged.context_anchor, old_anchor)
        self.assertEqual(
            unchanged.context_revision,
            target.binding.context_revision,
        )
        self.assertEqual(self.runtime.active_binding_change_calls, pointer_changes)
        self.assertIn("群聊上下文读取能力尚不可用", str(self.channel.updates[-1][1]))

    async def test_sessions_activate_does_not_stop_old_running_turn(self) -> None:
        await self.new(message_id="om_new_one")
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        first = self.store.active_binding(scope.key)
        self.runtime.active[first.id] = ActiveTurnSnapshot(
            first.id, "native-one", "turn-one", "ou_user", ActiveState.RUNNING,
        )
        await self.new(message_id="om_new_two")
        second = self.store.active_binding(scope.key)

        await self.app.handle_message(
            FakeMessage("/sessions", message_id="om_sessions")
        )
        card = self.channel.replies[-1][1]
        activate = next(
            b
            for b in _elements(card.card, "button")
            if b["text"]["content"] == "设为当前"
        )
        await self.app.handle_card_action(
            self.direct_button_event(
                activate["behaviors"][0]["value"],
                message_id="om_sessions",
            )
        )

        self.assertEqual(self.store.active_binding(scope.key).id, first.id)
        self.assertIn(first.id, self.runtime.active)

    async def test_sessions_activate_refresh_failure_still_reports_success(self) -> None:
        await self.new(message_id="om_new_one")
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        first = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(first.id, "native-one")
        await self.new(message_id="om_new_two")

        await self.app.handle_message(
            FakeMessage("/sessions", message_id="om_sessions")
        )
        card = self.channel.replies[-1][1]
        activate = next(
            b
            for b in _elements(card.card, "button")
            if b["text"]["content"] == "设为当前"
        )

        self.runtime.thread_metadata_error = RuntimeError("history down")
        await self.app.handle_card_action(
            self.direct_button_event(
                activate["behaviors"][0]["value"],
                message_id="om_sessions",
            )
        )

        self.assertEqual(self.store.active_binding(scope.key).id, first.id)
        reply = self.channel.replies[-1][1]
        self.assertIn("已切换到会话", reply)

    async def test_sessions_activate_rejects_cross_scope_binding(self) -> None:
        await self.new(message_id="om_new_one")
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        # Create a second binding in the same scope so the card renders a
        # "设为当前" button (the active binding has no switch button).
        await self.new(message_id="om_new_two")
        other_scope = FeishuScope("cli_test", "oc_other", ScopeKind.DIRECT)
        other = self.store.create_binding(
            scope=other_scope,
            project_alias="test",
            creator_id="ou_other",
        )

        await self.app.handle_message(
            FakeMessage("/sessions", message_id="om_sessions")
        )
        card = self.channel.replies[-1][1]
        activate = next(
            b
            for b in _elements(card.card, "button")
            if b["text"]["content"] == "设为当前"
        )
        value = dict(activate["behaviors"][0]["value"])
        # Keep the current scope envelope but point at another scope's binding.
        value["binding_id"] = f"binding:v1:{other.id}"

        before_active = self.store.active_binding(scope.key).id
        await self.app.handle_card_action(
            self.direct_button_event(value, message_id="om_sessions")
        )
        # The active binding pointer must not change for a cross-scope binding.
        self.assertEqual(self.store.active_binding(scope.key).id, before_active)
        self.assertIn("操作失败", str(self.channel.updates[-1][1]))

    async def test_sessions_activate_lazy_binding_succeeds(self) -> None:
        await self.new(message_id="om_new_lazy")
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        lazy = self.store.active_binding(scope.key)
        await self.new(message_id="om_new_other")

        await self.app.handle_message(
            FakeMessage("/sessions", message_id="om_sessions")
        )
        card = self.channel.replies[-1][1]
        activate = next(
            b
            for b in _elements(card.card, "button")
            if b["text"]["content"] == "设为当前"
        )
        await self.app.handle_card_action(
            self.direct_button_event(
                activate["behaviors"][0]["value"],
                message_id="om_sessions",
            )
        )

        self.assertEqual(self.store.active_binding(scope.key).id, lazy.id)
        self.assertIn("已切换到会话", str(self.channel.updates[-1][1]))

    def test_card_action_lock_matcher_uses_numeric_codes_not_platform_text(
        self,
    ) -> None:
        self.assertTrue(
            channel_app._is_feishu_card_action_lock(card_action_lock_result())
        )
        self.assertFalse(
            channel_app._is_feishu_card_action_lock(
                card_action_lock_result(outer_code=230098)
            )
        )
        self.assertFalse(
            channel_app._is_feishu_card_action_lock(
                card_action_lock_result(inner_code=11311)
            )
        )
        self.assertFalse(
            channel_app._is_feishu_card_action_lock(
                failed_reply_result(
                    code=230099,
                    message="Failed to create card content, ext=ErrCode: 113100;",
                )
            )
        )

    async def test_sessions_delete_prepare_exhausts_card_action_lock_retries(
        self,
    ) -> None:
        await self.new(message_id="om_new_delete_target")
        await self.app.handle_message(
            FakeMessage("/sessions", message_id="om_sessions")
        )
        card = self.channel.replies[-1][1]
        prepare = next(
            button
            for button in _elements(card.card, "button")
            if button["text"]["content"] == "删除"
        )
        self.channel.card_update_results.extend(
            card_action_lock_result() for _ in range(3)
        )
        delays: list[float] = []

        async def record_delay(delay: float) -> None:
            delays.append(delay)

        replies_before = len(self.channel.replies)
        with (
            patch.object(channel_app.asyncio, "sleep", record_delay),
            self.assertLogs("netizen.channel_app", level="WARNING"),
        ):
            await self.app.handle_card_action(
                self.direct_button_event(
                    prepare["behaviors"][0]["value"],
                    message_id="om_sessions",
                )
            )

        self.assertEqual(delays, [0.2, 0.5])
        self.assertEqual(len(self.channel.updates), 3)
        self.assertEqual(self.runtime.delete_binding_calls, [])
        self.assertEqual(len(self.channel.replies), replies_before + 1)
        self.assertIn("无法打开删除确认卡", str(self.channel.replies[-1][1]))

    async def test_sessions_delete_prepare_does_not_retry_other_card_error(
        self,
    ) -> None:
        await self.new(message_id="om_new_delete_target")
        await self.app.handle_message(
            FakeMessage("/sessions", message_id="om_sessions")
        )
        card = self.channel.replies[-1][1]
        prepare = next(
            button
            for button in _elements(card.card, "button")
            if button["text"]["content"] == "删除"
        )
        self.channel.card_update_results.append(
            card_action_lock_result(inner_code=11311)
        )
        delays: list[float] = []

        async def record_delay(delay: float) -> None:
            delays.append(delay)

        with (
            patch.object(channel_app.asyncio, "sleep", record_delay),
            self.assertLogs("netizen.channel_app", level="ERROR"),
        ):
            await self.app.handle_card_action(
                self.direct_button_event(
                    prepare["behaviors"][0]["value"],
                    message_id="om_sessions",
                )
            )

        self.assertEqual(delays, [])
        self.assertEqual(len(self.channel.updates), 1)
        self.assertEqual(self.runtime.delete_binding_calls, [])

    async def test_sessions_delete_lazy_is_two_stage_and_keeps_current(
        self,
    ) -> None:
        await self.new(message_id="om_new_delete_target")
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        target = self.store.active_binding(scope.key)
        await self.new(message_id="om_new_current")
        current = self.store.active_binding(scope.key)

        await self.app.handle_message(
            FakeMessage("/sessions", message_id="om_sessions")
        )
        card = self.channel.replies[-1][1]
        prepare = next(
            button
            for button in _elements(card.card, "button")
            if button["text"]["content"] == "删除"
            and button["behaviors"][0]["value"]["binding_id"]
            == f"binding:v1:{target.id}"
        )

        await self.app.handle_card_action(
            self.direct_button_event(
                prepare["behaviors"][0]["value"],
                message_id="om_sessions",
            )
        )

        self.assertEqual(self.runtime.delete_binding_calls, [])
        confirmation = self.channel.updates[-1][1]
        self.assertEqual(confirmation["header"]["template"], "red")
        self.assertIn("只永久删除本地 Binding", str(confirmation))
        final = next(
            button
            for button in _elements(confirmation, "button")
            if button["text"]["content"] == "永久删除此会话"
        )
        final_value = final["behaviors"][0]["value"]
        self.assertEqual(final_value["binding_id"], f"binding:v1:{target.id}")
        self.assertNotIn("expected_active_binding_id", final_value)
        self.assertIsNone(final_value["expected_native_thread_id"])
        self.assertIn("confirm", final)
        pointer_change_count = len(self.runtime.active_binding_change_calls)

        self.channel.card_update_results.extend(
            (
                card_action_lock_result(),
                card_action_lock_result(),
                SimpleNamespace(success=True),
            )
        )
        delays: list[float] = []

        async def record_delay(delay: float) -> None:
            delays.append(delay)

        with (
            patch.object(channel_app.asyncio, "sleep", record_delay),
            self.assertLogs("netizen.channel_app", level="WARNING"),
        ):
            await self.app.handle_card_action(
                self.direct_button_event(final_value, message_id="om_sessions")
            )

        self.assertEqual(delays, [0.2, 0.5])
        self.assertEqual(self.runtime.delete_binding_calls, [target.id])
        with self.assertRaises(BindingNotFound):
            self.store.get(target.id)
        self.assertEqual(self.store.active_binding(scope.key).id, current.id)
        self.assertEqual(
            len(self.runtime.active_binding_change_calls),
            pointer_change_count,
        )
        updated = self.channel.updates[-1][1]
        self.assertIn("✅ 已删除 Lazy 会话", str(updated))
        self.assertIn("1 个普通会话", str(updated))

    async def test_sessions_delete_materialized_current_clears_pointer(
        self,
    ) -> None:
        await self.new(message_id="om_new_delete_current")
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        target = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(target.id, "native-target")
        self.runtime.available_capabilities = frozenset({NativeCapability.DELETE})
        self.runtime.thread_metadata_values["native-target"] = NativeThreadMetadata(
            "native-target",
            "Delete target",
            "old task",
        )

        await self.app.handle_message(
            FakeMessage("/sessions", message_id="om_sessions")
        )
        card = self.channel.replies[-1][1]
        prepare = next(
            button
            for button in _elements(card.card, "button")
            if button["text"]["content"] == "删除"
        )
        await self.app.handle_card_action(
            self.direct_button_event(
                prepare["behaviors"][0]["value"],
                message_id="om_sessions",
            )
        )
        confirmation = self.channel.updates[-1][1]
        self.assertIn("spawned descendants", str(confirmation))
        self.assertIn("Delete target", str(confirmation))
        final = next(
            button
            for button in _elements(confirmation, "button")
            if button["text"]["content"] == "永久删除此会话"
        )
        self.assertEqual(
            final["behaviors"][0]["value"]["expected_native_thread_id"],
            "native-thread:v1:native-target",
        )

        await self.app.handle_card_action(
            self.direct_button_event(
                final["behaviors"][0]["value"],
                message_id="om_sessions",
            )
        )

        self.assertEqual(self.runtime.delete_binding_calls, [target.id])
        self.assertIsNone(self.store.active_binding(scope.key))
        self.assertEqual(
            self.runtime.active_binding_change_calls[-1],
            (target.id, None),
        )
        self.assertIn("0 个普通会话", str(self.channel.updates[-1][1]))

    async def test_sessions_running_delete_delegates_without_local_stop(
        self,
    ) -> None:
        await self.new(message_id="om_new_running_delete")
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-running")
        self.runtime.thread_metadata_values["native-running"] = (
            NativeThreadMetadata("native-running", "Running", "work")
        )
        self.runtime.active[binding.id] = ActiveTurnSnapshot(
            binding.id,
            "native-running",
            "turn-running",
            "ou_user",
            ActiveState.RUNNING,
        )
        self.runtime.activity_revisions[binding.id] = 5
        self.runtime.available_capabilities = frozenset({NativeCapability.DELETE})

        await self.app.handle_message(
            FakeMessage("/sessions", message_id="om_sessions_running_delete")
        )
        card = self.channel.replies[-1][1]
        prepare = next(
            button
            for button in _elements(card.card, "button")
            if button["text"]["content"] == "删除"
        )
        await self.app.handle_card_action(
            self.direct_button_event(
                prepare["behaviors"][0]["value"],
                message_id="om_sessions_running_delete",
            )
        )
        confirmation = self.channel.updates[-1][1]
        delete = next(
            button
            for button in _elements(confirmation, "button")
            if button["text"]["content"] == "永久删除此会话"
        )
        await self.app.handle_card_action(
            self.direct_button_event(
                delete["behaviors"][0]["value"],
                message_id="om_sessions_running_delete",
            )
        )

        self.assertEqual(self.runtime.stop_calls, [])
        self.assertEqual(self.runtime.delete_binding_calls, [binding.id])
        with self.assertRaises(BindingNotFound):
            self.store.get(binding.id)

    async def test_sessions_materialized_delete_is_hidden_without_capability(
        self,
    ) -> None:
        await self.new(message_id="om_new_materialized")
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        materialized = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(materialized.id, "native-target")
        await self.new(message_id="om_new_lazy")
        lazy = self.store.active_binding(scope.key)

        await self.app.handle_message(
            FakeMessage("/sessions", message_id="om_sessions")
        )
        card = self.channel.replies[-1][1]
        delete_values = [
            button["behaviors"][0]["value"]
            for button in _elements(card.card, "button")
            if button["text"]["content"] == "删除"
        ]

        self.assertEqual(len(delete_values), 1)
        self.assertEqual(delete_values[0]["binding_id"], f"binding:v1:{lazy.id}")

    async def test_sessions_delete_survives_active_pointer_change(self) -> None:
        await self.new(message_id="om_new_delete_target")
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        target = self.store.active_binding(scope.key)
        await self.new(message_id="om_new_expected_current")

        await self.app.handle_message(
            FakeMessage("/sessions", message_id="om_sessions")
        )
        card = self.channel.replies[-1][1]
        prepare = next(
            button
            for button in _elements(card.card, "button")
            if button["text"]["content"] == "删除"
            and button["behaviors"][0]["value"]["binding_id"]
            == f"binding:v1:{target.id}"
        )
        await self.app.handle_card_action(
            self.direct_button_event(
                prepare["behaviors"][0]["value"],
                message_id="om_sessions",
            )
        )
        confirmation = self.channel.updates[-1][1]
        final = next(
            button
            for button in _elements(confirmation, "button")
            if button["text"]["content"] == "永久删除此会话"
        )
        await self.new(message_id="om_new_changed_current")
        changed_current = self.store.active_binding(scope.key)

        await self.app.handle_card_action(
            self.direct_button_event(
                final["behaviors"][0]["value"],
                message_id="om_sessions",
            )
        )

        self.assertEqual(self.runtime.delete_binding_calls, [target.id])
        self.assertEqual(self.store.active_binding(scope.key).id, changed_current.id)
        with self.assertRaises(BindingNotFound):
            self.store.get(target.id)
        self.assertIn("✅ 已删除 Lazy 会话", str(self.channel.updates[-1][1]))

    async def test_sessions_delete_final_rejects_lazy_materialization(self) -> None:
        await self.new(message_id="om_new_delete_target")
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        target = self.store.active_binding(scope.key)
        await self.app.handle_message(
            FakeMessage("/sessions", message_id="om_sessions")
        )
        card = self.channel.replies[-1][1]
        prepare = next(
            button
            for button in _elements(card.card, "button")
            if button["text"]["content"] == "删除"
        )
        await self.app.handle_card_action(
            self.direct_button_event(
                prepare["behaviors"][0]["value"],
                message_id="om_sessions",
            )
        )
        confirmation = self.channel.updates[-1][1]
        final = next(
            button
            for button in _elements(confirmation, "button")
            if button["text"]["content"] == "永久删除此会话"
        )
        self.store.assign_native_thread_id(target.id, "native-raced")

        await self.app.handle_card_action(
            self.direct_button_event(
                final["behaviors"][0]["value"],
                message_id="om_sessions",
            )
        )

        self.assertEqual(self.runtime.delete_binding_calls, [])
        self.assertEqual(
            self.store.get(target.id).native_thread_id,
            "native-raced",
        )
        self.assertIn("原生历史已变化", str(self.channel.updates[-1][1]))

    async def test_sessions_delete_surfaces_native_lifecycle_failure(
        self,
    ) -> None:
        await self.new(message_id="om_new_delete_target")
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        target = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(target.id, "native-target")
        self.runtime.available_capabilities = frozenset({NativeCapability.DELETE})
        await self.app.handle_message(
            FakeMessage("/sessions", message_id="om_sessions")
        )
        card = self.channel.replies[-1][1]
        prepare = next(
            button
            for button in _elements(card.card, "button")
            if button["text"]["content"] == "删除"
        )
        await self.app.handle_card_action(
            self.direct_button_event(
                prepare["behaviors"][0]["value"],
                message_id="om_sessions",
            )
        )
        confirmation = self.channel.updates[-1][1]
        final = next(
            button
            for button in _elements(confirmation, "button")
            if button["text"]["content"] == "永久删除此会话"
        )
        self.runtime.delete_binding_error = ThreadLifecycleError(
            "Codex 删除请求失败。"
        )

        await self.app.handle_card_action(
            self.direct_button_event(
                final["behaviors"][0]["value"],
                message_id="om_sessions",
            )
        )

        self.assertEqual(self.runtime.delete_binding_calls, [])
        self.assertEqual(self.store.get(target.id).id, target.id)
        self.assertIn("Codex 删除请求失败", str(self.channel.updates[-1][1]))

    async def test_sessions_delete_refresh_failure_still_reports_success(
        self,
    ) -> None:
        await self.new(message_id="om_new_delete_target")
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        target = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(target.id, "native-target")
        self.runtime.thread_metadata_values["native-target"] = NativeThreadMetadata(
            "native-target", "Delete target", "old task"
        )
        await self.new(message_id="om_new_current")
        current = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(current.id, "native-current")
        self.runtime.thread_metadata_values["native-current"] = NativeThreadMetadata(
            "native-current", "Current", "current task"
        )
        self.runtime.available_capabilities = frozenset({NativeCapability.DELETE})
        await self.app.handle_message(
            FakeMessage("/sessions", message_id="om_sessions")
        )
        card = self.channel.replies[-1][1]
        prepare = next(
            button
            for button in _elements(card.card, "button")
            if button["text"]["content"] == "删除"
            and button["behaviors"][0]["value"]["binding_id"]
            == f"binding:v1:{target.id}"
        )
        await self.app.handle_card_action(
            self.direct_button_event(
                prepare["behaviors"][0]["value"],
                message_id="om_sessions",
            )
        )
        confirmation = self.channel.updates[-1][1]
        final = next(
            button
            for button in _elements(confirmation, "button")
            if button["text"]["content"] == "永久删除此会话"
        )
        self.runtime.thread_metadata_error = RuntimeError("history down")

        with self.assertLogs("netizen.channel_app", level="WARNING"):
            await self.app.handle_card_action(
                self.direct_button_event(
                    final["behaviors"][0]["value"],
                    message_id="om_sessions",
                )
            )

        self.assertEqual(self.runtime.delete_binding_calls, [target.id])
        self.assertEqual(self.store.active_binding(scope.key).id, current.id)
        reply = self.channel.replies[-1][1]
        self.assertIsInstance(reply, str)
        self.assertIn("✅ 已永久删除原生 Codex 会话", reply)

    async def test_sessions_delete_refresh_clamps_removed_last_page(self) -> None:
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        self.store._id_factory = lambda: str(uuid.uuid4())
        for i in range(11):
            await self.new(message_id=f"om_new_delete_page_{i}")

        await self.app.handle_message(
            FakeMessage("/sessions", message_id="om_sessions")
        )
        first_page = self.channel.replies[-1][1]
        next_value = next(
            button
            for button in _elements(first_page.card, "button")
            if button["text"]["content"] == "下一页"
        )["behaviors"][0]["value"]
        await self.app.handle_card_action(
            self.direct_button_event(next_value, message_id="om_sessions")
        )
        second_page = self.channel.updates[-1][1]
        prepare = next(
            button
            for button in _elements(second_page, "button")
            if button["text"]["content"] == "删除"
        )
        self.assertEqual(prepare["behaviors"][0]["value"]["page"], 1)
        await self.app.handle_card_action(
            self.direct_button_event(
                prepare["behaviors"][0]["value"],
                message_id="om_sessions",
            )
        )
        confirmation = self.channel.updates[-1][1]
        final = next(
            button
            for button in _elements(confirmation, "button")
            if button["text"]["content"] == "永久删除此会话"
        )
        self.assertEqual(final["behaviors"][0]["value"]["page"], 1)

        await self.app.handle_card_action(
            self.direct_button_event(
                final["behaviors"][0]["value"],
                message_id="om_sessions",
            )
        )

        clamped = self.channel.updates[-1][1]
        clamped_text = str(clamped)
        self.assertIn("10 个普通会话", clamped_text)
        self.assertNotIn("上一页", clamped_text)
        self.assertNotIn("下一页", clamped_text)

    async def test_sessions_archives_exact_inactive_binding_and_keeps_current(
        self,
    ) -> None:
        await self.new(message_id="om_new_archive_target")
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        target = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(target.id, "native-target")
        self.runtime.thread_metadata_values["native-target"] = NativeThreadMetadata(
            "native-target",
            "Archive target",
            "old task",
        )
        await self.new(message_id="om_new_current")
        current = self.store.active_binding(scope.key)

        await self.app.handle_message(
            FakeMessage("/sessions", message_id="om_sessions")
        )
        card = self.channel.replies[-1][1]
        archive = next(
            button
            for button in _elements(card.card, "button")
            if button["text"]["content"] == "归档"
        )
        value = archive["behaviors"][0]["value"]
        self.assertEqual(value["binding_id"], f"binding:v1:{target.id}")
        self.assertNotIn("expected_active_binding_id", value)
        pointer_change_count = len(self.runtime.active_binding_change_calls)

        await self.app.handle_card_action(
            self.direct_button_event(value, message_id="om_sessions")
        )

        self.assertEqual(self.runtime.archive_binding_calls, [target.id])
        self.assertEqual(self.store.active_binding(scope.key).id, current.id)
        self.assertEqual(
            len(self.runtime.active_binding_change_calls),
            pointer_change_count,
        )
        self.assertIn(
            "native-target",
            self.runtime.archived_thread_metadata_values,
        )
        updated = self.channel.updates[-1][1]
        self.assertIn("✅ 已归档会话", str(updated))
        self.assertIn("1 个普通会话", str(updated))
        self.assertNotIn("Archive target", str(updated))

    async def test_sessions_running_archive_delegates_without_local_stop(self) -> None:
        await self.new(message_id="om_new_running_archive")
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-running")
        self.runtime.thread_metadata_values["native-running"] = (
            NativeThreadMetadata("native-running", "Running", "work")
        )
        self.runtime.active[binding.id] = ActiveTurnSnapshot(
            binding.id,
            "native-running",
            "turn-running",
            "ou_user",
            ActiveState.RUNNING,
        )
        self.runtime.activity_revisions[binding.id] = 8

        await self.app.handle_message(
            FakeMessage("/sessions", message_id="om_sessions_running_archive")
        )
        card = self.channel.replies[-1][1]
        archive = next(
            button
            for button in _elements(card.card, "button")
            if button["text"]["content"] == "归档"
        )
        await self.app.handle_card_action(
            self.direct_button_event(
                archive["behaviors"][0]["value"],
                message_id="om_sessions_running_archive",
            )
        )

        self.assertEqual(self.runtime.stop_calls, [])
        self.assertEqual(self.runtime.archive_binding_calls, [binding.id])
        self.assertIsNone(self.store.active_binding(scope.key))

    async def test_sessions_unavailable_row_exposes_recheck_and_lifecycle(self) -> None:
        await self.new(message_id="om_new_recovery")
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-recovery")
        self.runtime.thread_metadata_values["native-recovery"] = (
            NativeThreadMetadata("native-recovery", "Recovery", "work")
        )
        self.runtime.active[binding.id] = ActiveTurnSnapshot(
            binding.id,
            "native-recovery",
            "turn-unavailable",
            "ou_user",
            ActiveState.OBSERVATION_UNAVAILABLE,
        )
        self.runtime.activity_revisions[binding.id] = 12
        self.runtime.available_capabilities = frozenset({NativeCapability.DELETE})

        await self.app.handle_message(
            FakeMessage("/sessions", message_id="om_sessions_recovery")
        )
        card = self.channel.replies[-1][1]
        labels = {
            button["text"]["content"]
            for button in _elements(card.card, "button")
        }
        self.assertTrue({"归档", "删除", "停止", "重新检查"} <= labels)
        recheck = next(
            button
            for button in _elements(card.card, "button")
            if button["text"]["content"] == "重新检查"
        )
        await self.app.handle_card_action(
            self.direct_button_event(
                recheck["behaviors"][0]["value"],
                message_id="om_sessions_recovery",
            )
        )

        self.assertEqual(
            self.runtime.recheck_calls,
            [(binding.id, 12, "turn-unavailable")],
        )
        self.assertIn(
            "已启动一次有界的 exact Turn 状态重读",
            str(self.channel.updates[-1][1]),
        )

    async def test_sessions_archives_current_binding_and_clears_pointer(
        self,
    ) -> None:
        await self.new(message_id="om_new_current")
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        current = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(current.id, "native-current")
        self.runtime.thread_metadata_values["native-current"] = NativeThreadMetadata(
            "native-current",
            "Current work",
            "current task",
        )

        await self.app.handle_message(
            FakeMessage("/sessions", message_id="om_sessions")
        )
        card = self.channel.replies[-1][1]
        archive = next(
            button
            for button in _elements(card.card, "button")
            if button["text"]["content"] == "归档"
        )
        value = archive["behaviors"][0]["value"]
        self.assertNotIn("expected_active_binding_id", value)

        await self.app.handle_card_action(
            self.direct_button_event(value, message_id="om_sessions")
        )

        self.assertEqual(self.runtime.archive_binding_calls, [current.id])
        self.assertIsNone(self.store.active_binding(scope.key))
        self.assertEqual(
            self.runtime.active_binding_change_calls[-1],
            (current.id, None),
        )
        updated = self.channel.updates[-1][1]
        self.assertIn("0 个普通会话", str(updated))
        self.assertNotIn("Current work", str(updated))

    async def test_sessions_archive_survives_active_pointer_changes(
        self,
    ) -> None:
        await self.new(message_id="om_new_archive_target")
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        target = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(target.id, "native-target")
        self.runtime.thread_metadata_values["native-target"] = NativeThreadMetadata(
            "native-target",
            "Archive target",
            "old task",
        )
        await self.new(message_id="om_new_expected_current")

        await self.app.handle_message(
            FakeMessage("/sessions", message_id="om_sessions")
        )
        card = self.channel.replies[-1][1]
        archive = next(
            button
            for button in _elements(card.card, "button")
            if button["text"]["content"] == "归档"
        )
        value = archive["behaviors"][0]["value"]

        await self.new(message_id="om_new_changed_current")
        changed_current = self.store.active_binding(scope.key)
        await self.app.handle_card_action(
            self.direct_button_event(value, message_id="om_sessions")
        )

        self.assertEqual(self.runtime.archive_binding_calls, [target.id])
        self.assertEqual(
            self.store.active_binding(scope.key).id,
            changed_current.id,
        )
        self.assertIn("✅ 已归档会话", str(self.channel.updates[-1][1]))
        self.assertEqual(
            self.store.get(target.id).native_thread_id,
            "native-target",
        )

    async def test_sessions_archive_rejects_cross_scope_binding(self) -> None:
        await self.new(message_id="om_new_archive_target")
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        target = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(target.id, "native-target")
        await self.new(message_id="om_new_current")
        current = self.store.active_binding(scope.key)
        other_scope = FeishuScope("cli_test", "oc_other", ScopeKind.DIRECT)
        other = self.store.create_binding(
            scope=other_scope,
            project_alias="test",
            creator_id="ou_other",
        )
        self.store.assign_native_thread_id(other.id, "native-other")

        await self.app.handle_message(
            FakeMessage("/sessions", message_id="om_sessions")
        )
        card = self.channel.replies[-1][1]
        archive = next(
            button
            for button in _elements(card.card, "button")
            if button["text"]["content"] == "归档"
        )
        value = dict(archive["behaviors"][0]["value"])
        value["binding_id"] = f"binding:v1:{other.id}"

        await self.app.handle_card_action(
            self.direct_button_event(value, message_id="om_sessions")
        )

        self.assertEqual(self.runtime.archive_binding_calls, [])
        self.assertEqual(self.store.active_binding(scope.key).id, current.id)
        self.assertIn("操作失败", str(self.channel.updates[-1][1]))

    async def test_sessions_archive_surfaces_native_lifecycle_failure(self) -> None:
        await self.new(message_id="om_new_archive_target")
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        target = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(target.id, "native-target")

        await self.app.handle_message(
            FakeMessage("/sessions", message_id="om_sessions")
        )
        card = self.channel.replies[-1][1]
        archive = next(
            button
            for button in _elements(card.card, "button")
            if button["text"]["content"] == "归档"
        )
        self.runtime.archive_binding_error = ThreadLifecycleError(
            "Codex 归档请求失败。"
        )

        await self.app.handle_card_action(
            self.direct_button_event(
                archive["behaviors"][0]["value"],
                message_id="om_sessions",
            )
        )

        self.assertEqual(self.runtime.archive_binding_calls, [])
        self.assertEqual(self.store.active_binding(scope.key).id, target.id)
        self.assertIn("Codex 归档请求失败", str(self.channel.updates[-1][1]))

    async def test_sessions_archive_refresh_failure_still_reports_success(
        self,
    ) -> None:
        await self.new(message_id="om_new_archive_target")
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        target = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(target.id, "native-target")
        await self.new(message_id="om_new_current")

        await self.app.handle_message(
            FakeMessage("/sessions", message_id="om_sessions")
        )
        card = self.channel.replies[-1][1]
        archive = next(
            button
            for button in _elements(card.card, "button")
            if button["text"]["content"] == "归档"
        )
        self.runtime.thread_metadata_error = RuntimeError("history down")

        with self.assertLogs("netizen.channel_app", level="WARNING"):
            await self.app.handle_card_action(
                self.direct_button_event(
                    archive["behaviors"][0]["value"],
                    message_id="om_sessions",
                )
            )

        self.assertEqual(self.runtime.archive_binding_calls, [target.id])
        reply = self.channel.replies[-1][1]
        self.assertIsInstance(reply, str)
        self.assertIn("✅ 已归档会话", reply)

    async def test_sessions_archive_refresh_clamps_removed_last_page(self) -> None:
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        self.store._id_factory = lambda: str(uuid.uuid4())
        for i in range(11):
            await self.new(message_id=f"om_new_archive_page_{i}")
            binding = self.store.active_binding(scope.key)
            self.store.assign_native_thread_id(binding.id, f"native-page-{i}")
            self.runtime.thread_metadata_values[f"native-page-{i}"] = (
                NativeThreadMetadata(
                    f"native-page-{i}",
                    f"Session {i}",
                    f"task {i}",
                )
            )

        await self.app.handle_message(
            FakeMessage("/sessions", message_id="om_sessions")
        )
        first_page = self.channel.replies[-1][1]
        next_value = next(
            button
            for button in _elements(first_page.card, "button")
            if button["text"]["content"] == "下一页"
        )["behaviors"][0]["value"]
        await self.app.handle_card_action(
            self.direct_button_event(next_value, message_id="om_sessions")
        )
        second_page = self.channel.updates[-1][1]
        archive = next(
            button
            for button in _elements(second_page, "button")
            if button["text"]["content"] == "归档"
        )
        self.assertEqual(archive["behaviors"][0]["value"]["page"], 1)

        await self.app.handle_card_action(
            self.direct_button_event(
                archive["behaviors"][0]["value"],
                message_id="om_sessions",
            )
        )

        clamped = self.channel.updates[-1][1]
        clamped_text = str(clamped)
        self.assertIn("10 个普通会话", clamped_text)
        self.assertNotIn("上一页", clamped_text)
        self.assertNotIn("下一页", clamped_text)

    async def test_sessions_page_rereads_live_list_and_clamps_out_of_range(self) -> None:
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        self.store._id_factory = lambda: str(uuid.uuid4())
        for i in range(15):
            await self.new(message_id=f"om_new_{i}")
            binding = self.store.active_binding(scope.key)
            self.store.assign_native_thread_id(binding.id, f"native-{i}")
            self.runtime.thread_metadata_values[f"native-{i}"] = NativeThreadMetadata(
                f"native-{i}", f"Session {i}", f"task {i}",
            )

        await self.app.handle_message(
            FakeMessage("/sessions", message_id="om_sessions")
        )
        first_page = self.channel.replies[-1][1]
        self.assertIn("第 1/2 页", str(first_page.card))
        next_button = next(
            b
            for b in _elements(first_page.card, "button")
            if b["text"]["content"] == "下一页"
        )
        next_value = next_button["behaviors"][0]["value"]
        self.assertEqual(next_value["page"], 1)

        await self.app.handle_card_action(
            self.direct_button_event(next_value, message_id="om_sessions")
        )
        second_page = self.channel.updates[-1][1]
        self.assertIn("第 2/2 页", str(second_page))

        # Shrink the live list to one page by archiving 8 bindings (7 remain).
        for i in range(8):
            self.runtime.archived_thread_metadata_values[f"native-{i}"] = (
                NativeThreadMetadata(f"native-{i}", "Archived", "old")
            )

        # Request a page that is now out of range; the handler must clamp it
        # to the only valid page rather than using the stale list.
        out_of_range_value = {
            "v": 4,
            "intent": "sessions.page",
            "chat_id": "oc_direct",
            "scope_kind": "direct",
            "page": 99,
            "nonce": next_value["nonce"],
        }
        await self.app.handle_card_action(
            self.direct_button_event(out_of_range_value, message_id="om_sessions")
        )
        clamped = self.channel.updates[-1][1]
        clamped_text = str(clamped)
        self.assertIn("7 个普通会话", clamped_text)
        self.assertNotIn("下一页", clamped_text)
        self.assertNotIn("上一页", clamped_text)

    async def test_unarchive_command_restores_exact_local_binding(self) -> None:
        await self.new(message_id="om_new_archived")
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        archived = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(archived.id, "native-one")
        self.store.deactivate(scope_key=scope.key, binding_id=archived.id)
        self.runtime.archived_thread_metadata_values["native-one"] = (
            NativeThreadMetadata("native-one", "Archived work", "old task")
        )
        await self.new(message_id="om_new_current")

        await self.app.handle_message(
            FakeMessage(
                f"/unarchive {archived.short_id}",
                message_id="om_unarchive",
            )
        )

        self.assertEqual(self.runtime.unarchive_binding_calls, [archived.id])
        self.assertEqual(self.store.active_binding(scope.key).id, archived.id)
        self.assertIn("已恢复并切换", str(self.channel.replies[-1][1]))

    async def test_status_shows_native_name_and_preview(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-one")
        self.runtime.thread_metadata_values["native-one"] = NativeThreadMetadata(
            thread_id="native-one",
            name="  Release\nreview ",
            preview="Check\tcurrent changes",
        )

        await self.app.handle_message(
            FakeMessage("/status", message_id="om_status")
        )

        lines = self.channel.replies[-1][1].splitlines()
        self.assertIn("名称：Release review", lines)
        self.assertIn("会话预览：Check current changes", lines)
        self.assertIn("上下文窗口：暂不可用（下次可观测 Turn 完成后更新）", lines)
        self.assertEqual(self.runtime.thread_metadata_calls, [("native-one",)])

    async def test_status_shows_observed_context_window_usage(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-one")
        self.runtime.thread_metadata_values["native-one"] = NativeThreadMetadata(
            thread_id="native-one",
            name=None,
            preview="First task",
        )
        self.runtime.context_window_usage_values[binding.id] = ContextWindowUsage(
            used_tokens=24_500,
            context_window_tokens=100_000,
        )

        await self.app.handle_message(
            FakeMessage("/status", message_id="om_status")
        )

        self.assertIn(
            "上下文窗口：24,500 / 100,000 tokens（24.5% 已用）",
            self.channel.replies[-1][1].splitlines(),
        )
        self.assertEqual(self.runtime.context_window_usage_calls, [binding.id])

    async def test_status_labels_previous_usage_while_turn_is_running(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-one")
        self.runtime.context_window_usage_values[binding.id] = ContextWindowUsage(
            used_tokens=24_500,
            context_window_tokens=100_000,
        )
        self.runtime.active[binding.id] = ActiveTurnSnapshot(
            binding_id=binding.id,
            thread_id="native-one",
            turn_id="turn-two",
            owner_id="ou_user",
            state=ActiveState.RUNNING,
        )

        await self.app.handle_message(
            FakeMessage("/status", message_id="om_status")
        )

        lines = self.channel.replies[-1][1].splitlines()
        self.assertIn("状态：running", lines)
        self.assertIn(
            "上下文窗口（上一轮完成时）：24,500 / 100,000 tokens（24.5% 已用）",
            lines,
        )

    async def test_status_keeps_usage_when_window_size_is_unavailable(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-one")
        self.runtime.context_window_usage_values[binding.id] = ContextWindowUsage(
            used_tokens=24_500,
            context_window_tokens=None,
        )

        await self.app.handle_message(
            FakeMessage("/status", message_id="om_status")
        )

        self.assertIn(
            "上下文窗口：已用 24,500 tokens（窗口大小暂不可用）",
            self.channel.replies[-1][1].splitlines(),
        )

    async def test_status_clamps_overfilled_context_percentage(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-one")
        self.runtime.context_window_usage_values[binding.id] = ContextWindowUsage(
            used_tokens=105_000,
            context_window_tokens=100_000,
        )

        await self.app.handle_message(
            FakeMessage("/status", message_id="om_status")
        )

        self.assertIn(
            "上下文窗口：105,000 / 100,000 tokens（100.0% 已用）",
            self.channel.replies[-1][1].splitlines(),
        )

    async def test_status_uses_preview_when_native_name_is_unset(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-one")
        self.runtime.thread_metadata_values["native-one"] = NativeThreadMetadata(
            thread_id="native-one",
            name=None,
            preview="First task",
        )

        await self.app.handle_message(
            FakeMessage("/status", message_id="om_status")
        )

        lines = self.channel.replies[-1][1].splitlines()
        self.assertIn("名称：未设置", lines)
        self.assertIn("会话预览：First task", lines)

    async def test_thread_metadata_failure_keeps_status_but_sessions_fail_closed(
        self,
    ) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-one")
        self.runtime.thread_metadata_error = RuntimeError("history unavailable")

        with self.assertLogs("netizen.channel_app", level="WARNING"):
            await self.app.handle_message(
                FakeMessage("/status", message_id="om_status")
            )
        status_reply = self.channel.replies[-1][1]
        self.assertIn("名称：暂不可用", status_reply)
        self.assertIn("会话预览：暂不可用", status_reply)
        self.assertIn("状态：idle", status_reply)

        await self.app.handle_message(
            FakeMessage("/sessions", message_id="om_sessions")
        )
        sessions_reply = self.channel.replies[-1][1]
        self.assertIn("无法读取 Codex 归档会话列表", sessions_reply)

    async def test_status_resolves_exact_persistent_turn_settings(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.set_turn_settings(
            binding_id=binding.id,
            expected_revision=binding.settings_revision,
            settings=BindingTurnSettings(
                "future-model",
                "ultra",
                "priority-v2",
            ),
        )

        await self.app.handle_message(
            FakeMessage("/status", message_id="om_status")
        )

        reply = self.channel.replies[-1][1]
        self.assertIn("\nModel：gpt-future-codex\n", reply)
        self.assertIn("\nEffort：ultra\n", reply)
        self.assertIn("\nSpeed：Fast v2\n", reply)
        self.assertTrue(reply.endswith("配置来源：Netizen 会话配置"))
        self.assertEqual(self.runtime.model_catalog_calls, 1)
        self.assertEqual(self.runtime.resolve_model_settings_calls, [])

    async def test_status_without_override_always_shows_codex_inheritance(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-one")

        await self.app.handle_message(
            FakeMessage("/status", message_id="om_status")
        )

        lines = self.channel.replies[-1][1].splitlines()
        self.assertIn("Native Thread：native-one", lines)
        self.assertIn(
            "Model：继承 Codex",
            lines,
        )
        self.assertIn(
            "Effort：继承 Codex",
            lines,
        )
        self.assertIn(
            "Speed：继承 Codex",
            lines,
        )
        self.assertIn("配置来源：Codex", lines)
        self.assertEqual(self.runtime.resolve_model_settings_calls, [])

    async def test_status_falls_back_to_persistent_ids_when_catalog_is_down(
        self,
    ) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.set_turn_settings(
            binding_id=binding.id,
            expected_revision=binding.settings_revision,
            settings=BindingTurnSettings(
                "future-model",
                "ultra",
                "priority-v2",
            ),
        )
        self.runtime.model_catalog_error = RuntimeError("catalog down")

        with self.assertLogs("netizen.channel_app", level="WARNING") as logs:
            await self.app.handle_message(
                FakeMessage("/status", message_id="om_status")
            )

        reply = self.channel.replies[-1][1]
        self.assertIn("\nModel：future-model\n", reply)
        self.assertIn("\nEffort：ultra\n", reply)
        self.assertIn("\nSpeed：priority-v2\n", reply)
        self.assertTrue(
            reply.endswith(
                "配置来源：Netizen 会话配置（模型目录暂不可用）"
            )
        )
        self.assertIn("model catalog unavailable", logs.output[0])

    async def test_status_marks_persistent_selection_invalid_when_catalog_changed(
        self,
    ) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.set_turn_settings(
            binding_id=binding.id,
            expected_revision=binding.settings_revision,
            settings=BindingTurnSettings(
                "removed-model",
                "removed-effort",
                "removed-tier",
            ),
        )

        await self.app.handle_message(
            FakeMessage("/status", message_id="om_status")
        )

        reply = self.channel.replies[-1][1]
        self.assertIn("\nModel：removed-model\n", reply)
        self.assertTrue(
            reply.endswith(
                "配置来源：Netizen 会话配置（已失效，请使用 /config 更新）"
            )
        )

    async def test_skills_command_is_rejected_without_native_mutation(self) -> None:
        await self.new()
        self.runtime.available_capabilities = frozenset({NativeCapability.SKILLS})

        await self.app.handle_message(FakeMessage("/skills", message_id="om_skills"))

        reply = str(self.channel.replies[-1][1])
        self.assertIn("未知命令：/skills", reply)
        self.assertEqual(self.runtime.submit_calls, [])

    async def test_multiple_skills_from_current_message_reach_one_submission(
        self,
    ) -> None:
        await self.new()
        self.runtime.available_capabilities = frozenset({NativeCapability.SKILLS})
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.runtime.submission = Submission(
            SubmitDisposition.STARTED,
            binding.id,
            "native-one",
            "turn-one",
            lambda: None,
        )

        await self.app.handle_message(
            FakeMessage(
                "$code-review $test-triage inspect",
                message_id="om_skilled",
                display_name="$old-skill Current User",
            )
        )

        call = self.runtime.submit_calls[-1]
        self.assertEqual(
            call["skill_names"],
            ("code-review", "test-triage"),
        )
        self.assertNotIn("$old-skill", call["input"])
        self.assertIn(r"\u0024old-skill", call["input"])
        self.assertEqual(len(self.runtime.submit_calls), 1)

    async def test_quoted_skill_marker_never_becomes_a_typed_reference(self) -> None:
        await self.new()
        self.runtime.available_capabilities = frozenset({NativeCapability.SKILLS})
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.runtime.submission = Submission(
            SubmitDisposition.STARTED,
            binding.id,
            "native-one",
            "turn-one",
            lambda: None,
        )
        self.channel.inbound_messages["om_old"] = quoted_inbound(
            message_id="om_old",
            content_text="$old-skill historical",
        )

        await self.app.handle_message(
            FakeMessage(
                "$new-skill current",
                message_id="om_current",
                reply_id="om_old",
            )
        )

        call = self.runtime.submit_calls[-1]
        self.assertEqual(call["skill_names"], ("new-skill",))
        self.assertNotIn("$old-skill", call["input"])
        self.assertIn(r"\u0024old-skill", call["input"])

    async def test_goal_command_starts_one_logical_operation_and_renders_card(
        self,
    ) -> None:
        await self.new()
        self.runtime.available_capabilities = frozenset({NativeCapability.GOAL})
        self.runtime.goal_snapshot_value = native_goal()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        released = False

        def release() -> None:
            nonlocal released
            released = True

        self.runtime.goal_submission = GoalSubmission(
            binding.id,
            "native-one",
            "goal-one",
            release,
        )
        self.channel.reply_results.append(
            sent_result("om_goal_card", chat_id="oc_direct")
        )

        await self.app.handle_message(
            FakeMessage("/goal ship safely", message_id="om_goal")
        )

        self.assertTrue(released)
        self.assertEqual(len(self.runtime.start_goal_calls), 1)
        self.assertEqual(
            self.runtime.start_goal_calls[0]["objective"],
            "ship safely",
        )
        self.assertIsInstance(self.channel.replies[-1][1], OutboundCard)
        self.assertIn(
            "Goal 已启动",
            json.dumps(self.channel.replies[-1][1].card, ensure_ascii=False),
        )

    async def test_goal_start_card_failure_has_visible_text_receipt(self) -> None:
        await self.new()
        self.runtime.available_capabilities = frozenset({NativeCapability.GOAL})
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.runtime.goal_snapshot_value = native_goal()
        released = False

        def release() -> None:
            nonlocal released
            released = True

        self.runtime.goal_submission = GoalSubmission(
            binding.id,
            "native-one",
            "goal-one",
            release,
        )
        self.channel.reply_results.append(RuntimeError("card send failed"))

        with self.assertLogs("netizen.channel_app", level="ERROR"):
            await self.app.handle_message(
                FakeMessage("/goal ship safely", message_id="om_goal_start_fail")
            )

        self.assertTrue(released)
        self.assertEqual(len(self.runtime.start_goal_calls), 1)
        self.assertIn("Goal 已启动并在原生 Codex 中执行", self.channel.replies[-1][1])
        self.assertIn("状态卡暂时无法展示", self.channel.replies[-1][1])

    async def test_goal_status_recovers_failed_initial_card_without_losing_terminal_result(
        self,
    ) -> None:
        await self.new()
        self.runtime.available_capabilities = frozenset({NativeCapability.GOAL})
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        running = native_goal()
        self.runtime.goal_snapshot_value = running
        self.runtime.active_goals[binding.id] = ActiveGoalSnapshot(
            binding_id=binding.id,
            thread_id="native-one",
            logical_turn_id="goal-one",
            owner_id="ou_user",
            state=GoalOperationState.RUNNING,
            persisted=running,
        )
        self.runtime.goal_submission = GoalSubmission(
            binding.id,
            "native-one",
            "goal-one",
            lambda: None,
        )
        self.channel.reply_results.append(RuntimeError("initial card failed"))

        with self.assertLogs("netizen.channel_app", level="ERROR"):
            await self.app.handle_message(
                FakeMessage("/goal ship safely", message_id="om_goal_initial_fail")
            )

        self.channel.reply_results.append(
            sent_result("om_goal_recovered", chat_id="oc_direct")
        )
        await self.app.handle_message(
            FakeMessage("/goal", message_id="om_goal_recover_status")
        )
        origin = self.runtime.start_goal_calls[0]["origin"]
        self.assertIsInstance(origin, channel_app.GoalCardOrigin)
        paused = native_goal(GoalStatus.PAUSED)

        await self.app.handle_completion(
            GoalOutcome(
                binding_id=binding.id,
                thread_id="native-one",
                logical_turn_id="goal-one",
                owner_id="ou_user",
                origin=origin,
                goal=paused,
                final_physical_turn_id="turn-final",
                final_turn_status="interrupted",
                final_response="terminal result survives recovery",
            )
        )

        self.assertEqual(self.channel.updates[-1][0], "om_goal_recovered")
        serialized = json.dumps(
            self.channel.updates[-1][1],
            ensure_ascii=False,
        )
        self.assertIn("terminal result survives recovery", serialized)

    async def test_goal_resume_card_failure_has_visible_receipts(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        paused = native_goal(GoalStatus.PAUSED)
        self.runtime.goal_snapshot_value = paused
        card = await self.register_goal_card(
            scope=scope,
            binding=binding,
            goal=paused,
            message_id="om_goal_resume_fail",
            runtime_state="goal-paused",
        )
        resume_value = next(
            behavior["value"]
            for button in _elements(card.card, "button")
            if button["text"]["content"] == "恢复 Goal"
            for behavior in button.get("behaviors", ())
        )
        self.runtime.goal_submission = GoalSubmission(
            binding.id,
            "native-one",
            "goal-resumed",
            lambda: None,
        )
        self.channel.fail_card_updates = True

        with self.assertLogs("netizen.channel_app", level="ERROR"):
            await self.app.handle_card_action(
                self.direct_button_event(
                    resume_value,
                    message_id="om_goal_resume_fail",
                )
            )

        self.assertEqual(len(self.runtime.resume_goal_calls), 1)
        self.assertIn("Goal 已恢复并在原生 Codex 中执行", self.channel.replies[-1][1])
        self.assertIn("原卡片暂时无法更新", self.channel.replies[-1][1])

    async def test_goal_resume_command_card_failure_has_text_receipt(self) -> None:
        await self.new()
        self.runtime.available_capabilities = frozenset({NativeCapability.GOAL})
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        paused = native_goal(GoalStatus.PAUSED)
        self.runtime.goal_snapshot_value = paused
        await self.register_goal_card(
            scope=scope,
            binding=binding,
            goal=paused,
            message_id="om_goal_resume_command_card",
            runtime_state="goal-paused",
        )
        self.runtime.goal_submission = GoalSubmission(
            binding.id,
            "native-one",
            "goal-resumed",
            lambda: None,
        )
        self.channel.fail_card_updates = True

        await self.app.handle_message(
            FakeMessage("/goal resume", message_id="om_goal_resume_command")
        )

        self.assertEqual(len(self.runtime.resume_goal_calls), 1)
        self.assertIn("Goal 已恢复并在原生 Codex 中执行", self.channel.replies[-1][1])
        self.assertIn("状态卡暂时无法展示", self.channel.replies[-1][1])

    async def test_goal_status_refreshes_the_canonical_running_card(self) -> None:
        await self.new()
        self.runtime.available_capabilities = frozenset({NativeCapability.GOAL})
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.runtime.goal_snapshot_value = native_goal()
        self.runtime.goal_submission = GoalSubmission(
            binding.id,
            "native-one",
            "goal-one",
            lambda: None,
        )
        self.channel.reply_results.append(
            sent_result("om_goal_canonical", chat_id="oc_direct")
        )

        await self.app.handle_message(
            FakeMessage("/goal ship safely", message_id="om_goal_start")
        )
        await self.app.handle_message(
            FakeMessage("/goal", message_id="om_goal_status")
        )

        self.assertEqual(len(self.channel.replies), 1)
        self.assertEqual(self.channel.updates[-1][0], "om_goal_canonical")

    async def test_goal_status_after_restart_registers_new_controls(self) -> None:
        await self.new()
        self.runtime.available_capabilities = frozenset({NativeCapability.GOAL})
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-one")
        paused = native_goal(GoalStatus.PAUSED)
        self.runtime.goal_snapshot_value = paused
        self.runtime.goal_submission = GoalSubmission(
            binding.id,
            "native-one",
            "goal-resumed",
            lambda: None,
        )
        self.channel.reply_results.append(
            sent_result("om_goal_restart_snapshot", chat_id="oc_direct")
        )

        await self.app.handle_message(
            FakeMessage("/goal", message_id="om_goal_restart_status")
        )

        snapshot_card = self.channel.replies[-1][1]
        self.assertIsInstance(snapshot_card, OutboundCard)
        assert isinstance(snapshot_card, OutboundCard)
        resume_value = next(
            behavior["value"]
            for button in _elements(snapshot_card.card, "button")
            if button["text"]["content"] == "恢复 Goal"
            for behavior in button.get("behaviors", ())
        )
        await self.app.handle_card_action(
            self.direct_button_event(
                resume_value,
                message_id="om_goal_restart_snapshot",
            )
        )

        self.assertEqual(len(self.runtime.resume_goal_calls), 1)
        self.assertEqual(self.channel.updates[-1][0], "om_goal_restart_snapshot")

    async def test_stale_logical_goal_completion_cannot_overwrite_resume(
        self,
    ) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        goal = native_goal(GoalStatus.ACTIVE)
        generation = goal_generation(goal)
        origin = channel_app.GoalCardOrigin(
            message_id=None,
            scope=scope,
            binding_id=binding.id,
            short_id=binding.short_id,
            project_alias=binding.project_alias,
            fallback_origin=FakeMessage("/goal", message_id="om_origin"),
        )
        first = ReplyCardProjection(
            scope=scope,
            goal=channel_app._reply_goal_module(
                binding=binding,
                goal=goal,
                runtime_state=GoalOperationState.RUNNING.value,
                notice="first run",
            ),
        )
        resumed = ReplyCardProjection(
            scope=scope,
            goal=channel_app._reply_goal_module(
                binding=binding,
                goal=goal,
                runtime_state=GoalOperationState.RUNNING.value,
                notice="resumed run",
            ),
        )
        self.channel.reply_results.append(
            sent_result("om_goal_race", chat_id="oc_direct")
        )
        self.assertTrue(
            await self.app._progress_cards.start_goal(
                binding_id=binding.id,
                thread_id="native-one",
                logical_turn_id="goal-old",
                generation=generation,
                origin=origin,
                projection=first,
                revision=(1,),
                refresh=None,
            )
        )
        self.assertTrue(
            await self.app._progress_cards.start_goal(
                binding_id=binding.id,
                thread_id="native-one",
                logical_turn_id="goal-new",
                generation=generation,
                origin=origin,
                projection=resumed,
                revision=(2,),
                refresh=None,
            )
        )
        update_count = len(self.channel.updates)

        delivered = await self.app._progress_cards.finish_goal(
            binding_id=binding.id,
            thread_id="native-one",
            logical_turn_id="goal-old",
            generation=generation,
            origin=origin,
            projection=first,
            retain_session=True,
        )

        self.assertTrue(delivered)
        self.assertEqual(len(self.channel.updates), update_count)
        session = self.app._progress_cards._goal_sessions[
            (binding.id, "native-one", generation)
        ]
        self.assertEqual(session.logical_turn_id, "goal-new")

        self.assertIs(
            await self.app._progress_cards.finish_goal(
                binding_id=binding.id,
                thread_id="native-one",
                logical_turn_id="goal-new",
                generation=generation,
                origin=origin,
                projection=resumed,
                retain_session=False,
            ),
            channel_app._GoalCardDelivery.DELIVERED,
        )
        update_count = len(self.channel.updates)
        self.assertNotIn(
            (binding.id, "native-one", generation),
            self.app._progress_cards._goal_sessions,
        )

        self.assertIs(
            await self.app._progress_cards.finish_goal(
                binding_id=binding.id,
                thread_id="native-one",
                logical_turn_id="goal-old",
                generation=generation,
                origin=origin,
                projection=first,
                retain_session=True,
            ),
            channel_app._GoalCardDelivery.SUPERSEDED,
        )
        self.assertEqual(len(self.channel.updates), update_count)
        current = self.app._progress_cards.goal_projection(
            source_id="om_goal_race",
            generation=generation,
        )
        self.assertIsNone(current)
        self.assertIn(
            "resumed run",
            json.dumps(self.channel.updates[-1][1], ensure_ascii=False),
        )

    async def test_goal_finish_does_not_deadlock_with_refresh_in_flight(self) -> None:
        await self.new()
        await self.app._progress_cards.close()
        self.app._progress_cards = channel_app._ProgressCardController(
            self.channel,
            self.runtime,  # type: ignore[arg-type]
            poll_seconds=0.01,
            operation_timeout_seconds=0.5,
        )
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        goal = native_goal(GoalStatus.ACTIVE)
        generation = goal_generation(goal)
        projection = ReplyCardProjection(
            scope=scope,
            goal=channel_app._reply_goal_module(
                binding=binding,
                goal=goal,
                runtime_state=GoalOperationState.RUNNING.value,
            ),
        )
        origin = channel_app.GoalCardOrigin(
            message_id=None,
            scope=scope,
            binding_id=binding.id,
            short_id=binding.short_id,
            project_alias=binding.project_alias,
            fallback_origin=FakeMessage("/goal", message_id="om_goal_gate"),
        )
        refresh_entered = asyncio.Event()
        release_refresh = asyncio.Event()

        async def refresh():
            refresh_entered.set()
            await release_refresh.wait()
            return (2,), projection

        self.channel.reply_results.append(
            sent_result("om_goal_gate_card", chat_id="oc_direct")
        )
        self.assertTrue(
            await self.app._progress_cards.start_goal(
                binding_id=binding.id,
                thread_id="native-one",
                logical_turn_id="goal-one",
                generation=generation,
                origin=origin,
                projection=projection,
                revision=(1,),
                refresh=refresh,
            )
        )
        async with asyncio.timeout(1):
            await refresh_entered.wait()

        finishing = asyncio.create_task(
            self.app._progress_cards.finish_goal(
                binding_id=binding.id,
                thread_id="native-one",
                logical_turn_id="goal-one",
                generation=generation,
                origin=origin,
                projection=projection,
                retain_session=False,
            )
        )
        await asyncio.sleep(0)
        self.assertFalse(finishing.done())
        release_refresh.set()

        async with asyncio.timeout(1):
            self.assertIs(
                await finishing,
                channel_app._GoalCardDelivery.DELIVERED,
            )
        self.assertEqual(self.channel.updates[-1][0], "om_goal_gate_card")

    async def test_superseded_goal_fallback_is_suppressed_before_reply(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        goal = native_goal(GoalStatus.ACTIVE)
        generation = goal_generation(goal)
        origin = channel_app.GoalCardOrigin(
            message_id=None,
            scope=scope,
            binding_id=binding.id,
            short_id=binding.short_id,
            project_alias=binding.project_alias,
            fallback_origin=FakeMessage("/goal", message_id="om_fallback_origin"),
        )
        old_projection = ReplyCardProjection(
            scope=scope,
            goal=channel_app._reply_goal_module(
                binding=binding,
                goal=goal,
                runtime_state=GoalOperationState.RUNNING.value,
                notice="old run",
            ),
        )
        new_projection = replace(
            old_projection,
            goal=replace(old_projection.goal, notice="new run"),
        )
        self.channel.reply_results.append(
            sent_result("om_goal_fallback_race", chat_id="oc_direct")
        )
        self.assertTrue(
            await self.app._progress_cards.start_goal(
                binding_id=binding.id,
                thread_id="native-one",
                logical_turn_id="goal-old",
                generation=generation,
                origin=origin,
                projection=old_projection,
                revision=(1,),
                refresh=None,
            )
        )
        self.assertTrue(
            await self.app._progress_cards.start_goal(
                binding_id=binding.id,
                thread_id="native-one",
                logical_turn_id="goal-new",
                generation=generation,
                origin=origin,
                projection=new_projection,
                revision=(2,),
                refresh=None,
            )
        )
        reply_count = len(self.channel.replies)

        delivery = await self.app._progress_cards.reply_goal_fallback(
            binding_id=binding.id,
            thread_id="native-one",
            logical_turn_id="goal-old",
            generation=generation,
            target=origin.fallback_origin,
            card=reply_card(old_projection),
            origin=origin,
            projection=old_projection,
            retain_session=True,
        )

        self.assertIs(delivery, channel_app._GoalCardDelivery.SUPERSEDED)
        self.assertEqual(len(self.channel.replies), reply_count)

    async def test_superseded_oversized_goal_result_is_not_replied(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        goal = native_goal(GoalStatus.ACTIVE)
        generation = goal_generation(goal)
        origin = channel_app.GoalCardOrigin(
            message_id=None,
            scope=scope,
            binding_id=binding.id,
            short_id=binding.short_id,
            project_alias=binding.project_alias,
            fallback_origin=FakeMessage("/goal", message_id="om_oversized_origin"),
        )
        projection = ReplyCardProjection(
            scope=scope,
            goal=channel_app._reply_goal_module(
                binding=binding,
                goal=goal,
                runtime_state=GoalOperationState.RUNNING.value,
            ),
        )
        self.channel.reply_results.append(
            sent_result("om_goal_oversized_race", chat_id="oc_direct")
        )
        self.assertTrue(
            await self.app._progress_cards.start_goal(
                binding_id=binding.id,
                thread_id="native-one",
                logical_turn_id="goal-old",
                generation=generation,
                origin=origin,
                projection=projection,
                revision=(1,),
                refresh=None,
            )
        )
        self.assertTrue(
            await self.app._progress_cards.start_goal(
                binding_id=binding.id,
                thread_id="native-one",
                logical_turn_id="goal-new",
                generation=generation,
                origin=origin,
                projection=projection,
                revision=(2,),
                refresh=None,
            )
        )
        reply_count = len(self.channel.replies)
        update_count = len(self.channel.updates)

        await self.app.handle_completion(
            GoalOutcome(
                binding_id=binding.id,
                thread_id="native-one",
                logical_turn_id="goal-old",
                owner_id="ou_user",
                origin=origin,
                goal=native_goal(GoalStatus.PAUSED),
                final_physical_turn_id="turn-old-final",
                final_turn_status="interrupted",
                final_response="x" * 100_001,
            )
        )

        self.assertEqual(len(self.channel.replies), reply_count)
        self.assertEqual(len(self.channel.updates), update_count)

    async def test_goal_session_projection_survives_cache_eviction(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        goal = native_goal(GoalStatus.PAUSED)
        generation = goal_generation(goal)
        projection = ReplyCardProjection(
            scope=scope,
            goal=channel_app._reply_goal_module(
                binding=binding,
                goal=goal,
                runtime_state="goal-paused",
            ),
            result=ReplyCardResultModule("retained result"),
        )
        origin = channel_app.GoalCardOrigin(
            message_id=None,
            scope=scope,
            binding_id=binding.id,
            short_id=binding.short_id,
            project_alias=binding.project_alias,
            fallback_origin=FakeMessage("/goal", message_id="om_cache_origin"),
        )
        self.channel.reply_results.append(
            sent_result("om_cache_goal", chat_id="oc_direct")
        )
        self.assertTrue(
            await self.app._progress_cards.start_goal(
                binding_id=binding.id,
                thread_id="native-one",
                logical_turn_id="goal-one",
                generation=generation,
                origin=origin,
                projection=projection,
                revision=(1,),
                refresh=None,
            )
        )

        for index in range(channel_app._GOAL_REPLY_CARD_CACHE_LIMIT + 1):
            self.app._progress_cards._remember_goal_projection(
                f"om_cache_{index}",
                f"generation_{index}",
                projection,
            )

        self.assertNotIn(
            ("om_cache_goal", generation),
            self.app._progress_cards._goal_cards,
        )
        self.assertEqual(
            self.app._progress_cards.goal_projection(
                source_id="om_cache_goal",
                generation=generation,
            ),
            projection,
        )

    async def test_goal_pause_card_finishes_on_the_same_card(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.runtime.stop_result = StopDisposition.GOAL_REQUESTED
        self.runtime.goal_snapshot_value = native_goal(GoalStatus.ACTIVE)
        self.runtime.goal_snapshot_after_stop = native_goal(GoalStatus.PAUSED)
        card = await self.register_goal_card(
            scope=scope,
            binding=binding,
            goal=self.runtime.goal_snapshot_value,
            message_id="om_goal_card",
            runtime_state=GoalOperationState.RUNNING.value,
        )
        pause = next(
            item
            for item in _elements(card.card, "button")
            if item["text"]["content"] == "暂停 Goal"
        )

        await self.app.handle_card_action(
            SimpleNamespace(
                message_id="om_goal_card",
                chat_id="oc_direct",
                operator=SimpleNamespace(open_id="ou_user"),
                action=SimpleNamespace(
                    tag="button",
                    value=pause["behaviors"][0]["value"],
                    form_value=None,
                ),
            )
        )

        pausing = json.dumps(self.channel.updates[-1][1], ensure_ascii=False)
        self.assertIn("正在暂停 Goal", pausing)
        await self.app.handle_completion(
            GoalOutcome(
                binding_id=binding.id,
                thread_id="native-one",
                logical_turn_id="goal-one",
                owner_id="ou_user",
                origin=channel_app.GoalCardOrigin(
                    message_id="om_goal_card",
                    scope=scope,
                    binding_id=binding.id,
                    short_id=binding.short_id,
                    project_alias=binding.project_alias,
                ),
                goal=self.runtime.goal_snapshot_after_stop,
                final_physical_turn_id="turn-final",
                final_turn_status="interrupted",
            )
        )

        rendered = json.dumps(self.channel.updates[-1][1], ensure_ascii=False)
        self.assertIn("Goal 已暂停", rendered)
        self.assertIn("goal-paused", rendered)
        self.assertIn("前台工具进程不受此接口保证，可能仍在运行", rendered)

    async def test_goal_uses_one_composed_card_through_result_files_and_paging(
        self,
    ) -> None:
        await self.new()
        self.runtime.available_capabilities = frozenset({NativeCapability.GOAL})
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-one")
        feedback = BindingTaskFeedback(progress_card_enabled=True)
        activity = goal_activity_snapshot(
            binding_id=binding.id,
            steps=tuple(
                TurnPlanStepSnapshot(
                    "generate outputs" if index == 0 else f"step {index}",
                    TurnPlanStepState.COMPLETED,
                )
                for index in range(13)
            ),
            commentary=(
                TurnActivityEntrySnapshot(
                    TurnActivityKind.COMMENTARY,
                    TurnActivityStatus.COMPLETED,
                    555,
                    text="goal progress",
                ),
            ),
            operations=(
                TurnActivityEntrySnapshot(
                    TurnActivityKind.TOOL,
                    TurnActivityStatus.COMPLETED,
                    666,
                    text="github.get_file_contents",
                ),
            ),
        )
        self.runtime.goal_activity_values[binding.id] = activity
        self.runtime.goal_snapshot_value = native_goal(GoalStatus.ACTIVE)
        self.runtime.goal_submission = GoalSubmission(
            binding.id,
            "native-one",
            "goal-one",
            lambda: None,
            task_feedback=feedback,
        )
        self.channel.reply_results.append(
            sent_result("om_goal_composed", chat_id="oc_direct")
        )
        paths = tuple(f"goal-result-{index:02}.txt" for index in range(10))
        for path in paths:
            (self.project / path).write_text(path, encoding="utf-8")

        await self.app.handle_message(
            FakeMessage("/goal ship safely", message_id="om_goal_composed_origin")
        )

        self.assertEqual(len(self.channel.replies), 1)
        origin = self.runtime.start_goal_calls[-1]["origin"]
        self.assertIsInstance(origin, channel_app.GoalCardOrigin)
        assert isinstance(origin, channel_app.GoalCardOrigin)
        self.assertEqual(origin.message_id, "om_goal_composed")
        initial = self.channel.replies[0][1]
        assert isinstance(initial, OutboundCard)
        self.assertEqual(len(tuple(_elements(initial.card, "collapsible_panel"))), 1)

        final_item = ThreadItem.model_validate({
            "type": "fileChange", "id": "final-patches", "status": "completed",
            "changes": [
                {"path": path, "kind": {"type": "update"},
                 "diff": "@@ -1 +1 @@\n-old\n+new\n"}
                for path in paths
            ] + [{"path": "deleted.txt", "kind": {"type": "delete"},
                  "diff": "deleted one\ndeleted two\n"}],
        })
        final_diff = "".join(
            (
                f"diff --git a/{path} b/{path}\n"
                f"--- a/{path}\n"
                f"+++ b/{path}\n"
                "@@ -1 +1 @@\n"
                "-old\n"
                "+new\n"
            )
            for path in paths
        ) + (
            "diff --git a/deleted.txt b/deleted.txt\n"
            "--- a/deleted.txt\n"
            "+++ /dev/null\n"
            "@@ -1,2 +0,0 @@\n"
            "-deleted one\n"
            "-deleted two\n"
        )
        await self.app.handle_completion(
            GoalOutcome(
                binding_id=binding.id,
                thread_id="native-one",
                logical_turn_id="goal-one",
                owner_id="ou_user",
                origin=origin,
                goal=native_goal(GoalStatus.COMPLETE),
                final_physical_turn_id="goal-turn-final",
                final_turn_status="completed",
                final_items=(final_item,),
                final_response="goal files ready",
                turn_diff=final_diff,
                task_feedback=feedback,
                activity=activity,
                finalization=GoalFinalizationStatus.CLEARED,
            )
        )

        self.assertEqual(len(self.channel.replies), 1)
        self.assertEqual(self.channel.updates[-1][0], "om_goal_composed")
        terminal = self.channel.updates[-1][1]
        rendered = json.dumps(terminal, ensure_ascii=False)
        self.assertIn("Goal 已完成并自动结束", rendered)
        self.assertIn("generate outputs", rendered)
        self.assertIn("另有 1 项未展示", rendered)
        self.assertIn("goal files ready", rendered)
        self.assertIn("goal-result-00.txt", rendered)
        self.assertIn("+10", rendered)
        self.assertIn("-12", rendered)
        self.assertIn("millisecond='555'", rendered)
        self.assertIn("millisecond='666'", rendered)
        self.assertIn("github.get", rendered)
        self.assertIn("file", rendered)
        self.assertNotIn("结束 Goal", rendered)
        panel = next(iter(_elements(terminal, "collapsible_panel")))
        self.assertFalse(panel["expanded"])
        page_value = next(
            behavior["value"]
            for button in _elements(terminal, "button")
            for behavior in button.get("behaviors", ())
            if behavior["value"]["intent"] == "turn-file.page"
        )
        self.assertEqual(page_value["v"], 5)
        self.assertEqual((page_value["a"], page_value["d"]), (10, 12))
        self.assertEqual(
            (page_value["files"][8]["a"], page_value["files"][8]["d"]),
            (1, 1),
        )
        self.runtime.goal_snapshot_value = None

        await self.app.handle_card_action(
            self.direct_button_event(
                page_value,
                message_id="om_goal_composed",
            )
        )

        paged = json.dumps(self.channel.updates[-1][1], ensure_ascii=False)
        self.assertIn("Goal 已完成并自动结束", paged)
        self.assertIn("generate outputs", paged)
        self.assertIn("goal files ready", paged)
        self.assertIn("goal-result-08.txt", paged)
        self.assertIn("+10", paged)
        self.assertIn("-12", paged)
        self.assertIn("+1", paged)
        self.assertIn("millisecond='555'", paged)
        self.assertIn("millisecond='666'", paged)
        self.assertIn("github.get", paged)
        self.assertIn("file", paged)
        return_value = next(
            behavior["value"]
            for button in _elements(self.channel.updates[-1][1], "button")
            for behavior in button.get("behaviors", ())
            if behavior["value"]["intent"] == "turn-file.page"
        )
        self.assertEqual(return_value["page"], 0)
        self.assertNotEqual(return_value["nonce"], page_value["nonce"])
        await self.app.handle_card_action(
            self.direct_button_event(
                return_value,
                message_id="om_goal_composed",
            )
        )
        next_value = next(
            behavior["value"]
            for button in _elements(self.channel.updates[-1][1], "button")
            for behavior in button.get("behaviors", ())
            if behavior["value"]["intent"] == "turn-file.page"
        )
        self.assertEqual(
            {key: value for key, value in next_value.items() if key != "nonce"},
            {key: value for key, value in page_value.items() if key != "nonce"},
        )
        self.assertNotEqual(next_value["nonce"], page_value["nonce"])
        # The cleared G1 card remains a frozen result even after G2 exists.
        # Paging G1 updates only G1's exact source message.
        self.runtime.goal_snapshot_value = native_goal(
            GoalStatus.ACTIVE,
            created_at=2,
        )
        update_count = len(self.channel.updates)
        await self.app.handle_card_action(
            self.direct_button_event(
                next_value,
                message_id="om_goal_composed",
            )
        )
        self.assertEqual(len(self.channel.updates), update_count + 1)
        self.assertIn(
            "goal-result-08.txt",
            json.dumps(self.channel.updates[-1][1], ensure_ascii=False),
        )

    async def test_goal_completion_survives_exact_binding_deletion(self) -> None:
        await self.new()
        self.runtime.available_capabilities = frozenset({NativeCapability.GOAL})
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-one")
        self.runtime.goal_snapshot_value = native_goal(GoalStatus.ACTIVE)
        self.runtime.goal_submission = GoalSubmission(
            binding.id,
            "native-one",
            "goal-one",
            lambda: None,
        )
        self.channel.reply_results.append(
            sent_result("om_goal_deleted", chat_id="oc_direct")
        )
        await self.app.handle_message(
            FakeMessage("/goal ship safely", message_id="om_goal_origin")
        )
        origin = self.runtime.start_goal_calls[-1]["origin"]
        result_path = "goal-after-binding-deletion.txt"
        (self.project / result_path).write_text("result", encoding="utf-8")
        self.store.delete_binding(binding.id)

        await self.app.handle_completion(
            GoalOutcome(
                binding_id=binding.id,
                thread_id="native-one",
                logical_turn_id="goal-one",
                owner_id="ou_user",
                origin=origin,
                goal=native_goal(GoalStatus.COMPLETE),
                final_physical_turn_id="goal-turn-final",
                final_turn_status="completed",
                final_items=(file_change_item(result_path),),
                final_response="result after binding deletion",
                finalization=GoalFinalizationStatus.CLEARED,
            )
        )

        self.assertEqual(self.channel.updates[-1][0], "om_goal_deleted")
        self.assertIn(
            "result after binding deletion",
            json.dumps(self.channel.updates[-1][1], ensure_ascii=False),
        )
        self.assertIn(
            result_path,
            json.dumps(self.channel.updates[-1][1], ensure_ascii=False),
        )

    async def test_paused_goal_completed_turn_keeps_files_without_text_response(
        self,
    ) -> None:
        await self.new()
        self.runtime.available_capabilities = frozenset({NativeCapability.GOAL})
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-one")
        self.runtime.goal_snapshot_value = native_goal(GoalStatus.ACTIVE)
        self.runtime.goal_submission = GoalSubmission(
            binding.id,
            "native-one",
            "goal-one",
            lambda: None,
        )
        self.channel.reply_results.append(
            sent_result("om_goal_paused_files", chat_id="oc_direct")
        )
        await self.app.handle_message(
            FakeMessage("/goal ship safely", message_id="om_goal_paused_files_origin")
        )
        origin = self.runtime.start_goal_calls[-1]["origin"]
        result_path = "paused-goal-output.txt"
        (self.project / result_path).write_text("paused output", encoding="utf-8")

        await self.app.handle_completion(
            GoalOutcome(
                binding_id=binding.id,
                thread_id="native-one",
                logical_turn_id="goal-one",
                owner_id="ou_user",
                origin=origin,
                goal=native_goal(GoalStatus.PAUSED),
                final_physical_turn_id="goal-turn-final",
                final_turn_status="completed",
                final_items=(file_change_item(result_path),),
                final_response=None,
            )
        )

        rendered = json.dumps(self.channel.updates[-1][1], ensure_ascii=False)
        self.assertIn("Goal 已暂停，未产生文本回复", rendered)
        self.assertIn(result_path, rendered)

    async def test_terminal_goal_fallback_card_clear_preserves_result_and_files(
        self,
    ) -> None:
        await self.new()
        self.runtime.available_capabilities = frozenset({NativeCapability.GOAL})
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-one")
        self.runtime.goal_snapshot_value = native_goal(GoalStatus.ACTIVE)
        self.runtime.goal_submission = GoalSubmission(
            binding.id,
            "native-one",
            "goal-one",
            lambda: None,
        )
        self.channel.reply_results.extend(
            (
                sent_result("om_goal_original", chat_id="oc_direct"),
                sent_result("om_goal_fallback", chat_id="oc_direct"),
            )
        )
        await self.app.handle_message(
            FakeMessage("/goal ship safely", message_id="om_goal_origin")
        )
        origin = self.runtime.start_goal_calls[-1]["origin"]
        result_path = self.project / "fallback-result.txt"
        result_path.write_text("result", encoding="utf-8")
        self.channel.fail_card_updates = True

        with self.assertLogs("netizen.channel_app", level="ERROR"):
            await self.app.handle_completion(
                GoalOutcome(
                    binding_id=binding.id,
                    thread_id="native-one",
                    logical_turn_id="goal-one",
                    owner_id="ou_user",
                    origin=origin,
                    goal=native_goal(GoalStatus.COMPLETE),
                    final_physical_turn_id="goal-turn-final",
                    final_turn_status="completed",
                    final_items=(file_change_item(result_path.name),),
                    final_response="fallback answer survives",
                )
            )

        self.channel.fail_card_updates = False
        fallback = self.channel.replies[-1][1]
        self.assertIsInstance(fallback, OutboundCard)
        assert isinstance(fallback, OutboundCard)
        fallback_text = json.dumps(fallback.card, ensure_ascii=False)
        self.assertIn("fallback answer survives", fallback_text)
        self.assertIn(result_path.name, fallback_text)
        clear_value = next(
            behavior["value"]
            for button in _elements(fallback.card, "button")
            if button["text"]["content"] == "结束 Goal"
            for behavior in button.get("behaviors", ())
        )
        self.runtime.goal_snapshot_value = native_goal(GoalStatus.COMPLETE)

        await self.app.handle_card_action(
            self.direct_button_event(
                clear_value,
                message_id="om_goal_fallback",
            )
        )

        self.assertEqual(self.runtime.clear_goal_calls, [self.store.get(binding.id)])
        updated = json.dumps(self.channel.updates[-1][1], ensure_ascii=False)
        self.assertIn("Goal 已结束", updated)
        self.assertIn("fallback answer survives", updated)
        self.assertIn(result_path.name, updated)

    async def test_restart_stale_goal_control_cannot_drop_result_modules(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-one")
        paused = native_goal(GoalStatus.PAUSED)
        result_path = self.project / "restart-retained.txt"
        result_path.write_text("retained", encoding="utf-8")
        stale = reply_card(
            ReplyCardProjection(
                scope=scope,
                goal=channel_app._reply_goal_module(
                    binding=self.store.get(binding.id),
                    goal=paused,
                    runtime_state="goal-paused",
                ),
                result=ReplyCardResultModule("retained after restart"),
                files=ReplyCardFilesModule(
                    binding_id=binding.id,
                    turn_id="goal-turn-final",
                    items=(
                        ReplyCardFileItem(
                            path=str(result_path),
                            label=result_path.name,
                            size=result_path.stat().st_size,
                            media_kind="file",
                        ),
                    ),
                ),
            )
        )
        clear_value = next(
            behavior["value"]
            for button in _elements(stale.card, "button")
            if button["text"]["content"] == "结束 Goal"
            for behavior in button.get("behaviors", ())
        )
        self.runtime.goal_snapshot_value = paused

        await self.app.handle_card_action(
            self.direct_button_event(
                clear_value,
                message_id="om_restart_stale_goal",
            )
        )

        self.assertEqual(self.runtime.clear_goal_calls, [])
        self.assertEqual(self.channel.updates, [])
        self.assertIn("Goal 卡片已过期", self.channel.replies[-1][1])

    async def test_goal_clear_command_preserves_result_files_and_current_page(
        self,
    ) -> None:
        await self.new()
        self.runtime.available_capabilities = frozenset({NativeCapability.GOAL})
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-one")
        binding = self.store.get(binding.id)
        paused = native_goal(GoalStatus.PAUSED)
        items = []
        for index in range(10):
            path = self.project / f"clear-page-{index:02}.txt"
            path.write_text(str(index), encoding="utf-8")
            items.append(
                ReplyCardFileItem(
                    path=str(path),
                    label=path.name,
                    size=path.stat().st_size,
                    media_kind="file",
                )
            )
        projection = ReplyCardProjection(
            scope=scope,
            goal=channel_app._reply_goal_module(
                binding=binding,
                goal=paused,
                runtime_state="goal-paused",
            ),
            result=ReplyCardResultModule("clear keeps this result"),
            files=ReplyCardFilesModule(
                binding_id=binding.id,
                turn_id="goal-turn-final",
                items=tuple(items),
            ),
        )
        generation = goal_generation(paused)
        self.assertTrue(
            await self.app._progress_cards.start_goal(
                binding_id=binding.id,
                thread_id="native-one",
                logical_turn_id="goal-terminal",
                generation=generation,
                origin=channel_app.GoalCardOrigin(
                    message_id="om_goal_clear_page",
                    scope=scope,
                    binding_id=binding.id,
                    short_id=binding.short_id,
                    project_alias=binding.project_alias,
                ),
                projection=projection,
                revision=("terminal",),
                refresh=None,
            )
        )
        self.channel.updates.clear()
        card = reply_card(projection)
        page_value = next(
            behavior["value"]
            for button in _elements(card.card, "button")
            for behavior in button.get("behaviors", ())
            if behavior["value"]["intent"] == "turn-file.page"
        )
        self.runtime.goal_snapshot_value = paused

        await self.app.handle_card_action(
            self.direct_button_event(
                page_value,
                message_id="om_goal_clear_page",
            )
        )
        await self.app.handle_message(
            FakeMessage("/goal clear", message_id="om_goal_clear_command")
        )

        updated = json.dumps(self.channel.updates[-1][1], ensure_ascii=False)
        self.assertIn("Goal 已结束", updated)
        self.assertIn("clear keeps this result", updated)
        visible = "\n".join(
            element["content"]
            for element in _elements(self.channel.updates[-1][1], "markdown")
        )
        self.assertIn("clear-page-08.txt", visible)
        self.assertNotIn("clear-page-00.txt", visible)

    async def test_oversized_goal_result_falls_back_without_losing_text(
        self,
    ) -> None:
        await self.new()
        self.runtime.available_capabilities = frozenset({NativeCapability.GOAL})
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-one")
        self.runtime.goal_snapshot_value = native_goal(GoalStatus.ACTIVE)
        self.runtime.goal_submission = GoalSubmission(
            binding.id,
            "native-one",
            "goal-one",
            lambda: None,
        )
        self.channel.reply_results.append(
            sent_result("om_goal_oversized", chat_id="oc_direct")
        )
        await self.app.handle_message(
            FakeMessage("/goal ship safely", message_id="om_goal_origin")
        )
        origin = self.runtime.start_goal_calls[-1]["origin"]
        oversized = "x" * 100_001

        await self.app.handle_completion(
            GoalOutcome(
                binding_id=binding.id,
                thread_id="native-one",
                logical_turn_id="goal-one",
                owner_id="ou_user",
                origin=origin,
                goal=native_goal(GoalStatus.COMPLETE),
                final_physical_turn_id="goal-turn-final",
                final_turn_status="completed",
                final_response=oversized,
                finalization=GoalFinalizationStatus.CLEARED,
            )
        )

        self.assertIn(
            "结果正文无法完整放入卡片",
            json.dumps(self.channel.updates[-1][1], ensure_ascii=False),
        )
        self.assertEqual(self.channel.replies[-1][1], oversized)

    async def test_stale_goal_file_page_cannot_overwrite_cleared_card(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-one")
        paused = native_goal(GoalStatus.PAUSED)
        items = []
        for index in range(9):
            path = self.project / f"stale-page-{index}.txt"
            path.write_text(str(index), encoding="utf-8")
            items.append(
                ReplyCardFileItem(
                    path=str(path),
                    label=path.name,
                    size=None,
                    media_kind=None,
                )
            )
        card = reply_card(
            ReplyCardProjection(
                scope=scope,
                goal=channel_app._reply_goal_module(
                    binding=binding,
                    goal=paused,
                    runtime_state="goal-paused",
                ),
                result=ReplyCardResultModule("paused result"),
                files=ReplyCardFilesModule(
                    binding_id=binding.id,
                    turn_id="goal-turn-final",
                    items=tuple(items),
                ),
            )
        )
        page_value = next(
            behavior["value"]
            for button in _elements(card.card, "button")
            for behavior in button.get("behaviors", ())
            if behavior["value"]["intent"] == "turn-file.page"
        )
        intent = decode_turn_file_action(
            app_id="cli_test",
            message_id="om_cleared_goal",
            callback_chat_id="oc_direct",
            sender_id="ou_user",
            tag="button",
            value=page_value,
        )
        self.runtime.goal_snapshot_value = None

        with self.assertRaisesRegex(CardActionError, "Goal 已变化"):
            await self.app._turn_file_action(intent)

        self.assertEqual(self.channel.updates, [])

    async def test_goal_file_page_rejects_changed_current_file_manifest(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-one")
        paused = native_goal(GoalStatus.PAUSED)
        old_items = []
        for index in range(9):
            path = self.project / f"old-page-{index}.txt"
            path.write_text(str(index), encoding="utf-8")
            old_items.append(
                ReplyCardFileItem(
                    path=str(path),
                    label=path.name,
                    size=path.stat().st_size,
                    media_kind="file",
                )
            )
        old_projection = ReplyCardProjection(
            scope=scope,
            goal=channel_app._reply_goal_module(
                binding=binding,
                goal=paused,
                runtime_state="goal-paused",
            ),
            result=ReplyCardResultModule("old result"),
            files=ReplyCardFilesModule(
                binding_id=binding.id,
                turn_id="goal-turn-final",
                items=tuple(old_items),
            ),
        )
        card = reply_card(old_projection)
        page_value = next(
            behavior["value"]
            for button in _elements(card.card, "button")
            for behavior in button.get("behaviors", ())
            if behavior["value"]["intent"] == "turn-file.page"
        )
        intent = decode_turn_file_action(
            app_id="cli_test",
            message_id="om_changed_files",
            callback_chat_id="oc_direct",
            sender_id="ou_user",
            tag="button",
            value=page_value,
        )
        replacement = self.project / "replacement.txt"
        replacement.write_text("replacement", encoding="utf-8")
        generation = goal_generation(paused)
        self.app._progress_cards._remember_goal_projection(
            "om_changed_files",
            generation,
            replace(
                old_projection,
                files=ReplyCardFilesModule(
                    binding_id=binding.id,
                    turn_id="goal-turn-final",
                    items=(
                        ReplyCardFileItem(
                            path=str(replacement),
                            label=replacement.name,
                            size=replacement.stat().st_size,
                            media_kind="file",
                        ),
                    ),
                ),
            ),
        )
        self.runtime.goal_snapshot_value = paused

        with self.assertRaisesRegex(CardActionError, "结果或文件已变化"):
            await self.app._turn_file_action(intent)

        self.assertEqual(self.channel.updates, [])

    async def test_goal_clear_unknown_keeps_result_and_disables_controls(self) -> None:
        await self.new()
        self.runtime.available_capabilities = frozenset({NativeCapability.GOAL})
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-one")
        self.runtime.goal_snapshot_value = native_goal(GoalStatus.ACTIVE)
        self.runtime.goal_submission = GoalSubmission(
            binding.id,
            "native-one",
            "goal-one",
            lambda: None,
        )
        self.channel.reply_results.append(
            sent_result("om_goal_unknown", chat_id="oc_direct")
        )
        await self.app.handle_message(
            FakeMessage("/goal ship safely", message_id="om_goal_unknown_origin")
        )
        origin = self.runtime.start_goal_calls[-1]["origin"]

        await self.app.handle_completion(
            GoalOutcome(
                binding_id=binding.id,
                thread_id="native-one",
                logical_turn_id="goal-one",
                owner_id="ou_user",
                origin=origin,
                goal=native_goal(GoalStatus.COMPLETE),
                final_physical_turn_id="goal-turn-final",
                final_turn_status="completed",
                final_response="answer survives",
                finalization=GoalFinalizationStatus.UNKNOWN,
                finalization_error=RuntimeError("clear response lost"),
            )
        )

        rendered = json.dumps(self.channel.updates[-1][1], ensure_ascii=False)
        self.assertIn("自动结束结果未知", rendered)
        self.assertIn("answer survives", rendered)
        self.assertIn("goal-unknown", rendered)
        self.assertNotIn("暂停 Goal", rendered)
        self.assertNotIn("恢复 Goal", rendered)
        self.assertNotIn("结束 Goal", rendered)
        self.assertEqual(len(self.channel.replies), 1)

    async def test_goal_unknown_with_native_absent_keeps_frozen_status_and_rejects_clear(
        self,
    ) -> None:
        await self.new()
        self.runtime.available_capabilities = frozenset({NativeCapability.GOAL})
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-one")
        frozen = native_goal(GoalStatus.COMPLETE)
        self.runtime.goal_snapshot_value = frozen
        self.runtime.active_goals[binding.id] = ActiveGoalSnapshot(
            binding_id=binding.id,
            thread_id="native-one",
            logical_turn_id="goal-one",
            owner_id="ou_user",
            state=GoalOperationState.UNKNOWN,
            persisted=frozen,
        )
        self.runtime.clear_goal_error = GoalStateUnknown(
            "当前 Goal 状态未确认；该会话保持占用。"
        )
        self.channel.reply_results.append(
            sent_result("om_goal_frozen_unknown", chat_id="oc_direct")
        )

        await self.app.handle_message(
            FakeMessage("/goal", message_id="om_goal_unknown_status")
        )

        status_card = self.channel.replies[-1][1]
        self.assertIsInstance(status_card, OutboundCard)
        assert isinstance(status_card, OutboundCard)
        serialized = json.dumps(status_card.card, ensure_ascii=False)
        self.assertIn("goal-unknown", serialized)
        self.assertIn("ship safely", serialized)
        self.assertNotIn("当前原生 Thread 没有 Goal", serialized)

        await self.app.handle_message(
            FakeMessage("/goal clear", message_id="om_goal_unknown_clear")
        )

        self.assertEqual(len(self.runtime.clear_goal_calls), 1)
        self.assertIn("Goal 状态未确认", self.channel.replies[-1][1])
        self.assertEqual(self.channel.updates, [])

    async def test_goal_start_unknown_without_snapshot_never_claims_no_goal(
        self,
    ) -> None:
        await self.new()
        self.runtime.available_capabilities = frozenset({NativeCapability.GOAL})
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-one")
        self.runtime.goal_snapshot_value = None
        self.runtime.active_goals[binding.id] = ActiveGoalSnapshot(
            binding_id=binding.id,
            thread_id="native-one",
            logical_turn_id=None,
            owner_id="ou_user",
            state=GoalOperationState.UNKNOWN,
            persisted=None,
        )

        await self.app.handle_message(
            FakeMessage("/goal", message_id="om_goal_start_unknown_status")
        )

        status_card = self.channel.replies[-1][1]
        self.assertIsInstance(status_card, OutboundCard)
        assert isinstance(status_card, OutboundCard)
        serialized = json.dumps(status_card.card, ensure_ascii=False)
        self.assertIn("Goal 状态未确认", serialized)
        self.assertIn("当前会话仍保持占用", serialized)
        self.assertNotIn("当前原生 Thread 没有 Goal", serialized)

    async def test_goal_card_controls_exact_binding_after_active_switch(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        goal_binding = self.store.active_binding(scope.key)
        await self.create_binding(scope)
        self.assertNotEqual(
            self.store.active_binding(scope.key).id,
            goal_binding.id,
        )
        self.runtime.goal_snapshot_value = native_goal(GoalStatus.ACTIVE)
        card = await self.register_goal_card(
            scope=scope,
            binding=goal_binding,
            goal=self.runtime.goal_snapshot_value,
            message_id="om_background_goal",
            runtime_state=GoalOperationState.RUNNING.value,
        )
        pause = next(
            item
            for item in _elements(card.card, "button")
            if item["text"]["content"] == "暂停 Goal"
        )

        await self.app.handle_card_action(
            self.direct_button_event(
                pause["behaviors"][0]["value"],
                message_id="om_background_goal",
            )
        )

        self.assertEqual(self.runtime.stop_calls, [goal_binding.id])
        self.assertEqual(self.channel.updates[-1][0], "om_background_goal")
        self.assertIn(
            "正在暂停 Goal",
            json.dumps(self.channel.updates[-1][1], ensure_ascii=False),
        )

    async def test_stale_goal_generation_has_zero_native_mutation(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        old = native_goal(GoalStatus.ACTIVE, created_at=1)
        card = await self.register_goal_card(
            scope=scope,
            binding=binding,
            goal=old,
            message_id="om_stale_goal",
            runtime_state=GoalOperationState.RUNNING.value,
        )
        self.runtime.goal_snapshot_value = native_goal(
            GoalStatus.ACTIVE,
            created_at=2,
        )
        pause = next(
            item
            for item in _elements(card.card, "button")
            if item["text"]["content"] == "暂停 Goal"
        )

        await self.app.handle_card_action(
            self.direct_button_event(
                pause["behaviors"][0]["value"],
                message_id="om_stale_goal",
            )
        )

        self.assertEqual(self.runtime.stop_calls, [])
        self.assertEqual(self.channel.updates[-1][0], "om_stale_goal")
        self.assertIn(
            "Goal 已变化",
            json.dumps(self.channel.updates[-1][1], ensure_ascii=False),
        )

    async def test_same_second_goal_fingerprint_still_requires_exact_card_owner(
        self,
    ) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        first_goal = native_goal(GoalStatus.ACTIVE, created_at=1)
        old_card = await self.register_goal_card(
            scope=scope,
            binding=binding,
            goal=first_goal,
            message_id="om_goal_same_second_old",
            runtime_state=GoalOperationState.RUNNING.value,
            logical_turn_id="goal-old",
        )
        old_pause = next(
            item
            for item in _elements(old_card.card, "button")
            if item["text"]["content"] == "暂停 Goal"
        )
        generation = goal_generation(first_goal)
        self.assertTrue(
            await self.app._progress_cards.update_goal_module(
                source_id="om_goal_same_second_old",
                generation=generation,
                scope=scope,
                goal=channel_app._reply_goal_module(
                    binding=self.store.get(binding.id),
                    goal=None,
                    notice="Goal 已结束。",
                ),
                retain_session=False,
            )
        )
        second_goal = native_goal(GoalStatus.ACTIVE, created_at=1)
        self.assertEqual(goal_generation(second_goal), generation)
        await self.register_goal_card(
            scope=scope,
            binding=self.store.get(binding.id),
            goal=second_goal,
            message_id="om_goal_same_second_new",
            runtime_state=GoalOperationState.RUNNING.value,
            logical_turn_id="goal-new",
        )
        self.channel.updates.clear()
        self.runtime.goal_snapshot_value = second_goal

        await self.app.handle_card_action(
            self.direct_button_event(
                old_pause["behaviors"][0]["value"],
                message_id="om_goal_same_second_old",
            )
        )

        self.assertEqual(self.runtime.stop_calls, [])
        self.assertEqual(self.channel.updates, [])
        self.assertIn("Goal 卡片已过期", self.channel.replies[-1][1])

    async def test_direct_image_message_is_native_visual_input(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.runtime.submission = Submission(
            SubmitDisposition.STARTED,
            binding.id,
            "native-one",
            "turn-one",
            lambda: None,
        )
        self.channel.resource_bodies[("om_image", "img_current")] = PNG
        image = FakeMessage(
            "![image](img_current)",
            message_id="om_image",
            raw_content_type="image",
            content=ImageContent(image_key="img_current"),
            resources=[ResourceDescriptor(type="image", file_key="img_current")],
        )

        await self.app.handle_message(image)

        self.assertEqual(self.runtime.capture_calls, [binding.id])
        self.assertEqual(
            self.channel.download_resource_calls,
            [("img_current", "image", "om_image")],
        )
        submitted = self.runtime.submit_calls[0]
        self.assertEqual(submitted["admission"].binding_id, binding.id)
        native_input = submitted["input"]
        self.assertIsInstance(native_input, list)
        self.assertIsInstance(native_input[0], TextInput)
        self.assertIsInstance(native_input[1], ImageInput)
        label = json.loads(native_input[0].text)
        self.assertEqual(label["source"], "current_message")
        self.assertEqual(label["ref"], "img1")
        self.assertNotIn("file_key", label)
        self.assertNotIn("message_id", label)
        request_text, current_context = plain_prompt_projection(native_input)
        self.assertEqual(request_text, "![image](img1)")
        self.assertEqual(current_context["message_id"], "om_image")
        self.assertEqual(current_context["message_type"], "image")
        self.assertEqual(current_context["content_fidelity"], "full_multimodal")
        self.assertEqual(current_context["sender"]["open_id"], "ou_user")
        self.assertEqual(
            sum(
                item.text.count("![image](img1)")
                for item in native_input
                if isinstance(item, TextInput)
            ),
            1,
        )

    async def test_observation_unavailable_rejection_keeps_actionable_message(
        self,
    ) -> None:
        await self.new()
        self.runtime.capture_error = TurnObservationUnavailable(
            "当前 Turn 观测不可用，暂不能接收新消息；"
            "请在 /sessions 中重新检查或停止本次 Turn。"
        )

        await self.app.handle_message(
            FakeMessage("continue", message_id="om_recovery_prompt")
        )

        self.assertEqual(self.runtime.submit_calls, [])
        self.assertIn("观测不可用", str(self.channel.replies[-1][1]))
        self.assertNotIn("Codex 后端处理失败", str(self.channel.replies[-1][1]))

    async def test_image_without_binding_does_not_download(self) -> None:
        image = FakeMessage(
            "![image](img_current)",
            message_id="om_image",
            raw_content_type="image",
            content=ImageContent(image_key="img_current"),
            resources=[ResourceDescriptor(type="image", file_key="img_current")],
        )

        await self.app.handle_message(image)

        self.assertEqual(self.runtime.capture_calls, [])
        self.assertEqual(self.channel.download_resource_calls, [])
        self.assertEqual(self.runtime.submit_calls, [])
        self.assertIn("还没有会话", str(self.channel.replies[-1][1]))

    async def test_current_post_and_quoted_image_are_both_native_visual_inputs(
        self,
    ) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.runtime.submission = Submission(
            SubmitDisposition.STARTED,
            binding.id,
            "native-one",
            "turn-one",
            lambda: None,
        )
        self.channel.inbound_messages["om_quote_image"] = quoted_inbound(
            message_id="om_quote_image",
            content=ImageContent(image_key="img_quoted"),
            content_text="![image](img_quoted)",
            raw_content_type="image",
            resources=[ResourceDescriptor(type="image", file_key="img_quoted")],
        )
        self.channel.resource_bodies[("om_quote_image", "img_quoted")] = PNG
        self.channel.resource_bodies[("om_current_post", "img_current")] = PNG
        current = FakeMessage(
            "compare quoted ![image](img_current)",
            message_id="om_current_post",
            raw_content_type="post",
            content=PostContent(
                text="compare quoted",
                post={
                    "zh_cn": {
                        "content_v2": [[
                            {"tag": "text", "text": "compare quoted "},
                            {"tag": "img", "image_key": "img_current"},
                        ]]
                    }
                },
            ),
            resources=[ResourceDescriptor(type="image", file_key="img_current")],
            reply_id="om_quote_image",
            raw={"parent_id": "om_quote_image", "root_id": "om_root"},
        )

        await self.app.handle_message(current)

        self.assertEqual(
            self.channel.download_resource_calls,
            [
                ("img_quoted", "image", "om_quote_image"),
                ("img_current", "image", "om_current_post"),
            ],
        )
        native_input = self.runtime.submit_calls[0]["input"]
        self.assertIsInstance(native_input, list)
        labels = [json.loads(native_input[index].text) for index in (0, 2)]
        self.assertEqual(
            [(label["source"], label["ref"]) for label in labels],
            [
                ("quoted_message", "img1"),
                ("current_message", "img2"),
            ],
        )
        self.assertTrue(
            all(
                "file_key" not in label and "message_id" not in label
                for label in labels
            )
        )
        envelope = json.loads(native_input[-1].text)
        self.assertEqual(envelope["kind"], "feishu_quoted_prompt")
        self.assertEqual(envelope["version"], 4)
        self.assertEqual(
            envelope["current_message"]["request_text"],
            "compare quoted ![image](img2)",
        )
        self.assertEqual(
            envelope["current_message"]["content_fidelity"],
            "full_multimodal",
        )
        self.assertEqual(envelope["quoted_message"]["ref"], "h1")
        self.assertEqual(
            envelope["quoted_message"]["attachments"],
            [{"type": "image", "ref": "img1"}],
        )
        self.assertNotIn("content_fidelity", envelope["quoted_message"])
        self.assertNotIn("content_read", envelope["quoted_message"])
        self.assertNotIn("resources", envelope["quoted_message"])
        current_text = "compare quoted ![image](img2)"
        self.assertEqual(
            sum(
                item.text.count(current_text)
                for item in native_input
                if isinstance(item, TextInput)
            ),
            1,
        )

    async def test_quoted_post_downloads_all_images_in_rendered_order(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.runtime.submission = Submission(
            SubmitDisposition.STARTED,
            binding.id,
            "native-one",
            "turn-one",
            lambda: None,
        )
        self.channel.inbound_messages["om_quote_post"] = quoted_inbound(
            message_id="om_quote_post",
            content=PostContent(
                text="before and after",
                post={
                    "zh_cn": {
                        "content": [[
                            {"tag": "img", "image_key": "img_hidden_v1"},
                        ]],
                        "content_v2": [[
                            {"tag": "text", "text": "before "},
                            {"tag": "img", "image_key": "img_one"},
                            {"tag": "text", "text": " after "},
                            {"tag": "img", "image_key": "img_two"},
                        ]],
                    }
                },
            ),
            content_text="before ![image](img_one) after ![image](img_two)",
            raw_content_type="post",
            resources=[
                # SDK 1.4.0 can omit content_v2 images and expose an
                # unrendered content-v1 resource instead.
                ResourceDescriptor(type="image", file_key="img_hidden_v1"),
            ],
        )
        self.channel.resource_bodies[("om_quote_post", "img_one")] = PNG
        self.channel.resource_bodies[("om_quote_post", "img_two")] = PNG

        await self.app.handle_message(
            FakeMessage(
                "summarize the post",
                message_id="om_prompt",
                reply_id="om_quote_post",
                raw={"parent_id": "om_quote_post", "root_id": "om_root"},
            )
        )

        self.assertEqual(
            self.channel.download_resource_calls,
            [
                ("img_one", "image", "om_quote_post"),
                ("img_two", "image", "om_quote_post"),
            ],
        )
        native_input = self.runtime.submit_calls[0]["input"]
        labels = [json.loads(native_input[index].text) for index in (0, 2)]
        self.assertEqual([label["index"] for label in labels], [1, 2])
        self.assertEqual([label["count"] for label in labels], [2, 2])
        self.assertEqual([label["ref"] for label in labels], ["img1", "img2"])
        envelope = json.loads(native_input[-1].text)
        self.assertEqual(envelope["kind"], "feishu_quoted_prompt")
        self.assertEqual(envelope["version"], 4)
        self.assertEqual(
            envelope["quoted_message"]["text"],
            "before ![image](img1) after ![image](img2)",
        )
        self.assertEqual(
            envelope["quoted_message"]["attachments"],
            [
                {"type": "image", "ref": "img1"},
                {"type": "image", "ref": "img2"},
            ],
        )
        self.assertEqual(
            envelope["current_message"]["request_text"],
            "summarize the post",
        )

    async def test_image_download_failure_and_image_control_do_not_submit(self) -> None:
        await self.new()
        self.runtime.submit_calls.clear()
        missing = FakeMessage(
            "![image](img_missing)",
            message_id="om_missing_image",
            raw_content_type="image",
            content=ImageContent(image_key="img_missing"),
            resources=[ResourceDescriptor(type="image", file_key="img_missing")],
        )

        await self.app.handle_message(missing)

        self.assertEqual(self.runtime.submit_calls, [])
        self.assertIn("无法读取消息中的图片", str(self.channel.replies[-1][1]))

        command = FakeMessage(
            "/status ![image](img_command)",
            message_id="om_image_command",
            raw_content_type="post",
            content=PostContent(
                text="/status",
                post={
                    "zh_cn": {
                        "content": [[
                            {"tag": "text", "text": "/status "},
                            {"tag": "img", "image_key": "img_command"},
                        ]]
                    }
                },
            ),
            resources=[ResourceDescriptor(type="image", file_key="img_command")],
        )
        await self.app.handle_message(command)

        self.assertEqual(self.runtime.submit_calls, [])
        self.assertIn("不接受参数", str(self.channel.replies[-1][1]))
        self.assertNotIn(
            ("img_command", "image", "om_image_command"),
            self.channel.download_resource_calls,
        )

    async def test_different_bindings_prepare_images_concurrently(self) -> None:
        await self.new()
        other_scope = FeishuScope("cli_test", "oc_other", ScopeKind.DIRECT)
        self.store.create_binding(
            scope=other_scope,
            project_alias="test",
            creator_id="ou_other",
        )
        self.runtime.submission = Submission(
            SubmitDisposition.STARTED,
            "binding-any",
            "native-any",
            "turn-any",
            lambda: None,
        )
        first_gate = asyncio.Event()
        second_gate = asyncio.Event()
        self.channel.resource_bodies[("om_image_one", "img_one")] = first_gate
        self.channel.resource_bodies[("om_image_two", "img_two")] = second_gate

        first = FakeMessage(
            "![image](img_one)",
            message_id="om_image_one",
            raw_content_type="image",
            content=ImageContent(image_key="img_one"),
            resources=[ResourceDescriptor(type="image", file_key="img_one")],
        )
        second = FakeMessage(
            "![image](img_two)",
            message_id="om_image_two",
            sender_id="ou_other",
            chat_id="oc_other",
            raw_content_type="image",
            content=ImageContent(image_key="img_two"),
            resources=[ResourceDescriptor(type="image", file_key="img_two")],
        )

        first_task = asyncio.create_task(self.app.handle_message(first))
        second_task = asyncio.create_task(self.app.handle_message(second))
        for _ in range(20):
            await asyncio.sleep(0)
            if len(self.channel.download_resource_calls) == 2:
                break

        self.assertCountEqual(
            self.channel.download_resource_calls,
            [
                ("img_one", "image", "om_image_one"),
                ("img_two", "image", "om_image_two"),
            ],
        )
        first_gate.set()
        second_gate.set()
        await asyncio.gather(first_task, second_task)
        self.assertEqual(len(self.runtime.submit_calls), 2)
        self.assertNotIn(
            "Codex 后端处理失败",
            [reply[1] for reply in self.channel.replies],
        )

    async def test_first_level_quote_fetches_exact_message_and_composes_context(
        self,
    ) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.runtime.submission = Submission(
            SubmitDisposition.STARTED,
            binding.id,
            "native-one",
            "turn-one",
            lambda: None,
        )
        self.channel.inbound_messages["om_quoted"] = quoted_inbound(
            content_text="quoted account ou_secret"
        )

        await self.app.handle_message(
            FakeMessage(
                "what does this mean?",
                message_id="om_prompt",
                sender_id="ou_current",
                display_name="Current User",
                raw={"parent_id": "om_quoted", "root_id": "om_quoted"},
            )
        )

        self.assertEqual(self.runtime.capture_calls, [binding.id])
        self.assertEqual(self.channel.fetch_inbound_calls, ["om_quoted"])
        submitted = self.runtime.submit_calls[0]
        self.assertEqual(submitted["admission"].binding_id, binding.id)
        envelope = json.loads(submitted["input"])
        self.assertEqual(envelope["kind"], "feishu_quoted_prompt")
        self.assertEqual(envelope["version"], 4)
        self.assertEqual(
            envelope["current_message"]["request_text"],
            "what does this mean?",
        )
        self.assertEqual(
            envelope["current_message"]["sender"]["open_id"],
            "ou_current",
        )
        self.assertIn("quoted account", envelope["quoted_message"]["text"])
        self.assertIn("ou_secret", submitted["input"])
        self.assertEqual(envelope["quoted_message"]["ref"], "h1")
        self.assertNotIn("message_id", envelope["quoted_message"])
        self.assertNotIn("conversation", envelope["quoted_message"])
        self.assertNotIn("om_quoted", submitted["input"])
        self.assertNotIn("oc_direct", submitted["input"])
        self.assertEqual(
            envelope["quoted_message"]["sender"]["open_id"],
            "ou_quoted",
        )
        self.assertNotEqual(
            envelope["current_message"]["sender"],
            envelope["quoted_message"]["sender"],
        )

    async def test_interactive_quote_recovers_cardkit_v2_visible_text(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.runtime.submission = Submission(
            SubmitDisposition.STARTED,
            binding.id,
            "native-one",
            "turn-one",
            lambda: None,
        )
        self.channel.inbound_messages["om_card"] = quoted_inbound(
            message_id="om_card",
            content=InteractiveContent(card={"schema": "2.0"}, card_version="v2"),
            content_text="[interactive]",
            raw_content_type="interactive",
        )
        card = {
            "schema": "2.0",
            "header": {
                "title": {"tag": "plain_text", "content": "Application title"}
            },
            "body": {
                "elements": [
                    {"tag": "markdown", "content": "Visible application content"}
                ]
            },
        }
        self.channel.quoted_contexts["om_card"] = QuotedContext(
            message_id="om_card",
            content_type="interactive",
            text="",
            raw={"body": {"content": json.dumps(card)}},
        )

        await self.app.handle_message(
            FakeMessage(
                "summarize it",
                message_id="om_prompt",
                reply_id="om_card",
                raw={"parent_id": "om_card", "root_id": "om_root"},
            )
        )

        self.assertEqual(self.channel.fetch_quoted_calls, ["om_card"])
        envelope = json.loads(self.runtime.submit_calls[0]["input"])
        self.assertEqual(envelope["quoted_message"]["message_type"], "interactive")
        self.assertEqual(
            envelope["quoted_message"]["text"],
            "Application title\nVisible application content",
        )

    async def test_interactive_quote_rejects_unverifiable_fallbacks(self) -> None:
        await self.new()
        self.channel.inbound_messages["om_card"] = quoted_inbound(
            message_id="om_card",
            content=InteractiveContent(card={}, card_version="v1"),
            content_text="[interactive]",
            raw_content_type="interactive",
        )
        invalid_fallbacks = [
            None,
            SimpleNamespace(
                message_id="om_other",
                content_type="interactive",
                text="visible",
            ),
            SimpleNamespace(
                message_id="om_card",
                content_type="text",
                text="visible",
            ),
        ]

        for index, fallback in enumerate(invalid_fallbacks):
            with self.subTest(fallback=fallback):
                self.channel.quoted_contexts["om_card"] = fallback
                await self.app.handle_message(
                    FakeMessage(
                        "summarize it",
                        message_id=f"om_prompt_{index}",
                        reply_id="om_card",
                        raw={"parent_id": "om_card", "root_id": "om_root"},
                    )
                )
                self.assertIn(
                    "没有可验证的可见内容",
                    str(self.channel.replies[-1][1]),
                )

        self.assertEqual(self.runtime.submit_calls, [])
        self.assertEqual(self.channel.fetch_quoted_calls, ["om_card"] * 3)

    async def test_conflicting_quote_relation_fails_before_fetch_or_submit(self) -> None:
        await self.new()

        await self.app.handle_message(
            FakeMessage(
                "inspect it",
                message_id="om_prompt",
                reply_id="om_public",
                raw={"parent_id": "om_raw", "root_id": "om_root"},
            )
        )

        self.assertEqual(self.runtime.capture_calls, [])
        self.assertEqual(self.channel.fetch_inbound_calls, [])
        self.assertEqual(self.runtime.submit_calls, [])
        self.assertIn("相互冲突的引用目标", str(self.channel.replies[-1][1]))

    async def test_quote_fetch_failure_does_not_submit(self) -> None:
        await self.new()
        self.runtime.submit_calls.clear()

        await self.app.handle_message(
            FakeMessage(
                "inspect it",
                message_id="om_prompt",
                raw={"parent_id": "om_missing", "root_id": "om_missing"},
            )
        )

        self.assertEqual(self.channel.fetch_inbound_calls, ["om_missing"])
        self.assertEqual(self.runtime.submit_calls, [])
        self.assertIn("无法读取被引用的消息", str(self.channel.replies[-1][1]))

    async def test_quote_fetch_timeout_does_not_submit(self) -> None:
        await self.new()
        self.runtime.submit_calls.clear()

        async def never_returns(_message_id: str) -> object:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        with (
            patch.object(
                self.channel,
                "fetch_inbound_message",
                side_effect=never_returns,
            ),
            patch("netizen.channel_app._QUOTE_FETCH_TIMEOUT_SECONDS", 0.001),
        ):
            await self.app.handle_message(
                FakeMessage(
                    "inspect it",
                    message_id="om_prompt",
                    raw={"parent_id": "om_slow", "root_id": "om_slow"},
                )
            )

        self.assertEqual(self.runtime.submit_calls, [])
        self.assertIn("读取被引用消息超时", str(self.channel.replies[-1][1]))

    async def test_interactive_quote_fetches_have_independent_timeouts(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.runtime.submission = Submission(
            SubmitDisposition.STARTED,
            binding.id,
            "native-one",
            "turn-one",
            lambda: None,
        )
        self.channel.inbound_messages["om_card"] = quoted_inbound(
            message_id="om_card",
            content=InteractiveContent(card={}, card_version="v1"),
            content_text="[interactive]",
            raw_content_type="interactive",
        )
        self.channel.quoted_contexts["om_card"] = QuotedContext(
            message_id="om_card",
            content_type="interactive",
            text="Visible card",
        )
        fetch_inbound = self.channel.fetch_inbound_message
        fetch_context = self.channel.fetch_quoted_context

        async def delayed_inbound(message_id: str) -> object | None:
            await asyncio.sleep(0.03)
            return await fetch_inbound(message_id)

        async def delayed_context(message_id: str) -> object | None:
            await asyncio.sleep(0.03)
            return await fetch_context(message_id)

        with (
            patch.object(
                self.channel,
                "fetch_inbound_message",
                side_effect=delayed_inbound,
            ),
            patch.object(
                self.channel,
                "fetch_quoted_context",
                side_effect=delayed_context,
            ),
            patch("netizen.channel_app._QUOTE_FETCH_TIMEOUT_SECONDS", 0.05),
        ):
            await self.app.handle_message(
                FakeMessage(
                    "summarize it",
                    message_id="om_prompt",
                    reply_id="om_card",
                    raw={"parent_id": "om_card", "root_id": "om_root"},
                )
            )

        self.assertEqual(len(self.runtime.submit_calls), 1)
        envelope = json.loads(self.runtime.submit_calls[0]["input"])
        self.assertEqual(envelope["quoted_message"]["text"], "Visible card")

    async def test_interactive_fallback_timeout_does_not_submit(self) -> None:
        await self.new()
        self.channel.inbound_messages["om_card"] = quoted_inbound(
            message_id="om_card",
            content=InteractiveContent(card={}, card_version="v1"),
            content_text="[interactive]",
            raw_content_type="interactive",
        )

        async def never_returns(_message_id: str) -> object:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        with (
            patch.object(
                self.channel,
                "fetch_quoted_context",
                side_effect=never_returns,
            ),
            patch("netizen.channel_app._QUOTE_FETCH_TIMEOUT_SECONDS", 0.001),
        ):
            await self.app.handle_message(
                FakeMessage(
                    "summarize it",
                    message_id="om_prompt",
                    reply_id="om_card",
                    raw={"parent_id": "om_card", "root_id": "om_root"},
                )
            )

        self.assertEqual(self.runtime.submit_calls, [])
        self.assertIn("读取被引用消息超时", str(self.channel.replies[-1][1]))

    async def test_quote_without_binding_does_not_fetch(self) -> None:
        await self.app.handle_message(
            FakeMessage(
                "inspect it",
                message_id="om_prompt",
                raw={"parent_id": "om_quoted", "root_id": "om_quoted"},
            )
        )

        self.assertEqual(self.runtime.capture_calls, [])
        self.assertEqual(self.channel.fetch_inbound_calls, [])
        self.assertIn("还没有会话", str(self.channel.replies[-1][1]))

    async def test_control_command_with_quote_does_not_fetch(self) -> None:
        await self.new()

        await self.app.handle_message(
            FakeMessage(
                "/status",
                message_id="om_status",
                raw={"parent_id": "om_quoted", "root_id": "om_quoted"},
            )
        )

        self.assertEqual(self.runtime.capture_calls, [])
        self.assertEqual(self.channel.fetch_inbound_calls, [])

    async def test_topic_parent_relation_is_not_treated_as_quote(self) -> None:
        topic_scope = FeishuScope(
            "cli_test",
            "oc_group",
            ScopeKind.TOPIC,
            "omt_topic",
        )
        await self.create_binding(topic_scope)
        binding = self.store.active_binding(topic_scope.key)
        self.runtime.submission = Submission(
            SubmitDisposition.STARTED,
            binding.id,
            "native-topic",
            "turn-topic",
            lambda: None,
        )

        await self.app.handle_message(
            FakeMessage(
                "topic follow-up",
                message_id="om_topic_prompt",
                chat_id="oc_group",
                chat_type="group",
                thread_id="omt_topic",
                raw={"parent_id": "om_root", "root_id": "om_root"},
            )
        )

        self.assertEqual(self.channel.fetch_inbound_calls, [])
        self.assertEqual(self.runtime.capture_calls, [binding.id])
        request_text, current_context = plain_prompt_projection(
            self.runtime.submit_calls[0]["input"]
        )
        self.assertEqual(request_text, "topic follow-up")
        self.assertEqual(current_context["message_id"], "om_topic_prompt")

    async def test_resume_during_quote_fetch_rejects_prepared_prompt(self) -> None:
        await self.new(message_id="om_new_1")
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        first = self.store.active_binding(scope.key)
        await self.new(message_id="om_new_2")
        second = self.store.active_binding(scope.key)
        await self.app.handle_message(
            FakeMessage(f"/resume {first.short_id}", message_id="om_resume_first")
        )
        self.runtime.submission = Submission(
            SubmitDisposition.STARTED,
            first.id,
            "native-one",
            "turn-one",
            lambda: None,
        )
        self.runtime.enforce_active_submission = True
        quoted = quoted_inbound(content_text="quoted context")
        fetch_started = asyncio.Event()
        release_fetch = asyncio.Event()

        async def delayed_fetch(message_id: str) -> object:
            self.assertEqual(message_id, "om_quoted")
            fetch_started.set()
            await release_fetch.wait()
            return quoted

        with patch.object(
            self.channel,
            "fetch_inbound_message",
            side_effect=delayed_fetch,
        ):
            prompt_task = asyncio.create_task(
                self.app.handle_message(
                    FakeMessage(
                        "inspect it",
                        message_id="om_prompt",
                        raw={
                            "parent_id": "om_quoted",
                            "root_id": "om_quoted",
                        },
                    )
                )
            )
            await asyncio.wait_for(fetch_started.wait(), timeout=0.1)
            await self.app.handle_message(
                FakeMessage(
                    f"/resume {second.short_id}",
                    message_id="om_resume_second",
                )
            )
            release_fetch.set()
            await asyncio.wait_for(prompt_task, timeout=0.1)

        self.assertEqual(self.store.active_binding(scope.key).id, second.id)
        self.assertEqual(self.runtime.capture_calls, [first.id])
        self.assertEqual(self.runtime.submit_calls, [])
        self.assertIn("active 会话已切换", str(self.channel.replies[-1][1]))

    async def test_settings_and_bare_new_reply_with_cards_in_the_origin_scope(self) -> None:
        topic = FakeMessage(
            "/settings",
            message_id="om_settings",
            chat_id="oc_group",
            chat_type="group",
            thread_id="omt_settings",
        )
        await self.app.handle_message(topic)
        settings_reply = self.channel.replies[-1]
        self.assertEqual(settings_reply[0], "om_settings")
        self.assertIsInstance(settings_reply[1], OutboundCard)
        self.assertEqual(settings_reply[1].card["schema"], "2.0")
        self.assertIn("project_manage_v1", str(settings_reply[1].card))
        self.assertIn("project_create_v1", str(settings_reply[1].card))

        projects_tab = next(
            element
            for element in _elements(settings_reply[1].card, "button")
            if element["text"]["content"] == "Projects"
        )
        await self.app.handle_card_action(
            SimpleNamespace(
                message_id="om_settings_card",
                chat_id="oc_group",
                operator=SimpleNamespace(open_id="ou_other_participant"),
                action=SimpleNamespace(
                    tag="button",
                    value=projects_tab["behaviors"][0]["value"],
                    form_value=None,
                ),
            )
        )
        self.assertEqual(self.channel.updates[-1][0], "om_settings_card")
        self.assertIn("project_manage_v1", str(self.channel.updates[-1][1]))

        await self.app.handle_message(
            FakeMessage(
                "/new",
                message_id="om_picker",
                chat_id="oc_group",
                chat_type="group",
                thread_id="omt_settings",
            )
        )
        picker = self.channel.replies[-1][1]
        self.assertIsInstance(picker, OutboundCard)
        self.assertIn("test ·", str(picker.card))
        self.assertIn("new_binding_v6", str(picker.card))
        self.assertNotIn("配置方式", str(picker.card))
        self.assertNotIn("下一条真实任务", str(picker.card))
        scope = FeishuScope(
            "cli_test", "oc_group", ScopeKind.TOPIC, "omt_settings"
        )
        self.assertIsNone(self.store.active_binding(scope.key))
        form = next(
            item
            for item in _elements(picker.card, "form")
            if item["name"] == "new_binding_v6"
        )
        project_select = next(
            item
            for item in form["elements"]
            if item.get("name") == "new_project"
        )
        self.assertNotIn("initial_option", project_select)

    async def test_new_reuses_scope_binding_history_without_new_storage(self) -> None:
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        other_path = self.project_root / "other"
        unused_path = self.project_root / "unused"
        other_path.mkdir()
        unused_path.mkdir()
        other = self.projects.register(
            alias="other",
            path=str(other_path),
            create_directory=False,
        )
        unused = self.projects.register(
            alias="unused",
            path=str(unused_path),
            create_directory=False,
        )
        first = (await self.create_binding(scope)).binding
        current = (
            await self.app._management.create_current_binding(
                scope=scope,
                creator_id="ou_user",
                project_alias="other",
                expected_project_revision=other.revision,
            )
        ).binding
        await self.app._management.create_exact_lazy_binding(
            scope_key=scope.key,
            project_alias="unused",
            expected_project_revision=unused.revision,
            expected_active_binding_id=current.id,
            activate=False,
        )

        async def render(message_id: str) -> tuple[OutboundCard, str | None]:
            await self.app.handle_message(FakeMessage("/new", message_id=message_id))
            card = self.channel.replies[-1][1]
            forms = _elements(card.card, "form")
            if not forms:
                return card, None
            form = next(item for item in forms if item["name"] == "new_binding_v6")
            project_select = next(
                item
                for item in form["elements"]
                if item.get("name") == "new_project"
            )
            return card, project_select.get("initial_option")

        _card, selected = await render("om_current")
        self.assertEqual(selected, f"project:v1:other:{other.revision}")
        self.assertEqual(len(self.store.list_bindings(scope.key)), 3)

        self.store.deactivate(scope_key=scope.key, binding_id=current.id)
        _card, selected = await render("om_recent")
        self.assertEqual(selected, f"project:v1:other:{other.revision}")

        other = self.projects.set_enabled(
            alias="other",
            enabled=False,
            expected_revision=other.revision,
        )
        test = self.projects.resolve_for_new("test")
        _card, selected = await render("om_older_enabled")
        self.assertEqual(selected, f"project:v1:test:{test.revision}")

        self.projects.set_enabled(
            alias="test",
            enabled=False,
            expected_revision=test.revision,
        )
        _card, selected = await render("om_never_activated")
        self.assertIsNone(selected)

        self.projects.set_enabled(
            alias="unused",
            enabled=False,
            expected_revision=unused.revision,
        )
        disabled_picker, selected = await render("om_no_projects")
        self.assertIsNone(selected)
        self.assertEqual(_elements(disabled_picker.card, "form"), [])
        self.assertIn("/settings", str(disabled_picker.card))
        self.assertEqual(len(self.store.list_bindings(scope.key)), 3)
        self.assertEqual(first.project_alias, "test")
        self.assertFalse(other.enabled)

    async def test_new_model_configuration_creates_only_lazy_binding(self) -> None:
        await self.app.handle_message(FakeMessage("/new", message_id="om_picker"))
        picker = self.channel.replies[-1][1]
        form = next(
            item
            for item in _elements(picker.card, "form")
            if item["name"] == "new_binding_v6"
        )
        fields = {
            item["name"]: item
            for item in form["elements"]
            if "name" in item
        }
        project_reference = next(
            option["value"]
            for option in fields["new_project"]["options"]
            if option["text"]["content"].startswith("test ·")
        )
        event = self.direct_card_event(
            {
                "new_project": project_reference,
                "new_model": fields["new_model"]["initial_option"],
                "new_effort": fields["new_effort"]["initial_option"],
                "new_speed": fields["new_speed"]["initial_option"],
                "new_task_reactions": fields["new_task_reactions"][
                    "initial_option"
                ],
                "new_progress_card": fields["new_progress_card"][
                    "initial_option"
                ],
            }
        )

        await self.app.handle_card_action(event)

        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.assertEqual(
            binding.id,
            "11111111-0000-0000-0000-000000000001",
        )
        self.assertEqual(binding.project_alias, "test")
        self.assertIsNone(binding.native_thread_id)
        self.assertEqual(
            binding.turn_settings,
            BindingTurnSettings("future-model", "ultra", "priority-v2"),
        )
        self.assertEqual(binding.task_feedback, BindingTaskFeedback())
        self.assertEqual(self.runtime.submit_calls, [])
        rendered = str(self.channel.updates[-1][1])
        self.assertIn("Project 选择成功", rendered)
        self.assertIn("后续新 Turn 将使用", rendered)
        self.assertIn("Fast v2", rendered)
        self.assertEqual(self.runtime.model_catalog_calls, 1)
        self.assertEqual(len(self.runtime.resolve_model_settings_calls), 1)

    async def test_config_targets_exact_binding_and_only_saves_settings(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        await self.app.handle_message(FakeMessage("/config", message_id="om_config"))
        config = self.channel.replies[-1][1]
        self.assertIsInstance(config, OutboundCard)
        await self.app.handle_card_action(
            self.direct_card_event(self.config_form_values(config))
        )

        self.assertEqual(len(self.store.list_bindings(scope.key)), 1)
        configured = self.store.get(binding.id)
        self.assertEqual(
            configured.turn_settings,
            BindingTurnSettings("future-model", "ultra", "priority-v2"),
        )
        self.assertEqual(configured.settings_revision, 2)
        self.assertEqual(self.runtime.submit_calls, [])
        self.assertEqual(len(self.runtime.configure_settings_calls), 1)
        self.assertIn("会话配置已保存", str(self.channel.updates[-1][1]))
        self.assertIn("后续新 Turn 将使用", str(self.channel.updates[-1][1]))

    async def test_config_enables_pulse_and_progress_without_starting_turn(
        self,
    ) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        await self.app.handle_message(FakeMessage("/config", message_id="om_config"))
        card = self.channel.replies[-1][1]

        await self.app.handle_card_action(
            self.direct_card_event(
                self.config_form_values(
                    card,
                    reaction_pulse_enabled=True,
                    progress_card_enabled=True,
                )
            )
        )

        configured = self.store.get(binding.id)
        self.assertEqual(
            configured.task_feedback,
            BindingTaskFeedback(
                reaction_pulse_enabled=True,
                progress_card_enabled=True,
            ),
        )
        self.assertEqual(configured.feedback_revision, 2)
        self.assertEqual(self.runtime.submit_calls, [])
        rendered = str(self.channel.updates[-1][1])
        self.assertIn("执行中表情闪烁：开启", rendered)
        self.assertIn("进度卡：开启", rendered)

    async def test_config_replaces_persistent_settings_without_starting_turn(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        settings = BindingTurnSettings("future-model", "ultra", "priority-v2")
        binding = self.store.set_turn_settings(
            binding_id=binding.id,
            expected_revision=1,
            settings=settings,
        )
        await self.app.handle_message(FakeMessage("/config", message_id="om_config"))
        card = self.channel.replies[-1][1]

        await self.app.handle_card_action(
            self.direct_card_event(
                self.config_form_values(
                    card,
                    effort_id="low",
                    speed_id="default",
                )
            )
        )

        configured = self.store.get(binding.id)
        self.assertEqual(
            configured.turn_settings,
            BindingTurnSettings("future-model", "low", "default"),
        )
        self.assertEqual(configured.settings_revision, 3)
        self.assertEqual(self.runtime.submit_calls, [])
        self.assertEqual(
            self.runtime.configure_settings_calls[-1]["settings"],
            BindingTurnSettings("future-model", "low", "default"),
        )
        self.assertIn("后续新 Turn 将使用", str(self.channel.updates[-1][1]))

    async def test_second_config_card_is_rejected_by_settings_revision(self) -> None:
        await self.new()
        await self.app.handle_message(FakeMessage("/config", message_id="om_config_1"))
        first = self.channel.replies[-1][1]
        await self.app.handle_message(FakeMessage("/config", message_id="om_config_2"))
        second = self.channel.replies[-1][1]

        await self.app.handle_card_action(
            self.direct_card_event(
                self.config_form_values(first),
                message_id="om_card_1",
            )
        )
        selected = self.store.active_binding(
            FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT).key
        ).turn_settings
        await self.app.handle_card_action(
            self.direct_card_event(
                self.config_form_values(second),
                message_id="om_card_2",
            )
        )

        self.assertEqual(
            self.store.active_binding(
                FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT).key
            ).turn_settings,
            selected,
        )
        self.assertIn("会话配置已变化", str(self.channel.updates[-1][1]))
        self.assertEqual(self.runtime.submit_calls, [])

    async def test_stale_config_card_cannot_apply_after_resume_or_new(self) -> None:
        await self.new(message_id="om_new_1")
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        first = self.store.active_binding(scope.key)
        await self.app.handle_message(FakeMessage("/config", message_id="om_config"))
        config = self.channel.replies[-1][1]
        await self.new(message_id="om_new_2")
        second = self.store.active_binding(scope.key)
        self.assertNotEqual(first.id, second.id)

        await self.app.handle_card_action(
            self.direct_card_event(self.config_form_values(config))
        )

        self.assertEqual(self.runtime.submit_calls, [])
        self.assertEqual(self.runtime.configure_settings_calls, [])
        self.assertEqual(self.store.active_binding(scope.key).id, second.id)
        self.assertIn("active 会话已切换", str(self.channel.updates[-1][1]))

    async def test_config_rejects_running_turn_before_rendering_card(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.runtime.active[binding.id] = ActiveTurnSnapshot(
            binding.id,
            "native-one",
            "turn-one",
            "ou_user",
            ActiveState.RUNNING,
        )

        await self.app.handle_message(FakeMessage("/config", message_id="om_config"))

        self.assertIn("当前 Turn 正在执行", str(self.channel.replies[-1][1]))
        self.assertEqual(self.runtime.model_catalog_calls, 0)

    async def test_new_can_inherit_when_model_catalog_fails(self) -> None:
        self.runtime.model_catalog_error = RuntimeError("catalog unavailable")

        with self.assertLogs("netizen.channel_app", level="WARNING"):
            await self.app.handle_message(FakeMessage("/new", message_id="om_picker"))

        picker = self.channel.replies[-1][1]
        rendered = str(picker.card)
        self.assertIn("Model / Effort / Speed 暂不可用", rendered)
        self.assertNotIn("/new alias", rendered)
        self.assertEqual(len(_elements(picker.card, "form")), 1)
        self.assertIn("继承 Codex", rendered)
        self.assertIn("new_model", rendered)
        self.assertNotIn("new_effort", rendered)
        self.assertNotIn("new_speed", rendered)
        await self.app.handle_card_action(
            self.direct_card_event(self.new_form_values(picker))
        )
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.assertIsNotNone(binding)
        self.assertIsNone(binding.turn_settings)

    async def test_new_form_creates_lazy_configured_binding_in_exact_topic(self) -> None:
        await self.app.handle_message(
            FakeMessage(
                "/new",
                message_id="om_picker",
                chat_id="oc_group",
                chat_type="group",
                thread_id="omt_picker",
            )
        )
        picker = self.channel.replies[-1][1]
        self.channel.fetched_messages["om_card"] = {
            "data": {
                "items": [
                    {"chat_id": "oc_group", "thread_id": "omt_picker"}
                ]
            }
        }
        event = SimpleNamespace(
            message_id="om_card",
            chat_id="oc_group",
            operator=SimpleNamespace(open_id="ou_other_participant"),
            action=SimpleNamespace(
                tag="button",
                value={},
                form_value=self.new_form_values(picker),
            ),
        )

        await self.app.handle_card_action(event)

        scope = FeishuScope(
            "cli_test", "oc_group", ScopeKind.TOPIC, "omt_picker"
        )
        binding = self.store.active_binding(scope.key)
        self.assertEqual(binding.project_alias, "test")
        self.assertIsNone(binding.native_thread_id)
        self.assertEqual(
            binding.turn_settings,
            BindingTurnSettings("future-model", "ultra", "priority-v2"),
        )
        self.assertEqual(len(self.runtime.resolve_model_settings_calls), 1)
        self.assertEqual(self.channel.updates[-1][0], "om_card")
        rendered = str(self.channel.updates[-1][1])
        self.assertIn("Project 选择成功", rendered)
        self.assertIn(binding.short_id, rendered)
        self.assertIn("现在可以直接发送任务", rendered)

    async def test_new_form_replies_when_success_card_cannot_update(
        self,
    ) -> None:
        await self.app.handle_message(
            FakeMessage(
                "/new",
                message_id="om_picker",
                chat_id="oc_group",
                chat_type="group",
                thread_id="omt_picker",
            )
        )
        picker = self.channel.replies[-1][1]
        self.channel.card_update_success = False
        self.channel.fetched_messages["om_card"] = {
            "data": {
                "items": [
                    {"chat_id": "oc_group", "thread_id": "omt_picker"}
                ]
            }
        }
        event = SimpleNamespace(
            message_id="om_card",
            chat_id="oc_group",
            operator=SimpleNamespace(open_id="ou_user"),
            action=SimpleNamespace(
                tag="button",
                value={},
                form_value=self.new_form_values(picker),
            ),
        )

        with self.assertLogs("netizen.channel_app", level="ERROR"):
            await self.app.handle_card_action(event)

        scope = FeishuScope(
            "cli_test",
            "oc_group",
            ScopeKind.TOPIC,
            "omt_picker",
        )
        binding = self.store.active_binding(scope.key)
        self.assertIsNotNone(binding)
        fallback = next(
            text
            for message_id, text in self.channel.replies
            if message_id == "om_card" and "Project 选择成功" in str(text)
        )
        self.assertIn("`test`", fallback)
        self.assertIn(binding.short_id, fallback)
        target = self.channel.reply_targets[-1]
        self.assertEqual(target.chat_id, "oc_group")
        self.assertEqual(target.conversation.thread_id, "omt_picker")

    async def test_new_form_replies_when_failure_card_cannot_update(
        self,
    ) -> None:
        await self.app.handle_message(FakeMessage("/new", message_id="om_picker"))
        picker = self.channel.replies[-1][1]
        values = self.new_form_values(picker)
        project = self.projects.resolve_for_new("test")
        self.projects.set_enabled(
            alias="test",
            enabled=False,
            expected_revision=project.revision,
        )
        self.channel.card_update_success = False
        event = self.direct_card_event(values)

        with self.assertLogs("netizen.channel_app", level="ERROR"):
            await self.app.handle_card_action(event)

        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        self.assertIsNone(self.store.active_binding(scope.key))
        fallback = next(
            text
            for message_id, text in self.channel.replies
            if message_id == "om_card" and "会话操作失败" in str(text)
        )
        self.assertIn("其他操作修改", fallback)

    async def test_inline_project_create_recovers_topic_and_persists_registry(self) -> None:
        await self.app.handle_message(
            FakeMessage(
                "/settings",
                message_id="om_settings",
                chat_id="oc_group",
                chat_type="group",
                thread_id="omt_settings",
            )
        )
        settings = self.channel.replies[-1][1]
        self.assertIn("project_create_v1", str(settings.card))
        self.assertIn("project_manage_v1", str(settings.card))
        create_mode = _project_mode_value(settings, "create")

        self.channel.fetched_messages["om_card"] = {
            "data": {
                "items": [
                    {"chat_id": "oc_group", "thread_id": "omt_settings"}
                ]
            }
        }
        await self.app.handle_card_action(
            SimpleNamespace(
                message_id="om_card",
                chat_id="oc_group",
                operator=SimpleNamespace(open_id="ou_other_participant"),
                action=SimpleNamespace(
                    tag="button",
                    value={},
                    form_value={
                        "project_alias": "demo",
                        "project_mode": create_mode,
                        "project_path": "",
                    },
                ),
            )
        )

        project = self.projects.resolve_for_new("demo")
        self.assertEqual(project.cwd, (self.project_root / "demo").resolve())
        self.assertTrue(project.cwd.is_dir())
        self.assertIn("已登记 Project demo", str(self.channel.updates[-1][1]))
        self.assertIn("project_create_v1", str(self.channel.updates[-1][1]))
        self.assertIn("demo · 已启用", str(self.channel.updates[-1][1]))
        self.assertEqual(self.channel.chat_info_calls, [])

    async def test_project_management_targets_revision_and_keeps_errors_inline(self) -> None:
        await self.app.handle_message(
            FakeMessage(
                "/settings",
                message_id="om_settings",
                chat_id="oc_group",
                chat_type="group",
                thread_id="omt_settings",
            )
        )
        settings = self.channel.replies[-1][1]
        manage = next(
            form
            for form in _elements(settings.card, "form")
            if form["name"] == "project_manage_v1"
        )
        target = next(
            element
            for element in manage["elements"]
            if element.get("name") == "project_manage_target"
        )
        project_reference = next(
            option["value"]
            for option in target["options"]
            if option["text"]["content"].startswith("test ·")
        )
        self.channel.fetched_messages["om_card"] = {
            "data": {
                "items": [
                    {"chat_id": "oc_group", "thread_id": "omt_settings"}
                ]
            }
        }

        async def submit(reference: str, operation: str) -> None:
            await self.app.handle_card_action(
                SimpleNamespace(
                    message_id="om_card",
                    chat_id="oc_group",
                    operator=SimpleNamespace(open_id="ou_other_participant"),
                    action=SimpleNamespace(
                        tag="button",
                        value={},
                        form_value={
                            "project_manage_target": reference,
                            "project_manage_operation": operation,
                        },
                    ),
                )
            )

        await submit(project_reference, "disable")
        project = next(
            project for project in self.projects.list() if project.alias == "test"
        )
        self.assertFalse(project.enabled)
        self.assertIn("已停用 Project test", str(self.channel.updates[-1][1]))
        self.assertIn("project_create_v1", str(self.channel.updates[-1][1]))

        await submit(project_reference, "enable")
        stale = str(self.channel.updates[-1][1])
        self.assertIn("已被其他操作修改", stale)
        self.assertIn("project_manage_v1", stale)
        self.assertIn("project_create_v1", stale)
        self.assertFalse(
            next(
                project
                for project in self.projects.list()
                if project.alias == "test"
            ).enabled
        )

    async def test_project_create_validation_error_stays_in_settings(self) -> None:
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        existing_mode = _project_mode_value(
            self.app._settings_card(scope),
            "existing",
        )
        self.channel.fetched_messages["om_card"] = {
            "data": {"items": [{"chat_id": "oc_direct", "thread_id": None}]}
        }
        self.channel.chat_types["oc_direct"] = "p2p"

        async def submit(mode: str) -> None:
            await self.app.handle_card_action(
                SimpleNamespace(
                    message_id="om_card",
                    chat_id="oc_direct",
                    operator=SimpleNamespace(open_id="ou_user"),
                    action=SimpleNamespace(
                        tag="button",
                        value={},
                        form_value={
                            "project_alias": "missing_path",
                            "project_mode": mode,
                            "project_path": "",
                        },
                    ),
                )
            )

        await submit(existing_mode)

        rendered = str(self.channel.updates[-1][1])
        self.assertIn("登记已有目录时必须填写绝对路径", rendered)
        self.assertIn("project_manage_v1", rendered)
        self.assertIn("project_create_v1", rendered)
        redrawn_mode = _project_mode_value(
            self.channel.updates[-1][1],
            "existing",
        )
        self.assertEqual(
            existing_mode.rsplit(":", 1)[0],
            redrawn_mode.rsplit(":", 1)[0],
        )
        self.assertNotEqual(existing_mode, redrawn_mode)

        update_count = len(self.channel.updates)
        await submit(redrawn_mode)
        self.assertEqual(len(self.channel.updates), update_count + 1)
        self.assertIn(
            "登记已有目录时必须填写绝对路径",
            str(self.channel.updates[-1][1]),
        )

    async def test_committed_project_is_not_misreported_when_card_update_fails(self) -> None:
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        create_mode = _project_mode_value(
            self.app._settings_card(scope),
            "create",
        )
        self.channel.fetched_messages["om_card"] = {
            "data": {"items": [{"chat_id": "oc_direct", "thread_id": None}]}
        }
        self.channel.chat_types["oc_direct"] = "p2p"
        self.channel.fail_card_updates = True

        with self.assertLogs("netizen.channel_app", level="ERROR"):
            await self.app.handle_card_action(
                SimpleNamespace(
                    message_id="om_card",
                    chat_id="oc_direct",
                    operator=SimpleNamespace(open_id="ou_user"),
                    action=SimpleNamespace(
                        tag="button",
                        value={},
                        form_value={
                            "project_alias": "committed",
                            "project_mode": create_mode,
                            "project_path": "",
                        },
                    ),
                )
            )

        self.assertEqual(self.projects.resolve_for_new("committed").alias, "committed")
        self.assertEqual(len(self.channel.updates), 1)

    async def test_working_reaction_failure_releases_completion_barrier(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        released = False

        def release() -> None:
            nonlocal released
            released = True

        self.runtime.submission = Submission(
            SubmitDisposition.STARTED,
            binding.id,
            "native-one",
            "turn-one",
            release,
        )
        self.channel.fail_once_reaction_on = "Typing"

        with self.assertLogs("netizen.channel_app", level="ERROR"):
            await self.app.handle_message(FakeMessage("hello", message_id="om_prompt"))

        self.assertTrue(released)
        self.assertIn(("om_prompt", "Typing"), self.channel.reactions)
        self.assertFalse(
            any(
                isinstance(text, str) and "发送一条新消息重试" in text
                for _message_id, text in self.channel.replies
            )
        )

    async def test_busy_prompt_is_reported_as_native_steer(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.runtime.submission = Submission(
            SubmitDisposition.STEERED,
            binding.id,
            "native-one",
            "turn-one",
        )

        await self.app.handle_message(FakeMessage("new direction", message_id="om_steer"))

        self.assertNotIn(("om_steer", "已调整当前任务。"), self.channel.replies)
        self.assertNotIn(("om_steer", "已接收调整。"), self.channel.replies)
        self.assertEqual(self.channel.reactions, [("om_steer", "OnIt")])

    async def test_missing_sender_name_blocks_ordinary_turn_before_context_io(
        self,
    ) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.runtime.submission = Submission(
            SubmitDisposition.STARTED,
            binding.id,
            "native-one",
            "turn-one",
            lambda: None,
        )

        await self.app.handle_message(
            FakeMessage(
                "inspect this image",
                message_id="om_missing_sender",
                display_name="",
                raw_content_type="image",
                content=ImageContent(image_key="img_current"),
                resources=[
                    ResourceDescriptor(type="image", file_key="img_current")
                ],
                raw={"parent_id": "om_quoted", "root_id": "om_quoted"},
            )
        )

        self.assertEqual(self.runtime.submit_calls, [])
        self.assertEqual(self.channel.fetch_inbound_calls, [])
        self.assertEqual(self.channel.download_resource_calls, [])
        self.assertIn(
            "im:chat.members:read",
            str(self.channel.replies[-1][1]),
        )
        self.assertIn("本条消息未执行", str(self.channel.replies[-1][1]))

    async def test_missing_sender_name_blocks_running_steer(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.runtime.admission = SubmissionAdmission(
            binding.id,
            3,
            "native-one",
            "turn-running",
            1,
        )

        await self.app.handle_message(
            FakeMessage(
                "new direction",
                message_id="om_missing_steer_sender",
                display_name="",
            )
        )

        self.assertEqual(self.runtime.capture_calls, [])
        self.assertEqual(self.runtime.submit_calls, [])
        self.assertEqual(self.channel.reactions, [])
        self.assertIn(
            "im:chat.members:read",
            str(self.channel.replies[-1][1]),
        )

    async def test_accepted_steer_keeps_original_turn_pulse_anchored(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.runtime.submission = Submission(
            SubmitDisposition.STEERED,
            binding.id,
            "native-one",
            "turn-one",
        )
        self.assertTrue(
            await self.app._reactions.start(
                "turn-one",
                "om_original",
                pulse_enabled=True,
            )
        )

        await self.app.handle_message(
            FakeMessage(
                "new direction",
                message_id="om_steer",
                sender_id="ou_bob",
                display_name="Bob",
            )
        )

        call = self.runtime.submit_calls[-1]
        request_text, current_context = plain_prompt_projection(call["input"])
        self.assertEqual(request_text, "new direction")
        self.assertEqual(call["owner_id"], "ou_bob")
        self.assertEqual(current_context["sender"]["display_name"], "Bob")
        self.assertEqual(current_context["sender"]["open_id"], "ou_bob")
        self.assertEqual(call["origin"].id, "om_steer")
        self.assertIn("turn-one", self.app._reactions._pulses)
        self.assertEqual(self.channel.reaction_removals, [])
        self.assertIn(("om_original", "Typing"), self.channel.reactions)
        self.assertIn(("om_original", "THINKING"), self.channel.reactions)
        self.assertIn(("om_steer", "OnIt"), self.channel.reactions)

    async def test_failed_steer_has_no_confirmation_reaction(self) -> None:
        await self.new()

        async def fail_submit(**_kwargs: object) -> Submission:
            raise SteerRace("当前任务恰好已经结束，本条消息未执行，请重新发送。")

        self.runtime.submit = fail_submit  # type: ignore[method-assign]

        await self.app.handle_message(
            FakeMessage("new direction", message_id="om_steer")
        )

        self.assertNotIn(("om_steer", "OnIt"), self.channel.reactions)
        self.assertIn(
            (
                "om_steer",
                "当前任务恰好已经结束，本条消息未执行，请重新发送。",
            ),
            self.channel.replies,
        )

    async def test_accepted_steer_falls_back_to_text_if_reaction_fails(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.runtime.submission = Submission(
            SubmitDisposition.STEERED,
            binding.id,
            "native-one",
            "turn-one",
        )
        self.channel.fail_once_reaction_on = "OnIt"

        with self.assertLogs("netizen.channel_app", level="ERROR"):
            await self.app.handle_message(
                FakeMessage("new direction", message_id="om_steer")
            )

        self.assertIn(("om_steer", "已接收调整。"), self.channel.replies)

    async def test_any_delivered_sender_can_manage_a_group_scope(self) -> None:
        await self.new()
        self.assertEqual(
            len(self.store.list_bindings(
                FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT).key
            )),
            1,
        )
        group = FakeMessage(
            "/new",
            message_id="om_group",
            sender_id="ou_not_configured",
            chat_id="oc_not_configured",
            chat_type="group",
            mentioned_bot=True,
        )

        await self.app.handle_message(group)
        picker = self.channel.replies[-1][1]
        await self.app.handle_card_action(
            self.form_card_event(
                self.new_form_values(picker),
                message_id="om_group_card",
                chat_id="oc_not_configured",
                thread_id=None,
                sender_id="ou_not_configured",
            )
        )

        scope = FeishuScope(
            "cli_test", "oc_not_configured", ScopeKind.GROUP
        )
        self.assertIsNotNone(self.store.active_binding(scope.key))
        self.assertTrue(
            any(
                message_id == "om_group_card" and "Project 选择成功" in str(text)
                for message_id, text in self.channel.replies
            )
            or "Project 选择成功" in str(self.channel.updates[-1][1])
        )

    async def test_group_and_topics_require_mention_and_have_distinct_scopes(self) -> None:
        ignored = FakeMessage(
            "/new",
            message_id="om_ignored",
            sender_id="ou_participant",
            chat_id="oc_group",
            chat_type="group",
            thread_id="omt_unmentioned",
            mentioned_bot=False,
            raw_content_type="post",
        )
        await self.app.handle_message(ignored)
        self.assertEqual(self.channel.replies, [])

        first = FakeMessage(
            "/new",
            message_id="om_topic_1",
            sender_id="ou_participant",
            chat_id="oc_group",
            chat_type="group",
            thread_id="omt_one",
        )
        second = FakeMessage(
            "/new",
            message_id="om_topic_2",
            sender_id="ou_participant",
            chat_id="oc_group",
            chat_type="group",
            thread_id="omt_two",
        )
        await self.app.handle_message(first)
        await self.app.handle_message(second)
        first_picker, second_picker = (
            self.channel.replies[-2][1],
            self.channel.replies[-1][1],
        )
        await self.app.handle_card_action(
            self.form_card_event(
                self.new_form_values(first_picker),
                message_id="om_topic_card_1",
                chat_id="oc_group",
                thread_id="omt_one",
            )
        )
        await self.app.handle_card_action(
            self.form_card_event(
                self.new_form_values(second_picker),
                message_id="om_topic_card_2",
                chat_id="oc_group",
                thread_id="omt_two",
            )
        )

        one = FeishuScope(
            "cli_test", "oc_group", ScopeKind.TOPIC, "omt_one"
        )
        two = FeishuScope(
            "cli_test", "oc_group", ScopeKind.TOPIC, "omt_two"
        )
        self.assertNotEqual(
            self.store.active_binding(one.key).id,
            self.store.active_binding(two.key).id,
        )

    async def test_topic_root_post_repairs_sdk_bot_mention_before_command(self) -> None:
        bot_mention = SimpleNamespace(
            key="@_user_1",
            open_id="ou_bot",
            name="椰羊",
        )
        message = FakeMessage(
            "@椰羊 /new",
            message_id="om_topic_root",
            sender_id="ou_participant",
            chat_id="oc_group",
            chat_type="group",
            thread_id="omt_new_topic",
            mentioned_bot=True,
            raw_content_type="post",
            mentions=[bot_mention],
            content=SimpleNamespace(
                post={
                    "title": "",
                    "content": [[
                        {
                            "tag": "at",
                            "user_id": "@_user_1",
                            "user_name": "椰羊",
                            "style": [],
                        },
                        {"tag": "text", "text": " /new", "style": []},
                    ]],
                    "content_v2": [[
                        {
                            "tag": "at",
                            "user_id": "@_user_1",
                            "user_name": "椰羊",
                            "style": [],
                        },
                        {"tag": "text", "text": " /new", "style": []},
                    ]],
                }
            ),
        )

        await self.app.handle_message(message)
        picker = self.channel.replies[-1][1]
        await self.app.handle_card_action(
            self.form_card_event(
                self.new_form_values(picker),
                message_id="om_topic_root_card",
                chat_id="oc_group",
                thread_id="omt_new_topic",
            )
        )

        scope = FeishuScope(
            "cli_test", "oc_group", ScopeKind.TOPIC, "omt_new_topic"
        )
        self.assertIsNotNone(self.store.active_binding(scope.key))
        self.assertTrue(
            any(
                message_id == "om_topic_root_card"
                and "Project 选择成功" in str(text)
                for message_id, text in self.channel.replies
            )
            or "Project 选择成功" in str(self.channel.updates[-1][1])
        )

    async def test_group_post_with_image_repairs_bot_mention_and_submits_pixels(
        self,
    ) -> None:
        await self.create_binding(
            FeishuScope(
                "cli_test", "oc_group", ScopeKind.TOPIC, "omt_image_topic"
            )
        )
        scope = FeishuScope(
            "cli_test", "oc_group", ScopeKind.TOPIC, "omt_image_topic"
        )
        binding = self.store.active_binding(scope.key)
        self.runtime.submission = Submission(
            SubmitDisposition.STARTED,
            binding.id,
            "native-one",
            "turn-one",
            lambda: None,
        )
        bot_mention = SimpleNamespace(
            key="@_user_1",
            open_id="ou_bot",
            name="椰羊",
        )
        message = FakeMessage(
            "@椰羊 inspect ![image](img_group)",
            message_id="om_group_post_image",
            chat_id="oc_group",
            chat_type="group",
            thread_id="omt_image_topic",
            raw_content_type="post",
            mentions=[bot_mention],
            resources=[ResourceDescriptor(type="image", file_key="img_group")],
            content=PostContent(
                post={
                    "content": [[
                        {
                            "tag": "at",
                            "user_id": "@_user_1",
                            "user_name": "椰羊",
                            "style": [],
                        },
                        {"tag": "text", "text": " inspect ", "style": []},
                        {"tag": "img", "image_key": "img_group"},
                    ]]
                }
            ),
        )
        self.channel.resource_bodies[("om_group_post_image", "img_group")] = PNG

        await self.app.handle_message(message)

        native_input = self.runtime.submit_calls[0]["input"]
        self.assertEqual(
            json.loads(native_input[0].text)["source"],
            "current_message",
        )
        self.assertEqual(json.loads(native_input[0].text)["ref"], "img1")
        self.assertNotIn("file_key", json.loads(native_input[0].text))
        self.assertIsInstance(native_input[1], ImageInput)
        request_text, current_context = plain_prompt_projection(native_input)
        self.assertEqual(request_text, "inspect ![image](img1)")
        self.assertEqual(current_context["message_type"], "post")
        self.assertEqual(current_context["content_fidelity"], "full_multimodal")
        self.assertEqual(
            self.channel.download_resource_calls,
            [("img_group", "image", "om_group_post_image")],
        )

    async def test_post_does_not_strip_another_leading_mention(self) -> None:
        message = FakeMessage(
            "@同事 @椰羊 /new test",
            message_id="om_other_mention_first",
            chat_id="oc_group",
            chat_type="group",
            thread_id="omt_other_mention_first",
            raw_content_type="post",
            mentions=[
                SimpleNamespace(
                    key="@_user_1",
                    open_id="ou_colleague",
                    name="同事",
                ),
                SimpleNamespace(
                    key="@_user_2",
                    open_id="ou_bot",
                    name="椰羊",
                ),
            ],
            content=SimpleNamespace(
                post={
                    "title": "",
                    "content": [[
                        {
                            "tag": "at",
                            "user_id": "@_user_1",
                            "user_name": "同事",
                            "style": [],
                        },
                        {"tag": "text", "text": " ", "style": []},
                        {
                            "tag": "at",
                            "user_id": "@_user_2",
                            "user_name": "椰羊",
                            "style": [],
                        },
                        {"tag": "text", "text": " /new test", "style": []},
                    ]],
                }
            ),
        )

        await self.app.handle_message(message)

        scope = FeishuScope(
            "cli_test", "oc_group", ScopeKind.TOPIC, "omt_other_mention_first"
        )
        self.assertIsNone(self.store.active_binding(scope.key))
        self.assertEqual(
            self.channel.replies,
            [
                (
                    "om_other_mention_first",
                    "当前聊天或话题还没有会话，请先发送 /new。",
                )
            ],
        )

    async def test_post_with_unsupported_resource_is_rejected(self) -> None:
        message = FakeMessage(
            "/new",
            message_id="om_topic_with_resource",
            chat_id="oc_group",
            chat_type="group",
            thread_id="omt_with_resource",
            raw_content_type="post",
            resources=[object()],
        )

        await self.app.handle_message(message)

        scope = FeishuScope(
            "cli_test", "oc_group", ScopeKind.TOPIC, "omt_with_resource"
        )
        self.assertIsNone(self.store.active_binding(scope.key))
        self.assertEqual(
            self.channel.replies,
            [
                (
                    "om_topic_with_resource",
                    "当前消息还包含暂不支持的附件；"
                    "目前仅支持普通图片和富文本图片。",
                )
            ],
        )

    async def test_post_top_level_folder_with_body_is_rejected(self) -> None:
        message = FakeMessage(
            "/new",
            message_id="om_topic_with_folder",
            chat_id="oc_group",
            chat_type="group",
            thread_id="omt_with_folder",
            raw_content_type="post",
            content=PostContent(
                post={
                    "zh_cn": {
                        "content_v2": [[
                            {"tag": "text", "text": "/new"},
                        ]]
                    },
                    "files": [
                        {
                            "file_key": "folder_specs",
                            "file_name": "Specs",
                            "is_folder": True,
                        }
                    ],
                }
            ),
        )

        await self.app.handle_message(message)

        scope = FeishuScope(
            "cli_test", "oc_group", ScopeKind.TOPIC, "omt_with_folder"
        )
        self.assertIsNone(self.store.active_binding(scope.key))
        self.assertEqual(
            self.channel.replies,
            [
                (
                    "om_topic_with_folder",
                    "当前消息还包含暂不支持的附件；"
                    "目前仅支持普通图片和富文本图片。",
                )
            ],
        )

    async def test_bare_group_mention_is_not_sent_to_codex(self) -> None:
        message = FakeMessage(
            "",
            message_id="om_bare_mention",
            sender_id="ou_participant",
            chat_id="oc_group",
            chat_type="group",
            mentioned_bot=True,
        )
        message.safe_content_text = "@Netizen"

        await self.app.handle_message(message)

        self.assertEqual(self.runtime.submit_calls, [])
        self.assertIn((message.id, "消息内容为空。"), self.channel.replies)

    async def test_resume_switches_binding_while_old_runtime_can_remain_active(self) -> None:
        await self.new(message_id="om_new_1")
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        first = self.store.active_binding(scope.key)
        self.runtime.active[first.id] = ActiveTurnSnapshot(
            first.id,
            "native-one",
            "turn-one",
            "ou_user",
            ActiveState.RUNNING,
        )
        await self.new(message_id="om_new_2")
        second = self.store.active_binding(scope.key)
        self.assertNotEqual(first.id, second.id)

        await self.app.handle_message(
            FakeMessage(f"/resume {first.short_id}", message_id="om_resume")
        )

        self.assertEqual(self.store.active_binding(scope.key).id, first.id)
        self.assertIn(first.id, self.runtime.active)
        self.assertEqual(
            self.runtime.active_binding_change_calls[-1],
            (second.id, first.id),
        )

    async def test_compact_is_unavailable_without_native_mutation(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-one")
        command = FakeMessage("/compact", message_id="om_compact")

        await self.app.handle_message(command)

        self.assertEqual(self.runtime.compact_calls, [])
        self.assertIn(
            (
                "om_compact",
                "/compact 尚未开放：固定 openai-codex 0.147.0 的压缩后同连接"
                "继续 Turn 兼容验证未通过，本条消息未执行。",
            ),
            self.channel.replies,
        )

    async def test_compacting_state_blocks_config_and_stop_does_not_fake_interrupt(
        self,
    ) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.store.assign_native_thread_id(binding.id, "native-one")
        self.runtime.compacting.add(binding.id)

        await self.app.handle_message(FakeMessage("/status", message_id="om_status"))
        self.assertIn("状态：compacting", self.channel.replies[-1][1])

        await self.app.handle_message(FakeMessage("/config", message_id="om_config"))
        self.assertIn("正在压缩上下文", self.channel.replies[-1][1])
        self.assertEqual(self.runtime.model_catalog_calls, 0)

        self.runtime.stop_result = StopDisposition.COMPACTING
        await self.app.handle_message(FakeMessage("/stop", message_id="om_stop"))
        self.assertIn("/stop 只中断普通 Turn", self.channel.replies[-1][1])
        self.assertNotIn(
            ("om_stop", "正在中断当前 Codex Turn。"),
            self.channel.replies,
        )

    async def test_scope_participant_can_stop_and_completion_uses_origin(self) -> None:
        origin = await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.runtime.active[binding.id] = ActiveTurnSnapshot(
            binding.id,
            "native-one",
            "turn-one",
            "ou_originator",
            ActiveState.RUNNING,
        )

        stop_sender = "ou_other_participant"
        self.assertNotEqual(stop_sender, self.runtime.active[binding.id].owner_id)
        await self.app.handle_message(
            FakeMessage("/stop", message_id="om_stop", sender_id=stop_sender)
        )
        self.assertIn(("om_stop", "正在中断当前 Codex Turn。"), self.channel.replies)

        outcome = TurnOutcome(
            binding_id=binding.id,
            thread_id="native-one",
            turn_id="turn-one",
            owner_id="ou_originator",
            origin=origin,
            result=SimpleNamespace(
                final_response="done",
                status=SimpleNamespace(value="completed"),
            ),
        )
        await self.app.handle_completion(outcome)
        self.assertIn((origin.id, "done"), self.channel.replies)
        self.assertIn((origin.id, "DONE"), self.channel.reactions)

    async def test_turn_observation_unavailable_notice_preserves_exit_paths(
        self,
    ) -> None:
        origin = await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        unavailable = turn_activity_snapshot(
            binding_id=binding.id,
            revision=2,
            state=ActiveState.OBSERVATION_UNAVAILABLE,
        )
        self.runtime.turn_activity_values[binding.id] = unavailable
        self.channel.reply_results.append(
            sent_result("om_unavailable_progress", chat_id="oc_direct")
        )
        self.assertTrue(
            await self.app._progress_cards.start(
                binding_id=binding.id,
                thread_id="native-one",
                turn_id="turn-one",
                origin=origin,
            )
        )
        self.assertTrue(
            await self.app._reactions.start(
                "turn-one",
                origin.id,
                pulse_enabled=True,
            )
        )
        outcome = TurnObservationUnavailableOutcome(
            binding_id=binding.id,
            thread_id="native-one",
            turn_id="turn-one",
            owner_id="ou_user",
            origin=origin,
            error=TerminalStateUnknown("bounded observation failed"),
        )

        await self.app.handle_completion(outcome)

        notice = self.channel.replies[-1][1]
        self.assertIn("短暂重试后仍无法确认状态", notice)
        self.assertIn("已停止后台读取", notice)
        self.assertIn("当前会话及上下文仍保留", notice)
        self.assertIn("`/sessions`", notice)
        self.assertEqual(self.app._progress_cards._sessions, {})
        self.assertEqual(self.app._reactions._pulses, {})
        self.assertIn("Turn 观测不可用", str(self.channel.updates[-1][1]))

        feedback = BindingTaskFeedback(
            reaction_pulse_enabled=True,
            progress_card_enabled=True,
        )
        await self.app.handle_completion(
            TurnOutcome(
                binding_id=binding.id,
                thread_id="native-one",
                turn_id="turn-one",
                owner_id="ou_user",
                origin=origin,
                result=completed_turn_result(final_response="recovered terminal"),
                task_feedback=feedback,
                activity=unavailable,
            )
        )
        self.assertIn((origin.id, "recovered terminal"), self.channel.replies)

    async def test_thread_activity_discard_stops_presenters_without_terminal(self) -> None:
        origin = await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        activity = turn_activity_snapshot(binding_id=binding.id)
        self.runtime.turn_activity_values[binding.id] = activity
        self.channel.reply_results.append(
            sent_result("om_discarded_progress", chat_id="oc_direct")
        )
        self.assertTrue(
            await self.app._progress_cards.start(
                binding_id=binding.id,
                thread_id="native-one",
                turn_id="turn-one",
                origin=origin,
            )
        )
        self.assertTrue(
            await self.app._reactions.start(
                "turn-one",
                origin.id,
                pulse_enabled=True,
            )
        )
        reply_count = len(self.channel.replies)

        await self.app.handle_completion(
            ThreadActivityDiscardedOutcome(
                binding_id=binding.id,
                thread_id="native-one",
                turn_id="turn-one",
            )
        )

        self.assertEqual(self.app._progress_cards._sessions, {})
        self.assertEqual(self.app._reactions._pulses, {})
        self.assertEqual(len(self.channel.replies), reply_count)

    async def test_thread_discard_clears_a_static_goal_card_route(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        goal = native_goal(GoalStatus.PAUSED)
        await self.register_goal_card(
            scope=scope,
            binding=binding,
            goal=goal,
            message_id="om_archived_goal",
            runtime_state=GoalStatus.PAUSED.value,
            logical_turn_id=f"snapshot:{goal_generation(goal)}",
        )

        await self.app.handle_completion(
            ThreadActivityDiscardedOutcome(
                binding_id=binding.id,
                thread_id=goal.thread_id,
                turn_id=None,
            )
        )

        self.assertEqual(self.app._progress_cards._goal_sessions, {})
        self.assertEqual(self.app._progress_cards._goal_latest_runs, {})
        self.assertEqual(self.app._progress_cards._goal_cards, {})

    async def test_progress_card_start_rechecks_exact_turn_after_reply(self) -> None:
        origin = await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.runtime.turn_activity_values[binding.id] = turn_activity_snapshot(
            binding_id=binding.id
        )
        reply_started = asyncio.Event()
        release_reply = asyncio.Event()

        async def delayed_reply(message, content, opts=None):
            reply_started.set()
            await release_reply.wait()
            return sent_result("om_stale_progress", chat_id="oc_direct")

        self.channel.reply = delayed_reply  # type: ignore[method-assign]
        starting = asyncio.create_task(
            self.app._progress_cards.start(
                binding_id=binding.id,
                thread_id="native-one",
                turn_id="turn-one",
                origin=origin,
            )
        )
        await asyncio.wait_for(reply_started.wait(), timeout=0.1)
        self.runtime.turn_activity_values.pop(binding.id)
        release_reply.set()

        self.assertFalse(await starting)
        self.assertEqual(self.app._progress_cards._sessions, {})

    async def test_stop_ack_precedes_background_cleanup_warning(self) -> None:
        origin = await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.runtime.active[binding.id] = ActiveTurnSnapshot(
            binding.id,
            "native-one",
            "turn-one",
            "ou_user",
            ActiveState.RUNNING,
        )

        await self.app.handle_message(FakeMessage("/stop", message_id="om_stop"))
        self.assertIn(
            (
                "om_stop",
                "正在中断当前 Codex Turn。",
            ),
            self.channel.replies,
        )

        outcome = TurnOutcome(
            binding_id=binding.id,
            thread_id="native-one",
            turn_id="turn-one",
            owner_id="ou_user",
            origin=origin,
            result=SimpleNamespace(
                final_response=None,
                status=SimpleNamespace(value="interrupted"),
            ),
            background_cleanup_requested=True,
        )
        await self.app.handle_completion(outcome)
        self.assertIn(
            (
                origin.id,
                "Codex Turn 已中断；已请求清理该 Thread 中已登记的后台终端。"
                "前台工具进程不受此接口保证，可能仍在运行。",
            ),
            self.channel.replies,
        )
        self.assertLess(
            self.channel.replies.index(("om_stop", "正在中断当前 Codex Turn。")),
            self.channel.replies.index(
                (
                    origin.id,
                    "Codex Turn 已中断；已请求清理该 Thread 中已登记的后台终端。"
                    "前台工具进程不受此接口保证，可能仍在运行。",
                )
            ),
        )
        self.assertEqual(
            [
                text
                for message_id, text in self.channel.replies
                if message_id == "om_stop"
            ],
            ["正在中断当前 Codex Turn。"],
        )

    async def test_cleanup_failure_is_visible_and_does_not_claim_stopped(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.runtime.active[binding.id] = ActiveTurnSnapshot(
            binding.id,
            "native-one",
            "turn-one",
            "ou_user",
            ActiveState.RUNNING,
        )

        async def fail_stop(
            _binding_id: str,
            *,
            acknowledge,
        ) -> StopDisposition:
            await acknowledge()
            raise TerminalCleanupFailed(
                "Codex Turn 已请求中断，但已登记后台终端的清理请求失败；"
                "当前会话保持 stopping，不能假定后台终端已经停止。"
                "请再次发送 /stop 重试清理。"
            )

        self.runtime.stop = fail_stop  # type: ignore[method-assign]
        await self.app.handle_message(FakeMessage("/stop", message_id="om_stop"))

        replies = [
            text for message_id, text in self.channel.replies if message_id == "om_stop"
        ]
        self.assertEqual(replies[0], "正在中断当前 Codex Turn。")
        reply = replies[-1]
        self.assertIn("已登记后台终端的清理请求失败", reply)
        self.assertIn("不能假定后台终端已经停止", reply)
        self.assertIn("再次发送 /stop", reply)

    async def test_stop_ack_is_immediate_while_native_cleanup_is_blocked(self) -> None:
        await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        self.runtime.active[binding.id] = ActiveTurnSnapshot(
            binding.id,
            "native-one",
            "turn-one",
            "ou_user",
            ActiveState.RUNNING,
        )
        stop_entered = asyncio.Event()
        release_stop = asyncio.Event()

        async def blocked_stop(
            _binding_id: str,
            *,
            acknowledge,
        ) -> StopDisposition:
            await acknowledge()
            stop_entered.set()
            await release_stop.wait()
            return StopDisposition.REQUESTED

        self.runtime.stop = blocked_stop  # type: ignore[method-assign]
        task = asyncio.create_task(
            self.app.handle_message(FakeMessage("/stop", message_id="om_stop"))
        )
        await stop_entered.wait()

        self.assertIn(
            ("om_stop", "正在中断当前 Codex Turn。"),
            self.channel.replies,
        )
        self.assertFalse(task.done())
        release_stop.set()
        await task

    async def test_external_interrupt_does_not_claim_background_cleanup(self) -> None:
        origin = await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)
        await self.app.handle_completion(
            TurnOutcome(
                binding_id=binding.id,
                thread_id="native-one",
                turn_id="turn-one",
                owner_id="ou_user",
                origin=origin,
                result=SimpleNamespace(
                    final_response=None,
                    status=SimpleNamespace(value="interrupted"),
                ),
            )
        )

        self.assertIn(
            (
                origin.id,
                "Codex Turn 已被外部中断；本服务未请求清理已登记的后台终端。"
                "前台工具进程可能仍在运行。",
            ),
            self.channel.replies,
        )
        self.assertIn((origin.id, "CrossMark"), self.channel.reactions)

    async def test_failed_turn_uses_error_reaction_and_failure_reply(self) -> None:
        origin = await self.new()
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        binding = self.store.active_binding(scope.key)

        await self.app.handle_completion(
            TurnOutcome(
                binding_id=binding.id,
                thread_id="native-one",
                turn_id="turn-one",
                owner_id="ou_user",
                origin=origin,
                result=SimpleNamespace(
                    final_response="native failure",
                    status=SimpleNamespace(value="failed"),
                ),
            )
        )

        self.assertIn((origin.id, "ERROR"), self.channel.reactions)
        self.assertIn((origin.id, "任务未完成：native failure"), self.channel.replies)


class SideChannelApplicationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.project = root / "project"
        self.project.mkdir()
        self.next_id = 0

        def make_id() -> str:
            self.next_id += 1
            return f"record-{self.next_id}"

        self.store = BindingStore(id_factory=make_id)
        self.channel = FakeChannel()
        self.runtime = StubRuntime()
        self.runtime.binding_store = self.store
        self.runtime.available_capabilities = frozenset({NativeCapability.SIDE})
        self.projects = ProjectRegistry(
            store=self.store,
            project_root=root,
            projects={"test": self.project},
        )
        self.app = ChannelApplication(
            app_id="cli_test",
            channel=self.channel,
            runtime=self.runtime,  # type: ignore[arg-type]
            bindings=self.store,
            projects=self.projects,
        )

    async def asyncTearDown(self) -> None:
        await self.app.close()
        self.store.close()
        self.tmp.cleanup()

    def binding_for(
        self,
        message: FakeMessage,
        *,
        task_feedback: BindingTaskFeedback = BindingTaskFeedback(),
    ):
        scope = self.app._scope(message)
        binding = self.store.create_binding(
            scope=scope,
            project_alias="test",
            creator_id="ou_owner",
            task_feedback=task_feedback,
        )
        self.store.assign_native_thread_id(binding.id, f"native-{binding.id}")
        return self.store.get(binding.id)

    def queue_promoted_topic(
        self,
        *,
        chat_id: str,
        root_id: str,
        seed_id: str,
        topic_id: str,
    ) -> None:
        self.channel.send_results.extend(
            (
                sent_result(root_id, chat_id=chat_id),
                sent_result(
                    seed_id,
                    chat_id=chat_id,
                    thread_id=topic_id,
                    root_id=root_id,
                    parent_id=root_id,
                ),
            )
        )

    def queue_direct_topic(
        self,
        *,
        chat_id: str,
        root_id: str,
        topic_id: str,
    ) -> None:
        self.channel.send_results.append(
            sent_result(
                root_id,
                chat_id=chat_id,
                thread_id=topic_id,
                root_id=root_id,
            )
        )

    async def open_direct_side(
        self,
        *,
        task_feedback: BindingTaskFeedback = BindingTaskFeedback(),
    ):
        source = FakeMessage(
            "/side",
            message_id="om-side-source",
            chat_id="oc-direct",
            chat_type="p2p",
            mentioned_bot=False,
        )
        binding = self.binding_for(source, task_feedback=task_feedback)
        self.queue_promoted_topic(
            chat_id="oc-direct",
            root_id="om-side-root",
            seed_id="om-side-seed",
            topic_id="omt-side",
        )
        await self.app.handle_message(source)
        record = self.store.side_topic_for_source(
            app_id="cli_test",
            source_message_id=source.id,
        )
        assert record is not None
        return binding, record

    async def test_side_turn_default_feedback_keeps_rich_text_terminal_reply(
        self,
    ) -> None:
        binding, record = await self.open_direct_side()
        prompt = FakeMessage(
            "work quietly",
            message_id="om-side-prompt",
            chat_id="oc-direct",
            chat_type="p2p",
            thread_id=record.topic_id,
            mentioned_bot=False,
        )

        await self.app.handle_message(prompt)

        self.assertEqual(
            self.channel.reactions,
            [("om-side-prompt", "Typing")],
        )
        self.assertEqual(self.channel.replies, [])
        await self.app.handle_completion(
            SideTurnOutcome(
                side_id=record.id,
                parent_binding_id=binding.id,
                thread_id="native-side-1",
                turn_id="side-turn-1",
                owner_id="ou_user",
                origin=prompt,
                cwd=self.project,
                result=completed_turn_result(final_response="side answer"),
            )
        )

        self.assertEqual(self.channel.replies, [(prompt.id, "side answer")])
        self.assertEqual(
            self.channel.reactions,
            [
                ("om-side-prompt", "Typing"),
                ("om-side-prompt", "DONE"),
            ],
        )
        self.assertEqual(
            self.channel.reaction_removals,
            [("om-side-prompt", "reaction-1")],
        )

    async def test_side_rejected_steer_has_no_lifecycle_confirmation(self) -> None:
        _binding, record = await self.open_direct_side()

        async def reject_side_steer(**_kwargs: object) -> SideSubmission:
            raise SteerRace(
                "当前 Side Turn 恰好已经结束，本条消息未执行，请重新发送。"
            )

        self.runtime.submit_side = reject_side_steer  # type: ignore[method-assign]
        prompt = FakeMessage(
            "adjust side",
            message_id="om-side-rejected-steer",
            chat_id="oc-direct",
            chat_type="p2p",
            thread_id=record.topic_id,
            mentioned_bot=False,
        )

        await self.app.handle_message(prompt)

        self.assertNotIn((prompt.id, "OnIt"), self.channel.reactions)
        self.assertIn(
            (
                prompt.id,
                "当前 Side Turn 恰好已经结束，本条消息未执行，请重新发送。",
            ),
            self.channel.replies,
        )

    async def test_side_accepted_steer_falls_back_when_reaction_fails(
        self,
    ) -> None:
        _binding, record = await self.open_direct_side()
        self.runtime.side_submission = SideSubmission(
            SubmitDisposition.STEERED,
            record.id,
            "native-side-1",
            "side-turn-1",
        )
        self.channel.fail_once_reaction_on = "OnIt"
        prompt = FakeMessage(
            "adjust side",
            message_id="om-side-steer-fallback",
            chat_id="oc-direct",
            chat_type="p2p",
            thread_id=record.topic_id,
            mentioned_bot=False,
        )

        with self.assertLogs("netizen.channel_app", level="ERROR"):
            await self.app.handle_message(prompt)

        self.assertIn((prompt.id, "已接收 Side 调整。"), self.channel.replies)

    async def test_side_failed_and_interrupted_turns_keep_terminal_reactions(
        self,
    ) -> None:
        binding, record = await self.open_direct_side()
        cases = (
            ("failed", "ERROR", "side failure"),
            ("interrupted", "CrossMark", None),
        )

        for index, (status, reaction, final_response) in enumerate(cases, start=1):
            with self.subTest(status=status):
                origin = FakeMessage(
                    "side work",
                    message_id=f"om-side-{status}",
                    chat_id="oc-direct",
                    chat_type="p2p",
                    thread_id=record.topic_id,
                    mentioned_bot=False,
                )
                await self.app.handle_completion(
                    SideTurnOutcome(
                        side_id=record.id,
                        parent_binding_id=binding.id,
                        thread_id="native-side-1",
                        turn_id=f"side-turn-terminal-{index}",
                        owner_id="ou_user",
                        origin=origin,
                        cwd=self.project,
                        result=SimpleNamespace(
                            status=SimpleNamespace(value=status),
                            final_response=final_response,
                        ),
                    )
                )

                self.assertIn((origin.id, reaction), self.channel.reactions)

    async def test_side_turn_without_progress_uses_result_files_card(self) -> None:
        binding, record = await self.open_direct_side()
        artifact = self.project / "side-output.txt"
        artifact.write_text("side", encoding="utf-8")
        prompt = FakeMessage(
            "create a file",
            message_id="om-side-file-prompt",
            chat_id="oc-direct",
            chat_type="p2p",
            thread_id=record.topic_id,
            mentioned_bot=False,
        )
        await self.app.handle_message(prompt)

        await self.app.handle_completion(
            SideTurnOutcome(
                side_id=record.id,
                parent_binding_id=binding.id,
                thread_id="native-side-1",
                turn_id="side-turn-1",
                owner_id="ou_user",
                origin=prompt,
                cwd=self.project,
                result=completed_turn_result(
                    file_change_item("side-output.txt"),
                    final_response="side file ready",
                ),
            )
        )

        card = self.channel.replies[-1][1]
        self.assertIsInstance(card, OutboundCard)
        assert isinstance(card, OutboundCard)
        visible = json.dumps(card.card, ensure_ascii=False)
        self.assertIn("side file ready", visible)
        self.assertIn("side-output.txt", visible)
        self.assertNotIn("collapsible_panel", visible)
        self.assertIn("<font color='green'>+0", visible)
        self.assertIn("累计修改", visible)
        self.assertNotIn("4 B", visible)
        send_value = _card_button_value(card, "发送")
        self.assertEqual(send_value["v"], 4)
        self.assertEqual(send_value["binding_id"], f"binding:v1:{binding.id}")
        self.assertEqual(send_value["topic_id"], record.topic_id)

    async def test_side_turn_progress_updates_one_card_and_keeps_files(
        self,
    ) -> None:
        feedback = BindingTaskFeedback(
            reaction_pulse_enabled=True,
            progress_card_enabled=True,
        )
        binding, record = await self.open_direct_side(task_feedback=feedback)
        artifact = self.project / "side-progress.txt"
        artifact.write_text("side", encoding="utf-8")
        initial = side_turn_activity_snapshot(side_id=record.id)
        self.runtime.side_turn_activity_values[record.id] = initial
        self.app._progress_cards = channel_app._ProgressCardController(
            self.channel,
            self.runtime,  # type: ignore[arg-type]
            poll_seconds=0.01,
        )
        self.channel.reply_results.append(
            sent_result(
                "om-side-progress",
                chat_id="oc-direct",
                thread_id=record.topic_id,
            )
        )
        root_updates = len(self.channel.updates)
        prompt = FakeMessage(
            "create with progress",
            message_id="om-side-progress-prompt",
            chat_id="oc-direct",
            chat_type="p2p",
            thread_id=record.topic_id,
            mentioned_bot=False,
        )

        await self.app.handle_message(prompt)

        self.assertEqual(len(self.channel.replies), 1)
        self.assertIn((prompt.id, "Typing"), self.channel.reactions)
        running = self.channel.replies[0][1]
        self.assertIsInstance(running, OutboundCard)
        assert isinstance(running, OutboundCard)
        panel = next(iter(_elements(running.card, "collapsible_panel")))
        self.assertTrue(panel["expanded"])
        updated = side_turn_activity_snapshot(
            side_id=record.id,
            revision=2,
            steps=(
                TurnPlanStepSnapshot(
                    "create side file",
                    TurnPlanStepState.COMPLETED,
                ),
            ),
        )
        self.runtime.side_turn_activity_values[record.id] = updated
        async with asyncio.timeout(1):
            while len(self.channel.updates) == root_updates:
                await asyncio.sleep(0.01)
        self.assertEqual(self.channel.updates[-1][0], "om-side-progress")

        await self.app.handle_completion(
            SideTurnOutcome(
                side_id=record.id,
                parent_binding_id=binding.id,
                thread_id="native-side-1",
                turn_id="side-turn-1",
                owner_id="ou_user",
                origin=prompt,
                cwd=self.project,
                result=completed_turn_result(
                    file_change_item("side-progress.txt"),
                    final_response="side progress complete",
                ),
                task_feedback=feedback,
                activity=updated,
            )
        )

        self.assertEqual(len(self.channel.replies), 1)
        self.assertEqual(self.channel.updates[-1][0], "om-side-progress")
        terminal = self.channel.updates[-1][1]
        panel = next(iter(_elements(terminal, "collapsible_panel")))
        self.assertFalse(panel["expanded"])
        visible = json.dumps(terminal, ensure_ascii=False)
        self.assertIn("side progress complete", visible)
        self.assertIn("side-progress.txt", visible)
        self.assertIn("create side file", visible)
        self.assertIn((prompt.id, "DONE"), self.channel.reactions)

    async def test_side_progress_start_failure_falls_back_at_terminal(self) -> None:
        feedback = BindingTaskFeedback(progress_card_enabled=True)
        binding, record = await self.open_direct_side(task_feedback=feedback)
        activity = side_turn_activity_snapshot(side_id=record.id)
        self.runtime.side_turn_activity_values[record.id] = activity
        self.channel.reply_results.append(RuntimeError("progress send failed"))
        root_updates = len(self.channel.updates)
        prompt = FakeMessage(
            "survive display failure",
            message_id="om-side-progress-failed",
            chat_id="oc-direct",
            chat_type="p2p",
            thread_id=record.topic_id,
            mentioned_bot=False,
        )

        with self.assertLogs("netizen.channel_app", level="ERROR"):
            await self.app.handle_message(prompt)
        await self.app.handle_completion(
            SideTurnOutcome(
                side_id=record.id,
                parent_binding_id=binding.id,
                thread_id="native-side-1",
                turn_id="side-turn-1",
                owner_id="ou_user",
                origin=prompt,
                cwd=self.project,
                result=completed_turn_result(
                    final_response="side answer survives",
                ),
                task_feedback=feedback,
                activity=activity,
            )
        )

        self.assertEqual(
            self.channel.replies[-1],
            (prompt.id, "side answer survives"),
        )
        self.assertEqual(len(self.channel.updates), root_updates)

    async def test_side_rejects_goal_even_when_native_goal_is_available(self) -> None:
        self.runtime.available_capabilities = frozenset(
            {NativeCapability.SIDE, NativeCapability.GOAL}
        )
        _binding, record = await self.open_direct_side()

        await self.app.handle_message(
            FakeMessage(
                "/goal ship",
                message_id="om-side-goal",
                chat_id="oc-direct",
                chat_type="p2p",
                thread_id=record.topic_id,
                mentioned_bot=False,
            )
        )

        self.assertEqual(self.runtime.start_goal_calls, [])
        self.assertIn("该命令在 Side 中不可用", str(self.channel.replies[-1][1]))

    async def test_side_creation_covers_all_entry_contexts_and_always_sends_fresh(
        self,
    ) -> None:
        cases = (
            FakeMessage(
                "/side",
                message_id="om-direct",
                chat_id="oc-direct",
                chat_type="p2p",
                mentioned_bot=False,
            ),
            FakeMessage(
                "/side",
                message_id="om-direct-topic",
                chat_id="oc-direct-topic",
                chat_type="p2p",
                thread_id="omt-parent-direct",
                mentioned_bot=False,
            ),
            FakeMessage(
                "/side",
                message_id="om-group",
                chat_id="oc-group",
                chat_type="group",
            ),
            FakeMessage(
                "/side",
                message_id="om-group-topic",
                chat_id="oc-group-topic",
                chat_type="group",
                thread_id="omt-parent-group",
            ),
            FakeMessage(
                "/side",
                message_id="om-topic-mode",
                chat_id="oc-topic-mode",
                chat_type="group",
            ),
        )
        for index, message in enumerate(cases, start=1):
            self.binding_for(message)
            if index == len(cases):
                self.queue_direct_topic(
                    chat_id=message.conversation.chat_id,
                    root_id=f"om-root-{index}",
                    topic_id=f"omt-side-{index}",
                )
            else:
                self.queue_promoted_topic(
                    chat_id=message.conversation.chat_id,
                    root_id=f"om-root-{index}",
                    seed_id=f"om-seed-{index}",
                    topic_id=f"omt-side-{index}",
                )

            before = len(self.channel.send_calls)
            await self.app.handle_message(message)
            calls = self.channel.send_calls[before:]
            self.assertEqual(calls[0][0], message.conversation.chat_id)
            self.assertIsNone(calls[0][2].reply_to)
            self.assertEqual(calls[0][2].receive_id_type, "chat_id")
            self.assertNotIn("side.close", str(calls[0][1]))
            if message.conversation.thread_id is not None:
                self.assertNotIn(
                    message.conversation.thread_id,
                    str(calls[0][1]),
                )
            if index == len(cases):
                self.assertEqual(len(calls), 1)
            else:
                self.assertEqual(len(calls), 2)
                self.assertEqual(
                    calls[1][1],
                    channel_app._SIDE_EMPTY_TOPIC_PROMPT,
                )
                opts = calls[1][2]
                self.assertEqual(opts.receive_id_type, "chat_id")
                self.assertEqual(opts.reply_to, f"om-root-{index}")
                self.assertTrue(opts.reply_in_thread)
                self.assertEqual(opts.reply_target_gone, "fail")
            source_replies = [
                content
                for message_id, content in self.channel.replies
                if message_id == message.id
            ]
            self.assertEqual(source_replies, [])
            open_card = str(self.channel.updates[-1][1])
            self.assertNotIn("Side 已创建，可以开始多轮对话", open_card)
            if index == len(cases):
                self.assertIn(channel_app._SIDE_EMPTY_TOPIC_PROMPT, open_card)
            else:
                self.assertNotIn(channel_app._SIDE_EMPTY_TOPIC_PROMPT, open_card)
            record = self.store.side_topic_for_source(
                app_id="cli_test",
                source_message_id=message.id,
            )
            assert record is not None
            self.assertEqual(record.state, SideTopicState.OPEN)
            self.assertEqual(record.topic_id, f"omt-side-{index}")
            self.assertNotEqual(record.topic_id, message.conversation.thread_id)
            self.assertEqual(record.requires_mention, index >= 3)
            root_uuid = calls[0][2].uuid
            self.assertEqual(
                root_uuid,
                channel_app._side_send_uuid(
                    channel_app._SIDE_ROOT_UUID_PREFIX,
                    record.id,
                ),
            )
            self.assertLessEqual(len(root_uuid), 50)
            if len(calls) == 2:
                seed_uuid = calls[1][2].uuid
                self.assertEqual(
                    seed_uuid,
                    channel_app._side_send_uuid(
                        channel_app._SIDE_SEED_UUID_PREFIX,
                        record.id,
                    ),
                )
                self.assertNotEqual(seed_uuid, root_uuid)

        self.assertEqual(len(self.runtime.create_side_calls), 5)
        self.assertEqual(len(self.runtime.attach_side_calls), 5)

    async def test_initial_prompt_stays_in_new_topic_and_redelivery_is_idempotent(
        self,
    ) -> None:
        source = FakeMessage(
            "/side inspect this",
            message_id="om-source",
            chat_id="oc-direct",
            chat_type="p2p",
            mentioned_bot=False,
            reply_id="om-quoted-control",
            raw={
                "parent_id": "om-quoted-control",
                "root_id": "om-quoted-control",
            },
        )
        binding = self.binding_for(source, task_feedback=PULSE_ON)
        self.queue_promoted_topic(
            chat_id="oc-direct",
            root_id="om-root",
            seed_id="om-seed",
            topic_id="omt-side",
        )

        await self.app.handle_message(source)

        self.assertEqual(len(self.runtime.submit_side_calls), 1)
        self.assertEqual(
            self.channel.send_calls[1][1],
            channel_app._side_initial_question_echo("inspect this"),
        )
        self.assertEqual(
            [
                content
                for message_id, content in self.channel.replies
                if message_id == source.id
            ],
            [],
        )
        self.assertNotIn(
            "Side 已创建，可以开始多轮对话",
            str(self.channel.updates[-1][1]),
        )
        submission = self.runtime.submit_side_calls[0]
        request_text, current_context = plain_prompt_projection(submission["input"])
        self.assertEqual(request_text, "inspect this")
        self.assertEqual(current_context["message_id"], "om-source")
        self.assertEqual(current_context["sender"]["open_id"], "ou_user")
        self.assertEqual(self.channel.fetch_inbound_calls, [])
        self.assertEqual(submission["owner_id"], "ou_user")
        self.assertEqual(submission["origin"].message_id, "om-seed")
        self.assertEqual(submission["origin"].conversation.thread_id, "omt-side")
        self.assertIn(("om-seed", "Typing"), self.channel.reactions)
        self.assertIn(("om-seed", "THINKING"), self.channel.reactions)

        await self.app.handle_message(source)

        self.assertEqual(len(self.runtime.create_side_calls), 1)
        self.assertEqual(len(self.channel.send_calls), 2)
        self.assertEqual(len(self.runtime.submit_side_calls), 1)
        self.assertEqual(
            [
                content
                for message_id, content in self.channel.replies
                if message_id == source.id
            ],
            [],
        )

        origin = submission["origin"]
        await self.app.handle_completion(
            SideTurnOutcome(
                side_id=submission["side_id"],
                parent_binding_id=binding.id,
                thread_id="native-side-1",
                turn_id="side-turn-1",
                owner_id="ou_user",
                origin=origin,
                cwd=self.project,
                result=SimpleNamespace(
                    status=SimpleNamespace(value="completed"),
                    final_response="side answer",
                ),
                task_feedback=PULSE_ON,
            )
        )
        self.assertIn(("om-seed", "side answer"), self.channel.replies)
        self.assertNotIn(("om-source", "side answer"), self.channel.replies)

    async def test_initial_prompt_missing_sender_name_reports_permission(self) -> None:
        source = FakeMessage(
            "/side inspect this",
            message_id="om-missing-side-source",
            display_name="",
            chat_id="oc-direct",
            chat_type="p2p",
            mentioned_bot=False,
        )
        self.binding_for(source)
        self.queue_promoted_topic(
            chat_id="oc-direct",
            root_id="om-missing-side-root",
            seed_id="om-missing-side-seed",
            topic_id="omt-missing-side",
        )

        await self.app.handle_message(source)

        self.assertEqual(self.runtime.submit_side_calls, [])
        self.assertIn(
            (
                "om-missing-side-seed",
                "无法获取当前消息发送者姓名，本条消息未执行；"
                "请为飞书应用开通 im:chat.members:read 权限、"
                "发布应用版本后重试。",
            ),
            self.channel.replies,
        )
        self.assertIn(
            "im:chat.members:read",
            str(self.channel.updates[-1][1]),
        )

    async def test_side_followup_missing_sender_name_blocks_steer(self) -> None:
        source = FakeMessage(
            "/side",
            message_id="om-side-source",
            chat_id="oc-direct",
            chat_type="p2p",
            mentioned_bot=False,
        )
        self.binding_for(source)
        self.queue_promoted_topic(
            chat_id="oc-direct",
            root_id="om-side-root",
            seed_id="om-side-seed",
            topic_id="omt-side-missing-name",
        )
        await self.app.handle_message(source)
        record = self.store.side_topic_for_source(
            app_id="cli_test",
            source_message_id=source.id,
        )
        assert record is not None
        self.runtime.side_submission = SideSubmission(
            SubmitDisposition.STEERED,
            record.id,
            "native-side-running",
            "side-turn-running",
        )

        await self.app.handle_message(
            FakeMessage(
                "adjust side",
                message_id="om-side-missing-sender",
                display_name="",
                chat_id="oc-direct",
                chat_type="p2p",
                thread_id="omt-side-missing-name",
                mentioned_bot=False,
            )
        )

        self.assertEqual(self.runtime.submit_side_calls, [])
        self.assertEqual(self.channel.reactions, [])
        self.assertIn(
            "im:chat.members:read",
            str(self.channel.replies[-1][1]),
        )

    async def test_direct_topic_initial_prompt_uses_question_echo_as_origin(
        self,
    ) -> None:
        source = FakeMessage(
            "/side inspect direct",
            message_id="om-source-direct",
            chat_id="oc-direct",
            chat_type="p2p",
            mentioned_bot=False,
        )
        self.binding_for(source, task_feedback=PULSE_ON)
        self.queue_direct_topic(
            chat_id="oc-direct",
            root_id="om-root-direct",
            topic_id="omt-side-direct",
        )
        self.channel.send_results.append(
            sent_result(
                "om-question-direct",
                chat_id="oc-direct",
                thread_id="omt-side-direct",
                root_id="om-root-direct",
                parent_id="om-root-direct",
            )
        )

        await self.app.handle_message(source)

        submission = self.runtime.submit_side_calls[0]
        self.assertEqual(submission["origin"].message_id, "om-question-direct")
        self.assertEqual(
            submission["origin"].conversation.thread_id,
            "omt-side-direct",
        )
        self.assertIn(("om-question-direct", "Typing"), self.channel.reactions)
        self.assertEqual(len(self.channel.send_calls), 2)
        _chat_id, content, opts = self.channel.send_calls[1]
        self.assertEqual(
            content,
            channel_app._side_initial_question_echo("inspect direct"),
        )
        self.assertEqual(opts.reply_to, "om-root-direct")
        self.assertTrue(opts.reply_in_thread)
        self.assertEqual(opts.reply_target_gone, "fail")
        record = self.store.side_topic_for_source(
            app_id="cli_test",
            source_message_id=source.id,
        )
        assert record is not None
        self.assertEqual(
            opts.uuid,
            channel_app._side_send_uuid(
                channel_app._SIDE_SEED_UUID_PREFIX,
                record.id,
            ),
        )
        self.assertNotEqual(opts.uuid, self.channel.send_calls[0][2].uuid)
        self.assertEqual(
            [
                content
                for message_id, content in self.channel.replies
                if message_id == source.id
            ],
            [],
        )

    def test_initial_question_echo_is_bounded_and_marks_excerpt(self) -> None:
        text = "x" * (channel_app._SIDE_INITIAL_QUESTION_MAX_CHARS + 100)

        echo = channel_app._side_initial_question_echo(text)

        self.assertIn("首轮问题（来自 /side 发起消息，内容节选）", echo)
        self.assertTrue(echo.endswith("…"))
        self.assertLess(len(echo), 3500)

        mention_echo = channel_app._side_initial_question_echo(
            '<AT user_id="all">everyone</AT>'
        )
        self.assertNotIn("<at", mention_echo.lower())
        self.assertNotIn("</at", mention_echo.lower())
        self.assertIn("‹AT", mention_echo)

    async def test_redelivery_identity_mismatch_fails_closed(self) -> None:
        original = FakeMessage(
            "/side",
            message_id="om-source",
            chat_id="oc-original",
            chat_type="p2p",
            mentioned_bot=False,
        )
        binding = self.binding_for(original)
        self.store.create_side_topic(
            app_id="cli_test",
            chat_id="oc-original",
            source_message_id="om-source",
            parent_binding_id=binding.id,
            creator_id="ou_user",
            requires_mention=False,
        )

        await self.app.handle_message(
            FakeMessage(
                "/side",
                message_id="om-source",
                chat_id="oc-other",
                chat_type="p2p",
                mentioned_bot=False,
            )
        )

        self.assertEqual(self.runtime.create_side_calls, [])
        self.assertEqual(self.channel.send_calls, [])
        self.assertTrue(
            any(
                message_id == "om-source" and "different identity" in str(content)
                for message_id, content in self.channel.replies
            )
        )

    async def test_fresh_side_root_must_not_belong_to_an_existing_topic(self) -> None:
        source = FakeMessage(
            "/side",
            message_id="om-source",
            chat_id="oc-direct",
            chat_type="p2p",
            mentioned_bot=False,
        )
        self.binding_for(source)
        self.channel.send_results.append(
            sent_result(
                "om-root",
                chat_id="oc-direct",
                root_id="om-existing-root",
                parent_id="om-existing-parent",
            )
        )

        await self.app.handle_message(source)

        record = self.store.side_topic_for_source(
            app_id="cli_test",
            source_message_id=source.id,
        )
        assert record is not None
        self.assertEqual(record.state, SideTopicState.FAILED)
        self.assertEqual(len(self.channel.send_calls), 1)
        self.assertEqual(
            self.runtime.close_side_calls,
            [(record.id, SideTopicState.FAILED)],
        )
        self.assertTrue(
            any("不是新话题根消息" in str(content) for _mid, content in self.channel.replies)
        )

    async def test_unknown_lark_send_reconciles_once_with_the_same_uuid(self) -> None:
        source = FakeMessage(
            "/side",
            message_id="om-source-reconcile",
            chat_id="oc-direct",
            chat_type="p2p",
            mentioned_bot=False,
        )
        self.binding_for(source)
        self.channel.send_results.extend(
            (
                RuntimeError("response lost"),
                sent_result(
                    "om-root-reconciled",
                    chat_id="oc-direct",
                    thread_id="omt-side-reconciled",
                    root_id="om-root-reconciled",
                ),
            )
        )

        await self.app.handle_message(source)

        record = self.store.side_topic_for_source(
            app_id="cli_test",
            source_message_id=source.id,
        )
        assert record is not None
        self.assertEqual(record.state, SideTopicState.OPEN)
        self.assertEqual(len(self.channel.send_calls), 2)
        first_opts = self.channel.send_calls[0][2]
        second_opts = self.channel.send_calls[1][2]
        self.assertIs(first_opts, second_opts)
        self.assertEqual(first_opts.uuid, second_opts.uuid)

    async def test_retryable_lark_send_reconciles_once_with_the_same_uuid(
        self,
    ) -> None:
        source = FakeMessage(
            "/side",
            message_id="om-source-retryable",
            chat_id="oc-direct",
            chat_type="p2p",
            mentioned_bot=False,
        )
        self.binding_for(source)
        self.channel.send_results.extend(
            (
                retryable_sent_result(),
                sent_result(
                    "om-root-reconciled",
                    chat_id="oc-direct",
                    thread_id="omt-side-reconciled",
                    root_id="om-root-reconciled",
                ),
            )
        )

        await self.app.handle_message(source)

        record = self.store.side_topic_for_source(
            app_id="cli_test",
            source_message_id=source.id,
        )
        assert record is not None
        self.assertEqual(record.state, SideTopicState.OPEN)
        calls = self.channel.send_calls[-2:]
        self.assertEqual(len(calls), 2)
        self.assertIs(calls[0][2], calls[1][2])
        self.assertEqual(calls[0][2].uuid, calls[1][2].uuid)

    async def test_lark_send_reconciliation_has_one_shared_retry_budget(self) -> None:
        cases = (
            (RuntimeError("response lost"), retryable_sent_result()),
            (retryable_sent_result(), retryable_sent_result()),
        )
        for index, results in enumerate(cases, start=1):
            with self.subTest(index=index):
                source = FakeMessage(
                    "/side",
                    message_id=f"om-source-budget-{index}",
                    chat_id=f"oc-budget-{index}",
                    chat_type="p2p",
                    mentioned_bot=False,
                )
                self.binding_for(source)
                self.channel.send_results.extend(results)
                before = len(self.channel.send_calls)

                await self.app.handle_message(source)

                record = self.store.side_topic_for_source(
                    app_id="cli_test",
                    source_message_id=source.id,
                )
                assert record is not None
                calls = self.channel.send_calls[before:]
                self.assertEqual(record.state, SideTopicState.FAILED)
                self.assertEqual(len(calls), 2)
                self.assertIs(calls[0][2], calls[1][2])
                self.assertEqual(calls[0][2].uuid, calls[1][2].uuid)

    async def test_seed_and_source_topic_relationships_fail_closed(self) -> None:
        for index, seed in enumerate(
            (
                sent_result(
                    "om-seed-wrong-root",
                    chat_id="oc-chat",
                    thread_id="omt-side-1",
                    root_id="om-other",
                    parent_id="om-root-1",
                ),
                sent_result(
                    "om-seed-wrong-parent",
                    chat_id="oc-chat",
                    thread_id="omt-side-2",
                    root_id="om-root-2",
                    parent_id="om-other",
                ),
            ),
            start=1,
        ):
            with self.subTest(index=index):
                source = FakeMessage(
                    "/side",
                    message_id=f"om-source-{index}",
                    chat_id="oc-chat",
                    chat_type="group",
                    thread_id=f"omt-parent-{index}",
                )
                self.binding_for(source)
                self.channel.send_results.extend(
                    (sent_result(f"om-root-{index}", chat_id="oc-chat"), seed)
                )
                await self.app.handle_message(source)
                record = self.store.side_topic_for_source(
                    app_id="cli_test",
                    source_message_id=source.id,
                )
                assert record is not None
                self.assertEqual(record.state, SideTopicState.FAILED)

        direct_query = FakeMessage(
            "/side inspect direct mismatch",
            message_id="om-source-direct-mismatch",
            chat_id="oc-direct-mismatch",
            chat_type="p2p",
            mentioned_bot=False,
        )
        self.binding_for(direct_query)
        self.channel.send_results.extend(
            (
                sent_result(
                    "om-root-direct-mismatch",
                    chat_id="oc-direct-mismatch",
                    thread_id="omt-side-direct",
                    root_id="om-root-direct-mismatch",
                ),
                sent_result(
                    "om-question-direct-mismatch",
                    chat_id="oc-direct-mismatch",
                    thread_id="omt-wrong-side",
                    root_id="om-root-direct-mismatch",
                    parent_id="om-root-direct-mismatch",
                ),
            )
        )
        await self.app.handle_message(direct_query)
        direct_record = self.store.side_topic_for_source(
            app_id="cli_test",
            source_message_id=direct_query.id,
        )
        assert direct_record is not None
        self.assertEqual(direct_record.state, SideTopicState.FAILED)
        self.assertEqual(self.runtime.submit_side_calls, [])

        same_topic = FakeMessage(
            "/side",
            message_id="om-source-same",
            chat_id="oc-same",
            chat_type="group",
            thread_id="omt-existing",
        )
        self.binding_for(same_topic)
        self.queue_direct_topic(
            chat_id="oc-same",
            root_id="om-root-same",
            topic_id="omt-existing",
        )
        await self.app.handle_message(same_topic)
        same_record = self.store.side_topic_for_source(
            app_id="cli_test",
            source_message_id=same_topic.id,
        )
        assert same_record is not None
        self.assertEqual(same_record.state, SideTopicState.FAILED)

    async def test_side_routing_precedes_binding_and_uses_underlying_chat_mention_rule(
        self,
    ) -> None:
        group_source = FakeMessage(
            "/side",
            message_id="om-group-source",
            chat_id="oc-group",
            chat_type="group",
        )
        self.binding_for(group_source)
        self.queue_direct_topic(
            chat_id="oc-group",
            root_id="om-group-root",
            topic_id="omt-group-side",
        )
        await self.app.handle_message(group_source)
        group_record = self.store.side_topic_for_source(
            app_id="cli_test",
            source_message_id=group_source.id,
        )
        assert group_record is not None

        await self.app.handle_message(
            FakeMessage(
                "ignored",
                message_id="om-unmentioned",
                chat_id="oc-group",
                chat_type="group",
                thread_id="omt-group-side",
                mentioned_bot=False,
            )
        )
        self.assertEqual(self.runtime.submit_side_calls, [])
        await self.app.handle_message(
            FakeMessage(
                "accepted",
                message_id="om-mentioned",
                chat_id="oc-group",
                chat_type="group",
                thread_id="omt-group-side",
                mentioned_bot=True,
            )
        )
        self.assertEqual(len(self.runtime.submit_side_calls), 1)

        direct_source = FakeMessage(
            "/side",
            message_id="om-direct-source",
            chat_id="oc-direct",
            chat_type="p2p",
            mentioned_bot=False,
        )
        self.binding_for(direct_source)
        self.queue_direct_topic(
            chat_id="oc-direct",
            root_id="om-direct-root",
            topic_id="omt-direct-side",
        )
        await self.app.handle_message(direct_source)
        direct_record = self.store.side_topic_for_source(
            app_id="cli_test",
            source_message_id="om-direct-source",
        )
        assert direct_record is not None
        self.runtime.side_submission = SideSubmission(
            SubmitDisposition.STEERED,
            direct_record.id,
            "native-side-running",
            "side-turn-running",
        )
        await self.app.handle_message(
            FakeMessage(
                "accepted without mention",
                message_id="om-direct-prompt",
                sender_id="ou_side_bob",
                display_name="Side Bob",
                chat_id="oc-direct",
                chat_type="p2p",
                thread_id="omt-direct-side",
                mentioned_bot=False,
            )
        )
        self.assertEqual(len(self.runtime.submit_side_calls), 2)
        request_text, current_context = plain_prompt_projection(
            self.runtime.submit_side_calls[-1]["input"]
        )
        self.assertEqual(request_text, "accepted without mention")
        self.assertEqual(current_context["message_id"], "om-direct-prompt")
        self.assertEqual(
            current_context["sender"]["open_id"],
            "ou_side_bob",
        )
        self.assertEqual(
            self.runtime.submit_side_calls[-1]["owner_id"],
            "ou_side_bob",
        )
        self.assertEqual(
            self.runtime.submit_side_calls[-1]["origin"].id,
            "om-direct-prompt",
        )
        self.assertIn(("om-direct-prompt", "OnIt"), self.channel.reactions)

        ordinary_topic = FakeMessage(
            "ordinary p2p topic",
            message_id="om-ordinary",
            chat_id="oc-ordinary",
            chat_type="p2p",
            thread_id="omt-ordinary",
            mentioned_bot=False,
        )
        binding = self.binding_for(ordinary_topic)
        self.runtime.submission = Submission(
            SubmitDisposition.STARTED,
            binding.id,
            binding.native_thread_id or "",
            "turn-ordinary",
            lambda: None,
        )
        await self.app.handle_message(ordinary_topic)
        request_text, current_context = plain_prompt_projection(
            self.runtime.submit_calls[-1]["input"]
        )
        self.assertEqual(request_text, "ordinary p2p topic")
        self.assertEqual(current_context["message_id"], "om-ordinary")

    async def test_terminal_and_root_fallback_tombstones_never_become_bindings(self) -> None:
        parent = FakeMessage(
            "/side",
            message_id="om-parent",
            chat_id="oc-chat",
            chat_type="group",
        )
        binding = self.binding_for(parent)
        closed = self.store.create_side_topic(
            app_id="cli_test",
            chat_id="oc-chat",
            source_message_id="om-closed-source",
            parent_binding_id=binding.id,
            creator_id="ou_owner",
            requires_mention=True,
        )
        self.store.set_side_topic_root(closed.id, "om-closed-root")
        self.store.open_side_topic(closed.id, "omt-closed")
        self.store.transition_side_topic(closed.id, SideTopicState.CLOSED)
        closed_scope = FeishuScope(
            "cli_test", "oc-chat", ScopeKind.TOPIC, "omt-closed"
        )
        self.store.create_binding(
            scope=closed_scope,
            project_alias="test",
            creator_id="ou_owner",
        )

        await self.app.handle_message(
            FakeMessage(
                "must not run",
                message_id="om-closed-message",
                chat_id="oc-chat",
                chat_type="group",
                thread_id="omt-closed",
                mentioned_bot=True,
            )
        )
        self.assertEqual(self.runtime.submit_calls, [])
        self.assertTrue(
            any("不会转成普通会话" in str(content) for _mid, content in self.channel.replies)
        )

        creating = self.store.create_side_topic(
            app_id="cli_test",
            chat_id="oc-chat",
            source_message_id="om-creating-source",
            parent_binding_id=binding.id,
            creator_id="ou_owner",
            requires_mention=True,
        )
        self.store.set_side_topic_root(creating.id, "om-creating-root")
        await self.app.handle_message(
            FakeMessage(
                "promotion response was lost",
                message_id="om-unknown-topic-message",
                chat_id="oc-chat",
                chat_type="group",
                thread_id="omt-unknown",
                mentioned_bot=True,
                raw={"root_id": "om-creating-root"},
            )
        )
        self.assertTrue(
            any("仍在创建" in str(content) for _mid, content in self.channel.replies)
        )

    async def test_open_route_without_runtime_session_is_expired_fail_closed(self) -> None:
        source = FakeMessage(
            "/side",
            message_id="om-parent",
            chat_id="oc-chat",
            chat_type="p2p",
            mentioned_bot=False,
        )
        binding = self.binding_for(source)
        record = self.store.create_side_topic(
            app_id="cli_test",
            chat_id="oc-chat",
            source_message_id="om-orphan-source",
            parent_binding_id=binding.id,
            creator_id="ou_owner",
            requires_mention=False,
        )
        self.store.set_side_topic_root(record.id, "om-orphan-root")
        self.store.open_side_topic(record.id, "omt-orphan")

        await self.app.handle_message(
            FakeMessage(
                "must expire",
                message_id="om-orphan-message",
                chat_id="oc-chat",
                chat_type="p2p",
                thread_id="omt-orphan",
                mentioned_bot=False,
            )
        )

        self.assertEqual(
            self.store.get_side_topic(record.id).state,
            SideTopicState.EXPIRED,
        )
        self.assertEqual(self.runtime.submit_calls, [])
        self.assertEqual(self.runtime.submit_side_calls, [])

    async def test_side_controls_are_scoped_and_close_button_validates_root(self) -> None:
        source = FakeMessage(
            "/side",
            message_id="om-source",
            chat_id="oc-direct",
            chat_type="p2p",
            mentioned_bot=False,
        )
        self.binding_for(source)
        self.queue_direct_topic(
            chat_id="oc-direct",
            root_id="om-root",
            topic_id="omt-side",
        )
        await self.app.handle_message(source)
        record = self.store.side_topic_for_source(
            app_id="cli_test",
            source_message_id=source.id,
        )
        assert record is not None

        with patch(
            "netizen.channel_app.git_branch_status",
            return_value="HEAD (no branch)",
        ) as read_branch:
            await self.app.handle_message(
                FakeMessage(
                    "/status",
                    message_id="om-status",
                    chat_id="oc-direct",
                    chat_type="p2p",
                    thread_id="omt-side",
                    mentioned_bot=False,
                )
            )
        await self.app.handle_message(
            FakeMessage(
                "/new",
                message_id="om-new-forbidden",
                chat_id="oc-direct",
                chat_type="p2p",
                thread_id="omt-side",
                mentioned_bot=False,
            )
        )
        self.assertTrue(any("当前 Side" in str(content) for _mid, content in self.channel.replies))
        self.assertTrue(
            any(
                "Git Branch：HEAD (no branch)" in str(content)
                for _mid, content in self.channel.replies
            )
        )
        read_branch.assert_awaited_once_with(self.project.resolve())
        self.assertTrue(any("Side 中不可用" in str(content) for _mid, content in self.channel.replies))

        open_card = self.channel.updates[-1][1]
        button = _elements(open_card, "button")[0]
        value = button["behaviors"][0]["value"]
        tampered = {**value, "topic_id": "omt-other"}
        await self.app.handle_card_action(
            SimpleNamespace(
                message_id="om-root",
                chat_id="oc-direct",
                operator=SimpleNamespace(open_id="ou_other"),
                action=SimpleNamespace(tag="button", value=tampered, form_value=None),
            )
        )
        self.assertEqual(self.runtime.close_side_calls, [])
        self.assertIn("side.close", str(self.channel.updates[-1][1]))

        other = self.store.create_side_topic(
            app_id="cli_test",
            chat_id="oc-direct",
            source_message_id="om-other-side-source",
            parent_binding_id=record.parent_binding_id,
            creator_id="ou_owner",
            requires_mention=False,
        )
        self.store.set_side_topic_root(other.id, "om-other-root")
        self.store.open_side_topic(other.id, "omt-other-side")
        tampered_side = {**value, "side_id": f"side:v1:{other.id}"}
        await self.app.handle_card_action(
            SimpleNamespace(
                message_id="om-root",
                chat_id="oc-direct",
                operator=SimpleNamespace(open_id="ou_other"),
                action=SimpleNamespace(
                    tag="button",
                    value=tampered_side,
                    form_value=None,
                ),
            )
        )
        self.assertEqual(self.runtime.close_side_calls, [])
        self.assertEqual(self.channel.updates[-1][0], "om-root")
        self.assertIn("side.close", str(self.channel.updates[-1][1]))

        await self.app.handle_card_action(
            SimpleNamespace(
                message_id="om-root",
                chat_id="oc-direct",
                operator=SimpleNamespace(open_id="ou_other"),
                action=SimpleNamespace(tag="button", value=value, form_value=None),
            )
        )
        self.assertEqual(
            self.runtime.close_side_calls,
            [(record.id, SideTopicState.CLOSED)],
        )
        self.assertEqual(
            self.store.get_side_topic(record.id).state,
            SideTopicState.CLOSED,
        )

    async def test_side_close_button_expires_missing_runtime_session(self) -> None:
        source = FakeMessage(
            "/side",
            message_id="om-source",
            chat_id="oc-direct",
            chat_type="p2p",
            mentioned_bot=False,
        )
        self.binding_for(source)
        self.queue_direct_topic(
            chat_id="oc-direct",
            root_id="om-root",
            topic_id="omt-side",
        )
        await self.app.handle_message(source)
        record = self.store.side_topic_for_source(
            app_id="cli_test",
            source_message_id=source.id,
        )
        assert record is not None
        card = self.channel.updates[-1][1]
        value = _elements(card, "button")[0]["behaviors"][0]["value"]
        self.runtime.side_snapshots.pop(record.id)
        self.runtime.side_close_error = SideSessionNotFound(record.id)

        await self.app.handle_card_action(
            SimpleNamespace(
                message_id="om-root",
                chat_id="oc-direct",
                operator=SimpleNamespace(open_id="ou_other"),
                action=SimpleNamespace(tag="button", value=value, form_value=None),
            )
        )

        self.assertEqual(
            self.store.get_side_topic(record.id).state,
            SideTopicState.EXPIRED,
        )
        self.assertTrue(
            any("已没有对应 Side Session" in str(card) for _mid, card in self.channel.updates)
        )

    async def test_creating_side_exposes_retriable_close_in_its_known_topic(self) -> None:
        source = FakeMessage(
            "/side",
            message_id="om-source",
            chat_id="oc-direct",
            chat_type="p2p",
            mentioned_bot=False,
        )
        self.binding_for(source)
        self.queue_direct_topic(
            chat_id="oc-direct",
            root_id="om-root",
            topic_id="omt-side",
        )
        self.runtime.side_close_error = SideCloseFailed("cleanup lost")
        with patch.object(
            self.runtime,
            "attach_side_topic",
            side_effect=RuntimeError("attach failed"),
        ):
            await self.app.handle_message(source)

        record = self.store.side_topic_for_source(
            app_id="cli_test",
            source_message_id=source.id,
        )
        assert record is not None
        self.assertEqual(record.state, SideTopicState.CREATING)
        self.assertEqual(record.topic_id, "omt-side")
        self.assertIn("side.close", str(self.channel.updates[-1][1]))

        self.runtime.side_close_error = None
        await self.app.handle_message(
            FakeMessage(
                "/side close",
                message_id="om-close",
                chat_id="oc-direct",
                chat_type="p2p",
                thread_id="omt-side",
                mentioned_bot=False,
                raw={"root_id": "om-root"},
            )
        )
        self.assertEqual(
            self.store.get_side_topic(record.id).state,
            SideTopicState.FAILED,
        )

    async def test_project_resolution_and_handler_cancellation_compensate(self) -> None:
        missing = FakeMessage(
            "/side",
            message_id="om-missing-project",
            chat_id="oc-missing-project",
            chat_type="p2p",
            mentioned_bot=False,
        )
        self.binding_for(missing)
        with patch.object(
            self.projects,
            "resolve_for_binding",
            side_effect=RuntimeError("project disappeared"),
        ):
            await self.app.handle_message(missing)
        missing_record = self.store.side_topic_for_source(
            app_id="cli_test",
            source_message_id=missing.id,
        )
        assert missing_record is not None
        self.assertEqual(missing_record.state, SideTopicState.FAILED)

        cancelled = FakeMessage(
            "/side",
            message_id="om-cancelled",
            chat_id="oc-cancelled",
            chat_type="p2p",
            mentioned_bot=False,
        )
        self.binding_for(cancelled)
        send_entered = asyncio.Event()
        never = asyncio.Event()

        async def blocked_send(*_args, **_kwargs):
            send_entered.set()
            await never.wait()

        with patch.object(self.channel, "send", new=blocked_send):
            handling = asyncio.create_task(self.app.handle_message(cancelled))
            await send_entered.wait()
            handling.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await handling
        cancelled_record = self.store.side_topic_for_source(
            app_id="cli_test",
            source_message_id=cancelled.id,
        )
        assert cancelled_record is not None
        self.assertEqual(cancelled_record.state, SideTopicState.FAILED)

    async def test_side_stop_keeps_topic_open_then_side_close_ends_it(self) -> None:
        source = FakeMessage(
            "/side",
            message_id="om-source",
            chat_id="oc-direct",
            chat_type="p2p",
            mentioned_bot=False,
        )
        self.binding_for(source)
        self.queue_direct_topic(
            chat_id="oc-direct",
            root_id="om-root",
            topic_id="omt-side",
        )
        await self.app.handle_message(source)
        record = self.store.side_topic_for_source(
            app_id="cli_test",
            source_message_id=source.id,
        )
        assert record is not None
        before = self.runtime.side_snapshot(record.id)
        self.runtime.side_snapshots[record.id] = SideSessionSnapshot(
            side_id=before.side_id,
            parent_binding_id=before.parent_binding_id,
            parent_thread_id=before.parent_thread_id,
            thread_id=before.thread_id,
            project_alias=before.project_alias,
            cwd=before.cwd,
            creator_id=before.creator_id,
            state=before.state,
            topic_id=before.topic_id,
            root_message_id=before.root_message_id,
            turn_id="side-turn-running",
            turn_state=ActiveState.RUNNING,
            last_activity=before.last_activity,
        )

        await self.app.handle_message(
            FakeMessage(
                "/stop",
                message_id="om-stop",
                chat_id="oc-direct",
                chat_type="p2p",
                thread_id="omt-side",
                mentioned_bot=False,
            )
        )
        self.assertEqual(self.runtime.stop_side_calls, [record.id])
        self.assertEqual(self.store.get_side_topic(record.id).state, SideTopicState.OPEN)

        await self.app.handle_message(
            FakeMessage(
                "/side close",
                message_id="om-close",
                chat_id="oc-direct",
                chat_type="p2p",
                thread_id="omt-side",
                mentioned_bot=False,
            )
        )
        self.assertEqual(
            self.runtime.close_side_calls,
            [(record.id, SideTopicState.CLOSED)],
        )
        self.assertEqual(
            self.store.get_side_topic(record.id).state,
            SideTopicState.CLOSED,
        )

    async def test_lark_230071_fails_explicitly_and_compensates(self) -> None:
        source = FakeMessage(
            "/side",
            message_id="om-source",
            chat_id="oc-direct",
            chat_type="p2p",
            mentioned_bot=False,
        )
        self.binding_for(source)
        self.channel.send_results.append(
            sent_result(
                "om-root",
                chat_id="oc-direct",
                success=False,
                code=230071,
            )
        )

        await self.app.handle_message(source)

        record = self.store.side_topic_for_source(
            app_id="cli_test",
            source_message_id=source.id,
        )
        assert record is not None
        self.assertEqual(record.state, SideTopicState.FAILED)
        self.assertEqual(
            self.runtime.close_side_calls,
            [(record.id, SideTopicState.FAILED)],
        )
        self.assertTrue(any("230071" in str(content) for _mid, content in self.channel.replies))

    def test_side_send_result_validator_rejects_every_identity_boundary(self) -> None:
        valid = sent_result(
            "om-valid",
            chat_id="oc-chat",
            thread_id="omt-valid",
            root_id="om-valid",
        )
        parsed = channel_app._validated_sent_message(
            valid,
            expected_chat_id="oc-chat",
        )
        self.assertEqual(parsed.message_id, "om-valid")

        missing_code = sent_result("om-missing-code", chat_id="oc-chat")
        missing_code.raw.pop("code")
        nonzero_code = sent_result("om-code", chat_id="oc-chat", code=1)
        chunked = sent_result("om-chunked", chat_id="oc-chat")
        chunked.chunk_ids = ("om-chunked", "om-other")
        mismatch = sent_result("om-result", chat_id="oc-chat")
        mismatch.raw["data"]["message_id"] = "om-data"
        wrong_chat = sent_result("om-wrong-chat", chat_id="oc-other")
        missing_data = sent_result("om-missing-data", chat_id="oc-chat")
        missing_data.raw.pop("data")
        unsuccessful = sent_result(
            "om-unsuccessful",
            chat_id="oc-chat",
            success=False,
            code=2,
        )

        for label, result in (
            ("missing-code", missing_code),
            ("nonzero-code", nonzero_code),
            ("chunked", chunked),
            ("mismatched-message", mismatch),
            ("wrong-chat", wrong_chat),
            ("missing-data", missing_data),
            ("unsuccessful", unsuccessful),
        ):
            with self.subTest(label=label), self.assertRaises(
                SideTopicCreateFailed
            ):
                channel_app._validated_sent_message(
                    result,
                    expected_chat_id="oc-chat",
                )


def _elements(value: object, tag: str) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    if isinstance(value, dict):
        if value.get("tag") == tag:
            found.append(value)
        for child in value.values():
            found.extend(_elements(child, tag))
    elif isinstance(value, list):
        for child in value:
            found.extend(_elements(child, tag))
    return found


def _card_button_value(card: OutboundCard, label: str) -> dict[str, object]:
    values = _card_button_values(card, label)
    if values:
        return values[0]
    raise AssertionError(f"button not found: {label}")


def _card_button_values(card: OutboundCard, label: str) -> list[dict[str, object]]:
    values: list[dict[str, object]] = []
    for button in _elements(card.card, "button"):
        text = button.get("text")
        if isinstance(text, dict) and text.get("content") == label:
            behaviors = button.get("behaviors")
            if isinstance(behaviors, list) and len(behaviors) == 1:
                behavior = behaviors[0]
                if isinstance(behavior, dict):
                    value = behavior.get("value")
                    if isinstance(value, dict):
                        values.append(value)
    return values


def _project_mode_value(
    card: OutboundCard | dict[str, object],
    mode: str,
) -> str:
    labels = {"create": "创建空目录", "existing": "登记已有目录"}
    content = card.card if isinstance(card, OutboundCard) else card
    create_form = next(
        form
        for form in _elements(content, "form")
        if form.get("name") == "project_create_v1"
    )
    mode_field = next(
        element
        for element in create_form["elements"]
        if element.get("name") == "project_mode"
    )
    return next(
        option["value"]
        for option in mode_field["options"]
        if option["text"]["content"] == labels[mode]
    )
