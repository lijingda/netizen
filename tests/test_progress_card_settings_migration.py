from __future__ import annotations

import dataclasses
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from netizen_cli import database_migrations as migrations
from netizen_cli.bindings import BindingFeedbackRevisionConflict, BindingStore, validate_channel_database
from netizen_cli.domain import FeishuScope, MentionContextMode, ScopeKind
from netizen_cli.migrations import v18
from netizen_cli.migrations.schema import require_schema
from netizen_cli.progress_card_settings import ProgressCardSettings
from netizen_cli.session_settings import BindingTaskFeedback, BindingTurnSettings, SessionSettings


class ProgressCardSettingsMigrationTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "channel.sqlite3"
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executescript((Path(__file__).parent / "fixtures/channel_v17.sql").read_text())

    def contents(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return tuple(connection.iterdump())

    def test_v17_migration_defaults_all_settings_without_touching_other_metadata(self):
        with closing(sqlite3.connect(self.path)) as connection:
            tables = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name!='schema_version'",
            )]
            before = {table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
        plan = migrations.plan_channel_database(self.path)
        self.assertEqual(plan, {"source_version": 17, "target_version": 19,
                                "steps": [{"from_version": 17, "to_version": 18}, {"from_version": 18, "to_version": 19}]})
        self.assertEqual(migrations.migrate_channel_database(self.path, expected_source_version=17), plan)
        with closing(sqlite3.connect(self.path)) as connection:
            for table, rows in before.items():
                actual = connection.execute(f"SELECT * FROM {table}").fetchall()
                if rows:
                    actual = [row[:len(rows[0])] for row in actual]
                if table in {"session_defaults", "schedule_plans"}:
                    columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
                    index = columns.index("session_settings_json")
                    for old, new in zip(rows, actual, strict=True):
                        if old[index] is None:
                            self.assertIsNone(new[index])
                        else:
                            settings = json.loads(new[index])
                            self.assertEqual(settings.pop("progress_card_settings"), ProgressCardSettings().to_dict())
                            self.assertEqual(settings, json.loads(old[index]))
                        self.assertEqual(new[:index] + new[index + 1:], old[:index] + old[index + 1:])
                else:
                    self.assertEqual(actual, rows, table)
        validate_channel_database(self.path)
        with closing(BindingStore(self.path)) as store:
            self.assertEqual(store.get("binding").task_feedback.progress_card_settings, ProgressCardSettings())
            self.assertEqual(store.defaults.get("app", "default-chat").session_settings.task_feedback.progress_card_settings,
                             ProgressCardSettings())
            self.assertEqual(store.schedules.get("plan-topic").session_settings.task_feedback.progress_card_settings,
                             ProgressCardSettings())
            for identifier in ("plan-binding", "deleted-binding"):
                self.assertIsNone(store.schedules.get(identifier, include_deleted=True).session_settings)
            self.assertEqual(store.schedules.get("deleted-new_topic", include_deleted=True).session_settings, SessionSettings())
        unchanged = self.contents()
        self.assertEqual(migrations.migrate_channel_database(self.path, expected_source_version=19)["steps"], [])
        self.assertEqual(self.contents(), unchanged)

    def test_fresh_and_migrated_constraints_are_equivalent(self):
        migrations.migrate_channel_database(self.path, expected_source_version=17)
        with closing(BindingStore()) as fresh, closing(sqlite3.connect(self.path)) as upgraded:
            upgraded.row_factory = sqlite3.Row
            statements = tuple(row[0] for row in fresh._connection.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
                "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END",
            ))
            require_schema(upgraded, statements)

    def test_failed_conversion_rolls_back_schema_json_and_version(self):
        def fail(connection):
            v18.apply(connection)
            raise RuntimeError("after progress card conversion")

        before = self.contents()
        step = migrations.Migration(17, 18, fail, v18.validate)
        with patch.object(migrations, "MIGRATIONS", (*migrations.MIGRATIONS[:3], step, *migrations.MIGRATIONS[4:])):
            with self.assertRaisesRegex(RuntimeError, "after progress card conversion"):
                migrations.migrate_channel_database(self.path, expected_source_version=17)
        self.assertEqual(self.contents(), before)
        self.assertEqual(migrations.plan_channel_database(self.path)["source_version"], 17)

    def test_current_validation_rejects_invalid_settings_without_repair(self):
        migrations.migrate_channel_database(self.path, expected_source_version=17)
        valid = ProgressCardSettings().to_dict()
        invalid_values = (False, {}, {**valid, "extra": True}, {**valid, "show_plan": 1},
                          {**valid, "operation_limit": True}, {**valid, "commentary_limit": 9},
                          {**valid, "operation_max_chars": 241}, {**valid, "organization": "other"})
        for table, column, key, identifier in (
            ("bindings", "progress_card_settings_json", "binding_id", "binding"),
            ("session_defaults", "session_settings_json", "id", "default-chat"),
            ("schedule_plans", "session_settings_json", "plan_id", "plan-topic"),
        ):
            for invalid in invalid_values:
                with self.subTest(table=table, invalid=invalid):
                    with closing(sqlite3.connect(self.path)) as connection:
                        original = connection.execute(f"SELECT {column} FROM {table} WHERE {key}=?", (identifier,)).fetchone()[0]
                        value = invalid if table == "bindings" else {**json.loads(original), "progress_card_settings": invalid}
                        connection.execute(f"UPDATE {table} SET {column}=? WHERE {key}=?", (json.dumps(value), identifier))
                        connection.commit()
                    before = self.contents()
                    for validate in (validate_channel_database, migrations.plan_channel_database):
                        with self.assertRaises(RuntimeError):
                            validate(self.path)
                    self.assertEqual(self.contents(), before)
                    with closing(sqlite3.connect(self.path)) as connection:
                        connection.execute(f"UPDATE {table} SET {column}=? WHERE {key}=?", (original, identifier))
                        connection.commit()


