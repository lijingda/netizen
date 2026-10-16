from __future__ import annotations

from contextlib import redirect_stderr
import io
import signal
import subprocess
import unittest
from unittest.mock import patch

from scripts.probe_sdk_response_recovery import _run_worker


class ResponseRecoveryDeadlineTest(unittest.TestCase):
    def test_timeout_stops_owned_native_process_group_without_retry(self) -> None:
        with (
            patch("scripts.probe_sdk_response_recovery.subprocess.Popen") as popen,
            patch("scripts.probe_sdk_response_recovery.os.killpg") as kill_group,
            redirect_stderr(io.StringIO()) as diagnostics,
        ):
            process = popen.return_value
            process.pid = 12345
            process.wait.side_effect = [subprocess.TimeoutExpired("probe", 75), -9]
            self.assertEqual(_run_worker("cold_permissions", None), 124)
            self.assertEqual(popen.call_count, 1)
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            kill_group.assert_called_once_with(process.pid, signal.SIGKILL)
            self.assertEqual([call.kwargs["timeout"] for call in process.wait.call_args_list], [75, 5])
        self.assertEqual(diagnostics.getvalue(), "FAIL: response recovery probe exceeded 75s deadline.\n")

    def test_worker_failure_is_returned_without_retry_or_signalling(self) -> None:
        with (
            patch("scripts.probe_sdk_response_recovery.subprocess.Popen") as popen,
            patch("scripts.probe_sdk_response_recovery.os.killpg") as kill_group,
        ):
            popen.return_value.wait.return_value = 1
            self.assertEqual(_run_worker("all", None), 1)
            self.assertEqual(popen.call_count, 1)
            kill_group.assert_not_called()

    def test_parent_interruption_or_exception_stops_owned_group_and_propagates(self) -> None:
        for failure in (KeyboardInterrupt(), RuntimeError("parent wait failed")):
            with (
                self.subTest(failure=type(failure).__name__),
                patch("scripts.probe_sdk_response_recovery.subprocess.Popen") as popen,
                patch("scripts.probe_sdk_response_recovery.os.killpg") as kill_group,
            ):
                process = popen.return_value
                process.pid = 12345
                process.wait.side_effect = [failure, -9]
                with self.assertRaises(type(failure)) as raised:
                    _run_worker("cold_permissions", None)
                self.assertIs(raised.exception, failure)
                self.assertEqual(popen.call_count, 1)
                self.assertTrue(popen.call_args.kwargs["start_new_session"])
                kill_group.assert_called_once_with(process.pid, signal.SIGKILL)
                self.assertEqual(process.wait.call_args.kwargs, {"timeout": 5})

    def test_bounded_cleanup_failure_does_not_swallow_parent_interruption(self) -> None:
        with (
            patch("scripts.probe_sdk_response_recovery.subprocess.Popen") as popen,
            patch("scripts.probe_sdk_response_recovery.os.killpg") as kill_group,
        ):
            process = popen.return_value
            process.pid = 12345
            interruption = KeyboardInterrupt()
            process.wait.side_effect = [interruption, subprocess.TimeoutExpired("probe", 5)]
            with self.assertRaises(KeyboardInterrupt) as raised:
                _run_worker("cold_permissions", None)
            self.assertIs(raised.exception, interruption)
            kill_group.assert_called_once_with(process.pid, signal.SIGKILL)
            self.assertEqual(process.wait.call_args.kwargs, {"timeout": 5})


if __name__ == "__main__":
    unittest.main()
