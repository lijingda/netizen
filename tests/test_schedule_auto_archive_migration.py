from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from netizen_cli import database_migrations as migrations
from netizen_cli.bindings import BindingStore, validate_channel_database
from netizen_cli.migrations import v20
from netizen_cli.migrations.schema import require_schema


class ScheduleAutoArchiveMigrationTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "channel.sqlite3"
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executescript((Path(__file__).parent / "fixtures/channel_v19.sql").read_text())

    def contents(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return tuple(connection.iterdump())

    def test_upgrade_defaults_all_plans_off_and_preserves_existing_data(self):
        with closing(sqlite3.connect(self.path)) as connection:
            tables = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name!='schema_version'",
            )]
            before = {table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
        expected = {"source_version": 19, "target_version": 20,
                    "steps": [{"from_version": 19, "to_version": 20}]}
        original = self.contents()
        self.assertEqual(migrations.plan_channel_database(self.path), expected)
        self.assertEqual(self.contents(), original)
        self.assertEqual(migrations.migrate_channel_database(self.path, expected_source_version=19), expected)
        with closing(sqlite3.connect(self.path)) as connection:
            for table, rows in before.items():
                actual = connection.execute(f"SELECT * FROM {table}").fetchall()
                if table == "schedule_plans":
                    self.assertTrue(all(row[-1] == 0 for row in actual))
                    actual = [row[:-1] for row in actual]
                self.assertEqual(actual, rows, table)
        validate_channel_database(self.path)
        with closing(BindingStore(self.path)) as store:
            for identifier in ("plan-topic", "plan-binding", "deleted-new_topic", "deleted-binding"):
                self.assertIs(store.schedules.get(identifier, include_deleted=True).auto_archive, False)
        migrated = self.contents()
        self.assertEqual(migrations.migrate_channel_database(self.path, expected_source_version=20)["steps"], [])
        self.assertEqual(self.contents(), migrated)

    def test_fresh_and_migrated_schema_and_policy_persistence_match(self):
        migrations.migrate_channel_database(self.path, expected_source_version=19)
        with closing(BindingStore()) as fresh, closing(sqlite3.connect(self.path)) as upgraded:
            upgraded.row_factory = sqlite3.Row
            statements = tuple(row[0] for row in fresh._connection.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
                "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END",
            ))
            require_schema(upgraded, statements)
        with closing(BindingStore(self.path)) as store:
            plan = store.schedules.get("plan-topic")
            store.schedules.update(plan.id, expected_revision=plan.revision, request_id="enable-auto-archive",
                                   changes={"auto_archive": True})
        validate_channel_database(self.path)
        with closing(BindingStore(self.path)) as store:
            self.assertIs(store.schedules.get("plan-topic").auto_archive, True)
            plan = store.schedules.get("plan-topic")
            store.schedules.delete(plan.id, expected_revision=plan.revision, request_id="delete-auto-archive")
            self.assertIs(store.schedules.get(plan.id, include_deleted=True).auto_archive, False)
        validate_channel_database(self.path)

    def test_failed_target_validation_rolls_back_column_and_version(self):
        def fail(connection):
            v20.validate(connection)
            raise RuntimeError("after auto archive validation")

        before = self.contents()
        step = migrations.Migration(19, 20, v20.apply, fail)
        with patch.object(migrations, "MIGRATIONS", (*migrations.MIGRATIONS[:-1], step)):
            with self.assertRaisesRegex(RuntimeError, "after auto archive validation"):
                migrations.migrate_channel_database(self.path, expected_source_version=19)
        self.assertEqual(self.contents(), before)

    def test_invalid_policy_values_and_wrong_targets_fail_closed(self):
        migrations.migrate_channel_database(self.path, expected_source_version=19)
        for identifier, value in (("plan-topic", 2), ("plan-topic", "true"),
                                  ("plan-binding", 1), ("deleted-new_topic", 1)):
            with self.subTest(identifier=identifier, value=value):
                with closing(sqlite3.connect(self.path)) as connection:
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute("UPDATE schedule_plans SET auto_archive=? WHERE plan_id=?", (value, identifier))
                    connection.execute("PRAGMA ignore_check_constraints=ON")
                    connection.execute("UPDATE schedule_plans SET auto_archive=? WHERE plan_id=?", (value, identifier))
                    connection.commit()
                before = self.contents()
                for validate in (validate_channel_database, migrations.plan_channel_database):
                    with self.assertRaises(RuntimeError):
                        validate(self.path)
                self.assertEqual(self.contents(), before)
                with closing(sqlite3.connect(self.path)) as connection:
                    connection.execute("UPDATE schedule_plans SET auto_archive=0 WHERE plan_id=?", (identifier,))
                    connection.commit()


if __name__ == "__main__":
    unittest.main()
