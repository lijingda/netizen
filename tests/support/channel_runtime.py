"""Controllable runtime port used by Channel behavior tests."""

from __future__ import annotations

from pathlib import Path
from netizen.bindings import (
    BindingStore,
    BindingTaskFeedback,
    BindingTurnSettings,
    SideTopicState,
)
from netizen.codex_runtime import (
    ActiveTurnSnapshot,
    BindingRuntimeSnapshot,
    CompactSubmission,
    ContextWindowUsage,
    GoalActivitySnapshot,
    GoalSubmission,
    NativeThreadMetadata,
    ReleaseDisposition,
    SideLifecycleOutcome,
    SideSessionNotFound,
    SideSessionSnapshot,
    SideSessionState,
    SideSubmission,
    SideSubmissionAdmission,
    SideTurnActivitySnapshot,
    SubmissionAdmission,
    StopDisposition,
    Submission,
    SubmitDisposition,
    SteerRace,
    ThreadDeleteUnavailable,
    ThreadArchived,
    ThreadLifecycleError,
    ThreadSubscriptionSnapshot,
    TurnProgressSnapshot,
    TurnActivitySnapshot,
)
from netizen.domain import MentionContextMode, MessageContextAnchor
from netizen.model_settings import (
    EffortOption,
    ModelCatalog,
    ModelOption,
    ServiceTierOption,
    TurnModelSettings,
)
from netizen.sdk_gap_adapter import GoalSnapshot


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
        self.thread_metadata_options: list[dict] = []
        self.thread_summary_calls: list[str] = []
        self.thread_summary_values: dict[str, NativeThreadMetadata] = {}
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
        **options,
    ) -> dict[str, NativeThreadMetadata]:
        self.thread_metadata_options.append(options)
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

    async def thread_summary(self, thread_id: str) -> NativeThreadMetadata:
        self.thread_summary_calls.append(thread_id)
        return self.thread_summary_values[thread_id]

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
