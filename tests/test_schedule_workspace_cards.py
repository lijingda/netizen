"""Workspace policy cards retain exact identity across read-only refreshes."""

from __future__ import annotations

import unittest
from pathlib import Path

from netizen_cli.cards.callbacks import CardActionError
from netizen_cli.cards.scheduled import (
    decode_schedule_action, read_schedule_form, refresh_schedule_workspace,
    schedule_draft_card, schedule_form_card, schedule_manager_card, schedule_retry_card,
)
from netizen_cli.cards.workspace import workspace_reference
from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.projects import Project
from netizen_cli.session_settings import SessionSettings
from tests.support.channel_cards import callback, elements, form_values


class ScheduleWorkspaceCardsTest(unittest.TestCase):
    def setUp(self):
        self.scope = FeishuScope("app", "oc_group", ScopeKind.TOPIC, "omt_exact")
        self.project = Project("work", Path("/tmp/work"), True, 4)
        self.options = {
            "projectAlias": "work", "projectRevision": 4, "isGit": True,
            "items": [
                {"id": "wt-ready", "state": "ready", "available": True, "cwd": "/tmp/wt-ready", "branch": None},
                {"id": "wt-missing", "state": "ready", "available": False, "cwd": "/tmp/wt-missing"},
            ],
            "branches": [{"name": "main", "ref": "refs/heads/main", "remote": False}],
        }
        self.plan = {
            "id": "plan-one", "revision": 7, "name": "检查", "instructions": "检查项目",
            "project_alias": "work", "chat_id": "oc_group", "enabled": True,
            "schedule": {"kind": "daily", "timezone": "UTC", "at": "09:00"},
            "session_settings": SessionSettings().to_dict(), "can_run_now": True,
        }

    def editor(self, workspace=None, **kwargs):
        return schedule_form_card(self.scope, projects=[self.project], default_timezone="UTC",
            plan={**self.plan, "workspace": workspace or {"mode": "project"}}, **kwargs)

    def test_three_policies_roundtrip_without_expanding_head_to_a_commit(self):
        for policy in ({"mode": "project"}, {"mode": "existing", "worktree_id": "wt-ready"},
                {"mode": "new"}, {"mode": "new", "ref": "refs/heads/main"}):
            with self.subTest(policy=policy):
                card = self.editor(policy, workspace_options=self.options)
                action = decode_schedule_action(scope=self.scope, value=None, form=form_values(card))
                self.assertEqual(action.payload["workspace"], policy)
                self.assertEqual(action.payload["expected_project_revision"], 4)
                control = next(item for item in elements(card.card, "select_static")
                    if item["name"] == "cron_workspace")
                self.assertEqual(len(control["options"]), 3)
                self.assertNotIn("wt-missing", str(control))

    def test_missing_saved_selection_is_visible_and_can_be_kept_for_metadata_update(self):
        policy = {"mode": "existing", "worktree_id": "wt-deleted"}
        card = self.editor(policy, workspace_options=self.options, workspace_error="已登记工作区不存在")
        self.assertIn("wt-deleted", str(card.card))
        self.assertIn("已登记工作区不存在", str(card.card))
        values = form_values(card)
        values[next(key for key in values if key.startswith("cron_name"))] = "只改名称"
        self.assertEqual(decode_schedule_action(scope=self.scope, value=None, form=values).payload["workspace"], policy)
        control = next(item for item in elements(card.card, "select_static") if item["name"] == "cron_workspace")
        self.assertEqual(len(control["options"]), 4)
        values["cron_workspace"] = control["options"][0]["value"]
        self.assertEqual(decode_schedule_action(scope=self.scope, value=None, form=values).payload["workspace"], {"mode": "project"})

    def test_same_project_refresh_keeps_complete_and_incomplete_drafts_and_write_identity(self):
        policy = {"mode": "new", "ref": "refs/heads/removed"}
        card = self.editor(policy, workspace_options=self.options)
        values = form_values(card)
        values[next(key for key in values if key.startswith("cron_name"))] = ""
        values[next(key for key in values if key.startswith("cron_instructions"))] = ""
        values.update(cron_every_minutes="not-yet-valid", cron_target_mode="id",
            cron_chat_id="oc_pending", cron_chat_query="草稿查询")
        before = read_schedule_form(self.scope, values)
        load = decode_schedule_action(scope=self.scope, value=callback(card, "加载 worktree 选项"), form=values)
        self.assertEqual(load.action, "load_workspace")
        for options in (None, {**self.options, "projectRevision": 5}):
            with self.subTest(loaded=options is not None):
                refreshed = refresh_schedule_workspace(load.payload["draft"], options)
                rendered = schedule_draft_card(refreshed, projects=[self.project], workspace_options=options,
                    workspace_error="暂时读取失败" if options is None else None)
                after = read_schedule_form(self.scope, form_values(rendered))
                self.assertEqual({key: value for key, value in after.fields.items() if key != "cron_project"},
                    {key: value for key, value in before.fields.items() if key != "cron_project"})
                self.assertEqual(after.target, before.target)
                self.assertEqual(after.session_fields, before.session_fields)
                self.assertEqual(after.scope, before.scope)
                self.assertEqual(after.meta, {**before.meta, "project_revision": 5 if options else 4})
                self.assertEqual(after.workspace_fields["cron_worktree_ref"], "refs/heads/removed")

    def test_project_change_requires_explicit_load_and_resets_workspace_visibly(self):
        other = Project("other", Path("/tmp/other"), True, 2)
        card = schedule_form_card(self.scope, projects=[self.project, other], default_timezone="UTC",
            plan={**self.plan, "workspace": {"mode": "existing", "worktree_id": "wt-ready"}},
            workspace_options=self.options)
        values = form_values(card)
        project_control = next(item for item in elements(card.card, "select_static") if item["name"] == "cron_project")
        values["cron_project"] = project_control["options"][1]["value"]
        with self.assertRaisesRegex(CardActionError, "重新加载"):
            decode_schedule_action(scope=self.scope, value=None, form=values)
        original = read_schedule_form(self.scope, values)
        draft = refresh_schedule_workspace(original, {
            "projectAlias": "other", "projectRevision": 2, "isGit": False, "items": [], "branches": [],
        })
        rendered = schedule_draft_card(draft, projects=[self.project, other])
        decoded = decode_schedule_action(scope=self.scope, value=None, form=form_values(rendered))
        self.assertEqual(decoded.payload["project"], "other")
        self.assertEqual(decoded.payload["workspace"], {"mode": "project"})
        self.assertEqual(decoded.request_id, original.meta["request_id"])
        self.assertEqual(decoded.payload["expected_revision"], self.plan["revision"])

    def test_retry_keeps_saved_workspace_even_without_catalog_or_projects(self):
        policy = {"mode": "new", "ref": "refs/remotes/origin/retained"}
        values = form_values(self.editor(policy, workspace_options=self.options))
        original = decode_schedule_action(scope=self.scope, value=None, form=values)
        scope, retried = schedule_retry_card(app_id=self.scope.app_id, chat_id=self.scope.chat_id,
            scope=self.scope, value=None, form=values, projects=[], notice="读取失败")
        self.assertEqual(scope, self.scope)
        self.assertEqual(decode_schedule_action(scope=self.scope, value=None, form=form_values(retried)), original)

    def test_binding_has_readonly_workspace_and_rejects_injected_policy(self):
        card = schedule_form_card(self.scope, projects=[], default_timezone="UTC", plan={
            **self.plan, "target_kind": "binding", "target_binding_id": "binding-exact",
            "session_settings": None, "workspace": None,
            "effective_workspace": {"working_cwd": "/tmp/binding-cwd", "worktree_id": "wt-binding",
                "native_cwd_unresolved": True},
        })
        values = form_values(card)
        self.assertFalse({"cron_workspace", "cron_worktree_ref"} & values.keys())
        self.assertIn("实际工作目录", str(card.card))
        self.assertIn("/tmp/binding-cwd", str(card.card))
        self.assertIn("wt-binding", str(card.card))
        self.assertIn("以原生会话为准", str(card.card))
        self.assertNotIn("加载 worktree 选项", str(card.card))
        values["cron_workspace"] = workspace_reference("work", 4, {"mode": "project"})
        with self.assertRaises(CardActionError):
            decode_schedule_action(scope=self.scope, value=None, form=values)
        with self.assertRaises(CardActionError):
            read_schedule_form(self.scope, values)

    def test_unavailable_plan_and_retained_run_resources_remain_inspectable(self):
        plan = {**self.plan, "workspace": {"mode": "existing", "worktree_id": "wt-deleted"},
            "can_run_now": False, "blocked_reason": "workspace_unavailable",
            "workspace_error": {"code": "missing", "message": "目录不存在，原资源引用保留"}}
        card = schedule_manager_card(self.scope, {"plans": [plan]}, selected={"plan": plan}, runs={"runs": [
            {"error_code": "workspace_creation_unknown", "worktree_id": "wt-unknown",
                "worktree": {"id": "wt-unknown", "root": "/tmp/retained", "cwd": "/tmp/retained/src"}},
            {"error_code": "workspace_handoff_failed", "worktree_id": "wt-removed",
                "worktree": {"id": "wt-removed", "root": None, "cwd": None}},
        ]})
        text = str(card.card)
        for retained in ("wt-deleted", "目录不存在", "wt-unknown", "/tmp/retained/src", "wt-removed", "待确认"):
            self.assertIn(retained, text)
        self.assertNotIn("立即运行", [item["text"]["content"] for item in elements(card.card, "button")])
        self.assertEqual(callback(card, "暂停")["payload"]["enabled"], False)

    def test_large_workspace_catalog_fails_without_silently_dropping_options(self):
        options = {**self.options, "items": [
            {"id": f"wt-{index}", "state": "ready", "available": True, "cwd": f"/tmp/wt-{index}"}
            for index in range(180)
        ]}
        with self.assertRaises(CardActionError):
            self.editor(workspace_options=options)
        card = self.editor(workspace_options=self.options)
        draft = read_schedule_form(self.scope, form_values(card))
        with self.assertRaises(CardActionError):
            schedule_draft_card(draft, projects=[self.project], workspace_options=options)
        self.assertEqual(len(options["items"]), 180)
