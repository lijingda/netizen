from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from openai_codex import ExternalMessage, InvalidRequestError

from netizen_cli.bindings import BindingStore, BindingTurnSettings
from netizen_cli.codex_runtime import (
    ActiveState, CodexRuntime, ExternalGoalActive, GoalStateUnknown, RuntimeClosed, SteerRace,
    SubmitDisposition, ThreadLifecycleError, ThreadStopping, TurnStartFailed,
)
from netizen_cli.domain import FeishuScope, GoalOperationState, MentionContextMode, MessageContextAnchor, ScopeKind
from netizen_cli.model_settings import ModelCatalogError
from netizen_cli.progress_card_settings import ProgressCardSettings
from netizen_cli.sdk_gap_adapter import GoalControlError, GoalStatus
from tests import test_codex_runtime as fixtures
from tests.test_model_settings import Effort, effort, model


class ExternalThread(fixtures.FakeThread):
    async def turn(self, input, **kwargs):
        if isinstance(input, ExternalMessage):
            running = [
                item for item in self.codex.handles
                if item.thread_id == self.id and item.record.status.value == "inProgress"
            ]
            if running and self.codex.advance_on_external:
                running[-1].complete(response="first result")
            elif running:
                self.codex.turn_calls.append((self.id, input, kwargs))
                self.codex.turn_inputs.append((self.id, input))
                return fixtures.FakeTurnHandle(self.id, running[-1].id, self.codex.events)
        return await super().turn(input, **kwargs)


class ExternalCodex(fixtures.FakeCodex):
    def __init__(self):
        super().__init__()
        self.advance_on_external = False
        self._ensure_initialized = AsyncMock()
        self._client = SimpleNamespace(thread_read=self.read)
        self.native_model = "wire-alpha"
        self.model_response = SimpleNamespace(data=[
            model("alpha", default=True, efforts=[effort(Effort.LOW)], default_effort=Effort.LOW),
            model("beta", default=False, efforts=[effort(Effort.LOW)], default_effort=Effort.LOW),
        ])

    async def read(self, thread_id, *, include_turns=False):
        response = await ExternalThread(thread_id, self).read(include_turns=include_turns)
        response.thread.model = self.native_model
        return response

    async def thread_start(self, **kwargs):
        original = await super().thread_start(**kwargs)
        return ExternalThread(original.id, self)

    async def thread_resume(self, thread_id, **kwargs):
        original = await super().thread_resume(thread_id, **kwargs)
        return ExternalThread(original.id, self)


class SessionExternalRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch("netizen_cli.codex_runtime._INTERRUPTION_CONFIRMATION_SECONDS", 0.001))
        self.cwd = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = BindingStore()
        self.addCleanup(self.store.close)
        self.store.bootstrap_project(alias="project", cwd=str(self.cwd))
        self.scope = FeishuScope("app", "chat", ScopeKind.DIRECT)
        self.binding = self.store.create_channel_binding(
            scope=self.scope, project_alias="project", creator_id="creator",
        )
        self.codex = ExternalCodex()
        self.outcomes = []
        self.completion = asyncio.Event()
        self.progress_card_defaults = ProgressCardSettings()

        async def complete(outcome):
            self.outcomes.append(outcome)
            self.completion.set()

        self.runtime = CodexRuntime(
            codex=self.codex, bindings=self.store,
            terminal_cleanup=fixtures.FakeTerminalCleanup(self.codex.events),
            on_completion=complete, automatic_thread_naming=False,
            progress_card_defaults=lambda: self.progress_card_defaults,
            poll_interval_seconds=0,
        )

    async def asyncTearDown(self):
        self.runtime.close_admission()
        await self.runtime.cancel_tasks()

    async def send(self, *, binding=None, owner="agent", origin=None, selected_model=None):
        return await self.runtime.submit_external(
            binding=binding or self.binding, cwd=self.cwd,
            message=ExternalMessage("send_message_to_thread", "tool-origin task", "netizen"),
            owner_id=owner, origin=origin, model=selected_model,
        )

    async def finish(self, submission, response="done"):
        next(handle for handle in self.codex.handles if handle.id == submission.turn_id).complete(
            response=response,
        )
        if submission.release_receipt_attempt is not None:
            submission.release_receipt_attempt()
        self.assertTrue(await self.runtime.wait_idle(timeout=1))

    async def test_external_first_turn_preserves_tool_input_and_feedback_origin(self):
        origin = object()
        first = await self.send(origin=origin)
        self.assertEqual(first.disposition, SubmitDisposition.STARTED)
        self.assertIsInstance(self.codex.turn_inputs[0][1], ExternalMessage)
        self.assertEqual(self.store.get(self.binding.id).native_thread_id, first.thread_id)
        await self.finish(first)
        self.assertIs(self.outcomes[0].origin, origin)
        self.assertEqual(self.outcomes[0].owner_id, "agent")
        self.assertEqual(self.outcomes[0].final_response, "done")

    async def test_join_keeps_original_consumer_owner_and_origin(self):
        origin = object()
        first_settings = ProgressCardSettings(organization="time")
        self.progress_card_defaults = first_settings
        first = await self.send(owner="original", origin=origin)
        self.progress_card_defaults = ProgressCardSettings(operation_limit=16)
        active = self.runtime._active[self.binding.id]
        task = active.task
        read_count = len(self.codex.read_calls)
        second = await self.runtime.submit_external(
            binding=self.binding,
            message=ExternalMessage("send_message_to_thread", "join", "netizen"),
            owner_id="new", origin=object(),
        )
        self.assertEqual(second.disposition, SubmitDisposition.JOINED)
        self.assertEqual((second.thread_id, second.turn_id), (first.thread_id, first.turn_id))
        self.assertIsNone(second.release_receipt_attempt)
        self.assertEqual(second.task_feedback.progress_card_settings, first_settings)
        self.assertIs(self.runtime._active[self.binding.id].task, task)
        self.assertEqual(self.codex.handles[0].steers, [])
        self.assertEqual(self.codex.resume_calls, [])
        self.assertEqual(len(self.codex.read_calls), read_count)
        await self.finish(first)
        self.assertEqual(len(self.outcomes), 1)
        self.assertEqual(self.outcomes[0].owner_id, "original")
        self.assertIs(self.outcomes[0].origin, origin)

    async def test_native_next_turn_keeps_previous_result_without_removing_new_active(self):
        first_origin, second_origin = object(), object()
        worktree = self.cwd / "worktree"
        worktree.mkdir()
        first = await self.runtime.submit_external(
            binding=self.binding, cwd=worktree,
            message=ExternalMessage("send_message_to_thread", "first task", "netizen"),
            owner_id="agent", origin=first_origin,
        )
        first.release_receipt_attempt()
        first_settings = first.task_feedback.progress_card_settings
        self.progress_card_defaults = ProgressCardSettings(organization="time")
        self.codex.advance_on_external = True
        read_count = len(self.codex.read_calls)
        # The helper passes the stale Project cwd, which is not an execution
        # directory for this already materialized, locally active Thread.
        second = await self.send(origin=second_origin)
        second.release_receipt_attempt()
        self.assertEqual(second.disposition, SubmitDisposition.STARTED)
        self.assertEqual(second.task_feedback.progress_card_settings, self.progress_card_defaults)
        self.assertNotEqual(second.turn_id, first.turn_id)
        self.assertEqual(self.runtime._active[self.binding.id].cwd, worktree)
        self.assertEqual(len(self.codex.read_calls), read_count)
        self.assertEqual(self.codex.resume_calls, [])
        catalog = fixtures.FakeSkillCatalog(fixtures.fake_skills())
        self.runtime._skill_catalog = catalog
        steered = await self.runtime.submit(
            binding=self.binding, input="$code-review inspect",
            skill_names=("code-review",), owner_id="user", origin=object(),
        )
        self.assertEqual(steered.turn_id, second.turn_id)
        self.assertEqual(catalog.calls, [(worktree, True)])
        self.assertEqual(len(self.codex.read_calls), read_count)
        self.assertEqual(self.codex.resume_calls, [])
        await asyncio.wait_for(self.completion.wait(), timeout=1)
        self.assertEqual(self.outcomes[0].turn_id, first.turn_id)
        self.assertEqual(self.outcomes[0].final_response, "first result")
        self.assertEqual(self.outcomes[0].task_feedback.progress_card_settings, first_settings)
        self.assertIs(self.outcomes[0].origin, first_origin)
        self.assertEqual(self.runtime.active_turn(self.binding.id).turn_id, second.turn_id)
        await self.finish(second, "second result")
        self.assertEqual([item.turn_id for item in self.outcomes], [first.turn_id, second.turn_id])
        self.assertIs(self.outcomes[1].origin, second_origin)
        self.assertEqual([item.cwd for item in self.outcomes], [worktree, worktree])
        self.assertEqual(self.runtime._retiring_turns, {})
        self.assertEqual(self.outcomes[1].task_feedback.progress_card_settings, self.progress_card_defaults)
        self.assertIsNone(self.store.get(self.binding.id).task_feedback.progress_card_settings)

    async def test_active_external_input_never_falls_back_when_accepted_cwd_is_missing(self):
        first = await self.send()
        active = self.runtime._active[self.binding.id]
        active.cwd = None
        read_count, turn_count = len(self.codex.read_calls), len(self.codex.turn_calls)
        for advance in (False, True):
            with self.subTest(advance=advance):
                self.codex.advance_on_external = advance
                with self.assertRaises(SteerRace):
                    await self.send()
        self.assertEqual(len(self.codex.read_calls), read_count)
        self.assertEqual(len(self.codex.turn_calls), turn_count)
        self.assertEqual(self.codex.resume_calls, [])
        self.assertTrue(self.runtime._accepting)
        active.cwd = self.cwd
        await self.finish(first)

    async def test_idle_target_resumes_same_native_thread(self):
        first = await self.send()
        await self.finish(first)
        second = await self.send()
        self.assertEqual(second.thread_id, first.thread_id)
        self.assertNotEqual(second.turn_id, first.turn_id)
        self.assertEqual(len(self.codex.start_kwargs), 1)
        self.assertEqual(self.codex.resume_calls[0][0], first.thread_id)
        await self.finish(second)

    async def test_previous_terminal_stream_cannot_replace_newer_usage_or_result(self):
        first = await self.send()
        first.release_receipt_attempt()
        old_handle = self.codex.handles[0]
        old_handle.notifications.put_nowait(fixtures.token_usage_notification(
            thread_id=first.thread_id, turn_id=first.turn_id, used_tokens=10_000,
        ))
        stream_entered, release_stream = asyncio.Event(), asyncio.Event()
        original_stream = old_handle.stream

        async def delayed_stream():
            stream_entered.set()
            await release_stream.wait()
            async for notification in original_stream():
                yield notification

        old_handle.stream = delayed_stream
        self.codex.advance_on_external = True
        second = await self.send()
        second.release_receipt_attempt()
        await asyncio.wait_for(stream_entered.wait(), timeout=1)
        new_handle = self.codex.handles[1]
        new_handle.notifications.put_nowait(fixtures.token_usage_notification(
            thread_id=second.thread_id, turn_id=second.turn_id, used_tokens=20_000,
        ))
        new_handle.complete(response="new result")
        await asyncio.wait_for(self.completion.wait(), timeout=1)
        self.assertEqual([item.turn_id for item in self.outcomes], [second.turn_id])
        self.assertEqual(self.runtime.context_window_usage(self.binding.id).used_tokens, 20_000)
        release_stream.set()
        self.assertTrue(await self.runtime.wait_idle(timeout=1))
        self.assertEqual([item.turn_id for item in self.outcomes], [second.turn_id, first.turn_id])
        self.assertEqual(self.runtime.context_window_usage(self.binding.id).used_tokens, 20_000)

    async def test_background_target_is_rejected_without_activation(self):
        other = self.store.create_channel_binding(
            scope=self.scope, project_alias="project", creator_id="creator",
        )
        with self.assertRaises(SteerRace):
            await self.send()
        self.assertTrue(self.store.get(other.id).active)
        self.assertEqual(self.codex.start_kwargs, [])
        self.assertEqual(self.codex.resume_calls, [])

    async def test_external_input_does_not_consume_catchup_boundary(self):
        anchor = MessageContextAnchor("old-message", 1000)
        binding = self.store.create_channel_binding(
            scope=FeishuScope("app", "group", ScopeKind.GROUP),
            project_alias="project", creator_id="creator",
            message_context_mode=MentionContextMode.CATCH_UP, context_anchor=anchor,
        )
        first = await self.send(binding=binding)
        self.assertEqual(self.store.get(binding.id).context_anchor, anchor)
        await self.finish(first)

    async def test_model_override_only_changes_native_request(self):
        configured = BindingTurnSettings("alpha", "low", "default")
        binding = self.store.create_channel_binding(
            scope=self.scope, project_alias="project", creator_id="creator",
            turn_settings=configured,
        )
        first = await self.send(binding=binding, selected_model="beta")
        self.assertEqual(self.codex.start_kwargs[-1]["model"], "wire-beta")
        self.assertEqual(self.codex.turn_calls[-1][2], {"model": "wire-beta"})
        self.assertEqual(self.store.get(binding.id).turn_settings, configured)
        await self.finish(first)

    async def test_model_read_is_exact_readonly_and_missing_value_is_not_guessed(self):
        self.store.assign_native_thread_id(self.binding.id, "native-source")
        self.assertEqual(await self.runtime.native_thread_model(self.binding.id), "wire-alpha")
        self.assertEqual(self.codex.resume_calls, [])
        self.codex.native_model = None
        with self.assertRaises(ModelCatalogError):
            await self.runtime.native_thread_model(self.binding.id)

    async def test_goal_target_is_rejected_before_delivery(self):
        self.store.assign_native_thread_id(self.binding.id, "native-source")
        goals = fixtures.FakeGoalControl(self.codex)
        goals.persisted = fixtures.goal_snapshot(GoalStatus.ACTIVE)
        self.runtime._goal_control = goals
        with self.assertRaises(ExternalGoalActive):
            await self.send()
        self.assertEqual(self.codex.turn_inputs, [])

        self.runtime._goals[self.binding.id].state = GoalOperationState.UNKNOWN
        with self.assertRaises(GoalStateUnknown):
            await self.send()
        self.assertEqual(self.codex.turn_inputs, [])
        self.assertTrue(self.runtime._accepting)

    async def test_goal_read_failure_rejects_without_attempting_delivery(self):
        self.store.assign_native_thread_id(self.binding.id, "native-source")
        self.runtime._goal_control = SimpleNamespace(
            get=AsyncMock(side_effect=ConnectionError("Goal read unavailable")),
        )
        with self.assertRaises(GoalControlError):
            await self.send()
        self.assertEqual(self.codex.turn_inputs, [])
        self.assertEqual(self.codex.resume_calls, [])
        self.assertTrue(self.runtime._accepting)

    async def test_stopping_target_is_rejected_before_native_request(self):
        first = await self.send()
        self.runtime._active[self.binding.id].state = ActiveState.STOPPING
        with self.assertRaises(ThreadStopping):
            await self.send()
        self.assertEqual(len(self.codex.turn_inputs), 1)
        self.runtime._active[self.binding.id].state = ActiveState.RUNNING
        await self.finish(first)

    async def test_unknown_delivery_closes_admission_without_retry(self):
        self.codex.turn_errors_after_start.append(ConnectionError("response lost"))
        with self.assertRaises(TurnStartFailed):
            await self.send()
        with self.assertRaises(RuntimeClosed):
            await self.send()
        self.assertEqual(len(self.codex.turn_inputs), 1)
        self.assertIsNotNone(self.store.get(self.binding.id).native_thread_id)

    async def test_explicit_native_rejection_does_not_close_other_bindings(self):
        self.codex.turn_errors_after_start.append(InvalidRequestError(-32600, "rejected"))
        with self.assertRaises(InvalidRequestError):
            await self.send()
        self.assertTrue(self.runtime._accepting)
        self.assertEqual(len(self.codex.turn_inputs), 1)

    async def test_exact_resume_rejections_do_not_close_unrelated_bindings(self):
        thread_id = "native-target"
        self.store.assign_native_thread_id(self.binding.id, thread_id)
        for message in (
            f"session {thread_id} is archived. Run `codex unarchive {thread_id}` "
            "to unarchive it first.",
            f"no rollout found for thread id {thread_id}",
            f"thread {thread_id} is closing; retry thread/resume after the thread is closed",
        ):
            with self.subTest(message=message):
                self.codex.resume_errors.append(InvalidRequestError(-32600, message))
                with self.assertRaises(ThreadLifecycleError):
                    await self.send()
                self.assertTrue(self.runtime._accepting)
                self.assertEqual(self.codex.turn_inputs, [])

    async def test_admission_closing_during_thread_creation_preserves_id_without_sending(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original_start = self.codex.thread_start

        async def delayed_start(**kwargs):
            entered.set()
            await release.wait()
            return await original_start(**kwargs)

        self.codex.thread_start = delayed_start
        sending = asyncio.create_task(self.send())
        await asyncio.wait_for(entered.wait(), timeout=1)
        self.runtime.close_admission()
        release.set()
        with self.assertRaises(RuntimeClosed):
            await sending
        self.assertIsNotNone(self.store.get(self.binding.id).native_thread_id)
        self.assertEqual(self.codex.turn_inputs, [])

    async def test_shutdown_owns_turn_accepted_after_its_initial_snapshot(self):
        entered, release = asyncio.Event(), asyncio.Event()
        self.codex.turn_entered["native-1"] = entered
        self.codex.turn_gates["native-1"] = release
        sending = asyncio.create_task(self.send())
        await asyncio.wait_for(entered.wait(), timeout=1)
        await self.runtime.interrupt_all()
        release.set()
        accepted = await sending
        self.assertEqual(accepted.disposition, SubmitDisposition.STARTED)
        self.assertTrue(await self.runtime.wait_idle(timeout=1))
        self.assertEqual(self.codex.handles[0].interrupt_count, 1)
        self.assertEqual(self.outcomes[0].turn_id, accepted.turn_id)
        self.assertEqual(self.outcomes[0].result.status.value, "interrupted")

    async def test_target_switched_during_resume_is_not_sent_or_reactivated(self):
        self.store.assign_native_thread_id(self.binding.id, "native-target")
        entered, release = asyncio.Event(), asyncio.Event()
        original_resume = self.codex.thread_resume

        async def delayed_resume(thread_id, **kwargs):
            entered.set()
            await release.wait()
            return await original_resume(thread_id, **kwargs)

        self.codex.thread_resume = delayed_resume
        sending = asyncio.create_task(self.send())
        await asyncio.wait_for(entered.wait(), timeout=1)
        current = self.store.create_channel_binding(
            scope=self.scope, project_alias="project", creator_id="creator",
        )
        release.set()
        with self.assertRaises(SteerRace):
            await sending
        self.assertEqual(self.codex.turn_inputs, [])
        self.assertTrue(self.store.get(current.id).active)

    async def test_external_role_cannot_accidentally_enter_ordinary_steer(self):
        with self.assertRaises(TypeError):
            await self.runtime.submit(
                binding=self.binding, cwd=self.cwd,
                input=ExternalMessage("external", "content"),
                owner_id="owner", origin=object(),
            )
        self.assertEqual(self.codex.turn_inputs, [])

    async def test_topic_creation_reuses_fork_project_shutdown_ownership(self):
        async with self.runtime.track_topic_creation("project"):
            self.assertTrue(self.runtime.project_has_fork_creation("project"))
            self.runtime.require_topic_creation_open("project")
            self.runtime.require_fork_creation_open("project")
        self.assertFalse(self.runtime.project_has_fork_creation("project"))


if __name__ == "__main__":
    unittest.main()
