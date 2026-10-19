"""Worktree selection uses exact cards, not implicit checkout or native starts."""
from __future__ import annotations

import unittest
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from netizen_cli.cards import ACTION_VERSION, CardActionError, decode_button_action, decode_card_form, new_binding_card
from netizen_cli.cards.controls import _new_workspace_elements
from netizen_cli.domain import CardControlName, FeishuScope, ScopeKind
from netizen_cli.management import CreatedBinding
from netizen_cli.projects import Project
from netizen_cli.worktrees.errors import WorktreeError
from netizen_cli.worktrees.settings import WorktreeSettingsService
from tests.support.channel_cards import _elements, direct_card_event, form_controls
from tests.support.channel_fixtures import channel_fixture
from tests.support.channel_messages import FakeMessage


SCOPE = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
WORKTREE_ID = "11111111-2222-3333-4444-555555555555"


def options(*, git: bool = True, revision: int = 1) -> dict:
    return {
        "projectAlias": "test", "projectRevision": revision, "isGit": git,
        "items": [{"id": WORKTREE_ID, "cwd": "/worktrees/task", "branch": "task",
                   "head": "1234", "state": "ready", "available": True}],
        "branches": [
            {"name": "main", "ref": "refs/heads/main", "remote": False,
             "occupiedPaths": ["/project"]},
            {"name": "topic", "ref": "refs/heads/topic", "remote": False,
             "occupiedPaths": []},
            {"name": "origin/main", "ref": "refs/remotes/origin/main", "remote": True,
             "occupiedPaths": []},
        ],
    }


def values(choice: str = "project", **extra) -> dict:
    return {
        "new_project": "project:v1:test:1", "new_model": "new-model:v1:inherit",
        "new_task_reactions": "task-feedback:v2:off",
        "new_progress_card": "task-feedback:v2:on",
        "new_completion_mention": "task-feedback:v2:on",
        "new_workspace": "workspace:v1:test:1:" + choice,
        "new_worktree_mode": "detached", "new_worktree_ref": "HEAD",
        **extra,
    }


def decode(payload):
    return decode_card_form(scope=SCOPE, message_id="om_card", sender_id="ou_user",
                            tag="button", form_value=payload)


def fields(card):
    form = next(item for item in _elements(card.card, "form")
                if item["name"] == "new_binding_v6")
    return {item["name"]: item for item in form_controls(form)}


