from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from netizen_cli import database_migrations as migrations
from netizen_cli.bindings import BindingStore, validate_channel_database
from netizen_cli.migrations import v22


class OutcomeMigrationTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "channel.sqlite3"
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript((Path(__file__).parent / "fixtures/channel_v21.sql").read_text())

    def test_v21_upgrade_preserves_all_rows_and_adds_only_null_assessments(self):
        with closing(sqlite3.connect(self.path)) as db:
            before = {name: db.execute(f"SELECT * FROM {name}").fetchall() for (name,) in
                      db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='schema_version'")}
        plan = migrations.migrate_channel_database(self.path, expected_source_version=21)
        self.assertEqual(plan["steps"], [{"from_version": 21, "to_version": 22}])
        with closing(sqlite3.connect(self.path)) as db:
            for table, rows in before.items():
                if table == "schedule_runs":
                    rows = [(*row, None) for row in rows]
                self.assertEqual(db.execute(f"SELECT * FROM {table}").fetchall(), rows)
        with closing(BindingStore(self.path)) as store:
            schedules = store.schedules
            run = schedules.new_topic_run_for_binding("binding")
            self.assertEqual(run.id, "run-topic")
            self.assertIsNone(run.self_reported_outcome)
            for outcome in ("failed", "succeeded"):
                self.assertTrue(schedules.record_self_reported_outcome(run.id, "binding", outcome))
                self.assertEqual(schedules.get_run(run.id).self_reported_outcome, outcome)
            with self.assertRaises(ValueError):
                schedules.record_self_reported_outcome(run.id, "binding", "unreported")
            self.assertFalse(schedules.record_self_reported_outcome(run.id, "other", "failed"))
            self.assertFalse(schedules.record_self_reported_outcome("run-binding", "binding", "failed"))
            self.assertFalse(schedules.record_self_reported_outcome("pruned", "binding", "failed"))
            schedules.set_run(run.id, binding_removed=True)
            self.assertFalse(schedules.record_self_reported_outcome(run.id, "binding", "failed"))
            self.assertEqual(schedules.get_run(run.id).self_reported_outcome, "succeeded")
            query = store._connection.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM schedule_runs WHERE binding_id=? AND target_kind='new_topic'", ("binding",))
            self.assertIn("schedule_runs_binding (", query.fetchone()[3])
        validate_channel_database(self.path)

    def test_constraint_rejects_invalid_outcomes_and_targets(self):
        migrations.migrate_channel_database(self.path, expected_source_version=21)
        with closing(sqlite3.connect(self.path)) as db:
            for run_id, binding_id, outcome in (
                ("run-topic", "binding", "unreported"), ("run-topic", "binding", b"failed"),
                ("run-topic", None, "failed"), ("run-topic", "", "succeeded"),
                ("run-binding", "binding", "succeeded"),
            ):
                with self.subTest(run=run_id, binding=binding_id, outcome=outcome), self.assertRaises(sqlite3.IntegrityError):
                    db.execute("UPDATE schedule_runs SET binding_id=?, self_reported_outcome=? WHERE run_id=?",
                               (binding_id, outcome, run_id))

    def test_corrupt_assessment_is_rejected_at_preflight(self):
        migrations.migrate_channel_database(self.path, expected_source_version=21)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA ignore_check_constraints=ON")
            db.execute("UPDATE schedule_runs SET self_reported_outcome='failed' WHERE target_kind='binding'")
            db.commit()
        for validate in (migrations.plan_channel_database, validate_channel_database):
            with self.assertRaisesRegex(RuntimeError, "scheduled Run self-assessment"):
                validate(self.path)

    def test_validator_rejects_weaker_constraint_and_wrong_nullability(self):
        for column in ("self_reported_outcome TEXT", "self_reported_outcome TEXT NOT NULL DEFAULT ''",
                       v22.COLUMN.replace("AND length(binding_id) > 0", "")):
            with self.subTest(column=column), closing(sqlite3.connect(self.path)) as db:
                db.execute("BEGIN")
                db.execute(f"ALTER TABLE schedule_runs ADD COLUMN {column}")
                db.execute("UPDATE schema_version SET version=22")
                db.row_factory = sqlite3.Row
                with self.assertRaisesRegex(RuntimeError, "invalid table structure: schedule_runs"):
                    v22.validate(db)
                db.rollback()

    def test_failure_after_adding_column_rolls_back_entire_database(self):
        def fail(db):
            v22.validate(db)
            raise RuntimeError("interrupted outcome validation")

        with closing(sqlite3.connect(self.path)) as db:
            before = tuple(db.iterdump())
        step = migrations.Migration(21, 22, v22.apply, fail)
        with patch.object(migrations, "MIGRATIONS", (*migrations.MIGRATIONS[:-1], step)):
            with self.assertRaisesRegex(RuntimeError, "interrupted outcome validation"):
                migrations.migrate_channel_database(self.path, expected_source_version=21)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(tuple(db.iterdump()), before)
