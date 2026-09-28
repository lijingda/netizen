from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from netizen.bindings import BindingStore
from netizen.channel_app import ChannelApplication
from netizen.codex_runtime import CodexRuntime
from netizen.management import (
    InstanceManagementService,
    ManagementRuntimePort,
    ScopeCoordinator,
)
from netizen.projects import ProjectRegistry
from netizen.session_settings import BindingTaskFeedback, SessionSettings
from tests.support.channel_messages import FakeChannel, FakeMessage, plain_prompt_projection
from tests.test_codex_runtime import FakeCodex, FakeTerminalCleanup


class DefaultsRuntimeTest(unittest.IsolatedAsyncioTestCase):
    """Keep real input preparation, exact admission and native identity writes."""

    async def asyncSetUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.default_cwd = self.root / "default"
        self.existing_cwd = self.root / "existing"
        self.default_cwd.mkdir()
        self.existing_cwd.mkdir()
        self.store = self.enterContext(closing(BindingStore()))
        self.channel = FakeChannel()
        self.channel.chat_types["oc_direct"] = "p2p"
        self.codex = FakeCodex()
        self.runtime = CodexRuntime(
            codex=self.codex, bindings=self.store,
            terminal_cleanup=FakeTerminalCleanup(self.codex.events),
            automatic_thread_naming=False,
        )
        self.projects = ProjectRegistry(
            store=self.store, project_root=self.root,
            projects={"default": self.default_cwd, "existing": self.existing_cwd},
        )
        self.management = InstanceManagementService(
            bindings=self.store, projects=self.projects,
            runtime=ManagementRuntimePort(self.runtime),
            scope_coordinator=ScopeCoordinator(),
        )
        self.app = ChannelApplication(
            app_id="cli_test", channel=self.channel, runtime=self.runtime,
            bindings=self.store, projects=self.projects, management=self.management,
        )
        self.addAsyncCleanup(self.management.close)
        self.addAsyncCleanup(self.app.close)
        self.addAsyncCleanup(self.runtime.cancel_tasks)
        self.addCleanup(self.runtime.close_admission)
        self.feedback = BindingTaskFeedback(False, False, False)
        self.message = FakeMessage("执行原始请求", message_id="om_original")
        self.scope = self.app._scope(self.message)
        self.store.defaults.save(
            app_id="cli_test", kind="chat", chat_id=self.scope.chat_id,
            keyword=None, project="default",
            session_settings=SessionSettings(task_feedback=self.feedback),
            rule_id=None, expected_revision=None,
        )

    async def create_manual(self, project):
        created = await self.management.create_current_binding(
            scope=self.scope, creator_id="ou_manual", project_alias=project,
            task_feedback=self.feedback,
        )
        return created.binding

    async def handle_with_manual_creation(self, *, missing_project=False):
        """Insert a manual /new while the missing-current input resolves defaults."""
        defaults = self.management.defaults
        validate = defaults.validate
        selected = []

        async def create_during_validation(rule):
            project = await validate(rule)
            selected.append(await self.create_manual("existing"))
            if missing_project:
                self.existing_cwd.rename(self.root / "moved-existing")
            return project

        with patch.object(defaults, "validate", create_during_validation):
            await self.app.handle_message(self.message)
        self.assertEqual(len(selected), 1)
        return selected[0]

    async def test_existing_project_failure_does_not_retarget_to_later_current(self):
        original_io = self.management._blocking_io.submit

        async def switch_during_project_read(function, *args, deadline=None, **kwargs):
            if function == self.projects.resolve_for_binding and args == ("existing",):
                # The old conditional-create path had already selected X, then
                # yielded for this read. A manual /new could select Y before
                # X's real Project error reached the default fallback handler.
                await self.create_manual("default")
            return await original_io(function, *args, deadline=deadline, **kwargs)

        with patch.object(self.management._blocking_io, "submit", switch_during_project_read):
            selected = await self.handle_with_manual_creation(missing_project=True)

        self.assertEqual(self.codex.start_kwargs, [])
        self.assertEqual(self.codex.turn_inputs, [])
        self.assertIsNone(self.store.get(selected.id).native_thread_id)
        self.assertEqual(self.store.active_binding(self.scope.key).id, selected.id)
        self.assertEqual(len(self.channel.replies), 1)
        self.assertIn("Project existing cwd", self.channel.replies[0][1])
        self.assertNotIn("默认会话配置不可用", self.channel.replies[0][1])

    async def test_reused_binding_runs_in_its_own_project(self):
        selected = await self.handle_with_manual_creation()

        self.assertEqual(self.codex.start_kwargs, [{"cwd": str(self.existing_cwd.resolve())}])
        self.assertEqual(len(self.codex.turn_inputs), 1)
        native_id, native_input = self.codex.turn_inputs[0]
        self.assertEqual(self.store.get(selected.id).native_thread_id, native_id)
        self.assertEqual(self.store.active_binding(self.scope.key).id, selected.id)
        self.assertEqual(len(self.store.list_bindings(self.scope.key)), 1)
        request, source = plain_prompt_projection(native_input)
        self.assertEqual(request, self.message.body_text)
        self.assertEqual(source["message_id"], self.message.id)
        self.assertEqual(self.channel.replies, [])

    async def test_switch_during_preparation_rejects_without_retargeting(self):
        prepare = self.app._input_preparer.prepare
        replacements = []

        async def switch_during_preparation(**kwargs):
            replacements.append(await self.create_manual("default"))
            return await prepare(**kwargs)

        with patch.object(self.app._input_preparer, "prepare", switch_during_preparation):
            selected = await self.handle_with_manual_creation()

        self.assertEqual(len(replacements), 1)
        replacement = replacements[0]
        self.assertEqual(self.store.active_binding(self.scope.key).id, replacement.id)
        self.assertNotEqual(selected.id, replacement.id)
        self.assertIsNone(self.store.get(selected.id).native_thread_id)
        self.assertIsNone(self.store.get(replacement.id).native_thread_id)
        self.assertEqual(self.codex.start_kwargs, [])
        self.assertEqual(self.codex.turn_inputs, [])
        self.assertEqual(len(self.channel.replies), 1)
        self.assertIn("active 会话已切换", self.channel.replies[0][1])
        self.assertIn("本条消息未执行", self.channel.replies[0][1])


if __name__ == "__main__":
    unittest.main()
