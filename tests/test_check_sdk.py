from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CheckSdkTest(unittest.TestCase):
    def test_probes_inherit_process_context_and_fail_fast_in_order(self) -> None:
        expected = [
            ["probe_sdk_turn_plan.py", "--timeout", "5"],
            [
                "probe_sdk_completion_race.py",
                "--attempts", "20", "--timeout", "3",
            ],
            [
                "probe_sdk_completion_race.py",
                "--read-recovery", "--attempts", "20", "--timeout", "3",
            ],
            [
                "probe_sdk_completion_race.py",
                "--usage-drain", "--attempts", "40", "--timeout", "10",
            ],
        ]
        probe = """\
import json
import os
import sys
from pathlib import Path

log = Path(os.environ["NETIZEN_TEST_PROBE_LOG"])
index = len(log.read_text().splitlines()) + 1 if log.exists() else 1
with log.open("a") as output:
    output.write(json.dumps({
        "argv": [Path(sys.argv[0]).name, *sys.argv[1:]],
        "python": sys.executable,
        "cwd": str(Path.cwd()),
        "pid": os.getpid(),
        "parent": os.getppid(),
    }) + "\\n")
print(f"probe {index}")
sys.exit(17 if index == int(os.environ["NETIZEN_TEST_PROBE_FAIL_AT"]) else 0)
"""
        for fail_at in range(len(expected) + 1):
            with (
                self.subTest(fail_at=fail_at),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory).resolve()
                scripts = root / "scripts"
                scripts.mkdir()
                shutil.copyfile(ROOT / "scripts/check_sdk.py", scripts / "check_sdk.py")
                for script in {command[0] for command in expected}:
                    (scripts / script).write_text(probe, encoding="utf-8")
                log = root / "probes.jsonl"
                result = subprocess.run(
                    [sys.executable, scripts / "check_sdk.py"],
                    cwd=root,
                    env={
                        **os.environ,
                        "NETIZEN_TEST_PROBE_LOG": str(log),
                        "NETIZEN_TEST_PROBE_FAIL_AT": str(fail_at),
                    },
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                records = [json.loads(line) for line in log.read_text().splitlines()]
                count = fail_at or len(expected)
                self.assertEqual(result.returncode, 17 if fail_at else 0, result.stderr)
                self.assertEqual(
                    [record["argv"] for record in records], expected[:count]
                )
                self.assertEqual(
                    {record["python"] for record in records}, {sys.executable}
                )
                self.assertEqual({record["cwd"] for record in records}, {str(root)})
                self.assertEqual(len({record["pid"] for record in records}), count)
                self.assertEqual(len({record["parent"] for record in records}), 1)
                self.assertEqual(
                    result.stdout.splitlines(),
                    [f"probe {i}" for i in range(1, count + 1)],
                )

    @unittest.skipUnless(shutil.which("make"), "make is not installed")
    def test_make_check_runs_the_shared_gate_with_the_selected_python(self) -> None:
        result = subprocess.run(
            ["make", "--no-print-directory", "-n", "check", "PYTHON=/chosen/python"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            [shlex.split(line) for line in result.stdout.splitlines()],
            [
                ["/chosen/python", "-m", "unittest", "discover", "-s", "tests", "-v"],
                ["/chosen/python", "-m", "compileall", "-q", "netizen", "scripts", "tests"],
                ["/chosen/python", "-m", "pip", "check"],
                ["/chosen/python", "scripts/check_sdk.py"],
            ],
        )
