from __future__ import annotations

import asyncio
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from netizen_cli.bindings import (
    BindingConflict, BindingStore, BindingTaskFeedback, BindingTurnSettings,
    ProjectDisabled, ProjectRevisionConflict, ScopeConflict, ScopeNotFound,
)
from netizen_cli.domain import FeishuScope, MentionContextMode, MessageContextAnchor, ScopeKind


class PersistentForkBindingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = BindingStore()
        self.addCleanup(self.store.close)
        self.store.bootstrap_project(alias="work", cwd="/tmp/work")
        self.origin = FeishuScope("app", "source-chat", ScopeKind.GROUP)
        self.target = FeishuScope("app", "target-chat", ScopeKind.TOPIC, "topic")
        self.source = self.store.create_channel_binding(
            scope=self.origin, project_alias="work", creator_id="source-user",
            turn_settings=BindingTurnSettings("model", "high", "priority"),
            task_feedback=BindingTaskFeedback(True, True, True),
            message_context_mode=MentionContextMode.CATCH_UP,
            context_anchor=MessageContextAnchor("source-anchor", 1000),
        )
        self.store.assign_native_thread_id(self.source.id, "source-native")
        self.source = self.store.get(self.source.id)

    def create(self, **overrides):
        return self.store.create_fork_binding(**{
            "scope": self.target, "source": self.source,
            "native_thread_id": "fork-native", "root_message_id": "root",
            "creator_id": "fork-user", "expected_project_revision": 1,
            "target_chat_kind": "group",
            "context_anchor": MessageContextAnchor("target-seed", 2000),
            **overrides,
        })

    def test_complete_fork_copies_intent_and_uses_destination_anchor(self) -> None:
        branch = self.create().binding
        self.assertEqual(branch.native_thread_id, "fork-native")
        self.assertEqual(branch.project_alias, self.source.project_alias)
        self.assertEqual(branch.turn_settings, self.source.turn_settings)
        self.assertEqual(branch.task_feedback, self.source.task_feedback)
        self.assertEqual(branch.context_anchor, MessageContextAnchor("target-seed", 2000))
        self.assertEqual(self.store.active_binding(self.origin.key).id, self.source.id)
        self.assertEqual(self.store.active_binding(self.target.key).id, branch.id)
        self.assertEqual(branch.creator_id, "fork-user")

    def test_early_ordinary_session_is_never_overwritten(self) -> None:
        existing = self.store.create_channel_binding(
            scope=self.target, project_alias="work", creator_id="early-user",
        )
        with self.assertRaises(ScopeConflict):
            self.create()
        self.assertEqual(self.store.active_binding(self.target.key).id, existing.id)
        self.assertEqual(len(self.store.list_bindings(self.target.key)), 1)
        self.assertIsNone(self.store.get(existing.id).native_thread_id)

    def test_p2p_target_only_normalizes_future_catch_up_not_source_or_other_settings(self) -> None:
        branch = self.create(target_chat_kind="p2p").binding
        self.assertEqual(branch.message_context_mode, MentionContextMode.CURRENT_ONLY)
        self.assertIsNone(branch.context_anchor)
        self.assertEqual(branch.turn_settings, self.source.turn_settings)
        self.assertEqual(branch.task_feedback, self.source.task_feedback)
        self.assertEqual(branch.native_thread_id, "fork-native")
        self.assertEqual(self.store.get(self.source.id), self.source)

    def test_unknown_target_kind_cannot_create_binding(self) -> None:
        with self.assertRaises(ValueError):
            self.create(target_chat_kind="unknown")
        with self.assertRaises(ScopeNotFound):
            self.store.get_scope(self.target.key)

    def test_duplicate_native_identity_rolls_back_scope_and_current(self) -> None:
        branch = self.create().binding
        other = FeishuScope("app", "target-chat", ScopeKind.TOPIC, "other-topic")
        with self.assertRaises(BindingConflict):
            self.create(scope=other)
        with self.assertRaises(ScopeNotFound):
            self.store.get_scope(other.key)
        self.assertEqual(self.store.active_binding(self.target.key).id, branch.id)

    def test_project_revision_change_rolls_back_whole_binding(self) -> None:
        with self.assertRaises(ProjectRevisionConflict):
            self.create(expected_project_revision=2)
        with self.assertRaises(ScopeNotFound):
            self.store.get_scope(self.target.key)

    def test_project_delete_reservation_prevents_late_binding(self) -> None:
        snapshot = asyncio.run(self.store.preview_project_delete("work"))
        self.store.begin_project_delete(
            alias="work", expected_revision=1,
            expected_inventory_fingerprint=snapshot.fingerprint,
        )
        with self.assertRaises(ProjectDisabled):
            self.create()
        with self.assertRaises(ScopeNotFound):
            self.store.get_scope(self.target.key)

    def test_side_route_cannot_be_reused(self) -> None:
        route = self.store.create_side_topic(
            app_id="app", chat_id="target-chat", source_message_id="side-command",
            parent_binding_id=self.source.id, creator_id="user", requires_mention=True,
        )
        self.store.set_side_topic_root(route.id, "root")
        with self.assertRaises(ScopeConflict):
            self.create()
        with self.assertRaises(ScopeNotFound):
            self.store.get_scope(self.target.key)

    def test_current_only_and_implicit_model_stay_implicit(self) -> None:
        source = self.store.create_channel_binding(
            scope=self.origin, project_alias="work", creator_id="source-user",
        )
        self.store.assign_native_thread_id(source.id, "other-source")
        branch = self.create(source=self.store.get(source.id), context_anchor=None).binding
        self.assertIsNone(branch.turn_settings)
        self.assertIsNone(branch.context_anchor)
        self.assertEqual(branch.message_context_mode, MentionContextMode.CURRENT_ONLY)

    def test_main_replacement_keeps_previous_binding_and_returns_exact_pointer(self):
        main = FeishuScope("app", "target-chat", ScopeKind.GROUP)
        previous = self.store.create_channel_binding(scope=main, project_alias="work", creator_id="previous")
        result = self.create(scope=main)
        self.assertEqual(result.previous_active_binding_id, previous.id)
        self.assertEqual(self.store.active_binding(main.key).id, result.binding.id)
        self.assertFalse(self.store.get(previous.id).active)
        self.assertEqual(len(self.store.list_bindings(main.key)), 2)
        self.assertEqual(result.binding.turn_settings, self.source.turn_settings)
        self.assertEqual(result.binding.context_anchor, MessageContextAnchor("target-seed", 2000))

    def test_main_previous_pointer_is_read_inside_creation_transaction(self):
        main = FeishuScope("app", "target-chat", ScopeKind.GROUP)
        previous = self.store.create_channel_binding(scope=main, project_alias="work", creator_id="previous")
        transaction = self.store._transaction
        @contextmanager
        def archive_before_transaction():
            # Archive's pointer update does not take the caller's Scope lock.
            # Inject it at the last boundary before the writer transaction.
            with patch.object(self.store, "_transaction", transaction):
                self.store.archive_binding(previous.id)
            with transaction():
                yield
        with patch.object(self.store, "_transaction", archive_before_transaction):
            result = self.create(scope=main)
        self.assertIsNone(result.previous_active_binding_id)
        self.assertEqual(self.store.active_binding(main.key).id, result.binding.id)
        self.assertFalse(self.store.get(previous.id).active)

    def test_direct_main_has_no_catch_up_and_empty_previous_pointer(self):
        main = FeishuScope("app", "private-chat", ScopeKind.DIRECT)
        result = self.create(scope=main, target_chat_kind="p2p")
        self.assertIsNone(result.previous_active_binding_id)
        self.assertEqual(result.binding.message_context_mode, MentionContextMode.CURRENT_ONLY)
        self.assertIsNone(result.binding.context_anchor)
        self.assertEqual(self.store.get(self.source.id), self.source)

    def test_main_scope_kind_app_and_source_identity_must_match(self):
        for scope, kind, error in (
            (self.origin, "group", ValueError),
            (FeishuScope("app", "other", ScopeKind.DIRECT), "group", ValueError),
            (FeishuScope("app", "other", ScopeKind.GROUP), "p2p", ValueError),
            (FeishuScope("other-app", "other", ScopeKind.GROUP), "group", ScopeConflict),
        ):
            with self.subTest(scope=scope, kind=kind), self.assertRaises(error):
                self.create(scope=scope, target_chat_kind=kind)
        self.assertEqual(self.store.active_binding(self.origin.key), self.source)

    def test_main_replacement_rolls_back_on_duplicate_native_or_stale_project(self):
        self.create()
        main = FeishuScope("app", "target-chat", ScopeKind.GROUP)
        previous = self.store.create_channel_binding(scope=main, project_alias="work", creator_id="previous")
        for changes, error in (({}, BindingConflict),
                               ({"expected_project_revision": 2}, ProjectRevisionConflict)):
            with self.subTest(changes=changes), self.assertRaises(error):
                self.create(scope=main, **changes)
            self.assertEqual(self.store.active_binding(main.key), previous)
            self.assertEqual(len(self.store.list_bindings(main.key)), 1)
