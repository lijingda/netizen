from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from netizen.domain import FeishuScope, ScopeKind
from netizen.session_settings import BindingTaskFeedback
from tests.support.channel_fixtures import channel_fixture


class ConditionalCurrentCreationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = await self.enterAsyncContext(channel_fixture())
        self.management = self.fixture.management
        self.store = self.fixture.store
        self.scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)

    async def create(self, *, only_if_empty=True, feedback=BindingTaskFeedback()):
        return await self.management.create_current_binding(
            scope=self.scope, creator_id="ou_user", project_alias="test",
            task_feedback=feedback, only_if_empty=only_if_empty,
        )

    async def test_concurrent_conditional_creation_makes_one_binding(self):
        reached = asyncio.Event()
        resolved = 0

        async def resolve(function, *args, deadline=None, **kwargs):
            nonlocal resolved
            result = function(*args, **kwargs)
            resolved += 1
            if resolved == 2:
                reached.set()
            return result

        with patch.object(self.management._blocking_io, "submit", resolve):
            async with self.management._scope_coordinator.hold(self.scope.key):
                tasks = [asyncio.create_task(self.create()) for _ in range(2)]
                await asyncio.wait_for(reached.wait(), 1)
            first, second = await asyncio.wait_for(asyncio.gather(*tasks), 1)
        self.assertEqual(first.binding.id, second.binding.id)
        self.assertEqual(sorted((first.created, second.created)), [False, True])
        self.assertEqual(len(self.store.list_bindings(self.scope.key)), 1)
        self.assertEqual(self.fixture.runtime.active_binding_change_calls, [(None, first.binding.id)])

    async def test_manual_creation_first_returns_its_settings_without_overwrite(self):
        manual = await self.create(only_if_empty=False, feedback=BindingTaskFeedback(True, False, False))
        automatic = await self.create(feedback=BindingTaskFeedback(False, True, True))
        self.assertFalse(automatic.created)
        self.assertEqual(automatic.binding, manual.binding)
        self.assertIsNone(automatic.project)
        self.assertEqual(len(self.store.list_bindings(self.scope.key)), 1)

    async def test_existing_binding_defers_project_check_to_ordinary_input(self):
        other = self.fixture.project_root / "other"
        other.mkdir()
        self.fixture.projects.register(alias="other", path=str(other), create_directory=False)
        manual = await self.management.create_current_binding(
            scope=self.scope, creator_id="ou_user", project_alias="other",
        )
        other.rename(self.fixture.project_root / "moved-other")
        with patch.object(self.fixture.projects, "resolve_for_binding") as resolve_existing:
            automatic = await self.create()
        resolve_existing.assert_not_called()
        self.assertFalse(automatic.created)
        self.assertIsNone(automatic.project)
        self.assertEqual(automatic.binding, manual.binding)
        self.assertEqual(len(self.store.list_bindings(self.scope.key)), 1)

    async def test_manual_creation_after_automatic_still_creates_and_switches(self):
        automatic = await self.create()
        manual = await self.create(only_if_empty=False)
        self.assertTrue(automatic.created)
        self.assertTrue(manual.created)
        self.assertNotEqual(automatic.binding.id, manual.binding.id)
        self.assertFalse(self.store.get(automatic.binding.id).active)
        self.assertEqual(self.store.active_binding(self.scope.key).id, manual.binding.id)
        self.assertEqual(len(self.store.list_bindings(self.scope.key)), 2)


if __name__ == "__main__":
    unittest.main()
