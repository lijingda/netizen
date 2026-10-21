from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from netizen_cli.bindings import BindingStore, _COMPATIBLE_INDEXES
from netizen_cli.database_migrations import plan_channel_database, migrate_channel_database
from netizen_cli.migrations.schema import require_schema


FIXTURES = Path(__file__).parent / "fixtures"


class MigrationSchemaValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "channel.sqlite3"

    def create_v14(self, *, conflict_policy: str = "") -> None:
        sql = (FIXTURES / "channel_v14.sql").read_text()
        sql = sql.replace("native_thread_id TEXT UNIQUE,", f"native_thread_id TEXT UNIQUE{conflict_policy},")
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executescript(sql)
            connection.executescript((FIXTURES / "channel_v14_data.sql").read_text())

    def test_frozen_v14_schema_and_data_are_accepted(self) -> None:
        self.create_v14()
        before = self.database.read_bytes()
        self.assertEqual(plan_channel_database(self.database)["source_version"], 14)
        self.assertEqual(self.database.read_bytes(), before)

    def test_compatible_updated_indexes_preserve_v14_data_and_match_new_store(self) -> None:
        self.create_v14()
        migrate_channel_database(self.database, expected_source_version=14)

        def saved_data(connection):
            return tuple(line for line in connection.iterdump() if line.startswith("INSERT INTO"))

        def index_shapes(connection):
            return {
                name: tuple((row[2], row[3]) for row in connection.execute(f"PRAGMA index_xinfo({name})") if row[5])
                for name in ("side_topics_global_updated", "side_topics_state_updated")
            }

        with closing(sqlite3.connect(self.database)) as connection:
            before = saved_data(connection)
            self.assertEqual(index_shapes(connection), {
                "side_topics_global_updated": (), "side_topics_state_updated": (),
            })
        for _ in range(2):
            with closing(BindingStore(self.database)) as store, closing(BindingStore()) as fresh:
                self.assertEqual(saved_data(store._connection), before)
                self.assertEqual(index_shapes(store._connection), index_shapes(fresh._connection))
                self.assertEqual(index_shapes(store._connection), {
                    "side_topics_global_updated": (("updated_at", 1), ("side_id", 1)),
                    "side_topics_state_updated": (("state", 0), ("updated_at", 1), ("side_id", 1)),
                })
            self.assertEqual(plan_channel_database(self.database), {
                "source_version": 19, "target_version": 19, "steps": [],
            })

    def test_failed_compatible_index_creation_rolls_back_without_changing_data(self) -> None:
        self.create_v14()
        migrate_channel_database(self.database, expected_source_version=14)
        with closing(sqlite3.connect(self.database)) as connection:
            before = tuple(line for line in connection.iterdump() if line.startswith("INSERT INTO"))
        with patch("netizen_cli.bindings._COMPATIBLE_INDEXES", (*_COMPATIBLE_INDEXES, "INVALID SQL")):
            with self.assertRaises(sqlite3.OperationalError):
                BindingStore(self.database)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                tuple(line for line in connection.iterdump() if line.startswith("INSERT INTO")), before,
            )
            self.assertEqual(connection.execute(
                "SELECT name FROM sqlite_master WHERE name IN ('side_topics_global_updated', 'side_topics_state_updated')",
            ).fetchall(), [])
        self.assertEqual(plan_channel_database(self.database)["steps"], [])

    def test_null_defaults_order_identity_is_rejected_without_writes(self) -> None:
        self.create_v14()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("INSERT INTO session_defaults_order VALUES(NULL, 1)")
            connection.commit()
        before = self.database.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "defaults order revision is invalid"):
            plan_channel_database(self.database)
        self.assertEqual(self.database.read_bytes(), before)

    def test_replace_conflict_policy_is_rejected_without_writes(self) -> None:
        self.create_v14(conflict_policy=" ON CONFLICT REPLACE")
        before = self.database.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "unsupported conflict policy: bindings"):
            plan_channel_database(self.database)
        self.assertEqual(self.database.read_bytes(), before)

    def test_explicit_constraint_policies_are_rejected_including_comments(self) -> None:
        expected = ("CREATE TABLE example (id TEXT PRIMARY KEY, value TEXT NOT NULL UNIQUE)",)
        definitions = (
            "id TEXT PRIMARY KEY ON CONFLICT REPLACE, value TEXT NOT NULL UNIQUE",
            "id TEXT PRIMARY KEY, value TEXT NOT NULL ON CONFLICT IGNORE UNIQUE",
            "id TEXT PRIMARY KEY, value TEXT NOT NULL UNIQUE ON /* policy */ CONFLICT -- policy\n REPLACE",
            "id TEXT PRIMARY KEY, value TEXT NOT NULL UNIQUE ON CONFLICT ABORT",
            "id TEXT PRIMARY KEY, value TEXT NOT NULL, UNIQUE(value) ON CONFLICT REPLACE",
        )
        for definition in definitions:
            with self.subTest(definition=definition), closing(sqlite3.connect(":memory:")) as connection:
                connection.execute(f"CREATE TABLE example ({definition})")
                with self.assertRaisesRegex(RuntimeError, "unsupported conflict policy"):
                    require_schema(connection, expected)

    def test_reordered_columns_keep_equivalent_shape(self) -> None:
        expected = (
            "CREATE TABLE example (id TEXT PRIMARY KEY, value TEXT NOT NULL UNIQUE, revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1))",
        )
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute(
                "CREATE TABLE example (revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1), value TEXT NOT NULL UNIQUE, id TEXT PRIMARY KEY)"
            )
            connection.execute("INSERT INTO example(id, value) VALUES ('identity', 'value')")
            require_schema(connection, expected)

    def test_added_column_matches_full_schema(self) -> None:
        expected = (
            "CREATE TABLE example (id TEXT PRIMARY KEY, value TEXT NOT NULL UNIQUE, revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1))",
        )
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute("CREATE TABLE example (id TEXT PRIMARY KEY, value TEXT NOT NULL UNIQUE)")
            connection.execute("INSERT INTO example(id, value) VALUES ('identity', 'value')")
            connection.execute("ALTER TABLE example ADD COLUMN revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1)")
            require_schema(connection, expected)
            self.assertEqual(connection.execute("SELECT revision FROM example").fetchall(), [(1,)])

    def test_conflict_words_in_literals_are_not_constraint_policies(self) -> None:
        expected = ("CREATE TABLE example (value TEXT CHECK(value != 'ON CONFLICT REPLACE'))",)
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute(expected[0])
            require_schema(connection, expected)


if __name__ == "__main__":
    unittest.main()
