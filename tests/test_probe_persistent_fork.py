from __future__ import annotations

import os
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from scripts.probe_persistent_fork import _fixture_config, _save_config_snapshot


class PersistentForkProbeConfigTest(unittest.TestCase):
    def test_process_trust_parses_exact_path_including_dots_spaces_and_quotes(self) -> None:
        cwd = Path('/tmp/a.b/quoted "project" path')
        config = _fixture_config(cwd)
        self.assertEqual(config.cwd, str(cwd))
        self.assertEqual(len(config.config_overrides), 1)
        key, value = config.config_overrides[0].split("=", 1)
        self.assertEqual(key, "projects")
        parsed = tomllib.loads("value=" + value)["value"]
        self.assertEqual(parsed, {str(cwd): {"trust_level": "trusted"}})

    def test_requested_snapshot_is_private_exact_and_never_overwrites(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "codex"
            home.mkdir()
            original = b'# preserve bytes\nmodel = "fixture"\n'
            (home / "config.toml").write_bytes(original)
            output = Path(directory) / "snapshots"
            with patch.dict(os.environ, {"CODEX_HOME": str(home)}):
                _save_config_snapshot(output, "before")
                saved = output / "config-before.toml"
                self.assertEqual(saved.read_bytes(), original)
                self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
                with self.assertRaises(FileExistsError):
                    _save_config_snapshot(output, "before")
                self.assertEqual((home / "config.toml").read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
