from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from netizen_cli import database_migrations as migrations
from netizen_cli.bindings import BindingStore, validate_channel_database
from netizen_cli.migrations import v21
from netizen_cli.migrations.schema import require_schema


class QueryIndexesMigrationTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "channel.sqlite3"
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executescript((Path(__file__).parent / "fixtures/channel_v20.sql").read_text())

    def contents(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return tuple(connection.iterdump())

    def test_adjacent_upgrade_preserves_every_row_and_matches_fresh_indexes(self):
        with closing(sqlite3.connect(self.path)) as connection:
            tables = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name!='schema_version'",
            )]
            before = {table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
        expected = {"source_version": 20, "target_version": 21,
                    "steps": [{"from_version": 20, "to_version": 21}]}
        original = self.contents()
        self.assertEqual(migrations.plan_channel_database(self.path), expected)
        self.assertEqual(self.contents(), original)
        self.assertEqual(migrations.migrate_channel_database(self.path, expected_source_version=20), expected)
        with closing(sqlite3.connect(self.path)) as upgraded, closing(BindingStore()) as fresh:
            upgraded.row_factory = sqlite3.Row
            for table, rows in before.items():
                self.assertEqual([tuple(row) for row in upgraded.execute(f"SELECT * FROM {table}")], rows, table)
            v21.validate(upgraded)
            v21.validate(fresh._connection)
            require_schema(upgraded, v21.SCHEMA)
            for statement in v21.INDEXES:
                name = statement.split()[2]
                for pragma in ("index_info", "index_xinfo"):
                    self.assertEqual(
                        [tuple(row)[0:1] + tuple(row)[2:] for row in upgraded.execute(f"PRAGMA {pragma}({name})")],
                        [tuple(row)[0:1] + tuple(row)[2:] for row in fresh._connection.execute(f"PRAGMA {pragma}({name})")],
                    )
        validate_channel_database(self.path)
        with closing(BindingStore(self.path, create=False)) as store:
            self.assertTrue(store.schedules.get("plan-topic").auto_archive)
        migrated = self.contents()
        self.assertEqual(migrations.migrate_channel_database(self.path, expected_source_version=21)["steps"], [])
        self.assertEqual(self.contents(), migrated)

    def test_index_failure_rolls_back_all_indexes_and_version(self):
        def fail(connection):
            connection.execute(v21.INDEXES[0])
            connection.execute(v21.INDEXES[1])
            raise RuntimeError("interrupted index creation")

        before = self.contents()
        step = migrations.Migration(20, 21, fail, v21.validate)
        with patch.object(migrations, "MIGRATIONS", (*migrations.MIGRATIONS[:-1], step)):
            with self.assertRaisesRegex(RuntimeError, "interrupted index creation"):
                migrations.migrate_channel_database(self.path, expected_source_version=20)
        self.assertEqual(self.contents(), before)

    def test_missing_or_wrong_required_index_is_rejected_without_repair(self):
        migrations.migrate_channel_database(self.path, expected_source_version=20)
        for statement in v21.INDEXES:
            name = statement.split()[2]
            for replacement in (None, f"CREATE INDEX {name} ON dedup_keys(dedup_key)"):
                with self.subTest(name=name, replacement=replacement):
                    with closing(sqlite3.connect(self.path)) as connection:
                        connection.execute(f"DROP INDEX {name}")
                        if replacement:
                            connection.execute(replacement)
                        connection.commit()
                    before = self.contents()
                    for validate in (validate_channel_database, migrations.plan_channel_database):
                        with self.assertRaisesRegex(RuntimeError, "schema v21 query index is invalid"):
                            validate(self.path)
                    self.assertEqual(self.contents(), before)
                    with closing(sqlite3.connect(self.path)) as connection:
                        connection.execute(f"DROP INDEX IF EXISTS {name}")
                        connection.execute(statement)
                        connection.commit()


if __name__ == "__main__":
    unittest.main()
