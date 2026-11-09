"""Ordinary input admission, start/steer submission and Skill input preparation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openai_codex import (
    ExternalMessage,
    ImageInput,
    InvalidRequestError,
    SkillInput,
    TextInput,
)

from ..bindings import (
    BindingTaskFeedback,
    BindingTurnSettings,
    ProjectDisabled,
    ThreadBinding,
)
from ..domain import (
    ActiveState,
    MessageContextAnchor,
    MentionContextMode,
)
from ..model_settings import ModelCatalogError, TurnModelSettings
from .contracts import (
    ModelProviderRejected,
    NativeThread,
    NativeTurnHandle,
    RuntimeClosed,
    ThreadStopping,
    ThreadOccupied,
    ThreadRunningConfiguration,
    ThreadCompacting,
    SteerRace,
    TurnStartFailed,
    SkillReferenceError,
    ThreadLifecycleError,
    ScheduledInitialStartConflict,
    SubmitDisposition,
    ThreadSubscriptionState,
    Submission,
    SubmissionAdmission,
    ContextCursorCommit,
)

from .native_values import (
    _model_provider_rejection,
    _thread_creation_cwd,
    _accepted_execution_cwd,
)
from .state import (
    _ActiveTurn,
    _ActiveGoal,
    _ThreadSubscription,
)
from .compaction import CompactionRuntime


_REVISION_RACES = (
    "准备本条消息期间会话配置已变化，本条消息未执行，请重新发送。",
    "准备本条消息期间上下文边界已变化，本条消息未执行，请重新发送。",
    "准备本条消息期间任务反馈配置已变化，本条消息未执行，请重新发送。",
)


@dataclass(slots=True)
class _ScheduledInitialSubmission:
    run_id: str
    begun: bool = False


@dataclass(frozen=True)
class _PreparedSubmission:
    """Lock-free preparation that ``_submit`` re-validates under the Binding lock."""

    admission: SubmissionAdmission | None
    revisions: tuple[int, int, int]
    resolved_settings: TurnModelSettings | None
    native_input: Any
    execution_cwd: Path | None
    prepared_skill_thread: tuple[NativeThread, _ThreadSubscription, int] | None
    prepared_skill_thread_id: str | None


def _revisions(record: ThreadBinding | SubmissionAdmission) -> tuple[int, int, int]:
    return (record.settings_revision, record.context_revision, record.feedback_revision)


def _require_unchanged_revisions(
    expected: tuple[int, int, int], actual: tuple[int, int, int],
) -> None:
    """Abort prepared input when settings, context or feedback changed, in that order."""
    for before, after, message in zip(expected, actual, _REVISION_RACES):
        if before != after:
            raise SteerRace(message)


class SubmissionRuntime(CompactionRuntime):
    """Ordinary input admission, start/steer submission and Skill input preparation."""

    def _init_submission_state(self) -> None:
        self._shutdown_requested = False

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
    ) -> ThreadBinding:
        """Atomically update Turn settings, context, and task feedback."""

        if not self._accepting:
            raise RuntimeClosed("服务正在停止，暂不能修改会话配置。")
        if binding_id in self._compacting:
            raise ThreadCompacting("当前会话正在压缩上下文，完成前不能修改会话配置。")
        if binding_id in self._goals:
            raise self._goal_slot_error(self._goals[binding_id])
        active = self._active.get(binding_id)
        if active is not None:
            raise ThreadRunningConfiguration(
                "当前 Turn 正在执行，不能修改会话配置；请等待完成或先发送 /stop。"
            )

        async with self._lock(binding_id):
            if not self._accepting:
                raise RuntimeClosed("服务正在停止，暂不能修改会话配置。")
            self._guard_no_lifecycle_locked(binding_id)
            if binding_id in self._compacting:
                raise ThreadCompacting(
                    "当前会话正在压缩上下文，完成前不能修改会话配置。"
                )
            binding = self._bindings.get(binding_id)
            await self._guard_no_goal_locked(binding)
            binding = self._bindings.get(binding_id)
            active = self._active.get(binding_id)
            if active is not None:
                if active.state is ActiveState.STOPPING:
                    raise ThreadStopping(
                        "当前 Turn 正在停止，不能修改会话配置；"
                        "若 /stop 曾提示清理失败，请再次发送 /stop 重试。"
                    )
                raise ThreadRunningConfiguration(
                    "当前 Turn 正在执行，不能修改会话配置；"
                    "请等待完成或先发送 /stop。"
                )
            updated = self._bindings.set_configuration(
                binding_id=binding_id,
                expected_settings_revision=expected_settings_revision,
                expected_context_revision=expected_context_revision,
                expected_feedback_revision=expected_feedback_revision,
                settings=settings,
                task_feedback=task_feedback,
                message_context_mode=message_context_mode,
                context_anchor=context_anchor,
            )
            if (
                updated.settings_revision != binding.settings_revision
                or updated.context_revision != binding.context_revision
                or updated.feedback_revision != binding.feedback_revision
            ):
                self._advance_admission_revision(binding_id)
            return updated

    async def capture_submission_admission(
        self,
        binding_id: str,
    ) -> SubmissionAdmission:
        """Snapshot the exact start/steer target without reserving it.

        Callers may perform bounded network I/O after this method returns, then
        pass the token back to :meth:`submit`. Any intervening prompt, stop, or
        completion invalidates the token instead of changing its disposition.
        """

        if not self._accepting:
            raise RuntimeClosed("服务正在停止，暂不接受新任务。")
        self._guard_no_scheduled_initial(binding_id)
        recheck_revision = await self._recheck_before_submission(binding_id)
        async with self._lock(binding_id):
            if not self._accepting:
                raise RuntimeClosed("服务正在停止，暂不接受新任务。")
            self._guard_no_lifecycle_locked(binding_id)
            self._guard_no_scheduled_initial(binding_id)
            if binding_id in self._compacting:
                raise ThreadCompacting(
                    "当前会话正在压缩上下文，完成前暂不接受新消息。"
                )
            binding = self._bindings.get(binding_id)
            if not binding.active:
                raise SteerRace(
                    "准备本条消息期间 active 会话已切换，本条消息未执行，请重新发送。"
                )
            goal = await self._prompt_goal_locked(binding)
            binding = self._bindings.get(binding_id)
            if not binding.active:
                raise SteerRace(
                    "准备本条消息期间 active 会话已切换，本条消息未执行，请重新发送。"
                )
            active = self._active.get(binding_id)
            if active is not None and active.state is ActiveState.STOPPING:
                raise ThreadStopping(
                    "当前任务正在停止，暂不接受新消息；"
                    "若 /stop 曾提示清理失败，请再次发送 /stop 重试。"
                )
            if (
                active is not None
                and active.state is ActiveState.OBSERVATION_UNAVAILABLE
            ):
                raise self._unavailable_submission_error(active)
            if (
                recheck_revision is not None
                and recheck_revision != self._admission_revision(binding_id)
            ):
                raise SteerRace("检查期间任务状态已变化，本条消息未执行，请重新发送。")
            return SubmissionAdmission(
                binding_id=binding_id,
                revision=self._admission_revision(binding_id),
                thread_id=(
                    goal.thread_id if goal is not None
                    else active.handle.thread_id if active is not None else None
                ),
                turn_id=(
                    self._goal_steer_turn_id(goal) if goal is not None
                    else active.handle.id if active is not None else None
                ),
                settings_revision=binding.settings_revision,
                context_revision=binding.context_revision,
                feedback_revision=binding.feedback_revision,
            )

    async def submit(
        self,
        *,
        binding: ThreadBinding,
        cwd: Path | None = None,
        input: Any,
        owner_id: str,
        origin: object,
        admission: SubmissionAdmission | None = None,
        context_commit: ContextCursorCommit | None = None,
        skill_names: tuple[str, ...] = (),
    ) -> Submission:
        """Submit input; cwd is only a lazy Thread's creation-directory intent."""
        active = self._active.get(binding.id)
        if (
            admission is None
            and active is not None
            and active.state is ActiveState.OBSERVATION_UNAVAILABLE
        ):
            admission = await self.capture_submission_admission(binding.id)
        return await self._submit(
            binding=binding, creation_cwd=cwd, input=input, owner_id=owner_id,
            origin=origin, admission=admission, context_commit=context_commit,
            skill_names=skill_names,
        )

    async def submit_initial(
        self,
        *,
        run_id: str,
        binding: ThreadBinding,
        cwd: Path,
        input: Any,
        owner_id: str,
        origin: object,
        skill_names: tuple[str, ...] = (),
    ) -> Submission:
        """Start a claimed occurrence on its exact fresh ordinary Binding.

        The persisted reservation blocks ordinary input during preparation.
        Reusing the ordinary start branch preserves native admission, receipt,
        completion, and skill behavior without allowing resume or steer.
        """
        if not run_id:
            raise ValueError("scheduled Run ID must not be empty")
        initial = _ScheduledInitialSubmission(run_id)
        try:
            return await self._submit(
                binding=binding, creation_cwd=cwd, input=input, owner_id=owner_id,
                origin=origin, skill_names=skill_names, initial=initial,
            )
        except BaseException as error:
            run = self._bindings.schedules.get_run(run_id)
            if run.binding_id == binding.id and run.barrier != "released":
                if run.phase == "binding_ready":
                    # No native mutation has been attempted by this claim.
                    self._bindings.schedules.release(
                        run_id, error_code="initial_start_rejected",
                    )
                elif initial.begun and run.phase == "starting_turn":
                    if isinstance(error, ModelProviderRejected):
                        self._bindings.schedules.release(
                            run_id, error_code="input_rejected",
                        )
                        raise
                    # A lost start response must never be retried. The ordinary
                    # mutation path independently closes global admission.
                    self._bindings.schedules.set_run(
                        run_id, barrier="unknown", error_code="initial_start_unknown",
                    )
            raise

    async def submit_external(
        self,
        *,
        binding: ThreadBinding,
        cwd: Path | None = None,
        message: ExternalMessage,
        owner_id: str,
        origin: object,
        model: str | None = None,
        title: str | None = None,
    ) -> Submission:
        """Deliver verified tool-origin content to an exact current Thread.

        Only this entry uses native start/join semantics. Ordinary Feishu,
        question-card and scheduled inputs retain their exact-Turn admission.
        cwd is only a lazy Thread's creation-directory intent.
        """

        if not isinstance(message, ExternalMessage):
            raise TypeError("external submission requires an ExternalMessage")
        if title is not None:
            title = " ".join(title.split())
            if not title or len(title) > 120:
                raise ValueError("会话名称必须为 1 到 120 个字符。")
        native_model = None
        if model is not None:
            catalog = await self.model_catalog()
            selected = next(
                (item for item in catalog.models if model in {item.id, item.model}),
                None,
            )
            if selected is None:
                raise ModelCatalogError("所选 Model 已不可用，跨会话消息未发送。")
            native_model = selected.model
        return await self._submit(
            binding=binding, creation_cwd=cwd, input=message, owner_id=owner_id,
            origin=origin, external=True, external_model=native_model,
            preferred_title=title,
        )

    async def _submit(
        self,
        *,
        binding: ThreadBinding,
        creation_cwd: Path | None,
        input: Any,
        owner_id: str,
        origin: object,
        admission: SubmissionAdmission | None = None,
        context_commit: ContextCursorCommit | None = None,
        skill_names: tuple[str, ...] = (),
        initial: _ScheduledInitialSubmission | None = None,
        external: bool = False,
        external_model: str | None = None,
        preferred_title: str | None = None,
    ) -> Submission:
        if isinstance(input, ExternalMessage) != external:
            raise TypeError("ExternalMessage must use the verified external input entry")
        initial_run_id = initial.run_id if initial is not None else None
        prepared = await self._prepare_submission(
            binding=binding, creation_cwd=creation_cwd, input=input,
            admission=admission, context_commit=context_commit,
            skill_names=skill_names, initial=initial, external=external,
            external_model=external_model,
        )
        admission = prepared.admission
        resolved_settings = prepared.resolved_settings
        native_input = prepared.native_input
        execution_cwd = prepared.execution_cwd
        prepared_skill_thread = prepared.prepared_skill_thread
        prepared_skill_thread_id = prepared.prepared_skill_thread_id
        if not self._accepting:
            raise RuntimeClosed("服务正在停止，暂不接受新任务。")
        if binding.id in self._compacting:
            raise ThreadCompacting(
                "当前会话正在压缩上下文，完成前暂不接受新消息。"
            )
        active = self._active.get(binding.id)
        if active is not None and active.state is ActiveState.STOPPING:
            raise ThreadStopping(
                "当前任务正在停止，暂不接受新消息；"
                "若 /stop 曾提示清理失败，请再次发送 /stop 重试。"
            )
        if (
            active is not None
            and active.state is ActiveState.OBSERVATION_UNAVAILABLE
        ):
            raise self._unavailable_submission_error(active)

        async with self._lock(binding.id):
            if not self._accepting:
                raise RuntimeClosed("服务正在停止，暂不接受新任务。")
            self._guard_no_lifecycle_locked(binding.id)
            self._bindings.worktrees.require_binding_not_deleting(binding.id)
            if initial_run_id is None:
                self._guard_no_scheduled_initial(binding.id)
            if binding.id in self._compacting:
                raise ThreadCompacting(
                    "当前会话正在压缩上下文，完成前暂不接受新消息。"
                )
            binding = self._bindings.get(binding.id)
            if not binding.active:
                raise SteerRace(
                    "准备本条消息期间 active 会话已切换，本条消息未执行，请重新发送。"
                )
            if external:
                self._bindings.require_project_not_deleting(binding.project_alias)
                if not self._bindings.get_project(binding.project_alias).enabled:
                    raise ProjectDisabled(binding.project_alias)
            _require_unchanged_revisions(prepared.revisions, _revisions(binding))
            if initial is not None or external:
                await self._guard_no_goal_locked(binding)
                goal = None
            else:
                goal = await self._prompt_goal_locked(binding)
            binding = self._bindings.get(binding.id)
            if not binding.active:
                raise SteerRace(
                    "准备本条消息期间 active 会话已切换，本条消息未执行，请重新发送。"
                )
            active = self._active.get(binding.id)
            if active is None and goal is None and binding.native_thread_id is None:
                execution_cwd = _thread_creation_cwd(binding, creation_cwd)
            if initial_run_id is not None:
                if active is not None or binding.native_thread_id is not None:
                    raise ScheduledInitialStartConflict(
                        "定时任务的首次执行会话已改变，本次未启动。"
                    )
                self._bindings.begin_scheduled_initial(initial_run_id, binding.id)
                assert initial is not None
                initial.begun = True
            if active is not None:
                if active.state is ActiveState.STOPPING:
                    raise ThreadStopping(
                        "当前任务正在停止，暂不接受新消息；"
                        "若 /stop 曾提示清理失败，请再次发送 /stop 重试。"
                    )
                if active.state is ActiveState.OBSERVATION_UNAVAILABLE:
                    raise self._unavailable_submission_error(active)
            self._redeem_submission_admission(
                binding_id=binding.id,
                active=active,
                admission=admission,
                goal=goal,
            )
            if skill_names and binding.native_thread_id != prepared_skill_thread_id:
                # A failed concurrent operation may materialize a lazy Thread
                # without consuming an admission revision. Its cwd no longer
                # belongs to the lazy directory used for Skill discovery.
                raise SteerRace(
                    "准备 Skill 输入期间 Codex 会话身份已变化，本条消息未执行，请重新发送。"
                )
            if prepared_skill_thread is not None:
                prepared_thread, prepared_subscription, generation = prepared_skill_thread
                if (
                    self._subscriptions.get(binding.id) is not prepared_subscription
                    or prepared_subscription.generation != generation
                    or prepared_subscription.thread is not prepared_thread
                    or binding.native_thread_id != prepared_thread.id
                    or prepared_subscription.state not in {
                        ThreadSubscriptionState.SUBSCRIBED,
                        ThreadSubscriptionState.RELEASE_PENDING,
                    }
                ):
                    raise SteerRace(
                        "准备 Skill 输入期间 Thread 订阅已变化，本条消息未执行，请重新发送。"
                    )
            if goal is not None:
                execution_cwd = _accepted_execution_cwd(goal.cwd)
            elif active is not None:
                execution_cwd = _accepted_execution_cwd(active.cwd)
            self._advance_admission_revision(binding.id)
            if goal is not None:
                assert admission is not None and admission.turn_id is not None
                return await self._steer_goal_locked(
                    binding=binding,
                    active=goal,
                    expected_turn_id=admission.turn_id,
                    input=native_input,
                    context_commit=context_commit,
                )
            if active is not None:
                self._notify_input_received(
                    active.handle.thread_id, active.handle.id, active.handle.id,
                )
            if active is not None and not external:
                return await self._steer_active_turn_locked(
                    binding=binding, active=active, native_input=native_input,
                    context_commit=context_commit,
                )

            # A caller may hold the lazy Binding snapshot returned by /new even
            # after an earlier Turn persisted its native Thread ID.
            binding = self._bindings.get(binding.id)
            thread = await self._open_submission_thread_locked(
                binding=binding,
                active=active,
                prepared_skill_thread=prepared_skill_thread,
                execution_cwd=execution_cwd,
                resolved_settings=resolved_settings,
                external_model=external_model,
            )

            if binding.native_thread_id is None:
                # Reserve the App Server-issued ID before sending the first
                # prompt.  This is still lazy (/new never reaches here), but
                # prevents a conflicting Binding from receiving any Turn.
                try:
                    self._bindings.assign_native_thread_id(binding.id, thread.id)
                except Exception:
                    self.close_admission()
                    raise

            subscription = self._mark_thread_subscribed_locked(
                self._bindings.get(binding.id),
                thread,
            )

            if (
                active is None
                and binding.native_thread_id is not None
                and prepared_skill_thread is None
            ):
                try:
                    execution_cwd = await self._read_submission_cwd(binding.id, thread)
                except BaseException:
                    # Resume is known successful, but no input was sent. Retain
                    # its handle and use the normal idle-release policy.
                    self._schedule_subscription_release_locked(subscription)
                    raise

            if initial_run_id is not None or external:
                # A Project deletion may freeze creation while thread/start
                # is in flight. Its exact native identity is now retained, but
                # no first Turn may begin after that freeze.
                try:
                    self._bindings.require_project_not_deleting(binding.project_alias)
                    if not self._bindings.get_project(binding.project_alias).enabled:
                        raise ProjectDisabled(binding.project_alias)
                except Exception:
                    if initial_run_id is not None:
                        self._bindings.schedules.release(
                            initial_run_id, error_code="project_unavailable",
                        )
                    raise

            if external and not self._accepting:
                # start/resume and Goal inspection await native I/O. Retain an
                # already assigned Thread ID, but do not send a first/new task
                # after shutdown or an unknown mutation closed admission.
                raise RuntimeClosed("服务正在停止，跨会话消息未发送。")
            if external and not self._bindings.get(binding.id).active:
                raise SteerRace("目标会话已切到后台，跨会话消息未发送。")
            assert execution_cwd is not None
            task_feedback = self._capture_task_feedback(binding.task_feedback)
            try:
                handle = await self._start_native_turn_locked(
                    binding_id=binding.id,
                    thread=thread,
                    native_input=native_input,
                    resolved_settings=resolved_settings,
                    external_model=external_model,
                    external=external,
                )
            except ModelProviderRejected:
                self._schedule_subscription_release_locked(subscription)
                raise

            if active is not None and handle.id == active.handle.id:
                # The returned handle has its own SDK notification cursor.
                # Dropping it releases that unused cursor through SDK-owned
                # finalization. Keep the original consumer, owner and origin.
                self._refresh_turn_activity(active)
                active.plan_may_be_stale = True
                active.plan_stale_after_cursor = active.plan_cursor
                active.activity_revision += 1
                return Submission(
                    disposition=SubmitDisposition.JOINED,
                    binding_id=binding.id, thread_id=thread.id, turn_id=handle.id,
                    task_feedback=active.task_feedback,
                    feedback_revision=active.feedback_revision,
                )
            if active is not None:
                self._retiring_turns[(binding.id, active.handle.id)] = active

            receipt_attempted = asyncio.Event()
            active = _ActiveTurn(
                binding_id=binding.id,
                thread=thread,
                handle=handle,
                owner_id=owner_id,
                origin=origin,
                receipt_attempted=receipt_attempted,
                cwd=execution_cwd,
                task_feedback=task_feedback,
                feedback_revision=binding.feedback_revision,
                plan_available=self._turn_plan_observer is not None,
            )
            if initial_run_id is not None:
                try:
                    self._bindings.mark_scheduled_turn_started(
                        initial_run_id, binding.id, handle.id,
                    )
                except BaseException as error:
                    # Native accepted the Turn. Keep its sole observer alive
                    # even when its scheduling handoff cannot be committed.
                    self._track(active)
                    receipt_attempted.set()
                    self.close_admission()
                    raise TurnStartFailed(
                        "Codex 已接受本次定时任务，但首次 Turn 引用未能持久化；"
                        "服务已停止接收新任务，请重启后对账。"
                    ) from error
            self._track(active)
            if external and self._shutdown_requested:
                # turn/start was already in flight when shutdown took its
                # snapshot. Its newly accepted handle still needs the same
                # exact interrupt/cleanup ownership as snapshot members.
                self._track_late_shutdown_turn(active)
            try:
                self._commit_context_cursor_locked(
                    binding=binding,
                    commit=context_commit,
                )
            except BaseException:
                # The native Turn is already accepted and tracked. Let its
                # consumer proceed even though the caller receives the exact
                # persistence warning instead of a normal receipt callback.
                receipt_attempted.set()
                raise
            self._start_automatic_name(
                binding.id, thread, handle.id, resolved_settings,
                preferred_title=preferred_title,
                external_input=input if external else None,
            )
            return Submission(
                disposition=SubmitDisposition.STARTED,
                binding_id=binding.id,
                thread_id=thread.id,
                turn_id=handle.id,
                release_receipt_attempt=receipt_attempted.set,
                task_feedback=active.task_feedback,
                feedback_revision=active.feedback_revision,
            )

    async def _prepare_submission(
        self,
        *,
        binding: ThreadBinding,
        creation_cwd: Path | None,
        input: Any,
        admission: SubmissionAdmission | None,
        context_commit: ContextCursorCommit | None,
        skill_names: tuple[str, ...],
        initial: _ScheduledInitialSubmission | None,
        external: bool,
        external_model: str | None,
    ) -> _PreparedSubmission:
        """Validate and resolve one input before the Binding lock is taken.

        Everything here may race with other operations. ``_submit`` re-checks
        the captured revisions and native identity under the Binding lock and
        aborts instead of reinterpreting the prepared input.
        """
        initial_run_id = initial.run_id if initial is not None else None
        if initial_run_id is None:
            self._guard_no_scheduled_initial(binding.id)
        else:
            self._bindings.require_scheduled_initial(initial_run_id, binding.id)
        if admission is not None and admission.binding_id != binding.id:
            raise ValueError("submission admission belongs to another Binding")
        if not external and admission is None and initial is None and binding.id in self._goals:
            admission = await self.capture_submission_admission(binding.id)
        prepared_binding = self._bindings.get(binding.id)
        if not prepared_binding.active:
            raise SteerRace(
                "准备本条消息期间 active 会话已切换，本条消息未执行，请重新发送。"
            )
        revisions = _revisions(prepared_binding)
        configured_settings = prepared_binding.turn_settings
        if admission is not None:
            _require_unchanged_revisions(_revisions(admission), revisions)
        if external:
            # Tool-origin input is not a new human context window. Do not read
            # chat catch-up, consume its cursor or rebase it onto a user role.
            if admission is not None or context_commit is not None or skill_names or initial:
                raise ValueError("external input cannot use ordinary preparation")
        elif prepared_binding.message_context_mode is MentionContextMode.CATCH_UP:
            if initial_run_id is None:
                if admission is None or context_commit is None:
                    raise ValueError("catch-up submission requires admission and cursor commit")
                if (
                    context_commit.expected_context_revision
                    != admission.context_revision
                ):
                    raise ValueError("context cursor commit does not match admission")
            # A reserved scheduled first Turn has no current human message or
            # history window. Its new topic anchor was verified at Binding
            # creation and remains the boundary for ordinary follow-up input.
        elif context_commit is not None:
            raise ValueError("current-only submission cannot commit a context cursor")
        if (
            configured_settings is not None
            and admission is None
            and initial_run_id is None
            and not external
        ):
            admission = await self.capture_submission_admission(binding.id)
            prepared_binding = self._bindings.get(binding.id)
            revisions = _revisions(prepared_binding)
            _require_unchanged_revisions(_revisions(admission), revisions)
            configured_settings = prepared_binding.turn_settings

        resolved_settings = None
        settings_for_new_turn = configured_settings
        if external_model is not None:
            # Explicit model selection belongs to this native request. It must
            # not silently rewrite the target's Binding settings or force the
            # target's configured effort onto a different model.
            settings_for_new_turn = None
        if admission is not None and admission.thread_id is not None:
            # A running Turn can only be steered. Binding settings apply only
            # when Netizen starts a new Turn and are never injected into steer.
            settings_for_new_turn = None
        if settings_for_new_turn is not None:
            # models() reaches App Server, so keep it outside the Binding lock.
            # The admission and lock-time settings revision checks below make
            # any intervening state change abort instead of reinterpreting the
            # prepared input.
            resolved_settings = await self.resolve_model_settings(
                model_id=settings_for_new_turn.model_id,
                effort_id=settings_for_new_turn.effort_id,
                service_tier_id=settings_for_new_turn.service_tier_id,
            )
        native_input = input
        execution_cwd: Path | None = None
        prepared_skill_thread: tuple[NativeThread, _ThreadSubscription, int] | None = None
        prepared_skill_thread_id: str | None = None
        if skill_names:
            if admission is None and initial_run_id is None:
                admission = await self.capture_submission_admission(binding.id)
            if initial_run_id is None:
                assert admission is not None
                execution_cwd, prepared_skill_thread = await self._prepare_skill_cwd(
                    binding_id=binding.id, creation_cwd=creation_cwd, admission=admission,
                )
            else:
                execution_cwd = _thread_creation_cwd(prepared_binding, creation_cwd)
            prepared_skill_thread_id = self._bindings.get(binding.id).native_thread_id
            native_input = await self._compile_skill_input(
                cwd=execution_cwd,
                text=input,
                names=skill_names,
            )
        return _PreparedSubmission(
            admission=admission,
            revisions=revisions,
            resolved_settings=resolved_settings,
            native_input=native_input,
            execution_cwd=execution_cwd,
            prepared_skill_thread=prepared_skill_thread,
            prepared_skill_thread_id=prepared_skill_thread_id,
        )

    async def _steer_active_turn_locked(
        self,
        *,
        binding: ThreadBinding,
        active: _ActiveTurn,
        native_input: Any,
        context_commit: ContextCursorCommit | None,
    ) -> Submission:
        """Steer the exact running ordinary Turn; never queue or start another."""
        self._mark_thread_subscribed_locked(binding, active.thread)
        self._refresh_turn_activity(active)
        stale_after_cursor = active.plan_cursor
        await self._steer_turn(
            active.handle.steer(native_input),
            race_message="当前任务恰好已经结束，本条消息未执行，请重新发送。",
        )
        active.steer_count += 1
        last_update = active.plan_last_update_cursor
        if last_update is not None and last_update > stale_after_cursor:
            # A concurrent /status may already have observed a plan
            # emitted after steer began but before its RPC returned.
            active.plan_may_be_stale = False
            active.plan_stale_after_cursor = None
        else:
            active.plan_may_be_stale = True
            active.plan_stale_after_cursor = stale_after_cursor
        active.activity_revision += 1
        self._commit_context_cursor_locked(
            binding=binding,
            commit=context_commit,
        )
        return Submission(
            disposition=SubmitDisposition.STEERED,
            binding_id=binding.id,
            thread_id=active.handle.thread_id,
            turn_id=active.handle.id,
            task_feedback=active.task_feedback,
            feedback_revision=active.feedback_revision,
        )

    async def _open_submission_thread_locked(
        self,
        *,
        binding: ThreadBinding,
        active: _ActiveTurn | None,
        prepared_skill_thread: tuple[NativeThread, _ThreadSubscription, int] | None,
        execution_cwd: Path | None,
        resolved_settings: TurnModelSettings | None,
        external_model: str | None,
    ) -> NativeThread:
        """Reuse, start or resume the native Thread for a new Turn.

        Start/resume is side-effectful, so an unconfirmed result closes admission.
        """
        try:
            if active is not None:
                thread = active.thread
            elif prepared_skill_thread is not None:
                thread = prepared_skill_thread[0]
            elif binding.native_thread_id is None:
                # The public API has no "inherit Ask/Custom" sentinel;
                # omitting approval_mode deliberately selects its
                # auto_review default.
                assert execution_cwd is not None
                start_kwargs: dict[str, object] = {"cwd": str(execution_cwd)}
                if resolved_settings is not None:
                    # Initialize native model metadata with the first Turn's
                    # selected model, including its context window budget.
                    start_kwargs["model"] = resolved_settings.model
                if external_model is not None:
                    start_kwargs["model"] = external_model
                thread = await self._codex.thread_start(**start_kwargs)
            else:
                # Do not override cwd, sandbox, model, approval, config, or
                # env while resuming an existing native Thread.
                thread = await self._resume_thread_for_operation(
                    binding.native_thread_id
                )
                if thread.id != binding.native_thread_id:
                    raise RuntimeError(
                        "thread_resume returned a different native ID"
                    )
        except (ThreadOccupied, ThreadLifecycleError):
            raise
        except asyncio.CancelledError:
            self.close_admission()
            raise
        except Exception as error:
            # Starting/resuming is side-effectful and a lost response does
            # not prove that App Server left native state untouched.
            self.close_admission()
            raise TurnStartFailed(
                "Codex Thread 启动或恢复结果未确认；"
                "服务已停止接收新任务，请重启服务。"
            ) from error
        return thread

    async def _start_native_turn_locked(
        self,
        *,
        binding_id: str,
        thread: NativeThread,
        native_input: Any,
        resolved_settings: TurnModelSettings | None,
        external_model: str | None,
        external: bool,
    ) -> NativeTurnHandle:
        """Send turn/start once; an unconfirmed result closes service admission."""
        try:
            turn_kwargs: dict[str, object] = {}
            if resolved_settings is not None:
                turn_kwargs = {
                    "model": resolved_settings.model,
                    "effort": resolved_settings.effort,
                    "service_tier": resolved_settings.service_tier_id,
                }
            if external_model is not None:
                turn_kwargs["model"] = external_model
            handle = await thread.turn(native_input, **turn_kwargs)
        except asyncio.CancelledError:
            self._invalidate_context_window_usage(binding_id)
            self.close_admission()
            raise
        except Exception as error:
            rejection = _model_provider_rejection(error)
            if rejection is not None:
                raise rejection from error
            if external and isinstance(error, InvalidRequestError):
                # An explicit native rejection proves non-delivery. Never
                # retry it as steer/user input or close unrelated Bindings.
                raise
            # turn/start is side-effectful: a lost response cannot prove
            # that App Server did not start the Turn. Never admit another
            # Turn on this or any other Binding until the service restarts.
            self._invalidate_context_window_usage(binding_id)
            self.close_admission()
            raise TurnStartFailed(
                "Codex Turn 启动结果未确认；服务已停止接收新任务，请重启服务。"
            ) from error
        if handle.thread_id != thread.id:
            self._invalidate_context_window_usage(binding_id)
            self.close_admission()
            raise RuntimeError(
                "native Turn handle returned a different Thread ID; "
                "service admission is closed"
            )
        return handle

    async def _prepare_skill_cwd(
        self, *, binding_id: str, creation_cwd: Path | None, admission: SubmissionAdmission,
    ) -> tuple[Path, tuple[NativeThread, _ThreadSubscription, int] | None]:
        """Resolve an idle Thread before cwd-dependent, lock-free discovery.

        The caller keeps its original admission. A resumed handle is reusable
        only while the existing subscription generation remains unchanged.
        """
        async with self._lock(binding_id):
            if not self._accepting:
                raise RuntimeClosed("服务正在停止，暂不接受新任务。")
            self._guard_no_lifecycle_locked(binding_id)
            self._guard_no_scheduled_initial(binding_id)
            self._bindings.worktrees.require_binding_not_deleting(binding_id)
            if binding_id in self._compacting:
                raise ThreadCompacting("当前会话正在压缩上下文，完成前暂不接受新消息。")
            binding = self._bindings.get(binding_id)
            if not binding.active:
                raise SteerRace("准备本条消息期间 active 会话已切换，本条消息未执行，请重新发送。")
            active = self._active.get(binding_id)
            goal = self._goals.get(binding_id)
            self._redeem_submission_admission(
                binding_id=binding_id, active=active, admission=admission, goal=goal,
            )
            accepted = goal if goal is not None else active
            if accepted is not None:
                # Steer belongs to the already accepted physical Turn, not a
                # fresh resume or a caller's earlier read-only cwd snapshot.
                accepted_cwd = _accepted_execution_cwd(accepted.cwd)
                return accepted_cwd, None
            if binding.native_thread_id is None:
                # Lazy Skill validation must remain free of thread/start.
                return _thread_creation_cwd(binding, creation_cwd), None
            try:
                thread = await self._resume_thread_for_operation(binding.native_thread_id)
                if thread.id != binding.native_thread_id:
                    raise RuntimeError("thread_resume returned a different native ID")
            except (ThreadOccupied, ThreadLifecycleError):
                raise
            except asyncio.CancelledError:
                self.close_admission()
                raise
            except Exception as error:
                self.close_admission()
                raise TurnStartFailed(
                    "Codex Thread 启动或恢复结果未确认；"
                    "服务已停止接收新任务，请重启服务。"
                ) from error
            subscription = self._mark_thread_subscribed_locked(binding, thread)
            try:
                execution_cwd = await self._read_submission_cwd(binding_id, thread)
            finally:
                # Discovery may fail, be cancelled or outlive idle release.
                # Its normal subscription policy needs no preparation lease.
                self._schedule_subscription_release_locked(subscription)
            return execution_cwd, (thread, subscription, subscription.generation)

    async def _compile_skill_input(
        self,
        *,
        cwd: Path,
        text: Any,
        names: tuple[str, ...],
    ) -> list[TextInput | ImageInput | SkillInput]:
        if isinstance(text, str):
            native_input: list[TextInput | ImageInput | SkillInput] = [
                TextInput(text)
            ]
        elif (
            isinstance(text, list)
            and text
            and all(isinstance(item, (TextInput, ImageInput)) for item in text)
        ):
            native_input = list(text)
        else:
            raise SkillReferenceError(
                "带 $skill 的消息必须是文本或图片输入，本条消息未执行。"
            )
        if len(set(names)) != len(names):
            raise SkillReferenceError("同一条消息不能重复引用一个 Skill。")
        catalog = self._skill_catalog
        if catalog is None:
            raise SkillReferenceError("当前 Codex Skills 发现功能不可用。")
        snapshot = await catalog.list(cwd, force_reload=True)
        if snapshot.errors:
            raise SkillReferenceError(
                "Codex 扫描当前 Project 的 Skills 时报告错误："
                + "；".join(snapshot.errors)[:1_000]
            )
        resolved: list[SkillInput] = []
        for name in names:
            matches = tuple(skill for skill in snapshot.skills if skill.name == name)
            if not matches:
                raise SkillReferenceError(
                    f"当前 Project 中找不到 Skill ${name}；本条消息未执行。"
                )
            if len(matches) != 1:
                raise SkillReferenceError(
                    f"Skill ${name} 在当前目录中存在歧义；本条消息未执行。"
                )
            skill = matches[0]
            if not skill.enabled:
                raise SkillReferenceError(
                    f"Skill ${name} 当前未启用；本条消息未执行。"
                )
            resolved.append(SkillInput(name=skill.name, path=skill.path))
        return [*native_input, *resolved]

    def _redeem_submission_admission(
        self,
        *,
        binding_id: str,
        active: _ActiveTurn | None,
        admission: SubmissionAdmission | None,
        goal: _ActiveGoal | None = None,
    ) -> None:
        if admission is None:
            if goal is not None:
                raise SteerRace("当前任务已变为 Goal，本条消息未执行，请重新发送。")
            return
        if admission.revision != self._admission_revision(binding_id):
            raise SteerRace(
                "准备本条消息期间当前任务状态已变化，本条消息未执行，请重新发送。"
            )
        binding = self._bindings.get(binding_id)
        if admission.settings_revision != binding.settings_revision:
            raise SteerRace(
                "准备本条消息期间会话配置已变化，本条消息未执行，请重新发送。"
            )
        if admission.context_revision != binding.context_revision:
            raise SteerRace(
                "准备本条消息期间上下文边界已变化，本条消息未执行，请重新发送。"
            )
        actual_thread_id = (
            goal.thread_id if goal is not None
            else active.handle.thread_id if active is not None else None
        )
        actual_turn_id = (
            self._goal_steer_turn_id(goal) if goal is not None
            else active.handle.id if active is not None else None
        )
        if (
            admission.thread_id != actual_thread_id
            or admission.turn_id != actual_turn_id
        ):
            if goal is not None:
                # Native Goal rollover is independent of Binding revisions.
                # Reject the captured target, never rebase it to the new Turn.
                raise SteerRace(
                    "准备本条消息期间 Goal 物理 Turn 已变化，本条消息未执行，请重新发送。"
                )
            self.close_admission()
            raise RuntimeError(
                "submission admission state changed without a revision; "
                "service admission is closed"
            )
