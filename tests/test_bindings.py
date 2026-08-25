from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from netizen.bindings import (
    AmbiguousBinding,
    BindingCursor,
    BindingConflict,
    BindingContextRevisionConflict,
    BindingNotFound,
    BindingQuery,
    BindingQueryBusy,
    BindingQueryTimeout,
    BindingSettingsRevisionConflict,
    BindingStore,
    BindingTurnSettings,
    ProjectConflict,
    ProjectDisabled,
    ProjectNotFound,
    ProjectRevisionConflict,
    ScopeConflict,
    ScopeNotFound,
    SideTopicCursor,
    SideTopicConflict,
    SideTopicQuery,
    SideTopicState,
    _binding_inventory_statement,
    _side_inventory_statement,
    _Transaction,
    migrate_channel_database_v5_to_v6,
)
from netizen.domain import (
    FeishuScope,
    MentionContextMode,
    MessageContextAnchor,
    ScopeKind,
)


class BindingStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        ids = iter(
            [
                "aaaaaaaa-0000-0000-0000-000000000001",
                "aaaaaaaa-0000-0000-0000-000000000002",
                "bbbbbbbb-0000-0000-0000-000000000003",
                "cccccccc-0000-0000-0000-000000000004",
            ]
        )
        self.now = 100.0
        self.store = BindingStore(
            id_factory=lambda: next(ids),
            wall_clock=lambda: self.now,
        )
        self.scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)

    def tearDown(self) -> None:
        self.store.close()

    def create(self, project: str = "none"):
        return self.store.create_binding(
            scope=self.scope,
            project_alias=project,
            creator_id="ou_user",
        )

    def test_new_binding_is_lazy_and_switches_active_pointer(self) -> None:
        first = self.create("test")
        second = self.create("none")

        self.assertIsNone(first.native_thread_id)
        self.assertIsNone(first.turn_settings)
        self.assertEqual(first.settings_revision, 1)
        self.assertEqual(
            first.message_context_mode,
            MentionContextMode.CURRENT_ONLY,
        )
        self.assertIsNone(first.context_anchor)
        self.assertEqual(first.context_revision, 1)
        self.assertEqual(self.store.active_binding(self.scope.key).id, second.id)
        bindings = self.store.list_bindings(self.scope.key)
        self.assertEqual({binding.id for binding in bindings}, {first.id, second.id})
        self.assertEqual(sum(binding.active for binding in bindings), 1)

    def test_native_thread_id_is_write_once_and_unique(self) -> None:
        first = self.create()
        second = self.create()
        self.store.assign_native_thread_id(first.id, "native-1")
        self.store.assign_native_thread_id(first.id, "native-1")

        with self.assertRaises(BindingConflict):
            self.store.assign_native_thread_id(first.id, "native-other")
        with self.assertRaises(BindingConflict):
            self.store.assign_native_thread_id(second.id, "native-1")

        self.assertEqual(self.store.get(first.id).native_thread_id, "native-1")
        self.assertIsNone(self.store.get(second.id).native_thread_id)

    def test_turn_settings_are_persistent_atomic_and_revision_guarded(self) -> None:
        selected = BindingTurnSettings("model", "high", "priority")
        binding = self.store.create_binding(
            scope=self.scope,
            project_alias="test",
            creator_id="ou_user",
            turn_settings=selected,
        )
        self.assertEqual(binding.turn_settings, selected)
        self.assertEqual(binding.settings_revision, 1)

        unchanged = self.store.set_turn_settings(
            binding_id=binding.id,
            expected_revision=1,
            settings=selected,
        )
        self.assertEqual(unchanged.settings_revision, 1)

        cleared = self.store.set_turn_settings(
            binding_id=binding.id,
            expected_revision=1,
            settings=None,
        )
        self.assertIsNone(cleared.turn_settings)
        self.assertEqual(cleared.settings_revision, 2)
        with self.assertRaises(BindingSettingsRevisionConflict):
            self.store.set_turn_settings(
                binding_id=binding.id,
                expected_revision=1,
                settings=selected,
            )

    def test_database_rejects_partial_turn_settings(self) -> None:
        binding = self.create()

        with self.assertRaises(sqlite3.IntegrityError):
            self.store._connection.execute(
                "UPDATE bindings SET model_id = ? WHERE binding_id = ?",
                ("model", binding.id),
            )
        self.assertIsNone(self.store.get(binding.id).turn_settings)

    def test_catch_up_context_is_group_or_topic_only_and_requires_anchor(
        self,
    ) -> None:
        anchor = MessageContextAnchor("om_initial", 1_700_000_000_000)
        with self.assertRaisesRegex(ValueError, "direct"):
            self.store.create_binding(
                scope=self.scope,
                project_alias="none",
                creator_id="ou_user",
                message_context_mode=MentionContextMode.CATCH_UP,
                context_anchor=anchor,
            )
        self.assertEqual(
            self.store._connection.execute("SELECT COUNT(*) FROM scopes").fetchone()[
                0
            ],
            0,
        )

        group = FeishuScope("cli_test", "oc_group", ScopeKind.GROUP)
        with self.assertRaisesRegex(ValueError, "must not have"):
            self.store.create_binding(
                scope=group,
                project_alias="none",
                creator_id="ou_user",
                context_anchor=anchor,
            )
        with self.assertRaisesRegex(ValueError, "requires"):
            self.store.create_binding(
                scope=group,
                project_alias="none",
                creator_id="ou_user",
                message_context_mode=MentionContextMode.CATCH_UP,
            )
        binding = self.store.create_binding(
            scope=group,
            project_alias="none",
            creator_id="ou_user",
            message_context_mode=MentionContextMode.CATCH_UP,
            context_anchor=anchor,
        )
        self.assertEqual(binding.message_context_mode, MentionContextMode.CATCH_UP)
        self.assertEqual(binding.context_anchor, anchor)
        self.assertEqual(binding.context_revision, 1)

        topic = FeishuScope(
            "cli_test",
            "oc_group",
            ScopeKind.TOPIC,
            "omt_topic",
        )
        topic_binding = self.store.create_binding(
            scope=topic,
            project_alias="none",
            creator_id="ou_user",
            message_context_mode=MentionContextMode.CATCH_UP,
            context_anchor=anchor,
        )
        self.assertEqual(topic_binding.context_anchor, anchor)

        with self.assertRaises(sqlite3.IntegrityError):
            self.store._connection.execute(
                """
                UPDATE bindings
                SET context_anchor_message_id = NULL
                WHERE binding_id = ?
                """,
                (binding.id,),
            )
        self.assertEqual(self.store.get(binding.id).context_anchor, anchor)

        direct = self.store.create_binding(
            scope=self.scope,
            project_alias="none",
            creator_id="ou_user",
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "direct"):
            self.store._connection.execute(
                """
                UPDATE bindings
                SET message_context_mode = 'catch-up',
                    context_anchor_message_id = ?,
                    context_anchor_create_time_ms = ?
                WHERE binding_id = ?
                """,
                (anchor.message_id, anchor.create_time_ms, direct.id),
            )
        self.assertEqual(
            self.store.get(direct.id).message_context_mode,
            MentionContextMode.CURRENT_ONLY,
        )
        with self.assertRaisesRegex(ValueError, "direct"):
            self.store.set_configuration(
                binding_id=direct.id,
                expected_settings_revision=1,
                expected_context_revision=1,
                settings=BindingTurnSettings("model", "high", "priority"),
                message_context_mode=MentionContextMode.CATCH_UP,
                context_anchor=anchor,
            )
        unchanged = self.store.get(direct.id)
        self.assertIsNone(unchanged.turn_settings)
        self.assertEqual(unchanged.settings_revision, 1)
        self.assertEqual(unchanged.context_revision, 1)

    def test_configuration_atomically_guards_settings_and_context_revisions(
        self,
    ) -> None:
        group = FeishuScope("cli_test", "oc_group", ScopeKind.GROUP)
        binding = self.store.create_binding(
            scope=group,
            project_alias="none",
            creator_id="ou_user",
        )
        selected = BindingTurnSettings("model", "high", "priority")
        initial = MessageContextAnchor("om_initial", 1_700_000_000_000)

        configured = self.store.set_configuration(
            binding_id=binding.id,
            expected_settings_revision=1,
            expected_context_revision=1,
            settings=selected,
            message_context_mode=MentionContextMode.CATCH_UP,
            context_anchor=initial,
        )
        self.assertEqual(configured.turn_settings, selected)
        self.assertEqual(configured.settings_revision, 2)
        self.assertEqual(configured.message_context_mode, MentionContextMode.CATCH_UP)
        self.assertEqual(configured.context_anchor, initial)
        self.assertEqual(configured.context_revision, 2)

        inherited = self.store.set_configuration(
            binding_id=binding.id,
            expected_settings_revision=2,
            expected_context_revision=2,
            settings=None,
            message_context_mode=MentionContextMode.CATCH_UP,
            context_anchor=None,
        )
        self.assertIsNone(inherited.turn_settings)
        self.assertEqual(inherited.settings_revision, 3)
        self.assertEqual(inherited.context_anchor, initial)
        self.assertEqual(inherited.context_revision, 2)

        with self.assertRaises(BindingContextRevisionConflict):
            self.store.set_configuration(
                binding_id=binding.id,
                expected_settings_revision=3,
                expected_context_revision=1,
                settings=selected,
                message_context_mode=MentionContextMode.CURRENT_ONLY,
                context_anchor=None,
            )
        unchanged = self.store.get(binding.id)
        self.assertIsNone(unchanged.turn_settings)
        self.assertEqual(unchanged.settings_revision, 3)
        self.assertEqual(unchanged.context_anchor, initial)

        cleared = self.store.set_configuration(
            binding_id=binding.id,
            expected_settings_revision=3,
            expected_context_revision=2,
            settings=None,
            message_context_mode=MentionContextMode.CURRENT_ONLY,
            context_anchor=None,
        )
        self.assertEqual(cleared.settings_revision, 3)
        self.assertEqual(cleared.context_revision, 3)
        self.assertIsNone(cleared.context_anchor)

    def test_context_anchor_commit_is_revision_guarded(self) -> None:
        group = FeishuScope("cli_test", "oc_group", ScopeKind.GROUP)
        initial = MessageContextAnchor("om_initial", 1_700_000_000_000)
        binding = self.store.create_binding(
            scope=group,
            project_alias="none",
            creator_id="ou_user",
            message_context_mode=MentionContextMode.CATCH_UP,
            context_anchor=initial,
        )
        current = MessageContextAnchor("om_current", 1_700_000_001_000)

        committed = self.store.commit_context_anchor(
            binding_id=binding.id,
            expected_context_revision=1,
            anchor=current,
        )
        self.assertEqual(committed.context_anchor, current)
        self.assertEqual(committed.context_revision, 2)
        with self.assertRaises(BindingContextRevisionConflict):
            self.store.commit_context_anchor(
                binding_id=binding.id,
                expected_context_revision=1,
                anchor=MessageContextAnchor("om_late", 1_700_000_002_000),
            )
        self.assertEqual(self.store.get(binding.id), committed)

        current_only = self.store.create_binding(
            scope=group,
            project_alias="none",
            creator_id="ou_user",
        )
        with self.assertRaisesRegex(BindingConflict, "no context anchor"):
            self.store.commit_context_anchor(
                binding_id=current_only.id,
                expected_context_revision=1,
                anchor=current,
            )

    def test_catch_up_activation_requires_and_commits_a_reset_anchor(self) -> None:
        group = FeishuScope("cli_test", "oc_group", ScopeKind.GROUP)
        initial = MessageContextAnchor("om_initial", 1_700_000_000_000)
        catch_up = self.store.create_binding(
            scope=group,
            project_alias="none",
            creator_id="ou_user",
            message_context_mode=MentionContextMode.CATCH_UP,
            context_anchor=initial,
        )
        current_only = self.store.create_binding(
            scope=group,
            project_alias="none",
            creator_id="ou_user",
        )
        self.assertEqual(self.store.active_binding(group.key).id, current_only.id)

        with self.assertRaisesRegex(ValueError, "requires"):
            self.store.activate(scope_key=group.key, binding_id=catch_up.id)
        self.assertEqual(self.store.active_binding(group.key).id, current_only.id)

        reset = MessageContextAnchor("om_resume", 1_700_000_003_000)
        activated = self.store.activate(
            scope_key=group.key,
            binding_id=catch_up.id,
            context_anchor=reset,
        )
        self.assertTrue(activated.active)
        self.assertEqual(activated.context_anchor, reset)
        self.assertEqual(activated.context_revision, 2)

    def test_reference_resolution_is_scope_local_and_detects_ambiguity(self) -> None:
        first = self.create()
        second = self.create()

        self.assertEqual(
            self.store.resolve_reference(
                scope_key=self.scope.key,
                reference=first.id,
            ).id,
            first.id,
        )
        with self.assertRaises(AmbiguousBinding):
            self.store.resolve_reference(
                scope_key=self.scope.key,
                reference="aaaaaaaa",
            )
        with self.assertRaises(BindingNotFound):
            self.store.resolve_reference(
                scope_key=self.scope.key,
                reference="missing",
            )
        self.store.activate(scope_key=self.scope.key, binding_id=first.id)
        self.assertEqual(self.store.active_binding(self.scope.key).id, first.id)
        self.assertNotEqual(first.id, second.id)

    def test_deactivate_clears_only_the_exact_active_pointer(self) -> None:
        binding = self.create("test")
        self.store.assign_native_thread_id(binding.id, "native-1")

        deactivated = self.store.deactivate(
            scope_key=self.scope.key,
            binding_id=binding.id,
        )

        self.assertFalse(deactivated.active)
        self.assertEqual(deactivated.native_thread_id, "native-1")
        self.assertIsNone(self.store.active_binding(self.scope.key))
        with self.assertRaises(BindingConflict):
            self.store.deactivate(
                scope_key=self.scope.key,
                binding_id=binding.id,
            )

    def test_delete_binding_is_atomic_and_clears_active_pointer(self) -> None:
        first = self.create("test")
        second = self.create("none")
        self.store.activate(scope_key=self.scope.key, binding_id=first.id)

        deleted = self.store.delete_binding(first.id)

        self.assertEqual(deleted.id, first.id)
        self.assertIsNone(self.store.active_binding(self.scope.key))
        with self.assertRaises(BindingNotFound):
            self.store.get(first.id)
        self.assertEqual(self.store.get(second.id).id, second.id)

    def test_dedup_store_uses_ttl(self) -> None:
        self.assertFalse(self.store.seen("msg:one"))
        self.store.mark("msg:one", 10)
        self.assertTrue(self.store.seen("msg:one"))

        self.now = 111
        self.assertFalse(self.store.seen("msg:one"))

    def test_memory_store_admin_reads_share_the_test_connection(self) -> None:
        binding = self.create("test")

        page = asyncio.run(self.store.query_bindings())

        self.assertEqual([item.binding.id for item in page.items], [binding.id])
        self.assertIsNone(self.store._query_connection)

    def test_failed_begin_releases_transaction_lock(self) -> None:
        class FailingConnection:
            def execute(self, _statement: str) -> None:
                raise RuntimeError("begin failed")

        lock = threading.Lock()
        transaction = _Transaction(FailingConnection(), lock)  # type: ignore[arg-type]

        with self.assertRaisesRegex(RuntimeError, "begin failed"):
            transaction.__enter__()

        self.assertFalse(lock.locked())

    def test_file_store_survives_restart_and_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "channel.sqlite3"
            first = BindingStore(path, id_factory=lambda: "binding-one")
            scope = FeishuScope("cli_test", "oc_chat", ScopeKind.GROUP)
            anchor = MessageContextAnchor("om_initial", 1_700_000_000_000)
            binding = first.create_binding(
                scope=scope,
                project_alias="test",
                creator_id="ou_operator",
                message_context_mode=MentionContextMode.CATCH_UP,
                context_anchor=anchor,
            )
            first.assign_native_thread_id(binding.id, "native-one")
            first.close()

            second = BindingStore(path)
            try:
                restored = second.active_binding(scope.key)
                self.assertEqual(restored.native_thread_id, "native-one")
                self.assertEqual(
                    restored.message_context_mode,
                    MentionContextMode.CATCH_UP,
                )
                self.assertEqual(restored.context_anchor, anchor)
                self.assertEqual(restored.context_revision, 1)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            finally:
                second.close()

    def test_side_topic_identity_is_idempotent_and_terminal_rows_are_tombstones(
        self,
    ) -> None:
        binding = self.create("test")
        side = self.store.create_side_topic(
            app_id="cli_test",
            chat_id="oc_direct",
            source_message_id="om_side",
            parent_binding_id=binding.id,
            creator_id="ou_user",
            requires_mention=False,
        )
        duplicate = self.store.create_side_topic(
            app_id="cli_test",
            chat_id="oc_direct",
            source_message_id="om_side",
            parent_binding_id=binding.id,
            creator_id="ou_user",
            requires_mention=False,
        )
        self.assertEqual(duplicate.id, side.id)
        self.assertEqual(side.state, SideTopicState.CREATING)

        for changed in (
            {"chat_id": "oc_other"},
            {"parent_binding_id": "binding-other"},
            {"creator_id": "ou_other"},
            {"requires_mention": True},
        ):
            identity = {
                "app_id": "cli_test",
                "chat_id": "oc_direct",
                "source_message_id": "om_side",
                "parent_binding_id": binding.id,
                "creator_id": "ou_user",
                "requires_mention": False,
            }
            identity.update(changed)
            with self.subTest(changed=changed), self.assertRaises(
                SideTopicConflict
            ):
                self.store.create_side_topic(**identity)

        with self.assertRaisesRegex(ValueError, "boolean"):
            self.store.create_side_topic(
                app_id="cli_test",
                chat_id="oc_direct",
                source_message_id="om_other",
                parent_binding_id=binding.id,
                creator_id="ou_user",
                requires_mention=1,  # type: ignore[arg-type]
            )

        side = self.store.set_side_topic_root(side.id, "om_root")
        side = self.store.set_side_topic_topic(side.id, "omt_side")
        self.assertEqual(side.state, SideTopicState.CREATING)
        self.assertEqual(side.topic_id, "omt_side")
        side = self.store.open_side_topic(side.id, "omt_side")
        self.assertEqual(side.state, SideTopicState.OPEN)
        self.assertEqual(
            self.store.side_topic_for_message(
                app_id="cli_test",
                chat_id="oc_direct",
                topic_id="omt_side",
            ).id,
            side.id,
        )
        closed = self.store.transition_side_topic(side.id, SideTopicState.CLOSED)
        self.assertEqual(closed.state, SideTopicState.CLOSED)
        self.assertEqual(
            self.store.side_topic_for_message(
                app_id="cli_test",
                chat_id="oc_direct",
                topic_id="omt_side",
            ).state,
            SideTopicState.CLOSED,
        )
        with self.assertRaises(SideTopicConflict):
            self.store.transition_side_topic(side.id, SideTopicState.EXPIRED)

    def test_side_message_route_cross_validates_topic_and_root(self) -> None:
        binding = self.create("test")
        first = self.store.create_side_topic(
            app_id="cli_test",
            chat_id="oc_direct",
            source_message_id="om_first_source",
            parent_binding_id=binding.id,
            creator_id="ou_user",
            requires_mention=False,
        )
        self.store.set_side_topic_root(first.id, "om_first_root")
        first = self.store.open_side_topic(first.id, "omt_first")
        second = self.store.create_side_topic(
            app_id="cli_test",
            chat_id="oc_direct",
            source_message_id="om_second_source",
            parent_binding_id=binding.id,
            creator_id="ou_user",
            requires_mention=False,
        )
        self.store.set_side_topic_root(second.id, "om_second_root")
        self.store.open_side_topic(second.id, "omt_second")

        self.assertEqual(
            self.store.side_topic_for_message(
                app_id="cli_test",
                chat_id="oc_direct",
                topic_id="omt_first",
                root_message_id="om_first_root",
            ).id,
            first.id,
        )
        self.assertEqual(
            self.store.side_topic_for_message(
                app_id="cli_test",
                chat_id="oc_direct",
                topic_id=None,
                root_message_id="om_first_root",
            ).id,
            first.id,
        )
        for topic_id, root_id in (
            ("omt_first", "om_second_root"),
            ("omt_first", "om_unknown_root"),
            ("omt_unknown", "om_first_root"),
        ):
            with self.subTest(topic_id=topic_id, root_id=root_id), self.assertRaises(
                SideTopicConflict
            ):
                self.store.side_topic_for_message(
                    app_id="cli_test",
                    chat_id="oc_direct",
                    topic_id=topic_id,
                    root_message_id=root_id,
                )

        creating = self.store.create_side_topic(
            app_id="cli_test",
            chat_id="oc_direct",
            source_message_id="om_creating_source",
            parent_binding_id=binding.id,
            creator_id="ou_user",
            requires_mention=False,
        )
        self.store.set_side_topic_root(creating.id, "om_creating_root")
        self.assertEqual(
            self.store.side_topic_for_message(
                app_id="cli_test",
                chat_id="oc_direct",
                topic_id="omt_unconfirmed",
                root_message_id="om_creating_root",
            ).id,
            creating.id,
        )

    def test_service_start_explicitly_expires_live_side_topics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "channel.sqlite3"
            ids = iter(("binding-one", "side-one"))
            first = BindingStore(path, id_factory=lambda: next(ids))
            scope = FeishuScope("cli_test", "oc_chat", ScopeKind.GROUP)
            binding = first.create_binding(
                scope=scope,
                project_alias="test",
                creator_id="ou_operator",
            )
            side = first.create_side_topic(
                app_id="cli_test",
                chat_id="oc_chat",
                source_message_id="om_side",
                parent_binding_id=binding.id,
                creator_id="ou_operator",
                requires_mention=True,
            )
            first.set_side_topic_root(side.id, "om_root")
            first.open_side_topic(side.id, "omt_side")
            first.close()

            second = BindingStore(path)
            try:
                self.assertEqual(
                    second.get_side_topic(side.id).state,
                    SideTopicState.OPEN,
                )
                expired = second.expire_live_side_topics()
                self.assertEqual([item.id for item in expired], [side.id])
                restored = second.get_side_topic(side.id)
                self.assertEqual(restored.state, SideTopicState.EXPIRED)
                self.assertTrue(restored.requires_mention)
                self.assertEqual(second.expire_live_side_topics(), ())
            finally:
                second.close()

    def test_schema_contains_only_binding_scoped_settings_and_context_intent(
        self,
    ) -> None:
        connection = self.store._connection  # Architecture contract.
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        columns = {
            row[1]
            for table in tables
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }

        self.assertEqual(
            tables,
            {
                "schema_version",
                "scopes",
                "bindings",
                "projects",
                "dedup_keys",
                "side_topics",
            },
        )
        self.assertTrue(
            {
                "prompt",
                "response",
                "cwd_copy",
                "turn_status",
                "turn_id",
                "queue",
                "effective_model",
                "effective_effort",
                "effective_service_tier",
            }.isdisjoint(columns)
        )
        self.assertTrue(
            {
                "model_id",
                "effort_id",
                "service_tier_id",
                "settings_revision",
                "ever_activated",
                "message_context_mode",
                "context_anchor_message_id",
                "context_anchor_create_time_ms",
                "context_revision",
            }.issubset(columns)
        )
        side_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(side_topics)").fetchall()
        }
        self.assertTrue(
            {
                "native_thread_id",
                "prompt",
                "response",
                "turn_id",
                "model_id",
                "effort_id",
                "service_tier_id",
            }.isdisjoint(side_columns)
        )

    def test_project_registry_rows_are_persistent_and_revision_guarded(self) -> None:
        first = self.store.bootstrap_project(alias="none", cwd="/tmp/default")
        self.assertTrue(first.enabled)
        self.assertEqual(first.revision, 1)
        self.store.bootstrap_project(alias="none", cwd="/tmp/ignored")
        self.assertEqual(self.store.get_project("none").cwd, "/tmp/default")

        project = self.store.register_project(alias="test", cwd="/tmp/test")
        with self.assertRaises(ProjectConflict):
            self.store.register_project(alias="test", cwd="/tmp/other")
        disabled = self.store.set_project_enabled(
            alias="test",
            enabled=False,
            expected_revision=project.revision,
        )
        self.assertFalse(disabled.enabled)
        self.assertEqual(disabled.revision, 2)
        with self.assertRaises(ProjectRevisionConflict):
            self.store.set_project_enabled(
                alias="test",
                enabled=True,
                expected_revision=project.revision,
            )

    def test_obsolete_schema_is_rejected_instead_of_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "channel.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE schema_version (version INTEGER NOT NULL);
                INSERT INTO schema_version(version) VALUES (3);
                """
            )
            connection.close()

            with self.assertRaisesRegex(RuntimeError, "recreate"):
                BindingStore(path)

            connection = sqlite3.connect(path)
            try:
                version = connection.execute(
                    "SELECT version FROM schema_version"
                ).fetchone()[0]
                self.assertEqual(version, 3)
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                self.assertEqual(tables, {"schema_version"})
            finally:
                connection.close()


class BindingStoreManagementSchemaTest(unittest.TestCase):
    def test_atomic_create_variants_enforce_project_and_scope_facts(self) -> None:
        next_id = 0

        def make_id() -> str:
            nonlocal next_id
            next_id += 1
            return f"binding-{next_id}"

        store = BindingStore(id_factory=make_id)
        direct = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        missing = FeishuScope("cli_test", "oc_missing", ScopeKind.DIRECT)
        try:
            project = store.bootstrap_project(alias="enabled", cwd="/tmp/enabled")
            disabled = store.bootstrap_project(alias="disabled", cwd="/tmp/disabled")
            disabled = store.set_project_enabled(
                alias=disabled.alias,
                enabled=False,
                expected_revision=disabled.revision,
            )

            channel = store.create_channel_binding(
                scope=direct,
                project_alias=project.alias,
                expected_project_revision=project.revision,
                creator_id="ou_user",
            )
            self.assertTrue(channel.active)
            self.assertIsNotNone(channel.activated_at)
            self.assertEqual(store.get_scope(direct.key).scope, direct)

            admin = store.create_admin_binding(
                scope=direct,
                project_alias=project.alias,
                expected_project_revision=project.revision,
            )
            self.assertFalse(admin.active)
            self.assertIsNone(admin.activated_at)
            self.assertEqual(admin.creator_id, "admin:web")
            self.assertEqual(store.active_binding(direct.key).id, channel.id)
            activated = store.activate(
                scope_key=direct.key,
                binding_id=admin.id,
            )
            self.assertIsNotNone(activated.activated_at)
            self.assertTrue(activated.active)
            self.assertEqual(
                store._connection.execute(
                    "SELECT ever_activated FROM bindings WHERE binding_id = ?",
                    (admin.id,),
                ).fetchone()[0],
                1,
            )

            created_current = store.create_admin_binding(
                scope=direct,
                project_alias=project.alias,
                expected_project_revision=project.revision,
                activate=True,
            )
            self.assertTrue(created_current.active)
            self.assertIsNotNone(created_current.activated_at)
            self.assertEqual(created_current.creator_id, "admin:web")
            self.assertEqual(
                store.active_binding(direct.key).id,
                created_current.id,
            )

            with self.assertRaises(ScopeNotFound):
                store.create_admin_binding(
                    scope=missing,
                    project_alias=project.alias,
                    expected_project_revision=project.revision,
                )
            with self.assertRaises(ProjectDisabled):
                store.create_channel_binding(
                    scope=missing,
                    project_alias=disabled.alias,
                    creator_id="ou_user",
                )
            with self.assertRaises(ProjectRevisionConflict):
                store.create_channel_binding(
                    scope=missing,
                    project_alias=project.alias,
                    expected_project_revision=project.revision + 1,
                    creator_id="ou_user",
                )

            store._connection.execute(
                "UPDATE scopes SET app_id = 'tampered' WHERE scope_key = ?",
                (direct.key,),
            )
            with self.assertRaises(ScopeConflict):
                store.create_channel_binding(
                    scope=direct,
                    project_alias=project.alias,
                    creator_id="ou_user",
                )
        finally:
            store.close()

    def test_compatibility_create_checks_projects_after_registry_bootstrap(self) -> None:
        store = BindingStore(id_factory=lambda: "binding")
        scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        try:
            store.bootstrap_project(alias="known", cwd="/tmp/known")
            with self.assertRaises(ProjectNotFound):
                store.create_binding(
                    scope=scope,
                    project_alias="missing",
                    creator_id="ou_user",
                )
        finally:
            store.close()

    def test_v5_database_is_rejected_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "channel.sqlite3"
            _create_frozen_v5_database(path)
            before_bytes = path.read_bytes()
            before = sqlite3.connect(path)
            try:
                before_schema = before.execute(
                    "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
                ).fetchall()
                before_rows = before.execute(
                    "SELECT alias, cwd, enabled, revision FROM projects"
                ).fetchall()
            finally:
                before.close()

            with self.assertRaisesRegex(RuntimeError, "recreate"):
                BindingStore(path)

            self.assertEqual(path.read_bytes(), before_bytes)
            self.assertFalse(Path(str(path) + "-wal").exists())
            self.assertFalse(Path(str(path) + "-shm").exists())
            after = sqlite3.connect(path)
            try:
                self.assertEqual(
                    after.execute(
                        "SELECT type, name, sql FROM sqlite_master "
                        "ORDER BY type, name"
                    ).fetchall(),
                    before_schema,
                )
                self.assertEqual(
                    after.execute(
                        "SELECT alias, cwd, enabled, revision FROM projects"
                    ).fetchall(),
                    before_rows,
                )
                self.assertEqual(
                    after.execute("SELECT version FROM schema_version").fetchone()[0],
                    5,
                )
            finally:
                after.close()

    def test_explicit_v5_to_v6_migration_preserves_rows_and_is_idempotent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "channel.sqlite3"
            _create_migratable_v5_database(path)

            self.assertTrue(migrate_channel_database_v5_to_v6(path))
            self.assertFalse(migrate_channel_database_v5_to_v6(path))

            connection = sqlite3.connect(path)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT version FROM schema_version"
                    ).fetchone()[0],
                    6,
                )
                self.assertEqual(
                    connection.execute(
                        """
                        SELECT binding_id, message_context_mode,
                               context_anchor_message_id,
                               context_anchor_create_time_ms,
                               context_revision
                        FROM bindings
                        """
                    ).fetchall(),
                    [("legacy-binding", "current-only", None, None, 1)],
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT side_id, state FROM side_topics"
                    ).fetchall(),
                    [("legacy-side", "closed")],
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        UPDATE bindings
                        SET message_context_mode = 'catch-up',
                            context_anchor_message_id = 'om_anchor',
                            context_anchor_create_time_ms = 1
                        """
                    )
            finally:
                connection.close()

            migrated = BindingStore(path)
            try:
                binding = migrated.get("legacy-binding")
                self.assertEqual(
                    binding.message_context_mode,
                    MentionContextMode.CURRENT_ONLY,
                )
                self.assertIsNone(binding.context_anchor)
            finally:
                migrated.close()

    def test_explicit_migration_rejects_partial_v5_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "channel.sqlite3"
            _create_frozen_v5_database(path)
            before = path.read_bytes()

            with self.assertRaisesRegex(RuntimeError, "missing required tables"):
                migrate_channel_database_v5_to_v6(path)

            self.assertEqual(path.read_bytes(), before)

    def test_file_database_uses_bounded_full_wal_writer_and_hot_wal_is_legacy_readable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "channel.sqlite3"
            store = BindingStore(path, id_factory=lambda: "binding")
            try:
                self.assertEqual(
                    store._connection.execute("PRAGMA journal_mode").fetchone()[0],
                    "wal",
                )
                self.assertEqual(
                    store._connection.execute("PRAGMA synchronous").fetchone()[0],
                    2,
                )
                self.assertLessEqual(
                    store._connection.execute("PRAGMA busy_timeout").fetchone()[0],
                    250,
                )
                assert store._query_connection is not None
                self.assertEqual(
                    store._query_connection.execute("PRAGMA query_only").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    store._query_connection.execute("PRAGMA busy_timeout").fetchone()[0],
                    0,
                )
                store._connection.execute("PRAGMA wal_autocheckpoint = 0")
                store.bootstrap_project(alias="none", cwd="/tmp/none")
                scope = FeishuScope("cli_test", "oc_hot", ScopeKind.DIRECT)
                store.create_channel_binding(
                    scope=scope,
                    project_alias="none",
                    creator_id="ou_user",
                )
                wal_path = Path(str(path) + "-wal")
                self.assertTrue(wal_path.exists())
                self.assertGreater(wal_path.stat().st_size, 0)

                legacy = sqlite3.connect(path, timeout=0.1)
                try:
                    self.assertEqual(
                        legacy.execute("SELECT COUNT(*) FROM bindings").fetchone()[0],
                        1,
                    )
                    legacy.execute(
                        """
                        INSERT INTO bindings(
                            binding_id, scope_key, project_alias, native_thread_id,
                            model_id, effort_id, service_tier_id, settings_revision,
                            creator_id, created_at, activated_at
                        ) VALUES (?, ?, ?, NULL, NULL, NULL, NULL, 1, ?, ?, ?)
                        """,
                        (
                            "legacy-hot",
                            scope.key,
                            "none",
                            "ou_legacy",
                            "2030-01-01T00:00:00+00:00",
                            "2030-01-01T00:00:00+00:00",
                        ),
                    )
                    legacy.commit()
                finally:
                    legacy.close()
                self.assertIsNotNone(store.get("legacy-hot").activated_at)
            finally:
                store.close()

    def test_project_disable_serializes_before_binding_create(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "channel.sqlite3"
            writer = BindingStore(path)
            creator = BindingStore(path, id_factory=lambda: "binding")
            scope = FeishuScope("cli_test", "oc_race", ScopeKind.DIRECT)
            project = writer.bootstrap_project(alias="race", cwd="/tmp/race")
            started = threading.Event()
            result: list[BaseException | object] = []

            def create() -> None:
                started.set()
                try:
                    result.append(
                        creator.create_channel_binding(
                            scope=scope,
                            project_alias=project.alias,
                            creator_id="ou_user",
                        )
                    )
                except BaseException as error:
                    result.append(error)

            thread = threading.Thread(target=create)
            try:
                with writer._transaction():
                    writer._connection.execute(
                        """
                        UPDATE projects
                        SET enabled = 0, revision = revision + 1, updated_at = ?
                        WHERE alias = ?
                        """,
                        ("2030-01-01T00:00:00+00:00", project.alias),
                    )
                    thread.start()
                    self.assertTrue(started.wait(0.2))
                    time.sleep(0.03)
                    self.assertTrue(thread.is_alive())
                thread.join(0.5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(len(result), 1)
                self.assertIsInstance(result[0], ProjectDisabled)
                self.assertEqual(
                    writer._connection.execute(
                        "SELECT COUNT(*) FROM bindings"
                    ).fetchone()[0],
                    0,
                )
                with self.assertRaises(ScopeNotFound):
                    writer.get_scope(scope.key)
            finally:
                if thread.is_alive():
                    thread.join(0.5)
                creator.close()
                writer.close()

    def test_project_revision_cas_has_one_concurrent_winner(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "channel.sqlite3"
            first = BindingStore(path)
            second = BindingStore(path)
            project = first.bootstrap_project(alias="race", cwd="/tmp/race")
            barrier = threading.Barrier(2)
            results: list[BaseException | object] = []
            result_lock = threading.Lock()

            def disable(store: BindingStore) -> None:
                barrier.wait()
                try:
                    result: BaseException | object = store.set_project_enabled(
                        alias=project.alias,
                        enabled=False,
                        expected_revision=project.revision,
                    )
                except BaseException as error:
                    result = error
                with result_lock:
                    results.append(result)

            threads = (
                threading.Thread(target=disable, args=(first,)),
                threading.Thread(target=disable, args=(second,)),
            )
            try:
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(0.5)
                    self.assertFalse(thread.is_alive())
                self.assertEqual(
                    sum(not isinstance(result, BaseException) for result in results),
                    1,
                )
                self.assertEqual(
                    sum(
                        isinstance(result, ProjectRevisionConflict)
                        for result in results
                    ),
                    1,
                )
                self.assertFalse(first.get_project(project.alias).enabled)
                self.assertEqual(first.get_project(project.alias).revision, 2)
            finally:
                second.close()
                first.close()

    def test_context_anchor_cas_has_one_concurrent_winner(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "channel.sqlite3"
            first = BindingStore(path, id_factory=lambda: "binding")
            second = BindingStore(path)
            group = FeishuScope("cli_test", "oc_context_race", ScopeKind.GROUP)
            initial = MessageContextAnchor("om_initial", 1_700_000_000_000)
            binding = first.create_binding(
                scope=group,
                project_alias="none",
                creator_id="ou_user",
                message_context_mode=MentionContextMode.CATCH_UP,
                context_anchor=initial,
            )
            barrier = threading.Barrier(2)
            results: list[BaseException | object] = []
            result_lock = threading.Lock()

            def commit(store: BindingStore, suffix: str) -> None:
                barrier.wait()
                try:
                    result: BaseException | object = store.commit_context_anchor(
                        binding_id=binding.id,
                        expected_context_revision=1,
                        anchor=MessageContextAnchor(
                            f"om_{suffix}",
                            1_700_000_001_000,
                        ),
                    )
                except BaseException as error:
                    result = error
                with result_lock:
                    results.append(result)

            threads = (
                threading.Thread(target=commit, args=(first, "first")),
                threading.Thread(target=commit, args=(second, "second")),
            )
            try:
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(0.5)
                    self.assertFalse(thread.is_alive())
                self.assertEqual(
                    sum(not isinstance(result, BaseException) for result in results),
                    1,
                )
                self.assertEqual(
                    sum(
                        isinstance(result, BindingContextRevisionConflict)
                        for result in results
                    ),
                    1,
                )
                self.assertEqual(first.get(binding.id).context_revision, 2)
            finally:
                second.close()
                first.close()


class BindingStoreQueryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        ids = (f"binding-{number:03d}" for number in range(100))
        self.store = BindingStore(
            Path(self.temp.name) / "channel.sqlite3",
            id_factory=lambda: next(ids),
        )
        self.store.bootstrap_project(alias="alpha", cwd="/tmp/alpha")
        self.store.bootstrap_project(alias="beta", cwd="/tmp/beta")
        self.direct = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        self.topic = FeishuScope(
            "cli_test", "oc_group", ScopeKind.TOPIC, "omt_topic"
        )

    async def asyncTearDown(self) -> None:
        await self.store.aclose()
        self.temp.cleanup()

    def create_binding(
        self,
        *,
        scope: FeishuScope,
        project: str,
        created_at: str,
        native: bool = False,
    ):
        binding = self.store.create_channel_binding(
            scope=scope,
            project_alias=project,
            creator_id="ou_user",
        )
        self.store._connection.execute(
            "UPDATE bindings SET created_at = ?, activated_at = ? WHERE binding_id = ?",
            (created_at, created_at, binding.id),
        )
        if native:
            self.store.assign_native_thread_id(binding.id, "native-" + binding.id)
        return self.store.get(binding.id)

    async def test_binding_keyset_is_stable_and_filters_in_sql(self) -> None:
        first = self.create_binding(
            scope=self.direct,
            project="alpha",
            created_at="2030-01-01T00:00:00+00:00",
        )
        second = self.create_binding(
            scope=self.topic,
            project="beta",
            created_at="2030-01-02T00:00:00+00:00",
            native=True,
        )
        third = self.create_binding(
            scope=self.topic,
            project="beta",
            created_at="2030-01-02T00:00:00+00:00",
        )

        page = await self.store.query_bindings(limit=2)
        self.assertEqual(
            [item.binding.id for item in page.items],
            sorted((second.id, third.id), reverse=True),
        )
        self.assertIsNotNone(page.next_cursor)
        remainder = await self.store.query_bindings(
            cursor=page.next_cursor,
            limit=2,
        )
        self.assertEqual([item.binding.id for item in remainder.items], [first.id])
        self.assertIsNone(remainder.next_cursor)

        filtered = await self.store.query_bindings(
            query=BindingQuery(
                project_alias="beta",
                scope_kind=ScopeKind.TOPIC,
                chat_id="oc_group",
                topic_id="omt_topic",
                materialized=True,
                identity=second.native_thread_id,
                created_from="2030-01-02T00:00:00+00:00",
                created_before="2030-01-03T00:00:00+00:00",
            )
        )
        self.assertEqual([item.binding.id for item in filtered.items], [second.id])
        self.assertEqual(filtered.items[0].scope.kind, ScopeKind.TOPIC)

        current = await self.store.query_bindings(
            query=BindingQuery(current=True)
        )
        self.assertEqual(
            {item.binding.id for item in current.items},
            {first.id, third.id},
        )
        hundred = await self.store.query_bindings(limit=100)
        self.assertEqual(len(hundred.items), 3)
        with self.assertRaises(ValueError):
            await self.store.query_bindings(limit=101)
        with self.assertRaises(ValueError):
            await self.store.query_bindings(query=BindingQuery(chat_id=""))

    async def test_side_keyset_project_filters_and_project_aggregates(self) -> None:
        alpha = self.create_binding(
            scope=self.direct,
            project="alpha",
            created_at="2030-01-01T00:00:00+00:00",
            native=True,
        )
        beta = self.create_binding(
            scope=self.topic,
            project="beta",
            created_at="2030-01-02T00:00:00+00:00",
        )
        side_ids: list[str] = []
        for number, binding in enumerate((alpha, beta, beta)):
            side = self.store.create_side_topic(
                app_id="cli_test",
                chat_id="oc_group",
                source_message_id=f"om_source_{number}",
                parent_binding_id=binding.id,
                creator_id="ou_user",
                requires_mention=True,
            )
            timestamp = f"2030-01-0{number + 1}T00:00:00+00:00"
            self.store._connection.execute(
                "UPDATE side_topics SET created_at = ?, updated_at = ? WHERE side_id = ?",
                (timestamp, timestamp, side.id),
            )
            side_ids.append(side.id)
        self.store.transition_side_topic(side_ids[-1], SideTopicState.FAILED)

        first_page = await self.store.query_side_topics(limit=2)
        self.assertEqual(
            [item.side_topic.id for item in first_page.items],
            list(reversed(side_ids[1:])),
        )
        second_page = await self.store.query_side_topics(
            cursor=first_page.next_cursor,
            limit=2,
        )
        self.assertEqual(
            [item.side_topic.id for item in second_page.items],
            [side_ids[0]],
        )
        filtered = await self.store.query_side_topics(
            query=SideTopicQuery(
                project_alias="beta",
                parent_binding_id=beta.id,
                chat_id="oc_group",
                state=SideTopicState.FAILED,
            )
        )
        self.assertEqual(
            [item.side_topic.id for item in filtered.items],
            [side_ids[-1]],
        )
        self.assertEqual(filtered.items[0].project_alias, "beta")

        inactive = self.store.create_admin_binding(
            scope=self.direct,
            project_alias="alpha",
            expected_project_revision=1,
        )
        aggregates = await self.store.query_project_aggregates(limit=1)
        self.assertEqual(aggregates.items[0].project.alias, "alpha")
        self.assertEqual(aggregates.items[0].binding_count, 2)
        self.assertEqual(aggregates.items[0].lazy_binding_count, 1)
        self.assertEqual(aggregates.items[0].materialized_binding_count, 1)
        self.assertEqual(
            aggregates.items[0].last_activated_at,
            "2030-01-01T00:00:00+00:00",
        )
        self.assertIsNone(inactive.activated_at)
        with self.assertRaises(ValueError):
            await self.store.query_side_topics(limit=51)
        with self.assertRaises(ValueError):
            await self.store.query_project_aggregates(limit=51)
        self.assertEqual(aggregates.next_cursor, "alpha")
        beta_page = await self.store.query_project_aggregates(
            cursor=aggregates.next_cursor,
            limit=1,
        )
        self.assertEqual(beta_page.items[0].project.alias, "beta")
        native_projects = await self.store.project_aliases_for_native_threads(
            (alpha.native_thread_id, "native-missing")
        )
        self.assertEqual(
            native_projects,
            {alpha.native_thread_id: "alpha"},
        )

    async def test_query_deadline_does_not_stall_loop_and_admission_is_bounded(
        self,
    ) -> None:
        heartbeats = 0
        running = True

        async def heartbeat() -> None:
            nonlocal heartbeats
            while running:
                heartbeats += 1
                await asyncio.sleep(0)

        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            with self.assertRaises(BindingQueryTimeout):
                await self.store._read_rows(
                    """
                    WITH RECURSIVE numbers(value) AS (
                        VALUES(1) UNION ALL
                        SELECT value + 1 FROM numbers WHERE value < 1000000
                    )
                    SELECT SUM(left_side.value * right_side.value)
                    FROM numbers left_side, numbers right_side
                    """,
                    deadline_seconds=0.002,
                )
        finally:
            running = False
            await heartbeat_task
        self.assertGreater(heartbeats, 1)

        entered = threading.Event()
        release = threading.Event()

        def blocked_query(connection: sqlite3.Connection) -> int:
            entered.set()
            release.wait(1)
            return connection.execute("SELECT 1").fetchone()[0]

        first = asyncio.create_task(
            self.store._submit_query(blocked_query, deadline_seconds=2)
        )
        while not entered.is_set():
            await asyncio.sleep(0)
        with self.assertRaises(BindingQueryBusy):
            await self.store.query_bindings()
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertTrue(self.store._query_futures)
        release.set()
        await self.store.drain_queries()
        self.assertFalse(self.store._query_futures)

    async def test_wal_reader_does_not_block_real_writer_commit(self) -> None:
        project = self.store.get_project("alpha")
        self.create_binding(
            scope=self.direct,
            project="alpha",
            created_at="2030-01-01T00:00:00+00:00",
        )
        entered = threading.Event()
        release = threading.Event()

        def held_read(connection: sqlite3.Connection) -> int:
            connection.execute("BEGIN")
            try:
                count = connection.execute("SELECT COUNT(*) FROM bindings").fetchone()[0]
                entered.set()
                release.wait(1)
                return count
            finally:
                connection.execute("COMMIT")

        reader = asyncio.create_task(
            self.store._submit_query(held_read, deadline_seconds=2)
        )
        while not entered.is_set():
            await asyncio.sleep(0)
        started = time.monotonic()
        disabled = await asyncio.to_thread(
            self.store.set_project_enabled,
            alias=project.alias,
            enabled=False,
            expected_revision=project.revision,
        )
        elapsed = time.monotonic() - started
        self.assertFalse(disabled.enabled)
        self.assertLess(elapsed, 0.25)
        release.set()
        self.assertEqual(await reader, 1)

    async def test_query_plans_use_compatible_keyset_indexes(self) -> None:
        binding_sql, binding_parameters = _binding_inventory_statement(
            query=BindingQuery(),
            cursor=BindingCursor("2030-01-02", "binding-050"),
            limit=26,
        )
        binding_plan = " ".join(
            row[3]
            for row in self.store._connection.execute(
                "EXPLAIN QUERY PLAN " + binding_sql,
                binding_parameters,
            ).fetchall()
        )
        self.assertIn("bindings_global_created", binding_plan)
        self.assertNotIn("USE TEMP B-TREE", binding_plan)

        project_sql, project_parameters = _binding_inventory_statement(
            query=BindingQuery(project_alias="alpha"),
            cursor=None,
            limit=26,
        )
        project_plan = " ".join(
            row[3]
            for row in self.store._connection.execute(
                "EXPLAIN QUERY PLAN " + project_sql,
                project_parameters,
            ).fetchall()
        )
        self.assertIn("bindings_project_created", project_plan)
        self.assertNotIn("USE TEMP B-TREE", project_plan)

        side_sql, side_parameters = _side_inventory_statement(
            query=SideTopicQuery(state=SideTopicState.OPEN),
            cursor=SideTopicCursor("2030-01-02", "side-050"),
            limit=26,
        )
        side_plan = " ".join(
            row[3]
            for row in self.store._connection.execute(
                "EXPLAIN QUERY PLAN " + side_sql,
                side_parameters,
            ).fetchall()
        )
        self.assertIn("side_topics_state_created", side_plan)
        self.assertNotIn("USE TEMP B-TREE", side_plan)


def _create_migratable_v5_database(path: Path) -> None:
    scope = FeishuScope("cli_test", "oc_legacy", ScopeKind.DIRECT)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version(version) VALUES (5);
            CREATE TABLE scopes (
                scope_key TEXT PRIMARY KEY,
                app_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                topic_id TEXT,
                active_binding_id TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE bindings (
                binding_id TEXT PRIMARY KEY,
                scope_key TEXT NOT NULL REFERENCES scopes(scope_key),
                project_alias TEXT NOT NULL,
                native_thread_id TEXT UNIQUE,
                model_id TEXT,
                effort_id TEXT,
                service_tier_id TEXT,
                settings_revision INTEGER NOT NULL DEFAULT 1,
                creator_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                activated_at TEXT NOT NULL,
                ever_activated INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE projects (
                alias TEXT PRIMARY KEY,
                cwd TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                revision INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE dedup_keys (
                dedup_key TEXT PRIMARY KEY,
                expires_at REAL NOT NULL
            );
            CREATE TABLE side_topics (
                side_id TEXT PRIMARY KEY,
                app_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                topic_id TEXT,
                root_message_id TEXT,
                source_message_id TEXT NOT NULL,
                parent_binding_id TEXT NOT NULL,
                creator_id TEXT NOT NULL,
                requires_mention INTEGER NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(app_id, source_message_id)
            );
            INSERT INTO projects(
                alias, cwd, enabled, revision, created_at, updated_at
            ) VALUES (
                'legacy', '/tmp/legacy', 1, 1,
                '2029-01-01T00:00:00+00:00',
                '2029-01-01T00:00:00+00:00'
            );
            """
        )
        connection.execute(
            """
            INSERT INTO scopes(
                scope_key, app_id, chat_id, kind, topic_id,
                active_binding_id, updated_at
            ) VALUES (?, ?, ?, ?, NULL, 'legacy-binding', ?)
            """,
            (
                scope.key,
                scope.app_id,
                scope.chat_id,
                scope.kind.value,
                "2029-01-01T00:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO bindings(
                binding_id, scope_key, project_alias, native_thread_id,
                model_id, effort_id, service_tier_id, settings_revision,
                creator_id, created_at, activated_at, ever_activated
            ) VALUES (?, ?, 'legacy', 'thread-legacy', NULL, NULL, NULL, 3,
                      'ou_legacy', ?, ?, 1)
            """,
            (
                "legacy-binding",
                scope.key,
                "2029-01-01T00:00:00+00:00",
                "2029-01-01T00:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO side_topics(
                side_id, app_id, chat_id, topic_id, root_message_id,
                source_message_id, parent_binding_id, creator_id,
                requires_mention, state, created_at, updated_at
            ) VALUES (
                'legacy-side', 'cli_test', 'oc_legacy', 'omt_side',
                'om_root', 'om_source', 'legacy-binding', 'ou_legacy',
                1, 'closed',
                '2029-01-02T00:00:00+00:00',
                '2029-01-02T00:00:00+00:00'
            )
            """
        )
        connection.execute(
            "INSERT INTO dedup_keys(dedup_key, expires_at) VALUES ('event', 1.0)"
        )
        connection.commit()
    finally:
        connection.close()


def _create_frozen_v5_database(path: Path) -> None:
    scope = FeishuScope("cli_test", "oc_legacy", ScopeKind.DIRECT)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version(version) VALUES (5);
            CREATE TABLE scopes (
                scope_key TEXT PRIMARY KEY,
                app_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                topic_id TEXT,
                active_binding_id TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE bindings (
                binding_id TEXT PRIMARY KEY,
                scope_key TEXT NOT NULL REFERENCES scopes(scope_key),
                project_alias TEXT NOT NULL,
                native_thread_id TEXT UNIQUE,
                model_id TEXT,
                effort_id TEXT,
                service_tier_id TEXT,
                settings_revision INTEGER NOT NULL DEFAULT 1,
                creator_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                activated_at TEXT NOT NULL
            );
            CREATE TABLE projects (
                alias TEXT PRIMARY KEY,
                cwd TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                revision INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO projects(
                alias, cwd, enabled, revision, created_at, updated_at
            ) VALUES (
                'legacy', '/tmp/legacy', 1, 1,
                '2029-01-01T00:00:00+00:00', '2029-01-01T00:00:00+00:00'
            );
            """
        )
        connection.execute(
            """
            INSERT INTO scopes(
                scope_key, app_id, chat_id, kind, topic_id,
                active_binding_id, updated_at
            ) VALUES (?, ?, ?, ?, NULL, NULL, ?)
            """,
            (
                scope.key,
                scope.app_id,
                scope.chat_id,
                scope.kind.value,
                "2029-01-01T00:00:00+00:00",
            ),
        )
        connection.commit()
    finally:
        connection.close()
