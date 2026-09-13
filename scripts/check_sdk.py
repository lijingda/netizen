#!/usr/bin/env python3
"""Run the synthetic SDK gates shared by local and target-host validation."""

from __future__ import annotations

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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
