from __future__ import annotations

import asyncio
import subprocess
import unittest
from unittest.mock import patch

from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.worktrees.errors import WorktreeError
from netizen_cli.worktrees.settings import WorktreeSettingsService
from tests.support.channel_fixtures import channel_fixture


class DefaultWorktreeCreationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = await self.enterAsyncContext(channel_fixture())
        self.management = self.fixture.management
        self.store = self.fixture.store
        for args in (
            ("init", "-q", "--initial-branch=main"),
            ("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
             "commit", "-q", "--allow-empty", "-m", "initial"),
        ):
            subprocess.run(
                ["git", *args], cwd=self.fixture.project,
                capture_output=True, check=True, timeout=10,
            )
        self.worktrees = self.management.enable_worktrees(
            settings=WorktreeSettingsService(default_root=self.fixture.project_root / "managed"),
        )
        self.scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)

    async def create(self, *, scope=None, **kwargs):
        return await self.management.create_current_binding(
            scope=scope or self.scope, creator_id="ou_user", project_alias="test",
            only_if_empty=True, **kwargs,
        )

    async def joined(self, event, **kwargs):
        event.set()
        return await self.create(**kwargs)

    async def test_cancelling_waiter_preserves_owner_and_single_worktree(self):
        started, release, joined = asyncio.Event(), asyncio.Event(), asyncio.Event()
        create = self.worktrees.create

        async def delayed(**kwargs):
            resource = await create(**kwargs)
            started.set()
            await release.wait()
            return resource

        with patch.object(self.worktrees, "create", delayed):
            owner = asyncio.create_task(self.create(worktree_create={"mode": "detached"}))
            await asyncio.wait_for(started.wait(), 5)
            waiter = asyncio.create_task(self.joined(joined, worktree_create={"mode": "detached"}))
            await asyncio.wait_for(joined.wait(), 1)
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            release.set()
            created = await asyncio.wait_for(owner, 5)
        self.assertTrue(created.created)
        resources = self.store.worktrees.list_for_project("test")
        self.assertEqual([record.id for record in resources], [created.binding.worktree_id])
        self.assertEqual(len(self.store.list_bindings(self.scope.key)), 1)

    async def test_cancelling_owner_releases_waiters_and_keeps_created_resource(self):
        started, joined = asyncio.Event(), asyncio.Event()
        create = self.worktrees.create

        async def delayed(**kwargs):
            resource = await create(**kwargs)
            started.set()
            await asyncio.Event().wait()
            return resource

        with patch.object(self.worktrees, "create", delayed):
            owner = asyncio.create_task(self.create(worktree_create={"mode": "detached"}))
            await asyncio.wait_for(started.wait(), 5)
            waiter = asyncio.create_task(self.joined(joined, worktree_create={"mode": "detached"}))
            await asyncio.wait_for(joined.wait(), 1)
            owner.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await owner
            with self.assertRaises(WorktreeError) as raised:
                await asyncio.wait_for(waiter, 1)
        self.assertEqual(raised.exception.code, "creation_interrupted")
        self.assertIsNone(self.store.active_binding(self.scope.key))
        resources = self.store.worktrees.list_for_project("test")
        self.assertEqual(len(resources), 1)
        # A new explicit attempt can select the retained result, without
        # inheriting a cancelled reservation or creating another directory.
        recovered = await self.create(worktree_id=resources[0].id)
        self.assertTrue(recovered.created)
        self.assertEqual(recovered.binding.worktree_id, resources[0].id)

    async def test_different_scopes_create_worktrees_concurrently(self):
        both_started, release = asyncio.Event(), asyncio.Event()
        create = self.worktrees.create
        calls = 0

        async def delayed(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                both_started.set()
            await release.wait()
            return await create(**kwargs)

        other = FeishuScope("cli_test", "oc_direct", ScopeKind.TOPIC, "topic_other")
        with patch.object(self.worktrees, "create", delayed):
            tasks = [
                asyncio.create_task(self.create(scope=scope, worktree_create={"mode": "detached"}))
                for scope in (self.scope, other)
            ]
            try:
                await asyncio.wait_for(both_started.wait(), 5)
            finally:
                release.set()
            first, second = await asyncio.wait_for(asyncio.gather(*tasks), 10)
        self.assertNotEqual(first.binding.worktree_id, second.binding.worktree_id)
        self.assertNotEqual(first.binding.working_cwd, second.binding.working_cwd)

    async def test_selection_and_binding_commit_recheck_worktree_delete_reservation(self):
        resource = await self.worktrees.create(project_alias="test")
        select = self.worktrees.select

        async def delete_after_selection(**kwargs):
            record = await select(**kwargs)
            snapshot = self.store.worktrees.preview_delete(record.id)
            self.store.worktrees.reserve_delete(record.id, expected_fingerprint=snapshot.fingerprint)
            return record

        try:
            with patch.object(self.worktrees, "select", delete_after_selection):
                with self.assertRaises(WorktreeError) as raised:
                    await self.create(worktree_id=resource["id"])
            self.assertEqual(raised.exception.code, "unavailable")
            self.assertIsNone(self.store.active_binding(self.scope.key))
        finally:
            self.store.worktrees.release_delete(resource["id"])

    async def test_deleted_selected_resource_has_safe_unavailable_error(self):
        resource = await self.worktrees.create(project_alias="test")
        select = self.worktrees.select

        async def delete_after_selection(**kwargs):
            record = await select(**kwargs)
            preview = await self.worktrees.preview_delete(record.id)
            await self.worktrees.delete(record.id, expected_fingerprint=preview["fingerprint"])
            return record

        with patch.object(self.worktrees, "select", delete_after_selection):
            with self.assertRaises(WorktreeError) as raised:
                await self.create(worktree_id=resource["id"])
        self.assertEqual(raised.exception.code, "unavailable")
        self.assertIsNone(self.store.active_binding(self.scope.key))

    async def test_manual_winner_retention_notice_belongs_only_to_creator(self):
        started, release, joined = asyncio.Event(), asyncio.Event(), asyncio.Event()
        create = self.worktrees.create

        async def delayed(**kwargs):
            resource = await create(**kwargs)
            started.set()
            await release.wait()
            return resource

        with patch.object(self.worktrees, "create", delayed):
            owner = asyncio.create_task(self.create(worktree_create={"mode": "detached"}))
            await asyncio.wait_for(started.wait(), 5)
            waiter = asyncio.create_task(self.joined(joined, worktree_create={"mode": "detached"}))
            await asyncio.wait_for(joined.wait(), 1)
            manual = await self.management.create_current_binding(
                scope=self.scope, creator_id="ou_manual", project_alias="test",
            )
            release.set()
            first, second = await asyncio.wait_for(asyncio.gather(owner, waiter), 5)
        self.assertEqual((first.binding.id, second.binding.id), (manual.binding.id, manual.binding.id))
        self.assertFalse(first.created)
        self.assertFalse(second.created)
        self.assertIsNotNone(first.retained_worktree_cwd)
        self.assertIsNone(second.retained_worktree_cwd)
        self.assertEqual(len(self.store.worktrees.list_for_project("test")), 1)
