from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openai_codex import InternalRpcError, InvalidRequestError, TransportClosedError

from netizen_cli.bindings import BindingStore
from netizen_cli.codex_runtime import (
    CodexRuntime,
    ContextAnchorRequired,
    ThreadArchived,
    ThreadLifecycleError,
    ThreadOccupied,
    ThreadResumeFailed,
    ThreadResumeNotFound,
    ThreadSubscriptionState,
)
from netizen_cli.domain import FeishuScope, MentionContextMode, MessageContextAnchor, ScopeKind
from tests import test_codex_runtime as fixtures


class ResumeActivationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.cwd = Path(self.directory.name)
        self.store = BindingStore()
        self.store.bootstrap_project(alias="test", cwd=str(self.cwd))
        self.scope = FeishuScope("app", "chat", ScopeKind.DIRECT)
        self.codex = fixtures.FakeCodex()
        self.subscription = fixtures.FakeThreadSubscriptionControl()
        self.runtime = self.new_runtime()

    def new_runtime(self):
        return CodexRuntime(
            codex=self.codex,
            bindings=self.store,
            terminal_cleanup=fixtures.FakeTerminalCleanup(self.codex.events),
            thread_subscription_control=self.subscription,
            background_terminal_inspector=fixtures.FakeBackgroundTerminalInspector(),
            ordinary_thread_idle_seconds=600,
            automatic_thread_naming=False,
        )

    async def asyncTearDown(self) -> None:
        self.runtime.close_admission()
        await self.runtime.cancel_tasks()
        self.store.close()
        self.directory.cleanup()

    def binding(self, *, native_id=None, catch_up=False):
        binding = self.store.create_channel_binding(
            scope=(FeishuScope("app", "group", ScopeKind.GROUP) if catch_up else self.scope),
            project_alias="test",
            creator_id="user",
            message_context_mode=(MentionContextMode.CATCH_UP if catch_up else MentionContextMode.CURRENT_ONLY),
            context_anchor=MessageContextAnchor("old", 100) if catch_up else None,
        )
        if native_id is not None:
            self.store.assign_native_thread_id(binding.id, native_id)
        return self.store.get(binding.id)

    async def test_lazy_switch_does_not_create_or_resume_native_thread(self) -> None:
        target = self.binding()
        self.binding()
        activated = await self.runtime.activate_exact(target.id)
        self.assertTrue(activated.active)
        self.assertEqual(self.codex.start_kwargs, [])
        self.assertEqual(self.codex.resume_calls, [])

    async def test_known_id_resumes_without_catalog_or_read_and_registers_returned_handle(self) -> None:
        target = self.binding(native_id="fork-with-no-new-turn")
        self.binding()
        returned = fixtures.FakeThread(target.native_thread_id, self.codex)
        with patch.object(self.codex, "thread_resume", return_value=returned) as resume:
            activated = await self.runtime.activate_exact(target.id)
        resume.assert_awaited_once_with(target.native_thread_id, include_turns=False)
        self.assertTrue(activated.active)
        self.assertEqual(self.codex.thread_list_calls, [])
        self.assertEqual(self.codex.read_calls, [])
        self.assertEqual(self.codex.turn_inputs, [])
        self.assertIs(self.runtime._subscriptions[target.id].thread, returned)
        self.assertEqual(
            self.runtime.thread_subscription_snapshot(target.id).state,
            ThreadSubscriptionState.RELEASE_PENDING,
        )

    async def test_anchor_validation_precedes_resume_and_success_commits_new_anchor(self) -> None:
        target = self.binding(native_id="native-1", catch_up=True)
        other = self.binding(catch_up=True)
        with self.assertRaises(ContextAnchorRequired):
            await self.runtime.activate_exact(target.id)
        self.assertEqual(self.codex.resume_calls, [])
        self.assertEqual(self.store.active_binding(target.scope_key).id, other.id)
        anchor = MessageContextAnchor("new", 200)
        activated = await self.runtime.activate_exact(target.id, context_anchor=anchor)
        self.assertEqual(activated.context_anchor, anchor)
        self.assertEqual(activated.context_revision, target.context_revision + 1)

    async def test_verified_native_rejections_keep_binding_and_current(self) -> None:
        target = self.binding(native_id="native-1")
        other = self.binding()
        for message, expected in (
            ("session native-1 is archived. Run `codex unarchive native-1` to unarchive it first.", ThreadArchived),
            ("no rollout found for thread id native-1", ThreadResumeNotFound),
            ("thread native-1 already has an active writer", ThreadOccupied),
            ("thread native-1 is closing; retry thread/resume after the thread is closed", ThreadLifecycleError),
        ):
            with self.subTest(message=message):
                self.codex.resume_errors.append(InvalidRequestError(-32600, message))
                with self.assertRaises(expected):
                    await self.runtime.activate_exact(target.id)
                self.assertEqual(self.store.active_binding(self.scope.key).id, other.id)
                self.assertEqual(self.store.get(target.id).native_thread_id, "native-1")
                self.assertIsNone(self.runtime.thread_subscription_snapshot(target.id))
                self.assertTrue(self.runtime._accepting)
        self.assertEqual(len(self.codex.resume_calls), 4)
        self.assertEqual(self.codex.thread_list_calls, [])
        self.assertEqual(self.codex.archive_calls, [])
        self.assertEqual(self.codex.unarchive_calls, [])

    async def test_unverified_errors_close_admission_without_switch_or_retry(self) -> None:
        target = self.binding(native_id="native-1")
        other = self.binding()
        for error in (
            TransportClosedError("lost response"),
            TimeoutError("deadline"),
            InvalidRequestError(-32600, "no rollout found for thread id another-thread"),
            InvalidRequestError(-32600, "prefix no rollout found for thread id native-1"),
            InvalidRequestError(-32603, "no rollout found for thread id native-1"),
            InternalRpcError(-32600, "no rollout found for thread id native-1"),
        ):
            with self.subTest(error=error):
                self.runtime = self.new_runtime()
                self.codex.resume_errors.append(error)
                calls = len(self.codex.resume_calls)
                with self.assertRaises(ThreadResumeFailed):
                    await self.runtime.activate_exact(target.id)
                self.assertEqual(len(self.codex.resume_calls), calls + 1)
                self.assertFalse(self.runtime._accepting)
                self.assertEqual(self.store.active_binding(self.scope.key).id, other.id)
                self.assertEqual(self.store.get(target.id).native_thread_id, "native-1")

    async def test_resume_cancellation_closes_admission_and_preserves_current(self) -> None:
        target = self.binding(native_id="native-1")
        other = self.binding()
        entered = asyncio.Event()

        async def blocked_resume(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        with patch.object(self.codex, "thread_resume", side_effect=blocked_resume):
            pending = asyncio.create_task(self.runtime.activate_exact(target.id))
            await asyncio.wait_for(entered.wait(), timeout=1)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
        self.assertFalse(self.runtime._accepting)
        self.assertEqual(self.store.active_binding(self.scope.key).id, other.id)

    async def test_wrong_native_identity_cannot_commit_current(self) -> None:
        target = self.binding(native_id="native-1")
        other = self.binding()
        self.codex.resume_thread_id_override = "wrong-native"
        with self.assertRaises(ThreadResumeFailed):
            await self.runtime.activate_exact(target.id)
        self.assertFalse(self.runtime._accepting)
        self.assertEqual(self.store.active_binding(self.scope.key).id, other.id)
        self.assertIsNone(self.runtime.thread_subscription_snapshot(target.id))

    async def test_local_failure_reports_partial_and_retains_known_subscription(self) -> None:
        target = self.binding(native_id="native-1")
        other = self.binding()
        original = self.store.activate
        for after_commit in (False, True):
            with self.subTest(after_commit=after_commit):
                def failing_activate(**kwargs):
                    if after_commit:
                        original(**kwargs)
                    raise RuntimeError("local activation failed")

                with patch.object(self.store, "activate", side_effect=failing_activate):
                    with self.assertRaisesRegex(ThreadResumeFailed, "原生 Codex 会话已恢复，但本地切换结果未确认"):
                        await self.runtime.activate_exact(target.id)
                self.assertEqual(
                    self.store.active_binding(self.scope.key).id,
                    target.id if after_commit else other.id,
                )
                self.assertIsNotNone(self.runtime.thread_subscription_snapshot(target.id))
                self.assertEqual(self.store.get(target.id).native_thread_id, "native-1")

    async def test_running_turn_rejoin_preserves_turn_handle(self) -> None:
        target = self.binding()
        self.codex.read_gate = asyncio.Event()
        submission = await self.runtime.submit(
            binding=target, cwd=self.cwd, input="work", owner_id="user", origin=object(),
        )
        running = self.runtime._active[target.id]
        self.binding()
        activated = await self.runtime.activate_exact(target.id)
        self.assertTrue(activated.active)
        self.assertIs(self.runtime._active[target.id], running)
        self.assertEqual(self.runtime.active_turn(target.id).turn_id, submission.turn_id)
        self.assertEqual(len(self.codex.handles), 1)
        self.assertEqual(self.codex.handles[0].interrupt_count, 0)

    async def test_running_goal_rejoin_preserves_existing_handle_without_new_goal_policy(self) -> None:
        target = self.binding()
        control = fixtures.FakeGoalControl(self.codex)
        self.runtime._goal_control = control
        await self.runtime.start_goal(
            binding=target, cwd=self.cwd, objective="work", owner_id="user", origin=object(),
        )
        running = self.runtime._goals[target.id]
        reads = list(control.get_calls)
        self.binding()
        activated = await self.runtime.activate_exact(target.id)
        self.assertTrue(activated.active)
        self.assertIs(self.runtime._goals[target.id], running)
        self.assertEqual(control.get_calls, reads)
        self.assertEqual(control.resume_calls, [])
        self.assertEqual(len(control.handles), 1)
