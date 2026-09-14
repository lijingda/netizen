from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from netizen.bindings import BindingStore, SideTopicState, migrate_channel_database, validate_channel_database
from netizen.domain import FeishuScope, ScopeKind
from netizen.schedules.models import ScheduleRule
from netizen.session_settings import BindingTaskFeedback, SessionSettings


def create_v10_database(path: Path) -> tuple[str, str]:
    store = BindingStore(path)
    store.register_project(alias="p", cwd="/tmp/retained")
    binding = store.create_binding(
        scope=FeishuScope("app", "chat", ScopeKind.DIRECT), project_alias="p",
        creator_id="owner", task_feedback=BindingTaskFeedback(True, False, False),
    )
    store.assign_native_thread_id(binding.id, "native-retained")
    side = store.create_side_topic(
        app_id="app", chat_id="chat", source_message_id="side-source",
        parent_binding_id=binding.id, creator_id="owner", requires_mention=False,
    )
    store.set_side_topic_root(side.id, "side-root")
    store.open_side_topic(side.id, "side-topic")
    store.transition_side_topic(side.id, SideTopicState.CLOSED)
    store.register_project(alias="deleted", cwd="/tmp/deleted")
    store._connection.execute("UPDATE projects SET deleted=1, enabled=0 WHERE alias='deleted'")
    plan = store.schedules.create(
        name="retained plan", instructions="retained instruction", project_alias="p",
        app_id="app", chat_id="chat", schedule=ScheduleRule("daily", "UTC", at="09:00"),
        request_id="retained-request", now=100,
        session_settings=SessionSettings(task_feedback=BindingTaskFeedback(True, True, False)),
    )
    deleted = store.schedules.create(
        name="deleted plan", instructions="cleared instruction", project_alias="p",
        app_id="app", chat_id="chat", schedule=ScheduleRule("daily", "UTC", at="09:00"),
        request_id="deleted-create", now=100,
    )
    store.schedules.delete(deleted.plan_id, expected_revision=1, request_id="deleted-delete", now=101)
    store.close()
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE bindings DROP COLUMN completion_mention_enabled")
        for plan_id, settings in connection.execute("SELECT plan_id, session_settings_json FROM schedule_plans WHERE deleted=0").fetchall():
            value = json.loads(settings)
            del value["completion_mention_enabled"]
            connection.execute("UPDATE schedule_plans SET session_settings_json=? WHERE plan_id=?", (json.dumps(value), plan_id))
        connection.execute("UPDATE schema_version SET version=10")
    connection.close()
    return binding.id, plan.plan_id


class DatabaseMigrationTest(unittest.TestCase):
    def test_install_upgrade_preserves_metadata_and_enables_existing_sessions(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "channel.sqlite3"
            binding_id, plan_id = create_v10_database(path)
            with sqlite3.connect(path) as connection:
                before_binding = connection.execute("SELECT * FROM bindings").fetchone()
                preserved_tables = {
                    table: connection.execute(f"SELECT * FROM {table}").fetchall()
                    for table in ("scopes", "projects", "side_topics", "schedule_requests", "schedule_runs")
                }
                deleted_plan = connection.execute("SELECT * FROM schedule_plans WHERE deleted=1").fetchone()
            for opener in (BindingStore, validate_channel_database):
                with self.assertRaisesRegex(RuntimeError, "unsupported.*schema version"):
                    opener(path)
            migrate_channel_database(path)
            validate_channel_database(path)
            store = BindingStore(path)
            try:
                binding = store.get(binding_id)
                self.assertEqual(binding.native_thread_id, "native-retained")
                self.assertEqual(binding.feedback_revision, 1)
                self.assertEqual(binding.task_feedback, BindingTaskFeedback(True, False, True))
                self.assertEqual(store.schedules.get(plan_id).session_settings.task_feedback, BindingTaskFeedback(True, True, True))
                self.assertEqual(store._connection.execute("SELECT * FROM bindings").fetchone()[:-1], before_binding)
                for table, rows in preserved_tables.items():
                    self.assertEqual([tuple(row) for row in store._connection.execute(f"SELECT * FROM {table}")], rows)
                self.assertEqual(tuple(store._connection.execute("SELECT * FROM schedule_plans WHERE deleted=1").fetchone()), deleted_plan)
                self.assertEqual(store.schedules.get(plan_id).revision, 1)
            finally:
                store.close()
            before = path.read_bytes()
            migrate_channel_database(path)
            self.assertEqual(path.read_bytes(), before)

    def test_malformed_v10_plan_rolls_back_schema_and_settings_together(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "channel.sqlite3"
            create_v10_database(path)
            with sqlite3.connect(path) as connection:
                connection.execute("UPDATE schedule_plans SET session_settings_json='{}'")
            with sqlite3.connect(path) as connection:
                before = "\n".join(connection.iterdump())
            with self.assertRaisesRegex(RuntimeError, "v10 scheduled session settings"):
                migrate_channel_database(path)
            with sqlite3.connect(path) as connection:
                self.assertEqual("\n".join(connection.iterdump()), before)
