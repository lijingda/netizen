from __future__ import annotations

import asyncio
import unittest

from netizen.bindings import BindingNotFound, ProjectNotFound, SideTopicState
from tests import test_channel_app as fixtures


class ProjectDeletionChannelPublicationTest(unittest.IsolatedAsyncioTestCase):
    async def test_pending_root_or_seed_publication_keeps_route_until_fresh_confirmation(self):
        for gated_send in (1, 2):
            with self.subTest(gated_send=gated_send):
                fixture = fixtures.SideChannelApplicationTest()
                await fixture.asyncSetUp()
                publication: asyncio.Task[None] | None = None
                release = asyncio.Event()
                try:
                    fixture.runtime.available_capabilities = frozenset({
                        fixtures.NativeCapability.SIDE, fixtures.NativeCapability.DELETE,
                    })
                    fixture.runtime.project_side_snapshots = lambda alias: tuple(
                        side for side in fixture.runtime.side_snapshots.values()
                        if side.project_alias == alias
                    )

                    async def drain(_binding_id: str) -> None:
                        # The stub native fork already completed before send.
                        pass

                    fixture.runtime.drain_project_side_creation = drain
                    source = fixtures.FakeMessage(
                        "/side", message_id="om-source", chat_id="oc-direct",
                        chat_type="p2p", mentioned_bot=False,
                    )
                    binding = fixture.binding_for(source)
                    fixture.queue_promoted_topic(
                        chat_id="oc-direct", root_id="om-root",
                        seed_id="om-seed", topic_id="omt-side",
                    )
                    entered = asyncio.Event()
                    original_send = fixture.channel.send
                    send_count = 0

                    async def delayed_send(*args, **kwargs):
                        nonlocal send_count
                        result = await original_send(*args, **kwargs)
                        send_count += 1
                        if send_count == gated_send:
                            entered.set()
                            await release.wait()
                        return result

                    fixture.channel.send = delayed_send
                    publication = asyncio.create_task(fixture.app.handle_message(source))
                    await asyncio.wait_for(entered.wait(), 1)
                    route = fixture.store.list_side_topics()[0]
                    self.assertEqual(route.state, SideTopicState.CREATING)
                    project = fixture.store.get_project("test")
                    preview = await fixture.management.preview_project_delete(
                        alias="test", expected_revision=project.revision,
                        deadline=asyncio.get_running_loop().time() + 1,
                    )
                    result = await fixture.management.delete_project(
                        alias="test", expected_revision=preview.project.revision,
                        expected_inventory_fingerprint=preview.fingerprint,
                    )

                    self.assertFalse(result.deleted)
                    self.assertEqual(result.code, "side_creation_in_progress")
                    self.assertEqual(result.remaining_side_count, 1)
                    self.assertEqual(fixture.runtime.close_side_calls, [])
                    self.assertEqual(fixture.runtime.delete_binding_calls, [])
                    self.assertFalse(fixture.store.get_project("test").enabled)
                    self.assertFalse(fixture.store.project_delete_in_progress("test"))
                    self.assertEqual(fixture.store.get(binding.id), binding)
                    self.assertEqual(fixture.store.get_side_topic(route.id).state, SideTopicState.CREATING)

                    release.set()
                    await asyncio.wait_for(publication, 1)
                    opened = fixture.store.get_side_topic(route.id)
                    self.assertEqual(opened.state, SideTopicState.OPEN)
                    self.assertEqual(opened.root_message_id, "om-root")
                    self.assertEqual(opened.topic_id, "omt-side")
                    project = fixture.store.get_project("test")
                    preview = await fixture.management.preview_project_delete(
                        alias="test", expected_revision=project.revision,
                        deadline=asyncio.get_running_loop().time() + 1,
                    )
                    completed = await fixture.management.delete_project(
                        alias="test", expected_revision=preview.project.revision,
                        expected_inventory_fingerprint=preview.fingerprint,
                    )

                    self.assertTrue(completed.deleted)
                    with self.assertRaises(ProjectNotFound):
                        fixture.store.get_project("test")
                    with self.assertRaises(BindingNotFound):
                        fixture.store.get(binding.id)
                    tombstone = fixture.store.side_topic_for_message(
                        app_id="cli_test", chat_id="oc-direct",
                        topic_id="omt-side", root_message_id="om-root",
                    )
                    self.assertIsNotNone(tombstone)
                    self.assertEqual(tombstone.state, SideTopicState.CLOSED)
                    self.assertEqual(fixture.runtime.close_side_calls, [(route.id, SideTopicState.CLOSED)])
                    self.assertEqual(fixture.runtime.delete_binding_calls, [binding.id])
                finally:
                    release.set()
                    if publication is not None:
                        if not publication.done():
                            publication.cancel()
                        await asyncio.gather(publication, return_exceptions=True)
                    await fixture.asyncTearDown()
