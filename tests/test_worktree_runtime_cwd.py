from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from openai_codex import ExternalMessage, ImageInput, SkillInput, TextInput
from openai_codex.generated.v2_all import AbsolutePathBuf

from netizen_cli.bindings import BindingStore
from netizen_cli.codex_runtime import (
    CodexRuntime, ReleaseDisposition, SkillReferenceError, SteerRace,
    SubmitDisposition, ThreadCatalogError,
)
from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.sdk_gap_adapter import GoalControlError, GoalStatus
from netizen_cli.worktrees.models import WorktreeConflict
from tests import test_codex_runtime as fixtures


class CwdCodex(fixtures.FakeCodex):
    def __init__(self):
        super().__init__()
        self._ensure_initialized = AsyncMock()
        self._client = SimpleNamespace(thread_read=self.read)
        self.read_override = None

    async def read(self, thread_id, *, include_turns=False):
        if self.read_override is not None:
            return self.read_override
        return await fixtures.FakeThread(thread_id, self).read(include_turns=include_turns)


class WorktreeRuntimeCwdTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.main = self.root / "main"
        self.main.mkdir()
        self.selected = self.root / "worktree"
        self.selected.mkdir()
        self.store = BindingStore()
        self.addCleanup(self.store.close)
        self.store.bootstrap_project(alias="project", cwd=str(self.main))
        self.binding = self.store.create_channel_binding(
            scope=FeishuScope("app", "chat", ScopeKind.DIRECT),
            project_alias="project", creator_id="user", working_cwd=str(self.selected),
        )
        self.codex = CwdCodex()
        self.inspector = fixtures.FakeBackgroundTerminalInspector()
        self.goals = fixtures.FakeGoalControl(self.codex)
        self.side = fixtures.FakeSideThreadControl()
        self.outcomes = []

        async def capture(outcome):
            self.outcomes.append(outcome)

        self.runtime = CodexRuntime(
            codex=self.codex, bindings=self.store,
            terminal_cleanup=fixtures.FakeTerminalCleanup(self.codex.events),
            background_terminal_inspector=self.inspector,
            goal_control=self.goals, side_boundary_control=self.side,
            thread_subscription_control=self.side, on_completion=capture,
            automatic_thread_naming=False, poll_interval_seconds=0,
        )

    async def asyncTearDown(self):
        self.runtime.close_admission()
        await self.runtime.cancel_tasks()

    def materialize(self, cwd=None):
        self.store.assign_native_thread_id(self.binding.id, "native-source")
        self.binding = self.store.get(self.binding.id)
        self.codex.thread_cwds["native-source"] = str(cwd or self.selected)

    def managed(self, cwd):
        record = self.store.worktrees.register(
            project_alias="project", expected_project_revision=self.store.get_project("project").revision,
            root=str(cwd), cwd=str(cwd), common_dir=str(self.main / ".git"),
        )
        return self.store.worktrees.mark_ready(
            record.id, git_dir=str(cwd / ".git-data"), identity_token=record.id,
        )

    async def submit(self, **kwargs):
        cwd = await self.runtime.resolve_binding_cwd(self.binding.id, default_cwd=self.main)
        return await self.runtime.submit(
            binding=self.binding, cwd=cwd, input="work", owner_id="user", origin=object(),
            **kwargs,
        )

    async def finish(self, submission):
        self.codex.handles[-1].complete()
        submission.release_receipt_attempt()
        self.assertTrue(await self.runtime.wait_idle(timeout=1))

    async def test_lazy_selection_drives_first_turn_and_its_files_context(self):
        self.assertEqual(await self.runtime.resolve_binding_cwd(self.binding.id, default_cwd=self.main), self.selected)
        submission = await self.runtime.submit(
            binding=self.binding, input="work", owner_id="user", origin=object(),
        )
        self.assertEqual(self.codex.start_kwargs, [{"cwd": str(self.selected)}])
        self.assertEqual(self.codex.resume_calls, [])
        self.assertEqual(self.codex.read_calls, [])
        await self.finish(submission)
        self.assertEqual(self.outcomes[-1].cwd, self.selected)

    async def test_lazy_without_selected_or_explicit_absolute_creation_cwd_is_rejected(self):
        binding = self.store.create_channel_binding(
            scope=FeishuScope("app", "unselected", ScopeKind.DIRECT),
            project_alias="project", creator_id="user",
        )
        for cwd in (None, Path("relative")):
            with self.subTest(cwd=cwd):
                with self.assertRaisesRegex(SteerRace, "绝对工作目录"):
                    await self.runtime.submit(
                        binding=binding, cwd=cwd, input="work", owner_id="user", origin=object(),
                    )
                with self.assertRaisesRegex(SteerRace, "绝对工作目录"):
                    await self.runtime.submit_external(
                        binding=binding, cwd=cwd,
                        message=ExternalMessage("send_message_to_thread", "work", "netizen"),
                        owner_id="user", origin=object(),
                    )
                with self.assertRaisesRegex(SteerRace, "绝对工作目录"):
                    await self.runtime.start_goal(
                        binding=binding, cwd=cwd, objective="work", owner_id="user", origin=object(),
                    )
        self.assertEqual(self.codex.start_kwargs, [])
        self.assertEqual(self.codex.resume_calls, [])
        self.assertEqual(self.codex.read_calls, [])
        self.assertEqual(self.codex.turn_calls, [])
        self.assertEqual(self.goals.start_calls, [])
        self.assertIsNone(self.store.get(binding.id).native_thread_id)
        self.assertTrue(self.runtime._accepting)

    async def test_lazy_selection_drives_skill_compilation_before_native_start(self):
        compile_input = AsyncMock(return_value="compiled")
        with patch.object(self.runtime, "_compile_skill_input", compile_input):
            submission = await self.submit(skill_names=("review",))
        self.assertEqual(compile_input.call_args.kwargs["cwd"], self.selected)
        await self.finish(submission)

    async def test_materialized_read_uses_native_even_when_selected_directory_disappears(self):
        missing = self.root / "already-deleted"
        self.materialize(missing)
        self.assertEqual(await self.runtime.resolve_binding_cwd(self.binding.id, default_cwd=self.main), missing)
        self.assertEqual(self.codex.read_calls, [("native-source", False)])
        self.assertEqual(self.codex.resume_calls, [])
        self.assertEqual(self.codex.start_kwargs, [])

    async def test_invalid_native_cwd_never_falls_back(self):
        self.materialize()
        for native in (
            SimpleNamespace(id="native-source"),
            SimpleNamespace(id="native-source", cwd="relative"),
            SimpleNamespace(id="wrong-thread", cwd=str(self.selected)),
        ):
            with self.subTest(native=native):
                self.codex.read_override = SimpleNamespace(thread=native)
                with self.assertRaises(ThreadCatalogError):
                    await self.runtime.resolve_binding_cwd(self.binding.id, default_cwd=self.main)

    async def test_public_sdk_absolute_path_model_is_decoded(self):
        self.materialize()
        self.codex.read_override = SimpleNamespace(thread=SimpleNamespace(
            id="native-source", cwd=AbsolutePathBuf(str(self.selected)),
        ))
        self.assertEqual(await self.runtime.native_thread_cwd(self.binding.id), self.selected)

    async def test_resume_does_not_override_native_cwd_and_terminal_uses_native_fact(self):
        self.materialize()
        submission = await self.runtime.submit(
            binding=self.binding, input="work", owner_id="user", origin=object(),
        )
        self.assertEqual(self.codex.resume_calls, [("native-source", {"include_turns": False})])
        self.assertEqual(self.codex.read_calls, [("native-source", False)])
        self.assertNotIn("cwd", self.codex.turn_calls[-1][2])
        await self.finish(submission)
        self.assertEqual(self.outcomes[-1].cwd, self.selected)

    async def test_resume_handoff_adopts_native_cwd_and_continues_same_input(self):
        self.materialize()
        prepared = await self.runtime.resolve_binding_cwd(self.binding.id, default_cwd=self.main)
        original_resume = self.codex.thread_resume

        async def moved_resume(thread_id, **kwargs):
            self.codex.thread_cwds[thread_id] = str(self.main)
            return await original_resume(thread_id, **kwargs)

        with patch.object(self.codex, "thread_resume", side_effect=moved_resume), patch.object(
            self.runtime, "_commit_context_cursor_locked",
        ) as commit:
            native_input = [TextInput("same input"), ImageInput("data:image/png;base64,aW1n")]
            submission = await self.runtime.submit(
                binding=self.binding, cwd=prepared, input=native_input,
                owner_id="user", origin=object(),
            )
            commit.assert_called_once()
        self.assertEqual(len(self.codex.turn_calls), 1)
        self.assertEqual(self.codex.turn_calls[0][1], native_input)
        self.assertTrue(self.runtime._accepting)
        self.assertEqual(self.codex.resume_calls, [("native-source", {"include_turns": False})])
        self.assertEqual(self.store.get(self.binding.id).working_cwd, str(self.main))
        self.assertEqual(self.codex.thread_cwds["native-source"], str(self.main))
        await self.finish(submission)
        self.assertEqual(self.outcomes[-1].cwd, self.main)
        self.assertNotIn("cwd", self.codex.turn_calls[-1][2])

    async def test_resume_cwd_read_failure_rejects_only_this_input(self):
        self.materialize()
        self.codex.read_errors.append(RuntimeError("read unavailable"))
        with self.assertRaisesRegex(SteerRace, "无法确认恢复后的原生工作目录"):
            await self.runtime.submit(
                binding=self.binding, cwd=self.selected, input="prepared", owner_id="user", origin=object(),
            )
        self.assertEqual(self.codex.turn_calls, [])
        self.assertTrue(self.runtime._accepting)
        self.assertIsNotNone(self.runtime._subscriptions[self.binding.id].idle_task)
        self.assertEqual(self.store.get(self.binding.id).working_cwd, str(self.selected))

    async def test_materialized_skill_is_discovered_only_after_resume_in_actual_cwd(self):
        self.materialize()
        original_resume = self.codex.thread_resume
        catalog = fixtures.FakeSkillCatalog(())
        self.runtime._skill_catalog = catalog
        skill_path = str(self.main / "skills/review/SKILL.md")

        async def moved_resume(thread_id, **kwargs):
            self.codex.thread_cwds[thread_id] = str(self.main)
            return await original_resume(thread_id, **kwargs)

        async def discover(cwd, *, force_reload=True):
            self.assertEqual(cwd, self.main)
            self.assertEqual(len(self.codex.resume_calls), 1)
            self.assertEqual(self.codex.turn_calls, [])
            return fixtures.SkillCatalogSnapshot(
                cwd, (fixtures.DiscoveredSkill("review", skill_path, True),), (),
            )

        with patch.object(self.codex, "thread_resume", side_effect=moved_resume), patch.object(
            catalog, "list", side_effect=discover,
        ) as discovery:
            submission = await self.runtime.submit(
                binding=self.binding, input="$review inspect",
                owner_id="user", origin=object(), skill_names=("review",),
            )
        discovery.assert_awaited_once_with(self.main, force_reload=True)
        self.assertEqual(self.codex.turn_inputs[0][1], [
            TextInput("$review inspect"), SkillInput("review", skill_path),
        ])
        self.assertEqual(len(self.codex.resume_calls), 1)
        self.assertEqual(self.codex.read_calls, [("native-source", False)])
        self.assertNotIn("cwd", self.codex.turn_calls[0][2])
        await self.finish(submission)
        self.assertEqual(self.outcomes[-1].cwd, self.main)

    async def test_lazy_skill_rejects_identity_materialized_by_failed_goal_start(self):
        catalog = fixtures.FakeSkillCatalog(())
        self.runtime._skill_catalog = catalog

        async def discover(cwd, *, force_reload=True):
            self.assertEqual(cwd, self.selected)
            self.codex.read_errors.append(RuntimeError("goal readiness failed"))
            with self.assertRaises(GoalControlError):
                await self.runtime.start_goal(
                    binding=self.binding, cwd=self.selected, objective="concurrent goal",
                    owner_id="user", origin=object(),
                )
            self.assertEqual(self.store.get(self.binding.id).native_thread_id, "native-1")
            self.assertEqual(
                await self.runtime.release_exact(self.binding.id), ReleaseDisposition.RELEASED,
            )
            # Another native client can now change this newly materialized cwd.
            self.codex.thread_cwds["native-1"] = str(self.main)
            return fixtures.SkillCatalogSnapshot(cwd, (
                fixtures.DiscoveredSkill("review", str(cwd / "review/SKILL.md"), True),
            ), ())

        with patch.object(catalog, "list", side_effect=discover), patch.object(
            self.runtime, "_commit_context_cursor_locked",
        ) as commit:
            with self.assertRaisesRegex(SteerRace, "原生会话身份已变化"):
                await self.runtime.submit(
                    binding=self.binding, cwd=self.selected, input="$review inspect",
                    owner_id="user", origin=object(), skill_names=("review",),
                )
            commit.assert_not_called()
        self.assertEqual(self.codex.resume_calls, [])
        self.assertEqual(self.codex.turn_calls, [])
        self.assertEqual(self.goals.start_calls, [])
        self.assertEqual(self.codex.thread_cwds["native-1"], str(self.main))
        self.assertTrue(self.runtime._accepting)

    async def test_materialized_skill_discovery_keeps_original_admission_and_unlocks(self):
        self.materialize()
        catalog = fixtures.FakeSkillCatalog(fixtures.fake_skills())
        catalog.gate = asyncio.Event()
        self.runtime._skill_catalog = catalog
        admission = await self.runtime.capture_submission_admission(self.binding.id)
        delayed = asyncio.create_task(self.runtime.submit(
            binding=self.binding, cwd=self.selected, input="$code-review delayed",
            owner_id="user", origin=object(), skill_names=("code-review",),
            admission=admission,
        ))
        await asyncio.wait_for(catalog.called.wait(), timeout=1)
        # Ordinary input can acquire the lock and consume the exact idle target.
        first = await asyncio.wait_for(self.runtime.submit(
            binding=self.binding, cwd=self.selected, input="intervening input",
            owner_id="user", origin=object(),
        ), timeout=1)
        catalog.gate.set()
        with self.assertRaises(SteerRace):
            await delayed
        self.assertEqual(len(self.codex.turn_calls), 1)
        self.assertEqual(self.codex.handles[0].steers, [])
        self.assertTrue(self.runtime._accepting)
        await self.finish(first)

    async def test_materialized_skill_does_not_reuse_released_subscription(self):
        self.materialize()
        catalog = fixtures.FakeSkillCatalog(fixtures.fake_skills())
        catalog.gate = asyncio.Event()
        self.runtime._skill_catalog = catalog
        delayed = asyncio.create_task(self.runtime.submit(
            binding=self.binding, cwd=self.selected, input="$code-review delayed",
            owner_id="user", origin=object(), skill_names=("code-review",),
        ))
        await asyncio.wait_for(catalog.called.wait(), timeout=1)
        released = await asyncio.wait_for(self.runtime.release_exact(self.binding.id), timeout=1)
        self.assertEqual(released, ReleaseDisposition.RELEASED)
        catalog.gate.set()
        with self.assertRaisesRegex(SteerRace, "订阅已变化"):
            await delayed
        self.assertEqual(len(self.codex.resume_calls), 1)
        self.assertEqual(self.codex.turn_calls, [])
        self.assertTrue(self.runtime._accepting)

    async def test_materialized_skill_failure_leaves_normal_idle_release(self):
        self.materialize()
        self.runtime._skill_catalog = fixtures.FakeSkillCatalog(())
        with self.assertRaises(SkillReferenceError):
            await self.runtime.submit(
                binding=self.binding, cwd=self.selected, input="$missing inspect",
                owner_id="user", origin=object(), skill_names=("missing",),
            )
        self.assertEqual(self.codex.turn_calls, [])
        self.assertTrue(self.runtime._accepting)
        self.assertIsNotNone(self.runtime._subscriptions[self.binding.id].idle_task)

    async def test_cancelled_skill_discovery_leaves_normal_idle_release(self):
        self.materialize()
        catalog = fixtures.FakeSkillCatalog(fixtures.fake_skills())
        catalog.gate = asyncio.Event()
        self.runtime._skill_catalog = catalog
        delayed = asyncio.create_task(self.runtime.submit(
            binding=self.binding, cwd=self.selected, input="$code-review delayed",
            owner_id="user", origin=object(), skill_names=("code-review",),
        ))
        await asyncio.wait_for(catalog.called.wait(), timeout=1)
        delayed.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await delayed
        self.assertEqual(self.codex.turn_calls, [])
        self.assertTrue(self.runtime._accepting)
        self.assertIsNotNone(self.runtime._subscriptions[self.binding.id].idle_task)

    async def test_resume_unchanged_missing_cwd_is_not_replaced_or_banned(self):
        missing = self.root / "missing"
        self.materialize(missing)
        submission = await self.submit()
        await self.finish(submission)
        self.assertEqual(self.outcomes[-1].cwd, missing)
        self.assertNotIn("cwd", self.codex.turn_calls[-1][2])

    async def test_resume_cwd_read_rejects_invalid_evidence_without_fallback(self):
        self.materialize()
        for native in (
            SimpleNamespace(id="native-source"),
            SimpleNamespace(id="native-source", cwd="relative"),
            SimpleNamespace(id="wrong-thread", cwd=str(self.selected)),
        ):
            with self.subTest(native=native), patch.object(
                fixtures.FakeThread, "read", AsyncMock(return_value=SimpleNamespace(thread=native)),
            ):
                with self.assertRaises(SteerRace):
                    await self.runtime.submit(
                        binding=self.binding, cwd=self.selected, input="prepared", owner_id="user", origin=object(),
                    )
            self.assertEqual(self.codex.turn_calls, [])
            self.assertTrue(self.runtime._accepting)

    async def test_resume_cwd_timeout_retains_subscription_without_starting_turn(self):
        self.materialize()
        self.codex.read_gate = asyncio.Event()
        with patch("netizen_cli.codex_runtime._PREPARED_CWD_READ_TIMEOUT_SECONDS", 0):
            with self.assertRaises(SteerRace):
                await self.runtime.submit(
                    binding=self.binding, cwd=self.selected, input="prepared", owner_id="user", origin=object(),
                )
        self.codex.read_gate.set()
        self.assertEqual(self.codex.turn_calls, [])
        self.assertTrue(self.runtime._accepting)
        self.assertIsNotNone(self.runtime._subscriptions[self.binding.id].idle_task)

    async def test_resume_handoff_obeys_new_worktree_delete_intent(self):
        self.materialize()
        destination = self.managed(self.main)
        inventory = self.store.worktrees.preview_delete(destination.id)
        self.store.worktrees.reserve_delete(destination.id, expected_fingerprint=inventory.fingerprint)
        self.addCleanup(self.store.worktrees.release_delete, destination.id)
        self.codex.thread_cwds["native-source"] = str(self.main)
        with self.assertRaises(WorktreeConflict):
            await self.runtime.submit(
                binding=self.binding, cwd=self.selected, input="prepared", owner_id="user", origin=object(),
            )
        self.assertEqual(self.codex.turn_calls, [])
        self.assertTrue(self.runtime._accepting)
        self.assertEqual(self.store.get(self.binding.id).working_cwd, str(self.selected))

    async def test_external_input_uses_resumed_cwd_without_reinterpreting_message(self):
        self.materialize(self.main)
        message = ExternalMessage("send_message_to_thread", "prepared", "netizen")
        submission = await self.runtime.submit_external(
            binding=self.binding, cwd=self.selected, message=message,
            owner_id="agent", origin=object(),
        )
        self.assertEqual(self.codex.turn_calls[0][1], message)
        self.assertEqual(len(self.codex.resume_calls), 1)
        await self.finish(submission)
        self.assertEqual(self.outcomes[-1].cwd, self.main)
        self.assertTrue(self.runtime._accepting)

    async def test_active_steer_keeps_exact_turn_without_resume_cwd_read(self):
        self.materialize()
        first = await self.submit()
        resume_count, read_count = len(self.codex.resume_calls), len(self.codex.read_calls)
        steered = await self.runtime.submit(
            binding=self.binding, input="additional input", owner_id="user", origin=object(),
        )
        self.assertEqual(steered.disposition, SubmitDisposition.STEERED)
        self.assertEqual(steered.turn_id, first.turn_id)
        self.assertEqual(len(self.codex.resume_calls), resume_count)
        self.assertEqual(len(self.codex.read_calls), read_count)
        await self.finish(first)

    async def test_active_steer_missing_cwd_never_falls_back_to_caller_or_project(self):
        first = await self.submit()
        active = self.runtime._active[self.binding.id]
        active.cwd = None
        read_count, resume_count = len(self.codex.read_calls), len(self.codex.resume_calls)
        for skills in ((), ("code-review",)):
            with self.subTest(skills=skills):
                with self.assertRaises(SteerRace):
                    await self.runtime.submit(
                        binding=self.binding, cwd=self.main, input="inspect",
                        owner_id="user", origin=object(), skill_names=skills,
                    )
        self.assertEqual(self.codex.handles[0].steers, [])
        self.assertEqual(len(self.codex.read_calls), read_count)
        self.assertEqual(len(self.codex.resume_calls), resume_count)
        self.assertEqual(len(self.codex.turn_calls), 1)
        self.assertTrue(self.runtime._accepting)
        active.cwd = self.selected
        await self.finish(first)

    async def test_active_skill_steer_uses_accepted_cwd_without_resume(self):
        self.materialize()
        first = await self.submit()
        catalog = fixtures.FakeSkillCatalog(fixtures.fake_skills())
        self.runtime._skill_catalog = catalog
        resume_count, read_count = len(self.codex.resume_calls), len(self.codex.read_calls)
        steered = await self.runtime.submit(
            binding=self.binding, cwd=self.main, input="$code-review inspect",
            owner_id="user", origin=object(), skill_names=("code-review",),
        )
        self.assertEqual(steered.turn_id, first.turn_id)
        self.assertEqual(catalog.calls, [(self.selected, True)])
        self.assertEqual(len(self.codex.resume_calls), resume_count)
        self.assertEqual(len(self.codex.read_calls), read_count)
        await self.finish(first)

    async def test_terminal_read_does_not_replace_accepted_turn_directory(self):
        self.materialize()
        submission = await self.submit()
        self.codex.thread_cwds["native-source"] = str(self.main)
        await self.finish(submission)
        self.assertEqual(self.outcomes[-1].cwd, self.selected)

    async def test_terminal_read_cannot_fill_missing_accepted_turn_directory(self):
        self.materialize()
        submission = await self.submit()
        self.runtime._active[self.binding.id].cwd = None
        self.codex.thread_cwds["native-source"] = str(self.selected)
        await self.finish(submission)
        self.assertIsNone(self.outcomes[-1].cwd)
        self.assertEqual(self.outcomes[-1].status, "completed")

    async def test_observed_native_move_updates_managed_relation_and_delete_usage(self):
        first = self.managed(self.selected)
        other = self.root / "other-worktree"
        other.mkdir()
        second = self.managed(other)
        self.materialize()
        await self.runtime.native_thread_cwd(self.binding.id)
        self.assertEqual(self.store.get(self.binding.id).worktree_id, first.id)
        self.codex.thread_cwds["native-source"] = str(other)
        submission = await self.submit()
        current = self.store.get(self.binding.id)
        self.assertEqual((current.working_cwd, current.worktree_id), (str(other), second.id))
        inventory = self.store.worktrees.preview_delete(second.id)
        self.assertIn(self.binding.id, [item.id for item in inventory.bindings])
        self.assertEqual(await self.runtime.worktree_usage(self.binding.id), {"cwd": str(other), "status": "active"})
        await self.finish(submission)

    async def test_target_delete_intent_blocks_observed_native_move_before_start(self):
        self.managed(self.selected)
        other = self.root / "other-worktree"
        other.mkdir()
        second = self.managed(other)
        self.materialize()
        await self.runtime.native_thread_cwd(self.binding.id)
        self.codex.thread_cwds["native-source"] = str(other)
        inventory = self.store.worktrees.preview_delete(second.id)
        self.store.worktrees.reserve_delete(second.id, expected_fingerprint=inventory.fingerprint)
        self.addCleanup(self.store.worktrees.release_delete, second.id)
        with self.assertRaises(WorktreeConflict):
            await self.submit()
        self.assertEqual(self.codex.resume_calls, [])
        self.assertEqual(self.codex.turn_calls, [])
        # The deletion's own readonly usage check must not trip its intent.
        self.assertEqual(await self.runtime.worktree_usage(self.binding.id), {"cwd": str(other), "status": "idle"})

    async def test_goal_first_thread_uses_lazy_selection_and_retains_native_cwd(self):
        result = await self.runtime.start_goal(
            binding=self.binding, objective="work", owner_id="user", origin=object(),
        )
        result.release_receipt_attempt()
        self.assertEqual(self.codex.start_kwargs[0]["cwd"], str(self.selected))
        self.assertEqual(self.codex.resume_calls, [])
        self.assertEqual(self.codex.read_calls, [("native-1", False)])
        self.assertEqual(self.runtime._goals[self.binding.id].cwd, self.selected)

    async def test_goal_creation_adopts_resumed_cwd_and_continues_objective(self):
        first = await self.submit()
        await self.finish(first)
        self.codex.thread_cwds["native-1"] = str(self.main)
        result = await self.runtime.start_goal(
            binding=self.store.get(self.binding.id),
            objective="prepared goal", owner_id="user", origin=object(),
        )
        result.release_receipt_attempt()
        self.assertEqual(self.goals.start_calls, [("native-1", "prepared goal")])
        self.assertEqual(self.runtime._goals[self.binding.id].cwd, self.main)
        self.assertTrue(self.runtime._accepting)
        self.assertEqual(self.store.get(self.binding.id).working_cwd, str(self.main))
        self.assertEqual(self.codex.resume_calls, [("native-1", {"include_turns": False})])

    async def test_goal_creation_cwd_read_failure_does_not_start_goal(self):
        self.materialize()
        self.codex.read_errors.append(RuntimeError("read unavailable"))
        with self.assertRaises(GoalControlError):
            await self.runtime.start_goal(
                binding=self.binding, cwd=self.selected, objective="prepared goal", owner_id="user", origin=object(),
            )
        self.assertEqual(self.goals.start_calls, [])
        self.assertIsNone(self.runtime.active_goal(self.binding.id))
        self.assertTrue(self.runtime._accepting)

    async def test_goal_skill_steer_uses_accepted_cwd_without_resume(self):
        started = await self.runtime.start_goal(
            binding=self.binding, cwd=self.selected, objective="work",
            owner_id="user", origin=object(),
        )
        started.release_receipt_attempt()
        catalog = fixtures.FakeSkillCatalog(fixtures.fake_skills())
        self.runtime._skill_catalog = catalog
        resume_count, read_count = len(self.codex.resume_calls), len(self.codex.read_calls)
        handle = self.goals.handles[0]
        handle.rollover("physical-turn-2")
        steered = await self.runtime.submit(
            binding=self.store.get(self.binding.id),
            input="$code-review inspect", owner_id="user", origin=object(),
            skill_names=("code-review",),
        )
        self.assertEqual(steered.turn_id, "physical-turn-2")
        self.assertEqual(catalog.calls, [(self.selected, True)])
        self.assertEqual(len(self.codex.resume_calls), resume_count)
        self.assertEqual(len(self.codex.read_calls), read_count)
        self.assertEqual(len(handle.steers), 1)
        self.assertEqual(self.codex.turn_calls, [])
        handle.finish(response="done")
        self.assertTrue(await self.runtime.wait_idle(timeout=1))

    async def test_goal_steer_missing_cwd_never_falls_back_to_caller_or_project(self):
        started = await self.runtime.start_goal(
            binding=self.binding, objective="work", owner_id="user", origin=object(),
        )
        started.release_receipt_attempt()
        active = self.runtime._goals[self.binding.id]
        active.cwd = None
        read_count = len(self.codex.read_calls)
        for skills in ((), ("code-review",)):
            with self.subTest(skills=skills):
                with self.assertRaises(SteerRace):
                    await self.runtime.submit(
                        binding=self.binding, cwd=self.main, input="inspect",
                        owner_id="user", origin=object(), skill_names=skills,
                    )
        self.assertEqual(self.goals.handles[0].steers, [])
        self.assertEqual(self.codex.resume_calls, [])
        self.assertEqual(len(self.codex.read_calls), read_count)
        self.assertEqual(self.codex.turn_calls, [])
        self.assertTrue(self.runtime._accepting)
        active.cwd = self.selected
        self.goals.handles[0].finish(response="done")
        self.assertTrue(await self.runtime.wait_idle(timeout=1))

    async def test_goal_cold_resume_uses_post_resume_cwd_without_prepared_input(self):
        first = await self.submit()
        await self.finish(first)
        self.goals.persisted = fixtures.goal_snapshot(GoalStatus.PAUSED)
        original_resume = self.codex.thread_resume

        async def moved_resume(thread_id, **kwargs):
            self.codex.thread_cwds[thread_id] = str(self.main)
            return await original_resume(thread_id, **kwargs)

        with patch.object(self.codex, "thread_resume", side_effect=moved_resume):
            resumed = await self.runtime.resume_goal(
                binding=self.store.get(self.binding.id), owner_id="user", origin=object(),
            )
        resumed.release_receipt_attempt()
        self.assertEqual(self.runtime._goals[self.binding.id].cwd, self.main)
        self.assertEqual(self.store.get(self.binding.id).working_cwd, str(self.main))
        self.assertEqual(self.goals.resume_calls, ["native-1"])
        self.assertEqual(self.codex.resume_calls[-1], ("native-1", {"include_turns": False}))

    async def test_goal_cold_resume_checks_actual_worktree_delete_intent(self):
        first = await self.submit()
        await self.finish(first)
        self.goals.persisted = fixtures.goal_snapshot(GoalStatus.PAUSED)
        destination = self.managed(self.main)
        inventory = self.store.worktrees.preview_delete(destination.id)
        self.store.worktrees.reserve_delete(destination.id, expected_fingerprint=inventory.fingerprint)
        self.addCleanup(self.store.worktrees.release_delete, destination.id)
        self.codex.thread_cwds["native-1"] = str(self.main)
        with self.assertRaises(WorktreeConflict):
            await self.runtime.resume_goal(
                binding=self.store.get(self.binding.id), owner_id="user", origin=object(),
            )
        self.assertEqual(self.goals.resume_calls, [])
        self.assertIsNone(self.runtime.active_goal(self.binding.id))
        self.assertTrue(self.runtime._accepting)

    async def test_side_snapshot_uses_native_fork_directory_not_callers_default(self):
        native_cwd = self.root / "native-cwd-alias"
        native_cwd.symlink_to(self.selected, target_is_directory=True)
        self.materialize(native_cwd)
        snapshot = await self.runtime.create_side(
            side_id="side", binding=self.binding, cwd=self.main, creator_id="user",
        )
        self.assertEqual(snapshot.cwd, native_cwd)
        self.assertNotIn("cwd", self.codex.fork_calls[0][1])

    async def test_worktree_side_usage_keeps_frozen_directory_after_parent_removal(self):
        self.materialize()
        snapshot = await self.runtime.create_side(
            side_id="side", binding=self.binding, cwd=self.main, creator_id="user",
        )
        self.codex.thread_cwds["native-source"] = str(self.main)
        await self.runtime.native_thread_cwd(self.binding.id)
        self.store.delete_binding(self.binding.id)
        self.assertEqual(self.runtime.worktree_side_snapshots(self.selected), (snapshot,))
        self.assertEqual(self.runtime.worktree_side_snapshots(self.main), ())

    async def test_side_in_worktree_subdirectory_obeys_root_delete_intent(self):
        resource = self.managed(self.selected)
        nested = self.selected / "nested"
        nested.mkdir()
        self.materialize(nested)
        await self.runtime.create_side(side_id="side", binding=self.binding, cwd=nested, creator_id="user")
        inventory = self.store.worktrees.preview_delete(resource.id)
        self.store.worktrees.reserve_delete(resource.id, expected_fingerprint=inventory.fingerprint)
        self.addCleanup(self.store.worktrees.release_delete, resource.id)
        with self.assertRaises(WorktreeConflict):
            await self.runtime.capture_side_submission_admission("side")

    async def test_delete_usage_requires_native_idle_and_no_background_terminal(self):
        self.materialize()
        self.assertEqual(await self.runtime.worktree_usage(self.binding.id), {"cwd": str(self.selected), "status": "idle"})
        self.inspector.results.append(True)
        self.assertEqual((await self.runtime.worktree_usage(self.binding.id))["status"], "active")
        self.codex.read_statuses.append("active")
        self.assertEqual((await self.runtime.worktree_usage(self.binding.id))["status"], "active")
        self.codex.read_errors.append(RuntimeError("read unavailable"))
        self.assertEqual(await self.runtime.worktree_usage(self.binding.id), {"cwd": None, "status": "unknown"})
        self.assertEqual(self.codex.resume_calls, [])
        self.assertEqual(self.side.unsubscribe_calls, [])

    async def test_delete_usage_waits_for_exact_inflight_admission(self):
        self.materialize()
        self.codex.resume_gate = asyncio.Event()
        submission_task = asyncio.create_task(self.submit())
        for _ in range(100):
            if self.codex.resume_calls:
                break
            await asyncio.sleep(0)
        self.assertTrue(self.codex.resume_calls)
        usage_task = asyncio.create_task(self.runtime.worktree_usage(self.binding.id))
        await asyncio.sleep(0)
        self.assertFalse(usage_task.done())
        self.codex.resume_gate.set()
        submission = await submission_task
        self.assertEqual((await usage_task)["status"], "active")
        await self.finish(submission)

    async def test_short_delete_intent_blocks_native_start(self):
        with patch.object(self.store.worktrees, "require_binding_not_deleting", side_effect=WorktreeConflict("deleting")):
            with self.assertRaises(WorktreeConflict):
                await self.submit()
        self.assertEqual(self.codex.start_kwargs, [])
