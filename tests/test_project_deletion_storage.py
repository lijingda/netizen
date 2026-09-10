from __future__ import annotations

import asyncio
import concurrent.futures
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from netizen.bindings import (
    BindingStore,
    validate_channel_database,
    ProjectDeleteLimitExceeded,
    ProjectDeleting,
    ProjectDisabled,
    ProjectInventoryConflict,
    ProjectNotFound,
    ProjectRevisionConflict,
    SideTopicState,
)
from netizen.domain import FeishuScope, ScopeKind
from netizen.projects import ProjectError, ProjectRegistry, StaleProject, UnknownProject


class ProjectDeletionStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = BindingStore()
        self.project = self.store.register_project(alias="test", cwd="/tmp/test")
        self.scope = FeishuScope("cli_test", "oc_test", ScopeKind.GROUP)

    def tearDown(self) -> None:
        self.store.close()

    def binding(self, *, project_alias: str = "test"):
        return self.store.create_channel_binding(
            scope=self.scope, project_alias=project_alias, creator_id="ou_test"
        )

    def side(self, binding_id: str, source: str = "source"):
        return self.store.create_side_topic(
            app_id=self.scope.app_id,
            chat_id=self.scope.chat_id,
            parent_binding_id=binding_id,
            source_message_id=source,
            creator_id="ou_test",
            requires_mention=True,
        )

    def begin(self):
        preview = self.store.preview_project_delete("test")
        return self.store.begin_project_delete(
            alias="test", expected_revision=preview.project.revision,
            expected_inventory_fingerprint=preview.fingerprint,
        )

    def finish(self, snapshot):
        self.store.finish_project_delete(
            alias="test", expected_revision=snapshot.project.revision,
            expected_inventory_fingerprint=snapshot.fingerprint,
        )

    def test_reservation_blocks_new_bindings_sides_and_reenable_only_for_target(self):
        binding = self.binding()
        self.store.register_project(alias="other", cwd="/tmp/other")
        snapshot = self.begin()
        self.assertFalse(snapshot.project.enabled)
        self.assertTrue(self.store.project_delete_in_progress("test"))
        with self.assertRaises(ProjectDeleting):
            self.binding()
        with self.assertRaises(ProjectDeleting):
            self.side(binding.id)
        with self.assertRaises(ProjectDeleting):
            self.store.set_project_enabled(
                alias="test", enabled=True, expected_revision=snapshot.project.revision
            )
        with self.assertRaises(ProjectDeleting):
            self.store.preview_project_delete("test")
        other = self.binding(project_alias="other")
        self.assertEqual(other.project_alias, "other")
        self.store.delete_binding(binding.id)
        with self.assertRaises(ProjectDeleting):
            self.side(binding.id)
        self.finish(snapshot)
        self.assertFalse(self.store.project_delete_in_progress("test"))
        self.assertEqual(self.store.get(other.id).project_alias, "other")

    def test_empty_project_aggregate_has_no_lazy_sessions(self):
        page = asyncio.run(self.store.query_project_aggregates())
        self.assertEqual(len(page.items), 1)
        self.assertEqual(page.items[0].binding_count, 0)
        self.assertEqual(page.items[0].lazy_binding_count, 0)
        self.assertEqual(page.items[0].materialized_binding_count, 0)
        self.binding()
        page = asyncio.run(self.store.query_project_aggregates())
        self.assertEqual(page.items[0].binding_count, 1)
        self.assertEqual(page.items[0].lazy_binding_count, 1)
        self.assertEqual(page.items[0].materialized_binding_count, 0)

    def test_changed_binding_inventory_rejects_confirmation_without_disabling(self):
        self.binding()
        preview = self.store.preview_project_delete("test")
        self.binding()
        with self.assertRaises(ProjectInventoryConflict):
            self.store.begin_project_delete(
                alias="test", expected_revision=preview.project.revision,
                expected_inventory_fingerprint=preview.fingerprint,
            )
        self.assertTrue(self.store.get_project("test").enabled)
        self.assertFalse(self.store.project_delete_in_progress("test"))

    def test_concurrent_confirmations_have_one_winner(self):
        preview = self.store.preview_project_delete("test")
        barrier = threading.Barrier(2)

        def confirm():
            barrier.wait(timeout=2)
            try:
                return self.store.begin_project_delete(
                    alias="test", expected_revision=preview.project.revision,
                    expected_inventory_fingerprint=preview.fingerprint,
                )
            except ProjectDeleting:
                return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(confirm), executor.submit(confirm)]
            results = [future.result(timeout=2) for future in futures]
        winners = [result for result in results if result is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(self.store.get_project("test").revision, 2)
        self.finish(winners[0])

    def test_materialization_and_side_identity_stale_but_state_transition_does_not(self):
        binding = self.binding()
        initial = self.store.preview_project_delete("test")
        self.store.assign_native_thread_id(binding.id, "native")
        materialized = self.store.preview_project_delete("test")
        self.assertNotEqual(initial.fingerprint, materialized.fingerprint)
        side = self.side(binding.id)
        creating = self.store.preview_project_delete("test")
        self.store.set_side_topic_root(side.id, "root")
        rooted = self.store.preview_project_delete("test")
        self.assertNotEqual(creating.fingerprint, rooted.fingerprint)
        self.store.open_side_topic(side.id, "topic")
        opened = self.store.preview_project_delete("test")
        self.store.transition_side_topic(side.id, SideTopicState.CLOSED)
        closed = self.store.preview_project_delete("test")
        self.assertEqual(opened.fingerprint, closed.fingerprint)
        self.assertEqual(closed.bindings[0].binding.native_thread_id, "native")
        self.assertEqual(closed.sides[0].state, SideTopicState.CLOSED)

    def test_finish_requires_no_bindings_or_live_routes_even_after_parent_removed(self):
        binding = self.binding()
        side = self.side(binding.id)
        snapshot = self.begin()
        with self.assertRaises(ProjectInventoryConflict):
            self.finish(snapshot)
        self.store.delete_binding(binding.id)
        with self.assertRaises(ProjectInventoryConflict):
            self.finish(snapshot)
        self.store.transition_side_topic(side.id, SideTopicState.FAILED)
        self.finish(snapshot)
        with self.assertRaises(ProjectNotFound):
            self.store.get_project("test")
        self.assertEqual(self.store.list_projects(), [])
        self.assertEqual(asyncio.run(self.store.query_project_aggregates()).items, ())
        self.assertEqual(self.store.get_side_topic(side.id).state, SideTopicState.FAILED)

    def test_release_preserves_disabled_project_and_rejects_stale_reservation(self):
        snapshot = self.begin()
        with self.assertRaises(ProjectRevisionConflict):
            self.store.release_project_delete(
                alias="test", expected_revision=snapshot.project.revision - 1
            )
        self.store.release_project_delete(
            alias="test", expected_revision=snapshot.project.revision
        )
        self.assertFalse(self.store.get_project("test").enabled)
        with self.assertRaises(ProjectDisabled):
            self.binding()
        next_snapshot = self.begin()
        with self.assertRaises(ProjectRevisionConflict):
            self.store.release_project_delete(
                alias="test", expected_revision=snapshot.project.revision
            )
        self.assertTrue(self.store.project_delete_in_progress("test"))
        self.finish(next_snapshot)

    def test_runtime_orphan_sides_join_confirmation_and_prevent_early_completion(self):
        binding = self.binding()
        side = self.side(binding.id)
        self.store.delete_binding(binding.id)
        preview = self.store.preview_project_delete("test", extra_side_ids=(side.id,))
        self.assertEqual(preview.bindings, ())
        self.assertEqual(tuple(item.id for item in preview.sides), (side.id,))
        with self.assertRaises(ProjectInventoryConflict):
            self.store.begin_project_delete(
                alias="test", expected_revision=preview.project.revision,
                expected_inventory_fingerprint=preview.fingerprint,
            )
        snapshot = self.store.begin_project_delete(
            alias="test", expected_revision=preview.project.revision,
            expected_inventory_fingerprint=preview.fingerprint,
            extra_side_ids=(side.id,),
        )
        with self.assertRaises(ProjectInventoryConflict):
            self.finish(snapshot)
        with self.assertRaises(ProjectDeleting):
            self.side(binding.id, "new-source")
        self.store.transition_side_topic(side.id, SideTopicState.FAILED)
        self.finish(snapshot)
        self.assertTrue(self.store.get_project("test", include_deleted=True).deleted)
        self.assertEqual(self.store.get_side_topic(side.id).parent_binding_id, binding.id)

    def test_extra_sides_must_exist_and_not_belong_to_another_registered_project(self):
        with self.assertRaises(ProjectInventoryConflict):
            self.store.preview_project_delete("test", extra_side_ids=("missing",))
        self.store.register_project(alias="other", cwd="/tmp/other")
        other = self.binding(project_alias="other")
        side = self.side(other.id)
        with self.assertRaises(ProjectInventoryConflict):
            self.store.preview_project_delete("test", extra_side_ids=(side.id,))
        self.assertTrue(self.store.get_project("test").enabled)

    def test_joined_and_runtime_side_inventory_deduplicates_and_shares_one_limit(self):
        binding = self.binding()
        side = self.side(binding.id)
        preview = self.store.preview_project_delete(
            "test", limit=1, extra_side_ids=(side.id,),
        )
        self.assertEqual(len(preview.sides), 1)
        orphan = self.store.create_side_topic(
            app_id="cli_test", chat_id="oc_orphan", parent_binding_id="old-parent",
            source_message_id="orphan-source", creator_id="ou_test", requires_mention=False,
        )
        with self.assertRaises(ProjectDeleteLimitExceeded):
            self.store.preview_project_delete("test", limit=1, extra_side_ids=(orphan.id,))

    def test_limits_reject_whole_inventory_instead_of_truncating(self):
        binding = self.binding()
        self.side(binding.id, "one")
        self.side(binding.id, "two")
        with self.assertRaises(ProjectDeleteLimitExceeded):
            self.store.preview_project_delete("test", limit=1)
        for side in self.store.list_side_topics():
            self.store.transition_side_topic(side.id, SideTopicState.FAILED)
        self.binding()
        with self.assertRaises(ProjectDeleteLimitExceeded):
            self.store.preview_project_delete("test", limit=1)
        self.assertTrue(self.store.get_project("test").enabled)


class ProjectDeletionRegistryTest(unittest.TestCase):
    def test_tombstone_survives_restart_yaml_does_not_resurrect_and_explicit_reuse_is_monotonic(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cwd = root / "original"
            cwd.mkdir()
            retained = cwd / "keep.txt"
            retained.write_text("project source")
            database = root / "channel.sqlite3"
            store = BindingStore(database)
            try:
                registry = ProjectRegistry(store=store, project_root=root, projects={"test": cwd})
                preview = registry.preview_delete("test")
                reserved = registry.begin_delete(
                    alias="test", expected_revision=preview.project.revision,
                    expected_inventory_fingerprint=preview.fingerprint,
                )
                with self.assertRaises(ProjectError):
                    registry.set_enabled(
                        alias="test", enabled=True,
                        expected_revision=reserved.project.revision,
                    )
                registry.finish_delete(
                    alias="test", expected_revision=reserved.project.revision,
                    expected_inventory_fingerprint=reserved.fingerprint,
                )
                tombstone = store.get_project("test", include_deleted=True)
                self.assertTrue(tombstone.deleted)
                self.assertEqual(retained.read_text(), "project source")
            finally:
                store.close()
            store = BindingStore(database)
            try:
                registry = ProjectRegistry(
                    store=store, project_root=root, projects={"test": root / "missing"}
                )
                self.assertEqual(registry.list(), ())
                self.assertEqual(registry.aliases(), ())
                with self.assertRaises(UnknownProject):
                    registry.resolve("test")
                new_cwd = root / "new"
                new_cwd.mkdir()
                registered = registry.register(
                    alias="test", path=str(new_cwd), create_directory=False
                )
                self.assertGreater(registered.revision, tombstone.revision)
                self.assertEqual(registered.cwd, new_cwd.resolve())
                with self.assertRaises(StaleProject):
                    registry.set_enabled(
                        alias="test", enabled=False,
                        expected_revision=preview.project.revision,
                    )
            finally:
                store.close()


class ProjectTombstoneSchemaTest(unittest.TestCase):
    def test_current_schema_missing_routes_is_rejected_without_recreating_them(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "channel.sqlite3"
            BindingStore(path).close()
            connection = sqlite3.connect(path)
            connection.execute("DROP TABLE side_topics")
            connection.commit()
            connection.close()
            before = path.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "missing required tables: side_topics"):
                validate_channel_database(path)
            self.assertEqual(path.read_bytes(), before)

    def test_current_schema_wrong_tombstone_shape_is_rejected_without_mutation(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "channel.sqlite3"
            BindingStore(path).close()
            connection = sqlite3.connect(path)
            connection.execute("ALTER TABLE projects DROP COLUMN deleted")
            connection.execute("ALTER TABLE projects ADD COLUMN deleted TEXT DEFAULT '0'")
            connection.commit()
            connection.close()
            before = path.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "tombstone column has invalid shape"):
                validate_channel_database(path)
            self.assertEqual(path.read_bytes(), before)
