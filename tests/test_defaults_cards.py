from __future__ import annotations

import base64
import copy
import json
import unittest
from pathlib import Path

from netizen_cli.cards.callbacks import CardActionError, _new_model_reference
from netizen_cli.cards.controls import decode_session_settings_form, session_settings_form_elements
from netizen_cli.cards.defaults import defaults_card, decode_defaults_action, is_defaults_card_action
from netizen_cli.domain import FeishuScope, MentionContextMode, ScopeKind
from netizen_cli.model_settings import EffortOption, ModelCatalog, ModelOption, ServiceTierOption
from netizen_cli.projects import Project
from netizen_cli.session_settings import BindingTaskFeedback, BindingTurnSettings, SessionSettings
from tests.support.channel_cards import callback, elements, form_controls, form_values, option_value


class DefaultsCardsTest(unittest.TestCase):
    def setUp(self):
        self.scope = FeishuScope("app", "oc_group", ScopeKind.TOPIC, "omt_topic")
        self.projects = [Project("work", Path("/tmp/work"), True, 2)]
        self.catalog = ModelCatalog((ModelOption(
            "model", "model", "Model", "", True, "high", "default",
            (EffortOption("high", "", "high"),), (ServiceTierOption("default", "Standard", ""),),
        ),))
        self.settings = SessionSettings(
            BindingTurnSettings("model", "high", "default"),
            BindingTaskFeedback(True, False, False), MentionContextMode.CATCH_UP,
        )
        self.record = {"id": "defaults-1", "kind": "chat", "chat_id": "oc_group", "keyword": None,
                       "project": "work", "session_settings": self.settings.to_dict(),
                       "workspace": {"mode": "project"}, "revision": 3, "position": None}
        self.view = {"exact": self.record, "effective": self.record, "chat_kind": "group", "match_error": None}

    def card(self, *, view=None, projects=None, catalog=True, scope=None, **kwargs):
        return defaults_card(scope or self.scope, view or self.view,
            self.projects if projects is None else projects, self.catalog if catalog else None, **kwargs)

    @staticmethod
    def project_field(card):
        return next(item for item in elements(card.card, "select_static") if item["name"].startswith("defaults_project"))

    @staticmethod
    def replace_metadata(form, **updates):
        result = dict(form)
        name = next(name for name in result if name.startswith("defaults_project"))
        metadata = {**option_value(result[name]), **updates}
        result[name] = base64.urlsafe_b64encode(json.dumps(metadata).encode()).decode().rstrip("=")
        return result

    def test_exact_form_roundtrips_settings_and_chat_identity(self):
        card = self.card()
        self.assertEqual(len(elements(card.card, "form")), 1)
        form = form_values(card)
        self.assertTrue(is_defaults_card_action({}, form))
        self.assertEqual(decode_defaults_action(self.scope, {}, form), {
            "mode": "save", "kind": "chat", "chat_id": "oc_group", "id": "defaults-1",
            "expected_revision": 3, "project": "work", "expected_project_revision": 2,
            "session_settings": self.settings.to_dict(),
        })
        self.assertIn(self.scope.chat_id, str(card.card))
        self.assertEqual([button["text"]["content"] for button in elements(card.card, "button")],
            ["保存默认配置", "删除默认配置"])

    def test_rule_prefill_creates_chat_override_not_rule_update(self):
        rule = {**self.record, "id": "rule-2", "kind": "group_name", "chat_id": None, "keyword": "oncall", "position": 0}
        view = {**self.view, "exact": None, "effective": rule}
        card = self.card(view=view)
        request = decode_defaults_action(self.scope, {}, form_values(card))
        self.assertEqual(request["session_settings"], rule["session_settings"])
        self.assertEqual(request["kind"], "chat")
        self.assertIsNone(request["expected_revision"])
        self.assertNotIn("id", request)
        self.assertNotIn("删除默认配置", str(card.card))
        self.assertIn("oncall", str(card.card))

    def test_editing_any_setting_and_explicitly_choosing_inherit(self):
        form = form_values(self.card())
        form.update(defaults_session_model="new-model:v1:inherit",
            defaults_session_task_reactions="task-feedback:v2:off",
            defaults_session_progress_card="task-feedback:v2:on",
            defaults_session_completion_mention="task-feedback:v2:on",
            defaults_session_context_mode="context-mode:v1:current-only")
        request = decode_defaults_action(self.scope, {}, form)
        self.assertEqual(request["session_settings"], SessionSettings.new_defaults(None).to_dict())
        self.assertEqual(request["expected_revision"], self.record["revision"])

    def test_unconfigured_leaves_project_blank_and_uses_ordinary_new_defaults(self):
        view = {**self.view, "exact": None, "effective": None}
        for catalog in (True, False):
            with self.subTest(catalog=catalog):
                card = self.card(view=view, catalog=catalog)
                field = self.project_field(card)
                self.assertNotIn("initial_option", field)
                form = form_values(card)
                with self.assertRaises(CardActionError):
                    decode_defaults_action(self.scope, {}, form)
                form[field["name"]] = field["options"][0]["value"]
                request = decode_defaults_action(self.scope, {}, form)
                self.assertEqual(request["session_settings"], SessionSettings.new_defaults(self.catalog if catalog else None).to_dict())
                self.assertIsNone(request["expected_revision"])
                self.assertNotIn("删除默认配置", str(card.card))

    def test_delete_is_independent_of_invalid_project_and_catalog(self):
        invalid = {**self.record, "project": "removed", "session_settings": {
            **self.settings.to_dict(), "turn_settings": {
                "model_id": "missing-model", "effort_id": "missing-effort", "service_tier_id": "missing-speed",
            },
        }}
        card = self.card(view={**self.view, "exact": invalid, "effective": invalid}, projects=[], catalog=False)
        saved = decode_defaults_action(self.scope, {}, form_values(card))
        self.assertEqual(saved["project"], "removed")
        self.assertIsNone(saved["expected_project_revision"])
        self.assertEqual(saved["session_settings"], invalid["session_settings"])
        value = callback(card, "删除默认配置")
        self.assertTrue(is_defaults_card_action(value))
        self.assertEqual(decode_defaults_action(self.scope, value), {
            "mode": "delete", "kind": "chat", "chat_id": "oc_group", "id": "defaults-1", "expected_revision": 3,
        })
        self.assertNotIn("behaviors", elements(card.card, "form")[0])

    def test_disabled_project_is_preserved_and_catalog_mismatch_does_not_inherit(self):
        settings = {**self.settings.to_dict(), "turn_settings": {
            "model_id": "unsupported-model", "effort_id": "high", "service_tier_id": "default",
        }}
        invalid = {**self.record, "session_settings": settings}
        card = self.card(view={**self.view, "exact": invalid}, projects=[Project("work", Path("/tmp/work"), False, 3)])
        project = option_value(self.project_field(card)["initial_option"])
        self.assertEqual(project["project"], "work")
        self.assertIsNone(project["project_revision"])
        self.assertEqual(decode_defaults_action(self.scope, {}, form_values(card))["session_settings"], settings)

    def test_p2p_main_and_topic_hide_and_reject_catchup(self):
        direct = FeishuScope("app", "oc_direct", ScopeKind.DIRECT)
        topic = FeishuScope("app", "oc_direct", ScopeKind.TOPIC, "omt_p2p")
        for scope in (direct, topic):
            with self.subTest(scope=scope):
                view = {"exact": None, "effective": None, "chat_kind": "p2p", "match_error": None}
                card = self.card(scope=scope, view=view)
                form = form_values(card)
                field = self.project_field(card)
                form[field["name"]] = field["options"][0]["value"]
                self.assertNotIn("defaults_session_context_mode", form)
                request = decode_defaults_action(scope, {}, form)
                self.assertEqual(request["chat_id"], "oc_direct")
                self.assertEqual(request["session_settings"]["message_context_mode"], "current-only")
                with self.assertRaises(CardActionError):
                    decode_defaults_action(scope, {}, {**form, "defaults_session_context_mode": "context-mode:v1:catch-up"})

    def test_scope_and_chat_id_cannot_be_redirected_from_form(self):
        card = self.card()
        form = form_values(card)
        other = FeishuScope("app", "other", ScopeKind.TOPIC, "omt_topic")
        same_chat_other_topic = FeishuScope("app", self.scope.chat_id, ScopeKind.TOPIC, "omt_other")
        for changed_scope in (other, same_chat_other_topic):
            with self.subTest(scope=changed_scope), self.assertRaises(CardActionError):
                decode_defaults_action(changed_scope, {}, form)
        with self.assertRaises(CardActionError):
            decode_defaults_action(self.scope, {}, self.replace_metadata(form, chat_id="other"))
        delete = callback(card, "删除默认配置")
        for changed_scope in (other, same_chat_other_topic):
            with self.subTest(scope=changed_scope), self.assertRaises(CardActionError):
                decode_defaults_action(changed_scope, delete)
        with self.assertRaises(CardActionError):
            decode_defaults_action(self.scope, {**delete, "chat_id": "other"})

    def test_strict_revision_version_and_form_shapes(self):
        form = form_values(self.card())
        malformed = [
            {**form, "unexpected": "value"},
            {**form, "defaults_project_v1__" + "a" * 32: "duplicate"},
            self.replace_metadata(form, expected_revision=True),
            self.replace_metadata(form, expected_revision=0),
            self.replace_metadata(form, expected_revision=None),
            self.replace_metadata(form, id=None),
            self.replace_metadata(form, project_revision=True),
            self.replace_metadata(form, v=True),
            self.replace_metadata(form, v=2),
            self.replace_metadata(form, chat_kind="unknown"),
            self.replace_metadata(form, chat_kind=[]),
            self.replace_metadata(form, project="../elsewhere"),
        ]
        for missing in ("defaults_session_context_mode", "defaults_session_completion_mention", "defaults_session_effort"):
            modified = dict(form)
            modified.pop(missing)
            malformed.append(modified)
        for modified in malformed:
            with self.subTest(form=modified), self.assertRaises(CardActionError):
                decode_defaults_action(self.scope, {}, modified)
        with self.assertRaises(CardActionError):
            decode_defaults_action(self.scope, callback(self.card(), "删除默认配置"), form)

    def test_rerender_changes_transport_identity_not_request(self):
        first, second = self.card(), self.card()
        first_form, second_form = form_values(first), form_values(second)
        self.assertNotEqual(first_form.keys(), second_form.keys())
        self.assertEqual(decode_defaults_action(self.scope, {}, first_form), decode_defaults_action(self.scope, {}, second_form))
        first_delete, second_delete = callback(first, "删除默认配置"), callback(second, "删除默认配置")
        self.assertNotEqual(first_delete["nonce"], second_delete["nonce"])
        self.assertEqual(decode_defaults_action(self.scope, first_delete), decode_defaults_action(self.scope, second_delete))

    def test_unconfigured_without_projects_explains_setup_without_empty_select(self):
        view = {**self.view, "exact": None, "effective": None}
        card = self.card(view=view, projects=[])
        self.assertIn("/settings", str(card.card))
        self.assertFalse(elements(card.card, "select_static"))
        self.assertNotIn("删除默认配置", str(card.card))

    def test_real_feishu_control_shapes_and_all_projects_are_preserved(self):
        projects = [Project(f"project{i}", Path(f"/tmp/project{i}"), True, i + 1) for i in range(20)]
        view = {**self.view, "exact": None, "effective": None}
        scope = FeishuScope("cli_" + "a" * 16, "oc_" + "a" * 32, ScopeKind.TOPIC, "omt_" + "b" * 32)
        card = self.card(view=view, projects=projects, scope=scope)
        self.assertEqual(len(self.project_field(card)["options"]), len(projects))
        for control in elements(card.card, "select_static"):
            self.assertNotIn("label", control)
            self.assertNotIn("initial_options", control)
            self.assertNotIn("behaviors", control)
            self.assertLessEqual(len(control["name"]), 100)
        names = [control["name"] for control in elements(card.card, "select_static")]
        self.assertEqual(len(names), len(set(names)))
        before = copy.deepcopy(view)
        self.card(view=view)
        self.assertEqual(view, before)

    def workspace_options(self, **changes):
        return {
            "projectAlias": "work", "projectRevision": 2, "isGit": True,
            "items": [
                {"id": "wt-ready", "cwd": "/tmp/worktrees/ready", "branch": None,
                 "state": "ready", "available": True},
                {"id": "wt-missing", "cwd": "/tmp/worktrees/missing", "branch": None,
                 "state": "missing", "available": False},
            ],
            "branches": [{"name": "main", "ref": "refs/heads/main", "remote": False}],
            **changes,
        }

    def workspace_card(self, workspace=None, **kwargs):
        record = {**self.record, "workspace": workspace or {"mode": "project"}}
        return self.card(view={**self.view, "exact": record, "effective": record},
                         allow_workspace_selection=True, **kwargs)

    def test_workspace_policies_roundtrip_and_only_offer_available_resources(self):
        for workspace in (
            {"mode": "project"}, {"mode": "existing", "worktree_id": "wt-ready"},
            {"mode": "new"}, {"mode": "new", "ref": "refs/heads/main"},
        ):
            with self.subTest(workspace=workspace):
                card = self.workspace_card(workspace, workspace_options=self.workspace_options())
                request = decode_defaults_action(self.scope, {}, form_values(card))
                self.assertEqual(request["workspace"], workspace)
                self.assertEqual(request["session_settings"], self.settings.to_dict())
                self.assertNotIn("wt-missing", str(card.card))
                self.assertIn("加载 worktree 选项", str(card.card))
        card = self.workspace_card({"mode": "new", "ref": "abc123"}, workspace_options=self.workspace_options())
        self.assertEqual(decode_defaults_action(self.scope, {}, form_values(card))["workspace"]["ref"], "abc123")

    def test_missing_workspace_and_failed_catalog_preserve_policy_and_delete(self):
        workspace = {"mode": "existing", "worktree_id": "wt-removed"}
        for options in (None, self.workspace_options(), self.workspace_options(isGit=False)):
            with self.subTest(options=options):
                card = self.workspace_card(workspace, workspace_options=options, workspace_error="目录查询失败")
                self.assertIn("wt-removed", str(card.card))
                self.assertIn("保留原配置", str(card.card))
                request = decode_defaults_action(self.scope, {}, form_values(card))
                self.assertEqual(request["workspace"], workspace)
                self.assertEqual(decode_defaults_action(self.scope, callback(card, "删除默认配置"))["mode"], "delete")

    def test_load_preserves_draft_policy_settings_and_configuration_revision(self):
        card = self.workspace_card({"mode": "new"}, workspace_options=self.workspace_options())
        form = form_values(card)
        form.update(defaults_session_model=_new_model_reference("future"),
                    defaults_session_effort="ultra", defaults_session_speed="priority-v2",
                    defaults_session_task_reactions="task-feedback:v2:off",
                    defaults_session_completion_mention="task-feedback:v2:on",
                    defaults_session_context_mode="context-mode:v1:current-only",
                    defaults_worktree_ref="refs/heads/main")
        request = decode_defaults_action(self.scope, callback(card, "加载 worktree 选项"), form)
        self.assertEqual(request["mode"], "load_workspace")
        self.assertEqual(request["workspace"], {"mode": "new", "ref": "refs/heads/main"})
        updated = {**self.record, "revision": 6}
        refreshed = self.card(view={**self.view, "exact": updated, "effective": updated}, draft=request,
                              workspace_options=self.workspace_options(), allow_workspace_selection=True, catalog=False)
        saved = decode_defaults_action(self.scope, {}, form_values(refreshed))
        self.assertEqual(saved["expected_revision"], 3)
        self.assertEqual(saved["workspace"], request["workspace"])
        self.assertEqual(saved["session_settings"], request["session_settings"])

    def test_switching_project_rejects_stale_workspace_until_explicit_load(self):
        other = Project("other", Path("/tmp/other"), True, 4)
        card = self.workspace_card({"mode": "existing", "worktree_id": "wt-ready"},
                                   workspace_options=self.workspace_options(), projects=[*self.projects, other])
        form = form_values(card)
        project = self.project_field(card)
        form[project["name"]] = project["options"][1]["value"]
        with self.assertRaisesRegex(CardActionError, "重新加载"):
            decode_defaults_action(self.scope, {}, form)
        request = decode_defaults_action(self.scope, callback(card, "加载 worktree 选项"), form)
        self.assertEqual(request["project"], "other")
        self.assertEqual(request["workspace"], {"mode": "project"})
        refreshed = self.card(draft=request, projects=[*self.projects, other], allow_workspace_selection=True,
                              workspace_options=self.workspace_options(projectAlias="other", projectRevision=4))
        self.assertEqual(decode_defaults_action(self.scope, {}, form_values(refreshed))["workspace"], {"mode": "project"})

    def test_load_ignores_invalid_old_workspace_but_save_rejects_it(self):
        card = self.workspace_card({"mode": "new"}, workspace_options=self.workspace_options())
        form = form_values(card)
        malformed = [
            {**form, "defaults_workspace": "bad"},
            {**form, "defaults_worktree_ref": ["not", "a", "ref"]},
            {**form, "defaults_workspace": base64.urlsafe_b64encode(json.dumps({
                "project": "work", "project_revision": True, "mode": "new",
            }).encode()).decode().rstrip("=")},
            {**form, "defaults_workspace": base64.urlsafe_b64encode(json.dumps({
                "project": "work", "project_revision": 2, "mode": [],
            }).encode()).decode().rstrip("=")},
        ]
        for fields in malformed:
            with self.subTest(fields=fields):
                with self.assertRaises(CardActionError):
                    decode_defaults_action(self.scope, {}, fields)
                self.assertEqual(
                    decode_defaults_action(self.scope, callback(card, "加载 worktree 选项"), fields)["mode"],
                    "load_workspace",
                )

    def test_non_git_and_unloaded_projects_allow_refresh_without_new_policy(self):
        for view, options in (
            (self.view, self.workspace_options(isGit=False, items=[], branches=[])),
            ({**self.view, "exact": None, "effective": None}, None),
        ):
            with self.subTest(view=view):
                card = self.card(view=view, workspace_options=options, allow_workspace_selection=True)
                self.assertIn("加载 worktree 选项", str(card.card))
                self.assertNotIn("defaults_worktree_ref", str(card.card))
                self.assertNotIn("每个新会话新建 worktree", str(card.card))

    def test_grouped_form_keeps_workspace_last_and_delete_outside_form(self):
        card = self.workspace_card({"mode": "new", "ref": "refs/heads/main"},
                                   workspace_options=self.workspace_options())
        form = elements(card.card, "form")[0]
        groups = [[item["name"] for item in form_controls(section)]
                  for section in form["elements"][:-1]]
        self.assertEqual(groups, [
            [self.project_field(card)["name"]],
            ["defaults_session_model", "defaults_session_effort", "defaults_session_speed"],
            ["defaults_session_context_mode"],
            ["defaults_session_task_reactions", "defaults_session_progress_card", "defaults_session_completion_mention"],
            ["defaults_workspace_load", "defaults_workspace", "defaults_worktree_ref"],
        ])
        self.assertEqual([item["name"] for item in form_controls(form["elements"][-1])], ["defaults_save"])
        self.assertFalse(any(button.get("behaviors", [{}])[0].get("value", {}).get("action") == "delete"
                             for button in elements(form, "button")))
        self.assertEqual(decode_defaults_action(self.scope, callback(card, "删除默认配置"))["mode"], "delete")
        request = decode_defaults_action(self.scope, {}, form_values(card))
        self.assertEqual(request["chat_id"], self.scope.chat_id)
        self.assertEqual(request["session_settings"], self.settings.to_dict())
        self.assertEqual(request["workspace"], {"mode": "new", "ref": "refs/heads/main"})

    def test_grouped_and_flat_shared_settings_preserve_explicit_choices_when_catalog_is_unavailable(self):
        settings = SessionSettings(
            BindingTurnSettings("unavailable-model", "unavailable-effort", "unavailable-speed"),
            self.settings.task_feedback, MentionContextMode.CURRENT_ONLY,
        )
        for catalog in (None, self.catalog):
            for allow_context in (True, False):
                with self.subTest(catalog=catalog, allow_context=allow_context):
                    values = []
                    for grouped in (False, True):
                        controls = session_settings_form_elements(
                            prefix="test", settings=settings, catalog=catalog,
                            allow_context_mode=allow_context, grouped=grouped,
                        )
                        fields = {item["name"]: item["initial_option"] for item in form_controls(controls)
                                  if item["tag"] == "select_static"}
                        values.append(fields)
                        self.assertEqual("test_context_mode" in fields, allow_context)
                        self.assertEqual(decode_session_settings_form(fields, prefix="test"), settings)
                    self.assertEqual(values[0], values[1])


if __name__ == "__main__":
    unittest.main()
