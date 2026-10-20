from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from netizen_cli.bindings import BindingStore, validate_channel_database
from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.schedules.models import ScheduleConflict, ScheduleError, ScheduleRequestConflict, ScheduleRule
from netizen_cli.schedules.store import require_schema
from netizen_cli.worktrees.policy import WorkspacePolicy


class ScheduleWorkspaceStorageTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "channel.sqlite3"
        self.store = BindingStore(self.path, wall_clock=lambda: 100)
        self.addCleanup(self.store.close)
        self.schedules = self.store.schedules
        self.project = self.store.register_project(alias="p", cwd="/repo")
        self.rule = ScheduleRule("interval", "UTC", every_minutes=1, anchor=100)
        self.counter = 0

    def create(self, **fields):
        self.counter += 1
        return self.schedules.create(**{
            "name": "Workspace plan", "instructions": "Inspect the project",
            "project_alias": "p", "app_id": "app", "chat_id": "chat",
            "schedule": self.rule, "request_id": f"create-{self.counter}",
            "workspace": WorkspacePolicy(), **fields,
        })

    def resource(self):
        self.counter += 1
        record = self.store.worktrees.register(
            project_alias="p", expected_project_revision=self.store.get_project("p").revision,
            root=f"/managed/{self.counter}", cwd=f"/managed/{self.counter}", common_dir="/repo/.git",
        )
        return self.store.worktrees.mark_ready(
            record.id, git_dir=f"/repo/.git/worktrees/{self.counter}", identity_token=f"identity-{self.counter}",
        )

    def binding(self):
        return self.store.create_channel_binding(
            scope=FeishuScope("app", "chat", ScopeKind.GROUP), project_alias="p", creator_id="user",
        )

    def remove_resource(self, record):
        preview = self.store.worktrees.preview_delete(record.id)
        self.store.worktrees.reserve_delete(record.id, expected_fingerprint=preview.fingerprint)
        self.store.worktrees.finish_delete(record.id, expected_fingerprint=preview.fingerprint)

    def test_new_topic_stores_project_policy_and_binding_inherits_without_policy(self):
        topic = self.create()
        self.assertEqual(self.schedules.get(topic.plan_id).workspace, WorkspacePolicy())
        binding = self.binding()
        existing = self.create(target_kind="binding", target_binding_id=binding.id, session_settings=None, workspace=None)
        self.assertIsNone(self.schedules.get(existing.plan_id).workspace)
        with self.assertRaises(ScheduleError):
            self.create(target_kind="binding", target_binding_id=binding.id,
                        session_settings=None, workspace=WorkspacePolicy())
        validate_channel_database(self.path)

    def test_workspace_updates_replace_whole_policy_and_claim_retains_original(self):
        original = WorkspacePolicy("new", ref="refs/heads/main")
        created = self.create(workspace=original)
        claim = self.schedules.claim_due(created.plan_id, app_id="app", now=160)
        self.schedules.update(created.plan_id, expected_revision=1, request_id="rename", changes={"name": "Renamed"})
        self.assertEqual(self.schedules.get(created.plan_id).workspace, original)
        self.schedules.update(created.plan_id, expected_revision=2, request_id="workspace",
                              changes={"workspace": WorkspacePolicy("new")})
        self.assertEqual(self.schedules.get(created.plan_id).workspace.to_dict(), {"mode": "new"})
        self.assertEqual(claim.plan.workspace, original)
        self.schedules.delete(created.plan_id, expected_revision=3, request_id="delete")
        self.assertIsNone(self.schedules.get(created.plan_id, include_deleted=True).workspace)
        self.assertEqual(claim.plan.workspace, original)
        self.assertIsNone(self.store._connection.execute(
            "SELECT workspace_json FROM schedule_plans WHERE plan_id=?", (created.plan_id,),
        ).fetchone()[0])
        validate_channel_database(self.path)

    def test_deleted_resource_keeps_plan_intent_and_allows_metadata_changes(self):
        record = self.resource()
        policy = WorkspacePolicy("existing", worktree_id=record.id)
        created = self.create(workspace=policy, checked_project_revision=1)
        self.remove_resource(record)
        current = self.schedules.get(created.plan_id)
        self.assertTrue(current.enabled)
        self.assertEqual(current.workspace, policy)
        self.schedules.update(created.plan_id, expected_revision=1, request_id="rename-missing", changes={"name": "Still saved"})
        self.schedules.update(created.plan_id, expected_revision=2, request_id="pause-missing", changes={"enabled": False})
        self.assertEqual(self.schedules.get(created.plan_id).workspace, policy)
        with self.assertRaises(ScheduleConflict) as caught:
            self.schedules.update(created.plan_id, expected_revision=3, request_id="reselect",
                                  changes={"workspace": policy}, checked_project_revision=1)
        self.assertEqual(caught.exception.code, "workspace_unavailable")
        self.assertEqual(self.schedules.get(created.plan_id).revision, 3)
        validate_channel_database(self.path)

    def test_save_rechecks_project_revision_and_worktree_delete_reservation(self):
        record = self.resource()
        policy = WorkspacePolicy("existing", worktree_id=record.id)
        preview = self.store.worktrees.preview_delete(record.id)
        self.store.worktrees.reserve_delete(record.id, expected_fingerprint=preview.fingerprint)
        with self.assertRaises(ScheduleConflict) as caught:
            self.create(workspace=policy, checked_project_revision=1)
        self.assertEqual(caught.exception.code, "workspace_unavailable")
        self.store.worktrees.release_delete(record.id)
        disabled = self.store.set_project_enabled(alias="p", enabled=False, expected_revision=1)
        self.store.set_project_enabled(alias="p", enabled=True, expected_revision=disabled.revision)
        with self.assertRaises(ScheduleConflict) as caught:
            self.create(workspace=policy, checked_project_revision=1)
        self.assertEqual(caught.exception.code, "project_unavailable")
        self.assertEqual(self.schedules.list(app_id="app"), ())

    def test_explicit_project_revision_does_not_revalidate_saved_workspace(self):
        record = self.resource()
        policy = WorkspacePolicy("existing", worktree_id=record.id)
        created = self.create(workspace=policy, checked_project_revision=1)
        self.remove_resource(record)
        with self.assertRaises(ScheduleConflict) as caught:
            self.schedules.update(
                created.plan_id, expected_revision=1, request_id="stale-project",
                changes={"name": "Renamed"}, expected_project_revision=999,
            )
        self.assertEqual(caught.exception.code, "project_changed")
        self.assertEqual(self.schedules.get(created.plan_id).revision, 1)
        self.schedules.update(
            created.plan_id, expected_revision=1, request_id="current-project",
            changes={"name": "Renamed"}, expected_project_revision=1,
        )
        disabled = self.store.set_project_enabled(alias="p", enabled=False, expected_revision=1)
        self.schedules.update(
            created.plan_id, expected_revision=2, request_id="disabled-project",
            changes={"enabled": False}, expected_project_revision=disabled.revision,
        )
        saved = self.schedules.get(created.plan_id)
        self.assertEqual(saved.workspace, policy)
        self.assertEqual(saved.name, "Renamed")
        self.assertFalse(saved.enabled)

    def test_project_revision_is_part_of_update_request_identity_and_replay_is_first(self):
        created = self.create()
        arguments = dict(
            expected_revision=1, request_id="rename",
            changes={"name": "Renamed"}, expected_project_revision=1,
        )
        changed = self.schedules.update(created.plan_id, **arguments)
        self.store.set_project_enabled(alias="p", enabled=False, expected_revision=1)
        replay = self.schedules.update(created.plan_id, **arguments)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.revision, changed.revision)
        with self.assertRaises(ScheduleRequestConflict):
            self.schedules.update(created.plan_id, **{**arguments, "expected_project_revision": 2})

    def test_project_reregistration_cannot_adopt_old_workspace(self):
        record = self.resource()
        preview = self.store.preview_project_delete("p")
        reserved = self.store.begin_project_delete(
            alias="p", expected_revision=preview.project.revision,
            expected_inventory_fingerprint=preview.fingerprint,
        )
        self.store.finish_project_delete(
            alias="p", expected_revision=reserved.project.revision,
            expected_inventory_fingerprint=reserved.fingerprint,
        )
        replacement = self.store.register_project(alias="p", cwd="/repo")
        with self.assertRaises(ScheduleConflict) as caught:
            self.create(workspace=WorkspacePolicy("existing", worktree_id=record.id),
                        checked_project_revision=replacement.revision)
        self.assertEqual(caught.exception.code, "workspace_unavailable")

    def test_run_resource_receipt_is_exact_and_survives_resource_or_plan_deletion(self):
        record = self.resource()
        created = self.create(workspace=WorkspacePolicy("new"))
        claim = self.schedules.claim_due(created.plan_id, app_id="app", now=160)
        self.assertIsNone(claim.run.worktree_id)
        self.schedules.begin_publication(claim.run.id)
        receipt = self.schedules.set_run(claim.run.id, worktree_id=record.id)
        self.assertEqual(self.schedules.set_run(claim.run.id, worktree_id=record.id).worktree_id, record.id)
        for invalid in (None, "", "other-id"):
            with self.subTest(invalid=invalid), self.assertRaises(ScheduleConflict):
                self.schedules.set_run(claim.run.id, worktree_id=invalid)
        self.schedules.release(claim.run.id, error_code="before_native")
        self.remove_resource(record)
        self.schedules.delete(created.plan_id, expected_revision=1, request_id="delete-plan")
        self.assertEqual(self.schedules.get_run(claim.run.id).worktree_id, receipt.worktree_id)
        self.assertEqual(self.store._connection.execute("PRAGMA foreign_key_list(schedule_plans)").fetchall(), [])
        validate_channel_database(self.path)
        binding = self.binding()
        bound = self.create(target_kind="binding", target_binding_id=binding.id, session_settings=None, workspace=None)
        _, bound_claim = self.schedules.claim_manual(bound.plan_id, app_id="app", expected_revision=1, request_id="binding-run")
        with self.assertRaises(ScheduleConflict):
            self.schedules.set_run(bound_claim.run.id, worktree_id=record.id)
        self.assertIsNone(self.schedules.get_run(bound_claim.run.id).worktree_id)

    def test_workspace_type_errors_do_not_consume_mutation_request(self):
        with self.assertRaises(ScheduleError):
            self.create(workspace={"mode": "new"}, request_id="typed-create")
        created = self.create(workspace=WorkspacePolicy("new"), request_id="typed-create")
        self.assertTrue(self.create(workspace=WorkspacePolicy("new"), request_id="typed-create").replayed)
        for invalid in (None, {"mode": "project"}):
            with self.subTest(invalid=invalid), self.assertRaises(ScheduleError):
                self.schedules.update(created.plan_id, expected_revision=1, request_id="typed-update",
                                      changes={"workspace": invalid})
        changed = self.schedules.update(created.plan_id, expected_revision=1, request_id="typed-update",
                                        changes={"workspace": WorkspacePolicy()})
        self.assertEqual(changed.revision, 2)

    def test_current_schema_rejects_missing_or_mixed_workspace_instead_of_defaulting(self):
        created = self.create()
        binding = self.binding()
        bound = self.create(target_kind="binding", target_binding_id=binding.id, session_settings=None, workspace=None)
        for plan_id, raw in ((created.plan_id, None), (created.plan_id, "{}"),
                             (created.plan_id, '{"mode":"existing"}'), (bound.plan_id, '{"mode":"project"}')):
            with self.subTest(plan_id=plan_id, raw=raw):
                self.store._connection.execute("SAVEPOINT invalid_workspace")
                self.store._connection.execute("UPDATE schedule_plans SET workspace_json=? WHERE plan_id=?", (raw, plan_id))
                with self.assertRaises(RuntimeError):
                    require_schema(self.store._connection)
                self.store._connection.execute("ROLLBACK TO invalid_workspace")
                self.store._connection.execute("RELEASE invalid_workspace")
        _, claim = self.schedules.claim_manual(
            bound.plan_id, app_id="app", expected_revision=1, request_id="binding-run",
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store._connection.execute(
                "UPDATE schedule_runs SET worktree_id='not-allowed' WHERE run_id=?", (claim.run.id,),
            )
