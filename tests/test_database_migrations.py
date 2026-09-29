from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from netizen_cli import database_migrations as migrations
from netizen_cli.bindings import BindingStore, validate_channel_database
from netizen_cli.migrations import v14
from netizen_cli.migrations.schema import require_schema


FIXTURES = Path(__file__).parent / "fixtures"
NOTES_V15 = "CREATE TABLE migration_notes (binding_id TEXT PRIMARY KEY REFERENCES bindings(binding_id), note TEXT NOT NULL)"
NOTES_V16 = "CREATE TABLE migration_notes (binding_id TEXT PRIMARY KEY REFERENCES bindings(binding_id), note TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1))"


def apply_15(connection: sqlite3.Connection) -> None:
    connection.execute(NOTES_V15)
    connection.execute("INSERT INTO migration_notes SELECT binding_id, 'migrated' FROM bindings")


def apply_16(connection: sqlite3.Connection) -> None:
    connection.execute("ALTER TABLE migration_notes ADD COLUMN revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1)")
    connection.execute("UPDATE migration_notes SET note = 'converted', revision = 2")


def validate_15(connection: sqlite3.Connection) -> None:
    require_schema(connection, (*v14.SCHEMA, NOTES_V15))


def validate_16(connection: sqlite3.Connection) -> None:
    require_schema(connection, (*v14.SCHEMA, NOTES_V16))


STEP_15 = migrations.Migration(14, 15, apply_15, validate_15)
STEP_16 = migrations.Migration(15, 16, apply_16, validate_16)


@contextmanager
def future_schema(*steps: migrations.Migration, version: int = 16):
    with patch.object(migrations, "MIGRATIONS", steps or (STEP_15, STEP_16)), patch.object(migrations, "SCHEMA_VERSION", version):
        yield


class DatabaseMigrationsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / "channel.sqlite3"
        with sqlite3.connect(self.database) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript((FIXTURES / "channel_v14.sql").read_text())
            connection.executescript((FIXTURES / "channel_v14_data.sql").read_text())
        connection.close()

    def contents(self) -> list[str]:
        connection = sqlite3.connect(self.database)
        try:
            return list(connection.iterdump())
        finally:
            connection.close()

    def files(self) -> dict[str, bytes]:
        return {
            item.name: item.read_bytes() for item in self.database.parent.iterdir()
            if item.name != self.database.name + "-shm"
        }

    def test_current_database_noop_and_plan_do_not_write(self) -> None:
        before = self.files()
        expected = {"source_version": 14, "target_version": 14, "steps": []}
        self.assertEqual(migrations.plan_channel_database(self.database), expected)
        self.assertEqual(migrations.migrate_channel_database(self.database, expected_source_version=14), expected)
        self.assertEqual(self.files(), before)
        validate_channel_database(self.database)

    def test_frozen_baseline_matches_current_runtime_database(self) -> None:
        path = self.database.parent / "fresh.sqlite3"
        store = BindingStore(path)
        store.close()
        self.assertEqual(migrations.plan_channel_database(path)["source_version"], 14)

    def test_supported_cross_version_path_preserves_all_original_metadata(self) -> None:
        before = self.contents()
        with future_schema():
            plan = migrations.plan_channel_database(self.database)
            self.assertEqual(json.loads(json.dumps(plan)), {
                "source_version": 14, "target_version": 16,
                "steps": [{"from_version": 14, "to_version": 15}, {"from_version": 15, "to_version": 16}],
            })
            self.assertEqual(self.contents(), before)
            self.assertEqual(migrations.migrate_channel_database(self.database, expected_source_version=14), plan)
            with sqlite3.connect(self.database) as connection:
                self.assertEqual(connection.execute("SELECT * FROM migration_notes").fetchall(), [("binding", "converted", 2)])
                connection.execute("DROP TABLE migration_notes")
                connection.execute("UPDATE schema_version SET version = 14")
            self.assertEqual(self.contents(), before)

    def test_repeat_execution_and_starting_at_intermediate_version(self) -> None:
        with future_schema(STEP_15, version=15):
            migrations.migrate_channel_database(self.database, expected_source_version=14)
        with future_schema():
            plan = migrations.migrate_channel_database(self.database, expected_source_version=15)
            self.assertEqual(plan["steps"], [{"from_version": 15, "to_version": 16}])
            before = self.files()
            self.assertEqual(migrations.migrate_channel_database(self.database, expected_source_version=16)["steps"], [])
            self.assertEqual(self.files(), before)

    def test_intermediate_source_uses_its_registered_version_validator(self) -> None:
        with future_schema(STEP_15, version=15):
            migrations.migrate_channel_database(self.database, expected_source_version=14)
        with sqlite3.connect(self.database) as connection:
            connection.execute("DROP TABLE migration_notes")
        before = self.files()
        with future_schema():
            with self.assertRaisesRegex(RuntimeError, "schema objects"):
                migrations.plan_channel_database(self.database)
            with self.assertRaisesRegex(RuntimeError, "schema objects"):
                migrations.migrate_channel_database(self.database, expected_source_version=15)
        self.assertEqual(self.files(), before)

    def test_source_version_must_match_preflight(self) -> None:
        before = self.files()
        with self.assertRaisesRegex(RuntimeError, "source version changed"):
            migrations.migrate_channel_database(self.database, expected_source_version=13)
        self.assertEqual(self.files(), before)

    def test_second_step_failure_rolls_back_both_steps_and_version(self) -> None:
        def fail(connection: sqlite3.Connection) -> None:
            apply_16(connection)
            raise RuntimeError("injected migration failure")

        before = self.contents()
        step = migrations.Migration(15, 16, fail, validate_16)
        with future_schema(STEP_15, step), self.assertRaisesRegex(RuntimeError, "injected migration failure"):
            migrations.migrate_channel_database(self.database, expected_source_version=14)
        self.assertEqual(self.contents(), before)
        self.assertEqual(migrations.plan_channel_database(self.database)["source_version"], 14)

    def test_target_structure_validation_failure_rolls_back(self) -> None:
        def incomplete(connection: sqlite3.Connection) -> None:
            connection.execute("UPDATE projects SET revision = revision + 1")

        before = self.contents()
        step = migrations.Migration(14, 15, incomplete, validate_15)
        with future_schema(step, version=15), self.assertRaisesRegex(RuntimeError, "schema objects"):
            migrations.migrate_channel_database(self.database, expected_source_version=14)
        self.assertEqual(self.contents(), before)

    def test_foreign_key_validation_rolls_back_deferred_violation(self) -> None:
        def invalid(connection: sqlite3.Connection) -> None:
            connection.execute("CREATE TABLE invalid_reference (binding_id TEXT REFERENCES bindings(binding_id) DEFERRABLE INITIALLY DEFERRED)")
            connection.execute("INSERT INTO invalid_reference VALUES ('missing')")

        before = self.contents()
        step = migrations.Migration(14, 15, invalid, lambda connection: None)
        with future_schema(step, version=15), self.assertRaisesRegex(RuntimeError, "foreign-key check"):
            migrations.migrate_channel_database(self.database, expected_source_version=14)
        self.assertEqual(self.contents(), before)

    def test_callbacks_cannot_escape_transaction_or_change_version(self) -> None:
        callbacks = (
            lambda connection: connection.commit(),
            lambda connection: connection.rollback(),
            lambda connection: connection.executescript("CREATE TABLE escaped (value TEXT);"),
            lambda connection: connection.execute("SAVEPOINT escaped"),
            lambda connection: connection.execute("PRAGMA foreign_keys = OFF"),
            lambda connection: connection.execute("UPDATE schema_version SET version = 15"),
        )
        before = self.contents()
        for callback in callbacks:
            def apply(connection: sqlite3.Connection) -> None:
                apply_15(connection)
                callback(connection)

            step = migrations.Migration(14, 15, apply, validate_15)
            with self.subTest(callback=callback), future_schema(step, version=15):
                with self.assertRaises(RuntimeError):
                    migrations.migrate_channel_database(self.database, expected_source_version=14)
                self.assertEqual(self.contents(), before)

    def test_target_validator_cannot_commit_or_mutate(self) -> None:
        before = self.contents()
        for callback in (
            lambda connection: connection.commit(),
            lambda connection: connection.execute("UPDATE projects SET revision = 8"),
        ):
            step = migrations.Migration(14, 15, apply_15, callback)
            with self.subTest(callback=callback), future_schema(step, version=15):
                with self.assertRaisesRegex(RuntimeError, "not authorized"):
                    migrations.migrate_channel_database(self.database, expected_source_version=14)
                self.assertEqual(self.contents(), before)

    def test_process_exit_mid_path_recovers_whole_transaction(self) -> None:
        connection = sqlite3.connect(self.database)
        connection.execute("PRAGMA journal_mode = WAL")
        connection.close()
        before = self.contents()
        result = subprocess.run([
            sys.executable, "-c",
            "import os,sys; sys.path.insert(0, 'tests'); "
            "from test_database_migrations import *; "
            "migrations.SCHEMA_VERSION=16; "
            "migrations.MIGRATIONS=(STEP_15, migrations.Migration(15,16,lambda c:os._exit(42),validate_16)); "
            "migrations.migrate_channel_database(sys.argv[1], expected_source_version=14)",
            str(self.database),
        ], capture_output=True, text=True, cwd=Path(__file__).parents[1], timeout=15)
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertEqual(self.contents(), before)
        self.assertEqual(migrations.plan_channel_database(self.database)["source_version"], 14)

    def test_missing_or_ambiguous_path_rejected_without_writes(self) -> None:
        before = self.files()
        for steps in ((STEP_15,), (STEP_16,), (STEP_15, STEP_15, STEP_16)):
            with self.subTest(steps=steps), future_schema(*steps), self.assertRaises(RuntimeError):
                migrations.migrate_channel_database(self.database, expected_source_version=14)
            self.assertEqual(self.files(), before)

    def test_unsupported_versions_and_malformed_version_are_read_only(self) -> None:
        for version in (12, 13, 15, "invalid", None):
            with self.subTest(version=version):
                with sqlite3.connect(self.database) as connection:
                    connection.execute("DELETE FROM schema_version")
                    if version is not None:
                        connection.execute("INSERT INTO schema_version VALUES (?)", (version,))
                before = self.files()
                with self.assertRaises(RuntimeError):
                    migrations.migrate_channel_database(self.database, expected_source_version=14)
                self.assertEqual(self.files(), before)

    def test_wal_rejection_preserves_database_and_wal_bytes(self) -> None:
        connection = sqlite3.connect(self.database, isolation_level=None)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("UPDATE schema_version SET version = 13")
        before = self.files()
        self.assertIn("channel.sqlite3-wal", before)
        with self.assertRaisesRegex(RuntimeError, "automatic upgrades start"):
            migrations.plan_channel_database(self.database)
        self.assertEqual(self.files(), before)

    def test_current_wal_preflight_and_noop_preserve_uncheckpointed_data(self) -> None:
        connection = sqlite3.connect(self.database, isolation_level=None)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("UPDATE projects SET revision = 7")
        before = self.files()
        self.assertIn("channel.sqlite3-wal", before)
        self.assertEqual(migrations.migrate_channel_database(self.database, expected_source_version=14)["steps"], [])
        self.assertEqual(self.files(), before)
        self.assertEqual(connection.execute("SELECT revision FROM projects").fetchone()[0], 7)

    def test_missing_check_constraint_rejected_even_with_valid_rows(self) -> None:
        path = self.database.parent / "missing-check.sqlite3"
        sql = (FIXTURES / "channel_v14.sql").read_text().replace("CHECK(settings_revision >= 1)", "")
        connection = sqlite3.connect(path)
        connection.executescript(sql)
        connection.executescript((FIXTURES / "channel_v14_data.sql").read_text())
        connection.close()
        before = self.files()
        with self.assertRaisesRegex(RuntimeError, "invalid table structure: bindings"):
            migrations.plan_channel_database(path)
        self.assertEqual(self.files(), before)

    def test_corrupt_schema_foreign_keys_and_semantic_data_rejected_read_only(self) -> None:
        mutations = (
            "DROP INDEX schedule_runs_due",
            "DROP TRIGGER bindings_context_shape_insert",
            "ALTER TABLE scopes DROP COLUMN updated_at",
            "UPDATE bindings SET scope_key = 'missing'",
            "UPDATE session_defaults SET session_settings_json = '{}'",
        )
        for index, sql in enumerate(mutations):
            path = self.database.parent / f"corrupt-{index}.sqlite3"
            source = sqlite3.connect(self.database)
            connection = sqlite3.connect(path)
            source.backup(connection)
            source.close()
            connection.execute(sql)
            connection.commit()
            connection.close()
            before = self.files()
            with self.subTest(sql=sql), self.assertRaises(RuntimeError):
                migrations.plan_channel_database(path)
            self.assertEqual(self.files(), before)

    def test_absent_empty_symlink_and_non_database_files_are_rejected(self) -> None:
        absent = self.database.parent / "absent.sqlite3"
        empty = self.database.parent / "empty.sqlite3"
        empty.touch()
        garbage = self.database.parent / "garbage.sqlite3"
        garbage.write_text("not sqlite")
        link = self.database.parent / "link.sqlite3"
        link.symlink_to(self.database)
        before = self.files()
        for path in (absent, empty, garbage, link, self.database.parent):
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                migrations.plan_channel_database(path)
        self.assertEqual(self.files(), before)


if __name__ == "__main__":
    unittest.main()
