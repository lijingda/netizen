from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from openai_codex import InvalidRequestError

from netizen_cli.bindings import BindingStore, ProjectDeleting, ProjectDisabled
from netizen_cli.codex_runtime import (
    ActiveState, CodexRuntime, ExternalGoalActive, RuntimeClosed, SteerRace,
    ThreadLifecycleError, ThreadNotMaterialized, ThreadRunningConfiguration,
    ThreadSubscriptionState,
)
from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.sdk_gap_adapter import GoalStatus
from tests import test_codex_runtime as fixtures


class ForkCodex(fixtures.FakeCodex):
    def __init__(self):
        super().__init__()
        self._ensure_initialized = AsyncMock()
        self._client = SimpleNamespace(thread_read=self.read)
        self.read_entered = asyncio.Event()
        self.fork_entered = asyncio.Event()
        self.fork_gate = None
        self.fork_id_override = None

    async def read(self, thread_id, *, include_turns=False):
        self.read_entered.set()
        return await fixtures.FakeThread(thread_id, self).read(include_turns=include_turns)

    async def thread_fork(self, thread_id, **kwargs):
        self.fork_calls.append((thread_id, kwargs))
        self.fork_entered.set()
        if self.fork_gate is not None:
            await self.fork_gate.wait()
        if self.fork_errors:
            raise self.fork_errors.pop(0)
        return fixtures.FakeThread(
            self.fork_id_override or f"fork-{len(self.fork_calls)}", self,
            forked_from_id=thread_id,
        )


class PersistentForkRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.store = BindingStore()
        self.store.bootstrap_project(alias="project", cwd=str(Path(directory)))
        self.project_revision = self.store.get_project("project").revision
        self.scope = FeishuScope("app", "chat", ScopeKind.DIRECT)
        source = self.store.create_channel_binding(
            scope=self.scope, project_alias="project", creator_id="owner",
        )
        self.store.assign_native_thread_id(source.id, "native-source")
        self.source = self.store.get(source.id)
        self.codex = ForkCodex()
        self.subscriptions = fixtures.FakeThreadSubscriptionControl()
        self.goals = fixtures.FakeGoalControl(self.codex)
        self.runtime = CodexRuntime(
            codex=self.codex, bindings=self.store,
            terminal_cleanup=fixtures.FakeTerminalCleanup(self.codex.events),
            goal_control=self.goals,
            thread_subscription_control=self.subscriptions,
            background_terminal_inspector=fixtures.FakeBackgroundTerminalInspector(),
            automatic_thread_naming=False,
        )
        self.addCleanup(self.store.close)

    async def asyncTearDown(self):
        self.runtime.close_admission()
        await self.runtime.cancel_tasks()

    async def fork(self, source=None):
        async with self.runtime.track_fork_creation("project"):
            return await self.runtime.fork_exact(
                source or self.source, expected_project_revision=self.project_revision,
            )

    def begin_delete(self):
        snapshot = self.store.preview_project_delete("project")
        self.store.begin_project_delete(
            alias="project", expected_revision=snapshot.project.revision,
            expected_inventory_fingerprint=snapshot.fingerprint,
        )

    async def test_idle_and_unloaded_sources_use_public_fork_without_resume(self):
        for status in ("idle", "notLoaded"):
            with self.subTest(status=status):
                self.codex.read_statuses.append(status)
                await self.fork()
        self.assertEqual(self.codex.fork_calls, [
            ("native-source", {"ephemeral": False, "include_turns": False}),
            ("native-source", {"ephemeral": False, "include_turns": False}),
        ])
        self.assertEqual(self.codex.read_calls, [("native-source", False)] * 2)
        self.assertEqual(self.codex.resume_calls, [])
        self.assertEqual(self.codex.thread_list_calls, [])
        self.assertEqual(self.codex.turn_calls, [])
        self.assertEqual(self.store.active_binding(self.scope.key).id, self.source.id)
        self.assertFalse(self.runtime.project_has_fork_creation("project"))

    async def test_paused_goal_is_allowed_without_resuming_goal(self):
        self.goals.persisted = fixtures.goal_snapshot(GoalStatus.PAUSED)
        await self.fork()
        self.assertEqual(self.goals.resume_calls, [])
        self.assertEqual(self.goals.start_calls, [])
        self.assertEqual(len(self.codex.fork_calls), 1)

    async def test_active_goal_and_running_sources_do_not_fork(self):
        self.goals.persisted = fixtures.goal_snapshot(GoalStatus.ACTIVE)
        with self.assertRaises(ExternalGoalActive):
            await self.fork()
        self.assertEqual(self.codex.fork_calls, [])
        self.runtime._goals.clear()
        self.goals.persisted = None
        self.runtime._active[self.source.id] = SimpleNamespace(state=ActiveState.RUNNING)
        with self.assertRaises(ThreadRunningConfiguration):
            await self.fork()
        self.runtime._active.clear()
        self.codex.read_statuses.append("active")
        with self.assertRaises(ThreadRunningConfiguration):
            await self.fork()
        self.assertEqual(self.codex.fork_calls, [])

    async def test_stale_source_and_lazy_binding_reject_before_native_io(self):
        for field, value in (
            ("native_thread_id", "changed"), ("settings_revision", 2),
            ("context_revision", 2), ("feedback_revision", 2),
        ):
            with self.subTest(field=field), self.assertRaises(SteerRace):
                await self.fork(replace(self.source, **{field: value}))
        lazy = self.store.create_channel_binding(
            scope=self.scope, project_alias="project", creator_id="owner",
        )
        with self.assertRaises(SteerRace):
            await self.fork()
        with self.assertRaises(ThreadNotMaterialized):
            await self.fork(lazy)
        self.assertEqual(self.codex.read_calls, [])
        self.assertEqual(self.codex.fork_calls, [])

    async def test_disabled_or_deleting_project_rejects_creation(self):
        self.store.set_project_enabled(
            alias="project", enabled=False, expected_revision=self.project_revision,
        )
        with self.assertRaises(ProjectDisabled):
            await self.fork()
        self.begin_delete()
        with self.assertRaises(ProjectDeleting):
            await self.fork()
        self.assertEqual(self.codex.read_calls, [])
        self.assertEqual(self.codex.fork_calls, [])

    async def test_project_deletion_during_read_is_checked_before_fork(self):
        self.codex.read_gate = asyncio.Event()
        creating = asyncio.create_task(self.fork())
        await asyncio.wait_for(self.codex.read_entered.wait(), 1)
        self.assertTrue(self.runtime.project_has_fork_creation("project"))
        self.assertFalse(self.runtime.project_has_fork_creation("unrelated"))
        self.begin_delete()
        self.codex.read_gate.set()
        with self.assertRaises(ProjectDeleting):
            await creating
        self.assertEqual(self.codex.fork_calls, [])

    async def test_shutdown_during_read_prevents_late_native_fork(self):
        self.codex.read_gate = asyncio.Event()
        creating = asyncio.create_task(self.fork())
        await asyncio.wait_for(self.codex.read_entered.wait(), 1)
        self.runtime.close_admission()
        self.codex.read_gate.set()
        with self.assertRaises(RuntimeClosed):
            await creating
        self.assertEqual(self.codex.fork_calls, [])
        self.assertFalse(self.runtime.project_has_fork_creation("project"))

    async def test_rejection_unknown_and_invalid_id_do_not_close_other_admission(self):
        for error in (InvalidRequestError(code=-32600, message="source is archived"), OSError("lost")):
            with self.subTest(error=type(error).__name__):
                self.codex.fork_errors.append(error)
                with self.assertRaises(ThreadLifecycleError):
                    await self.fork()
                self.assertTrue(self.runtime._accepting)
        self.codex.fork_id_override = "native-source"
        with self.assertRaises(ThreadLifecycleError):
            await self.fork()
        self.assertEqual(len(self.codex.fork_calls), 3)
        self.assertEqual(self.subscriptions.calls, [])
        self.assertTrue(self.runtime._accepting)

    async def test_successful_handoff_registers_exact_handle_and_idle_release(self):
        async with self.runtime.track_fork_creation("project"):
            thread = await self.runtime.fork_exact(
                self.source, expected_project_revision=self.project_revision,
            )
            target = self.store.create_channel_binding(
                scope=FeishuScope("app", "chat", ScopeKind.TOPIC, "topic"),
                project_alias="project", creator_id="owner",
            )
            self.store.assign_native_thread_id(target.id, thread.id)
            target = self.store.get(target.id)
            self.assertTrue(await self.runtime.adopt_fork(target, thread, name="  fork\nname  "))
            snapshot = self.runtime.thread_subscription_snapshot(target.id)
            self.assertEqual(snapshot.thread_id, thread.id)
            self.assertEqual(snapshot.state, ThreadSubscriptionState.RELEASE_PENDING)
            self.assertGreater(snapshot.release_in_seconds, 0)
            self.assertIs(self.runtime._subscriptions[target.id].thread, thread)
        self.assertEqual(self.codex.resume_calls, [])
        self.assertEqual(self.codex.set_name_calls, [(thread.id, "fork name")])
        self.assertEqual(self.codex.turn_calls, [])
        self.assertEqual(self.store.active_binding(self.scope.key).id, self.source.id)

    async def test_fork_name_failure_keeps_binding_subscription_and_idle_policy(self):
        for error in (OSError("name result lost"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                async with self.runtime.track_fork_creation("project"):
                    thread = await self.runtime.fork_exact(
                        self.source, expected_project_revision=self.project_revision,
                    )
                    target = self.store.create_channel_binding(
                        scope=FeishuScope("app", "chat", ScopeKind.TOPIC, thread.id),
                        project_alias="project", creator_id="owner",
                    )
                    self.store.assign_native_thread_id(target.id, thread.id)
                    target = self.store.get(target.id)
                    self.codex.set_name_errors.append(error)
                    if isinstance(error, asyncio.CancelledError):
                        with self.assertRaises(asyncio.CancelledError):
                            await self.runtime.adopt_fork(target, thread, name="fork")
                    else:
                        with self.assertLogs("netizen_cli.codex_runtime", level="WARNING"):
                            self.assertFalse(await self.runtime.adopt_fork(target, thread, name="fork"))
                    self.assertEqual(self.store.get(target.id).native_thread_id, thread.id)
                    snapshot = self.runtime.thread_subscription_snapshot(target.id)
                    self.assertEqual(snapshot.state, ThreadSubscriptionState.RELEASE_PENDING)
                    self.assertGreater(snapshot.release_in_seconds, 0)
                    self.assertIs(self.runtime._subscriptions[target.id].thread, thread)
                    self.assertNotIn(target.id, self.runtime._lifecycles)
        self.assertEqual(self.codex.resume_calls, [])
        self.assertEqual(self.codex.turn_calls, [])
        self.assertTrue(self.runtime._accepting)

    async def test_shutdown_after_native_return_preserves_known_handle_for_caller(self):
        self.codex.fork_gate = asyncio.Event()
        received = []

        async def handoff():
            async with self.runtime.track_fork_creation("project"):
                thread = await self.runtime.fork_exact(
                    self.source, expected_project_revision=self.project_revision,
                )
                received.append(thread.id)
                self.assertEqual(self.subscriptions.calls, [])
                try:
                    self.runtime.require_fork_creation_open("project")
                finally:
                    await self.runtime.release_unbound_fork(thread)

        creating = asyncio.create_task(handoff())
        await asyncio.wait_for(self.codex.fork_entered.wait(), 1)
        self.assertFalse(await self.runtime.wait_idle(timeout=0))
        self.runtime.close_admission()
        self.codex.fork_gate.set()
        with self.assertRaises(RuntimeClosed):
            await creating
        self.assertEqual(received, ["fork-1"])
        self.assertEqual(self.subscriptions.calls, ["fork-1"])
        self.assertTrue(await self.runtime.wait_idle(timeout=0))
        self.assertFalse(self.runtime.project_has_fork_creation("project"))

    async def test_shutdown_cancels_creation_and_prevents_final_commit(self):
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def handoff():
            async with self.runtime.track_fork_creation("project"):
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    with self.assertRaises(RuntimeClosed):
                        self.runtime.require_fork_creation_open("project")
                    raise

        creating = asyncio.create_task(handoff())
        await asyncio.wait_for(entered.wait(), 1)
        self.runtime.close_admission()
        await asyncio.wait_for(self.runtime.cancel_tasks(), 1)
        self.assertTrue(cancelled.is_set())
        self.assertTrue(creating.cancelled())
        self.assertFalse(self.runtime.project_has_fork_creation("project"))

    async def test_unbound_release_failure_is_local_and_not_retried(self):
        thread = await self.fork()
        self.subscriptions.errors.append(OSError("unsubscribe lost"))
        with self.assertLogs("netizen_cli.codex_runtime", level="WARNING"):
            self.assertFalse(await self.runtime.release_unbound_fork(thread))
        self.assertEqual(self.subscriptions.calls, [thread.id])
        self.assertTrue(self.runtime._accepting)
        self.assertEqual(self.codex.archive_calls, [])


if __name__ == "__main__":
    unittest.main()
