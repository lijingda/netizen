"""Old entry points explain migration without touching services or files."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class RetiredDeploymentTests(unittest.TestCase):
    def test_retired_commands_refuse_from_outside_the_source_tree(self) -> None:
        commands = [
            [sys.executable, str(ROOT / "scripts" / name)]
            for name in ("netizen_installer.py", "netizen_updater.py",
                         "netizen_service_launcher.py", "build_release_artifact.py")
        ] + [["/bin/sh", str(ROOT / name)]
             for name in ("install.sh", "dev-install.sh", "service.sh", "uninstall.sh")]
        with tempfile.TemporaryDirectory() as raw:
            working = Path(raw)
            instance = working / ".netizen"
            instance.mkdir()
            sentinel = instance / "unrelated"
            sentinel.write_bytes(b"preserve")
            for command in commands:
                with self.subTest(command=command):
                    result = subprocess.run(command, cwd=working, text=True,
                                            capture_output=True, check=False, timeout=10,
                                            env={**os.environ, "NETIZEN_ROOT": str(instance)})
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("retired", result.stderr.lower())
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(list(working.iterdir()), [instance])
                    self.assertEqual(list(instance.iterdir()), [sentinel])
                    self.assertEqual(sentinel.read_bytes(), b"preserve")
