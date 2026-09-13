from __future__ import annotations

import io
import subprocess
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from scripts.probe_sdk_completion_race import _driver, _last_client_stage


class CompletionProbeDiagnosticsTest(unittest.TestCase):
    def test_timeout_stage_accepts_only_exact_known_markers(self) -> None:
        known = (
            "unrelated private text\n"
            "SDK_COMPLETION_STAGE=sdk_import\n"
            "SDK_COMPLETION_STAGE=thread_read\n"
            "SDK_COMPLETION_STAGE=private-secret\n"
            "SDK_COMPLETION_STAGE=client_close private-secret\n"
        )
        for stderr, expected in (
            (known, "thread_read"),
            (known.encode(), "thread_read"),
            (b"\xff\nSDK_COMPLETION_STAGE=client_close\n", "client_close"),
            ("private-secret", "unreported"),
            (None, "unreported"),
        ):
            with self.subTest(stderr=stderr):
                self.assertEqual(_last_client_stage(stderr), expected)

    def test_timeout_reports_stage_without_leaking_stderr_or_retrying(self) -> None:
        for read_recovery, stage in ((False, "handle_run"), (True, "thread_read")):
            for as_bytes in (False, True):
                with self.subTest(read_recovery=read_recovery, as_bytes=as_bytes):
                    stderr = f"private-secret\nSDK_COMPLETION_STAGE={stage}\nprivate-tail"
                    error = subprocess.TimeoutExpired(
                        "client", 3,
                        stderr=stderr.encode() if as_bytes else stderr,
                    )
                    output = io.StringIO()
                    with (
                        patch("scripts.probe_sdk_completion_race.subprocess.run", side_effect=error) as run,
                        redirect_stderr(output),
                    ):
                        result = _driver(attempts=20, timeout=3, read_recovery=read_recovery)
                    self.assertEqual(result, 1)
                    self.assertEqual(run.call_count, 1)
                    self.assertEqual(run.call_args.kwargs["timeout"], 3)
                    self.assertEqual(
                        output.getvalue(),
                        "FAIL: completion probe client subprocess timed out "
                        f"(attempt 1; stage={stage}).\n",
                    )
                    self.assertNotIn("private", output.getvalue())
