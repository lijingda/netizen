from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from netizen.autonomy.models import Candidate, DecisionToken
from netizen.bindings import (
    BindingContextRevisionConflict,
    BindingStore,
    BindingTaskFeedback,
    BindingTurnSettings,
    SCHEMA_VERSION,
    validate_channel_database,
)
from netizen.codex_runtime import CodexRuntime, SteerRace
from netizen.domain import FeishuScope, MentionContextMode, MessageContextAnchor, ScopeKind


class AutonomyBindingsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = BindingStore()
        self.addCleanup(self.store.close)
        self.scope = FeishuScope("app", "chat", ScopeKind.GROUP)
        self.store.bootstrap_project(alias="test", cwd="/workspace/test")

    def create(self, **kwargs: Any) -> Any:
        return self.store.create_channel_binding(
            **{"scope": self.scope, "project_alias": "test", "creator_id": "user", **kwargs},
        )

    def configure(self, binding: Any, **kwargs: Any) -> Any:
        values = dict(
            binding_id=binding.id,
            expected_settings_revision=binding.settings_revision,
            expected_context_revision=binding.context_revision,
            expected_feedback_revision=binding.feedback_revision,
            settings=binding.turn_settings, task_feedback=binding.task_feedback,
            message_context_mode=binding.message_context_mode, context_anchor=None,
        )
        return self.store.set_configuration(**{**values, **kwargs})

    def record(self, binding: Any) -> None:
        settings = self.store.autonomy.receive(binding.id)
        token = DecisionToken(binding.id, settings.revision, 1, settings.received, settings.last_accepted, "message")
        self.assertTrue(self.store.autonomy.accepted(token, Candidate("message", "hello"), "turn"))
        self.assertTrue(self.store.autonomy.final(binding.id, "turn", "response"))

    def test_existing_and_admin_creation_remain_ordinary_with_no_feature_records(self) -> None:
        ordinary = self.create()
        admin = self.store.create_admin_binding(
            scope=self.scope, project_alias="test", expected_project_revision=1,
        )
        self.assertFalse(self.store.autonomy.is_enabled(ordinary.id))
        self.assertFalse(self.store.autonomy.is_enabled(admin.id))
        self.assertEqual(self.store._connection.execute("SELECT count(*) FROM autonomy_bindings").fetchone()[0], 0)
        unchanged = self.configure(ordinary)
        self.assertEqual(unchanged, ordinary)
        self.assertEqual(self.store._connection.execute("SELECT count(*) FROM autonomy_bindings").fetchone()[0], 0)

    def test_group_and_topic_creation_activate_the_independent_flag(self) -> None:
        for method in (self.store.create_binding, self.store.create_channel_binding):
            for scope in (self.scope, FeishuScope("app", "chat", ScopeKind.TOPIC, "topic")):
                with self.subTest(method=method.__name__, scope=scope.kind):
                    binding = method(scope=scope, project_alias="test", creator_id="user", autonomy_enabled=True)
                    self.assertTrue(self.store.autonomy.is_enabled(binding.id))
                    self.assertIs(binding.message_context_mode, MentionContextMode.CURRENT_ONLY)
                    self.assertEqual(binding.context_revision, 1)

    def test_invalid_scope_mode_and_nonboolean_are_rejected_without_creating(self) -> None:
        cases = (
            {"scope": FeishuScope("app", "direct", ScopeKind.DIRECT), "autonomy_enabled": True},
            {"autonomy_enabled": True, "message_context_mode": MentionContextMode.CATCH_UP, "context_anchor": MessageContextAnchor("anchor", 1)},
            {"autonomy_enabled": 1},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.create(**kwargs)
        self.assertEqual(self.store._connection.execute("SELECT count(*) FROM bindings").fetchone()[0], 0)
        self.assertEqual(self.store._connection.execute("SELECT count(*) FROM scopes").fetchone()[0], 0)
        direct = self.create(scope=FeishuScope("app", "direct", ScopeKind.DIRECT))
        with self.assertRaises(ValueError):
            self.configure(direct, autonomy_enabled=True)
        for enabled in (1, "yes"):
            with self.subTest(enabled=enabled), self.assertRaises(ValueError):
                self.configure(direct, autonomy_enabled=enabled)
        self.assertEqual(self.store.get(direct.id), direct)

    def test_create_failure_rolls_back_flag_binding_and_active_pointer(self) -> None:
        existing = self.create()
        original = self.store.autonomy.set_enabled

        def fail(binding_id: str, enabled: bool) -> None:
            original(binding_id, enabled)
            raise OSError("write failed")

        with patch.object(self.store.autonomy, "set_enabled", side_effect=fail), self.assertRaises(OSError):
            self.create(autonomy_enabled=True)
        self.assertEqual(self.store.active_binding(self.scope.key).id, existing.id)
        self.assertEqual([item.id for item in self.store.list_bindings(self.scope.key)], [existing.id])
        self.assertEqual(self.store._connection.execute("SELECT count(*) FROM autonomy_bindings").fetchone()[0], 0)

    def test_configuration_failure_rolls_back_all_ordinary_and_experiment_values(self) -> None:
        binding = self.create()
        original = self.store.autonomy.set_enabled

        def fail(binding_id: str, enabled: bool) -> None:
            original(binding_id, enabled)
            raise OSError("write failed")

        with patch.object(self.store.autonomy, "set_enabled", side_effect=fail), self.assertRaises(OSError):
            self.configure(
                binding, autonomy_enabled=True, settings=BindingTurnSettings("model", "high", "default"),
                task_feedback=BindingTaskFeedback(True, True, False),
            )
        self.assertEqual(self.store.get(binding.id), binding)
        self.assertFalse(self.store.autonomy.is_enabled(binding.id))
        self.assertEqual(self.store.autonomy.settings(binding.id).revision, 0)

    def test_toggle_invalidates_context_revision_and_none_preserves_enabled_state(self) -> None:
        binding = self.create()
        enabled = self.configure(binding, autonomy_enabled=True)
        self.assertEqual(enabled.context_revision, binding.context_revision + 1)
        self.assertEqual(enabled.settings_revision, binding.settings_revision)
        self.assertEqual(enabled.feedback_revision, binding.feedback_revision)
        with self.assertRaises(BindingContextRevisionConflict):
            self.configure(binding, autonomy_enabled=False)
        self.assertTrue(self.store.autonomy.is_enabled(binding.id))
        unchanged = self.configure(enabled, autonomy_enabled=None)
        self.assertEqual(unchanged, enabled)
        self.assertTrue(self.store.autonomy.is_enabled(binding.id))
        unchanged = self.configure(enabled, autonomy_enabled=True)
        self.assertEqual(unchanged, enabled)
        disabled = self.configure(enabled, autonomy_enabled=False)
        self.assertEqual(disabled.context_revision, enabled.context_revision + 1)
        self.assertFalse(self.store.autonomy.is_enabled(binding.id))

    def test_catch_up_disables_autonomy_and_reenable_keeps_history(self) -> None:
        binding = self.create(autonomy_enabled=True)
        self.record(binding)
        previous = self.store.autonomy.context(binding.id)
        with self.assertRaises(ValueError):
            self.configure(binding, autonomy_enabled=True, message_context_mode=MentionContextMode.CATCH_UP,
                context_anchor=MessageContextAnchor("anchor", 10))
        caught_up = self.configure(binding, message_context_mode=MentionContextMode.CATCH_UP,
            context_anchor=MessageContextAnchor("anchor", 10))
        self.assertFalse(self.store.autonomy.is_enabled(binding.id))
        self.assertEqual(caught_up.context_revision, binding.context_revision + 1)
        self.assertEqual(self.store.autonomy.context(binding.id), previous)
        enabled = self.configure(caught_up, message_context_mode=MentionContextMode.CURRENT_ONLY, autonomy_enabled=True)
        self.assertIsNone(enabled.context_anchor)
        self.assertTrue(self.store.autonomy.is_enabled(binding.id))
        self.assertEqual(self.store.autonomy.context(binding.id), previous)

    def test_binding_and_project_deletion_leave_no_autonomous_records(self) -> None:
        binding = self.create(autonomy_enabled=True)
        self.record(binding)
        preview = self.store.preview_project_delete(alias="test")
        reserved = self.store.begin_project_delete(
            alias="test", expected_revision=preview.project.revision, expected_inventory_fingerprint=preview.fingerprint,
        )
        self.store.delete_binding(binding.id)
        self.store.finish_project_delete(
            alias="test", expected_revision=reserved.project.revision, expected_inventory_fingerprint=reserved.fingerprint,
        )
        self.assertEqual(self.store._connection.execute("SELECT count(*) FROM autonomy_bindings").fetchone()[0], 0)
        self.assertEqual(self.store._connection.execute("SELECT count(*) FROM autonomy_records").fetchone()[0], 0)
        self.assertEqual(self.store._connection.execute("SELECT count(*) FROM autonomy_pending_turns").fetchone()[0], 0)
        self.assertEqual(list(self.store._connection.execute("PRAGMA foreign_key_check")), [])

    def test_current_schema_reopens_persisted_autonomy_and_old_schema_is_readonly_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "channel.sqlite"
            store = BindingStore(path)
            binding = store.create_binding(scope=self.scope, project_alias="test", creator_id="user", autonomy_enabled=True)
            store.close()
            validate_channel_database(path)
            reopened = BindingStore(path)
            try:
                self.assertTrue(reopened.autonomy.is_enabled(binding.id))
                self.assertEqual(reopened._connection.execute("SELECT version FROM schema_version").fetchone()[0], 14)
                self.assertEqual(SCHEMA_VERSION, 14)
            finally:
                reopened.close()
            connection = sqlite3.connect(path)
            connection.execute("UPDATE schema_version SET version=13")
            connection.commit()
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.close()
            before = {item.name: item.read_bytes() for item in Path(directory).iterdir()}
            for opener in (validate_channel_database, BindingStore):
                with self.subTest(opener=opener.__name__), self.assertRaisesRegex(RuntimeError, "unsupported.*schema version"):
                    opener(path)
                self.assertEqual({item.name: item.read_bytes() for item in Path(directory).iterdir()}, before)


class AutonomyRuntimeConfigurationTest(unittest.IsolatedAsyncioTestCase):
    async def test_toggle_uses_existing_runtime_admission_invalidation(self) -> None:
        store = BindingStore()
        self.addCleanup(store.close)
        binding = store.create_binding(
            scope=FeishuScope("app", "chat", ScopeKind.GROUP), project_alias="test", creator_id="user",
        )
        runtime = CodexRuntime(codex=SimpleNamespace(), bindings=store, terminal_cleanup=SimpleNamespace())
        before = await runtime.capture_submission_admission(binding.id)
        updated = await runtime.configure_context_exact(
            binding_id=binding.id, expected_settings_revision=1, expected_context_revision=1, expected_feedback_revision=1,
            settings=None, task_feedback=binding.task_feedback, message_context_mode=MentionContextMode.CURRENT_ONLY,
            context_anchor=None, autonomy_enabled=True,
        )
        after = await runtime.capture_submission_admission(binding.id)
        self.assertTrue(store.autonomy.is_enabled(binding.id))
        self.assertGreater(after.revision, before.revision)
        self.assertEqual(after.context_revision, updated.context_revision)
        with self.assertRaises(SteerRace):
            await runtime.submit(binding=updated, cwd=Path("/workspace/test"), input="hello", owner_id="user", origin=None, admission=before)


if __name__ == "__main__":
    unittest.main()
