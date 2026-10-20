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
from netizen_cli.migrations import v17
from netizen_cli.migrations.schema import require_schema
from netizen_cli.worktrees.policy import WorkspacePolicy


FIXTURES = Path(__file__).parent / "fixtures"


class ScheduleWorkspaceMigrationTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "channel.sqlite3"
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executescript((FIXTURES / "channel_v16.sql").read_text())

    def contents(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return tuple(connection.iterdump())

    def test_v16_migration_initializes_only_live_new_topic_policies_and_preserves_metadata(self):
        with closing(sqlite3.connect(self.path)) as connection:
            tables = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name!='schema_version'",
            )]
            before = {table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
        plan = migrations.plan_channel_database(self.path)
        self.assertEqual(plan, {
            "source_version": 16, "target_version": 17, "steps": [{"from_version": 16, "to_version": 17}],
        })
        self.assertEqual(migrations.migrate_channel_database(self.path, expected_source_version=16), plan)
        with closing(sqlite3.connect(self.path)) as connection:
            for table, rows in before.items():
                actual = connection.execute(f"SELECT * FROM {table}").fetchall()
                if rows:
                    actual = [row[:len(rows[0])] for row in actual]
                self.assertEqual(actual, rows, table)
            self.assertEqual(connection.execute(
                "SELECT plan_id,workspace_json FROM schedule_plans ORDER BY plan_id",
            ).fetchall(), [
                ("deleted-binding", None), ("deleted-new_topic", None),
                ("plan-binding", None), ("plan-topic", '{"mode":"project"}'),
            ])
            self.assertEqual(connection.execute(
                "SELECT DISTINCT worktree_id FROM schedule_runs",
            ).fetchall(), [(None,)])
        validate_channel_database(self.path)
        with closing(BindingStore(self.path)) as store:
            self.assertEqual(store.schedules.get("plan-topic").workspace, WorkspacePolicy())
            for identifier in ("plan-binding", "deleted-binding", "deleted-new_topic"):
                self.assertIsNone(store.schedules.get(identifier, include_deleted=True).workspace)
            self.assertEqual(store.defaults.get("app", "default-chat").workspace,
                             WorkspacePolicy("existing", worktree_id="worktree-shared"))
            self.assertEqual(store.defaults.get("app", "default-group").workspace,
                             WorkspacePolicy("new", ref="refs/heads/main"))
        before_noop = self.contents()
        self.assertEqual(migrations.migrate_channel_database(self.path, expected_source_version=17)["steps"], [])
        self.assertEqual(self.contents(), before_noop)

    def test_fresh_and_migrated_databases_have_equivalent_constraints_without_worktree_fk(self):
        migrations.migrate_channel_database(self.path, expected_source_version=16)
        with closing(BindingStore()) as fresh, closing(sqlite3.connect(self.path)) as upgraded:
            upgraded.row_factory = sqlite3.Row
            statements = tuple(row[0] for row in fresh._connection.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
                "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END",
            ))
            require_schema(upgraded, statements)
            self.assertEqual(upgraded.execute("PRAGMA foreign_key_list(schedule_plans)").fetchall(), [])
            self.assertEqual(
                [row["table"] for row in upgraded.execute("PRAGMA foreign_key_list(schedule_runs)")],
                ["schedule_plans"],
            )

    def test_failed_step_rolls_back_columns_conversion_and_version(self):
        def fail(connection):
            v17.apply(connection)
            connection.execute("UPDATE schedule_plans SET revision=99")
            raise RuntimeError("after scheduled workspace conversion")

        step = migrations.Migration(16, 17, fail, v17.validate)
        before = self.contents()
        with patch.object(migrations, "MIGRATIONS", (*migrations.MIGRATIONS[:2], step)):
            with self.assertRaisesRegex(RuntimeError, "after scheduled workspace conversion"):
                migrations.migrate_channel_database(self.path, expected_source_version=16)
        self.assertEqual(self.contents(), before)
        self.assertEqual(migrations.plan_channel_database(self.path)["source_version"], 16)

    def test_invalid_policy_shapes_are_rejected_but_deleted_resource_references_remain_valid(self):
        migrations.migrate_channel_database(self.path, expected_source_version=16)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("UPDATE schedule_plans SET workspace_json=? WHERE plan_id='plan-topic'",
                               (json.dumps({"mode": "existing", "worktree_id": "deleted-worktree"}),))
            connection.execute("UPDATE schedule_runs SET worktree_id='deleted-worktree' WHERE target_kind='new_topic'")
            connection.commit()
        validate_channel_database(self.path)
        self.assertEqual(migrations.plan_channel_database(self.path)["steps"], [])
        for identifier, value in (("plan-topic", None), ("plan-topic", {}),
                                  ("plan-topic", {"mode": "new", "ref": None}),
                                  ("plan-binding", {"mode": "project"}),
                                  ("deleted-new_topic", {"mode": "project"})):
            with self.subTest(identifier=identifier, value=value):
                with closing(sqlite3.connect(self.path)) as connection:
                    original = connection.execute(
                        "SELECT workspace_json FROM schedule_plans WHERE plan_id=?", (identifier,),
                    ).fetchone()[0]
                    connection.execute("UPDATE schedule_plans SET workspace_json=? WHERE plan_id=?",
                                       (json.dumps(value) if value is not None else None, identifier))
                    connection.commit()
                before = self.contents()
                for validate in (validate_channel_database, migrations.plan_channel_database):
                    with self.assertRaises(RuntimeError):
                        validate(self.path)
                self.assertEqual(self.contents(), before)
                with closing(sqlite3.connect(self.path)) as connection:
                    connection.execute("UPDATE schedule_plans SET workspace_json=? WHERE plan_id=?", (original, identifier))
                    connection.commit()
