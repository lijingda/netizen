from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from netizen_cli.cli_data import (
    INSTANCE_DATA_MARKER,
    InstanceDataError,
    InstancePurgeError,
    StartupRejected,
    acquire_lifetime_lock,
    begin_instance_setup,
    ensure_instance_root,
    initialize_instance_data,
    instance_lifetime_lock,
    prepare_instance,
    purge_instance_data,
    purge_inventory,
    root_maintenance_lock,
    validate_lifetime_lock,
    validate_prepared_instance,
)
from netizen_cli import database_migrations as migrations
from netizen_cli.bindings import BindingStore
from netizen_cli.instance import INSTANCE_ROOT_MARKER
from tests.test_database_migrations import STEP_15, future_schema, validate_15


class CliDataTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name).resolve()
        self.root = self.parent / "instance"
        ensure_instance_root(self.root)
        self.config = self.root / "config.yaml"
        self.config.write_text(
            f"instance:\n  dataDir: {self.root / 'state'}\n"
            f"  projectRoot: {self.parent / 'projects'}\n"
            "adminWeb:\n  enabled: false\n",
            encoding="utf-8",
        )
        self.config.chmod(0o600)
        self.database = self.root / "state" / "channel.sqlite3"

    def initialize(self) -> None:
        with instance_lifetime_lock(self.root) as descriptor:
            initialize_instance_data(self.root, lifetime_descriptor=descriptor)

    def version(self, path: Path | None = None) -> int:
        with sqlite3.connect(path or self.database) as connection:
            return connection.execute("SELECT version FROM schema_version").fetchone()[0]

    def test_setup_initializes_once_and_start_repeatedly_validates_without_backup(self) -> None:
        self.initialize()
        before = self.database.read_bytes()
        self.initialize()
        for _ in range(2):
            with instance_lifetime_lock(self.root) as descriptor:
                plan = prepare_instance(self.root, lifetime_descriptor=descriptor)
            self.assertEqual(plan, {"source_version": 14, "target_version": 14, "steps": []})
        self.assertEqual(self.database.read_bytes(), before)
        self.assertFalse((self.root / "state" / "migration-backups").exists())

    def test_start_never_initializes_unprepared_root(self) -> None:
        with instance_lifetime_lock(self.root) as descriptor:
            with self.assertRaisesRegex(InstanceDataError, "not initialized"):
                prepare_instance(self.root, lifetime_descriptor=descriptor)
        self.assertFalse(self.database.exists())

    def test_configuration_stage_is_owned_without_creating_a_database(self) -> None:
        with instance_lifetime_lock(self.root) as descriptor:
            self.assertIsNone(begin_instance_setup(self.root, lifetime_descriptor=descriptor))
            self.assertIsNone(begin_instance_setup(self.root, lifetime_descriptor=descriptor))
            self.assertFalse(self.database.exists())
            inventory = purge_inventory(self.root)
            self.assertIn(self.config, inventory)
            purge_instance_data(self.root, expected_inventory=inventory, lifetime_descriptor=descriptor)
        self.assertFalse(self.config.exists())

    def test_configuration_stage_does_not_claim_unknown_or_lost_database(self) -> None:
        self.initialize()
        with instance_lifetime_lock(self.root) as descriptor:
            marker = self.root / INSTANCE_DATA_MARKER
            before = self.database.read_bytes()
            marker.unlink()
            with self.assertRaisesRegex(InstanceDataError, "existing database data"):
                begin_instance_setup(self.root, lifetime_descriptor=descriptor)
            self.assertFalse(marker.exists())
            self.assertEqual(self.database.read_bytes(), before)
            marker.write_bytes(b"netizen-instance-data-v1:initialized\n")
            marker.chmod(0o600)
            self.database.unlink()
            with self.assertRaisesRegex(InstanceDataError, "database is missing"):
                begin_instance_setup(self.root, lifetime_descriptor=descriptor)
            self.assertEqual(marker.read_bytes(), b"netizen-instance-data-v1:initialized\n")

    def test_setup_reentry_and_start_reject_lost_database(self) -> None:
        self.initialize()
        self.database.unlink()
        for action in (initialize_instance_data, prepare_instance):
            with instance_lifetime_lock(self.root) as descriptor:
                with self.assertRaisesRegex(InstanceDataError, "database is missing"):
                    action(self.root, lifetime_descriptor=descriptor)
            self.assertFalse(self.database.exists())

    def test_interrupted_setup_before_database_creation_can_retry_explicitly(self) -> None:
        original_open = os.open
        def fail_database(path, *args, **kwargs):
            if Path(path) == self.database:
                raise PermissionError("synthetic initialization interruption")
            return original_open(path, *args, **kwargs)
        with instance_lifetime_lock(self.root) as descriptor:
            with patch("netizen_cli.cli_data.os.open", side_effect=fail_database):
                with self.assertRaises(PermissionError):
                    initialize_instance_data(self.root, lifetime_descriptor=descriptor)
            self.assertFalse(self.database.exists())
            with self.assertRaises(StartupRejected):
                prepare_instance(self.root, lifetime_descriptor=descriptor)
            initialize_instance_data(self.root, lifetime_descriptor=descriptor)
        self.assertEqual(self.version(), 14)

    def test_setup_sqlite_failure_is_reported_and_partial_database_is_preserved(self) -> None:
        with instance_lifetime_lock(self.root) as descriptor:
            with patch("netizen_cli.cli_data.BindingStore", side_effect=sqlite3.OperationalError("private SQL detail")):
                with self.assertRaisesRegex(InstanceDataError, "database initialization failed") as failure:
                    initialize_instance_data(self.root, lifetime_descriptor=descriptor)
            self.assertNotIn("private SQL detail", str(failure.exception))
            self.assertTrue(self.database.is_file())
            self.assertEqual(self.database.stat().st_mode & 0o777, 0o600)
            before = self.database.read_bytes()
            with self.assertRaises(RuntimeError):
                initialize_instance_data(self.root, lifetime_descriptor=descriptor)
            self.assertEqual(self.database.read_bytes(), before)

    def test_setup_does_not_adopt_a_database_without_initialized_evidence(self) -> None:
        self.initialize()
        (self.root / INSTANCE_DATA_MARKER).unlink()
        before = self.database.read_bytes()
        with instance_lifetime_lock(self.root) as descriptor:
            with self.assertRaisesRegex(InstanceDataError, "existing database data"):
                initialize_instance_data(self.root, lifetime_descriptor=descriptor)
        self.assertEqual(self.database.read_bytes(), before)

    def test_corrupt_or_future_database_preserves_data_and_creates_no_backup(self) -> None:
        self.initialize()
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE schema_version SET version=999")
        before = self.database.read_bytes()
        with instance_lifetime_lock(self.root) as descriptor:
            with self.assertRaisesRegex(RuntimeError, "newer"):
                prepare_instance(self.root, lifetime_descriptor=descriptor)
        self.assertEqual(self.database.read_bytes(), before)
        self.database.write_bytes(b"not a database")
        with instance_lifetime_lock(self.root) as descriptor:
            with self.assertRaisesRegex(RuntimeError, "preflight failed"):
                prepare_instance(self.root, lifetime_descriptor=descriptor)
        self.assertEqual(self.database.read_bytes(), b"not a database")
        self.assertFalse((self.root / "state" / "migration-backups").exists())

    def test_transient_sqlite_codes_and_backup_timeout_are_not_permanent_rejections(self) -> None:
        self.initialize()
        for code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_IOERR):
            original = sqlite3.OperationalError("arbitrary localized message")
            original.sqlite_errorcode = code
            error = RuntimeError("database preflight failed")
            error.__cause__ = original
            with self.subTest(code=code), instance_lifetime_lock(self.root) as descriptor:
                with patch("netizen_cli.cli_data.plan_channel_database", side_effect=error):
                    with self.assertRaises(RuntimeError) as failure:
                        prepare_instance(self.root, lifetime_descriptor=descriptor)
                self.assertIs(failure.exception, error)
                self.assertNotIsInstance(failure.exception, StartupRejected)
        with future_schema(STEP_15, version=15), instance_lifetime_lock(self.root) as descriptor:
            with patch("netizen_cli.cli_data._backup", side_effect=TimeoutError("external writer")):
                with self.assertRaises(TimeoutError):
                    prepare_instance(self.root, lifetime_descriptor=descriptor)

    def test_supported_old_schema_validates_read_only_then_start_migrates_once(self) -> None:
        self.initialize()
        before = self.database.read_bytes()
        with future_schema(STEP_15, version=15):
            self.assertEqual(validate_prepared_instance(self.root)["steps"], [{"from_version": 14, "to_version": 15}])
            self.assertEqual(self.database.read_bytes(), before)
            with instance_lifetime_lock(self.root) as descriptor:
                prepare_instance(self.root, lifetime_descriptor=descriptor)
            self.assertEqual(self.version(), 15)
            backups = list((self.root / "state" / "migration-backups").glob("*/channel.sqlite3"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(self.version(backups[0]), 14)
            with instance_lifetime_lock(self.root) as descriptor:
                prepare_instance(self.root, lifetime_descriptor=descriptor)
            self.assertEqual(list((self.root / "state" / "migration-backups").glob("*/channel.sqlite3")), backups)

    def test_failed_migration_rolls_back_transaction_and_retains_backup(self) -> None:
        self.initialize()
        def fail(connection: sqlite3.Connection) -> None:
            connection.execute("CREATE TABLE partial_change (id INTEGER)")
            raise RuntimeError("synthetic migration failure")
        step = migrations.Migration(14, 15, fail, validate_15)
        with future_schema(step, version=15):
            with instance_lifetime_lock(self.root) as descriptor:
                with self.assertRaisesRegex(RuntimeError, "synthetic migration failure"):
                    prepare_instance(self.root, lifetime_descriptor=descriptor)
        self.assertEqual(self.version(), 14)
        with sqlite3.connect(self.database) as connection:
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='partial_change'").fetchone())
        self.assertEqual(len(list((self.root / "state" / "migration-backups").glob("*/channel.sqlite3"))), 1)

    def test_startup_failure_after_migration_commit_does_not_restore_backup(self) -> None:
        self.initialize()
        with future_schema(STEP_15, version=15):
            with self.assertRaisesRegex(RuntimeError, "runtime failed"):
                with instance_lifetime_lock(self.root) as descriptor:
                    prepare_instance(self.root, lifetime_descriptor=descriptor)
                    raise RuntimeError("runtime failed")
            self.assertEqual(self.version(), 15)

    def test_every_mutation_requires_exact_held_lifetime_lock(self) -> None:
        self.initialize()
        path = self.root / "state" / "service.lifetime.lock"
        unlocked = os.open(path, os.O_RDWR)
        try:
            with self.assertRaisesRegex(InstanceDataError, "not held"):
                prepare_instance(self.root, lifetime_descriptor=unlocked)
            with instance_lifetime_lock(self.root) as descriptor:
                self.assertFalse(os.get_inheritable(descriptor))
                with self.assertRaisesRegex(InstanceDataError, "another process"):
                    validate_lifetime_lock(self.root, unlocked)
                with self.assertRaises(BlockingIOError):
                    acquire_lifetime_lock(self.root)
        finally:
            os.close(unlocked)

    def test_maintenance_and_lifetime_locks_have_distinct_roles(self) -> None:
        with root_maintenance_lock(self.root):
            with self.assertRaises(BlockingIOError):
                with root_maintenance_lock(self.root):
                    self.fail("maintenance lock should be busy")
            with instance_lifetime_lock(self.root):
                pass

    def test_root_creation_preserves_foreign_files_but_refuses_unowned_state(self) -> None:
        for managed in (False, True):
            root = self.parent / str(managed)
            root.mkdir(mode=0o700)
            target = root / "state" if managed else root
            target.mkdir(mode=0o700, exist_ok=True)
            foreign = target / "foreign.txt"
            foreign.write_text("keep")
            if managed:
                with self.assertRaisesRegex(InstanceDataError, "unowned"):
                    ensure_instance_root(root)
                self.assertFalse((root / INSTANCE_ROOT_MARKER).exists())
            else:
                ensure_instance_root(root)
            self.assertEqual(foreign.read_text(), "keep")

    def test_root_does_not_claim_invalid_preconfigured_credentials(self) -> None:
        root = self.parent / "invalid-credentials"
        profile = root / "lark-app" / "config.json"
        profile.parent.mkdir(mode=0o700, parents=True)
        profile.write_text("{}")
        profile.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "apps array"):
            ensure_instance_root(root)
        self.assertFalse((root / INSTANCE_ROOT_MARKER).exists())
        self.assertEqual(profile.read_text(), "{}")

    def test_purge_exact_files_preserves_unknown_projects_codex_and_root(self) -> None:
        self.initialize()
        identity = self.root / "state" / "service.identity.json"
        identity.write_text('{"format":1,"pid":123}')
        identity.chmod(0o600)
        stderr = self.root / "state" / "launchd.stderr.log"
        stderr.write_text("diagnostic")
        stderr.chmod(0o600)
        paths = [self.root / "foreign.txt", self.root / "state" / "notes.txt", self.root / "Projects" / "work.py"]
        for path in paths:
            path.parent.mkdir(mode=0o700, exist_ok=True)
            path.write_text("keep")
        inventory = purge_inventory(self.root)
        self.assertIn(self.database, inventory)
        self.assertIn(identity, inventory)
        self.assertIn(stderr, inventory)
        self.assertNotIn(self.root / INSTANCE_DATA_MARKER, inventory)
        with instance_lifetime_lock(self.root) as descriptor:
            deleted = purge_instance_data(self.root, expected_inventory=inventory, lifetime_descriptor=descriptor)
        self.assertEqual(deleted, inventory)
        self.assertTrue((self.root / INSTANCE_ROOT_MARKER).is_file())
        self.assertTrue((self.root / "state" / "service.lifetime.lock").is_file())
        for path in paths:
            self.assertEqual(path.read_text(), "keep")
        with self.assertRaisesRegex(InstanceDataError, "not initialized"):
            validate_prepared_instance(self.root)
        self.config.write_text(f"instance:\n  projectRoot: {self.parent / 'projects'}\n")
        self.config.chmod(0o600)
        self.initialize()
        self.assertEqual(self.version(), 14)

    def test_purge_rejects_changed_confirmation_inventory(self) -> None:
        self.initialize()
        inventory = purge_inventory(self.root)
        log = self.root / "state" / "netizen.log"
        log.write_text("new log")
        log.chmod(0o600)
        with instance_lifetime_lock(self.root) as descriptor:
            with self.assertRaisesRegex(InstanceDataError, "changed since confirmation"):
                purge_instance_data(self.root, expected_inventory=inventory, lifetime_descriptor=descriptor)
        self.assertTrue(self.database.exists())

    def test_purge_accepts_service_shutdown_removing_confirmed_ready_file(self) -> None:
        self.initialize()
        ready = self.root / "state" / "service.ready"
        ready.write_text("ready")
        ready.chmod(0o600)
        inventory = purge_inventory(self.root)
        ready.unlink()
        with instance_lifetime_lock(self.root) as descriptor:
            removed = purge_instance_data(self.root, expected_inventory=inventory, lifetime_descriptor=descriptor)
        self.assertNotIn(ready, removed)
        self.assertFalse(self.database.exists())

    def test_purge_rejects_symlinks_hardlinks_and_protected_project_overlap(self) -> None:
        self.initialize()
        original = self.config.read_text()
        for protected in (self.root / "state", self.database):
            self.config.write_text(f"instance:\n  projectRoot: {protected}\n")
            with self.assertRaisesRegex(InstanceDataError, "overlaps"):
                purge_inventory(self.root)

        self.config.write_text(original)
        log = self.root / "state" / "netizen.log"
        external = self.parent / "external"
        external.write_text("keep")
        external.chmod(0o600)
        for link in (lambda: log.symlink_to(external), lambda: os.link(external, log)):
            link()
            with self.assertRaisesRegex(InstanceDataError, "unsafe instance file"):
                purge_inventory(self.root)
            log.unlink()
            self.assertEqual(external.read_text(), "keep")
        with patch.dict(os.environ, {"CODEX_HOME": str(self.root / "state")}):
            with self.assertRaisesRegex(InstanceDataError, "overlaps"):
                purge_inventory(self.root)

    def test_purge_protects_bound_environment_codex_home_as_well_as_callers(self) -> None:
        self.initialize()
        with patch.dict(os.environ, {"CODEX_HOME": str(self.parent / "caller-codex")}):
            inventory = purge_inventory(self.root)
            bound_home = self.root / "state"
            with self.assertRaisesRegex(InstanceDataError, "shared Codex state"):
                purge_inventory(self.root, protected_codex_home=bound_home)
            with instance_lifetime_lock(self.root) as descriptor:
                with self.assertRaisesRegex(InstanceDataError, "shared Codex state"):
                    purge_instance_data(self.root, expected_inventory=inventory,
                                        lifetime_descriptor=descriptor, protected_codex_home=bound_home)
            self.assertTrue(self.database.is_file())
        with patch.dict(os.environ, {"CODEX_HOME": str(self.root / "state")}):
            with self.assertRaisesRegex(InstanceDataError, "shared Codex state"):
                purge_inventory(self.root, protected_codex_home=self.parent / "bound-codex")

    def test_purge_protects_projects_registered_in_database_but_not_yaml(self) -> None:
        self.initialize()
        store = BindingStore(self.database)
        store.register_project(alias="admin-registered", cwd=str(self.root / "state"))
        store.close()
        with self.assertRaisesRegex(InstanceDataError, "overlaps Project"):
            purge_inventory(self.root)
        self.assertTrue(self.database.exists())

    def test_partial_purge_reports_deletions_and_refuses_retry_without_project_evidence(self) -> None:
        self.initialize()
        inventory = purge_inventory(self.root)
        original_unlink = Path.unlink
        def fail_config(path: Path, *args, **kwargs) -> None:
            if path == self.config:
                raise OSError("synthetic deletion failure")
            original_unlink(path, *args, **kwargs)
        with instance_lifetime_lock(self.root) as descriptor:
            with patch.object(Path, "unlink", fail_config):
                with self.assertRaises(InstancePurgeError) as failure:
                    purge_instance_data(self.root, expected_inventory=inventory, lifetime_descriptor=descriptor)
            self.assertEqual(failure.exception.deleted, tuple(path for path in inventory if path != self.config))
            self.assertTrue(self.config.exists())
            with self.assertRaisesRegex(InstanceDataError, "purge is incomplete"):
                initialize_instance_data(self.root, lifetime_descriptor=descriptor)
            with self.assertRaisesRegex(InstanceDataError, "persisted Project paths without the database"):
                purge_inventory(self.root)
        self.assertTrue(self.config.exists())

    def test_purge_recognized_migration_backups_preserves_foreign_backup_files(self) -> None:
        self.initialize()
        with future_schema(STEP_15, version=15):
            with instance_lifetime_lock(self.root) as descriptor:
                prepare_instance(self.root, lifetime_descriptor=descriptor)
        backup = next((self.root / "state" / "migration-backups").glob("*/channel.sqlite3"))
        foreign = backup.parent / "notes.txt"
        foreign.write_text("keep")
        with future_schema(STEP_15, version=15):
            inventory = purge_inventory(self.root)
            self.assertIn(backup, inventory)
            with instance_lifetime_lock(self.root) as descriptor:
                purge_instance_data(self.root, expected_inventory=inventory, lifetime_descriptor=descriptor)
        self.assertEqual(foreign.read_text(), "keep")

    def test_unfinished_legacy_recovery_blocks_setup_and_start(self) -> None:
        self.initialize()
        (self.root / "state" / ".activation-intent.json").write_text("{}")
        with instance_lifetime_lock(self.root) as descriptor:
            for operation in (initialize_instance_data, prepare_instance):
                with self.assertRaisesRegex(InstanceDataError, "unfinished legacy"):
                    operation(self.root, lifetime_descriptor=descriptor)


if __name__ == "__main__":
    unittest.main()
