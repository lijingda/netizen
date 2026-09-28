#!/usr/bin/env python3
"""Run the synthetic SDK gates shared by local and target-host validation."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path


def main() -> int:
    scripts = Path(__file__).resolve().parent
    probes = (
        ("probe_sdk_turn_plan.py", "--timeout", "5"),
        (
            "probe_sdk_completion_race.py",
            "--attempts", "20", "--timeout", "3",
        ),
        (
            "probe_sdk_completion_race.py",
            "--read-recovery", "--attempts", "20", "--timeout", "3",
        ),
        (
            "probe_sdk_completion_race.py",
            "--usage-drain", "--attempts", "40", "--timeout", "10",
        ),
    )
    for script, *arguments in probes:
        result = subprocess.run([sys.executable, scripts / script, *arguments])
        if result.returncode:
            return result.returncode
    # This uses a real App Server, but only disposable unauthenticated state.
    # Own the process group so SDK shutdown cannot outlive the probe deadline.
    process = subprocess.Popen(
        [sys.executable, scripts / "probe_skill_roots.py", "--timeout", "15"],
        start_new_session=True,
    )
    try:
        return process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        print("process-local Skill discovery exceeded its deadline", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
