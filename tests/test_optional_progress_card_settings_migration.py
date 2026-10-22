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
from netizen_cli.bindings import BindingStore, validate_channel_database
from netizen_cli.domain import FeishuScope, MentionContextMode, ScopeKind
from netizen_cli.migrations import v19
from netizen_cli.progress_card_settings import ProgressCardSettings
from netizen_cli.schedules.models import ScheduleRule
from netizen_cli.session_settings import BindingTaskFeedback, SessionSettings
from netizen_cli.worktrees.policy import WorkspacePolicy


class OptionalProgressCardSettingsMigrationTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "channel.sqlite3"
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executescript((Path(__file__).parent / "fixtures/channel_v18.sql").read_text())

    def contents(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return tuple(connection.iterdump())

    def test_upgrade_preserves_all_explicit_settings_and_changes_only_version(self):
        custom = ProgressCardSettings(organization="time", operation_limit=16)
        with closing(sqlite3.connect(self.path)) as connection:
            raw = connection.execute("SELECT session_settings_json FROM schedule_plans WHERE plan_id='plan-topic'").fetchone()[0]
            settings = json.loads(raw)
            settings["progress_card_settings"] = custom.to_dict()
            connection.execute("UPDATE schedule_plans SET session_settings_json=? WHERE plan_id='plan-topic'", (json.dumps(settings),))
            connection.commit()
        before = self.contents()
        expected = {"source_version": 18, "target_version": 19,
                    "steps": [{"from_version": 18, "to_version": 19}]}
        with patch.object(migrations, "MIGRATIONS", migrations.MIGRATIONS[:5]), patch.object(migrations, "SCHEMA_VERSION", 19):
            self.assertEqual(migrations.plan_channel_database(self.path), expected)
            self.assertEqual(migrations.migrate_channel_database(self.path, expected_source_version=18), expected)
        self.assertEqual(self.contents(), tuple(
            line.replace('INSERT INTO "schema_version" VALUES(18);', 'INSERT INTO "schema_version" VALUES(19);')
            for line in before
        ))
        migrations.migrate_channel_database(self.path, expected_source_version=19)
        validate_channel_database(self.path)
        with closing(BindingStore(self.path)) as store:
            self.assertEqual(store.get("binding").task_feedback.progress_card_settings, ProgressCardSettings())
            self.assertEqual(store.defaults.get("app", "default-chat").session_settings.task_feedback.progress_card_settings,
                             ProgressCardSettings())
            self.assertEqual(store.schedules.get("plan-topic").session_settings.task_feedback.progress_card_settings, custom)
        before_noop = self.contents()
        self.assertEqual(migrations.migrate_channel_database(self.path, expected_source_version=20)["steps"], [])
        self.assertEqual(self.contents(), before_noop)

    def test_old_version_rejects_null_intent_without_silently_reinterpreting_data(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("UPDATE bindings SET progress_card_settings_json='null'")
            connection.commit()
        before = self.contents()
        with self.assertRaisesRegex(RuntimeError, "schema v18 persisted configuration"):
            migrations.migrate_channel_database(self.path, expected_source_version=18)
        self.assertEqual(self.contents(), before)

    def test_target_validation_failure_rolls_back_version_and_data(self):
        def fail(connection):
            v19.validate(connection)
            raise RuntimeError("after nullable intent validation")

        before = self.contents()
        step = migrations.Migration(18, 19, v19.apply, fail)
        with patch.object(migrations, "MIGRATIONS", (*migrations.MIGRATIONS[:4], step, *migrations.MIGRATIONS[5:])):
            with self.assertRaisesRegex(RuntimeError, "after nullable intent validation"):
                migrations.migrate_channel_database(self.path, expected_source_version=18)
        self.assertEqual(self.contents(), before)


class OptionalProgressCardSettingsStorageTest(unittest.TestCase):
    def test_absent_and_explicit_default_intent_remain_distinct_across_storage_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "channel.sqlite3"
            with closing(BindingStore(path)) as store:
                project = store.bootstrap_project(alias="p", cwd=directory)
                binding = store.create_channel_binding(
                    scope=FeishuScope("app", "chat", ScopeKind.GROUP), project_alias="p", creator_id="user",
                )
                self.assertIsNone(binding.task_feedback.progress_card_settings)
                self.assertEqual(store._connection.execute(
                    "SELECT progress_card_settings_json FROM bindings WHERE binding_id=?", (binding.id,),
                ).fetchone()[0], "null")
                store.assign_native_thread_id(binding.id, "native-source")
                binding = store.get(binding.id)
                fork = store.create_fork_binding(
                    scope=FeishuScope("app", "chat", ScopeKind.TOPIC, "fork-topic"), source=binding,
                    native_thread_id="native-fork", root_message_id="root", creator_id="user",
                    expected_project_revision=project.revision, target_chat_kind="group",
                )
                self.assertIsNone(fork.task_feedback.progress_card_settings)
                rule = store.defaults.save(
                    app_id="app", kind="chat", chat_id="chat", keyword=None, project="p",
                    session_settings=SessionSettings.from_binding(binding), rule_id=None, expected_revision=None,
                )
                plan = store.schedules.create(
                    name="plan", instructions="work", project_alias="p", app_id="app", chat_id="chat",
                    schedule=ScheduleRule("interval", "UTC", every_minutes=1, anchor=100),
                    request_id="create", workspace=WorkspacePolicy(), now=100,
                    session_settings=SessionSettings.from_binding(binding),
                )
                arguments = dict(binding_id=binding.id, expected_settings_revision=1,
                                 expected_context_revision=1, settings=None,
                                 message_context_mode=MentionContextMode.CURRENT_ONLY, context_anchor=None)
                self.assertEqual(store.set_configuration(**arguments, expected_feedback_revision=1,
                                                        task_feedback=binding.task_feedback), binding)
                explicit = dataclasses.replace(binding.task_feedback, progress_card_settings=ProgressCardSettings())
                changed = store.set_configuration(**arguments, expected_feedback_revision=1, task_feedback=explicit)
                self.assertEqual(changed.feedback_revision, 2)
                inherited = store.set_configuration(**arguments, expected_feedback_revision=2,
                                                    task_feedback=BindingTaskFeedback())
                self.assertEqual(inherited.feedback_revision, 3)
                self.assertIsNone(inherited.task_feedback.progress_card_settings)
                explicit_binding = store.create_channel_binding(
                    scope=FeishuScope("app", "another-chat", ScopeKind.GROUP), project_alias="p", creator_id="user",
                    task_feedback=explicit,
                )
            validate_channel_database(path)
            with closing(BindingStore(path)) as reopened:
                for item in (reopened.get(binding.id), reopened.get(fork.id)):
                    self.assertIsNone(item.task_feedback.progress_card_settings)
                self.assertEqual(reopened.get(explicit_binding.id).task_feedback.progress_card_settings, ProgressCardSettings())
                self.assertIsNone(reopened.defaults.get("app", rule.id).session_settings.task_feedback.progress_card_settings)
                self.assertIsNone(reopened.schedules.get(plan.plan_id).session_settings.task_feedback.progress_card_settings)


if __name__ == "__main__":
    unittest.main()
