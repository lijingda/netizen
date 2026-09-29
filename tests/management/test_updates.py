from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from netizen_cli.deployment.restart_worker import assert_no_pending_restart, finish_manual_restart, run
from netizen_cli.deployment.update_executor import UpdateDispatchUnknown, UpdateExecutorError
from netizen_cli.deployment.update_protocol import (
    acquire_install_lock, advance_operation, new_cli_restart_operation,
    read_operation, write_operation,
)
from netizen_cli.management.updates import InstalledPackage, UpdateError, UpdateService


class MaintenanceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = self.home / ".netizen"
        (self.root / "state").mkdir(parents=True, mode=0o700)
        self.current = InstalledPackage("1.2.3", self.home / "env/bin/python",
                                        self.home / "env", self.home / "env/site/netizen_cli")
        self.executor = Mock()
        self.executor.is_active.return_value = True
        self.binding = Mock(return_value=True)
        self.service = self.make_service()

    def make_service(self) -> UpdateService:
        result = UpdateService(root=self.root, home=self.home, current=self.current,
                               executor=self.executor, binding_matches=self.binding)
        self.addAsyncCleanup(result.close)
        return result

    async def test_status_and_check_never_offer_package_updates(self) -> None:
        for status in (await self.service.status(), await self.service.check()):
            self.assertFalse(status["supported"])
            self.assertFalse(status["available"])
            self.assertIsNone(status["latest"])
            self.assertTrue(status["restartAvailable"])
            self.assertEqual(status["current"]["installationId"], self.current.identity)
        with self.assertRaisesRegex(UpdateError, "update_unsupported"):
            await self.service.start(target={"url": "https://untrusted.invalid"})
        self.executor.launch.assert_not_called()

    async def test_restart_uses_bound_python_and_typed_installation_identity_once(self) -> None:
        operation = await self.service.restart(installation_id=self.current.identity)
        self.assertEqual(operation["schema"], 3)
        self.assertEqual(operation["target"], {
            "version": "1.2.3", "installationId": self.current.identity,
        })
        self.executor.launch.assert_called_once_with(operation["operationId"], self.current.python)
        with self.assertRaisesRegex(UpdateError, "update_already_submitted"):
            await self.service.restart(installation_id=self.current.identity)

    async def test_wrong_or_changed_binding_and_stale_identity_do_not_dispatch(self) -> None:
        with self.assertRaisesRegex(UpdateError, "update_installation_changed"):
            await self.service.restart(installation_id="0" * 64)
        self.binding.return_value = False
        with self.assertRaisesRegex(UpdateError, "restart_unsupported"):
            await self.service.restart(installation_id=self.current.identity)
        self.binding.side_effect = [True, False]
        with self.assertRaisesRegex(UpdateError, "update_installation_changed"):
            await self.service.restart(installation_id=self.current.identity)
        self.executor.launch.assert_not_called()

    async def test_environment_id_does_not_collapse_shared_python_symlinks(self) -> None:
        other = InstalledPackage(self.current.version, self.home / "other/bin/python",
                                 self.home / "other", self.current.package)
        self.assertNotEqual(other.identity, self.current.identity)

    async def test_dispatch_unknown_is_not_retried_and_definite_failure_is_recorded(self) -> None:
        self.executor.launch.side_effect = UpdateDispatchUnknown("secret")
        operation = await self.service.restart(installation_id=self.current.identity)
        self.assertEqual(operation["phase"], "accepted")
        self.assertEqual((await self.service.status())["operation"]["phase"], "accepted")
        self.executor.launch.assert_called_once()
        advance_operation(self.root, operation["operationId"], "failed", "dispatch_failed")
        self.executor.launch.side_effect = UpdateExecutorError("secret")
        operation = await self.service.restart(installation_id=self.current.identity)
        self.assertEqual((operation["phase"], operation["code"]), ("failed", "dispatch_failed"))

    async def test_active_lock_preserves_nonterminal_result(self) -> None:
        operation = new_cli_restart_operation(self.current.version, self.current.identity)
        write_operation(self.root, operation)
        lock = acquire_install_lock(self.root)
        try:
            self.assertEqual((await self.service.status())["operation"]["phase"], "accepted")
            with self.assertRaisesRegex(UpdateError, "update_busy"):
                await self.service.restart(installation_id=self.current.identity)
        finally:
            os.close(lock)

    async def test_worker_absence_is_unknown_not_success(self) -> None:
        operation = new_cli_restart_operation(self.current.version, self.current.identity)
        write_operation(self.root, operation)
        self.executor.is_active.return_value = False
        status = await self.service.status()
        self.assertEqual(status["operation"]["code"], "worker_lost")
        self.assertFalse(status["restartAvailable"])

    async def test_only_new_ready_process_can_recover_failed_restart(self) -> None:
        operation = new_cli_restart_operation(self.current.version, self.current.identity)
        write_operation(self.root, operation)
        advance_operation(self.root, operation["operationId"], "restarting")
        restarted = self.make_service()
        advance_operation(self.root, operation["operationId"], "recovery_required", "restart_failed")
        self.service.set_service_ready(True)
        self.assertEqual((await self.service.status())["operation"]["phase"], "recovery_required")
        self.assertEqual((await restarted.status())["operation"]["phase"], "recovery_required")
        restarted.set_service_ready(True)
        status = await restarted.status()
        self.assertEqual((status["operation"]["phase"], status["operation"]["code"]),
                         ("recovered", "service_ready"))
        self.assertTrue(status["restartAvailable"])

    async def test_worker_stops_before_start_and_reports_ready_failure_without_rollback(self) -> None:
        from netizen_cli.cli_services import ServiceError

        manager = Mock()
        manager.inspect.return_value = SimpleNamespace(binding=SimpleNamespace(
            prefix=self.current.prefix, python=self.current.python))
        for fail in (False, True):
            with self.subTest(fail=fail):
                manager.reset_mock()
                manager.start.side_effect = ServiceError("not ready") if fail else None
                operation = new_cli_restart_operation(self.current.version, self.current.identity)
                write_operation(self.root, operation)
                with patch("netizen_cli.management.updates.installed_package", return_value=self.current):
                    self.assertEqual(run(self.root, operation["operationId"], manager=manager), int(fail))
                names = [call[0] for call in manager.mock_calls]
                self.assertEqual(names, ["inspect", "stop", "start"])
                expected = "recovery_required" if fail else "succeeded"
                self.assertEqual(read_operation(self.root)["phase"], expected)

    async def test_worker_does_not_stop_when_target_changed_or_operation_stale(self) -> None:
        operation = new_cli_restart_operation(self.current.version, "0" * 64)
        write_operation(self.root, operation)
        manager = Mock()
        with patch("netizen_cli.management.updates.installed_package", return_value=self.current):
            self.assertEqual(run(self.root, operation["operationId"], manager=manager), 1)
        manager.stop.assert_not_called()
        manager.start.assert_not_called()
        self.assertEqual(read_operation(self.root)["code"], "previous_release_changed")
        self.assertEqual(run(self.root, operation["operationId"], manager=manager), 1)

    async def test_explicit_ready_cli_recovery_keeps_original_target_without_claiming_success(self) -> None:
        operation = new_cli_restart_operation("0.1.0", "0" * 64)
        write_operation(self.root, operation)
        advance_operation(self.root, operation["operationId"], "recovery_required", "worker_lost")
        status = SimpleNamespace(running=True, ready=True, binding=SimpleNamespace(root=self.root))
        self.executor.is_active.return_value = False
        lock = acquire_install_lock(self.root)
        try:
            result = finish_manual_restart(self.root, lock_descriptor=lock, status=status,
                                           executor=self.executor)
        finally:
            os.close(lock)
        self.assertEqual(result["phase"], "recovered")
        self.assertEqual(result["code"], "manual_recovery")
        self.assertEqual(result["target"], operation["target"])
        self.executor.cleanup.assert_called_once_with(operation["operationId"])

    async def test_manual_recovery_cannot_clear_an_active_or_unobserved_worker(self) -> None:
        operation = new_cli_restart_operation("0.1.0", "0" * 64)
        write_operation(self.root, operation)
        advance_operation(self.root, operation["operationId"], "recovery_required", "worker_lost")
        status = SimpleNamespace(running=True, ready=True, binding=SimpleNamespace(root=self.root))
        lock = acquire_install_lock(self.root)
        try:
            for outcome in (True, UpdateExecutorError("unknown")):
                self.executor.is_active.side_effect = [outcome]
                result = finish_manual_restart(self.root, lock_descriptor=lock, status=status,
                                               executor=self.executor)
                self.assertEqual(result["phase"], "recovery_required")
            self.assertEqual(self.executor.cleanup.call_count, 2)
        finally:
            os.close(lock)

    async def test_fresh_pending_restart_blocks_cli_mutation_without_cancelling_dispatch(self) -> None:
        operation = new_cli_restart_operation(self.current.version, self.current.identity)
        write_operation(self.root, operation)
        lock = acquire_install_lock(self.root)
        try:
            with self.assertRaisesRegex(RuntimeError, "pending dispatch"):
                assert_no_pending_restart(self.root, lock_descriptor=lock, now=operation["createdAt"] + 59)
            self.assertEqual(read_operation(self.root), operation)
            ready = SimpleNamespace(running=True, ready=True, binding=SimpleNamespace(root=self.root))
            result = finish_manual_restart(self.root, lock_descriptor=lock, status=ready,
                                           executor=self.executor)
            self.assertEqual(result["phase"], "accepted")
            self.executor.cleanup.assert_not_called()
        finally:
            os.close(lock)

    async def test_stale_dispatch_is_fenced_before_delayed_worker_can_restart_removed_instance(self) -> None:
        operation = new_cli_restart_operation(self.current.version, self.current.identity)
        write_operation(self.root, operation)
        manager = Mock()
        lock = acquire_install_lock(self.root)
        with ThreadPoolExecutor(max_workers=1) as pool:
            delayed = pool.submit(run, self.root, operation["operationId"], manager=manager)
            try:
                result = assert_no_pending_restart(self.root, lock_descriptor=lock,
                                                   now=operation["createdAt"] + 60)
                self.assertEqual((result["phase"], result["code"]), ("failed", "dispatch_failed"))
                # Simulated remove/purge occurs here under the same lock. Only
                # the retained terminal operation and lock are needed to fence
                # this helper; no instance binding or data is reconstructed.
                manager.assert_not_called()
            finally:
                os.close(lock)
            self.assertEqual(delayed.result(timeout=5), 1)
        self.assertEqual(manager.mock_calls, [])
        self.assertEqual(read_operation(self.root), result)

    async def test_abandoned_restarting_phase_is_unknown_until_explicit_ready_recovery(self) -> None:
        operation = new_cli_restart_operation(self.current.version, self.current.identity)
        write_operation(self.root, operation)
        advance_operation(self.root, operation["operationId"], "restarting")
        lock = acquire_install_lock(self.root)
        try:
            result = assert_no_pending_restart(self.root, lock_descriptor=lock)
            self.assertEqual((result["phase"], result["code"]), ("recovery_required", "worker_lost"))
            self.assertEqual(result["target"], operation["target"])
        finally:
            os.close(lock)

    async def test_pending_guard_requires_real_lock_and_does_not_clear_corrupt_records(self) -> None:
        operation = new_cli_restart_operation(self.current.version, self.current.identity)
        write_operation(self.root, operation)
        lock = acquire_install_lock(self.root)
        os.close(lock)
        with self.assertRaises(OSError):
            assert_no_pending_restart(self.root, lock_descriptor=lock)
        self.assertEqual(read_operation(self.root), operation)
        lock = acquire_install_lock(self.root)
        path = self.root / "state" / "update.json"
        path.write_text("{broken", encoding="utf-8")
        try:
            with self.assertRaisesRegex(RuntimeError, "could not read"):
                assert_no_pending_restart(self.root, lock_descriptor=lock)
            self.assertEqual(path.read_text(encoding="utf-8"), "{broken")
        finally:
            os.close(lock)
