import asyncio
from unittest import IsolatedAsyncioTestCase

from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.management.service import BackendUnsupported, CurrentSideTarget, ExactBindingTarget, SessionInventoryState, SessionQuery
from netizen_cli.runtime.contracts import ThreadOccupied
from tests.support.channel_messages import FakeMessage
from tests.test_claude_channel import owner_channel


class ClaudeResourcesTests(IsolatedAsyncioTestCase):
    async def test_project_preview_and_commit_block_materialization(self):
        async with owner_channel() as (app, fixture, clients, clock, runtime):
            clock[0] = 2.
            scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
            created = await app._management.create_current_binding(scope=scope, creator_id="ou_user", project_alias="test")
            revision = fixture.store.get_project("test").revision
            kwargs = {"alias": "test", "expected_revision": revision, "deadline": asyncio.get_running_loop().time() + 5}
            preview = await app._management.preview_project_delete(**kwargs)
            gate = asyncio.Event()
            clients.configure = lambda client: setattr(client, "connect_gate", gate)
            receipt = asyncio.create_task(app.handle_message(FakeMessage("go", message_id="om")))
            try:
                await asyncio.wait_for(clients.created.wait(), 1)
                with self.assertRaises(ThreadOccupied):
                    await app._management.delete_project(**kwargs, expected_inventory_fingerprint=preview.fingerprint)
                fixture.store.require_project_not_deleting("test")
            finally:
                gate.set()
                await asyncio.wait_for(receipt, 2)
            client = clients.calls[0]
            client.init()
            client.result()
            self.assertTrue(await runtime.wait_idle(2))
            for action in (app._management.preview_project_delete(**kwargs),
                           app._management.delete_project(**kwargs, expected_inventory_fingerprint=preview.fingerprint)):
                with self.assertRaisesRegex(BackendUnsupported, "Project 含已建立的 Claude 会话"):
                    await action
            fixture.store.require_project_not_deleting("test")
            self.assertEqual(fixture.store.get(created.binding.id).native_thread_id, client.session)
            usage = await runtime.resource_usage(created.binding.id)
            self.assertEqual(usage, {"cwd": str(fixture.project), "status": "unknown"})
            self.assertEqual(len(clients.calls), 1)

    async def test_local_inventory_and_confirmed_lazy_cleanup(self):
        async with owner_channel() as (app, fx, clients, clock, runtime):
            clock[0] = 2.
            scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
            binding = (await app._management.create_current_binding(scope=scope, creator_id="ou_user", project_alias="test")).binding
            deadline = asyncio.get_running_loop().time() + 4
            page = await app._management.query_sessions(deadline=deadline)
            self.assertEqual([row.record.binding.id for row in page.items], [binding.id])
            for query in (SessionQuery(sort_by="updated_at"), SessionQuery(inventory_state=SessionInventoryState.ARCHIVED)):
                with self.assertRaises(BackendUnsupported):
                    await app._management.query_sessions(query=query, deadline=deadline)
            with self.assertRaises(BackendUnsupported):
                await app._management.stop_side(target=CurrentSideTarget("missing", "cli_test", "oc_direct", None, None))
            with self.assertRaises(BackendUnsupported):
                await app._management.delete_exact_lazy_binding(target=ExactBindingTarget(scope.key, binding.id, binding.id))
            args = {"alias": "test", "deadline": deadline, "expected_revision": fx.store.get_project("test").revision}
            preview = await app._management.preview_project_delete(**args)
            result = await app._management.delete_project(**args, expected_inventory_fingerprint=preview.fingerprint)
            self.assertTrue(result.deleted, result)
            self.assertFalse(fx.store.has_bindings(scope.key))
            self.assertFalse(clients.calls)