class ProgressCardSettingsStorageTest(unittest.TestCase):
    def test_preferences_survive_reopen_fork_defaults_and_unrelated_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "channel.sqlite3"
            card = ProgressCardSettings(show_commentary=False, show_plan=False, commentary_limit=8,
                                        operation_limit=16, commentary_max_chars=320,
                                        operation_max_chars=240, organization="time")
            feedback = BindingTaskFeedback(True, True, False, card)
            with closing(BindingStore(path)) as store:
                project = store.bootstrap_project(alias="p", cwd=directory)
                binding = store.create_channel_binding(
                    scope=FeishuScope("app", "chat", ScopeKind.GROUP), project_alias="p",
                    creator_id="user", task_feedback=feedback,
                )
                store.assign_native_thread_id(binding.id, "native-source")
                binding = store.get(binding.id)
                rule = store.defaults.save(
                    app_id="app", kind="chat", chat_id="chat", keyword=None, project="p",
                    session_settings=SessionSettings.from_binding(binding), rule_id=None, expected_revision=None,
                )
                fork = store.create_fork_binding(
                    scope=FeishuScope("app", "chat", ScopeKind.TOPIC, "fork-topic"), source=binding,
                    native_thread_id="native-fork", root_message_id="root", creator_id="user",
                    expected_project_revision=project.revision, target_chat_kind="group",
                )
                self.assertEqual(fork.task_feedback, feedback)
                updated = store.set_configuration(
                    binding_id=binding.id, expected_settings_revision=1, expected_context_revision=1,
                    expected_feedback_revision=1, settings=BindingTurnSettings("model", "high", "default"),
                    task_feedback=dataclasses.replace(feedback, completion_mention_enabled=True),
                    message_context_mode=MentionContextMode.CURRENT_ONLY, context_anchor=None,
                )
                self.assertEqual(updated.task_feedback.progress_card_settings, card)
                self.assertEqual(updated.feedback_revision, 2)
                args = dict(binding_id=binding.id, expected_settings_revision=2, expected_context_revision=1,
                            expected_feedback_revision=2, settings=updated.turn_settings,
                            message_context_mode=MentionContextMode.CURRENT_ONLY, context_anchor=None)
                self.assertEqual(store.set_configuration(**args, task_feedback=updated.task_feedback), updated)
                changed = store.set_configuration(
                    **args, task_feedback=dataclasses.replace(updated.task_feedback,
                                                             progress_card_settings=ProgressCardSettings()),
                )
                self.assertEqual(changed.feedback_revision, 3)
                self.assertEqual(changed.settings_revision, 2)
                with self.assertRaises(BindingFeedbackRevisionConflict):
                    store.set_configuration(**args, task_feedback=feedback)
                self.assertEqual(store.defaults.get("app", rule.id).session_settings.task_feedback, feedback)
            validate_channel_database(path)
            with closing(BindingStore(path)) as reopened:
                self.assertEqual(reopened.get(fork.id).task_feedback, feedback)
                self.assertEqual(reopened.get(binding.id).task_feedback.progress_card_settings, ProgressCardSettings())
                self.assertEqual(reopened.defaults.get("app", rule.id).session_settings.task_feedback, feedback)
