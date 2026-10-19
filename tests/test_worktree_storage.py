from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from netizen_cli import database_migrations as migrations
from netizen_cli.bindings import BindingConflict, BindingStore, ProjectConflict, ProjectInventoryConflict, validate_channel_database
from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.migrations import v15
from netizen_cli.migrations.schema import require_schema
from netizen_cli.worktrees.models import WorktreeConflict, WorktreeNotFound
from netizen_cli.worktrees.store import WorktreeStore


FIXTURES = Path(__file__).parent / "fixtures"


class WorktreeStorageTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = BindingStore()
        self.addCleanup(self.store.close)
        self.project = self.store.register_project(alias="repo", cwd="/repo")
        self.scope = FeishuScope("app", "chat", ScopeKind.DIRECT)

    def reserve(self, suffix: str = "one"):
        return self.store.worktrees.register(
            project_alias="repo", expected_project_revision=self.store.get_project("repo").revision,
            root=f"/worktrees/{suffix}", cwd=f"/worktrees/{suffix}/src", common_dir="/repo/.git",
        )

    def ready(self, suffix: str = "one"):
        record = self.reserve(suffix)
        return self.store.worktrees.mark_ready(
            record.id, git_dir=f"/repo/.git/worktrees/{suffix}", identity_token=f"identity-{suffix}",
        )

    def bind(self, record):
        return self.store.create_channel_binding(
            scope=self.scope, project_alias="repo", creator_id="user",
            working_cwd=record.cwd, worktree_id=record.id,
        )

    def delete_project(self) -> None:
        snapshot = self.store.preview_project_delete("repo")
        reserved = self.store.begin_project_delete(
            alias="repo", expected_revision=snapshot.project.revision,
            expected_inventory_fingerprint=snapshot.fingerprint,
        )
        self.store.finish_project_delete(
            alias="repo", expected_revision=reserved.project.revision,
            expected_inventory_fingerprint=reserved.fingerprint,
        )

    async def test_discard_unstarted_only_clears_exact_live_reservation_not_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            sentinel = root / "external-file"
            sentinel.write_text("keep")
            record = self.store.worktrees.register(project_alias="repo", expected_project_revision=1,
                root=str(root), cwd=str(root), common_dir="/repo/.git")
            with self.assertRaises(WorktreeConflict):
                self.store.worktrees.discard_unstarted(replace(record, revision=record.revision + 1))
            self.store.worktrees.discard_unstarted(record)
            self.assertEqual(sentinel.read_text(), "keep")
            with self.assertRaises(WorktreeNotFound):
                self.store.worktrees.get(record.id)
            self.assertEqual(self.store.preview_project_delete("repo").worktrees, ())

    async def test_discard_unstarted_cannot_forget_unknown_ready_or_restarted_resources(self) -> None:
        pending = self.reserve("pending")
        with self.assertRaises(WorktreeConflict):
            WorktreeStore(self.store).discard_unstarted(pending)
        unknown = self.store.worktrees.mark_unknown(pending.id)
        ready = self.ready("ready")
        self.bind(ready)
        for record in (unknown, ready):
            with self.subTest(state=record.creation_state), self.assertRaises(WorktreeConflict):
                self.store.worktrees.discard_unstarted(record)
            self.assertEqual(self.store.worktrees.get(record.id), record)

    async def test_registration_survives_binding_failure_and_paginates(self) -> None:
        first = self.ready()
        second = self.ready("two")
        with self.assertRaises(WorktreeConflict):
            self.store.create_channel_binding(
                scope=self.scope, project_alias="repo", creator_id="user",
                working_cwd="/wrong", worktree_id=first.id,
            )
        page = await self.store.worktrees.list_page(limit=1)
        next_page = await self.store.worktrees.list_page(limit=1, cursor=page.next_cursor)
        self.assertEqual({item.id for item in (*page.items, *next_page.items)}, {first.id, second.id})
        self.assertIsNone(next_page.next_cursor)
        self.assertEqual(self.store.worktrees.find_by_cwd(first.cwd), first)

    async def test_delete_preserves_selected_cwd_and_does_not_delete_sessions(self) -> None:
        record = self.ready()
        binding = self.bind(record)
        snapshot = self.store.worktrees.preview_delete(record.id)
        self.store.worktrees.reserve_delete(record.id, expected_fingerprint=snapshot.fingerprint)
        with self.assertRaises(WorktreeConflict):
            self.bind(record)
        self.store.worktrees.finish_delete(record.id, expected_fingerprint=snapshot.fingerprint)
        kept = self.store.get(binding.id)
        self.assertEqual(kept.working_cwd, record.cwd)
        self.assertIsNone(kept.worktree_id)
        with self.assertRaises(WorktreeNotFound):
            self.store.worktrees.get(record.id)

    async def test_delete_fingerprint_detects_new_association_and_materialization(self) -> None:
        record = self.ready()
        snapshot = self.store.worktrees.preview_delete(record.id)
        binding = self.bind(record)
        with self.assertRaises(WorktreeConflict):
            self.store.worktrees.reserve_delete(record.id, expected_fingerprint=snapshot.fingerprint)
        snapshot = self.store.worktrees.preview_delete(record.id)
        self.store.assign_native_thread_id(binding.id, "native")
        with self.assertRaises(WorktreeConflict):
            self.store.worktrees.reserve_delete(record.id, expected_fingerprint=snapshot.fingerprint)

    async def test_project_generation_changes_only_after_reregister(self) -> None:
        record = self.ready()
        disabled = self.store.set_project_enabled(alias="repo", enabled=False, expected_revision=1)
        enabled = self.store.set_project_enabled(alias="repo", enabled=True, expected_revision=disabled.revision)
        self.assertEqual(enabled.generation, 1)
        self.delete_project()
        new = self.store.register_project(alias="repo", cwd="/repo")
        self.assertEqual(new.generation, 2)
        self.assertEqual(self.store.worktrees.get(record.id), record)
        self.assertEqual(self.store.worktrees.list_for_project("repo", generation=new.generation), ())
        with self.assertRaises(WorktreeConflict):
            self.bind(record)

    async def test_project_delete_tracks_worktrees_and_blocks_pending_creation(self) -> None:
        preview = self.store.preview_project_delete("repo")
        record = self.reserve()
        with self.assertRaises(WorktreeConflict):
            self.store.preview_project_delete("repo")
        self.store.worktrees.mark_unknown(record.id)
        with self.assertRaises(ProjectInventoryConflict):
            self.store.begin_project_delete(
                alias="repo", expected_revision=preview.project.revision,
                expected_inventory_fingerprint=preview.fingerprint,
            )
        current = self.store.preview_project_delete("repo")
        self.assertEqual(current.worktrees[0].creation_state, "unknown")

    async def test_independent_origin_record_survives_creator_deletion(self) -> None:
        creator = self.store.create_channel_binding(scope=self.scope, project_alias="repo", creator_id="user")
        record = self.store.worktrees.register(
            project_alias="repo", expected_project_revision=1, root="/worktrees/one",
            cwd="/worktrees/one", common_dir="/repo/.git", origin_binding_id=creator.id,
        )
        self.store.worktrees.mark_unknown(record.id)
        self.store.delete_binding(creator.id)
        self.assertEqual(self.store.worktrees.get(record.id).origin_binding_id, creator.id)
        self.assertEqual(self.store.worktrees.bindings(record.id), ())

    async def test_ready_identity_cannot_be_replaced_and_pending_is_not_bindable(self) -> None:
        pending = self.reserve()
        with self.assertRaises(WorktreeConflict):
            self.bind(pending)
        record = self.store.worktrees.mark_ready(pending.id, git_dir="/repo/.git/worktrees/one", identity_token="first")
        with self.assertRaises(WorktreeConflict):
            self.store.worktrees.mark_ready(record.id, git_dir=record.git_dir, identity_token="replacement")
        self.assertEqual(self.store.worktrees.mark_unknown(record.id), record)

    async def test_native_cwd_inheritance_matches_managed_identity_and_delete_admission(self) -> None:
        record = self.ready()
        inherited = self.store.create_channel_binding(
            scope=self.scope, project_alias="repo", creator_id="user", working_cwd=record.cwd,
        )
        self.assertEqual(inherited.worktree_id, record.id)
        snapshot = self.store.worktrees.preview_delete(record.id)
        self.store.worktrees.reserve_delete(record.id, expected_fingerprint=snapshot.fingerprint)
        with self.assertRaises(WorktreeConflict):
            self.store.worktrees.require_binding_not_deleting(inherited.id)
        with self.assertRaises(WorktreeConflict):
            self.store.create_channel_binding(
                scope=self.scope, project_alias="repo", creator_id="user", working_cwd=record.cwd,
            )
        self.store.worktrees.release_delete(record.id)
        self.store.worktrees.require_binding_not_deleting(inherited.id)

    async def test_creation_origin_is_protected_but_not_cascade_association(self) -> None:
        origin = self.store.create_channel_binding(scope=self.scope, project_alias="repo", creator_id="user")
        record = self.store.worktrees.register(
            project_alias="repo", expected_project_revision=1, root="/worktrees/one",
            cwd="/worktrees/one", common_dir="/repo/.git", origin_binding_id=origin.id,
        )
        record = self.store.worktrees.mark_ready(record.id, git_dir="/repo/.git/worktrees/one", identity_token="origin")
        snapshot = self.store.worktrees.preview_delete(record.id)
        self.assertEqual(snapshot.bindings, ())
        self.store.worktrees.reserve_delete(record.id, expected_fingerprint=snapshot.fingerprint)
        with self.assertRaises(WorktreeConflict):
            self.store.worktrees.require_binding_not_deleting(origin.id)
        self.store.worktrees.finish_delete(record.id, expected_fingerprint=snapshot.fingerprint)
        self.store.worktrees.require_binding_not_deleting(origin.id)

    async def test_persistent_fork_preserves_source_workspace_without_creating_one(self) -> None:
        record = self.ready()
        source = self.bind(record)
        self.store.assign_native_thread_id(source.id, "native-source")
        source = self.store.get(source.id)
        fork = self.store.create_fork_binding(
            scope=FeishuScope("app", "chat", ScopeKind.TOPIC, "fork-topic"), source=source,
            native_thread_id="native-fork", root_message_id="root", creator_id="user",
            expected_project_revision=1, target_chat_kind="p2p",
        )
        self.assertEqual((fork.working_cwd, fork.worktree_id), (record.cwd, record.id))
        self.assertEqual(len(self.store.worktrees.list_for_project("repo")), 1)

    async def test_delete_intent_rejects_project_registration_at_root_or_descendant(self) -> None:
        record = self.ready()
        snapshot = self.store.worktrees.preview_delete(record.id)
        self.store.worktrees.reserve_delete(record.id, expected_fingerprint=snapshot.fingerprint)
        for register in (self.store.register_project, self.store.bootstrap_project):
            for cwd in (record.root, record.cwd, record.root + "/nested/project"):
                with self.subTest(register=register.__name__, cwd=cwd), self.assertRaises(ProjectConflict):
                    register(alias="new", cwd=cwd)
        unaffected = self.store.register_project(alias="similar-prefix", cwd=record.root + "-sibling")
        self.assertEqual(unaffected.cwd, record.root + "-sibling")
        self.store.worktrees.release_delete(record.id)
        self.assertEqual(self.store.register_project(alias="new", cwd=record.cwd).cwd, record.cwd)

    async def test_project_delete_admission_resolves_symlinks_before_writer_lock(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve() / "managed"
            root.mkdir()
            linked = Path(raw) / "linked"
            linked.symlink_to(root, target_is_directory=True)
            record = self.store.worktrees.register(
                project_alias="repo", expected_project_revision=1, root=str(root), cwd=str(root), common_dir="/repo/.git",
            )
            self.store.worktrees.mark_ready(record.id, git_dir="/repo/.git/worktrees/temporary", identity_token="identity")
            snapshot = self.store.worktrees.preview_delete(record.id)
            self.store.worktrees.reserve_delete(record.id, expected_fingerprint=snapshot.fingerprint)
            original = Path.resolve

            def resolve(path, *args, **kwargs):
                self.assertFalse(self.store._lock._is_owned(), "filesystem lookup must precede the writer lock")
                return original(path, *args, **kwargs)

            with patch.object(Path, "resolve", resolve):
                for register in (self.store.register_project, self.store.bootstrap_project):
                    with self.subTest(register=register.__name__), self.assertRaises(ProjectConflict):
                        register(alias="linked-project", cwd=str(linked / "future-subdir"))
            self.store.worktrees.release_delete(record.id)

    async def test_binding_count_exceeds_delete_inventory_limit_without_decoding_rows(self) -> None:
        record = self.ready()
        source = self.bind(record)
        with self.store._transaction():
            self.store._connection.executemany(
                "INSERT INTO bindings(binding_id,scope_key,project_alias,creator_id,created_at,activated_at,working_cwd,worktree_id) "
                "VALUES(?,?,?,?,?,?,?,?)",
                ((f"extra-{number}", source.scope_key, source.project_alias, "user", source.created_at,
                  source.created_at, record.cwd, record.id) for number in range(1000)),
            )
        self.assertEqual(self.store.worktrees.binding_count(record.id), 1001)
        self.assertEqual(self.store.worktrees.binding_count("absent"), 0)
        with self.assertRaises(WorktreeConflict):
            self.store.worktrees.bindings(record.id)

    async def test_native_workspace_sync_moves_exact_relationship_and_preserves_native_identity(self) -> None:
        first, second = self.ready(), self.ready("two")
        source = self.bind(first)
        self.store.assign_native_thread_id(source.id, "native")
        synced = self.store.sync_native_workspace(
            source.id, expected_native_thread_id="native", working_cwd=second.cwd,
        )
        self.assertEqual((synced.working_cwd, synced.worktree_id, synced.native_thread_id), (second.cwd, second.id, "native"))
        self.assertEqual(self.store.worktrees.bindings(first.id), ())
        self.assertEqual(self.store.worktrees.binding_count(second.id), 1)
        self.assertEqual(self.store.sync_native_workspace(
            source.id, expected_native_thread_id="native", working_cwd=second.cwd,
        ), synced)
        # An external/missing cwd is still a fact, not permission to substitute Project.cwd.
        external = self.store.sync_native_workspace(
            source.id, expected_native_thread_id="native", working_cwd="/missing/native-cwd",
        )
        self.assertEqual(external.working_cwd, "/missing/native-cwd")
        self.assertIsNone(external.worktree_id)
        self.assertEqual(self.store.worktrees.binding_count(second.id), 0)

    async def test_native_workspace_sync_rejects_wrong_native_identity_and_deletion_intents(self) -> None:
        first, second = self.ready(), self.ready("two")
        source = self.bind(first)
        self.store.assign_native_thread_id(source.id, "native")
        with self.assertRaises(BindingConflict):
            self.store.sync_native_workspace(source.id, expected_native_thread_id="wrong", working_cwd=second.cwd)
        for protected in (first, second):
            preview = self.store.worktrees.preview_delete(protected.id)
            self.store.worktrees.reserve_delete(protected.id, expected_fingerprint=preview.fingerprint)
            with self.subTest(protected=protected.id), self.assertRaises(WorktreeConflict):
                self.store.sync_native_workspace(source.id, expected_native_thread_id="native", working_cwd=second.cwd)
            self.assertEqual(self.store.get(source.id).worktree_id, first.id)
            self.store.worktrees.release_delete(protected.id)

    async def test_native_workspace_sync_respects_project_generation_and_creation_origin(self) -> None:
        old = self.ready()
        self.delete_project()
        self.store.register_project(alias="repo", cwd="/repo")
        source = self.store.create_channel_binding(scope=self.scope, project_alias="repo", creator_id="user")
        self.store.assign_native_thread_id(source.id, "native")
        synced = self.store.sync_native_workspace(source.id, expected_native_thread_id="native", working_cwd=old.cwd)
        self.assertIsNone(synced.worktree_id)  # Same alias never adopts the orphaned previous generation.
        self.assertEqual(synced.working_cwd, old.cwd)
        owned = self.store.worktrees.register(
            project_alias="repo", expected_project_revision=self.store.get_project("repo").revision,
            root="/worktrees/created", cwd="/worktrees/created", common_dir="/repo/.git", origin_binding_id=source.id,
        )
        self.store.worktrees.mark_ready(owned.id, git_dir="/repo/.git/worktrees/created", identity_token="created")
        preview = self.store.worktrees.preview_delete(owned.id)
        self.store.worktrees.reserve_delete(owned.id, expected_fingerprint=preview.fingerprint)
        with self.assertRaises(WorktreeConflict):
            self.store.sync_native_workspace(source.id, expected_native_thread_id="native", working_cwd="/elsewhere")
        self.store.worktrees.release_delete(owned.id)

    async def test_unassociated_native_subdirectory_users_are_usage_only_and_obey_intent(self) -> None:
        record = self.ready()
        associated = self.bind(record)
        nested = self.store.create_channel_binding(
            scope=self.scope, project_alias="repo", creator_id="user", working_cwd=record.root + "/other/subdir",
        )
        sibling = self.store.create_channel_binding(
            scope=self.scope, project_alias="repo", creator_id="user", working_cwd=record.root + "-sibling",
        )
        self.assertIsNone(nested.worktree_id)
        self.assertEqual(self.store.worktrees.bindings(record.id), (self.store.get(associated.id),))
        self.assertEqual(self.store.worktrees.usage_bindings(record.id), (self.store.get(nested.id),))
        preview = self.store.worktrees.preview_delete(record.id)
        self.store.worktrees.reserve_delete(record.id, expected_fingerprint=preview.fingerprint)
        with self.assertRaises(WorktreeConflict):
            self.store.worktrees.require_binding_not_deleting(nested.id)
        with self.assertRaises(WorktreeConflict):
            self.store.create_channel_binding(
                scope=self.scope, project_alias="repo", creator_id="user", working_cwd=record.root + "/new-subdir",
            )
        self.store.worktrees.require_binding_not_deleting(sibling.id)
        self.store.worktrees.release_delete(record.id)
        self.store.worktrees.require_binding_not_deleting(nested.id)

    async def test_orphan_usage_remains_unassociated_and_incoming_native_path_obeys_intent(self) -> None:
        record = self.ready()
        self.delete_project()
        self.store.register_project(alias="repo", cwd="/repo")
        source = self.store.create_channel_binding(scope=self.scope, project_alias="repo", creator_id="user")
        self.store.assign_native_thread_id(source.id, "native")
        observed = self.store.sync_native_workspace(source.id, expected_native_thread_id="native", working_cwd=record.cwd)
        self.assertIsNone(observed.worktree_id)
        self.assertEqual(self.store.worktrees.usage_bindings(record.id), (observed,))
        self.assertEqual(self.store.worktrees.bindings(record.id), ())
        preview = self.store.worktrees.preview_delete(record.id)
        self.store.worktrees.reserve_delete(record.id, expected_fingerprint=preview.fingerprint)
        with self.assertRaises(WorktreeConflict):
            self.store.worktrees.require_binding_not_deleting(source.id)
        incoming = self.store.create_channel_binding(scope=self.scope, project_alias="repo", creator_id="user")
        self.store.assign_native_thread_id(incoming.id, "incoming-native")
        with self.assertRaises(WorktreeConflict):
            self.store.sync_native_workspace(
                incoming.id, expected_native_thread_id="incoming-native", working_cwd=record.root + "/new-native-subdir",
            )
        self.assertIsNone(self.store.get(incoming.id).working_cwd)
        self.store.worktrees.release_delete(record.id)

    async def test_usage_inventory_is_bounded_and_includes_other_worktree_association(self) -> None:
        record = self.ready()
        first = self.store.create_channel_binding(
            scope=self.scope, project_alias="repo", creator_id="user", working_cwd=record.root + "/nested/a",
        )
        nested_record = self.store.worktrees.register(
            project_alias="repo", expected_project_revision=1, root=record.root + "/nested/managed",
            cwd=record.root + "/nested/managed", common_dir="/repo/.git",
        )
        nested_record = self.store.worktrees.mark_ready(
            nested_record.id, git_dir="/repo/.git/worktrees/nested", identity_token="nested",
        )
        second = self.bind(nested_record)
        self.assertEqual({item.id for item in self.store.worktrees.usage_bindings(record.id)}, {first.id, second.id})
        with self.assertRaises(WorktreeConflict):
            self.store.worktrees.usage_bindings(record.id, limit=1)


class WorktreeMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "channel.sqlite3"
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executescript((FIXTURES / "channel_v14.sql").read_text())
            connection.executescript((FIXTURES / "channel_v14_data.sql").read_text())

    def test_frozen_v14_upgrade_preserves_old_rows_and_initializes_empty_choices(self) -> None:
        with closing(sqlite3.connect(self.path)) as connection:
            before = {name: connection.execute(f"SELECT * FROM {name}").fetchall()
                      for name in ("bindings", "projects", "schedule_plans", "schedule_runs", "session_defaults")}
        migrations.migrate_channel_database(self.path, expected_source_version=14)
        with closing(sqlite3.connect(self.path)) as connection:
            for name, rows in before.items():
                actual = connection.execute(f"SELECT * FROM {name}").fetchall()
                self.assertEqual([row[:len(rows[0])] for row in actual], rows)
            self.assertEqual(connection.execute("SELECT working_cwd,worktree_id FROM bindings").fetchall(), [(None, None)])
            self.assertEqual(connection.execute("SELECT generation FROM projects").fetchall(), [(1,)])
            connection.executescript((FIXTURES / "channel_v15_worktrees.sql").read_text())
        validate_channel_database(self.path)
        with closing(BindingStore(self.path)) as store:
            self.assertEqual(store.worktrees.get("worktree-shared").identity_token, "root:1:2/gitdir:3:4")
            self.assertEqual(store.worktrees.get("worktree-unknown").creation_state, "unknown")
            self.assertEqual({item.id for item in store.worktrees.bindings("worktree-shared")}, {"binding", "lazy-worktree-binding"})

    def test_migrated_and_fresh_database_have_equivalent_complete_structure(self) -> None:
        migrations.migrate_channel_database(self.path, expected_source_version=14)
        with closing(BindingStore()) as fresh, closing(sqlite3.connect(self.path)) as upgraded:
            upgraded.row_factory = sqlite3.Row
            statements = tuple(row[0] for row in fresh._connection.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
                "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END"
            ))
            require_schema(upgraded, statements)

    def test_real_worktree_migration_failure_rolls_back_new_columns_and_records(self) -> None:
        def fail(connection):
            v15.apply(connection)
            raise RuntimeError("after Worktree tables")

        step = migrations.Migration(14, 15, fail, v15.validate)
        before = self.path.read_bytes()
        with patch.object(migrations, "MIGRATIONS", (step,)), self.assertRaisesRegex(RuntimeError, "after Worktree"):
            migrations.migrate_channel_database(self.path, expected_source_version=14)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(migrations.plan_channel_database(self.path)["source_version"], 14)
