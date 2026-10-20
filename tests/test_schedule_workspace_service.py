from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from netizen_cli.worktrees.errors import WorktreeError
from netizen_cli.worktrees.policy import WorkspacePolicy
from tests import test_schedule_service as support
from tests import test_schedule_run_now as manual_support


class ScheduleWorkspaceServiceTest(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = support.ScheduleServiceTest.asyncTearDown
    new_service = support.ScheduleServiceTest.new_service
    binding = support.ScheduleServiceTest.binding
    create_request = support.ScheduleServiceTest.create_request
    create = support.ScheduleServiceTest.create

    async def asyncSetUp(self):
        await support.ScheduleServiceTest.asyncSetUp(self)
        self.worktrees = SimpleNamespace(
            validate_policy=AsyncMock(return_value=None),
            options=AsyncMock(return_value={
                "projectAlias": "p", "projectRevision": 1,
                "isGit": True, "items": [], "branches": [],
            }),
        )
        self.service._worktrees = lambda: self.worktrees

    def resource(self, identifier="workspace"):
        record = self.store.worktrees.register(
            project_alias="p", expected_project_revision=1,
            root=str(self.cwd / identifier), cwd=str(self.cwd / identifier),
            common_dir=str(self.cwd / ".git"), worktree_id=identifier,
        )
        return self.store.worktrees.mark_ready(
            record.id, git_dir=str(self.cwd / ".git" / "worktrees" / identifier),
            identity_token="test-identity",
        )

    async def update(self, plan, **fields):
        self.request_sequence += 1
        return await self.service.manage({
            "mode": "update", "plan_id": plan["id"],
            "expected_revision": plan["revision"], "request_id": f"edit-{self.request_sequence}",
            **fields,
        })

    async def test_creation_defaults_to_project_and_updates_preserve_or_replace_policy(self):
        plan = await self.create()
        self.assertEqual(plan["workspace"], {"mode": "project"})
        self.worktrees.validate_policy.assert_not_awaited()
        changed = await self.update(plan, workspace={"mode": "new", "ref": "refs/heads/main"})
        self.assertTrue(changed["ok"], changed)
        self.worktrees.validate_policy.assert_awaited_once()
        plan = changed["plan"]
        self.assertEqual(plan["workspace"], {"mode": "new", "ref": "refs/heads/main"})
        unchanged = await self.update(plan, name="Only name changed")
        self.assertTrue(unchanged["ok"], unchanged)
        self.assertEqual(unchanged["plan"]["workspace"], plan["workspace"])
        replaced = await self.update(unchanged["plan"], workspace={"mode": "project"})
        self.assertTrue(replaced["ok"], replaced)
        self.assertEqual(replaced["plan"]["workspace"], {"mode": "project"})

    async def test_workspace_shape_and_binding_overrides_fail_before_mutation(self):
        for policy in (None, {}, {"mode": "project", "ref": "HEAD"}, {"mode": "existing"},
                       {"mode": "new", "cwd": "/tmp"}, {"mode": "new", "ref": None}):
            with self.subTest(policy=policy):
                result = await self.service.manage(self.create_request(workspace=policy), native_thread_id="native-a")
                self.assertEqual(result["error"]["code"], "invalid_workspace")
        for policy in (None, {"mode": "project"}, {"mode": "new"}):
            result = await self.service.manage(
                self.create_request(target_kind="binding", workspace=policy), native_thread_id="native-a",
            )
            self.assertFalse(result["ok"], result)
        plan = await self.create(target_kind="binding")
        self.assertIsNone(plan["workspace"])
        self.assertIsNone(plan["workspace_available"])
        self.assertEqual(plan["effective_workspace"]["source"], "binding")
        self.assertTrue(plan["effective_workspace"]["native_cwd_unresolved"])
        self.worktrees.validate_policy.assert_not_awaited()
        self.assertFalse((await self.update(plan, workspace={"mode": "project"}))["ok"])

    async def test_unavailable_saved_policy_keeps_enabled_and_allows_unrelated_edits(self):
        resource = self.resource()
        policy = {"mode": "existing", "worktree_id": resource.id}
        plan = await self.create(workspace=policy)
        self.worktrees.validate_policy.side_effect = WorktreeError("not_found", "受管工作区登记不存在。")
        viewed = await self.service.manage({"mode": "view", "plan_id": plan["id"]})
        value = viewed["plan"]
        self.assertTrue(value["enabled"])
        self.assertFalse(value["workspace_available"])
        self.assertEqual(value["workspace_error"]["code"], "not_found")
        self.assertEqual(value["blocked_reason"], "workspace_unavailable")
        self.assertEqual(value["suspended_reason"], "workspace_unavailable")
        self.assertFalse(value["can_run_now"])
        changed = await self.update(value, name="Keep resource reference", workspace=policy, enabled=False,
                                    expected_project_revision=1)
        self.assertTrue(changed["ok"], changed)
        self.assertEqual(changed["plan"]["workspace"], policy)
        self.assertFalse(changed["plan"]["enabled"])
        self.assertEqual(changed["plan"]["workspace_error"]["code"], "not_found")
        rejected = await self.update(changed["plan"], workspace={"mode": "new"})
        self.assertEqual(rejected["error"]["code"], "workspace_unavailable")

    async def test_policy_validation_and_save_recheck_exact_resource_admission(self):
        resource = self.resource()

        async def deleting(policy, **kwargs):
            snapshot = self.store.worktrees.preview_delete(resource.id)
            self.store.worktrees.reserve_delete(resource.id, expected_fingerprint=snapshot.fingerprint)

        self.worktrees.validate_policy.side_effect = deleting
        try:
            result = await self.service.manage(
                self.create_request(workspace={"mode": "existing", "worktree_id": resource.id}),
                native_thread_id="native-a",
            )
            self.assertEqual(result["error"]["code"], "workspace_unavailable")
            self.assertEqual(self.store.schedules.list(app_id="app"), ())
        finally:
            self.store.worktrees.release_delete(resource.id)

    async def test_workspace_validation_timeout_is_reported_but_internal_errors_propagate(self):
        self.worktrees.validate_policy.side_effect = TimeoutError()
        result = await self.service.manage(
            self.create_request(workspace={"mode": "new"}), native_thread_id="native-a",
        )
        self.assertEqual(result["error"]["code"], "workspace_unavailable")
        self.assertIn("超时", result["error"]["message"])
        self.worktrees.validate_policy.side_effect = AssertionError("internal validation defect")
        with self.assertRaisesRegex(AssertionError, "internal validation defect"):
            await self.service.manage(
                self.create_request(workspace={"mode": "new"}), native_thread_id="native-a",
            )
        self.assertEqual(self.store.schedules.list(app_id="app"), ())

    async def test_project_change_cannot_keep_a_foreign_worktree(self):
        resource = self.resource()
        plan = await self.create(workspace={"mode": "existing", "worktree_id": resource.id})
        self.store.register_project(alias="other", cwd=str(self.cwd / "other"))
        result = await self.update(plan, project="other")
        self.assertEqual(result["error"]["code"], "workspace_unavailable")
        self.assertEqual(self.store.schedules.get(plan["id"]).project_alias, "p")

    async def test_options_preserve_other_inputs_when_workspace_discovery_fails(self):
        self.worktrees.options.side_effect = WorktreeError("not_git", "该项目不是 Git 仓库。")
        result = await self.service.manage({
            "mode": "options", "project": "p", "expected_project_revision": 1,
        }, native_thread_id="native-a")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["workspace_options_error"]["code"], "not_git")
        self.assertIsNone(result["workspace_options"])
        self.assertEqual(result["source_binding_id"], self.first.id)
        self.assertTrue(result["binding_targets"])
        self.assertIn("session_settings", result)
        self.worktrees.options.assert_awaited_once_with(project_alias="p", expected_project_revision=1)
        self.worktrees.validate_policy.assert_not_awaited()

    async def test_workspace_only_options_do_not_reload_other_form_inputs(self):
        with patch.object(self.chats, "get_chat_info", AsyncMock(side_effect=AssertionError("unexpected chat read"))), \
             patch.object(self.store, "query_bindings", AsyncMock(side_effect=AssertionError("unexpected binding read"))):
            choices, error = await self.service.workspace_catalog_options("p", 1)
        self.assertIsNone(error)
        self.assertEqual(choices["projectAlias"], "p")
        self.assertEqual(self.runtime.catalog_calls, 0)

    async def test_workspace_options_timeout_and_internal_error_keep_other_groups(self):
        self.worktrees.options.side_effect = TimeoutError()
        result = await self.service.manage({"mode": "options", "project": "p"}, native_thread_id="native-a")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["workspace_options_error"]["code"], "workspace_unavailable")
        self.assertIn("超时", result["workspace_options_error"]["message"])
        self.assertTrue(result["binding_targets"])
        self.worktrees.options.side_effect = AssertionError("private exception details")
        with self.assertLogs("netizen_cli.schedules.service", level="ERROR") as captured:
            result = await self.service.manage({"mode": "options", "project": "p"}, native_thread_id="native-a")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["workspace_options_error"]["code"], "internal_error")
        self.assertTrue(result["binding_targets"])
        self.assertIn("session_settings", result)
        self.assertIn("AssertionError", str(captured.output))
        self.assertNotIn("private exception details", str(result) + str(captured.output))

    async def test_public_options_keep_workspace_choices_when_chat_read_fails(self):
        support.ScheduleServiceTest.session_catalog(self)
        self.chats.types["group-a"] = OSError("private transport details")
        for source in ("admin", "mcp"):
            with self.subTest(source=source):
                result = await self.service.manage(
                    {"mode": "options", "project": "p"},
                    native_thread_id="native-a", source=source,
                )
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["chat_info_error"]["code"], "chat_unavailable")
                self.assertEqual(result["workspace_options"]["projectAlias"], "p")
                self.assertIsNone(result["workspace_options_error"])
                self.assertEqual(result["models"][0]["id"], "model-a")
                self.assertTrue(result["binding_targets"])
                self.assertIsNone(result["context_mode_available"])
                self.assertNotIn("private transport details", str(result))
        rejected = await self.service.manage(self.create_request(), native_thread_id="native-a")
        self.assertFalse(rejected["ok"], rejected)
        self.assertEqual(self.store.schedules.list(app_id="app"), ())

    async def test_form_option_groups_start_concurrently_and_preserve_independent_results(self):
        support.ScheduleServiceTest.session_catalog(self)
        entered, release = asyncio.Event(), asyncio.Event()
        started = set()
        self.worktrees.options.side_effect = WorktreeError("unavailable", "工作区暂不可读。")

        def gated(name, function):
            async def read(*args, **kwargs):
                started.add(name)
                if len(started) == 4:
                    entered.set()
                await release.wait()
                return await function(*args, **kwargs)
            return read

        with patch.object(self.chats, "get_chat_info", gated("chat", self.chats.get_chat_info)), \
             patch.object(self.runtime, "model_catalog", gated("models", self.runtime.model_catalog)), \
             patch.object(self.store, "query_bindings", gated("bindings", self.store.query_bindings)), \
             patch.object(self.worktrees, "options", gated("workspaces", self.worktrees.options)):
            pending = asyncio.create_task(self.service.form_options(native_thread_id="native-a", project="p"))
            try:
                await asyncio.wait_for(entered.wait(), 1)
            finally:
                release.set()
                result, catalog = await asyncio.wait_for(pending, 1)
        self.assertEqual(started, {"chat", "models", "bindings", "workspaces"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["models"][0]["id"], "model-a")
        self.assertIsNotNone(catalog)
        self.assertTrue(result["binding_targets"])
        self.assertTrue(result["context_mode_available"])
        self.assertEqual(result["workspace_options_error"]["message"], "工作区暂不可读。")
        self.assertIsNone(result["model_catalog_error"])
        self.assertIsNone(result["chat_info_error"])

    async def test_project_revision_is_checked_before_workspace_query_and_save(self):
        result = await self.service.manage(
            self.create_request(workspace={"mode": "new"}, expected_project_revision=99),
            native_thread_id="native-a",
        )
        self.assertEqual(result["error"]["code"], "project_changed")
        self.worktrees.validate_policy.assert_not_awaited()
        self.assertEqual(self.store.schedules.list(app_id="app"), ())

    async def test_explicit_project_revision_is_honored_for_metadata_only_updates(self):
        plan = await self.create()
        rejected = await self.update(plan, name="Renamed", expected_project_revision=999)
        self.assertEqual(rejected["error"]["code"], "project_changed")
        self.assertEqual(self.store.schedules.get(plan["id"]).revision, plan["revision"])
        self.assertEqual(self.store.schedules.get(plan["id"]).name, plan["name"])
        self.worktrees.validate_policy.assert_not_awaited()
        changed = await self.update(plan, name="Renamed", expected_project_revision=1)
        self.assertTrue(changed["ok"], changed)
        self.assertEqual(changed["plan"]["name"], "Renamed")
        self.worktrees.validate_policy.assert_not_awaited()

    async def test_metadata_update_rechecks_project_revision_after_async_preparation(self):
        plan = await self.create()
        original = self.service._validate_submitted_chat

        async def change_during_chat_read(chat_id):
            kind = await original(chat_id)
            self.store.set_project_enabled(alias="p", enabled=False, expected_revision=1)
            return kind

        with patch.object(self.service, "_validate_submitted_chat", change_during_chat_read):
            result = await self.update(plan, name="Renamed", session_settings={}, expected_project_revision=1)
        self.assertEqual(result["error"]["code"], "project_changed")
        self.assertEqual(self.store.schedules.get(plan["id"]).revision, 1)

    async def test_metadata_update_project_revision_is_checked_in_store_mutation(self):
        plan = await self.create()
        original = self.store.schedules.update

        def change_before_mutation(*args, **kwargs):
            self.store.set_project_enabled(alias="p", enabled=False, expected_revision=1)
            return original(*args, **kwargs)

        with patch.object(self.store.schedules, "update", change_before_mutation):
            result = await self.update(plan, name="Renamed", expected_project_revision=1)
        self.assertEqual(result["error"]["code"], "project_changed")
        self.assertEqual(self.store.schedules.get(plan["id"]).revision, 1)

    async def test_list_deduplicates_policy_reads_and_preserves_existing_block_reason(self):
        for index in range(4):
            await self.create(workspace={"mode": "new", "ref": f"ref-{index}"}, name=f"Plan {index}")
        self.worktrees.validate_policy.reset_mock()
        self.worktrees.validate_policy.side_effect = WorktreeError("unavailable", "状态未取得。")
        response = await self.service.manage({"mode": "list", "all": True})
        self.assertEqual(len(response["plans"]), 4)
        self.worktrees.validate_policy.assert_awaited_once_with(WorkspacePolicy("new"), project_alias="p")
        self.assertTrue(all(not plan["can_run_now"] for plan in response["plans"]))
        self.store.set_project_enabled(alias="p", enabled=False, expected_revision=1)
        response = await self.service.manage({"mode": "list", "all": True})
        self.assertTrue(all(plan["blocked_reason"] == "project_disabled" for plan in response["plans"]))
        self.assertTrue(all(plan["suspended_reason"] == "project_disabled" for plan in response["plans"]))

    async def test_list_workspace_reads_have_one_bounded_budget(self):
        for index in range(6):
            resource = self.resource(f"workspace-{index}")
            await self.create(workspace={"mode": "existing", "worktree_id": resource.id})
        entered = asyncio.Event()
        active, maximum = 0, 0

        async def blocked(*args, **kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                active -= 1

        self.worktrees.validate_policy.side_effect = blocked
        with patch("netizen_cli.schedules.service._READ_TIMEOUT_SECONDS", 0.05):
            task = asyncio.create_task(self.service.manage({"mode": "list", "all": True}))
            await asyncio.wait_for(entered.wait(), 1)
            response = await asyncio.wait_for(task, 1)
        self.assertLessEqual(maximum, 4)
        self.assertEqual(active, 0)
        self.assertTrue(all(plan["workspace_error"]["code"] == "workspace_unavailable" for plan in response["plans"]))
        self.assertTrue(all("超时" in plan["workspace_error"]["message"] for plan in response["plans"]))

    async def test_list_isolates_internal_workspace_failure_without_mislabeling_other_plans(self):
        failed = await self.create(workspace={"mode": "new"})
        resource = self.resource()
        ready = await self.create(workspace={"mode": "existing", "worktree_id": resource.id})
        project = await self.create()

        async def inspect(policy, **kwargs):
            if policy.mode == "new":
                raise AssertionError("private exception details")

        self.worktrees.validate_policy.side_effect = inspect
        with self.assertLogs("netizen_cli.schedules.service", level="ERROR") as captured:
            response = await self.service.manage({"mode": "list", "all": True})
        self.assertTrue(response["ok"], response)
        plans = {plan["id"]: plan for plan in response["plans"]}
        self.assertEqual(plans[failed["id"]]["workspace_error"]["code"], "internal_error")
        self.assertFalse(plans[failed["id"]]["can_run_now"])
        for identifier in (ready["id"], project["id"]):
            self.assertIsNone(plans[identifier]["workspace_error"])
            self.assertTrue(plans[identifier]["workspace_available"])
            self.assertTrue(plans[identifier]["can_run_now"])
        self.assertIn("AssertionError", str(captured.output))
        self.assertNotIn("private exception details", str(response) + str(captured.output))

    async def test_run_creation_receipt_exposes_retained_path_and_survives_resource_deletion(self):
        plan = await self.create(workspace={"mode": "new"})
        self.now = plan["next_due_at"]
        claim = self.store.schedules.claim_due(plan["id"], app_id="app", now=self.now)
        self.assertIsNotNone(claim)
        resource = self.resource()
        self.store.schedules.set_run(claim.run.id, worktree_id=resource.id)
        self.store.schedules.release(claim.run.id, error_code="workspace_creation_failed")
        response = await self.service.manage({"mode": "runs", "plan_id": plan["id"]})
        receipt = response["runs"][0]["worktree"]
        self.assertEqual(receipt, {
            "id": resource.id, "root": resource.root, "cwd": resource.cwd, "creation_state": "ready",
        })
        detail = await self.service.manage({"mode": "view", "plan_id": plan["id"]})
        self.assertEqual(detail["plan"]["latest_run"]["worktree"], receipt)
        self.assertEqual(detail["plan"]["execution"]["worktree"], receipt)
        snapshot = self.store.worktrees.preview_delete(resource.id)
        self.store.worktrees.reserve_delete(resource.id, expected_fingerprint=snapshot.fingerprint)
        self.store.worktrees.finish_delete(resource.id, expected_fingerprint=snapshot.fingerprint)
        response = await self.service.manage({"mode": "runs", "plan_id": plan["id"]})
        self.assertEqual(response["runs"][0]["worktree_id"], resource.id)
        self.assertEqual(response["runs"][0]["worktree"], {
            "id": resource.id, "root": None, "cwd": None, "creation_state": None,
        })


class ScheduleWorkspaceRunNowTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = manual_support.ScheduleRunNowTest.asyncSetUp
    asyncTearDown = manual_support.ScheduleRunNowTest.asyncTearDown
    trigger = manual_support.ScheduleRunNowTest.trigger
    until = manual_support.ScheduleRunNowTest.until

    def workspace_plan(self):
        changed = self.store.schedules.update(
            self.plan.plan_id, expected_revision=1, request_id="workspace",
            changes={"workspace": WorkspacePolicy("new")}, now=self.now,
        )
        self.request["expected_revision"] = changed.revision
        self.worktrees = SimpleNamespace(validate_policy=AsyncMock(return_value=None))
        self.service._worktrees = lambda: self.worktrees

    async def test_preclaim_workspace_rejection_creates_no_run_or_cursor_change(self):
        self.workspace_plan()
        before = self.store.schedules.get(self.plan.plan_id)
        self.worktrees.validate_policy.side_effect = WorktreeError("unavailable", "仓库状态未取得。")
        response = await self.trigger()
        self.assertEqual(response["error"]["code"], "workspace_unavailable")
        self.assertEqual(self.store.schedules.get(before.id), before)
        self.assertEqual(self.store.schedules.list_runs(before.id), ())
        self.assertEqual(self.dispatched, [])

    async def test_preclaim_timeout_and_internal_error_leave_no_execution_receipt(self):
        self.workspace_plan()
        before = self.store.schedules.get(self.plan.plan_id)
        self.worktrees.validate_policy.side_effect = TimeoutError()
        response = await self.trigger()
        self.assertEqual(response["error"]["code"], "workspace_unavailable")
        self.assertIn("超时", response["error"]["message"])
        self.worktrees.validate_policy.side_effect = AssertionError("internal validation defect")
        with self.assertRaisesRegex(AssertionError, "internal validation defect"):
            await self.trigger()
        self.assertEqual(self.store.schedules.get(before.id), before)
        self.assertEqual(self.store.schedules.list_runs(before.id), ())
        self.assertEqual(self.dispatched, [])

    async def test_replay_bypasses_new_workspace_failure(self):
        self.workspace_plan()
        first = await self.trigger()
        self.assertTrue(first["accepted"])
        self.worktrees.validate_policy.side_effect = WorktreeError("unavailable", "目录失效。")
        replay = await self.trigger()
        self.assertTrue(replay["accepted"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["run_id"], first["run_id"])
        self.assertEqual(self.worktrees.validate_policy.await_count, 1)

    async def test_plan_mutation_during_precheck_prevents_claim_of_a_different_version(self):
        self.workspace_plan()

        async def change(*args, **kwargs):
            current = self.store.schedules.get(self.plan.plan_id)
            self.store.schedules.update(
                current.id, expected_revision=current.revision, request_id="changed-during-read",
                changes={"name": "Revised"}, now=self.now,
            )

        self.worktrees.validate_policy.side_effect = change
        response = await self.trigger()
        self.assertEqual(response["error"]["code"], "revision_conflict")
        self.assertEqual(self.store.schedules.list_runs(self.plan.plan_id), ())
        self.assertEqual(self.dispatched, [])
