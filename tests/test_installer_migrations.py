from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import netizen_installer as installer
from support.installer_migrations import (
    CANDIDATE, OLD, OTHER, PREVIOUS, Scenario, activate, activation_environment,
)


ROOT = Path(__file__).resolve().parents[1]


class InstallerMigrationsTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def _assert_original_release(self, scenario: Scenario, original: bytes) -> None:
        self.assertEqual(scenario.database.read_bytes(), original)
        self.assertEqual(installer._read_release_link(scenario.layout.current, scenario.layout).name, OLD)
        self.assertEqual(installer._read_release_link(scenario.layout.previous, scenario.layout).name, PREVIOUS)
        self.assertEqual(scenario.layout.service_file.read_bytes(), b"old service definition\n")
        self.assertIsNone(installer._read_activation_intent(scenario.layout))

    def _assert_migrated(self, scenario: Scenario, *, target: int = 16) -> None:
        self.assertEqual(scenario.rows("SELECT version FROM schema_version"), [(target,)])
        self.assertEqual(scenario.rows("SELECT cwd FROM projects"), [("/workspace/migrated",)])
        self.assertEqual(scenario.rows("SELECT native_thread_id FROM bindings"), [("native-thread",)])
        self.assertEqual(
            scenario.rows("SELECT run_id, initial_turn_id, disposition FROM schedule_runs ORDER BY run_id"),
            [("run-binding", "turn-2", "steered"), ("run-topic", "turn-1", None)],
        )
        self.assertEqual(scenario.rows("SELECT COUNT(*) FROM session_defaults"), [(2,)])
        names = scenario.rows("SELECT name FROM schedule_plans ORDER BY plan_id")
        suffix = " (migrated)" if target == 16 else ""
        self.assertEqual(names, [(f"Binding plan{suffix}",), (f"Topic plan{suffix}",)])

    def _crash(self, scenario: Scenario, point: str) -> None:
        environment = os.environ | {"PYTHONPATH": str(ROOT / "tests") + os.pathsep + str(ROOT)}
        process = subprocess.run(
            [sys.executable, "-m", "support.installer_migrations", str(scenario.root), point],
            cwd=ROOT, env=environment, text=True, capture_output=True, timeout=30,
        )
        self.assertEqual(process.returncode, -signal.SIGKILL, process.stdout + process.stderr)

    def test_stopped_upgrade_migrates_one_or_multiple_versions_and_preserves_stopped_state(self) -> None:
        for target in (15, 16):
            with self.subTest(target=target):
                scenario = Scenario.create(self.root / str(target))
                with activation_environment(scenario, target=target) as backend:
                    activate(scenario)
                    self._assert_migrated(scenario, target=target)
                    first_database = scenario.database.read_bytes()
                    activate(scenario)
                self.assertEqual(scenario.database.read_bytes(), first_database)
                self.assertEqual(backend.inspect_state(), installer.ServiceState(False, False))
                self.assertFalse(any(event.startswith("start:") for event in backend.events))
                self.assertEqual(installer._read_release_link(scenario.layout.current, scenario.layout).name, CANDIDATE)
                self.assertEqual(installer._read_release_link(scenario.layout.previous, scenario.layout).name, OLD)
                self.assertIsNone(installer._read_activation_intent(scenario.layout))

    def test_preflight_rejects_newer_schema_and_missing_path_without_stopping_service(self) -> None:
        for problem in ("newer", "missing"):
            with self.subTest(problem=problem):
                scenario = Scenario.create(self.root / problem, running=True)
                if problem == "newer":
                    with sqlite3.connect(scenario.database) as connection:
                        connection.execute("UPDATE schema_version SET version = 17")
                original = scenario.database.read_bytes()
                with activation_environment(scenario, missing_path=problem == "missing") as backend:
                    with self.assertRaisesRegex(installer.InstallError, "newer|missing.*migration"):
                        activate(scenario)
                self.assertEqual(backend.events, [])
                self.assertEqual(backend.inspect_state(), installer.ServiceState(True, True))
                self._assert_original_release(scenario, original)

    def test_failures_before_admission_restore_database_release_and_service(self) -> None:
        for point in ("migration_apply", "migration_committed", "current_published", "publish", "before_admission"):
            with self.subTest(point=point):
                scenario = Scenario.create(self.root / point, running=True)
                original = scenario.database.read_bytes()
                with activation_environment(scenario, point=point) as backend:
                    with self.assertRaisesRegex(installer.InstallError, "was rolled back"):
                        activate(scenario)
                self._assert_original_release(scenario, original)
                self.assertEqual(backend.inspect_state(), installer.ServiceState(True, True))
                self.assertEqual(backend.events[-1], f"start:{OLD}")

    def test_failure_after_admission_retains_data_and_exact_retry_finishes_forward(self) -> None:
        scenario = Scenario.create(self.root, running=True)
        original = scenario.database.read_bytes()
        with activation_environment(scenario, point="after_admission"):
            with self.assertRaisesRegex(installer.InstallError, "may have accepted input"):
                activate(scenario)
        recovery = scenario.recovery()
        original_snapshot = recovery.root / "database/channel.sqlite3"
        self.assertEqual(original_snapshot.read_bytes(), original)
        self.assertTrue(recovery.admission_observed())
        self._assert_migrated(scenario)
        self.assertEqual(scenario.rows("SELECT dedup_key FROM dedup_keys WHERE dedup_key='accepted-message'"), [("accepted-message",)])
        with activation_environment(scenario, point="before_admission"):
            with self.assertRaisesRegex(installer.InstallError, "requires recovery"):
                activate(scenario)
        self.assertEqual(scenario.recovery().id, recovery.id)
        self.assertEqual(original_snapshot.read_bytes(), original)
        with activation_environment(scenario) as backend:
            activate(scenario)
        self._assert_migrated(scenario)
        self.assertEqual(scenario.rows("SELECT COUNT(*) FROM dedup_keys WHERE dedup_key='accepted-message'"), [(1,)])
        self.assertEqual(backend.inspect_state(), installer.ServiceState(True, True))
        self.assertEqual(installer._read_release_link(scenario.layout.current, scenario.layout).name, CANDIDATE)
        self.assertEqual(installer._read_release_link(scenario.layout.previous, scenario.layout).name, OLD)
        self.assertIsNone(installer._read_activation_intent(scenario.layout))

    def test_nonexact_retry_rejects_without_stopping_admitted_candidate(self) -> None:
        scenario = Scenario.create(self.root, running=True)
        self._crash(scenario, "ready")
        recovery = scenario.recovery()
        before = scenario.database.read_bytes()
        with activation_environment(scenario) as backend:
            with self.assertRaisesRegex(installer.InstallError, "exact release"):
                activate(scenario, digest=OTHER)
        self.assertEqual(backend.events, [])
        self.assertEqual(backend.inspect_state(), installer.ServiceState(True, True))
        self.assertEqual(scenario.database.read_bytes(), before)
        self.assertEqual(scenario.recovery().id, recovery.id)
        self.assertEqual(installer._read_release_link(scenario.layout.current, scenario.layout).name, CANDIDATE)

    def test_exact_retry_rejects_changed_current_without_restoring_old_database(self) -> None:
        scenario = Scenario.create(self.root, running=True)
        self._crash(scenario, "ready")
        recovery = scenario.recovery()
        before = scenario.database.read_bytes()
        installer._set_release_link(scenario.layout.current, scenario.layout.releases / OLD, scenario.layout)
        with activation_environment(scenario) as backend:
            with self.assertRaisesRegex(installer.InstallError, "candidate release changed"):
                activate(scenario)
        self.assertNotIn("restore", backend.events)
        self.assertFalse(any(event.startswith("start:") for event in backend.events))
        self.assertEqual(scenario.database.read_bytes(), before)
        self.assertEqual(scenario.recovery().id, recovery.id)
        self.assertEqual(installer._read_release_link(scenario.layout.current, scenario.layout).name, OLD)

    def test_retry_after_old_service_restart_preserves_its_new_data(self) -> None:
        scenario = Scenario.create(self.root, running=True)
        original = scenario.database.read_bytes()
        with activation_environment(scenario, point="publish") as backend:
            start = backend.start_and_wait

            def start_old_and_lose_response(*, timeout: float) -> None:
                start(timeout=timeout)
                with sqlite3.connect(scenario.database) as connection:
                    connection.execute("INSERT INTO dedup_keys VALUES ('old-service-new-input', 2200000000)")
                raise installer.InstallError("old service started but response was lost")

            with patch.object(backend, "start_and_wait", side_effect=start_old_and_lose_response):
                with self.assertRaisesRegex(installer.InstallError, "rollback incomplete"):
                    activate(scenario)
        recovery = scenario.recovery()
        self.assertEqual(recovery.payload["phase"], "restoring_service")
        self.assertEqual(scenario.rows("SELECT version FROM schema_version"), [(14,)])
        self.assertEqual((recovery.root / "database/channel.sqlite3").read_bytes(), original)
        with activation_environment(scenario) as backend:
            activate(scenario)
        self.assertNotIn("restore", backend.events)
        self._assert_migrated(scenario)
        self.assertEqual(scenario.rows("SELECT COUNT(*) FROM dedup_keys WHERE dedup_key='old-service-new-input'"), [(1,)])
        self.assertIsNone(installer._read_activation_intent(scenario.layout))

    def test_sigkill_before_admission_recovers_original_snapshot_then_migrates_once(self) -> None:
        for point in ("migration_apply", "migration_committed", "current_published"):
            with self.subTest(point=point):
                scenario = Scenario.create(self.root / point, running=True)
                original = scenario.database.read_bytes()
                self._crash(scenario, point)
                recovery = scenario.recovery()
                self.assertFalse(recovery.admission_observed())
                self.assertEqual((recovery.root / "database/channel.sqlite3").read_bytes(), original)
                with activation_environment(scenario) as backend:
                    activate(scenario)
                self._assert_migrated(scenario)
                self.assertIn("restore", backend.events)
                self.assertEqual(backend.inspect_state(), installer.ServiceState(True, True))
                self.assertEqual(installer._read_release_link(scenario.layout.previous, scenario.layout).name, OLD)
                self.assertIsNone(installer._read_activation_intent(scenario.layout))

    def _restart_old_service_and_write(self, scenario: Scenario, key: str) -> None:
        self.assertEqual(installer._read_release_link(scenario.layout.current, scenario.layout).name, OLD)
        self.assertEqual(scenario.rows("SELECT version FROM schema_version"), [(14,)])
        with activation_environment(scenario) as backend:
            # The v14 baseline service has no admission hook. A reboot can
            # restart it after SQLite has rolled back the killed migration.
            backend.start_and_wait(timeout=1)
        with sqlite3.connect(scenario.database) as connection:
            connection.execute("INSERT INTO dedup_keys VALUES (?, 2200000000)", (key,))
        self.assertFalse(scenario.recovery().admission_observed())

    def test_old_service_input_after_migration_sigkill_survives_upgrade_retry(self) -> None:
        scenario = Scenario.create(self.root, running=True)
        original = scenario.database.read_bytes()
        self._crash(scenario, "migration_apply")
        recovery = scenario.recovery()
        self.assertEqual(recovery.payload["phase"], "snapshot")
        self._restart_old_service_and_write(scenario, "old-service-after-reboot")
        self.assertEqual((recovery.root / "database/channel.sqlite3").read_bytes(), original)
        with activation_environment(scenario):
            activate(scenario)
        self._assert_migrated(scenario)
        self.assertEqual(
            scenario.rows("SELECT COUNT(*) FROM dedup_keys WHERE dedup_key='old-service-after-reboot'"),
            [(1,)],
        )
        self.assertIsNone(installer._read_activation_intent(scenario.layout))

    def test_sigkill_during_source_preservation_never_reverts_to_early_snapshot(self) -> None:
        scenario = Scenario.create(self.root, running=True)
        original = scenario.database.read_bytes()
        self._crash(scenario, "migration_apply")
        recovery = scenario.recovery()
        self._restart_old_service_and_write(scenario, "old-service-after-reboot")
        self._crash(scenario, "restoring_source")
        resumed = scenario.recovery()
        self.assertEqual(resumed.id, recovery.id)
        self.assertEqual(resumed.payload["phase"], "restoring_source")
        self.assertEqual((resumed.root / "database/channel.sqlite3").read_bytes(), original)
        self._restart_old_service_and_write(scenario, "old-service-after-second-reboot")
        with activation_environment(scenario):
            activate(scenario)
        self._assert_migrated(scenario)
        self.assertEqual(
            scenario.rows("SELECT dedup_key FROM dedup_keys WHERE dedup_key LIKE 'old-service-%' ORDER BY dedup_key"),
            [("old-service-after-reboot",), ("old-service-after-second-reboot",)],
        )
        self.assertIsNone(installer._read_activation_intent(scenario.layout))

    def test_old_service_input_after_interrupted_rollback_survives_upgrade_retry(self) -> None:
        scenario = Scenario.create(self.root, running=True)
        original = scenario.database.read_bytes()
        self._crash(scenario, "rollback_current_published")
        recovery = scenario.recovery()
        self.assertEqual(recovery.payload["phase"], "database_restored")
        self.assertEqual(scenario.database.read_bytes(), original)
        self._restart_old_service_and_write(scenario, "old-service-during-restoring")
        self.assertEqual((recovery.root / "database/channel.sqlite3").read_bytes(), original)
        with activation_environment(scenario):
            activate(scenario)
        self._assert_migrated(scenario)
        self.assertEqual(
            scenario.rows("SELECT COUNT(*) FROM dedup_keys WHERE dedup_key='old-service-during-restoring'"),
            [(1,)],
        )
        self.assertIsNone(installer._read_activation_intent(scenario.layout))

    def test_sigkill_during_wal_restore_keeps_candidate_guard_and_recopies_complete_snapshot(self) -> None:
        scenario = Scenario.create(self.root, running=True)
        # Abrupt writer exit leaves a committed WAL record absent from the
        # checkpointed main file, just as a stopped process can after a crash.
        write_wal = (
            "import os, sqlite3, sys\n"
            "connection = sqlite3.connect(sys.argv[1])\n"
            "connection.execute('PRAGMA journal_mode = WAL')\n"
            "connection.execute('PRAGMA wal_autocheckpoint = 0')\n"
            "connection.execute(\"INSERT INTO dedup_keys VALUES ('committed-in-wal', 2300000000)\")\n"
            "connection.commit()\n"
            "os._exit(0)\n"
        )
        writer = subprocess.run(
            [sys.executable, "-c", write_wal, str(scenario.database)],
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(writer.returncode, 0, writer.stderr)
        self.assertGreater(scenario.database.with_name("channel.sqlite3-wal").stat().st_size, 0)

        self._crash(scenario, "rollback_main_restored")
        recovery = scenario.recovery()
        self.assertEqual(recovery.payload["phase"], "restoring")
        self.assertIn("channel.sqlite3-wal", recovery.payload["database_files"])
        self.assertEqual(installer._read_release_link(scenario.layout.current, scenario.layout).name, CANDIDATE)
        self.assertFalse(scenario.database.with_name("channel.sqlite3-wal").exists())
        self.assertEqual(scenario.rows("SELECT version FROM schema_version"), [(14,)])
        self.assertEqual(scenario.rows("PRAGMA integrity_check"), [("ok",)])
        self.assertEqual(scenario.rows("SELECT COUNT(*) FROM dedup_keys WHERE dedup_key='committed-in-wal'"), [(0,)])

        with activation_environment(scenario):
            activate(scenario)
        self._assert_migrated(scenario)
        self.assertEqual(
            scenario.rows("SELECT COUNT(*) FROM dedup_keys WHERE dedup_key='committed-in-wal'"),
            [(1,)],
        )
        self.assertIsNone(installer._read_activation_intent(scenario.layout))

    def test_sigkill_after_admission_or_ready_never_restores_accepted_data(self) -> None:
        for point in ("after_admission", "ready"):
            with self.subTest(point=point):
                scenario = Scenario.create(self.root / point, running=True)
                self._crash(scenario, point)
                self.assertTrue(scenario.recovery().admission_observed())
                with activation_environment(scenario) as backend:
                    activate(scenario)
                self._assert_migrated(scenario)
                self.assertNotIn("restore", backend.events)
                self.assertEqual(scenario.rows("SELECT COUNT(*) FROM dedup_keys WHERE dedup_key='accepted-message'"), [(1,)])
                self.assertIsNone(installer._read_activation_intent(scenario.layout))

    def test_damaged_recovery_snapshot_fails_closed_before_restoring_any_file(self) -> None:
        scenario = Scenario.create(self.root, running=True)
        self._crash(scenario, "current_published")
        recovery = scenario.recovery()
        snapshot = recovery.root / "database/channel.sqlite3"
        snapshot.write_bytes(b"damaged snapshot")
        candidate_database = scenario.database.read_bytes()
        with activation_environment(scenario) as backend:
            with self.assertRaisesRegex(installer.InstallError, "requires recovery.*snapshot"):
                activate(scenario)
        self.assertNotIn("restore", backend.events)
        self.assertEqual(scenario.database.read_bytes(), candidate_database)
        self.assertEqual(installer._read_release_link(scenario.layout.current, scenario.layout).name, CANDIDATE)
        self.assertEqual(scenario.recovery().id, recovery.id)
        self.assertEqual(snapshot.read_bytes(), b"damaged snapshot")

    def test_database_restore_holds_lifetime_lock_for_entire_write(self) -> None:
        scenario = Scenario.create(self.root, running=True)
        original = scenario.database.read_bytes()
        actual_copy = installer.shutil.copy2
        attempts: list[int] = []
        probe = (
            "import fcntl, os, sys\n"
            "fd = os.open(sys.argv[1], os.O_RDWR)\n"
            "try:\n"
            "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "except BlockingIOError:\n"
            "    sys.exit(42)\n"
            "finally:\n"
            "    os.close(fd)\n"
        )

        def inspect_restore(source: Path, target: Path, **kwargs: object) -> object:
            if Path(target) == scenario.database:
                for _ in range(2):
                    result = subprocess.run(
                        [sys.executable, "-c", probe, str(scenario.layout.lifetime_lock_file)],
                        capture_output=True, timeout=10,
                    )
                    attempts.append(result.returncode)
                    self.assertEqual(result.returncode, 42, result.stderr)
                    if len(attempts) == 1:
                        actual_copy(source, target, **kwargs)
                return target
            return actual_copy(source, target, **kwargs)

        with activation_environment(scenario, point="publish"), patch.object(installer.shutil, "copy2", side_effect=inspect_restore):
            with self.assertRaisesRegex(installer.InstallError, "was rolled back"):
                activate(scenario)
        self.assertEqual(attempts, [42, 42])
        self._assert_original_release(scenario, original)


if __name__ == "__main__":
    unittest.main()
