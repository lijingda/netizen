from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from netizen_cli import database_migrations as migrations
from netizen_cli.bindings import BindingStore, validate_channel_database
from netizen_cli.defaults.models import WorkspacePolicy
from netizen_cli.migrations import v16
from netizen_cli.migrations.schema import require_schema


FIXTURES = Path(__file__).parent / "fixtures"


class SessionDefaultsMigrationTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "channel.sqlite3"
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executescript((FIXTURES / "channel_v15.sql").read_text())

    def contents(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return tuple(connection.iterdump())

    def test_frozen_v15_migrates_workspace_defaults_and_preserves_all_metadata(self):
        with closing(sqlite3.connect(self.path)) as connection:
            tables = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name!='schema_version'",
            )]
            before = {table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
        self.assertEqual(migrations.plan_channel_database(self.path), {
            "source_version": 15, "target_version": 16, "steps": [{"from_version": 15, "to_version": 16}],
        })
        migrations.migrate_channel_database(self.path, expected_source_version=15)
        validate_channel_database(self.path)
        with closing(sqlite3.connect(self.path)) as connection:
            for table, rows in before.items():
                actual = connection.execute(f"SELECT * FROM {table}").fetchall()
                if rows:
                    actual = [row[:len(rows[0])] for row in actual]
                self.assertEqual(actual, rows, table)
            self.assertEqual(
                [json.loads(row[0]) for row in connection.execute("SELECT workspace_json FROM session_defaults")],
                [{"mode": "project"}, {"mode": "project"}],
            )
        with closing(BindingStore(self.path)) as store:
            self.assertEqual(store.defaults.exact("app", "chat").workspace, WorkspacePolicy())
            self.assertEqual(store.worktrees.get("worktree-shared").identity_token, "root:1:2/gitdir:3:4")
            self.assertEqual(store.get("binding").worktree_id, "worktree-shared")
        before_noop = self.contents()
        self.assertEqual(migrations.migrate_channel_database(self.path, expected_source_version=16)["steps"], [])
        self.assertEqual(self.contents(), before_noop)

    def test_fresh_and_upgraded_schema_have_identical_constraints(self):
        migrations.migrate_channel_database(self.path, expected_source_version=15)
        with closing(BindingStore()) as fresh, closing(sqlite3.connect(self.path)) as upgraded:
            upgraded.row_factory = sqlite3.Row
            statements = tuple(row[0] for row in fresh._connection.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
                "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END",
            ))
            require_schema(upgraded, statements)
            self.assertEqual(upgraded.execute("PRAGMA foreign_key_list(session_defaults)").fetchall(), [])

    def test_workspace_migration_failure_rolls_back_data_schema_and_version(self):
        def fail(connection):
            v16.apply(connection)
            connection.execute("UPDATE session_defaults SET revision=99")
            raise RuntimeError("after workspace column")

        step = migrations.Migration(15, 16, fail, v16.validate)
        before = self.contents()
        with patch.object(migrations, "MIGRATIONS", (*migrations.MIGRATIONS[:1], step)):
            with self.assertRaisesRegex(RuntimeError, "after workspace column"):
                migrations.migrate_channel_database(self.path, expected_source_version=15)
        self.assertEqual(self.contents(), before)
        self.assertEqual(migrations.plan_channel_database(self.path)["source_version"], 15)

    def test_malformed_workspace_is_rejected_without_repair_but_deleted_reference_is_valid(self):
        migrations.migrate_channel_database(self.path, expected_source_version=15)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("UPDATE session_defaults SET workspace_json=?", (
                json.dumps({"mode": "existing", "worktree_id": "deleted-resource"}),
            ))
            connection.commit()
        validate_channel_database(self.path)
        self.assertEqual(migrations.plan_channel_database(self.path)["steps"], [])
        invalid = ({"mode": "new", "ref": None}, {"mode": "existing"}, {"mode": "project", "ref": "HEAD"})
        for value in invalid:
            with self.subTest(value=value):
                with closing(sqlite3.connect(self.path)) as connection:
                    connection.execute("UPDATE session_defaults SET workspace_json=?", (json.dumps(value),))
                    connection.commit()
                before = self.contents()
                for validate in (validate_channel_database, migrations.plan_channel_database):
                    with self.assertRaises(RuntimeError):
                        validate(self.path)
                self.assertEqual(self.contents(), before)