class WorktreeNewCardTest(unittest.TestCase):
    def render(self, data=None):
        return new_binding_card(
            scope=SCOPE, projects=(Project("test", Path("/project"), True, 1),),
            initial_project_alias="test", allow_context_mode=False,
            workspace_options=data, allow_workspace_selection=True,
        )

    def test_non_git_projects_hide_workspace_and_branch_fields(self):
        for data in (None, options(git=False)):
            with self.subTest(data=data):
                selected = fields(self.render(data))
                self.assertNotIn("new_workspace", selected)
                self.assertNotIn("new_worktree_ref", selected)

    def test_git_default_is_project_and_new_checkout_defaults_to_detached_head(self):
        selected = fields(self.render(options()))
        self.assertEqual(selected["new_workspace"]["initial_option"], "workspace:v1:test:1:project")
        self.assertEqual(selected["new_worktree_mode"]["initial_option"], "detached")
        self.assertEqual(selected["new_worktree_ref"]["initial_option"], "HEAD")
        self.assertEqual(len(selected["new_workspace"]["options"]), 3)
        self.assertIn("已占用", str(selected["new_worktree_ref"]["options"][1]))

    def test_all_ready_items_and_branch_options_are_rendered_without_truncation(self):
        data = options()
        data["items"] += [{"id": "missing", "cwd": "/missing", "state": "missing", "available": False}]
        data["branches"] += [
            {"name": f"topic-{n}", "ref": f"refs/heads/topic-{n}", "remote": False,
             "occupiedPaths": []} for n in range(120)
        ]
        selected = fields(self.render(data))
        self.assertEqual(len(selected["new_workspace"]["options"]), 3)
        self.assertEqual(len(selected["new_worktree_ref"]["options"]), 124)

    def test_workspace_layout_is_last_and_keeps_original_controls(self):
        data = options()
        card = self.render(data)
        form = next(item for item in _elements(card.card, "form") if item["name"] == "new_binding_v6")
        sections = form["elements"][:-1]
        self.assertEqual([item["columns"][0]["elements"][0]["content"] for item in sections],
                         ["**项目**", "**模型配置**", "**任务反馈**", "**工作区**"])
        selected = fields(card)
        for original in form_controls(_new_workspace_elements(data)):
            self.assertEqual({key: value for key, value in selected[original["name"]].items() if key != "width"}, original)
        self.assertEqual([item["name"] for item in _elements(card.card, "form")],
                         ["new_workspace_project_v1", "new_binding_v6"])
        self.assertEqual(_elements(form, "form"), [form])

    def test_project_default_does_not_apply_new_branch_settings(self):
        intent = decode(values(new_worktree_mode="new", new_worktree_branch="unused"))
        self.assertIsNone(intent.worktree_id)
        self.assertIsNone(intent.worktree_create)

    def test_existing_worktree_ignores_creation_configuration(self):
        intent = decode(values("id:" + WORKTREE_ID,
                               new_worktree_mode="new", new_worktree_branch="unused"))
        self.assertEqual(intent.worktree_id, WORKTREE_ID)
        self.assertIsNone(intent.worktree_create)

    def test_new_checkout_modes_remain_explicit(self):
        for extra, expected in (
            ({}, {"mode": "detached", "ref": "HEAD", "branch": None}),
            ({"new_worktree_mode": "new", "new_worktree_ref": "refs/remotes/origin/main",
              "new_worktree_branch": "feature/task"},
             {"mode": "new", "ref": "refs/remotes/origin/main", "branch": "feature/task"}),
            ({"new_worktree_mode": "existing", "new_worktree_ref": "refs/heads/topic"},
             {"mode": "existing", "ref": "refs/heads/topic", "branch": "topic"}),
        ):
            with self.subTest(expected=expected):
                self.assertEqual(decode(values("new", **extra)).worktree_create, expected)

    def test_rejects_stale_project_selection_and_invalid_creation_fields(self):
        invalid = (
            values("new", new_project="project:v1:other:1"),
            values("id:" + WORKTREE_ID, new_project="project:v1:test:2"),
            values("new", new_worktree_mode="existing"),
            values("new", new_worktree_mode="existing", new_worktree_ref="refs/remotes/origin/main"),
            values("new", new_worktree_mode="new"),
            values("new", new_worktree_mode="force"),
            values("new", new_cwd="/arbitrary"),
            {key: value for key, value in values().items() if key != "new_worktree_mode"},
        )
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(CardActionError):
                decode(payload)

    def test_old_default_form_and_changed_project_default_remain_supported(self):
        old = {key: value for key, value in values().items()
               if not key.startswith("new_work")}
        self.assertEqual(decode(old).name, CardControlName.CREATE_BINDING)
        self.assertIsNone(decode(values(new_project="project:v1:other:3")).worktree_id)

    def test_load_form_is_read_only_exact_and_nonce_changes_on_redraw(self):
        refs = []
        for _ in range(2):
            form = next(item for item in _elements(self.render().card, "form")
                        if item["name"] == "new_workspace_project_v1")
            field = next(item for item in form["elements"] if item.get("name") == "new_workspace_project")
            refs.append(field["initial_option"])
        self.assertNotEqual(refs[0], refs[1])
        for reference in refs:
            intent = decode({"new_workspace_project": reference})
            self.assertEqual(intent.name, CardControlName.PREPARE_NEW_WORKSPACE)
            self.assertEqual((intent.project_alias, intent.expected_revision), ("test", 1))
            self.assertIsNone(intent.worktree_create)
        with self.assertRaises(CardActionError):
            decode({"new_workspace_project": refs[0], "new_project": "project:v1:test:1"})

    def test_load_requires_source_card_form_scope_instead_of_button_envelope(self):
        with self.assertRaisesRegex(CardActionError, "只能通过对应表单"):
            decode_button_action(
                app_id=SCOPE.app_id, message_id="om_card", callback_chat_id=SCOPE.chat_id,
                sender_id="ou_user", tag="button", value={
                    "v": ACTION_VERSION, "intent": CardControlName.PREPARE_NEW_WORKSPACE.value,
                    "chat_id": SCOPE.chat_id, "scope_kind": SCOPE.kind.value,
                },
            )


