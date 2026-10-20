"""Scheduled workspace ownership at real Git and ordinary input boundaries."""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from netizen_cli.schedules.models import ScheduleRule
from netizen_cli.schedules.scheduler import Scheduler
from netizen_cli.worktrees.git import GitError, WorktreeState
from netizen_cli.worktrees.models import WorktreeNotFound
from netizen_cli.worktrees.policy import WorkspacePolicy
from netizen_cli.worktrees.settings import WorktreeSettingsService
from tests.support.channel_fixtures import scheduled_channel_fixture
from tests.support.channel_results import sent_result


class ScheduledWorktreeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = await self.enterAsyncContext(scheduled_channel_fixture())
        self.store = self.f.store.schedules
        self.sequence = 0
        self.git("init", "-q", "--initial-branch=main")
        self.git("commit", "-q", "--allow-empty", "-m", "initial")
        self.f.management.enable_worktrees(settings=WorktreeSettingsService(
            default_root=self.f.project_root / "managed",
        ))
        self.worktrees = self.f.management.worktrees

    def git(self, *args, cwd=None):
        return subprocess.run(
            ["git", "-c", "user.name=Netizen Test", "-c", "user.email=test@example.invalid", *args],
            cwd=cwd or self.f.project, check=True, capture_output=True, text=True, timeout=10,
        ).stdout.strip()

    def claim(self, workspace):
        self.sequence += 1
        plan = self.store.create(
            name="Worktree plan", instructions="检查项目", project_alias="work", app_id="app",
            chat_id="oc_group", workspace=WorkspacePolicy.from_dict(workspace),
            schedule=ScheduleRule("interval", "UTC", every_minutes=1, anchor=100),
            request_id=f"create-{self.sequence}", now=100,
        )
        return self.store.claim_due(plan.plan_id, app_id="app", now=160)

    def topic(self, suffix="one"):
        self.f.channel.send_results.append(sent_result(
            "om_" + suffix, chat_id="oc_group", thread_id="omt_" + suffix, root_id="om_" + suffix,
        ))

    def resources(self):
        return self.f.store.worktrees.list_for_project("work")

    async def dispatch(self, claim):
        await self.f.app.dispatch_scheduled_run(claim)
        return self.store.get_run(claim.run.id)

    async def test_project_mode_keeps_original_directory_without_resource(self):
        claim = self.claim({"mode": "project"})
        self.topic()
        run = await self.dispatch(claim)
        self.assertIsNone(run.worktree_id)
        self.assertEqual(self.f.submissions[0]["cwd"], self.f.project.resolve())
        self.assertEqual(self.resources(), ())

    async def test_existing_selection_is_frozen_and_reaches_binding_and_runtime(self):
        resource = await self.worktrees.create(project_alias="work")
        claim = self.claim({"mode": "existing", "worktree_id": resource["id"]})
        self.store.update(claim.plan.id, expected_revision=1, request_id="change", now=161,
                          changes={"workspace": WorkspacePolicy("new")})
        self.topic()
        run = await self.dispatch(claim)
        binding = self.f.store.get(run.binding_id)
        self.assertEqual((run.worktree_id, binding.worktree_id), (resource["id"], resource["id"]))
        self.assertEqual(binding.working_cwd, resource["cwd"])
        self.assertEqual(self.f.submissions[0]["cwd"], Path(resource["cwd"]))
        self.assertEqual(len(self.resources()), 1)

    async def test_each_run_creates_detached_checkout_from_current_project_head(self):
        first = self.claim({"mode": "new"})
        self.assertEqual(self.resources(), ())
        for index in range(2):
            claim = first if index == 0 else self.store.claim_due(first.plan.id, app_id="app", now=220)
            self.git("commit", "-q", "--allow-empty", "-m", f"change-{index}")
            expected = self.git("rev-parse", "HEAD")
            self.topic(str(index))
            with patch.object(self.worktrees._git, "inspect", wraps=self.worktrees._git.inspect) as inspect:
                run = await self.dispatch(claim)
            # Creation already verifies its checkout; handoff consumes that receipt.
            inspect.assert_awaited_once()
            record = self.f.store.worktrees.get(run.worktree_id)
            binding = self.f.store.get(run.binding_id)
            self.assertEqual(binding.worktree_id, record.id)
            self.assertEqual(binding.working_cwd, record.cwd)
            self.assertEqual(self.git("rev-parse", "HEAD", cwd=record.cwd), expected)
            self.assertEqual(self.git("branch", "--show-current", cwd=record.cwd), "")
            self.assertEqual(self.f.submissions[-1]["cwd"], Path(record.cwd))
            self.store.release(run.id)
        self.assertEqual(len(self.resources()), 2)
        self.assertEqual(len({run.worktree_id for run in self.store.list_runs(first.plan.id)}), 2)
        self.assertEqual(self.git("branch", "--show-current"), "main")

    async def test_explicit_baseline_is_used_without_changing_project_checkout(self):
        baseline = self.git("rev-parse", "HEAD")
        self.git("tag", "baseline")
        self.git("commit", "-q", "--allow-empty", "-m", "next")
        project_head = self.git("rev-parse", "HEAD")
        claim = self.claim({"mode": "new", "ref": "refs/tags/baseline"})
        self.topic()
        run = await self.dispatch(claim)
        record = self.f.store.worktrees.get(run.worktree_id)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=record.cwd), baseline)
        self.assertEqual(self.git("rev-parse", "HEAD"), project_head)

    async def test_duplicate_dispatch_uses_single_publication_and_resource_owner(self):
        claim = self.claim({"mode": "new"})
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.worktrees._git.create

        async def create(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        self.topic()
        with patch.object(self.worktrees._git, "create", side_effect=create) as create_call:
            first = asyncio.create_task(self.dispatch(claim))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                await self.dispatch(claim)
            finally:
                release.set()
                await first
        self.assertEqual(create_call.call_count, 1)
        self.assertEqual(len(self.f.channel.send_calls), 1)
        self.assertEqual(len(self.resources()), 1)
        self.assertEqual(len(self.f.submissions), 1)

    async def test_missing_selected_directory_sends_nothing_and_preserves_enabled_plan(self):
        resource = await self.worktrees.create(project_alias="work")
        claim = self.claim({"mode": "existing", "worktree_id": resource["id"]})
        shutil.rmtree(resource["root"])
        run = await self.dispatch(claim)
        self.assertEqual((run.barrier, run.error_code), ("released", "workspace_unavailable"))
        self.assertIsNone(run.binding_id)
        self.assertIsNone(run.root_message_id)
        self.assertEqual(self.f.channel.send_calls, [])
        self.assertEqual(self.f.submissions, [])
        plan = self.store.get(claim.plan.id)
        self.assertTrue(plan.enabled)
        self.assertEqual(plan.workspace.worktree_id, resource["id"])
        self.assertEqual(plan.next_due_at, 220)

    async def test_unconfirmed_or_replaced_resource_never_falls_back(self):
        resource = await self.worktrees.create(project_alias="work")
        for state in ("unknown", "replaced"):
            with self.subTest(state=state):
                claim = self.claim({"mode": "existing", "worktree_id": resource["id"]})
                with patch.object(self.worktrees, "_state", AsyncMock(return_value=WorktreeState(state))):
                    run = await self.dispatch(claim)
                self.assertEqual(run.error_code, "workspace_unavailable")
        self.assertEqual(self.f.channel.send_calls, [])
        self.assertEqual(self.f.submissions, [])

    async def test_confirmed_resource_deletion_preserves_plan_and_exact_reference(self):
        resource = await self.worktrees.create(project_alias="work")
        claim = self.claim({"mode": "existing", "worktree_id": resource["id"]})
        with patch.object(self.f.runtime, "worktree_side_snapshots", return_value=(), create=True):
            preview = await self.worktrees.preview_delete(resource["id"])
            await self.worktrees.delete(resource["id"], expected_fingerprint=preview["fingerprint"])
        with self.assertRaises(WorktreeNotFound):
            self.f.store.worktrees.get(resource["id"])
        run = await self.dispatch(claim)
        self.assertEqual(run.error_code, "workspace_unavailable")
        self.assertEqual(self.store.get(claim.plan.id).workspace.worktree_id, resource["id"])
        self.assertTrue(self.store.get(claim.plan.id).enabled)
        self.assertEqual(self.f.channel.send_calls, [])
        self.assertEqual(self.f.submissions, [])

    async def test_deletion_after_preflight_keeps_real_topic_and_refuses_binding(self):
        resource = await self.worktrees.create(project_alias="work")
        claim = self.claim({"mode": "existing", "worktree_id": resource["id"]})
        select = self.worktrees.select
        selections = 0

        async def concurrent_delete(**kwargs):
            nonlocal selections
            selections += 1
            if selections == 2:
                shutil.rmtree(resource["root"])
            return await select(**kwargs)

        self.topic()
        with patch.object(self.worktrees, "select", side_effect=concurrent_delete):
            run = await self.dispatch(claim)
        self.assertEqual(run.error_code, "workspace_unavailable")
        self.assertEqual(run.root_message_id, "om_one")
        self.assertEqual(run.worktree_id, resource["id"])
        self.assertIsNone(run.binding_id)
        self.assertEqual(self.f.submissions, [])
        self.assertIn(resource["id"], self.f.channel.replies[-1][1])

    async def test_delete_reservation_before_binding_is_rejected_for_existing_and_new(self):
        create_binding = self.f.store.create_scheduled_binding

        def deleting_before_bind(**kwargs):
            record = self.f.store.worktrees.get(kwargs["worktree_id"])
            self.assertEqual(kwargs["working_cwd"], record.cwd)
            snapshot = self.f.store.worktrees.preview_delete(record.id)
            self.f.store.worktrees.reserve_delete(record.id, expected_fingerprint=snapshot.fingerprint)
            return create_binding(**kwargs)

        for mode in ("existing", "new"):
            with self.subTest(mode=mode):
                policy = {"mode": mode}
                if mode == "existing":
                    resource = await self.worktrees.create(project_alias="work")
                    policy["worktree_id"] = resource["id"]
                claim = self.claim(policy)
                self.topic(mode)
                try:
                    with patch.object(self.f.store, "create_scheduled_binding", side_effect=deleting_before_bind):
                        run = await self.dispatch(claim)
                finally:
                    run = self.store.get_run(claim.run.id)
                    if run.worktree_id is not None:
                        self.f.store.worktrees.release_delete(run.worktree_id)
                self.assertEqual(run.error_code, "workspace_unavailable")
                self.assertEqual(run.root_message_id, "om_" + mode)
                self.assertIsNone(run.binding_id)
                self.assertEqual(self.f.submissions, [])

    async def test_creation_failure_preserves_atomic_run_receipt_and_unknown_resource(self):
        claim = self.claim({"mode": "new"})
        self.topic()
        observed = []

        async def fail(*args, **kwargs):
            run = self.store.get_run(claim.run.id)
            record = self.f.store.worktrees.get(run.worktree_id)
            observed.append(record)
            self.assertEqual(record.creation_state, "creating")
            self.assertEqual(run.origin_message_id, "om_one")
            raise GitError("create", "unknown failure", outcome="unknown")

        with patch.object(self.worktrees._git, "create", side_effect=fail):
            run = await self.dispatch(claim)
        self.assertEqual((run.barrier, run.error_code), ("released", "workspace_creation_unknown"))
        self.assertEqual(run.worktree_id, observed[0].id)
        self.assertEqual(self.f.store.worktrees.get(run.worktree_id).creation_state, "unknown")
        self.assertEqual(self.f.submissions, [])
        self.assertIn(run.worktree_id, self.f.channel.replies[-1][1])

    async def test_cancellation_during_git_is_not_unknown_feishu_or_native_execution(self):
        claim = self.claim({"mode": "new"})
        self.topic()
        entered = asyncio.Event()

        async def create(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        with patch.object(self.worktrees._git, "create", side_effect=create):
            task = asyncio.create_task(self.dispatch(claim))
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        run = self.store.get_run(claim.run.id)
        self.assertEqual((run.barrier, run.error_code), ("released", "workspace_creation_unknown"))
        self.assertEqual(self.f.store.worktrees.get(run.worktree_id).creation_state, "unknown")
        self.assertEqual(self.f.submissions, [])
        self.assertEqual(run.origin_message_id, "om_one")

    async def test_cancel_before_resource_registration_does_not_invent_git_uncertainty(self):
        claim = self.claim({"mode": "new"})
        self.topic()
        entered = asyncio.Event()

        async def not_started(**kwargs):
            entered.set()
            await asyncio.Event().wait()

        with patch.object(self.worktrees, "create", side_effect=not_started):
            task = asyncio.create_task(self.dispatch(claim))
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        run = self.store.get_run(claim.run.id)
        self.assertEqual(run.error_code, "workspace_creation_failed")
        self.assertIsNone(run.worktree_id)
        self.assertEqual(self.resources(), ())

    async def test_scheduler_timeout_preserves_created_checkout_and_confirmed_topic(self):
        claim = self.claim({"mode": "new"})
        self.topic()
        entered = asyncio.Event()
        git_create = self.worktrees._git.create
        real_timeout = asyncio.timeout
        dispatch_timeout = real_timeout(None)
        budget = 0.125

        async def created_before_receipt(*args, **kwargs):
            created = await git_create(*args, **kwargs)
            entered.set()
            await asyncio.Event().wait()
            return created

        def timeout(seconds):
            # Arm the real Scheduler timeout at the Git handoff, so slow
            # subprocess startup cannot move this test's cancellation point.
            return dispatch_timeout if seconds == budget else real_timeout(seconds)

        scheduler = Scheduler(self.f.store, self.f.runtime, "app",
                              self.f.app.dispatch_scheduled_run, wall_clock=lambda: 160)
        try:
            with patch("netizen_cli.schedules.scheduler.WORKTREE_DISPATCH_TIMEOUT_SECONDS", budget), \
                    patch("netizen_cli.schedules.scheduler.asyncio.timeout", side_effect=timeout), \
                    patch.object(self.worktrees._git, "create", side_effect=created_before_receipt):
                dispatch = asyncio.create_task(scheduler._dispatch_once(claim))
                try:
                    await asyncio.wait_for(entered.wait(), 5)
                    dispatch_timeout.reschedule(asyncio.get_running_loop().time())
                    await asyncio.wait_for(dispatch, 5)
                finally:
                    if not dispatch.done():
                        dispatch.cancel()
                    await asyncio.gather(dispatch, return_exceptions=True)
            self.assertTrue(dispatch_timeout.expired())
            run = self.store.get_run(claim.run.id)
            self.assertEqual((run.barrier, run.error_code), ("released", "workspace_creation_unknown"))
            self.assertEqual((run.root_message_id, run.origin_message_id, run.topic_id),
                             ("om_one", "om_one", "omt_one"))
            record = self.f.store.worktrees.get(run.worktree_id)
            self.assertEqual(record.creation_state, "unknown")
            self.assertTrue(Path(record.cwd).is_dir())
            self.assertIsNone(run.binding_id)
            self.assertIsNone(run.initial_turn_id)
            self.assertEqual(self.f.submissions, [])
        finally:
            await scheduler.close()

    async def test_scheduler_shutdown_settles_before_git_cancellation_cleanup(self):
        scheduler = Scheduler(self.f.store, self.f.runtime, "app",
                              self.f.app.dispatch_scheduled_run, wall_clock=lambda: 160)
        await scheduler.recover()
        scheduler.start()
        claim = self.claim({"mode": "new"})
        self.topic()
        entered, cancelling, release_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()
        git_create = self.worktrees._git.create

        async def created_before_receipt(*args, **kwargs):
            created = await git_create(*args, **kwargs)
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelling.set()
                await release_cleanup.wait()
                raise
            return created

        drain = None
        dispatch = None
        try:
            with patch.object(self.worktrees._git, "create", side_effect=created_before_receipt):
                scheduler._dispatch_claim(claim)
                dispatch = scheduler._dispatches[claim.run.id][1]
                await asyncio.wait_for(entered.wait(), 5)
                await scheduler.close()
                drain = asyncio.create_task(scheduler.drain(asyncio.get_running_loop().time()))
                await asyncio.wait_for(cancelling.wait(), 5)
                # Shutdown settles the Run before the cancelled Git coroutine
                # has returned to WorktreeService's cleanup.
                run = self.store.get_run(claim.run.id)
                self.assertEqual((run.barrier, run.error_code), ("released", "workspace_creation_unknown"))
                self.assertEqual(self.f.store.worktrees.get(run.worktree_id).creation_state, "creating")
                release_cleanup.set()
                self.assertFalse(await asyncio.wait_for(drain, 5))
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(dispatch, 5)
            run = self.store.get_run(claim.run.id)
            self.assertEqual((run.barrier, run.error_code), ("released", "workspace_creation_unknown"))
            self.assertEqual((run.root_message_id, run.origin_message_id, run.topic_id),
                             ("om_one", "om_one", "omt_one"))
            record = self.f.store.worktrees.get(run.worktree_id)
            self.assertEqual(record.creation_state, "unknown")
            self.assertTrue(Path(record.cwd).is_dir())
            self.assertIsNone(run.binding_id)
            self.assertIsNone(run.initial_turn_id)
            self.assertEqual(self.f.submissions, [])
        finally:
            release_cleanup.set()
            await scheduler.close()
            await scheduler.drain(asyncio.get_running_loop().time() + 5)
            await asyncio.gather(*(task for task in (dispatch, drain) if task is not None),
                                 return_exceptions=True)

    async def test_proven_unstarted_git_discards_only_reservation_and_keeps_historic_receipt(self):
        claim = self.claim({"mode": "new"})
        self.topic()
        with patch.object(self.worktrees._git, "create", side_effect=GitError(
            "create", "invalid baseline", outcome="not_started",
        )):
            run = await self.dispatch(claim)
        self.assertEqual(run.error_code, "workspace_creation_failed")
        self.assertIsNotNone(run.worktree_id)
        self.assertEqual(self.resources(), ())
        self.assertEqual(self.f.submissions, [])

    async def test_ready_checkout_survives_binding_failure_and_plan_deletion(self):
        claim = self.claim({"mode": "new"})
        self.topic()
        with patch.object(self.f.store, "create_scheduled_binding", side_effect=RuntimeError("handoff failed")):
            run = await self.dispatch(claim)
        self.assertEqual(run.error_code, "workspace_handoff_failed")
        self.store.delete(claim.plan.id, expected_revision=1, request_id="delete", now=170)
        record = self.f.store.worktrees.get(run.worktree_id)
        self.assertTrue(Path(record.cwd).is_dir())
        self.assertEqual(record.creation_state, "ready")
        self.assertEqual(self.f.submissions, [])

    async def test_run_receipt_failure_rolls_back_resource_before_git(self):
        claim = self.claim({"mode": "new"})
        self.topic()
        original = self.store._set_run

        def reject_receipt(run_id, **changes):
            if "worktree_id" in changes:
                raise RuntimeError("receipt storage failed")
            return original(run_id, **changes)

        with patch.object(self.store, "_set_run", side_effect=reject_receipt), \
                patch.object(self.worktrees._git, "create", AsyncMock()) as create:
            run = await self.dispatch(claim)
        create.assert_not_called()
        self.assertIsNone(run.worktree_id)
        self.assertEqual(self.resources(), ())
        self.assertEqual(run.error_code, "workspace_creation_failed")

    async def test_deleted_historical_checkout_does_not_block_future_new_run(self):
        first = self.claim({"mode": "new"})
        self.topic("first")
        first_run = await self.dispatch(first)
        self.store.release(first_run.id)
        record = self.f.store.worktrees.get(first_run.worktree_id)
        self.git("worktree", "remove", record.root)
        # Removing the exact managed registration preserves the Plan and receipt.
        with patch.object(self.f.runtime, "worktree_side_snapshots", return_value=(), create=True), \
                patch.object(self.f.runtime, "worktree_usage", AsyncMock(return_value={
                    "cwd": record.cwd, "status": "idle",
                }), create=True):
            preview = await self.worktrees.preview_delete(record.id)
            await self.worktrees.delete(record.id, expected_fingerprint=preview["fingerprint"])
        second = self.store.claim_due(first.plan.id, app_id="app", now=220)
        self.topic("second")
        second_run = await self.dispatch(second)
        self.assertNotEqual(second_run.worktree_id, first_run.worktree_id)
        self.assertEqual(len(self.f.submissions), 2)
        self.assertTrue(self.store.get(first.plan.id).enabled)

    async def test_recovery_retains_resource_receipt_without_replaying_git_or_messages(self):
        for ready in (False, True):
            with self.subTest(ready=ready):
                claim = self.claim({"mode": "new"})
                self.store.begin_publication(claim.run.id)
                self.store.set_run(claim.run.id, root_message_id="root" + claim.run.id,
                                   origin_message_id="seed" + claim.run.id, topic_id="topic" + claim.run.id)
                if ready:
                    resource = await self.worktrees.create(project_alias="work", scheduled_run_id=claim.run.id)
                else:
                    record = self.f.store.worktrees.register(
                        project_alias="work", expected_project_revision=self.f.store.get_project("work").revision,
                        root=str(self.f.project / "lost"), cwd=str(self.f.project / "lost"),
                        common_dir=str(self.f.project / ".git"), scheduled_run_id=claim.run.id,
                    )
                    resource = {"id": record.id}
                scheduler = Scheduler(self.f.store, self.f.runtime, "app", self.f.app.dispatch_scheduled_run,
                                      wall_clock=lambda: 160)
                try:
                    with patch.object(self.worktrees._git, "create", AsyncMock()) as create:
                        await scheduler.recover()
                    create.assert_not_called()
                    run = self.store.get_run(claim.run.id)
                    self.assertEqual(run.worktree_id, resource["id"])
                    self.assertEqual(run.barrier, "released")
                    self.assertEqual(run.error_code, "workspace_handoff_failed" if ready else "workspace_creation_unknown")
                finally:
                    await scheduler.close()
        self.assertEqual(self.f.channel.send_calls, [])
        self.assertEqual(self.f.submissions, [])
