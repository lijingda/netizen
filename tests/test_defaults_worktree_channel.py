"""Default workspace intent reaches the ordinary Channel input boundary."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from netizen_cli.codex_runtime import Submission, SubmitDisposition
from netizen_cli.domain import MentionContextMode, MessageContextAnchor
from netizen_cli.message_history import MessageHistoryStats, MessageHistoryWindow
from netizen_cli.session_settings import SessionSettings
from netizen_cli.worktrees.errors import WorktreeError
from netizen_cli.worktrees.git import GitError
from netizen_cli.worktrees.settings import WorktreeSettingsService
from tests.support.channel_cards import direct_card_event, new_form_values
from tests.support.channel_fixtures import channel_fixture
from tests.support.channel_messages import FakeMessage, plain_prompt_projection


class DefaultsWorktreeChannelTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = await self.enterAsyncContext(channel_fixture())
        self.app, self.store = self.f.app, self.f.store
        self.defaults = self.f.management.defaults
        self.submissions = []
        self.f.channel.chat_types["oc_direct"] = "p2p"
        self.git("init", "-q", "--initial-branch=main")
        self.git("commit", "-q", "--allow-empty", "-m", "initial")
        self.f.management.enable_worktrees(settings=WorktreeSettingsService(
            default_root=self.f.project_root / "managed",
        ))
        self.worktrees = self.f.management.worktrees
        self.enterContext(patch.object(self.f.runtime, "submit", side_effect=self.accept))

    def git(self, *args, cwd=None):
        return subprocess.run(
            ["git", "-c", "user.name=Netizen Test", "-c", "user.email=test@example.invalid", *args],
            cwd=cwd or self.f.project, check=True, capture_output=True, text=True, timeout=10,
        ).stdout.strip()

    async def accept(self, **kwargs):
        self.submissions.append(kwargs)
        binding = kwargs["binding"]
        self.store.assign_native_thread_id(binding.id, "native-" + binding.id)
        commit = kwargs["context_commit"]
        if commit is not None:
            self.store.commit_context_anchor(
                binding_id=binding.id, expected_context_revision=commit.expected_context_revision,
                anchor=commit.anchor,
            )
        return Submission(
            SubmitDisposition.STARTED, binding.id, "native-" + binding.id,
            "turn-" + kwargs["origin"].id, lambda: None, task_feedback=binding.task_feedback,
        )

    async def save(self, workspace, *, chat_id="oc_group", keyword=None, settings=None, rule=None):
        request = {
            "mode": "save", "kind": "group_name" if keyword else "chat",
            "project": "test", "workspace": workspace,
            "session_settings": (settings or SessionSettings()).to_dict(),
            "expected_revision": rule["revision"] if rule else None,
        }
        request["keyword" if keyword else "chat_id"] = keyword or chat_id
        if rule:
            request["id"] = rule["id"]
        return (await self.defaults.manage(request))["rule"]

    def message(self, message_id="om_first", *, topic="omt_task", **kwargs):
        return FakeMessage(
            "分析当前任务", message_id=message_id, chat_id="oc_group",
            chat_type="group", thread_id=topic, **kwargs,
        )

    def resources(self):
        return self.store.worktrees.list_for_project("test")

    async def existing_rule_with_fallback(self):
        resource = await self.worktrees.create(project_alias="test")
        rule = await self.save({"mode": "existing", "worktree_id": resource["id"]}, keyword="oncall")
        await self.save({"mode": "project"}, keyword="oncall")
        self.enterContext(patch.object(self.f.channel, "get_chat_info", AsyncMock(
            return_value=SimpleNamespace(chat_mode="group", name="oncall"),
        )))
        return resource, rule

    async def assert_unavailable(self, rule, message_id):
        message = self.message(message_id)
        before = len(self.f.channel.replies)
        await self.app.handle_message(message)
        self.assertEqual(self.submissions, [])
        self.assertIsNone(self.store.active_binding(self.app._scope(message).key))
        replies = self.f.channel.replies[before:]
        self.assertEqual(len(replies), 2)
        self.assertIn("默认会话配置不可用", replies[0][1])
        self.assertIn("/new", replies[1][1])
        self.assertIn("尚未执行", replies[1][1])
        self.assertEqual(self.store.defaults.get("cli_test", rule["id"]).to_dict(), rule)

    async def test_existing_workspace_reaches_actual_cwd_and_existing_binding_ignores_default_changes(self):
        resource = await self.worktrees.create(project_alias="test")
        rule = await self.save({"mode": "existing", "worktree_id": resource["id"]})
        first = self.message(sender_id="ou_alice")
        await self.app.handle_message(first)
        binding = self.store.active_binding(self.app._scope(first).key)
        submitted = self.submissions[0]
        self.assertEqual(binding.worktree_id, resource["id"])
        self.assertEqual(submitted["cwd"], Path(resource["cwd"]))
        self.assertIs(submitted["origin"], first)
        self.assertEqual(submitted["owner_id"], "ou_alice")
        self.assertEqual(plain_prompt_projection(submitted["input"])[0], first.body_text)

        await self.save({"mode": "new"}, rule=rule)
        self.assertEqual(len(self.resources()), 1)
        with patch.object(self.defaults, "resolve", side_effect=AssertionError("existing binding must skip defaults")):
            await self.app.handle_message(self.message("om_again"))
        self.assertEqual(self.submissions[-1]["binding"].id, binding.id)
        self.assertIsNone(self.submissions[-1]["cwd"], "materialized cwd remains native-owned")
        self.assertEqual(self.store.get(binding.id).working_cwd, resource["cwd"])
        self.assertEqual(len(self.resources()), 1)

    async def test_new_policy_creates_one_detached_workspace_per_topic_from_head_at_creation(self):
        await self.save({"mode": "new"})
        self.assertEqual(self.resources(), ())
        self.assertFalse((self.f.project_root / "managed").exists())
        bindings = []
        for index in range(2):
            self.git("commit", "-q", "--allow-empty", "-m", f"revision-{index}")
            expected_head = self.git("rev-parse", "HEAD")
            message = self.message(f"om_topic_{index}", topic=f"omt_{index}")
            await self.app.handle_message(message)
            binding = self.store.active_binding(self.app._scope(message).key)
            bindings.append(binding)
            self.assertIsNotNone(binding.worktree_id)
            self.assertEqual(self.submissions[-1]["cwd"], Path(binding.working_cwd))
            self.assertEqual(self.git("rev-parse", "HEAD", cwd=binding.working_cwd), expected_head)
            self.assertEqual(self.git("branch", "--show-current", cwd=binding.working_cwd), "")
        self.assertNotEqual(bindings[0].worktree_id, bindings[1].worktree_id)
        self.assertNotEqual(bindings[0].working_cwd, bindings[1].working_cwd)
        self.assertEqual(self.git("branch", "--show-current"), "main")
        await self.app.handle_message(self.message("om_repeat", topic="omt_0"))
        self.assertEqual(self.submissions[-1]["binding"].id, bindings[0].id)
        self.assertEqual(len(self.resources()), 2)
        self.assertEqual(self.f.channel.send_calls, [])

    async def test_managed_deletion_preserves_rule_and_refuses_input_without_matching_next_rule(self):
        resource, rule = await self.existing_rule_with_fallback()
        with patch.object(self.f.runtime, "worktree_side_snapshots", return_value=(), create=True):
            preview = await self.worktrees.preview_delete(resource["id"])
            self.assertTrue(preview["safe"], preview)
            result = await self.worktrees.delete(resource["id"], expected_fingerprint=preview["fingerprint"])
        self.assertTrue(result["deleted"], result)
        self.assertFalse(Path(resource["cwd"]).exists())
        await self.assert_unavailable(rule, "om_deleted")

    async def test_external_directory_removal_refuses_input_without_project_fallback(self):
        resource, rule = await self.existing_rule_with_fallback()
        shutil.rmtree(resource["root"])
        await self.assert_unavailable(rule, "om_missing_directory")
        self.assertEqual(len(self.resources()), 1)

    async def test_deleted_project_and_same_alias_reregistration_never_adopt_old_workspace(self):
        resource, rule = await self.existing_rule_with_fallback()
        preview = self.f.projects.preview_delete("test")
        reserved = self.f.projects.begin_delete(
            alias="test", expected_revision=preview.project.revision,
            expected_inventory_fingerprint=preview.fingerprint,
        )
        self.f.projects.finish_delete(
            alias="test", expected_revision=reserved.project.revision,
            expected_inventory_fingerprint=reserved.fingerprint,
        )
        await self.assert_unavailable(rule, "om_deleted_project")
        self.f.projects.register(alias="test", path=str(self.f.project), create_directory=False)
        await self.assert_unavailable(rule, "om_reregistered_project")
        self.assertTrue(Path(resource["cwd"]).exists())
        self.assertEqual(len(self.resources()), 1)

    async def test_runtime_handoff_failure_retains_resource_and_reports_failure_despite_manual_current(self):
        await self.save({"mode": "new"})
        message = self.message()
        scope = self.app._scope(message)
        create = self.f.management.create_current_binding
        manual = None

        async def create_then_manual(**kwargs):
            nonlocal manual
            try:
                return await create(**kwargs)
            except WorktreeError as error:
                self.assertEqual(error.code, "session_handoff_failed")
                manual = await create(scope=scope, creator_id="ou_manual", project_alias="test")
                raise

        with patch.object(self.f.runtime, "binding_pointer_changed", AsyncMock(side_effect=[
            RuntimeError("test handoff failure"), None,
        ])), patch.object(self.f.management, "create_current_binding", create_then_manual):
            await self.app.handle_message(message)
        self.assertIsNotNone(manual)
        self.assertEqual(self.store.active_binding(scope.key).id, manual.binding.id)
        self.assertEqual(self.submissions, [])
        self.assertEqual(len(self.store.list_bindings(scope.key)), 2)
        resource, = self.resources()
        self.assertTrue(Path(resource.cwd).exists())
        response = str(self.f.channel.replies[-1][1])
        self.assertIn("运行时交接失败", response)
        self.assertIn(resource.cwd, response)
        self.assertIn("已保留", response)
        self.assertIn("尚未执行", response)
        self.assertNotIn("test handoff failure", response)

    async def test_uncertain_git_completion_still_reports_unknown_when_manual_current_exists(self):
        await self.save({"mode": "new"})
        message = self.message()
        scope = self.app._scope(message)
        git_create = self.worktrees._git.create
        manual = None

        async def create_then_unknown(*args, **kwargs):
            nonlocal manual
            await git_create(*args, **kwargs)
            manual = await self.f.create_binding(scope)
            raise GitError("worktree add", "test lost completion", outcome="unknown")

        with patch.object(self.worktrees._git, "create", create_then_unknown):
            await self.app.handle_message(message)
        self.assertIsNotNone(manual)
        self.assertEqual(self.store.active_binding(scope.key).id, manual.binding.id)
        self.assertEqual(self.submissions, [])
        resource, = self.resources()
        self.assertEqual(resource.creation_state, "unknown")
        self.assertTrue(Path(resource.cwd).exists())
        response = str(self.f.channel.replies[-1][1])
        self.assertIn("未确认完成", response)
        self.assertIn("保留", response)
        self.assertIn("尚未执行", response)
        self.assertNotIn("test lost completion", response)

    async def test_manual_new_wins_during_creation_and_retained_workspace_is_reported(self):
        await self.save({"mode": "new"}, chat_id="oc_direct")
        message = FakeMessage("执行原始任务", message_id="om_auto")
        scope = self.app._scope(message)
        entered, release = asyncio.Event(), asyncio.Event()
        create = self.worktrees.create

        async def delayed(**kwargs):
            result = await create(**kwargs)
            entered.set()
            await release.wait()
            return result

        with patch.object(self.worktrees, "create", delayed):
            pending = asyncio.create_task(self.app.handle_message(message))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                await self.app.handle_message(FakeMessage("/new", message_id="om_manual"))
                card = self.f.channel.replies[-1][1]
                await self.app.handle_card_action(direct_card_event(
                    self.f.channel, new_form_values(card), message_id="om_manual_card",
                ))
                manual = self.store.active_binding(scope.key)
                self.assertIsNotNone(manual)
                self.assertIsNone(manual.worktree_id)
            finally:
                release.set()
                await asyncio.wait_for(pending, 5)
        resource, = self.resources()
        self.assertEqual(len(self.store.list_bindings(scope.key)), 1)
        self.assertEqual(self.submissions[0]["binding"].id, manual.id)
        self.assertIs(self.submissions[0]["origin"], message)
        self.assertEqual(self.submissions[0]["cwd"], self.f.project.resolve())
        self.assertEqual(plain_prompt_projection(self.submissions[0]["input"])[0], message.body_text)
        notice = next(str(content) for message_id, content in self.f.channel.replies
                      if message_id == message.id)
        self.assertIn(resource.cwd, notice)
        self.assertIn("已保留", notice)
        self.assertIn("沿用", notice)
        self.assertEqual(self.store.worktrees.bindings(resource.id), ())

    async def test_concurrent_initial_messages_share_one_created_workspace_and_binding(self):
        await self.save({"mode": "new"})
        entered, joined, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        create = self.worktrees.create
        create_binding = self.f.management.create_current_binding
        creation_calls = 0
        admission_calls = 0

        async def delayed(**kwargs):
            nonlocal creation_calls
            creation_calls += 1
            result = await create(**kwargs)
            entered.set()
            await release.wait()
            return result

        async def admitted(**kwargs):
            nonlocal admission_calls
            admission_calls += 1
            if admission_calls == 2:
                joined.set()
            return await create_binding(**kwargs)

        first, second = self.message("om_first"), self.message("om_second")
        with patch.object(self.worktrees, "create", delayed), \
             patch.object(self.f.management, "create_current_binding", admitted):
            pending = [asyncio.create_task(self.app.handle_message(first))]
            try:
                await asyncio.wait_for(entered.wait(), 5)
                pending.append(asyncio.create_task(self.app.handle_message(second)))
                await asyncio.wait_for(joined.wait(), 5)
            finally:
                release.set()
                await asyncio.wait_for(asyncio.gather(*pending), 5)
        self.assertEqual(creation_calls, 1)
        self.assertEqual(len(self.resources()), 1)
        self.assertEqual(len(self.store.list_bindings(self.app._scope(first).key)), 1)
        self.assertEqual(len(self.submissions), 2)
        self.assertEqual({item["origin"].id for item in self.submissions}, {first.id, second.id})
        self.assertEqual(len({item["binding"].id for item in self.submissions}), 1)
        resource, = self.resources()
        self.assertEqual(self.submissions[0]["cwd"], Path(resource.cwd))
        self.assertTrue(all(item["cwd"] in (None, Path(resource.cwd)) for item in self.submissions))

    async def test_failed_retained_workspace_notice_does_not_block_original_input(self):
        await self.save({"mode": "new"})
        message = self.message()
        create = self.worktrees.create
        manual = None

        async def create_after_manual(**kwargs):
            nonlocal manual
            manual = await self.f.create_binding(self.app._scope(message))
            return await create(**kwargs)

        with patch.object(self.worktrees, "create", create_after_manual), \
             patch.object(self.app, "_reply", AsyncMock(side_effect=RuntimeError("display unavailable"))), \
             self.assertLogs("netizen_cli.channel_app", level="WARNING"):
            await self.app.handle_message(message)
        self.assertEqual(len(self.resources()), 1)
        self.assertEqual(len(self.submissions), 1)
        self.assertEqual(self.submissions[0]["binding"].id, manual.binding.id)
        self.assertIs(self.submissions[0]["origin"], message)

    async def test_new_workspace_keeps_first_catchup_anchor_quote_and_later_history(self):
        await self.save({"mode": "new"}, settings=SessionSettings(message_context_mode=MentionContextMode.CATCH_UP))
        self.f.channel.inbound_messages["om_quote"] = self.message("om_quote", topic=None)
        first = self.message("om_first", topic=None, reply_id="om_quote")
        scope = self.app._scope(first)
        lower = MessageContextAnchor(first.id, 1000)
        self.f.message_history.anchors[first.id] = lower
        await self.app.handle_message(first)
        self.assertEqual(self.f.message_history.read_calls, [])
        self.assertEqual(self.f.channel.fetch_inbound_calls, ["om_quote"])
        submitted = self.submissions[0]
        self.assertIs(submitted["origin"], first)
        self.assertEqual(json.loads(submitted["input"])["quoted_message"]["text"], first.body_text)
        self.assertEqual(submitted["context_commit"].anchor, lower)
        binding = self.store.get(submitted["binding"].id)
        self.assertEqual(binding.context_revision, 2)
        second = self.message("om_later", topic=None)
        upper = MessageContextAnchor(second.id, 2000)
        self.f.message_history.window = MessageHistoryWindow(
            lower, upper, (), MessageHistoryStats(1, 2, 0, 0, 0, False, False),
        )
        await self.app.handle_message(second)
        self.assertEqual(self.f.message_history.read_calls, [(scope, lower, second.id)])
        self.assertEqual(self.submissions[-1]["binding"].id, binding.id)
        self.assertEqual(self.submissions[-1]["context_commit"].expected_context_revision, 2)
        self.assertEqual(self.submissions[-1]["context_commit"].anchor, upper)
        self.assertEqual(len(self.resources()), 1)