class WorktreeNewChannelTest(unittest.IsolatedAsyncioTestCase):
    async def test_disposable_git_creation_and_existing_reuse_are_lazy(self):
        async with channel_fixture() as f:
            def git(*args, cwd=None):
                return subprocess.run(
                    ["git", "-c", "user.name=Netizen Test", "-c", "user.email=test@example.invalid", *args],
                    cwd=cwd or f.project, check=True, capture_output=True, text=True,
                    timeout=10,
                ).stdout.strip()

            git("init", "-q", "--initial-branch=main")
            git("commit", "-q", "--allow-empty", "-m", "initial")
            f.management.enable_worktrees(settings=WorktreeSettingsService(
                default_root=f.project_root / "managed",
            ))
            await f.app.handle_card_action(direct_card_event(f.channel, values(
                "new", new_worktree_mode="new", new_worktree_branch="feature/task",
            )))
            first = f.store.active_binding(SCOPE.key)
            self.assertIsNotNone(first, str(f.channel.updates[-1][1]))
            self.assertIsNone(first.native_thread_id)
            self.assertIsNotNone(first.worktree_id)
            self.assertEqual(git("branch", "--show-current", cwd=first.working_cwd), "feature/task")
            self.assertEqual(git("branch", "--show-current"), "main")
            picker = await f.app._new_binding_picker(
                scope=SCOPE, initial_project_alias="test", allow_context_mode=False,
            )
            self.assertIn(first.worktree_id, str(fields(picker)["new_workspace"]))
            await f.app.handle_card_action(direct_card_event(
                f.channel, values("id:" + first.worktree_id,
                                  new_worktree_mode="new", new_worktree_branch="ignored"),
                message_id="om_reuse",
            ))
            second = f.store.active_binding(SCOPE.key)
            self.assertNotEqual(second.id, first.id)
            self.assertEqual(second.worktree_id, first.worktree_id)
            self.assertEqual(second.working_cwd, first.working_cwd)
            self.assertEqual(git("branch", "--show-current", cwd=second.working_cwd), "feature/task")
            self.assertIsNone(second.native_thread_id)
            self.assertEqual(f.runtime.submit_calls, [])

    async def test_load_options_keeps_exact_topic_and_does_not_create_binding_or_thread(self):
        async with channel_fixture() as f:
            service = SimpleNamespace(options=AsyncMock(return_value=options()))
            manager = SimpleNamespace(worktrees=service)
            event = direct_card_event(f.channel, {"new_workspace_project": "project:v1:test:1"})
            event.chat_id = "oc_group"
            f.channel.chat_types["oc_group"] = "group"
            f.channel.fetched_messages["om_card"] = {
                "data": {"items": [{"chat_id": "oc_group", "thread_id": "omt_exact"}]},
            }
            with patch.object(f.app, "_management", manager):
                await f.app.handle_card_action(event)
            service.options.assert_awaited_once_with(project_alias="test", expected_project_revision=1)
            self.assertIn("new_workspace", str(f.channel.updates[-1][1]))
            self.assertIn("new_context_mode", str(f.channel.updates[-1][1]))
            self.assertEqual(f.runtime.submit_calls, [])
            scope = FeishuScope("cli_test", "oc_group", ScopeKind.TOPIC, "omt_exact")
            self.assertIsNone(f.store.active_binding(scope.key))

    async def test_new_has_read_only_loader_and_existing_project_stays_default(self):
        async with channel_fixture() as f:
            await f.create_binding(SCOPE)
            service = SimpleNamespace(options=AsyncMock(return_value=options(git=False)))
            with patch.object(f.app, "_management", SimpleNamespace(worktrees=service)):
                await f.app.handle_message(FakeMessage("/new", message_id="om_new"))
            service.options.assert_awaited_once_with(project_alias="test", expected_project_revision=None)
            self.assertNotIn("new_workspace", fields(f.channel.replies[-1][1]))
            self.assertEqual(f.runtime.submit_calls, [])

    async def test_worktree_selection_flows_to_shared_creation_and_displays_actual_cwd(self):
        async with channel_fixture() as f:
            created = await f.create_binding(SCOPE)
            bound = replace(created.binding, working_cwd="/managed/task", worktree_id=WORKTREE_ID)
            create = AsyncMock(return_value=CreatedBinding(project=created.project, binding=bound))
            with patch.object(f.app, "_management", SimpleNamespace(create_current_binding=create)):
                await f.app.handle_card_action(direct_card_event(f.channel, values("id:" + WORKTREE_ID)))
            self.assertEqual(create.await_args.kwargs["worktree_id"], WORKTREE_ID)
            self.assertIsNone(create.await_args.kwargs["worktree_create"])
            self.assertEqual(create.await_args.kwargs["expected_project_revision"], 1)
            self.assertEqual(create.await_args.kwargs["scope"], SCOPE)
            self.assertEqual(f.runtime.submit_calls, [])
            self.assertIn("/managed/task", str(f.channel.updates[-1][1]))

    async def test_safe_partial_creation_error_survives_card_update_failure(self):
        async with channel_fixture() as f:
            error = WorktreeError("binding_failed", "会话创建失败；worktree /managed/task 已保留，可在 Admin 管理。")
            error.__cause__ = RuntimeError("secret raw git stderr")
            create = AsyncMock(side_effect=error)
            with patch.object(f.app, "_management", SimpleNamespace(create_current_binding=create)), \
                 patch.object(f.app, "_safe_update_card", return_value=False):
                await f.app.handle_card_action(direct_card_event(f.channel, values("new")))
            payload = create.await_args.kwargs["worktree_create"]
            self.assertEqual(payload, {"mode": "detached", "ref": "HEAD", "branch": None})
            message = str(f.channel.replies[-1][1])
            self.assertIn("/managed/task 已保留", message)
            self.assertNotIn("secret raw git stderr", message)

    async def test_stale_load_failure_cannot_become_default_creation(self):
        async with channel_fixture() as f:
            service = SimpleNamespace(options=AsyncMock(side_effect=WorktreeError("stale", "Project 已变化")))
            with patch.object(f.app, "_management", SimpleNamespace(worktrees=service)):
                await f.app.handle_card_action(direct_card_event(f.channel, {"new_workspace_project": "project:v1:test:1"}))
            self.assertIsNone(f.store.active_binding(SCOPE.key))
            self.assertIn("Project 已变化", str(f.channel.updates[-1][1]))


if __name__ == "__main__":
    unittest.main()
